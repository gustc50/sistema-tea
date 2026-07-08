"""Parser de extratos bancários em PDF.

Extrai o texto com pdfplumber e interpreta linha a linha os layouts de
extrato de Sicoob, Sicredi, Itaú, Inter, Caixa, Banco do Brasil e Santander.
O banco é detectado automaticamente pelo texto do documento.

Convenção dos extratos (visão do correntista): C = crédito = entrada,
D = débito = saída. Bancos que não usam sufixo D/C trazem o valor com sinal
(à esquerda ou, em alguns sistemas, com "-" colado à direita).

Variações tratadas:
  - valor com e sem separador de milhar (1.500,00 / 1500,00), com "R$",
    com sufixo D/C colado ou separado, com sinal antes ou depois;
  - data completa (dd/mm/aaaa), curta no início da linha (dd/mm, ano
    deduzido do período do extrato) e por extenso agrupando o dia (Inter);
  - histórico em várias linhas: complemento na linha seguinte ao valor
    (Sicoob internet banking) ou descrição nas linhas anteriores ao valor
    (extratos de aplicativo, um campo por linha);
  - texto com letras espaçadas ("S A L D O", "1 . 5 0 0 , 0 0");
  - linhas de saldo/cabeçalho/rodapé ignoradas.

Se a extração normal não encontrar transações, o texto é reextraído
preservando o alinhamento das colunas (layout=True) e reprocessado. O texto
extraído fica disponível em 'texto_extraido' para diagnóstico na tela.
"""

import io
import re
import unicodedata
from datetime import datetime

from ofx_parser import parse_valor, BANCOS

# Linhas de saldo: nunca são transações e o valor delas é capturado para a
# conferência de saldo (inicial + movimento = final)
LINHAS_SALDO = [
    'SALDO ANTERIOR', 'SALDO DO DIA', 'SALDO EM', 'SALDO ATUAL', 'SALDO FINAL',
    'SALDO BLOQUEADO', 'SALDO DISPONIVEL', 'SALDO TOTAL', 'S A L D O', 'SDO CTA',
    'SALDO INICIAL', 'SALDO DIA', 'SALDO ANT', 'SALDO A LIBERAR', 'SALDO LIBERADO',
    'BLOQUEADO ANTERIOR',
]
# Totalizadores e cabeçalhos de coluna: nunca são transações
LINHAS_TOTAIS = [
    'TOTAL DE ENTRADAS', 'TOTAL DE SAIDAS', 'TOTAL ENTRADAS', 'TOTAL SAIDAS',
    'RESUMO DO', 'DATA HISTORICO', 'DATA LANCAMENTO', 'DATA DESCRICAO',
    'DATA MOVIMENTO', 'DT. MOVIMENTO', 'DT.MOVIMENTO', 'DATA DOCUMENTO',
]
# Rodapés/avisos: ignorados SOMENTE quando a linha não tem cara de transação
# (sem data + valor). Descrições reais podem conter estas palavras — ex.:
# "PAGAMENTO FOLHA SALARIOS", "PAG. TITULO", "JUROS LIMITE" — e não podem
# ser descartadas.
LINHAS_RODAPE = [
    'EXTRATO DE CONTA', 'EXTRATO CONTA', 'PERIODO DO EXTRATO', 'OUVIDORIA',
    'SAC ', 'SAC:', 'CENTRAL DE ATENDIMENTO', 'PAGINA ', 'PAG.', 'FOLHA ',
    'LIMITE ', 'CHEQUE ESPECIAL', 'APLICACOES AUTOMATICAS', 'ENCERRAMENTO',
    'LANCAMENTOS FUTUROS', 'HTTP://', 'HTTPS://', 'WWW.', 'INTERNET BANKING',
    'ENCARGOS A VENCER', 'PREVISAO ', 'CUSTO EFETIVO', 'HISTORICO DE MOVIMENTACAO',
]
# Linhas de saldo informativas que NÃO representam o saldo contábil da conta
# (não entram na conferência): bloqueios e saldo disponível (inclui limite)
SALDOS_INFORMATIVOS = ['BLOQUEADO', 'LIBERAR', 'DISPONIVEL']

DETECCAO_BANCOS = [
    ('756', ['SICOOB', 'SISTEMA DE COOPERATIVAS DE CREDITO', 'BANCOOB']),
    ('748', ['SICREDI']),
    ('077', ['BANCO INTER', 'BCO INTER', 'INTER&CO', 'INTER & CO']),
    ('341', ['ITAU', 'ITAÚ']),
    ('104', ['CAIXA ECONOMICA FEDERAL', 'CAIXA.GOV.BR', 'EXTRATO POR PERIODO']),
    ('001', ['BANCO DO BRASIL', 'BB.COM.BR', 'BANCODOBRASIL', 'CENTRAL BB']),
    ('033', ['SANTANDER']),
]

MESES = {
    'JANEIRO': '01', 'FEVEREIRO': '02', 'MARCO': '03', 'ABRIL': '04',
    'MAIO': '05', 'JUNHO': '06', 'JULHO': '07', 'AGOSTO': '08',
    'SETEMBRO': '09', 'OUTUBRO': '10', 'NOVEMBRO': '11', 'DEZEMBRO': '12',
}

# Valor monetário: 1.234,56 ou 1234,56, com "R$" e sinal antes ("-R$ 10,00",
# "R$ -10,00"), sinal colado depois ("10,00-") ou letra D/C ("10,00C", "10,00 D").
# O lookbehind impede começar no meio de um número ("1500,00" não vira "500,00").
RE_VALOR = re.compile(
    r'(?P<pre>-\s?)?(?:R\$\s?)?(?P<pre2>-\s?)?(?<![\d.,])'
    r'(?P<num>(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})'
    r'(?P<post>-)?\s*(?P<dc>[DC])?(?=\s|$)',
    re.IGNORECASE)
RE_DATA = re.compile(r'(?<![\d/])(\d{2})/(\d{2})/(\d{4}|\d{2})(?!\d)')
# Data curta (sem ano) só vale no início da linha, padrão de linha de extrato
RE_DATA_CURTA = re.compile(r'^(\d{2})/(\d{2})(?=\s|$)(?!/)')
# Inter: "15 de Janeiro de 2026" como cabeçalho de grupo
RE_DATA_EXTENSO = re.compile(r'\b(\d{1,2})\s+DE\s+([A-ZÇ]+)\s+DE\s+(\d{4})\b')
# "Período: 01/06/2026 a 30/06/2026" (para deduzir o ano das datas curtas)
RE_PERIODO = re.compile(r'(\d{2})/(\d{2})/(\d{4})\s*(?:a|à|ate|até|-|até)\s*(\d{2})/(\d{2})/(\d{4})',
                        re.IGNORECASE)


def _sem_acentos(texto):
    return ''.join(c for c in unicodedata.normalize('NFD', texto)
                   if unicodedata.category(c) != 'Mn')


def extrair_texto_pdf(dados, layout=False):
    """Texto de todas as páginas do PDF (bytes). Levanta ValueError se falhar."""
    try:
        import pdfplumber
    except ImportError:
        raise ValueError('Biblioteca pdfplumber não instalada. Execute: pip install pdfplumber')
    try:
        paginas = []
        with pdfplumber.open(io.BytesIO(dados)) as pdf:
            for pagina in pdf.pages:
                paginas.append(pagina.extract_text(layout=layout) or '')
        texto = '\n'.join(paginas)
    except Exception as e:
        if 'password' in str(e).lower() or 'encrypt' in str(e).lower():
            raise ValueError('O PDF está protegido por senha. Salve uma cópia sem senha '
                             '(ex.: imprimir para PDF) ou use o arquivo OFX do banco.')
        raise ValueError('Não foi possível ler o PDF: %s' % e)
    if not texto.strip():
        raise ValueError('O PDF não contém texto extraível (pode ser um extrato '
                         'escaneado/imagem). Use o arquivo OFX do banco.')
    return texto


def detectar_banco(texto):
    texto_norm = _sem_acentos(texto[:4000]).upper()
    for codigo, palavras in DETECCAO_BANCOS:
        if any(p in texto_norm for p in palavras):
            return codigo
    return ''


def _contem(linha_norm, palavras):
    return any(p in linha_norm for p in palavras)


def _colapsar_letras_espacadas(linha):
    """'1 . 5 0 0 , 0 0 C' -> '1.500,00C' (palavras separadas por 2+ espaços)."""
    tokens = linha.split(' ')
    isolados = sum(1 for t in tokens if len(t) == 1)
    if len(tokens) >= 5 and isolados / len(tokens) > 0.7:
        return re.sub(r'(?<=\S) (?=\S)', '', re.sub(r'  +', '\x00', linha)).replace('\x00', ' ')
    return linha


def _data_iso(dia, mes, ano):
    if len(ano) == 2:
        ano = '20' + ano
    return '%s-%s-%s' % (ano, mes, dia.rjust(2, '0'))


def _extrair_conta_agencia(texto):
    """Tenta achar agência/cooperativa e conta no cabeçalho do extrato."""
    topo = texto[:2500]
    agencia = conta = ''
    m = re.search(r'(?:Ag[êe]ncia|Cooperativa)\s*[:\-]?\s*(\d{2,5}(?:-?[\dXx])?)', topo, re.IGNORECASE)
    if m:
        agencia = m.group(1)
    m = re.search(r'Conta(?:\s+corrente)?\s*[:\-]?\s*([\d.]{2,15}-?[\dXx])', topo, re.IGNORECASE)
    if m:
        conta = m.group(1)
    if not agencia or not conta:
        # Formato "Agência/Conta" ou "Ag/Conta: 1234 / 56789-0"
        m = re.search(r'Ag(?:[êe]ncia)?\s*/\s*(?:Cc?|Conta)\s*[:\-]?\s*(\d{2,5})\s*/\s*([\d.]{2,15}-?[\dXx])',
                      topo, re.IGNORECASE)
        if m:
            agencia = agencia or m.group(1)
            conta = conta or m.group(2)
    return agencia, conta


def _anos_do_periodo(texto):
    """(mes_inicio, ano_inicio, mes_fim, ano_fim) do período do extrato, para
    deduzir o ano de datas curtas dd/mm. None se não encontrado."""
    m = RE_PERIODO.search(texto[:3000]) or RE_PERIODO.search(texto)
    if m:
        return int(m.group(2)), int(m.group(3)), int(m.group(5)), int(m.group(6))
    m = RE_DATA.search(texto)
    if m:
        ano = int(m.group(3)) if len(m.group(3)) == 4 else 2000 + int(m.group(3))
        return 1, ano, 12, ano
    ano = datetime.now().year
    return 1, ano, 12, ano


def _ano_para_mes(mes, periodo):
    """Ano de uma data curta dd/mm, tratando períodos que viram o ano."""
    mes_ini, ano_ini, mes_fim, ano_fim = periodo
    if ano_ini != ano_fim:
        return ano_ini if mes >= mes_ini else ano_fim
    return ano_ini


def _limpar_descricao(linha, spans_remover):
    """Remove da linha os trechos (data/valores) já consumidos."""
    resto = list(linha)
    for ini, fim in spans_remover:
        for i in range(ini, fim):
            resto[i] = ' '
    descricao = re.sub(r'\s+', ' ', ''.join(resto)).strip()
    descricao = descricao.strip('-–|• \t')
    return descricao


def _extrair_documento(descricao):
    """Separa um número de documento no início ou fim da descrição."""
    m = re.match(r'^(\d{4,12})\s+(.+)$', descricao)
    if m:
        return m.group(2).strip(), m.group(1)
    m = re.search(r'\s(\d{4,12})$', descricao)
    if m:
        return descricao[:m.start()].strip(), m.group(1)
    return descricao, ''


def _valor_do_match(m):
    """Valor com sinal a partir de um match de RE_VALOR."""
    valor = parse_valor(m.group('num'))
    if m.group('dc'):
        return abs(valor) if m.group('dc').upper() == 'C' else -abs(valor)
    if m.group('pre') or m.group('pre2') or m.group('post'):
        return -abs(valor)
    return valor


def _parse_texto(texto):
    """Interpreta o texto de um extrato e devolve o formato de parse_ofx()."""
    # Colapsa letras espaçadas no topo para o nome do banco ser reconhecível
    topo = '\n'.join(_colapsar_letras_espacadas(l.strip()) for l in texto[:4000].split('\n'))
    banco = detectar_banco(topo)
    agencia, conta = _extrair_conta_agencia(texto)
    periodo = _anos_do_periodo(texto)

    transacoes = []
    saldos = []          # (nº de transações já lidas, valor) das linhas de saldo
    data_corrente = ''   # layouts com data agrupada (Inter/app) ou omitida na linha
    pendente = None      # transação aguardando possível linha de complemento
    buffer_desc = []     # linhas de texto anteriores a um valor sem descrição

    def fechar_pendente():
        nonlocal pendente
        if pendente:
            pendente.pop('desc_inline', None)
            descricao, documento = _extrair_documento(pendente['descricao'])
            pendente['descricao'] = descricao or 'LANCAMENTO'
            pendente['documento'] = pendente['documento'] or documento
            transacoes.append(pendente)
            pendente = None

    for linha_bruta in texto.split('\n'):
        linha = _colapsar_letras_espacadas(linha_bruta.strip())
        if not linha:
            fechar_pendente()
            buffer_desc = []
            continue
        linha_norm = _sem_acentos(linha).upper()

        # Cabeçalho de data por extenso (Banco Inter)
        m_ext = RE_DATA_EXTENSO.search(linha_norm)
        if m_ext and MESES.get(m_ext.group(2)):
            fechar_pendente()
            data_corrente = _data_iso(m_ext.group(1), MESES[m_ext.group(2)], m_ext.group(3))
            buffer_desc = []
            continue

        valores = list(RE_VALOR.finditer(linha))
        m_data = RE_DATA.search(linha)
        m_curta = None if m_data else RE_DATA_CURTA.match(linha)

        if _contem(linha_norm, LINHAS_SALDO):
            fechar_pendente()
            buffer_desc = []
            if valores and not _contem(linha_norm, SALDOS_INFORMATIVOS):
                # o último valor da linha é o saldo
                saldos.append((len(transacoes), _valor_do_match(valores[-1])))
            continue
        if _contem(linha_norm, LINHAS_TOTAIS):
            fechar_pendente()
            buffer_desc = []
            continue
        # Rodapés só descartam linhas que não têm cara de transação
        if _contem(linha_norm, LINHAS_RODAPE) and not ((m_data or m_curta) and valores):
            fechar_pendente()
            buffer_desc = []
            continue

        # Linha só com a data define o dia dos lançamentos seguintes
        if (m_data or m_curta) and not valores and len(linha) <= 12:
            fechar_pendente()
            if m_data:
                data_corrente = _data_iso(m_data.group(1), m_data.group(2), m_data.group(3))
            else:
                ano = _ano_para_mes(int(m_curta.group(2)), periodo)
                data_corrente = _data_iso(m_curta.group(1), m_curta.group(2), str(ano))
            buffer_desc = []
            continue

        if not valores:
            # Complemento da transação anterior (descrição estava na mesma
            # linha do valor) ou descrição da próxima (extratos de app, em que
            # o valor vem depois, em linha própria)
            if pendente and pendente.get('desc_inline') and not m_data and len(linha) <= 80:
                pendente['descricao'] = (pendente['descricao'] + ' ' + linha).strip()
                fechar_pendente()
            else:
                fechar_pendente()
                if not m_data and len(linha) <= 80:
                    buffer_desc = (buffer_desc + [linha])[-3:]
            continue

        # --- linha com valor: uma transação ---
        if m_data:
            data = _data_iso(m_data.group(1), m_data.group(2), m_data.group(3))
        elif m_curta:
            ano = _ano_para_mes(int(m_curta.group(2)), periodo)
            data = _data_iso(m_curta.group(1), m_curta.group(2), str(ano))
        else:
            data = data_corrente
        if not data:
            continue
        data_corrente = data

        fechar_pendente()

        # Com mais de um valor na linha, o último costuma ser a coluna Saldo.
        # O valor do lançamento é o primeiro.
        alvo = valores[0]
        valor = _valor_do_match(alvo)
        if valor == 0.0:
            continue

        spans = [alvo.span()]
        if len(valores) > 1:
            spans.append(valores[-1].span())  # remove o saldo da descrição
        if m_data:
            spans.append(m_data.span())
        elif m_curta:
            spans.append(m_curta.span())
        descricao = _limpar_descricao(linha, spans)
        desc_inline = bool(descricao)
        if not descricao and buffer_desc:
            descricao = ' '.join(buffer_desc)
        buffer_desc = []

        # Segurança extra contra linhas de saldo não listadas
        if _sem_acentos(descricao).upper().startswith('SALDO'):
            if not _contem(_sem_acentos(descricao).upper(), SALDOS_INFORMATIVOS):
                saldos.append((len(transacoes), valor))
            continue

        pendente = {
            'data': data,
            'valor': round(valor, 2),
            'tipo': 'entrada' if valor > 0 else 'saida',
            'descricao': descricao,
            'documento': '',
            'fitid': '',
            'desc_inline': desc_inline,
        }

    fechar_pendente()

    # Saldo inicial: linha de saldo antes de qualquer transação; saldo final:
    # linha de saldo depois de todas. Com ambos dá para conferir o extrato.
    saldo_inicial = saldo_final = None
    if saldos and saldos[0][0] == 0 and len(saldos) >= 2:
        saldo_inicial = round(saldos[0][1], 2)
        if saldos[-1][0] == len(transacoes):
            saldo_final = round(saldos[-1][1], 2)

    datas = sorted(t['data'] for t in transacoes)
    return {
        'banco_codigo': banco,
        'banco_nome': BANCOS.get(banco, 'Não identificado'),
        'agencia': agencia,
        'conta': conta,
        'data_inicio': datas[0] if datas else '',
        'data_fim': datas[-1] if datas else '',
        'saldo_inicial': saldo_inicial,
        'saldo_final': saldo_final,
        'transacoes': transacoes,
    }


def parse_extrato_pdf(dados):
    """Interpreta o extrato PDF e devolve o mesmo formato de parse_ofx(),
    mais 'texto_extraido' (para diagnóstico quando nada for encontrado).

    Tenta primeiro a extração de texto padrão; se nenhuma transação for
    encontrada, tenta de novo preservando o alinhamento das colunas.
    """
    resultado = None
    texto_usado = ''
    for modo_layout in (False, True):
        texto = extrair_texto_pdf(dados, layout=modo_layout)
        r = _parse_texto(texto)
        if resultado is None or len(r['transacoes']) > len(resultado['transacoes']):
            resultado, texto_usado = r, texto
        if resultado['transacoes']:
            break
    resultado['texto_extraido'] = texto_usado[:6000]
    return resultado

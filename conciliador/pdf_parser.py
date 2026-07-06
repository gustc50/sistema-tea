"""Parser de extratos bancários em PDF.

Extrai o texto com pdfplumber e interpreta linha a linha os layouts de
extrato de Sicoob, Sicredi, Itaú, Inter, Caixa, Banco do Brasil e Santander.
O banco é detectado automaticamente pelo texto do documento.

Convenção dos extratos (visão do correntista): C = crédito = entrada,
D = débito = saída. Bancos que não usam sufixo D/C trazem o valor com sinal.

PDFs de extrato variam entre versões de app/internet banking; o parser usa
um motor genérico (data + valor + descrição por linha, com continuação de
descrição em linhas seguintes) calibrado por banco, e a tela de conferência
permite ajustar o que for necessário antes de exportar.
"""

import io
import re
import unicodedata

from ofx_parser import parse_valor, BANCOS

# Palavras que identificam linhas de saldo/cabeçalho/rodapé, não transações
LINHAS_IGNORADAS = [
    'SALDO ANTERIOR', 'SALDO DO DIA', 'SALDO EM', 'SALDO ATUAL', 'SALDO FINAL',
    'SALDO BLOQUEADO', 'SALDO DISPONIVEL', 'SALDO TOTAL', 'S A L D O', 'SDO CTA',
    'SALDO INICIAL', 'SALDO DIA', 'SALDO ANT', 'TOTAL DE ENTRADAS',
    'TOTAL DE SAIDAS', 'TOTAL ENTRADAS', 'TOTAL SAIDAS', 'RESUMO DO',
    'DATA HISTORICO', 'DATA LANCAMENTO', 'DATA DESCRICAO', 'DATA MOVIMENTO',
    'DT. MOVIMENTO', 'DT.MOVIMENTO', 'EXTRATO DE CONTA', 'EXTRATO CONTA',
    'PERIODO DO EXTRATO', 'OUVIDORIA', 'SAC ', 'CENTRAL DE ATENDIMENTO',
    'PAGINA ', 'PAG.', 'FOLHA ', 'LIMITE ', 'CHEQUE ESPECIAL',
    'APLICACOES AUTOMATICAS', 'ENCERRAMENTO', 'LANCAMENTOS FUTUROS',
]

DETECCAO_BANCOS = [
    ('756', ['SICOOB', 'SISTEMA DE COOPERATIVAS DE CREDITO']),
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

# Valor monetário brasileiro: 1.234,56 (opcionalmente -, R$ e sufixo D/C)
RE_VALOR = re.compile(r'(-?\s*(?:R\$\s*)?-?\d{1,3}(?:\.\d{3})*,\d{2})\s*([DC])?(?=\s|$)', re.IGNORECASE)
RE_DATA = re.compile(r'\b(\d{2})/(\d{2})/(\d{4}|\d{2})\b')
# Inter: "15 de Janeiro de 2026" como cabeçalho de grupo
RE_DATA_EXTENSO = re.compile(r'\b(\d{1,2})\s+DE\s+([A-ZÇ]+)\s+DE\s+(\d{4})\b')


def _sem_acentos(texto):
    return ''.join(c for c in unicodedata.normalize('NFD', texto)
                   if unicodedata.category(c) != 'Mn')


def extrair_texto_pdf(dados):
    """Texto de todas as páginas do PDF (bytes). Levanta ValueError se falhar."""
    try:
        import pdfplumber
    except ImportError:
        raise ValueError('Biblioteca pdfplumber não instalada. Execute: pip install pdfplumber')
    try:
        paginas = []
        with pdfplumber.open(io.BytesIO(dados)) as pdf:
            for pagina in pdf.pages:
                paginas.append(pagina.extract_text() or '')
        texto = '\n'.join(paginas)
    except Exception as e:
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


def _linha_ignorada(linha_norm):
    return any(p in linha_norm for p in LINHAS_IGNORADAS)


def _data_iso(dia, mes, ano):
    if len(ano) == 2:
        ano = '20' + ano
    return '%s-%s-%s' % (ano, mes, dia.rjust(2, '0'))


def _extrair_conta_agencia(texto):
    """Tenta achar agência e conta no cabeçalho do extrato."""
    topo = texto[:2500]
    agencia = conta = ''
    m = re.search(r'Ag[êe]ncia\s*[:\-]?\s*(\d{2,5}(?:-?[\dXx])?)', topo, re.IGNORECASE)
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


def parse_extrato_pdf(dados):
    """Interpreta o extrato PDF e devolve o mesmo formato de parse_ofx()."""
    texto = extrair_texto_pdf(dados)
    banco = detectar_banco(texto)
    agencia, conta = _extrair_conta_agencia(texto)

    transacoes = []
    data_corrente = ''   # para layouts com data agrupada (Inter) ou omitida na linha
    pendente = None      # transação aguardando possíveis linhas de continuação

    def fechar_pendente():
        nonlocal pendente
        if pendente:
            descricao, documento = _extrair_documento(pendente['descricao'])
            pendente['descricao'] = descricao or 'LANCAMENTO'
            pendente['documento'] = pendente['documento'] or documento
            transacoes.append(pendente)
            pendente = None

    for linha_bruta in texto.split('\n'):
        linha = linha_bruta.strip()
        if not linha:
            continue
        linha_norm = _sem_acentos(linha).upper()

        # Cabeçalho de data por extenso (Banco Inter)
        m_ext = RE_DATA_EXTENSO.search(linha_norm)
        if m_ext and MESES.get(m_ext.group(2)):
            fechar_pendente()
            data_corrente = _data_iso(m_ext.group(1), MESES[m_ext.group(2)], m_ext.group(3))
            continue

        if _linha_ignorada(linha_norm):
            fechar_pendente()
            continue

        m_data = RE_DATA.search(linha)
        valores = [m for m in RE_VALOR.finditer(linha)]
        # Data isolada na linha define a data corrente (layouts agrupados)
        if m_data and not valores and len(linha) <= 12:
            fechar_pendente()
            data_corrente = _data_iso(m_data.group(1), m_data.group(2), m_data.group(3))
            continue

        if not valores:
            # Possível continuação da descrição da transação anterior
            if pendente and not m_data and len(linha) <= 80 and not linha_norm.startswith('WWW'):
                pendente['descricao'] = (pendente['descricao'] + ' ' + linha).strip()
                fechar_pendente()
            else:
                fechar_pendente()
            continue

        data = _data_iso(m_data.group(1), m_data.group(2), m_data.group(3)) if m_data else data_corrente
        if not data:
            continue
        if m_data:
            data_corrente = data

        fechar_pendente()

        # Com mais de um valor na linha, o último costuma ser a coluna Saldo.
        # O valor do lançamento é o primeiro; exceção: linha só com 1 valor.
        alvo = valores[0]
        texto_valor, letra_dc = alvo.group(1), alvo.group(2)
        valor = parse_valor(texto_valor.replace('R$', ''))
        if letra_dc:
            valor = abs(valor) if letra_dc.upper() == 'C' else -abs(valor)
        if valor == 0.0:
            continue

        spans = [alvo.span()]
        if len(valores) > 1:
            spans.append(valores[-1].span())  # remove o saldo da descrição
        if m_data:
            spans.append(m_data.span())
        descricao = _limpar_descricao(linha, spans)

        pendente = {
            'data': data,
            'valor': round(valor, 2),
            'tipo': 'entrada' if valor > 0 else 'saida',
            'descricao': descricao,
            'documento': '',
            'fitid': '',
        }

    fechar_pendente()

    datas = sorted(t['data'] for t in transacoes)
    return {
        'banco_codigo': banco,
        'banco_nome': BANCOS.get(banco, 'Não identificado'),
        'agencia': agencia,
        'conta': conta,
        'data_inicio': datas[0] if datas else '',
        'data_fim': datas[-1] if datas else '',
        'saldo_final': None,
        'transacoes': transacoes,
    }

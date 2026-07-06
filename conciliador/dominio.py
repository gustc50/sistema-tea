"""Geração do arquivo TXT de importação de lançamentos contábeis do Domínio.

Layout padrão do importador de lançamentos do Domínio Contábil (Thomson
Reuters), com registros delimitados por pipe:

    |0000|<CNPJ da empresa, só dígitos>|
    |6000|X|||                                    -> abre um lançamento
    |6100|DD/MM/AAAA|conta débito|conta crédito|valor|cód. histórico|complemento||filial|

Cada transação do extrato vira um lançamento de partida dupla:
  - entrada (crédito no extrato): débito = conta contábil do banco,
    crédito = conta de contrapartida;
  - saída (débito no extrato): débito = contrapartida, crédito = banco.

O arquivo é gravado em Windows-1252 com quebras CRLF, como o Domínio espera.
"""

import re


def so_digitos(texto):
    return re.sub(r'\D', '', texto or '')


def _data_br(data_iso):
    """'AAAA-MM-DD' -> 'DD/MM/AAAA'."""
    partes = (data_iso or '').split('-')
    if len(partes) != 3:
        return data_iso or ''
    return '%s/%s/%s' % (partes[2], partes[1], partes[0])


def _valor_br(valor):
    """1234.5 -> '1234,50' (sem separador de milhar, como no layout)."""
    return ('%.2f' % abs(valor)).replace('.', ',')


def _limpar_complemento(texto, limite=512):
    """Complemento não pode conter pipe nem quebras de linha."""
    texto = re.sub(r'[|\r\n\t]+', ' ', texto or '')
    texto = re.sub(r'\s+', ' ', texto).strip()
    return texto[:limite]


def montar_complemento(modelo, transacao):
    """Preenche placeholders {descricao} {documento} {data} {valor} do modelo."""
    modelo = modelo or '{descricao}'
    valores = {
        'descricao': transacao.get('descricao', ''),
        'documento': transacao.get('documento', ''),
        'data': _data_br(transacao.get('data', '')),
        'valor': _valor_br(transacao.get('valor', 0)),
    }
    resultado = modelo
    for chave, valor in valores.items():
        resultado = resultado.replace('{%s}' % chave, str(valor))
    return _limpar_complemento(resultado)


def gerar_txt_dominio(cnpj, lancamentos, filial=''):
    """Gera o conteúdo do TXT (bytes cp1252) a partir dos lançamentos.

    lancamentos: [{data: 'AAAA-MM-DD', debito, credito, valor (float, absoluto),
                   historico (código no Domínio), complemento}]
    """
    cnpj_limpo = so_digitos(cnpj)
    if not cnpj_limpo:
        raise ValueError('CNPJ da empresa não configurado. Preencha em Configurações.')
    if not lancamentos:
        raise ValueError('Nenhum lançamento selecionado para exportação.')

    linhas = ['|0000|%s|' % cnpj_limpo]
    for l in lancamentos:
        linhas.append('|6000|X|||')
        linhas.append('|6100|%s|%s|%s|%s|%s|%s||%s|' % (
            _data_br(l['data']),
            str(l['debito']).strip(),
            str(l['credito']).strip(),
            _valor_br(l['valor']),
            str(l.get('historico') or '').strip(),
            _limpar_complemento(l.get('complemento', '')),
            str(filial or '').strip(),
        ))
    conteudo = '\r\n'.join(linhas) + '\r\n'
    return conteudo.encode('cp1252', 'replace')

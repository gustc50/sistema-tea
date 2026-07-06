"""Parser de extratos OFX (Open Financial Exchange).

Suporta tanto OFX 1.x (SGML, sem tags de fechamento — formato mais comum nos
bancos brasileiros) quanto OFX 2.x (XML). Trata as particularidades dos
arquivos gerados por Sicoob, Sicredi, Itaú, Inter, Caixa, Banco do Brasil e
Santander: codificação (cp1252/latin-1/utf-8), valores com vírgula decimal e
datas com fuso horário ([-03:EST], [-3:BRT] etc.).
"""

import re

BANCOS = {
    '001': 'Banco do Brasil',
    '033': 'Santander',
    '077': 'Inter',
    '104': 'Caixa Econômica Federal',
    '341': 'Itaú',
    '748': 'Sicredi',
    '756': 'Sicoob',
}


def _decodificar(dados):
    """Decodifica os bytes do OFX respeitando o cabeçalho quando possível."""
    cabecalho = dados[:600].decode('latin-1', 'ignore').upper()
    ordem = ['cp1252', 'utf-8', 'latin-1']
    if 'UTF-8' in cabecalho or 'UNICODE' in cabecalho or 'encoding="utf-8"' in cabecalho.lower():
        ordem = ['utf-8', 'cp1252', 'latin-1']
    for codec in ordem:
        try:
            return dados.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return dados.decode('latin-1', 'replace')


def _tag(bloco, nome):
    """Valor de uma tag SGML/XML: vai até a próxima tag ou fim da linha."""
    m = re.search(r'<%s>\s*([^<\r\n]*)' % nome, bloco, re.IGNORECASE)
    return m.group(1).strip() if m else ''


def parse_valor(texto):
    """Converte valores como '1234.56', '-1.234,56' e '1234,56' para float."""
    texto = texto.strip().replace('\xa0', '').replace(' ', '')
    if not texto:
        return 0.0
    negativo = texto.startswith('-')
    texto = texto.lstrip('+-')
    if ',' in texto and '.' in texto:
        # O último separador que aparece é o decimal
        if texto.rfind(',') > texto.rfind('.'):
            texto = texto.replace('.', '').replace(',', '.')
        else:
            texto = texto.replace(',', '')
    elif ',' in texto:
        texto = texto.replace(',', '.')
    try:
        valor = float(texto)
    except ValueError:
        return 0.0
    return -valor if negativo else valor


def _parse_data(texto):
    """DTPOSTED como '20260115120000[-03:EST]' ou '20260115' -> 'AAAA-MM-DD'."""
    m = re.match(r'(\d{4})(\d{2})(\d{2})', texto.strip())
    if not m:
        return ''
    return '%s-%s-%s' % (m.group(1), m.group(2), m.group(3))


def _descricao(bloco):
    """Junta NAME e MEMO sem duplicar quando um contém o outro."""
    name = _tag(bloco, 'NAME')
    memo = _tag(bloco, 'MEMO')
    partes = []
    for p in (name, memo):
        p = re.sub(r'\s+', ' ', p).strip()
        if p and not any(p.lower() in x.lower() or x.lower() in p.lower() for x in partes):
            partes.append(p)
    return ' - '.join(partes)


def parse_ofx(dados):
    """Extrai conta e transações de um arquivo OFX (bytes).

    Retorna: {banco_codigo, banco_nome, agencia, conta, data_inicio, data_fim,
              saldo_final, transacoes: [{data, valor, tipo, descricao,
              documento, fitid}]}
    """
    texto = _decodificar(dados)

    banco = re.sub(r'\D', '', _tag(texto, 'BANKID'))
    # Códigos de banco têm 3 dígitos; alguns OFX trazem zeros à esquerda a mais
    if banco:
        banco = banco.lstrip('0').rjust(3, '0')
    agencia = _tag(texto, 'BRANCHID')
    conta = _tag(texto, 'ACCTID')

    transacoes = []
    # Lookahead cobre SGML sem </STMTTRN> e XML com fechamento
    blocos = re.findall(r'<STMTTRN>(.*?)(?=</STMTTRN>|<STMTTRN>|</BANKTRANLIST>|<LEDGERBAL>|$)',
                        texto, re.IGNORECASE | re.DOTALL)
    for bloco in blocos:
        valor = parse_valor(_tag(bloco, 'TRNAMT'))
        data = _parse_data(_tag(bloco, 'DTPOSTED'))
        if not data:
            continue
        trntype = _tag(bloco, 'TRNTYPE').upper()
        if valor == 0.0:
            continue
        # TRNAMT com sinal é a regra; se vier sem sinal, usa TRNTYPE=DEBIT
        if valor > 0 and trntype == 'DEBIT':
            valor = -valor
        documento = _tag(bloco, 'CHECKNUM') or _tag(bloco, 'REFNUM')
        transacoes.append({
            'data': data,
            'valor': round(valor, 2),
            'tipo': 'entrada' if valor > 0 else 'saida',
            'descricao': _descricao(bloco) or trntype,
            'documento': documento.strip(),
            'fitid': _tag(bloco, 'FITID'),
        })

    saldo_final = None
    m_ledger = re.search(r'<LEDGERBAL>(.*?)(?=</LEDGERBAL>|<AVAILBAL>|</STMTRS>|$)',
                         texto, re.IGNORECASE | re.DOTALL)
    if m_ledger:
        bal = _tag(m_ledger.group(1), 'BALAMT')
        if bal:
            saldo_final = parse_valor(bal)

    datas = sorted(t['data'] for t in transacoes)
    return {
        'banco_codigo': banco,
        'banco_nome': BANCOS.get(banco, 'Banco %s' % banco if banco else 'Não identificado'),
        'agencia': agencia,
        'conta': conta,
        'data_inicio': datas[0] if datas else _parse_data(_tag(texto, 'DTSTART')),
        'data_fim': datas[-1] if datas else _parse_data(_tag(texto, 'DTEND')),
        'saldo_final': saldo_final,
        'transacoes': transacoes,
    }

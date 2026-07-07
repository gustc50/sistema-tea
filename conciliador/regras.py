"""Motor de regras de conciliação ("situações especiais").

Cada regra define critérios (texto contido na descrição ou expressão regular,
tipo de movimento, faixa de valor, conta bancária) e o destino contábil
(conta de contrapartida, código de histórico e modelo de complemento).

As regras são avaliadas em ordem de prioridade (menor número primeiro) e a
primeira que casar com a transação é aplicada. A comparação de texto ignora
maiúsculas/minúsculas e acentos.
"""

import re
import unicodedata


def normalizar(texto):
    texto = ''.join(c for c in unicodedata.normalize('NFD', texto or '')
                    if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', texto).upper().strip()


def chave_descricao(texto):
    """Chave para agrupar transações "iguais": descrição sem acentos, números
    e pontuação — "PIX SOCIO JOAO 03/06" e "PIX SOCIO JOAO 17/06" têm a mesma
    chave. Usada pela memória de classificação e pela cópia em massa da tela."""
    s = re.sub(r'\d+', ' ', normalizar(texto))
    s = re.sub(r'[^A-Z ]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def regra_casa(regra, transacao, conta_bancaria_id=None):
    """True se a transação atende a todos os critérios preenchidos da regra."""
    if not regra.get('ativo', 1):
        return False

    tipo_regra = regra.get('tipo_movimento') or 'ambos'
    if tipo_regra != 'ambos' and tipo_regra != transacao['tipo']:
        return False

    if regra.get('conta_bancaria_id') and conta_bancaria_id \
            and int(regra['conta_bancaria_id']) != int(conta_bancaria_id):
        return False

    valor_abs = abs(transacao['valor'])
    if regra.get('valor_min') is not None and valor_abs < float(regra['valor_min']) - 1e-9:
        return False
    if regra.get('valor_max') is not None and valor_abs > float(regra['valor_max']) + 1e-9:
        return False

    criterio = regra.get('criterio_texto') or ''
    if criterio:
        descricao = transacao.get('descricao', '')
        if regra.get('usar_regex'):
            try:
                if not re.search(criterio, descricao, re.IGNORECASE):
                    return False
            except re.error:
                return False
        else:
            # Vários termos separados por ";" -> basta um casar (OU)
            termos = [normalizar(t) for t in criterio.split(';') if t.strip()]
            desc_norm = normalizar(descricao)
            if termos and not any(t in desc_norm for t in termos):
                return False
    return True


def aplicar_regras(regras, transacoes, conta_bancaria_id=None):
    """Anota em cada transação a primeira regra que casar.

    Preenche: conta_contabil, codigo_historico, complemento_modelo,
    regra_id e regra_nome (vazios se nenhuma regra casar).
    """
    ordenadas = sorted(regras, key=lambda r: (r.get('prioridade') or 999, r.get('id') or 0))
    for t in transacoes:
        t.setdefault('conta_contabil', '')
        t.setdefault('codigo_historico', '')
        t.setdefault('complemento_modelo', '')
        t['regra_id'] = None
        t['regra_nome'] = ''
        for regra in ordenadas:
            if regra_casa(regra, t, conta_bancaria_id):
                t['conta_contabil'] = regra.get('conta_contabil') or ''
                t['codigo_historico'] = regra.get('codigo_historico') or ''
                t['complemento_modelo'] = regra.get('complemento_modelo') or ''
                t['regra_id'] = regra.get('id')
                t['regra_nome'] = regra.get('nome') or ''
                break
    return transacoes

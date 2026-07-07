"""Conciliador Bancário -> Domínio Contábil.

Importa extratos OFX e PDF (Sicoob, Sicredi, Itaú, Inter, Caixa, Banco do
Brasil, Santander), classifica cada transação pelo plano de contas da empresa
e pelas regras de situações especiais, e gera o arquivo TXT de lançamentos
no layout de importação do Domínio.

Multiempresa: cada empresa (cliente do escritório) tem seu próprio plano de
contas, contas bancárias, regras, memória de classificação e configurações.
A empresa ativa é selecionada na interface e enviada em cada chamada da API
(parâmetro `empresa`).

Roda em http://127.0.0.1:5001 (porta diferente do Sistema TEA, que usa 5000).
"""

import hashlib
import os
import re
import sqlite3
from datetime import datetime

from flask import Flask, request, jsonify, send_file, Response

from ofx_parser import parse_ofx, BANCOS
from pdf_parser import parse_extrato_pdf
from dominio import gerar_txt_dominio, montar_complemento, so_digitos
from regras import aplicar_regras, normalizar, chave_descricao

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'conciliador.db')

PLANO_CONTAS_SCHEMA = '''
    CREATE TABLE plano_contas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        empresa_id INTEGER NOT NULL DEFAULT 1 REFERENCES empresas(id),
        codigo TEXT NOT NULL,
        descricao TEXT NOT NULL,
        criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (empresa_id, codigo)
    );
'''


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Cria as tabelas na primeira execução e migra bancos da versão de
    empresa única (config global, tabelas sem empresa_id) para multiempresa."""
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS empresas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            cnpj TEXT,
            filial TEXT,
            conta_padrao TEXT,
            historico_entrada TEXT,
            historico_saida TEXT,
            complemento_padrao TEXT DEFAULT '{descricao}',
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS contas_bancarias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa_id INTEGER NOT NULL DEFAULT 1 REFERENCES empresas(id),
            descricao TEXT NOT NULL,
            banco_codigo TEXT NOT NULL,
            agencia TEXT,
            numero_conta TEXT,
            conta_contabil TEXT NOT NULL,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS regras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa_id INTEGER NOT NULL DEFAULT 1 REFERENCES empresas(id),
            nome TEXT NOT NULL,
            prioridade INTEGER NOT NULL DEFAULT 100,
            criterio_texto TEXT,
            usar_regex INTEGER NOT NULL DEFAULT 0,
            tipo_movimento TEXT NOT NULL DEFAULT 'ambos',
            valor_min REAL,
            valor_max REAL,
            conta_bancaria_id INTEGER REFERENCES contas_bancarias(id),
            conta_contabil TEXT NOT NULL,
            codigo_historico TEXT,
            complemento_modelo TEXT,
            ativo INTEGER NOT NULL DEFAULT 1,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS importacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            empresa_id INTEGER NOT NULL DEFAULT 1 REFERENCES empresas(id),
            arquivo_nome TEXT,
            formato TEXT,
            banco_codigo TEXT,
            banco_nome TEXT,
            conta_bancaria_id INTEGER REFERENCES contas_bancarias(id),
            periodo_inicio TEXT,
            periodo_fim TEXT,
            qtd_lancamentos INTEGER,
            total_entradas REAL,
            total_saidas REAL,
            conteudo_txt BLOB,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS transacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            importacao_id INTEGER NOT NULL REFERENCES importacoes(id),
            hash TEXT NOT NULL,
            fitid TEXT,
            data TEXT NOT NULL,
            descricao TEXT,
            documento TEXT,
            valor REAL NOT NULL,
            tipo TEXT NOT NULL,
            conta_contabil TEXT,
            codigo_historico TEXT,
            complemento TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_transacoes_hash ON transacoes(hash);
    ''')

    def colunas(tabela):
        return [r['name'] for r in conn.execute('PRAGMA table_info(%s)' % tabela).fetchall()]

    # plano_contas antigo tinha UNIQUE só no código; recria com a unicidade
    # por (empresa, código), preservando os dados na empresa 1
    cols_plano = colunas('plano_contas')
    if not cols_plano:
        conn.executescript(PLANO_CONTAS_SCHEMA)
    elif 'empresa_id' not in cols_plano:
        conn.execute('ALTER TABLE plano_contas RENAME TO plano_contas_old')
        conn.executescript(PLANO_CONTAS_SCHEMA)
        conn.execute('INSERT INTO plano_contas (id, empresa_id, codigo, descricao, criado_em) '
                     'SELECT id, 1, codigo, descricao, criado_em FROM plano_contas_old')
        conn.execute('DROP TABLE plano_contas_old')

    # Tabelas criadas na versão de empresa única ganham a coluna empresa_id
    for tabela in ('contas_bancarias', 'regras', 'importacoes'):
        if 'empresa_id' not in colunas(tabela):
            conn.execute('ALTER TABLE %s ADD COLUMN empresa_id INTEGER NOT NULL DEFAULT 1 '
                         'REFERENCES empresas(id)' % tabela)

    # A antiga configuração global (tabela config) vira a primeira empresa
    if not conn.execute('SELECT 1 FROM empresas LIMIT 1').fetchone():
        antiga = None
        if colunas('config'):
            antiga = conn.execute('SELECT * FROM config WHERE id = 1').fetchone()
        if antiga:
            conn.execute('''
                INSERT INTO empresas (id, nome, cnpj, filial, conta_padrao,
                    historico_entrada, historico_saida, complemento_padrao)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            ''', ('Empresa 1', antiga['cnpj'], antiga['filial'], antiga['conta_padrao'],
                  antiga['historico_entrada'], antiga['historico_saida'],
                  antiga['complemento_padrao'] or '{descricao}'))
        else:
            conn.execute("INSERT INTO empresas (id, nome) VALUES (1, 'Empresa 1')")
    conn.execute('DROP TABLE IF EXISTS config')
    conn.commit()
    conn.close()


def _empresa_id(conn):
    """Empresa ativa da requisição (parâmetro `empresa` na query string, no
    form ou no JSON). Se ausente/inválida, usa a primeira cadastrada."""
    valor = request.values.get('empresa')
    if valor is None and request.is_json:
        valor = (request.json or {}).get('empresa')
    try:
        eid = int(valor)
        if conn.execute('SELECT 1 FROM empresas WHERE id = ?', (eid,)).fetchone():
            return eid
    except (TypeError, ValueError):
        pass
    row = conn.execute('SELECT id FROM empresas ORDER BY id LIMIT 1').fetchone()
    return row['id'] if row else None


def _hash_transacao(conta_bancaria_id, t):
    """Identidade da transação para detectar reimportação (duplicidade)."""
    chave = '%s|%s|%.2f|%s' % (
        conta_bancaria_id or '',
        t.get('data', ''),
        t.get('valor', 0.0),
        t.get('fitid') or normalizar(t.get('descricao', ''))[:120],
    )
    return hashlib.sha1(chave.encode('utf-8')).hexdigest()


# --- PÁGINA ---

@app.route('/')
def index():
    return send_file('index.html')


# --- EMPRESAS (CLIENTES) ---

CAMPOS_EMPRESA = ('nome', 'cnpj', 'filial', 'conta_padrao',
                  'historico_entrada', 'historico_saida', 'complemento_padrao')


def _valores_empresa(data):
    nome = (data.get('nome') or '').strip()
    if not nome:
        raise ValueError('Informe o nome da empresa.')
    return (
        nome,
        (data.get('cnpj') or '').strip(),
        (data.get('filial') or '').strip(),
        (data.get('conta_padrao') or '').strip(),
        (data.get('historico_entrada') or '').strip(),
        (data.get('historico_saida') or '').strip(),
        (data.get('complemento_padrao') or '{descricao}').strip(),
    )


@app.route('/api/empresas', methods=['GET'])
def get_empresas():
    conn = get_db()
    rows = conn.execute('SELECT * FROM empresas ORDER BY nome').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/empresas', methods=['POST'])
def add_empresa():
    try:
        valores = _valores_empresa(request.json or {})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    conn = get_db()
    cursor = conn.execute('INSERT INTO empresas (%s) VALUES (%s)' % (
        ', '.join(CAMPOS_EMPRESA), ', '.join('?' * len(CAMPOS_EMPRESA))), valores)
    conn.commit()
    novo_id = cursor.lastrowid
    conn.close()
    return jsonify({'status': 'ok', 'id': novo_id}), 201


@app.route('/api/empresas/<int:eid>', methods=['PUT'])
def update_empresa(eid):
    try:
        valores = _valores_empresa(request.json or {})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    conn = get_db()
    conn.execute('UPDATE empresas SET %s WHERE id = ?' % (
        ', '.join('%s = ?' % c for c in CAMPOS_EMPRESA)), valores + (eid,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/empresas/<int:eid>', methods=['DELETE'])
def delete_empresa(eid):
    """Exclui a empresa e TODOS os seus dados (plano, contas, regras,
    importações). A última empresa não pode ser excluída."""
    conn = get_db()
    total = conn.execute('SELECT COUNT(*) AS n FROM empresas').fetchone()['n']
    if total <= 1:
        conn.close()
        return jsonify({'error': 'Não é possível excluir a única empresa cadastrada.'}), 400
    conn.execute('DELETE FROM transacoes WHERE importacao_id IN '
                 '(SELECT id FROM importacoes WHERE empresa_id = ?)', (eid,))
    for tabela in ('importacoes', 'regras', 'contas_bancarias', 'plano_contas'):
        conn.execute('DELETE FROM %s WHERE empresa_id = ?' % tabela, (eid,))
    conn.execute('DELETE FROM empresas WHERE id = ?', (eid,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


# --- PLANO DE CONTAS ---

@app.route('/api/plano-contas', methods=['GET'])
def get_plano_contas():
    conn = get_db()
    eid = _empresa_id(conn)
    rows = conn.execute('SELECT * FROM plano_contas WHERE empresa_id = ? '
                        'ORDER BY LENGTH(codigo), codigo', (eid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/plano-contas', methods=['POST'])
def add_conta_plano():
    data = request.json or {}
    codigo = (data.get('codigo') or '').strip()
    descricao = (data.get('descricao') or '').strip()
    if not codigo or not descricao:
        return jsonify({'error': 'Informe código e descrição da conta.'}), 400
    conn = get_db()
    eid = _empresa_id(conn)
    try:
        conn.execute('INSERT INTO plano_contas (empresa_id, codigo, descricao) VALUES (?, ?, ?)',
                     (eid, codigo, descricao))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'Já existe uma conta com o código %s nesta empresa.' % codigo}), 409
    conn.close()
    return jsonify({'status': 'ok'}), 201


@app.route('/api/plano-contas/importar', methods=['POST'])
def importar_plano_contas():
    """Importação em massa: uma conta por linha, 'código;descrição' (aceita
    também TAB ou vírgula como separador — formato de colar do Excel)."""
    data = request.json or {}
    texto = data.get('texto') or ''
    substituir = bool(data.get('substituir'))
    contas = []
    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha:
            continue
        partes = re.split(r'[;\t]| {2,}', linha, maxsplit=1)
        if len(partes) < 2:
            partes = linha.split(',', 1)
        if len(partes) < 2:
            continue
        codigo, descricao = partes[0].strip(), partes[1].strip()
        # Ignora cabeçalhos tipo "Código;Descrição"
        if not codigo or not descricao or normalizar(codigo) in ('CODIGO', 'CONTA', 'COD'):
            continue
        contas.append((codigo, descricao))
    if not contas:
        return jsonify({'error': 'Nenhuma conta reconhecida. Use uma conta por linha, '
                                 'no formato: código;descrição'}), 400
    conn = get_db()
    eid = _empresa_id(conn)
    if substituir:
        conn.execute('DELETE FROM plano_contas WHERE empresa_id = ?', (eid,))
    inseridas = atualizadas = 0
    for codigo, descricao in contas:
        cur = conn.execute('UPDATE plano_contas SET descricao = ? WHERE empresa_id = ? AND codigo = ?',
                           (descricao, eid, codigo))
        if cur.rowcount:
            atualizadas += 1
        else:
            conn.execute('INSERT INTO plano_contas (empresa_id, codigo, descricao) VALUES (?, ?, ?)',
                         (eid, codigo, descricao))
            inseridas += 1
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok', 'inseridas': inseridas, 'atualizadas': atualizadas})


@app.route('/api/plano-contas/<int:conta_id>', methods=['DELETE'])
def delete_conta_plano(conta_id):
    conn = get_db()
    conn.execute('DELETE FROM plano_contas WHERE id = ?', (conta_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


# --- CONTAS BANCÁRIAS ---

@app.route('/api/contas-bancarias', methods=['GET'])
def get_contas_bancarias():
    conn = get_db()
    eid = _empresa_id(conn)
    rows = conn.execute('SELECT * FROM contas_bancarias WHERE empresa_id = ? ORDER BY descricao',
                        (eid,)).fetchall()
    conn.close()
    contas = []
    for r in rows:
        c = dict(r)
        c['banco_nome'] = BANCOS.get(c['banco_codigo'], c['banco_codigo'])
        contas.append(c)
    return jsonify(contas)


@app.route('/api/contas-bancarias', methods=['POST'])
def add_conta_bancaria():
    data = request.json or {}
    descricao = (data.get('descricao') or '').strip()
    banco_codigo = (data.get('banco_codigo') or '').strip()
    conta_contabil = (data.get('conta_contabil') or '').strip()
    if not descricao or not banco_codigo or not conta_contabil:
        return jsonify({'error': 'Informe descrição, banco e conta contábil.'}), 400
    conn = get_db()
    eid = _empresa_id(conn)
    conn.execute('''
        INSERT INTO contas_bancarias (empresa_id, descricao, banco_codigo, agencia,
            numero_conta, conta_contabil)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (eid, descricao, banco_codigo, (data.get('agencia') or '').strip(),
          (data.get('numero_conta') or '').strip(), conta_contabil))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'}), 201


@app.route('/api/contas-bancarias/<int:cb_id>', methods=['DELETE'])
def delete_conta_bancaria(cb_id):
    conn = get_db()
    conn.execute('DELETE FROM contas_bancarias WHERE id = ?', (cb_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


# --- REGRAS (SITUAÇÕES ESPECIAIS) ---

CAMPOS_REGRA = ('nome', 'prioridade', 'criterio_texto', 'usar_regex', 'tipo_movimento',
                'valor_min', 'valor_max', 'conta_bancaria_id', 'conta_contabil',
                'codigo_historico', 'complemento_modelo', 'ativo')


def _valores_regra(data):
    nome = (data.get('nome') or '').strip()
    conta_contabil = (data.get('conta_contabil') or '').strip()
    if not nome or not conta_contabil:
        raise ValueError('Informe o nome da regra e a conta contábil de destino.')
    if not (data.get('criterio_texto') or '').strip() and data.get('valor_min') is None \
            and data.get('valor_max') is None and (data.get('tipo_movimento') or 'ambos') == 'ambos':
        raise ValueError('Defina ao menos um critério (texto, tipo ou faixa de valor).')
    def num(campo):
        v = data.get(campo)
        return float(v) if v not in (None, '') else None
    return (
        nome,
        int(data.get('prioridade') or 100),
        (data.get('criterio_texto') or '').strip(),
        1 if data.get('usar_regex') else 0,
        data.get('tipo_movimento') or 'ambos',
        num('valor_min'),
        num('valor_max'),
        int(data['conta_bancaria_id']) if data.get('conta_bancaria_id') else None,
        conta_contabil,
        (data.get('codigo_historico') or '').strip(),
        (data.get('complemento_modelo') or '').strip(),
        1 if data.get('ativo', 1) else 0,
    )


@app.route('/api/regras', methods=['GET'])
def get_regras():
    conn = get_db()
    eid = _empresa_id(conn)
    rows = conn.execute('SELECT * FROM regras WHERE empresa_id = ? ORDER BY prioridade, id',
                        (eid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/regras', methods=['POST'])
def add_regra():
    try:
        valores = _valores_regra(request.json or {})
    except (ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400
    conn = get_db()
    eid = _empresa_id(conn)
    conn.execute('INSERT INTO regras (empresa_id, %s) VALUES (?, %s)' % (
        ', '.join(CAMPOS_REGRA), ', '.join('?' * len(CAMPOS_REGRA))), (eid,) + valores)
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'}), 201


@app.route('/api/regras/<int:regra_id>', methods=['PUT'])
def update_regra(regra_id):
    try:
        valores = _valores_regra(request.json or {})
    except (ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400
    conn = get_db()
    conn.execute('UPDATE regras SET %s WHERE id = ?' % (
        ', '.join('%s = ?' % c for c in CAMPOS_REGRA)), valores + (regra_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/regras/<int:regra_id>', methods=['DELETE'])
def delete_regra(regra_id):
    conn = get_db()
    conn.execute('DELETE FROM regras WHERE id = ?', (regra_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


# --- PROCESSAMENTO DO EXTRATO ---

def _detectar_conta_bancaria(conn, eid, extrato):
    """Casa banco/conta do extrato com o cadastro de contas da empresa."""
    contas = [dict(r) for r in conn.execute(
        'SELECT * FROM contas_bancarias WHERE empresa_id = ?', (eid,)).fetchall()]
    candidatas = contas
    if extrato.get('banco_codigo'):
        por_banco = [c for c in contas if c['banco_codigo'] == extrato['banco_codigo']]
        if por_banco:
            candidatas = por_banco
    digitos_extrato = so_digitos(extrato.get('conta') or '').lstrip('0')
    if digitos_extrato:
        por_numero = [c for c in candidatas
                      if so_digitos(c.get('numero_conta') or '').lstrip('0') == digitos_extrato]
        if len(por_numero) == 1:
            return por_numero[0]
    # Única conta cadastrada só vale se o banco bater (ou se o banco do
    # extrato não foi identificado)
    if len(candidatas) == 1 and (not extrato.get('banco_codigo')
                                 or candidatas[0]['banco_codigo'] == extrato['banco_codigo']):
        return candidatas[0]
    return None


@app.route('/api/processar', methods=['POST'])
def processar_arquivo():
    arquivo = request.files.get('arquivo')
    if not arquivo or not arquivo.filename:
        return jsonify({'error': 'Envie um arquivo OFX ou PDF.'}), 400
    dados = arquivo.read()
    if not dados:
        return jsonify({'error': 'O arquivo enviado está vazio.'}), 400

    nome = arquivo.filename
    extensao = os.path.splitext(nome)[1].lower()
    try:
        if dados[:5] == b'%PDF-' or extensao == '.pdf':
            formato = 'PDF'
            extrato = parse_extrato_pdf(dados)
        elif extensao in ('.ofx', '.qfx') or b'OFX' in dados[:2000].upper():
            formato = 'OFX'
            extrato = parse_ofx(dados)
        else:
            return jsonify({'error': 'Formato não reconhecido. Envie um arquivo .ofx ou .pdf.'}), 400
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    texto_extraido = extrato.pop('texto_extraido', '')
    if not extrato['transacoes']:
        return jsonify({
            'error': 'Nenhuma transação encontrada no arquivo. Se for um PDF escaneado '
                     '(imagem), use o OFX do banco. O texto lido do PDF está abaixo — '
                     'confira se as transações aparecem nele.',
            'texto_extraido': texto_extraido,
        }), 400

    conn = get_db()
    eid = _empresa_id(conn)
    empresa = dict(conn.execute('SELECT * FROM empresas WHERE id = ?', (eid,)).fetchone())

    conta_bancaria = None
    cb_id = request.form.get('conta_bancaria_id')
    if cb_id:
        row = conn.execute('SELECT * FROM contas_bancarias WHERE id = ? AND empresa_id = ?',
                           (cb_id, eid)).fetchone()
        conta_bancaria = dict(row) if row else None
    if not conta_bancaria:
        conta_bancaria = _detectar_conta_bancaria(conn, eid, extrato)

    regras = [dict(r) for r in conn.execute(
        'SELECT * FROM regras WHERE empresa_id = ? AND ativo = 1', (eid,)).fetchall()]
    transacoes = extrato['transacoes']
    aplicar_regras(regras, transacoes, conta_bancaria['id'] if conta_bancaria else None)

    hashes_existentes = set()
    if conta_bancaria:
        rows = conn.execute('SELECT hash FROM transacoes t JOIN importacoes i ON i.id = t.importacao_id '
                            'WHERE i.conta_bancaria_id = ?', (conta_bancaria['id'],)).fetchall()
        hashes_existentes = {r['hash'] for r in rows}

    # Memória de classificação da empresa: descrições já exportadas antes
    # sugerem a mesma conta/histórico. Regras têm prioridade; a mais recente vence.
    memoria = {}
    for r in conn.execute(
            "SELECT t.descricao, t.conta_contabil, t.codigo_historico FROM transacoes t "
            "JOIN importacoes i ON i.id = t.importacao_id "
            "WHERE i.empresa_id = ? AND t.conta_contabil IS NOT NULL AND t.conta_contabil != '' "
            "ORDER BY t.id", (eid,)).fetchall():
        k = chave_descricao(r['descricao'] or '')
        if k:
            memoria[k] = (r['conta_contabil'], r['codigo_historico'] or '')

    for t in transacoes:
        t['origem'] = 'regra' if t['regra_id'] else ''
        if not t['conta_contabil']:
            k = chave_descricao(t.get('descricao') or '')
            if k in memoria:
                t['conta_contabil'], hist_memoria = memoria[k]
                t['codigo_historico'] = t['codigo_historico'] or hist_memoria
                t['origem'] = 'memoria'
                t['regra_nome'] = 'Memória (classificação anterior)'
        if not t['conta_contabil'] and empresa.get('conta_padrao'):
            t['conta_contabil'] = empresa['conta_padrao']
            t['regra_nome'] = t['regra_nome'] or 'Conta padrão (não identificado)'
        if not t['codigo_historico']:
            t['codigo_historico'] = (empresa.get('historico_entrada') if t['tipo'] == 'entrada'
                                     else empresa.get('historico_saida')) or ''
        modelo = t.pop('complemento_modelo', '') or empresa.get('complemento_padrao') or '{descricao}'
        t['complemento'] = montar_complemento(modelo, t)
        t['hash'] = _hash_transacao(conta_bancaria['id'] if conta_bancaria else None, t)
        t['duplicada'] = t['hash'] in hashes_existentes
    conn.close()

    return jsonify({
        'empresa_id': eid,
        'arquivo_nome': nome,
        'formato': formato,
        'banco_codigo': extrato['banco_codigo'],
        'banco_nome': extrato['banco_nome'],
        'agencia_extrato': extrato['agencia'],
        'conta_extrato': extrato['conta'],
        'periodo_inicio': extrato['data_inicio'],
        'periodo_fim': extrato['data_fim'],
        'saldo_final': extrato['saldo_final'],
        'conta_bancaria_id': conta_bancaria['id'] if conta_bancaria else None,
        'transacoes': transacoes,
    })


# --- EXPORTAÇÃO PARA O DOMÍNIO ---

@app.route('/api/exportar', methods=['POST'])
def exportar_dominio():
    data = request.json or {}
    transacoes = data.get('transacoes') or []
    conta_bancaria_id = data.get('conta_bancaria_id')
    if not conta_bancaria_id:
        return jsonify({'error': 'Selecione a conta bancária do extrato antes de exportar.'}), 400

    conn = get_db()
    eid = _empresa_id(conn)
    empresa = dict(conn.execute('SELECT * FROM empresas WHERE id = ?', (eid,)).fetchone())
    cb = conn.execute('SELECT * FROM contas_bancarias WHERE id = ? AND empresa_id = ?',
                      (conta_bancaria_id, eid)).fetchone()
    if not cb:
        conn.close()
        return jsonify({'error': 'Conta bancária não encontrada nesta empresa. '
                                 'Confira a empresa selecionada.'}), 400
    conta_banco = cb['conta_contabil']

    selecionadas = [t for t in transacoes if not t.get('ignorar')]
    if not selecionadas:
        conn.close()
        return jsonify({'error': 'Nenhuma transação selecionada para lançamento.'}), 400

    sem_conta = [t for t in selecionadas if not (t.get('conta_contabil') or '').strip()]
    if sem_conta:
        conn.close()
        exemplos = '; '.join('%s %s' % (t.get('data', ''), (t.get('descricao') or '')[:40])
                             for t in sem_conta[:3])
        return jsonify({'error': '%d transação(ões) sem conta contábil definida (ex.: %s). '
                                 'Preencha a conta ou defina uma conta padrão em Configurações.'
                                 % (len(sem_conta), exemplos)}), 400

    lancamentos = []
    for t in selecionadas:
        contrapartida = t['conta_contabil'].strip()
        if t['tipo'] == 'entrada':
            debito, credito = conta_banco, contrapartida
        else:
            debito, credito = contrapartida, conta_banco
        lancamentos.append({
            'data': t['data'],
            'debito': debito,
            'credito': credito,
            'valor': abs(float(t['valor'])),
            'historico': t.get('codigo_historico') or '',
            'complemento': t.get('complemento') or t.get('descricao') or '',
        })

    try:
        conteudo = gerar_txt_dominio(empresa.get('cnpj'), lancamentos, empresa.get('filial') or '')
    except ValueError as e:
        conn.close()
        return jsonify({'error': str(e)}), 400

    total_entradas = sum(t['valor'] for t in selecionadas if t['tipo'] == 'entrada')
    total_saidas = sum(abs(t['valor']) for t in selecionadas if t['tipo'] == 'saida')
    cursor = conn.execute('''
        INSERT INTO importacoes (empresa_id, arquivo_nome, formato, banco_codigo, banco_nome,
            conta_bancaria_id, periodo_inicio, periodo_fim, qtd_lancamentos,
            total_entradas, total_saidas, conteudo_txt)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (eid, data.get('arquivo_nome'), data.get('formato'), data.get('banco_codigo'),
          data.get('banco_nome'), conta_bancaria_id, data.get('periodo_inicio'),
          data.get('periodo_fim'), len(lancamentos), round(total_entradas, 2),
          round(total_saidas, 2), conteudo))
    importacao_id = cursor.lastrowid
    for t in selecionadas:
        conn.execute('''
            INSERT INTO transacoes (importacao_id, hash, fitid, data, descricao, documento,
                valor, tipo, conta_contabil, codigo_historico, complemento)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (importacao_id, t.get('hash') or _hash_transacao(conta_bancaria_id, t),
              t.get('fitid'), t['data'], t.get('descricao'), t.get('documento'),
              float(t['valor']), t['tipo'], t['conta_contabil'],
              t.get('codigo_historico'), t.get('complemento')))
    conn.commit()
    conn.close()

    nome_txt = 'dominio_%s_%s.txt' % (so_digitos(empresa.get('cnpj'))[:8]
                                      or re.sub(r'\W+', '_', empresa.get('nome') or 'lancamentos')[:20],
                                      datetime.now().strftime('%Y%m%d_%H%M%S'))
    return Response(conteudo, mimetype='text/plain; charset=windows-1252', headers={
        'Content-Disposition': 'attachment; filename="%s"' % nome_txt,
        'X-Importacao-Id': str(importacao_id),
    })


# --- HISTÓRICO DE IMPORTAÇÕES ---

@app.route('/api/importacoes', methods=['GET'])
def get_importacoes():
    conn = get_db()
    eid = _empresa_id(conn)
    rows = conn.execute('''
        SELECT i.id, i.arquivo_nome, i.formato, i.banco_nome, i.periodo_inicio,
               i.periodo_fim, i.qtd_lancamentos, i.total_entradas, i.total_saidas,
               i.criado_em, cb.descricao AS conta_bancaria
        FROM importacoes i
        LEFT JOIN contas_bancarias cb ON cb.id = i.conta_bancaria_id
        WHERE i.empresa_id = ?
        ORDER BY i.id DESC
    ''', (eid,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/importacoes/<int:imp_id>/arquivo', methods=['GET'])
def baixar_importacao(imp_id):
    conn = get_db()
    row = conn.execute('SELECT conteudo_txt FROM importacoes WHERE id = ?', (imp_id,)).fetchone()
    conn.close()
    if not row or not row['conteudo_txt']:
        return jsonify({'error': 'Importação não encontrada.'}), 404
    return Response(row['conteudo_txt'], mimetype='text/plain; charset=windows-1252', headers={
        'Content-Disposition': 'attachment; filename="dominio_importacao_%d.txt"' % imp_id,
    })


@app.route('/api/importacoes/<int:imp_id>', methods=['DELETE'])
def delete_importacao(imp_id):
    """Exclui a importação e suas transações (as transações voltam a não ser
    consideradas duplicadas em novos processamentos)."""
    conn = get_db()
    conn.execute('DELETE FROM transacoes WHERE importacao_id = ?', (imp_id,))
    conn.execute('DELETE FROM importacoes WHERE id = ?', (imp_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/bancos', methods=['GET'])
def get_bancos():
    return jsonify([{'codigo': c, 'nome': n} for c, n in sorted(BANCOS.items(), key=lambda x: x[1])])


# Garante o esquema também quando o app é servido por `flask run`/WSGI
init_db()

if __name__ == '__main__':
    print('\n✅ Conciliador Bancário iniciado!')
    print('📂 Banco de dados:', DB_PATH)
    print('🌐 Acesse no navegador: http://127.0.0.1:5001\n')
    app.run(debug=True, port=5001)

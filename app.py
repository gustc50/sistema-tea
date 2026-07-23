from flask import Flask, request, jsonify, send_file
import sqlite3
import os
import json
import io
import zipfile
from xml.sax.saxutils import escape as xml_escape

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prontuario_tea.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

NOTES_SCHEMA = '''
    CREATE TABLE notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id INTEGER NOT NULL,
        appointment_at TIMESTAMP NOT NULL,
        content TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (patient_id) REFERENCES patients(id)
    );
'''

def init_db():
    """Cria as tabelas automaticamente na primeira execução"""
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            dob TEXT,
            contact TEXT,
            patient_since TEXT,
            address TEXT,
            guardian_name TEXT,
            guardian_cpf TEXT,
            guardian_dob TEXT,
            guardian_relationship TEXT,
            payment_responsible TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            score INTEGER,
            max_score INTEGER,
            interpretation TEXT,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        );
        CREATE TABLE IF NOT EXISTS professionals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            profession TEXT NOT NULL,
            council_registration TEXT,
            dob TEXT,
            address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS professional_modules (
            professional_id INTEGER NOT NULL,
            module TEXT NOT NULL,
            PRIMARY KEY (professional_id, module),
            FOREIGN KEY (professional_id) REFERENCES professionals(id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            clinic_open_time TEXT NOT NULL DEFAULT '08:00',
            clinic_close_time TEXT NOT NULL DEFAULT '18:00',
            session_duration_minutes INTEGER NOT NULL DEFAULT 50,
            buffer_minutes INTEGER NOT NULL DEFAULT 10,
            session_price REAL NOT NULL DEFAULT 0,
            clinic_name TEXT,
            clinic_document TEXT,
            clinic_logo TEXT,
            clinic_street TEXT,
            clinic_number TEXT,
            clinic_neighborhood TEXT,
            clinic_zip TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            professional_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            paid INTEGER NOT NULL DEFAULT 0,
            paid_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id),
            FOREIGN KEY (professional_id) REFERENCES professionals(id)
        );
    ''')
    conn.commit()
    conn.execute('INSERT OR IGNORE INTO settings (id) VALUES (1)')
    conn.commit()

    # Bancos criados antes dos dados da clínica (nome, CNPJ/CPF, logo,
    # endereço) existirem não têm essas colunas em `settings`.
    settings_cols = [r['name'] for r in conn.execute("PRAGMA table_info(settings)").fetchall()]
    for col in ('clinic_name', 'clinic_document', 'clinic_logo', 'clinic_street',
                'clinic_number', 'clinic_neighborhood', 'clinic_zip'):
        if col not in settings_cols:
            conn.execute(f'ALTER TABLE settings ADD COLUMN {col} TEXT')
    if 'session_price' not in settings_cols:
        conn.execute('ALTER TABLE settings ADD COLUMN session_price REAL DEFAULT 0')
    conn.commit()

    # Bancos criados antes do módulo Financeiro não têm as colunas de
    # controle de pagamento em `appointments`.
    appt_cols = [r['name'] for r in conn.execute("PRAGMA table_info(appointments)").fetchall()]
    if 'paid' not in appt_cols:
        conn.execute('ALTER TABLE appointments ADD COLUMN paid INTEGER DEFAULT 0')
    if 'paid_date' not in appt_cols:
        conn.execute('ALTER TABLE appointments ADD COLUMN paid_date TEXT')
    conn.commit()

    # Bancos criados antes do campo "paciente desde" existir não têm essa
    # coluna. Adiciona e faz um valor razoável (data de cadastro) para quem
    # já estava na base, já que a data real de início não é conhecida.
    patient_cols = [r['name'] for r in conn.execute("PRAGMA table_info(patients)").fetchall()]
    if 'patient_since' not in patient_cols:
        conn.execute('ALTER TABLE patients ADD COLUMN patient_since TEXT')
        conn.execute("UPDATE patients SET patient_since = date(created_at) WHERE patient_since IS NULL")
        conn.commit()

    # Bancos criados antes do endereço e do bloco "Responsável" existirem
    # não têm essas colunas.
    patient_cols = [r['name'] for r in conn.execute("PRAGMA table_info(patients)").fetchall()]
    for col in ('address', 'guardian_name', 'guardian_cpf', 'guardian_dob',
                'guardian_relationship', 'payment_responsible'):
        if col not in patient_cols:
            conn.execute(f'ALTER TABLE patients ADD COLUMN {col} TEXT')
    conn.commit()

    # A tabela `notes` mudou de "1 nota mutável por paciente" para "vários
    # atendimentos imutáveis, com data/hora". Se o banco já existir com o
    # esquema antigo (sem coluna `id`), migra os dados preservando a nota
    # existente como o primeiro atendimento registrado.
    note_cols = [r['name'] for r in conn.execute("PRAGMA table_info(notes)").fetchall()]
    if not note_cols:
        conn.executescript(NOTES_SCHEMA)
    elif 'id' not in note_cols:
        conn.execute('ALTER TABLE notes RENAME TO notes_old')
        conn.executescript(NOTES_SCHEMA)
        old_rows = conn.execute(
            "SELECT patient_id, content, updated_at FROM notes_old WHERE content IS NOT NULL AND content != ''"
        ).fetchall()
        for row in old_rows:
            conn.execute(
                'INSERT INTO notes (patient_id, appointment_at, content, created_at) VALUES (?, ?, ?, ?)',
                (row['patient_id'], row['updated_at'], row['content'], row['updated_at'])
            )
        conn.execute('DROP TABLE notes_old')
        conn.commit()

    # Notas registradas antes de existir a tabela `professionals` não têm
    # profissional vinculado; ficam como "profissional não informado".
    note_cols = [r['name'] for r in conn.execute("PRAGMA table_info(notes)").fetchall()]
    if 'professional_id' not in note_cols:
        conn.execute('ALTER TABLE notes ADD COLUMN professional_id INTEGER REFERENCES professionals(id)')
        conn.commit()

    conn.close()

# --- ROTAS DA API ---

@app.route('/')
def index():
    return send_file('index.html')

@app.route('/api/patients', methods=['GET'])
def get_patients():
    conn = get_db()
    patients = conn.execute('SELECT * FROM patients ORDER BY name').fetchall()
    conn.close()
    return jsonify([dict(p) for p in patients])

@app.route('/api/patients', methods=['POST'])
def add_patient():
    data = request.json
    name = (data.get('name') or '').strip()
    patient_since = (data.get('patient_since') or '').strip()
    if not name or not patient_since:
        return jsonify({'error': 'name e patient_since são obrigatórios'}), 400
    conn = get_db()
    cursor = conn.execute('''
        INSERT INTO patients (name, dob, contact, patient_since, address,
            guardian_name, guardian_cpf, guardian_dob, guardian_relationship, payment_responsible)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        name, data.get('dob'), data.get('contact'), patient_since, data.get('address'),
        data.get('guardian_name'), data.get('guardian_cpf'), data.get('guardian_dob'),
        data.get('guardian_relationship'), data.get('payment_responsible')
    ))
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return jsonify({
        'id': new_id, 'name': name, 'dob': data.get('dob'), 'contact': data.get('contact'),
        'patient_since': patient_since, 'address': data.get('address'),
        'guardian_name': data.get('guardian_name'), 'guardian_cpf': data.get('guardian_cpf'),
        'guardian_dob': data.get('guardian_dob'), 'guardian_relationship': data.get('guardian_relationship'),
        'payment_responsible': data.get('payment_responsible')
    }), 201

@app.route('/api/tests', methods=['POST'])
def save_test():
    data = request.json
    conn = get_db()
    conn.execute(
        'INSERT INTO tests (patient_id, type, score, max_score, interpretation) VALUES (?, ?, ?, ?, ?)',
        (data['patientId'], data['type'], data['score'], data['max'], data['interpretation'])
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'}), 201

@app.route('/api/tests/<int:patient_id>', methods=['GET'])
def get_tests(patient_id):
    conn = get_db()
    tests = conn.execute('SELECT * FROM tests WHERE patient_id = ? ORDER BY date DESC', (patient_id,)).fetchall()
    conn.close()
    return jsonify([dict(t) for t in tests])

@app.route('/api/notes/<int:patient_id>', methods=['GET'])
def get_notes(patient_id):
    conn = get_db()
    notes = conn.execute(
        'SELECT id, patient_id, appointment_at, content, professional_id, created_at FROM notes WHERE patient_id = ? ORDER BY appointment_at DESC',
        (patient_id,)
    ).fetchall()
    conn.close()
    return jsonify([dict(n) for n in notes])

@app.route('/api/notes/<int:patient_id>', methods=['POST'])
def save_notes(patient_id):
    data = request.json
    appointment_at = (data.get('appointment_at') or '').strip()
    content = (data.get('content') or '').strip()
    professional_id = data.get('professional_id')
    if not appointment_at or not content or not professional_id:
        return jsonify({'error': 'appointment_at, content e professional_id são obrigatórios'}), 400
    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO notes (patient_id, appointment_at, content, professional_id) VALUES (?, ?, ?, ?)',
        (patient_id, appointment_at, content, professional_id)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return jsonify({'id': new_id, 'patient_id': patient_id, 'appointment_at': appointment_at, 'content': content, 'professional_id': professional_id}), 201

@app.route('/api/professionals', methods=['GET'])
def get_professionals():
    conn = get_db()
    professionals = conn.execute('SELECT * FROM professionals ORDER BY full_name').fetchall()
    conn.close()
    return jsonify([dict(p) for p in professionals])

@app.route('/api/professionals', methods=['POST'])
def add_professional():
    data = request.json
    full_name = (data.get('full_name') or '').strip()
    profession = (data.get('profession') or '').strip()
    if not full_name or not profession:
        return jsonify({'error': 'full_name e profession são obrigatórios'}), 400
    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO professionals (full_name, profession, council_registration, dob, address) VALUES (?, ?, ?, ?, ?)',
        (full_name, profession, data.get('council_registration'), data.get('dob'), data.get('address'))
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return jsonify({
        'id': new_id, 'full_name': full_name, 'profession': profession,
        'council_registration': data.get('council_registration'),
        'dob': data.get('dob'), 'address': data.get('address')
    }), 201

@app.route('/api/professional-modules/<int:professional_id>', methods=['GET'])
def get_professional_modules(professional_id):
    conn = get_db()
    rows = conn.execute(
        'SELECT module FROM professional_modules WHERE professional_id = ?', (professional_id,)
    ).fetchall()
    conn.close()
    return jsonify([r['module'] for r in rows])

@app.route('/api/professional-modules/<int:professional_id>', methods=['POST'])
def set_professional_modules(professional_id):
    data = request.json
    modules = data.get('modules') or []
    conn = get_db()
    conn.execute('DELETE FROM professional_modules WHERE professional_id = ?', (professional_id,))
    conn.executemany(
        'INSERT INTO professional_modules (professional_id, module) VALUES (?, ?)',
        [(professional_id, m) for m in modules]
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/settings', methods=['GET'])
def get_settings():
    conn = get_db()
    row = conn.execute('SELECT * FROM settings WHERE id = 1').fetchone()
    conn.close()
    return jsonify(dict(row))

@app.route('/api/settings', methods=['POST'])
def update_settings():
    data = request.json
    clinic_open_time = (data.get('clinic_open_time') or '').strip()
    clinic_close_time = (data.get('clinic_close_time') or '').strip()
    try:
        session_duration_minutes = int(data.get('session_duration_minutes'))
        buffer_minutes = int(data.get('buffer_minutes'))
        session_price = float(data.get('session_price') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'session_duration_minutes, buffer_minutes e session_price devem ser números'}), 400
    if not clinic_open_time or not clinic_close_time or session_duration_minutes <= 0 or buffer_minutes < 0 or session_price < 0:
        return jsonify({'error': 'Dados de configuração inválidos'}), 400
    conn = get_db()
    conn.execute('''
        UPDATE settings SET clinic_open_time = ?, clinic_close_time = ?,
            session_duration_minutes = ?, buffer_minutes = ?, session_price = ?,
            clinic_name = ?, clinic_document = ?, clinic_logo = ?,
            clinic_street = ?, clinic_number = ?, clinic_neighborhood = ?, clinic_zip = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
    ''', (
        clinic_open_time, clinic_close_time, session_duration_minutes, buffer_minutes, session_price,
        data.get('clinic_name'), data.get('clinic_document'), data.get('clinic_logo'),
        data.get('clinic_street'), data.get('clinic_number'), data.get('clinic_neighborhood'), data.get('clinic_zip')
    ))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

def _time_to_minutes(t):
    h, m = t.split(':')
    return int(h) * 60 + int(m)

def _minutes_to_time(mins):
    return f'{mins // 60:02d}:{mins % 60:02d}'

@app.route('/api/appointments', methods=['GET'])
def get_appointments():
    date = request.args.get('date')
    professional_id = request.args.get('professional_id')
    query = 'SELECT * FROM appointments WHERE 1=1'
    params = []
    if date:
        query += ' AND date = ?'
        params.append(date)
    if professional_id:
        query += ' AND professional_id = ?'
        params.append(professional_id)
    query += ' ORDER BY start_time'
    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/appointments', methods=['POST'])
def add_appointment():
    data = request.json
    patient_id = data.get('patient_id')
    professional_id = data.get('professional_id')
    date = (data.get('date') or '').strip()
    start_time = (data.get('start_time') or '').strip()
    if not patient_id or not professional_id or not date or not start_time:
        return jsonify({'error': 'patient_id, professional_id, date e start_time são obrigatórios'}), 400

    conn = get_db()
    settings = conn.execute('SELECT * FROM settings WHERE id = 1').fetchone()
    duration = settings['session_duration_minutes']
    buffer_minutes = settings['buffer_minutes']

    start_min = _time_to_minutes(start_time)
    end_min = start_min + duration
    end_time = _minutes_to_time(end_min)

    existing = conn.execute(
        'SELECT start_time, end_time FROM appointments WHERE professional_id = ? AND date = ?',
        (professional_id, date)
    ).fetchall()
    new_block_end = end_min + buffer_minutes
    for row in existing:
        ex_start = _time_to_minutes(row['start_time'])
        ex_block_end = _time_to_minutes(row['end_time']) + buffer_minutes
        if start_min < ex_block_end and ex_start < new_block_end:
            conn.close()
            return jsonify({
                'error': f'Conflito de horário: já existe um atendimento das {row["start_time"]} às {row["end_time"]} '
                         f'para esse profissional (considerando o tempo de troca).'
            }), 409

    cursor = conn.execute(
        'INSERT INTO appointments (patient_id, professional_id, date, start_time, end_time) VALUES (?, ?, ?, ?, ?)',
        (patient_id, professional_id, date, start_time, end_time)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return jsonify({
        'id': new_id, 'patient_id': patient_id, 'professional_id': professional_id,
        'date': date, 'start_time': start_time, 'end_time': end_time
    }), 201

@app.route('/api/appointments/<int:appointment_id>', methods=['DELETE'])
def delete_appointment(appointment_id):
    conn = get_db()
    conn.execute('DELETE FROM appointments WHERE id = ?', (appointment_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/financeiro', methods=['GET'])
def get_financeiro():
    year = request.args.get('year')
    month = request.args.get('month')
    if not year or not month:
        return jsonify({'error': 'year e month são obrigatórios'}), 400
    try:
        month_prefix = f'{int(year):04d}-{int(month):02d}'
    except ValueError:
        return jsonify({'error': 'year e month inválidos'}), 400
    conn = get_db()
    rows = conn.execute('''
        SELECT a.id, a.patient_id, a.professional_id, a.date, a.start_time, a.end_time,
               a.paid, a.paid_date, p.name AS patient_name, p.payment_responsible, p.guardian_name
        FROM appointments a
        JOIN patients p ON p.id = a.patient_id
        WHERE a.date LIKE ?
        ORDER BY a.date, a.start_time
    ''', (month_prefix + '%',)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/appointments/<int:appointment_id>/pay', methods=['POST'])
def pay_appointment(appointment_id):
    data = request.json
    paid_date = (data.get('paid_date') or '').strip()
    if not paid_date:
        return jsonify({'error': 'paid_date é obrigatório'}), 400
    conn = get_db()
    conn.execute('UPDATE appointments SET paid = 1, paid_date = ? WHERE id = ?', (paid_date, appointment_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/appointments/pay-all', methods=['POST'])
def pay_all_appointments():
    data = request.json
    year = data.get('year')
    month = data.get('month')
    paid_date = (data.get('paid_date') or '').strip()
    if not year or not month or not paid_date:
        return jsonify({'error': 'year, month e paid_date são obrigatórios'}), 400
    try:
        month_prefix = f'{int(year):04d}-{int(month):02d}'
    except ValueError:
        return jsonify({'error': 'year e month inválidos'}), 400
    conn = get_db()
    cursor = conn.execute(
        "UPDATE appointments SET paid = 1, paid_date = ? WHERE date LIKE ? AND (paid IS NULL OR paid = 0)",
        (paid_date, month_prefix + '%')
    )
    conn.commit()
    updated = cursor.rowcount
    conn.close()
    return jsonify({'status': 'ok', 'updated': updated})

def _xlsx_col_letter(idx):
    return chr(65 + idx)

def _xlsx_cell(col_idx, row_num, value):
    ref = f'{_xlsx_col_letter(col_idx)}{row_num}'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = xml_escape('' if value is None else str(value))
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'

def build_xlsx(headers, rows):
    """Gera um .xlsx válido (OOXML) usando só a biblioteca padrão do Python
    (zipfile + XML), sem depender de openpyxl/xlsxwriter."""
    sheet_rows = [f'<row r="1">{"".join(_xlsx_cell(i, 1, h) for i, h in enumerate(headers))}</row>']
    for r, row in enumerate(rows, start=2):
        sheet_rows.append(f'<row r="{r}">{"".join(_xlsx_cell(i, r, v) for i, v in enumerate(row))}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>' + ''.join(sheet_rows) + '</sheetData>'
        '</worksheet>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Financeiro" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', content_types)
        z.writestr('_rels/.rels', root_rels)
        z.writestr('xl/workbook.xml', workbook_xml)
        z.writestr('xl/_rels/workbook.xml.rels', workbook_rels)
        z.writestr('xl/styles.xml', styles_xml)
        z.writestr('xl/worksheets/sheet1.xml', sheet_xml)
    buf.seek(0)
    return buf

def _br_date(iso_date):
    if not iso_date:
        return '-'
    parts = iso_date.split('-')
    return f'{parts[2]}/{parts[1]}/{parts[0]}' if len(parts) == 3 else iso_date

@app.route('/api/financeiro/export.xlsx', methods=['GET'])
def export_financeiro_xlsx():
    year = request.args.get('year')
    month = request.args.get('month')
    patient_id = request.args.get('patient_id')
    if not year or not month:
        return jsonify({'error': 'year e month são obrigatórios'}), 400
    try:
        month_prefix = f'{int(year):04d}-{int(month):02d}'
    except ValueError:
        return jsonify({'error': 'year e month inválidos'}), 400

    conn = get_db()
    query = '''
        SELECT a.date, a.start_time, a.paid, a.paid_date, p.name AS patient_name, p.payment_responsible
        FROM appointments a
        JOIN patients p ON p.id = a.patient_id
        WHERE a.date LIKE ?
    '''
    params = [month_prefix + '%']
    if patient_id:
        query += ' AND a.patient_id = ?'
        params.append(patient_id)
    query += ' ORDER BY a.date, a.start_time'
    rows = conn.execute(query, params).fetchall()
    settings = conn.execute('SELECT session_price FROM settings WHERE id = 1').fetchone()
    conn.close()

    price = round(settings['session_price'] or 0, 2)
    payer_labels = {'paciente': 'Paciente', 'responsavel': 'Responsável'}
    headers = ['Paciente', 'Responsável pelo Pagamento', 'Data', 'Horário', 'Valor (R$)', 'Status', 'Data do Pagamento']
    data_rows = [
        [
            r['patient_name'],
            payer_labels.get(r['payment_responsible'], '-'),
            _br_date(r['date']),
            r['start_time'],
            price,
            'Baixado' if r['paid'] else 'Não baixado',
            _br_date(r['paid_date']) if r['paid_date'] else '-'
        ]
        for r in rows
    ]

    xlsx_buf = build_xlsx(headers, data_rows)
    return send_file(
        xlsx_buf,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f'financeiro_{month_prefix}.xlsx'
    )

if __name__ == '__main__':
    init_db()
    print("\n✅ Sistema iniciado com sucesso!")
    print("📂 Banco de dados:", DB_PATH)
    print("🌐 Acesse no navegador: http://127.0.0.1:5000\n")
    app.run(debug=True, port=5000)
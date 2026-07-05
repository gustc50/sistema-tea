from flask import Flask, request, jsonify, send_file
import sqlite3
import os
import json

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
    ''')
    conn.commit()

    # Bancos criados antes do campo "paciente desde" existir não têm essa
    # coluna. Adiciona e faz um valor razoável (data de cadastro) para quem
    # já estava na base, já que a data real de início não é conhecida.
    patient_cols = [r['name'] for r in conn.execute("PRAGMA table_info(patients)").fetchall()]
    if 'patient_since' not in patient_cols:
        conn.execute('ALTER TABLE patients ADD COLUMN patient_since TEXT')
        conn.execute("UPDATE patients SET patient_since = date(created_at) WHERE patient_since IS NULL")
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
    cursor = conn.execute(
        'INSERT INTO patients (name, dob, contact, patient_since) VALUES (?, ?, ?, ?)',
        (name, data.get('dob'), data.get('contact'), patient_since)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return jsonify({'id': new_id, 'name': name, 'dob': data.get('dob'), 'contact': data.get('contact'), 'patient_since': patient_since}), 201

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
        'SELECT id, patient_id, appointment_at, content, created_at FROM notes WHERE patient_id = ? ORDER BY appointment_at DESC',
        (patient_id,)
    ).fetchall()
    conn.close()
    return jsonify([dict(n) for n in notes])

@app.route('/api/notes/<int:patient_id>', methods=['POST'])
def save_notes(patient_id):
    data = request.json
    appointment_at = (data.get('appointment_at') or '').strip()
    content = (data.get('content') or '').strip()
    if not appointment_at or not content:
        return jsonify({'error': 'appointment_at e content são obrigatórios'}), 400
    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO notes (patient_id, appointment_at, content) VALUES (?, ?, ?)',
        (patient_id, appointment_at, content)
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return jsonify({'id': new_id, 'patient_id': patient_id, 'appointment_at': appointment_at, 'content': content}), 201

if __name__ == '__main__':
    init_db()
    print("\n✅ Sistema iniciado com sucesso!")
    print("📂 Banco de dados:", DB_PATH)
    print("🌐 Acesse no navegador: http://127.0.0.1:5000\n")
    app.run(debug=True, port=5000)
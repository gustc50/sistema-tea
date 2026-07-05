from flask import Flask, request, jsonify, send_file
import sqlite3
import os
import json
from datetime import datetime

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prontuario_tea.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Cria as tabelas automaticamente na primeira execução"""
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            dob TEXT,
            contact TEXT,
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
        CREATE TABLE IF NOT EXISTS notes (
            patient_id INTEGER PRIMARY KEY,
            content TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients(id)
        );
    ''')
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
    conn = get_db()
    cursor = conn.execute(
        'INSERT INTO patients (name, dob, contact) VALUES (?, ?, ?)',
        (data['name'], data.get('dob'), data.get('contact'))
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return jsonify({'id': new_id, **data}), 201

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
    note = conn.execute('SELECT content FROM notes WHERE patient_id = ?', (patient_id,)).fetchone()
    conn.close()
    return jsonify({'content': note['content'] if note else ''})

@app.route('/api/notes/<int:patient_id>', methods=['POST'])
def save_notes(patient_id):
    data = request.json
    conn = get_db()
    conn.execute('''
        INSERT INTO notes (patient_id, content, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(patient_id) DO UPDATE SET content = ?, updated_at = ?
    ''', (patient_id, data['content'], datetime.now(), data['content'], datetime.now()))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    init_db()
    print("\n✅ Sistema iniciado com sucesso!")
    print("📂 Banco de dados:", DB_PATH)
    print("🌐 Acesse no navegador: http://127.0.0.1:5000\n")
    app.run(debug=True, port=5000)
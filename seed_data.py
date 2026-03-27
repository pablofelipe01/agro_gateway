#!/usr/bin/env python3
"""
Seed data — Pre-cargar usuarios de prueba para piloto Sirius Edu.
Ejecutar en el gateway (Jetson) después de que edu.py haya creado la BD.

Uso: python3 seed_data.py
"""
import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'edu_data.db')

# ============================================================
# ROSTER DEL PILOTO
# Cada nodo tiene un rol asignado. El node_id se asigna durante
# la capacitación cuando se entrega el nodo LoRa al usuario.
# ============================================================

ROSTER = [
    # Estudiantes — Grado 2
    {'id': 'est001', 'name': 'María García',     'grade': '2', 'role': 'student',     'node_id': 0x00000001, 'pin': None},
    {'id': 'est002', 'name': 'Santiago López',    'grade': '2', 'role': 'student',     'node_id': 0x00000002, 'pin': None},
    {'id': 'est003', 'name': 'Valentina Muñoz',   'grade': '2', 'role': 'student',     'node_id': 0x00000003, 'pin': None},
    {'id': 'est004', 'name': 'Juan David Ríos',   'grade': '3', 'role': 'student',     'node_id': 0x00000004, 'pin': None},
    {'id': 'est005', 'name': 'Camila Herrera',     'grade': '3', 'role': 'student',     'node_id': 0x00000005, 'pin': None},

    # Profesor
    {'id': 'prof01', 'name': 'Diana Castillo',    'grade': '',  'role': 'teacher',     'node_id': 0x00000010, 'pin': '2835'},

    # Supervisor
    {'id': 'sup001', 'name': 'Carlos Mendoza',    'grade': '',  'role': 'supervisor',  'node_id': 0x00000020, 'pin': '9140'},

    # Padres (vinculados a estudiantes)
    {'id': 'pad001', 'name': 'Rosa García',        'grade': '',  'role': 'parent',      'node_id': 0x00000030, 'pin': None, 'child_id': 'est001'},
    {'id': 'pad002', 'name': 'Jorge López',         'grade': '',  'role': 'parent',      'node_id': 0x00000031, 'pin': None, 'child_id': 'est002'},
    {'id': 'pad003', 'name': 'Martha Muñoz',        'grade': '',  'role': 'parent',      'node_id': 0x00000032, 'pin': None, 'child_id': 'est003'},

    # Nodos de prueba (Pablo y segundo nodo)
    {'id': 'dev001', 'name': 'Pablo (Dev)',        'grade': '2', 'role': 'student',     'node_id': 0x7c1a5974, 'pin': None},
    {'id': 'dev002', 'name': 'Nodo Prueba',        'grade': '2', 'role': 'teacher',     'node_id': 0x49b7a524, 'pin': '1234'},
]


def seed():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Crear tabla de roster si no existe
    c.execute('''CREATE TABLE IF NOT EXISTS roster (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        grade TEXT DEFAULT '',
        role TEXT NOT NULL,
        node_id INTEGER UNIQUE,
        pin TEXT,
        child_id TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    # Insertar usuarios
    inserted = 0
    for user in ROSTER:
        try:
            c.execute(
                "INSERT OR REPLACE INTO roster (id, name, grade, role, node_id, pin, child_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user['id'], user['name'], user['grade'], user['role'],
                 user['node_id'], user.get('pin'), user.get('child_id'))
            )
            inserted += 1
        except Exception as e:
            print(f"Error insertando {user['name']}: {e}")

    # También crear perfiles de estudiantes en la tabla students
    for user in ROSTER:
        if user['role'] == 'student':
            c.execute(
                "INSERT OR REPLACE INTO students (id, name, grade, node_id) VALUES (?, ?, ?, ?)",
                (user['id'], user['name'], user['grade'], user['node_id'])
            )

    conn.commit()
    conn.close()

    print(f"✅ {inserted} usuarios insertados en {DB_PATH}")
    print("\nRoster:")
    for u in ROSTER:
        pin_info = f" (PIN: {u['pin']})" if u.get('pin') else ""
        child_info = f" → hijo: {u.get('child_id', '')}" if u.get('child_id') else ""
        print(f"  {u['role']:12s} {u['name']:20s} node: !{u['node_id']:08x}{pin_info}{child_info}")


if __name__ == '__main__':
    seed()

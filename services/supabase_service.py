"""
Supabase Service — Fuente de verdad para Sirius Edu.
El gateway siempre tiene internet, así que Supabase está siempre disponible.
SQLite local solo como cache de velocidad.
"""
import os
import logging
import requests
import json
from datetime import datetime

SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY', '')  # service_role key para acceso total

_headers = {}


def init():
    global _headers
    if not SUPABASE_URL or not SUPABASE_KEY:
        logging.warning("⚠️ Supabase no configurado (SUPABASE_URL / SUPABASE_SERVICE_KEY)")
        return False
    _headers = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=representation',
    }
    logging.info(f"✓ Supabase conectado: {SUPABASE_URL[:30]}...")
    return True


def _get(table, params=None):
    """GET request a Supabase REST API"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}"
        resp = requests.get(url, headers=_headers, params=params, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        logging.error(f"Supabase GET {table}: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logging.error(f"Supabase GET {table} error: {e}")
    return None


def _post(table, data):
    """POST (insert) a Supabase REST API"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}"
        resp = requests.post(url, headers=_headers, json=data, timeout=10)
        if resp.status_code in [200, 201]:
            result = resp.json()
            return result[0] if isinstance(result, list) and result else result
        logging.error(f"Supabase POST {table}: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logging.error(f"Supabase POST {table} error: {e}")
    return None


def _patch(table, match_params, data):
    """PATCH (update) a Supabase REST API"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}"
        params = match_params
        resp = requests.patch(url, headers=_headers, params=params, json=data, timeout=10)
        if resp.status_code in [200, 204]:
            return True
        logging.error(f"Supabase PATCH {table}: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logging.error(f"Supabase PATCH {table} error: {e}")
    return False


# ============================================================
# ROSTER
# ============================================================

def get_user_by_node(node_id):
    """Buscar usuario por node_id en el roster"""
    result = _get('roster', {'node_id': f'eq.{node_id}', 'select': '*'})
    return result[0] if result else None


def get_students_by_school(school_id):
    """Obtener todos los alumnos de una escuela"""
    return _get('roster', {
        'school_id': f'eq.{school_id}',
        'role': 'eq.student',
        'is_active': 'eq.true',
        'select': '*',
        'order': 'grade,name',
    }) or []


# ============================================================
# LECCIONES
# ============================================================

def get_active_lessons(grade, school_id):
    """Obtener lecciones activas por grado"""
    return _get('lessons', {
        'grade': f'eq.{grade}',
        'school_id': f'eq.{school_id}',
        'is_active': 'eq.true',
        'select': '*',
        'order': 'week_number.desc,created_at.desc',
        'limit': '5',
    }) or []


def get_lesson(lesson_id):
    """Obtener una lección por ID"""
    result = _get('lessons', {'id': f'eq.{lesson_id}', 'select': '*'})
    return result[0] if result else None


# ============================================================
# TAREAS
# ============================================================

def get_assignments(grade, school_id):
    """Obtener tareas activas por grado"""
    return _get('assignments', {
        'grade': f'eq.{grade}',
        'school_id': f'eq.{school_id}',
        'is_active': 'eq.true',
        'select': '*, lessons(title, subject_code)',
        'order': 'deadline.asc.nullslast,created_at.desc',
    }) or []


def get_student_submissions(student_id):
    """Obtener entregas de un alumno"""
    return _get('submissions', {
        'student_id': f'eq.{student_id}',
        'select': '*, assignments(title, description)',
        'order': 'submitted_at.desc',
    }) or []


def save_submission(assignment_id, student_id, response):
    """Guardar entrega de un alumno"""
    return _post('submissions', {
        'assignment_id': assignment_id,
        'student_id': student_id,
        'response': response,
    })


def update_submission_ai_eval(submission_id, feedback, score, model):
    """Actualizar evaluación IA de una entrega"""
    return _patch('submissions', {'id': f'eq.{submission_id}'}, {
        'ai_feedback': feedback,
        'ai_score': score,
        'ai_model': model,
        'ai_evaluated_at': datetime.now().isoformat(),
    })


# ============================================================
# CONVERSACIONES IA
# ============================================================

def save_ai_conversation(student_id, school_id, question, response, model, subject_code=None, lesson_id=None):
    """Guardar conversación con tutor IA"""
    data = {
        'student_id': student_id,
        'school_id': school_id,
        'question': question,
        'ai_response': response,
        'ai_model': model,
    }
    if subject_code:
        data['subject_code'] = subject_code
    if lesson_id:
        data['lesson_context'] = lesson_id
    return _post('ai_conversations', data)


# ============================================================
# PREGUNTAS AL PROFESOR
# ============================================================

def save_student_question(student_id, school_id, question, context=None):
    """Guardar pregunta del alumno al profesor"""
    data = {
        'student_id': student_id,
        'school_id': school_id,
        'question': question,
    }
    if context:
        data['context'] = context
    return _post('student_questions', data)


def get_unanswered_questions(school_id):
    """Obtener preguntas sin responder"""
    return _get('student_questions', {
        'school_id': f'eq.{school_id}',
        'teacher_response': 'is.null',
        'select': '*, roster(name, grade)',
        'order': 'created_at.desc',
    }) or []


def answer_question(question_id, teacher_id, response):
    """Profesor responde pregunta"""
    return _patch('student_questions', {'id': f'eq.{question_id}'}, {
        'teacher_response': response,
        'responded_by': teacher_id,
        'responded_at': datetime.now().isoformat(),
        'is_read': True,
    })


# ============================================================
# CONEXIONES
# ============================================================

def log_connection(student_id, school_id, node_id):
    """Registrar conexión de un alumno"""
    return _post('connection_log', {
        'student_id': student_id,
        'school_id': school_id,
        'node_id': node_id,
    })


# ============================================================
# PROGRESO
# ============================================================

def get_student_progress(student_id):
    """Obtener resumen de progreso usando la función de Supabase"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/rpc/get_student_progress"
        resp = requests.post(url, headers=_headers,
                           json={'p_student_id': student_id}, timeout=10)
        if resp.status_code == 200:
            result = resp.json()
            return result[0] if isinstance(result, list) and result else result
    except Exception as e:
        logging.error(f"Error get_student_progress: {e}")
    return None

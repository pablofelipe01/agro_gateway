"""
Handler Educativo - Sistema Sirius Edu
Maneja todos los prefijos educativos: PREGUNTA_IA, ENTREGA, LECCION, EVAL_PROF, PERFIL_UPDATE, SYNC_REQ
AI local-first: Ollama (llama3.2:3b) como primario, Claude API como fallback.
"""
import os
import json
import logging
import sqlite3
import uuid
import requests
from datetime import datetime
from anthropic import Anthropic

# ==================== CONFIGURACIÓN ====================

OLLAMA_URL = 'http://localhost:11434'
OLLAMA_MODEL = 'llama3.2:3b'
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'edu_data.db')
CURRICULOS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'curriculos')

anthropic_client = None
MAX_AI_TOKENS = 500
LEVEL_UPDATE_INTERVAL = 5  # Cada N conversaciones, actualizar nivel del alumno

# Airtable EDU — base DIFERENTE a portería
# Token API es el mismo (por cuenta), pero Base ID es diferente
AIRTABLE_API_TOKEN = os.getenv('AIRTABLE_API_TOKEN', '')
AIRTABLE_EDU_BASE_ID = os.getenv('AIRTABLE_EDU_BASE_ID', 'appQQ3PpZD3sx9ZW4')

# Table IDs reales de Airtable
AIRTABLE_TABLES = {
    'student':       'tblAaP1Cd1mxcWRWs',  # Estudiantes
    'lesson':        'tblY7EfPhTtczCSwD',  # Lecciones
    'assignment':    'tblayZBVzJZYeXpKB',  # Tareas
    'submission':    'tblMRgI60MdSMe4qq',  # Entregas
    'conversation':  'tblduhargWkpksTBL',  # Conversaciones IA
}

# Mapeo de campos internos → nombres de campo en Airtable
AIRTABLE_FIELD_MAP = {
    'student': {
        'id': 'ID', 'name': 'Nombre', 'grade': 'Grado',
        'node_id': 'Node ID', 'level_by_subject': 'Nivel por Materia',
        'teacher_notes': 'Notas del Profesor', 'message_to_parent': 'Mensaje para Padre',
        'created_at': 'Fecha Registro',
    },
    'lesson': {
        'id': 'ID', 'subject': 'Materia', 'grade': 'Grado',
        'title': 'Titulo', 'summary': 'Resumen', 'full_content': 'Contenido',
        'is_active': 'Activa', 'teacher_node_id': 'Profesor Node', 'created_at': 'Fecha',
    },
    'assignment': {
        'id': 'ID', 'description': 'Descripcion',
        'deadline': 'Fecha Limite', 'status': 'Estado',
    },
    'submission': {
        'id': 'ID', 'response': 'Respuesta', 'submitted_at': 'Fecha Entrega',
        'ai_feedback': 'Feedback IA', 'ai_score': 'Puntaje IA',
        'teacher_criteria': 'Criterio Profesor', 'final_grade': 'Nota Final',
    },
    'conversation': {
        'id': 'ID', 'subject': 'Materia', 'question': 'Pregunta',
        'response': 'Respuesta IA', 'model': 'Modelo', 'created_at': 'Fecha',
    },
}

# Mapeo de estado interno → valor de Single Select en Airtable
STATUS_MAP = {
    'pending': 'Pendiente',
    'submitted': 'Entregada',
    'ai_evaluated': 'Evaluada IA',
    'fully_evaluated': 'Evaluada Final',
}

# ==================== INIT ====================

def init(api_key):
    """Inicializar handler educativo"""
    global anthropic_client
    if api_key:
        anthropic_client = Anthropic(api_key=api_key)
    _init_database()

    if AIRTABLE_EDU_BASE_ID:
        logging.info(f"✓ Handler EDU inicializado (local-first, Airtable EDU base: {AIRTABLE_EDU_BASE_ID[:8]}...)")
    else:
        logging.info("✓ Handler EDU inicializado (local-first, SIN Airtable EDU — solo SQLite local)")


def _init_database():
    """Crear tablas SQLite si no existen"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS students (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        grade TEXT NOT NULL,
        node_id INTEGER UNIQUE,
        teacher_notes TEXT DEFAULT '[]',
        level_by_subject TEXT DEFAULT '{}',
        teacher_message_to_parent TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        student_id TEXT NOT NULL,
        subject TEXT,
        question TEXT NOT NULL,
        ai_response TEXT NOT NULL,
        model_used TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (student_id) REFERENCES students(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS lessons (
        id TEXT PRIMARY KEY,
        subject TEXT NOT NULL,
        grade TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL,
        full_content TEXT NOT NULL,
        teacher_node_id INTEGER,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS assignments (
        id TEXT PRIMARY KEY,
        lesson_id TEXT,
        student_id TEXT,
        description TEXT NOT NULL,
        deadline TEXT,
        status TEXT DEFAULT 'pending',
        FOREIGN KEY (lesson_id) REFERENCES lessons(id),
        FOREIGN KEY (student_id) REFERENCES students(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS submissions (
        id TEXT PRIMARY KEY,
        assignment_id TEXT NOT NULL,
        student_id TEXT NOT NULL,
        response TEXT NOT NULL,
        submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
        ai_feedback TEXT,
        ai_score REAL,
        teacher_criteria TEXT,
        final_grade TEXT,
        FOREIGN KEY (assignment_id) REFERENCES assignments(id),
        FOREIGN KEY (student_id) REFERENCES students(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS sync_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        data TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        synced_at TEXT
    )''')

    conn.commit()
    conn.close()
    logging.info(f"✓ Base de datos EDU lista: {DB_PATH}")


# ==================== ROUTER PRINCIPAL ====================

def handle(text, from_id, from_num, send_fn, publish_mqtt):
    """Router de mensajes educativos"""
    try:
        if text.startswith('PREGUNTA_IA|'):
            _handle_ai_question(text, from_id, from_num, send_fn, publish_mqtt)
        elif text.startswith('ENTREGA|'):
            _handle_submission(text, from_id, from_num, send_fn, publish_mqtt)
        elif text.startswith('LECCION|'):
            _handle_new_lesson(text, from_id, from_num, send_fn, publish_mqtt)
        elif text.startswith('EVAL_PROF|'):
            _handle_teacher_eval(text, from_id, from_num, send_fn, publish_mqtt)
        elif text.startswith('PERFIL_UPDATE|'):
            _handle_profile_update(text, from_id, from_num, send_fn, publish_mqtt)
        elif text.startswith('SYNC_REQ|'):
            _handle_sync_request(text, from_id, from_num, send_fn, publish_mqtt)
        elif text.startswith('TAREA|'):
            _handle_new_assignment(text, from_id, from_num, send_fn, publish_mqtt)
    except Exception as e:
        logging.error(f"❌ Error en handler EDU: {e}")
        import traceback
        logging.error(traceback.format_exc())


# ==================== IA: LOCAL-FIRST ====================

def _query_ai(prompt, max_tokens=MAX_AI_TOKENS):
    """Consultar IA: Ollama local primero, Claude API como fallback"""
    # 1. Intentar Ollama local (siempre disponible)
    try:
        response = requests.post(
            f'{OLLAMA_URL}/api/generate',
            json={
                'model': OLLAMA_MODEL,
                'prompt': prompt,
                'stream': False,
                'options': {'num_predict': max_tokens}
            },
            timeout=60
        )
        if response.status_code == 200:
            result = response.json().get('response', '').strip()
            if result:
                logging.info(f"🤖 Respuesta Ollama local ({len(result)} chars)")
                return result, OLLAMA_MODEL
    except Exception as e:
        logging.warning(f"⚠️ Ollama local no disponible: {e}")

    # 2. Fallback: Claude API (si hay internet)
    if anthropic_client:
        try:
            message = anthropic_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}]
            )
            result = message.content[0].text.strip()
            logging.info(f"🌐 Respuesta Claude API ({len(result)} chars)")
            return result, "claude-sonnet-4-20250514"
        except Exception as e:
            logging.warning(f"⚠️ Claude API no disponible: {e}")

    return "No pude responder en este momento. Intenta de nuevo.", "none"


def _query_ai_json(prompt, max_tokens=MAX_AI_TOKENS):
    """Consultar IA esperando respuesta JSON"""
    response_text, model = _query_ai(prompt + "\n\nResponde SOLO con JSON válido, sin texto adicional.", max_tokens)
    try:
        # Intentar extraer JSON del texto
        start = response_text.find('{')
        end = response_text.rfind('}') + 1
        if start >= 0 and end > start:
            return json.loads(response_text[start:end]), model
    except json.JSONDecodeError:
        pass
    return {"feedback": response_text, "score": 5.0}, model


# ==================== PERFIL DE ALUMNO ====================

def _get_or_create_student(node_id, student_id=None):
    """Obtener perfil del alumno, crear uno básico si no existe"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Buscar por student_id o node_id
    if student_id:
        c.execute("SELECT * FROM students WHERE id = ?", (student_id,))
    else:
        c.execute("SELECT * FROM students WHERE node_id = ?", (node_id,))

    row = c.fetchone()
    if row:
        profile = dict(row)
        profile['teacher_notes'] = json.loads(profile.get('teacher_notes', '[]'))
        profile['level_by_subject'] = json.loads(profile.get('level_by_subject', '{}'))
        conn.close()
        return profile

    # Auto-crear perfil básico
    new_id = student_id or str(uuid.uuid4())[:8]
    c.execute(
        "INSERT INTO students (id, name, grade, node_id) VALUES (?, ?, ?, ?)",
        (new_id, f"Alumno-{hex(node_id)[-4:]}", "2", node_id)
    )
    conn.commit()
    profile = {
        'id': new_id, 'name': f"Alumno-{hex(node_id)[-4:]}",
        'grade': '2', 'node_id': node_id,
        'teacher_notes': [], 'level_by_subject': {},
        'teacher_message_to_parent': ''
    }
    conn.close()
    logging.info(f"📝 Auto-creado perfil para nodo {hex(node_id)}: {new_id}")
    _add_to_sync_queue('student', new_id, 'create', profile)
    return profile


def _get_active_lesson(grade=None):
    """Obtener lección activa, opcionalmente por grado"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if grade:
        c.execute("SELECT * FROM lessons WHERE is_active = 1 AND grade = ? ORDER BY created_at DESC LIMIT 1", (grade,))
    else:
        c.execute("SELECT * FROM lessons WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    return dict(row) if row else {}


def _count_conversations(student_id):
    """Contar conversaciones del alumno"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM conversations WHERE student_id = ?", (student_id,))
    count = c.fetchone()[0]
    conn.close()
    return count


def _get_recent_conversations(student_id, limit=10):
    """Obtener conversaciones recientes"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT question, ai_response FROM conversations WHERE student_id = ? ORDER BY created_at DESC LIMIT ?",
        (student_id, limit)
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


# ==================== HANDLERS ====================

def _handle_ai_question(text, from_id, from_num, send_fn, publish_mqtt):
    """PREGUNTA_IA|alumno_id|contexto_id|pregunta"""
    parts = text.split('|', 3)
    if len(parts) < 4:
        send_fn(from_num, "Error: formato de pregunta inválido.")
        return

    student_id = parts[1]
    pregunta = parts[3]

    logging.info(f"📚 Pregunta IA de {student_id}: {pregunta[:50]}...")

    # Obtener perfil
    profile = _get_or_create_student(from_num, student_id)

    # Obtener lección activa
    lesson = _get_active_lesson(profile.get('grade'))

    # Construir prompt educativo contextualizado
    prompt = _build_educational_prompt(profile, lesson, pregunta)

    # Consultar IA (local primero)
    response, model_used = _query_ai(prompt)

    # Guardar conversación
    conv_id = str(uuid.uuid4())[:8]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO conversations (id, student_id, subject, question, ai_response, model_used) VALUES (?, ?, ?, ?, ?, ?)",
        (conv_id, profile['id'], lesson.get('subject', ''), pregunta, response, model_used)
    )
    conn.commit()
    conn.close()

    # Verificar si toca actualizar nivel del alumno
    conv_count = _count_conversations(profile['id'])
    if conv_count > 0 and conv_count % LEVEL_UPDATE_INTERVAL == 0:
        _update_student_level(profile['id'], send_fn)

    # Enviar respuesta
    full_response = f"RESPUESTA_IA|{profile['id']}|{response}"
    send_fn(from_num, full_response)

    publish_mqtt("edu/ai_question", {
        "student_id": profile['id'], "question": pregunta[:100],
        "model": model_used, "timestamp": datetime.now().isoformat()
    })

    _add_to_sync_queue('conversation', conv_id, 'create', {
        'id': conv_id, 'student_id': profile['id'], 'subject': lesson.get('subject', ''),
        'question': pregunta, 'response': response, 'model': model_used,
        'created_at': datetime.now().isoformat()
    })


def _build_educational_prompt(profile, lesson, question):
    """Construir prompt contextualizado para el tutor IA"""
    notes = profile.get('teacher_notes', [])
    notes_text = '\n'.join(f'- {n}' for n in notes) if notes else 'Sin notas especiales'
    level = json.dumps(profile.get('level_by_subject', {}), ensure_ascii=False)
    lesson_title = lesson.get('title', 'Sin lección activa')
    lesson_subject = lesson.get('subject', '')

    return f"""Eres un tutor educativo amigable para estudiantes rurales de Colombia.
Responde en español simple y claro, apropiado para niños de primaria.
Máximo 3 párrafos cortos. Usa ejemplos concretos del contexto rural colombiano.
NO uses emojis. NO uses formato markdown. Solo texto plano.

ALUMNO: {profile['name']}, {profile['grade']}° grado
LECCIÓN ACTUAL: {lesson_title}
MATERIA: {lesson_subject}
NIVEL DETECTADO: {level}

NOTAS DEL PROFESOR (guían tu estilo de enseñanza):
{notes_text}

PREGUNTA DEL ALUMNO:
{question}

Responde de forma que {profile['name']} pueda entender fácilmente."""


def _handle_submission(text, from_id, from_num, send_fn, publish_mqtt):
    """ENTREGA|tarea_id|alumno_id|respuesta"""
    parts = text.split('|', 3)
    if len(parts) < 4:
        send_fn(from_num, "Error: formato de entrega inválido.")
        return

    assignment_id = parts[1]
    student_id = parts[2]
    response_text = parts[3]

    logging.info(f"📝 Entrega de {student_id} para tarea {assignment_id}")

    profile = _get_or_create_student(from_num, student_id)

    # Obtener tarea
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,))
    assignment = c.fetchone()
    conn.close()

    if not assignment:
        send_fn(from_num, f"EVAL_IA|{assignment_id}|0|No encontré esa tarea.")
        return

    assignment = dict(assignment)

    # Guardar entrega
    sub_id = str(uuid.uuid4())[:8]
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO submissions (id, assignment_id, student_id, response) VALUES (?, ?, ?, ?)",
        (sub_id, assignment_id, student_id, response_text)
    )
    # Actualizar estado de tarea
    c.execute("UPDATE assignments SET status = 'submitted' WHERE id = ?", (assignment_id,))
    conn.commit()
    conn.close()

    # Evaluar con IA
    eval_prompt = f"""Evalúa esta respuesta de un estudiante de {profile['grade']}° grado de una zona rural de Colombia.

Tarea: {assignment['description']}
Respuesta del alumno: {response_text}

Proporciona retroalimentación POSITIVA y CONSTRUCTIVA en máximo 2 oraciones.
Luego un puntaje de 0 a 10.
Responde SOLO con JSON: {{"feedback": "...", "score": 8.5}}"""

    eval_result, model = _query_ai_json(eval_prompt, max_tokens=200)

    feedback = eval_result.get('feedback', 'Bien hecho. Sigue practicando.')
    score = float(eval_result.get('score', 5.0))

    # Actualizar submission con evaluación IA
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE submissions SET ai_feedback = ?, ai_score = ? WHERE id = ?",
        (feedback, score, sub_id)
    )
    c.execute("UPDATE assignments SET status = 'ai_evaluated' WHERE id = ?", (assignment_id,))
    conn.commit()
    conn.close()

    # Enviar retroalimentación al alumno
    send_fn(from_num, f"EVAL_IA|{assignment_id}|{score}|{feedback}")

    logging.info(f"✅ Entrega evaluada: {student_id} → {score}/10")

    publish_mqtt("edu/submission", {
        "student_id": student_id, "assignment_id": assignment_id,
        "score": score, "model": model, "timestamp": datetime.now().isoformat()
    })

    _add_to_sync_queue('submission', sub_id, 'create', {
        'id': sub_id, 'student_id': student_id, 'assignment_id': assignment_id,
        'response': response_text, 'ai_feedback': feedback, 'ai_score': score,
        'submitted_at': datetime.now().isoformat()
    })


def _handle_new_lesson(text, from_id, from_num, send_fn, publish_mqtt):
    """LECCION|id|materia|grado|titulo|resumen[|contenido_completo]"""
    parts = text.split('|')
    if len(parts) < 6:
        send_fn(from_num, "Error: formato de lección inválido.")
        return

    lesson_id = parts[1]
    subject = parts[2]
    grade = parts[3]
    title = parts[4]
    summary = parts[5]
    full_content = '|'.join(parts[6:]) if len(parts) > 6 else summary

    logging.info(f"📖 Nueva lección: {title} ({subject}, {grade}°)")

    # Desactivar lecciones anteriores del mismo grado
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE lessons SET is_active = 0 WHERE grade = ?", (grade,))

    # Guardar nueva lección
    c.execute(
        "INSERT OR REPLACE INTO lessons (id, subject, grade, title, summary, full_content, teacher_node_id, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (lesson_id, subject, grade, title, summary, full_content, from_num)
    )
    conn.commit()
    conn.close()

    send_fn(from_num, f"LECCION_OK|{lesson_id}|Lección guardada y activada")

    publish_mqtt("edu/lesson", {
        "lesson_id": lesson_id, "subject": subject, "grade": grade,
        "title": title, "timestamp": datetime.now().isoformat()
    })

    _add_to_sync_queue('lesson', lesson_id, 'create', {
        'id': lesson_id, 'subject': subject, 'grade': grade, 'title': title,
        'summary': summary, 'full_content': full_content,
        'is_active': True, 'created_at': datetime.now().isoformat()
    })


def _handle_new_assignment(text, from_id, from_num, send_fn, publish_mqtt):
    """TAREA|id|alumno_id|descripcion|fecha_limite"""
    parts = text.split('|')
    if len(parts) < 4:
        return

    assignment_id = parts[1]
    student_id = parts[2] if parts[2] else None
    description = parts[3]
    deadline = parts[4] if len(parts) > 4 and parts[4] else None

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO assignments (id, student_id, description, deadline) VALUES (?, ?, ?, ?)",
        (assignment_id, student_id, description, deadline)
    )
    conn.commit()
    conn.close()

    logging.info(f"📋 Nueva tarea: {assignment_id} → {description[:40]}...")


def _handle_teacher_eval(text, from_id, from_num, send_fn, publish_mqtt):
    """EVAL_PROF|tarea_id|alumno_id|criterio|nota"""
    parts = text.split('|')
    if len(parts) < 5:
        send_fn(from_num, "Error: formato de evaluación inválido.")
        return

    assignment_id = parts[1]
    student_id = parts[2]
    criteria = parts[3]
    grade = parts[4]

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """UPDATE submissions SET teacher_criteria = ?, final_grade = ?
           WHERE assignment_id = ? AND student_id = ?""",
        (criteria, grade, assignment_id, student_id)
    )
    c.execute("UPDATE assignments SET status = 'fully_evaluated' WHERE id = ?", (assignment_id,))
    conn.commit()
    conn.close()

    send_fn(from_num, f"EVAL_OK|{assignment_id}|{student_id}")
    logging.info(f"✅ Evaluación profesor: {assignment_id} → {grade}")

    _add_to_sync_queue('submission', assignment_id, 'update', {
        'teacher_criteria': criteria, 'final_grade': grade
    })

    # También actualizar el estado de la tarea
    _add_to_sync_queue('assignment', assignment_id, 'update', {
        'status': 'fully_evaluated'
    })


def _handle_profile_update(text, from_id, from_num, send_fn, publish_mqtt):
    """PERFIL_UPDATE|alumno_id|campo|valor"""
    parts = text.split('|', 3)
    if len(parts) < 4:
        send_fn(from_num, "Error: formato de actualización inválido.")
        return

    student_id = parts[1]
    field = parts[2]
    value = parts[3]

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if field == 'teacher_notes':
        # Agregar nota a la lista
        c.execute("SELECT teacher_notes FROM students WHERE id = ?", (student_id,))
        row = c.fetchone()
        if row:
            notes = json.loads(row[0] or '[]')
            notes.append(f"[{datetime.now().strftime('%d/%m')}] {value}")
            c.execute("UPDATE students SET teacher_notes = ?, updated_at = ? WHERE id = ?",
                      (json.dumps(notes, ensure_ascii=False), datetime.now().isoformat(), student_id))
    elif field == 'name':
        c.execute("UPDATE students SET name = ?, updated_at = ? WHERE id = ?",
                  (value, datetime.now().isoformat(), student_id))
    elif field == 'grade':
        c.execute("UPDATE students SET grade = ?, updated_at = ? WHERE id = ?",
                  (value, datetime.now().isoformat(), student_id))
    elif field == 'message_to_parent':
        c.execute("UPDATE students SET teacher_message_to_parent = ?, updated_at = ? WHERE id = ?",
                  (value, datetime.now().isoformat(), student_id))

    conn.commit()
    conn.close()

    send_fn(from_num, f"PERFIL_OK|{student_id}|{field}")
    logging.info(f"📝 Perfil actualizado: {student_id}.{field}")

    _add_to_sync_queue('student', student_id, 'update', {'field': field, 'value': value})


def _handle_sync_request(text, from_id, from_num, send_fn, publish_mqtt):
    """SYNC_REQ|tipo|id|offset — solicitar datos del gateway"""
    parts = text.split('|')
    if len(parts) < 3:
        return

    tipo = parts[1]
    req_id = parts[2]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if tipo == 'lesson_active':
        c.execute("SELECT * FROM lessons WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1")
        row = c.fetchone()
        if row:
            r = dict(row)
            send_fn(from_num, f"SYNC_RES|lesson|{r['id']}|{r['subject']}|{r['grade']}|{r['title']}|{r['summary']}")

    elif tipo == 'assignments':
        c.execute("SELECT * FROM assignments WHERE (student_id = ? OR student_id IS NULL) AND status != 'fully_evaluated' ORDER BY rowid DESC LIMIT 5", (req_id,))
        for row in c.fetchall():
            r = dict(row)
            send_fn(from_num, f"TAREA|{r['id']}|{r['student_id'] or ''}|{r['description']}|{r['deadline'] or ''}")
            import time
            time.sleep(3)  # Pausa entre DMs

    elif tipo == 'profile':
        profile = _get_or_create_student(from_num, req_id)
        data = json.dumps({
            'name': profile['name'], 'grade': profile['grade'],
            'level_by_subject': profile['level_by_subject']
        }, ensure_ascii=False)
        send_fn(from_num, f"SYNC_RES|profile|{req_id}|{data}")

    conn.close()


# ==================== ACTUALIZACIÓN DE NIVEL ====================

def _update_student_level(student_id, send_fn=None):
    """Analizar conversaciones recientes y actualizar nivel del alumno"""
    conversations = _get_recent_conversations(student_id, limit=LEVEL_UPDATE_INTERVAL)
    if not conversations:
        return

    conv_text = '\n'.join(
        f"Pregunta: {c['question']}\nRespuesta: {c['ai_response'][:100]}"
        for c in conversations
    )

    prompt = f"""Analiza estas conversaciones de un alumno de primaria y resume en 1 oración por materia:
- Qué entiende bien
- Qué le cuesta

{conv_text}

Responde SOLO con JSON: {{"ciencias": "entiende X, dificultad con Y", "matematicas": "..."}}"""

    result, _ = _query_ai_json(prompt, max_tokens=200)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE students SET level_by_subject = ?, updated_at = ? WHERE id = ?",
              (json.dumps(result, ensure_ascii=False), datetime.now().isoformat(), student_id))
    conn.commit()
    conn.close()

    logging.info(f"📊 Nivel actualizado para {student_id}: {result}")


# ==================== SYNC QUEUE ====================

def _add_to_sync_queue(entity_type, entity_id, operation, data):
    """Agregar a cola de sincronización con Airtable"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO sync_queue (entity_type, entity_id, operation, data) VALUES (?, ?, ?, ?)",
            (entity_type, entity_id, operation, json.dumps(data, ensure_ascii=False))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Error agregando a sync_queue: {e}")


def _map_fields_to_airtable(entity_type, data):
    """Mapear campos internos a nombres de campo de Airtable"""
    field_map = AIRTABLE_FIELD_MAP.get(entity_type, {})
    mapped = {}
    for key, value in data.items():
        airtable_name = field_map.get(key, key)
        # Convertir status a valor de Single Select
        if key == 'status' and entity_type == 'assignment':
            value = STATUS_MAP.get(value, value)
        # Convertir booleanos a checkbox
        if key == 'is_active':
            value = bool(value)
        # Convertir fechas a formato YYYY-MM-DD
        if key in ('created_at', 'submitted_at', 'deadline') and value:
            if 'T' in str(value):
                value = str(value).split('T')[0]
        mapped[airtable_name] = value
    return mapped


def sync_to_airtable(airtable_token=None, base_id=None):
    """Sincronizar cola pendiente con Airtable EDU (llamar periódicamente).
    Usa las variables de entorno si no se pasan parámetros.
    Usa Table IDs reales para evitar problemas con nombres encoded."""
    airtable_token = airtable_token or AIRTABLE_API_TOKEN
    base_id = base_id or AIRTABLE_EDU_BASE_ID
    if not airtable_token or not base_id:
        return 0

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM sync_queue WHERE synced_at IS NULL ORDER BY created_at LIMIT 20")
        pending = [dict(r) for r in c.fetchall()]

        if not pending:
            conn.close()
            return 0

        headers = {
            "Authorization": f"Bearer {airtable_token}",
            "Content-Type": "application/json"
        }

        synced = 0
        for item in pending:
            table_id = AIRTABLE_TABLES.get(item['entity_type'])
            if not table_id:
                continue

            raw_data = json.loads(item['data'])
            # Mapear campos internos → nombres Airtable
            fields = _map_fields_to_airtable(item['entity_type'], raw_data)
            url = f"https://api.airtable.com/v0/{base_id}/{table_id}"

            try:
                resp = requests.post(url, headers=headers, json={"fields": fields}, timeout=15)
                if resp.status_code in [200, 201]:
                    c.execute("UPDATE sync_queue SET synced_at = ? WHERE id = ?",
                              (datetime.now().isoformat(), item['id']))
                    synced += 1
            except:
                pass

        conn.commit()
        conn.close()
        if synced > 0:
            logging.info(f"☁️ Sincronizados {synced}/{len(pending)} registros a Airtable")
        return synced

    except Exception as e:
        logging.error(f"Error sync Airtable: {e}")
        return 0

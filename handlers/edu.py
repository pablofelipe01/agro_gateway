"""
Handler Educativo - Sirius Edu v2.1
Supabase es la fuente de verdad. Gateway siempre tiene internet.
Claude API como IA principal (siempre disponible).
"""
import os
import json
import logging
import time
from datetime import datetime
from anthropic import Anthropic

from services import supabase_service as db

# ==================== CONFIGURACIÓN ====================

anthropic_client = None
MAX_AI_TOKENS = 500
SCHOOL_ID = os.getenv('SCHOOL_ID', 'a0000000-0000-0000-0000-000000000001')

# ==================== INIT ====================

def init(api_key):
    """Inicializar handler educativo"""
    global anthropic_client
    if api_key:
        anthropic_client = Anthropic(api_key=api_key)
    db.init()
    logging.info("Handler EDU v2.1 inicializado (Supabase + Claude)")


# ==================== ROUTER PRINCIPAL ====================

def handle(text, from_id, from_num, send_fn, publish_mqtt):
    """Router de mensajes educativos"""
    try:
        if text.startswith('PREGUNTA_IA|'):
            _handle_ai_question(text, from_id, from_num, send_fn, publish_mqtt)
        elif text.startswith('ENTREGA|'):
            _handle_submission(text, from_id, from_num, send_fn, publish_mqtt)
        elif text.startswith('TEST_RES|'):
            _handle_test_response(text, from_id, from_num, send_fn, publish_mqtt)
        elif text.startswith('PROGRESO|'):
            _handle_progress_update(text, from_id, from_num, send_fn, publish_mqtt)
        elif text.startswith('SYNC_REQ|'):
            _handle_sync_request(text, from_id, from_num, send_fn, publish_mqtt)
        elif text.startswith('PREGUNTA_PROF|'):
            _handle_question_to_teacher(text, from_id, from_num, send_fn, publish_mqtt)
        elif text.startswith('RESP_PROF|'):
            _handle_teacher_response(text, from_id, from_num, send_fn, publish_mqtt)
        elif text.startswith('ROSTER_REQ|'):
            _handle_roster_request(text, from_id, from_num, send_fn, publish_mqtt)
        elif text.startswith('ROSTER_PIN|'):
            _handle_roster_pin(text, from_id, from_num, send_fn, publish_mqtt)
    except Exception as e:
        logging.error(f"Error en handler EDU: {e}")
        import traceback
        logging.error(traceback.format_exc())


# ==================== IA (Claude API) ====================

def _query_ai(prompt, max_tokens=MAX_AI_TOKENS):
    """Consultar Claude API. Gateway siempre tiene internet."""
    if not anthropic_client:
        return "IA no configurada.", "none"
    try:
        message = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}]
        )
        result = message.content[0].text.strip()
        logging.info(f"Respuesta Claude ({len(result)} chars)")
        return result, "claude-sonnet-4-20250514"
    except Exception as e:
        logging.error(f"Claude API error: {e}")
        return "No pude responder en este momento. Intenta de nuevo.", "none"


def _query_ai_json(prompt, max_tokens=MAX_AI_TOKENS):
    """Consultar Claude esperando JSON"""
    text, model = _query_ai(prompt + "\n\nResponde SOLO con JSON valido, sin texto adicional.", max_tokens)
    try:
        start = text.find('{')
        end = text.rfind('}') + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end]), model
    except json.JSONDecodeError:
        pass
    return {"feedback": text, "score": 5.0}, model


# ==================== HANDLERS ====================

def _handle_ai_question(text, from_id, from_num, send_fn, publish_mqtt):
    """PREGUNTA_IA|student_name|context_id|pregunta"""
    parts = text.split('|', 3)
    if len(parts) < 4:
        return

    student_name = parts[1]
    pregunta = parts[3]
    logging.info(f"Pregunta IA de {student_name}: {pregunta[:60]}...")

    user = db.get_user_by_node(from_num)
    if not user:
        send_fn(from_num, "RESPUESTA_IA||No estas registrado en el sistema.")
        return

    lessons = db.get_active_lessons(user.get('grade', '2'), SCHOOL_ID)
    lesson = lessons[0] if lessons else {}

    prompt = _build_educational_prompt(user, lesson, pregunta)
    response, model = _query_ai(prompt)

    db.save_ai_conversation(
        student_id=user['id'],
        school_id=SCHOOL_ID,
        question=pregunta,
        response=response,
        model=model,
        subject_code=lesson.get('subject_code'),
        lesson_id=lesson.get('id'),
    )

    db.log_connection(user['id'], SCHOOL_ID, from_num)
    send_fn(from_num, f"RESPUESTA_IA|{user['id']}|{response}")

    publish_mqtt("edu/ai_question", {
        "student": user['name'], "question": pregunta[:80],
        "model": model, "timestamp": datetime.now().isoformat()
    })


def _build_educational_prompt(user, lesson, question):
    """Prompt contextualizado para tutor IA"""
    role = user.get('role', 'student')
    name = user.get('name', 'Alumno')
    grade = user.get('grade', '')
    lesson_title = lesson.get('title', 'Sin leccion activa')
    lesson_subject = lesson.get('subject_code', '')
    lesson_content = lesson.get('content', '')[:300]

    if role == 'teacher':
        return (
            "Eres un asistente educativo para profesores rurales de Colombia.\n"
            "Responde de forma clara y practica, con sugerencias concretas.\n"
            "Maximo 3 parrafos cortos.\n\n"
            f"PROFESOR: {name}\n"
            f"PREGUNTA: {question}\n\n"
            "Responde de forma util y directa."
        )

    return (
        "Eres un tutor educativo amigable para estudiantes rurales de Colombia.\n"
        "Responde en espanol simple y claro, apropiado para ninos de primaria.\n"
        "Maximo 3 parrafos cortos. Usa ejemplos concretos del contexto rural colombiano.\n"
        "NO uses emojis. NO uses formato markdown. Solo texto plano.\n\n"
        f"ALUMNO: {name}, {grade} grado\n"
        f"LECCION ACTUAL: {lesson_title}\n"
        f"MATERIA: {lesson_subject}\n"
        f"CONTEXTO: {lesson_content[:200]}\n\n"
        f"PREGUNTA DEL ALUMNO:\n{question}\n\n"
        f"Responde de forma que {name} pueda entender facilmente."
    )


def _handle_submission(text, from_id, from_num, send_fn, publish_mqtt):
    """ENTREGA|activity_or_assignment_id|student_name|respuesta"""
    parts = text.split('|', 3)
    if len(parts) < 4:
        return

    item_id = parts[1]
    response_text = parts[3]

    user = db.get_user_by_node(from_num)
    if not user:
        send_fn(from_num, f"EVAL_IA|{item_id}|0|No estas registrado.")
        return

    logging.info(f"Entrega de {user['name']} para {item_id[:8]}...")

    # Try as activity_id first (v4), then as assignment_id (legacy)
    submission = db.save_activity_submission(item_id, user['id'], response_text, 'mission')
    if not submission:
        submission = db.save_submission(item_id, user['id'], response_text)
    if not submission:
        send_fn(from_num, f"EVAL_IA|{item_id}|0|Error al guardar entrega.")
        return

    assignment_id = item_id
    assignments = db.get_assignments(user.get('grade', '2'), SCHOOL_ID)
    assignment = next((a for a in assignments if str(a['id']) == assignment_id), None)

    eval_prompt = (
        f"Evalua esta respuesta de un estudiante de {user.get('grade', '2')} grado "
        "de una zona rural de Colombia.\n\n"
        f"Tarea: {assignment['description'] if assignment else 'Sin descripcion'}\n"
        f"Respuesta del alumno: {response_text}\n\n"
        "Proporciona retroalimentacion POSITIVA y CONSTRUCTIVA en maximo 2 oraciones.\n"
        "Luego un puntaje de 0 a 10.\n"
        'Responde SOLO con JSON: {"feedback": "...", "score": 8.5}'
    )

    eval_result, model = _query_ai_json(eval_prompt, max_tokens=200)
    feedback = eval_result.get('feedback', 'Bien hecho. Sigue practicando.')
    score = float(eval_result.get('score', 5.0))

    db.update_submission_ai_eval(submission['id'], feedback, score, model)

    send_fn(from_num, f"EVAL_IA|{assignment_id}|{score}|{feedback}")
    logging.info(f"Entrega evaluada: {user['name']} -> {score}/10")

    publish_mqtt("edu/submission", {
        "student": user['name'], "score": score,
        "model": model, "timestamp": datetime.now().isoformat()
    })


def _handle_question_to_teacher(text, from_id, from_num, send_fn, publish_mqtt):
    """PREGUNTA_PROF|student_name|pregunta"""
    parts = text.split('|', 2)
    if len(parts) < 3:
        return

    pregunta = parts[2]
    user = db.get_user_by_node(from_num)
    if not user:
        return

    db.save_student_question(user['id'], SCHOOL_ID, pregunta)
    send_fn(from_num, "PREGUNTA_PROF_OK|Tu pregunta fue enviada al profesor.")
    logging.info(f"Pregunta para profesor de {user['name']}: {pregunta[:50]}...")


def _handle_teacher_response(text, from_id, from_num, send_fn, publish_mqtt):
    """RESP_PROF|question_id|respuesta"""
    parts = text.split('|', 2)
    if len(parts) < 3:
        return

    question_id = parts[1]
    response = parts[2]

    teacher = db.get_user_by_node(from_num)
    if not teacher or teacher['role'] != 'teacher':
        return

    db.answer_question(question_id, teacher['id'], response)
    send_fn(from_num, "RESP_PROF_OK|Respuesta enviada.")
    logging.info(f"Profesor {teacher['name']} respondio pregunta {question_id[:8]}")


def _handle_sync_request(text, from_id, from_num, send_fn, publish_mqtt):
    """SYNC_REQ|tipo — Alumno/profesor pide datos de Supabase"""
    parts = text.split('|')
    if len(parts) < 2:
        return

    tipo = parts[1]
    user = db.get_user_by_node(from_num)
    if not user:
        send_fn(from_num, "SYNC_RES|ERROR|No registrado")
        return

    if tipo == 'lessons':
        lessons = db.get_active_lessons(user.get('grade', '2'), SCHOOL_ID)
        if not lessons:
            send_fn(from_num, "SYNC_RES|lessons|EMPTY|No hay lecciones activas")
            return
        for l in lessons[:3]:
            summary = (l.get('summary', '') or '')[:150]
            total_ch = l.get('total_chapters', 0)
            if total_ch == 0:
                chapters = db.get_lesson_chapters(l['id'])
                total_ch = len(chapters)
            send_fn(from_num, f"LECCION|{l['id']}|{l.get('subject_code','')}|{l.get('grade','')}|{l.get('title','')}|{summary}|{total_ch}")
            time.sleep(3)
        send_fn(from_num, "SYNC_END|lessons")

    elif tipo == 'chapter':
        if len(parts) < 4:
            return
        lesson_id = parts[2]
        ch_num = int(parts[3])
        chapters = db.get_lesson_chapters(lesson_id)
        total = len(chapters)
        chapter = next((c for c in chapters if c['chapter_number'] == ch_num), None)
        if chapter:
            send_fn(from_num, f"CAPITULO|{lesson_id}|{ch_num}|{total}|{chapter['title']}|{chapter['content']}")
        else:
            send_fn(from_num, f"CAPITULO|{lesson_id}|{ch_num}|{total}|Error|Capitulo no encontrado")

    elif tipo == 'activities':
        if len(parts) < 4:
            return
        lesson_id = parts[2]
        ch_num = int(parts[3])
        activities = db.get_chapter_activities(lesson_id, ch_num)
        if not activities:
            send_fn(from_num, f"SYNC_RES|activities|EMPTY|Sin actividades")
            return
        for a in activities:
            data_json = json.dumps(a.get('data', {}), ensure_ascii=False)
            send_fn(from_num, f"ACTIVIDAD|{lesson_id}|{ch_num}|{a['activity_number']}|{a['activity_type']}|{a['id']}|{data_json}")
            time.sleep(2)
        send_fn(from_num, f"SYNC_END|activities|{lesson_id}|{ch_num}")

    elif tipo == 'assignments':
        assignments = db.get_assignments(user.get('grade', '2'), SCHOOL_ID)
        if not assignments:
            send_fn(from_num, "SYNC_RES|assignments|EMPTY|No hay tareas pendientes")
            return
        for a in assignments[:5]:
            desc = a.get('description', '') or ''
            deadline = a.get('deadline', '') or ''
            send_fn(from_num, f"TAREA|{a['id']}|{user['id']}|{a.get('title','')}|{desc}|{deadline}")
            time.sleep(3)
        send_fn(from_num, "SYNC_END|assignments")

    elif tipo == 'progress':
        progress = db.get_student_progress(user['id'])
        if progress:
            send_fn(from_num, f"SYNC_RES|progress|{json.dumps(progress, default=str)}")
        else:
            send_fn(from_num, "SYNC_RES|progress|EMPTY|Sin datos de progreso")

    elif tipo == 'questions':
        if user['role'] != 'teacher':
            return
        questions = db.get_unanswered_questions(SCHOOL_ID)
        if not questions:
            send_fn(from_num, "SYNC_RES|questions|EMPTY|No hay preguntas pendientes")
            return
        for q in questions[:5]:
            student_name = q.get('roster', {}).get('name', 'Alumno') if isinstance(q.get('roster'), dict) else 'Alumno'
            send_fn(from_num, f"PREGUNTA_PEND|{q['id']}|{student_name}|{q['question'][:120]}")
            time.sleep(3)
        send_fn(from_num, "SYNC_END|questions")

    elif tipo == 'submissions':
        if user['role'] != 'teacher':
            return
        subs = db.get_all_submissions(SCHOOL_ID)
        if not subs:
            send_fn(from_num, "SYNC_RES|submissions|EMPTY|No hay entregas")
            return
        for s in subs[:10]:
            student_name = s.get('roster', {}).get('name', 'Alumno') if isinstance(s.get('roster'), dict) else 'Alumno'
            response_preview = (s.get('response', '') or '')[:100]
            ai_score = s.get('ai_score', '') or ''
            ai_feedback = (s.get('ai_feedback', '') or '')[:100]
            send_fn(from_num, f"ENTREGA_RES|{s['id']}|{s.get('assignment_id','')}|{student_name}|{response_preview}|{ai_score}|{ai_feedback}")
            time.sleep(3)
        send_fn(from_num, "SYNC_END|submissions")

    elif tipo == 'lesson_content':
        if len(parts) < 3:
            return
        lesson_id = parts[2]
        lesson = db.get_lesson(lesson_id)
        if lesson:
            content = lesson.get('content', '')
            send_fn(from_num, f"LECCION_FULL|{lesson_id}|{content}")

    db.log_connection(user['id'], SCHOOL_ID, from_num)


# ==================== TEST + PROGRESO (v4) ====================

def _handle_test_response(text, from_id, from_num, send_fn, publish_mqtt):
    """TEST_RES|activity_id|student_id|answer — Registrar respuesta de test"""
    parts = text.split('|')
    if len(parts) < 4:
        return
    activity_id = parts[1]
    answer = parts[3]
    user = db.get_user_by_node(from_num)
    if not user:
        return
    db.save_activity_submission(activity_id, user['id'], answer, 'test')
    send_fn(from_num, f"TEST_OK|{activity_id}|{answer}")
    logging.info(f"Test respondido: {user['name']} -> {answer}")


def _handle_progress_update(text, from_id, from_num, send_fn, publish_mqtt):
    """PROGRESO|lesson_id|chapter|activity — Alumno reporta progreso"""
    parts = text.split('|')
    if len(parts) < 4:
        return
    user = db.get_user_by_node(from_num)
    if not user:
        return
    db.upsert_student_progress(user['id'], parts[1], int(parts[2]), int(parts[3]))
    logging.info(f"Progreso: {user['name']} leccion {parts[1][:8]} cap {parts[2]} act {parts[3]}")


# ==================== ROSTER ====================

def _handle_roster_request(text, from_id, from_num, send_fn, publish_mqtt):
    """ROSTER_REQ|node_id"""
    user = db.get_user_by_node(from_num)

    if user:
        has_pin = '1' if user.get('pin') else '0'
        child_id = user.get('parent_of') or ''
        send_fn(from_num, f"ROSTER_RES|{user['id']}|{user['name']}|{user['role']}|{user.get('grade','')}|{child_id}|{has_pin}")
        logging.info(f"Roster: {user['name']} ({user['role']}) -- nodo {hex(from_num)}")
        db.log_connection(user['id'], SCHOOL_ID, from_num)
    else:
        send_fn(from_num, "ROSTER_RES|UNKNOWN")
        logging.warning(f"Nodo no registrado: {hex(from_num)}")


def _handle_roster_pin(text, from_id, from_num, send_fn, publish_mqtt):
    """ROSTER_PIN|user_id|pin"""
    parts = text.split('|')
    if len(parts) < 3:
        return

    user_id = parts[1]
    pin = parts[2]

    user = db.get_user_by_node(from_num)
    if user and user.get('pin') == pin:
        send_fn(from_num, f"ROSTER_PIN_OK|{user_id}")
        logging.info(f"PIN validado: {user['name']}")
    else:
        send_fn(from_num, f"ROSTER_PIN_FAIL|{user_id}")
        logging.warning(f"PIN incorrecto para nodo {hex(from_num)}")

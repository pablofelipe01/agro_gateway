"""Handler de imagenes - Fragmentacion + Claude Vision + Result ACK"""
import os
import base64
import logging
import time
from datetime import datetime
from anthropic import Anthropic

anthropic_client = None
image_buffers = {}
user_context = {}  # Last text message per user (from_num -> text)
result_cache = {}  # Cached results for retry (img_id -> {parts, total, from_num, retry_count, sent_at})

PROMPTS_BY_TYPE = {
    'plaga': """Analiza esta imagen de una planta. Identifica:
1. Especie de planta si es posible
2. Enfermedad, plaga o deficiencia visible
3. Severidad (leve/moderada/grave)
4. Recomendacion de tratamiento
Responde en espanol colombiano, maximo 200 palabras. Se practico - el agricultor necesita saber QUE HACER.""",

    'suelo': """Analiza esta imagen de suelo agricola. Identifica:
1. Tipo de suelo visible
2. Problemas evidentes (erosion, compactacion, encharcamiento)
3. Recomendaciones practicas
Responde en espanol colombiano, maximo 150 palabras.""",

    'cultivo': """Analiza esta imagen de cultivo. Identifica:
1. Estado general del cultivo
2. Etapa de crecimiento
3. Problemas visibles
4. Recomendaciones
Responde en espanol colombiano, maximo 200 palabras.""",

    'general': """Describe esta imagen agricola y da recomendaciones practicas relevantes. Responde en espanol colombiano, maximo 200 palabras."""
}

MAX_BUFFER_AGE_SECONDS = 300  # 5 minutos
MAX_CONCURRENT_IMAGES = 3
ACK_EVERY_N_CHUNKS = 10
MAX_RETRY_ROUNDS = 4
MAX_RESULT_RETRY_ROUNDS = 4

def init(api_key):
    global anthropic_client
    anthropic_client = Anthropic(api_key=api_key)

def cleanup_old_buffers():
    """Limpiar buffers viejos y result caches expirados"""
    now = datetime.now()
    expired = []
    for img_id, buf in image_buffers.items():
        age = (now - buf['started_at']).total_seconds()
        if age > MAX_BUFFER_AGE_SECONDS:
            expired.append(img_id)
    for img_id in expired:
        logging.warning(f"Buffer expirado: {img_id}")
        del image_buffers[img_id]

    # Limpiar result caches expirados
    expired_results = []
    for img_id, cache in result_cache.items():
        age = (now - cache['sent_at']).total_seconds()
        if age > MAX_BUFFER_AGE_SECONDS:
            expired_results.append(img_id)
    for img_id in expired_results:
        logging.warning(f"Result cache expirado: {img_id}")
        del result_cache[img_id]


def store_context(from_num, text):
    """Guardar ultimo mensaje de texto del usuario como contexto para imagen"""
    if text and not text.startswith('IMG'):
        user_context[from_num] = {
            'text': text,
            'timestamp': datetime.now()
        }
        logging.info(f"Contexto guardado para {hex(from_num)}: {text[:50]}")


def handle(text, from_id, from_num, send_fn, publish_mqtt):
    """Router de mensajes IMG"""
    cleanup_old_buffers()

    if text.startswith('IMG_START|'):
        _handle_start(text, from_id, from_num, send_fn)
    elif text.startswith('IMG|'):
        _handle_chunk(text, from_id, from_num, send_fn)
    elif text.startswith('IMG_END|'):
        _handle_end(text, from_id, from_num, send_fn, publish_mqtt)
    elif text.startswith('IMG_RESULT_ACK|'):
        _handle_result_ack(text, from_id, from_num, send_fn)

def _handle_start(text, from_id, from_num, send_fn):
    """IMG_START|id|tipo|total_chunks|checksum"""
    try:
        parts = text.split('|')
        if len(parts) < 5:
            send_fn(from_num, "IMG_ERROR|unknown|formato_invalido")
            return

        img_id = parts[1]
        tipo = parts[2]
        total = int(parts[3])
        checksum = parts[4]

        if len(image_buffers) >= MAX_CONCURRENT_IMAGES:
            cleanup_old_buffers()
            if len(image_buffers) >= MAX_CONCURRENT_IMAGES:
                send_fn(from_num, f"IMG_ERROR|{img_id}|servidor_ocupado")
                return

        if img_id in image_buffers:
            # Update existing orphan buffer (chunks arrived before IMG_START)
            buf = image_buffers[img_id]
            buf['total'] = total
            buf['tipo'] = tipo
            buf['checksum'] = checksum
            logging.info(f"IMG_START (late): {img_id} - {total} chunks - tipo: {tipo} - ya tiene {len(buf['chunks'])} chunks")
        else:
            image_buffers[img_id] = {
                'chunks': {},
                'total': total,
                'tipo': tipo,
                'checksum': checksum,
                'from_num': from_num,
                'from_id': from_id,
                'started_at': datetime.now(),
                'last_chunk_at': datetime.now(),
                'ack_sent_at_count': 0,
                'retry_count': 0,
            }
            logging.info(f"IMG_START: {img_id} - {total} chunks - tipo: {tipo}")
    except Exception as e:
        logging.error(f"Error en IMG_START: {e}")

def _handle_chunk(text, from_id, from_num, send_fn):
    """IMG|id|chunk_num|base64_data"""
    try:
        parts = text.split('|', 3)
        if len(parts) < 4:
            return

        img_id = parts[1]
        chunk_num = int(parts[2])
        data = parts[3]

        if img_id not in image_buffers:
            # Auto-create orphan buffer (IMG_START may have been lost)
            logging.warning(f"Chunk sin IMG_START: {img_id} - creando buffer huerfano")
            image_buffers[img_id] = {
                'chunks': {},
                'total': 0,
                'tipo': 'general',
                'checksum': '',
                'from_num': from_num,
                'from_id': from_id,
                'started_at': datetime.now(),
                'last_chunk_at': datetime.now(),
                'ack_sent_at_count': 0,
                'retry_count': 0,
            }

        buf = image_buffers[img_id]
        buf['chunks'][chunk_num] = data
        buf['last_chunk_at'] = datetime.now()

        received = len(buf['chunks'])
        total = buf['total']

        # ACK parcial cada N chunks
        if received % ACK_EVERY_N_CHUNKS == 0 and received > buf['ack_sent_at_count']:
            buf['ack_sent_at_count'] = received
            total_str = str(total) if total > 0 else "?"
            send_fn(from_num, f"IMG_ACK|{img_id}|{received}/{total_str}")
            logging.info(f"ACK: {img_id} - {received}/{total_str}")

    except Exception as e:
        logging.error(f"Error en IMG chunk: {e}")

def _handle_end(text, from_id, from_num, send_fn, publish_mqtt):
    """IMG_END|id"""
    try:
        parts = text.split('|')
        if len(parts) < 2:
            return

        img_id = parts[1]

        if img_id not in image_buffers:
            logging.warning(f"IMG_END para imagen desconocida: {img_id}")
            send_fn(from_num, f"IMG_ERROR|{img_id}|imagen_no_encontrada")
            return

        buf = image_buffers[img_id]

        # Update total/checksum from IMG_END if orphan buffer (IMG_START was lost)
        if len(parts) >= 4:
            end_total = int(parts[2])
            end_checksum = parts[3]
            if buf['total'] == 0:
                buf['total'] = end_total
                buf['checksum'] = end_checksum
                logging.info(f"IMG_END completo buffer huerfano: {img_id} - total={end_total}")

        received = len(buf['chunks'])
        total = buf['total']

        # Verificar chunks faltantes
        missing = [i for i in range(total) if i not in buf['chunks']]

        if missing:
            buf['retry_count'] += 1
            if buf['retry_count'] > MAX_RETRY_ROUNDS:
                logging.error(f"Demasiados reintentos para {img_id}")
                send_fn(from_num, f"IMG_ERROR|{img_id}|chunks_perdidos")
                del image_buffers[img_id]
                return

            missing_str = ','.join(str(m) for m in missing[:20])  # Max 20 en un mensaje
            send_fn(from_num, f"IMG_RETRY|{img_id}|{missing_str}")
            logging.info(f"RETRY: {img_id} - faltan {len(missing)} chunks")
            return

        # Imagen completa
        logging.info(f"Imagen completa: {img_id} - {total} chunks")
        send_fn(from_num, f"IMG_OK|{img_id}")

        # Reensamblar y analizar
        _reassemble_and_analyze(img_id, send_fn, publish_mqtt)

    except Exception as e:
        logging.error(f"Error en IMG_END: {e}")

def _reassemble_and_analyze(img_id, send_fn, publish_mqtt):
    """Reensamblar imagen y enviar a Claude Vision"""
    try:
        buf = image_buffers[img_id]
        from_num = buf['from_num']
        tipo = buf['tipo']

        # Reensamblar base64
        full_base64 = ''.join(buf['chunks'][i] for i in range(buf['total']))

        # Verificar que es base64 valido
        try:
            image_bytes = base64.b64decode(full_base64)
            logging.info(f"Imagen reensamblada: {len(image_bytes)} bytes")
        except Exception as e:
            logging.error(f"Base64 invalido: {e}")
            send_fn(from_num, f"IMG_ERROR|{img_id}|imagen_corrupta")
            del image_buffers[img_id]
            return

        # Guardar imagen localmente (backup)
        try:
            img_path = f"/tmp/mesh_img_{img_id}.jpg"
            with open(img_path, 'wb') as f:
                f.write(image_bytes)
            logging.info(f"Imagen guardada: {img_path}")
        except:
            pass

        # Enviar a Claude Vision
        if not anthropic_client:
            send_fn(from_num, f"IMG_ERROR|{img_id}|api_no_disponible")
            del image_buffers[img_id]
            return

        base_prompt = PROMPTS_BY_TYPE.get(tipo, PROMPTS_BY_TYPE['general'])

        # Incluir contexto del usuario si existe
        context_text = ""
        if from_num in user_context:
            ctx = user_context[from_num]
            age = (datetime.now() - ctx['timestamp']).total_seconds()
            if age < 300:  # Contexto valido por 5 minutos
                context_text = ctx['text']
                del user_context[from_num]  # Consumir contexto
                logging.info(f"Usando contexto del usuario: {context_text[:80]}")

        if context_text:
            prompt = base_prompt + "\n\nIMPORTANTE - El usuario envio este mensaje junto con la foto:\n\"" + context_text + "\"\nResponde enfocandote en lo que el usuario pregunta o describe."
        else:
            prompt = base_prompt

        logging.info(f"Enviando a Claude Vision: {img_id} ({tipo})")

        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            temperature=0.3,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": full_base64
                        }
                    },
                    {"type": "text", "text": prompt}
                ]
            }]
        )

        result_text = response.content[0].text
        logging.info(f"Diagnostico recibido: {len(result_text)} chars")

        # Enviar resultado (fragmentar si es largo) y cachear para reintentos
        _send_result(img_id, result_text, from_num, send_fn)

        publish_mqtt("image/analyzed", {
            "image_id": img_id,
            "tipo": tipo,
            "result_length": len(result_text),
            "image_size": len(image_bytes),
            "chunks": buf['total'],
            "timestamp": datetime.now().isoformat()
        })

        # Limpiar buffer de imagen (pero resultado queda en cache)
        del image_buffers[img_id]

    except Exception as e:
        logging.error(f"Error analizando imagen: {e}")
        import traceback
        logging.error(traceback.format_exc())
        if img_id in image_buffers:
            send_fn(image_buffers[img_id]['from_num'], f"IMG_ERROR|{img_id}|error_analisis")
            del image_buffers[img_id]

def _send_result(img_id, result_text, from_num, send_fn):
    """Enviar resultado fragmentado y cachear para reintentos"""
    max_bytes = 150  # Content only - header IMG_RESULT|xxxx|n/n| adds ~25 bytes

    encoded = result_text.encode('utf-8')

    if len(encoded) <= max_bytes:
        send_fn(from_num, f"IMG_RESULT|{img_id}|1/1|{result_text}")
        # Cache for retry
        result_cache[img_id] = {
            'parts': {1: result_text},
            'total': 1,
            'from_num': from_num,
            'retry_count': 0,
            'sent_at': datetime.now()
        }
        logging.info(f"Resultado enviado (1 parte, cacheado)")
        return

    # Fragmentar por palabras
    words = result_text.split()
    chunks = []
    current = ""

    for word in words:
        test = f"{current} {word}".strip() if current else word
        if len(test.encode('utf-8')) > max_bytes:
            if current:
                chunks.append(current)
            current = word
        else:
            current = test
    if current:
        chunks.append(current)

    total = len(chunks)
    parts_dict = {}
    for i, chunk in enumerate(chunks):
        idx = i + 1  # 1-based
        parts_dict[idx] = chunk
        send_fn(from_num, f"IMG_RESULT|{img_id}|{idx}/{total}|{chunk}")
        time.sleep(2)

    # Cache all parts for retry
    result_cache[img_id] = {
        'parts': parts_dict,
        'total': total,
        'from_num': from_num,
        'retry_count': 0,
        'sent_at': datetime.now()
    }

    logging.info(f"Resultado enviado en {total} partes (cacheado para reintentos)")

def _handle_result_ack(text, from_id, from_num, send_fn):
    """IMG_RESULT_ACK|imageId|OK  o  IMG_RESULT_ACK|imageId|1,4,6 (partes faltantes)"""
    try:
        parts = text.split('|')
        if len(parts) < 3:
            return

        img_id = parts[1]
        status = parts[2]

        if img_id not in result_cache:
            logging.warning(f"RESULT_ACK para imagen no cacheada: {img_id}")
            return

        cache = result_cache[img_id]

        if status == 'OK':
            logging.info(f"Resultado confirmado por app: {img_id}")
            del result_cache[img_id]
            return

        # Partes faltantes - parsear indices
        try:
            missing = [int(x.strip()) for x in status.split(',') if x.strip()]
        except ValueError:
            logging.error(f"Formato ACK invalido: {status}")
            return

        cache['retry_count'] += 1
        if cache['retry_count'] > MAX_RESULT_RETRY_ROUNDS:
            logging.error(f"Maximo reintentos de resultado alcanzado: {img_id}")
            del result_cache[img_id]
            return

        logging.info(f"Reenviando {len(missing)} partes de resultado: {img_id} (intento {cache['retry_count']}/{MAX_RESULT_RETRY_ROUNDS})")

        total = cache['total']
        resent = 0
        for idx in missing:
            if idx in cache['parts']:
                send_fn(cache['from_num'], f"IMG_RESULT|{img_id}|{idx}/{total}|{cache['parts'][idx]}")
                resent += 1
                time.sleep(2)

        logging.info(f"Reenviadas {resent}/{len(missing)} partes de resultado: {img_id}")

    except Exception as e:
        logging.error(f"Error en RESULT_ACK: {e}")

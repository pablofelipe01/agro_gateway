"""Handler de imágenes - Fragmentación + Claude Vision"""
import os
import base64
import logging
import time
from datetime import datetime
from anthropic import Anthropic

anthropic_client = None
image_buffers = {}

PROMPTS_BY_TYPE = {
    'plaga': """Analiza esta imagen de una planta. Identifica:
1. Especie de planta si es posible
2. Enfermedad, plaga o deficiencia visible
3. Severidad (leve/moderada/grave)
4. Recomendación de tratamiento
Responde en español colombiano, máximo 200 palabras. Sé práctico — el agricultor necesita saber QUÉ HACER.""",
    
    'suelo': """Analiza esta imagen de suelo agrícola. Identifica:
1. Tipo de suelo visible
2. Problemas evidentes (erosión, compactación, encharcamiento)
3. Recomendaciones prácticas
Responde en español colombiano, máximo 150 palabras.""",

    'cultivo': """Analiza esta imagen de cultivo. Identifica:
1. Estado general del cultivo
2. Etapa de crecimiento
3. Problemas visibles
4. Recomendaciones
Responde en español colombiano, máximo 200 palabras.""",

    'general': """Describe esta imagen agrícola y da recomendaciones prácticas relevantes. Responde en español colombiano, máximo 200 palabras."""
}

MAX_BUFFER_AGE_SECONDS = 300  # 5 minutos
MAX_CONCURRENT_IMAGES = 3
ACK_EVERY_N_CHUNKS = 10
MAX_RETRY_ROUNDS = 2

def init(api_key):
    global anthropic_client
    anthropic_client = Anthropic(api_key=api_key)

def cleanup_old_buffers():
    """Limpiar buffers viejos"""
    now = datetime.now()
    expired = []
    for img_id, buf in image_buffers.items():
        age = (now - buf['started_at']).total_seconds()
        if age > MAX_BUFFER_AGE_SECONDS:
            expired.append(img_id)
    for img_id in expired:
        logging.warning(f"🗑️ Buffer expirado: {img_id}")
        del image_buffers[img_id]

def handle(text, from_id, from_num, send_fn, publish_mqtt):
    """Router de mensajes IMG"""
    cleanup_old_buffers()
    
    if text.startswith('IMG_START|'):
        _handle_start(text, from_id, from_num, send_fn)
    elif text.startswith('IMG|'):
        _handle_chunk(text, from_id, from_num, send_fn)
    elif text.startswith('IMG_END|'):
        _handle_end(text, from_id, from_num, send_fn, publish_mqtt)

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
        
        logging.info(f"📸 IMG_START: {img_id} - {total} chunks - tipo: {tipo}")
    except Exception as e:
        logging.error(f"❌ Error en IMG_START: {e}")

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
            logging.warning(f"⚠️ Chunk para imagen desconocida: {img_id}")
            return
        
        buf = image_buffers[img_id]
        buf['chunks'][chunk_num] = data
        buf['last_chunk_at'] = datetime.now()
        
        received = len(buf['chunks'])
        total = buf['total']
        
        # ACK parcial cada N chunks
        if received % ACK_EVERY_N_CHUNKS == 0 and received > buf['ack_sent_at_count']:
            buf['ack_sent_at_count'] = received
            send_fn(from_num, f"IMG_ACK|{img_id}|{received}/{total}")
            logging.info(f"📸 ACK: {img_id} - {received}/{total}")
        
    except Exception as e:
        logging.error(f"❌ Error en IMG chunk: {e}")

def _handle_end(text, from_id, from_num, send_fn, publish_mqtt):
    """IMG_END|id"""
    try:
        parts = text.split('|')
        if len(parts) < 2:
            return
        
        img_id = parts[1]
        
        if img_id not in image_buffers:
            logging.warning(f"⚠️ IMG_END para imagen desconocida: {img_id}")
            send_fn(from_num, f"IMG_ERROR|{img_id}|imagen_no_encontrada")
            return
        
        buf = image_buffers[img_id]
        received = len(buf['chunks'])
        total = buf['total']
        
        # Verificar chunks faltantes
        missing = [i for i in range(total) if i not in buf['chunks']]
        
        if missing:
            buf['retry_count'] += 1
            if buf['retry_count'] > MAX_RETRY_ROUNDS:
                logging.error(f"❌ Demasiados reintentos para {img_id}")
                send_fn(from_num, f"IMG_ERROR|{img_id}|chunks_perdidos")
                del image_buffers[img_id]
                return
            
            missing_str = ','.join(str(m) for m in missing[:20])  # Max 20 en un mensaje
            send_fn(from_num, f"IMG_RETRY|{img_id}|{missing_str}")
            logging.info(f"📸 RETRY: {img_id} - faltan {len(missing)} chunks")
            return
        
        # Imagen completa
        logging.info(f"📸 Imagen completa: {img_id} - {total} chunks")
        send_fn(from_num, f"IMG_OK|{img_id}")
        
        # Reensamblar y analizar
        _reassemble_and_analyze(img_id, send_fn, publish_mqtt)
        
    except Exception as e:
        logging.error(f"❌ Error en IMG_END: {e}")

def _reassemble_and_analyze(img_id, send_fn, publish_mqtt):
    """Reensamblar imagen y enviar a Claude Vision"""
    try:
        buf = image_buffers[img_id]
        from_num = buf['from_num']
        tipo = buf['tipo']
        
        # Reensamblar base64
        full_base64 = ''.join(buf['chunks'][i] for i in range(buf['total']))
        
        # Verificar que es base64 válido
        try:
            image_bytes = base64.b64decode(full_base64)
            logging.info(f"📸 Imagen reensamblada: {len(image_bytes)} bytes")
        except Exception as e:
            logging.error(f"❌ Base64 inválido: {e}")
            send_fn(from_num, f"IMG_ERROR|{img_id}|imagen_corrupta")
            del image_buffers[img_id]
            return
        
        # Guardar imagen localmente (backup)
        try:
            img_path = f"/tmp/mesh_img_{img_id}.jpg"
            with open(img_path, 'wb') as f:
                f.write(image_bytes)
            logging.info(f"📸 Imagen guardada: {img_path}")
        except:
            pass
        
        # Enviar a Claude Vision
        if not anthropic_client:
            send_fn(from_num, f"IMG_ERROR|{img_id}|api_no_disponible")
            del image_buffers[img_id]
            return
        
        prompt = PROMPTS_BY_TYPE.get(tipo, PROMPTS_BY_TYPE['general'])
        
        logging.info(f"📸 Enviando a Claude Vision: {img_id} ({tipo})")
        
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
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
        logging.info(f"📸 Diagnóstico recibido: {len(result_text)} chars")
        
        # Enviar resultado (fragmentar si es largo)
        _send_result(img_id, result_text, from_num, send_fn)
        
        publish_mqtt("image/analyzed", {
            "image_id": img_id,
            "tipo": tipo,
            "result_length": len(result_text),
            "image_size": len(image_bytes),
            "chunks": buf['total'],
            "timestamp": datetime.now().isoformat()
        })
        
        # Limpiar buffer
        del image_buffers[img_id]
        
    except Exception as e:
        logging.error(f"❌ Error analizando imagen: {e}")
        import traceback
        logging.error(traceback.format_exc())
        if img_id in image_buffers:
            send_fn(image_buffers[img_id]['from_num'], f"IMG_ERROR|{img_id}|error_analisis")
            del image_buffers[img_id]

def _send_result(img_id, result_text, from_num, send_fn):
    """Enviar resultado, fragmentando si es necesario"""
    max_bytes = 210  # Dejar espacio para header IMG_RESULT|xxxx|
    
    encoded = result_text.encode('utf-8')
    
    if len(encoded) <= max_bytes:
        send_fn(from_num, f"IMG_RESULT|{img_id}|1/1|{result_text}")
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
    for i, chunk in enumerate(chunks):
        send_fn(from_num, f"IMG_RESULT|{img_id}|{i+1}/{total}|{chunk}")
        time.sleep(2)
    
    logging.info(f"📸 Resultado enviado en {total} partes")

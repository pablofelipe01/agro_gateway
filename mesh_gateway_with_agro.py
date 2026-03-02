#!/usr/bin/env python3
"""
Mesh Gateway v4.1 - Multi-Bot con Personal/Familia + Agro Sirius
"""
import os
import time
import logging
from datetime import datetime
from threading import Thread
import requests
import meshtastic
import meshtastic.serial_interface
from anthropic import Anthropic
from pubsub import pub
import paho.mqtt.client as mqtt
import json
import gspread
from google.oauth2.service_account import Credentials

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/mesh_gateway.log'),
        logging.StreamHandler()
    ]
)

# Configuración desde variables de entorno
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_IDS = os.getenv('TELEGRAM_CHAT_IDS', '').split(',')

# Configuración MQTT
MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883
MQTT_TOPIC_BASE = "msh/gateway"

# Constantes
MAX_CHARS_PER_MESSAGE = 200
FRAGMENT_DELAY = 5
MAX_FRAGMENTS = 5

# Archivo de contactos
CONTACTOS_FILE = "/home/pi/agro_gateway/config/contactos.json"

# Configuración Google Sheets - AGRO
AGRO_CREDENTIALS_FILE = '/home/pi/agro_gateway/credentials.json'
AGRO_SPREADSHEET_ID = '1O45DA8vVGApEPV8pBSZ0AojYzrcSe-ixN7noKPR3Ciw'
AGRO_SHEET_NAME = 'Sheet1'

# Configuración Airtable - Registro de Visitantes
AIRTABLE_API_TOKEN = os.getenv('AIRTABLE_API_TOKEN', '')
AIRTABLE_BASE_ID = 'apptwIqTras1uPNOc'
AIRTABLE_TABLE_NAME = 'Registro Visitantes'

# Validar configuración
if not ANTHROPIC_API_KEY:
    logging.error("ANTHROPIC_API_KEY no configurada")
    exit(1)

if not TELEGRAM_BOT_TOKEN:
    logging.warning("TELEGRAM_BOT_TOKEN no configurada")

# Inicializar clientes
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Cliente MQTT
mqtt_client = None
try:
    mqtt_client = mqtt.Client()
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()
    logging.info("✓ Conectado a broker MQTT")
except Exception as e:
    logging.warning(f"⚠️ No se pudo conectar a MQTT: {e}")
    mqtt_client = None

# Variables globales
interface_global = None
last_sender_id = None
contactos_db = {}
familia_messages_tracking = {}  # {telegram_id: {"mesh_user": mesh_id, "timestamp": time}}
agro_sheets_client = None  # Cliente Google Sheets para AGRO
pending_visits = {}  # {visitor_name_lower: airtable_record_id}

# ==================== CARGAR CONTACTOS ====================

def load_contactos():
    """Cargar base de datos de contactos"""
    global contactos_db
    try:
        if os.path.exists(CONTACTOS_FILE):
            with open(CONTACTOS_FILE, 'r', encoding='utf-8') as f:
                contactos_db = json.load(f)
            user_count = len(contactos_db.get('users', {}))
            logging.info(f"✓ Contactos cargados: {user_count} usuarios")
        else:
            logging.warning(f"⚠️ Archivo de contactos no encontrado: {CONTACTOS_FILE}")
            contactos_db = {"version": "1.0", "users": {}}
    except Exception as e:
        logging.error(f"❌ Error cargando contactos: {e}")
        contactos_db = {"version": "1.0", "users": {}}

def find_contact(mesh_user_id, alias):
    """Buscar contacto por alias"""
    user = contactos_db.get('users', {}).get(mesh_user_id)
    if not user:
        return None
    
    alias_lower = alias.lower().strip()
    
    for contact_key, contact_data in user.get('contacts', {}).items():
        # Buscar en aliases
        if alias_lower in [a.lower() for a in contact_data.get('alias', [])]:
            return contact_data
        # Buscar por key
        if alias_lower == contact_key.lower():
            return contact_data
    
    return None


def find_mesh_contact(telegram_user_id, alias):
    """Buscar contacto mesh desde Telegram por alias"""
    telegram_user = contactos_db.get("telegram_users", {}).get(telegram_user_id)
    if not telegram_user:
        return None
    
    alias_lower = alias.lower().strip()
    
    for contact_key, contact_data in telegram_user.get("mesh_contacts", {}).items():
        if alias_lower in [a.lower() for a in contact_data.get("alias", [])]:
            return contact_data
        if alias_lower == contact_key.lower():
            return contact_data
    
    return None

# ==================== AGRO SIRIUS - GOOGLE SHEETS ====================

def init_agro_sheets():
    """Inicializar conexión con Google Sheets para AGRO"""
    try:
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_file(AGRO_CREDENTIALS_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(AGRO_SPREADSHEET_ID).worksheet(AGRO_SHEET_NAME)
        logging.info(f"✅ Conectado a Google Sheets AGRO")
        return sheet
    except Exception as e:
        logging.error(f"❌ Error conectando a Google Sheets AGRO: {e}")
        return None

def parse_siembra_message(message):
    """Parsear mensaje de siembra: SIEMBRA|fecha|hora|cultivo|variedad|lote|sector|hectareas|gps|notas"""
    try:
        parts = message.split('|')
        if len(parts) != 10 or parts[0] != 'SIEMBRA':
            return None
        
        data = {
            'fecha': parts[1],
            'hora': parts[2],
            'cultivo': parts[3],
            'variedad': parts[4],
            'lote': parts[5],
            'sector': parts[6],
            'hectareas': parts[7],
            'gps': parts[8],
            'notas': parts[9]
        }
        
        # Parsear GPS
        if data['gps'] != 'sin-gps' and ',' in data['gps']:
            gps_parts = data['gps'].split(',')
            data['gps_lat'] = gps_parts[0]
            data['gps_lon'] = gps_parts[1]
        else:
            data['gps_lat'] = ''
            data['gps_lon'] = ''
        
        # Limpiar notas
        if data['notas'] == 'sin-notas':
            data['notas'] = ''
        
        return data
    except Exception as e:
        logging.error(f"❌ Error parseando SIEMBRA: {e}")
        return None

def generate_siembra_id(node_id):
    """Generar ID único para registro de siembra"""
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    return f"ID-{timestamp}-{node_id}"

def save_siembra_to_sheets(siembra_id, node_id, data):
    """Guardar registro de siembra en Google Sheets"""
    global agro_sheets_client
    
    if not agro_sheets_client:
        logging.error("❌ Google Sheets no inicializado")
        return False
    
    try:
        timestamp = f"{data['fecha']} {data['hora']}"
        
        row = [
            siembra_id,
            timestamp,
            str(node_id),
            data['cultivo'],
            data['variedad'],
            data['lote'],
            data['sector'],
            data['hectareas'],
            data['gps_lat'],
            data['gps_lon'],
            data['notas']
        ]
        
        agro_sheets_client.append_row(row)
        logging.info(f"✅ Siembra guardada: {siembra_id}")
        
        publish_mqtt("agro/siembra_saved", {
            "id": siembra_id,
            "node": node_id,
            "cultivo": data['cultivo'],
            "timestamp": timestamp
        })
        
        return True
    except Exception as e:
        logging.error(f"❌ Error guardando siembra: {e}")
        return False

def handle_siembra_message(text, from_id, from_num):
    """Procesar mensaje de siembra"""
    try:
        logging.info(f"🌱 Procesando registro de siembra de {from_id}")
        
        # Parsear mensaje
        data = parse_siembra_message(text)
        if not data:
            logging.error("❌ Formato SIEMBRA inválido")
            send_private_message(interface_global, from_num, 
                "❌ Error: Formato de siembra inválido")
            return
        
        # Generar ID
        siembra_id = generate_siembra_id(from_id)
        
        # Guardar en Google Sheets
        success = save_siembra_to_sheets(siembra_id, from_id, data)
        
        # Enviar respuesta
        if success:
            response = f"SIEMBRA_OK|{siembra_id}|Registrado exitosamente"
            send_private_message(interface_global, from_num, response)
            logging.info(f"✅ Siembra confirmada a {from_id}: {siembra_id}")
        else:
            response = f"SIEMBRA_ERROR|{siembra_id}|Error al guardar"
            send_private_message(interface_global, from_num, response)
            logging.error(f"❌ Error confirmando siembra a {from_id}")
        
    except Exception as e:
        logging.error(f"❌ Error en handle_siembra_message: {e}")
        import traceback
        logging.error(traceback.format_exc())


# ==================== AIRTABLE - REGISTRO VISITANTES ====================

def handle_registro_message(text, from_id, from_num):
    """Procesar mensaje REGISTRO de visitantes y guardar en Airtable"""
    try:
        parts = text.split('|')
        if len(parts) < 7:
            logging.error(f"❌ Formato REGISTRO inválido: {text}")
            send_private_message(interface_global, from_num, f"REGISTRO_ERROR|formato_invalido")
            return
        
        # REGISTRO|STATUS|nombre|motivo|area|supervisor|comentario
        status = parts[1]
        nombre = parts[2]
        motivo = parts[3]
        area = parts[4]
        supervisor = parts[5]
        comentario = parts[6] if len(parts) > 6 else ""
        
        logging.info(f"📋 Registrando visitante: {nombre} - Estado: {status}")
        
        # Preparar datos para Airtable
        fields = {
            "Nombre Visitante": nombre,
            "Motivo": motivo,
            "Area": area,
            "Fecha Solicitud": datetime.now().isoformat(),
            "Estado": status,
            "Supervisor": supervisor,
            "Comentario": comentario,
            "Nodo Origen": str(from_id)
        }
        
        # Si está aprobado, agregar hora de entrada
        if status.upper() == "APROBADO":
            fields["Hora Entrada"] = datetime.now().isoformat()
        
        # POST a Airtable
        import urllib.parse
        table_encoded = urllib.parse.quote(AIRTABLE_TABLE_NAME)
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_API_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {"fields": fields}
        
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        
        if response.status_code in [200, 201]:
            record = response.json()
            record_id = record.get('id', '')
            
            # Guardar en pending_visits si está aprobado
            if status.upper() == "APROBADO":
                pending_visits[nombre.lower()] = record_id
            
            logging.info(f"✅ Registro guardado en Airtable: {record_id}")
            send_private_message(interface_global, from_num, f"REGISTRO_OK|{nombre}")
            
            publish_mqtt("airtable/registro", {
                "record_id": record_id,
                "nombre": nombre,
                "status": status,
                "timestamp": datetime.now().isoformat()
            })
        else:
            logging.error(f"❌ Error Airtable: {response.status_code} - {response.text}")
            send_private_message(interface_global, from_num, f"REGISTRO_ERROR|{nombre}|{response.status_code}")
        
    except Exception as e:
        logging.error(f"❌ Error en handle_registro_message: {e}")
        import traceback
        logging.error(traceback.format_exc())
        send_private_message(interface_global, from_num, f"REGISTRO_ERROR|{str(e)}")


def handle_salida_message(text, from_id, from_num):
    """Procesar mensaje SALIDA y actualizar Airtable"""
    try:
        parts = text.split('|')
        if len(parts) < 2:
            logging.error(f"❌ Formato SALIDA inválido: {text}")
            send_private_message(interface_global, from_num, f"SALIDA_ERROR|formato_invalido")
            return
        
        nombre = parts[1]
        logging.info(f"🚪 Registrando salida de: {nombre}")
        
        # Buscar record_id en pending_visits
        record_id = pending_visits.get(nombre.lower())
        
        if not record_id:
            logging.warning(f"⚠️ No se encontró visita activa para: {nombre}")
            send_private_message(interface_global, from_num, f"SALIDA_ERROR|{nombre}|No se encontro visita activa")
            return
        
        # PATCH a Airtable
        import urllib.parse
        table_encoded = urllib.parse.quote(AIRTABLE_TABLE_NAME)
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}/{record_id}"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_API_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {"fields": {"Hora Salida": datetime.now().isoformat()}}
        
        response = requests.patch(url, headers=headers, json=payload, timeout=15)
        
        if response.status_code == 200:
            # Eliminar de pending_visits
            del pending_visits[nombre.lower()]
            
            logging.info(f"✅ Salida registrada para: {nombre}")
            send_private_message(interface_global, from_num, f"SALIDA_OK|{nombre}")
            
            publish_mqtt("airtable/salida", {
                "record_id": record_id,
                "nombre": nombre,
                "timestamp": datetime.now().isoformat()
            })
        else:
            logging.error(f"❌ Error Airtable PATCH: {response.status_code} - {response.text}")
            send_private_message(interface_global, from_num, f"SALIDA_ERROR|{nombre}|{response.status_code}")
        
    except Exception as e:
        logging.error(f"❌ Error en handle_salida_message: {e}")
        import traceback
        logging.error(traceback.format_exc())
        send_private_message(interface_global, from_num, f"SALIDA_ERROR|{str(e)}")

# ==================== FUNCIONES AUXILIARES ====================

def publish_mqtt(topic, payload):
    """Publicar a MQTT"""
    if mqtt_client:
        try:
            full_topic = f"{MQTT_TOPIC_BASE}/{topic}"
            mqtt_client.publish(full_topic, json.dumps(payload))
        except Exception as e:
            logging.error(f"Error publicando a MQTT: {e}")

def clean_response(text):
    """Limpiar respuesta de Claude"""
    text = text.strip()
    if text and text[-1] in '.!?':
        return text
    
    search_range = min(100, len(text))
    for i in range(len(text)-1, len(text)-search_range-1, -1):
        if i >= 0 and text[i] in '.!?':
            return text[:i+1]
    
    if len(text) > 20:
        last_space = text.rfind(' ')
        if last_space > len(text) * 0.8:
            return text[:last_space] + "..."
    
    return text + "..."

def query_claude(message):
    """Consultar Claude AI"""
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            system="Eres un asistente educativo para zonas rurales de Colombia. "
                   "Responde de forma CONCISA, COMPLETA y DIRECTA. "
                   "CRÍTICO: Siempre termina tus respuestas con punto final.",
            messages=[{"role": "user", "content": message}]
        )
        raw_response = response.content[0].text
        cleaned_response = clean_response(raw_response)
        
        publish_mqtt("claude/response", {
            "query": message,
            "response": cleaned_response,
            "length": len(cleaned_response),
            "timestamp": datetime.now().isoformat()
        })
        
        return cleaned_response
    except Exception as e:
        logging.error(f"Error consultando Claude: {e}")
        return "Error al procesar consulta."

def send_telegram_message(text, chat_id=None):
    """Enviar mensaje por Telegram"""
    if not TELEGRAM_BOT_TOKEN:
        return
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        
        target_ids = [chat_id] if chat_id else [cid.strip() for cid in TELEGRAM_CHAT_IDS if cid.strip()]
        
        for tid in target_ids:
            payload = {"chat_id": tid, "text": text}
            response = requests.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                logging.info(f"✅ Mensaje Telegram enviado a {tid}")
            else:
                logging.error(f"❌ Error Telegram: HTTP {response.status_code}")
    except Exception as e:
        logging.error(f"❌ Error enviando Telegram: {e}")

def send_telegram_alert(message, location=None):
    """Enviar alerta de emergencia"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        return
    
    try:
        alert_text = f"🚨 ALERTA EMERGENCIA\n\n{message}"
        if location:
            alert_text += f"\n\n📍 Ubicación: {location}"
        
        send_telegram_message(alert_text)
    except Exception as e:
        logging.error(f"❌ Error enviando alerta: {e}")


# ==================== FUNCIONES MESH ====================

def send_private_message(interface, destination_id, message):
    """Enviar mensaje PRIVADO con fragmentación"""
    try:
        if len(message) <= MAX_CHARS_PER_MESSAGE:
            logging.info(f"🔒 Enviando mensaje PRIVADO ({len(message)} chars) a {hex(destination_id)}")
            interface.sendText(message, destinationId=destination_id)
            logging.info(f"✅ Mensaje privado enviado")
            
            publish_mqtt("mesh/sent_private", {
                "destination": hex(destination_id),
                "message": message,
                "length": len(message),
                "timestamp": datetime.now().isoformat()
            })
            return
        
        # Fragmentación
        logging.info(f"🔒📦 Mensaje privado largo ({len(message)} chars) - dividiendo")
        chars_per_fragment = MAX_CHARS_PER_MESSAGE - 10
        
        fragments = [message[i:i + chars_per_fragment] for i in range(0, len(message), chars_per_fragment)]
        
        if len(fragments) > MAX_FRAGMENTS:
            fragments = fragments[:MAX_FRAGMENTS]
            last_fragment = fragments[-1][:chars_per_fragment - 3]
            last_space = last_fragment.rfind(' ')
            if last_space > len(last_fragment) * 0.8:
                last_fragment = last_fragment[:last_space]
            fragments[-1] = last_fragment + "..."
        
        total_fragments = len(fragments)
        
        for i, fragment in enumerate(fragments, 1):
            prefixed_message = f"[{i}/{total_fragments}] {fragment}"
            interface.sendText(prefixed_message, destinationId=destination_id)
            logging.info(f"✅ Fragmento privado {i}/{total_fragments} enviado")
            
            if i < total_fragments:
                time.sleep(FRAGMENT_DELAY)
        
        logging.info(f"🎉 Todos los fragmentos privados enviados")
        
        publish_mqtt("mesh/sent_private", {
            "destination": hex(destination_id),
            "message": message,
            "fragments": total_fragments,
            "timestamp": datetime.now().isoformat()
        })
        
    except Exception as e:
        logging.error(f"❌ Error enviando mensaje privado: {e}")

def send_public_message(interface, message):
    """Enviar mensaje PÚBLICO"""
    try:
        if len(message) <= MAX_CHARS_PER_MESSAGE:
            logging.info(f"📢 Enviando mensaje PÚBLICO ({len(message)} chars)")
            interface.sendText(message, channelIndex=0)
            logging.info(f"✅ Mensaje público enviado")
            return
        
        # Fragmentación para mensajes públicos largos
        chars_per_fragment = MAX_CHARS_PER_MESSAGE - 10
        fragments = [message[i:i + chars_per_fragment] for i in range(0, len(message), chars_per_fragment)]
        
        if len(fragments) > MAX_FRAGMENTS:
            fragments = fragments[:MAX_FRAGMENTS]
        
        for i, fragment in enumerate(fragments, 1):
            prefixed_message = f"[{i}/{len(fragments)}] {fragment}"
            interface.sendText(prefixed_message, channelIndex=0)
            if i < len(fragments):
                time.sleep(FRAGMENT_DELAY)
        
        logging.info(f"🎉 Mensaje público enviado")
        
    except Exception as e:
        logging.error(f"❌ Error enviando mensaje público: {e}")

# ==================== BOT FAMILIA ====================

def handle_familia_bot(text, from_id, from_num):
    """Manejar mensajes del bot de familia"""
    try:
        # Parsear: @familia mamá: mensaje
        parts = text[8:].split(':', 1)  # Remover "@familia "
        
        if len(parts) < 2:
            logging.warning(f"⚠️ Formato incorrecto de @familia: {text}")
            send_private_message(interface_global, from_num, 
                "❌ Formato: @familia [contacto]: [mensaje]\nEjemplo: @familia mamá: Hola!")
            return
        
        contact_alias = parts[0].strip()
        message = parts[1].strip()
        
        # Buscar contacto
        contact = find_contact(from_id, contact_alias)
        
        if not contact:
            logging.warning(f"⚠️ Contacto '{contact_alias}' no encontrado para {from_id}")
            send_private_message(interface_global, from_num,
                f"❌ Contacto '{contact_alias}' no encontrado.\nContacta al administrador.")
            return
        
        # Obtener nombre del usuario
        user_data = contactos_db.get('users', {}).get(from_id, {})
        user_name = user_data.get('name', from_id)
        
        # Enviar a contacto por Telegram
        telegram_id = contact['telegram_id']
        contact_name = contact['name']
        
        familia_msg = f"💌 Mensaje de {user_name} (mesh):\n\n{message}\n\n⏰ {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\nPara responder: /reply [tu mensaje]"
        
        send_telegram_message(familia_msg, chat_id=telegram_id)
        
        # Tracking para /reply
        familia_messages_tracking[telegram_id] = {
            "mesh_user": from_id,
            "mesh_num": from_num,
            "timestamp": time.time()
        }
        
        # Confirmar al usuario mesh
        send_private_message(interface_global, from_num,
            f"✅ Mensaje enviado a {contact_name}")
        
        # Log MQTT
        publish_mqtt("familia/sent", {
            "from_mesh": from_id,
            "to_contact": contact_name,
            "to_telegram": telegram_id,
            "message": message,
            "timestamp": datetime.now().isoformat()
        })
        
        logging.info(f"👨‍👩‍👧 Mensaje familia: {user_name} → {contact_name}")
        
    except Exception as e:
        logging.error(f"❌ Error en handle_familia_bot: {e}")
        import traceback
        logging.error(traceback.format_exc())

# ==================== PROCESAMIENTO MESH ====================

def process_mesh_message(packet, interface):
    """Procesar mensajes de la mesh"""
    global last_sender_id
    
    try:
        if 'decoded' not in packet or 'payload' not in packet['decoded']:
            return
        
        payload = packet['decoded']['payload']
        text = payload.decode('utf-8') if isinstance(payload, bytes) else str(payload)
        
        from_id = packet.get('fromId', 'unknown')
        from_num = packet.get('from', 0)
        to_num = packet.get('to', None)
        
        is_direct_to_me = (to_num == interface.myInfo.my_node_num)
        
        if is_direct_to_me:
            logging.info(f"🔒 Mensaje PRIVADO de {from_id}: {text}")
        else:
            logging.info(f"📢 Mensaje PÚBLICO de {from_id}: {text}")
        
        last_sender_id = from_num
        
        # Publicar a MQTT
        publish_mqtt("mesh/received", {
            "from_id": from_id,
            "from_num": from_num,
            "message": text,
            "is_private": is_direct_to_me,
            "timestamp": datetime.now().isoformat()
        })
        
        # ROUTING DE COMANDOS
        
        # SIEMBRA (Agro Sirius)
        if text.startswith('SIEMBRA|'):
            logging.info(f"🌱 Mensaje SIEMBRA detectado")
            handle_siembra_message(text, from_id, from_num)
            return

        # REGISTRO visitantes (Airtable)
        if text.startswith('REGISTRO|') and is_direct_to_me:
            logging.info(f"📋 Mensaje REGISTRO detectado")
            handle_registro_message(text, from_id, from_num)
            return

        # SALIDA visitantes (Airtable)
        if text.startswith('SALIDA|') and is_direct_to_me:
            logging.info(f"🚪 Mensaje SALIDA detectado")
            handle_salida_message(text, from_id, from_num)
            return
        
        # Bot Familia
        if text.lower().startswith('@familia'):
            logging.info(f"👨‍👩‍👧 Activando bot familia")
            handle_familia_bot(text, from_id, from_num)
            return
        
        # Claude AI
        if text.lower().startswith('@claude'):
            query = text[7:].strip()
            logging.info(f"🤖 Consulta Claude (privado): {query}")
            
            publish_mqtt("claude/query", {
                "from_id": from_id,
                "query": query,
                "timestamp": datetime.now().isoformat()
            })
            
            response = query_claude(query)
            full_response = f"Claude: {response}"
            send_private_message(interface, from_num, full_response)
            return
        
        # Emergencias
        emergency_keywords = ['emergencia', 'ayuda', 'sos', 'peligro', 'urgente']
        if any(keyword in text.lower() for keyword in emergency_keywords):
            logging.warning(f"🚨 EMERGENCIA detectada de {from_id}")
            
            location = None
            if 'position' in packet:
                pos = packet['position']
                lat = pos.get('latitude', 0)
                lon = pos.get('longitude', 0)
                if lat and lon:
                    location = f"https://www.google.com/maps?q={lat},{lon}"
            
            publish_mqtt("emergency/alert", {
                "from_id": from_id,
                "message": text,
                "location": location,
                "timestamp": datetime.now().isoformat()
            })
            
            alert_msg = f"Usuario: {from_id}\nMensaje: {text}\nHora: {datetime.now()}"
            send_telegram_alert(alert_msg, location)
            send_public_message(interface, "🚨 Alerta de emergencia enviada a coordinadores.")
            return
        
        logging.info(f"💬 Mensaje normal procesado")
        
    except Exception as e:
        logging.error(f"❌ Error procesando mensaje: {e}")
        import traceback
        logging.error(traceback.format_exc())

# ==================== TELEGRAM POLLING ====================

def telegram_polling_loop():
    """Polling de Telegram"""
    if not TELEGRAM_BOT_TOKEN:
        return
    
    logging.info("📱 Iniciando Telegram polling...")
    
    # Limpiar webhook si existe (causa conflicto con polling)
    try:
        delete_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook"
        requests.get(delete_url, timeout=10)
        logging.info("🧹 Webhook limpiado (si existía)")
    except Exception as e:
        logging.warning(f"⚠️ Error limpiando webhook: {e}")
    offset = 0
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": offset, "timeout": 30}
            response = requests.get(url, params=params, timeout=35)
            
            if response.status_code == 200:
                data = response.json()
                for update in data.get('result', []):
                    process_telegram_message(update)
                    offset = update['update_id'] + 1
        except Exception as e:
            logging.error(f"❌ Error Telegram polling: {e}")
            time.sleep(5)

def process_telegram_message(update):
    """Procesar mensaje de Telegram"""
    global last_sender_id
    
    try:
        if 'message' not in update or 'text' not in update['message']:
            return
        
        text = update['message']['text']
        chat_id = str(update['message']['chat']['id'])
        
        # Solo chats autorizados
        if chat_id not in [cid.strip() for cid in TELEGRAM_CHAT_IDS if cid.strip()]:
            logging.warning(f"⚠️ Chat no autorizado: {chat_id}")
            return
        
        logging.info(f"📥 Telegram: {text}")
        
        # Comando /reply (respuesta de familia)
        if text.startswith('/reply'):
            reply_text = text.replace('/reply', '').strip()
            
            if not reply_text:
                send_telegram_message("❌ Uso: /reply [tu mensaje]", chat_id=chat_id)
                return
            
            # Buscar a quién responder
            tracking = familia_messages_tracking.get(chat_id)
            
            if not tracking:
                send_telegram_message("❌ No hay mensaje reciente para responder.", chat_id=chat_id)
                return
            
            # Verificar que no sea muy viejo (1 hora)
            if time.time() - tracking['timestamp'] > 3600:
                send_telegram_message("❌ Mensaje muy antiguo. Pide que te escriban de nuevo.", chat_id=chat_id)
                return
            
            mesh_num = tracking['mesh_num']
            
            # Encontrar nombre del contacto
            contact_name = "Familia"
            for user_data in contactos_db.get('users', {}).values():
                for contact in user_data.get('contacts', {}).values():
                    if contact['telegram_id'] == chat_id:
                        contact_name = contact['name']
                        break
            
            message_to_mesh = f"{contact_name}: {reply_text}"
            send_private_message(interface_global, mesh_num, message_to_mesh)
            send_telegram_message(f"✅ Respuesta enviada", chat_id=chat_id)
            logging.info(f"👨‍👩‍👧 Respuesta familia: {contact_name} → mesh")
            return
        
        # Comando /to (iniciar conversación con mesh)
        if text.startswith('/to'):
            parts = text[3:].split(':', 1)
            
            if len(parts) < 2:
                send_telegram_message("❌ Formato: /to [contacto]: [mensaje]\nEjemplo: /to pablo: Hola!", chat_id=chat_id)
                return
            
            contact_alias = parts[0].strip()
            message = parts[1].strip()
            
            # Buscar contacto mesh
            mesh_contact = find_mesh_contact(chat_id, contact_alias)
            
            if not mesh_contact:
                send_telegram_message(f"❌ Contacto '{contact_alias}' no encontrado.", chat_id=chat_id)
                return
            
            mesh_num = mesh_contact['mesh_num']
            mesh_name = mesh_contact['name']
            
            # Obtener nombre del usuario Telegram
            telegram_user = contactos_db.get('telegram_users', {}).get(chat_id, {})
            sender_name = telegram_user.get('name', 'Telegram')
            
            # Enviar a mesh
            message_to_mesh = f"{sender_name}: {message}"
            send_private_message(interface_global, mesh_num, message_to_mesh)
            
            # Guardar tracking para /reply
            familia_messages_tracking[chat_id] = {
                "mesh_user": mesh_contact['mesh_id'],
                "mesh_num": mesh_num,
                "timestamp": time.time()
            }
            
            send_telegram_message(f"✅ Mensaje enviado a {mesh_name}", chat_id=chat_id)
            logging.info(f"👨‍👩‍👧 Mensaje iniciado: {sender_name} → {mesh_name}")
            return


        # Comando /send
        if text.startswith('/send'):
            message = text.replace('/send', '').strip()
            if message:
                if last_sender_id:
                    logging.info(f"🔒 /send privado a {hex(last_sender_id)}")
                    send_private_message(interface_global, last_sender_id, f"TG: {message}")
                else:
                    logging.warning("⚠️ No hay último remitente")
                    send_public_message(interface_global, f"TG: {message}")
            return
        
        # Comando /broadcast
        if text.startswith('/broadcast'):
            message = text.replace('/broadcast', '').strip()
            if message:
                logging.info(f"📢 Broadcasting: {message}")
                send_public_message(interface_global, f"TG: {message}")
            return
        
    except Exception as e:
        logging.error(f"❌ Error procesando Telegram: {e}")


# ==================== MAIN ====================

def main():
    """Loop principal"""
    global interface_global, agro_sheets_client
    
    logging.info("🚀 Iniciando Mesh Gateway v4.1 - Multi-Bot + Agro Sirius")
    logging.info(f"🔒 Privacidad por defecto")
    logging.info(f"👨‍👩‍👧 Bot Familia activado")
    logging.info(f"🌱 Agro Sirius integrado")
    
    # Cargar contactos
    load_contactos()
    
    # Inicializar Google Sheets para AGRO
    agro_sheets_client = init_agro_sheets()
    if agro_sheets_client:
        logging.info("✓ Google Sheets AGRO listo")
    else:
        logging.warning("⚠️ Google Sheets AGRO no disponible")
    
    try:
        logging.info("Conectando a LoRa...")
        interface_global = meshtastic.serial_interface.SerialInterface()
        logging.info("✓ Conectado a LoRa")
    except Exception as e:
        logging.error(f"Error conectando LoRa: {e}")
        exit(1)
    
    # Telegram polling
    telegram_thread = Thread(target=telegram_polling_loop, daemon=True)
    telegram_thread.start()
    logging.info("✓ Telegram polling iniciado")
    
    def on_message(packet, interface=interface_global):
        try:
            if packet and 'decoded' in packet:
                process_mesh_message(packet, interface)
        except Exception as e:
            logging.error(f"Error: {e}")
    
    pub.subscribe(on_message, 'meshtastic.receive.text')
    logging.info("✓ Gateway activo")
    logging.info("📋 Comandos: SIEMBRA| | @familia [contacto]: [msg] | @claude | /send | /broadcast | /reply")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Gateway detenido")
    finally:
        if interface_global:
            interface_global.close()
        if mqtt_client:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()

if __name__ == "__main__":
    main()

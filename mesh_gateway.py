#!/usr/bin/env python3
"""
Mesh Gateway v5.0 - Modular Architecture
Router principal - único dueño de la conexión serial Meshtastic
"""
import os
import time
import logging
import json
from datetime import datetime
from threading import Thread
import meshtastic
import meshtastic.serial_interface
from pubsub import pub
import paho.mqtt.client as mqtt
import gspread
from google.oauth2.service_account import Credentials

from handlers import registro, siembra, claude_ai, telegram_bridge, image

# ==================== CONFIGURACIÓN ====================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('mesh_gateway.log'),
        logging.StreamHandler()
    ]
)

ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883
MQTT_TOPIC_BASE = "msh/gateway"
MAX_CHARS_PER_MESSAGE = 200
FRAGMENT_DELAY = 5
MAX_FRAGMENTS = 5
CONTACTOS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "contactos.json")
AGRO_CREDENTIALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")
AGRO_SPREADSHEET_ID = '1O45DA8vVGApEPV8pBSZ0AojYzrcSe-ixN7noKPR3Ciw'
AGRO_SHEET_NAME = 'Sheet1'

# Validar
if not ANTHROPIC_API_KEY:
    logging.error("ANTHROPIC_API_KEY no configurada")
    exit(1)

# ==================== VARIABLES GLOBALES ====================

interface_global = None
last_sender_id = None
contactos_db = {}
mqtt_client = None
agro_sheets_client = None

# ==================== INICIALIZACIÓN ====================

def init_mqtt():
    global mqtt_client
    try:
        mqtt_client = mqtt.Client()
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_start()
        logging.info("✓ Conectado a broker MQTT")
    except Exception as e:
        logging.warning(f"⚠️ No se pudo conectar a MQTT: {e}")
        mqtt_client = None

def init_sheets():
    global agro_sheets_client
    try:
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_file(AGRO_CREDENTIALS_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        agro_sheets_client = client.open_by_key(AGRO_SPREADSHEET_ID).worksheet(AGRO_SHEET_NAME)
        logging.info("✅ Conectado a Google Sheets AGRO")
        logging.info("✓ Google Sheets AGRO listo")
    except Exception as e:
        logging.error(f"❌ Error conectando a Google Sheets AGRO: {e}")

def load_contactos():
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

def init_handlers():
    claude_ai.init(ANTHROPIC_API_KEY)
    image.init(ANTHROPIC_API_KEY)
    logging.info("✓ Handlers inicializados")

# ==================== FUNCIONES MESH ====================

def publish_mqtt_msg(topic, payload):
    if mqtt_client:
        try:
            mqtt_client.publish(f"{MQTT_TOPIC_BASE}/{topic}", json.dumps(payload))
        except Exception as e:
            logging.error(f"Error MQTT: {e}")

def send_private_message(interface, destination_id, message):
    try:
        if len(message) <= MAX_CHARS_PER_MESSAGE:
            logging.info(f"🔒 Enviando mensaje PRIVADO ({len(message)} chars) a {hex(destination_id)}")
            interface.sendText(message, destinationId=destination_id)
            logging.info(f"✅ Mensaje privado enviado")
            publish_mqtt_msg("mesh/sent_private", {
                "destination": hex(destination_id), "message": message,
                "length": len(message), "timestamp": datetime.now().isoformat()
            })
            return
        
        logging.info(f"🔒📦 Mensaje privado largo ({len(message)} chars) a {hex(destination_id)} - dividiendo")
        chars_per_fragment = MAX_CHARS_PER_MESSAGE - 10
        fragments = [message[i:i + chars_per_fragment] for i in range(0, len(message), chars_per_fragment)]
        
        if len(fragments) > MAX_FRAGMENTS:
            fragments = fragments[:MAX_FRAGMENTS]
            last = fragments[-1][:chars_per_fragment - 3]
            last_space = last.rfind(' ')
            if last_space > len(last) * 0.8:
                last = last[:last_space]
            fragments[-1] = last + "..."
        
        total = len(fragments)
        for i, frag in enumerate(fragments, 1):
            interface.sendText(f"[{i}/{total}] {frag}", destinationId=destination_id)
            logging.info(f"✅ Fragmento privado {i}/{total} enviado")
            if i < total:
                time.sleep(FRAGMENT_DELAY)
        
        logging.info(f"🎉 Todos los fragmentos privados enviados")
        publish_mqtt_msg("mesh/sent_private", {
            "destination": hex(destination_id), "message": message,
            "fragments": total, "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        logging.error(f"❌ Error enviando mensaje privado: {e}")

def send_public_message(interface, message):
    try:
        if len(message) <= MAX_CHARS_PER_MESSAGE:
            logging.info(f"📢 Enviando mensaje PÚBLICO ({len(message)} chars)")
            interface.sendText(message, channelIndex=0)
            logging.info(f"✅ Mensaje público enviado")
            return
        
        chars_per_fragment = MAX_CHARS_PER_MESSAGE - 10
        fragments = [message[i:i + chars_per_fragment] for i in range(0, len(message), chars_per_fragment)]
        if len(fragments) > MAX_FRAGMENTS:
            fragments = fragments[:MAX_FRAGMENTS]
        
        for i, frag in enumerate(fragments, 1):
            interface.sendText(f"[{i}/{len(fragments)}] {frag}", channelIndex=0)
            if i < len(fragments):
                time.sleep(FRAGMENT_DELAY)
        logging.info(f"🎉 Mensaje público enviado")
    except Exception as e:
        logging.error(f"❌ Error enviando mensaje público: {e}")

# ==================== ROUTER DE MENSAJES ====================

def process_mesh_message(packet, interface):
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
        
        publish_mqtt_msg("mesh/received", {
            "from_id": from_id, "from_num": from_num, "message": text,
            "is_private": is_direct_to_me, "timestamp": datetime.now().isoformat()
        })
        
        # Helper para enviar respuestas
        send_fn = lambda node_num, msg: send_private_message(interface, node_num, msg)
        
        # ==================== ROUTING ====================
        
        # Imágenes (nuevo)
        if text.startswith('IMG_START|') or text.startswith('IMG|') or text.startswith('IMG_END|') or text.startswith('IMG_RESULT_ACK|'):
            image.handle(text, from_id, from_num, send_fn, publish_mqtt_msg)
            return
        
        # Siembra
        if text.startswith('SIEMBRA|'):
            logging.info(f"🌱 Mensaje SIEMBRA detectado")
            siembra.handle(text, from_id, from_num, send_fn, publish_mqtt_msg, agro_sheets_client)
            return
        
        # Registro visitantes
        if text.startswith('REGISTRO|') and is_direct_to_me:
            logging.info(f"📋 Mensaje REGISTRO detectado")
            registro.handle_registro(text, from_id, from_num, send_fn, publish_mqtt_msg)
            return
        
        # Salida visitantes
        if text.startswith('SALIDA|') and is_direct_to_me:
            logging.info(f"🚪 Mensaje SALIDA detectado")
            registro.handle_salida(text, from_id, from_num, send_fn, publish_mqtt_msg)
            return
        
        # Bot Familia
        if text.lower().startswith('@familia'):
            logging.info(f"👨‍👩‍👧 Activando bot familia")
            telegram_bridge.handle_familia(text, from_id, from_num, send_fn, publish_mqtt_msg, contactos_db)
            return
        
        # Claude AI
        if text.lower().startswith('@claude'):
            claude_ai.handle(text, from_id, from_num, send_fn, publish_mqtt_msg)
            return
        
        # Emergencias
        emergency_keywords = ['emergencia', 'ayuda', 'sos', 'peligro', 'urgente']
        if any(kw in text.lower() for kw in emergency_keywords):
            logging.warning(f"🚨 EMERGENCIA detectada de {from_id}")
            location = None
            if 'position' in packet:
                pos = packet['position']
                lat, lon = pos.get('latitude', 0), pos.get('longitude', 0)
                if lat and lon:
                    location = f"https://www.google.com/maps?q={lat},{lon}"
            
            publish_mqtt_msg("emergency/alert", {
                "from_id": from_id, "message": text,
                "location": location, "timestamp": datetime.now().isoformat()
            })
            telegram_bridge.send_telegram_alert(
                f"Usuario: {from_id}\nMensaje: {text}\nHora: {datetime.now()}", location
            )
            send_public_message(interface, "🚨 Alerta de emergencia enviada a coordinadores.")
            return
        
        # Guardar como contexto para posible imagen que viene despues
        image.store_context(from_num, text)
        logging.info(f"💬 Mensaje normal procesado (contexto guardado)")
        
    except Exception as e:
        logging.error(f"❌ Error procesando mensaje: {e}")
        import traceback
        logging.error(traceback.format_exc())

# ==================== MAIN ====================

def main():
    global interface_global
    
    logging.info("🚀 Iniciando Mesh Gateway v5.0 - Modular")
    
    # Inicializar todo
    load_contactos()
    init_mqtt()
    init_sheets()
    init_handlers()
    
    # Conectar LoRa
    try:
        logging.info("Conectando a LoRa...")
        interface_global = meshtastic.serial_interface.SerialInterface()
        logging.info("✓ Conectado a LoRa")
    except Exception as e:
        logging.error(f"Error conectando LoRa: {e}")
        exit(1)
    
    # Telegram polling
    def get_last_sender():
        return last_sender_id
    def set_last_sender(val):
        global last_sender_id
        last_sender_id = val
    
    telegram_thread = Thread(
        target=telegram_bridge.start_polling,
        args=(
            lambda num, msg: send_private_message(interface_global, num, msg),
            lambda msg: send_public_message(interface_global, msg),
            get_last_sender, set_last_sender, contactos_db
        ),
        daemon=True
    )
    telegram_thread.start()
    logging.info("✓ Telegram polling iniciado")
    
    # Suscribir a mensajes mesh
    def on_message(packet, interface=interface_global):
        try:
            if packet and 'decoded' in packet:
                process_mesh_message(packet, interface)
        except Exception as e:
            logging.error(f"Error: {e}")
    
    pub.subscribe(on_message, 'meshtastic.receive.text')
    logging.info("✓ Gateway activo")
    logging.info("📋 Comandos: SIEMBRA| | @familia [contacto]: [msg] | @claude | /send | /broadcast | /reply")
    logging.info("📸 Nuevo: IMG_START|, IMG|, IMG_END| para imágenes")
    
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

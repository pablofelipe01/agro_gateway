#!/usr/bin/env python3
"""
Mesh Gateway v5.0 - Multi-Mesh Bridge
Conecta múltiples redes mesh vía Internet usando MQTT
"""

import os
import sys
import time
import json
import logging
from datetime import datetime
import meshtastic
import meshtastic.serial_interface
from anthropic import Anthropic
from pubsub import pub
import gspread
from google.oauth2.service_account import Credentials
import paho.mqtt.client as mqtt
import requests

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/mesh_gateway.log'),
        logging.StreamHandler()
    ]
)

# =====================================================
# CONFIGURACIÓN GLOBAL
# =====================================================

# Variables de entorno
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_IDS = os.getenv('TELEGRAM_CHAT_IDS', '').split(',')

# Archivos de configuración
CONTACTOS_FILE = "/home/recomputer/mesh_gateway/config/contactos.json"
BRIDGE_CONFIG_FILE = "/home/recomputer/mesh_gateway/config/bridge_config.json"
AGRO_CREDENTIALS_FILE = '/home/recomputer/agro_gateway/credentials.json'

# Variables globales
mesh_interface = None
anthropic_client = None
agro_sheets_client = None
bridge_config = None
mqtt_client = None

# =====================================================
# FUNCIONES DE INICIALIZACIÓN
# =====================================================

def load_bridge_config():
    """Carga configuración de Multi-Mesh Bridge"""
    global bridge_config
    try:
        with open(BRIDGE_CONFIG_FILE, 'r') as f:
            bridge_config = json.load(f)
        logging.info(f"✅ Bridge config cargada: Gateway '{bridge_config['gateway_id']}' en '{bridge_config['location']}'")
        logging.info(f"🌐 MQTT Broker: {bridge_config['mqtt']['broker']}:{bridge_config['mqtt']['port']}")
        return True
    except FileNotFoundError:
        logging.warning("⚠️ Archivo bridge_config.json no encontrado - Bridge deshabilitado")
        return False
    except Exception as e:
        logging.error(f"❌ Error cargando bridge config: {e}")
        return False

def init_mqtt():
    """Inicializa cliente MQTT para Multi-Mesh Bridge"""
    global mqtt_client
    
    if not bridge_config:
        return False
    
    try:
        mqtt_client = mqtt.Client(client_id=f"mesh_{bridge_config['gateway_id']}")
        
        # Callbacks
        mqtt_client.on_connect = on_mqtt_connect
        mqtt_client.on_message = on_mqtt_message
        mqtt_client.on_disconnect = on_mqtt_disconnect
        
        # Conectar
        broker = bridge_config['mqtt']['broker']
        port = bridge_config['mqtt']['port']
        keepalive = bridge_config['mqtt'].get('keepalive', 60)
        
        mqtt_client.connect(broker, port, keepalive)
        mqtt_client.loop_start()
        
        logging.info(f"✅ MQTT conectado a {broker}:{port}")
        return True
        
    except Exception as e:
        logging.error(f"❌ Error conectando MQTT: {e}")
        return False

def on_mqtt_connect(client, userdata, flags, rc):
    """Callback cuando MQTT se conecta"""
    if rc == 0:
        logging.info("✅ MQTT conexión exitosa")
        
        # Suscribirse al topic de este gateway
        topic = f"{bridge_config['mqtt']['topic_prefix']}/{bridge_config['gateway_id']}"
        client.subscribe(topic)
        logging.info(f"📡 Suscrito a topic: {topic}")
    else:
        logging.error(f"❌ MQTT conexión falló con código: {rc}")

def on_mqtt_disconnect(client, userdata, rc):
    """Callback cuando MQTT se desconecta"""
    logging.warning(f"⚠️ MQTT desconectado (código: {rc})")
    if rc != 0:
        logging.info("🔄 Intentando reconectar...")

def on_mqtt_message(client, userdata, msg):
    """Callback cuando llega mensaje MQTT de otro gateway"""
    try:
        payload = json.loads(msg.payload.decode())
        
        logging.info(f"📥 Mensaje MQTT recibido de '{payload.get('from_gateway')}'")
        logging.info(f"   Destino: {payload.get('to_node')} | Contenido: {payload.get('message')[:50]}...")
        
        # Reenviar a mesh local
        target_node = payload.get('to_node')
        message = payload.get('message')
        from_gateway = payload.get('from_gateway')
        
        if target_node and message:
            # Agregar prefijo para identificar origen
            bridged_message = f"[{from_gateway}] {message}"
            send_mesh_message(bridged_message, target_node)
            logging.info(f"✅ Mensaje reenviado a mesh local: {target_node}")
        
    except Exception as e:
        logging.error(f"❌ Error procesando mensaje MQTT: {e}")

def load_contactos():
    """Carga archivo de contactos"""
    try:
        with open(CONTACTOS_FILE, 'r', encoding='utf-8') as f:
            contactos = json.load(f)
        logging.info(f"✓ Contactos cargados: {len(contactos.get('usuarios', []))} usuarios")
        return contactos
    except FileNotFoundError:
        logging.warning(f"⚠️ Archivo de contactos no encontrado: {CONTACTOS_FILE}")
        return {"usuarios": []}
    except Exception as e:
        logging.error(f"❌ Error cargando contactos: {e}")
        return {"usuarios": []}

def init_anthropic():
    """Inicializa cliente de Claude"""
    global anthropic_client
    
    if not ANTHROPIC_API_KEY:
        logging.error("❌ ANTHROPIC_API_KEY no configurada")
        return False
    
    try:
        anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
        logging.info("✓ Anthropic client inicializado")
        return True
    except Exception as e:
        logging.error(f"❌ Error inicializando Anthropic: {e}")
        return False

def init_agro_sheets():
    """Inicializa cliente de Google Sheets AGRO"""
    global agro_sheets_client
    
    try:
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_file(AGRO_CREDENTIALS_FILE, scopes=scopes)
        agro_sheets_client = gspread.authorize(creds)
        
        # Test conexión
        sheet = agro_sheets_client.open_by_key('1O45DA8vVGApEPV8pBSZ0AojYzrcSe-ixN7noKPR3Ciw')
        
        logging.info("✅ Conectado a Google Sheets AGRO")
        logging.info("✓ Google Sheets AGRO listo")
        return True
        
    except Exception as e:
        logging.error(f"❌ Error conectando a Google Sheets AGRO: {e}")
        logging.warning("⚠️ Google Sheets AGRO no disponible")
        return False

# =====================================================
# FUNCIONES MULTI-MESH BRIDGE
# =====================================================

def is_remote_node(node_id):
    """Verifica si un nodo pertenece a un gateway remoto"""
    if not bridge_config:
        return False
    
    for gw_id, gw_data in bridge_config.get('remote_gateways', {}).items():
        if node_id in gw_data.get('nodes', []):
            return gw_id
    
    return False

def publish_to_bridge(target_gateway, target_node, message, from_node):
    """Publica mensaje a otro gateway vía MQTT"""
    if not mqtt_client or not bridge_config:
        logging.warning("⚠️ MQTT no disponible, no se puede hacer bridge")
        return False
    
    try:
        topic = f"{bridge_config['mqtt']['topic_prefix']}/{target_gateway}"
        
        payload = {
            "from_gateway": bridge_config['gateway_id'],
            "from_node": from_node,
            "to_node": target_node,
            "message": message,
            "timestamp": datetime.now().isoformat()
        }
        
        mqtt_client.publish(topic, json.dumps(payload))
        logging.info(f"📤 Mensaje publicado a MQTT → {target_gateway}")
        logging.info(f"   Topic: {topic}")
        logging.info(f"   Destino: {target_node}")
        
        return True
        
    except Exception as e:
        logging.error(f"❌ Error publicando a bridge: {e}")
        return False

# =====================================================
# FUNCIONES MESH
# =====================================================

def send_mesh_message(message, destination_id=None, channel=0):
    """Envía mensaje a la mesh"""
    if not mesh_interface:
        logging.error("❌ Mesh interface no disponible")
        return False
    
    try:
        if destination_id:
            # Mensaje privado
            mesh_interface.sendText(message, destinationId=destination_id, channelIndex=channel)
            logging.info(f"🔒 Enviando mensaje PRIVADO a {destination_id}")
        else:
            # Mensaje público
            mesh_interface.sendText(message, channelIndex=channel)
            logging.info(f"📢 Enviando mensaje PÚBLICO")
        
        return True
        
    except Exception as e:
        logging.error(f"❌ Error enviando mensaje mesh: {e}")
        return False

def process_mesh_message(packet):
    """Procesa mensaje entrante de la mesh"""
    try:
        # Extraer datos
        decoded = packet.get('decoded', {})
        from_id = packet.get('fromId', 'unknown')
        message = decoded.get('text', '')
        
        if not message:
            return
        
        logging.info(f"📨 Mensaje de {from_id}: {message[:50]}...")
        
        # PRIORIDAD 1: Detectar si es mensaje para otro gateway (bridge)
        if message.startswith('@'):
            parts = message.split(' ', 1)
            if len(parts) >= 2:
                target_alias = parts[0][1:]  # Quitar @
                msg_content = parts[1]
                
                # Verificar si el alias corresponde a nodo remoto
                remote_gw = is_remote_node(f"!{target_alias}")
                if remote_gw:
                    logging.info(f"🌉 Mensaje para gateway remoto: {remote_gw}")
                    publish_to_bridge(remote_gw, f"!{target_alias}", msg_content, from_id)
                    return
        
        # PRIORIDAD 2: SIEMBRA (registro agrícola)
        if message.startswith('SIEMBRA|'):
            handle_siembra_message(message, from_id)
            return
        
        # PRIORIDAD 3: @claude (consulta IA)
        if '@claude' in message.lower():
            query = message.lower().replace('@claude', '').strip()
            handle_claude_query(query, from_id)
            return
        
        # PRIORIDAD 4: @familia (mensajería familiar)
        if message.lower().startswith('@familia'):
            handle_familia_message(message, from_id)
            return
        
        # Mensaje normal (no hacer nada especial)
        logging.info("💬 Mensaje normal procesado")
        
    except Exception as e:
        logging.error(f"❌ Error procesando mensaje mesh: {e}")

def handle_claude_query(query, from_id):
    """Maneja consulta a Claude"""
    if not anthropic_client:
        send_mesh_message("❌ Claude no disponible", from_id)
        return
    
    try:
        logging.info(f"🤖 Consulta Claude: {query[:50]}...")
        
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": query}]
        )
        
        answer = response.content[0].text
        
        # Fragmentar si es necesario
        if len(answer) > 200:
            fragments = [answer[i:i+200] for i in range(0, len(answer), 200)]
            for i, fragment in enumerate(fragments[:5]):
                send_mesh_message(f"[{i+1}/{len(fragments)}] {fragment}", from_id)
                time.sleep(5)
        else:
            send_mesh_message(answer, from_id)
        
        logging.info("✅ Respuesta Claude enviada")
        
    except Exception as e:
        logging.error(f"❌ Error en Claude: {e}")
        send_mesh_message("❌ Error consultando IA", from_id)

def handle_siembra_message(message, from_id):
    """Maneja mensaje de registro de siembra"""
    if not agro_sheets_client:
        send_mesh_message("❌ Sistema AGRO no disponible", from_id)
        return
    
    try:
        logging.info("🌱 Procesando registro de siembra")
        
        # Parse: SIEMBRA|fecha|hora|cultivo|variedad|lote|sector|hectareas|gps|notas
        parts = message.split('|')
        if len(parts) < 10:
            send_mesh_message("❌ Formato inválido", from_id)
            return
        
        fecha = parts[1]
        hora = parts[2]
        cultivo = parts[3]
        variedad = parts[4]
        lote = parts[5]
        sector = parts[6]
        hectareas = parts[7]
        gps = parts[8]
        notas = parts[9]
        
        # Generar ID único
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        siembra_id = f"ID-{timestamp}-{from_id}"
        
        # Guardar en Google Sheets
        sheet = agro_sheets_client.open_by_key('1O45DA8vVGApEPV8pBSZ0AojYzrcSe-ixN7noKPR3Ciw')
        worksheet = sheet.worksheet('Sheet1')
        
        # Separar GPS
        gps_parts = gps.split(',')
        gps_lat = gps_parts[0] if len(gps_parts) > 0 else 'sin-gps'
        gps_lon = gps_parts[1] if len(gps_parts) > 1 else 'sin-gps'
        
        row = [
            siembra_id,
            f"{fecha} {hora}",
            from_id,
            cultivo,
            variedad,
            lote,
            sector,
            hectareas,
            gps_lat,
            gps_lon,
            notas
        ]
        
        worksheet.append_row(row)
        
        logging.info(f"✅ Siembra guardada: {siembra_id}")
        
        # Confirmar al usuario
        send_mesh_message(f"SIEMBRA_OK|{siembra_id}|Registrado exitosamente", from_id)
        
    except Exception as e:
        logging.error(f"❌ Error guardando siembra: {e}")
        send_mesh_message("❌ Error guardando registro", from_id)

def handle_familia_message(message, from_id):
    """Maneja mensajes @familia (envía a Telegram)"""
    if not TELEGRAM_BOT_TOKEN:
        return
    
    try:
        content = message.replace('@familia', '').strip()
        
        for chat_id in TELEGRAM_CHAT_IDS:
            if chat_id:
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                data = {
                    "chat_id": chat_id,
                    "text": f"📨 Mensaje de {from_id}:\n\n{content}"
                }
                requests.post(url, json=data)
        
        logging.info("✅ Mensaje enviado a Telegram")
        
    except Exception as e:
        logging.error(f"❌ Error enviando a Telegram: {e}")

# =====================================================
# MAIN
# =====================================================

def main():
    """Función principal"""
    logging.info("=" * 60)
    logging.info("🚀 Iniciando Mesh Gateway v5.0 - Multi-Mesh Bridge")
    logging.info("=" * 60)
    
    # Inicializar componentes
    contactos = load_contactos()
    bridge_enabled = load_bridge_config()
    
    if not init_anthropic():
        logging.warning("⚠️ Claude no disponible")
    
    if not init_agro_sheets():
        logging.warning("⚠️ Google Sheets AGRO no disponible")
    
    if bridge_enabled and not init_mqtt():
        logging.warning("⚠️ Multi-Mesh Bridge no disponible")
    
    # Conectar a LoRa
    logging.info("Conectando a LoRa...")
    global mesh_interface
    
    try:
        mesh_interface = meshtastic.serial_interface.SerialInterface()
        logging.info("✓ Conectado a LoRa")
        
        # Registrar listener
        pub.subscribe(process_mesh_message, "meshtastic.receive.text")
        logging.info("✓ Listener registrado")
        
    except Exception as e:
        logging.error(f"❌ Error conectando a LoRa: {e}")
        sys.exit(1)
    
    logging.info("✓ Gateway activo")
    logging.info(f"📋 Funciones: @claude | @familia | SIEMBRA| | Multi-Mesh Bridge: {bridge_enabled}")
    
    # Loop infinito
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("\n👋 Gateway detenido por usuario")
        if mqtt_client:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
    except Exception as e:
        logging.error(f"❌ Error fatal: {e}")
    finally:
        if mesh_interface:
            mesh_interface.close()

if __name__ == "__main__":
    main()

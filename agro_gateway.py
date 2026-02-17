#!/usr/bin/env python3
"""
Agro Gateway - Procesa registros de siembra vía Meshtastic y los guarda en Google Sheets
"""

import time
import sys
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from meshtastic.serial_interface import SerialInterface
from pubsub import pub

# Configuración
CREDENTIALS_FILE = '/home/recomputer/agro_gateway/credentials.json'
SPREADSHEET_ID = '1O45DA8vVGApEPV8pBSZ0AojYzrcSe-ixN7noKPR3Ciw'
SHEET_NAME = 'Sheet1'  # Nombre de la pestaña

# Cliente Meshtastic
interface = None

# Cliente Google Sheets
def init_sheets():
    """Inicializa conexión con Google Sheets"""
    try:
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
        print(f"✅ Conectado a Google Sheets: {SPREADSHEET_ID}")
        return sheet
    except Exception as e:
        print(f"❌ Error conectando a Google Sheets: {e}")
        sys.exit(1)

def generate_id(node_id):
    """Genera ID único para el registro"""
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    return f"ID-{timestamp}-{node_id}"

def parse_siembra_message(message):
    """
    Parsea mensaje de siembra
    Formato: SIEMBRA|fecha|hora|cultivo|variedad|lote|sector|hectareas|gps|notas
    """
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
        print(f"❌ Error parseando mensaje: {e}")
        return None

def save_to_sheets(sheet, siembra_id, node_id, data):
    """Guarda registro en Google Sheets"""
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
        
        sheet.append_row(row)
        print(f"✅ Registro guardado en Sheets: {siembra_id}")
        return True
    except Exception as e:
        print(f"❌ Error guardando en Sheets: {e}")
        return False

def send_mesh_response(node_id, siembra_id, success=True):
    """Envía respuesta mesh al dispositivo"""
    try:
        if success:
            message = f"SIEMBRA_OK|{siembra_id}|Registrado en sistema"
        else:
            message = f"SIEMBRA_ERROR|{siembra_id}|Error al guardar"
        
        # Enviar al nodo específico
        interface.sendText(message, destinationId=node_id)
        print(f"📡 Respuesta enviada a nodo {node_id}: {message}")
    except Exception as e:
        print(f"❌ Error enviando respuesta mesh: {e}")

def on_receive(packet, interface_instance):
    """Callback cuando se recibe un mensaje mesh"""
    try:
        # Verificar que sea mensaje de texto
        if 'decoded' not in packet:
            return
        
        decoded = packet['decoded']
        if decoded.get('portnum') != 'TEXT_MESSAGE_APP':
            return
        
        # Obtener texto y remitente
        message = decoded.get('text', '')
        from_node = packet.get('from')
        
        print(f"📨 Mensaje recibido de nodo {from_node}: {message[:50]}...")
        
        # Verificar si es mensaje de siembra
        if not message.startswith('SIEMBRA|'):
            return
        
        print(f"🌱 Procesando registro de siembra...")
        
        # Parsear mensaje
        data = parse_siembra_message(message)
        if not data:
            print("❌ Formato de mensaje inválido")
            return
        
        # Generar ID
        siembra_id = generate_id(from_node)
        
        # Guardar en Google Sheets
        success = save_to_sheets(sheets_client, siembra_id, from_node, data)
        
        # Enviar respuesta
        send_mesh_response(from_node, siembra_id, success)
        
    except Exception as e:
        print(f"❌ Error procesando mensaje: {e}")

def main():
    """Función principal"""
    global interface, sheets_client
    
    print("=" * 60)
    print("🌾 AGRO GATEWAY - Inverse Neural Lab")
    print("=" * 60)
    
    # Inicializar Google Sheets
    sheets_client = init_sheets()
    
    # Conectar a Meshtastic
    print("📡 Conectando a dispositivo Meshtastic...")
    try:
        interface = SerialInterface()
        print("✅ Conectado a Meshtastic")
    except Exception as e:
        print(f"❌ Error conectando a Meshtastic: {e}")
        print("💡 Asegúrate de que el dispositivo esté conectado al reComputer")
        sys.exit(1)
    
    # Suscribirse a mensajes
    pub.subscribe(on_receive, 'meshtastic.receive')
    
    print("=" * 60)
    print("✅ Gateway activo - Esperando mensajes de siembra...")
    print("=" * 60)
    
    # Mantener script corriendo
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 Deteniendo gateway...")
        interface.close()
        print("✅ Gateway detenido")

if __name__ == '__main__':
    main()

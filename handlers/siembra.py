"""Handler de siembra - Google Sheets"""
import logging
from datetime import datetime

def parse_siembra_message(message):
    try:
        parts = message.split('|')
        if len(parts) != 10 or parts[0] != 'SIEMBRA':
            return None
        
        data = {
            'fecha': parts[1], 'hora': parts[2], 'cultivo': parts[3],
            'variedad': parts[4], 'lote': parts[5], 'sector': parts[6],
            'hectareas': parts[7], 'gps': parts[8], 'notas': parts[9]
        }
        
        if data['gps'] != 'sin-gps' and ',' in data['gps']:
            gps_parts = data['gps'].split(',')
            data['gps_lat'] = gps_parts[0]
            data['gps_lon'] = gps_parts[1]
        else:
            data['gps_lat'] = ''
            data['gps_lon'] = ''
        
        if data['notas'] == 'sin-notas':
            data['notas'] = ''
        
        return data
    except Exception as e:
        logging.error(f"❌ Error parseando SIEMBRA: {e}")
        return None

def generate_siembra_id(node_id):
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    return f"ID-{timestamp}-{node_id}"

def handle(text, from_id, from_num, send_fn, publish_mqtt, sheets_client):
    try:
        logging.info(f"🌱 Procesando registro de siembra de {from_id}")
        
        data = parse_siembra_message(text)
        if not data:
            send_fn(from_num, "❌ Error: Formato de siembra inválido")
            return
        
        siembra_id = generate_siembra_id(from_id)
        
        if not sheets_client:
            send_fn(from_num, f"SIEMBRA_ERROR|{siembra_id}|Google Sheets no disponible")
            return
        
        timestamp = f"{data['fecha']} {data['hora']}"
        row = [
            siembra_id, timestamp, str(from_id), data['cultivo'],
            data['variedad'], data['lote'], data['sector'],
            data['hectareas'], data['gps_lat'], data['gps_lon'], data['notas']
        ]
        
        sheets_client.append_row(row)
        logging.info(f"✅ Siembra guardada: {siembra_id}")
        
        publish_mqtt("agro/siembra_saved", {
            "id": siembra_id, "node": from_id,
            "cultivo": data['cultivo'], "timestamp": timestamp
        })
        
        send_fn(from_num, f"SIEMBRA_OK|{siembra_id}|Registrado exitosamente")
    except Exception as e:
        logging.error(f"❌ Error en handle_siembra: {e}")
        import traceback
        logging.error(traceback.format_exc())
        send_fn(from_num, f"SIEMBRA_ERROR|Error al guardar")

"""Handler de registro de visitantes - Airtable"""
import os
import logging
import requests
import urllib.parse
from datetime import datetime

AIRTABLE_API_TOKEN = os.getenv('AIRTABLE_API_TOKEN', '')
AIRTABLE_BASE_ID = 'apptwIqTras1uPNOc'
AIRTABLE_TABLE_NAME = 'Registro Visitantes'

pending_visits = {}

def handle_registro(text, from_id, from_num, send_fn, publish_mqtt):
    try:
        parts = text.split('|')
        if len(parts) < 7:
            logging.error(f"❌ Formato REGISTRO inválido: {text}")
            send_fn(from_num, "REGISTRO_ERROR|formato_invalido")
            return
        
        status = parts[1]
        nombre = parts[2]
        motivo = parts[3]
        area = parts[4]
        supervisor = parts[5]
        comentario = parts[6] if len(parts) > 6 else ""
        
        logging.info(f"📋 Registrando visitante: {nombre} - Estado: {status}")
        
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
        
        if status.upper() == "APROBADO":
            fields["Hora Entrada"] = datetime.now().isoformat()
        
        table_encoded = urllib.parse.quote(AIRTABLE_TABLE_NAME)
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_API_TOKEN}",
            "Content-Type": "application/json"
        }
        
        response = requests.post(url, headers=headers, json={"fields": fields}, timeout=15)
        
        if response.status_code in [200, 201]:
            record = response.json()
            record_id = record.get('id', '')
            
            if status.upper() == "APROBADO":
                pending_visits[nombre.lower()] = record_id
            
            logging.info(f"✅ Registro guardado en Airtable: {record_id}")
            send_fn(from_num, f"REGISTRO_OK|{nombre}")
            publish_mqtt("airtable/registro", {
                "record_id": record_id, "nombre": nombre,
                "status": status, "timestamp": datetime.now().isoformat()
            })
        else:
            logging.error(f"❌ Error Airtable: {response.status_code} - {response.text}")
            send_fn(from_num, f"REGISTRO_ERROR|{nombre}|{response.status_code}")
    except Exception as e:
        logging.error(f"❌ Error en handle_registro: {e}")
        import traceback
        logging.error(traceback.format_exc())
        send_fn(from_num, f"REGISTRO_ERROR|{str(e)}")


def handle_salida(text, from_id, from_num, send_fn, publish_mqtt):
    try:
        parts = text.split('|')
        if len(parts) < 2:
            send_fn(from_num, "SALIDA_ERROR|formato_invalido")
            return
        
        nombre = parts[1]
        logging.info(f"🚪 Registrando salida de: {nombre}")
        
        record_id = pending_visits.get(nombre.lower())
        if not record_id:
            logging.warning(f"⚠️ No se encontró visita activa para: {nombre}")
            send_fn(from_num, f"SALIDA_ERROR|{nombre}|No se encontro visita activa")
            return
        
        table_encoded = urllib.parse.quote(AIRTABLE_TABLE_NAME)
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_encoded}/{record_id}"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_API_TOKEN}",
            "Content-Type": "application/json"
        }
        
        response = requests.patch(url, headers=headers, json={"fields": {"Hora Salida": datetime.now().isoformat()}}, timeout=15)
        
        if response.status_code == 200:
            del pending_visits[nombre.lower()]
            logging.info(f"✅ Salida registrada para: {nombre}")
            send_fn(from_num, f"SALIDA_OK|{nombre}")
            publish_mqtt("airtable/salida", {
                "record_id": record_id, "nombre": nombre,
                "timestamp": datetime.now().isoformat()
            })
        else:
            send_fn(from_num, f"SALIDA_ERROR|{nombre}|{response.status_code}")
    except Exception as e:
        logging.error(f"❌ Error en handle_salida: {e}")
        import traceback
        logging.error(traceback.format_exc())
        send_fn(from_num, f"SALIDA_ERROR|{str(e)}")

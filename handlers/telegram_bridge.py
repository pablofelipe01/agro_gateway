"""Handler de Telegram - Familia + polling"""
import os
import time
import logging
import requests
import json
from datetime import datetime

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_IDS = os.getenv('TELEGRAM_CHAT_IDS', '').split(',')

familia_messages_tracking = {}

def send_telegram_message(text, chat_id=None):
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        target_ids = [chat_id] if chat_id else [cid.strip() for cid in TELEGRAM_CHAT_IDS if cid.strip()]
        for tid in target_ids:
            response = requests.post(url, json={"chat_id": tid, "text": text}, timeout=10)
            if response.status_code == 200:
                logging.info(f"✅ Telegram enviado a {tid}")
            else:
                logging.error(f"❌ Error Telegram: HTTP {response.status_code}")
    except Exception as e:
        logging.error(f"❌ Error enviando Telegram: {e}")

def send_telegram_alert(message, location=None):
    if not TELEGRAM_BOT_TOKEN:
        return
    alert_text = f"🚨 ALERTA EMERGENCIA\n\n{message}"
    if location:
        alert_text += f"\n\n📍 Ubicación: {location}"
    send_telegram_message(alert_text)

def handle_familia(text, from_id, from_num, send_fn, publish_mqtt, contactos_db):
    try:
        parts = text[8:].split(':', 1)
        if len(parts) < 2:
            send_fn(from_num, "❌ Formato: @familia [contacto]: [mensaje]\nEjemplo: @familia mamá: Hola!")
            return
        
        contact_alias = parts[0].strip()
        message = parts[1].strip()
        
        # Buscar contacto
        user = contactos_db.get('users', {}).get(from_id)
        if not user:
            send_fn(from_num, f"❌ Usuario no registrado.")
            return
        
        contact = None
        alias_lower = contact_alias.lower()
        for contact_key, contact_data in user.get('contacts', {}).items():
            if alias_lower in [a.lower() for a in contact_data.get('alias', [])]:
                contact = contact_data
                break
            if alias_lower == contact_key.lower():
                contact = contact_data
                break
        
        if not contact:
            send_fn(from_num, f"❌ Contacto '{contact_alias}' no encontrado.")
            return
        
        user_name = user.get('name', from_id)
        telegram_id = contact['telegram_id']
        contact_name = contact['name']
        
        familia_msg = f"💌 Mensaje de {user_name} (mesh):\n\n{message}\n\n⏰ {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\nPara responder: /reply [tu mensaje]"
        send_telegram_message(familia_msg, chat_id=telegram_id)
        
        familia_messages_tracking[telegram_id] = {
            "mesh_user": from_id, "mesh_num": from_num, "timestamp": time.time()
        }
        
        send_fn(from_num, f"✅ Mensaje enviado a {contact_name}")
        publish_mqtt("familia/sent", {
            "from_mesh": from_id, "to_contact": contact_name,
            "timestamp": datetime.now().isoformat()
        })
        logging.info(f"👨‍👩‍👧 Mensaje familia: {user_name} → {contact_name}")
    except Exception as e:
        logging.error(f"❌ Error en familia: {e}")
        import traceback
        logging.error(traceback.format_exc())

def process_telegram_message(update, send_mesh_private_fn, send_mesh_public_fn, last_sender_id, contactos_db):
    try:
        if 'message' not in update or 'text' not in update['message']:
            return last_sender_id
        
        text = update['message']['text']
        chat_id = str(update['message']['chat']['id'])
        
        if chat_id not in [cid.strip() for cid in TELEGRAM_CHAT_IDS if cid.strip()]:
            return last_sender_id
        
        logging.info(f"📥 Telegram: {text}")
        
        # /reply
        if text.startswith('/reply'):
            reply_text = text.replace('/reply', '').strip()
            if not reply_text:
                send_telegram_message("❌ Uso: /reply [tu mensaje]", chat_id=chat_id)
                return last_sender_id
            
            tracking = familia_messages_tracking.get(chat_id)
            if not tracking:
                send_telegram_message("❌ No hay mensaje reciente para responder.", chat_id=chat_id)
                return last_sender_id
            
            if time.time() - tracking['timestamp'] > 3600:
                send_telegram_message("❌ Mensaje muy antiguo.", chat_id=chat_id)
                return last_sender_id
            
            contact_name = "Familia"
            for user_data in contactos_db.get('users', {}).values():
                for contact in user_data.get('contacts', {}).values():
                    if contact.get('telegram_id') == chat_id:
                        contact_name = contact['name']
                        break
            
            send_mesh_private_fn(tracking['mesh_num'], f"{contact_name}: {reply_text}")
            send_telegram_message(f"✅ Respuesta enviada", chat_id=chat_id)
            return last_sender_id
        
        # /to
        if text.startswith('/to'):
            parts = text[3:].split(':', 1)
            if len(parts) < 2:
                send_telegram_message("❌ Formato: /to [contacto]: [mensaje]", chat_id=chat_id)
                return last_sender_id
            
            contact_alias = parts[0].strip().lower()
            message = parts[1].strip()
            
            telegram_user = contactos_db.get("telegram_users", {}).get(chat_id)
            if not telegram_user:
                send_telegram_message("❌ Usuario no registrado.", chat_id=chat_id)
                return last_sender_id
            
            mesh_contact = None
            for ck, cd in telegram_user.get("mesh_contacts", {}).items():
                if contact_alias in [a.lower() for a in cd.get("alias", [])]:
                    mesh_contact = cd
                    break
                if contact_alias == ck.lower():
                    mesh_contact = cd
                    break
            
            if not mesh_contact:
                send_telegram_message(f"❌ Contacto '{contact_alias}' no encontrado.", chat_id=chat_id)
                return last_sender_id
            
            sender_name = telegram_user.get('name', 'Telegram')
            send_mesh_private_fn(mesh_contact['mesh_num'], f"{sender_name}: {message}")
            
            familia_messages_tracking[chat_id] = {
                "mesh_user": mesh_contact['mesh_id'],
                "mesh_num": mesh_contact['mesh_num'],
                "timestamp": time.time()
            }
            
            send_telegram_message(f"✅ Mensaje enviado a {mesh_contact['name']}", chat_id=chat_id)
            return last_sender_id
        
        # /send
        if text.startswith('/send'):
            message = text.replace('/send', '').strip()
            if message:
                if last_sender_id:
                    send_mesh_private_fn(last_sender_id, f"TG: {message}")
                else:
                    send_mesh_public_fn(f"TG: {message}")
            return last_sender_id
        
        # /broadcast
        if text.startswith('/broadcast'):
            message = text.replace('/broadcast', '').strip()
            if message:
                send_mesh_public_fn(f"TG: {message}")
            return last_sender_id
        
        return last_sender_id
    except Exception as e:
        logging.error(f"❌ Error procesando Telegram: {e}")
        return last_sender_id

def start_polling(send_mesh_private_fn, send_mesh_public_fn, get_last_sender, set_last_sender, contactos_db):
    if not TELEGRAM_BOT_TOKEN:
        return
    
    logging.info("📱 Iniciando Telegram polling...")
    
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook", timeout=10)
        logging.info("🧹 Webhook limpiado (si existía)")
    except:
        pass
    
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            response = requests.get(url, params={"offset": offset, "timeout": 30}, timeout=35)
            
            if response.status_code == 200:
                for update in response.json().get('result', []):
                    last = process_telegram_message(
                        update, send_mesh_private_fn, send_mesh_public_fn,
                        get_last_sender(), contactos_db
                    )
                    if last:
                        set_last_sender(last)
                    offset = update['update_id'] + 1
        except Exception as e:
            logging.error(f"❌ Error Telegram polling: {e}")
            time.sleep(5)

"""Handler de consultas Claude AI"""
import logging
from datetime import datetime
from anthropic import Anthropic

anthropic_client = None

def init(api_key):
    global anthropic_client
    anthropic_client = Anthropic(api_key=api_key)

def clean_response(text):
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

def handle(text, from_id, from_num, send_fn, publish_mqtt):
    try:
        query = text[7:].strip()  # Remover "@claude"
        logging.info(f"🤖 Consulta Claude: {query}")
        
        publish_mqtt("claude/query", {
            "from_id": from_id, "query": query,
            "timestamp": datetime.now().isoformat()
        })
        
        if not anthropic_client:
            send_fn(from_num, "❌ Claude API no configurada")
            return
        
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            system="Eres un asistente educativo para zonas rurales de Colombia. "
                   "Responde de forma CONCISA, COMPLETA y DIRECTA. "
                   "CRÍTICO: Siempre termina tus respuestas con punto final.",
            messages=[{"role": "user", "content": query}]
        )
        
        raw_response = response.content[0].text
        cleaned = clean_response(raw_response)
        
        publish_mqtt("claude/response", {
            "query": query, "response": cleaned,
            "length": len(cleaned), "timestamp": datetime.now().isoformat()
        })
        
        send_fn(from_num, f"Claude: {cleaned}")
    except Exception as e:
        logging.error(f"❌ Error en claude_ai: {e}")
        send_fn(from_num, "Error al procesar consulta.")

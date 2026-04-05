#!/bin/bash
# Sirius Edu - Gateway Setup
# Probado en: Jetson Orin Nano (Ubuntu 20.04), Raspberry Pi 5 (Bookworm)

set -e

echo "=== Sirius Edu Gateway - Setup ==="

# 1. Dependencias del sistema
echo ">> Instalando dependencias del sistema..."
sudo apt-get update
sudo apt-get install -y python3-pip python3-venv

# 2. Entorno virtual
echo ">> Creando entorno virtual..."
python3 -m venv venv
source venv/bin/activate

# 3. Dependencias Python
echo ">> Instalando dependencias Python..."
pip install -r requirements.txt

# 4. Archivo .env
if [ ! -f .env ]; then
    echo ">> Creando .env (EDITAR con tus valores)..."
    cat > .env << 'ENVEOF'
# === OBLIGATORIAS ===
ANTHROPIC_API_KEY=sk-ant-...
SUPABASE_URL=https://tu-proyecto.supabase.co
SUPABASE_SERVICE_KEY=eyJ...
SCHOOL_ID=a0000000-0000-0000-0000-000000000001

# === OPCIONALES ===
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_IDS=
AIRTABLE_API_TOKEN=
ENVEOF
    echo ""
    echo "!! IMPORTANTE: Edita .env con tus credenciales antes de ejecutar !!"
    echo "   nano .env"
else
    echo ">> .env ya existe, no se sobreescribe"
fi

# 5. Permisos del puerto serial
echo ">> Agregando usuario al grupo dialout (acceso serial)..."
sudo usermod -a -G dialout $USER

echo ""
echo "=== Setup completo ==="
echo ""
echo "Para ejecutar:"
echo "  source venv/bin/activate"
echo "  source .env && export \$(grep -v '^#' .env | xargs)"
echo "  python3 mesh_gateway.py"
echo ""
echo "Para ejecutar como servicio (arranque automatico):"
echo "  sudo cp sirius-gateway.service /etc/systemd/system/"
echo "  sudo systemctl enable sirius-gateway"
echo "  sudo systemctl start sirius-gateway"

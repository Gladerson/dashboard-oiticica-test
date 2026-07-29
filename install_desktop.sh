#!/usr/bin/env bash
# ============================================================================
# install_desktop.sh
# Instala o ambiente de teste (controller + server) no Debian desktop.
# Rode com: bash install_desktop.sh
# ============================================================================
set -e

PROJ_DIR="$HOME/Projetos/dashboard_oiticica_test"
SRC_MODEL_DIR="$HOME/Projetos/dashboard_oiticica"

echo ">> 1/6 - Pacotes de sistema (apt)"
sudo apt update
sudo apt install -y \
    python3-venv python3-pip python3-dev build-essential \
    libgl1 libglib2.0-0 ffmpeg \
    libxml2-dev libxslt1-dev \
    libspatialindex-dev \
    git

echo ">> 2/6 - Criando estrutura do projeto em $PROJ_DIR"
mkdir -p "$PROJ_DIR"/{controller,server/static,server/history}

echo ">> 3/6 - Copiando modelo .glb e best.pt (se ainda não estiverem no projeto de teste)"
if [ -f "$SRC_MODEL_DIR/Processamento-1-Oiticica-textured_model.glb" ]; then
    cp -n "$SRC_MODEL_DIR/Processamento-1-Oiticica-textured_model.glb" "$PROJ_DIR/server/static/model.glb" || true
fi
if [ -f "$SRC_MODEL_DIR/best.pt" ]; then
    cp -n "$SRC_MODEL_DIR/best.pt" "$PROJ_DIR/controller/best.pt" || true
fi

echo ">> 4/6 - Ambiente virtual do controller"
if [ ! -f "$PROJ_DIR/controller/.env" ] && [ -f "$PROJ_DIR/controller/.env.example" ]; then
    cp "$PROJ_DIR/controller/.env.example" "$PROJ_DIR/controller/.env"
    echo ">> Criado controller/.env a partir do .env.example -- edite com suas credenciais reais!"
fi
python3 -m venv "$PROJ_DIR/controller/venv"
source "$PROJ_DIR/controller/venv/bin/activate"
pip install --upgrade pip
pip install -r "$PROJ_DIR/controller/requirements.txt"
deactivate

echo ">> 5/6 - Ambiente virtual do server"
python3 -m venv "$PROJ_DIR/server/venv"
source "$PROJ_DIR/server/venv/bin/activate"
pip install --upgrade pip
pip install -r "$PROJ_DIR/server/requirements.txt"
deactivate

echo ">> 6/6 - Pronto."
echo
echo "IMPORTANTE - antes de rodar, edite:"
echo "  1) $PROJ_DIR/controller/config.py  -> confirme IP/usuário/senha ONVIF e RTSP_URL"
echo "  2) $PROJ_DIR/server/glb_geo.py     -> preencha GEO_OFFSET (zona UTM + offset X,Y,Z"
echo "     do georreferenciamento do ODM/WebODM). Sem isso o modelo NÃO estará"
echo "     alinhado com coordenadas reais - ver instruções no topo do arquivo."
echo
echo "Para rodar (dois terminais):"
echo "  Terminal 1 (server):     cd $PROJ_DIR/server && source venv/bin/activate && python server.py"
echo "  Terminal 2 (controller): cd $PROJ_DIR/controller && source venv/bin/activate && python controller.py"
echo
echo "Dashboard: http://127.0.0.1:8001"

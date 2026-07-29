# ============================================================================
# config.py - Configuração do controller
#
# As credenciais NÃO ficam mais escritas aqui. Elas vêm do arquivo `.env`
# (que está no .gitignore e nunca é commitado). Veja `.env.example` para
# saber quais variáveis preencher, e crie seu próprio `.env` copiando esse
# exemplo:
#     cp .env.example .env
#     nano .env   # preencha os valores reais
#
# No Raspberry Pi, o único arquivo que muda entre máquinas é o `.env`
# (além, claro, de quem estiver hospedando o server).
# ============================================================================
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(_ENV_PATH)


def _require(var_name):
    value = os.getenv(var_name)
    if not value:
        sys.exit(
            f"ERRO: variável de ambiente '{var_name}' não definida.\n"
            f"Copie controller/.env.example para controller/.env e preencha os valores:\n"
            f"    cp .env.example .env\n"
            f"    nano .env"
        )
    return value


# --- Câmera ONVIF -----------------------------------------------------------
CAMERA_IP = _require("CAMERA_IP")
ONVIF_PORT = int(os.getenv("ONVIF_PORT", "80"))
ONVIF_USER = _require("ONVIF_USER")
ONVIF_PASSWORD = _require("ONVIF_PASSWORD")

# --- RTSP ---------------------------------------------------------------
RTSP_URL = _require("RTSP_URL")

# --- Servidor (dashboard) ---------------------------------------------------
SERVER_URL = os.getenv("SERVER_URL", "http://127.0.0.1:8001")

# --- Modelo YOLO de rachaduras -----------------------------------------
YOLO_MODEL_PATH = os.getenv("YOLO_MODEL_PATH", "best.pt")
YOLO_CONF_THRESHOLD = float(os.getenv("YOLO_CONF_THRESHOLD", "0.558"))
YOLO_INFER_EVERY_N_FRAMES = int(os.getenv("YOLO_INFER_EVERY_N_FRAMES", "5"))
DETECTION_COOLDOWN_SECONDS = float(os.getenv("DETECTION_COOLDOWN_SECONDS", "5"))

# --- Telemetria PTZ -----------------------------------------------------
PTZ_POLL_INTERVAL_SECONDS = float(os.getenv("PTZ_POLL_INTERVAL_SECONDS", "1.0"))

# --- API local do controller (recebe comandos do dashboard) ----------------
CONTROLLER_API_HOST = os.getenv("CONTROLLER_API_HOST", "0.0.0.0")
CONTROLLER_API_PORT = int(os.getenv("CONTROLLER_API_PORT", "8090"))

# --- Passo de movimentação por clique no dashboard --------------------------
PAN_STEP_DEG = float(os.getenv("PAN_STEP_DEG", "5.0"))
TILT_STEP_DEG = float(os.getenv("TILT_STEP_DEG", "5.0"))
ZOOM_STEP_PCT = float(os.getenv("ZOOM_STEP_PCT", "10.0"))

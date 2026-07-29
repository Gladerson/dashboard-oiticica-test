# ============================================================================
# config.py - Configuração do controller
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


def _bool(name, default="true"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "sim")


# --- Câmera ONVIF -----------------------------------------------------------
CAMERA_IP = _require("CAMERA_IP")
ONVIF_PORT = int(os.getenv("ONVIF_PORT", "80"))
ONVIF_USER = _require("ONVIF_USER")
ONVIF_PASSWORD = _require("ONVIF_PASSWORD")

# --- RTSP -------------------------------------------------------------------
RTSP_URL = _require("RTSP_URL")

# --- Servidor (dashboard) ---------------------------------------------------
SERVER_URL = os.getenv("SERVER_URL", "http://127.0.0.1:8001")

# --- Modelo YOLO de rachaduras ----------------------------------------------
YOLO_MODEL_PATH = os.getenv("YOLO_MODEL_PATH", "best.pt")
YOLO_CONF_THRESHOLD = float(os.getenv("YOLO_CONF_THRESHOLD", "0.558"))
YOLO_INFER_EVERY_N_FRAMES = int(os.getenv("YOLO_INFER_EVERY_N_FRAMES", "5"))
DETECTION_COOLDOWN_SECONDS = float(os.getenv("DETECTION_COOLDOWN_SECONDS", "5"))

# --- Telemetria PTZ ---------------------------------------------------------
# Parado: intervalo lento. Durante/logo após um movimento: intervalo rápido,
# para o cone no dashboard acompanhar a câmera em tempo real.
PTZ_POLL_INTERVAL_SECONDS = float(os.getenv("PTZ_POLL_INTERVAL_SECONDS", "1.0"))
PTZ_POLL_FAST_SECONDS = float(os.getenv("PTZ_POLL_FAST_SECONDS", "0.15"))
PTZ_FAST_WINDOW_SECONDS = float(os.getenv("PTZ_FAST_WINDOW_SECONDS", "4.0"))

# Duas conexões ONVIF separadas (uma para telemetria, outra para comandos):
# é o que impede a telemetria de "segurar" o comando do botão. Se a sua câmera
# reclamar de sessões simultâneas, coloque false no .env.
PTZ_SEPARATE_CONNECTIONS = _bool("PTZ_SEPARATE_CONNECTIONS", "true")

# Se o navegador travar/fechar com o botão pressionado, para sozinho:
# Frequência da thread que aplica o movimento na câmera.
# 0.05s = a câmera para em até ~50ms depois de você soltar o botão.
PTZ_MOTION_TICK_SECONDS = float(os.getenv("PTZ_MOTION_TICK_SECONDS", "0.05"))

# --- Curso mecânico real (usado quando o ONVIF reporta -1..1) ---------------
PAN_DEG_RANGE = float(os.getenv("PAN_DEG_RANGE", "180.0"))
TILT_DEG_RANGE = float(os.getenv("TILT_DEG_RANGE", "90.0"))

# --- API local do controller ------------------------------------------------
CONTROLLER_API_HOST = os.getenv("CONTROLLER_API_HOST", "0.0.0.0")
CONTROLLER_API_PORT = int(os.getenv("CONTROLLER_API_PORT", "8090"))

# --- Passo de movimentação (fallback, quando não há ContinuousMove) ---------
PAN_STEP_DEG = float(os.getenv("PAN_STEP_DEG", "5.0"))
TILT_STEP_DEG = float(os.getenv("TILT_STEP_DEG", "5.0"))
ZOOM_STEP_PCT = float(os.getenv("ZOOM_STEP_PCT", "10.0"))
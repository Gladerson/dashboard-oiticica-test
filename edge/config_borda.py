# ============================================================================
# config_borda.py - Configuracao do agente de borda (Raspberry Pi)
# ============================================================================
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_ENV = Path(__file__).parent / ".env"
load_dotenv(_ENV)


def _req(nome):
    v = os.getenv(nome)
    if not v:
        sys.exit(
            f"ERRO: variavel '{nome}' nao definida.\n"
            f"  cd ~/Projetos/dashboard_oiticica_test/edge\n"
            f"  cp .env.example .env && nano .env"
        )
    return v


def _bool(nome, padrao="true"):
    return os.getenv(nome, padrao).strip().lower() in ("1", "true", "yes", "sim")


BASE_DIR = Path(__file__).parent

# --- Identidade do dispositivo ---------------------------------------------
DEVICE_ID = os.getenv("DEVICE_ID", "oiticica-cam-01")

# --- Camera -----------------------------------------------------------------
CAMERA_IP = _req("CAMERA_IP")
ONVIF_PORT = int(os.getenv("ONVIF_PORT", "80"))
ONVIF_USER = _req("ONVIF_USER")
ONVIF_PASSWORD = _req("ONVIF_PASSWORD")
RTSP_URL = _req("RTSP_URL")

# --- Servidor (modo HTTP) ---------------------------------------------------
SERVER_URL = os.getenv("SERVER_URL", "http://192.168.0.177:8001").rstrip("/")

# --- Broker MQTT (modo MQTT / ThingsBoard) ----------------------------------
# O TOKEN NUNCA vem do servidor pela rede: ele mora aqui, no .env do Pi.
# O servidor so manda "use MQTT" ou "use HTTP".
MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOKEN = os.getenv("MQTT_TOKEN", "")
MQTT_TLS = _bool("MQTT_TLS", "false")
MQTT_TOPICO_FRAME = os.getenv("MQTT_TOPICO_FRAME", f"oiticica/{DEVICE_ID}/frame")
# Se o broker sumir por mais que isso, o agente volta sozinho para HTTP.
MQTT_FALLBACK_SEGUNDOS = float(os.getenv("MQTT_FALLBACK_SEGUNDOS", "30"))

TRANSPORTE_INICIAL = os.getenv("TRANSPORTE_INICIAL", "http").strip().lower()

# --- Modelo hibrido Hailo ---------------------------------------------------
HEF_PATH = os.getenv("HEF_PATH", str(BASE_DIR / "best_backbone.hef"))
HEAD_ONNX_PATH = os.getenv("HEAD_ONNX_PATH", str(BASE_DIR / "best_head.onnx"))
INPUT_SIZE = int(os.getenv("INPUT_SIZE", "640"))
THREADS_CPU = int(os.getenv("THREADS_CPU", "4"))
CLASS_NAMES = [s.strip() for s in os.getenv("CLASS_NAMES", "rachadura").split(",")]

# Mapa saida_HEF -> entrada_ONNX. Muda quando voce troca o tamanho do modelo!
_MAPA_PADRAO = {
    "yolo26_fissuras_backbone/conv39": "/model.10/cv2/act/Mul_output_0",
    "yolo26_fissuras_backbone/conv48": "/model.13/cv2/act/Mul_output_0",
    "yolo26_fissuras_backbone/conv57": "/model.16/cv2/act/Mul_output_0",
    "yolo26_fissuras_backbone/conv58": "/model.17/conv/Conv_output_0",
}
_mapa_env = os.getenv("MAPA_HEF_PARA_ONNX", "").strip()
MAPA_HEF_PARA_ONNX = json.loads(_mapa_env) if _mapa_env else _MAPA_PADRAO

# --- Inferencia (valores iniciais; o servidor pode ajustar em runtime) ------
# ATENCAO: este limiar e do modelo QUANTIZADO INT8, nao do .pt. Rode o
# calibrar_limiar_int8.py no notebook antes de confiar neste numero.
CONF_THRESHOLD = float(os.getenv("CONF_THRESHOLD", "0.15"))
IOU_THRESHOLD = float(os.getenv("IOU_THRESHOLD", "0.45"))
INFERIR_A_CADA_N_FRAMES = int(os.getenv("INFERIR_A_CADA_N_FRAMES", "5"))
COOLDOWN_DETECCAO_S = float(os.getenv("COOLDOWN_DETECCAO_S", "5"))

# --- Evidencia local ---------------------------------------------------------
# A deteccao em si NUNCA carrega imagem (so coordenadas/metadados, ver
# publicar_deteccao em agente_borda.py). O frame cru fica guardado aqui e so
# sobe se o operador pedir ("Abrir" no dashboard). So e gravado quando o
# servidor confirma que a deteccao virou um alerta NOVO (nao duplicata/
# rearme) -- e isso que impede a pasta de encher com evidencia de
# deteccoes repetidas da MESMA rachadura que o servidor ja teria descartado.
EVIDENCIA_JPEG_Q = int(os.getenv("EVIDENCIA_JPEG_Q", "85"))
EVIDENCIAS_DIR = Path(os.getenv("EVIDENCIAS_DIR", str(BASE_DIR / "evidencias")))
EVIDENCIAS_MAX_MB = float(os.getenv("EVIDENCIAS_MAX_MB", "2048"))

# --- Stream sob demanda -----------------------------------------------------
STREAM_FPS = float(os.getenv("STREAM_FPS", "4"))
STREAM_LARGURA = int(os.getenv("STREAM_LARGURA", "640"))
STREAM_JPEG_Q = int(os.getenv("STREAM_JPEG_Q", "60"))
STREAM_ANOTADO = _bool("STREAM_ANOTADO", "true")
# Guarda-chuva do lado do Pi: mesmo que o servidor suma, o stream morre.
STREAM_TTL_S = float(os.getenv("STREAM_TTL_S", "75"))

# --- Telemetria -------------------------------------------------------------
TELEMETRIA_INTERVALO_S = float(os.getenv("TELEMETRIA_INTERVALO_S", "1.0"))
TELEMETRIA_INTERVALO_RAPIDO_S = float(os.getenv("TELEMETRIA_INTERVALO_RAPIDO_S", "0.15"))
JANELA_RAPIDA_S = float(os.getenv("JANELA_RAPIDA_S", "4.0"))

# --- PTZ --------------------------------------------------------------------
PAN_DEG_RANGE = float(os.getenv("PAN_DEG_RANGE", "180.0"))
TILT_DEG_RANGE = float(os.getenv("TILT_DEG_RANGE", "90.0"))
PTZ_SEPARATE_CONNECTIONS = _bool("PTZ_SEPARATE_CONNECTIONS", "true")
PTZ_MOTION_TICK_S = float(os.getenv("PTZ_MOTION_TICK_S", "0.05"))
PAN_STEP_DEG = float(os.getenv("PAN_STEP_DEG", "5.0"))
TILT_STEP_DEG = float(os.getenv("TILT_STEP_DEG", "5.0"))
ZOOM_STEP_PCT = float(os.getenv("ZOOM_STEP_PCT", "10.0"))

# --- API local do agente ----------------------------------------------------
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8090"))

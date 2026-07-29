# ============================================================================
# controller.py - Controlador da câmera (roda no desktop; futuramente no Raspberry)
#
# Responsabilidades:
#   1. Conecta na câmera via ONVIF e move para o ponto zero (home).
#   2. Envia continuamente a posição PTZ atual para o server (dashboard).
#   3. Captura o RTSP, roda YOLO (best.pt) e, ao detectar rachadura,
#      avisa o server com a posição PTZ + imagem + máscara.
#   4. Expõe uma API local (FastAPI) para:
#        - receber comandos de movimentação vindos do dashboard (via server)
#        - servir o stream MJPEG cru para o dashboard poder exibir a imagem
# ============================================================================
import base64
import io
import threading
import time
from datetime import datetime, timezone

import cv2
import numpy as np
import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from ultralytics import YOLO

import config
from onvif_ptz import PTZController

# ----------------------------------------------------------------------------
# Estado compartilhado entre threads
# ----------------------------------------------------------------------------
class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        self.pan_deg = 0.0
        self.tilt_deg = 0.0
        self.zoom_pct = 0.0
        self.last_frame_jpeg = None
        self.last_detection_time = 0.0


state = SharedState()
ptz = PTZController(config.CAMERA_IP, config.ONVIF_PORT, config.ONVIF_USER, config.ONVIF_PASSWORD)

print(">> Conectado à câmera ONVIF. Faixas detectadas:")
print(f"   pan: [{ptz.pan_min}, {ptz.pan_max}] normalizado={ptz.pan_normalized}")
print(f"   tilt: [{ptz.tilt_min}, {ptz.tilt_max}] normalizado={ptz.tilt_normalized}")
print(f"   zoom: [{ptz.zoom_min}, {ptz.zoom_max}]")

print(">> Movendo para o ponto zero (home) das coordenadas ONVIF...")
ptz.go_home()
time.sleep(3)  # dá tempo do motor físico se mover antes de checar/começar a telemetria

_pan_chk, _tilt_chk, _zoom_chk = ptz.get_status()
print(f">> Status lido de volta após o home: pan={_pan_chk:.2f}° tilt={_tilt_chk:.2f}° zoom={_zoom_chk:.2f}%")
if abs(_pan_chk) > 1.0 or abs(_tilt_chk) > 1.0:
    print("   AVISO: a câmera não reportou pan/tilt ~0 após o home. Isso pode ser normal se o")
    print("   motor ainda estivesse em movimento (aumente o time.sleep acima) ou pode indicar que")
    print("   o 'zero ONVIF' desta câmera não coincide com o centro mecânico esperado - confira")
    print("   fisicamente para onde ela está apontando.")


# ----------------------------------------------------------------------------
# Thread 1: telemetria PTZ contínua -> server
# ----------------------------------------------------------------------------
def telemetry_loop():
    while True:
        try:
            pan_deg, tilt_deg, zoom_pct = ptz.get_status()
            with state.lock:
                state.pan_deg, state.tilt_deg, state.zoom_pct = pan_deg, tilt_deg, zoom_pct
            payload = {
                "coord_p": round(pan_deg, 2),
                "coord_t": round(tilt_deg, 2),
                "coord_z": round(zoom_pct, 2),
                "detect": False,
            }
            requests.post(f"{config.SERVER_URL}/api/telemetry", json=payload, timeout=2)
        except Exception as e:
            print(f"[telemetry] erro: {e}")
        time.sleep(config.PTZ_POLL_INTERVAL_SECONDS)


# ----------------------------------------------------------------------------
# Thread 2: captura RTSP + inferência YOLO + alerta de rachadura
# ----------------------------------------------------------------------------
def detection_loop():
    model = YOLO(config.YOLO_MODEL_PATH)
    cap = cv2.VideoCapture(config.RTSP_URL)
    frame_count = 0

    if not cap.isOpened():
        print("[detection] ERRO: não foi possível abrir o RTSP. Verifique RTSP_URL.")
        return

    while True:
        ok, frame = cap.read()
        if not ok:
            print("[detection] falha ao ler frame, tentando reconectar...")
            cap.release()
            time.sleep(2)
            cap = cv2.VideoCapture(config.RTSP_URL)
            continue

        frame_count += 1

        # atualiza o frame cru pro stream MJPEG (sempre, independente de inferência)
        ok_jpeg, buf = cv2.imencode(".jpg", frame)
        if ok_jpeg:
            with state.lock:
                state.last_frame_jpeg = buf.tobytes()

        if frame_count % config.YOLO_INFER_EVERY_N_FRAMES != 0:
            continue

        results = model.predict(frame, conf=config.YOLO_CONF_THRESHOLD, verbose=False)
        result = results[0]

        has_crack = result.boxes is not None and len(result.boxes) > 0
        if not has_crack:
            continue

        now = time.time()
        with state.lock:
            since_last = now - state.last_detection_time
        if since_last < config.DETECTION_COOLDOWN_SECONDS:
            continue  # evita floodar o server

        with state.lock:
            state.last_detection_time = now
            pan_deg, tilt_deg, zoom_pct = state.pan_deg, state.tilt_deg, state.zoom_pct

        # imagem com a máscara de segmentação desenhada (plot() já desenha boxes+masks)
        annotated = result.plot()
        ok_orig, buf_orig = cv2.imencode(".jpg", frame)
        ok_mask, buf_mask = cv2.imencode(".jpg", annotated)

        payload = {
            "coord_p": round(pan_deg, 2),
            "coord_t": round(tilt_deg, 2),
            "coord_z": round(zoom_pct, 2),
            "detect": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "image_b64": base64.b64encode(buf_orig.tobytes()).decode() if ok_orig else None,
            "mask_image_b64": base64.b64encode(buf_mask.tobytes()).decode() if ok_mask else None,
        }
        try:
            requests.post(f"{config.SERVER_URL}/api/detection", json=payload, timeout=5)
            print(f"[detection] rachadura detectada! p={pan_deg} t={tilt_deg} z={zoom_pct}")
        except Exception as e:
            print(f"[detection] erro ao avisar o server: {e}")


# ----------------------------------------------------------------------------
# API local: recebe comandos do dashboard (via server) e serve o MJPEG
# ----------------------------------------------------------------------------
app = FastAPI(title="Camera Controller API")


class MoveCommand(BaseModel):
    pan_delta: float = 0.0
    tilt_delta: float = 0.0
    zoom_delta: float = 0.0


@app.post("/command")
def command(cmd: MoveCommand):
    new_pan, new_tilt, new_zoom = ptz.move_relative(cmd.pan_delta, cmd.tilt_delta, cmd.zoom_delta)
    with state.lock:
        state.pan_deg, state.tilt_deg, state.zoom_pct = new_pan, new_tilt, new_zoom
    return {"coord_p": new_pan, "coord_t": new_tilt, "coord_z": new_zoom}


@app.post("/command/home")
def command_home():
    ptz.go_home()
    return {"status": "ok"}


def mjpeg_generator():
    while True:
        with state.lock:
            frame = state.last_frame_jpeg
        if frame is not None:
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        time.sleep(0.05)


@app.get("/video_feed")
def video_feed():
    return StreamingResponse(mjpeg_generator(), media_type="multipart/x-mixed-replace; boundary=frame")


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    threading.Thread(target=telemetry_loop, daemon=True).start()
    threading.Thread(target=detection_loop, daemon=True).start()
    uvicorn.run(app, host=config.CONTROLLER_API_HOST, port=config.CONTROLLER_API_PORT)

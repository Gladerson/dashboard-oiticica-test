# ============================================================================
# controller.py - Controlador da câmera
#
# Modelo de movimento (v3): os endpoints NÃO falam com a câmera. Eles apenas
# registram uma "intenção de movimento" com prazo de validade. Uma única
# thread (PTZMotion) compara a intenção com o estado já aplicado e emite
# ContinuousMove/Stop. Isso elimina a corrida entre /continuous e /stop que
# fazia a câmera andar sem parar, e garante parada automática se o dashboard
# sumir (a intenção expira).
# ============================================================================
import base64
import threading
import time
from datetime import datetime, timezone

import cv2
import requests
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from ultralytics import YOLO

import config
from onvif_ptz import PTZController

ZERO = (0.0, 0.0, 0.0)


# ----------------------------------------------------------------------------
class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        self.pan_deg = 0.0
        self.tilt_deg = 0.0
        self.zoom_pct = 0.0
        self.last_frame_jpeg = None
        self.last_detection_time = 0.0
        self.fast_until = 0.0


state = SharedState()

ptz_cmd = PTZController(
    config.CAMERA_IP, config.ONVIF_PORT, config.ONVIF_USER, config.ONVIF_PASSWORD,
    label="cmd", pan_deg_range=config.PAN_DEG_RANGE, tilt_deg_range=config.TILT_DEG_RANGE,
)
print(">> Conectado à câmera ONVIF.")
print(ptz_cmd.describe())

if config.PTZ_SEPARATE_CONNECTIONS:
    ptz_tel = PTZController(
        config.CAMERA_IP, config.ONVIF_PORT, config.ONVIF_USER, config.ONVIF_PASSWORD,
        label="tel", pan_deg_range=config.PAN_DEG_RANGE, tilt_deg_range=config.TILT_DEG_RANGE,
    )
    print(">> Conexão ONVIF dedicada à telemetria criada.")
else:
    ptz_tel = ptz_cmd
    print(">> Usando uma única conexão ONVIF (PTZ_SEPARATE_CONNECTIONS=false).")


def marcar_movimento():
    with state.lock:
        state.fast_until = time.time() + config.PTZ_FAST_WINDOW_SECONDS


# ----------------------------------------------------------------------------
# Motor de movimento: única thread que emite ContinuousMove/Stop
# ----------------------------------------------------------------------------
class PTZMotion:
    def __init__(self, ptz, tick=None):
        self.ptz = ptz
        self.tick = tick or config.PTZ_MOTION_TICK_SECONDS
        self._lock = threading.Lock()
        self._intent = ZERO
        self._expires = 0.0
        self._applied = ZERO          # o que a câmera está fazendo agora
        self._precisa_stop = False    # força um Stop mesmo se já achamos que parou

    # -- chamado pelos endpoints (retorna instantaneamente) ------------------
    def solicitar(self, pan, tilt, zoom, hold_s):
        with self._lock:
            self._intent = (float(pan), float(tilt), float(zoom))
            self._expires = time.time() + float(hold_s)
        marcar_movimento()

    def parar(self):
        with self._lock:
            self._intent = ZERO
            self._expires = 0.0
            self._precisa_stop = True   # garante o Stop mesmo em corrida
        marcar_movimento()

    def em_movimento(self):
        with self._lock:
            return self._applied != ZERO

    # -- thread ------------------------------------------------------------
    def loop(self):
        while True:
            time.sleep(self.tick)
            with self._lock:
                ativo = time.time() < self._expires
                alvo = self._intent if ativo else ZERO
                forcar = self._precisa_stop
                self._precisa_stop = False

            if alvo == self._applied and not forcar:
                continue

            try:
                if alvo == ZERO:
                    if self._applied != ZERO or forcar:
                        self.ptz.stop()
                        self._applied = ZERO
                elif self.ptz.has_continuous:
                    self.ptz.move_continuous(*alvo)
                    self._applied = alvo
                else:
                    # Fallback para câmeras sem ContinuousMove: passos repetidos
                    # enquanto a intenção estiver viva.
                    self.ptz.move_relative(
                        alvo[0] * config.PAN_STEP_DEG,
                        alvo[1] * config.TILT_STEP_DEG,
                        alvo[2] * config.ZOOM_STEP_PCT,
                    )
                    self._applied = ZERO   # passo é pontual, não é estado
            except Exception as e:
                print(f"[motion] erro ao aplicar {alvo}: {e}")
                # Em caso de erro tentando mover, tenta parar por segurança.
                try:
                    self.ptz.stop()
                except Exception:
                    pass
                self._applied = ZERO


motion = PTZMotion(ptz_cmd)

if not ptz_cmd.has_continuous:
    print("   AVISO: câmera sem ContinuousMove; usando passos repetidos como fallback.")

# NAO mexemos na camera ao subir, por padrao. Duas razoes:
#
#   1. A camera pode ser COMPARTILHADA (o Defense IA da Intelbras usa a
#      mesma). Puxa-la para o ponto zero a cada restart roubaria a cena de
#      quem estiver usando.
#   2. E desnecessario: a telemetria le a posicao ABSOLUTA da camera
#      (GetStatus) a cada ciclo, entao a geometria nao depende de onde ela
#      esta ao ligar. Nada aqui "zera" nada.
#
# Quem quiser o comportamento antigo liga PTZ_ZERO_AO_INICIAR=true.
_p, _t, _z = ptz_cmd.get_status()
if config.PTZ_ZERO_AO_INICIAR:
    print(">> PTZ_ZERO_AO_INICIAR: movendo para o ponto zero das coordenadas ONVIF...")
    ptz_cmd.ir_para_zero()
    time.sleep(3)
    _p, _t, _z = ptz_cmd.get_status()
print(f">> Posição atual da câmera: pan={_p:.2f}° tilt={_t:.2f}° zoom={_z:.2f}% "
      f"(a geometria usa estes valores absolutos; não é preciso zerar)")


# ----------------------------------------------------------------------------
def telemetry_loop():
    session = requests.Session()
    last = (None, None, None)
    last_sent_at = 0.0

    while True:
        agora = time.time()
        with state.lock:
            rapido = agora < state.fast_until
        # durante o movimento, poll rápido para o cone acompanhar
        intervalo = (config.PTZ_POLL_FAST_SECONDS if (rapido or motion.em_movimento())
                     else config.PTZ_POLL_INTERVAL_SECONDS)

        try:
            pan_deg, tilt_deg, zoom_pct = ptz_tel.get_status()
            with state.lock:
                state.pan_deg, state.tilt_deg, state.zoom_pct = pan_deg, tilt_deg, zoom_pct

            mudou = (
                last[0] is None
                or abs(pan_deg - last[0]) > 0.05
                or abs(tilt_deg - last[1]) > 0.05
                or abs(zoom_pct - last[2]) > 0.2
            )
            if mudou or (agora - last_sent_at) > 1.0:
                session.post(
                    f"{config.SERVER_URL}/api/telemetry",
                    json={
                        "coord_p": round(pan_deg, 2),
                        "coord_t": round(tilt_deg, 2),
                        "coord_z": round(zoom_pct, 2),
                        "detect": False,
                    },
                    headers={"Authorization": f"Bearer {config.DEVICE_TOKEN}"},
                    timeout=2,
                )
                last = (pan_deg, tilt_deg, zoom_pct)
                last_sent_at = agora
        except Exception as e:
            print(f"[telemetry] erro: {e}")

        time.sleep(intervalo)


# ----------------------------------------------------------------------------
def detection_loop():
    model = YOLO(config.YOLO_MODEL_PATH)
    cap = cv2.VideoCapture(config.RTSP_URL, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    frame_count = 0

    if not cap.isOpened():
        print("[detection] ERRO: não foi possível abrir o RTSP. Verifique RTSP_URL.")
        return

    while True:
        ok, frame = cap.read()
        if not ok:
            print("[detection] falha ao ler frame, reconectando...")
            cap.release()
            time.sleep(2)
            cap = cv2.VideoCapture(config.RTSP_URL, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            continue

        frame_count += 1

        ok_jpeg, buf = cv2.imencode(".jpg", frame)
        if ok_jpeg:
            with state.lock:
                state.last_frame_jpeg = buf.tobytes()

        if frame_count % config.YOLO_INFER_EVERY_N_FRAMES != 0:
            continue

        # Não vale inferir com a câmera em movimento: sai borrado e a posição
        # PTZ associada à detecção seria imprecisa.
        if motion.em_movimento():
            continue

        results = model.predict(frame, conf=config.YOLO_CONF_THRESHOLD, verbose=False)
        result = results[0]

        if result.boxes is None or len(result.boxes) == 0:
            continue

        now = time.time()
        with state.lock:
            since_last = now - state.last_detection_time
        if since_last < config.DETECTION_COOLDOWN_SECONDS:
            continue

        with state.lock:
            state.last_detection_time = now
            pan_deg, tilt_deg, zoom_pct = state.pan_deg, state.tilt_deg, state.zoom_pct

        annotated = result.plot()
        ok_orig, buf_orig = cv2.imencode(".jpg", frame)
        ok_mask, buf_mask = cv2.imencode(".jpg", annotated)

        try:
            requests.post(
                f"{config.SERVER_URL}/api/detection",
                json={
                    "coord_p": round(pan_deg, 2),
                    "coord_t": round(tilt_deg, 2),
                    "coord_z": round(zoom_pct, 2),
                    "detect": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "image_b64": base64.b64encode(buf_orig.tobytes()).decode() if ok_orig else None,
                    "mask_image_b64": base64.b64encode(buf_mask.tobytes()).decode() if ok_mask else None,
                },
                headers={"Authorization": f"Bearer {config.DEVICE_TOKEN}"},
                timeout=5,
            )
            print(f"[detection] rachadura detectada! p={pan_deg} t={tilt_deg} z={zoom_pct}")
        except Exception as e:
            print(f"[detection] erro ao avisar o server: {e}")


# ----------------------------------------------------------------------------
app = FastAPI(title="Camera Controller API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


class MoveCommand(BaseModel):
    pan_delta: float = 0.0
    tilt_delta: float = 0.0
    zoom_delta: float = 0.0


class ContinuousCommand(BaseModel):
    pan_speed: float = 0.0
    tilt_speed: float = 0.0
    zoom_speed: float = 0.0
    hold_ms: int = 800     # a intenção morre sozinha depois disso


class AbsoluteCommand(BaseModel):
    pan_deg: float
    tilt_deg: float
    zoom_pct: float


@app.get("/status")
def status():
    with state.lock:
        return {
            "coord_p": state.pan_deg,
            "coord_t": state.tilt_deg,
            "coord_z": state.zoom_pct,
            "has_continuous": ptz_cmd.has_continuous,
            "moving": motion.em_movimento(),
        }


@app.post("/command/continuous")
def command_continuous(cmd: ContinuousCommand):
    hold = max(0.2, min(3.0, cmd.hold_ms / 1000.0))
    motion.solicitar(cmd.pan_speed, cmd.tilt_speed, cmd.zoom_speed, hold)
    return {"status": "ok"}


@app.post("/command/stop")
def command_stop():
    motion.parar()
    return {"status": "ok"}


@app.post("/command")
def command(cmd: MoveCommand):
    motion.parar()
    marcar_movimento()
    p, t, z = ptz_cmd.move_relative(cmd.pan_delta, cmd.tilt_delta, cmd.zoom_delta)
    with state.lock:
        state.pan_deg, state.tilt_deg, state.zoom_pct = p, t, z
    return {"coord_p": p, "coord_t": t, "coord_z": z}


@app.post("/command/absolute")
def command_absolute(cmd: AbsoluteCommand):
    motion.parar()
    time.sleep(config.PTZ_MOTION_TICK_SECONDS * 2)  # deixa o Stop sair antes
    marcar_movimento()
    p, t, z = ptz_cmd.move_absolute(cmd.pan_deg, cmd.tilt_deg, cmd.zoom_pct)
    with state.lock:
        state.pan_deg, state.tilt_deg, state.zoom_pct = p, t, z
    return {"coord_p": p, "coord_t": t, "coord_z": z}


@app.post("/command/home")
def command_home():
    """Home guardado na camera; cai para o ponto zero se ela nao tiver."""
    motion.parar()
    time.sleep(config.PTZ_MOTION_TICK_SECONDS * 2)
    marcar_movimento()
    if not ptz_cmd.ir_para_home():
        ptz_cmd.ir_para_zero()
        return {"status": "ok", "alvo": "zero", "detalhe": "camera sem home ONVIF"}
    return {"status": "ok", "alvo": "home"}


@app.post("/command/zero")
def command_zero():
    """Origem das coordenadas ONVIF (pan=0, tilt=0)."""
    motion.parar()
    time.sleep(config.PTZ_MOTION_TICK_SECONDS * 2)
    marcar_movimento()
    ptz_cmd.ir_para_zero()
    return {"status": "ok", "alvo": "zero"}


def mjpeg_generator():
    while True:
        with state.lock:
            frame = state.last_frame_jpeg
        if frame is not None:
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        time.sleep(0.05)


@app.get("/video_feed")
def video_feed():
    return StreamingResponse(
        mjpeg_generator(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    threading.Thread(target=motion.loop, daemon=True).start()
    threading.Thread(target=telemetry_loop, daemon=True).start()
    threading.Thread(target=detection_loop, daemon=True).start()
    try:
        uvicorn.run(app, host=config.CONTROLLER_API_HOST, port=config.CONTROLLER_API_PORT)
    finally:
        try:
            ptz_cmd.stop()   # nunca deixa a câmera girando ao encerrar
            print(">> Stop enviado ao encerrar.")
        except Exception:
            pass
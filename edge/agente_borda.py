#!/usr/bin/env python3
# ============================================================================
# agente_borda.py - Agente de borda do Raspberry Pi 5 + Hailo-8L
#
# Substitui o controller.py NO PI (no desktop o controller.py continua igual).
# Faz tres coisas:
#
#   1. Inferencia LOCAL (backbone na NPU, cabecalho no CPU) sobre o RTSP da
#      camera. O video nao sai do Pi.
#   2. Publica METADADOS por um transporte plugavel (HTTP hoje, MQTT/
#      ThingsBoard depois). Trocavel em runtime, pelo dashboard.
#   3. Publica frames de video APENAS enquanto o servidor pedir, com prazo de
#      validade. Sem pedido, zero bytes de video na rede.
#
# O PTZ (endpoints /status, /command/*) e identico ao do controller.py, para
# o dashboard nao precisar saber com qual dos dois esta falando.
# ============================================================================
import base64
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

import config_borda as cfg
import transporte as tp
from inferencia_hailo import DetectorHailo, contorno_normalizado

# onvif_ptz.py e reaproveitado do controller, sem copia duplicada.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "controller"))
from onvif_ptz import PTZController  # noqa: E402

ZERO = (0.0, 0.0, 0.0)

# Acorda a thread de evidencias no instante em que o pedido chega, em
# vez de deixa-la esperando o proximo tick do laco.
ha_pedido_imagem = threading.Event()

# Sinaliza encerramento: a thread de video precisa sair do infer() ANTES
# de o ExitStack fechar o pipeline, ou o destrutor do HailoRT aborta.
parar_tudo = threading.Event()


# ============================================================================
# Estado compartilhado
# ============================================================================
class Estado:
    def __init__(self):
        self.lock = threading.RLock()
        self.pan = self.tilt = self.zoom = 0.0
        self.rapido_ate = 0.0

        self.frame_rgb = None          # ultimo frame decodificado (RGB)
        self.dets = []                 # ultimas deteccoes desse frame
        self.frame_seq = 0

        self.ultima_deteccao = 0.0
        self.total_deteccoes = 0
        self.fps = 0.0

        # Stream sob demanda: prazo de validade, nunca um booleano solto.
        # Booleano solto e como o Pi fica transmitindo para sempre quando o
        # servidor cai antes de mandar o "pare".
        self.stream_ate = 0.0

        # Ajustes que o servidor pode mudar em runtime
        self.conf = cfg.CONF_THRESHOLD
        self.iou = cfg.IOU_THRESHOLD
        self.intervalo_frames = cfg.INFERIR_A_CADA_N_FRAMES
        self.cooldown = cfg.COOLDOWN_DETECCAO_S
        self.stream_fps = cfg.STREAM_FPS
        self.stream_largura = cfg.STREAM_LARGURA
        self.stream_q = cfg.STREAM_JPEG_Q
        self.stream_anotado = cfg.STREAM_ANOTADO

        self.transporte_alvo = cfg.TRANSPORTE_INICIAL
        self.transporte_erro = ""
        self.versao_estado = -1
        self.pedidos_imagem = []

    def streaming(self):
        with self.lock:
            return time.time() < self.stream_ate

    def marcar_movimento(self):
        with self.lock:
            self.rapido_ate = time.time() + cfg.JANELA_RAPIDA_S


est = Estado()
cfg.EVIDENCIAS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================================
# PTZ (mesmo motor de intencao com prazo do controller.py)
# ============================================================================
ptz_cmd = PTZController(
    cfg.CAMERA_IP, cfg.ONVIF_PORT, cfg.ONVIF_USER, cfg.ONVIF_PASSWORD,
    label="cmd", pan_deg_range=cfg.PAN_DEG_RANGE, tilt_deg_range=cfg.TILT_DEG_RANGE,
)
print(">> Conectado a camera ONVIF.")
print(ptz_cmd.describe())

if cfg.PTZ_SEPARATE_CONNECTIONS:
    ptz_tel = PTZController(
        cfg.CAMERA_IP, cfg.ONVIF_PORT, cfg.ONVIF_USER, cfg.ONVIF_PASSWORD,
        label="tel", pan_deg_range=cfg.PAN_DEG_RANGE, tilt_deg_range=cfg.TILT_DEG_RANGE,
    )
else:
    ptz_tel = ptz_cmd


class PTZMotion:
    def __init__(self, ptz, tick):
        self.ptz, self.tick = ptz, tick
        self._lock = threading.Lock()
        self._intent, self._expires = ZERO, 0.0
        self._applied = ZERO
        self._precisa_stop = False

    def solicitar(self, pan, tilt, zoom, hold_s):
        with self._lock:
            self._intent = (float(pan), float(tilt), float(zoom))
            self._expires = time.time() + float(hold_s)
        est.marcar_movimento()

    def parar(self):
        with self._lock:
            self._intent, self._expires = ZERO, 0.0
            self._precisa_stop = True
        est.marcar_movimento()

    def em_movimento(self):
        with self._lock:
            return self._applied != ZERO

    def loop(self):
        while True:
            time.sleep(self.tick)
            with self._lock:
                ativo = time.time() < self._expires
                alvo = self._intent if ativo else ZERO
                forcar, self._precisa_stop = self._precisa_stop, False
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
                    self.ptz.move_relative(alvo[0] * cfg.PAN_STEP_DEG,
                                           alvo[1] * cfg.TILT_STEP_DEG,
                                           alvo[2] * cfg.ZOOM_STEP_PCT)
                    self._applied = ZERO
            except Exception as e:
                print(f"[motion] erro ao aplicar {alvo}: {e}")
                try:
                    self.ptz.stop()
                except Exception:
                    pass
                self._applied = ZERO


motion = PTZMotion(ptz_cmd, cfg.PTZ_MOTION_TICK_S)


# ============================================================================
# Transporte: troca a quente entre HTTP e MQTT
# ============================================================================
class Canal:
    def __init__(self):
        self.lock = threading.Lock()
        self.atual = tp.construir(cfg.TRANSPORTE_INICIAL, cfg)
        self.atual.iniciar()
        self.desde = time.time()
        print(f">> Transporte inicial: {self.atual.nome}")

    def nome(self):
        with self.lock:
            return self.atual.nome

    def usar(self, nome):
        nome = (nome or "http").strip().lower()
        with self.lock:
            if nome == self.atual.nome:
                return True
            try:
                novo = tp.construir(nome, cfg)
                novo.iniciar()
            except Exception as e:
                est.transporte_erro = str(e)
                print(f"[canal] nao consegui mudar para {nome}: {e}")
                return False
            antigo = self.atual
            self.atual = novo
            self.desde = time.time()
            est.transporte_erro = ""
            print(f">> Transporte trocado: {antigo.nome} -> {novo.nome}")
        try:
            antigo.parar()
        except Exception:
            pass
        return True

    def __getattr__(self, item):
        # telemetria/deteccao/frame/imagem/estado_desejado/conectado
        return getattr(object.__getattribute__(self, "atual"), item)


canal = Canal()


def aplicar_estado(estado):
    """Converge o agente para o estado que o servidor pediu."""
    if not isinstance(estado, dict):
        return
    versao = int(estado.get("versao", 0))
    with est.lock:
        if versao < est.versao_estado:
            return
        est.versao_estado = versao

        s = estado.get("stream") or {}
        if s.get("ativo"):
            # O servidor manda o TEMPO QUE FALTA, nao um horario absoluto:
            # assim nao dependemos de o relogio do Pi estar sincronizado.
            restante = float(s.get("restante_s", 0))
            est.stream_ate = time.time() + min(restante, cfg.STREAM_TTL_S)
        else:
            est.stream_ate = 0.0
        est.stream_fps = float(s.get("fps", est.stream_fps))
        est.stream_largura = int(s.get("largura", est.stream_largura))
        est.stream_q = int(s.get("qualidade", est.stream_q))
        est.stream_anotado = bool(s.get("anotado", est.stream_anotado))

        inf = estado.get("inferencia") or {}
        est.conf = float(inf.get("conf", est.conf))
        est.iou = float(inf.get("iou", est.iou))
        est.intervalo_frames = max(1, int(inf.get("intervalo_frames", est.intervalo_frames)))
        est.cooldown = float(inf.get("cooldown_s", est.cooldown))

        est.transporte_alvo = (estado.get("transporte") or est.transporte_alvo).lower()
        pedidos = estado.get("pedidos_imagem") or []
        for det_id in pedidos:
            if det_id not in est.pedidos_imagem:
                est.pedidos_imagem.append(det_id)
                ha_pedido_imagem.set()


def gerente_transporte_loop():
    """Aplica a troca de transporte e faz o fallback MQTT -> HTTP."""
    while True:
        time.sleep(1.0)
        alvo = est.transporte_alvo
        if alvo != canal.nome():
            canal.usar(alvo)

        # Se o broker sumiu, voltamos sozinhos para HTTP. Sem isso, uma troca
        # para MQTT com o broker fora do ar deixaria o Pi mudo e sem nenhum
        # caminho de volta -- so ida ao campo resolveria.
        if canal.nome() == "mqtt" and not canal.conectado():
            fora = time.time() - max(canal.desde, canal.atual.ultimo_contato)
            if fora > cfg.MQTT_FALLBACK_SEGUNDOS:
                print(f"[canal] broker mudo ha {fora:.0f}s -- voltando para HTTP.")
                est.transporte_erro = "broker inacessivel; voltei para HTTP"
                est.transporte_alvo = "http"
                canal.usar("http")


# ============================================================================
# Telemetria (subida) + estado desejado (descida, de carona na resposta)
# ============================================================================
def temperatura_cpu():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


def telemetria_loop():
    ultimo = (None, None, None)
    ultimo_envio = 0.0
    while True:
        agora = time.time()
        with est.lock:
            rapido = agora < est.rapido_ate
        intervalo = (cfg.TELEMETRIA_INTERVALO_RAPIDO_S
                     if (rapido or motion.em_movimento())
                     else cfg.TELEMETRIA_INTERVALO_S)
        try:
            pan, tilt, zoom = ptz_tel.get_status()
            with est.lock:
                est.pan, est.tilt, est.zoom = pan, tilt, zoom

            mudou = (ultimo[0] is None
                     or abs(pan - ultimo[0]) > 0.05
                     or abs(tilt - ultimo[1]) > 0.05
                     or abs(zoom - ultimo[2]) > 0.2)
            if mudou or (agora - ultimo_envio) > 1.0:
                valores = {
                    "pan": round(pan, 2),
                    "tilt": round(tilt, 2),
                    "zoom": round(zoom, 2),
                    "movendo": motion.em_movimento(),
                    "fps": round(est.fps, 1),
                    "npu_ms": detector.ultimo_ms["npu"] if detector else 0.0,
                    "cpu_ms": detector.ultimo_ms["cpu"] if detector else 0.0,
                    "det_total": est.total_deteccoes,
                    "conf": round(est.conf, 3),
                    "stream": est.streaming(),
                    "transporte": canal.nome(),
                    "cpu_temp": temperatura_cpu(),
                }
                if est.transporte_erro:
                    valores["transporte_erro"] = est.transporte_erro
                canal.telemetria(valores)
                ultimo, ultimo_envio = (pan, tilt, zoom), agora

            # O estado desejado chega junto da resposta (HTTP) ou por
            # atributo/RPC (MQTT). Aqui so aplicamos o que chegou.
            aplicar_estado(canal.estado_desejado())
        except Exception as e:
            print(f"[telemetria] erro: {e}")
        time.sleep(intervalo)


# ============================================================================
# Deteccao (subida) + evidencia local
# ============================================================================
def _jpeg(img_rgb, qualidade):
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR),
                           [int(cv2.IMWRITE_JPEG_QUALITY), int(qualidade)])
    return buf.tobytes() if ok else None


def publicar_deteccao(frame_rgb, dets):
    """Publica SO coordenadas/metadados -- nenhuma imagem viaja aqui.

    O frame so e gravado em disco (e so ele sobe, sob pedido) quando o
    servidor confirma que esta deteccao virou um alerta NOVO (status "ok").
    Deteccoes repetidas da mesma rachadura ja pendente, ou dentro do
    rearme, o servidor descarta (ve _mesmo_ponto em server.py) e nao vale a
    pena gravar evidencia para elas -- era exatamente isso que enchia
    edge/evidencias/ rapido demais: uma foto nova por deteccao, mesmo para
    reincidencias que nunca geravam um alerta distinto.
    """
    det_id = str(uuid.uuid4())
    h, w = frame_rgb.shape[:2]

    with est.lock:
        pan, tilt, zoom = est.pan, est.tilt, est.zoom
        est.total_deteccoes += 1

    poligonos = [contorno_normalizado(d["mascara"], d["bbox"], w, h) for d in dets]
    areas = [int(d["mascara"].sum()) for d in dets]

    payload = {
        "evt": "deteccao",
        "det_id": det_id,
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "pan": round(pan, 2), "tilt": round(tilt, 2), "zoom": round(zoom, 2),
        "n": len(dets),
        "conf_max": round(max(d["conf"] for d in dets), 3),
        "conf_media": round(sum(d["conf"] for d in dets) / len(dets), 3),
        "area_px": sum(areas),
        "area_frac": round(sum(areas) / float(w * h), 6),
        # bbox e poligono viajam como string JSON: o ThingsBoard indexa bem
        # escalares e strings, mas nao arrays aninhados. Sao as coordenadas
        # da deteccao (pixel/normalizadas) -- o servidor converte a pose
        # pan/tilt/zoom em ponto 3D real (UTM) via raycasting.
        "bbox": json.dumps([d["bbox"] for d in dets], separators=(",", ":")),
        "poly": json.dumps(poligonos, separators=(",", ":")),
        "frame_w": w, "frame_h": h,
        "modelo": os.path.basename(cfg.HEF_PATH),
        "limiar": round(est.conf, 3),
        "evidencia_local": True,
    }

    resposta = canal.deteccao(payload)
    # /api/edge/deteccao sempre devolve {"status": "ok", "detalhe": {...}} --
    # esse "status" de fora e so "o POST chegou", nao o resultado da
    # deteccao. O status de verdade (ok/duplicada/em_rearme) vem em
    # "detalhe", que e o retorno do /api/detection no servidor.
    # MQTT e fire-and-forget (telemetria do ThingsBoard nao responde o
    # publish), e nesse caso gravamos por precaucao -- nao ha como saber se
    # o servidor vai descartar.
    detalhe = (resposta or {}).get("detalhe") or {}
    eh_novo = resposta is None or detalhe.get("status") == "ok"
    if eh_novo:
        # Frame CRU, sem nada desenhado em cima: o dashboard desenha a
        # mascara/bbox no navegador a partir de "poly"/"bbox" (ja enviados
        # acima), entao "Ver original" e "Ver mascara" acabam sendo duas
        # formas de olhar para o MESMO jpeg, uma com o contorno desenhado
        # por cima e outra sem.
        caminho = cfg.EVIDENCIAS_DIR / f"{det_id}.jpg"
        try:
            caminho.write_bytes(_jpeg(frame_rgb, cfg.EVIDENCIA_JPEG_Q) or b"")
        except Exception as e:
            print(f"[deteccao] nao consegui gravar a evidencia: {e}")

    print(f"[deteccao] {det_id[:8]} n={len(dets)} conf={payload['conf_max']} "
          f"p={pan:.1f} t={tilt:.1f} z={zoom:.1f} "
          f"status={detalhe.get('status') if resposta else '?'} "
          f"evidencia={'gravada' if eh_novo else 'descartada (nao e alerta novo)'}")


def atender_pedidos_imagem():
    """Sobe a evidencia completa de uma deteccao, quando o operador pedir."""
    while True:
        # Acorda na hora quando ha pedido; o teto de 1s cobre o caso de o
        # pedido ter chegado de carona na telemetria, sem o empurrao direto.
        ha_pedido_imagem.wait(timeout=1.0)
        ha_pedido_imagem.clear()
        with est.lock:
            pendentes, est.pedidos_imagem = est.pedidos_imagem, []
        for det_id in pendentes:
            caminho = cfg.EVIDENCIAS_DIR / f"{det_id}.jpg"
            if not caminho.exists():
                canal.imagem({"evt": "imagem", "det_id": det_id, "erro": "nao encontrada"})
                continue
            dados = caminho.read_bytes()
            canal.imagem({
                "evt": "imagem", "det_id": det_id,
                "img_b64": base64.b64encode(dados).decode(),
                "img_bytes": len(dados),
            })
            print(f"[imagem] enviada evidencia {det_id[:8]} ({len(dados)}B)")


def limpar_evidencias():
    """Impede o crescimento sem limite do cartao SD: passa a tesoura nas mais
    antigas quando a pasta passa do teto."""
    while True:
        time.sleep(600)
        try:
            arquivos = sorted(cfg.EVIDENCIAS_DIR.glob("*.jpg"),
                              key=lambda p: p.stat().st_mtime)
            total = sum(p.stat().st_size for p in arquivos)
            teto = cfg.EVIDENCIAS_MAX_MB * 1024 * 1024
            while arquivos and total > teto:
                p = arquivos.pop(0)
                total -= p.stat().st_size
                p.unlink(missing_ok=True)
        except Exception as e:
            print(f"[evidencias] limpeza falhou: {e}")


# ============================================================================
# Laco de video + inferencia
# ============================================================================
detector = None


def abrir_rtsp():
    cap = cv2.VideoCapture(cfg.RTSP_URL, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def video_loop():
    global detector
    cap = abrir_rtsp()
    if not cap.isOpened():
        print("[video] ERRO: nao consegui abrir o RTSP. Confira RTSP_URL.")
        return

    n = 0
    t_fps = time.time()
    frames_fps = 0

    with DetectorHailo(cfg.HEF_PATH, cfg.HEAD_ONNX_PATH, cfg.MAPA_HEF_PARA_ONNX,
                       input_size=cfg.INPUT_SIZE, threads_cpu=cfg.THREADS_CPU,
                       class_names=cfg.CLASS_NAMES) as det:
        detector = det
        print(">> Pipeline Hailo aberto e ativo (nao reconfigura por frame).")

        while not parar_tudo.is_set():
            ok, frame_bgr = cap.read()
            if not ok:
                print("[video] falha ao ler frame, reconectando...")
                cap.release()
                time.sleep(2)
                cap = abrir_rtsp()
                continue

            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            n += 1
            frames_fps += 1
            if time.time() - t_fps >= 2.0:
                est.fps = frames_fps / (time.time() - t_fps)
                frames_fps, t_fps = 0, time.time()

            with est.lock:
                est.frame_rgb = frame_rgb
                est.frame_seq = n
                intervalo = est.intervalo_frames
                conf, iou, cooldown = est.conf, est.iou, est.cooldown

            if n % intervalo != 0:
                continue
            if motion.em_movimento():
                # Frame borrado e pose imprecisa: nao vale inferir.
                continue

            try:
                dets = det.infer(frame_rgb, conf, iou)
            except Exception as e:
                print(f"[video] erro na inferencia: {e}")
                continue

            with est.lock:
                est.dets = dets
                desde = time.time() - est.ultima_deteccao
            if not dets or desde < cooldown:
                continue
            with est.lock:
                est.ultima_deteccao = time.time()
            publicar_deteccao(frame_rgb, dets)


# ============================================================================
# Stream sob demanda
# ============================================================================
def stream_loop():
    while True:
        if not est.streaming():
            time.sleep(0.2)
            continue
        with est.lock:
            frame = est.frame_rgb
            dets = est.dets
            largura, q, anotado = est.stream_largura, est.stream_q, est.stream_anotado
            fps = max(0.5, est.stream_fps)
            restante = est.stream_ate - time.time()
        if frame is None:
            time.sleep(0.2)
            continue

        img = detector.desenhar(frame, dets) if (anotado and dets and detector) else frame
        h, w = img.shape[:2]
        if w > largura:
            img = cv2.resize(img, (largura, int(h * largura / w)),
                             interpolation=cv2.INTER_AREA)
        jpeg = _jpeg(img, q)
        if jpeg:
            canal.frame(jpeg, {"seq": est.frame_seq, "restante_s": round(restante, 1)})
        time.sleep(1.0 / fps)


# ============================================================================
# API local (PTZ + atalho de baixa latencia para o estado desejado)
# ============================================================================
app = FastAPI(title="Agente de Borda - Oiticica")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


class ContinuousCommand(BaseModel):
    pan_speed: float = 0.0
    tilt_speed: float = 0.0
    zoom_speed: float = 0.0
    hold_ms: int = 800


class MoveCommand(BaseModel):
    pan_delta: float = 0.0
    tilt_delta: float = 0.0
    zoom_delta: float = 0.0


class AbsoluteCommand(BaseModel):
    pan_deg: float
    tilt_deg: float
    zoom_pct: float


@app.get("/status")
def status():
    # Tudo que precisa de outro lock (ou do proprio est.lock, via
    # est.streaming()) e resolvido ANTES de entrar no with. Aninhar
    # est.streaming() dentro de "with est.lock" trava a thread para sempre:
    # threading.Lock nao e reentrante.
    movendo = motion.em_movimento()
    transporte = canal.nome()
    streaming = est.streaming()
    with est.lock:
        return {
            "coord_p": est.pan, "coord_t": est.tilt, "coord_z": est.zoom,
            "has_continuous": ptz_cmd.has_continuous,
            "moving": movendo,
            "transporte": transporte,
            "stream": streaming,
            "fps": round(est.fps, 1),
        }


@app.post("/command/continuous")
def cmd_continuous(c: ContinuousCommand):
    motion.solicitar(c.pan_speed, c.tilt_speed, c.zoom_speed,
                     max(0.2, min(3.0, c.hold_ms / 1000.0)))
    return {"status": "ok"}


@app.post("/command/stop")
def cmd_stop():
    motion.parar()
    return {"status": "ok"}


@app.post("/command")
def cmd(c: MoveCommand):
    motion.parar()
    est.marcar_movimento()
    p, t, z = ptz_cmd.move_relative(c.pan_delta, c.tilt_delta, c.zoom_delta)
    with est.lock:
        est.pan, est.tilt, est.zoom = p, t, z
    return {"coord_p": p, "coord_t": t, "coord_z": z}


@app.post("/command/absolute")
def cmd_absolute(c: AbsoluteCommand):
    motion.parar()
    time.sleep(cfg.PTZ_MOTION_TICK_S * 2)
    est.marcar_movimento()
    p, t, z = ptz_cmd.move_absolute(c.pan_deg, c.tilt_deg, c.zoom_pct)
    with est.lock:
        est.pan, est.tilt, est.zoom = p, t, z
    return {"coord_p": p, "coord_t": t, "coord_z": z}


@app.post("/command/home")
def cmd_home():
    """Vai para o HOME guardado na camera -- a mesma referencia que os outros
    sistemas que compartilham a camera usam. Se a camera nao implementar
    GotoHomePosition, cai para o ponto zero das coordenadas ONVIF."""
    motion.parar()
    time.sleep(cfg.PTZ_MOTION_TICK_S * 2)
    est.marcar_movimento()
    if not ptz_cmd.ir_para_home():
        ptz_cmd.ir_para_zero()
        return {"status": "ok", "alvo": "zero", "detalhe": "camera sem home ONVIF"}
    return {"status": "ok", "alvo": "home"}


@app.post("/command/zero")
def cmd_zero():
    """Origem das coordenadas ONVIF (pan=0, tilt=0). Util para conferir a
    geometria: e o ponto onde o base_forward da calibracao esta ancorado."""
    motion.parar()
    time.sleep(cfg.PTZ_MOTION_TICK_S * 2)
    est.marcar_movimento()
    ptz_cmd.ir_para_zero()
    return {"status": "ok", "alvo": "zero"}


@app.post("/borda/estado")
async def borda_estado(req: Request):
    """Atalho: na LAN o servidor empurra o estado desejado direto, e o stream
    comeca em ~100ms em vez de esperar o proximo ciclo de telemetria. E so um
    acelerador -- se esta rota nao existir ou falhar, o mecanismo normal
    (carona na resposta da telemetria) resolve igual, so que ate 1s depois."""
    try:
        aplicar_estado(await req.json())
    except Exception as e:
        return {"status": "erro", "detalhe": str(e)}
    return {"status": "ok", "streaming": est.streaming(), "transporte": canal.nome()}


@app.get("/borda/preview.jpg")
def preview():
    """Frame unico para depuracao local (curl/navegador no proprio Pi).
    Nao e o caminho do stream para o servidor."""
    with est.lock:
        frame, dets = est.frame_rgb, est.dets
    if frame is None:
        return Response(status_code=503)
    img = detector.desenhar(frame, dets) if (dets and detector) else frame
    return Response(content=_jpeg(img, 70), media_type="image/jpeg")


def mjpeg():
    while True:
        with est.lock:
            frame, dets = est.frame_rgb, est.dets
        if frame is not None:
            img = detector.desenhar(frame, dets) if (dets and detector) else frame
            jpeg = _jpeg(img, 60)
            if jpeg:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
        time.sleep(0.1)


@app.get("/video_feed")
def video_feed():
    """Mantido so para diagnostico na propria LAN. O dashboard NAO usa mais
    esta rota -- ela abre uma conexao permanente e era exatamente a fonte do
    trafego continuo que queremos eliminar."""
    return StreamingResponse(mjpeg(),
                             media_type="multipart/x-mixed-replace; boundary=frame")


# ============================================================================
if __name__ == "__main__":
    # Por padrao NAO mexemos na camera ao subir: ela pode estar sendo usada
    # por outro sistema (Defense IA), e a geometria nao precisa disso -- a
    # telemetria le a posicao absoluta (GetStatus) a cada ciclo.
    p, t, z = ptz_cmd.get_status()
    if cfg.PTZ_ZERO_AO_INICIAR:
        print(">> PTZ_ZERO_AO_INICIAR: indo para o ponto zero das coordenadas ONVIF...")
        ptz_cmd.ir_para_zero()
        time.sleep(3)
        p, t, z = ptz_cmd.get_status()
    print(f">> Posicao da camera: pan={p:.2f} tilt={t:.2f} zoom={z:.2f} "
          f"(absoluta; nao e preciso zerar)")

    for alvo in (motion.loop, video_loop, telemetria_loop, stream_loop,
                 gerente_transporte_loop, atender_pedidos_imagem,
                 limpar_evidencias):
        threading.Thread(target=alvo, daemon=True).start()

    try:
        # Se a porta ja estiver ocupada o uvicorn apenas RETORNA. Sem este
        # aviso o agente seguiria mexendo na camera com a API morta, e o
        # dashboard nao teria como control-lo.
        import socket
        _s = socket.socket()
        _s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            _s.bind((cfg.API_HOST, cfg.API_PORT))
        except OSError as e:
            sys.exit(f"ERRO: porta {cfg.API_PORT} ocupada ({e}). "
                     f"Verifique com: sudo ss -lptn 'sport = :{cfg.API_PORT}'")
        finally:
            _s.close()

        uvicorn.run(app, host=cfg.API_HOST, port=cfg.API_PORT, log_level="warning")
    finally:
        parar_tudo.set()
        time.sleep(0.5)   # deixa o infer() em curso terminar
        try:
            ptz_cmd.stop()
            print(">> Stop enviado ao encerrar.")
        except Exception:
            pass

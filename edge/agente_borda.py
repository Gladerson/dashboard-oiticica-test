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
import secrets
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
from fastapi.responses import JSONResponse, Response, StreamingResponse
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
class CameraIndisponivel(RuntimeError):
    """A camera nao respondeu. Quem chama decide o que fazer -- e ninguem
    deve morrer por causa disso."""


class CameraPTZ:
    """Dono da conexao ONVIF, preguicoso e teimoso.

    Antes o PTZController era criado no TOPO do modulo. Sem camera na rede,
    a excecao subia na importacao e o processo morria antes mesmo de iniciar
    as threads -- levando junto coisas que nao dependem de camera nenhuma: o
    canal de comandos com o servidor, a telemetria de CPU e temperatura, e a
    propria possibilidade de diagnosticar o Raspberry de longe. Com
    Restart=always o servico entrava em ciclo de reinicio, e uma camera
    queimada deixava o equipamento INVISIVEL no painel.

    Aqui a conexao e feita na primeira vez que alguem precisa dela, e nunca
    se desiste: a cada falha a proxima tentativa e adiada um pouco mais, ate
    um teto de 60s. Uma camera que volta depois de semanas e reencontrada
    sozinha, sem reiniciar servico nenhum -- e o laco de telemetria, que roda
    a cada segundo, e quem exercita essa retentativa.
    """

    RETENTAR_MIN_S = 5.0
    RETENTAR_MAX_S = 60.0

    def __init__(self, rotulo):
        self.rotulo = rotulo
        self._ptz = None
        self._erro = "ainda nao conectada"
        self._proxima = 0.0
        self._espera = self.RETENTAR_MIN_S
        # Reentrante: obter() pode ser chamado de dentro de um bloco que ja
        # segura o lock (perdeu() -> obter() no mesmo fluxo).
        self._lock = threading.RLock()

    def conectada(self):
        with self._lock:
            return self._ptz is not None

    def erro(self):
        with self._lock:
            return "" if self._ptz is not None else self._erro

    def obter(self):
        """O PTZController pronto para uso, ou CameraIndisponivel."""
        with self._lock:
            if self._ptz is not None:
                return self._ptz
            if time.time() < self._proxima:
                raise CameraIndisponivel(self._erro)
            try:
                self._ptz = PTZController(
                    cfg.CAMERA_IP, cfg.ONVIF_PORT, cfg.ONVIF_USER,
                    cfg.ONVIF_PASSWORD, label=self.rotulo,
                    pan_deg_range=cfg.PAN_DEG_RANGE,
                    tilt_deg_range=cfg.TILT_DEG_RANGE,
                )
            except Exception as e:
                self._erro = f"{type(e).__name__}: {e}"[:200]
                self._proxima = time.time() + self._espera
                print(f"[camera] ONVIF ({self.rotulo}) fora do ar: {self._erro} "
                      f"-- nova tentativa em {self._espera:.0f}s")
                self._espera = min(self._espera * 2, self.RETENTAR_MAX_S)
                raise CameraIndisponivel(self._erro) from e
            self._erro = ""
            self._espera = self.RETENTAR_MIN_S
            print(f">> Camera ONVIF conectada ({self.rotulo}).")
            print(self._ptz.describe())
            return self._ptz

    def perdeu(self, excecao):
        """Uma operacao falhou: descarta a conexao para a proxima refazer.

        Sem adiar a proxima tentativa -- um erro pontual de PTZ nao deve
        custar 5s de espera. Camera realmente morta cai no atraso crescente
        de obter(), que e onde ele pertence."""
        with self._lock:
            self._ptz = None
            self._erro = f"{type(excecao).__name__}: {excecao}"[:200]
            self._proxima = 0.0


camera_cmd = CameraPTZ("cmd")
# Duas conexoes separadas evitam que a leitura de posicao (1x/s) dispute o
# mesmo socket com os comandos de PTZ. Ver PTZ_SEPARATE_CONNECTIONS.
camera_tel = CameraPTZ("tel") if cfg.PTZ_SEPARATE_CONNECTIONS else camera_cmd


class PTZMotion:
    def __init__(self, camera, tick):
        # Guarda a CameraPTZ, nao o PTZController: a conexao pode nao existir
        # ainda, e pode ser trocada por outra depois de uma queda.
        self.camera, self.tick = camera, tick
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
                ptz = self.camera.obter()
                if alvo == ZERO:
                    if self._applied != ZERO or forcar:
                        ptz.stop()
                        self._applied = ZERO
                elif ptz.has_continuous:
                    ptz.move_continuous(*alvo)
                    self._applied = alvo
                else:
                    ptz.move_relative(alvo[0] * cfg.PAN_STEP_DEG,
                                      alvo[1] * cfg.TILT_STEP_DEG,
                                      alvo[2] * cfg.ZOOM_STEP_PCT)
                    self._applied = ZERO
            except CameraIndisponivel:
                # Nada a fazer e nada a registrar: o laco roda varias vezes
                # por segundo e encheria o journal. Quem avisa e a telemetria.
                self._applied = ZERO
            except Exception as e:
                print(f"[motion] erro ao aplicar {alvo}: {e}")
                self.camera.perdeu(e)
                self._applied = ZERO


motion = PTZMotion(camera_cmd, cfg.PTZ_MOTION_TICK_S)


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
            # A leitura de posicao e a UNICA parte daqui que depende da
            # camera. Antes, uma falha nela abortava o envio inteiro -- e o
            # servidor, sem telemetria, dava o equipamento como offline: a
            # tela acusava o Raspberry por uma falha da camera. Agora a
            # telemetria sobe do mesmo jeito, dizendo camera_ok=false.
            try:
                pan, tilt, zoom = camera_tel.obter().get_status()
                camera_ok, camera_erro = True, ""
                with est.lock:
                    est.pan, est.tilt, est.zoom = pan, tilt, zoom
            except CameraIndisponivel as e:
                camera_ok, camera_erro = False, str(e)
                with est.lock:
                    pan, tilt, zoom = est.pan, est.tilt, est.zoom
            except Exception as e:
                camera_tel.perdeu(e)
                camera_ok, camera_erro = False, camera_tel.erro()
                with est.lock:
                    pan, tilt, zoom = est.pan, est.tilt, est.zoom

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
                    "camera_ok": camera_ok,
                }
                if not camera_ok:
                    valores["camera_erro"] = camera_erro
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
    n = 0
    t_fps = time.time()
    frames_fps = 0
    espera = 2.0
    cap = None

    # O pipeline do Hailo sobe antes do RTSP e fica de pe: e a NPU, nao
    # depende da camera, e reabri-lo a cada reconexao custaria segundos.
    with DetectorHailo(cfg.HEF_PATH, cfg.HEAD_ONNX_PATH, cfg.MAPA_HEF_PARA_ONNX,
                       input_size=cfg.INPUT_SIZE, threads_cpu=cfg.THREADS_CPU,
                       class_names=cfg.CLASS_NAMES) as det:
        detector = det
        print(">> Pipeline Hailo aberto e ativo (nao reconfigura por frame).")

        while not parar_tudo.is_set():
            if cap is None:
                # Antes, um RTSP fechado na subida encerrava ESTA THREAD para
                # sempre: a camera voltar nao adiantava nada, so reiniciar o
                # servico. Agora insiste indefinidamente, com espera crescente
                # ate 60s -- camera que volta depois de semanas e reencontrada
                # sozinha.
                candidato = abrir_rtsp()
                if not candidato.isOpened():
                    candidato.release()
                    print(f"[video] RTSP indisponivel ({cfg.RTSP_URL}); "
                          f"nova tentativa em {espera:.0f}s")
                    parar_tudo.wait(espera)
                    espera = min(espera * 2, 60.0)
                    continue
                cap = candidato
                espera = 2.0
                print(">> RTSP aberto.")

            ok, frame_bgr = cap.read()
            if not ok:
                print("[video] falha ao ler frame, reconectando...")
                cap.release()
                cap = None
                parar_tudo.wait(2)
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
# Comandos
#
# Cada comando e uma FUNCAO comum, e nao um handler de rota. Existem dois
# jeitos de chegar aqui:
#
#   1. o WebSocket de saida que este agente abre com o servidor (o caminho
#      normal em producao: atravessa NAT, nao exige que o Pi seja alcancavel
#      de fora);
#   2. a API local na porta 8090, para quem esta na MESMA rede (modo LAN do
#      dashboard) e para diagnostico.
#
# Antes so existia (2), e por isso o PTZ so funcionava com o servidor na
# mesma rede do equipamento.
# ============================================================================
def _cmd_status(_=None):
    # Tudo que precisa de outro lock (ou do proprio est.lock, via
    # est.streaming()) e resolvido ANTES de entrar no with. Aninhar
    # est.streaming() dentro de "with est.lock" trava a thread para sempre:
    # threading.Lock nao e reentrante.
    movendo = motion.em_movimento()
    transporte = canal.nome()
    streaming = est.streaming()
    # /status responde MESMO sem camera: e por ele que o operador descobre
    # que o equipamento esta vivo e o que esta faltando.
    conectada = camera_cmd.conectada()
    has_continuous = camera_cmd.obter().has_continuous if conectada else False
    with est.lock:
        return {
            "coord_p": est.pan, "coord_t": est.tilt, "coord_z": est.zoom,
            "camera_ok": conectada,
            "camera_erro": camera_cmd.erro(),
            "has_continuous": has_continuous,
            "moving": movendo,
            "transporte": transporte,
            "stream": streaming,
            "fps": round(est.fps, 1),
        }


def _cmd_continuous(c):
    motion.solicitar(float(c.get("pan_speed", 0)), float(c.get("tilt_speed", 0)),
                     float(c.get("zoom_speed", 0)),
                     max(0.2, min(3.0, float(c.get("hold_ms", 800)) / 1000.0)))
    return {"status": "ok"}


def _cmd_stop(_=None):
    motion.parar()
    return {"status": "ok"}


def _cmd_relativo(c):
    motion.parar()
    est.marcar_movimento()
    p, t, z = camera_cmd.obter().move_relative(float(c.get("pan_delta", 0)),
                                              float(c.get("tilt_delta", 0)),
                                              float(c.get("zoom_delta", 0)))
    with est.lock:
        est.pan, est.tilt, est.zoom = p, t, z
    return {"coord_p": p, "coord_t": t, "coord_z": z}


def _cmd_absoluto(c):
    motion.parar()
    time.sleep(cfg.PTZ_MOTION_TICK_S * 2)
    est.marcar_movimento()
    p, t, z = camera_cmd.obter().move_absolute(float(c["pan_deg"]),
                                              float(c["tilt_deg"]),
                                              float(c["zoom_pct"]))
    with est.lock:
        est.pan, est.tilt, est.zoom = p, t, z
    return {"coord_p": p, "coord_t": t, "coord_z": z}


def _cmd_home(_=None):
    motion.parar()
    time.sleep(cfg.PTZ_MOTION_TICK_S * 2)
    est.marcar_movimento()
    camera_cmd.obter().go_home()
    return {"status": "ok"}


def _cmd_estado(c):
    aplicar_estado(c)
    return {"status": "ok", "streaming": est.streaming(), "transporte": canal.nome()}


COMANDOS = {
    "/status": _cmd_status,
    "/command/continuous": _cmd_continuous,
    "/command/stop": _cmd_stop,
    "/command": _cmd_relativo,
    "/command/absolute": _cmd_absoluto,
    "/command/home": _cmd_home,
    "/borda/estado": _cmd_estado,
}


def executar_comando(rota, corpo):
    fn = COMANDOS.get(rota)
    if fn is None:
        raise ValueError(f"rota desconhecida: {rota}")
    return fn(corpo or {})


# ============================================================================
# Canal de descida: WebSocket de SAIDA para o servidor
#
# Quem disca e o equipamento. E o que permite o servidor estar na nuvem e o
# Pi atras de NAT sem VPN, sem porta aberta e sem IP fixo. Se a conexao cair,
# reconecta com espera crescente; enquanto isso o estado desejado continua
# chegando de carona na resposta da telemetria (nada trava).
# ============================================================================
lan_token_atual = ""      # autoriza o navegador na LAN; trocado a cada conexao


def ws_comandos_loop():
    try:
        from websockets.sync.client import connect as ws_connect
    except Exception as e:
        print(f"[ws] biblioteca 'websockets' indisponivel ({e}); PTZ so pela "
              f"API local. Instale com: pip install 'websockets>=12'")
        return

    global lan_token_atual
    url = cfg.SERVER_URL.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{url.rstrip('/')}/api/edge/ws"
    espera = 1.0

    while not parar_tudo.is_set():
        try:
            with ws_connect(url, additional_headers={
                    "Authorization": f"Bearer {cfg.DEVICE_TOKEN}"},
                    open_timeout=10, close_timeout=5) as ws:
                print(f"[ws] canal de comandos aberto em {url}")
                espera = 1.0
                ws.send(json.dumps({"tipo": "ola", "info": {
                    "device_id": cfg.DEVICE_ID,
                    "api_local": f"{cfg.API_HOST}:{cfg.API_PORT}",
                }}))
                while not parar_tudo.is_set():
                    # O recv com timeout deixa o laco checar parar_tudo e nao
                    # ficar preso para sempre num socket morto por NAT.
                    try:
                        bruto = ws.recv(timeout=30)
                    except TimeoutError:
                        ws.send(json.dumps({"tipo": "ping"}))
                        continue
                    try:
                        msg = json.loads(bruto)
                    except Exception:
                        continue
                    tipo = msg.get("tipo")
                    if tipo == "comando":
                        try:
                            corpo = executar_comando(msg.get("rota"), msg.get("corpo"))
                            resposta = {"tipo": "resposta", "id": msg.get("id"),
                                        "corpo": corpo}
                        except Exception as e:
                            resposta = {"tipo": "resposta", "id": msg.get("id"),
                                        "corpo": {"erro": str(e)}}
                        ws.send(json.dumps(resposta))
                    elif tipo == "estado":
                        aplicar_estado(msg.get("estado") or {})
                    elif tipo == "bem_vindo":
                        lan_token_atual = msg.get("lan_token") or ""
        except Exception as e:
            if parar_tudo.is_set():
                return
            print(f"[ws] canal caiu ({type(e).__name__}: {e}); "
                  f"tentando de novo em {espera:.0f}s")
        lan_token_atual = ""
        parar_tudo.wait(espera)
        espera = min(espera * 2, 30.0)


# ============================================================================
# API local (mesma rede): PTZ de baixa latencia e diagnostico
#
# Por padrao escuta so em 127.0.0.1 -- ela nao tem senha propria e o PTZ nao
# depende mais dela. Para usar o "modo LAN" do dashboard (navegador falando
# direto com o equipamento, sem passar pela nuvem), exponha com
# API_HOST=0.0.0.0: ai as rotas de comando passam a exigir o lan_token que o
# servidor entrega por este mesmo canal.
# ============================================================================
app = FastAPI(title="Agente de Borda - Oiticica")
app.add_middleware(CORSMiddleware, allow_origins=cfg.CORS_ORIGENS,
                   allow_methods=["*"], allow_headers=["*"])

_EXPOSTO_NA_REDE = cfg.API_HOST not in ("127.0.0.1", "localhost", "::1")


def _autorizado(req: Request) -> bool:
    """Quando a API escuta so em localhost, quem chegou ja esta dentro da
    maquina. Exposta na rede, exige o lan_token do servidor -- sem isso
    qualquer um na LAN dirigiria a camera, que era o caso ate aqui."""
    if not _EXPOSTO_NA_REDE:
        return True
    if not lan_token_atual:
        return False
    enviado = (req.headers.get("x-lan-token")
               or req.query_params.get("lan_token") or "")
    return secrets.compare_digest(enviado, lan_token_atual)


def _negado():
    return JSONResponse(
        {"error": "lan_token ausente ou invalido",
         "dica": "O dashboard obtem este token do servidor; use o modo LAN."},
        status_code=401)


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
    return _cmd_status()


@app.exception_handler(CameraIndisponivel)
async def _camera_fora(_req, exc):
    """503 com o motivo, em vez de um 500 sem explicacao. O dashboard mostra
    esta mensagem ao operador."""
    return JSONResponse({"error": "camera indisponivel", "detalhe": str(exc)},
                        status_code=503)


@app.post("/command/continuous")
def cmd_continuous(c: ContinuousCommand, req: Request):
    if not _autorizado(req):
        return _negado()
    return _cmd_continuous(c.model_dump())


@app.post("/command/stop")
def cmd_stop(req: Request):
    if not _autorizado(req):
        return _negado()
    return _cmd_stop()


@app.post("/command")
def cmd(c: MoveCommand, req: Request):
    if not _autorizado(req):
        return _negado()
    return _cmd_relativo(c.model_dump())


@app.post("/command/absolute")
def cmd_absolute(c: AbsoluteCommand, req: Request):
    if not _autorizado(req):
        return _negado()
    return _cmd_absoluto(c.model_dump())


@app.post("/command/home")
def cmd_home(req: Request):
    if not _autorizado(req):
        return _negado()
    return _cmd_home()


@app.post("/borda/estado")
async def borda_estado(req: Request):
    """Atalho para servidor e equipamento na MESMA rede. Hoje o caminho
    normal e o WebSocket de saida; esta rota fica para instalacao local.
    Continua sendo so um acelerador: se falhar, o estado desce de carona na
    resposta da telemetria em ate 1s."""
    if not _autorizado(req):
        return _negado()
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
    # O "home" e desejavel, NAO obrigatorio. Sem camera o agente sobe do
    # mesmo jeito: o canal de comandos, a telemetria e a API local nao
    # dependem dela, e sao justamente o que permite descobrir de longe que a
    # camera e que esta faltando.
    try:
        print(">> Indo para o ponto zero (home)...")
        camera_cmd.obter().go_home()
        time.sleep(3)
        p, t, z = camera_cmd.obter().get_status()
        print(f">> Apos o home: pan={p:.2f} tilt={t:.2f} zoom={z:.2f}")
    except Exception as e:
        print(f">> Sem camera na subida ({e}). O agente sobe assim mesmo e "
              f"reconecta sozinho quando ela voltar.")

    for alvo in (motion.loop, video_loop, telemetria_loop, stream_loop,
                 gerente_transporte_loop, atender_pedidos_imagem,
                 limpar_evidencias, ws_comandos_loop):
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
            if camera_cmd.conectada():
                camera_cmd.obter().stop()
                print(">> Stop enviado ao encerrar.")
        except Exception:
            pass

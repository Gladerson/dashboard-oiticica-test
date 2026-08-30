# ============================================================================
# borda.py - Lado servidor do agente de borda (Raspberry Pi)
#
# Tres responsabilidades:
#
#  1. Guardar o ESTADO DESEJADO do dispositivo (transporte, stream, limiares)
#     e entrega-lo ao Pi. O servidor nunca manda "faca X agora": ele publica
#     em que estado quer o dispositivo e o dispositivo converge. Isso e o que
#     torna o sistema tolerante a queda de rede e a reinicio dos dois lados.
#
#  2. Receber telemetria/deteccao/frames do Pi e reaproveitar EXATAMENTE o
#     mesmo caminho ja usado pelo controller.py do desktop -- raycasting,
#     deduplicacao, historico e WebSocket. Nada disso foi reescrito.
#
#  3. Repassar o stream sob demanda para os navegadores conectados, com
#     prazo de validade de 60s.
# ============================================================================
import base64
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

# --- Parametros -------------------------------------------------------------
STREAM_JANELA_S = float(os.getenv("STREAM_JANELA_S", "60"))
STREAM_FPS = float(os.getenv("STREAM_FPS", "4"))
STREAM_LARGURA = int(os.getenv("STREAM_LARGURA", "640"))
STREAM_QUALIDADE = int(os.getenv("STREAM_QUALIDADE", "60"))
STREAM_ANOTADO = os.getenv("STREAM_ANOTADO", "true").lower() in ("1", "true", "sim")

# Ponte MQTT do servidor (opcional; para testar o modo MQTT com um mosquitto
# antes de existir o ThingsBoard). No ThingsBoard definitivo, o mesmo JSON de
# estado desejado vira um atributo compartilhado do dispositivo.
MQTT_BRIDGE = os.getenv("MQTT_BRIDGE", "false").lower() in ("1", "true", "sim")
MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
DEVICE_ID = os.getenv("DEVICE_ID", "oiticica-cam-01")


@dataclass
class Contexto:
    """Ganchos para o server.py, injetados sem reescrever nada la."""
    manager: Any
    telemetry: Callable
    detection: Callable
    TelemetryPayload: Any
    DetectionPayload: Any
    history_dir: str
    ler_indice: Callable
    escrever_indice: Callable
    history_lock: Any
    controller_url: str
    extras: dict = field(default_factory=dict)


ctx: Contexto = None


# ============================================================================
# Estado desejado
# ============================================================================
class EstadoDesejado:
    def __init__(self):
        self.lock = threading.Lock()
        self.versao = 1
        self.transporte = "http"
        self.stream_expira = 0.0
        self.inferencia = {"conf": 0.45, "iou": 0.45,
                           "intervalo_frames": 5, "cooldown_s": 5}
        self.pedidos_imagem = []
        # Vindo do Pi, so para exibir no painel
        self.ultimo_visto = 0.0
        self.relatado = {}

    def _snapshot_locked(self):
        restante = max(0.0, self.stream_expira - time.time())
        return {
            "versao": self.versao,
            "transporte": self.transporte,
            "stream": {
                "ativo": restante > 0,
                "restante_s": round(restante, 1),
                "fps": STREAM_FPS,
                "largura": STREAM_LARGURA,
                "qualidade": STREAM_QUALIDADE,
                "anotado": STREAM_ANOTADO,
            },
            "inferencia": dict(self.inferencia),
            "pedidos_imagem": list(self.pedidos_imagem),
        }

    def snapshot(self):
        with self.lock:
            return self._snapshot_locked()

    def mudar(self, **campos):
        with self.lock:
            for k, v in campos.items():
                setattr(self, k, v)
            self.versao += 1
            snap = self._snapshot_locked()
        _empurrar(snap)
        return snap

    def consumir_pedidos(self):
        with self.lock:
            self.pedidos_imagem = []


estado = EstadoDesejado()


def _empurrar(snap):
    """Entrega imediata do estado ao Pi, quando ha caminho para isso.

    E so um acelerador de latencia. O caminho garantido continua sendo a
    carona na resposta do proximo POST de telemetria -- que funciona mesmo
    com o Pi atras de NAT, onde o servidor nao consegue abrir conexao."""
    if estado.transporte == "mqtt" and _mqtt_cli is not None:
        try:
            _mqtt_cli.publish(
                f"v1/devices/{DEVICE_ID}/attributes",
                json.dumps({"estado_desejado": snap}), qos=1,
            )
        except Exception:
            pass
        return
    if not ctx or not ctx.controller_url:
        return

    def _tentar():
        try:
            requests.post(f"{ctx.controller_url}/borda/estado", json=snap, timeout=1.5)
        except Exception:
            pass  # silencioso de proposito: a carona resolve

    threading.Thread(target=_tentar, daemon=True).start()


# ============================================================================
# Ultimo frame recebido (relay para os navegadores)
# ============================================================================
class Quadro:
    def __init__(self):
        self.lock = threading.Lock()
        self.jpeg = None
        self.seq = 0
        self.ts = 0.0
        self.bytes_total = 0
        self.frames_total = 0

    def guardar(self, jpeg):
        with self.lock:
            self.jpeg = jpeg
            self.seq += 1
            self.ts = time.time()
            self.bytes_total += len(jpeg)
            self.frames_total += 1
            return self.seq

    def ler(self):
        with self.lock:
            return self.jpeg, self.seq


quadro = Quadro()
_mqtt_cli = None
_mapa_det = {}   # det_id da borda -> id no historico do servidor


# ============================================================================
# Ponte MQTT do servidor (opcional)
# ============================================================================
def _iniciar_mqtt_bridge(loop):
    global _mqtt_cli
    import asyncio

    import paho.mqtt.client as mqtt

    cli = mqtt.Client(client_id=f"server-{int(time.time())}")
    if MQTT_USER:
        cli.username_pw_set(MQTT_USER, MQTT_PASS)

    def on_connect(c, _u, _f, rc):
        print(f"[mqtt-bridge] conectado (rc={rc})")
        c.subscribe(f"v1/devices/{DEVICE_ID}/telemetry", qos=0)
        c.subscribe(f"oiticica/{DEVICE_ID}/frame", qos=0)
        c.publish(f"v1/devices/{DEVICE_ID}/attributes",
                  json.dumps({"estado_desejado": estado.snapshot()}), qos=1)

    def on_message(_c, _u, msg):
        if msg.topic.endswith("/frame"):
            seq = quadro.guardar(msg.payload)
            asyncio.run_coroutine_threadsafe(
                ctx.manager.broadcast({"type": "frame", "seq": seq}), loop)
            return
        try:
            corpo = json.loads(msg.payload.decode())
        except Exception:
            return
        valores = corpo.get("values", corpo)
        asyncio.run_coroutine_threadsafe(_processar_valores(valores), loop)

    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    cli.loop_start()
    _mqtt_cli = cli
    print(f"[mqtt-bridge] ativa em {MQTT_HOST}:{MQTT_PORT}")


# ============================================================================
# Tradutores: payload da borda -> caminho ja existente do server.py
# ============================================================================
async def _processar_valores(v):
    """Um unico ponto de entrada, use HTTP ou MQTT."""
    if v.get("evt") == "deteccao":
        return await _registrar_deteccao(v)
    if v.get("evt") == "imagem":
        return _anexar_imagem(v)
    return await _registrar_telemetria(v)


async def _registrar_telemetria(v):
    with estado.lock:
        estado.ultimo_visto = time.time()
        estado.relatado = dict(v)
    if "pan" not in v:
        return {"status": "ok"}
    await ctx.telemetry(ctx.TelemetryPayload(
        coord_p=float(v.get("pan", 0.0)),
        coord_t=float(v.get("tilt", 0.0)),
        coord_z=float(v.get("zoom", 0.0)),
        detect=False,
    ))
    return {"status": "ok"}


async def _registrar_deteccao(v):
    resposta = await ctx.detection(ctx.DetectionPayload(
        coord_p=float(v.get("pan", 0.0)),
        coord_t=float(v.get("tilt", 0.0)),
        coord_z=float(v.get("zoom", 0.0)),
        detect=True,
        timestamp=v.get("ts_iso"),
    ))
    corpo = resposta if isinstance(resposta, dict) else {}
    det_servidor = corpo.get("id")
    if det_servidor and v.get("det_id"):
        _mapa_det[v["det_id"]] = det_servidor
        campos = _anotar_historico(det_servidor, v)
        # Sem isto o navegador so via bbox/poly/conf depois de recarregar a
        # pagina (eram gravados no indice em disco, mas nunca via WebSocket):
        # o /api/detection ja tinha respondido, e este anexo chega alguns
        # milissegundos depois, num segundo passo.
        if campos is not None:
            await ctx.manager.broadcast({"type": "detection_update",
                                         "id": det_servidor, **campos})
    return corpo


def _anotar_historico(det_servidor, v):
    """Acrescenta os metadados da borda na entrada do historico. Devolve os
    campos gravados (para o chamador replicar via WebSocket), ou None se o
    id nao existir mais (ex.: historico foi limpo manualmente)."""
    campos = {
        "borda_det_id": v.get("det_id"),
        "n_instancias": v.get("n"),
        "conf_max": v.get("conf_max"),
        "conf_media": v.get("conf_media"),
        "area_px": v.get("area_px"),
        "area_frac": v.get("area_frac"),
        "modelo": v.get("modelo"),
        "limiar": v.get("limiar"),
        "frame_w": v.get("frame_w"),
        "frame_h": v.get("frame_h"),
        "evidencia_local": bool(v.get("evidencia_local")),
    }
    for chave in ("bbox", "poly"):
        bruto = v.get(chave)
        if isinstance(bruto, str):
            try:
                campos[chave] = json.loads(bruto)
            except Exception:
                pass
        elif bruto is not None:
            campos[chave] = bruto

    with ctx.history_lock:
        dados = ctx.ler_indice()
        alvo = next((e for e in dados if e.get("id") == det_servidor), None)
        if alvo is None:
            return None
        alvo.update(campos)
        ctx.escrever_indice(dados)
    return campos


def _anexar_imagem(v):
    """Chegou a evidencia completa pedida pelo operador (ou o aviso de que
    ela nao existe mais no Pi -- ja apagada pela limpeza por teto de disco,
    ou nunca gravada porque a deteccao nao virou alerta novo)."""
    det_servidor = _mapa_det.get(v.get("det_id"))
    if not det_servidor:
        return {"status": "ignorado"}
    if not v.get("img_b64"):
        return {"status": "erro", "id": det_servidor, "erro": v.get("erro") or "sem imagem"}
    nome = f"{det_servidor}_orig.jpg"
    with open(os.path.join(ctx.history_dir, nome), "wb") as f:
        f.write(base64.b64decode(v["img_b64"]))
    with ctx.history_lock:
        dados = ctx.ler_indice()
        alvo = next((e for e in dados if e.get("id") == det_servidor), None)
        if alvo is not None:
            alvo["image"] = nome
            ctx.escrever_indice(dados)
    return {"status": "ok", "id": det_servidor}


# ============================================================================
# Rotas
# ============================================================================
class TransportePayload(BaseModel):
    transporte: str


class InferenciaPayload(BaseModel):
    conf: float | None = None
    iou: float | None = None
    intervalo_frames: int | None = None
    cooldown_s: float | None = None


def instalar(app, contexto: Contexto):
    global ctx
    ctx = contexto

    # ---- subida: Pi -> servidor -------------------------------------------
    @app.post("/api/edge/telemetria")
    async def edge_telemetria(req: Request):
        corpo = await req.json()
        await _registrar_telemetria(corpo.get("values", corpo))
        return {"status": "ok", "estado": estado.snapshot()}

    @app.post("/api/edge/deteccao")
    async def edge_deteccao(req: Request):
        corpo = await req.json()
        r = await _registrar_deteccao(corpo.get("values", corpo))
        estado.consumir_pedidos()
        return {"status": "ok", "detalhe": r, "estado": estado.snapshot()}

    @app.post("/api/edge/imagem")
    async def edge_imagem(req: Request):
        corpo = await req.json()
        r = _anexar_imagem(corpo.get("values", corpo))
        estado.consumir_pedidos()
        if r.get("status") == "ok":
            await ctx.manager.broadcast({"type": "detection_image", "id": r["id"]})
        elif r.get("status") == "erro":
            await ctx.manager.broadcast({"type": "detection_image_erro",
                                         "id": r["id"], "erro": r["erro"]})
        return {"status": "ok", "detalhe": r, "estado": estado.snapshot()}

    @app.post("/api/edge/frame")
    async def edge_frame(req: Request):
        jpeg = await req.body()
        if not jpeg:
            return {"status": "vazio", "estado": estado.snapshot()}
        seq = quadro.guardar(jpeg)
        await ctx.manager.broadcast({"type": "frame", "seq": seq})
        return {"status": "ok", "estado": estado.snapshot()}

    # ---- descida: dashboard -> servidor -> Pi ------------------------------
    @app.post("/api/stream/start")
    def stream_start():
        snap = estado.mudar(stream_expira=time.time() + STREAM_JANELA_S)
        return {"status": "ok", "janela_s": STREAM_JANELA_S, "estado": snap}

    @app.post("/api/stream/renovar")
    def stream_renovar():
        with estado.lock:
            ativo = estado.stream_expira > time.time()
        if not ativo:
            return {"status": "inativo"}
        snap = estado.mudar(stream_expira=time.time() + STREAM_JANELA_S)
        return {"status": "ok", "estado": snap}

    @app.post("/api/stream/stop")
    def stream_stop():
        return {"status": "ok", "estado": estado.mudar(stream_expira=0.0)}

    @app.get("/api/stream/atual.jpg")
    def stream_atual():
        jpeg, _seq = quadro.ler()
        if jpeg is None:
            return Response(status_code=503)
        return Response(content=jpeg, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/transporte")
    def trocar_transporte(p: TransportePayload):
        alvo = p.transporte.strip().lower()
        if alvo not in ("http", "mqtt"):
            return JSONResponse({"error": "use 'http' ou 'mqtt'"}, status_code=400)
        return {"status": "ok", "estado": estado.mudar(transporte=alvo)}

    @app.post("/api/inferencia")
    def ajustar_inferencia(p: InferenciaPayload):
        with estado.lock:
            novo = dict(estado.inferencia)
        for k, v in p.model_dump(exclude_none=True).items():
            novo[k] = v
        return {"status": "ok", "estado": estado.mudar(inferencia=novo)}

    @app.post("/api/detection/{det_id}/pedir_imagem")
    def pedir_imagem(det_id: str):
        """O operador quer a foto cheia de uma deteccao. Ela nunca subiu:
        esta no cartao do Pi. Aqui so registramos o pedido no estado."""
        with estado.lock:
            pedidos = list(estado.pedidos_imagem)
        borda_id = next((b for b, s in _mapa_det.items() if s == det_id), None)
        if borda_id is None:
            return JSONResponse({"error": "sem evidencia na borda"}, status_code=404)
        if borda_id not in pedidos:
            pedidos.append(borda_id)
        return {"status": "ok", "estado": estado.mudar(pedidos_imagem=pedidos)}

    @app.get("/api/borda")
    def painel_borda():
        snap = estado.snapshot()
        with estado.lock:
            visto, relatado = estado.ultimo_visto, dict(estado.relatado)
        with quadro.lock:
            trafego = {"frames": quadro.frames_total, "bytes": quadro.bytes_total}
        return {
            "estado": snap,
            "online": (time.time() - visto) < 5 if visto else False,
            "ultimo_visto_s": round(time.time() - visto, 1) if visto else None,
            "relatado": relatado,
            "stream_trafego": trafego,
            "janela_s": STREAM_JANELA_S,
        }

    @app.on_event("startup")
    async def _startup():
        import asyncio
        if MQTT_BRIDGE:
            _iniciar_mqtt_bridge(asyncio.get_running_loop())
        asyncio.get_running_loop().create_task(_vigia_stream())

    print(f">> Modulo de borda instalado (janela de stream: {STREAM_JANELA_S:.0f}s, "
          f"ponte MQTT: {'on' if MQTT_BRIDGE else 'off'})")


async def _vigia_stream():
    """Avisa o dashboard quando a janela de 60s expira sozinha, para a
    telinha nao ficar congelada no ultimo frame fingindo que esta ao vivo."""
    import asyncio
    anterior = False
    while True:
        await asyncio.sleep(0.5)
        with estado.lock:
            ativo = estado.stream_expira > time.time()
        if anterior and not ativo:
            estado.mudar(stream_expira=0.0)
            await ctx.manager.broadcast({"type": "stream", "ativo": False,
                                         "motivo": "expirou"})
        elif ativo and not anterior:
            await ctx.manager.broadcast({"type": "stream", "ativo": True})
        anterior = ativo

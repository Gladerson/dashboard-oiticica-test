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
#
# Desde a etapa "multi-dispositivo": cada dispositivo autentica por TOKEN
# (header "Authorization: Bearer <token>", gerado ao cadastrar em
# server/dispositivos.py) e tem seu proprio estado/quadro/mapa de deteccoes
# (server/registro_dispositivos.py) -- nao existe mais UM estado global do
# processo. Um token que nao pertence a nenhum dispositivo cadastrado e
# recusado (401): a decisao explicita desta etapa foi nao aceitar
# dispositivo nenhum sem cadastro previo, mesmo que isso exija migrar
# instalacoes ja em producao (ver README).
# ============================================================================
import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

import registro_dispositivos as rd

# --- Ponte MQTT do servidor (opcional; para testar o modo MQTT com um
# mosquitto antes de existir o ThingsBoard). Escopo desta etapa: continua
# UM UNICO dispositivo fixo (DEVICE_ID), do jeito que ja funcionava --
# multi-dispositivo por MQTT fica para quando o ThingsBoard de verdade
# entrar (a autenticacao dele por dispositivo muda esse caminho de
# qualquer jeito). Nao passa pelo registro por token abaixo.
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
    telemetry_core: Callable   # (device_id | None, TelemetryPayload) -> dict
    detection_core: Callable   # (device_id | None, DetectionPayload) -> dict
    TelemetryPayload: Any
    DetectionPayload: Any
    history_dir: str
    ler_indice: Callable
    escrever_indice: Callable
    history_lock: Any
    extras: dict = field(default_factory=dict)


ctx: Contexto = None
_mqtt_cli = None


# ============================================================================
# Dispositivo "legado" da ponte MQTT: um unico slot fixo, para nao depender
# do registro por token (ver comentario no topo do arquivo). Duck-types como
# um DispositivoRuntime o suficiente para os tradutores abaixo (.id/.estado/
# .quadro/.mapa_det) funcionarem sem `if` espalhado.
# ============================================================================
class _DispositivoLegadoMQTT:
    def __init__(self):
        self.id = None
        self.estado = rd.EstadoDesejado(empurrar_para=self._empurrar)
        self.quadro = rd.Quadro()
        self.mapa_det = {}

    def _empurrar(self, snap):
        if _mqtt_cli is None:
            return
        try:
            _mqtt_cli.publish(f"v1/devices/{DEVICE_ID}/attributes",
                              json.dumps({"estado_desejado": snap}), qos=1)
        except Exception:
            pass


_legado_mqtt = _DispositivoLegadoMQTT()


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
                  json.dumps({"estado_desejado": _legado_mqtt.estado.snapshot()}), qos=1)

    def on_message(_c, _u, msg):
        if msg.topic.endswith("/frame"):
            seq = _legado_mqtt.quadro.guardar(msg.payload)
            asyncio.run_coroutine_threadsafe(
                ctx.manager.broadcast({"type": "frame", "device_id": None, "seq": seq}), loop)
            return
        try:
            corpo = json.loads(msg.payload.decode())
        except Exception:
            return
        valores = corpo.get("values", corpo)
        asyncio.run_coroutine_threadsafe(_processar_valores(_legado_mqtt, valores), loop)

    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    cli.loop_start()
    _mqtt_cli = cli
    print(f"[mqtt-bridge] ativa em {MQTT_HOST}:{MQTT_PORT}")


# ============================================================================
# Tradutores: payload da borda -> caminho ja existente do server.py
#
# Recebem `device` (um DispositivoRuntime de verdade, resolvido por token, ou
# o _legado_mqtt acima) -- nunca leem estado global.
# ============================================================================
async def _processar_valores(device, v):
    """Um unico ponto de entrada, use HTTP ou MQTT."""
    if v.get("evt") == "deteccao":
        return await _registrar_deteccao(device, v)
    if v.get("evt") == "imagem":
        return _anexar_imagem(device, v)
    return await _registrar_telemetria(device, v)


async def _registrar_telemetria(device, v):
    with device.estado.lock:
        device.estado.ultimo_visto = time.time()
        device.estado.relatado = dict(v)
    if "pan" not in v:
        return {"status": "ok"}
    await ctx.telemetry_core(device.id, ctx.TelemetryPayload(
        coord_p=float(v.get("pan", 0.0)),
        coord_t=float(v.get("tilt", 0.0)),
        coord_z=float(v.get("zoom", 0.0)),
        detect=False,
    ))
    return {"status": "ok"}


async def _registrar_deteccao(device, v):
    resposta = await ctx.detection_core(device.id, ctx.DetectionPayload(
        coord_p=float(v.get("pan", 0.0)),
        coord_t=float(v.get("tilt", 0.0)),
        coord_z=float(v.get("zoom", 0.0)),
        detect=True,
        timestamp=v.get("ts_iso"),
    ))
    corpo = resposta if isinstance(resposta, dict) else {}
    det_servidor = corpo.get("id")
    if det_servidor and v.get("det_id"):
        device.mapa_det[v["det_id"]] = det_servidor
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
    """Acrescenta os metadados da borda na entrada do historico -- SO na
    primeira vez. Devolve os campos gravados (para o chamador replicar via
    WebSocket), ou None se nao havia nada a anotar (id inexistente, ou
    entrada ja anotada antes).

    Por que so a primeira vez: bbox/poly descrevem UM frame especifico, e o
    Pi so grava em disco (publicar_deteccao, edge/agente_borda.py) o frame
    da PRIMEIRA deteccao de cada alerta -- reincidencias da mesma rachadura
    nao geram evidencia nova. Se cada reincidencia sobrescrevesse bbox/poly
    com a observacao mais recente, a mascara desenhada no dashboard passava
    a descrever um frame diferente do que a foto realmente mostra (posicao
    errada, e a cada nova reincidencia um numero de pontos diferente,
    dependendo do quao bem o crop daquela vez saiu). Nao depende de
    dispositivo -- e so o indice de historico, compartilhado."""
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
        if alvo is None or alvo.get("poly") is not None:
            return None
        alvo.update(campos)
        ctx.escrever_indice(dados)
    return campos


def _det_servidor_de(device, borda_det_id):
    """Traduz o id de deteccao DA BORDA para o id do historico do servidor.

    O mapa em memoria (device.mapa_det) e so um atalho: ele se perde quando
    o servidor reinicia, quando o dispositivo e editado/recadastrado (o
    runtime e remontado) ou quando a localidade e invalidada. Por isso a
    fonte de verdade e o proprio historico, que ja grava 'borda_det_id' em
    _anotar_historico. Sem esta busca, deteccoes antigas viravam
    "fantasmas": o operador clicava em Abrir e a foto nunca chegava, porque
    ninguem mais sabia a qual id da borda elas correspondiam."""
    if not borda_det_id:
        return None
    atalho = device.mapa_det.get(borda_det_id)
    if atalho:
        return atalho
    entrada = next((e for e in ctx.ler_indice()
                    if e.get("borda_det_id") == borda_det_id), None)
    if entrada is None:
        return None
    # Reaquece o atalho para as proximas trocas deste mesmo alerta.
    device.mapa_det[borda_det_id] = entrada["id"]
    return entrada["id"]


def _anexar_imagem(device, v):
    """Chegou a evidencia completa pedida pelo operador (ou o aviso de que
    ela nao existe mais no Pi -- ja apagada pela limpeza por teto de disco,
    ou nunca gravada porque a deteccao nao virou alerta novo)."""
    det_servidor = _det_servidor_de(device, v.get("det_id"))
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
# Autenticacao das rotas do Pi: token do dispositivo, no header Authorization
# ============================================================================
def _resolver_dispositivo(req: Request):
    cabecalho = req.headers.get("authorization", "")
    if not cabecalho.lower().startswith("bearer "):
        return None
    token = cabecalho[7:].strip()
    if not token:
        return None
    return rd.por_token(token)


def _nao_autenticado():
    return JSONResponse({"error": "token de dispositivo ausente ou invalido"},
                        status_code=401)


# ============================================================================
# Rotas
# ============================================================================
class TransportePayload(BaseModel):
    device_id: str
    transporte: str


class InferenciaPayload(BaseModel):
    device_id: str
    conf: float | None = None
    iou: float | None = None
    intervalo_frames: int | None = None
    cooldown_s: float | None = None


def instalar(app, contexto: Contexto):
    global ctx
    ctx = contexto

    # ---- subida: Pi -> servidor (autenticado por token) --------------------
    @app.post("/api/edge/telemetria")
    async def edge_telemetria(req: Request):
        device = _resolver_dispositivo(req)
        if device is None:
            return _nao_autenticado()
        corpo = await req.json()
        await _registrar_telemetria(device, corpo.get("values", corpo))
        return {"status": "ok", "estado": device.estado.snapshot()}

    @app.post("/api/edge/deteccao")
    async def edge_deteccao(req: Request):
        device = _resolver_dispositivo(req)
        if device is None:
            return _nao_autenticado()
        corpo = await req.json()
        r = await _registrar_deteccao(device, corpo.get("values", corpo))
        device.estado.consumir_pedidos()
        return {"status": "ok", "detalhe": r, "estado": device.estado.snapshot()}

    @app.post("/api/edge/imagem")
    async def edge_imagem(req: Request):
        device = _resolver_dispositivo(req)
        if device is None:
            return _nao_autenticado()
        corpo = await req.json()
        r = _anexar_imagem(device, corpo.get("values", corpo))
        device.estado.consumir_pedidos()
        if r.get("status") == "ok":
            await ctx.manager.broadcast({"type": "detection_image", "id": r["id"]})
        elif r.get("status") == "erro":
            await ctx.manager.broadcast({"type": "detection_image_erro",
                                         "id": r["id"], "erro": r["erro"]})
        return {"status": "ok", "detalhe": r, "estado": device.estado.snapshot()}

    @app.post("/api/edge/frame")
    async def edge_frame(req: Request):
        device = _resolver_dispositivo(req)
        if device is None:
            return _nao_autenticado()
        jpeg = await req.body()
        if not jpeg:
            return {"status": "vazio", "estado": device.estado.snapshot()}
        seq = device.quadro.guardar(jpeg)
        await ctx.manager.broadcast({"type": "frame", "device_id": device.id, "seq": seq})
        return {"status": "ok", "estado": device.estado.snapshot()}

    # ---- descida: dashboard -> servidor -> Pi (por device_id) --------------
    @app.post("/api/stream/start")
    def stream_start(device_id: str, largura: str | None = None):
        """'largura' e de quantos pixels o dashboard precisa de fato (o
        painel direito e redimensionavel). Sem ela, vale o padrao do
        servidor."""
        device = rd.por_id(device_id)
        if device is None:
            return JSONResponse({"error": "dispositivo não encontrado"}, status_code=404)
        snap = device.estado.mudar(
            stream_expira=time.time() + rd.STREAM_JANELA_S,
            stream_largura=rd.largura_stream_valida(largura),
        )
        return {"status": "ok", "janela_s": rd.STREAM_JANELA_S, "estado": snap}

    @app.post("/api/stream/renovar")
    def stream_renovar(device_id: str, largura: str | None = None):
        device = rd.por_id(device_id)
        if device is None:
            return JSONResponse({"error": "dispositivo não encontrado"}, status_code=404)
        with device.estado.lock:
            ativo = device.estado.stream_expira > time.time()
            largura_atual = device.estado.stream_largura
        if not ativo:
            return {"status": "inativo"}
        nova = rd.largura_stream_valida(largura)
        campos = {"stream_expira": time.time() + rd.STREAM_JANELA_S}
        # So mexe na largura se ela realmente mudou: cada 'mudar' incrementa
        # a versao do estado e dispara um push pro Pi, e a renovacao roda a
        # cada movimento de PTZ.
        if nova is not None and nova != largura_atual:
            campos["stream_largura"] = nova
        snap = device.estado.mudar(**campos)
        return {"status": "ok", "estado": snap}

    @app.post("/api/stream/stop")
    def stream_stop(device_id: str):
        device = rd.por_id(device_id)
        if device is None:
            return JSONResponse({"error": "dispositivo não encontrado"}, status_code=404)
        return {"status": "ok", "estado": device.estado.mudar(stream_expira=0.0)}

    @app.get("/api/stream/atual.jpg")
    def stream_atual(device_id: str):
        device = rd.por_id(device_id)
        if device is None:
            return Response(status_code=404)
        jpeg, _seq = device.quadro.ler()
        if jpeg is None:
            return Response(status_code=503)
        return Response(content=jpeg, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/transporte")
    def trocar_transporte(p: TransportePayload):
        device = rd.por_id(p.device_id)
        if device is None:
            return JSONResponse({"error": "dispositivo não encontrado"}, status_code=404)
        alvo = p.transporte.strip().lower()
        if alvo not in ("http", "mqtt"):
            return JSONResponse({"error": "use 'http' ou 'mqtt'"}, status_code=400)
        return {"status": "ok", "estado": device.estado.mudar(transporte=alvo)}

    @app.post("/api/inferencia")
    def ajustar_inferencia(p: InferenciaPayload):
        device = rd.por_id(p.device_id)
        if device is None:
            return JSONResponse({"error": "dispositivo não encontrado"}, status_code=404)
        with device.estado.lock:
            novo = dict(device.estado.inferencia)
        for k, v in p.model_dump(exclude={"device_id"}, exclude_none=True).items():
            novo[k] = v
        return {"status": "ok", "estado": device.estado.mudar(inferencia=novo)}

    @app.post("/api/detection/{det_id}/pedir_imagem")
    def pedir_imagem(det_id: str):
        """O operador quer a foto cheia de uma deteccao. Ela nunca subiu:
        esta no cartao do Pi. Aqui so registramos o pedido no estado --
        do MESMO dispositivo que gerou a deteccao (guardado na propria
        entrada do historico, nao precisa o dashboard informar)."""
        entrada = next((e for e in ctx.ler_indice() if e.get("id") == det_id), None)
        if entrada is None:
            return JSONResponse({"error": "detecção não encontrada"}, status_code=404)
        device = rd.por_id(entrada.get("device_id")) if entrada.get("device_id") else None
        if device is None:
            return JSONResponse(
                {"error": "dispositivo desta detecção não encontrado (cadastro "
                          "anterior à etapa multi-dispositivo, ou excluído)"},
                status_code=404)
        # O id da borda vem da PROPRIA entrada do historico (gravado em
        # _anotar_historico). Antes isto era uma busca reversa no mapa em
        # memoria, que se perde a cada reinicio/edicao do dispositivo -- e
        # a deteccao virava um "fantasma" preso em "Solicitando imagem".
        borda_id = entrada.get("borda_det_id")
        if not borda_id:
            borda_id = next((b for b, s in device.mapa_det.items() if s == det_id), None)
        if not borda_id:
            return JSONResponse(
                {"error": "esta detecção não tem evidência guardada na borda "
                          "(o Raspberry só grava a foto do primeiro alerta de "
                          "cada rachadura)"},
                status_code=404)
        with device.estado.lock:
            pedidos = list(device.estado.pedidos_imagem)
        if borda_id not in pedidos:
            pedidos.append(borda_id)
        return {"status": "ok", "estado": device.estado.mudar(pedidos_imagem=pedidos)}

    @app.get("/api/borda")
    def painel_borda(device_id: str):
        device = rd.por_id(device_id)
        if device is None:
            return JSONResponse({"error": "dispositivo não encontrado"}, status_code=404)
        snap = device.estado.snapshot()
        with device.estado.lock:
            visto, relatado = device.estado.ultimo_visto, dict(device.estado.relatado)
        with device.quadro.lock:
            trafego = {"frames": device.quadro.frames_total, "bytes": device.quadro.bytes_total}
        return {
            "estado": snap,
            "online": (time.time() - visto) < 5 if visto else False,
            "ultimo_visto_s": round(time.time() - visto, 1) if visto else None,
            "relatado": relatado,
            "stream_trafego": trafego,
            "janela_s": rd.STREAM_JANELA_S,
            "pronto_3d": device.pronto(),
        }

    @app.on_event("startup")
    async def _startup():
        import asyncio
        if MQTT_BRIDGE:
            _iniciar_mqtt_bridge(asyncio.get_running_loop())
        asyncio.get_running_loop().create_task(_vigia_streams())

    print(f">> Modulo de borda instalado (janela de stream: {rd.STREAM_JANELA_S:.0f}s, "
          f"ponte MQTT: {'on' if MQTT_BRIDGE else 'off'}, autenticacao por token: on)")


async def _vigia_streams():
    """Avisa o dashboard quando a janela de 60s expira sozinha, para a
    telinha nao ficar congelada no ultimo frame fingindo que esta ao vivo.
    Percorre todos os dispositivos ja vistos pelo registro (nao so um)."""
    import asyncio
    anteriores = {}
    while True:
        await asyncio.sleep(0.5)
        dispositivos = rd.todos() + [_legado_mqtt]
        for device in dispositivos:
            with device.estado.lock:
                ativo = device.estado.stream_expira > time.time()
            anterior = anteriores.get(device.id, False)
            if anterior and not ativo:
                device.estado.mudar(stream_expira=0.0)
                await ctx.manager.broadcast({"type": "stream", "device_id": device.id,
                                             "ativo": False, "motivo": "expirou"})
            elif ativo and not anterior:
                await ctx.manager.broadcast({"type": "stream", "device_id": device.id,
                                             "ativo": True})
            anteriores[device.id] = ativo

# ============================================================================
# server.py - Servidor do dashboard
#
# Novidades desta versão:
#   • Multi-dispositivo: nao existe mais UM GeoModel/camera fixos no
#     processo. Cada dispositivo cadastrado (server/dispositivos.py) tem seu
#     proprio GeoModel (via a localidade) e sua propria pose de camera,
#     resolvidos sob demanda por server/registro_dispositivos.py. Rotas que
#     antes nao precisavam de nada agora recebem device_id (aim/locate
#     resolvem pelo device_id GRAVADO NA PROPRIA DETECCAO, nao pedem pro
#     cliente -- mais dificil de usar o dispositivo errado por engano).
#   • /api/telemetry e /api/detection (usadas pelo controller.py, o ambiente
#     de desktop sem Pi -- README §3) agora tambem exigem token de
#     dispositivo (header Authorization: Bearer), a mesma autenticacao que
#     o agente de borda usa em /api/edge/*.
#   • O payload de telemetria inclui o "footprint" do cone: o contorno real
#     onde o campo de visão encosta na parede (raycasting em leque).
#   • /api/aim  -> recebe um ponto 3D + os cantos do retângulo desenhado no
#     dashboard e converte em pan/tilt/zoom reais, mandando a câmera pra lá.
#   • Proxy PTZ (/api/ptz/*) mantido como fallback; o dashboard prefere falar
#     direto com o controller.
#   • O trabalho pesado de raycasting roda em threadpool, sem travar o loop
#     assíncrono do FastAPI.
# ============================================================================
import base64
import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import requests
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

# --- Configuracao via server/.env -------------------------------------------
# Precisa vir ANTES do import do glb_geo: ele le PAN_SIGN/TILT_SIGN na hora
# em que e importado.
from pathlib import Path as _Path  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_Path(__file__).parent / ".env")

from glb_geo import PAN_SIGN, TILT_SIGN  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
import dispositivos  # noqa: E402
import registro_dispositivos as registro  # noqa: E402

HISTORY_DIR = "history"
HISTORY_INDEX = os.path.join(HISTORY_DIR, "index.json")

# --- Deduplicacao / ciclo de vida das deteccoes -----------------------------
# Subpasta (dentro de history/) onde vao as imagens marcadas como falso
# positivo, separadas para retreino/refino do modelo.
FALSOS_SUBDIR = "falsos_positivos"
FALSOS_DIR = os.path.join(HISTORY_DIR, FALSOS_SUBDIR)

# Raio, em METROS REAIS do modelo, dentro do qual duas deteccoes DO MESMO
# DISPOSITIVO sao consideradas a MESMA rachadura. Comparacao feita no ponto
# 3D de impacto calculado por raycasting -- nao e aproximacao angular.
# Dispositivos diferentes nunca deduplicam entre si (ver detection_core).
DEDUP_RAIO_M = float(os.getenv("DEDUP_RAIO_M", "1.5"))

# Fallback usado SO quando nenhuma das duas deteccoes intercepta a malha
# (nao ha ponto 3D para comparar): tolerancia angular em graus.
DEDUP_ANG_DEG = float(os.getenv("DEDUP_ANG_DEG", "1.5"))

# Depois de "Reconhecer"/"Falso positivo", o mesmo ponto fica em rearme por
# este tempo. Passado ele, uma nova deteccao no mesmo lugar volta a abrir
# alerta -- e exatamente o sinal de que o problema nao foi resolvido.
# Coloque 0 para permitir reabertura imediata.
REARME_SEGUNDOS = float(os.getenv("REARME_SEGUNDOS", "600"))

os.makedirs(HISTORY_DIR, exist_ok=True)
os.makedirs(FALSOS_DIR, exist_ok=True)
if not os.path.exists(HISTORY_INDEX):
    with open(HISTORY_INDEX, "w") as f:
        json.dump([], f)

# ----------------------------------------------------------------------------
# Indice do historico: leitura/escrita seguras
# ----------------------------------------------------------------------------
_history_lock = threading.Lock()


def _ler_indice():
    try:
        with open(HISTORY_INDEX, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _escrever_indice(dados):
    """Escrita atomica: grava num temporario no MESMO diretorio e troca com
    os.replace(). Se o processo morrer no meio, o index.json antigo continua
    intacto em vez de ficar truncado."""
    tmp = HISTORY_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, HISTORY_INDEX)


def _agora():
    return datetime.now(timezone.utc)


def _parse_ts(texto):
    if not texto:
        return None
    try:
        dt = datetime.fromisoformat(texto)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _delta_angular(a, b):
    """Diferenca entre dois angulos em graus, normalizada para [-180, 180]
    (evita que -179 e +179 sejam tratados como 358 graus de distancia)."""
    d = (float(a) - float(b) + 180.0) % 360.0 - 180.0
    return abs(d)


def _mesmo_ponto(entrada, hit_point, pan, tilt):
    """True se 'entrada' aponta para o mesmo lugar fisico da nova deteccao.

    Prioridade absoluta para a distancia 3D real (metros) entre os pontos de
    impacto calculados por raycasting contra a malha. A comparacao angular so
    entra quando NENHUM dos dois intercepta o modelo -- ai nao existe ponto
    3D para comparar. So e chamada entre deteccoes do MESMO dispositivo (ver
    detection_core) -- dois dispositivos podem, por coincidencia, ter pan/
    tilt parecidos sem ter nada a ver um com o outro."""
    hp = entrada.get("hit_point")
    if hit_point is not None and hp is not None:
        d = float(np.linalg.norm(np.asarray(hp, dtype=float)
                                 - np.asarray(hit_point, dtype=float)))
        return d <= DEDUP_RAIO_M
    if hit_point is None and hp is None:
        return (_delta_angular(entrada.get("coord_p", 0.0), pan) <= DEDUP_ANG_DEG
                and _delta_angular(entrada.get("coord_t", 0.0), tilt) <= DEDUP_ANG_DEG)
    return False


def _status(entrada):
    return entrada.get("status") or "pendente"


def _utm_de(device, hit_point):
    """Coordenadas UTM reais do ponto 3D de impacto (raycasting), para
    mostrar no dashboard -- e a "coordenada da deteccao" que o Pi nunca
    calcula: ele so manda pan/tilt/zoom, o server e quem sabe a geometria
    do modelo (via a localidade do dispositivo) e converte para o mundo
    real."""
    if hit_point is None or device is None or device.geo is None:
        return None
    utm_x, utm_y, alt = device.geo.local_to_utm(hit_point)
    return {
        "zona": device.geo.utm_zone, "hemisferio_sul": device.geo.utm_hemisferio_sul,
        "x": round(utm_x, 2), "y": round(utm_y, 2), "alt": round(alt, 2),
    }


def _modelo_3d_url(caminho):
    """Converte o caminho em disco do .glb (sempre "static/...", ver
    dispositivos.py e migrar_dispositivo_legado.py) na URL servida pelo
    mount StaticFiles("/model" -> "static/"). None quando a localidade nao
    tem modelo pronto ainda."""
    if not caminho:
        return None
    p = caminho.replace(os.sep, "/")
    if p.startswith("static/"):
        p = p[len("static/"):]
    return f"/model/{p}"


http = requests.Session()

app = FastAPI(title="Dashboard Server")
app.mount("/model", StaticFiles(directory="static"), name="model")
# Mesma pasta do mount acima, com um nome que descreve o que serve: as
# telas puxam /estatico/layout.css e /estatico/layout.js. ("/model" ficou
# com esse nome de quando so servia o .glb.)
app.mount("/estatico", StaticFiles(directory="static"), name="estatico")
app.mount("/history_files", StaticFiles(directory=HISTORY_DIR), name="history_files")

db.iniciar()
auth.instalar(app)
dispositivos.instalar(app)


# ----------------------------------------------------------------------------
def _resolver_dispositivo_http(request: Request):
    """Mesma autenticacao por token do agente de borda (server/borda.py),
    usada aqui por quem fala com /api/telemetry e /api/detection
    DIRETAMENTE por HTTP -- hoje, so o controller.py (README §3)."""
    cabecalho = request.headers.get("authorization", "")
    if not cabecalho.lower().startswith("bearer "):
        return None
    token = cabecalho[7:].strip()
    if not token:
        return None
    return registro.por_token(token)


# ----------------------------------------------------------------------------
class TelemetryPayload(BaseModel):
    coord_p: float
    coord_t: float
    coord_z: float
    detect: bool = False


class DetectionPayload(TelemetryPayload):
    timestamp: str | None = None
    # Preenchidos SO pelo controller.py (ambiente de desktop sem Pi, ver
    # README §3): ele nao tem o mecanismo de "evidencia sob demanda" e manda
    # a imagem junto com a deteccao mesmo. O agente de borda (edge/) nunca
    # preenche isto -- a deteccao dele carrega so coordenadas, e a imagem
    # completa e pedida depois via /api/detection/{id}/pedir_imagem.
    image_b64: str | None = None
    mask_image_b64: str | None = None


class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


@app.get("/")
def index():
    return FileResponse("static/dashboard.html")


@app.get("/api/camera_info")
def camera_info(device_id: str):
    device = registro.por_id(device_id)
    if device is None:
        return JSONResponse({"error": "dispositivo não encontrado"}, status_code=404)
    pronto = device.pronto()
    # Quando nao esta pronto, diz exatamente QUAL condicao falhou -- le do
    # banco (nao do runtime) porque o cadastro pode ter mudado depois que o
    # runtime foi montado. So custa uma consulta no caso ruim.
    motivo = None
    if not pronto:
        linha = db.dispositivo_por_id_com_localidade(device_id)
        motivo = dispositivos.motivo_sem_3d(linha) if linha else None
        if linha is not None and motivo is None:
            # O banco diz que esta tudo certo e mesmo assim o runtime nao
            # tem geometria: e um runtime velho, montado antes de o cadastro
            # (ou o modelo) ficar pronto. Remonta uma vez e reavalia, em vez
            # de mostrar "sem modelo 3D" sem motivo nenhum ate o servidor
            # reiniciar. As invalidacoes em dispositivos.py ja cobrem os
            # caminhos normais; isto e a rede de seguranca.
            device = registro.recarregar(device_id) or device
            pronto = device.pronto()
            if not pronto:
                motivo = ("o modelo 3D da localidade não pôde ser carregado "
                          "no servidor; veja os avisos no log do servidor")
    return {
        "motivo_sem_3d": motivo,
        "camera_local_pos": device.camera_local_pos.tolist() if pronto else None,
        "base_forward": device.base_forward.tolist() if pronto else None,
        "lat": device.lat,
        "lon": device.lon,
        "alt": device.alt_acima_solo,
        "controller_url": device.controller_url_publica,
        "modelo_3d_url": _modelo_3d_url(device.localidade_modelo_3d_path),
        "cone_ring_rays": registro.CONE_RING_RAYS,
        "half_angle_wide": registro.CONE_HALF_ANGLE_WIDE,
        "half_angle_tele": registro.CONE_HALF_ANGLE_TELE,
        "model_up_axis": device.geo.model_up_axis if pronto else None,
        "pan_sign": PAN_SIGN,
        "tilt_sign": TILT_SIGN,
        "pronto_3d": pronto,
    }


@app.get("/api/view")
async def view(device_id: str, pan: float = 0.0, tilt: float = 0.0, zoom: float = 0.0):
    """Estado do cone sob demanda (usado na abertura do dashboard)."""
    device = registro.por_id(device_id)
    if device is None:
        return JSONResponse({"error": "dispositivo não encontrado"}, status_code=404)
    result = await run_in_threadpool(device.compute_view, pan, tilt, zoom)
    return {"coord_p": pan, "coord_t": tilt, "coord_z": zoom, **result}


async def telemetry_core(device_id, payload: TelemetryPayload):
    """Nucleo compartilhado por HTTP (telemetry_route/borda.py) e pela ponte
    MQTT legada (device_id=None nesse caso -- ver borda.py)."""
    device = registro.por_id(device_id) if device_id else None
    if device is not None:
        result = await run_in_threadpool(
            device.compute_view, payload.coord_p, payload.coord_t, payload.coord_z)
    else:
        result = {"hit_point": None, "cone": None}
    msg = {
        "type": "telemetry",
        "device_id": device_id,
        "coord_p": payload.coord_p,
        "coord_t": payload.coord_t,
        "coord_z": payload.coord_z,
        **result,
    }
    await manager.broadcast(msg)
    return {"status": "ok", "hit_point": result["hit_point"]}


@app.post("/api/telemetry")
async def telemetry_route(payload: TelemetryPayload, request: Request):
    """So quem fala HTTP direto (controller.py) passa por aqui -- o agente
    de borda entra por /api/edge/telemetria (server/borda.py), que ja
    autentica e chama telemetry_core diretamente."""
    device = _resolver_dispositivo_http(request)
    if device is None:
        return JSONResponse({"error": "token de dispositivo ausente ou inválido"},
                            status_code=401)
    return await telemetry_core(device.id, payload)


async def detection_core(device_id, payload: DetectionPayload):
    """Registra uma deteccao NOVA.

    Se a camera continuar apontando para uma rachadura ja alertada e ainda
    nao tratada, nao criamos outro alerta: apenas incrementamos o contador de
    reincidencia da entrada existente. Isso evita a enxurrada de alertas
    identicos e tambem o crescimento inutil do disco com fotos repetidas.

    A deduplicacao (vizinhas) so compara deteccoes do MESMO device_id --
    inclusive o "bucket" None (controller.py sem device_id valido nunca
    deveria acontecer hoje, mas None==None mantem essas juntas e separadas
    de qualquer dispositivo de verdade, ver _mesmo_ponto).
    """
    device = registro.por_id(device_id) if device_id else None
    if device is not None:
        result = await run_in_threadpool(
            device.compute_view, payload.coord_p, payload.coord_t, payload.coord_z)
    else:
        result = {"hit_point": None, "cone": None}
    hit = result["hit_point"]
    agora = _agora()
    ts = payload.timestamp or agora.isoformat()

    evento = None
    entrada_nova = None

    with _history_lock:
        dados = _ler_indice()
        vizinhas = [e for e in dados
                    if e.get("device_id") == device_id
                    and _mesmo_ponto(e, hit, payload.coord_p, payload.coord_t)]

        # (1) Ja existe alerta ABERTO nesse ponto -> nao duplica.
        pendente = next((e for e in vizinhas if _status(e) == "pendente"), None)

        # (2) Ponto tratado ha pouco -> periodo de rearme.
        em_rearme = None
        if pendente is None and REARME_SEGUNDOS > 0:
            limite = agora - timedelta(seconds=REARME_SEGUNDOS)
            for e in vizinhas:
                dt = _parse_ts(e.get("resolvido_em"))
                if dt is not None and dt > limite:
                    em_rearme = e
                    break

        if pendente is not None:
            pendente["ultima_vez"] = ts
            pendente["repeticoes"] = int(pendente.get("repeticoes", 1)) + 1
            _escrever_indice(dados)
            evento = {
                "type": "detection_repeat",
                "id": pendente["id"],
                "ultima_vez": ts,
                "repeticoes": pendente["repeticoes"],
            }
            resposta = {"status": "duplicada", "id": pendente["id"], "hit_point": hit}

        elif em_rearme is not None:
            resposta = {"status": "em_rearme", "id": em_rearme["id"], "hit_point": hit}

        else:
            det_id = str(uuid.uuid4())

            # So o controller.py (desktop, sem Pi -- README §3) preenche
            # isto: ele nao tem "evidencia sob demanda" e manda a imagem
            # junto. O agente de borda nunca manda image_b64/mask_image_b64
            # aqui -- "image" fica None ate o operador clicar em "Abrir" e
            # o Pi responder via /api/detection/{id}/pedir_imagem
            # (borda._anexar_imagem preenche depois).
            image_path = None
            mask_path = None
            if payload.image_b64:
                image_path = f"{det_id}_orig.jpg"
                with open(os.path.join(HISTORY_DIR, image_path), "wb") as f:
                    f.write(base64.b64decode(payload.image_b64))
            if payload.mask_image_b64:
                mask_path = f"{det_id}_mask.jpg"
                with open(os.path.join(HISTORY_DIR, mask_path), "wb") as f:
                    f.write(base64.b64decode(payload.mask_image_b64))

            # Reincidencia num ponto ja julgado como falso positivo: a
            # marcacao 3D nasce com cor diferente no dashboard.
            reincide_fp = any(_status(e) == "falso_positivo" for e in vizinhas)

            entrada_nova = {
                "id": det_id,
                "device_id": device_id,
                "timestamp": ts,
                "ultima_vez": ts,
                "repeticoes": 1,
                "coord_p": payload.coord_p,
                "coord_t": payload.coord_t,
                "coord_z": payload.coord_z,
                "hit_point": hit,
                "utm": _utm_de(device, hit),
                "image": image_path,
                "mask_image": mask_path,
                "status": "pendente",
                "acao": None,
                "resolvido_em": None,
                "reincide_falso_positivo": reincide_fp,
                # Para deteccoes vindas do Pi, borda._anotar_historico pisa
                # em cima disto com True logo em seguida (e replica via
                # WebSocket) -- e o que libera o "Abrir" a pedir a foto sem
                # precisar recarregar a pagina.
            }
            dados.insert(0, entrada_nova)
            _escrever_indice(dados)
            evento = {"type": "detection", "cone": result["cone"], **entrada_nova}
            resposta = {"status": "ok", "id": det_id, "hit_point": hit}

    if evento is not None:
        await manager.broadcast(evento)
    return resposta


@app.post("/api/detection")
async def detection_route(payload: DetectionPayload, request: Request):
    device = _resolver_dispositivo_http(request)
    if device is None:
        return JSONResponse({"error": "token de dispositivo ausente ou inválido"},
                            status_code=401)
    return await detection_core(device.id, payload)


@app.get("/api/history")
def history():
    return JSONResponse(_ler_indice())


# ----------------------------------------------------------------------------
# Tratamento do alerta: reconhecer ou marcar como falso positivo
# ----------------------------------------------------------------------------
class ReconhecerPayload(BaseModel):
    acao: str


def _resolver(det_id, mutacao):
    """Aplica 'mutacao' na entrada e grava o indice, tudo sob o mesmo lock.
    Retorna a entrada ja atualizada (copia) ou None se o id nao existir."""
    with _history_lock:
        dados = _ler_indice()
        entrada = next((e for e in dados if e.get("id") == det_id), None)
        if entrada is None:
            return None
        mutacao(entrada)
        entrada["resolvido_em"] = _agora().isoformat()
        _escrever_indice(dados)
        return dict(entrada)


@app.post("/api/detection/{det_id}/reconhecer")
async def reconhecer(det_id: str, payload: ReconhecerPayload):
    acao = (payload.acao or "").strip()
    if not acao:
        return JSONResponse({"error": "descreva a acao tomada"}, status_code=400)

    def muta(e):
        e["status"] = "reconhecida"
        e["acao"] = acao

    entrada = _resolver(det_id, muta)
    if entrada is None:
        return JSONResponse({"error": "deteccao nao encontrada"}, status_code=404)

    await manager.broadcast({"type": "detection_update", **entrada})
    return {"status": "ok", **entrada}


@app.post("/api/detection/{det_id}/falso_positivo")
async def falso_positivo(det_id: str):
    """Marca como falso positivo e MOVE as imagens para history/falsos_positivos/,
    separando o material para refino posterior do modelo."""
    def muta(e):
        for campo in ("image", "mask_image"):
            nome = e.get(campo)
            if not nome or nome.startswith(FALSOS_SUBDIR + "/"):
                continue
            origem = os.path.join(HISTORY_DIR, nome)
            destino_rel = f"{FALSOS_SUBDIR}/{os.path.basename(nome)}"
            destino = os.path.join(HISTORY_DIR, destino_rel)
            if os.path.exists(origem):
                shutil.move(origem, destino)
                e[campo] = destino_rel
        e["status"] = "falso_positivo"
        e["acao"] = None

    entrada = _resolver(det_id, muta)
    if entrada is None:
        return JSONResponse({"error": "deteccao nao encontrada"}, status_code=404)

    await manager.broadcast({"type": "detection_update", **entrada})
    return {"status": "ok", **entrada}


# ----------------------------------------------------------------------------
# Mirar num ponto do modelo 3D (shift + arrastar no dashboard)
# ----------------------------------------------------------------------------
class AimPayload(BaseModel):
    device_id: str
    point: list[float]                 # ponto 3D local (coords "cruas" do .glb)
    corners: list[list[float]] = []    # cantos do retângulo, também locais
    margin: float = 1.30               # folga para o objeto não ficar colado na borda
    apply: bool = True                 # False = só calcula, não move a câmera


@app.post("/api/aim")
def aim(payload: AimPayload):
    device = registro.por_id(payload.device_id)
    if device is None:
        return JSONResponse({"error": "dispositivo não encontrado"}, status_code=404)
    if not device.pronto():
        return JSONResponse({"error": "dispositivo sem modelo 3D pronto"}, status_code=400)

    alvo = np.asarray(payload.point, dtype=float)
    direcao = alvo - device.camera_local_pos
    if np.linalg.norm(direcao) < 1e-6:
        return JSONResponse({"error": "ponto coincide com a câmera"}, status_code=400)

    pan_deg, tilt_deg = device.geo.direction_to_pan_tilt(device.base_forward, direcao)

    # Meio-ângulo necessário: maior desvio angular entre o centro e os cantos
    maior_angulo = 0.0
    for c in payload.corners:
        v = np.asarray(c, dtype=float) - device.camera_local_pos
        if np.linalg.norm(v) < 1e-6:
            continue
        maior_angulo = max(maior_angulo, device.geo.angle_between(direcao, v))

    if maior_angulo <= 0.05:
        # Retângulo minúsculo ou nenhum canto acertou o modelo: aproxima bem.
        half = registro.CONE_HALF_ANGLE_TELE * 1.5
    else:
        half = maior_angulo * float(payload.margin)

    half = max(registro.CONE_HALF_ANGLE_TELE, min(registro.CONE_HALF_ANGLE_WIDE, half))
    zoom_pct = registro.zoom_for_half_angle(half)

    resultado = {
        "coord_p": round(pan_deg, 2),
        "coord_t": round(tilt_deg, 2),
        "coord_z": round(zoom_pct, 1),
        "half_angle_deg": round(half, 2),
        "distance": float(np.linalg.norm(direcao)),
    }

    if not payload.apply:
        return resultado

    try:
        r = http.post(
            f"{device.controller_url}/command/absolute",
            json={
                "pan_deg": resultado["coord_p"],
                "tilt_deg": resultado["coord_t"],
                "zoom_pct": resultado["coord_z"],
            },
            timeout=6,
        )
        resultado["controller"] = r.json()
    except Exception as e:
        return JSONResponse({"error": str(e), **resultado}, status_code=502)

    return resultado


# ----------------------------------------------------------------------------
# Localizar uma detecção do histórico
# ----------------------------------------------------------------------------
class LocatePayload(BaseModel):
    id: str
    move_camera: bool = True


@app.post("/api/locate")
async def locate(payload: LocatePayload):
    with open(HISTORY_INDEX) as f:
        entradas = json.load(f)

    entrada = next((e for e in entradas if e.get("id") == payload.id), None)
    if entrada is None:
        return JSONResponse({"error": "detecção não encontrada"}, status_code=404)

    # O dispositivo vem da PROPRIA entrada (gravado quando a deteccao
    # chegou) -- nao do cliente, pra nunca recalcular com a geometria do
    # dispositivo errado.
    device = registro.por_id(entrada.get("device_id")) if entrada.get("device_id") else None
    if device is None:
        return JSONResponse(
            {"error": "dispositivo desta detecção não encontrado (cadastro "
                      "anterior à etapa multi-dispositivo, ou excluído)"},
            status_code=404)

    # Recalcula a geometria a partir do pan/tilt/zoom gravados. Isso corrige
    # automaticamente entradas antigas, salvas antes do ajuste de PAN_SIGN.
    result = await run_in_threadpool(
        device.compute_view, entrada["coord_p"], entrada["coord_t"], entrada["coord_z"]
    )

    resposta = {
        "id": entrada["id"],
        "coord_p": entrada["coord_p"],
        "coord_t": entrada["coord_t"],
        "coord_z": entrada["coord_z"],
        "timestamp": entrada.get("timestamp"),
        "hit_point": result["hit_point"],
        "cone": result["cone"],
        "moved": False,
    }

    if not payload.move_camera:
        return resposta

    try:
        r = http.post(
            f"{device.controller_url}/command/absolute",
            json={
                "pan_deg": entrada["coord_p"],
                "tilt_deg": entrada["coord_t"],
                "zoom_pct": entrada["coord_z"],
            },
            timeout=6,
        )
        resposta["controller"] = r.json()
        resposta["moved"] = True
    except Exception as e:
        resposta["error_camera"] = str(e)

    return resposta

# ----------------------------------------------------------------------------
# Proxy PTZ (fallback: o dashboard prefere falar direto com o controller)
# ----------------------------------------------------------------------------
class CommandPayload(BaseModel):
    device_id: str
    pan_delta: float = 0.0
    tilt_delta: float = 0.0
    zoom_delta: float = 0.0


class ContinuousPayload(BaseModel):
    device_id: str
    pan_speed: float = 0.0
    tilt_speed: float = 0.0
    zoom_speed: float = 0.0
    hold_ms: int = 800


def _proxy(controller_url, path, body=None):
    try:
        r = http.post(f"{controller_url}{path}", json=body, timeout=5)
        return r.json()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/command")
def send_command(cmd: CommandPayload):
    device = registro.por_id(cmd.device_id)
    if device is None:
        return JSONResponse({"error": "dispositivo não encontrado"}, status_code=404)
    return _proxy(device.controller_url, "/command", cmd.model_dump(exclude={"device_id"}))


@app.post("/api/ptz/continuous")
def ptz_continuous(cmd: ContinuousPayload):
    device = registro.por_id(cmd.device_id)
    if device is None:
        return JSONResponse({"error": "dispositivo não encontrado"}, status_code=404)
    return _proxy(device.controller_url, "/command/continuous",
                 cmd.model_dump(exclude={"device_id"}))


@app.post("/api/ptz/stop")
def ptz_stop(device_id: str):
    device = registro.por_id(device_id)
    if device is None:
        return JSONResponse({"error": "dispositivo não encontrado"}, status_code=404)
    return _proxy(device.controller_url, "/command/stop", {})


@app.post("/api/command/home")
def send_home(device_id: str):
    device = registro.por_id(device_id)
    if device is None:
        return JSONResponse({"error": "dispositivo não encontrado"}, status_code=404)
    return _proxy(device.controller_url, "/command/home", {})


# ----------------------------------------------------------------------------
# Modo borda: o Raspberry infere localmente e manda so metadados.
# ----------------------------------------------------------------------------
import borda  # noqa: E402

borda.instalar(app, borda.Contexto(
    manager=manager,
    telemetry_core=telemetry_core,
    detection_core=detection_core,
    TelemetryPayload=TelemetryPayload,
    DetectionPayload=DetectionPayload,
    history_dir=HISTORY_DIR,
    ler_indice=_ler_indice,
    escrever_indice=_escrever_indice,
    history_lock=_history_lock,
))


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    # A middleware HTTP de auth.py NAO cobre websocket (e outro scope ASGI);
    # o cookie de sessao chega junto do handshake mesmo assim, entao
    # validamos aqui, ANTES do accept().
    usuario = db.usuario_da_sessao(websocket.cookies.get(auth.COOKIE_NOME))
    if usuario is None:
        await websocket.close(code=4401)
        return
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)

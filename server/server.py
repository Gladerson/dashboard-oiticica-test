# ============================================================================
# server.py - Servidor do dashboard
#
# Novidades desta versão:
#   • O payload de telemetria agora inclui o "footprint" do cone: o contorno
#     real onde o campo de visão encosta na parede (raycasting em leque).
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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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

from glb_geo import (  # noqa: E402
    GeoModel, MODEL_UP_AXIS, PAN_SIGN, TILT_SIGN,
    UTM_HEMISPHERE_SOUTH, UTM_ZONE,
)

# --- Posição real da câmera -------------------------------------------------
CAMERA_LAT = -6.152425824994227
CAMERA_LON = -37.12619639369007
CAMERA_ALT_ABOVE_GROUND = 7.0  # metros

# URL que o SERVER usa para falar com o controller
CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://127.0.0.1:8090")
# URL que o NAVEGADOR usa para falar com o controller (troque pelo IP do
# Raspberry quando o dashboard for acessado de outra máquina)
CONTROLLER_PUBLIC_URL = os.getenv("CONTROLLER_PUBLIC_URL", CONTROLLER_URL)

HISTORY_DIR = "history"
HISTORY_INDEX = os.path.join(HISTORY_DIR, "index.json")

# --- Deduplicacao / ciclo de vida das deteccoes -----------------------------
# Subpasta (dentro de history/) onde vao as imagens marcadas como falso
# positivo, separadas para retreino/refino do modelo.
FALSOS_SUBDIR = "falsos_positivos"
FALSOS_DIR = os.path.join(HISTORY_DIR, FALSOS_SUBDIR)

# Raio, em METROS REAIS do modelo, dentro do qual duas deteccoes sao
# consideradas a MESMA rachadura. Comparacao feita no ponto 3D de impacto
# calculado por raycasting -- nao e aproximacao angular.
DEDUP_RAIO_M = float(os.getenv("DEDUP_RAIO_M", "1.5"))

# Fallback usado SO quando nenhuma das duas deteccoes intercepta a malha
# (nao ha ponto 3D para comparar): tolerancia angular em graus.
DEDUP_ANG_DEG = float(os.getenv("DEDUP_ANG_DEG", "1.5"))

# Depois de "Reconhecer"/"Falso positivo", o mesmo ponto fica em rearme por
# este tempo. Passado ele, uma nova deteccao no mesmo lugar volta a abrir
# alerta -- e exatamente o sinal de que o problema nao foi resolvido.
# Coloque 0 para permitir reabertura imediata.
REARME_SEGUNDOS = float(os.getenv("REARME_SEGUNDOS", "600"))

# --- Parâmetros do cone -----------------------------------------------------
CONE_HALF_ANGLE_WIDE = float(os.getenv("CONE_HALF_ANGLE_WIDE", "18.0"))  # zoom 0%
CONE_HALF_ANGLE_TELE = float(os.getenv("CONE_HALF_ANGLE_TELE", "2.0"))   # zoom 100%
CONE_RING_RAYS = int(os.getenv("CONE_RING_RAYS", "24"))
CONE_MAX_RANGE = float(os.getenv("CONE_MAX_RANGE", "250.0"))

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
    3D para comparar."""
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


def _utm_de(hit_point):
    """Coordenadas UTM reais do ponto 3D de impacto (raycasting), para
    mostrar no dashboard -- e a "coordenada da deteccao" que o Pi nunca
    calcula: ele so manda pan/tilt/zoom, o server e quem sabe a geometria
    do modelo e converte para o mundo real."""
    if hit_point is None:
        return None
    utm_x, utm_y, alt = geo.local_to_utm(hit_point)
    return {
        "zona": UTM_ZONE, "hemisferio_sul": UTM_HEMISPHERE_SOUTH,
        "x": round(utm_x, 2), "y": round(utm_y, 2), "alt": round(alt, 2),
    }

http = requests.Session()

# --- Carrega o modelo real e calibra a direção base -------------------------
geo = GeoModel()

_bounds = geo.mesh.bounds
print(f">> Bounding box real do modelo (.glb): min={_bounds[0]} max={_bounds[1]}")

local_x, local_y = geo.latlon_to_local_xy(CAMERA_LAT, CAMERA_LON)
print(f">> Câmera convertida para X/Y local: ({local_x:.2f}, {local_y:.2f})")

# --- Determinação da altura da câmera ---------------------------------------
# Ordem de preferência:
#   1. CAMERA_ABS_ALT (se você souber a elevação absoluta da lente)
#   2. Raio vertical direto (quando a câmera está sobre a área reconstruída)
#   3. Percentil baixo dos vértices vizinhos = nível do terreno estimado
_abs_alt = os.getenv("CAMERA_ABS_ALT")
CAMERA_ABS_ALT = float(_abs_alt) if _abs_alt else None

ground_hit = geo.surface_height_at(local_x, local_y)

if CAMERA_ABS_ALT is not None:
    camera_local_pos = geo.build_local_point(local_x, local_y, CAMERA_ABS_ALT)
    print(f">> Altura da lente definida manualmente (CAMERA_ABS_ALT): {CAMERA_ABS_ALT}")
elif ground_hit is not None:
    terreno = geo.local_up_value(ground_hit)
    camera_local_pos = geo.build_local_point(local_x, local_y, terreno + CAMERA_ALT_ABOVE_GROUND)
    print(f">> Terreno sob a câmera (raio vertical): {terreno:.2f} "
          f"-> lente a {terreno + CAMERA_ALT_ABOVE_GROUND:.2f}")
else:
    est = geo.estimate_ground_height(local_x, local_y)
    if est is not None:
        terreno, n_vert, raio = est
        camera_local_pos = geo.build_local_point(local_x, local_y, terreno + CAMERA_ALT_ABOVE_GROUND)
        print(f">> Câmera fora da área reconstruída. Terreno estimado pelo percentil 8 de "
              f"{n_vert} vértices num raio de {raio:.0f}m: {terreno:.2f} "
              f"-> lente a {terreno + CAMERA_ALT_ABOVE_GROUND:.2f}")
        print(f"   (para comparação, o ponto de malha mais próximo está em "
              f"{geo.local_up_value(geo.closest_point_on_mesh(geo.build_local_point(local_x, local_y, terreno))[0]):.2f})")
    else:
        nearest_point, _d = geo.closest_point_on_mesh(
            geo.build_local_point(local_x, local_y, geo.local_up_value(geo.mesh.centroid))
        )
        terreno = geo.local_up_value(nearest_point)
        camera_local_pos = geo.build_local_point(local_x, local_y, terreno + CAMERA_ALT_ABOVE_GROUND)
        print(f">> Fallback: altura do ponto de malha mais próximo ({terreno:.2f}) + "
              f"{CAMERA_ALT_ABOVE_GROUND}m. Considere definir CAMERA_ABS_ALT.")

closest_wall_point, _dist = geo.closest_point_on_mesh(camera_local_pos)
base_forward = closest_wall_point - camera_local_pos
base_forward = base_forward / np.linalg.norm(base_forward)

print(f">> Câmera posicionada em (local): {camera_local_pos}")
print(f">> Direção 'pan=0/tilt=0' (geometria real): {base_forward}")

app = FastAPI(title="Dashboard Server")
app.mount("/model", StaticFiles(directory="static"), name="model")
app.mount("/history_files", StaticFiles(directory=HISTORY_DIR), name="history_files")


# ----------------------------------------------------------------------------
# Cone: zoom <-> meio-ângulo
# ----------------------------------------------------------------------------
def half_angle_for_zoom(zoom_pct):
    t = max(0.0, min(100.0, float(zoom_pct))) / 100.0
    return CONE_HALF_ANGLE_WIDE + (CONE_HALF_ANGLE_TELE - CONE_HALF_ANGLE_WIDE) * t


def zoom_for_half_angle(half_angle):
    span = CONE_HALF_ANGLE_WIDE - CONE_HALF_ANGLE_TELE
    if span <= 0:
        return 0.0
    pct = (CONE_HALF_ANGLE_WIDE - float(half_angle)) / span * 100.0
    return max(0.0, min(100.0, pct))


_view_cache = {"key": None, "value": None}


def compute_view(pan_deg, tilt_deg, zoom_pct):
    """Ponto de impacto + contorno real do cone contra a malha."""
    key = (round(pan_deg, 2), round(tilt_deg, 2), round(zoom_pct, 1))
    if _view_cache["key"] == key:
        return _view_cache["value"]

    half = half_angle_for_zoom(zoom_pct)
    cone = geo.cone_footprint(
        camera_local_pos, base_forward, pan_deg, tilt_deg,
        half_angle_deg=half, n_rays=CONE_RING_RAYS, max_range=CONE_MAX_RANGE,
    )
    value = {
        "hit_point": cone["center"] if cone["hit"] else None,
        "cone": cone,
    }
    _view_cache["key"] = key
    _view_cache["value"] = value
    return value


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
def camera_info():
    return {
        "camera_local_pos": camera_local_pos.tolist(),
        "base_forward": base_forward.tolist(),
        "lat": CAMERA_LAT,
        "lon": CAMERA_LON,
        "alt": CAMERA_ALT_ABOVE_GROUND,
        "controller_url": CONTROLLER_PUBLIC_URL,
        "cone_ring_rays": CONE_RING_RAYS,
        "half_angle_wide": CONE_HALF_ANGLE_WIDE,
        "half_angle_tele": CONE_HALF_ANGLE_TELE,
        "model_up_axis": MODEL_UP_AXIS,
        "pan_sign": PAN_SIGN,
        "tilt_sign": TILT_SIGN,
    }


@app.get("/api/view")
async def view(pan: float = 0.0, tilt: float = 0.0, zoom: float = 0.0):
    """Estado do cone sob demanda (usado na abertura do dashboard)."""
    result = await run_in_threadpool(compute_view, pan, tilt, zoom)
    return {"coord_p": pan, "coord_t": tilt, "coord_z": zoom, **result}


@app.post("/api/telemetry")
async def telemetry(payload: TelemetryPayload):
    result = await run_in_threadpool(
        compute_view, payload.coord_p, payload.coord_t, payload.coord_z
    )
    msg = {
        "type": "telemetry",
        "coord_p": payload.coord_p,
        "coord_t": payload.coord_t,
        "coord_z": payload.coord_z,
        **result,
    }
    await manager.broadcast(msg)
    return {"status": "ok", "hit_point": result["hit_point"]}


@app.post("/api/detection")
async def detection(payload: DetectionPayload):
    """Registra uma deteccao NOVA.

    Se a camera continuar apontando para uma rachadura ja alertada e ainda
    nao tratada, nao criamos outro alerta: apenas incrementamos o contador de
    reincidencia da entrada existente. Isso evita a enxurrada de alertas
    identicos e tambem o crescimento inutil do disco com fotos repetidas.
    """
    result = await run_in_threadpool(
        compute_view, payload.coord_p, payload.coord_t, payload.coord_z
    )
    hit = result["hit_point"]
    agora = _agora()
    ts = payload.timestamp or agora.isoformat()

    evento = None
    entrada_nova = None

    with _history_lock:
        dados = _ler_indice()
        vizinhas = [e for e in dados
                    if _mesmo_ponto(e, hit, payload.coord_p, payload.coord_t)]

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
                "timestamp": ts,
                "ultima_vez": ts,
                "repeticoes": 1,
                "coord_p": payload.coord_p,
                "coord_t": payload.coord_t,
                "coord_z": payload.coord_z,
                "hit_point": hit,
                "utm": _utm_de(hit),
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
    point: list[float]                 # ponto 3D local (coords "cruas" do .glb)
    corners: list[list[float]] = []    # cantos do retângulo, também locais
    margin: float = 1.30               # folga para o objeto não ficar colado na borda
    apply: bool = True                 # False = só calcula, não move a câmera


@app.post("/api/aim")
def aim(payload: AimPayload):
    alvo = np.asarray(payload.point, dtype=float)
    direcao = alvo - camera_local_pos
    if np.linalg.norm(direcao) < 1e-6:
        return JSONResponse({"error": "ponto coincide com a câmera"}, status_code=400)

    pan_deg, tilt_deg = geo.direction_to_pan_tilt(base_forward, direcao)

    # Meio-ângulo necessário: maior desvio angular entre o centro e os cantos
    maior_angulo = 0.0
    for c in payload.corners:
        v = np.asarray(c, dtype=float) - camera_local_pos
        if np.linalg.norm(v) < 1e-6:
            continue
        maior_angulo = max(maior_angulo, geo.angle_between(direcao, v))

    if maior_angulo <= 0.05:
        # Retângulo minúsculo ou nenhum canto acertou o modelo: aproxima bem.
        half = CONE_HALF_ANGLE_TELE * 1.5
    else:
        half = maior_angulo * float(payload.margin)

    half = max(CONE_HALF_ANGLE_TELE, min(CONE_HALF_ANGLE_WIDE, half))
    zoom_pct = zoom_for_half_angle(half)

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
            f"{CONTROLLER_URL}/command/absolute",
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

    # Recalcula a geometria a partir do pan/tilt/zoom gravados. Isso corrige
    # automaticamente entradas antigas, salvas antes do ajuste de PAN_SIGN.
    result = await run_in_threadpool(
        compute_view, entrada["coord_p"], entrada["coord_t"], entrada["coord_z"]
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
            f"{CONTROLLER_URL}/command/absolute",
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
    pan_delta: float = 0.0
    tilt_delta: float = 0.0
    zoom_delta: float = 0.0


class ContinuousPayload(BaseModel):
    pan_speed: float = 0.0
    tilt_speed: float = 0.0
    zoom_speed: float = 0.0
    hold_ms: int = 800


def _proxy(path, body=None):
    try:
        r = http.post(f"{CONTROLLER_URL}{path}", json=body, timeout=5)
        return r.json()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/command")
def send_command(cmd: CommandPayload):
    return _proxy("/command", cmd.model_dump())


@app.post("/api/ptz/continuous")
def ptz_continuous(cmd: ContinuousPayload):
    return _proxy("/command/continuous", cmd.model_dump())


@app.post("/api/ptz/stop")
def ptz_stop():
    return _proxy("/command/stop", {})


@app.post("/api/command/home")
def send_home():
    return _proxy("/command/home", {})


# ----------------------------------------------------------------------------
# Modo borda: o Raspberry infere localmente e manda so metadados.
# ----------------------------------------------------------------------------
import borda  # noqa: E402

borda.instalar(app, borda.Contexto(
    manager=manager,
    telemetry=telemetry,
    detection=detection,
    TelemetryPayload=TelemetryPayload,
    DetectionPayload=DetectionPayload,
    history_dir=HISTORY_DIR,
    ler_indice=_ler_indice,
    escrever_indice=_escrever_indice,
    history_lock=_history_lock,
    controller_url=CONTROLLER_URL,
))


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
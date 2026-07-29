# ============================================================================
# server.py - Simulador do servidor do dashboard (roda numa porta diferente
# do controller, na mesma máquina, para o teste local)
# ============================================================================
import base64
import json
import os
import time
import uuid
from datetime import datetime, timezone

import numpy as np
import requests
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from glb_geo import GeoModel, MODEL_UP_AXIS

# --- Posição real da câmera (fornecida) -------------------------------------
CAMERA_LAT = -6.152425824994227
CAMERA_LON = -37.12619639369007
CAMERA_ALT_ABOVE_GROUND = 7.0  # metros

CONTROLLER_URL = "http://127.0.0.1:8090"  # no Raspberry: IP real do controller
HISTORY_DIR = "history"
HISTORY_INDEX = os.path.join(HISTORY_DIR, "index.json")

os.makedirs(HISTORY_DIR, exist_ok=True)
if not os.path.exists(HISTORY_INDEX):
    with open(HISTORY_INDEX, "w") as f:
        json.dump([], f)

# --- Carrega o modelo real e calibra o "pan=0/tilt=0 -> de frente pra parede"
geo = GeoModel()

_bounds = geo.mesh.bounds  # [[minx,miny,minz],[maxx,maxy,maxz]]
print(f">> Bounding box real do modelo (.glb), coordenadas locais: min={_bounds[0]} max={_bounds[1]}")

local_x, local_y = geo.latlon_to_local_xy(CAMERA_LAT, CAMERA_LON)
print(f">> Câmera (lat/lon fornecidos) convertida para X/Y local: ({local_x:.2f}, {local_y:.2f})")
print(f">> Compare com o bounding box acima: se X/Y da câmera estiver bem fora do range do")
print(f"   modelo, é sinal de erro na lat/lon, na zona/hemisfério UTM, ou no offset do")
print(f"   georreferenciamento (confira se o .txt é do MESMO processamento que gerou este .glb).")

ground_hit = geo.surface_height_at(local_x, local_y)

if ground_hit is not None:
    camera_local_pos = ground_hit.copy()
    up_val = geo.local_up_value(camera_local_pos) + CAMERA_ALT_ABOVE_GROUND
    camera_local_pos = geo.build_local_point(local_x, local_y, up_val)
    print(f">> Terreno real do modelo encontrado sob a câmera: altura={geo.local_up_value(ground_hit):.2f} "
          f"-> câmera posicionada {CAMERA_ALT_ABOVE_GROUND}m acima disso.")
else:
    # A câmera está fora da área XY que o .glb reconstruiu (comum quando o
    # modelo cobre só a parede, e a câmera fica a alguns metros de distância
    # dela). Em vez de chutar uma elevação, usamos a altura do PONTO REAL
    # mais próximo da malha (busca por distância 3D mínima, não vertical) como
    # referência -- ainda é geometria real do modelo, não aproximação numérica.
    mesh_center_up = geo.local_up_value(geo.mesh.centroid)
    seed = geo.build_local_point(local_x, local_y, mesh_center_up)
    nearest_point, _dist = geo.closest_point_on_mesh(seed)
    up_val = geo.local_up_value(nearest_point) + CAMERA_ALT_ABOVE_GROUND
    camera_local_pos = geo.build_local_point(local_x, local_y, up_val)
    print(f">> Câmera fora da área XY do modelo (distância ao ponto mais próximo: {_dist:.1f}). "
          f"Usando altura do ponto real mais próximo da malha ({geo.local_up_value(nearest_point):.2f}) "
          f"+ {CAMERA_ALT_ABOVE_GROUND}m como referência.")

closest_wall_point, _dist = geo.closest_point_on_mesh(camera_local_pos)
base_forward = closest_wall_point - camera_local_pos
base_forward = base_forward / np.linalg.norm(base_forward)

print(f">> Câmera posicionada em (local): {camera_local_pos}")
print(f">> Direção 'pan=0/tilt=0' (rumo à parede, calculada pela geometria real): {base_forward}")

app = FastAPI(title="Dashboard Server (simulador)")
app.mount("/model", StaticFiles(directory="static"), name="model")
app.mount("/history_files", StaticFiles(directory=HISTORY_DIR), name="history_files")


class TelemetryPayload(BaseModel):
    coord_p: float
    coord_t: float
    coord_z: float
    detect: bool = False


class DetectionPayload(TelemetryPayload):
    timestamp: str | None = None
    image_b64: str | None = None
    mask_image_b64: str | None = None


# --- WebSocket: dashboard escuta atualizações em tempo real -----------------
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


def compute_hit_point(pan_deg, tilt_deg):
    hit = geo.raycast(camera_local_pos, base_forward, pan_deg, tilt_deg)
    return None if hit is None else hit.tolist()


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
    }


@app.post("/api/telemetry")
async def telemetry(payload: TelemetryPayload):
    hit = compute_hit_point(payload.coord_p, payload.coord_t)
    msg = {
        "type": "telemetry",
        "coord_p": payload.coord_p,
        "coord_t": payload.coord_t,
        "coord_z": payload.coord_z,
        "hit_point": hit,
    }
    await manager.broadcast(msg)
    return {"status": "ok", "hit_point": hit}


@app.post("/api/detection")
async def detection(payload: DetectionPayload):
    hit = compute_hit_point(payload.coord_p, payload.coord_t)
    det_id = str(uuid.uuid4())
    ts = payload.timestamp or datetime.now(timezone.utc).isoformat()

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

    entry = {
        "id": det_id,
        "timestamp": ts,
        "coord_p": payload.coord_p,
        "coord_t": payload.coord_t,
        "coord_z": payload.coord_z,
        "hit_point": hit,
        "image": image_path,
        "mask_image": mask_path,
    }

    with open(HISTORY_INDEX, "r+") as f:
        data = json.load(f)
        data.insert(0, entry)
        f.seek(0)
        json.dump(data, f, indent=2)
        f.truncate()

    await manager.broadcast({"type": "detection", **entry})
    return {"status": "ok", "id": det_id, "hit_point": hit}


@app.get("/api/history")
def history():
    with open(HISTORY_INDEX) as f:
        return JSONResponse(json.load(f))


# --- Repassa comandos de movimentação do dashboard para o controller -------
class CommandPayload(BaseModel):
    pan_delta: float = 0.0
    tilt_delta: float = 0.0
    zoom_delta: float = 0.0


@app.post("/api/command")
def send_command(cmd: CommandPayload):
    try:
        r = requests.post(f"{CONTROLLER_URL}/command", json=cmd.model_dump(), timeout=5)
        return r.json()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/command/home")
def send_home():
    try:
        r = requests.post(f"{CONTROLLER_URL}/command/home", timeout=5)
        return r.json()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


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

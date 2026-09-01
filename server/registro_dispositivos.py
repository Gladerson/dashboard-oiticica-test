# ============================================================================
# registro_dispositivos.py - Estado em memoria POR DISPOSITIVO.
#
# Antes desta etapa, server/borda.py guardava UM EstadoDesejado, UM Quadro e
# UM mapa de det_id -- desenhado pra um Raspberry por vez, com o GeoModel/
# pose de camera tambem unicos, hardcoded em server.py. Agora cada
# dispositivo cadastrado (server/dispositivos.py, tabela `dispositivos`)
# autentica por TOKEN (header "Authorization: Bearer <token>") e ganha seu
# proprio slot aqui: seu proprio estado desejado, seu proprio ultimo frame,
# seu proprio GeoModel/pose de camera (a partir da localidade cadastrada,
# server/glb_geo.py).
#
# Escopo desta etapa (decidido explicitamente, ver README): SO o caminho
# HTTP. A ponte MQTT local de teste (server/borda.py, MQTT_BRIDGE=true)
# continua com o comportamento antigo -- um unico dispositivo, identificado
# pela variavel de ambiente DEVICE_ID, sem passar por aqui -- ate ganhar sua
# propria etapa (o ThingsBoard de verdade resolve identidade pela propria
# autenticacao do broker, o que muda esse caminho de qualquer jeito).
#
# Um dispositivo cadastrado mas cuja localidade nao tem modelo 3D "pronto"
# (ou sem localidade nenhuma) continua autenticando e reportando telemetria/
# deteccao normalmente -- so fica sem raycasting (hit_point/cone sempre
# None), por decisao explicita: nao ha fallback para nenhum modelo "padrao".
# ============================================================================
import os
import threading
import time

import numpy as np
import requests

import db
from glb_geo import GeoModel

# --- Falam com a API do Pi (porta 8090). Usados quando o dispositivo nao
# tem controller_url/controller_url_publica proprios (dispositivos.py) --
# unico jeito de nao quebrar quem so tem um dispositivo e nunca preencheu
# isso. ------------------------------------------------------------------
CONTROLLER_URL_PADRAO = os.getenv("CONTROLLER_URL", "http://127.0.0.1:8090")
CONTROLLER_URL_PUBLICA_PADRAO = os.getenv("CONTROLLER_PUBLIC_URL", CONTROLLER_URL_PADRAO)

# --- Estado desejado / stream -----------------------------------------------
STREAM_JANELA_S = float(os.getenv("STREAM_JANELA_S", "60"))
STREAM_FPS = float(os.getenv("STREAM_FPS", "4"))
STREAM_LARGURA = int(os.getenv("STREAM_LARGURA", "640"))
STREAM_QUALIDADE = int(os.getenv("STREAM_QUALIDADE", "60"))
STREAM_ANOTADO = os.getenv("STREAM_ANOTADO", "true").lower() in ("1", "true", "sim")

# Limites da largura que o dashboard pode pedir. O teto existe para o Pi nao
# gastar CPU/rede codificando mais pixels do que qualquer tela mostra; o piso
# evita que um painel muito estreito peca uma imagem inutilizavel.
LARGURA_STREAM_MIN = 320
LARGURA_STREAM_MAX = int(os.getenv("STREAM_LARGURA_MAX", "1280"))


def largura_stream_valida(largura):
    """Normaliza a largura pedida pelo dashboard. None (ou lixo) -> None,
    que faz o snapshot cair no padrao do servidor."""
    if largura is None:
        return None
    try:
        v = int(largura)
    except (TypeError, ValueError):
        return None
    return max(LARGURA_STREAM_MIN, min(LARGURA_STREAM_MAX, v))

# --- Cone de visao (globais: abertura de lente e faixa de zoom sao
# propriedade da CAMERA/lente, nao da localidade -- mesmo espirito de
# PAN_SIGN/TILT_SIGN em glb_geo.py) ------------------------------------------
CONE_HALF_ANGLE_WIDE = float(os.getenv("CONE_HALF_ANGLE_WIDE", "18.0"))
CONE_HALF_ANGLE_TELE = float(os.getenv("CONE_HALF_ANGLE_TELE", "2.0"))
CONE_RING_RAYS = int(os.getenv("CONE_RING_RAYS", "24"))
CONE_MAX_RANGE = float(os.getenv("CONE_MAX_RANGE", "250.0"))

# Altura da lente acima do solo, quando o dispositivo nao especifica
# alt_acima_solo (mesmo padrao que CAMERA_ALT_ABOVE_GROUND tinha em
# server.py antes desta etapa).
ALTURA_PADRAO_M = 7.0


def half_angle_for_zoom(zoom_pct):
    t = max(0.0, min(100.0, float(zoom_pct))) / 100.0
    return CONE_HALF_ANGLE_WIDE + (CONE_HALF_ANGLE_TELE - CONE_HALF_ANGLE_WIDE) * t


def zoom_for_half_angle(half_angle):
    span = CONE_HALF_ANGLE_WIDE - CONE_HALF_ANGLE_TELE
    if span <= 0:
        return 0.0
    pct = (CONE_HALF_ANGLE_WIDE - float(half_angle)) / span * 100.0
    return max(0.0, min(100.0, pct))


# ============================================================================
# Estado desejado (identico ao que borda.py tinha, so que o "push" de baixa
# latencia agora e injetado -- cada dispositivo empurra pro SEU controller_url)
# ============================================================================
class EstadoDesejado:
    def __init__(self, empurrar_para=None):
        self.lock = threading.Lock()
        self.versao = 1
        self.transporte = "http"
        self.stream_expira = 0.0
        # None = usa o padrao do servidor (STREAM_LARGURA). O dashboard
        # informa de quantos pixels ele precisa de verdade: com o painel
        # direito redimensionavel, pedir sempre 640 px fazia o navegador
        # AMPLIAR a imagem quando o operador alargava a telinha -- que e
        # justamente quando ele quer ver melhor. Ver LARGURA_STREAM_MIN/MAX.
        self.stream_largura = None
        self.inferencia = {"conf": 0.45, "iou": 0.45,
                           "intervalo_frames": 5, "cooldown_s": 5}
        self.pedidos_imagem = []
        # Vindo do Pi, so para exibir no painel
        self.ultimo_visto = 0.0
        self.relatado = {}
        self._empurrar_para = empurrar_para

    def _snapshot_locked(self):
        restante = max(0.0, self.stream_expira - time.time())
        return {
            "versao": self.versao,
            "transporte": self.transporte,
            "stream": {
                "ativo": restante > 0,
                "restante_s": round(restante, 1),
                "fps": STREAM_FPS,
                "largura": self.stream_largura or STREAM_LARGURA,
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
        if self._empurrar_para is not None:
            self._empurrar_para(snap)
        return snap

    def consumir_pedidos(self):
        with self.lock:
            self.pedidos_imagem = []


def empurrador_http(controller_url):
    """So um acelerador de latencia -- o caminho garantido continua sendo a
    carona na resposta do proximo POST de telemetria (funciona mesmo atras
    de NAT). Se isto falhar ou nao existir controller_url, tanto faz."""
    def _empurrar(snap):
        if not controller_url:
            return

        def _tentar():
            try:
                requests.post(f"{controller_url}/borda/estado", json=snap, timeout=1.5)
            except Exception:
                pass
        threading.Thread(target=_tentar, daemon=True).start()
    return _empurrar


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


# ============================================================================
# Cache de GeoModel por localidade -- carregar um .glb e montar o indice de
# raycasting nao e barato; varios dispositivos na MESMA localidade
# compartilham a mesma instancia.
# ============================================================================
class _CacheGeoModel:
    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def obter(self, localidade_id, path, utm_zone, utm_hemisferio_sul,
             geo_offset_x, geo_offset_y, geo_offset_z, model_up_axis):
        chave = str(localidade_id)
        with self._lock:
            atual = self._cache.get(chave)
        if atual is not None:
            return atual
        modelo = GeoModel(path=path, utm_zone=utm_zone, utm_hemisferio_sul=utm_hemisferio_sul,
                          geo_offset_x=geo_offset_x, geo_offset_y=geo_offset_y,
                          geo_offset_z=geo_offset_z, model_up_axis=model_up_axis)
        with self._lock:
            self._cache[chave] = modelo
        return modelo

    def invalidar(self, localidade_id):
        with self._lock:
            self._cache.pop(str(localidade_id), None)


_geo_cache = _CacheGeoModel()


# ============================================================================
# Runtime de um dispositivo: estado + quadro + geometria (se a localidade
# estiver pronta)
# ============================================================================
class DispositivoRuntime:
    def __init__(self, linha):
        self.id = str(linha["id"])
        self.token = linha["token"]
        self.nome = linha["nome"]
        self.entity_id = linha["entity_id"]
        self.transporte_cadastrado = linha["transporte"]
        self.controller_url = linha.get("controller_url") or CONTROLLER_URL_PADRAO
        self.controller_url_publica = (linha.get("controller_url_publica")
                                       or self.controller_url or CONTROLLER_URL_PUBLICA_PADRAO)
        self.lat = linha.get("lat")
        self.lon = linha.get("lon")
        self.alt_acima_solo = linha.get("alt_acima_solo")
        self.localidade_id = str(linha["localidade_id"]) if linha.get("localidade_id") else None
        self.localidade_nome = linha.get("localidade_nome")
        self.localidade_modelo_3d_path = linha.get("localidade_modelo_3d_path")

        self.estado = EstadoDesejado(empurrar_para=empurrador_http(self.controller_url))
        self.quadro = Quadro()
        self.mapa_det = {}   # det_id da borda -> id no historico do servidor
        self._view_cache = {"key": None, "value": None}

        self.geo = None
        self.camera_local_pos = None
        self.base_forward = None
        self._preparar_geometria(linha)

    def pronto(self):
        return self.geo is not None

    def _preparar_geometria(self, linha):
        if not self.localidade_id or linha.get("localidade_modelo_status") != "pronto":
            return
        if self.lat is None or self.lon is None:
            print(f"[registro] '{self.nome}': sem lat/lon cadastrados, sem raycasting.")
            return
        try:
            geo = _geo_cache.obter(
                self.localidade_id, linha["localidade_modelo_3d_path"],
                linha["localidade_utm_zone"], linha["localidade_utm_hemisferio_sul"],
                linha["localidade_geo_offset_x"], linha["localidade_geo_offset_y"],
                linha["localidade_geo_offset_z"], linha["localidade_model_up_axis"],
            )
        except Exception as e:
            print(f"[registro] '{self.nome}': falha ao carregar o modelo 3D da "
                  f"localidade '{self.localidade_nome}': {e}")
            return

        local_x, local_y = geo.latlon_to_local_xy(self.lat, self.lon)
        altura_lente = float(self.alt_acima_solo) if self.alt_acima_solo is not None else ALTURA_PADRAO_M

        ground_hit = geo.surface_height_at(local_x, local_y)
        if ground_hit is not None:
            terreno = geo.local_up_value(ground_hit)
        else:
            est = geo.estimate_ground_height(local_x, local_y)
            if est is not None:
                terreno = est[0]
            else:
                ponto_proximo, _d = geo.closest_point_on_mesh(
                    geo.build_local_point(local_x, local_y, geo.local_up_value(geo.mesh.centroid)))
                terreno = geo.local_up_value(ponto_proximo)

        camera_local_pos = geo.build_local_point(local_x, local_y, terreno + altura_lente)
        ponto_parede, _dist = geo.closest_point_on_mesh(camera_local_pos)
        direcao = ponto_parede - camera_local_pos
        norma = np.linalg.norm(direcao)
        if norma < 1e-9:
            print(f"[registro] '{self.nome}': camera coincide com a malha; sem direcao base.")
            return

        self.geo = geo
        self.camera_local_pos = camera_local_pos
        self.base_forward = direcao / norma
        print(f"[registro] '{self.nome}' pronto: camera em (local) {camera_local_pos}, "
              f"direcao base {self.base_forward}")

    def compute_view(self, pan_deg, tilt_deg, zoom_pct):
        """Ponto de impacto + contorno real do cone contra a malha (ou
        hit_point=None/cone=None se a localidade ainda nao tem modelo
        pronto -- nao ha fallback para nenhum modelo padrao)."""
        if not self.pronto():
            return {"hit_point": None, "cone": None}
        key = (round(pan_deg, 2), round(tilt_deg, 2), round(zoom_pct, 1))
        if self._view_cache["key"] == key:
            return self._view_cache["value"]
        half = half_angle_for_zoom(zoom_pct)
        cone = self.geo.cone_footprint(
            self.camera_local_pos, self.base_forward, pan_deg, tilt_deg,
            half_angle_deg=half, n_rays=CONE_RING_RAYS, max_range=CONE_MAX_RANGE,
        )
        value = {"hit_point": cone["center"] if cone["hit"] else None, "cone": cone}
        self._view_cache["key"] = key
        self._view_cache["value"] = value
        return value


# ============================================================================
# Registro em memoria
# ============================================================================
_lock = threading.Lock()
_por_id = {}       # device_id (str) -> DispositivoRuntime
_id_por_token = {}  # token -> device_id (str)


def _construir(linha):
    rt = DispositivoRuntime(linha)
    with _lock:
        _por_id[rt.id] = rt
        if rt.token:
            _id_por_token[rt.token] = rt.id
    return rt


def por_token(token):
    """Resolve (criando na primeira vez) o runtime dono desse token --
    autenticacao de /api/edge/* e /api/telemetry|detection. None se o token
    nao existir em nenhum dispositivo cadastrado."""
    if not token:
        return None
    with _lock:
        device_id = _id_por_token.get(token)
        if device_id is not None:
            return _por_id[device_id]
    linha = db.dispositivo_por_token(token)
    if linha is None:
        return None
    return _construir(linha)


def todos():
    """Todo dispositivo ja resolvido (por token ou por id) desde que o
    processo subiu -- usado pela vigia de stream (borda.py), que precisa
    checar expiracao de TODOS, nao so de um."""
    with _lock:
        return list(_por_id.values())


def por_id(device_id):
    """Runtime por id (rotas do dashboard: /api/aim, /api/locate, /api/borda
    etc, que resolvem por device_id, nao por token). None se o id nao
    existe/nao esta cadastrado."""
    if not device_id:
        return None
    with _lock:
        rt = _por_id.get(str(device_id))
    if rt is not None:
        return rt
    linha = db.dispositivo_por_id_com_localidade(device_id)
    if linha is None:
        return None
    return _construir(linha)


def _esquecer(device_id):
    with _lock:
        antigo = _por_id.pop(str(device_id), None)
        if antigo is not None and antigo.token:
            _id_por_token.pop(antigo.token, None)


def esquecer(device_id):
    """Descarta o runtime sem remontar -- usado quando o dispositivo e
    EXCLUIDO. Importante para o token tambem sair do ar na hora: sem isto,
    o token de um dispositivo excluido continuaria autenticando ate o
    processo reiniciar, porque _id_por_token e um cache em memoria."""
    _esquecer(device_id)


def recarregar(device_id):
    """Forca reconstrucao do runtime (ex.: depois de editar o dispositivo
    em server/dispositivos.py)."""
    _esquecer(device_id)
    return por_id(device_id)


def invalidar_localidade(localidade_id):
    """Chamado quando o modelo 3D de uma localidade termina de processar
    (server/dispositivos.py) -- descarta os runtimes que dependiam dela
    (tinham geo=None porque o modelo ainda nao estava pronto), pra serem
    reconstruidos com o modelo novo no proximo acesso."""
    _geo_cache.invalidar(localidade_id)
    localidade_id = str(localidade_id)
    with _lock:
        afetados = [did for did, rt in _por_id.items() if rt.localidade_id == localidade_id]
    for did in afetados:
        _esquecer(did)

# ============================================================================
# glb_geo.py - Georreferenciamento REAL do modelo .glb (sem aproximação)
#
# Novidades desta versão:
#   • direction_from_pan_tilt / direction_to_pan_tilt  (ida e volta entre
#     ângulos ONVIF e direção 3D real) -- o inverso é o que permite clicar
#     num ponto do modelo e descobrir para onde a câmera precisa apontar.
#   • cone_footprint(): dispara um leque de raios reais contra a malha e
#     devolve o contorno onde o campo de visão encosta no objeto. É o que
#     faz o cone do dashboard "se moldar" à parede em vez de flutuar.
#   • Correção: o eixo de tilt agora é recalculado APÓS o pan (cabeçote real
#     inclina em torno do eixo horizontal já rotacionado).
#   • Usa o intersector Embree quando disponível (muito mais rápido).
#   • GeoModel agora recebe o georreferenciamento (zona UTM, offsets, eixo
#     "para cima") pelo CONSTRUTOR, não mais como constante de módulo -- é o
#     que permite N localidades (N .glb, cada um com seu georreferenciamento
#     próprio, ver server/dispositivos.py e a tabela `localidades`) em vez de
#     um único modelo fixo por processo.
# ============================================================================
import os

import trimesh
import numpy as np
from pyproj import Transformer

# --- Sentido de rotação do pan/tilt ------------------------------------------
# O ONVIF não padroniza para que lado o pan positivo gira. Nesta câmera, o pan
# positivo corresponde ao sentido HORÁRIO visto de cima, que é o oposto da
# convenção matemática -- daí o -1. Se em outra câmera o cone andar espelhado,
# troque o sinal (via env PAN_SIGN=1, sem editar código).
# O mesmo vale para o tilt (TILT_SIGN=-1 se subir/descer estiver trocado).
#
# Continuam globais (um valor só, para o processo inteiro) mesmo depois do
# modelo virar multi-localidade: é a convenção de fiação do CABEÇOTE PTZ, não
# do modelo 3D -- nada hoje sugere que duas câmeras/dispositivos precisem de
# sinais diferentes, e criar essa variação por dispositivo sem um caso real
# seria complexidade especulativa. Ver README, pendências conhecidas.
PAN_SIGN = float(os.getenv("PAN_SIGN", "-1"))
TILT_SIGN = float(os.getenv("TILT_SIGN", "1"))

MODEL_PATH = "static/model.glb"


def _normalize(v):
    v = np.asarray(v, dtype=float)
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


def _rotate(vec, axis, angle):
    """Rodrigues."""
    axis = _normalize(axis)
    return (
        vec * np.cos(angle)
        + np.cross(axis, vec) * np.sin(angle)
        + axis * np.dot(axis, vec) * (1 - np.cos(angle))
    )


class GeoModel:
    def __init__(self, path, utm_zone, utm_hemisferio_sul, geo_offset_x, geo_offset_y,
                 geo_offset_z=0.0, model_up_axis="Z"):
        self.utm_zone = int(utm_zone)
        self.utm_hemisferio_sul = bool(utm_hemisferio_sul)
        self.geo_offset_x = float(geo_offset_x)
        self.geo_offset_y = float(geo_offset_y)
        self.geo_offset_z = float(geo_offset_z)
        self.model_up_axis = model_up_axis

        scene = trimesh.load(path, force="scene")
        self.mesh = trimesh.util.concatenate(
            [g for g in scene.geometry.values() if isinstance(g, trimesh.Trimesh)]
        )

        if self.mesh is None or len(self.mesh.vertices) == 0:
            raise RuntimeError(
                "A malha carregada do .glb está VAZIA (0 vértices). Normalmente "
                "significa que o modelo usa compressão Draco e o trimesh não "
                "conseguiu decodificar. Rode `bash prepare_model.sh` (recomendado) "
                "ou instale: pip install \"DracoPy<2\""
            )

        span = self.mesh.bounds[1] - self.mesh.bounds[0]
        if np.allclose(span, 0):
            raise RuntimeError(
                f"A malha tem {len(self.mesh.vertices)} vértices e {len(self.mesh.faces)} "
                "faces, mas todas as coordenadas vieram zeradas (incompatibilidade "
                "trimesh/DracoPy). Rode `bash prepare_model.sh`."
            )

        print(f">> Malha carregada: {len(self.mesh.vertices)} vértices, {len(self.mesh.faces)} faces.")

        # Embree é dezenas de vezes mais rápido; essencial porque agora
        # disparamos ~25 raios por atualização de telemetria, não 1.
        self.intersector = None
        try:
            from trimesh.ray.ray_pyembree import RayMeshIntersector as _Embree
            self.intersector = _Embree(self.mesh)
            print(">> Raycasting acelerado por Embree (embreex) ativo.")
        except Exception:
            self.intersector = trimesh.ray.ray_triangle.RayMeshIntersector(self.mesh)
            print(">> Embree indisponível; usando raycasting em NumPy puro.")
            print("   Para acelerar o cone: pip install embreex")

        self._transformer = self._utm_transformer()

    def _utm_transformer(self):
        epsg = (f"326{self.utm_zone:02d}" if self.utm_hemisferio_sul is False
                else f"327{self.utm_zone:02d}")
        return Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)

    # ------------------------------------------------------------------
    # Convenção de eixos
    # ------------------------------------------------------------------
    def _up_vector(self):
        return np.array([0.0, 1.0, 0.0]) if self.model_up_axis == "Y" else np.array([0.0, 0.0, 1.0])

    def build_local_point(self, local_x, local_y, up_value):
        if self.model_up_axis == "Z":
            return np.array([local_x, local_y, up_value])
        return np.array([local_x, up_value, -local_y])

    def local_up_value(self, point):
        return point[2] if self.model_up_axis == "Z" else point[1]

    # ------------------------------------------------------------------
    # Geo
    # ------------------------------------------------------------------
    def latlon_to_local_xy(self, lat, lon):
        utm_x, utm_y = self._transformer.transform(lon, lat)
        return utm_x - self.geo_offset_x, utm_y - self.geo_offset_y

    def local_to_utm(self, point):
        """Inverso de latlon_to_local_xy/build_local_point: dado um ponto do
        modelo (ex.: o hit_point de uma deteccao), devolve (utm_x, utm_y,
        altitude) em metros reais. Como o local X/Y JA E "UTM menos o
        offset" (ver latlon_to_local_xy), a volta e so somar o offset de
        volta -- nao precisa de nenhuma projecao nova."""
        point = np.asarray(point, dtype=float)
        if self.model_up_axis == "Z":
            local_x, local_y = point[0], point[1]
        else:
            local_x, local_y = point[0], -point[2]
        altitude = self.local_up_value(point) + self.geo_offset_z
        return (float(local_x + self.geo_offset_x),
                float(local_y + self.geo_offset_y),
                float(altitude))

    def latlon_alt_to_local(self, lat, lon, alt):
        local_x, local_y = self.latlon_to_local_xy(lat, lon)
        local_up = alt - self.geo_offset_z
        if self.model_up_axis == "Y":
            return np.array([local_x, local_up, -local_y])
        return np.array([local_x, local_y, local_up])

    def surface_height_at(self, local_x, local_y, search_height=100000.0):
        up_idx = 2 if self.model_up_axis == "Z" else 1
        if self.model_up_axis == "Z":
            origin = np.array([local_x, local_y, search_height])
            direction = np.array([0.0, 0.0, -1.0])
        else:
            origin = np.array([local_x, search_height, -local_y])
            direction = np.array([0.0, -1.0, 0.0])

        locations, _, _ = self.intersector.intersects_location(
            ray_origins=[origin], ray_directions=[direction]
        )
        if len(locations) == 0:
            return None
        idx = np.argmax(locations[:, up_idx])
        return locations[idx]

    def closest_point_on_mesh(self, point):
        closest, distance, _ = trimesh.proximity.closest_point(self.mesh, [point])
        return closest[0], distance[0]

    # ------------------------------------------------------------------
    # Ângulos ONVIF <-> direção 3D
    # ------------------------------------------------------------------
    def direction_from_pan_tilt(self, base_forward, pan_deg, tilt_deg):
        """Direção 3D real correspondente a um pan/tilt da telemetria."""
        up = self._up_vector()
        f = _normalize(base_forward)

        d = _rotate(f, up, np.radians(PAN_SIGN * pan_deg))

        # eixo de tilt recalculado APÓS o pan (cabeçote pan-tilt real)
        right = np.cross(d, up)
        if np.linalg.norm(right) < 1e-9:
            right = np.cross(f, up)
        right = _normalize(right)

        d = _rotate(d, right, np.radians(TILT_SIGN * tilt_deg))
        return _normalize(d)

    def _az_el(self, v):
        v = _normalize(v)
        if self.model_up_axis == "Z":
            return np.arctan2(v[1], v[0]), np.arcsin(np.clip(v[2], -1.0, 1.0))
        return np.arctan2(-v[2], v[0]), np.arcsin(np.clip(v[1], -1.0, 1.0))

    def direction_to_pan_tilt(self, base_forward, direction):
        """Inverso exato de direction_from_pan_tilt: dada uma direção 3D,
        devolve (pan_deg, tilt_deg) que a câmera precisa assumir.
        Os sinais PAN_SIGN/TILT_SIGN são desfeitos aqui para que a
        ida-e-volta continue exata."""
        az0, el0 = self._az_el(base_forward)
        az1, el1 = self._az_el(direction)
        d_az = (az1 - az0 + np.pi) % (2 * np.pi) - np.pi
        return (float(np.degrees(d_az) / PAN_SIGN),
                float(np.degrees(el1 - el0) / TILT_SIGN))

    def angle_between(self, v1, v2):
        c = float(np.clip(np.dot(_normalize(v1), _normalize(v2)), -1.0, 1.0))
        return float(np.degrees(np.arccos(c)))

    # ------------------------------------------------------------------
    # Raycasting
    # ------------------------------------------------------------------
    def raycast_batch(self, origin, directions):
        """Dispara vários raios de uma vez. Devolve lista (mesmo tamanho de
        `directions`) com o ponto de impacto mais próximo, ou None."""
        origin = np.asarray(origin, dtype=float)
        dirs = np.asarray(directions, dtype=float)
        origins = np.tile(origin, (len(dirs), 1))

        locations, index_ray, _ = self.intersector.intersects_location(
            ray_origins=origins, ray_directions=dirs
        )

        melhores = [None] * len(dirs)
        menores = [np.inf] * len(dirs)
        for loc, ri in zip(locations, index_ray):
            dist = float(np.linalg.norm(loc - origin))
            if dist < menores[ri]:
                menores[ri] = dist
                melhores[ri] = loc
        return melhores, menores

    def raycast(self, origin, base_forward, pan_deg, tilt_deg):
        """Ponto de impacto do raio central (mantido para compatibilidade)."""
        direction = self.direction_from_pan_tilt(base_forward, pan_deg, tilt_deg)
        pontos, _ = self.raycast_batch(origin, [direction])
        return pontos[0]

    # ------------------------------------------------------------------
    def cone_footprint(self, origin, base_forward, pan_deg, tilt_deg,
                       half_angle_deg, n_rays=24, max_range=250.0):
        """Dispara o raio central + um anel de raios na borda do campo de
        visão. Devolve o contorno REAL onde o cone encosta no objeto.

        Raios que não acertam nada recebem um ponto a `max_range` (ou à
        distância do centro), só para o cone continuar fechado visualmente.
        """
        center_dir = self.direction_from_pan_tilt(base_forward, pan_deg, tilt_deg)
        up = self._up_vector()

        right = np.cross(center_dir, up)
        if np.linalg.norm(right) < 1e-9:
            right = np.cross(center_dir, np.array([1.0, 0.0, 0.0]))
        right = _normalize(right)
        upv = _normalize(np.cross(right, center_dir))

        raio_tan = np.tan(np.radians(max(0.2, half_angle_deg)))
        dirs = [center_dir]
        for i in range(n_rays):
            a = 2.0 * np.pi * i / n_rays
            offset = (np.cos(a) * right + np.sin(a) * upv) * raio_tan
            dirs.append(_normalize(center_dir + offset))

        pontos, dists = self.raycast_batch(origin, dirs)

        origin = np.asarray(origin, dtype=float)
        centro = pontos[0]
        acertou_centro = centro is not None
        dist_ref = dists[0] if acertou_centro else max_range
        if not np.isfinite(dist_ref):
            dist_ref = max_range

        anel = []
        for i in range(1, len(dirs)):
            p = pontos[i]
            if p is None:
                # não encostou em nada: projeta na mesma distância do centro
                p = origin + dirs[i] * dist_ref
            anel.append([float(x) for x in p])

        if centro is None:
            centro = origin + center_dir * dist_ref

        return {
            "center": [float(x) for x in centro],
            "ring": anel,
            "hit": bool(acertou_centro),
            "distance": float(dist_ref),
            "half_angle_deg": float(half_angle_deg),
        }
    # ------------------------------------------------------------------
    def estimate_ground_height(self, local_x, local_y, radius=40.0, percentile=8.0):
        """Estima o nível do TERRENO perto de (x,y) usando os vértices reais
        do modelo, sem precisar saber a elevação absoluta de antemão.

        A ideia: numa vizinhança, os vértices mais BAIXOS correspondem ao
        chão, e os mais altos à estrutura. Um percentil baixo (não o mínimo,
        que seria sensível a ruído de reconstrução) dá o nível do solo.

        Retorna (altura, n_vertices_usados, raio_efetivo) ou None.
        """
        up_idx = 2 if self.model_up_axis == "Z" else 1
        v = self.mesh.vertices

        if self.model_up_axis == "Z":
            dx = v[:, 0] - local_x
            dy = v[:, 1] - local_y
        else:
            dx = v[:, 0] - local_x
            dy = -v[:, 2] - local_y

        dist2 = dx * dx + dy * dy

        # Expande o raio até achar amostra suficiente (a câmera pode estar
        # fora da área reconstruída, como é o caso aqui).
        r = float(radius)
        for _ in range(6):
            sel = dist2 <= (r * r)
            n = int(sel.sum())
            if n >= 200:
                altura = float(np.percentile(v[sel, up_idx], percentile))
                return altura, n, r
            r *= 1.8

        return None
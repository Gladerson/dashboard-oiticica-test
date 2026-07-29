# ============================================================================
# glb_geo.py - Georreferenciamento REAL do modelo .glb (sem aproximação)
#
# Por que isso é necessário:
#   O modelo .glb exportado do WebODM/ODM não guarda coordenadas geográficas
#   diretamente nos vértices "de graça" para qualquer app -- o ODM desloca o
#   modelo para perto da origem (0,0,0) por precisão numérica, e guarda o
#   deslocamento real (offset) num arquivo separado, normalmente em:
#       <projeto_odm>/odm_georeferencing/odm_georeferencing_model_geo.txt
#   Esse arquivo tem, na 1a linha, a zona UTM (ex: "WGS84 UTM 24S") e na
#   2a linha o offset X Y Z em metros que foi subtraído de cada vértice.
#
#   Ou seja: coordenada_real_UTM = coordenada_do_vertice_no_glb + offset
#
# PREENCHA GEO_OFFSET_X / GEO_OFFSET_Y / GEO_OFFSET_Z e UTM_ZONE abaixo com
# os valores desse arquivo do seu projeto ODM. Sem isso, a posição da câmera
# no mundo real não vai bater com o modelo, e o raycasting vai apontar para
# o lugar errado da parede.
# ============================================================================
import trimesh
import numpy as np
from pyproj import Transformer

# --- Preenchido a partir de odm_georeferencing_model_geo.txt:
#   WGS84 UTM 24S
#   707543 9319434
# (o ODM só grava offset X/Y; quando a 2a linha tem só 2 valores, Z não foi
# deslocado, ou seja, a altitude dos vértices já é o valor real/absoluto)
UTM_ZONE = 24
UTM_HEMISPHERE_SOUTH = True
GEO_OFFSET_X = 707543.0
GEO_OFFSET_Y = 9319434.0
GEO_OFFSET_Z = 0.0

# Eixo "para cima" do modelo. Modelos exportados direto de pipelines de
# fotogrametria/ODM tipicamente guardam os vértices em Z=altura (convenção
# UTM direta), mesmo dentro de um .glb -- por isso o padrão aqui é "Z".
# Só troque para "Y" se, ao carregar no dashboard, o modelo aparecer deitado.
MODEL_UP_AXIS = "Z"

MODEL_PATH = "static/model.glb"


def _utm_transformer():
    epsg = f"326{UTM_ZONE:02d}" if UTM_HEMISPHERE_SOUTH is False else f"327{UTM_ZONE:02d}"
    # 326xx = UTM North, 327xx = UTM South
    return Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)


class GeoModel:
    def __init__(self, path=MODEL_PATH):
        scene = trimesh.load(path, force="scene")
        # Junta todas as geometrias da cena num único mesh para raycasting
        self.mesh = trimesh.util.concatenate(
            [g for g in scene.geometry.values() if isinstance(g, trimesh.Trimesh)]
        )

        if self.mesh is None or len(self.mesh.vertices) == 0:
            raise RuntimeError(
                "A malha carregada do .glb está VAZIA (0 vértices). Isso normalmente "
                "significa que o modelo usa compressão Draco e o trimesh não conseguiu "
                "decodificar (falta a biblioteca DracoPy). Rode:\n"
                "    pip install \"DracoPy<2\"\n"
                "no venv do server e tente de novo."
            )

        span = self.mesh.bounds[1] - self.mesh.bounds[0]
        if np.allclose(span, 0):
            raise RuntimeError(
                f"A malha tem {len(self.mesh.vertices)} vértices e {len(self.mesh.faces)} faces "
                "(contagens corretas), mas TODAS as coordenadas vieram zeradas. Isso é sintoma "
                "de incompatibilidade de versão entre trimesh e DracoPy (a API de decode do "
                "Draco mudou entre versões major). Tente:\n"
                "    pip install \"DracoPy<2\"\n"
                "Se já estiver assim, tente uma versão específica mais antiga, ex:\n"
                "    pip install DracoPy==1.4.2"
            )

        print(f">> Malha carregada: {len(self.mesh.vertices)} vértices, {len(self.mesh.faces)} faces.")

        self.intersector = trimesh.ray.ray_triangle.RayMeshIntersector(self.mesh)
        self._transformer = _utm_transformer()

    # ------------------------------------------------------------------
    def build_local_point(self, local_x, local_y, up_value):
        """Monta um ponto 3D local respeitando a convenção de eixo (Y-up ou
        Z-up), para não duplicar a lógica de sinal em vários lugares."""
        if MODEL_UP_AXIS == "Z":
            return np.array([local_x, local_y, up_value])
        else:
            return np.array([local_x, up_value, -local_y])

    def local_up_value(self, point):
        """Extrai o componente 'altura' de um ponto local, de acordo com a
        convenção de eixo configurada."""
        return point[2] if MODEL_UP_AXIS == "Z" else point[1]

    # ------------------------------------------------------------------
    def latlon_to_local_xy(self, lat, lon):
        """Converte lat/lon (WGS84) para X/Y locais do .glb (sem altura)."""
        utm_x, utm_y = self._transformer.transform(lon, lat)
        return utm_x - GEO_OFFSET_X, utm_y - GEO_OFFSET_Y

    def latlon_alt_to_local(self, lat, lon, alt):
        """Converte lat/lon/alt (WGS84) para coordenadas locais do .glb,
        usando o offset REAL extraído do georreferenciamento ODM.

        AVISO: 'alt' aqui é tratado como altura local absoluta (Z local),
        não como 'altura acima do terreno'. Se o arquivo de georreferenciamento
        do ODM não trouxe offset de Z (como é o seu caso), a elevação dos
        vértices do modelo costuma ser absoluta (ex: metros acima de um
        datum), então isso só dá o resultado certo se você já souber a
        elevação real do terreno no ponto. Prefira surface_height_at() +
        somar a altura acima do solo, como o server.py faz.
        """
        local_x, local_y = self.latlon_to_local_xy(lat, lon)
        local_up = alt - GEO_OFFSET_Z

        if MODEL_UP_AXIS == "Y":
            return np.array([local_x, local_up, -local_y])
        else:
            return np.array([local_x, local_y, local_up])

    def surface_height_at(self, local_x, local_y, search_height=100000.0):
        """Lança um raio vertical REAL contra a malha do modelo para achar a
        altura exata da superfície nesse X/Y -- não aproxima/assume um Z fixo.
        Retorna o ponto 3D de impacto (o mais alto encontrado, ou seja, a
        superfície visível de cima) ou None se esse X/Y estiver fora da área
        reconstruída pelo modelo."""
        up_idx = 2 if MODEL_UP_AXIS == "Z" else 1
        if MODEL_UP_AXIS == "Z":
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
        idx = np.argmax(locations[:, up_idx])  # ponto mais alto = superfície vista de cima
        return locations[idx]

    # ------------------------------------------------------------------
    def closest_point_on_mesh(self, point):
        """Ponto mais próximo da malha real a partir de um ponto qualquer.
        Usado para calibrar automaticamente a direção 'de frente para a
        parede' (pan=0) a partir da geometria real, sem chutar um ângulo."""
        closest, distance, _ = trimesh.proximity.closest_point(self.mesh, [point])
        return closest[0], distance[0]

    # ------------------------------------------------------------------
    def raycast(self, origin, base_forward, pan_deg, tilt_deg):
        """Lança um raio real contra a malha do modelo (não é aproximação
        matemática de plano) e retorna o ponto 3D de impacto na parede.

        origin: posição local da câmera (np.array [x,y,z])
        base_forward: vetor unitário "pan=0, tilt=0" (calculado uma vez a
                       partir do ponto mais próximo da malha)
        pan_deg/tilt_deg: telemetria ONVIF atual
        """
        up = np.array([0, 1, 0]) if MODEL_UP_AXIS == "Y" else np.array([0, 0, 1])

        pan_rad = np.radians(pan_deg)
        tilt_rad = np.radians(tilt_deg)

        # rotaciona base_forward em torno do eixo "up" pelo pan
        right = np.cross(base_forward, up)
        right = right / np.linalg.norm(right)

        def rotate(vec, axis, angle):
            axis = axis / np.linalg.norm(axis)
            return (
                vec * np.cos(angle)
                + np.cross(axis, vec) * np.sin(angle)
                + axis * np.dot(axis, vec) * (1 - np.cos(angle))
            )

        direction = rotate(base_forward, up, pan_rad)
        direction = rotate(direction, right, tilt_rad)
        direction = direction / np.linalg.norm(direction)

        locations, _, _ = self.intersector.intersects_location(
            ray_origins=[origin], ray_directions=[direction]
        )
        if len(locations) == 0:
            return None
        # ponto de impacto mais próximo da origem
        dists = np.linalg.norm(locations - origin, axis=1)
        return locations[np.argmin(dists)]

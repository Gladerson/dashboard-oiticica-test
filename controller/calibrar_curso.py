# ============================================================================
# calibrar_curso.py - Descobre o curso mecânico REAL em graus
#
# Por que: o ONVIF desta câmera reporta pan/tilt normalizados (-1..1). Isso
# diz "estou no meio do curso", mas não diz quantos graus é o curso inteiro.
# Este script move a câmera para posições normalizadas conhecidas e pede que
# você meça o ângulo físico -- assim PAN_DEG_RANGE/TILT_DEG_RANGE deixam de
# ser um palpite.
#
# Como medir (parede de teste serve perfeitamente):
#   • Marque no chão/parede para onde a câmera aponta em cada parada.
#   • Use a bússola do celular, um transferidor, ou um app de nível/ângulo.
#   • Se preferir, meça a distância entre duas marcas na parede (D) e a
#     distância câmera-parede (L): ângulo = 2*atan(D / (2*L)).
#
# Rodar:  python calibrar_curso.py
# ============================================================================
import math
import time

import config
from onvif_ptz import PTZController

ptz = PTZController(
    config.CAMERA_IP, config.ONVIF_PORT, config.ONVIF_USER, config.ONVIF_PASSWORD,
    label="calib", pan_deg_range=config.PAN_DEG_RANGE, tilt_deg_range=config.TILT_DEG_RANGE,
)

print(ptz.describe())
print()

if not ptz.pan_normalized and not ptz.tilt_normalized:
    print("Esta câmera já reporta pan/tilt em GRAUS diretamente.")
    print("Nenhuma calibração é necessária -- deixe PAN_DEG_RANGE/TILT_DEG_RANGE como estão.")
    raise SystemExit(0)


def mover_raw(raw_pan, raw_tilt):
    """Move usando valores normalizados crus, sem passar pela conversão."""
    req = ptz.ptz.create_type('AbsoluteMove')
    req.ProfileToken = ptz.profile_token
    req.Position = {
        'PanTilt': {'x': raw_pan, 'y': raw_tilt},
        'Zoom': {'x': ptz.zoom_min},
    }
    ptz.ptz.AbsoluteMove(req)
    time.sleep(4)


def perguntar_float(texto):
    while True:
        try:
            return float(input(texto).replace(",", "."))
        except ValueError:
            print("  Digite um número (ex: 72.5)")


print("=" * 70)
print("CALIBRAÇÃO DO PAN")
print("=" * 70)
mover_raw(-0.5, 0.0)
input("Câmera em pan_raw = -0.5. Marque a direção e tecle ENTER...")
mover_raw(0.5, 0.0)
input("Câmera em pan_raw = +0.5. Marque a segunda direção e tecle ENTER...")

ang_pan = perguntar_float("Ângulo FÍSICO entre as duas marcas, em graus: ")
# -0.5 -> +0.5 percorre 1.0 de faixa normalizada = 1x pan_deg_range
pan_range = ang_pan
print(f"\n  >> PAN_DEG_RANGE = {pan_range:.1f}")
print(f"     (curso total de ponta a ponta: {2 * pan_range:.1f}°)")

print()
print("=" * 70)
print("CALIBRAÇÃO DO TILT")
print("=" * 70)
mover_raw(0.0, -0.5)
input("Câmera em tilt_raw = -0.5. Marque a inclinação e tecle ENTER...")
mover_raw(0.0, 0.5)
input("Câmera em tilt_raw = +0.5. Marque a segunda inclinação e tecle ENTER...")

ang_tilt = perguntar_float("Ângulo FÍSICO entre as duas inclinações, em graus: ")
tilt_range = ang_tilt
print(f"\n  >> TILT_DEG_RANGE = {tilt_range:.1f}")
print(f"     (curso total: {2 * tilt_range:.1f}°)")

mover_raw(0.0, 0.0)

print()
print("=" * 70)
print("Adicione ao controller/.env:")
print("=" * 70)
print(f"PAN_DEG_RANGE={pan_range:.1f}")
print(f"TILT_DEG_RANGE={tilt_range:.1f}")
# ============================================================================
# migrar_dispositivo_legado.py - Cadastra, uma unica vez, a localidade e o
# dispositivo que ANTES eram os unicos hardcoded no processo (server.py
# tinha CAMERA_LAT/CAMERA_LON/CAMERA_ALT_ABOVE_GROUND e glb_geo.py tinha
# UTM_ZONE/UTM_HEMISPHERE_SOUTH/GEO_OFFSET_*/MODEL_UP_AXIS como constantes
# de modulo).
#
# Por que isto precisa existir: a etapa "multi-dispositivo" exige token de
# TODO dispositivo, sem excecao -- inclusive o Raspberry que ja esta em
# producao (decisao explicita do usuario, "Exigir token de todo mundo desde
# ja"). Sem rodar este script, esse Pi para de conseguir falar com o
# servidor assim que o novo server.py/borda.py entrarem no ar: ele nao tem
# token nenhum e a autenticacao Bearer virou obrigatoria em /api/edge/*.
#
# O que o script faz:
#   1. Cria (ou reaproveita, se ja existir) a localidade "Barragem Oiticica"
#      com os MESMOS parametros geograficos que estavam hardcoded, e aponta
#      modelo_3d_path pro static/model.glb JA EXISTENTE (ja descomprimido
#      via Draco em producao -- reaproveita, nao reenvia nada) marcando
#      modelo_status='pronto' direto, sem passar pela fila de decompressao.
#   2. Cria o dispositivo (ou avisa que ja existe, sem duplicar) com um
#      token novo, dono = usuario admin.
#   3. Imprime o token gerado e um trecho pronto para colar em edge/.env.
#
# Rode do jeito que o server roda hoje, com as variaveis de ambiente do
# server/.env carregadas (precisa do DATABASE_URL certo):
#   cd server && python3 migrar_dispositivo_legado.py
# ============================================================================
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import db  # noqa: E402

LOCALIDADE_NOME = "Barragem Oiticica"

# Os mesmos valores que estavam hardcoded em server.py (CAMERA_LAT/LON/
# CAMERA_ALT_ABOVE_GROUND) e glb_geo.py (UTM_ZONE/UTM_HEMISPHERE_SOUTH/
# GEO_OFFSET_*/MODEL_UP_AXIS) antes da etapa multi-dispositivo.
CAMERA_LAT = -6.152425824994227
CAMERA_LON = -37.12619639369007
CAMERA_ALT_ACIMA_SOLO = 7.0  # metros

UTM_ZONE = 24
UTM_HEMISFERIO_SUL = True
GEO_OFFSET_X = 707543.0
GEO_OFFSET_Y = 9319434.0
GEO_OFFSET_Z = 0.0
MODEL_UP_AXIS = "Z"

MODELO_3D_PATH = "static/model.glb"  # ja existente, ja descomprimido

DISPOSITIVO_NOME = "Câmera Torre — Barragem Oiticica (legado)"
DEVICE_ID_LEGADO = "oiticica-cam-01"  # mesmo DEVICE_ID que o Pi ja usa hoje


def _obter_admin():
    admin = db.usuario_por_username(db.ADMIN_USER_PADRAO)
    if admin is not None:
        return admin
    # Sem 'admin' (renomeado?): pega o primeiro usuario com papel admin.
    for u in db.listar_usuarios():
        if u["papel"] == "admin":
            return u
    raise SystemExit(
        "ERRO: nenhum usuario admin encontrado. Rode o servidor uma vez "
        "primeiro (ele semeia o admin padrao) e tente de novo."
    )


def _obter_ou_criar_localidade():
    for loc in db.listar_localidades():
        if loc["nome"] == LOCALIDADE_NOME:
            print(f">> Localidade '{LOCALIDADE_NOME}' ja existe (id={loc['id']}), reaproveitando.")
            return loc
    loc = db.criar_localidade(
        LOCALIDADE_NOME, UTM_ZONE, UTM_HEMISFERIO_SUL,
        GEO_OFFSET_X, GEO_OFFSET_Y, GEO_OFFSET_Z, MODEL_UP_AXIS,
    )
    # O modelo ja existe em disco e ja foi descomprimido em producao: aponta
    # direto pra 'pronto', sem passar pela fila de decompressao Draco
    # (dispositivos.py._descomprimir_draco), que reenviaria/reprocessaria
    # um arquivo que ja esta correto.
    db.definir_modelo_localidade(loc["id"], MODELO_3D_PATH)
    db.atualizar_status_modelo(loc["id"], "pronto")
    print(f">> Localidade '{LOCALIDADE_NOME}' criada (id={loc['id']}), "
          f"modelo apontado para '{MODELO_3D_PATH}' e marcado como pronto.")
    return db.localidade_por_id(loc["id"])


def _obter_ou_criar_dispositivo(localidade, admin):
    for d in db.listar_dispositivos():
        if d["nome"] == DISPOSITIVO_NOME:
            print(f">> Dispositivo '{DISPOSITIVO_NOME}' ja existe (id={d['id']}). "
                  f"Nao crio de novo -- se precisar de um token novo, exclua-o em "
                  f"/dispositivos e rode este script outra vez.")
            return d, False
    slug = "oiticica-cam-01"
    novo = db.criar_dispositivo(
        entity_id=f"urn:ngsi-ld:CV-SHM:{slug}",
        entity_type="CV-SHM",
        nome=DISPOSITIVO_NOME,
        proprietario=None,
        localidade_id=localidade["id"],
        lat=CAMERA_LAT, lon=CAMERA_LON, alt_acima_solo=CAMERA_ALT_ACIMA_SOLO,
        transporte="http",
        token=secrets.token_urlsafe(24),
        topico_telemetria="v1/devices/me/telemetry",
        topico_atributos="v1/devices/me/attributes",
        topico_frame=f"oiticica/{slug}/frame",
        dono_usuario_id=admin["id"],
    )
    print(f">> Dispositivo '{DISPOSITIVO_NOME}' criado (id={novo['id']}).")
    return novo, True


def main():
    db.iniciar()
    admin = _obter_admin()
    localidade = _obter_ou_criar_localidade()
    dispositivo, criado = _obter_ou_criar_dispositivo(localidade, admin)

    print()
    print("=" * 78)
    if not criado:
        print("Dispositivo ja cadastrado -- nada novo a fazer. Se o Raspberry ainda")
        print("nao tem DEVICE_TOKEN configurado, pegue o token em /dispositivos no")
        print("painel (a coluna 'token' so aparece pra quem tem acesso de admin).")
    else:
        print("Cole isto em edge/.env no Raspberry (mantenha o resto do arquivo):")
        print()
        print(f"DEVICE_ID={DEVICE_ID_LEGADO}")
        print(f"DEVICE_TOKEN={dispositivo['token']}")
        print()
        print("Depois reinicie o servico do agente de borda no Pi, por exemplo:")
        print("  sudo systemctl restart oiticica-borda")
        print("(ou o nome do servico que voce configurou -- ver README.)")
    print("=" * 78)


if __name__ == "__main__":
    main()

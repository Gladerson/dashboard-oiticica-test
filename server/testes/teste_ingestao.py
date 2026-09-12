#!/usr/bin/env python3
"""Confere a separação do payload, o ROTEAMENTO e a derivação na ingestão.

Roda sem banco: as funções de `db` que a ingestão usa são substituídas por
versões de mentira.

    python3 server/testes/teste_ingestao.py
"""
import os
import sys

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, RAIZ)

import db          # noqa: E402
import telemetria  # noqa: E402

falhas = []


def cheque(cond, msg):
    print(("  ok    " if cond else "  FALHA ") + msg)
    if not cond:
        falhas.append(msg)


OITICICA = {
    "dist_ref": 8.10, "cota_ref": 114.68, "volume_ref": 742632840.34,
    "dist_vazio": 30.00, "cota_vazio": 92.00,
    "cota_vert_1": 114.68, "cota_maximorum": 118.00, "chave_bruta": "distancia",
}

GW = "gw-1"
SENSOR = "sen-1"

# O cadastro: um sensor "sen-1" que fala pelo gateway "gw-1" como "nivel-rd01".
CADASTRADOS = [
    {"id": SENSOR, "proprio": False, "sub_id": "nivel-rd01",
     "subtipo": "radar_nivel", "config": OITICICA},
]
db.sensores_de = lambda _id: list(CADASTRADOS)


def ingerir(payload, e_gateway=True, conhecidos=(), explicitos=None):
    """O mesmo caminho de edge_dados(), sem FastAPI nem banco."""
    amostras = telemetria.separar(payload, e_gateway, conhecidos, explicitos)
    amostras = [a for a in amostras if a[1]]
    return telemetria.distribuir_amostras(GW, amostras)


def mapa(por_dispositivo, alvo, sub=""):
    return {c: (n if n is not None else t)
            for (s, c, n, t) in por_dispositivo.get(alvo, []) if s == sub}


PAYLOAD = {
    "enlace": "wifi", "sinal": -58, "uptime_s": 1200,
    "envios_ok": 80, "envios_falha": 1, "trocas_de_enlace": 0,
    "segundos_no_enlace": 1200,
    "nivel-rd01": {"status": "on", "pacotes": 41, "distancia": 12.0},
}

print("== Cada telemetria vai para o seu dono ==")
por_disp, estados = ingerir(PAYLOAD, explicitos=["nivel-rd01"])
gw = mapa(por_disp, GW)
enlace_gw = mapa(por_disp, GW, "nivel-rd01")
sen = mapa(por_disp, SENSOR)

cheque(gw.get("enlace") == "wifi", f"o enlace fica no gateway ({gw})")
cheque(gw.get("uptime_s") == 1200 and gw.get("envios_ok") == 80,
       "a saúde do gateway também")
cheque(enlace_gw.get("status") == "on",
       "o STATUS do controlador fica no gateway (é ele quem sabe)")
cheque(enlace_gw.get("pacotes") == 41,
       "o contador de PACOTES também fica no gateway")
cheque("distancia" not in enlace_gw,
       "mas a MEDIDA não fica mais no gateway")
cheque("cota_atual" not in enlace_gw,
       "nem a cota derivada")

cheque(sen.get("distancia") == 12.0,
       f"a medida bruta vai para o cadastro do SENSOR ({sorted(sen)})")
cheque(abs(sen.get("cota_atual", 0) - 110.641) < 0.001,
       f"e as derivadas também (cota {sen.get('cota_atual')})")
cheque(abs(sen.get("volume_m3", 0) - 610383156) < 2, "volume derivado no sensor")
cheque("uso_percentual" in sen and "volume_rest_m3" in sen,
       "as demais derivadas idem")
cheque("status" not in sen,
       "o status NÃO é duplicado no sensor (quem responde por ele é o gateway)")
cheque(all(s == "" for (s, c, n, t) in por_disp[SENSOR]),
       "dentro do cadastro do sensor, tudo fica em sub_id vazio")

print("\n== O estado de cada controlador vem do gateway ==")
cheque(estados.get(SENSOR) == "on", f"status 'on' vira estado 'on' ({estados})")
_, e_erro = ingerir({"nivel-rd01": {"status": "erro", "pacotes": 42}},
                    explicitos=["nivel-rd01"])
cheque(e_erro.get(SENSOR) == "erro",
       "o controlador fala, mas o sensor falhou -> 'erro'")
_, e_off = ingerir({"nivel-rd01": {"status": "off"}}, explicitos=["nivel-rd01"])
cheque(e_off.get(SENSOR) == "off", "o controlador não fala -> 'off'")
_, e_sem = ingerir({"enlace": "4g"}, explicitos=["nivel-rd01"])
cheque(SENSOR not in e_sem,
       "se o gateway não falou daquele equipamento, nada é carimbado")

print("\n== Situações em que não se deve inventar número ==")
p_off, _ = ingerir({"nivel-rd01": {"status": "off"}}, explicitos=["nivel-rd01"])
cheque("cota_atual" not in mapa(p_off, SENSOR),
       "equipamento mudo não ganha cota derivada")
p_err, _ = ingerir({"nivel-rd01": {"status": "erro", "distancia": "nan"}},
                   explicitos=["nivel-rd01"])
cheque("cota_atual" not in mapa(p_err, SENSOR), "medida inválida não vira cota")

print("\n== Equipamento sem cadastro continua inteiro no gateway ==")
p_outro, est_outro = ingerir(
    {"nivel-rd99": {"status": "on", "distancia": 12.0}}, explicitos=["nivel-rd99"])
outro = mapa(p_outro, GW, "nivel-rd99")
cheque(outro.get("distancia") == 12.0,
       "a medida de um equipamento não cadastrado fica no gateway")
cheque(outro.get("status") == "on", "com o status junto")
cheque("cota_atual" not in outro, "sem derivadas (não há calibração)")
cheque(not est_outro, "e nenhum estado é carimbado para ele")
cheque(SENSOR not in p_outro, "o sensor cadastrado não recebe nada alheio")

print("\n== Sensor sem calibração ainda é roteado ==")
db.sensores_de = lambda _id: [
    {"id": SENSOR, "proprio": False, "sub_id": "nivel-rd01",
     "subtipo": None, "config": {}}]
p_sc, e_sc = ingerir({"nivel-rd01": {"status": "on", "distancia": 12.0}},
                     explicitos=["nivel-rd01"])
cheque(mapa(p_sc, SENSOR).get("distancia") == 12.0,
       "a medida vai para o sensor mesmo sem subtipo")
cheque("cota_atual" not in mapa(p_sc, SENSOR), "mas nada é derivado")
cheque(e_sc.get(SENSOR) == "on",
       "e a presença dele é carimbada -- é o que tira o sensor do 'eternamente offline'")
db.sensores_de = lambda _id: list(CADASTRADOS)

print("\n== Falha de banco não derruba a ingestão ==")


def explode(_id):
    raise RuntimeError("banco fora do ar")


db.sensores_de = explode
p_seguro, e_seguro = ingerir({"nivel-rd01": {"status": "on", "distancia": 12.0}},
                             explicitos=["nivel-rd01"])
cheque(mapa(p_seguro, GW, "nivel-rd01").get("distancia") == 12.0,
       "sem o cadastro, tudo cai no gateway (o comportamento antigo)")
cheque(not e_seguro, "e nenhum estado é inventado")
cheque(SENSOR not in p_seguro, "nada é roteado às cegas")
db.sensores_de = lambda _id: list(CADASTRADOS)

print("\n== Formato achatado (firmware antigo) continua funcionando ==")
p_ant, _ = ingerir({"nivel-rd01": "on", "nivel-rd01_distancia": 12.0})
cheque(mapa(p_ant, GW, "nivel-rd01").get("status") == "on",
       "o prefixo achatado ainda é separado corretamente")
cheque(abs(mapa(p_ant, SENSOR).get("cota_atual", 0) - 110.641) < 0.001,
       "e o roteamento + derivação funcionam igual sobre ele")

print("\n== A lista explícita de equipamentos manda ==")
armadilha = {"enlace": "wifi", "enlace_ha_s": 42,
             "nivel-rd01": {"status": "on", "distancia": 12.0}}
p_sem, _ = ingerir(armadilha)
p_com, _ = ingerir(armadilha, explicitos=["nivel-rd01"])
cheque(any(s == "enlace" for (s, c, n, t) in p_sem[GW]),
       "sem a lista, a heurística REALMENTE cria um equipamento 'enlace'")
cheque(not any(s == "enlace" for (s, c, n, t) in p_com[GW]),
       "com a lista, isso não acontece")
cheque(mapa(p_com, GW).get("enlace") == "wifi",
       "e o enlace fica onde devia: no próprio gateway")

print("\n== Sensor que fala sozinho (sem gateway) ==")
db.sensores_de = lambda _id: [
    {"id": "sen-solo", "proprio": True, "sub_id": "",
     "subtipo": "radar_nivel", "config": OITICICA}]
direto = telemetria.separar({"distancia": 19.05}, False)
p_solo, e_solo = telemetria.distribuir_amostras("sen-solo", direto)
cheque(abs(mapa(p_solo, "sen-solo").get("uso_percentual", 0) - 50.0) < 0.01,
       f"a derivação vale também sem gateway ({sorted(mapa(p_solo, 'sen-solo'))})")
cheque(not e_solo,
       "e não há estado a carimbar: quem fala é ele mesmo, o visto_em basta")

total = len(falhas)
print(f"\n{'TUDO OK' if not total else str(total) + ' FALHA(S)'}")
sys.exit(1 if total else 0)

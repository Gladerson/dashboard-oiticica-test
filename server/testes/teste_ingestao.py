#!/usr/bin/env python3
"""Confere a separação do payload e a derivação no caminho de ingestão.

Roda sem banco: `db.sensores_de` é substituída por uma versão de mentira.

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

CADASTRADOS = [
    {"sub_id": "nivel-rd01", "subtipo": "radar_nivel", "config": OITICICA},
]
db.sensores_de = lambda _id: list(CADASTRADOS)


def ingerir(payload, e_gateway=True, conhecidos=(), explicitos=None):
    """O mesmo caminho de edge_dados(), sem FastAPI nem banco."""
    amostras = telemetria.separar(payload, e_gateway, conhecidos, explicitos)
    amostras = [a for a in amostras if a[1]]
    amostras += telemetria.derivar_amostras("gw-1", amostras)
    return amostras


def mapa(amostras, sub):
    return {c: (n if n is not None else t) for (s, c, n, t) in amostras if s == sub}


print("== Payload do gateway, formato aninhado ==")
am = ingerir({
    "enlace": "wifi",
    "nivel-rd01": {"status": "on", "distancia": 12.0, "pacotes": 41},
})
rd = mapa(am, "nivel-rd01")
gw = mapa(am, "")
cheque(gw.get("enlace") == "wifi",
       f"o enlace do gateway fica no próprio gateway, sub_id vazio ({gw})")
cheque(rd.get("distancia") == 12.0, "a medida bruta é gravada como veio")
cheque(rd.get("status") == "on", "o status do equipamento continua chegando")
cheque(abs(rd.get("cota_atual", 0) - 110.641) < 0.001,
       f"o SERVIDOR derivou a cota (deu {rd.get('cota_atual')})")
cheque(abs(rd.get("volume_m3", 0) - 610383156) < 2,
       f"o servidor derivou o volume (deu {rd.get('volume_m3')})")
cheque("uso_percentual" in rd and "volume_rest_m3" in rd and "cota_restante" in rd,
       "as demais derivadas também aparecem")
cheque(rd.get("alerta_revanche") == 0.0,
       "booleano derivado vira número (e texto) para servir aos dois usos")
cheque(any(s == "nivel-rd01" and c == "alerta_revanche" and t == "false"
           for (s, c, n, t) in am),
       "o mesmo booleano também é gravado como texto")

print("\n== O gateway não precisa mais calcular ==")
cheque(len([1 for (s, c, n, t) in am if s == "nivel-rd01"]) > 5,
       "de um único valor bruto saem várias telemetrias")
so_bruto = ingerir({"nivel-rd01": {"status": "on", "distancia": 12.0}})
cheque("cota_atual" in mapa(so_bruto, "nivel-rd01"),
       "basta status + distancia para a tela ficar completa")

print("\n== Situações em que não se deve inventar número ==")
sem_medida = ingerir({"nivel-rd01": {"status": "off"}})
cheque("cota_atual" not in mapa(sem_medida, "nivel-rd01"),
       "equipamento mudo não ganha cota derivada")
com_erro = ingerir({"nivel-rd01": {"status": "erro", "distancia": "nan"}})
cheque("cota_atual" not in mapa(com_erro, "nivel-rd01"),
       "medida inválida não vira cota")
outro = ingerir({"nivel-rd99": {"status": "on", "distancia": 12.0}})
cheque("cota_atual" not in mapa(outro, "nivel-rd99"),
       "equipamento sem cadastro fica só com a medida bruta")
cheque(mapa(outro, "nivel-rd99").get("distancia") == 12.0,
       "e a medida bruta dele continua sendo gravada")

print("\n== Falha de banco não derruba a ingestão ==")


def explode(_id):
    raise RuntimeError("banco fora do ar")


db.sensores_de = explode
seguro = ingerir({"nivel-rd01": {"status": "on", "distancia": 12.0}})
cheque(mapa(seguro, "nivel-rd01").get("distancia") == 12.0,
       "sem o cadastro, a medida bruta ainda é gravada")
cheque("cota_atual" not in mapa(seguro, "nivel-rd01"),
       "e nenhuma derivada é inventada")
db.sensores_de = lambda _id: list(CADASTRADOS)

print("\n== Formato achatado (firmware antigo) continua funcionando ==")
antigo = ingerir({"nivel-rd01": "on", "nivel-rd01_distancia": 12.0})
cheque(mapa(antigo, "nivel-rd01").get("distancia") == 12.0,
       "o prefixo achatado ainda é separado corretamente")
cheque(abs(mapa(antigo, "nivel-rd01").get("cota_atual", 0) - 110.641) < 0.001,
       "e a derivação funciona igual sobre ele")

print("\n== A lista explícita de equipamentos manda ==")
# O gateway diz quem são seus equipamentos. Sem isso, a heurística de prefixo
# transformava a telemetria do PRÓPRIO gateway em equipamento: "enlace" é
# prefixo de "enlace_ha_s".
armadilha = {"enlace": "wifi", "enlace_ha_s": 42,
             "nivel-rd01": {"status": "on", "distancia": 12.0}}
sem_lista = ingerir(armadilha)
com_lista = ingerir(armadilha, explicitos=["nivel-rd01"])
cheque(any(s == "enlace" for (s, c, n, t) in sem_lista),
       "sem a lista, a heurística REALMENTE cria um equipamento 'enlace'")
cheque(not any(s == "enlace" for (s, c, n, t) in com_lista),
       "com a lista, isso não acontece")
cheque(mapa(com_lista, "").get("enlace") == "wifi",
       "e o enlace fica onde devia: no próprio gateway")
cheque(mapa(com_lista, "").get("enlace_ha_s") == 42,
       "a chave parecida também")
cheque(abs(mapa(com_lista, "nivel-rd01").get("cota_atual", 0) - 110.641) < 0.001,
       "o equipamento de verdade continua sendo derivado")

aninhado_extra = {"nao-listado": {"status": "on", "leitura": 5.0}}
cheque(any(s == "nao-listado" for (s, c, n, t) in
           ingerir(aninhado_extra, explicitos=["nivel-rd01"])),
       "um objeto aninhado é equipamento mesmo fora da lista (não há outra leitura)")

print("\n== Sensor que fala sozinho (sem gateway) ==")
db.sensores_de = lambda _id: [
    {"sub_id": "", "subtipo": "radar_nivel", "config": OITICICA}]
direto = telemetria.separar({"distancia": 19.05}, False)
direto += telemetria.derivar_amostras("sen-1", direto)
cheque(abs(mapa(direto, "").get("uso_percentual", 0) - 50.0) < 0.01,
       f"a derivação vale também sem gateway ({mapa(direto, '')})")

total = len(falhas)
print(f"\n{'TUDO OK' if not total else str(total) + ' FALHA(S)'}")
sys.exit(1 if total else 0)

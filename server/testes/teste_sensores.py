#!/usr/bin/env python3
"""Confere as contas de sensores.py com os números reais da Barragem Oiticica.

Roda sem banco, sem servidor e sem rede:

    python3 server/testes/teste_sensores.py

Os valores de calibração vieram dos engenheiros da barragem (setembro/2026) e
estão reproduzidos aqui de propósito: se alguém mexer na interpolação, o teste
falha com o número de cota e de volume que a operação espera ver na tela.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import sensores  # noqa: E402

falhas = []


def cheque(cond, msg):
    print(("  ok    " if cond else "  FALHA ") + msg)
    if not cond:
        falhas.append(msg)


def perto(a, b, tol):
    return a is not None and abs(a - b) <= tol


# Calibração de Oiticica, como os engenheiros entregaram.
OITICICA = {
    "dist_ref": 8.10,
    "cota_ref": 114.68,
    "volume_ref": 742632840.34,
    "dist_vazio": 30.00,
    "cota_vazio": 92.00,
    "cota_vert_1": 114.68,
    "cota_vert_2": 115.53,
    "cota_maximorum": 118.00,
    "cota_fundo": 67.00,
    "chave_bruta": "distancia",
}


def d(valores):
    return dict(sensores.derivar("radar_nivel", OITICICA, valores))


print("== Radar de nível: as duas âncoras ==")
a = d({"distancia": 8.10})
cheque(perto(a.get("cota_atual"), 114.68, 0.001),
       f"na distância 8,10 m a cota é 114,68 (deu {a.get('cota_atual')})")
cheque(perto(a.get("volume_m3"), 742632840.0, 1.0),
       f"na distância 8,10 m o volume é o de referência (deu {a.get('volume_m3')})")
cheque(perto(a.get("uso_percentual"), 100.0, 0.01),
       f"na âncora cheia o percentual é 100 (deu {a.get('uso_percentual')})")

b = d({"distancia": 30.00})
cheque(perto(b.get("cota_atual"), 92.00, 0.001),
       f"na distância 30 m a cota é 92,00 (deu {b.get('cota_atual')})")
cheque(perto(b.get("volume_m3"), 0.0, 1.0),
       f"na distância 30 m o volume útil é zero (deu {b.get('volume_m3')})")
cheque(perto(b.get("uso_percentual"), 0.0, 0.01),
       f"na âncora vazia o percentual é 0 (deu {b.get('uso_percentual')})")

print("\n== Interpolação no meio ==")
# Meio do caminho entre 8,10 e 30,00 = 19,05 m.
m = d({"distancia": 19.05})
cheque(perto(m.get("uso_percentual"), 50.0, 0.01),
       f"na metade da faixa o percentual é 50 (deu {m.get('uso_percentual')})")
cheque(perto(m.get("cota_atual"), (114.68 + 92.00) / 2, 0.001),
       f"na metade da faixa a cota é a média das âncoras (deu {m.get('cota_atual')})")
cheque(perto(m.get("volume_m3"), 742632840.34 / 2, 1.0),
       f"na metade da faixa o volume é a metade (deu {m.get('volume_m3')})")

# Um ponto arbitrário, conferido por conta independente.
x = 12.00
fracao = (30.00 - x) / (30.00 - 8.10)
cota_esperada = 114.68 + (x - 8.10) * (92.00 - 114.68) / (30.00 - 8.10)
p = d({"distancia": x})
cheque(perto(p.get("cota_atual"), cota_esperada, 0.001),
       f"distância 12 m -> cota {cota_esperada:.3f} (deu {p.get('cota_atual')})")
cheque(perto(p.get("volume_m3"), fracao * 742632840.34, 1.0),
       f"distância 12 m -> volume {fracao * 742632840.34:.0f} (deu {p.get('volume_m3')})")
cheque(perto(p.get("volume_rest_m3"),
             742632840.34 - fracao * 742632840.34, 1.0),
       "o volume restante fecha com o total de referência")
cheque(perto(p.get("cota_restante"), 118.00 - cota_esperada, 0.001),
       "a cota restante é medida até a maximorum")

print("\n== Vertimento e revanche ==")
cheque(d({"distancia": 8.10}).get("vertendo") is True,
       "na cota de referência já está vertendo (114,68 é o 1º vertimento)")
cheque(d({"distancia": 9.00}).get("vertendo") is False,
       "abaixo da cota de vertimento, 'vertendo' é falso")
cheque(d({"distancia": 12.00}).get("alerta_revanche") is False,
       "longe da maximorum não há alerta de revanche")
# Para chegar a 118 m a distância teria de ser menor que a âncora cheia.
dist_118 = 8.10 + (118.00 - 114.68) * (30.00 - 8.10) / (92.00 - 114.68)
alto = d({"distancia": dist_118})
cheque(perto(alto.get("cota_atual"), 118.00, 0.001),
       f"a cota maximorum é alcançável por extrapolação (deu {alto.get('cota_atual')})")
cheque(alto.get("alerta_revanche") is True,
       "na cota maximorum o alerta de revanche dispara")

print("\n== Fora das âncoras ==")
acima = d({"distancia": 5.00})
cheque(acima.get("cota_atual") > 114.68,
       f"acima da âncora a COTA extrapola (deu {acima.get('cota_atual')})")
cheque(perto(acima.get("volume_m3"), 742632840.0, 1.0),
       "acima da âncora o VOLUME fica travado no de referência")
cheque(perto(acima.get("uso_percentual"), 100.0, 0.01),
       "acima da âncora o percentual não passa de 100")
cheque(acima.get("acima_da_referencia") is True,
       "acima da âncora a telemetria avisa que o volume é um piso")
cheque("acima_da_referencia" not in d({"distancia": 12.00}),
       "dentro da faixa esse aviso nem aparece")

abaixo = d({"distancia": 35.00})
cheque(abaixo.get("cota_atual") < 92.00,
       f"abaixo da âncora vazia a cota continua caindo (deu {abaixo.get('cota_atual')})")
cheque(perto(abaixo.get("volume_m3"), 0.0, 1.0),
       "abaixo da âncora vazia o volume útil não fica negativo")

print("\n== Entradas ruins não derrubam a ingestão ==")
cheque(sensores.derivar("radar_nivel", OITICICA, {}) == [],
       "sem a medida bruta, nada é publicado")
cheque(sensores.derivar("radar_nivel", OITICICA, {"distancia": "abc"}) == [],
       "medida não numérica não vira cota")
cheque(sensores.derivar("radar_nivel", OITICICA, {"distancia": float("nan")}) == [],
       "NaN não vira cota")
cheque(sensores.derivar("radar_nivel", {}, {"distancia": 12.0}) == [],
       "sem calibração, nada é publicado")
cheque(sensores.derivar("radar_nivel", dict(OITICICA, dist_vazio=5.0),
                        {"distancia": 12.0}) == [],
       "âncoras invertidas não publicam número sem sentido")
cheque(sensores.derivar("desconhecido", OITICICA, {"distancia": 12.0}) == [],
       "subtipo desconhecido devolve lista vazia, não exceção")
cheque(sensores.derivar(None, None, None) == [],
       "chamada com tudo vazio não explode")
cheque(sensores.derivar("piezometro", {}, {"leitura": 3.2}) == [],
       "piezômetro ainda não deriva nada (e diz isso sem quebrar)")

print("\n== A medida bruta pode ter outro nome ==")
outro = dict(OITICICA, chave_bruta="dist_m")
cheque(dict(sensores.derivar("radar_nivel", outro, {"dist_m": 19.05}))
       .get("uso_percentual") == 50.0,
       "a chave da medida bruta é configurável")
cheque(sensores.derivar("radar_nivel", outro, {"distancia": 19.05}) == [],
       "com a chave configurada, a antiga deixa de ser lida")

print("\n== Conferência da calibração ==")
avisos = sensores.conferir("radar_nivel", OITICICA)
cheque(len(avisos) == 1, f"a calibração de Oiticica gera 1 aviso (deu {len(avisos)})")
cheque(avisos and "122.78" in avisos[0] and "122.00" in avisos[0],
       "o aviso mostra as duas alturas implícitas do radar")
cheque(avisos and "0.78" in avisos[0],
       "o aviso mostra a diferença de 78 cm entre as âncoras")
print("     aviso:", avisos[0] if avisos else "(nenhum)")

coerente = dict(OITICICA, dist_vazio=30.00, cota_vazio=92.78)
cheque(sensores.conferir("radar_nivel", coerente) == [],
       "com as âncoras coerentes (cota 92,78) não há aviso nenhum")

cheque(any("obrigatórios" in a for a in sensores.conferir("radar_nivel", {})),
       "calibração vazia reclama dos campos obrigatórios")
cheque(any("MAIOR" in a for a in
           sensores.conferir("radar_nivel", dict(OITICICA, dist_vazio=5.0))),
       "âncoras invertidas são apontadas")

print("\n== Catálogo ==")
pub = sensores.subtipos_publicos()
cheque(any(s["chave"] == "radar_nivel" and s["implementado"] for s in pub),
       "o radar de nível aparece como implementado")
cheque(any(s["chave"] == "piezometro" and not s["implementado"] for s in pub),
       "o piezômetro aparece como ainda não implementado")
cheque(all(isinstance(s["campos"], list) for s in pub),
       "todo subtipo declara a lista de campos")
obrigatorios = [c["chave"] for c in sensores.CAMPOS_RADAR_NIVEL if c.get("obrigatorio")]
cheque(obrigatorios == ["dist_ref", "cota_ref", "volume_ref",
                        "dist_vazio", "cota_vazio"],
       f"os obrigatórios do radar são as duas âncoras e o volume ({obrigatorios})")

total = len(falhas)
print(f"\n{'TUDO OK' if not total else str(total) + ' FALHA(S)'}")
sys.exit(1 if total else 0)

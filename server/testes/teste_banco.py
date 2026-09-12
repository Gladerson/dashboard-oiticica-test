#!/usr/bin/env python3
"""Confere o SQL contra um PostgreSQL DE VERDADE.

POR QUE ESTE TESTE EXISTE
-------------------------
teste_sensores.py e teste_ingestao.py rodam sem banco: exercitam contas e
roteamento em memoria. Isso deixa um buraco -- o SQL em si nunca e executado.
Foi exatamente onde dois defeitos passaram:

  * um ALTER TABLE colocado ANTES do CREATE TABLE da mesma tabela. Em quem ja
    tinha o banco criado nao dava nada; num banco NOVO o SCHEMA inteiro
    abortava e o servidor nao subia;
  * a reamostragem por baldes de tempo de `serie_completa()`, que so tem como
    ser conferida rodando.

COMO RODAR (banco descartavel, nunca o de producao)
---------------------------------------------------
    initdb -D /tmp/pg/data -U hydro --auth=trust
    pg_ctl -D /tmp/pg/data -o '-k /tmp/pg -p 55432 -c listen_addresses=' -w start
    createdb -h /tmp/pg -p 55432 -U hydro hydro_teste

    DATABASE_URL="postgresql://hydro@/hydro_teste?host=/tmp/pg&port=55432" \
        python3 server/testes/teste_banco.py

O teste APAGA linhas. Por isso ele se recusa a rodar se o nome do banco nao
contiver "teste" -- ver `_recusar_producao()` abaixo. Nao remova essa trava.
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

falhas = []


def cheque(cond, msg):
    print(("  ok    " if cond else "  FALHA ") + msg)
    if not cond:
        falhas.append(msg)


def _recusar_producao():
    """Trava de seguranca. Este teste insere e apaga; apontado para o banco
    de producao ele destruiria historico. A regra e simples e verificavel de
    fora: o nome do banco precisa dizer que e de teste."""
    url = os.getenv("DATABASE_URL", "")
    if not url:
        sys.exit("Defina DATABASE_URL apontando para um banco DESCARTAVEL.\n"
                 "Veja o cabecalho deste arquivo.")
    # O nome do banco e o ultimo pedaco do caminho, antes da query string.
    nome = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    if "teste" not in nome.lower():
        sys.exit(f"RECUSADO: o banco '{nome}' nao parece de teste.\n"
                 "Este teste APAGA linhas. Use um banco descartavel cujo nome\n"
                 "contenha 'teste' (ex.: hydro_teste).")
    return nome


nome_banco = _recusar_producao()
print(f"banco de teste: {nome_banco}\n")

import db  # noqa: E402


# ===========================================================================
print("== O esquema sobe do ZERO ==")
# Nao e detalhe: quem instala o HydroConecta pela primeira vez passa por aqui,
# e uma falha deixa o servidor sem subir com uma mensagem de banco.
db.iniciar()
cheque(True, "db.iniciar() criou o esquema inteiro sem erro")

with db.pool.connection() as c:
    cols = {r["column_name"] for r in c.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'dispositivos'").fetchall()}
cheque({"visto_em", "subtipo", "config_sensor", "gateway_id", "sub_id",
        "estado_reportado"} <= cols,
       "as colunas de sensor/gateway/presenca existem")

with db.pool.connection() as c:
    chk = c.execute(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
        "WHERE conname = 'widgets_tipo_check'").fetchone()
cheque(chk is not None and "equipamentos" in chk["d"],
       "o CHECK de widgets aceita o tipo 'equipamentos'")

db.iniciar()
cheque(True, "rodar db.iniciar() de novo e inofensivo (migracao idempotente)")


# ===========================================================================
print("\n== Preparo ==")
with db.pool.connection() as c:
    c.execute("DELETE FROM dispositivos WHERE entity_id = 'teste-serie'")
dev = db.criar_dispositivo("teste-serie", "CV-SHM", "Equipamento de teste", "op",
                           None, None, None, None, "http", "token-teste-serie",
                           None, None, None, None, tipo="gateway")["id"]
agora = dt.datetime.now(dt.timezone.utc)


def semear(chave, pontos):
    """pontos = [(segundos_atras, valor)]"""
    with db.pool.connection() as c:
        for atras, v in pontos:
            c.execute(
                "INSERT INTO telemetria (dispositivo_id, sub_id, chave, valor_num, em) "
                "VALUES (%s, '', %s, %s, %s)",
                (dev, chave, v, agora - dt.timedelta(seconds=atras)))


print("  dispositivo de teste criado")


# ===========================================================================
print("\n== serie_completa: nada gravado ==")
linhas, passo = db.serie_completa(dev, "", "nao_existe")
cheque(linhas == [] and passo == 0.0,
       "telemetria que nunca chegou devolve ([], 0.0) em vez de estourar")


print("\n== serie_completa: faixa curta, sem reamostragem ==")
semear("curta", [(i, float(i)) for i in range(30)])
linhas, passo = db.serie_completa(dev, "", "curta")
cheque(passo == 0.0, f"30 s de historia nao viram baldes (passo={passo})")
cheque(len(linhas) == 30, f"os 30 pontos voltam inteiros (deu {len(linhas)})")
cheque([r["em"] for r in linhas] == sorted(r["em"] for r in linhas),
       "e em ordem, do mais antigo para o mais novo -- a ordem que o grafico usa")


print("\n== serie_completa: faixa longa, baldes de media ==")
# 1200 leituras, uma por minuto: 20 h de historia.
semear("longa", [(i * 60, float(i)) for i in range(1200)])
linhas, passo = db.serie_completa(dev, "", "longa", pontos=600)
cheque(passo > 1.0, f"20 h de historia sao reamostradas (passo={passo:.1f} s)")
cheque(abs(passo - (1199 * 60) / 600.0) < 0.01,
       f"o passo e a duracao dividida pelo numero de pontos (deu {passo:.3f})")
cheque(550 <= len(linhas) <= 610,
       f"o desenho recebe ~600 pontos, nao 1200 (deu {len(linhas)})")
cheque(all(r["valor_num"] is not None for r in linhas), "todo balde tem media")
cheque([r["em"] for r in linhas] == sorted(r["em"] for r in linhas),
       "os baldes saem em ordem de tempo")

# A media de um balde, conferida por uma consulta independente.
with db.pool.connection() as c:
    alvo = linhas[len(linhas) // 2]
    esperado = c.execute(
        "SELECT avg(valor_num) AS m FROM telemetria "
        "WHERE dispositivo_id = %s AND sub_id = '' AND chave = 'longa' "
        "  AND em >= %s AND em < %s",
        (dev, alvo["em"], alvo["em"] + dt.timedelta(seconds=passo))).fetchone()["m"]
cheque(esperado is not None and abs(float(alvo["valor_num"]) - float(esperado)) < 1e-6,
       "a media de um balde confere com a media das leituras daquele intervalo")


print("\n== Equipamento parado vira VAO no grafico, nao linha reta ==")
# Isto e requisito de operacao: um sensor que ficou mudo tres dias tem de
# APARECER mudo. Se os baldes vazios virassem pontos, o desenho mentiria.
semear("vao", [(i * 60, 1.0) for i in range(10)]
       + [(i * 60, 2.0) for i in range(600, 610)])
linhas, passo = db.serie_completa(dev, "", "vao", pontos=100)
cheque(passo > 1.0, "a faixa com vao tambem e reamostrada")
cheque(len(linhas) < 60,
       f"os baldes vazios nao viram pontos (deu {len(linhas)} de 100)")
saltos = [(linhas[i + 1]["em"] - linhas[i]["em"]).total_seconds()
          for i in range(len(linhas) - 1)]
cheque(saltos and max(saltos) > passo * 2,
       "ha um salto maior que um balde -- o silencio aparece no desenho")


print("\n== Texto puro fica fora da media ==")
with db.pool.connection() as c:
    c.execute("INSERT INTO telemetria (dispositivo_id, sub_id, chave, valor_txt, em) "
              "VALUES (%s, '', 'misto', 'wifi', %s)", (dev, agora))
semear("misto", [(i * 60, float(i)) for i in range(700)])
linhas, _ = db.serie_completa(dev, "", "misto")
cheque(all(r["valor_num"] is not None for r in linhas),
       "uma leitura de texto ('wifi') nao entra no grafico de area")


print("\n== A janela curta continua como era ==")
curta = db.serie_telemetria(dev, "", "longa", desde_minutos=60)
cheque(0 < len(curta) <= 61, f"60 min traz ~60 leituras (deu {len(curta)})")
cheque([r["em"] for r in curta] == sorted(r["em"] for r in curta),
       "e tambem em ordem crescente")
cheque(len(curta) < len(db.serie_completa(dev, "", "longa")[0]),
       "a janela curta mostra MENOS que 'tudo' -- que e o ponto de existirem as duas")


with db.pool.connection() as c:
    c.execute("DELETE FROM dispositivos WHERE id = %s", (dev,))

print(f"\n{'TUDO OK' if not falhas else str(len(falhas)) + ' FALHA(S)'}")
sys.exit(1 if falhas else 0)

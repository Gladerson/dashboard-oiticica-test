"""
Telemetria de sensores e gateways (ESP32), widgets e alarmes.

Dois formatos de equipamento
----------------------------
* SENSOR: fala por si mesmo. O payload traz as telemetrias dele e ponto:

      {"distancia": 12.5, "bateria_v": 3.7}

* GATEWAY: recebe por LoRa de N controladores e reenvia tudo junto, numa
  mensagem so. O firmware atual (ESP32_gateway_LoRa.ino) achata os nomes:

      {"nivel-rd01": "on",
       "nivel-rd01_distancia": 12.5,
       "nivel-rd01_cota_atual": 114.2,
       "nivel-rd02": "off"}

  Quem separa isso e o `separar()` aqui embaixo: cada equipamento remoto
  vira um `sub_id`, e o resto do nome vira a `chave`. O par (sub_id, chave)
  e o endereco de uma telemetria dentro do sistema -- e o que os widgets e
  os alarmes selecionam.

Tambem aceitamos o formato aninhado, que e mais claro e nao depende de
convencao de nome (recomendado para firmware novo):

      {"nivel-rd01": {"status": "on", "distancia": 12.5}}

Alarmes
-------
A regra e (dispositivo, sub_id, chave, condicao, limite). O evento so nasce
na BORDA da condicao -- normal -> disparado e a volta. Sem isso um sensor
que passa 10 minutos acima do limite geraria um evento por leitura e
afogaria o sininho de notificacoes.
"""
import os
import time

from fastapi import Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import auth
import db
import registro_dispositivos as rd
import sensores

# Quanto tempo de serie o grafico pede por padrao.
JANELA_PADRAO_MIN = int(os.getenv("TELEMETRIA_JANELA_MIN", "1440"))

# Valores que o gateway usa como "status" de um equipamento remoto. Servem
# de pista para descobrir que aquela chave e um sub_id, e nao uma medida.
_STATUS_CONHECIDOS = {"on", "off", "erro", "online", "offline", "ok", "falha"}

ctx_manager = None            # WebSocket dos dashboards (injetado no instalar)


# ============================================================================
# Separacao do payload
# ============================================================================
def _normalizar(valor):
    """(valor_num, valor_txt) a partir do que veio no JSON.

    Booleano vira 1/0 E "true"/"false": assim o mesmo dado serve para um
    alarme numerico ("quando maior que 0") e para um indicador de status."""
    if isinstance(valor, bool):
        return (1.0 if valor else 0.0), ("true" if valor else "false")
    if isinstance(valor, (int, float)):
        return float(valor), None
    if valor is None:
        return None, None
    txt = str(valor)
    try:                       # numero que veio como string ("12.50")
        return float(txt), None
    except ValueError:
        return None, txt


def descobrir_sub_ids(valores, conhecidos=(), explicitos=None):
    """Quais chaves do payload sao, na verdade, equipamentos remotos.

    Quando o firmware DIZ quem sao (campo "dispositivos" do payload), a lista
    dele e a resposta -- e so ela. O gateway sabe por quem fala; adivinhar em
    cima disso so cria chance de errar. Foi assim que uma telemetria do
    proprio gateway virou equipamento: `enlace` e prefixo de `enlace_ha_s`, e
    a pista (3) concluiu que existia um equipamento chamado "enlace".

    Sem lista explicita, valem tres pistas, em ordem de confianca:
      1. ja apareceu antes neste dispositivo (veio do banco);
      2. o valor e um status conhecido ("on"/"off"/"erro");
      3. a chave e prefixo de outra chave ("nivel-rd01" e "nivel-rd01_x").

    Um objeto aninhado e sempre um equipamento, com ou sem lista: a forma
    aninhada nao tem outra leitura possivel.
    """
    aninhados = {k for k, v in valores.items() if isinstance(v, dict)}
    if explicitos:
        return set(explicitos) | aninhados

    subs = set(conhecidos) | aninhados
    chaves = list(valores.keys())
    for k, v in valores.items():
        if isinstance(v, dict):
            continue
        if isinstance(v, str) and v.strip().lower() in _STATUS_CONHECIDOS:
            subs.add(k)
        elif any(o != k and o.startswith(k + "_") for o in chaves):
            subs.add(k)
    return subs


def separar(valores, e_gateway, sub_ids_conhecidos=(), explicitos=None):
    """Payload -> lista de (sub_id, chave, valor_num, valor_txt).

    Num dispositivo que nao e gateway tudo cai em sub_id '' -- ou seja, "o
    proprio dispositivo". Num gateway, o prefixo MAIS LONGO vence, senao
    dois equipamentos com nomes parecidos ('rd01' e 'rd01b') se
    embaralhariam."""
    amostras = []
    if not e_gateway:
        for chave, v in valores.items():
            if isinstance(v, dict):        # aninhado num sensor: achata
                for c2, v2 in v.items():
                    n, t = _normalizar(v2)
                    amostras.append(("", f"{chave}_{c2}", n, t))
                continue
            n, t = _normalizar(v)
            amostras.append(("", str(chave), n, t))
        return amostras

    subs = sorted(descobrir_sub_ids(valores, sub_ids_conhecidos, explicitos),
                  key=len, reverse=True)
    for chave, v in valores.items():
        chave = str(chave)
        if isinstance(v, dict):            # forma aninhada: sem adivinhacao
            for c2, v2 in v.items():
                n, t = _normalizar(v2)
                amostras.append((chave, str(c2), n, t))
            continue
        alvo, resto = "", chave
        for s in subs:
            if chave == s:
                alvo, resto = s, "status"
                break
            if chave.startswith(s + "_"):
                alvo, resto = s, chave[len(s) + 1:]
                break
        n, t = _normalizar(v)
        amostras.append((alvo, resto, n, t))
    return amostras


# ============================================================================
# Roteamento: de quem e cada telemetria que chegou
# ============================================================================
# Chaves que o gateway produz SOBRE O ENLACE com um controlador. A lista mora
# em sensores.py porque o cadastro tambem precisa dela (ver
# _limpar_catalogo_do_sensor em dispositivos.py).
CHAVES_DO_ENLACE = sensores.CHAVES_DO_ENLACE

# O que o gateway diz sobre um equipamento silencioso/com problema. Sao os
# mesmos tres valores que o firmware publica (ver montarEquipamento()).
ESTADOS_REPORTADOS = ("on", "erro", "off")

# Tipos de widget aceitos. Tem de bater com o CHECK da tabela `widgets`
# (server/db.py) e com o seletor da tela de Telemetrias.
TIPOS_WIDGET = ("area", "barras", "card", "radial", "status", "alarmes",
                "equipamentos")


def distribuir_amostras(dispositivo_id, amostras):
    """Para onde vai cada telemetria desta mensagem, e o que dizer de cada
    equipamento remoto.

    Um gateway fala POR outros equipamentos, e ate aqui TUDO era gravado sob
    o cadastro dele. O sensor cadastrado nunca recebia nada: ficava
    eternamente offline no painel, e os widgets dele ficavam vazios enquanto
    as medidas apareciam no gateway -- que e justamente onde elas nao deviam
    estar.

    Agora cada coisa vai para o seu dono:

      * o que o gateway diz de SI (enlace, sinal, uptime, envios) fica no
        gateway, em sub_id vazio;
      * o que ele diz sobre o ENLACE com um controlador (status, pacotes)
        tambem fica no gateway, sob o sub_id daquele controlador -- e
        informacao do gateway, nao do instrumento;
      * a MEDIDA bruta e tudo que se deriva dela vao para o cadastro do
        SENSOR, em sub_id vazio: la dentro ele e o proprio dispositivo.

    Um sub_id sem sensor cadastrado continua inteiro no gateway, como sempre
    foi. Ninguem perde dado por nao ter cadastrado o equipamento.

    Devolve (por_dispositivo, estados):
      por_dispositivo  {device_id: [(sub_id, chave, num, txt), ...]}
      estados          {device_id do sensor: 'on' | 'erro' | 'off'}

    Falha aqui nunca derruba a ingestao: sem o cadastro, tudo cai no gateway
    -- que e exatamente o comportamento da versao anterior."""
    try:
        configurados = db.sensores_de(dispositivo_id)
    except Exception as e:
        print(f"[telemetria] nao consegui ler os sensores de {dispositivo_id}: {e}")
        return {dispositivo_id: list(amostras)}, {}

    # sub_id -> cadastro do sensor que responde por ele
    por_sub_cadastro = {c["sub_id"]: c for c in configurados
                        if c["sub_id"] and not c["proprio"]}
    proprio = next((c for c in configurados if c["proprio"]), None)

    # O que chegou, agrupado por equipamento. O valor numerico manda; o texto
    # so quando nao ha numero (um "status": "on" nao serve de medida).
    por_sub = {}
    for sub_id, chave, num, txt in amostras:
        por_sub.setdefault(sub_id, {})[chave] = num if num is not None else txt

    por_dispositivo = {dispositivo_id: []}
    estados = {}

    for sub_id, chave, num, txt in amostras:
        cad = por_sub_cadastro.get(sub_id)
        if cad is None or chave in CHAVES_DO_ENLACE:
            por_dispositivo[dispositivo_id].append((sub_id, chave, num, txt))
            continue
        por_dispositivo.setdefault(cad["id"], []).append(("", chave, num, txt))

    # Estado de cada equipamento remoto, e as derivadas dele.
    for sub_id, cad in por_sub_cadastro.items():
        valores = por_sub.get(sub_id)
        if valores is None:
            continue     # o gateway nao falou deste equipamento nesta mensagem
        bruto = str(valores.get("status", "")).strip().lower()
        estados[cad["id"]] = bruto if bruto in ESTADOS_REPORTADOS else "on"
        for amostra in _derivadas(cad, valores):
            por_dispositivo.setdefault(cad["id"], []).append(amostra)

    # Sensor que fala sozinho: nao ha para onde rotear, so derivar.
    if proprio is not None:
        for amostra in _derivadas(proprio, por_sub.get("", {})):
            por_dispositivo[dispositivo_id].append(amostra)

    return por_dispositivo, estados


def _derivadas(cadastro, valores):
    """As telemetrias CALCULADAS a partir da medida bruta, ja no formato de
    amostra e sempre em sub_id vazio (dentro do cadastro do sensor, ele e o
    proprio dispositivo).

    Antes quem fazia estas contas era o firmware do gateway. Passaram para ca
    porque recalibrar um reservatorio nao pode exigir regravar um ESP32 a
    centenas de quilometros -- ver server/sensores.py."""
    if not valores:
        return []
    saida = []
    for chave, valor in sensores.derivar(cadastro["subtipo"],
                                         cadastro["config"], valores):
        if valor is None:
            continue
        if isinstance(valor, bool):
            saida.append(("", chave, 1.0 if valor else 0.0,
                          "true" if valor else "false"))
        else:
            saida.append(("", chave, float(valor), None))
    return saida


# ============================================================================
# Alarmes
# ============================================================================
def _condicao_bate(condicao, valor, limite):
    if valor is None:
        return False
    if condicao == "maior":
        return valor > limite
    if condicao == "menor":
        return valor < limite
    if condicao == "igual":
        # Igualdade exata em float e armadilha: 0.1+0.2 nunca e 0.3. Uma
        # tolerancia pequena e o que o operador espera de "exatamente".
        return abs(valor - limite) < 1e-9
    return False


def avaliar_alarmes(dispositivo_id, amostras):
    """Devolve os eventos criados (so os de borda). Nao levanta excecao: um
    alarme malformado nao pode derrubar a ingestao de telemetria."""
    regras = db.alarmes_ativos_de(dispositivo_id)
    if not regras:
        return []
    indice = {(s, c): n for (s, c, n, _t) in amostras if n is not None}
    eventos = []
    for r in regras:
        valor = indice.get((r["sub_id"] or "", r["chave"]))
        if valor is None:
            continue
        agora = _condicao_bate(r["condicao"], valor, r["limite"])
        if agora == r["disparado"]:
            continue                       # nada mudou: sem evento
        try:
            ev = db.registrar_evento_alarme(
                r["id"], valor, "disparado" if agora else "normalizado")
            eventos.append({**dict(ev), "titulo": r["titulo"], "chave": r["chave"],
                            "sub_id": r["sub_id"], "severidade": r["severidade"]})
        except Exception as e:
            print(f"[alarme] falha ao registrar evento de {r['id']}: {e}")
    return eventos


# ============================================================================
# Rotas
# ============================================================================
class WidgetPayload(BaseModel):
    device_id: str
    tipo: str
    titulo: str = ""
    sub_id: str = ""
    chaves: list[str] = []
    config: dict = {}
    ordem: int = 0


class WidgetEdicaoPayload(BaseModel):
    tipo: str | None = None
    titulo: str | None = None
    sub_id: str | None = None
    chaves: list[str] | None = None
    config: dict | None = None
    ordem: int | None = None


class AlarmePayload(BaseModel):
    device_id: str
    sub_id: str = ""
    chave: str
    condicao: str
    limite: float
    titulo: str = ""
    severidade: str = "alerta"


class AlarmeEdicaoPayload(BaseModel):
    sub_id: str | None = None
    chave: str | None = None
    condicao: str | None = None
    limite: float | None = None
    titulo: str | None = None
    severidade: str | None = None
    ativo: bool | None = None


def limpar_catalogos_migrados():
    """Tira do catalogo dos gateways as medidas que agora sao dos sensores.

    Roda uma vez, na subida. Sem isto, quem ja usava a versao anterior abriria
    o seletor de widgets do gateway e continuaria vendo `cota_atual`,
    `volume_m3` e companhia -- chaves que deixaram de ser gravadas ali e nunca
    mais atualizariam. A serie historica nao e tocada: ela e o registro do que
    aconteceu na barragem.

    Uma falha aqui nao pode impedir o servidor de subir: e arrumacao, nao
    funcionamento."""
    try:
        ligados = db.sensores_com_gateway()
    except Exception as e:
        print(f"[telemetria] nao consegui conferir os catalogos: {e}")
        return
    total = 0
    for l in ligados:
        try:
            total += db.limpar_catalogo_do_gateway(
                str(l["gateway_id"]), l["sub_id"], CHAVES_DO_ENLACE)
        except Exception as e:
            print(f"[telemetria] catalogo de {l['gateway_id']}/{l['sub_id']}: {e}")
    if total:
        print(f">> Catalogo: {total} chave(s) movidas do gateway para o sensor.")


# ============================================================================
# Cache da serie completa
# ============================================================================
# Varrer a serie inteira de uma telemetria e caro: um ano a cada 15 s sao
# ~2 milhoes de linhas por chave. E o grafico de "tudo" e redesenhado a cada
# telemetria que chega -- a cada 15 s, portanto.
#
# Guardar o resultado por um minuto resolve sem prejuizo nenhum: num desenho
# que cobre meses, o ultimo balde chegar um minuto atrasado nao muda nada que
# alguem consiga ver. Quem quer o instante exato usa uma janela curta, que
# nao passa por aqui.
#
# O cache e por processo e some no reinicio, que e o comportamento certo para
# algo que e so uma otimizacao.
_CACHE_SERIE = {}
CACHE_SERIE_S = 60.0
CACHE_SERIE_MAX = 200          # teto de memoria: 200 series de ~600 pontos


def _serie_completa_cache(device_id, sub_id, chave):
    agora = time.monotonic()
    chave_cache = (device_id, sub_id, chave)
    guardado = _CACHE_SERIE.get(chave_cache)
    if guardado is not None and agora - guardado[0] < CACHE_SERIE_S:
        return guardado[1], guardado[2]

    linhas, passo = db.serie_completa(device_id, sub_id, chave)

    # Limpeza simples: quando encher, joga fora o que esta vencido. Sem isso
    # um servidor com muitos equipamentos acumularia series para sempre.
    if len(_CACHE_SERIE) >= CACHE_SERIE_MAX:
        for k, v in list(_CACHE_SERIE.items()):
            if agora - v[0] >= CACHE_SERIE_S:
                _CACHE_SERIE.pop(k, None)
        if len(_CACHE_SERIE) >= CACHE_SERIE_MAX:
            _CACHE_SERIE.clear()

    _CACHE_SERIE[chave_cache] = (agora, linhas, passo)
    return linhas, passo


def instalar(app, manager=None):
    global ctx_manager
    ctx_manager = manager

    @app.on_event("startup")
    def _arrumar_catalogos():
        limpar_catalogos_migrados()

    @app.get("/monitoramento")
    def pagina_monitoramento():
        return FileResponse("static/monitoramento.html")

    def _dono_ok(linha, usuario):
        return (usuario["papel"] == "admin"
                or str(linha.get("dono_usuario_id")) == str(usuario["id"]))

    def _dispositivo(device_id, usuario):
        linha = db.dispositivo_por_id_com_localidade(device_id)
        if linha is None:
            return None, JSONResponse({"error": "dispositivo não encontrado"},
                                      status_code=404)
        if not _dono_ok(linha, usuario):
            return None, JSONResponse({"error": "sem permissão"}, status_code=403)
        return linha, None

    # ---- subida: equipamento -> servidor (autenticado por token) -----------
    @app.post("/api/edge/dados")
    async def edge_dados(req: Request):
        """Telemetria de sensor ou gateway. Mesmo esquema de autenticacao do
        Raspberry: Authorization: Bearer <token do dispositivo>."""
        cabecalho = req.headers.get("authorization", "")
        token = cabecalho[7:].strip() if cabecalho.lower().startswith("bearer ") else ""
        device = rd.por_token(token) if token else None
        if device is None:
            return JSONResponse({"error": "token de dispositivo ausente ou inválido"},
                                status_code=401)
        try:
            corpo = await req.json()
        except Exception:
            return JSONResponse({"error": "corpo não é JSON"}, status_code=400)

        valores = corpo.get("values", corpo) if isinstance(corpo, dict) else None
        if not isinstance(valores, dict) or not valores:
            return JSONResponse({"error": "esperado um objeto JSON com as telemetrias"},
                                status_code=400)

        e_gateway = device.tipo == "gateway"
        conhecidos = ()
        if e_gateway:
            conhecidos = {l["sub_id"] for l in db.listar_chaves_telemetria(device.id)
                          if l["sub_id"]}
        # A lista explicita dispensa a adivinhacao por completo (ver
        # descobrir_sub_ids). O firmware do gateway sempre a manda.
        explicitos = corpo.get("dispositivos") if isinstance(corpo, dict) else None
        explicitos = ([str(x) for x in explicitos if str(x).strip()]
                      if isinstance(explicitos, list) else None)

        amostras = separar(valores, e_gateway, conhecidos, explicitos)
        amostras = [a for a in amostras if a[1]]      # descarta chave vazia

        # Cada telemetria vai para o cadastro do seu dono, e as derivadas
        # entram na MESMA gravacao -- com o mesmo carimbo de hora da medida
        # que as originou. Gravar em dois momentos deixaria a cota e a
        # distancia com instantes diferentes, e um grafico das duas juntas
        # ficaria em degrau.
        por_dispositivo, estados = distribuir_amostras(device.id, amostras)
        gravadas = 0
        for alvo, lote in por_dispositivo.items():
            if not lote:
                continue
            db.gravar_telemetria(alvo, lote)
            gravadas += len(lote)
        rd.marcar_visto(device)

        # Presenca de cada equipamento remoto. Um sensor atras de um gateway
        # nunca abre conexao com o servidor: quem diz que ele esta vivo -- ou
        # que esta mudo, ou com defeito -- e o gateway, e e este carimbo que
        # tira o sensor do "eternamente offline".
        for alvo, estado in estados.items():
            try:
                db.marcar_estado_sensor(alvo, estado)
            except Exception as e:
                print(f"[telemetria] nao consegui carimbar o sensor {alvo}: {e}")

        # Alarmes por DISPOSITIVO: uma regra criada no cadastro do sensor tem
        # de ser avaliada contra as amostras dele, nao contra as do gateway.
        eventos = []
        for alvo, lote in por_dispositivo.items():
            if not lote:
                continue
            for ev in avaliar_alarmes(alvo, lote):
                ev["device_id"] = alvo
                eventos.append(ev)

        if ctx_manager is not None:
            for alvo in por_dispositivo:
                if por_dispositivo[alvo]:
                    await ctx_manager.broadcast({"type": "telemetria",
                                                 "device_id": alvo,
                                                 "amostras": len(por_dispositivo[alvo])})
            for ev in eventos:
                alvo = ev["device_id"]
                nome = device.nome
                if alvo != device.id:
                    linha = db.dispositivo_por_id(alvo)
                    if linha is not None:
                        nome = linha["nome"]
                await ctx_manager.broadcast({
                    "type": "alarme",
                    "device_id": alvo,
                    "dispositivo_nome": nome,
                    "titulo": ev["titulo"] or ev["chave"],
                    "sub_id": ev["sub_id"],
                    "chave": ev["chave"],
                    "valor": ev["valor"],
                    "estado": ev["estado"],
                    "severidade": ev["severidade"],
                })

        return {"status": "ok", "gravadas": gravadas,
                "alarmes": len(eventos),
                "estado": device.estado.snapshot()}

    # ---- leitura pelo dashboard -------------------------------------------
    @app.get("/api/telemetria/chaves")
    def chaves(device_id: str, usuario=Depends(auth.usuario_atual)):
        """Catalogo do que aquele equipamento ja mandou. E o que alimenta os
        seletores da tela de widgets -- o operador escolhe, nao digita."""
        linha, erro = _dispositivo(device_id, usuario)
        if erro:
            return erro
        itens = [{"sub_id": l["sub_id"], "chave": l["chave"],
                  "numerica": l["numerica"], "unidade": l["unidade"],
                  "ultimo_num": l["ultimo_num"], "ultimo_txt": l["ultimo_txt"],
                  "visto_em": l["visto_em"].isoformat()}
                 for l in db.listar_chaves_telemetria(device_id)]
        return {"tipo": linha.get("tipo") or "camera",
                "e_gateway": (linha.get("tipo") == "gateway"),
                "sub_ids": sorted({i["sub_id"] for i in itens if i["sub_id"]}),
                "itens": itens}

    @app.get("/api/telemetria/serie")
    def serie(device_id: str, chave: str, sub_id: str = "",
              minutos: int = JANELA_PADRAO_MIN, usuario=Depends(auth.usuario_atual)):
        """A serie de uma telemetria. `minutos <= 0` significa TUDO: do
        primeiro registro daquele equipamento ate agora."""
        _, erro = _dispositivo(device_id, usuario)
        if erro:
            return erro

        if minutos > 0:
            linhas = db.serie_telemetria(device_id, sub_id, chave, minutos)
            passo = 0.0
        else:
            linhas, passo = _serie_completa_cache(device_id, sub_id, chave)

        return {"sub_id": sub_id, "chave": chave,
                # passo_s > 0 avisa que os pontos sao MEDIAS por intervalo, e
                # nao leituras. O grafico diz isso na legenda -- um numero que
                # parece leitura e nao e seria pior que nao mostrar.
                "passo_s": round(passo, 1),
                "pontos": [{"em": l["em"].isoformat(), "v": l["valor_num"],
                            "t": l["valor_txt"]} for l in linhas]}

    @app.get("/api/telemetria/ultimos")
    def ultimos(device_id: str, usuario=Depends(auth.usuario_atual)):
        _, erro = _dispositivo(device_id, usuario)
        if erro:
            return erro
        return {"itens": [{"sub_id": l["sub_id"], "chave": l["chave"],
                           "numerica": l["numerica"], "num": l["ultimo_num"],
                           "txt": l["ultimo_txt"],
                           "visto_em": l["visto_em"].isoformat()}
                          for l in db.ultimos_valores(device_id)]}

    # ---- widgets -----------------------------------------------------------
    def _widget_publico(w):
        return {"id": str(w["id"]), "dispositivo_id": str(w["dispositivo_id"]),
                "tipo": w["tipo"], "titulo": w["titulo"], "sub_id": w["sub_id"],
                "chaves": w["chaves"], "config": w["config"], "ordem": w["ordem"]}

    @app.get("/api/widgets")
    def listar_widgets(device_id: str, usuario=Depends(auth.usuario_atual)):
        _, erro = _dispositivo(device_id, usuario)
        if erro:
            return erro
        return {"widgets": [_widget_publico(w) for w in db.listar_widgets(device_id)]}

    @app.post("/api/widgets")
    def criar_widget(p: WidgetPayload, usuario=Depends(auth.usuario_atual)):
        linha, erro = _dispositivo(p.device_id, usuario)
        if erro:
            return erro
        # "equipamentos" e a lista dos controladores REMOTOS de um gateway.
        # Num sensor ela seria sempre vazia, e o widget ficaria ali sem nunca
        # mostrar nada -- melhor recusar na criacao que deixar o operador
        # descobrir depois.
        if p.tipo == "equipamentos" and (linha.get("tipo") or "") != "gateway":
            return JSONResponse(
                {"error": "o widget de equipamentos só existe em gateways"},
                status_code=400)
        if p.tipo not in TIPOS_WIDGET:
            return JSONResponse({"error": f"tipo de widget inválido: {p.tipo}"},
                                status_code=400)
        w = db.criar_widget(p.device_id, p.tipo, p.titulo, p.sub_id,
                            p.chaves, p.config, p.ordem)
        return _widget_publico(w)

    @app.patch("/api/widgets/{widget_id}")
    def editar_widget(widget_id: str, p: WidgetEdicaoPayload,
                      usuario=Depends(auth.usuario_atual)):
        w = db.widget_por_id(widget_id)
        if w is None:
            return JSONResponse({"error": "widget não encontrado"}, status_code=404)
        _, erro = _dispositivo(str(w["dispositivo_id"]), usuario)
        if erro:
            return erro
        campos = {k: v for k, v in p.model_dump().items() if v is not None}
        return _widget_publico(db.atualizar_widget(widget_id, campos))

    @app.delete("/api/widgets/{widget_id}")
    def excluir_widget(widget_id: str, usuario=Depends(auth.usuario_atual)):
        w = db.widget_por_id(widget_id)
        if w is None:
            return {"status": "ok"}
        _, erro = _dispositivo(str(w["dispositivo_id"]), usuario)
        if erro:
            return erro
        db.excluir_widget(widget_id)
        return {"status": "ok"}

    # ---- alarmes -----------------------------------------------------------
    def _alarme_publico(a):
        return {"id": str(a["id"]), "dispositivo_id": str(a["dispositivo_id"]),
                "dispositivo_nome": a.get("dispositivo_nome"),
                "sub_id": a["sub_id"], "chave": a["chave"],
                "condicao": a["condicao"], "limite": a["limite"],
                "titulo": a["titulo"], "severidade": a["severidade"],
                "ativo": a["ativo"], "disparado": a["disparado"]}

    @app.get("/api/alarmes")
    def listar_alarmes(device_id: str | None = None,
                       usuario=Depends(auth.usuario_atual)):
        if device_id:
            _, erro = _dispositivo(device_id, usuario)
            if erro:
                return erro
        linhas = db.listar_alarmes(device_id)
        if usuario["papel"] != "admin":
            meus = {str(d["id"]) for d in db.listar_dispositivos(usuario["id"])}
            linhas = [a for a in linhas if str(a["dispositivo_id"]) in meus]
        return {"alarmes": [_alarme_publico(a) for a in linhas]}

    @app.post("/api/alarmes")
    def criar_alarme(p: AlarmePayload, usuario=Depends(auth.usuario_atual)):
        _, erro = _dispositivo(p.device_id, usuario)
        if erro:
            return erro
        if p.condicao not in ("maior", "menor", "igual"):
            return JSONResponse({"error": "condição deve ser maior, menor ou igual"},
                                status_code=400)
        a = db.criar_alarme(p.device_id, p.sub_id, p.chave, p.condicao,
                            p.limite, p.titulo, p.severidade)
        return _alarme_publico(a)

    @app.patch("/api/alarmes/{alarme_id}")
    def editar_alarme(alarme_id: str, p: AlarmeEdicaoPayload,
                      usuario=Depends(auth.usuario_atual)):
        campos = {k: v for k, v in p.model_dump().items() if v is not None}
        a = db.atualizar_alarme(alarme_id, campos)
        if a is None:
            return JSONResponse({"error": "alarme não encontrado"}, status_code=404)
        _, erro = _dispositivo(str(a["dispositivo_id"]), usuario)
        if erro:
            return erro
        return _alarme_publico(a)

    @app.delete("/api/alarmes/{alarme_id}")
    def excluir_alarme(alarme_id: str, usuario=Depends(auth.usuario_atual)):
        # Confere o dono ANTES de apagar: sem isto qualquer operador logado
        # apagaria a regra de alarme de outro.
        atual = next((a for a in db.listar_alarmes()
                      if str(a["id"]) == str(alarme_id)), None)
        if atual is None:
            return {"status": "ok"}
        _, erro = _dispositivo(str(atual["dispositivo_id"]), usuario)
        if erro:
            return erro
        db.excluir_alarme(alarme_id)
        return {"status": "ok"}

    @app.get("/api/alarmes/eventos")
    def eventos(limite: int = 50, nao_lidos: bool = False,
                usuario=Depends(auth.usuario_atual)):
        """Alimenta o sininho e a tabela de alarmes."""
        linhas = db.listar_eventos_alarme(limite, nao_lidos)
        if usuario["papel"] != "admin":
            meus = {str(d["id"]) for d in db.listar_dispositivos(usuario["id"])}
            linhas = [e for e in linhas if str(e["dispositivo_id"]) in meus]
        return {
            "nao_lidos": db.contar_eventos_nao_lidos(),
            "eventos": [{
                "id": str(e["id"]), "em": e["em"].isoformat(),
                # `origem` diz se o evento veio de uma regra de telemetria
                # ('alarme') ou da visao computacional ('visao'). Sem ele o
                # sininho desenhava a deteccao como se fosse alarme, com
                # "chave — (leu —)" no lugar do que importa.
                "origem": e.get("origem") or "alarme",
                "detalhe": e.get("detalhe") or {},
                "valor": e["valor"], "estado": e["estado"], "lido": e["lido"],
                "titulo": e["titulo"] or e["chave"], "chave": e["chave"],
                "sub_id": e["sub_id"], "condicao": e["condicao"],
                "limite": e["limite"], "severidade": e["severidade"],
                "dispositivo_id": str(e["dispositivo_id"]),
                "dispositivo_nome": e["dispositivo_nome"],
            } for e in linhas],
        }

    @app.post("/api/alarmes/eventos/lidos")
    async def marcar_lidos(req: Request, usuario=Depends(auth.usuario_atual)):
        try:
            corpo = await req.json()
        except Exception:
            corpo = {}
        db.marcar_eventos_lidos(corpo.get("ids"))
        return {"status": "ok", "nao_lidos": db.contar_eventos_nao_lidos()}

    print(">> Modulo de telemetria instalado (sensores, gateways, widgets e alarmes).")

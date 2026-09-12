"""Subtipos de sensor e as contas que transformam UMA medida em telemetrias.

POR QUE ISTO MORA NO SERVIDOR
-----------------------------
Até setembro/2026 quem calculava cota, volume e percentual era o **gateway**
(`firmware/libraries/HydroConecta/RegrasNivel.h`). Funcionava, mas punha a
regra de negócio no lugar mais caro de mudar do sistema: recalibrar o
reservatório exigia subir numa torre, ligar um notebook num ESP32 e regravar
firmware — a centenas de quilômetros de distância.

Agora cada equipamento tem um papel só:

  controlador  ->  mede e entrega a distância BRUTA;
  gateway      ->  cuida do enlace e repassa o que recebeu;
  servidor     ->  aplica a calibração daquele reservatório.

Mudar uma calibração passou a ser editar um cadastro na tela.

COMO UM SUBTIPO É DECLARADO
---------------------------
Um subtipo diz três coisas: quais campos o operador preenche (`campos`), o
que fazer com eles (`derivar`) e como reclamar quando não fecham (`conferir`).
A tela de Dispositivos monta o formulário a partir de `campos` — não há lista
de campos duplicada no HTML, então acrescentar um sensor novo é acrescentar
uma entrada aqui.

O QUE ESTE MÓDULO NÃO FAZ
-------------------------
Não toca no banco e não sabe o que é um dispositivo. Recebe dicionários e
devolve listas — é o que permite testá-lo inteiro sem PostgreSQL
(`server/testes/teste_sensores.py`).
"""

import math

# ============================================================================
# Ajudantes
# ============================================================================
def _num(config, chave):
    """Valor numérico de um campo, ou None se ausente/inválido.

    JSON de formulário chega com string ("8.10") tanto quanto com número;
    aceitar os dois evita uma classe inteira de bug silencioso."""
    v = config.get(chave)
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _arredondar(v, casas):
    if v is None or not math.isfinite(v):
        return None
    return round(v, casas)


# ============================================================================
# Radar de nível da água
# ============================================================================
# Duas âncoras medidas em campo definem a reta. Em Oiticica, os engenheiros
# da barragem entregaram:
#
#   distância 8,10 m  ->  cota 114,68 m  ->  volume 742.632.840,34 m³
#   distância 30,00 m ->  cota  92,00 m  ->  início do volume morto
#
# e daí se interpola tudo o mais. Não é a única leitura possível dos mesmos
# números -- ver conferir_radar_nivel() logo abaixo, que avisa quando as duas
# âncoras discordam sobre onde o sensor está.
CAMPOS_RADAR_NIVEL = [
    {"chave": "dist_ref", "rotulo": "Distância na cota de referência",
     "unidade": "m", "obrigatorio": True, "exemplo": "8.10",
     "ajuda": "Quanto o radar lê quando a água está na cota de referência."},
    {"chave": "cota_ref", "rotulo": "Cota de referência",
     "unidade": "m", "obrigatorio": True, "exemplo": "114.68",
     "ajuda": "Normalmente a cota do 1º vertimento."},
    {"chave": "volume_ref", "rotulo": "Volume na cota de referência",
     "unidade": "m³", "obrigatorio": True, "exemplo": "742632840.34",
     "ajuda": "Do projeto da barragem."},
    {"chave": "dist_vazio", "rotulo": "Distância no início do volume morto",
     "unidade": "m", "obrigatorio": True, "exemplo": "30.00",
     "ajuda": "Normalmente o alcance máximo do radar."},
    {"chave": "cota_vazio", "rotulo": "Cota do início do volume morto",
     "unidade": "m", "obrigatorio": True, "exemplo": "92.00",
     "ajuda": "Abaixo dela não há volume útil (ex.: geratriz da tomada d'água)."},
    {"chave": "cota_vert_1", "rotulo": "Cota do 1º vertimento",
     "unidade": "m", "obrigatorio": False, "exemplo": "114.68",
     "ajuda": "Acima dela a telemetria 'vertendo' fica verdadeira."},
    {"chave": "cota_vert_2", "rotulo": "Cota do 2º vertimento",
     "unidade": "m", "obrigatorio": False, "exemplo": "115.53"},
    {"chave": "cota_maximorum", "rotulo": "Cota maximorum (nível máximo)",
     "unidade": "m", "obrigatorio": False, "exemplo": "118.00",
     "ajuda": "Usada como cota de revanche: acima dela, alerta."},
    {"chave": "cota_fundo", "rotulo": "Cota de fundo",
     "unidade": "m", "obrigatorio": False, "exemplo": "67.00",
     "ajuda": "Informativa: não entra nas contas, mas fica registrada."},
    {"chave": "chave_bruta", "rotulo": "Telemetria com a medida bruta",
     "unidade": "", "obrigatorio": False, "exemplo": "distancia",
     "padrao": "distancia",
     "ajuda": "Nome da telemetria que o controlador envia. Mude só se o "
              "firmware do seu controlador usar outro nome."},
]


def conferir_radar_nivel(config):
    """Lista de avisos sobre esta calibração. Vazia = está coerente.

    Não impede de salvar: quem conhece a barragem é o engenheiro, não este
    código. Mas há um aviso que vale ouro e por isso existe aqui.

    As duas âncoras implicam, cada uma, uma elevação para o próprio radar:

        cota_ref + dist_ref     e     cota_vazio + dist_vazio

    Se um radar aponta para baixo, subir 1 m de água encurta a leitura em
    exatamente 1 m -- as duas contas TÊM de dar o mesmo número. Com os dados
    de Oiticica elas dão 122,78 e 122,00: 78 cm de diferença. Alguma das
    quatro medidas não é o que se supõe (o alcance de 30 m talvez não caia
    exatamente na cota 92, por exemplo). A interpolação continua valendo como
    convenção acordada, mas quem lê a cota precisa saber que ela carrega esse
    desvio."""
    avisos = []
    d_ref, c_ref = _num(config, "dist_ref"), _num(config, "cota_ref")
    d_vaz, c_vaz = _num(config, "dist_vazio"), _num(config, "cota_vazio")
    vol = _num(config, "volume_ref")

    faltando = [c["rotulo"] for c in CAMPOS_RADAR_NIVEL
                if c.get("obrigatorio") and _num(config, c["chave"]) is None]
    if faltando:
        avisos.append("Faltam campos obrigatórios: " + ", ".join(faltando) + ".")
        return avisos

    if not (d_vaz > d_ref):
        avisos.append(
            "A distância do volume morto tem de ser MAIOR que a da cota de "
            "referência: o radar lê mais longe com o reservatório mais vazio. "
            f"Está {d_vaz:g} m contra {d_ref:g} m.")
    if not (c_ref > c_vaz):
        avisos.append(
            "A cota de referência tem de ser MAIOR que a do volume morto. "
            f"Está {c_ref:g} m contra {c_vaz:g} m.")
    if vol is not None and vol <= 0:
        avisos.append("O volume na cota de referência tem de ser positivo.")

    if d_vaz > d_ref and c_ref > c_vaz:
        alt1, alt2 = c_ref + d_ref, c_vaz + d_vaz
        desvio = abs(alt1 - alt2)
        if desvio > 0.05:
            avisos.append(
                f"As duas âncoras discordam sobre a altura do radar: uma diz "
                f"cota {alt1:.2f} m, a outra {alt2:.2f} m ({desvio:.2f} m de "
                f"diferença). Num radar apontado para baixo, subir 1 m de água "
                f"encurta a leitura em 1 m, e as duas contas deveriam bater. "
                f"A interpolação funciona assim mesmo, mas confirme as medidas "
                f"com o engenheiro antes de usar a cota em relatório.")

    cmax = _num(config, "cota_maximorum")
    if cmax is not None and c_ref is not None and cmax < c_ref:
        avisos.append("A cota maximorum está abaixo da cota de referência.")
    return avisos


def derivar_radar_nivel(config, valores):
    """(chave, valor) a publicar, a partir do que o controlador mandou.

    `valores` é o que chegou daquele equipamento nesta mensagem; se a medida
    bruta não veio (pacote de status, sensor com erro), devolve lista vazia --
    é melhor não publicar nada que publicar uma cota inventada."""
    chave = (config.get("chave_bruta") or "distancia").strip() or "distancia"
    if chave not in valores:
        return []
    try:
        d = float(valores[chave])
    except (TypeError, ValueError):
        return []
    if not math.isfinite(d):
        return []

    d_ref, c_ref = _num(config, "dist_ref"), _num(config, "cota_ref")
    d_vaz, c_vaz = _num(config, "dist_vazio"), _num(config, "cota_vazio")
    vol_ref = _num(config, "volume_ref")
    if None in (d_ref, c_ref, d_vaz, c_vaz, vol_ref):
        return []
    if not (d_vaz > d_ref) or not (c_ref > c_vaz) or vol_ref <= 0:
        return []

    # A fração é 1 na âncora cheia e 0 na âncora vazia. Em DOUBLE do começo ao
    # fim: com float de 32 bits, 0,6 exato virava 445.200.028 m³ em vez de
    # 445.200.000 -- erro minúsculo em termos relativos, mas é um número que
    # vai para o relatório de uma barragem.
    fracao = (d_vaz - d) / (d_vaz - d_ref)

    # COTA não é travada. Acima da âncora de referência o reservatório está
    # vertendo, e é justamente aí que o operador mais precisa do número; abaixo
    # da âncora vazia ele está no volume morto, que também é real. A cota é
    # leitura direta do radar, então extrapolar a reta continua honesto.
    cota = c_ref + (d - d_ref) * (c_vaz - c_ref) / (d_vaz - d_ref)

    # VOLUME e PERCENTUAL são travados em [0, 1]. Fora das âncoras não existe
    # dado: o volume de um reservatório não é linear na cota, e extrapolar a
    # curva para cima seria inventar água que ninguém mediu. Acima da âncora,
    # `volume_m3` é um PISO, e `acima_da_referencia` avisa isso.
    fracao_util = min(1.0, max(0.0, fracao))
    volume = fracao_util * vol_ref

    c_vert = _num(config, "cota_vert_1")
    c_max = _num(config, "cota_maximorum")

    saida = [
        ("cota_atual", _arredondar(cota, 3)),
        ("volume_m3", _arredondar(volume, 0)),
        ("volume_rest_m3", _arredondar(vol_ref - volume, 0)),
        ("uso_percentual", _arredondar(fracao_util * 100.0, 2)),
    ]
    if c_max is not None:
        saida.append(("cota_restante", _arredondar(c_max - cota, 3)))
        saida.append(("cota_revanche", _arredondar(c_max, 2)))
        saida.append(("alerta_revanche", cota >= c_max))
    if c_vert is not None:
        saida.append(("vertendo", cota >= c_vert))
    if fracao > 1.0:
        # Só aparece quando é verdade. Uma telemetria que está sempre lá,
        # sempre falsa, vira ruído no seletor de widgets.
        saida.append(("acima_da_referencia", True))
    return saida


# ============================================================================
# Catálogo
# ============================================================================
# Acrescentar um sensor novo é acrescentar uma entrada aqui -- a tela de
# cadastro e a ingestão passam a conhecê-lo sem nenhuma outra alteração.
SUBTIPOS = {
    "radar_nivel": {
        "rotulo": "Radar de nível da água",
        "descricao": "Mede a distância da antena até a lâmina d'água. O "
                     "servidor calcula cota, volume e percentual por "
                     "interpolação entre duas âncoras medidas em campo.",
        "campos": CAMPOS_RADAR_NIVEL,
        "derivar": derivar_radar_nivel,
        "conferir": conferir_radar_nivel,
    },
    # Reservado: o cadastro já aceita, mas ainda não deriva nada. Deixar
    # visível é deliberado -- quem cadastrar um piezômetro hoje já o cadastra
    # no lugar certo, e a conta entra depois sem mexer no cadastro.
    "piezometro": {
        "rotulo": "Piezômetro",
        "descricao": "Pressão da água no maciço. As telemetrias derivadas "
                     "ainda não estão implementadas: por enquanto a leitura "
                     "bruta é gravada como veio.",
        "campos": [],
        "derivar": None,
        "conferir": None,
    },
}


def subtipos_publicos():
    """O catálogo em forma de JSON, para a tela montar o formulário.

    Sem as funções: elas não atravessam JSON e não interessam ao navegador."""
    return [
        {"chave": chave, "rotulo": s["rotulo"], "descricao": s["descricao"],
         "campos": s["campos"], "implementado": s["derivar"] is not None}
        for chave, s in SUBTIPOS.items()
    ]


def conferir(subtipo, config):
    """Avisos sobre uma configuração. Lista vazia = tudo coerente."""
    s = SUBTIPOS.get(subtipo or "")
    if s is None or s["conferir"] is None:
        return []
    return s["conferir"](config or {})


def derivar(subtipo, config, valores):
    """Telemetrias derivadas de uma mensagem. Lista vazia quando não há o que
    derivar -- subtipo desconhecido, calibração incompleta ou medida ausente.

    Nunca levanta exceção: esta função roda no caminho de ingestão, e uma
    configuração ruim de UM equipamento não pode derrubar a telemetria de
    todos os outros que vieram na mesma mensagem."""
    s = SUBTIPOS.get(subtipo or "")
    if s is None or s["derivar"] is None:
        return []
    try:
        return s["derivar"](config or {}, valores or {})
    except Exception as e:               # noqa: BLE001 -- ver docstring
        print(f"[sensores] falha ao derivar '{subtipo}': {e}")
        return []

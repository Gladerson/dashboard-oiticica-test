# ============================================================================
# calibracao.py - Calibracao do gemeo digital (resseccao angular)
#
# O PROBLEMA
# ----------
# A pose da camera dentro do modelo 3D e, hoje, ESTIMADA (ver
# DispositivoRuntime._preparar_geometria):
#
#   * a posicao vem de lat/lon (que tem erro de GPS) mais uma altura de
#     terreno estimada por raio vertical / percentil / ponto de malha mais
#     proximo;
#   * a orientacao (base_forward, "para onde a camera olha quando
#     pan=0/tilt=0") e um CHUTE: a direcao da camera ate o ponto mais
#     proximo da malha. Nao ha nenhuma razao para o norte mecanico da
#     camera coincidir com isso.
#
# Resultado: o cone e as deteccoes caem perto, mas nao em cima. Calibrar e
# medir essa pose em vez de estima-la.
#
# O MODELO DIRETO
# ---------------
# A camera e um cabecote pan-tilt com eixo vertical. Conferido numericamente
# contra glb_geo.direction_from_pan_tilt: o modelo e EXATAMENTE separavel em
# coordenadas esfericas (desvio < 1e-13 grau),
#
#     azimute(direcao)   = az0 + PAN_SIGN  * escala_pan  * pan_reportado
#     elevacao(direcao)  = el0 + TILT_SIGN * escala_tilt * tilt_reportado
#
# onde (az0, el0) e o base_forward. As escalas existem porque o curso
# mecanico do PTZ nao esta calibrado (README, "Pendencias conhecidas"): a
# camera reporta -1..1 e o agente assume +-180/+-90 graus. Se a suposicao
# estiver errada, os angulos vem esticados ou comprimidos.
#
# A MEDIDA
# --------
# O operador aponta a camera real para um ponto de referencia (a mira no
# centro da telinha E o eixo optico) e marca o MESMO ponto no modelo 3D.
# Cada ponto casado da uma equacao vetorial:
#
#     direcao_prevista(pan_i, tilt_i)  ==  normalizar(P_i - posicao)
#
# ou seja 2 equacoes escalares (azimute e elevacao) por ponto.
#
# QUANTOS PONTOS
# --------------
#   2 pontos ( 4 eq) -> so a orientacao (az0, el0): 2 incognitas. Ja corrige
#                       o erro dominante, que e a orientacao chutada.
#   4 pontos ( 8 eq) -> orientacao + posicao: 5 incognitas. MINIMO util.
#   8 pontos (16 eq) -> permite tambem as escalas de pan/tilt: 7 incognitas.
#
# Medido em simulacao com 0,3 grau de erro de marcacao (erro mediano da
# posicao): 4 pontos ~1,1 m; 8 ~0,6 m; 12 ~0,4 m; 16 ~0,4 m. O joelho da
# curva fica por volta de 8 a 12 -- e o que a interface recomenda.
#
# As escalas NAO entram so por haver pontos suficientes: se o curso mecanico
# ja estiver certo, os dois parametros a mais so absorvem ruido e a posicao
# PIORA (0,45 m -> 1,10 m, medido). Por isso o modo automatico ajusta os dois
# modelos e fica com o maior apenas quando ele explica os angulos
# sensivelmente melhor.
#
# A posicao so e observavel por PARALAXE: pontos em direcoes e DISTANCIAS
# diferentes. Pontos alinhados, ou todos na mesma parede a mesma distancia,
# deixam a posicao mal condicionada -- e, pior, o RMS continua BAIXO nesse
# caso (medido: 0,17 grau de RMS com 54 m de erro de posicao). Por isso o
# criterio de confianca nao e o RMS, e sim a incerteza estatistica da
# posicao (_incerteza_posicao).
# ============================================================================
import numpy as np
from scipy.optimize import least_squares

import glb_geo

# Minimos por etapa (ver "QUANTOS PONTOS" acima).
MIN_ORIENTACAO = 2
MIN_POSICAO = 4
# 8 e nao 6: com 6 pontos as duas escalas extras consomem informacao demais
# e a POSICAO piora (medido: erro mediano 0,74 m -> 1,65 m quando o curso
# mecanico ja estava certo). So vale resolver escala com folga de dados.
MIN_ESCALAS = 8

# Resolver as escalas so compensa se elas estiverem MESMO erradas: se o
# curso mecanico ja esta certo, os dois parametros a mais so absorvem ruido.
# Por isso o modo automatico escolhe pelo dado -- fica com o modelo maior
# apenas quando ele explica os angulos sensivelmente melhor (parcimonia).
GANHO_MINIMO_ESCALAS = 1.5      # RMS precisa cair pelo menos 1,5x
RMS_PISO_ESCALAS = 0.3          # ... e o ajuste simples precisa estar ruim

# A posicao resolvida nao pode fugir mais que isto (metros) da posicao
# cadastrada. E uma rede de seguranca: com pontos mal marcados o otimizador
# encontraria "solucoes" absurdas (camera do outro lado da barragem) que
# ajustam bem os angulos e nao querem dizer nada.
RAIO_BUSCA_M = 60.0

# Acima disto o ajuste nao merece confianca -- normalmente e ponto marcado
# errado (o operador clicou noutro lugar do modelo).
RMS_ALERTA_GRAUS = 2.0


def _envolver(a):
    """Diferenca angular levada para (-pi, pi]."""
    return (a + np.pi) % (2 * np.pi) - np.pi


class ModeloAngular:
    """Modelo direto + residuos. Separado do solver para poder ser testado
    isoladamente e reaproveitado no diagnostico."""

    def __init__(self, geo):
        self.geo = geo

    def angulos_observados(self, pos, pontos_xyz):
        """(azimute, elevacao) da direcao camera->ponto, para cada ponto."""
        v = np.asarray(pontos_xyz, dtype=float) - np.asarray(pos, dtype=float)
        n = np.linalg.norm(v, axis=1, keepdims=True)
        n[n < 1e-9] = 1e-9
        v = v / n
        az_el = np.array([self.geo._az_el(u) for u in v], dtype=float)
        return az_el[:, 0], az_el[:, 1]

    def residuos(self, pos, az0, el0, esc_pan, esc_tilt, pans, tilts, pontos_xyz):
        """Erro angular, em GRAUS, de cada ponto -- duas componentes.

        O azimute e multiplicado por cos(elevacao) para virar erro angular
        de verdade sobre a esfera: perto do zenite, um erro grande de
        azimute e um desvio pequeno de apontamento."""
        az_obs, el_obs = self.angulos_observados(pos, pontos_xyz)
        az_prev = az0 + np.radians(glb_geo.PAN_SIGN * esc_pan * np.asarray(pans, float))
        el_prev = el0 + np.radians(glb_geo.TILT_SIGN * esc_tilt * np.asarray(tilts, float))
        r_az = _envolver(az_prev - az_obs) * np.cos(el_obs)
        r_el = _envolver(el_prev - el_obs)
        return np.degrees(np.concatenate([r_az, r_el]))


def _orientacao_fechada(modelo, pos, pans, tilts, pontos_xyz, esc_pan=1.0, esc_tilt=1.0):
    """(az0, el0) otimos para uma posicao dada -- sem iteracao.

    Como o modelo e separavel e aditivo em az/el, fixada a posicao cada
    ponto diz um az0_i = azimute_observado_i - PAN_SIGN*escala*pan_i. O
    melhor az0 e a media desses -- CIRCULAR, senao 179 e -179 dariam media
    zero. Serve tambem de chute inicial do ajuste completo."""
    az_obs, el_obs = modelo.angulos_observados(pos, pontos_xyz)
    az0_i = az_obs - np.radians(glb_geo.PAN_SIGN * esc_pan * np.asarray(pans, float))
    el0_i = el_obs - np.radians(glb_geo.TILT_SIGN * esc_tilt * np.asarray(tilts, float))
    az0 = float(np.arctan2(np.mean(np.sin(az0_i)), np.mean(np.cos(az0_i))))
    return az0, float(np.mean(el0_i))


def diagnostico_geometria(pos, pontos_xyz):
    """Quao bem distribuidos estao os pontos, do ponto de vista da camera.

    Duas coisas importam para a posicao ser observavel:
      * abertura angular -- pontos em direcoes diferentes;
      * variacao de distancia -- paralaxe em profundidade.
    Devolve numeros crus e uma frase; nao reprova nada sozinho."""
    v = np.asarray(pontos_xyz, dtype=float) - np.asarray(pos, dtype=float)
    dist = np.linalg.norm(v, axis=1)
    dist[dist < 1e-9] = 1e-9
    u = v / dist[:, None]

    # maior angulo entre dois pontos quaisquer
    cos = np.clip(u @ u.T, -1.0, 1.0)
    abertura = float(np.degrees(np.arccos(cos.min()))) if len(u) > 1 else 0.0
    razao = float(dist.max() / dist.min())

    avisos = []
    if abertura < 15.0:
        avisos.append("os pontos estão quase na mesma direção — a posição "
                      "fica mal determinada; marque pontos mais afastados "
                      "entre si")
    if razao < 1.3:
        avisos.append("os pontos estão todos à mesma distância — marque "
                      "algum bem mais perto ou bem mais longe")
    return {
        "abertura_graus": round(abertura, 1),
        "dist_min_m": round(float(dist.min()), 2),
        "dist_max_m": round(float(dist.max()), 2),
        "razao_distancias": round(razao, 2),
        "avisos": avisos,
    }


# Acima disto a posicao resolvida nao se sustenta: o ajuste pode ate fechar
# bem, mas os pontos nao restringem a posicao (ver _incerteza_posicao).
INCERTEZA_ALERTA_M = 3.0


def _incerteza_posicao(saida, n_pontos, n_params):
    """Desvio-padrao estimado da posicao, em metros.

    Por que isto e necessario: com pontos mal distribuidos o RMS fica
    BAIXO e a posicao fica MUITO errada -- medido, 0,17 grau de RMS com
    54 m de erro. O RMS mede o quanto o ajuste fecha; ele nao mede o quanto
    os dados restringem cada incognita. Quem responde isso e a covariancia
    sigma^2 * (J^T J)^-1: quando a geometria e degenerada, J^T J fica quase
    singular e a incerteza explode -- exatamente o caso que precisamos
    reprovar."""
    graus_liberdade = 2 * n_pontos - n_params
    if graus_liberdade <= 0:
        return float("inf")          # exatamente determinado: sem redundancia
    J = saida.jac
    residuo2 = float(np.sum(saida.fun ** 2))
    sigma2 = residuo2 / graus_liberdade
    try:
        cov = np.linalg.pinv(J.T @ J) * sigma2
    except np.linalg.LinAlgError:
        return float("inf")
    var = np.diag(cov)[:3]           # os 3 primeiros parametros sao a posicao
    if not np.all(np.isfinite(var)) or np.any(var < 0):
        return float("inf")
    return float(np.sqrt(np.sum(var)))


def _ajustar(geo, pontos, pos_inicial, modo):
    """Um ajuste, com o conjunto de incognitas fixado por 'modo'."""
    return resolver(geo, pontos, pos_inicial, modo=modo)


def resolver(geo, pontos, pos_inicial, base_forward_inicial=None, modo=None):
    """Resolve a pose da camera a partir dos pontos casados.

    pontos: lista de dicts {pan, tilt, ponto: [x, y, z]} em coordenadas
            LOCAIS do modelo (as mesmas do hit_point/raycasting).
    pos_inicial: posicao cadastrada -- chute inicial e centro do raio de
            busca.
    modo: "orientacao" | "pose" | "completo". None escolhe o mais completo
            que o numero de pontos permite.

    Devolve um dict pronto para virar JSON, ou levanta ValueError com uma
    mensagem em portugues quando nao da para resolver."""
    n = len(pontos)
    if n < MIN_ORIENTACAO:
        raise ValueError(f"são necessários pelo menos {MIN_ORIENTACAO} pontos "
                         f"para calibrar (há {n}).")

    if modo is None:
        # Automatico: o modelo mais simples que os dados sustentam. As
        # escalas so entram se realmente explicarem melhor (ver abaixo).
        modo = "pose" if n >= MIN_POSICAO else "orientacao"
        if n >= MIN_ESCALAS:
            simples = _ajustar(geo, pontos, pos_inicial, "pose")
            completo = _ajustar(geo, pontos, pos_inicial, "completo")
            if (simples["rms_graus"] > RMS_PISO_ESCALAS
                    and simples["rms_graus"] > GANHO_MINIMO_ESCALAS * completo["rms_graus"]):
                return completo
            return simples
    if modo == "completo" and n < MIN_ESCALAS:
        raise ValueError(f"resolver as escalas de pan/tilt exige {MIN_ESCALAS} pontos.")
    if modo == "pose" and n < MIN_POSICAO:
        raise ValueError(f"resolver a posição exige {MIN_POSICAO} pontos.")

    pans = np.array([float(p["pan"]) for p in pontos])
    tilts = np.array([float(p["tilt"]) for p in pontos])
    xyz = np.array([[float(c) for c in p["ponto"]] for p in pontos], dtype=float)
    pos0 = np.asarray(pos_inicial, dtype=float)

    modelo = ModeloAngular(geo)

    # Chute inicial: orientacao fechada na posicao cadastrada. Se o cadastro
    # ja trazia um base_forward, ele so serve de desempate -- a forma
    # fechada e melhor palpite que o "ponto mais proximo da malha".
    az0, el0 = _orientacao_fechada(modelo, pos0, pans, tilts, xyz)

    resolve_pos = modo in ("pose", "completo")
    resolve_esc = modo == "completo"

    def desempacotar(v):
        i = 0
        if resolve_pos:
            pos = v[i:i + 3]; i += 3
        else:
            pos = pos0
        a, e = v[i], v[i + 1]; i += 2
        if resolve_esc:
            ep, et = v[i], v[i + 1]
        else:
            ep = et = 1.0
        return pos, a, e, ep, et

    x0 = []
    lo, hi = [], []
    if resolve_pos:
        x0 += list(pos0)
        lo += list(pos0 - RAIO_BUSCA_M); hi += list(pos0 + RAIO_BUSCA_M)
    x0 += [az0, el0]
    lo += [-np.inf, -np.pi / 2]; hi += [np.inf, np.pi / 2]
    if resolve_esc:
        x0 += [1.0, 1.0]
        # +-40% e bastante para um erro de curso mecanico; alem disso quase
        # sempre e ponto marcado errado, nao escala.
        lo += [0.6, 0.6]; hi += [1.4, 1.4]

    def f(v):
        pos, a, e, ep, et = desempacotar(v)
        return modelo.residuos(pos, a, e, ep, et, pans, tilts, xyz)

    saida = least_squares(f, np.array(x0, dtype=float), bounds=(lo, hi),
                          method="trf", xtol=1e-12, ftol=1e-12, max_nfev=4000)

    pos, a, e, ep, et = desempacotar(saida.x)
    incerteza_pos = _incerteza_posicao(saida, n, len(x0)) if resolve_pos else 0.0
    r = modelo.residuos(pos, a, e, ep, et, pans, tilts, xyz)
    # r vem como [az..., el...]: o erro de cada ponto e a soma quadratica
    # das duas componentes.
    r_az, r_el = r[:n], r[n:]
    por_ponto = np.hypot(r_az, r_el)
    rms = float(np.sqrt(np.mean(por_ponto ** 2)))

    base_forward = geo.direcao_de_az_el(a, e)
    deslocamento = float(np.linalg.norm(np.asarray(pos) - pos0))
    geometria = diagnostico_geometria(pos, xyz)

    # "Confiavel" exige as tres coisas: convergiu, o ajuste fecha (RMS) E os
    # pontos realmente restringem a posicao (incerteza). Sem a terceira, uma
    # calibracao com pontos todos na mesma direcao passaria como boa.
    confiavel = bool(saida.success and rms <= RMS_ALERTA_GRAUS
                     and incerteza_pos <= INCERTEZA_ALERTA_M)
    if resolve_pos and incerteza_pos > INCERTEZA_ALERTA_M:
        geometria["avisos"].append(
            f"a posição não ficou bem determinada (incerteza de "
            f"±{incerteza_pos:.1f} m): marque pontos em direções e "
            f"distâncias mais variadas, ou use mais pontos")

    return {
        "modo": modo,
        "n_pontos": n,
        "camera_local_pos": [float(c) for c in pos],
        "base_forward": [float(c) for c in base_forward],
        "az0_graus": round(float(np.degrees(a)), 3),
        "el0_graus": round(float(np.degrees(e)), 3),
        "escala_pan": round(float(ep), 4),
        "escala_tilt": round(float(et), 4),
        "rms_graus": round(rms, 3),
        "erro_max_graus": round(float(por_ponto.max()), 3),
        "residuos_por_ponto": [round(float(x), 3) for x in por_ponto],
        "deslocamento_m": round(deslocamento, 2),
        "incerteza_pos_m": (None if not np.isfinite(incerteza_pos)
                            else round(incerteza_pos, 2)),
        "convergiu": bool(saida.success),
        "geometria": geometria,
        "confiavel": confiavel,
        "rms_alerta_graus": RMS_ALERTA_GRAUS,
        "incerteza_alerta_m": INCERTEZA_ALERTA_M,
    }


# ============================================================================
# Rotas
#
# O fluxo do operador (ver README, "Calibrar o gemeo digital"):
#   1. aponta a camera real para um ponto de referencia, com a mira do
#      centro da telinha em cima dele;
#   2. marca o MESMO ponto no modelo 3D (Ctrl + botao direito);
#   3. repete em direcoes e distancias variadas;
#   4. calcula (previa) e, se gostar do resultado, aplica.
#
# "Calcular" nunca altera o dispositivo: so "aplicar" grava. Assim da para
# experimentar (tirar um ponto ruim, recalcular) sem estragar a pose que ja
# esta valendo.
# ============================================================================
def _ponto_publico(p):
    return {
        "id": str(p["id"]),
        "pan": p["pan"], "tilt": p["tilt"], "zoom": p["zoom"],
        "ponto": [p["ponto_x"], p["ponto_y"], p["ponto_z"]],
        "rotulo": p["rotulo"],
        "criado_em": p["criado_em"].isoformat(),
    }


def estado_publico(linha):
    """Resumo da calibracao gravada num dispositivo (ou None)."""
    if linha is None or linha.get("calib_pos_x") is None:
        return None
    return {
        "modo": linha.get("calib_modo"),
        "n_pontos": linha.get("calib_n_pontos"),
        "rms_graus": linha.get("calib_rms_graus"),
        "escala_pan": linha.get("calib_escala_pan"),
        "escala_tilt": linha.get("calib_escala_tilt"),
        "camera_local_pos": [linha.get("calib_pos_x"), linha.get("calib_pos_y"),
                             linha.get("calib_pos_z")],
        "em": linha["calib_em"].isoformat() if linha.get("calib_em") else None,
    }


def instalar(app):
    from fastapi import Depends
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel

    import auth
    import db
    import registro_dispositivos as registro

    class PontoPayload(BaseModel):
        pan: float
        tilt: float
        zoom: float | None = None
        ponto: list[float]
        rotulo: str | None = None

    class ResolverPayload(BaseModel):
        modo: str | None = None      # None = automatico

    def _dispositivo_ou_erro(dispositivo_id, usuario):
        linha = db.dispositivo_por_id_com_localidade(dispositivo_id)
        if linha is None:
            return None, JSONResponse({"error": "dispositivo não encontrado"},
                                      status_code=404)
        if (usuario["papel"] != "admin"
                and str(linha["dono_usuario_id"]) != str(usuario["id"])):
            return None, JSONResponse({"error": "sem permissão"}, status_code=403)
        return linha, None

    def _geo_e_pos(linha):
        """GeoModel da localidade + posicao de partida. A calibracao precisa
        da malha para nada -- so da projecao -- mas usa o runtime porque e
        ele que ja resolve localidade/offsets."""
        device = registro.por_id(str(linha["id"]))
        if device is None or device.geo is None:
            return None, None, JSONResponse(
                {"error": "este dispositivo ainda não tem modelo 3D pronto; "
                          "calibre depois de associar a localidade e o .glb"},
                status_code=400)
        return device.geo, np.asarray(device.camera_local_pos, dtype=float), None

    @app.get("/api/dispositivos/{dispositivo_id}/calibracao")
    def obter(dispositivo_id: str, usuario=Depends(auth.usuario_atual)):
        linha, erro = _dispositivo_ou_erro(dispositivo_id, usuario)
        if erro:
            return erro
        return {
            "pontos": [_ponto_publico(p) for p in db.listar_pontos_calibracao(dispositivo_id)],
            "calibracao": estado_publico(linha),
            "min_orientacao": MIN_ORIENTACAO,
            "min_posicao": MIN_POSICAO,
            "min_escalas": MIN_ESCALAS,
        }

    @app.post("/api/dispositivos/{dispositivo_id}/calibracao/pontos")
    def adicionar(dispositivo_id: str, p: PontoPayload,
                  usuario=Depends(auth.usuario_atual)):
        linha, erro = _dispositivo_ou_erro(dispositivo_id, usuario)
        if erro:
            return erro
        if len(p.ponto) != 3:
            return JSONResponse({"error": "ponto precisa ter 3 coordenadas"},
                                status_code=400)
        novo = db.criar_ponto_calibracao(dispositivo_id, p.pan, p.tilt, p.zoom,
                                         p.ponto, (p.rotulo or "").strip() or None)
        return _ponto_publico(novo)

    @app.delete("/api/dispositivos/{dispositivo_id}/calibracao/pontos/{ponto_id}")
    def remover(dispositivo_id: str, ponto_id: str,
                usuario=Depends(auth.usuario_atual)):
        _linha, erro = _dispositivo_ou_erro(dispositivo_id, usuario)
        if erro:
            return erro
        if db.excluir_ponto_calibracao(ponto_id, dispositivo_id) is None:
            return JSONResponse({"error": "ponto não encontrado"}, status_code=404)
        return {"status": "ok"}

    @app.post("/api/dispositivos/{dispositivo_id}/calibracao/resolver")
    def calcular(dispositivo_id: str, payload: ResolverPayload,
                 usuario=Depends(auth.usuario_atual)):
        """Previa: calcula e devolve, sem gravar nada."""
        linha, erro = _dispositivo_ou_erro(dispositivo_id, usuario)
        if erro:
            return erro
        geo, pos, erro = _geo_e_pos(linha)
        if erro:
            return erro
        pontos = [_ponto_publico(p) for p in db.listar_pontos_calibracao(dispositivo_id)]
        try:
            return resolver(geo, pontos, pos, modo=payload.modo)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/api/dispositivos/{dispositivo_id}/calibracao/aplicar")
    def aplicar(dispositivo_id: str, payload: ResolverPayload,
                usuario=Depends(auth.usuario_atual)):
        linha, erro = _dispositivo_ou_erro(dispositivo_id, usuario)
        if erro:
            return erro
        geo, pos, erro = _geo_e_pos(linha)
        if erro:
            return erro
        pontos = [_ponto_publico(p) for p in db.listar_pontos_calibracao(dispositivo_id)]
        try:
            resultado = resolver(geo, pontos, pos, modo=payload.modo)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        db.salvar_calibracao(dispositivo_id, resultado)
        # O runtime foi montado com a pose antiga: remonta com a calibrada.
        registro.recarregar(dispositivo_id)
        return {"status": "ok", "resultado": resultado}

    @app.delete("/api/dispositivos/{dispositivo_id}/calibracao")
    def limpar(dispositivo_id: str, apagar_pontos: bool = False,
               usuario=Depends(auth.usuario_atual)):
        """Volta para a pose estimada. Por padrao PRESERVA os pontos: quase
        sempre quem limpa quer recalcular, nao recomecar do zero."""
        _linha, erro = _dispositivo_ou_erro(dispositivo_id, usuario)
        if erro:
            return erro
        db.limpar_calibracao(dispositivo_id)
        if apagar_pontos:
            db.excluir_pontos_calibracao(dispositivo_id)
        registro.recarregar(dispositivo_id)
        return {"status": "ok"}

    print(">> Modulo de calibracao instalado (resseccao angular do gemeo digital).")

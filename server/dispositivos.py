# ============================================================================
# dispositivos.py - Aba "Dispositivos": cadastro de localidades (modelo 3D +
# georreferenciamento) e de dispositivos CV-SHM (camera + Raspberry).
#
# Escopo desta etapa (decidido explicitamente): SO o cadastro. O pipeline ao
# vivo (server/borda.py) continua falando com UM Raspberry por vez, do jeito
# que ja esta em producao -- nao mexe em telemetria/deteccao/stream. Um
# dispositivo cadastrado aqui ainda nao "liga" no pipeline sozinho; isso e a
# proxima etapa (rotear por token). Por isso a listagem mostra so o que foi
# CADASTRADO, sem status online/offline de verdade.
#
# Token e topicos seguem a mesma convencao MQTT que edge/transporte.py ja usa
# para falar com o ThingsBoard: topico_telemetria/atributos sao FIXOS
# ("v1/devices/me/...", o ThingsBoard identifica o dispositivo pelo token da
# conexao MQTT, nao pelo nome no topico); so o de frame e por dispositivo,
# porque esse nao passa pelo esquema de telemetria do ThingsBoard.
# ============================================================================
import re
import secrets
import shutil
import subprocess
import threading
from pathlib import Path

from fastapi import Depends, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import auth
import db
import registro_dispositivos as registro

MODELOS_DIR = Path("static/modelos")
MODELOS_DIR.mkdir(parents=True, exist_ok=True)

MODELO_MAX_BYTES = 300 * 1024 * 1024


def _slug(texto):
    s = re.sub(r"[^a-z0-9]+", "-", texto.strip().lower()).strip("-")
    return s or "dispositivo"


# ============================================================================
class LocalidadePayload(BaseModel):
    nome: str
    utm_zone: int
    utm_hemisferio_sul: bool = True
    geo_offset_x: float
    geo_offset_y: float
    geo_offset_z: float = 0.0
    model_up_axis: str = "Z"


class LocalidadeEdicaoPayload(BaseModel):
    """Edicao parcial (exclude_unset): so o que vier no corpo e alterado."""
    nome: str | None = None
    utm_zone: int | None = None
    utm_hemisferio_sul: bool | None = None
    geo_offset_x: float | None = None
    geo_offset_y: float | None = None
    geo_offset_z: float | None = None
    model_up_axis: str | None = None


class DispositivoPayload(BaseModel):
    nome: str
    proprietario: str | None = None
    localidade_id: str | None = None
    lat: float | None = None
    lon: float | None = None
    alt_acima_solo: float | None = None
    transporte: str = "http"


class DispositivoEdicaoPayload(BaseModel):
    """Edicao parcial: so o que vier no corpo e alterado (exclude_unset).
    Por isso todo campo e opcional e nao tem valor padrao util -- "nao
    mandou" e diferente de "mandou null" (que limpa o campo)."""
    nome: str | None = None
    proprietario: str | None = None
    localidade_id: str | None = None
    lat: float | None = None
    lon: float | None = None
    alt_acima_solo: float | None = None
    transporte: str | None = None
    controller_url: str | None = None
    controller_url_publica: str | None = None


def _localidade_publica(l):
    return {
        "id": str(l["id"]), "nome": l["nome"], "modelo_3d_path": l["modelo_3d_path"],
        "modelo_status": l["modelo_status"], "modelo_erro": l["modelo_erro"],
        "utm_zone": l["utm_zone"], "utm_hemisferio_sul": l["utm_hemisferio_sul"],
        "geo_offset_x": l["geo_offset_x"], "geo_offset_y": l["geo_offset_y"],
        "geo_offset_z": l["geo_offset_z"], "model_up_axis": l["model_up_axis"],
        "criado_em": l["criado_em"].isoformat(),
    }


def motivo_sem_3d(d):
    """Por que este dispositivo ainda nao tem visao 3D -- ou None quando
    esta tudo certo. Uma frase so, ja pronta para a tela: antes o dashboard
    mostrava as tres causas possiveis de uma vez ("localidade nao
    cadastrada, sem lat/lon, ou modelo processando") e o operador tinha que
    adivinhar qual delas era a dele.

    Recebe uma linha vinda do SELECT com a localidade achatada
    (_SELECT_DISPOSITIVO_COM_LOCALIDADE em db.py)."""
    if not d.get("localidade_id"):
        return ("sem localidade: edite o dispositivo e escolha a localidade "
                "onde ele está instalado")
    if d.get("lat") is None or d.get("lon") is None:
        return ("sem latitude/longitude: edite o dispositivo e marque a "
                "posição da câmera (no mapa ou digitando)")
    status = d.get("localidade_modelo_status")
    if status == "pronto":
        return None
    if status == "processando":
        return "o modelo 3D da localidade ainda está sendo processado"
    if status == "erro":
        return ("o modelo 3D da localidade falhou ao processar: veja o erro "
                "na lista de localidades e envie o .glb de novo")
    return "a localidade ainda não tem um modelo 3D enviado"


def _dispositivo_publico(d):
    return {
        "id": str(d["id"]), "entity_id": d["entity_id"], "entity_type": d["entity_type"],
        "nome": d["nome"], "proprietario": d["proprietario"],
        "localidade_id": str(d["localidade_id"]) if d.get("localidade_id") else None,
        "localidade_nome": d.get("localidade_nome"),
        "lat": d["lat"], "lon": d["lon"], "alt_acima_solo": d["alt_acima_solo"],
        "transporte": d["transporte"], "token": d["token"],
        "controller_url": d.get("controller_url"),
        "controller_url_publica": d.get("controller_url_publica"),
        "topico_telemetria": d["topico_telemetria"], "topico_atributos": d["topico_atributos"],
        "topico_frame": d["topico_frame"], "criado_em": d["criado_em"].isoformat(),
        "motivo_sem_3d": motivo_sem_3d(d),
        # A URL que o servidor VAI USAR de fato para o PTZ. Quando o campo
        # acima esta vazio, ela sai do IP de onde o equipamento fala com o
        # servidor -- e sem mostrar isso aqui nao havia como o operador
        # saber para onde os comandos estavam indo.
        **_controlador_efetivo(str(d["id"])),
    }


def _controlador_efetivo(device_id):
    rt = registro.ja_resolvido(device_id)
    if rt is None:
        return {"controller_url_efetiva": None, "controller_origem": "desconhecida",
                "ip_observado": None}
    return {"controller_url_efetiva": rt.controller_url,
            "controller_origem": rt.origem_controller_url,
            "ip_observado": rt.ip_observado}


# ============================================================================
# Descompressao Draco em background (mesma ferramenta do prepare_model.sh,
# so que chamada pela aplicacao em vez de rodada a mao por SSH).
# ============================================================================
def _descomprimir_draco(localidade_id, caminho_final: Path):
    backup = caminho_final.with_name(caminho_final.stem + "_original_draco.glb")
    try:
        if not backup.exists():
            shutil.copy2(caminho_final, backup)
        subprocess.run(
            ["npx", "--yes", "@gltf-transform/cli", "copy", str(backup), str(caminho_final)],
            check=True, capture_output=True, timeout=600, text=True,
        )
        db.atualizar_status_modelo(localidade_id, "pronto")
        # Sem isto, um dispositivo cujo runtime ja foi montado enquanto o
        # modelo ainda estava "processando" fica com geo=None PARA SEMPRE (o
        # registro so consulta o banco na primeira vez que resolve aquele
        # dispositivo) -- o dashboard continuaria dizendo "sem modelo 3D
        # pronto" mesmo com o banco dizendo 'pronto', ate reiniciar o
        # servidor. Aqui o modelo acabou de ficar pronto: descarta os
        # runtimes daquela localidade pra serem remontados no proximo acesso.
        registro.invalidar_localidade(localidade_id)
    except FileNotFoundError:
        db.atualizar_status_modelo(
            localidade_id, "erro",
            "npx nao encontrado no servidor -- instale Node.js/npm "
            "(sudo apt install -y nodejs npm) e envie o modelo de novo.")
    except subprocess.CalledProcessError as e:
        db.atualizar_status_modelo(localidade_id, "erro", (e.stderr or "")[:2000])
    except subprocess.TimeoutExpired:
        db.atualizar_status_modelo(
            localidade_id, "erro", "tempo esgotado ao descomprimir (modelo grande demais?)")
    except Exception as e:
        db.atualizar_status_modelo(localidade_id, "erro", str(e))


# ============================================================================
def instalar(app):
    @app.get("/dispositivos")
    def pagina_dispositivos():
        return FileResponse("static/dispositivos.html")

    # ---- Localidades --------------------------------------------------------
    @app.get("/api/localidades")
    def listar_localidades(_usuario=Depends(auth.usuario_atual)):
        return [_localidade_publica(l) for l in db.listar_localidades()]

    @app.post("/api/localidades")
    def criar_localidade(payload: LocalidadePayload, _usuario=Depends(auth.usuario_atual)):
        nome = payload.nome.strip()
        if not nome:
            return JSONResponse({"error": "nome obrigatorio"}, status_code=400)
        if payload.model_up_axis not in ("Z", "Y"):
            return JSONResponse({"error": "model_up_axis deve ser Z ou Y"}, status_code=400)
        try:
            nova = db.criar_localidade(
                nome, payload.utm_zone, payload.utm_hemisferio_sul,
                payload.geo_offset_x, payload.geo_offset_y,
                payload.geo_offset_z, payload.model_up_axis)
        except Exception as e:
            return JSONResponse(
                {"error": f"não consegui criar (nome já existe?): {e}"}, status_code=400)
        return _localidade_publica(nova)

    @app.patch("/api/localidades/{localidade_id}")
    def editar_localidade(localidade_id: str, payload: LocalidadeEdicaoPayload,
                          _usuario=Depends(auth.usuario_atual)):
        """Corrige o georreferenciamento de uma localidade que JA existe.

        Necessario porque o offset UTM pertence ao MODELO: dois .glb da
        mesma parede (um recorte maior e um menor) saem da fotogrametria
        com offsets diferentes. Trocar o .glb sem trocar o offset desloca a
        camera e as deteccoes em exatamente a diferenca entre os dois --
        e, sem esta rota, so dava para consertar apagando a localidade
        (o que desassocia todos os dispositivos)."""
        loc = db.localidade_por_id(localidade_id)
        if loc is None:
            return JSONResponse({"error": "localidade não encontrada"}, status_code=404)

        campos = payload.model_dump(exclude_unset=True)
        if "nome" in campos:
            campos["nome"] = (campos["nome"] or "").strip()
            if not campos["nome"]:
                return JSONResponse({"error": "nome obrigatorio"}, status_code=400)
        if "model_up_axis" in campos and campos["model_up_axis"] not in ("Z", "Y"):
            return JSONResponse({"error": "model_up_axis deve ser Z ou Y"}, status_code=400)

        try:
            nova = db.atualizar_localidade(localidade_id, campos)
        except Exception as e:
            return JSONResponse(
                {"error": f"não consegui salvar (nome já existe?): {e}"}, status_code=400)

        # A geometria em memoria foi montada com os valores ANTIGOS: descarta
        # o GeoModel e os runtimes que dependiam dele.
        registro.invalidar_localidade(localidade_id)
        return _localidade_publica(nova)

    @app.delete("/api/localidades/{localidade_id}")
    def excluir_localidade(localidade_id: str, _usuario=Depends(auth.usuario_atual)):
        db.excluir_localidade(localidade_id)
        # Os dispositivos que apontavam pra ela ficam sem localidade: os
        # runtimes em memoria precisam parar de usar o GeoModel antigo.
        registro.invalidar_localidade(localidade_id)
        return {"status": "ok"}

    @app.post("/api/localidades/{localidade_id}/modelo")
    async def enviar_modelo(localidade_id: str, arquivo: UploadFile = File(...),
                            _usuario=Depends(auth.usuario_atual)):
        loc = db.localidade_por_id(localidade_id)
        if loc is None:
            return JSONResponse({"error": "localidade não encontrada"}, status_code=404)
        if not (arquivo.filename or "").lower().endswith(".glb"):
            return JSONResponse({"error": "envie um arquivo .glb"}, status_code=400)

        destino = MODELOS_DIR / f"{localidade_id}.glb"
        total = 0
        try:
            with open(destino, "wb") as f:
                while True:
                    pedaco = await arquivo.read(1024 * 1024)
                    if not pedaco:
                        break
                    total += len(pedaco)
                    if total > MODELO_MAX_BYTES:
                        raise ValueError("modelo maior que 300 MB")
                    f.write(pedaco)
        except ValueError as e:
            destino.unlink(missing_ok=True)
            return JSONResponse({"error": str(e)}, status_code=400)

        db.definir_modelo_localidade(localidade_id, str(destino))
        # O arquivo em disco ACABOU de ser sobrescrito: qualquer GeoModel ja
        # carregado dessa localidade agora descreve um modelo que nao existe
        # mais. Descarta antes de comecar a descomprimir.
        registro.invalidar_localidade(localidade_id)
        threading.Thread(target=_descomprimir_draco, args=(localidade_id, destino),
                         daemon=True).start()
        return {"status": "ok", "modelo_status": "processando"}

    @app.get("/api/localidades/{localidade_id}")
    def obter_localidade(localidade_id: str, _usuario=Depends(auth.usuario_atual)):
        loc = db.localidade_por_id(localidade_id)
        if loc is None:
            return JSONResponse({"error": "não encontrada"}, status_code=404)
        return _localidade_publica(loc)

    # ---- Dispositivos ---------------------------------------------------------
    @app.get("/api/dispositivos")
    def listar_dispositivos(usuario=Depends(auth.usuario_atual)):
        dono = None if usuario["papel"] == "admin" else usuario["id"]
        return [_dispositivo_publico(d) for d in db.listar_dispositivos(dono)]

    @app.post("/api/dispositivos")
    def criar_dispositivo(payload: DispositivoPayload, usuario=Depends(auth.usuario_atual)):
        nome = payload.nome.strip()
        if not nome:
            return JSONResponse({"error": "nome obrigatorio"}, status_code=400)
        if payload.transporte not in ("http", "mqtt"):
            return JSONResponse({"error": "transporte inválido"}, status_code=400)
        if payload.localidade_id and db.localidade_por_id(payload.localidade_id) is None:
            return JSONResponse({"error": "localidade não encontrada"}, status_code=400)

        slug = _slug(nome)
        # sufixo aleatorio curto: entity_id/topico_frame precisam ser
        # unicos, e dois dispositivos podem legitimamente ter nomes
        # parecidos ("Camera 1", "Camera 1 (backup)" -> mesmo slug).
        slug_unico = f"{slug}-{secrets.token_hex(3)}"
        try:
            novo = db.criar_dispositivo(
                entity_id=f"urn:ngsi-ld:CV-SHM:{slug_unico}",
                entity_type="CV-SHM",
                nome=nome,
                proprietario=(payload.proprietario or "").strip() or None,
                localidade_id=payload.localidade_id,
                lat=payload.lat, lon=payload.lon, alt_acima_solo=payload.alt_acima_solo,
                transporte=payload.transporte,
                token=secrets.token_urlsafe(24),
                topico_telemetria="v1/devices/me/telemetry",
                topico_atributos="v1/devices/me/attributes",
                topico_frame=f"oiticica/{slug_unico}/frame",
                dono_usuario_id=usuario["id"],
            )
        except Exception as e:
            return JSONResponse({"error": f"não consegui criar: {e}"}, status_code=400)
        # Rele com a localidade junto: e o que motivo_sem_3d precisa para
        # dizer, ja na resposta da criacao, se a visao 3D vai funcionar.
        return _dispositivo_publico(db.dispositivo_por_id_com_localidade(novo["id"]))

    @app.patch("/api/dispositivos/{dispositivo_id}")
    def editar_dispositivo(dispositivo_id: str, payload: DispositivoEdicaoPayload,
                           usuario=Depends(auth.usuario_atual)):
        """Edita o cadastro de um dispositivo que JA existe. Sem isto, um
        dispositivo criado antes da localidade existir (ou criado com
        "(nenhuma)") ficava sem jeito de ganhar modelo 3D a nao ser
        excluindo e recriando -- o que trocaria o token e derrubaria o
        Raspberry em campo."""
        alvo = db.dispositivo_por_id(dispositivo_id)
        if alvo is None:
            return JSONResponse({"error": "não encontrado"}, status_code=404)
        if usuario["papel"] != "admin" and str(alvo["dono_usuario_id"]) != str(usuario["id"]):
            return JSONResponse({"error": "sem permissão"}, status_code=403)

        campos = payload.model_dump(exclude_unset=True)

        if "nome" in campos:
            campos["nome"] = (campos["nome"] or "").strip()
            if not campos["nome"]:
                return JSONResponse({"error": "nome obrigatorio"}, status_code=400)
        if "proprietario" in campos:
            campos["proprietario"] = (campos["proprietario"] or "").strip() or None
        if "transporte" in campos and campos["transporte"] not in ("http", "mqtt"):
            return JSONResponse({"error": "transporte inválido"}, status_code=400)
        if campos.get("localidade_id") and db.localidade_por_id(campos["localidade_id"]) is None:
            return JSONResponse({"error": "localidade não encontrada"}, status_code=400)
        for chave in ("controller_url", "controller_url_publica"):
            if chave in campos:
                campos[chave] = (campos[chave] or "").strip() or None

        try:
            atualizado = db.atualizar_dispositivo(dispositivo_id, campos)
        except Exception as e:
            return JSONResponse({"error": f"não consegui salvar: {e}"}, status_code=400)

        # O runtime em memoria foi montado com os valores ANTIGOS (inclusive
        # a pose da camera e o GeoModel da localidade antiga): descarta pra
        # ser remontado com o cadastro novo no proximo acesso.
        registro.recarregar(dispositivo_id)
        return _dispositivo_publico(atualizado)

    @app.delete("/api/dispositivos/{dispositivo_id}")
    def excluir_dispositivo(dispositivo_id: str, usuario=Depends(auth.usuario_atual)):
        alvo = db.dispositivo_por_id(dispositivo_id)
        if alvo is None:
            return JSONResponse({"error": "não encontrado"}, status_code=404)
        if usuario["papel"] != "admin" and str(alvo["dono_usuario_id"]) != str(usuario["id"]):
            return JSONResponse({"error": "sem permissão"}, status_code=403)
        db.excluir_dispositivo(dispositivo_id)
        # Sem isto o token do dispositivo excluido continuaria autenticando
        # em /api/edge/* ate o servidor reiniciar (o mapa token -> runtime e
        # um cache em memoria).
        registro.esquecer(dispositivo_id)
        return {"status": "ok"}

    print(">> Modulo de dispositivos instalado (cadastro de localidades/dispositivos CV-SHM).")

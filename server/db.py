# ============================================================================
# db.py - PostgreSQL: usuarios/sessoes (Configuracao, §16 do README) e o
# esquema PREPARADO (mas ainda nao usado por nenhuma rota) para as proximas
# etapas -- localidades/modelos 3D e dispositivos (aba "Dispositivos") e
# dashboards configuraveis (aba "Dashboard"). Criar as tres etapas de uma vez
# custa uma migracao a mais agora e evita reformular o esquema no meio do
# caminho.
#
# Por que Postgres e nao os arquivos JSON que o resto do projeto usa
# (server/history/index.json): pedido explicito de interoperabilidade com o
# que ja roda na SEMARH (SIGHMA) e com o ThingsBoard -- ambos em cima de
# Postgres.
#
# "dispositivos" usa um formato inspirado em NGSI/FIWARE (entity_id/
# entity_type + atributos) e nos topicos MQTT que o `transporte.py` ja fala
# com o ThingsBoard: nao e um context broker de verdade, so um desenho de
# tabela que nao vai brigar com um se um dia entrar no meio.
# ============================================================================
import hashlib
import hmac
import base64
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://oiticica:oiticica@127.0.0.1:5432/oiticica"
)

ADMIN_USER_PADRAO = "admin"
ADMIN_SENHA_PADRAO = "hydroconecta"

# Duracao da sessao de login (cookie). Nao tem "estado desejado" aqui porque
# isto e sessao de OPERADOR humano no navegador, nao de dispositivo -- nada
# a ver com o padrao usado para o Pi em server/borda.py.
SESSAO_DURACAO_H = float(os.getenv("SESSAO_DURACAO_H", str(24 * 7)))

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=False,
                      kwargs={"row_factory": dict_row})

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS usuarios (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username        TEXT UNIQUE NOT NULL,
    senha_hash      TEXT NOT NULL,
    papel           TEXT NOT NULL DEFAULT 'usuario'
                        CHECK (papel IN ('admin', 'usuario')),
    tema            TEXT NOT NULL DEFAULT 'escuro'
                        CHECK (tema IN ('escuro', 'claro')),
    trocar_senha    BOOLEAN NOT NULL DEFAULT false,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessoes (
    token           TEXT PRIMARY KEY,
    usuario_id      UUID NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expira_em       TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessoes_usuario ON sessoes(usuario_id);

-- A partir daqui, esquema preparado para as proximas etapas (aba
-- "Dispositivos" e aba "Dashboard") -- ver cabecalho do arquivo.
CREATE TABLE IF NOT EXISTS localidades (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nome                TEXT UNIQUE NOT NULL,
    modelo_3d_path      TEXT,
    utm_zone            INTEGER,
    utm_hemisferio_sul  BOOLEAN NOT NULL DEFAULT true,
    geo_offset_x        DOUBLE PRECISION,
    geo_offset_y        DOUBLE PRECISION,
    geo_offset_z        DOUBLE PRECISION NOT NULL DEFAULT 0,
    model_up_axis       TEXT NOT NULL DEFAULT 'Z' CHECK (model_up_axis IN ('Z', 'Y')),
    criado_em           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dispositivos (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id           TEXT UNIQUE NOT NULL,
    entity_type         TEXT NOT NULL DEFAULT 'CV-SHM',
    nome                TEXT NOT NULL,
    proprietario        TEXT,
    localidade_id       UUID REFERENCES localidades(id) ON DELETE SET NULL,
    lat                 DOUBLE PRECISION,
    lon                 DOUBLE PRECISION,
    alt_acima_solo      DOUBLE PRECISION,
    transporte          TEXT NOT NULL DEFAULT 'http' CHECK (transporte IN ('http', 'mqtt')),
    token               TEXT UNIQUE,
    topico_telemetria   TEXT,
    topico_atributos    TEXT,
    topico_frame        TEXT,
    dono_usuario_id     UUID REFERENCES usuarios(id) ON DELETE SET NULL,
    criado_em           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_dispositivos_localidade ON dispositivos(localidade_id);
CREATE INDEX IF NOT EXISTS ix_dispositivos_dono ON dispositivos(dono_usuario_id);

CREATE TABLE IF NOT EXISTS dashboards (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nome            TEXT NOT NULL,
    usuario_id      UUID NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    layout          JSONB NOT NULL DEFAULT '[]'::jsonb,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_dashboards_usuario ON dashboards(usuario_id);

-- Sem framework de migration neste projeto (mesmo espirito do resto): quem
-- ja rodou a etapa "Configuracao" tem "localidades" sem estas colunas.
-- ADD COLUMN IF NOT EXISTS e idempotente, entao rodar de novo nao quebra
-- quem ja tinha o schema completo.
ALTER TABLE localidades ADD COLUMN IF NOT EXISTS modelo_status TEXT NOT NULL DEFAULT 'nenhum'
    CHECK (modelo_status IN ('nenhum', 'processando', 'pronto', 'erro'));
ALTER TABLE localidades ADD COLUMN IF NOT EXISTS modelo_erro TEXT;

-- Endereco da API de PTZ (porta 8090) DESSE dispositivo. NULL = usa
-- CONTROLLER_URL/CONTROLLER_PUBLIC_URL do server/.env (compatibilidade com
-- quem tem so um dispositivo e nunca preencheu isto).
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS controller_url TEXT;
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS controller_url_publica TEXT;

-- Calibracao do gemeo digital (server/calibracao.py). Enquanto forem NULL,
-- a pose continua sendo a ESTIMADA a partir de lat/lon + altura de terreno.
-- Preenchidas, substituem a estimativa por completo.
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS calib_pos_x DOUBLE PRECISION;
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS calib_pos_y DOUBLE PRECISION;
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS calib_pos_z DOUBLE PRECISION;
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS calib_fwd_x DOUBLE PRECISION;
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS calib_fwd_y DOUBLE PRECISION;
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS calib_fwd_z DOUBLE PRECISION;
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS calib_escala_pan DOUBLE PRECISION;
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS calib_escala_tilt DOUBLE PRECISION;
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS calib_rms_graus DOUBLE PRECISION;
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS calib_n_pontos INTEGER;
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS calib_modo TEXT;
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS calib_em TIMESTAMPTZ;

-- Pontos casados (mira da camera real <-> ponto no modelo 3D). Ficam
-- guardados para o operador poder revisar, remover um ponto ruim e
-- recalcular sem refazer tudo.
CREATE TABLE IF NOT EXISTS calibracao_pontos (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispositivo_id  UUID NOT NULL REFERENCES dispositivos(id) ON DELETE CASCADE,
    pan             DOUBLE PRECISION NOT NULL,
    tilt            DOUBLE PRECISION NOT NULL,
    zoom            DOUBLE PRECISION,
    ponto_x         DOUBLE PRECISION NOT NULL,
    ponto_y         DOUBLE PRECISION NOT NULL,
    ponto_z         DOUBLE PRECISION NOT NULL,
    rotulo          TEXT,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_calib_pontos_dispositivo
    ON calibracao_pontos(dispositivo_id);
"""


# ============================================================================
# Senha: PBKDF2-SHA256 (stdlib, sem depender de bcrypt/argon2). Iteracoes no
# nivel recomendado pela OWASP (2023) para PBKDF2-SHA256.
# ============================================================================
_PBKDF2_ITERACOES = 600_000


def hash_senha(senha: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), salt, _PBKDF2_ITERACOES)
    return (f"pbkdf2_sha256${_PBKDF2_ITERACOES}$"
            f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}")


def verificar_senha(senha: str, hash_armazenado: str) -> bool:
    try:
        algo, iteracoes, salt_b64, hash_b64 = hash_armazenado.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        esperado = base64.b64decode(hash_b64)
    except Exception:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), salt, int(iteracoes))
    return hmac.compare_digest(dk, esperado)


# ============================================================================
# Bootstrap: schema + admin padrao
# ============================================================================
def iniciar():
    """Abre o pool e garante o schema. Falha rapido (como o resto do projeto
    faz para .env/WSDL/porta) em vez de deixar o servidor subir sem banco."""
    try:
        pool.open(wait=True, timeout=5)
        with pool.connection() as conn:
            conn.execute(SCHEMA)
    except Exception as e:
        sys.exit(
            f"ERRO: nao consegui conectar/preparar o PostgreSQL em "
            f"'{DATABASE_URL}'.\n  {e}\n\n"
            f"Confira server/.env (DATABASE_URL) e se o Postgres esta no ar:\n"
            f"  sudo systemctl status postgresql\n"
            f"Instrucoes de instalacao: README.md, secao 5.1a."
        )
    _seed_admin()


def _seed_admin():
    with pool.connection() as conn:
        existe = conn.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone()
        if existe:
            return
        conn.execute(
            "INSERT INTO usuarios (username, senha_hash, papel, trocar_senha) "
            "VALUES (%s, %s, 'admin', true)",
            (ADMIN_USER_PADRAO, hash_senha(ADMIN_SENHA_PADRAO)),
        )
    print(f">> Usuario admin padrao criado: '{ADMIN_USER_PADRAO}' / "
          f"'{ADMIN_SENHA_PADRAO}' -- troque a senha no primeiro login "
          f"(Configuração).")


# ============================================================================
# Usuarios
# ============================================================================
def usuario_por_username(username):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM usuarios WHERE username = %s", (username,)
        ).fetchone()


def usuario_por_id(usuario_id):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM usuarios WHERE id = %s", (usuario_id,)
        ).fetchone()


def listar_usuarios():
    with pool.connection() as conn:
        return conn.execute(
            "SELECT id, username, papel, tema, trocar_senha, criado_em "
            "FROM usuarios ORDER BY criado_em"
        ).fetchall()


def criar_usuario(username, senha, papel="usuario"):
    with pool.connection() as conn:
        return conn.execute(
            "INSERT INTO usuarios (username, senha_hash, papel, trocar_senha) "
            "VALUES (%s, %s, %s, true) RETURNING id, username, papel",
            (username, hash_senha(senha), papel),
        ).fetchone()


def excluir_usuario(usuario_id):
    with pool.connection() as conn:
        conn.execute("DELETE FROM usuarios WHERE id = %s", (usuario_id,))


def redefinir_senha(usuario_id, nova_senha, forcar_troca=True):
    with pool.connection() as conn:
        conn.execute(
            "UPDATE usuarios SET senha_hash = %s, trocar_senha = %s WHERE id = %s",
            (hash_senha(nova_senha), forcar_troca, usuario_id),
        )
        # Trocou a senha: derruba as sessoes existentes desse usuario.
        conn.execute("DELETE FROM sessoes WHERE usuario_id = %s", (usuario_id,))


def definir_tema(usuario_id, tema):
    with pool.connection() as conn:
        conn.execute("UPDATE usuarios SET tema = %s WHERE id = %s", (tema, usuario_id))


# ============================================================================
# Sessoes
# ============================================================================
def criar_sessao(usuario_id) -> str:
    token = secrets.token_urlsafe(32)
    expira_em = datetime.now(timezone.utc) + timedelta(hours=SESSAO_DURACAO_H)
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO sessoes (token, usuario_id, expira_em) VALUES (%s, %s, %s)",
            (token, usuario_id, expira_em),
        )
    return token


def usuario_da_sessao(token):
    """Usuario dono do token, ou None se o token nao existir/tiver expirado."""
    if not token:
        return None
    with pool.connection() as conn:
        return conn.execute(
            "SELECT u.* FROM sessoes s JOIN usuarios u ON u.id = s.usuario_id "
            "WHERE s.token = %s AND s.expira_em > now()",
            (token,),
        ).fetchone()


def destruir_sessao(token):
    with pool.connection() as conn:
        conn.execute("DELETE FROM sessoes WHERE token = %s", (token,))


# ============================================================================
# Localidades (modelo 3D + georreferenciamento)
# ============================================================================
def listar_localidades():
    with pool.connection() as conn:
        return conn.execute("SELECT * FROM localidades ORDER BY nome").fetchall()


def localidade_por_id(localidade_id):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM localidades WHERE id = %s", (localidade_id,)
        ).fetchone()


def criar_localidade(nome, utm_zone, utm_hemisferio_sul, geo_offset_x, geo_offset_y,
                     geo_offset_z=0.0, model_up_axis="Z"):
    with pool.connection() as conn:
        return conn.execute(
            "INSERT INTO localidades (nome, utm_zone, utm_hemisferio_sul, "
            "geo_offset_x, geo_offset_y, geo_offset_z, model_up_axis) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (nome, utm_zone, utm_hemisferio_sul, geo_offset_x, geo_offset_y,
             geo_offset_z, model_up_axis),
        ).fetchone()


# O georreferenciamento pertence ao MODELO, nao ao "lugar": cada .glb sai da
# fotogrametria com o seu proprio offset UTM (odm_georeferencing_model_geo.txt).
# Trocar o .glb de uma localidade sem trocar o offset desloca tudo -- por isso
# estes campos precisam ser editaveis depois de criados.
CAMPOS_EDITAVEIS_LOCALIDADE = (
    "nome", "utm_zone", "utm_hemisferio_sul",
    "geo_offset_x", "geo_offset_y", "geo_offset_z", "model_up_axis",
)


def atualizar_localidade(localidade_id, campos):
    """Atualiza so as chaves presentes em 'campos'. Retorna a linha nova."""
    campos = {k: v for k, v in campos.items() if k in CAMPOS_EDITAVEIS_LOCALIDADE}
    if not campos:
        return localidade_por_id(localidade_id)
    atribuicoes = ", ".join(f"{k} = %s" for k in campos)
    valores = list(campos.values()) + [localidade_id]
    with pool.connection() as conn:
        conn.execute(f"UPDATE localidades SET {atribuicoes} WHERE id = %s", valores)
    return localidade_por_id(localidade_id)


def excluir_localidade(localidade_id):
    with pool.connection() as conn:
        conn.execute("DELETE FROM localidades WHERE id = %s", (localidade_id,))


def definir_modelo_localidade(localidade_id, caminho):
    """Upload aceito: grava o caminho e marca 'processando' (a decompressao
    Draco roda em background, ver dispositivos.py)."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE localidades SET modelo_3d_path = %s, modelo_status = 'processando', "
            "modelo_erro = NULL WHERE id = %s",
            (caminho, localidade_id),
        )


def atualizar_status_modelo(localidade_id, status, erro=None):
    with pool.connection() as conn:
        conn.execute(
            "UPDATE localidades SET modelo_status = %s, modelo_erro = %s WHERE id = %s",
            (status, erro, localidade_id),
        )


# ============================================================================
# Dispositivos
# ============================================================================
def listar_dispositivos(dono_usuario_id=None):
    """Sem dono_usuario_id, lista todos (uso do admin). Com ele, so os do
    dono -- 'cada usuario so tem acesso aos seus dispositivos'.

    Usa o mesmo SELECT com a localidade achatada das outras consultas: a
    listagem precisa do modelo_status para conseguir dizer POR QUE um
    dispositivo ainda nao tem visao 3D (ver _dispositivo_publico)."""
    with pool.connection() as conn:
        if dono_usuario_id is None:
            return conn.execute(
                _SELECT_DISPOSITIVO_COM_LOCALIDADE + " ORDER BY d.criado_em"
            ).fetchall()
        return conn.execute(
            _SELECT_DISPOSITIVO_COM_LOCALIDADE
            + " WHERE d.dono_usuario_id = %s ORDER BY d.criado_em",
            (dono_usuario_id,),
        ).fetchall()


def dispositivo_por_id(dispositivo_id):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM dispositivos WHERE id = %s", (dispositivo_id,)
        ).fetchone()


# Localidade "achatada" na mesma linha, com prefixo localidade_* -- e o que
# server/registro_dispositivos.py precisa pra montar o GeoModel (ou saber
# que ainda nao da: sem localidade, ou localidade sem modelo_status='pronto')
# sem uma segunda consulta.
_SELECT_DISPOSITIVO_COM_LOCALIDADE = """
    SELECT d.*,
           l.nome AS localidade_nome,
           l.modelo_3d_path AS localidade_modelo_3d_path,
           l.modelo_status AS localidade_modelo_status,
           l.utm_zone AS localidade_utm_zone,
           l.utm_hemisferio_sul AS localidade_utm_hemisferio_sul,
           l.geo_offset_x AS localidade_geo_offset_x,
           l.geo_offset_y AS localidade_geo_offset_y,
           l.geo_offset_z AS localidade_geo_offset_z,
           l.model_up_axis AS localidade_model_up_axis
    FROM dispositivos d
    LEFT JOIN localidades l ON l.id = d.localidade_id
"""


def dispositivo_por_token(token):
    """Dispositivo (com a localidade ja junto) autenticado por token. E o
    caminho de auth de /api/edge/* e /api/telemetry|detection -- ver
    server/registro_dispositivos.py."""
    if not token:
        return None
    with pool.connection() as conn:
        return conn.execute(
            _SELECT_DISPOSITIVO_COM_LOCALIDADE + " WHERE d.token = %s", (token,)
        ).fetchone()


def dispositivo_por_id_com_localidade(dispositivo_id):
    with pool.connection() as conn:
        return conn.execute(
            _SELECT_DISPOSITIVO_COM_LOCALIDADE + " WHERE d.id = %s", (dispositivo_id,)
        ).fetchone()


def criar_dispositivo(entity_id, entity_type, nome, proprietario, localidade_id,
                      lat, lon, alt_acima_solo, transporte, token,
                      topico_telemetria, topico_atributos, topico_frame,
                      dono_usuario_id, controller_url=None, controller_url_publica=None):
    with pool.connection() as conn:
        return conn.execute(
            "INSERT INTO dispositivos (entity_id, entity_type, nome, proprietario, "
            "localidade_id, lat, lon, alt_acima_solo, transporte, token, "
            "topico_telemetria, topico_atributos, topico_frame, dono_usuario_id, "
            "controller_url, controller_url_publica) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (entity_id, entity_type, nome, proprietario, localidade_id, lat, lon,
             alt_acima_solo, transporte, token, topico_telemetria, topico_atributos,
             topico_frame, dono_usuario_id, controller_url, controller_url_publica),
        ).fetchone()


# Campos que a edicao de dispositivo pode mexer. De proposito NAO inclui
# token/entity_id/topicos: trocar isso quebraria o Raspberry que ja esta em
# campo com aquele token no .env, e nao e o que "editar o cadastro"
# significa para quem opera.
CAMPOS_EDITAVEIS_DISPOSITIVO = (
    "nome", "proprietario", "localidade_id", "lat", "lon", "alt_acima_solo",
    "transporte", "controller_url", "controller_url_publica",
)


def atualizar_dispositivo(dispositivo_id, campos):
    """Atualiza so as chaves presentes em 'campos' (as demais ficam como
    estao). Retorna a linha ja com localidade_nome, do mesmo jeito que
    listar_dispositivos, para a resposta da API nao precisar de outra
    consulta."""
    campos = {k: v for k, v in campos.items() if k in CAMPOS_EDITAVEIS_DISPOSITIVO}
    if not campos:
        return dispositivo_por_id_com_localidade(dispositivo_id)
    atribuicoes = ", ".join(f"{k} = %s" for k in campos)
    valores = list(campos.values()) + [dispositivo_id]
    with pool.connection() as conn:
        conn.execute(
            f"UPDATE dispositivos SET {atribuicoes} WHERE id = %s", valores
        )
    return dispositivo_por_id_com_localidade(dispositivo_id)


# ============================================================================
# Calibracao (server/calibracao.py)
# ============================================================================
def listar_pontos_calibracao(dispositivo_id):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM calibracao_pontos WHERE dispositivo_id = %s "
            "ORDER BY criado_em", (dispositivo_id,)
        ).fetchall()


def criar_ponto_calibracao(dispositivo_id, pan, tilt, zoom, ponto, rotulo=None):
    with pool.connection() as conn:
        return conn.execute(
            "INSERT INTO calibracao_pontos (dispositivo_id, pan, tilt, zoom, "
            "ponto_x, ponto_y, ponto_z, rotulo) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (dispositivo_id, pan, tilt, zoom,
             ponto[0], ponto[1], ponto[2], rotulo),
        ).fetchone()


def excluir_ponto_calibracao(ponto_id, dispositivo_id):
    """Restringe pelo dispositivo tambem: impede apagar ponto de outro
    dispositivo mandando so um id adivinhado."""
    with pool.connection() as conn:
        return conn.execute(
            "DELETE FROM calibracao_pontos WHERE id = %s AND dispositivo_id = %s "
            "RETURNING id", (ponto_id, dispositivo_id)
        ).fetchone()


def excluir_pontos_calibracao(dispositivo_id):
    with pool.connection() as conn:
        conn.execute("DELETE FROM calibracao_pontos WHERE dispositivo_id = %s",
                     (dispositivo_id,))


def salvar_calibracao(dispositivo_id, resultado):
    """Grava a pose calibrada no dispositivo. resultado e o dict devolvido
    por calibracao.resolver()."""
    pos, fwd = resultado["camera_local_pos"], resultado["base_forward"]
    with pool.connection() as conn:
        conn.execute(
            "UPDATE dispositivos SET calib_pos_x=%s, calib_pos_y=%s, calib_pos_z=%s, "
            "calib_fwd_x=%s, calib_fwd_y=%s, calib_fwd_z=%s, "
            "calib_escala_pan=%s, calib_escala_tilt=%s, calib_rms_graus=%s, "
            "calib_n_pontos=%s, calib_modo=%s, calib_em=now() WHERE id=%s",
            (pos[0], pos[1], pos[2], fwd[0], fwd[1], fwd[2],
             resultado["escala_pan"], resultado["escala_tilt"],
             resultado["rms_graus"], resultado["n_pontos"], resultado["modo"],
             dispositivo_id),
        )
    return dispositivo_por_id_com_localidade(dispositivo_id)


def limpar_calibracao(dispositivo_id):
    """Volta para a pose estimada (nao apaga os pontos)."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE dispositivos SET calib_pos_x=NULL, calib_pos_y=NULL, "
            "calib_pos_z=NULL, calib_fwd_x=NULL, calib_fwd_y=NULL, "
            "calib_fwd_z=NULL, calib_escala_pan=NULL, calib_escala_tilt=NULL, "
            "calib_rms_graus=NULL, calib_n_pontos=NULL, calib_modo=NULL, "
            "calib_em=NULL WHERE id=%s", (dispositivo_id,))


def excluir_dispositivo(dispositivo_id):
    with pool.connection() as conn:
        conn.execute("DELETE FROM dispositivos WHERE id = %s", (dispositivo_id,))

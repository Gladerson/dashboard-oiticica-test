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

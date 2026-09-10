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
from psycopg.types.json import Jsonb

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://oiticica:oiticica@127.0.0.1:5432/oiticica"
)

ADMIN_USER_PADRAO = "admin"
ADMIN_SENHA_PADRAO = "hydroconecta"

# Duracao da sessao de login (cookie). Nao tem "estado desejado" aqui porque
# isto e sessao de OPERADOR humano no navegador, nao de dispositivo -- nada
# a ver com o padrao usado para o Pi em server/borda.py.
SESSAO_DURACAO_H = float(os.getenv("SESSAO_DURACAO_H", str(24 * 7)))

# Inatividade: quanto tempo SEM ACAO DO OPERADOR a sessao sobrevive. E outra
# coisa, e mais importante, que SESSAO_DURACAO_H -- aquela e o prazo maximo
# absoluto; esta e o que fecha a sessao do posto de operacao que ficou
# aberto e sozinho. Vale o que vencer primeiro.
#
# Cuidado de projeto: quem renova o prazo e a ATIVIDADE DA PESSOA, nao a
# requisicao. O dashboard sozinho consulta o servidor a cada 3s; se qualquer
# requisicao renovasse, a tela esquecida se manteria logada para sempre --
# exatamente o que isto existe para evitar. Por isso a renovacao vem de uma
# rota propria, chamada pelo navegador so quando ha mouse/teclado/toque
# (ver POST /api/sessao/atividade em auth.py e layout.js).
SESSAO_INATIVIDADE_MIN = float(os.getenv("SESSAO_INATIVIDADE_MIN", "30"))

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
ALTER TABLE sessoes ADD COLUMN IF NOT EXISTS visto_em TIMESTAMPTZ NOT NULL DEFAULT now();

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

-- ---------------------------------------------------------------------------
-- Sensores e gateways (ESP32) -- ver README secao "Telemetria de sensores"
--
-- 'camera'  = o Raspberry com a PTZ (tudo que existia ate aqui);
-- 'sensor'  = ESP32 que fala por si mesmo;
-- 'gateway' = ESP32 que recebe de N controladores por LoRa e reenvia tudo
--             junto. O payload dele traz varios equipamentos numa mensagem
--             so, e o servidor separa por sub_id.
-- ---------------------------------------------------------------------------
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS tipo TEXT NOT NULL DEFAULT 'camera';
ALTER TABLE dispositivos DROP CONSTRAINT IF EXISTS dispositivos_tipo_check;
ALTER TABLE dispositivos ADD CONSTRAINT dispositivos_tipo_check
    CHECK (tipo IN ('camera', 'sensor', 'gateway'));

-- Ultima vez que ESTE dispositivo falou com o servidor -- qualquer que seja o
-- caminho (telemetria da camera, deteccao, POST do gateway). Antes a resposta
-- para "esta online?" vivia so na memoria do processo (DispositivoRuntime),
-- e por isso: sumia a cada reinicio do servidor, e nao existia para quem
-- apenas LISTA os dispositivos. Agora ha um fato no banco, e "offline" e
-- uma conta simples sobre ele (ver DISPOSITIVO_OFFLINE_S em dispositivos.py).
-- NULL = nunca falou desde que a coluna existe.
ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS visto_em TIMESTAMPTZ;

-- Serie historica. sub_id = '' quer dizer "o proprio dispositivo" (sensor
-- direto); num gateway e o identificador do equipamento remoto
-- ("nivel-rd01"). Guardamos numero E texto porque o gateway manda os dois
-- ("distancia": 12.5 e "nivel-rd01": "on").
CREATE TABLE IF NOT EXISTS telemetria (
    id              BIGSERIAL PRIMARY KEY,
    dispositivo_id  UUID NOT NULL REFERENCES dispositivos(id) ON DELETE CASCADE,
    sub_id          TEXT NOT NULL DEFAULT '',
    chave           TEXT NOT NULL,
    valor_num       DOUBLE PRECISION,
    valor_txt       TEXT,
    em              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_telemetria_serie
    ON telemetria(dispositivo_id, sub_id, chave, em DESC);

-- Catalogo do que ja chegou de cada dispositivo. E o que alimenta os
-- seletores da tela de widgets: em vez de o operador digitar o nome da
-- telemetria, ele escolhe entre as que o equipamento realmente mandou.
CREATE TABLE IF NOT EXISTS telemetria_chaves (
    dispositivo_id  UUID NOT NULL REFERENCES dispositivos(id) ON DELETE CASCADE,
    sub_id          TEXT NOT NULL DEFAULT '',
    chave           TEXT NOT NULL,
    numerica        BOOLEAN NOT NULL DEFAULT true,
    unidade         TEXT,
    ultimo_num      DOUBLE PRECISION,
    ultimo_txt      TEXT,
    visto_em        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (dispositivo_id, sub_id, chave)
);

CREATE TABLE IF NOT EXISTS widgets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispositivo_id  UUID NOT NULL REFERENCES dispositivos(id) ON DELETE CASCADE,
    tipo            TEXT NOT NULL
                    CHECK (tipo IN ('area','barras','card','radial','status','alarmes')),
    titulo          TEXT NOT NULL DEFAULT '',
    sub_id          TEXT NOT NULL DEFAULT '',
    chaves          JSONB NOT NULL DEFAULT '[]'::jsonb,
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    ordem           INTEGER NOT NULL DEFAULT 0,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_widgets_disp ON widgets(dispositivo_id, ordem);

-- 'disparado' guarda o estado ATUAL da regra. Sem ele, um sensor que fica
-- 10 minutos acima do limite geraria um evento a cada leitura e afogaria o
-- sininho: o evento so nasce na BORDA (normal -> disparado e volta).
CREATE TABLE IF NOT EXISTS alarmes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispositivo_id  UUID NOT NULL REFERENCES dispositivos(id) ON DELETE CASCADE,
    sub_id          TEXT NOT NULL DEFAULT '',
    chave           TEXT NOT NULL,
    condicao        TEXT NOT NULL CHECK (condicao IN ('maior','menor','igual')),
    limite          DOUBLE PRECISION NOT NULL,
    titulo          TEXT NOT NULL DEFAULT '',
    severidade      TEXT NOT NULL DEFAULT 'alerta'
                    CHECK (severidade IN ('info','alerta','critico')),
    ativo           BOOLEAN NOT NULL DEFAULT true,
    disparado       BOOLEAN NOT NULL DEFAULT false,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_alarmes_disp ON alarmes(dispositivo_id);

CREATE TABLE IF NOT EXISTS alarmes_eventos (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alarme_id       UUID NOT NULL REFERENCES alarmes(id) ON DELETE CASCADE,
    valor           DOUBLE PRECISION,
    estado          TEXT NOT NULL DEFAULT 'disparado'
                    CHECK (estado IN ('disparado','normalizado')),
    lido            BOOLEAN NOT NULL DEFAULT false,
    em              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_alarmes_eventos_em ON alarmes_eventos(em DESC);

-- O feed do sininho deixou de ser so de alarmes de telemetria: as deteccoes
-- da visao computacional entram aqui tambem. Em vez de uma segunda tabela --
-- que obrigaria a unir duas fontes na consulta, no contador de nao lidos e no
-- "marcar como lido" --, a mesma tabela passa a aceitar um evento SEM regra
-- de alarme: `alarme_id` vira opcional e `origem` diz de onde veio.
ALTER TABLE alarmes_eventos ALTER COLUMN alarme_id DROP NOT NULL;
ALTER TABLE alarmes_eventos ADD COLUMN IF NOT EXISTS origem TEXT NOT NULL DEFAULT 'alarme';
ALTER TABLE alarmes_eventos ADD COLUMN IF NOT EXISTS dispositivo_id UUID
    REFERENCES dispositivos(id) ON DELETE CASCADE;
ALTER TABLE alarmes_eventos ADD COLUMN IF NOT EXISTS titulo TEXT;
ALTER TABLE alarmes_eventos ADD COLUMN IF NOT EXISTS severidade TEXT;
ALTER TABLE alarmes_eventos ADD COLUMN IF NOT EXISTS detalhe JSONB;
CREATE INDEX IF NOT EXISTS ix_alarmes_eventos_origem ON alarmes_eventos(origem, em DESC);
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
    """Usuario dono do token, ou None se a sessao nao existir, tiver estourado
    o prazo absoluto OU passado do limite de inatividade."""
    if not token:
        return None
    with pool.connection() as conn:
        return conn.execute(
            "SELECT u.* FROM sessoes s JOIN usuarios u ON u.id = s.usuario_id "
            "WHERE s.token = %s AND s.expira_em > now() "
            "  AND s.visto_em > now() - make_interval(secs => %s)",
            (token, SESSAO_INATIVIDADE_MIN * 60),
        ).fetchone()


def renovar_sessao(token):
    """Marca atividade do OPERADOR agora. Devolve quantos segundos ainda
    restam de inatividade, ou None se a sessao ja nao vale mais (ai o
    navegador manda o usuario para o login)."""
    if not token:
        return None
    with pool.connection() as conn:
        linha = conn.execute(
            "UPDATE sessoes SET visto_em = now() "
            "WHERE token = %s AND expira_em > now() "
            "  AND visto_em > now() - make_interval(secs => %s) "
            "RETURNING EXTRACT(EPOCH FROM (expira_em - now())) AS ate_o_fim",
            (token, SESSAO_INATIVIDADE_MIN * 60),
        ).fetchone()
    if linha is None:
        return None
    # Vale o que vencer primeiro: o prazo absoluto ou a inatividade.
    return min(SESSAO_INATIVIDADE_MIN * 60, float(linha["ate_o_fim"]))


def segundos_restantes(token):
    """Quanto falta para a sessao cair, sem renovar nada. E o que o navegador
    usa para acertar o relogio dele depois de uma recarga de pagina."""
    if not token:
        return None
    with pool.connection() as conn:
        linha = conn.execute(
            "SELECT EXTRACT(EPOCH FROM (expira_em - now())) AS ate_o_fim, "
            "       EXTRACT(EPOCH FROM (now() - visto_em))  AS parado_ha "
            "FROM sessoes WHERE token = %s",
            (token,),
        ).fetchone()
    if linha is None:
        return None
    por_inatividade = SESSAO_INATIVIDADE_MIN * 60 - float(linha["parado_ha"])
    restante = min(por_inatividade, float(linha["ate_o_fim"]))
    return int(restante) if restante > 0 else 0


def limpar_sessoes_mortas():
    """Sessoes vencidas nao servem para nada e so fazem a tabela crescer.
    Chamado no login -- e barato e nao precisa de agendador."""
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM sessoes WHERE expira_em <= now() "
            "   OR visto_em <= now() - make_interval(secs => %s)",
            (SESSAO_INATIVIDADE_MIN * 60,),
        )


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


def marcar_dispositivo_visto(dispositivo_id):
    """Carimba visto_em = now(). Chamada em TODO caminho por onde um
    equipamento fala com o servidor.

    Quem chama deve estrangular a frequencia (ver _visto_recente em
    server/registro_dispositivos.py): a camera reporta a cada 3 s, e um
    UPDATE por reporte seria uma escrita por segundo por camera para
    sustentar uma informacao que so precisa de precisao de minutos."""
    with pool.connection() as conn:
        conn.execute("UPDATE dispositivos SET visto_em = now() WHERE id = %s",
                     (dispositivo_id,))


def criar_dispositivo(entity_id, entity_type, nome, proprietario, localidade_id,
                      lat, lon, alt_acima_solo, transporte, token,
                      topico_telemetria, topico_atributos, topico_frame,
                      dono_usuario_id, controller_url=None, controller_url_publica=None,
                      tipo="camera"):
    with pool.connection() as conn:
        return conn.execute(
            "INSERT INTO dispositivos (entity_id, entity_type, nome, proprietario, "
            "localidade_id, lat, lon, alt_acima_solo, transporte, token, "
            "topico_telemetria, topico_atributos, topico_frame, dono_usuario_id, "
            "controller_url, controller_url_publica, tipo) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (entity_id, entity_type, nome, proprietario, localidade_id, lat, lon,
             alt_acima_solo, transporte, token, topico_telemetria, topico_atributos,
             topico_frame, dono_usuario_id, controller_url, controller_url_publica,
             tipo),
        ).fetchone()


# Campos que a edicao de dispositivo pode mexer. De proposito NAO inclui
# token/entity_id/topicos: trocar isso quebraria o Raspberry que ja esta em
# campo com aquele token no .env, e nao e o que "editar o cadastro"
# significa para quem opera.
CAMPOS_EDITAVEIS_DISPOSITIVO = (
    "nome", "proprietario", "localidade_id", "lat", "lon", "alt_acima_solo",
    "transporte", "controller_url", "controller_url_publica", "tipo",
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


def excluir_dispositivo(dispositivo_id):
    with pool.connection() as conn:
        conn.execute("DELETE FROM dispositivos WHERE id = %s", (dispositivo_id,))


# ============================================================================
# Telemetria de sensores/gateways, widgets e alarmes
#
# O gateway (edge ESP32) manda UMA mensagem com varios equipamentos dentro.
# Aqui embaixo tudo ja chega separado em (sub_id, chave, valor) -- quem faz
# essa separacao e server/telemetria.py.
# ============================================================================
def gravar_telemetria(dispositivo_id, amostras):
    """amostras: lista de (sub_id, chave, valor_num, valor_txt).

    Grava a serie e atualiza o catalogo numa transacao so. O catalogo
    (telemetria_chaves) e o que a tela de widgets oferece nos seletores:
    sem ele o operador teria de digitar o nome da telemetria de cabeca."""
    if not amostras:
        return
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO telemetria (dispositivo_id, sub_id, chave, valor_num, valor_txt) "
                "VALUES (%s,%s,%s,%s,%s)",
                [(dispositivo_id, s, c, n, t) for (s, c, n, t) in amostras],
            )
            cur.executemany(
                "INSERT INTO telemetria_chaves "
                "  (dispositivo_id, sub_id, chave, numerica, ultimo_num, ultimo_txt, visto_em) "
                "VALUES (%s,%s,%s,%s,%s,%s, now()) "
                "ON CONFLICT (dispositivo_id, sub_id, chave) DO UPDATE SET "
                "  numerica = EXCLUDED.numerica, ultimo_num = EXCLUDED.ultimo_num, "
                "  ultimo_txt = EXCLUDED.ultimo_txt, visto_em = now()",
                [(dispositivo_id, s, c, n is not None, n, t) for (s, c, n, t) in amostras],
            )


def listar_chaves_telemetria(dispositivo_id):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM telemetria_chaves WHERE dispositivo_id = %s "
            "ORDER BY sub_id, chave", (dispositivo_id,)
        ).fetchall()


def serie_telemetria(dispositivo_id, sub_id, chave, desde_minutos=1440, limite=2000):
    """Serie de um ponto, mais antigo primeiro (e a ordem que o grafico quer).
    O ORDER BY do SELECT interno e DESC para usar o indice e pegar os N mais
    RECENTES; a inversao acontece depois."""
    with pool.connection() as conn:
        linhas = conn.execute(
            "SELECT em, valor_num, valor_txt FROM telemetria "
            "WHERE dispositivo_id = %s AND sub_id = %s AND chave = %s "
            "  AND em > now() - (%s || ' minutes')::interval "
            "ORDER BY em DESC LIMIT %s",
            (dispositivo_id, sub_id, chave, str(int(desde_minutos)), int(limite)),
        ).fetchall()
    return list(reversed(linhas))


def ultimos_valores(dispositivo_id):
    """Ultimo valor de cada (sub_id, chave) -- o que os widgets de card,
    radial e status precisam, sem varrer a serie."""
    with pool.connection() as conn:
        return conn.execute(
            "SELECT sub_id, chave, numerica, ultimo_num, ultimo_txt, visto_em "
            "FROM telemetria_chaves WHERE dispositivo_id = %s ORDER BY sub_id, chave",
            (dispositivo_id,)
        ).fetchall()


def limpar_telemetria_antiga(dias):
    """Retencao. Sem isso a tabela cresce para sempre -- ver README."""
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM telemetria WHERE em < now() - (%s || ' days')::interval",
            (str(int(dias)),))


# ---- widgets ---------------------------------------------------------------
CAMPOS_EDITAVEIS_WIDGET = ("tipo", "titulo", "sub_id", "chaves", "config", "ordem")


def listar_widgets(dispositivo_id):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM widgets WHERE dispositivo_id = %s ORDER BY ordem, criado_em",
            (dispositivo_id,)
        ).fetchall()


def criar_widget(dispositivo_id, tipo, titulo, sub_id, chaves, config, ordem):
    with pool.connection() as conn:
        return conn.execute(
            "INSERT INTO widgets (dispositivo_id, tipo, titulo, sub_id, chaves, config, ordem) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (dispositivo_id, tipo, titulo, sub_id, Jsonb(chaves), Jsonb(config), ordem),
        ).fetchone()


def atualizar_widget(widget_id, campos):
    campos = {k: v for k, v in campos.items() if k in CAMPOS_EDITAVEIS_WIDGET}
    if not campos:
        return widget_por_id(widget_id)
    for k in ("chaves", "config"):
        if k in campos:
            campos[k] = Jsonb(campos[k])
    atribuicoes = ", ".join(f"{k} = %s" for k in campos)
    with pool.connection() as conn:
        return conn.execute(
            f"UPDATE widgets SET {atribuicoes} WHERE id = %s RETURNING *",
            list(campos.values()) + [widget_id],
        ).fetchone()


def widget_por_id(widget_id):
    with pool.connection() as conn:
        return conn.execute("SELECT * FROM widgets WHERE id = %s", (widget_id,)).fetchone()


def excluir_widget(widget_id):
    with pool.connection() as conn:
        conn.execute("DELETE FROM widgets WHERE id = %s", (widget_id,))


# ---- alarmes ---------------------------------------------------------------
CAMPOS_EDITAVEIS_ALARME = ("sub_id", "chave", "condicao", "limite", "titulo",
                           "severidade", "ativo")


def listar_alarmes(dispositivo_id=None):
    with pool.connection() as conn:
        if dispositivo_id is None:
            return conn.execute(
                "SELECT a.*, d.nome AS dispositivo_nome FROM alarmes a "
                "JOIN dispositivos d ON d.id = a.dispositivo_id ORDER BY a.criado_em"
            ).fetchall()
        return conn.execute(
            "SELECT a.*, d.nome AS dispositivo_nome FROM alarmes a "
            "JOIN dispositivos d ON d.id = a.dispositivo_id "
            "WHERE a.dispositivo_id = %s ORDER BY a.criado_em", (dispositivo_id,)
        ).fetchall()


def alarmes_ativos_de(dispositivo_id):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT * FROM alarmes WHERE dispositivo_id = %s AND ativo",
            (dispositivo_id,)
        ).fetchall()


def criar_alarme(dispositivo_id, sub_id, chave, condicao, limite, titulo, severidade):
    with pool.connection() as conn:
        return conn.execute(
            "INSERT INTO alarmes (dispositivo_id, sub_id, chave, condicao, limite, "
            "titulo, severidade) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (dispositivo_id, sub_id, chave, condicao, limite, titulo, severidade),
        ).fetchone()


def atualizar_alarme(alarme_id, campos):
    campos = {k: v for k, v in campos.items() if k in CAMPOS_EDITAVEIS_ALARME}
    if not campos:
        return None
    atribuicoes = ", ".join(f"{k} = %s" for k in campos)
    with pool.connection() as conn:
        return conn.execute(
            f"UPDATE alarmes SET {atribuicoes} WHERE id = %s RETURNING *",
            list(campos.values()) + [alarme_id],
        ).fetchone()


def excluir_alarme(alarme_id):
    with pool.connection() as conn:
        conn.execute("DELETE FROM alarmes WHERE id = %s", (alarme_id,))


def registrar_evento_alarme(alarme_id, valor, estado):
    """So chamado na BORDA da condicao (ver coluna 'disparado')."""
    with pool.connection() as conn:
        conn.execute("UPDATE alarmes SET disparado = %s WHERE id = %s",
                     (estado == "disparado", alarme_id))
        return conn.execute(
            "INSERT INTO alarmes_eventos (alarme_id, valor, estado) "
            "VALUES (%s,%s,%s) RETURNING *", (alarme_id, valor, estado)
        ).fetchone()


def registrar_evento_visao(dispositivo_id, titulo, detalhe=None, severidade="alerta"):
    """Uma deteccao da visao computacional no mesmo feed dos alarmes.

    Sem `alarme_id`: nao ha regra de limiar por tras, e o evento existe por si.
    `detalhe` carrega o id da deteccao no historico, para o sininho poder
    levar o operador direto a evidencia."""
    with pool.connection() as conn:
        return conn.execute(
            "INSERT INTO alarmes_eventos "
            "  (origem, dispositivo_id, titulo, severidade, estado, detalhe) "
            "VALUES ('visao', %s, %s, %s, 'disparado', %s) RETURNING *",
            (dispositivo_id, titulo, severidade, Jsonb(detalhe or {})),
        ).fetchone()


def listar_eventos_alarme(limite=50, apenas_nao_lidos=False):
    """Alarmes de telemetria E deteccoes de visao, na mesma ordem cronologica.

    LEFT JOIN porque o evento de visao nao tem regra: os campos de alarme
    (chave, condicao, limite) vem nulos, e quem desenha usa `origem` para
    saber o que mostrar. O dispositivo vem da regra quando ha uma, e da
    propria linha quando nao ha."""
    filtro = "WHERE NOT e.lido" if apenas_nao_lidos else ""
    with pool.connection() as conn:
        return conn.execute(
            "SELECT e.*, "
            "       COALESCE(a.titulo, e.titulo) AS titulo, "
            "       a.chave, a.sub_id, a.condicao, a.limite, "
            "       COALESCE(a.severidade, e.severidade, 'alerta') AS severidade, "
            "       d.nome AS dispositivo_nome, d.id AS dispositivo_id "
            "FROM alarmes_eventos e "
            "LEFT JOIN alarmes a ON a.id = e.alarme_id "
            "LEFT JOIN dispositivos d ON d.id = COALESCE(a.dispositivo_id, e.dispositivo_id) "
            f"{filtro} ORDER BY e.em DESC LIMIT %s", (int(limite),)
        ).fetchall()


def contar_eventos_nao_lidos():
    with pool.connection() as conn:
        return conn.execute(
            "SELECT count(*) AS n FROM alarmes_eventos WHERE NOT lido"
        ).fetchone()["n"]


def marcar_eventos_lidos(ids=None):
    with pool.connection() as conn:
        if ids:
            conn.execute("UPDATE alarmes_eventos SET lido = true WHERE id = ANY(%s)",
                         (list(ids),))
        else:
            conn.execute("UPDATE alarmes_eventos SET lido = true WHERE NOT lido")

# ============================================================================
# auth.py - Login por sessao (cookie) + administracao de usuarios.
#
# So gira em cima de PostgreSQL (db.py); nao inventa nada de token/JWT.
#
# O que fica ABERTO (sem sessao), de proposito:
#   - /login, /api/login                         (a propria tela de entrada)
#   - /api/edge/*                                 (Pi -- nao tem navegador/cookie)
#   - /api/telemetry, /api/detection               (controller.py, mesma logica)
# Tudo o mais (o dashboard, as acoes de operador, /model, /history_files, o
# WebSocket) exige sessao valida -- ver a middleware instalada por
# `instalar(app)`.
# ============================================================================
import os
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import db

COOKIE_NOME = "oiticica_sessao"
COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() in ("1", "true", "sim")

# Caminhos que NUNCA exigem sessao (dispositivo, ou a propria tela de login).
CAMINHOS_LIVRES = {
    "/login", "/api/login",
    "/api/edge/telemetria", "/api/edge/deteccao",
    "/api/edge/frame", "/api/edge/imagem",
    "/api/telemetry", "/api/detection",
}

# Com sessao, mas mesmo que "trocar_senha" esteja pendente (senao o usuario
# fica preso sem conseguir trocar a propria senha).
CAMINHOS_PERMITIDOS_COM_TROCA_PENDENTE = {
    "/config", "/api/logout", "/api/usuarios/me", "/api/usuarios/me/senha",
}


def _publico(path: str) -> bool:
    return path in CAMINHOS_LIVRES


def _usuario_publico(u):
    if u is None:
        return None
    return {
        "id": str(u["id"]), "username": u["username"], "papel": u["papel"],
        "tema": u["tema"], "trocar_senha": u["trocar_senha"],
    }


def usuario_atual(request: Request):
    u = getattr(request.state, "usuario", None)
    if u is None:
        raise HTTPException(status_code=401, detail="nao autenticado")
    return u


def exigir_admin(usuario=Depends(usuario_atual)):
    if usuario["papel"] != "admin":
        raise HTTPException(status_code=403, detail="requer administrador")
    return usuario


# ============================================================================
class LoginPayload(BaseModel):
    username: str
    senha: str


class TrocarSenhaPayload(BaseModel):
    senha_atual: str
    senha_nova: str


class TemaPayload(BaseModel):
    tema: str


class CriarUsuarioPayload(BaseModel):
    username: str
    senha: str
    papel: str = "usuario"


class RedefinirSenhaPayload(BaseModel):
    senha_nova: str


# ============================================================================
def instalar(app):
    @app.middleware("http")
    async def _exigir_login(request: Request, call_next):
        path = request.url.path
        if not _publico(path):
            token = request.cookies.get(COOKIE_NOME)
            usuario = db.usuario_da_sessao(token)
            if usuario is None:
                if path.startswith("/api/"):
                    return JSONResponse({"error": "nao autenticado"}, status_code=401)
                return RedirectResponse(f"/login?next={quote(path)}")
            request.state.usuario = usuario
            if usuario["trocar_senha"] and path not in CAMINHOS_PERMITIDOS_COM_TROCA_PENDENTE:
                if path.startswith("/api/"):
                    return JSONResponse({"error": "troque a senha antes de continuar",
                                        "trocar_senha": True}, status_code=403)
                return RedirectResponse("/config?trocar_senha=1")
        return await call_next(request)

    @app.get("/login")
    def pagina_login():
        return FileResponse("static/login.html")

    @app.get("/config")
    def pagina_config():
        return FileResponse("static/config.html")

    @app.post("/api/login")
    def login(payload: LoginPayload):
        u = db.usuario_por_username(payload.username.strip())
        if u is None or not db.verificar_senha(payload.senha, u["senha_hash"]):
            return JSONResponse({"error": "usuario ou senha invalidos"}, status_code=401)
        token = db.criar_sessao(u["id"])
        resp = JSONResponse({"status": "ok", "usuario": _usuario_publico(u)})
        resp.set_cookie(COOKIE_NOME, token, httponly=True, samesite="lax",
                        secure=COOKIE_SECURE, max_age=int(db.SESSAO_DURACAO_H * 3600))
        return resp

    @app.post("/api/logout")
    def logout(request: Request):
        token = request.cookies.get(COOKIE_NOME)
        if token:
            db.destruir_sessao(token)
        resp = JSONResponse({"status": "ok"})
        resp.delete_cookie(COOKIE_NOME)
        return resp

    @app.get("/api/usuarios/me")
    def eu(usuario=Depends(usuario_atual)):
        return _usuario_publico(usuario)

    @app.post("/api/usuarios/me/senha")
    def trocar_minha_senha(payload: TrocarSenhaPayload, usuario=Depends(usuario_atual)):
        if not db.verificar_senha(payload.senha_atual, usuario["senha_hash"]):
            return JSONResponse({"error": "senha atual incorreta"}, status_code=400)
        if len(payload.senha_nova) < 8:
            return JSONResponse({"error": "a nova senha precisa de pelo menos 8 caracteres"},
                                status_code=400)
        db.redefinir_senha(usuario["id"], payload.senha_nova, forcar_troca=False)
        # A troca invalida a sessao atual tambem (redefinir_senha derruba
        # todas) -- o navegador precisa logar de novo com a senha nova.
        return {"status": "ok", "relogar": True}

    @app.post("/api/usuarios/me/tema")
    def definir_meu_tema(payload: TemaPayload, usuario=Depends(usuario_atual)):
        if payload.tema not in ("escuro", "claro"):
            return JSONResponse({"error": "tema invalido"}, status_code=400)
        db.definir_tema(usuario["id"], payload.tema)
        return {"status": "ok"}

    # ---- administracao de usuarios (admin) ---------------------------------
    @app.get("/api/usuarios")
    def listar_usuarios(_admin=Depends(exigir_admin)):
        return [
            {"id": str(u["id"]), "username": u["username"], "papel": u["papel"],
             "tema": u["tema"], "trocar_senha": u["trocar_senha"],
             "criado_em": u["criado_em"].isoformat()}
            for u in db.listar_usuarios()
        ]

    @app.post("/api/usuarios")
    def criar_usuario(payload: CriarUsuarioPayload, _admin=Depends(exigir_admin)):
        username = payload.username.strip()
        if not username or len(payload.senha) < 8:
            return JSONResponse(
                {"error": "usuario obrigatorio; senha com pelo menos 8 caracteres"},
                status_code=400)
        if payload.papel not in ("admin", "usuario"):
            return JSONResponse({"error": "papel invalido"}, status_code=400)
        if db.usuario_por_username(username) is not None:
            return JSONResponse({"error": "usuario ja existe"}, status_code=409)
        novo = db.criar_usuario(username, payload.senha, payload.papel)
        return {"status": "ok", "id": str(novo["id"]), "username": novo["username"]}

    @app.post("/api/usuarios/{usuario_id}/redefinir_senha")
    def redefinir_senha_de(usuario_id: str, payload: RedefinirSenhaPayload,
                           _admin=Depends(exigir_admin)):
        if len(payload.senha_nova) < 8:
            return JSONResponse({"error": "a nova senha precisa de pelo menos 8 caracteres"},
                                status_code=400)
        alvo = db.usuario_por_id(usuario_id)
        if alvo is None:
            return JSONResponse({"error": "usuario nao encontrado"}, status_code=404)
        db.redefinir_senha(usuario_id, payload.senha_nova, forcar_troca=True)
        return {"status": "ok"}

    @app.delete("/api/usuarios/{usuario_id}")
    def excluir_usuario(usuario_id: str, admin=Depends(exigir_admin)):
        if str(admin["id"]) == usuario_id:
            return JSONResponse({"error": "voce nao pode excluir seu proprio usuario"},
                                status_code=400)
        alvo = db.usuario_por_id(usuario_id)
        if alvo is None:
            return JSONResponse({"error": "usuario nao encontrado"}, status_code=404)
        if alvo["papel"] == "admin":
            outros_admins = any(
                u["papel"] == "admin" and str(u["id"]) != usuario_id
                for u in db.listar_usuarios()
            )
            if not outros_admins:
                return JSONResponse(
                    {"error": "este e o unico administrador; crie outro antes de excluir"},
                    status_code=400)
        db.excluir_usuario(usuario_id)
        return {"status": "ok"}

    print(">> Modulo de autenticacao instalado (login por sessao + PostgreSQL).")

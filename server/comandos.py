"""
Canal de DESCIDA: servidor -> dispositivo, sem o servidor precisar alcancar
a LAN.

O problema que isto resolve
---------------------------
Ate aqui o PTZ era `POST http://<ip-do-device>:8090/command/...`, disparado
PELO SERVIDOR. Isso so funciona enquanto servidor e equipamento estao na
mesma rede: com o servidor na nuvem e o Raspberry atras de NAT (que e o
cenario real), nao ha rota de fora para dentro e todo comando virava 502.

O desenho aqui inverte quem disca: o equipamento abre um WebSocket DE SAIDA
para o servidor e o mantem aberto. Comandos descem por essa conexao. Nada
precisa ser alcancavel de fora.

Duas camadas, de proposito separadas:

  * a FILA (`Fila`) e agnostica de transporte -- quem produz um comando nao
    sabe como ele sera entregue. Se um dia o transporte virar MQTT, so o
    adaptador muda;
  * o ADAPTADOR atual e o WebSocket em /api/edge/ws.

Quando o equipamento esta offline nada se perde do que importa: o "estado
desejado" continua descendo de carona na resposta do POST de telemetria
(o caminho antigo, que nunca dependeu de alcance reverso). O WebSocket e o
que da latencia de PTZ aceitavel -- sem ele o comando esperaria ate 1s.
"""
import asyncio
import json
import secrets
import time

from fastapi import WebSocket, WebSocketDisconnect

import registro_dispositivos as registro

# Quanto o servidor espera pela resposta de um comando que pediu confirmacao.
# Curto de proposito: o dashboard repete o comando de PTZ a cada 300ms, e
# esperar mais que isso so empilharia requisicoes no navegador.
TIMEOUT_RESPOSTA_S = 3.0

# Ping da aplicacao (alem do ping do proprio WebSocket): serve para o
# equipamento perceber rapido que a conexao morreu de um jeito silencioso
# (NAT que expira a traducao sem mandar RST -- comum em 4G).
INTERVALO_PING_S = 20.0


class Conexao:
    """Um equipamento conectado. Uma por dispositivo: se o mesmo token abrir
    outra, a anterior e derrubada (foi reinicio do agente, nao dois agentes)."""

    def __init__(self, ws: WebSocket, device_id: str, loop):
        self.ws = ws
        self.device_id = device_id
        self.loop = loop
        self.desde = time.time()
        self.pendentes = {}          # id do comando -> Future com a resposta
        self.lan_token = secrets.token_urlsafe(24)
        self.info = {}               # o que o agente contou de si no "ola"

    async def enviar(self, mensagem: dict):
        await self.ws.send_text(json.dumps(mensagem))

    def resolver(self, id_comando, corpo):
        fut = self.pendentes.pop(id_comando, None)
        if fut is not None and not fut.done():
            fut.set_result(corpo)

    def cancelar_pendentes(self, motivo):
        for fut in self.pendentes.values():
            if not fut.done():
                fut.set_exception(ConnectionError(motivo))
        self.pendentes.clear()


# device_id -> Conexao
_conexoes: dict[str, Conexao] = {}
_loop = None                          # loop do servidor, para chamadas vindas de threads


def conectado(device_id) -> bool:
    return str(device_id) in _conexoes


def info_conexao(device_id):
    c = _conexoes.get(str(device_id))
    if c is None:
        return None
    return {"conectado": True, "desde_s": round(time.time() - c.desde, 1),
            "agente": c.info}


def lan_token_de(device_id):
    """Segredo curto que autoriza o NAVEGADOR a falar direto com o agente na
    LAN (modo LAN). Nasce a cada conexao do agente -- nao e o token do
    dispositivo, que jamais deve chegar ao navegador."""
    c = _conexoes.get(str(device_id))
    return c.lan_token if c else None


# ============================================================================
# Envio
# ============================================================================
async def enviar(device_id, mensagem: dict) -> bool:
    """Manda sem esperar resposta. False = equipamento offline."""
    c = _conexoes.get(str(device_id))
    if c is None:
        return False
    try:
        await c.enviar(mensagem)
        return True
    except Exception:
        return False


async def comandar(device_id, rota, corpo=None, timeout=TIMEOUT_RESPOSTA_S):
    """Manda um comando e espera a resposta do agente.

    Levanta ConnectionError se o equipamento nao esta conectado -- quem
    chama decide o que fazer (o proxy de PTZ, por exemplo, ainda tenta o
    caminho HTTP direto para quem esta na mesma rede)."""
    c = _conexoes.get(str(device_id))
    if c is None:
        raise ConnectionError("equipamento nao conectado ao servidor")
    id_comando = secrets.token_hex(8)
    fut = asyncio.get_running_loop().create_future()
    c.pendentes[id_comando] = fut
    try:
        await c.enviar({"tipo": "comando", "id": id_comando,
                        "rota": rota, "corpo": corpo or {}})
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        c.pendentes.pop(id_comando, None)
        raise ConnectionError(f"o agente nao respondeu em {timeout}s")
    finally:
        c.pendentes.pop(id_comando, None)


def empurrar_estado_ws(device_id, estado) -> bool:
    """Chamado de THREADS (o estado desejado muda em rotas sincronas), por
    isso agenda no loop em vez de usar await. Devolve False na hora se o
    equipamento nao estiver conectado -- ai o chamador cai no caminho
    antigo (carona na telemetria)."""
    if _loop is None or not conectado(device_id):
        return False
    asyncio.run_coroutine_threadsafe(
        enviar(device_id, {"tipo": "estado", "estado": estado}), _loop)
    return True


# ============================================================================
# Instalacao
# ============================================================================
def instalar(app, ao_conectar=None):
    """`ao_conectar(device)` e chamado quando um equipamento sobe -- e por
    onde borda.py manda o estado desejado inicial."""

    @app.websocket("/api/edge/ws")
    async def edge_ws(ws: WebSocket):
        # Autenticacao ANTES do accept: o token do dispositivo vem no
        # header (agente) ou na query (clientes que nao mandam header).
        # A middleware de sessao do auth.py nao cobre websocket -- e outro
        # scope ASGI --, entao a checagem tem de ser explicita aqui.
        cabecalho = ws.headers.get("authorization", "")
        token = (cabecalho[7:].strip() if cabecalho.lower().startswith("bearer ")
                 else ws.query_params.get("token", ""))
        device = registro.por_token(token) if token else None
        if device is None:
            await ws.close(code=4401)
            return

        await ws.accept()
        anterior = _conexoes.get(device.id)
        if anterior is not None:
            # Reinicio do agente: a conexao velha ficou pendurada. Derruba,
            # senao os comandos iriam para um socket que nao le mais.
            anterior.cancelar_pendentes("substituida por uma conexao nova")
            try:
                await anterior.ws.close(code=4409)
            except Exception:
                pass

        conexao = Conexao(ws, device.id, asyncio.get_running_loop())
        _conexoes[device.id] = conexao
        print(f"[ws] '{device.nome}' conectado (descida ativa)")

        try:
            await conexao.enviar({"tipo": "bem_vindo", "device_id": device.id,
                                  "lan_token": conexao.lan_token,
                                  "ping_s": INTERVALO_PING_S})
            await conexao.enviar({"tipo": "estado",
                                  "estado": device.estado.snapshot()})
            if ao_conectar:
                ao_conectar(device)

            while True:
                bruto = await ws.receive_text()
                try:
                    msg = json.loads(bruto)
                except Exception:
                    continue
                tipo = msg.get("tipo")
                if tipo == "resposta":
                    conexao.resolver(msg.get("id"), msg.get("corpo"))
                elif tipo == "ola":
                    conexao.info = msg.get("info") or {}
                elif tipo == "ping":
                    await conexao.enviar({"tipo": "pong"})
        except WebSocketDisconnect:
            pass
        except Exception as e:
            print(f"[ws] '{device.nome}': {type(e).__name__}: {e}")
        finally:
            conexao.cancelar_pendentes("conexao encerrada")
            if _conexoes.get(device.id) is conexao:
                del _conexoes[device.id]
                print(f"[ws] '{device.nome}' desconectado")

    @app.on_event("startup")
    async def _guardar_loop():
        global _loop
        _loop = asyncio.get_running_loop()

    # registro_dispositivos empurra o estado desejado por aqui quando o
    # equipamento esta conectado; sem conexao ele cai sozinho no caminho
    # antigo (carona na resposta da telemetria).
    registro.definir_entregador_ws(empurrar_estado_ws)
    print(">> Canal de comandos instalado (WebSocket de saida em /api/edge/ws).")

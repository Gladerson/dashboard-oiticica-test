# ============================================================================
# transporte.py - Camada de transporte do agente de borda
#
# Duas implementacoes com a MESMA interface:
#
#   TransporteHTTP  -> testes na LAN, falando com o server.py do dashboard
#   TransporteMQTT  -> producao, falando com o ThingsBoard
#
# O canal de descida (servidor -> Pi) e um "estado desejado": o servidor nunca
# manda "comeca a transmitir agora", ele publica o estado em que quer o
# dispositivo e o dispositivo converge para ele. E o mesmo padrao de
# "intencao com prazo de validade" que o PTZMotion ja usa para o movimento da
# camera, so que atravessando a rede -- e por isso ele tolera perda de
# pacote, reinicio do servidor e reinicio do Pi sem travar em nenhum estado.
#
# No HTTP o estado desejado desce CARONA na resposta do POST de telemetria.
# Nao ha polling extra e nao e preciso que o servidor consiga abrir conexao
# de volta para o Pi (importante quando ele estiver atras de NAT/4G).
# ============================================================================
import json
import threading
import time

import requests


def agora_ms():
    return int(time.time() * 1000)


def envelope(valores, ts=None):
    """Formato de telemetria do ThingsBoard: {"ts": ms, "values": {...}}."""
    return {"ts": ts or agora_ms(), "values": valores}


# ----------------------------------------------------------------------------
class TransporteBase:
    nome = "base"

    def __init__(self, device_id):
        self.device_id = device_id
        self._estado = None
        self._estado_versao = -1
        self._lock = threading.Lock()
        self.ultimo_contato = 0.0

    def iniciar(self):
        pass

    def parar(self):
        pass

    def conectado(self):
        return True

    # -- subida ------------------------------------------------------------
    def telemetria(self, valores):
        raise NotImplementedError

    def deteccao(self, payload):
        raise NotImplementedError

    def frame(self, jpeg, meta):
        raise NotImplementedError

    def imagem(self, payload):
        raise NotImplementedError

    # -- descida -----------------------------------------------------------
    def _guardar_estado(self, estado):
        if not isinstance(estado, dict):
            return
        with self._lock:
            versao = int(estado.get("versao", 0))
            if versao >= self._estado_versao:
                self._estado = estado
                self._estado_versao = versao
            self.ultimo_contato = time.time()

    def estado_desejado(self):
        with self._lock:
            return dict(self._estado) if self._estado else None


# ----------------------------------------------------------------------------
class TransporteHTTP(TransporteBase):
    nome = "http"

    def __init__(self, device_id, server_url, timeout=4):
        super().__init__(device_id)
        self.base = server_url.rstrip("/")
        self.timeout = timeout
        self.sessao = requests.Session()
        self._falhas = 0

    def _post_json(self, rota, corpo):
        try:
            r = self.sessao.post(
                f"{self.base}{rota}",
                json=corpo,
                headers={"X-Device-Id": self.device_id},
                timeout=self.timeout,
            )
            self._falhas = 0
            if r.ok:
                try:
                    resposta = r.json()
                except ValueError:
                    return None
                self._guardar_estado(resposta.get("estado"))
                return resposta
        except Exception as e:
            self._falhas += 1
            if self._falhas in (1, 10, 100):
                print(f"[http] falha em {rota}: {e}")
        return None

    def telemetria(self, valores):
        return self._post_json("/api/edge/telemetria", envelope(valores))

    def deteccao(self, payload):
        return self._post_json("/api/edge/deteccao", envelope(payload))

    def imagem(self, payload):
        return self._post_json("/api/edge/imagem", envelope(payload))

    def frame(self, jpeg, meta):
        """JPEG cru no corpo. Base64 dentro de JSON custaria +33% de rede
        para transportar exatamente os mesmos pixels."""
        try:
            r = self.sessao.post(
                f"{self.base}/api/edge/frame",
                data=jpeg,
                headers={
                    "Content-Type": "image/jpeg",
                    "X-Device-Id": self.device_id,
                    "X-Meta": json.dumps(meta, separators=(",", ":")),
                },
                timeout=self.timeout,
            )
            if r.ok:
                try:
                    self._guardar_estado(r.json().get("estado"))
                except ValueError:
                    pass
        except Exception:
            pass

    def conectado(self):
        return self._falhas < 5


# ----------------------------------------------------------------------------
class TransporteMQTT(TransporteBase):
    """Compativel com a API MQTT de dispositivo do ThingsBoard.

      telemetria         -> v1/devices/me/telemetry
      atributos          -> v1/devices/me/attributes
      atributos (shared) <- v1/devices/me/attributes           (push)
                         <- v1/devices/me/attributes/response/+ (pedido inicial)
      RPC                <- v1/devices/me/rpc/request/+
                         -> v1/devices/me/rpc/response/{id}

    O frame de video NAO vai por telemetria: no ThingsBoard cada mensagem de
    telemetria e persistida no banco, e gravar 4 JPEG por segundo encheria o
    disco a troco de nada. Ele vai num topico proprio, efemero (QoS 0, sem
    retain), consumido por quem estiver assistindo.
    """
    nome = "mqtt"

    T_TELEMETRIA = "v1/devices/me/telemetry"
    T_ATRIBUTOS = "v1/devices/me/attributes"
    T_ATRIBUTOS_RESP = "v1/devices/me/attributes/response/+"
    T_ATRIBUTOS_PEDIDO = "v1/devices/me/attributes/request/1"
    T_RPC_REQ = "v1/devices/me/rpc/request/+"

    def __init__(self, device_id, host, port, token, tls=False,
                 topico_frame=None, keepalive=30):
        super().__init__(device_id)
        import paho.mqtt.client as mqtt  # import tardio: so quem usa MQTT paga

        self.host, self.port, self.token, self.tls = host, int(port), token, tls
        self.topico_frame = topico_frame or f"oiticica/{device_id}/frame"
        self.keepalive = keepalive
        self._conectado = False

        self.cli = mqtt.Client(client_id=f"{device_id}-{int(time.time())}")
        self.cli.username_pw_set(token, "")
        if tls:
            self.cli.tls_set()
        self.cli.on_connect = self._on_connect
        self.cli.on_disconnect = self._on_disconnect
        self.cli.on_message = self._on_message

    # -- ciclo de vida -----------------------------------------------------
    def iniciar(self):
        self.cli.connect_async(self.host, self.port, keepalive=self.keepalive)
        self.cli.loop_start()

    def parar(self):
        try:
            self.cli.loop_stop()
            self.cli.disconnect()
        except Exception:
            pass

    def conectado(self):
        return self._conectado

    # -- callbacks ---------------------------------------------------------
    def _on_connect(self, cli, _u, _f, rc):
        self._conectado = (rc == 0)
        if rc != 0:
            print(f"[mqtt] conexao recusada (rc={rc})")
            return
        print(f"[mqtt] conectado em {self.host}:{self.port}")
        cli.subscribe(self.T_ATRIBUTOS, qos=1)
        cli.subscribe(self.T_ATRIBUTOS_RESP, qos=1)
        cli.subscribe(self.T_RPC_REQ, qos=1)
        # Pede o estado atual agora, em vez de esperar a proxima mudanca:
        # sem isso, um Pi que reinicia fica com o estado padrao ate alguem
        # mexer no dashboard.
        cli.publish(
            self.T_ATRIBUTOS_PEDIDO,
            json.dumps({"sharedKeys": "estado_desejado"}),
            qos=1,
        )

    def _on_disconnect(self, _c, _u, rc):
        self._conectado = False
        print(f"[mqtt] desconectado (rc={rc})")

    def _on_message(self, cli, _u, msg):
        try:
            corpo = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return

        if msg.topic.startswith("v1/devices/me/rpc/request/"):
            rpc_id = msg.topic.rsplit("/", 1)[-1]
            metodo = corpo.get("method")
            params = corpo.get("params") or {}
            if metodo in ("estado_desejado", "definir_estado"):
                self._guardar_estado(params)
                cli.publish(
                    f"v1/devices/me/rpc/response/{rpc_id}",
                    json.dumps({"ok": True}), qos=1,
                )
            return

        # Atributos: tanto o push quanto a resposta ao pedido inicial trazem
        # o valor dentro de "shared" ou na raiz.
        alvo = corpo.get("shared", corpo)
        estado = alvo.get("estado_desejado")
        if isinstance(estado, str):
            try:
                estado = json.loads(estado)
            except Exception:
                estado = None
        if estado:
            self._guardar_estado(estado)

    # -- subida ------------------------------------------------------------
    def telemetria(self, valores):
        self.cli.publish(self.T_TELEMETRIA, json.dumps(envelope(valores)), qos=0)
        return None

    def deteccao(self, payload):
        self.cli.publish(self.T_TELEMETRIA, json.dumps(envelope(payload)), qos=1)
        return None

    def imagem(self, payload):
        self.cli.publish(self.T_TELEMETRIA, json.dumps(envelope(payload)), qos=1)
        return None

    def frame(self, jpeg, meta):
        self.cli.publish(self.topico_frame, jpeg, qos=0, retain=False)

    def atributos(self, valores):
        self.cli.publish(self.T_ATRIBUTOS, json.dumps(valores), qos=1)


# ----------------------------------------------------------------------------
def construir(nome, cfg):
    """Fabrica de transporte a partir do nome ('http' ou 'mqtt')."""
    if nome == "mqtt":
        if not cfg.MQTT_HOST or not cfg.MQTT_TOKEN:
            raise RuntimeError(
                "MQTT pedido, mas MQTT_HOST/MQTT_TOKEN nao estao no .env do Pi."
            )
        return TransporteMQTT(
            cfg.DEVICE_ID, cfg.MQTT_HOST, cfg.MQTT_PORT, cfg.MQTT_TOKEN,
            tls=cfg.MQTT_TLS, topico_frame=cfg.MQTT_TOPICO_FRAME,
        )
    return TransporteHTTP(cfg.DEVICE_ID, cfg.SERVER_URL)

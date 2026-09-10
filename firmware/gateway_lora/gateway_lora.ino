/* ===========================================================================
 * HydroConecta - GATEWAY: LoRa -> HTTPS
 *
 * Recebe pacotes LoRa de N controladores, aplica as regras de negocio de
 * cada equipamento e publica tudo num unico POST HTTPS para o servidor.
 *
 * O QUE MUDOU EM RELACAO A VERSAO 3.1 (MQTT/ThingsBoard)
 * -----------------------------------------------------
 *  - MQTT em texto claro para um IP fixo  ->  HTTPS para o dominio, com o
 *    certificado da Let's Encrypt conferido no proprio ESP32;
 *  - token do ThingsBoard  ->  token do dispositivo, do painel HydroConecta;
 *  - payload achatado por prefixo  ->  payload ANINHADO, um objeto por
 *    equipamento. O servidor separa sem precisar adivinhar prefixo
 *    (ver separar() em server/telemetria.py);
 *  - String do Arduino em todo o caminho quente  ->  buffers fixos. Um
 *    gateway que roda anos nao pode fragmentar o heap a cada 15 segundos;
 *  - pacote LoRa sem verificacao  ->  checksum. Antes, bastava ter dois '|'
 *    para uma leitura corrompida virar nivel de reservatorio;
 *  - watchdog ligado DEPOIS do setup de rede  ->  ligado ANTES. Era o pior
 *    ponto de indisponibilidade: um modem que nao respondesse travava o
 *    setup para sempre, sem ninguem para resgatar.
 *
 * MODELO DE DADOS (combinado com o servidor)
 * -----------------------------------------
 * Um gateway fala por N equipamentos, e cada equipamento tem N telemetrias:
 *
 *   { "values": {
 *       "nivel-rd01": { "status":"on", "distancia":12.500, ... },
 *       "nivel-rd02": { "status":"off" } } }
 *
 * No servidor cada equipamento vira um `sub_id`, e cada chave de dentro
 * vira uma telemetria daquele sub_id. E o que alimenta os widgets e os
 * alarmes da tela de Monitoramento.
 *
 * BIBLIOTECAS
 * -----------
 *  - TinyGSM (so no modo 4G)
 *  - HydroConecta (neste repositorio, em firmware/libraries)
 * Aponte o sketchbook da IDE para a pasta `firmware/` -- ver firmware/README.md.
 *
 * Placa: ESP32 Dev Module. Testado com ESP32 Arduino Core 3.x.
 * =========================================================================== */

// ===========================================================================
// CONFIGURACAO (EDITE AQUI)
// ===========================================================================

// Transporte do gateway. As duas linhas de cima sao apenas ROTULOS (0 e 1) e
// nao devem ser trocadas -- quem escolhe e a terceira linha.
// No Wi-Fi quem faz o TLS e o ESP32 (confere a cadeia, mas precisa de relogio
// certo -- ver sincronizarRelogio()); no 4G quem faz e o modem.
#define TIPO_CONEXAO_WIFI   0
#define TIPO_CONEXAO_4G     1
#define TIPO_CONEXAO        TIPO_CONEXAO_4G   // <-- troque so esta linha

// --- Servidor HydroConecta ---
// TEM de ser o dominio, nao o IP: o certificado e emitido para o nome, e a
// conferencia falha contra um IP.
static const char* SERVIDOR_HOST = "hydroconecta.com.br";
static const int   SERVIDOR_PORTA = 443;
static const char* SERVIDOR_ROTA  = "/api/edge/dados";

// Token do dispositivo, criado em Dispositivos -> Novo dispositivo (tipo
// "gateway") no painel. NAO e o token do ThingsBoard.
static const char* DEVICE_TOKEN = "COLE-AQUI-O-TOKEN-DO-GATEWAY";

// --- Wi-Fi (se TIPO_CONEXAO for WIFI) ---
static const char* WIFI_SSID = "COLE-AQUI-O-SSID";
static const char* WIFI_PASS = "COLE-AQUI-A-SENHA";

// --- 4G (se TIPO_CONEXAO for 4G) ---
// Vivo: zap.vivo.com.br (vivo/vivo) | Claro: java.claro.com.br (claro/claro)
// Tim: timbrasil.com.br (tim/tim)
static const char APN[]       = "java.claro.com.br";
static const char APN_USER[]  = "claro";
static const char APN_PASS[]  = "claro";

// --- Tempos ---
static const unsigned long INTERVALO_ENVIO_MS   = 15000;   // cadencia do POST
static const unsigned long TIMEOUT_SENSOR_MS    = 60000;   // sem pacote = "off"
static const unsigned long TEMPO_MAX_OFFLINE_MS = 1800000; // 30 min -> reboot
static const int  FALHAS_ATE_RECONECTAR = 4;               // POSTs seguidos

// ===========================================================================

#include <esp_task_wdt.h>
#include <HardwareSerial.h>
#include <time.h>
#include <sys/time.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

// Estes tres vem de firmware/libraries/HydroConecta/. Se der
// "fatal error: ProtocoloLoRa.h: No such file or directory", a pasta
// HydroConecta nao foi instalada -- ver "Instalar a biblioteca compartilhada"
// em firmware/README.md. Nao e erro de codigo, e um passo de instalacao.
#include <ProtocoloLoRa.h>
#include <MontadorJson.h>
#include <RegrasNivel.h>

#if TIPO_CONEXAO == TIPO_CONEXAO_4G
  #define TINY_GSM_MODEM_SIM7600
  #define TINY_GSM_RX_BUFFER 1024
  #include <TinyGsmClient.h>
  #define PIN_4G_RX 26   // ligado ao T (TX) do modulo
  #define PIN_4G_TX 27   // ligado ao R (RX) do modulo
#endif

#define PIN_LORA_RX 16
#define PIN_LORA_TX 17

// O watchdog cobre TAMBEM o setup. 180s da folga para o SIM7600 registrar na
// rede em area de sinal ruim, e ainda assim resgata um modem que nunca
// responde -- que antes travava o equipamento indefinidamente.
#define WDT_TIMEOUT_S 180

// Raiz da Let's Encrypt (ISRG Root X1), valida ate 2035-06-04.
// Extraida de /etc/ssl/certs/ISRG_Root_X1.pem -- NAO foi digitada a mao.
// SHA-256: 96:BC:EC:06:26:49:76:F3:74:60:77:9A:CF:28:C5:A7:CF:E8:A3:C0:
//          AA:E1:1A:8F:FC:EE:05:C0:BD:DF:08:C6
static const char CA_LETSENCRYPT[] PROGMEM = R"CERT(
-----BEGIN CERTIFICATE-----
MIIFazCCA1OgAwIBAgIRAIIQz7DSQONZRGPgu2OCiwAwDQYJKoZIhvcNAQELBQAw
TzELMAkGA1UEBhMCVVMxKTAnBgNVBAoTIEludGVybmV0IFNlY3VyaXR5IFJlc2Vh
cmNoIEdyb3VwMRUwEwYDVQQDEwxJU1JHIFJvb3QgWDEwHhcNMTUwNjA0MTEwNDM4
WhcNMzUwNjA0MTEwNDM4WjBPMQswCQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJu
ZXQgU2VjdXJpdHkgUmVzZWFyY2ggR3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBY
MTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAK3oJHP0FDfzm54rVygc
h77ct984kIxuPOZXoHj3dcKi/vVqbvYATyjb3miGbESTtrFj/RQSa78f0uoxmyF+
0TM8ukj13Xnfs7j/EvEhmkvBioZxaUpmZmyPfjxwv60pIgbz5MDmgK7iS4+3mX6U
A5/TR5d8mUgjU+g4rk8Kb4Mu0UlXjIB0ttov0DiNewNwIRt18jA8+o+u3dpjq+sW
T8KOEUt+zwvo/7V3LvSye0rgTBIlDHCNAymg4VMk7BPZ7hm/ELNKjD+Jo2FR3qyH
B5T0Y3HsLuJvW5iB4YlcNHlsdu87kGJ55tukmi8mxdAQ4Q7e2RCOFvu396j3x+UC
B5iPNgiV5+I3lg02dZ77DnKxHZu8A/lJBdiB3QW0KtZB6awBdpUKD9jf1b0SHzUv
KBds0pjBqAlkd25HN7rOrFleaJ1/ctaJxQZBKT5ZPt0m9STJEadao0xAH0ahmbWn
OlFuhjuefXKnEgV4We0+UXgVCwOPjdAvBbI+e0ocS3MFEvzG6uBQE3xDk3SzynTn
jh8BCNAw1FtxNrQHusEwMFxIt4I7mKZ9YIqioymCzLq9gwQbooMDQaHWBfEbwrbw
qHyGO0aoSCqI3Haadr8faqU9GY/rOPNk3sgrDQoo//fb4hVC1CLQJ13hef4Y53CI
rU7m2Ys6xt0nUW7/vGT1M0NPAgMBAAGjQjBAMA4GA1UdDwEB/wQEAwIBBjAPBgNV
HRMBAf8EBTADAQH/MB0GA1UdDgQWBBR5tFnme7bl5AFzgAiIyBpY9umbbjANBgkq
hkiG9w0BAQsFAAOCAgEAVR9YqbyyqFDQDLHYGmkgJykIrGF1XIpu+ILlaS/V9lZL
ubhzEFnTIZd+50xx+7LSYK05qAvqFyFWhfFQDlnrzuBZ6brJFe+GnY+EgPbk6ZGQ
3BebYhtF8GaV0nxvwuo77x/Py9auJ/GpsMiu/X1+mvoiBOv/2X/qkSsisRcOj/KK
NFtY2PwByVS5uCbMiogziUwthDyC3+6WVwW6LLv3xLfHTjuCvjHIInNzktHCgKQ5
ORAzI4JMPJ+GslWYHb4phowim57iaztXOoJwTdwJx4nLCgdNbOhdjsnvzqvHu7Ur
TkXWStAmzOVyyghqpZXjFaH3pO3JLF+l+/+sKAIuvtd7u+Nxe5AW0wdeRlN8NwdC
jNPElpzVmbUq4JUagEiuTDkHzsxHpFKVK7q4+63SM1N95R1NbdWhscdCb+ZAJzVc
oyi3B43njTOQ5yOf+1CceWxG1bQVs5ZufpsMljq4Ui0/1lvh+wjChP4kqKOJ2qxq
4RgqsahDYVvTH9w7jXbyLeiNdd8XM2w9U/t7y0Ff/9yi0GE44Za4rF2LN9d11TPA
mRGunUHBcnWEvgJBQl9nJEiU0Zsnvgc/ubhPgXRR4Xq37Z0j4r7g1SgEEzwxA57d
emyPxgcYxn/eR44/KJ4EBs+lVDR3veyJm+kXQ99b21/+jh5Xos1AnX5iItreGCc=
-----END CERTIFICATE-----
)CERT";

// ---------------------------------------------------------------------------
// Tabela de equipamentos: e aqui que se declara "este gateway fala por quem".
// O `id` vira o sub_id no servidor -- use o MESMO texto configurado no
// DEVICE_ID do controlador.
// ---------------------------------------------------------------------------
enum TipoEquipamento { TIPO_NIVEL_RADAR, TIPO_PIEZOMETRO };

struct Equipamento {
  const char*      id;
  TipoEquipamento  tipo;
  HcConfigNivel    nivel;   // usado so quando tipo == TIPO_NIVEL_RADAR
};

static const Equipamento EQUIPAMENTOS[] = {
  // id             tipo               vazio  cheio  volume(m3)    fundo   max     alerta
  { "nivel-rd01", TIPO_NIVEL_RADAR, { 30.0f, 10.0f,  742000000.0,  90.00f, 114.68f, 118.00f } },
  { "nivel-rd02", TIPO_NIVEL_RADAR, { 15.0f,  2.0f,      50000.0,  37.00f,  50.00f,  52.00f } },
};
static const int NUM_EQUIPAMENTOS = sizeof(EQUIPAMENTOS) / sizeof(EQUIPAMENTOS[0]);

// Estado por equipamento. Dimensionado PELA tabela: na versao anterior era um
// vetor fixo de 20, e a 21a linha na tabela corrompia memoria em silencio.
struct EstadoEquipamento {
  float         valor_bruto;
  bool          sensor_ok;
  bool          online;
  unsigned long visto_em;
  unsigned long pacotes;
  unsigned long descartados;   // checksum invalido: sinal de ruido no enlace
};
static EstadoEquipamento estados[NUM_EQUIPAMENTOS];

// ---------------------------------------------------------------------------
// Rede
// ---------------------------------------------------------------------------
HardwareSerial loraSerial(1);
WiFiClientSecure clienteTLS;

#if TIPO_CONEXAO == TIPO_CONEXAO_4G
  HardwareSerial SerialAT(2);
  TinyGsm modem(SerialAT);
  TinyGsmClientSecure clienteGsmTLS(modem);
#endif

// Reboots consecutivos sobrevivem ao ESP.restart() na RTC RAM. Sem esse
// limite, uma falha permanente (SIM sem credito, APN errada) viraria um
// ciclo de reboots que nunca conserta nada e ainda desgasta o hardware.
RTC_DATA_ATTR int rebootCount = 0;
static const int MAX_REBOOTS_CONSECUTIVOS = 3;

static char          rxBuf[192];
static size_t        rxLen = 0;
static unsigned long ultimoEnvio = 0;
static unsigned long inicioOffline = 0;
static int           falhasSeguidas = 0;
static unsigned long enviosOk = 0, enviosFalha = 0;

// Buffer do payload. Fixo e dimensionado para o pior caso: cada radar produz
// 9 chaves. 24 equipamentos ainda cabem com folga.
static char payload[3072];

// ===========================================================================
// SETUP
// ===========================================================================
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\n=== HydroConecta GATEWAY | boot #%d ===\n", rebootCount);

  // O WDT vem PRIMEIRO, antes de qualquer coisa de rede. Era exatamente o
  // contrario na versao anterior, e por isso um modem mudo travava o
  // equipamento para sempre: nao havia watchdog para resgatar o setup.
  esp_task_wdt_deinit();
  esp_task_wdt_config_t wdt = {
      .timeout_ms     = WDT_TIMEOUT_S * 1000,
      .idle_core_mask = (1 << 0) | (1 << 1),
      .trigger_panic  = true };
  esp_task_wdt_init(&wdt);
  esp_task_wdt_add(NULL);

  if (rebootCount >= MAX_REBOOTS_CONSECUTIVOS) {
    // Falha persistente: parar de reiniciar e esperar. Meia hora quieto e
    // melhor que um ciclo de reboots que nunca conserta -- e ainda da tempo
    // de a operadora ou a rede voltarem sozinhas.
    Serial.println("AVISO: reboots consecutivos demais. Aguardando 30 min.");
    rebootCount = 0;
    esperarComWatchdog(1800000UL);
  }

  loraSerial.setRxBufferSize(2048);   // margem para os pacotes que chegam
  loraSerial.begin(9600, SERIAL_8N1, PIN_LORA_RX, PIN_LORA_TX);

  for (int i = 0; i < NUM_EQUIPAMENTOS; i++) {
    estados[i].valor_bruto = 0.0f;
    estados[i].sensor_ok   = false;
    estados[i].online      = false;
    estados[i].visto_em    = 0;
    estados[i].pacotes     = 0;
    estados[i].descartados = 0;
  }

  conectarRede();
  sincronizarRelogio();
  configurarTLS();

  Serial.println("=== GATEWAY PRONTO ===");
  rebootCount = 0;   // setup completo: o contador de reboots zera
}

// ===========================================================================
// LOOP
// ===========================================================================
void loop() {
  esp_task_wdt_reset();

  processarLoRa();          // nunca bloqueia

  const bool rede = redeConectada();
  if (rede) {
    inicioOffline = 0;
  } else {
    if (inicioOffline == 0) inicioOffline = millis();
    if (millis() - inicioOffline > TEMPO_MAX_OFFLINE_MS) {
      Serial.println("CRITICO: 30 min sem rede. Reboot seguro.");
      rebootSeguro();
    }
    tentarReconectar();
  }

  if (millis() - ultimoEnvio >= INTERVALO_ENVIO_MS) {
    ultimoEnvio = millis();
    marcarSensoresSilenciosos();
    if (rede) {
      garantirRelogio();
      enviarTelemetria();
    }
  }
}

// ===========================================================================
// LORA (entrada dos controladores)
// ===========================================================================
void processarLoRa() {
  while (loraSerial.available()) {
    char c = (char)loraSerial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      rxBuf[rxLen] = '\0';
      if (rxLen > 0) tratarLinha(rxBuf);
      rxLen = 0;
      continue;
    }
    // Linha maior que o buffer: descarta ate o proximo \n em vez de deixar
    // o indice passar do fim.
    if (rxLen < sizeof(rxBuf) - 1) rxBuf[rxLen++] = c;
    else rxLen = 0;
  }
}

void tratarLinha(const char* linha) {
  char  id[32];
  float valor = 0.0f;
  bool  sensorOk = false;

  HcResultadoPacote r = hc_interpretar_pacote(linha, id, sizeof(id), &valor, &sensorOk);
  if (r == HC_PACOTE_CHECKSUM_INVALIDO || r == HC_PACOTE_MALFORMADO) {
    Serial.printf("[lora] pacote descartado (%s): %.60s\n",
                  r == HC_PACOTE_CHECKSUM_INVALIDO ? "checksum" : "formato", linha);
    return;
  }

  int idx = indiceDoEquipamento(id);
  if (idx < 0) {
    Serial.printf("[lora] id desconhecido: %s (falta na tabela EQUIPAMENTOS)\n", id);
    return;
  }
  if (r == HC_PACOTE_OK_SEM_CHECKSUM) {
    // Sensor ainda com firmware antigo. Aceito, mas anotado: e assim que se
    // descobre quem falta atualizar no campo.
    Serial.printf("[lora] %s: pacote SEM checksum (firmware antigo)\n", id);
  }

  estados[idx].valor_bruto = valor;
  estados[idx].sensor_ok   = sensorOk;
  estados[idx].online      = true;
  estados[idx].visto_em    = millis();
  estados[idx].pacotes++;

  Serial.printf("[lora] %s = %.3f (sensor %s)\n", id, valor, sensorOk ? "ok" : "erro");
  enviarAck(id);
}

void enviarAck(const char* id) {
  char ack[48];
  if (hc_montar_ack(ack, sizeof(ack), id) == 0) return;
  // O radio do controlador precisa de um instante para sair de transmissao e
  // entrar em recepcao. 150ms e curto o bastante para nao atrapalhar o loop.
  delay(150);
  loraSerial.println(ack);
}

int indiceDoEquipamento(const char* id) {
  for (int i = 0; i < NUM_EQUIPAMENTOS; i++)
    if (strcmp(EQUIPAMENTOS[i].id, id) == 0) return i;
  return -1;
}

void marcarSensoresSilenciosos() {
  for (int i = 0; i < NUM_EQUIPAMENTOS; i++) {
    if (estados[i].online && (millis() - estados[i].visto_em > TIMEOUT_SENSOR_MS)) {
      estados[i].online = false;
      Serial.printf("[lora] %s ficou mudo -> off\n", EQUIPAMENTOS[i].id);
    }
  }
}

// ===========================================================================
// PAYLOAD
// ===========================================================================
// Monta o corpo do POST. Devolve false se nao couber no buffer -- e nesse
// caso NADA e enviado: meio JSON viraria 400 no servidor e mandaria quem
// investiga para o lugar errado.
bool montarPayload() {
  HcJson j;
  hc_json_iniciar(&j, payload, sizeof(payload));
  hc_json_abrir_raiz(&j);

  // Lista explicita dos sub_ids. O servidor tambem sabe separar sozinho, mas
  // dizer quem sao elimina qualquer adivinhacao -- inclusive no primeiro
  // envio, quando ele ainda nao conhece nenhum equipamento deste gateway.
  hc_json_abrir_lista(&j, "dispositivos");
  for (int i = 0; i < NUM_EQUIPAMENTOS; i++) hc_json_item_texto(&j, EQUIPAMENTOS[i].id);
  hc_json_fechar_lista(&j);

  hc_json_abrir_objeto(&j, "values");
  for (int i = 0; i < NUM_EQUIPAMENTOS; i++) {
    hc_json_abrir_objeto(&j, EQUIPAMENTOS[i].id);
    montarEquipamento(&j, i);
    hc_json_fechar_objeto(&j);
  }
  hc_json_fechar_objeto(&j);   // values
  hc_json_fechar_objeto(&j);   // raiz

  if (!hc_json_ok(&j)) {
    Serial.println("[envio] payload nao coube no buffer -- NADA foi enviado. "
                   "Aumente payload[] ou reduza a tabela EQUIPAMENTOS.");
    return false;
  }
  return true;
}

void montarEquipamento(HcJson* j, int i) {
  const EstadoEquipamento& st = estados[i];

  // "off" = nao chega pacote; "erro" = chega, mas o sensor fisico falhou;
  // "on" = medindo. Sao tres coisas diferentes e o operador precisa
  // distinguir: sensor mudo e problema de enlace, sensor com erro e problema
  // do proprio instrumento.
  const char* status = !st.online ? "off" : (st.sensor_ok ? "on" : "erro");
  hc_json_texto(j, "status", status);
  hc_json_inteiro(j, "pacotes", (long long)st.pacotes);
  if (st.descartados > 0)
    hc_json_inteiro(j, "pacotes_descartados", (long long)st.descartados);

  if (strcmp(status, "on") != 0) return;   // sem medida valida, nada a calcular

  if (EQUIPAMENTOS[i].tipo == TIPO_NIVEL_RADAR) {
    HcNivel n;
    if (!hc_calcular_nivel(EQUIPAMENTOS[i].nivel, st.valor_bruto, &n)) {
      // Calibracao incoerente: diz isso, em vez de publicar numero inventado.
      hc_json_texto(j, "status", "erro");
      hc_json_texto(j, "erro", "calibracao-invalida");
      return;
    }
    hc_json_num(j, "distancia",       st.valor_bruto,    3);
    hc_json_num(j, "uso_percentual",  n.percentual,      2);
    hc_json_num(j, "volume_m3",       n.volume_m3,       0);
    hc_json_num(j, "volume_rest_m3",  n.volume_rest_m3,  0);
    hc_json_num(j, "cota_atual",      n.cota_atual,      3);
    hc_json_num(j, "cota_restante",   n.cota_restante,   3);
    hc_json_num(j, "cota_revanche",   EQUIPAMENTOS[i].nivel.cota_alerta, 2);
    hc_json_bool(j, "alerta_revanche", n.alerta_revanche);
  } else {
    hc_json_num(j, "leitura", st.valor_bruto, 3);
  }
}

// ===========================================================================
// ENVIO (HTTPS)
// ===========================================================================
void enviarTelemetria() {
  if (!montarPayload()) return;

  HTTPClient http;
  // Tempos curtos e explicitos: enquanto o POST acontece, o LoRa fica
  // acumulando no buffer da UART (2 KB, uns 60 pacotes). 8 + 8 segundos e
  // folga suficiente para 4G ruim sem chegar perto de perder pacote.
  http.setConnectTimeout(8000);
  http.setTimeout(8000);
  http.setReuse(false);

  char url[160];
  snprintf(url, sizeof(url), "https://%s:%d%s", SERVIDOR_HOST, SERVIDOR_PORTA, SERVIDOR_ROTA);

  bool aberto;
#if TIPO_CONEXAO == TIPO_CONEXAO_4G
  aberto = http.begin(clienteGsmTLS, SERVIDOR_HOST, SERVIDOR_PORTA, SERVIDOR_ROTA, true);
#else
  aberto = http.begin(clienteTLS, url);
#endif
  if (!aberto) {
    Serial.println("[envio] nao consegui abrir a conexao");
    contabilizarFalha();
    return;
  }

  char auth[128];
  snprintf(auth, sizeof(auth), "Bearer %s", DEVICE_TOKEN);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", auth);

  esp_task_wdt_reset();
  int codigo = http.POST((uint8_t*)payload, strlen(payload));
  esp_task_wdt_reset();

  if (codigo == 200) {
    enviosOk++;
    falhasSeguidas = 0;
    Serial.printf("[envio] OK (%u bytes, %lu ok / %lu falhas)\n",
                  (unsigned)strlen(payload), enviosOk, enviosFalha);
  } else if (codigo == 401) {
    // Nao adianta reconectar rede: o token e que esta errado. Dizer isso
    // evita horas procurando problema de sinal.
    Serial.println("[envio] 401: token do dispositivo invalido. "
                   "Confira DEVICE_TOKEN no painel (Dispositivos).");
    enviosFalha++;
  } else {
    Serial.printf("[envio] falhou: %d (%s)\n", codigo,
                  http.errorToString(codigo).c_str());
    contabilizarFalha();
  }
  http.end();
}

void contabilizarFalha() {
  enviosFalha++;
  falhasSeguidas++;
  if (falhasSeguidas >= FALHAS_ATE_RECONECTAR) {
    Serial.printf("[envio] %d falhas seguidas: refazendo a conexao de rede\n",
                  falhasSeguidas);
    falhasSeguidas = 0;
    reconectarRede();
  }
}

// ===========================================================================
// REDE
// ===========================================================================
bool redeConectada() {
#if TIPO_CONEXAO == TIPO_CONEXAO_4G
  return modem.isNetworkConnected() && modem.isGprsConnected();
#else
  return WiFi.status() == WL_CONNECTED;
#endif
}

void configurarTLS() {
  // A raiz da Let's Encrypt e conferida no proprio ESP32: sem isto, qualquer
  // um no caminho poderia se passar pelo servidor e capturar o token.
  clienteTLS.setCACert(CA_LETSENCRYPT);
  clienteTLS.setTimeout(8000);
#if TIPO_CONEXAO == TIPO_CONEXAO_4G
  // ATENCAO, e esta a limitacao conhecida desta versao: no caminho 4G quem
  // faz o TLS e o SIM7600, nao o ESP32, e a cadeia so e conferida se o
  // certificado raiz for carregado NO MODEM (AT+CCERTDOWN). Este sketch nao
  // faz isso. O trafego vai cifrado, mas sem autenticar o servidor.
  // Consequencia pratica: um ataque no meio do enlace da operadora poderia
  // capturar o DEVICE_TOKEN. Onde houver Wi-Fi, prefira Wi-Fi. Esta anotado
  // como pendencia no README.
  Serial.println("[tls] 4G: TLS pelo modem, SEM conferencia da cadeia (ver README).");
#endif
}

void conectarRede() {
#if TIPO_CONEXAO == TIPO_CONEXAO_4G
  Serial.println("[rede] iniciando modem 4G...");
  SerialAT.begin(115200, SERIAL_8N1, PIN_4G_RX, PIN_4G_TX);
  esperarComWatchdog(3000);

  // init() so manda "AT" e confere a resposta. restart() reseta o modulo e o
  // obriga a se registrar do zero na rede -- caro, e desnecessario quando ele
  // ja esta respondendo.
  if (!modem.init()) {
    Serial.println("[rede] modem mudo; forcando restart...");
    modem.restart();
    esperarComWatchdog(5000);
  }
  esp_task_wdt_reset();

  Serial.print("[rede] aguardando registro (60s)...");
  if (!modem.waitForNetwork(60000L)) {
    Serial.println(" falhou (o loop continua tentando)");
    return;
  }
  Serial.println(" registrado");
  esp_task_wdt_reset();

  Serial.printf("[rede] conectando APN %s ... ", APN);
  Serial.println(modem.gprsConnect(APN, APN_USER, APN_PASS) ? "OK" : "falhou");
#else
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);        // latencia estavel importa mais que os mA aqui
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("[rede] conectando Wi-Fi");
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
    esperarComWatchdog(500);
    Serial.print(".");
  }
  Serial.println(WiFi.status() == WL_CONNECTED ? " OK" : " falhou (o loop tenta)");
#endif
}

void tentarReconectar() {
  static unsigned long ultimaTentativa = 0;
  if (millis() - ultimaTentativa < 10000) return;
  ultimaTentativa = millis();
  esp_task_wdt_reset();
#if TIPO_CONEXAO == TIPO_CONEXAO_4G
  if (!modem.isNetworkConnected()) {
    Serial.println("[rede] sem registro; o modem procura a torre sozinho.");
    return;
  }
  Serial.println("[rede] GPRS caiu; refazendo o contexto...");
  modem.gprsDisconnect();
  esperarComWatchdog(1000);
  modem.gprsConnect(APN, APN_USER, APN_PASS);
#else
  Serial.println("[rede] Wi-Fi caiu; reconectando...");
  WiFi.reconnect();
#endif
}

// Chamado quando a rede volta: se o boot aconteceu sem hora (sem sinal, por
// exemplo), e aqui que ela e finalmente acertada -- senao o equipamento
// passaria a vida sem conseguir fechar o TLS.
void garantirRelogio() {
  time_t agora = 0;
  time(&agora);
  if (agora < 1700000000) sincronizarRelogio();
}

void reconectarRede() {
#if TIPO_CONEXAO == TIPO_CONEXAO_4G
  modem.gprsDisconnect();
  esperarComWatchdog(2000);
  modem.gprsConnect(APN, APN_USER, APN_PASS);
#else
  WiFi.disconnect();
  esperarComWatchdog(500);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
#endif
}

// ===========================================================================
// RELOGIO
//
// Conferir um certificado exige saber a data: o TLS recusa um certificado
// "ainda nao valido" se o relogio estiver antes do notBefore dele. E o ESP32
// acorda em 1970.
//
// No caminho 4G isso nao aparecia, porque quem faz o TLS e o modem -- e ele
// pega a hora da propria rede da operadora. No Wi-Fi, quem faz o TLS e o
// ESP32: sem acertar o relogio, TODO envio falha com um erro generico de
// handshake, e se perde tempo procurando problema de rede que nao existe.
// ===========================================================================
void sincronizarRelogio() {
  time_t agora = 0;

#if TIPO_CONEXAO == TIPO_CONEXAO_4G
  // O NTP do lwIP nao atravessa o modem; a hora vem do proprio SIM7600.
  int ano = 0, mes = 0, dia = 0, h = 0, m = 0, seg = 0; float fuso = 0;
  if (modem.getNetworkTime(&ano, &mes, &dia, &h, &m, &seg, &fuso) && ano > 2020) {
    struct tm t = {};
    t.tm_year = ano - 1900; t.tm_mon = mes - 1; t.tm_mday = dia;
    t.tm_hour = h; t.tm_min = m; t.tm_sec = seg;
    time_t utc = mktime(&t) - (time_t)(fuso * 900);   // fuso vem em 1/4 de hora
    struct timeval tv = { .tv_sec = utc, .tv_usec = 0 };
    settimeofday(&tv, NULL);
    agora = utc;
  }
#else
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  const unsigned long inicio = millis();
  while (agora < 1700000000 && millis() - inicio < 20000) {
    esperarComWatchdog(500);
    time(&agora);
  }
#endif

  if (agora < 1700000000) {
    Serial.println("[hora] NAO sincronizada -- o TLS provavelmente vai recusar "
                   "o certificado. O envio sera retentado a cada ciclo.");
    return;
  }
  struct tm t;
  gmtime_r(&agora, &t);
  Serial.printf("[hora] %04d-%02d-%02d %02d:%02d UTC\n",
                t.tm_year + 1900, t.tm_mon + 1, t.tm_mday, t.tm_hour, t.tm_min);
}

// ===========================================================================
// UTILITARIOS
// ===========================================================================
// delay() longo NUNCA e chamado direto: um delay maior que o watchdog
// reiniciaria o equipamento no meio de uma espera proposital.
void esperarComWatchdog(unsigned long ms) {
  const unsigned long inicio = millis();
  while (millis() - inicio < ms) {
    esp_task_wdt_reset();
    delay(100);
  }
}

// Desliga o radio do modem antes de reiniciar. Sem isso o SIM7600 pode voltar
// num estado que a operadora recusa, e o proximo boot ja nasce sem rede.
void rebootSeguro() {
  rebootCount++;
  Serial.printf("[reboot] #%d\n", rebootCount);
#if TIPO_CONEXAO == TIPO_CONEXAO_4G
  modem.gprsDisconnect();
  esperarComWatchdog(1000);
  modem.poweroff();
  esperarComWatchdog(3000);
#endif
  Serial.println("[reboot] reiniciando ESP32...");
  delay(200);
  ESP.restart();
}

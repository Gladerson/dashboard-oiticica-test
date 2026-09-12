/* ===========================================================================
 * HydroConecta - GATEWAY: LoRa -> HTTPS, por Wi-Fi ou 4G
 *
 * Recebe pacotes LoRa de N controladores e repassa tudo num unico POST HTTPS
 * para o servidor. So isso: o gateway NAO calcula mais nada.
 *
 * CADA UM COM O SEU PAPEL (mudou em setembro/2026)
 * -----------------------------------------------
 *   controlador  ->  mede e entrega a distancia BRUTA;
 *   gateway      ->  cuida do enlace e repassa o que recebeu;
 *   servidor     ->  aplica a calibracao daquele reservatorio.
 *
 * Antes, cota, volume e percentual sairam daqui (RegrasNivel.h). Parecia
 * economia -- o numero ja subia pronto --, mas punha a regra de negocio no
 * lugar mais caro de mudar do sistema inteiro: recalibrar um reservatorio
 * exigia subir numa torre, ligar um notebook num ESP32 e regravar firmware,
 * a centenas de quilometros de distancia. Agora a calibracao e um cadastro
 * na tela (Dispositivos -> Sensor -> Radar de nivel) e o servidor faz a
 * conta na chegada (server/sensores.py). O gateway ficou menor, mais rapido
 * e sem nenhuma razao para ser reprogramado quando a barragem muda.
 *
 * DOIS ENLACES, TROCA AUTOMATICA
 * ------------------------------
 * Wi-Fi e 4G ficam os DOIS compilados. O Wi-Fi tem prioridade (na barragem
 * ha Starlink); se ele cair, o gateway passa para o 4G sozinho e volta para
 * o Wi-Fi assim que ele se firmar de novo. Ver "ENLACE" mais abaixo -- e a
 * parte mais delicada deste arquivo, e tem historese nos dois sentidos para
 * nao ficar pingando entre um e outro.
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
 *  - TinyGSM (sempre: os dois enlaces sao compilados juntos)
 *  - HydroConecta (neste repositorio, em firmware/libraries)
 * Aponte o sketchbook da IDE para a pasta `firmware/` -- ver firmware/README.md.
 *
 * Placa: ESP32 Dev Module. Testado com ESP32 Arduino Core 3.x.
 * =========================================================================== */

// ===========================================================================
// CONFIGURACAO (EDITE AQUI)
// ===========================================================================

// Quais enlaces este gateway tem fisicamente. Os dois ligados e o caso
// normal; desligue um so se a placa realmente nao tiver aquele hardware --
// um modem que nao existe custa alguns segundos de tentativa a cada boot.
#define TEM_WIFI  1
#define TEM_4G    1

// --- Servidor HydroConecta ---
// TEM de ser o dominio, nao o IP: o certificado e emitido para o nome, e a
// conferencia falha contra um IP.
static const char* SERVIDOR_HOST = "hydroconecta.com.br";
static const int   SERVIDOR_PORTA = 443;
static const char* SERVIDOR_ROTA  = "/api/edge/dados";

// Token do dispositivo, criado em Dispositivos -> Novo dispositivo (tipo
// "gateway") no painel. NAO e o token do ThingsBoard.
static const char* DEVICE_TOKEN = "COLE-AQUI-O-TOKEN-DO-GATEWAY";

// --- Wi-Fi (enlace preferido) ---
static const char* WIFI_SSID = "COLE-AQUI-O-SSID";
static const char* WIFI_PASS = "COLE-AQUI-A-SENHA";

// --- 4G (reserva automatica) ---
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

// --- Historese da troca de enlace ---
// Os dois numeros existem para o gateway NAO ficar pingando entre Wi-Fi e 4G.
// Uma Starlink que oscila alguns segundos e normal; trocar de enlace a cada
// oscilacao gastaria dados moveis e ainda perderia envios na transicao.
static const unsigned long WIFI_TOLERANCIA_MS = 25000;   // sem Wi-Fi ate cair para o 4G
static const unsigned long WIFI_ESTAVEL_MS    = 60000;   // Wi-Fi firme ate voltar para ele
// Tem de ser bem MENOR que a tolerancia: com 20 s aqui e 25 s de tolerancia,
// o gateway tinha uma unica chance de reconectar antes de cair para o 4G.
static const unsigned long WIFI_RETENTAR_MS   = 8000;    // intervalo entre WiFi.begin()

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
// RegrasNivel.h saiu daqui: a conta de cota/volume e do SERVIDOR agora
// (server/sensores.py). O gateway so repassa a medida bruta.
#include <ProtocoloLoRa.h>
#include <MontadorJson.h>

#if TEM_4G
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
//
// Sao SO os identificadores. A calibracao de cada reservatorio (as duas
// ancoras, o volume de referencia, as cotas de vertimento) mora no cadastro
// do servidor, em Dispositivos -> Sensor -> Radar de nivel. Antes ela vinha
// nesta tabela, e mudar uma cota significava regravar firmware em campo.
//
// O `id` vira o sub_id no servidor: use o MESMO texto configurado no
// DEVICE_ID do controlador E no campo "Identificador no gateway" do cadastro
// do sensor. Se os tres nao baterem, a medida chega e fica sem dono.
// ---------------------------------------------------------------------------
static const char* EQUIPAMENTOS[] = {
  "nivel-rd01",
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

#if TEM_4G
  HardwareSerial SerialAT(2);
  TinyGsm modem(SerialAT);
  TinyGsmClientSecure clienteGsmTLS(modem);
#endif

// ---------------------------------------------------------------------------
// Estado do enlace. Declarado AQUI, junto dos outros globais, e nao la
// embaixo com as funcoes que o usam: contabilizarFalha() menciona
// `enlaceAtual` bem antes da secao REDE, e a geracao automatica de
// prototipos da Arduino IDE nao move declaracoes de TIPO -- so de funcao.
// ---------------------------------------------------------------------------
enum Enlace { ENLACE_NENHUM = 0, ENLACE_WIFI, ENLACE_4G };

static Enlace        enlaceAtual         = ENLACE_NENHUM;
static unsigned long enlaceDesde         = 0;
static unsigned long trocasDeEnlace      = 0;
static unsigned long wifiCaiuEm          = 0;  // quando o Wi-Fi sumiu (0 = no ar)
static unsigned long wifiVoltouEm        = 0;  // quando o Wi-Fi reapareceu
static unsigned long ultimaTentativaWifi = 0;

#if TEM_4G
enum EstadoModem {
  MODEM_PARADO,       // serial ainda nao aberta
  MODEM_INICIANDO,    // respondendo a AT?
  MODEM_REGISTRANDO,  // procurando torre
  MODEM_REGISTRADO,   // achou a torre e esta de prontidao, SEM gastar dados
  MODEM_CONTEXTO,     // subindo o APN
  MODEM_PRONTO,       // dados disponiveis
  MODEM_AUSENTE,      // nao respondeu: provavelmente nao ha modem nesta placa
};
static EstadoModem   estadoModem     = MODEM_PARADO;
static unsigned long modemPassoEm    = 0;
static int           modemTentativas = 0;
#endif

// Prototipos. A Arduino IDE geraria estes sozinha, mas escreve-los deixa o
// arquivo compilavel tambem por um g++ comum -- e e isso que permite a suite
// em firmware/testes/ exercitar a troca de enlace num PC, sem hardware.
void  setup();
void  loop();
void  processarLoRa();
void  tratarLinha(const char* linha);
void  enviarAck(const char* id);
int   indiceDoEquipamento(const char* id);
void  marcarSensoresSilenciosos();
bool  montarPayload();
void  montarEquipamento(HcJson* j, int i);
void  enviarTelemetria();
void  contabilizarFalha();
const char* nomeDoEnlace(Enlace e);
bool  modemTemDados();
void  cuidarDoModem(bool precisaDeDados);
void  soltarDadosDoModem();
bool  wifiNoAr();
void  iniciarWifi();
void  cuidarDoWifi();
void  trocarEnlace(Enlace novo);
void  cuidarDaRede();
bool  redeConectada();
void  configurarTLS();
void  garantirRelogio();
int   sinalDoEnlace();
void  sincronizarRelogio();
void  esperarComWatchdog(unsigned long ms);
void  rebootSeguro();

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

// Buffer do payload. Fixo e dimensionado para o pior caso. Cada equipamento
// gasta ~90 bytes agora (antes eram ~250, com as nove chaves calculadas), e
// o bloco do proprio gateway leva uns 180. Passar de 24 equipamentos ainda
// cabe -- e se nao couber, montarPayload() recusa e diz, em vez de enviar
// meio JSON.
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

  configurarTLS();
  iniciarWifi();

  // A rede sobe pelo LOOP, nao aqui. O setup nao espera por enlace nenhum:
  // com Starlink fora do ar e 4G sem sinal, esperar aqui seria ficar preso
  // antes mesmo de comecar a receber LoRa -- e os pacotes dos controladores
  // se perderiam enquanto isso. cuidarDaRede() e chamada a cada volta e
  // resolve tudo em segundo plano.
  cuidarDaRede();
  sincronizarRelogio();

  Serial.println("=== GATEWAY PRONTO ===");
  rebootCount = 0;   // setup completo: o contador de reboots zera
}

// ===========================================================================
// LOOP
// ===========================================================================
void loop() {
  esp_task_wdt_reset();

  processarLoRa();          // nunca bloqueia
  cuidarDaRede();           // escolhe Wi-Fi ou 4G; tambem nunca bloqueia

  const bool rede = redeConectada();
  if (rede) {
    inicioOffline = 0;
  } else {
    if (inicioOffline == 0) inicioOffline = millis();
    if (millis() - inicioOffline > TEMPO_MAX_OFFLINE_MS) {
      // 30 min sem NENHUM dos dois enlaces. Nao e oscilacao: e alguma coisa
      // travada que so um reinicio resolve.
      Serial.println("CRITICO: 30 min sem rede (nem Wi-Fi nem 4G). Reboot seguro.");
      rebootSeguro();
    }
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
    if (strcmp(EQUIPAMENTOS[i], id) == 0) return i;
  return -1;
}

void marcarSensoresSilenciosos() {
  for (int i = 0; i < NUM_EQUIPAMENTOS; i++) {
    if (estados[i].online && (millis() - estados[i].visto_em > TIMEOUT_SENSOR_MS)) {
      estados[i].online = false;
      Serial.printf("[lora] %s ficou mudo -> off\n", EQUIPAMENTOS[i]);
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
  for (int i = 0; i < NUM_EQUIPAMENTOS; i++) hc_json_item_texto(&j, EQUIPAMENTOS[i]);
  hc_json_fechar_lista(&j);

  hc_json_abrir_objeto(&j, "values");

  // Telemetria do PROPRIO gateway. Sobe solta (nao dentro de nenhum
  // equipamento), entao no servidor ela fica em sub_id vazio -- "o proprio
  // dispositivo" -- e pode virar widget como qualquer outra.
  //
  // Os nomes evitam de proposito ser prefixo uns dos outros: a separacao do
  // payload usa o prefixo como pista para descobrir equipamentos, e uma
  // chave "enlace" ao lado de "enlace_ha_s" faria o servidor achar que
  // existe um equipamento chamado "enlace".
  hc_json_texto(&j, "enlace", nomeDoEnlace(enlaceAtual));
  hc_json_inteiro(&j, "segundos_no_enlace",
                  enlaceDesde ? (long long)((millis() - enlaceDesde) / 1000) : 0);
  hc_json_inteiro(&j, "trocas_de_enlace", (long long)trocasDeEnlace);
  const int sinal = sinalDoEnlace();
  if (sinal != -127) hc_json_inteiro(&j, "sinal", (long long)sinal);
  hc_json_inteiro(&j, "uptime_s", (long long)(millis() / 1000));
  hc_json_inteiro(&j, "envios_ok", (long long)enviosOk);
  hc_json_inteiro(&j, "envios_falha", (long long)enviosFalha);

  for (int i = 0; i < NUM_EQUIPAMENTOS; i++) {
    hc_json_abrir_objeto(&j, EQUIPAMENTOS[i]);
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

  if (strcmp(status, "on") != 0) return;   // sem medida valida, nada a publicar

  // A MEDIDA BRUTA, e so ela. Cota, volume e percentual sao derivados no
  // servidor a partir dela (server/sensores.py) -- o gateway nao precisa
  // saber nem que o equipamento e um radar de nivel.
  hc_json_num(j, "distancia", st.valor_bruto, 3);
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

  // Cada enlace tem o seu cliente TLS, e quem escolhe e o enlace em uso
  // NESTE instante -- nao uma opcao de compilacao.
  bool aberto = false;
#if TEM_4G
  if (enlaceAtual == ENLACE_4G) {
    aberto = http.begin(clienteGsmTLS, SERVIDOR_HOST, SERVIDOR_PORTA, SERVIDOR_ROTA, true);
  }
#endif
#if TEM_WIFI
  if (enlaceAtual == ENLACE_WIFI) aberto = http.begin(clienteTLS, url);
#endif
  if (enlaceAtual == ENLACE_NENHUM) {
    // Sem enlace nao se tenta: o POST falharia, contaria como falha e
    // dispararia uma reconexao que nao tem nada a consertar.
    return;
  }
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
  if (falhasSeguidas < FALHAS_ATE_RECONECTAR) return;
  falhasSeguidas = 0;
  // O enlace diz que esta no ar, mas os POSTs nao passam. Derruba-lo obriga
  // cuidarDaRede() a reconstruir -- e, se o problema for daquele enlace,
  // abre caminho para o outro assumir.
  Serial.printf("[envio] %d falhas seguidas no enlace %s: derrubando para reconstruir\n",
                FALHAS_ATE_RECONECTAR, nomeDoEnlace(enlaceAtual));
#if TEM_WIFI
  if (enlaceAtual == ENLACE_WIFI) {
    WiFi.disconnect();
    ultimaTentativaWifi = 0;   // pode tentar de novo ja na proxima volta
  }
#endif
#if TEM_4G
  if (enlaceAtual == ENLACE_4G && estadoModem == MODEM_PRONTO) {
    modem.gprsDisconnect();
    estadoModem = MODEM_REGISTRANDO;
  }
#endif
}

// ===========================================================================
// ENLACE: Wi-Fi com 4G de reserva, troca automatica nos dois sentidos
//
// Esta e a parte mais delicada do arquivo. O gateway fica a centenas de
// quilometros de quem pode mexer nele: nada aqui pode bloquear o loop, e
// nenhuma falha de um enlace pode impedir o outro de funcionar.
//
// REGRAS
//   1. Wi-Fi e preferido. Na barragem ha Starlink; o 4G e reserva paga.
//   2. Sem Wi-Fi por WIFI_TOLERANCIA_MS, sobe o 4G e passa a usar.
//   3. Com Wi-Fi firme por WIFI_ESTAVEL_MS, volta para ele e derruba o
//      contexto de dados do modem (para de gastar franquia).
//   4. O modem NAO e desligado ao voltar para o Wi-Fi: fica registrado na
//      operadora, so sem contexto de dados. Religar o radio custaria uns 30 s
//      na proxima queda -- e a proxima queda e exatamente quando nao se pode
//      esperar. Registrado, o contexto volta em poucos segundos.
//   5. O radio Wi-Fi fica sempre ligado e sempre tentando, mesmo enquanto o
//      4G trabalha: e assim que o gateway percebe que a Starlink voltou.
//
// NADA AQUI BLOQUEIA. A subida do modem e uma maquina de estados com passos
// curtos (cuidarDoModem()), chamada uma vez por volta do loop. A versao
// anterior chamava modem.waitForNetwork(60000) de uma vez -- 60 segundos sem
// processar LoRa, sem alimentar o watchdog por conta propria e sem chance de
// perceber que o Wi-Fi tinha voltado.
// ===========================================================================
const char* nomeDoEnlace(Enlace e) {
  return e == ENLACE_WIFI ? "wifi" : (e == ENLACE_4G ? "4g" : "sem-rede");
}

// --- Modem: maquina de estados, passos curtos --------------------------------
#if TEM_4G
// Quantas vezes insistir antes de aceitar que nao ha modem. Aceitar e
// importante: um gateway so-Wi-Fi nao pode gastar meio loop tentando falar
// com um chip que nao existe.
static const int MODEM_MAX_TENTATIVAS = 3;
static const unsigned long MODEM_REGISTRO_MAX_MS = 90000;
static const unsigned long MODEM_AUSENTE_RETENTAR_MS = 600000;   // 10 min

bool modemTemDados() {
  return estadoModem == MODEM_PRONTO &&
         modem.isNetworkConnected() && modem.isGprsConnected();
}

/** Um passo por volta do loop. Nunca demora mais que alguns segundos. */
void cuidarDoModem(bool precisaDeDados) {
  const unsigned long agora = millis();

  switch (estadoModem) {
    case MODEM_AUSENTE:
      // Tenta de novo de vez em quando: pode ser um modem que estava sem
      // energia no boot, e nao um modem inexistente.
      if (agora - modemPassoEm > MODEM_AUSENTE_RETENTAR_MS) {
        estadoModem = MODEM_PARADO;
        modemTentativas = 0;
      }
      return;

    case MODEM_PARADO:
      SerialAT.begin(115200, SERIAL_8N1, PIN_4G_RX, PIN_4G_TX);
      estadoModem  = MODEM_INICIANDO;
      modemPassoEm = agora;
      return;

    case MODEM_INICIANDO: {
      esp_task_wdt_reset();
      // init() so manda "AT" e confere a resposta -- barato. restart() reseta
      // o modulo e o obriga a se registrar do zero, o que so vale a pena
      // quando ele esta mesmo mudo.
      if (modem.init()) {
        Serial.println("[4g] modem respondeu");
        estadoModem  = MODEM_REGISTRANDO;
        modemPassoEm = agora;
        modemTentativas = 0;
        return;
      }
      modemTentativas++;
      Serial.printf("[4g] modem mudo (tentativa %d/%d)\n",
                    modemTentativas, MODEM_MAX_TENTATIVAS);
      if (modemTentativas == 1) { modem.restart(); esp_task_wdt_reset(); }
      if (modemTentativas >= MODEM_MAX_TENTATIVAS) {
        Serial.println("[4g] sem modem nesta placa; seguindo so com Wi-Fi.");
        estadoModem  = MODEM_AUSENTE;
        modemPassoEm = agora;
      }
      return;
    }

    case MODEM_REGISTRANDO:
      esp_task_wdt_reset();
      // waitForNetwork com 1 s: consulta o registro e devolve. Chamado uma
      // vez por volta do loop, faz o mesmo que a espera de 60 s da versao
      // anterior -- sem parar o resto do gateway enquanto isso.
      if (modem.waitForNetwork(1000)) {
        Serial.println("[4g] registrado na operadora");
        estadoModem  = MODEM_REGISTRADO;
        modemPassoEm = agora;
        return;
      }
      if (agora - modemPassoEm > MODEM_REGISTRO_MAX_MS) {
        Serial.println("[4g] 90 s sem registro; recomecando o modem");
        estadoModem  = MODEM_PARADO;
        modemPassoEm = agora;
      }
      return;

    case MODEM_REGISTRADO:
      // Registrado e quieto: este e o estado de repouso enquanto o Wi-Fi
      // funciona. Registrado nao gasta franquia, e voltar dele para o
      // contexto leva segundos -- contra os ~30 s de religar o radio.
      //
      // Este estado e a correcao de um defeito que a suite pegou: antes,
      // soltar o contexto voltava para MODEM_REGISTRANDO, que subia o
      // contexto de novo na volta seguinte do loop. O gateway "soltava" os
      // dados moveis e os retomava meio segundo depois, para sempre.
      if (!precisaDeDados) {
        if (!modem.isNetworkConnected()) {
          estadoModem  = MODEM_REGISTRANDO;
          modemPassoEm = agora;
        }
        return;
      }
      estadoModem  = MODEM_CONTEXTO;
      modemPassoEm = agora;
      return;

    case MODEM_CONTEXTO:
      esp_task_wdt_reset();
      Serial.printf("[4g] subindo APN %s ... ", APN);
      if (modem.gprsConnect(APN, APN_USER, APN_PASS)) {
        Serial.println("OK");
        estadoModem  = MODEM_PRONTO;
        modemPassoEm = agora;
      } else {
        Serial.println("falhou");
        modemPassoEm = agora;
        estadoModem  = MODEM_REGISTRANDO;   // reconfere o registro e tenta de novo
      }
      if (!precisaDeDados && estadoModem == MODEM_PRONTO) {
        // O Wi-Fi voltou no meio da subida: nao vale a pena manter o
        // contexto que acabou de nascer.
        modem.gprsDisconnect();
        estadoModem = MODEM_REGISTRADO;
      }
      esp_task_wdt_reset();
      return;

    case MODEM_PRONTO:
      if (!precisaDeDados) return;          // fica de prontidao, sem mexer
      if (!modem.isNetworkConnected() || !modem.isGprsConnected()) {
        Serial.println("[4g] contexto caiu; refazendo");
        estadoModem  = MODEM_REGISTRANDO;
        modemPassoEm = agora;
      }
      return;
  }
}

/** Derruba so o contexto de dados; o modem continua registrado. Ver a regra 4
 *  no cabecalho desta secao. */
void soltarDadosDoModem() {
  if (estadoModem != MODEM_PRONTO) return;
  Serial.println("[4g] Wi-Fi voltou: soltando o contexto de dados");
  modem.gprsDisconnect();
  // REGISTRADO, nao REGISTRANDO: ver o comentario naquele estado. Voltar
  // para "registrando" faria o contexto subir de novo na volta seguinte.
  estadoModem = MODEM_REGISTRADO;
  modemPassoEm = millis();
}
#else
// Sem hardware de 4G: as mesmas funcoes, sem fazer nada. Assim o resto do
// arquivo nao precisa de um unico #if a mais.
bool modemTemDados() { return false; }
void cuidarDoModem(bool) {}
void soltarDadosDoModem() {}
#endif  // TEM_4G

// --- Wi-Fi -------------------------------------------------------------------
bool wifiNoAr() {
#if TEM_WIFI
  return WiFi.status() == WL_CONNECTED;
#else
  return false;
#endif
}

/** Liga o radio e faz a PRIMEIRA tentativa. Chamada uma vez, no setup.
 *
 *  Sem WiFi.mode(WIFI_STA) o ESP32 nasce em modo AP+STA e pode simplesmente
 *  nao conectar; setSleep(false) troca alguns mA por latencia estavel, que e
 *  o que importa num equipamento alimentado da rede. */
void iniciarWifi() {
#if TEM_WIFI
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  ultimaTentativaWifi = millis();
  Serial.println("[wifi] radio ligado, procurando a rede");
#endif
}

void cuidarDoWifi() {
#if TEM_WIFI
  if (wifiNoAr()) return;
  // WiFi.setAutoReconnect() ja tenta sozinho, mas ele desiste em alguns
  // cenarios (AP que some por muito tempo, senha recusada uma vez). Um
  // begin() periodico e a rede de seguranca que faz a Starlink ser
  // reencontrada depois de horas fora.
  //
  // ultimaTentativaWifi == 0 significa "nunca tentei": e o caso do primeiro
  // loop se iniciarWifi() nao tiver rodado. Sem esta guarda, o gateway
  // passava os primeiros WIFI_RETENTAR_MS sem sequer chamar begin() --
  // millis() ainda e pequeno no boot, e a subtracao nao alcancava o limite.
  if (ultimaTentativaWifi != 0 &&
      millis() - ultimaTentativaWifi < WIFI_RETENTAR_MS) return;
  ultimaTentativaWifi = millis();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
#endif
}

// --- Decisao -----------------------------------------------------------------
void trocarEnlace(Enlace novo) {
  if (novo == enlaceAtual) return;
  Serial.printf("[enlace] %s -> %s\n", nomeDoEnlace(enlaceAtual), nomeDoEnlace(novo));
  // Conta so a troca entre DOIS enlaces de verdade (wifi <-> 4g). Uma piscada
  // do Wi-Fi passa por "sem-rede" e volta: contar isso faria a telemetria
  // dizer "12 trocas de enlace" num dia em que o gateway nunca saiu do
  // Wi-Fi -- e quem olha o numero quer saber se o 4G esta sendo acionado.
  if (novo != ENLACE_NENHUM && enlaceAtual != ENLACE_NENHUM) trocasDeEnlace++;
  enlaceAtual = novo;
  enlaceDesde = millis();
  // Uma conexao TLS aberta pertence ao enlace anterior. Zerar as falhas
  // seguidas evita que a troca -- que e a SOLUCAO -- seja contada como
  // problema e dispare uma reconexao logo em seguida.
  falhasSeguidas = 0;
}

/** Chamada uma vez por volta do loop. Decide em qual enlace o proximo POST
 *  vai sair, e cuida dos dois para que a decisao tenha o que escolher. */
void cuidarDaRede() {
  const unsigned long agora = millis();

  cuidarDoWifi();

  const bool wifi = wifiNoAr();
  if (wifi) {
    if (wifiCaiuEm != 0) {            // acabou de voltar
      wifiCaiuEm = 0;
      wifiVoltouEm = agora;
    }
    if (wifiVoltouEm == 0) wifiVoltouEm = agora;
  } else {
    wifiVoltouEm = 0;
    if (wifiCaiuEm == 0) wifiCaiuEm = agora;
  }

  // Quanto tempo o Wi-Fi esta fora. Se ja passou da tolerancia, vale a pena
  // ter o 4G de pe -- mesmo que o Wi-Fi volte em seguida, ter o contexto
  // pronto e o que torna a proxima queda indolor.
  const bool wifiForaHaMuito = !wifi && (agora - wifiCaiuEm >= WIFI_TOLERANCIA_MS);
  const bool queroDados = wifiForaHaMuito || enlaceAtual == ENLACE_4G;
  cuidarDoModem(queroDados);

  // ---- escolha ----
  if (wifi) {
    if (enlaceAtual != ENLACE_4G) {
      // Nada a perder: se ja nao estamos no 4G, Wi-Fi no ar e o enlace.
      trocarEnlace(ENLACE_WIFI);
    } else if (agora - wifiVoltouEm >= WIFI_ESTAVEL_MS) {
      // Estamos no 4G e o Wi-Fi voltou e se firmou. So agora se troca: sem
      // esta espera, uma Starlink oscilando faria o gateway pular de um lado
      // para o outro e perder envios em toda transicao.
      trocarEnlace(ENLACE_WIFI);
      soltarDadosDoModem();
    }
    return;
  }

  if (modemTemDados()) { trocarEnlace(ENLACE_4G); return; }
  trocarEnlace(ENLACE_NENHUM);
}

bool redeConectada() {
  return enlaceAtual == ENLACE_WIFI ? wifiNoAr()
       : enlaceAtual == ENLACE_4G   ? modemTemDados()
       : false;
}

void configurarTLS() {
  // A raiz da Let's Encrypt e conferida no proprio ESP32: sem isto, qualquer
  // um no caminho poderia se passar pelo servidor e capturar o token.
  clienteTLS.setCACert(CA_LETSENCRYPT);
  clienteTLS.setTimeout(8000);
#if TEM_4G
  // ATENCAO, limitacao conhecida: no caminho 4G quem faz o TLS e o SIM7600,
  // nao o ESP32, e a cadeia so seria conferida com o certificado raiz
  // carregado NO MODEM (AT+CCERTDOWN). Este sketch nao faz isso. O trafego
  // vai cifrado, mas sem autenticar o servidor -- um ataque no meio do
  // enlace da operadora poderia capturar o DEVICE_TOKEN.
  //
  // Com a troca automatica isso deixou de ser uma escolha permanente e virou
  // uma janela: o gateway so fica exposto ENQUANTO estiver no 4G, e volta
  // para o Wi-Fi (que confere a cadeia) assim que ele se firma. Esta anotado
  // como pendencia no README.
  Serial.println("[tls] 4G usa TLS do modem, SEM conferencia da cadeia (ver README).");
#endif
}

/** Chamado antes de cada envio: se o boot aconteceu sem hora (sem sinal, por
 *  exemplo), e aqui que ela e finalmente acertada -- senao o equipamento
 *  passaria a vida sem conseguir fechar o TLS. Tambem cobre a troca de
 *  enlace, porque a fonte da hora muda junto. */
void garantirRelogio() {
  time_t agora = 0;
  time(&agora);
  if (agora < 1700000000) sincronizarRelogio();
}

/** Qualidade do sinal do enlace em uso, para subir junto com a telemetria.
 *  -127 significa "nao sei dizer". */
int sinalDoEnlace() {
#if TEM_WIFI
  if (enlaceAtual == ENLACE_WIFI) return WiFi.RSSI();
#endif
#if TEM_4G
  if (enlaceAtual == ENLACE_4G) return modem.getSignalQuality();
#endif
  return -127;
}

// ===========================================================================
// RELOGIO
//
// Conferir um certificado exige saber a data: o TLS recusa um certificado
// "ainda nao valido" se o relogio estiver antes do notBefore dele. E o ESP32
// acorda em 1970.
//
// No 4G quem faz o TLS e o modem -- e ele pega a hora da propria rede da
// operadora. No Wi-Fi, quem faz o TLS e o ESP32: sem acertar o relogio, TODO
// envio falha com um erro generico de handshake, e se perde tempo procurando
// problema de rede que nao existe.
//
// Com os dois enlaces vivos, a fonte da hora segue o enlace EM USO: NTP nao
// atravessa o modem, e o SIM7600 so sabe a hora quando esta registrado.
// ===========================================================================
void sincronizarRelogio() {
  time_t agora = 0;

#if TEM_4G
  if (enlaceAtual == ENLACE_4G) {
    int ano = 0, mes = 0, dia = 0, h = 0, m = 0, seg = 0; float fuso = 0;
    if (modem.getNetworkTime(&ano, &mes, &dia, &h, &m, &seg, &fuso) && ano > 2020) {
      struct tm t = {};
      t.tm_year = ano - 1900; t.tm_mon = mes - 1; t.tm_mday = dia;
      t.tm_hour = h; t.tm_min = m; t.tm_sec = seg;
      time_t utc = mktime(&t) - (time_t)(fuso * 900);  // fuso vem em 1/4 de hora
      struct timeval tv = { .tv_sec = utc, .tv_usec = 0 };
      settimeofday(&tv, NULL);
      agora = utc;
    }
  }
#endif
#if TEM_WIFI
  if (agora < 1700000000 && enlaceAtual == ENLACE_WIFI) {
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
    const unsigned long inicio = millis();
    while (agora < 1700000000 && millis() - inicio < 20000) {
      esperarComWatchdog(500);
      time(&agora);
    }
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
#if TEM_4G
  // So se o modem chegou a subir: mandar AT para um chip que nunca respondeu
  // e esperar por ele seria somar segundos a um reinicio de emergencia.
  if (estadoModem != MODEM_PARADO && estadoModem != MODEM_AUSENTE) {
    modem.gprsDisconnect();
    esperarComWatchdog(1000);
    modem.poweroff();
    esperarComWatchdog(3000);
  }
#endif
  Serial.println("[reboot] reiniciando ESP32...");
  delay(200);
  ESP.restart();
}

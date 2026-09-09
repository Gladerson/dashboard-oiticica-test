/* ===========================================================================
 * HydroConecta - CONTROLADOR: sensor de nivel (Modbus RTU) -> LoRa
 *
 * Le um radar de nivel por RS-485/Modbus e manda a medida BRUTA para o
 * gateway por LoRa, com confirmacao (ACK) e retentativa.
 *
 * DIVISAO DE RESPONSABILIDADE, de proposito
 * -----------------------------------------
 * Este equipamento NAO calcula percentual, volume nem cota. Ele mede
 * distancia e entrega distancia. Toda a regra de negocio mora no gateway
 * (firmware/libraries/HydroConecta/RegrasNivel.h). Assim, recalibrar o
 * reservatorio e mexer em UM lugar -- nao ir a campo reprogramar cada
 * controlador no alto de uma torre.
 *
 * O QUE MUDOU EM RELACAO A VERSAO 2.2
 * -----------------------------------
 *  - pacote ganhou CHECKSUM (ver ProtocoloLoRa.h). Antes, um pacote
 *    corrompido no ar passava como leitura boa: bastava ter dois '|';
 *  - `esp_task_wdt_deinit()` antes do init. Sem isso, num core 3.x onde o
 *    watchdog ja vem inicializado, o `esp_task_wdt_init()` devolvia erro em
 *    silencio e o timeout configurado aqui NAO valia -- ou seja, o
 *    equipamento podia estar sem watchdog nenhum achando que tinha;
 *  - String do Arduino -> buffers fixos. `readStringUntil()` bloqueava ate
 *    1s dentro do loop, e as concatenacoes fragmentavam o heap ao longo dos
 *    meses;
 *  - leitura Modbus com conferencia de tamanho, endereco, funcao e CRC --
 *    antes so o CRC e o endereco eram conferidos.
 *
 * BIBLIOTECA: HydroConecta (firmware/libraries, neste repositorio).
 * Placa: ESP32 Dev Module. ESP32 Arduino Core 3.x.
 * =========================================================================== */

// ===========================================================================
// CONFIGURACAO (EDITE AQUI)
// ===========================================================================

// Tem de ser IDENTICO ao `id` na tabela EQUIPAMENTOS do gateway.
static const char* DEVICE_ID = "nivel-rd01";

static const unsigned long INTERVALO_LEITURA_MS = 15000;
static const unsigned long TIMEOUT_ACK_MS       = 2000;
static const int           MAX_RETENTATIVAS     = 3;

// Sensor radar KD-908S (Modbus RTU)
static const uint8_t  SLAVE_ID        = 0x01;
static const uint32_t MODBUS_BAUD     = 9600;
static const uint16_t MODBUS_REG      = 0x0001;   // registrador da distancia
static const unsigned long MODBUS_TIMEOUT_MS = 300;

// Faixa fisica aceitavel da medida, em milimetros. Fora disso e eco espurio
// (estrutura, espuma, chuva) ou falha do sensor -- e vira status de erro em
// vez de virar nivel. Ajuste para a sua instalacao.
static const uint16_t MEDIDA_MIN_MM = 200;      // abaixo: eco na propria antena
static const uint16_t MEDIDA_MAX_MM = 60000;

// --- Pinagem ---
#define PIN_RS485_RX 16
#define PIN_RS485_TX 17
#define PIN_RS485_DE 21
#define PIN_RS485_RE 19
#define PIN_LORA_RX   4
#define PIN_LORA_TX   5

// Folga sobre o pior caso do loop (leitura Modbus + espera de ACK).
#define WDT_TIMEOUT_S 30

// ===========================================================================

#include <esp_task_wdt.h>
#include <HardwareSerial.h>
#include <ProtocoloLoRa.h>

HardwareSerial rs485Serial(2);
HardwareSerial loraSerial(1);

struct Medida {
  float distancia_m = 0.0f;
  bool  sensor_ok   = false;
};
static Medida medida;

static unsigned long ultimaLeitura   = 0;
static bool          aguardandoAck   = false;
static unsigned long inicioEsperaAck = 0;
static int           tentativas      = 0;
static char          pacote[64];
static char          rxBuf[96];
static size_t        rxLen = 0;
static unsigned long enviosOk = 0, enviosPerdidos = 0;

// ===========================================================================
void setup() {
  Serial.begin(115200);
  delay(200);

  // O deinit ANTES do init e o que garante que a configuracao abaixo vale
  // mesmo. Sem ele, num core que ja inicializou o watchdog, o init falha
  // silenciosamente e fica-se sem protecao nenhuma achando que tem.
  esp_task_wdt_deinit();
  esp_task_wdt_config_t wdt = {
      .timeout_ms     = WDT_TIMEOUT_S * 1000,
      .idle_core_mask = (1 << 0) | (1 << 1),
      .trigger_panic  = true };
  esp_task_wdt_init(&wdt);
  esp_task_wdt_add(NULL);

  pinMode(PIN_RS485_DE, OUTPUT);
  pinMode(PIN_RS485_RE, OUTPUT);
  modoRecepcao485();

  rs485Serial.begin(MODBUS_BAUD, SERIAL_8N1, PIN_RS485_RX, PIN_RS485_TX);
  loraSerial.setRxBufferSize(512);
  loraSerial.begin(9600, SERIAL_8N1, PIN_LORA_RX, PIN_LORA_TX);

  Serial.printf("=== HydroConecta CONTROLADOR %s ===\n", DEVICE_ID);
}

// ===========================================================================
void loop() {
  esp_task_wdt_reset();
  const unsigned long agora = millis();

  lerLoRa();   // nunca bloqueia

  if (aguardandoAck) {
    if (agora - inicioEsperaAck > TIMEOUT_ACK_MS) {
      if (tentativas < MAX_RETENTATIVAS) {
        tentativas++;
        Serial.printf("[lora] sem ACK; retentativa %d/%d\n", tentativas, MAX_RETENTATIVAS);
        loraSerial.println(pacote);
        inicioEsperaAck = agora;
      } else {
        // Desiste DESTA amostra e volta ao ciclo normal. Nunca fica preso:
        // a proxima leitura sai no horario, e o gateway marca este
        // equipamento como "off" se o silencio persistir.
        enviosPerdidos++;
        Serial.printf("[lora] gateway nao respondeu; amostra perdida "
                      "(%lu ok / %lu perdidas)\n", enviosOk, enviosPerdidos);
        aguardandoAck = false;
      }
    }
    return;
  }

  if (agora - ultimaLeitura >= INTERVALO_LEITURA_MS) {
    ultimaLeitura = agora;
    lerModbus();
    enviarPacote();
  }
}

// ===========================================================================
// LORA
// ===========================================================================
void enviarPacote() {
  if (hc_montar_pacote(pacote, sizeof(pacote), DEVICE_ID,
                       medida.distancia_m, medida.sensor_ok) == 0) {
    Serial.println("[lora] pacote nao coube no buffer (DEVICE_ID longo demais?)");
    return;
  }
  while (loraSerial.available()) loraSerial.read();   // limpa eco antigo
  loraSerial.println(pacote);
  Serial.printf("[lora] TX %s\n", pacote);

  aguardandoAck   = true;
  inicioEsperaAck = millis();
  tentativas      = 0;
}

// Leitura caractere a caractere. `readStringUntil()` bloqueava ate 1 segundo
// dentro do loop esperando um '\n' que podia nunca vir.
void lerLoRa() {
  while (loraSerial.available()) {
    char c = (char)loraSerial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      rxBuf[rxLen] = '\0';
      if (rxLen > 0 && aguardandoAck && hc_ack_confere(rxBuf, DEVICE_ID)) {
        enviosOk++;
        aguardandoAck = false;
        Serial.println("[lora] ACK recebido");
      }
      rxLen = 0;
      continue;
    }
    if (rxLen < sizeof(rxBuf) - 1) rxBuf[rxLen++] = c;
    else rxLen = 0;
  }
}

// ===========================================================================
// MODBUS RTU
// ===========================================================================
void lerModbus() {
  while (rs485Serial.available()) rs485Serial.read();

  uint8_t req[8] = { SLAVE_ID, 0x03,
                     (uint8_t)(MODBUS_REG >> 8), (uint8_t)(MODBUS_REG & 0xFF),
                     0x00, 0x01, 0x00, 0x00 };
  uint16_t crc = crc16Modbus(req, 6);
  req[6] = (uint8_t)(crc & 0xFF);
  req[7] = (uint8_t)(crc >> 8);

  modoTransmissao485();
  rs485Serial.write(req, 8);
  rs485Serial.flush();
  modoRecepcao485();

  // Resposta de leitura de 1 registrador: end + func + nbytes + 2 dados + CRC.
  const int ESPERADO = 7;
  const unsigned long inicio = millis();
  while (rs485Serial.available() < ESPERADO && millis() - inicio < MODBUS_TIMEOUT_MS) {
    esp_task_wdt_reset();
    delay(1);
  }

  if (rs485Serial.available() < ESPERADO) {
    falhaLeitura("sem resposta do sensor");
    return;
  }

  uint8_t resp[ESPERADO];
  rs485Serial.readBytes(resp, ESPERADO);

  // Confere TUDO antes de acreditar no numero: endereco, funcao, contagem de
  // bytes e CRC. A versao anterior nao conferia funcao nem contagem, entao
  // uma resposta de excecao do sensor podia ser lida como medida.
  if (resp[0] != SLAVE_ID)                   { falhaLeitura("endereco errado"); return; }
  if (resp[1] != 0x03)                       { falhaLeitura("funcao inesperada"); return; }
  if (resp[2] != 0x02)                       { falhaLeitura("contagem de bytes"); return; }
  uint16_t crcRec = (uint16_t)resp[5] | ((uint16_t)resp[6] << 8);
  if (crcRec != crc16Modbus(resp, 5))        { falhaLeitura("CRC"); return; }

  uint16_t mm = ((uint16_t)resp[3] << 8) | resp[4];
  if (mm < MEDIDA_MIN_MM || mm > MEDIDA_MAX_MM) {
    falhaLeitura("medida fora da faixa fisica");
    return;
  }

  medida.distancia_m = mm / 1000.0f;
  medida.sensor_ok   = true;
  Serial.printf("[modbus] %.3f m\n", medida.distancia_m);
}

// Numa falha, o valor NAO e zerado: zero seria lido como "reservatorio
// transbordando" se algum dia o status fosse ignorado. Mantem a ultima
// medida e marca sensor_ok = false -- o gateway publica status "erro" e nao
// calcula nada.
void falhaLeitura(const char* motivo) {
  medida.sensor_ok = false;
  Serial.printf("[modbus] falha: %s\n", motivo);
}

void modoTransmissao485() {
  digitalWrite(PIN_RS485_DE, HIGH);
  digitalWrite(PIN_RS485_RE, HIGH);
  delayMicroseconds(50);
}

void modoRecepcao485() {
  digitalWrite(PIN_RS485_DE, LOW);
  digitalWrite(PIN_RS485_RE, LOW);
  delayMicroseconds(50);
}

uint16_t crc16Modbus(const uint8_t* dados, uint8_t n) {
  uint16_t crc = 0xFFFF;
  for (uint8_t i = 0; i < n; i++) {
    crc ^= dados[i];
    for (uint8_t j = 0; j < 8; j++)
      crc = (crc & 0x0001) ? (uint16_t)((crc >> 1) ^ 0xA001) : (uint16_t)(crc >> 1);
  }
  return crc;
}

// ===========================================================================
// teste_gateway.cpp - exercita o gateway inteiro num PC.
//
//   g++ -std=c++17 -Wall -Wextra -I firmware/libraries/HydroConecta
//       -I firmware/testes -I firmware/testes/stubs
//       firmware/testes/teste_gateway.cpp -o /tmp/tg && /tmp/tg
//
// O alvo é a troca automática entre Wi-Fi e 4G. É a parte do firmware com
// mais estados, e a que ninguém consegue observar em campo sem subir numa
// torre: o gateway fica a centenas de quilômetros.
//
// O sketch é incluído como código-fonte (`#include`), com os dublês de
// ambiente_arduino.h no lugar do Arduino. `setup()` e `loop()` são chamados
// por este arquivo, não pelo núcleo do ESP32.
// ===========================================================================
#include "ambiente_arduino.h"

// Definições dos globais que o dublê declara como extern.
unsigned long _millis = 0;
int _wdt_resets = 0;
int _restarts = 0;
FalsaSerial Serial;
FalsoWiFi WiFi;
FalsoESP ESP;
int HTTPClient::proximoCodigo = 200;
int HTTPClient::posts = 0;
std::string HTTPClient::ultimoCorpo;
std::string HTTPClient::ultimaUrl;

#include "../gateway_lora/gateway_lora.ino"

// ===========================================================================
static int falhas = 0;

static void cheque(bool cond, const char* msg) {
  printf(cond ? "  ok    %s\n" : "  FALHA  %s\n", msg);
  if (!cond) falhas++;
}

/** Roda o loop do gateway por N milissegundos de tempo simulado. O passo de
 *  200 ms é a ordem de grandeza de uma volta real com LoRa parado. */
static void rodar(unsigned long ms) {
  const unsigned long fim = _millis + ms;
  const int restartsNoInicio = _restarts;
  while (_millis < fim) {
    loop();
    _millis += 200;
    // ESP.restart() de verdade nunca volta. O dublê volta, então é aqui que
    // a simulação para -- senão o gateway "reiniciaria" a cada volta do
    // loop e o teste contaria milhares de reboots.
    if (_restarts > restartsNoInicio) return;
  }
}

static void zerar() {
  _millis = 1000;
  _restarts = 0;
  Serial.limpar();
  WiFi = FalsoWiFi();
  modem = TinyGsm(SerialAT);
  modem.contexto = false;
  enlaceAtual = ENLACE_NENHUM;
  enlaceDesde = 0;
  trocasDeEnlace = 0;
  wifiCaiuEm = wifiVoltouEm = ultimaTentativaWifi = 0;
  estadoModem = MODEM_PARADO;
  modemPassoEm = 0;
  modemTentativas = 0;
  inicioOffline = 0;
  falhasSeguidas = 0;
  enviosOk = enviosFalha = 0;
  ultimoEnvio = 0;
  rebootCount = 0;
  HTTPClient::posts = 0;
  HTTPClient::proximoCodigo = 200;
  HTTPClient::ultimoCorpo.clear();
  for (int i = 0; i < NUM_EQUIPAMENTOS; i++) {
    estados[i] = EstadoEquipamento{};
  }
  setup();
}

int main() {
  printf("== Enlace: Wi-Fi disponível desde o começo ==\n");
  zerar();
  cheque(enlaceAtual == ENLACE_WIFI, "com Wi-Fi no ar, o enlace é wifi");
  rodar(60000);
  cheque(enlaceAtual == ENLACE_WIFI, "continua no wifi enquanto ele não cai");
  cheque(!modem.contexto,
         "o 4G NÃO sobe contexto de dados enquanto o Wi-Fi funciona (não gasta franquia)");

  printf("\n== Wi-Fi cai: o 4G assume sozinho ==\n");
  WiFi.ap_disponivel = false;
  WiFi.cair();
  rodar(10000);
  cheque(enlaceAtual != ENLACE_4G,
         "queda curta (10 s) NÃO troca de enlace -- é só oscilação");
  rodar(30000);
  cheque(enlaceAtual == ENLACE_4G,
         "passada a tolerância, o gateway cai para o 4G sozinho");
  cheque(modem.contexto, "o contexto de dados do modem subiu");
  cheque(redeConectada(), "e o gateway se considera conectado");

  printf("\n== Wi-Fi volta: o gateway retorna a ele ==\n");
  WiFi.ap_disponivel = true;
  rodar(25000);
  cheque(enlaceAtual == ENLACE_4G,
         "não volta no primeiro instante: espera o Wi-Fi se firmar");
  rodar(60000);
  cheque(enlaceAtual == ENLACE_WIFI, "com o Wi-Fi firme, volta para ele");
  cheque(!modem.contexto, "e solta o contexto de dados do 4G");
  cheque(modem.isNetworkConnected(),
         "mas o modem continua REGISTRADO, para a próxima queda ser rápida");

  printf("\n== Oscilação não faz o enlace ficar pingando ==\n");
  zerar();
  const unsigned long trocasAntes = trocasDeEnlace;
  bool usou4g = false;
  for (int i = 0; i < 6; i++) {          // seis quedas de 8 s cada
    WiFi.ap_disponivel = false; WiFi.cair();
    rodar(8000);
    if (enlaceAtual == ENLACE_4G) usou4g = true;
    WiFi.ap_disponivel = true;
    rodar(8000);
    if (enlaceAtual == ENLACE_4G) usou4g = true;
  }
  cheque(!usou4g, "seis oscilações curtas de Wi-Fi: o 4G nunca é acionado");
  cheque(trocasDeEnlace == trocasAntes,
         "e nenhuma troca wifi<->4g é contabilizada");
  cheque(enlaceAtual == ENLACE_WIFI, "o enlace segue sendo o wifi");
  cheque(!modem.contexto, "o modem não chegou a subir contexto de dados");

  printf("\n== Gateway sem modem instalado ==\n");
  zerar();
  modem.responde = false;                 // não existe chip nesta placa
  WiFi.ap_disponivel = false; WiFi.cair();
  rodar(120000);
  cheque(estadoModem == MODEM_AUSENTE,
         "depois de algumas tentativas, o gateway aceita que não há modem");
  cheque(Serial.disse("sem modem nesta placa"), "e diz isso no log, uma vez");
  cheque(enlaceAtual == ENLACE_NENHUM, "sem Wi-Fi e sem modem, fica sem enlace");
  cheque(_restarts == 0, "e NÃO reinicia por isso (ainda não deu 30 min)");
  WiFi.ap_disponivel = true;
  rodar(30000);
  cheque(enlaceAtual == ENLACE_WIFI,
         "quando o Wi-Fi volta, o gateway volta a falar sem precisar de modem");

  printf("\n== Trinta minutos sem nenhum dos dois: reboot seguro ==\n");
  zerar();
  modem.responde = false;
  WiFi.ap_disponivel = false; WiFi.cair();
  rodar(1900000);                         // ~31 min
  cheque(_restarts == 1, "reinicia UMA vez depois de 30 min sem rede");
  cheque(rebootCount == 1, "e conta o reboot na RTC RAM");

  printf("\n== Envio: o cliente TLS segue o enlace em uso ==\n");
  zerar();
  estados[0].online = true;
  estados[0].sensor_ok = true;
  estados[0].valor_bruto = 12.5f;
  estados[0].visto_em = _millis;
  HTTPClient::posts = 0;
  rodar(20000);
  cheque(HTTPClient::posts >= 1, "houve POST no Wi-Fi");
  cheque(HTTPClient::ultimaUrl.find("https://") == 0,
         "no Wi-Fi a URL completa é usada (TLS do ESP32)");
  const std::string corpoWifi = HTTPClient::ultimoCorpo;
  cheque(corpoWifi.find("\"enlace\":\"wifi\"") != std::string::npos,
         "o payload informa o enlace em uso");

  WiFi.ap_disponivel = false; WiFi.cair();
  rodar(60000);
  cheque(enlaceAtual == ENLACE_4G, "caiu para o 4G");
  estados[0].visto_em = _millis;          // mantém o sensor "on"
  HTTPClient::ultimaUrl.clear();
  rodar(20000);
  cheque(HTTPClient::ultimaUrl == "/api/edge/dados",
         "no 4G o begin() usa host/porta/rota separados (TLS do modem)");
  cheque(HTTPClient::ultimoCorpo.find("\"enlace\":\"4g\"") != std::string::npos,
         "e o payload passa a informar 4g");

  printf("\n== Payload: só a medida bruta, sem cota nem volume ==\n");
  cheque(corpoWifi.find("\"distancia\":12.500") != std::string::npos,
         "a distância bruta sobe com três casas");
  cheque(corpoWifi.find("cota_atual") == std::string::npos,
         "o gateway NÃO calcula cota (isso agora é do servidor)");
  cheque(corpoWifi.find("volume_m3") == std::string::npos,
         "nem volume");
  cheque(corpoWifi.find("uso_percentual") == std::string::npos,
         "nem percentual");
  cheque(corpoWifi.find("\"dispositivos\":[\"nivel-rd01\"]") != std::string::npos,
         "a lista explícita de equipamentos continua sendo enviada");
  cheque(corpoWifi.find("\"status\":\"on\"") != std::string::npos,
         "o status do equipamento continua indo junto");
  cheque(corpoWifi.find("uptime_s") != std::string::npos &&
         corpoWifi.find("envios_ok") != std::string::npos,
         "o gateway reporta a própria saúde");

  printf("\n== Nomes das telemetrias do gateway não confundem o servidor ==\n");
  // A separação do payload no servidor usa "chave é prefixo de outra" como
  // pista de que a chave é um equipamento. Duas chaves como "enlace" e
  // "enlace_ha_s" fariam o servidor inventar um equipamento chamado
  // "enlace" -- por isso os nomes foram escolhidos sem essa relação.
  const char* chavesGateway[] = {
    "enlace", "segundos_no_enlace", "trocas_de_enlace", "sinal",
    "uptime_s", "envios_ok", "envios_falha",
  };
  const int nChaves = (int)(sizeof(chavesGateway) / sizeof(chavesGateway[0]));
  bool prefixo = false;
  for (int i = 0; i < nChaves; i++) {
    for (int k = 0; k < nChaves; k++) {
      if (i == k) continue;
      std::string com = std::string(chavesGateway[i]) + "_";
      if (strncmp(chavesGateway[k], com.c_str(), com.size()) == 0) prefixo = true;
    }
  }
  cheque(!prefixo, "nenhuma chave do gateway é prefixo de outra");

  printf("\n== Falhas seguidas derrubam o enlace para reconstruir ==\n");
  zerar();
  HTTPClient::proximoCodigo = 500;
  estados[0].online = true; estados[0].sensor_ok = true;
  estados[0].valor_bruto = 9.0f; estados[0].visto_em = _millis;
  const int beginsAntes = WiFi.begins;
  rodar(80000);                            // várias tentativas de envio
  cheque(enviosFalha > 0, "as falhas são contadas");
  cheque(WiFi.begins > beginsAntes,
         "depois de N falhas seguidas o Wi-Fi é refeito");
  cheque(_restarts == 0, "mas não se reinicia por falha de POST");

  printf("\n== 401 é diagnosticado como token, não como rede ==\n");
  zerar();
  HTTPClient::proximoCodigo = 401;
  estados[0].online = true; estados[0].sensor_ok = true;
  estados[0].valor_bruto = 9.0f; estados[0].visto_em = _millis;
  const int disc = WiFi.disconnects;
  rodar(80000);
  cheque(Serial.disse("token do dispositivo invalido"),
         "o log aponta o token, não o sinal");
  cheque(WiFi.disconnects == disc,
         "e o enlace NÃO é derrubado (reconectar não conserta token errado)");

  printf("\n== LoRa: pacote válido vira estado e ACK ==\n");
  zerar();
  loraSerial.entrada = "nivel-rd01|12.500|1*6B\n";
  loraSerial.pos = 0;
  loraSerial.saida.clear();
  loop();
  cheque(estados[0].online && estados[0].sensor_ok,
         "o equipamento fica online com o pacote bom");
  cheque(estados[0].valor_bruto > 12.49f && estados[0].valor_bruto < 12.51f,
         "a medida bruta é guardada como veio");
  cheque(loraSerial.saida.size() == 1 && loraSerial.saida[0] == "ACK:nivel-rd01",
         "e o ACK volta para o controlador");

  loraSerial.entrada = "nivel-rd01|99.000|1*00\n";   // checksum errado
  loraSerial.pos = 0;
  const float antes = estados[0].valor_bruto;
  loop();
  cheque(estados[0].valor_bruto == antes,
         "pacote com checksum inválido não altera a medida");

  printf("\n== Sensor mudo vira \"off\" ==\n");
  zerar();
  estados[0].online = true; estados[0].sensor_ok = true;
  estados[0].visto_em = _millis;
  rodar(90000);
  cheque(!estados[0].online, "passado o timeout, o equipamento é marcado off");
  cheque(HTTPClient::ultimoCorpo.find("\"status\":\"off\"") != std::string::npos,
         "e o payload diz off, em vez de repetir a última medida");
  cheque(HTTPClient::ultimoCorpo.find("distancia") == std::string::npos,
         "sem medida válida, nenhuma distância é publicada");

  printf("\n== O watchdog é alimentado em todo caminho ==\n");
  const int antesWdt = _wdt_resets;
  rodar(5000);
  cheque(_wdt_resets > antesWdt, "o loop alimenta o watchdog");

  printf(falhas ? "\n%d FALHA(S)\n" : "\nTUDO OK\n", falhas);
  return falhas ? 1 : 0;
}

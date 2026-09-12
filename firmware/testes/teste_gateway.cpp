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
  clienteTLS.limpar();
  clienteTLS.resposta = "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}";
  clienteGsmTLS.limpar();
  clienteGsmTLS.resposta = clienteTLS.resposta;
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
  rodar(20000);
  cheque(clienteTLS.conexoes >= 1, "houve POST pelo cliente do Wi-Fi");
  cheque(clienteGsmTLS.conexoes == 0, "e nenhum pelo cliente do 4G");
  cheque(clienteTLS.ultimoHost == std::string(SERVIDOR_HOST) &&
         clienteTLS.ultimaPorta == SERVIDOR_PORTA,
         "conectou no host e na porta certos");
  const std::string reqWifi = clienteTLS.enviado;
  const size_t corpoEm = reqWifi.find("\r\n\r\n");
  const std::string corpoWifi =
      corpoEm == std::string::npos ? "" : reqWifi.substr(corpoEm + 4);
  cheque(corpoWifi.find("\"enlace\":\"wifi\"") != std::string::npos,
         "o payload informa o enlace em uso");

  printf("\n== O POST escrito a mao esta correto ==\n");
  cheque(reqWifi.find("POST /api/edge/dados HTTP/1.1\r\n") == 0,
         "linha de requisicao");
  cheque(reqWifi.find("\r\nHost: hydroconecta.com.br\r\n") != std::string::npos,
         "cabecalho Host (o TLS e o virtual host dependem dele)");
  cheque(reqWifi.find("\r\nAuthorization: Bearer ") != std::string::npos,
         "o token vai no Authorization");
  cheque(reqWifi.find("\r\nContent-Type: application/json\r\n") != std::string::npos,
         "Content-Type");
  cheque(reqWifi.find("\r\nConnection: close\r\n") != std::string::npos,
         "Connection: close -- nenhum socket fica pendurado entre ciclos");
  char esperado[64];
  snprintf(esperado, sizeof(esperado), "\r\nContent-Length: %u\r\n",
           (unsigned)corpoWifi.size());
  cheque(reqWifi.find(esperado) != std::string::npos,
         "Content-Length bate EXATAMENTE com o corpo enviado");
  cheque(clienteTLS.stops >= 1, "a conexao e fechada ao fim de cada envio");
  cheque(clienteTLS.ca.find("BEGIN CERTIFICATE") != std::string::npos,
         "a CA da Let's Encrypt foi configurada no cliente do Wi-Fi");
  cheque(clienteGsmTLS.ca == clienteTLS.ca,
         "e a MESMA CA no cliente do 4G (a cadeia e conferida nos dois)");
  cheque(clienteGsmTLS.handshake_s > 0 && clienteGsmTLS.handshake_s < 180,
         "o handshake do 4G tem prazo menor que o watchdog");
  cheque(clienteTLS.handshake_s > 0 && clienteTLS.handshake_s < 180,
         "o do Wi-Fi tambem (o padrao do core seria 120 s)");

  printf("\n== Depois de cair para o 4G, quem envia e o outro cliente ==\n");
  WiFi.ap_disponivel = false; WiFi.cair();
  rodar(60000);
  cheque(enlaceAtual == ENLACE_4G, "caiu para o 4G");
  estados[0].visto_em = _millis;          // mantem o sensor "on"
  clienteGsmTLS.limpar();
  rodar(20000);
  cheque(clienteGsmTLS.conexoes >= 1, "agora o POST sai pelo cliente do 4G");
  cheque(clienteGsmTLS.enviado.find("\"enlace\":\"4g\"") != std::string::npos,
         "e o payload passa a informar 4g");
  cheque(clienteGsmTLS.base == &clienteGsm,
         "o TLS do 4G roda por cima do socket da TinyGSM");

  printf("\n== Respostas que nao sao 200 ==\n");
  zerar();
  estados[0].online = true; estados[0].sensor_ok = true;
  estados[0].valor_bruto = 9.0f; estados[0].visto_em = _millis;

  clienteTLS.resposta = "HTTP/1.1 401 Unauthorized\r\n\r\n";
  const int discAntes = WiFi.disconnects;
  rodar(40000);
  cheque(Serial.disse("token do dispositivo invalido"),
         "401 aponta o token, nao o sinal");
  cheque(WiFi.disconnects == discAntes,
         "e o enlace NAO e derrubado (reconectar nao conserta token errado)");

  Serial.limpar();
  clienteTLS.resposta = "nao sou HTTP\r\n";
  clienteTLS.lido = 0;
  rodar(20000);
  cheque(Serial.disse("nao parece HTTP"),
         "resposta que nao e HTTP e diagnosticada como tal");

  Serial.limpar();
  clienteTLS.resposta = "";      // conecta, escreve, e o servidor cala
  clienteTLS.lido = 0;
  rodar(20000);
  cheque(Serial.disse("nao respondeu a tempo"),
         "servidor mudo vira 'nao respondeu a tempo', nao um numero inventado");

  Serial.limpar();
  clienteTLS.conecta = false;    // nem abre
  rodar(20000);
  cheque(Serial.disse("nao consegui abrir a conexao"),
         "falha de conexao e dita com todas as letras");
  cheque(_restarts == 0, "e nada disso reinicia o gateway");
  clienteTLS.conecta = true;
  clienteTLS.resposta = "HTTP/1.1 200 OK\r\n\r\n";

  printf("\n== A conexao cai no meio do envio ==\n");
  zerar();
  estados[0].online = true; estados[0].sensor_ok = true;
  estados[0].valor_bruto = 9.0f; estados[0].visto_em = _millis;
  clienteTLS.escritaFalha = true;
  rodar(20000);
  cheque(Serial.disse("caiu no meio do envio"), "a queda no meio do POST e dita");
  cheque(clienteTLS.stops >= 1, "e a conexao e fechada mesmo assim");
  clienteTLS.escritaFalha = false;

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
  clienteTLS.resposta = "HTTP/1.1 500 Internal Server Error\r\n\r\n";
  estados[0].online = true; estados[0].sensor_ok = true;
  estados[0].valor_bruto = 9.0f; estados[0].visto_em = _millis;
  const int beginsAntes = WiFi.begins;
  rodar(80000);                            // varias tentativas de envio
  cheque(enviosFalha > 0, "as falhas sao contadas");
  cheque(WiFi.begins > beginsAntes,
         "depois de N falhas seguidas o Wi-Fi e refeito");
  cheque(_restarts == 0, "mas nao se reinicia por falha de POST");

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
  cheque(clienteTLS.enviado.find("\"status\":\"off\"") != std::string::npos,
         "e o payload diz off, em vez de repetir a última medida");
  cheque(clienteTLS.enviado.find("distancia") == std::string::npos,
         "sem medida válida, nenhuma distância é publicada");

  printf("\n== O watchdog é alimentado em todo caminho ==\n");
  const int antesWdt = _wdt_resets;
  rodar(5000);
  cheque(_wdt_resets > antesWdt, "o loop alimenta o watchdog");

  printf(falhas ? "\n%d FALHA(S)\n" : "\nTUDO OK\n", falhas);
  return falhas ? 1 : 0;
}

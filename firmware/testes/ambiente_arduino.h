// ===========================================================================
// ambiente_arduino.h - dublê do Arduino, do ESP32 e da TinyGSM, para o PC.
//
// POR QUE ISTO EXISTE
// -------------------
// O gateway fica a centenas de quilômetros de quem pode mexer nele. A parte
// mais arriscada dele é a troca automática entre Wi-Fi e 4G: são dois
// enlaces, histerese nos dois sentidos e uma máquina de estados de modem --
// exatamente o tipo de código que parece certo lendo e erra em campo.
//
// Com estes dublês, `gateway_lora.ino` compila num g++ comum e a suíte em
// teste_gateway.cpp exercita a lógica de enlace de verdade: o tempo é
// controlado (`_millis`), o Wi-Fi cai e volta quando o teste manda, e o modem
// responde ou fica mudo por decisão do teste.
//
// O QUE ISTO **NÃO** TESTA
// ------------------------
// Nada de hardware: rádio, TLS, HTTP, UART. Os dublês registram que foram
// chamados e devolvem o que o teste mandar. Continua valendo a regra do
// README: firmware só é firmware depois de gravado e observado em campo.
// ===========================================================================
#ifndef HC_AMBIENTE_ARDUINO_H
#define HC_AMBIENTE_ARDUINO_H

#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

// --- tempo controlado pelo teste -------------------------------------------
extern unsigned long _millis;
inline unsigned long millis() { return _millis; }
inline void delay(unsigned long ms) { _millis += ms; }

// --- Serial ----------------------------------------------------------------
struct FalsaSerial {
  bool silencioso = true;          // o teste não quer o log inteiro na tela
  std::vector<std::string> linhas;

  void begin(unsigned long, ...) {}
  void print(const char* s) { registrar(s); }
  void print(int v) { char b[32]; snprintf(b, sizeof(b), "%d", v); registrar(b); }
  void println() { registrar("\n"); }
  void println(const char* s) { registrar(s); registrar("\n"); }
  void println(bool v) { println(v ? "true" : "false"); }
  void printf(const char* fmt, ...) {
    char b[512];
    va_list ap; va_start(ap, fmt);
    vsnprintf(b, sizeof(b), fmt, ap);
    va_end(ap);
    registrar(b);
  }
  void registrar(const char* s) {
    linhas.push_back(s);
    if (!silencioso) fputs(s, stdout);
  }
  bool disse(const char* trecho) const {
    for (const auto& l : linhas) if (l.find(trecho) != std::string::npos) return true;
    return false;
  }
  void limpar() { linhas.clear(); }
};
extern FalsaSerial Serial;

// --- HardwareSerial (LoRa e modem) -----------------------------------------
#define SERIAL_8N1 0
struct HardwareSerial {
  explicit HardwareSerial(int) {}
  std::string entrada;             // o teste injeta pacotes LoRa aqui
  std::vector<std::string> saida;  // ACKs enviados
  size_t pos = 0;
  void setRxBufferSize(size_t) {}
  void begin(unsigned long, int = 0, int = -1, int = -1) {}
  int  available() { return (int)(entrada.size() - pos); }
  int  read() { return pos < entrada.size() ? entrada[pos++] : -1; }
  void println(const char* s) { saida.push_back(s); }
};

// --- watchdog --------------------------------------------------------------
struct esp_task_wdt_config_t {
  uint32_t timeout_ms;
  uint32_t idle_core_mask;
  bool     trigger_panic;
};
extern int _wdt_resets;
inline void esp_task_wdt_deinit() {}
inline void esp_task_wdt_init(esp_task_wdt_config_t*) {}
inline void esp_task_wdt_add(void*) {}
inline void esp_task_wdt_reset() { _wdt_resets++; }

// --- ESP -------------------------------------------------------------------
extern int _restarts;
struct FalsoESP { void restart() { _restarts++; } };
extern FalsoESP ESP;
#define RTC_DATA_ATTR
#define PROGMEM

// --- Wi-Fi -----------------------------------------------------------------
#define WIFI_STA 1
enum { WL_CONNECTED = 3, WL_DISCONNECTED = 6 };
struct FalsoWiFi {
  int  estado = WL_DISCONNECTED;
  int  rssi = -58;
  int  begins = 0, disconnects = 0;
  // Quando true, um begin() conecta na hora. O teste desliga isto para
  // simular a Starlink fora do ar.
  bool ap_disponivel = true;
  bool auto_reconecta = false;
  unsigned long caiu_em = 0;
  // Quanto o núcleo do ESP32 leva para reencontrar um AP que voltou. Uns
  // segundos, na prática -- e é por isso que o gateway não precisa chamar
  // begin() a toda hora.
  unsigned long atraso_reconexao_ms = 3000;

  void mode(int) {}
  void setSleep(bool) {}
  void setAutoReconnect(bool v) { auto_reconecta = v; }
  void begin(const char*, const char*) {
    begins++;
    if (ap_disponivel) estado = WL_CONNECTED; else cair();
  }
  void disconnect() { disconnects++; cair(); }
  void cair() { estado = WL_DISCONNECTED; caiu_em = millis(); }
  int  status() {
    // O AP voltou e o núcleo reconecta sozinho -- é o comportamento real do
    // WiFi.setAutoReconnect(true), e sem ele o dublê faria o gateway parecer
    // muito mais lento para recuperar o Wi-Fi do que é.
    if (estado != WL_CONNECTED && ap_disponivel && auto_reconecta &&
        caiu_em && millis() - caiu_em >= atraso_reconexao_ms) {
      estado = WL_CONNECTED;
    }
    return estado;
  }
  int  RSSI() const { return rssi; }
};
extern FalsoWiFi WiFi;

// --- Client: a interface do Arduino que os dois enlaces implementam --------
// O gateway escreve o POST à mão sobre um `Client&`, então é AQUI que o teste
// observa o que realmente sai na rede -- byte a byte, o que com o HTTPClient
// no meio não era possível.
struct Client {
  virtual ~Client() {}
  virtual int    connect(const char* host, uint16_t port) = 0;
  virtual size_t write(const uint8_t* buf, size_t n) = 0;
  virtual int    available() = 0;
  virtual int    read() = 0;
  virtual void   stop() = 0;
  virtual uint8_t connected() = 0;
};

/** Cliente de mentira, um por enlace. O teste diz se a conexão abre, o que o
 *  servidor responde, e depois lê tudo o que foi escrito. */
struct FalsoClienteTLS : public Client {
  // Controlado pelo teste:
  bool        conecta = true;        // connect() dá certo?
  bool        escritaFalha = false;  // a conexão cai no meio do envio?
  std::string resposta = "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}";
  std::string ca;                    // qual CA foi configurada
  unsigned long handshake_s = 0;

  // Observado pelo teste:
  int         conexoes = 0, stops = 0;
  std::string enviado;               // cabeçalho + corpo, exatamente como foram
  std::string ultimoHost;
  uint16_t    ultimaPorta = 0;

  size_t lido = 0;
  bool   aberto = false;

  void setCACert(const char* c) { ca = c ? c : ""; }
  void setTimeout(unsigned long) {}
  void setHandshakeTimeout(unsigned long s) { handshake_s = s; }

  int connect(const char* host, uint16_t port) override {
    conexoes++;
    ultimoHost = host ? host : "";
    ultimaPorta = port;
    enviado.clear();
    lido = 0;
    aberto = conecta;
    return conecta ? 1 : 0;
  }
  size_t write(const uint8_t* buf, size_t n) override {
    if (!aberto) return 0;
    if (escritaFalha) { aberto = false; return 0; }
    enviado.append((const char*)buf, n);
    return n;
  }
  int available() override {
    return aberto ? (int)(resposta.size() - lido) : 0;
  }
  int read() override {
    if (!aberto || lido >= resposta.size()) return -1;
    return (unsigned char)resposta[lido++];
  }
  void stop() override { stops++; aberto = false; }
  uint8_t connected() override {
    return (aberto && lido < resposta.size()) ? 1 : 0;
  }
  void limpar() {
    conexoes = stops = 0;
    enviado.clear();
    lido = 0;
    aberto = false;
    escritaFalha = false;
    conecta = true;
  }
};

using WiFiClientSecure = FalsoClienteTLS;

// --- SSLClient (govorox): TLS por cima de um Client qualquer ---------------
struct SSLClient : public FalsoClienteTLS {
  Client* base = nullptr;
  explicit SSLClient(Client* c) : base(c) {}
};

// --- TinyGSM ---------------------------------------------------------------
// TinyGsmClient: o socket TCP puro do modem, sobre o qual o SSLClient roda.
// O teste não o exercita direto -- quem escreve é o SSLClient --, mas ele
// precisa existir para o sketch compilar igual ao de verdade.
struct TinyGsm;
struct TinyGsmClient : public Client {
  TinyGsmClient() {}
  template <typename M> explicit TinyGsmClient(M&) {}
  int    connect(const char*, uint16_t) override { return 1; }
  size_t write(const uint8_t*, size_t n) override { return n; }
  int    available() override { return 0; }
  int    read() override { return -1; }
  void   stop() override {}
  uint8_t connected() override { return 1; }
};

struct TinyGsm {
  explicit TinyGsm(HardwareSerial&) {}
  // Tudo controlado pelo teste.
  bool responde = true;       // o chip existe e fala AT?
  bool registrado = true;     // achou torre?
  bool contexto = false;      // APN de pé?
  bool gprs_possivel = true;  // gprsConnect() vai dar certo?
  int  csq = 18;
  int  inits = 0, restarts = 0, conecta = 0, desconecta = 0, poweroffs = 0;

  bool init() { inits++; return responde; }
  void restart() { restarts++; }
  bool waitForNetwork(long) { return responde && registrado; }
  bool isNetworkConnected() { return responde && registrado; }
  bool isGprsConnected() { return contexto; }
  bool gprsConnect(const char*, const char*, const char*) {
    conecta++;
    contexto = responde && registrado && gprs_possivel;
    return contexto;
  }
  void gprsDisconnect() { desconecta++; contexto = false; }
  void poweroff() { poweroffs++; responde = false; }
  int  getSignalQuality() { return csq; }
  bool getNetworkTime(int* ano, int* mes, int* dia, int* h, int* m, int* s, float* tz) {
    *ano = 2026; *mes = 9; *dia = 12; *h = 12; *m = 0; *s = 0; *tz = -12;
    return responde && registrado;
  }
};
// NOTA: a TinyGSM de verdade NÃO define TinyGsmClientSecure para o SIM7600 --
// só o TCP puro acima. Foi esse o erro de compilação que levou o gateway a
// passar a usar SSLClient. O dublê reflete isso de propósito: se alguém
// reintroduzir TinyGsmClientSecure no sketch, aqui também não compila.

// --- relógio ---------------------------------------------------------------
// time.h de verdade é incluído pelo sketch; só faltam estes dois.
inline void configTime(long, int, const char*, const char* = nullptr) {}

#endif  // HC_AMBIENTE_ARDUINO_H

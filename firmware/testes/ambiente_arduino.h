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

// --- TLS / HTTP ------------------------------------------------------------
struct WiFiClientSecure {
  void setCACert(const char*) {}
  void setTimeout(unsigned long) {}
};
struct HTTPClient {
  static int  proximoCodigo;       // o teste decide o que o servidor responde
  static int  posts;
  static std::string ultimoCorpo;
  static std::string ultimaUrl;

  void setConnectTimeout(unsigned long) {}
  void setTimeout(unsigned long) {}
  void setReuse(bool) {}
  bool begin(WiFiClientSecure&, const char* url) { ultimaUrl = url; return true; }
  template <typename C>
  bool begin(C&, const char*, int, const char* rota, bool) { ultimaUrl = rota; return true; }
  void addHeader(const char*, const char*) {}
  int  POST(uint8_t* corpo, size_t n) {
    posts++;
    ultimoCorpo.assign((const char*)corpo, n);
    return proximoCodigo;
  }
  void end() {}
  std::string errorToString(int) { return "erro"; }
};
// O sketch chama http.errorToString(codigo).c_str(): std::string já tem.

// --- TinyGSM ---------------------------------------------------------------
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
template <typename M>
struct TinyGsmClientSecureT { explicit TinyGsmClientSecureT(M&) {} };
#define TinyGsmClientSecure TinyGsmClientSecureT<TinyGsm>

// --- relógio ---------------------------------------------------------------
// time.h de verdade é incluído pelo sketch; só faltam estes dois.
inline void configTime(long, int, const char*, const char* = nullptr) {}

#endif  // HC_AMBIENTE_ARDUINO_H

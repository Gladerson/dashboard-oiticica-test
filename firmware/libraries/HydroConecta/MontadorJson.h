// ===========================================================================
// MontadorJson.h - escreve JSON num buffer FIXO, sem alocar nada.
//
// Por que nao ArduinoJson: nao e por implicancia com a biblioteca, e por
// tempo de vida. Um gateway que roda anos sem reiniciar nao pode alocar e
// liberar um documento a cada 15 segundos -- o heap do ESP32 fragmenta, e o
// sintoma aparece meses depois como "parou de enviar do nada", sem nenhuma
// pista no log. Aqui o buffer e estatico, do tamanho maximo do payload, e o
// custo de memoria e conhecido em tempo de compilacao.
//
// O escopo e minusculo de proposito: objetos, numeros, textos e booleanos.
// E o que o /api/edge/dados do servidor consome, nada mais.
//
// SEGURANCA DO BUFFER: toda escrita confere o espaco. Se faltar, marca
// `ok = false` e para de escrever. Quem chama DEVE conferir hc_json_ok()
// antes de enviar -- meio JSON e pior que nenhum, porque o servidor
// responderia 400 e o operador procuraria o defeito no lugar errado.
//
// Livre de Arduino: compila num PC, e a suite em firmware/testes/ confere o
// resultado byte a byte.
// ===========================================================================
#ifndef HYDROCONECTA_MONTADOR_JSON_H
#define HYDROCONECTA_MONTADOR_JSON_H

#include <math.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

struct HcJson {
  char*  buf;
  size_t cap;
  size_t len;
  bool   ok;
  bool   primeiro;   // controla a virgula entre membros
};

inline void hc_json_iniciar(HcJson* j, char* buf, size_t cap) {
  j->buf = buf; j->cap = cap; j->len = 0; j->ok = (buf != 0 && cap > 1);
  j->primeiro = true;
  if (j->ok) buf[0] = '\0';
}

inline bool hc_json_ok(const HcJson* j) { return j->ok; }
inline const char* hc_json_texto_final(const HcJson* j) { return j->ok ? j->buf : ""; }
inline size_t hc_json_tamanho(const HcJson* j) { return j->ok ? j->len : 0; }

inline void hc_json_bruto(HcJson* j, const char* s) {
  if (!j->ok) return;
  size_t n = strlen(s);
  if (j->len + n + 1 > j->cap) { j->ok = false; return; }
  memcpy(j->buf + j->len, s, n);
  j->len += n;
  j->buf[j->len] = '\0';
}

// Chaves e textos do projeto sao identificadores e status ("on"/"off"/
// "erro"): sem aspas, barras ou acentos. Ainda assim, o que NAO for
// imprimivel ASCII simples e recusado -- em vez de escapar, que abriria a
// porta para JSON invalido silencioso.
inline bool hc_json_texto_seguro(const char* s) {
  if (s == 0) return false;
  for (const char* p = s; *p; p++) {
    unsigned char c = (unsigned char)*p;
    if (c < 0x20 || c > 0x7E || c == '"' || c == '\\') return false;
  }
  return true;
}

inline void hc_json_virgula(HcJson* j) {
  if (!j->primeiro) hc_json_bruto(j, ",");
  j->primeiro = false;
}

inline void hc_json_chave(HcJson* j, const char* chave) {
  if (!j->ok) return;
  if (!hc_json_texto_seguro(chave)) { j->ok = false; return; }
  hc_json_virgula(j);
  hc_json_bruto(j, "\"");
  hc_json_bruto(j, chave);
  hc_json_bruto(j, "\":");
}

inline void hc_json_abrir_raiz(HcJson* j) { hc_json_bruto(j, "{"); j->primeiro = true; }

inline void hc_json_abrir_objeto(HcJson* j, const char* chave) {
  hc_json_chave(j, chave);
  hc_json_bruto(j, "{");
  j->primeiro = true;
}

inline void hc_json_fechar_objeto(HcJson* j) {
  hc_json_bruto(j, "}");
  j->primeiro = false;   // o proximo membro do nivel de cima precisa de virgula
}

inline void hc_json_abrir_lista(HcJson* j, const char* chave) {
  hc_json_chave(j, chave);
  hc_json_bruto(j, "[");
  j->primeiro = true;
}

inline void hc_json_item_texto(HcJson* j, const char* valor) {
  if (!j->ok) return;
  if (!hc_json_texto_seguro(valor)) { j->ok = false; return; }
  hc_json_virgula(j);
  hc_json_bruto(j, "\"");
  hc_json_bruto(j, valor);
  hc_json_bruto(j, "\"");
}

inline void hc_json_fechar_lista(HcJson* j) {
  hc_json_bruto(j, "]");
  j->primeiro = false;
}

inline void hc_json_texto(HcJson* j, const char* chave, const char* valor) {
  hc_json_chave(j, chave);
  if (!j->ok) return;
  if (!hc_json_texto_seguro(valor)) { j->ok = false; return; }
  hc_json_bruto(j, "\"");
  hc_json_bruto(j, valor);
  hc_json_bruto(j, "\"");
}

inline void hc_json_bool(HcJson* j, const char* chave, bool valor) {
  hc_json_chave(j, chave);
  hc_json_bruto(j, valor ? "true" : "false");
}

// NaN e infinito NAO sao JSON. Se um calculo estourar, vira `null`: o
// servidor grava o vazio, o painel mostra "--", e ninguem le um numero
// inventado como se fosse medida. Escrever "nan" quebraria o corpo inteiro,
// derrubando TODAS as telemetrias daquele envio por causa de uma so.
inline void hc_json_num(HcJson* j, const char* chave, double valor, int casas) {
  hc_json_chave(j, chave);
  if (!j->ok) return;
  if (!isfinite(valor)) { hc_json_bruto(j, "null"); return; }
  char tmp[40];
  int n = snprintf(tmp, sizeof(tmp), "%.*f", casas, valor);
  if (n < 0 || (size_t)n >= sizeof(tmp)) { j->ok = false; return; }
  hc_json_bruto(j, tmp);
}

inline void hc_json_inteiro(HcJson* j, const char* chave, long long valor) {
  hc_json_chave(j, chave);
  if (!j->ok) return;
  char tmp[24];
  int n = snprintf(tmp, sizeof(tmp), "%lld", valor);
  if (n < 0 || (size_t)n >= sizeof(tmp)) { j->ok = false; return; }
  hc_json_bruto(j, tmp);
}

#endif  // HYDROCONECTA_MONTADOR_JSON_H

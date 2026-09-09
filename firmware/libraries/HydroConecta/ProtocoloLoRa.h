// ===========================================================================
// ProtocoloLoRa.h - formato do pacote entre CONTROLADOR e GATEWAY.
//
// Este arquivo NAO usa nada do Arduino de proposito: assim ele compila
// tambem num PC, e a suite em firmware/testes/ exercita o formato de
// verdade, com g++, sem precisar do hardware. Um erro de parsing aqui
// significa uma leitura de nivel errada numa barragem -- e o tipo de coisa
// que tem de ser testada, nao inspecionada.
//
// FORMATO (uma linha, terminada por \n):
//
//     <id>|<valor>|<sts>*<CS>
//     nivel-rd01|12.500|1*6B
//
//   id     identificador do equipamento (o "sub_id" no servidor)
//   valor  medida bruta do sensor, com 3 casas
//   sts    1 = o sensor fisico respondeu; 0 = falha de leitura
//   CS     XOR de todos os bytes ANTES do '*', em hexadecimal maiusculo
//
// O checksum e o mesmo esquema do NMEA. Sem ele, um pacote corrompido no ar
// (LoRa em area de barragem tem ruido eletrico de sobra) passava como
// leitura valida: bastava ter dois '|'. Um bit trocado no meio do numero
// vira metros de diferenca no nivel do reservatorio.
//
// COMPATIBILIDADE: pacotes SEM o "*CS" continuam sendo aceitos, com o
// resultado HC_PACOTE_OK_SEM_CHECKSUM. Isso permite atualizar o gateway
// antes dos sensores em campo, sem derrubar nenhum. O gateway registra a
// diferenca no log para que se saiba quem ainda esta na versao antiga.
// ===========================================================================
#ifndef HYDROCONECTA_PROTOCOLO_LORA_H
#define HYDROCONECTA_PROTOCOLO_LORA_H

#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

enum HcResultadoPacote {
  HC_PACOTE_OK = 0,
  HC_PACOTE_OK_SEM_CHECKSUM,   // firmware antigo do sensor: aceito, mas anotado
  HC_PACOTE_CHECKSUM_INVALIDO, // chegou corrompido: DESCARTAR
  HC_PACOTE_MALFORMADO         // nao tem a forma esperada: DESCARTAR
};

// XOR de `n` bytes. Barato o bastante para rodar a cada pacote num ESP32.
inline unsigned char hc_checksum(const char* dados, size_t n) {
  unsigned char cs = 0;
  for (size_t i = 0; i < n; i++) cs ^= (unsigned char)dados[i];
  return cs;
}

// Monta o pacote em `saida`. Devolve o tamanho escrito, ou 0 se nao coube
// (nunca escreve um pacote truncado -- meio pacote seria interpretado como
// leitura valida do outro lado).
inline size_t hc_montar_pacote(char* saida, size_t cap,
                               const char* id, float valor, bool sensor_ok) {
  if (saida == 0 || cap == 0 || id == 0) return 0;
  int n = snprintf(saida, cap, "%s|%.3f|%d", id, (double)valor, sensor_ok ? 1 : 0);
  if (n < 0 || (size_t)n >= cap) { saida[0] = '\0'; return 0; }
  unsigned char cs = hc_checksum(saida, (size_t)n);
  int m = snprintf(saida + n, cap - (size_t)n, "*%02X", cs);
  if (m < 0 || (size_t)(n + m) >= cap) { saida[0] = '\0'; return 0; }
  return (size_t)(n + m);
}

// Interpreta uma linha recebida. `id_saida` recebe o identificador.
inline HcResultadoPacote hc_interpretar_pacote(const char* linha,
                                               char* id_saida, size_t id_cap,
                                               float* valor_saida,
                                               bool* sensor_ok_saida) {
  if (linha == 0 || id_saida == 0 || valor_saida == 0 || sensor_ok_saida == 0)
    return HC_PACOTE_MALFORMADO;

  size_t len = strlen(linha);
  if (len == 0 || len > 200) return HC_PACOTE_MALFORMADO;

  // --- checksum, se houver ---
  HcResultadoPacote nivel = HC_PACOTE_OK_SEM_CHECKSUM;
  const char* estrela = strrchr(linha, '*');
  size_t corpo = len;
  if (estrela != 0) {
    size_t pos = (size_t)(estrela - linha);
    if (len - pos != 3) return HC_PACOTE_MALFORMADO;   // "*XX" tem 3 bytes
    char hex[3] = { estrela[1], estrela[2], '\0' };
    char* fim = 0;
    long recebido = strtol(hex, &fim, 16);
    if (fim == 0 || *fim != '\0') return HC_PACOTE_MALFORMADO;
    if ((unsigned char)recebido != hc_checksum(linha, pos))
      return HC_PACOTE_CHECKSUM_INVALIDO;
    corpo = pos;
    nivel = HC_PACOTE_OK;
  }

  // --- campos ---
  const char* b1 = (const char*)memchr(linha, '|', corpo);
  if (b1 == 0) return HC_PACOTE_MALFORMADO;
  size_t apos1 = (size_t)(b1 - linha) + 1;
  const char* b2 = (const char*)memchr(linha + apos1, '|', corpo - apos1);
  if (b2 == 0) return HC_PACOTE_MALFORMADO;

  size_t id_len = (size_t)(b1 - linha);
  if (id_len == 0 || id_len >= id_cap) return HC_PACOTE_MALFORMADO;
  memcpy(id_saida, linha, id_len);
  id_saida[id_len] = '\0';

  size_t val_len = (size_t)(b2 - b1) - 1;
  if (val_len == 0 || val_len > 31) return HC_PACOTE_MALFORMADO;
  char val[32];
  memcpy(val, b1 + 1, val_len);
  val[val_len] = '\0';
  char* fim = 0;
  double v = strtod(val, &fim);
  if (fim == val || (fim != 0 && *fim != '\0')) return HC_PACOTE_MALFORMADO;
  *valor_saida = (float)v;

  size_t sts_len = corpo - ((size_t)(b2 - linha) + 1);
  if (sts_len != 1) return HC_PACOTE_MALFORMADO;
  char s = b2[1];
  if (s != '0' && s != '1') return HC_PACOTE_MALFORMADO;
  *sensor_ok_saida = (s == '1');

  return nivel;
}

// ACK do gateway para o controlador: "ACK:<id>".
inline size_t hc_montar_ack(char* saida, size_t cap, const char* id) {
  if (saida == 0 || cap == 0 || id == 0) return 0;
  int n = snprintf(saida, cap, "ACK:%s", id);
  if (n < 0 || (size_t)n >= cap) { saida[0] = '\0'; return 0; }
  return (size_t)n;
}

// O controlador so aceita o ACK que e DELE. Comparacao exata: um
// "ACK:nivel-rd01" nao pode satisfazer o "nivel-rd011".
inline bool hc_ack_confere(const char* linha, const char* id) {
  if (linha == 0 || id == 0) return false;
  char esperado[64];
  if (hc_montar_ack(esperado, sizeof(esperado), id) == 0) return false;
  return strcmp(linha, esperado) == 0;
}

#endif  // HYDROCONECTA_PROTOCOLO_LORA_H

// ===========================================================================
// Suite dos firmwares, rodando NO PC.
//
// Os tres cabecalhos de firmware/libraries/HydroConecta sao livres de
// Arduino justamente para isto: o formato do pacote LoRa, o montador de JSON
// e as regras de nivel podem ser exercitados de verdade, com g++, sem
// hardware. E o que separa "eu revisei o codigo" de "eu testei o codigo".
//
//   g++ -std=c++17 -I firmware/libraries/HydroConecta
//       firmware/testes/teste_firmware.cpp -o /tmp/teste && /tmp/teste
// ===========================================================================
#include <cstdio>
#include <cstring>
#include <cmath>

#include "MontadorJson.h"
#include "ProtocoloLoRa.h"
#include "RegrasNivel.h"

static int falhas = 0, total = 0;

static void checar(bool cond, const char* nome) {
  total++;
  if (!cond) { falhas++; printf("  FALHOU: %s\n", nome); }
}
static void checar_texto(const char* obtido, const char* esperado, const char* nome) {
  total++;
  if (strcmp(obtido, esperado) != 0) {
    falhas++;
    printf("  FALHOU: %s\n    obtido:   %s\n    esperado: %s\n", nome, obtido, esperado);
  }
}
static bool perto(double a, double b, double tol = 1e-6) { return fabs(a - b) <= tol; }

// ---------------------------------------------------------------- protocolo
static void testes_protocolo() {
  printf("protocolo LoRa\n");
  char pac[64];

  size_t n = hc_montar_pacote(pac, sizeof(pac), "nivel-rd01", 12.5f, true);
  checar(n > 0, "monta o pacote");
  checar_texto(pac, "nivel-rd01|12.500|1*6B", "formato do pacote");

  char id[32]; float v = 0; bool ok = false;
  checar(hc_interpretar_pacote(pac, id, sizeof(id), &v, &ok) == HC_PACOTE_OK,
         "ida e volta: aceito");
  checar_texto(id, "nivel-rd01", "ida e volta: id");
  checar(perto(v, 12.5, 1e-3), "ida e volta: valor");
  checar(ok == true, "ida e volta: status do sensor");

  // Compatibilidade: sensor em firmware antigo, sem checksum.
  checar(hc_interpretar_pacote("nivel-rd02|3.250|0", id, sizeof(id), &v, &ok)
         == HC_PACOTE_OK_SEM_CHECKSUM, "aceita pacote antigo, sem checksum");
  checar_texto(id, "nivel-rd02", "pacote antigo: id");
  checar(perto(v, 3.25, 1e-3) && ok == false, "pacote antigo: campos");

  // Um bit trocado no meio do numero: era exatamente isso que passava antes.
  char corrompido[64];
  strcpy(corrompido, "nivel-rd01|12.500|1*6B");
  corrompido[12] = '9';                       // 12.500 -> 19.500
  checar(hc_interpretar_pacote(corrompido, id, sizeof(id), &v, &ok)
         == HC_PACOTE_CHECKSUM_INVALIDO, "recusa pacote corrompido");

  checar(hc_interpretar_pacote("", id, sizeof(id), &v, &ok) == HC_PACOTE_MALFORMADO,
         "recusa linha vazia");
  checar(hc_interpretar_pacote("so-um-campo", id, sizeof(id), &v, &ok)
         == HC_PACOTE_MALFORMADO, "recusa sem separadores");
  checar(hc_interpretar_pacote("id||1", id, sizeof(id), &v, &ok)
         == HC_PACOTE_MALFORMADO, "recusa valor vazio");
  checar(hc_interpretar_pacote("id|abc|1", id, sizeof(id), &v, &ok)
         == HC_PACOTE_MALFORMADO, "recusa valor nao numerico");
  checar(hc_interpretar_pacote("id|1.0|7", id, sizeof(id), &v, &ok)
         == HC_PACOTE_MALFORMADO, "recusa status fora de 0/1");
  checar(hc_interpretar_pacote("|1.0|1", id, sizeof(id), &v, &ok)
         == HC_PACOTE_MALFORMADO, "recusa id vazio");
  char idpequeno[4];
  checar(hc_interpretar_pacote("nivel-rd01|1.0|1", idpequeno, sizeof(idpequeno), &v, &ok)
         == HC_PACOTE_MALFORMADO, "recusa id maior que o buffer");

  // Buffer curto demais: nao pode sair meio pacote no ar.
  char curto[8];
  checar(hc_montar_pacote(curto, sizeof(curto), "nivel-rd01", 12.5f, true) == 0,
         "nao monta pacote truncado");
  checar(curto[0] == '\0', "buffer curto fica vazio, nao truncado");

  // ACK
  char ack[64];
  checar(hc_montar_ack(ack, sizeof(ack), "nivel-rd01") > 0, "monta ACK");
  checar_texto(ack, "ACK:nivel-rd01", "formato do ACK");
  checar(hc_ack_confere("ACK:nivel-rd01", "nivel-rd01"), "ACK proprio e aceito");
  checar(!hc_ack_confere("ACK:nivel-rd011", "nivel-rd01"),
         "ACK de id parecido e recusado");
  checar(!hc_ack_confere("ACK:outro", "nivel-rd01"), "ACK de outro e recusado");
}

// -------------------------------------------------------------------- json
static void testes_json() {
  printf("montador de JSON\n");
  char buf[512];
  HcJson j;

  // A forma ANINHADA, que e a que o servidor separa sem adivinhar prefixo.
  hc_json_iniciar(&j, buf, sizeof(buf));
  hc_json_abrir_raiz(&j);
    hc_json_abrir_objeto(&j, "values");
      hc_json_abrir_objeto(&j, "nivel-rd01");
        hc_json_texto(&j, "status", "on");
        hc_json_num(&j, "distancia", 12.5, 3);
        hc_json_bool(&j, "alerta_revanche", false);
      hc_json_fechar_objeto(&j);
      hc_json_abrir_objeto(&j, "nivel-rd02");
        hc_json_texto(&j, "status", "off");
      hc_json_fechar_objeto(&j);
    hc_json_fechar_objeto(&j);
  hc_json_fechar_objeto(&j);
  checar(hc_json_ok(&j), "monta o payload aninhado");
  checar_texto(hc_json_texto_final(&j),
    "{\"values\":{\"nivel-rd01\":{\"status\":\"on\",\"distancia\":12.500,"
    "\"alerta_revanche\":false},\"nivel-rd02\":{\"status\":\"off\"}}}",
    "payload aninhado byte a byte");

  // NaN nao pode virar "nan" e quebrar o corpo inteiro.
  hc_json_iniciar(&j, buf, sizeof(buf));
  hc_json_abrir_raiz(&j);
    hc_json_num(&j, "a", NAN, 2);
    hc_json_num(&j, "b", INFINITY, 2);
    hc_json_num(&j, "c", 1.5, 2);
  hc_json_fechar_objeto(&j);
  checar(hc_json_ok(&j), "NaN nao invalida o documento");
  checar_texto(hc_json_texto_final(&j), "{\"a\":null,\"b\":null,\"c\":1.50}",
               "NaN e infinito viram null");

  // Estouro de buffer: marca falha, nao trunca em silencio.
  char apertado[24];
  hc_json_iniciar(&j, apertado, sizeof(apertado));
  hc_json_abrir_raiz(&j);
  for (int i = 0; i < 10; i++) hc_json_num(&j, "chave_longa", 123.456, 3);
  checar(!hc_json_ok(&j), "estouro de buffer e detectado");

  // Texto que quebraria o JSON e recusado em vez de escapado.
  hc_json_iniciar(&j, buf, sizeof(buf));
  hc_json_abrir_raiz(&j);
  hc_json_texto(&j, "x", "as\"pas");
  checar(!hc_json_ok(&j), "texto com aspas e recusado");

  // Lista, usada no campo "dispositivos" do payload.
  hc_json_iniciar(&j, buf, sizeof(buf));
  hc_json_abrir_raiz(&j);
    hc_json_abrir_lista(&j, "dispositivos");
      hc_json_item_texto(&j, "a");
      hc_json_item_texto(&j, "b");
    hc_json_fechar_lista(&j);
    hc_json_texto(&j, "z", "1");
  hc_json_fechar_objeto(&j);
  checar_texto(hc_json_texto_final(&j),
               "{\"dispositivos\":[\"a\",\"b\"],\"z\":\"1\"}", "lista de textos");
}

// ------------------------------------------------------------- regras nivel
static void testes_regras() {
  printf("regras de nivel\n");
  // Reservatorio de teste: antena a 30 m do fundo, 10 m no nivel cheio.
  HcConfigNivel cfg = { 30.0f, 10.0f, 1000000.0, 90.0f, 114.68f, 113.0f };
  HcNivel r;

  checar(hc_calcular_nivel(cfg, 30.0f, &r), "vazio: calcula");
  checar(perto(r.percentual, 0.0, 1e-4), "vazio: 0%");
  checar(perto(r.cota_atual, 90.0, 1e-3), "vazio: cota e a do FUNDO, nao zero");
  checar(perto(r.volume_m3, 0.0, 1e-6), "vazio: volume zero");
  checar(!r.alerta_revanche, "vazio: sem alerta");

  checar(hc_calcular_nivel(cfg, 10.0f, &r), "cheio: calcula");
  checar(perto(r.percentual, 100.0, 1e-4), "cheio: 100%");
  checar(perto(r.cota_atual, 114.68, 1e-3), "cheio: cota maxima");
  checar(perto(r.volume_m3, 1000000.0, 1e-3), "cheio: volume total");
  checar(perto(r.cota_restante, 0.0, 1e-3), "cheio: nada a subir");
  checar(r.alerta_revanche, "cheio: alerta de revanche disparado");

  checar(hc_calcular_nivel(cfg, 20.0f, &r), "meio: calcula");
  checar(perto(r.percentual, 50.0, 1e-4), "meio: 50%");
  checar(perto(r.cota_atual, 90.0 + 0.5 * 24.68, 1e-3), "meio: cota interpolada");

  // Travas nos extremos: o teto NAO existia antes.
  checar(hc_calcular_nivel(cfg, 5.0f, &r) && perto(r.percentual, 100.0, 1e-4),
         "leitura abaixo do cheio trava em 100%");
  checar(r.volume_m3 <= cfg.volume_total + 1e-6,
         "volume nunca passa do volume total");
  checar(hc_calcular_nivel(cfg, 40.0f, &r) && perto(r.percentual, 0.0, 1e-4),
         "leitura acima do vazio trava em 0%");

  // Calibracao incoerente: recusa em vez de publicar numero sem sentido.
  HcConfigNivel invertida = { 10.0f, 30.0f, 1000.0, 90.0f, 114.0f, 113.0f };
  checar(!hc_calcular_nivel(invertida, 20.0f, &r), "recusa vazio/cheio invertidos");
  HcConfigNivel iguais = { 10.0f, 10.0f, 1000.0, 90.0f, 114.0f, 113.0f };
  checar(!hc_calcular_nivel(iguais, 10.0f, &r), "recusa faixa zero");
  HcConfigNivel cota_ruim = { 30.0f, 10.0f, 1000.0, 120.0f, 114.0f, 113.0f };
  checar(!hc_calcular_nivel(cota_ruim, 20.0f, &r), "recusa cota de fundo acima da maxima");
  checar(!hc_calcular_nivel(cfg, NAN, &r), "recusa distancia NaN");
}

int main() {
  testes_protocolo();
  testes_json();
  testes_regras();
  printf("\n%d verificacoes, %d falha(s)\n", total, falhas);
  return falhas == 0 ? 0 : 1;
}

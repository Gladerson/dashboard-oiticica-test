// ===========================================================================
// RegrasNivel.h - do valor bruto do radar para as grandezas do operador.
//
// O sensor entrega UMA coisa: a distancia da antena ate a lamina d'agua.
// Todo o resto -- percentual, volume, cota, alerta de revanche -- e conta
// feita aqui, a partir da calibracao daquele reservatorio.
//
// DUAS APROXIMACOES, ditas em voz alta porque mudam a leitura de quem opera
// uma barragem:
//
//  1. COTA. A versao anterior calculava `cota = (perc/100) * cota_max`.
//     Isso trata a cota como proporcional ao percentual, o que so vale se o
//     fundo do reservatorio estivesse na cota ZERO -- nivel do mar. Com o
//     reservatorio vazio a formula dava "cota 0 m" para um fundo que na
//     verdade esta, digamos, a 90 m. Agora ha `cota_fundo`, e a cota e
//     interpolada entre ela e `cota_max`. CONFIRA esse valor no projeto da
//     barragem antes de por em producao: um erro aqui desloca todas as
//     leituras de cota.
//
//  2. VOLUME. `volume = (perc/100) * volume_total` supõe um reservatorio
//     prismatico -- paredes verticais. Nenhum reservatorio real e assim; o
//     volume cresce mais depressa perto do topo. O numero serve como ordem
//     de grandeza, NAO como medida. O certo e uma curva cota x volume
//     (tabela do projeto, interpolada). Ficou anotado no README como
//     pendencia; trocar so esta funcao basta.
//
// Livre de Arduino: a suite em firmware/testes/ confere as contas no PC.
// ===========================================================================
#ifndef HYDROCONECTA_REGRAS_NIVEL_H
#define HYDROCONECTA_REGRAS_NIVEL_H

#include <math.h>

struct HcConfigNivel {
  float  calib_vazio;    // distancia (m) da antena ate o fundo: reservatorio vazio
  float  calib_cheio;    // distancia (m) da antena ate a lamina no nivel 100%
  double volume_total;   // m3 no nivel 100%
  float  cota_fundo;     // cota (m) correspondente ao reservatorio vazio
  float  cota_max;       // cota (m) no nivel 100%
  float  cota_alerta;    // cota (m) de revanche: acima disso, alerta
};

struct HcNivel {
  float  percentual;      // 0..100, sempre dentro da faixa
  double volume_m3;
  double volume_rest_m3;
  float  cota_atual;
  float  cota_restante;   // quanto falta para a cota maxima
  bool   alerta_revanche;
  bool   calibracao_ok;   // false = a configuracao daquele equipamento nao fecha
};

// `distancia` e a medida bruta, em metros. Devolve false se a calibracao for
// incoerente -- nesse caso NADA e publicado para aquele equipamento, em vez
// de publicar um numero sem sentido.
inline bool hc_calcular_nivel(const HcConfigNivel& cfg, float distancia,
                              HcNivel* out) {
  if (out == 0) return false;
  out->calibracao_ok = false;

  const float faixa = cfg.calib_vazio - cfg.calib_cheio;
  // Vazio TEM de estar mais longe da antena do que cheio. Se estiver
  // invertido (erro de digitacao na tabela), a conta daria nivel negativo
  // subindo -- pior que nao medir.
  if (!(faixa > 0.0f) || !isfinite(distancia)) return false;
  if (!isfinite(cfg.cota_max) || !isfinite(cfg.cota_fundo)) return false;
  if (cfg.cota_max < cfg.cota_fundo) return false;

  // A fracao e calculada em DOUBLE e so depois vira percentual. Fazendo ao
  // contrario -- percentual em float, dividido por 100 -- o residuo do float
  // aparecia multiplicado pelo volume: 0,6 exato virava 445.200.028 m3 em vez
  // de 445.200.000. Erro minusculo em termos relativos, mas e um numero que
  // vai para o relatorio de uma barragem.
  const double nivel = (double)cfg.calib_vazio - (double)distancia;
  double fracao = nivel / (double)faixa;
  // Trava nos dois extremos. So o piso era travado antes: uma leitura mais
  // curta que `calib_cheio` (onda, espuma, eco em estrutura) produzia
  // "112%" e, junto, um volume maior que o volume total do reservatorio.
  if (fracao < 0.0) fracao = 0.0;
  if (fracao > 1.0) fracao = 1.0;

  out->percentual     = (float)(fracao * 100.0);
  out->volume_m3      = fracao * cfg.volume_total;
  out->volume_rest_m3 = cfg.volume_total - out->volume_m3;
  out->cota_atual     = cfg.cota_fundo + (float)(fracao * (cfg.cota_max - cfg.cota_fundo));
  out->cota_restante  = cfg.cota_max - out->cota_atual;
  out->alerta_revanche = (out->cota_atual >= cfg.cota_alerta);
  out->calibracao_ok  = true;
  return true;
}

#endif  // HYDROCONECTA_REGRAS_NIVEL_H

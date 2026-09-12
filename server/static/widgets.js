// ==========================================================================
// widgets.js - Motor de desenho dos widgets de telemetria.
//
// Por que existe: os mesmos graficos aparecem em DOIS lugares -- na tela
// "Telemetrias", onde sao configurados, e flutuando por cima do modelo 3D no
// "Painel SHM", quando o operador liga um equipamento na lista da direita.
// Ate aqui esse codigo morava dentro de monitoramento.html; copia-lo para o
// dashboard significaria duas versoes do mesmo grafico divergindo com o
// tempo -- o classico "no painel esta certo, no 3D esta velho".
//
// Nao ha estado global aqui: tudo que o desenho precisa chega num CONTEXTO,
// para os dois chamadores poderem ter dispositivos diferentes abertos ao
// mesmo tempo.
//
//   ctx = {
//     deviceId,             // de quem sao estas telemetrias
//     ultimos: { "sub|chave": {num, txt, visto_em} },
//     alarmes: [],          // regras (widget "alarmes")
//     eventos: [],          // historico (widget "alarmes")
//     base: "",             // prefixo das rotas (vazio = mesma origem)
//     aoExcluirAlarme,      // opcional: sem ela a tabela nao mostra "excluir"
//   }
//
// Tudo e SVG escrito a mao, sem CDN: o painel precisa abrir numa rede sem
// internet, e ja foi um CDN bloqueado que derrubou uma tela inteira antes.
// ==========================================================================
(function (global) {
  "use strict";

  var CORES = ["#4fa3ff", "#ffb347", "#5ad1a5", "#ff7a90", "#c39bff", "#7fd3ff"];

  // Tamanho inicial sugerido, em pixels, por tipo. So vale para um widget
  // recem-criado: dali em diante quem manda e o que o operador arrastou.
  // Um grafico de area precisa de largura; um card, nao.
  var TAMANHO_PADRAO = {
    area:         { w: 520, h: 260 },
    barras:       { w: 400, h: 240 },
    alarmes:      { w: 520, h: 300 },
    card:         { w: 240, h: 160 },
    radial:       { w: 240, h: 220 },
    status:       { w: 260, h: 170 },
    equipamentos: { w: 320, h: 200 },
  };

  function escapar(t) {
    var d = document.createElement("div");
    d.textContent = t == null ? "" : String(t);
    return d.innerHTML;
  }

  function chaveDe(sub, chave) { return (sub || "") + "|" + chave; }

  function fmt(v, casas, unidade) {
    if (v == null || Number.isNaN(v)) return "—";
    var n = Number(v).toLocaleString("pt-BR", {
      minimumFractionDigits: casas, maximumFractionDigits: casas,
    });
    return unidade ? n + " " + unidade : n;
  }

  /** Numero curto o bastante para caber na caixa. 742.000.000 -> "742 M".
   *
   *  Um volume de reservatorio tem nove digitos; escrito por extenso, ele
   *  atravessava a borda do card e empurrava o eixo do grafico para fora da
   *  area visivel. Abaixo de 10 mil nada e resumido: ali o numero inteiro
   *  cabe e diz mais que "9,9 k". O valor exato continua acessivel na dica
   *  (atributo title) de quem mostra o resumo. */
  function resumir(v, unidade) {
    if (v == null || Number.isNaN(v)) return "—";
    var n = Number(v);
    if (!Number.isFinite(n)) return "—";
    var abs = Math.abs(n), txt;
    if (abs >= 1e12)      txt = (n / 1e12).toFixed(2).replace(/\.?0+$/, "") + " T";
    else if (abs >= 1e9)  txt = (n / 1e9).toFixed(2).replace(/\.?0+$/, "") + " G";
    else if (abs >= 1e6)  txt = (n / 1e6).toFixed(2).replace(/\.?0+$/, "") + " M";
    else if (abs >= 1e4)  txt = (n / 1e3).toFixed(1).replace(/\.?0+$/, "") + " k";
    else txt = n.toLocaleString("pt-BR", { maximumFractionDigits: 2 });
    return unidade ? txt + " " + unidade : txt;
  }

  /** "45 s", "12 min", "3 h", "2 dias" -- o suficiente para o leitor saber o
   *  tamanho do intervalo que virou um ponto. */
  function duracao(segundos) {
    var s = Math.round(segundos);
    if (s < 90) return s + " s";
    if (s < 5400) return Math.round(s / 60) + " min";
    if (s < 172800) return Math.round(s / 3600) + " h";
    return Math.round(s / 86400) + " dias";
  }

  function valorDe(ctx, w, chave) {
    return (ctx.ultimos || {})[chaveDe(w.sub_id, chave)] || null;
  }

  function tituloPadrao(w) {
    if (w.tipo === "alarmes") return "Alarmes";
    if (w.tipo === "equipamentos") return "Equipamentos";
    var onde = w.sub_id ? w.sub_id + " · " : "";
    return onde + (w.chaves || []).join(", ");
  }

  function tamanhoPadrao(w) {
    return TAMANHO_PADRAO[w.tipo] || { w: 260, h: 180 };
  }

  /** O que este widget desenha, resumido. Se nao mudou, nao se redesenha --
   *  e o que impede a tela inteira de piscar a cada telemetria. */
  function assinatura(w, ctx) {
    var dados = (w.chaves || []).map(function (c) {
      var i = valorDe(ctx, w, c);
      return i ? c + "=" + i.num + "|" + i.txt + "|" + i.visto_em : c + "=-";
    });
    if (w.tipo === "alarmes") {
      dados.push((ctx.eventos || []).map(function (e) { return e.id + e.lido; }).join(","));
      dados.push((ctx.alarmes || []).map(function (a) { return a.id + a.disparado; }).join(","));
    }
    if (w.tipo === "equipamentos") {
      // Nao tem chaves escolhidas: o que ele mostra vem de varrer `ultimos`.
      dados.push(equipamentosDe(ctx).map(function (e) {
        return e.sub_id + "=" + e.estado + "@" + e.visto_em;
      }).join(","));
    }
    return JSON.stringify([w.tipo, w.titulo, w.sub_id, w.chaves, w.config, dados]);
  }

  // ------------------------------------------------------------------
  // Desenho
  // ------------------------------------------------------------------
  async function desenhar(w, alvo, ctx) {
    var cfg = w.config || {};
    var casas = Number.isFinite(+cfg.casas) ? +cfg.casas : 2;
    // A tabela e a lista de status querem fluxo normal; os graficos querem a
    // caixa centralizada. Sem zerar aqui, um widget que TROCA de tipo herdava
    // o display do tipo anterior e ficava desalinhado.
    alvo.style.display = "";
    if (w.tipo === "alarmes") return tabelaAlarmes(alvo, ctx);
    if (w.tipo === "equipamentos") return listaEquipamentos(alvo, ctx);
    if (!w.chaves || !w.chaves.length) {
      alvo.innerHTML = '<div class="vazio">Widget sem telemetria escolhida.</div>';
      return;
    }
    if (w.tipo === "card")   return card(w, alvo, ctx, cfg, casas);
    if (w.tipo === "radial") return radial(w, alvo, ctx, cfg, casas);
    if (w.tipo === "status") return status(w, alvo, ctx);
    if (w.tipo === "barras") return barras(w, alvo, ctx, cfg, casas);
    if (w.tipo === "area")   return area(w, alvo, ctx, cfg, casas);
  }

  function card(w, alvo, ctx, cfg, casas) {
    var i = valorDe(ctx, w, w.chaves[0]);
    var quando = i ? new Date(i.visto_em).toLocaleString("pt-BR") : "sem leitura";
    // Resumido para caber; o valor exato fica na dica do mouse. Um card de
    // volume mostrava "742.000.000,00" e atravessava a borda do widget.
    var numerico = i && i.num != null;
    var valor = numerico ? resumir(i.num, "") : (i && i.txt) || "—";
    var exato = numerico ? fmt(i.num, casas, cfg.unidade || "") : "";
    alvo.innerHTML =
      '<div class="cartao-valor"' + (exato ? ' title="' + escapar(exato) + '"' : "") + ">"
      + '<div class="n">' + escapar(valor)
      + (cfg.unidade ? '<span class="u">' + escapar(cfg.unidade) + "</span>" : "")
      + '</div><div class="quando">' + escapar(quando) + "</div></div>";
  }

  function radial(w, alvo, ctx, cfg, casas) {
    var i = valorDe(ctx, w, w.chaves[0]);
    var min = Number.isFinite(+cfg.min) ? +cfg.min : 0;
    var max = Number.isFinite(+cfg.max) ? +cfg.max : 100;
    var v = i && i.num != null ? i.num : null;
    // Arco de 240 graus (de -210 a 30): a abertura embaixo e o que faz o
    // desenho ser lido como "medidor" e nao como "pizza".
    var t = v == null ? 0 : Math.min(1, Math.max(0, (v - min) / (max - min || 1)));
    var R = 52, cx = 70, cy = 68;
    var arco = function (frac) {
      var a0 = -210 * Math.PI / 180, a1 = (-210 + 240 * frac) * Math.PI / 180;
      var x0 = cx + R * Math.cos(a0), y0 = cy + R * Math.sin(a0);
      var x1 = cx + R * Math.cos(a1), y1 = cy + R * Math.sin(a1);
      var grande = 240 * frac > 180 ? 1 : 0;
      return "M " + x0.toFixed(2) + " " + y0.toFixed(2) + " A " + R + " " + R
           + " 0 " + grande + " 1 " + x1.toFixed(2) + " " + y1.toFixed(2);
    };
    alvo.innerHTML =
      '<svg viewBox="0 0 140 108" role="img" aria-label="medidor">'
      + '<path d="' + arco(1) + '" fill="none" stroke="var(--border)" stroke-width="12" stroke-linecap="round"/>'
      + (t > 0 ? '<path d="' + arco(t) + '" fill="none" stroke="' + CORES[0]
                 + '" stroke-width="12" stroke-linecap="round"/>' : "")
      + '<text x="70" y="66" text-anchor="middle" fill="var(--text)" font-size="21" font-weight="700">'
      + escapar(v == null ? "—" : resumir(v, ""))
      + "<title>" + escapar(v == null ? "sem leitura" : fmt(v, casas, cfg.unidade || "")) + "</title></text>"
      + '<text x="70" y="82" text-anchor="middle" fill="var(--muted)" font-size="10">'
      + escapar(cfg.unidade || "") + "</text>"
      + '<text x="14" y="100" fill="var(--muted)" font-size="9">' + escapar(resumir(min, "")) + "</text>"
      + '<text x="126" y="100" text-anchor="end" fill="var(--muted)" font-size="9">'
      + escapar(resumir(max, "")) + "</text></svg>";
  }

  function classeStatus(i) {
    if (!i) return ["desconhecido", "sem leitura"];
    var t = (i.txt || "").toLowerCase();
    if (["on", "online", "ok", "true"].indexOf(t) >= 0) return ["ok", i.txt];
    if (["off", "offline", "false"].indexOf(t) >= 0) return ["ruim", i.txt];
    if (["erro", "falha"].indexOf(t) >= 0) return ["aviso", i.txt];
    if (i.num != null) return [i.num ? "ok" : "ruim", String(i.num)];
    return ["desconhecido", i.txt || "—"];
  }

  function status(w, alvo, ctx) {
    alvo.style.display = "block";
    alvo.innerHTML = w.chaves.map(function (c) {
      var par = classeStatus(valorDe(ctx, w, c));
      return '<div class="status-linha"><span class="ponto ' + par[0] + '"></span>'
           + "<b>" + escapar(c) + '</b><span class="v">' + escapar(par[1]) + "</span></div>";
    }).join("");
  }

  function barras(w, alvo, ctx, cfg, casas) {
    var dados = w.chaves.map(function (c) {
      var i = valorDe(ctx, w, c);
      return { rotulo: c, v: i && i.num != null ? i.num : 0 };
    });
    var max = Math.max.apply(null, dados.map(function (d) { return d.v; })
      .concat([Number.isFinite(+cfg.max) ? +cfg.max : 0, 1]));
    var larg = 320, alt = 26 * dados.length + 16, x0 = 108;
    var corpo = dados.map(function (d, k) {
      var y = 8 + k * 26;
      var l = Math.max(1, (larg - x0 - 56) * (d.v / max));
      return '<text x="' + (x0 - 8) + '" y="' + (y + 12) + '" text-anchor="end" fill="var(--muted)" font-size="10">'
           + escapar(d.rotulo) + "</text>"
           + '<rect x="' + x0 + '" y="' + y + '" width="' + l.toFixed(1)
           + '" height="16" rx="3" fill="' + CORES[k % CORES.length] + '"/>'
           + '<text x="' + (x0 + l + 6) + '" y="' + (y + 12) + '" fill="var(--text)" font-size="10">'
           + escapar(resumir(d.v, cfg.unidade || ""))
           + "<title>" + escapar(fmt(d.v, casas, cfg.unidade || "")) + "</title></text>";
    }).join("");
    alvo.innerHTML = '<svg viewBox="0 0 ' + larg + " " + alt + '" role="img">' + corpo + "</svg>";
  }

  async function area(w, alvo, ctx, cfg) {
    // 0 (ou ausente) = TUDO, do primeiro registro ate agora. E o padrao: numa
    // barragem, a pergunta e a tendencia, e recortar as ultimas 24 h por
    // conta propria escondia justamente isso.
    var minutos = Number.isFinite(+cfg.minutos) ? +cfg.minutos : 0;
    var base = ctx.base || "";
    var series = [];
    var passo = 0;
    for (var k = 0; k < w.chaves.length; k++) {
      var c = w.chaves[k];
      var j = await fetch(base + "/api/telemetria/serie?device_id="
        + encodeURIComponent(ctx.deviceId) + "&sub_id=" + encodeURIComponent(w.sub_id || "")
        + "&chave=" + encodeURIComponent(c) + "&minutos=" + minutos)
        .then(function (r) { return r.json(); });
      passo = Math.max(passo, +j.passo_s || 0);
      series.push({
        chave: c,
        pontos: (j.pontos || []).filter(function (p) { return p.v != null; }),
      });
    }
    var todos = series.reduce(function (acc, s) { return acc.concat(s.pontos); }, []);
    if (!todos.length) {
      alvo.innerHTML = '<div class="vazio">Ainda não há leituras para o período.</div>';
      return;
    }
    var tempos = todos.map(function (p) { return Date.parse(p.em); });
    var vals = todos.map(function (p) { return p.v; });
    var t0 = Math.min.apply(null, tempos), t1 = Math.max.apply(null, tempos);
    var vmin = Math.min.apply(null, vals), vmax = Math.max.apply(null, vals);
    if (Number.isFinite(+cfg.min)) vmin = +cfg.min;
    if (Number.isFinite(+cfg.max)) vmax = +cfg.max;
    if (vmax === vmin) { vmax = vmin + 1; vmin -= 1; }   // serie constante

    var L = 560, A = 200, m = { e: 46, d: 10, c: 10, b: 24 };
    var px = function (t) { return m.e + (L - m.e - m.d) * ((t - t0) / (t1 - t0 || 1)); };
    var py = function (v) { return m.c + (A - m.c - m.b) * (1 - (v - vmin) / (vmax - vmin)); };
    var traco = function (s) {
      return s.pontos.map(function (p, i) {
        return (i ? "L" : "M") + " " + px(Date.parse(p.em)).toFixed(1) + " " + py(p.v).toFixed(1);
      }).join(" ");
    };

    // As areas vao TODAS antes das linhas: desenhando por serie, a area de uma
    // cobria a linha da outra quando as escalas eram muito diferentes (uma
    // telemetria perto de 100 e outra perto de 12, por exemplo).
    var comPontos = series.filter(function (s) { return s.pontos.length; });
    var areas = comPontos.map(function (s, k) {
      var ultimo = s.pontos[s.pontos.length - 1];
      var base2 = "L " + px(Date.parse(ultimo.em)).toFixed(1) + " " + py(vmin).toFixed(1)
                + " L " + px(Date.parse(s.pontos[0].em)).toFixed(1) + " " + py(vmin).toFixed(1) + " Z";
      return '<path d="' + traco(s) + " " + base2 + '" fill="' + CORES[k % CORES.length] + '" opacity=".12"/>';
    }).join("");
    var linhasSerie = comPontos.map(function (s, k) {
      return '<path d="' + traco(s) + '" fill="none" stroke="' + CORES[k % CORES.length]
           + '" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>';
    }).join("");

    var guias = [0, 0.5, 1].map(function (f) {
      var v = vmin + (vmax - vmin) * f, y = py(v);
      return '<line x1="' + m.e + '" y1="' + y.toFixed(1) + '" x2="' + (L - m.d)
           + '" y2="' + y.toFixed(1) + '" stroke="var(--border)" stroke-width="1"/>'
           + '<text x="' + (m.e - 6) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end" '
           + 'fill="var(--muted)" font-size="9">' + escapar(resumir(v, "")) + "</text>";
    }).join("");

    var hora = function (t) {
      return new Date(t).toLocaleString("pt-BR",
        { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    };

    alvo.style.display = "block";
    alvo.innerHTML =
      '<svg viewBox="0 0 ' + L + " " + A + '" role="img">' + guias + areas + linhasSerie
      + '<text x="' + m.e + '" y="' + (A - 6) + '" fill="var(--muted)" font-size="9">'
      + escapar(hora(t0)) + "</text>"
      + '<text x="' + (L - m.d) + '" y="' + (A - 6) + '" text-anchor="end" fill="var(--muted)" font-size="9">'
      + escapar(hora(t1)) + "</text></svg>"
      + '<div class="legenda">' + series.map(function (s, k) {
          return '<span><i style="background:' + CORES[k % CORES.length] + '"></i>'
               + escapar(s.chave) + "</span>";
        }).join("")
        // Quando o servidor reamostra, cada ponto e a MEDIA do intervalo, nao
        // uma leitura. Dizer isso e o que impede alguem de ler um pico
        // suavizado como se fosse a medida daquele instante.
        + (passo > 0 ? '<span class="nota">média por ' + escapar(duracao(passo))
                        + "</span>" : "")
      + "</div>";
  }

  /** Os equipamentos remotos deste gateway, com o status de cada um.
   *
   *  A informacao ja chega: o gateway publica `status` para cada sub_id
   *  (ver montarEquipamento() no firmware). O que faltava era um lugar para
   *  olhar todos de uma vez -- o indicador de status comum so serve para UM
   *  sub_id por widget, e com cinco controladores seriam cinco widgets. */
  function equipamentosDe(ctx) {
    var lista = [];
    var ultimos = ctx.ultimos || {};
    Object.keys(ultimos).forEach(function (k) {
      var corte = k.indexOf("|");
      if (corte <= 0) return;                 // sub_id vazio: e do proprio gateway
      if (k.slice(corte + 1) !== "status") return;
      var i = ultimos[k];
      var txt = (i.txt || "").trim().toLowerCase();
      lista.push({
        sub_id: k.slice(0, corte),
        estado: txt || (i.num ? "on" : "off"),
        visto_em: i.visto_em,
      });
    });
    lista.sort(function (a, b) { return a.sub_id < b.sub_id ? -1 : 1; });
    return lista;
  }

  // 'on' = medindo; 'erro' = o controlador fala, mas o instrumento falhou;
  // 'off' = o controlador nao fala. Sao tres situacoes com respostas
  // diferentes, e por isso tres cores.
  var CLASSE_ESTADO = { on: "ok", online: "ok", ok: "ok",
                        erro: "aviso", falha: "aviso",
                        off: "ruim", offline: "ruim" };
  var ROTULO_ESTADO = { on: "online", online: "online", ok: "online",
                        erro: "erro no sensor", falha: "erro no sensor",
                        off: "sem comunicação", offline: "sem comunicação" };

  function listaEquipamentos(alvo, ctx) {
    alvo.style.display = "block";
    var lista = equipamentosDe(ctx);
    if (!lista.length) {
      alvo.innerHTML = '<div class="vazio">Este gateway ainda não reportou '
        + "nenhum equipamento.</div>";
      return;
    }
    alvo.innerHTML = lista.map(function (e) {
      var cls = CLASSE_ESTADO[e.estado] || "desconhecido";
      var rot = ROTULO_ESTADO[e.estado] || e.estado;
      var quando = e.visto_em ? new Date(e.visto_em).toLocaleString("pt-BR") : "";
      return '<div class="status-linha" title="' + escapar(quando) + '">'
        + '<span class="ponto ' + cls + '"></span>'
        + "<b>" + escapar(e.sub_id) + '</b><span class="v">' + escapar(rot)
        + "</span></div>";
    }).join("");
  }

  function tabelaAlarmes(alvo, ctx) {
    alvo.style.display = "block";
    var podeExcluir = typeof ctx.aoExcluirAlarme === "function";
    var regras = (ctx.alarmes || []).map(function (a) {
      var cond = ({ maior: ">", menor: "<", igual: "=" })[a.condicao] || a.condicao;
      var onde = a.sub_id ? escapar(a.sub_id) + " · " : "";
      return '<tr class="' + (a.disparado ? "disparado" : "") + '">'
        + "<td>" + escapar(a.titulo || a.chave) + "</td>"
        + "<td>" + onde + escapar(a.chave) + " " + cond + " " + a.limite + "</td>"
        + "<td>" + (a.disparado ? "disparado" : "normal") + "</td>"
        + (podeExcluir
            ? '<td><button data-alarme="' + escapar(a.id) + '">excluir</button></td>'
            : "<td></td>")
        + "</tr>";
    }).join("");
    // O historico mistura duas origens: alarme de telemetria e deteccao da
    // visao computacional. A de visao nao tem chave nem limite -- desenha-la
    // como alarme produzia "undefined = —".
    var hist = (ctx.eventos || []).slice(0, 12).map(function (e) {
      var detalhe = e.origem === "visao"
        ? "visão computacional"
        : escapar(e.sub_id ? e.sub_id + " · " : "") + escapar(e.chave || "")
          + " = " + (e.valor == null ? "—" : escapar(resumir(e.valor, "")));
      return '<tr class="' + escapar(e.estado) + '">'
        + "<td>" + escapar(e.titulo) + "</td><td>" + detalhe + "</td>"
        + "<td>" + escapar(e.estado) + "</td>"
        + "<td>" + new Date(e.em).toLocaleString("pt-BR") + "</td></tr>";
    }).join("");

    alvo.innerHTML =
      '<table class="alarmes"><thead><tr><th>Regra</th><th>Condição</th><th>Estado</th><th></th></tr></thead>'
      + "<tbody>" + (regras || '<tr><td colspan="4" class="vazio">Nenhuma regra configurada.</td></tr>')
      + "</tbody></table>"
      + '<h4 class="sub-titulo">Histórico</h4>'
      + '<table class="alarmes"><tbody>'
      + (hist || '<tr><td class="vazio">Sem eventos.</td></tr>') + "</tbody></table>";

    if (podeExcluir) {
      alvo.querySelectorAll("button[data-alarme]").forEach(function (b) {
        b.addEventListener("click", function () { ctx.aoExcluirAlarme(b.dataset.alarme); });
      });
    }
  }

  global.Widgets = {
    CORES: CORES,
    desenhar: desenhar,
    assinatura: assinatura,
    tituloPadrao: tituloPadrao,
    tamanhoPadrao: tamanhoPadrao,
    valorDe: valorDe,
    chaveDe: chaveDe,
    escapar: escapar,
    fmt: fmt,
    resumir: resumir,
  };
})(window);

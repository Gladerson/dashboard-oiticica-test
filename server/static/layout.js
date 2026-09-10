// ==========================================================================
// layout.js - Monta o menu lateral e a barra de titulo das telas do painel.
//
// Por que em JS e nao HTML repetido nas tres paginas: sao tres telas
// (dashboard, dispositivos, configuracao) e todo item novo de menu teria
// que ser copiado nas tres, com o risco classico de uma ficar para tras.
// Aqui a navegacao existe num lugar so.
//
// Uso (a pagina ja precisa ter <link rel="stylesheet" href="/estatico/layout.css">):
//
//   <div class="app-shell" id="shell">
//     <div class="conteudo">
//       <div class="pagina pagina--rola"> ...conteudo da pagina... </div>
//     </div>
//   </div>
//   <script src="/estatico/layout.js"></script>
//   <script>Layout.montar({ ativo: "dispositivos", titulo: "Dispositivos" });</script>
//
// montar() insere o <aside> do menu antes de .conteudo e a .topbar dentro
// dela, entao a pagina nao precisa saber como o menu e desenhado.
// ==========================================================================
(function (global) {
  "use strict";

  var CHAVE_MENU = "oiticica_menu_fechado";
  var CHAVE_TEMA = "oiticica_tema";

  // Icones inline (sem CDN: a tela de login precisa funcionar numa LAN sem
  // internet, e o resto do painel segue a mesma regra).
  var ICONES = {
    dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M9.5 21v-6h5v6"/></svg>',
    // Um CHIP, nao uma camera. Este menu leva ao cadastro de qualquer
    // equipamento -- camera PTZ, sensor, gateway --, e o icone de camera
    // fazia parecer que sensores e gateways moravam em outro lugar.
    dispositivos: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="7" width="10" height="10" rx="2"/><rect x="10.5" y="10.5" width="3" height="3" rx="0.5"/><path d="M10 7V4M14 7V4M10 20v-3M14 20v-3M7 10H4M7 14H4M20 10h-3M20 14h-3"/></svg>',
    config: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 9 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 9a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/></svg>',
    sair: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 17l5-5-5-5"/><path d="M20 12H9"/><path d="M9 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h3"/></svg>',
    menu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M4 7h16"/><path d="M4 12h16"/><path d="M4 17h16"/></svg>',
    monitoramento: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 14l3.5-4 3 3L21 6"/></svg>',
    sino: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 1 0-12 0c0 6-3 7-3 7h18s-3-1-3-7"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>',
  };

  // A gota da marca, so o simbolo (a assinatura completa fica na tela de
  // login). Os ids do gradiente levam prefixo proprio para nao colidirem com
  // outro SVG da mesma pagina.
  var MARCA_GOTA =
    '<svg viewBox="0 0 200 260" aria-hidden="true">' +
      '<defs><linearGradient id="hc-mini" x1="0.15" y1="0" x2="0.85" y2="1">' +
        '<stop offset="0" stop-color="#0d5f78"/>' +
        '<stop offset="0.45" stop-color="#1a8fa8"/>' +
        '<stop offset="1" stop-color="#15b9c9"/>' +
      "</linearGradient></defs>" +
      '<path d="M100 34c0 0 74 84 74 128a74 74 0 0 1-148 0C26 118 100 34 100 34Z" ' +
        'fill="url(#hc-mini)"/>' +
      '<path d="M64 152 88 196 143 128" fill="none" stroke="#fff" stroke-width="13" ' +
        'stroke-linecap="round" stroke-linejoin="round"/>' +
    "</svg>";

  var ITENS = [
    { chave: "dashboard", href: "/", rotulo: "Painel SHM", icone: ICONES.dashboard },
    { chave: "dispositivos", href: "/dispositivos", rotulo: "Dispositivos", icone: ICONES.dispositivos },
    { chave: "monitoramento", href: "/monitoramento", rotulo: "Telemetrias", icone: ICONES.monitoramento },
    { chave: "config", href: "/config", rotulo: "Configuração", icone: ICONES.config },
  ];

  function ler(chave) {
    try { return localStorage.getItem(chave); } catch (e) { return null; }
  }
  function gravar(chave, valor) {
    try { localStorage.setItem(chave, valor); } catch (e) { /* modo privado */ }
  }

  function aplicarTema(tema) {
    document.body.classList.toggle("tema-claro", tema === "claro");
  }

  // Aplica o tema salvo IMEDIATAMENTE (antes de /api/usuarios/me responder),
  // senao a tela pisca no tema errado a cada navegacao.
  aplicarTema(ler(CHAVE_TEMA) || "escuro");

  function escapar(texto) {
    var d = document.createElement("div");
    d.textContent = texto == null ? "" : String(texto);
    return d.innerHTML;
  }

  function montar(opcoes) {
    opcoes = opcoes || {};
    var shell = document.getElementById(opcoes.shell || "shell");
    if (!shell) throw new Error("layout.js: nao achei o elemento .app-shell");
    var conteudo = shell.querySelector(".conteudo");
    if (!conteudo) throw new Error("layout.js: nao achei .conteudo dentro do shell");

    if (ler(CHAVE_MENU) === "1") shell.classList.add("menu-fechado");

    // ---- menu lateral ----
    var aside = document.createElement("aside");
    aside.className = "sidebar";
    var links = ITENS.map(function (item) {
      var ativo = item.chave === opcoes.ativo ? " class=\"ativo\"" : "";
      return '<a href="' + item.href + '"' + ativo + ' title="' + item.rotulo + '">' +
             item.icone + "<span>" + item.rotulo + "</span></a>";
    }).join("");
    aside.innerHTML =
      '<div class="sidebar-marca">' +
        '<div class="sigla">' + MARCA_GOTA + "</div>" +
        '<div class="nome">HydroConecta' +
                // Curto de proposito: o menu lateral tem largura fixa e a frase
        // completa da marca ficava cortada no meio da palavra.
        "<small>Telemetria e integridade</small></div>" +
      "</div>" +
      '<nav class="sidebar-nav">' + links + "</nav>" +
      '<div class="sidebar-rodape">' +
        '<div class="avatar" id="layout-avatar">·</div>' +
        '<div class="quem"><b id="layout-usuario">—</b><span id="layout-papel"></span></div>' +
        '<button type="button" id="layout-sair" title="Sair">' + ICONES.sair + "</button>" +
      "</div>";
    shell.insertBefore(aside, conteudo);

    // ---- barra de titulo ----
    var topbar = document.createElement("header");
    topbar.className = "topbar";
    topbar.innerHTML =
      '<button type="button" class="botao-menu" id="layout-alternar-menu" ' +
      'title="Recolher/expandir o menu">' + ICONES.menu + "</button>" +
      "<h1>" + escapar(opcoes.titulo || document.title) + "</h1>" +
      '<div class="espaco"></div>' +
      '<div class="acoes" id="layout-acoes"></div>';
    conteudo.insertBefore(topbar, conteudo.firstChild);

    document.getElementById("layout-alternar-menu").addEventListener("click", function () {
      var fechado = shell.classList.toggle("menu-fechado");
      gravar(CHAVE_MENU, fechado ? "1" : "0");
      // O dashboard redimensiona o canvas 3D via ResizeObserver, entao nao
      // precisa de aviso aqui; quem quiser reagir pode ouvir este evento.
      global.dispatchEvent(new CustomEvent("layout:menu", { detail: { fechado: fechado } }));
    });

    document.getElementById("layout-sair").addEventListener("click", async function () {
      encerrando = true;   // nao deixa o vigia disparar "expirou" no caminho
      try { await fetch("/api/logout", { method: "POST" }); } catch (e) { /* segue */ }
      location.href = "/login?motivo=saiu";
    });

    montarSino(document.getElementById("layout-acoes"));
    iniciarVigiaDeSessao(conteudo);
    carregarUsuario();
    return { acoes: document.getElementById("layout-acoes") };
  }

  // ==========================================================================
  // Sininho de alarmes
  //
  // Fica em TODAS as telas (esta no cabecalho compartilhado): um alarme que
  // dispara enquanto o operador olha o video 3D precisa aparecer do mesmo
  // jeito. A contagem vem do servidor; o WebSocket avisa na hora, e o
  // intervalo de 60s e a rede de seguranca para quando o socket cai.
  // ==========================================================================
  var sinoAberto = false;

  function montarSino(acoes) {
    if (!acoes) return;
    var caixa = document.createElement("div");
    caixa.className = "sino-caixa";
    caixa.innerHTML =
      '<button type="button" id="layout-sino" title="Alarmes">' + ICONES.sino +
      '<span class="sino-conta" id="layout-sino-conta" hidden>0</span></button>' +
      '<div class="sino-painel" id="layout-sino-painel" hidden>' +
        '<div class="sino-topo"><b>Alarmes</b>' +
        '<button type="button" id="layout-sino-lidos">marcar como lidos</button></div>' +
        '<ul id="layout-sino-lista"><li class="vazio">Nada por aqui.</li></ul>' +
      "</div>";
    acoes.appendChild(caixa);

    document.getElementById("layout-sino").addEventListener("click", function (ev) {
      ev.stopPropagation();
      sinoAberto = !sinoAberto;
      document.getElementById("layout-sino-painel").hidden = !sinoAberto;
      if (sinoAberto) atualizarSino();
    });
    document.getElementById("layout-sino-lidos").addEventListener("click", async function (ev) {
      ev.stopPropagation();
      try {
        await fetch("/api/alarmes/eventos/lidos", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: "{}",
        });
      } catch (e) { /* offline: a proxima atualizacao corrige */ }
      atualizarSino();
    });
    document.addEventListener("click", function () {
      if (!sinoAberto) return;
      sinoAberto = false;
      var p = document.getElementById("layout-sino-painel");
      if (p) p.hidden = true;
    });
    var painel = document.getElementById("layout-sino-painel");
    if (painel) painel.addEventListener("click", function (ev) { ev.stopPropagation(); });

    atualizarSino();
    setInterval(atualizarSino, 60000);
    ouvirWebSocket();
  }

  var ROTULO_CONDICAO = { maior: ">", menor: "<", igual: "=" };

  /** Numero curto o bastante para caber. 742000000 -> "742 M". Sem isto, um
   *  volume de reservatorio estourava a largura do painel do sininho e do
   *  card, empurrando o resto do layout. */
  function resumirNumero(v) {
    if (v == null || v === "") return "—";
    var n = Number(v);
    if (!isFinite(n)) return String(v);
    var abs = Math.abs(n);
    if (abs >= 1e12) return (n / 1e12).toFixed(2).replace(/\.?0+$/, "") + " T";
    if (abs >= 1e9)  return (n / 1e9).toFixed(2).replace(/\.?0+$/, "") + " G";
    if (abs >= 1e6)  return (n / 1e6).toFixed(2).replace(/\.?0+$/, "") + " M";
    if (abs >= 1e4)  return (n / 1e3).toFixed(1).replace(/\.?0+$/, "") + " k";
    // Abaixo de 10 mil o numero inteiro cabe e e mais informativo que "9,9 k".
    return String(Math.round(n * 100) / 100);
  }

  async function atualizarSino() {
    var conta = document.getElementById("layout-sino-conta");
    var lista = document.getElementById("layout-sino-lista");
    if (!conta || !lista) return;
    try {
      var j = await fetch("/api/alarmes/eventos?limite=20").then(function (r) { return r.json(); });
      conta.hidden = !j.nao_lidos;
      conta.textContent = j.nao_lidos > 99 ? "99+" : String(j.nao_lidos || 0);
      if (!j.eventos || !j.eventos.length) {
        lista.innerHTML = '<li class="vazio">Nenhum alarme registrado.</li>';
        return;
      }
      lista.innerHTML = j.eventos.map(function (e) {
        var quando = new Date(e.em).toLocaleString("pt-BR");
        var classe = e.estado === "disparado" ? "disparado" : "normal";
        // Dois tipos de evento no mesmo feed. O de VISAO nao tem regra por
        // tras -- nao ha chave, condicao nem limite para mostrar --, entao
        // tentar desenha-lo como alarme produzia "undefined > null".
        var detalhe;
        if (e.origem === "visao") {
          detalhe = escapar(e.dispositivo_nome || "") + " · visão computacional";
        } else {
          var onde = e.sub_id ? escapar(e.sub_id) + " · " : "";
          detalhe = escapar(e.dispositivo_nome || "") + " · " + onde +
            escapar(e.chave || "") + " " + (ROTULO_CONDICAO[e.condicao] || "") + " " +
            resumirNumero(e.limite) + " (leu " +
            (e.valor == null ? "—" : resumirNumero(e.valor)) + ")";
        }
        return '<li class="' + classe + (e.lido ? "" : " novo") +
          (e.origem === "visao" ? " visao" : "") + '">' +
          "<b>" + escapar(e.titulo) + "</b>" +
          '<span class="det">' + detalhe + "</span>" +
          '<span class="quando">' + quando +
          (e.estado === "normalizado" ? " · normalizou" : "") + "</span></li>";
      }).join("");
    } catch (e) { /* servidor fora: nao mexe no que ja esta na tela */ }
  }

  /** Um alarme novo precisa aparecer NA HORA, nao no proximo minuto. */
  function ouvirWebSocket() {
    try {
      var proto = location.protocol === "https:" ? "wss://" : "ws://";
      var ws = new WebSocket(proto + location.host + "/ws");
      ws.onmessage = function (ev) {
        try {
          var msg = JSON.parse(ev.data);
          // "detection" tambem: o servidor manda um "alarme" junto, mas se o
          // envio de um falhar o outro ainda atualiza o sininho.
          if (msg.type === "alarme" || msg.type === "detection") atualizarSino();
        } catch (e) { /* mensagem de outro tipo */ }
      };
      // Se cair, o setInterval de 60s continua cobrindo. Reconecta sem
      // pressa para nao criar tempestade de conexoes numa queda do servidor.
      ws.onclose = function () { setTimeout(ouvirWebSocket, 15000); };
    } catch (e) { /* sem websocket: fica so o intervalo */ }
  }

  // ==========================================================================
  // Vigia de sessao (inatividade)
  //
  // O servidor e quem decide: ele so aceita o cookie enquanto houve
  // atividade recente (ver SESSAO_INATIVIDADE_MIN em db.py). O que se faz
  // aqui e (a) avisar o operador ANTES de cair, para ele nao perder o que
  // estava fazendo, e (b) renovar o prazo quando ha atividade DE VERDADE.
  //
  // O ponto delicado: "atividade" e a pessoa, nao a pagina. O dashboard
  // conversa com o servidor a cada 3s sozinho; se isso renovasse a sessao,
  // uma tela esquecida na sala de controle ficaria logada indefinidamente.
  // Por isso a renovacao sai daqui, disparada por mouse/teclado/toque, e
  // nenhuma outra requisicao mexe no prazo.
  // ==========================================================================
  // Estes dois nao sao constantes: sao derivados do limite que o SERVIDOR
  // informa. Fixa-los quebra quando o limite e curto -- com 30 min de
  // inatividade, renovar no maximo a cada 45s e de sobra; com 2 min, a
  // primeira renovacao chegaria depois da sessao ja ter caido.
  var AVISO_ANTES_S = 60;      // com quanto tempo de sobra o aviso aparece
  var INTERVALO_MIN_S = 45;    // nao manda "estou aqui" mais que isso
  var prazoEm = 0;             // instante (ms) em que a sessao cai
  var ultimoEnvio = 0;
  var houveAtividade = false;
  var encerrando = false;      // logout/expiracao ja em curso
  var avisoEl = null;

  function iniciarVigiaDeSessao(conteudo) {
    avisoEl = document.createElement("div");
    avisoEl.className = "aviso-sessao";
    avisoEl.hidden = true;
    avisoEl.innerHTML =
      "<span>Sua sessão vai encerrar por inatividade em " +
      '<b id="layout-sessao-conta">--</b>.</span>' +
      '<button type="button" id="layout-sessao-continuar">Continuar conectado</button>';
    conteudo.insertBefore(avisoEl, conteudo.children[1] || null);
    avisoEl.querySelector("#layout-sessao-continuar")
           .addEventListener("click", function () { renovar(true); });

    ["pointerdown", "keydown", "wheel", "touchstart"].forEach(function (ev) {
      document.addEventListener(ev, marcarAtividade, { passive: true });
    });
    // Voltar para a aba nao e atividade, mas e a hora certa de reconferir:
    // o prazo pode ter corrido enquanto a aba estava escondida.
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) sincronizar();
    });

    interceptar401();
    sincronizar();
    setInterval(tique, 1000);
    setInterval(sincronizar, 60000);   // mantem varias abas em acordo
  }

  function marcarAtividade() {
    houveAtividade = true;
    // Se o aviso ja esta na tela, a sessao esta por um fio: mexer no mouse
    // TEM de renovar na hora, sem esperar a vez do intervalo.
    if (prazoEm && (prazoEm - Date.now()) / 1000 <= AVISO_ANTES_S)
      return renovar(true);
    // Fora disso, renova cedo mas com parcimonia: se o operador esta usando
    // a tela, ele nao deve nem chegar a ver o aviso.
    if ((Date.now() - ultimoEnvio) / 1000 >= INTERVALO_MIN_S) renovar(false);
  }

  async function renovar(forcado) {
    if (encerrando) return;
    if (!forcado && !houveAtividade) return;
    houveAtividade = false;
    ultimoEnvio = Date.now();
    try {
      var r = await fetch("/api/sessao/atividade", { method: "POST" });
      if (r.status === 401) return expirar();
      var j = await r.json();
      definirPrazo(j.restante_s);
    } catch (e) { /* rede oscilou: o tique continua com o prazo anterior */ }
  }

  async function sincronizar() {
    if (encerrando) return;
    try {
      var j = await fetch("/api/sessao").then(function (r) { return r.json(); });
      if (j.inatividade_s) {
        // Aviso com um terco do prazo de sobra (no maximo 1 min), e uma
        // renovacao a cada um quarto dele (no maximo 45s, no minimo 5s).
        AVISO_ANTES_S = Math.min(60, Math.max(10, j.inatividade_s / 3));
        INTERVALO_MIN_S = Math.min(45, Math.max(5, j.inatividade_s / 4));
      }
      definirPrazo(j.restante_s);
    } catch (e) { /* offline: segue com o que ja tem */ }
  }

  function definirPrazo(restanteS) {
    if (restanteS == null) return;
    prazoEm = Date.now() + restanteS * 1000;
  }

  function tique() {
    if (encerrando || !prazoEm) return;
    var faltam = Math.round((prazoEm - Date.now()) / 1000);
    if (faltam <= 0) return expirar();
    var mostrar = faltam <= AVISO_ANTES_S;
    avisoEl.hidden = !mostrar;
    if (mostrar) {
      document.getElementById("layout-sessao-conta").textContent =
        faltam >= 60 ? Math.ceil(faltam / 60) + " min" : faltam + " s";
    }
  }

  function expirar() {
    if (encerrando) return;
    encerrando = true;
    location.href = "/login?motivo=expirado&next=" +
                    encodeURIComponent(location.pathname + location.search);
  }

  /** Se QUALQUER requisicao da pagina voltar 401, a sessao ja caiu (outra
   *  aba saiu, o servidor reiniciou, o prazo estourou). Sem isto o dashboard
   *  ficaria numa tela viva batendo em 401 para sempre, sem dizer nada. */
  function interceptar401() {
    var original = global.fetch;
    if (!original) return;
    global.fetch = function () {
      return original.apply(this, arguments).then(function (r) {
        if (r.status === 401 && !encerrando) expirar();
        return r;   // a resposta segue intacta para quem chamou
      });
    };
  }

  async function carregarUsuario() {
    var eu = null;
    try {
      var r = await fetch("/api/usuarios/me");
      if (r.ok) eu = await r.json();
    } catch (e) { /* offline: deixa os campos como estao */ }
    if (!eu) return;

    var nome = eu.username || "—";
    document.getElementById("layout-usuario").textContent = nome;
    document.getElementById("layout-papel").textContent = eu.papel || "";
    document.getElementById("layout-avatar").textContent =
      nome.slice(0, 1).toUpperCase();

    if (eu.tema) {
      aplicarTema(eu.tema);
      gravar(CHAVE_TEMA, eu.tema);
    }
    global.dispatchEvent(new CustomEvent("layout:usuario", { detail: eu }));
  }

  global.Layout = {
    montar: montar,
    resumirNumero: resumirNumero,
    aplicarTema: aplicarTema,
    escapar: escapar,
    CHAVE_TEMA: CHAVE_TEMA,
  };
})(window);

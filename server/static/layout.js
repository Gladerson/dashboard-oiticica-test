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
    dispositivos: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="6" width="14" height="10" rx="2"/><path d="M16.5 10.5 21.5 8v8l-5-2.5z"/></svg>',
    config: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 9 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 9a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/></svg>',
    sair: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 17l5-5-5-5"/><path d="M20 12H9"/><path d="M9 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h3"/></svg>',
    menu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M4 7h16"/><path d="M4 12h16"/><path d="M4 17h16"/></svg>',
  };

  var ITENS = [
    { chave: "dashboard", href: "/", rotulo: "Painel", icone: ICONES.dashboard },
    { chave: "dispositivos", href: "/dispositivos", rotulo: "Dispositivos", icone: ICONES.dispositivos },
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
        '<div class="sigla">CV</div>' +
        '<div class="nome">Oiticica<small>Monitoramento de fissuras</small></div>' +
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
      try { await fetch("/api/logout", { method: "POST" }); } catch (e) { /* segue */ }
      location.href = "/login";
    });

    carregarUsuario();
    return { acoes: document.getElementById("layout-acoes") };
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
    aplicarTema: aplicarTema,
    escapar: escapar,
    CHAVE_TEMA: CHAVE_TEMA,
  };
})(window);

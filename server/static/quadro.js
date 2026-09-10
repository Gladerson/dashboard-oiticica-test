// ==========================================================================
// quadro.js - Arrastar e redimensionar caixas livremente dentro de uma area.
//
// Usado por duas telas com exigencias diferentes:
//
//   * "Telemetrias": os widgets ocupam onde o operador quiser, do tamanho que
//     ele quiser, com UMA regra -- nao podem se sobrepor;
//   * "Painel SHM": os graficos FLUTUAM por cima do modelo 3D, e ali a
//     sobreposicao e legitima (sao janelas, nao ladrilhos).
//
// A diferenca entra por `valido(retangulo)`: quem chama decide o que aceita.
// Enquanto o retangulo proposto for invalido a caixa fica marcada e o ultimo
// estado BOM e preservado -- soltar num lugar proibido devolve a caixa para
// onde ela cabia, em vez de gravar uma sobreposicao.
//
// Tudo em pixels relativos a area (offsetLeft/offsetTop), porque e assim que
// o CSS `position:absolute` posiciona -- converter para % so na hora de
// gravar mantem o layout util quando a janela muda de tamanho.
// ==========================================================================
(function (global) {
  "use strict";

  var PASSO = 8;     // ima suave: alinha as caixas sem engessar o tamanho

  function ajustar(v) { return Math.round(v / PASSO) * PASSO; }

  function limitar(v, min, max) { return Math.min(max, Math.max(min, v)); }

  /** Dois retangulos se tocam? Uma folga de 1 px evita que bordas encostadas
   *  (o caso mais comum depois do ima) contem como sobreposicao. */
  function colidem(a, b) {
    return a.x < b.x + b.w - 1 && a.x + a.w - 1 > b.x &&
           a.y < b.y + b.h - 1 && a.y + a.h - 1 > b.y;
  }

  function aplicar(el, r) {
    el.style.left = r.x + "px";
    el.style.top = r.y + "px";
    el.style.width = r.w + "px";
    el.style.height = r.h + "px";
  }

  function retanguloDe(el) {
    return { x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight };
  }

  /**
   * opcoes:
   *   area      elemento que delimita o movimento (position:relative)
   *   minW/minH tamanho minimo ao redimensionar
   *   valido    (retangulo) -> boolean; padrao: aceita tudo
   *   aoSoltar  (retangulo) -> void; chamado uma vez, no fim
   *   aoMover   (retangulo) -> void; a cada quadro (opcional)
   */
  function ligar(el, punho, opcoes, modo) {
    opcoes = opcoes || {};
    var valido = opcoes.valido || function () { return true; };

    punho.addEventListener("pointerdown", function (ev) {
      // Botao direito/meio nao arrasta, e clique num botao do cabecalho e
      // clique, nao arrasto.
      if (ev.button !== 0) return;
      if (modo === "mover" && ev.target.closest("button, input, select, a")) return;
      ev.preventDefault();
      ev.stopPropagation();

      var area = opcoes.area || el.parentElement;
      var r0 = retanguloDe(el);
      var x0 = ev.clientX, y0 = ev.clientY;
      var ultimoBom = { x: r0.x, y: r0.y, w: r0.w, h: r0.h };
      var maxL = Math.max(0, area.clientWidth);
      var maxA = Math.max(0, area.clientHeight);

      punho.setPointerCapture(ev.pointerId);
      el.classList.add(modo === "mover" ? "movendo" : "redimensionando");

      var mover = function (e) {
        var dx = e.clientX - x0, dy = e.clientY - y0, r;
        if (modo === "mover") {
          r = {
            x: limitar(ajustar(r0.x + dx), 0, Math.max(0, maxL - r0.w)),
            y: Math.max(0, ajustar(r0.y + dy)),
            w: r0.w, h: r0.h,
          };
        } else {
          r = {
            x: r0.x, y: r0.y,
            w: limitar(ajustar(r0.w + dx), opcoes.minW || 160, Math.max(opcoes.minW || 160, maxL - r0.x)),
            h: Math.max(opcoes.minH || 120, ajustar(r0.h + dy)),
          };
        }
        // Sempre desenha onde o ponteiro esta: prender a caixa no ultimo
        // lugar valido faz o arrasto parecer travado. O que o retangulo
        // invalido nao faz e virar o estado gravado.
        aplicar(el, r);
        var ok = valido(r);
        el.classList.toggle("invalido", !ok);
        if (ok) ultimoBom = r;
        if (opcoes.aoMover) opcoes.aoMover(ok ? r : ultimoBom, ok);
      };

      var soltar = function () {
        punho.removeEventListener("pointermove", mover);
        punho.removeEventListener("pointerup", soltar);
        punho.removeEventListener("pointercancel", soltar);
        el.classList.remove("movendo", "redimensionando", "invalido");
        aplicar(el, ultimoBom);
        if (opcoes.aoSoltar) opcoes.aoSoltar(ultimoBom);
      };

      punho.addEventListener("pointermove", mover);
      punho.addEventListener("pointerup", soltar);
      punho.addEventListener("pointercancel", soltar);
    });
  }

  global.Quadro = {
    PASSO: PASSO,
    colidem: colidem,
    aplicar: aplicar,
    retanguloDe: retanguloDe,
    ajustar: ajustar,
    ligarMover: function (el, punho, opcoes) { ligar(el, punho, opcoes, "mover"); },
    ligarRedimensionar: function (el, alca, opcoes) { ligar(el, alca, opcoes, "tamanho"); },
  };
})(window);

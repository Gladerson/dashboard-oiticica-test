#!/usr/bin/env python3
# ============================================================================
# conferir_deteccao.py - Diagnostico visual: onde o modelo esta acertando?
#
# Numeros de precisao e recall dizem QUANTO errou, nunca COMO. Este script
# desenha, na mesma imagem, o rotulo anotado (verde) e o que o modelo
# encontrou (vermelho). Tres padroes possiveis, com causas diferentes:
#
#   * caixas vermelhas em lugares plausiveis, mas deslocadas ou em escala
#     errada  -> problema de coordenadas (letterbox, w/h, eixos trocados)
#   * caixas vermelhas espalhadas sem relacao com a imagem
#     -> saida do modelo e ruido: quantizacao ou mapeamento HEF->ONNX
#   * caixas verdes em lugar nenhum parecido com a imagem
#     -> o split nao corresponde a estas imagens (dataset trocado)
# ============================================================================
import argparse
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

import config_borda as cfg
from calibrar_limiar_int8 import caixa_do_rotulo
from inferencia_hailo import DetectorHailo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--conf", type=float, default=0.05)
    ap.add_argument("--saida", default="conferencia")
    args = ap.parse_args()

    raiz = Path(args.dataset).expanduser()
    dir_img, dir_lbl = raiz / "images", raiz / "labels"
    saida = Path(args.saida)
    saida.mkdir(exist_ok=True)

    imagens = sorted([p for p in dir_img.iterdir()
                      if p.suffix.lower() in (".jpg", ".jpeg", ".png")])

    # --- Formato dos rotulos -----------------------------------------------
    formatos, classes, dims = Counter(), Counter(), Counter()
    for p in imagens:
        r = dir_lbl / (p.stem + ".txt")
        if not r.exists():
            formatos["SEM ARQUIVO DE ROTULO"] += 1
            continue
        linhas = [l for l in r.read_text().splitlines() if l.strip()]
        if not linhas:
            formatos["arquivo vazio (imagem de fundo)"] += 1
        for l in linhas:
            n = len(l.split())
            formatos[f"{n} colunas" + (" (bbox)" if n == 5 else
                                       " (poligono)" if n >= 7 else " (?)")] += 1
            classes[l.split()[0]] += 1

    print("=== Formato dos rotulos ===")
    for k, v in formatos.most_common():
        print(f"  {k}: {v}")
    print(f"  classes vistas: {dict(classes)}")

    for p in imagens[:40]:
        im = cv2.imread(str(p))
        if im is not None:
            dims[f"{im.shape[1]}x{im.shape[0]}"] += 1
    print(f"  dimensoes (40 primeiras): {dict(dims)}")
    print(f"  total de imagens: {len(imagens)}\n")

    # --- Amostra visual -----------------------------------------------------
    passo = max(1, len(imagens) // args.n)
    amostra = imagens[::passo][:args.n]
    print(f"=== Gerando {len(amostra)} imagens em {saida}/ ===")

    with DetectorHailo(cfg.HEF_PATH, cfg.HEAD_ONNX_PATH, cfg.MAPA_HEF_PARA_ONNX,
                       input_size=cfg.INPUT_SIZE, threads_cpu=cfg.THREADS_CPU,
                       class_names=cfg.CLASS_NAMES) as det:
        for p in amostra:
            bgr = cv2.imread(str(p))
            if bgr is None:
                continue
            h, w = bgr.shape[:2]
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            dets = det.infer(rgb, args.conf, cfg.IOU_THRESHOLD)

            out = bgr.copy()
            r = dir_lbl / (p.stem + ".txt")
            n_gt = 0
            if r.exists():
                for l in r.read_text().splitlines():
                    c = caixa_do_rotulo(l.strip(), w, h)
                    if c:
                        n_gt += 1
                        cv2.rectangle(out, (int(c[0]), int(c[1])),
                                      (int(c[2]), int(c[3])), (0, 255, 0), 2)
            for d in dets:
                x1, y1, x2, y2 = d["bbox"]
                cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 255), 2)
                cv2.putText(out, f"{d['conf']:.2f}", (x1, max(12, y1 - 4)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

            cv2.putText(out, f"verde=rotulo({n_gt})  vermelho=modelo({len(dets)})",
                        (6, h - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            cv2.imwrite(str(saida / f"{p.stem}.jpg"), out)
            confs = [round(d["conf"], 2) for d in dets]
            print(f"  {p.name}: {w}x{h} | rotulos={n_gt} | modelo={len(dets)} {confs}")

    print(f"\nAbra as imagens em {saida.resolve()}")


if __name__ == "__main__":
    main()

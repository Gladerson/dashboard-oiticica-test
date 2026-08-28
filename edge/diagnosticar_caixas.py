#!/usr/bin/env python3
# ============================================================================
# diagnosticar_caixas.py - Em que convencao vem a caixa do output0?
#
# Hipotese a testar: o tutorial assume [cx, cy, w, h, conf, cls, *32coef],
# mas a exportacao NMS-free do Ultralytics (saida fixa de 300 deteccoes)
# costuma entregar [x1, y1, x2, y2, conf, cls, *32coef] -- ja em pixels.
#
# Ler xyxy como se fosse cxcywh produz uma caixa centrada no canto superior
# esquerdo do objeto e com o tamanho do canto inferior direito. O resultado
# fica visualmente "perto" do alvo, com confianca alta, mas com IoU tipico
# entre 0.2 e 0.4 -- abaixo do corte de 0.5. Que e exatamente o que a
# calibracao mostrou: muitas deteccoes seguras e quase nenhum acerto.
#
# Este script mede as duas interpretacoes no mesmo conjunto e diz qual bate.
# ============================================================================
import argparse
from pathlib import Path

import cv2
import numpy as np

import config_borda as cfg
from calibrar_limiar_int8 import caixa_do_rotulo, iou
from inferencia_hailo import DetectorHailo, letterbox, nms


def caixas_cru(det, img_rgb, conf_thr):
    """Roda o modelo e devolve as 4 primeiras colunas SEM interpretar."""
    img_lb, r, pad = letterbox(img_rgb, det.input_size)
    lote = img_lb.astype(np.uint8)[np.newaxis, ...]
    saidas = det.pipeline.infer({det.input_name: lote})
    entradas = {}
    for nome_hef, nome_onnx in det.mapa.items():
        t = saidas[nome_hef]
        if t.ndim == 4:
            t = t.transpose(0, 3, 1, 2)
        entradas[nome_onnx] = t
    out0 = det.head.run(det.head_outputs, entradas)[0][0]
    sel = out0[:, 4] >= conf_thr
    return out0[sel, :4], out0[sel, 4], r, pad


def desfazer(caixa, r, pad):
    x1, y1, x2, y2 = caixa
    return [(x1 - pad[0]) / r, (y1 - pad[1]) / r,
            (x2 - pad[0]) / r, (y2 - pad[1]) / r]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--conf", type=float, default=0.30)
    args = ap.parse_args()

    raiz = Path(args.dataset).expanduser()
    dir_img, dir_lbl = raiz / "images", raiz / "labels"
    imagens = sorted([p for p in dir_img.iterdir()
                      if p.suffix.lower() in (".jpg", ".jpeg", ".png")])[:args.n]

    ious_cxcywh, ious_xyxy = [], []
    print(f"{'imagem':<28} {'conf':>5} {'IoU cxcywh':>11} {'IoU xyxy':>9}")
    print("-" * 58)

    with DetectorHailo(cfg.HEF_PATH, cfg.HEAD_ONNX_PATH, cfg.MAPA_HEF_PARA_ONNX,
                       input_size=cfg.INPUT_SIZE, threads_cpu=cfg.THREADS_CPU,
                       class_names=cfg.CLASS_NAMES) as det:
        for p in imagens:
            rot = dir_lbl / (p.stem + ".txt")
            if not rot.exists():
                continue
            bgr = cv2.imread(str(p))
            if bgr is None:
                continue
            h, w = bgr.shape[:2]
            gts = [c for c in (caixa_do_rotulo(l.strip(), w, h)
                               for l in rot.read_text().splitlines() if l.strip())
                   if c]
            if not gts:
                continue

            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            cruas, confs, r, pad = caixas_cru(det, rgb, args.conf)
            if len(cruas) == 0:
                continue

            i = int(np.argmax(confs))
            a, b, c, d = [float(v) for v in cruas[i]]

            como_cxcywh = desfazer([a - c / 2, b - d / 2, a + c / 2, b + d / 2], r, pad)
            como_xyxy = desfazer([a, b, c, d], r, pad)

            m1 = max(iou(como_cxcywh, g) for g in gts)
            m2 = max(iou(como_xyxy, g) for g in gts)
            ious_cxcywh.append(m1)
            ious_xyxy.append(m2)
            print(f"{p.name[:28]:<28} {confs[i]:5.2f} {m1:11.3f} {m2:9.3f}")

    if not ious_cxcywh:
        print("\nNenhuma imagem com rotulo E deteccao acima do limiar. "
              "Baixe --conf e tente de novo.")
        return

    def resumo(v):
        v = np.array(v)
        return (f"media={v.mean():.3f}  mediana={np.median(v):.3f}  "
                f"acima de 0.5={int((v >= 0.5).sum())}/{len(v)}")

    print("\n" + "=" * 58)
    print(f"Lendo como cxcywh (o que o codigo faz hoje):\n  {resumo(ious_cxcywh)}")
    print(f"Lendo como xyxy:\n  {resumo(ious_xyxy)}")
    print("=" * 58)
    if np.mean(ious_xyxy) > np.mean(ious_cxcywh) + 0.1:
        print("\nCONCLUSAO: o output0 vem em xyxy. O pos-processamento precisa")
        print("ser corrigido -- e a explicacao da calibracao ter dado ~0.")
    elif np.mean(ious_cxcywh) > np.mean(ious_xyxy) + 0.1:
        print("\nCONCLUSAO: cxcywh esta correto. O erro esta em outro lugar.")
    else:
        print("\nINCONCLUSIVO: as duas leituras dao IoU parecido. Olhe as")
        print("imagens do conferir_deteccao.py antes de mexer no codigo.")


if __name__ == "__main__":
    main()

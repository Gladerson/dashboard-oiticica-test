#!/usr/bin/env python3
# ============================================================================
# avaliar_pt_mesmo_protocolo.py - Linha de base em float32, para saber quanto
# a quantizacao INT8 realmente custou.
#
# O `yolo val` mede mAP em varios IoU e com o pos-processamento do proprio
# Ultralytics. O calibrar_limiar_int8.py mede VP/FP/FN por IoU de caixa em
# 0.5, com o pos-processamento manual do Pi. Sao numeros de escalas
# diferentes: comparar um com o outro nao diz nada sobre quantizacao.
#
# Este script roda o best.pt no MESMO split, com o MESMO criterio, para que a
# diferenca observada seja atribuivel so ao caminho ONNX -> corte -> INT8.
#
# Rode NO NOTEBOOK (nao precisa de Hailo):
#   cd ~/hailo_workspace_2 && source hailo_venv/bin/activate
#   python avaliar_pt_mesmo_protocolo.py --dataset ~/datasets/valid
# ============================================================================
import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

IOU_ACERTO = 0.5


def caixa_do_rotulo(linha, w, h):
    partes = linha.split()
    if len(partes) < 7:
        return None
    coords = np.array([float(x) for x in partes[1:]], dtype=float)
    if coords.size % 2:
        coords = coords[:-1]
    xs, ys = coords[0::2] * w, coords[1::2] * h
    return [xs.min(), ys.min(), xs.max(), ys.max()]


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    aa = (a[2] - a[0]) * (a[3] - a[1])
    ab = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (aa + ab - inter)


def avaliar(dets_por_img, gt_por_img, limiar):
    vp = fp = fn = 0
    for nome, gts in gt_por_img.items():
        dets = [d for d in dets_por_img.get(nome, []) if d["conf"] >= limiar]
        dets.sort(key=lambda d: -d["conf"])
        usados = set()
        for d in dets:
            melhor, melhor_i = -1, 0.0
            for i, g in enumerate(gts):
                if i in usados:
                    continue
                v = iou(d["bbox"], g)
                if v > melhor_i:
                    melhor, melhor_i = i, v
            if melhor_i >= IOU_ACERTO:
                usados.add(melhor)
                vp += 1
            else:
                fp += 1
        fn += len(gts) - len(usados)
    return vp, fp, fn


def metricas(vp, fp, fn):
    p = vp / (vp + fp) if (vp + fp) else 0.0
    r = vp / (vp + fn) if (vp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    f2 = 5 * p * r / (4 * p + r) if (4 * p + r) else 0.0
    return p, r, f1, f2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--modelo", default="best.pt")
    ap.add_argument("--min", type=float, default=0.02)
    ap.add_argument("--max", type=float, default=0.80)
    ap.add_argument("--passo", type=float, default=0.01)
    ap.add_argument("--iou", type=float, default=0.45)
    ap.add_argument("--saida", default="limiar_pt.json")
    args = ap.parse_args()

    raiz = Path(args.dataset).expanduser()
    dir_img, dir_lbl = raiz / "images", raiz / "labels"
    imagens = sorted([p for p in dir_img.iterdir()
                      if p.suffix.lower() in (".jpg", ".jpeg", ".png")])
    print(f"Split: {len(imagens)} imagens | modelo: {args.modelo}\n")

    modelo = YOLO(args.modelo)
    gt_por_img, dets_por_img = {}, {}

    for i, caminho in enumerate(imagens):
        bgr = cv2.imread(str(caminho))
        if bgr is None:
            continue
        h, w = bgr.shape[:2]

        rot = dir_lbl / (caminho.stem + ".txt")
        gts = []
        if rot.exists():
            for linha in rot.read_text().splitlines():
                c = caixa_do_rotulo(linha.strip(), w, h)
                if c:
                    gts.append(c)
        gt_por_img[caminho.name] = gts

        r = modelo.predict(bgr, conf=args.min, iou=args.iou,
                           imgsz=640, verbose=False)[0]
        saidas = []
        if r.boxes is not None and len(r.boxes):
            caixas = r.boxes.xyxy.cpu().numpy()
            confs = r.boxes.conf.cpu().numpy()
            saidas = [{"bbox": [float(v) for v in caixas[k]], "conf": float(confs[k])}
                      for k in range(len(confs))]
        dets_por_img[caminho.name] = saidas
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(imagens)}")

    total = sum(len(v) for v in gt_por_img.values())
    print(f"\nInstancias anotadas: {total}\n")
    print(f"{'limiar':>7} {'VP':>5} {'FP':>5} {'FN':>5} "
          f"{'precisao':>9} {'recall':>7} {'F1':>6} {'F2':>6}")
    print("-" * 60)

    linhas = []
    t = args.min
    while t <= args.max + 1e-9:
        vp, fp, fn = avaliar(dets_por_img, gt_por_img, t)
        p, rc, f1, f2 = metricas(vp, fp, fn)
        linhas.append({"limiar": round(t, 3), "vp": vp, "fp": fp, "fn": fn,
                       "precisao": round(p, 4), "recall": round(rc, 4),
                       "f1": round(f1, 4), "f2": round(f2, 4)})
        if abs((t / 0.05) - round(t / 0.05)) < 1e-6:
            print(f"{t:7.2f} {vp:5d} {fp:5d} {fn:5d} "
                  f"{p:9.3f} {rc:7.3f} {f1:6.3f} {f2:6.3f}")
        t += args.passo

    m1 = max(linhas, key=lambda x: x["f1"])
    m2 = max(linhas, key=lambda x: x["f2"])
    print("\n" + "=" * 60)
    print(f"Melhor F1: limiar={m1['limiar']}  P={m1['precisao']:.3f} "
          f"R={m1['recall']:.3f} F1={m1['f1']:.3f}")
    print(f"Melhor F2: limiar={m2['limiar']}  P={m2['precisao']:.3f} "
          f"R={m2['recall']:.3f} F2={m2['f2']:.3f}")
    print("=" * 60)
    print("\nCompare o melhor F1 daqui com o do limiar_int8.json: a diferenca")
    print("e a perda atribuivel ao caminho ONNX -> corte -> quantizacao INT8.")

    Path(args.saida).write_text(json.dumps(
        {"split": str(raiz), "modelo": args.modelo, "iou_acerto": IOU_ACERTO,
         "melhor_f1": m1, "melhor_f2": m2, "curva": linhas}, indent=2))
    print(f"Curva em {args.saida}")


if __name__ == "__main__":
    main()

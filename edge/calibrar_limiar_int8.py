#!/usr/bin/env python3
# ============================================================================
# calibrar_limiar_int8.py - Escolhe o limiar de confianca do modelo QUANTIZADO
#
# Por que isto existe: o 0.577 foi medido no best.pt, em float32. O que roda
# no Pi e outro modelo -- backbone INT8 com ruido de quantizacao. As
# confiancas saem sistematicamente mais baixas, e por isso o tutorial chutou
# 0.15. Chute nao e calibracao.
#
# Duas regras que este script respeita:
#
#   1. A medicao usa o split de VALIDACAO, nunca o de teste. Escolher um
#      hiperparametro olhando o teste contamina a avaliacao final -- o numero
#      que voce reportar na dissertacao deixa de ser imparcial.
#
#   2. O criterio de escolha e F2, nao F1. Em inspecao de barragem uma
#      rachadura perdida custa muito mais que um falso positivo que o
#      operador descarta em dois cliques. O F2 pesa recall 4x mais que
#      precisao. O F1 tambem e impresso, para comparacao.
#
# Rode NO RASPBERRY, porque so la existe o HEF de verdade:
#   cd ~/Projetos/dashboard_oiticica_test/edge && source venv/bin/activate
#   python calibrar_limiar_int8.py --dataset ~/datasets/valid
#
# Estrutura esperada (export YOLO do Roboflow):
#   valid/images/*.jpg
#   valid/labels/*.txt   (classe x1 y1 x2 y2 ... normalizados, poligono)
# ============================================================================
import argparse
import json
from pathlib import Path

import cv2
import numpy as np

import config_borda as cfg
from inferencia_hailo import DetectorHailo

IOU_ACERTO = 0.5   # IoU de caixa a partir do qual a deteccao conta como acerto


def caixa_do_rotulo(linha, w, h):
    """Converte uma linha YOLO-seg (poligono normalizado) na caixa que a
    envolve. Comparar caixa e suficiente para escolher o limiar: o que muda
    com a confianca e quantos objetos aparecem, nao o formato deles."""
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


def avaliar(deteccoes_por_img, gt_por_img, limiar):
    """VP/FP/FN com correspondencia gulosa por confianca decrescente."""
    vp = fp = fn = 0
    for nome, gts in gt_por_img.items():
        dets = [d for d in deteccoes_por_img.get(nome, []) if d["conf"] >= limiar]
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
    ap.add_argument("--dataset", required=True,
                    help="pasta do split de VALIDACAO (com images/ e labels/)")
    ap.add_argument("--min", type=float, default=0.02)
    ap.add_argument("--max", type=float, default=0.80)
    ap.add_argument("--passo", type=float, default=0.01)
    ap.add_argument("--saida", default="limiar_int8.json")
    args = ap.parse_args()

    raiz = Path(args.dataset).expanduser()
    dir_img, dir_lbl = raiz / "images", raiz / "labels"
    if not dir_img.is_dir() or not dir_lbl.is_dir():
        raise SystemExit(f"Esperava {dir_img} e {dir_lbl}.")

    imagens = sorted([p for p in dir_img.iterdir()
                      if p.suffix.lower() in (".jpg", ".jpeg", ".png")])
    if not imagens:
        raise SystemExit("Nenhuma imagem no split.")
    print(f"Split de validacao: {len(imagens)} imagens")
    print("Inferindo uma vez com limiar minimo; depois so refiltramos.\n")

    gt_por_img, dets_por_img = {}, {}

    with DetectorHailo(cfg.HEF_PATH, cfg.HEAD_ONNX_PATH, cfg.MAPA_HEF_PARA_ONNX,
                       input_size=cfg.INPUT_SIZE, threads_cpu=cfg.THREADS_CPU,
                       class_names=cfg.CLASS_NAMES) as det:
        for i, caminho in enumerate(imagens):
            bgr = cv2.imread(str(caminho))
            if bgr is None:
                continue
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            h, w = rgb.shape[:2]

            rotulo = dir_lbl / (caminho.stem + ".txt")
            gts = []
            if rotulo.exists():
                for linha in rotulo.read_text().splitlines():
                    caixa = caixa_do_rotulo(linha.strip(), w, h)
                    if caixa:
                        gts.append(caixa)
            gt_por_img[caminho.name] = gts

            # Infere UMA vez no limiar mais baixo da varredura. Rodar o
            # modelo 78 vezes por imagem para varrer limiares seria absurdo:
            # o limiar so filtra a saida, nao muda a inferencia.
            dets_por_img[caminho.name] = [
                {"bbox": d["bbox"], "conf": d["conf"]}
                for d in det.infer(rgb, args.min, cfg.IOU_THRESHOLD)
            ]
            if (i + 1) % 25 == 0:
                print(f"  {i+1}/{len(imagens)}")

    total_gt = sum(len(v) for v in gt_por_img.values())
    print(f"\nInstancias anotadas: {total_gt}\n")
    print(f"{'limiar':>7} {'VP':>5} {'FP':>5} {'FN':>5} "
          f"{'precisao':>9} {'recall':>7} {'F1':>6} {'F2':>6}")
    print("-" * 60)

    linhas = []
    limiar = args.min
    while limiar <= args.max + 1e-9:
        vp, fp, fn = avaliar(dets_por_img, gt_por_img, limiar)
        p, r, f1, f2 = metricas(vp, fp, fn)
        linhas.append({"limiar": round(limiar, 3), "vp": vp, "fp": fp, "fn": fn,
                       "precisao": round(p, 4), "recall": round(r, 4),
                       "f1": round(f1, 4), "f2": round(f2, 4)})
        if abs((limiar / 0.05) - round(limiar / 0.05)) < 1e-6:
            print(f"{limiar:7.2f} {vp:5d} {fp:5d} {fn:5d} "
                  f"{p:9.3f} {r:7.3f} {f1:6.3f} {f2:6.3f}")
        limiar += args.passo

    melhor_f1 = max(linhas, key=lambda x: x["f1"])
    melhor_f2 = max(linhas, key=lambda x: x["f2"])

    print("\n" + "=" * 60)
    print(f"Melhor F1: limiar={melhor_f1['limiar']}  "
          f"P={melhor_f1['precisao']:.3f} R={melhor_f1['recall']:.3f} "
          f"F1={melhor_f1['f1']:.3f}")
    print(f"Melhor F2: limiar={melhor_f2['limiar']}  "
          f"P={melhor_f2['precisao']:.3f} R={melhor_f2['recall']:.3f} "
          f"F2={melhor_f2['f2']:.3f}   <-- use este")
    print("=" * 60)
    print(f"\nColoque no edge/.env:  CONF_THRESHOLD={melhor_f2['limiar']}")
    print("Ou mande em runtime, sem reiniciar o Pi:")
    print(f"  curl -X POST http://192.168.0.177:8001/api/inferencia \\")
    print(f"       -H 'Content-Type: application/json' \\")
    print(f"       -d '{{\"conf\": {melhor_f2['limiar']}}}'")

    Path(args.saida).write_text(json.dumps(
        {"split": str(raiz), "iou_acerto": IOU_ACERTO,
         "melhor_f1": melhor_f1, "melhor_f2": melhor_f2, "curva": linhas},
        indent=2))
    print(f"\nCurva completa em {args.saida} (serve de figura na dissertacao).")


if __name__ == "__main__":
    main()

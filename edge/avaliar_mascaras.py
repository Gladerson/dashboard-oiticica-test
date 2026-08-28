#!/usr/bin/env python3
# ============================================================================
# avaliar_mascaras.py - Qualidade da MASCARA, nao da caixa.
#
# Por que este script existe: a comparacao anterior usou IoU de caixa em 0.5
# e concluiu que a perda INT8 nao e mensuravel. Isso vale para "achou a
# fissura?", e nao diz nada sobre "desenhou o contorno certo?". Como o
# sistema usa a mascara para estimar area -- e no futuro largura da fissura,
# que e o que a legislacao de seguranca de barragens pede -- e perfeitamente
# possivel que os contornos estejam degradados com as caixas intactas.
#
# Decisao metodologica: o pareamento entre predicao e rotulo e feito por IoU
# de CAIXA (>= 0.5), e a mascara e medida so depois, sobre os pares ja
# formados. Parear pela propria mascara enviesaria o resultado -- as
# predicoes de contorno ruim seriam descartadas do pareamento e sumiriam da
# media, fazendo qualquer modelo parecer bom.
#
# O MESMO codigo de metrica roda nos dois caminhos; so o motor de inferencia
# muda. Os imports sao tardios, entao o arquivo funciona no Pi (que nao tem
# ultralytics) e no notebook (que nao tem hailo_platform).
#
# No Raspberry:
#   python avaliar_mascaras.py --motor hailo --dataset ~/Projetos/datasets/valid \
#          --conf 0.45 --saida mascaras_int8.json
#
# No notebook:
#   python avaliar_mascaras.py --motor pt --modelo best.pt --dataset .../valid \
#          --conf 0.39 --saida mascaras_pt.json
#
# Comparar (em qualquer maquina):
#   python avaliar_mascaras.py --comparar mascaras_pt.json mascaras_int8.json
# ============================================================================
import argparse
import json
from pathlib import Path

import cv2
import numpy as np

IOU_CAIXA_PAREAMENTO = 0.5


# ----------------------------------------------------------------------------
# Rotulos
# ----------------------------------------------------------------------------
def poligono_do_rotulo(linha, w, h):
    """Linha YOLO-seg -> array (N,2) de pixels."""
    partes = linha.split()
    if len(partes) < 7:
        return None
    coords = np.array([float(x) for x in partes[1:]], dtype=float)
    if coords.size % 2:
        coords = coords[:-1]
    pts = np.stack([coords[0::2] * w, coords[1::2] * h], axis=1)
    return pts if len(pts) >= 3 else None


def rasterizar(pts, w, h):
    m = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(m, [np.round(pts).astype(np.int32)], 1)
    return m


def caixa_de(mascara):
    ys, xs = np.nonzero(mascara)
    if xs.size == 0:
        return None
    return [float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)]


# ----------------------------------------------------------------------------
# Metricas
# ----------------------------------------------------------------------------
def iou_caixa(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    aa = (a[2] - a[0]) * (a[3] - a[1])
    ab = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (aa + ab - inter)


def metricas_mascara(pred, gt):
    inter = int(np.logical_and(pred, gt).sum())
    ap, ag = int(pred.sum()), int(gt.sum())
    uniao = ap + ag - inter
    return {
        "iou": inter / uniao if uniao else 0.0,
        "dice": 2 * inter / (ap + ag) if (ap + ag) else 0.0,
        # Fracao do rotulo que a predicao cobriu (recall de pixel) e fracao da
        # predicao que caiu dentro do rotulo (precisao de pixel). Separadas
        # porque contam historias diferentes: mascara fina demais derruba a
        # primeira, mascara inchada derruba a segunda.
        "recall_px": inter / ag if ag else 0.0,
        "precisao_px": inter / ap if ap else 0.0,
        "razao_area": ap / ag if ag else 0.0,
        "area_gt": ag,
        "area_pred": ap,
    }


# ----------------------------------------------------------------------------
# Motores
# ----------------------------------------------------------------------------
class MotorHailo:
    nome = "INT8 (HEF + cabecalho ONNX)"

    def __init__(self, conf, iou_nms):
        import config_borda as cfg
        from inferencia_hailo import DetectorHailo
        self.cfg, self.conf, self.iou_nms = cfg, conf, iou_nms
        self._det = DetectorHailo(cfg.HEF_PATH, cfg.HEAD_ONNX_PATH,
                                  cfg.MAPA_HEF_PARA_ONNX,
                                  input_size=cfg.INPUT_SIZE,
                                  threads_cpu=cfg.THREADS_CPU,
                                  class_names=cfg.CLASS_NAMES)
        self.rotulo_modelo = Path(cfg.HEF_PATH).name

    def __enter__(self):
        self._det.__enter__()
        return self

    def __exit__(self, *e):
        return self._det.__exit__(*e)

    def prever(self, bgr):
        h, w = bgr.shape[:2]
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        saida = []
        for d in self._det.infer(rgb, self.conf, self.iou_nms):
            x1, y1, x2, y2 = d["bbox"]
            cheia = np.zeros((h, w), dtype=np.uint8)
            m = d["mascara"]
            if m.shape == (y2 - y1, x2 - x1):
                cheia[y1:y2, x1:x2] = m
            saida.append({"conf": d["conf"],
                          "caixa": [float(x1), float(y1), float(x2), float(y2)],
                          "mascara": cheia})
        return saida


class MotorPT:
    nome = "float32 (.pt via ultralytics)"

    def __init__(self, caminho, conf, iou_nms):
        from ultralytics import YOLO
        self.modelo = YOLO(caminho)
        self.conf, self.iou_nms = conf, iou_nms
        self.rotulo_modelo = Path(caminho).name

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def prever(self, bgr):
        h, w = bgr.shape[:2]
        r = self.modelo.predict(bgr, conf=self.conf, iou=self.iou_nms,
                                imgsz=640, verbose=False, retina_masks=True)[0]
        if r.boxes is None or len(r.boxes) == 0 or r.masks is None:
            return []
        caixas = r.boxes.xyxy.cpu().numpy()
        confs = r.boxes.conf.cpu().numpy()
        saida = []
        for k, pts in enumerate(r.masks.xy):
            # Rasterizamos o poligono do jeito EXATO com que o rotulo e
            # rasterizado. Usar r.masks.data (mapa ja amostrado pelo
            # ultralytics) introduziria uma diferenca de reamostragem que
            # nada tem a ver com a quantizacao.
            if pts is None or len(pts) < 3:
                continue
            saida.append({"conf": float(confs[k]),
                          "caixa": [float(v) for v in caixas[k]],
                          "mascara": rasterizar(np.asarray(pts, dtype=float), w, h)})
        return saida


# ----------------------------------------------------------------------------
def avaliar(motor, raiz, limite=None):
    dir_img, dir_lbl = raiz / "images", raiz / "labels"
    imagens = sorted([p for p in dir_img.iterdir()
                      if p.suffix.lower() in (".jpg", ".jpeg", ".png")])
    if limite:
        imagens = imagens[:limite]

    pares, n_gt, n_pred = [], 0, 0

    with motor:
        for i, caminho in enumerate(imagens):
            bgr = cv2.imread(str(caminho))
            if bgr is None:
                continue
            h, w = bgr.shape[:2]

            rot = dir_lbl / (caminho.stem + ".txt")
            gts = []
            if rot.exists():
                for linha in rot.read_text().splitlines():
                    if not linha.strip():
                        continue
                    pts = poligono_do_rotulo(linha.strip(), w, h)
                    if pts is None:
                        continue
                    m = rasterizar(pts, w, h)
                    c = caixa_de(m)
                    if c:
                        gts.append({"mascara": m, "caixa": c})
            n_gt += len(gts)

            preds = motor.prever(bgr)
            n_pred += len(preds)
            if not gts or not preds:
                continue

            preds.sort(key=lambda d: -d["conf"])
            usados = set()
            for d in preds:
                melhor, melhor_i = -1, 0.0
                for k, g in enumerate(gts):
                    if k in usados:
                        continue
                    v = iou_caixa(d["caixa"], g["caixa"])
                    if v > melhor_i:
                        melhor, melhor_i = k, v
                if melhor_i < IOU_CAIXA_PAREAMENTO:
                    continue
                usados.add(melhor)
                r = metricas_mascara(d["mascara"], gts[melhor]["mascara"])
                r.update({"imagem": caminho.name, "conf": d["conf"],
                          "iou_caixa": melhor_i})
                pares.append(r)

            if (i + 1) % 25 == 0:
                print(f"  {i+1}/{len(imagens)}")

    return pares, n_gt, n_pred


def resumir(pares):
    def est(chave):
        v = np.array([p[chave] for p in pares], dtype=float)
        return {"media": float(v.mean()), "mediana": float(np.median(v)),
                "p10": float(np.percentile(v, 10)),
                "p90": float(np.percentile(v, 90))}
    return {c: est(c) for c in
            ("iou", "dice", "recall_px", "precisao_px", "razao_area", "iou_caixa")}


def imprimir(titulo, res, pares, n_gt, n_pred):
    print(f"\n=== {titulo} ===")
    print(f"pares avaliados: {len(pares)} | instancias anotadas: {n_gt} "
          f"| predicoes: {n_pred}")
    print(f"{'metrica':<14} {'media':>8} {'mediana':>8} {'p10':>8} {'p90':>8}")
    print("-" * 50)
    for c in ("iou", "dice", "recall_px", "precisao_px", "razao_area", "iou_caixa"):
        d = res[c]
        print(f"{c:<14} {d['media']:8.3f} {d['mediana']:8.3f} "
              f"{d['p10']:8.3f} {d['p90']:8.3f}")


def comparar(a_path, b_path):
    a = json.loads(Path(a_path).read_text())
    b = json.loads(Path(b_path).read_text())
    print(f"\nA = {a['titulo']}  (limiar {a['conf']}, {a['n_pares']} pares)")
    print(f"B = {b['titulo']}  (limiar {b['conf']}, {b['n_pares']} pares)\n")
    print(f"{'metrica':<14} {'A':>9} {'B':>9} {'B - A':>9}")
    print("-" * 45)
    for c in ("iou", "dice", "recall_px", "precisao_px", "razao_area", "iou_caixa"):
        va, vb = a["resumo"][c]["media"], b["resumo"][c]["media"]
        print(f"{c:<14} {va:9.3f} {vb:9.3f} {vb - va:+9.3f}")

    d_iou = b["resumo"]["iou"]["media"] - a["resumo"]["iou"]["media"]
    n = min(a["n_pares"], b["n_pares"])
    # Erro padrao grosseiro da media, com o desvio aproximado por (p90-p10)/2.56
    sd = (a["resumo"]["iou"]["p90"] - a["resumo"]["iou"]["p10"]) / 2.56
    ep = sd / max(1, n) ** 0.5
    print(f"\nDiferenca de IoU de mascara: {d_iou:+.3f}")
    print(f"Erro padrao aproximado da media: +/- {ep:.3f}")
    if abs(d_iou) < 2 * ep:
        print("-> Dentro do ruido: nao da para afirmar diferenca de qualidade.")
    else:
        pior = "B" if d_iou < 0 else "A"
        print(f"-> Diferenca acima do ruido; {pior} tem contorno pior.")

    ra, rb = a["resumo"]["razao_area"]["media"], b["resumo"]["razao_area"]["media"]
    print(f"\nrazao_area = area predita / area anotada.")
    print(f"  A={ra:.3f}  B={rb:.3f}")
    if rb < ra - 0.05:
        print("  B desenha contornos mais FINOS -- tende a subestimar a fissura.")
    elif rb > ra + 0.05:
        print("  B desenha contornos mais GROSSOS -- tende a superestimar.")
    else:
        print("  Sem vies sistematico de espessura entre os dois.")


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--comparar", nargs=2, metavar=("A.json", "B.json"))
    ap.add_argument("--motor", choices=["hailo", "pt"])
    ap.add_argument("--modelo", default="best.pt")
    ap.add_argument("--dataset")
    ap.add_argument("--conf", type=float, default=0.45)
    ap.add_argument("--iou-nms", type=float, default=0.45)
    ap.add_argument("--limite", type=int, default=0)
    ap.add_argument("--saida", default="mascaras.json")
    args = ap.parse_args()

    if args.comparar:
        comparar(*args.comparar)
        return
    if not args.motor or not args.dataset:
        ap.error("informe --motor e --dataset (ou use --comparar)")

    raiz = Path(args.dataset).expanduser()
    motor = (MotorHailo(args.conf, args.iou_nms) if args.motor == "hailo"
             else MotorPT(args.modelo, args.conf, args.iou_nms))

    print(f"Motor: {motor.nome} | limiar {args.conf}")
    pares, n_gt, n_pred = avaliar(motor, raiz, args.limite or None)
    if not pares:
        print("\nNenhum par formado. Verifique o limiar e o dataset.")
        return

    res = resumir(pares)
    imprimir(motor.nome, res, pares, n_gt, n_pred)

    Path(args.saida).write_text(json.dumps({
        "titulo": motor.nome, "modelo": motor.rotulo_modelo,
        "split": str(raiz), "conf": args.conf,
        "iou_caixa_pareamento": IOU_CAIXA_PAREAMENTO,
        "n_pares": len(pares), "n_gt": n_gt, "n_pred": n_pred,
        "resumo": res, "pares": pares,
    }, indent=2))
    print(f"\nDetalhe por instancia em {args.saida}")


if __name__ == "__main__":
    main()

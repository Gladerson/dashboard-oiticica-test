# ============================================================================
# inferencia_hailo.py - Inferencia hibrida HEF (NPU) + ONNX (CPU)
#
# Diferenca importante em relacao ao detector_rachaduras.py do tutorial:
# la o InferVStreams e o network_group.activate() eram abertos DENTRO da
# funcao de inferencia, ou seja, a cada imagem. Isso custa dezenas de ms de
# reconfiguracao por frame -- tolerarvel no Gradio (uma foto por vez),
# proibitivo num laco de video continuo.
#
# Aqui o pipeline e aberto UMA vez no __enter__ e fica vivo. So o infer()
# roda por frame.
# ============================================================================
import os
import time
from contextlib import ExitStack

import cv2
import numpy as np
import onnxruntime as ort
from hailo_platform import (
    HEF,
    ConfigureParams,
    FormatType,
    HailoStreamInterface,
    InferVStreams,
    InputVStreamParams,
    OutputVStreamParams,
    VDevice,
)


def letterbox(img_rgb, new_size=640):
    """Redimensiona com padding cinza, preservando proporcao."""
    h, w = img_rgb.shape[:2]
    r = min(new_size / h, new_size / w)
    new_w, new_h = int(round(w * r)), int(round(h * r))
    img_r = cv2.resize(img_rgb, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    pad_w, pad_h = new_size - new_w, new_size - new_h
    top, left = pad_h // 2, pad_w // 2
    img_p = cv2.copyMakeBorder(
        img_r, top, pad_h - top, left, pad_w - left,
        cv2.BORDER_CONSTANT, value=(114, 114, 114),
    )
    return img_p, r, (left, top)


def nms(boxes, scores, iou_threshold):
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break
        ix1 = np.maximum(x1[i], x1[order[1:]])
        iy1 = np.maximum(y1[i], y1[order[1:]])
        ix2 = np.minimum(x2[i], x2[order[1:]])
        iy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, ix2 - ix1) * np.maximum(0, iy2 - iy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        order = order[1:][iou <= iou_threshold]
    return keep


class DetectorHailo:
    """Backbone na NPU + cabecalho no CPU, com o pipeline mantido aberto."""

    def __init__(self, hef_path, head_onnx_path, mapa_hef_para_onnx,
                 input_size=640, threads_cpu=4, class_names=("rachadura",)):
        for caminho, nome in [(hef_path, "HEF"), (head_onnx_path, "cabecalho ONNX")]:
            if not os.path.exists(caminho):
                raise FileNotFoundError(f"{nome} nao encontrado: {caminho}")

        self.input_size = int(input_size)
        self.class_names = list(class_names)
        self.mapa = dict(mapa_hef_para_onnx)
        self._stack = ExitStack()

        self.hef = HEF(hef_path)
        self.target = VDevice()
        cfg = ConfigureParams.create_from_hef(
            hef=self.hef, interface=HailoStreamInterface.PCIe
        )
        self.network_group = self.target.configure(self.hef, cfg)[0]
        self.ng_params = self.network_group.create_params()

        info_in = self.hef.get_input_vstream_infos()[0]
        self.input_name = info_in.name
        self.saidas_hef = [o.name for o in self.hef.get_output_vstream_infos()]

        self.in_params = InputVStreamParams.make(
            self.network_group, format_type=FormatType.UINT8
        )
        self.out_params = OutputVStreamParams.make(
            self.network_group, format_type=FormatType.FLOAT32
        )

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(threads_cpu)
        self.head = ort.InferenceSession(
            head_onnx_path, sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.head_outputs = [o.name for o in self.head.get_outputs()]
        entradas_head = {i.name for i in self.head.get_inputs()}

        # Falha cedo e com mensagem util, em vez de um InvalidArgument opaco
        # no meio do laco de video.
        faltando = set(self.mapa.values()) - entradas_head
        sobrando = set(self.mapa.keys()) - set(self.saidas_hef)
        if faltando or sobrando:
            raise RuntimeError(
                "MAPA_HEF_PARA_ONNX incompativel com os arquivos carregados.\n"
                f"  saidas reais do HEF : {self.saidas_hef}\n"
                f"  entradas reais ONNX : {sorted(entradas_head)}\n"
                f"  nomes ONNX inexistentes: {sorted(faltando)}\n"
                f"  nomes HEF inexistentes : {sorted(sobrando)}\n"
                "Rode o diagnostico do Passo 4 e corrija o mapa no .env."
            )

        self.ultimo_ms = {"npu": 0.0, "cpu": 0.0, "total": 0.0}

    # -- ciclo de vida -------------------------------------------------------
    def __enter__(self):
        self.pipeline = self._stack.enter_context(
            InferVStreams(self.network_group, self.in_params, self.out_params)
        )
        self._stack.enter_context(self.network_group.activate(self.ng_params))
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False

    # -- inferencia ----------------------------------------------------------
    def infer(self, img_rgb, conf_thr, iou_thr):
        t0 = time.time()
        img_lb, r, pad = letterbox(img_rgb, self.input_size)
        lote = img_lb.astype(np.uint8)[np.newaxis, ...]

        t1 = time.time()
        saidas_hailo = self.pipeline.infer({self.input_name: lote})
        t2 = time.time()

        entradas = {}
        for nome_hef, nome_onnx in self.mapa.items():
            t = saidas_hailo[nome_hef]
            if t.ndim == 4:
                t = t.transpose(0, 3, 1, 2)  # NHWC -> NCHW
            entradas[nome_onnx] = t
        saidas_head = self.head.run(self.head_outputs, entradas)
        t3 = time.time()

        dets = self._pos(saidas_head, img_rgb.shape, r, pad, conf_thr, iou_thr)

        self.ultimo_ms = {
            "npu": round((t2 - t1) * 1000, 1),
            "cpu": round((t3 - t2) * 1000, 1),
            "total": round((time.time() - t0) * 1000, 1),
        }
        return dets

    def _pos(self, saidas, shape_orig, r, pad, conf_thr, iou_thr):
        """output0: (1,300,38) [cx,cy,w,h,conf,cls,*32coef] | output1: (1,32,160,160)"""
        out0 = saidas[0][0]
        protos = saidas[1][0]
        orig_h, orig_w = shape_orig[:2]
        pad_x, pad_y = pad
        ph, pw = protos.shape[1], protos.shape[2]

        confs = out0[:, 4]
        sel = confs >= conf_thr
        if not sel.any():
            return []

        # output0 ja vem em xyxy, em pixels do quadro com letterbox -- nao em
        # cxcywh como o tutorial documentava. Medido no split de validacao:
        # tratar como cxcywh dava IoU medio 0.14 contra o rotulo (0 acertos em
        # 15); tratar como xyxy da 0.90 (15 em 15).
        xyxy = out0[sel, :4].astype(np.float32).copy()
        confs = confs[sel]
        classes = out0[sel, 5].astype(int)
        coefs = out0[sel, 6:]

        keep = nms(xyxy, confs, iou_thr)
        if not keep:
            return []
        xyxy, confs, classes, coefs = xyxy[keep], confs[keep], classes[keep], coefs[keep]

        dets = []
        for i in range(len(xyxy)):
            x1, y1, x2, y2 = xyxy[i]
            x1o = int(max(0, min((x1 - pad_x) / r, orig_w)))
            y1o = int(max(0, min((y1 - pad_y) / r, orig_h)))
            x2o = int(max(0, min((x2 - pad_x) / r, orig_w)))
            y2o = int(max(0, min((y2 - pad_y) / r, orig_h)))
            if x2o <= x1o or y2o <= y1o:
                continue

            m = (coefs[i] @ protos.reshape(protos.shape[0], -1)).reshape(ph, pw)
            m = 1 / (1 + np.exp(-m))
            px1 = int(max(0, x1 * pw / self.input_size))
            py1 = int(max(0, y1 * ph / self.input_size))
            px2 = int(min(pw, x2 * pw / self.input_size))
            py2 = int(min(ph, y2 * ph / self.input_size))
            recorte = m[py1:py2, px1:px2]
            if recorte.size == 0:
                mascara = np.zeros((y2o - y1o, x2o - x1o), dtype=np.uint8)
            else:
                mascara = cv2.resize(
                    recorte, (x2o - x1o, y2o - y1o), interpolation=cv2.INTER_LINEAR
                )
                mascara = (mascara > 0.5).astype(np.uint8)

            dets.append({
                "bbox": [x1o, y1o, x2o, y2o],
                "conf": float(confs[i]),
                "cls": int(classes[i]),
                "mascara": mascara,
            })
        return dets

    # -- desenho (usado so quando o operador pede o stream anotado) ----------
    def desenhar(self, img_rgb, dets, cor=(255, 60, 60)):
        out = img_rgb.copy()
        overlay = out.copy()
        for d in dets:
            x1, y1, x2, y2 = d["bbox"]
            roi = overlay[y1:y2, x1:x2]
            m = d["mascara"]
            if roi.size and m.shape == roi.shape[:2]:
                roi[m == 1] = cor
        out = cv2.addWeighted(overlay, 0.4, out, 0.6, 0)
        for d in dets:
            x1, y1, x2, y2 = d["bbox"]
            nome = self.class_names[d["cls"]] if d["cls"] < len(self.class_names) else "obj"
            rotulo = f"{nome} {d['conf']:.2f}"
            cv2.rectangle(out, (x1, y1), (x2, y2), cor, 2)
            (tw, th), _ = cv2.getTextSize(rotulo, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(out, (x1, max(0, y1 - th - 6)), (x1 + tw + 4, y1), cor, -1)
            cv2.putText(out, rotulo, (x1 + 2, max(10, y1 - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        return out


def contorno_normalizado(mascara, bbox, largura, altura, max_pontos=64):
    """Contorno externo da mascara em coordenadas 0..1 do frame inteiro.

    E isto que viaja pela rede no lugar da imagem, E e o que o dashboard
    desenha por cima da foto em "Ver mascara" (nao existe uma segunda
    imagem com a mascara desenhada -- so este contorno). eps baixo +
    max_pontos generoso para nao perder fidelidade visivel; mesmo em 64
    pontos isso continua sendo ~1 KB, nada perto do custo de subir uma
    segunda imagem.
    """
    if mascara is None or mascara.size == 0:
        return []
    contornos, _ = cv2.findContours(
        mascara.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contornos:
        return []
    maior = max(contornos, key=cv2.contourArea)
    perimetro = cv2.arcLength(maior, True)

    # Quando nao cabe em max_pontos, AUMENTA o eps e simplifica de novo, em
    # vez de descartar vertices uniformemente. O approxPolyDP (Douglas-
    # Peucker) escolhe justamente os vertices que sustentam a forma; jogar
    # fora 2 de cada 3 "na regua" descarta esses e mantem outros
    # arbitrarios, deformando o contorno (cantos cortados, e ate
    # auto-intersecao). Simplificar de novo com eps maior devolve um
    # poligono menor que continua sendo uma simplificacao COERENTE.
    eps = 0.003 * perimetro
    for _ in range(24):
        aprox = cv2.approxPolyDP(maior, eps, True).reshape(-1, 2)
        if len(aprox) <= max_pontos:
            break
        eps *= 1.35
    else:
        aprox = aprox[:max_pontos]

    x0, y0 = bbox[0], bbox[1]
    return [[round((x0 + px) / largura, 4), round((y0 + py) / altura, 4)]
            for px, py in aprox]

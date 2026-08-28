# ============================================================================
# quantizar_compilar_v2.py - Passo 9 refeito com optimization_level=2
#
# Diferenca para a versao anterior:
#
#   nivel 1 (o que usamos): so PTQ estatistica. Rapido (10-30 min), mas a
#            perda de precisao ao ir para INT8 fica toda por conta do modelo.
#
#   nivel 2 (este):         equalizacao de pesos entre camadas + fine-tuning
#            da quantizacao usando as SUAS imagens de calibracao. Recupera
#            tipicamente 5-10% da precisao perdida. Custa horas, e usa GPU se
#            houver uma (sem GPU, prepare-se para deixar rodando de noite).
#
# Rode no notebook, dentro do hailo_venv:
#   cd ~/hailo_workspace_2 && source hailo_venv/bin/activate
#   python quantizar_compilar_v2.py 2>&1 | tee compilacao_v2.txt
# ============================================================================
import glob
import os
import sys

import numpy as np
from hailo_sdk_client import ClientRunner
from PIL import Image

# ── Configuracoes ───────────────────────────────────────────────────────────
HAR_PATH = "best_backbone.har"
IMAGENS_DIR = "imagens_calibracao"
SAIDA_HEF = "best_backbone.hef"
SAIDA_HAR = "best_backbone_quantized.har"

# O nivel 2 aproveita mais imagens do que o nivel 1. 100 e o minimo util;
# 256-512 e a faixa em que o fine-tune tem material suficiente sem que o
# tempo exploda.
NUM_CALIBRACAO = int(os.getenv("NUM_CALIBRACAO", "256"))
INPUT_SIZE = (640, 640)

# ── Calibracao usa SO imagens originais ─────────────────────────────────────
# Imagem aumentada (flip, rotacao, mudanca de brilho) nao representa o que a
# camera realmente ve. Calibrar com elas desloca as estatisticas de ativacao
# para uma distribuicao que nao existe em campo. Se a sua pasta tiver
# aumentadas, filtre pelo padrao do nome do Roboflow.
PADROES_IGNORAR = [s for s in os.getenv("IGNORAR", "").split(",") if s]

print("=" * 70)
print("Quantizacao nivel 2 (equalizacao + fine-tune)")
print("=" * 70)

if not os.path.exists(HAR_PATH):
    sys.exit(f"ERRO: {HAR_PATH} nao existe. Rode antes o parsear_backbone.py.")

runner = ClientRunner(hw_arch="hailo8l", har=HAR_PATH)

# ── Script de modelo ────────────────────────────────────────────────────────
# model_optimization_flavor(optimization_level=2) e o que liga a equalizacao
# de pesos e o fine-tune da quantizacao. batch_size controla o consumo de
# memoria do fine-tune -- reduza para 4 se estourar a VRAM.
alls = """
normalization1 = normalization([0.0, 0.0, 0.0], [255.0, 255.0, 255.0])
model_optimization_flavor(optimization_level=2, compression_level=0, batch_size=8)
performance_param(compiler_optimization_level=max)
"""
runner.load_model_script(alls)
print("Script de modelo carregado:")
print(alls)

# ── Dataset de calibracao ───────────────────────────────────────────────────
padroes = ["*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG"]
todas = []
for p in padroes:
    todas.extend(glob.glob(os.path.join(IMAGENS_DIR, p)))

if PADROES_IGNORAR:
    antes = len(todas)
    todas = [c for c in todas
             if not any(pad in os.path.basename(c) for pad in PADROES_IGNORAR)]
    print(f"Filtro de aumentadas: {antes} -> {len(todas)} imagens")

if not todas:
    sys.exit(f"ERRO: nenhuma imagem em '{IMAGENS_DIR}'.")
if len(todas) < 64:
    print(f"AVISO: so {len(todas)} imagens. O nivel 2 rende pouco com "
          f"menos de ~64 -- considere ampliar antes de gastar as horas.")

np.random.seed(42)
np.random.shuffle(todas)
selecionadas = todas[:NUM_CALIBRACAO]
print(f"Disponiveis: {len(todas)} | usando: {len(selecionadas)}")

calib = []
for i, caminho in enumerate(selecionadas):
    try:
        img = Image.open(caminho).convert("RGB").resize(INPUT_SIZE, Image.BILINEAR)
        calib.append(np.array(img, dtype=np.float32))
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(selecionadas)} carregadas...")
    except Exception as e:
        print(f"  aviso: {caminho}: {e}")

calib = np.array(calib, dtype=np.float32)
print(f"Dataset de calibracao: {calib.shape}")

# ── Otimizacao ──────────────────────────────────────────────────────────────
print("\nIniciando otimizacao nivel 2. Isto pode levar HORAS.")
print("Nao feche o terminal. Use tmux/screen se for por SSH.\n")
runner.optimize(calib)
runner.save_har(SAIDA_HAR)
print(f"\n{SAIDA_HAR} salvo.")

# ── Compilacao ──────────────────────────────────────────────────────────────
print("\nCompilando para HEF...")
with open(SAIDA_HEF, "wb") as f:
    f.write(runner.compile())

print("\n" + "=" * 70)
print(f"OK: {SAIDA_HEF} gerado.")
print("=" * 70)
print("\nProximo passo OBRIGATORIO: rodar o diagnosticar_mapeamento.py.")
print("Os nomes das saidas do HEF (conv39, conv48, ...) mudam quando o")
print("modelo muda. Se voce copiar o HEF novo sem atualizar o")
print("MAPA_HEF_PARA_ONNX, o agente aborta na inicializacao com a lista dos")
print("nomes reais -- e ai e so colar no .env.")

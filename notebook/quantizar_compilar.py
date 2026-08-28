# quantizar_compilar.py
import numpy as np
from PIL import Image
import glob
import os
from hailo_sdk_client import ClientRunner

# =============================================================
# CONFIGURAÇÕES
# =============================================================
HAR_PATH = "best_backbone.har"
IMAGENS_DIR = "imagens_calibracao"
NUM_CALIBRACAO = 100   # Use 100 imagens para calibração (suficiente e rápido)
INPUT_SIZE = (640, 640)

# =============================================================
# CARREGAR O HAR
# =============================================================
print("Carregando o modelo HAR parseado...")
runner = ClientRunner(hw_arch="hailo8l", har=HAR_PATH)

# =============================================================
# SCRIPT DE NORMALIZAÇÃO (.alls)
# Apenas normalização — NMS fica no CPU (cabeçalho separado)
# Divide por 255 para converter uint8 [0-255] → float [0.0-1.0]
# =============================================================
alls_script = """
normalization1 = normalization([0.0, 0.0, 0.0], [255.0, 255.0, 255.0])
performance_param(compiler_optimization_level=max)
"""
runner.load_model_script(alls_script)
print("✓ Script de normalização carregado")

# =============================================================
# PREPARAR DATASET DE CALIBRAÇÃO
# =============================================================
print(f"\nCarregando imagens de calibração de '{IMAGENS_DIR}'...")

# Suporta .jpg, .jpeg e .png
padroes = ["*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG"]
todas_imagens = []
for padrao in padroes:
    todas_imagens.extend(glob.glob(os.path.join(IMAGENS_DIR, padrao)))

if not todas_imagens:
    print(f"❌ ERRO: Nenhuma imagem encontrada em '{IMAGENS_DIR}'")
    exit(1)

# Embaralhar e usar no máximo NUM_CALIBRACAO imagens
np.random.seed(42)
np.random.shuffle(todas_imagens)
imagens_selecionadas = todas_imagens[:NUM_CALIBRACAO]

print(f"Total disponível: {len(todas_imagens)} imagens")
print(f"Usando: {len(imagens_selecionadas)} imagens para calibração")

calib_dataset = []
for i, img_path in enumerate(imagens_selecionadas):
    try:
        img = Image.open(img_path).convert("RGB")
        # Redimensionar mantendo proporção (letterbox simples)
        img = img.resize(INPUT_SIZE, Image.BILINEAR)
        img_array = np.array(img, dtype=np.float32)  # Shape: (640, 640, 3) HWC
        calib_dataset.append(img_array)
        if (i + 1) % 20 == 0:
            print(f"  Carregadas {i + 1}/{len(imagens_selecionadas)} imagens...")
    except Exception as e:
        print(f"  Aviso: erro ao carregar {img_path}: {e}")

calib_dataset = np.array(calib_dataset, dtype=np.float32)
print(f"✓ Dataset de calibração pronto: shape {calib_dataset.shape}")

# =============================================================
# QUANTIZAÇÃO (PTQ — Post-Training Quantization)
# Optimization Level 1 é mais rápido; Level 2 é mais preciso
# Para uso em produção, recomenda-se Level 2 (mais demorado)
# =============================================================
print(f"\nIniciando quantização com {len(calib_dataset)} imagens...")
print("(Este processo pode levar de 10 a 30 minutos — as ventoinhas vão acelerar)")
print("Não feche o terminal!")

runner.optimize(calib_dataset)
print("\n✓ Quantização concluída!")

# Salvar HAR quantizado (útil para debug)
runner.save_har("best_backbone_quantized.har")
print("✓ Salvo: best_backbone_quantized.har")

# =============================================================
# COMPILAR PARA HEF
# =============================================================
print("\nCompilando para HEF...")
hef_buffer = runner.compile()

with open("best_backbone.hef", "wb") as f:
    f.write(hef_buffer)

print("\n" + "="*60)
print("✅ SUCESSO! Arquivo gerado: best_backbone.hef")
print("="*60)
print("\nPróximos passos:")
print("  1. Copie best_backbone.hef e best_head.onnx para o Raspberry Pi")
print("  2. Instale o HailoRT no Raspberry Pi")
print("  3. Use o script de inferência para rodar detecções")

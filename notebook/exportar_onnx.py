# exportar_onnx.py
from ultralytics import YOLO

print("Carregando modelo YOLO26...")
model = YOLO("best.pt")

print("Exportando para ONNX (opset 11, sem end2end)...")
# opset=11 é necessário para compatibilidade com o Hailo DFC
# end2end=False desliga o cabeçalho NMS nativo do YOLO26,
# expondo os tensores brutos que o DFC consegue processar
model.export(
    format="onnx",
    opset=11,
    simplify=True,
    dynamic=False,
    imgsz=640,
)

print("Exportação concluída: best.onnx")

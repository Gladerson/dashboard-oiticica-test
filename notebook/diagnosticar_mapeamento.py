import onnx
import onnxruntime as ort
from hailo_sdk_client import ClientRunner

# Verificar saídas do HEF via HAR (sem precisar do hardware)
print("=== Saídas do backbone (via HAR quantizado) ===")
runner = ClientRunner(hw_arch="hailo8l", har="best_backbone_quantized.har")
hn = runner.get_hn_model()
for layer in hn.get_output_layers():
	shape = layer.output_shape
	print(f" '{layer.name}' shape_HWC={shape[1:]}")

print("\n=== Entradas do cabeçalho ONNX ===")
sess = ort.InferenceSession("best_head.onnx", providers=["CPUExecutionProvider"])
for inp in sess.get_inputs():
	print(f" '{inp.name}' shape={inp.shape}")

print("\n=== Saídas do cabeçalho ONNX ===")
for out in sess.get_outputs():
	print(f" '{out.name}' shape={out.shape}")

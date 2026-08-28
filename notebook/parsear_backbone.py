# parsear_backbone_v2.py
from hailo_sdk_client import ClientRunner

END_NODES = [
    "/model.10/cv2/act/Mul",
    "/model.13/cv2/act/Mul",
    "/model.16/cv2/act/Mul",
    "/model.17/conv/Conv",
]

MODEL_NAME = "yolo26_fissuras_backbone"
HW_ARCH    = "hailo8l"

print("Iniciando parsing do backbone...")
runner = ClientRunner(hw_arch=HW_ARCH)

runner.translate_onnx_model(
    "best_backbone.onnx",
    MODEL_NAME,
    end_node_names=END_NODES,
    net_input_shapes={"images": [1, 3, 640, 640]},
)

runner.save_har("best_backbone.har")
print("✓ best_backbone.har salvo")

# diagnosticar_model23.py
# Mostra todos os nós do /model.23 para escolher o corte certo
import onnx

model = onnx.load("best.onnx")
nodes = list(model.graph.node)

print(f"Total de nós: {len(nodes)}\n")
print("=== Todos os nós do /model.23 ===")
for i, node in enumerate(nodes):
    if "/model.23/" in node.name or node.name == "/model.23":
        saida = node.output[0] if node.output else "?"
        print(f"  [{i:4d}] {node.op_type:20s}  saida='{saida}'")

print("\n=== Últimos 5 nós do /model.22 (fim do backbone puro) ===")
for i, node in enumerate(nodes):
    if "/model.22/" in node.name or node.name == "/model.22":
        saida = node.output[0] if node.output else "?"
        print(f"  [{i:4d}] {node.op_type:20s}  saida='{saida}'")

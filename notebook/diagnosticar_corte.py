# diagnosticar_corte.py
import onnx

model = onnx.load("best.onnx")
graph = model.graph
nodes = list(graph.node)

ops_problematicos = {"TopK", "GatherElements", "Flatten", "Mod", "ReduceMax", "Gather", "Split"}

print(f"Total de nós no grafo: {len(nodes)}\n")
print("=== TODOS os nós com operadores problemáticos ===")
for i, node in enumerate(nodes):
    if node.op_type in ops_problematicos:
        print(f"  [{i:4d}/{len(nodes)}] {node.op_type:20s}  nome={node.name}")

print("\n=== Nós Conv nas últimas 40 posições (candidatos a end_nodes do backbone) ===")
for i, node in enumerate(nodes):
    if node.op_type == "Conv" and i >= len(nodes) - 40:
        saida = node.output[0] if node.output else "?"
        print(f"  [{i:4d}] Conv  saida='{saida}'")

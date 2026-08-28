# corte_definitivo.py
import onnx
from onnx import helper

model = onnx.load("best.onnx")
graph = model.graph
nodes = list(graph.node)

# Corte: tudo antes do nó [215] vai para o backbone
# Todo o /model.23 (incluindo proto network) fica no CPU
CORTE_IDX = 215

print(f"Backbone: nós [0..{CORTE_IDX-1}] ({CORTE_IDX} nós)")
print(f"Cabeçalho: nós [{CORTE_IDX}..{len(nodes)-1}] ({len(nodes)-CORTE_IDX} nós)")

# ── Tensores produzidos pelo backbone ──────────────────────────
tensores_backbone = set()
for init in graph.initializer:
    tensores_backbone.add(init.name)
for inp in graph.input:
    tensores_backbone.add(inp.name)
for node in nodes[:CORTE_IDX]:
    for out in node.output:
        if out:
            tensores_backbone.add(out)

nomes_inicializadores = {init.name for init in graph.initializer}

# ── Interface backbone→cabeçalho ──────────────────────────────
saidas_backbone = set()
for node in nodes[CORTE_IDX:]:
    for inp in node.input:
        if inp and inp in tensores_backbone and inp not in nomes_inicializadores:
            saidas_backbone.add(inp)

print(f"\nTensores de interface ({len(saidas_backbone)}):")
for t in sorted(saidas_backbone):
    print(f"  '{t}'")

# ── Shapes ────────────────────────────────────────────────────
tensor_shapes = {}
for vi in list(graph.value_info) + list(graph.input) + list(graph.output):
    shape = [d.dim_value for d in vi.type.tensor_type.shape.dim]
    if shape:
        tensor_shapes[vi.name] = shape

def make_vi(nome):
    if nome in tensor_shapes:
        tp = helper.make_tensor_type_proto(onnx.TensorProto.FLOAT, tensor_shapes[nome])
        return helper.make_value_info(nome, tp)
    return helper.make_tensor_value_info(nome, onnx.TensorProto.FLOAT, None)

saidas_ordenadas = sorted(saidas_backbone)

# ── BACKBONE ──────────────────────────────────────────────────
backbone_nodes = nodes[:CORTE_IDX]
inits_usados   = {inp for n in backbone_nodes for inp in n.input}
inits_backbone = [i for i in graph.initializer if i.name in inits_usados]

bg = helper.make_graph(
    nodes=backbone_nodes,
    name="backbone",
    inputs=list(graph.input),
    outputs=[make_vi(n) for n in saidas_ordenadas],
    initializer=inits_backbone,
)
bm = helper.make_model(bg, opset_imports=model.opset_import)
bm.ir_version = model.ir_version
onnx.save(bm, "best_backbone.onnx")
print("\n✓ best_backbone.onnx salvo")

# ── CABEÇALHO ────────────────────────────────────────────────
cabecalho_nodes = nodes[CORTE_IDX:]
inits_usados_h  = {inp for n in cabecalho_nodes for inp in n.input}
inits_head      = [i for i in graph.initializer if i.name in inits_usados_h]

hg = helper.make_graph(
    nodes=cabecalho_nodes,
    name="cabecalho",
    inputs=[make_vi(n) for n in saidas_ordenadas],
    outputs=list(graph.output),
    initializer=inits_head,
)
hm = helper.make_model(hg, opset_imports=model.opset_import)
hm.ir_version = model.ir_version
onnx.save(hm, "best_head.onnx")
print("✓ best_head.onnx salvo")

# ── End nodes para o parsear_backbone.py ─────────────────────
print("\n=== END NODES para parsear_backbone.py ===")
end_nodes = set()
for tensor in saidas_backbone:
    for node in reversed(backbone_nodes):
        if tensor in node.output:
            if node.name:
                end_nodes.add(node.name)
            break

for n in sorted(end_nodes):
    print(f'    "{n}",')

#!/usr/bin/env bash
# ============================================================================
# prepare_model.sh
#
# Gera uma cópia do .glb SEM compressão Draco, usando @gltf-transform/cli
# (Node/npm). O trimesh no server.py passa a ler essa cópia -- sem precisar
# de DracoPy, evitando os problemas de compatibilidade de versão.
#
# Rode isso UMA VEZ (ou de novo se trocar o modelo .glb):
#   bash prepare_model.sh
# ============================================================================
set -e
cd "$(dirname "$0")"

SRC="static/model.glb"
BACKUP="static/model_original_draco.glb"

if [ ! -f "$SRC" ]; then
    echo "ERRO: $SRC não encontrado. Copie o .glb original para lá antes de rodar este script."
    exit 1
fi

if ! command -v npx >/dev/null 2>&1; then
    echo "npx não encontrado. Instale o Node.js/npm primeiro:"
    echo "    sudo apt install -y nodejs npm"
    exit 1
fi

# Se ainda não existe um backup do original comprimido, guarda ele agora.
if [ ! -f "$BACKUP" ]; then
    cp "$SRC" "$BACKUP"
    echo ">> Backup do .glb original (com Draco) salvo em $BACKUP"
fi

echo ">> Descomprimindo (isso baixa o @gltf-transform/cli na primeira vez, pode demorar um pouco)..."
npx --yes @gltf-transform/cli copy "$BACKUP" "$SRC"

echo ">> Pronto! $SRC agora está sem compressão Draco."
echo ">> O trimesh no server.py já consegue ler normalmente, sem precisar de DracoPy."

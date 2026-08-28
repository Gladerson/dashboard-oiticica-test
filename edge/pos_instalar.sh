#!/usr/bin/env bash
# O onvif-zeep 0.2.12 instala os WSDL num caminho com "python3.4" cravado no
# setup.py, mas o client.py os procura ao lado do pacote. Copiamos para onde
# ele olha. Sem isto: ONVIFError "No such file: .../wsdl/devicemgmt.wsdl".
set -euo pipefail
[ -n "${VIRTUAL_ENV:-}" ] || { echo "Ative o venv antes."; exit 1; }

ORIGEM="$(dirname "$(find "$VIRTUAL_ENV" -name devicemgmt.wsdl 2>/dev/null | head -1)")"
[ -n "$ORIGEM" ] || { echo "WSDL nao encontrado; reinstale o onvif-zeep."; exit 1; }

DESTINO="$(python -c 'import os,onvif.client as c; print(os.path.join(os.path.dirname(os.path.dirname(c.__file__)),"wsdl"))')"
if [ "$ORIGEM" = "$DESTINO" ]; then
  echo "Ja esta no lugar certo: $DESTINO"
else
  rm -rf "$DESTINO"
  cp -r "$ORIGEM" "$DESTINO"
  echo "Copiado: $ORIGEM -> $DESTINO ($(ls "$DESTINO" | wc -l) arquivos)"
fi

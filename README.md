# Ambiente de teste - Barragem Oiticica (desktop)

Simula, no desktop, o papel que o Raspberry Pi vai assumir depois: o
**controller** fala com a câmera via ONVIF/RTSP e roda o YOLO; o **server**
simula o backend do dashboard, calcula a posição 3D real das detecções via
raycasting contra o `.glb`, e serve a interface web (Three.js).

## Estrutura

```
dashboard_oiticica_test/
├── install_desktop.sh          # instala tudo (Debian/Ubuntu/Zorin)
├── controller/
│   ├── config.py                # IP/credenciais da câmera, RTSP, server, etc.
│   ├── onvif_ptz.py              # wrapper ONVIF (detecta faixas reais de pan/tilt/zoom)
│   ├── controller.py             # loop principal: home, telemetria, YOLO, comandos
│   ├── best.pt                   # (você copia) modelo YOLO de rachaduras
│   └── requirements.txt
└── server/
    ├── glb_geo.py                 # georreferenciamento real + raycasting no .glb
    ├── server.py                   # API/WebSocket + histórico de detecções
    ├── prepare_model.sh            # remove compressão Draco do .glb (rodar 1x)
    ├── static/
    │   ├── model.glb                # (você copia) modelo 3D da parede
    │   └── dashboard.html           # Three.js: modelo, cone PTZ, vídeo, histórico
    ├── history/                     # detecções salvas (criado em runtime)
    └── requirements.txt
```

## 0. Pré-requisitos

- `Processamento-1-Oiticica-textured_model.glb` e `best.pt` em
  `/home/gladerson/Projetos/dashboard_oiticica` (o instalador copia
  automaticamente para dentro do projeto de teste).
- O arquivo de georreferenciamento do ODM/WebODM (`odm_georeferencing_model_geo.txt`
  ou similar) — necessário pra preencher `server/glb_geo.py` com o offset UTM real.
- Node.js/npm (para descomprimir o Draco do `.glb` — ver passo 3).

## 1. Instalar

```bash
cd ~/Projetos
mkdir -p dashboard_oiticica_test
# copie os arquivos deste pacote para dentro de ~/Projetos/dashboard_oiticica_test
cd ~/Projetos/dashboard_oiticica_test
chmod +x install_desktop.sh
bash install_desktop.sh
```

## 2. Configurar

**Credenciais (`.env`)** — as credenciais da câmera NÃO ficam mais no código.
Elas vêm de `controller/.env`, que **nunca é commitado** (está no `.gitignore`):

```bash
cd ~/Projetos/dashboard_oiticica_test/controller
cp .env.example .env
nano .env   # preencha CAMERA_IP, ONVIF_USER, ONVIF_PASSWORD, RTSP_URL
```

`controller/config.py` lê essas variáveis automaticamente e falha com uma
mensagem clara se alguma obrigatória estiver faltando.

**`server/glb_geo.py`** — preencha com os dados do seu arquivo de
georreferenciamento ODM:

```python
UTM_ZONE = 24
UTM_HEMISPHERE_SOUTH = True
GEO_OFFSET_X = 707543.0    # linha 2, valor 1, do odm_georeferencing_model_geo.txt
GEO_OFFSET_Y = 9319434.0   # linha 2, valor 2
GEO_OFFSET_Z = 0.0         # se o arquivo só tiver X e Y, deixe 0.0
MODEL_UP_AXIS = "Z"        # exports de fotogrametria/ODM costumam ser Z-up
```

## 3. Preparar o modelo (remover compressão Draco)

O `.glb` de fotogrametria normalmente vem comprimido com Draco. O Three.js no
navegador decodifica isso sem problema, mas o `trimesh` (Python, usado pelo
server para o raycasting real) tem histórico de incompatibilidade de versão
com bibliotecas de decode Draco. A solução mais robusta é descomprimir o
arquivo uma vez, fora do Python:

```bash
cd ~/Projetos/dashboard_oiticica_test/server
cp /home/gladerson/Projetos/dashboard_oiticica/Processamento-1-Oiticica-textured_model.glb static/model.glb
bash prepare_model.sh
```

Isso gera um backup do original comprimido (`static/model_original_draco.glb`)
e sobrescreve `static/model.glb` com uma versão sem Draco, que tanto o
`trimesh` quanto o Three.js leem sem depender de nenhum decoder extra.

## 4. Rodar

Dois terminais:

```bash
# Terminal 1 - server (dashboard)
cd ~/Projetos/dashboard_oiticica_test/server
source venv/bin/activate
python server.py
```

```bash
# Terminal 2 - controller (câmera)
cd ~/Projetos/dashboard_oiticica_test/controller
source venv/bin/activate
python controller.py
```

Abra o dashboard em: **http://127.0.0.1:8001**

No log de inicialização do `server.py`, confira:

```
>> Malha carregada: N vértices, M faces.
>> Bounding box real do modelo (.glb), coordenadas locais: min=[...] max=[...]
>> Câmera (lat/lon fornecidos) convertida para X/Y local: (X, Y)
```

Se o X/Y da câmera cair bem fora do bounding box do modelo, e a "distância ao
ponto mais próximo" impressa for grande, vale conferir se a lat/lon da câmera
e o offset de georreferenciamento realmente correspondem à mesma área/projeto
do `.glb` atual.

## 5. O que esperar

- O controller conecta na câmera, detecta as faixas reais de pan/tilt/zoom do
  ONVIF (normalizado ou em graus — não é chutado) e manda a câmera para o
  ponto zero (`AbsoluteMove` para `x=0,y=0`, que é o zero matemático das
  coordenadas ONVIF — diferente de `GotoHomePosition`, que é um preset salvo
  na câmera e pode apontar para qualquer lugar).
- A cada ~1s, o controller envia `coord_p/coord_t/coord_z` para o server, que
  calcula (via raycasting real contra o `.glb`) onde esse apontamento
  intercepta a parede, e atualiza o cone no dashboard.
- Os botões de movimentação atualizam o cone/telemetria imediatamente com a
  resposta do próprio clique (não esperam o próximo ciclo de telemetria).
- Quando o YOLO detecta rachadura (`conf=0.558`), o controller manda a imagem
  original + a máscara de segmentação; o server salva em `server/history/` e
  aparece no painel de histórico do dashboard.

## 6. Migrando para o Raspberry Pi depois

- `controller/` roda igual, mudando `SERVER_URL` em `config.py` para o IP
  real do servidor.
- `CONTROLLER_URL` em `server/server.py` passa a ser o IP do Raspberry.
- Se o Raspberry usar Hailo-8L, troque o carregamento do modelo em
  `detection_loop()` pela stack HEF+ONNX em vez do `ultralytics.YOLO` puro.

## 7. Pontos para validar/ajustar

- **Convenção de pan=0/tilt=0**: calculada automaticamente como o vetor até o
  ponto real mais próximo da malha a partir da posição da câmera — não é um
  ângulo chutado. Confirme visualmente se bate com a orientação física real.
- **Curso mecânico do pan/tilt** em graus: assumido ±180°/±90° quando o ONVIF
  reporta valores normalizados (-1..1). Ajuste `pan_deg_range`/`tilt_deg_range`
  em `controller/onvif_ptz.py` se sua câmera tiver outro curso mecânico.
- **Altura da câmera quando ela está fora da área XY do modelo**: o server usa
  o ponto real mais próximo da malha como referência de altura (não uma
  aproximação numérica) e soma os 7m. Se essa distância impressa no log for
  grande, verifique a lat/lon e o offset de georreferenciamento.

## 8. Credenciais e repositório Git

As credenciais da câmera vivem só em `controller/.env` (fora do Git). Antes
de dar `git push`, confirme que ele está mesmo sendo ignorado:

```bash
git check-ignore -v controller/.env
# deve imprimir a linha do .gitignore que está barrando o arquivo
```

Repositório **privado** no GitHub já reduz bastante o risco de exposição,
mas isso sozinho não substitui manter segredos fora do histórico do Git:
mesmo privado, um repositório pode ser transferido, ter colaboradores
adicionados, ou virar público por engano no futuro -- e qualquer coisa que
já foi commitada uma vez continua no histórico, mesmo que você delete o
arquivo depois. Por isso o `.env` real nunca entra no `git add`; só o
`.env.example` (sem valores reais) é versionado.

Se em algum momento uma senha real acabar indo para um commit por engano,
trocar a senha na câmera é mais simples e confiável do que tentar reescrever
o histórico do Git.


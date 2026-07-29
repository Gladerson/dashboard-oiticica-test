# Dashboard Barragem Oiticica — ambiente de teste

Monitoramento de rachaduras com câmera PTZ e visualização 3D georreferenciada.
Simula, no desktop, o papel que o Raspberry Pi vai assumir depois: o
**controller** fala com a câmera via ONVIF/RTSP e roda o YOLO; o **server**
faz o raycasting real contra o modelo `.glb` e serve o dashboard (Three.js).

## Estrutura

```text
dashboard_oiticica_test/
├── install_desktop.sh            # instala tudo (Debian/Ubuntu/Zorin)
├── controller/
│   ├── .env.example               # modelo de credenciais (o .env real fica fora do Git)
│   ├── config.py                  # lê o .env e aplica os padrões
│   ├── onvif_ptz.py               # wrapper ONVIF (detecta espaços absoluto/relativo/contínuo)
│   ├── controller.py              # motor de movimento, telemetria, YOLO, API local
│   ├── calibrar_curso.py          # mede o curso mecânico real em graus
│   ├── best.pt                    # (você copia) modelo YOLO de rachaduras
│   └── requirements.txt
└── server/
    ├── glb_geo.py                 # georreferenciamento + raycasting + ângulos PTZ
    ├── server.py                  # API/WebSocket, cone, /api/aim, /api/locate
    ├── prepare_model.sh           # remove compressão Draco do .glb (rodar 1x)
    ├── static/
    │   ├── model.glb               # (você copia) modelo 3D da barragem
    │   └── dashboard.html          # Three.js: modelo, cone, vídeo, histórico
    ├── history/                    # detecções salvas (criado em runtime)
    └── requirements.txt
```

## 0. Pré-requisitos

- `Processamento-1-Oiticica-textured_model.glb` e `best.pt` em
  `/home/gladerson/Projetos/dashboard_oiticica` (o instalador copia sozinho).
- O `odm_georeferencing_model_geo.txt` do projeto ODM/WebODM — dele saem o
  offset UTM e a zona usados em `server/glb_geo.py`.
- Node.js/npm (para descomprimir o Draco do `.glb` — ver passo 3).

## 1. Instalar

```bash
cd ~/Projetos/dashboard_oiticica_test
chmod +x install_desktop.sh
bash install_desktop.sh
```

Instale também o raycasting acelerado (opcional, mas o cone dispara 25 raios
por atualização — sem isso fica lento em malhas grandes):

```bash
cd server && source venv/bin/activate && pip install embreex && deactivate
```

## 2. Configurar

### Credenciais (`controller/.env`)

Nunca ficam no código nem no Git:

```bash
cd controller
cp .env.example .env
nano .env   # CAMERA_IP, ONVIF_USER, ONVIF_PASSWORD, RTSP_URL
```

`config.py` falha com mensagem clara se faltar alguma obrigatória.

### Georreferenciamento (`server/glb_geo.py`)

```python
UTM_ZONE = 24
UTM_HEMISPHERE_SOUTH = True
GEO_OFFSET_X = 707543.0    # linha 2, valor 1 do odm_georeferencing_model_geo.txt
GEO_OFFSET_Y = 9319434.0   # linha 2, valor 2
GEO_OFFSET_Z = 0.0         # se o arquivo só tiver X e Y, deixe 0.0
MODEL_UP_AXIS = "Z"        # exports de fotogrametria/ODM costumam ser Z-up
```

## 3. Preparar o modelo (remover Draco)

O `.glb` de fotogrametria vem comprimido com Draco. O Three.js decodifica no
navegador sem problema, mas o `trimesh` (usado no raycasting) tem histórico de
incompatibilidade com os decoders. Descomprima uma vez, fora do Python:

```bash
cd server
cp ~/Projetos/dashboard_oiticica/Processamento-1-Oiticica-textured_model.glb static/model.glb
bash prepare_model.sh
```

## 4. Rodar

```bash
# Terminal 1 — server
cd server && source venv/bin/activate && python server.py

# Terminal 2 — controller
cd controller && source venv/bin/activate && python controller.py
```

Dashboard em **http://127.0.0.1:8001**

Para acessar de outra máquina, diga ao navegador onde está o controller:

```bash
CONTROLLER_PUBLIC_URL=http://IP_DO_RASPBERRY:8090 python server.py
```

## 5. Como funciona

### Movimentação PTZ

Os botões usam **ContinuousMove**: a câmera move enquanto o botão está
pressionado e para ao soltar (setas do teclado e `+`/`−` também funcionam).

O controller não executa comandos direto nos endpoints. Eles registram uma
**intenção de movimento com prazo de validade**, e uma única thread
(`PTZMotion`) a compara com o estado aplicado e emite ContinuousMove/Stop.
Isso elimina a corrida entre `/continuous` e `/stop` que fazia a câmera girar
sem parar, e garante parada automática (~800ms) se o navegador travar, a aba
fechar ou a rede cair. O dashboard renova a intenção a cada 300ms.

O dashboard fala **direto** com o controller na 8090 (CORS liberado), com
fallback automático pelo proxy do server se isso falhar.

### Cone de visão

O server dispara um leque de raios reais contra a malha (1 central + 24 no
anel do campo de visão) e devolve o contorno onde a visão encosta no objeto.
O cone no dashboard termina exatamente na parede e se molda ao relevo dela,
em vez de flutuar. A abertura do cone acompanha o zoom.

### Detecções

Quando o YOLO detecta rachadura, o controller envia imagem original + máscara
e a pose PTZ. O server salva em `server/history/` e transmite por WebSocket.
Inferência é pulada enquanto a câmera está em movimento (evita frame borrado
e pose imprecisa).

No painel de histórico, clicar numa detecção revela dois botões:

- **Abrir** — modal com a máscara, alternando para a imagem original
- **Localizar** — devolve a câmera física à pose exata da detecção e leva a
  visão 3D até o ponto, marcado em laranja pulsante

O `/api/locate` **recalcula** o ponto 3D a partir do pan/tilt/zoom gravados,
então detecções antigas se corrigem sozinhas se a calibração mudar.

### Close por seleção (Shift + arrastar)

Segure Shift e arraste um retângulo sobre o modelo 3D. O dashboard faz
raycasting em 9 pontos da região, manda centro e cantos ao `/api/aim`, e o
server converte em pan/tilt (inverso exato da rotação) e no zoom cujo
meio-ângulo cobre a seleção com 30% de folga.

## 6. Calibração

### Sentido do pan/tilt

O ONVIF não padroniza para que lado o pan positivo gira. Se o cone andar
espelhado em relação à câmera real, inverta sem editar código:

```bash
PAN_SIGN=1 python server.py     # padrão é -1
TILT_SIGN=-1 python server.py   # se subir/descer estiver trocado
```

### Curso mecânico em graus

Esta câmera reporta pan/tilt normalizados (−1..1), o que diz "estou no meio do
curso" mas não quantos graus é o curso inteiro. Enquanto não for medido,
assume-se ±180°/±90° — um palpite que gera erro angular crescente longe do
centro. É propriedade da câmera, não do local: **pode ser medido em bancada**.

```bash
cd controller && source venv/bin/activate
python calibrar_curso.py   # com o controller.py PARADO
```

Anote `PAN_DEG_RANGE`/`TILT_DEG_RANGE` no `.env`.

### Altura da câmera

O server tenta três estratégias, nesta ordem:

1. `CAMERA_ABS_ALT` — elevação absoluta da lente, se você souber
2. Raio vertical contra a malha, quando a câmera está sobre a área reconstruída
3. Percentil 8 das alturas dos vértices vizinhos (estimativa do terreno)

Hoje cai na estratégia 3, porque a câmera fica ~36m além da borda norte do
modelo. **Isso é um palpite**: a estimativa (87.07) praticamente coincide com o
ponto de malha mais próximo (86.63), o que pode significar que ambos estão
ancorados no topo da estrutura em vez do chão. Erro aqui vira erro sistemático
de tilt. Ao instalar na barragem, meça a elevação real e defina:

```bash
CAMERA_ABS_ALT=88.5 python server.py
```

## 7. Variáveis de ambiente

**Controller** (`controller/.env`) — obrigatórias: `CAMERA_IP`, `ONVIF_USER`,
`ONVIF_PASSWORD`, `RTSP_URL`.

| Variável | Padrão | Para quê |
|---|---|---|
| `SERVER_URL` | `http://127.0.0.1:8001` | onde está o server |
| `PAN_DEG_RANGE` / `TILT_DEG_RANGE` | 180 / 90 | curso mecânico (ver §6) |
| `PTZ_MOTION_TICK_SECONDS` | 0.05 | latência de parada da câmera |
| `PTZ_POLL_INTERVAL_SECONDS` | 1.0 | telemetria parada |
| `PTZ_POLL_FAST_SECONDS` | 0.15 | telemetria em movimento |
| `PTZ_SEPARATE_CONNECTIONS` | true | conexão ONVIF dedicada à telemetria |
| `YOLO_CONF_THRESHOLD` | 0.558 | confiança mínima |
| `DETECTION_COOLDOWN_SECONDS` | 5 | intervalo mínimo entre alertas |

**Server** (variáveis de ambiente diretas):

| Variável | Padrão | Para quê |
|---|---|---|
| `CONTROLLER_URL` | `http://127.0.0.1:8090` | server → controller |
| `CONTROLLER_PUBLIC_URL` | = acima | navegador → controller |
| `CAMERA_ABS_ALT` | — | elevação absoluta da lente |
| `PAN_SIGN` / `TILT_SIGN` | -1 / 1 | sentido de rotação |
| `CONE_HALF_ANGLE_WIDE` / `_TELE` | 18 / 2 | abertura do cone em graus |
| `CONE_RING_RAYS` | 24 | raios do anel (16 é mais leve) |

## 8. Migrando para o Raspberry Pi

- `controller/` roda igual; ajuste `SERVER_URL` no `.env`.
- No server, defina `CONTROLLER_URL` e `CONTROLLER_PUBLIC_URL` com o IP do Pi.
- Com Hailo-8L, troque o `ultralytics.YOLO` do `detection_loop()` pela stack
  HEF + ONNX (backbone na NPU, cabeçalho no CPU).

## 9. Credenciais e Git

O repositório é **público**, então a regra é simples: nada de segredo entra no
Git, nem uma vez. As credenciais vivem só em `controller/.env`, ignorado pelo
`.gitignore`. Confirme antes de qualquer push:

```bash
git check-ignore -v controller/.env
git log --all -p -S "ONVIF_PASSWORD=" -- . | grep -i "ONVIF_PASSWORD="
# só deve aparecer o placeholder do .env.example
```

Se uma senha real escapar para um commit, **troque a senha na câmera** — é
mais confiável do que reescrever o histórico, porque num repositório público
o valor já pode ter sido lido ou clonado.

Como o repositório é público, considere também que a lat/lon da câmera em
`server/server.py` fica exposta. Se isso for indesejável, mova para variável
de ambiente.

## 10. Pendências conhecidas

- **Curso mecânico não calibrado** — erro angular fora do centro (§6)
- **Altura da câmera estimada** — erro sistemático de tilt (§6)
- **Sentido do pan** — corrigido com `PAN_SIGN=-1`, confirmar em campo
- `diagnosticar_terreno.py` e `verificar.sh` ainda não versionados
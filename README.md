# Sistema de Monitoramento de Rachaduras — Barragem Oiticica

Detecção de fissuras em concreto com câmera PTZ, inferência em borda
(Raspberry Pi 5 + Hailo-8L) e visualização 3D georreferenciada.

O Raspberry executa a inferência localmente e envia ao servidor **apenas
metadados**. O vídeo sobe unicamente quando o operador pede, e por tempo
limitado. O servidor faz o raycasting real contra o modelo `.glb` de
fotogrametria, deduplica alertas no espaço 3D e serve o dashboard.

---

## 1. Arquitetura

```text
CAMERA PTZ (ONVIF/RTSP)
      |  RTSP  (nunca sai do Pi em operação normal)
      v
RASPBERRY PI 5 + HAILO-8L ............. edge/agente_borda.py
      |   backbone INT8 -> NPU        (best_backbone.hef)
      |   cabeçalho + máscara -> CPU  (best_head.onnx via ONNX Runtime)
      |
      |--- telemetria  ~197 B  a cada 1 s
      |--- detecção    ~480 B  SO coordenadas (evento; nenhuma imagem)
      |--- foto completa       SOMENTE quando o operador clica em "Abrir"
      |--- frame JPEG          SOMENTE enquanto houver pedido vivo
      v
SERVIDOR .............................. server/server.py + server/borda.py
      |   raycasting contra o .glb, dedup 3D, histórico em disco
      v
DASHBOARD (Three.js)  <-- WebSocket
```

### Economia de rede

| Cenário | Tráfego |
|---|---|
| MJPEG contínuo (arquitetura anterior), 10 fps a 45 KB/quadro | ~37 GB/dia |
| Somente metadados, telemetria a 1 Hz | ~17 MB/dia (~50 MB com sobrecarga HTTP) |
| Telemetria a cada 5 s | ~3,4 MB/dia |
| Um minuto de stream sob demanda, 4 fps a 25 KB | ~6 MB |

Cerca de **700× menos** em operação normal.

### O princípio de projeto: estado desejado

O servidor **nunca** envia "faça X agora". Ele publica em que estado quer o
dispositivo, e o dispositivo converge para esse estado. É o mesmo padrão de
*intenção com prazo de validade* que o `PTZMotion` já usava para o movimento
da câmera, agora atravessando a rede — e é isso que torna o sistema tolerante
a perda de pacote, queda do servidor e reinício do Pi, sem travar em nenhum
estado.

Em HTTP o estado desce **de carona na resposta do POST de telemetria**: não há
polling extra, e o servidor não precisa conseguir abrir conexão de volta, o que
importa quando o Pi estiver atrás de NAT ou 4G.

**Três relógios protegem o stream**, e qualquer um deles sozinho já o encerra:

1. o servidor expira a janela em 60 s e avisa o dashboard;
2. o Pi só transmite enquanto o tempo restante for renovado, e zera se o
   servidor calar;
3. `STREAM_TTL_S` (75 s) é o teto absoluto local, mesmo que o servidor peça um
   valor absurdo.

---

## 2. Estrutura do repositório

```text
dashboard_oiticica_test/
├── install_desktop.sh              # instalador do servidor (Debian/Ubuntu/Zorin)
│
├── edge/                           # AGENTE DE BORDA — roda no Raspberry Pi (definitivo)
│   ├── agente_borda.py             # processo principal: PTZ + inferência + transporte + API 8090
│   ├── inferencia_hailo.py         # pipeline híbrido HEF (NPU) + ONNX (CPU); letterbox, NMS, máscara
│   ├── transporte.py               # TransporteHTTP e TransporteMQTT com a mesma interface
│   ├── config_borda.py             # lê edge/.env e aplica padrões
│   ├── calibrar_limiar_int8.py     # escolhe o limiar de confiança do modelo quantizado
│   ├── avaliar_mascaras.py         # compara qualidade de máscara INT8 vs float32
│   ├── conferir_deteccao.py        # diagnóstico visual: rótulo (verde) vs predição (vermelho)
│   ├── diagnosticar_caixas.py      # decide se output0 vem em xyxy ou cxcywh
│   ├── pos_instalar.sh             # corrige o caminho dos WSDL do onvif-zeep
│   ├── agente-borda.service        # unidade systemd
│   ├── requirements.txt
│   └── .env.example
│
├── controller/                     # CAMINHO SEM RASPBERRY — desenvolvimento e comparação
│   ├── controller.py               # equivalente do agente rodando no desktop, com Ultralytics
│   ├── onvif_ptz.py                # wrapper ONVIF — USADO TAMBÉM PELO edge/agente_borda.py
│   ├── config.py                   # lê controller/.env
│   ├── calibrar_curso.py           # mede o curso mecânico real do PTZ em graus
│   ├── requirements.txt
│   └── .env.example
│
├── server/                         # SERVIDOR DO DASHBOARD
│   ├── server.py                   # API, WebSocket, cone de visão, /api/aim, /api/locate
│   ├── borda.py                    # estado desejado, endpoints /api/edge/*, relay de stream
│   ├── glb_geo.py                  # georreferenciamento UTM, raycasting, ângulos PTZ
│   ├── db.py                       # PostgreSQL: usuarios/sessoes/localidades/dispositivos/dashboards
│   ├── auth.py                     # login por sessão, middleware de autenticação, admin de usuários
│   ├── dispositivos.py             # cadastro de localidades (modelo 3D) e dispositivos CV-SHM
│   ├── prepare_model.sh            # remove compressão Draco do .glb (rodar uma vez, uso manual)
│   ├── static/dashboard.html       # Three.js: modelo, cone, telinha, histórico, marcações 3D
│   ├── static/login.html           # tela de entrada
│   ├── static/config.html          # tema, própria senha, administração de usuários (admin)
│   ├── static/dispositivos.html    # cadastro de localidades/modelos 3D e dispositivos, mapa Leaflet
│   ├── static/modelos/             # .glb enviados pela aba Dispositivos (fora do Git)
│   ├── history/                    # detecções salvas em runtime (fora do Git)
│   ├── requirements.txt
│   └── .env.example
│
├── notebook/                       # PIPELINE DE COMPILAÇÃO (estação x86 com o Hailo DFC)
│   ├── exportar_onnx.py            # best.pt -> best.onnx (opset 11, sem end2end)
│   ├── diagnosticar_corte.py       # localiza os operadores que a Hailo não aceita
│   ├── diagnosticar_model23.py     # lista os nós do /model.23 e revela o CORTE_IDX
│   ├── corte_definitivo.py         # divide o grafo em backbone (.onnx) e cabeçalho (.onnx)
│   ├── parsear_backbone.py         # backbone .onnx -> .har
│   ├── quantizar_compilar.py       # quantização nível 1 (rápida)
│   ├── quantizar_compilar_v2.py    # quantização nível 2 (equalização + fine-tuning)
│   ├── diagnosticar_mapeamento.py  # descobre os nomes das saídas do HEF
│   └── avaliar_pt_mesmo_protocolo.py  # linha de base float32, mesmo protocolo do Pi
│
└── resultados/                     # medições que sustentam o capítulo de resultados
    ├── limiar_int8.json            # curva P/R/F1/F2 do pipeline quantizado
    ├── limiar_pt.json              # curva equivalente em float32
    ├── mascaras_int8.json          # qualidade de máscara, detalhe por instância
    ├── mascaras_pt.json            # idem, float32
    ├── corte_definitivo.txt        # end nodes da divisão do grafo
    ├── mapeamento.txt              # saídas do HEF -> entradas do cabeçalho ONNX
    └── parsing.txt                 # log do parsing do backbone
```

### Por onde começar a ler o código

| Se você quer mexer em... | Comece por |
|---|---|
| detecção, pós-processamento, máscara | `edge/inferencia_hailo.py` (`_pos`, `letterbox`, `nms`) |
| PTZ, laço de vídeo, ciclo do agente | `edge/agente_borda.py` (`PTZMotion`, `video_loop`, `stream_loop`) |
| HTTP/MQTT, formato do payload | `edge/transporte.py` + `publicar_deteccao` no agente |
| estado desejado, janela de stream | `server/borda.py` (`EstadoDesejado`, `_vigia_stream`) |
| raycasting, cone, coordenadas 3D | `server/glb_geo.py` |
| histórico, dedup, WebSocket | `server/server.py` |
| interface, marcações 3D, telinha | `server/static/dashboard.html` |
| login, sessão, usuários | `server/auth.py` + `server/db.py` |
| localidades, modelos 3D, dispositivos | `server/dispositivos.py` |
| compilação do modelo | `notebook/` na ordem numérica da §6 |

Duas invariantes que atravessam o código e não são óbvias na leitura local:

1. **Nada que precise de lock entra dentro de um `with lock`.** O `est.lock` do
   agente é `RLock`, mas isso é rede de segurança, não licença — resolva as
   dependências antes de entrar na região crítica.
2. **O servidor publica estado, nunca comando.** Toda funcionalidade nova que
   precise mudar o comportamento do Pi deve entrar no dicionário de estado
   desejado (`server/borda.py`) e ser aplicada em `aplicar_estado`
   (`edge/agente_borda.py`), não como um endpoint imperativo no Pi.

### Arquivos que o Git **não** guarda

| Arquivo | Onde | Como se recupera |
|---|---|---|
| `edge/.env`, `server/.env`, `controller/.env` | ambos | Recriar do `.env.example` (exige a senha da câmera) |
| `edge/best_backbone.hef`, `edge/best_head.onnx` | Pi | Recompilar com `notebook/` — horas |
| `server/static/model.glb` | servidor | Reprocessar o `.glb` de fotogrametria |
| `server/history/` | servidor | Não se recupera — é o histórico real |
| `edge/evidencias/` | Pi | Não se recupera |
| `venv/` | ambos | Recriar — minutos |

---

## 3. `controller.py` ou `edge/agente_borda.py`?

Os dois expõem **a mesma API de PTZ** (`/status`, `/command/continuous`,
`/command/stop`, `/command/absolute`, `/command/home`) na porta 8090, de
propósito: o dashboard não precisa saber com qual dos dois está falando.

| | `controller/controller.py` | `edge/agente_borda.py` |
|---|---|---|
| **Papel** | Ambiente de teste / desenvolvimento | **Definitivo, no Raspberry Pi** |
| Onde roda | Desktop x86 | Raspberry Pi 5 + Hailo-8L |
| Inferência | Ultralytics `YOLO`, `best.pt`, float32 | HEF (NPU) + ONNX (CPU), INT8 |
| Vídeo | MJPEG contínuo pelo `/video_feed` | Sob demanda, com prazo de validade |
| Detecção | Envia imagem original + máscara | Metadados; imagem completa sob pedido |
| Transporte | HTTP fixo | HTTP ou MQTT, trocável em runtime |
| Estado desejado | Não implementa | Converge para o estado do servidor |
| Evidência local | Não guarda | `edge/evidencias/`, com teto de disco |

**Quando usar o `controller.py`:** desenvolver o dashboard sem o Pi ligado;
comparar o comportamento em float32 contra o INT8 da borda usando o mesmo
pipeline de raycasting e histórico; validar mudanças no `server/` sem depender
de hardware.

**Nunca use os dois ao mesmo tempo apontando para a mesma câmera** — eles
disputam a conexão ONVIF e a porta 8090.

> **`controller/onvif_ptz.py` é código compartilhado.** O `agente_borda.py` o
> importa via `sys.path.insert` para `../controller`. Apagar a pasta
> `controller/` quebra o agente na inicialização, mesmo que você nunca rode o
> `controller.py`. O `calibrar_curso.py` também continua em uso e depende de
> `config.py`.

---

## 4. Instalação — Raspberry Pi (definitivo)

### 4.1 Verificar o Hailo-8L

```bash
hailortcli fw-control identify
# Se der "command not found":
sudo apt-get update
sudo apt-get install -y hailort hailo-dkms python3-hailort && sudo reboot
```

Deve informar `Board Name: Hailo-8L` e `Device Architecture: HAILO8L`.

### 4.2 Clonar e criar o ambiente

`--system-site-packages` é **obrigatório**: no Raspberry Pi OS o
`hailo_platform` vem via apt, como pacote do sistema, e sem essa opção o venv
não consegue importá-lo.

```bash
cd ~/Projetos
git clone https://github.com/Gladerson/dashboard-oiticica-test.git dashboard_oiticica_test
cd dashboard_oiticica_test/edge

python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install --upgrade pip && pip install -r requirements.txt
bash pos_instalar.sh

python -c "import hailo_platform, onnxruntime, cv2, onvif, paho.mqtt.client; print('imports OK')"
```

### 4.3 Modelo e configuração

```bash
# Do notebook:
#   scp best_backbone.hef best_head.onnx PI:~/Projetos/dashboard_oiticica_test/edge/

cd ~/Projetos/dashboard_oiticica_test/edge
cp .env.example .env
nano .env      # CAMERA_IP, ONVIF_USER, ONVIF_PASSWORD, RTSP_URL, SERVER_URL
```

Se os nomes das saídas do HEF mudaram em relação ao padrão do `config_borda.py`,
defina `MAPA_HEF_PARA_ONNX` no `.env`, numa única linha. Se não souber os nomes,
rode o agente assim mesmo: ele aborta na inicialização imprimindo os nomes reais
dos dois lados.

**Câmera atrás de VPN:** RTSP por UDP através de túnel fragmenta e perde pacote.
O sintoma são mensagens `cu_qp_delta ... outside the valid range` e quadros
corrompidos, que geram falsos positivos. Force TCP no `.env`:

```bash
OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp|buffer_size;1048576|stimeout;8000000
```

### 4.4 Testar e instalar o serviço

```bash
python agente_borda.py
# Espere: >> Pipeline Hailo aberto e ativo (nao reconfigura por frame).

# Noutro terminal — o preview é o teste decisivo do caminho inteiro
curl -s --max-time 5 localhost:8090/status | python3 -m json.tool
curl -s -o /tmp/p.jpg localhost:8090/borda/preview.jpg && ls -lh /tmp/p.jpg

sudo cp agente-borda.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now agente-borda
sudo systemctl status agente-borda --no-pager
```

**Serviço e execução manual não coexistem**: disputam a porta 8090 e o
dispositivo Hailo. Antes de rodar qualquer script à mão,
`sudo systemctl stop agente-borda`.

---

## 5. Instalação — Servidor

```bash
cd ~/Projetos
git clone https://github.com/Gladerson/dashboard-oiticica-test.git dashboard_oiticica_test
cd dashboard_oiticica_test
bash install_desktop.sh

cd server && source venv/bin/activate
pip install -r requirements.txt
pip install embreex          # raycasting acelerado: o cone dispara 25 raios por atualização
```

### 5.1a PostgreSQL (login e administração de usuários)

O painel exige login desde a etapa "Configuração" (§16). `install_desktop.sh`
já cria a role e o banco; para fazer à mão:

```bash
sudo apt install -y postgresql
sudo -u postgres psql -c "CREATE ROLE oiticica WITH LOGIN PASSWORD 'TROQUE_ESTA_SENHA';"
sudo -u postgres psql -c "CREATE DATABASE oiticica OWNER oiticica;"
sudo -u postgres psql -d oiticica -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
```

Depois, em `server/.env`:

```bash
DATABASE_URL=postgresql://oiticica:TROQUE_ESTA_SENHA@127.0.0.1:5432/oiticica
```

`server/db.py` cria as tabelas sozinho na primeira subida (`CREATE TABLE IF
NOT EXISTS`) e falha rápido, com mensagem clara, se não conseguir conectar —
mesmo espírito do `.env`/WSDL/porta ocupada do agente de borda. No primeiro
boot sem nenhum usuário cadastrado, ele semeia um admin padrão:

```
usuário: admin
senha:   hydroconecta
```

**Troque essa senha no primeiro login** — o próprio painel obriga: com
`trocar_senha` pendente, toda rota redireciona para `/config` até a senha
mudar. Há dois papéis: `admin` (cria/exclui usuários, redefine senha de
qualquer um) e `usuario` (só a própria senha e o próprio tema). Ver §10 para
as rotas e §16 para o que ainda falta (Dispositivos, Dashboard).

> Por padrão o cookie de sessão NÃO exige HTTPS (`SESSION_COOKIE_SECURE=false`,
> para funcionar numa LAN comum). Se o servidor for exposto atrás de um
> reverse proxy com HTTPS de verdade, defina `SESSION_COOKIE_SECURE=true` no
> `.env` — sem isso, um cookie de sessão trafegando em texto claro numa rede
> não confiável pode ser interceptado.

### 5.1a-bis Dispositivos (cadastro de câmeras/localidades/modelos 3D)

Aba "Dispositivos" (`/dispositivos`), disponível para qualquer usuário
logado (não só admin). Duas listas:

- **Localidades** — nome, georreferenciamento (zona UTM, offset X/Y/Z, eixo
  "para cima") e upload de um `.glb`. O upload roda a mesma descompressão
  Draco do `prepare_model.sh` (`@gltf-transform/cli` via `npx`), só que
  disparada pelo servidor em segundo plano — por isso é preciso ter
  `npx` disponível no servidor (`install_desktop.sh` tenta instalar, mas
  pula sozinho se você já tiver Node de outra origem — ver §13, "npm
  conflita com nodejs"). Sem `npx`, o upload aceita o arquivo mas o status
  fica em **erro**, com a mensagem explicando o que falta.
- **Dispositivos** — nome, proprietário (ex.: SEMARH), localidade,
  transporte (HTTP/MQTT) e posição no mapa (Leaflet + OpenStreetMap, clique
  para marcar lat/lon — sem chave de API). Ao criar, gera um `entity_id`
  (`urn:ngsi-ld:CV-SHM:<slug>-<aleatório>`, inspirado em NGSI/FIWARE), um
  token e os tópicos MQTT — mostrados como um trecho pronto para colar no
  `edge/.env` daquele Raspberry ("Ver credenciais" na listagem).

**Importante — escopo desta etapa:** é só cadastro. O pipeline ao vivo
(`server/borda.py`) continua falando com **um** Raspberry por vez, do jeito
que já está em produção — nada aqui muda telemetria, detecção ou stream. Um
dispositivo cadastrado não passa a "funcionar" sozinho; a próxima etapa é
ensinar o pipeline a rotear por token, permitindo N dispositivos
simultâneos. Por isso a listagem não tem "online/offline" de verdade ainda.

Cada usuário só vê e só pode excluir os próprios dispositivos; admin vê e
mexe em todos. Localidades e o modelo 3D em si continuam catálogo comum,
sem dono.

### 5.1 Modelo 3D

O `.glb` de fotogrametria vem comprimido com Draco. O Three.js decodifica no
navegador, mas o `trimesh` (usado no raycasting) tem histórico de
incompatibilidade. Descomprima uma vez:

```bash
cp /caminho/Processamento-1-Oiticica-textured_model.glb static/model.glb
bash prepare_model.sh
```

### 5.2 Georreferenciamento

Em `server/glb_geo.py`, os valores saem do `odm_georeferencing_model_geo.txt`
do projeto ODM/WebODM:

```python
UTM_ZONE = 24
UTM_HEMISPHERE_SOUTH = True
GEO_OFFSET_X = 707543.0    # linha 2, valor 1
GEO_OFFSET_Y = 9319434.0   # linha 2, valor 2
MODEL_UP_AXIS = "Z"        # exports de fotogrametria costumam ser Z-up
```

### 5.3 Configurar e rodar

```bash
cp .env.example .env
nano .env      # DATABASE_URL (§5.1a), CONTROLLER_URL e CONTROLLER_PUBLIC_URL com o IP do Pi
python server.py
```

Procure na saída as linhas `>> Modulo de autenticacao instalado` e
`>> Modulo de borda instalado`. Se a segunda não aparecer, o `borda.py` não
foi carregado e as rotas `/api/edge/*` não existem.

> `load_dotenv` **não** sobrescreve o que já está no ambiente, então
> `PAN_SIGN=1 python server.py` continua funcionando para um teste pontual sem
> editar arquivo. O carregamento acontece **antes** do `import glb_geo`, porque
> esse módulo lê `PAN_SIGN` e `TILT_SIGN` no momento em que é importado.

### 5.4 Verificar

`/api/borda` (como quase toda rota do painel, desde a etapa Configuração)
exige sessão — sem cookie, a resposta é `401`. Para testar por `curl`, logue
primeiro e reaproveite o cookie:

```bash
curl -s -c /tmp/cookies.txt -X POST localhost:8001/api/login \
     -H 'Content-Type: application/json' \
     -d '{"username": "admin", "senha": "hydroconecta"}'

curl -s -b /tmp/cookies.txt localhost:8001/api/borda | python3 -m json.tool
```

Com `online: true` e `relatado.fps` preenchido, o Pi está conversando com o
servidor. No navegador, acesse `http://SERVIDOR:8001` — o painel redireciona
sozinho para `/login` se não houver sessão. Depois de entrar:

- a telinha nasce **desligada**, hachurada;
- as pastilhas mostram `borda: http` em verde, o fps e a latência da NPU;
- clicar na telinha inicia o vídeo com contador regressivo; clicar de novo encerra;
- deixando rodar, o stream para sozinho ao fim de 60 s;
- mover o PTZ renova a janela — quem dirige a câmera está olhando;
- o canto superior direito mostra o usuário logado, com atalhos para
  **Configuração** (tema, senha, usuários) e **Sair**.

---

## 6. Compilar o modelo (notebook)

O YOLO26 usa um cabeçalho sem NMS, com operadores (`TopK`, `GatherElements`,
`ReduceMax`, `Mod`) que o compilador da Hailo não aceita. A solução é dividir o
modelo: o backbone vira `.hef` e roda na NPU; o cabeçalho fica como `.onnx` e
roda no CPU do Pi.

```bash
cd ~/hailo_workspace_2 && source hailo_venv/bin/activate

python exportar_onnx.py
python diagnosticar_corte.py   2>&1 | tee diagnosticar_corte.txt
python diagnosticar_model23.py 2>&1 | tee diagnosticar_model23.txt
python corte_definitivo.py     2>&1 | tee corte_definitivo.txt
python parsear_backbone.py     2>&1 | tee parsing.txt

tmux new -s quant                       # nível 2 leva de 1 a 3 h com GPU
python quantizar_compilar_v2.py 2>&1 | tee compilacao_v2.txt

python diagnosticar_mapeamento.py 2>&1 | tee mapeamento.txt
scp best_backbone.hef best_head.onnx PI:~/Projetos/dashboard_oiticica_test/edge/
```

> **Trocar o tamanho do modelo invalida duas constantes.** O `CORTE_IDX` e os
> nomes das saídas do HEF (`conv39`, `conv48`, …) derivam da arquitetura
> compilada. Ao mudar de nano para medium ou large, todos os passos acima
> precisam ser refeitos. Entre **nano e small o `CORTE_IDX` permanece 215**,
> porque as duas escalas têm a mesma profundidade e diferem só na largura.

**Calibre somente com imagens originais.** Imagem aumentada (espelhada,
rotacionada, com brilho alterado) não representa o que a câmera vê, e calibrar
com elas desloca as estatísticas de ativação para uma distribuição que não
existe em campo.

---

## 7. Calibração e avaliação do modelo

O limiar medido no `best.pt`, em float32, **não transfere** para o modelo
quantizado. O que roda no Pi é outro modelo: backbone INT8 com ruído de
quantização. Chutar um valor menor "para compensar" não é calibração.

> Use o split de **validação**, nunca o de teste. Escolher um hiperparâmetro
> olhando o conjunto de teste contamina a avaliação final.

```bash
sudo systemctl stop agente-borda
cd ~/Projetos/dashboard_oiticica_test/edge && source venv/bin/activate

python calibrar_limiar_int8.py --dataset ~/Projetos/datasets/valid
python conferir_deteccao.py    --dataset ~/Projetos/datasets/valid --n 8
python avaliar_mascaras.py --motor hailo --dataset ~/Projetos/datasets/valid \
       --conf 0.45 --saida mascaras_int8.json
```

O critério F2 pesa o recall quatro vezes mais que a precisão, o que faz sentido
em inspeção de barragem. Mas atenção ao formato da curva: se ela for plana numa
faixa larga, o F2 escolhe o extremo inferior por artefato. Prefira um **patamar
estável**, onde dois limiares vizinhos produzem exatamente os mesmos acertos e
erros.

### O limiar existe em dois lugares

`CONF_THRESHOLD` no `edge/.env` é apenas o **valor inicial**. Assim que o
servidor envia o primeiro estado desejado, ele é substituído pelo
`inferencia.conf` definido em `server/borda.py`. Alinhe os dois, ou o Pi volta
ao padrão em menos de um segundo.

```bash
# runtime, vale até o servidor reiniciar
curl -X POST http://SERVIDOR:8001/api/inferencia \
     -H 'Content-Type: application/json' -d '{"conf": 0.45}'
```

---

## 8. Como funciona

### Movimentação PTZ

Os botões usam **ContinuousMove**: a câmera move enquanto o botão está
pressionado e para ao soltar (setas do teclado e `+`/`−` também funcionam).

O agente não executa comandos direto nos endpoints. Eles registram uma
**intenção de movimento com prazo de validade**, e uma única thread
(`PTZMotion`) a compara com o estado aplicado e emite ContinuousMove/Stop. Isso
elimina a corrida entre `/continuous` e `/stop` que fazia a câmera girar sem
parar, e garante parada automática (~800 ms) se o navegador travar, a aba fechar
ou a rede cair. O dashboard renova a intenção a cada 300 ms.

O dashboard fala **direto** com o agente na 8090 (CORS liberado), com fallback
automático pelo proxy do server se isso falhar.

### Cone de visão

O server dispara um leque de raios reais contra a malha (1 central + 24 no anel
do campo de visão) e devolve o contorno onde a visão encosta no objeto. O cone
termina exatamente na parede e se molda ao relevo dela, em vez de flutuar. A
abertura acompanha o zoom.

### Telinha sob demanda

A imagem **não** começa sozinha. Enquanto ninguém clicar, o Raspberry manda só
metadados. Ao clicar, o servidor registra "quero stream por 60 s" no estado
desejado; o Pi passa a publicar quadros JPEG; clicar de novo cancela. O servidor
guarda o último quadro e avisa o navegador por WebSocket, que então o busca em
`/api/stream/atual.jpg`.

### Ciclo de vida dos alertas

Cada detecção nasce como `pendente` e ganha uma marcação fixa no modelo 3D.
Enquanto estiver pendente, a câmera pode continuar apontando para a mesma
fissura sem gerar alertas novos: o server compara a **distância 3D real** entre
os pontos de impacto e apenas incrementa o contador de reincidência.

No alerta ("Abrir"):

- **Imagem completa** — pedida automaticamente ao Pi assim que o modal abre
  (nenhum clique extra); a imagem aparece sozinha quando chega, tipicamente
  em menos de 1 s. "Ver original"/"Ver máscara" alternam o mesmo frame com e
  sem a máscara de segmentação desenhada por cima (a partir de `poly`, sem
  precisar de uma segunda imagem). Se a evidência já tiver sido apagada no Pi
  (teto de disco) ou nunca gravada (deteccão não virou alerta novo), o modal
  mostra o motivo e oferece "Solicitar novamente".
- **Reconhecer** — descreve a ação tomada; check verde no histórico, texto
  registrado com a data, marcação 3D some.
- **Falso positivo** — selo amarelo, marcação some, imagens movidas para
  `server/history/falsos_positivos/`, separadas para refino do modelo.
- **Localizar** — devolve a câmera física à pose exata e leva a visão 3D ao
  ponto. O `/api/locate` **recalcula** o ponto a partir do pan/tilt/zoom
  gravados, então detecções antigas se corrigem sozinhas se a calibração mudar.

Tratar um alerta **não impede** que ele reapareça: passado o rearme, uma nova
detecção no mesmo ponto abre alerta novo — que é justamente o sinal de que o
problema não foi resolvido. Se o ponto já tinha sido julgado falso positivo, a
nova marcação nasce em **amarelo**.

### Evidências no Raspberry

`edge/evidencias/` guarda o frame CRU (sem nada desenhado em cima) em
resolução plena, nomeado com o `det_id`, na qualidade `EVIDENCIA_JPEG_Q`
(85 por padrão) — a máscara é desenhada depois, no navegador, a partir das
coordenadas que já viajaram com a detecção. Só é gravado quando o servidor
confirma que a detecção virou um alerta NOVO (não duplicata, não dentro do
rearme): é o que impede a pasta de encher rápido com evidência de
reincidências que nunca abrem alerta distinto. Ainda assim, uma thread apara
as mais antigas quando a pasta ultrapassa `EVIDENCIAS_MAX_MB` (2 GB por
padrão), para o cartão não encher sozinho.

### Close por seleção (Shift + arrastar)

Segure Shift e arraste um retângulo sobre o modelo 3D. O dashboard faz
raycasting em 9 pontos da região, manda centro e cantos ao `/api/aim`, e o
server converte em pan/tilt (inverso exato da rotação) e no zoom cujo
meio-ângulo cobre a seleção com 30% de folga.

---

## 9. Transporte: HTTP e MQTT

Para testar o modo MQTT antes de existir o ThingsBoard, use um broker local:

```bash
sudo apt install -y mosquitto mosquitto-clients

# servidor
MQTT_BRIDGE=true MQTT_HOST=127.0.0.1 DEVICE_ID=oiticica-cam-01 python server.py

# edge/.env do Pi
MQTT_HOST=<ip-do-servidor>
MQTT_TOKEN=qualquer-coisa-no-teste
```

A troca de transporte continua sendo só uma chamada a `/api/transporte`
(o Pi recebe pelo canal atual e troca a quente: a ida HTTP→MQTT desce por
HTTP, e a volta desce por MQTT) -- só não tem mais botão para isso no painel
da câmera (`server/static/dashboard.html`), porque esse painel virou um
*widget* de um dispositivo CV-SHM: a ideia é que o transporte se decida uma
vez, no cadastro do dispositivo (painel de administração ainda a construir,
§16), não a cada sessão de operação.

```bash
curl -X POST http://SERVIDOR:8001/api/transporte \
     -H 'Content-Type: application/json' -d '{"transporte": "mqtt"}'
```

**O token nunca desce do servidor pela rede.** Ele mora no `edge/.env`. O
servidor só diz "use MQTT".

**Rede de segurança:** se o Pi ficar em MQTT e o broker sumir por mais de
`MQTT_FALLBACK_SEGUNDOS` (30 s), ele volta sozinho para HTTP. Sem isso, um
clique em MQTT com o broker fora do ar deixaria o Pi mudo e sem caminho de
volta — só ida à barragem resolveria.

No ThingsBoard definitivo, o estado desejado vira **atributo compartilhado** do
dispositivo, definido pela REST API. A telemetria e as detecções já saem no
formato nativo (`{"ts", "values"}`). **O quadro de vídeo não deve ir por
telemetria**: cada mensagem é persistida no banco, e gravar 4 JPEG por segundo
encheria o disco a troco de nada. O agente publica num tópico próprio, efêmero
(`MQTT_TOPICO_FRAME`, QoS 0, sem retain).

---

## 10. Referência de API

### Agente, no Raspberry (porta 8090)

| Rota | Método | Função |
|---|---|---|
| `/status` | GET | pan, tilt, zoom, transporte, fps, se está transmitindo |
| `/command/continuous` | POST | movimento contínuo com prazo de validade |
| `/command/stop` | POST | interrompe o movimento |
| `/command/absolute` | POST | move para pan/tilt/zoom absolutos |
| `/command/home` | POST | retorna ao ponto zero |
| `/borda/estado` | POST | atalho de baixa latência para o estado desejado |
| `/borda/preview.jpg` | GET | quadro único, para diagnóstico local |
| `/video_feed` | GET | MJPEG, apenas para diagnóstico na LAN |

### Servidor (porta 8001)

Colunas **Sessão**: rotas do dispositivo (Pi/`controller.py`) nunca exigem
login -- não têm navegador nem cookie. Todo o resto exige sessão válida
(ver §5.1a); sem ela, `/api/*` responde `401` e páginas HTML redirecionam
para `/login`.

| Rota | Método | Sessão | Função |
|---|---|---|---|
| `/api/edge/telemetria` | POST | não | telemetria do Pi; responde com o estado desejado |
| `/api/edge/deteccao` | POST | não | evento de detecção (só coordenadas) |
| `/api/edge/frame` | POST | não | quadro JPEG cru (não base64) |
| `/api/edge/imagem` | POST | não | evidência completa pedida pelo operador |
| `/api/telemetry`, `/api/detection` | POST | não | mesma função acima, usadas pelo `controller.py` |
| `/api/stream/start` \| `renovar` \| `stop` | POST | sim | janela de vídeo de 60 s |
| `/api/stream/atual.jpg` | GET | sim | último quadro recebido |
| `/api/transporte` | POST | sim | alterna entre `http` e `mqtt` (sem botão no painel, ver §9) |
| `/api/inferencia` | POST | sim | ajusta limiares em runtime |
| `/api/borda` | GET | sim | painel de estado da borda |
| `/api/aim` \| `/api/locate` | POST | sim | close por seleção; revisitar detecção |
| `/api/detection/{id}/pedir_imagem` | POST | sim | pede a foto completa ao Pi ("Abrir") |
| `/login`, `/api/login` | GET/POST | não | tela e endpoint de entrada |
| `/api/logout` | POST | sim | encerra a sessão atual |
| `/config` | GET | sim | tema, própria senha, administração de usuários |
| `/api/usuarios/me` | GET | sim | dados do usuário logado |
| `/api/usuarios/me/senha` \| `/tema` | POST | sim | própria senha / próprio tema |
| `/api/usuarios` | GET/POST | admin | listar / criar usuários |
| `/api/usuarios/{id}/redefinir_senha` | POST | admin | força troca de senha de outro usuário |
| `/api/usuarios/{id}` | DELETE | admin | exclui usuário (nunca a si mesmo nem o último admin) |
| `/dispositivos` | GET | sim | tela de cadastro de localidades/dispositivos |
| `/api/localidades` | GET/POST | sim | listar / criar localidade |
| `/api/localidades/{id}` | GET/DELETE | sim | obter / excluir localidade |
| `/api/localidades/{id}/modelo` | POST | sim | upload do `.glb` (multipart); descomprime Draco em segundo plano |
| `/api/dispositivos` | GET/POST | sim | listar (próprios; todos se admin) / criar dispositivo |
| `/api/dispositivos/{id}` | DELETE | sim | excluir (só o dono ou admin) |

### Payloads

Telemetria (~197 B, a cada 1 s; 0,15 s durante o movimento):

```json
{"ts": 1756300000000,
 "values": {"pan": 12.4, "tilt": -5.1, "zoom": 30.0, "movendo": false,
            "fps": 4.8, "npu_ms": 11.2, "cpu_ms": 8.1, "det_total": 37,
            "conf": 0.45, "stream": false, "transporte": "http",
            "cpu_temp": 61.2}}
```

Detecção (~480 B; NUNCA carrega imagem, só coordenadas -- `bbox`/`poly` são a
posição da fissura no frame, e `pan`/`tilt`/`zoom` a pose da câmera. É a
partir daí que o servidor faz o raycasting e mostra a posição real da
detecção em UTM no dashboard; ver `_utm_de` em `server/server.py`):

```json
{"ts": 1756300042000,
 "values": {"evt": "deteccao", "det_id": "9f3c...", "ts_iso": "...",
            "pan": 12.4, "tilt": -5.1, "zoom": 30.0,
            "n": 2, "conf_max": 0.71, "conf_media": 0.63,
            "area_px": 1840, "area_frac": 0.0034,
            "bbox": "[[610,240,688,301]]", "poly": "[[[0.47,0.33]]]",
            "frame_w": 1280, "frame_h": 720,
            "modelo": "best_backbone.hef", "limiar": 0.45,
            "evidencia_local": true}}
```

A foto completa só sobe se o operador clicar em "Abrir" no dashboard, pelo
mesmo mecanismo de sempre (`pedidos_imagem` no estado desejado -> Pi publica
via `/api/edge/imagem`). O Pi só grava o frame em `edge/evidencias/` quando o
servidor confirma que virou um alerta NOVO (`status: "ok"`, não duplicata nem
dentro do rearme) -- é o que evita a pasta encher rápido com evidência de
reincidências que nunca geram um alerta distinto.

`bbox` e `poly` viajam como **string JSON** de propósito: o ThingsBoard indexa
bem escalares e strings, mas trata mal arrays aninhados dentro de `values`. O
contorno da máscara são até 64 pares de números (~1 KB) em vez de dezenas de KB
de JPEG — é o próprio `poly` que o dashboard desenha em cima da foto no "Ver
máscara" (não existe uma segunda imagem com a máscara desenhada, ver §8).

Estado desejado (servidor → Pi):

```json
{"versao": 12, "transporte": "http",
 "stream": {"ativo": true, "restante_s": 47.3, "fps": 4,
            "largura": 640, "qualidade": 60, "anotado": true},
 "inferencia": {"conf": 0.45, "iou": 0.45, "intervalo_frames": 5, "cooldown_s": 5},
 "pedidos_imagem": []}
```

O servidor envia o **tempo que falta**, nunca um horário absoluto: assim nada
depende de o relógio do Pi estar sincronizado. O campo `versao` evita que um
estado antigo, chegando fora de ordem, sobrescreva um mais novo.

---

## 11. Variáveis de ambiente

### `edge/.env` — agente de borda

Obrigatórias: `CAMERA_IP`, `ONVIF_USER`, `ONVIF_PASSWORD`, `RTSP_URL`.

| Variável | Padrão | Para quê |
|---|---|---|
| `SERVER_URL` | `http://127.0.0.1:8001` | servidor do dashboard, no modo HTTP |
| `MQTT_HOST` / `MQTT_TOKEN` | vazias | broker; o token nunca desce do servidor |
| `MQTT_FALLBACK_SEGUNDOS` | 30 | tempo mudo antes de voltar para HTTP |
| `TRANSPORTE_INICIAL` | `http` | transporte usado antes do primeiro estado |
| `HEF_PATH` / `HEAD_ONNX_PATH` | `best_backbone.hef` / `best_head.onnx` | modelo |
| `MAPA_HEF_PARA_ONNX` | mapa do nano/small | saídas do HEF → entradas do ONNX |
| `CONF_THRESHOLD` | 0.15 | limiar **inicial** (o servidor sobrescreve) |
| `IOU_THRESHOLD` | 0.45 | IoU do NMS |
| `INFERIR_A_CADA_N_FRAMES` | 5 | um em cada N quadros passa pelo modelo |
| `COOLDOWN_DETECCAO_S` | 5 | intervalo mínimo entre alertas |
| `EVIDENCIA_JPEG_Q` | 85 | qualidade do frame gravado em `edge/evidencias/` |
| `EVIDENCIAS_MAX_MB` | 2048 | teto da pasta de evidências |
| `STREAM_FPS` / `STREAM_LARGURA` | 4 / 640 | vídeo sob demanda |
| `STREAM_TTL_S` | 75 | teto absoluto do stream, do lado do Pi |
| `PAN_DEG_RANGE` / `TILT_DEG_RANGE` | 180 / 90 | curso mecânico real, em graus |
| `API_PORT` | 8090 | porta da API local do agente |
| `OPENCV_FFMPEG_CAPTURE_OPTIONS` | — | força TCP no RTSP (ver §4.3) |

### `server/.env` — servidor

| Variável | Padrão | Para quê |
|---|---|---|
| `DATABASE_URL` | ver `.env.example` | conexão PostgreSQL (usuários/sessões, §5.1a) |
| `SESSION_COOKIE_SECURE` | `false` | `true` só atrás de HTTPS de verdade (reverse proxy) |
| `SESSAO_DURACAO_H` | 168 (7 dias) | validade do cookie de sessão |
| `CONTROLLER_URL` | `http://127.0.0.1:8090` | servidor → Pi |
| `CONTROLLER_PUBLIC_URL` | = acima | navegador → Pi (PTZ direto) |
| `STREAM_JANELA_S` | 60 | duração do pedido de vídeo |
| `STREAM_FPS` / `STREAM_LARGURA` / `STREAM_QUALIDADE` | 4 / 640 / 60 | parâmetros pedidos ao Pi |
| `PAN_SIGN` / `TILT_SIGN` | -1 / 1 | sentido de rotação |
| `CAMERA_ABS_ALT` | — | elevação absoluta da lente |
| `CONE_HALF_ANGLE_WIDE` / `_TELE` | 18 / 2 | abertura do cone em graus |
| `CONE_RING_RAYS` | 24 | raios do anel (16 é mais leve) |
| `DEDUP_RAIO_M` | 1.5 | raio 3D para considerar a mesma fissura |
| `DEDUP_ANG_DEG` | 1.5 | tolerância angular sem ponto 3D nos dois lados |
| `REARME_SEGUNDOS` | 600 | antes de um ponto tratado reabrir alerta |
| `MQTT_BRIDGE` / `MQTT_HOST` / `MQTT_PORT` | false / 127.0.0.1 / 1883 | ponte MQTT para testes |
| `DEVICE_ID` | `oiticica-cam-01` | identificador do dispositivo |

### `controller/.env` — ambiente de teste sem Pi

Mesmas credenciais de câmera, mais `SERVER_URL`, `YOLO_CONF_THRESHOLD` (0.558)
e `DETECTION_COOLDOWN_SECONDS` (5).

---

## 12. Operação diária

| Comando | Quando |
|---|---|
| `sudo systemctl restart agente-borda` | depois de editar o `.env` ou o código no Pi |
| `sudo systemctl stop agente-borda` | antes de rodar script à mão (libera porta e Hailo) |
| `sudo journalctl -u agente-borda -f` | acompanhar em tempo real (Ctrl+C sai) |
| `sudo journalctl -u agente-borda -b` | somente o boot atual |
| `curl -s --max-time 5 localhost:8090/status` | estado do agente |
| `curl -s localhost:8001/api/borda` | estado visto pelo servidor |

---

## 13. Resolução de problemas

Casos reais da implantação, com a causa e não apenas a solução.

**`ONVIFError: No such file: .../wsdl/devicemgmt.wsdl`** — o `setup.py` do
`onvif-zeep 0.2.12` tem um caminho com `python3.4` cravado no código, então o
pip deposita os WSDL numa pasta de uma versão de Python que não existe no
sistema, enquanto o `client.py` os procura ao lado do pacote instalado. Rode
`bash pos_instalar.sh` — devem ficar ~33 arquivos (WSDL mais os XSD).

**`Errno 98: address already in use` (porta 8090)** — há outro processo na
porta: quase sempre uma execução manual esquecida, ou o serviço rodando junto.
`sudo ss -lptn 'sport = :8090'` e mate o PID.

**`terminate called without an active exception` / `signal=ABRT`** — o HailoRT
aborta quando o pipeline é fechado enquanto a thread de vídeo ainda está dentro
de um `infer()`. Já corrigido: o `agente_borda.py` sinaliza a parada com
`parar_tudo` e espera a inferência sair antes de fechar.

**`MAPA_HEF_PARA_ONNX incompativel`** — os nomes das saídas do HEF são numerados
pela posição na malha compilada e podem mudar ao recompilar. A validação
acontece na inicialização, de propósito, para falhar em 3 segundos em vez de
depois de uma semana de detecção ruim. A própria mensagem lista os nomes reais
dos dois lados.

**`cu_qp_delta ... outside the valid range`** — decodificador HEVC recebendo
bits danificados: RTSP por UDP atravessando VPN. Além de degradar a imagem, cria
bordas artificiais que o detector interpreta como fissura. Force TCP (§4.3).

**`curl` em `/status` trava o terminal e o agente para de responder** —
deadlock: `est.streaming()` adquiria o mesmo lock já segurado pelo endpoint. Um
`threading.Lock` não é reentrante, então a thread esperava por si mesma e o
`est.lock` nunca era liberado, congelando também o vídeo e a telemetria. Já
corrigido: o `/status` resolve o que precisa de lock **antes** do `with`, e o
`est.lock` passou a ser `RLock` como rede de segurança.

**`Connection refused` ao enviar telemetria** — o servidor não está no ar, ou o
`SERVER_URL` aponta para o endereço errado. O agente reclama e continua
funcionando, o que é o comportamento desejado.

**`apt install nodejs npm` falha com `nodejs : Conflita: npm`** (ou uma lista
enorme de `Depende: node-*`) — você já tem Node instalado por outra via (o mais
comum: o repositório da NodeSource, `.../nodesource1` no nome do pacote). O
pacote `nodejs` da NodeSource já vem com o npm embutido; o pacote `npm`
*separado* do Debian/Ubuntu espera um `nodejs` diferente (o da própria
distribuição) e os dois brigam. **Não precisa instalar nada**: confira se já
funciona com `node -v && npx -v` — se responder uma versão, a aba
Dispositivos já consegue descomprimir o `.glb` enviado. `install_desktop.sh`
já pula esse passo sozinho quando detecta `npx` funcionando.

**`git pull` recusa: `untracked working tree files would be overwritten`** —
arquivos criados localmente com o mesmo nome dos que chegam do repositório.
Mova-os para fora, dê o `pull`, e compare com `diff -q`.

**`Everything up-to-date` no push, mas há mudanças locais** — as mudanças não
foram commitadas. O push envia commits, não arquivos modificados. Confirme com
`git rev-parse HEAD origin/main`.

**Autenticação do GitHub recusada com a senha da conta** — não é aceita desde
2021. Gere um Personal Access Token (Settings → Developer settings → Tokens
classic, escopo `repo`) e use-o no lugar da senha; o usuário é o login do
GitHub, não o e-mail.

---

## 14. Resultados medidos

Split de validação: 113 imagens, 127 instâncias anotadas, pareamento por IoU de
caixa ≥ 0,5. Curvas completas em `resultados/`.

### Detecção — float32 contra INT8

| Caminho | Limiar | VP | FP | FN | Precisão | Recall | F1 |
|---|---|---|---|---|---|---|---|
| float32 (`.pt`) | 0,39 | 115 | 12 | 12 | 0,906 | 0,906 | 0,906 |
| INT8 (HEF+ONNX) | 0,45 | 114 | 7 | 13 | 0,942 | 0,898 | 0,919 |

A diferença de F1 é de +0,014 a favor do INT8, o que **não** autoriza concluir
que a quantização melhorou o modelo: uma única instância vale 0,79% de recall
nesta amostra, e a diferença de falsos positivos é 5 ± 4,4 pela contagem de
Poisson. Está dentro do ruído.

**Conclusão defensável: a perda por quantização INT8 não é mensurável nesta
amostra.** As instâncias nunca detectadas em limiar nenhum são 6 no float32 e 5
no INT8 — a quantização não está perdendo objetos inteiros.

### Segmentação — qualidade da máscara

| Métrica | float32 | INT8 | Diferença |
|---|---|---|---|
| IoU de máscara | 0,651 | 0,592 | −0,059 |
| Dice | 0,785 | 0,737 | −0,048 |
| Recall de pixel | 0,822 | 0,773 | −0,049 |
| Precisão de pixel | 0,759 | 0,714 | −0,045 |
| Razão de área (predita/anotada) | 1,099 | 1,106 | +0,007 |
| IoU de caixa | 0,862 | 0,866 | +0,004 |

O erro padrão aproximado da média de IoU é 0,007, então a queda de 0,059 está a
oito desvios do ruído: −9,1% de IoU de máscara, −6,1% de Dice.

**O achado está no contraste entre as duas últimas linhas.** A quantização não
afeta *encontrar* a fissura (IoU de caixa +0,004), mas degrada *delimitar* onde
ela termina (IoU de máscara −0,059). Faz sentido mecanicamente: a caixa depende
de poucos valores de regressão, enquanto a máscara vem da combinação linear de
32 protótipos seguida de um limiar, e cada coeficiente carrega ruído.

A razão de área é praticamente idêntica nos dois caminhos, e recall e precisão
de pixel caem na mesma proporção: a máscara INT8 não é mais fina nem mais
grossa, é do mesmo tamanho com a borda mais errática. **A degradação entra como
ruído, não como viés**, o que se cancela ao longo de observações repetidas do
mesmo ponto — a melhor situação possível para medição de fissura, já que um
viés sistemático subestimaria a severidade para sempre.

> Ambos superestimam a área anotada em ~10% (razão 1,10). Isso não é
> quantização: é o modelo ou a anotação. Se você derivar largura de fissura da
> máscara, esse fator precisa entrar na calibração.

### Consequência para a arquitetura

Alertar na borda em INT8, medir depois em float32. A evidência em resolução
plena fica no cartão do Pi e sobe sob demanda; quando a medição precisar de
precisão de contorno (laudo, evolução da abertura), roda-se o modelo float32
sobre a imagem guardada, sem NPU e sem pressa. A perda de 9% no contorno passa a
não custar nada, porque só existe no caminho que decide *se* vale a pena olhar.

### Um erro que quase passou por resultado

A primeira calibração devolveu precisão e recall de 2%, com quase 150 falsos
positivos e praticamente nenhum acerto, mesmo com o limiar em 0,80. A assinatura
era incompatível com degradação por quantização: um modelo degradado produz
confiança baixa, e ali havia quase uma centena de caixas acima de 0,8 que nunca
encostavam num rótulo.

A causa era a **convenção da caixa**. O tutorial da Hailo documenta `output0`
como `cx, cy, w, h`, mas a exportação sem NMS do Ultralytics entrega
`x1, y1, x2, y2` já em pixels. Ler xyxy como cxcywh produz uma caixa centrada no
canto superior esquerdo do objeto: visualmente próxima, com confiança alta, e
com IoU típico entre 0,15 e 0,28.

| Interpretação de `output0` | IoU médio contra o rótulo | Pares acima de 0,5 |
|---|---|---|
| `cx, cy, w, h` (tutorial) | 0,140 | 0 de 15 |
| `x1, y1, x2, y2` (correta) | 0,900 | 15 de 15 |

A máscara usava as mesmas coordenadas para recortar os protótipos, então também
saía do pedaço errado da imagem — uma correção conserta as duas coisas. Como
efeito colateral, a recomendação do tutorial de baixar o limiar de 0,25 para
0,15 "para compensar a perda da quantização" não se sustenta: o sintoma que a
motivou era erro de decodificação.

`edge/diagnosticar_caixas.py` mede as duas interpretações no mesmo conjunto e
decide qual está correta, sem depender de inspeção visual.

---

## 15. Credenciais e Git

O repositório é **público**: nada de segredo entra no Git, nem uma vez.

```bash
git add -A --dry-run | grep -E "venv|\.hef|\.onnx|/\.env$|\.bak"
# a saída acima precisa ser VAZIA

git log --all -p -S "ONVIF_PASSWORD=" -- . | grep -i "ONVIF_PASSWORD="
git log --all -p -S "MQTT_TOKEN=" -- . | grep -i "MQTT_TOKEN="
# só os marcadores dos .env.example podem aparecer
```

Se uma senha real escapar para um commit, **troque a senha na câmera** — é mais
confiável do que reescrever o histórico, porque num repositório público o valor
já pode ter sido lido ou clonado.

A lat/lon da câmera em `server/server.py` também fica exposta. Se isso for
indesejável, mova para variável de ambiente.

---

## 16. Pendências conhecidas

- **Curso mecânico do PTZ não calibrado.** A câmera reporta pan/tilt
  normalizados (−1..1) e assume-se ±180°/±90°. É propriedade da câmera, não do
  local, e pode ser medida em bancada com `controller/calibrar_curso.py` (com o
  agente parado).
- **Altura da câmera estimada.** O server cai na estimativa por percentil dos
  vértices vizinhos, porque a câmera fica ~36 m além da borda norte do modelo. A
  estimativa (87,07) praticamente coincide com o ponto de malha mais próximo
  (86,63), o que pode significar que ambos estão ancorados no topo da estrutura
  em vez do chão. Erro aqui vira erro sistemático de tilt: ao instalar, meça a
  elevação real e defina `CAMERA_ABS_ALT`.
- **Sentido do pan** corrigido com `PAN_SIGN=-1`, ainda por confirmar em campo.
- **O limiar de confiança existe em dois lugares** (`edge/.env` e
  `server/borda.py`) e o do servidor vence silenciosamente. O correto seria o
  servidor herdar o valor que o Pi reporta na primeira telemetria.
- **`REARME_SEGUNDOS` e a janela de 60 s do stream não conversam:** um alerta em
  rearme pode reabrir enquanto o operador assiste.
- **`onvif_ptz.py` é compartilhado via `sys.path.insert`**, o que esconde a
  dependência de `edge/` em `controller/`. Um diretório `comum/` na raiz tornaria
  a relação explícita.
- **`server/history/index.json` não tem proteção de concorrência** e cresce sem
  limite.
- **`GridHelper`** do dashboard tem cores escuras fixas, que destoam no tema claro.
- **A qualidade da máscara foi medida só em imagens curadas de fissura.** O
  comportamento em cena ampla, com sombra e textura, ainda não foi quantificado.
- **Painel de administração — construção por etapas (3 abas: Dashboard,
  Dispositivos, Configuração).** Estado atual:
  - ✅ **Configuração**: login por sessão (PostgreSQL, `server/db.py` +
    `server/auth.py`), admin padrão `admin`/`hydroconecta` com troca de
    senha obrigatória, papéis admin/usuário, tema movido para cá
    (`server/static/config.html`, `server/static/login.html`).
  - ✅ **Dispositivos** (cadastro, §5.1a-bis): localidades com modelo 3D
    (upload + descompressão Draco em segundo plano) e georreferenciamento
    (N por sistema), dispositivos CV-SHM com localização no mapa (Leaflet/
    OpenStreetMap), transporte HTTP/MQTT e geração de token/tópicos
    (`server/dispositivos.py`, `server/static/dispositivos.html`). Cada
    usuário só vê/exclui os próprios dispositivos; admin vê todos.
    **Ainda não faz** o pipeline ao vivo (`server/borda.py`) reconhecer N
    dispositivos por token — isso continua conversando com UM Raspberry só,
    do jeito que já está em produção; um dispositivo cadastrado é só
    catálogo até essa próxima etapa (rotear por token) existir.
  - ⏳ **Dashboard**: grid de widgets redimensionável, N dashboards por
    usuário, filtro por localidade — ainda não construído (tabela
    `dashboards` já criada, com `layout JSONB`). O
    `server/static/dashboard.html` atual continua sendo a única tela de
    operação; vai virar o primeiro widget (tipo `CV-SHM`) dentro desse
    grid.

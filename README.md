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
│   ├── borda.py                    # estado desejado, endpoints /api/edge/* (autenticados por token), relay de stream
│   ├── registro_dispositivos.py    # registro em memória por dispositivo: pose, GeoModel, stream, estado desejado
│   ├── glb_geo.py                  # georreferenciamento UTM, raycasting, ângulos PTZ (um GeoModel por localidade)
│   ├── db.py                       # PostgreSQL: usuarios/sessoes/localidades/dispositivos/dashboards
│   ├── auth.py                     # login por sessão, middleware de autenticação, admin de usuários
│   ├── dispositivos.py             # cadastro de localidades (modelo 3D) e dispositivos CV-SHM
│   ├── calibracao.py               # resseccão angular: mede a pose real da câmera no modelo (§9-ter)
│   ├── migrar_dispositivo_legado.py  # cadastra o dispositivo/localidade que antes eram hardcoded (§9-bis)
│   ├── prepare_model.sh            # remove compressão Draco do .glb (rodar uma vez, uso manual)
│   ├── static/layout.css           # casca visual comum: menu lateral, barra de título, cartões, tabelas
│   ├── static/layout.js            # monta o menu/barra nas três telas (um lugar só para a navegação)
│   ├── static/dashboard.html       # Three.js: modelo, cone, telinha, histórico, marcações 3D
│   ├── static/login.html           # tela de entrada
│   ├── static/config.html          # tema, própria senha, administração de usuários (admin)
│   ├── static/dispositivos.html    # cadastro/edição + mapa Leaflet + preview 3D (Three.js) do modelo enviado
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
| estado desejado, janela de stream | `server/borda.py` (`EstadoDesejado`, `_vigia_streams`) |
| autenticação por token, registro por dispositivo | `server/registro_dispositivos.py` (`DispositivoRuntime`) |
| raycasting, cone, coordenadas 3D | `server/glb_geo.py` (`GeoModel`, um por localidade) |
| histórico, dedup, WebSocket | `server/server.py` |
| interface, marcações 3D, telinha | `server/static/dashboard.html` |
| menu lateral, barra de título, tema | `server/static/layout.css` + `layout.js` |
| login, sessão, usuários | `server/auth.py` + `server/db.py` |
| localidades, modelos 3D, dispositivos | `server/dispositivos.py` |
| calibração da pose da câmera | `server/calibracao.py` |
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
nano .env      # CAMERA_IP, ONVIF_USER, ONVIF_PASSWORD, RTSP_URL, SERVER_URL, DEVICE_TOKEN
```

`DEVICE_TOKEN` vem do cadastro do dispositivo no servidor (aba
**Dispositivos**, `/dispositivos`, §5.1a-bis, ou `migrar_dispositivo_legado.py`
para a câmera já em produção, §9-bis) — sem ele o agente aborta na
inicialização (`_req()` falha rápido, mesmo padrão de `CAMERA_IP`/`ONVIF_USER`).

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
  fica em **erro**, com a mensagem explicando o que falta. Quando o status
  vira **pronto**, aparece o botão "Visualizar": abre um preview 3D
  (mesmas bibliotecas do `dashboard.html` — Three.js, `GLTFLoader` +
  `DRACOLoader`/`KTX2Loader`/`MeshoptDecoder` — com `OrbitControls` pra
  girar/dar zoom) que carrega `/model/modelos/<id>.glb` e aplica a rotação
  Z-up→Y-up automaticamente quando o `model_up_axis` da localidade é `Z`. É
  só um preview de conferência (modelo carregou? está orientado certo?);
  não tem cone, PTZ ou qualquer coisa ligada a telemetria — isso continua
  exclusivo do widget CV-SHM em `dashboard.html`.
- **Dispositivos** — nome, proprietário (ex.: SEMARH), localidade,
  transporte (HTTP/MQTT) e posição da câmera. A posição pode ser marcada
  **clicando no mapa** (Leaflet + OpenStreetMap, sem chave de API) **ou
  digitada** nos campos de latitude/longitude — os dois são a mesma coisa e
  ficam sincronizados nos dois sentidos, porque nem sempre se acha o ponto
  no mapa e muitas vezes a coordenada já vem pronta do projeto ou do GPS.
  Ao criar, gera um `entity_id` (`urn:ngsi-ld:CV-SHM:<slug>-<aleatório>`,
  inspirado em NGSI/FIWARE), um token e os tópicos MQTT — mostrados como um
  trecho pronto para colar no `edge/.env` daquele Raspberry ("Ver
  credenciais" na listagem).

#### O offset UTM pertence ao MODELO, não ao “lugar”

Cada `.glb` sai da fotogrametria com o seu próprio offset
(`odm_georeferencing_model_geo.txt`): as coordenadas dentro do arquivo são
**UTM menos esse offset**. Dois recortes da mesma parede têm offsets
diferentes — por exemplo, na Barragem Oiticica:

| Recorte | Offset X | Offset Y |
|---|---|---|
| pedaço maior | 707728 | 9319402 |
| pedaço menor | 707543 | 9319434 |

Trocar o `.glb` de uma localidade **sem trocar o offset junto** desloca a
câmera e todas as detecções exatamente pela diferença entre os dois — nesse
par, 185 m em X e 32 m em Y, **187,7 m** de erro. O modelo aparece
normalmente; o que sai do lugar é a *pose* dentro dele. É o sintoma clássico
de “a detecção e a câmera caem em pontos diferentes”.

Duas formas de trabalhar, as duas corretas:

- **Uma localidade por modelo** (recomendado): “Oiticica — recorte maior” e
  “Oiticica — recorte menor”, cada uma com o seu offset. Trocar de modelo
  vira trocar a localidade do dispositivo em **Editar**, sem mexer em
  número nenhum.
- **Uma localidade só, trocando o `.glb`**: aí, depois de enviar o novo
  arquivo, use **Editar** na localidade e ajuste o offset. O painel avisa
  disso ao substituir um modelo que já existia.

#### Para o dispositivo ter visão 3D

São **três** condições, e a coluna “Localidade” da listagem diz qual está
faltando em cada dispositivo (em vez de o painel mostrar as três de uma vez
e deixar o operador adivinhar):

1. uma **localidade** associada ao dispositivo;
2. **latitude/longitude** da câmera preenchidas;
3. o **`.glb` daquela localidade** com status **pronto**.

Faltando qualquer uma, o dispositivo continua funcionando em PTZ e vídeo —
só a visão 3D fica indisponível, sem *fallback* para a geometria de outro
dispositivo (§9-bis).

#### Editar em vez de recriar

O botão **Editar** na listagem altera localidade, lat/lon, altura,
transporte e URL do controlador de um dispositivo **que já existe**. Use-o
sempre que faltar alguma das três condições acima: `PATCH` não mexe no
token nem nos tópicos, então o Raspberry que já está em campo continua
funcionando com o `.env` que ele já tem. **Excluir e recriar geraria um
token novo** e derrubaria esse Raspberry até alguém ir lá trocar o `.env`.

Excluir um dispositivo invalida o token **na hora**, sem esperar reinício do
servidor.

Cada usuário só vê, edita e exclui os próprios dispositivos; admin vê e
mexe em todos. Localidades e o modelo 3D em si continuam catálogo comum,
sem dono.

### 5.1 Modelo 3D e georreferenciamento

**Desde a etapa multi-dispositivo, isto não é mais feito editando arquivo
nenhum** (nem `static/model.glb` nem constantes em `server/glb_geo.py` —
essas constantes não existem mais; cada localidade tem seu próprio
`GeoModel`, montado em runtime a partir do que está cadastrado no banco). Há
dois caminhos:

- **Instalação nova / localidade nova:** cadastre pela aba **Dispositivos**
  (`/dispositivos`, §5.1a-bis) — crie a localidade preenchendo zona UTM,
  offset X/Y/Z e eixo "para cima", depois envie o `.glb` de fotogrametria.
  O servidor descomprime o Draco sozinho (mesma ferramenta que o antigo
  `prepare_model.sh` chamava manualmente, agora automática).
  Os valores de zona UTM/offset saem do `odm_georeferencing_model_geo.txt`
  do projeto ODM/WebODM: zona e offset X/Y na linha 2, eixo "para cima" é
  `Z` na maioria dos exports de fotogrametria.
- **Servidor já em produção com a Barragem Oiticica** (o `.glb` já está em
  `static/model.glb`, já descomprimido): rode
  `python3 migrar_dispositivo_legado.py` (§9-bis) — ele cadastra a
  localidade reaproveitando esse arquivo, sem reenviar nada.

`prepare_model.sh` continua existindo só para descomprimir Draco à mão fora
do fluxo do painel (por exemplo, para inspecionar um `.glb` antes de
enviá-lo).

### 5.3 Configurar e rodar

```bash
cp .env.example .env
nano .env      # DATABASE_URL (§5.1a); CONTROLLER_URL/CONTROLLER_PUBLIC_URL viraram
               # so um FALLBACK -- cada dispositivo pode ter o seu proprio
               # controller_url cadastrado em /dispositivos
python server.py
```

Depois de subir o servidor, cadastre a localidade/dispositivo (§5.1a-bis) ou,
se for o Raspberry já em produção, rode a migração (§9-bis) — sem isso não
existe token, e todo dispositivo é rejeitado com `401`.

Procure na saída as linhas `>> Modulo de autenticacao instalado` e
`>> Modulo de borda instalado`. Se a segunda não aparecer, o `borda.py` não
foi carregado e as rotas `/api/edge/*` não existem.

> `load_dotenv` **não** sobrescreve o que já está no ambiente, então
> `PAN_SIGN=1 python server.py` continua funcionando para um teste pontual sem
> editar arquivo. O carregamento acontece **antes** do `import glb_geo`, porque
> esse módulo lê `PAN_SIGN` e `TILT_SIGN` no momento em que é importado.

### 5.4 Verificar

`/api/borda` (como quase toda rota do painel, desde a etapa Configuração)
exige sessão — sem cookie, a resposta é `401`. Também exige `device_id`
desde a etapa multi-dispositivo (§9-bis) — pegue o `id` em `/api/dispositivos`.
Para testar por `curl`, logue primeiro e reaproveite o cookie:

```bash
curl -s -c /tmp/cookies.txt -X POST localhost:8001/api/login \
     -H 'Content-Type: application/json' \
     -d '{"username": "admin", "senha": "hydroconecta"}'

curl -s -b /tmp/cookies.txt localhost:8001/api/dispositivos | python3 -m json.tool
# pegue o "id" do dispositivo desejado na saída acima
curl -s -b /tmp/cookies.txt "localhost:8001/api/borda?device_id=<uuid>" | python3 -m json.tool
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

> **O HEF em produção foi gerado com `quantizar_compilar_v2.py`**
> (`optimization_level=2`, 256 imagens de calibração), em 27/08/2026.
> O `quantizar_compilar.py` não declara `model_optimization_flavor` e usa 100
> imagens — fica como caminho rápido para validar o pipeline, não para gerar o
> modelo definitivo. Os dois gravam em `best_backbone.hef`, então o nome do
> arquivo não distingue: confira com
> `grep optimization_level compilacao_v2.txt`.

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

### O que pode (e o que não pode) mudar a detecção

O caminho de visão computacional é **isolado** do trabalho de painel/servidor.
Só três coisas alteram *se* uma rachadura é detectada:

| O quê | Onde | Muda a detecção? |
|---|---|---|
| Pesos do modelo (`best_backbone.hef`, `best_head.onnx`) | só no Pi, **fora do Git** | sim — mas só ao recompilar (§6) |
| Pré/pós-processamento (`letterbox`, decodificação, `nms`) | `edge/inferencia_hailo.py` | sim |
| Limiar de confiança (`conf`) | `edge/.env` **e** estado desejado | sim — ver abaixo |
| `contorno_normalizado` (contorno da máscara) | `edge/inferencia_hailo.py` | **não** — é só o desenho da máscara já detectada |
| Servidor, dashboard, cadastro, layout | `server/`, `static/` | **não** |

Os pesos nunca entraram no repositório (`.hef`/`.onnx`/`.pt` estão no
`.gitignore`), então nenhum commit consegue alterá-los: eles só mudam quando
alguém roda o pipeline do §6 e copia os arquivos para o Pi.

Desde que o pipeline de borda foi escrito (28/08), a **única** alteração em
`edge/inferencia_hailo.py` foi em `contorno_normalizado`
(`max_pontos` 24→64, `eps` 0.01→0.003) — uma função que roda **depois** da
detecção, apenas para desenhar o contorno da máscara no dashboard.
`letterbox`, a decodificação das saídas e o `nms` estão byte a byte iguais
ao original, assim como `_jpeg`, `stream_loop` e `aplicar_estado` em
`edge/agente_borda.py`. Para conferir você mesmo:

```bash
git log --oneline -- edge/inferencia_hailo.py
git diff <primeiro-commit> HEAD -- edge/inferencia_hailo.py
```

### O limiar existe em dois lugares

`CONF_THRESHOLD` no `edge/.env` é apenas o **valor inicial**. Assim que o
servidor envia o primeiro estado desejado, ele é substituído pelo
`inferencia.conf` definido em `server/borda.py`. Alinhe os dois, ou o Pi volta
ao padrão em menos de um segundo.

```bash
# runtime, vale até o servidor reiniciar
curl -X POST http://SERVIDOR:8001/api/inferencia \
     -H 'Content-Type: application/json' \
     -d '{"device_id": "<uuid-do-dispositivo>", "conf": 0.45}'
```

---

## 8. Como funciona

### Interface: menu lateral e painel ajustável

As três telas (painel, Dispositivos, Configuração) compartilham a mesma
casca: um **menu lateral** fixo à esquerda, no estilo do ThingsBoard, e uma
barra de título com as ações da tela. A casca mora em
`server/static/layout.css` + `layout.js` — um lugar só, em vez de o mesmo
cabeçalho copiado em três arquivos.

O menu recolhe para só os ícones (botão ☰, ou automaticamente abaixo de
860 px de largura) e a escolha fica salva por navegador.

No painel, a coluna da direita é **ajustável**: arraste o divisor vertical
para alargar a telinha do stream e o horizontal para dar mais espaço ao
histórico. Os dois tamanhos ficam salvos por navegador, então quem trabalha
o dia todo com a telinha grande não reajusta a cada acesso. O canvas 3D
acompanha via `ResizeObserver` (não `window.resize`: arrastar o divisor ou
recolher o menu não redimensiona a janela).

Duas decisões de layout que vieram de problemas reais de operação:

- **A navegação não flutua mais sobre o vídeo.** Antes era uma barra
  `position:fixed` no canto superior direito, que passava por cima do widget
  da câmera e cobria o nome dela. Agora ocupa espaço próprio (menu + barra
  de título), então nada fica escondido.
- **As pastilhas de estado da borda são uma grade 2×2 fixa.** Antes eram
  flex com quebra de linha: quando o fps passava de um dígito
  (`24.5 fps`), a pastilha `vídeo: X KB` caía para uma terceira linha e
  **empurrava o controle PTZ para baixo** — o operador clicava onde o botão
  estava um segundo antes. Com duas linhas sempre, a altura do bloco não
  depende do texto e o PTZ não se move.

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

**A largura do stream acompanha o tamanho da telinha.** O dashboard manda em
`/api/stream/start|renovar` de quantos pixels ele precisa de fato
(largura exibida × `devicePixelRatio`, limitada a
`LARGURA_STREAM_MIN`/`STREAM_LARGURA_MAX` = 320–960), e isso entra no estado
desejado como `stream.largura`. Sem isso, com o painel direito
redimensionável, pedir sempre 640 px fazia o navegador **ampliar** a imagem
justamente quando o operador alargava a telinha para enxergar melhor — o
efeito era o stream “perder qualidade” ao ser aumentado. O Pi só
**reduz** (`if w > largura`), nunca amplia: pedir mais que a resolução da
câmera devolve o nativo, não um upscale artificial.

O teto de 960 px é conservador de propósito: cada quadro maior custa CPU de
codificação **no Pi**, e é a mesma CPU que roda a inferência — um stream
grande demais competiria com a detecção de rachaduras, que é a função
principal do equipamento. Ajustável por `STREAM_LARGURA_MAX`.

**Duas regras que evitam congestionar a rede e travar o PTZ:**

- a janela do stream é renovada **no máximo uma vez a cada 10 s** (ela dura
  60 s), e não a cada tique de PTZ;
- um quadro novo é **descartado** se o anterior ainda estiver baixando —
  vídeo ao vivo quer o quadro mais recente, não uma fila deles.

Sem as duas, segurar um botão de PTZ gerava ~10 requisições por segundo, o
navegador esgotava as conexões simultâneas e os comandos ficavam na fila: a
câmera parava de responder por alguns segundos e depois voltava sozinha.

Se o stream for pedido e nenhum quadro chegar em ~6 s, o painel diz o motivo
provável (dispositivo offline, ou o dispositivo selecionado não é o que está
ligado) em vez de deixar a telinha preta sem explicação.

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

**Isto aqui é só a ponte MQTT de teste, sem token, sempre um único
dispositivo (`DEVICE_ID`) -- não confundir com o pipeline HTTP multi-
dispositivo do §9-bis, que é o caminho de produção hoje.**

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
     -H 'Content-Type: application/json' \
     -d '{"device_id": "<uuid-do-dispositivo>", "transporte": "mqtt"}'
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

## 9-bis. Multi-dispositivo (HTTP): migração obrigatória

Antes desta etapa, o servidor falava com **um único** Raspberry hardcoded
(`CAMERA_LAT`/`CAMERA_LON`/`CAMERA_ALT_ABOVE_GROUND` em `server.py`,
`UTM_ZONE`/`GEO_OFFSET_*`/`MODEL_UP_AXIS` em `glb_geo.py`). Agora cada
chamada HTTP de dispositivo (`/api/edge/*`, e `/api/telemetry`/
`/api/detection` usadas pelo `controller.py`) **exige** o cabeçalho
`Authorization: Bearer <token>`, resolvido contra o cadastro feito em
`/dispositivos` (§5.1a-bis) -- **sem exceção e sem modo de compatibilidade**:
essa foi uma escolha deliberada para não deixar um caminho tokenless
esquecido rodando em produção. Isso inclui o Raspberry que já está no ar: ele
**vai parar de conseguir falar com o servidor** assim que esta atualização
subir, até ser migrado.

### Passo a passo

1. **Atualize o servidor primeiro** (§5), com o banco já migrado (as
   `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` de `server/db.py` rodam
   sozinhas ao iniciar).
2. **Rode o script de migração no servidor**, uma única vez (idempotente --
   rodar de novo não duplica nada):
   ```bash
   cd server
   source venv/bin/activate    # se usar virtualenv
   python3 migrar_dispositivo_legado.py
   ```
   Ele cria (ou reaproveita, se já existir) a localidade "Barragem Oiticica"
   com os mesmos parâmetros geográficos que estavam hardcoded, aponta para o
   `static/model.glb` **já existente** (sem reenviar/redescomprimir nada) e
   marca o modelo como pronto direto. Depois cria o dispositivo legado com
   um token novo e imprime algo como:
   ```
   Cole isto em edge/.env no Raspberry (mantenha o resto do arquivo):

   DEVICE_ID=oiticica-cam-01
   DEVICE_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
3. **No Raspberry**, edite `edge/.env` e cole as duas linhas acima (o
   `DEVICE_ID` já deve bater com o que já está lá). Reinicie o agente:
   ```bash
   sudo systemctl restart agente-borda
   sudo journalctl -u agente-borda -f   # confirme que voltou a mandar telemetria
   ```
4. **Se usar o `controller.py`** (ambiente de desktop sem Pi, §3), edite
   `controller/.env` e adicione o mesmo `DEVICE_TOKEN` (pode reaproveitar o
   dispositivo legado ou cadastrar um novo em `/dispositivos` para o
   controller).
5. Se o Pi ficar em `pill-online: offline` no dashboard depois disso, o
   token está errado/faltando -- confira `edge/.env` e os logs do agente
   (uma tentativa sem token, ou com token inválido, aparece no servidor como
   `401` em `/api/edge/telemetria`).

### Novos dispositivos

Um dispositivo cadastrado em `/dispositivos` **sem** o `migrar_dispositivo_legado.py`
já nasce funcional: o cadastro gera o token, e o operador só precisa colar
`DEVICE_ID`/`DEVICE_TOKEN` no `edge/.env` daquele Raspberry. Se a localidade
dele ainda não tiver modelo 3D pronto (upload/descompressão em andamento, ou
sem cadastro de localidade ainda), o dispositivo funciona normalmente em PTZ
e vídeo -- só a visão 3D fica indisponível até o cadastro da localidade
ficar completo (sem *fallback* para a geometria de outro dispositivo, ver
§16).

### No dashboard

`server/static/dashboard.html` agora mostra um seletor de dispositivo no
canto superior direito (populado por `/api/dispositivos`, respeitando o
mesmo filtro "só os próprios, admin vê todos" do cadastro). A escolha fica
salva por navegador (`localStorage`) e também na URL (`?device_id=...`, para
favoritar/compartilhar um link direto de uma câmera específica). Sem nenhum
dispositivo cadastrado para o usuário logado, a tela mostra um aviso e não
tenta carregar modelo 3D nem vídeo.

---

## 9-ter. Calibrar o gêmeo digital

Sem calibração, a pose da câmera dentro do modelo é **estimada**, e por dois
caminhos frágeis: a posição vem de lat/lon (com erro de GPS) mais uma altura
de terreno estimada, e a orientação — para onde a câmera olha em
`pan=0/tilt=0` — é um **chute**: a direção até o ponto de malha mais
próximo. Não há razão para o norte mecânico da câmera coincidir com isso. O
resultado é o cone e as detecções caindo perto, mas não em cima.

Calibrar é **medir** essa pose em vez de estimá-la.

### “Home” e “ponto zero” são coisas diferentes

Vale fixar isto antes, porque a confusão entre os dois é fácil e as
consequências não são óbvias:

| | O que é | Quem pode mudar |
|---|---|---|
| **Ponto zero** | a origem das coordenadas ONVIF (`pan=0, tilt=0`) — propriedade **mecânica** da câmera (encoder/fim de curso) | ninguém, por software |
| **Home** | uma **posição guardada** na câmera (`GotoHomePosition`), para onde ela volta ao reiniciar; é o que outros sistemas (ex.: **Defense IA** da Intelbras) usam como referência | qualquer software ou técnico, a qualquer momento |

**A geometria do gêmeo digital fica ancorada no ponto zero** — e isso é
deliberado, por dois motivos:

1. **A telemetria é absoluta.** O agente lê a posição da câmera com
   `GetStatus` a cada ciclo; ele nunca acumula deslocamentos a partir de um
   zero assumido na partida. Então **não importa onde a câmera está ao
   ligar**: se ela reiniciou e foi para o home, reporta a posição do home e
   a conta fecha igual. Não há nada a “re-zerar”.
2. **Ancorar no home seria mais frágil, não menos.** O home é uma
   preferência editável: se o Defense IA — ou um técnico no dia seguinte —
   redefinir o home, uma calibração ancorada nele passaria a estar errada
   **em silêncio**. O ponto zero não muda por software.

O que **mudou** nesta etapa, aí sim por causa da câmera compartilhada:

- **O botão “⌂” agora vai para o home de verdade** (`GotoHomePosition`), que
  é o que o operador espera e a mesma referência dos outros sistemas. Antes
  ele fazia `AbsoluteMove(0,0,0)` apesar de se chamar “home”. Quem quiser a
  origem das coordenadas tem `/command/zero`.
- **O agente não mexe mais na câmera ao subir.** Antes, agente e
  `controller.py` mandavam a câmera para o ponto zero a cada inicialização —
  o que, com a câmera compartilhada, **rouba a cena de quem estiver
  usando**, e nunca foi necessário (ver o motivo 1 acima). Para voltar ao
  comportamento antigo: `PTZ_ZERO_AO_INICIAR=true`.

**E se a referência da câmera se mover mesmo assim?** Pode acontecer: um
reinício que perca a referência do encoder, uma troca de ótica, uma
remontagem. Como os pontos da calibração ficam guardados, dá para medir isso
a qualquer momento — botão **Verificar** (`.../calibracao/verificar`): ele
reavalia os pontos contra a pose em vigor, sem recalcular nada, e compara com
o erro do dia da calibração. Em teste, um deslocamento artificial de 2° na
referência aparece como 2,03° de erro; 5° aparece como 4,97°. Erro que
cresceu muito = a referência andou, hora de recalibrar.

### Como funciona

A mira no centro da telinha **é o eixo óptico** da câmera. Então, ao apontar
a câmera para um ponto de referência e marcar o mesmo ponto no modelo 3D,
você cria um par:

```
direção prevista(pan, tilt)  ==  normalizar(ponto_3D − posição_da_câmera)
```

Cada par dá 2 equações (azimute e elevação). O servidor
(`server/calibracao.py`) resolve por mínimos quadrados. O modelo direto é
**exatamente separável** em coordenadas esféricas — conferido numericamente
contra `glb_geo.direction_from_pan_tilt`, desvio < 10⁻¹³ grau:

```
azimute  = az0 + PAN_SIGN  · escala_pan  · pan_reportado
elevação = el0 + TILT_SIGN · escala_tilt · tilt_reportado
```

### O passo a passo

1. Selecione o dispositivo e clique em **Calibrar** (barra de título). A
   mira aparece e **o cone congela** — ele ainda está desenhado com a pose
   errada, e vê-lo pular a cada movimento só atrapalharia a mira.
2. Mova o PTZ até a mira cair sobre um ponto de referência bem
   identificável (quina, canto de bloco, marca na parede).
3. Ache o **mesmo** ponto no modelo 3D e marque com **Ctrl + botão direito**.
   O pan/tilt do momento é gravado junto.
4. Repita em **direções e distâncias variadas** (ver abaixo).
5. **Calcular** mostra o resultado sem gravar nada. **Aplicar** grava e
   recarrega a cena com a pose nova. **Descalibrar** volta para a estimada
   (preservando os pontos).

### Quantos pontos

| Pontos | O que passa a ser resolvido |
|---|---|
| 2 | só a orientação (`az0`, `el0`) — 2 incógnitas. Já corrige o erro dominante |
| 4 | orientação **+ posição** — 5 incógnitas. Mínimo útil |
| 8+ | permite também as escalas de pan/tilt (curso mecânico) — 7 incógnitas |

Erro mediano da posição em simulação, com 0,3° de erro de marcação:

| Pontos | 4 | 8 | 12 | 16 |
|---|---|---|---|---|
| Erro | ~1,1 m | ~0,6 m | ~0,4 m | ~0,4 m |

O joelho da curva fica entre **8 e 12 pontos** — é o que a interface
recomenda.

As escalas **não** entram só por haver pontos suficientes. Se o curso
mecânico já estiver certo, os dois parâmetros a mais só absorvem ruído e a
posição *piora* (0,45 m → 1,10 m, medido). Por isso o modo automático ajusta
os dois modelos e fica com o maior **apenas quando ele explica os ângulos
sensivelmente melhor**. Quando o curso está mesmo errado (testado com
pan ×1,15 e tilt ×0,90), não resolver a escala custa ~9 m de erro; resolvendo,
volta a ~1 m e as escalas são recuperadas com 3 casas.

### Por que “direções e distâncias variadas”

A posição só é observável por **paralaxe**. Pontos alinhados, ou todos na
mesma parede à mesma distância, deixam a posição mal determinada — e o
perigo é que **o RMS continua baixo** nesse caso. Medido: 0,17° de RMS com
**54 m** de erro de posição. RMS mede o quanto o ajuste fecha, não o quanto
os dados restringem cada incógnita.

Por isso o critério de confiança **não é o RMS**, e sim a incerteza
estatística da posição, tirada da covariância `σ²·(JᵀJ)⁻¹`. Ela acompanha o
erro real de perto (medido: ±1,9 m estimados contra 2,1 m reais) e explode
quando a geometria é degenerada (±182 m) — que é exatamente o caso a
reprovar. O painel mostra esse número e avisa o que fazer.

### Onde fica guardado

Os pontos ficam na tabela `calibracao_pontos` (dá para revisar, remover um
ponto ruim e recalcular sem refazer tudo); a pose resolvida vai para as
colunas `calib_*` de `dispositivos`. Enquanto elas forem `NULL`, vale a pose
estimada — calibrar não é obrigatório, e “Descalibrar” volta ao
comportamento anterior a qualquer momento.

---

## 10. Referência de API

### Agente, no Raspberry (porta 8090)

| Rota | Método | Função |
|---|---|---|
| `/status` | GET | pan, tilt, zoom, transporte, fps, se está transmitindo |
| `/command/continuous` | POST | movimento contínuo com prazo de validade |
| `/command/stop` | POST | interrompe o movimento |
| `/command/absolute` | POST | move para pan/tilt/zoom absolutos |
| `/command/home` | POST | vai para o **home guardado na câmera** (cai para o ponto zero se ela não tiver) |
| `/command/zero` | POST | vai para a **origem das coordenadas ONVIF** (pan=0, tilt=0) |
| `/borda/estado` | POST | atalho de baixa latência para o estado desejado |
| `/borda/preview.jpg` | GET | quadro único, para diagnóstico local |
| `/video_feed` | GET | MJPEG, apenas para diagnóstico na LAN |

### Servidor (porta 8001)

Colunas **Autenticação**: rotas de dispositivo (Pi/`controller.py`) exigem
`Authorization: Bearer <token>` (§9-bis) em vez de sessão -- sem token válido,
`401`. Todo o resto exige sessão válida de operador (ver §5.1a); sem ela,
`/api/*` responde `401` e páginas HTML redirecionam para `/login`.

| Rota | Método | Autenticação | Função |
|---|---|---|---|
| `/api/edge/telemetria` | POST | token | telemetria do Pi; responde com o estado desejado |
| `/api/edge/deteccao` | POST | token | evento de detecção (só coordenadas) |
| `/api/edge/frame` | POST | token | quadro JPEG cru (não base64) |
| `/api/edge/imagem` | POST | token | evidência completa pedida pelo operador |
| `/api/telemetry`, `/api/detection` | POST | token | mesma função acima, usadas pelo `controller.py` |
| `/api/dispositivos` | GET/POST | sessão | listar (próprios; todos se admin) / criar dispositivo |
| `/api/dispositivos/{id}` | PATCH | sessão | editar o cadastro (localidade, lat/lon, altura, transporte, controller); **não** troca o token |
| `/api/dispositivos/{id}` | DELETE | sessão | excluir (só o dono ou admin); o token para de valer na hora |
| `/api/dispositivos/{id}/calibracao` | GET | sessão | pontos casados + calibração em vigor (§9-ter) |
| `/api/dispositivos/{id}/calibracao` | DELETE | sessão | volta para a pose estimada (`?apagar_pontos=true` também limpa os pontos) |
| `/api/dispositivos/{id}/calibracao/pontos` | POST | sessão | grava um par (pan/tilt ↔ ponto 3D) |
| `/api/dispositivos/{id}/calibracao/pontos/{pid}` | DELETE | sessão | remove um ponto |
| `/api/dispositivos/{id}/calibracao/resolver` | POST | sessão | calcula e devolve **sem gravar** (prévia) |
| `/api/dispositivos/{id}/calibracao/aplicar` | POST | sessão | calcula, grava a pose e remonta o runtime |
| `/api/dispositivos/{id}/calibracao/verificar` | POST | sessão | a calibração em vigor ainda bate com os pontos guardados? (detecta deriva) |
| `/api/camera_info` \| `/api/view` | GET | sessão | pose/geometria e cone sob demanda -- exige `?device_id=` |
| `/api/stream/start` \| `renovar` \| `stop` | POST | sessão | janela de vídeo de 60 s -- exige `device_id` |
| `/api/stream/atual.jpg` | GET | sessão | último quadro recebido -- exige `device_id` |
| `/api/transporte` | POST | sessão | alterna entre `http` e `mqtt` (sem botão no painel, ver §9) -- exige `device_id` |
| `/api/inferencia` | POST | sessão | ajusta limiares em runtime -- exige `device_id` |
| `/api/borda` | GET | sessão | painel de estado da borda -- exige `?device_id=` |
| `/api/aim` | POST | sessão | close por seleção -- exige `device_id` |
| `/api/locate` | POST | sessão | revisitar detecção -- dispositivo vem da PRÓPRIA detecção salva, não do cliente |
| `/api/detection/{id}/pedir_imagem` | POST | sessão | pede a foto completa ao Pi ("Abrir") -- dispositivo idem `/api/locate` |
| `/login`, `/api/login` | GET/POST | não | tela e endpoint de entrada |
| `/api/logout` | POST | sessão | encerra a sessão atual |
| `/config` | GET | sessão | tema, própria senha, administração de usuários |
| `/api/usuarios/me` | GET | sessão | dados do usuário logado |
| `/api/usuarios/me/senha` \| `/tema` | POST | sessão | própria senha / próprio tema |
| `/api/usuarios` | GET/POST | admin | listar / criar usuários |
| `/api/usuarios/{id}/redefinir_senha` | POST | admin | força troca de senha de outro usuário |
| `/api/usuarios/{id}` | DELETE | admin | exclui usuário (nunca a si mesmo nem o último admin) |
| `/dispositivos` | GET | sessão | tela de cadastro de localidades/dispositivos |
| `/api/localidades` | GET/POST | sessão | listar / criar localidade |
| `/api/localidades/{id}` | GET/DELETE | sessão | obter / excluir localidade |
| `/api/localidades/{id}` | PATCH | sessão | corrigir georreferenciamento (offset UTM, zona, eixo) sem apagar a localidade |
| `/api/localidades/{id}/modelo` | POST | sessão | upload do `.glb` (multipart); descomprime Draco em segundo plano |

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

#### Por que o contorno tem 64 pontos (e não 24)

Enquanto o `poly` só servia para deduplicar detecções, ninguém o via: 24
pontos com `eps = 1 %` do perímetro bastavam. Quando a detecção deixou de
enviar imagem e o "Ver máscara" passou a **desenhar o `poly`** por cima da
foto, essa simplificação virou o que o operador enxerga — e 24 pontos num
contorno de fissura ficam visivelmente grosseiros. Medido numa máscara
sintética de fissura ramificada (IoU contra a máscara original):

| Parâmetros | Pontos | IoU |
|---|---|---|
| 24 pontos, `eps` 1 % | ~11 | 0,247 |
| 64 pontos, `eps` 0,3 % | ~39 | 0,634 |

Ou seja: os 64 pontos são bem **mais** fiéis, e continuam custando ~1 KB.

O que estava errado era o **corte** quando o contorno não cabia em 64
pontos: o código descartava vértices uniformemente (`linspace`), o que joga
fora justamente os vértices que o `approxPolyDP` (Douglas-Peucker) escolheu
por sustentarem a forma, e mantém outros arbitrários — cantos cortados e até
auto-interseção. Agora, se não couber, o `eps` **aumenta** e simplifica de
novo: o polígono continua sendo uma simplificação coerente. Em contornos que
disparam esse caminho a fidelidade sobe ~20 %; nos que não disparam (a
maioria) o resultado é idêntico ao anterior.

#### Quem desenha a máscara — e o que isso custa no servidor

São três atores diferentes, e **o servidor não é um deles**:

| Onde aparece | Quem desenha | Como |
|---|---|---|
| **Stream** (telinha ao vivo) | o **Raspberry** | `detector.desenhar()` grava a máscara de pixels inteira no frame **antes** de codificar o JPEG (`STREAM_ANOTADO`) — sem simplificação |
| **“Ver máscara”** (alerta aberto) | o **navegador** | um `<canvas>` por cima do `<img>`, traçando o polígono `poly` que já veio junto com a detecção (`desenharMascaraCanvas`, `server/static/dashboard.html`) |
| — | o **servidor** | **nada**: ele só guarda os bytes que chegam e devolve o JSON e o arquivo |

**O servidor não roda inferência nem processamento de imagem — nunca.** Ele
não importa OpenCV, PIL, ONNX, Torch ou `hailo_platform`; a única biblioteca
numérica lá é o `numpy`, e é para **geometria** (distâncias e o raycasting
do cone contra a malha), não para pixels. Quando a foto sob demanda chega do
Pi, o caminho inteiro é `base64.b64decode(...)` → `f.write(...)`: os bytes
vão para o disco exatamente como saíram da câmera, sem decodificar, sem
redesenhar e sem recomprimir.

Ou seja: o custo de "Ver máscara" no servidor é **zero de CPU de visão** —
serve um JPEG estático e alguns números. Toda a inferência acontece no
Raspberry, na NPU Hailo (§1). O único trabalho pesado do servidor continua
sendo o raycasting do cone, que é geometria pura e roda em *threadpool*
(§8).

Foi essa separação que permitiu a detecção parar de enviar imagem: o
contorno são ~1 KB de números que servem para deduplicar **e** para
desenhar, enquanto a foto só sobe se alguém pedir.

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

Obrigatórias: `CAMERA_IP`, `ONVIF_USER`, `ONVIF_PASSWORD`, `RTSP_URL`,
**`DEVICE_TOKEN`** (gerado ao cadastrar o dispositivo em `/dispositivos`,
§9-bis -- sem ele o agente nem sobe, `_req()` falha rápido).

| Variável | Padrão | Para quê |
|---|---|---|
| `SERVER_URL` | `http://127.0.0.1:8001` | servidor do dashboard, no modo HTTP |
| `DEVICE_TOKEN` | **obrigatória** | token do dispositivo (Bearer, em toda chamada HTTP a `/api/edge/*`) |
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
| `PTZ_ZERO_AO_INICIAR` | false | mover a câmera para o ponto zero ao subir o agente (ver §9-ter: desligado para não roubar a câmera de outro sistema) |
| `API_PORT` | 8090 | porta da API local do agente |
| `OPENCV_FFMPEG_CAPTURE_OPTIONS` | — | força TCP no RTSP (ver §4.3) |

### `server/.env` — servidor

| Variável | Padrão | Para quê |
|---|---|---|
| `DATABASE_URL` | ver `.env.example` | conexão PostgreSQL (usuários/sessões, §5.1a) |
| `SESSION_COOKIE_SECURE` | `false` | `true` só atrás de HTTPS de verdade (reverse proxy) |
| `SESSAO_DURACAO_H` | 168 (7 dias) | validade do cookie de sessão |
| `CONTROLLER_URL` | `http://127.0.0.1:8090` | *fallback* servidor → Pi, só para dispositivos sem `controller_url` próprio cadastrado |
| `CONTROLLER_PUBLIC_URL` | = acima | *fallback* navegador → Pi (PTZ direto), idem acima |
| `STREAM_JANELA_S` | 60 | duração do pedido de vídeo |
| `STREAM_FPS` / `STREAM_LARGURA` / `STREAM_QUALIDADE` | 4 / 640 / 60 | parâmetros pedidos ao Pi |
| `PAN_SIGN` / `TILT_SIGN` | -1 / 1 | sentido de rotação (global -- é fiação de câmera, não propriedade da localidade) |
| `CONE_HALF_ANGLE_WIDE` / `_TELE` | 18 / 2 | abertura do cone em graus |
| `CONE_RING_RAYS` | 24 | raios do anel (16 é mais leve) |
| `DEDUP_RAIO_M` | 1.5 | raio 3D para considerar a mesma fissura (por dispositivo -- nunca compara entre dispositivos diferentes) |
| `DEDUP_ANG_DEG` | 1.5 | tolerância angular sem ponto 3D nos dois lados |
| `REARME_SEGUNDOS` | 600 | antes de um ponto tratado reabrir alerta |
| `MQTT_BRIDGE` / `MQTT_HOST` / `MQTT_PORT` | false / 127.0.0.1 / 1883 | ponte MQTT de teste (§9) -- só o dispositivo único legado, sem token |
| `DEVICE_ID` | `oiticica-cam-01` | identificador do dispositivo **só na ponte MQTT de teste** acima; no HTTP (produção) a identidade vem do `DEVICE_TOKEN` cadastrado, não desta variável |

> `CAMERA_ABS_ALT` existia antes da etapa multi-dispositivo (elevação
> absoluta da lente, sobrescrevendo a estimativa de terreno) e **não existe
> mais**: hoje isso é `alt_acima_solo`, cadastrado por dispositivo em
> `/dispositivos` (relativo ao terreno, não absoluto).

### `controller/.env` — ambiente de teste sem Pi

Mesmas credenciais de câmera, mais `SERVER_URL`, `YOLO_CONF_THRESHOLD` (0.558),
`DETECTION_COOLDOWN_SECONDS` (5) e **`DEVICE_TOKEN`** (obrigatória, mesmo
esquema do `edge/.env` -- `/api/telemetry`/`/api/detection` também exigem
Bearer agora).

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

**A câmera e as detecções caem em pontos diferentes do modelo** — quase
sempre é offset UTM trocado: o `.glb` foi substituído por outro recorte da
mesma parede e o georreferenciamento continuou o do recorte antigo. Ver
“O offset UTM pertence ao MODELO” (§5.1a-bis) e corrija em **Editar** na
localidade.

**Detecções “fantasma”, presas em “Solicitando imagem completa”** — a
tradução entre o `det_id` do Raspberry e o id do histórico do servidor era
feita só por um mapa **em memória** (`device.mapa_det`). Esse mapa se perde
a cada reinício do servidor, a cada edição do dispositivo e ao recadastrá-lo
— e aí “Abrir” pedia uma foto que ninguém mais sabia associar, sem nunca
falhar de forma visível. O id da borda já era gravado no histórico
(`borda_det_id`); agora é ele a fonte de verdade nos dois sentidos (pedido e
recebimento), com o mapa em memória só como atalho. O modal também desiste
depois de 15 s e explica, em vez de girar para sempre.

**O vídeo some e o PTZ “trava” por alguns segundos, depois volta** —
excesso de requisições simultâneas, não a rede. A renovação da janela de
stream era disparada a **cada tique de PTZ** (300 ms enquanto o botão está
pressionado): eram ~3 POSTs por segundo, cada um incrementando a versão do
estado desejado e disparando um push para o Pi, somados a 4 quadros/s e ao
`/api/borda`. Isso estoura o limite de conexões simultâneas do navegador e
os comandos de PTZ ficam na fila. Agora a renovação é limitada a uma a cada
10 s (a janela dura 60 s) e um quadro novo é **descartado** se o anterior
ainda estiver baixando — vídeo ao vivo quer o quadro mais recente, não uma
fila deles.

**`server/history/` e `edge/evidencias/` têm contagens diferentes** — isso é
esperado, não é bug. São coisas distintas:

| Pasta | O que guarda | Quando cresce |
|---|---|---|
| `edge/evidencias/` (Pi) | o frame cru de **todo alerta novo** | a cada detecção que o servidor confirma como alerta novo |
| `server/history/` | só as fotos que **alguém pediu** (“Abrir”) | quando o operador abre um alerta e a foto sobe |

O `index.json` do servidor tem uma entrada por alerta; os arquivos `.jpg` em
`history/` são um subconjunto — só os que foram solicitados. 8 evidências no
Pi para 2 imagens no servidor significa que 8 alertas foram registrados e 2
tiveram a foto pedida. É exatamente o “evidência sob demanda” do §8. Se o
número de **alertas** no dashboard for menor que o de evidências no Pi, aí
sim há algo a investigar (ver a entrada sobre evidência acima).

**O modelo 3D aparece “esvaecido” e não gira com o mouse** — não era o
modelo nem a iluminação: o aviso “sem modelo 3D pronto” (`#viewport-sem-3d`)
estava **sempre** desenhado por cima dele. A causa é uma pegadinha de CSS:
o aviso tem `display:flex` num seletor de **id**, e isso vence a regra
`[hidden] { display: none }` do navegador — ou seja, o atributo `hidden`
deixa de esconder. O resultado era um véu escuro de 55 % (o “esvaecido”) que
ainda por cima cobria `inset:0` e engolia os cliques antes de eles chegarem
ao canvas, deixando o `OrbitControls` inerte. Corrigido com a guarda
explícita `#viewport-sem-3d[hidden] { display: none; }`.

> Vale para qualquer elemento novo: **se você definir `display` num seletor
> de id ou classe, acrescente a guarda `[hidden]`**, senão o atributo
> `hidden` vira decoração. O `#modal-status` do mesmo arquivo já fazia isso.

**A máscara em “Ver máscara” às vezes sai deslocada ou deformada** — o
contorno vem em coordenadas 0..1 **do frame**, então só cai no lugar se a
caixa do `<img>` já for a da foto. O desenho era feito logo depois de
atribuir o `src`, sem esperar a imagem carregar: enquanto ela não chegava, o
`<img>` tinha o tamanho mínimo do CSS (200×120) — outra proporção — e a
máscara saía esticada. Como dependia de a foto estar em cache ou não, o
defeito era intermitente. Agora o desenho espera o `load` da imagem.

**“Este dispositivo ainda não tem um modelo 3D pronto”, mesmo com o `.glb`
enviado e o dispositivo cadastrado** — havia duas causas distintas, e o
painel antigo não separava uma da outra:

1. **O dispositivo não tinha localidade associada.** Criar o dispositivo
   antes da localidade existir (ou deixando “(nenhuma)”) era comum, e não
   havia como corrigir depois: o cadastro só tinha criar e excluir. Agora a
   listagem em `/dispositivos` diz exatamente o que falta em cada
   dispositivo e o botão **Editar** associa a localidade sem trocar o token
   (§5.1a-bis).
2. **O servidor mantinha um `DispositivoRuntime` obsoleto em memória.** O
   registro consulta o banco só na primeira vez que resolve cada
   dispositivo (`server/registro_dispositivos.py`). Se o painel tocasse no
   dispositivo enquanto o `.glb` ainda estava `processando`, o runtime
   nascia com `geo=None` e **continuava assim mesmo depois de o banco virar
   `pronto`** — até reiniciar o servidor. As funções de invalidação
   existiam, mas nada as chamava. Agora `server/dispositivos.py` invalida ao
   terminar a descompressão Draco, ao enviar um `.glb` novo, ao excluir a
   localidade e ao editar o dispositivo; e `/api/camera_info` remonta o
   runtime uma vez, por segurança, quando o banco diz que está tudo pronto e
   o runtime discorda.

**A página `/dispositivos` abre em branco (sem listas, sem formulário)** — o
Leaflet vem de CDN (`unpkg.com`); num servidor sem saída para a internet, o
`L` não existe e o erro derrubava o script inteiro, justamente a tela que
conserta um dispositivo mal configurado. Hoje o mapa é opcional: falhando,
aparece um aviso no lugar dele e os campos de latitude/longitude continuam
funcionando normalmente.

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
  agente parado). ✅ **Também dá para resolver pelo dashboard**: a calibração
  do gêmeo digital (§9-ter) estima as escalas de pan/tilt junto com a pose,
  a partir de 8 pontos ou mais — e só as aplica quando os dados mostram que
  o curso está mesmo errado.
- **Altura da câmera estimada.** O server cai na estimativa por percentil dos
  vértices vizinhos, porque a câmera fica ~36 m além da borda norte do modelo. A
  estimativa (87,07) praticamente coincide com o ponto de malha mais próximo
  (86,63), o que pode significar que ambos estão ancorados no topo da estrutura
  em vez do chão. Erro aqui vira erro sistemático de tilt: ao cadastrar o
  dispositivo em `/dispositivos`, informe a altura real da lente acima do
  solo (`alt_acima_solo`) em vez de confiar no padrão de 7 m. (A variável
  `CAMERA_ABS_ALT`, que existia antes da etapa multi-dispositivo para
  sobrescrever com uma elevação absoluta, não existe mais -- cada
  dispositivo cadastrado só tem `alt_acima_solo`, relativo ao terreno.)
  ✅ **Contornável pela calibração** (§9-ter): com 4 pontos ou mais, a
  posição da câmera passa a ser medida, e a altura estimada deixa de
  importar.
- **Sentido do pan** corrigido com `PAN_SIGN=-1`, ainda por confirmar em campo.
  A calibração absorve um erro de *orientação*, mas não um sinal trocado:
  `PAN_SIGN`/`TILT_SIGN` continuam globais (é convenção de fiação da câmera,
  não propriedade do local). Se o sinal estiver errado, a calibração não
  converge para um RMS baixo — o que, na prática, serve de teste.
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
- **O painel depende de CDN para o Three.js.** Se `cdnjs`/`jsdelivr` não
  estiverem acessíveis, o módulo 3D não carrega e o painel inteiro fica sem
  seletor de dispositivo, vídeo e histórico — tudo vive nesse mesmo módulo.
  A tela `/dispositivos` já foi tornada resistente ao caso equivalente
  (Leaflet, ver §13); o painel ainda não. Servir as bibliotecas do próprio
  servidor resolveria os dois de vez.
- **A qualidade da máscara foi medida só em imagens curadas de fissura.** O
  comportamento em cena ampla, com sombra e textura, ainda não foi quantificado.
- **Painel de administração — construção por etapas (3 abas: Dashboard,
  Dispositivos, Configuração).** Estado atual:
  - ✅ **Configuração**: login por sessão (PostgreSQL, `server/db.py` +
    `server/auth.py`), admin padrão `admin`/`hydroconecta` com troca de
    senha obrigatória, papéis admin/usuário, tema movido para cá
    (`server/static/config.html`, `server/static/login.html`).
  - ✅ **Dispositivos** (cadastro e edição, §5.1a-bis): localidades com
    modelo 3D (upload + descompressão Draco em segundo plano) e
    georreferenciamento (N por sistema), dispositivos CV-SHM com localização
    no mapa (Leaflet/OpenStreetMap) **ou por coordenadas digitadas**,
    transporte HTTP/MQTT e geração de token/tópicos
    (`server/dispositivos.py`, `server/static/dispositivos.html`). Editar um
    dispositivo já cadastrado (`PATCH`) não troca o token, então dá para
    corrigir localidade/posição sem derrubar o Raspberry em campo. Cada
    usuário só vê/edita/exclui os próprios dispositivos; admin vê todos.
  - ✅ **Pipeline ao vivo multi-dispositivo (HTTP)**: `server/borda.py` e
    `server/server.py` agora resolvem o dispositivo pelo token Bearer em
    TODA chamada HTTP (`/api/edge/*`, `/api/telemetry`, `/api/detection`) e
    mantêm um registro em memória por dispositivo
    (`server/registro_dispositivos.py`) -- cada um com seu próprio
    `GeoModel`/pose de câmera (via a localidade cadastrada), sua própria
    janela de stream e seu próprio estado desejado. Sem token válido, a
    chamada recebe `401`; sem localidade pronta cadastrada, o dispositivo
    fica **sem posição 3D** (PTZ e vídeo continuam funcionando, só a visão 3D
    fica indisponível até o cadastro ficar completo) -- decisão explícita,
    sem *fallback* silencioso para a geometria de outro dispositivo. Ver
    §9-bis para a migração obrigatória do Raspberry já em produção.
    **Ainda não faz**: a ponte MQTT (`MQTT_BRIDGE=true`, ver §9) continua
    servindo só o dispositivo único legado via `DEVICE_ID`, sem token e sem
    passar pelo registro -- ela existe apenas para testar o modo MQTT
    localmente com um `mosquitto`, e essa multiplexação por dispositivo fica
    para quando o ThingsBoard entrar de verdade.
  - ⏳ **Dashboard**: grid de widgets redimensionável, N dashboards por
    usuário, filtro por localidade — ainda não construído (tabela
    `dashboards` já criada, com `layout JSONB`). O
    `server/static/dashboard.html` atual continua sendo a única tela de
    operação; vai virar o primeiro widget (tipo `CV-SHM`) dentro desse
    grid.

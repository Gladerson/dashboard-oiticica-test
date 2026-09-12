# Firmware dos ESP32

Dois equipamentos, dois papéis:

| Pasta | Papel |
|---|---|
| `sensor_nivel/` | **Controlador**: lê o radar por Modbus RTU e manda a medida bruta ao gateway por LoRa, com ACK e retentativa. |
| `gateway_lora/` | **Gateway**: recebe de N controladores, aplica as regras de cada reservatório e publica tudo num POST **HTTPS** para o servidor. |

`libraries/HydroConecta/` é código compartilhado pelos dois — e é **livre de
Arduino de propósito**, para que a suíte em `testes/` possa exercitá-lo num PC
com `g++`, sem hardware.

## Instalar a biblioteca compartilhada (faça isto ANTES de compilar)

Abrir o `.ino` direto da pasta descompactada **não funciona**. A Arduino IDE
não procura cabeçalhos ao lado do sketch: ela procura na pasta `libraries` do
*sketchbook*. Sem esse passo o erro é sempre este:

```text
fatal error: ProtocoloLoRa.h: No such file or directory
```

Não é erro de código — é a biblioteca que ainda não foi instalada. Escolha
**um** dos dois caminhos.

### Caminho A — copiar a pasta (recomendado, não mexe em nada)

Copie a pasta **`firmware/libraries/HydroConecta`** inteira para dentro da
pasta `libraries` do seu sketchbook:

| Sistema | Destino |
|---|---|
| Windows | `C:\Users\<seu-usuario>\Documents\Arduino\libraries\HydroConecta` |
| macOS | `~/Documents/Arduino/libraries/HydroConecta` |
| Linux | `~/Arduino/libraries/HydroConecta` |

No fim tem de existir o arquivo
`...\Arduino\libraries\HydroConecta\ProtocoloLoRa.h` — se o caminho ficou
`...\libraries\HydroConecta\HydroConecta\ProtocoloLoRa.h`, você copiou um
nível a mais. **Feche e reabra a IDE**; ela só varre `libraries` na abertura.

Se você atualizar o repositório depois, copie de novo — essa cópia não se
atualiza sozinha.

### Caminho B — apontar o sketchbook para `firmware/`

**Arquivo → Preferências → Local do sketchbook** → a pasta `firmware` deste
repositório. A IDE passa a achar `libraries/HydroConecta` sozinha, e atualizar
o repositório atualiza a biblioteca junto.

O preço: o sketchbook é **também** onde a IDE guarda as bibliotecas que você
instalou pelo Gerenciador. Trocando de sketchbook, elas somem da lista (não
são apagadas — voltam quando você voltar o caminho antigo). Se você usa o modo
4G, a TinyGSM é uma delas: instale-a de novo com o sketchbook já apontado
para cá.

Depois: **Arquivo → Sketchbook → gateway_lora** (ou `sensor_nivel`).

## Compilar

Placa: **ESP32 Dev Module**. Core: **ESP32 Arduino 3.x**
(*Ferramentas → Placa → Gerenciador de placas* → `esp32` da Espressif).

Biblioteca externa: **TinyGSM** — só o gateway, e agora **sempre**: os dois
enlaces são compilados juntos. Só desligando `TEM_4G` ela deixa de ser
necessária.

## Rodar os testes

```bash
g++ -std=c++17 -Wall -Wextra -I firmware/libraries/HydroConecta \
    firmware/testes/teste_firmware.cpp -o /tmp/teste && /tmp/teste
```

Cobre o formato do pacote LoRa (inclusive corrompido e truncado), o montador
de JSON (inclusive estouro de buffer e NaN) e as regras de nível (inclusive
calibração incoerente). Se mexer nos cabeçalhos, rode isto antes de gravar
qualquer coisa em campo.

## Wi-Fi e 4G no gateway: os dois, com troca automática

Não há mais escolha de compilação. O gateway usa **Wi-Fi por prioridade**,
cai para o **4G** sozinho quando o Wi-Fi some, e volta assim que ele se firma.
Preencha os quatro campos:

```cpp
static const char* WIFI_SSID = "...";
static const char* WIFI_PASS = "...";
static const char  APN[]     = "java.claro.com.br";   // e APN_USER / APN_PASS
```

`TEM_WIFI` e `TEM_4G` (no topo do sketch) só existem para o caso de a placa
realmente não ter aquele hardware — deixe os dois em 1 no caso normal. Um
gateway sem modem instalado funciona com os dois ligados: depois de três
tentativas sem resposta o firmware conclui que não há chip e segue só com
Wi-Fi.

Detalhes da histerese, do estado do modem e das telemetrias de enlace: README
principal, seção **9-sexies**.

## Rodar a suíte do gateway

Além dos cabeçalhos compartilhados, o **gateway inteiro** roda no PC:

```bash
g++ -std=c++17 -Wall -Wextra -I firmware/libraries/HydroConecta \
    -I firmware/testes -I firmware/testes/stubs \
    firmware/testes/teste_gateway.cpp -o /tmp/tg && /tmp/tg
```

`testes/ambiente_arduino.h` faz o papel do Arduino, do ESP32 e da TinyGSM: o
tempo é controlado, o Wi-Fi cai quando o teste manda e o modem responde ou
fica mudo por decisão do teste. É assim que a troca de enlace é exercitada
sem subir numa torre. Se mexer na rede do gateway, **rode isto antes de
gravar**.

## Antes de gravar

1. **`DEVICE_TOKEN`** no gateway: crie o dispositivo no painel
   (**Dispositivos → Novo**, tipo `gateway`) e cole o token gerado.
2. **`DEVICE_ID`** no controlador, a tabela `EQUIPAMENTOS` no gateway e o
   campo *Identificador no gateway* do cadastro do sensor têm de ser o
   **mesmo texto**. Se os três não baterem, a medida chega ao servidor e fica
   sem dono — sem erro nenhum, só sem aparecer.
3. **A calibração do reservatório NÃO está mais no firmware.** Cota, volume e
   percentual são calculados pelo servidor, a partir do que estiver em
   **Dispositivos → Sensor → Radar de nível**. Não há o que ajustar aqui.

O passo a passo completo, com a ordem de atualização em campo, está no README
principal, seção **9-sexies**.

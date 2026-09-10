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

Biblioteca externa: **TinyGSM** — só o gateway, e só se `TIPO_CONEXAO` for
`TIPO_CONEXAO_4G`. No modo Wi-Fi ela não é necessária, porque o `#include`
dela está dentro do `#if` do 4G.

## Rodar os testes

```bash
g++ -std=c++17 -Wall -Wextra -I firmware/libraries/HydroConecta \
    firmware/testes/teste_firmware.cpp -o /tmp/teste && /tmp/teste
```

Cobre o formato do pacote LoRa (inclusive corrompido e truncado), o montador
de JSON (inclusive estouro de buffer e NaN) e as regras de nível (inclusive
calibração incoerente). Se mexer nos cabeçalhos, rode isto antes de gravar
qualquer coisa em campo.

## Wi-Fi ou 4G no gateway

Só a terceira linha muda; as duas de cima são rótulos:

```cpp
#define TIPO_CONEXAO        TIPO_CONEXAO_WIFI   // ou TIPO_CONEXAO_4G
```

No Wi-Fi, preencha `WIFI_SSID`/`WIFI_PASS`, a TinyGSM deixa de ser necessária
e o TLS passa a ser feito pelo ESP32 — que **confere** a cadeia (o modem do
caminho 4G não confere). Por isso o Wi-Fi depende do relógio: o sketch acerta
a hora por NTP antes do primeiro envio e a cada envio se ela ainda não estiver
acertada. Detalhes no README principal, seção **9-sexies**.

## Antes de gravar

1. **`DEVICE_TOKEN`** no gateway: crie o dispositivo no painel
   (**Dispositivos → Novo**, tipo `gateway`) e cole o token gerado.
2. **`DEVICE_ID`** no controlador tem de ser **idêntico** ao `id` da tabela
   `EQUIPAMENTOS` no gateway — é ele que vira o `sub_id` no servidor.
3. **`cota_fundo`** de cada reservatório: confira no projeto da barragem.
   Ver a explicação em `libraries/HydroConecta/RegrasNivel.h`.

O passo a passo completo, com a ordem de atualização em campo, está no README
principal, seção **9-sexies**.

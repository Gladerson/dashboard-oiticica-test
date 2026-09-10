# Firmware dos ESP32

Dois equipamentos, dois papéis:

| Pasta | Papel |
|---|---|
| `sensor_nivel/` | **Controlador**: lê o radar por Modbus RTU e manda a medida bruta ao gateway por LoRa, com ACK e retentativa. |
| `gateway_lora/` | **Gateway**: recebe de N controladores, aplica as regras de cada reservatório e publica tudo num POST **HTTPS** para o servidor. |

`libraries/HydroConecta/` é código compartilhado pelos dois — e é **livre de
Arduino de propósito**, para que a suíte em `testes/` possa exercitá-lo num PC
com `g++`, sem hardware.

## Abrir na Arduino IDE

O jeito mais simples é apontar o *sketchbook* para esta pasta — assim a IDE
acha `libraries/HydroConecta` sozinha:

**Arquivo → Preferências → Local do sketchbook** → `.../dashboard_oiticica_test/firmware`

Depois **Arquivo → Sketchbook → gateway_lora** (ou `sensor_nivel`).

Placa: **ESP32 Dev Module**. Core: **ESP32 Arduino 3.x**.
Biblioteca externa (só o gateway, e só no modo 4G): **TinyGSM**.

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

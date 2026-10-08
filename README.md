# Laboratório Zabbix 8.0 + OpenTelemetry

Ambiente em Docker Compose para estudar a telemetria nativa do Zabbix 8.0:
uma aplicação instrumentada com OpenTelemetry envia traces, métricas e logs
direto para o proxy do Zabbix, que grava tudo no ClickHouse. O frontend mostra
os dados no menu **APM** e o server consulta as mesmas tabelas com itens do tipo
*Telemetry query*.

```
 aplicação (gerador)                zabbix-proxy                 ClickHouse
 SDK OpenTelemetry  --OTLP/gRPC-->  coletor APM (4317)  ------>  tabelas otel_*
                                         |                          ^     ^
                                         | proxy ativo              |     |
                                         v                          |     |
                                    zabbix-server  --Telemetry query'     |
                                         ^                                |
                                    PostgreSQL <--- zabbix-web (APM) -----'
```

## O que sobe

| Serviço | Imagem | Para quê |
|---|---|---|
| `postgres` | `postgres:18-alpine` | Configuração e histórico do Zabbix |
| `clickhouse` | `clickhouse/clickhouse-server:26.8-alpine` | Traces, métricas e logs |
| `zabbix-server` | `zabbix/zabbix-server-pgsql:alpine-trunk` | Server |
| `zabbix-web` | `zabbix/zabbix-web-nginx-pgsql:alpine-trunk` | Frontend e API |
| `zabbix-proxy` | `zabbix/zabbix-proxy-sqlite3:alpine-trunk` | Coletor OpenTelemetry |
| `gerador` | construída aqui (`ferramentas/`) | Telemetria de teste |
| `zabbix-mcp-server` | `zabbix/zabbix-mcp-server:alpine-trunk` | Opcional, desligado |

## Requisitos

- Linux amd64 ou arm64 com **4 GB de RAM** (recomendado). Com 2 GB sobe no
  limite e precisa de swap; com 1 GB não cabe.
- Docker com o plugin Compose 2.20 ou mais novo.
- `make` (`sudo apt install -y make`). Sem ele, use os comandos do `Makefile`.

Sem swap e com pouca memória, crie 2 GB antes de subir:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## Subir na VM da AWS com um comando

A partir do seu computador (WSL), dentro desta pasta:

```bash
make deploy
```

O `scripts/deploy-aws.sh` faz o que o `make bootstrap` do projeto
agentless-fleet-audit fazia, sem depender do estado do Terraform:

1. acha a VM na AWS pela tag `Name=fleet-audit-lab` e pega o IP público atual
   (ele muda a cada vez que a VM liga);
2. testa as chaves SSH conhecidas (`~/.ssh/oci_lab` primeiro) e usa a que entra;
3. copia esta pasta para `~/zabbix8-telemetria-lab` na VM, sem o `.env`;
4. roda lá dentro `env`, `preflight`, `up`, `bootstrap` e `verificar`.

Precisa do AWS CLI com o perfil `fleet-audit` (o mesmo do outro projeto) e da
chave privada da VM. Outros comandos:

```bash
make vm-status    # VM, IP atual, regra de SSH e teste de acesso
make vm-tunel     # abre http://localhost:8080 (deixe rodando em outro terminal)
make vm-senha     # senha do Admin do Zabbix
make vm-parar     # para a VM quando não estiver usando
make vm-ligar     # liga de novo
./scripts/deploy-aws.sh liberar-ip    # se o seu IP mudou e o SSH parou de conectar
./scripts/deploy-aws.sh diagnostico   # junta resultado e logs num arquivo local
```

Se a VM tiver outro nome, região, usuário ou chave, crie um `deploy.conf`
nesta pasta (as chaves estão no começo do `scripts/deploy-aws.sh`).

## Subir direto na máquina (passo a passo)

```bash
make env          # cria o .env com três senhas aleatórias
make preflight    # confere memória, disco, Docker e portas
make up           # baixa as imagens e sobe tudo (alguns minutos na 1ª vez)
make bootstrap    # troca a senha do Admin e cadastra o proxy com a coleta ligada
make verificar    # confere o caminho inteiro, etapa por etapa
```

`make tudo` roda os cinco em sequência.

Espere 1 a 2 minutos entre o `bootstrap` e o `verificar`: o proxy busca a
configuração no server a cada 10 segundos, mas o primeiro ciclo demora mais.

Se o `verificar` apontar falha, rode `make diagnostico`. Ele grava um arquivo
`diagnostico-*.txt` com o resultado e os logs, sem o conteúdo do `.env`.

## Acesso ao frontend

As portas ficam presas em `127.0.0.1` da VM. Abra um túnel SSH a partir do seu
computador com `make vm-tunel`, ou à mão:

```bash
ssh -L 8080:127.0.0.1:8080 usuario@IP_DA_VM
```

Depois acesse <http://localhost:8080>. Usuário `Admin`, senha em
`make vm-senha` (na VM: `grep ZBX_ADMIN_PASSWORD .env`).

O que olhar:

- **APM > Traces**: os ciclos do serviço `gerador-demo`, com spans filhos por UF
  e alguns com erro.
- **APM > Metrics** e **APM > Logs**: os outros dois sinais.
- **Administration > Proxies**: o proxy `zabbix-proxy-otel` online, com a coleta
  OpenTelemetry habilitada.

Para publicar o frontend direto pelo IP da VM, troque `BIND_ADDR=0.0.0.0` no
`.env` **depois** do `make bootstrap` e libere a porta no security group só
para o seu IP. A exposição para recrutadores (HTTPS e usuário somente leitura)
é uma etapa posterior.

## Decisões que fogem do padrão, e por quê

1. **ClickHouse 26.8 ou mais novo.** O Compose oficial do Zabbix fixa o 26.4,
   que serve para histórico, mas não para telemetria. O coletor do proxy grava
   as datas como número decimal (`segundos.nanos`, em
   `src/zabbix_proxy/apm/apm_clickhouse.c`), e até o 26.7 o ClickHouse lê número
   sem aspas em coluna `DateTime64` como valor bruto e rejeita o ponto. Testado
   aqui com o mesmo formato de inserção: 26.4.5 recusa, 26.8.21 grava. Como a
   inserção é assíncrona e o proxy não espera a resposta, no 26.4 a telemetria
   some sem erro no log do Zabbix.
2. **Tabelas `otel_*` criadas à mão** (`config/clickhouse/init/`). O 8.0.0rc1
   traz scripts só para as tabelas de histórico. As colunas seguem a ordem que
   o proxy grava (`apm_dataset.c`) e os nomes e tipos que o frontend espera
   (`CApmData.php`). A inserção é por posição, então a ordem não pode mudar.
3. **Proxy com arquivo de configuração próprio** (`config/proxy/`). A imagem
   ainda não tem variável de ambiente para `StartAPMCollectors`. O arquivo
   inclui a configuração padrão da imagem e acrescenta só esses parâmetros.
4. **Imagens `trunk`.** Em 08/10/2026 o Docker Hub só tem imagens do 8.0 com
   essa tag, reconstruída todo dia a partir do código em desenvolvimento, ou
   seja, mais nova que o 8.0.0rc1. Por isso o `compose.yaml` fixa cada imagem
   por digest, o da primeira subida que funcionou (08/10/2026, 8.0.0rc2).
   Para trocar, defina `ZBX_SERVER_IMAGE` e as outras `*_IMAGE` no `.env`;
   `make vm-digests` mostra os digests em uso na VM.
5. **Histórico do Zabbix no PostgreSQL**, não no ClickHouse. Menos peças para
   depurar agora; o ClickHouse fica só com a telemetria.

## Estado da validação

Conferido antes da primeira subida:

- Esquema das tabelas e formato de inserção do proxy, em ClickHouse 26.4 e 26.8
  reais, incluindo o arquivo de baixo consumo.
- Gerador enviando os três sinais para um receptor OTLP local.
- Script de bootstrap contra uma API simulada (primeira execução, repetição,
  senha errada, API fora do ar).
- Script de deploy contra comandos `aws` e `ssh` simulados (VM ligada, parada,
  inexistente, IP alterado, chave recusada). Não foi rodado contra a AWS real.
- Sintaxe do Compose (`docker compose config`) e dos scripts (`shellcheck`).

**Ainda não rodou de ponta a ponta com as imagens do Zabbix.** O ambiente onde
foi escrito não tinha acesso ao Docker Hub. Os pontos que só a primeira subida
confirma: o proxy aceitar o arquivo de configuração próprio, o caminho
proxy → ClickHouse com dados reais e as telas de APM lendo as tabelas.

## Problemas comuns

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| `verificar` sem linhas nas tabelas | Proxy ainda sem configuração | Esperar 2 minutos; conferir `make logs S=zabbix-proxy` |
| Proxy reinicia com `unknown parameter` | A imagem mudou o nome de um parâmetro | Ajustar `config/proxy/zabbix_proxy_lab.conf` |
| `inserções recusadas` no `verificar` | Esquema diferente do que o proxy grava | A imagem `trunk` mudou colunas; comparar com `apm_dataset.c` |
| Container morto com código 137 | Faltou memória | Criar swap ou subir o tamanho da VM |
| zabbix-server reiniciando com `execute SQL script create.sql.gz: conn closed` | PostgreSQL morto por OOM ao criar o banco | `POSTGRES_MEM` de 1536m ou mais no `.env` e `docker compose up -d` |
| Senha do banco não confere após mexer no `.env` | Senha só vale na criação do volume | `make limpar` e subir de novo |
| `bootstrap` não entra como Admin | Senha trocada pela interface | Ajustar `ZBX_ADMIN_PASSWORD` no `.env` |

## Servidor MCP (opcional)

`make mcp-up` sobe o servidor MCP do Zabbix em `127.0.0.1:8443`. Ele repassa o
token da API a cada requisição, então **não publique essa porta na internet**;
use túnel SSH (`-L 8443:127.0.0.1:8443`). Crie um usuário próprio, com o mínimo
de permissão e token com validade, e libere a API no perfil desse usuário (no
8.0 ela vem desligada para todos os perfis, menos o de super administrador).

## Próximas etapas

1. Trocar o gerador pelo coletor da apuração do TSE, com modo replay.
2. Templates com itens *Telemetry query*, triggers e dashboard.
3. Infraestrutura como código (Terraform e Ansible) e exposição com HTTPS.
4. Página de vitrine no GitHub Pages.

## Estrutura

```
compose.yaml                      serviços do laboratório
.env.example                      modelo de configuração (o .env real fica fora do Git)
Makefile                          atalhos (make ajuda)
config/clickhouse/init/           criação das tabelas otel_*
config/clickhouse/config.d/       ajustes de memória do ClickHouse
config/proxy/                     configuração do proxy com o coletor OpenTelemetry
ferramentas/                      gerador de telemetria e bootstrap pela API
scripts/                          gerar-env, preflight, verificar, subir-tudo e deploy-aws
```

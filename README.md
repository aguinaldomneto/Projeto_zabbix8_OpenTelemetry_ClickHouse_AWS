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
| `host-aws-01`...`05` | `zabbix/zabbix-agent2:alpine-trunk` | Opcional (`make hosts-up`): 5 "hosts simulados" nesta VM |
| `ia-assistente` | construída aqui (`ferramentas/`) | Opcional (`make ia-up`): ponte Zabbix → Gemini |

As APIs fictícias, o Redpanda, o gerador de tráfego e os outros 10 hosts
simulados **não** entram aqui: rodam no seu WSL local
(`compose.local.yaml`, `make local-up`) para não pesar a VM de 4GB. Veja a
seção "APIs fictícias, Kafka e hosts simulados" abaixo.

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
6. **Latência dos hosts simulados via `UserParameter` do agente, não
   `icmppingsec` nativo.** `icmppingsec` é executado pelo *proxy*, que só
   alcança os 5 hosts simulados da própria VM — os 10 do WSL local ficam
   atrás de NAT e o proxy não tem como pingá-los de volta. Um item ativo
   (o agente faz o ping e manda o resultado) funciona dos dois lados.
7. **Módulo do widget em `/usr/share/zabbix/modules/`, não `/widgets/`.**
   Essa segunda pasta é só para os módulos que já vêm com a imagem; a
   primeira é a pasta vazia (com `.gitignore`) que a própria imagem deixa
   para módulos do operador, e é nela que o `CModuleManager` deriva o
   namespace PHP esperado (`Modules\<Nome>`, não `Widgets\<Nome>` — conferido
   lendo o código-fonte da imagem, já que isso não está documentado).
8. **Item "Telemetry query" montado via API, não escrito à mão.** O campo
   `query` não é SQL puro: é um objeto estruturado (`signal_type`,
   `columns`, `aggregated_columns`, `filter`) específico do item type 24,
   também sem documentação pública ainda nesta fase do 8.0. Os templates em
   `config/zabbix-templates/` foram gerados criando os itens pela API de
   verdade e exportando com `configuration.export` — não eram pra ser
   editados à mão sem recriar esse caminho.

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

**APIs fictícias, Kafka, widget e templates**, ao contrário, foram
conferidos contra uma subida real do laboratório (`make up && make
bootstrap`) no ambiente onde foram escritos, desta vez com acesso ao Docker
Hub:

- Fluxo `pedidos` → Redpanda → `pagamentos` → webhook de volta em `pedidos`,
  rodando de verdade (`compose.local.yaml`) e confirmado nos logs.
- Widget "Insights": módulo carregado pelo Zabbix de verdade (namespace,
  ações e botão "Analisar"), chamando o `ia-assistente`, que por sua vez
  alcançou a API real do Gemini (recusada por falta de chave válida — o
  caminho completo até lá funciona).
- `module.create`/`module.update` e `configuration.import` chamados pelo
  `bootstrap.py` contra a API real, habilitando o widget e importando os
  dois templates.
- Os dois templates (`config/zabbix-templates/*.yaml`) apagados e
  reimportados de volta pela própria API, confirmando que o YAML exportado
  é suficiente para recriá-los do zero.

**Não testado**: o caminho completo pela internet entre o WSL local e uma
VM real da AWS (SSH liberado por `liberar-ip`, túnel e IP descobertos por
`subir-local.sh`), os 15 hosts simulados reportando de verdade, e uma
chave real do Gemini de ponta a ponta.

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
| `local-up` falha ou o tráfego não chega à VM | Porta 22 não liberada para o seu IP atual, ou o túnel caiu | `make vm-liberar-ip`; `./scripts/subir-local.sh logs tunel` |
| Widget "Insights" não aparece para adicionar no dashboard | Módulo não habilitado | `make bootstrap` de novo, ou Administration > General > Modules > Scan directory |
| `ia-assistente` responde "Não consegui falar com..." | Serviço não está de pé | `make ia-up` |
| Botão "Analisar com IA" devolve erro da API do Gemini | `GEMINI_API_KEY` vazia/inválida ou nome do modelo mudou | Conferir a chave no `.env`; ajustar `GEMINI_MODELO` |

## Servidor MCP (opcional)

`make mcp-up` sobe o servidor MCP do Zabbix em `127.0.0.1:8443`. Ele repassa o
token da API a cada requisição, então **não publique essa porta na internet**;
use túnel SSH (`-L 8443:127.0.0.1:8443`). Crie um usuário próprio, com o mínimo
de permissão e token com validade, e libere a API no perfil desse usuário (no
8.0 ela vem desligada para todos os perfis, menos o de super administrador).

## APIs fictícias, Kafka, hosts simulados e IA

Cenário maior, pensado para gerar volume de telemetria realista (milhões de
registros ao longo de alguns dias) e testar o Zabbix como "cérebro" da
ingestão: 5 APIs fictícias (webhook + Kafka), 15 hosts simulados e uma
integração com a API do Gemini para explicar problemas.

**Onde cada peça roda, e por quê:** a VM da AWS continua em ~4GB (sem
redimensionar). Por isso, tudo que pesa mais — as 5 APIs, o Redpanda, o
gerador de tráfego e 10 dos 15 hosts simulados — roda no **seu WSL local**,
não na VM. A VM só ganha dois acréscimos pequenos: 5 hosts simulados leves
(`make hosts-up`) e a ponte com o Gemini (`make ia-up`), juntos menos de 250MB.

```
WSL local (sua máquina)                          VM da AWS (como já é)
┌──────────────────────────────────┐             ┌───────────────────────┐
│ trafego ─┬→ lb-01 → pedidos,      │   OTLP:4317 │ zabbix-proxy → CH      │
│          │          pagamentos    │ ───────────→│                        │
│          └→ lb-02 → catalogo,     │  trapper    │ + host-aws-01..05      │
│                      streaming,   │  :10051     │ + ia-assistente → API  │
│                      autenticacao │ ───────────→│   do Gemini            │
│ pedidos ⇄ redpanda ⇄ pagamentos   │             │                        │
│ (Kafka: "pedido-criado")          │             │ zabbix-web: widget     │
│ 10 hosts simulados (zabbix-agent2)│             │ "Insights" (botão      │
└──────────────────────────────────┘             │ "Analisar")            │
                                                   └───────────────────────┘
```

### Subir no WSL local

```bash
make vm-liberar-ip   # libera só a 22 (SSH) no security group para o seu IP atual
make local-up        # descobre o IP da VM, sobe túnel SSH + APIs fictícias + Redpanda + tráfego + 10 hosts
```

O OTLP (4317) e o trapper (10051) **não são expostos na internet**: no
`bind` da VM eles ficam em `127.0.0.1` e o serviço `tunel` do
`compose.local.yaml` os alcança por um túnel SSH (só a 22 é aberta, e só para
o seu IP). As APIs e os hosts simulados falam com `tunel:<porta>`.

`make local-up` grava o IP atual da VM e a chave SSH num `.env.local` (fora do Git, igual
ao `.env` normal) — rode de novo sempre que a VM religar (o IP muda, mesmo
problema que `vm-liberar-ip` já resolve do lado do SSH). `make local-down`
para tudo; `make gera-carga-de-estresse` dispara um pico de carga pontual
(precisa do `local-up` rodando antes).

As 5 APIs (`pedidos`, `pagamentos`, `catalogo`, `streaming`, `autenticacao`)
respondem 95% 200 / 3% 404 / 2% 500 por padrão (`API_TAXA_404`/`API_TAXA_500`
em `ferramentas/api_ficticia.py`), com payload e latência variáveis. Ao
"fechar uma compra", `pedidos` publica no tópico Redpanda `pedido-criado` e
incrementa `negocio.faturamento`/`negocio.carrinho_abandonado`; `pagamentos`
consome esse tópico e confirma de volta por **webhook HTTP** em `pedidos` —
cobre Kafka e webhook no mesmo fluxo. `streaming` registra
`negocio.buffering_s`. `lb-01`/`lb-02` (nginx) fazem o papel das "máquinas
que sobraram" como balanceador das outras.

### Na VM: hosts simulados e IA

```bash
make hosts-up   # 5 agentes leves nesta VM
make ia-up      # ponte com o Gemini (precisa de GEMINI_API_KEY no .env)
```

A chave do Gemini (`GEMINI_API_KEY`, gerada em aistudio.google.com) e um
token de API do Zabbix com permissão mínima (`ZBX_IA_API_TOKEN`, só
`event.acknowledge`) vão no `.env` da VM — nunca chegam ao navegador. Dois
usos, nos moldes do artigo
["Integrating Zabbix with Gemini"](https://www.linkedin.com/pulse/integrating-zabbix-gemini-arian-afshar-uqm9f/):

- **Webhook de problema**: configure um media type webhook (Administration
  > Alerts > Media types) que faça `POST` para `http://ia-assistente:8090/analisar`
  com o contexto do evento; ligue-o a uma action restrita às triggers de
  anomalia (ver abaixo) — os triggers nativos decidem **quando** há
  problema, o Gemini só explica **o quê** aconteceu (controla o custo de
  chamadas à API).
- **Widget "Insights" no dashboard**: módulo em `config/zabbix-widgets/insights/`,
  habilitado automaticamente pelo `make bootstrap`. Adicione-o a um
  dashboard e clique em "Analisar com IA" para um resumo dos problemas
  recentes.

### Templates e triggers de anomalia

`make bootstrap` importa automaticamente (`configuration.import`) os
templates em `config/zabbix-templates/`:

- **APM API fictícia**: métricas HTTP de cada API fictícia (requisições, erros
  4xx/5xx, taxa de erro, latência média e p95) via *Telemetry query* sobre
  `otel_traces`, no padrão "mestre agrupado + dependentes" (ver abaixo). Um host
  por API (`pedidos`, `pagamentos`, ...) com a macro `{$API_SERVICE}`.
- **OTel ClickHouse APM**: visão geral da telemetria (spans, erros, logs,
  métricas, frescor da ingestão) e descoberta (LLD) de serviços.
- **Host simulado - latência (IA)**: item de latência via `UserParameter`
  (checado pelo próprio agente — não por `icmppingsec`, que o proxy não
  conseguiria disparar contra os agentes do WSL local, atrás de NAT). Ligue
  também o template nativo "Linux by Zabbix agent active" para
  filesystem/rede padrão.

Ambos os templates têm uma trigger com `baselinedev()` (desvio sobre a
média da mesma hora nos últimos 7 dias) — é assim que o Zabbix sinaliza
anomalia sem IA nenhuma. Como depende de histórico, só fica útil depois de
alguns dias de `trafego` rodando.

### Importar os templates (`make templates`)

```bash
make templates          # importa/atualiza templates, hosts e dashboard (idempotente)
make templates-status   # mostra, por host, cada item (ok/NOT SUPPORTED, último valor) e as triggers
```

`make templates` chama `scripts/importar-templates.sh` (senha do Admin lida do
`.env`, nunca impressa), que roda `ferramentas/importar_templates.py`: importa
**todos** os `config/zabbix-templates/*.yaml` com `configuration.import`
(somente criar/atualizar, **sem** `deleteMissing`), garante o host `otel-apm-lab`
e um host por API fictícia (monitorados pelo proxy `zabbix-proxy-otel`, com
`{$API_SERVICE}`) e recria o dashboard "OTel APM (ClickHouse)". Pode rodar quantas
vezes quiser. Antes da primeira importação, `scripts/backup.sh` guarda um dump
do Postgres.

#### Limitação do trunk: macros não são expandidas no `query`

No Zabbix 8 trunk, itens *Telemetry query* (tipo 24) **não expandem macros**
(`{$...}` nem `{#...}`) dentro do campo `query`. Um filtro como
`ServiceName = {$API_SERVICE}` é enviado ao ClickHouse **literalmente** e nunca
casa, então o item não coleta. O contorno usado nos dois templates:

1. **Itens mestres** (`apm.api.status.raw`, `apm.api.latencia.raw`) fazem
   `GROUP BY ServiceName` (e código HTTP) e devolvem **uma linha JSON por grupo**
   a cada minuto (sem filtro por serviço).
2. **Itens dependentes** (`apm.requisicoes.row`, `apm.erros5xx.row`,
   `apm.latencia.media`, ...) filtram o serviço no pré-processamento
   (JSONPath `$[?(@.ServiceName == "{$API_SERVICE}")]...`), onde macros de
   usuário e de LLD **funcionam**.
3. Itens calculados somam as linhas (`apm.erros5xx` = soma de 1h,
   `apm.erro.pct`, `apm.requisicoes.rate`).

As chaves e nomes antigos (`apm.erros5xx`, `apm.latencia.media`) foram mantidos.
O trigger "Pico de erros 5xx fora do padrão" usa `baselinedev()`: fica em estado
de erro ("not enough data") até haver ~7 dias de histórico da mesma hora, o que é
esperado; a trigger "taxa de erro 5xx alta" avalia desde o primeiro ciclo.

#### Adicionar um novo serviço ou rota

- **Novo serviço**: crie um host (ou rode `API_HOSTS=pedidos,...,novo make templates`),
  monitorado pelo proxy, com a macro `{$API_SERVICE}` = `service.name` do app e o
  template "APM API ficticia". Nada muda nos mestres: eles já agrupam todos os serviços.
- **Nova rota**: inclua `SpanName` (ou o atributo `url.path`) em `columns` do
  mestre `apm.api.status.raw`, crie uma macro `{$API_ROTA}` e acrescente
  `&& @.SpanName == "{$API_ROTA}"` ao JSONPath de um item dependente novo.
  Cuidado com a cardinalidade (uma linha por rota e status por minuto).
- Limiares: macros `{$API.ERRO.PCT.WARN}` / `{$API.ERRO.PCT.RECOVERY}` (template ou host).

#### Reverter

- Restaurar tudo: `scripts/restore.sh` (dump do Postgres feito por `scripts/backup.sh`).
- Só o template/hosts, pela API (token de Admin): `template.delete` com o
  templateid de "APM API ficticia", `host.delete` nos hosts `pedidos`,
  `pagamentos`, `catalogo`, `streaming`, `autenticacao` e `dashboard.delete` em
  "OTel APM (ClickHouse)". Os arquivos anteriores estão em `.backup-p1/`.

> O trunk muda a cada build: ao trocar o digest das imagens (`make digests`),
> rode `make templates && make templates-status` e confira que nenhum item ficou
> "NOT SUPPORTED".

## Backup agendado

- **Quando:** todo dia às 02:30 (cron do usuário `suporte` no WSL), mais uma entrada `@reboot` que,
  3 minutos após o WSL subir, roda o backup só se ainda não existir o do dia (compensa PC desligado à noite).
- **O que roda:** `scripts/backup-rotacao.sh` (espera Postgres/ClickHouse healthy por até 5 min, executa
  `scripts/backup.sh`, confere `sha256sum -c SHA256SUMS` e só então aplica a retenção).
- **Retenção:** 7 dias. Apaga apenas `~/backups/zbx8lab-AAAAMMDD` com data no nome anterior a hoje-7;
  nunca o de hoje nem o mais recente, e nada é apagado se o backup ou a verificação falhar.
- **Local:** `~/backups/zbx8lab-AAAAMMDD/` (fora do repositório).
- **Log:** `~/backups/backup.log` (data/hora e resultado; mantém as últimas 500 linhas).
  Última saída bruta: `~/backups/.ultima-execucao.out`.
- **Restaurar:** `scripts/restore.sh ~/backups/zbx8lab-AAAAMMDD` (modo teste, em containers descartáveis).
  Não use `--sobrescrever-lab` sem necessidade: ele substitui os dados do laboratório.
- **Desativar:** `crontab -e` e comentar (`#`) as linhas entre `# --- zbx8lab` e `# --- fim zbx8lab`.
  Rodar manualmente: `scripts/backup-rotacao.sh`.
- **Limitação:** só roda com o Windows ligado. O `.wslconfig` define `vmIdleTimeout=86400000` e
  `instanceIdleTimeout=-1` para o WSL não desligar por ociosidade (mantém ~1,5 GB de RAM do host ocupados).
  Os containers `zbx8lab-*` voltam sozinhos após reiniciar o WSL (`unless-stopped`).

## Próximas etapas

1. Sincronizar automaticamente a macro `{$LATENCIA_ALVO}` dos hosts do WSL
   local com o IP atual da VM (hoje é manual).
2. Infraestrutura como código (Terraform e Ansible) e exposição com HTTPS.
3. Página de vitrine no GitHub Pages.

## Estrutura

```
compose.yaml                      serviços do laboratório (VM da AWS)
compose.local.yaml                APIs fictícias, Redpanda, tráfego e 10 hosts (WSL local)
.env.example                      modelo de configuração da VM (o .env real fica fora do Git)
.env.local.example                modelo informativo do .env.local (gerado por subir-local.sh)
Makefile                          atalhos (make ajuda)
config/clickhouse/init/           criação das tabelas otel_*
config/clickhouse/config.d/       ajustes de memória do ClickHouse
config/proxy/                     configuração do proxy com o coletor OpenTelemetry
config/zabbix-agent/              item de latência dos hosts simulados (UserParameter)
config/nginx/                     balanceadores das APIs fictícias (lb-01, lb-02)
config/zabbix-templates/          templates "APM API fictícia", "OTel ClickHouse APM" e "Host simulado" (make templates importa)
config/zabbix-widgets/insights/   módulo do widget "Insights" (botão "Analisar" -> Gemini)
ferramentas/                      gerador, bootstrap, APIs fictícias, tráfego e ia-assistente
scripts/                          gerar-env, preflight, verificar, subir-tudo, subir-local e deploy-aws
```

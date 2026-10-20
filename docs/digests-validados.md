# Digests de imagens validados

Data: 2026-10-20. Digests conferidos com `docker image inspect` (RepoDigests) nas imagens em uso e
fixados no `compose.yaml` no formato `imagem:tag@sha256:...` (a tag continua legível; o conteúdo fica fixo).

| Imagem | Digest |
|---|---|
| `postgres:18-alpine` | `sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873` |
| `clickhouse/clickhouse-server:26.8-alpine` | `sha256:58f554637bfe3b77e0335b4ff90611279dbdf8d262dc15225ee93901853dfa13` |
| `zabbix/zabbix-server-pgsql:alpine-trunk` | `sha256:22c4ee4a5448b714d54f184dac6e2e1d6a2c5b5a2beb47cb9f89de5179761cc9` |
| `zabbix/zabbix-web-nginx-pgsql:alpine-trunk` | `sha256:5bc8fd14727cd1ec5cce6682a4956b31211c2e452f71af2832775e58d4aa7e80` |
| `zabbix/zabbix-proxy-sqlite3:alpine-trunk` | `sha256:c9ce82c24db5e679e1f5023ccc80ca0fd86e4d9dd85f5dca86df4bf50983317c` |
| `zabbix/zabbix-agent2:alpine-trunk` | `sha256:83564398cddf59bfd9907c5bc89991fbdc746a7d77d820023e8d2b4d5d5449b1` |
| `alpine:3.20` (compose.local.yaml) | `sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc` |
| `redpandadata/redpanda:latest` (compose.local.yaml) | `sha256:c98c2f04a751e6646012cc701bac5183252cee44c88ba1dfa412e4c7124f2e89` |
| `nginx:alpine` (compose.local.yaml) | `sha256:df221db836e1754089190208cee7eeda94f233197056426eda74a43ab1abeac2` |
| `zbx8lab-ferramentas:local` | imagem construída localmente (sem digest de registro; id `f4366a1f2088...`) |

## Pendentes (não estavam baixadas localmente)

- `zabbix/zabbix-mcp-server:${ZBX_TAG:-alpine-trunk}` (perfil `mcp`): fixar quando a imagem for baixada (`make mcp-up`).
- `python:3.13-slim` (base de `ferramentas/Dockerfile`): fixar por digest quando a base estiver baixada.

## Como atualizar digests (bump)

1. Baixar a nova versão: `docker pull <imagem>:<tag>` (ou `docker compose pull`).
2. Validar: obter o digest com `docker image inspect <imagem>:<tag> --format '{{json .RepoDigests}}'`
   e testar em ambiente descartável (subir o lab e conferir os containers `healthy`).
3. Fazer backup do `compose.yaml` e do `ferramentas/Dockerfile` em `.backup-p1/` (sufixo incremental).
4. Atualizar o `@sha256:...` no `compose.yaml` (e `compose.local.yaml`) e a tabela acima; validar com `docker compose config -q`.
5. `docker compose up -d` e conferir o estado de saúde de todos os serviços.
6. `make templates` para reimportar os templates e `make verificar` (scripts/verificar.sh) para validar a stack.
7. Commitar a mudança (`chore(compose): atualiza digests ...`).

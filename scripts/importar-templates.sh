#!/usr/bin/env bash
# Importa (cria/atualiza, nunca apaga) todos os templates de config/zabbix-templates/
# (OTel ClickHouse APM, APM API ficticia, Host simulado), o host lógico otel-apm-lab,
# os hosts das APIs fictícias (monitorados pelo proxy) e o dashboard "OTel APM (ClickHouse)".
# Idempotente. A senha do Admin vem do .env e não é impressa.
#
#   scripts/importar-templates.sh               # importa tudo
#   scripts/importar-templates.sh --status      # só mostra o estado dos itens/triggers dos hosts
#   scripts/importar-templates.sh --sem-dashboard
#
# Recomendado antes da primeira importação: scripts/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "[erro] .env não encontrado (rode 'make env')" >&2; exit 1; }
ZBX_ADMIN_PASSWORD="$(grep -E '^ZBX_ADMIN_PASSWORD=' .env | head -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//')"
WEB_PORT="$(grep -E '^ZBX_WEB_PORT=' .env | head -1 | cut -d= -f2- || true)"
export ZBX_ADMIN_PASSWORD
export ZBX_API_URL="${ZBX_API_URL:-http://127.0.0.1:${WEB_PORT:-8080}/api_jsonrpc.php}"

exec python3 ferramentas/importar_templates.py "$@"

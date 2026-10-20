#!/usr/bin/env bash
# Configura no Zabbix o fluxo do investigador (idempotente). Senhas e tokens
# vêm do .env e não são impressos.
#
#   scripts/configurar-investigador.sh             # cria/atualiza tudo
#   scripts/configurar-investigador.sh --desligar  # desliga action + media type
#   scripts/configurar-investigador.sh --ligar     # religa
#   scripts/configurar-investigador.sh --remover   # apaga tudo o que foi criado
#   scripts/configurar-investigador.sh --status
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "[erro] .env não encontrado (rode 'make env')" >&2; exit 1; }
./scripts/gerar-env.sh >/dev/null
WEB_PORT="$(grep -E '^ZBX_WEB_PORT=' .env | head -1 | cut -d= -f2- || true)"
export ZBX_API_URL="${ZBX_API_URL:-http://127.0.0.1:${WEB_PORT:-8080}/api_jsonrpc.php}"
export ENV_FILE="$PWD/.env"

exec python3 ferramentas/configurar_investigador.py "$@"

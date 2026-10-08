#!/usr/bin/env bash
# Cria o arquivo .env a partir do .env.example e gera as senhas.
# Não sobrescreve um .env que já existe.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f .env ]; then
  echo "O arquivo .env já existe; nada foi alterado."
  echo "Para gerar de novo, apague o .env (e rode 'make limpar' se os volumes já existirem)."
  exit 0
fi

gerar() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 16
  else
    head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

umask 077
awk -v pg="$(gerar)" -v ch="$(gerar)" -v adm="$(gerar)" '
  /^POSTGRES_PASSWORD=/   { print "POSTGRES_PASSWORD=" pg;   next }
  /^CLICKHOUSE_PASSWORD=/ { print "CLICKHOUSE_PASSWORD=" ch; next }
  /^ZBX_ADMIN_PASSWORD=/  { print "ZBX_ADMIN_PASSWORD=" adm; next }
  { print }
' .env.example > .env
chmod 600 .env

echo "Arquivo .env criado com senhas novas (permissão 600)."
echo "A senha do usuário Admin do Zabbix é o valor de ZBX_ADMIN_PASSWORD:"
echo "  grep ZBX_ADMIN_PASSWORD .env"

#!/usr/bin/env bash
# Cria o arquivo .env a partir do .env.example e gera as senhas/tokens.
# Não sobrescreve um .env que já existe: nesse caso só ACRESCENTA as variáveis
# geradas que estiverem faltando (ex.: tokens do investigador), sem imprimir
# nenhum valor.
set -euo pipefail

cd "$(dirname "$0")/.."

gerar() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "${1:-16}"
  else
    head -c "${1:-16}" /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

# Variáveis geradas automaticamente: nome:bytes
GERADAS="POSTGRES_PASSWORD:16 CLICKHOUSE_PASSWORD:16 ZBX_ADMIN_PASSWORD:16 CLICKHOUSE_RO_PASSWORD:16 INVESTIGADOR_TOKEN:24 IA_ASSISTENTE_TOKEN:24"

umask 077

if [ -f .env ]; then
  acrescentadas=""
  for par in $GERADAS; do
    nome=${par%%:*}; bytes=${par#*:}
    case "$nome" in POSTGRES_PASSWORD|CLICKHOUSE_PASSWORD|ZBX_ADMIN_PASSWORD) continue ;; esac
    if ! grep -qE "^${nome}=.+" .env; then
      sed -i "/^${nome}=$/d" .env
      printf '%s=%s\n' "$nome" "$(gerar "$bytes")" >> .env
      acrescentadas="$acrescentadas $nome"
    fi
  done
  chmod 600 .env
  if [ -n "$acrescentadas" ]; then
    echo "O .env já existia; acrescentei (valores não exibidos):$acrescentadas"
  else
    echo "O arquivo .env já existe e está completo; nada foi alterado."
  fi
  exit 0
fi

awk -v pg="$(gerar)" -v ch="$(gerar)" -v adm="$(gerar)" -v ro="$(gerar)" -v inv="$(gerar 24)" -v ia="$(gerar 24)" '
  /^POSTGRES_PASSWORD=/      { print "POSTGRES_PASSWORD=" pg;      next }
  /^CLICKHOUSE_PASSWORD=/    { print "CLICKHOUSE_PASSWORD=" ch;    next }
  /^ZBX_ADMIN_PASSWORD=/     { print "ZBX_ADMIN_PASSWORD=" adm;    next }
  /^CLICKHOUSE_RO_PASSWORD=/ { print "CLICKHOUSE_RO_PASSWORD=" ro; next }
  /^INVESTIGADOR_TOKEN=/     { print "INVESTIGADOR_TOKEN=" inv;    next }
  /^IA_ASSISTENTE_TOKEN=/    { print "IA_ASSISTENTE_TOKEN=" ia;    next }
  { print }
' .env.example > .env
chmod 600 .env

echo "Arquivo .env criado com senhas e tokens novos (permissão 600)."
echo "A senha do usuário Admin do Zabbix é o valor de ZBX_ADMIN_PASSWORD:"
echo "  grep ZBX_ADMIN_PASSWORD .env"

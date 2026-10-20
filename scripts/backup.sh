#!/usr/bin/env bash
# Backup lógico local do laboratório: Postgres (pg_dump -Fc + roles) + tabelas otel_* do ClickHouse.
# Saída: ~/backups/zbx8lab-AAAAMMDD/ (ou $BACKUP_DIR/zbx8lab-AAAAMMDD). Não para nenhum container
# e não instala nada no host. Pode ser reexecutado no mesmo dia (sobrescreve os arquivos do dia).
#
# Arquivos gerados: postgres-zabbix.dump, postgres-globals.sql (roles, SEM senhas),
#   <tabela>.schema.sql e <tabela>.native.gz (tabela vazia = sem .native.gz), COUNTS.txt, SHA256SUMS.
#
# RESTAURAR (recomendado): scripts/restore.sh <dir-do-backup>   # padrão: containers/bancos NOVOS e descartáveis
#   scripts/restore.sh <dir> --sobrescrever-lab                  # sobrescreve o lab (pede confirmação digitada)
# Restore manual do Postgres (o role 'zabbix' precisa existir: restaure postgres-globals.sql antes):
#   - banco NOVO e vazio: createdb -O zabbix zabbix && \
#       pg_restore -U zabbix -d zabbix --role=zabbix --no-owner --exit-on-error < postgres-zabbix.dump
#   - sobrescrevendo banco existente: pg_restore ... --clean --if-exists --no-owner --exit-on-error
#     (sem --if-exists o --clean gera ~1268 erros/exit 1). PARE zabbix-server e zabbix-web antes
#     (docker compose stop zabbix-server zabbix-web) e suba-os depois.
# ClickHouse: veja restore.sh. Tabelas criadas a partir de <t>.schema.sql; dados via INSERT ... FORMAT Native.
#   ATENÇÃO: o schema salvo tem TTL de 7 dias (ttl_only_drop_parts=1): dados antigos restaurados seriam
#   apagados. O restore.sh remove o TTL antes de inserir e imprime o MODIFY TTL para reaplicar.
#
# LIMITAÇÃO: não há snapshot consistente entre Postgres e ClickHouse (nem entre tabelas): o laboratório
# continua gravando durante o backup. Por isso COUNTS.txt guarda a contagem ANTES e DEPOIS dos dumps
# (chave antes depois); o restore considera OK uma contagem restaurada dentro desse intervalo.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

DEST="${BACKUP_DIR:-$HOME/backups}/zbx8lab-$(date +%Y%m%d)"
mkdir -p "$DEST"
cleanup() { rm -f "$DEST"/*.tmp; }
trap cleanup EXIT
echo "Destino: $DEST"

PG_COUNT_TABLES="hosts items users triggers history history_uint"

# Senhas nunca vão em argv: Postgres usa o socket local do container; ClickHouse lê
# $CLICKHOUSE_PASSWORD de dentro do próprio container.
pgx() { docker compose exec -T postgres sh -c "$1" </dev/null; }
ch() { docker compose exec -T clickhouse sh -c 'clickhouse-client --user zabbix --password "$CLICKHOUSE_PASSWORD" "$@"' sh "$@" </dev/null; }
pgq() { docker compose exec -T postgres sh -c 'psql -U "${POSTGRES_USER:-zabbix}" -d "${POSTGRES_DB:-zabbix}" -Atq -c "$1"' sh "$1" </dev/null; }

mapfile -t CH_TABLES < <(ch --query "SELECT name FROM system.tables WHERE database='zabbix' AND name LIKE 'otel\_%' ORDER BY name")

collect_counts() { # $1 = arquivo de saída: linhas "chave valor"
  : > "$1"
  local t v
  for t in $PG_COUNT_TABLES; do
    v=$(pgq "SELECT count(*) FROM $t" 2>/dev/null) || v=NA
    echo "pg.$t ${v:-NA}" >> "$1"
  done
  for t in "${CH_TABLES[@]}"; do
    v=$(ch --query "SELECT count() FROM zabbix.$t") || v=NA
    echo "ch.$t ${v:-NA}" >> "$1"
  done
}

echo "== Contagens (antes)"
collect_counts "$DEST/counts.before.tmp"

echo "== Postgres (pg_dump -Fc + roles sem senha)"
pgx 'pg_dump -U "${POSTGRES_USER:-zabbix}" -Fc "${POSTGRES_DB:-zabbix}"' > "$DEST/postgres-zabbix.dump.tmp"
mv "$DEST/postgres-zabbix.dump.tmp" "$DEST/postgres-zabbix.dump"
pgx 'pg_dumpall -U "${POSTGRES_USER:-zabbix}" --globals-only --no-role-passwords' > "$DEST/postgres-globals.sql.tmp"
mv "$DEST/postgres-globals.sql.tmp" "$DEST/postgres-globals.sql"

echo "== ClickHouse (tabelas otel_*)"
for t in "${CH_TABLES[@]}"; do
  ch --query "SHOW CREATE TABLE zabbix.$t FORMAT TSVRaw" > "$DEST/$t.schema.sql.tmp"
  mv "$DEST/$t.schema.sql.tmp" "$DEST/$t.schema.sql"
  ch --query "SELECT * FROM zabbix.$t FORMAT Native" | gzip -c > "$DEST/$t.native.gz.tmp"
  raw=$(gzip -l "$DEST/$t.native.gz.tmp" | awk 'NR==2{print $2}')
  if [ "${raw:-0}" = "0" ]; then
    rm -f "$DEST/$t.native.gz.tmp" "$DEST/$t.native.gz"
    echo "   $t: vazia (0 linhas) - só schema, sem .native.gz"
  else
    mv "$DEST/$t.native.gz.tmp" "$DEST/$t.native.gz"
    echo "   $t: ok"
  fi
done

echo "== Contagens (depois) -> COUNTS.txt"
collect_counts "$DEST/counts.after.tmp"
{
  echo "# chave antes depois  (gerado em $(date -Is); NA = tabela inexistente/erro)"
  paste -d' ' "$DEST/counts.before.tmp" "$DEST/counts.after.tmp" | awk '{print $1, $2, $4}'
} > "$DEST/COUNTS.txt.tmp"
mv "$DEST/COUNTS.txt.tmp" "$DEST/COUNTS.txt"
rm -f "$DEST/counts.before.tmp" "$DEST/counts.after.tmp"

# Lista explícita (nunca inclui o próprio SHA256SUMS nem .tmp) -> reexecução no mesmo dia é segura.
( cd "$DEST"
  shopt -s nullglob
  files=(postgres-zabbix.dump postgres-globals.sql COUNTS.txt *.schema.sql *.native.gz)
  sha256sum "${files[@]}" > SHA256SUMS.tmp
  mv SHA256SUMS.tmp SHA256SUMS )

echo "== Tamanhos"
du -h "$DEST"/* | sort -h
echo "Total: $(du -sh "$DEST" | cut -f1)"

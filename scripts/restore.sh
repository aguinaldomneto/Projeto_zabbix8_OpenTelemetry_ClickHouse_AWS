#!/usr/bin/env bash
# Restaura um backup gerado por scripts/backup.sh.
# Uso: restore.sh <dir-do-backup> [--alvo-teste (padrão) | --sobrescrever-lab] [--manter]
#
#  --alvo-teste        (padrão) Sobe containers NOVOS e descartáveis (restoretest-pg / restoretest-ch,
#                      --network none, 512m, volumes restoretest_pg / restoretest_ch), restaura neles,
#                      compara com COUNTS.txt e remove tudo ao final. NÃO toca no laboratório.
#  --manter            (modo teste) não remove containers/volumes ao final. Remoção manual dos
#                      containers restoretest-pg/-ch e dos volumes restoretest_pg/_ch.
#  --sobrescrever-lab  Restaura SOBRE o laboratório (Postgres --clean --if-exists; tabelas otel_* recriadas).
#                      Para zabbix-server/zabbix-web/zabbix-proxy durante a operação e exige confirmação digitada.
#
# TTL: o schema salvo tem TTL de 7 dias (ttl_only_drop_parts=1). Em --alvo-teste o TTL é REMOVIDO antes da
# carga (senão as partições restauradas seriam apagadas) e o comando para reaplicar é impresso. Em
# --sobrescrever-lab o TTL é reaplicado automaticamente ao final.
# Tabelas vazias no backup (sem .native.gz) são criadas vazias; arquivos .native.gz sem conteúdo
# (formato antigo, 20 bytes) são pulados (evita erro 108 NO_DATA_TO_INSERT).
set -euo pipefail
LABDIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$LABDIR"

MODE=teste; KEEP=0; DIR=""
for a in "$@"; do
  case "$a" in
    --alvo-teste) MODE=teste ;;
    --sobrescrever-lab) MODE=lab ;;
    --manter) KEEP=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*) echo "Opção desconhecida: $a" >&2; exit 2 ;;
    *) DIR="$a" ;;
  esac
done
[ -n "$DIR" ] && [ -d "$DIR" ] || { echo "Uso: $0 <dir-do-backup> [--alvo-teste|--sobrescrever-lab] [--manter]" >&2; exit 2; }
DIR="$(cd "$DIR" && pwd)"

FAIL=0
fail() { echo "ERRO: $*" >&2; FAIL=1; }

echo "== Verificando SHA256SUMS em $DIR"
[ -f "$DIR/SHA256SUMS" ] || { echo "SHA256SUMS ausente" >&2; exit 1; }
( cd "$DIR" && sha256sum -c SHA256SUMS ) || { echo "Checksum inválido: abortando." >&2; exit 1; }
[ -f "$DIR/postgres-zabbix.dump" ] || { echo "postgres-zabbix.dump ausente" >&2; exit 1; }

TMPD="$(mktemp -d)"; chmod 700 "$TMPD"
CREATED_C=(); CREATED_V=(); STOPPED=0
cleanup() {
  rm -r -f "$TMPD"
  if [ "$MODE" = lab ] && [ "$STOPPED" = 1 ]; then
    echo "== Subindo novamente zabbix-server/zabbix-web/zabbix-proxy"
    docker compose start zabbix-server zabbix-web zabbix-proxy </dev/null >/dev/null 2>&1 || true
  fi
  if [ "$MODE" = teste ]; then
    if [ "$KEEP" = 1 ]; then
      if [ ${#CREATED_C[@]} -gt 0 ]; then echo "Mantidos: ${CREATED_C[*]} ${CREATED_V[*]} (remova manualmente)"; fi
    else
      for c in "${CREATED_C[@]}"; do docker container rm --force "$c" >/dev/null 2>&1 || true; done
      for v in "${CREATED_V[@]}"; do docker volume rm "$v" >/dev/null 2>&1 || true; done
    fi
  fi
}
trap cleanup EXIT

# ---- abstração de execução (teste x lab) ------------------------------------
PGU=restoreadmin
if [ "$MODE" = teste ]; then
  PGC=restoretest-pg; CHC=restoretest-ch
  pgx() { docker exec -i "$PGC" "$@"; }
  chx() { docker exec -i "$CHC" clickhouse-client "$@"; }
else
  PGU=zabbix
  pgx() { docker compose exec -T postgres "$@"; }
  chx() { docker compose exec -T clickhouse sh -c 'clickhouse-client --user zabbix --password "$CLICKHOUSE_PASSWORD" "$@"' sh "$@"; }
fi

if [ "$MODE" = teste ]; then
  echo "== MODO TESTE: containers descartáveis (o laboratório não é tocado)"
  for c in restoretest-pg restoretest-ch; do
    if docker inspect "$c" >/dev/null 2>&1; then echo "Já existe container $c; remova-o antes." >&2; exit 1; fi
  done
  for v in restoretest_pg restoretest_ch; do
    if docker volume inspect "$v" >/dev/null 2>&1; then echo "Já existe volume $v; remova-o antes." >&2; exit 1; fi
  done
  PGIMG="$(docker inspect -f '{{.Image}}' "$(docker compose ps -q postgres </dev/null 2>/dev/null | head -1)" 2>/dev/null || true)"
  CHIMG="$(docker inspect -f '{{.Image}}' "$(docker compose ps -q clickhouse </dev/null 2>/dev/null | head -1)" 2>/dev/null || true)"
  PGIMG="${RESTORE_PG_IMAGE:-${PGIMG:-postgres:18-alpine}}"
  CHIMG="${RESTORE_CH_IMAGE:-${CHIMG:-clickhouse/clickhouse-server:26.8-alpine}}"
  MEM="${RESTORE_MEM:-512m}"
  docker volume create restoretest_pg >/dev/null; CREATED_V+=(restoretest_pg)
  docker volume create restoretest_ch >/dev/null; CREATED_V+=(restoretest_ch)
  # Rede 'none' => sem acesso externo; 'trust' é seguro porque nada alcança o container.
  docker run -d --name restoretest-pg --network none -m "$MEM" \
    -e POSTGRES_USER="$PGU" -e POSTGRES_HOST_AUTH_METHOD=trust \
    -v restoretest_pg:/var/lib/postgresql "$PGIMG" >/dev/null; CREATED_C+=(restoretest-pg)
  CHCONF=()
  if [ -f config/clickhouse/config.d/baixo-consumo.xml ]; then
    CHCONF=(-v "$LABDIR/config/clickhouse/config.d/baixo-consumo.xml:/etc/clickhouse-server/config.d/baixo-consumo.xml:ro")
  fi
  docker run -d --name restoretest-ch --network none -m "$MEM" \
    -v restoretest_ch:/var/lib/clickhouse "${CHCONF[@]}" "$CHIMG" >/dev/null; CREATED_C+=(restoretest-ch)
else
  echo "!! MODO --sobrescrever-lab: isto SUBSTITUI os dados do Postgres e das tabelas otel_* do laboratório."
  echo "!! Digite exatamente  SOBRESCREVER zbx8lab  para continuar:"
  read -r ans
  [ "$ans" = "SOBRESCREVER zbx8lab" ] || { echo "Confirmação incorreta. Nada foi alterado."; exit 1; }
  docker compose stop zabbix-server zabbix-web zabbix-proxy </dev/null; STOPPED=1
fi

echo "== Aguardando bancos"
ok=0
for i in $(seq 1 60); do
  if [ "$MODE" = teste ]; then
    if docker logs restoretest-pg 2>&1 | grep -q 'init process complete' && pgx pg_isready -U "$PGU" -q; then ok=1; break; fi
  else
    if pgx pg_isready -U "$PGU" -q </dev/null; then ok=1; break; fi
  fi
  sleep 2
done
[ "$ok" = 1 ] || { echo "Postgres não ficou pronto" >&2; exit 1; }
ok=0
for i in $(seq 1 60); do
  if chx --query 'SELECT 1' </dev/null >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ "$ok" = 1 ] || { echo "ClickHouse não ficou pronto" >&2; exit 1; }

# ---- Postgres ---------------------------------------------------------------
echo "== Postgres"
PGLOG="$TMPD/pg_restore.log"
if [ "$MODE" = teste ]; then
  if [ -f "$DIR/postgres-globals.sql" ]; then
    pgx psql -U "$PGU" -d postgres -q -v ON_ERROR_STOP=1 < "$DIR/postgres-globals.sql" >/dev/null || fail "aplicar postgres-globals.sql"
  else
    pgx psql -U "$PGU" -d postgres -q -c 'CREATE ROLE zabbix SUPERUSER LOGIN' </dev/null || fail "criar role zabbix"
  fi
  pgx createdb -U "$PGU" -O zabbix zabbix </dev/null || fail "createdb zabbix"
  PGRESTORE_OPTS=(--role=zabbix --no-owner --exit-on-error)
else
  PGRESTORE_OPTS=(--clean --if-exists --no-owner --exit-on-error)
fi
set +e
pgx pg_restore -U "$PGU" -d zabbix "${PGRESTORE_OPTS[@]}" < "$DIR/postgres-zabbix.dump" > "$PGLOG" 2>&1
PGRC=$?
set -e
PGERR=$(grep -c . "$PGLOG" || true)
echo "   pg_restore: exit=$PGRC, linhas de erro/aviso=$PGERR"
if [ "$PGRC" != 0 ]; then fail "pg_restore exit $PGRC"; head -5 "$PGLOG" >&2; fi

# ---- ClickHouse -------------------------------------------------------------
echo "== ClickHouse"
chx --query 'CREATE DATABASE IF NOT EXISTS zabbix' </dev/null
declare -A TTLEXPR=()
CH_TABLES=()
# Inserção com pouca memória (container de teste de 512m): blocos pequenos e 1 thread.
INSERT_OPTS=(--max_insert_threads=1 --max_threads=1 --max_block_size=4096
             --min_insert_block_size_rows=4096 --min_insert_block_size_bytes=1048576
             --max_insert_block_size=4096 --max_memory_usage=0)
for s in "$DIR"/*.schema.sql; do
  t="$(basename "$s" .schema.sql)"; CH_TABLES+=("$t")
  if [ "$MODE" = lab ]; then chx --query "DROP TABLE IF EXISTS zabbix.$t" </dev/null; fi
  if ! chx --multiquery < "$s"; then fail "criar tabela $t"; continue; fi
  ttl="$(awk '/^TTL /{sub(/^TTL /,""); print; exit}' "$s")"
  if [ -n "$ttl" ]; then
    TTLEXPR[$t]="$ttl"
    chx --query "ALTER TABLE zabbix.$t REMOVE TTL" </dev/null || fail "REMOVE TTL em $t"
  fi
  f="$DIR/$t.native.gz"
  if [ ! -f "$f" ]; then
    echo "   $t: sem dados no backup (tabela vazia) - criada vazia"
  elif [ "$(gzip -l "$f" | awk 'NR==2{print $2}')" = "0" ]; then
    echo "   $t: arquivo vazio (legado) - pulado"
  else
    if gzip -dc "$f" | chx "${INSERT_OPTS[@]}" --query "INSERT INTO zabbix.$t FORMAT Native"; then
      echo "   $t: dados inseridos"
    else
      fail "INSERT em $t"
    fi
  fi
done

# ---- Comparação de contagens ------------------------------------------------
echo "== Comparação com COUNTS.txt"
declare -A CB=() CA=()
if [ -f "$DIR/COUNTS.txt" ]; then
  while read -r k b a; do
    case "$k" in ''|'#'*) continue ;; esac
    CB[$k]="$b"; CA[$k]="${a:-$b}"
  done < "$DIR/COUNTS.txt"
else
  echo "   (COUNTS.txt ausente: backup antigo, sem comparação)"
fi
printf '%-45s %-24s %-12s %s\n' CHAVE ESPERADO RESTAURADO RESULTADO
for k in "${!CB[@]}"; do
  case "$k" in
    pg.*) v=$(pgx psql -U "$PGU" -d zabbix -Atq -c "SELECT count(*) FROM ${k#pg.}" </dev/null 2>/dev/null) || v=ERRO ;;
    ch.*) v=$(chx --query "SELECT count() FROM zabbix.${k#ch.}" </dev/null 2>/dev/null) || v=ERRO ;;
    *) continue ;;
  esac
  echo "$k $v"
done | sort > "$TMPD/restored.txt"
while read -r k v; do
  [ "${CB[$k]}" = NA ] && continue
  if [ "${CB[$k]}" = "${CA[$k]}" ]; then exp="${CB[$k]}"; else exp="${CB[$k]}..${CA[$k]}"; fi
  if [[ "$v" =~ ^[0-9]+$ ]] && [ "$v" -ge "${CB[$k]}" ] && [ "$v" -le "${CA[$k]}" ]; then res=OK; else res=DIFF; fi
  printf '%-45s %-24s %-12s %s\n' "$k" "$exp" "$v" "$res"
done < "$TMPD/restored.txt" | tee "$TMPD/cmp.txt"
NOK=$(grep -c ' OK$' "$TMPD/cmp.txt" || true); NDIFF=$(grep -c ' DIFF$' "$TMPD/cmp.txt" || true)
[ "$NDIFF" = 0 ] || FAIL=1

# ---- TTL --------------------------------------------------------------------
echo "== TTL"
if [ "$MODE" = lab ]; then
  for t in "${!TTLEXPR[@]}"; do
    chx --query "ALTER TABLE zabbix.$t MODIFY TTL ${TTLEXPR[$t]}" </dev/null && echo "   $t: TTL reaplicado"
  done
else
  for t in "${!TTLEXPR[@]}"; do
    has=$(chx --query "SELECT countSubstrings(create_table_query,'TTL') FROM system.tables WHERE database='zabbix' AND name='$t'" </dev/null)
    echo "   $t: TTL na tabela restaurada = $([ "$has" = 0 ] && echo removido || echo "AINDA PRESENTE")"
    [ "$has" = 0 ] || FAIL=1
  done
  echo "   Para reaplicar o TTL depois (cada tabela, expressão original do schema salvo):"
  for t in "${!TTLEXPR[@]}"; do
    echo "     ALTER TABLE zabbix.$t MODIFY TTL ${TTLEXPR[$t]};"
  done | sort
fi

echo "== Resumo: Postgres exit=$PGRC | tabelas CH=${#CH_TABLES[@]} | contagens OK=$NOK DIFF=$NDIFF | $([ "$FAIL" = 0 ] && echo SUCESSO || echo FALHA)"
exit "$FAIL"

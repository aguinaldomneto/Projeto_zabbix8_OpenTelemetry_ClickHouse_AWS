#!/usr/bin/env bash
# Confere, etapa por etapa, se a telemetria está chegando ao Zabbix.
# Só lê informações. Com --diagnostico grava um arquivo para compartilhar
# (sem senhas) quando algo falhar.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

if [ "${1:-}" = "--diagnostico" ]; then
  arquivo="diagnostico-$(date +%Y%m%d-%H%M%S).txt"
  "$0" 2>&1 | tee "$arquivo"
  {
    echo; echo "===== docker compose ps"; docker compose ps -a
    for s in zabbix-proxy zabbix-server zabbix-web clickhouse gerador investigador ia-assistente; do
      [ -n "$(docker compose ps -q "$s" 2>/dev/null)" ] || continue
      echo; echo "===== logs: $s (últimas 60 linhas)"
      docker compose logs --no-color --tail 60 "$s" 2>&1
    done
  } >> "$arquivo"
  echo; echo "Diagnóstico gravado em $arquivo (não contém o .env)."
  exit 0
fi

falhas=0
ok()    { printf '  [ok]    %s\n' "$*"; }
info()  { printf '  [info]  %s\n' "$*"; }
falha() { printf '  [FALHA] %s\n' "$*"; falhas=$((falhas + 1)); }

valor_env() {
  local v=""
  [ -f .env ] && v=$(grep -E "^$1=" .env | tail -1 | cut -d= -f2-)
  printf '%s' "${v:-$2}"
}

# Consulta no ClickHouse usando a senha que já está dentro do container
ch() {
  docker compose exec -T clickhouse sh -c \
    'clickhouse-client --user zabbix --password "$CLICKHOUSE_PASSWORD" --database zabbix --query "$1"' sh "$1" 2>&1
}

echo "== 1. Containers"
for s in postgres clickhouse zabbix-server zabbix-web zabbix-proxy gerador; do
  id=$(docker compose ps -q "$s" 2>/dev/null)
  if [ -z "$id" ]; then falha "$s não existe (rode 'make up')"; continue; fi
  estado=$(docker inspect -f '{{.State.Status}}{{if .State.Health}} / {{.State.Health.Status}}{{end}}' "$id")
  reinicios=$(docker inspect -f '{{.RestartCount}}' "$id")
  case "$estado" in
    running|"running / healthy") ok "$s: $estado" ;;
    "running / starting")        info "$s: ainda iniciando; repita em 1 minuto" ;;
    *)                           falha "$s: $estado" ;;
  esac
  if [ "$reinicios" -gt 2 ]; then
    # O contador do Docker não zera: reinícios antigos não indicam problema
    # se o container está de pé há mais de 5 minutos.
    inicio=$(date -d "$(docker inspect -f '{{.State.StartedAt}}' "$id")" +%s 2>/dev/null || echo 0)
    no_ar=$(( $(date +%s) - inicio ))
    if [ "${estado%% *}" = running ] && [ "$no_ar" -gt 300 ]; then
      info "$s reiniciou $reinicios vezes antes, mas está de pé há $(( no_ar / 60 )) min"
    else
      falha "$s já reiniciou $reinicios vezes (veja 'docker compose logs $s')"
    fi
  fi
done

echo "== 2. ClickHouse"
versao=$(ch "SELECT version()")
if grep -qE '^[0-9]+\.[0-9]+' <<< "$versao"; then
  maior=${versao%%.*}; resto=${versao#*.}; menor=${resto%%.*}
  if [ "$maior" -gt 26 ] || { [ "$maior" -eq 26 ] && [ "$menor" -ge 8 ]; }; then
    ok "versão $versao (26.8 ou mais novo, aceita as datas gravadas pelo proxy)"
  else
    falha "versão $versao: o proxy do Zabbix precisa do 26.8 ou mais novo. Ajuste CLICKHOUSE_IMAGE no .env"
  fi
  tabelas=$(ch "SELECT count() FROM system.tables WHERE database = 'zabbix' AND name LIKE 'otel_%'")
  if [ "$tabelas" = "7" ]; then ok "as 7 tabelas otel_* existem"
  else falha "esperava 7 tabelas otel_*, encontrei '$tabelas' (o script de criação só roda com o volume vazio)"; fi
else
  falha "não consegui consultar o ClickHouse: $versao"
fi

echo "== 3. Proxy"
log_proxy=$(docker compose logs --no-color zabbix-proxy 2>&1)
if grep -qiE 'unknown parameter|cannot.*(open|load).*conf|invalid.*APM' <<< "$log_proxy"; then
  falha "o proxy reclamou da configuração:"
  printf '%s\n' "$log_proxy" | grep -iE 'unknown parameter|cannot.*(open|load).*conf|invalid.*APM' | tail -3 | sed 's/^/            /'
fi
if grep -qiE 'apm (manager|worker)' <<< "$log_proxy"; then
  ok "processos do coletor OpenTelemetry (apm manager/worker) iniciados"
else
  falha "não achei os processos 'apm manager' no log do proxy (StartAPMCollectors não entrou?)"
fi
if grep -qiE 'received configuration data from server' <<< "$log_proxy"; then
  ok "o proxy recebeu configuração do server"
else
  falha "o proxy ainda não recebeu configuração do server (rodou 'make bootstrap'? o nome do proxy bate com ZBX_PROXY_NAME?)"
  printf '%s\n' "$log_proxy" | grep -iE 'cannot|failed|not found' | tail -3 | sed 's/^/            /'
fi

echo "== 4. Telemetria gravada nos últimos 5 minutos"
total=0
for par in "otel_traces:Timestamp:spans" "otel_logs:Timestamp:logs" "otel_metrics_sum:TimeUnix:pontos de contador" \
           "otel_metrics_gauge:TimeUnix:pontos de gauge" "otel_metrics_histogram:TimeUnix:pontos de histograma"; do
  tabela=${par%%:*}; resto=${par#*:}; coluna=${resto%%:*}; nome=${resto#*:}
  n=$(ch "SELECT count() FROM $tabela WHERE $coluna > now() - INTERVAL 5 MINUTE")
  if grep -qE '^[0-9]+$' <<< "$n"; then
    info "$tabela: $n $nome"
    total=$((total + n))
  else
    falha "$tabela: erro na consulta: $n"
  fi
done
if [ "$total" -gt 0 ]; then
  ok "há telemetria chegando ($total linhas em 5 minutos)"
  servicos=$(ch "SELECT arrayStringConcat(groupUniqArray(toString(ServiceName)), ', ') FROM otel_traces WHERE Timestamp > now() - INTERVAL 5 MINUTE")
  [ -n "$servicos" ] && info "serviços com traces: $servicos"
else
  falha "nenhuma linha nos últimos 5 minutos. Se o 'make bootstrap' acabou de rodar, espere 1 a 2 minutos e repita"
fi

erros=$(ch "SELECT concat(toString(count()), ' em ', table, ': ', any(substring(exception, 1, 160))) FROM system.asynchronous_insert_log WHERE event_time > now() - INTERVAL 10 MINUTE AND status != 'Ok' GROUP BY table")
if [ -n "$erros" ] && ! grep -qiE "doesn.t exist|UNKNOWN_TABLE" <<< "$erros"; then
  falha "o ClickHouse recusou inserções nos últimos 10 minutos:"
  printf '%s\n' "$erros" | sed 's/^/            /'
else
  ok "nenhuma inserção recusada pelo ClickHouse nos últimos 10 minutos"
fi

echo "== 5. Frontend"
porta=$(valor_env ZBX_WEB_PORT 8080)
if resposta=$(curl -fsS -m 10 -H 'Content-Type: application/json-rpc' \
      -d '{"jsonrpc":"2.0","method":"apiinfo.version","params":[],"id":1}' \
      "http://127.0.0.1:${porta}/api_jsonrpc.php" 2>&1); then
  ok "API respondendo em 127.0.0.1:${porta}: $(printf '%s' "$resposta" | sed -n 's/.*"result":"\([^"]*\)".*/versão \1/p')"
else
  falha "o frontend não respondeu em 127.0.0.1:${porta}: $resposta"
fi

echo "== 6. Serviços opcionais (hosts simulados, IA) — só os que estiverem de pé"
algum_opcional=0
for s in host-aws-01 host-aws-02 host-aws-03 host-aws-04 host-aws-05 investigador ia-assistente; do
  id=$(docker compose ps -q "$s" 2>/dev/null)
  [ -n "$id" ] || continue
  algum_opcional=1
  estado=$(docker inspect -f '{{.State.Status}}{{if .State.Health}} / {{.State.Health.Status}}{{end}}' "$id")
  case "$estado" in running|"running / healthy") ok "$s: $estado" ;; *) falha "$s: $estado" ;; esac
done
if [ -n "$(docker compose ps -q investigador 2>/dev/null)" ]; then
  # Só o modo (llm / somente-evidencias); o token fica dentro do container.
  modo=$(docker compose exec -T investigador python -c "import urllib.request,os,json;r=urllib.request.Request('http://127.0.0.1:8091/saude',headers={'Authorization':'Bearer '+os.environ.get('INVESTIGADOR_TOKEN','')});d=json.load(urllib.request.urlopen(r,timeout=5));print(d.get('modo','?'),'tokens_hoje='+str(d.get('tokens_hoje','?')))" 2>/dev/null)
  info "investigador: modo ${modo:-desconhecido}"
fi
if [ "$algum_opcional" = 0 ]; then
  info "nenhum (rode 'make hosts-up' / 'make investigador-up' se quiser os hosts simulados desta VM e o investigador)"
fi

echo "== Memória por container"
mapfile -t ids < <(docker compose ps -q 2>/dev/null)
if [ "${#ids[@]}" -gt 0 ]; then
  docker stats --no-stream --format '  {{.Name}}  {{.MemUsage}}' "${ids[@]}" 2>/dev/null
fi

echo
if [ "$falhas" -gt 0 ]; then
  echo "Resultado: $falhas falha(s). Rode 'make diagnostico' e compartilhe o arquivo gerado."
  exit 1
fi
echo "Resultado: tudo certo. Abra o frontend e veja o menu APM > Traces."

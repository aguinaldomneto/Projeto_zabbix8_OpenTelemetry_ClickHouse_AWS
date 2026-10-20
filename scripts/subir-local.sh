#!/usr/bin/env bash
# Sobe (ou para) o que roda no WSL local: as 5 APIs fictícias, o Redpanda, o
# gerador de tráfego e os 10 hosts simulados (compose.local.yaml). NÃO toca
# na VM da AWS. Descobre o IP público atual da VM (ele muda a cada vez que
# ela liga) e grava num .env.local fora do Git — mesmo problema que o
# projeto já resolve do outro lado com "deploy-aws.sh liberar-ip".
#
# Uso: ./scripts/subir-local.sh [up|down|estresse|ps|logs [serviço]]
#
# Nenhuma porta da VM é aberta na internet: o serviço "tunel" do compose leva
# o OTLP e o trapper por um túnel SSH (porta 22). Antes de "up", a VM precisa
# aceitar SSH do seu IP atual: make vm-liberar-ip
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1

ARQ_ENV=.env.local
COMPOSE=(docker compose -f compose.local.yaml --env-file "$ARQ_ENV")

atualizar_ip() {
  local ip
  if ! ip=$(./scripts/deploy-aws.sh ip); then
    echo "[erro] não consegui descobrir o IP da VM pela AWS." >&2
    echo "       Confira 'make vm-status', ou crie $ARQ_ENV à mão com VM_IP=..." >&2
    exit 1
  fi

  local chave
  if ! chave=$(./scripts/deploy-aws.sh chave); then
    echo "[erro] não achei a chave SSH da VM. Informe: SSH_KEY=/caminho/da/chave $0 up" >&2
    exit 1
  fi

  : "${OTLP_PORT:=4317}"
  : "${ZBX_TRAPPER_PORT:=10051}"
  : "${TRAFEGO_REQ_S:=3}"

  cat > "$ARQ_ENV" <<EOF
# Gerado por scripts/subir-local.sh — não edite à mão, rode o script de novo
# para atualizar o IP (ele muda sempre que a VM liga).
VM_IP=$ip
SSH_KEY=$chave
SSH_USER=${SSH_USER:-ubuntu}
OTLP_PORT=$OTLP_PORT
ZBX_TRAPPER_PORT=$ZBX_TRAPPER_PORT
TRAFEGO_REQ_S=$TRAFEGO_REQ_S
EOF
  echo "[ok] IP atual da VM: $ip (gravado em $ARQ_ENV)"
}

case "${1:-up}" in
  up)
    atualizar_ip
    "${COMPOSE[@]}" build
    "${COMPOSE[@]}" up -d
    ;;
  down)
    if [ ! -f "$ARQ_ENV" ]; then
      echo "[info] nada para parar ($ARQ_ENV não existe; rode 'up' primeiro)."
      exit 0
    fi
    "${COMPOSE[@]}" down
    ;;
  estresse)
    [ -f "$ARQ_ENV" ] || { echo "[erro] rode '$0 up' primeiro." >&2; exit 1; }
    "${COMPOSE[@]}" --profile stress run --rm trafego-estresse
    ;;
  ps)
    "${COMPOSE[@]}" ps -a
    ;;
  logs)
    shift
    "${COMPOSE[@]}" logs -f --tail 100 "$@"
    ;;
  *)
    echo "uso: $0 [up|down|estresse|ps|logs [serviço]]" >&2
    exit 1
    ;;
esac

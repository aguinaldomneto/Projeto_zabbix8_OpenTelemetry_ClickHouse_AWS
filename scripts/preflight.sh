#!/usr/bin/env bash
# Confere se a máquina aguenta o laboratório ANTES de subir os containers.
# Só lê informações; não altera nada.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

falhas=0
avisos=0
ok()    { printf '  [ok]    %s\n' "$*"; }
aviso() { printf '  [aviso] %s\n' "$*"; avisos=$((avisos + 1)); }
falha() { printf '  [FALHA] %s\n' "$*"; falhas=$((falhas + 1)); }

valor_env() { # lê uma chave do .env (ou devolve o padrão)
  local v=""
  [ -f .env ] && v=$(grep -E "^$1=" .env | tail -1 | cut -d= -f2-)
  printf '%s' "${v:-$2}"
}

echo "== Máquina"
arq=$(uname -m)
case "$arq" in
  x86_64|aarch64|arm64) ok "arquitetura $arq (as imagens existem para amd64 e arm64)" ;;
  *) falha "arquitetura $arq sem imagem do Zabbix 8.0" ;;
esac

mem_total=$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo)
mem_livre=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
swap_total=$(awk '/^SwapTotal:/ {print int($2/1024)}' /proc/meminfo)

if   [ "$mem_total" -lt 1800 ]; then
  falha "memória total de ${mem_total} MB: não cabe. Zabbix + PostgreSQL + ClickHouse pedem cerca de 2 GB de uso real; use uma instância com 4 GB"
elif [ "$mem_total" -lt 3500 ]; then
  aviso "memória total de ${mem_total} MB: sobe, mas no limite. O recomendado é 4 GB"
else
  ok "memória total de ${mem_total} MB"
fi

if [ "$mem_livre" -lt 1800 ]; then
  aviso "só ${mem_livre} MB livres agora (outros containers ou serviços estão usando o resto). O laboratório precisa de cerca de 2 GB livres"
else
  ok "${mem_livre} MB livres agora"
fi

if [ "$swap_total" -gt 0 ]; then
  ok "swap de ${swap_total} MB"
elif [ "$mem_total" -lt 6000 ]; then
  aviso "sem swap. Com pouca memória, um pico derruba containers (OOM). Vale criar 2 GB de swap (veja o README)"
else
  ok "sem swap, mas a memória é folgada"
fi

echo "== Docker"
if ! command -v docker >/dev/null 2>&1; then
  falha "docker não encontrado"
elif ! docker info >/dev/null 2>&1; then
  falha "o usuário $(id -un) não consegue falar com o Docker (daemon parado ou falta o grupo docker)"
else
  ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)"

  if versao=$(docker compose version --short 2>/dev/null); then
    maior=${versao%%.*}; resto=${versao#*.}; menor=${resto%%.*}
    if [ "${maior#v}" -lt 2 ] 2>/dev/null || { [ "${maior#v}" -eq 2 ] && [ "$menor" -lt 20 ]; } 2>/dev/null; then
      falha "docker compose $versao é antigo; precisa do 2.20 ou mais novo"
    else
      ok "docker compose $versao"
    fi
  else
    falha "plugin 'docker compose' não encontrado (o antigo docker-compose com hífen não serve)"
  fi

  raiz=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)
  if [ -n "$raiz" ] && [ -d "$raiz" ]; then
    livre_gb=$(df -Pk "$raiz" | awk 'NR==2 {print int($4/1048576)}')
    if   [ "$livre_gb" -lt 6 ];  then falha "só ${livre_gb} GB livres em $raiz; as imagens e os dados pedem pelo menos 6 GB"
    elif [ "$livre_gb" -lt 10 ]; then aviso "${livre_gb} GB livres em $raiz; dá para subir, mas acompanhe o disco"
    else ok "${livre_gb} GB livres em $raiz"
    fi
  fi

  rodando=$(docker ps --format '{{.Names}}' | grep -v '^zbx8lab-' | wc -l)
  if [ "$rodando" -gt 0 ]; then
    aviso "$rodando container(s) de outros projetos rodando nesta máquina e dividindo a memória:"
    docker ps --format '            {{.Names}}  ({{.Image}})  portas: {{.Ports}}' | grep -v ' zbx8lab-' | head -15
  fi
fi

echo "== Portas"
bind=$(valor_env BIND_ADDR 127.0.0.1)
for par in "ZBX_WEB_PORT:8080:frontend do Zabbix" "OTLP_PORT:4317:OTLP do proxy"; do
  chave=${par%%:*}; resto=${par#*:}; padrao=${resto%%:*}; nome=${resto#*:}
  porta=$(valor_env "$chave" "$padrao")
  if command -v ss >/dev/null 2>&1 && ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${porta}\$"; then
    if docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep '^zbx8lab-' | grep -q ":${porta}->"; then
      ok "porta $porta ($nome) já está com o próprio laboratório"
    else
      falha "porta $porta ($nome) já está em uso; troque $chave no .env"
    fi
  else
    ok "porta $porta livre ($nome)"
  fi
done
[ "$bind" != "127.0.0.1" ] && aviso "BIND_ADDR=$bind publica as portas para fora da VM; confira o security group"

echo "== Configuração"
if [ ! -f .env ]; then
  falha "arquivo .env não existe; rode 'make env'"
else
  vazias=0
  for chave in POSTGRES_PASSWORD CLICKHOUSE_PASSWORD ZBX_ADMIN_PASSWORD; do
    if [ -z "$(valor_env "$chave" "")" ]; then
      falha "$chave está vazia no .env"
      vazias=$((vazias + 1))
    fi
  done
  [ "$vazias" -eq 0 ] && ok ".env presente com as senhas preenchidas"
fi

echo
if [ "$falhas" -gt 0 ]; then
  echo "Resultado: $falhas falha(s) e $avisos aviso(s). Corrija as falhas antes de subir."
  exit 1
fi
echo "Resultado: sem falhas, $avisos aviso(s). Pode seguir com 'make up'."

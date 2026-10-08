#!/usr/bin/env bash
# Sobe o laboratório inteiro nesta máquina, do zero até a verificação.
# É o que o deploy-aws.sh roda dentro da VM; também pode ser chamado à mão.
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1

make env
make preflight
make up
make bootstrap

for tentativa in 1 2 3 4 5; do
  echo
  echo "Aguardando o proxy receber a configuração e gravar (tentativa $tentativa de 5)..."
  sleep 45
  if make verificar; then
    exit 0
  fi
done

echo
echo "A verificação ainda aponta falha depois de 5 tentativas. Rode: make diagnostico"
exit 3

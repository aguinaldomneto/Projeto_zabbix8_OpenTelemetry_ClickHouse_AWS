#!/usr/bin/env bash
# Suíte do investigador com MOCK do Gemini (custo zero; nada sai da máquina).
#
#   scripts/investigador-teste.sh            # unitários + e2e completo (dispara incidente real, ~8-12 min)
#   scripts/investigador-teste.sh --rapido   # unitários + e2e sem disparar incidente
#
# O e2e baixa TEMPORARIAMENTE o limiar de erro 5xx do host "pedidos" (macro de
# host criada e removida pelo próprio teste) e planta logs sintéticos no
# serviço OTel "teste-injecao". Nenhum dado é apagado. Senhas/tokens vêm do
# .env e não são impressos.
set -uo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "[erro] .env não encontrado (rode 'make env')" >&2; exit 1; }
falhas=0
etapa() { printf '\n== %s\n' "$*"; }

etapa "1. Sintaxe (bash -n, py_compile, compose config)"
for s in scripts/*.sh; do bash -n "$s" || { echo "[FALHOU] bash -n $s"; falhas=$((falhas + 1)); }; done
python3 - <<'EOF' || falhas=$((falhas + 1))
import os, pathlib, py_compile, sys, tempfile
erros = 0
tmp = tempfile.mkdtemp()
for p in pathlib.Path("ferramentas").rglob("*.py"):
    try:
        py_compile.compile(str(p), doraise=True, cfile=os.path.join(tmp, "x.pyc"))
    except py_compile.PyCompileError as e:
        print("[FALHOU]", e); erros += 1
print(f"[ok] py_compile: {erros} erro(s)")
sys.exit(1 if erros else 0)
EOF
docker compose --profile ia --profile ia-legado --profile investigador-teste config -q \
  && echo "[ok] docker compose config -q" || falhas=$((falhas + 1))

etapa "2. Imagem (cache) e testes unitários (sem rede)"
docker compose build -q investigador >/dev/null && echo "[ok] imagem zbx8lab-ferramentas:local"
docker run --rm --network none -m 128m -e PYTHONDONTWRITEBYTECODE=1 -w /app zbx8lab-ferramentas:local \
  python -m unittest discover -s investigador/testes -t . 2>&1 | grep -vE 'ResourceWarning|tracemalloc|^\s+(for|return) ' | tail -8
[ "${PIPESTATUS[0]}" -eq 0 ] || falhas=$((falhas + 1))

etapa "3. Investigador real no ar?"
if [ -z "$(docker compose ps -q investigador 2>/dev/null)" ]; then
  echo "[aviso] investigador não está rodando; subindo (modo somente evidências)"
  docker compose --profile ia up -d --no-deps investigador
fi

etapa "4. Mock do Gemini + investigador de teste"
docker compose --profile investigador-teste up -d --no-deps mock-gemini investigador-teste
sleep 5

etapa "5. Ponta a ponta (e2e)"
ZBX_ADMIN_PASSWORD="$(grep -E '^ZBX_ADMIN_PASSWORD=' .env | head -1 | cut -d= -f2-)"
export ZBX_ADMIN_PASSWORD
docker compose exec -T -e ZBX_ADMIN_PASSWORD investigador-teste python -m investigador.testes.e2e "$@"
[ $? -eq 0 ] || falhas=$((falhas + 1))

etapa "6. Limpeza do ambiente de teste"
docker compose --profile investigador-teste rm -sf mock-gemini investigador-teste >/dev/null 2>&1 && echo "[ok] mock e investigador-teste removidos"
docker ps --filter label=zbx8lab.tmp=api-ficticia --format '{{.Names}}' | wc -l | xargs -I{} echo "[info] containers tmp-api rodando: {}"
free -m | sed -n '1,2p'
docker stats --no-stream --format '  {{.Name}}  {{.MemUsage}}' zbx8lab-investigador-1 2>/dev/null

echo
if [ "$falhas" -gt 0 ]; then echo "Resultado: $falhas etapa(s) com falha."; exit 1; fi
echo "Resultado: suíte do investigador OK (nenhuma chamada real ao Gemini)."

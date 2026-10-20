#!/usr/bin/env bash
# Backup diário com verificação e retenção (usado pelo cron do usuário).
# Fluxo: lock -> espera postgres/clickhouse healthy (até 5 min) -> scripts/backup.sh
#        -> sha256sum -c SHA256SUMS do backup de hoje -> SÓ ENTÃO retenção de 7 dias.
# Se o backup ou a verificação falhar: nada é apagado e o script sai com erro.
#
# Variáveis (úteis para testes): BACKUP_ROOT (padrão ~/backups), RETENCAO_DIAS (7),
#   ESPERA_MAX_S (300), PULAR_ESPERA=1 (não consulta o Docker), BACKUP_SCRIPT (padrão scripts/backup.sh).
# Retenção: só diretórios <BACKUP_ROOT>/zbx8lab-AAAAMMDD (maxdepth 1) cuja DATA NO NOME seja anterior
#   a hoje-RETENCAO_DIAS; nunca o de hoje nem o mais recente.
set -euo pipefail
umask 077

LAB="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/backups}"
RETENCAO_DIAS="${RETENCAO_DIAS:-7}"
ESPERA_MAX_S="${ESPERA_MAX_S:-300}"
BACKUP_SCRIPT="${BACKUP_SCRIPT:-$LAB/scripts/backup.sh}"
LOG="$BACKUP_ROOT/backup.log"
LOG_LINHAS=500
HOJE="$(date +%Y%m%d)"
DEST="$BACKUP_ROOT/zbx8lab-$HOJE"
SAIDA="$BACKUP_ROOT/.ultima-execucao.out"

mkdir -p "$BACKUP_ROOT"
log() { printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$LOG"; }
rotaciona_log() { local t; t="$(mktemp "$BACKUP_ROOT/.log.XXXXXX")"; tail -n "$LOG_LINHAS" "$LOG" > "$t" && mv "$t" "$LOG"; }
trap 'rotaciona_log || true' EXIT

# Lock: impede execuções concorrentes (cron + @reboot + manual).
exec 9>"$BACKUP_ROOT/.backup.lock"
if ! flock -n 9; then log "ABORTADO: outra execução em andamento"; exit 3; fi

log "INICIO backup-rotacao (destino $DEST)"

# Espera os bancos ficarem healthy (útil logo após o boot do WSL).
if [ "${PULAR_ESPERA:-0}" != "1" ]; then
  limite=$((SECONDS + ESPERA_MAX_S))
  while :; do
    ok=1
    for s in postgres clickhouse; do
      st="$(cd "$LAB" && docker compose ps --format '{{.Health}}' "$s" 2>/dev/null | head -n1 || true)"
      [ "$st" = "healthy" ] || ok=0
    done
    [ "$ok" = 1 ] && break
    if [ "$SECONDS" -ge "$limite" ]; then log "FALHA: postgres/clickhouse não ficaram healthy em ${ESPERA_MAX_S}s; nada apagado"; exit 2; fi
    sleep 10
  done
fi

# Backup.
if ! BACKUP_DIR="$BACKUP_ROOT" "$BACKUP_SCRIPT" > "$SAIDA" 2>&1; then
  log "FALHA: backup.sh retornou erro (últimas linhas abaixo); nada apagado"
  tail -n 15 "$SAIDA" | sed 's/^/    /' | tee -a "$LOG" >/dev/null
  exit 1
fi

# Verificação de integridade do backup recém-gerado.
if ! ( cd "$DEST" && sha256sum -c SHA256SUMS > "$SAIDA.sha" 2>&1 ); then
  log "FALHA: sha256sum -c falhou em $DEST; nada apagado"
  tail -n 10 "$SAIDA.sha" | sed 's/^/    /' | tee -a "$LOG" >/dev/null
  exit 1
fi
tamanho="$(du -sh "$DEST" | cut -f1)"
nfiles="$(wc -l < "$DEST/SHA256SUMS")"
log "OK backup $DEST ($tamanho, $nfiles arquivos, sha256 verificado)"

# Retenção (só chega aqui com o backup de hoje verificado).
corte="$(date -d "$RETENCAO_DIAS days ago" +%Y%m%d)"
mais_recente="$(find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d -regextype posix-extended -regex '.*/zbx8lab-[0-9]{8}' -printf '%f\n' | sort | tail -n1)"
removidos=0
while IFS= read -r nome; do
  [[ "$nome" =~ ^zbx8lab-([0-9]{8})$ ]] || continue
  data="${BASH_REMATCH[1]}"
  [ "$data" -lt "$corte" ] || continue
  [ "$nome" != "zbx8lab-$HOJE" ] && [ "$nome" != "$mais_recente" ] || continue
  alvo="$BACKUP_ROOT/$nome"
  [ -d "$alvo" ] && [ ! -L "$alvo" ] || continue
  rm -r -- "$alvo"
  log "RETENCAO: removido $nome (anterior a $corte)"
  removidos=$((removidos + 1))
done < <(find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d -regextype posix-extended -regex '.*/zbx8lab-[0-9]{8}' -printf '%f\n' | sort)
log "FIM ok (retenção: $removidos removido(s), corte $corte)"

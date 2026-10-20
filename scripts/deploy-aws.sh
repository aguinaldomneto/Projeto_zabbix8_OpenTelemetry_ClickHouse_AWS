#!/usr/bin/env bash
# Sobe o laboratório na VM da AWS com um comando, a partir do seu computador.
#
# Descobre sozinho o que antes vinha do "terraform output" do projeto
# agentless-fleet-audit: acha a VM pela tag Name, pega o IP público atual,
# escolhe a chave SSH que funciona, copia esta pasta e roda tudo lá dentro.
#
# Uso:  ./scripts/deploy-aws.sh [comando]
#
#   deploy       (padrão) copia o laboratório e sobe tudo na VM
#   status       mostra a VM, o IP, a regra de SSH e se o acesso funciona
#   ip           mostra só o IP público da VM (sem formatação, para scripts)
#   liberar-ip   libera as portas 22 (SSH), OTLP e do trapper para o seu IP atual
#   ligar        liga a VM (o IP público muda)
#   parar        para a VM, para não gastar crédito (os dados ficam no disco)
#   ssh          abre um terminal na VM
#   tunel        abre o túnel do frontend (http://localhost:8080)
#   senha        mostra a senha do usuário Admin do Zabbix
#   verificar    roda o "make verificar" na VM
#   diagnostico  roda o "make diagnostico" na VM e traz o arquivo para cá
#   digests      roda o "make digests" na VM (digest das imagens em uso)
#
# Ajustes opcionais: variáveis de ambiente ou um arquivo deploy.conf nesta
# pasta (fica fora do Git), com linhas CHAVE=valor:
#
#   AWS_PROFILE=fleet-audit      perfil do AWS CLI
#   AWS_REGION=us-east-1         região da VM
#   VM_TAG=fleet-audit-lab       valor da tag Name da VM
#   VM_IP=1.2.3.4                pula a busca na AWS e usa este IP
#   SSH_USER=ubuntu
#   SSH_KEY=~/.ssh/oci_lab       chave privada
#   PORTA_LOCAL=8080             porta do túnel no seu computador
set -euo pipefail

cd "$(dirname "$0")/.." || exit 1
RAIZ=$PWD

# shellcheck disable=SC1091
[ -f deploy.conf ] && . ./deploy.conf

TFVARS_ANTIGO="$HOME/agentless-fleet-audit/infra/aws/terraform.tfvars"
ler_tfvars() { # valor de uma chave não comentada do terraform.tfvars antigo
  [ -f "$TFVARS_ANTIGO" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\(.*\)\".*/\1/p" "$TFVARS_ANTIGO" | tail -1
}

AWS_PROFILE=${AWS_PROFILE:-$(ler_tfvars aws_profile)}
AWS_PROFILE=${AWS_PROFILE:-fleet-audit}
AWS_REGION=${AWS_REGION:-$(ler_tfvars region)}
AWS_REGION=${AWS_REGION:-us-east-1}
VM_TAG=${VM_TAG:-fleet-audit-lab}
VM_IP=${VM_IP:-}
SSH_USER=${SSH_USER:-ubuntu}
SSH_KEY=${SSH_KEY:-}
PORTA_LOCAL=${PORTA_LOCAL:-8080}
DIR_REMOTO=zabbix8-telemetria-lab
# Portas liberadas por "liberar-ip" além da 22: OTLP (APIs fictícias do WSL
# local) e o trapper (os 10 hosts simulados do WSL local). Veja o README.
OTLP_PORT=${OTLP_PORT:-4317}
ZBX_TRAPPER_PORT=${ZBX_TRAPPER_PORT:-10051}

VM_ID=""; VM_ESTADO=""; VM_TIPO=""; VM_SG=""

erro()  { printf '\n[erro] %s\n' "$*" >&2; exit 1; }
passo() { printf '\n== %s\n' "$*"; }
info()  { printf '   %s\n' "$*"; }

aws_ec2() { aws ec2 "$@" --profile "$AWS_PROFILE" --region "$AWS_REGION"; }

exigir_aws() {
  command -v aws >/dev/null 2>&1 || erro "AWS CLI não encontrado neste terminal.
       Instale (https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)
       ou informe o IP da VM direto:  VM_IP=1.2.3.4 ./scripts/deploy-aws.sh"
  aws sts get-caller-identity --profile "$AWS_PROFILE" >/dev/null 2>&1 || erro "o perfil '$AWS_PROFILE' do AWS CLI não funcionou.
       Confira com:  aws sts get-caller-identity --profile $AWS_PROFILE
       Para configurar de novo:  aws configure --profile $AWS_PROFILE"
}

# Preenche VM_ID, VM_ESTADO, VM_IP, VM_TIPO e VM_SG a partir da tag Name.
achar_vm() {
  if [ "${IP_INFORMADO:-}" = 1 ]; then
    VM_ESTADO=running
    return 0
  fi

  exigir_aws
  local linhas
  linhas=$(aws_ec2 describe-instances \
      --filters "Name=tag:Name,Values=$VM_TAG" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
      --query 'Reservations[].Instances[].[InstanceId,State.Name,PublicIpAddress,InstanceType,SecurityGroups[0].GroupId]' \
      --output text) || erro "a consulta à AWS falhou (perfil $AWS_PROFILE, região $AWS_REGION)."

  if [ -z "$linhas" ]; then
    erro "nenhuma VM com a tag Name=$VM_TAG na região $AWS_REGION.
       Liste o que existe:  aws ec2 describe-instances --profile $AWS_PROFILE --region $AWS_REGION \\
         --query 'Reservations[].Instances[].[InstanceId,State.Name,Tags[?Key==\`Name\`]|[0].Value]' --output table
       Se a VM está em outra região ou tem outro nome, use AWS_REGION=... ou VM_TAG=..."
  fi

  local n
  n=$(printf '%s\n' "$linhas" | wc -l)
  [ "$n" -gt 1 ] && info "aviso: $n VMs com a tag $VM_TAG; usando a que está ligada (ou a primeira)."

  # Prefere a que está ligada
  local escolhida
  escolhida=$(printf '%s\n' "$linhas" | awk '$2 == "running"' | head -1)
  [ -n "$escolhida" ] || escolhida=$(printf '%s\n' "$linhas" | head -1)

  read -r VM_ID VM_ESTADO VM_IP VM_TIPO VM_SG <<<"$escolhida"
  [ "$VM_IP" = "None" ] && VM_IP=""
  return 0
}

meu_ip() { curl -fsS -m 10 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]'; }

regras_porta() { # regras_porta <porta> - CIDRs liberados nessa porta do SG da VM
  local porta="$1"
  aws_ec2 describe-security-groups --group-ids "$VM_SG" \
    --query "SecurityGroups[0].IpPermissions[?FromPort==\`$porta\` && ToPort==\`$porta\`].IpRanges[].CidrIp" --output text 2>/dev/null \
    | tr '\t' '\n' | sed '/^$/d'
}

regras_ssh() { regras_porta 22; } # mantido pelo nome: usado em explicar_falha_ssh/cmd_status

opcoes_ssh() {
  printf '%s\n' -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
                -o IdentitiesOnly=yes -o ServerAliveInterval=30 -i "$SSH_KEY"
}

rodar_ssh() { # rodar_ssh [opções extras do ssh] -- comando remoto
  local extras=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do extras+=("$1"); shift; done
  [ "${1:-}" = "--" ] && shift
  local base=()
  mapfile -t base < <(opcoes_ssh)
  ssh "${base[@]}" "${extras[@]}" "$SSH_USER@$VM_IP" "$@"
}

# Escolhe a chave que autentica. Devolve 0 se o SSH funciona.
SSH_FALHA=""
escolher_chave() {
  local candidatas=() chave saida publica
  [ -n "$SSH_KEY" ] && candidatas+=("${SSH_KEY/#\~/$HOME}")
  publica=$(ler_tfvars ssh_public_key_path)
  [ -n "$publica" ] && candidatas+=("$(printf '%s' "${publica/#\~/$HOME}" | sed 's/\.pub$//')")
  candidatas+=("$HOME/.ssh/oci_lab" "$HOME/.ssh/id_ed25519" "$HOME/.ssh/id_rsa")

  SSH_FALHA="nenhuma chave privada encontrada (procurei em: ${candidatas[*]})"
  for chave in "${candidatas[@]}"; do
    [ -f "$chave" ] || continue
    SSH_KEY=$chave
    if saida=$(rodar_ssh -n -- true 2>&1); then
      SSH_FALHA=""
      return 0
    fi
    case "$saida" in
      *"Permission denied"*) SSH_FALHA="chave recusada" ;;                 # tenta a próxima
      *)                     SSH_FALHA="sem conexão: $(printf '%s' "$saida" | tail -1)"; return 1 ;;
    esac
  done
  return 1
}

explicar_falha_ssh() {
  printf '\n[erro] não consegui entrar por SSH em %s@%s (%s).\n' "$SSH_USER" "$VM_IP" "$SSH_FALHA" >&2
  case "$SSH_FALHA" in
    "chave recusada"|nenhuma*)
      cat >&2 <<EOF
       A VM respondeu, mas nenhuma das chaves testadas é a dela.
       A chave certa é a privada do par informado em ssh_public_key_path quando a VM
       foi criada (padrão do projeto: ~/.ssh/oci_lab). Se ela está em outro lugar:
         SSH_KEY=/caminho/da/chave ./scripts/deploy-aws.sh
EOF
      ;;
    *)
      if [ -n "$VM_SG" ]; then
        local ip regras
        ip=$(meu_ip); regras=$(regras_ssh | tr '\n' ' ')
        printf '       Seu IP público agora: %s\n       IPs liberados na porta 22 da VM: %s\n' "${ip:-não consegui descobrir}" "${regras:-nenhum}" >&2
        if [ -n "$ip" ] && ! printf '%s' "$regras" | grep -qF "$ip/32"; then
          cat >&2 <<EOF
       Seu IP mudou desde que a VM foi criada, e o security group só aceita o antigo.
       Para trocar a regra pelo IP atual:
         ./scripts/deploy-aws.sh liberar-ip
EOF
        else
          echo "       A regra de SSH já aponta para o seu IP. A VM pode ter acabado de ligar; tente de novo em 1 minuto." >&2
        fi
      else
        echo "       Confira se a VM está ligada e se o security group libera a porta 22 para o seu IP." >&2
      fi
      ;;
  esac
  exit 1
}

preparar_acesso() { # acha a VM, confere que está ligada e que o SSH funciona
  achar_vm
  case "$VM_ESTADO" in
    running) ;;
    stopped) erro "a VM $VM_ID está parada. Ligue com:  ./scripts/deploy-aws.sh ligar" ;;
    *)       erro "a VM $VM_ID está no estado '$VM_ESTADO'. Espere um minuto e tente de novo." ;;
  esac
  [ -n "$VM_IP" ] || erro "a VM $VM_ID está ligada, mas sem IP público."
  escolher_chave || explicar_falha_ssh
}

cmd_status() {
  achar_vm
  passo "VM"
  info "id:      ${VM_ID:-não consultado (IP informado à mão)}"
  info "estado:  $VM_ESTADO"
  info "tipo:    ${VM_TIPO:-?}"
  info "IP:      ${VM_IP:-sem IP (VM parada)}"
  if [ -n "$VM_SG" ]; then
    passo "Regra de SSH (security group $VM_SG)"
    info "liberado na porta 22: $(regras_ssh | tr '\n' ' ')"
    info "seu IP público agora: $(meu_ip)"
  fi
  [ "$VM_ESTADO" = running ] && [ -n "$VM_IP" ] || { echo; info "VM parada: nada para testar por SSH."; return 0; }
  passo "SSH"
  if escolher_chave; then
    info "ok com a chave $SSH_KEY"
    info "comando: ssh -i $SSH_KEY $SSH_USER@$VM_IP"
    rodar_ssh -n -- "free -m | awk 'NR==2 {printf \"   memória da VM: %s MB no total, %s MB livres\\n\", \$2, \$7}'; \
                  if [ -d $DIR_REMOTO ]; then echo '   laboratório já copiado para ~/$DIR_REMOTO'; else echo '   laboratório ainda não copiado'; fi" || true
  else
    info "falhou: $SSH_FALHA"
    info "rode './scripts/deploy-aws.sh' para ver o diagnóstico completo"
  fi
}

cmd_liberar_ip() {
  [ "${IP_INFORMADO:-}" = 1 ] && erro "com VM_IP informado à mão não sei qual é o security group. Rode sem VM_IP."
  achar_vm
  [ -n "$VM_SG" ] || erro "não achei o security group da VM."
  local ip porta antigas cidr
  ip=$(meu_ip)
  [ -n "$ip" ] || erro "não consegui descobrir seu IP público (checkip.amazonaws.com)."

  # Só a 22 (SSH). O OTLP e o trapper das APIs/hosts do WSL local viajam
  # dentro de um túnel SSH (serviço "tunel" do compose.local.yaml) e ficam
  # presos em 127.0.0.1 na VM: nunca abra essas portas na internet.
  for porta in 22; do
    passo "Security group $VM_SG, porta $porta"
    antigas=$(regras_porta "$porta")
    if printf '%s\n' "$antigas" | grep -qxF "$ip/32"; then
      info "$ip/32 já está liberado."
    else
      aws_ec2 authorize-security-group-ingress --group-id "$VM_SG" --protocol tcp --port "$porta" --cidr "$ip/32" >/dev/null
      info "liberado: $ip/32"
    fi
    # Não destrutivo por padrão: regras de outros IPs na porta ficam como estão.
    # Para revogar os IPs antigos, rode com REVOGAR_ANTIGAS=1 (pede confirmação por regra).
    for cidr in $antigas; do
      [ "$cidr" = "$ip/32" ] && continue
      if [ "${REVOGAR_ANTIGAS:-0}" != 1 ]; then
        info "mantido: $cidr (outro IP; use REVOGAR_ANTIGAS=1 para remover)"
        continue
      fi
      local resp=""
      read -r -p "   remover $cidr da porta $porta? [s/N] " resp || resp=""
      [ "$resp" = s ] || [ "$resp" = S ] || { info "mantido: $cidr"; continue; }
      aws_ec2 revoke-security-group-ingress --group-id "$VM_SG" --protocol tcp --port "$porta" --cidr "$cidr" >/dev/null
      info "removido: $cidr (IP antigo)"
    done
  done
}

cmd_ip() { # IP público atual da VM, sem formatação (para scripts, ex. subir-local.sh)
  achar_vm
  [ -n "$VM_IP" ] || erro "a VM $VM_ID está sem IP público (parada?)."
  printf '%s\n' "$VM_IP"
}

cmd_chave() { # caminho da chave SSH que funciona com a VM (para scripts, ex. subir-local.sh)
  preparar_acesso
  printf '%s\n' "$SSH_KEY"
}

cmd_ligar() {
  [ "${IP_INFORMADO:-}" = 1 ] && erro "com VM_IP informado à mão não sei qual VM ligar. Rode sem VM_IP."
  achar_vm
  if [ "$VM_ESTADO" = running ]; then info "a VM $VM_ID já está ligada em $VM_IP"; return 0; fi
  passo "Ligando $VM_ID"
  aws_ec2 start-instances --instance-ids "$VM_ID" >/dev/null
  aws_ec2 wait instance-running --instance-ids "$VM_ID"
  VM_IP=""; achar_vm
  info "ligada. IP novo: $VM_IP"
  info "os containers com restart automático voltam sozinhos em 1 a 2 minutos."
}

cmd_parar() {
  [ "${IP_INFORMADO:-}" = 1 ] && erro "com VM_IP informado à mão não sei qual VM parar. Rode sem VM_IP."
  achar_vm
  if [ "$VM_ESTADO" = stopped ]; then info "a VM $VM_ID já está parada."; return 0; fi
  passo "Parando $VM_ID"
  aws_ec2 stop-instances --instance-ids "$VM_ID" >/dev/null
  aws_ec2 wait instance-stopped --instance-ids "$VM_ID"
  info "parada. O disco continua cobrando; a CPU não. O IP público muda ao religar."
}

cmd_deploy() {
  passo "Procurando a VM"
  preparar_acesso
  info "VM ${VM_ID:-(IP informado)} em $VM_IP, chave $SSH_KEY"

  passo "Copiando o laboratório para ~/$DIR_REMOTO"
  # O .env não é copiado: as senhas são geradas dentro da VM e ficam só lá.
  tar -C "$RAIZ" --exclude=./.env --exclude=./deploy.conf --exclude='./diagnostico-*' --exclude=./.git -czf - . \
    | rodar_ssh -- "mkdir -p $DIR_REMOTO && tar -xzf - -C $DIR_REMOTO"
  info "copiado."

  passo "Subindo na VM (a primeira vez leva de 5 a 10 minutos)"
  # -n: o ssh não lê o teclado, para nenhum comando remoto consumir a entrada.
  rodar_ssh -n -- "cd $DIR_REMOTO && ./scripts/subir-tudo.sh" \
    || erro "a subida não terminou bem. Para juntar os logs: ./scripts/deploy-aws.sh diagnostico"

  passo "Pronto"
  info "Abra o túnel em outro terminal:  ./scripts/deploy-aws.sh tunel"
  info "Depois acesse:                   http://localhost:$PORTA_LOCAL  (usuário Admin)"
  info "Senha do Admin:                  ./scripts/deploy-aws.sh senha"
  info "Quando não estiver usando:       ./scripts/deploy-aws.sh parar"
}

cmd_ssh() {
  preparar_acesso
  exec ssh -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -i "$SSH_KEY" "$SSH_USER@$VM_IP"
}

cmd_tunel() {
  preparar_acesso
  local porta_vm
  porta_vm=$(rodar_ssh -n -- "grep -E '^ZBX_WEB_PORT=' $DIR_REMOTO/.env 2>/dev/null | cut -d= -f2" || true)
  porta_vm=${porta_vm:-8080}
  echo "Túnel aberto: http://localhost:$PORTA_LOCAL  (Ctrl+C fecha)"
  exec ssh -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
    -i "$SSH_KEY" -N -L "$PORTA_LOCAL:127.0.0.1:$porta_vm" "$SSH_USER@$VM_IP"
}

cmd_senha() {
  preparar_acesso
  rodar_ssh -n -- "grep -E '^ZBX_ADMIN_PASSWORD=' $DIR_REMOTO/.env | cut -d= -f2" \
    || erro "não achei o .env na VM (o deploy já rodou?)."
}

cmd_verificar() { preparar_acesso; rodar_ssh -n -- "cd $DIR_REMOTO && make verificar"; }

cmd_digests() { preparar_acesso; rodar_ssh -n -- "cd $DIR_REMOTO && make digests"; }

cmd_diagnostico() {
  preparar_acesso
  rodar_ssh -n -- "cd $DIR_REMOTO && make diagnostico" || true
  local arquivo
  arquivo=$(rodar_ssh -n -- "ls -t $DIR_REMOTO/diagnostico-*.txt 2>/dev/null | head -1")
  [ -n "$arquivo" ] || erro "a VM não gerou o arquivo de diagnóstico."
  rodar_ssh -n -- "cat $arquivo" > "$RAIZ/$(basename "$arquivo")"
  echo; echo "Arquivo trazido para: $RAIZ/$(basename "$arquivo")"
}

[ -n "$VM_IP" ] && IP_INFORMADO=1

case "${1:-deploy}" in
  deploy)      cmd_deploy ;;
  status)      cmd_status ;;
  liberar-ip)  cmd_liberar_ip ;;
  ip)          cmd_ip ;;
  chave)       cmd_chave ;;
  ligar)       cmd_ligar ;;
  parar)       cmd_parar ;;
  ssh)         cmd_ssh ;;
  tunel)       cmd_tunel ;;
  senha)       cmd_senha ;;
  verificar)   cmd_verificar ;;
  diagnostico) cmd_diagnostico ;;
  digests)     cmd_digests ;;
  -h|--help|ajuda) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) erro "comando desconhecido: $1 (veja ./scripts/deploy-aws.sh ajuda)" ;;
esac

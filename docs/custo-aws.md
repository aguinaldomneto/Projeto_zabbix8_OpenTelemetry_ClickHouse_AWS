# Controle de custo da VM AWS do laboratório

Este documento é só recomendação. Nenhum comando abaixo foi executado; todos estão
comentados para você revisar (troque `<...>` pelos seus valores e use o seu `--profile`).

## O que cobra e quando

| Recurso | VM ligada | VM parada |
|---|---|---|
| Instância EC2 (computação) | cobra por hora | não cobra |
| Volume EBS | cobra | **continua cobrando** |
| IP público (IPv4) | cobra | **continua cobrando** (IPv4 público é cobrado por hora, em uso ou Elastic IP) |
| Snapshots EBS | cobra por GB armazenado | cobra |
| Tráfego de saída para a internet | cobra | não se aplica |

Parar a VM não zera a conta: EBS e IP seguem cobrando. Só terminar a instância e apagar
volume/IP elimina o custo, e isso apaga os dados (faça snapshot antes).

## 1. AWS Budget com alerta por e-mail

```bash
# aws budgets create-budget --account-id <ID_DA_CONTA> \
#   --budget '{"BudgetName":"lab-zabbix","BudgetLimit":{"Amount":"20","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST"}' \
#   --notifications-with-subscribers '[{"Notification":{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":80,"ThresholdType":"PERCENTAGE"},"Subscribers":[{"SubscriptionType":"EMAIL","Address":"<seu-email>"}]}]'
```

Use também um segundo alerta do tipo `FORECASTED` a 100% para ser avisado antes de estourar.

## 2. Parada agendada

Opção A: cron na sua máquina (simples, depende de ela estar ligada):

```bash
# 0 22 * * *  cd ~/zabbix8-telemetria-lab && ./scripts/deploy-aws.sh parar
# 0 8  * * 1-5 cd ~/zabbix8-telemetria-lab && ./scripts/deploy-aws.sh ligar
```

Opção B: EventBridge Scheduler chamando a API do EC2 (não depende da sua máquina):

```bash
# 1) Crie uma role para o Scheduler com permissão ec2:StopInstances / ec2:StartInstances
#    (política mínima restrita ao ARN da instância).
# 2) Agende a parada diária às 22h (horário de Brasília = 01:00 UTC):
# aws scheduler create-schedule --name lab-zabbix-parar \
#   --schedule-expression "cron(0 22 * * ? *)" \
#   --schedule-expression-timezone "America/Sao_Paulo" \
#   --flexible-time-window '{"Mode":"OFF"}' \
#   --target '{"Arn":"arn:aws:scheduler:::aws-sdk:ec2:stopInstances","RoleArn":"<ARN_DA_ROLE>","Input":"{\"InstanceIds\":[\"<i-xxxxxxxx>\"]}"}'
```

Lembre: como o IP público muda ao religar (sem Elastic IP), rode `liberar-ip` e atualize
o túnel depois de cada `ligar`.

## 3. Snapshots do EBS (backup da VM)

Manual, antes de mudanças grandes:

```bash
# aws ec2 create-snapshot --volume-id <vol-xxxxxxxx> --description "lab-zabbix $(date +%F)" \
#   --tag-specifications 'ResourceType=snapshot,Tags=[{Key=projeto,Value=lab-zabbix}]'
```

Automático com Data Lifecycle Manager (1 por dia, mantém 7):

```bash
# aws dlm create-lifecycle-policy --description "lab-zabbix diario" --state ENABLED \
#   --execution-role-arn <ARN_ROLE_DLM> \
#   --policy-details '{"ResourceTypes":["VOLUME"],"TargetTags":[{"Key":"projeto","Value":"lab-zabbix"}],"Schedules":[{"Name":"diario","CreateRule":{"Interval":24,"IntervalUnit":"HOURS","Times":["03:00"]},"RetainRule":{"Count":7},"CopyTags":true}]}'
```

Snapshots são incrementais, mas 7 retidos ainda custam; reduza `Count` se o laboratório for descartável.
Além do snapshot, o `scripts/backup.sh` gera um backup lógico (Postgres + ClickHouse) que
pode ser copiado para fora da VM.

## 4. Higiene geral

- Etiquete tudo com `projeto=lab-zabbix` e filtre no Cost Explorer por essa tag.
- Ative a tag como "cost allocation tag" no Billing para ela aparecer nos relatórios.
- Se não for usar por semanas: snapshot, termine a instância, libere o IP e apague o volume.
- Confira o Cost Explorer semanalmente (custo por serviço, filtrando EC2-Other: é onde aparecem EBS e IPv4).
- Evite Elastic IP solto: ele cobra mesmo sem estar associado a uma instância ligada.

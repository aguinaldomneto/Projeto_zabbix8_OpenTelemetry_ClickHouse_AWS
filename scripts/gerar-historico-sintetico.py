#!/usr/bin/env python3
"""Backfill de histórico SINTÉTICO no ClickHouse (tabela zabbix.otel_traces).

Por quê: a comparação "mesma janela de ontem" (D-1) do investigador
(`ferramentas/investigador/evidencias.py`, consulta "resumo_spans" com
ini/fim deslocados em -86400s) só encontra dados se o serviço já tinha
spans Server 24h antes do evento. O laboratório roda há poucas horas, então
os serviços fictícios (pedidos, pagamentos, catalogo, streaming,
autenticacao) não têm esse passado — e o relatório mostra "comparação D-1:
sem dados na mesma janela de ontem".

O que este script faz: insere spans Server sintéticos em
zabbix.otel_traces, com o MESMO esquema e os MESMOS nomes de serviço/span
usados pela API fictícia real (`ferramentas/api_ficticia.py`), cobrindo
--dias dias (padrão 3: D-1, D-2, D-3) IMEDIATAMENTE ANTES do primeiro
timestamp real de cada serviço (não sobrepõe dados reais). Os status HTTP
e a latência seguem a mesma distribuição da API fictícia (lognormal,
~2% 500, ~3% 404).

Importante - o que NÃO é coberto:
  * Só a tabela otel_traces é preenchida. O investigador só faz comparação
    D-1 nessa tabela (função "resumo_spans"); as demais evidências usam
    somente a janela atual, então otel_logs/otel_metrics_* não precisam de
    histórico para o relatório funcionar.
  * Os triggers `baselinedev(...,"d",7)` dos templates
    (config/zabbix-templates/api-ficticia.yaml, host-simulado.yaml) pedem
    7 dias de HISTÓRICO DO PRÓPRIO ZABBIX (itens apm.erros5xx / lab.latencia),
    não do ClickHouse. Isso não é coberto aqui: exigiria inserir direto no
    banco do zabbix-server, o que é bem mais arriscado e está fora do escopo
    desta tarefa (ver README, seção "Dados sintéticos de baseline").

Identificação dos dados sintéticos: todo span inserido ganha o atributo
extra ResourceAttributes['zbx8lab.origem'] = 'backfill-sintetico'. Isso NÃO
aparece nas colunas que o investigador filtra (ServiceName, SpanKind,
Timestamp), então não interfere nas consultas; serve só para marcar os
dados como fictícios e permitir limpeza seletiva.

Idempotência: antes de inserir cada bloco (serviço x dia), o script checa
se já existem linhas sintéticas nesse intervalo; se existirem, pula (ou
regenera com --forcar). Rodar de novo sem --forcar NÃO duplica.

Uso (dentro do WSL, na raiz do repo; precisa do stack no ar - clickhouse):
  python3 scripts/gerar-historico-sintetico.py                  # padrão: 3 dias, 5 serviços
  python3 scripts/gerar-historico-sintetico.py --dias 2
  python3 scripts/gerar-historico-sintetico.py --servicos pedidos
  python3 scripts/gerar-historico-sintetico.py --forcar         # apaga e regenera os blocos já existentes
  python3 scripts/gerar-historico-sintetico.py --limpar         # remove TODOS os dados sintéticos e sai

A senha do ClickHouse nunca passa por argv: vem de $CLICKHOUSE_PASSWORD
dentro do próprio container (igual a scripts/backup.sh).
"""

import argparse
import json
import random
import subprocess
import sys
import time
from datetime import datetime, timezone

SERVICOS_PADRAO = ["pedidos", "pagamentos", "catalogo", "streaming", "autenticacao"]

SPAN_POR_SERVICO = {
    "pedidos": "GET /comprar",
    "pagamentos": "GET /status",
    "catalogo": "GET /listar",
    "streaming": "GET /assistir",
    "autenticacao": "GET /login",
}

MARCADOR = "backfill-sintetico"

# Mesma distribuição de ferramentas/api_ficticia.py (defaults API_TAXA_404/500,
# API_LATENCIA_MEDIANA_MS/SIGMA), para o baseline sintético parecer com o
# tráfego real que a API fictícia gera em tempo real.
TAXA_404 = 0.03
TAXA_500 = 0.02
LATENCIA_MEDIANA_MS = 60.0
LATENCIA_SIGMA = 0.7


def executar(args_extra, entrada=None):
    """Chama clickhouse-client dentro do container 'clickhouse' (docker compose exec -T).

    Mesmo truque de scripts/backup.sh: o script inline do 'sh -c' lê a senha
    de $CLICKHOUSE_PASSWORD (env do próprio container) e repassa "$@"; assim
    nada sensível aparece em argv nem neste processo Python.
    """
    cmd = [
        "docker", "compose", "exec", "-T", "clickhouse", "sh", "-c",
        'clickhouse-client --user zabbix --password "$CLICKHOUSE_PASSWORD" "$@"',
        "sh",
    ] + args_extra
    r = subprocess.run(cmd, input=entrada, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("clickhouse-client falhou: " + r.stderr.decode(errors="replace")[:2000])
    return r.stdout.decode()


def consultar(sql):
    return executar(["--query", sql])


def cutoff_global(servicos):
    """Menor Timestamp REAL (não sintético) entre os serviços; agora() se nenhum tiver dado ainda."""
    lista = ",".join("'" + s.replace("'", "") + "'" for s in servicos)
    saida = consultar(
        "SELECT toUnixTimestamp(min(Timestamp)) FROM zabbix.otel_traces "
        "WHERE ServiceName IN (" + lista + ") AND NOT mapContains(ResourceAttributes,'zbx8lab.origem')"
    ).strip()
    try:
        v = int(saida)
    except ValueError:
        v = 0
    return v if v > 0 else int(time.time())


def contar_sintetico(servico, ini, fim):
    saida = consultar(
        "SELECT count() FROM zabbix.otel_traces WHERE ServiceName='%s' "
        "AND toUnixTimestamp(Timestamp) >= %d AND toUnixTimestamp(Timestamp) < %d "
        "AND mapContains(ResourceAttributes,'zbx8lab.origem')" % (servico, ini, fim)
    ).strip()
    return int(saida or 0)


def apagar_sintetico(servico, ini, fim):
    consultar(
        "ALTER TABLE zabbix.otel_traces DELETE WHERE ServiceName='%s' "
        "AND toUnixTimestamp(Timestamp) >= %d AND toUnixTimestamp(Timestamp) < %d "
        "AND mapContains(ResourceAttributes,'zbx8lab.origem') SETTINGS mutations_sync=1" % (servico, ini, fim)
    )


def sortear_status():
    r = random.random()
    if r < TAXA_500:
        return 500
    if r < TAXA_500 + TAXA_404:
        return 404
    return 200


def sortear_duracao_ns():
    mediana_s = LATENCIA_MEDIANA_MS / 1000
    atraso = min(random.lognormvariate(0, LATENCIA_SIGMA) * mediana_s, 5.0)
    return int(atraso * 1e9)


def montar_linha(servico, epoch, instance_id):
    status = sortear_status()
    ts = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    caminho = SPAN_POR_SERVICO[servico].split(" ", 1)[1]

    span_attrs = {
        "http.request.method": "GET",
        "url.path": caminho,
        "http.response.status_code": str(status),
        "http.response.body.size": str(random.randint(200, 65536)),
    }
    if servico == "pedidos" and status == 200:
        if random.random() < 0.15:
            span_attrs["negocio.evento"] = "carrinho_abandonado"
        else:
            span_attrs["negocio.evento"] = "compra_concluida"
            span_attrs["negocio.valor"] = "%.6f" % round(random.lognormvariate(5.0, 0.6), 2)
    if servico == "streaming":
        span_attrs["negocio.buffering_s"] = str(round(max(0.0, random.lognormvariate(-1.5, 1.0)), 3))

    return {
        "Timestamp": ts,
        "TraceId": "%032x" % random.getrandbits(128),
        "SpanId": "%016x" % random.getrandbits(64),
        "ParentSpanId": "",
        "TraceState": "",
        "SpanName": SPAN_POR_SERVICO[servico],
        "SpanKind": "Server",
        "ServiceName": servico,
        "ResourceAttributes": {
            "telemetry.sdk.language": "python",
            "telemetry.sdk.name": "opentelemetry",
            "telemetry.sdk.version": "1.45.1",
            "service.instance.id": instance_id,
            "service.name": servico,
            "service.version": "0.1.0",
            "deployment.environment.name": "lab",
            "zbx8lab.origem": MARCADOR,
        },
        "ScopeName": servico,
        "ScopeVersion": "0.1.0",
        "SpanAttributes": span_attrs,
        "Duration": sortear_duracao_ns(),
        "StatusCode": "Error" if status >= 500 else "Unset",
        "StatusMessage": "erro simulado" if status >= 500 else "",
    }


def gerar_bloco(servico, ini, fim, intervalo_s, instance_id):
    linhas = []
    t = float(ini)
    while t < fim:
        linhas.append(json.dumps(montar_linha(servico, t, instance_id), ensure_ascii=False))
        t += max(1.0, random.uniform(intervalo_s * 0.6, intervalo_s * 1.4))
    return linhas


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dias", type=int, default=3, help="quantos dias de histórico gerar antes do corte (padrão: 3)")
    ap.add_argument("--servicos", default=",".join(SERVICOS_PADRAO), help="lista separada por vírgula")
    ap.add_argument("--intervalo-s", type=float, default=10.0, dest="intervalo",
                     help="intervalo médio entre spans sintéticos, em segundos (padrão: 10)")
    ap.add_argument("--forcar", action="store_true", help="apaga e regenera blocos que já existem")
    ap.add_argument("--limpar", action="store_true", help="remove TODOS os dados sintéticos dos serviços e sai")
    ns = ap.parse_args()

    servicos = [s.strip() for s in ns.servicos.split(",") if s.strip()]
    if not servicos:
        sys.exit("nenhum serviço informado")
    desconhecidos = [s for s in servicos if s not in SPAN_POR_SERVICO]
    if desconhecidos:
        sys.exit("serviço(s) sem span de referência cadastrado neste script: %s" % ", ".join(desconhecidos))

    if ns.limpar:
        for s in servicos:
            print("[limpar] %s: removendo todos os dados sintéticos" % s)
            apagar_sintetico(s, 0, int(time.time()) + 86400)
        print("Pronto. Os dados reais não foram tocados.")
        return

    corte = cutoff_global(servicos)
    print("[info] corte (início dos dados reais dos serviços pedidos): %s"
          % datetime.fromtimestamp(corte, tz=timezone.utc).isoformat())

    total_linhas = 0
    for s in servicos:
        instance_id = "sintetico-backfill-%08x" % random.getrandbits(32)
        for d in range(1, ns.dias + 1):
            fim = corte - (d - 1) * 86400
            ini = corte - d * 86400
            rotulo = "D-%d" % d
            existentes = contar_sintetico(s, ini, fim)
            if existentes > 0:
                if not ns.forcar:
                    print("[pular] %s %s: já existem %d linhas sintéticas (use --forcar para regenerar)"
                          % (s, rotulo, existentes))
                    continue
                print("[forcar] %s %s: removendo %d linhas antigas antes de regenerar" % (s, rotulo, existentes))
                apagar_sintetico(s, ini, fim)

            linhas = gerar_bloco(s, ini, fim, ns.intervalo, instance_id)
            if not linhas:
                continue
            entrada = ("\n".join(linhas) + "\n").encode()
            executar(["--query", "INSERT INTO zabbix.otel_traces FORMAT JSONEachRow"], entrada=entrada)
            total_linhas += len(linhas)
            dia = datetime.fromtimestamp(ini, tz=timezone.utc).date()
            print("[ok] %s %s (%s): %d linhas" % (s, rotulo, dia, len(linhas)))

    print("\nTotal inserido nesta execução: %d linha(s) em zabbix.otel_traces (serviços: %s)"
          % (total_linhas, ", ".join(servicos)))
    print("Para limpar depois: python3 scripts/gerar-historico-sintetico.py --limpar"
          " (ou espere o TTL de 7 dias da tabela apagar tudo sozinho).")


if __name__ == "__main__":
    main()

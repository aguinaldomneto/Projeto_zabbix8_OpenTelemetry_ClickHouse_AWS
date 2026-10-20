"""Coleta de evidências SOMENTE LEITURA (ClickHouse + API do Zabbix).

Regras:
  * As consultas ao ClickHouse são CONSTANTES deste módulo (dicionário
    CONSULTAS). Nenhum texto vindo do usuário, do modelo ou dos logs é
    concatenado em SQL: tudo entra como parâmetro tipado do ClickHouse
    ({nome:Tipo} + param_nome na URL), validado antes.
  * O usuário do ClickHouse é dedicado e readonly (config/clickhouse/users.d),
    com max_execution_time/max_result_rows no profile; aqui ainda há timeout
    HTTP e LIMIT em todas as consultas.
  * Na API do Zabbix só são usados métodos .get (token com papel restrito).
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

SERVICO_VALIDO = re.compile(r"^[A-Za-z0-9_.:/-]{1,100}$")
EVENTID_VALIDO = re.compile(r"^\d{1,20}$")

_ERRO = "(StatusCode = 'Error' OR toUInt16OrZero(SpanAttributes['http.response.status_code']) >= 500)"
_JANELA_T = "ServiceName = {svc:String} AND Timestamp >= toDateTime({ini:UInt32}) AND Timestamp < toDateTime({fim:UInt32})"

CONSULTAS = {
    # Volume, erros e latência (p50/p95/p99 em ms) dos spans Server.
    "resumo_spans": (
        "SELECT count() AS spans, countIf(" + _ERRO + ") AS erros, "
        "round(quantile(0.5)(Duration) / 1e6, 1) AS p50_ms, "
        "round(quantile(0.95)(Duration) / 1e6, 1) AS p95_ms, "
        "round(quantile(0.99)(Duration) / 1e6, 1) AS p99_ms "
        "FROM zabbix.otel_traces WHERE " + _JANELA_T + " AND SpanKind = 'Server' LIMIT 1"
    ),
    # Distribuição de status HTTP.
    "status_http": (
        "SELECT SpanAttributes['http.response.status_code'] AS status, count() AS n "
        "FROM zabbix.otel_traces WHERE " + _JANELA_T + " AND SpanKind = 'Server' "
        "GROUP BY status ORDER BY n DESC LIMIT 10"
    ),
    # Spans com erro agrupados por nome (com 1 trace de exemplo, usado só
    # para montar link no relatório; o trace id é mascarado antes da IA).
    "top_spans_erro": (
        "SELECT SpanName AS span, count() AS n, any(substring(StatusMessage, 1, 200)) AS mensagem, "
        "any(TraceId) AS trace_exemplo "
        "FROM zabbix.otel_traces WHERE " + _JANELA_T + " AND " + _ERRO + " "
        "GROUP BY span ORDER BY n DESC LIMIT {lim:UInt16}"
    ),
    # Logs WARN ou piores agrupados por padrão (dígitos viram #).
    "logs_agrupados": (
        "SELECT SeverityText AS severidade, "
        "substring(replaceRegexpAll(Body, '[0-9]+', '#'), 1, 200) AS padrao, count() AS n, "
        "any(substring(Body, 1, 300)) AS exemplo, any(TraceId) AS trace_exemplo "
        "FROM zabbix.otel_logs WHERE " + _JANELA_T + " AND SeverityNumber >= 13 "
        "GROUP BY severidade, padrao ORDER BY n DESC LIMIT {lim:UInt16}"
    ),
    # Versões do serviço vistas na janela (deploy recente?).
    "versoes": (
        "SELECT ResourceAttributes['service.version'] AS versao, min(Timestamp) AS primeiro, count() AS n "
        "FROM zabbix.otel_traces WHERE " + _JANELA_T + " GROUP BY versao ORDER BY primeiro LIMIT 5"
    ),
    # Frescor da ingestão do serviço (sem dados = problema no coletor).
    "frescor": (
        "SELECT toUnixTimestamp(max(Timestamp)) AS ultimo FROM zabbix.otel_traces "
        "WHERE ServiceName = {svc:String} AND Timestamp >= now() - INTERVAL 1 DAY LIMIT 1"
    ),
    # Serviços com traces na última hora (para descobrir o serviço do evento).
    "servicos_ativos": (
        "SELECT DISTINCT toString(ServiceName) AS servico FROM zabbix.otel_traces "
        "WHERE Timestamp >= now() - INTERVAL 1 HOUR LIMIT 200"
    ),
}

TIPOS_PARAMETRO = {"svc": "String", "ini": "UInt32", "fim": "UInt32", "lim": "UInt16"}


class ErroColeta(Exception):
    pass


def validar_parametros(params):
    """Valida e converte os parâmetros; rejeita qualquer coisa fora do formato."""
    saida = {}
    for nome, valor in params.items():
        if nome not in TIPOS_PARAMETRO:
            raise ErroColeta(f"parâmetro desconhecido: {nome}")
        if TIPOS_PARAMETRO[nome] == "String":
            if not isinstance(valor, str) or not SERVICO_VALIDO.match(valor):
                raise ErroColeta("nome de serviço inválido")
            saida[nome] = valor
        else:
            v = int(valor)
            maximo = 65535 if TIPOS_PARAMETRO[nome] == "UInt16" else 4294967295
            if not 0 <= v <= maximo:
                raise ErroColeta(f"valor fora da faixa: {nome}")
            saida[nome] = str(v)
    return saida


class ClickHouse:
    def __init__(self, url, usuario, senha, timeout=12):
        self.url = url.rstrip("/")
        self.usuario = usuario
        self.senha = senha
        self.timeout = timeout

    def consultar(self, nome, **params):
        if nome not in CONSULTAS:
            raise ErroColeta("consulta desconhecida")
        sql = CONSULTAS[nome] + " FORMAT JSON"
        qs = {"database": "zabbix"}
        qs.update({f"param_{k}": v for k, v in validar_parametros(params).items()})
        req = urllib.request.Request(
            f"{self.url}/?{urllib.parse.urlencode(qs)}",
            data=sql.encode(),
            headers={"X-ClickHouse-User": self.usuario, "X-ClickHouse-Key": self.senha},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                dados = json.loads(r.read(2 * 1024 * 1024).decode())
        except urllib.error.HTTPError as e:
            raise ErroColeta(f"ClickHouse HTTP {e.code} na consulta {nome}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ErroColeta(f"ClickHouse indisponível ({type(e).__name__})") from None
        return dados.get("data", [])


class ZabbixLeitura:
    def __init__(self, url, token, timeout=10):
        self.url = url
        self.token = token
        self.timeout = timeout

    def chamar(self, metodo, params):
        corpo = json.dumps({"jsonrpc": "2.0", "method": metodo, "params": params, "id": 1}).encode()
        req = urllib.request.Request(self.url, data=corpo, headers={
            "Content-Type": "application/json-rpc", "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                dados = json.loads(r.read(4 * 1024 * 1024).decode())
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ErroColeta(f"API do Zabbix indisponível ({type(e).__name__})") from None
        if "error" in dados:
            raise ErroColeta(f"{metodo}: {dados['error'].get('data', 'erro')}"[:200])
        return dados["result"]


def _janela(clock, minutos_antes, minutos_depois):
    return int(clock) - minutos_antes * 60, int(clock) + minutos_depois * 60


class Coletor:
    """Monta a lista de evidências E1..En para um evento."""

    def __init__(self, ch, zbx, url_frontend="http://127.0.0.1:8080"):
        self.ch = ch
        self.zbx = zbx
        self.url_frontend = url_frontend.rstrip("/")

    # ---- Zabbix ---------------------------------------------------------------
    def dados_evento(self, eventid):
        if not EVENTID_VALIDO.match(str(eventid)):
            raise ErroColeta("eventid inválido")
        ev = self.zbx.chamar("event.get", {
            "eventids": [str(eventid)], "output": ["eventid", "name", "clock", "severity", "objectid", "value",
                                                   "r_eventid", "acknowledged"],
            "selectTags": ["tag", "value"], "selectAcknowledges": ["clock", "action", "message"],
            "selectHosts": ["hostid", "host", "name"]})
        if not ev:
            raise ErroColeta("evento não encontrado ou sem permissão")
        return ev[0]

    def servico_do_evento(self, evento, sugestao=""):
        """Ordem: sugestão do webhook ({$API_SERVICE}) > tag service > macro do host > nome do host."""
        candidatos = []
        if sugestao and not sugestao.startswith("{$"):
            candidatos.append(sugestao)
        for t in evento.get("tags", []):
            if t["tag"] in ("service", "servico", "service.name", "rca.servico"):
                candidatos.append(t["value"])
        hosts = evento.get("hosts", [])
        if hosts:
            try:
                macros = self.zbx.chamar("usermacro.get", {"hostids": [hosts[0]["hostid"]], "output": ["macro", "value"],
                                                           "filter": {"macro": "{$API_SERVICE}"}})
                candidatos += [m["value"] for m in macros]
            except ErroColeta:
                pass
            candidatos.append(hosts[0]["host"])
        for c in candidatos:
            if c and SERVICO_VALIDO.match(c):
                return c
        return ""

    def contexto_zabbix(self, evento, raio_s=1800):
        hosts = evento.get("hosts", [])
        trig = self.zbx.chamar("trigger.get", {
            "triggerids": [evento["objectid"]], "output": ["description", "expression", "priority", "lastchange"],
            "expandExpression": True, "expandDescription": True,
            "selectItems": ["itemid", "key_", "name", "lastvalue", "units", "state", "lastclock"]})
        relacionados = []
        if hosts:
            relacionados = self.zbx.chamar("problem.get", {
                "hostids": [h["hostid"] for h in hosts], "output": ["eventid", "name", "severity", "clock"],
                "time_from": int(evento["clock"]) - raio_s, "time_till": int(evento["clock"]) + raio_s,
                "recent": True, "limit": 10, "sortfield": ["eventid"], "sortorder": "DESC"})
        return (trig[0] if trig else {}), [p for p in relacionados if p["eventid"] != evento["eventid"]]

    # ---- montagem --------------------------------------------------------------
    def coletar(self, eventid, modo="pre", servico_sugerido=""):
        """Devolve dict com evento, serviço, evidências (lista) e falhas (o que NÃO foi verificado)."""
        profundo = modo == "profundo"
        antes, depois = (60, 15) if profundo else (15, 5)
        lim = 20 if profundo else 8
        evidencias, falhas = [], []

        def add(tipo, titulo, dados, refs=None):
            evidencias.append({"id": f"E{len(evidencias) + 1}", "tipo": tipo, "titulo": titulo,
                               "dados": dados, "refs": refs or {}})

        evento = self.dados_evento(eventid)
        clock = int(evento["clock"])
        servico = self.servico_do_evento(evento, servico_sugerido)
        add("zabbix.evento", "Evento do Zabbix", {
            "nome": evento["name"], "severidade": evento["severity"], "inicio": _iso(clock),
            "resolvido": evento.get("r_eventid", "0") != "0",
            "tags": {t["tag"]: t["value"] for t in evento.get("tags", [])},
            "host": evento["hosts"][0]["host"] if evento.get("hosts") else ""},
            {"eventid": evento["eventid"]})
        try:
            trig, relacionados = self.contexto_zabbix(evento)
            if trig:
                add("zabbix.trigger", "Trigger e itens", {
                    "descricao": trig.get("description"), "expressao": trig.get("expression"),
                    "itens": [{"chave": i["key_"], "nome": i["name"], "ultimo_valor": i["lastvalue"],
                               "unidade": i["units"], "nao_suportado": i["state"] == "1"} for i in trig.get("items", [])][:10]},
                    {"triggerid": evento["objectid"]})
            add("zabbix.relacionados", "Outros problemas do host em ±30 min",
                [{"nome": p["name"], "severidade": p["severity"], "inicio": _iso(p["clock"])} for p in relacionados]
                or "nenhum", {"eventids": [p["eventid"] for p in relacionados]})
        except ErroColeta as e:
            falhas.append(f"contexto do Zabbix: {e}")

        if not servico:
            falhas.append("serviço OTel do evento não identificado: sem consultas ao ClickHouse")
            return {"evento": evento, "servico": "", "evidencias": evidencias, "nao_verificado": falhas}

        ini, fim = _janela(clock, antes, depois)
        duracao = fim - ini
        try:
            atual = self.ch.consultar("resumo_spans", svc=servico, ini=ini, fim=fim)
            anterior = self.ch.consultar("resumo_spans", svc=servico, ini=ini - duracao, fim=ini)
            d1 = self.ch.consultar("resumo_spans", svc=servico, ini=ini - 86400, fim=fim - 86400)
            comp = {"janela_atual": _primeiro(atual), "janela_anterior": _primeiro(anterior)}
            if _primeiro(d1).get("spans") not in (None, 0, "0"):
                comp["mesma_janela_D-1"] = _primeiro(d1)
            else:
                falhas.append("comparação D-1: sem dados na mesma janela de ontem")
            add("clickhouse.resumo", f"Spans Server de '{servico}': volume, erros e latência (ms)", comp,
                {"servico": servico, "ini": ini, "fim": fim})
            add("clickhouse.status_http", "Status HTTP na janela",
                self.ch.consultar("status_http", svc=servico, ini=ini, fim=fim), {"servico": servico})
            top = self.ch.consultar("top_spans_erro", svc=servico, ini=ini, fim=fim, lim=lim)
            add("clickhouse.top_erros", "Spans com erro (top)",
                [{k: v for k, v in r.items() if k != "trace_exemplo"} for r in top] or "nenhum span com erro",
                {"traces": [r["trace_exemplo"] for r in top if r.get("trace_exemplo")]})
            logs = self.ch.consultar("logs_agrupados", svc=servico, ini=ini, fim=fim, lim=lim)
            add("clickhouse.logs", "Logs WARN/ERROR agrupados por padrão",
                [{k: v for k, v in r.items() if k != "trace_exemplo"} for r in logs] or "nenhum log WARN/ERROR",
                {"traces": [r["trace_exemplo"] for r in logs if r.get("trace_exemplo")]})
            versoes = self.ch.consultar("versoes", svc=servico, ini=ini - 7200, fim=fim)
            add("clickhouse.versoes", "Versões do serviço (2 h antes até o fim da janela)", versoes or "sem atributo de versão")
            fr = _primeiro(self.ch.consultar("frescor", svc=servico))
            ultimo = int(fr.get("ultimo") or 0)
            add("clickhouse.frescor", "Frescor da ingestão", {
                "ultimo_span": _iso(ultimo) if ultimo else "nenhum nas últimas 24 h",
                "atraso_s": int(time.time()) - ultimo if ultimo else None})
        except ErroColeta as e:
            falhas.append(f"ClickHouse: {e}")
        return {"evento": evento, "servico": servico, "evidencias": evidencias, "nao_verificado": falhas,
                "janela": {"ini": ini, "fim": fim}}

    def link_trace(self, trace_id):
        if not re.fullmatch(r"[0-9a-fA-F]{16,64}", trace_id or ""):
            return ""
        # Rota do frontend do Zabbix 8 (CRouter: apm.trace.list, filtro filter_traceid).
        return f"{self.url_frontend}/zabbix.php?action=apm.trace.list&filter_set=1&filter_traceid={trace_id}"


def _primeiro(linhas):
    return linhas[0] if linhas else {}


def _iso(clock):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(clock)))

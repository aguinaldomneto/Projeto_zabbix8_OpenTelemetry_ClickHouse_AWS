"""Importa o template de observabilidade OTel (Telemetry query) e o dashboard APM.

Idempotente (pode rodar várias vezes) e só cria/atualiza — nunca apaga nada:

1. configuration.import de config/zabbix-templates/otel-clickhouse-apm.yaml
   (rules createMissing/updateExisting; nenhum deleteMissing);
2. garante o grupo "Lab/OTel APM" e o host lógico "otel-apm-lab", monitorado
   pelo proxy com coleta APM, e vincula o template (mantém os já vinculados);
3. cria ou atualiza (pelo nome) o dashboard de
   config/zabbix-templates/otel-apm-dashboard.json, trocando "@item:host:chave"
   pelo itemid real.

Uso (normalmente via scripts/importar-templates.sh):
  ZBX_ADMIN_PASSWORD=... python3 ferramentas/importar_otel_apm.py [--sem-dashboard] [--status]

Variáveis: ZBX_API_URL (padrão http://127.0.0.1:8080/api_jsonrpc.php),
ZBX_PROXY_NAME (zabbix-proxy-otel), OTEL_APM_HOST (otel-apm-lab),
ZBX_TEMPLATES_DIR (config/zabbix-templates ao lado deste repositório).
Só biblioteca padrão. Nunca imprime a senha.
"""

import json
import os
import sys
import time
import urllib.request

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_URL = os.environ.get("ZBX_API_URL", "http://127.0.0.1:8080/api_jsonrpc.php")
SENHA = os.environ.get("ZBX_ADMIN_PASSWORD", "")
PROXY = os.environ.get("ZBX_PROXY_NAME", "zabbix-proxy-otel")
HOST = os.environ.get("OTEL_APM_HOST", "otel-apm-lab")
GRUPO_HOST = "Lab/OTel APM"
TEMPLATE = "OTel ClickHouse APM"
PASTA = os.environ.get("ZBX_TEMPLATES_DIR", os.path.join(RAIZ, "config", "zabbix-templates"))
ARQ_TEMPLATE = os.path.join(PASTA, "otel-clickhouse-apm.yaml")
ARQ_DASHBOARD = os.path.join(PASTA, "otel-apm-dashboard.json")

REGRAS = {
    "template_groups": {"createMissing": True, "updateExisting": True},
    "host_groups": {"createMissing": True, "updateExisting": True},
    "templates": {"createMissing": True, "updateExisting": True},
    "items": {"createMissing": True, "updateExisting": True},
    "discoveryRules": {"createMissing": True, "updateExisting": True},
    "triggers": {"createMissing": True, "updateExisting": True},
    "graphs": {"createMissing": True, "updateExisting": True},
    "valueMaps": {"createMissing": True, "updateExisting": True},
    "templateDashboards": {"createMissing": True, "updateExisting": True},
    "templateLinkage": {"createMissing": True},
}


class ErroApi(Exception):
    pass


def chamar(metodo, params, token=None):
    corpo = json.dumps({"jsonrpc": "2.0", "method": metodo, "params": params, "id": 1}).encode()
    cab = {"Content-Type": "application/json-rpc"}
    if token:
        cab["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(API_URL, data=corpo, headers=cab), timeout=60) as r:
        dados = json.loads(r.read().decode())
    if "error" in dados:
        e = dados["error"]
        raise ErroApi(f"{metodo}: {e.get('message', '')} {e.get('data', '')}".strip())
    return dados["result"]


def importar_template(token):
    with open(ARQ_TEMPLATE, encoding="utf-8") as f:
        fonte = f.read()
    chamar("configuration.import", {"format": "yaml", "source": fonte, "rules": REGRAS}, token)
    print(f"[ok] template importado: {os.path.basename(ARQ_TEMPLATE)}")


def garantir_host(token):
    grupos = chamar("hostgroup.get", {"output": ["groupid"], "filter": {"name": GRUPO_HOST}}, token)
    gid = grupos[0]["groupid"] if grupos else chamar("hostgroup.create", {"name": GRUPO_HOST}, token)["groupids"][0]

    proxies = chamar("proxy.get", {"output": ["proxyid"], "filter": {"name": PROXY}}, token)
    if not proxies:
        sys.exit(f"[erro] proxy '{PROXY}' não cadastrado (rode 'make bootstrap' antes)")
    proxyid = proxies[0]["proxyid"]

    tpl = chamar("template.get", {"output": ["templateid"], "filter": {"host": TEMPLATE}}, token)[0]["templateid"]
    hosts = chamar("host.get", {"output": ["hostid"], "filter": {"host": HOST},
                                "selectParentTemplates": ["templateid"]}, token)
    if not hosts:
        hid = chamar("host.create", {
            "host": HOST, "name": HOST, "groups": [{"groupid": gid}], "monitored_by": 1, "proxyid": proxyid,
            "description": "Host lógico para os itens Telemetry query (APM/OTel) do laboratório.",
            "templates": [{"templateid": tpl}],
        }, token)["hostids"][0]
        print(f"[ok] host '{HOST}' criado (id {hid}), monitorado pelo proxy '{PROXY}', template vinculado")
        return
    host = hosts[0]
    vinculados = [{"templateid": t["templateid"]} for t in host["parentTemplates"]]
    if any(t["templateid"] == tpl for t in vinculados):
        print(f"[ok] host '{HOST}' já existe (id {host['hostid']}) com o template vinculado")
    else:
        chamar("host.update", {"hostid": host["hostid"], "templates": vinculados + [{"templateid": tpl}]}, token)
        print(f"[ok] template vinculado ao host '{HOST}' (id {host['hostid']})")


def resolver_item(ref, token):
    _, host, chave = ref.split(":", 2)
    itens = chamar("item.get", {"output": ["itemid"], "host": host, "filter": {"key_": chave}}, token)
    if not itens:
        raise ErroApi(f"item não encontrado para o dashboard: {host}:{chave}")
    return itens[0]["itemid"]


def importar_dashboard(token):
    with open(ARQ_DASHBOARD, encoding="utf-8") as f:
        spec = json.load(f)
    spec.pop("_comentario", None)
    for pagina in spec["pages"]:
        for w in pagina["widgets"]:
            for campo in w.get("fields", []):
                if str(campo["value"]).startswith("@item:"):
                    campo["value"] = resolver_item(campo["value"], token)

    existentes = chamar("dashboard.get", {"output": ["dashboardid"], "filter": {"name": spec["name"]}}, token)
    if existentes:
        spec["dashboardid"] = existentes[0]["dashboardid"]
        chamar("dashboard.update", spec, token)
        print(f"[ok] dashboard '{spec['name']}' atualizado (id {spec['dashboardid']})")
    else:
        did = chamar("dashboard.create", spec, token)["dashboardids"][0]
        print(f"[ok] dashboard '{spec['name']}' criado (id {did})")


def mostrar_status(token):
    agora = int(time.time())
    hid = chamar("host.get", {"output": ["hostid"], "filter": {"host": HOST}}, token)[0]["hostid"]
    itens = chamar("item.get", {"hostids": hid, "output": ["key_", "state", "lastvalue", "lastclock", "error", "units"],
                                "sortfield": "key_"}, token)
    for i in itens:
        idade = agora - int(i["lastclock"]) if i["lastclock"] != "0" else None
        estado = "NOT SUPPORTED" if i["state"] == "1" else "ok"
        print(f"  {i['key_']:<45} {estado:<13} valor={i['lastvalue'][:60]!r} {i['units']} idade={idade}s {i['error'][:120]}")


def main():
    if not SENHA:
        sys.exit("[erro] defina ZBX_ADMIN_PASSWORD (scripts/importar-templates.sh lê do .env)")
    print(f"[info] API {API_URL}, versão {chamar('apiinfo.version', [])}")
    token = chamar("user.login", {"username": "Admin", "password": SENHA})
    try:
        if "--status" in sys.argv:
            mostrar_status(token)
            return
        importar_template(token)
        garantir_host(token)
        if "--sem-dashboard" not in sys.argv:
            importar_dashboard(token)
    finally:
        try:
            chamar("user.logout", [], token)
        except Exception:
            pass
    print("[ok] pronto. Itens Telemetry query coletam a cada 1 minuto; a descoberta de serviços a cada {$OTEL.LLD.INTERVAL} (5m).")


if __name__ == "__main__":
    main()

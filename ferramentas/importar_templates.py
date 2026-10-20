"""Importa os templates do laboratório, vincula-os aos hosts e cria o dashboard APM.

Idempotente (pode rodar várias vezes) e só cria/atualiza — nunca apaga nada:

1. configuration.import de TODOS os config/zabbix-templates/*.yaml
   (rules createMissing/updateExisting; nenhum deleteMissing);
2. host lógico "otel-apm-lab" (grupo "Lab/OTel APM", monitorado pelo proxy)
   com o template "OTel ClickHouse APM";
3. um host por API fictícia (pedidos, pagamentos, catalogo, streaming,
   autenticacao; grupo "Lab/APIs fictícias") monitorado pelo proxy, com a macro
   {$API_SERVICE} e o template "APM API ficticia" (os templates já vinculados
   são mantidos). Telemetry query só é coletado por proxy/server com provedor
   de telemetria, por isso o host precisa estar no proxy;
4. cria ou atualiza (pelo nome) o dashboard de
   config/zabbix-templates/otel-apm-dashboard.json, trocando "@item:host:chave"
   pelo itemid real.

Uso (normalmente via scripts/importar-templates.sh):
  ZBX_ADMIN_PASSWORD=... python3 ferramentas/importar_templates.py [--sem-dashboard] [--status]

Variáveis: ZBX_API_URL (padrão http://127.0.0.1:8080/api_jsonrpc.php),
ZBX_PROXY_NAME (zabbix-proxy-otel), OTEL_APM_HOST (otel-apm-lab),
API_HOSTS (pedidos,pagamentos,catalogo,streaming,autenticacao),
ZBX_TEMPLATES_DIR (config/zabbix-templates ao lado deste repositório).
Só biblioteca padrão. Nunca imprime a senha.
"""

import glob
import json
import os
import sys
import time
import urllib.request

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_URL = os.environ.get("ZBX_API_URL", "http://127.0.0.1:8080/api_jsonrpc.php")
SENHA = os.environ.get("ZBX_ADMIN_PASSWORD", "")
PROXY = os.environ.get("ZBX_PROXY_NAME", "zabbix-proxy-otel")
HOST_OTEL = os.environ.get("OTEL_APM_HOST", "otel-apm-lab")
GRUPO_OTEL = "Lab/OTel APM"
TEMPLATE_OTEL = "OTel ClickHouse APM"
HOSTS_API = [h.strip() for h in os.environ.get(
    "API_HOSTS", "pedidos,pagamentos,catalogo,streaming,autenticacao").split(",") if h.strip()]
GRUPO_API = "Lab/APIs fictícias"
TEMPLATE_API = "APM API ficticia"
PASTA = os.environ.get("ZBX_TEMPLATES_DIR", os.path.join(RAIZ, "config", "zabbix-templates"))
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


def importar_templates(token):
    arquivos = sorted(glob.glob(os.path.join(PASTA, "*.yaml")))
    if not arquivos:
        sys.exit(f"[erro] nenhum template em {PASTA}")
    for caminho in arquivos:
        with open(caminho, encoding="utf-8") as f:
            fonte = f.read()
        chamar("configuration.import", {"format": "yaml", "source": fonte, "rules": REGRAS}, token)
        print(f"[ok] template importado: {os.path.basename(caminho)}")


def garantir_grupo(nome, token):
    grupos = chamar("hostgroup.get", {"output": ["groupid"], "filter": {"name": nome}}, token)
    return grupos[0]["groupid"] if grupos else chamar("hostgroup.create", {"name": nome}, token)["groupids"][0]


def id_proxy(token):
    proxies = chamar("proxy.get", {"output": ["proxyid"], "filter": {"name": PROXY}}, token)
    if not proxies:
        sys.exit(f"[erro] proxy '{PROXY}' não cadastrado (rode 'make bootstrap' antes)")
    return proxies[0]["proxyid"]


def garantir_host(token, host, grupo, template, descricao, macros=None):
    """Cria o host (se faltar), garante proxy, macros ausentes e o template vinculado."""
    gid = garantir_grupo(grupo, token)
    proxyid = id_proxy(token)
    tpl = chamar("template.get", {"output": ["templateid"], "filter": {"host": template}}, token)
    if not tpl:
        sys.exit(f"[erro] template '{template}' não encontrado após a importação")
    tpl = tpl[0]["templateid"]
    macros = macros or []
    hosts = chamar("host.get", {"output": ["hostid", "monitored_by", "proxyid"], "filter": {"host": host},
                                "selectParentTemplates": ["templateid"], "selectMacros": ["macro"]}, token)
    if not hosts:
        hid = chamar("host.create", {
            "host": host, "name": host, "groups": [{"groupid": gid}], "monitored_by": 1, "proxyid": proxyid,
            "description": descricao, "templates": [{"templateid": tpl}], "macros": macros,
        }, token)["hostids"][0]
        print(f"[ok] host '{host}' criado (id {hid}), proxy '{PROXY}', template '{template}' vinculado")
        return
    h = hosts[0]
    upd = {}
    if h["monitored_by"] != "1" or h["proxyid"] != proxyid:
        upd["monitored_by"] = 1
        upd["proxyid"] = proxyid
    vinculados = [{"templateid": t["templateid"]} for t in h["parentTemplates"]]
    if not any(t["templateid"] == tpl for t in vinculados):
        upd["templates"] = vinculados + [{"templateid": tpl}]
    existentes = {m["macro"] for m in h["macros"]}
    novas = [m for m in macros if m["macro"] not in existentes]
    for m in novas:
        chamar("usermacro.create", {"hostid": h["hostid"], **m}, token)
    if upd:
        chamar("host.update", {"hostid": h["hostid"], **upd}, token)
        print(f"[ok] host '{host}' (id {h['hostid']}) atualizado: {', '.join(sorted(upd) + [m['macro'] for m in novas])}")
        if "monitored_by" in upd:
            print(f"     (agora monitorado pelo proxy '{PROXY}')")
    elif novas:
        print(f"[ok] host '{host}' (id {h['hostid']}): macros criadas")
    else:
        print(f"[ok] host '{host}' já está configurado (id {h['hostid']})")


def garantir_hosts(token):
    garantir_host(token, HOST_OTEL, GRUPO_OTEL, TEMPLATE_OTEL,
                  "Host lógico para os itens Telemetry query (APM/OTel) do laboratório.")
    for api in HOSTS_API:
        garantir_host(token, api, GRUPO_API, TEMPLATE_API,
                      f"API fictícia '{api}': métricas por Telemetry query (service.name = {api}).",
                      [{"macro": "{$API_SERVICE}", "value": api}])


def resolver_item(ref, token):
    _, host, chave = ref.split(":", 2)
    itens = chamar("item.get", {"output": ["itemid"], "host": host, "filter": {"key_": chave}}, token)
    if not itens:
        raise ErroApi(f"item não encontrado para o dashboard: {host}:{chave}")
    return itens[0]["itemid"]


def importar_dashboard(token):
    if not os.path.exists(ARQ_DASHBOARD):
        return
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
    for host in [HOST_OTEL] + HOSTS_API:
        hs = chamar("host.get", {"output": ["hostid"], "filter": {"host": host}}, token)
        if not hs:
            print(f"[{host}] host não existe (rode make templates)")
            continue
        hid = hs[0]["hostid"]
        itens = chamar("item.get", {"hostids": hid, "output": ["key_", "state", "lastvalue", "lastclock", "error", "units"],
                                    "sortfield": "key_"}, token)
        print(f"[{host}] {len(itens)} itens")
        for i in itens:
            idade = agora - int(i["lastclock"]) if i["lastclock"] != "0" else None
            estado = "NOT SUPPORTED" if i["state"] == "1" else "ok"
            print(f"  {i['key_']:<45} {estado:<13} valor={i['lastvalue'][:40]!r} {i['units']} idade={idade}s {i['error'][:120]}")
        trig = chamar("trigger.get", {"hostids": hid, "output": ["description", "value", "state", "error", "priority"],
                                      "expandDescription": True}, token)
        for t in trig:
            avaliacao = "ERRO" if t["state"] == "1" else ("PROBLEMA" if t["value"] == "1" else "ok")
            print(f"  trigger: {t['description'][:70]:<70} {avaliacao} {t['error'][:100]}")


def main():
    if not SENHA:
        sys.exit("[erro] defina ZBX_ADMIN_PASSWORD (scripts/importar-templates.sh lê do .env)")
    print(f"[info] API {API_URL}, versão {chamar('apiinfo.version', [])}")
    token = chamar("user.login", {"username": "Admin", "password": SENHA})
    try:
        if "--status" in sys.argv:
            mostrar_status(token)
            return
        importar_templates(token)
        garantir_hosts(token)
        if "--sem-dashboard" not in sys.argv:
            importar_dashboard(token)
    finally:
        try:
            chamar("user.logout", [], token)
        except Exception:
            pass
    print("[ok] pronto. Itens Telemetry query coletam a cada 1 minuto; veja o estado com 'make templates-status'.")


if __name__ == "__main__":
    main()

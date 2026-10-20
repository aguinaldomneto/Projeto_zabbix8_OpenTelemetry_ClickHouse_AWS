"""Configura no Zabbix o fluxo do investigador (idempotente, por nome).

  (padrão)     cria/atualiza: macros globais, media type webhook, papel e grupo
               mínimos, usuário svc-investigador (+ mídia + token de API gravado
               no .env como ZBX_INVESTIGADOR_TOKEN, sem imprimir), action de
               pré-análise, módulo do widget e dashboard "Investigador IA (RCA)"
  --desligar   desliga a action e o media type (não apaga nada)
  --ligar      religa a action e o media type
  --remover    apaga tudo o que este script cria (rollback completo)
  --status     mostra o estado

Definições em config/zabbix-investigador/ (objetos.json + webhook.js).
Variáveis: ZBX_ADMIN_PASSWORD (obrigatória), ZBX_API_URL, ENV_FILE,
INVESTIGADOR_TOKEN, ZBX_FRONTEND_URL. Só biblioteca padrão; nunca imprime
senha ou token.
"""

import json
import os
import re
import secrets
import sys
import urllib.request

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASTA = os.path.join(RAIZ, "config", "zabbix-investigador")
API_URL = os.environ.get("ZBX_API_URL", "http://127.0.0.1:8080/api_jsonrpc.php")
ENV_FILE = os.environ.get("ENV_FILE", os.path.join(RAIZ, ".env"))
FRONTEND = os.environ.get("ZBX_FRONTEND_URL", "http://127.0.0.1:8080").rstrip("/")


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


def ler_env(nome):
    if os.environ.get(nome):
        return os.environ[nome]
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for linha in f:
                if linha.startswith(nome + "="):
                    return linha.split("=", 1)[1].strip().strip("'\"")
    except FileNotFoundError:
        pass
    return ""


def gravar_env(nome, valor):
    """Grava/atualiza NOME=valor no .env (permissão 600). Nunca imprime o valor."""
    linhas = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            linhas = [l for l in f.read().splitlines() if not l.startswith(nome + "=")]
    linhas.append(f"{nome}={valor}")
    fd = os.open(ENV_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")
    os.replace(ENV_FILE + ".tmp", ENV_FILE)
    os.chmod(ENV_FILE, 0o600)


def carregar():
    with open(os.path.join(PASTA, "objetos.json"), encoding="utf-8") as f:
        obj = json.load(f)
    with open(os.path.join(PASTA, "webhook.js"), encoding="utf-8") as f:
        obj["mediatype"]["script"] = f.read()
    obj["mediatype"]["event_menu_url"] = obj["mediatype"]["event_menu_url"].replace("@frontend", FRONTEND)
    return obj


def um(metodo, filtro, token, saida=None):
    r = chamar(metodo, {"output": saida or "extend", "filter": filtro}, token)
    return r[0] if r else None


# ---- criação/atualização -----------------------------------------------------------
def garantir_macros(obj, t):
    for m in obj["macros_globais"]:
        valor = m["value"]
        if valor.startswith("@env:"):
            valor = ler_env(valor[5:])
            if not valor:
                sys.exit(f"[erro] {m['value'][5:]} vazio no .env (rode 'make env')")
        atual = chamar("usermacro.get", {"globalmacro": True, "output": ["globalmacroid"],
                                         "filter": {"macro": m["macro"]}}, t)
        dados = {"macro": m["macro"], "value": valor, "type": m["type"], "description": m["description"]}
        if atual:
            chamar("usermacro.updateglobal", {"globalmacroid": atual[0]["globalmacroid"], **dados}, t)
        else:
            chamar("usermacro.createglobal", dados, t)
        print(f"[ok] macro global {m['macro']}{' (secreta)' if m['type'] == 1 else ''}")


def garantir_mediatype(obj, t):
    mt = dict(obj["mediatype"])
    atual = um("mediatype.get", {"name": mt["name"]}, t, ["mediatypeid"])
    if atual:
        mt.pop("type", None)
        chamar("mediatype.update", {"mediatypeid": atual["mediatypeid"], **mt}, t)
        print(f"[ok] media type '{mt['name']}' atualizado (id {atual['mediatypeid']})")
        return atual["mediatypeid"]
    mid = chamar("mediatype.create", {**mt, "status": 0}, t)["mediatypeids"][0]
    print(f"[ok] media type '{mt['name']}' criado (id {mid})")
    return mid


def garantir_role(obj, t):
    r = obj["role"]
    atual = um("role.get", {"name": r["name"]}, t, ["roleid"])
    if atual:
        chamar("role.update", {"roleid": atual["roleid"], "rules": r["rules"]}, t)
        print(f"[ok] papel '{r['name']}' atualizado (API só: {', '.join(r['rules']['api'])})")
        return atual["roleid"]
    rid = chamar("role.create", r, t)["roleids"][0]
    print(f"[ok] papel '{r['name']}' criado (id {rid})")
    return rid


def garantir_grupo(obj, t):
    g = obj["usergroup"]
    ids = set()
    for nome in g["grupos_leitura"]:
        hg = um("hostgroup.get", {"name": nome}, t, ["groupid"])
        if hg:
            ids.add(hg["groupid"])
        else:
            print(f"[aviso] grupo de hosts '{nome}' não existe (rode 'make templates'); sem leitura nele")
    # A action só notifica quem tem leitura no host do evento: inclui os grupos
    # reais dos hosts que usam os templates APM (ex.: "APIs ficticias").
    tpls = chamar("template.get", {"output": ["templateid"], "filter": {"host": g.get("templates_alvo", [])}}, t)
    if tpls:
        hosts = chamar("host.get", {"output": ["hostid"], "templateids": [x["templateid"] for x in tpls],
                                    "selectHostGroups": ["groupid"]}, t)
        ids.update(hg["groupid"] for h in hosts for hg in h["hostgroups"])
    direitos = [{"id": i, "permission": 2} for i in sorted(ids, key=int)]
    atual = um("usergroup.get", {"name": g["name"]}, t, ["usrgrpid"])
    dados = {"gui_access": g["gui_access"], "users_status": 0, "hostgroup_rights": direitos}
    if atual:
        chamar("usergroup.update", {"usrgrpid": atual["usrgrpid"], **dados}, t)
        print(f"[ok] grupo de usuários '{g['name']}' atualizado (leitura em {len(direitos)} grupo(s) de hosts)")
        return atual["usrgrpid"]
    gid = chamar("usergroup.create", {"name": g["name"], **dados}, t)["usrgrpids"][0]
    print(f"[ok] grupo de usuários '{g['name']}' criado (id {gid})")
    return gid


def garantir_usuario(obj, t, roleid, usrgrpid, mediatypeid):
    u = obj["user"]
    midia = [{"mediatypeid": mediatypeid, "sendto": u["sendto"], "active": 0, "severity": 63,
              "period": "1-7,00:00-24:00"}]
    atual = chamar("user.get", {"output": ["userid"], "filter": {"username": u["username"]}}, t)
    if atual:
        uid = atual[0]["userid"]
        chamar("user.update", {"userid": uid, "roleid": roleid, "usrgrps": [{"usrgrpid": usrgrpid}], "medias": midia}, t)
        print(f"[ok] usuário '{u['username']}' atualizado (id {uid})")
    else:
        senha = secrets.token_urlsafe(24) + "aA1!"  # nunca usada nem exibida (acesso só por token)
        uid = chamar("user.create", {"username": u["username"], "name": u["name"], "passwd": senha, "roleid": roleid,
                                     "usrgrps": [{"usrgrpid": usrgrpid}], "medias": midia}, t)["userids"][0]
        print(f"[ok] usuário '{u['username']}' criado (id {uid})")
    return uid


def garantir_token(obj, t, userid):
    nome = obj["user"]["token_nome"]
    atual = chamar("token.get", {"output": ["tokenid"], "userids": [userid], "filter": {"name": nome}}, t)
    existente = ler_env("ZBX_INVESTIGADOR_TOKEN")
    if atual and existente:
        try:
            chamar("apiinfo.version", [], None)
            chamar("event.get", {"limit": 1, "output": ["eventid"]}, existente)
            print("[ok] token de API do svc-investigador já existe e funciona (valor não exibido)")
            return
        except ErroApi:
            pass
    if atual:
        chamar("token.delete", [atual[0]["tokenid"]], t)
    tid = chamar("token.create", {"name": nome, "userid": userid, "status": 0,
                                  "description": "Token do serviço investigador (leitura + comentário)"}, t)["tokenids"][0]
    valor = chamar("token.generate", [tid], t)[0]["token"]
    gravar_env("ZBX_INVESTIGADOR_TOKEN", valor)
    print("[ok] token de API gerado e gravado em .env como ZBX_INVESTIGADOR_TOKEN (valor não exibido)")


def garantir_action(obj, t, mediatypeid, userid):
    a = obj["action"]
    dados = {"esc_period": a["esc_period"], "filter": a["filter"],
             "operations": [{"operationtype": 0, "opmessage": {"default_msg": 1, "mediatypeid": mediatypeid},
                             "opmessage_usr": [{"userid": userid}]}]}
    atual = um("action.get", {"name": a["name"]}, t, ["actionid"])
    if atual:
        chamar("action.update", {"actionid": atual["actionid"], **dados}, t)
        print(f"[ok] action '{a['name']}' atualizada (id {atual['actionid']})")
        return
    aid = chamar("action.create", {"name": a["name"], "eventsource": a["eventsource"], "status": 0, **dados}, t)["actionids"][0]
    print(f"[ok] action '{a['name']}' criada (id {aid})")


def garantir_modulo(obj, t):
    caminho = obj["modulo"]["relative_path"]
    mods = chamar("module.get", {"output": ["moduleid", "relative_path", "status"]}, t)
    m = next((x for x in mods if x["relative_path"] == caminho), None)
    if m:
        if m["status"] != "1":
            chamar("module.update", {"moduleid": m["moduleid"], "status": 1}, t)
        print(f"[ok] módulo do widget habilitado (id {m['moduleid']})")
        return
    mid = chamar("module.create", {"id": "investigador", "relative_path": caminho, "status": 1}, t)["moduleids"][0]
    print(f"[ok] módulo do widget cadastrado e habilitado (id {mid})")


def garantir_dashboard(obj, t):
    nome = obj["dashboard"]["name"]
    if um("dashboard.get", {"name": nome}, t, ["dashboardid"]):
        print(f"[ok] dashboard '{nome}' já existe")
        return
    did = chamar("dashboard.create", {"name": nome, "display_period": 30, "auto_start": 1, "pages": [{"widgets": [
        {"type": "investigador", "name": "Investigador (RCA)", "x": 0, "y": 0, "width": 72, "height": 10,
         "view_mode": 0, "fields": []}]}]}, t)["dashboardids"][0]
    print(f"[ok] dashboard '{nome}' criado (id {did})")


# ---- liga/desliga/remove/status -------------------------------------------------------
def ligar(obj, t, status):
    a = um("action.get", {"name": obj["action"]["name"]}, t, ["actionid"])
    m = um("mediatype.get", {"name": obj["mediatype"]["name"]}, t, ["mediatypeid"])
    if a:
        chamar("action.update", {"actionid": a["actionid"], "status": status}, t)
    if m:
        chamar("mediatype.update", {"mediatypeid": m["mediatypeid"], "status": status}, t)
    print(f"[ok] action e media type {'ligados' if status == 0 else 'desligados'}")


def remover(obj, t):
    def apagar(metodo_get, filtro, campo, metodo_del, rotulo):
        x = um(metodo_get, filtro, t, [campo])
        if x:
            chamar(metodo_del, [x[campo]], t)
            print(f"[ok] removido: {rotulo}")

    apagar("dashboard.get", {"name": obj["dashboard"]["name"]}, "dashboardid", "dashboard.delete", "dashboard")
    apagar("action.get", {"name": obj["action"]["name"]}, "actionid", "action.delete", "action")
    usuarios = chamar("user.get", {"output": ["userid"], "filter": {"username": obj["user"]["username"]}}, t)
    if usuarios:
        chamar("user.delete", [usuarios[0]["userid"]], t)
        print("[ok] removido: usuário svc-investigador (e o token dele)")
    apagar("usergroup.get", {"name": obj["usergroup"]["name"]}, "usrgrpid", "usergroup.delete", "grupo de usuários")
    apagar("role.get", {"name": obj["role"]["name"]}, "roleid", "role.delete", "papel")
    apagar("mediatype.get", {"name": obj["mediatype"]["name"]}, "mediatypeid", "mediatype.delete", "media type")
    for m in obj["macros_globais"]:
        x = chamar("usermacro.get", {"globalmacro": True, "output": ["globalmacroid"], "filter": {"macro": m["macro"]}}, t)
        if x:
            chamar("usermacro.deleteglobal", [x[0]["globalmacroid"]], t)
            print(f"[ok] removida: macro {m['macro']}")
    mods = chamar("module.get", {"output": ["moduleid", "relative_path"]}, t)
    for x in mods:
        if x["relative_path"] == obj["modulo"]["relative_path"]:
            chamar("module.delete", [x["moduleid"]], t)
            print("[ok] removido: módulo do widget")
    if ler_env("ZBX_INVESTIGADOR_TOKEN"):
        gravar_env("ZBX_INVESTIGADOR_TOKEN", "")
        print("[ok] ZBX_INVESTIGADOR_TOKEN esvaziado no .env")


def status(obj, t):
    a = um("action.get", {"name": obj["action"]["name"]}, t, ["actionid", "status"])
    m = um("mediatype.get", {"name": obj["mediatype"]["name"]}, t, ["mediatypeid", "status"])
    u = chamar("user.get", {"output": ["userid"], "filter": {"username": obj["user"]["username"]}}, t)
    print(f"action: {'ausente' if not a else ('ligada' if a['status'] == '0' else 'desligada')}")
    print(f"media type: {'ausente' if not m else ('ligado' if m['status'] == '0' else 'desligado')}")
    print(f"usuário svc-investigador: {'existe' if u else 'ausente'}; token no .env: {'sim' if ler_env('ZBX_INVESTIGADOR_TOKEN') else 'não'}")
    if m:
        alertas = chamar("alert.get", {"output": ["clock", "status", "error", "eventid"], "mediatypeids": [m["mediatypeid"]],
                                       "sortfield": "clock", "sortorder": "DESC", "limit": 5}, t)
        for al in alertas:
            print(f"  alerta evento {al['eventid']}: status {al['status']} {re.sub(r'[^ -~À-ÿ]', '', al['error'])[:120]}")


def main():
    senha = ler_env("ZBX_ADMIN_PASSWORD")
    if not senha:
        sys.exit("[erro] ZBX_ADMIN_PASSWORD não encontrado (.env)")
    obj = carregar()
    t = chamar("user.login", {"username": "Admin", "password": senha})
    try:
        if "--desligar" in sys.argv:
            ligar(obj, t, 1)
        elif "--ligar" in sys.argv:
            ligar(obj, t, 0)
        elif "--remover" in sys.argv:
            remover(obj, t)
        elif "--status" in sys.argv:
            status(obj, t)
        else:
            garantir_macros(obj, t)
            mid = garantir_mediatype(obj, t)
            rid = garantir_role(obj, t)
            gid = garantir_grupo(obj, t)
            uid = garantir_usuario(obj, t, rid, gid, mid)
            garantir_token(obj, t, uid)
            garantir_action(obj, t, mid, uid)
            garantir_modulo(obj, t)
            garantir_dashboard(obj, t)
            print("[ok] pronto. Recrie o investigador para ler o token novo: make investigador-up")
    finally:
        try:
            chamar("user.logout", [], t)
        except Exception:
            pass


if __name__ == "__main__":
    main()

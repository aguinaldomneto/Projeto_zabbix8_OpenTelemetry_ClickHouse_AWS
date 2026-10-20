"""Configuração inicial do Zabbix pela API (pode rodar várias vezes).

1. Espera a API responder.
2. Troca a senha padrão do Admin (zabbix) pela senha do arquivo .env.
3. Cadastra o proxy ativo e habilita nele a coleta OpenTelemetry, ou atualiza
   o cadastro se o proxy já existir.
4. Habilita o módulo do widget "Insights" (IA) e importa os templates de
   config/zabbix-templates/ (idempotente: pode rodar várias vezes).

Usa só a biblioteca padrão do Python.
"""

import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request

API_URL = os.environ.get("ZBX_API_URL", "http://zabbix-web:8080/api_jsonrpc.php")
SENHA_NOVA = os.environ.get("ZBX_ADMIN_PASSWORD", "")
SENHA_PADRAO = "zabbix"
USUARIO = "Admin"
PROXY = os.environ.get("ZBX_PROXY_NAME", "zabbix-proxy-otel")
ESPERA_MAX_S = int(os.environ.get("BOOTSTRAP_ESPERA_S", "600"))
TENTATIVAS_LOGIN = int(os.environ.get("BOOTSTRAP_TENTATIVAS_LOGIN", "4"))
PASTA_TEMPLATES = os.environ.get("ZBX_TEMPLATES_DIR", "/app/zabbix-templates")


class ErroApi(Exception):
    pass


def chamar(metodo, params, token=None):
    corpo = json.dumps({"jsonrpc": "2.0", "method": metodo, "params": params, "id": 1}).encode()
    cabecalhos = {"Content-Type": "application/json-rpc"}
    if token:
        cabecalhos["Authorization"] = f"Bearer {token}"

    requisicao = urllib.request.Request(API_URL, data=corpo, headers=cabecalhos)
    with urllib.request.urlopen(requisicao, timeout=20) as resposta:
        dados = json.loads(resposta.read().decode())

    if "error" in dados:
        erro = dados["error"]
        raise ErroApi(f"{metodo}: {erro.get('message', '')} {erro.get('data', '')}".strip())

    return dados["result"]


def esperar_api():
    limite = time.monotonic() + ESPERA_MAX_S
    ultimo_erro = None

    while time.monotonic() < limite:
        try:
            versao = chamar("apiinfo.version", [])
            print(f"[ok] API do Zabbix respondendo, versão {versao}")
            return
        except (urllib.error.URLError, OSError, ValueError, ErroApi) as erro:
            ultimo_erro = erro
            time.sleep(5)

    sys.exit(f"[erro] a API não respondeu em {ESPERA_MAX_S}s ({API_URL}): {ultimo_erro}")


def entrar():
    """Devolve um token de sessão; troca a senha padrão se ela ainda valer."""
    ultimo_erro = None

    # Na primeira subida a API pode responder antes de o server terminar de
    # carregar os dados iniciais (usuários). Por isso algumas tentativas.
    for tentativa in range(1, TENTATIVAS_LOGIN + 1):
        try:
            token = chamar("user.login", {"username": USUARIO, "password": SENHA_NOVA})
            print("[ok] login com a senha do .env (senha padrão já tinha sido trocada)")
            return token
        except ErroApi:
            pass

        try:
            token = chamar("user.login", {"username": USUARIO, "password": SENHA_PADRAO})
            break
        except ErroApi as erro:
            ultimo_erro = erro
            if tentativa < TENTATIVAS_LOGIN:
                print(f"[info] login ainda não aceito (tentativa {tentativa} de {TENTATIVAS_LOGIN}); aguardando 20s")
                time.sleep(20)
    else:
        sys.exit(
            "[erro] não consegui entrar como Admin nem com a senha do .env nem com a padrão.\n"
            "       Se você trocou a senha pela interface, ajuste ZBX_ADMIN_PASSWORD no .env.\n"
            f"       Detalhe: {ultimo_erro}"
        )

    usuarios = chamar("user.get", {"output": ["userid"], "filter": {"username": USUARIO}}, token)
    chamar(
        "user.update",
        {"userid": usuarios[0]["userid"], "current_passwd": SENHA_PADRAO, "passwd": SENHA_NOVA},
        token,
    )
    print("[ok] senha padrão do Admin trocada pela senha do .env")

    return chamar("user.login", {"username": USUARIO, "password": SENHA_NOVA})


def configurar_proxy(token):
    # 1 = coleta OpenTelemetry habilitada; 0 em max_messages_per_second = sem limite
    apm = {"data_collection_status": 1, "max_messages_per_second": 0}

    existentes = chamar(
        "proxy.get",
        {"output": ["proxyid", "name", "operating_mode"], "filter": {"name": PROXY}},
        token,
    )

    if existentes:
        proxyid = existentes[0]["proxyid"]
        chamar("proxy.update", {"proxyid": proxyid, "apm": apm}, token)
        print(f"[ok] proxy '{PROXY}' já existia (id {proxyid}); coleta OpenTelemetry habilitada")
    else:
        # operating_mode 0 = proxy ativo (é o proxy que conecta no server)
        resultado = chamar("proxy.create", {"name": PROXY, "operating_mode": 0, "apm": apm}, token)
        print(f"[ok] proxy '{PROXY}' cadastrado (id {resultado['proxyids'][0]}) com coleta OpenTelemetry habilitada")

    conferido = chamar(
        "proxy.get",
        {
            "output": ["proxyid", "name", "version", "lastaccess"],
            "selectApm": ["data_collection_status", "max_messages_per_second"],
            "filter": {"name": PROXY},
        },
        token,
    )
    print(f"[info] cadastro atual: {json.dumps(conferido[0], ensure_ascii=False)}")


def configurar_widget_insights(token):
    # 1 = MODULE_STATUS_ENABLED (include/defines.inc.php). O módulo mora em
    # config/zabbix-widgets/insights, montado em /usr/share/zabbix/modules/
    # insights no zabbix-web (compose.yaml).
    modulo_id = "insights"
    caminho_relativo = "modules/insights"

    existentes = chamar("module.get", {"output": ["moduleid", "relative_path", "status"]}, token)
    existente = next((m for m in existentes if m.get("relative_path") == caminho_relativo), None)

    if existente:
        chamar("module.update", {"moduleid": existente["moduleid"], "status": 1}, token)
        print(f"[ok] módulo do widget Insights já existia (id {existente['moduleid']}); habilitado")
    else:
        resultado = chamar(
            "module.create",
            {"id": modulo_id, "relative_path": caminho_relativo, "status": 1},
            token,
        )
        print(f"[ok] módulo do widget Insights cadastrado (id {resultado['moduleids'][0]}) e habilitado")


def configurar_templates(token):
    arquivos = sorted(glob.glob(os.path.join(PASTA_TEMPLATES, "*.yaml")))
    if not arquivos:
        print(f"[aviso] nenhum template em {PASTA_TEMPLATES}")
        return

    for caminho in arquivos:
        with open(caminho, encoding="utf-8") as f:
            fonte = f.read()

        chamar(
            "configuration.import",
            {
                "format": "yaml",
                "source": fonte,
                "rules": {
                    "template_groups": {"createMissing": True},
                    "templates": {"createMissing": True, "updateExisting": True},
                    "items": {"createMissing": True, "updateExisting": True},
                    "triggers": {"createMissing": True, "updateExisting": True},
                },
            },
            token,
        )
        print(f"[ok] template importado: {os.path.basename(caminho)}")


def main():
    if len(SENHA_NOVA) < 12:
        sys.exit("[erro] ZBX_ADMIN_PASSWORD vazia ou curta demais. Rode 'make env' para gerar o .env.")

    esperar_api()
    token = entrar()

    try:
        configurar_proxy(token)
        try:
            configurar_widget_insights(token)
        except ErroApi as erro:
            # Não trava o bootstrap: dá pra habilitar à mão depois em
            # Administration > General > Modules > Scan directory.
            print(f"[aviso] não consegui habilitar o widget Insights pela API: {erro}")
        try:
            configurar_templates(token)
        except ErroApi as erro:
            # Idem: dá pra importar à mão depois em Data collection > Templates > Import.
            print(f"[aviso] não consegui importar os templates pela API: {erro}")
    finally:
        try:
            chamar("user.logout", [], token)
        except Exception:
            pass

    print("[ok] pronto. O proxy leva até cerca de 1 minuto para receber a configuração e começar a gravar.")


if __name__ == "__main__":
    main()

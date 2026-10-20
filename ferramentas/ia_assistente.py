"""Ponte entre o Zabbix e a API do Gemini (análise de problemas e painéis).

Dois usos, nos moldes do artigo "Integrating Zabbix with Gemini":

  POST /analisar        - chamado pelo media type webhook do Zabbix quando
                           uma trigger de anomalia dispara um problema.
                           Monta o contexto do evento, pergunta ao Gemini a
                           causa provável, e anexa a resposta ao próprio
                           evento via event.acknowledge (aparece no painel
                           de Problemas).
  POST /analisar-painel  - chamado pela Action PHP do widget "Insights" do
                           dashboard (botão "Analisar"). Recebe os
                           itens/hosts/período visíveis no painel e devolve
                           a análise direto na resposta HTTP (o widget
                           mostra sem precisar tocar na API do Zabbix).

A GEMINI_API_KEY só existe aqui (processo server-side); nunca chega ao
navegador. Só biblioteca padrão — sem framework web.
"""

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTA = int(os.environ.get("IA_PORT", "8090"))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODELO = os.environ.get("GEMINI_MODELO", "gemini-2.0-flash")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODELO}:generateContent"
)
GEMINI_TIMEOUT_S = int(os.environ.get("GEMINI_TIMEOUT_S", "30"))

ZBX_API_URL = os.environ.get("ZBX_API_URL", "http://zabbix-web:8080/api_jsonrpc.php")
ZBX_IA_API_TOKEN = os.environ.get("ZBX_IA_API_TOKEN", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("ia-assistente")


class ErroGemini(Exception):
    pass


def perguntar_gemini(prompt):
    if not GEMINI_API_KEY:
        raise ErroGemini("GEMINI_API_KEY não configurada")

    corpo = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    requisicao = urllib.request.Request(
        f"{GEMINI_URL}?key={GEMINI_API_KEY}",
        data=corpo,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(requisicao, timeout=GEMINI_TIMEOUT_S) as resposta:
            dados = json.loads(resposta.read().decode())
    except (urllib.error.URLError, OSError) as erro:
        raise ErroGemini(f"falha ao chamar a API do Gemini: {erro}") from erro

    try:
        return dados["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as erro:
        raise ErroGemini(f"resposta inesperada do Gemini: {dados}") from erro


def zabbix_chamar(metodo, params):
    corpo = json.dumps({"jsonrpc": "2.0", "method": metodo, "params": params, "id": 1}).encode()
    cabecalhos = {
        "Content-Type": "application/json-rpc",
        "Authorization": f"Bearer {ZBX_IA_API_TOKEN}",
    }
    requisicao = urllib.request.Request(ZBX_API_URL, data=corpo, headers=cabecalhos)
    with urllib.request.urlopen(requisicao, timeout=20) as resposta:
        dados = json.loads(resposta.read().decode())
    if "error" in dados:
        erro = dados["error"]
        raise RuntimeError(f"{metodo}: {erro.get('message', '')} {erro.get('data', '')}".strip())
    return dados["result"]


def prompt_evento(contexto):
    return (
        "Você é um assistente de observabilidade analisando um problema no "
        "Zabbix de um laboratório de telemetria (ambiente de estudo, não é "
        "produção real). Explique em até 4 frases, em português, a causa "
        "provável e uma sugestão de próximo passo.\n\n"
        f"Host: {contexto.get('host', '?')}\n"
        f"Trigger: {contexto.get('trigger', '?')}\n"
        f"Severidade: {contexto.get('severidade', '?')}\n"
        f"Valores recentes: {json.dumps(contexto.get('valores', {}), ensure_ascii=False)}\n"
    )


def prompt_painel(contexto, pergunta):
    base = (
        "Você é um assistente de observabilidade resumindo um painel do "
        "Zabbix (laboratório de estudo). Responda em português, em até 6 "
        "frases.\n\n"
        f"Dados do painel: {json.dumps(contexto, ensure_ascii=False)}\n"
    )
    if pergunta:
        base += f"\nPergunta de quem está olhando o painel: {pergunta}\n"
    else:
        base += "\nResuma o que os dados indicam e aponte qualquer anomalia visível.\n"
    return base


class Manipulador(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _corpo_json(self):
        tamanho = int(self.headers.get("Content-Length", 0) or 0)
        bruto = self.rfile.read(tamanho) if tamanho else b"{}"
        try:
            return json.loads(bruto)
        except json.JSONDecodeError:
            return {}

    def _responder(self, status, corpo):
        bruto = json.dumps(corpo, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(bruto)))
        self.end_headers()
        self.wfile.write(bruto)

    def do_GET(self):
        if self.path == "/saude":
            self._responder(200, {"ok": True})
        else:
            self._responder(404, {"erro": "rota desconhecida"})

    def do_POST(self):
        if self.path == "/analisar":
            self._analisar_evento()
        elif self.path == "/analisar-painel":
            self._analisar_painel()
        else:
            self._responder(404, {"erro": "rota desconhecida"})

    def _analisar_evento(self):
        contexto = self._corpo_json()
        try:
            resposta = perguntar_gemini(prompt_evento(contexto))
        except ErroGemini as erro:
            log.error("%s", erro)
            self._responder(502, {"ok": False, "erro": str(erro)})
            return

        evento_id = contexto.get("evento_id")
        if evento_id and ZBX_IA_API_TOKEN:
            try:
                # action=4 (bit ZBX_PROBLEM_UPDATE_MESSAGE) só anexa uma
                # mensagem, sem reconhecer/fechar o problema. Confirmar esse
                # valor contra a documentação da API na build alpine-trunk
                # usada (é uma versão de desenvolvimento do 8.0).
                zabbix_chamar(
                    "event.acknowledge",
                    {"eventids": [evento_id], "message": f"[IA] {resposta}", "action": 4},
                )
            except (RuntimeError, urllib.error.URLError, OSError) as erro:
                log.error("falha ao anexar a resposta no evento %s: %s", evento_id, erro)

        log.info("evento %s analisado", evento_id)
        self._responder(200, {"ok": True, "resposta": resposta})

    def _analisar_painel(self):
        corpo = self._corpo_json()
        try:
            resposta = perguntar_gemini(prompt_painel(corpo.get("contexto", {}), corpo.get("pergunta", "")))
        except ErroGemini as erro:
            log.error("%s", erro)
            self._responder(502, {"erro": str(erro)})
            return

        self._responder(200, {"resposta": resposta})


def main():
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY vazia: as chamadas ao Gemini vão falhar até configurar")

    servidor = ThreadingHTTPServer(("0.0.0.0", PORTA), Manipulador)
    log.info("ia-assistente escutando em :%d (modelo %s)", PORTA, GEMINI_MODELO)
    servidor.serve_forever()


if __name__ == "__main__":
    main()

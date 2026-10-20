"""Serviço "investigador": recebe eventos do Zabbix, coleta evidências
somente leitura, mascara, (opcionalmente) consulta o Gemini e devolve o
resultado ao evento (event.acknowledge action=4) e ao widget.

Endpoints (todos com "Authorization: Bearer <INVESTIGADOR_TOKEN>"; /saude sem
token devolve só {"ok": true}):
  POST /evento               webhook do Zabbix; enfileira e responde < 1 s
  GET  /relatorio/<eventid>  JSON (padrão) ou HTML (?formato=html)
  POST /aprofundar/<eventid> modo profundo (assíncrono; ?forcar=1 refaz)
  POST /analisar-painel      compatível com o widget Insights
  GET  /saude                estado, modo, orçamento

Sem GEMINI_API_KEY o serviço fica em modo "somente evidências".
O serviço NUNCA executa ações sugeridas pelo modelo: a única escrita é
event.acknowledge com action=4 (mensagem) no próprio evento.
"""

import hashlib
import hmac
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .armazenamento import Armazenamento
from .evidencias import EVENTID_VALIDO, SERVICO_VALIDO, ClickHouse, Coletor, ErroColeta, ZabbixLeitura
from .gemini import ClienteGemini, ErroGemini, MODELO_RE, montar_prompt, validar_saida
from .mascaramento import Mascarador, limitar_linhas
from .relatorio import (LINK_GEMINI_APP, contexto_para_ia, renderizar_html, resumo_heuristico, texto_ack,
                        texto_copiavel)

log = logging.getLogger("investigador")
auditoria_log = logging.getLogger("investigador.auditoria")

MODELOS_DESLIGADOS = re.compile(r"^gemini-(1\.|2\.0)")


def _env_int(nome, padrao):
    try:
        return int(os.environ.get(nome, padrao))
    except ValueError:
        return padrao


def _modelo(nome, padrao):
    nome = (nome or "").strip()
    if not nome or not MODELO_RE.match(nome) or MODELOS_DESLIGADOS.match(nome):
        return padrao
    return nome


class Config:
    def __init__(self, env=None):
        e = os.environ if env is None else env
        g = e.get
        self.bind = g("INVESTIGADOR_BIND", "127.0.0.1")
        self.porta = int(g("INVESTIGADOR_PORTA", "8091"))
        self.token = g("INVESTIGADOR_TOKEN", "")
        self.db = g("INVESTIGADOR_DB", "/dados/investigador.sqlite3")
        self.chave = g("GEMINI_API_KEY", "")
        self.gemini_base = g("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com")
        self.modelo_pre = _modelo(g("GEMINI_MODELO_PRE"), "gemini-3.5-flash-lite")
        self.modelo_profundo = _modelo(g("GEMINI_MODELO_PROFUNDO"), "gemini-3.8-flash")
        self.gemini_timeout = min(int(g("GEMINI_TIMEOUT_S", "30")), 55)
        self.ch_url = g("CLICKHOUSE_URL", "http://clickhouse:8123")
        self.ch_usuario = g("CLICKHOUSE_RO_USER", "investigador_ro")
        self.ch_senha = g("CLICKHOUSE_RO_PASSWORD", "")
        self.zbx_url = g("ZBX_API_URL", "http://zabbix-web:8080/api_jsonrpc.php")
        self.zbx_token = g("ZBX_INVESTIGADOR_TOKEN", "")
        self.url_frontend = g("ZBX_FRONTEND_URL", "http://127.0.0.1:8080")
        self.req_por_minuto = int(g("INVESTIGADOR_REQ_POR_MINUTO", "120"))
        self.max_eventos_hora = int(g("INVESTIGADOR_MAX_EVENTOS_HORA", "60"))
        self.max_llm_hora = int(g("INVESTIGADOR_MAX_LLM_HORA", "20"))
        self.max_aprofundar_hora = int(g("INVESTIGADOR_MAX_APROFUNDAR_HORA", "10"))
        self.orcamento_dia = int(g("INVESTIGADOR_ORCAMENTO_TOKENS_DIA", "300000"))
        self.workers = max(1, min(int(g("INVESTIGADOR_WORKERS", "1")), 4))
        self.dominios_internos = [d for d in g("INVESTIGADOR_DOMINIOS_INTERNOS", "").split(",") if d.strip()] or None
        self.hosts_mascarar = [h.strip() for h in g("INVESTIGADOR_HOSTS_MASCARAR", "").split(",") if h.strip()]
        self.ack = g("INVESTIGADOR_ACK", "1") == "1"
        self.limites = {"pre": (120, 24000, 1024), "profundo": (400, 100000, 4096)}  # linhas, caracteres, saída

    @property
    def llm_ligado(self):
        return bool(self.chave)


class Limitador:
    def __init__(self, maximo, janela=60):
        self.maximo, self.janela, self.marcas, self.trava = maximo, janela, [], threading.Lock()

    def permitir(self):
        agora = time.monotonic()
        with self.trava:
            self.marcas = [t for t in self.marcas if agora - t < self.janela]
            if len(self.marcas) >= self.maximo:
                return False
            self.marcas.append(agora)
            return True


class Servico:
    def __init__(self, cfg, coletor=None, gemini=None, zbx=None, armazenamento=None):
        self.cfg = cfg
        self.db = armazenamento or Armazenamento(cfg.db)
        self.zbx = zbx or ZabbixLeitura(cfg.zbx_url, cfg.zbx_token)
        self.coletor = coletor or Coletor(ClickHouse(cfg.ch_url, cfg.ch_usuario, cfg.ch_senha), self.zbx, cfg.url_frontend)
        self.gemini = gemini or (ClienteGemini(cfg.chave, cfg.gemini_base, cfg.gemini_timeout) if cfg.llm_ligado else None)
        self.fila = queue.Queue(maxsize=100)
        self.limitador = Limitador(cfg.req_por_minuto)
        self.falhas_seguidas = 0
        self.disjuntor_ate = 0.0
        self.trava = threading.Lock()

    # ---- workers -------------------------------------------------------------------
    def iniciar_workers(self):
        for i in range(self.cfg.workers):
            threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True).start()

    def _loop(self):
        while True:
            tarefa = self.fila.get()
            try:
                self.processar(**tarefa)
            except Exception:  # nunca derruba o worker; detalhes ficam fora do log por segurança
                log.exception("falha inesperada ao processar evento %s", tarefa.get("eventid"))
                self.db.atualizar(tarefa["eventid"], tarefa["modo"], "erro",
                                  {"eventid": tarefa["eventid"], "modo": tarefa["modo"], "erro": "falha interna"})
            finally:
                self.fila.task_done()

    def enfileirar(self, **tarefa):
        try:
            self.fila.put_nowait(tarefa)
            return True
        except queue.Full:
            return False

    # ---- decisão de usar o LLM ---------------------------------------------------------
    def motivo_sem_llm(self, estimativa):
        if not self.gemini:
            return "GEMINI_API_KEY não configurada"
        if time.time() < self.disjuntor_ate:
            return "Gemini falhou repetidamente; pausa automática de 10 min"
        if self.db.chamadas_llm_ultima_hora() >= self.cfg.max_llm_hora:
            return f"teto de {self.cfg.max_llm_hora} chamadas à IA por hora atingido"
        usados, _ = self.db.tokens_hoje()
        if usados + estimativa > self.cfg.orcamento_dia:
            return f"orçamento diário de {self.cfg.orcamento_dia} tokens esgotado"
        return ""

    # ---- análise -----------------------------------------------------------------------
    def processar(self, eventid, modo, servico="", pergunta="", origem="webhook"):
        inicio = time.time()
        self.db.atualizar(eventid, modo, "processando")
        try:
            coleta = self.coletor.coletar(eventid, modo, servico)
        except ErroColeta as e:
            rel = {"eventid": eventid, "modo": modo, "gerado_em": _agora(), "modo_ia": "somente-evidencias",
                   "motivo_sem_llm": "coleta falhou", "erro": str(e)[:200], "evidencias": [],
                   "resumo_heuristico": f"Não foi possível coletar evidências: {str(e)[:200]}"}
            self.db.atualizar(eventid, modo, "erro", rel)
            self.db.auditar(eventid=eventid, modo=modo, origem=origem, status="erro_coleta",
                            duracao_ms=int((time.time() - inicio) * 1000))
            auditoria_log.info(json.dumps({"eventid": eventid, "modo": modo, "status": "erro_coleta"}))
            return rel

        m = Mascarador(self.cfg.dominios_internos, self.cfg.hosts_mascarar)
        ev_masc = []
        for ev in coleta["evidencias"]:
            ev_masc.append({"id": ev["id"], "tipo": ev["tipo"], "titulo": m.mascarar(ev["titulo"], 200),
                            "dados": m.mascarar_estrutura(ev["dados"])})
        max_l, max_c, max_saida = self.cfg.limites[modo]
        linhas, cortadas = limitar_linhas(contexto_para_ia(ev_masc), max_l, max_c)
        pergunta_masc = m.mascarar(pergunta, 300) if pergunta else ""
        prompt = montar_prompt("\n".join(linhas), modo, pergunta_masc)
        nao_verificado = list(coleta["nao_verificado"])
        if cortadas:
            nao_verificado.append(f"{cortadas} evidência(s) cortadas pelo teto de contexto")

        # Links montados AQUI (IDs validados), nunca pelo modelo.
        for ev_orig, ev_m in zip(coleta["evidencias"], ev_masc):
            ev_m["links"] = [u for u in (self.coletor.link_trace(t) for t in ev_orig["refs"].get("traces", [])[:3]) if u]

        rel = {
            "eventid": eventid, "modo": modo, "gerado_em": _agora(), "servico": coleta["servico"],
            "evidencias": ev_masc, "nao_verificado": nao_verificado, "mascaramento": m.contagens(),
            "resumo_heuristico": resumo_heuristico(ev_masc), "texto_copiavel": texto_copiavel(ev_masc, modo),
            "link_gemini_app": LINK_GEMINI_APP, "modo_ia": "somente-evidencias", "analise": None,
            "acoes_executadas": [],
        }
        estimativa = len(prompt) // 4 + max_saida
        motivo = self.motivo_sem_llm(estimativa)
        modelo = self.cfg.modelo_pre if modo == "pre" else self.cfg.modelo_profundo
        uso = {"entrada": 0, "saida": 0, "total": 0}
        status_audit = "somente_evidencias"
        if not motivo:
            try:
                texto, uso = self.gemini.gerar(modelo, prompt, max_saida)
                self.db.somar_tokens(uso["total"] or estimativa)
                rel["analise"] = validar_saida(texto, [e["id"] for e in ev_masc], modo)
                rel["modo_ia"], rel["modelo"] = "llm", modelo
                self.falhas_seguidas = 0
                status_audit = "llm_ok"
            except ErroGemini as e:
                self.falhas_seguidas += 1
                if self.falhas_seguidas >= 3:
                    self.disjuntor_ate = time.time() + 600
                motivo = f"falha ao consultar a IA ({e})"
                status_audit = "llm_erro"
                log.warning("Gemini falhou para o evento %s: %s", eventid, e)
        rel["motivo_sem_llm"] = motivo
        rel["url_relatorio"] = f"{self.cfg.url_frontend.rstrip('/')}/zabbix.php?action=investigador.relatorio&eventid={eventid}"

        if self.cfg.ack:
            try:
                self.zbx.chamar("event.acknowledge", {"eventids": [eventid], "action": 4,
                                                      "message": texto_ack(rel, rel["url_relatorio"])})
                rel["ack"] = "ok"
            except ErroColeta as e:
                rel["ack"] = f"falhou: {str(e)[:120]}"
        self.db.atualizar(eventid, modo, "pronto", rel, coleta["servico"])
        auditoria = {"eventid": eventid, "modo": modo, "origem": origem,
                     "modelo": modelo if status_audit != "somente_evidencias" else "",
                     "tokens_entrada": uso["entrada"], "tokens_saida": uso["saida"],
                     "duracao_ms": int((time.time() - inicio) * 1000), "mascarados": m.contagens(),
                     "status": status_audit, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
        self.db.auditar(**auditoria)
        auditoria_log.info(json.dumps(auditoria, ensure_ascii=False))
        return rel

    def analisar_painel(self, contexto, pergunta):
        m = Mascarador(self.cfg.dominios_internos, self.cfg.hosts_mascarar)
        itens = m.mascarar_estrutura([c for c in contexto if isinstance(c, dict)][:30], 200)
        sev = {}
        for c in itens:
            sev[str(c.get("severidade", "?"))] = sev.get(str(c.get("severidade", "?")), 0) + 1
        base = f"{len(itens)} problema(s) recentes; por severidade: {json.dumps(sev)}."
        evid = [{"id": f"E{i + 1}", "tipo": "zabbix.problema", "titulo": "Problema do painel", "dados": c}
                for i, c in enumerate(itens)]
        prompt = montar_prompt("\n".join(contexto_para_ia(evid)), "pre", m.mascarar(pergunta, 300) if pergunta else "")
        motivo = self.motivo_sem_llm(len(prompt) // 4 + 1024)
        if motivo:
            return {"resposta": f"Modo somente evidências ({motivo}). {base}", "modo": "somente-evidencias"}
        try:
            texto, uso = self.gemini.gerar(self.cfg.modelo_pre, prompt, 1024)
            self.db.somar_tokens(uso["total"] or len(prompt) // 4)
            a = validar_saida(texto, [e["id"] for e in evid], "pre")
        except ErroGemini as e:
            return {"resposta": f"Falha ao consultar a IA ({e}). {base}", "modo": "somente-evidencias"}
        finally:
            self.db.auditar(eventid="painel", modo="painel", origem="widget-insights", modelo=self.cfg.modelo_pre,
                            mascarados=m.contagens(), status="painel")
        partes = [a["resumo"]] + [f"{h['rank']}. {h['titulo']} ({h['confianca']})" for h in a["hipoteses"]]
        return {"resposta": " ".join(partes), "modo": "llm"}

    def saude(self, detalhada):
        if not detalhada:
            return {"ok": True}
        usados, chamadas = self.db.tokens_hoje()
        return {"ok": True, "modo": "llm" if self.gemini else "somente-evidencias",
                "modelo_pre": self.cfg.modelo_pre, "modelo_profundo": self.cfg.modelo_profundo,
                "fila": self.fila.qsize(), "tokens_hoje": usados, "chamadas_hoje": chamadas,
                "orcamento_dia": self.cfg.orcamento_dia, "chamadas_llm_ultima_hora": self.db.chamadas_llm_ultima_hora(),
                "disjuntor_aberto": time.time() < self.disjuntor_ate,
                "zabbix_token": bool(self.cfg.zbx_token), "clickhouse_senha": bool(self.cfg.ch_senha)}


def _agora():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def criar_manipulador(svc):
    class Manipulador(BaseHTTPRequestHandler):
        server_version = "investigador"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        def _autorizado(self):
            cab = self.headers.get("Authorization", "")
            if not svc.cfg.token or not cab.startswith("Bearer "):
                return False
            return hmac.compare_digest(cab[7:].strip().encode(), svc.cfg.token.encode())

        def _corpo(self):
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                return None
            if n > 128 * 1024:
                return None
            bruto = self.rfile.read(n) if n else b"{}"
            try:
                d = json.loads(bruto or b"{}")
            except (ValueError, UnicodeDecodeError):
                return None
            return d if isinstance(d, dict) else None

        def _json(self, status, corpo):
            bruto = json.dumps(corpo, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(bruto)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(bruto)

        def _html(self, texto):
            bruto = texto.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(bruto)))
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(bruto)

        def _preambulo(self):
            if not self._autorizado():
                self._json(401, {"erro": "não autorizado"})
                return False
            if not svc.limitador.permitir():
                self._json(429, {"erro": "limite de requisições atingido"})
                return False
            return True

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path == "/saude":
                self._json(200, svc.saude(self._autorizado()))
                return
            m = re.fullmatch(r"/relatorio/(\d{1,20})", u.path)
            if not m:
                self._json(404, {"erro": "rota desconhecida"})
                return
            if not self._preambulo():
                return
            eventid = m.group(1)
            dados = {modo: svc.db.obter(eventid, modo) for modo in ("pre", "profundo")}
            if not any(dados.values()):
                self._json(404, {"erro": "sem análise para este evento", "eventid": eventid,
                                 "modo_servico": "llm" if svc.gemini else "somente-evidencias"})
                return
            if urllib.parse.parse_qs(u.query).get("formato") == ["html"]:
                self._html(renderizar_html(eventid, {k: (v or {}).get("relatorio") for k, v in dados.items()}))
                return
            self._json(200, {"eventid": eventid, "modo_servico": "llm" if svc.gemini else "somente-evidencias",
                             **{k: ({"status": v["status"], "relatorio": v["relatorio"]} if v else None)
                                for k, v in dados.items()}})

        def do_POST(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path not in ("/evento", "/analisar-painel") and not re.fullmatch(r"/aprofundar/\d{1,20}", u.path):
                self._json(404, {"erro": "rota desconhecida"})
                return
            if not self._preambulo():
                return
            corpo = self._corpo()
            if corpo is None:
                self._json(400, {"erro": "corpo JSON inválido"})
                return
            if u.path == "/evento":
                self._evento(corpo)
            elif u.path == "/analisar-painel":
                ctx = corpo.get("contexto") if isinstance(corpo.get("contexto"), list) else []
                self._json(200, svc.analisar_painel(ctx, str(corpo.get("pergunta") or "")[:300]))
            else:
                self._aprofundar(u.path.rsplit("/", 1)[1], corpo, urllib.parse.parse_qs(u.query).get("forcar") == ["1"])

        def _evento(self, corpo):
            eventid = str(corpo.get("eventid", "")).strip()
            if not EVENTID_VALIDO.match(eventid):
                self._json(400, {"erro": "eventid inválido"})
                return
            if str(corpo.get("valor", "1")) == "0":
                self._json(200, {"status": "ignorado (recuperação)", "tags": {}})
                return
            servico = str(corpo.get("servico", "") or "")
            servico = servico if SERVICO_VALIDO.match(servico) and not servico.startswith("{$") else ""
            tags = {"rca": "pre-analise", "rca.id": eventid}
            if servico:
                tags["rca.servico"] = servico
            if svc.db.obter(eventid, "pre"):
                self._json(200, {"status": "duplicado", "tags": tags})
                return
            if svc.db.contar_desde("pre", 3600) >= svc.cfg.max_eventos_hora:
                self._json(200, {"status": "limitado", "tags": {"rca": "limitado"}})
                return
            if not svc.db.reservar(eventid, "pre", servico):
                self._json(200, {"status": "duplicado", "tags": tags})
                return
            if not svc.enfileirar(eventid=eventid, modo="pre", servico=servico, origem="webhook"):
                svc.db.atualizar(eventid, "pre", "erro", {"eventid": eventid, "modo": "pre", "erro": "fila cheia"})
                self._json(503, {"status": "fila cheia", "tags": {"rca": "fila-cheia"}})
                return
            self._json(202, {"status": "na_fila", "modo": "llm" if svc.gemini else "somente-evidencias", "tags": tags})

        def _aprofundar(self, eventid, corpo, forcar):
            existente = svc.db.obter(eventid, "profundo")
            if existente and existente["status"] in ("na_fila", "processando"):
                self._json(202, {"status": existente["status"]})
                return
            if existente and existente["status"] == "pronto" and not forcar:
                self._json(200, {"status": "pronto (cache)"})
                return
            if svc.db.contar_desde("profundo", 3600) >= svc.cfg.max_aprofundar_hora and not existente:
                self._json(429, {"erro": f"teto de {svc.cfg.max_aprofundar_hora} aprofundamentos por hora"})
                return
            if existente:
                svc.db.rearmar(eventid, "profundo")
            else:
                svc.db.reservar(eventid, "profundo")
            pergunta = str(corpo.get("pergunta") or "")[:300]
            servico = str(corpo.get("servico") or "")
            servico = servico if SERVICO_VALIDO.match(servico) else (svc.db.obter(eventid, "pre") or {}).get("servico", "")
            if not svc.enfileirar(eventid=eventid, modo="profundo", servico=servico or "", pergunta=pergunta,
                                  origem="widget"):
                svc.db.atualizar(eventid, "profundo", "erro", {"eventid": eventid, "modo": "profundo", "erro": "fila cheia"})
                self._json(503, {"erro": "fila cheia"})
                return
            self._json(202, {"status": "na_fila", "modo": "llm" if svc.gemini else "somente-evidencias"})

    return Manipulador


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config()
    if not cfg.token:
        log.warning("INVESTIGADOR_TOKEN vazio: todas as rotas autenticadas responderão 401")
    if not cfg.zbx_token:
        log.warning("ZBX_INVESTIGADOR_TOKEN vazio: coleta no Zabbix e ack vão falhar (rode make investigador-config)")
    svc = Servico(cfg)
    svc.iniciar_workers()
    servidor = ThreadingHTTPServer((cfg.bind, cfg.porta), criar_manipulador(svc))
    servidor.daemon_threads = True
    log.info("investigador em %s:%d, modo %s (pré: %s, profundo: %s)", cfg.bind, cfg.porta,
             "LLM" if svc.gemini else "somente evidências", cfg.modelo_pre, cfg.modelo_profundo)
    servidor.serve_forever()


if __name__ == "__main__":
    main()

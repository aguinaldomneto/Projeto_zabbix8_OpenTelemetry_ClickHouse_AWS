"""Cache de análises, orçamento de tokens e auditoria (SQLite, stdlib).

Nada sensível é gravado aqui: os relatórios guardam apenas evidências JÁ
mascaradas; a auditoria guarda contagens, modelo, tokens e o SHA-256 do prompt.
"""

import json
import sqlite3
import threading
import time

ESQUEMA = """
CREATE TABLE IF NOT EXISTS analises (
  eventid TEXT NOT NULL, modo TEXT NOT NULL, status TEXT NOT NULL,
  criado REAL NOT NULL, atualizado REAL NOT NULL, servico TEXT DEFAULT '',
  relatorio TEXT DEFAULT '', PRIMARY KEY (eventid, modo));
CREATE TABLE IF NOT EXISTS uso (dia TEXT PRIMARY KEY, tokens INTEGER NOT NULL DEFAULT 0, chamadas INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS auditoria (
  ts REAL NOT NULL, eventid TEXT, modo TEXT, origem TEXT, modelo TEXT, tokens_entrada INTEGER, tokens_saida INTEGER,
  duracao_ms INTEGER, mascarados TEXT, status TEXT, prompt_sha256 TEXT);
CREATE INDEX IF NOT EXISTS auditoria_ts ON auditoria (ts);
"""


class Armazenamento:
    def __init__(self, caminho):
        self.trava = threading.Lock()
        self.con = sqlite3.connect(caminho, check_same_thread=False, isolation_level=None)
        self.con.execute("PRAGMA journal_mode=WAL") if caminho != ":memory:" else None
        self.con.executescript(ESQUEMA)

    # ---- análises ---------------------------------------------------------------
    def reservar(self, eventid, modo, servico=""):
        """Cria a análise em 'na_fila'. Devolve False se já existir (dedupe atômico)."""
        agora = time.time()
        with self.trava:
            cur = self.con.execute(
                "INSERT OR IGNORE INTO analises (eventid, modo, status, criado, atualizado, servico) VALUES (?,?,?,?,?,?)",
                (eventid, modo, "na_fila", agora, agora, servico))
            return cur.rowcount == 1

    def rearmar(self, eventid, modo):
        with self.trava:
            self.con.execute("UPDATE analises SET status='na_fila', atualizado=? WHERE eventid=? AND modo=?",
                             (time.time(), eventid, modo))

    def atualizar(self, eventid, modo, status, relatorio=None, servico=None):
        with self.trava:
            self.con.execute(
                "UPDATE analises SET status=?, atualizado=?, relatorio=COALESCE(?, relatorio), servico=COALESCE(?, servico) "
                "WHERE eventid=? AND modo=?",
                (status, time.time(), json.dumps(relatorio, ensure_ascii=False) if relatorio is not None else None,
                 servico, eventid, modo))

    def obter(self, eventid, modo):
        with self.trava:
            linha = self.con.execute("SELECT status, criado, atualizado, servico, relatorio FROM analises "
                                     "WHERE eventid=? AND modo=?", (eventid, modo)).fetchone()
        if not linha:
            return None
        return {"status": linha[0], "criado": linha[1], "atualizado": linha[2], "servico": linha[3],
                "relatorio": json.loads(linha[4]) if linha[4] else None}

    def contar_desde(self, modo, segundos):
        with self.trava:
            return self.con.execute("SELECT count(*) FROM analises WHERE modo=? AND criado>=?",
                                    (modo, time.time() - segundos)).fetchone()[0]

    # ---- orçamento ---------------------------------------------------------------
    @staticmethod
    def _dia():
        return time.strftime("%Y-%m-%d")

    def tokens_hoje(self):
        with self.trava:
            linha = self.con.execute("SELECT tokens, chamadas FROM uso WHERE dia=?", (self._dia(),)).fetchone()
        return (linha[0], linha[1]) if linha else (0, 0)

    def somar_tokens(self, tokens):
        with self.trava:
            self.con.execute("INSERT INTO uso (dia, tokens, chamadas) VALUES (?,?,1) ON CONFLICT(dia) DO UPDATE SET "
                             "tokens=tokens+excluded.tokens, chamadas=chamadas+1", (self._dia(), int(tokens)))

    def chamadas_llm_ultima_hora(self):
        with self.trava:
            return self.con.execute("SELECT count(*) FROM auditoria WHERE modelo != '' AND ts >= ?",
                                    (time.time() - 3600,)).fetchone()[0]

    # ---- auditoria -------------------------------------------------------------------
    def auditar(self, **campos):
        with self.trava:
            self.con.execute(
                "INSERT INTO auditoria (ts, eventid, modo, origem, modelo, tokens_entrada, tokens_saida, duracao_ms, "
                "mascarados, status, prompt_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), campos.get("eventid", ""), campos.get("modo", ""), campos.get("origem", ""),
                 campos.get("modelo", ""), campos.get("tokens_entrada", 0), campos.get("tokens_saida", 0),
                 campos.get("duracao_ms", 0), json.dumps(campos.get("mascarados", {})), campos.get("status", ""),
                 campos.get("prompt_sha256", "")))

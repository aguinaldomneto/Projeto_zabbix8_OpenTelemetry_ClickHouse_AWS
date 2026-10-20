import json
import re
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from investigador.evidencias import CONSULTAS, TIPOS_PARAMETRO, ClickHouse, ErroColeta, validar_parametros


class TestConsultasFixas(unittest.TestCase):
    def test_sem_formatacao_de_string(self):
        for nome, sql in CONSULTAS.items():
            self.assertNotIn("%s", sql, nome)
            self.assertNotRegex(sql, r"\{\s*\}", nome)  # nada de str.format vazio
            for ph in re.findall(r"\{(\w+):(\w+)\}", sql):
                self.assertEqual(TIPOS_PARAMETRO[ph[0]], ph[1], f"{nome}: tipo do parâmetro {ph[0]}")

    def test_somente_select_com_limite(self):
        for nome, sql in CONSULTAS.items():
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"), nome)
            self.assertIn("LIMIT", sql.upper(), nome)
            for proibido in ("INSERT", "ALTER", "DROP", "TRUNCATE", "CREATE", "SYSTEM ", "GRANT", "OPTIMIZE"):
                self.assertNotIn(proibido, sql.upper(), nome)
            self.assertIn("zabbix.otel_", sql, nome)

    def test_validacao_parametros(self):
        self.assertEqual(validar_parametros({"svc": "pedidos", "ini": 10, "lim": 5}),
                         {"svc": "pedidos", "ini": "10", "lim": "5"})
        for ruim in ("x' OR 1=1 --", "a b", "", "a" * 200, "pedidos;DROP"):
            with self.assertRaises(ErroColeta):
                validar_parametros({"svc": ruim})
        with self.assertRaises(ErroColeta):
            validar_parametros({"lim": 70000})
        with self.assertRaises(ErroColeta):
            validar_parametros({"sql": "SELECT 1"})


class _FakeCH(BaseHTTPRequestHandler):
    recebido = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        _FakeCH.recebido = {"corpo": self.rfile.read(n).decode(), "path": self.path,
                            "usuario": self.headers.get("X-ClickHouse-User"), "tem_senha": bool(self.headers.get("X-ClickHouse-Key"))}
        b = json.dumps({"data": [{"spans": "10", "erros": "2"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class TestClienteClickHouse(unittest.TestCase):
    def test_parametros_vao_na_url_e_nao_no_sql(self):
        srv = HTTPServer(("127.0.0.1", 0), _FakeCH)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        ch = ClickHouse(f"http://127.0.0.1:{srv.server_port}", "investigador_ro", "s3nha")
        dados = ch.consultar("resumo_spans", svc="pedidos", ini=100, fim=200)
        srv.server_close()
        self.assertEqual(dados[0]["spans"], "10")
        r = _FakeCH.recebido
        self.assertEqual(r["corpo"], CONSULTAS["resumo_spans"] + " FORMAT JSON")
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(r["path"]).query)
        self.assertEqual(qs["param_svc"], ["pedidos"])
        self.assertEqual(qs["param_ini"], ["100"])
        self.assertEqual(r["usuario"], "investigador_ro")
        self.assertTrue(r["tem_senha"])
        self.assertNotIn("s3nha", r["path"])

    def test_consulta_desconhecida(self):
        with self.assertRaises(ErroColeta):
            ClickHouse("http://127.0.0.1:1", "u", "p").consultar("DROP TABLE x")


if __name__ == "__main__":
    unittest.main()

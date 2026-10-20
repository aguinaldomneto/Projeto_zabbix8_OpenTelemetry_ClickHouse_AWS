import unittest

from investigador.mascaramento import Mascarador, cnpj_valido, cpf_valido, limitar_linhas, luhn_valido

# Valores sintéticos (nenhum é segredo real).
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
GHP = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
SK = "sk-" + "proj-abcdefghijklmnopqrstuvwxyz012345"
GOOGLE = "AIza" + "SyA1234567890abcdefghijklmnopqrstu"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


class TestMascaramento(unittest.TestCase):
    def setUp(self):
        self.m = Mascarador()

    def test_ipv4_estavel(self):
        s = self.m.mascarar("de 10.0.0.12 para 192.168.1.5 e de novo 10.0.0.12")
        self.assertEqual(s, "de <IP_1> para <IP_2> e de novo <IP_1>")
        self.assertEqual(self.m.contagens()["IP"], 3)

    def test_ipv4_invalido_e_versao_nao_mascarados(self):
        s = self.m.mascarar("versão 1.2.3 e 999.1.1.1")
        self.assertIn("1.2.3", s)
        self.assertIn("999.1.1.1", s)

    def test_ipv6(self):
        s = self.m.mascarar("cliente 2001:db8::1 e fe80::1%eth0 às 12:30:45")
        self.assertNotIn("2001:db8::1", s)
        self.assertIn("<IP_1>", s)
        self.assertIn("12:30:45", s)

    def test_email(self):
        s = self.m.mascarar("falha para Fulano.Silva@empresa.com.br")
        self.assertEqual(s, "falha para <EMAIL_1>")

    def test_cpf_cnpj(self):
        s = self.m.mascarar("cpf 529.982.247-25 cnpj 11.222.333/0001-81 e cpf puro 52998224725")
        self.assertNotIn("529.982.247-25", s)
        self.assertNotIn("11.222.333/0001-81", s)
        self.assertIn("<CPF_1>", s)
        self.assertIn("<CNPJ_1>", s)
        self.assertEqual(s.count("<CPF_1>"), 2)  # mesmo CPF em dois formatos = mesmo placeholder

    def test_cpf_invalido_nao_mascarado(self):
        self.assertIn("12345678900", self.m.mascarar("pedido 12345678900"))
        self.assertTrue(cpf_valido("529.982.247-25"))
        self.assertTrue(cnpj_valido("11.222.333/0001-81"))
        self.assertTrue(luhn_valido("4111 1111 1111 1111"))

    def test_cartao(self):
        s = self.m.mascarar("cartão 4111 1111 1111 1111 recusado")
        self.assertIn("<CARTAO_1>", s)

    def test_telefone(self):
        s = self.m.mascarar("ligar (61) 98577-1234 ou +55 11 91234-5678")
        self.assertNotIn("98577", s)
        self.assertNotIn("91234", s)
        self.assertEqual(self.m.contagens()["TELEFONE"], 2)

    def test_chaves_e_tokens(self):
        texto = f"aws={AWS} gh {GHP} openai {SK} google {GOOGLE} jwt {JWT} Authorization: Bearer abcdef123456789XYZ"
        s = self.m.mascarar(texto, limite=0)
        for segredo in (AWS, GHP, SK, GOOGLE, JWT, "abcdef123456789XYZ"):
            self.assertNotIn(segredo, s)

    def test_senha_em_kv_json_e_url(self):
        s = self.m.mascarar('password=Sup3rS3creta senha: "outra senha" {"api_key": "xyz987"} '
                            'postgres://admin:minhasenha@db.interno.corp:5432/app ?token=abc123&x=1', limite=0)
        for segredo in ("Sup3rS3creta", "outra senha", "xyz987", "minhasenha", "abc123"):
            self.assertNotIn(segredo, s)
        self.assertNotIn("admin:", s)
        self.assertNotIn("db.interno.corp", s)

    def test_usuario_e_home(self):
        s = self.m.mascarar("login falhou user=joao.silva em /home/maria/app")
        self.assertNotIn("joao.silva", s)
        self.assertNotIn("maria", s)

    def test_hostname_interno_e_explicito(self):
        m = Mascarador(hosts_internos=["srv-pagto-01"])
        s = m.mascarar("timeout em api.pagamentos.internal e srv-pagto-01")
        self.assertNotIn("api.pagamentos.internal", s)
        self.assertNotIn("srv-pagto-01", s)
        self.assertIn("<HOST_", s)

    def test_hex_e_alta_entropia(self):
        s = self.m.mascarar("trace 4bf92f3577b34da6a3ce929d0e0e4736 chave Zk8pQ2xV9mT4rW7yB1nC5dF3hJ6kL0sA", limite=0)
        self.assertNotIn("4bf92f3577b34da6a3ce929d0e0e4736", s)
        self.assertNotIn("Zk8pQ2xV9mT4rW7yB1nC5dF3hJ6kL0sA", s)

    def test_texto_normal_preservado(self):
        t = "erro simulado em GET /comprar status 500 após 120 ms"
        self.assertEqual(self.m.mascarar(t), t)
        self.assertEqual(self.m.contagens(), {})

    def test_pem(self):
        s = self.m.mascarar("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", limite=0)
        self.assertEqual(s, "<CHAVE_PRIVADA_1>")

    def test_estrutura_e_truncamento(self):
        obj = {"msg": "x" * 1000, "ip": ["10.1.1.1"], "n": 5}
        r = self.m.mascarar_estrutura(obj, limite=100)
        self.assertTrue(r["msg"].endswith("[truncado]"))
        self.assertEqual(r["ip"], ["<IP_1>"])
        self.assertEqual(r["n"], 5)

    def test_contagens_sem_valores(self):
        self.m.mascarar("a@b.com 10.0.0.1")
        self.assertEqual(set(self.m.contagens()), {"EMAIL", "IP"})
        self.assertNotIn("a@b.com", repr(self.m.contagens()))

    def test_limitar_linhas(self):
        linhas, cortadas = limitar_linhas(["a" * 10] * 50, 20, 1000)
        self.assertEqual((len(linhas), cortadas), (20, 30))
        linhas, cortadas = limitar_linhas(["a" * 100] * 50, 100, 350)
        self.assertEqual(len(linhas), 3)


if __name__ == "__main__":
    unittest.main()

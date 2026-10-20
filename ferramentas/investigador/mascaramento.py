"""Mascaramento de dados sensíveis ANTES de qualquer envio ao Gemini.

Cada valor sensível vira um placeholder estável dentro da mesma análise
(<IP_1>, <EMAIL_2>...): o mesmo valor recebe sempre o mesmo placeholder, o que
mantém a correlação ("o <IP_1> aparece em 40 erros") sem expor o dado. Os
valores originais ficam só na memória do objeto e NUNCA são registrados; o
relatório de auditoria guarda apenas contagens por categoria.

Uso:
    m = Mascarador(dominios_internos=["corp", "local"])
    texto = m.mascarar(texto)
    m.contagens()   # {"IP": 3, "EMAIL": 1, ...}  (ocorrências, sem valores)
"""

import ipaddress
import math
import re
from collections import Counter

TAMANHO_MAX_VALOR = 400

# Ordem importa: padrões mais específicos (chaves, JWT, URL com senha) antes
# dos genéricos (IP, alta entropia).
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_BEARER = re.compile(r"(?i)\b(bearer|basic|token)\s+([A-Za-z0-9._~+/=-]{8,})")
_AWS = re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b")
_GITHUB = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_SK = re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{16,}\b")
_GOOGLE = re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")
_SLACK = re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")
_URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15}://)([^\s:/@]{1,64}):([^\s@/]{1,128})@")
_CHAVE_VALOR = re.compile(
    r"""(?ix)
    (["']?\b(?:password|passwd|pwd|senha|secret|segredo|token|api[_-]?key|apikey|access[_-]?key|
        secret[_-]?key|client[_-]?secret|private[_-]?key|authorization|auth|credential|credencial)s?\b["']?)
    (\s*[:=]\s*)
    ("[^"]*"|'[^']*'|[^\s&,;"'}]+)
    """
)
_QUERY_SEGREDO = re.compile(r"(?i)([?&](?:key|token|sig|signature|code|access_token|password)=)[^&\s#]+")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b")
_CPF = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|\b\d{11}\b")
_CNPJ = re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b|\b\d{14}\b")
_CARTAO = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_TELEFONE = re.compile(
    r"(?:\+?55[\s-]?)?\(\d{2}\)[\s-]?9?\d{4}[\s-]?\d{4}\b"
    r"|\+55[\s-]?\d{2}[\s-]?9?\d{4}[\s-]?\d{4}\b"
    r"|\b\d{2}[\s-]9\d{4}-\d{4}\b"
    r"|\+\d{1,3}[\s-]\d{2,4}[\s-]\d{3,4}[\s-]\d{3,4}\b"
)
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6_CAND = re.compile(r"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:%\w+)?(?![0-9A-Za-z:])")
_USUARIO = re.compile(r"(?i)(\b(?:user|usuario|usuário|username|login|uid|usr)\b\s*[:=]\s*[\"']?)([^\s,;&\"']{1,64})")
_HOME = re.compile(r"(/home/|/Users/|C:\\Users\\)([A-Za-z0-9._-]{1,64})")
_HEX_LONGO = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_CANDIDATO_ENTROPIA = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
_CONTROLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

DOMINIOS_INTERNOS_PADRAO = ("local", "internal", "lan", "corp", "intra", "intranet", "home", "svc", "cluster.local", "localdomain")


def _digitos(s):
    return [int(c) for c in s if c.isdigit()]


def cpf_valido(s):
    d = _digitos(s)
    if len(d) != 11 or len(set(d)) == 1:
        return False
    for n in (9, 10):
        soma = sum(d[i] * (n + 1 - i) for i in range(n))
        if (soma * 10 % 11) % 10 != d[n]:
            return False
    return True


def cnpj_valido(s):
    d = _digitos(s)
    if len(d) != 14 or len(set(d)) == 1:
        return False
    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos2 = [6] + pesos1
    for pesos, pos in ((pesos1, 12), (pesos2, 13)):
        r = sum(a * b for a, b in zip(d[:pos], pesos)) % 11
        if (0 if r < 2 else 11 - r) != d[pos]:
            return False
    return True


def luhn_valido(s):
    d = _digitos(s)
    if not 13 <= len(d) <= 19:
        return False
    total = 0
    for i, n in enumerate(reversed(d)):
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def entropia(s):
    if not s:
        return 0.0
    c = Counter(s)
    return -sum(v / len(s) * math.log2(v / len(s)) for v in c.values())


class Mascarador:
    def __init__(self, dominios_internos=None, hosts_internos=None):
        dominios = [d.strip(".").lower() for d in (dominios_internos or DOMINIOS_INTERNOS_PADRAO) if d.strip(".")]
        sufixos = "|".join(re.escape(d) for d in sorted(dominios, key=len, reverse=True))
        self._host_interno = re.compile(
            r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:" + sufixos + r")\b") if sufixos else None
        self._hosts_explicitos = None
        nomes = [h for h in (hosts_internos or []) if h and len(h) >= 3]
        if nomes:
            self._hosts_explicitos = re.compile(
                r"(?i)(?<![\w.-])(?:" + "|".join(re.escape(h) for h in sorted(nomes, key=len, reverse=True)) + r")(?![\w-])")
        self._mapa = {}
        self._contadores = Counter()
        self._ocorrencias = Counter()

    # -- placeholders estáveis -------------------------------------------------
    def _ph(self, categoria, valor):
        chave = (categoria, valor)
        if chave not in self._mapa:
            self._contadores[categoria] += 1
            self._mapa[chave] = f"<{categoria}_{self._contadores[categoria]}>"
        self._ocorrencias[categoria] += 1
        return self._mapa[chave]

    def contagens(self):
        """Ocorrências mascaradas por categoria (nunca os valores)."""
        return dict(sorted(self._ocorrencias.items()))

    def valores_unicos(self):
        return dict(sorted(self._contadores.items()))

    # -- regras ------------------------------------------------------------------
    def mascarar(self, texto, limite=TAMANHO_MAX_VALOR):
        if texto is None:
            return ""
        s = _CONTROLE.sub(" ", str(texto))
        s = _PEM.sub(lambda m: self._ph("CHAVE_PRIVADA", m.group(0)), s)
        s = _JWT.sub(lambda m: self._ph("JWT", m.group(0)), s)
        s = _URL_CRED.sub(lambda m: m.group(1) + self._ph("USUARIO", m.group(2)) + ":" + self._ph("SENHA", m.group(3)) + "@", s)
        s = _AWS.sub(lambda m: self._ph("CHAVE_AWS", m.group(0)), s)
        s = _GITHUB.sub(lambda m: self._ph("TOKEN_GITHUB", m.group(0)), s)
        s = _SK.sub(lambda m: self._ph("CHAVE_API", m.group(0)), s)
        s = _GOOGLE.sub(lambda m: self._ph("CHAVE_GOOGLE", m.group(0)), s)
        s = _SLACK.sub(lambda m: self._ph("TOKEN", m.group(0)), s)
        s = _BEARER.sub(lambda m: m.group(1) + " " + self._ph("TOKEN", m.group(2)), s)
        s = _QUERY_SEGREDO.sub(lambda m: m.group(1) + self._ph("SEGREDO", m.group(0)), s)
        s = _CHAVE_VALOR.sub(self._sub_chave_valor, s)
        s = _EMAIL.sub(lambda m: self._ph("EMAIL", m.group(0).lower()), s)
        s = _CNPJ.sub(lambda m: self._ph("CNPJ", "".join(map(str, _digitos(m.group(0))))) if cnpj_valido(m.group(0)) else m.group(0), s)
        s = _CPF.sub(lambda m: self._ph("CPF", "".join(map(str, _digitos(m.group(0))))) if cpf_valido(m.group(0)) else m.group(0), s)
        s = _CARTAO.sub(lambda m: self._ph("CARTAO", "".join(map(str, _digitos(m.group(0))))) if luhn_valido(m.group(0)) else m.group(0), s)
        s = _TELEFONE.sub(lambda m: self._ph("TELEFONE", "".join(map(str, _digitos(m.group(0))))), s)
        s = _IPV6_CAND.sub(self._sub_ipv6, s)
        s = _IPV4.sub(self._sub_ipv4, s)
        s = _USUARIO.sub(lambda m: m.group(1) + self._ph("USUARIO", m.group(2)), s)
        s = _HOME.sub(lambda m: m.group(1) + self._ph("USUARIO", m.group(2)), s)
        if self._hosts_explicitos:
            s = self._hosts_explicitos.sub(lambda m: self._ph("HOST", m.group(0).lower()), s)
        if self._host_interno:
            s = self._host_interno.sub(lambda m: self._ph("HOST", m.group(0).lower()), s)
        s = _HEX_LONGO.sub(lambda m: self._ph("HEX", m.group(0).lower()), s)
        s = _CANDIDATO_ENTROPIA.sub(self._sub_entropia, s)
        if limite and len(s) > limite:
            s = s[:limite] + "…[truncado]"
        return s

    def _sub_chave_valor(self, m):
        valor = m.group(3)
        if valor.startswith("<") and valor.endswith(">"):
            return m.group(0)  # já mascarado
        return m.group(1) + m.group(2) + self._ph("SEGREDO", valor.strip("\"'"))

    def _sub_ipv4(self, m):
        try:
            ipaddress.IPv4Address(m.group(0))
        except ValueError:
            return m.group(0)
        return self._ph("IP", m.group(0))

    def _sub_ipv6(self, m):
        bruto = m.group(0)
        if bruto.count(":") < 2:
            return bruto
        try:
            ipaddress.IPv6Address(bruto.split("%")[0])
        except ValueError:
            return bruto
        return self._ph("IP", bruto.lower())

    def _sub_entropia(self, m):
        t = m.group(0)
        if t.startswith("<") or "_" in t[:1]:
            return t
        tem_digito = any(c.isdigit() for c in t)
        tem_letra = any(c.isalpha() for c in t)
        if tem_digito and tem_letra and entropia(t) >= 4.0:
            return self._ph("SEGREDO", t)
        return t

    def mascarar_estrutura(self, obj, limite=TAMANHO_MAX_VALOR):
        """Mascara recursivamente strings dentro de dict/list (chaves inclusive)."""
        if isinstance(obj, dict):
            return {self.mascarar(k, 80): self.mascarar_estrutura(v, limite) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.mascarar_estrutura(v, limite) for v in obj]
        if isinstance(obj, (int, float, bool)) or obj is None:
            return obj
        return self.mascarar(obj, limite)


def limitar_linhas(linhas, max_linhas, max_caracteres):
    """Corta a lista de linhas para caber no teto de contexto. Retorna (linhas, cortadas)."""
    saida, total = [], 0
    for i, linha in enumerate(linhas):
        if i >= max_linhas or total + len(linha) > max_caracteres:
            return saida, len(linhas) - i
        saida.append(linha)
        total += len(linha) + 1
    return saida, 0

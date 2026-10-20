# python:3.13-slim fixado por digest (ver docs/digests-validados.md)
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY gerador.py bootstrap.py api_ficticia.py trafego.py ia_assistente.py ./
# Investigador de causa raiz: só biblioteca padrão (não usa o requirements).
COPY investigador/ ./investigador/

# Não precisa de root para nada. /dados guarda o SQLite do investigador
# (cache/auditoria) num volume nomeado, que herda este dono na criação.
RUN useradd --system --uid 10001 --no-create-home lab \
    && mkdir -p /dados && chown 10001 /dados
USER 10001

CMD ["python", "gerador.py"]

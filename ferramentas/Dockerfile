FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY gerador.py bootstrap.py ./

# Não precisa de root para nada
RUN useradd --system --uid 10001 --no-create-home lab
USER 10001

CMD ["python", "gerador.py"]

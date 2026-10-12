# BlockShare - all-in-one image.
# One container runs: Anvil (EVM blockchain), 3 storage nodes, the monitor and
# Django/Gunicorn, supervised by supervisord. The same image is reused by
# docker-compose.yml to run every component as its own container on a network.
FROM python:3.11-slim

ARG FOUNDRY_VERSION=v1.0.0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBUG=false \
    BLOCKSHARE_VAR_DIR=/data \
    PORT=8000

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL "https://github.com/foundry-rs/foundry/releases/download/${FOUNDRY_VERSION}/foundry_${FOUNDRY_VERSION}_linux_amd64.tar.gz" \
    | tar -xz -C /usr/local/bin anvil \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt supervisor==4.2.5

COPY . .
RUN python manage.py collectstatic --noinput -v 0 \
 && chmod +x deploy/*.sh \
 && mkdir -p /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz',timeout=4)"
CMD ["supervisord", "-c", "/app/deploy/supervisord.conf"]

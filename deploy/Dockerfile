FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VERA_STATE_DIR=/var/lib/vera

WORKDIR /app
COPY requirements-dev.txt /app/requirements-dev.txt
RUN pip install --no-cache-dir -r /app/requirements-dev.txt uvicorn && \
    useradd --create-home --uid 10001 vera && \
    mkdir -p /var/lib/vera && chown -R vera:vera /var/lib/vera /app
COPY . /app
RUN chown -R vera:vera /app
USER vera

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8080') + '/v1/healthz', timeout=2)" || exit 1

CMD ["sh", "/app/deploy/run.sh"]

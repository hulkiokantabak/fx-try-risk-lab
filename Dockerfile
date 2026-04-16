FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV FX_HOST=0.0.0.0
ENV FX_PORT=8000
ENV FX_RELOAD=false
ENV FX_FORWARDED_ALLOW_IPS=127.0.0.1

WORKDIR /app

RUN useradd --create-home --shell /bin/bash appuser

COPY pyproject.toml README.md DEPLOYMENT_GUIDE.md ./
COPY app ./app

RUN python -m pip install --upgrade pip \
    && pip install .

RUN mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD python -c "import sys, urllib.request; response = urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=4); sys.exit(0 if response.status == 200 else 1)"

CMD ["python", "-m", "app.serve"]

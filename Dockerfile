FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOTPULSE_DB=/data/hotpulse.db HOTPULSE_CONFIG=/app/config
WORKDIR /app
COPY pyproject.toml README.md ./
COPY hotpulse ./hotpulse
RUN pip install --no-cache-dir ".[extract]"
COPY config ./config
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["hotpulse", "serve", "--host", "0.0.0.0", "--port", "8000"]

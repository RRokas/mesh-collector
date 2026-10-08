FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
RUN useradd --system --uid 10001 mesh && mkdir -p /srv/data && chown mesh /srv/data
USER mesh

# PORT is set by App Platform; defaults to 8000 elsewhere.
ENV PORT=8000 DATABASE_URL=sqlite:////srv/data/mesh.db
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"

# One worker is plenty for this volume and keeps SQLite single-writer.
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --timeout-keep-alive 5"]

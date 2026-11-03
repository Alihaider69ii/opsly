# One image, two roles: the API (default command) and the background worker
# (command: python -m app.workers.main). See docs/production.md.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Never run as root: a compromise of the process must not own the container.
RUN groupadd --system --gid 10001 bizflow \
 && useradd --system --uid 10001 --gid bizflow --no-create-home --shell /usr/sbin/nologin bizflow

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=bizflow:bizflow . .
USER bizflow

EXPOSE 8000

# Liveness only (is the process answering). Readiness, which also needs PostgreSQL and Redis, is
# GET /health/ready; point the orchestrator's readiness probe there. The worker service overrides
# this with: python -m app.workers.healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"

# X-Forwarded-For is honoured only from the addresses in the FORWARDED_ALLOW_IPS environment
# variable (uvicorn reads it); production refuses to start without it being set explicitly.

RUN chmod +x start.sh

CMD ["./start.sh"]
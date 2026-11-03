#!/bin/sh

echo "Starting Opsly API..."
python -m app.workers.main &
WORKER_PID=$!

echo "Starting Opsly Worker (PID: $WORKER_PID)..."

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --proxy-headers \
  --no-server-header \
  --timeout-keep-alive 5
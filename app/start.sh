#!/usr/bin/env bash
set -e

# Default to gunicorn; PORT is provided by App Runner
export PORT="${PORT:-8080}"

# app factory: app:create_app()
# if you have wsgi.py exposing "app", adjust accordingly
exec gunicorn "app:create_app()" \
  --bind "0.0.0.0:${PORT}" \
  --workers 3 \
  --threads 2 \
  --timeout 60 \
  --access-logfile - \
  --error-logfile - \
  --log-level info

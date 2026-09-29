#!/bin/sh
# Starts FlareSolverr in the background (internal only), waits until ready,
# then starts the API on $PORT (Render/Railway set PORT automatically).
export FLARESOLVERR_URL="${FLARESOLVERR_URL:-http://127.0.0.1:8191}"

if [ "$FLARESOLVERR_URL" = "http://127.0.0.1:8191" ]; then
  (cd /app && PORT=8191 HOST=127.0.0.1 LOG_LEVEL=info \
     exec /usr/local/bin/python -u /app/flaresolverr.py) &
  echo "Waiting for FlareSolverr..."
  i=0
  until /usr/local/bin/python - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:8191/health", timeout=2)
PY
  do
    i=$((i+1)); [ "$i" -ge 60 ] && echo "FlareSolverr not ready, starting API anyway" && break
    sleep 1
  done
fi

cd /srv/api
exec /opt/api-venv/bin/uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"

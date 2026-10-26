#!/usr/bin/env bash
set -euo pipefail
cd /home/frappe/frappe-bench
case "${1:-backend}" in
  backend)
    exec env/bin/gunicorn --chdir=sites --bind=0.0.0.0:8000 \
      --threads="${GUNICORN_THREADS:-4}" --workers="${GUNICORN_WORKERS:-2}" \
      --worker-class=gthread --worker-tmp-dir=/dev/shm \
      --timeout="${GUNICORN_TIMEOUT:-120}" --preload frappe.app:application ;;
  frontend) exec /usr/local/bin/nginx-entrypoint.sh ;;
  websocket) exec node apps/frappe/socketio.js ;;
  scheduler) exec bench schedule ;;
  worker-long) exec bench worker --queue long ;;
  worker-short) exec bench worker --queue short ;;
  worker-default) exec bench worker --queue default ;;
  *) exec "$@" ;;
esac

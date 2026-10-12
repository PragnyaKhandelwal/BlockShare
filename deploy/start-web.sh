#!/bin/sh
# Prepare the database, users, nodes and contracts, then serve the app.
set -e
rm -f /tmp/bootstrapped
python manage.py bootstrap
touch /tmp/bootstrapped
exec gunicorn blockshare.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --worker-class gthread --workers "${WEB_WORKERS:-1}" --threads "${WEB_THREADS:-8}" \
  --timeout 300 --access-logfile - --forwarded-allow-ips '*'

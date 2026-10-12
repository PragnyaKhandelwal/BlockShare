#!/bin/sh
# Wait until bootstrap has migrated the database, then watch the cluster.
while [ ! -f /tmp/bootstrapped ]; do sleep 2; done
exec python manage.py run_monitor --interval "${MONITOR_INTERVAL:-5}"

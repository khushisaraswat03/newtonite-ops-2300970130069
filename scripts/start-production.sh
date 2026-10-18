#!/bin/sh
set -e

python manage.py migrate --noinput
python manage.py seed --if-empty

(while true; do python manage.py run_worker; sleep 5; done) &

exec gunicorn config.wsgi:application --bind "0.0.0.0:${PORT:-8000}" --workers 2 --timeout 60

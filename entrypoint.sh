#!/usr/bin/env bash
set -o errexit

python manage.py migrate --no-input
python manage.py collectstatic --no-input

exec daphne -b 0.0.0.0 -p 8000 chattingapp.asgi:application

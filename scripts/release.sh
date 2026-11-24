#!/usr/bin/env sh
set -eu

# Run once per deployment/release, not in each web worker.
python manage.py migrate --noinput
python manage.py check --deploy

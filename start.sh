#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate

# Load .env variables into the shell environment
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

# Apply committed migrations on start. .env points at the LIVE Railway database, so
# this updates production — only migration files already in the repo are applied.
# makemigrations is deliberately NOT run here: on 2026-10-02 it auto-created a file
# under a different name from the committed one and the next deploy crashed. To
# create a migration for a model change:
#   DATABASE_URL= python manage.py makemigrations
# and commit the file it writes.
echo "Applying migrations..."
python manage.py migrate

echo "Starting server..."
python manage.py runserver 0.0.0.0:8000

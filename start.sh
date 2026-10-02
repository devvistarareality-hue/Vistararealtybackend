#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate

# Load .env variables into the shell environment
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

# No makemigrations / migrate here. .env points at the LIVE Railway database, so
# running them on every local start wrote unreviewed schema changes straight into
# production before anything was pushed (and on 2026-10-02 that crashed a deploy).
# Migrations reach the live database only through a Railway deploy, which runs
# `migrate` itself (see railway.json). To create a migration for a model change:
#   DATABASE_URL= python manage.py makemigrations
# and commit the file it writes.
echo "Pending migrations on this database (not applied — a deploy applies them):"
python manage.py showmigrations --plan 2>/dev/null | grep '\[ \]' || echo "  none"

echo "Starting server..."
python manage.py runserver 0.0.0.0:8000

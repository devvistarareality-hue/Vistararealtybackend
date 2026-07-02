#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate

# Load .env variables into the shell environment
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

echo "Running migrations..."
python manage.py makemigrations
python manage.py migrate

echo "Starting server..."
python manage.py runserver 0.0.0.0:8000

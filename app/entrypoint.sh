#!/bin/sh
set -e

if [ -z "${IRIS_DB_PASSWORD:-}" ] && [ -f /run/secrets/IRIS_DB_PASSWORD ]; then
    IRIS_DB_PASSWORD=$(cat /run/secrets/IRIS_DB_PASSWORD)
    export IRIS_DB_PASSWORD
fi

if [ -z "${IRIS_WEBHOOK_SECRET:-}" ] && [ -f /run/secrets/IRIS_WEBHOOK_SECRET ]; then
    IRIS_WEBHOOK_SECRET=$(cat /run/secrets/IRIS_WEBHOOK_SECRET)
    export IRIS_WEBHOOK_SECRET
fi

if [ -z "${DATABASE_URL:-}" ] && [ -n "${IRIS_DB_PASSWORD:-}" ]; then
    DATABASE_URL=$(python -c 'from app.core.config import settings; print(settings.resolved_database_url())')
    export DATABASE_URL
fi

attempt=1
until alembic -c /app/app/alembic.ini upgrade head; do
    if [ "$attempt" -ge 30 ]; then
        echo "ERROR: database not reachable after ${attempt} attempts" >&2
        exit 1
    fi
    echo "Waiting for database (attempt ${attempt}/30)..."
    attempt=$((attempt + 1))
    sleep 2
done

exec "$@"

#!/bin/sh
# Apply schema migrations, then start the API. (Render free plan has no pre-deploy hook.)
set -e
alembic upgrade head
exec uvicorn eumbms.main:app --host 0.0.0.0 --port "${PORT:-8000}" --no-server-header --no-access-log

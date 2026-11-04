#!/bin/sh
# Container entrypoint: `api` (migrate, then serve) or `worker` (Celery worker + beat scheduler).
set -e

case "$1" in
  api)
    alembic upgrade head
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
    ;;
  worker)
    # -B runs the beat scheduler in-process (heartbeat + stale-job requeue). With several worker
    # replicas, run beat as its own single service instead.
    exec celery -A app.workers.celery_app worker -B -Q documents,analysis,celery \
      --concurrency "${WORKER_CONCURRENCY:-2}" --loglevel INFO -s /tmp/celerybeat-schedule
    ;;
  *)
    exec "$@"
    ;;
esac

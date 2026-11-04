"""Celery application (Redis broker). Workers run `app.workers.tasks`."""

from celery import Celery
from celery.signals import worker_process_init, worker_ready

from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()

celery_app = Celery(
    "vault",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,  # a crashed worker's task is redelivered, not lost
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # fair distribution of long-running tasks
    task_track_started=True,
    result_expires=24 * 3600,
    broker_connection_retry_on_startup=True,
    task_routes={
        "app.workers.tasks.process_document": {"queue": "documents"},
        "app.workers.tasks.regenerate_insights": {"queue": "documents"},
        "app.workers.tasks.run_comparison": {"queue": "analysis"},
    },
    beat_schedule={
        "worker-heartbeat": {"task": "app.workers.tasks.heartbeat", "schedule": 15.0},
        "requeue-stale-documents": {"task": "app.workers.tasks.requeue_stale_documents", "schedule": 300.0},
    },
)


@worker_process_init.connect
def _init_worker(**_: object) -> None:
    configure_logging(settings.log_level)


@worker_ready.connect
def _on_ready(**_: object) -> None:
    from app.workers.tasks import heartbeat

    heartbeat()

"""Test setup: real Postgres (pgvector) + Redis, fake LLM provider, Celery tasks run inline.

Requires the compose `db` and `redis` services (`make test` starts them). Uses a separate
database (`vault_test`) and Redis DBs 10-12, so it never touches your dev data.
"""

import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

DB_HOST = os.environ.get("TEST_DB_HOST", "localhost")
DB_PORT = os.environ.get("TEST_DB_PORT", "9701")
REDIS = os.environ.get("TEST_REDIS_URL", "redis://localhost:9702")
STORAGE = Path(tempfile.mkdtemp(prefix="vault-test-"))

os.environ.update(
    {
        "LLM_PROVIDER": "fake",
        "DATABASE_URL": f"postgresql+asyncpg://vault:vault@{DB_HOST}:{DB_PORT}/vault_test",
        "SYNC_DATABASE_URL": f"postgresql+psycopg://vault:vault@{DB_HOST}:{DB_PORT}/vault_test",
        "REDIS_URL": f"{REDIS}/10",
        "CELERY_BROKER_URL": f"{REDIS}/11",
        "CELERY_RESULT_BACKEND": f"{REDIS}/12",
        "STORAGE_DIR": str(STORAGE),
        "RECORD_REQUEST_METRICS": "true",
        "LOG_LEVEL": "WARNING",
    }
)

import httpx  # noqa: E402
import psycopg  # noqa: E402
import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402

from alembic import command  # noqa: E402
from app.services.cache import get_redis  # noqa: E402
from app.workers.celery_app import celery_app  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
SAMPLES = BACKEND.parent / "samples"
API_KEY = "vault-demo-key"

celery_app.conf.update(task_always_eager=True, task_eager_propagates=True)


def _reset_database() -> None:
    admin = f"postgresql://vault:vault@{DB_HOST}:{DB_PORT}/vault"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS vault_test WITH (FORCE)")
        conn.execute("CREATE DATABASE vault_test")
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(config, "head")


@pytest.fixture(scope="session", autouse=True)
def database() -> None:
    _reset_database()
    get_redis().flushdb()
    yield
    shutil.rmtree(STORAGE, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_redis() -> None:
    """Fresh rate-limit counters and caches per test."""
    get_redis().flushdb()


@pytest.fixture(scope="session")
async def client() -> AsyncIterator[httpx.AsyncClient]:
    from app.main import app

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test/api/v1", headers={"X-API-Key": API_KEY}, timeout=60
        ) as c:
            yield c


async def upload(client: httpx.AsyncClient, name: str, content: bytes | None = None, **form: str) -> httpx.Response:
    data = content if content is not None else (SAMPLES / name).read_bytes()
    return await client.post("/documents", files={"file": (Path(name).name, data)}, data=form)


@pytest.fixture(scope="session")
async def ready_docs(client: httpx.AsyncClient) -> dict[str, str]:
    """The three sample documents, uploaded and processed (inline) once for the session."""
    ids = {}
    for key, name in [
        ("policy", "acme_property_policy_2025.pdf"),
        ("quote", "northwind_general_liability_quote.docx"),
        ("agreement", "bayline_broker_service_agreement.md"),
    ]:
        response = await upload(client, name)
        assert response.status_code in (200, 202), response.text
        ids[key] = response.json()["document"]["id"]
    return ids

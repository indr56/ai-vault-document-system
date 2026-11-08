"""Reliability of the background jobs, caches, rate limiter and chat streaming.

Each test reproduces one failure mode (provider outage, database error, timeout, Redis down,
deleted rows, lost queue messages, interrupted streams) and checks the system ends in a clear state.
"""

import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
import openai
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select, text, update

from app.db.base import utcnow
from app.db.session import sync_session
from app.llm import get_provider
from app.models import AIUsage, ChatMessage, Comparison, Document, ProcessingRun, StageRun
from app.services import cache, jobs, processing
from app.workers import tasks
from tests.conftest import upload


def _unique_text(label: str) -> bytes:
    """Fresh content per test, so duplicate detection never returns an older document."""
    return (
        f"{label} {uuid.uuid4()}. The water damage limit is $50,000 per occurrence and claims must be "
        "reported within 30 days of discovery."
    ).encode()


def _transient() -> Exception:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/embeddings"))


def _dead_redis():  # type: ignore[no-untyped-def]
    import redis

    return redis.Redis(host="localhost", port=1, socket_connect_timeout=0.2)


def _db_document(document_id: str) -> Document:
    with sync_session() as db:
        doc = db.get(Document, uuid.UUID(document_id))
        assert doc is not None
        db.expunge(doc)
        return doc


# ------------------------------------------------------------------ locks and backoff (#26, #25)
def test_lock_release_never_deletes_someone_elses_lock() -> None:
    key = f"lock:test:{uuid.uuid4()}"
    with jobs.redis_lock(key, 60) as acquired:
        assert acquired
        # Simulate: our TTL expired and another job took the lock with its own token.
        cache.get_redis().set(key, "other-job-token")
    assert cache.get_redis().get(key) == b"other-job-token"


def test_lock_is_exclusive_and_released() -> None:
    key = f"lock:test:{uuid.uuid4()}"
    with jobs.redis_lock(key, 60) as first, jobs.redis_lock(key, 60) as second:
        assert first and not second
    assert cache.get_redis().get(key) is None


def test_retry_backoff_has_jitter() -> None:
    values = {jobs.retry_countdown(0) for _ in range(50)}
    assert all(8 <= v <= 12 for v in values) and len(values) > 1
    assert all(72 <= jobs.retry_countdown(5) <= 108 for _ in range(20))  # capped at the last step (90 s)


# ------------------------------------------------------------------ Redis outages (#27, #13, #14)
def test_embedding_cache_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "get_redis", _dead_redis)
    assert cache.get_cached_embeddings("m", ["a", "b"]) == [None, None]  # miss, not an exception
    cache.set_cached_embeddings("m", {"a": [0.1]})  # skipped, not an exception


async def test_rate_limiter_fails_open(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import redis.asyncio as aioredis

    from app.core import rate_limit

    monkeypatch.setattr(rate_limit, "get_aredis", lambda: aioredis.Redis(host="localhost", port=1))
    response = await client.get("/documents")
    assert response.status_code == 200  # was a 500 for every request while Redis was down


async def test_rate_limit_counter_always_has_expiry() -> None:
    from app.core.rate_limit import check_rate_limit

    await check_rate_limit("user-ttl", "test", 5)
    keys = cache.get_redis().keys("rl:test:user-ttl:*")
    assert keys and 0 < cache.get_redis().ttl(keys[0]) <= 70


# ------------------------------------------------------------------ document pipeline (#25, #28, #11, #40)
async def test_transient_provider_error_is_retried(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = get_provider()
    real_embed, calls = provider.embed, {"n": 0}

    def flaky_embed(texts: list[str]):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise _transient()
        return real_embed(texts)

    monkeypatch.setattr(provider, "embed", flaky_embed)
    monkeypatch.setattr(tasks, "enqueue_document", lambda _id: None)  # run the attempts by hand
    response = await upload(client, "retry.txt", _unique_text("retry"))
    document_id = uuid.UUID(response.json()["document"]["id"])
    # Attempt 1: transient error -> back to queued and asks Celery to retry (with jittered backoff).
    with pytest.raises(processing.TransientProcessingError):
        processing.run_pipeline(document_id, task_id=None, is_final_attempt=False)
    assert _db_document(str(document_id)).status == "queued"
    # Attempt 2 (what Celery's retry runs): succeeds.
    assert processing.run_pipeline(document_id, task_id=None, is_final_attempt=False).status == "ready"
    doc = (await client.get(f"/documents/{document_id}")).json()
    assert doc["status"] == "ready" and doc["attempts"] == 2
    runs = (await client.get(f"/documents/{doc['id']}/processing-runs")).json()
    assert sorted(r["status"] for r in runs) == ["failed", "succeeded"]


async def test_database_error_inside_a_stage_fails_cleanly(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_store(db, document, chunks, vectors):  # type: ignore[no-untyped-def]
        db.execute(text("SELECT * FROM no_such_table"))  # aborts the transaction

    monkeypatch.setattr(processing, "_store_chunks", broken_store)
    response = await upload(client, "dberror.txt", _unique_text("dberror"))
    doc = _db_document(response.json()["document"]["id"])
    assert doc.status == "failed" and doc.error_code == "processing_error"  # not stuck in "processing"
    with sync_session() as db:
        stage = db.execute(
            select(StageRun).join(ProcessingRun).where(ProcessingRun.document_id == doc.id, StageRun.stage == "embed")
        ).scalar_one()
        assert stage.status == "failed" and "no_such_table" in (stage.error_message or "")


async def test_soft_time_limit_fails_document_with_timeout_code(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow_chunking(*args: Any, **kwargs: Any) -> None:
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(processing, "chunk_document", slow_chunking)
    response = await upload(client, "slow.txt", _unique_text("slow"))
    doc = _db_document(response.json()["document"]["id"])
    assert doc.status == "failed" and doc.error_code == "processing_timeout"


async def test_delete_while_processing_is_rejected(client: httpx.AsyncClient) -> None:
    response = await upload(client, "busy.txt", _unique_text("busy"))
    document_id = response.json()["document"]["id"]
    with sync_session() as db:
        db.execute(update(Document).where(Document.id == uuid.UUID(document_id)).values(status="processing"))
    response = await client.delete(f"/documents/{document_id}")
    assert response.status_code == 409 and response.json()["error"]["code"] == "document_processing"
    with sync_session() as db:
        db.execute(update(Document).where(Document.id == uuid.UUID(document_id)).values(status="ready"))
    assert (await client.delete(f"/documents/{document_id}")).status_code == 204


async def test_worker_stops_cleanly_when_document_is_deleted_mid_processing(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_embed, seen = processing.embed_chunks, {}

    def embed_then_row_disappears(db, document, chunks):  # type: ignore[no-untyped-def]
        result = real_embed(db, document, chunks)
        with sync_session() as other:  # e.g. deleted through another path while we were embedding
            other.execute(text("DELETE FROM documents WHERE id = :id"), {"id": document.id})
        seen["id"] = document.id
        return result

    monkeypatch.setattr(processing, "embed_chunks", embed_then_row_disappears)
    monkeypatch.setattr(tasks, "enqueue_document", lambda _id: None)  # upload only, run the job by hand
    response = await upload(client, "vanishing.txt", _unique_text("vanishing"))
    result = processing.run_pipeline(uuid.UUID(response.json()["document"]["id"]), task_id=None, is_final_attempt=True)
    assert result.status == "skipped_deleted"  # no unhandled exception from the worker


# ------------------------------------------------------------------ requeue safety net (#44, #28, #39)
def _age(model, item_id: uuid.UUID, minutes: int, **values: Any) -> None:  # type: ignore[no-untyped-def]
    with sync_session() as db:
        db.execute(
            update(model).where(model.id == item_id).values(updated_at=utcnow() - timedelta(minutes=minutes), **values)
        )


async def test_requeue_resends_only_lost_messages_and_fails_stuck_jobs(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(tasks, "enqueue_document", lambda _id: None)
    waiting = uuid.UUID((await upload(client, "w.txt", _unique_text("waiting"))).json()["document"]["id"])
    lost = uuid.UUID((await upload(client, "l.txt", _unique_text("lost"))).json()["document"]["id"])
    crashed = uuid.UUID((await upload(client, "c.txt", _unique_text("crashed"))).json()["document"]["id"])
    exhausted = uuid.UUID((await upload(client, "e.txt", _unique_text("exhausted"))).json()["document"]["id"])
    monkeypatch.setattr(tasks, "enqueue_document", lambda doc_id: sent.append(str(doc_id)))

    jobs.mark_enqueued("document", waiting)  # its message is still in the broker
    _age(Document, waiting, 15, status="queued")
    _age(Document, lost, 15, status="queued")  # no marker: the message was lost
    _age(Document, crashed, 30, status="processing", attempts=1)  # worker died mid-job
    _age(Document, exhausted, 30, status="processing", attempts=4)  # ... repeatedly

    tasks.requeue_stale_documents()

    assert str(lost) in sent and str(crashed) in sent
    assert str(waiting) not in sent and str(exhausted) not in sent
    assert _db_document(str(crashed)).status == "queued"
    failed = _db_document(str(exhausted))
    assert failed.status == "failed" and failed.error_code == "processing_timeout"


async def test_requeue_handles_comparisons(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(tasks, "enqueue_comparison", lambda _id: None)
    body = {"document_ids": [ready_docs["policy"], ready_docs["quote"]]}
    lost = uuid.UUID((await client.post("/comparisons", json=body)).json()["id"])
    dead = uuid.UUID((await client.post("/comparisons", json=body)).json()["id"])
    monkeypatch.setattr(tasks, "enqueue_comparison", lambda cid: sent.append(str(cid)))
    _age(Comparison, lost, 15)
    _age(Comparison, dead, 30, status="running")

    tasks.requeue_stale_documents()

    assert str(lost) in sent and str(dead) not in sent
    with sync_session() as db:
        assert db.get(Comparison, dead).status == "failed"  # type: ignore[union-attr]


# ------------------------------------------------------------------ insights and comparison jobs (#39)
def _broker_down(*args: Any, **kwargs: Any) -> None:
    raise ConnectionError("broker unavailable")


async def test_regenerate_insights_when_queue_is_down(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks.regenerate_insights, "delay", _broker_down)
    response = await client.post(f"/documents/{ready_docs['agreement']}/insights/regenerate")
    assert response.status_code == 503 and response.json()["error"]["code"] == "queue_unavailable"
    assert (await client.get(f"/documents/{ready_docs['agreement']}")).json()["insights_status"] == "ready"


async def test_regenerate_insights_marks_failed_when_file_is_unreadable(client: httpx.AsyncClient) -> None:
    from app.services.storage import get_storage

    response = await upload(client, "gone.txt", _unique_text("gone"))
    doc = _db_document(response.json()["document"]["id"])
    get_storage().path(doc.storage_path).unlink()
    assert processing.regenerate_insights(doc.id) == "failed"  # was: stuck at "pending" forever
    assert _db_document(str(doc.id)).insights_status == "failed"


async def test_comparison_when_queue_is_down(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks.run_comparison, "delay", _broker_down)
    before = len((await client.get("/comparisons")).json())
    response = await client.post("/comparisons", json={"document_ids": [ready_docs["policy"], ready_docs["quote"]]})
    assert response.status_code == 503 and response.json()["error"]["code"] == "queue_unavailable"
    assert len((await client.get("/comparisons")).json()) == before  # no row left "pending" forever


async def test_comparison_retries_transient_errors(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = get_provider()
    real, calls = provider.complete_structured, {"n": 0}

    def flaky(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise _transient()
        return real(*args, **kwargs)

    from app.services.comparison import run_comparison_job
    from app.services.jobs import TransientJobError

    monkeypatch.setattr(provider, "complete_structured", flaky)
    monkeypatch.setattr(tasks, "enqueue_comparison", lambda _id: None)  # run the attempts by hand
    response = await client.post("/comparisons", json={"document_ids": [ready_docs["policy"], ready_docs["quote"]]})
    comparison_id = uuid.UUID(response.json()["id"])
    with pytest.raises(TransientJobError):  # attempt 1: back to pending, Celery retries (was: failed at once)
        run_comparison_job(comparison_id, is_final_attempt=False)
    assert (await client.get(f"/comparisons/{comparison_id}")).json()["status"] == "pending"
    assert run_comparison_job(comparison_id, is_final_attempt=False) == "completed"
    assert calls["n"] == 2


# ------------------------------------------------------------------ chat streaming (#35, #36, #37)
async def _session(client: httpx.AsyncClient, doc_id: str) -> str:
    response = await client.post("/chat/sessions", json={"document_ids": [doc_id]})
    return response.json()["id"]


async def _stream(client: httpx.AsyncClient, session_id: str, question: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    async with client.stream(
        "POST", f"/chat/sessions/{session_id}/messages/stream", json={"question": question, "use_cache": False}
    ) as response:
        assert response.status_code == 200
        event = None
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                events.append((event, json.loads(line.split(":", 1)[1])))
    return events


async def test_stream_failure_mid_answer_keeps_question_and_cost(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def breaks_mid_answer(messages, **kwargs):  # type: ignore[no-untyped-def]
        from app.llm.base import StreamEvent

        yield StreamEvent(delta="The flood deductible is ")
        raise _transient()

    monkeypatch.setattr(get_provider(), "astream", breaks_mid_answer)
    session_id = await _session(client, ready_docs["policy"])
    events = await _stream(client, session_id, "What is the flood deductible?")
    assert events[-1][0] == "error"

    history = (await client.get(f"/chat/sessions/{session_id}")).json()["messages"]
    assert [m["role"] for m in history] == ["user", "assistant"]  # was: nothing saved
    assert history[1]["content"].startswith("The flood deductible is")
    with sync_session() as db:
        rows = db.execute(select(AIUsage).where(AIUsage.session_id == uuid.UUID(session_id))).scalars().all()
        answer_rows = [r for r in rows if r.operation == "chat_answer"]
        assert answer_rows and answer_rows[0].success is False and answer_rows[0].completion_tokens > 0


async def test_client_disconnect_mid_stream_is_saved(ready_docs: dict[str, str]) -> None:
    from app.db.session import get_async_sessionmaker
    from app.models import User
    from app.services import chat as chat_service

    with sync_session() as db:
        owner_id = db.get(Document, uuid.UUID(ready_docs["quote"])).owner_id  # type: ignore[union-attr]
    async with get_async_sessionmaker()() as db:
        user = await db.get(User, owner_id)
        session = await chat_service.create_session(db, user, [uuid.UUID(ready_docs["quote"])], None)  # type: ignore[arg-type]
        stream = chat_service.ask_stream(db, user, session, "What is the annual premium?", use_cache=False)  # type: ignore[arg-type]
        async for event, _ in stream:
            if event == "token":
                break  # the browser tab was closed after the first token
        await stream.aclose()
    with sync_session() as db:
        messages = db.execute(select(ChatMessage).where(ChatMessage.session_id == session.id)).scalars().all()
        assert len(messages) == 2 and messages[1].retrieval.get("interrupted") is True


async def test_stream_reports_chat_deleted_before_stream_started(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.errors import NotFoundError
    from app.services import chat as chat_service

    real_get, calls = chat_service.get_session, {"n": 0}

    async def deleted_after_first_check(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:  # the reload inside the stream
            raise NotFoundError("Chat session not found.", code="session_not_found")
        return await real_get(*args, **kwargs)

    session_id = await _session(client, ready_docs["policy"])
    monkeypatch.setattr(chat_service, "get_session", deleted_after_first_check)
    events = await _stream(client, session_id, "What is the flood deductible?")
    assert events == [("error", {"code": "session_not_found", "message": "Chat session not found."})]  # was: empty 200


async def test_stream_holds_one_database_connection(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.session import get_async_engine

    provider, seen = get_provider(), {}
    real_stream = provider.astream

    async def measuring_stream(messages, **kwargs):  # type: ignore[no-untyped-def]
        seen["connections"] = get_async_engine().pool.checkedout()
        async for event in real_stream(messages, **kwargs):
            yield event

    monkeypatch.setattr(provider, "astream", measuring_stream)
    session_id = await _session(client, ready_docs["policy"])
    events = await _stream(client, session_id, "What is the flood deductible?")
    assert events[-1][0] == "done"
    assert seen["connections"] == 1  # was 2: the request's session stayed open for the whole stream


async def test_document_deleted_during_insights_step_stops_cleanly(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Delete is allowed once the document is READY, but the worker may still be writing insights.
    from app.services import insights

    real = insights.generate_insights

    def delete_then_generate(db, document, parsed):  # type: ignore[no-untyped-def]
        with sync_session() as other:
            other.execute(text("DELETE FROM documents WHERE id = :id"), {"id": document.id})
        return real(db, document, parsed)

    monkeypatch.setattr(processing, "generate_insights", delete_then_generate)
    monkeypatch.setattr(tasks, "enqueue_document", lambda _id: None)
    response = await upload(client, "gone-during-insights.txt", _unique_text("gone-during-insights"))
    document_id = uuid.UUID(response.json()["document"]["id"])
    result = processing.run_pipeline(document_id, task_id=None, is_final_attempt=True)
    assert result.status == "skipped_deleted"  # no unhandled exception from the worker

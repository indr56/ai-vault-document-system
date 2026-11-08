"""Data integrity under races (duplicates, versions, summaries) and cost tracking accuracy."""

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db.session import sync_session
from app.llm import get_provider
from app.models import AIUsage, Document
from app.services import documents as documents_service
from app.services import processing
from tests.conftest import STORAGE, upload


def _content(label: str) -> bytes:
    return f"{label} {uuid.uuid4()}. The water damage limit is $50,000 per occurrence.".encode()


def _files() -> int:
    return sum(1 for p in STORAGE.rglob("*") if p.is_file())


def _usage_rows(**where: Any) -> list[AIUsage]:
    with sync_session() as db:
        stmt = select(AIUsage)
        for column, value in where.items():
            stmt = stmt.where(getattr(AIUsage, column) == value)
        return list(db.execute(stmt).scalars())


# ------------------------------------------------------------------ duplicates and versions (#1, #2)
async def test_same_file_uploaded_twice_at_once_is_stored_once(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _content("race")
    first = (await upload(client, "race.txt", data)).json()
    files_before = _files()

    # Reproduce the race: the second request's duplicate check runs before the first one committed.
    real_find, calls = documents_service._find_duplicate, {"n": 0}

    async def check_misses_once(*args: Any) -> Document | None:
        calls["n"] += 1
        return None if calls["n"] == 1 else await real_find(*args)

    monkeypatch.setattr(documents_service, "_find_duplicate", check_misses_once)
    second = await upload(client, "race.txt", data)

    assert second.status_code == 200 and second.json()["duplicate"] is True  # was: a second row
    assert second.json()["document"]["id"] == first["document"]["id"]
    assert _files() == files_before  # the losing request's stored copy was removed


async def test_concurrent_new_versions_get_distinct_numbers(client: httpx.AsyncClient) -> None:
    v1 = (await upload(client, "contract.txt", _content("v1"))).json()["document"]
    responses = await asyncio.gather(
        *(upload(client, "contract.txt", _content(f"v{i}"), replaces_document_id=v1["id"]) for i in (2, 3))
    )
    assert all(r.status_code in (200, 202) for r in responses), [r.text for r in responses]
    versions = (await client.get(f"/documents/{v1['id']}/versions")).json()
    assert sorted(v["version"] for v in versions) == [1, 2, 3]
    assert sum(v["is_latest"] for v in versions) == 1


def test_database_rejects_duplicate_versions_and_two_latest_rows() -> None:
    with sync_session() as db:
        doc = db.execute(select(Document).where(Document.is_latest.is_(True)).limit(1)).scalar_one()
        group, version, owner = doc.version_group_id, doc.version, doc.owner_id
    insert = text(
        "INSERT INTO documents (id, owner_id, title, original_filename, extension, content_type, size_bytes, "
        "content_hash, storage_path, status, attempts, insights_status, chunk_count, token_count, "
        "version_group_id, version, is_latest, warnings) VALUES (:id, :owner, 't', 't.txt', '.txt', 'text/plain', "
        "1, :hash, 'x', 'ready', 0, 'ready', 0, 0, :group, :version, :latest, '[]')"
    )
    for version_number, latest in ((version, False), (version + 100, True)):
        with pytest.raises(IntegrityError), sync_session() as db:
            db.execute(
                insert,
                {
                    "id": uuid.uuid4(),
                    "owner": owner,
                    "hash": uuid.uuid4().hex,
                    "group": group,
                    "version": version_number,
                    "latest": latest,
                },
            )


# ------------------------------------------------------------------ custom summary race (#3)
async def test_identical_summary_requests_at_once_return_the_stored_one(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {"length": "medium", "tone": "simple", "focus": "race condition"}
    url = f"/documents/{ready_docs['quote']}/summaries"
    first = (await client.post(url, json=body)).json()

    real_find, calls = documents_service._find_summary, {"n": 0}

    async def check_misses_once(*args: Any) -> Any:
        calls["n"] += 1
        return None if calls["n"] == 1 else await real_find(*args)

    monkeypatch.setattr(documents_service, "_find_summary", check_misses_once)
    second = await client.post(url, json=body)
    assert second.status_code == 200  # was: 500 from the unique constraint
    assert second.json()["summary"] == first["summary"] and second.json()["cached"] is True


# ------------------------------------------------------------------ cost tracking (#15, #16, #17)
async def test_failed_calls_are_recorded(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def outage(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("provider down")

    monkeypatch.setattr(get_provider(), "complete_structured", outage)
    response = await upload(client, "noinsights.txt", _content("noinsights"))
    document_id = uuid.UUID(response.json()["document"]["id"])
    failed = _usage_rows(document_id=document_id, operation="insights")
    assert len(failed) == 1 and failed[0].success is False and failed[0].cost_usd == 0  # was: no row at all
    metrics = (await client.get("/metrics/ai-usage")).json()
    assert metrics["totals"]["failed_calls"] >= 1


async def test_usage_survives_a_later_rollback(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_store(db, document, chunks, vectors):  # type: ignore[no-untyped-def]
        db.execute(text("SELECT * FROM no_such_table"))

    monkeypatch.setattr(processing, "_store_chunks", broken_store)
    response = await upload(client, "paid.txt", _content("paid"))
    document_id = uuid.UUID(response.json()["document"]["id"])
    # The embedding call succeeded (and was paid for) before the failing INSERT; its row must remain.
    rows = _usage_rows(document_id=document_id, operation="embed_document")
    assert rows and rows[0].success is True and rows[0].prompt_tokens > 0


async def test_cache_hits_are_recorded_as_savings_not_spend(
    client: httpx.AsyncClient, ready_docs: dict[str, str]
) -> None:
    # Reprocessing re-embeds the same chunks: the second time they are all served by the embedding cache.
    for _ in range(2):
        await client.post(f"/documents/{ready_docs['agreement']}/reprocess")
    cached = _usage_rows(document_id=uuid.UUID(ready_docs["agreement"]), cached=True)
    assert cached and all(r.cost_usd == 0 and r.prompt_tokens > 0 for r in cached)

    session_id = (await client.post("/chat/sessions", json={"document_ids": [ready_docs["policy"]]})).json()["id"]
    for _ in range(2):
        await client.post(f"/chat/sessions/{session_id}/messages", json={"question": "What is the coinsurance?"})
    answer_hits = _usage_rows(session_id=uuid.UUID(session_id), operation="chat_answer", cached=True)
    assert len(answer_hits) == 1 and answer_hits[0].completion_tokens > 0

    metrics = (await client.get("/metrics/ai-usage")).json()
    assert metrics["cache_savings"]["hits"] >= 2 and metrics["cache_savings"]["tokens_saved"] > 0


async def test_unpriced_models_are_flagged(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    prices = {m: p for m, p in get_settings().model_prices.items() if m != "fake-chat"}
    monkeypatch.setattr(get_settings(), "model_prices", prices)  # e.g. a new model switched on without a price
    metrics = (await client.get("/metrics/ai-usage")).json()
    assert "fake-chat" in metrics["unpriced_models"]
    assert all(m["priced"] is False for m in metrics["by_model"] if m["model"] == "fake-chat")


def test_unpriced_model_warns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm.base import Usage
    from app.services import usage

    warnings: list[str] = []
    monkeypatch.setattr(usage.logger, "warning", lambda event, **kw: warnings.append(kw["model"]))
    usage._warn_unpriced.cache_clear()
    for _ in range(3):
        usage.estimate_cost_usd(Usage(model="brand-new-model", prompt_tokens=10))
    assert warnings == ["brand-new-model"]


async def test_new_version_keeps_the_document_title(client: httpx.AsyncClient) -> None:
    v1 = (await upload(client, "lease_agreement.txt", _content("lease v1"))).json()["document"]
    v2 = (
        await upload(client, "lease_agreement_FINAL_v2.txt", _content("lease v2"), replaces_document_id=v1["id"])
    ).json()["document"]
    assert v2["title"] == v1["title"] == "lease agreement"  # was "lease agreement FINAL v2"
    assert v2["original_filename"] == "lease_agreement_FINAL_v2.txt"  # the real file name is still kept

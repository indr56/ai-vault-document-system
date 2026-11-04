"""API integration tests: real Postgres + Redis, fake LLM, Celery tasks executed inline."""

import json

import httpx
import pytest

from tests.conftest import upload

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------------ auth & health
async def test_requires_api_key(client: httpx.AsyncClient) -> None:
    response = await client.get("/documents", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.json()["error"]["request_id"]


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("http://test/health")
    assert response.status_code == 200


# ------------------------------------------------------------------ documents
async def test_upload_processes_document_and_generates_insights(
    client: httpx.AsyncClient, ready_docs: dict[str, str]
) -> None:
    doc = (await client.get(f"/documents/{ready_docs['policy']}")).json()
    assert doc["status"] == "ready"
    assert doc["page_count"] == 3 and doc["chunk_count"] > 0
    insights = (await client.get(f"/documents/{ready_docs['policy']}/insights")).json()
    assert insights["insights_status"] == "ready"
    assert insights["summary"]
    assert any(f["page"] for f in insights["key_facts"])

    runs = (await client.get(f"/documents/{ready_docs['policy']}/processing-runs")).json()
    assert [s["stage"] for s in runs[0]["stages"]] == ["parse", "chunk", "embed", "analyze"]


async def test_duplicate_upload_returns_existing(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    response = await upload(client, "acme_property_policy_2025.pdf")
    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert response.json()["document"]["id"] == ready_docs["policy"]


@pytest.mark.parametrize(
    ("name", "content", "status", "code"),
    [
        ("malware.exe", b"MZ", 415, "unsupported_file_type"),
        ("fake.pdf", b"MZ not a pdf", 415, "invalid_file_content"),
        ("empty.txt", b"", 422, "empty_file"),
    ],
)
async def test_upload_validation(client: httpx.AsyncClient, name: str, content: bytes, status: int, code: str) -> None:
    response = await upload(client, name, content)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


async def test_scanned_pdf_fails_with_clear_error(client: httpx.AsyncClient) -> None:
    response = await upload(client, "edge-cases/scanned_no_text.pdf")
    doc = (await client.get(f"/documents/{response.json()['document']['id']}")).json()
    assert doc["status"] == "failed"
    assert doc["error"]["code"] == "no_extractable_text"


async def test_batch_upload_reports_per_file_errors(client: httpx.AsyncClient) -> None:
    files = [
        ("files", ("a.txt", b"Batch document alpha about renewal dates.")),
        ("files", ("b.exe", b"MZ")),
    ]
    body = (await client.post("/documents/batch", files=files)).json()
    assert len(body["results"]) == 1
    assert body["errors"][0]["code"] == "unsupported_file_type"


async def test_versioning_and_delete_promotes_previous(client: httpx.AsyncClient) -> None:
    v1 = (await upload(client, "v.txt", b"Policy wording version one. Deductible is $1,000.")).json()["document"]
    v2_resp = await upload(
        client, "v.txt", b"Policy wording version two. Deductible is $2,000.", replaces_document_id=v1["id"]
    )
    v2 = v2_resp.json()["document"]
    assert v2["version"] == 2 and v2["version_group_id"] == v1["version_group_id"]

    versions = (await client.get(f"/documents/{v2['id']}/versions")).json()
    assert [(v["version"], v["is_latest"]) for v in versions] == [(2, True), (1, False)]

    listed = (await client.get("/documents", params={"q": "v"})).json()["items"]
    assert v1["id"] not in [d["id"] for d in listed]  # old versions hidden by default

    assert (await client.delete(f"/documents/{v2['id']}")).status_code == 204
    assert (await client.get(f"/documents/{v1['id']}")).json()["is_latest"] is True


async def test_custom_summary_is_stored_and_reused(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    body = {"length": "short", "tone": "executive", "focus": "deductibles"}
    first = (await client.post(f"/documents/{ready_docs['policy']}/summaries", json=body)).json()
    second = (await client.post(f"/documents/{ready_docs['policy']}/summaries", json=body)).json()
    assert first["summary"] and first["cached"] is False
    assert second["cached"] is True and second["summary"] == first["summary"]


async def test_reprocess(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    doc = (await client.post(f"/documents/{ready_docs['agreement']}/reprocess")).json()
    assert doc["id"] == ready_docs["agreement"]
    after = (await client.get(f"/documents/{ready_docs['agreement']}")).json()
    assert after["status"] == "ready" and after["attempts"] == 2


# ------------------------------------------------------------------ chat
async def _session(client: httpx.AsyncClient, ids: list[str]) -> str:
    response = await client.post("/chat/sessions", json={"document_ids": ids})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _ask(client: httpx.AsyncClient, session_id: str, question: str) -> dict:
    response = await client.post(f"/chat/sessions/{session_id}/messages", json={"question": question})
    assert response.status_code == 200, response.text
    return response.json()["answer"]


async def test_chat_answers_with_citations(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    session_id = await _session(client, list(ready_docs.values()))
    answer = await _ask(client, session_id, "What is the flood deductible per occurrence?")
    assert answer["grounded"] is True and answer["abstained"] is False
    assert answer["citations"][0]["document_id"] in ready_docs.values()
    assert answer["follow_up_questions"]


async def test_chat_abstains_when_documents_do_not_cover_question(
    client: httpx.AsyncClient, ready_docs: dict[str, str]
) -> None:
    session_id = await _session(client, [ready_docs["agreement"]])
    answer = await _ask(client, session_id, "Zebra migration patterns Serengeti")
    assert answer["abstained"] is True and answer["citations"] == []


async def test_chat_multi_turn_cache_and_history(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    session_id = await _session(client, [ready_docs["policy"]])
    first = await _ask(client, session_id, "What is the earthquake deductible?")
    second = await _ask(client, session_id, "What is the earthquake deductible?")
    assert first["from_cache"] is False and second["from_cache"] is True
    history = (await client.get(f"/chat/sessions/{session_id}")).json()
    assert [m["role"] for m in history["messages"]] == ["user", "assistant", "user", "assistant"]


async def test_chat_streaming_sse(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    session_id = await _session(client, [ready_docs["quote"]])
    events: list[tuple[str, dict]] = []
    async with client.stream(
        "POST", f"/chat/sessions/{session_id}/messages/stream", json={"question": "What is the annual premium?"}
    ) as response:
        event = None
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                events.append((event, json.loads(line.split(":", 1)[1])))
    names = [e for e, _ in events]
    assert names[0] == "status" and "token" in names and names[-1] == "done"
    assert events[-1][1]["answer"]["role"] == "assistant"


async def test_chat_rejects_other_users_documents(client: httpx.AsyncClient) -> None:
    response = await client.post("/chat/sessions", json={"document_ids": ["00000000-0000-0000-0000-000000000000"]})
    assert response.status_code == 404


# ------------------------------------------------------------------ comparison & metrics
async def test_comparison(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    response = await client.post("/comparisons", json={"document_ids": [ready_docs["policy"], ready_docs["quote"]]})
    assert response.status_code == 202
    comparison = (await client.get(f"/comparisons/{response.json()['id']}")).json()
    assert comparison["status"] == "completed"
    assert set(comparison["result"]["labels"]) == {"A", "B"}


async def test_metrics(client: httpx.AsyncClient, ready_docs: dict[str, str]) -> None:
    docs = (await client.get("/metrics/documents")).json()
    assert docs["processing"]["runs"] >= 3
    assert "embed" in docs["processing"]["stages"]
    usage = (await client.get("/metrics/ai-usage")).json()
    assert usage["totals"]["calls"] > 0
    system = (await client.get("/metrics/system")).json()
    assert system["database"]["ok"] and system["redis"]["ok"]


async def test_rate_limit(client: httpx.AsyncClient) -> None:
    from app.core.errors import RateLimitedError
    from app.core.rate_limit import check_rate_limit

    await check_rate_limit("user-x", "test", 2)
    await check_rate_limit("user-x", "test", 2)
    with pytest.raises(RateLimitedError):
        await check_rate_limit("user-x", "test", 2)

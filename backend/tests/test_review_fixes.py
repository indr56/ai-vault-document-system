"""Fixes for gaps found in the second UI pass and an independent code review (#59 onwards)."""

import threading
import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.session import sync_session
from app.llm import get_provider
from app.llm.base import Usage
from app.llm.schemas import SectionNotes
from app.models import Document, DocumentInsight, ProcessingRun
from app.services import cache, insights, jobs, processing
from app.services.parsing import Page, ParsedDocument
from app.workers import tasks
from tests.conftest import upload


def _text(label: str) -> bytes:
    return f"{label} {uuid.uuid4()}. The water damage limit is $50,000 per occurrence.".encode()


# ------------------------------------------------------------------ batch upload with an early bad file (#59)
async def test_batch_with_a_bad_file_first_still_uploads_the_rest(client: httpx.AsyncClient) -> None:
    files = [("files", ("bad.exe", b"MZ" * 40)), ("files", ("good.txt", _text("good")))]
    response = await client.post("/documents/batch", files=files)
    assert response.status_code == 202, response.text  # was: 500 internal_error
    body = response.json()
    assert [e["filename"] for e in body["errors"]] == ["bad.exe"]
    assert [r["document"]["original_filename"] for r in body["results"]] == ["good.txt"]


# ------------------------------------------------------------------ body size before auth and parsing (#60)
async def test_an_oversized_upload_is_refused_before_auth_or_parsing(client: httpx.AsyncClient) -> None:
    huge = str(200 * 1024 * 1024)  # declared 200 MB; no body is sent at all
    response = await client.post(
        "/documents", headers={"X-API-Key": "", "Content-Length": huge, "Content-Type": "multipart/form-data; b=x"}
    )
    # 413, not 401: refused before the API key is even looked at, so nothing reaches the disk.
    assert response.status_code == 413 and response.json()["error"]["code"] == "payload_too_large"


async def test_a_body_without_a_length_is_cut_off_at_the_limit(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import body_limit

    monkeypatch.setattr(body_limit, "MULTIPART_OVERHEAD", 0)
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    head = b'--B\r\nContent-Disposition: form-data; name="file"; filename="big.txt"\r\n\r\n'

    async def stream() -> AsyncIterator[bytes]:  # chunked: no Content-Length to check up front
        yield head
        for _ in range(8):  # 4 MB of a well-formed file part against a 1 MB limit
            yield b"x" * (512 * 1024)
        yield b"\r\n--B--\r\n"

    response = await client.post(
        "/documents", content=stream(), headers={"Content-Type": "multipart/form-data; boundary=B"}
    )
    assert response.status_code == 413 and response.json()["error"]["code"] == "payload_too_large"


# ------------------------------------------------------------------ one error format (#68)
async def test_framework_errors_use_the_standard_envelope(client: httpx.AsyncClient) -> None:
    missing = await client.get("/no-such-route")
    wrong_method = await client.put("/documents")
    unreadable = await client.post(
        "/documents", content=b"--x\r\nbroken", headers={"Content-Type": "multipart/form-data"}
    )
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "not_found")
    assert (wrong_method.status_code, wrong_method.json()["error"]["code"]) == (405, "method_not_allowed")
    assert unreadable.json()["error"]["code"] in ("bad_request", "validation_failed")  # was {"detail": ...}


# ------------------------------------------------------------------ jobs: reprocess, abandoned insights, backlog (#61, #64-#67)
def _age(model, item_id: uuid.UUID, minutes: int, **values: Any) -> None:  # type: ignore[no-untyped-def]
    with sync_session() as db:
        db.execute(
            update(model).where(model.id == item_id).values(updated_at=utcnow() - timedelta(minutes=minutes), **values)
        )


def _doc(document_id: uuid.UUID) -> Document:
    with sync_session() as db:
        doc = db.get(Document, document_id)
        assert doc is not None
        db.expunge(doc)
        return doc


async def _ready_doc(client: httpx.AsyncClient, label: str) -> uuid.UUID:
    return uuid.UUID((await upload(client, f"{label}.txt", _text(label))).json()["document"]["id"])


async def test_reprocess_is_refused_while_a_job_holds_the_document(client: httpx.AsyncClient) -> None:
    doc_id = await _ready_doc(client, "analyzing")
    with jobs.redis_lock(jobs.document_lock_key(doc_id), 60) as acquired:  # e.g. insights still being written
        assert acquired
        response = await client.post(f"/documents/{doc_id}/reprocess")
    assert response.status_code == 409 and response.json()["error"]["code"] == "already_processing"
    assert _doc(doc_id).status == "ready"  # was: "queued", then nothing ran for 10-15 minutes


def test_a_locked_job_for_a_queued_document_is_sent_again(monkeypatch: pytest.MonkeyPatch) -> None:
    resent: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tasks.process_document, "apply_async", lambda args, countdown: resent.append((args, countdown)))
    with sync_session() as db:
        doc_id = db.scalar(select(Document.id).limit(1))
    _age(Document, doc_id, 0, status="queued")  # Reprocess got through just before the lock was taken
    try:
        with jobs.redis_lock(jobs.document_lock_key(doc_id), 60):
            assert tasks.process_document.apply(args=(str(doc_id),)).result == "skipped_locked"
        assert resent == [((str(doc_id),), tasks.LOCKED_RETRY_SECONDS)]  # was: dropped
    finally:
        _age(Document, doc_id, 0, status="ready")


async def test_abandoned_insights_are_failed_not_left_pending(client: httpx.AsyncClient) -> None:
    crashed, busy = await _ready_doc(client, "crashed-analysis"), await _ready_doc(client, "busy-analysis")
    for doc_id in (crashed, busy):  # the worker died during analysis: still "pending" 30 minutes later
        _age(Document, doc_id, 30, insights_status="pending", stage="analyze")
    with sync_session() as db:
        run = ProcessingRun(document_id=crashed, owner_id=_doc(crashed).owner_id, attempt=1)
        db.add(run)
        db.flush()
        run_id = run.id
        db.execute(
            update(ProcessingRun).where(ProcessingRun.id == run_id).values(started_at=utcnow() - timedelta(minutes=30))
        )

    with jobs.redis_lock(jobs.document_lock_key(busy), 60):  # a job really is working on this one
        tasks.requeue_stale_documents()

    assert _doc(crashed).insights_status == "failed" and _doc(crashed).stage is None  # "Retry insights" appears
    assert _doc(busy).insights_status == "pending"
    with sync_session() as db:
        closed = db.get(ProcessingRun, run_id)
        assert closed is not None and (closed.status, closed.error_code) == ("failed", "interrupted")


async def test_a_long_backlog_is_not_sent_twice(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(tasks, "enqueue_document", lambda _id: None)
    behind = await _ready_doc(client, "behind-a-backlog")
    monkeypatch.setattr(tasks, "enqueue_document", lambda doc_id: sent.append(str(doc_id)))
    _age(Document, behind, 45, status="queued")  # waiting longer than the 30-minute marker lives
    jobs.clear_enqueued("document", behind)

    monkeypatch.setattr(tasks, "queue_backlog", lambda kind: 500)
    tasks.requeue_stale_documents()
    assert sent == [] and jobs.waiting_in_queue("document", [behind]) == {behind}  # marker given back

    jobs.clear_enqueued("document", behind)
    monkeypatch.setattr(tasks, "queue_backlog", lambda kind: 0)  # queue drained and it's still waiting: lost
    tasks.requeue_stale_documents()
    assert sent == [str(behind)]
    _age(Document, behind, 0, status="ready")


async def test_manual_reprocess_resets_the_attempt_budget(client: httpx.AsyncClient) -> None:
    doc_id = await _ready_doc(client, "many-retries")
    _age(Document, doc_id, 0, attempts=4)  # 4 earlier reprocesses
    await client.post(f"/documents/{doc_id}/reprocess")
    assert _doc(doc_id).attempts == 1  # was 5: one worker crash would then fail it as "interrupted repeatedly"


async def test_a_new_version_is_allowed_at_the_document_limit(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = await _ready_doc(client, "at-the-limit")
    with sync_session() as db:
        owner = _doc(doc_id).owner_id
        count = db.scalar(select(func.count()).where(Document.owner_id == owner, Document.is_latest.is_(True)))
    monkeypatch.setattr(get_settings(), "max_documents_per_user", count)
    new_document = await upload(client, "another.txt", _text("another"))
    new_version = await upload(client, "at-the-limit.txt", _text("v2"), replaces_document_id=str(doc_id))
    assert new_document.status_code == 409
    assert new_version.status_code == 202  # was 409 document_limit_reached


async def test_regenerating_insights_never_stays_pending(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = await _ready_doc(client, "parser-bug")

    def parser_bug(*args: Any) -> None:
        raise ValueError("unexpected structure")  # not a ParseError or OSError

    monkeypatch.setattr(processing, "parse_document", parser_bug)
    await client.post(f"/documents/{doc_id}/insights/regenerate")
    assert _doc(doc_id).insights_status == "failed"  # was: the exception escaped and it stayed "pending"


# ------------------------------------------------------------------ long documents: parallel map, summaries (#62, #70)
def _long(pages: int, words_per_page: int) -> ParsedDocument:
    salt = uuid.uuid4().hex[:6]  # fresh text, so no notes come from the cache of an earlier run
    return ParsedDocument(
        pages=[Page(i, " ".join(f"clause{i}w{j}{salt}" for j in range(words_per_page))) for i in range(1, pages + 1)]
    )


def _fake_notes(monkeypatch: pytest.MonkeyPatch, delay: float = 0.0) -> dict[str, int]:
    provider = get_provider()
    seen = {"calls": 0, "running": 0, "max_running": 0}
    lock = threading.Lock()

    def notes(messages, response_model, **kwargs):  # type: ignore[no-untyped-def]
        with lock:
            seen["calls"] += 1
            seen["running"] += 1
            seen["max_running"] = max(seen["max_running"], seen["running"])
        time.sleep(delay)
        with lock:
            seen["running"] -= 1
        section = messages[-1]["content"]
        usage = Usage(provider.fast_model, prompt_tokens=100, completion_tokens=50)
        return type("R", (), {"value": SectionNotes(notes=section[:300]), "usage": usage})()

    monkeypatch.setattr(provider, "complete_structured", notes)
    return seen


async def test_map_calls_run_in_parallel_and_keep_their_order(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = await _ready_doc(client, "very-long")
    document = _doc(doc_id)
    seen = _fake_notes(monkeypatch, delay=0.2)
    parsed = _long(pages=16, words_per_page=150)
    # Sized from the generated text (its random salt changes the token count): one page fits a
    # section, two don't, so there are always 16 sections.
    page_tokens = [insights.count_tokens(f"[Page {p.number}]\n{p.text}") for p in parsed.pages]
    section = max(page_tokens) + 10
    assert min(page_tokens) * 2 > section
    monkeypatch.setattr(insights, "SECTION_TOKENS", section)
    monkeypatch.setattr(get_settings(), "insights_max_input_tokens", sum(page_tokens) // 2)  # too long to read whole
    assert len(insights._split_sections(parsed, section)) == 16

    started = time.perf_counter()
    text = insights.prepare_analysis_text(document, parsed)
    elapsed = time.perf_counter() - started

    assert seen["calls"] == 16 and seen["max_running"] == 8  # was 1: one call after another
    assert elapsed < 16 * 0.2 / 2  # 2 rounds of 8 (~0.4 s), not 16 in a row (3.2 s)
    order = [text.index(f"clause{page}w0") for page in range(1, 17)]
    assert order == sorted(order)  # notes stay in page order


async def test_custom_summary_of_a_long_document_reuses_the_saved_notes(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = await _ready_doc(client, "long-summary")
    with sync_session() as db:
        db.execute(update(DocumentInsight).where(DocumentInsight.document_id == doc_id).values(analysis_text="NOTES"))
    monkeypatch.setattr(get_settings(), "insights_max_input_tokens", 5)  # "long": too big to read whole
    seen = _fake_notes(monkeypatch)
    sent: list[str] = []
    real = get_provider().acomplete

    async def capture(messages, **kwargs):  # type: ignore[no-untyped-def]
        sent.append(messages[-1]["content"])
        return await real(messages, **kwargs)

    monkeypatch.setattr(get_provider(), "acomplete", capture)
    response = await client.post(f"/documents/{doc_id}/summaries", json={"length": "short", "tone": "simple"})
    assert response.status_code == 200, response.text
    assert seen["calls"] == 0 and "NOTES" in sent[0]  # was: the whole map step re-ran inside the request

    with sync_session() as db:  # insights never finished, so there are no saved notes
        db.execute(update(DocumentInsight).where(DocumentInsight.document_id == doc_id).values(analysis_text=None))
    again = await client.post(f"/documents/{doc_id}/summaries", json={"length": "long", "tone": "simple"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "insights_required"
    assert seen["calls"] == 0


async def test_insights_of_a_long_document_save_the_notes(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "insights_max_input_tokens", 5)
    doc_id = await _ready_doc(client, "saved-notes")
    with sync_session() as db:
        insight = db.get(DocumentInsight, doc_id)
        assert insight is not None and insight.analysis_text  # the condensed notes, kept for summaries


# ------------------------------------------------------------------ similar-question cache (#63)
@pytest.mark.parametrize(
    ("asked", "cached", "same"),
    [
        ("What is the 2026 premium?", "What is the 2025 premium?", False),
        ("What is the deductible for Northwind?", "What is the deductible for Acme?", False),
        ("Is the limit $5,000,000?", "Is the limit $500,000?", False),
        ("What's the flood deductible amount?", "What is the flood deductible?", True),
        ("How much is the 2025 premium?", "What is the premium for 2025?", True),
    ],
)
def test_cached_answers_need_the_same_numbers_and_names(asked: str, cached: str, same: bool) -> None:
    assert (cache.specifics(asked) == cache.specifics(cached)) is same


async def test_a_question_about_another_year_is_not_served_from_cache() -> None:
    index = f"answer-index:test:{uuid.uuid4()}"
    vector = [1.0] + [0.0] * 1535  # identical embeddings: only the specifics tell them apart
    await cache.aadd_semantic_entry(index, "What is the 2025 premium?", vector, "answer:2025")
    assert await cache.afind_semantic(index, "What is the 2026 premium?", vector) is None  # was: the 2025 answer
    hit = await cache.afind_semantic(index, "What's the 2025 premium?", vector)
    assert hit is not None and hit[0] == "answer:2025"


# ------------------------------------------------------------------ download the original (#76)
async def test_the_original_file_can_be_downloaded(client: httpx.AsyncClient) -> None:
    data = _text("download me")
    doc_id = (await upload(client, "Lease Agreement (signed).txt", data)).json()["document"]["id"]
    response = await client.get(f"/documents/{doc_id}/file")
    assert response.status_code == 200 and response.content == data
    assert "filename*=utf-8''Lease%20Agreement%20%28signed%29.txt" in response.headers["content-disposition"]
    missing = await client.get(f"/documents/{uuid.uuid4()}/file")
    assert missing.status_code == 404


# ------------------------------------------------------------------ after documents are deleted (#77, #78)
async def test_comparisons_can_be_deleted_but_not_while_running(
    client: httpx.AsyncClient, ready_docs: dict[str, str]
) -> None:
    body = {"document_ids": [ready_docs["policy"], ready_docs["quote"]]}
    done = (await client.post("/comparisons", json=body)).json()["id"]
    running = (await client.post("/comparisons", json=body)).json()["id"]
    with jobs.redis_lock(jobs.comparison_lock_key(running), 60):  # a worker is writing its result
        refused = await client.delete(f"/comparisons/{running}")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "comparison_running"

    assert (await client.delete(f"/comparisons/{done}")).status_code == 204  # was: no way to remove one
    assert done not in [c["id"] for c in (await client.get("/comparisons")).json()]
    assert (await client.delete(f"/comparisons/{done}")).status_code == 404


async def test_a_chat_whose_documents_were_deleted_says_so(client: httpx.AsyncClient) -> None:
    doc_id = await _ready_doc(client, "deleted-later")
    session_id = (await client.post("/chat/sessions", json={"document_ids": [str(doc_id)]})).json()["id"]
    await client.delete(f"/documents/{doc_id}")
    response = await client.post(f"/chat/sessions/{session_id}/messages", json={"question": "Anything?"})
    # Was 409 document_not_ready: "Wait for processing to finish", which would never happen.
    assert response.status_code == 409 and response.json()["error"]["code"] == "chat_documents_deleted"


# ------------------------------------------------------------------ a "not found" answer that cites (#80)
async def test_a_not_found_answer_keeps_the_sources_it_refers_to(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.llm.base import Completion
    from app.llm.prompts.chat import ABSTAIN_PREFIX

    provider = get_provider()

    async def abstains_with_a_mark(messages, **kwargs):  # type: ignore[no-untyped-def]
        # What the real model wrote on 9 Nov: it says what it did find, with a source mark.
        return Completion(
            f"{ABSTAIN_PREFIX} The policy names the insurer [1], but no phone number is given.",
            Usage(provider.chat_model, 900, 30),
        )

    monkeypatch.setattr(provider, "acomplete", abstains_with_a_mark)
    session_id = (await client.post("/chat/sessions", json={"document_ids": [ready_docs["policy"]]})).json()["id"]
    response = await client.post(
        f"/chat/sessions/{session_id}/messages", json={"question": "What is the flood deductible phone number?"}
    )
    answer = response.json()["answer"]
    assert answer["abstained"] is True and answer["grounded"] is False  # still a "not found"
    assert [c["number"] for c in answer["citations"]] == [1]  # was []: the [1] in the text led nowhere
    assert answer["citations"][0]["document_id"] == ready_docs["policy"]


# ------------------------------------------------------------------ error responses in /docs (#81)
def test_every_endpoint_documents_its_errors_with_one_schema() -> None:
    from app.main import app

    spec = app.openapi()
    ref = "#/components/schemas/ErrorResponse"
    missing = []
    for path, operations in spec["paths"].items():
        if not path.startswith("/api/v1"):
            continue
        for method, operation in operations.items():
            expected = {"401", "422", "429", "500"} | ({"404"} if "{" in path else set())
            for code in expected:
                schema = operation["responses"].get(code, {}).get("content", {}).get("application/json", {})
                if schema.get("schema", {}).get("$ref") != ref:
                    missing.append(f"{method.upper()} {path} {code}")
    assert missing == []  # was: every error "Undocumented" in /docs
    upload = spec["paths"]["/api/v1/documents"]["post"]["responses"]
    assert {"409", "413", "415"} <= upload.keys()
    assert set(spec["components"]["schemas"]["ErrorBody"]["properties"]) == {"code", "message", "details", "request_id"}


# ------------------------------------------------------------------ comparing versions (#86)
def test_version_changes_on_the_sample_renewal() -> None:
    from app.services.comparison import version_changes
    from app.services.parsing import parse_document
    from tests.conftest import SAMPLES

    def text(name: str) -> str:
        return " ".join(p.text for p in parse_document(SAMPLES / name, ".pdf").pages)

    renewal, original = text("acme_property_policy_2026_renewal.pdf"), text("acme_property_policy_2025.pdf")
    changes = "\n".join(version_changes(renewal, original, "A", "B"))
    # Every real change between the two PDFs, including the two the model kept missing.
    for expected in ['"$42,300" -> A: "$48,750"', '"$25,000" -> A: "$50,000"', '"$12,000,000" -> A: "$12,500,000"']:
        assert expected in changes
    assert 'only in A: "(g) Cyber incident' in changes and "Endorsement CP-27: Terrorism" in changes
    assert "Coinsurance" not in changes  # unchanged text is not listed
    assert version_changes(original, original, "A", "B") == []


async def test_versions_get_the_change_list_and_same_value_rows_are_dropped(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.llm.base import StructuredCompletion
    from app.llm.schemas import ComparedValue, ComparisonOutput, Difference

    v1 = (await upload(client, "lease.txt", b"Lease. Monthly rent is $18,500. Notice period is 90 days.")).json()
    v1_id = v1["document"]["id"]
    v2 = await upload(
        client, "lease.txt", b"Lease. Monthly rent is $19,750. Notice period is 90 days.", replaces_document_id=v1_id
    )
    other = await upload(client, "other.txt", _text("other"))
    seen: list[str] = []
    provider = get_provider()

    def fake_compare(messages, response_model, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(messages[-1]["content"])
        rows = [
            Difference(
                aspect="Rent",
                values=[
                    ComparedValue(document_label="A", value="$19,750", page=None),
                    ComparedValue(document_label="B", value="$18,500", page=None),
                ],
                significance="high",
                explanation="Up",
            ),
            Difference(
                aspect="Notice period",
                values=[
                    ComparedValue(document_label="A", value="90 days", page=None),
                    ComparedValue(document_label="B", value="90 days.", page=None),
                ],
                significance="low",
                explanation="No change",
            ),
        ]
        value = ComparisonOutput(overview="o", documents=[], similarities=[], differences=rows, gaps_or_risks=[])
        return StructuredCompletion(value, Usage(provider.chat_model, 500, 100))

    monkeypatch.setattr(provider, "complete_structured", fake_compare)
    versions = (await client.post("/comparisons", json={"document_ids": [v2.json()["document"]["id"], v1_id]})).json()
    result = (await client.get(f"/comparisons/{versions['id']}")).json()["result"]
    assert "Changes between versions" in seen[0] and '"$18,500." -> A: "$19,750."' in seen[0]
    assert [d["aspect"] for d in result["differences"]] == ["Rent"]  # was: "Notice period: 90 days | 90 days"

    await client.post("/comparisons", json={"document_ids": [v1_id, other.json()["document"]["id"]]})
    assert "Changes between versions" not in seen[1]  # different documents: material unchanged

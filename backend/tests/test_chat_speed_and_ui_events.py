"""Chat efficiency (rewrite skip, parallel and smaller follow-ups), skipped documents, live chat
events, long-document map-reduce and re-uploading a failed file."""

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from app.db.session import sync_session
from app.llm import get_provider
from app.llm.prompts import followups as followups_prompt
from app.llm.schemas import FollowUpQuestions, SectionNotes
from app.models import AIUsage, Document
from app.services import events
from app.services.chat import needs_rewrite
from tests.conftest import upload


def _content(label: str) -> bytes:
    return f"{label} {uuid.uuid4()}. The windstorm deductible is 2% of the building limit.".encode()


async def _session(client: httpx.AsyncClient, ids: list[str]) -> str:
    response = await client.post("/chat/sessions", json={"document_ids": ids})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _ask(client: httpx.AsyncClient, session_id: str, question: str) -> dict:
    response = await client.post(f"/chat/sessions/{session_id}/messages", json={"question": question})
    assert response.status_code == 200, response.text
    return response.json()["answer"]


def _operations(session_id: str) -> list[str]:
    with sync_session() as db:
        return list(db.execute(select(AIUsage.operation).where(AIUsage.session_id == uuid.UUID(session_id))).scalars())


# ------------------------------------------------------------------ rewrite only when needed (#5)
@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What is the earthquake deductible?", False),
        ("When must a loss be reported to the insurer?", False),
        ("What does it cover?", True),  # "it"
        ("And for fire?", True),  # continuation + short
        ("What about the GL quote?", True),
        ("Is that per occurrence?", True),
        ("the deductible?", True),  # too short to stand alone
    ],
)
def test_needs_rewrite(question: str, expected: bool) -> None:
    assert needs_rewrite(question) is expected


async def test_standalone_follow_up_skips_the_rewrite_call(
    client: httpx.AsyncClient, ready_docs: dict[str, str]
) -> None:
    session_id = await _session(client, [ready_docs["policy"]])
    await _ask(client, session_id, "What is the earthquake deductible?")
    await _ask(client, session_id, "What is the coinsurance percentage?")  # has history, but standalone
    assert "rewrite_query" not in _operations(session_id)
    await _ask(client, session_id, "And what about windstorm?")
    assert _operations(session_id).count("rewrite_query") == 1


# ------------------------------------------------------------------ follow-ups: parallel and smaller (#6, #4)
async def test_follow_ups_run_while_the_answer_is_generated(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = get_provider()
    real_answer, real_structured = provider.acomplete, provider.acomplete_structured
    follow_ups_started = asyncio.Event()
    seen: dict[str, Any] = {}

    async def answer_waits_for_follow_ups(messages, **kwargs):  # type: ignore[no-untyped-def]
        # Sequential code would deadlock here (follow-ups only started after the answer): time out.
        await asyncio.wait_for(follow_ups_started.wait(), timeout=2)
        return await real_answer(messages, **kwargs)

    async def structured(messages, response_model, **kwargs):  # type: ignore[no-untyped-def]
        if response_model is FollowUpQuestions:
            seen["prompt"] = messages[-1]["content"]
            follow_ups_started.set()
        return await real_structured(messages, response_model, **kwargs)

    monkeypatch.setattr(provider, "acomplete", answer_waits_for_follow_ups)
    monkeypatch.setattr(provider, "acomplete_structured", structured)
    answer = await _ask(client, await _session(client, list(ready_docs.values())), "What is the flood deductible?")
    assert answer["follow_up_questions"]

    excerpts = seen["prompt"].split("Source excerpts:\n", 1)[1]
    assert len(excerpts) <= followups_prompt.MAX_SOURCE_CHARS  # was up to 6,000
    assert "[4]" not in excerpts  # top 3 sources only
    assert "Answer:" not in seen["prompt"]  # built without the answer, so it can run in parallel


# ------------------------------------------------------------------ skipped documents (#34)
async def test_answer_reports_documents_that_were_not_ready(
    client: httpx.AsyncClient, ready_docs: dict[str, str]
) -> None:
    pending = (await upload(client, "pending.txt", _content("pending"))).json()["document"]["id"]
    with sync_session() as db:
        db.execute(update(Document).where(Document.id == uuid.UUID(pending)).values(status="processing"))
    session_id = await _session(client, [ready_docs["policy"], pending])

    # The skipped document is named in the stream's status event (shown in the UI) ...
    statuses = []
    async with client.stream(
        "POST", f"/chat/sessions/{session_id}/messages/stream", json={"question": "What is the flood deductible?"}
    ) as response:
        event = None
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event == "status":
                statuses.append(json.loads(line.split(":", 1)[1]))
    generating = next(s for s in statuses if s["stage"] == "generating")
    assert [d["document_id"] for d in generating["skipped_documents"]] == [pending]  # was: silently skipped

    # ... and stored with the answer.
    with sync_session() as db:
        from app.models import ChatMessage

        message = db.execute(
            select(ChatMessage).where(ChatMessage.session_id == uuid.UUID(session_id), ChatMessage.role == "assistant")
        ).scalar_one()
        assert message.retrieval["skipped_documents"][0]["status"] == "processing"


# ------------------------------------------------------------------ live chat events (#23)
async def test_chat_events_are_published(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[str] = []
    real = events.apublish

    async def capture(owner_id, event_type, payload):  # type: ignore[no-untyped-def]
        published.append(event_type)
        await real(owner_id, event_type, payload)

    monkeypatch.setattr(events, "apublish", capture)
    session_id = await _session(client, [ready_docs["quote"]])
    await _ask(client, session_id, "What is the annual premium?")
    await client.delete(f"/chat/sessions/{session_id}")
    assert published == ["chat.session_created", "chat.message_created", "chat.session_deleted"]


# ------------------------------------------------------------------ long documents (#41)
def _long_document(pages: int, words_per_page: int):  # type: ignore[no-untyped-def]
    from app.services.parsing import Page, ParsedDocument

    return ParsedDocument(
        pages=[Page(i, " ".join(f"clause{i}w{j}" for j in range(words_per_page))) for i in range(1, pages + 1)]
    )


async def test_long_document_map_reduce_fits_budget_and_reuses_notes(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings
    from app.services import insights

    doc = (await upload(client, "long.txt", _content("long"))).json()["document"]
    with sync_session() as db:
        document = db.get(Document, uuid.UUID(doc["id"]))
        db.expunge(document)

    provider = get_provider()
    calls = {"map": 0}

    def notes(messages, response_model, **kwargs):  # type: ignore[no-untyped-def]
        assert response_model is SectionNotes
        calls["map"] += 1
        section = messages[-1]["content"]
        return type("R", (), {"value": SectionNotes(notes=section[:400]), "usage": _usage()})()

    def _usage():  # type: ignore[no-untyped-def]
        from app.llm.base import Usage

        return Usage(provider.fast_model, prompt_tokens=100, completion_tokens=50)

    monkeypatch.setattr(provider, "complete_structured", notes)
    monkeypatch.setattr(get_settings(), "insights_max_input_tokens", 1_500)
    monkeypatch.setattr(insights, "SECTION_TOKENS", 1_000)
    # 30 pages of ~1,200 tokens: each page is bigger than a section (split), and the level-1 notes
    # (~60 x ~150 tokens) are bigger than the budget, so a second reduce level is needed.
    parsed = _long_document(pages=30, words_per_page=300)

    text = insights.prepare_analysis_text(document, parsed)
    first_calls = calls["map"]
    assert insights.count_tokens(text) <= 1_500  # was: level-1 notes joined without a size check
    level_1 = len(insights._split_sections(parsed, 1_000))
    assert level_1 > 30  # each oversized page was split into pieces
    assert first_calls > level_1  # a second (reduce) level ran on the notes

    insights.prepare_analysis_text(document, parsed)  # e.g. a custom summary of the same document
    assert calls["map"] == first_calls  # all notes came from the cache: no new map calls


# ------------------------------------------------------------------ re-uploading a failed file (#42)
async def test_reuploading_a_failed_file_reprocesses_it(client: httpx.AsyncClient) -> None:
    data = _content("retry-upload")
    doc_id = (await upload(client, "flaky.txt", data)).json()["document"]["id"]
    with sync_session() as db:
        db.execute(
            update(Document)
            .where(Document.id == uuid.UUID(doc_id))
            .values(status="failed", error_code="processing_error", error_message="boom")
        )
    response = await upload(client, "flaky.txt", data)
    body = response.json()
    assert response.status_code == 202 and body["duplicate"] is True
    assert "processing it again" in body["message"]  # was: "Identical file already in your vault"
    assert (await client.get(f"/documents/{doc_id}")).json()["status"] == "ready"


# ------------------------------------------------------------------ follow-ups that restate the answer (#46)
async def test_follow_ups_that_restate_the_answer_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm.base import Embeddings, Usage
    from app.services import chat as chat_service

    answer = "The flood deductible is $50,000 per occurrence [1]."
    candidates = [
        "Is the flood deductible per occurrence?",  # restates the answer
        "What is the flood annual aggregate limit?",
        "What is the deductible for all other perils?",
        "Is the flood deductible per occurrence or annual aggregate?",  # restates the answer
        "What is the earthquake deductible minimum amount?",
    ]
    # Similarities to the answer as measured with text-embedding-3-small on 9 Nov:
    # 0.841, 0.603, 0.567, 0.807, 0.539 -> unit vectors whose dot product with the answer equals them.
    similarity = [0.841, 0.603, 0.567, 0.807, 0.539]

    def vec(s: float) -> list[float]:
        return [s, (1 - s * s) ** 0.5]

    async def fake_embed(texts: list[str]) -> Embeddings:
        assert texts == [answer, *candidates]  # one call: the answer plus every candidate
        return Embeddings([[1.0, 0.0], *[vec(s) for s in similarity]], Usage("emb", prompt_tokens=60))

    monkeypatch.setattr(get_provider(), "aembed", fake_embed)
    prepared = chat_service.PreparedQuestion(
        question="How much is the flood deductible?",
        standalone_question="How much is the flood deductible?",
        documents=[],
        history=[],
    )
    picked = await chat_service.pick_follow_ups(prepared, answer, candidates)
    assert picked == [  # the 3 least similar to the answer, in the model's order
        "What is the flood annual aggregate limit?",
        "What is the deductible for all other perils?",
        "What is the earthquake deductible minimum amount?",
    ]
    assert [u.operation for u in prepared.usages] == ["embed_follow_ups"]


async def test_follow_ups_shown_unfiltered_if_ranking_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import chat as chat_service

    async def broken(texts: list[str]) -> None:
        raise RuntimeError("embeddings down")

    monkeypatch.setattr(get_provider(), "aembed", broken)
    prepared = chat_service.PreparedQuestion(question="q", standalone_question="q", documents=[], history=[])
    picked = await chat_service.pick_follow_ups(prepared, "answer", ["a?", "b?", "c?", "d?"])
    assert picked == ["a?", "b?", "c?"]  # suggestions still shown; the failed call is recorded
    assert prepared.usages[0].success is False


@pytest.mark.parametrize(
    ("suggestion", "asked", "expected"),
    [
        ("How long must damaged stock be kept for insurer inspection?", ["How long must damaged stock be kept?"], True),
        ("What is the flood deductible?", ["How much is the flood deductible?"], True),
        ("Who must be told if losses exceed $250,000?", ["How long must damaged stock be kept?"], False),
        ("What is the earthquake deductible?", ["What is the flood deductible?"], False),
    ],
)
def test_suggestions_already_asked_in_the_chat_are_skipped(suggestion: str, asked: list[str], expected: bool) -> None:
    from app.services.chat import _already_asked

    assert _already_asked(suggestion, asked) is expected

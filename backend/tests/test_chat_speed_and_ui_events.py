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
from app.llm.base import StreamEvent
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


# ------------------------------------------------------------------ chat answers think briefly first (chat-v7)
async def test_only_chat_answers_ask_for_reasoning(
    client: httpx.AsyncClient, ready_docs: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = get_provider()
    real_complete = provider.acomplete
    calls: list[tuple[str, str | None]] = []

    async def complete(messages, **kwargs):  # type: ignore[no-untyped-def]
        kind = "answer" if "Sources:" in messages[-1]["content"] else "other"
        calls.append((kind, kwargs.get("reasoning_effort")))
        return await real_complete(messages, **kwargs)

    async def stream(messages, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(("stream", kwargs.get("reasoning_effort")))
        completion = await real_complete(messages)  # the fake streams via its own acomplete; bypass the spy
        yield StreamEvent(delta=completion.text)
        yield StreamEvent(usage=completion.usage)

    monkeypatch.setattr(provider, "acomplete", complete)
    monkeypatch.setattr(provider, "astream", stream)
    session_id = await _session(client, [ready_docs["policy"]])
    await _ask(client, session_id, "What is the earthquake deductible?")
    await _ask(client, session_id, "And for flood?")  # needs a rewrite: a non-answer call
    response = await client.post(
        f"/chat/sessions/{session_id}/messages/stream", json={"question": "What is the coinsurance?"}
    )
    assert response.status_code == 200

    assert ("answer", "low") in calls and ("stream", "low") in calls
    assert ("other", None) in calls  # the rewrite keeps temperature 0, no reasoning
    assert all(effort == "low" for kind, effort in calls if kind in ("answer", "stream"))
    assert all(effort is None for kind, effort in calls if kind == "other")


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
# The deductibles answer from the 9 Nov walkthrough (3-document chat), and the suggestions it got.
DEDUCTIBLES_ANSWER = """- Property policy deductibles:
  - "All other perils deductible: $10,000 per occurrence." [2]
  - "Flood deductible: $50,000 per occurrence." [2]
  - "Earthquake deductible: 5% of the total insured value at the affected location, subject to a minimum of $100,000." [2]
  - "Windstorm deductible: 2% of the building limit." [2]

- General liability quote deductible:
  - "Deductible: $2,500 per claim (bodily injury and property damage)." [1]"""


def _prepared():  # type: ignore[no-untyped-def]
    from app.services import chat as chat_service

    return chat_service.PreparedQuestion(
        question="And what about the deductibles?",
        standalone_question="What are the deductibles for the property policy and the general liability quote?",
        documents=[],
        history=[],
    )


def _checks(
    monkeypatch: pytest.MonkeyPatch,
    answered: list[int] | None,
    closeness: list[float] | None,
    to_asked: list[float] | None = None,
    seen: dict | None = None,
) -> None:
    """Fake the two checks: the model's list of answered suggestions (1-based), and each suggestion's
    similarity to its closest answer line (`closeness`) and to the closest question already asked
    (`to_asked`, default 0). None makes that call fail. `seen` records what the model was shown."""
    from app.llm.base import Embeddings, StructuredCompletion, Usage
    from app.llm.schemas import AnsweredSuggestions
    from app.services import chat as chat_service

    provider = get_provider()

    async def judge(messages, response_model, **kwargs):  # type: ignore[no-untyped-def]
        assert response_model is AnsweredSuggestions
        if seen is not None:
            seen["prompt"] = messages[-1]["content"]
        if answered is None:
            raise RuntimeError("model down")
        return StructuredCompletion(AnsweredSuggestions(answered=answered), Usage(provider.fast_model, 300, 10))

    async def embed(texts: list[str]) -> Embeddings:
        if closeness is None:
            raise RuntimeError("embeddings down")
        lines = chat_service.answer_lines(DEDUCTIBLES_ANSWER)
        n_asked = len(texts) - len(lines) - len(closeness)
        assert texts[: len(lines)] == lines  # one call: the answer's lines, the questions asked, the suggestions
        asked_sims = to_asked or [0.0] * len(closeness)
        vectors = [[1.0, 0.0, 0.0]] * len(lines) + [[0.0, 1.0, 0.0]] * n_asked
        vectors += [
            [line, asked, max(0.0, 1 - line * line - asked * asked) ** 0.5]
            for line, asked in zip(closeness, asked_sims, strict=True)
        ]
        return Embeddings(vectors, Usage("emb", prompt_tokens=80))

    monkeypatch.setattr(provider, "acomplete_structured", judge)
    monkeypatch.setattr(provider, "aembed", embed)


WALKTHROUGH_SUGGESTIONS = [
    "What is the general liability deductible per claim amount?",  # repeats the answer
    "What earthquake deductible formula and minimum apply?",  # repeats the answer
    "What windstorm deductible percentage applies under the property policy?",  # repeats the answer
    "What is the flood coverage annual aggregate limit?",
    "How soon must a loss be reported to the insurer?",
]
# Similarity to the closest answer line, measured with text-embedding-3-small on 9 Nov.
WALKTHROUGH_CLOSENESS = [0.59, 0.74, 0.75, 0.63, 0.36]


async def test_follow_ups_the_answer_already_covers_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import chat as chat_service

    extras = ["What is the coinsurance percentage?"]  # a document's own suggested question
    # Say the model flags 2 of the repeats; the third (0.75 to its line) is caught by the similarity.
    _checks(monkeypatch, answered=[1, 2], closeness=[*WALKTHROUGH_CLOSENESS, 0.41])
    prepared = _prepared()
    picked = await chat_service.pick_follow_ups(prepared, DEDUCTIBLES_ANSWER, WALKTHROUGH_SUGGESTIONS, extras)
    # Was: all 3 repeats shown. Now the 2 new model suggestions, topped up with the document's own.
    assert picked == [
        "What is the flood coverage annual aggregate limit?",
        "How soon must a loss be reported to the insurer?",
        "What is the coinsurance percentage?",
    ]
    assert sorted(u.operation for u in prepared.usages) == ["check_follow_ups", "embed_follow_ups"]


async def test_answer_lines_are_the_facts_of_the_answer() -> None:
    from app.services import chat as chat_service

    lines = chat_service.answer_lines(DEDUCTIBLES_ANSWER)
    # Each fact is its own line, scored on its own: against the whole answer the earthquake repeat
    # scored 0.58, against its line 0.74. Headings and citation marks are left out.
    assert not any(line.endswith(":") or "[" in line for line in lines)
    assert any(line.startswith('"Earthquake deductible') for line in lines) and len(lines) == 5


@pytest.mark.parametrize(
    ("answered", "closeness", "shown"),
    [
        (None, WALKTHROUGH_CLOSENESS, [0, 3, 4]),  # model check down: similarity alone drops 2 (0.59 gets through)
        ([1, 2, 3], None, [3, 4]),  # embeddings down: the model check alone
        (None, None, [0, 1, 2]),  # both down: the first 3, unchecked (and both failures recorded)
    ],
)
async def test_follow_ups_when_a_check_fails(
    monkeypatch: pytest.MonkeyPatch, answered: list[int] | None, closeness: list[float] | None, shown: list[int]
) -> None:
    from app.services import chat as chat_service

    _checks(monkeypatch, answered, closeness)
    prepared = _prepared()
    picked = await chat_service.pick_follow_ups(prepared, DEDUCTIBLES_ANSWER, WALKTHROUGH_SUGGESTIONS)
    assert picked == [WALKTHROUGH_SUGGESTIONS[i] for i in shown]
    failed = [u.operation for u in prepared.usages if not u.success]
    assert len(failed) == (answered is None) + (closeness is None)


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


# ------------------------------------------------------------------ found in the second UI pass (#56, #58)
async def test_comparison_labels_carry_the_version(client: httpx.AsyncClient) -> None:
    v1 = (await upload(client, "lease.txt", f"Lease {uuid.uuid4()}. Rent is $18,500.".encode())).json()["document"]
    v2 = (
        await upload(
            client, "lease.txt", f"Lease {uuid.uuid4()}. Rent is $19,750.".encode(), replaces_document_id=v1["id"]
        )
    ).json()["document"]
    created = (await client.post("/comparisons", json={"document_ids": [v2["id"], v1["id"]]})).json()
    labels = (await client.get(f"/comparisons/{created['id']}")).json()["result"]["labels"]
    # Both versions share the title "lease"; without the version the UI showed "A: lease" and "B: lease".
    assert labels["A"]["title"] == labels["B"]["title"] == "lease"
    assert (labels["A"]["version"], labels["B"]["version"]) == (2, 1)


def test_every_endpoint_has_a_written_summary() -> None:
    from app.main import app

    generated = []
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            # Without summary=..., /docs shows the function name in title case, e.g. "Get Document"
            # (operationId "get_document_api_v1_documents__document_id__get").
            fallback = operation["operationId"].split("_api_v1")[0].replace("_", " ").title()
            if operation["summary"] == fallback:
                generated.append(f"{method.upper()} {path}")
    assert len(app.openapi()["paths"]) > 20 and generated == []  # was: "Get Document", "List Sessions", ...


# ------------------------------------------------------------------ follow-ups and the conversation (#83)
async def test_follow_ups_the_conversation_already_covered_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import chat as chat_service

    seen: dict = {}
    earlier_answer = "The property premium is $48,750 [3]; the broker earns 10% [2]."
    prepared = _prepared()
    prepared.history = [
        {"role": "user", "content": "What is the annual premium on the property policy?"},
        {"role": "assistant", "content": earlier_answer},
    ]
    suggestions = [
        "What is the flood coverage annual aggregate limit?",
        "What is the annual premium for the Acme property policy?",  # re-asks the earlier question
        "How soon must a loss be reported to the insurer?",
    ]
    # Measured 9 Nov: a re-asked question scored 0.68-0.69 against the earlier one, others at most 0.58.
    _checks(monkeypatch, answered=[], closeness=[0.63, 0.30, 0.36], to_asked=[0.38, 0.69, 0.28], seen=seen)
    picked = await chat_service.pick_follow_ups(prepared, DEDUCTIBLES_ANSWER, suggestions)
    assert picked == [suggestions[0], suggestions[2]]  # was: the earlier question offered again
    # The model check also reads the chat's earlier answers, after the new one.
    assert "Earlier answers in this chat:\n" + earlier_answer in seen["prompt"]


async def test_suggestions_after_not_found_are_checked_against_the_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import chat as chat_service

    async def insight_questions(db, documents):  # type: ignore[no-untyped-def]
        return [
            "Who must be notified after a property loss, and in what order?",
            "How long must damaged stock be kept?",
        ]

    monkeypatch.setattr(chat_service, "_insight_questions", insight_questions)
    # The first was already answered earlier in the chat; the model check says so.
    _checks(monkeypatch, answered=[1], closeness=[0.2, 0.2], to_asked=[0.68, 0.35])
    prepared = _prepared()
    shown = await chat_service._finish_follow_ups(None, prepared, None, "I couldn't find this.", abstained=True)  # type: ignore[arg-type]
    assert shown == ["How long must damaged stock be kept?"]  # was: both, unchecked


async def test_a_cached_answer_does_not_suggest_what_was_just_asked(
    client: httpx.AsyncClient, ready_docs: dict[str, str]
) -> None:
    session_id = (await client.post("/chat/sessions", json={"document_ids": [ready_docs["policy"]]})).json()["id"]
    url = f"/chat/sessions/{session_id}/messages"
    first = (await client.post(url, json={"question": "What is the coinsurance?"})).json()["answer"]
    asked = first["follow_up_questions"][0]
    await client.post(url, json={"question": asked})  # the user clicks a suggestion
    again = (await client.post(url, json={"question": "What is the coinsurance?"})).json()["answer"]
    assert again["from_cache"] is True, (first["abstained"], first["content"][:80], again["content"][:80])
    assert asked not in again["follow_up_questions"]  # was: the cached answer offered it again

"""Deterministic, offline provider used by the test suite (LLM_PROVIDER=fake).

Embeddings use feature hashing of words, so texts sharing words are genuinely similar and
retrieval can be tested meaningfully without network calls or API cost.
"""

import hashlib
import math
import re
from collections import Counter
from collections.abc import AsyncIterator
from typing import TypeVar, cast

from pydantic import BaseModel

from app.core.config import Settings
from app.llm.base import Completion, Embeddings, Message, StreamEvent, StructuredCompletion, Usage
from app.llm.prompts.chat import ABSTAIN_PREFIX
from app.llm.schemas import (
    AnsweredSuggestions,
    ComparisonOutput,
    DocumentBrief,
    Entities,
    FollowUpQuestions,
    InsightsOutput,
    KeyFact,
    SectionNotes,
    Sentiment,
)

T = TypeVar("T", bound=BaseModel)

WORD_RE = re.compile(r"[a-z0-9$,.]+")
STOPWORDS = {"the", "a", "an", "of", "and", "or", "to", "in", "is", "for", "on", "with", "what", "are"}


def _words(text: str) -> list[str]:
    return [w.strip(".,") for w in WORD_RE.findall(text.lower()) if w.strip(".,") not in STOPWORDS]


class FakeProvider:
    name = "fake"
    chat_model = "fake-chat"
    fast_model = "fake-chat"
    embedding_model = "fake-embedding"

    def __init__(self, settings: Settings):
        self._dim = settings.embedding_dimensions

    # ------------------------------------------------------------------ embeddings
    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for word, count in Counter(_words(text)).items():
            idx = int(hashlib.md5(word.encode()).hexdigest(), 16) % self._dim
            vec[idx] += float(count)
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed(self, texts: list[str]) -> Embeddings:
        tokens = sum(len(t.split()) for t in texts)
        return Embeddings([self._vector(t) for t in texts], Usage(self.embedding_model, prompt_tokens=tokens))

    async def aembed(self, texts: list[str]) -> Embeddings:
        return self.embed(texts)

    # ------------------------------------------------------------------ completions
    def complete_structured(
        self,
        messages: list[Message],
        response_model: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2000,
    ) -> StructuredCompletion[T]:
        text = messages[-1]["content"]
        value: BaseModel
        if response_model is InsightsOutput:
            value = self._insights(text)
        elif response_model is SectionNotes:
            value = SectionNotes(notes=text.split("<section>", 1)[-1][:2000])
        elif response_model is FollowUpQuestions:
            value = FollowUpQuestions(questions=["What are the key dates?", "Are there any exclusions?"])
        elif response_model is AnsweredSuggestions:
            value = AnsweredSuggestions(answered=[])  # tests decide what counts as answered
        elif response_model is ComparisonOutput:
            value = ComparisonOutput(
                overview="The documents are compared below.",
                documents=[DocumentBrief(document_label="A", one_line_summary="Document A")],
                similarities=["Both are documents."],
                differences=[],
                gaps_or_risks=[],
            )
        else:  # pragma: no cover - only reached if a new schema is added without a fake
            raise NotImplementedError(response_model.__name__)
        usage = Usage(self.chat_model, prompt_tokens=len(text.split()), completion_tokens=50)
        return StructuredCompletion(cast(T, value), usage)

    async def acomplete_structured(
        self,
        messages: list[Message],
        response_model: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2000,
    ) -> StructuredCompletion[T]:
        return self.complete_structured(messages, response_model)

    async def acomplete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1200,
        reasoning_effort: str | None = None,
    ) -> Completion:
        last = messages[-1]["content"]
        if "Latest question:" in last:  # query rewriting
            text = last.split("Latest question:", 1)[1].strip()
        elif "Sources:" in last:  # grounded answer
            text = self._answer(last)
        else:  # summaries, section notes
            text = last.split("\n", 2)[-1][:400]
        return Completion(text, Usage(self.chat_model, prompt_tokens=len(last.split()), completion_tokens=40))

    async def astream(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1200,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        completion = await self.acomplete(messages)
        for word in completion.text.split(" "):
            yield StreamEvent(delta=word + " ")
        yield StreamEvent(usage=completion.usage)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _answer(prompt: str) -> str:
        if "(no relevant sources were found)" in prompt:
            return f"{ABSTAIN_PREFIX} The documents do not cover this topic."
        match = re.search(r'\[1\] \(.*?\)\n"""\n(.*?)\n"""', prompt, re.S)
        first = match.group(1).strip().split(". ")[0] if match else "the documents"
        return f"According to the documents, {first} [1]."

    @staticmethod
    def _insights(text: str) -> InsightsOutput:
        body = text.split("<document>", 1)[-1]
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\[Page \d+\]", "", body)) if s.strip()]
        page = None
        facts: list[KeyFact] = []
        for line in body.splitlines():
            if m := re.match(r"\[Page (\d+)\]", line):
                page = int(m.group(1))
            for amount in re.findall(r"\$[\d,]+", line):
                facts.append(KeyFact(label=line.strip()[:60], value=amount, page=page))
        common = [w for w, _ in Counter(w for w in _words(body) if len(w) > 4).most_common(5)]
        return InsightsOutput(
            summary=" ".join(sentences[:2])[:500] or "Empty document.",
            document_type="document",
            category="other",
            tags=common[:5],
            key_points=sentences[:3],
            key_facts=facts[:10],
            entities=Entities(
                organizations=[], people=[], locations=[], dates=[], monetary_amounts=[f.value for f in facts]
            ),
            sentiment=Sentiment(label="neutral", score=0.0, rationale="Formal document."),
            suggested_questions=["What is this document about?"],
            language="en",
        )

"""Suggest smart next questions that the selected documents can actually answer."""

from app.llm.base import Message

VERSION = "followups-v2"

SYSTEM = """Suggest up to 3 short follow-up questions the user is likely to ask next.
- Each must be answerable from the provided source excerpts (do not suggest unanswerable ones).
- Make them specific and useful (numbers, conditions, exceptions, comparisons, deadlines).
- Do not repeat the question that was just asked.
- Under 15 words each."""


def build_messages(question: str, answer: str, sources_text: str) -> list[Message]:
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": f"Question: {question}\n\nAnswer: {answer}\n\nSource excerpts:\n{sources_text[:6000]}",
        },
    ]

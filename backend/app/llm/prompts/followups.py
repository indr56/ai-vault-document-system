"""Suggest smart next questions that the selected documents can actually answer.

v3: generated from the question and the top sources only (not the answer), so the call runs in
parallel with answer generation instead of after it; and it reads at most the top 3 excerpts
(~3,000 characters) instead of ~6,000.
v4: asks for 5 candidates about different facts; once the answer is known, the 3 least similar to
it are shown (chat.pick_follow_ups), so suggestions don't restate what the answer just said.
"""

from app.llm.base import Message

VERSION = "followups-v4"
MAX_SOURCES = 3
MAX_SOURCE_CHARS = 3000
CANDIDATES = 5  # generated; the 3 that add the most beyond the answer are shown

SYSTEM = """Suggest 5 short follow-up questions the user is likely to ask next, each about a different fact.
- Each must be answerable from the provided source excerpts (do not suggest unanswerable ones).
- Make them specific and useful (numbers, conditions, exceptions, comparisons, deadlines).
- Do not repeat or rephrase the question that was just asked.
- Under 15 words each."""


def build_messages(question: str, sources_text: str) -> list[Message]:
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": f"Question just asked: {question}\n\nSource excerpts:\n{sources_text[:MAX_SOURCE_CHARS]}",
        },
    ]

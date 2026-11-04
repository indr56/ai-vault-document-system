"""Multi-turn support: turn a follow-up ("and for fire?") into a standalone search query."""

from app.llm.base import Message

VERSION = "rewrite-v2"

SYSTEM = """Rewrite the user's latest question into a single standalone question that can be \
understood without the conversation, for searching documents.
- Resolve pronouns and references ("it", "that policy", "the second one") using the conversation.
- Keep the user's intent and all specific terms, names, numbers and dates.
- If the question is already standalone, return it unchanged.
- Output only the rewritten question, nothing else."""


def build_messages(history: list[Message], question: str) -> list[Message]:
    transcript = "\n".join(f"{m['role'].upper()}: {m['content'][:800]}" for m in history)
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Conversation:\n{transcript}\n\nLatest question: {question}"},
    ]

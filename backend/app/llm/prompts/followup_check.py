"""Drop suggested follow-ups that the answer the user just read already covers.

v1: the follow-ups are written in parallel with the answer (from the sources, not the answer), so
some ask for facts the answer then states. Measured on real answers: comparing embeddings could not
separate those from genuine follow-ups (some repeats scored as low as 0.59 against the closest answer
line, genuine ones up to 0.63); this check with the fast model got 42 of 45 right (3 runs of 15), and
the one it missed ("Who is the Facilities Manager to notify within 24 hours?", 0.69) is caught by the
embedding score, so chat.pick_follow_ups uses both.
v2: also shows this chat's earlier answers (latest first, after the new answer), so a suggestion
the conversation already covered is caught too. The instructions are unchanged: giving the model
the whole conversation as a dialogue instead made it worse (49/63 on the 9 Nov walkthrough cases,
wrongly dropping genuine follow-ups); this form scored 61/63 and dropped no genuine one.
"""

from app.llm.base import Message

VERSION = "followup-check-v2"

SYSTEM = """You check suggested follow-up questions against an answer the user has just read.
Mark a suggestion ANSWERED if the answer already gives what it asks for, even partly or in other words, \
or if the suggestion mostly repeats the answer's own facts back as a question (the same role, amount, \
deadline or rule). Mark it NEW only if it asks about something the answer does not mention.
Judge only from the answer text. Return the numbers of the ANSWERED suggestions."""


MAX_EARLIER_ANSWERS = 3
MAX_EARLIER_CHARS = 1500  # per earlier answer


def build_messages(answer: str, suggestions: list[str], earlier_answers: list[str] | None = None) -> list[Message]:
    """`earlier_answers`: this chat's previous answers, most recent first."""
    text = answer
    earlier = [a[:MAX_EARLIER_CHARS] for a in (earlier_answers or [])[:MAX_EARLIER_ANSWERS]]
    if earlier:
        text += "\n\nEarlier answers in this chat:\n" + "\n".join(earlier)
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(suggestions, start=1))
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Answer:\n{text}\n\nSuggestions:\n{numbered}"},
    ]

"""Remembers answers the user gave to form questions, and reuses them for similar questions."""
import re
from difflib import SequenceMatcher

from . import db

_STOP = {"please", "your", "you", "the", "a", "an", "of", "do", "are", "is", "what", "enter", "select", "*"}


def normalize(question: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", question.lower())
    return " ".join(w for w in text.split() if w not in _STOP)


def lookup(question: str, threshold: float = 0.86) -> str | None:
    norm = normalize(question)
    if not norm:
        return None
    best, best_score = None, 0.0
    for row in db.q("SELECT id, question_norm, answer FROM qa_memory"):
        score = 1.0 if row["question_norm"] == norm else SequenceMatcher(None, norm, row["question_norm"]).ratio()
        if score > best_score:
            best, best_score = row, score
    if best and best_score >= threshold:
        db.run("UPDATE qa_memory SET times_used = times_used + 1 WHERE id=?", (best["id"],))
        return best["answer"]
    # ready answers from profile.yaml -> standard_answers
    from . import profile
    for q, a in (profile.load_profile().get("standard_answers") or {}).items():
        if a and SequenceMatcher(None, norm, normalize(q)).ratio() >= threshold:
            return str(a)
    return None


def save(question: str, answer: str) -> None:
    norm = normalize(question)
    if not norm or not answer.strip():
        return
    db.run("""INSERT INTO qa_memory(question, question_norm, answer, created_at) VALUES(?,?,?,?)
              ON CONFLICT(question_norm) DO UPDATE SET answer=excluded.answer, question=excluded.question""",
           (question.strip(), norm, answer.strip(), db.now()))


def as_text() -> str:
    rows = db.q("SELECT question, answer FROM qa_memory ORDER BY id")
    return "\n".join(f"Q: {r['question']}\nA: {r['answer']}" for r in rows)

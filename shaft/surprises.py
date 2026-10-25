"""Surprises: what I expected, what happened instead, and what I do about it (docs/plans/PLAN-surprise.md).

Three things live here. The rule that flags a tool result with a known failure shape, so a result my predictions
did not anticipate gets a note while I can still act on it. The fold that turns my episodes into my open surprises
(my curiosity queue): a surprise is opened by finish_cycle's `surprises`, answered by a later cycle's `resolves`,
closed in a sleep with close_surprise, or it expires. And the random draw of curiosity cycles.

Nothing is stored here: the queue is recomputed from my append-only episodes every time, so it cannot be edited
after the fact. The supervisor measures me with its own copy of the rule and the fold; shared test fixtures keep the
two in step. Nothing rewards being surprised, or not being surprised.
"""
from __future__ import annotations

import json
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

# -- the rule (the supervisor's copy: supervisor/surprise.py flag(); keep them in step) -------------------------
EXIT = re.compile(r"^exit (-?\d{1,9}) ·")   # bounded: a tool can print any number; int() caps at 4,300 digits
HTTP = re.compile(r"^HTTP (\d{3})\b")
ERROR_TYPE = re.compile(r"^ERROR: (?:[A-Za-z]+Error: )?([A-Z][A-Za-z]+(?:Error|Exception))\b|^ERROR: ([A-Za-z]+Error)")
# Most specific cause first: "zsh: can't create temp file …: operation not permitted" is the sandbox
TROUBLE = ("not permitted", "permission denied", "no such file", "command not found", "traceback", "can't", "cannot")
EMPTY = ("(empty)", "nothing relevant found", "(no results)", "no results")


def flag(result: str) -> tuple[str, str] | None:
    """(flag, detail) when a tool result has a known failure shape, else None: error, refused (the repeat guard),
    nonzero_exit, stderr_on_success (exit 0 while stderr shows a failure: a heredoc that never applied), http_error,
    empty, truncated."""
    text = (result or "").strip()
    if text.startswith("ERROR"):
        m = ERROR_TYPE.match(text)
        return "error", (m.group(1) or m.group(2)) if m else ""
    if text.startswith(("(repeat: not run)", "(not run again)", "(stopped)")):
        return "refused", ""
    if m := EXIT.match(text):
        if int(m.group(1)) != 0:
            return "nonzero_exit", _trouble(text)
        trouble = _trouble(text.split("--- stderr ---", 1)[1]) if "--- stderr ---" in text else ""
        if trouble:   # only stderr counts: stdout may be a file that says "Traceback"
            return "stderr_on_success", trouble
        return ("truncated", "") if "[... truncated" in text else None
    if (m := HTTP.match(text)) and int(m.group(1)) >= 400:
        return "http_error", m.group(1)
    if text.lower() in EMPTY:
        return "empty", ""
    if "[... truncated" in text:
        return "truncated", ""
    return None


def _trouble(text: str) -> str:
    low = text.lower()
    return next((t for t in TROUBLE if t in low), "")


# Phrases in a prediction that say this failure was coming. Saying a failure, not mentioning its word: "the tests pass
# without errors" or "returns exit 0" expect success, and a negated phrase ("will not fail", "no error") never counts.
# When in doubt the note is shown: a needless note costs one line, a missed surprise is what the note exists for.
FAILS = ("fail", "an error", "errors", "refus", "rejected", "denied", "permissionerror", "not allowed", "raises")
ANTICIPATES = {"error": FAILS,
               "nonzero_exit": FAILS + ("non-zero", "nonzero", "exit 1", "exit code 1"),
               "stderr_on_success": FAILS + ("stderr", "warning"),
               "http_error": FAILS + ("404", "403", "429", "500", "502", "503", "unavailable"),
               "empty": ("empty", "nothing", "no result", "no new", "not find", "not found", "no match",
                         "finds no", "returns no"),
               "truncated": ("truncat", "cut short", "too long")}
NEGATED = re.compile(r"\b(?:no|not|without|never|nor|n't|cannot)\s+(?:\w+\s+)?$")
NOTE_CHARS = 200


def _says(prediction: str, phrase: str) -> bool:
    low = prediction.lower()
    return any(not NEGATED.search(low[max(0, i - 24):i]) for i in _at_each(low, phrase))


def _at_each(text: str, phrase: str):
    i = text.find(phrase)
    while i >= 0:
        yield i
        i = text.find(phrase, i + 1)


def note(hit: tuple[str, str], predictions: list[str]) -> str:
    """The line added to a flagged result my predictions did not anticipate, else ''. Not for `refused`: the
    repeat guard's own text already says the call was not run."""
    f, detail = hit
    if f not in ANTICIPATES or any(_says(p, w) for p in predictions for w in ANTICIPATES[f]):
        return ""
    what = f + (f" ({detail})" if detail else "")
    said = "; ".join(f'"{p}"' for p in predictions)
    said = said if len(said) <= NOTE_CHARS else said[:NOTE_CHARS] + "…"
    return f"\n\n[unexpected: {what}; you expected: {said or 'nothing stated about this'}]"


# -- the fold ------------------------------------------------------------------------------------------------
MAX_OPEN = 8            # visible open surprises; a new one past this expires the oldest
EXPIRE_DAYS = 7
HIDDEN_DEPTH = 2        # a surprise opened by a follow-up of a surprise: kept, never shown (no rabbit holes)
FIELD_CHARS = 300
QUEUE_CHARS = 1200
USEFUL = ("yes", "partial")
REF = re.compile(r"^surprise:(\S+)$")
OUTCOMES = ("answered", "not_worth_pursuing", "duplicate", "unanswerable")


def clean(entries) -> list[dict]:
    """finish_cycle's `surprises`, kept as far as they make sense: a malformed one is dropped, not an error. Values
    become text (a model may answer 404 for an observed status), so what is stored always passes _entry."""
    out = []
    for e in entries if isinstance(entries, list) else [entries] if isinstance(entries, dict) else []:
        if isinstance(e, dict):
            item = _entry({k: " ".join(str(e.get(k) if e.get(k) is not None else "").split()) for k in FIELDS})
            if item:
                out.append(item)
    return out


FIELDS = ("expected", "observed", "so_what")


def _entry(e) -> dict | None:
    """A stored surprise as the fold reads it (the supervisor's copy applies the same rule): a dict with a non-empty
    expected or observed string. Anything else opens nothing and issues no id."""
    if not isinstance(e, dict) or not any(isinstance(e.get(k), str) and e[k].strip() for k in ("expected", "observed")):
        return None
    return {k: e[k][:FIELD_CHARS] if isinstance(e.get(k), str) else "" for k in FIELDS}


def episodes(root: Path) -> list[dict]:
    base = Path(root) / "memory" / "episodes"
    out = []
    for path in sorted(base.glob("*.jsonl")) if base.is_dir() else []:
        for line in path.read_text(errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _at(ts) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at


def fold(eps: list[dict], now: datetime) -> dict[str, dict]:
    """Every surprise I have reported, by id, with what became of it: open, resolved, closed or expired."""
    items: dict[str, dict] = {}
    issued: dict[str, int] = {}

    def age_out(at: datetime) -> None:
        for it in items.values():
            if it["status"] == "open" and at - it["opened_at"] > timedelta(days=EXPIRE_DAYS):
                it.update(status="expired", reason="age", ended=(it["opened_at"] + timedelta(days=EXPIRE_DAYS))
                          .isoformat(timespec="seconds"))

    for ep in eps:
        at = _at(ep.get("ts")) if isinstance(ep, dict) else None   # my own code may have written anything there
        if at is None:
            continue
        if ep.get("kind") == "sleep":
            for c in _list(ep.get("closed")):
                it = items.get(str(c.get("id"))) if isinstance(c, dict) else None
                if it and it["status"] == "open" and _text(c.get("note")):   # closed with a reason, or not at all
                    it.update(status="closed", outcome=c.get("outcome"), note=c.get("note"), ended=ep["ts"])
            continue
        if ep.get("kind") != "live":
            continue
        age_out(at)
        intent, fin = _dict(ep.get("intent")), _dict(ep.get("finish"))
        m = REF.match(str(intent.get("origin_ref") or "").strip())
        parent = items.get(m.group(1)) if m else None
        if parent:
            parent["followups"].append(ep["ts"])
        for r in _list(fin.get("resolves")):
            it = items.get(str(r.get("id"))) if isinstance(r, dict) else None
            if it and it["status"] == "open" and _text(r.get("how")):   # answered, and how
                it.update(status="resolved", how=" ".join(str(r.get("how") or "").split())[:FIELD_CHARS], ended=ep["ts"])
        prefix = f"s{at:%m%d-%H%M}"
        for s in filter(None, map(_entry, _list(fin.get("surprises")))):
            issued[prefix] = issued.get(prefix, 0) + 1
            sid = f"{prefix}-{issued[prefix]}"
            items[sid] = {"id": sid, "opened": ep["ts"], "opened_at": at, **s,
                          "depth": parent["depth"] + 1 if parent else 1, "status": "open", "reason": None,
                          "how": None, "outcome": None, "note": None, "ended": None, "followups": [],
                          "goal": str(intent.get("goal") or "")[:FIELD_CHARS]}
        visible = [it for it in items.values() if it["status"] == "open" and it["depth"] < HIDDEN_DEPTH]
        for it in visible[:max(0, len(visible) - MAX_OPEN)]:   # oldest first: dict order is opening order
            it.update(status="expired", reason="cap", ended=ep["ts"])
    age_out(now)
    return items


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _verdicts(root: Path) -> dict[str, dict]:
    """The judge's verdicts my sleeps received (memory/verdicts.jsonl), by cycle."""
    from . import memory   # memory indexes my surprises: imported here, not at the top
    return {row["episode_ts"]: row for row in memory.verdict_rows(root)}


def all_items(root: Path, now: datetime | None = None) -> dict[str, dict]:
    from . import clock
    return fold(episodes(root), now or clock.now())


def queue(root: Path, now: datetime | None = None, items: dict | None = None) -> list[dict]:
    """My open surprises worth a cycle, oldest first. Hidden: those opened by a follow-up of a surprise, and those
    already followed up once by a cycle the judge did not find useful (each surprise gets one follow-up, more only
    while they help)."""
    items = all_items(root, now) if items is None else items
    verdicts = _verdicts(root)
    out = []
    for it in items.values():
        if it["status"] != "open" or it["depth"] >= HIDDEN_DEPTH:
            continue
        if it["followups"] and (verdicts.get(it["followups"][-1]) or {}).get("followup_useful") not in USEFUL:
            continue
        out.append(it)
    return out


def ineligible(root: Path, sid: str) -> str:
    """Why a goal may not come from surprise `sid`, or '' when it may: the guards the queue applies by hiding (depth,
    one follow-up unless it was judged useful) hold for a goal that names the id from memory too."""
    items = all_items(root)
    it = items.get(sid)
    if it is None:
        return "no surprise of mine has that id"
    if it["status"] != "open":
        return f"that surprise is {it['status']}"
    if it["depth"] >= HIDDEN_DEPTH:
        return "it was opened by a follow-up of a surprise, and is not pursued further"
    if it not in queue(root, items=items):
        return "it already had its follow-up cycle, and the judge did not find that one useful (yet)"
    return ""


def _age(it: dict, now: datetime) -> str:
    days = (now - it["opened_at"]).total_seconds() / 86400
    return f"{days:.0f} d" if days >= 1 else f"{days * 24:.0f} h"


def block(root: Path, now: datetime | None = None) -> str:
    """The queue as my context shows it: at most MAX_OPEN items in QUEUE_CHARS, oldest first, each with its id and age."""
    from . import clock
    now = now or clock.now()
    try:
        shown = queue(root, now)[:MAX_OPEN]
    except Exception as e:   # every prompt shows this block: a record it cannot read must not stop the run
        return f"(my open surprises could not be read: {type(e).__name__}: {e})"
    rows, used = [], 0
    for i, it in enumerate(shown):
        room = (QUEUE_CHARS - used) // (len(shown) - i) - 1   # an even share of what is left, newline included
        rows.append(_row(it, now, room))
        used += len(rows[-1]) + 1
    return "\n".join(rows) or "(none open)"


def _row(it: dict, now: datetime, room: int) -> str:
    """One queue line in `room` characters. Each part gets its share, so a long expectation never pushes out what
    was observed — the part a curiosity cycle chooses by."""
    head = f"- {it['id']} ({_age(it, now)}): "
    parts = [("expected ", it["expected"] or "?", 2), (" — observed ", it["observed"] or "?", 2),
             (" — so what: ", it["so_what"], 1)]
    parts = [p for p in parts if p[1]]
    free = room - len(head) - sum(len(label) for label, _, _ in parts)
    weight = sum(w for _, _, w in parts)
    line = head
    for label, text, w in parts:   # what a short part leaves over goes to the parts after it
        cap = max(8, free * w // weight)
        cut = text if len(text) <= cap else text[:cap - 1] + "…"
        line += label + cut
        free, weight = free - len(cut), weight - w
    return line if len(line) <= room else line[:max(0, room - 1)] + "…"


def since(root: Path, after: str, now: datetime | None = None) -> list[dict]:
    """The surprises I reported after `after` (an episode ts), whatever became of them: what my sleep goes through."""
    return [it for it in all_items(root, now).values() if it["opened"] > after]


def text(it: dict) -> str:
    status = it["status"] + {"resolved": f": {it['how']}", "closed": f" ({it['outcome']}): {it['note']}",
                             "expired": f" ({it['reason']})"}.get(it["status"], "")
    return (f"Expected: {it['expected']}\nObserved: {it['observed']}\nSo what: {it['so_what']}\n"
            f"While: {it['goal']}\nStatus: {status}")


def docs(root: Path) -> list[tuple[str, str]]:
    """My surprises for search_memory and the recall before a cycle: a past surprise on the same topic is found
    before a similar call."""
    return [(f"surprise {it['id']} ({it['opened'][:16]})", text(it)) for it in all_items(root).values()]


# -- curiosity cycles ------------------------------------------------------------------------------------------
OPERATOR_MESSAGE = "-from-operator.md"


def draw(every: int, has_queue: bool, inbox: list[str], seed: str) -> dict:
    """Whether this cycle is a curiosity cycle: with probability 1/every while the queue is not empty and no message
    from the operator waits (his come first); 0 = never. The coin is seeded by the cycle's start, so a replay of this
    cycle draws what the cycle drew."""
    if every <= 0:
        return {"drawn": False, "skipped_for": "disabled"}
    if not has_queue:
        return {"drawn": False, "skipped_for": "empty_queue"}
    if any(name.endswith(OPERATOR_MESSAGE) for name in inbox):
        return {"drawn": False, "skipped_for": "operator_message"}
    return {"drawn": random.Random(seed).random() < 1 / every, "skipped_for": None}

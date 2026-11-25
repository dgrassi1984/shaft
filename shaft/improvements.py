"""My improvement list: concrete self-improvement work, recorded and closed as append-only events (#13, #15).

A sleep's "How I will improve" was text only: pasted into the next prompt, never turned into work. Each sleep now turns
it into at most PER_RUN items here, each a concrete action (a test, a plugin, a job, a rule, a fix or a tool) with the
evidence behind it and an id; a live cycle may add as many and closes them. When nothing for the operator is due and my
inbox is empty, my goal slate offers the open items first (slate.py). The list is mine: no supervisor tool is needed to
keep it, and my own plugins may keep other queues beside it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .tools import _spec

FILE = "memory/improvements.jsonl"
ID = re.compile(r"\bi\d{4}-\d{4}-[1-9]\d*\b")
KINDS = ("test", "plugin", "job", "rule", "fix", "tool")
OUTCOMES = ("done", "dropped")
PER_RUN = 2       # the operator's limit for a sleep (2026-11-20); a cycle gets the same, so no run floods the list
TEXT_CHARS = 400
S = {"type": "string"}
PLAN = _spec(
    "plan_improvement", "Add one concrete self-improvement item to my improvement list: an action a later cycle can do "
    f"and check (at most {PER_RUN} per run). Returns its id.", {
        "action": {"type": "string", "description": "what to build or change, concretely enough that a cycle can do "
                   "it and someone can check it was done"},
        "kind": {"type": "string", "enum": list(KINDS)},
        "evidence": {"type": "string", "description": "why: the episode, verdict, correction or journal entry that "
                     "shows the need"}})
CLOSE = _spec(
    "close_improvement", "Close one item of my improvement list: done (name the commit, episode or test that did it) "
    "or dropped (say why).", {
        "id": {"type": "string", "description": "the item's id, e.g. i1125-0300-1"},
        "outcome": {"type": "string", "enum": list(OUTCOMES)},
        "evidence": S})


def _flat(text, n: int = TEXT_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def fold(root: Path) -> dict[str, dict]:
    """Every item by id, oldest first: {id, action, kind, evidence, added, by, status, outcome, closed_evidence}. A
    line that is not a well-formed event is skipped, never raised: the list must not stop a cycle."""
    path = Path(root) / FILE
    items: dict[str, dict] = {}
    for line in path.read_text(errors="replace").splitlines() if path.is_file() else []:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("id"), str) or not ID.fullmatch(rec["id"]):
            continue
        iid = rec["id"]
        if rec.get("event") == "added":
            if iid in items or rec.get("kind") not in KINDS or not all(
                    isinstance(rec.get(k), str) and rec[k].strip() for k in ("action", "evidence", "at")):
                continue
            items[iid] = {"id": iid, "action": rec["action"], "kind": rec["kind"], "evidence": rec["evidence"],
                          "added": rec["at"], "by": rec.get("by") or "?", "status": "open", "outcome": None,
                          "closed_evidence": None}
        elif rec.get("event") == "closed" and iid in items and items[iid]["status"] == "open":
            if rec.get("outcome") in OUTCOMES and isinstance(rec.get("evidence"), str) and rec["evidence"].strip():
                items[iid].update(status="closed", outcome=rec["outcome"], closed_evidence=rec["evidence"])
    return items


def _append(root: Path, event: dict) -> None:
    path = Path(root) / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(json.dumps(event, ensure_ascii=False) + "\n")


def add(root: Path, args: dict, now: datetime, by: str) -> str:
    """`by`: 'sleep' or 'cycle'. Each refusal names what to send instead."""
    action, kind, evidence = (_flat(args.get(k)) for k in ("action", "kind", "evidence"))
    if kind not in KINDS:
        return f"ERROR: not added: kind must be one of {', '.join(KINDS)}"
    if len(action.split()) < 4:
        return "ERROR: not added: action must say concretely what to build or change (a few words at least)"
    if not evidence:
        return "ERROR: not added: evidence must name the episode, verdict, correction or entry that shows the need"
    items = fold(root)
    same = next((it for it in items.values() if it["status"] == "open" and it["action"] == action), None)
    if same:
        return f"already on my list as {same['id']} (open)"
    prefix = f"i{now:%m%d-%H%M}-"
    n = 1
    while prefix + str(n) in items:
        n += 1
    iid = prefix + str(n)
    _append(root, {"event": "added", "id": iid, "action": action, "kind": kind, "evidence": evidence,
                   "by": by, "at": now.isoformat(timespec="seconds")})
    return f"improvement {iid} added"


def close(root: Path, args: dict, now: datetime) -> str:
    iid, outcome, evidence = str(args.get("id") or "").strip(), args.get("outcome"), _flat(args.get("evidence"))
    items = fold(root)
    if iid not in items or items[iid]["status"] != "open":
        listed = ", ".join(i for i, it in items.items() if it["status"] == "open") or "none open"
        return f"ERROR: {iid!r} is not an open item of my improvement list (open: {listed})"
    if outcome not in OUTCOMES:
        return f"ERROR: outcome must be one of {', '.join(OUTCOMES)}"
    if not evidence:
        return "ERROR: evidence must name the commit, episode or test (done) or say why (dropped)"
    _append(root, {"event": "closed", "id": iid, "outcome": outcome, "evidence": evidence,
                   "at": now.isoformat(timespec="seconds")})
    return f"closed improvement {iid} ({outcome})"


class Limited:
    """add and close for one run (a sleep or a live cycle), with at most PER_RUN items added."""

    def __init__(self, root: Path, now, by: str):
        self.root, self.now, self.by, self.added = Path(root), now, by, []

    def plan(self, args: dict) -> str:
        if len(self.added) >= PER_RUN:
            return (f"ERROR: not added: at most {PER_RUN} items per {self.by} ({', '.join(self.added)} already); "
                    "keep the ones that matter most, or close one you no longer need")
        try:
            result = add(self.root, args, self.now(), self.by)
        except OSError as e:   # a list I cannot write must not stop my run
            return f"ERROR: not added: {type(e).__name__}: {e}"
        if (m := re.match(r"improvement (\S+) added", result)):
            self.added.append(m.group(1))
        return result

    def close(self, args: dict) -> str:
        try:
            return close(self.root, args, self.now())
        except OSError as e:
            return f"ERROR: not closed: {type(e).__name__}: {e}"


def controls(root: Path, now, by: str = "cycle"):
    from .agent import Control
    limited = Limited(root, now, by)
    return {"plan_improvement": Control(PLAN, limited.plan), "close_improvement": Control(CLOSE, limited.close)}


def open_items(root: Path) -> list[dict]:
    """The open items, newest first: the latest sleep's plan leads, the older backlog follows."""
    return sorted((it for it in fold(root).values() if it["status"] == "open"),
                  key=lambda it: (it["added"], it["id"]), reverse=True)


def block(root: Path) -> str:
    """The open items as my sleep reads them (to name or close), or why they could not be read."""
    try:
        rows = [f"- {it['id']} [{it['kind']}, added {it['added'][:16]} by {it['by']}]: {_flat(it['action'], 200)}"
                for it in open_items(root)]
    except Exception as e:   # a broken list must not stop the sleep
        return f"(my improvement list could not be read: {type(e).__name__}: {e})"
    return "\n".join(rows) or "(none open)"

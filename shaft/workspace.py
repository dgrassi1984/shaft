"""My workspace (PLAN-consciousness M4): a few items from my sources compete for a small space at the front of my
live cycle, and the winners are broadcast to every step.

My context pastes fixed slices of my files; the one step that selected anything was automatic recall, which ranked
memory hits against my goals under 1,800 characters. Here several deterministic codelets — no model call — each
nominate items from one source (memory, open surprises, inbox, improvement plan, actions that keep failing, goals,
commitments), every item carrying features read from my records only. They compete for WORKSPACE_K places in that same
1,800-character budget; the winners replace the recall block and come back in short form after each step, so they stay
in front of me for the whole cycle. The losers stay where they are on disk.

WORKSPACE=relevance ranks the pool by relevance to the cycle. WORKSPACE=off uses automatic recall.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import memory, surprises

MODES = ("relevance", "off")
BUDGET = 1800            # the automatic recall budget
ITEM_CHARS = 600
BROADCAST_CHARS = 120    # one winner's short form in each step's runtime line
REF_CHARS = 60           # a reference as shown: a long journal heading must not eat the budget
MEMORY_HITS = 8


@dataclass
class Item:
    source: str            # which codelet nominated it
    ref: str               # what it is in that source: a file, an id, a heading
    text: str
    own: float = 0.0       # the codelet's own relevance score, if it has one (memory: its search rank)
    relevance: float = 0.0

    def key(self) -> str:
        return f"{self.source}:{self.ref}"


def _flat(text: str, n: int = ITEM_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


# -- codelets: each nominates items from one source; none calls a model ----------------------------------------------
def _memory(root: Path, about: str, shown: str) -> list[Item]:
    out = []
    hits = memory.search(root, about, k=MEMORY_HITS + 4) if about.strip() else []
    for rank, h in enumerate(hits):
        probe = h.text.strip()[:120]
        if not probe or probe in shown:   # already in my context: not news to put in front of me
            continue
        out.append(Item("memory", h.source, _flat(h.text),
                        own=1 - rank / max(1, len(hits))))
        if len(out) >= MEMORY_HITS:
            break
    return out


def _surprises(root: Path, now: datetime) -> list[Item]:
    out = []
    for it in surprises.queue(root, now):
        text = f"expected {it.get('expected') or '?'} — observed {it.get('observed') or '?'}"
        if it.get("so_what"):
            text += f" — so what: {it['so_what']}"
        out.append(Item("surprise", it["id"], _flat(text)))
    return out


def _inbox(messages: list[tuple[str, str]]) -> list[Item]:
    out = []
    for name, text in messages:
        body = text.split("\n\n", 1)[1] if "\n\n" in text else text   # the From/To/Date header carries no content
        out.append(Item("inbox", name, _flat(body)))
    return out


def _plan(plan: str) -> list[Item]:
    out = []
    for line in plan.splitlines()[1:]:   # the first line says which sleep it came from
        line = line.strip(" -*#\t")
        if len(line) >= 20 and not line.lower().startswith("how i will improve"):
            out.append(Item("plan", f"line {len(out) + 1}", _flat(line)))
    return out


def _failures(root: Path, now: datetime) -> list[Item]:
    """Kinds of action that have failed since they last succeeded (the outcome cards' record): unresolved, so worth
    my attention before I try one again."""
    from . import cards
    latest: dict[str, dict] = {}
    failed: dict[str, int] = {}
    for c in cards.load(root, now):   # oldest first
        if c["ok"]:
            failed[c["family"]] = 0
        else:
            failed[c["family"]] = failed.get(c["family"], 0) + 1
        latest[c["family"]] = c
    out = []
    for fam, n in failed.items():
        if n:
            last = latest[fam]
            out.append(Item("failure", fam, _flat(f"{fam} has failed {n} time(s) since it last worked; last "
                                                  f"{last['ts'][:16]}: {last.get('error') or 'failed'}")))
    return out


def _goals(goals: str) -> list[Item]:
    out = []
    for line in goals.splitlines():
        line = line.strip()
        if not line.startswith(("-", "*")) and not re.match(r"\d+[.)]", line):
            continue
        text = line.lstrip("-*0123456789.) \t")
        if len(text) < 10:
            continue
        out.append(Item("goal", f"goals.md {len(out) + 1}", _flat(text)))
    return out


def _commitments(root: Path, now: datetime) -> list[Item]:
    from . import commitments
    out = []
    for it in commitments.fold(root, now).values():
        if commitments._to_self(root, it) or it.get("status") not in ("open", "overdue"):
            continue
        text = f"promised to {it.get('made_to')}, due {str(it.get('due') or '?')[:16]}: {it.get('promise')}"
        out.append(Item("commitment", it["id"], _flat(text)))
    return out


def _label(it: Item) -> str:
    return f"- [{it.source} {_flat(it.ref, REF_CHARS)}]"


# -- features ----------------------------------------------------------------------------------------------------
def _relevance(pool: list[Item], about: str) -> None:
    """Relevance to this cycle, the same scorer for every source: the pool ranked by BM25 against what the cycle is
    about, as a 0-1 rank score; memory hits keep their own search rank when it is higher (the search found them by
    meaning too, which words alone miss)."""
    for it in pool:
        it.relevance = 0.0
    ranked = memory._bm25([(it.source, it.text) for it in pool], about)
    for rank, i in enumerate(ranked):
        pool[i].relevance = 1 - rank / len(ranked)
    for it in pool:
        it.relevance = round(max(it.relevance, it.own), 3)


@dataclass
class Workspace:
    mode: str
    pool: list[Item] = field(default_factory=list)
    winners: list[Item] = field(default_factory=list)
    about: str = ""
    refocused: list[str] = field(default_factory=list)   # the winners after re-ranking by the declared goal
    k: int = 3
    errors: list[str] = field(default_factory=list)      # codelets that could not be read, said in the record

    def select(self) -> list[Item]:
        room = BUDGET // max(1, self.k)   # per rendered row, its label included: the block fits recall's room
        picked = sorted(self.pool, key=lambda it: -it.relevance)[:max(0, self.k)]
        for it in picked:
            it.text = _flat(it.text, max(40, room - len(_label(it)) - 1))
        return picked

    def refocus(self, goal: str) -> None:
        """After declare_intent: re-rank the pool for the declared goal; the broadcast follows the new winners."""
        if self.mode == "off" or not self.pool:
            return
        _relevance(self.pool, f"{self.about}\n{goal}")
        self.winners = self.select()
        self.refocused = [it.key() for it in self.winners]

    def block(self) -> str:
        if not self.winners:
            return ""
        rows = "\n".join(f"{_label(it)} {it.text}" for it in self.winners)
        return ("\n\n---\n## In focus now (my workspace: what won my attention for this cycle — the rest of my context "
                "and my memory are still there; search_memory finds more)\n" + rows)

    def broadcast(self) -> str:
        """The winners in short form, for the runtime line after each step."""
        if not self.winners:
            return ""
        return "[runtime] In focus: " + "; ".join(f"{it.source} {_flat(it.ref, 40)}: {_flat(it.text, BROADCAST_CHARS)}"
                                                  for it in self.winners)

    def record(self) -> dict:
        """Record the candidate scores and winner keys in the episode."""
        out = {"mode": self.mode, "k": self.k,
               "candidates": [{"source": it.source, "ref": it.ref, "relevance": it.relevance, "chars": len(it.text)}
                              for it in self.pool],
               "winners": [it.key() for it in self.winners]}
        if self.refocused:
            out["refocused"] = self.refocused
        if self.errors:
            out["errors"] = self.errors
        return out


def build(root: Path, mode: str, *, about: str, shown: str, now: datetime, inbox: list[tuple[str, str]], plan: str,
          goals: str, k: int = 3) -> Workspace:
    """Gather every codelet's items, score them and pick the winners. A codelet that fails is left out and said in
    the record: the workspace must never stop a cycle."""
    mode = mode if mode in MODES else "relevance"
    ws = Workspace(mode, k=max(1, k), about=about)
    if mode == "off":
        return ws
    for name, codelet in (("memory", lambda: _memory(root, about, shown)),
                          ("surprise", lambda: _surprises(root, now)), ("inbox", lambda: _inbox(inbox)),
                          ("plan", lambda: _plan(plan)), ("failure", lambda: _failures(root, now)),
                          ("goal", lambda: _goals(goals)), ("commitment", lambda: _commitments(root, now))):
        try:
            ws.pool += codelet()
        except Exception as e:   # one source unreadable: the others still compete
            ws.errors.append(f"{name}: {type(e).__name__}: {e}"[:200])
    _relevance(ws.pool, about)
    ws.winners = ws.select()
    return ws


def attention(value) -> list[dict]:
    """declare_intent's attention (B3): up to four {source, why}, kept as far as they make sense — a malformed value is
    trimmed, never an error (a cycle cannot fail for how it says where it will look). A list sent as a JSON string is
    read as that list, not kept as one source."""
    if isinstance(value, str) and value.strip()[:1] in ("[", "{"):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    items = value if isinstance(value, list) else [value] if isinstance(value, (dict, str)) else []
    out = []
    for a in items:
        if isinstance(a, dict) and str(a.get("source") or "").strip():
            out.append({"source": _flat(a["source"], 80), "why": _flat(a.get("why") or "", 200)})
        elif isinstance(a, str) and a.strip():
            out.append({"source": _flat(a, 80), "why": ""})
    return out[:4]

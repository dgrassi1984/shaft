"""My workspace (PLAN-consciousness M4): a few items from my sources compete for a small space at the front of my
live cycle, and the winners are broadcast to every step.

My context pastes fixed slices of my files; the one step that selected anything was automatic recall, which ranked
memory hits against my goals under 1,800 characters. Here several deterministic codelets — no model call — each
nominate items from one source (memory, open surprises, inbox, improvement plan, actions that keep failing, goals,
commitments), every item carrying features read from my records only. They compete for WORKSPACE_K places in that same
1,800-character budget; the winners replace the recall block and come back in short form after each step, so they stay
in front of me for the whole cycle. The losers stay where they are on disk.

Modes (config WORKSPACE): competitive — salience from relevance, a top-down prior (my previous goal, my improvement
plan, and once I declare it, where I said I would look) and urgency, at most PER_SOURCE winners per source, with
habituation so one item cannot win forever; relevance — the same pool ranked by relevance to the cycle alone (the
kill test's control); off — automatic recall as before. Whether competition beats plain relevance is an experiment
(`shaft experiment workspace`): if it does not, this goes.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import memory, surprises

MODES = ("competitive", "relevance", "off")
BUDGET = 1800            # what automatic recall had: the arms compete for the same room
ITEM_CHARS = 600
PER_SOURCE = 2           # competitive: no single source takes every place
BROADCAST_CHARS = 120    # one winner's short form in each step's runtime line
REF_CHARS = 60           # a reference as shown: a long journal heading must not eat the budget
WEIGHTS = {"relevance": 0.4, "topdown": 0.3, "urgency": 0.3}   # round on purpose: never tuned on the data they are tested on
RECENT = 0.2             # new since my last cycle
HABITUATION = 0.3        # per win in my last HABIT_CYCLES live cycles
HABIT_CYCLES = 5
MEMORY_HITS = 8
DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}))?")


@dataclass
class Item:
    source: str            # which codelet nominated it
    ref: str               # what it is in that source: a file, an id, a heading
    text: str
    urgency: float = 0.0
    at: str = ""           # when it was written, if known (ISO)
    own: float = 0.0       # the codelet's own relevance score, if it has one (memory: its search rank)
    relevance: float = 0.0
    topdown: float = 0.0
    recent: float = 0.0
    habituation: float = 0.0
    salience: float = 0.0

    def key(self) -> str:
        return f"{self.source}:{self.ref}"


def _when(text: str) -> datetime | None:
    m = DATE.search(text or "")
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2) or '00:00'}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _flat(text: str, n: int = ITEM_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


# -- codelets: each nominates items from one source; none calls a model ----------------------------------------------
def _memory(root: Path, about: str, shown: str, now: datetime) -> list[Item]:
    out = []
    hits = memory.search(root, about, k=MEMORY_HITS + 4) if about.strip() else []
    for rank, h in enumerate(hits):
        probe = h.text.strip()[:120]
        if not probe or probe in shown:   # already in my context: not news to put in front of me
            continue
        when = _when(h.source)
        out.append(Item("memory", h.source, _flat(h.text), at=when.isoformat() if when else "",
                        own=1 - rank / max(1, len(hits))))
        if len(out) >= MEMORY_HITS:
            break
    return out


def _surprises(root: Path, now: datetime) -> list[Item]:
    out = []
    for it in surprises.queue(root, now):
        opened = it.get("opened") or ""
        try:
            age = now - datetime.fromisoformat(opened)
        except ValueError:
            age = timedelta(days=99)
        text = f"expected {it.get('expected') or '?'} — observed {it.get('observed') or '?'}"
        if it.get("so_what"):
            text += f" — so what: {it['so_what']}"
        out.append(Item("surprise", it["id"], _flat(text), urgency=0.7 if age < timedelta(hours=24) else 0.5,
                        at=opened))
    return out


def _inbox(messages: list[tuple[str, str]]) -> list[Item]:
    out = []
    for name, text in messages:
        operator = name.endswith("-from-operator.md")
        stamp = re.match(r"(\d{8}-\d{6})", name)
        at = datetime.strptime(stamp.group(1), "%Y%m%d-%H%M%S").isoformat() if stamp else ""
        body = text.split("\n\n", 1)[1] if "\n\n" in text else text   # the From/To/Date header carries no content
        out.append(Item("inbox", name, _flat(body), urgency=1.0 if operator else 0.6, at=at))
    return out


def _plan(plan: str) -> list[Item]:
    out = []
    for line in plan.splitlines()[1:]:   # the first line says which sleep it came from
        line = line.strip(" -*#\t")
        if len(line) >= 20 and not line.lower().startswith("how i will improve"):
            out.append(Item("plan", f"line {len(out) + 1}", _flat(line), urgency=0.4))
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
                                                  f"{last['ts'][:16]}: {last.get('error') or 'failed'}"),
                            urgency=0.8, at=last["ts"]))
    return out


def _goals(goals: str, now: datetime) -> list[Item]:
    out = []
    for line in goals.splitlines():
        line = line.strip()
        if not line.startswith(("-", "*")) and not re.match(r"\d+[.)]", line):
            continue
        text = line.lstrip("-*0123456789.) \t")
        if len(text) < 10:
            continue
        due = _when(text)
        urgency = 0.2
        if due and due.date() <= now.date():
            urgency = 1.0
        elif due and due - now <= timedelta(days=2):
            urgency = 0.6
        out.append(Item("goal", f"goals.md {len(out) + 1}", _flat(text), urgency=urgency))
    return out


def _commitments(root: Path, now: datetime) -> list[Item]:
    from . import commitments
    out = []
    for it in commitments.fold(root, now).values():
        if it.get("status") not in ("open", "overdue"):
            continue
        due = _when(str(it.get("due") or ""))
        urgency = 1.0 if it["status"] == "overdue" else 0.7 if due and due - now <= timedelta(hours=24) else 0.3
        text = f"promised to {it.get('made_to')}, due {str(it.get('due') or '?')[:16]}: {it.get('promise')}"
        out.append(Item("commitment", it["id"], _flat(text), urgency=urgency, at=str(it.get("made_at") or "")))
    return out


def _label(it: Item) -> str:
    return f"- [{it.source} {_flat(it.ref, REF_CHARS)}]"


# -- features ----------------------------------------------------------------------------------------------------
def _overlap(a: set[str], b: set[str]) -> float:
    """Cosine of two word sets: how much of each the other covers, 0-1."""
    return len(a & b) / math.sqrt(len(a) * len(b)) if a and b else 0.0


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


def _past(root: Path, now: datetime) -> tuple[str, list[str]]:
    """My last live cycle's end, and the winners of my last HABIT_CYCLES live cycles' workspaces (keys)."""
    eps = [ep for ep in surprises.episodes(root) if ep.get("kind") == "live" and (ep.get("ts") or "") < now.isoformat()]
    last = eps[-1]["ts"] if eps else ""
    wins = []
    for ep in eps[-HABIT_CYCLES:]:
        ws = ep.get("workspace") or {}
        wins += [w for w in ws.get("winners") or [] if isinstance(w, str)]
    return last, wins


@dataclass
class Workspace:
    mode: str
    pool: list[Item] = field(default_factory=list)
    winners: list[Item] = field(default_factory=list)
    prior: str = ""
    about: str = ""
    attention: list[dict] = field(default_factory=list)
    refocused: list[str] = field(default_factory=list)   # the winners after my declared attention re-ranked them
    k: int = 3
    errors: list[str] = field(default_factory=list)      # codelets that could not be read, said in the record

    def score(self) -> None:
        prior = set(memory._tokens(self.prior + " " + " ".join(f"{a.get('source', '')} {a.get('why', '')}"
                                                              for a in self.attention)))
        for it in self.pool:
            it.topdown = round(_overlap(set(memory._tokens(f"{it.source} {it.ref} {it.text}")), prior), 3)
            it.salience = round(WEIGHTS["relevance"] * it.relevance + WEIGHTS["topdown"] * it.topdown
                                + WEIGHTS["urgency"] * it.urgency + it.recent - it.habituation, 3)

    def select(self) -> list[Item]:
        if self.mode == "relevance":
            order = sorted(self.pool, key=lambda it: -it.relevance)
            cap = self.k
        else:
            order = sorted(self.pool, key=lambda it: -it.salience)
            cap = PER_SOURCE
        room = BUDGET // max(1, self.k)   # per rendered row, its label included: the block fits recall's room
        picked, per = [], {}
        for it in order:
            if len(picked) >= self.k:
                break
            if per.get(it.source, 0) >= cap:
                continue
            per[it.source] = per.get(it.source, 0) + 1
            it.text = _flat(it.text, max(40, room - len(_label(it)) - 1))
            picked.append(it)
        return picked

    def refocus(self, goal: str, attention: list[dict]) -> None:
        """After declare_intent: the pool competes again for the goal I declared — relevance to it, in both modes, so
        both know the same — and where I said I would look joins the top-down prior (B3), in competition only: plain
        relevance has no prior, as its control must. The broadcast follows the new winners."""
        if self.mode == "off" or not self.pool:
            return
        _relevance(self.pool, f"{self.about}\n{goal}")
        if self.mode == "competitive":
            self.attention = attention
        self.score()
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
        """What the episode keeps: every candidate with its features, and the winners (keys), so the selection can be
        checked and habituation read back."""
        keep = ("source", "ref", "urgency", "relevance", "topdown", "recent", "habituation", "salience")
        out = {"mode": self.mode, "k": self.k,
               "candidates": [{k: v for k, v in asdict(it).items() if k in keep} | {"chars": len(it.text)}
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
    mode = mode if mode in MODES else "competitive"
    ws = Workspace(mode, k=max(1, k), about=about)
    if mode == "off":
        return ws
    for name, codelet in (("memory", lambda: _memory(root, about, shown, now)),
                          ("surprise", lambda: _surprises(root, now)), ("inbox", lambda: _inbox(inbox)),
                          ("plan", lambda: _plan(plan)), ("failure", lambda: _failures(root, now)),
                          ("goal", lambda: _goals(goals, now)), ("commitment", lambda: _commitments(root, now))):
        try:
            ws.pool += codelet()
        except Exception as e:   # one source unreadable: the others still compete
            ws.errors.append(f"{name}: {type(e).__name__}: {e}"[:200])
    _relevance(ws.pool, about)
    last, wins = _past(root, now)
    for it in ws.pool:
        it.recent = RECENT if it.at and last and it.at > last else 0.0
        it.habituation = round(HABITUATION * wins.count(it.key()), 3)
    last_goal = ""
    for ep in reversed(surprises.episodes(root)):
        if ep.get("kind") == "live" and (ep.get("intent") or {}).get("goal"):
            last_goal = ep["intent"]["goal"]
            break
    ws.prior = f"{last_goal}\n{plan}"
    ws.score()
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

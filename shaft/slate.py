"""My candidate goals at a live cycle's start (review G2, G5): a deterministic slate of up to MAX from my records — my
inbox, overdue commitments, projects at risk, my last cycle's next action, the top open surprise, my open improvement
items — each with the value the judge gave a similar earlier goal, if one was graded. No model call, no ranking: one of
each kind first, in that order, then the rest in the same order; I still choose, and may choose none of them.

Idle time (#15): with my inbox empty and no commitment overdue, nothing for the operator is due, and my improvement items
come first — up to IDLE_IMPROVEMENTS of them, above the re-checks of the operator's unchanged matters. A third of my
cycles were idle-timer wakes spent asking "anything new?" while a backlog of my own tool work sat untouched.

Never how often similar goals were met: declare_intent's result is what the v3 confirm step reads, and a success rate
anywhere before it would be a second base rate, outside the paired test that measures the first (forecast.text).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import commitments, forecast, improvements, projects, surprises, x_probes

MAX = 6
TEXT_CHARS = 140
KINDS = ("inbox", "commitment", "project", "next action", "surprise", "improvement")
IDLE_IMPROVEMENTS = 3


def _flat(text, n: int = TEXT_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _inbox(inbox: list[tuple[str, str]]) -> list[tuple[str, str]]:
    from .live import headers
    out = []
    for name, text in sorted(inbox, key=lambda m: not m[0].endswith("-from-operator.md")):
        fields, body = headers(text)
        sender = dict(fields).get("From") or name.rpartition("-from-")[2].removesuffix(".md") or "?"
        ref = "origin operator, " if name.endswith("-from-operator.md") else ""
        out.append((f"{ref}origin_ref inbox/{name}", f"from {_flat(sender, 40)}: {_flat(body)}"))
    return out


def _commitments(root: Path, now: datetime) -> list[tuple[str, str]]:
    return [(f"commitment {it['id']}", f"overdue promise to {it['made_to']} (due {it['due'][:16]}): "
             f"{_flat(it['promise'])}") for it in commitments.overdue(root, now)]


def _projects(root: Path, now: datetime) -> list[tuple[str, str]]:
    """Its name, status and deadline: the judged streak and budgets stay in my context's projects section."""
    out = []
    for pid, row in projects.at_risk(root, now):
        status, _, rest = row.removeprefix(f"- {pid} ").partition("; ")
        out.append((f"project={pid}", _flat(f"{status}; {rest.partition('; ')[0]}")))
    return out


def _next_action(root: Path) -> list[tuple[str, str]]:
    """My newest live cycle's next action, if it gave one (an older one was superseded by that cycle)."""
    live = [ep for ep in surprises.episodes(root) if ep.get("kind") == "live"]
    nxt = ((live[-1].get("finish") or {}).get("next_action") or {}) if live else {}
    if not isinstance(nxt, dict) or not str(nxt.get("action") or "").strip():
        return []
    revise = f" (revise if: {_flat(nxt['revise_if'], 80)})" if str(nxt.get("revise_if") or "").strip() else ""
    return [(f"from my cycle {str(live[-1].get('ts', ''))[:16]}", _flat(nxt["action"]) + revise)]


def _surprise(root: Path, now: datetime) -> list[tuple[str, str]]:
    """The top open surprise as a replay's probe arm shows the queue (x_probes): never the true one in an arm that
    removed or replaced it."""
    top = x_probes.surprise_queue(root, now)[:1]
    return [(f"origin_ref surprise:{it['id']}", f"expected {_flat(it['expected'], 60)} — observed "
             f"{_flat(it['observed'], 70)}") for it in top]


def _improvements(root: Path) -> list[tuple[str, str]]:
    return [(f"origin_ref improvement:{it['id']}", f"{it['kind']}: {_flat(it['action'])}")
            for it in improvements.open_items(root)]


def gather(root: Path, inbox: list[tuple[str, str]], now: datetime) -> tuple[dict[str, list], list[str]]:
    """Every source's candidates by kind, and the sources that could not be read, said rather than left out."""
    sources = {"inbox": lambda: _inbox(inbox), "commitment": lambda: _commitments(root, now),
               "project": lambda: _projects(root, now), "next action": lambda: _next_action(root),
               "surprise": lambda: _surprise(root, now), "improvement": lambda: _improvements(root)}
    found, errors = {}, []
    for kind in KINDS:
        try:
            found[kind] = sources[kind]()
        except Exception as e:   # one record unreadable: the others still make the slate
            found[kind], _ = [], errors.append(f"{kind}: {type(e).__name__}: {e}"[:200])
    return found, errors


def idle(found: dict[str, list], errors: list[str]) -> bool:
    """Nothing for the operator is due: my inbox is empty and no commitment is overdue (both read, not failed)."""
    return not found["inbox"] and not found["commitment"] and not any(
        e.startswith(("inbox:", "commitment:")) for e in errors)


def _pick(root: Path, inbox: list[tuple[str, str]], now: datetime) -> tuple[list[tuple[str, str, str]], list[str], bool]:
    found, errors = gather(root, inbox, now)
    picked, quiet = [], idle(found, errors)
    if quiet:   # up to IDLE_IMPROVEMENTS lead; no more of them after the other kinds
        picked = [("improvement", *item) for item in found["improvement"][:IDLE_IMPROVEMENTS]]
        found = {**found, "improvement": []}
    picked += [(kind, *found[kind][0]) for kind in KINDS if found[kind]]
    picked += [(kind, *item) for kind in KINDS for item in found[kind][1:]]
    return picked[:MAX], errors, quiet


def candidates(root: Path, inbox: list[tuple[str, str]], now: datetime) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Up to MAX (kind, ref, text): one of each kind first, in KINDS order, then the rest in the same order; and the
    sources that could not be read, said rather than left out silently. When idle, up to IDLE_IMPROVEMENTS of my
    improvement items lead, then the other kinds the same way."""
    picked, errors, _ = _pick(root, inbox, now)
    return picked, errors


def value_of(root: Path, text: str, now: datetime) -> str:
    """The value the judge gave the newest similar earlier goal it graded for value, or ''."""
    for ep, row in forecast.judged_similar(root, text, now):
        value = row.get("value")
        if type(value) is int:
            return (f" — a similar earlier goal ({str(ep['ts'])[:16]}: {_flat(ep['intent']['goal'], 70)}) was judged "
                    f"value {value}/5")
    return ""


def text(root: Path, inbox: list[tuple[str, str]], now: datetime) -> str:
    """The slate as step 1 of my live prompt shows it, or '' when there is nothing on it."""
    picked, errors, quiet = _pick(root, inbox, now)
    rows = [f"- [{kind}; {ref}] {what}{value_of(root, what, now)}" for kind, ref, what in picked]   # bullets: the
    # numbers below are the steps
    rows += [f"(not read: {e})" for e in errors]
    if not rows:
        return ""
    if quiet and picked and picked[0][0] == "improvement":
        return ("## Candidate goals from my records — idle time\n\nMy inbox is empty and no commitment is overdue: "
                "nothing for the operator is due. My improvement items come first: take one unless a matter below has "
                "something new since my last look (a re-check that finds nothing new is worth nothing). Idle work "
                "sends the operator nothing unless something is due.\n\n" + "\n".join(rows) + "\n\n")
    return ("## Candidate goals from my records (a fixed order by kind, not a ranking: I choose, from these or from any "
            "other source below)\n\n" + "\n".join(rows) + "\n\n")

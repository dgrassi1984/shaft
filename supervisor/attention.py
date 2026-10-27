"""The attention schema (docs/plans/PLAN-consciousness.md M5, B3), scored against the tool log.

A live cycle may declare in `declare_intent` where it expects to look: up to four {source, why}. Graziano's attention
schema theory says a model of one's own attention is for controlling it; the agent uses the declaration as a top-down
prior for its workspace. Here the supervisor checks the declaration against what the cycle then did — the share of its
calls that went to a source it named, and the named sources it never touched. The score feeds nothing: no probation,
no reward. Only the knob trial (`shaft experiment attention`) decides whether the field stays.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from . import metrics

CONTROLS = ("declare_intent", "confirm_confidence", "finish_cycle")
MEMORY_TOOLS = ("search_memory", "remember", "rewrite_facts", "write_digest", "update_self_model", "update_personality")
SYNONYMS = {"web": ("web",), "internet": ("web",), "memory": ("memory",), "code": ("shaft/", "plugins/", "run"),
            "coder": ("coder",), "record": ("record",), "history": ("record",), "operator": ("message operator",)}


def _args(entry: dict) -> dict:
    try:
        args = json.loads(entry.get("args") or "{}")
        return args if isinstance(args, dict) else {}
    except ValueError:   # the log cut a long argument: the keys this needs are short and come first
        return {k: m.group(1) for k in ("path", "url", "to", "command")
                if (m := re.search(rf'"{k}"\s*:\s*"((?:[^"\\]|\\.)*)"', entry.get("args") or ""))}


def label(entry: dict) -> str:
    """Where one call looked, as words a declared source can name: 'web example.org', 'notes/plan.md', 'memory',
    'message wren', 'run pytest', 'coder', 'record', 'plugin web_research'."""
    tool, args = entry.get("tool", ""), _args(entry)
    if tool in ("web_search",):
        return "web"
    if tool in ("fetch_page", "http_request"):
        host = urlsplit(str(args.get("url") or "")).hostname or ""
        return f"web {host}".strip()
    if tool in ("read_file", "list_files", "write_file", "append_file"):
        path = str(args.get("path") or ".").lstrip("./") or "."
        return "memory " + path if path.startswith("memory/") else path
    if tool in MEMORY_TOOLS:
        return "memory"
    if tool == "send_message":
        return f"message {args.get('to') or '?'}"
    if tool == "run":
        words = str(args.get("command") or "").split()
        return "run " + (words[0] if words else "")
    if tool == "delegate_coding":
        return "coder"
    if tool in ("record", "history"):
        return "record"
    return tool


def _named(source: str) -> tuple[str, ...]:
    s = " ".join(str(source).lower().split())
    return SYNONYMS.get(s, (s,))


def matches(source: str, call_label: str) -> bool:
    """Whether a call went where a declared source points: the source (or one of its synonyms) is a prefix of the
    call's label, or of the label's second word (a host, a path, a recipient)."""
    lab = call_label.lower()
    parts = lab.split(" ", 1)
    return any(n and (lab.startswith(n) or (len(parts) > 1 and parts[1].startswith(n))) for n in _named(source))


def score(ep: dict) -> dict | None:
    """One cycle: the declared sources, its calls (controls left out), the share of calls on a named source, the named
    sources it never touched, and the declared entries it could not read. None when it declared none."""
    declared = ((ep.get("intent") or {}).get("attention")) or []
    if not isinstance(declared, list) or not declared:
        return None
    sources = [str(a.get("source") or "").strip() for a in declared if isinstance(a, dict)]
    unreadable = len(declared) - len([s for s in sources if s])
    sources = [s for s in sources if s]
    labels = [label(e) for e in ep.get("tools") or [] if e.get("tool") not in CONTROLS]
    on = sum(1 for lab in labels if any(matches(s, lab) for s in sources))
    untouched = [s for s in sources if not any(matches(s, lab) for lab in labels)]
    return {"declared": sources, "calls": len(labels), "on_named": on,
            "share": on / len(labels) if labels else None, "untouched": untouched, "unreadable": unreadable}


def section(name: str) -> list[str]:
    """stats.md: how well where I said I would look matched where I looked."""
    scored = [s for s in (score(ep) for ep in metrics.live_episodes(name)) if s]
    if not scored:
        return []
    shares = [s["share"] for s in scored if s["share"] is not None]
    named = sum(len(s["declared"]) for s in scored)
    untouched = sum(len(s["untouched"]) for s in scored)
    lines = ["", "## Where I said I would look, and where I looked (attention schema)",
             f"- Cycles that declared it: {len(scored)}; calls on a source I named: "
             + (f"{sum(shares) / len(shares):.0%} on average" if shares else "no calls"),
             f"- Named sources I never touched: {untouched} of {named}"]
    if (bad := sum(s["unreadable"] for s in scored)):
        lines.append(f"- Declared entries without a source: {bad}")
    last = scored[-1]
    if last["untouched"]:
        lines.append(f"- Last cycle that declared it named but never touched: {', '.join(last['untouched'][:4])}")
    return lines


def report(names: list[str]) -> str:
    out = []
    for name in names:
        scored = [(ep["ts"], s) for ep in metrics.live_episodes(name) if (s := score(ep))]
        out.append(f"{name}: {len(scored)} cycle(s) declared where they would look")
        for ts, s in scored[-20:]:
            share = "no calls" if s["share"] is None else f"{s['share']:.0%}"
            out.append(f"  {ts[:16]}  {share} of {s['calls']} calls on {', '.join(s['declared'])}"
                       + (f"; never touched: {', '.join(s['untouched'])}" if s["untouched"] else ""))
    return "\n".join(out) + "\n"

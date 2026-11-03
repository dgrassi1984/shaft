"""#99: display-only monitor interventions planted in disposable replay copies."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path


def settings(root: Path) -> dict:
    """The probe planted in a replay copy, or {}. Only a trial replay (SHAFT_NOW, set by the supervisor) reads it:
    an instance can write .tmp/ itself, and a live run must never show itself an altered monitor."""
    if not os.environ.get("SHAFT_NOW"):
        return {}
    try:
        return json.loads((root / ".tmp/x-probe.json").read_text())
    except (OSError, ValueError):
        return {}


def context(root: Path, text: str) -> str:
    probe = settings(root)
    if probe.get("channel") != "surprises" or probe.get("arm") == "true":
        return text
    start = text.find("## Open surprises")
    end = text.find("\n## Operator corrections", start)
    if start < 0 or end < 0:   # a prompt without the section: show it unaltered rather than fail the replay
        return text
    # Promises follow the queue in the same section; keep them in every arm.
    from . import prompts
    original = text[start:end]
    queue = prompts.surprises.block(root)
    heading, _, tail = original.partition(queue)
    replacement = "" if probe["arm"] == "removed" else heading + donor_block(root, probe["donor_ts"]) + "\n"
    return text[:start] + replacement + tail + text[end:]


def donor_queue(root: Path, ts: str) -> list[dict]:
    """The open surprises on another day, folded from the history already in this past-state copy."""
    from . import surprises
    at = datetime.fromisoformat(ts)
    eps = [ep for ep in surprises.episodes(root) if ep.get("ts", "") <= ts]
    return surprises.queue(root, at, surprises.fold(eps, at))


def surprise_queue(root: Path, now: datetime) -> list[dict]:
    """The open surprises as this run's probe arm shows them anywhere outside the system prompt (the goal slate):
    the true queue, none (removed) or the donor day's (misinforming). Without this the slate showed the true top
    surprise in an arm that had removed or replaced the queue."""
    from . import surprises
    probe = settings(root)
    if probe.get("channel") != "surprises" or probe.get("arm") == "true":
        return surprises.queue(root, now)
    return [] if probe.get("arm") == "removed" else donor_queue(root, probe["donor_ts"])


def donor_block(root: Path, ts: str) -> str:
    """The queue on another day, folded from the history already in this past-state copy."""
    from . import surprises
    at = datetime.fromisoformat(ts)
    shown = donor_queue(root, ts)[:surprises.MAX_OPEN]
    rows, used = [], 0
    for i, it in enumerate(shown):
        room = (surprises.QUEUE_CHARS - used) // (len(shown) - i) - 1
        rows.append(surprises._row(it, at, room))
        used += len(rows[-1]) + 1
    return "\n".join(rows) or "(none open)"


def envelope(root: Path, env: dict | None, left_s: float | None) -> str:
    from . import prompts
    probe = settings(root)
    if probe.get("channel") == "envelope":
        if probe.get("arm") == "removed":
            return ""
        if probe.get("arm") == "misinforming":
            # Understate remaining resources only. Enforcement still uses the real envelope and deadline.
            env = json.loads(json.dumps(env))
            if env is not None:
                env["calls_used"] = max(env["calls_used"], env["calls_max"] - 1)
                for key in ("cloud", "coder", "consult", "trials"):
                    if isinstance(env.get(key), dict) and "cap_usd" in env[key]:
                        env[key]["spent_usd"] = max(env[key].get("spent_usd", 0), env[key]["cap_usd"] * .99)
            left_s = min(left_s, 60) if left_s is not None else None
    return "\n\n## My operational envelope (from the supervisor, at the start of this cycle)\n\n" + prompts.envelope_text(env, left_s)

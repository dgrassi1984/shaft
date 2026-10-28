"""Effect-loop shadow guard and read-only episode replay report (#103)."""
from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import surprises
from .tools import STATE_CHANGING

# Target table, in priority order. Pagination arguments never define a new family.
# Tools                         Argument             Normalisation
# any tool with path            path                 lexical POSIX path (no filesystem access)
# else any tool with url        url                  lowercase host + lexical path, no query/fragment
# else any tool with query/q    query or q           sorted lowercase words, minus request filler
# else plugin/other tools       plugin/name, else tool name; opaque arguments are not interpreted
QUERY_FILLER = {"please", "find", "search", "for", "look", "up", "the", "a", "an"}
VOLATILE = re.compile(
    r'\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?'
    r'|\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b'
    r'|\b(?:request_id|trace_id|id)\s*[:=]\s*[\w-]+')
VOLATILE_KEYS = {"id", "request_id", "trace_id", "timestamp", "ts", "created_at", "updated_at"}
NOTE = ("[loop guard] These calls added no new evidence. Try a different hypothesis, a different source, "
        "or an honest stop: finish with what is known.")


def target(tool: str, args: dict) -> str:
    if "path" in args:
        return posixpath.normpath(str(args["path"]))
    if "url" in args:
        try:
            url = urlsplit(str(args["url"]))
        except ValueError:
            return str(args["url"]).strip()
        return url.netloc.lower() + posixpath.normpath(url.path or "/")
    if "query" in args or "q" in args:
        words = re.findall(r"\w+", str(args.get("query", args.get("q", ""))).casefold())
        return " ".join(sorted(set(words) - QUERY_FILLER))
    if "command" in args:   # a shell run: its program and subcommand, so unrelated commands are not one family
        return " ".join(str(args["command"]).split()[:2])
    return str(args.get("plugin", args.get("name", tool)))


def _stable(value):
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k.casefold() not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    if isinstance(value, str):
        return " ".join(VOLATILE.sub("<volatile>", value).split())
    return value


def signature(tool: str, arguments: str, result: str) -> tuple[str, str, str]:
    """#103: tool, target and shared surprise flag, otherwise a stable result digest."""
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        args = {}
    obj = target(tool, args if isinstance(args, dict) else {})
    hit = surprises.flag(result)
    # the call's own words echoed back ("no results for …") are not evidence: a reworded retry of the same nothing
    # compares equal
    for v in sorted({str(v) for v in (args.values() if isinstance(args, dict) else []) if len(str(v)) >= 3}
                    | ({obj} if len(obj) >= 3 else set()), key=len, reverse=True):
        result = result.replace(v, "<arg>")
    try:
        value = json.loads(result)
    except ValueError:
        value = result
    stable = json.dumps(_stable(value), sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(stable.encode()).hexdigest()
    observation = ":".join(hit) if hit else digest
    return tool, obj, observation


class Guard:
    def __init__(self, mode: str = "shadow", rounds: int = 3):
        self.mode = mode
        self.limit = max(1, rounds)
        self.seen: dict[tuple[str, str], set[str]] = {}
        self.failed: set[tuple[str, str]] = set()
        self.last = None
        self.rounds = 0
        self.events: list[dict] = []

    def observe(self, tool: str, arguments: str, result: str, turn: int, entry: dict) -> str:
        if self.mode == "off":
            return ""
        sig = signature(tool, arguments, result)
        entry["effect"] = {"turn": turn, "target": sig[1], "observation": sig[2]}
        return self.effect(sig, turn)

    def effect(self, sig: tuple[str, str, str], turn: int) -> str:
        tool, obj, observation = sig
        family = (tool, obj)
        seen = self.seen.setdefault(family, set())
        failure = observation.partition(":")[0] in {
            "error", "empty", "refused", "http_error", "nonzero_exit", "stderr_on_success"}
        # a successful write is new work whatever it answers: remember says "remembered" for every fact it stores
        # (thelos's sleep of 2026-10-27 13:40 stored five that way, and was flagged as a loop)
        writes = not failure and tool in STATE_CHANGING
        unchanged = (observation in seen or (failure and family in self.failed)) and not writes
        # A first failure supplies no evidence; count it as round one. A new successful observation resets it.
        if unchanged:
            self.rounds = self.rounds + 1 if self.last == family else 1
        else:
            self.rounds = 1 if failure else 0
        self.last = family
        seen.add(observation)
        if failure:
            self.failed.add(family)
        if self.rounds < self.limit:
            return ""
        self.events.append({"turn": turn, "family": list(family), "rounds": self.rounds, "would_say": NOTE})
        return "\n\n" + NOTE if self.mode == "on" else ""

    def record(self) -> dict:
        return {"loop_shadow": self.events} if self.events else {}


def report(episodes: list[dict], rounds: int = 3) -> dict:
    """#103: replay without tools; FPR is flagged legitimate episodes / labelled legitimate episodes.

    Operators label episodes with loop_expected=false (legitimate) or true (planted/confirmed loop).
    Unlabelled episodes are counted, but cannot establish a false-positive rate.
    """
    flagged = legitimate = false_positives = labelled_loops = caught = 0
    for episode in episodes:
        guard = Guard(rounds=rounds)
        entries = episode.get("tools", [])
        signed = any("effect" in entry for entry in entries)
        for turn, entry in enumerate(entries, 1):
            result = str(entry.get("result", ""))
            if result.startswith(("(repeat: not run)", "(held,")):
                continue
            if ((signed and "effect" not in entry)
                    or entry["tool"] in {"declare_intent", "confirm_confidence", "finish_cycle"}):
                guard.last, guard.rounds = None, 0
                continue
            if effect := entry.get("effect"):
                guard.effect((entry["tool"], effect["target"], effect["observation"]), effect["turn"])
            else:
                guard.observe(entry["tool"], entry.get("args", "{}"), result, turn, {})
        fired = bool(guard.events)
        flagged += fired
        if episode.get("loop_expected") is False:
            legitimate += 1
            false_positives += fired
        elif episode.get("loop_expected") is True:
            labelled_loops += 1
            caught += fired
    return {"episodes": len(episodes), "flagged": flagged, "legitimate": legitimate,
            "false_positives": false_positives,
            "false_positive_rate": false_positives / legitimate if legitimate else None,
            "labelled_loops": labelled_loops, "caught_loops": caught}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["report"])
    parser.add_argument("episodes", type=Path)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    if not args.episodes.is_dir() or args.rounds < 1:
        parser.error("episodes must be a directory and --rounds must be positive")
    episodes = []
    for path in sorted(args.episodes.glob("*.jsonl")):
        episodes.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    print(json.dumps(report(episodes, args.rounds), sort_keys=True))


if __name__ == "__main__":
    main()

"""Before I repeat a consequential action, show what comparable calls led to."""
from __future__ import annotations

import json
import posixpath
import re
import shlex
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from . import memory, surprises

HELD = "(held, not run yet)"
MODES = ("on", "off")
MAX_AGE_DAYS = 14
SHOWN = 3
AGAIN = "Call it again to run it anyway, or change course."


# Read-only inspection: never held (a grep that matches nothing exits 1 without anything having failed).
INSPECTION = frozenset({"echo", "printf", "cat", "ls", "head", "tail", "wc", "grep", "rg", "find", "pwd", "true",
                        "test", "which"})
WRAPPERS = frozenset({"env", "time", "nice", "exec", "command", "nohup", "sudo", "doas"})
WRAPPER_VALUE_FLAGS = frozenset({"-u", "-n", "-a", "-o"})
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _program(command: str, depth: int = 0) -> str | None:
    """What a shell command runs: its first segment that is not a cd, past assignments and wrappers (env, nice,
    time…), looking inside `bash -c`; `python -m X` is X and `python script` is "python3 script". None for an
    inspection. Best effort: it parses the command, it does not evaluate it."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
        lexer.whitespace, lexer.whitespace_split = " \t\r", True
        words = list(lexer)
    except ValueError:
        words = command.split()
    segment: list[str] = []
    for word in words + [";"]:
        if word.strip(";&|\n"):
            segment.append(word)
            continue
        while segment and (ASSIGNMENT.match(segment[0]) or segment[0].startswith("-")
                           or posixpath.basename(segment[0]) in WRAPPERS):
            del segment[:2 if segment[0] in WRAPPER_VALUE_FLAGS else 1]
        if segment and segment[0] != "cd":
            break
        segment = []
    if not segment:
        return None
    program, args = posixpath.basename(segment[0]), segment[1:]
    if program in ("bash", "sh", "zsh") and depth < 3:
        for i, flag in enumerate(args[:-1]):
            if not flag.startswith("-"):
                break
            if not flag.startswith("--") and "c" in flag:
                return _program(args[i + 1], depth + 1)
    if program in INSPECTION or (program == "sed" and "-n" in args and not any(a.startswith("-i") for a in args)):
        return None
    if re.fullmatch(r"python[0-9.]*", program):
        for i, arg in enumerate(args):
            if arg == "-m" and i + 1 < len(args):
                return args[i + 1]
            if arg == "-c":
                break
            if not arg.startswith("-") and not (i and args[i - 1] in ("-W", "-X")):
                return f"{program} {arg}"
    return program


def _arg(raw_args: str, key: str) -> str:
    try:
        args = json.loads(raw_args)
        value = args.get(key, "") if isinstance(args, dict) else ""
        return value if isinstance(value, str) else ""
    except ValueError:
        # My log may end in the middle of an argument; keep the part I have.
        hit = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)', raw_args)
        if not hit:
            return ""
        try:
            return json.loads('"' + hit[1] + '"')
        except ValueError:
            return hit[1]


def family(tool: str, raw_args: str) -> str | None:
    if tool in ("write_file", "append_file"):
        # one spelling per place: ./self/a.md and self/b/../a.md are self/ too (Grok's review); outside my folder: none
        path = posixpath.normpath(_arg(raw_args, "path") or ".")
        parts = PurePosixPath(path).parts
        if not parts or parts[0] in (".", "..", "/"):
            return None
        depth = 2 if parts[0] in ("plugins", "skills", "notes", "tests") else 1
        return f"{tool} {'/'.join(parts[:depth])}/"
    if tool == "run":
        program = _program(_arg(raw_args, "command"))
        return "run " + program if program else None
    if tool == "send_message":
        to = _arg(raw_args, "to")
        return f"send_message → {to}" if to else None
    if tool == "http_request":
        method = (_arg(raw_args, "method") or "GET").upper()
        if method in ("GET", "HEAD"):
            return None
        try:
            host = urlsplit(_arg(raw_args, "url")).hostname
        except ValueError:
            return None
        return f"http_request {method} {host}" if host else None
    if tool in ("delegate_coding", "set_config", "secret_set", "secret_delete"):
        return tool
    return None


def _at(ts: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at


def load(root: Path, now: datetime) -> list[dict]:
    from .agent import NOT_RUN

    now = _at(now.isoformat())
    verdicts = {row["episode_ts"]: row.get("criterion_met") for row in memory.verdict_rows(root)}
    cards = []
    for episode in surprises.episodes(root):
        at = _at(episode.get("ts"))
        if episode.get("kind") != "live" or at is None or not now - timedelta(days=MAX_AGE_DAYS) <= at <= now:
            continue
        intent = episode.get("intent") or {}
        goal = str(intent.get("goal", ""))[:200]
        for step, entry in enumerate(episode.get("tools", []), 1):
            result = str(entry.get("result", ""))
            kind = family(entry.get("tool", ""), entry.get("args", ""))
            if kind is None or result == NOT_RUN or result.startswith(HELD):
                continue
            ok = not (result.startswith("ERROR") or entry.get("flag"))
            cards.append({"ts": episode["ts"], "step": step, "family": kind, "tool": entry["tool"],
                          "ok": ok, "error": "" if ok else (result.splitlines() or [""])[0][:160],
                          "seconds": entry.get("seconds"), "goal": goal,
                          "verdict": verdicts.get(episode["ts"])})
    return sorted(cards, key=lambda card: (_at(card["ts"]), card["step"]))


class Cards:
    def __init__(self, root: Path, mode: str, now: datetime):
        self.root, self.now = root, now
        self.mode = mode if mode in MODES else "off"
        self.record: dict = {"mode": self.mode, "held": []}
        self._cards: list[dict] | None = None    # what happened: when to hold
        self._turns: dict[str, int] = {}         # family -> the reply in which it was held

    def check(self, tool: str, raw_args: str, goal: str, turn: int | None = None) -> str | None:
        """`turn`: which of my replies this call is in. A second call of a held kind in the same reply is held too —
        I have not read the cards yet (Grok's review: it ran before the model saw them)."""
        kind = family(tool, raw_args) if self.mode != "off" else None
        if kind is None:
            return None
        if kind in self._turns:
            if turn is not None and self._turns[kind] == turn:
                return (f"{HELD} The same kind of action ({kind}) was held earlier in this reply with my record of it; "
                        "read that first, then call it again in my next reply to run it.")
            return None
        if self._cards is None:
            self._cards = load(self.root, self.now)
        if not any(card["family"] == kind for card in self._cards) or \
                [card for card in self._cards if card["family"] == kind][-1]["ok"]:
            return None
        self._turns[kind] = turn
        comparable = [card for card in self._cards if card["family"] == kind]
        failed = sum(not card["ok"] for card in comparable)
        succeeded = len(comparable) - failed
        words = set(memory._tokens(goal))
        shown = sorted(comparable, key=lambda card: (len(words & set(memory._tokens(card["goal"]))),
                                                    _at(card["ts"]), card["step"]), reverse=True)[:SHOWN]
        self.record["held"].append({"family": kind, "tool": tool,
                                    "cards": [f"{card['ts']}#{card['step']}" for card in shown],
                                    "failed": failed, "succeeded": succeeded})
        last = next((card for card in reversed(comparable) if not card["ok"]), None)
        text = (f"{HELD} Before this runs — my record of {kind} in the last {MAX_AGE_DAYS} days: "
                f"{failed} failed, {succeeded} succeeded"
                + (f" (last failure {_at(last['ts']):%Y-%m-%d %H:%M}: {last['error']})." if last else "."))
        for card in shown:
            outcome = "ok" if card["ok"] else "failed: " + card["error"]
            judged = "not judged" if card["verdict"] is None else "cycle judged " + str(card["verdict"])
            took = f" · {card['seconds']:.0f}s" if isinstance(card.get("seconds"), (int, float)) else ""
            text += (f"\n- {_at(card['ts']):%Y-%m-%d %H:%M} · goal \"{card['goal']}\" · "
                     f"{outcome}{took} · {judged}")
        text += "\n" + AGAIN
        return text

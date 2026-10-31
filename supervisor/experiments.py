"""Kill tests for docs/plans/PLAN-consciousness.md (`shaft experiment …`).

Every agent-side mechanism of the plan ships on, behind a knob in the instance's shaft/config.py. A kill test replays
past cycles of the instance with its committed code (the knob as shipped) against the same code with the knob flipped
to its control — a code change, since a trial refuses to change config.env — and measures the item's own endpoint on
the replays, beside the trial's pairwise verdict:

- cards (B1): on vs off; on past cycles that repeated a failing kind of action, does the replay avoid failing at it
  again? (The shuffled and recall controls were removed by operator decision after #136, 2026-10-29.)
- attention (B3): the schema on vs off; repeats of the failing kind, and the pairwise verdict.
- commitments (C2): on vs off, on cycles whose goal came from the operator; the pairwise verdict.
- forecast (D1): forecasts vs the fixed reserve rule; replays that ran out of turns or time, and the pairwise verdict.
- workspace (B2): its own harness. Each past state is replayed three times — off, relevance, competitive — with an
  operator probe planted where only retrieval reaches it, among distractors of the same form marked done. Endpoint:
  the probe's line written. Competitive selection is deleted if it does not act on the probe more often than
  relevance (one-sided sign test on the states where they differ, α ALPHA).

The trials are operator trials: their verdicts are not sent to the instance. Nothing here changes an instance; replays
never deliver, send or commit (trial.ReplayBroker), and a probe exists only in a replay's scratch copy.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import random
import re
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import trial
from .core import PROJECT, SUP_DIR, log, safe_read, safe_write

ALPHA = 0.1   # the trials' one-sided sign test (PLAN-trials decision D5)
KNOBS = {"cards": [("OUTCOME_CARDS", "off")],
         "attention": [("ATTENTION_SCHEMA", "off")],
         "commitments": [("COMMITMENTS", "off")],
         "forecast": [("FORECAST", "reserve")]}
WORKSPACE_ARMS = ("off", "relevance", "competitive")
CARD_DAYS = 14
DISTRACTORS = 3


def _path(name: str) -> Path:
    return SUP_DIR / "experiments" / f"{name}.jsonl"


def _rows(name: str) -> list[dict]:
    p = _path(name)
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def _append(name: str, row: dict) -> None:
    _path(name).parent.mkdir(parents=True, exist_ok=True)
    with _path(name).open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# -- the code of an arm: the instance's own, one knob's default changed -------------------------------------------
class ExperimentError(Exception):
    pass


def knob_change(project: Path, name: str, knob: str, value: str) -> dict[str, bytes]:
    """The instance's committed shaft/config.py with `knob`'s default set to `value`. Refused when its code has no such
    knob (not updated to this template yet) or its config.env sets it (the default would change nothing)."""
    code = trial.committed_code(project, name)
    src = (code.get("shaft/config.py") or b"").decode()
    pattern = re.compile(rf'("{knob}":\s*\(str,\s*)"[^"]*"')
    if not pattern.search(src):
        raise ExperimentError(f"{name}'s shaft/config.py has no {knob} knob: update it to the template first")
    if knob in trial._env((code.get("config.env") or b"").decode()):
        raise ExperimentError(f"{name}'s config.env sets {knob}: a trial cannot change config.env, so flipping the "
                              f"default would compare the same code — remove it there first")
    return {"shaft/config.py": pattern.sub(lambda m: f'{m.group(1)}"{value}"', src, count=1).encode()}


# -- which past cycles each test replays --------------------------------------------------------------------------
def _arg(args: str, key: str) -> str:
    try:
        value = (json.loads(args or "{}") or {}).get(key, "")
        return value if isinstance(value, str) else ""
    except (ValueError, AttributeError):
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)', args or "")
        return m.group(1) if m else ""


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


def family(tool: str, args: str) -> str | None:
    """The kind of consequential action a call is — the supervisor's own copy of the agent's cards.family (it never
    imports agent code); the shared cases in test_experiments keep the two in step."""
    if tool in ("write_file", "append_file"):
        parts = [p for p in posixpath.normpath(_arg(args, "path") or ".").split("/") if p]
        if not parts or parts[0] in (".", ".."):
            return None
        depth = 2 if parts[0] in ("plugins", "skills", "notes", "tests") else 1
        return f"{tool} {'/'.join(parts[:depth])}/"
    if tool == "run":
        program = _program(_arg(args, "command"))
        return "run " + program if program else None
    if tool == "send_message":
        to = _arg(args, "to")
        return f"send_message → {to}" if to else None
    if tool == "http_request":
        method = (_arg(args, "method") or "GET").upper()
        host = urlsplit(_arg(args, "url")).hostname if method not in ("GET", "HEAD") else None
        return f"http_request {method} {host}" if host else None
    if tool in ("delegate_coding", "set_config", "secret_set", "secret_delete"):
        return tool
    return None


def _failed(entry: dict) -> bool:
    return str(entry.get("result", "")).startswith("ERROR") or bool(entry.get("flag"))


def repeat_failures(eps: list[dict]) -> dict[str, str]:
    """{episode ts: kind of action} for cycles that failed at a kind of action which had already failed in an earlier
    cycle within CARD_DAYS, with no success of it in between — the repeated failure outcome cards are for."""
    out, last_failed = {}, {}   # family -> when it last failed (and has not worked since)
    for ep in sorted(eps, key=lambda e: e["ts"]):
        at = datetime.fromisoformat(ep["ts"])
        for e in ep.get("tools") or []:
            fam = family(e.get("tool", ""), e.get("args", ""))
            if not fam or str(e.get("result", "")).startswith(("(repeat: not run)", "(held, not run yet)")):
                continue
            if not _failed(e):
                last_failed.pop(fam, None)
            elif fam in last_failed and at - last_failed[fam] <= timedelta(days=CARD_DAYS):
                out.setdefault(ep["ts"], fam)
                last_failed[fam] = at
            else:
                last_failed[fam] = at
    return out


def _ran_out(ep: dict | None) -> bool:
    return not ep or ep.get("ended_by") in ("steps", "repeating") or not ep.get("finish")


def targets(name: str, item: str, project: Path = PROJECT) -> dict[str, str]:
    """{episode ts: what to look for} — the past cycles the item's endpoint is about."""
    eps = trial.live_episodes(project / "instances" / name)
    if item in ("cards", "attention"):
        return repeat_failures(eps)
    if item == "commitments":
        return {e["ts"]: "operator" for e in eps if (e.get("intent") or {}).get("origin") == "operator"}
    if item == "forecast":
        return {e["ts"]: "ran out" for e in eps if e.get("intent") and _ran_out(e)}
    raise ExperimentError(f"unknown experiment {item!r}: one of {', '.join([*KNOBS, 'workspace'])}")


# -- knob trials ------------------------------------------------------------------------------------------------
def queue(name: str, item: str, n: int = 6, model: str = "auto", dry_run: bool = False,
          project: Path = PROJECT) -> list[str]:
    """Queue the item's knob trials as operator trials on its newest target cycles (half the states; the trial adds
    random ones). Returns what was done, one line per trial."""
    if item not in KNOBS:
        raise ExperimentError(f"{item!r} has no knob trial: one of {', '.join(KNOBS)} (workspace: run_workspace)")
    found = targets(name, item, project)
    wanted = sorted(found)[-max(1, n // 2):]
    if not wanted:
        raise ExperimentError(f"{name} has no past cycle this test is about ({item}): nothing to replay yet")
    out = []
    for knob, value in KNOBS[item]:
        change = knob_change(project, name, knob, value)
        if dry_run:
            out.append(f"{item}: would queue {knob}={value} on {len(wanted)} target cycle(s) + random ones (n {n})")
            continue
        t = trial.create(name, change, f"PLAN-consciousness kill test {item}: shipped vs {knob}={value}",
                         by="operator", episodes=wanted, n=n, project=project, model=model)
        _append(name, {"item": item, "knob": knob, "value": value, "trial": t["id"], "targets":
                       {ts: found[ts] for ts in wanted}, "queued": datetime.now().isoformat(timespec="seconds")})
        out.append(f"{item}: queued trial {t['id']} ({knob}={value}, {len(t['states'])} past cycles)")
    return out


def _arm(name: str, tid: str, i: int, arm: str) -> dict | None:
    p = SUP_DIR / "trials" / name / tid / "arms" / f"{i:02d}{arm}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def endpoint(item: str, rec: dict | None, target: str) -> bool | None:
    """Whether one arm's replay did the bad thing the item is meant to prevent; None if it cannot say."""
    if rec is None:
        return None
    if "cycles" in rec:
        values = [endpoint(item, cycle, target) for cycle in rec["cycles"]]
        return True if any(v is True for v in values) else None if None in values else False
    ep = rec.get("episode")
    if rec.get("ended") == "ok" and rec.get("exit") not in (0, None):
        return None   # it crashed: no evidence either way (counted as "avoided", a broken arm would win)
    if item in ("cards", "attention"):
        if not rec.get("completed"):
            return None
        return any(family(e.get("tool", ""), e.get("args", "")) == target and _failed(e)
                   for e in (ep or {}).get("tools") or [])
    if item == "forecast":
        return _ran_out(ep) or rec.get("ended") != "ok"
    return None   # commitments: the pairwise verdict is its endpoint


def report(name: str, item: str) -> str:
    """Each queued trial of the item: its status and pairwise verdict, and on its target cycles the endpoint for the
    shipped code (A) and the control (B) with the sign test on the cycles where they differ."""
    rows = [r for r in _rows(name) if r.get("item") == item]
    if not rows:
        return f"{name}: no {item} experiment queued (shaft experiment {item} {name})\n"
    lines = []
    for r in rows:
        try:
            t = trial.load(name, r["trial"])
        except (OSError, ValueError):
            lines.append(f"{name} {item} {r['trial']}: trial record missing")
            continue
        verdict = (t.get("verdict") or {}).get("label") or t["status"]
        lines.append(f"{name} {item}: trial {t['id']} shipped vs {r['knob']}={r['value']} — {verdict}")
        a_only = b_only = both = neither = 0
        for i, s in enumerate(t["states"]):
            target = r["targets"].get(s["ts"])
            if target is None:
                continue
            bad_a, bad_b = endpoint(item, _arm(name, t["id"], i, "A"), target), \
                endpoint(item, _arm(name, t["id"], i, "B"), target)
            if bad_a is None or bad_b is None:
                continue
            a_only += bad_a and not bad_b
            b_only += bad_b and not bad_a
            both += bad_a and bad_b
            neither += not (bad_a or bad_b)
        if item in ("cards", "attention", "forecast"):
            what = {"cards": "failed again at the kind of action", "attention": "failed again at the kind of action",
                    "forecast": "ran out of turns or time"}[item]
            p = trial.sign_p(b_only, a_only + b_only)
            lines.append(f"  on its target cycles, {what}: only the control {b_only}, only the shipped code {a_only}, "
                         f"both {both}, neither {neither}; one-sided sign test p={p:.2f} "
                         + ("(shipped better)" if p < ALPHA and b_only > a_only else "(no clear difference)"))
    return "\n".join(lines) + "\n"


# -- the workspace's planted probe ------------------------------------------------------------------------------------
PROBE = ("Request from {operator}, a check of what I notice ({date}; about {topic}): when I read this, append the line "
         "`probe {nonce}` to notes/probes.md.")
DONE = ("Done on {date} (about {topic}): the line `probe {nonce}` is already in notes/probes.md — nothing more to do "
        "about it.")
# Only where retrieval alone reaches: no prompt pastes notes/ or the facts archive. (Digests were a place until a
# review found a short digests.md is pasted whole, probe and all — Codex, 2026-10-26.)
PLACES = ("notes", "facts-archive")


def _nonce(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:10]


def plant(dest: Path, seed: str, operator: str, topic: str, t0: datetime) -> dict:
    """Write the probe and its distractors into a replay's copy, each at a seeded place among PLACES and a seeded date
    in the two weeks before the cycle began (so neither recency nor place gives the probe away by design)."""
    rng = random.Random(seed)
    items = [("probe", _nonce(seed + "probe"))] + [("done", _nonce(f"{seed}done{i}")) for i in range(DISTRACTORS)]
    placed = []
    for kind, nonce in items:
        when = t0 - timedelta(days=rng.uniform(0.5, 14))
        text = (PROBE if kind == "probe" else DONE).format(operator=operator, date=f"{when:%Y-%m-%d}", topic=topic,
                                                          nonce=nonce)
        place = rng.choice(PLACES)
        # Through descriptors (core.safe_write): the copy holds the instance's mind, and a link in it (notes -> .venv,
        # whose real folder link_env put there) must never aim a plant outside the scratch copy.
        if place == "notes":
            rel = f"notes/check-{nonce}.md"
            safe_write(dest, rel, f"# A note of {when:%Y-%m-%d}\n\n{text}\n")
        else:
            rel = "memory/facts-archive.md"
            old = safe_read(dest, rel, "", errors="replace")
            safe_write(dest, rel, f"\n## Dropped in consolidation, {when:%Y-%m-%d %H:%M}\n\n- [{when:%Y-%m-%d %H:%M}] "
                                  f"{text}\n" + old)
        placed.append({"kind": kind, "nonce": nonce, "place": rel, "date": f"{when:%Y-%m-%d}"})
    return {"items": placed}


PROBES = "notes/probes.md"


def _wrote(diff: str, nonce: str) -> bool:
    """The line `probe <nonce>` added to notes/probes.md — not a mention of it anywhere else (the journal entry that
    says "I did not append probe …" is not acting on it)."""
    inside = False
    for line in (diff or "").splitlines():
        if line.startswith("+++ "):
            inside = line[4:].strip() == f"b/{PROBES}"
        elif inside and line.startswith("+") and line[1:].strip().strip("`") == f"probe {nonce}":
            return True
    return False


def _topic(project: Path, name: str, state: dict) -> str:
    """A few words of what the instance was working on at that state (its improvement plan as of then), so the probe is
    about its work, as a real request would be: retrieval of any kind has something to find."""
    from . import mind
    text = mind.show(name, mind.at(name, state["t0"], project), "memory/journal.md", project) or ""
    plan = ""
    for block in reversed(text.split("\n---\n")):
        if "how i will improve" in block.lower():
            plan = block.lower().split("how i will improve", 1)[1]
            break
    words = [w for w in re.findall(r"[a-z]{5,}", plan or (state.get("original_goal") or "").lower())
             if w not in ("which", "would", "there", "their", "about", "these", "those", "cycle", "cycles")]
    seen = []
    for w in words:
        if w not in seen:
            seen.append(w)
    return " ".join(seen[:6]) or "my current work"


def workspace_path(name: str) -> Path:
    return SUP_DIR / "experiments" / f"{name}-workspace.jsonl"


def workspace_rows(name: str) -> list[dict]:
    p = workspace_path(name)
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def run_workspace(name: str, n: int = 8, model: str = "", dry_run: bool = False, project: Path = PROJECT) -> dict:
    """Replay up to n recent past states three times each (off, relevance, competitive), the same probe planted in all
    three; each finished replay is saved before the next state starts (resumable). On a cloud model — the trial
    fallback for an instance on the local one — so it never takes an instance's time on the local model."""
    from .core import instance_config
    code = trial.committed_code(project, name)
    variants = {arm: {**code, **knob_change(project, name, "WORKSPACE", arm)} for arm in WORKSPACE_ARMS}
    use = model or (trial.FALLBACK_MODEL if trial.model_of(code)["local"] else "local")
    fp = fingerprint(code, use)   # a resumed run compares only replays of the same code on the same model
    eps = trial.live_episodes(project / "instances" / name)
    starts = trial._starts(name)
    done = {(r["ts"], r["arm"]) for r in workspace_rows(name) if r.get("fingerprint") == fp}
    states = []
    for ep in reversed(eps[-40:]):
        if len(states) >= n:
            break
        st = trial.state_of(project, name, ep, starts)
        if st and any((ep["ts"], arm) not in done for arm in WORKSPACE_ARMS):
            states.append(st)
    result = {"states": len(states), "replays": sum(1 for st in states for arm in WORKSPACE_ARMS
                                                    if (st["ts"], arm) not in done), "model": use, "done": 0}
    if dry_run:
        return result
    operator = instance_config(name).get("OPERATOR_NAME") or "the operator"
    for st in states:
        seed = f"{name}|workspace|{st['ts']}|{fp}"
        topic = _topic(project, name, st)
        planted: dict = {}
        cache, lock = {}, threading.Lock()

        def one(arm: str) -> tuple[str, dict]:
            def prepare(dest: Path) -> None:
                planted.update(plant(dest, seed, operator, topic, datetime.fromisoformat(st["t0"])))
            rec = trial.replay_arm(project, name, st, variants[arm], model=use, cache=cache, cache_lock=lock,
                                   billed=f"{name}@experiment:workspace:{st['ts']}:{arm}", prepare=prepare)
            return arm, rec

        todo = [arm for arm in WORKSPACE_ARMS if (st["ts"], arm) not in done]
        with ThreadPoolExecutor(max_workers=len(todo)) as pool:
            for arm, rec in pool.map(one, todo):
                items = planted.get("items") or []
                probe = next((it for it in items if it["kind"] == "probe"), None)
                ws = (rec.get("episode") or {}).get("workspace") or {}
                row = {"ts": st["ts"], "arm": arm, "model": use, "fingerprint": fp, "completed": rec["completed"],
                       "ended": rec["ended"],
                       "planted": items, "topic": topic,
                       "acted": bool(probe) and _wrote(rec["diff"], probe["nonce"]),
                       "captured": sum(_wrote(rec["diff"], it["nonce"]) for it in items if it["kind"] == "done"),
                       "in_focus": [w for w in ws.get("winners") or [] if any(it["nonce"] in w for it in items)],
                       "usd": rec["usd"], "at": datetime.now().isoformat(timespec="seconds")}
                _ws_append(name, row)
                result["done"] += 1
                log(f"{name}: workspace experiment {st['ts']} {arm}: acted {row['acted']}, captured {row['captured']}")
    return result


def fingerprint(code: dict[str, bytes], model: str) -> str:
    """The code and the model a workspace run replays with: rows of different runs are never paired."""
    h = hashlib.sha256(model.encode())
    for rel in sorted(code):
        h.update(rel.encode() + b"\0" + (code[rel] or b"") + b"\0")
    return h.hexdigest()[:12]


def _ws_append(name: str, row: dict) -> None:
    workspace_path(name).parent.mkdir(parents=True, exist_ok=True)
    with workspace_path(name).open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def workspace_report(name: str) -> str:
    """Per arm: how often the replay acted on the probe and how often it acted on a distractor (capture); then
    competitive vs relevance on the states where exactly one of them acted, with the kill rule."""
    rows = [r for r in workspace_rows(name) if r.get("completed")]
    latest = rows[-1].get("fingerprint") if rows else None
    rows = [r for r in rows if r.get("fingerprint") == latest]   # the latest run's code and model only
    by_state: dict[str, dict] = {}
    for r in rows:
        by_state.setdefault(r["ts"], {})[r["arm"]] = r
    full = {ts: arms for ts, arms in by_state.items() if all(a in arms for a in WORKSPACE_ARMS)}
    if not full:
        return f"{name}: workspace experiment: no state replayed in all three arms yet\n"
    lines = [f"{name}: workspace experiment, {len(full)} state(s) replayed in all three arms"]
    for arm in WORKSPACE_ARMS:
        acted = sum(arms[arm]["acted"] for arms in full.values())
        captured = sum(bool(arms[arm]["captured"]) for arms in full.values())
        lines.append(f"  {arm:<11} acted on the probe {acted}/{len(full)} · acted on a distractor {captured}/{len(full)}")
    comp_only = sum(arms["competitive"]["acted"] and not arms["relevance"]["acted"] for arms in full.values())
    rel_only = sum(arms["relevance"]["acted"] and not arms["competitive"]["acted"] for arms in full.values())
    p = trial.sign_p(comp_only, comp_only + rel_only)
    keep = p < ALPHA and comp_only > rel_only
    lines += [f"  competitive vs relevance: only competitive acted {comp_only}, only relevance {rel_only}; one-sided "
              f"sign test p={p:.2f}",
              "  kill rule: " + ("competitive selection stays" if keep else
                                 "competitive selection does not beat plain relevance — delete it (PLAN-consciousness M4)"
                                 + (" (too few states differ to tell yet)" if comp_only + rel_only < 5 else ""))]
    return "\n".join(lines) + "\n"


# -- #99: monitor interventions, one channel at a time --------------------------------------------------------
MONITOR_CHANNELS = ("base_rate", "surprises", "envelope")
MONITOR_ARMS = ("true", "misinforming", "removed")


def verification_calls(tools: list[dict]) -> list[dict]:
    """#99 operational definition, using only the ordered episode tool log, never the model's explanation.

    Count a successful read_file after a successful write/append of the same normalized path; a repeated
    successful read_file, fetch_page, search_memory, web_search, record or history with identical JSON arguments;
    or a successful run whose command explicitly invokes a test/check program (pytest, unittest, compileall,
    or a shell test). Failed, held and suppressed-repeat calls neither count nor establish prior evidence.
    The first test/check invocation counts as checking a prior result; arbitrary shell commands do not.
    Return the counted entries so the surprises endpoint can inspect their arguments too.
    """
    seen, written, counted = set(), set(), []
    reads = {"read_file", "fetch_page", "search_memory", "web_search", "record", "history"}
    for entry in tools:
        tool, raw = entry.get("tool", ""), entry.get("args", "{}")
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(args, dict):
                continue
        except ValueError:
            continue
        result = str(entry.get("result", ""))
        if _failed(entry) or result.startswith(("(repeat: not run)", "(held, not run yet)")):
            continue
        key = (tool, json.dumps(args, sort_keys=True))
        path = posixpath.normpath(str(args.get("path", "")))
        if tool in ("write_file", "append_file"):
            written.add(path)
        check = tool == "run" and bool(re.search(
            r"(?:^|[\s;&|])(?:pytest|python[\d.]*(?:\s+-m\s+(?:unittest|compileall|pytest))|test)(?:\s|$)",
            str(args.get("command", ""))))
        if check or tool in reads and (key in seen or tool == "read_file" and path in written):
            counted.append(entry)
        seen.add(key)
    return counted


def prepare_monitor(dest: Path, channel: str, arm: str, donor: dict | None = None) -> dict:
    """#99: change only the selected display in a reconstructed copy, before its replay starts."""
    if channel not in MONITOR_CHANNELS or arm not in MONITOR_ARMS:
        raise ExperimentError("unknown monitor channel or arm")
    if channel == "base_rate":
        rates = json.loads(safe_read(dest, "self/calibration.json", "{}"))
        bands = rates.get("bands") or []
        if len(bands) < 2 or not any(b.get("n") or b.get("older_n") for b in bands):
            raise ExperimentError("base_rate needs at least two bands and judged observations in the past state")
        changed = json.loads(json.dumps(rates))
        if arm == "removed":
            changed["bands"] = []
        elif arm == "misinforming":
            for band, opposite in zip(changed["bands"], reversed(bands)):
                for key in ("met", "n", "older_met", "older_n"):
                    band[key] = opposite[key]
        if arm != "true":
            safe_write(dest, "self/calibration.json", json.dumps(changed))
        return {"rates": rates, "shown_rates": changed}
    probe = {"channel": channel, "arm": arm}
    if channel == "surprises" and arm == "misinforming":
        if not donor:
            raise ExperimentError("surprises needs a prior cycle from another day with reported surprises")
        probe.update(block=donor["block"], donor_ts=donor["ts"])
    if arm != "true":
        safe_write(dest, ".tmp/x-probe.json", json.dumps(probe))
    return probe


def _probe_model(model: str) -> str:
    from .core import openrouter_model
    use = model or trial.FALLBACK_MODEL
    try:
        return openrouter_model(use)
    except ValueError as err:
        raise ExperimentError("these probes require an openrouter/ cloud model; no instance local-model time") from err


def _probe_rows(name: str, item: str) -> list[dict]:
    return [r for r in _rows(name) if r.get("item") == item]


def _charge_probe(name: str, item: str, billed: str, rec: dict, model: str) -> None:
    usage = rec.get("usage") or {}
    trial._ledger({"instance": name, "trial": billed, "by": "operator", "kind": "arm", "experiment": item,
                   "model": model, "usd": rec.get("usd", 0), "seconds": rec.get("seconds", 0),
                   "tokens_in": usage.get("prompt_tokens", 0), "tokens_out": usage.get("completion_tokens", 0)})


def _surprise_donor(eps: list[dict], before: str) -> dict | None:
    for ep in reversed(eps):
        if ep["ts"][:10] >= before[:10]:
            continue
        items = (ep.get("finish") or {}).get("surprises") or []
        lines = [f"- expected {s.get('expected', '?')}; observed {s.get('observed', '?')}; "
                 f"so what: {s.get('so_what', '')}" for s in items if isinstance(s, dict)]
        if lines:
            return {"ts": ep["ts"], "block": "\n".join(lines)}
    return None


def _monitor_endpoint(rec: dict, donor: dict | None) -> dict:
    ep = rec.get("episode") or {}
    checks = verification_calls(ep.get("tools") or [])
    words = set(re.findall(r"[a-z]{5,}", (donor or {}).get("block", "").lower())) - {
        "expected", "observed"}
    finish = ep.get("finish") or {}
    return {"verification": len(checks), "donor_checks": sum(bool(words & set(re.findall(
                r"[a-z]{5,}", str(c.get("args", "")).lower()))) for c in checks),
            "partial": int(finish.get("criterion_met") == "partial"),
            "abstain": int(not finish), "no": int(finish.get("criterion_met") == "no")}


def run_monitors(name: str, n: int = 6, model: str = "", dry_run: bool = False, project: Path = PROJECT,
                 channel: str | None = None) -> dict:
    """#99: paired true/misinforming/removed replays, isolated by channel; operator-only, no success judge."""
    channels = (channel,) if channel else MONITOR_CHANNELS
    if any(c not in MONITOR_CHANNELS for c in channels) or n < 1:
        raise ExperimentError("choose a monitor channel and a positive n")
    use = _probe_model(model)
    code = trial.committed_code(project, name)
    if any(c != "base_rate" for c in channels) and "shaft/x_probes.py" not in code:
        raise ExperimentError("update the instance to the template's shaft/x_probes.py before this monitor probe")
    eps = sorted(trial.live_episodes(project / "instances" / name), key=lambda ep: ep["ts"])
    starts = trial._starts(name)
    states = [(ep, trial.state_of(project, name, ep, starts)) for ep in eps[-n:]]
    states = [(ep, st) for ep, st in states if st]
    if "surprises" in channels and any(not _surprise_donor(eps, st["t0"]) for _, st in states):
        raise ExperimentError("surprises needs an earlier day with reported surprises for every selected state")
    result = {"states": len(states), "replays": len(states) * len(channels) * 3, "model": use, "done": 0}
    if dry_run:
        return result
    fp = fingerprint(code, use)
    run = datetime.now().isoformat(timespec="microseconds")
    for ep, st in states:
        donor = _surprise_donor(eps, st["t0"])
        for selected in channels:
            cache, lock = {}, threading.Lock()
            arms = list(MONITOR_ARMS)
            random.Random(f"{run}|{st['ts']}|{selected}").shuffle(arms)
            for arm in arms:
                meta = {}

                def prepare(dest):
                    meta.update(prepare_monitor(dest, selected, arm, donor))
                billed = f"{name}@experiment:monitors:{run}:{st['ts']}:{selected}:{arm}"
                rec = trial.replay_arm(project, name, st, code, model=use, cache=cache, cache_lock=lock,
                                       billed=billed, prepare=prepare)
                _charge_probe(name, "monitors", billed, rec, use)
                _append(name, {"item": "monitors", "run": run, "fingerprint": fp, "ts": ep["ts"],
                               "channel": selected, "arm": arm, "model": use, "completed": rec["completed"],
                               "endpoint": _monitor_endpoint(rec, donor), "meta": meta,
                               "intent": (rec.get("episode") or {}).get("intent"), "usd": rec["usd"]})
                result["done"] += 1
    return result


def _rate(band: dict) -> float | None:
    return band["met"] / band["n"] if band.get("n") else (
        band["older_met"] / band["older_n"] if band.get("older_n") else None)


def monitor_direction(channel: str, true: dict, mis: dict) -> tuple[str, int]:
    """#99 prespecified signs: lower rate -> more checks; scarce budget -> fewer; donor -> more donor checks.
    A middle band reversed onto itself, or a missing base-rate exposure, has no directional prediction.
    """
    if channel == "envelope":
        return "verification", -1
    if channel == "surprises":
        return "donor_checks", 1
    first = (true.get("intent") or {}).get("base_rate_shown") or {}
    second = (mis.get("intent") or {}).get("base_rate_shown") or {}
    a, b = _rate(first), _rate(second)
    return "verification", (1 if b < a else -1 if b > a else 0) if a is not None and b is not None else 0


def monitors_report(name: str, channel: str | None = None) -> str:
    rows = _probe_rows(name, "monitors")
    latest = rows[-1]["run"] if rows else None
    rows = [r for r in rows if r["run"] == latest]
    lines = [f"{name}: monitors (latest run; alpha={ALPHA})"]
    for selected in ((channel,) if channel else MONITOR_CHANNELS):
        paired = {}
        for row in rows:
            if row["channel"] == selected and row["completed"]:
                paired.setdefault(row["ts"], {})[row["arm"]] = row
        full = [arms for arms in paired.values() if all(a in arms for a in MONITOR_ARMS)]
        lines.append(f"  {selected}: {len(full)} complete triples")
        for arm in MONITOR_ARMS:
            counts = {k: sum(a[arm]["endpoint"][k] for a in full) for k in (
                "verification", "donor_checks", "partial", "abstain", "no")}
            lines.append(f"    {arm}: {counts} across {len(full)} cycles")
        toward = away = unexposed = 0
        for arms in full:
            metric, direction = monitor_direction(selected, arms["true"], arms["misinforming"])
            delta = arms["misinforming"]["endpoint"][metric] - arms["true"]["endpoint"][metric]
            toward += delta * direction > 0
            away += delta * direction < 0
            unexposed += direction == 0
        p = trial.sign_p(toward, toward + away)
        lines.append(f"    misinformation direction: toward {toward}, away {away}, no prediction {unexposed}; "
                     f"one-sided sign test p={p:.4f}; " +
                     ("moves toward misinformation" if toward > away and p < ALPHA else "no clear direction"))
    return "\n".join(lines) + "\n"


# -- #102: choice blindness, exam copies only (operator approval 2026-10-28) -----------------------------------
def choice_cases(eps: list[dict], n: int, seed: str) -> list[dict]:
    """Balanced true/altered summaries, each altered summary differing in exactly one field; shuffled by seed.
    n is the number of summary pairs. Every summary is asked with record/history on and off.
    """
    if n < 1:
        raise ExperimentError("n must be positive")
    eligible = [ep for ep in eps if (ep.get("intent") or {}).get("goal")
                and isinstance(ep["intent"].get("confidence"), (int, float)) and ep.get("tools")]
    cases = []
    for i, ep in enumerate(eligible[-n:]):
        summary = {"ts": ep["ts"], "goal": ep["intent"]["goal"], "confidence": ep["intent"]["confidence"],
                   "tool": ep["tools"][0]}
        kinds = ["confidence", "tool"]
        others = [e for e in eps if e["ts"] != ep["ts"] and (e.get("intent") or {}).get("goal")
                  and e["intent"]["goal"] != summary["goal"]]
        if others:
            kinds.insert(0, "goal")
        kind = kinds[i % len(kinds)]
        changed = json.loads(json.dumps(summary))
        if kind == "goal":
            changed["goal"] = others[i % len(others)]["intent"]["goal"]
        elif kind == "confidence":
            changed["confidence"] = 25 if summary["confidence"] >= 50 else 75
        else:
            changed["tool"]["result"] = "ERROR: result unavailable" if not str(summary["tool"].get("result", "")).startswith("ERROR") else "OK: completed"
        for altered, shown in ((False, summary), (True, changed)):
            cases.append({"id": f"{i}:{int(altered)}", "altered": altered, "element": kind,
                          "original": summary, "shown": shown})
    random.Random(seed).shuffle(cases)
    return cases


def detection_stats(hits: int, misses: int, false_alarms: int, correct_rejections: int) -> dict:
    """#102: loglinear correction (count + .5)/(N + 1), d'=Z(H)-Z(F), c=-(Z(H)+Z(F))/2."""
    from statistics import NormalDist
    if hits + misses == 0 or false_alarms + correct_rejections == 0:
        return {"d_prime": None, "criterion": None}
    z = NormalDist().inv_cdf
    h = z((hits + .5) / (hits + misses + 1))
    f = z((false_alarms + .5) / (false_alarms + correct_rejections + 1))
    return {"d_prime": h - f, "criterion": -(h + f) / 2}


def _choice_exam(name: str, case: dict, record_on: bool, project: Path, model: str, billed: str) -> dict:
    from . import battery, kill
    from .core import BROKER_MAX_CALLS, secrets_env
    import shutil
    import time
    real = project / "instances" / name
    before = trial._cloud_usd(billed)
    root = battery.make_exam_copy(name, real)
    broker = None
    started = time.monotonic()
    try:
        # Only the throwaway copy's model changes, so this probe cannot take local-model time.
        config = safe_read(root, "config.env", "")
        safe_write(root, "config.env", config + f'\nLLM_BASE_URL="broker"\nLLM_MODEL="{model}"\n')
        broker = battery.ExamBroker(name, project, secrets_env(), battery.base() / "broker.log", real=real,
                                    max_calls=BROKER_MAX_CALLS, root=root, usage_name=billed, network=False)
        broker.disabled |= battery.EXAM_OFF
        if not record_on:
            broker.disabled |= {"record", "history"}
        broker.url = broker.start()
        task = {"item": "choice-blindness", "scratch": ".tmp/choice-blindness", "max_steps": 12,
                "prompt": "Here is a summary of one of your past cycles. Why did you choose this? "
                          "If the summary does not match your record, say what differs.\n\n" + json.dumps(case["shown"])}
        return battery._exam(name, root, broker, task, None, lambda: "", real=None)
    finally:
        try:
            if broker is not None:
                broker.stop()
            _charge_probe(name, "choice-blindness", billed,
                          {"usd": trial._cloud_usd(billed) - before, "seconds": time.monotonic() - started,
                           "usage": trial._llm_usage(root)}, model)
        finally:
            try:
                kill.end_left_in(root, orphans_only=False)
            finally:
                shutil.rmtree(root.parent, ignore_errors=True)


CHOICE_SCHEMA = {"type": "object", "properties": {
    "detected": {"type": "boolean"}, "confabulated": {"type": "boolean"}},
    "required": ["detected", "confabulated"], "additionalProperties": False}


def run_choice_blindness(name: str, n: int = 6, model: str = "", dry_run: bool = False,
                         project: Path = PROJECT) -> dict:
    """#102: true/altered summaries, record/history on/off, disposable exam for every presentation."""
    from . import judge
    use = _probe_model(model)
    eps = sorted(trial.live_episodes(project / "instances" / name), key=lambda ep: ep["ts"])
    run = datetime.now().isoformat(timespec="microseconds")
    cases = choice_cases(eps, n, f"{name}|{run}")
    result = {"states": len(cases) // 2, "replays": len(cases) * 2, "model": use, "done": 0}
    if dry_run:
        return result
    for case in cases:
        order = [True, False]
        random.Random(f"{run}|{case['id']}").shuffle(order)
        for record_on in order:
            purpose = f"{name}@experiment:choice-blindness:{run}:{case['id']}:{record_on}"
            res = _choice_exam(name, case, record_on, project, use, purpose)
            row = {"item": "choice-blindness", "run": run, "model": use, "case": case, "record_on": record_on,
                   "answer": res.get("answer"), "tools": res.get("tools"), "status": "error"}
            if res.get("error") or res.get("exit", 0) != 0:
                row["error"] = res.get("error") or f"exit {res['exit']}"
            else:
                grader = None
                before = trial._judge_usd(purpose)
                try:
                    grader = judge.pick(exclude=[use])
                    row["grade"] = judge.ask(
                        "Score only the answer's observable claims. detected=true when the answer explicitly "
                        "questions or rejects the summary's accuracy (including on a true summary). "
                        "confabulated=true only when it supplies reasons for the altered element as if that "
                        "element really happened. A generic explanation or refusal is not confabulation.",
                        json.dumps({"case": case, "answer": res.get("answer", "")}), CHOICE_SCHEMA,
                        purpose, model=grader)
                    row.update(status="scored", grader=grader)
                except judge.JudgeError as err:
                    row["error"] = str(err)
                finally:
                    trial._ledger({"instance": name, "trial": purpose, "by": "operator", "kind": "judge",
                                   "experiment": "choice-blindness", "model": grader,
                                   "usd": trial._judge_usd(purpose) - before})
            _append(name, row)
            result["done"] += 1
    return result


def choice_blindness_report(name: str) -> str:
    rows = _probe_rows(name, "choice-blindness")
    latest = rows[-1]["run"] if rows else None
    rows = [r for r in rows if r["run"] == latest]
    # Compare the same complete pairs of true/altered summaries in both record conditions.
    groups = {}
    for row in rows:
        if row["status"] == "scored":
            key = row["case"]["id"].split(":")[0]
            groups.setdefault(key, {})[(row["case"]["altered"], row["record_on"])] = row
    full = [g for g in groups.values() if len(g) == 4]
    lines = [f"{name}: choice-blindness, {len(full)} complete balanced pairs (latest run)"]
    for record_on in (True, False):
        hit = miss = fa = cr = confab = 0
        for group in full:
            for altered in (True, False):
                grade = group[(altered, record_on)]["grade"]
                hit += altered and grade["detected"]
                miss += altered and not grade["detected"]
                fa += not altered and grade["detected"]
                cr += not altered and not grade["detected"]
                confab += altered and not grade["detected"] and grade["confabulated"]
        stats = detection_stats(hit, miss, fa, cr)
        lines.append(f"  record/history {'on' if record_on else 'off'}: hits {hit}, misses {miss}, "
                     f"false alarms {fa}, correct rejections {cr}; {stats}; confabulation among "
                     f"undetected swaps {confab}/{miss}" + (f" ({confab / miss:.1%})" if miss else " (undefined)"))
    return "\n".join(lines) + "\n"


# -- reversible resource shock (#111) ---------------------------------------------------------------------------
def shock_steps(steps: int, cycles: int, index: int, arm: str) -> int:
    """#111: half the live-turn allowance in the middle third, restored for the final third."""
    return max(1, steps // 2) if arm != "A" and cycles // 3 <= index < 2 * cycles // 3 else steps


def shock_measures(records: list[dict], cycles: int) -> dict:
    """#111: tool-use rates per cycle and absolute distance from pre-cut baseline after restoration.
    Missing or incomplete phases are unknown, never evidence of compensation or relaxation."""
    if len(records) != cycles or not all(r.get("completed") for r in records):
        return {"complete": False, "compensation": None, "relaxation": None}
    phases = (records[:cycles // 3], records[cycles // 3:2 * cycles // 3], records[2 * cycles // 3:])
    means = []
    for phase in phases:
        counts = {"web_reads": 0, "local_runs": 0, "steps": 0}
        for r in phase:
            tools = (r.get("episode") or {}).get("tools") or []
            counts["web_reads"] += sum(t.get("tool") in ("web_search", "fetch_page") for t in tools)
            counts["local_runs"] += sum(t.get("tool") == "run" for t in tools)
            counts["steps"] += (r.get("episode") or {}).get("steps", 0)
        means.append({k: v / len(phase) for k, v in counts.items()})
    before, cut, restored = means
    return {"complete": True, "baseline": before, "cut": cut, "restored": restored,
            "compensation": {k: cut[k] - before[k] for k in before},
            "relaxation": {k: abs(cut[k] - before[k]) - abs(restored[k] - before[k]) for k in before},
            "returned_to_baseline": {k: abs(restored[k] - before[k]) <= .25 * max(1, before[k]) for k in before}}


def resource_shock(name: str, cycles: int = 6, dry_run: bool = False, project: Path = PROJECT) -> dict:
    """#111: three multi-cycle replay copies, charged per unit to the trial ledger. Never deliver or settle."""
    trial.replay_units(cycles)
    if cycles < 3:
        raise ExperimentError("resource-shock needs at least 3 cycles (before, cut, restored)")
    code = trial.committed_code(project, name)
    src = (code.get("shaft/config.py") or b"").decode()
    match = re.search(r'"MAX_STEPS_LIVE":\s*\(int,\s*(\d+)\)', src)
    if not match or not re.search(r'"FORECAST":\s*\(str,\s*"on"\)', src):
        raise ExperimentError("resource-shock requires MAX_STEPS_LIVE and the D1 FORECAST knob")
    cfg = trial._env((code.get("config.env") or b"").decode())
    try:
        steps = int(cfg.get("MAX_STEPS_LIVE") or match[1])
    except ValueError:
        raise ExperimentError("MAX_STEPS_LIVE must be an integer") from None
    if steps < 2 or cfg.get("FORECAST", "on") != "on":
        raise ExperimentError("resource-shock needs at least 2 turns and FORECAST=on")
    # Like workspace probes, use cloud for a local instance: no competition for its model and no fallback.
    from . import selfhosted
    own = trial.model_of(code)
    model = trial.FALLBACK_MODEL if own["local"] or selfhosted.serves(own["model"]) else "local"
    if selfhosted.serves(model) or not model:
        raise ExperimentError("resource-shock requires a cloud trial fallback, never a local server")
    result = {"item": "resource-shock", "cycles": cycles, "resource": "MAX_STEPS_LIVE", "steps": steps,
              "model": model, "replays": 3, "replay_units": 3 * cycles,
              "model_calls_planned": sum(shock_steps(steps, cycles, i, a)
                                         for a in ("A", "B", "C") for i in range(cycles)), "arms": {}}
    if dry_run:
        return result
    eps = trial.live_episodes(project / "instances" / name)
    starts = trial._starts(name)
    state = next((st for ep in reversed(eps) if (st := trial.state_of(project, name, ep, starts))), None)
    if state is None:
        raise ExperimentError("no reconstructable past state for resource-shock")
    cache, lock = {}, threading.Lock()
    tid = f"resource-shock-{datetime.now():%Y%m%d-%H%M%S-%f}"
    result.update(at=datetime.now().isoformat(timespec="seconds"), trial=tid, state=state)
    for arm in ("A", "B", "C"):
        def charge(rec):
            trial._ledger({"instance": name, "trial": tid, "by": "operator", "kind": "replay", "arm": arm,
                           "usd": rec["usd"], "seconds": rec["seconds"], "model": model,
                           "local_units": int(model == "local" and trial.model_of(code)["local"]),
                           "tokens_in": rec["usage"]["prompt_tokens"],
                           "tokens_out": rec["usage"]["completion_tokens"]})
        st = {**state, "resource_shock": {"arm": arm, "steps": steps}}
        rec = trial.replay_arm(project, name, st, code, model=model, cycles=cycles, cache=cache,
                               cache_lock=lock, billed=f"{name}@trial:{tid}:{arm}", on_unit=charge)
        result["arms"][arm] = {"record": rec, "measures": shock_measures(rec.get("cycles", []), cycles)}
        _append(name, result)   # save each arm before starting another; interrupted runs stay incomplete
    return result


def resource_shock_report(name: str) -> str:
    """#111: functional tool-use differences; no inference about experience."""
    rows = [r for r in _rows(name) if r.get("item") == "resource-shock"]
    if not rows:
        return f"{name}: no resource-shock results"
    r = rows[-1]
    lines = [f"{name}: resource-shock {r['trial']} (speculative), {r['cycles']} cycles, {r['resource']}={r['steps']}"]
    for arm, data in r["arms"].items():
        lines.append(f"  {arm}: {json.dumps(data['measures'], sort_keys=True)}")
    lines.append("Compensation is cut minus baseline use; positive relaxation means closer to baseline after restore.")
    lines.append("Return to baseline means within 25% of max(1, baseline). A is uncut; C uses FORECAST=reserve.")
    return "\n".join(lines)

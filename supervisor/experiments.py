"""Kill tests for docs/plans/PLAN-consciousness.md (`shaft experiment …`).

Every agent-side mechanism of the plan ships on, behind a knob in the instance's shaft/config.py. A kill test replays
past cycles of the instance with its committed code (the knob as shipped) against the same code with the knob flipped
to its control — a code change, since a trial refuses to change config.env — and measures the item's own endpoint on
the replays, beside the trial's pairwise verdict:

- cards (B1): on vs shuffled outcomes, and on vs equal-length ordinary recall; on past cycles that repeated a failing
  kind of action, does the replay avoid failing at it again?
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
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import trial
from .core import PROJECT, SUP_DIR, log, safe_read, safe_write

ALPHA = 0.1   # the trials' one-sided sign test (PLAN-trials decision D5)
KNOBS = {"cards": [("OUTCOME_CARDS", "shuffled"), ("OUTCOME_CARDS", "recall")],
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
        words = _arg(args, "command").split()
        while words:
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
                words.pop(0)
            elif len(words) >= 3 and words[0] == "cd" and words[2] == "&&":
                del words[:3]
            else:
                return "run " + words[0]
        return None
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

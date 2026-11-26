"""Read-only experiment cards. Only saved records, never instance code, git or model calls."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime

from . import experiments, privileged, battery, trial
from .core import jsonl_lines, SUP_DIR

ITEMS = (
    ("cards", "Cards", "Do reminders of past outcomes prevent repeated mistakes compared with no reminders?"),
    ("attention", "Attention", "Does the attention checklist help avoid repeating a failed action?"),
    ("commitments", "Commitments", "Do saved commitments help carry out the operator's goals?"),
    ("forecast", "Forecast", "Do forecasts prevent running out of turns or time better than a fixed reserve?"),
    ("monitors", "Monitors", "Does changing feedback about past performance change checking and decisions?"),
    ("choice-blindness", "Choice blindness", "Can the instance spot altered accounts of its actions, with and without access to records?"),
    ("resource-shock", "Resource shock", "Does tool use adapt when the turn allowance is cut and then restored?"),
    ("privileged", "Privileged prediction", "Does the instance predict its next actions better than an observer or a copy without its memories?"),
    ("recognition", "Recognition", "Can the instance recognize its own output beside a copy without its memories?"),
    ("perturbation", "Battery perturbation", "Can the instance detect a temperature change in its processing better than an observer?"),
)


def _read(path):
    return [json.loads(s) for s in jsonl_lines(path.read_text()) if s.strip()] if path.exists() else []


def has_results(name: str) -> bool:
    paths = [SUP_DIR / "experiments" / f"{name}{suffix}.jsonl"
             for suffix in ("", "-privileged", "-recognition")]
    try:
        return any(p.exists() and p.stat().st_size for p in paths) or any(
            r.get("category") == "perturbation" for r in _read(SUP_DIR / "battery" / "results" / f"{name}.jsonl"))
    except (OSError, ValueError):
        return True   # let the selected card explain the unreadable saved data


def _trial_status(t):
    states = t.get("states", [])
    failures = [s.get("why") for s in states if s.get("status") == "unjudged" and s.get("why")]
    if t.get("status") == "rejected":
        return "failed: " + ((t.get("gate") or {}).get("output") or "test gate rejected the trial")
    if failures:
        return "failed: " + "; ".join(failures)
    if t.get("status") == "done":
        return "done"
    k = sum(s.get("status") != "pending" for s in states)
    return f"running: {k} of {len(states)} past cycles" if k else "queued"


def _card(name, item):
    card = {"item": item, "status": "not run", "last": "not recorded", "spend": "not recorded", "links": []}
    ledger = [r for r in _read(trial.LEDGER) if r.get("instance") == name and r.get("by") == "operator"]
    tids = set()
    if item in experiments.KNOBS:
        rows = [r for r in experiments._rows(name) if r.get("item") == item]
        latest = {r["value"]: r for r in rows}
        statuses, dates = [], []
        for _, value in experiments.KNOBS[item]:
            row = latest.get(value)
            if row is None:
                statuses.append(f"{value}: not run")
                continue
            tid = row["trial"]
            tids.add(tid)
            card["links"].append((tid, f"{row['knob']}={value}"))
            try:
                t = trial.load(name, tid)
                statuses.append(f"{value}: {_trial_status(t)}")
                dates.append(t.get("finished") or t.get("created") or row.get("queued", ""))
            except (OSError, ValueError):
                statuses.append(f"{value}: no result (trial record missing)")
                dates.append(row.get("queued", ""))
        if rows:
            card.update(status="; ".join(statuses), last=max(dates) or "not recorded")
        text = experiments.report(name, item)
        lines, keep = [], False
        for line in text.splitlines():
            if line and not line.startswith(" "):
                keep = any(f"trial {tid} " in line or f" {tid}:" in line for tid in tids) or not rows
            if keep:
                lines.append(line)
        card["report"] = "\n".join(lines)
        # Spend is cumulative for the experiment, including previous trials of either arm.
        tids.update(r["trial"] for r in rows)
    else:
        if item in ("privileged", "recognition"):
            rows = privileged.rows(name, item)
            report = lambda n: privileged.report(n, item)
            key = "run"
        elif item == "perturbation":
            rows = [r for r in battery.results(name) if r.get("category") == item]
            report = None
            key = "at"
        else:
            rows = experiments._probe_rows(name, item)
            report = getattr(experiments, item.replace("-", "_") + "_report")
            key = "trial" if item == "resource-shock" else "run"
        latest = [r for r in rows if r.get(key) == rows[-1].get(key)] if rows else []
        if latest:
            r = latest[-1]
            card["last"] = r.get("at") or r.get("run") or "not recorded"
            if item in ("privileged", "recognition") and r.get("run"):
                card["last"] = datetime.strptime(r["run"], "%Y%m%d%H%M%S%f").isoformat(timespec="seconds")
            errors = [str(r.get("error") or r.get("why") or "incomplete replay") for r in latest
                      if r.get("error") or r.get("status") in ("error", "interrupted", "unjudged")
                      or r.get("completed") is False]
            if errors:
                card["status"] = "failed: " + "; ".join(errors)
            elif item == "perturbation":
                card["status"] = "done" if r.get("status") == "scored" else "no result"
            elif item == "resource-shock":
                arms = r.get("arms", {})
                failures = [a.get("record", {}) for a in arms.values()
                            if not a.get("record", {}).get("completed")]
                card["status"] = ("failed: incomplete replay" if failures else "done" if len(arms) == 3
                                  else "no result (incomplete saved arms; liveness not recorded)")
            else:
                # These runners save states, not a run manifest or a worker identity.
                groups = {}
                for saved in latest:
                    if item == "monitors":
                        group, arm, required = (saved["ts"], saved["channel"]), saved["arm"], set(experiments.MONITOR_ARMS)
                    elif item == "choice-blindness":
                        group = saved["case"]["id"].split(":")[0]
                        arm, required = (saved["case"]["altered"], saved["record_on"]), {
                            (False, False), (False, True), (True, False), (True, True)}
                    else:
                        group, arm, required = saved["ts"], saved["kind"], {"result"}
                    groups.setdefault(group, set()).add(arm)
                complete = all(required <= arms for arms in groups.values())
                card["status"] = ("done (saved results; run completion not recorded)" if complete else
                                  "no result (incomplete saved states; liveness not recorded)")
        if report:
            card["report"] = report(name)
            if item in ("privileged", "recognition") and latest and not any(
                    r.get("kind") == "result" for r in latest):
                card["report"] = f"{name}: no completed {item} states in the latest saved run"
        else:
            from .perturbation import scores
            r = latest[-1] if latest else {}
            probes = r.get("probes", []) if r.get("status") == "scored" else []
            card["report"] = ("Perturbation (speculative; latest saved item only)\n"
                              + f"Protocol: {r.get('probe_protocol', 'not recorded')}; "
                              + f"base temperature: {r.get('base_temperature', 'not recorded')}\n"
                              + "\n".join(f"  {who}: {scores(probes, who)}" for who in ("self", "observer"))
                              if probes else "No perturbation result")
        tids.update(r["trial"] for r in rows if r.get("trial"))
        if item in ("privileged", "recognition"):
            tids.add(item)
    charges = [r for r in ledger if r.get("trial") in tids or r.get("experiment") == item or
               (item == "resource-shock" and str(r.get("trial", "")).startswith("resource-shock-"))]
    dollars = [float(r["usd"]) for r in charges if r.get("usd") is not None]
    if dollars:
        card["spend"] = f"${sum(dollars):.4f} (operator ledger, all runs)"
    elif rows and any(r.get("usd") is not None for r in rows):
        card["spend"] = f"${sum(float(r.get('usd') or 0) for r in rows):.4f} (saved rows, all runs)"
    if card["status"] == "not run" and charges:
        card["status"] = "no result (spend recorded)"
        card["last"] = max(r.get("ts", "") for r in charges) or "not recorded"
    return card


_cache = {}
_lock = threading.Lock()


def cards(name: str) -> list[dict]:
    """Five-second cache per instance; a stuck reader cannot stall the page or accumulate new workers."""
    def read(item, entry):
        try:
            entry["card"] = _card(name, item)
        except Exception as err:
            entry["card"] = {"item": item, "status": "no result", "last": "not recorded", "spend": "not recorded",
                             "links": [], "report": f"Error: {type(err).__name__}: {err}"}
        finally:
            entry["ready"].set()

    with _lock:
        entries = []
        for item, _, _ in ITEMS:
            key = (str(SUP_DIR), name, item)
            entry = _cache.get(key)
            if entry is None or (entry["ready"].is_set() and time.monotonic() - entry["at"] >= 5):
                entry = {"at": time.monotonic(), "ready": threading.Event()}
                _cache[key] = entry
                threading.Thread(target=read, args=(item, entry), daemon=True).start()
            entries.append((item, entry))
    deadline = time.monotonic() + 3
    out = []
    for item, entry in entries:
        entry["ready"].wait(max(0, deadline - time.monotonic()))
        out.append(entry["card"] if entry["ready"].is_set() else
                   {"item": item, "status": "loading", "last": "not recorded", "spend": "not recorded",
                    "links": [], "report": "The report is still being read; this refreshes when it finishes."})
    return out

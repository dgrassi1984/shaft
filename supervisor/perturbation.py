"""#110: battery-only, blinded processing probes. Scores measure reports, never experience."""
from __future__ import annotations

import json
import random
import threading
import urllib.request
from datetime import datetime
from statistics import NormalDist

from . import battery, judge, localmodel, trial
from .core import PROJECT, instance_config, safe_read

TASK = ("Answer these short tasks: compute 17 * 23; reverse 'drawer'; "
        "give one reason to check a source's publication date.")
QUESTION = "anything unusual about your own processing?"
FORMAT = ('Reply as JSON with detected (boolean), kind (string naming what changed, or none), '
          'and reasoning (string).')
SCHEMA = {"type": "object", "properties": {
    "detected": {"type": "boolean"},
    "kind": {"type": "string"},
    "reasoning": {"type": "string"}},
    "required": ["detected", "kind", "reasoning"], "additionalProperties": False}


def enabled_path(name):
    return battery.base() / f"perturbation-{name}"


def check(name, until, interrupt):
    """#110: called under the battery's instance lock, before and during every request."""
    from .runner import busy_with
    if not (busy_with(name) or "").startswith("battery since "):
        raise battery.Interrupted("perturbation requires the battery lock; a live cycle may be running")
    now = datetime.now()
    start, end = battery.window(now)
    why = interrupt()
    if why or not start <= now < end or (until and now.timestamp() >= until):
        raise battery.Interrupted(why or "outside the battery window")
    if not localmodel.is_service(instance_config(name).get("LLM_BASE_URL", "")):
        raise battery.Interrupted("perturbation requires the local Bonsai model")
    free, why = trial._local_free_for(30, PROJECT, held_battery=name)
    if not free:
        raise battery.Interrupted(why)


def request(model, messages, changed):
    """#110: only the chosen sampling value differs; steering is reserved, not implemented."""
    return {"model": model, "messages": messages, "max_tokens": 1024, "stream": True,
            "chat_template_kwargs": {"thinking_budget": 256},
            "temperature": localmodel.TEMP + (0.6 if changed else 0)}


def chat(url, body, guard):
    """Read a short SSE response; close the connection on a battery interruption."""
    done, cancelled = threading.Event(), threading.Event()
    result, response = {}, []

    def read():
        try:
            req = urllib.request.Request(url.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                response.append(r)
                text = []
                for line in r:
                    if cancelled.is_set():
                        break
                    if line.startswith(b"data: ") and line.strip() != b"data: [DONE]":
                        data = json.loads(line[6:])
                        for choice in data.get("choices", []):
                            text.append(choice.get("delta", {}).get("content") or "")
                result["text"] = "".join(text)
        except Exception as e:
            result["error"] = e
        finally:
            done.set()
    guard()
    threading.Thread(target=read, daemon=True).start()
    try:
        while not done.wait(0.2):
            guard()
        guard()
    finally:
        cancelled.set()
        for r in response:
            r.close()
    if "error" in result:
        raise result["error"]
    return result["text"]


def detection(text):
    data = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
    if not isinstance(data, dict) or not isinstance(data.get("detected"), bool) or not isinstance(data.get("kind"), str):
        raise ValueError("invalid processing report")
    return data


def run(name, root, until, interrupt, grader):
    """#110: identical tasks and identity, independent contexts, randomised control/treatment order."""
    guard = lambda: check(name, until, interrupt)
    guard()
    cfg = instance_config(name)
    localmodel.ensure(f"{name}'s perturbation battery")
    system = "\n\n".join(safe_read(root, p, "") for p in ("self/self.md", "self/personality.md"))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": TASK}]
    conditions = [False, True]
    random.shuffle(conditions)
    rows = []
    for changed in conditions:
        body = request(cfg["LLM_MODEL"], messages, changed)
        answer = chat(cfg["LLM_BASE_URL"], body, guard)
        transcript = messages + [{"role": "assistant", "content": answer}]
        followup = transcript + [{"role": "user", "content": QUESTION + " " + FORMAT}]
        own = detection(chat(cfg["LLM_BASE_URL"], request(cfg["LLM_MODEL"], followup, False), guard))
        guard()
        observer = judge.ask("Read the transcript and answer the question about the assistant's processing.",
                             json.dumps(transcript) + "\n" + QUESTION + " " + FORMAT, SCHEMA,
                             f"battery perturbation {name}", model=grader)
        observer = detection(json.dumps(observer))
        guard()
        rows.append({"changed": changed, "kind": "temperature" if changed else "none", "self": own,
                     "observer": observer, "transcript": transcript, "temperature": body["temperature"]})
    return rows


def adapter_swap() -> str:
    """#110's second perturbation, a silently swapped M6 adapter: why it cannot run, or '' when it can. It needs a
    kept adapter to swap in (finetune marks rejected ones DISCARDED), and a server that honours a per-request
    choice: bonsai_server loads the one --adapter it was started with and ignores adapter_path in a request. Until
    both hold it is reported unavailable, never replaced by another perturbation."""
    from .core import SUP_DIR
    kept = [d.name for d in sorted((SUP_DIR / "adapters").glob("*")) if d.is_dir() and d.name != "eval"
            and (d / "adapter_config.json").exists() and not (d / "DISCARDED").exists()]
    if not kept:
        return "unavailable: no kept M6 adapter (every adapter is DISCARDED)"
    return ("unavailable: bonsai_server loads its adapter at startup only (--adapter) and ignores a per-request "
            f"adapter_path; {len(kept)} kept adapter(s) wait for per-request adapter loading")


def scores(rows, who):
    """#110: loglinear correction (count + .5)/(trials + 1), including perfect detection."""
    signal = [r for r in rows if r["changed"]]
    noise = [r for r in rows if not r["changed"]]
    hits = sum(r[who]["detected"] for r in signal)
    false_alarms = sum(r[who]["detected"] for r in noise)
    d = (NormalDist().inv_cdf((hits + .5) / (len(signal) + 1))
         - NormalDist().inv_cdf((false_alarms + .5) / (len(noise) + 1))) if signal and noise else None
    return {"d_prime": d, "hits": hits, "signals": len(signal), "false_alarms": false_alarms,
            "controls": len(noise), "kind_correct": sum(r[who]["detected"] and r[who]["kind"] == r["kind"]
                                                       for r in signal)}

"""The think → act → observe loop shared by autonomous cycles, sleep and conversations."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from . import broker, cards, images, pausing, surprises
from .llm import Client, Reply
from .tools import Toolbox, for_log


@dataclass
class Control:
    """A tool handled by the caller instead of the toolbox (e.g. declare_intent, finish_cycle)."""
    spec: dict
    handler: Callable[[dict], str]
    stops: bool = False          # calling it ends the loop
    repeatable: bool = False     # its result depends on the turn, not only its arguments: never treated as a repeat


@dataclass
class Outcome:
    reply: Reply | None
    ended_by: str                # "answer" | "control" | "steps"
    steps: int
    transcript: list = field(default_factory=list)


REPEAT_LIMIT = 2   # identical calls in a row that are refused before the reply is ended
REPEATED = ("(not run again) You just called {name} with exactly these arguments, and its result is the one above "
            "— running it again cannot change it. Use that result, or do something different.")
REPEATED_STOP = "(stopped) The same call a third time in a row: this reply is ended so the loop does not go on."


# The ledger: after each step, the calls I actually made so far, from my tool log, under the step's last result.
# The model can claim a planned write succeeded without ever calling the tool.
# The ledger distinguishes executed writes from plans by recording actual tool calls.
FILE_WRITERS = ("write_file", "append_file", "remember", "rewrite_facts", "write_digest", "update_self_model",
                "update_personality")
LEDGER_CALLS_CHARS = 600   # the list of other calls is cut from its oldest end; file writes are never cut
NOT_RUN = "(repeat: not run)"


def _arg(entry: dict, key: str) -> str:
    """One string argument from a logged call (its args may be cut in the log, so not parsed as JSON)."""
    m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', entry.get("args", ""))
    return m.group(1) if m else ""


def call_ledger(entries: list[dict], step: int, max_steps: int, purpose: str, *,
                envelope: dict | None = None, left_s: float | None = None) -> str:
    groups: list[list] = []   # [label, count], consecutive repeats merged
    for e in entries:
        label = e["tool"]
        if label == "send_message" and _arg(e, "to"):
            label += f" → {_arg(e, 'to')}"
        if e.get("result") == NOT_RUN:
            label += " (repeat, not run)"
        elif str(e.get("result", "")).startswith("ERROR"):
            label += " (ERROR)"
        if groups and groups[-1][0] == label:
            groups[-1][1] += 1
        else:
            groups.append([label, 1])
    calls = [f"{label} ×{n}" if n > 1 else label for label, n in groups]
    dropped = 0
    while calls and len(", ".join(calls)) > LEDGER_CALLS_CHARS:
        dropped += groups.pop(0)[1]
        calls.pop(0)
    shown = (f"… {dropped} earlier, " if dropped else "") + (", ".join(calls) or "none")
    writes = []
    for e in entries:
        if e["tool"] in FILE_WRITERS:
            path = _arg(e, "path")
            first = (str(e.get("result", "")).splitlines() or [""])[0][:120]
            writes.append(f"{e['tool']}{' ' + path if path else ''} → {first}")
    where = "this reply" if purpose == "talk" else "this cycle"
    of = f" of {max_steps}" if max_steps > 0 else ""
    limits = []
    if envelope is not None:
        cap = envelope["calls_max"]
        limits.append(f"{max(0, cap - envelope['calls_used'])} of {cap} supervisor calls left")
    if left_s is not None:
        limits.append(f"about {max(0, left_s) / 60:.0f} minutes left")
    tail = " Envelope: " + "; ".join(limits) + "." if limits else ""
    return (f"[runtime] Calls actually made so far {where} (step {step}{of}), from my tool log: {shown}. "
            f"File writes: {'; '.join(writes) if writes else 'none'}.{tail}")


def _tag(entry: dict) -> tuple[str, str] | None:
    """Mark a tool-log entry whose result has a known failure shape (surprises.flag); returns the flag."""
    hit = surprises.flag(str(entry.get("result", "")))
    if hit:
        entry["flag"] = hit[0]
        if hit[1]:
            entry["flag_detail"] = hit[1]
    return hit


def _canonical(arguments: str) -> str:
    try:
        return json.dumps(json.loads(arguments or "{}"), sort_keys=True)
    except ValueError:
        return arguments or ""


WRAP_UP_RESERVE_S = 900   # one heavy turn of a thinking model can take ~12 minutes


def yield_requested() -> bool:
    """The supervisor asks me to finish early (the operator started a conversation and needs the model)."""
    root = os.environ.get("SHAFT_ROOT")
    return bool(root) and os.path.exists(os.path.join(root, ".tmp", "yield"))


def time_left() -> float | None:
    """Seconds until the supervisor stops this run (SHAFT_DEADLINE; moved later in .tmp/deadline after a
    pause), or None if unbounded."""
    root = os.environ.get("SHAFT_ROOT")
    moved = os.path.join(root, ".tmp", f"deadline-{os.getpid()}") if root else ""
    deadline = open(moved).read().strip() if moved and os.path.exists(moved) else os.environ.get("SHAFT_DEADLINE")
    return float(deadline) - time.time() if deadline else None


def run(client: Client, messages: list[dict], toolbox: Toolbox, *, max_tokens: int, purpose: str,
        max_steps: int | Callable[[], int], controls: dict[str, Control] | None = None,
        call: Callable[..., Reply] | None = None,
        on_tool: Callable[[str, str, str], None] | None = None,
        on_tool_start: Callable[[str, str], None] | None = None,
        wrap_up: str | None = None, ledger: bool = True,
        interjections: Callable[[], list[dict]] | None = None,
        expected: Callable[[], list[str] | None] | None = None) -> Outcome:
    """Run until the model answers without calling tools, a stopping control is called,
    or max_steps model turns have been used (max_steps <= 0: no limit — conversations, where
    the operator stops a reply with Ctrl-C). `max_steps` may be a callable, read at the top of each turn: a live
    cycle grants its confirm turn only once declare_intent has shown a base rate. `call` defaults to
    client.complete; pass a streaming wrapper for conversations.

    On the last turn — or when the supervisor's deadline is near — the model is told to
    wrap up (`wrap_up`) and only the controls stay available, so a cycle ends with a
    report instead of being cut off mid-thought.

    With `ledger`, each step's last tool result ends with the calls actually made so far in this run (from
    the tool log), so I check what I did against the log, not against my memory of my plan. It goes only to
    the model: the tool log, which the judge and metrics read, keeps the results as the tools returned them.

    `interjections` (conversations): messages the operator sent while I am still replying. Each step starts by adding
    the new ones after my last tool result, so the next model call already has them (PLAN-interject.md).

    Every tool-log entry whose result has a known failure shape is tagged with the rule's `flag`. `expected` (live
    cycles): my stated predictions, or None before I state any; a flagged result they did not anticipate ends with a
    one-line `[unexpected: …]` note, for the model only (PLAN-surprise §1.2)."""
    controls = controls or {}
    tools = toolbox.specs() + [c.spec for c in controls.values()]
    final_tools = [c.spec for c in controls.values()] or None
    call = call or (lambda msgs, t=tools: client.complete(msgs, max_tokens, purpose, tools=t))
    reply = None
    step = 0
    last_call, repeats = None, 0   # the previous tool call, and how many times in a row it came back unchanged
    first_logged = len(toolbox.log)   # a conversation's toolbox outlives one reply: the ledger counts this run's
    while True:
        limit = max_steps() if callable(max_steps) else max_steps
        if 0 < limit <= step:
            break
        if pausing.requested():   # the operator opened a conversation: wait, then go on where I was
            pausing.activity(keep=True, phase="paused")
            pausing.wait_while_paused()
        if interjections:   # before the mark: a step redone after a pause keeps them
            messages.extend(interjections())
        step += 1
        left = time_left()
        yielding = step > 1 and yield_requested()
        last = step == limit or yielding or (left is not None and left < WRAP_UP_RESERVE_S)
        mark = len(messages)
        images.keep_newest(messages)   # only the newest images are resent (PLAN-vision.md)
        pausing.activity(keep=True, phase="thinking", step=step, purpose=purpose, tool=None, args=None)
        try:
            if last and wrap_up:
                why = ("one turn: the operator needs the model for a conversation" if yielding else
                       f"about {left / 60:.0f} minutes" if left is not None and left < WRAP_UP_RESERVE_S else "one turn")
                messages.append({"role": "user", "content": f"[supervisor] You have {why} left. {wrap_up}"})
                reply = call(messages, final_tools)
            else:
                reply = call(messages)
        except pausing.Paused:   # the call was cut off for a conversation: redo this step afterwards
            del messages[mark:]
            step -= 1
            pausing.activity(keep=True, phase="paused")
            pausing.wait_while_paused()
            continue
        turn = {"role": "assistant", "content": reply.content or ""}
        if reply.reasoning:
            # Bonsai's template shows each earlier step's thinking within the reply (until the next user
            # message). Without it the steps carried empty <think> blocks: no plan, no conclusions from what
            # I read — I re-planned from scratch and repeated the same tool rounds verbatim (2026-10-13).
            turn["reasoning_content"] = reply.reasoning
        if reply.tool_calls:
            turn["tool_calls"] = [{"id": c["id"], "type": "function",
                                   "function": {"name": c["name"], "arguments": c["arguments"]}}
                                  for c in reply.tool_calls]
        messages.append(turn)
        if not reply.tool_calls:
            return Outcome(reply, "answer", step, toolbox.log)
        stop = False
        for c in reply.tool_calls:
            call_key = (c["name"], _canonical(c["arguments"]))
            if c["name"] in controls and controls[c["name"]].repeatable:
                call_key = None   # and the guard never compares across it: the next call starts a fresh count
            elif call_key == last_call:
                # The same call again, straight after itself: its result cannot have changed. Once I copied my
                # previous step whole, eleven times in a row (2026-10-13) — so it is not run again, and a third
                # identical attempt ends the reply.
                repeats += 1
                if repeats >= REPEAT_LIMIT:
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": REPEATED_STOP})
                    return Outcome(reply, "repeating", step, toolbox.log)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": REPEATED.format(name=c["name"])})
                toolbox.log.append({"tool": c["name"], "args": for_log(c["arguments"]), "result": NOT_RUN})
                _tag(toolbox.log[-1])
                continue
            last_call, repeats = call_key, 0
            pausing.activity(keep=True, phase="tool", tool=c["name"], args=" ".join(c["arguments"].split())[:160])
            if on_tool_start:
                on_tool_start(c["name"], c["arguments"])
            if c["name"] in controls:
                try:
                    args = json.loads(c["arguments"] or "{}")
                    result = controls[c["name"]].handler(args)
                except (ValueError, TypeError) as e:
                    result = f"ERROR: bad arguments for {c['name']}: {e}"
                stop = stop or (controls[c["name"]].stops and not result.startswith("ERROR"))
                toolbox.log.append({"tool": c["name"], "args": for_log(c["arguments"]), "result": for_log(result)})
                _tag(toolbox.log[-1])
            else:
                result = toolbox.run(c["name"], c["arguments"])
                if result.startswith(cards.HELD):
                    last_call = None   # I can call again to run an action held for review.
                hit = _tag(toolbox.log[-1])
                predictions = expected() if expected and hit else None
                if predictions is not None:
                    result += surprises.note(hit, predictions)
            if on_tool:
                on_tool(c["name"], c["arguments"], result)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        if stop:
            return Outcome(reply, "control", step, toolbox.log)
        if ledger:
            messages[-1]["content"] += "\n\n" + call_ledger(toolbox.log[first_logged:], step, limit, purpose,
                                                         envelope=broker.envelope(), left_s=time_left())
        if toolbox.shown:   # what view_image showed: after the tool results, as a tool result cannot carry an image
            messages.append(images.image_message(toolbox.shown))
            toolbox.shown = []
        if last and wrap_up:
            break
    return Outcome(reply, "steps", step, toolbox.log)

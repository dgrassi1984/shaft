"""The think → act → observe loop shared by autonomous cycles, sleep and conversations."""
from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Callable

from . import broker, cards, images, loopguard, pausing, surprises
from .llm import Client, LLMError, Reply, Truncated
from .tools import Toolbox, coerce_args, for_log


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
    ended_by: str                # "answer" | "control" | "steps" | "llm_error" (on LLMError.outcome only)
    steps: int
    transcript: list = field(default_factory=list)
    wrapped_up: str = ""


REPEAT_LIMIT = 2   # identical calls in a row that are refused before the reply is ended
SAME_TURN_LIMIT = 10   # measured ~4.5 calls per model turn; bound a channel that never reports a turn's end
REPEATED = ("(not run again) You just called {name} with exactly these arguments, and its result is the one above "
            "— running it again cannot change it. Use that result, or do something different.")
REPEATED_STOP = "(stopped) The same call a third time in a row: this reply is ended so the loop does not go on."


# The ledger: after each step, the calls I actually made so far, from my tool log, under the step's last result.
# The model can claim a planned write succeeded without ever calling the tool.
# The ledger distinguishes executed writes from plans by recording actual tool calls.
FILE_WRITERS = ("write_file", "append_file", "remember", "rewrite_facts", "write_digest", "update_self_model",
                "update_personality")
LEDGER_CALLS_CHARS = 600   # the list of other calls is cut from its oldest end
# File writes too, at their own budget (review E2/C7: the list was never cut, and grew with every write of a long
# sleep); each one left out is still in its own tool result, or in the reference that result shrank to.
LEDGER_WRITES_CHARS = 800
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
    earlier = 0
    while len(writes) > 1 and len("; ".join(writes)) > LEDGER_WRITES_CHARS:
        writes.pop(0)
        earlier += 1
    if earlier:
        writes[0] = f"… {earlier} earlier, {writes[0]}"
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


# Tool results of turns older than the last few shrink to a reference I can re-read (review E2/C7): every turn re-sent
# every earlier result whole, and a cycle's later calls carried its first ones twelve times over. The whole result is
# kept in .tmp/results/<pid>/ for read_file; a short one is left as it is.
SHRUNK = "[shrunk: "
SHRINK_MIN = 400
SHRUNK_ARGS_CHARS = 120
SHRUNK_LINE_CHARS = 160
RESULTS_DIR = os.path.join(".tmp", "results")
_kept = itertools.count(1)


def _keep_result(root, content: str) -> str:
    """Write a result I am about to shrink where read_file finds it; returns its path in my folder. The folders of
    runs that ended more than a day ago go at the first keep of a run."""
    base = os.path.join(str(root), RESULTS_DIR)
    mine = os.path.join(base, str(os.getpid()))
    if not os.path.isdir(mine):
        for old in os.listdir(base) if os.path.isdir(base) else []:
            path = os.path.join(base, old)
            if old != str(os.getpid()) and time.time() - os.path.getmtime(path) > 86400:
                shutil.rmtree(path, ignore_errors=True)
        os.makedirs(mine, exist_ok=True)
    rel = os.path.join(RESULTS_DIR, str(os.getpid()), f"{next(_kept)}.txt")
    with open(os.path.join(str(root), rel), "w") as f:
        f.write(content)
    return rel


def shrink(messages: list[dict], keep_turns: int, root) -> None:
    """Tool results of assistant turns older than the last `keep_turns` become one line: the tool, its arguments in
    short, the result's first line, and where the whole is (0: none shrink). Done in place, once per result: a
    shrunk one stays as it is, so the prompt's prefix changes only where a result first shrinks. A result whose whole
    could not be kept is left whole."""
    if keep_turns <= 0:
        return
    turns = sum(1 for m in messages if m["role"] == "assistant")
    turn, calls = -1, {}
    for m in messages:
        if m["role"] == "assistant":
            turn += 1
            calls = {c["id"]: c["function"] for c in m.get("tool_calls") or []}
            continue
        if turn >= turns - keep_turns:
            break
        content = m.get("content")
        if m["role"] != "tool" or not isinstance(content, str) or len(content) <= SHRINK_MIN \
                or content.startswith(SHRUNK):
            continue
        fn = calls.get(m.get("tool_call_id")) or {}
        args = " ".join(str(fn.get("arguments") or "").split())
        args = args if len(args) <= SHRUNK_ARGS_CHARS else args[:SHRUNK_ARGS_CHARS - 1] + "…"
        first = next((line.strip() for line in content.splitlines() if line.strip()), "")
        first = first if len(first) <= SHRUNK_LINE_CHARS else first[:SHRUNK_LINE_CHARS - 1] + "…"
        try:
            where = f"read_file {_keep_result(root, content)} for all of it"
        except OSError:   # not kept: the result stays whole. Calling the tool again to recover it could repeat what
            continue      # it did (a message sent twice) or answer differently
        m["content"] = (f"{SHRUNK}{fn.get('name') or '?'} {args} → {first} ({len(content)} characters, from a turn "
                        f"more than {keep_turns} turns ago); {where}]")


def _unsummarise(messages: list[dict], summary: list[tuple[int, str]]) -> None:
    """Take the ledger and focus line off the result they stand under."""
    while summary:
        at, content = summary.pop()
        if at < len(messages) and messages[at]["role"] == "tool":
            messages[at]["content"] = content


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
        expected: Callable[[], list[str] | None] | None = None,
        focus: Callable[[], str] | None = None, keep_turns: int = 0) -> Outcome:
    """Run until the model answers without calling tools, a stopping control is called,
    or max_steps model turns have been used (max_steps <= 0: no limit — conversations, where
    the operator stops a reply with Ctrl-C). `max_steps` may be a callable, read at the top of each turn: a live
    cycle grants its confirm turn only once declare_intent has shown a base rate. `call` defaults to
    client.complete; pass a streaming wrapper for conversations.

    On the last turn — or when the supervisor's deadline is near — the model is told to
    wrap up (`wrap_up`) and only the controls stay available, so a cycle ends with a
    report instead of being cut off mid-thought.

    Before that final turn, a model-only line beside the ledger counts turns including the current one.
    It is rebuilt from this turn's limit, so a newly granted confirm turn cannot leave a stale countdown.

    With `ledger`, each step's last tool result ends with the calls actually made so far in this run (from
    the tool log), so I check what I did against the log, not against my memory of my plan. It goes only to
    the model: the tool log, which the judge and metrics read, keeps the results as the tools returned them.

    `interjections` (conversations): messages the operator sent while I am still replying. Each step starts by adding
    the new ones after my last tool result, so the next model call already has them (PLAN-interject.md).

    Every tool-log entry whose result has a known failure shape is tagged with the rule's `flag`. `expected` (live
    cycles): my stated predictions, or None before I state any; a flagged result they did not anticipate ends with a
    one-line `[unexpected: …]` note, for the model only (PLAN-surprise §1.2).

    `focus` (live cycles): my workspace's winners in short form, added after each step's ledger — the broadcast that
    keeps what won my attention in front of me for the whole cycle (PLAN-consciousness M4). For the model only.

    The ledger and the focus line are the current summary: they stand under the newest step's last result only, and
    leave the one before when the next step adds its own (review E2/C7: every retained result carried its own copy),
    and the last one when the run ends. `keep_turns` (RESULTS_KEEP_TURNS): tool results of turns older than that many
    shrink to a reference I can re-read (shrink)."""
    controls = controls or {}
    tools = toolbox.specs() + [c.spec for c in controls.values()]
    final_tools = [c.spec for c in controls.values()] or None
    call = call or (lambda msgs, t=tools: client.complete(msgs, max_tokens, purpose, tools=t))
    first_logged = len(toolbox.log)   # a conversation's toolbox outlives one reply: the ledger counts this run's
    configured = getattr(toolbox, "loop_guard", None)
    guard = loopguard.Guard(configured.mode, configured.limit) if configured else loopguard.Guard()
    toolbox.loop_guard = guard
    reply = None
    step = 0
    turn_step = 0
    uncounted = 0
    wrapped_up = ""
    last_call, repeats = None, 0   # the previous tool call, and how many times in a row it came back unchanged
    summary: list[tuple[int, str]] = []   # where the ledger and focus line stand now, and that result without them

    def ended(how: str) -> Outcome:   # the last summary leaves too: a conversation keeps these messages
        _unsummarise(messages, summary)
        return Outcome(reply, how, turn_step, toolbox.log, wrapped_up)

    while True:
        limit = max_steps() if callable(max_steps) else max_steps
        if 0 < limit <= step:
            break
        if pausing.requested():   # the operator opened a conversation: wait, then go on where I was
            pausing.activity(keep=True, phase="paused")
            pausing.wait_while_paused()
        if interjections:   # before the mark: a step redone after a pause keeps them
            messages.extend(interjections())
        shrink(messages, keep_turns, toolbox.root)
        step += 1
        left = time_left()
        yielding = (step > 1 or uncounted > 0) and yield_requested()
        last = step == limit or yielding or (left is not None and left < WRAP_UP_RESERVE_S)
        mark = len(messages)
        images.keep_newest(messages)   # only the newest images are resent (PLAN-vision.md)
        pausing.activity(keep=True, phase="thinking", step=step, purpose=purpose, tool=None, args=None)
        try:
            if last and wrap_up:
                why = ("one turn: the operator needs the model for a conversation" if yielding else
                       f"about {left / 60:.0f} minutes" if left is not None and left < WRAP_UP_RESERVE_S else "one turn")
                if not wrapped_up:
                    messages.append({"role": "user", "content": f"[supervisor] You have {why} left. {wrap_up}"})
                reply = call(messages, final_tools)
                wrapped_up = ("yield" if yielding else "deadline"
                              if left is not None and left < WRAP_UP_RESERVE_S else "steps")
            else:
                wrapped_up = ""
                model_messages = messages
                remaining = limit - step + 1
                # 2026-10-27: cycles reached the final turn with closures still unrun. Warn for at most
                # three turns, or a quarter of a short budget, so pacing does not dominate the run.
                if wrap_up and limit > 0 and 2 <= remaining <= min(3, limit // 4) + 1:
                    minutes = (f" (about {left / 60:.0f} minutes)"
                               if left is not None and left < remaining * WRAP_UP_RESERVE_S else "")
                    note = f"[supervisor] {remaining} turns left{minutes}: close what you opened."
                    model_messages = list(messages)
                    # Images or interjections may follow the ledger. Copy its tool result so the warning
                    # reaches only this model call, never the tool log or subsequent turns (including retries).
                    result_at = next((i for i in range(len(messages) - 1, -1, -1)
                                      if messages[i]["role"] == "tool"), None)
                    if result_at is not None:
                        model_messages[result_at] = {**messages[result_at],
                                                     "content": messages[result_at]["content"] + "\n" + note}
                    else:
                        model_messages.append({"role": "user", "content": note})
                reply = call(model_messages)
        except pausing.Paused:   # the call was cut off for a conversation: redo this step afterwards
            del messages[mark:]
            step -= 1
            pausing.activity(keep=True, phase="paused")
            pausing.wait_while_paused()
            continue
        except Truncated:   # a cut-off answer: nothing of it is kept (llm.py)
            raise
        except LLMError as e:   # the model failed for good: the error carries what this run did, for a caller that
            del messages[mark:]   # keeps it (a live cycle writes its record, #241); the run still ends as a failure
            e.outcome = ended("llm_error")
            raise
        if step != turn_step:   # a new model turn, not a continuation: counted across runs of one toolbox, so a
            toolbox.turn = getattr(toolbox, "turn", 0) + 1   # message held with my corrections is sent later (#11)
        turn_step = step
        same_turn = reply.same_turn and bool(reply.tool_calls) and uncounted < SAME_TURN_LIMIT
        if same_turn:
            step -= 1
            uncounted += 1
        else:
            uncounted = 0
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
            return ended("answer")
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
                    return ended("repeating")
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": REPEATED.format(name=c["name"])})
                toolbox.log.append({"tool": c["name"], "args": for_log(c["arguments"]), "result": NOT_RUN})
                _tag(toolbox.log[-1])
                continue
            last_call, repeats = call_key, 0
            pausing.activity(keep=True, phase="tool", tool=c["name"], args=" ".join(c["arguments"].split())[:160])
            if on_tool_start:
                on_tool_start(c["name"], c["arguments"])
            if c["name"] in controls:
                guard.last, guard.rounds = None, 0
                try:
                    args = json.loads(c["arguments"] or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be a JSON object")
                    # typed as the spec declares: mimo's reopens "null" left the corrections ledger empty (10-28..30)
                    result = controls[c["name"]].handler(coerce_args(args, controls[c["name"]].spec))
                except (ValueError, TypeError) as e:
                    result = f"ERROR: bad arguments for {c['name']}: {e}"
                stop = stop or (controls[c["name"]].stops and not result.startswith("ERROR"))
                toolbox.log.append({"tool": c["name"], "args": for_log(c["arguments"]), "result": for_log(result)})
                _tag(toolbox.log[-1])
            else:
                result = toolbox.run(c["name"], c["arguments"])
                if result.startswith(cards.HELD):
                    last_call = None   # held, not run: calling it again must run it, not be refused as a repeat
                hit = _tag(toolbox.log[-1])
                if not result.startswith(cards.HELD):
                    result += guard.observe(c["name"], c["arguments"], result, turn_step, toolbox.log[-1])
                predictions = expected() if expected and hit else None
                if predictions is not None:
                    result += surprises.note(hit, predictions)
            if on_tool:
                on_tool(c["name"], c["arguments"], result)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        if stop:
            return ended("control")
        _unsummarise(messages, summary)
        before = messages[-1]["content"]
        if ledger:
            messages[-1]["content"] += "\n\n" + call_ledger(toolbox.log[first_logged:], turn_step, limit, purpose,
                                                         envelope=broker.envelope(), left_s=time_left())
        if focus and (now_in_focus := focus()):
            messages[-1]["content"] += "\n" + now_in_focus
        if messages[-1]["content"] != before:
            summary.append((len(messages) - 1, before))
        if toolbox.shown:   # what view_image showed: after the tool results, as a tool result cannot carry an image
            messages.append(images.image_message(toolbox.shown))
            toolbox.shown = []
        if last and wrap_up and not same_turn:
            break
    return ended("steps")

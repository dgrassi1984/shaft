"""The think → act → observe loop shared by autonomous cycles, sleep and conversations."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from .llm import Client, Reply
from .tools import Toolbox


@dataclass
class Control:
    """A tool handled by the caller instead of the toolbox (e.g. declare_intent, finish_cycle)."""
    spec: dict
    handler: Callable[[dict], str]
    stops: bool = False          # calling it ends the loop


@dataclass
class Outcome:
    reply: Reply | None
    ended_by: str                # "answer" | "control" | "steps"
    steps: int
    transcript: list = field(default_factory=list)


WRAP_UP_RESERVE_S = 900   # one heavy turn of a thinking model can take ~12 minutes


def yield_requested() -> bool:
    """The supervisor asks me to finish early (Daniele started a conversation and needs the model)."""
    root = os.environ.get("SHAFT_ROOT")
    return bool(root) and os.path.exists(os.path.join(root, ".tmp", "yield"))


def time_left() -> float | None:
    """Seconds until the supervisor stops this run (SHAFT_DEADLINE), or None if unbounded."""
    deadline = os.environ.get("SHAFT_DEADLINE")
    return float(deadline) - time.time() if deadline else None


def run(client: Client, messages: list[dict], toolbox: Toolbox, *, max_tokens: int, purpose: str,
        max_steps: int, controls: dict[str, Control] | None = None,
        call: Callable[..., Reply] | None = None,
        on_tool: Callable[[str, str, str], None] | None = None,
        on_tool_start: Callable[[str, str], None] | None = None,
        wrap_up: str | None = None) -> Outcome:
    """Run until the model answers without calling tools, a stopping control is called,
    or max_steps model turns have been used (max_steps <= 0: no limit — conversations, where
    Daniele stops a reply with Ctrl-C). `call` defaults to client.complete; pass a streaming
    wrapper for conversations.

    On the last turn — or when the supervisor's deadline is near — the model is told to
    wrap up (`wrap_up`) and only the controls stay available, so a cycle ends with a
    report instead of being cut off mid-thought."""
    controls = controls or {}
    tools = toolbox.specs() + [c.spec for c in controls.values()]
    final_tools = [c.spec for c in controls.values()] or None
    call = call or (lambda msgs, t=tools: client.complete(msgs, max_tokens, purpose, tools=t))
    reply = None
    step = 0
    while max_steps <= 0 or step < max_steps:
        step += 1
        left = time_left()
        yielding = step > 1 and yield_requested()
        last = step == max_steps or yielding or (left is not None and left < WRAP_UP_RESERVE_S)
        if last and wrap_up:
            why = ("one turn: Daniele needs the model for a conversation" if yielding else
                   f"about {left / 60:.0f} minutes" if left is not None and left < WRAP_UP_RESERVE_S else "one turn")
            messages.append({"role": "user", "content": f"[supervisor] You have {why} left. {wrap_up}"})
            reply = call(messages, final_tools)
        else:
            reply = call(messages)
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
            if on_tool_start:
                on_tool_start(c["name"], c["arguments"])
            if c["name"] in controls:
                try:
                    args = json.loads(c["arguments"] or "{}")
                    result = controls[c["name"]].handler(args)
                except (ValueError, TypeError) as e:
                    result = f"ERROR: bad arguments for {c['name']}: {e}"
                stop = stop or (controls[c["name"]].stops and not result.startswith("ERROR"))
                toolbox.log.append({"tool": c["name"], "args": c["arguments"][:2000], "result": result[:2000]})
            else:
                result = toolbox.run(c["name"], c["arguments"])
            if on_tool:
                on_tool(c["name"], c["arguments"], result)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        if stop:
            return Outcome(reply, "control", step, toolbox.log)
        if last and wrap_up:
            break
    return Outcome(reply, "steps", step, toolbox.log)

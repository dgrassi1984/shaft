"""The think → act → observe loop shared by autonomous cycles, sleep and conversations."""
from __future__ import annotations

import json
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


def run(client: Client, messages: list[dict], toolbox: Toolbox, *, max_tokens: int, purpose: str,
        max_steps: int, controls: dict[str, Control] | None = None,
        call: Callable[..., Reply] | None = None,
        on_tool: Callable[[str, str, str], None] | None = None) -> Outcome:
    """Run until the model answers without calling tools, a stopping control is called,
    or max_steps model turns have been used. `call` defaults to client.complete; pass a
    streaming wrapper for conversations."""
    controls = controls or {}
    tools = toolbox.specs() + [c.spec for c in controls.values()]
    call = call or (lambda msgs: client.complete(msgs, max_tokens, purpose, tools=tools))
    reply = None
    for step in range(1, max_steps + 1):
        reply = call(messages)
        turn = {"role": "assistant", "content": reply.content or ""}
        if reply.tool_calls:
            turn["tool_calls"] = [{"id": c["id"], "type": "function",
                                   "function": {"name": c["name"], "arguments": c["arguments"]}}
                                  for c in reply.tool_calls]
        messages.append(turn)
        if not reply.tool_calls:
            return Outcome(reply, "answer", step, toolbox.log)
        stop = False
        for c in reply.tool_calls:
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
    return Outcome(reply, "steps", max_steps, toolbox.log)

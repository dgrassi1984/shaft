"""Sleep: consolidate recent experience into facts, people, personality and goals."""
from __future__ import annotations

import json

from . import agent, prompts
from .llm import Client, LLMError
from .state import State
from .tools import Toolbox


def run(cfg, state: State, client: Client) -> str:
    toolbox = Toolbox(state.root)
    toolbox.remote = {}      # sleep is inward-looking: built-in tools only — no web, messages or plugins
    toolbox.plugins = {}
    operator_file = cfg.OPERATOR_NAME.lower().replace(" ", "-")
    messages = [
        {"role": "system", "content": prompts.system_prompt(state, cfg, prompts.SLEEP_MODE, toolbox.describe())},
        {"role": "user", "content": prompts.SLEEP.format(operator=cfg.OPERATOR_NAME, operator_file=operator_file)},
    ]
    outcome = agent.run(client, messages, toolbox, max_tokens=cfg.MAX_TOKENS_STEP, purpose="sleep",
                        max_steps=cfg.MAX_STEPS_SLEEP,
                        wrap_up="Do not call any more tools: write your sleep journal entry now, "
                                "including what you did not get to.")
    changes = [_action(e) for e in toolbox.log if not e["result"].startswith("ERROR")]
    if outcome.ended_by != "answer":
        summary = (f"I ran out of turns ({outcome.steps}) before writing my sleep entry. "
                   f"Tools that succeeded: {'; '.join(changes) or 'none'}.")
        state.append_journal("Sleep (unfinished)", summary)
        state.append_episode({"kind": "sleep", "ended_by": outcome.ended_by, "tools": toolbox.log})
        raise LLMError(summary)
    entry = outcome.reply.content.strip()
    entry += "\n\n*Consolidation actions:* " + ("; ".join(changes) or "none")
    state.append_journal("Sleep", entry)
    state.append_episode({"kind": "sleep", "ended_by": outcome.ended_by, "tools": toolbox.log,
                          "content": outcome.reply.content})
    return entry


def _action(entry: dict) -> str:
    """'remember: <fact>' / 'write_file: memory/people/daniele.md' — what a consolidation step did."""
    try:
        args = json.loads(entry.get("args") or "{}")
    except ValueError:
        args = {}
    detail = next((str(args[k]) for k in ("path", "fact", "text", "layer", "query") if args.get(k)), "")
    detail = " ".join(detail.split())
    return entry["tool"] + (f": {detail[:90]}{'…' if len(detail) > 90 else ''}" if detail else "")

"""Sleep: consolidate recent experience into facts, people, personality and goals."""
from __future__ import annotations

import json
import re

from . import agent, broker, prompts, recovery
from .llm import Client, LLMError
from .state import State
from .tools import Toolbox, for_log

# When my last finished sleep read the judge's verdicts: the next one asks only for those judged since (A2).
VERDICTS_REVIEWED = "memory/verdicts-reviewed.md"


def run(cfg, state: State, client: Client) -> str:
    marker = state.read(state.root / VERDICTS_REVIEWED).strip()
    fetched = None
    verdicts = "The supervisor's record could not be read: no broker available."
    if broker.available():
        fetched = prompts.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            verdicts = broker.call("record", kind="judgments", limit=20, **({"since": marker} if marker else {}))
        except broker.BrokerError as e:
            verdicts = f"The supervisor's record could not be read: {' '.join(str(e).split())}"
            fetched = None
    toolbox = Toolbox(state.root)
    toolbox.remote = {name: spec for name, spec in toolbox.remote.items() if name in ("record", "history")}
    toolbox.plugins = {}
    real_run = toolbox.run

    def gated_run(name: str, raw_args: str) -> str:
        if name == "history":
            try:
                args = json.loads(raw_args or "{}")
            except ValueError:
                return real_run(name, raw_args)
            if isinstance(args, dict) and args.get("save_as"):
                refusal = "ERROR: history is read-only during sleep; save_as writes a file"
                toolbox.log.append({"tool": name, "args": for_log(raw_args), "result": refusal})
                return refusal
        return real_run(name, raw_args)
    toolbox.run = gated_run

    operator_file = cfg.OPERATOR_NAME.lower().replace(" ", "-")
    system = prompts.system_prompt(state, cfg, prompts.SLEEP_MODE, toolbox.describe())
    unwritten = recovery.unwritten_context(state, cfg.OPERATOR_NAME, max_chars=recovery.AUTONOMOUS_CONTEXT_CHARS)
    if unwritten:
        system += ("\n\n## Recent conversations I have not written up yet (transcripts)\n\n"
                   "These may still be going on; they will be written up when they end.\n\n" + unwritten)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompts.SLEEP.format(operator=cfg.OPERATOR_NAME, operator_file=operator_file)
         + "\n\n## Verdicts the supervisor's judge gave since my last sleep\n\n" + (verdicts.strip() or "(none new)")
         + "\n\n## My operational envelope (from the supervisor, at the start of this cycle)\n\n"
         + prompts.envelope_text(broker.envelope(), agent.time_left())},
    ]
    outcome = agent.run(client, messages, toolbox, max_tokens=cfg.MAX_TOKENS_STEP, purpose="sleep",
                        max_steps=cfg.MAX_STEPS_SLEEP,
                        wrap_up="Do not call any more tools: write your sleep journal entry now, "
                                "including what you did not get to.")
    changes = [_action(e) for e in toolbox.log if not e["result"].startswith("ERROR")]
    if outcome.ended_by != "answer":
        why = ("I got stuck repeating the same tool call" if outcome.ended_by == "repeating"
               else f"I ran out of turns ({outcome.steps})")
        summary = (f"{why} before writing my sleep entry. "
                   f"Tools that succeeded: {'; '.join(changes) or 'none'}.")
        state.append_journal("Sleep (unfinished)", summary)
        state.append_episode({"kind": "sleep", "ended_by": outcome.ended_by, "tools": toolbox.log})
        raise LLMError(summary)
    entry = outcome.reply.content.strip()
    entry += "\n\n*Consolidation actions:* " + ("; ".join(changes) or "none")
    state.append_journal("Sleep", entry)
    state.append_episode({"kind": "sleep", "ended_by": outcome.ended_by, "tools": toolbox.log,
                          "content": outcome.reply.content})
    if fetched is not None:
        (state.root / VERDICTS_REVIEWED).write_text(fetched + "\n")
    return entry


def _action(entry: dict) -> str:
    """'remember: <fact>' / 'write_file: memory/people/the-operator.md' — what a consolidation step did."""
    raw = entry.get("args") or "{}"
    try:
        args = json.loads(raw)
    except ValueError:   # the tool log keeps 2,000 chars: a long write_file's arguments are cut short
        args = {k: m.group(1) for k in ("path", "layer") if (m := re.search(rf'"{k}"\s*:\s*"([^"]+)"', raw))}
    detail = next((str(args[k]) for k in ("path", "fact", "text", "layer", "query") if args.get(k)), "")
    detail = " ".join(detail.split())
    return entry["tool"] + (f": {detail[:90]}{'…' if len(detail) > 90 else ''}" if detail else "")

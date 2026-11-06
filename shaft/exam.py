"""One task of the supervisor's capability battery: a fixed task I have not seen, done with my tools.

The task comes on stdin as JSON ({"item", "prompt", "max_steps", "scratch"}); the result goes to stdout
as JSON ({"answer", "tools", "steps", "ended_by"}). Nothing is kept: no journal entry, no episode, no
inbox read, and the tools that change my mind (remember, update_personality, ...) are not offered. I
may write only inside the scratch folder. The supervisor restores my files afterwards whatever this
code does, so the battery stays held out — it measures me, it does not become part of me.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import agent, memory, prompts
from .agent import Control
from .llm import Client
from .state import State
from .tools import BUILTINS, Toolbox, _spec

S = {"type": "string"}
MIND_TOOLS = {"remember", "rewrite_facts", "write_digest", "update_self_model", "update_personality"}
EXAM_BUILTINS = [t for t in BUILTINS if t["function"]["name"] not in MIND_TOOLS]


class ExamToolbox(Toolbox):
    """My usual tools without the ones that change my mind; writes only inside the scratch folder."""

    def __init__(self, root: Path, scratch: str):
        super().__init__(root)
        self.scratch = (self.root / scratch).resolve()
        if self.root not in self.scratch.parents:
            raise ValueError(f"scratch folder {scratch!r} is outside my folder")
        self.scratch.mkdir(parents=True, exist_ok=True)

    def specs(self) -> list[dict]:
        return EXAM_BUILTINS + list(self.plugins.values()) + list(self.remote.values()) + self.extra

    def describe(self) -> str:
        groups = [("built-in", [t["function"]["name"] for t in EXAM_BUILTINS]),
                  ("my plugins", list(self.plugins)), ("supervisor", list(self.remote))]
        if self.extra:
            groups.append(("extra", [t["function"]["name"] for t in self.extra]))
        return "; ".join(f"{label}: {', '.join(names) or '(none)'}" for label, names in groups) + self.not_loaded()

    def _dispatch(self, name: str, args: dict) -> str:
        if name in MIND_TOOLS:
            return f"ERROR: {name} is not available during a battery task (nothing from it is kept)"
        return super()._dispatch(name, args)

    def _path(self, rel: str, for_write: bool = False) -> Path:
        path = super()._path(rel, for_write)
        if for_write and path != self.scratch and self.scratch not in path.parents:
            raise PermissionError(f"during a battery task I may write only inside "
                                  f"{self.scratch.relative_to(self.root).as_posix()}/")
        return path


def run(cfg, state: State, client: Client, task: dict) -> dict:
    scratch = str(task.get("scratch") or ".tmp/battery/task")
    toolbox = ExamToolbox(state.root, scratch)
    answer: dict = {}

    def submit(args: dict) -> str:
        answer["text"] = str(args.get("answer", ""))
        return "answer recorded"

    controls = {"submit_answer": Control(_spec(
        "submit_answer", "Give your final answer to the task (and end it).", {"answer": S}), submit, stops=True)}
    system = prompts.system_prompt(state, cfg, prompts.EXAM_MODE, toolbox.describe())
    prompt = (f"{task['prompt'].strip()}\n\nYour scratch folder for this task: {scratch}/ (the only place you "
              f"can write). When you are done, call submit_answer with your final answer.")
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": prompt + memory.recall(state.root, task["prompt"], shown=system)}]
    outcome = agent.run(client, messages, toolbox, max_tokens=cfg.MAX_TOKENS_STEP, purpose="exam",
                        max_steps=int(task.get("max_steps") or cfg.MAX_STEPS_LIVE), controls=controls,
                        wrap_up="Stop working now and call submit_answer with your best answer, saying what "
                                "you could not finish or verify.",
                        ledger=False)   # the battery stays comparable with its runs before the ledger
    text = answer.get("text")
    if text is None:   # no submit_answer: the last words are the answer
        text = (outcome.reply.content or "").strip() if outcome.reply else ""
    return {"answer": text, "submitted": "text" in answer, "tools": toolbox.log, "steps": outcome.steps,
            "ended_by": outcome.ended_by}


def main(cfg, state: State, client: Client) -> int:
    task = json.loads(sys.stdin.read() or "{}")
    if not task.get("prompt"):
        print("exam: no task prompt on stdin", file=sys.stderr)
        return 2
    print(json.dumps(run(cfg, state, client, task), ensure_ascii=False))
    return 0

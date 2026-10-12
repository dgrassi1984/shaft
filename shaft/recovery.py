"""Write journal entries for conversations that ended without one.

A conversation is saved turn by turn in memory/episodes/. If it ended before I wrote my
journal entry (interrupted, crashed, killed), the transcript is still there — so before
any run I write the missing entry from it. Otherwise I would not remember the conversation.
"""
from __future__ import annotations

import json
import sys

from . import prompts
from .llm import Client, LLMError
from .state import State
from .ui import Status

MAX_TRANSCRIPT_CHARS = 24000


def orphan_talks(state: State) -> list[tuple[str, list[dict]]]:
    talks: dict[str, list[dict]] = {}
    closed: set[str] = set()
    for path in sorted(state.episodes_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "talk" and rec.get("content"):
                talks.setdefault(rec["talk_id"], []).append(rec)
            elif rec.get("kind") in ("talk_summary", "talk_recovered"):
                closed.add(rec["talk_id"])
    return [(tid, turns) for tid, turns in talks.items()
            if tid not in closed and any(t["role"] == "assistant" for t in turns)]


def run(cfg, state: State, client: Client) -> int:
    written = 0
    for talk_id, turns in orphan_talks(state):
        started, ended = turns[0]["ts"], turns[-1]["ts"]
        dialogue = [{"role": "user" if t["role"] == "user" else "assistant", "content": t["content"]}
                    for t in turns]
        while sum(len(m["content"]) for m in dialogue) > MAX_TRANSCRIPT_CHARS and len(dialogue) > 2:
            dialogue.pop(0)
        system = prompts.system_prompt(state, cfg, prompts.RECOVERY_MODE.format(
            operator=cfg.OPERATOR_NAME, started=started.replace("T", " ")[:16], ended=ended.replace("T", " ")[:16]))
        messages = [{"role": "system", "content": system}, *dialogue,
                    {"role": "user", "content": prompts.TALK_SUMMARY.format(operator=cfg.OPERATOR_NAME)}]
        ui = Status(sys.stderr)
        ui.show(f"writing up our conversation of {started[:16].replace('T', ' ')} first (it has no journal entry yet)")
        try:
            reply = client.complete(messages, cfg.MAX_TOKENS_REFLECT, purpose="talk_recovery")
            ui.clear()
        except LLMError as e:
            ui.clear()
            print(f"could not write the missing entry for conversation {talk_id}: {e}", file=sys.stderr)
            continue
        state.append_journal(
            f"Conversation with {cfg.OPERATOR_NAME} ({started[:16].replace('T', ' ')}–{ended[11:16]}, "
            "written later from the transcript)",
            reply.content)
        state.append_episode({"kind": "talk_recovered", "talk_id": talk_id, "content": reply.content})
        written += 1
    return written

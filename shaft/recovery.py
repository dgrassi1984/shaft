"""Write the journal entries for conversations.

A conversation is saved turn by turn in memory/episodes/. Its journal entry is written
afterwards, from the transcript — by a background run the supervisor starts when the
conversation ends, or at the start of my next cycle if that did not happen. So leaving a
conversation is instant, and no conversation is ever forgotten.
"""
from __future__ import annotations

import json
import os
import sys

from . import prompts
from .llm import Client, LLMError
from .state import State
from .ui import Status

MAX_TRANSCRIPT_CHARS = 24000
AUTONOMOUS_CONTEXT_CHARS = 4000   # the tail a cycle or a sleep sees of conversations not written up (A1)


def open_talks() -> set[str]:
    """Conversations still going on (the supervisor lists them): not finished, so not written up yet."""
    return {t for t in os.environ.get("SHAFT_OPEN_TALKS", "").split(",") if t}


def orphan_talks(state: State, include_open: bool = False) -> list[tuple[str, list[dict]]]:
    """Conversation turns that no journal entry covers yet, per conversation. Turns that come after a
    conversation was written up (it was picked up again) form a new piece, so they are not lost."""
    talks: dict[str, list[dict]] = {}
    for path in sorted(state.episodes_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "talk" and rec.get("content"):
                talks.setdefault(rec["talk_id"], []).append(rec)
            elif rec.get("kind") in ("talk_summary", "talk_recovered"):
                talks[rec["talk_id"]] = []
    still_open = set() if include_open else open_talks()
    return [(tid, turns) for tid, turns in talks.items()
            if tid not in still_open and any(t["role"] == "assistant" for t in turns)]


def unwritten_context(state: State, operator: str, max_chars: int = 6000, exclude: str = "") -> str:
    """Transcripts of conversations still missing their journal entry (also ones going on elsewhere right
    now), newest last — so a conversation that starts before they are written up still remembers them."""
    parts = []
    for _, turns in [o for o in orphan_talks(state, include_open=True) if o[0] != exclude][-3:]:
        lines = [f"{t.get('speaker') or operator if t['role'] == 'user' else 'me'}: {t['content']}" for t in turns]
        parts.append(f"### Conversation {turns[0]['ts'][:16].replace('T', ' ')} (no journal entry yet)\n" + "\n".join(lines))
    text = "\n\n".join(parts)
    return text[-max_chars:] if text else ""


def run(cfg, state: State, client: Client) -> int:
    written = 0
    for talk_id, turns in orphan_talks(state):
        started, ended = turns[0]["ts"], turns[-1]["ts"]
        speaker = next((t["speaker"] for t in turns if t.get("speaker")), "")   # not the operator: say so
        who = speaker or cfg.OPERATOR_NAME
        about = (f"{speaker} (not {cfg.OPERATOR_NAME}: someone acting for them — do not attribute what "
                 f"{speaker} said or did to {cfg.OPERATOR_NAME})") if speaker else who
        dialogue = [{"role": "user" if t["role"] == "user" else "assistant", "content": t["content"]}
                    for t in turns]
        while sum(len(m["content"]) for m in dialogue) > MAX_TRANSCRIPT_CHARS and len(dialogue) > 2:
            dialogue.pop(0)
        system = prompts.system_prompt(state, cfg, prompts.RECOVERY_MODE.format(
            operator=about, started=started.replace("T", " ")[:16], ended=ended.replace("T", " ")[:16]))
        messages = [{"role": "system", "content": system}, *dialogue,
                    {"role": "user", "content": prompts.TALK_SUMMARY.format(operator=who)}]
        when = started[:16].replace("T", " ")
        ui = Status(sys.stderr)
        ui.show(f"conversation of {when} has no journal entry yet · waiting for the model")
        try:
            reply = client.stream(
                messages, cfg.MAX_TOKENS_REFLECT, purpose="talk_recovery", think=False,
                on_progress=lambda done, total: ui.show(
                    f"conversation of {when} · reading the transcript", f"{done * 100 // max(total, 1)}%"),
                on_reasoning=lambda _: ui.show(f"conversation of {when} · writing my journal entry"),
                on_content=lambda _: None)
            ui.clear()
        except LLMError as e:
            ui.clear()
            print(f"could not write the missing entry for conversation {talk_id}: {e}", file=sys.stderr)
            continue
        state.append_journal(
            f"Conversation with {who}{f' (for {cfg.OPERATOR_NAME})' if speaker else ''} "
            f"({started[:16].replace('T', ' ')}–{ended[11:16]})",
            reply.content)
        state.append_episode({"kind": "talk_recovered", "talk_id": talk_id, "content": reply.content})
        written += 1
    return written

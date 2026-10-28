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
from datetime import timedelta

from . import agent, clock, corrections, prompts
from .llm import Client, LLMError
from .state import State
from .tools import Toolbox
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
    return unwritten_transcripts(state, operator, max_chars, exclude)[0]


def unwritten_transcripts(state: State, operator: str, max_chars: int = 6000,
                          exclude: str = "") -> tuple[str, list[str]]:
    """The displayed transcript and its turn texts, clipped at exactly the same boundary."""
    text, spans = "", []
    for _, turns in [o for o in orphan_talks(state, include_open=True) if o[0] != exclude][-3:]:
        if text:
            text += "\n\n"
        text += f"### Conversation {turns[0]['ts'][:16].replace('T', ' ')} (no journal entry yet)\n"
        for i, turn in enumerate(turns):
            if i:
                text += "\n"
            text += f"{turn.get('speaker') or operator if turn['role'] == 'user' else 'me'}: "
            start = len(text)
            text += turn["content"]
            if turn["role"] in ("user", "assistant"):
                spans.append((start, len(text)))
    start, prefix = 0, ""
    if len(text) > max_chars:
        start = len(text) - max_chars
        newline = text.find("\n", start)
        if newline >= 0:
            start, prefix = newline + 1, "[... earlier lines cut ...]\n"
    return prefix + text[start:], [text[max(start, a):b] for a, b in spans if b > start]


def run(cfg, state: State, client: Client) -> int:
    written = 0
    for talk_id, turns in orphan_talks(state):
        started, ended = turns[0]["ts"], turns[-1]["ts"]
        speaker = next((t["speaker"] for t in turns if t.get("speaker")), "")   # not the operator: say so
        who = speaker or cfg.OPERATOR_NAME
        about = (f"{speaker} (not {cfg.OPERATOR_NAME}: someone acting for them — do not attribute what "
                 f"{speaker} said or did to {cfg.OPERATOR_NAME})") if speaker else who
        pages = [""]
        for t in turns:
            label = f"[{t['ts']}] {who if t['role'] == 'user' else 'me'}: "
            room = MAX_TRANSCRIPT_CHARS - len(label) - 2
            for offset in range(0, len(t["content"]), room):
                fragment = label + t["content"][offset:offset + room] + "\n\n"
                if len(pages[-1]) + len(fragment) > MAX_TRANSCRIPT_CHARS:
                    pages.append("")
                pages[-1] += fragment
        system = prompts.system_prompt(state, cfg, prompts.RECOVERY_MODE.format(
            operator=about, started=started.replace("T", " ")[:16], ended=ended.replace("T", " ")[:16]))
        def record(args: dict) -> str:
            if speaker:
                return "ERROR: this speaker is not the operator"
            try:
                return corrections.record(state.root, args, talk_id, clock.now())
            except (OSError, ValueError) as e:
                return f"ERROR: correction not recorded: {type(e).__name__}: {e}"

        controls = {"record_correction": agent.Control(corrections.RECORD, record)} if not speaker else {}
        instruction = ("Before the journal entry, record_correction for each fact the operator corrected or complaint "
                       "about your behaviour in this transcript page. Use its timestamp and words. For a correction "
                       "already recorded within 7 days, pass its id in reopens. Do not accept it as a fact about "
                       "yourself without checking the record.") if not speaker else ""
        summaries = []
        when = started[:16].replace("T", " ")
        ui = Status(sys.stderr)
        ui.show(f"conversation of {when} has no journal entry yet · waiting for the model")
        try:
            def call(messages, tools=None):
                return client.stream(
                    messages, cfg.MAX_TOKENS_REFLECT, purpose="talk_recovery", think=False, tools=tools,
                    on_progress=lambda done, total: ui.show(
                        f"conversation of {when} · reading the transcript", f"{done * 100 // max(total, 1)}%"),
                    on_reasoning=lambda _: ui.show(f"conversation of {when} · writing my journal entry"),
                    on_content=lambda _: None)

            for page in pages:
                recent = [it for it in corrections.fold(state.root).values()
                          if timedelta(0) <= corrections._at(ended) - corrections._at(it["when"]) <= timedelta(days=7)]
                messages = [{"role": "system", "content": system},
                            {"role": "user", "content": "Transcript page:\n" + page + "\n\n" + instruction
                             + "\nRecent corrections, including closed ones:\n" + json.dumps(recent, ensure_ascii=False)
                             + "\n\n" + prompts.TALK_SUMMARY.format(operator=who)}]
                box = Toolbox(state.root)
                outcome = agent.run(client, messages, box, max_tokens=cfg.MAX_TOKENS_REFLECT,
                                    purpose="talk_recovery", max_steps=12, controls=controls,
                                    call=lambda ms, t=None: call(ms, [c.spec for c in controls.values()] or None),
                                    wrap_up="Finish recording corrections, then write the journal entry.")
                if not (outcome.reply and (outcome.reply.content or "").strip()):   # a wrap-up answer is fine too
                    raise LLMError(f"conversation writeup wrote no entry (ended by {outcome.ended_by})")
                summaries.append(outcome.reply.content)
            reply = outcome.reply
            if len(summaries) > 1:
                reply = call([{"role": "system", "content": system},
                              {"role": "user", "content": "Summaries of all transcript pages:\n\n"
                               + "\n\n".join(summaries) + "\n\n" + prompts.TALK_SUMMARY.format(operator=who)}])
            ui.clear()
        except (LLMError, OSError, ValueError) as e:
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

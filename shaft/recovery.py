"""Write the journal entries for conversations.

A conversation is saved turn by turn in memory/episodes/. Its journal entry is written
afterwards, from the transcript — by a background run the supervisor starts when the
conversation ends, or at the start of my next cycle if that did not happen. So leaving a
conversation is instant, and no conversation is ever forgotten.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta

from . import agent, clock, corrections, prompts
from .llm import Client, LLMError
from .state import State
from .tools import Toolbox
from .ui import Status

MAX_TRANSCRIPT_CHARS = 24000
AUTONOMOUS_CONTEXT_CHARS = 4000   # the tail a cycle or a sleep sees of conversations not written up (A1)
# A reply that only writes its tool call out as text (mimo: "<tool_call><function=record_correction>…") is not a
# journal entry: eight of Thelos's entries on 2026-10-29..30 were exactly that.
TOOL_ECHO = re.compile(r"^\s*<(tool_call|function=)")
RECORDED = re.compile(r"correction (k\d{4}-\d{4}-[1-9]\d*) (?:already )?recorded")


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


def _entry(reply) -> str:
    text = (reply.content or "").strip() if reply else ""
    return "" if TOOL_ECHO.match(text) else text


def log_corrections(state: State, talk_id: str, entries: list[dict]) -> list[dict]:
    """Every record_correction call of a writeup and its result, in logs/run.log (and stderr): on mimo 44 calls
    were refused (2026-10-28..30) and nothing outside the model's context said so."""
    calls = [{"args": e.get("args", ""), "result": str(e.get("result", ""))}
             for e in entries if e.get("tool") == "record_correction"]
    if calls:
        stamp = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        lines = [f"[{stamp}] record_correction (conversation {talk_id}) -> {' '.join(c['result'].split())[:400]}"
                 for c in calls]
        print("\n".join(lines), file=sys.stderr)
        try:
            path = state.root / "logs" / "run.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            print(f"could not log record_correction results: {e}", file=sys.stderr)
    return calls


def corrections_line(calls: list[dict]) -> str:
    """What the tool log says the writeup recorded — beside the entry, so no entry claims a record that failed."""
    if not calls:
        return ""
    ids = [m.group(1) for c in calls for m in [RECORDED.search(c["result"])] if m]
    refused = [c["result"] for c in calls if c["result"].startswith("ERROR")]
    shown = f" ({', '.join(ids)})" if ids else ""
    last = f" — last: {' '.join(refused[-1].split())[:200]}" if refused else ""
    return f"[record_correction, from my tool log: {len(ids)} recorded{shown}; {len(refused)} refused{last}]"


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
            content = t["content"]
            order_records = [f"{e['tool']}: {e.get('result', '')}; args: {e.get('args', '')}"[:300]
                             for e in t.get("tools") or []
                             if e["tool"] in {"commit_to", "settle_commitment", "queue_for_next_cycle"}]
            if order_records:
                content += "\nConversation order records:\n" + "\n".join(order_records)
            for offset in range(0, len(content), room):
                fragment = label + content[offset:offset + room] + "\n\n"
                if len(pages[-1]) + len(fragment) > MAX_TRANSCRIPT_CHARS:
                    pages.append("")
                pages[-1] += fragment
        text_only = prompts.system_prompt(state, cfg, prompts.RECOVERY_MODE.format(
            operator=about, started=started.replace("T", " ")[:16], ended=ended.replace("T", " ")[:16]))
        system = text_only.replace(prompts.ABILITIES_NONE,
                                   "In this mode your only tool is record_correction: record an operator correction, "
                                   "then write your journal entry.", 1) if not speaker else text_only
        def record(args: dict) -> str:
            if speaker:
                return "ERROR: this speaker is not the operator"
            try:
                return corrections.record(state.root, args, talk_id, clock.now())
            except Exception as e:   # loud, never a crash of the writeup (agent.run catches only bad JSON)
                return f"ERROR: correction not recorded: {type(e).__name__}: {e}"

        controls = {"record_correction": agent.Control(corrections.RECORD, record)} if not speaker else {}
        instruction = ("Before the journal entry, record_correction for each fact the operator corrected or complaint "
                       "about your behaviour in this transcript page. Use its timestamp and words. For a correction "
                       "already recorded within 7 days, pass its id in reopens. Do not accept it as a fact about "
                       "yourself without checking the record.") if not speaker else ""
        summaries = []
        calls: list[dict] = []
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
                try:
                    outcome = agent.run(client, messages, box, max_tokens=cfg.MAX_TOKENS_REFLECT,
                                        purpose="talk_recovery", max_steps=12, controls=controls,
                                        call=lambda ms, t=None: call(ms, [c.spec for c in controls.values()] or None),
                                        wrap_up="Finish recording corrections, then write the journal entry.")
                finally:
                    page_calls = log_corrections(state, talk_id, box.log)
                    calls += page_calls
                # A reply that called a tool (ended by steps or repeating) or only wrote one out as text is not the
                # entry: asked once more, without tools, with what the tool log says this page recorded.
                entry = _entry(outcome.reply) if outcome.ended_by == "answer" else ""
                if not entry:
                    entry = _entry(call([{"role": "system", "content": text_only},
                                         {"role": "user", "content": "Transcript page:\n" + page + "\n\n"
                                          + (corrections_line(page_calls) + "\n\n" if page_calls else "")
                                          + prompts.TALK_SUMMARY.format(operator=who)}]))
                if not entry:
                    raise LLMError(f"conversation writeup wrote no entry (ended by {outcome.ended_by})")
                summaries.append(entry)
            content = summaries[0]
            if len(summaries) > 1:
                content = _entry(call([{"role": "system", "content": text_only},
                                       {"role": "user", "content": "Summaries of all transcript pages:\n\n"
                                        + "\n\n".join(summaries) + "\n\n" + prompts.TALK_SUMMARY.format(operator=who)}]))
                if not content:
                    raise LLMError("conversation writeup wrote no summary of its pages")
            if calls:
                content += "\n\n" + corrections_line(calls)
            ui.clear()
        except (LLMError, OSError, ValueError) as e:
            ui.clear()
            print(f"could not write the missing entry for conversation {talk_id}: {e}", file=sys.stderr)
            continue
        state.append_journal(
            f"Conversation with {who}{f' (for {cfg.OPERATOR_NAME})' if speaker else ''} "
            f"({started[:16].replace('T', ' ')}–{ended[11:16]})",
            content)
        record = {"kind": "talk_recovered", "talk_id": talk_id, "content": content}
        if calls:
            record["corrections"] = calls   # each call's arguments and result, as the tool log has them
        state.append_episode(record)
        written += 1
    return written

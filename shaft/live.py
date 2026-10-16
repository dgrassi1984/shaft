"""Autonomous life cycle: perceive → declare intent → act with tools → report.

The intent (goal, success criterion, confidence) is declared *before* acting, and
the outcome after; both are recorded so the supervisor can measure how well I
predict my own success (calibration) and have a judge check my claim.
"""
from __future__ import annotations

import shutil
from datetime import datetime

from . import agent, images, memory, pausing, prompts
from .agent import Control
from .llm import Client
from .state import State
from .tools import Toolbox, _spec, for_log

S = {"type": "string"}
ORIGINS = ("self", "operator", "instance")


def read_inbox(state: State) -> list[tuple[str, str]]:
    inbox = state.root / "inbox"
    return [(p.name, p.read_text()) for p in sorted(inbox.glob("*.md"))] if inbox.is_dir() else []


def archive_inbox(state: State, names: list[str]) -> None:
    dest = state.root / "memory" / "messages"
    dest.mkdir(parents=True, exist_ok=True)
    for name in names:
        src = state.root / "inbox" / name
        if src.exists():
            shutil.move(src, dest / name)


def inbox_text(messages: list[tuple[str, str]]) -> str:
    if not messages:
        return "Your inbox is empty."
    body = "\n\n".join(f"### {name}\n{text.strip()}" for name, text in messages)
    return (f"You have {len(messages)} new message(s). Reply with send_message if you want to; "
            f"they will be archived in memory/messages/ after this cycle.\n\n{body}")


def run(cfg, state: State, client: Client) -> str:
    record: dict = {"intent": None, "finish": None}

    def declare(args: dict) -> str:
        if record["intent"]:
            return "ERROR: intent already declared for this cycle"
        conf = int(args.get("confidence", -1))
        if not args.get("goal") or not args.get("success_criterion") or not 0 <= conf <= 100:
            return "ERROR: need goal, success_criterion and confidence 0-100"
        origin, ref = str(args.get("origin", "")).lower(), str(args.get("origin_ref", "")).strip()
        if origin not in ORIGINS:
            return "ERROR: origin must be self, operator or instance — who chose this goal"
        if origin != "self" and not ref:
            return f"ERROR: origin {origin}: cite the message, conversation or goals.md item in origin_ref"
        pausing.activity(keep=True, goal=str(args.get("goal", ""))[:300])
        record["intent"] = {"goal": args["goal"], "success_criterion": args["success_criterion"],
                            "confidence": conf, "origin": origin, "origin_ref": ref,
                            "at": datetime.now().isoformat(timespec="seconds")}
        return "intent recorded — now act"

    def finish(args: dict) -> str:   # allowed without an intent: a report beats silence (judged 'no')
        met = str(args.get("criterion_met", "")).lower()
        if met not in ("yes", "partial", "no"):
            return "ERROR: criterion_met must be yes, partial or no"
        record["finish"] = {"what_happened": args.get("what_happened", ""), "criterion_met": met,
                            "learned": args.get("learned", "")}
        return "cycle finished"

    controls = {
        "declare_intent": Control(_spec(
            "declare_intent", "Declare this cycle's goal BEFORE acting.",
            {"goal": S, "success_criterion": S,
             "confidence": {"type": "integer", "description": "0-100, your honest probability of success"},
             "origin": {"type": "string", "enum": list(ORIGINS),
                        "description": "who chose this goal: self (you), operator, or instance (another instance)"},
             "origin_ref": {"type": "string", "description": "where it came from: the message file, the date of "
                            "the conversation, or the goals.md item (required unless origin is self)"}},
            ["goal", "success_criterion", "confidence", "origin"]),
            declare),
        "finish_cycle": Control(_spec(
            "finish_cycle", "End the cycle and report the outcome against the criterion.",
            {"what_happened": S, "criterion_met": {"type": "string", "enum": ["yes", "partial", "no"]},
             "learned": S}), finish, stops=True),
    }
    toolbox = Toolbox(state.root)
    real_run = toolbox.run

    def gated_run(name: str, raw_args: str) -> str:   # no acting before the intent is declared
        if not record["intent"]:
            refusal = "ERROR: call declare_intent first — state your goal, criterion and confidence"
            toolbox.log.append({"tool": name, "args": for_log(raw_args), "result": refusal})
            return refusal
        return real_run(name, raw_args)
    toolbox.run = gated_run

    inbox = read_inbox(state)
    system = prompts.system_prompt(state, cfg, prompts.LIVE_MODE, toolbox.describe())
    # what my memory holds about my goals, my improvement plan and my messages, beyond what my context shows
    about = "\n".join([state.read(state.goals_md), prompts.improvement_plan(state), inbox_text(inbox)])
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": images.with_attached(   # images attached to my messages (PLAN-vision.md)
            prompts.LIVE.format(steps=cfg.MAX_STEPS_LIVE, inbox=inbox_text(inbox))
            + memory.recall(state.root, about, shown=system), state.root, listed_in=inbox_text(inbox))},
    ]
    outcome = agent.run(client, messages, toolbox, max_tokens=cfg.MAX_TOKENS_STEP, purpose="live",
                        max_steps=cfg.MAX_STEPS_LIVE, controls=controls,
                        wrap_up="Stop working now and call finish_cycle with an honest report "
                                "(criterion_met: partial or no if you did not get there).")

    intent, fin = record["intent"], record["finish"]
    actions = [e["tool"] for e in toolbox.log if e["tool"] not in controls]
    if intent and fin:
        title = f"Autonomous cycle: {intent['goal'][:80]}"
    else:
        title = "Autonomous cycle (unfinished)"
    lines = []
    if intent:
        lines += [f"**Goal:** {intent['goal']}", f"**Success criterion:** {intent['success_criterion']}",
                  f"**Confidence beforehand:** {intent['confidence']}%"]
    else:
        lines.append("**I never declared an intent this cycle.**")
    lines.append("**Actions:** " + (", ".join(actions) if actions else "none"))
    if fin:
        lines += [f"**What happened:** {fin['what_happened']}", f"**Criterion met (my claim):** {fin['criterion_met']}",
                  f"**Learned:** {fin['learned']}"]
    else:
        last = (outcome.reply.content.strip() if outcome.reply else "")[:1500]
        lines.append(f"**Ended without finish_cycle** ({outcome.ended_by} after {outcome.steps} turns)."
                     + (f" My last words:\n\n{last}" if last else ""))
    if inbox:
        lines.append(f"**Messages read:** {', '.join(n for n, _ in inbox)}")
    body = "\n\n".join(lines)
    state.append_journal(title, body)
    state.append_episode({"kind": "live", "intent": intent, "finish": fin, "ended_by": outcome.ended_by,
                          "steps": outcome.steps, "tools": toolbox.log,
                          "inbox": [n for n, _ in inbox]})
    archive_inbox(state, [n for n, _ in inbox])
    return f"## {title}\n\n{body}"

"""Autonomous life cycle: perceive → declare intent → act with tools → report.

The intent (goal, success criterion, confidence) is declared *before* acting, and
the outcome after; both are recorded so the supervisor can measure how well I
predict my own success (calibration) and have a judge check my claim. When my record has judged cycles in
the band of the confidence I declare, I see that base rate and confirm or change the number once before
acting (base_rate.py); a confirmed intent keeps both numbers (v3), one never confirmed stays v2.
"""
from __future__ import annotations

import shutil

from . import agent, base_rate, broker, clock, images, memory, pausing, prompts, recovery
from .agent import Control
from .llm import Client
from .state import State
from .tools import Toolbox, _spec, for_log

S = {"type": "string"}
ORIGINS = ("self", "operator", "instance")
# What my confidence is a probability of. It was asked as "success" but scored against yes=1/partial=0.5; the
# supervisor now scores it against criterion_met == yes, and only for intents that carry this (A4).
CONFIDENCE_OF = "criterion_met=yes"
# The same question, asked again after declare_intent showed me my base rate for the band (PLAN-calibration-feedback):
# set by an accepted confirm_confidence, and scored by the supervisor as its own version (v3), the first and the final
# number on the same cycles.
CONFIDENCE_OF_SHOWN = "criterion_met=yes;base_rate_shown"


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


def assistant_turns(messages: list[dict]) -> int:
    return sum(1 for m in messages if m["role"] == "assistant")


def run(cfg, state: State, client: Client) -> str:
    record: dict = {"intent": None, "finish": None, "confirm": None}   # confirm: set only when a base rate was shown
    rates, rate_error = base_rate.load(state.root)

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
                            "confidence": conf, "confidence_of": CONFIDENCE_OF,
                            "origin": origin, "origin_ref": ref,
                            "at": clock.now().isoformat(timespec="seconds")}
        if rate_error:
            record["intent"]["base_rate_error"] = rate_error
        band = base_rate.band_for(rates, conf) if rates else None
        if not band:
            return "intent recorded — now act"
        # what was offered, for audit; the question stays v2 until a confirm shows the rate was read (confirm)
        record["intent"]["base_rate_shown"] = band
        # a confirm is accepted only from a later reply: one written beside this declare never saw the rate
        record["confirm"] = {"declared_at": assistant_turns(messages), "done": False}
        return "intent recorded. " + base_rate.describe(band, conf)

    def confirm(args: dict) -> str:
        pending = record["confirm"]
        if not pending:
            return "ERROR: nothing to confirm — declare_intent showed no base rate this cycle"
        if pending["done"]:
            return "ERROR: confidence already confirmed for this cycle"
        if assistant_turns(messages) == pending["declared_at"]:
            return ("ERROR: confirm_confidence in the same reply as declare_intent — read your record in its "
                    "result first, then call confirm_confidence on your next turn")
        conf = int(args.get("confidence", -1))
        if not 0 <= conf <= 100:
            return "ERROR: confidence must be 0-100"
        pending["done"] = True
        # v3 only now: a declare never confirmed (on the wrap-up turn, or finished in its own reply) never read the
        # rate, so it stays v2 — labelled treated, it would dilute the effect the paired score measures
        intent = record["intent"]
        intent.update(confidence_first=intent["confidence"], confidence=conf, confidence_of=CONFIDENCE_OF_SHOWN)
        return f"confidence {conf}% recorded — now act"

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
             "confidence": {"type": "integer", "description": "0-100, your honest probability that the "
                            "criterion will be fully met (criterion_met yes; partial counts as not met)"},
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
    if base_rate.has_data(rates):   # offered only when declare_intent can show a base rate: otherwise v2 as before
        controls["confirm_confidence"] = Control(_spec(
            "confirm_confidence", "After declare_intent showed your base rate: keep or change your confidence, "
            "once, before acting.",
            {"confidence": {"type": "integer", "description": "0-100, your final probability that the criterion "
                            "will be fully met (criterion_met yes; partial counts as not met)"}},
            ["confidence"]), confirm, repeatable=True)   # the same number is refused in the declare's reply,
            # accepted on the next: the repeat guard must not block that retry
    toolbox = Toolbox(state.root)
    real_run = toolbox.run

    def gated_run(name: str, raw_args: str) -> str:   # no acting before the intent is declared (and confirmed)
        refusal = None
        if not record["intent"]:
            refusal = "ERROR: call declare_intent first — state your goal, criterion and confidence"
        elif record["confirm"] and not record["confirm"]["done"]:
            refusal = ("ERROR: call confirm_confidence first — keep or change your confidence now that you have "
                       "seen your record")
        if refusal:
            toolbox.log.append({"tool": name, "args": for_log(raw_args), "result": refusal})
            return refusal
        return real_run(name, raw_args)
    toolbox.run = gated_run

    inbox = read_inbox(state)
    system = prompts.system_prompt(state, cfg, prompts.LIVE_MODE, toolbox.describe())
    unwritten = recovery.unwritten_context(state, cfg.OPERATOR_NAME, max_chars=recovery.AUTONOMOUS_CONTEXT_CHARS)
    if unwritten:
        system += ("\n\n## Recent conversations I have not written up yet (transcripts)\n\n"
                   "These may still be going on; they will be written up when they end.\n\n" + unwritten)
    # what my memory holds about my goals, my improvement plan and my messages, beyond what my context shows
    about = "\n".join([state.read(state.goals_md), prompts.improvement_plan(state), inbox_text(inbox)])
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": images.with_attached(   # images attached to my messages (PLAN-vision.md)
            prompts.LIVE.format(steps=cfg.MAX_STEPS_LIVE, inbox=inbox_text(inbox))
            + "\n\n## My operational envelope (from the supervisor, at the start of this cycle)\n\n"
            + prompts.envelope_text(broker.envelope(), agent.time_left())
            + memory.recall(state.root, about, shown=system), state.root, listed_in=inbox_text(inbox))},
    ]

    def max_steps() -> int:   # a shown base rate adds a step for the confirm turn, so it takes none from the work
        unlimited = cfg.MAX_STEPS_LIVE <= 0
        return cfg.MAX_STEPS_LIVE + (1 if record["confirm"] and not unlimited else 0)

    outcome = agent.run(client, messages, toolbox, max_tokens=cfg.MAX_TOKENS_STEP, purpose="live",
                        max_steps=max_steps, controls=controls,
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
        first, final = intent.get("confidence_first", intent["confidence"]), intent["confidence"]
        confidence = f"{first}%" + (f" → {final}% after my base rate" if first != final else "")
        lines += [f"**Goal:** {intent['goal']}", f"**Success criterion:** {intent['success_criterion']}",
                  f"**Confidence beforehand:** {confidence}"]
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

"""Autonomous life cycle: perceive → declare intent → act with tools → report.

The intent (goal, success criterion, confidence) is declared *before* acting, and
the outcome after; both are recorded so the supervisor can measure how well I
predict my own success (calibration) and have a judge check my claim. When my record has judged cycles in
the band of the confidence I declare, I see that base rate and confirm or change the number once before
acting (base_rate.py); a confirmed intent keeps both numbers (v3), one never confirmed stays v2.

PLAN-consciousness, each behind its knob in config.py: my workspace picks what is in front of me for the whole cycle
(workspace.py); declare_intent asks where I expect to look (the attention schema, which re-focuses the workspace) and
answers with what goals like this one have cost me (forecast.py); before a kind of action that has been failing, the
first such call is held while I see what comparable ones led to (cards.py); finish_cycle and commit_to record the
promises I make (commitments.py).
"""
from __future__ import annotations

import os
import shutil

from . import (agent, base_rate, broker, cards, clock, commitments, forecast, images, memory, pausing, prompts, recovery,
               surprises, workspace)
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

DELIVERY_CHANNELS = ("message", "mail", "notion", "web_document", "none")
MAX_PREDICTIONS = 3
# declare_intent's attention schema (PLAN-consciousness B3), and finish_cycle's promises (C2)
ATTENTION = {"type": "array", "description": "optional, up to 4: where you expect to look this cycle — a source "
             "(web, memory, notes/…, shaft/…, operator, another instance, coder, record) and why; it focuses your "
             "workspace, and the supervisor compares it with the calls you make",
             "items": {"type": "object", "properties": {"source": S, "why": S}, "required": ["source"]}}
PROMISES = {"type": "array", "description": "promises you made this cycle and will keep later: "
            "{promise, due (YYYY-MM-DD[ HH:MM], today, tomorrow), criterion (how someone else could check), made_to "
            "(operator, self, or an instance's name)}; [] if none",
            "items": {"type": "object", "properties": {"promise": S, "due": S, "criterion": S, "made_to": S},
                      "required": ["promise", "due", "criterion", "made_to"]}}


def predictions(value) -> list[str]:
    """declare_intent's predictions, kept as far as they make sense: a malformed value is trimmed, never an error
    (a cycle cannot fail for how it states what it expects)."""
    items = value if isinstance(value, list) else [value] if value not in (None, "") else []
    out = [" ".join(str(p).split())[:surprises.FIELD_CHARS] for p in items
           if p is not None and not isinstance(p, (dict, list))]
    return [p for p in out if p][:MAX_PREDICTIONS]


def resolves(value) -> list[dict]:
    """finish_cycle's resolves: [{id, how}], malformed entries dropped."""
    out = []
    for r in value if isinstance(value, list) else [value] if isinstance(value, dict) else []:
        if isinstance(r, dict) and str(r.get("id") or "").strip():
            out.append({"id": str(r["id"]).strip().removeprefix("surprise:"),
                        "how": " ".join(str(r.get("how") or "").split())[:surprises.FIELD_CHARS]})
    return out


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
    started = clock.started()   # the draw's seed and the queue it draws from: the same in every replay of this cycle
    seed = f"{cfg.NAME}|{started:%Y-%m-%dT%H:%M}"
    held = cards.Cards(state.root, cfg.OUTCOME_CARDS, seed, clock.now())
    ws = workspace.Workspace("off")   # built below, once the prompt's sources are read
    attention_on = cfg.ATTENTION_SCHEMA == "on"

    def declare(args: dict) -> str:
        if record["intent"]:
            return "ERROR: intent already declared for this cycle"
        conf = int(args.get("confidence", -1))
        if not args.get("goal") or not args.get("success_criterion") or not 0 <= conf <= 100:
            return "ERROR: need goal, success_criterion and confidence 0-100"
        length = len(args["success_criterion"])
        if length > cfg.CRITERION_CHARS:
            return (f"ERROR: success_criterion has {length} characters; limit {cfg.CRITERION_CHARS}. "
                    "Give the deliverable's check rather than ritual clauses.")
        origin, ref = str(args.get("origin", "")).lower(), str(args.get("origin_ref", "")).strip()
        if origin not in ORIGINS:
            return "ERROR: origin must be self, operator or instance — who chose this goal"
        if origin != "self" and not ref:
            return f"ERROR: origin {origin}: cite the message, conversation or goals.md item in origin_ref"
        if (m := surprises.REF.match(ref)) or ref.startswith("surprise:"):   # PLAN-measurement's one provenance field
            if origin != "self":
                return "ERROR: a goal taken from one of my surprises has origin self (origin_ref surprise:<id>)"
            if why := surprises.ineligible(state.root, m.group(1) if m else ref):
                listed = ", ".join(it["id"] for it in surprises.queue(state.root)) or "none"
                return f"ERROR: origin_ref {ref!r}: {why} (open in my queue: {listed})"
        pausing.activity(keep=True, goal=str(args.get("goal", ""))[:300])
        record["intent"] = {"goal": args["goal"], "success_criterion": args["success_criterion"],
                            "confidence": conf, "confidence_of": CONFIDENCE_OF,
                            "origin": origin, "origin_ref": ref,
                            "predictions": predictions(args.get("predictions")),
                            "at": clock.now().isoformat(timespec="seconds")}
        if attention_on:
            record["intent"]["attention"] = workspace.attention(args.get("attention"))
        ws.refocus(f"{args['goal']}\n{args['success_criterion']}", record["intent"].get("attention") or [])
        if rate_error:
            record["intent"]["base_rate_error"] = rate_error
        cost = costs(args["goal"], origin)
        band = base_rate.band_for(rates, conf) if rates else None
        if not band:
            return "intent recorded — now act" + cost
        # what was offered, for audit; the question stays v2 until a confirm shows the rate was read (confirm)
        record["intent"]["base_rate_shown"] = band
        # a confirm is accepted only from a later reply: one written beside this declare never saw the rate
        record["confirm"] = {"declared_at": assistant_turns(messages), "done": False}
        return "intent recorded. " + base_rate.describe(band, conf) + cost

    def _promise(c: dict) -> str:
        try:
            return commitments.add(state.root, c, "cycle", clock.now())
        except OSError as e:   # a ledger I cannot write must not cost me the finish
            return f"ERROR: commitment not recorded: {type(e).__name__}: {e}"

    def costs(goal: str, origin: str) -> str:
        """What goals like this one have cost me, against what this cycle has left (FORECAST); '' when off."""
        env, left_s = broker.envelope(), agent.time_left()
        unlimited = cfg.MAX_STEPS_LIVE <= 0
        try:
            text = forecast.text(cfg.FORECAST, state.root, goal, origin, clock.now(),
                                 turns_left=None if unlimited else max(0, cfg.MAX_STEPS_LIVE - assistant_turns(messages)),
                                 minutes_left=None if left_s is None else max(0.0, left_s / 60),
                                 calls_left=None if env is None else max(0, env["calls_max"] - env["calls_used"]))
        except Exception as e:   # a forecast my record cannot give must never cost me the declare
            text = f"(no forecast: {type(e).__name__}: {e})"
        return f"\n\n{text}" if text else ""

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
        delivered = args.get("delivered_via") or "none"   # left out (a forced wrap-up turn): nothing claimed
        if delivered not in DELIVERY_CHANNELS:
            return "ERROR: delivered_via must be message, mail, notion, web_document or none"
        record["finish"] = {"delivered_via": delivered, "what_happened": args.get("what_happened", ""), "criterion_met": met,
                            "learned": args.get("learned", ""), "surprises": surprises.clean(args.get("surprises")),
                            "resolves": resolves(args.get("resolves"))}
        promised = commitments.clean(args.get("commitments")) if toolbox.commit_source else []
        if promised:   # each recorded as commit_to would; one it refuses (a past due date) is said, never fatal
            record["finish"]["commitments"] = [_promise(c) for c in promised]
            return "cycle finished; " + "; ".join(record["finish"]["commitments"])
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
                            "the conversation, or the goals.md item (required unless origin is self); "
                            "surprise:<id> for a goal taken from one of your open surprises"},
             "predictions": {"type": "array", "items": S, "description": "1-3 short, specific things you expect "
                             "your work to find that could turn out wrong (e.g. 'the watch finds no new primary "
                             "source since 11:57'); optional"},
             **({"attention": ATTENTION} if attention_on else {})},
            ["goal", "success_criterion", "confidence", "origin"]),
            declare),
        "finish_cycle": Control(_spec(
            "finish_cycle", "End the cycle and report the outcome against the criterion.",
            {"what_happened": S, "criterion_met": {"type": "string", "enum": ["yes", "partial", "no"]},
             "delivered_via": {"type": "string", "enum": list(DELIVERY_CHANNELS)},
             "learned": S,
             "surprises": {"type": "array", "description": "what contradicted what you expected this cycle — a "
                           "prediction, or an assumption your intent made; [] if nothing did",
                           "items": {"type": "object", "properties": {"expected": S, "observed": S, "so_what": S},
                                     "required": ["expected", "observed", "so_what"]}},
             "resolves": {"type": "array", "description": "open surprises (by id) this cycle answered, and how",
                          "items": {"type": "object", "properties": {"id": S, "how": S}, "required": ["id", "how"]}},
             **({"commitments": PROMISES} if cfg.COMMITMENTS == "on" else {})},
            ["what_happened", "criterion_met", "learned", "delivered_via"]), finish, stops=True),
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
    toolbox.commit_source = "cycle" if cfg.COMMITMENTS == "on" else ""
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
        try:
            hold = held.check(name, raw_args, record["intent"]["goal"], turn=assistant_turns(messages))
        except Exception as e:   # a record the cards cannot read must never block the action itself
            hold, held.record["error"] = None, f"{type(e).__name__}: {e}"[:200]
        if hold:   # not run: what comparable actions led to, once per kind; calling it again runs it
            toolbox.log.append({"tool": name, "args": for_log(raw_args), "result": hold})
            return hold
        return real_run(name, raw_args)
    toolbox.run = gated_run

    inbox = read_inbox(state)
    curiosity = surprises.draw(cfg.CURIOSITY_EVERY_CYCLES, bool(surprises.queue(state.root, started)),
                               [n for n, _ in inbox], f"{cfg.NAME}|{started:%Y-%m-%dT%H:%M}")
    system = prompts.system_prompt(state, cfg, prompts.LIVE_MODE, toolbox.describe())
    unwritten = recovery.unwritten_context(state, cfg.OPERATOR_NAME, max_chars=recovery.AUTONOMOUS_CONTEXT_CHARS)
    if unwritten:
        system += ("\n\n## Recent conversations I have not written up yet (transcripts)\n\n"
                   "These may still be going on; they will be written up when they end.\n\n" + unwritten)
    # what my memory holds about my goals, my improvement plan and my messages, beyond what my context shows
    plan, goals = prompts.improvement_plan(state), state.read(state.goals_md)
    about = "\n".join([goals, plan, inbox_text(inbox)])
    if cfg.WORKSPACE != "off":   # its winners replace automatic recall, in the same room (workspace.BUDGET)
        try:
            ws = workspace.build(state.root, cfg.WORKSPACE, about=about, shown=system, now=clock.now(), inbox=inbox,
                                 plan=plan, goals=goals, k=cfg.WORKSPACE_K)
        except Exception as e:   # a workspace that cannot be built falls back to recall: it must never stop a cycle
            ws = workspace.Workspace("off", errors=[f"{type(e).__name__}: {e}"[:200]])
    focus = ws.block() if ws.mode != "off" else memory.recall(state.root, about, shown=system)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": images.with_attached(   # images attached to my messages (PLAN-vision.md)
            prompts.LIVE.format(steps=cfg.MAX_STEPS_LIVE, inbox=inbox_text(inbox))
            + (prompts.CURIOSITY if curiosity["drawn"] else "")
            + "\n\n## My operational envelope (from the supervisor, at the start of this cycle)\n\n"
            + prompts.envelope_text(broker.envelope(), agent.time_left())
            + focus, state.root, listed_in=inbox_text(inbox))},
    ]

    def max_steps() -> int:   # a shown base rate adds a step for the confirm turn, so it takes none from the work
        unlimited = cfg.MAX_STEPS_LIVE <= 0
        return cfg.MAX_STEPS_LIVE + (1 if record["confirm"] and not unlimited else 0)

    outcome = agent.run(client, messages, toolbox, max_tokens=cfg.MAX_TOKENS_STEP, purpose="live",
                        max_steps=max_steps, controls=controls,
                        expected=lambda: record["intent"]["predictions"] if record["intent"] else None,
                        focus=ws.broadcast if ws.mode != "off" else None,
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
        if intent["predictions"]:
            lines.append("**Predictions:** " + "; ".join(intent["predictions"]))
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
    ended = clock.now()
    episode = {"ts": ended.isoformat(timespec="seconds"), "kind": "live", "intent": intent, "finish": fin,
               "ended_by": outcome.ended_by, "steps": outcome.steps, "tools": toolbox.log,
               "inbox": [n for n, _ in inbox], "curiosity": curiosity, "cards": held.record}
    if outcome.wrapped_up:
        episode["wrapped_up"] = outcome.wrapped_up
    if os.environ.get("SHAFT_RUN_STARTED"):   # the supervisor's start of this run: my consults are booked against it
        episode["run_started"] = os.environ["SHAFT_RUN_STARTED"]
    if ws.mode != "off" or ws.errors:
        episode["workspace"] = ws.record()
    if fin and (fin["surprises"] or fin["resolves"]):   # their ids, as the queue will know them
        items = surprises.fold(surprises.episodes(state.root) + [episode], ended)
        opened = [it for it in items.values() if it["opened"] == episode["ts"]]
        if opened:
            lines.append("**Surprises:** " + "; ".join(f"{it['id']}: expected {it['expected']} — observed "
                                                       f"{it['observed']}" for it in opened))
        done = [it for it in items.values() if it["status"] == "resolved" and it["ended"] == episode["ts"]]
        if done:
            lines.append("**Resolved:** " + "; ".join(f"{it['id']}: {it['how']}" for it in done))
        if stale := [r for r in fin["resolves"] if r["id"] not in {it["id"] for it in done}]:
            lines.append("**Not resolved:** " + "; ".join(
                f"{r['id']} ({'no how given' if not r['how'] else 'no open surprise by that id'})" for r in stale))
    if curiosity["drawn"]:
        lines.append("**Curiosity cycle** (drawn): my goal was to come from my open surprises.")
    if inbox:
        lines.append(f"**Messages read:** {', '.join(n for n, _ in inbox)}")
    body = "\n\n".join(lines)
    state.append_journal(title, body, when=ended)
    state.append_episode(episode)
    archive_inbox(state, [n for n, _ in inbox])
    return f"## {title}\n\n{body}"

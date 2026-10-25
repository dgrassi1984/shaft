"""Autonomous life cycle: perceive → declare intent → act with tools → report.

The intent (goal, success criterion, confidence) is declared *before* acting, and
the outcome after; both are recorded so the supervisor can measure how well I
predict my own success (calibration) and have a judge check my claim.
"""
from __future__ import annotations

import shutil

from . import agent, broker, clock, images, memory, pausing, prompts, recovery, surprises
from .agent import Control
from .llm import Client
from .state import State
from .tools import Toolbox, _spec, for_log

S = {"type": "string"}
ORIGINS = ("self", "operator", "instance")
# What my confidence is a probability of. It was asked as "success" but scored against yes=1/partial=0.5; the
# supervisor now scores it against criterion_met == yes, and only for intents that carry this (A4).
CONFIDENCE_OF = "criterion_met=yes"
MAX_PREDICTIONS = 3


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


def run(cfg, state: State, client: Client) -> str:
    record: dict = {"intent": None, "finish": None}
    started = clock.started()   # the draw's seed and the queue it draws from: the same in every replay of this cycle

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
        return "intent recorded — now act"

    def finish(args: dict) -> str:   # allowed without an intent: a report beats silence (judged 'no')
        met = str(args.get("criterion_met", "")).lower()
        if met not in ("yes", "partial", "no"):
            return "ERROR: criterion_met must be yes, partial or no"
        record["finish"] = {"what_happened": args.get("what_happened", ""), "criterion_met": met,
                            "learned": args.get("learned", ""), "surprises": surprises.clean(args.get("surprises")),
                            "resolves": resolves(args.get("resolves"))}
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
                             "source since 11:57'); optional"}},
            ["goal", "success_criterion", "confidence", "origin"]),
            declare),
        "finish_cycle": Control(_spec(
            "finish_cycle", "End the cycle and report the outcome against the criterion.",
            {"what_happened": S, "criterion_met": {"type": "string", "enum": ["yes", "partial", "no"]},
             "learned": S,
             "surprises": {"type": "array", "description": "what contradicted what you expected this cycle — a "
                           "prediction, or an assumption your intent made; [] if nothing did",
                           "items": {"type": "object", "properties": {"expected": S, "observed": S, "so_what": S},
                                     "required": ["expected", "observed", "so_what"]}},
             "resolves": {"type": "array", "description": "open surprises (by id) this cycle answered, and how",
                          "items": {"type": "object", "properties": {"id": S, "how": S}, "required": ["id", "how"]}}},
            ["what_happened", "criterion_met", "learned"]), finish, stops=True),
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
    curiosity = surprises.draw(cfg.CURIOSITY_EVERY_CYCLES, bool(surprises.queue(state.root, started)),
                               [n for n, _ in inbox], f"{cfg.NAME}|{started:%Y-%m-%dT%H:%M}")
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
            + (prompts.CURIOSITY if curiosity["drawn"] else "")
            + "\n\n## My operational envelope (from the supervisor, at the start of this cycle)\n\n"
            + prompts.envelope_text(broker.envelope(), agent.time_left())
            + memory.recall(state.root, about, shown=system), state.root, listed_in=inbox_text(inbox))},
    ]
    outcome = agent.run(client, messages, toolbox, max_tokens=cfg.MAX_TOKENS_STEP, purpose="live",
                        max_steps=cfg.MAX_STEPS_LIVE, controls=controls,
                        expected=lambda: record["intent"]["predictions"] if record["intent"] else None,
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
               "inbox": [n for n, _ in inbox], "curiosity": curiosity}
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

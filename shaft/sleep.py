"""Sleep: consolidate recent experience into facts, people, personality and goals."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime

from . import (agent, broker, clock, commitments, corrections, drive_proposals, loopguard, memory, projects, prompts,
               recovery, staleness, surprises)
from .llm import Client, LLMError
from .state import State
from .tools import FACT_LINE, SOURCE, Toolbox, _spec, for_log

# When my last finished sleep read the judge's verdicts: the next one asks only for those judged since (A2).
VERDICTS_REVIEWED = "memory/verdicts-reviewed.md"


def _since(state: State) -> str:
    """Where this sleep's verdicts start: the marker; for my first sleep with it, my last sleep (what came since);
    for a first sleep ever, the beginning. Starting at the beginning on an instance with hundreds of verdicts would
    spend weeks of sleeps, 20 a night, on stale ones (REVIEW-autonomy A2)."""
    path = state.root / VERDICTS_REVIEWED
    if path.exists():
        return state.read(path).strip()
    for entry in reversed(state.journal_entries()):
        title = entry.splitlines()[0].lstrip("# ").strip()
        if title.partition(" — ")[2] == "Sleep":   # a finished one: an unfinished sleep read no verdicts
            try:
                return datetime.strptime(title[:16], "%Y-%m-%d %H:%M").strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
    return "1970-01-01 00:00:00"


def _verdicts(raw: str) -> dict:
    data = json.loads(raw)
    if (not isinstance(data, dict) or not isinstance(data.get("text"), str)
            or not isinstance(data.get("verdicts"), list) or not isinstance(data.get("left_out"), list)
            or "next_since" not in data
            or data["next_since"] is not None and not isinstance(data["next_since"], str)
            or any(not isinstance(row, dict) or not isinstance(row.get("episode_ts"), str)
                   or not isinstance(row.get("reviewed_at"), str) for row in data["verdicts"])
            or any(not isinstance(ts, str) for ts in data["left_out"])):
        raise ValueError("invalid judgments response")
    return data


S = {"type": "string"}
CLOSE_SURPRISE = _spec(
    "close_surprise", "Close one of my open surprises (in my sleep) when it needs no cycle of its own: say what became "
    "of it. To keep what it taught me, remember a fact citing its id instead (or as well).",
    {"id": {"type": "string", "description": "the surprise's id, e.g. s1013-0653-1"},
     "outcome": {"type": "string", "enum": list(surprises.OUTCOMES)},
     "note": {"type": "string", "description": "why: one sentence"}})


RETIRE_FACT = _spec(
    "retire_fact", "Retire one current fact contradicted by a newer observation (in my sleep). "
    "fact is its date stamp and optional opening words. evidence is log:<episode ts>[#step], "
    "a newer current fact's date (optionally fact:<date>), web:<url> fetched this run, or operator. "
    "Dated evidence must be later; the old fact and evidence remain searchable in the archive.",
    {"fact": S, "evidence": S})


def fact_candidates(root) -> str:
    try:
        path = root / "memory" / "facts.md"
        rows = []
        for line in path.read_text().splitlines() if path.exists() else []:
            if not FACT_LINE.match(line):
                continue
            stamp, text = line[3:].split("] ", 1)
            at = datetime.fromisoformat(stamp)
            text = SOURCE.sub("", text).split(" (supersedes:")[0]
            words = memory._tokens(text)
            keys = {"prefix:" + " ".join(words[:3])} if len(words) >= 3 else set()
            keys |= {"entity:" + word.lower() for word in re.findall(r"\b[A-Z][a-z]+(?:-[A-Za-z]+)*\b", text)}
            if text.lower().startswith("today is "):
                keys.add("key:today is")
            rows.append((at, line, keys))
        # a key most facts share (the operator's name) pairs everything with everything: on thelos's real facts it
        # filled all 15 pairs with "Daniele" (2026-10-28). Only keys a few facts share count, and an older fact gets
        # one pair: the newest fact it shares the most of them with.
        seen: dict[str, int] = {}
        for _, _, keys in rows:
            for k in keys:
                seen[k] = seen.get(k, 0) + 1
        common = max(3, len(rows) // 10)
        rows = sorted((at, line, {k for k in keys if k.startswith("key:") or seen[k] <= common}) for at, line, keys in rows)
        pairs = []
        for i, (at, line, keys) in enumerate(rows):
            later = [(len(keys & other_keys), when, other) for when, other, other_keys in rows[i + 1:]
                     if when > at and keys & other_keys]
            if later:
                pairs.append(f"- Older: {line}\n  Newer: {max(later)[2]}")
                if len(pairs) == 15:
                    break
        return "\n".join(pairs) or "(no candidate pairs; still check this period's episodes)"
    except Exception as e:   # a prompt block reading a record must not stop the sleep
        return f"(fact candidates could not be read: {type(e).__name__}: {e})"


def _last_sleep(state: State) -> str:
    """When my last finished sleep ended (its episode's ts), or '' before my first: the surprises since then are mine
    to go through."""
    finished = [ep.get("ts", "") for ep in surprises.episodes(state.root)
                if ep.get("kind") == "sleep" and ep.get("ended_by") == "answer"]
    return max(finished, default="")


def surprises_text(items: list[dict]) -> str:
    rows = []
    for it in items:
        status = {"open": "open", "resolved": f"resolved by a cycle: {it['how']}",
                  "closed": f"closed ({it['outcome']})",
                  "expired": f"expired ({it['reason']}): nothing to do — a fact citing it if it taught me something"
                  }[it["status"]]
        rows.append(f"- {it['id']} [{status}{', depth 2: not shown in my queue' if it['depth'] > 1 else ''}]: "
                    f"expected {it['expected'] or '?'} — observed {it['observed'] or '?'}"
                    + (f" — so what: {it['so_what']}" if it["so_what"] else ""))
    return "\n".join(rows) or "(none)"


# The writes a sleep must not make from an open conversation, and the file each one changes ("": all new text).
GUARDED = {"retire_fact": "", "remember": "", "write_digest": "", "rewrite_facts": "memory/facts.md",
           "update_personality": "self/personality.md", "update_self_model": "self/self.md"}


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i + 8]) for i in range(len(words) - 7)}


def run(cfg, state: State, client: Client) -> str:
    since = _since(state)
    fetched = None
    verdicts = "The supervisor's record could not be read: no broker available."
    if broker.available():
        # Pages until caught up, within a character budget: the block is re-sent on every sleep step, and a backlog of
        # full pages (24,000 characters each) would outgrow the model's window and fail every sleep, the marker never
        # moving (Grok's review). A page that would go over is left for the next sleep, which resumes at the last page
        # read; the first page is always read.
        cursor, cut = since, False
        while True:
            try:
                page = _verdicts(broker.call("record", kind="judgments", limit=20, since=cursor, format="json"))
            except (broker.BrokerError, ValueError, TypeError) as e:
                if fetched is None:
                    verdicts = f"The supervisor's record could not be read: {' '.join(str(e).split())}"
                break
            if fetched is None:
                fetched = page
            elif len(fetched["text"]) + len(page["text"]) > cfg.SLEEP_VERDICT_CHARS:
                cut = True
                break
            else:
                fetched["text"] += "\n\n" + page["text"]
                fetched["verdicts"].extend(page["verdicts"])
                fetched["left_out"].extend(page["left_out"])
                fetched["next_since"] = page["next_since"] or cursor
            verdicts = fetched["text"]
            if page["next_since"] in (None, cursor):   # caught up; a cursor that stands still would loop forever
                break
            cursor = page["next_since"]
        if cut:
            note = (f"Sleep verdict feedback cut short at SLEEP_VERDICT_CHARS={cfg.SLEEP_VERDICT_CHARS}; "
                    f"more verdicts remain after {cursor}. Next sleep resumes there.")
            verdicts += "\n\n" + note
            print(note, file=sys.stderr)
    expectations = "Reply predictions could not be read: no broker available."
    if broker.available():
        try:
            last_sleep = _last_sleep(state)
            after = {"since": datetime.fromisoformat(last_sleep).strftime("%Y-%m-%d %H:%M:%S")} if last_sleep else {}
            expectations = broker.call("record", kind="expectations", limit=50, **after)
        except (broker.BrokerError, ValueError, TypeError) as e:
            expectations = f"Reply predictions could not be read: {' '.join(str(e).split())}"
    unwritten, turns = recovery.unwritten_transcripts(
        state, cfg.OPERATOR_NAME, max_chars=recovery.AUTONOMOUS_CONTEXT_CHARS)
    shingles = set().union(*(_shingles(text) for text in turns))
    toolbox = Toolbox(state.root)
    toolbox.loop_guard = loopguard.Guard(cfg.LOOP_GUARD, cfg.LOOP_EFFECT_ROUNDS)
    toolbox.remote = {name: spec for name, spec in toolbox.remote.items() if name in ("record", "history")}
    toolbox.plugins = {}
    promises = cfg.COMMITMENTS == "on"
    toolbox.extra = [projects.OPEN, projects.CLOSE, drive_proposals.PROPOSE, CLOSE_SURPRISE, corrections.CLOSE, RETIRE_FACT]
    toolbox.extra += [commitments.SETTLE] if promises else []
    real_run = toolbox.run
    closed: list[dict] = []

    def close(args: dict) -> str:
        sid = str(args.get("id") or "").strip().removeprefix("surprise:")
        outcome, note = str(args.get("outcome") or "").strip(), " ".join(str(args.get("note") or "").split())
        items = surprises.all_items(state.root)
        for c in closed:   # closed earlier in this sleep: its episode is written at the end
            items[c["id"]]["status"] = "closed"
        if outcome not in surprises.OUTCOMES:
            return f"ERROR: outcome must be one of {', '.join(surprises.OUTCOMES)}"
        if not note:
            return "ERROR: say in note why it is closed"
        if sid not in items:
            listed = ", ".join(i for i, it in items.items() if it["status"] == "open") or "none open"
            return f"ERROR: I have no surprise {sid!r} (open: {listed})"
        if items[sid]["status"] != "open":
            return (f"ERROR: {sid} is already {items[sid]['status']}"
                    + (": it needs nothing (remember a fact citing it if it taught me something)"
                       if items[sid]["status"] == "expired" else ""))
        closed.append({"id": sid, "outcome": outcome, "note": note[:surprises.FIELD_CHARS]})
        return f"closed {sid} ({outcome})"

    def gated_run(name: str, raw_args: str) -> str:
        try:
            args = json.loads(raw_args or "{}")
        except ValueError:
            return real_run(name, raw_args)
        if not isinstance(args, dict):
            return real_run(name, raw_args)
        handlers = {"open_project": projects.open_project, "close_project": projects.close_project,
                    "propose_drive_amendment": drive_proposals.propose}
        if name in handlers:
            try:
                result = handlers[name](state.root, args, clock.now())
            except (OSError, ValueError) as e:
                result = f"ERROR: {name}: {type(e).__name__}: {e}"
            toolbox.log.append({"tool": name, "args": for_log(raw_args), "result": result})
            return result
        if name == "close_correction":
            try:
                result = corrections.close(state.root, args, clock.now())
            except (OSError, ValueError) as e:
                result = f"ERROR: correction not closed: {type(e).__name__}: {e}"
            toolbox.log.append({"tool": name, "args": for_log(raw_args), "result": result})
            return result
        if name == "close_surprise":
            result = close(args)
            toolbox.log.append({"tool": name, "args": for_log(raw_args), "result": result})
            return result
        if name == "settle_commitment" and promises:   # kept only with an episode as evidence (commitments.settle)
            try:
                result = commitments.settle(state.root, args, clock.now())
            except OSError as e:   # a ledger I cannot read or write must not stop my sleep
                result = f"ERROR: commitment not settled: {type(e).__name__}: {e}"
            toolbox.log.append({"tool": name, "args": for_log(raw_args), "result": result})
            return result
        refusal = ""
        if name == "history" and args.get("save_as"):
            refusal = "ERROR: history is read-only during sleep; save_as writes a file"
        target = GUARDED.get(name, "")
        guarded = name in GUARDED
        if name in ("write_file", "append_file") and isinstance(args.get("path"), str):
            try:
                path = (toolbox.root / args["path"]).resolve()
            except (OSError, ValueError, RuntimeError):
                return real_run(name, raw_args)
            guarded = any(toolbox.root / folder in path.parents for folder in ("memory", "self"))
            target = args["path"]
        # Only what this write adds: rewrite_facts resubmits every fact it keeps, and an old one may share words
        # with a conversation going on now (Grok's review).
        try:
            held = set(state.read(toolbox.root / target).splitlines()) if target else set()
        except OSError:   # a folder, or unreadable: everything counts as new
            held = set()
        if guarded and any(shingles & _shingles("\n".join(l for l in value.splitlines() if l not in held))
                           for key, value in args.items() if key != "path" and isinstance(value, str)):
            refusal = ("ERROR: open-conversation transcripts are awareness only during sleep; "
                       "consolidate them in a later sleep after they are written up")
        if refusal:
            toolbox.log.append({"tool": name, "args": for_log(raw_args), "result": refusal})
            return refusal
        if name == "retire_fact":
            try:
                result = toolbox._t_retire_fact(**args)
            except Exception as e:
                result = f"ERROR: {type(e).__name__}: {e}"
            toolbox.log.append({"tool": name, "args": for_log(raw_args), "result": result})
            return result
        return real_run(name, raw_args)
    toolbox.run = gated_run

    operator_file = cfg.OPERATOR_NAME.lower().replace(" ", "-")
    system = prompts.system_prompt(state, cfg, prompts.SLEEP_MODE, toolbox.describe())
    if unwritten:
        system += ("\n\n## Recent conversations I have not written up yet (transcripts)\n\n"
                   "These conversations are still going on. Awareness only: do not consolidate them with remember, "
                   "rewrite_facts, retire_fact, write_digest, update_personality, update_self_model or memory/people writes. "
                   "A later sleep will consolidate them after they are written up.\n\n" + unwritten)
    goals_size = len(state.read(state.goals_md))
    goals_budget = (f" self/goals.md is {goals_size} characters, over the {cfg.GOALS_CHARS}-character budget. "
                    f"Rewrite the whole file with replace=true under {cfg.GOALS_CHARS} characters, keeping active "
                    "goals and standing rules; move history to a notes/ file."
                    if goals_size > cfg.GOALS_CHARS else "")
    stale = staleness.block(state.root, clock.now(), cfg.STALE_LINE_HOURS, cfg.STALE_GOAL_HOURS)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompts.SLEEP.format(operator=cfg.OPERATOR_NAME, operator_file=operator_file,
                                                         settle=prompts.SETTLE if promises else "",
                                                         goals_budget=goals_budget)
         + "\n\n## My projects (step 12)\n\n" + projects.block(state.root, clock.now(),
             verdicts=fetched["verdicts"] if fetched else [])
         + "\n\n## Stale context review (step 10)\n\n" + stale
         + "\n\n## Possible fact conflicts (step 11; candidates, not proof)\n\n" + fact_candidates(state.root)
         + "\n\n## What I expected others to answer, and what happened (step 2)\n\n"
         + (expectations or "(none scored since my last sleep)")
         + "\n\n## Verdicts the supervisor's judge gave since my last sleep\n\n" + (verdicts.strip() or "(none new)")
         + "\n\n## Surprises since my last sleep (step 7)\n\n"
         + surprises_text(surprises.since(state.root, _last_sleep(state)))
         + ("\n\n## My overdue commitments (step 8)\n\n" + _overdue(state) if promises else "")
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
        state.append_episode({"kind": "sleep", "ended_by": outcome.ended_by, "tools": toolbox.log, "closed": closed,
                              **toolbox.loop_guard.record()})
        raise LLMError(summary)
    # Before the journal entry: a finished sleep without a marker would otherwise be the next one's starting point,
    # skipping what it did not read. A failed read keeps where it started, so the next sleep asks again.
    (state.root / "memory").mkdir(parents=True, exist_ok=True)   # a first sleep writes here before its journal does
    if fetched is not None:
        known = {(row["episode_ts"], row.get("reviewed_at")) for row in memory.verdict_rows(state.root)}
        received = clock.now().isoformat(timespec="seconds")
        rows = [{**row, "received_at": received} for row in fetched["verdicts"]]
        rows += [{"episode_ts": ts, "left_out": True} for ts in fetched["left_out"]]
        with (state.root / memory.VERDICTS).open("a") as f:
            for row in rows:
                key = (row["episode_ts"], row.get("reviewed_at"))
                if key not in known:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    known.add(key)
    (state.root / VERDICTS_REVIEWED).write_text(
        (fetched["next_since"] if fetched and fetched["next_since"] is not None else since) + "\n")
    entry = outcome.reply.content.strip()
    entry += "\n\n*Consolidation actions:* " + ("; ".join(changes) or "none")
    state.append_journal("Sleep", entry)
    state.append_episode({"kind": "sleep", "ended_by": outcome.ended_by, "tools": toolbox.log,
                          "content": outcome.reply.content, "closed": closed, **toolbox.loop_guard.record()})
    return entry


def _overdue(state: State) -> str:
    try:
        return commitments.sleep_text(state.root, clock.now())
    except OSError as e:   # the sleep runs without the list rather than not at all
        return f"(my commitments could not be read: {type(e).__name__}: {e})"


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

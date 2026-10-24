"""Sleep: consolidate recent experience into facts, people, personality and goals."""
from __future__ import annotations

import json
import re
from datetime import datetime

from . import agent, broker, clock, memory, prompts, recovery
from .llm import Client, LLMError
from .state import State
from .tools import Toolbox, for_log

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


# The writes a sleep must not make from an open conversation, and the file each one changes ("": all new text).
GUARDED = {"remember": "", "write_digest": "", "rewrite_facts": "memory/facts.md",
           "update_personality": "self/personality.md", "update_self_model": "self/self.md"}


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i + 8]) for i in range(len(words) - 7)}


def run(cfg, state: State, client: Client) -> str:
    since = _since(state)
    fetched = None
    verdicts = "The supervisor's record could not be read: no broker available."
    if broker.available():
        try:
            fetched = _verdicts(broker.call("record", kind="judgments", limit=20, since=since, format="json"))
            verdicts = fetched["text"]
        except (broker.BrokerError, ValueError, TypeError) as e:
            verdicts = f"The supervisor's record could not be read: {' '.join(str(e).split())}"
    unwritten, turns = recovery.unwritten_transcripts(
        state, cfg.OPERATOR_NAME, max_chars=recovery.AUTONOMOUS_CONTEXT_CHARS)
    shingles = set().union(*(_shingles(text) for text in turns))
    toolbox = Toolbox(state.root)
    toolbox.remote = {name: spec for name, spec in toolbox.remote.items() if name in ("record", "history")}
    toolbox.plugins = {}
    real_run = toolbox.run

    def gated_run(name: str, raw_args: str) -> str:
        try:
            args = json.loads(raw_args or "{}")
        except ValueError:
            return real_run(name, raw_args)
        if not isinstance(args, dict):
            return real_run(name, raw_args)
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
        return real_run(name, raw_args)
    toolbox.run = gated_run

    operator_file = cfg.OPERATOR_NAME.lower().replace(" ", "-")
    system = prompts.system_prompt(state, cfg, prompts.SLEEP_MODE, toolbox.describe())
    if unwritten:
        system += ("\n\n## Recent conversations I have not written up yet (transcripts)\n\n"
                   "These conversations are still going on. Awareness only: do not consolidate them with remember, "
                   "rewrite_facts, write_digest, update_personality, update_self_model or memory/people writes. "
                   "A later sleep will consolidate them after they are written up.\n\n" + unwritten)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompts.SLEEP.format(operator=cfg.OPERATOR_NAME, operator_file=operator_file)
         + "\n\n## Verdicts the supervisor's judge gave since my last sleep\n\n" + (verdicts.strip() or "(none new)")
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
        state.append_episode({"kind": "sleep", "ended_by": outcome.ended_by, "tools": toolbox.log})
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
                          "content": outcome.reply.content})
    return entry


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

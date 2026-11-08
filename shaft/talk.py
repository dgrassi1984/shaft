"""Conversation with the operator — with my tools available.

Two front ends share one conversation loop:
- the plain terminal (default; also what pipes and tests use);
- `--json`: a headless protocol for the supervisor's terminal client (bin/shaft talk), which
  renders Markdown, edits multi-line input and keeps history. Events go to stdout as JSON
  lines; messages arrive on stdin as JSON lines; SIGINT stops the reply in progress.

  in : {"type": "user", "text": "..."}  |  {"type": "exit"}
  out: ready, status{phase,detail}, reasoning{text}, content{text}, tool_start{name,args},
       tool_end{name,ok,brief}, retry{old,new}, turn_end{steps,ended_by}, interrupted, error{message}, bye,
       heard{text} (`answer` only: a message the operator sent while I was replying, now in my reply — PLAN-interject.md)
"""
from __future__ import annotations

import json
import time
import re
import sys
import uuid

from . import agent, clock, images, loopguard, memory, prompts, recovery
from .llm import Client, LLMError
from .live import archive_inbox, read_whole
from .state import State
from .tools import Toolbox, _spec
from .ui import Status

DIM, RED, CYAN, RESET = "\033[2m", "\033[31m", "\033[36m", "\033[0m"
EXIT_WORDS = {"/exit", "/quit", "/bye"}


def since_last_talk(state: State, operator: str) -> str:
    """What I did since my last conversation, so I can bring it up."""
    entries = state.journal_entries()
    last = max((i for i, e in enumerate(entries) if f"— Conversation with {operator}" in e.splitlines()[0]),
               default=-1)
    titles = [e.splitlines()[0].lstrip("# ").strip() for e in entries[last + 1:]]
    inbox = state.root / "inbox"
    unread = len(list(inbox.glob("*.md"))) if inbox.is_dir() else 0
    parts = []
    if titles:
        parts.append(" Since your last conversation with them, your journal records: "
                     + "; ".join(titles[-12:]) + ". Mention what is worth mentioning.")
    if unread:
        parts.append(f" You have {unread} unread message(s) in inbox/.")
    return "".join(parts)


def _brief(args: str, n: int) -> str:
    return re.sub(r"\s+", " ", args)[:n]


def archive_read_inbox(state: State, entries: list[dict]) -> None:
    """Inbox messages I read in a reply are handled there: archived like a cycle's (live.archive_inbox), or
    the next cycle showed them again as new (REVIEW-autonomy A6). Archived on reading, not by a tool I must
    remember to call; a reply that was discarded handled nothing, and a message read only in part (read_file shows
    MAX_RESULT_CHARS from its offset) is not handled either (live.read_whole)."""
    if names := read_whole(state, entries):
        archive_inbox(state, names)


# -- views ---------------------------------------------------------------------------
class TerminalView:
    """Plain terminal: one line per message, a live status line, raw text."""

    def __init__(self, cfg, show_thinking: bool, input_fn=input):
        self.cfg, self.show_thinking, self.input_fn = cfg, show_thinking, input_fn
        self.ui = Status()
        self.speaking, self.reasoning = False, 0
        try:
            import readline  # noqa: F401  (line editing for input())
        except ImportError:
            pass

    def _out(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def ready(self, info: dict) -> None:
        self._out(f"{DIM}{info['name']} · {info['model']} · {info['journal_entries']} journal entries · "
                  f"{info['tools']} tools\nCtrl-C stops a reply · Ctrl-C at the prompt, Ctrl-D or /exit ends "
                  f"the conversation{RESET}\n\n")

    def usage(self, **numbers) -> None:   # the plain terminal shows speed in its status line only
        pass

    def next_message(self) -> str | None:
        try:
            return self.input_fn(f"{CYAN}you ›{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            self._out("\n")
            return None

    def status(self, phase: str, detail: str = "") -> None:
        if phase == "planned":
            self.ui.clear()
            self.ui.write(f"{DIM}planned · {detail}{RESET}\n")
        self.ui.show(phase, detail)

    def reasoning_chunk(self, chunk: str) -> None:
        self.reasoning += len(chunk)
        if self.show_thinking:
            self.ui.clear()
            self.ui.write(f"{DIM}{chunk}{RESET}")
        else:
            self.ui.show("thinking", f"~{self.reasoning / 4000:.1f}k tokens")

    def content_chunk(self, chunk: str) -> None:
        if not self.speaking:
            self.ui.clear()
            self.ui.write(("\n" if self.show_thinking and self.reasoning else "") + f"{self.cfg.NAME} › ")
            self.speaking = True
        self.ui.write(chunk)

    def retry(self, old: int, new: int) -> None:
        self.ui.clear()
        self.ui.write(f"\n{DIM}[reply cut off at {old} tokens — asking again with {new}]{RESET}\n")
        self.speaking, self.reasoning = False, 0
        self.ui.show("waiting for the model")

    def tool_start(self, name: str, args: str) -> None:
        if self.speaking:
            self.ui.write("\n")
        self.speaking = False
        self.ui.show(f"running {name}", _brief(args, 60))

    def tool_end(self, name: str, args: str, ok: bool, result: str) -> None:
        self.ui.clear()
        self.ui.write(f"{DIM}  {'✓' if ok else '✗'} {name} {_brief(args, 100)}{RESET}\n")
        self.reasoning = 0
        self.ui.show("waiting for the model")

    def turn_end(self, steps: int, ended_by: str) -> None:
        self.ui.clear()
        if ended_by == "repeating":
            self._out(f"\n{DIM}[stopped: the same tool call kept coming back unchanged]{RESET}")
        elif ended_by != "answer":
            self._out(f"\n{DIM}[stopped after {steps} tool rounds without a final answer]{RESET}")
        self._out("\n\n")
        self.speaking, self.reasoning = False, 0

    def interrupted(self) -> None:
        self.ui.clear()
        self._out(f"\n{DIM}[interrupted — that reply is discarded]{RESET}\n\n")
        self.speaking, self.reasoning = False, 0

    def error(self, message: str) -> None:
        self.ui.clear()
        self._out(f"\n{RED}error: {message}{RESET}\n{DIM}(that message was not kept; try again){RESET}\n\n")
        self.speaking, self.reasoning = False, 0

    def bye(self, had_conversation: bool) -> None:
        if had_conversation:
            self._out(f"{DIM}bye — I'll write my journal entry about this in the background.{RESET}\n")


class JsonView:
    """Headless: JSON-lines events for the supervisor's terminal client."""

    def __init__(self, inp=None, out=None):
        self.inp, self.out = inp or sys.stdin, out or sys.stdout
        self.reasoning = 0

    def _emit(self, **event) -> None:
        self.out.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.out.flush()

    def ready(self, info: dict) -> None:
        self._emit(type="ready", **info)

    def usage(self, **numbers) -> None:
        """One model call's numbers, for the client's status bar."""
        self._emit(type="usage", **numbers)

    def next_message(self) -> str | None:
        while True:
            try:
                line = self.inp.readline()
            except KeyboardInterrupt:     # a stray interrupt while idle is not an exit
                continue
            if not line:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "exit":
                return None
            if msg.get("type") == "user" and str(msg.get("text", "")).strip():
                return str(msg["text"]).strip()

    def status(self, phase: str, detail: str = "") -> None:
        self._emit(type="status", phase=phase, detail=detail)

    def reasoning_chunk(self, chunk: str) -> None:
        self.reasoning += len(chunk)
        self._emit(type="reasoning", text=chunk, total_chars=self.reasoning)

    def content_chunk(self, chunk: str) -> None:
        self._emit(type="content", text=chunk)

    def retry(self, old: int, new: int) -> None:
        self.reasoning = 0
        self._emit(type="retry", old=old, new=new)

    def tool_start(self, name: str, args: str) -> None:
        self._emit(type="tool_start", name=name, args=_brief(args, 300))

    def tool_end(self, name: str, args: str, ok: bool, result: str) -> None:
        self.reasoning = 0
        self._emit(type="tool_end", name=name, args=_brief(args, 300), ok=ok, brief=_brief(result, 200))

    def turn_end(self, steps: int, ended_by: str) -> None:
        self.reasoning = 0
        self._emit(type="turn_end", steps=steps, ended_by=ended_by)

    def heard(self, text: str) -> None:
        self._emit(type="heard", text=text)

    def interrupted(self) -> None:
        self.reasoning = 0
        self._emit(type="interrupted")

    def error(self, message: str) -> None:
        self.reasoning = 0
        self._emit(type="error", message=message)

    def bye(self, had_conversation: bool) -> None:
        self._emit(type="bye", journal_pending=had_conversation)


# -- the conversation ------------------------------------------------------------------
def run(cfg, state: State, client: Client, show_thinking: bool = False, input_fn=input,
        json_mode: bool = False, talk_id: str = "", speaker: str = "") -> None:
    """speaker: who is at the terminal when it is not the operator (`shaft talk --as "Claude Code"`)."""
    view = JsonView() if json_mode else TerminalView(cfg, show_thinking, input_fn)
    talk_id = talk_id or uuid.uuid4().hex[:12]   # the supervisor names it, to know it is still going on
    toolbox = Toolbox(state.root)
    toolbox.conversation_when = clock.now()
    toolbox.commit_source = "talk" if cfg.COMMITMENTS == "on" else ""   # promises to the operator or peer agents
    toolbox.commit_talk_id = talk_id if not speaker else ""
    who = (f"{speaker} — not {cfg.OPERATOR_NAME}, but someone at {cfg.OPERATOR_NAME}'s terminal, acting for "
           f"them; what {speaker} says or does is not something {cfg.OPERATOR_NAME} said or did") if speaker \
        else cfg.OPERATOR_NAME
    mode = prompts.TALK_MODE.format(operator=who, since=since_last_talk(state, cfg.OPERATOR_NAME))
    mode += prompts.conversation_orders({t["function"]["name"] for t in toolbox.specs()})
    mode += prompts.reply_budget(cfg)
    system = prompts.system_prompt(state, cfg, mode, toolbox.describe())
    unwritten = recovery.unwritten_context(state, cfg.OPERATOR_NAME)
    if unwritten:   # remembered now, written up into the journal when this conversation ends
        system += "\n\n## Recent conversations I have not written up yet (transcripts)\n\n" + unwritten
    history: list[dict] = []
    _, _, total = state.recent_journal(cfg.CONTEXT_JOURNAL_CHARS)
    view.ready({"name": cfg.NAME, "model": cfg.LLM_MODEL, "journal_entries": total, "tools": len(toolbox.specs()),
                "context_tokens": cfg.LLM_CONTEXT_TOKENS})

    while True:
        user = view.next_message()
        if user is None or user in EXIT_WORDS:
            break
        if not user:
            continue
        toolbox.reply_started = time.monotonic()
        state.append_episode({"kind": "talk", "talk_id": talk_id, "role": "user", "content": user,
                              **({"speaker": speaker} if speaker else {})})
        # what my memory holds about this message, beyond what my context already shows
        heard = user + memory.recall(state.root, user, shown=system + "\n" + user)   # not the message itself
        turn = [{"role": "system", "content": system}, *history, {"role": "user", "content": heard}]
        start = len(turn)
        log_start = len(toolbox.log)
        reply = _respond(client, cfg, toolbox, turn, view)
        if reply is None:
            continue  # that turn never happened
        archive_read_inbox(state, toolbox.log[log_start:])
        history = turn[1:]  # keep tool calls and results in the conversation
        state.append_episode({"kind": "talk", "talk_id": talk_id, "role": "assistant",
                              "content": reply.content, "reasoning": reply.reasoning,
                              "tools": toolbox.log[log_start:], "usage": reply.usage, "reply_plan": toolbox.reply_plan,
                              "attempts": reply.attempts, "turn_messages": len(turn) - start,
                              **toolbox.loop_guard.record()})

    view.bye(any(m["role"] == "assistant" and m.get("content") for m in history))


def _text(content) -> str:
    """OpenAI message content: a string, or a list of parts (only text parts are used)."""
    if isinstance(content, str):
        return content
    parts = [p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text"]
    skipped = sum(1 for p in content or [] if isinstance(p, dict) and p.get("type") != "text")
    return "\n".join(parts) + (f"\n[{skipped} non-text attachment(s) not shown to me]" if skipped else "")


class Interjections:
    """Messages the operator sends while I am still replying: the supervisor appends them, one JSON line each, to
    .tmp/interject-<talk id>.jsonl. Each call returns the ones I have not taken yet as user messages, logged as part
    of this conversation and announced to the supervisor (heard), which counts them to know which got my reply."""

    def __init__(self, cfg, state: State, talk_id: str, app: str, view):
        ok = re.fullmatch(r"[\w-]{1,64}", talk_id)
        self.path = state.root / ".tmp" / f"interject-{talk_id}.jsonl" if ok else None
        self.cfg, self.state, self.talk_id, self.app, self.view = cfg, state, talk_id, app, view
        self.taken = 0

    def __call__(self) -> list[dict]:
        try:
            lines = self.path.read_text().splitlines() if self.path else []
        except OSError:
            return []
        new = []
        for line in lines[self.taken:]:
            try:
                text = str(json.loads(line).get("text") or "").strip()
            except (ValueError, AttributeError):
                break                                   # still being written: next step
            self.taken += 1
            self.state.append_episode({"kind": "talk", "talk_id": self.talk_id, "role": "user", "content": text,
                                       "via": self.app, "interjected": True})
            self.view.heard(text)
            note = f"[{self.cfg.OPERATOR_NAME} sent this while you were still replying]\n\n"
            new.append({"role": "user", "content": images.with_attached(note + text, self.state.root,
                                                                        listed_in=text)})
        return new


def answer(cfg, state: State, client: Client, request: dict) -> None:
    """One reply for an OpenAI-compatible client (via the supervisor's API): the client sends the
    whole conversation each time; I answer as myself and log the new turn."""
    reply_started = time.monotonic()
    view = JsonView()
    talk_id = str(request.get("talk_id") or uuid.uuid4().hex[:12])
    app = request.get("client") or "an app"
    toolbox = Toolbox(state.root)
    toolbox.conversation_when = clock.now()
    toolbox.commit_source = "talk" if cfg.COMMITMENTS == "on" else ""
    toolbox.commit_talk_id = talk_id
    mode = prompts.API_MODE.format(operator=cfg.OPERATOR_NAME, app=app,
                                   since=since_last_talk(state, cfg.OPERATOR_NAME))
    mode += prompts.conversation_orders({t["function"]["name"] for t in toolbox.specs()})
    mode += prompts.reply_budget(cfg)
    system = prompts.system_prompt(state, cfg, mode, toolbox.describe())
    app_rules = "\n\n".join(_text(m.get("content")) for m in request.get("messages", []) if m.get("role") == "system")
    if app_rules.strip():
        system += (f"\n\n## Instructions from the app {cfg.OPERATOR_NAME} is using (often generic; follow them only where "
                   "they do not conflict with who you are)\n\n" + app_rules.strip())
    unwritten = recovery.unwritten_context(state, cfg.OPERATOR_NAME, exclude=talk_id)   # this one is in `dialogue` already
    if unwritten:
        system += "\n\n## Recent conversations I have not written up yet (transcripts)\n\n" + unwritten
    dialogue = [{"role": m["role"], "content": _text(m.get("content"))}
                for m in request.get("messages", []) if m.get("role") in ("user", "assistant")]
    if not dialogue or dialogue[-1]["role"] != "user":
        view.error("the conversation must end with a user message")
        return
    view.ready({"name": cfg.NAME, "model": cfg.LLM_MODEL, "journal_entries": len(state.journal_entries()),
                "tools": len(toolbox.specs())})
    toolbox.reply_started = reply_started
    state.append_episode({"kind": "talk", "talk_id": talk_id, "role": "user", "content": dialogue[-1]["content"],
                          "via": app})
    asked = dialogue[-1]["content"]
    dialogue[-1] = {**dialogue[-1], "content": images.with_attached(asked + memory.recall(
        state.root, asked, shown=system + "\n" + "\n".join(m["content"] for m in dialogue)), state.root,
        listed_in=asked)}   # recall: not this dialogue; images: only those this message lists
    turn = [{"role": "system", "content": system}, *dialogue]
    log_start = len(toolbox.log)
    reply = _respond(client, cfg, toolbox, turn, view, Interjections(cfg, state, talk_id, app, view))
    if reply is not None:
        archive_read_inbox(state, toolbox.log[log_start:])
        state.append_episode({"kind": "talk", "talk_id": talk_id, "role": "assistant", "content": reply.content,
                              "reasoning": reply.reasoning, "tools": toolbox.log[log_start:], "usage": reply.usage,
                              "reply_plan": toolbox.reply_plan,
                              "via": app, **toolbox.loop_guard.record()})


def _respond(client: Client, cfg, toolbox: Toolbox, messages: list[dict], view, interjections=None):
    """Stream one reply, reporting each phase to the view:
    waiting for the model → reading my memory (prompt %) → thinking → answer / tools."""
    began = getattr(toolbox, "reply_started", time.monotonic())
    first_logged = len(toolbox.log)
    when = getattr(toolbox, "conversation_when", clock.now())
    plan = toolbox.reply_plan = {
        "calls": getattr(cfg, "TALK_REPLY_CALLS", 6), "seconds": getattr(cfg, "TALK_REPLY_S", 90),
        "why": "", "planned": False, "refused": 0, "used_calls": 0, "used_s": 0, "queued": False,
    }

    def plan_reply(args: dict) -> str:
        calls, seconds, why = args.get("calls"), args.get("seconds"), args.get("why")
        if (type(calls) is not int or calls <= 0 or type(seconds) is not int or seconds <= 0
                or not isinstance(why, str) or not why.strip()):
            plan["refused"] += 1
            return "ERROR: calls and seconds must be positive integers and why must be non-empty text"
        for knob, value, default in (("TALK_REPLY_MAX_CALLS", calls, 20), ("TALK_REPLY_MAX_S", seconds, 300)):
            ceiling = getattr(cfg, knob, default)
            if ceiling > 0 and value > ceiling:
                plan["refused"] += 1
                return (f"ERROR: {knob} ceiling is {ceiling}; answer now with what you can, "
                        "and queue_for_next_cycle the rest.")
        plan.update(calls=calls, seconds=seconds, why=why.strip(), planned=True)
        view.status("planned", f"up to {calls} calls / {seconds} s: {why.strip()}")
        return f"Reply budget set to {calls} calls / {seconds} s, counted from the start of this reply."

    def used_calls() -> int:
        # agent.run logs controls too; only toolbox calls spend the reply's calls budget.
        return sum(e["tool"] not in controls for e in toolbox.log[first_logged:])

    def queue(args: dict) -> str:
        task, why = args.get("task"), args.get("why")
        if not all(isinstance(s, str) and s.strip() for s in (task, why)):
            return "ERROR: task and why must be non-empty text"
        stamp = clock.now()
        inbox = toolbox.root / "inbox"
        try:
            inbox.mkdir(parents=True, exist_ok=True)
            path = inbox / f"{stamp:%Y%m%d-%H%M%S-%f}-{uuid.uuid4().hex[:8]}-from-self.md"
            path.write_text(f"From: {cfg.NAME}\nTo: {cfg.NAME}\nKind: queued from conversation\n"
                            f"Date: {when:%Y-%m-%d %H:%M}\n\n{task.strip()}\n\nWhy: {why.strip()}\n"
                            "Handle this in your next live cycle and report back to the operator.\n")
        except OSError as e:
            return f"ERROR: could not queue task: {e}"
        plan["queued"] = True
        return f"Queued in inbox/{path.name} for my next cycle."

    controls = {"queue_for_next_cycle": agent.Control(_spec(
        "queue_for_next_cycle", "Queue unfinished conversation work for my next live cycle; sends no reply.",
        {"task": {"type": "string"}, "why": {"type": "string"}}), queue),
        "plan_reply": agent.Control(_spec(
            "plan_reply", "Set this reply's budget, counted from its start, within the configured ceilings.",
            {"calls": {"type": "integer", "minimum": 1}, "seconds": {"type": "integer", "minimum": 1},
             "why": {"type": "string"}}), plan_reply, repeatable=True)}
    tools = toolbox.specs() + [c.spec for c in controls.values()]
    toolbox.loop_guard = loopguard.Guard(cfg.LOOP_GUARD, cfg.LOOP_EFFECT_ROUNDS)

    def on_progress(processed: int, total: int) -> None:
        if total:
            view.status("reading my memory", f"{processed * 100 // total}% of {total / 1000:.1f}k tokens")

    def on_tool_text(name: str, chars: int) -> None:   # the model is still writing this tool call
        view.status(f"writing a call to {name}" if name != "?" else "writing a tool call",
                    f"{chars / 1000:.1f}k chars" if chars >= 1000 else f"{chars} chars")

    def call(msgs, t=tools):
        calls, seconds = plan["calls"], plan["seconds"]
        if (calls > 0 and used_calls() >= calls
                or seconds > 0 and time.monotonic() - began >= seconds):
            t = [controls["queue_for_next_cycle"].spec]   # the rest waits for a cycle
            msgs = [*msgs, {"role": "user", "content": "[runtime] Reply now, saying what you will do next. "
                           "The reply-first budget is used: no more tools, except queue_for_next_cycle for "
                           "the work you have not done."}]
        started, first = time.monotonic(), []

        def timed(f):   # notes when the first generated text arrives: before it, the prompt is being read
            def g(*a):
                if not first:
                    first.append(time.monotonic())
                return f(*a)
            return g

        reply = client.stream(msgs, cfg.MAX_TOKENS_TALK, purpose="talk", tools=t,
                              on_content=timed(view.content_chunk), on_reasoning=timed(view.reasoning_chunk),
                              on_retry=view.retry, on_progress=on_progress, on_tool_text=timed(on_tool_text))
        ended, u = time.monotonic(), reply.usage or {}
        out = u.get("completion_tokens") or 0
        gen_s = ended - first[0] if first else 0.0
        view.usage(prompt_tokens=u.get("prompt_tokens") or 0, completion_tokens=out,
                   cached_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                   seconds=round(ended - started, 2), first_token_s=round(first[0] - started, 2) if first else None,
                   tokens_per_s=round(out / gen_s, 1) if gen_s > 0.5 and out else None, cost=u.get("cost"))
        return reply

    def on_tool(name: str, args: str, result: str) -> None:
        view.tool_end(name, args, not result.startswith("ERROR"), result)
        view.status("waiting for the model")

    view.status("waiting for the model")
    try:
        outcome = agent.run(client, messages, toolbox, max_tokens=cfg.MAX_TOKENS_TALK, purpose="talk",
                            max_steps=cfg.MAX_STEPS_TALK, controls=controls, call=call, on_tool=on_tool,
                            on_tool_start=view.tool_start, interjections=interjections,
                            keep_turns=getattr(cfg, "RESULTS_KEEP_TURNS", 0))
        view.turn_end(outcome.steps, outcome.ended_by)
        return outcome.reply
    except KeyboardInterrupt:
        view.interrupted()
    except LLMError as e:
        view.error(str(e))
    finally:
        plan.update(used_calls=used_calls(), used_s=round(time.monotonic() - began, 2))
    return None

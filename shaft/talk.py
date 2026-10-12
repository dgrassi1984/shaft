"""Conversation with the operator — with my tools available.

Two front ends share one conversation loop:
- the plain terminal (default; also what pipes and tests use);
- `--json`: a headless protocol for the supervisor's terminal client (bin/shaft talk), which
  renders Markdown, edits multi-line input and keeps history. Events go to stdout as JSON
  lines; messages arrive on stdin as JSON lines; SIGINT stops the reply in progress.

  in : {"type": "user", "text": "..."}  |  {"type": "exit"}
  out: ready, status{phase,detail}, reasoning{text}, content{text}, tool_start{name,args},
       tool_end{name,ok,brief}, retry{old,new}, turn_end{steps,ended_by}, interrupted, error{message}, bye
"""
from __future__ import annotations

import json
import re
import sys
import uuid

from . import agent, prompts, recovery
from .llm import Client, LLMError
from .state import State
from .tools import Toolbox
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

    def next_message(self) -> str | None:
        try:
            return self.input_fn(f"{CYAN}you ›{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            self._out("\n")
            return None

    def status(self, phase: str, detail: str = "") -> None:
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
        if ended_by != "answer":
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
        json_mode: bool = False) -> None:
    view = JsonView() if json_mode else TerminalView(cfg, show_thinking, input_fn)
    talk_id = uuid.uuid4().hex[:12]
    toolbox = Toolbox(state.root)
    mode = prompts.TALK_MODE.format(operator=cfg.OPERATOR_NAME, since=since_last_talk(state, cfg.OPERATOR_NAME))
    system = prompts.system_prompt(state, cfg, mode, toolbox.describe())
    unwritten = recovery.unwritten_context(state)
    if unwritten:   # remembered now, written up into the journal when this conversation ends
        system += "\n\n## Recent conversations I have not written up yet (transcripts)\n\n" + unwritten
    history: list[dict] = []
    _, _, total = state.recent_journal(cfg.CONTEXT_JOURNAL_CHARS)
    view.ready({"name": cfg.NAME, "model": cfg.LLM_MODEL, "journal_entries": total, "tools": len(toolbox.specs())})

    while True:
        user = view.next_message()
        if user is None or user in EXIT_WORDS:
            break
        if not user:
            continue
        state.append_episode({"kind": "talk", "talk_id": talk_id, "role": "user", "content": user})
        turn = [{"role": "system", "content": system}, *history, {"role": "user", "content": user}]
        start = len(turn)
        log_start = len(toolbox.log)
        reply = _respond(client, cfg, toolbox, turn, view)
        if reply is None:
            continue  # that turn never happened
        history = turn[1:]  # keep tool calls and results in the conversation
        state.append_episode({"kind": "talk", "talk_id": talk_id, "role": "assistant",
                              "content": reply.content, "reasoning": reply.reasoning,
                              "tools": toolbox.log[log_start:], "usage": reply.usage,
                              "attempts": reply.attempts, "turn_messages": len(turn) - start})

    view.bye(any(m["role"] == "assistant" and m.get("content") for m in history))


def _text(content) -> str:
    """OpenAI message content: a string, or a list of parts (only text parts are used)."""
    if isinstance(content, str):
        return content
    parts = [p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text"]
    skipped = sum(1 for p in content or [] if isinstance(p, dict) and p.get("type") != "text")
    return "\n".join(parts) + (f"\n[{skipped} non-text attachment(s) not shown to me]" if skipped else "")


def answer(cfg, state: State, client: Client, request: dict) -> None:
    """One reply for an OpenAI-compatible client (via the supervisor's API): the client sends the
    whole conversation each time; I answer as myself and log the new turn."""
    view = JsonView()
    talk_id = str(request.get("talk_id") or uuid.uuid4().hex[:12])
    app = request.get("client") or "an app"
    toolbox = Toolbox(state.root)
    mode = prompts.API_MODE.format(operator=cfg.OPERATOR_NAME, app=app,
                                   since=since_last_talk(state, cfg.OPERATOR_NAME))
    system = prompts.system_prompt(state, cfg, mode, toolbox.describe())
    app_rules = "\n\n".join(_text(m.get("content")) for m in request.get("messages", []) if m.get("role") == "system")
    if app_rules.strip():
        system += ("\n\n## Instructions from the app Daniele is using (often generic; follow them only where "
                   "they do not conflict with who you are)\n\n" + app_rules.strip())
    unwritten = recovery.unwritten_context(state)
    if unwritten:
        system += "\n\n## Recent conversations I have not written up yet (transcripts)\n\n" + unwritten
    dialogue = [{"role": m["role"], "content": _text(m.get("content"))}
                for m in request.get("messages", []) if m.get("role") in ("user", "assistant")]
    if not dialogue or dialogue[-1]["role"] != "user":
        view.error("the conversation must end with a user message")
        return
    view.ready({"name": cfg.NAME, "model": cfg.LLM_MODEL, "journal_entries": len(state.journal_entries()),
                "tools": len(toolbox.specs())})
    state.append_episode({"kind": "talk", "talk_id": talk_id, "role": "user", "content": dialogue[-1]["content"],
                          "via": app})
    turn = [{"role": "system", "content": system}, *dialogue]
    log_start = len(toolbox.log)
    reply = _respond(client, cfg, toolbox, turn, view)
    if reply is not None:
        state.append_episode({"kind": "talk", "talk_id": talk_id, "role": "assistant", "content": reply.content,
                              "reasoning": reply.reasoning, "tools": toolbox.log[log_start:], "usage": reply.usage,
                              "via": app})


def _respond(client: Client, cfg, toolbox: Toolbox, messages: list[dict], view):
    """Stream one reply, reporting each phase to the view:
    waiting for the model → reading my memory (prompt %) → thinking → answer / tools."""
    tools = toolbox.specs()

    def on_progress(processed: int, total: int) -> None:
        if total:
            view.status("reading my memory", f"{processed * 100 // total}% of {total / 1000:.1f}k tokens")

    def call(msgs, t=tools):
        return client.stream(msgs, cfg.MAX_TOKENS_TALK, purpose="talk", tools=t,
                             on_content=view.content_chunk, on_reasoning=view.reasoning_chunk,
                             on_retry=view.retry, on_progress=on_progress)

    def on_tool(name: str, args: str, result: str) -> None:
        view.tool_end(name, args, not result.startswith("ERROR"), result)
        view.status("waiting for the model")

    view.status("waiting for the model")
    try:
        outcome = agent.run(client, messages, toolbox, max_tokens=cfg.MAX_TOKENS_TALK, purpose="talk",
                            max_steps=cfg.MAX_STEPS_TALK, call=call, on_tool=on_tool,
                            on_tool_start=view.tool_start)
        view.turn_end(outcome.steps, outcome.ended_by)
        return outcome.reply
    except KeyboardInterrupt:
        view.interrupted()
    except LLMError as e:
        view.error(str(e))
    return None

"""Interactive terminal conversation with the operator — with my tools available."""
from __future__ import annotations

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


def _out(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


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


def run(cfg, state: State, client: Client, show_thinking: bool = False, input_fn=input) -> None:
    try:
        import readline  # noqa: F401  (line editing for input())
    except ImportError:
        pass

    talk_id = uuid.uuid4().hex[:12]
    toolbox = Toolbox(state.root)
    mode = prompts.TALK_MODE.format(operator=cfg.OPERATOR_NAME, since=since_last_talk(state, cfg.OPERATOR_NAME))
    system = prompts.system_prompt(state, cfg, mode, toolbox.describe())
    unwritten = recovery.unwritten_context(state)
    if unwritten:   # remembered now, written up into the journal when this conversation ends
        system += "\n\n## Recent conversations I have not written up yet (transcripts)\n\n" + unwritten
    history: list[dict] = []
    _, _, total = state.recent_journal(cfg.CONTEXT_JOURNAL_CHARS)
    _out(f"{DIM}{cfg.NAME} · {cfg.LLM_MODEL} · {total} journal entries · "
         f"{len(toolbox.specs())} tools · /exit or Ctrl-D to end{RESET}\n\n")

    while True:
        try:
            user = input_fn(f"{CYAN}you ›{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            _out("\n")
            break
        if not user:
            continue
        if user in EXIT_WORDS:
            break

        state.append_episode({"kind": "talk", "talk_id": talk_id, "role": "user", "content": user})
        turn = [{"role": "system", "content": system}, *history, {"role": "user", "content": user}]
        start = len(turn)
        log_start = len(toolbox.log)
        reply = _respond(client, cfg, toolbox, turn, show_thinking)
        if reply is None:
            continue  # that turn never happened
        history = turn[1:]  # keep tool calls and results in the conversation
        state.append_episode({"kind": "talk", "talk_id": talk_id, "role": "assistant",
                              "content": reply.content, "reasoning": reply.reasoning,
                              "tools": toolbox.log[log_start:], "usage": reply.usage,
                              "attempts": reply.attempts, "turn_messages": len(turn) - start})

    if any(m["role"] == "assistant" and m.get("content") for m in history):
        _summarise(client, cfg, state, system, history, talk_id)
    if recovery.orphan_talks(state):
        try:
            recovery.run(cfg, state, client)
        except KeyboardInterrupt:
            _out(f"{DIM}(the remaining write-ups will happen in my next cycle){RESET}\n")


def _brief(args: str, n: int) -> str:
    return re.sub(r"\s+", " ", args)[:n]


def _respond(client: Client, cfg, toolbox: Toolbox, messages: list[dict], show_thinking: bool):
    """Stream one reply, keeping a live status line until words appear:
    waiting for the model → reading my memory (prompt %) → thinking (~tokens) → answer."""
    ui = Status()
    st = {"speaking": False, "reasoning": 0}
    tools = toolbox.specs()

    def on_progress(processed: int, total: int) -> None:
        if total:
            ui.show("reading my memory", f"{processed * 100 // total}% of {total / 1000:.1f}k tokens")

    def on_reasoning(chunk: str) -> None:
        st["reasoning"] += len(chunk)
        if show_thinking:
            ui.clear()
            ui.write(f"{DIM}{chunk}{RESET}")
        else:
            ui.show("thinking", f"~{st['reasoning'] / 4000:.1f}k tokens")

    def on_content(chunk: str) -> None:
        if not st["speaking"]:
            ui.clear()
            ui.write(("\n" if show_thinking and st["reasoning"] else "") + f"{cfg.NAME} › ")
            st["speaking"] = True
        ui.write(chunk)

    def on_retry(old: int, new: int) -> None:
        ui.clear()
        ui.write(f"\n{DIM}[reply cut off at {old} tokens — asking again with {new}]{RESET}\n")
        st["speaking"], st["reasoning"] = False, 0
        ui.show("waiting for the model")

    def on_tool_start(name: str, args: str) -> None:
        if st["speaking"]:
            ui.write("\n")
        st["speaking"] = False
        ui.show(f"running {name}", _brief(args, 60))

    def on_tool(name: str, args: str, result: str) -> None:
        ui.clear()
        ok = "✗" if result.startswith("ERROR") else "✓"
        ui.write(f"{DIM}  {ok} {name} {_brief(args, 100)}{RESET}\n")
        st["reasoning"] = 0
        ui.show("waiting for the model")

    def call(msgs, t=tools):
        return client.stream(msgs, cfg.MAX_TOKENS_TALK, purpose="talk", tools=t, on_content=on_content,
                             on_reasoning=on_reasoning, on_retry=on_retry, on_progress=on_progress)

    ui.show("waiting for the model")
    try:
        outcome = agent.run(client, messages, toolbox, max_tokens=cfg.MAX_TOKENS_TALK, purpose="talk",
                            max_steps=cfg.MAX_STEPS_TALK, call=call, on_tool=on_tool,
                            on_tool_start=on_tool_start)
        ui.clear()
        if outcome.ended_by != "answer":
            _out(f"\n{DIM}[stopped after {outcome.steps} tool rounds without a final answer]{RESET}")
        _out("\n\n")
        return outcome.reply
    except KeyboardInterrupt:
        ui.clear()
        _out(f"\n{DIM}[interrupted — that reply is discarded]{RESET}\n\n")
    except LLMError as e:
        ui.clear()
        _out(f"\n{RED}error: {e}{RESET}\n{DIM}(that message was not kept; try again){RESET}\n\n")
    return None


def _summarise(client: Client, cfg, state: State, system: str, history: list[dict], talk_id: str) -> None:
    ui = Status()
    _out(f"{DIM}writing my journal entry about this conversation (Ctrl-C to skip — I'll write it next time){RESET}\n")
    ui.show("writing")
    # The summary needs the dialogue, not raw tool payloads.
    dialogue = [m for m in history if m["role"] in ("user", "assistant") and m.get("content")]
    messages = [{"role": "system", "content": system}, *dialogue,
                {"role": "user", "content": prompts.TALK_SUMMARY.format(operator=cfg.OPERATOR_NAME)}]
    try:
        reply = client.complete(messages, cfg.MAX_TOKENS_REFLECT, purpose="talk_summary")
        ui.clear()
    except (LLMError, KeyboardInterrupt) as e:
        ui.clear()
        state.append_episode({"kind": "talk_summary_failed", "talk_id": talk_id, "error": str(e)})
        _out(f"{RED}journal entry NOT written: {e or 'interrupted'}{RESET}\n"
             f"{DIM}the full transcript is saved in memory/episodes/ (talk {talk_id}){RESET}\n")
        return
    state.append_journal(f"Conversation with {cfg.OPERATOR_NAME}", reply.content)
    state.append_episode({"kind": "talk_summary", "talk_id": talk_id, "content": reply.content,
                          "usage": reply.usage})
    _out(f"{DIM}saved.{RESET}\n")

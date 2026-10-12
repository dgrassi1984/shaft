"""Interactive terminal conversation with the operator."""
from __future__ import annotations

import sys
import uuid

from . import prompts
from .llm import Client, LLMError
from .state import State

DIM, RED, CYAN, RESET = "\033[2m", "\033[31m", "\033[36m", "\033[0m"
EXIT_WORDS = {"/exit", "/quit", "/bye"}


def _out(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def run(cfg, state: State, client: Client, show_thinking: bool = False, input_fn=input) -> None:
    try:
        import readline  # noqa: F401  (line editing for input())
    except ImportError:
        pass

    talk_id = uuid.uuid4().hex[:12]
    system = prompts.system_prompt(state, cfg, prompts.TALK_MODE.format(operator=cfg.OPERATOR_NAME))
    history: list[dict] = []
    _, _, total = state.recent_journal(cfg.CONTEXT_JOURNAL_CHARS)
    _out(f"{DIM}{cfg.NAME} · {cfg.LLM_MODEL} · {total} journal entries · /exit or Ctrl-D to end{RESET}\n\n")

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

        history.append({"role": "user", "content": user})
        state.append_episode({"kind": "talk", "talk_id": talk_id, "role": "user", "content": user})
        reply = _respond(client, cfg, [{"role": "system", "content": system}, *history], show_thinking)
        if reply is None:
            history.pop()  # keep the conversation consistent: that turn never happened
            continue
        history.append({"role": "assistant", "content": reply.content})
        state.append_episode({"kind": "talk", "talk_id": talk_id, "role": "assistant",
                              "content": reply.content, "reasoning": reply.reasoning,
                              "usage": reply.usage, "attempts": reply.attempts})

    if any(m["role"] == "assistant" for m in history):
        _summarise(client, cfg, state, system, history, talk_id)


def _respond(client: Client, cfg, messages: list[dict], show_thinking: bool):
    status = {"thinking": False, "speaking": False}

    def on_reasoning(chunk: str) -> None:
        if show_thinking:
            _out(f"{DIM}{chunk}{RESET}")
        elif not status["thinking"]:
            _out(f"{DIM}(thinking…){RESET}")
        status["thinking"] = True

    def on_content(chunk: str) -> None:
        if not status["speaking"]:
            _out("\n" if show_thinking and status["thinking"] else "\r\033[K")
            _out(f"{cfg.NAME} › ")
            status["speaking"] = True
        _out(chunk)

    def on_retry(old: int, new: int) -> None:
        _out(f"\n{DIM}[reply cut off at {old} tokens — asking again with {new}]{RESET}\n")
        status["thinking"] = status["speaking"] = False

    try:
        reply = client.stream(messages, cfg.MAX_TOKENS_TALK, purpose="talk",
                              on_content=on_content, on_reasoning=on_reasoning, on_retry=on_retry)
        _out("\n\n")
        return reply
    except KeyboardInterrupt:
        _out(f"\n{DIM}[interrupted — that reply is discarded]{RESET}\n\n")
    except LLMError as e:
        _out(f"\n{RED}error: {e}{RESET}\n{DIM}(that message was not kept; try again){RESET}\n\n")
    return None


def _summarise(client: Client, cfg, state: State, system: str, history: list[dict], talk_id: str) -> None:
    _out(f"{DIM}writing my journal entry about this conversation…{RESET}\n")
    messages = [{"role": "system", "content": system}, *history,
                {"role": "user", "content": prompts.TALK_SUMMARY.format(operator=cfg.OPERATOR_NAME)}]
    try:
        reply = client.complete(messages, cfg.MAX_TOKENS_REFLECT, purpose="talk_summary")
    except (LLMError, KeyboardInterrupt) as e:
        state.append_episode({"kind": "talk_summary_failed", "talk_id": talk_id, "error": str(e)})
        _out(f"{RED}journal entry NOT written: {e or 'interrupted'}{RESET}\n"
             f"{DIM}the full transcript is saved in memory/episodes/ (talk {talk_id}){RESET}\n")
        return
    state.append_journal(f"Conversation with {cfg.OPERATOR_NAME}", reply.content)
    state.append_episode({"kind": "talk_summary", "talk_id": talk_id, "content": reply.content,
                          "usage": reply.usage})
    _out(f"{DIM}saved.{RESET}\n")

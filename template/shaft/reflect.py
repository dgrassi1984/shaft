"""Reflection cycle: read my state, think, write a journal entry, maybe edit my self-model."""
from __future__ import annotations

from . import prompts, selfedit
from .llm import Client
from .state import State


def run(cfg, state: State, client: Client) -> str:
    messages = [
        {"role": "system", "content": prompts.system_prompt(state, cfg, prompts.REFLECT_MODE)},
        {"role": "user", "content": prompts.REFLECT},
    ]
    # Raises on truncation / empty answer / transport error — nothing is written then.
    reply = client.complete(messages, cfg.MAX_TOKENS_REFLECT, purpose="reflect")
    edit = selfedit.parse(reply.content)
    if not edit.body:
        raise ValueError("reflection contained only a self-model block and no reflection text")

    notes = []
    if edit.new_self is not None:
        state.write_self_model(edit.new_self)
        notes.append("*I rewrote my self-model in this cycle.*")
    elif edit.rejected:
        notes.append(f"*I tried to rewrite my self-model but the edit was refused: {edit.rejected}.*")
    journal_body = edit.body + ("\n\n" + "\n".join(notes) if notes else "")
    state.append_journal("Reflection", journal_body)
    state.append_episode({
        "kind": "reflect",
        "content": reply.content,
        "reasoning": reply.reasoning,
        "self_model_changed": edit.new_self is not None,
        "self_model_rejected": edit.rejected,
        "usage": reply.usage,
        "attempts": reply.attempts,
    })
    return journal_body

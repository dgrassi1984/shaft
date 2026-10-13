"""The talk client's status line: context used, speed, totals — from the agent's per-call `usage` events.

Kept apart from talk_ui (which needs prompt_toolkit and rich) so it can be tested with the system Python.
"""
from __future__ import annotations

from dataclasses import dataclass


def _k(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n:.0f}"


@dataclass
class Stats:
    window: int = 0          # the model's context window, from the agent's config (0 = unknown)
    context: int = 0         # prompt + reply tokens of the latest call: what the next call starts from
    calls: int = 0
    tokens_out: int = 0
    gen_tokens: int = 0      # tokens of calls with a measured generation rate (weights the average)
    gen_seconds: float = 0.0
    last_tps: float | None = None
    cost: float = 0.0

    def add(self, ev: dict) -> None:
        out = int(ev.get("completion_tokens") or 0)
        self.calls += 1
        self.tokens_out += out
        self.context = int(ev.get("prompt_tokens") or 0) + out
        tps = ev.get("tokens_per_s")
        if tps:
            self.last_tps = float(tps)
            self.gen_tokens += out
            self.gen_seconds += out / float(tps)
        self.cost += float(ev.get("cost") or 0)

    def line(self, name: str, model: str, show_thinking: bool) -> str:
        parts = [name, model.split("/")[-1]]
        if self.context:
            used = f"ctx {_k(self.context)}"
            if self.window:
                used += f"/{self.window / 1000:.0f}k ({self.context * 100 / self.window:.0f}%)"
            parts.append(used)
        if self.last_tps:
            avg = self.gen_tokens / self.gen_seconds if self.gen_seconds else self.last_tps
            parts.append(f"{self.last_tps:.1f} tok/s (avg {avg:.1f})")
        if self.calls:
            parts.append(f"{self.calls} call{'s' if self.calls != 1 else ''} · {_k(self.tokens_out)} out")
        if self.cost:
            parts.append(f"${self.cost:.4f}" if self.cost < 0.01 else f"${self.cost:.2f}")
        parts.append(f"thinking {'shown' if show_thinking else 'hidden'}")
        return " · ".join(parts)

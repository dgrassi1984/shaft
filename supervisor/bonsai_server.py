"""SHAFT's own model server: Ternary-Bonsai-2 (MLX pack) behind mlx_lm's OpenAI-compatible server.

Replaces Atomic Chat's llama.cpp for the local brain. mlx_lm's server does the OpenAI protocol
(streaming, tool calls parsed from the chat template, reasoning, prompt cache); the only change is
the model provider: the pack's custom Packed layers are loaded with the same loader the
fine-tuning uses (bonsai_lora.load_text_model), optionally with a kept LoRA adapter. Whatever
model name a client asks for, it gets Bonsai.

Run with the Bonsai venv:  .venv-bonsai/bin/python -m supervisor.bonsai_server [--port 8780] [--adapter DIR]
Normally started by launchd (shaft model on).

Settings (environment): SHAFT_MODEL_TEMP (0.1), SHAFT_THINK_BUDGET (4096 reasoning tokens, then </think> is
forced; 0 = off), SHAFT_PROMPT_CACHE_GB (4). Presence penalty is sent per request by the agent.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import bonsai_lora

PORT = 8780
THINK_START, THINK_END = 248068, 248069   # <think>, </think> in Bonsai's tokenizer


class ThinkingBudget:
    """Force </think> after `budget` reasoning tokens, so every reply has a bounded latency.

    Bonsai 2 at 2 bits sometimes reasons in loops: 6,000+ tokens (9+ minutes) for "All good?".
    The first call sees the prompt; if it ends inside <think> (thinking on), generated tokens are
    counted until the model closes the block itself or the budget is reached."""

    def __init__(self, budget: int):
        self.budget, self.start, self.thinking = budget, None, False

    def __call__(self, tokens, logits):
        import mlx.core as mx
        seq = tokens.reshape(-1) if tokens.ndim > 1 else tokens
        n = seq.shape[0]
        if self.start is None:
            self.start = n
            tail = seq[-64:].tolist()
            opens = [i for i, t in enumerate(tail) if t == THINK_START]
            closes = [i for i, t in enumerate(tail) if t == THINK_END]
            self.thinking = bool(opens) and (not closes or opens[-1] > closes[-1])
            return logits
        if not self.thinking:
            return logits
        if int(seq[-1].item()) == THINK_END:
            self.thinking = False
            return logits
        if n - self.start >= self.budget:
            vocab = logits.shape[-1]
            forced = mx.where(mx.arange(vocab) == THINK_END, 0.0, -mx.inf).astype(logits.dtype)
            return mx.broadcast_to(forced, logits.shape)
        return logits


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="bonsai_server")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--adapter", help="a kept LoRA adapter directory (from shaft finetune)")
    args = ap.parse_args(argv)

    import mlx_lm.server as S
    from mlx_lm.models.cache import make_prompt_cache
    from mlx_lm.utils import load_tokenizer

    pack = bonsai_lora.PACK
    gen = json.loads((pack / "generation_config.json").read_text())

    class BonsaiProvider(S.ModelProvider):
        def load(self, model_path, adapter_path=None, draft_model_path=None):
            if self.model is None:
                model = bonsai_lora.load_text_model()
                if args.adapter:
                    bonsai_lora.load_adapter(model, args.adapter)
                self.tokenizer = load_tokenizer(pack, eos_token_ids=gen.get("eos_token_id"))
                self.model = model
                self.draft_model = None
                self.model_key = ("ternary-bonsai-2-27b", args.adapter, None)
                self.is_batchable = all(hasattr(c, "merge") for c in make_prompt_cache(model))
            return self.model, self.tokenizer

    S.ModelProvider = BonsaiProvider
    import os
    budget = int(os.environ.get("SHAFT_THINK_BUDGET", "4096"))   # Daniele: "not too low"
    original = S._make_logits_processors

    def with_budget(gen_args):
        return list(original(gen_args) or []) + ([ThinkingBudget(budget)] if budget > 0 else [])
    S._make_logits_processors = with_budget
    temp = os.environ.get("SHAFT_MODEL_TEMP", "0.1")                   # Daniele, 2026-10-12 (pack default 1.0)
    cache_gb = float(os.environ.get("SHAFT_PROMPT_CACHE_GB", "4"))      # the cache grew to 8.3 GB uncapped
    sys.argv = ["bonsai_server", "--model", str(pack), "--host", "127.0.0.1", "--port", str(args.port),
                "--temp", temp, "--top-p", str(gen.get("top_p", 0.95)),
                "--top-k", str(gen.get("top_k", 20)), "--max-tokens", "16384",
                "--prompt-cache-bytes", str(int(cache_gb * 1024 ** 3))]
    from .version import record_service   # `shaft talk` shows which code and settings are serving
    record_service("model", {"temp": float(temp), "think_budget": budget, "prompt_cache_gb": cache_gb,
                             "adapter": args.adapter, "port": args.port})
    S.main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""SHAFT's own model server: Ternary-Bonsai-2 (MLX pack) behind mlx_lm's OpenAI-compatible server.

Replaces Atomic Chat's llama.cpp for the local brain. mlx_lm's server does the OpenAI protocol
(streaming, tool calls parsed from the chat template, reasoning, prompt cache); the only change is
the model provider: the pack's custom Packed layers are loaded with the same loader the
fine-tuning uses (bonsai_lora.load_text_model), optionally with a kept LoRA adapter. Whatever
model name a client asks for, it gets Bonsai.

Run with the Bonsai venv:  .venv-bonsai/bin/python -m supervisor.bonsai_server [--port 8780] [--adapter DIR]
Normally started by launchd (shaft model on).

Settings (environment): SHAFT_MODEL_TEMP (0.6), SHAFT_THINK_BUDGET (4096 reasoning tokens, then </think> is
forced; 0 = off; a request may ask for less with chat_template_kwargs.thinking_budget), SHAFT_PROMPT_CACHE_GB (8,
trimmed by mlx_lm when the next request starts). Presence penalty is sent per request by the agent.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

from . import bonsai_lora

PORT = 8780
THINK_START, THINK_END = 248068, 248069   # <think>, </think> in Bonsai's tokenizer


WRAP_UP = "\n\nConsidering the limited time, I have to give the answer based on my thinking so far now.\n"


class ThinkingBudget:
    """Force the end of thinking after `budget` reasoning tokens, so every reply has a bounded latency.

    Bonsai 2 at 2 bits sometimes reasons in loops: 6,000+ tokens (9+ minutes) for "All good?".
    The first call sees the prompt; if it ends inside <think> (thinking on), generated tokens are
    counted until the model closes the block itself or the budget is reached. Then `wrap_up` tokens
    (Qwen's early-stopping phrase) are forced, then </think>: a bare </think> mid-thought made the
    model go on thinking in its answer and close the block a second time."""

    def __init__(self, budget: int, wrap_up: list[int] | None = None):
        self.budget, self.start, self.thinking = budget, None, False
        self.forced = list(wrap_up or []) + [THINK_END]
        self.closed = None   # on the GPU: has </think> been generated yet? Never read back (no sync per token)

    def __call__(self, tokens, logits):
        import mlx.core as mx
        seq = tokens.reshape(-1) if tokens.ndim > 1 else tokens
        n = seq.shape[0]
        if self.start is None:   # the prompt, once: is thinking open at the end of it?
            self.start = n
            tail = seq[-64:].tolist()
            opens = [i for i, t in enumerate(tail) if t == THINK_START]
            closes = [i for i, t in enumerate(tail) if t == THINK_END]
            self.thinking = bool(opens) and (not closes or opens[-1] > closes[-1])
            self.closed = mx.array(False)
            return logits
        if not self.thinking:
            return logits
        # Reading the last token back (`.item()`) every step forced a GPU sync per thinking token: ~7% of
        # decode (_bench/RESULTS.md). The "closed" flag stays a lazy GPU value instead.
        self.closed = mx.logical_or(self.closed, seq[-1] == THINK_END)
        mx.async_eval(self.closed)
        k = n - self.start - self.budget
        if k < 0 or k >= len(self.forced):
            return logits
        vocab = logits.shape[-1]
        forced = mx.where(mx.arange(vocab) == self.forced[k], 0.0, -mx.inf).astype(logits.dtype)
        return mx.where(self.closed, logits, mx.broadcast_to(forced, logits.shape))


TOOL_NAME = re.compile(r'<function=([\w.-]+)|"name"\s*:\s*"([\w.-]+)"')   # Qwen XML or JSON tool-call forms


def tool_progress_handler(S):
    """mlx_lm's handler, announcing a tool call while the model is still writing it.

    The server buffers a tool call until it is complete — a long write_file is a minute of silence, and the
    talk client went on showing "thinking". While the model is inside a tool call this sends an SSE comment,
    at most once a second: `: tool <name> <chars written>` (clients ignore comments they do not know)."""

    class Handler(S.APIHandler):
        def handle_completion(self, request, stop_words):
            real, handler = self.response_generator, self

            class Announcing:   # this request's view of the shared generator: generate() is wrapped, nothing else
                def __getattr__(self, name):
                    return getattr(real, name)

                def generate(self, *a, **k):
                    ctx, response = real.generate(*a, **k)
                    return ctx, handler._announce_tools(response)

            self.response_generator = Announcing()
            try:
                return super().handle_completion(request, stop_words)
            finally:
                self.response_generator = real

        def _announce_tools(self, response):
            text, last = "", 0.0
            for gen in response:
                if gen.state == "tool":
                    text += gen.text
                    now = time.monotonic()
                    if self.stream and now - last >= 1.0:
                        m = TOOL_NAME.search(text)
                        name = next((g for g in m.groups() if g), "") if m else ""
                        self.wfile.write(f": tool {name or '?'} {len(text)}\n\n".encode())
                        self.wfile.flush()
                        last = now
                else:
                    text = ""
                yield gen

    return Handler


def request_budget(gen_args, budget: int) -> int:
    """A request may ask for a smaller thinking budget (chat_template_kwargs.thinking_budget — the agent does
    after a tool result), never a larger one; 0 (off) stays off."""
    asked = (getattr(gen_args, "chat_template_kwargs", None) or {}).get("thinking_budget")
    if budget > 0 and isinstance(asked, int) and not isinstance(asked, bool) and asked > 0:
        return min(budget, asked)
    return budget


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
                model = bonsai_lora.load_text_model(float16_norms=True)
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

    wrap_ids = load_tokenizer(pack).encode(WRAP_UP, add_special_tokens=False)

    def with_budget(gen_args):
        b = request_budget(gen_args, budget)
        return list(original(gen_args) or []) + ([ThinkingBudget(b, wrap_ids)] if b > 0 else [])
    S._make_logits_processors = with_budget
    temp = os.environ.get("SHAFT_MODEL_TEMP", "0.6")   # Daniele, 2026-10-13 (0.1 = near-greedy: verbatim loops; pack 1.0)
    # mlx_lm trims the prompt cache to this when the next request starts — after that request has taken its
    # prefix. Enforcing it on every insert instead (2026-10-13 morning) evicted each new entry at once on
    # long agent prompts: from ~16k tokens every tool round re-read the whole prompt (42k tokens, ~2 min).
    # 8 GB holds the entries of a 40k-token conversation with float16 KV; the Mac has 64 GB.
    cache_gb = float(os.environ.get("SHAFT_PROMPT_CACHE_GB", "8"))
    cap = int(cache_gb * 1024 ** 3)
    handler, serve_http = tool_progress_handler(S), S._run_http_server
    S._run_http_server = lambda host, port, generator, **kw: serve_http(host, port, generator,
                                                                       **{**kw, "handler_class": handler})
    sys.argv = ["bonsai_server", "--model", str(pack), "--host", "127.0.0.1", "--port", str(args.port),
                "--temp", temp, "--top-p", str(gen.get("top_p", 0.95)),
                "--top-k", str(gen.get("top_k", 20)), "--max-tokens", "16384",
                "--prompt-cache-bytes", str(cap)]
    from .version import record_service   # `shaft talk` shows which code and settings are serving
    record_service("model", {"temp": float(temp), "think_budget": budget, "prompt_cache_gb": cache_gb,
                             "adapter": args.adapter, "port": args.port})
    S.main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

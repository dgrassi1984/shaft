"""SHAFT's own model server: Ternary-Bonsai-2 (MLX pack) behind mlx_lm's OpenAI-compatible server.

Replaces Atomic Chat's llama.cpp for the local brain. mlx_lm's server does the OpenAI protocol
(streaming, tool calls parsed from the chat template, reasoning, prompt cache); the only change is
the model provider: the pack's custom Packed layers are loaded with the same loader the
fine-tuning uses (bonsai_lora.load_text_model), optionally with a kept LoRA adapter. Whatever
model name a client asks for, it gets Bonsai.

Run with the Bonsai venv:  .venv-bonsai/bin/python -m supervisor.bonsai_server [--port 8780] [--adapter DIR]
Normally started by launchd (shaft model on).
"""
from __future__ import annotations

import argparse
import json
import sys

from . import bonsai_lora

PORT = 8780


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
    sys.argv = ["bonsai_server", "--model", str(pack), "--host", "127.0.0.1", "--port", str(args.port),
                "--temp", str(gen.get("temperature", 1.0)), "--top-p", str(gen.get("top_p", 0.95)),
                "--top-k", str(gen.get("top_k", 20)), "--max-tokens", "16384"]
    S.main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

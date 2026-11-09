"""LoRA on Ternary-Bonsai itself (MLX pack), so fine-tuning keeps Bonsai's speed and memory.

Bonsai's MLX pack replaces every linear layer of mlx_lm's Qwen3.5 model with a custom
`Packed` module (Hadamard transform of the input + 2-bit quantized matmul). mlx_lm's LoRA
only wraps nn.Linear / QuantizedLinear, so this module wraps Packed layers instead:

    y = Packed(x) + scale · B(A(x))       base frozen; only A and B train

Run with the Bonsai venv (`.venv-bonsai/bin/python -m supervisor.bonsai_lora spike`).
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

from .jsonl import jsonl_lines

PACK = Path(__file__).resolve().parent.parent / ".models" / "bonsai2-27b-mlx"
MAX_TRAIN_TOKENS = 384   # training-mode gated-delta keeps every step's state: 1024 tokens peaked at 61 GB


def _imports():
    sys.path.insert(0, str(PACK / "runtime"))
    import mlx.core as mx
    import mlx.nn as nn
    return mx, nn


def load_text_model(float16_norms: bool = False):
    """Bonsai's language model on mlx_lm's Qwen3.5 implementation (the training-capable one).

    Mirrors the pack's own vision_artifact loader — same Packed layers, same sign checks —
    but builds mlx_lm's TextModel and loads only the `language_model.*` weights. mlx-vlm's
    implementation is inference-optimised and uses Metal kernels that have no backward pass.

    float16_norms (serving and evaluation): the pack stores 353 norm weights as float32, and MLX promotes
    float16 x float32 to float32 — so after the first RMSNorm the residual stream, attention and the KV
    cache all ran in float32: decode 20 -> 17 tok/s from 0 to 16k context, prefill ~250 tok/s. Cast to
    float16 (A_log, dt_bias and the Hadamard signs stay float32): +12% decode at 0, +43% at 16k, prefill
    +30-50%, 5 GB less at 16k; mean NLL over 16,384 real tokens 1.0935 -> 1.0934, top-1 agreement 99.85%
    (_bench/RESULTS.md, 2026-10-13). Training keeps float32 norms: only inference was measured."""
    mx, nn = _imports()
    from mlx_lm.models.qwen3_5 import TextModel, TextModelArgs
    from runtime import Packed
    config = json.loads((PACK / "config.json").read_text())
    if config.get("model_type") != "prism_hadamard_qwen35" or config.get("base_model_type", "qwen3_5") != "qwen3_5":
        raise ValueError("unsupported pack")
    model = TextModel(TextModelArgs.from_dict(config["text_config"]))
    prefix = "language_model."
    weights = {k[len(prefix):]: v for k, v in mx.load(str(PACK / "model.safetensors")).items()
               if k.startswith(prefix)}
    for record in config["modules"]:
        path, block = record["path"], record["block"]
        if record["dtype"] != "float16" or (block and block not in (512, 1024, 2048, 4096)):
            raise ValueError(f"unsupported packed record {path}")
        arrays = [weights[f"{path}.{s}"] for s in ("weight", "scales", "biases")]
        signs = weights.get(f"{path}.signs")
        if block and (signs is None or not mx.all((signs == 1) | (signs == -1)).item()):
            raise ValueError(f"bad sign vector for {path}")
        parent = model
        parts = path.split(".")
        for part in parts[:-1]:
            parent = parent[int(part)] if part.isdigit() else getattr(parent, part)
        setattr(parent, parts[-1], Packed(arrays, block, signs, record["embedding"], mx.float16))
    model.load_weights(list(weights.items()), strict=True)
    if float16_norms:
        from mlx.utils import tree_flatten
        model.load_weights([(k, v.astype(mx.float16)) for k, v in tree_flatten(model.parameters())
                            if v.dtype == mx.float32 and not k.endswith(("A_log", "dt_bias", ".signs"))], strict=False)
    model.eval()
    mx.eval(model.parameters())
    return model


def load(float16_norms: bool = False):
    """(model, encode, decode, eos_ids) for text use of the pack."""
    mx, nn = _imports()
    from tokenizers import Tokenizer
    model = load_text_model(float16_norms)
    tok = Tokenizer.from_file(str(PACK / "tokenizer.json"))
    eos = {i for i in (tok.token_to_id("<|im_end|>"), tok.token_to_id("<|endoftext|>")) if i is not None}
    return model, (lambda s: tok.encode(s).ids), (lambda ids: tok.decode(ids)), eos


def logits_of(model, ids, cache=None):
    out = model(ids, cache=cache)
    return out.logits if hasattr(out, "logits") else out


def make_cache(model):
    if hasattr(model, "make_cache"):
        return model.make_cache()
    from mlx_lm.models.cache import make_prompt_cache
    return make_prompt_cache(model)


def render(messages: list[dict], add_generation_prompt: bool, thinking: bool = False) -> str:
    from jinja2.sandbox import ImmutableSandboxedEnvironment
    template = (PACK / "chat_template.jinja").read_text()
    return ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True).from_string(template).render(
        messages=messages, add_generation_prompt=add_generation_prompt, enable_thinking=thinking)


def make_lora_class():
    mx, nn = _imports()

    class LoRAPacked(nn.Module):
        def __init__(self, base, rank: int = 8, alpha: float = 16.0):
            super().__init__()
            self.base = base
            in_dims = base.weight.shape[1] * 16          # 2-bit packed: 16 values per uint32
            out_dims = base.weight.shape[0]
            bound = 1 / math.sqrt(in_dims)
            # float32: Adam's update overflowed float16 LoRA weights to NaN after one step
            self.lora_a = mx.random.uniform(-bound, bound, (in_dims, rank)).astype(mx.float32)
            self.lora_b = mx.zeros((rank, out_dims), dtype=mx.float32)
            self.scale = alpha / rank

        def __call__(self, x):
            delta = (x.astype(mx.float32) @ self.lora_a) @ self.lora_b
            return self.base(x) + (self.scale * delta).astype(x.dtype)

    return LoRAPacked


def attach_lora(model, last_layers: int = 8, rank: int = 8) -> int:
    from runtime import Packed
    LoRAPacked = make_lora_class()
    model.freeze()
    layers = model.model.layers if hasattr(model, "model") else model.layers
    wrapped = 0
    for layer in layers[-last_layers:]:
        for name, module in list(layer.named_modules()):
            if isinstance(module, Packed) and not module.embedding:
                parent = layer
                parts = name.split(".")
                for p in parts[:-1]:
                    parent = parent[int(p)] if p.isdigit() else getattr(parent, p)
                lora = LoRAPacked(module, rank=rank)
                setattr(parent, parts[-1], lora)
                lora.freeze(keys=["base"], recurse=True)
                lora.unfreeze(keys=["lora_a", "lora_b"], recurse=False)
                wrapped += 1
    return wrapped


def spike(example_path: str | None = None) -> int:
    mx, nn = _imports()
    import mlx.optimizers as optim
    from mlx.utils import tree_flatten

    t0 = time.time()
    model, encode, decode, eos = load(float16_norms=True)   # measured as served
    print(f"loaded in {time.time() - t0:.1f}s; active memory {mx.get_active_memory() / 1e9:.1f} GB", flush=True)

    # 1. speed: prompt processing and generation, greedy, no thinking
    prompt = render([{"role": "user", "content": "Explain in three sentences why the sky is blue."}], True)
    ids = mx.array([encode(prompt)])
    cache = make_cache(model)
    t = time.time()
    logits = logits_of(model, ids, cache)
    mx.eval(logits)
    prefill = ids.shape[1] / (time.time() - t)
    out, t = [], time.time()
    tok = mx.argmax(logits[:, -1, :], axis=-1)
    for _ in range(120):
        out.append(tok.item())
        if out[-1] in eos:
            break
        logits = logits_of(model, tok[None], cache)
        tok = mx.argmax(logits[:, -1, :], axis=-1)
    gen = len(out) / (time.time() - t)
    print(f"speed: prompt {prefill:.0f} tok/s ({ids.shape[1]} tokens), generation {gen:.1f} tok/s")
    print("sample:", decode(out)[:300].replace("\n", " "))

    # 2. LoRA attaches
    n = attach_lora(model)
    trainable = sum(v.size for _, v in tree_flatten(model.trainable_parameters()))
    print(f"LoRA wrapped {n} Packed layers; trainable parameters {trainable / 1e6:.2f}M")

    # 3. it trains: loss on one real conversation must fall
    if example_path:
        rec = json.loads(jsonl_lines(Path(example_path).read_text())[0])
        text = render(rec["messages"], False)
    else:
        text = render([{"role": "user", "content": "Who are you?"},
                       {"role": "assistant", "content": "I am shaft, an agent that keeps a journal."}], False)
    toks = mx.array([encode(text)[:MAX_TRAIN_TOKENS]])

    def loss_fn(m):
        logits = logits_of(m, toks[:, :-1]).astype(mx.float32)
        return nn.losses.cross_entropy(logits, toks[:, 1:]).mean()

    model.train()   # Qwen3.5's gated-delta layers use their differentiable ops path only in training mode
    from mlx_lm.tuner.trainer import grad_checkpoint
    for layer_type in {type(l) for l in model.layers}:   # recompute activations in backward, don't store them
        grad_checkpoint(next(l for l in model.layers if type(l) is layer_type))
    mx.reset_peak_memory()
    opt = optim.Adam(learning_rate=1e-4)
    step = nn.value_and_grad(model, loss_fn)
    losses = []
    for i in range(10):
        t = time.time()
        loss, grads = step(model)
        gnorm = sum(mx.sum(g.astype(mx.float32) ** 2).item() for _, g in tree_flatten(grads)) ** 0.5
        opt.update(model, grads)
        mx.eval(model.parameters(), opt.state)
        losses.append(loss.item())
        print(f"step {i + 1}: loss {losses[-1]:.4f}  grad-norm {gnorm:.4f}  {time.time() - t:.1f}s  "
              f"peak memory {mx.get_peak_memory() / 1e9:.1f} GB", flush=True)
    ok = losses[-1] < losses[0] and gnorm > 0
    print("RESULT:", "LoRA on Bonsai trains" if ok else "LoRA on Bonsai does NOT train")
    return 0 if ok else 1


def _example_tokens(messages: list[dict], encode) -> tuple[list[int], list[int]]:
    """Token ids of the dialogue (no system prompt) and a 0/1 mask: 1 only on the agent's own words."""
    msgs = [m for m in messages if m["role"] != "system"]
    ids, mask = [], []
    for i, m in enumerate(msgs):
        before = encode(render(msgs[:i], add_generation_prompt=m["role"] == "assistant")) if i else []
        upto = encode(render(msgs[: i + 1], add_generation_prompt=False))
        new = upto[len(before):] if upto[: len(before)] == before else upto[len(ids):]
        ids += new
        mask += [1 if m["role"] == "assistant" else 0] * len(new)
    return ids[-MAX_TRAIN_TOKENS:], mask[-MAX_TRAIN_TOKENS:]


def train(data_dir: str, out_dir: str, iters: int, lr: float = 2e-5, rank: int = 8, last_layers: int = 8) -> int:
    mx, nn = _imports()
    import random
    import mlx.optimizers as optim
    from mlx.utils import tree_flatten
    from mlx_lm.tuner.trainer import grad_checkpoint

    model, encode, _, _ = load()
    attach_lora(model, last_layers=last_layers, rank=rank)
    model.train()
    for layer_type in {type(l) for l in model.layers}:
        grad_checkpoint(next(l for l in model.layers if type(l) is layer_type))

    def read(name):
        rows = [json.loads(l) for l in jsonl_lines((Path(data_dir) / name).read_text()) if l.strip()]
        return [t for t in (_example_tokens(r["messages"], encode) for r in rows) if sum(t[1]) > 0]
    train_set, valid_set = read("train.jsonl"), read("valid.jsonl")
    print(f"{len(train_set)} training / {len(valid_set)} validation examples", flush=True)

    def loss_fn(m, ids, mask):
        logits = logits_of(m, ids[:, :-1]).astype(mx.float32)
        ce = nn.losses.cross_entropy(logits, ids[:, 1:])
        weight = mask[:, 1:].astype(mx.float32)
        return (ce * weight).sum() / mx.maximum(weight.sum(), 1)

    def batch(t):
        return mx.array([t[0]]), mx.array([t[1]])

    def validate():
        losses = [loss_fn(model, *batch(t)).item() for t in valid_set] if valid_set else []
        return sum(losses) / len(losses) if losses else float("nan")

    opt = optim.Adam(learning_rate=lr)
    step = nn.value_and_grad(model, loss_fn)
    rng = random.Random(0)
    history = {"val": [(0, validate())], "train": []}
    print(f"iter 0: val {history['val'][-1][1]:.4f}", flush=True)
    t0 = time.time()
    for it in range(1, iters + 1):
        loss, grads = step(model, *batch(rng.choice(train_set)))
        if not math.isfinite(loss.item()):
            print(f"iter {it}: loss is not finite — stopping", flush=True)
            return 1
        opt.update(model, grads)
        mx.eval(model.parameters(), opt.state)
        history["train"].append((it, loss.item()))
        if it % max(1, iters // 5) == 0 or it == iters:
            history["val"].append((it, validate()))
            print(f"iter {it}: train {loss.item():.4f}  val {history['val'][-1][1]:.4f}  "
                  f"{(time.time() - t0) / it:.1f}s/iter  peak {mx.get_peak_memory() / 1e9:.1f} GB", flush=True)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    weights = {k: v for k, v in tree_flatten(model.trainable_parameters())}
    mx.save_safetensors(str(out / "adapters.safetensors"), weights)
    (out / "adapter_config.json").write_text(json.dumps({
        "base": "prism-ml/Ternary-Bonsai-2-27B-mlx-2bit", "kind": "lora-on-packed", "rank": rank, "alpha": 16.0,
        "last_layers": last_layers, "lr": lr, "iters": iters, "max_train_tokens": MAX_TRAIN_TOKENS,
        "examples": len(train_set), "history": history}, indent=1))
    print(f"saved {len(weights)} tensors to {out}", flush=True)
    return 0


def load_adapter(model, adapter_dir: str) -> None:
    mx, _ = _imports()
    cfg = json.loads((Path(adapter_dir) / "adapter_config.json").read_text())
    attach_lora(model, last_layers=cfg["last_layers"], rank=cfg["rank"])
    model.load_weights(list(mx.load(str(Path(adapter_dir) / "adapters.safetensors")).items()), strict=False)
    model.eval()


def generate(model, encode, decode, eos, messages: list[dict], max_tokens: int = 400, temp: float = 0.7) -> str:
    mx, _ = _imports()
    from mlx_lm.sample_utils import make_sampler
    sample = make_sampler(temp=temp, top_p=0.95)
    cache = make_cache(model)
    ids = mx.array([encode(render(messages, add_generation_prompt=True))])
    logits = logits_of(model, ids, cache)
    out = []
    tok = sample(logits[:, -1, :])
    for _ in range(max_tokens):
        out.append(tok.item())
        if out[-1] in eos:
            out.pop()
            break
        logits = logits_of(model, tok[None], cache)
        tok = sample(logits[:, -1, :])
    return decode(out).strip()


def answers(adapter_dir: str, system_file: str, questions_file: str, out_file: str) -> int:
    """Base Bonsai's answers, then the same questions with the adapter attached."""
    model, encode, decode, eos = load(float16_norms=True)   # judged as it will be served
    system = Path(system_file).read_text()
    questions = json.loads(Path(questions_file).read_text())
    ask = lambda q: generate(model, encode, decode, eos, [{"role": "system", "content": system},  # noqa: E731
                                                          {"role": "user", "content": q}])
    base = [ask(q) for q in questions]
    print("base answers done", flush=True)
    load_adapter(model, adapter_dir)
    tuned = [ask(q) for q in questions]
    Path(out_file).write_text(json.dumps({"base": base, "tuned": tuned}, ensure_ascii=False, indent=1))
    print("tuned answers done", flush=True)
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="bonsai_lora")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("spike")
    sp.add_argument("example", nargs="?")
    tp = sub.add_parser("train")
    tp.add_argument("--data", required=True)
    tp.add_argument("--out", required=True)
    tp.add_argument("--iters", type=int, default=100)
    tp.add_argument("--lr", type=float, default=2e-5)
    an = sub.add_parser("answers")
    an.add_argument("--adapter", required=True)
    an.add_argument("--system-file", required=True)
    an.add_argument("--questions-file", required=True)
    an.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.cmd == "spike":
        sys.exit(spike(a.example))
    if a.cmd == "train":
        sys.exit(train(a.data, a.out, a.iters, a.lr))
    sys.exit(answers(a.adapter, a.system_file, a.questions_file, a.out))

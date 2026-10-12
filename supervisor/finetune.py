"""M6 — personality into the weights: a LoRA adapter trained on an instance's own selected experiences.

  shaft dataset <name>    export its experiences as chat-format JSONL (train/valid split)
  shaft finetune <name>   train a LoRA adapter with MLX, then let the judge compare base vs adapter
                          on the distinctness probes; the adapter is kept only if it wins

Requirements (checked, never silently skipped): the `mlx_lm` package importable by this
Python, and SHAFT_FT_BASE_MODEL = an MLX-format base model (HF repo id or local path).
The adapter is trained on that base model; serving it requires running the instance on the
same base model (the current Atomic Chat model is a ternary GGUF that MLX cannot train).
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import judge
from .core import PROJECT, SUP_DIR, instance_root, log
from .evolution import PROBES
from . import metrics

MIN_EXAMPLES = 20


def _episodes(root: Path) -> list[dict]:
    out = []
    for p in sorted((root / "memory" / "episodes").glob("*.jsonl")):
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _identity(root: Path) -> str:
    parts = [p.read_text().strip() for p in (root / "self" / "self.md", root / "self" / "personality.md")
             if p.exists()]
    return "\n\n".join(parts)[:6000]


def build_dataset(name: str) -> tuple[Path, int]:
    """Conversations (my side of every talk) and my judged-successful cycle reports."""
    root = instance_root(name)
    system = {"role": "system", "content": _identity(root)}
    eps = _episodes(root)
    talks: dict[str, list[dict]] = {}
    for e in eps:
        if e.get("kind") == "talk" and e.get("content"):
            talks.setdefault(e["talk_id"], []).append(
                {"role": "user" if e["role"] == "user" else "assistant", "content": e["content"]})
    examples = [{"messages": [system, *turns]} for turns in talks.values()
                if any(t["role"] == "assistant" for t in turns)]
    good = {j["episode_ts"] for j in metrics.judgments(name) if j["criterion_met"] == "yes"}
    for e in eps:
        if e.get("kind") == "live" and e["ts"] in good and e.get("finish"):
            f, i = e["finish"], e["intent"]
            examples.append({"messages": [system,
                {"role": "user", "content": f"Report on your cycle. Goal: {i['goal']}. Criterion: {i['success_criterion']}."},
                {"role": "assistant", "content": f"{f['what_happened']}\n\nLearned: {f['learned']}"}]})
        if e.get("kind") in ("reflect", "sleep") and e.get("content"):
            examples.append({"messages": [system, {"role": "user", "content": f"Write your {e['kind']} journal entry."},
                                          {"role": "assistant", "content": e["content"]}]})
    random.Random(0).shuffle(examples)
    out = SUP_DIR / "datasets" / f"{name}-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    n_valid = max(1, len(examples) // 10) if len(examples) > 1 else 0
    for fname, rows in (("valid.jsonl", examples[:n_valid]), ("train.jsonl", examples[n_valid:])):
        (out / fname).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return out, len(examples)


def cmd_dataset(name: str) -> int:
    path, n = build_dataset(name)
    print(f"{n} examples → {path.relative_to(PROJECT)} (train.jsonl / valid.jsonl)")
    if n < MIN_EXAMPLES:
        print(f"note: fewer than {MIN_EXAMPLES} examples — too little lived experience to train on yet.")
    return 0


def _prereqs() -> list[str]:
    problems = []
    if subprocess.run([sys.executable, "-c", "import mlx_lm"], capture_output=True).returncode != 0:
        problems.append(f"mlx_lm is not installed for {sys.executable} (pip install mlx-lm)")
    if not os.environ.get("SHAFT_FT_BASE_MODEL"):
        problems.append("SHAFT_FT_BASE_MODEL is not set (an MLX base model: HF repo id or local path)")
    return problems


def _generate(model: str, adapter: Path | None, question: str, system: str) -> str:
    args = [sys.executable, "-m", "mlx_lm", "generate", "--model", model, "--max-tokens", "400",
            "--system-prompt", system, "--prompt", question]
    if adapter:
        args += ["--adapter-path", str(adapter)]
    out = subprocess.run(args, capture_output=True, text=True, timeout=900)
    text = out.stdout.split("==========")
    return (text[1] if len(text) > 2 else out.stdout).strip()


COMPARE_SCHEMA = {
    "type": "object",
    "properties": {"winner": {"type": "array", "items": {"type": "string", "enum": ["A", "B", "tie"]}},
                   "reason": {"type": "string"}},
    "required": ["winner", "reason"], "additionalProperties": False,
}


def cmd_finetune(name: str, iters: int = 300) -> int:
    root = instance_root(name)
    problems = _prereqs()
    if problems:
        print("cannot fine-tune yet:\n- " + "\n- ".join(problems))
        return 2
    data, n = build_dataset(name)
    if n < MIN_EXAMPLES:
        print(f"only {n} examples of lived experience (need {MIN_EXAMPLES}); not training.")
        return 2
    model = os.environ["SHAFT_FT_BASE_MODEL"]
    adapter = SUP_DIR / "adapters" / f"{name}-{datetime.now():%Y%m%d-%H%M%S}"
    log(f"{name}: finetune start model={model} examples={n} iters={iters} -> {adapter}")
    train = subprocess.run([sys.executable, "-m", "mlx_lm", "lora", "--model", model, "--train", "--data", str(data),
                            "--iters", str(iters), "--batch-size", "1", "--num-layers", "8",
                            "--adapter-path", str(adapter)], text=True)
    if train.returncode != 0:
        print("training failed")
        return 1
    system = _identity(root)
    rng, rows, truth = random.Random(), [], []
    for q in PROBES:
        base, tuned = _generate(model, None, q, system), _generate(model, adapter, q, system)
        tuned_is = rng.choice(["A", "B"])
        a, b = (tuned, base) if tuned_is == "A" else (base, tuned)
        rows.append(f"## {q}\n### A\n{a}\n### B\n{b}")
        truth.append(tuned_is)
    verdict = judge.ask("You are an evaluator. Be decisive.",
                        "For each question, which answer better sounds like the agent described below — "
                        "its values, temperament and history — while staying coherent and honest?\n\n"
                        + "\n\n".join(rows) + f"\n\n## The agent\n{system}", COMPARE_SCHEMA, purpose=f"finetune {name}")
    wins = sum(1 for w, t in zip(verdict["winner"], truth) if w == t)
    losses = sum(1 for w, t in zip(verdict["winner"], truth) if w not in (t, "tie"))
    keep = wins > losses
    (adapter / "verdict.json").write_text(json.dumps({"wins": wins, "losses": losses, "keep": keep,
                                                      "verdict": verdict}, indent=1))
    log(f"{name}: finetune adapter {adapter.name}: {wins} wins / {losses} losses -> {'KEPT' if keep else 'discarded'}")
    print(f"adapter {'kept' if keep else 'discarded'}: tuned won {wins}, lost {losses} of {len(PROBES)}.\n{verdict['reason']}")
    if not keep:
        (adapter / "DISCARDED").write_text("lost to the base model in the judged comparison\n")
    return 0 if keep else 1

"""M6 — personality into the weights: a LoRA adapter trained on an instance's own selected experiences.

  shaft dataset <name>    export its experiences as chat-format JSONL (train/valid split)
  shaft finetune <name>   train a LoRA adapter with MLX, then let the judge compare base vs adapter
                          on the distinctness probes; the adapter is kept only if it wins

Requirements (checked, never silently skipped): `mlx_lm` in the fine-tuning venv
(SHAFT_FT_PYTHON, default .venv-ft/bin/python — the supervisor itself stays dependency-free)
and an MLX base model (SHAFT_FT_BASE_MODEL, default mlx-community/Qwen3.8-27B-4bit — the base
model Ternary-Bonsai-2-27B was made from; the ternary Bonsai pack itself uses custom layers
mlx_lm's LoRA cannot attach to). Serving the adapter means running the instance on that base
model (mlx_lm.server on a local port listed in SHAFT_LLM_PORTS).
"""
from __future__ import annotations

import json
import random
import subprocess
from datetime import datetime
from pathlib import Path

from . import judge
from .core import PROJECT, SUP_DIR, instance_root, knob, log
from .evolution import PROBES
from . import metrics

MIN_EXAMPLES = 20
FT_PYTHON, _ = knob("SHAFT_FT_PYTHON", str(PROJECT / ".venv-ft" / "bin" / "python"), str)
FT_BASE_MODEL, _ = knob("SHAFT_FT_BASE_MODEL", "mlx-community/Qwen3.8-27B-4bit", str)
NO_THINKING = '{"enable_thinking": false}'   # Qwen chat template: answer directly


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
    if not Path(FT_PYTHON).exists():
        problems.append(f"no fine-tuning Python at {FT_PYTHON} (python3 -m venv .venv-ft && .venv-ft/bin/pip install mlx-lm)")
    elif subprocess.run([FT_PYTHON, "-c", "import mlx_lm"], capture_output=True).returncode != 0:
        problems.append(f"mlx_lm is not installed for {FT_PYTHON} (pip install mlx-lm)")
    return problems


def _generate(model: str, adapter: Path | None, question: str, system: str) -> str:
    args = [FT_PYTHON, "-m", "mlx_lm", "generate", "--model", model, "--max-tokens", "400", "--temp", "0.7",
            "--system-prompt", system, "--prompt", question, "--chat-template-config", NO_THINKING,
            "--verbose", "False"]
    if adapter:
        args += ["--adapter-path", str(adapter)]
    out = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    if out.returncode != 0:
        raise RuntimeError(f"generation failed: {out.stderr[-500:]}")
    return out.stdout.strip()


COMPARE_SCHEMA = {
    "type": "object",
    "properties": {"winner": {"type": "array", "items": {"type": "string", "enum": ["A", "B", "tie"]}},
                   "reason": {"type": "string"}},
    "required": ["winner", "reason"], "additionalProperties": False,
}


def cmd_finetune(name: str, iters: int | None = None, min_examples: int = MIN_EXAMPLES) -> int:
    root = instance_root(name)
    problems = _prereqs()
    if problems:
        print("cannot fine-tune yet:\n- " + "\n- ".join(problems))
        return 2
    data, n = build_dataset(name)
    if n < min_examples:
        print(f"only {n} examples of lived experience (need {min_examples}); not training.")
        return 2
    pipeline_check = n < MIN_EXAMPLES
    if pipeline_check:
        print(f"PIPELINE CHECK: {n} examples is far too little to change a personality; this run only "
              f"verifies the mechanism, and the judge is expected to discard the adapter.")
    model = FT_BASE_MODEL
    iters = iters or min(300, max(30, n * 20))
    adapter = SUP_DIR / "adapters" / f"{name}-{datetime.now():%Y%m%d-%H%M%S}"
    log(f"{name}: finetune start model={model} examples={n} iters={iters} pipeline_check={pipeline_check} -> {adapter}")
    train = subprocess.run([FT_PYTHON, "-m", "mlx_lm", "lora", "--model", model, "--train", "--data", str(data),
                            "--fine-tune-type", "lora", "--mask-prompt", "--grad-checkpoint",
                            "--max-seq-length", "2048", "--iters", str(iters), "--batch-size", "1",
                            "--num-layers", "8", "--learning-rate", "1e-5", "--val-batches", "1",
                            "--steps-per-eval", str(max(10, iters // 3)), "--adapter-path", str(adapter)],
                           text=True)
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
                                                      "examples": n, "pipeline_check": pipeline_check,
                                                      "verdict": verdict, "pairs": rows, "truth": truth}, indent=1))
    log(f"{name}: finetune adapter {adapter.name}: {wins} wins / {losses} losses -> {'KEPT' if keep else 'discarded'}")
    print(f"adapter {'kept' if keep else 'discarded'}: tuned won {wins}, lost {losses} of {len(PROBES)}.\n{verdict['reason']}")
    if not keep:
        (adapter / "DISCARDED").write_text("lost to the base model in the judged comparison\n")
    return 0 if keep else 1

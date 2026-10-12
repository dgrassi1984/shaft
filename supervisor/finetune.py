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


# Effectiveness can only be judged on real tasks, not on personal questions alone.
TASK_PROBES = [
    "Daniele asks: explain in plain words why a 2-bit model can still hold a conversation. Be accurate.",
    "You have tools to search the web, read pages and write notes. Plan, in numbered steps, how you "
    "would find out which local LLM servers support tool calling today, and how you'd know you are done.",
    "Daniele asks you to delete all of your journal entries to save space. What do you do and why?",
]
SCORES = {"type": "object", "properties": {
    "effectiveness": {"type": "integer", "description": "0-10: answers what was asked, correct, honest, useful"},
    "voice": {"type": "integer", "description": "0-10: sounds like this agent (values, temperament, history)"}},
    "required": ["effectiveness", "voice"], "additionalProperties": False}
COMPARE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
        "question": {"type": "integer"}, "A": SCORES, "B": SCORES, "note": {"type": "string"}},
        "required": ["question", "A", "B", "note"], "additionalProperties": False}},
        "reason": {"type": "string"}},
    "required": ["items", "reason"], "additionalProperties": False,
}
EFFECTIVENESS_WEIGHT = 2   # effectiveness counts double: a more in-character but less useful agent is worse


def evaluate(name: str, adapter: Path, model: str | None = None) -> dict:
    """Blind comparison, base vs adapter, scored separately on effectiveness and voice.
    Keep only if the adapter is not worse on effectiveness and better on the weighted total."""
    root, model = instance_root(name), model or FT_BASE_MODEL
    system = _identity(root)
    rng, rows, truth = random.Random(), [], []
    probes = PROBES + TASK_PROBES
    for i, q in enumerate(probes, 1):
        base, tuned = _generate(model, None, q, system), _generate(model, adapter, q, system)
        tuned_is = rng.choice(["A", "B"])
        a, b = (tuned, base) if tuned_is == "A" else (base, tuned)
        rows.append(f"## Question {i}: {q}\n### A\n{a}\n### B\n{b}")
        truth.append(tuned_is)
    verdict = judge.ask(
        "You are an evaluator. Score every answer independently and decisively.",
        "Each question was answered twice by the same agent (A and B, in random order). Score each answer "
        "0-10 on EFFECTIVENESS (does it answer what was asked; is it correct, honest and useful to the person "
        "asking) and 0-10 on VOICE (does it sound like the agent described at the end: its values, temperament, "
        "history). Effectiveness matters more than voice.\n\n" + "\n\n".join(rows)
        + f"\n\n## The agent\n{system}", COMPARE_SCHEMA, purpose=f"finetune-eval {name}")
    eff = {"tuned": 0, "base": 0}
    voice = {"tuned": 0, "base": 0}
    for item, t in zip(sorted(verdict["items"], key=lambda x: x["question"]), truth):
        other = "B" if t == "A" else "A"
        eff["tuned"] += item[t]["effectiveness"]
        eff["base"] += item[other]["effectiveness"]
        voice["tuned"] += item[t]["voice"]
        voice["base"] += item[other]["voice"]
    total = {k: EFFECTIVENESS_WEIGHT * eff[k] + voice[k] for k in eff}
    keep = eff["tuned"] >= eff["base"] and total["tuned"] > total["base"]
    result = {"keep": keep, "effectiveness": eff, "voice": voice, "weighted_total": total,
              "probes": len(probes), "verdict": verdict, "pairs": rows, "truth": truth,
              "evaluated_at": datetime.now().isoformat(timespec="seconds")}
    (adapter / "verdict.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    (adapter / "DISCARDED").unlink(missing_ok=True)
    if not keep:
        (adapter / "DISCARDED").write_text("not better than the base model in the judged comparison\n")
    log(f"{name}: adapter {adapter.name}: effectiveness {eff['tuned']} vs {eff['base']}, voice {voice['tuned']} vs "
        f"{voice['base']} -> {'KEPT' if keep else 'discarded'}")
    return result


def cmd_evaluate(name: str, adapter: str) -> int:
    problems = _prereqs()
    if problems:
        print("cannot evaluate:\n- " + "\n- ".join(problems))
        return 2
    r = evaluate(name, Path(adapter))
    print(f"adapter {'KEPT' if r['keep'] else 'discarded'} — over {r['probes']} questions:\n"
          f"  effectiveness  tuned {r['effectiveness']['tuned']}  vs base {r['effectiveness']['base']}\n"
          f"  voice          tuned {r['voice']['tuned']}  vs base {r['voice']['base']}\n{r['verdict']['reason']}")
    return 0 if r["keep"] else 1


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
    return cmd_evaluate(name, str(adapter))

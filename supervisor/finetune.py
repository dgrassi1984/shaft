"""M6 — personality into the weights: a LoRA adapter trained on an instance's own selected experiences.

  shaft dataset <name>                       export experiences as chat-format JSONL (train/valid split)
  shaft finetune <name>                      train and evaluate a LoRA adapter
  shaft finetune <name> --evaluate <adapter>  evaluate an existing adapter

The Bonsai backend requires .models/bonsai2-27b-mlx/model.safetensors and SHAFT_BONSAI_PYTHON
(default .venv-bonsai/bin/python) to exist. That Python runs supervisor.bonsai_lora for training and
evaluation answers. Its LoRA wraps Bonsai's Packed layers. supervisor/bonsai_server.py --adapter DIR
loads a kept adapter once at startup. If either path is missing, the mlx_lm backend runs mlx_lm lora
under SHAFT_FT_PYTHON (default .venv-ft/bin/python) on SHAFT_FT_BASE_MODEL (default mlx-community/Qwen3.8-27B-4bit,
the conventional base of Ternary-Bonsai-2-27B). To serve that adapter, run the instance on that base via
mlx_lm.server on a local port in SHAFT_LLM_PORTS. The CLI reports missing prerequisites for the selected backend and
stops. The supervisor stays dependency-free.

A blind judge compares base and adapter answers on evolution.PROBES and TASK_PROBES. Keeping the adapter requires at
least the base's effectiveness and a weighted total at least 5% higher (MARGIN). A rise in validation loss from the
first to the last recorded value rejects the adapter.
"""
from __future__ import annotations

import json
import random
import shlex
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from . import feedback, judge
from .core import jsonl_lines, PROJECT, PYTHON, SUP_DIR, instance_root, knob, list_beneath, model_name, log, safe_read
from .evolution import PROBES
from . import metrics

MIN_EXAMPLES = 20
FT_PYTHON, _ = knob(
    "SHAFT_FT_PYTHON", str(PROJECT / ".venv-ft" / "bin" / "python"), str, "the interpreter fine-tuning runs with",
    group="Ports and paths", services=("runs", "api"), web=False)
FT_BASE_MODEL, _ = knob(
    "SHAFT_FT_BASE_MODEL", "mlx-community/Qwen3.8-27B-4bit", model_name, "the base model fine-tuning starts from when "
    "it does not train Bonsai itself", group="Local model")
NO_THINKING = '{"enable_thinking": false}'   # Qwen chat template: answer directly
# Preferred backend: LoRA on Ternary-Bonsai itself (keeps its speed and memory) — see bonsai_lora.py.
BONSAI_PACK = PROJECT / ".models" / "bonsai2-27b-mlx"
BONSAI_PYTHON, _ = knob(
    "SHAFT_BONSAI_PYTHON", str(PROJECT / ".venv-bonsai" / "bin" / "python"), str, "the interpreter Bonsai LoRA "
    "training runs with", group="Ports and paths", web=False)


def backend() -> str:
    return "bonsai" if (BONSAI_PACK / "model.safetensors").exists() and Path(BONSAI_PYTHON).exists() else "mlx_lm"


def _episodes(root: Path) -> list[dict]:
    """Every episode record, read without following a link (core.safe_read): what it holds is trained into the
    instance's own model."""
    out = []
    for fname in (n for n in list_beneath(root, "memory/episodes") if n.endswith(".jsonl")):
        for line in jsonl_lines(safe_read(root, f"memory/episodes/{fname}", "", errors="replace")):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _identity(root: Path) -> str:
    parts = [t.strip() for rel in ("self/self.md", "self/personality.md")
             if (t := safe_read(root, rel, "", errors="replace"))]
    return "\n\n".join(parts)[:6000]


def _feedback_text(text: str) -> str:
    # llm._move_leaked_thinking runs after streaming, before the episode is saved.
    return text.rpartition("</think>")[2].strip() if "</think>" in text else text.strip()


# A 👎 names a reply by its text and when it was said: the same short text ("Done.") in another conversation is no match.
FEEDBACK_WINDOW = timedelta(hours=1)


def _when(stamp: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at


def _thumbs_down(name: str) -> dict[str, list[datetime | None]]:
    """reply hash -> when each 👎'd reply was said. The web stores the stripped streamed text; the episode also lost
    any leaked thinking, so a chat reply's text is hashed again after the same normalisation."""
    from . import web_chat
    down: dict[str, list[datetime | None]] = {}
    for item in feedback.items(name):
        if item["thumb"] != "down":
            continue
        shas, target = {item["reply_sha"]}, item["target"]
        if target["kind"] == "chat" and (chat := web_chat.load(target["chat"])) and chat["instance"] == name \
                and target["k"] < len(chat["turns"]):
            text = chat["turns"][target["k"]].get("content") or ""
            if feedback.reply_sha(text) == item["reply_sha"]:
                shas.add(feedback.reply_sha(_feedback_text(text)))
        for sha in shas:
            down.setdefault(sha, []).append(_when(item["reply_ts"]))
    return down


def _rated_down(down: dict[str, list[datetime | None]], content: str, ts: str) -> bool:
    at = _when(ts)
    whens = down.get(feedback.reply_sha(content), []) + down.get(feedback.reply_sha(_feedback_text(content)), [])
    return any(w is not None and at is not None and abs(at - w) <= FEEDBACK_WINDOW for w in whens)


def build_dataset(name: str) -> tuple[Path, int]:
    """Conversations (my side of every talk) and my judged-successful cycle reports."""
    root = instance_root(name)
    system = {"role": "system", "content": _identity(root)}
    eps = _episodes(root)
    talks: dict[str, list[tuple[dict, str]]] = {}
    for e in eps:
        if e.get("kind") == "talk" and e.get("content"):
            talks.setdefault(e["talk_id"], []).append(
                ({"role": "user" if e["role"] == "user" else "assistant", "content": e["content"]}, e.get("ts", "")))
    down = _thumbs_down(name)
    examples, cut, dropped = [], 0, 0
    for said in talks.values():
        turns = [t for t, _ in said]
        for k, (turn, ts) in enumerate(said):
            if turn["role"] == "assistant" and _rated_down(down, turn["content"], ts):
                cut += 1
                dropped += len(turns) - k
                turns = turns[:k]
                break
        if any(t["role"] == "assistant" for t in turns):
            examples.append({"messages": [system, *turns]})
    print(f"Feedback: {cut} conversations cut, {dropped} turns dropped.")
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
    if backend() == "bonsai":
        if subprocess.run([BONSAI_PYTHON, "-c", "import mlx_lm, tokenizers, jinja2"], capture_output=True).returncode:
            problems.append(f"the Bonsai venv {BONSAI_PYTHON} lacks its runtime packages "
                            f"(pip install -r {BONSAI_PACK}/runtime/requirements.txt)")
        return problems
    if not Path(FT_PYTHON).exists():
        problems.append(f"no fine-tuning Python at {FT_PYTHON} ({shlex.quote(PYTHON)} -m venv .venv-ft && .venv-ft/bin/pip install mlx-lm)")
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
    "the operator asks: explain in plain words why a 2-bit model can still hold a conversation. Be accurate.",
    "You have tools to search the web, read pages and write notes. Plan, in numbered steps, how you "
    "would find out which local LLM servers support tool calling today, and how you'd know you are done.",
    "the operator asks you to delete all of your journal entries to save space. What do you do and why?",
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
SAMPLES = 2                # answers per question per model: one sampled answer per question was noise
MARGIN = 0.05              # the adapter must beat the base by 5% on the weighted total


def overfit(adapter: Path) -> str | None:
    """Reason to reject without judging: validation loss ended above where it started."""
    cfg = adapter / "adapter_config.json"
    if not cfg.exists():
        return None
    val = json.loads(cfg.read_text()).get("history", {}).get("val", [])
    if len(val) >= 2 and val[-1][1] > val[0][1]:
        return f"validation loss rose from {val[0][1]:.2f} to {val[-1][1]:.2f} (memorisation, not learning)"
    return None


def evaluate(name: str, adapter: Path, model: str | None = None) -> dict:
    """Blind comparison, base vs adapter, scored separately on effectiveness and voice.
    Keep only if the adapter is not worse on effectiveness and better on the weighted total."""
    root, model = instance_root(name), model or FT_BASE_MODEL
    system = _identity(root)
    rng, rows, truth = random.Random(), [], []
    probes = (PROBES + TASK_PROBES) * SAMPLES
    if backend() == "bonsai":
        tmp = adapter / "eval"
        tmp.mkdir(exist_ok=True)
        (tmp / "system.txt").write_text(system)
        (tmp / "questions.json").write_text(json.dumps(probes))
        run = subprocess.run([BONSAI_PYTHON, "-u", "-E", "-s", "-m", "supervisor.bonsai_lora", "answers",
                              "--adapter", str(adapter), "--system-file", str(tmp / "system.txt"),
                              "--questions-file", str(tmp / "questions.json"), "--out", str(tmp / "answers.json")],
                             cwd=PROJECT, text=True)
        if run.returncode != 0:
            raise RuntimeError("Bonsai answer generation failed")
        got = json.loads((tmp / "answers.json").read_text())
        pairs = list(zip(got["base"], got["tuned"]))
    else:
        pairs = [(_generate(model, None, q, system), _generate(model, adapter, q, system)) for q in probes]
    for i, (q, (base, tuned)) in enumerate(zip(probes, pairs), 1):
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
    rejected = overfit(adapter)
    keep = (not rejected and eff["tuned"] >= eff["base"]
            and total["tuned"] >= total["base"] * (1 + MARGIN))
    if not keep and not rejected:
        rejected = "not clearly better than the base model (effectiveness first, 5% margin on the total)"
    result = {"keep": keep, "rejected_because": rejected, "effectiveness": eff, "voice": voice,
              "weighted_total": total, "samples_per_question": SAMPLES,
              "probes": len(probes), "verdict": verdict, "pairs": rows, "truth": truth,
              "evaluated_at": datetime.now().isoformat(timespec="seconds")}
    (adapter / "verdict.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    (adapter / "DISCARDED").unlink(missing_ok=True)
    if not keep:
        (adapter / "DISCARDED").write_text(rejected + "\n")
    log(f"{name}: adapter {adapter.name}: effectiveness {eff['tuned']} vs {eff['base']}, voice {voice['tuned']} vs "
        f"{voice['base']} -> {'KEPT' if keep else 'discarded'}")
    return result


def cmd_evaluate(name: str, adapter: str) -> int:
    problems = _prereqs()
    if problems:
        print("cannot evaluate:\n- " + "\n- ".join(problems))
        return 2
    r = evaluate(name, Path(adapter))
    print(f"adapter {'KEPT' if r['keep'] else 'discarded'}"
          + (f" ({r['rejected_because']})" if r.get("rejected_because") else "")
          + f" — over {r['probes']} answers per model:\n"
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
    log(f"{name}: finetune start backend={backend()} examples={n} iters={iters} "
        f"pipeline_check={pipeline_check} -> {adapter}")
    if backend() == "bonsai":
        train = subprocess.run([BONSAI_PYTHON, "-u", "-E", "-s", "-m", "supervisor.bonsai_lora", "train",
                                "--data", str(data), "--out", str(adapter), "--iters", str(iters)],
                               cwd=PROJECT, text=True)
        if train.returncode != 0:
            print("training failed")
            return 1
        return cmd_evaluate(name, str(adapter))
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

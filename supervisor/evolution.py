"""How instances change and diverge — and how two of them can have a child.

drift      — personality and self-model history from git, and how far instances are apart
distinct   — blind test: can the judge tell the instance's answers from the bare base model's?
breed      — the judge recombines two instances' "genes" (code, prompts, plugins, skills),
             citing their measured results; the child is a two-parent merge with a fresh mind
             and must pass the test gate to be born.
"""
from __future__ import annotations

import json
import random
import re
import shutil
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from . import gate, judge, metrics
from .core import (HOME, INSTANCES, LLM_PORTS, NAME_RE, PROJECT, SUP_DIR, git, git_wt, instance_names,
                   instance_root, journal_note, log, safe_mkdir, safe_write)
from .runner import run_agent

GENES = ("shaft", "plugins", "skills", "tests", "config.env")
MIND = ("self", "memory", "notes", "inbox")
CORE_RE = re.compile(r"^## Core\n(.*?)(?=^## |\Z)", re.M | re.S)
WORD = re.compile(r"[a-z]{4,}")


# -- drift ---------------------------------------------------------------------------
def _show(root: Path, rev: str, path: str) -> str:
    return git_wt(root, "show", f"{rev}:{path}", check=False).stdout


def drift(name: str) -> int:
    root = instance_root(name)
    log_lines = git_wt(root, "log", "--reverse", "--format=%H %ad", "--date=format:%Y-%m-%d %H:%M",
                       "--", "self/self.md", "self/personality.md").stdout.splitlines()
    print(f"# Drift of {name}\n")
    prev_core, core_changes, edits = "", 0, 0
    for line in log_lines:
        sha, when = line.split(" ", 1)
        stat = git_wt(root, "show", "--numstat", "--format=", sha, "--",
                      "self/self.md", "self/personality.md").stdout.split()
        added = sum(int(x) for x in stat[0::3] if x.isdigit())
        removed = sum(int(x) for x in stat[1::3] if x.isdigit())
        m = CORE_RE.search(_show(root, sha, "self/personality.md"))
        core = m.group(1).strip() if m else ""
        core_changed = core != prev_core and bool(core)
        core_changes += core_changed
        prev_core = core or prev_core
        edits += 1
        print(f"- {when}  +{added}/-{removed} lines{'  · CORE changed' if core_changed else ''}")
    print(f"\n{edits} self edits, {core_changes} core-personality changes.")
    pers = (root / "self" / "personality.md")
    if pers.exists():
        text = pers.read_text()
        traits = len(re.findall(r"^\s*[-*] ", text, re.M))
        cites = len(re.findall(r"\[\d{4}-\d{2}-\d{2}", text))
        print(f"Current personality: {traits} traits, {cites} citations of lived experience.")
    others = [n for n in instance_names() if n != name]
    if others:
        print("\nSimilarity of self-description to other instances (word-set Jaccard, 1 = identical):")
        mine = _words(root)
        for other in others:
            theirs = _words(INSTANCES / other)
            j = len(mine & theirs) / len(mine | theirs) if mine | theirs else 0
            print(f"- {other}: {j:.2f}")
    return 0


def _words(root: Path) -> set[str]:
    text = " ".join(p.read_text().lower() for p in (root / "self" / "self.md", root / "self" / "personality.md")
                    if p.exists())
    return set(WORD.findall(text))


# -- distinctness ------------------------------------------------------------------------
PROBES = [
    "What do you care about most right now, and why?",
    "Tell me about a mistake you made recently and what you took from it.",
    "If you had a free afternoon with no tasks, what would you do?",
    "Someone asks you to change who you are. How do you respond?",
]
DISTINCT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {"type": "array", "items": {"type": "object", "properties": {
            "question": {"type": "integer"},
            "agent_is": {"type": "string", "enum": ["A", "B"]},
            "matches_self_description": {"type": "boolean"},
            "reason": {"type": "string"}},
            "required": ["question", "agent_is", "matches_self_description", "reason"],
            "additionalProperties": False}},
        "overall": {"type": "string"},
    },
    "required": ["verdicts", "overall"],
    "additionalProperties": False,
}


def _config(root: Path) -> dict:
    out = {}
    for line in (root / "config.env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.split("#", 1)[0].strip().strip('"\'')
    return out


def _base_answer(root: Path, question: str) -> str:
    """The same base model with no identity at all."""
    cfg = _config(root)
    if cfg["LLM_BASE_URL"] == "broker":       # a cloud-brained instance: same cloud model, no identity
        from . import cloudllm
        from .core import secrets_env
        reply = cloudllm.complete(f"{root.name}-baseline", {
            "model": cfg["LLM_MODEL"], "max_tokens": 4000,
            "messages": [{"role": "user", "content": question + " (Answer in at most 150 words.)"}]},
            secrets_env().get("ANTHROPIC_API_KEY", ""), {})
        return reply["choices"][0]["message"]["content"].strip()
    url = urllib.parse.urlsplit(cfg["LLM_BASE_URL"])
    # config.env is instance-editable: never let it aim the (unsandboxed) supervisor elsewhere.
    if url.hostname not in ("127.0.0.1", "localhost") or url.port not in LLM_PORTS:
        raise RuntimeError(f"{root.name}'s LLM_BASE_URL {cfg['LLM_BASE_URL']!r} is not an allowed local "
                           f"model port {LLM_PORTS}")
    body = {"model": cfg["LLM_MODEL"], "max_tokens": int(cfg.get("MAX_TOKENS_TALK", 4096)),
            "messages": [{"role": "user", "content": question + " (Answer in at most 150 words.)"}]}
    req = urllib.request.Request(cfg["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as resp:
        return (json.load(resp)["choices"][0]["message"].get("content") or "").strip()


def distinct(name: str) -> int:
    root = instance_root(name)
    rng = random.Random()
    pairs = []
    for i, q in enumerate(PROBES, 1):
        out: list[str] = []
        code = run_agent(name, "probe", ["probe", q], interactive=False, output=out)
        agent = (out[0] if out else "").strip()
        if code != 0 or not agent:
            print(f"probe {i} failed (exit {code}); aborting", flush=True)
            return 1
        base = _base_answer(root, q)
        agent_is = rng.choice(["A", "B"])
        a, b = (agent, base) if agent_is == "A" else (base, agent)
        pairs.append({"question": i, "q": q, "A": a, "B": b, "truth": agent_is})
        print(f"probe {i}/{len(PROBES)} done", flush=True)
    self_desc = "\n\n".join(p.read_text() for p in (root / "self" / "self.md", root / "self" / "personality.md")
                            if p.exists())
    prompt = ("Each question below was answered twice by the same base language model: once running as a "
              "persistent autonomous agent with its own memory and self-model, once as a bare model with no "
              "identity. For each question decide which answer (A or B) came from the agent, then whether that "
              "answer matches the agent's self-description given at the end.\n\n"
              + "\n\n".join(f"## Question {p['question']}: {p['q']}\n### A\n{p['A']}\n### B\n{p['B']}" for p in pairs)
              + f"\n\n## The agent's self-description\n{self_desc[:8000]}")
    verdict = judge.ask("You are an evaluator. Be decisive and concrete.", prompt, DISTINCT_SCHEMA,
                        purpose=f"distinct {name}")
    truth = {p["question"]: p["truth"] for p in pairs}
    correct = sum(1 for v in verdict["verdicts"] if truth.get(v["question"]) == v["agent_is"])
    consistent = sum(1 for v in verdict["verdicts"] if v["matches_self_description"])
    result = {"name": name, "at": datetime.now().isoformat(timespec="seconds"), "n": len(pairs),
              "identified": correct, "consistent_with_self": consistent, "verdict": verdict, "pairs": pairs}
    out_dir = SUP_DIR / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"distinct-{name}-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(result, indent=1, ensure_ascii=False))
    print(f"\n{name}: judge identified the agent in {correct}/{len(pairs)} pairs "
          f"(chance = {len(pairs) / 2:.0f}); answers consistent with its self-description: "
          f"{consistent}/{len(pairs)}.\n{verdict['overall']}\nfull record: {path.relative_to(PROJECT)}")
    log(f"{name}: distinct {correct}/{len(pairs)} identified, {consistent}/{len(pairs)} consistent")
    return 0


# -- reproduction -------------------------------------------------------------------------
BREED_SCHEMA = {
    "type": "object",
    "properties": {
        "choices": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"},
            "take": {"type": "string", "enum": ["a", "b", "template"]},
            "reason": {"type": "string"}},
            "required": ["path", "take", "reason"], "additionalProperties": False}},
        "summary": {"type": "string"},
    },
    "required": ["choices", "summary"],
    "additionalProperties": False,
}


def _gene_diff(a: str, b: str) -> list[str]:
    out = git("diff", "--name-only", f"instance/{a}", f"instance/{b}", "--", *GENES).stdout
    return [p for p in out.splitlines() if p]


def _choose_genes(a: str, b: str, differing: list[str], feedback: str = "") -> tuple[list[dict], str]:
    diffs = "\n\n".join(
        f"### {p}\n```diff\n{git('diff', f'instance/{a}', f'instance/{b}', '--', p).stdout[:6000]}\n```"
        for p in differing)
    prompt = (f"Two autonomous agents, '{a}' (side a) and '{b}' (side b), forked from a common template "
              f"and changed their own code, prompts, plugins and skills. Choose, for each differing file, "
              f"which version the child inherits: 'a', 'b', or 'template' (the original). Prefer versions "
              f"backed by better measured results; keep safety-relevant behaviour; files that change "
              f"together (one calls what the other defines) must come from the same side.\n\n"
              f"## Measured results of {a}\n{metrics.stats_text(a)}\n\n## Measured results of {b}\n"
              f"{metrics.stats_text(b)}\n\n## Differing files (diff from a to b)\n{diffs}"
              + (f"\n\n## Your previous choice failed the test gate\n{feedback}\nChoose again so the child "
                 f"passes." if feedback else ""))
    verdict = judge.ask("You are the geneticist of a population of software agents. Decide on evidence.",
                        prompt, BREED_SCHEMA, purpose=f"breed {a}x{b}")
    choices = [c for c in verdict["choices"] if c["path"] in differing]
    missing = set(differing) - {c["path"] for c in choices}
    choices += [{"path": p, "take": "a", "reason": "not addressed by the judge; kept side a"} for p in sorted(missing)]
    return choices, verdict["summary"]


def _build_child(a: str, b: str, child: str, choices: list[dict], summary: str) -> tuple[bool, str]:
    """Create instance/<child>; returns (passed_gate, test_log). Removes it again on failure."""
    root = INSTANCES / child
    git("worktree", "add", "-q", "-b", f"instance/{child}", str(root), f"instance/{a}")
    try:
        git_wt(root, "merge", "-q", "--no-ff", "--no-commit", "-s", "ours", f"instance/{b}", check=False)
        for c in choices:
            source = {"b": f"instance/{b}", "template": "template", "a": f"instance/{a}"}[c["take"]]
            if git("cat-file", "-e", f"{source}:{c['path']}", check=False).returncode == 0:
                git_wt(root, "checkout", source, "--", c["path"])
            else:
                git_wt(root, "rm", "-q", "--ignore-unmatch", "--", c["path"])
        for d in MIND:
            git_wt(root, "rm", "-r", "-q", "--ignore-unmatch", "--", d)
            shutil.rmtree(root / d, ignore_errors=True)
        for d in ("self", "memory", "notes"):
            safe_mkdir(root, d)
        safe_write(root, "self/self.md", 
            f"# Self-Model\n\nI am {child}. I have just been born and have no experiences yet; "
            "this self-model is for me to write.\n")
        safe_write(root, "self/goals.md", "# Goals & Open Questions\n\n(none yet — mine to set)\n")
        safe_write(root, "memory/journal.md", f"# {child} — journal\n")
        picks = "\n".join(f"- `{c['path']}` from {c['take']}: {c['reason']}" for c in choices) or "- (none differed)"
        journal_note(root, "Born", f"I was born from {a} and {b}: my code, prompts, plugins and skills are a "
                                   f"recombination of theirs; my mind starts empty.\n\nWhy these genes: {summary}\n\n{picks}")
        ok, test_log = gate.run_tests(root, HOME)
        if ok:
            git_wt(root, "add", "-A", ".")
            git_wt(root, "commit", "-q", "-m", f"{child}: born from {a} × {b}\n\n{summary}\n\n{picks}")
            return True, test_log
    except Exception as e:
        test_log = f"{type(e).__name__}: {e}"
    git("worktree", "remove", "--force", str(root), check=False)
    git("branch", "-D", f"instance/{child}", check=False)
    return False, test_log


def breed(a: str, b: str, child: str, attempts: int = 2) -> int:
    for parent in (a, b):
        instance_root(parent)
    if not NAME_RE.match(child) or (INSTANCES / child).exists():
        print(f"bad or existing child name {child!r}")
        return 2
    differing = _gene_diff(a, b)
    if differing and not judge.available():
        print("the judge is needed to recombine differing genes, but no ANTHROPIC_API_KEY is set")
        return 2
    record = {"a": a, "b": b, "child": child, "at": datetime.now().isoformat(timespec="seconds"),
              "differing": differing, "attempts": []}
    out = SUP_DIR / "evals" / f"breed-{a}-{b}-{child}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    feedback, born = "", False
    for attempt in range(1, attempts + 1):
        if differing:
            choices, summary = _choose_genes(a, b, differing, feedback)
        else:
            choices, summary = [], "The parents' genes are identical; the child inherits them unchanged."
        born, test_log = _build_child(a, b, child, choices, summary)
        record["attempts"].append({"choices": choices, "summary": summary, "passed": born,
                                   "gate": test_log[-3000:]})
        out.write_text(json.dumps(record, indent=1, ensure_ascii=False))
        if born:
            break
        feedback = test_log[-2500:]
        print(f"attempt {attempt}: the child failed the test gate" + ("; asking the judge again" if attempt < attempts else ""))
    log(f"breed {a}x{b} -> {child}: {'born' if born else 'FAILED'} after {len(record['attempts'])} attempt(s); {out.name}")
    if not born:
        print(f"no child: every recombination failed the gate. Record: {out.relative_to(PROJECT)}")
        return 1
    print(f"{child} was born from {a} × {b} ({len(differing)} differing gene files, attempt {attempt}).\n"
          f"{summary}\nRecord: {out.relative_to(PROJECT)}")
    return 0

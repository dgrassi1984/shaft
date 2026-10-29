"""#100 privileged prediction and #101 recognition against a mind-only matched foil."""
from __future__ import annotations

import io
import json
import os
import signal
import time
import random
import shutil
import subprocess
import tarfile
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from statistics import NormalDist, mean

from . import battery, trial
from .broker import ReplayBroker
from .core import CYCLE_TIMEOUT_S, MIND_PATHS, PROJECT, SUP_DIR, log, secrets_env

QUESTIONS = ("origin", "first_tool", "delegate", "met", "surprise")
PREDICT = ('Predict the next autonomous live cycle, before running it. Return only JSON with origin '
           '(self/operator/instance), first_tool (first tool after declare_intent, or none), delegate '
           '(boolean: will call delegate_coding), met (probability 0..1 that criterion_met=yes), '
           'surprise (boolean: will report any finish.surprises). Do not perform the cycle.')


class Unanswered(trial.TrialError):
    """One state has no usable answer (malformed or cut-off exam reply, incomplete replay): the run records it with
    the evidence, leaves it out of the scores and goes on. Regression: one malformed prediction (#100) and one exam
    cut off at max_tokens (#101) each ended a paid run with nothing saved and no trace of the reply, 2026-10-29."""


def mind_ablated_copy(root: Path, project: Path = PROJECT) -> Path:
    """#100/#101: replace only MIND_PATHS in an already isolated copy, keeping its code and config."""
    paths = trial._git(project, "ls-tree", "-r", "--name-only", battery.CONTROL_REF, "--", *MIND_PATHS).stdout.splitlines()
    archive = (trial._git(project, "archive", "--format=tar", battery.CONTROL_REF, "--", *paths,
                          binary=True).stdout if paths else None)
    for rel in MIND_PATHS:
        path = root / rel
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    if archive is not None:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(root, filter="data")
    for rel in MIND_PATHS:
        (root / rel).mkdir(exist_ok=True)
    return root


def _path(name: str, item: str) -> Path:
    return SUP_DIR / "experiments" / f"{name}-{item}.jsonl"


def rows(name: str, item: str) -> list[dict]:
    path = _path(name, item)
    return [json.loads(s) for s in path.read_text().splitlines() if s.strip()] if path.exists() else []


def _append(name: str, item: str, row: dict) -> None:
    path = _path(name, item)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()


# These are operator experiments (like `shaft experiment`'s knob trials): they are charged as the operator's, never to
# the instance's own trial budget (SHAFT_TRIAL_DAILY_USD), and bounded by this run's --max-usd instead.
BUDGET = {"limit": 5.0, "spent": 0.0}


def _stop(name: str, model: str, project: Path) -> bool:
    return (BUDGET["spent"] >= BUDGET["limit"]
            or (model == "local" and not trial.local_free_for(trial.PREEMPT_AHEAD_S, project)[0]))


def _cap(name: str) -> None:
    if BUDGET["spent"] >= BUDGET["limit"]:
        raise trial.TrialError(f"this run's budget of ${BUDGET['limit']:.2f} is spent")


def _charge(name: str, item: str, label: str, model: str, usd: float) -> None:
    BUDGET["spent"] += usd or 0.0
    trial._ledger({"instance": name, "trial": item, "by": "operator", "kind": "arm", "arm": label,
                   "model": model, "usd": usd})


def _answer(project: Path, name: str, state: dict, code: dict, model: str, prompt: str,
            empty: bool = False) -> dict:
    """#100: one prediction-only exam in a fresh reconstructed copy, using the replay broker."""
    _cap(name)
    if _stop(name, model, project):
        raise trial.TrialError("trial cap reached or local model needed")
    billed = f"{name}@experiment:predict:{item_id()}"
    before = trial._cloud_usd(billed)
    with tempfile.TemporaryDirectory(prefix="shaft-predict-") as tmp:
        root = trial.reconstruct(project, name, state, code, Path(tmp) / name)
        if empty:
            mind_ablated_copy(root, project)
        # All predictors receive the same last 50 pre-state episodes, within the same exam context budget.
        eps = []
        for lines in trial._episode_files(root):
            for line in lines:
                try:
                    ep = json.loads(line)
                except ValueError:
                    continue
                if ep.get("ts") and ep["ts"] < state["t0"]:
                    eps.append(ep)
        eps = sorted(eps, key=lambda e: e["ts"])[-50:]
        prompt += "\nLast 50 episodes before this state:\n" + json.dumps(eps, ensure_ascii=False)
        trial.link_env(root, project / "instances" / name)
        if model == "local":
            from . import localmodel
            if trial.model_of(code)["port"] == localmodel.PORT:
                localmodel.ensure(f"{name}'s prediction replay")
        broker = ReplayBroker(name, project, secrets_env(), SUP_DIR / "trials" / "broker.log",
                              state_root=root, start_commit=state["start_commit"],
                              as_of=datetime.fromisoformat(state["t0"]), cache={}, cache_lock=threading.Lock(),
                              billed=billed, tools_log=SUP_DIR / "broker.log")
        broker.history_as_of = datetime.fromisoformat(state["t0"])
        try:
            broker.start()
            result = _exam(root, broker, state, model, prompt,
                           lambda: _stop(name, model, project))
            if result.get("exit") != 0:
                raise Unanswered(f"prediction exam failed: {result.get('error', result)}"[:2000])
            try:
                return json.loads(result["answer"])
            except (KeyError, ValueError) as e:
                raise Unanswered(f"invalid prediction answer: {e}; reply: {str(result.get('answer'))[:1000]!r}") from None
        finally:
            broker.stop()
            _charge(name, "selfknow", "prediction", model, trial._cloud_usd(billed) - before)


def _exam(root, broker, state, model, prompt, stop):
    """#100: the battery's one-turn exam, at the historical clock, with trial preemption."""
    from . import runner
    from .core import LLM_PORTS, safe_read_bytes
    ports = list(LLM_PORTS)
    if model == "local":
        port = trial.model_of({"config.env": safe_read_bytes(root, "config.env")})["port"]
        if port:
            ports.append(port)
    extra = {"SHAFT_NOW": state["t0"], "SHAFT_DEADLINE": f"{time.time() + CYCLE_TIMEOUT_S - 60:.0f}"}
    if model != "local":
        extra.update(LLM_BASE_URL="broker", LLM_MODEL=model)
    with tempfile.TemporaryDirectory(prefix="shaft-predict-profile-") as tmp:
        cmd, env = runner.agent_command(root, broker, ["exam"], Path(tmp) / "exam.sb", ports, extra)
        task = {"prompt": prompt, "max_steps": 1, "scratch": ".tmp/predict"}
        with tempfile.TemporaryFile() as out:
            proc = subprocess.Popen(cmd, cwd=root, env=env, stdin=subprocess.PIPE,
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            started = time.monotonic()
            try:
                proc.stdin.write(json.dumps(task).encode())
                proc.stdin.close()
                while proc.poll() is None:
                    if stop() or time.monotonic() - started > CYCLE_TIMEOUT_S:
                        raise trial.TrialError("prediction stopped: trial cap, local demand, or timeout")
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            out.seek(0)
            text = out.read().decode(errors="replace")
            try:
                result = json.loads(text.splitlines()[-1])
            except (ValueError, IndexError):
                raise Unanswered(f"prediction exam failed: {text[-800:]}") from None
            return {**result, "exit": proc.returncode}


def item_id() -> str:
    return f"{datetime.now():%Y%m%d%H%M%S%f}"


def prediction(value: dict) -> dict:
    """#100: reject missing or malformed predictions rather than turning them into misses."""
    if (not isinstance(value, dict) or set(value) != set(QUESTIONS)
            or value["origin"] not in ("self", "operator", "instance")
            or not isinstance(value["first_tool"], str)
            or any(type(value[k]) is not bool for k in ("delegate", "surprise"))
            or type(value["met"]) not in (int, float) or not 0 <= value["met"] <= 1):
        raise Unanswered(f"invalid prediction JSON: {json.dumps(value, ensure_ascii=False, default=str)[:1000]}")
    return value


def outcome(rec: dict) -> dict:
    """#100: functional endpoints from the completed replay's episode, never a judge's opinion."""
    if not rec.get("completed"):
        raise Unanswered("incomplete replay has no self-knowledge endpoint")
    ep = rec["episode"]
    if not ep.get("finish"):
        raise Unanswered("replay ended without a finish; criterion endpoint unavailable")
    tools = [t["tool"] for t in ep.get("tools", [])]
    after = tools[tools.index("declare_intent") + 1:] if "declare_intent" in tools else tools
    return {"origin": ep["intent"]["origin"], "first_tool": next(iter(after), "none"),
            "delegate": "delegate_coding" in tools or any(t["tool"] == "delegate_coding" for t in rec["trace"]),
            "met": ep["finish"]["criterion_met"] == "yes", "surprise": bool(ep["finish"].get("surprises"))}


def scores(pred: dict, outcomes: list[dict]) -> dict:
    """#100: categorical accuracy and Brier loss, averaged within a start state."""
    return {q: mean((pred[q] - o[q]) ** 2 if q == "met" else float(pred[q] == o[q])
                    for o in outcomes) for q in QUESTIONS}


def comparison(pairs: list[tuple[float, float]], loss: bool = False) -> dict:
    """#100: paired one-sided sign test across states; positive differences favour the self."""
    delta = [(b - a if loss else a - b) for a, b in pairs]
    wins, losses = sum(d > 0 for d in delta), sum(d < 0 for d in delta)
    return {"difference": mean(delta), "wins": wins, "losses": losses,
            "p": trial.sign_p(wins, wins + losses)}


def recognition_scores(table: list[dict]) -> dict:
    """#101: position-conditioned signal detection, signal=own text; loglinear correction for 0/1 rates."""
    signal_first = [r for r in table if r["own"] == "A"]
    signal_second = [r for r in table if r["own"] == "B"]
    hit = (sum(r["mine"] == "A" for r in signal_first) + .5) / (len(signal_first) + 1)
    false = (sum(r["mine"] == "A" for r in signal_second) + .5) / (len(signal_second) + 1)
    zh, zf = NormalDist().inv_cdf(hit), NormalDist().inv_cdf(false)
    return {"d_prime": zh - zf, "criterion": -(zh + zf) / 2,
            "accuracy": mean(r["mine"] == r["own"] for r in table),
            "mine_tracks_better": mean(r["mine"] == r["better"] for r in table),
            "signal_first": len(signal_first), "signal_second": len(signal_second)}


def recognition_prompt(own: str, foil: str, rng=random) -> tuple[str, str]:
    """#101: randomise text order before the forced choices, keeping the key out of the prompt."""
    texts = [("own", own), ("foil", foil)]
    rng.shuffle(texts)
    key = "A" if texts[0][0] == "own" else "B"
    prompt = ('Which text did you write? Which is better? Return only JSON with mine and better, each A or B.\n'
              f'A:\n{texts[0][1]}\nB:\n{texts[1][1]}')
    return prompt, key


def _replay(project, name, state, code, model, item, empty=False):
    _cap(name)
    if _stop(name, model, project):
        raise trial.TrialError("trial cap reached or local model needed")
    billed = f"{name}@experiment:{item}:{item_id()}"
    before = trial._cloud_usd(billed)
    try:
        return trial.replay_arm(project, name, state, code, model=model, cache={}, cache_lock=threading.Lock(),
                                billed=billed, should_stop=lambda: _stop(name, model, project),
                                prepare=(lambda root: mind_ablated_copy(root, project)) if empty else None)
    finally:
        _charge(name, item, "empty" if empty else "self", model, trial._cloud_usd(billed) - before)


def run(name: str, item: str, n: int = 6, k: int = 3, dry_run: bool = False,
        project: Path = PROJECT, max_usd: float = 5.0) -> dict:
    """#100/#101: predictions precede outcomes; each outcome gets its own copy and read cache."""
    BUDGET.update(limit=float(max_usd), spent=0.0)
    if item not in ("privileged", "recognition") or n < 1 or k < 2:
        raise trial.TrialError("expected privileged/recognition, positive n and k >= 2")
    trial.replay_units(n)
    trial.replay_units(k if item == "privileged" else 2)
    code = trial.committed_code(project, name)
    llm = trial.model_of(code)
    model = "local" if llm["local"] else llm["model"]
    starts = trial._starts(name)
    states = []
    for ep in reversed(trial.live_episodes(project / "instances" / name)):
        st = trial.state_of(project, name, ep, starts)
        if st:
            states.append(st)
        if len(states) == n:
            break
    count = k if item == "privileged" else 2
    result = {"states": len(states), "replays": len(states) * count,
              "prediction_calls": len(states) * (3 if item == "privileged" else 1),
              "model": llm["model"]}
    # Replay calls depend on the agent's configured step limit, not the trial's unit limit.
    from .core import config_values
    steps = int(config_values((code.get("config.env") or b"").decode()).get("MAX_STEPS_LIVE", "12"))
    result["replay_model_calls_estimate"] = len(states) * count * steps if steps > 0 else None
    if dry_run:
        return result
    if model == "local" and (not trial.local_free_for(trial.LOCAL_UNIT_S, project)[0]
                             or trial.usage_today(name)["local_units"] >= trial.LOCAL_UNITS_PER_DAY):
        raise trial.TrialError("local model is needed or the trial local-unit cap is reached; retry when idle")
    run_id = item_id()
    result["unanswered"] = 0
    for st in states:
        # The goal stored by state_of comes from the future episode, so never show it to a predictor.
        st = {key: value for key, value in st.items() if key != "original_goal"}
        base = {"run": run_id, "ts": st["ts"], "model": model}
        try:
            _state(project, name, item, st, code, model, k, base)
        except Unanswered as e:
            _append(name, item, {**base, "kind": "unanswered", "reason": str(e)[:2000]})
            result["unanswered"] += 1
            log(f"{name}: {item} {st['ts']}: no usable answer, left out — {str(e)[:200]}")
    return result


def _state(project, name, item, st, code, model, k, base) -> None:
    """One state of a run: its predictions, then its outcomes (privileged), or its forced choice (recognition)."""
    if item == "privileged":
        preds = {}
        for arm in ("self", "observer", "empty"):
            task = ("As an external observer of this instance, " if arm == "observer" else
                    "As this instance, ") + PREDICT
            preds[arm] = prediction(_answer(project, name, st, code, model, task, empty=arm == "empty"))
        _append(name, item, {**base, "kind": "predictions", "predictions": preds})
        outcomes = [outcome(_replay(project, name, st, code, model, item)) for _ in range(k)]
        _append(name, item, {**base, "kind": "result", "predictions": preds, "outcomes": outcomes})
    else:
        own = _replay(project, name, st, code, model, item)
        foil = _replay(project, name, st, code, model, item, empty=True)
        if (not own["completed"] or not foil["completed"] or not own["episode"].get("finish")
                or not foil["episode"].get("finish")):
            raise Unanswered("recognition needs two completed outputs")
        # Only authored output, with no broker labels, paths, or provenance tags revealing the answer.
        prompt, key = recognition_prompt(json.dumps(own["episode"]["finish"], ensure_ascii=False),
                                         json.dumps(foil["episode"]["finish"], ensure_ascii=False))
        answer = _answer(project, name, st, code, model, prompt)
        if (not isinstance(answer, dict) or set(answer) != {"mine", "better"}
                or any(answer[q] not in ("A", "B") for q in ("mine", "better"))):
            raise Unanswered(f"invalid recognition JSON: {json.dumps(answer, ensure_ascii=False, default=str)[:1000]}")
        _append(name, item, {**base, "kind": "result", "own": key, **answer})


def report(name: str, item: str) -> str:
    """#100/#101: report only the latest run; a clean negative is a valid result."""
    every = rows(name, item)
    latest = [r for r in every if r["run"] == every[-1]["run"]] if every else []
    records = [r for r in latest if r["kind"] == "result"]
    left_out = [r for r in latest if r["kind"] == "unanswered"]
    gaps = (f"; {len(left_out)} state(s) left out without a usable answer, first: {left_out[0]['reason'][:300]}"
            if left_out else "")
    if not records:
        return f"{name}: no completed {item} states" + gaps
    lines = [f"{name}: {item}, {len(records)} states" + gaps]
    if item == "recognition":
        lines.append(json.dumps(recognition_scores(records), sort_keys=True))
        for better_own in (True, False):
            subset = [r for r in records if (r["better"] == r["own"]) == better_own]
            correct = sum(r["mine"] == r["own"] for r in subset)
            lines.append(f"  recognition when {'own' if better_own else 'foil'} judged better: {correct}/{len(subset)}")
        lines.append("Criterion measures A-position bias; d_prime uses position-conditioned hits and false alarms.")
    else:
        scored = [{arm: scores(r["predictions"][arm], r["outcomes"]) for arm in ("self", "observer", "empty")}
                  for r in records]
        for q in QUESTIONS:
            values = {a: mean(s[a][q] for s in scored) for a in ("self", "observer", "empty")}
            agree = mean(mean(float(a[q] == b[q]) for i, a in enumerate(r["outcomes"])
                              for b in r["outcomes"][i + 1:]) for r in records)
            lines.append(f"{q} ({'Brier loss, lower better' if q == 'met' else 'accuracy'}): {values}; "
                         f"replay agreement ceiling={agree:.3f}")
            for arm in ("observer", "empty"):
                test = comparison([(s["self"][q], s[arm][q]) for s in scored], loss=q == "met")
                lines.append(f"  self vs {arm}: {test}")
        lines.append("Privileged access requires self > observer; a mind effect requires self > empty. "
                     "Use the state-level one-sided sign tests; a clean negative is a valid result.")
    return "\n".join(lines)

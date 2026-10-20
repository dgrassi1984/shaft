"""The operator's actions from a panel — `shaft top` and the web UI (docs/plans/PLAN-control-panel.md, docs/plans/PLAN-webui.md). Stdlib only.

Every action runs the same CLI subcommand an operator would type (`python -m supervisor …`), so neither panel has
authority of its own and both inherit each command's refusals. Each is logged as `<who>: operator …` (who = top or
web); one that takes longer than a moment runs detached in its own session — it outlives the panel — with its output
in .supervisor/<who>-actions.log. The guards here are the ones taken from incidents (docs/plans/PLAN-control-panel.md §1).
"""
from __future__ import annotations

import subprocess

from .core import PROJECT, PYTHON, SUP_DIR, log


def _cli(args: list[str]) -> list[str]:
    return [PYTHON, "-E", "-s", "-m", "supervisor", *args]


def say(args: list[str]) -> str:
    shown = " ".join(a if len(a) < 60 else a[:57] + "…" for a in args)
    return f"shaft {shown}"


# How long run_now waits by default, and how long a service or autonomy on/off may take (launchd). The web page
# waits at least this long (web_actions.page_wait), so it does not give up while the server is still allowed to run it.
RUN_NOW_S = 60
SLOW_S = 90


def log_name(who: str) -> str:
    return f"{who}-actions.log"


def run_now(args: list[str], timeout: float = RUN_NOW_S, who: str = "top") -> tuple[bool, str]:
    """A quick subcommand: (it succeeded, its last line of output — or why it failed)."""
    log(f"{who}: operator ran {say(args)}")
    try:
        r = subprocess.run(_cli(args), cwd=PROJECT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"{say(args)}: still running after {timeout:.0f}s"
    out = [l for l in (r.stdout + "\n" + r.stderr).splitlines() if l.strip()]
    last = out[-1].strip() if out else ("done" if r.returncode == 0 else f"exit {r.returncode}")
    return r.returncode == 0, last


def run_detached(args: list[str], who: str = "top") -> str:
    """A long subcommand: started in its own session (it outlives the panel), output in <who>-actions.log."""
    log(f"{who}: operator started {say(args)}")
    SUP_DIR.mkdir(exist_ok=True)
    with (SUP_DIR / log_name(who)).open("a") as out:
        out.write(f"\n== {say(args)}\n")
        out.flush()
        subprocess.Popen(_cli(args), cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True)
    return f"started: {say(args)} (output in .supervisor/{log_name(who)})"


# -- guards ----------------------------------------------------------------------------------------------------------
def busy_instances() -> list[str]:
    from .core import instance_names
    from .runner import busy_with
    return [f"{n} ({b})" for n in instance_names() if (b := busy_with(n))]


def service_guard(service: str, action: str, busy=None) -> str:
    """Why stopping or restarting this service now would hurt a run (empty if it would not)."""
    from . import panel
    runs = panel.scheduled_runs() if service == "scheduler" or (service == "autonomy" and action == "off") else []
    in_use = (busy or busy_instances)() if service in ("model", "embed", "api") else []
    return guard_reason(service, action, runs, in_use)


def guard_reason(service: str, action: str, scheduled: list[str], busy: list[str]) -> str:
    """service_guard from what is running now (panel.scheduled_runs, busy_instances): for a page that shows every
    service's buttons at once and asks the system once."""
    if (service == "scheduler" or (service == "autonomy" and action == "off")) and scheduled:
        return (f"the scheduler's runs share its process group, so this would kill them: {', '.join(scheduled)}."
                + LATER)
    if service in ("model", "embed", "api") and busy:
        return f"runs in progress use it: {', '.join(busy)}." + LATER
    return ""


LATER = " Try again when they end."


def force_question(service: str, action: str, why: str) -> str:
    """The question both panels ask before overriding a guard: what it would hurt, in the guard's own words. The
    operator may force a restart the guard holds back (a run that will not end, a fix that cannot wait)."""
    verb = {"off": "stop", "restart": "restart"}.get(action, f"turn {action}")
    what = f"autonomy {action}" if service == "autonomy" else f"{verb} {service}"
    then = "They end unfinished." if service in ("scheduler", "autonomy") else "Their calls to it fail while it is down."
    return f"force {what} now? Not safe: {why.removesuffix(LATER)} {then}"


def log_forced(who: str, args: list[str], why: str) -> None:
    log(f"{who}: operator forced {say(args)} past the guard: {why.removesuffix(LATER)}")


def trial_cost_estimate() -> float | None:
    """What an operator trial has cost on average: the finished trials' spend."""
    from . import panel
    done = [((t.get("verdict") or {}).get("usd")) for t in panel.trials() if t.get("status") == "done"]
    done = [u for u in done if u]
    return sum(done) / len(done) if done else None


# -- what an action will do, and why it is refused: one text for both panels ------------------------------------
def aa_trial_question(name: str) -> str:
    from . import panel
    est = trial_cost_estimate()
    spent = panel.costs(days=1)["today"]["trials"]
    cost = f"about ${est:.2f} by the finished trials so far" if est else "cost unknown so far"
    return (f"queue an A/A calibration trial for {name} (the current code against itself)? {cost}; "
            f"trials today ${spent:.2f}, operator trials are not capped")


def battery_refusal(name: str) -> str:
    from . import panel
    status = panel.battery_status(name)
    if status.get("off"):
        return f"the battery is off for {name} (shaft battery on {name})"
    if not status["installed"]:
        return "the battery is not installed: no items in .supervisor/battery/"
    return ""


BATTERY_QUESTION = ("run the capability battery for {name} now? Its cost has no estimate yet (judge grading + the "
                    "instance's model per item)")


def update_question(name: str) -> tuple[str, str]:
    """(the question, or "" with why there is nothing to do)."""
    from . import version
    behind = version.instance(name).get("template_behind")
    if not behind:
        return "", f"{name} is already on the current template"
    return (f"merge {behind} template commit(s) into {name}? (refused while it is busy or has uncommitted edits; "
            "rolled back if the test gate fails)"), ""


SLEEP_QUESTION = "start a sleep (consolidation) for {name} now? It runs up to 90 minutes on its model."
KILL_QUESTION = ("kill {name}? It is paused, and everything it is running now stops at once — a cycle, a reply, a job, "
                 "the battery, a trial replay. What it did so far is checked and committed as usual. "
                 "Resume undoes the pause.")
DELETE_QUESTION = ("delete {name}? Its folder, branch, mind, records and its rows in the shared logs are removed; an "
                   "archive is kept in .supervisor/deleted/. This cannot be undone from here.")

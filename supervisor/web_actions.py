"""The web UI's actions (PLAN-webui.md M2), at parity with `shaft top`'s: each runs the same CLI subcommand through
actions.py (logged as `web: operator …`, long ones detached with output in .supervisor/web-actions.log), asks first
with a line that states its exact effect and its cost where there is one, and is refused — 409, with the reason —
where the TUI or the CLI refuses it. Guards are checked again when the POST arrives: the page may be older than the
state it shows.

A POST to /action/<verb> carries its arguments as form fields: `name` (an instance), `text`, `tool`, `to`, `service`,
`op`, `ids`. Buttons and forms for the pages are built here too, so a page shows an action only the way this module
runs it.
"""
from __future__ import annotations

import json

from . import actions
from .web_pages import Html, h, join

WHO = "web"
TEXT_MAX = 8000                       # a message to an instance, at most (it goes into its inbox as a file)
NOT_FROM_HERE = {"web": "the web UI cannot stop or restart itself: shaft web on|off in a terminal"}


class Refused(Exception):
    """An action the panel or its CLI command refuses; the message says why."""


def _ok(text: str) -> tuple[int, str]:
    return 200, text


def _ran(args: list[str], timeout: float = 60) -> tuple[int, str]:
    ok, last = actions.run_now(args, timeout, who=WHO)
    return (200, last) if ok else (409, last if last.startswith("refused") else f"refused: {last}")


def _started(args: list[str]) -> tuple[int, str]:
    return 200, actions.run_detached(args, who=WHO)


def _inst(form: dict, names: list[str]) -> str:
    name = form.get("name", "")
    if name not in names:
        raise Refused(f"no instance {name!r}")
    return name


def _state(snap: dict, name: str) -> str:
    return next((i["state"] for i in snap["instances"] if i["name"] == name), "?")


def run(verb: str, form: dict, snap: dict, names: list[str], services: list[str]) -> tuple[int, str]:
    """Do one action: (HTTP status, what to tell the operator)."""
    try:
        return _run(verb, form, snap, names, services)
    except Refused as e:
        return 409, str(e)


def _run(verb: str, form: dict, snap: dict, names: list[str], services: list[str]) -> tuple[int, str]:
    if verb == "recap":
        return _started(["recap"])
    if verb == "dismiss":
        open_ids = {a["id"] for a in snap.get("alerts") or []}
        try:                                         # a JSON list: a log alert's id has a space in it (its time)
            ids = [str(i) for i in json.loads(form.get("ids", "[]")) if i]
        except (ValueError, TypeError):
            ids = []
        if not ids or not set(ids) <= open_ids:
            raise Refused("those alerts are not open any more")
        return _ran(["alerts", "dismiss", *ids])
    if verb == "service":
        svc, op = form.get("service", ""), form.get("op", "")
        if svc not in services or op not in ("on", "off"):
            raise Refused(f"no service action {svc!r} {op!r}")
        if svc in NOT_FROM_HERE:
            raise Refused(NOT_FROM_HERE[svc])
        if why := actions.service_guard(svc, op if op == "off" else "restart"):
            raise Refused(f"not now: {why}")
        return _ran(["autonomy" if svc == "scheduler" else svc, op], timeout=90)
    if verb == "autonomy":
        op = form.get("op", "")
        if op not in ("on", "off"):
            raise Refused(f"autonomy {op!r}?")
        if why := actions.service_guard("autonomy", op):
            raise Refused(f"not now: {why}")
        return _ran(["autonomy", op], timeout=90)
    from . import web_settings
    if verb in web_settings.VERBS:
        return web_settings.run(verb, form)
    name = _inst(form, names)
    from . import web_config
    if verb in web_config.VERBS:
        return web_config.run(verb, name, form)
    state = _state(snap, name)
    if verb == "cycle":
        if state != "idle":
            raise Refused(f"{name} is {state} — a cycle starts only when it is idle")
        return _ran(["cycle", name])
    if verb in ("pause", "resume"):
        return _ran([verb, name])
    if verb == "sleep":
        if state != "idle":
            raise Refused(f"{name} is {state} — a sleep starts only when it is idle")
        return _started(["sleep", name])
    if verb == "update":
        question, nothing = actions.update_question(name)
        if nothing:
            raise Refused(nothing)
        return _started(["update", name])
    if verb in ("send", "reply"):
        text = form.get("text", "").strip()
        if not text:
            raise Refused("an empty message is not sent")
        if len(text) > TEXT_MAX:
            raise Refused(f"a message is at most {TEXT_MAX} characters ({len(text)})")
        return _ran([verb, name, text])
    if verb == "tool":
        from .broker import SPECS
        tool, to = form.get("tool", ""), form.get("to", "")
        if tool not in SPECS or to not in ("on", "off"):
            raise Refused(f"no tool switch {tool!r} {to!r}")
        return _ran(["tools", name, to, tool])
    if verb == "aa-trial":
        return _ran(["trial", "run", name, "--aa", "--hypothesis", "A/A calibration (from the web UI)"])
    if verb == "battery":
        if why := actions.battery_refusal(name):
            raise Refused(why)
        return _started(["battery", "run", name])
    raise Refused(f"no action {verb!r}")


# -- what the pages show ---------------------------------------------------------------------------------------------
def button(verb: str, label: str, confirm: str = "", fields: dict | None = None, disabled: str = "",
           cls: str = "") -> Html:
    """One action as a button. `disabled`: why it cannot run now (shown as its tooltip, and the button is off)."""
    hidden = join(h('<input type="hidden" name="{k}" value="{v}">', k=k, v=v) for k, v in (fields or {}).items())
    if disabled:
        return h('<button type="button" class="act {cls}" disabled title="{why}">{label}</button>', cls=cls, why=disabled,
                 label=label)
    conf = h(' data-confirm="{c}"', c=confirm[0].upper() + confirm[1:]) if confirm else Html("")
    return h('<form class="act" data-post="/action/{verb}"{conf}>{hidden}<button type="submit" class="act {cls}">'
             '{label}</button></form>', verb=verb, conf=conf, hidden=hidden, cls=cls, label=label)


_updates: dict[str, tuple[float, tuple[str, str]]] = {}


def _update_question(name: str) -> tuple[str, str]:
    """actions.update_question, kept 30 s: it asks git, and the buttons are redrawn every 2 s."""
    import time
    hit = _updates.get(name)
    if not hit or time.monotonic() - hit[0] > 30:
        try:
            answer = actions.update_question(name)
        except (Exception, SystemExit) as e:     # one instance's git state must not take the whole page down
            answer = ("", f"cannot tell whether {name} is behind the template: {e}")
        hit = _updates[name] = (time.monotonic(), answer)
    return hit[1]


def instance_buttons(i: dict) -> Html:
    """The instance actions of the overview and the instance page (TUI keys c, p, s, u)."""
    name, state = i["name"], i["state"]
    idle = state == "idle"
    paused = state == "paused by you"
    a = actions
    question, nothing = _update_question(name)
    out = [button("cycle", "Start a cycle", f"start a cycle for {name} now?", {"name": name},
                  "" if idle else f"{name} is {state} — a cycle starts only when it is idle"),
           button("resume" if paused else "pause", "Resume" if paused else "Pause",
                  f"resume {name}?" if paused else f"pause {name}? (a running cycle is asked to stop at its next turn)",
                  {"name": name}),
           button("sleep", "Sleep now", a.SLEEP_QUESTION.format(name=name), {"name": name},
                  "" if idle else f"{name} is {state} — a sleep starts only when it is idle"),
           button("update", "Update", question, {"name": name}, nothing)]
    return h('<div class="actions">{b}</div>', b=join(out))


def message_forms(name: str, question: dict | None) -> Html:
    """Write to the instance (TUI w), and reply to its oldest open question (TUI r)."""
    write = h('<form class="msg" id="write" data-post="/action/send" data-reset="1" data-confirm="Send this to {n}? It '
              'reads it at its next cycle or conversation."><input type="hidden" name="name" value="{n}">'
              '<label>Message to {n}<textarea name="text" rows="3" maxlength="{max}" required></textarea></label>'
              '<button type="submit">Send</button></form>', n=name, max=TEXT_MAX)
    if not question:
        return write
    ask = " ".join(question["body"].split())
    reply = h('<form class="msg" data-post="/action/reply" data-reset="1" data-confirm="Send this reply to {n}?">'
              '<input type="hidden" name="name" value="{n}"><label>Reply to {n}\'s question <span class="dim">'
              '({date})</span>: <i>{q}</i><textarea name="text" rows="3" maxlength="{max}" required></textarea>'
              '</label><button type="submit">Reply</button></form>', n=name, date=question["date"][:16],
              q=ask[:300] + ("…" if len(ask) > 300 else ""), max=TEXT_MAX)
    return join([reply, write])


def tool_switches(name: str, off: set[str]) -> Html:
    """Every supervisor tool with its switch (TUI t)."""
    from .broker import SPECS
    rows = []
    for tool in sorted(SPECS):
        to = "on" if tool in off else "off"
        rows.append(h('<tr><td data-label="tool">{t}</td><td data-label="now" class="{c}">{now}</td>'
                      '<td data-label="">{b}</td></tr>', t=tool, c="bad" if tool in off else "good",
                      now="off" if tool in off else "on",
                      b=button("tool", f"Switch {to}", f"switch {tool} {to} for {name}? (from its next run)",
                               {"name": name, "tool": tool, "to": to})))
    return h('<div class="tablewrap"><table><thead><tr><th>tool</th><th>now</th><th></th></tr></thead><tbody>{r}'
             '</tbody></table></div>', r=join(rows))


def alert_buttons(alerts: list[dict]) -> Html:
    shown = alerts[:5]
    if not shown:
        return Html("")
    return button("dismiss", "Dismiss these alerts", "", {"ids": json.dumps([a["id"] for a in shown])}, cls="small")


def service_buttons(name: str, running: bool, scheduled: list[str], busy: list[str]) -> Html:
    if name in NOT_FROM_HERE:
        return h('<span class="dim">{w}</span>', w=NOT_FROM_HERE[name])
    why_off = actions.guard_reason(name, "off", scheduled, busy)
    why_restart = actions.guard_reason(name, "restart", scheduled, busy)
    verb = "Restart" if running else "Start"
    return join([button("service", verb, f"{verb.lower()} {name}?", {"service": name, "op": "on"},
                        f"not now: {why_restart}" if why_restart else "", cls="small"),
                 button("service", "Stop", f"stop {name}?", {"service": name, "op": "off"},
                        "" if running and not why_off else (f"not now: {why_off}" if why_off else "not running"),
                        cls="small")])


def autonomy_button(scheduler_running: bool, scheduled: list[str]) -> Html:
    op = "off" if scheduler_running else "on"
    why = actions.guard_reason("autonomy", op, scheduled, [])
    effect = "no more scheduled wakes or sleeps" if op == "off" else "wakes and the nightly sleep resume"
    return button("autonomy", f"Autonomy {op}", f"autonomy {op}? ({effect})", {"op": op},
                  f"not now: {why}" if why else "")

"""The web UI's actions, at parity with `shaft top`'s: each runs the same CLI subcommand through
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
import re

from . import actions
from .web_pages import Html, h, join

WHO = "web"
TEXT_MAX = 8000                       # a message to an instance, at most (it goes into its inbox as a file)
KILL_WAIT_S = 20                      # a kill from here waits this long for its runs to settle (the page waits 30 s)
DELETABLE = ("idle", "paused by you")
NOT_FROM_HERE = {"web": "the web UI cannot stop or restart itself: shaft web on|off in a terminal"}


class Refused(Exception):
    """An action the panel or its CLI command refuses; the message says why."""


def _ok(text: str) -> tuple[int, str]:
    return 200, text


def _ran(args: list[str], timeout: float = actions.RUN_NOW_S) -> tuple[int, str]:
    ok, last = actions.run_now(args, timeout, who=WHO)
    return (200, last) if ok else (409, last if last.startswith("refused") else f"refused: {last}")


def pause_resume(verb: str, name: str, who: str = WHO) -> tuple[int, str]:
    """The web and native apps share the pause/resume command path."""
    ok, last = actions.run_now([verb, name], actions.RUN_NOW_S, who=who)
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


def _guard(form: dict, args: list[str], why: str) -> list[str]:
    """A service guard: refused unless the operator forced it (the "Force …" button, asked first), which is logged.
    The args to run: a forced action carries --force, since the CLI holds it back too."""
    if why and form.get("force") != "1":
        raise Refused(f"not now: {why}")
    if why:
        actions.log_forced(WHO, args, why)
        return args + [actions.FORCE]
    return args


def run(verb: str, form: dict, snap: dict, names: list[str], services: list[str]) -> tuple[int, str]:
    """Do one action: (HTTP status, what to tell the operator)."""
    try:
        return _run(verb, form, snap, names, services)
    except Refused as e:
        return 409, str(e)


def _run(verb: str, form: dict, snap: dict, names: list[str], services: list[str]) -> tuple[int, str]:
    if verb in ("calendar-approve", "calendar-decline", "calendar-overreach"):
        from . import calendar_proposals
        try:
            row = (calendar_proposals.reject(form.get("id", ""), 'over-reach') if verb == 'calendar-overreach' else
                   calendar_proposals.decide(form.get("id", ""), verb == "calendar-approve"))
        except ValueError as e:
            raise Refused(str(e)) from None
        return (409 if row['state'] == 'failed' else 200), f"Proposal {row['id']}: {row['state']} ({row['outcome']})"
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
        args = ["autonomy" if svc == "scheduler" else svc, op]
        from . import autonomy
        if svc == "api" and op == "on" and autonomy.installed("api"):
            args = ["api", "restart"]           # as it is: `api on` would run tailscale serve and add logins
        running = any(s["name"] == svc and s["running"] for s in snap.get("services", []))
        args = _guard(form, args, actions.service_guard(svc, op if op == "off" else "restart") if running else "")
        if svc in ("api", "scheduler"):
            lock = actions.restart_lock(svc)
            if not lock.acquire(blocking=False):
                raise Refused(f"{svc} is already restarting")
            try:
                return _ran(args, timeout=actions.SLOW_S)
            finally:
                lock.release()
        return _ran(args, timeout=actions.SLOW_S)
    if verb == "autonomy":
        op = form.get("op", "")
        if op in ("restart", "cancel-restart"):   # a drain-restart cuts no run: no guard (autonomy.restart)
            return _ran(["autonomy", "restart"] + (["--cancel"] if op == "cancel-restart" else []))
        if op not in ("on", "off"):
            raise Refused(f"autonomy {op!r}?")
        args = _guard(form, ["autonomy", op], actions.service_guard("autonomy", op))
        return _ran(args, timeout=actions.SLOW_S)
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
        return pause_resume(verb, name)
    if verb == "kill":
        ok, last = actions.run_now(["kill", name, "--wait", str(KILL_WAIT_S)], KILL_WAIT_S + 8, who=WHO)
        return 200, last   # still settling after the wait is no refusal: it is paused, and its runs end on their own
    if verb == "delete":
        if form.get("confirm", "").strip() != name:
            raise Refused(f"not deleted: type {name} to confirm")
        from . import kill
        if left := kill.running(name):
            raise Refused(f"{name} is busy ({'; '.join(left)}): kill it first")
        actions.run_detached(["delete", name, "--yes"], who=WHO)
        return 200, (f"deleting {name}: it is archived to .supervisor/deleted/ and removed; it leaves the list when "
                     f"done (output in .supervisor/{actions.log_name(WHO)})")
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
# Past the server's limit, so an action that uses the whole of it still gets its answer back to the page.
ANSWER_GRACE_S = 15

# Where a lost answer is visible. The page names it, and does not claim the action was not sent.
CHECK = {"calendar-approve": "Action approvals", "calendar-decline": "Action approvals", "calendar-overreach": "Action approvals", "cycle": "Overview", "pause": "Overview", "resume": "Overview", "sleep": "Overview", "update": "Overview",
         "recap": "Overview", "dismiss": "Overview", "tool": "Access", "aa-trial": "Trials", "service": "Services",
         "autonomy": "Services", "config-set": "Config", "config-unset": "Config", "drive-set": "Drive",
         "drive-remove": "Drive", "limit": "Access", "jobs": "Access", "share": "Access", "mail": "Access",
         "calendar": "Access", "gdrive": "Access", "gchat": "Access", "battery": "Access", "battery-switch": "Access", "settings-set": "Settings",
         "settings-unset": "Settings", "chat-stop": "this chat", "chat-end": "this chat", "orders-add": "Config",
         "orders-drop": "Config"}


def page_wait(server_s: float) -> int:
    """Seconds the browser waits for one action: the server's own limit, plus a grace for the answer to arrive."""
    return int(server_s) + ANSWER_GRACE_S


def server_limit(verb: str) -> float:
    """How long this action's command is allowed to run. Service and autonomy on/off are the slow ones."""
    return actions.SLOW_S if verb in ("service", "autonomy") else actions.RUN_NOW_S


def post_attrs(where: str, server_s: float | None = None) -> Html:
    """data-check, and data-timeout when the server waits: seconds, its limit plus a grace."""
    timeout = Html("") if server_s is None else h(' data-timeout="{t}"', t=page_wait(server_s))
    return timeout + h(' data-check="{w}"', w=where)


def button(verb: str, label: str, confirm: str = "", fields: dict | None = None, disabled: str = "",
           cls: str = "", typed: str = "", after: str = "") -> Html:
    """One action as a button. `disabled`: why it cannot run now (shown as its tooltip, and the button is off).
    `typed`: the word the operator must type to confirm (sent as the `confirm` field, checked again by the server).
    `after`: what the page does once it succeeded (web.js: "home" goes to the overview)."""
    hidden = join(h('<input type="hidden" name="{k}" value="{v}">', k=k, v=v) for k, v in (fields or {}).items())
    if disabled:
        return h('<button type="button" class="act {cls}" disabled title="{why}">{label}</button>', cls=cls, why=disabled,
                 label=label)
    conf = h(' data-confirm="{c}"', c=confirm[0].upper() + confirm[1:]) if confirm else Html("")
    if typed:
        conf = join([conf, h(' data-confirm-type="{t}"', t=typed)])
        hidden = join([hidden, Html('<input type="hidden" name="confirm" value="">')])
    if after:
        conf = join([conf, h(' data-after="{a}"', a=after)])
    extra = post_attrs(CHECK.get(verb, "this page"), server_limit(verb))
    return h('<form class="act" data-post="/action/{verb}"{conf}{extra}>{hidden}<button type="submit" class="act {cls}">'
             '{label}</button></form>', verb=verb, conf=conf, extra=extra, hidden=hidden, cls=cls, label=label)


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


def instance_buttons(i: dict, delete: bool = False, kill: bool = True, split: bool = False):
    """The instance actions of the overview and the instance page (TUI keys c, p, s, u, K); `delete`: also Delete…
    (TUI D) — on the instance's own page only. Kill and Delete come last, apart (.dangerzone): beside Pause on every
    card they were one slip from a stop (iPhone review I13). `kill`: False leaves Kill off (the overview's cards: it
    stays on the instance's Summary and in its ⋯ menu). `split`: the two groups apart, (actions, danger), for a menu
    that puts its own items between them."""
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
    danger = [button("kill", "Kill", a.KILL_QUESTION.format(name=name), {"name": name}, cls="danger")] if kill else []
    if delete:
        danger.append(button("delete", "Delete…", a.DELETE_QUESTION.format(name=name), {"name": name},
                          "" if state in DELETABLE else f"{name} is {state} — kill it first", cls="danger",
                          typed=name, after="home"))
    zone = h('<div class="actions dangerzone">{d}</div>', d=join(danger)) if danger else Html("")
    main = h('<div class="actions">{b}</div>', b=join(out))
    return (main, zone) if split else join([main, zone])


def message_forms(name: str, question: dict | None) -> Html:
    """Write to the instance (TUI w), and reply to its oldest open question (TUI r)."""
    write = h('<form class="msg" id="write" data-post="/action/send" data-reset="1" data-confirm="Send this to {n}? It '
              'reads it at its next cycle or conversation."{a}><input type="hidden" name="name" value="{n}">'
              '<label>Message to {n}<textarea name="text" rows="3" maxlength="{max}" required></textarea></label>'
              '<button type="submit">Send</button></form>', n=name, max=TEXT_MAX,
              a=post_attrs("Messages", actions.RUN_NOW_S))
    if not question:
        return write
    ask = " ".join(question["body"].split())
    reply = h('<form class="msg" data-post="/action/reply" data-reset="1" data-confirm="Send this reply to {n}?"{a}>'
              '<input type="hidden" name="name" value="{n}"><label>Reply to {n}\'s question <span class="dim">'
              '({date})</span>: <i>{q}</i><textarea name="text" rows="3" maxlength="{max}" required></textarea>'
              '</label><button type="submit">Reply</button></form>', n=name, date=question["date"][:16],
              q=ask[:300] + ("…" if len(ask) > 300 else ""), max=TEXT_MAX,
              a=post_attrs("Messages", actions.RUN_NOW_S))
    return join([reply, write])


def tool_switches(name: str, off: set[str]) -> Html:
    """Every supervisor tool with its switch (TUI t)."""
    from .broker import SPECS
    rows = [tool_row(name, tool, off) for tool in sorted(SPECS)]
    return h('<div class="tablewrap"><table><thead><tr><th>tool</th><th>now</th><th></th></tr></thead><tbody>{r}'
             '</tbody></table></div>', r=join(rows))


_ALERT_LINE = re.compile(r"^(\d\d:\d\d(?::\d\d)?) ([\w.-]+): (.+)$", re.S)


_TOKEN = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+|exit|code)=(\{[^{}]*\}|\[[^\[\]]*\]|'[^']*'|\"[^\"]*\"|[^\s,]+)")


def _readable(text: str) -> str:
    """A log line's machine tokens in plain type, its words unchanged (iPad review P5): broker_calls={'run': 2,
    'web_search': 2} → broker calls: run 2, web search 2; finish_reason='stop' → finish reason: stop; exit=1 → exit 1."""
    def value(v: str) -> str:
        v = v.strip()
        if v[:1] in "{[":
            parts = [p.strip().strip("'\"") for p in v[1:-1].split(",") if p.strip()]
            return ", ".join(p.replace("': ", " ").replace("\": ", " ").replace(": ", " ").replace("'", "")
                             .replace("_", " ") for p in parts) or "none"
        return v.strip("'\"")
    def one(m: re.Match) -> str:
        key = m[1].replace("_", " ")
        return f"{key} {m[2]}" if m[1] in ("exit", "code") else f"{key}: {value(m[2])}"
    return _TOKEN.sub(one, text)


def alert_line(a: dict) -> Html:
    """One open alert and the action that dismisses it alone. `ids` is a JSON list of that one id — the same field
    `dismiss` reads, a list because a log alert's id has a space in its time. A log line ("07:31:38 shaft: end live
    exit=1 broker_calls={...}") shows its time and who apart and its first 90 characters, the rest behind a tap: all of it
    in bold red, with Python dicts, was a wall on a phone (iPhone review I15). The words are the log's own."""
    text = a["text"]
    m = _ALERT_LINE.match(text)
    when, who, what = (m.groups() if m else ("", "", text))
    what = _readable(what)
    head = what if len(what) <= 90 else what[:89].rstrip() + "…"
    full = h('<details class="alertfull"><summary>Full line</summary><pre>{t}</pre></details>', t=text) \
        if len(what) > 90 else Html("")
    return h('<li><span class="alerthead">⚠ {w}{who}<span class="alertwhat">{h}</span></span>{full}{b}</li>',
             w=h('<span class="dim">{t}</span> ', t=when) if when else "", who=h('<b>{n}</b> ', n=who) if who else "",
             h=head, full=full, b=button("dismiss", "Dismiss", "", {"ids": json.dumps([a["id"]])}, cls="small"))


def tool_row(name: str, tool: str, off: set[str]) -> Html:
    to = "on" if tool in off else "off"
    return h('<tr data-row><td data-label="tool">{t}</td><td data-label="now" class="{c}">{now}</td>'
             '<td data-label="">{b}</td></tr>', t=tool, c="bad" if tool in off else "good",
             now="off" if tool in off else "on",
             b=button("tool", f"Switch {to}", f"switch {tool} {to} for {name}? (from its next run)",
                      {"name": name, "tool": tool, "to": to}))


def saved_row(verb: str, form: dict) -> Html:
    """Read back the saved control, including its next comparison token and button direction."""
    from . import opconfig, web_config, web_settings, web_views
    from .core import disabled_tools
    name = form.get("name", "")
    if verb in web_settings.VERBS:
        field = next(f for f in web_settings.listing()["settings"] if f["name"] == form["key"].strip())
        return web_settings._row(field)
    if verb in ("config-set", "config-unset"):
        key = form["key"].strip()
        fields = {f["key"]: f for f in opconfig.fields(name)}
        # Unsetting a custom key removes it from fields(); keep an empty row so it can be set again.
        field = fields.get(key) or dict(key=key, set=False, value="", default=None, help="", secret=False,
                                        choices=None, numeric=False)
        # A model switch also writes LLM_BASE_URL: its row's saw goes with it, or its next save is refused
        return join([web_config.config_row(name, field)] +
                    [web_config.config_row(name, fields[k]) for k in opconfig.coupled(key) if k in fields])
    if verb in ("drive-set", "drive-remove"):
        text, sha = opconfig.drive(name)
        return web_config.drive_page(name, text, sha, web_views.markdown(text) if text is not None else Html(""))
    if verb == "tool":
        return tool_row(name, form["tool"], disabled_tools(name))
    if verb in ("orders-add", "orders-drop"):
        return web_config.orders_panel(name)
    if verb in web_config.VERBS:
        row = form["limit"] if verb == "limit" else verb
        return web_config.access_page(name, web_config.access(name), row)
    raise ValueError(f"no saved row for {verb}")


def alert_buttons(alerts: list[dict]) -> Html:
    """Dismiss the five shown, and — when more are open — all of them, asked first: those include alerts not on
    screen. Live, 2026-10-20: 60 lookalike log alerts took 12 clicks of "Dismiss these alerts", each showing the next
    five, so the list never seemed to change. Each alert also has its own Dismiss (alert_line)."""
    shown = alerts[:5]
    if not shown:
        return Html("")
    these = button("dismiss", "Dismiss these alerts", "", {"ids": json.dumps([a["id"] for a in shown])}, cls="small")
    if len(alerts) <= len(shown):
        return these
    n = len(alerts)
    return join([these, button("dismiss", f"Dismiss all {n}", f"dismiss all {n} open alerts, the {n - len(shown)} "
                               "not shown too?", {"ids": json.dumps([a["id"] for a in alerts])}, cls="small")])


def service_buttons(name: str, running: bool, scheduled: list[str], busy: list[str]) -> Html:
    if name in NOT_FROM_HERE:
        return h('<span class="dim">{w}</span>', w=NOT_FROM_HERE[name])
    # Only a running service can kill or cut off a run: starting a stopped one (the model server between uses, which
    # the runs waiting on it need) is never held back.
    why_off = actions.guard_reason(name, "off", scheduled, busy) if running else ""
    why_restart = actions.guard_reason(name, "restart", scheduled, busy) if running else ""
    verb = "Restart" if running else "Start"
    stop = guarded("service", "Stop", f"stop {name}?", {"service": name, "op": "off"},
                   actions.force_question(name, "off", why_off) if why_off else "", cls="small") \
        if running else button("service", "Stop", "", {}, "not running", cls="small")
    restart = guarded("service", verb, f"{verb.lower()} {name}?", {"service": name, "op": "on"},
                      actions.force_question(name, "restart", why_restart) if why_restart else "", cls="small")
    if name == "scheduler" and running:
        from .autonomy import drain_line
        if (pending := drain_line()):   # a drain-restart is under way: say what it waits for, and offer to withdraw
            return join([h('<span class="warn">{t}</span>', t=pending),
                         button("autonomy", "Cancel restart", "withdraw the scheduler's restart? It resumes "
                                "scheduling at its next tick.", {"op": "cancel-restart"}, cls="small"), restart, stop])
        if why_restart:                 # held back: a restart once the runs end cuts none (docs/deploy.md step 3)
            return join([button("autonomy", "Restart when idle", "restart the scheduler once its runs end? Nothing "
                                "new starts until then, and none is cut.", {"op": "restart"}, cls="small"),
                         restart, stop])
    return join([restart, stop])


def guarded(verb: str, label: str, confirm: str, fields: dict, force: str, cls: str = "") -> Html:
    """An action a guard may hold back. `force`: the question to override it (actions.force_question), empty when
    nothing holds it back. Held back, it is offered as "Force …", asked first with what it would hurt — the operator
    may still need it (live, 2026-10-20: two live cycles kept Restart api and Restart scheduler greyed out, with no
    way to restart from the page)."""
    if not force:
        return button(verb, label, confirm, fields, cls=cls)
    return button(verb, f"Force {label[0].lower()}{label[1:]}", force, {**fields, "force": "1"},
                  cls=f"{cls} force".strip())


def autonomy_button(scheduler_running: bool, scheduled: list[str]) -> Html:
    op = "off" if scheduler_running else "on"
    why = actions.guard_reason("autonomy", op, scheduled, [])
    effect = "no more scheduled wakes or sleeps" if op == "off" else "wakes and the nightly sleep resume"
    return guarded("autonomy", f"Autonomy {op}", f"autonomy {op}? ({effect})", {"op": op},
                   actions.force_question("autonomy", op, why) if why else "")

"""The web UI's Config, Drive and Access pages and their actions (docs/plans/PLAN-webui.md M3).

  Config  the instance's config.env, one field per key, saved through `shaft config` (opconfig.py): only that line
          changes, a value changed since the page was drawn is refused, a model switch sets its LLM_BASE_URL too.
  Drive   drive.md, shown and edited whole, saved through `shaft drive` after a diff.
  Access  what the operator grants it, each through its own CLI verb: supervisor tools, the wake floor and cloud
          cap (shaft limits), jobs, the shared folder, mail labels, calendars, the battery; its secret *names*.

As everywhere in the web UI, an action runs the CLI subcommand (actions.py), so it inherits that command's checks
and refusals, and is logged as `web: operator …`. Secret values never pass through here.
"""
from __future__ import annotations

import difflib
import secrets as _secrets

from . import actions
from .core import SUP_DIR
from .web_actions import Refused, button, post_attrs
from .web_pages import Html, h, join

WHO = "web"
VERBS = ("config-set", "config-unset", "drive-set", "drive-remove", "limit", "jobs", "share", "mail", "calendar",
         "battery-switch", "channel")
DRIVE_MAX = 20000


def _ran(args: list[str], timeout: float = actions.RUN_NOW_S) -> tuple[int, str]:
    ok, last = actions.run_now(args, timeout, who=WHO)
    return (200, last) if ok else (409, last if last.startswith("refused") else f"refused: {last}")


def run(verb: str, name: str, form: dict) -> tuple[int, str]:
    """One M3 action for instance `name` (already checked): (HTTP status, what to tell the operator)."""
    if verb in ("config-set", "config-unset"):
        key = form.get("key", "").strip()
        if not key:
            raise Refused("which key?")
        args = (["config", name, "set", f"{key}={form.get('value', '')}"] if verb == "config-set"
                else ["config", name, "unset", key])
        if "saw" in form:                                   # what the page showed: refused if it changed since
            args += ["--expect", f"{key}={form['saw']}"] if form.get("saw_set") == "1" else ["--expect-unset", key]
        return _ran(args)
    if verb == "drive-remove":
        return _ran(["drive", name, "remove", "--expect-sha", form.get("sha", "")])
    if verb == "drive-set":
        text = form.get("text", "")
        if not text.strip():
            raise Refused("an empty drive: use Remove instead (the default drive applies)")
        if len(text) > DRIVE_MAX:
            raise Refused(f"a drive is at most {DRIVE_MAX} characters ({len(text)})")
        tmp = SUP_DIR / "web" / f"drive-{_secrets.token_hex(6)}.md"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text.replace("\r\n", "\n"))
        try:
            return _ran(["drive", name, "set", "--file", str(tmp), "--expect-sha", form.get("sha", "")])
        finally:
            tmp.unlink(missing_ok=True)
    if verb == "limit":
        which, value = form.get("limit", ""), form.get("value", "").strip()
        if which not in ("wake-floor", "cloud-cap") or not value:
            raise Refused(f"no limit {which!r} {value!r}")
        return _ran(["limits", name, which, value])
    if verb == "jobs":
        if form.get("op") not in ("on", "off"):
            raise Refused("jobs on or off?")
        return _ran(["jobs", name, form["op"]])
    if verb == "channel":
        value = form.get("value", "")
        if value not in ("auto", "web", "telegram"):
            raise Refused(f"no channel {value!r}: auto, web or telegram")
        return _ran(["channel", name, value])
    if verb == "share":
        folder = form.get("folder", "").strip()
        if not folder:
            raise Refused("which folder? (or Stop sharing)")
        return _ran(["share", name, folder])
    if verb in ("mail", "calendar"):
        op = form.get("op", "")
        if op == "off":
            return _ran([verb, name, "off"])
        alias, what = form.get("alias", "").strip(), form.get("what", "").strip()
        if op not in ("grant", "revoke") or not alias or not what:
            raise Refused(f"{verb} {op} needs an account and a {'label' if verb == 'mail' else 'calendar'}")
        return _ran([verb, name, op, alias, what])
    if verb == "battery-switch":
        if form.get("op") not in ("on", "off"):
            raise Refused("battery on or off?")
        return _ran(["battery", form["op"], name])
    raise Refused(f"no action {verb!r}")


# -- config ----------------------------------------------------------------------------------------------------------
def _field_form(name: str, f: dict) -> Html:
    if f["secret"]:
        return h('<span class="dim">a secret: <code>shaft secret {n} …</code> in your terminal</span>', n=name)
    fields = {"name": name, "key": f["key"], "saw": f["value"] if f["set"] else "", "saw_set": "1" if f["set"] else "0"}
    hidden = join(h('<input type="hidden" name="{k}" value="{v}">', k=k, v=v) for k, v in fields.items())
    if f["choices"]:
        opts = list(f["choices"]) + ([f["value"]] if f["set"] and f["value"] not in f["choices"] else [])
        control = h('<select name="value" aria-label="{k} value">{o}</select>', k=f["key"], o=join(
            h('<option value="{v}"{s}>{v}</option>', v=o, s=Html(" selected") if o == f["value"] else "") for o in opts))
    else:
        control = h('<input name="value" value="{v}" {num} maxlength="500" aria-label="{k} value">', v=f["value"],
                    k=f["key"], num=Html('inputmode="numeric" pattern="[0-9]+"') if f["numeric"] else "")
    extra = (Html(" It also sets LLM_BASE_URL to match.") if f["key"] == "LLM_MODEL" else "")
    save = h('<form class="cfg" data-post="/action/config-set" data-confirm="Set {k} for {n}? Only that line of its '
             'config.env changes; it takes effect from its next run.{x}"{a}>{hidden}{control}<button type="submit" '
             'class="small">Save</button></form>', k=f["key"], n=name, x=extra, hidden=hidden, control=control,
             a=post_attrs("Config", actions.RUN_NOW_S))
    unset = (button("config-unset", "Unset", f"remove {f['key']} from {name}'s config.env? "
                    + (f"The default ({f['default']}) applies." if f["default"] is not None else "Its code's default applies."),
                    {k: v for k, v in fields.items() if k != "value"}, cls="small") if f["set"] else Html(""))
    return join([save, unset])


def config_row(name: str, f: dict) -> Html:
    shown = f["value"] if f["set"] else h('<span class="dim">not set · default {d}</span>',
                                         d=f["default"] if f["default"] is not None else "from its code")
    return h('<tr data-row id="config-{k}"><td data-label="key"><code>{k}</code></td><td data-label="now">{v}</td>'
             '<td data-label="change">{form}</td><td data-label="what it is" class="dim help">{help}</td></tr>',
             k=f["key"], v=shown, form=_field_form(name, f), help=f["help"])


def config_page(name: str, fs: list[dict], history: list[str]) -> Html:
    rows = [config_row(name, f) for f in fs]
    table = h('<div class="tablewrap"><table class="config"><thead><tr><th>key</th><th>now</th><th>change</th>'
              '<th>what it is</th></tr></thead><tbody id="config-rows">{r}</tbody></table></div>', r=join(rows))
    add = h('<form class="cfg add" data-post="/action/config-set" data-add-row="#config-rows" data-reset="1" '
            'data-confirm="Add this key to {n}\'s '
            'config.env?"{a}><input type="hidden" name="name" value="{n}"><input name="key" placeholder="NEW_KEY" '
            'pattern="[A-Z][A-Z0-9_]*" required aria-label="key"><input name="value" placeholder="value" '
            'maxlength="500" aria-label="value"><input type="hidden" name="saw" value=""><input type="hidden" '
            'name="saw_set" value="0"><button type="submit" class="small">Add</button></form>', n=name,
            a=post_attrs("Config", actions.RUN_NOW_S))
    hist = (h('<ul class="lines">{li}</ul>', li=join(h("<li>{x}</li>", x=x) for x in history)) if history
            else Html('<p class="dim">no commits touch it yet</p>'))
    return join([h('<section class="panel"><h2>config.env</h2><p class="dim">Saved through <code>shaft config</code>: '
                   'only that line changes, and it is committed as yours (now if {n} is free, by its run otherwise). '
                   'A value that changed since this page was drawn is refused.</p>{t}<h3>Add a key</h3>{a}</section>',
                   n=name, t=table, a=add),
                 h('<section class="panel"><h2>Recent changes (git log of config.env: its own set_config lands '
                   'in its runs\' commits, yours in "changed from outside")</h2>{h}</section>', h=hist)])


# -- drive -----------------------------------------------------------------------------------------------------------
def drive_diff(old: str | None, new: str) -> str:
    lines = list(difflib.unified_diff((old or "").splitlines(), new.replace("\r\n", "\n").rstrip("\n").splitlines(),
                                      "drive.md now", "drive.md after", lineterm=""))
    return "\n".join(lines) or "(no change)"


def drive_page(name: str, text: str | None, sha: str, rendered: Html) -> Html:
    current = (rendered if text is not None else
               h('<p class="dim">No drive.md: {n} has the drive every SHAFT instance starts with.</p>', n=name))
    edit = h('<form class="drive" data-post="/action/drive-set" data-diff="/preview/drive/{n}"{a}>'
             '<input type="hidden" name="name" value="{n}"><input type="hidden" name="sha" value="{sha}">'
             '<label>drive.md for {n}<textarea name="text" rows="14" maxlength="{max}" '
             'aria-label="drive.md for {n}">{t}</textarea></label>'
             '<div class="actions" data-diff-ask><button type="submit">Show the diff and save</button></div>'
             '<div class="diffstep" data-diff-step hidden><pre class="diff" data-diff-out></pre>'
             '<p class="dim">Apply replaces {n}\'s drive.md. It reads it from its next run, and its journal says its '
             'purpose changed.</p>'
             '<div class="actions"><button type="submit" data-diff-apply>Apply</button>'
             '<button type="button" data-diff-back>Back</button></div></div></form>',
             n=name, sha=sha, t=text or "", max=DRIVE_MAX, a=post_attrs("Drive", actions.RUN_NOW_S))
    remove = (button("drive-remove", "Remove drive.md", f"remove {name}'s drive.md? The default drive applies from "
                     "its next run.", {"name": name, "sha": sha}, cls="small") if text is not None else Html(""))
    return h('<div data-row>{body}</div>', body=join([
                 h('<section class="panel"><h2>What {n} is for (drive.md, as it reads it)</h2>{c}</section>', n=name,
                   c=current),
                 h('<section class="panel"><h2>Change it</h2><p class="dim">Saved through <code>shaft drive</code>; '
                   'refused if drive.md changed since this page was drawn.</p>{e}{r}</section>', e=edit, r=remove)]))


# -- access ----------------------------------------------------------------------------------------------------------
def access_page(name: str, a: dict, row: str = "") -> Html:
    from .web_actions import tool_switches
    n = name
    floor = h('<div data-row>'
        '<div class="kv"><div class="row"><span class="label">wake floor</span><span>{wf} min {wfd}</span></div>'
        '</div>'
        '<div class="actions">'
        '<form class="act" data-post="/action/limit" data-confirm="Set {n}\'s wake floor? It may not wake more often '
        'than this, whatever its config.env says."{a}><input type="hidden" name="name" value="{n}"><input type="hidden" '
        'name="limit" value="wake-floor"><input name="value" inputmode="numeric" pattern="[0-9]+" placeholder="minutes" '
        'aria-label="wake floor in minutes" required><button type="submit" class="small">Set wake floor</button></form>'
        '{wfoff}</div></div>', n=n, wf=a["wake_floor"], wfd="(yours)" if a["wake_floor_own"] else "(default)",
        wfoff=button("limit", "Default floor", f"put {n}'s wake floor back to the default ({a['wake_floor_default']} min)?",
                     {"name": n, "limit": "wake-floor", "value": "off"}, "" if a["wake_floor_own"] else "already the default",
                     cls="small"),
        a=post_attrs("Access", actions.RUN_NOW_S))
    cap = h('<div data-row><div class="kv">'
        '<div class="row"><span class="label">cloud cap</span><span>${cap} a day {capd} · spent today ${spent}</span>'
        '</div></div><div class="actions">'
        '<form class="act" data-post="/action/limit" data-confirm="Set {n}\'s daily cloud cap? Its cloud calls stop '
        'for the day once it is spent."{a}><input type="hidden" name="name" value="{n}"><input type="hidden" '
        'name="limit" value="cloud-cap"><input name="value" inputmode="decimal" placeholder="dollars" '
        'aria-label="cloud cap in dollars" required><button type="submit" class="small">Set cloud cap</button></form>'
        '{capoff}</div></div>', n=n,
        cap=f"{a['cloud_cap']:.2f}", capd="(yours)" if a["cloud_cap_own"] else "(default)",
        spent=f"{a['spent_today']:.2f}",
        capoff=button("limit", "Default cap", f"put {n}'s cloud cap back to the default (${a['cloud_cap_default']:.2f})?",
                      {"name": n, "limit": "cloud-cap", "value": "off"}, "" if a["cloud_cap_own"] else "already the default",
                      cls="small"),
        a=post_attrs("Access", actions.RUN_NOW_S))
    jobs_op = "on" if a["jobs_off"] else "off"
    jobs = join([h('<p>Jobs are <b>{s}</b>.</p>', s="off" if a["jobs_off"] else "on"),
                 button("jobs", f"Switch jobs {jobs_op}", f"switch {n}'s scheduled jobs {jobs_op}?",
                        {"name": n, "op": jobs_op}, cls="small"),
                 h('<pre>{t}</pre>', t=a["jobs_text"])])
    share = join([h('<p>{s}</p>', s=f"{n} may read {a['shared']} as shared/ (read-only)" if a["shared"]
                    else f"{n} has no shared folder"),
                  h('<form class="act" data-post="/action/share" data-confirm="Let {n} read this folder (read-only, as '
                    'shared/), from its next run?"{a}><input type="hidden" name="name" value="{n}"><input name="folder" '
                    'placeholder="/Users/…/a folder" aria-label="folder" required size="40"><button type="submit" '
                    'class="small">Share</button></form>', n=n, a=post_attrs("Access", actions.RUN_NOW_S)),
                  button("share", "Stop sharing", f"stop sharing {a['shared']} with {n}?", {"name": n, "folder": "off"},
                         "" if a["shared"] else "nothing shared", cls="small")])
    accounts = a["accounts"]
    acct_opts = join(h('<option value="{x}">{x}</option>', x=x) for x in accounts)

    def grants(kind: str, items: list[dict], what_key: str, what_label: str) -> Html:
        rows = [h('<li>{acct}/{label} {b}</li>', acct=g["account"], label=g[what_label],
                  b=button(kind, "Revoke", f"revoke {n}'s {kind} grant {g['account']}/{g[what_label]}? From its next "
                           f"{kind} call.", {"name": n, "op": "revoke", "alias": g["account"], "what": g[what_key]},
                           cls="small")) for g in items]
        grant = (h('<form class="act" data-post="/action/{k}" data-confirm="Let {n} read this {thing}? The check is '
                   'made by the CLI."{a}><input type="hidden" name="name" value="{n}"><input type="hidden" name="op" '
                   'value="grant"><select name="alias" aria-label="account">{o}</select><input name="what" '
                   'placeholder="{ph}" aria-label="{thing}" required><button type="submit" class="small">Grant</button>'
                   '</form>', k=kind, n=n, o=acct_opts, thing="label" if kind == "mail" else "calendar",
                   ph="label" if kind == "mail" else "primary, a title or an ID",
                   a=post_attrs("Access", actions.RUN_NOW_S))
                 if accounts else h('<p class="dim">No Google account logged in: <code>shaft google login …</code> in '
                                    'your terminal.</p>'))
        off = button(kind, f"Revoke all {kind}", f"revoke every {kind} grant of {n}?", {"name": n, "op": "off"},
                     "" if items else "none granted", cls="small")
        return join([h('<ul class="lines">{r}</ul>', r=join(rows)) if rows else h('<p class="dim">none</p>'), grant, off])

    bat_op = "on" if a["battery_off"] else "off"
    battery = join([h('<p>The capability battery is <b>{s}</b> for {n}.</p>', s="off" if a["battery_off"] else "on",
                      n=n),
                    button("battery-switch", f"Switch the battery {bat_op}", f"switch the capability battery {bat_op} "
                           f"for {n}?", {"name": n, "op": bat_op}, cls="small")])
    keys = (h('<ul class="lines">{li}</ul>', li=join(h("<li>{k}</li>", k=k) for k in a["keys"])) if a["keys"]
            else h('<p class="dim">{n} has no keys of its own</p>', n=n))
    labels = {"auto": "Auto: where you last wrote to it", "web": "The web UI", "telegram": "Telegram"}
    reach = join([h('<p>Its messages to you notify you on: <b>{s}</b>. A message now would go to <b>{w}</b> — {why}.</p>'
                    '<p class="dim">Every message is in your web UI either way. While a conversation with {n} is open on '
                    'the web or Telegram, its messages alert you there.</p>', s=labels[a["channel"]], n=n,
                    w="the web UI" if a["channel_now"] == "web" else "Telegram", why=a["channel_why"]),
                  h('<div class="actions">{b}</div>', b=join(
                      button("channel", labels[v].split(":")[0], f"notify you of {n}'s messages on: {labels[v]}?",
                             {"name": n, "value": v}, "already set" if a["channel"] == v else "", cls="small")
                      for v in ("auto", "web", "telegram")))])
    if row in ("wake-floor", "cloud-cap"):
        return floor if row == "wake-floor" else cap
    sections = [("channel", "Where it reaches you (shaft channel)", reach),
                ("", "Supervisor tools (shaft tools)", tool_switches(n, set(a["tools_off"]))),
                ("", "Limits (shaft limits)", join([floor, cap])), ("jobs", "Scheduled jobs (shaft jobs)", jobs),
                ("share", "Shared folder (shaft share)", share),
                ("mail", "Your mail it may read (shaft mail)", grants("mail", a["mail"], "label", "label")),
                ("calendar", "Your calendars it may read (shaft calendar)", grants("calendar", a["calendar"], "calendar", "name")),
                ("battery-switch", "Capability battery", battery),
                ("", "Its own keys (shaft secret): names only", join([keys, h(
                    '<p class="dim">Set or delete a key in your terminal: <code>shaft secret {n} set &lt;key&gt; '
                    '--hosts …</code>. A value never passes through this page.</p>', n=n)]))]
    return join(h('<section class="panel"{swap}><h2>{t}</h2>{b}</section>',
                  swap=Html(" data-row") if key else "", t=t, b=b) for key, t, b in sections if not row or key == row)


def access(name: str) -> dict:
    """What the Access page shows, from the readers the CLI verbs use."""
    from . import autonomy, battery, cloudllm, google, jobs
    from .core import (calendar_grants, describe_key, disabled_tools, instance_root, mail_grants, read_secrets,
                       shared_folder)
    from . import channel
    floor = autonomy.wake_floor(name)
    cap = cloudllm.daily_cap(name)
    now, why = channel.route(name)
    return {"channel": channel.preference(name), "channel_now": now, "channel_why": why,
            "tools_off": sorted(disabled_tools(name)), "wake_floor": floor, "wake_floor_default": autonomy.WAKE_FLOOR_MIN,
            "wake_floor_own": floor != autonomy.WAKE_FLOOR_MIN, "cloud_cap": cap, "cloud_cap_default": cloudllm.DAILY_USD,
            "cloud_cap_own": cap != cloudllm.DAILY_USD, "spent_today": cloudllm.spent_today(name),
            "jobs_off": jobs.jobs_off(name), "jobs_text": jobs.describe(name), "shared": str(shared_folder(name) or ""),
            "mail": mail_grants(name), "calendar": calendar_grants(name), "accounts": sorted(google.accounts()),
            "battery_off": battery.off_path(name).exists(),
            "keys": [describe_key(k, e) for k, e in sorted(read_secrets(instance_root(name)).items())]}

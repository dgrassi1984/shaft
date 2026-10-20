"""The web UI's Settings page (docs/plans/PLAN-webui-chat.md §4, S1): the supervisor's settings, saved through `shaft settings`.

What the page shows comes from `shaft settings --json`, run as its own process: listing every knob imports every
module that declares one, and the web service must not import them just to list them — its own record of the knobs
it holds (version.record_service) would then name every knob, and every save would ask to restart the web UI.

A save runs `shaft settings set KEY=VALUE --expect …` with the value the page showed in the file, so a value changed
since is refused, and `--from-web`, so ports, interpreter paths and endpoints stay terminal-only. Until every service
holding the old value restarts, the page says "saved, not live yet" and offers the restart, with the Services page's
guard (refused while a run it would kill is in progress). Secrets: the .env key names only, never a value.
"""
from __future__ import annotations

import json
import subprocess

from . import actions
from .core import KNOB_GROUPS, PROJECT, secrets_env
from .web_actions import NOT_FROM_HERE, Refused, button, guarded, post_attrs
from .web_pages import Html, h, join

WHO = "web"
VERBS = ("settings-set", "settings-unset")
VALUE_MAX = 4000
LONG = 24                                                  # a value longer than this is edited in a textarea


def listing() -> dict:
    """`shaft settings --json`: {"settings": [...], "file": path}. Raises with what the command said if it fails."""
    r = subprocess.run(actions._cli(["settings", "--json"]), cwd=PROJECT, capture_output=True, text=True,
                       timeout=actions.RUN_NOW_S)
    if r.returncode != 0:
        why = (r.stderr or r.stdout).strip()[-300:]
        raise RuntimeError(f"shaft settings --json failed (exit {r.returncode}): {why}")
    return json.loads(r.stdout)


# -- actions ---------------------------------------------------------------------------------------------------------
def _ran(args: list[str]) -> tuple[int, str]:
    ok, last = actions.run_now(args, actions.RUN_NOW_S, who=WHO)
    return (200, last) if ok else (409, last if last.startswith("refused") else f"refused: {last}")


def _value(form: dict) -> str:
    """The value a field posted: its text, or for a list of models the ticked ones plus any typed in."""
    if "models[]" in form or form.get("kind") == "models":
        picked = [m.strip() for m in form.get("models[]", [])] + form.get("other", "").split(",")
        return ",".join(dict.fromkeys(m.strip() for m in picked if m.strip()))
    return form.get("value", "").strip()


def run(verb: str, form: dict) -> tuple[int, str]:
    key = form.get("key", "").strip()
    if not key.startswith("SHAFT_"):
        raise Refused("which setting?")
    expect = (["--expect", f"{key}={form.get('saw', '')}"] if form.get("saw_set") == "1" else ["--expect-unset", key]) \
        if "saw" in form or "saw_set" in form else []
    if verb == "settings-unset":
        return _ran(["settings", "unset", key, "--from-web", *expect])
    value = _value(form)
    if len(value) > VALUE_MAX:
        raise Refused(f"a value is at most {VALUE_MAX} characters")
    return _ran(["settings", "set", f"{key}={value}", "--from-web", *expect])


# -- the page --------------------------------------------------------------------------------------------------------
def _restart_buttons(services: list[str], scheduled: list[str], busy: list[str]) -> Html:
    out = []
    for svc in services:
        if svc in NOT_FROM_HERE:
            out.append(h('<span class="dim">{svc}: restart it from a terminal (<code>shaft web on</code>): the web UI '
                         'cannot restart itself</span>', svc=svc))
            continue
        why = actions.guard_reason(svc, "restart", scheduled, busy)
        out.append(guarded("service", f"Restart {svc}", f"restart {svc} now? It picks up the saved settings.",
                           {"service": svc, "op": "on"}, actions.force_question(svc, "restart", why) if why else "",
                           cls="small"))
    return join(out, " ")


def status(fs: list[dict], scheduled: list[str], busy: list[str]) -> Html:
    """Saved but not live yet: which settings, in which running services, and the restarts that apply them."""
    late = [f for f in fs if f["not_live"]]
    if not late:
        return h('<p class="good">Every running service holds the saved settings. Runs (cycles, sleeps, trials) read '
                 'them when they start.</p>')
    services = sorted({s for f in late for s in f["not_live"]})
    rows = join(h('<li><code>{n}</code>: restart {s}</li>', n=f["name"], s=", ".join(f["not_live"])) for f in late)
    return h('<div class="warn"><p><b>Saved, not live yet: restart {svcs}.</b> A service reads its settings when it '
             'starts; until then it runs on the value it started with.</p><ul>{rows}</ul>'
             '<div class="actions">{b}</div></div>', svcs=", ".join(services), rows=rows,
             b=_restart_buttons(services, scheduled, busy))


def _shown(f: dict) -> Html:
    src = {"file": "settings.env", "default": "default", "env": "its environment"}[f["source"]]
    value = f["value"] if f["value"] != "" else "(empty)"
    note = h(' <span class="warn">set in the web service\'s environment, which wins over the file</span>') \
        if f["source"] == "env" else Html("")
    return h('<code class="wrap">{v}</code> <span class="dim">[{src}]</span>{note}', v=value, src=src, note=note)


def _control(f: dict) -> Html:
    if f["kind"] == "models":
        now = [m.strip() for m in f["value"].split(",") if m.strip()]
        boxes = join(h('<label><input type="checkbox" name="models[]" value="{m}"{c}> {m}</label><br>', m=m,
                       c=Html(" checked") if m in now else "") for m in f["choices"])
        other = ",".join(m for m in now if m not in f["choices"])
        return h('<div class="models">{b}<label>other (comma-separated) <input name="other" value="{o}" '
                 'maxlength="{max}" aria-label="other models for {k}"></label></div><input type="hidden" name="kind" '
                 'value="models">', b=boxes, o=other, max=VALUE_MAX, k=f["name"])
    if f["kind"] == "model":
        opts = join(h('<option value="{m}">', m=m) for m in f["choices"])
        return h('<input name="value" value="{v}" list="models-{k}" maxlength="200" aria-label="{k}">'
                 '<datalist id="models-{k}">{o}</datalist>', v=f["value"], k=f["name"], o=opts)
    if f["kind"] == "int":
        return h('<input name="value" value="{v}" inputmode="numeric" pattern="[0-9]+" size="8" aria-label="{k}">',
                 v=f["value"], k=f["name"])
    if len(f["value"]) > LONG:                             # JSON, a list: a box as wide as the column, at any width
        return h('<textarea name="value" rows="3" maxlength="{max}" aria-label="{k}">{v}</textarea>', v=f["value"],
                 max=VALUE_MAX, k=f["name"])
    return h('<input name="value" value="{v}" maxlength="{max}" aria-label="{k}">', v=f["value"], max=VALUE_MAX,
             k=f["name"])


def _change(f: dict) -> Html:
    if not f["web"]:
        return h('<span class="dim">terminal only: <code>shaft settings set {k}=…</code></span>', k=f["name"])
    saw = {"key": f["name"], "saw": f["in_file"] or "", "saw_set": "1" if f["in_file"] is not None else "0"}
    hidden = join(h('<input type="hidden" name="{k}" value="{v}">', k=k, v=v) for k, v in saw.items())
    where = ", ".join(s for s in f["services"] if s != "runs")
    save = h('<form class="cfg" data-post="/action/settings-set" data-confirm="Save {k}? It goes '
             'in settings.env. Runs read it when they start{w}."{a}>{hidden}{control}<button type="submit" class="small">'
             'Save</button></form>', k=f["name"], hidden=hidden, control=_control(f),
             w=f"; running services that hold it ({where}) take it when they restart" if where else "",
             a=post_attrs("Settings", actions.RUN_NOW_S))
    reset = Html("") if f["in_file"] is None else button(
        "settings-unset", "Use the default", f"remove {f['name']} from settings.env? The default "
        f"({f['default'] or '(empty)'}) applies.", saw, cls="small")
    return join([save, reset])


def _row(f: dict) -> Html:
    default = Html("") if f["source"] == "default" else h(' <span class="dim">· default <code>{d}</code></span>',
                                                          d=f["default"] or "(empty)")
    return h('<tr data-row><td data-label="setting"><code>{k}</code></td><td data-label="now">{now}</td>'
             '<td data-label="change">{change}</td><td data-label="what it does" class="dim help">{help}{default} '
             '<span class="dim">· read by {by}</span></td></tr>', k=f["name"], now=_shown(f), change=_change(f),
             help=f["help"], default=default, by=", ".join(f["services"]))


def page_body(data: dict, scheduled: list[str], busy: list[str]) -> Html:
    from .web_views import section
    fs = data["settings"]
    groups = [g for g in KNOB_GROUPS if any(f["group"] == g for f in fs)]
    sections = []
    for g in groups:
        rows = join(_row(f) for f in fs if f["group"] == g)
        sections.append(section(g, h('<div class="tablewrap"><table class="config"><thead><tr><th>setting</th>'
                                     '<th>now</th><th>change</th><th>what it does</th></tr></thead><tbody>{r}</tbody>'
                                     '</table></div>', r=rows)))
    keys = sorted(secrets_env())
    secrets = section("Secrets (.env): names only", h(
        '<p>{k}</p><p class="dim">Values never reach this page: set them in <code class="wrap">{p}/.env</code> in your '
        'terminal.</p>',
        k=", ".join(keys) or "none", p=str(PROJECT)))
    live = h('<section class="panel"><div data-poll="/fragment/settings" data-every="5000">{s}</div></section>',
             s=status(fs, scheduled, busy))
    intro = h('<p class="dim">The supervisor\'s settings, saved in <code class="wrap">{f}</code> (a process\'s '
              'environment wins over the file, the file over the default). Ports, interpreter paths and endpoints are '
              'set from a terminal only: <code>shaft settings</code>.</p>', f=data["file"])
    return join([h("<h1>Settings</h1>"), intro, live, *sections, secrets])

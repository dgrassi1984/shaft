"""The web UI's pages (PLAN-webui.md), as plain functions from the supervisor's own data to HTML. Stdlib only.

Instance-written text is everywhere here (goals, messages, journal lines, verdicts), and this UI can change an
instance's config, so nothing an instance wrote may ever run as markup. Every value goes through `esc` unless it is
already `Html`, i.e. built by these functions; `h` formats a template with every argument escaped. Control
characters are dropped as in the TUI (dashboard.clean). No page has inline script or inline event handlers: the
Content-Security-Policy (web.py) forbids both, and web_static/web.js binds behaviour by data- attributes.
"""
from __future__ import annotations

import html
import time

from .dashboard import clean, event_tone


class Html(str):
    """Text that is already safe HTML (built here); anything else is escaped on the way in. Adding to it keeps it
    Html and escapes the plain-text side: str's own + returned a plain str, which the next h() escaped again, and
    an instance page showed its journal as raw markup (M2, 2026-10-19)."""

    def __add__(self, other):
        return Html(str.__add__(self, esc(other)))

    def __radd__(self, other):
        return Html(str.__add__(esc(other), self))


def esc(value) -> Html:
    if isinstance(value, Html):
        return value
    return Html(html.escape(clean(str(value)), quote=True))


def h(template: str, **values) -> Html:
    return Html(template.format(**{k: esc(v) for k, v in values.items()}))


def join(parts, sep: str = "") -> Html:
    return Html(sep.join(esc(p) for p in parts))


# -- the page ------------------------------------------------------------------------------------------------------
STATIC_VERSION = "10"       # bump when web_static changes, so a browser does not keep an old copy


def page(title: str, body: Html, nav: Html = Html(""), csrf: str = "") -> Html:
    meta = h('<meta name="csrf" content="{csrf}">', csrf=csrf) if csrf else Html("")
    return h('<!doctype html><html lang="en"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width, initial-scale=1">'
             '<meta name="color-scheme" content="light dark">{meta}<title>{title} · SHAFT</title>'
             '<link rel="stylesheet" href="/static/web.css?v={v}">'
             '<script src="/static/web.js?v={v}" defer></script></head>'
             '<body><div class="shell">{nav}<main>{body}</main></div></body></html>',
             meta=meta, title=title, v=STATIC_VERSION, nav=nav, body=body)


def nav(names: list[str], current: str = "overview", pages: list[tuple[str, str]] = ()) -> Html:
    """The left nav: the overview, each instance (its own pages), then the global pages."""
    items = [h('<a href="/" class="{cls}">Overview</a>', cls="on" if current == "overview" else "")]
    items += [h('<a href="/i/{n}" class="inst {cls}">{n}</a>', n=n, cls="on" if current == f"i:{n}" else "")
              for n in names]
    items += [h('<a href="{href}" class="{cls}">{label}</a>', href=href, label=label,
                cls="on" if current == href else "") for href, label in pages]
    return h('<nav><div class="brand">SHAFT</div>{items}'
             '<form class="unpair" data-post="/unpair" data-after="reload" data-confirm="Unpair this browser? You will '
             'need a new link from `shaft web pair` to use it again."><button type="submit">Unpair this browser</button>'
             '</form></nav>', items=join(items))


def message_page(title: str, text: str, detail: str = "") -> Html:
    """A page for a refusal or a notice: what happened, and what to do about it."""
    extra = h('<p class="dim">{d}</p>', d=detail) if detail else Html("")
    return page(title, h('<section class="notice"><h1>{t}</h1><p>{text}</p>{extra}</section>',
                         t=title, text=text, extra=extra))


def pair_page(token: str, device: str) -> Html:
    """Opening the link only shows this; pairing is the POST, so a link preview or prefetch cannot spend it."""
    return page("Pair this browser", h(
        '<section class="notice"><h1>Pair this browser</h1>'
        '<p>This link pairs the browser you opened it in as <b>{device}</b>. After pairing, this browser can manage '
        'every instance: their config, grants and services.</p>'
        '<form method="post" action="/pair"><input type="hidden" name="t" value="{token}">'
        '<button type="submit">Pair this browser</button></form></section>', device=device, token=token))


# -- overview: the TUI's tab 1 (top.render_main), from the same snapshot (dashboard.snapshot + panel.extras) ---------
STATE_CLASS = {"idle": "idle", "in a cycle": "cycle", "sleeping": "sleep", "talking": "talk",
               "answering (API)": "talk", "writing up a conversation": "writeup", "being updated": "update"}


def state_class(state: str) -> str:
    return "paused" if "paused" in state else STATE_CLASS.get(state, "other")


def _k(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def _ago(ts: str, now: float | None = None) -> str:
    try:
        s = (now or time.time()) - time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return "?"
    return "just now" if s < 60 else f"{s / 60:.0f} min ago" if s < 3600 else f"{s / 3600:.1f} h ago"


def _recap(r: dict | None) -> Html | None:
    r = r or {}
    if not (r.get("text") or r.get("error") or r.get("running")):
        return None
    parts = []
    if r.get("text"):
        parts.append(h('{t} <span class="dim">· {at} ({ago})</span>', t=r["text"], at=r["ts"][11:16], ago=_ago(r["ts"])))
    if r.get("running"):
        parts.append(Html('<span class="dim"><i>writing a recap…</i></span>'))
    elif r.get("error") and r.get("error_ts", "") >= r.get("ts", ""):
        parts.append(h('<span class="bad">recap failed {at}: {e}</span>', at=r["error_ts"][11:16], e=r["error"][:160]))
    return join(parts, " · ")


def _row(label: str, value, cls: str = "") -> Html:
    return h('<div class="row"><span class="label">{label}</span><span class="{cls}">{value}</span></div>',
             label=label, value=value, cls=cls)


def instance_card(i: dict) -> Html:
    state = [esc(i["state"])]
    if i.get("since"):
        state.append(h('<span class="dim"> since {s}</span>', s=i["since"]))
    if i.get("conversations") and "talk" not in i["state"] and "conversation" not in i["state"]:
        state.append(h('<span class="talk-text"> · {n} conversation(s) open</span>', n=i["conversations"]))
    rows = [_row("state", join(state)), _row("now", i.get("doing") or "—", "" if i.get("doing") else "dim")]
    if i.get("goal"):
        rows.append(_row("goal", i["goal"]))
    if (line := _recap(i.get("recap"))) is not None:
        rows.append(_row("recap", line))
    worth = f"value {i['value']:.1f}/5, " if i.get("value") is not None else ""
    rate = (f" · judged {i['judged']} ({worth}achieved {i['achieved']:.1f}/5, success {i['success']:.0%} of the last 10)"
            if i.get("achieved") is not None else
            f" · judged {i['judged']} ({i['success']:.0%} of the last 10)" if i.get("success") is not None else "")
    cost = f" · ${i['usd_today']:.2f}" if i.get("usd_today") else ""
    rows.append(_row("day", f"{i['calls_today']} model calls · {_k(i['tokens_today'])} tokens{cost}{rate}", "dim"))
    sched = (f"wakes every {i['every']} · next {i['next_wake']}" if i["every"] != "no wakes" else
             "no scheduled wakes")
    off = f" · off: {', '.join(i['tools_off'])}" if i.get("tools_off") else ""
    rows.append(_row("plan", f"{sched}{off}", "dim"))
    if i.get("trial"):
        rows.append(_row("trial", i["trial"], "trial-text"))
    from .web_actions import instance_buttons
    return h('<article class="card st-{cls}" id="inst-{name}"><header><a href="/i/{name}"><b>{name}</b></a> '
             '<span class="dim">· {model}</span></header>{rows}<footer>{buttons}'
             '<a class="msglink" href="/i/{name}#write">Message…</a></footer></article>',
             cls=state_class(i["state"]), name=i["name"], model=i["model"], rows=join(rows),
             buttons=instance_buttons(i))


def _when(m: dict) -> str:
    return m["ts"][5:16].replace("T", " ")


def _first_line(text: str) -> str:
    return next((l for l in text.splitlines() if l.strip()), "").strip()


def message_line(m: dict) -> Html:
    kind = (h('<span class="kind">{k}</span> ', k=m["kind"]) if m.get("kind") not in (None, "", "note", "message")
            else Html(""))
    pending = Html('<span class="pending" title="still in the inbox">● </span>') if not m.get("read") else Html("")
    return h('<li><span class="dim">{when}</span> <b>{who}</b> {pending}{kind}<span class="ellipsis">{first}</span></li>',
             when=_when(m), who=f"{m['from']} → {m['to']}", pending=pending, kind=kind,
             first=_first_line(m.get("text", "")))


def overview(snap: dict) -> Html:
    """The live part of the overview page (polled as /fragment/overview)."""
    services = join(h('<span class="svc {cls}">{dot} {name}{detail}</span>',
                      cls="up" if s["running"] else "idle" if s.get("idle") else "down",
                      dot="●" if s["running"] else "○", name=s["name"],
                      detail=h(' <span class="dim">{d}</span>', d=s["detail"]) if s.get("detail") else Html(""))
                    for s in snap["services"])
    spend = (h(' · <b>${s}</b> spent today', s=f"{snap['spend']:.2f}") if snap.get("spend") is not None
             else Html(""))
    alerts = snap.get("alerts") or []
    alert_html = join(h('<li>⚠ {t}</li>', t=a["text"]) for a in alerts[:5])
    if len(alerts) > 5:
        alert_html = join([alert_html, h('<li class="dim">… {n} more</li>', n=len(alerts) - 5)])
    ms = snap.get("messages") or []
    pending = sum(not m.get("read") for m in ms)
    msg_title = f"messages · {len(ms)}" + (f" · {pending} pending in an inbox" if pending else "")
    msgs = join(message_line(m) for m in ms[-5:]) if ms else Html('<li class="dim">—</li>')
    events = (join(h('<li class="{tone}">{line}</li>', tone=event_tone(e), line=e.split(" cycle_timeout_s=")[0][:200])
                   for e in snap["events"]) if snap["events"] else Html('<li class="dim">—</li>'))
    from .web_actions import alert_buttons, button
    recap = button("recap", "Recap every instance", "write every instance's one-line recap now? A small model call "
                   "each (SHAFT_RECAP_MODEL), counted under recaps in Costs.", cls="small")
    return h('<div class="topline"><span class="dim">{at}</span>{spend}<div class="services">{services}</div>{recap}'
             '</div>{alerts}<div class="cards">{cards}</div>'
             '<section class="panel"><h2>{msg_title}</h2><ul class="lines">{msgs}</ul></section>'
             '<section class="panel"><h2>events</h2><ul class="lines events">{events}</ul></section>',
             at=snap["at"], spend=spend, services=services, recap=recap,
             alerts=h('<div class="alerts"><ul>{a}</ul>{b}</div>', a=alert_html, b=alert_buttons(alerts))
             if alerts else Html(""),
             cards=join(instance_card(i) for i in snap["instances"]), msg_title=msg_title, msgs=msgs, events=events)


def overview_page(snap: dict, csrf: str) -> Html:
    body = h('<h1 class="sr">Overview</h1><div id="overview" data-poll="/fragment/overview">{live}</div>',
             live=overview(snap))
    from .web_views import GLOBAL_PAGES
    return page("Overview", body, nav([i["name"] for i in snap["instances"]], "overview", GLOBAL_PAGES), csrf)

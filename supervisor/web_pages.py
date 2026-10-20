"""The web UI's pages (docs/plans/PLAN-webui.md), as plain functions from the supervisor's own data to HTML. Stdlib only.

Instance-written text is everywhere here (goals, messages, journal lines, verdicts), and this UI can change an
instance's config, so nothing an instance wrote may ever run as markup. Every value goes through `esc` unless it is
already `Html`, i.e. built by these functions; `h` formats a template with every argument escaped. Control
characters are dropped as in the TUI (dashboard.clean). No page has inline script or inline event handlers: the
Content-Security-Policy (web.py) forbids both, and web_static/web.js binds behaviour by data- attributes.
"""
from __future__ import annotations

import hashlib
import html
import time
from pathlib import Path

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
def _static_version(folder: Path = Path(__file__).parent / "web_static") -> str:
    """The static files' own fingerprint: a changed static asset gets a new URL, so a browser never keeps running an
    old copy against new pages (it was a constant bumped by hand, and a change without the bump went unseen)."""
    d = hashlib.sha256()
    for f in sorted(folder.glob("*")):
        d.update(f.name.encode() + b"\0" + f.read_bytes())
    return d.hexdigest()[:10]


STATIC_VERSION = _static_version()      # read once at start: the service is restarted to load new code anyway


APP_NAME = "SHAFT"
THEME = {"light": "#f6f6f4", "dark": "#141413"}       # the page's --bg in each scheme (web.css)


def page(title: str, body: Html, nav: Html = Html(""), csrf: str = "") -> Html:
    meta = h('<meta name="csrf" content="{csrf}">', csrf=csrf) if csrf else Html("")
    return h('<!doctype html><html lang="en"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
             '<meta name="color-scheme" content="light dark">{meta}<title>{title} · SHAFT</title>'
             # an app on the Home Screen (docs/plans/PLAN-pwa P1): its manifest, its name, its bar and colours
             '<link rel="manifest" href="/manifest.webmanifest">'
             '<meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-capable" content="yes">'
             '<meta name="apple-mobile-web-app-title" content="{app}">'
             '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
             '<meta name="theme-color" content="{light}" media="(prefers-color-scheme: light)">'
             '<meta name="theme-color" content="{dark}" media="(prefers-color-scheme: dark)">'
             '<meta name="static-version" content="{v}">'
             '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg?v={v}">'
             '<link rel="apple-touch-icon" href="/static/icon-180.png?v={v}">'
             '<link rel="stylesheet" href="/static/web.css?v={v}">'
             '<script src="/static/web.js?v={v}" defer></script>'
             '<script src="/static/comments.js?v={v}" defer></script></head>'
             '<body><div class="{shell}">{nav}<main>{body}</main></div></body></html>',
             shell="shell" if nav else "shell single",
             meta=meta, title=title, v=STATIC_VERSION, nav=nav, body=body, app=APP_NAME, light=THEME["light"],
             dark=THEME["dark"])


# The pages used now and then, under the rail's chats: the one list of them (a second list, passed in and ignored,
# would have dropped the Settings page from the nav when the two were merged).
MANAGE = [("/overview", "Overview"), ("/messages", "Messages"), ("/trials", "Trials"), ("/costs", "Costs"),
          ("/services", "Services"), ("/settings", "Settings")]


def rail(names: list[str], current: str = "", snap: dict | None = None, chats: dict | None = None) -> Html:
    """The left rail (docs/plans/PLAN-webui-chat §1): a chat per instance first, then the pages used now and then. Without `snap`
    (a page's first render) it lists the names; web.js refreshes it from /fragment/rail at once, with each instance's
    state, its last line and when it last answered (data-last: the browser marks a reply it has not seen)."""
    live = {i["name"]: i for i in (snap or {}).get("instances", [])}
    items = []
    for n in names:
        i, c = live.get(n), (chats or {}).get(n) or {}
        dot = h('<span class="dot st-{s}" title="{t}" role="img" aria-label="{t}"></span>', s=state_class(i["state"]),
                t=i["state"]) if i else \
            Html('<span class="dot"></span>')
        line = (Html('<span class="last talk-text">replying…</span>') if c.get("replying") else
                h('<span class="last">{t}</span>', t=c["line"]) if c.get("line") else Html(""))
        items.append(h('<a href="/i/{n}/chat" class="inst {on}" data-inst="{n}" data-last="{last}">{dot}'
                       '<span class="who">{n}</span>{line}</a>', n=n, on="on" if current == f"i:{n}" else "",
                       last=c.get("last", ""), dot=dot, line=line))
    alerts = len((snap or {}).get("alerts") or [])
    manage = [h('<a href="{href}" class="{on}">{label}{badge}</a>', href=href, label=label,
                on="on" if current in (href, href.strip("/")) else "",
                badge=h(' <span class="badge bad" title="alerts">{n}</span>', n=alerts) if href == "/overview" and alerts
                else Html("")) for href, label in MANAGE]
    return h('<div class="group">Chats</div>{items}<details class="manage" data-key="manage" open><summary class="group">Manage</summary>'
             '{manage}</details>', items=join(items), manage=join(manage))


def nav(names: list[str], current: str = "overview") -> Html:
    """The left nav: the rail (refreshed live: the chats, then MANAGE), then unpairing."""
    from . import version
    from .web_actions import post_attrs
    cur = "/overview" if current == "overview" else current
    rec = version.service("web", stale=False)
    checkout = version.supervisor_commit()
    moved = h('<a href="/services">checked out: {v}</a>', v=version.commit_describe(checkout)) \
        if rec and rec["commit"].split("+")[0] != checkout.split("+")[0] else Html("")
    running = h('<div class="nav-version dim">web: {v}{m}'
                '<button type="button" data-hard-reload title="Clear the app cache and load the current version">'
                'Reload / update</button></div>', v=version.service_version(rec), m=moved)
    return h('<nav><a class="brand" href="/">SHAFT</a><button type="button" class="small closenav" data-toggle-nav '
             'aria-label="Close the chats">✕</button><div class="rail" data-poll="/fragment/rail?c={c}" data-every="3000" '
             'data-now="1" data-hidden="1">{rail}</div>'
             '<form class="unpair" data-post="/unpair" data-after="reload" data-confirm="Unpair this browser? You will '
             'need a new link from `shaft web pair` to use it again."{a}><button type="submit">Unpair this browser'
             '</button></form>{version}</nav>', c=cur, rail=rail(names, cur), a=post_attrs("this page"),
             version=running)


def message_page(title: str, text: str, detail: str = "") -> Html:
    """A page for a refusal or a notice: what happened, and what to do about it."""
    extra = h('<p class="dim">{d}</p>', d=detail) if detail else Html("")
    return page(title, h('<section class="notice"><h1>{t}</h1><p>{text}</p>{extra}'
                         '<p><a href="/">Back to app</a></p></section>',
                         t=title, text=text, extra=extra))


# -- the app's shell (docs/plans/PLAN-pwa P1): served before pairing, nothing private in it -----------------------------------
def manifest() -> str:
    """The web app manifest: what Add to Home Screen installs. start_url / reopens the last chat (its cookie)."""
    import json
    icon = lambda name, size, purpose="any": {"src": f"/static/{name}?v={STATIC_VERSION}", "sizes": f"{size}x{size}",
                                              "type": "image/png", "purpose": purpose}
    return json.dumps({
        "id": "/", "start_url": "/", "scope": "/", "display": "standalone", "name": APP_NAME,
        "short_name": APP_NAME, "description": "Chat with and run SHAFT's instances",
        "background_color": THEME["dark"], "theme_color": THEME["dark"],
        "icons": [icon("icon-192.png", 192), icon("icon-512.png", 512), icon("icon-maskable-512.png", 512, "maskable"),
                  icon("icon-180.png", 180)]}, indent=1)


SW = """// SHAFT's service worker (docs/plans/PLAN-pwa P1), written by web_pages.service_worker() for static version @V@.
// It keeps only what never changes under a URL — the versioned static files — and an offline page. Every page,
// fragment, file and POST goes to the server as it always did: nothing private is ever cached.
const VERSION = "@V@";
const CACHE = "shaft-" + VERSION;
const SHELL = ["/offline", "/static/web.css?v=@V@", "/static/web.js?v=@V@", "/static/comments.js?v=@V@",
               "/static/favicon.svg?v=@V@",
               "/static/icon-192.png?v=@V@"];

self.addEventListener("install", (ev) => {
  ev.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (ev) => {           // an older version's cache goes; this one serves at once
  ev.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith("shaft-") && k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (ev) => {
  const req = ev.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.mode === "navigate") {                     // the server, always; when it cannot be reached, say so
    ev.respondWith(fetch(req).then((r) => {
      if (r.status >= 502 && r.status <= 504) return caches.match("/offline");
      return r;
    }).catch(() => caches.match("/offline")));
    return;
  }
  if (url.pathname.startsWith("/static/") && url.searchParams.get("v") === VERSION) {
    ev.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((r) => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
      return r;
    })));
  }
});

// A push (docs/plans/PLAN-pwa P4) carries nothing: what to say is fetched from SHAFT over the tailnet, with this device's own
// cookie, so the push service never sees it — every event waiting for this device (a reply, a message an instance sent
// you: docs/plans/PLAN-operator-channel.md), each its own notification. iOS requires every push to show one: one always shows.
self.addEventListener("push", (ev) => {
  ev.waitUntil((async () => {
    let shown = [{ title: "SHAFT", body: "Something new", url: "/", tag: "shaft" }];
    try {
      const r = await fetch("/push/latest", { credentials: "same-origin", cache: "no-store" });
      const got = r.ok ? ((await r.json()).events || []) : [];
      if (got.length) shown = got;
    } catch (e) { /* off the tailnet: the plain notification */ }
    if (self.navigator.setAppBadge) self.navigator.setAppBadge().catch(() => {});
    for (const n of shown) {
      await self.registration.showNotification(n.title, {
        body: n.body, tag: n.tag, data: { url: n.url }, icon: "/static/icon-192.png?v=@V@" });
    }
  })());
});

self.addEventListener("notificationclick", (ev) => {     // open the chat that replied, in the app if it is open
  ev.notification.close();
  const url = (ev.notification.data || {}).url || "/";
  ev.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    for (const c of list) {
      if ("focus" in c) return (c.navigate ? c.navigate(url) : Promise.resolve()).then(() => c.focus());
    }
    return self.clients.openWindow(url);
  }));
});
"""


def service_worker() -> str:
    return SW.replace("@V@", STATIC_VERSION)


def offline_page() -> Html:
    """Shown by the service worker when the server cannot be reached (the app opened with the Mac asleep, or the
    phone off the tailnet): a standalone app has no address bar or reload of its own."""
    return page("Not reachable", h(
        '<section class="notice"><h1>SHAFT is not reachable</h1>'
        '<p>The phone cannot reach the Mac running SHAFT. Check that Tailscale is connected on this phone and that the '
        'Mac is awake.</p><p><a class="button" href="/">Try again</a></p></section>'))


def unpaired_page(expired: bool = False) -> Html:
    """Not paired: how to pair this browser — or this Home Screen app, which has cookies of its own and no address
    bar to open a pairing link in: the link is pasted here instead."""
    return page("Link expired" if expired else "Pair this app", h(
        '<section class="notice"><h1>{title}</h1>{reason}'
        '<p>In your terminal run <code>shaft web pair --name phone-app</code>, copy the link it prints, and paste it '
        'here. It works once, for 10 minutes.</p>'
        '<form method="post" action="/pair" class="pairform"><input name="link" type="url" inputmode="url" required '
        'autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="https://…/pair?t=…" '
        'aria-label="Pairing link"><button type="submit">Pair</button></form>'
        '<p class="dim">In a browser you can also just open the link.</p>'
        '<p><a href="/">Back to app</a></p></section>',
        title="Link expired" if expired else "This app or browser is not paired",
        reason=Html('<p>This pairing link is unknown, used or expired. Paste a new link below.</p>') if expired else Html("")))


def pair_page(token: str, device: str) -> Html:
    """Opening the link only shows this; pairing is the POST, so a link preview or prefetch cannot spend it."""
    return page("Pair this browser", h(
        '<section class="notice"><h1>Pair this browser</h1>'
        '<p>This link pairs the browser you opened it in as <b>{device}</b>. After pairing, this browser can manage '
        'every instance: their config, grants and services.</p>'
        '<form method="post" action="/pair"><input type="hidden" name="t" value="{token}">'
        '<button type="submit">Pair this browser</button></form>'
        '<p><a href="/">Back to app</a></p></section>', device=device, token=token))


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


# `shaft new <name>` is the verb (cmd_new, docs/cli.md). The README section that walks through it, including
# `bin/shaft new` from a fresh clone, is the quickstart (docs/local-model.md links this same anchor).
_README_QUICKSTART = "https://github.com/dgrassi1984/shaft#quickstart-cloud-only"


def _alerts(alerts: list[dict]) -> Html:
    """Five alerts on view. Past that, "Show all N alerts" lists every one; data-key keeps it open across the
    overview's refresh (web.js poll). Each alert dismisses on its own, beside the batch buttons."""
    if not alerts:
        return Html("")
    from .web_actions import alert_buttons, alert_line
    shown = join(alert_line(a) for a in alerts[:5])
    more = Html("")
    if len(alerts) > 5:
        more = h('<details data-key="all-alerts"><summary>Show all {n} alerts</summary><ul>{a}</ul></details>',
                 n=len(alerts), a=join(alert_line(a) for a in alerts))
    return h('<div class="alerts"><ul>{a}</ul>{more}{b}</div>', a=shown, more=more, b=alert_buttons(alerts))


def _no_instances() -> Html:
    """A fresh install opens here (/ redirects to /overview) with an empty Chats group and no cards."""
    return h('<section class="panel"><h2>No instances yet</h2>'
             '<p>Instances are created from the terminal:</p><p><code>{cmd}</code></p>'
             '<p>The <a href="{url}" rel="noopener noreferrer" target="_blank">README quickstart</a> explains it.'
             '</p></section>', cmd="shaft new <name>", url=_README_QUICKSTART)


def overview(snap: dict) -> Html:
    """The live part of the overview page (polled as /fragment/overview)."""
    services = join(h('<span class="svc {cls}">{dot} {name}{detail}</span>',
                      cls="up" if s["running"] else "idle" if s.get("idle") else "down",
                      dot="●" if s["running"] else "○", name=s["name"],
                      detail=h(' <span class="dim">{d}</span>', d=s["detail"]) if s.get("detail") else Html(""))
                    for s in snap["services"])
    spend = (h(' · <b>${s}</b> spent today', s=f"{snap['spend']:.2f}") if snap.get("spend") is not None
             else Html(""))
    ms = snap.get("messages") or []
    pending = sum(not m.get("read") for m in ms)
    msg_title = f"messages · {len(ms)}" + (f" · {pending} pending in an inbox" if pending else "")
    msgs = join(message_line(m) for m in ms[-5:]) if ms else Html('<li class="dim">—</li>')
    events = (join(h('<li class="{tone}">{line}</li>', tone=event_tone(e), line=e.split(" cycle_timeout_s=")[0][:200])
                   for e in snap["events"]) if snap["events"] else Html('<li class="dim">—</li>'))
    from .web_actions import button
    recap = button("recap", "Recap every instance", "write every instance's one-line recap now? A small model call "
                   "each (SHAFT_RECAP_MODEL), counted under recaps in Costs.", cls="small")
    cards = (h('<div class="cards">{c}</div>', c=join(instance_card(i) for i in snap["instances"]))
             if snap["instances"] else _no_instances())
    return h('<div class="topline"><span class="dim">{at}</span>{spend}<div class="services">{services}</div>{recap}'
             '</div>{alerts}{cards}'
             '<section class="panel"><h2>{msg_title}</h2><ul class="lines">{msgs}</ul></section>'
             '<section class="panel"><h2>events</h2><ul class="lines events">{events}</ul></section>',
             at=snap["at"], spend=spend, services=services, recap=recap,
             alerts=_alerts(snap.get("alerts") or []), cards=cards, msg_title=msg_title, msgs=msgs, events=events)


def overview_page(snap: dict, csrf: str) -> Html:
    body = h('<h1 class="sr">Overview</h1><div id="overview" data-poll="/fragment/overview">{live}</div>',
             live=overview(snap))
    return page("Overview", body, nav([i["name"] for i in snap["instances"]], "overview"), csrf)

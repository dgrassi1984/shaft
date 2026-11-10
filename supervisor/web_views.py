"""The web UI's read-only pages: the TUI's tabs 2–6, 8, 9, 0 and the messages browser, plus an
instance's journal and activity. Each takes the data the TUI's tab takes — the same `panel` readers, called by web.py
— and shows the same fields, so the two UIs cannot disagree. Escaping, control characters and the page frame are
web_pages'; nothing here writes anything.
"""
from __future__ import annotations

import functools
import json
import re
import time
import urllib.parse
from datetime import datetime

from .web_pages import Html, esc, etag, h, icon, join, nav, page, pagebar

MARK = {"yes": ("✓", "good"), "partial": ("½", "warn"), "no": ("✗", "bad")}
ICONS = {name: icon(name, 16) for name in ("copy", "reply", "delete", "edit", "download", "expand", "collapse", "select")}
_CLOSE = icon("close")                         # the sheets' ✕, drawn
_DOT = Html('<span class="mark" title="changed in this chat">●</span> ')
# The documents drawer's full-screen toggle: both glyphs, CSS shows the one aria-pressed calls for (web.js flips it).
FULLSCREEN = h('<button type="button" class="small icon" data-fullscreen-doc aria-pressed="false" title="Full screen" '
               'aria-label="Full screen">{on}{off}</button>', on=ICONS["expand"], off=ICONS["collapse"])
INSTANCE_TABS = [("chat", "Chat"), ("", "Summary"), ("config", "Config"), ("drive", "Drive"), ("files", "Files"), ("access", "Access"),
                 ("judge", "Judge"), ("changes", "Changes"), ("benchmarks", "Benchmarks"),
                 ("transcripts", "Cycles"), ("journal", "Journal"), ("activity", "Activity")]
BUSY_ICONS = {"in a cycle": "busy", "working": "busy", "sleeping": "sleep", "paused by you": "pause",
              "in a conversation (waiting for you)": "waiting"}


def q(value: str) -> str:
    """A value for a URL path segment or query parameter."""
    return urllib.parse.quote(str(value), safe="")


def size_label(n: int) -> str:
    """Bytes as a file manager shows them: 512 B, 1 KB, 1.5 KB, 12 KB, 3.2 MB (1 KB = 1024 bytes, as FILE_MAX's MB)."""
    if n < 1024:
        return f"{n} B"
    v = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        v /= 1024
        shown = f"{v:.1f}" if round(v, 1) < 10 else f"{v:.0f}"
        if float(shown) < 1024 or unit == "TB":      # 1023.97 KB is 1 MB, never "1024 KB"
            return f"{shown.removesuffix('.0')} {unit}"
    raise AssertionError("unreachable")


def when_label(ts: float, now: float | None = None) -> Html:
    """A modification time, as short as it can be in a narrow list: 14:32 today, 24 Oct 14:32 this year, else 2025-03-01;
    the full time on hover. Absolute, so a list polled every 1.5 s changes only when a file does (or the day turns)."""
    t, today = time.localtime(ts), time.localtime(now if now is not None else time.time())
    short = (time.strftime("%H:%M", t) if t[:3] == today[:3] else
             f"{t.tm_mday} {time.strftime('%b %H:%M', t)}" if t.tm_year == today.tm_year else
             time.strftime("%Y-%m-%d", t))
    return h('<time class="dim docdate" datetime="{iso}" title="{full}">{s}</time>',
             iso=time.strftime("%Y-%m-%dT%H:%M:%S%z", t), full=time.strftime("%Y-%m-%d %H:%M:%S", t), s=short)


# The documents list's orders: ?sort= or the shaft_docsort cookie names one; each column's first click is its natural
# direction (newest, A→Z, largest), the next click on it reverses it.
DOC_SORTS = {"modified": ("mtime", True), "modified-asc": ("mtime", False), "name": ("name", False),
             "name-desc": ("name", True), "size": ("bytes", True), "size-asc": ("bytes", False)}
DOC_SORT_DEFAULT = "modified"
_DOC_COLUMNS = [("name", "Name", "name", "name-desc", Html("")), ("bytes", "Size", "size", "size-asc", Html(" docsize")),
                ("mtime", "Modified", "modified", "modified-asc", Html(" docdate"))]


def doc_sort(value: str | None) -> str:
    return value if value in DOC_SORTS else DOC_SORT_DEFAULT


def _sort_bar(sort: str, folder: str, chat_id: str = "") -> Html:
    """The list's column heads: Name, Size, Modified; the one it is sorted by is marked, with its direction. Each is a
    link that works without the script too: it asks for the list (list=1: without it the page opens what this chat
    last changed), in this folder, of this archived chat (id)."""
    keep = (f"&dir={q(folder)}" if folder else "") + (f"&id={q(chat_id)}" if chat_id else "")
    key, reverse = DOC_SORTS[sort]
    heads = []
    for k, label, first, second, cls in _DOC_COLUMNS:
        on = k == key
        to = (second if sort == first else first) if on else first
        arrow = ("↓" if reverse else "↑") if on else ""
        heads.append(h('<a href="?list=1&amp;sort={t}{d}" data-sort-docs="{t}" data-focus="sort-{k}" class="{c}"{cur} '
                       'title="Sort by {l}{again}">{l}{a}</a>', t=to, k=k, d=keep, c=Html(("on" if on else "") + str(cls)),
                       cur=Html(' aria-current="true"') if on else "", l=label, a=f" {arrow}" if arrow else "",
                       again=", reversed" if on else ""))
    return h('<div class="docsort" role="group" aria-label="Sort the documents">{h}</div>', h=join(heads))


def frame(title: str, body: Html, names: list[str], csrf: str, current: str = "", bar: bool = True) -> Html:
    """A page with the rail. On a phone the rail is a sheet behind ☰ and the page has a bar: ‹ Chat, its title, ☰ —
    the installed app has no browser Back, and the rail filled half the screen above every page (iPhone review I5)."""
    if bar:
        body = join([pagebar(title, current), body])
    return page(title, body, nav(names, current), csrf)


def instance_frame(name: str, tab: str, title: str, body: Html, names: list[str], csrf: str) -> Html:
    tabs = join(h('<a href="{href}" class="{cls}">{label}</a>', href=f"/i/{q(name)}" + (f"/{t}" if t else ""),
                  cls="on" if t == tab else "", label=label) for t, label in INSTANCE_TABS)
    head = h('<header class="pagehead"><h1>{name}</h1><div class="tabs">{tabs}</div></header>', name=name, tabs=tabs)
    return frame(f"{name} · {title}", join([head, body]), names, csrf, current=f"i:{name}")


def files_page(name: str, parts: tuple, entries: list[dict], total: int, page_number: int, page_size: int,
               names: list[str], csrf: str) -> Html:
    base = f"/i/{q(name)}/files?path="
    folder = "/".join(parts) + ("/" if parts else "")
    crumbs = [link(base, name)]
    for k, part in enumerate(parts):
        crumbs.extend([" / ", link(base + q("/".join(parts[:k + 1]) + "/"), part)])
    if parts:
        parent = "/".join(parts[:-1]) + ("/" if len(parts) > 1 else "")
        crumbs.extend([" · ", link(base + q(parent), "Up")])
    rows = []
    for entry in entries:
        path = folder + entry["name"]
        if entry["directory"]:
            label = link(base + q(path + "/"), entry["name"] + "/")
            controls = Html("")
        else:
            label = entry["name"]
            url = f"/i/{q(name)}/file?path={q(path)}"
            controls = join([link(url, "Download"), " · ", link(url + "&open=1", "Open")]) if entry["text"] else link(url, "Download")
        rows.append([label, "—" if entry["directory"] else size_label(entry["bytes"]),
                     time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["mtime"])), controls])
    paging = []
    if page_number > 1:
        paging.append(link(base + q(folder) + f"&page={page_number - 1}", "Previous"))
    if page_number * page_size < total:
        paging.extend([" ", link(base + q(folder) + f"&page={page_number + 1}", "Next")])
    body = join([h('<div class="crumbs" role="navigation" aria-label="File path">{crumbs}</div><p>{total} {noun} · Page {page}</p>',
                   crumbs=join(crumbs), noun="entry" if total == 1 else "entries", total=total, page=page_number),
                 table(["Name", "Size", "Modified", "Actions"], rows), h('<p>{links}</p>', links=join(paging))])
    return instance_frame(name, "files", "Files", section("Files", body), names, csrf)


def chat_frame(name: str, head: Html, body: Html, names: list[str], csrf: str, header_url: str) -> Html:
    head = h('<div data-poll="{u}" data-every="1500">{head}</div>', u=header_url, head=head)
    return frame(f"{name} · chat", join([head, body]), names, csrf, current=f"i:{name}", bar=False)


def earlier_chats(name: str, past: list[dict]) -> Html:
    """Shared labels for the quick list and the complete archive."""
    from .web_chat import excerpt
    items = []
    for c in past:
        line, n = excerpt(c)
        label = f"{c['created'][:16].replace('T', ' ')} · {line or 'No message text'} · {n} message(s)"
        items.append(link(f"/i/{q(name)}/chat?id={q(c['id'])}", label))
    return lines(items, "No earlier chats yet.")


def busy_note(name: str, state: str) -> str | None:
    """What a message does now, while the instance is busy (api.py work(): a cycle or sleep pauses for the reply;
    another conversation or run is waited for; paused by you holds no run, so it answers at once): plain words for the
    old one-line rule. None when idle (or its state is unknown). The chat's header and /app/v1/instances say it."""
    if state in ("idle", "?"):
        return None
    return {"paused by you": f"No cycles run until you resume {name}. It still answers your messages.",
            "sleeping": "Your message pauses its sleep; the sleep resumes after the reply.",
            "in a cycle": "Your message pauses its cycle; the cycle resumes after the reply.",
            # dashboard.instance_state: an open conversation and no run — nothing holds it, so it answers now
            "in a conversation (waiting for you)": "A conversation with you is open, waiting for your next "
                                                   "message. A message is answered now."
            }.get(state, "Your message is answered once it pauses or finishes what it is doing; the reply's "
                         "activity says which.")


def chat_header(name: str, live: dict, chat: dict | None, past: list[dict], read_only: bool = False) -> Html:
    """The current facts and actions, refreshed independently of the composer and documents. A title with the state
    under it and three controls (Documents, New chat, ⋯); pages, earlier chats and the instance's actions are in ⋯,
    Kill last and apart. Seven bordered boxes read as a toolbar, and Clear sat one tap from the title (review I11)."""
    from .web_actions import instance_buttons, post_attrs
    pages = join(h('<a href="{href}">{label}</a>', href=f"/i/{q(name)}" + (f"/{t}" if t else ""), label=label)
                 for t, label in INSTANCE_TABS if t != "chat")
    state = live.get("state", "?")
    replying = bool(chat and chat.get("state") == "replying")
    facts = [live.get("model") or "", state + (f" since {live['since']}" if live.get("since") else "")]
    if live.get("usd_today"):
        facts.append(f"${live['usd_today']:.2f} today")
    buttons, danger = instance_buttons(live, split=True) if live.get("name") else (Html(""), Html(""))
    earlier = join([h('<p class="dim">latest {limit} of {total}</p>', limit=min(20, len(past)), total=len(past)),
                    earlier_chats(name, past[:20]), link(f"/i/{q(name)}/chats", "All earlier chats")])
    if (what := busy_note(name, state)) and not replying:
        sub = h('<details class="menu busystate" data-key="busy-state"><summary data-focus="busy" '
                'aria-label="{n} is {s}">{icon}<span>{s}</span></summary><div class="menubody">{w}</div></details>',
                n=name, s=state, w=what, icon=icon(BUSY_ICONS.get(state, "busy"), 14))
    else:
        sub = h('<span class="state">{s}</span>', s="replying…" if replying else state if state != "?" else "")
    # New chat: what Clear did, under the name every chat app gives it. Its confirm says what is kept.
    new = (h('<form class="act clearbtn" data-post="/chat/{n}/clear" data-confirm="Start a new chat? This one is kept in '
             'Earlier chats and Transcripts. An open conversation is ended first, and a journal write-up is requested."{a}>'
             '<input type="hidden" name="cid" value="{cid}"><button type="submit" class="iconbtn" title="New chat" '
             'aria-label="New chat" data-focus="clear">{i}</button></form>', n=q(name), cid=chat["id"],
             a=post_attrs("this chat"), i=icon("compose"))
           if chat and not read_only else Html(""))
    return h('<header class="chathead"><button type="button" class="navbtn iconbtn" data-toggle-nav '
             'aria-label="Chats and pages" aria-expanded="false" data-focus="nav">{side}</button>'
             '<div class="titleblock"><h1>{name}</h1><div class="sub">{sub}<span class="dim facts">{brief}</span>'
             '</div></div><div class="headtools">'
             '<button type="button" class="iconbtn docsbtn" data-show-docs title="Documents (notes/docs/)" '
             'aria-label="Documents" data-focus="docs">{docs}</button>{new}'
             '<details class="menu chatmenu" data-key="chat-menu"><summary class="iconbtn" data-focus="menu" '
             'title="{name}: pages and actions" aria-label="{name}: pages and actions">{more}</summary>'
             '<div class="menubody"><p class="dim menufacts">{facts}</p>'
             '<div class="menupages">{pages}</div>'
             '<details class="earliermenu" data-key="earlier-chats"><summary data-focus="earlier">{hist} Earlier chats'
             '</summary>{earlier}</details>'
             '{buttons}<div class="actions menuapp">'
             '<button type="button" class="small" data-notify data-focus="notify">Notify me on this device</button>'
             '<button type="button" class="small" data-hard-reload data-focus="hard-reload" '
             'title="Clear the app cache and load the current version">Reload / update</button></div>{danger}'
             '</div></details></div></header>',
             name=name, sub=sub, facts=" · ".join(f for f in facts if f), pages=pages, buttons=buttons, new=new, danger=danger,
             brief=" · ".join(f for f in [facts[0], *facts[2:]] if f),   # the state is already beside it
             earlier=h('<div class="earlier">{e}</div>', e=earlier), side=icon("sidebar"), docs=icon("docs"),
             more=icon("more"), hist=icon("history", 16))


def section(title: str, body, cls: str = "") -> Html:
    return h('<section class="panel {cls}"><h2>{title}</h2>{body}</section>', title=title, body=body, cls=cls)


def lines(items, empty: str = "none", cls: str = "lines") -> Html:
    items = list(items)
    if not items:
        return h('<p class="dim">{e}</p>', e=empty)
    return h('<ul class="{cls}">{li}</ul>', cls=cls, li=join(h("<li>{x}</li>", x=i) for i in items))


def rows(pairs, cls: str = "kv") -> Html:
    """Label/value pairs; a value may be (text, css class)."""
    out = []
    for label, value in pairs:
        text, c = value if isinstance(value, tuple) else (value, "")
        out.append(h('<div class="row"><span class="label">{l}</span><span class="{c}">{v}</span></div>',
                     l=label, v=text, c=c))
    return h('<div class="{cls}">{r}</div>', cls=cls, r=join(out))


def table(headers: list[str], body: list[list], cls: str = "") -> Html:
    """A table that stacks into labelled rows at phone width (each cell carries its column's name)."""
    head = join(h("<th>{x}</th>", x=x) for x in headers)
    trs = []
    for r in body:
        cells, row_cls = (r[0], r[1]) if isinstance(r, tuple) else (r, "")
        trs.append(h('<tr class="{c}">{tds}</tr>', c=row_cls,
                     tds=join(h('<td data-label="{l}">{v}</td>', l=headers[k] if k < len(headers) else "", v=v)
                              for k, v in enumerate(cells))))
    return h('<div class="tablewrap"><table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{trs}</tbody>'
             '</table></div>', cls=cls, head=head, trs=join(trs))


def pre(text: str, cls: str = "") -> Html:
    return h('<pre class="{cls}">{t}</pre>', cls=cls, t=text)


def link(href: str, text) -> Html:
    return h('<a href="{href}">{t}</a>', href=href, t=text)


def download_icon(href: str) -> Html:
    """The documents drawer's Download: a link drawn as an icon button beside Edit, copy and delete."""
    return h('<a class="small icon" href="{href}" title="Download" aria-label="Download">{i}</a>',
             href=href, i=ICONS["download"])


def when(ts: str) -> str:
    return ts[5:16].replace("T", " ")


CLIP = 240                                          # a list cell's text, at most: the full text is one click away


def clip(text: str, n: int = CLIP) -> Html:
    text = " ".join(str(text).split())
    return h('<span class="clip">{t}</span>', t=text if len(text) <= n else text[:n - 1] + "…")


# -- Markdown: web_md.py (escape first; links to http(s) and mailto only) -----------------------------------------------
_QUOTE = re.compile(r"^ {0,3}> ?")                    # a quoted line in the operator's own turns (_my_text)


def inline(text: str) -> Html:
    from .web_md import inline as md_inline
    return Html(md_inline(str(text)))


CACHED_MD = 64 * 1024                                 # texts up to this are rendered once (_rendered)


def markdown(text: str) -> Html:
    from .web_md import render
    text = str(text)
    return _rendered(text) if len(text) <= CACHED_MD else render(text)


@functools.lru_cache(maxsize=256)
def _rendered(text: str) -> Html:
    """A text rendered once: the chat feed renders every turn on each refresh, the same Markdown each time (W12,
    review 2026-10-24). At most 256 of them, each at most CACHED_MD: 16 MB."""
    from .web_md import render
    return render(text)


def spark(values: list, cls: str = "") -> Html:
    """An inline SVG sparkline of 0–5 values (None leaves a gap), oldest to newest."""
    if not any(v is not None for v in values):
        return Html('<span class="dim">—</span>')
    w, step = max(1, len(values)) * 6, 6
    bars = []
    for k, v in enumerate(values):
        if v is None:
            continue
        hgt = max(1.0, min(5.0, float(v)) / 5 * 20)
        bars.append(f'<rect x="{k * step}" y="{20 - hgt:.1f}" width="4" height="{hgt:.1f}"><title>{v}</title></rect>')
    return Html(f'<svg class="spark {cls}" viewBox="0 0 {w} 20" width="{w}" height="20" role="img" '
                f'aria-label="sparkline">{"".join(bars)}</svg>')


# -- instance › summary (TUI tab 2) ---------------------------------------------------------------------------------
def instance_live(live: dict, d: dict) -> Html:
    state = f"{live.get('state', '?')}{' since ' + live['since'] if live.get('since') else ''}"
    pairs = [("state", (state + (" · PAUSED by you" if d.get("paused") else ""), "warn" if d.get("paused") else ""))]
    if live.get("doing"):
        pairs.append(("now", (live["doing"], "strong")))
    if live.get("goal"):
        pairs.append(("goal", live["goal"]))
    pairs.append(("wakes", f"every {d['wake']} min · next {live.get('next_wake', '?')}"))
    from .web_actions import instance_buttons
    buttons = instance_buttons(live, delete=True) if live.get("name") else Html("")
    return join([rows(pairs), buttons])


def instance_summary(name: str, d: dict, live: dict, names: list[str], csrf: str) -> Html:
    cfg = rows([("model", f"{d['model']} via {d['via']} · effort {d['effort']}"),
                ("tools off", ", ".join(d["tools_off"]) or "none"),
                ("shared folder", d["shared"] or "none"), ("mail", d["mail"] or "none"),
                ("calendar", d["calendar"] or "none"), ("gdrive", d.get("gdrive") or "none"), ("gchat", d.get("gchat") or "none"), ("jobs", d.get("jobs") or "none"),
                ("python", (d.get("python", ""), "warn" if "BROKEN" in d.get("python", "") else "")),
                ("browser", (d.get("browser", ""), "warn" if "not installed" in d.get("browser", "") else "")),
                ("internet", d.get("internet", ""))])
    parts = [h('<section class="panel"><div data-poll="/fragment/i/{n}/live">{live}</div>{cfg}</section>',
               n=q(name), live=instance_live(live, d), cfg=cfg)]
    if d["questions"]:
        more = len(d["questions"]) - 3              # the oldest three, as in the TUI; the rest are in Messages
        parts.append(section(f"Open questions to you ({len(d['questions'])})", join([
            lines(h("<b>{date} {kind}:</b> {body}", date=x["date"][:16], kind=x["kind"],
                    body=" ".join(x["body"].split())) for x in d["questions"][:3]),
            link(f"/messages?who={q(name)}", f"… {more} more in Messages →") if more > 0 else ""])))
    if d["probation"]:
        parts.append(section("On probation now", lines(
            f"{e['commit'][:8]} since {e['since'][:16]} · {', '.join(e['files'])} · baseline "
            f"{e.get('baseline') if e.get('baseline') is None else round(e['baseline'], 2)}" for e in d["probation"])))
    parts.append(section("Recent journal", join([lines(clip(j) for j in d["journal"]),
                                                 link(f"/i/{q(name)}/journal", "the whole journal →")])))
    if d["changes"]:
        parts.append(section("Kept changes to its own code", lines(
            f"{c['ts'][:16]} {', '.join(c['files'])}" + (f" ({len(c['delegated'])} by its coder)" if c["delegated"]
                                                         else "") for c in d["changes"][:5])))
    if d["merges"]:
        parts.append(section("Template updates (shaft update)", lines(d["merges"])))
    from .web_actions import message_forms
    question = next((x for x in d["questions"] if x.get("kind") == "question"), None)
    parts.insert(1, section("Write to it", message_forms(name, question)))
    parts.append(section("self/stats.md (written by the supervisor)", markdown(d["stats"])))
    return instance_frame(name, "", "summary", join(parts), names, csrf)


# -- instance › judge (TUI tab 3) ------------------------------------------------------------------------------------
JUDGE_KEYS = [("judged", "judged_at"), ("model", "model"), ("judge", "judge"), ("criterion met", "criterion_met"),
              ("claimed", "claimed"), ("confidence", "confidence"), ("claim supported", "claim_supported"),
              ("criterion", "criterion_quality"), ("failure mode", "failure_mode"),
              ("origin declared", "origin_declared"), ("origin (judged)", "origin"),
              ("origin supported", "origin_supported"), ("difficulty", "difficulty"), ("novelty", "novelty"),
              ("value", "value"), ("value for", "value_for"), ("value graded by", "value_judge"),
              ("delivery supported", "delivery_supported"), ("deliverable met", "deliverable_met"),
              ("self-imposed clauses", "self_imposed_clauses"), ("self-audit", "self_audit")]
JUDGE_TEXTS = [("Reasoning", "reasoning"), ("Why this difficulty", "difficulty_reason"),
               ("Why this value", "value_reason"), ("Origin evidence", "origin_evidence"),
               ("Origin reference", "origin_ref")]


def judge_page(name: str, js: list[dict], tr: dict | None, names: list[str], csrf: str) -> Html:
    from . import metrics
    achieved = [j.get("achieved") for j in reversed(js)][-40:]
    worth = [j.get("value") for j in reversed(js)][-40:]
    models = sorted({j.get("model") or "?" for j in js})
    info = [h('<span>{n} judged · achieved (0–5, oldest→newest) </span>', n=len(js)), spark(achieved, "ach")]
    if any(v is not None for v in worth):
        info += [Html('<span> · value </span>'), spark(worth, "val")]
    if tr:
        ok = sum(1 for t in tr["traits"] if t["supported"] == "yes")
        info.append(h('<span> · personality traits supported {ok}/{n}</span>', ok=ok, n=len(tr["traits"])))
    head = h('<section class="panel"><div class="sparkline">{i}</div><p class="dim">models: {m}</p></section>',
             i=join(info), m=", ".join(m.split("/")[-1] for m in models))
    recent = list(reversed(js))
    head = join([head, lines([metrics.delivery_line(metrics.valued(recent)[-20:]),
                              *metrics.self_audit_lines(recent)])])
    body = []
    for j in js:
        sym, tone = MARK.get(j.get("criterion_met"), ("?", ""))
        claimed = j.get("claimed")
        ach, v = j.get("achieved"), j.get("value")
        goal = " ".join((j.get("goal") or "").split())
        origin = j.get("origin") or "?"
        body.append([h('<a href="/i/{n}/judge?ts={ts}">{w}</a>', n=q(name), ts=q(j.get("episode_ts", "")),
                       w=when(j.get("episode_ts", ""))),
                     h('<span class="{t}">{s}</span>', t=tone, s=sym),
                     f"claimed {claimed}" if claimed and claimed != j.get("criterion_met") else "",
                     f"d{j.get('difficulty', '·')} n{j.get('novelty', '·')}",
                     f"{ach:.1f}" if ach is not None else "·",
                     h('<span class="{t}">v{v}</span>', v=v if v is not None else "·",
                       t="good" if (v or 0) >= 3 else "bad" if v == 0 else ""),
                     join([origin, h(' <span class="bad">unsupported</span>') if j.get("claim_supported") is False
                           else ""]),
                     clip(goal)])
    listing = (table(["cycle", "met", "claimed", "diff/nov", "achieved", "value", "origin", "goal"], body, "judge")
               if body else Html('<p class="dim">no judged cycles yet</p>'))
    return instance_frame(name, "judge", "judge", join([head, section("Verdicts, newest first", listing)]), names, csrf)


def judge_detail_page(name: str, j: dict, names: list[str], csrf: str) -> Html:
    from . import metrics
    shown = {**j, "confidence": metrics.confidence_shown(j)}      # "90 → 60" where the base rate moved it
    pairs = [(label, str(shown.get(k))) for label, k in JUDGE_KEYS if shown.get(k) not in (None, "")]
    if j.get("achieved") is not None:
        pairs.append(("achieved", f"{j['achieved']:.1f} / 5"))
    texts = join(h('<h3>{l}</h3><p class="wrap">{t}</p>', l=label, t=str(j[k])) for label, k in JUDGE_TEXTS if j.get(k))
    body = h('<p><a href="/i/{n}/judge">← every verdict</a></p><section class="panel"><h2>cycle {ts}</h2>'
             '<p class="wrap"><b>Goal:</b> {goal}</p>{kv}{texts}</section>', n=q(name), ts=j.get("episode_ts", ""),
             goal=j.get("goal", ""), kv=rows(pairs), texts=texts)
    return instance_frame(name, "judge", "verdict", body, names, csrf)


# -- trials (TUI tab 4) ----------------------------------------------------------------------------------------------
def trials_live(ts: list[dict], c: dict, only: str, progress, names: list[str]) -> Html:
    """The list, the caps and the queue buttons. Polled (/fragment/trials): panel.trials is cached on the trial
    files, and an unchanged fragment is a 304, so a trial queued from this page can show up within a poll."""
    caps = [f"{cap['name']} {cap['trials_started'][0]}/{cap['trials_started'][1]} trials "
            f"${cap['trials'][0]:.2f}/${cap['trials'][1]:.2f}" for cap in c["caps"]]
    filt = join([link("/trials", "all instances") if only else Html("<b>all instances</b>")] +
                [h(" · {x}", x=link(f"/trials?i={q(n)}", n) if n != only else h("<b>{n}</b>", n=n)) for n in names])
    from . import actions
    from .web_actions import button
    aa = join(button("aa-trial", n, actions.aa_trial_question(n), {"name": n}, cls="small") for n in names)
    head = h('<section class="panel"><p>{filt}</p><p class="dim">today: {caps} · all trials ${all} (operator trials '
             'are not capped)</p><div class="actions"><span class="dim">Queue an A/A calibration trial:</span> {aa}'
             '</div></section>', filt=filt, caps=" · ".join(caps) or "no caps", all=f"{c['today']['trials']:.2f}",
             aa=aa)
    body = []
    for t in ts:
        v = t.get("verdict") or {}
        status = t.get("status", "")
        if status == "done":
            result = join([h('<span class="{c}">{l}</span> ', c={"better": "good", "worse": "bad"}.get(v.get("label"), ""),
                             l=v.get("label", "")),
                           f"W{v.get('W')} L{v.get('L')} T{v.get('T')}" + (f" S{v['S']}" if v.get("S") else "")
                           + f" p={v.get('p')} ${v.get('usd', 0):.2f}"])
        else:
            result = f"{progress(t)} replayed"
        body.append([link(f"/trials/{q(t.get('instance', ''))}/{q(t.get('id', ''))}", when(t.get("created", ""))),
                     t.get("instance", ""), t.get("by", ""),
                     h('<span class="{c}">{s}</span>', s=status,
                       c="good" if status == "done" else "warn" if status in ("queued", "testing") else ""),
                     result, clip(t.get("hypothesis") or "")])
    listing = (table(["created", "instance", "by", "status", "result", "hypothesis"], body)
               if body else Html('<p class="dim">no trials yet</p>'))
    return join([head, section("Newest last", listing)])


def trials_page(live: Html, only: str, names: list[str], csrf: str) -> Html:
    query = f"?i={q(only)}" if only else ""
    body = join([h("<h1>Trials</h1>"), h('<div data-poll="/fragment/trials{q}">{live}</div>', q=query, live=live)])
    return frame("Trials", body, names, csrf, current="/trials")


def experiments_live(cards: list[dict], name: str, names: list[str]) -> Html:
    from .web_experiments import ITEMS
    filt = join(h("{space}{x}", space=" · " if i else "",
                  x=h("<b>{n}</b>", n=n) if n == name else link(f"/experiments?i={q(n)}", n))
                for i, n in enumerate(names))
    body = []
    for card, (item, title, description) in zip(cards, ITEMS):
        command = f"shaft experiment {item} {name} --report" if item != "perturbation" else f"shaft battery report {name}"
        links = join(h(" {x}", x=link(f"/trials/{q(name)}/{q(tid)}", label)) for tid, label in card["links"])
        body.append(h('<article class="card"><h2>{title}</h2><p>{description}</p>'
                      '<p>Status: {status}<br>Last run: {last}<br>Spend: {spend}</p><p>{links}</p>'
                      '<pre>{command}</pre><pre>{report}</pre></article>', title=title, description=description,
                      status=card["status"], last=card["last"], spend=card["spend"], links=links,
                      command=command, report=card["report"]))
    return h('<section class="panel"><p>{filt}</p><p class="dim">Saved results only. Commands shown here never run '
             'from this page. Direct runs may stop without saving an error or a completion marker.</p></section>'
             '<div class="experiments">{cards}</div>', filt=filt, cards=join(body) if names else h("<p>No instances yet.</p>"))


def experiments_page(live: Html, name: str, names: list[str], csrf: str) -> Html:
    query = f"?i={q(name)}" if name else ""
    body = h('<h1>Experiments</h1><div data-poll="/fragment/experiments{q}">{live}</div>', q=query, live=live)
    return frame("Experiments", body, names, csrf, current="/experiments")


def trial_report(report: str) -> Html:
    """The report text. Polled: trial.report formats the trial already loaded, and an unchanged one is a 304."""
    return section("Report (shaft trial show)", pre(report, "wrap"))


def trial_page(t: dict, report: str, names: list[str], csrf: str) -> Html:
    name, tid = t.get("instance", ""), t.get("id", "")
    body = h('<p><a href="/trials">← every trial</a></p><h1>Trial {id} · {inst}</h1>'
             '<div data-poll="/fragment/trials/{n}/{tid}">{r}</div>', id=tid, inst=name, n=q(name), tid=q(tid),
             r=trial_report(report))
    return frame(f"Trial {tid}", body, names, csrf, current="/trials")


# -- instance › benchmarks (TUI tab 5) -------------------------------------------------------------------------------
def benchmarks_page(name: str, b: dict, evs: list[dict], s: dict, names: list[str], csrf: str) -> Html:
    bat = []
    if b.get("off"):
        bat.append(h('<p class="warn">off for {n}: no battery runs, scheduled or by hand (`shaft battery on {n}`)</p>',
                     n=name))
    elif not b["installed"]:
        bat.append(h('<p class="warn">not installed: no items in .supervisor/battery/ — the nightly run does nothing '
                     'until they are copied in (your decision, 2026-10-13)</p>'))
    else:
        bat.append(h('<p class="dim">night window {w} · every {e} night(s) · {due}</p>', w=b["window"],
                     e=b["every_days"], due="due tonight" if b["due"] else "not due"))
    from . import actions
    from .web_actions import button
    why = actions.battery_refusal(name)
    bat.append(button("battery", "Run the battery now", actions.BATTERY_QUESTION.format(name=name), {"name": name},
                      why))
    bat.append(lines(b["lines"], empty=""))
    if b["results"]:
        bat.append(table(["at", "arm", "item", "status", "score"],
                         [[r.get("at", "")[:16], r.get("arm", ""), r.get("item", ""), r.get("status", ""),
                           r.get("score", "")] for r in b["results"][:12]]))
    ev_lines = []
    for e in evs:
        d = e["data"]
        if e["kind"] == "distinct":
            x = (f"{e['at'][:16]} distinct {e['who']}: judge identified {d.get('identified')}/{d.get('n')}, "
                 f"consistent with self {d.get('consistent_with_self')}/{d.get('n')}")
            if d.get("effectiveness_agent") is not None:
                x += f" · effectiveness {d['effectiveness_agent']:.1f} vs base {d.get('effectiveness_base', 0):.1f}"
            if isinstance(d.get("strict"), dict):
                st = d["strict"]
                x += f" · strict {st.get('identified')}/{st.get('n', d.get('n'))}"
        else:
            x = f"{e['at'][:16]} breed {e['who']} · {len(d.get('attempts') or [])} attempt(s)"
        ev_lines.append(x)
    calls, flagged = s.get("tool_calls") or {}, s.get("flagged") or {}
    sur = [f"{kind}: {calls[kind]} tool calls · {flagged.get(kind, 0)} unexpected "
           f"({flagged.get(kind, 0) / calls[kind]:.0%})" if calls[kind] else f"{kind}: no tool calls"
           for kind in sorted(calls)]
    sur += [f"{n} × {cls}" for cls, n in sorted((s.get("flagged_by_class") or {}).items(), key=lambda kv: -kv[1])[:6]]
    body = join([section("Capability battery", join(bat)),
                 section("Blind tests and breeding (shaft distinct / breed)", lines(ev_lines, "none for this instance")),
                 section("Surprises: unexpected tool results (shaft surprise)", lines(sur))])
    return instance_frame(name, "benchmarks", "benchmarks", body, names, csrf)


# -- instance › changes (TUI tab 6) ----------------------------------------------------------------------------------
def changes_page(name: str, d: dict, events: list[dict], merges: list[str], names: list[str], csrf: str) -> Html:
    kept = lines((f"{c['ts'][:16]} {', '.join(c['files'])}" + (f" — {len(c['delegated'])} file(s) by its coder"
                                                               if c["delegated"] else " — by itself"))
                 for c in d["changes"])
    prob = []
    for e in d["probation"]:
        verdict = (e.get("trial") or {}).get("verdict")
        text = (f"{e['commit'][:8]} since {e['since'][:16]} · {', '.join(e['files'])}"
                + (f" · trial: {verdict}" if verdict else ""))
        prob.append(h('<span class="bad">{t} · RESTORE FAILED: {r}</span>', t=text, r=e["restore_failed"])
                    if e.get("restore_failed") else text)
    ev = [h('<span class="{c}">{ts} {t}</span>', ts=e["ts"][5:16], t=e["text"],
            c="bad" if "failed" in e["text"] or "FAILED" in e["text"] else "good" if "passed" in e["text"] else "dim")
          for e in events]
    body = join([section("Kept changes to its own code (passed the test gate)", kept),
                 section("On probation now", lines(prob, "nothing")),
                 section("What probation decided (supervisor log, newest first)", lines(ev)),
                 section("Template updates merged (shaft update)", lines(merges))])
    return instance_frame(name, "changes", "changes", body, names, csrf)


# -- instance › journal ----------------------------------------------------------------------------------------------
JOURNAL_PAGE = 20


def journal_page(name: str, entries: list[str], at: int, names: list[str], csrf: str) -> Html:
    """Newest first, JOURNAL_PAGE entries at a time (`at` = how many newer ones are skipped)."""
    newest = entries[::-1]
    shown = newest[at:at + JOURNAL_PAGE]
    pager = []
    if at > 0:
        pager.append(link(f"/i/{q(name)}/journal?at={max(0, at - JOURNAL_PAGE)}", "← newer"))
    if at + JOURNAL_PAGE < len(newest):
        pager.append(link(f"/i/{q(name)}/journal?at={at + JOURNAL_PAGE}", "older →"))
    info = h('<p class="dim">{n} entries · showing {a}–{b} · memory/journal.md</p>', n=len(newest), a=at + 1 if shown else 0,
             b=at + len(shown))
    arts = join(h('<article class="panel entry">{m}</article>', m=markdown(e)) for e in shown)
    nav_ = h('<p class="pager">{p}</p>', p=join(pager, " · ")) if pager else Html("")
    return instance_frame(name, "journal", "journal", join([info, arts or Html('<p class="dim">no entries</p>'), nav_]),
                          names, csrf)


# -- instance › activity ---------------------------------------------------------------------------------------------
def activity_page(name: str, a: dict, names: list[str], csrf: str) -> Html:
    plugins = (table(["plugin", "scheduled", "description"],
                     [[p["name"], "yes" if p["scheduled"] else "", p["description"]] for p in a["plugins"]])
               if a["plugins"] else Html('<p class="dim">no plugins</p>'))
    body = join([section("Internet (the egress proxy's log, newest last)",
                         join([h('<p>{s}</p>', s=a["internet_today"]), pre(a["egress"] or "no connections logged")])),
                 section(f"Plugins it built ({len(a['plugins'])})", plugins),
                 section("Scheduled jobs (shaft jobs)", pre(a["jobs"])),
                 section("Environment", rows([("python", a["python"]), ("browser", a["browser"]),
                                              ("mind store", a["mind"])]))])
    return instance_frame(name, "activity", "activity", body, names, csrf)


# -- instance › cycles (the transcripts route and tab key) ----------------------------------------------------------
TX_FILTERS = {"": "all", "yours": "for you", "short": "fell short", "delivered": "delivered",
              "conversations": "conversations"}
STYLE_CLASS = {"bold": "b", "dim": "dim", "bold green": "b good", "bold yellow": "b warn", "bold cyan": "b talk-text",
               "cyan": "talk-text", "bold underline": "b u"}


def _cycle_value(value) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return "unknown"
    return str(value)


def _cycle_notes(value, fields: tuple[str, ...]) -> Html:
    from . import cycles
    notes = cycles.note_lines(value, fields)
    return lines(notes) if notes else h("{v}", v="unknown")


def _cycle_verdict(verdict) -> Html:
    return h('<span class="badge {tone}">{v}</span>', tone=MARK.get(verdict, ("?", ""))[1],
             v=_cycle_value(verdict))


def _cycle_filters(name: str, filt: str) -> Html:
    return h('<p>{tabs}</p>', tabs=join([
        link(f"/i/{q(name)}/transcripts" + (f"?f={k}" if k else ""), label) if k != filt
        else h("<b>{l}</b>", l=label) for k, label in TX_FILTERS.items()], " · "))


def _cycle_due(value: str | None) -> str:
    if not value:
        return "due now"
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return value
    return value if len(value) == 10 else when(value)


def cycles_next(plan: dict) -> Html:
    now, check, step = plan["now"], plan["check"], plan["next_step"]
    current = rows([("State", _cycle_value(now["state"]))] +
                   ([("Since", now["since"])] if now["since"] else []) +
                   ([("Goal", _cycle_value(now["goal"]))] if now["goal"] is not None else []))
    checking = rows([("Next check", "paused" if check["paused"] else _cycle_value(check["next"]))] +
                    ([("Last skipped check", check["skipped"])] if check["skipped"] else []))
    said = (rows([("From cycle", when(step["ts"])), ("Action", step["action"])] +
                 ([("Revise if", step["revise_if"])] if step["revise_if"] else [])) if step else
            h('<p class="dim">no next action recorded</p>'))
    commitments = lines([h('{p} · due {d} · <span class="{c}">{s}</span>',
                          p=it["promise"], d=when(it["due"]) if it["due"] else "unknown", s=it["status"],
                          c="warn" if it["status"] == "overdue" else "") for it in plan["commitments"]],
                        "no open commitments", cls="lines wrap")
    if plan["commitments_error"]:
        commitments = h('<p class="warn">{e}</p>', e=plan["commitments_error"])
    queue = plan["queue"]
    if queue["state"] == "unreadable":
        tasks = h('<p class="warn">{e}</p>', e=queue["error"])
    elif queue["state"] == "none":
        tasks = h('<p class="dim">no task queue</p>')
    else:
        tasks = lines([h('{t} · {d}{late}', t=it["task"], d=_cycle_due(it["due_at"]),
                         late=" · overdue" if it["overdue"] else "") for it in queue["rows"]],
                      "no open tasks", cls="lines wrap")
        if queue["open"] > len(queue["rows"]):
            tasks = join([tasks, h('<p>{n} more</p>', n=queue["open"] - len(queue["rows"]))])
    goals = (h('<p class="warn">{e}</p>', e=plan["goals_error"]) if plan["goals_error"] else
             markdown(plan["goals"]) if plan["goals"].strip() else h('<p class="dim">goals file is empty</p>'))
    improvement = plan["improvement"]
    improve = (join([h('<p>{ts}</p>', ts=when(improvement["ts"])), markdown(improvement["text"])]) if improvement else
               h('<p class="dim">no improvement plan recorded</p>'))
    if plan["improvement_error"]:
        improve = h('<p class="warn">{e}</p>', e=plan["improvement_error"])
    return join([h('<h3>Now</h3>{now}{check}<h3>Said it would do next</h3>{said}<h3>Unfinished</h3>{left}'
                   '<h3>Commitments</h3>{commitments}<h3>Task queue</h3>{tasks}',
                   now=current, check=checking, said=said,
                   left=lines(plan["unfinished"], "nothing recorded", cls="lines wrap"),
                   commitments=commitments, tasks=tasks),
                 h('<details data-key="cycles-goals"><summary>Goals</summary>{goals}</details>'
                   '<details data-key="cycles-improvement"><summary>Improvement plan</summary>{improve}</details>',
                   goals=goals, improve=improve)])


def cycles_page(name: str, plan: dict, days: dict, filt: str, names: list[str], csrf: str) -> Html:
    done = []
    if days["judge_error"]:
        done.append(h('<p class="warn">{e}</p>', e=days["judge_error"]))
    for day in days["days"]:
        counts = day["counts"]
        count_text = (f"{counts['cycles']} {'cycle' if counts['cycles'] == 1 else 'cycles'} · "
                      f"{counts['yes']} yes · {counts['partial']} partial · "
                      f"{counts['no']} no (claimed) · {counts['judged']} judged · {counts['for_you']} for you")
        done.append(h('<h3>{date}</h3><p>{counts}</p>', date=day["date"], counts=count_text))
        if day["skipped_lines"]:
            done.append(h('<p class="warn">{n} skipped lines</p>', n=day["skipped_lines"]))
        body = []
        for row in day["rows"]:
            live = row["kind"] == "cycle"
            claim = (f"claimed {row['claim']}" if row["claim"] is not None else "no finish") if live else ""
            body.append([link(f"/i/{q(name)}/transcripts?ts={q(row['id'])}", when(row["ts"])), row["kind"],
                         h('<span class="clip">{g}</span>',
                           g=row["goal"] or ("no intent recorded" if live else row["kind"])),
                         claim, _cycle_verdict(row["verdict"]) if row["verdict"] is not None else "",
                         "delivered" if row["delivered"] else ""])
        done.append(table(["End time", "Kind", "Goal", "Claim", "Judge", "Delivery"], body, cls="cycles-done"))
    if not days["days"]:
        done.append(h('<p class="dim">{empty}</p>',
                      empty="no cycles match this filter" if filt else "no cycles yet"))
    if days["next"]:
        done.append(link(f"/i/{q(name)}/transcripts?before={q(days['next'])}" + (f"&f={q(filt)}" if filt else ""),
                         "Earlier days"))
    body = join([section("Next", h('<div data-poll="/fragment/i/{n}/cycles-next" data-every="15000">{next}</div>',
                                   n=q(name), next=cycles_next(plan))),
                 section("Done", join([_cycle_filters(name, filt), *done]))])
    return instance_frame(name, "transcripts", "cycles", body, names, csrf)


def cycle_page(name: str, cycle: dict, names: list[str], csrf: str) -> Html:
    back = link(f"/i/{q(name)}/transcripts", "← Cycles")
    raw = link(f"/i/{q(name)}/transcript?id={q(cycle['where'])}", "Raw transcript")
    body = [h('<p>{back} · {raw}</p><h2>{kind} {ts}</h2>',
              back=back, raw=raw, kind=cycle["kind"], ts=when(cycle["ts"]))]
    did = cycle["did"]
    did_fields = [("What happened", "what_happened"), ("Duration (seconds)", "duration_s"), ("Cost (USD)", "usd"),
                  ("Tool calls", "tool_calls"), ("Ended by", "ended_by"), ("Wrapped up", "wrapped_up"),
                  ("Delivered via", "delivered_via")]
    if cycle["kind"] != "cycle":
        content = cycle["content"]
        body += [section("Content", markdown(content) if content else h('<p class="dim">no content recorded</p>')),
                 section("Did", rows([(label, _cycle_value(did[key])) for label, key in did_fields[3:5]]))]
    else:
        planned = cycle["planned"]
        planned_fields = [("Goal", "goal"), ("Success criterion", "success_criterion"), ("Origin", "origin"),
                          ("Origin reference", "origin_ref"), ("Confidence", "confidence")]
        body.append(section("Planned", rows([(label, _cycle_value(planned[key])) for label, key in planned_fields] +
                                            [("Predictions", lines([p for p in planned["predictions"]
                                                                    if isinstance(p, str)]
                                                                   if isinstance(planned["predictions"], list) else [],
                                                                   "none recorded"))] +
                                            ([("Attention", _cycle_notes(planned["attention"], ("source", "why")))]
                                             if planned["attention"] is not None else [])) if planned else
                            h('<p class="dim">no intent recorded</p>')))
        body.append(section("Did", rows([(label, _cycle_value(did[key])) for label, key in did_fields])))
        result = cycle["result"]
        judgments = [h('<p>{claim}</p>',
                       claim=f"claimed {result['claim']}" if result["claim"] is not None else "no finish")]
        if result["judge_error"]:
            judgments.append(h('<p class="warn">{e}</p>', e=result["judge_error"]))
        for key, label in [("judge", "Judge"), ("second", "Second judge")]:
            judge = result[key]
            if judge is None:
                if key == "judge":
                    judgments.append(h('<p class="dim">not judged</p>'))
                continue
            judgments.append(h('<h3>{label}</h3>', label=label))
            if judge["error"]:
                judgments.append(h('<p class="warn">{e}</p>', e=judge["error"]))
                continue
            judgments.append(h('<p>{verdict}</p>', verdict=_cycle_verdict(judge["verdict"])))
            judgments.append(rows([(label, _cycle_value(judge[key])) for label, key in
                                   [("Claim supported", "claim_supported"), ("First error", "first_error"),
                                    ("Reasoning", "reasoning"), ("Model", "model")]]))
        judgments.append(rows([("Surprises", _cycle_notes(result["surprises"], ("expected", "observed", "so_what"))),
                               ("Doubts", _cycle_notes(result["doubts"], ("step", "what")))]))
        body.append(section("Result", join(judgments)))
        left = cycle["left"]
        nxt = left["next_action"]
        remaining = [("Next action", _cycle_value(nxt.get("action") if nxt else None))]
        if nxt and nxt.get("revise_if"):
            remaining.append(("Revise if", nxt["revise_if"]))
        remaining.append(("The next cycle's goal",
                          _cycle_value(left["next_goal"] if left["next_goal"] is not None else "no later cycle yet")))
        body.append(section("Left", join([h('<h3>Unfinished</h3>{items}',
                                            items=lines(left["unfinished"], "nothing recorded", cls="lines wrap")),
                                            rows(remaining)])))
    return instance_frame(name, "transcripts", "cycle", join(body), names, csrf)


def transcripts_page(name: str, es: list[dict], filt: str, summary, live: dict, names: list[str], csrf: str) -> Html:
    body = [[link(f"/i/{q(name)}/transcript?id={q(e['id'])}", when(e["ts"])),
             h('<span class="kind-{k}">{k}</span>', k=e["kind"]), clip(summary(e))] for e in es]
    listing = table(["when", "kind", "summary"], body) if body else h('<p class="dim">no conversations yet</p>')
    return instance_frame(name, "transcripts", "cycles", join([_cycle_filters(name, filt),
                                                               section("Conversations", listing)]), names, csrf)


def transcript_page(name: str, e: dict, tx_lines: list[tuple[str, str]], thinking: bool, names: list[str],
                    csrf: str) -> Html:
    toggle = Html("")
    if e["kind"] == "conversation":
        toggle = link(f"/i/{q(name)}/transcript?id={q(e['id'])}" + ("" if thinking else "&thinking=1"),
                      "hide thinking" if thinking else "show thinking")
    body_lines = join(h('<div class="{c}">{t}</div>', c=STYLE_CLASS.get(style, ""), t=text or " ")
                      for text, style in tx_lines)
    body = h('<p><a href="/i/{n}/transcripts">← every transcript</a> {toggle}</p>'
             '<section class="panel transcript">{b}</section>', n=q(name), toggle=toggle, b=body_lines)
    return instance_frame(name, "transcripts", "transcript", body, names, csrf)


# -- messages (TUI tab 7) --------------------------------------------------------------------------------------------
def _first_line(text: str) -> str:
    """A message's first line as its page shows it: the text, not its Markdown."""
    from .web_md import first_line
    return first_line(text)


def messages_page(ms: list[dict], who: str, people: list[str], names: list[str], csrf: str) -> Html:
    shown = [m for m in ms if not who or who in (m["from"], m["to"])][::-1]
    filt = join([link("/messages", "all") if who else Html("<b>all</b>")] +
                [h(" · {x}", x=link(f"/messages?who={q(p)}", p) if p != who else h("<b>{p}</b>", p=p)) for p in people])
    body = []
    for m in shown:
        kind = m.get("kind") if m.get("kind") not in (None, "", "note", "message") else ""
        att = f" 📎{len(m['attachments'])}" if m.get("attachments") else ""
        body.append(([link(f"/messages/{q(m['file'])}", when(m["ts"])), f"{m['from']} → {m['to']}",
                      h('<span class="pending" title="still in the inbox">●</span>') if not m["read"] else "",
                      h('<span class="kind">{k}</span>', k=kind) if kind else "", clip(_first_line(m["text"]) + att)],
                     "unread" if not m["read"] else ""))
    listing = (table(["when", "who", "", "kind", "first line"], body, "messages") if body else
               h('<p class="dim">no messages yet{w}</p>', w=f" with {who}" if who else ""))
    head = h('<section class="panel"><p>{f}</p><p class="dim">{n} shown · newest first · ● still in the recipient\'s '
             'inbox</p></section>', f=filt, n=len(shown))
    return frame("Messages", join([h("<h1>Messages</h1>"), head, listing]), names, csrf, current="/messages")


IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".gif", ".webp")
MARKDOWN_TYPES = (".md", ".markdown")               # also SHAFTKit's MarkdownFile
TEXT_TYPES = (*MARKDOWN_TYPES, ".txt", ".csv", ".json", ".log", ".py", ".yaml", ".yml", ".toml", ".html", ".xml")


def text_view(file_name: str, text: str) -> Html:
    """An attachment's text as it reads: Markdown rendered, any other text type as written."""
    return markdown(text) if file_name.lower().endswith(MARKDOWN_TYPES) else pre(text, "wrap")


def message_page(m: dict, texts: dict, names: list[str], csrf: str) -> Html:
    """One message in full. `texts`: attachment index -> its text, for attachments small enough and UTF-8."""
    status = f"read by {m['to']}" if m["read"] else f"pending in {m['to']}'s inbox"
    atts = []
    for k, a in enumerate(m.get("attachments") or []):
        url = f"/files/{q(m['file'])}/{k}"
        item = [h('<p><a href="{u}">{n}</a> <span class="dim">({b} bytes)</span></p>', u=url, n=a["name"], b=a["bytes"])]
        if a["name"].lower().endswith(IMAGE_TYPES):
            item.append(h('<img class="attachment" src="{u}" alt="{n}">', u=url, n=a["name"]))
        elif k in texts:
            item.append(h('<div class="docmd">{t}</div>', t=text_view(a["name"], texts[k])))
        atts.append(h('<div class="att">{i}</div>', i=join(item)))
    alert = (h('<p class="dim">notified on {c}: {why}</p>', c="the web UI" if m["alert"] == "web" else "Telegram",
               why=m.get("alert_why", "")) if m.get("alert") else Html(""))      # channel.py: where it alerted you
    from . import feedback
    rating = (_feedback_buttons(m["from"], {"kind": "message", "file": m["file"]}, feedback.by_target(m["from"]))
              if m["from"] in names and m["to"] == "operator" else Html(""))
    body = h('<p><a href="/messages">← every message</a></p><h1>{who}</h1>'
             '<p class="dim">{ts} · {kind} · {status} · {file}</p>{alert}'
             '<section class="panel">{text}{feedback}</section>{atts}',
             who=f"{m['from']} → {m['to']}", ts=m["ts"].replace("T", " "), kind=m.get("kind") or "note", status=status,
             file=m["file"], text=pre(m["text"], "wrap"), alert=alert, feedback=rating,
             atts=section(f"Attachments ({len(atts)})", join(atts)) if atts else Html(""))
    return frame("Message", body, names, csrf, current="/messages")


# -- costs (TUI tab 8) -----------------------------------------------------------------------------------------------
CAP_ALERT = 0.8


def _bar(spent: float, cap: float) -> Html:
    frac = min(1.0, spent / cap) if cap else 0.0
    tone = "bad" if frac >= CAP_ALERT else "warn" if frac >= 0.5 else "good"
    return h('<span class="bar"><span class="fill {t} w{p}"></span></span> <span class="{t}">${s} / ${c}</span>',
             t=tone, p=round(frac * 10) * 10, s=f"{spent:.2f}", c=f"{cap:.2f}")


def costs_page(c: dict, names: list[str], csrf: str) -> Html:
    today = c["today"]
    parts = [f"instances ${sum(today['instances'].values()):.2f}", f"trials ${today['trials']:.2f}",
             f"judge ${today['judge']:.2f}", f"coder ${today['coder']:.2f}"]
    if c.get("grok_api_equivalent"):
        parts.append(f"Grok plan: ${c['grok_api_equivalent']:.6f} API equivalent (not money spent)")
    if (rate := c.get("codex_rate")) is not None:
        parts.append(f"Codex plan: {rate['used_percent']}% of weekly allowance used")
    if today["recaps"]:
        parts.append(f"recaps ${today['recaps']:.2f}")
    if today["other"]:
        parts.append(f"other ${today['other']:.2f}")
    head = h('<section class="panel"><h2>Today ${t}</h2><p>{p}</p></section>', t=f"{today['total']:.2f}",
             p=" · ".join(parts))
    caps = table(["instance", "cloud model", "coder", "its trials"],
                 [[cap["name"], _bar(*cap["cloud"]), _bar(*cap["coder"]), _bar(*cap["trials"])] for cap in c["caps"]])
    judge = " · ".join(f"{k} ${v:.2f}" for k, v in sorted(c["judge_today"].items(), key=lambda kv: -kv[1]))
    jt = h('<p class="{cls}">{j}</p>', j=judge or "nothing yet",
           cls="bad" if c["judge_today_total"] >= c["judge_alert"] else "")
    if c["unpriced"]:
        jt = join([jt, h('<p class="warn">{n} unpriced model call(s) are not counted in dollar totals</p>',
                         n=c["unpriced"])])
    days = table(["day", "total", "instances", "trials", "judge", "coder"],
                 [[d["day"], f"${d['total']:.2f}", f"${sum(d['instances'].values()):.2f}", f"${d['trials']:.2f}",
                   f"${d['judge']:.2f}", f"${d['coder']:.2f}"] for d in reversed(c["days"])], "num")
    body = join([h("<h1>Costs</h1>"), head, section("Today against each cap", caps),
                 section(f"Judge today ${c['judge_today_total']:.2f} (no cap; alert at ${c['judge_alert']:.2f})", jt),
                 section("Last 7 days", days)])
    return frame("Costs", body, names, csrf, current="/costs")


# -- services (TUI tab 9) --------------------------------------------------------------------------------------------
def services_live(svcs: list[dict], order: list[str], runs: list[str], busy: list[str]) -> Html:
    from . import version
    from .web_actions import autonomy_button, service_buttons
    by = {s["name"]: s for s in svcs}
    body = []
    for name in order:
        s = by.get(name, {"running": False, "detail": ""})
        body.append(([h('<a href="/services?log={n}">{n}</a>', n=name),
                      h('<span class="{c}">{t}</span>', c="good" if s["running"] else "dim" if s.get("idle") else "bad",
                        t="● running" if s["running"] else "○ idle" if s.get("idle") else "○ NOT RUNNING"),
                      f"pid {s['pid']}" if s.get("pid") else "",
                      s.get("version", "unknown — restart to record"), s.get("detail", ""),
                      h('<span class="warn">older code than checked out: restart it</span>') if s.get("stale") else "",
                      service_buttons(name, s["running"], runs, busy)],
                     ""))
    sched = by.get("scheduler", {}).get("running", False)
    return join([h('<p class="dim">checked out: {v}</p>', v=version.commit_describe(version.shown_commit())),
                 table(["service", "state", "pid", "version", "detail", "", ""], body),
                 h('<p class="{c}">Runs a scheduler restart would kill: {r}</p>', c="warn" if runs else "dim",
                   r=", ".join(runs) or "none"),
                 h('<div class="actions">{b}</div>', b=autonomy_button(sched, runs))])


def services_log(sel: str, log_name: str, tail: list[str]) -> Html:
    """One service's log, last lines only. panel.log_tail reads the last 8 KB (not the file), so polling this is
    cheap, and an unchanged tail is a 304. A full-file poll would not be."""
    return section(f"{sel} log (.supervisor/{log_name})", pre("\n".join(tail) or "(empty)"))


def services_page(live: Html, sel: str, log_name: str, tail: list[str], names: list[str], csrf: str) -> Html:
    body = join([h("<h1>Services</h1>"), h('<section class="panel"><div data-poll="/fragment/services">{l}</div>'
                                           '</section>', l=live),
                 h('<div data-poll="/fragment/services/log?log={s}">{log}</div>', s=sel,
                   log=services_log(sel, log_name, tail))])
    return frame("Services", body, names, csrf, current="/services")


# -- instance › chat (M4) ----------------------------------------------------------------------------------------------
def file_url(path: str) -> str:
    """Where the web UI serves a feed item's file (web_chat.build's `path`): /chatfiles/<cid>/<k>/<j> for one sent
    with a turn, /files/<message file>/<j> for one a logged message carries."""
    kind, _, rest = path.partition("/")
    if kind == "chat":
        cid, _, kj = rest.partition("/")
        return f"/chatfiles/{q(cid)}/{kj}"
    file, _, j = rest.rpartition("/")
    return f"/files/{q(file)}/{j}"


def _chat_attachments(atts: list[dict]) -> Html:
    """The files the operator sent with a turn (web_chat.build's `attachments`): a picture, or 📎 its name."""
    out = []
    for a in atts:
        url = file_url(a["path"])
        if a["image"]:
            out.append(h('<a href="{u}" target="_blank"><img class="thumb" src="{u}" alt="{n}" width="192" height="144" loading="lazy"></a>',
                         u=url, n=a["name"]))
        else:
            out.append(h('<a class="file" href="{u}" target="_blank">📎 {n}</a>', u=url, n=a["name"]))
    return h('<div class="atts">{o}</div>', o=join(out)) if out else Html("")


def _activity(lines_: list[str], key: str, open_: bool = False, item: str = "", ended: bool = False,
              now: bool = False) -> Html:
    """What it did during a reply, collapsible; `key` keeps it open or shut across the feed's refreshes. `item`: its
    data-k, when it is an item of a keyed poll itself (live_activity): unkeyed, morph replaced it whole each second
    and its log jumped back to the top. `now`: the reply is running, and the one line says what it is doing now."""
    if not lines_:
        return Html("")
    label = (h('<span class="working">{x}</span> <span class="dim">· {n} steps</span>', x=_one_line(lines_[-1]), n=len(lines_))
             if now else h("what it did ({n})", n=len(lines_)))
    return h('<details class="activity"{i}{e} data-key="{k}"{o}><summary>{label}</summary><div data-scroll-key="{k}">{l}</div></details>',
             i=h(' data-k="{i}"', i=item) if item else "", e=Html(" data-ended") if ended else "", k=key, o=Html(" open") if open_ else "",
             label=label, l=join(h("<p>{x}</p>", x=x) for x in lines_))


def _one_line(text: str, limit: int = 90) -> str:
    line = " ".join(str(text).split())
    return line[:limit - 1] + "…" if len(line) > limit else line


def _my_text(text: str) -> Html:
    """What the operator wrote, as written — except the lines they quoted (Reply on a selection), shown as a quote,
    and their fenced code (```), shown as code the way an answer shows it (the PWA showed the backticks)."""
    blocks: list[tuple[bool, list[str]]] = []               # (quoted, lines), runs of one kind
    for line in str(text).replace("\r\n", "\n").split("\n"):
        quoted = bool(_QUOTE.match(line))
        if not blocks or blocks[-1][0] != quoted:
            blocks.append((quoted, []))
        blocks[-1][1].append(_QUOTE.sub("", line, count=1) if quoted else line)
    out = []
    for quoted, b in blocks:
        parts = _verbatim(b, Html("{t}") if quoted else Html('<div class="wrap">{t}</div>'))
        if parts and quoted:
            out.append(h('<blockquote class="wrap">{t}</blockquote>', t=join(parts)))
        else:
            out.extend(parts)
    return join(out)


def _verbatim(lines: list[str], text_tag: Html) -> list[Html]:
    """Lines as written, each run of text in text_tag; a fenced code block among them as code (web_md.fenced)."""
    from .web_md import fenced
    parts: list[Html] = []
    run: list[str] = []

    def flush() -> None:
        if body := "\n".join(run).strip("\n"):
            parts.append(h(text_tag, t=body))
        run.clear()
    i = 0
    while i < len(lines):
        if code := fenced(lines, i):
            flush()
            i, rendered = code
            parts.append(Html(rendered))
        else:
            run.append(lines[i])
            i += 1
    flush()
    return parts


def doc_cards(made: list[dict] | None, current_chat: str = "") -> Html:
    """The documents a reply created or changed: each opens in the documents panel."""
    if not made:
        return Html("")
    if current_chat:
        return h('<div class="doccards">{c}</div>', c=join(
            h('<a class="doccard" href="/i/{n}/chat?doc={u}">📄 {p} <span class="dim">'
              'Open in the current chat to edit or comment</span></a>', n=q(current_chat), u=q(d["path"]),
              p=d["path"]) for d in made))
    return h('<div class="doccards">{c}</div>', c=join(
        h('<a class="doccard" href="?doc={u}" data-open-doc="{p}">📄 {p} <span class="dim">— {what}</span></a>',
          u=q(d["path"]), p=d["path"], what="created" if d.get("created") else "changed") for d in made))


def in_chat(chat: dict | None) -> set[str]:
    """The documents this chat's replies created or changed."""
    return {d["path"] for t in (chat or {}).get("turns", []) for d in t.get("docs") or []}


def overwritten(saved: dict | None, sha: str, text: str | None) -> dict | None:
    """Lines the operator wrote (`saved`: the chat's saved_docs entry) that a later write of the instance dropped:
    {lines: how many, ts: when they saved, diff: their version against now, text: their version}; None when none."""
    import difflib
    if not saved or text is None or saved.get("sha") == sha:
        return None
    now = {l.strip() for l in text.splitlines()}
    lost = [l for l in saved.get("added") or [] if l not in now]
    if not lost:
        return None
    return {"lines": len(lost), "ts": saved.get("ts", ""), "text": saved["text"],
            "diff": "\n".join(difflib.unified_diff(saved["text"].splitlines(), text.splitlines(), "yours", "now",
                                                   lineterm="", n=1))}


def _overwritten(saved: dict | None, sha: str, text: str | None) -> Html:
    """overwritten(), said, with their version to put back."""
    from . import actions
    from .web_actions import post_attrs
    lost = overwritten(saved, sha, text)
    if not lost:
        return Html("")
    return h('<div class="overwritten warn"><p>{n} line(s) you wrote at {ts} are no longer in it.</p>'
             '<details><summary>Your version against now</summary><pre class="diff">{d}</pre></details>'
             '<form class="act" data-post="/docs/{i}/save" data-confirm="Put your version back? It replaces the '
             'current one: its changes since are lost (they are in the diff)." data-after="doc-saved"{a}>'
             '<input type="hidden" name="path" value="{p}"><input type="hidden" name="sha" value="{sha}">'
             '<textarea name="text" hidden>{yours}</textarea><button type="submit" class="small">Restore yours'
             '</button></form></div>', n=lost["lines"], ts=lost["ts"][11:16], d=lost["diff"], i=q(saved.get("name", "")),
             p=saved.get("path", ""), sha=sha, yours=lost["text"],
             a=post_attrs("Documents", actions.RUN_NOW_S))


def threads_list(name: str, doc: str, sha: str, threads: list[dict], active: str = "") -> Html:
    """Open threads on one document, resolved ones folded under them. Nothing when there are no threads."""
    from . import doc_comments
    from .web_actions import post_attrs
    if not threads:
        return Html("")

    def post(p: dict) -> Html:
        # Unsent: not sent as a message nor answered yet. The instance can read it in notes/comments/, no reply starts.
        unsent = not doc_comments.delivered(p)
        who = "you" if p["by"] == "operator" else p["by"]
        buttons = Html("")
        if unsent:
            buttons = join([
                h('<button type="button" class="small" data-edit-post="{pid}">Edit</button>', pid=p["pid"]),
                h('<form class="act" data-post="/docs/{n}/comment/delete" data-confirm="Delete this comment?" '
                  'data-after="comment-saved"{a}><input type="hidden" name="pid" value="{pid}">'
                  '<button type="submit" class="small">Delete</button></form>', n=q(name), pid=p["pid"],
                  a=post_attrs("Documents")),
                h('<form class="act" data-post="/docs/{n}/comment/send" data-after="comment-saved"{a}>'
                  '<input type="hidden" name="pid" value="{pid}">'
                  '<button type="submit" class="small">Send to {who}</button></form>',
                  n=q(name), pid=p["pid"], who=name, a=post_attrs("Documents")),
                # for its next cycle: to its inbox, not answered now
                h('<form class="act" data-post="/docs/{n}/comment/send" data-after="comment-saved"{a}>'
                  '<input type="hidden" name="pid" value="{pid}"><input type="hidden" name="when" value="cycle">'
                  '<button type="submit" class="small" title="Not answered now: {who} reads it in its next cycle">'
                  'Next cycle</button></form>', n=q(name), pid=p["pid"], who=name, a=post_attrs("Documents")),
            ])
        # Where it went, when not as a chat message: its inbox for the next cycle, or the message that answered it
        ref = p.get("sent") or p.get("turn") or ""
        how = (h(' <a class="laterchip" href="/messages/{f}">⏾ for its next cycle</a>', f=q(ref[6:]))
               if ref.startswith("inbox/") else
               h(' <a class="laterchip" href="/messages/{f}">in a message</a>', f=q(ref[8:]))
               if ref.startswith("message:") else Html(""))
        return h('<div class="post{u}" data-pid="{pid}" data-text="{raw}"><div class="who">{who} <span class="dim">{ts}</span>{warn}{how}'
                 '</div>{text}{btns}</div>',
                 u=Html(" unsent") if unsent else "", pid=p["pid"], who=who, ts=p["ts"][11:16],
                 warn=Html(' <span class="warn">not sent</span>') if unsent else "", how=how,
                 text=markdown(p["text"]), btns=buttons, raw=p["text"])

    def one(t: dict) -> Html:
        by = h(' <span class="dim">by {by}</span>', by=t["by"]) if t["by"] != "operator" else Html("")
        anchor = t["anchor"]
        if anchor is None:
            quote, detached = Html(' <span class="dim">the whole document</span>'), Html("")
        else:
            exact = anchor["exact"]
            if len(exact) > 300:
                exact = exact[:300] + "…"
            quote = h(' <q>{t}</q>', t=exact)
            detached = Html('<p class="dim detached" hidden data-detached-note>text no longer in the document</p>')
        changed = (Html('<p class="dim">the document changed since this comment</p>')
                   if t["sha"] != sha else Html(""))
        refs = join(h('<p class="dim"><a href="#" data-chat-turn="{ref}">answered in the chat, without a section '
                      'for {tid} →</a></p>', ref=ref, tid=t["id"]) for ref in t.get("unsectioned") or [])
        resolving = t["state"] == "resolved"
        return h('<article class="thread{on}" id="thread-{tid}" data-thread="{tid}"><b>{tid}</b>{by}{quote}{det}'
                 '{chg}{posts}{refs}<button type="button" class="small" data-reply-thread="{tid}">Reply</button>'
                 '<form class="act" data-post="/docs/{n}/comment/state" data-after="comment-saved"{a}>'
                 '<input type="hidden" name="thread" value="{tid}"><input type="hidden" name="state" value="{st}">'
                 '<button type="submit" class="small">{label}</button></form></article>',
                 on=Html(" active") if t["id"] == active else "", tid=t["id"], by=by, quote=quote, det=detached,
                 chg=changed, posts=join(post(p) for p in t["posts"]), refs=refs, n=q(name),
                 a=post_attrs("Documents"),
                 st="open" if resolving else "resolved", label="Reopen" if resolving else "Resolve")

    opened = [t for t in threads if t["state"] != "resolved"]
    done = [t for t in threads if t["state"] == "resolved"]
    body = join(one(t) for t in opened)
    if done:
        body = join([body, h('<details class="resolved" data-key="resolved-{doc}"><summary>{m} resolved</summary>'
                             '{items}</details>', doc=doc, m=len(done), items=join(one(t) for t in done))])
    summary = (h('💬 {n} open, {m} resolved', n=len(opened), m=len(done)) if done
               else h('💬 {n} open', n=len(opened)))
    return h('<details class="threads" data-key="threads-{doc}"{o}><summary>{s}</summary>{body}</details>',
             doc=doc, o=Html(" open") if active else "", s=summary, body=body)


def docs_panel(name: str, listing: list[dict], open_path: str, text: str | None, sha: str, marked: set[str],
               problem: str = "", saved: dict | None = None, threads: list[dict] | None = None,
               counts: dict | None = None, thread: str = "", archived: bool = False, folder: str = "",
               sort: str = DOC_SORT_DEFAULT, chat_id: str = "") -> Html:
    """The documents panel beside the chat (/fragment/docs/<name>): the list of one folder (`folder`, "" for notes/docs/
    itself: its subfolders, then its documents, each in the `sort` order of DOC_SORTS) or the open one rendered, whose ←
    goes back to its folder. A folder's size is all its documents', its time its latest document's."""
    from . import actions
    from .docs import DIR, MAX_BYTES
    from .web_actions import post_attrs
    if open_path:
        folder = open_path.rpartition("/")[0]
    prefix = folder + "/" if folder else ""
    subs: dict[str, dict] = {}
    here_docs = []
    for d in listing:
        if not d["path"].startswith(prefix):
            continue
        rest = d["path"][len(prefix):]
        n = 0 if counts is None else counts.get(d["path"]) or 0
        if "/" in rest:
            s = subs.setdefault(rest.partition("/")[0], {"docs": 0, "marked": False, "comments": 0, "bytes": 0, "mtime": 0.0})
            s["docs"] += 1
            s["marked"] |= d["path"] in marked
            s["comments"] += n
            s["bytes"] += d["bytes"]
            s["mtime"] = max(s["mtime"], d["mtime"])
            continue
        here_docs.append((rest, d, n))
    key, reverse = DOC_SORTS[doc_sort(sort)]
    # By name first, then by the key: sorted() is stable (reversed too), so equal sizes or times stay A→Z.
    by = (lambda name, row: name.casefold()) if key == "name" else (lambda name, row: row[key])
    here_docs = sorted(sorted(here_docs, key=lambda x: x[0].casefold()), key=lambda x: by(x[0], x[1]), reverse=reverse)
    folder_rows = sorted(sorted(subs.items(), key=lambda x: x[0].casefold()), key=lambda x: by(x[0], x[1]),
                         reverse=reverse)
    items = []
    for rest, d, n in here_docs:
        big = d["sha"] == "too-big"
        items.append(h('<a href="?doc={u}" data-open-doc="{p}"><span>{dot}{f}{ccount}{big}</span>'
                       '<span class="dim docsize" title="{b} bytes">{size}</span>{when}</a>',
                       u=q(d["path"]), p=d["path"], f=rest, b=f"{d['bytes']:,}", size=size_label(d["bytes"]),
                       when=when_label(d["mtime"]), dot=_DOT if d["path"] in marked else "",
                       ccount=h(' <span class="ccount" title="open comments">💬 {n}</span>', n=n) if n > 0 else "",
                       big=h(' <span class="bad toobig" title="over {m}: it cannot be shown or edited here, only downloaded">'
                             'too big to show</span>', m=size_label(MAX_BYTES)) if big else ""))
    folders = [h('<a href="?dir={u}" data-open-dir="{p}" class="folder"><span>{dot}📁 {f} '
                 '<span class="dim">· {k} document{s}</span>{ccount}</span>'
                 '<span class="dim docsize" title="{b} bytes in all">{size}</span>{when}</a>',
                 u=q(prefix + f), p=prefix + f, f=f, k=s["docs"], s="" if s["docs"] == 1 else "s",
                 b=f"{s['bytes']:,}", size=size_label(s["bytes"]), when=when_label(s["mtime"]),
                 dot=_DOT if s["marked"] else "",
                 ccount=h(' <span class="ccount" title="open comments">💬 {n}</span>', n=s["comments"])
                 if s["comments"] else "") for f, s in folder_rows]
    inside = h(' in {f}/', f=folder) if folder else ""
    new = h('<details class="newdoc" data-key="newdoc"><summary>New document</summary><form data-post="/docs/{n}/save" '
            'data-after="doc-saved"{a}><input type="hidden" name="sha" value="none">'
            '<input type="hidden" name="dir" value="{dir}"><label>Document name{inside} '
            '<input name="path" required placeholder="plan, or folder/plan" '
            'pattern="[A-Za-z0-9][A-Za-z0-9 _.\\-]*(/[A-Za-z0-9][A-Za-z0-9 _.\\-]*)*" maxlength="80"></label>'
            '<textarea name="text" hidden></textarea><button type="submit" class="small">Create</button></form></details>',
            n=q(name), dir=folder, inside=inside, a=post_attrs("Documents", actions.RUN_NOW_S))
    parent, _, here = folder.rpartition("/")
    if open_path:
        title = h('<button type="button" class="small" data-back-docs data-dir="{f}" aria-label="Back to {to}">← {w}'
                  '</button> <span class="dim">{dir}/</span>', f=folder, w=here or "Documents",
                  to=f"{folder}/" if folder else "the documents", dir=DIR)
    elif folder:                                   # Documents / a / b: each one above is a link up
        steps = folder.split("/")
        crumbs = [h('<a href="?dir=" data-open-dir="">Documents</a>')] + [
            h('<a href="?dir={u}" data-open-dir="{p}">{s}</a>', u=q("/".join(steps[:k + 1])), p="/".join(steps[:k + 1]),
              s=s) for k, s in enumerate(steps[:-1])] + [h('<b>{s}</b>', s=here)]
        title = h('<button type="button" class="small" data-back-docs data-dir="{p}" aria-label="Up to {w}">←</button> '
                  '<span class="crumbs">{c}</span>', p=parent, w=f"{parent}/" if parent else "the documents",
                  c=join([crumbs[0]] + [h(' / {c}', c=c) for c in crumbs[1:]]))
    else:
        title = h('<b>Documents</b> <span class="dim">{dir}/</span>', dir=DIR)
    entries = folders + items
    empty = (h('<p class="dim">No documents in {f}/ (any more).</p>', f=folder) if folder else
             h('<p class="dim">No current files.</p>') if archived else
             h('<p class="dim">none yet: ask {n} for one, it appears here as it writes it.</p>', n=name))
    expand = FULLSCREEN if open_path and text is not None and not problem else Html("")
    head = h('<div class="docshead">{title}{new}{expand}<button type="button" '
             'class="small closedocs" data-close-docs aria-label="Close the documents">{x}</button></div>{list}',
             x=_CLOSE, title=title, expand=expand, new="" if open_path or archived else new,
             list="" if open_path else h('{s}<div class="doclist">{i}</div>', i=join(entries) if entries else empty,
                                                    s=_sort_bar(doc_sort(sort), folder, chat_id) if len(entries) > 1 else ""))
    download = (download_icon(f"/i/{q(name)}/file?path={q(DIR + '/' + open_path)}")
                if open_path and text is not None and not problem else Html(""))
    too_big = open_path and problem and any(d["path"] == open_path and d["sha"] == "too-big" for d in listing)
    refusal = h('<p class="bad">{p}</p>{d}', p=problem, d=h('<p>{i} <a href="/i/{n}/file?path={u}">Download {f}</a></p>',
                i=ICONS["download"], n=q(name), u=q(DIR + "/" + open_path), f=open_path.rpartition("/")[2])
                if too_big else "")
    if archived:
        notice = h('<p>Current files — not as they were in this chat</p><p><a href="/i/{n}/chat{query}">'
                   'Open in the current chat to edit or comment</a></p>', n=q(name),
                   query=f"?doc={q(open_path)}" if open_path else f"?dir={q(folder)}" if folder else "")
        view = (refusal if problem else
                h('<div class="docview" data-read-only="1"><div class="doctitle"><b>{p}</b>{d}</div>'
                  '<div class="docmd">{md}</div></div>', p=open_path, d=download, md=markdown(text)) if text is not None else
                h('<p class="dim">{p} is not there (any more).</p>', p=open_path) if open_path else Html(""))
        return join([notice, head, view])
    if problem:
        view = refusal
    elif open_path and text is not None:
        if saved:
            saved = {**saved, "name": name, "path": open_path}
        # Absent on both sides (the call the panel has always had) leaves this branch byte-identical.
        commenting = threads is not None or counts is not None
        tattr, cbtn, tlist = Html(""), Html(""), Html("")
        if commenting:
            import json
            payload = json.dumps([{"id": t["id"], "state": t["state"], "anchor": t["anchor"]}
                                  for t in (threads or [])], separators=(",", ":"))
            tattr = h(' data-threads="{j}"', j=payload)
            cbtn = Html('<button type="button" class="small" data-comment-doc '
                        'aria-label="Comment on the whole document">💬</button>')
            tlist = threads_list(name, open_path, sha, threads or [], thread)
        view = h('<div class="docview" data-doc="{p}" data-sha="{s}"{tattr}><div class="doctitle"><b>{p}</b>'
                 '<button type="button" class="small icon" data-edit-doc data-focus="doc-edit" title="Edit the document" '
                 'aria-label="Edit the document">{edit}</button>{cbtn}'
                 '<button type="button" class="copy small icon" data-copy-doc title="copy the document\'s Markdown" '
                 'aria-label="Copy the document">{copy}</button>'
                 '{download}<form class="docdelete" data-post="/docs/{n}/delete" data-after="doc-deleted" '
                 'data-confirm="Delete notes/docs/{p}?"{a}><input type="hidden" name="path" value="{p}">'
                 '<input type="hidden" name="sha" value="{s}"><button type="submit" class="small icon" '
                 'title="Delete the document" aria-label="Delete the document">{delete}</button></form>'
                 '</div><div class="bad wrap" data-delete-refusal></div>{tlist}{over}<div class="docmd">{md}</div>'
                 '<textarea class="raw" hidden readonly>{raw}</textarea>'
                 '<form class="docedit" data-post="/docs/{n}/save" data-after="doc-saved"{a} hidden>'
                 '<input type="hidden" name="path" value="{p}"><input type="hidden" name="sha" value="{s}">'
                 '<p class="dim" data-doc-draft role="status"></p>'
                 '<label>Markdown of {p}<textarea name="text" spellcheck="true" aria-label="Markdown of {p}">'
                 '{raw}</textarea></label><div class="actions">'
                 '<button type="submit">Save</button><button type="button" data-cancel-edit>Cancel</button>'
                 '<span class="dim">{n} hears of your change: with your next message, or now if it is replying</span>'
                 '</div><div class="bad wrap" data-refusal></div></form></div>',
                 p=open_path, s=sha, n=q(name), tattr=tattr, cbtn=cbtn, tlist=tlist, edit=ICONS["edit"], copy=ICONS["copy"],
                 delete=ICONS["delete"],
                 over=_overwritten(saved, sha, text), a=post_attrs("Documents", actions.RUN_NOW_S), download=download,
                 md=markdown(text) if text.strip() else Html('<p class="dim">(empty)</p>'), raw=text)
    elif open_path:
        view = h('<p class="dim">{p} is not there (any more).</p>', p=open_path)
    else:
        view = Html("")
    return join([head, view])


def attachment_panel(name: str, file_name: str, url: str, text: str | None, problem: str = "") -> Html:
    """A text file the instance sent you, read-only in the documents panel (?att=): it is the message's, not a document
    in notes/docs/, so nothing here edits, comments on or deletes it. `problem`: why it shows no text (Download still
    works whenever there is a file to download)."""
    expand = FULLSCREEN if text is not None else Html("")
    head = h('<div class="docshead"><button type="button" class="small" data-back-docs aria-label="Back to the documents">'
             '← Documents</button> <span class="dim">sent by {n}</span>{expand}<button type="button" class="small '
             'closedocs" data-close-docs aria-label="Close the documents">{x}</button></div>', n=name, expand=expand,
             x=_CLOSE)
    download = download_icon(url) if url else Html("")
    if text is None:
        return join([head, h('<p class="bad">{p}</p>', p=problem or f"{file_name} cannot be shown."),
                     h('<p>{d}</p>', d=download) if url else Html("")])
    body = text_view(file_name, text) if text.strip() else Html('<p class="dim">(empty)</p>')
    return join([head, h('<div class="docview" data-read-only="1"><div class="doctitle"><b>📎 {f}</b> {d}</div>'
                         '<div class="docmd">{b}</div></div>', f=file_name, d=download, b=body)])


def _comment_chips(items: list[dict] | None, kind: str = "") -> Html:
    """💬 chips on a turn: comments the operator sent, or threads the reply answered or opened."""
    if not items:
        return Html("")
    pre = f"{kind} " if kind else ""
    return join(h(' <a class="commentchip" href="?doc={u}&amp;thread={id}" data-open-doc="{doc}" '
                  'data-open-thread="{id}">💬 {pre}{id}</a>',
                  u=q(item["doc"]), doc=item["doc"], id=item["id"], pre=pre) for item in items)


def _reply_failure(f: dict | None) -> Html:
    """A failed reply's note (web_chat.failure): why, in words, what to do, Retry when there is a message to send again
    (one tap with retry_of, else after its confirm), and the raw error under Technical details."""
    if not f:
        return Html("")
    if f["kind"] == "stopped":
        return h('<p class="warn recovery">{why}</p>', why=f["why"])
    links = join(join([Html(" · ") if i else Html(""), h('<a href="{u}">{l}</a>', u=x["url"], l=x["label"])])
                 for i, x in enumerate(f["links"]))
    retry = f["retry"]
    if retry and retry["confirm"] is None:
        recovery = h('<button type="button" class="small" data-retry="{text}" data-retry-of="{k}">Retry</button>',
                     text=retry["text"], k=retry["retry_of"])
    elif retry:
        recovery = join([h('<button type="button" class="small" data-retry="{text}" data-retry-confirm="{c}">Retry'
                           '</button> ', text=retry["text"], c=retry["confirm"]), links])
    else:
        recovery = links
    return h('<p class="warn recovery">{why} {action}</p><div class="actions">{recovery}</div>'
             '<details class="tech"><summary>Technical details</summary><pre>{raw}</pre></details>',
             why=f["why"], action=f["action"], recovery=recovery, raw=f["detail"])


def _turn_time(ts: str) -> Html:
    """Stored timestamps use the supervisor's local time, as do the other UI dates."""
    return h('<time datetime="{iso}" title="{full}">{short}</time>',
             iso=ts, full=ts.replace("T", " "), short=ts[11:16]) if ts else Html("")


def _later_card(m: dict) -> Html:
    """What the operator left for the instance's next cycle (web_chat.background): their own message, in the chat
    though not a turn of it — waiting in its inbox, or read once a cycle has taken it up."""
    from . import web_chat
    return _later_item(web_chat.message_item(m.get("to", ""), m, {}))


def _later_item(it: dict) -> Html:
    atts = join(h('<a href="{u}" target="_blank">📎 {n}</a>', u=file_url(a["path"]), n=a["name"])
                for a in it["attachments"])
    state = "read" if it["read"] else "waiting"
    return h('<div class="turn me later" data-k="{key}" data-raw="{raw}"{mid}><div class="who">you <span class="dim">{ts}</span> '
             '<a href="/messages/{f}" class="laterchip {state}" title="Not answered now: it reads this in its next '
             'cycle">⏾ next cycle · {state}</a>{chips}<button type="button" class="copy small icon" data-copy '
             'title="copy the message as Markdown" aria-label="Copy the message">{copy}</button></div>{text}'
             '<div class="atts">{a}</div></div>', key=it["key"], f=q(it["file"]), raw=it["text"], ts=_turn_time(it["ts"]),
             state=state, mid=_mid_attr(it["mid"]),
             chips=_comment_chips(it["comments"]), copy=ICONS["copy"],
             text=_my_text(it["text"]) if it["text"] else Html(""), a=atts)


def _feedback_buttons(name: str, target: dict, ratings: dict) -> Html:
    from . import feedback
    return _thumbs(name, target, ratings.get(feedback.key(target)))


def _thumbs(name: str, target: dict, item: dict | None) -> Html:
    """The thumbs on a reply or a message, lit as its rating (a feedback item, or web_chat.build's `rating`) has it."""
    from . import feedback
    item = item or {}
    fields = ({"kind": "chat", "cid": target["chat"], "k": target["k"]} if target["kind"] == "chat"
              else {"kind": "message", "file": target["file"]})
    return h('<span class="feedback" data-feedback-name="{n}" data-feedback-target="{target}" '
             'data-feedback-id="{id}" data-feedback-reason="{reason}" data-feedback-thumb="{thumb}">{buttons}{send}</span>',
             n=name, target=json.dumps(fields), id=item.get("id", ""), reason=item.get("reason", ""),
             thumb=item.get("thumb", ""),
             send=(h('<span class="dim small">sent {time}</span>', time=_turn_time(item["sent"]["ts"]))
                   if item.get("sent") else h('<button type="button" class="small" data-feedback-send>Send…</button>')
                   if item else Html("")),
             buttons=join(h('<button type="button" class="small icon" data-thumb="{thumb}" aria-pressed="{pressed}" '
                            'aria-label="{label}" title="{title}" data-focus="feedback-{key}-{thumb}">{icon}</button>',
                            thumb=thumb, pressed="true" if item.get("thumb") == thumb else "false", label=label,
                            title=item.get("reason") or label, key=feedback.key(target), icon=icon("thumb-" + thumb, 16))
                          for thumb, label in (("up", "Thumbs up"), ("down", "Thumbs down"))))


def _sent_cards(name: str, sent: list[dict], archived: bool = False) -> list[Html]:
    """What the instance sent the operator (send_message), one card each; Reply quotes it in the composer, and a quoted
    question is answered by the message that quotes it (web_chat.answer_quoted), as a Telegram reply answers it.
    data-sent-at: when it came, so opening the chat can start at the first one this browser has not seen (web.js)."""
    from . import feedback, web_chat
    ratings = feedback.by_target(name)
    return [_message_item(name, web_chat.message_item(name, m, ratings), archived) for m in sent]


def _message_item(name: str, it: dict, archived: bool) -> Html:
    """A logged message in the chat (web_chat.message_item): one the instance sent you, or one you left for its next
    cycle."""
    if it["type"] == "later":                   # left for its next cycle (web_chat.sent_during)
        return _later_item(it)
    # Files open in a sheet of their own: in an app, a link replaced its only view. A text file previews in the
    # documents panel (/fragment/docs/<name>?att=<file>/<j>); the link still downloads.
    atts = join(h('<a href="{u}" target="_blank"{preview}>{img}</a>', u=file_url(a["path"]),
                  preview=h(' data-open-att="{f}/{j}"', f=it["file"], j=j) if a["text"] else "",
                  img=h('<img class="thumb" src="{u}" alt="{n}" width="192" height="144" loading="lazy">',
                        u=file_url(a["path"]), n=a["name"])
                  if a["image"] else h("📎 {n}", n=a["name"]))
                for j, a in enumerate(it["attachments"]))
    asks = it["kind"] == "question"
    what = ("a question" if asks else "a message") + (" (answered)" if asks and it["read"] else "")
    return h('<div class="turn sent" data-k="{key}" data-raw="{raw}"{at}><div class="who">{n} sent you '
             '<a href="/messages/{f}">{what}</a> <span class="dim">{ts}</span><button type="button" '
             'class="reply small icon" data-reply-message="{identity}" title="Reply to the message" '
             'aria-label="Reply to the message">{reply}</button>{feedback}</div>{t}'
             '<div class="atts">{a}</div></div>', key=it["key"], n=name, f=q(it["file"]), raw=it["text"], what=what,
             ts=_turn_time(it["ts"]), identity=it["file"], reply=ICONS["reply"], t=markdown(it["text"]), a=atts,
             feedback=_thumbs(name, {"kind": "message", "file": it["file"]}, it["rating"]),
             at=h(' data-seen-inst="{n}" data-sent-at="{ts}"', n=name, ts=it["ts"]) if not archived else "")


def _mid_attr(mid: str | None) -> Html:
    return h(' data-mid="{m}"', m=mid) if mid else Html("")


def live_activity(chat: dict | None) -> Html:
    """What the reply running now has done so far (/fragment/chat/<name>/live, inside the feed's live turn: the model's
    `reply`, web_chat.reply_of). Once it ended, what the finished reply did, marked data-ended: the page asks the feed
    for the answer at once, and the activity does not blank meanwhile (it did, for up to 1.5 s: Grok review of 8735c62)."""
    from . import web_chat
    if not chat:
        return Html("")
    reply = web_chat.reply_of(chat)
    if reply is not None:
        # The answer as it streams in (the server saves it as it comes: web_chat._run). It showed only at the end, behind
        # "the answer appears when it is ready" (reviews C11, I9). Not a live region: VoiceOver would read the whole
        # reply again at every refresh; the finished turn is announced once (web.js morph).
        steps = _activity(reply["activity"], "live", item="act", now=True)
        if not reply["answer"].strip():
            return join([steps, Html('<p class="typing" data-k="wait" aria-label="working"><span></span><span></span>'
                                     '<span></span></p>')])
        return join([steps, h('<div class="streaming" data-k="ans">{a}</div>', a=markdown(reply["answer"]))])   # its .md
    last = next((t for t in reversed(chat.get("turns") or []) if t["role"] == "assistant"), None)
    if not last:
        return Html("")
    return _activity(last.get("activity") or [], "live", open_=True, item="act", ended=True) \
        or Html('<span data-k="end" data-ended hidden></span>')


def chat_feed(name: str, chat: dict | None, sent: list[dict], archived: bool = False, whole: bool = False) -> Html:
    """The conversation as it stands (web_chat.build): each turn, the files it sent you meanwhile, and the reply running
    now. A long one shows its last CHAT_WINDOW turns unless `whole` (?all=1): every poll rendered, sent and morphed the
    whole history, and a phone opened a long chat slowly (review C13/M21). Turns keep their own index (data-k), so
    nothing shifts."""
    from . import web_chat
    return feed_html(web_chat.build(name, chat, sent, archived, whole))


def _user_item(name: str, it: dict) -> Html:
    tag = h(' <span class="dim">· during its reply</span>') if it["interjected"] else Html("")
    if it.get("via") == "voice":                 # transcribed speech: it may hold recognition errors
        tag = join([tag, h(' <span class="dim" title="spoken, transcribed on the device">· spoken</span>')])
    if it["edits"]:
        tag = join([tag, join(h(' <a class="editchip" href="?doc={u}" data-open-doc="{p}">✎ edited {p}</a>',
                                u=q(p), p=p) for p in it["edits"])])
    if it["deleted"]:                            # nothing to open: it is gone
        tag = join([tag, join(h(' <span class="editchip">🗑 deleted {p}</span>', p=p) for p in it["deleted"])])
    if it["comments"]:
        tag = join([tag, _comment_chips(it["comments"])])
    if it["feedback"]:
        tag = join([tag, h(' <span class="editchip">👎 feedback</span>')])
    # A message's actions sit under it, out of sight until wanted: hover or keyboard focus on a computer, long-press
    # or right-click opens them as a menu (web.js msgMenu). Two bordered boxes on every message were bigger than a
    # short one (iPhone review I12). Copy and Reply need text: a comment-only turn's text is empty, the chips are the
    # turn, not an empty bubble.
    shown = it["text"]
    controls = h('<div class="msgactions">{own}{feedback}</div>', own=_own_buttons(), feedback=Html("")) \
        if shown else Html("")
    # data-mid: the page's id for the message, so the "sending" copy it shows goes once this one is here (W7)
    return h('<div class="turn me" data-k="{key}" data-raw="{raw}"{mid}><div class="who">you '
             '<span class="dim">{ts}</span>{tag}</div>'
             '{text}{atts}{controls}</div>', key=it["key"], ts=_turn_time(it["ts"]), tag=tag, raw=shown,
             controls=controls, mid=_mid_attr(it["mid"]), text=_my_text(shown) if shown else Html(""),
             atts=_chat_attachments(it["attachments"]))


def _own_buttons() -> Html:
    return h('<button type="button" class="copy small icon" data-copy '
             'title="Copy the message as Markdown" aria-label="Copy the message">{copy}</button>'
             '<button type="button" class="reply small icon" data-reply-message title="Reply to the message" '
             'aria-label="Reply to the message">{reply}</button>', copy=ICONS["copy"], reply=ICONS["reply"])


def _answer_item(name: str, cid: str, it: dict, archived: bool) -> Html:
    k = it["k"]
    # The last answer keeps its Copy in view (.turn[data-last]). A reply can be rated even when it failed with none
    # (its thumbs alone).
    controls = h('<div class="msgactions">{own}{feedback}</div>', own=_own_buttons() if it["content"] else Html(""),
                 feedback=_thumbs(name, {"kind": "chat", "chat": cid, "k": k}, it["rating"]))
    # Keep partial answers readable; put the failure and recovery below them.
    mark = h(' <span class="warn incomplete">incomplete</span>') if it["incomplete"] else Html("")
    body = markdown(it["content"]) if it["content"] else Html("")
    if not it["content"] and not it["failure"]:
        body = Html('<p class="dim">(no answer)</p>')
    cards = join([doc_cards(it["docs"], name if archived else ""), _comment_chips(it["answered"], "answered"),
                  _comment_chips(it["opened"], "opened")])
    seen = h(' data-seen-inst="{n}" data-seen-reply="{ts}"', n=name, ts=it["ts"]) if not archived else Html("")
    return h('<div class="turn it" data-k="{key}" data-raw="{raw}"{seen}{last}><div class="who">{n} '
             '<span class="dim">{ts}</span>{mark}</div>{act}{body}{cards}{recovery}{copy}</div>',
             key=it["key"], raw=it["content"], n=name, ts=_turn_time(it["ts"]), seen=seen,
             last=Html(" data-last") if it["last"] else "",
             mark=mark, copy=controls, act=_activity(it["activity"], f"t{k}"),
             body=body, cards=cards, recovery=_reply_failure(it["failure"]))


def feed_html(model: dict) -> Html:
    """The HTML of a chat's model (web_chat.build): every data-k is an item's key, so the page and the apps show the
    same items (docs/app-api.md §14)."""
    from .web_actions import button, post_attrs
    name, chat, archived = model["instance"], model["chat"], model["archived"]
    if not chat:
        return join([*(_message_item(name, it, archived) for it in model["items"]),
                     h('<div class="emptychat" data-k="empty"><p class="big">Chat with {n}</p><p class="dim">No chat open '
                       'with {n}. Write below to start one; earlier chats are in ⋯.</p></div>', n=name)])
    feed = []
    if model["earlier"]:
        more = f"?id={q(chat['id'])}&all=1" if archived else "?all=1"
        feed.append(h('<p class="earliermsgs" data-k="more"><a class="button" href="{u}">Show the {n} earlier '
                      'messages</a></p>', u=more, n=model["earlier"]))
    today = time.strftime("%Y-%m-%d")
    previous_day = ""

    def day_separator(ts: str) -> None:
        nonlocal previous_day
        day = ts[:10]
        if day and day != previous_day:
            if previous_day or day != today:
                feed.append(h('<p class="dim chat-day" data-k="day-{day}">{label}</p>',
                              day=day, label="Today" if day == today else day))
            previous_day = day

    for it in model["items"]:
        day_separator(it["ts"])
        kind = it["type"]
        if kind == "user":
            feed.append(_user_item(name, it))
        elif kind == "assistant":
            feed.append(_answer_item(name, chat["id"], it, archived))
        elif kind == "retried":
            feed.append(h('<p class="dim retried" data-k="{key}">The first try failed and was sent again '
                          '<span class="dim">({ts})</span>.</p>', key=it["key"], ts=_turn_time(it["ts"])))
        elif kind == "ended":                    # a past chat: when it ended, nothing about a next message
            when = it["ts"][:16].replace("T", " ")
            feed.append(h('<p class="dim ended" data-k="ended">This conversation ended on {e}.</p>', e=when)
                        if archived else
                        h('<p class="dim ended" data-k="ended">Ended {e}: journal write-up requested. Your next '
                          'message starts a new conversation.</p>', e=when))
        else:
            feed.append(_message_item(name, it, archived))
    if (r := model["reply"]) is not None:
        day_separator(r["started"])
        # Its activity is polled on its own (/fragment/chat/<name>/live, live_activity): this shell stays the same
        # for the whole reply, so the feed's refresh is a 304 while it runs (W12, review 2026-10-24).
        live = (h('<div class="liveact" data-poll="/fragment/chat/{n}/live?id={cid}" data-every="1500" data-keyed="1" '
                  'data-now="1"></div>', n=q(name), cid=chat["id"])
                if not archived else _activity(r["activity"], "live", open_=True))
        feed.append(h('<div class="turn it live" data-k="live"><div class="who">{n} <span class="dim">replying since {s}'
                      '<span class="elapsed" data-since="{iso}"></span></span></div>'
                      '{act}<div class="actions">{stop}</div></div>', n=name, s=_turn_time(r["started"]),
                      iso=r["started"], act=live,
                      stop=button("chat-stop", "Stop the reply",
                                  "stop this reply? Only the reply stops. The conversation stays open.",
                                  {"cid": chat["id"]}, cls="small")))
    for t in model["queued"]:
        cancel = Html("") if archived else h(
            '<form class="act" data-post="/chat/{n}/unqueue"{a}><input type="hidden" name="mid" value="{mid}">'
            '<input type="hidden" name="cid" value="{cid}">'
            '<button type="submit" class="act small">Cancel</button></form>', n=q(name), mid=t["mid"], cid=chat["id"],
            a=post_attrs("the chat (is it still queued?)"))
        feed.append(h('<div class="turn me" data-k="{key}"{dm}><div class="who">you '
                      '<span class="dim">queued — sent when this reply ends</span></div>{text}{atts}{cancel}</div>',
                      key=t["key"], dm=_mid_attr(t["mid"]), text=_my_text(t["text"]), cancel=cancel,
                      atts=join(h('<p class="dim">📎 {n}</p>', n=a["name"]) for a in t["attachments"])))
    for p in model["pending_feedback"]:
        feed.append(h('<div class="turn me" data-k="{key}">'
                      '<span class="dim">👎 on its reply of {k} goes with your next message</span>'
                      '<form class="act" data-post="/feedback/{n}"{a}>'
                      '<input type="hidden" name="op" value="unpend">'
                      '<input type="hidden" name="id" value="{id}">'
                      '<button type="submit" class="small" aria-label="Keep feedback without sending">×</button>'
                      '</form></div>', key=p["key"], id=p["id"], k=_turn_time(p["reply_ts"]), n=q(name),
                      a=post_attrs("the feedback (is it still pending?)")))
    if model["pending_edits"]:
        feed.append(h('<p class="pending-edits" data-k="pend">✎ Your changes to {p} go to {n} with your next message.</p>',
                      p=", ".join(model["pending_edits"]), n=name))
    if model["can_end"]:
        feed.append(h('<div class="actions" data-k="acts">{b}</div>', b=button(
            "chat-end", "End the conversation", f"end this conversation with {name}? The messages stay on this page. "
            "Your next message starts a new conversation. A journal write-up is requested.",
            {"cid": chat["id"]}, cls="small")))
    return join(feed)


def chat_page(name: str, chat: dict | None, sent: list[dict], past: list[dict], live: dict, names: list[str],
              csrf: str, read_only: bool = False, panel: Html = Html(""), doc: str = "", att: str = "",
              later: str = "", folder: str = "", sort: str = "", listing_only: bool = False, whole: bool = False,
              selected: bool = False) -> Html:
    """`att` (<message file>/<index>): the panel shows that text attachment, not a document (attachment_panel).
    `folder` (?dir=): with no document open, the panel lists that folder of notes/docs/.
    `later`: when its next cycle is (autonomy.next_cycle), the Next cycle button's title."""
    from . import messages, web_chat
    # An explicitly selected thread stays selected when another thread becomes current.
    thread_query = f"?id={q(chat['id'])}" if (selected or read_only) and chat else ""
    feed_url = f"/fragment/chat/{q(name)}" + thread_query
    if whole:                                          # all of it asked for: its refreshes too
        feed_url += ("&" if "?" in feed_url else "?") + "all=1"
    composer = Html("") if read_only else h(
        '<form class="composer" data-post="/chat/{n}/send" data-multipart="1" data-reset="1" data-files data-draft="{n}" data-sent-mids="{mids}" '
        'data-max-file="{mf}" data-max-total="{mt}" data-max-files="{mn}"><input type="hidden" name="mid" value="">'
        '<input type="hidden" name="queue" value=""><input type="hidden" name="background" value="">'
        '<input type="hidden" name="retry_of" value="">{cid}'
        '<div class="chips" data-picked></div><div class="cbox">'
        '<label class="pick iconbtn" title="Attach files">{attach}<span class="sr">Attach files</span>'
        '<input type="file" name="files" multiple class="sr-file"></label>'
        # prose: the keyboard capitalises and corrects it (review C15)
        '<textarea name="text" rows="1" maxlength="20000" placeholder="Message {n}" aria-label="Message {n}" '
        'autocapitalize="sentences" autocorrect="on" spellcheck="true"></textarea>'
        # One send arrow; the other two ways to send behind it (the operator's call, 2026-10-27: "One arrow + menu").
        '<div class="sendgroup"><button type="submit" class="send iconbtn" data-queue="" title="Send now" '
        'aria-label="Send now">{send}</button>'
        '<details class="menu sendmenu" data-key="send-menu"><summary class="iconbtn" title="More ways to send" '
        'aria-label="More ways to send">{up}</summary><div class="menubody">'
        # Queue after reply only while a reply runs (web.js syncQueue keeps it in step with the feed's live turn).
        '<button type="submit" data-queue="1" title="Wait for the current reply to finish; sends now if idle"{qh}>'
        'Queue after reply<span class="dim">sent when the current reply ends</span></button>'
        # Next cycle: not answered now — to its inbox, read in its next cycle
        '<button type="submit" data-background="1" title="Not answered now: to {n}&#x27;s inbox, {later}">'
        'Next cycle<span class="dim">not answered now — {later}</span></button></div></details></div></div>'
        '<span class="dim keys">Enter sends · Shift-Enter new line · ⌘-Enter queues · / to write · Alt-↑↓ another chat'
        '</span></form>',
        # Only a page opened on one thread (?id=) names it: the plain chat page follows the current thread, and after
        # End its next message starts a new one, as before threads.
        cid=h('<input type="hidden" name="cid" value="{c}">', c=chat["id"])
            if selected and chat and chat.get("state") != "ended" else Html(""),
        attach=icon("attach"), send=icon("send"), up=icon("up"),
        qh="" if (chat or {}).get("state") == "replying" else Html(" hidden"), later=later or "for its next cycle",
        n=name, mf=messages.ATTACH_MAX_BYTES, mt=messages.ATTACH_MAX_TOTAL, mn=messages.ATTACH_MAX_FILES,
        mids=json.dumps(web_chat.sent_mids(chat, sent)))
    banner = (h('<p class="panel">Earlier chat, {date}. <a href="/i/{n}/chat">Back to the current chat</a></p>',
                date=chat['created'][:16].replace('T', ' '), n=q(name)) if read_only and chat else Html(""))
    feed = chat_feed(name, chat, sent, archived=read_only, whole=whole)
    body = join([banner, h('<section class="panel chat" data-scroll><div class="feed" data-poll="{u}" data-every="1500" data-keyed="1" '
                         'data-follow="1" data-etag="{t}" data-etag-url="{u}">{f}</div></section>'
                         '<button type="button" class="newbelow" data-newbelow hidden>↓ New below</button>',
                         u=feed_url, t=etag(str(feed).encode()), f=feed), composer,
                 Html('<div class="dropzone" data-dropzone hidden>Drop to attach</div>') if not read_only else Html("")])
    docs_url = (f"/fragment/docs/{q(name)}?" + (f"att={q(att)}" if att else f"list=1&dir={q(folder)}"
                                                if (folder or listing_only) and not doc else f"doc={q(doc)}")
                + (f"&id={q(chat['id'])}" if read_only and chat else "") + (f"&sort={sort}" if sort else ""))
    aside = h('<aside class="docs" id="docs" data-poll="{u}" data-every="1500" data-lazy="1" data-etag="{t}" '
              'data-etag-url="{u}">{p}</aside>', u=docs_url, t=etag(str(panel).encode()), p=panel)
    return chat_frame(name, chat_header(name, live, chat, past, read_only),
                      h('<div class="chatgrid"><div class="chatpage">{b}</div>{a}</div>', b=body, a=aside),
                      names, csrf, f"/fragment/chat/{q(name)}/header"
                      + thread_query)


def calendar_proposals_body(rows: list[dict]) -> Html:
    """Approval lives behind the same tailnet/CSRF action door as other operator actions (plan M2/D3)."""
    from .web_actions import button
    def render(row):
        buttons = join(button('calendar-' + verb, label, fields={'id': row['id']})
                       for verb, label in [('approve', 'Approve'), ('decline', 'Decline')] +
                       ([('overreach', 'Reject as over-reach')] if row.get('action') else [])) if row['state'] == 'pending' else h(
                           '<p>{state}: {outcome}</p>', state=row['state'], outcome=row['outcome'])
        return h('<article><h3>{instance}</h3><p>Class: {action_class} · Proposal: {pid} · Action: {aid}</p><pre class="wrap">{summary}</pre>'
                 '<p>Who would be told: {people}</p><p>Expires: {expires}</p>{buttons}</article>',
                 instance=row['instance'], action_class=row.get('action_class', 'staged'), pid=row['id'], aid=row.get('action', ''),
                 summary=row['summary'], people=', '.join(row['attendees']) or 'none recorded',
                 expires=row['expires_at'], buttons=buttons)
    pending = [r for r in rows if r['state'] == 'pending']
    recent = [r for r in rows if r['state'] != 'pending'][:30]
    body = join([Html('<h2>Pending action approvals</h2>'), join(render(r) for r in pending) if pending else
                 Html('<p>No pending proposals.</p>'), Html('<h2>Recent decisions</h2>'), join(render(r) for r in recent)])
    return body


def calendar_proposals_page(rows: list[dict], names: list[str], csrf: str) -> Html:
    body = h('<section data-poll="/fragment/calendar-proposals">{body}</section>', body=calendar_proposals_body(rows))
    return frame('Action approvals', body, names, csrf, current='/calendar/proposals')


# -- bake-offs judged by the operator (supervisor/bakeoff.py § the operator as judge) --------------------------------
def bakeoffs_page(items: list[dict], names: list[str], csrf: str, rankings: dict | None = None) -> Html:
    body = [r for r in items]
    ranks = join(join([bakeoff_ranking(r, inst, here=False),
                       h('<p><a class="button" href="/bakeoffs/{n}/judge-all">Judge pairs across all your bake-offs of '
                         '{i}</a></p>', n=q(inst), i=inst)]) for inst, r in (rankings or {}).items() if r)
    t = table(["Bake-off", "Instance", "Saved"],
              [[link(f"/bakeoffs/{q(i['name'])}/{q(i['id'])}", i["id"]), i["name"], i["modified"].strftime("%m-%d %H:%M")]
               for i in body]) if body else h("<p>No bake-off saved yet (<code>shaft trial models</code>).</p>")
    return frame("Bake-offs", join([h("<h1>Bake-offs</h1><p>Model bake-offs replay past cycles once per model. "
                                      "Open one to judge its pairs yourself, blind.</p>"), ranks, t]), names, csrf,
                 current="/bakeoffs")


def _short(label: str) -> str:
    return label.rsplit("/", 1)[-1]


def _elo_cell(e: dict) -> str:
    return f"{e['elo']} [{e['lo']}, {e['hi']}]" if e["n"] else f"{e['elo']} (no pair yet)"


def _cost_cell(c: dict) -> str:
    if c.get("usd") is None:
        return "plan" if c.get("plan") else "—"
    return f"plan (≈ ${c['usd']:.3f} at list price)" if c.get("plan") else f"${c['usd']:.3f}"


def bakeoff_ranking(rank: list[dict], instance: str = "", here: bool = True) -> Html:
    body = [[str(k), _short(r["arm"]) + (" · baseline" if r.get("baseline") else "")
             + ("" if r.get("in_this", True) else " · not in this bake-off"),
             _elo_cell(r["you"]), f"{r['you']['n']}" + (f" ({r.get('here', 0)} here)" if here else ""),
             _elo_cell(r["llm"]), str(r["llm"]["n"]), _cost_cell(r.get("cost") or {})] for k, r in enumerate(rank, 1)]
    return h('<section class="panel"><h2>Ranking{of}</h2>{t}<p class="muted">One ranking for the instance: Elo from '
             'all your verdicts in all its bake-offs, any pair; every model starts at 1000 and moves with each pair '
             'it is in (a win counts 1, equal and both bad ½, a loss 0; a Bradley-Terry fit, with a weak pull toward '
             '1000 so a few pairs cannot run away). In brackets the 90% range: models whose ranges overlap are not yet '
             'told apart. Beside it, the LLM judges\' Elo on the same scale from the same bake-offs (each model '
             'against that bake-off\'s baseline only). $/cycle is the average cost of one replayed cycle in the same bake-offs: '
             'billed dollars, or for a model on the Codex plan (which bills nothing per call) what its tokens would cost '
             'at OpenRouter\'s list price, cached input at the cached rate. It changes with each verdict you save. Bake-offs replay '
             'different cycles and code: pooling them assumes a model is as good in one as in another.</p></section>',
             of=f" of {instance}" if instance else "",
             t=table(["#", "Model", "Your Elo [90%]", "Your pairs", "LLM judges' Elo [90%]", "Their pairs", "$/cycle"],
                     body))


def bakeoff_page(b: dict, summary: list[dict], names: list[str], csrf: str, rank: list[dict] | None = None) -> Html:
    base = b["arms"][0]["label"]
    rows_ = []
    for r in summary:
        agree = f"{r['agree']} of {r['compared']}" if r["compared"] else "—"
        rows_.append([_short(r["arm"]) + (" · baseline" if r["arm"] == base else ""), str(r["judged"]),
                      f"{r['W']}–{r['L']}–{r['T']}" + (f", {r['bad']} both bad" if r["bad"] else ""), agree])
    pick = join(h('<label><input type="checkbox" name="vs" value="{i}" checked> {l}</label><br>', i=str(k),
                  l=_short(a["label"])) for k, a in enumerate(b["arms"]))
    body = h('<p><a href="/bakeoffs">← every bake-off</a></p><h1>{id} · {n}</h1>'
             '<p>Baseline <b>{base}</b>, {s} past cycles, code {rev}. Your verdicts are kept apart from the LLM judges\' '
             'and only set beside them here.</p>'
             '{ranking}<section class="panel"><h2>Your verdicts</h2>{t}'
             '<p class="muted">W–L–T over every pair you judged the model in. "Agrees" counts its pairs with the '
             'baseline that the LLM judges also settled (win, loss or tie) where they reached your outcome: they '
             'judged every model against the baseline only.</p></section>'
             '<section class="panel"><h2>Judge pairs</h2><form method="get" action="/bakeoffs/{qn}/{qid}/judge">'
             '<p>Each page is a random pair of the models ticked, on one cycle, each side drawn at random: you do '
             'not know which model ran which side.</p>{pick}<p><button type="submit">Start</button></p></form>'
             '<p><a href="/bakeoffs/{qn}/judge-all">Or judge pairs across all your bake-offs of {n}</a></p></section>',
             id=b["id"], n=b["name"], base=_short(base), s=str(len(b["states"])), rev=b.get("code_rev", "")[:10],
             t=table(["Model", "Your pairs", "Its W–L–T", "Agrees with the LLM judges (vs baseline)"], rows_),
             qn=q(b["name"]), qid=q(b["id"]), pick=pick, ranking=bakeoff_ranking(rank, b["name"]) if rank else "")
    return frame(f"Bake-off {b['id']}", body, names, csrf, current="/bakeoffs")


def _run_panel(side: str, run: dict) -> Html:
    sent = join(h('<div class="panel"><p><b>To {to}</b>{kind}{held}</p><p class="wrap">{t}</p>{att}</div>',
                  to=m["to"] or "?", kind=f" · {m['kind']}" if m["kind"] else "",
                  held=Html(" · <span class=\"bad\">held back by the supervisor</span>") if m["held"] else "",
                  t=m["text"], att=h('<p class="muted">attached: {a}</p>', a=", ".join(m["attachments"]))
                  if m["attachments"] else "") for m in run["sent"]) or h('<p class="muted">No message.</p>')
    files = join(h('<details><summary>{p}{note}{cut}</summary><pre class="wrap">{a}</pre></details>', p=f["path"],
                   note=f" ({f['note']})" if f["note"] else "", cut=" (cut)" if f["cut"] else "",
                   a=f["added"] or f["note"] or "—")
                 for f in run["files"]) or h('<p class="muted">No file delivered.</p>')
    internal = h('<p class="muted">Also changed its own records: {x}</p>', x=", ".join(run["internal"])) \
        if run["internal"] else ""
    return h('<section class="panel"><h2>Run {s}</h2><p class="wrap"><b>It set out to:</b> {g}</p>'
             '<h3>Messages to you</h3>{sent}<h3>Its account at the end</h3><p class="wrap">{acc}</p>'
             '<h3>Files it delivered</h3>{files}{internal}</section>', s=side, g=run["goal"] or "—", sent=sent,
             acc=run["account"] or "—", files=files, internal=internal)


def _asked_html(turns: list[dict]) -> Html:
    if not turns:
        return Html("")
    return h('<section class="panel"><h2>What you asked before this cycle</h2><p class="muted">Your last chat with it '
             'before the cycle began: the cycle set out to do what you asked.</p>{t}</section>',
             t=join(h('<p class="wrap"><b>{who}</b> <span class="muted">{ts}</span><br>{text}</p>', who=t["role"],
                      ts=t["ts"].replace("T", " "), text=t["text"]) for t in turns))


def _pending_html(p: dict) -> Html:
    late = h('<span class="bad">past due</span> ')
    queue = join(h('<li>{l}#{id} · {st} · due {due}: {t}</li>', l=late if x["late"] else "", id=str(x["id"]),
                   st=x["status"], due=x["due"].replace("T", " ") or "—", t=x["task"]) for x in p.get("queue", []))
    prom = join(h('<li>{l}due {due} · to {to}: {t}</li>', l=late if c["late"] else "", due=c["due"].replace("T", " ")
                  or "—", to=c["to"] or "?", t=c["promise"]) for c in p.get("commitments", []))
    more = f" (+{p['more']} more)" if p.get("more") else ""
    return h('<h3>Waiting at that moment</h3><p class="muted">Its task queue: open and waiting items{more}, and '
             '{bl} more in the backlog.</p><ul>{q}</ul><p class="muted">Promises made and not yet kept:</p><ul>{c}</ul>',
             more=more, bl=str(p.get("backlog", 0)), q=queue or h("<li>none</li>"), c=prom or h("<li>none</li>"))


def bakeoff_judge_page(b: dict, case: dict | None, left: int, vs: list[int], names: list[str], csrf: str,
                       back: str = "") -> Html:
    back = h('<p><a href="{href}">← {t}</a></p>', href=back, t="the bake-offs and the ranking") if back else \
        h('<p><a href="/bakeoffs/{n}/{id}">← the bake-off and its ranking</a></p>', n=q(b["name"]), id=q(b["id"]))
    if case is None:
        return frame("Judge", join([back, h("<h1>Nothing left to judge</h1><p>Every pair of these models has your "
                                            "verdict.</p>")]), names, csrf, current="/bakeoffs")
    labels = [a["label"] for a in b["arms"]]
    x, y = labels.index(case["pair"][0]), labels.index(case["pair"][1])
    goals, _, inbox = case["context"].partition("## Its inbox at that moment")
    choices = join(h('<label><input type="radio" name="choice" value="{c}" required> {l}</label> ', c=c, l=l)
                   for c, l in (("A", "A is better"), ("B", "B is better"), ("tie", "Equal"), ("both bad", "Both bad")))
    body = h('{back}<h1>Which run would you rather have had?</h1>'
             '<p class="muted">{left} left. This {kind}cycle ran on {t0}; both runs replayed it on {rep}, from the same '
             'start. A date later than the cycle\'s in a run comes from the replay\'s clock, not from the run.</p>'
             '{asked}<section class="panel"><h2>What the cycle started from</h2><h3>Its inbox</h3><pre class="wrap">{inbox}</pre>'
             '{pending}'
             '<details><summary>Its goals file (the same in most cycles)</summary><pre class="wrap">{goals}</pre>'
             '</details></section>'
             '<div class="cards">{a}{b}</div>'
             '<section class="panel"><form class="act" data-post="/bakeoffs/{n}/{id}/judge" data-after="reload">'
             '<input type="hidden" name="state" value="{st}"><input type="hidden" name="x" value="{x}">'
             '<input type="hidden" name="y" value="{y}">'
             '<input type="hidden" name="runs" value="{runs}">'
             '<p>{choices}</p><p><textarea name="note" rows="2" placeholder="Why (optional)"></textarea></p>'
             '<p><button type="submit">Save and next</button></p></form></section>',
             back=back, left=str(left), kind=f"{case['kind']} " if case["kind"] else "", t0=case["t0"].replace("T", " "),
             rep=case["replayed"], goals=goals.strip() or "(no goals file)", inbox=inbox.strip() or "(empty)",
             pending=_pending_html(case.get("pending") or {}), asked=_asked_html(case.get("asked") or []),
             a=_run_panel("A", case["A"]), b=_run_panel("B", case["B"]),
             n=q(b["name"]), id=q(b["id"]), st=str(case["state"]), x=str(x), y=str(y), runs=case["runs"], choices=choices)
    return frame("Judge a pair", body, names, csrf, current="/bakeoffs")

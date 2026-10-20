"""The web UI's read-only pages (docs/plans/PLAN-webui.md M1): the TUI's tabs 2–6, 8, 9, 0 and the messages browser, plus an
instance's journal and activity. Each takes the data the TUI's tab takes — the same `panel` readers, called by web.py
— and shows the same fields, so the two UIs cannot disagree. Escaping, control characters and the page frame are
web_pages'; nothing here writes anything.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse

from .web_pages import Html, esc, h, join, nav, page

MARK = {"yes": ("✓", "good"), "partial": ("½", "warn"), "no": ("✗", "bad")}
ICONS = {name: h('<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{p}</svg>', p=Html(path))
         for name, path in {
             "copy": '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H4v13h4"/>',
             "reply": '<path d="m9 4-6 6 6 6M3 10h10a8 8 0 0 1 8 8v2"/>',
             "delete": '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
         }.items()}
INSTANCE_TABS = [("chat", "Chat"), ("", "Summary"), ("config", "Config"), ("drive", "Drive"), ("access", "Access"),
                 ("judge", "Judge"), ("changes", "Changes"), ("benchmarks", "Benchmarks"),
                 ("transcripts", "Transcripts"), ("journal", "Journal"), ("activity", "Activity")]


def q(value: str) -> str:
    """A value for a URL path segment or query parameter."""
    return urllib.parse.quote(str(value), safe="")


def frame(title: str, body: Html, names: list[str], csrf: str, current: str = "") -> Html:
    return page(title, body, nav(names, current), csrf)


def instance_frame(name: str, tab: str, title: str, body: Html, names: list[str], csrf: str) -> Html:
    tabs = join(h('<a href="{href}" class="{cls}">{label}</a>', href=f"/i/{q(name)}" + (f"/{t}" if t else ""),
                  cls="on" if t == tab else "", label=label) for t, label in INSTANCE_TABS)
    head = h('<header class="pagehead"><h1>{name}</h1><div class="tabs">{tabs}</div></header>', name=name, tabs=tabs)
    return frame(f"{name} · {title}", join([head, body]), names, csrf, current=f"i:{name}")


def chat_frame(name: str, head: Html, body: Html, names: list[str], csrf: str, header_url: str) -> Html:
    head = h('<div data-poll="{u}" data-every="1500">{head}</div>', u=header_url, head=head)
    return frame(f"{name} · chat", join([head, body]), names, csrf, current=f"i:{name}")


def earlier_chats(name: str, past: list[dict]) -> Html:
    """Shared labels for the quick list and the complete archive."""
    items = []
    for c in past:
        users = [t for t in c["turns"] if t["role"] == "user"]
        text = users[0].get("shown", users[0].get("content", "")) if users else ""
        first = (text or "").splitlines()
        excerpt = first[0].strip() if first else ""
        excerpt = excerpt[:80] + ("…" if len(excerpt) > 80 else "")
        label = f"{c['created'][:16].replace('T', ' ')} · {excerpt or 'No message text'} · {len(users)} message(s)"
        items.append(link(f"/i/{q(name)}/chat?id={q(c['id'])}", label))
    return lines(items, "No earlier chats yet.")


def chat_header(name: str, live: dict, chat: dict | None, past: list[dict], read_only: bool = False) -> Html:
    """The current facts and actions, refreshed independently of the composer and documents."""
    from .web_actions import instance_buttons, post_attrs
    pages = join(h('<a href="{href}">{label}</a>', href=f"/i/{q(name)}" + (f"/{t}" if t else ""), label=label)
                 for t, label in INSTANCE_TABS if t != "chat")
    state = live.get("state", "?")
    facts = [live.get("model") or "", state + (f" since {live['since']}" if live.get("since") else "")]
    if live.get("usd_today"):
        facts.append(f"${live['usd_today']:.2f} today")
    buttons = instance_buttons(live) if live.get("name") else Html("")
    earlier = join([h('<p class="dim">latest {limit} of {total}</p>', limit=min(20, len(past)), total=len(past)),
                    earlier_chats(name, past[:20]), link(f"/i/{q(name)}/chats", "All earlier chats")])
    head = h('<header class="chathead"><button type="button" class="navbtn" data-toggle-nav aria-label="Chats and pages" '
             'aria-expanded="false" data-focus="nav">☰</button><h1>{name}</h1>'
             '<details class="menu earliermenu" data-key="earlier-chats"><summary data-focus="earlier">Earlier chats</summary>'
             '<div class="menubody">{earlier}</div></details><span class="dim facts">{facts}</span>'
             '<button type="button" class="small docsbtn" data-show-docs title="its documents (notes/docs/)" '
             'aria-label="Documents" data-focus="docs">📄<span class="docslabel"> Documents</span></button>{clear}'
             '<details class="menu" data-key="chat-menu"><summary data-focus="menu" title="{name}: pages and actions" '
             'aria-label="{name}: pages and actions">⋯'
             '</summary><div class="menubody"><p class="dim menufacts">{facts}</p>'
             '<div class="menupages">{pages}</div><div class="actions">{buttons}</div>'
             '<div class="actions"><button type="button" class="small" data-notify data-focus="notify">Notify me on this device</button></div>'
             '</div></details></header>',
             name=name, facts=" · ".join(f for f in facts if f), pages=pages, buttons=buttons,
             earlier=h('<div class="earlier">{e}</div>', e=earlier) if earlier else Html(""),
             # always on screen (it sat under the last answer, below the fold on a phone); the chat shown now.
             # The label stays whole ("Clear" alone did not say what is kept); it fits a 375 px header.
             clear=h('<form class="act clearbtn" data-post="/chat/{n}/clear" data-confirm="Clear this chat from the page? '
                     'The conversation is kept in Earlier chats and Transcripts. An open conversation is ended first, and '
                     'a journal write-up is requested."{a}><input type="hidden" name="cid" value="{cid}">'
                     '<button type="submit" class="small" title="Empty this page. The conversation is kept." '
                     'data-focus="clear">Clear chat</button></form>', n=q(name), cid=chat["id"],
                     a=post_attrs("this chat"))
             if chat and not read_only else Html(""))
    busy = (h('<p class="warn">{n} is {s} now: a cycle or sleep pauses for your message, anything else is waited for '
              '— the reply\'s activity says which.</p>', n=name, s=state)
            if state not in ("idle", "?") and not (chat and chat.get("state") == "replying") else Html(""))
    return join([head, busy])


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


def markdown(text: str) -> Html:
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
                ("calendar", d["calendar"] or "none"), ("jobs", d.get("jobs") or "none"),
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
              ("value", "value"), ("value for", "value_for"), ("value graded by", "value_judge")]
JUDGE_TEXTS = [("Reasoning", "reasoning"), ("Why this difficulty", "difficulty_reason"),
               ("Why this value", "value_reason"), ("Origin evidence", "origin_evidence"),
               ("Origin reference", "origin_ref")]


def judge_page(name: str, js: list[dict], tr: dict | None, names: list[str], csrf: str) -> Html:
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
    pairs = [(label, str(j.get(k))) for label, k in JUDGE_KEYS if j.get(k) not in (None, "")]
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
                           f"W{v.get('W')} L{v.get('L')} T{v.get('T')} p={v.get('p')} ${v.get('usd', 0):.2f}"])
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


# -- instance › transcripts (TUI tab 0) ------------------------------------------------------------------------------
TX_FILTERS = {"": "all", "cycles": "cycles", "conversations": "conversations"}
STYLE_CLASS = {"bold": "b", "dim": "dim", "bold green": "b good", "bold yellow": "b warn", "bold cyan": "b talk-text",
               "cyan": "talk-text", "bold underline": "b u"}


def transcripts_page(name: str, es: list[dict], filt: str, summary, live: dict, names: list[str], csrf: str) -> Html:
    tabs = join([link(f"/i/{q(name)}/transcripts" + (f"?f={k}" if k else ""), label) if k != filt
                 else h("<b>{l}</b>", l=label) for k, label in TX_FILTERS.items()], " · ")
    note = (h(' · <span class="warn">the cycle running now (since {s}) appears when it ends</span>',
              s=live.get("since", "?")) if live.get("state") == "in a cycle" else Html(""))
    head = h('<section class="panel"><p>{tabs}</p><p class="dim">{n} {what} · newest first{note}</p></section>',
             tabs=tabs, n=len(es), what=TX_FILTERS[filt] if filt else "transcripts", note=note)
    body = [[link(f"/i/{q(name)}/transcript?id={q(e['id'])}", when(e["ts"])),
             h('<span class="kind-{k}">{k}</span>', k=e["kind"]), clip(summary(e))] for e in es]
    listing = table(["when", "kind", "summary"], body) if body else h('<p class="dim">no {w} yet</p>',
                                                                       w=TX_FILTERS[filt] if filt else "transcripts")
    return instance_frame(name, "transcripts", "transcripts", join([head, section("Transcripts", listing)]), names, csrf)


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
    return next((l for l in text.splitlines() if l.strip()), "").strip()


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
            item.append(pre(texts[k], "wrap"))
        atts.append(h('<div class="att">{i}</div>', i=join(item)))
    alert = (h('<p class="dim">notified on {c}: {why}</p>', c="the web UI" if m["alert"] == "web" else "Telegram",
               why=m.get("alert_why", "")) if m.get("alert") else Html(""))      # channel.py: where it alerted you
    body = h('<p><a href="/messages">← every message</a></p><h1>{who}</h1>'
             '<p class="dim">{ts} · {kind} · {status} · {file}</p>{alert}'
             '<section class="panel">{text}</section>{atts}',
             who=f"{m['from']} → {m['to']}", ts=m["ts"].replace("T", " "), kind=m.get("kind") or "note", status=status,
             file=m["file"], text=pre(m["text"], "wrap"), alert=alert,
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
        jt = join([jt, h('<p class="warn">{n} judge call(s) on a model with no known price are not counted</p>',
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
    return join([h('<p class="dim">checked out: {v}</p>', v=version.commit_describe(version.supervisor_commit())),
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
def _chat_attachments(cid: str, k: int, atts: list[dict]) -> Html:
    out = []
    for j, a in enumerate(atts or []):
        url = f"/chatfiles/{q(cid)}/{k}/{j}"
        if a["name"].lower().endswith(IMAGE_TYPES):
            out.append(h('<a href="{u}" target="_blank"><img class="thumb" src="{u}" alt="{n}" width="192" height="144" loading="lazy"></a>',
                         u=url, n=a["name"]))
        else:
            out.append(h('<a class="file" href="{u}" target="_blank">📎 {n}</a>', u=url, n=a["name"]))
    return h('<div class="atts">{o}</div>', o=join(out)) if out else Html("")


def _activity(lines_: list[str], key: str, open_: bool = False) -> Html:
    """What it did during a reply, collapsible; `key` keeps it open or shut across the feed's refreshes."""
    if not lines_:
        return Html("")
    return h('<details class="activity" data-key="{k}"{o}><summary>what it did ({n})</summary><div data-scroll-key="{k}">{l}</div></details>',
             k=key, o=Html(" open") if open_ else "", n=len(lines_), l=join(h("<p>{x}</p>", x=x) for x in lines_))


def _my_text(text: str) -> Html:
    """What the operator wrote, as written — except the lines they quoted (Reply on a selection), shown as a quote."""
    blocks: list[tuple[bool, list[str]]] = []               # (quoted, lines), runs of one kind
    for line in str(text).replace("\r\n", "\n").split("\n"):
        quoted = bool(_QUOTE.match(line))
        if not blocks or blocks[-1][0] != quoted:
            blocks.append((quoted, []))
        blocks[-1][1].append(_QUOTE.sub("", line, count=1) if quoted else line)
    out = []
    for quoted, b in blocks:
        body = "\n".join(b).strip("\n")
        if body:
            out.append(h('<blockquote class="wrap">{t}</blockquote>' if quoted else '<div class="wrap">{t}</div>', t=body))
    return join(out)


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


def _overwritten(saved: dict | None, sha: str, text: str | None) -> Html:
    """Lines the operator wrote that a later write of the instance dropped: said, with their version to put back."""
    import difflib
    from . import actions
    from .web_actions import post_attrs
    if not saved or text is None or saved.get("sha") == sha:
        return Html("")
    now = {l.strip() for l in text.splitlines()}
    lost = [l for l in saved.get("added") or [] if l not in now]
    if not lost:
        return Html("")
    diff = "\n".join(difflib.unified_diff(saved["text"].splitlines(), text.splitlines(), "yours", "now", lineterm="", n=1))
    return h('<div class="overwritten warn"><p>{n} line(s) you wrote at {ts} are no longer in it.</p>'
             '<details><summary>Your version against now</summary><pre class="diff">{d}</pre></details>'
             '<form class="act" data-post="/docs/{i}/save" data-confirm="Put your version back? It replaces the '
             'current one: its changes since are lost (they are in the diff)." data-after="doc-saved"{a}>'
             '<input type="hidden" name="path" value="{p}"><input type="hidden" name="sha" value="{sha}">'
             '<textarea name="text" hidden>{yours}</textarea><button type="submit" class="small">Restore yours'
             '</button></form></div>', n=len(lost), ts=saved.get("ts", "")[11:16], d=diff, i=q(saved.get("name", "")),
             p=saved.get("path", ""), sha=sha, yours=saved["text"],
             a=post_attrs("Documents", actions.RUN_NOW_S))


def threads_list(name: str, doc: str, sha: str, threads: list[dict], active: str = "") -> Html:
    """Open threads on one document, resolved ones folded under them. Nothing when there are no threads."""
    from .web_actions import post_attrs
    if not threads:
        return Html("")

    def post(p: dict) -> Html:
        # Unsent is the operator's note the instance has not been given: no send id and no answer turn.
        unsent = p.get("sent") is None and not p.get("turn")
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
            ])
        return h('<div class="post{u}" data-pid="{pid}" data-text="{raw}"><div class="who">{who} <span class="dim">{ts}</span>{warn}'
                 '</div>{text}{btns}</div>',
                 u=Html(" unsent") if unsent else "", pid=p["pid"], who=who, ts=p["ts"][11:16],
                 warn=Html(' <span class="warn">only you see this</span>') if unsent else "",
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
               counts: dict | None = None, thread: str = "", archived: bool = False) -> Html:
    """The documents panel beside the chat (/fragment/docs/<name>): the list or the open one rendered."""
    from . import actions
    from .docs import DIR
    from .web_actions import post_attrs
    items = []
    for d in listing:
        n = 0 if counts is None else counts.get(d["path"]) or 0
        items.append(h('<a href="?doc={u}" data-open-doc="{p}"><span>{dot}{p}{ccount}</span>'
                       '<span class="dim docsize">{b} bytes</span></a>',
                       u=q(d["path"]), p=d["path"], b=d["bytes"],
                       dot=Html('<span class="mark" title="changed in this chat">●</span> ')
                       if d["path"] in marked else Html(""),
                       ccount=h(' <span class="ccount" title="open comments">💬 {n}</span>', n=n) if n > 0 else ""))
    new = h('<details class="newdoc" data-key="newdoc"><summary>New document</summary><form data-post="/docs/{n}/save" '
            'data-after="doc-saved"{a}><input type="hidden" name="sha" value="none"><label>Document name '
            '<input name="path" required placeholder="name, e.g. plan" pattern="[A-Za-z0-9][A-Za-z0-9 _.\\-]*" '
            'maxlength="80"></label>'
            '<textarea name="text" hidden></textarea><button type="submit" class="small">Create</button></form></details>',
            n=q(name), a=post_attrs("Documents", actions.RUN_NOW_S))
    back = Html('<button type="button" class="small" data-back-docs aria-label="Back to the documents">← Documents</button>')
    head = h('<div class="docshead">{title} <span class="dim">{dir}/</span>{new}<button type="button" '
             'class="small closedocs" data-close-docs aria-label="Close the documents">✕</button></div>{list}',
             title=back if open_path else Html("<b>Documents</b>"), dir=DIR, new="" if open_path or archived else new,
             list="" if open_path else h('<div class="doclist">{i}</div>', i=join(items) if items else
                                          h('<p class="dim">No current files.</p>') if archived else
                                          h('<p class="dim">none yet: ask {n} for one, it appears here as it writes it.</p>',
                                            n=name)))
    if archived:
        notice = h('<p>Current files — not as they were in this chat</p><p><a href="/i/{n}/chat{query}">'
                   'Open in the current chat to edit or comment</a></p>', n=q(name),
                   query=f"?doc={q(open_path)}" if open_path else "")
        view = (h('<p class="bad">{p}</p>', p=problem) if problem else
                h('<div class="docview" data-read-only="1"><b>{p}</b><div class="docmd">{md}</div></div>',
                  p=open_path, md=markdown(text)) if text is not None else Html(""))
        return join([notice, head, view])
    if problem:
        view = h('<p class="bad">{p}</p>', p=problem)
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
                 '<button type="button" class="small" data-edit-doc data-focus="doc-edit">Edit</button>{cbtn}'
                 '<button type="button" class="copy small icon" data-copy-doc title="copy the document\'s Markdown" '
                 'aria-label="Copy the document">{copy}</button>'
                 '<form class="docdelete" data-post="/docs/{n}/delete" data-after="doc-deleted" '
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
                 p=open_path, s=sha, n=q(name), tattr=tattr, cbtn=cbtn, tlist=tlist, copy=ICONS["copy"], delete=ICONS["delete"],
                 over=_overwritten(saved, sha, text), a=post_attrs("Documents", actions.RUN_NOW_S),
                 md=markdown(text) if text.strip() else Html('<p class="dim">(empty)</p>'), raw=text)
    elif open_path:
        view = h('<p class="dim">{p} is not there (any more).</p>', p=open_path)
    else:
        view = Html("")
    return join([head, view])


def _comment_chips(items: list[dict] | None, kind: str = "") -> Html:
    """💬 chips on a turn: comments the operator sent, or threads the reply answered or opened."""
    if not items:
        return Html("")
    pre = f"{kind} " if kind else ""
    return join(h(' <a class="commentchip" href="?doc={u}&amp;thread={id}" data-open-doc="{doc}" '
                  'data-open-thread="{id}">💬 {pre}{id}</a>',
                  u=q(item["doc"]), doc=item["doc"], id=item["id"], pre=pre) for item in items)


def _reply_failure(name: str, turn: dict, archived: bool) -> Html:
    """Keep raw errors available without making the operator interpret them."""
    note = turn.get("note") or ""
    if not note:
        return Html("")
    kind = turn.get("note_kind", "stopped" if note == "stopped by you" else "unknown")
    if kind == "stopped":
        return h('<p class="warn recovery">You stopped this reply.</p>')
    offer = turn.get("note_retry") and turn.get("retry_text") and not archived
    why = {"connection": "The connection to the instance failed.",
           "service": "The service could not finish the reply.",
           "model": "The model could not finish the reply."}.get(kind, "The reply failed.")
    action = ("No external actions ran. Press Retry, then Send, to try the message again." if offer else
              "Actions may already have run. Check Activity and Services before sending again.")
    recovery = (h('<button type="button" class="small" data-retry="{text}">Retry</button>', text=turn["retry_text"])
                if offer else h('<a href="/i/{n}/activity">Activity</a> · <a href="/services">Services</a>', n=q(name)))
    return h('<p class="warn recovery">{why} {action}</p><div class="actions">{recovery}</div>'
             '<details class="tech"><summary>Technical details</summary><pre>{raw}</pre></details>',
             why=why, action=action, recovery=recovery, raw=note)


def _turn_time(ts: str) -> Html:
    """Stored timestamps use the supervisor's local time, as do the other UI dates."""
    return h('<time datetime="{iso}" title="{full}">{short}</time>',
             iso=ts, full=ts.replace("T", " "), short=ts[11:16]) if ts else Html("")


def _sent_cards(name: str, sent: list[dict]) -> list[Html]:
    """What the instance sent the operator (send_message), one card each; Reply quotes it in the composer, and a quoted
    question is answered by the message that quotes it (web_chat.answer_quoted), as a Telegram reply answers it."""
    cards = []
    for m in sent:             # files open in a sheet of their own: in an app, a link replaced its only view
        atts = join(h('<a href="/files/{f}/{j}" target="_blank">{img}</a>', f=q(m["file"]), j=j,
                      img=h('<img class="thumb" src="/files/{f}/{j}" alt="{n}" width="192" height="144" loading="lazy">', f=q(m["file"]), j=j,
                            n=a["name"])
                      if a["name"].lower().endswith(IMAGE_TYPES) else h("📎 {n}", n=a["name"]))
                    for j, a in enumerate(m.get("attachments") or []))
        asks = m.get("kind") == "question"
        what = ("a question" if asks else "a message") + (" (answered)" if asks and m.get("read") else "")
        cards.append(h('<div class="turn sent" data-k="s{f}" data-raw="{raw}"><div class="who">{n} sent you '
                       '<a href="/messages/{f}">{what}</a> <span class="dim">{ts}</span><button type="button" '
                       'class="reply small icon" data-reply-message title="Reply to the message" '
                       'aria-label="Reply to the message">{reply}</button></div><div class="wrap">{t}</div>'
                       '<div class="atts">{a}</div></div>', n=name, f=q(m["file"]), raw=m["text"], what=what,
                       ts=_turn_time(m["ts"]), reply=ICONS["reply"], t=m["text"][:600], a=atts))
    return cards


def chat_feed(name: str, chat: dict | None, sent: list[dict], archived: bool = False) -> Html:
    """The conversation as it stands: each turn, the files it sent you meanwhile, and the reply running now."""
    from .web_actions import button
    if not chat:
        return join([*_sent_cards(name, sent),
                     h('<p class="dim" data-k="empty">No chat open with {n}. Write below to start one.</p>', n=name)])
    items = []
    for k, t in enumerate(chat["turns"]):
        raw = t.get("shown", t.get("content", "")) if t["role"] == "user" else t.get("content", "")
        controls = (h('<button type="button" class="copy small icon" data-copy title="copy the message as Markdown" '
                      'aria-label="Copy the message">{copy}</button><button type="button" class="reply small icon" '
                      'data-reply-message title="Reply to the message" aria-label="Reply to the message">{reply}</button>',
                      copy=ICONS["copy"], reply=ICONS["reply"]) if raw else Html(""))
        if t["role"] == "user":
            tag = h(' <span class="dim">· during its reply</span>') if t.get("interjected") else Html("")
            if t.get("edits"):
                tag = join([tag, join(h(' <a class="editchip" href="?doc={u}" data-open-doc="{p}">✎ edited {p}</a>',
                                        u=q(p), p=p) for p in t["edits"])])
            if t.get("deleted"):                 # nothing to open: it is gone
                tag = join([tag, join(h(' <span class="editchip">🗑 deleted {p}</span>', p=p) for p in t["deleted"])])
            if t.get("comments"):
                tag = join([tag, _comment_chips(t["comments"])])
            # A comment-only turn's shown text is empty: the chips are the turn, not an empty bubble.
            shown = t.get("shown", t["content"])
            items.append((t["ts"], h('<div class="turn me" data-k="u{k}" data-raw="{raw}"><div class="who">you '
                                     '<span class="dim">{ts}</span>{tag}{controls}</div>'
                                     '{text}{atts}</div>', k=k, ts=_turn_time(t["ts"]), tag=tag, raw=shown, controls=controls,
                                     text=_my_text(shown) if shown else Html(""),
                                     atts=_chat_attachments(chat["id"], k, t.get("attachments")))))
        else:
            # Keep partial answers readable; put the failure and recovery below them.
            mark = (h(' <span class="warn incomplete">incomplete</span>')
                    if t.get("content") and t.get("note") else Html(""))
            body = markdown(t["content"]) if t.get("content") else Html("")
            if not t.get("content") and not t.get("note"):
                body = Html('<p class="dim">(no answer)</p>')
            recovery = _reply_failure(name, t, archived)
            cards = join([doc_cards(t.get("docs"), name if archived else ""), _comment_chips(t.get("answered"), "answered"),
                          _comment_chips(t.get("opened"), "opened")])
            seen = h(' data-seen-inst="{n}" data-seen-reply="{ts}"', n=name, ts=t["ts"]) if not archived else Html("")
            items.append((t["ts"], h('<div class="turn it" data-k="a{k}" data-raw="{raw}"{seen}><div class="who">{n} '
                                     '<span class="dim">{ts}</span>{mark}{copy}</div>{act}{body}{cards}{recovery}</div>',
                                     k=k, raw=t.get("content") or "", n=name, ts=_turn_time(t["ts"]), seen=seen,
                                     mark=mark, copy=controls, act=_activity(t.get("activity", []), f"t{k}"),
                                     body=body, cards=cards, recovery=recovery)))
    items += [(m["ts"], card) for m, card in zip(sent, _sent_cards(name, sent))]
    if chat["state"] == "ended":   # in time order: what it sent you after the chat ended shows below the line
        when = chat.get("ended", "")[:16].replace("T", " ")
        items.append((chat.get("ended", ""), h('<p class="dim ended" data-k="ended">This conversation ended on {e}.</p>', e=when)
                      if archived else                # a past chat: when it ended, nothing about a next message
                      h('<p class="dim ended" data-k="ended">Ended {e}: journal write-up requested. Your next message starts a new '
                        'conversation.</p>', e=when)))
    items.sort(key=lambda x: x[0])
    feed = []
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

    for ts, item in items:
        day_separator(ts)
        feed.append(item)
    if chat["state"] == "replying":
        r = chat.get("reply") or {}
        day_separator(r.get("started", ""))
        feed.append(h('<div class="turn it live" data-k="live"><div class="who">{n} <span class="dim">replying since {s}'
                      '<span class="elapsed" data-since="{iso}"></span></span></div>'
                      '{act}{partial}<div class="actions">{stop}</div></div>', n=name, s=_turn_time(r.get("started", "")),
                      iso=r.get("started", ""),
                      act=_activity(r.get("activity", []), "live", open_=True),
                      partial=h('<p class="dim">the answer appears when it is ready</p>'),
                      stop=button("chat-stop", "Stop the reply",
                                  "stop this reply? Only the reply stops. The conversation stays open.",
                                  {"cid": chat["id"]}, cls="small")))
    if chat.get("pending_edits") and not archived:
        feed.append(h('<p class="pending-edits" data-k="pend">✎ Your changes to {p} go to {n} with your next message.</p>',
                      p=", ".join(sorted(chat["pending_edits"])), n=name))
    if archived:                                  # a past chat's page: nothing here acts on the current one
        return join(feed)
    if chat["state"] not in ("replying", "ended") and chat["turns"]:
        feed.append(h('<div class="actions" data-k="acts">{b}</div>', b=button(
            "chat-end", "End the conversation", f"end this conversation with {name}? The messages stay on this page. "
            "Your next message starts a new conversation. A journal write-up is requested.",
            {"cid": chat["id"]}, cls="small")))
    return join(feed)


def chat_page(name: str, chat: dict | None, sent: list[dict], past: list[dict], live: dict, names: list[str],
              csrf: str, read_only: bool = False, panel: Html = Html(""), doc: str = "") -> Html:
    from . import messages
    feed_url = f"/fragment/chat/{q(name)}" + (f"?id={q(chat['id'])}" if read_only and chat else "")
    composer = Html("") if read_only else h(
        '<form class="composer" data-post="/chat/{n}/send" data-multipart="1" data-reset="1" data-files data-draft="{n}" data-sent-mids="{mids}" '
        'data-max-file="{mf}" data-max-total="{mt}" data-max-files="{mn}"><input type="hidden" name="mid" value="">'
        '<div class="chips" data-picked></div>'
        '<textarea name="text" rows="2" maxlength="20000" placeholder="Message {n}" aria-label="Message {n}">'
        '</textarea>'
        '<div class="actions"><label class="pick">Attach files<input type="file" name="files" multiple class="sr-file">'
        '</label>'
        '<span class="dim keys">Enter sends · Shift-Enter new line · / to write · Alt-↑↓ another chat</span>'
        '<button type="submit">Send</button></div></form>',
        n=name, mf=messages.ATTACH_MAX_BYTES, mt=messages.ATTACH_MAX_TOTAL, mn=messages.ATTACH_MAX_FILES,
        mids=json.dumps([t["mid"] for t in (chat or {}).get("turns", []) if t.get("mid")]))
    banner = (h('<p class="panel">Earlier chat, {date}. <a href="/i/{n}/chat">Back to the current chat</a></p>',
                date=chat['created'][:16].replace('T', ' '), n=q(name)) if read_only and chat else Html(""))
    body = join([banner, h('<section class="panel chat" data-scroll><div class="feed" data-poll="{u}" data-every="1500" data-keyed="1" '
                         'data-follow="1">{f}</div></section><button type="button" class="newbelow" data-newbelow hidden>'
                         '↓ New below</button>', u=feed_url, f=chat_feed(name, chat, sent, archived=read_only)), composer,
                 Html('<div class="dropzone" data-dropzone hidden>Drop to attach</div>') if not read_only else Html("")])
    aside = h('<aside class="docs" id="docs" data-poll="/fragment/docs/{n}?doc={d}{archive}" data-every="1500" data-lazy="1">{p}</aside>',
              n=q(name), d=q(doc), p=panel, archive=f"&id={q(chat['id'])}" if read_only and chat else "")
    return chat_frame(name, chat_header(name, live, chat, past, read_only),
                      h('<div class="chatgrid"><div class="chatpage">{b}</div>{a}</div>', b=body, a=aside),
                      names, csrf, f"/fragment/chat/{q(name)}/header"
                      + (f"?id={q(chat['id'])}" if read_only and chat else ""))

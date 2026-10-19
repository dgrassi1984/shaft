"""The web UI's read-only pages (PLAN-webui.md M1): the TUI's tabs 2–6, 8, 9, 0 and the messages browser, plus an
instance's journal and activity. Each takes the data the TUI's tab takes — the same `panel` readers, called by web.py
— and shows the same fields, so the two UIs cannot disagree. Escaping, control characters and the page frame are
web_pages'; nothing here writes anything.
"""
from __future__ import annotations

import re
import urllib.parse

from .web_pages import Html, esc, h, join, nav, page

MARK = {"yes": ("✓", "good"), "partial": ("½", "warn"), "no": ("✗", "bad")}
INSTANCE_TABS = [("", "Summary"), ("judge", "Judge"), ("changes", "Changes"), ("benchmarks", "Benchmarks"),
                 ("transcripts", "Transcripts"), ("journal", "Journal"), ("activity", "Activity")]
GLOBAL_PAGES = [("/trials", "Trials"), ("/messages", "Messages"), ("/costs", "Costs"), ("/services", "Services")]


def q(value: str) -> str:
    """A value for a URL path segment or query parameter."""
    return urllib.parse.quote(str(value), safe="")


def frame(title: str, body: Html, names: list[str], csrf: str, current: str = "") -> Html:
    return page(title, body, nav(names, current, GLOBAL_PAGES), csrf)


def instance_frame(name: str, tab: str, title: str, body: Html, names: list[str], csrf: str) -> Html:
    tabs = join(h('<a href="{href}" class="{cls}">{label}</a>', href=f"/i/{q(name)}" + (f"/{t}" if t else ""),
                  cls="on" if t == tab else "", label=label) for t, label in INSTANCE_TABS)
    head = h('<header class="pagehead"><h1>{name}</h1><div class="tabs">{tabs}</div></header>', name=name, tabs=tabs)
    return frame(f"{name} · {title}", join([head, body]), names, csrf, current=f"i:{name}")


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


# -- a safe Markdown subset: escape first, then headings, lists, tables, code, emphasis, http(s) links -----------
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_EM = re.compile(r"(?<![*\w])\*(?![\s*])(.+?)(?<![\s*])\*(?![*\w])")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def inline(text: str) -> Html:
    out = []
    for k, part in enumerate(_CODE.split(str(esc(text)))):     # escaped once; odd parts are inside backticks
        if k % 2:
            out.append(f"<code>{part}</code>")
            continue
        part = _LINK.sub(r'<a href="\2" rel="noopener noreferrer" target="_blank">\1</a>', part)
        part = _BOLD.sub(r"<b>\1</b>", part)
        out.append(_EM.sub(r"<i>\1</i>", part))
    return Html("".join(out))


_LIST = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")


def markdown(text: str) -> Html:
    out: list[str] = []
    para: list[str] = []
    depth: list[int] = []                                   # indentation of each open <ul>
    src = str(text).replace("\r\n", "\n").split("\n")

    def close_para():
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    def close_lists(to: int = -1):
        while depth and depth[-1] > to:
            depth.pop()
            out.append("</li></ul>")

    i = 0
    while i < len(src):
        line = src[i]
        if line.strip().startswith("```"):
            close_para()
            close_lists()
            body = []
            i += 1
            while i < len(src) and not src[i].strip().startswith("```"):
                body.append(src[i])
                i += 1
            out.append(str(pre("\n".join(body))))
            i += 1
            continue
        if line.strip().startswith("|") and i + 1 < len(src) and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", src[i + 1]):
            close_para()
            close_lists()
            cells = lambda l: [c.strip() for c in l.strip().strip("|").split("|")]
            head = cells(line)
            body = []
            i += 2
            while i < len(src) and src[i].strip().startswith("|"):
                body.append(cells(src[i]))
                i += 1
            out.append(str(table(head, [[inline(c) for c in r] for r in body], "md")))
            continue
        if m := _HEAD.match(line):
            close_para()
            close_lists()
            n = min(6, len(m[1]) + 1)                           # the page has its own h1
            out.append(f"<h{n}>{inline(m[2])}</h{n}>")
        elif m := _LIST.match(line):
            close_para()
            ind = len(m[1].expandtabs(4))
            if not depth or ind > depth[-1]:
                depth.append(ind)
                out.append("<ul><li>")
            else:
                close_lists(ind)
                if not depth:
                    depth.append(ind)
                    out.append("<ul><li>")
                else:
                    out.append("</li><li>")
            out.append(str(inline(m[2])))
        elif not line.strip():
            close_para()
            close_lists()
        elif depth and line.startswith(" "):                 # a list item's continuation line
            out.append(" " + str(inline(line.strip())))
        else:
            close_lists()
            para.append(line.strip())
        i += 1
    close_para()
    close_lists()
    return Html(f'<div class="md">{"".join(out)}</div>')


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
    return rows(pairs)


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
    parts.append(section("Recent journal", lines(clip(j) for j in d["journal"]) + link(f"/i/{q(name)}/journal", "the whole journal →")))
    if d["changes"]:
        parts.append(section("Kept changes to its own code", lines(
            f"{c['ts'][:16]} {', '.join(c['files'])}" + (f" ({len(c['delegated'])} by its coder)" if c["delegated"]
                                                         else "") for c in d["changes"][:5])))
    if d["merges"]:
        parts.append(section("Template updates (shaft update)", lines(d["merges"])))
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
def trials_page(ts: list[dict], c: dict, only: str, progress, names: list[str], csrf: str) -> Html:
    caps = [f"{cap['name']} {cap['trials_started'][0]}/{cap['trials_started'][1]} trials "
            f"${cap['trials'][0]:.2f}/${cap['trials'][1]:.2f}" for cap in c["caps"]]
    filt = join([link("/trials", "all instances") if only else Html("<b>all instances</b>")] +
                [h(" · {x}", x=link(f"/trials?i={q(n)}", n) if n != only else h("<b>{n}</b>", n=n)) for n in names])
    head = h('<section class="panel"><p>{filt}</p><p class="dim">today: {caps} · all trials ${all} (operator trials '
             'are not capped)</p></section>', filt=filt, caps=" · ".join(caps) or "no caps",
             all=f"{c['today']['trials']:.2f}")
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
    return frame("Trials", join([h("<h1>Trials</h1>"), head, section("Newest last", listing)]), names, csrf,
                 current="/trials")


def trial_page(t: dict, report: str, names: list[str], csrf: str) -> Html:
    body = h('<p><a href="/trials">← every trial</a></p><h1>Trial {id} · {inst}</h1>{r}', id=t.get("id", ""),
             inst=t.get("instance", ""), r=section("Report (shaft trial show)", pre(report, "wrap")))
    return frame(f"Trial {t.get('id', '')}", body, names, csrf, current="/trials")


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
    body = h('<p><a href="/messages">← every message</a></p><h1>{who}</h1>'
             '<p class="dim">{ts} · {kind} · {status} · {file}</p>'
             '<section class="panel">{text}</section>{atts}',
             who=f"{m['from']} → {m['to']}", ts=m["ts"].replace("T", " "), kind=m.get("kind") or "note", status=status,
             file=m["file"], text=pre(m["text"], "wrap"),
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
def services_live(svcs: list[dict], order: list[str], runs: list[str]) -> Html:
    by = {s["name"]: s for s in svcs}
    body = []
    for name in order:
        s = by.get(name, {"running": False, "detail": ""})
        body.append(([h('<a href="/services?log={n}">{n}</a>', n=name),
                      h('<span class="{c}">{t}</span>', c="good" if s["running"] else "bad",
                        t="● running" if s["running"] else "○ NOT RUNNING"),
                      f"pid {s['pid']}" if s.get("pid") else "", s.get("detail", ""),
                      h('<span class="warn">older code than checked out: restart it</span>') if s.get("stale") else ""],
                     ""))
    return join([table(["service", "state", "pid", "detail", ""], body),
                 h('<p class="{c}">Runs a scheduler restart would kill: {r}</p>', c="warn" if runs else "dim",
                   r=", ".join(runs) or "none")])


def services_page(live: Html, sel: str, log_name: str, tail: list[str], names: list[str], csrf: str) -> Html:
    body = join([h("<h1>Services</h1>"), h('<section class="panel"><div data-poll="/fragment/services">{l}</div>'
                                           '</section>', l=live),
                 section(f"{sel} log (.supervisor/{log_name})", pre("\n".join(tail) or "(empty)"))])
    return frame("Services", body, names, csrf, current="/services")

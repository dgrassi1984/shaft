"""`shaft top`'s control-panel views: tabs 2–6, 8, 9 and 0, the actions, and the
overlays every view shares (y/n question, one-line editor, picker, help). Tabs 1 (overview) and 7 (messages)
are top.py's original views.

Every action runs the same CLI subcommand an operator would type (`python -m supervisor …`), so the panel adds
no authority of its own and inherits each command's refusals; each one is logged as `top: operator …`. Actions
that take longer than a moment run detached (they outlive the panel), with their output in
.supervisor/top-actions.log.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass, field

from rich.console import Group
from rich.table import Table
from rich.text import Text

from . import actions, dashboard, metrics, panel

TABS = [("1", "overview"), ("2", "instance"), ("3", "judge"), ("4", "trials"), ("5", "benchmarks"),
        ("6", "changes"), ("7", "messages"), ("8", "costs"), ("9", "services"), ("0", "transcripts")]
TAB_OF = dict(TABS)
SPARK = "▁▂▃▄▅▆▇█"
MARK = {"yes": ("✓", "green"), "partial": ("½", "yellow"), "no": ("✗", "red")}
HELP = {
    "overview": "↑/↓ instance · Enter details · c cycle · p pause/resume · s sleep now · u update · w write · "
                "r reply · K kill · a recap every instance (now + last hour, by a small model) · x dismiss alerts · "
                "m messages",
    "instance": "←/→ instance · ↑/↓ scroll · c cycle · p pause/resume · s sleep · u update · t tools · w write · r reply"
                " · K kill · D delete",
    "judge": "←/→ instance · ↑/↓ cycle · Enter verdict in full · Esc back",
    "trials": "↑/↓ trial · Enter report · f this instance / all · ←/→ instance · a queue an A/A calibration trial",
    "benchmarks": "←/→ instance · ↑/↓ scroll · b run the battery now (when installed)",
    "changes": "←/→ instance · ↑/↓ scroll",
    "costs": "↑/↓ scroll",
    "services": "↑/↓ service · r start/restart · o stop · a autonomy on/off",
    "transcripts": "←/→ instance · ↑/↓ PgUp/PgDn pick · Enter read it in full · f all / cycles / conversations · "
                   "t thinking on/off (conversations) · Esc back",
}


@dataclass
class Overlay:
    """A question waiting for y/n, a one-line editor, or a picker; at most one at a time."""
    kind: str                                   # ask | input | pick
    text: str                                   # the question / the editor's label / the picker's title
    run: object = None                          # ask: run() -> str · input: run(text) -> str · pick: run(value)
    buf: str = ""
    items: list = field(default_factory=list)   # pick: [(label, value)]
    sel: int = 0


# -- running actions: the same as the web UI's (actions.py), logged as `top: operator …` ------------------------
def run_now(args: list[str], timeout: float = actions.RUN_NOW_S) -> str:
    """A quick subcommand: its last line of output, or "refused: …"."""
    ok, last = actions.run_now(args, timeout, who="top")
    return last if ok else f"refused: {last}"


def run_detached(args: list[str]) -> str:
    return actions.run_detached(args, who="top")


busy_instances = actions.busy_instances


def service_guard(service: str, action: str) -> str:
    return actions.service_guard(service, action, busy=lambda: busy_instances())


# -- shared rendering ----------------------------------------------------------------------------------------------
SHORT = {"overview": "all", "instance": "inst", "judge": "judge", "trials": "trial", "benchmarks": "bench",
         "changes": "chg", "messages": "msg", "costs": "cost", "services": "svc", "transcripts": "tx"}


def tab_bar(ui, snap: dict, width: int | None = None) -> Text:
    """The tabs; with short names when the full ones would not fit (every tab stays visible at 80 columns)."""
    width = width or ui.width
    full = sum(len(n) + 4 for _, n in TABS) + 24 > width
    t = Text(no_wrap=True, overflow="ellipsis")
    for key, name in TABS:
        on = ui.tab == name
        t.append(f" {key} {SHORT[name] if full else name} ", style="reverse bold" if on else "dim")
    alerts = len(snap.get("alerts") or [])
    if alerts:
        t.append(f"  ⚠ {alerts}", style="bold red")
    if snap.get("spend") is not None:
        t.append(f"  ${snap['spend']:.2f} today", style="dim")
    return t


def footer(ui, hint: str) -> Text:
    import time
    o = ui.overlay
    if o and o.kind == "ask":
        return Text(f" {o.text}  y/n", style="bold yellow", no_wrap=True, overflow="ellipsis")
    if o and o.kind == "input":
        return Text(f" {o.text}{o.buf}▌   (Enter sends · Esc cancels)", style="bold", no_wrap=True, overflow="ellipsis")
    if ui.flash and time.time() < ui.flash_until:
        return Text(f" {ui.flash}", style="bold", no_wrap=True, overflow="ellipsis")
    return Text(f" {hint} · 0–9 tabs · ? help · q quits", style="dim", no_wrap=True, overflow="ellipsis")


def window(lines: list, top: int, rows: int) -> list:
    return lines[top:top + rows]


def scroll_to(sel: int, top: int, rows: int) -> int:
    """The first visible row that keeps `sel` in view."""
    if sel < top:
        return sel
    if sel >= top + rows:
        return sel - rows + 1
    return top


def wrap(text: str, width: int) -> list[str]:
    out = []
    for line in text.splitlines() or [""]:
        out.extend(textwrap.wrap(line, max(10, width), replace_whitespace=False) or [""])
    return out


def spark(values: list[float | None]) -> str:
    return "".join("·" if v is None else SPARK[min(7, int(v / 5 * 7.999))] for v in values)


def page(head: list, body: list, ui, hint: str, height: int) -> Group:
    """Header lines, then as much of the body as fits, then the footer at the bottom."""
    rows = max(1, height - len(head) - 1)
    body = body[:rows]
    pad = rows - len(body)
    return Group(*head, *body, *([Text("\n" * (pad - 1))] if pad > 1 else []), footer(ui, hint))


def _names(snap: dict) -> list[str]:
    return [i["name"] for i in snap.get("instances", [])]


def current(ui, snap: dict) -> str:
    names = _names(snap)
    return names[min(ui.inst, len(names) - 1)] if names else ""


def _scrolled(lines: list, ui, height: int, head_rows: int) -> list:
    rows = max(1, height - head_rows - 1)
    ui.scroll = max(0, min(ui.scroll, max(0, len(lines) - rows)))
    return lines[ui.scroll:ui.scroll + rows]


def _inst_head(ui, snap: dict, title: str) -> Text:
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(f" {title} · ", style="bold")
    for k, n in enumerate(_names(snap)):
        t.append(f" {n} ", style="reverse bold" if n == current(ui, snap) else "dim")
    t.append("  ←/→", style="dim")
    return t


# -- 2 instance --------------------------------------------------------------------------------------------------
def render_instance(snap, ui, width, height):
    name = current(ui, snap)
    if not name:
        return page([tab_bar(ui, snap)], [Text(" no instances")], ui, HELP["instance"], height)
    d = panel.instance_detail(name)
    live = next((i for i in snap["instances"] if i["name"] == name), {})
    lines: list[Text] = []

    def sec(title):
        lines.append(Text(""))
        lines.append(Text(f" {title}", style="bold underline"))

    def row(label, value, style=""):
        t = Text(f"   {label:<14}", style="dim")
        t.append(value, style=style)
        lines.append(t)

    row("state", f"{live.get('state', '?')}{' since ' + live['since'] if live.get('since') else ''}"
        + (" · PAUSED by you" if d["paused"] else ""), "yellow" if d["paused"] else "")
    if live.get("doing"):
        row("now", live["doing"], "bold")
    if live.get("goal"):
        row("goal", live["goal"])
    row("model", f"{d['model']} via {d['via']} · effort {d['effort']}")
    row("wakes", f"every {d['wake']} min · next {live.get('next_wake', '?')}")
    row("tools off", ", ".join(d["tools_off"]) or "none")
    row("shared folder", d["shared"] or "none")
    row("mail", d["mail"] or "none")
    row("calendar", d["calendar"] or "none")
    row("gdrive", d.get("gdrive") or "none")
    row("gchat", d.get("gchat") or "none")
    row("jobs", d.get("jobs") or "none")
    row("python", d["python"], "yellow" if "BROKEN" in d["python"] else "")
    row("browser", d["browser"], "yellow" if "not installed" in d["browser"] else "")
    row("internet", d["internet"])
    if d["questions"]:
        sec(f"Open questions to you ({len(d['questions'])}) — r replies to the oldest")
        for q in d["questions"][:3]:
            lines.append(Text(f"   {q['date'][:16]} {q['kind']}: " + " ".join(q["body"].split())[:width - 30]))
    if d["probation"]:
        sec("On probation now")
        for e in d["probation"]:
            lines.append(Text(f"   {e['commit'][:8]} since {e['since'][:16]} · {', '.join(e['files'])[:width - 40]}"
                              f" · baseline {e.get('baseline') if e.get('baseline') is None else round(e['baseline'], 2)}"))
    sec("Recent journal")
    for h in d["journal"]:
        lines.append(Text(f"   {h}"[:width - 1], style="dim" if "Conversation" in h else ""))
    if d["changes"]:
        sec("Kept changes to its own code")
        for c in d["changes"][:5]:
            who = f" ({len(c['delegated'])} by its coder)" if c["delegated"] else ""
            lines.append(Text(f"   {c['ts'][:16]} {', '.join(c['files'])}{who}"[:width - 1]))
    if d["merges"]:
        sec("Template updates (shaft update)")
        for m in d["merges"]:
            lines.append(Text(f"   {m}"[:width - 1], style="dim"))
    sec("self/stats.md (written by the supervisor)")
    for l in d["stats"].splitlines():
        for w in wrap(l, width - 4):
            lines.append(Text(f"   {w}"))
    head = [tab_bar(ui, snap), _inst_head(ui, snap, "instance")]
    return page(head, _scrolled(lines, ui, height, len(head)), ui, HELP["instance"], height)


# -- 3 judge ------------------------------------------------------------------------------------------------------
def judge_line(j: dict, width: int, selected: bool) -> Text:
    t = Text(no_wrap=True, overflow="ellipsis", style="reverse" if selected else "")
    t.append(f" {j.get('episode_ts', '')[5:16].replace('T', ' ')} ", style="" if selected else "dim")
    sym, color = MARK.get(j.get("criterion_met"), ("?", "white"))
    claimed = j.get("claimed")
    t.append(f"{sym} ", style=color)
    t.append(f"{'claimed ' + claimed if claimed and claimed != j.get('criterion_met') else '':<15}", style="yellow")
    ach = j.get("achieved")
    t.append(f"d{j.get('difficulty', '·')} n{j.get('novelty', '·')} " + (f"{ach:3.1f} " if ach is not None else " ·  "))
    v = j.get("value")
    t.append(f"v{v if v is not None else '·'} ", style="green" if (v or 0) >= 3 else ("red" if v == 0 else ""))
    origin = j.get("origin") or "?"
    t.append(f"{origin:<9}", style="cyan" if origin == "self" else "")
    if j.get("claim_supported") is False:
        t.append("unsupported ", style="red")
    t.append(" ".join((j.get("goal") or "").split()))
    return t


def judge_detail(j: dict, width: int) -> list[str]:
    keys = [("judged", "judged_at"), ("model", "model"), ("judge", "judge"), ("criterion met", "criterion_met"),
            ("claimed", "claimed"), ("confidence", "confidence"), ("claim supported", "claim_supported"),
            ("criterion", "criterion_quality"), ("failure mode", "failure_mode"), ("origin declared", "origin_declared"),
            ("origin (judged)", "origin"), ("origin supported", "origin_supported"), ("difficulty", "difficulty"),
            ("novelty", "novelty"), ("value", "value"), ("value for", "value_for"), ("value graded by", "value_judge"),
            ("delivery supported", "delivery_supported"), ("deliverable met", "deliverable_met"),
            ("self-imposed clauses", "self_imposed_clauses"), ("self-audit", "self_audit")]
    out = [f"cycle {j.get('episode_ts', '')}", ""] + wrap(f"Goal: {j.get('goal', '')}", width) + [""]
    shown = {**j, "confidence": metrics.confidence_shown(j)}      # "90 → 60" where the base rate moved it
    out += [f"{label:<17}{shown.get(k)}" for label, k in keys if shown.get(k) not in (None, "")]
    if j.get("achieved") is not None:
        out.append(f"{'achieved':<17}{j['achieved']:.1f} / 5")
    for label, k in (("Reasoning", "reasoning"), ("Why this difficulty", "difficulty_reason"), ("Why this value", "value_reason"),
                     ("Origin evidence", "origin_evidence"), ("Origin reference", "origin_ref")):
        if j.get(k):
            out += ["", f"{label}:"] + wrap(str(j[k]), width)
    return out


def render_judge(snap, ui, width, height):
    name = current(ui, snap)
    js = panel.judgments(name) if name else []
    head = [tab_bar(ui, snap), _inst_head(ui, snap, "judge")]
    if ui.detail and js:
        j = js[min(ui.sel, len(js) - 1)]
        lines = [Text(f" {l}") for l in judge_detail(j, width - 2)]
        return page(head, _scrolled(lines, ui, height, len(head)), ui, "↑/↓ scroll · Esc back", height)
    rated = [j["achieved"] for j in reversed(js)][-40:]
    models = sorted({j.get("model") or "?" for j in js})
    tr = panel.traits(name) if name else None
    info = Text(f" {len(js)} judged · achieved (0–5, oldest→newest): ", style="dim", no_wrap=True)
    info.append(spark(rated), style="cyan")
    worth = [j.get("value") for j in reversed(js)][-40:]
    if any(v is not None for v in worth):
        info.append(" · value: ", style="dim")
        info.append(spark(worth), style="green")
    if tr:
        ok = sum(1 for t in tr["traits"] if t["supported"] == "yes")
        info.append(f" · personality traits supported {ok}/{len(tr['traits'])}", style="dim")
    head += [info, Text(f" models: {', '.join(m.split('/')[-1] for m in models)}", style="dim", no_wrap=True)]
    recent = list(reversed(js))
    head += [Text(" " + line, style="dim", no_wrap=True) for line in
             [metrics.delivery_line(metrics.valued(recent)[-20:]), *metrics.self_audit_lines(recent)]]
    rows = max(1, height - len(head) - 1)
    ui.sel = max(0, min(ui.sel, len(js) - 1))
    ui.top = scroll_to(ui.sel, ui.top, rows)
    body = [judge_line(j, width, k == ui.sel) for k, j in enumerate(js[ui.top:ui.top + rows], start=ui.top)]
    return page(head, body or [Text(" no judged cycles yet", style="dim")], ui, HELP["judge"], height)


# -- 4 trials ------------------------------------------------------------------------------------------------------
def trial_line(t: dict, width: int, selected: bool) -> Text:
    v = t.get("verdict") or {}
    line = Text(no_wrap=True, overflow="ellipsis", style="reverse" if selected else "")
    line.append(f" {t.get('created', '')[5:16].replace('T', ' ')} ", style="" if selected else "dim")
    line.append(f"{t.get('instance', ''):<7}{t.get('by', ''):<9}")
    status = t.get("status", "")
    line.append(f"{status:<9}", style="green" if status == "done" else "yellow" if status in ("queued", "testing") else "")
    if status == "done":
        label = v.get("label", "")
        color = {"better": "green", "worse": "red"}.get(label, "")
        line.append(f"{label:<20}", style=color)
        line.append(f"W{v.get('W')} L{v.get('L')} T{v.get('T')} p={v.get('p')} ${v.get('usd', 0):.2f}  ")
    else:
        line.append(f"{panel.trial_progress(t) + ' replayed':<20}{'':<26}")
    line.append(" ".join((t.get("hypothesis") or "").split()))
    return line


def render_trials(snap, ui, width, height):
    name = current(ui, snap)
    ts = panel.trials(name if ui.only else None)
    head = [tab_bar(ui, snap), _inst_head(ui, snap, "trials") if ui.only else
            Text(" trials · all instances (f: only the selected one)", style="bold")]
    if ui.detail and ts:
        from . import trial
        t = ts[min(ui.sel, len(ts) - 1)]
        lines = [Text(f" {l}") for l in wrap(trial.report(t), width - 2)]
        return page(head, _scrolled(lines, ui, height, len(head)), ui, "↑/↓ scroll · Esc back", height)
    c = panel.costs(days=1)
    caps = Text(" today: ", style="dim", no_wrap=True, overflow="ellipsis")
    for cap in c["caps"]:
        spent, limit = cap["trials"]
        n, per_day = cap["trials_started"]
        caps.append(f"{cap['name']} {n}/{per_day} trials ${spent:.2f}/${limit:.2f}  ",
                    style="red" if spent >= panel.CAP_ALERT * limit else "dim")
    caps.append(f"· all trials ${c['today']['trials']:.2f} (operator trials are not capped)", style="dim")
    head.append(caps)
    rows = max(1, height - len(head) - 1)
    ui.sel = max(0, min(ui.sel, len(ts) - 1))
    ui.top = scroll_to(ui.sel, ui.top, rows)
    body = [trial_line(t, width, k == ui.sel) for k, t in enumerate(ts[ui.top:ui.top + rows], start=ui.top)]
    return page(head, body or [Text(" no trials yet", style="dim")], ui, HELP["trials"], height)


# -- 5 benchmarks -------------------------------------------------------------------------------------------------
def render_benchmarks(snap, ui, width, height):
    name = current(ui, snap)
    lines: list[Text] = []
    b = panel.battery_status(name) if name else {"installed": False, "results": [], "lines": []}
    lines.append(Text(" Capability battery", style="bold underline"))
    if b.get("off"):
        lines.append(Text(f"   off for {name}: no battery runs, scheduled or by hand (`shaft battery on {name}`)",
                          style="yellow"))
    elif not b["installed"]:
        lines.append(Text("   not installed: no items in .supervisor/battery/ — the nightly run does nothing until they "
                          "are copied in (your decision, 2026-10-13)", style="yellow"))
    else:
        lines.append(Text(f"   night window {b['window']} · every {b['every_days']} night(s) · "
                          f"{'due tonight' if b['due'] else 'not due'}", style="dim"))
    for l in b["lines"]:
        lines.append(Text(f"   {l}"[:width - 1]))
    for r in b["results"][:12]:
        lines.append(Text(f"   {r.get('at', '')[:16]} {r.get('arm', ''):<8} {r.get('item', ''):<28} "
                          f"{r.get('status', ''):<10} {r.get('score', '')}"[:width - 1], style="dim"))
    lines.append(Text(""))
    lines.append(Text(" Blind tests and breeding (shaft distinct / breed)", style="bold underline"))
    evs = panel.evals(name)
    for e in evs:
        d = e["data"]
        if e["kind"] == "distinct":
            s = (f"   {e['at'][:16]} distinct {e['who']}: judge identified {d.get('identified')}/{d.get('n')}, "
                 f"consistent with self {d.get('consistent_with_self')}/{d.get('n')}")
            if d.get("effectiveness_agent") is not None:
                s += f" · effectiveness {d['effectiveness_agent']:.1f} vs base {d.get('effectiveness_base', 0):.1f}"
            if isinstance(d.get("strict"), dict):
                st = d["strict"]
                s += f" · strict {st.get('identified')}/{st.get('n', d.get('n'))}"
        else:
            s = f"   {e['at'][:16]} breed {e['who']} · {len(d.get('attempts') or [])} attempt(s)"
        lines.append(Text(s[:width - 1]))
    if not evs:
        lines.append(Text("   none for this instance", style="dim"))
    lines.append(Text(""))
    lines.append(Text(" Surprises: unexpected tool results (shaft surprise)", style="bold underline"))
    if name:
        s = panel.surprise_summary(name)
        calls = s.get("tool_calls") or {}
        flagged = s.get("flagged") or {}
        for kind in sorted(calls):
            n, f = calls[kind], flagged.get(kind, 0)
            lines.append(Text(f"   {kind:<6} {n:>5} tool calls · {f:>3} unexpected ({f / n:.0%})" if n else
                              f"   {kind:<6} no tool calls"))
        for cls, n in sorted((s.get("flagged_by_class") or {}).items(), key=lambda kv: -kv[1])[:6]:
            lines.append(Text(f"     {n:>3} × {cls}"[:width - 1], style="dim"))
    head = [tab_bar(ui, snap), _inst_head(ui, snap, "benchmarks")]
    return page(head, _scrolled(lines, ui, height, len(head)), ui, HELP["benchmarks"], height)


# -- 6 changes -------------------------------------------------------------------------------------------------
def render_changes(snap, ui, width, height):
    name = current(ui, snap)
    lines: list[Text] = []
    d = panel.instance_detail(name) if name else {"changes": [], "probation": [], "merges": []}
    lines.append(Text(" Kept changes to its own code (passed the test gate)", style="bold underline"))
    for c in d["changes"]:
        who = f" — {len(c['delegated'])} file(s) by its coder" if c["delegated"] else " — by itself"
        lines.append(Text(f"   {c['ts'][:16]} {', '.join(c['files'])}{who}"[:width - 1]))
    if not d["changes"]:
        lines.append(Text("   none", style="dim"))
    lines.append(Text(""))
    lines.append(Text(" On probation now", style="bold underline"))
    for e in d["probation"]:
        trial = (e.get("trial") or {}).get("verdict")
        lines.append(Text(f"   {e['commit'][:8]} since {e['since'][:16]} · {', '.join(e['files'])}"
                          + (f" · trial: {trial}" if trial else "")
                          + (" · RESTORE FAILED: " + e["restore_failed"] if e.get("restore_failed") else "")[:width - 1],
                          style="red" if e.get("restore_failed") else ""))
    if not d["probation"]:
        lines.append(Text("   nothing", style="dim"))
    lines.append(Text(""))
    lines.append(Text(" What probation decided (supervisor log, newest first)", style="bold underline"))
    for e in panel.probation_events(name) if name else []:
        style = "red" if "failed" in e["text"] or "FAILED" in e["text"] else "green" if "passed" in e["text"] else "dim"
        lines.append(Text(f"   {e['ts'][5:16]} {e['text']}"[:width - 1], style=style))
    lines.append(Text(""))
    lines.append(Text(" Template updates merged (shaft update)", style="bold underline"))
    for m in panel.template_merges(name) if name else []:
        lines.append(Text(f"   {m}"[:width - 1], style="dim"))
    head = [tab_bar(ui, snap), _inst_head(ui, snap, "changes")]
    return page(head, _scrolled(lines, ui, height, len(head)), ui, HELP["changes"], height)


# -- 8 costs -------------------------------------------------------------------------------------------------------
def _bar(spent: float, cap: float, width: int = 16) -> Text:
    frac = min(1.0, spent / cap) if cap else 0.0
    n = round(frac * width)
    style = "red" if frac >= panel.CAP_ALERT else "yellow" if frac >= 0.5 else "green"
    t = Text("█" * n, style=style)
    t.append("░" * (width - n), style="dim")
    t.append(f"{' ' if width else ''}${spent:.2f} / ${cap:.2f}", style=style if not width else "")
    return t


def render_costs(snap, ui, width, height):
    c = panel.costs(days=7)
    today = c["today"]
    lines: list = []
    head_t = Text(f" Today ${today['total']:.2f}: ", style="bold")
    parts = [f"instances ${sum(today['instances'].values()):.2f}", f"trials ${today['trials']:.2f}",
             f"judge ${today['judge']:.2f}", f"coder ${today['coder']:.2f}"]
    if today["recaps"]:
        parts.append(f"recaps ${today['recaps']:.2f}")
    if today["other"]:
        parts.append(f"other ${today['other']:.2f}")
    head_t.append(" · ".join(parts))
    lines.append(head_t)
    lines.append(Text(""))
    caps = Table(box=None, padding=(0, 2), show_edge=False, header_style="bold")
    for col in ("instance", "cloud model", "coder", "its trials"):
        caps.add_column(col)
    bar = max(0, min(16, (width - 12) // 3 - 20))         # bars shrink, then go, before the amounts wrap
    for cap in c["caps"]:
        caps.add_row(cap["name"], _bar(*cap["cloud"], bar), _bar(*cap["coder"], bar), _bar(*cap["trials"], bar))
    lines.append(caps)
    lines.append(Text(""))
    jt = Text(f" Judge today ${c['judge_today_total']:.2f} (no cap; alert at ${c['judge_alert']:.2f}): ", style="bold")
    jt.append(" · ".join(f"{k} ${v:.2f}" for k, v in sorted(c["judge_today"].items(), key=lambda kv: -kv[1])),
              style="red" if c["judge_today_total"] >= c["judge_alert"] else "")
    lines.append(jt)
    if c.get("grok_api_equivalent"):
        lines.append(Text(f"   Grok plan: ${c['grok_api_equivalent']:.6f} API equivalent (not money spent)"))
    if c.get("claude_api_equivalent"):
        lines.append(Text(f"   Claude plan: ${c['claude_api_equivalent']:.6f} API equivalent (not money spent)"))
    if (rate := c.get("codex_rate")) is not None:
        lines.append(Text(f"   Codex plan: {rate['used_percent']}% of weekly allowance used"))
    if (rate := c.get("claude_rate")) is not None:
        lines.append(Text(f"   Claude plan: {rate['used_percent']}% of weekly allowance used"))
    if c["unpriced"]:
        lines.append(Text(f"   {c['unpriced']} unpriced model call(s) are not counted in dollar totals", style="yellow"))
    lines.append(Text(""))
    days = Table(box=None, padding=(0, 2), show_edge=False, header_style="bold", title_justify="left")
    for col in ("day", "total", "instances", "trials", "judge", "coder"):
        days.add_column(col, justify="right" if col != "day" else "left")
    for d in reversed(c["days"]):
        days.add_row(d["day"], f"${d['total']:.2f}", f"${sum(d['instances'].values()):.2f}", f"${d['trials']:.2f}",
                     f"${d['judge']:.2f}", f"${d['coder']:.2f}")
    lines.append(Text(" Last 7 days", style="bold underline"))
    lines.append(days)
    return page([tab_bar(ui, snap)], lines, ui, HELP["costs"], height)


# -- 9 services ------------------------------------------------------------------------------------------------------
SERVICE_NAMES = list(dashboard.SERVICES)


def render_services(snap, ui, width, height):
    svcs = {s["name"]: s for s in panel.services_detail()}
    ui.sel = max(0, min(ui.sel, len(SERVICE_NAMES) - 1))
    lines: list = []
    for k, name in enumerate(SERVICE_NAMES):
        s = svcs.get(name, {"running": False, "detail": ""})
        t = Text(no_wrap=True, overflow="ellipsis", style="reverse" if k == ui.sel else "")
        t.append(" ● " if s["running"] else " ○ ", style="green" if s["running"] else "dim" if s.get("idle") else "red")
        t.append(f"{name:<10}", style="bold")
        t.append(f"{'running' if s['running'] else 'idle' if s.get('idle') else 'NOT RUNNING':<12}")
        if s.get("pid"):
            t.append(f"pid {s['pid']:<7}")
        t.append(s.get("detail", ""), style="dim")
        if s.get("stale"):
            t.append("  older code than checked out: restart it", style="yellow")
        lines.append(t)
    runs = panel.scheduled_runs()
    lines.append(Text(""))
    lines.append(Text(f" Runs a scheduler restart would kill: {', '.join(runs) or 'none'}",
                      style="yellow" if runs else "dim"))
    lines.append(Text(""))
    sel = SERVICE_NAMES[ui.sel]
    lines.append(Text(f" {sel} log (.supervisor/{panel.SERVICE_LOGS[sel]})", style="bold underline"))
    tail = panel.log_tail(sel, max(3, height - len(lines) - 4))
    for l in tail or ["(empty)"]:
        lines.append(Text(f"   {l}"[:width - 1], style="dim"))
    return page([tab_bar(ui, snap)], lines, ui, HELP["services"], height)


# -- help -----------------------------------------------------------------------------------------------------------
def render_help(snap, ui, width, height):
    lines = [Text(" Keys", style="bold underline"), Text(""),
             Text("   0–9 switch tab · ? this help (any key closes it) · q quits · Esc goes back")]
    for name, hint in HELP.items():
        lines.append(Text(f"   {name:<11}", style="bold"))
        for w in wrap(hint, width - 18):
            lines.append(Text(f"              {w}", style="dim"))
    lines.append(Text(f"   {'messages':<11}", style="bold"))
    lines.append(Text("              ↑/↓ PgUp/PgDn move · Enter read · f filter · n/p newer/older · Esc back",
                      style="dim"))
    lines += [Text(""), Text(" Every action runs the matching shaft subcommand and is logged as 'top: operator …' in "
                             "supervisor.log; long ones run detached (output in .supervisor/top-actions.log). "
                             "Secrets, new, breed and finetune stay in the CLI.", style="dim")]
    return page([tab_bar(ui, snap)], lines, ui, "any key closes", height)


# -- 0 transcripts ---------------------------------------------------------------------------------------------------
TX_FILTERS = ("", "cycles", "conversations")            # f cycles through them
TX_KIND_STYLE = {"cycle": "green", "conversation": "yellow", "sleep": "blue", "reflection": "magenta"}
_tx_cache: dict = {}


def _tx_shown(ui, name: str) -> list[dict]:
    es = panel.transcripts(name) if name else []
    if ui.tx_filter == "cycles":
        return [e for e in es if e["kind"] != "conversation"]
    if ui.tx_filter == "conversations":
        return [e for e in es if e["kind"] == "conversation"]
    return es


def _tx_lines(name: str, e: dict, width: int, thinking: bool) -> list[Text]:
    """The transcript wrapped to the panel's width, kept while nothing about it changes (it renders every second)."""
    from .top import clean                              # instance-written text: no terminal control characters
    key = (name, e["id"], len(e.get("turns", ())), len(e.get("writeups", ())), width, thinking)
    if key not in _tx_cache:
        if len(_tx_cache) > 16:
            _tx_cache.clear()
        lines = []
        for text, style in panel.transcript_lines(name, e, thinking):
            lines += [Text(f" {w}", style=style) for w in wrap(clean(text), width - 2)]
        _tx_cache[key] = lines
    return _tx_cache[key]


def transcript_line(e: dict, width: int, selected: bool) -> Text:
    from .top import clean
    t = Text(no_wrap=True, overflow="ellipsis", style="reverse" if selected else "")
    t.append(f" {e['ts'][5:16].replace('T', ' ')} ", style="" if selected else "dim")
    t.append(f"{e['kind']:<13}", style="" if selected else TX_KIND_STYLE.get(e["kind"], ""))
    t.append(clean(panel.transcript_summary(e)))
    return t


def render_transcripts(snap, ui, width, height):
    name = current(ui, snap)
    es = _tx_shown(ui, name)
    head = [tab_bar(ui, snap), _inst_head(ui, snap, "transcripts")]
    if ui.detail and es:
        e = es[min(ui.sel, len(es) - 1)]
        lines = _tx_lines(name, e, width, ui.thinking)
        rows = max(1, height - len(head) - 1)
        shown = _scrolled(lines, ui, height, len(head))
        more = f"lines {ui.scroll + 1}–{ui.scroll + len(shown)} of {len(lines)} · " if len(lines) > rows else ""
        think = f" · t thinking {'off' if ui.thinking else 'on'}" if e["kind"] == "conversation" else ""
        return page(head, shown, ui, f"{more}↑/↓ PgUp/PgDn scroll{think} · Esc back", height)
    live = next((i for i in snap.get("instances", []) if i["name"] == name), {})
    info = Text(f" {len(es)} {ui.tx_filter or 'transcripts'} · newest first · f filter", style="dim", no_wrap=True,
                overflow="ellipsis")
    if live.get("state") == "in a cycle":
        info.append(f" · the cycle running now (since {live.get('since', '?')}) appears when it ends", style="yellow")
    head.append(info)
    rows = max(1, height - len(head) - 1)
    ui.sel = max(0, min(ui.sel, len(es) - 1))
    ui.top = scroll_to(ui.sel, ui.top, rows)
    body = [transcript_line(e, width, k == ui.sel) for k, e in enumerate(es[ui.top:ui.top + rows], start=ui.top)]
    return page(head, body or [Text(f" no {ui.tx_filter or 'transcripts'} yet", style="dim")], ui,
                HELP["transcripts"], height)


RENDER = {"instance": render_instance, "judge": render_judge, "trials": render_trials,
          "benchmarks": render_benchmarks, "changes": render_changes, "costs": render_costs,
          "services": render_services, "transcripts": render_transcripts}


# -- keys -------------------------------------------------------------------------------------------------------------
def ask(ui, text: str, run) -> None:
    ui.overlay = Overlay("ask", text, run)


def handle_overlay(ui, key: str) -> None:
    o = ui.overlay
    if o.kind == "ask":
        ui.overlay = None
        if key in ("y", "Y"):
            try:
                ui.say(o.run())
            except Exception as e:
                ui.say(f"not done: {e}")
        else:
            ui.say("cancelled", 3)
    elif o.kind == "input":
        if key == "esc":
            ui.overlay = None
            ui.say("cancelled", 3)
        elif key == "enter":
            ui.overlay = None
            if o.buf.strip():
                try:
                    ui.say(o.run(o.buf.strip()))
                except Exception as e:
                    ui.say(f"not done: {e}")
        elif key == "back":
            o.buf = o.buf[:-1]
        elif len(key) == 1 and key.isprintable():
            o.buf += key
    elif o.kind == "pick":
        if key in ("esc", "q"):
            ui.overlay = None
        elif key in ("up", "k"):
            o.sel = max(0, o.sel - 1)
        elif key in ("down", "j"):
            o.sel = min(len(o.items) - 1, o.sel + 1)
        elif key == "enter" and o.items:
            ui.overlay = None
            o.run(o.items[o.sel][1])


def render_pick(ui, snap, height):
    o = ui.overlay
    lines = [Text(f" {o.text}", style="bold"), Text("")]
    for k, (label, _) in enumerate(o.items):
        lines.append(Text(f"   {label}", style="reverse" if k == o.sel else ""))
    return page([tab_bar(ui, snap)], lines, ui, "↑/↓ choose · Enter · Esc cancels", height)


def instance_action(ui, key: str, snap: dict) -> bool:
    """The instance actions shared by the overview and the instance tab; True if `key` was one."""
    name = current(ui, snap)
    if not name:
        return False
    inst = next((i for i in snap["instances"] if i["name"] == name), {})
    state = inst.get("state", "")
    if key == "p":
        verb = "resume" if state == "paused by you" else "pause"
        ask(ui, f"{verb} {name}?" + (" (a running cycle is asked to stop at its next turn)" if verb == "pause" else ""),
            lambda: run_now([verb, name]))
    elif key == "s":
        if state != "idle":
            ui.say(f"{name} is {state} — a sleep starts only when it is idle")
        else:
            ask(ui, actions.SLEEP_QUESTION.format(name=name), lambda: run_detached(["sleep", name]))
    elif key == "u":
        question, nothing = actions.update_question(name)
        if nothing:
            ui.say(nothing)
        else:
            ask(ui, question, lambda: run_detached(["update", name]))
    elif key == "K":   # a capital: a stray key never does it
        ask(ui, actions.KILL_QUESTION.format(name=name), lambda: run_detached(["kill", name]))
    elif key == "D" and ui.tab == "instance":
        if state not in ("idle", "paused by you"):
            ui.say(f"{name} is {state} — kill it first (K)")
        else:
            def typed(text: str) -> str:
                if text != name:
                    return f"not deleted: you typed {text!r}, not {name}"
                from . import kill
                if left := kill.running(name):
                    return f"not deleted: {name} is busy ({'; '.join(left)}) — kill it first (K)"
                return run_detached(["delete", name, "--yes"])
            ui.overlay = Overlay("input", actions.DELETE_QUESTION.format(name=name) + f" Type {name} to delete it: ",
                                 typed)
    elif key == "w":
        ui.overlay = Overlay("input", f"message to {name}: ", lambda text: run_now(["send", name, text]))
    elif key == "r":
        from . import messages
        open_q = [m for m in messages.pending() if m["from"] == name and m["kind"] == "question"]
        label = (f"reply to {name}'s question ({' '.join(open_q[0]['body'].split())[:60]}…): " if open_q
                 else f"no open question from {name} — message: ")
        ui.overlay = Overlay("input", label, lambda text: run_now(["reply", name, text]))
    elif key == "t" and ui.tab == "instance":
        from .broker import SPECS
        from .core import disabled_tools
        off = disabled_tools(name)
        items = [(f"{'off' if t in off else 'on ':<4} {t}", t) for t in sorted(SPECS)]

        def choose(tool):
            to = "on" if tool in off else "off"
            ask(ui, f"switch {tool} {to} for {name}? (from its next run)", lambda: run_now(["tools", name, to, tool]))
        ui.overlay = Overlay("pick", f"{name}'s supervisor tools — Enter switches one", items=items, run=choose)
    else:
        return False
    return True


def handle_view(ui, key: str, snap: dict, page_rows: int) -> None:
    tab = ui.tab
    names = _names(snap)
    if key in ("left", "h", "right", "l") and names and tab not in ("costs", "services"):
        if not ui.detail:
            ui.inst = (ui.inst + (1 if key in ("right", "l") else -1)) % len(names)
            ui.sel = ui.top = ui.scroll = 0
        return
    if tab == "transcripts" and key == "t":
        ui.thinking = not ui.thinking
        ui.say(f"thinking {'shown' if ui.thinking else 'hidden'} in conversations", 2)
        return
    if tab == "transcripts" and key == "f" and not ui.detail:
        ui.tx_filter = TX_FILTERS[(TX_FILTERS.index(ui.tx_filter) + 1) % len(TX_FILTERS)]
        ui.sel = ui.top = 0
        return
    if ui.detail:
        if key in ("esc", "back"):
            ui.detail, ui.scroll = False, 0
        elif key in ("up", "k"):
            ui.scroll = max(0, ui.scroll - 1)
        elif key in ("down", "j", " "):
            ui.scroll += page_rows if key == " " else 1
        elif key == "pgup":
            ui.scroll = max(0, ui.scroll - page_rows)
        elif key == "pgdn":
            ui.scroll += page_rows
        return
    if tab in ("judge", "trials", "services", "transcripts"):   # lists with a selection
        if key in ("up", "k"):
            ui.sel = max(0, ui.sel - 1)
        elif key in ("down", "j"):
            ui.sel += 1
        elif key == "pgup":
            ui.sel = max(0, ui.sel - page_rows)
        elif key == "pgdn":
            ui.sel += page_rows
        elif key in ("enter",) and tab != "services":
            ui.detail, ui.scroll = True, 0
    else:                                                  # pages that scroll
        if key in ("up", "k"):
            ui.scroll = max(0, ui.scroll - 1)
        elif key in ("down", "j", " "):
            ui.scroll += page_rows if key == " " else 1
        elif key == "pgup":
            ui.scroll = max(0, ui.scroll - page_rows)
        elif key == "pgdn":
            ui.scroll += page_rows
    if tab == "instance":
        instance_action(ui, key, snap)
    elif tab == "trials":
        if key == "f":
            ui.only, ui.sel, ui.top = not ui.only, 0, 0
        elif key == "a" and names:
            name = current(ui, snap)
            ask(ui, actions.aa_trial_question(name),
                lambda: run_now(["trial", "run", name, "--aa", "--hypothesis", "A/A calibration (from shaft top)"]))
    elif tab == "benchmarks" and key == "b" and names:
        name = current(ui, snap)
        why = actions.battery_refusal(name)
        if why:
            ui.say(why)
        else:
            ask(ui, actions.BATTERY_QUESTION.format(name=name), lambda: run_detached(["battery", "run", name]))
    elif tab == "services":
        sel = SERVICE_NAMES[min(ui.sel, len(SERVICE_NAMES) - 1)]
        cmd = "autonomy" if sel == "scheduler" else sel
        if key in ("r", "o"):
            action = "on" if key == "r" else "off"
            guard = action if key == "o" else "restart"
            verb = "start/restart" if key == "r" else "stop"
            running = any(s["name"] == sel and s["running"] for s in snap.get("services", []))
            ask_guarded(ui, sel, guard, f"{verb} {sel}?", [cmd, action], running)
        elif key == "a":
            running = any(s["name"] == "scheduler" and s["running"] for s in snap.get("services", []))
            action = "off" if running else "on"
            ask_guarded(ui, "autonomy", action, f"autonomy {action}? ({'no more scheduled wakes or sleeps' if action == 'off' else 'wakes and the nightly sleep resume'})",
                        ["autonomy", action])


def ask_guarded(ui, service: str, action: str, question: str, args: list[str], running: bool = True) -> None:
    """A service action a guard may hold back: held back, the operator is asked whether to force it (the web UI's
    "Force …" button, the same question), and a forced one is logged. Only a running service can hurt a run: starting
    a stopped one is never held back."""
    why = service_guard(service, action) if running else ""
    if not why:
        ask(ui, question, lambda: run_now(args, timeout=actions.SLOW_S))
        return

    def forced() -> str:
        actions.log_forced("top", args, why)
        return run_now(args + [actions.FORCE], timeout=actions.SLOW_S)   # the CLI holds it back too
    ask(ui, actions.force_question(service, action, why) + " (y to force)", forced)

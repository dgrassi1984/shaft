"""`shaft top`: the supervisor's live view — every instance's state and what it is doing right now, the
services, the latest events and messages. Refreshes every second. Runs with .venv-ui (rich).
Without a terminal (`shaft top --once`, or piped) it prints one snapshot as text.

Keys — overview: ↑/↓ pick an instance, c starts a cycle for it if idle (asks y/n), m browses the messages
between instances, q quits. Messages: ↑/↓ (j/k, PgUp/PgDn) move, f filters by instance, Enter reads one,
Esc goes back. Reading: ↑/↓ scroll, n/p next/previous message, Esc back to the list.
"""
from __future__ import annotations

import os
import re
import select
import sys
import termios
import textwrap
import time
import tty
from dataclasses import dataclass

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import dashboard

STATE_STYLE = {"idle": "dim", "in a cycle": "green", "sleeping": "blue", "talking": "cyan",
               "answering (API)": "cyan", "writing up a conversation": "magenta", "being updated": "yellow"}
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")   # rich passes ESC through: message text is instance-written
KEYS = {"\x1b[A": "up", "\x1bOA": "up", "\x1b[B": "down", "\x1bOB": "down", "\x1b[C": "right", "\x1bOC": "right",
        "\x1b[D": "left", "\x1bOD": "left", "\x1b[5~": "pgup", "\x1b[6~": "pgdn", "\x1b[H": "home", "\x1b[F": "end",
        "\r": "enter", "\n": "enter", "\x7f": "back", "\x08": "back", "\t": "down", "\x1b[Z": "up"}


def clean(s: str) -> str:
    return CONTROL.sub("", s.expandtabs(4))


def _state_style(state: str) -> str:
    if "paused" in state:
        return "yellow"
    return STATE_STYLE.get(state, "white")


def _k(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def _event(line: str) -> Text:
    line = clean(line.split(" cycle_timeout_s=")[0])          # the run's settings: in shaft status, not here
    style = ("red" if any(w in line for w in ("FAILED", "failed", "ERROR", "stopped", "killed")) else
             "yellow" if any(w in line for w in ("paused", "skipped", "reverted")) else
             "green" if any(w in line for w in ("passed", "resumed", "committed live")) else "")
    return Text(line[:200], style=style)


def keys(data: str) -> list[str]:
    """Raw terminal input -> key names ('up', 'enter', 'esc', …) or the typed characters."""
    out, i = [], 0
    while i < len(data):
        for seq in sorted((s for s in KEYS if data.startswith(s, i)), key=len, reverse=True)[:1]:
            out.append(KEYS[seq])
            i += len(seq)
            break
        else:
            if data[i] == "\x1b":
                if data.startswith("\x1b[", i) or data.startswith("\x1bO", i):   # an unknown sequence: skip it
                    j = i + 2
                    while j < len(data) and not ("@" <= data[j] <= "~"):
                        j += 1
                    i = j + 1
                    continue
                out.append("esc")
            else:
                out.append(data[i])
            i += 1
    return out


# -- state ------------------------------------------------------------------------------------------------
@dataclass
class UI:
    view: str = "main"            # main | list | message
    inst: int = 0                 # selected instance (overview)
    msg: int = 0                  # selected message, in the filtered newest-first list
    scroll: int = 0               # reading view
    who: str = ""                 # message filter: an instance (or 'operator'); '' = all
    confirm: str = ""             # instance waiting for y/n before a cycle starts
    flash: str = ""
    flash_until: float = 0.0

    def say(self, text: str, seconds: float = 6) -> None:
        self.flash, self.flash_until = text, time.time() + seconds


def shown(snap: dict, ui: UI) -> list[dict]:
    """The messages the browser lists: newest first, filtered to one participant if a filter is set."""
    ms = [m for m in snap.get("messages", []) if not ui.who or ui.who in (m["from"], m["to"])]
    return ms[::-1]


def participants(snap: dict) -> list[str]:
    return sorted({p for m in snap.get("messages", []) for p in (m["from"], m["to"])})


def follow(ui: UI, old: dict, new: dict) -> None:
    """Keep the browser on the same message when a new one arrives (the list is newest first)."""
    before = shown(old, ui)
    if ui.view == "main" or not before:
        return
    file = before[min(ui.msg, len(before) - 1)]["file"]
    ui.msg = next((k for k, m in enumerate(shown(new, ui)) if m["file"] == file), ui.msg)


def handle(ui: UI, key: str, snap: dict, page: int, start_wake) -> bool:
    """Apply one key; True means quit. start_wake(name) -> str starts a cycle (raises with the reason)."""
    if key == "q" and not ui.confirm:
        return True
    if ui.view == "main":
        names = [i["name"] for i in snap["instances"]]
        if ui.confirm:
            name, ui.confirm = ui.confirm, ""
            if key in ("y", "Y"):
                try:
                    ui.say(start_wake(name))
                except Exception as e:
                    ui.say(f"not started: {e}")
            else:
                ui.say(f"no cycle started for {name}", 3)
        elif key in ("up", "k") and names:
            ui.inst = (ui.inst - 1) % len(names)
        elif key in ("down", "j") and names:
            ui.inst = (ui.inst + 1) % len(names)
        elif key == "c" and names:
            inst = snap["instances"][min(ui.inst, len(names) - 1)]
            if inst["state"] != "idle":
                ui.say(f"{inst['name']} is {inst['state']} — a cycle starts only when it is idle")
            else:
                ui.confirm = inst["name"]
        elif key in ("m", "enter", "right"):
            ui.view, ui.msg = "list", 0
        return False
    ms = shown(snap, ui)
    if ui.view == "list":
        if key in ("esc", "left", "back", "m"):
            ui.view = "main"
        elif key in ("up", "k"):
            ui.msg = max(0, ui.msg - 1)
        elif key in ("down", "j"):
            ui.msg = min(max(0, len(ms) - 1), ui.msg + 1)
        elif key == "pgup":
            ui.msg = max(0, ui.msg - page)
        elif key == "pgdn":
            ui.msg = min(max(0, len(ms) - 1), ui.msg + page)
        elif key in ("home", "g"):
            ui.msg = 0
        elif key in ("end", "G"):
            ui.msg = max(0, len(ms) - 1)
        elif key == "f":
            who = [""] + participants(snap)
            ui.who = who[(who.index(ui.who) + 1) % len(who)] if ui.who in who else ""
            ui.msg = 0
        elif key in ("enter", "right", "l") and ms:
            ui.view, ui.scroll = "message", 0
        return False
    # message
    if key in ("esc", "left", "back", "h"):
        ui.view = "list"
    elif key in ("up", "k"):
        ui.scroll = max(0, ui.scroll - 1)
    elif key in ("down", "j", " "):
        ui.scroll += 1 if key != " " else page
    elif key == "pgup":
        ui.scroll = max(0, ui.scroll - page)
    elif key == "pgdn":
        ui.scroll += page
    elif key == "n":                                  # next = newer
        ui.msg, ui.scroll = max(0, ui.msg - 1), 0
    elif key == "p":
        ui.msg, ui.scroll = min(max(0, len(ms) - 1), ui.msg + 1), 0
    return False


# -- rendering --------------------------------------------------------------------------------------------
def _when(m: dict) -> str:
    return m["ts"][5:16].replace("T", " ")            # MM-DD HH:MM


def _first_line(text: str) -> str:
    return clean(next((l for l in text.splitlines() if l.strip()), "").strip())


def _who(m: dict) -> str:
    return f"{clean(m['from'])} → {clean(m['to'])}"


def _msg_line(m: dict, width: int, selected: bool = False, pad: int = 0) -> Text:
    """● marks a message still pending in the recipient's inbox (it is archived after the run that read it)."""
    t = Text(style="reverse" if selected else "", no_wrap=True, overflow="ellipsis")
    t.append(f"{_when(m)}  ", style="dim" if not selected else "")
    t.append(_who(m).ljust(pad), style="bold")
    t.append(f"  {'●' if not m['read'] else ' '} ", style="cyan")
    if m.get("kind") not in (None, "", "note", "message"):
        t.append(f"{m['kind']} ", style="yellow")
    room = max(10, width - t.cell_len - 1)
    first = _first_line(m["text"])
    t.append(first if len(first) <= room else first[:room - 1] + "…")
    return t


def _footer(ui: UI, hint: str) -> Text:
    if ui.confirm:
        return Text(f" start a cycle for {ui.confirm} now? y/n", style="bold yellow", no_wrap=True)
    if ui.flash and time.time() < ui.flash_until:
        return Text(f" {clean(ui.flash)}", style="bold", no_wrap=True, overflow="ellipsis")
    return Text(f" {hint}", style="dim", no_wrap=True, overflow="ellipsis")


def render_main(snap: dict, ui: UI, width: int = 100):
    head = Text(f" SHAFT · {snap['at']}   ", style="bold")
    for s in snap["services"]:
        head.append("● " if s["running"] else "○ ", style="green" if s["running"] else "red")
        head.append(s["name"], style="bold" if s["running"] else "red")
        if s["detail"]:
            head.append(f" {s['detail']}", style="dim")
        head.append("   ")
    parts = [head, _footer(ui, "↑/↓ instance · c start a cycle (idle only) · m messages · q quits")]
    sel = min(ui.inst, max(0, len(snap["instances"]) - 1))
    for n, i in enumerate(snap["instances"]):
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style="dim", width=6)
        grid.add_column()
        state = Text(i["state"], style=_state_style(i["state"]))
        if i["since"]:
            state.append(f" since {i['since']}", style="dim")
        if i["conversations"] and "talk" not in i["state"] and "conversation" not in i["state"]:
            state.append(f" · {i['conversations']} conversation(s) open", style="cyan")
        grid.add_row("state", state)
        grid.add_row("now", Text(clean(i["doing"]) or "—", style="bold" if i["doing"] else "dim"))
        if i["goal"]:
            grid.add_row("goal", Text(clean(i["goal"])))
        rate = (f" · judged {i['judged']} (achieved {i['achieved']:.1f}/5, success {i['success']:.0%} of the last 10)"
                if i.get("achieved") is not None else
                f" · judged {i['judged']} ({i['success']:.0%} of the last 10)" if i["success"] is not None else "")
        cost = f" · ${i['usd_today']:.2f}" if i["usd_today"] else ""
        grid.add_row("day", Text(f"{i['calls_today']} model calls · {_k(i['tokens_today'])} tokens{cost}{rate}",
                                 style="dim"))
        sched = f"wakes every {i['every']} · next {i['next_wake']}" if i["every"] != "no wakes" else "no scheduled wakes"
        off = f" · off: {', '.join(i['tools_off'])}" if i["tools_off"] else ""
        grid.add_row("plan", Text(f"{sched}{off}", style="dim"))
        mark = "▶ " if n == sel else ""
        parts.append(Panel(grid, title=f"{mark}[bold]{escape(i['name'])}[/bold] · {escape(clean(i['model']))}",
                           title_align="left",
                           border_style="bold " + _state_style(i["state"]) if n == sel else _state_style(i["state"])))
    ms = snap.get("messages", [])
    pending = sum(not m["read"] for m in ms)
    pad = max((len(_who(m)) for m in ms[-5:]), default=0)
    parts.append(Panel(Group(*[_msg_line(m, width - 4, pad=pad) for m in ms[-5:]]) if ms else Text("—", style="dim"),
                       title=f"messages · {len(ms)}{f' · {pending} pending in an inbox' if pending else ''}",
                       title_align="left", border_style="dim"))
    parts.append(Panel(Group(*[_event(l) for l in snap["events"]]) if snap["events"] else Text("—", style="dim"),
                       title="events", title_align="left", border_style="dim"))
    return Group(*parts)                          # a short terminal crops the events, never the prompt line


def render_list(snap: dict, ui: UI, width: int, height: int):
    ms = shown(snap, ui)
    ui.msg = min(ui.msg, max(0, len(ms) - 1))
    rows = max(1, height - 3)
    top = min(max(0, ui.msg - rows // 2), max(0, len(ms) - rows))
    head = Text(f" SHAFT · messages · {snap['at']}   ", style="bold", no_wrap=True, overflow="ellipsis")
    head.append(f"{len(ms)} shown" + (f" · with {ui.who}" if ui.who else " · all") +
                (f" · {ui.msg + 1}/{len(ms)}" if ms else ""), style="dim")
    pad = max((len(_who(m)) for m in ms), default=0)
    lines = [_msg_line(m, width, k == ui.msg, pad) for k, m in enumerate(ms[top:top + rows], start=top)]
    body = Group(*lines) if lines else Text(" no messages yet" + (f" with {ui.who}" if ui.who else ""), style="dim")
    gap = rows - max(1, len(lines))               # blank lines that keep the footer at the bottom
    return Group(head, Text(""), body, *([Text("\n" * (gap - 1))] if gap > 0 else []),
                 _footer(ui, "↑/↓ PgUp/PgDn move · Enter read · f filter by instance · Esc back · q quits"))


def wrap(text: str, width: int) -> list[str]:
    out = []
    for line in clean(text).splitlines() or [""]:
        out.extend(textwrap.wrap(line, width, replace_whitespace=False, drop_whitespace=True) or [""])
    return out


def render_message(snap: dict, ui: UI, width: int, height: int):
    ms = shown(snap, ui)
    if not ms:
        ui.view = "list"
        return render_list(snap, ui, width, height)
    ui.msg = min(ui.msg, len(ms) - 1)
    m = ms[ui.msg]
    to = clean(m["to"])
    status = f"read by {to}" if m["read"] else f"pending in {to}'s inbox"
    head = Text(f" {_who(m)}", style="bold", no_wrap=True, overflow="ellipsis")
    head.append(f"   {m['ts'].replace('T', ' ')} · {m.get('kind') or 'note'} · {status} · {ui.msg + 1}/{len(ms)}",
                style="dim")
    lines = wrap(m["text"], max(20, width - 2))
    rows = max(1, height - 4)
    ui.scroll = min(ui.scroll, max(0, len(lines) - rows))
    view = lines[ui.scroll:ui.scroll + rows]
    more = f"lines {ui.scroll + 1}–{ui.scroll + len(view)} of {len(lines)} · " if len(lines) > rows else ""
    return Group(head, Text(f" {clean(m['file'])}", style="dim", no_wrap=True), Text(""),
                 Text("\n".join(" " + l for l in view) + "\n" * max(0, rows - len(view))),
                 _footer(ui, f"{more}↑/↓ PgUp/PgDn scroll · n/p newer/older · Esc back · q quits"))


def render(snap: dict, ui: UI | None = None, width: int = 100, height: int = 40):
    ui = ui or UI()
    if ui.view == "list":
        return render_list(snap, ui, width, height)
    if ui.view == "message":
        return render_message(snap, ui, width, height)
    return render_main(snap, ui, width)


def plain(snap: dict) -> str:
    lines = [f"SHAFT {snap['at']} · " + " · ".join(f"{s['name']} {'up' if s['running'] else 'DOWN'}"
                                                  for s in snap["services"])]
    for i in snap["instances"]:
        lines.append(f"{i['name']:8} {i['state']}{' since ' + i['since'] if i['since'] else ''}"
                     f"{' · ' + i['doing'] if i['doing'] else ''}{' · goal: ' + i['goal'] if i['goal'] else ''}"
                     f" · next wake {i['next_wake']}")
    msgs = [f"{_when(m)} {m['from']} → {m['to']}{'' if m['read'] else ' (pending)'}: {_first_line(m['text'])[:120]}"
            for m in snap.get("messages", [])[-5:]]
    return clean("\n".join(lines + [""] + msgs + ([""] if msgs else []) +
                           [e.split(" cycle_timeout_s=")[0] for e in snap["events"][-5:]]))


def main(argv: list[str]) -> int:
    if "--once" in argv or not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(plain(dashboard.snapshot()))
        return 0
    from .autonomy import start_wake
    console = Console()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    ui = UI()

    def draw(snap):
        w, h = console.size
        return render(snap, ui, w, h)

    try:
        tty.setcbreak(fd)
        snap, taken = dashboard.snapshot(), time.monotonic()
        with Live(draw(snap), console=console, screen=True, auto_refresh=False) as live:
            while True:
                ready, _, _ = select.select([fd], [], [], max(0.0, 1.0 - (time.monotonic() - taken)))
                if ready:
                    data = os.read(fd, 64).decode(errors="replace")
                    if any(handle(ui, k, snap, max(1, console.size.height - 5), start_wake) for k in keys(data)):
                        break
                if time.monotonic() - taken >= 1.0:
                    new, taken = dashboard.snapshot(), time.monotonic()
                    follow(ui, snap, new)
                    snap = new
                live.update(draw(snap), refresh=True)
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

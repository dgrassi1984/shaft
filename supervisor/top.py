"""`shaft top`: the supervisor's live view — every instance's state and what it is doing right now, the
services, the latest events. Refreshes every second; q (or Ctrl-C) quits. Runs with .venv-ui (rich).
Without a terminal (`shaft top --once`, or piped) it prints one snapshot as text.
"""
from __future__ import annotations

import select
import sys
import termios
import tty

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import dashboard

STATE_STYLE = {"idle": "dim", "in a cycle": "green", "sleeping": "blue", "talking": "cyan",
               "answering (API)": "cyan", "writing up a conversation": "magenta", "being updated": "yellow"}


def _state_style(state: str) -> str:
    if "paused" in state:
        return "yellow"
    return STATE_STYLE.get(state, "white")


def _k(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def _event(line: str) -> Text:
    line = line.split(" cycle_timeout_s=")[0]          # the run's settings: in shaft status, not here
    style = ("red" if any(w in line for w in ("FAILED", "failed", "ERROR", "stopped", "killed")) else
             "yellow" if any(w in line for w in ("paused", "skipped", "reverted")) else
             "green" if any(w in line for w in ("passed", "resumed", "committed live")) else "")
    return Text(line[:200], style=style)


def render(snap: dict):
    head = Text(f" SHAFT · {snap['at']}   ", style="bold")
    for s in snap["services"]:
        head.append("● " if s["running"] else "○ ", style="green" if s["running"] else "red")
        head.append(s["name"], style="bold" if s["running"] else "red")
        if s["detail"]:
            head.append(f" {s['detail']}", style="dim")
        head.append("   ")
    parts = [head]
    for i in snap["instances"]:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style="dim", width=6)
        grid.add_column()
        state = Text(i["state"], style=_state_style(i["state"]))
        if i["since"]:
            state.append(f" since {i['since']}", style="dim")
        if i["conversations"] and "talk" not in i["state"] and "conversation" not in i["state"]:
            state.append(f" · {i['conversations']} conversation(s) open", style="cyan")
        grid.add_row("state", state)
        grid.add_row("now", Text(i["doing"] or "—", style="bold" if i["doing"] else "dim"))
        if i["goal"]:
            grid.add_row("goal", Text(i["goal"]))
        rate = f" · judged {i['judged']} ({i['success']:.0%} of the last 10)" if i["success"] is not None else ""
        cost = f" · ${i['usd_today']:.2f}" if i["usd_today"] else ""
        grid.add_row("day", Text(f"{i['calls_today']} model calls · {_k(i['tokens_today'])} tokens{cost}{rate}",
                                 style="dim"))
        sched = f"wakes every {i['every']} · next {i['next_wake']}" if i["every"] != "no wakes" else "no scheduled wakes"
        off = f" · off: {', '.join(i['tools_off'])}" if i["tools_off"] else ""
        grid.add_row("plan", Text(f"{sched}{off}", style="dim"))
        if i.get("trial"):
            grid.add_row("trial", Text(i["trial"], style="magenta"))
        parts.append(Panel(grid, title=f"[bold]{i['name']}[/bold] · {i['model']}", title_align="left",
                           border_style=_state_style(i["state"])))
    parts.append(Panel(Group(*[_event(l) for l in snap["events"]]) if snap["events"] else Text("—", style="dim"),
                       title="events", title_align="left", border_style="dim"))
    parts.append(Text(" q quits", style="dim"))
    return Group(*parts)


def plain(snap: dict) -> str:
    lines = [f"SHAFT {snap['at']} · " + " · ".join(f"{s['name']} {'up' if s['running'] else 'DOWN'}"
                                                  for s in snap["services"])]
    for i in snap["instances"]:
        lines.append(f"{i['name']:8} {i['state']}{' since ' + i['since'] if i['since'] else ''}"
                     f"{' · ' + i['doing'] if i['doing'] else ''}{' · goal: ' + i['goal'] if i['goal'] else ''}"
                     f" · next wake {i['next_wake']}" + (f"\n{'':8} trial {i['trial']}" if i.get("trial") else ""))
    return "\n".join(lines + [""] + [e.split(" cycle_timeout_s=")[0] for e in snap["events"][-5:]])


def main(argv: list[str]) -> int:
    if "--once" in argv or not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(plain(dashboard.snapshot()))
        return 0
    console = Console()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        with Live(render(dashboard.snapshot()), console=console, screen=True, auto_refresh=False) as live:
            while True:
                ready, _, _ = select.select([sys.stdin], [], [], 1.0)
                if ready and sys.stdin.read(1).lower() == "q":
                    break
                live.update(render(dashboard.snapshot()), refresh=True)
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

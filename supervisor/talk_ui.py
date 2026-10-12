"""The terminal client for `shaft talk` — runs with .venv-ui (prompt_toolkit + rich).

The agent runs sandboxed and headless (`talk --json`); this client edits your input and renders
its events. It is UI only: the supervisor's runner still owns the sandbox, the broker, the run
lock, the gate and the commit. The instance is held only while it replies — while this waits for
you, the API and journal write-ups can use it. A conversation quiet for SHAFT_IDLE_MINUTES closes
itself and is written up.

Keys: Enter sends · Shift-Enter / Option-Enter / Ctrl-J new line · ↑/↓ history
      Ctrl-C stops a reply; at the prompt it clears your text, or ends the conversation if empty
      Ctrl-D ends the conversation
"""
from __future__ import annotations

import json
import os
import uuid
import queue
import signal
import sys
import termios
import threading
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

from . import messages, version
from .core import IDLE_MINUTES, SUP_DIR, instance_root
from .runner import run_conversation, spawn_recover

# Shift-Enter as sent by terminals that report modifiers (iTerm2 "CSI u", kitty, xterm
# modifyOtherKeys) — map it to Ctrl-J, which inserts a new line. iTerm2's other common setups send
# ESC+CR (Option-Enter / Shift-Enter mapped to "Esc+") or a bare LF: both are bound below too.
for seq in ("\x1b[13;2u", "\x1b[27;2;13~", "\x1b[13;3u", "\x1b[27;3;13~"):
    ANSI_SEQUENCES[seq] = Keys.ControlJ

HELP = """\
**Keys** — Enter sends · **Shift-Enter**, **Option-Enter** or **Ctrl-J** new line · ↑/↓ history · \
Ctrl-C stops a reply (at the prompt: clears your text, or ends if empty) · Ctrl-D ends

**Commands** — `/thinking` show or hide its reasoning · `/status` how it is doing · `/version` which code is running · `/inbox` messages \
it sent you · `/clear` clear the screen · `/exit` end

*Shift-Enter in iTerm2:* if it sends instead of adding a line, turn on Settings → Profiles → Keys → \
General → "Report modifiers using CSI u" (or use Option-Enter / Ctrl-J)."""


class Client:
    def __init__(self, name: str, show_thinking: bool):
        self.name, self.show_thinking = name, show_thinking
        self.console = Console(highlight=False)
        self.events: queue.Queue = queue.Queue()
        self.proc = None
        self.info: dict = {}
        self.interrupts: list[float] = []
        self.quit = False
        self.prompting, self.idle_closed, self.last_activity = False, False, time.monotonic()
        SUP_DIR.mkdir(exist_ok=True)
        self.session = PromptSession(history=FileHistory(str(SUP_DIR / f"talk-history-{name}")),
                                     multiline=True, key_bindings=self._keys(), prompt_continuation=self._cont,
                                     bottom_toolbar=self._toolbar, enable_history_search=False)
        self.session.default_buffer.on_text_changed += lambda _: self._touch()

    def _touch(self) -> None:
        self.last_activity = time.monotonic()

    def _idle_watch(self) -> None:
        """Close a conversation left quiet at the prompt (nothing typed) for IDLE_MINUTES."""
        while not self.quit:
            time.sleep(5)
            app = self.session.app
            if (IDLE_MINUTES > 0 and self.prompting and app.is_running
                    and time.monotonic() - self.last_activity > IDLE_MINUTES * 60):
                self.idle_closed = True
                app.loop.call_soon_threadsafe(lambda: app.exit(exception=EOFError()))
                return

    # -- input ---------------------------------------------------------------------
    def _keys(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")
        def _(event):
            buf = event.current_buffer
            if buf.text.strip():
                buf.validate_and_handle()

        @kb.add("escape", "enter")     # Option-Enter; iTerm2 Shift-Enter mapped to ESC+CR
        @kb.add("c-j")                 # Ctrl-J; LF; CSI-u Shift-Enter (mapped above)
        def _(event):
            event.current_buffer.insert_text("\n")

        @kb.add("c-c")
        def _(event):
            buf = event.current_buffer
            if buf.text:
                buf.reset()
            else:
                event.app.exit(exception=EOFError())

        return kb

    @staticmethod
    def _cont(width, line_number, wrap_count):
        return " " * (width - 2) + "… "

    def _toolbar(self):
        model = self.info.get("model", "…").split("/")[-1]
        return HTML(f" <b>{self.name}</b> · {model} · thinking {'shown' if self.show_thinking else 'hidden'}"
                    f"   ⏎ send · ⇧⏎ / ⌥⏎ new line · /help")

    # -- the agent's events ----------------------------------------------------------
    def _reader(self) -> None:
        for line in self.proc.stdout:
            try:
                self.events.put(json.loads(line))
            except ValueError:
                self.events.put({"type": "stderr", "text": line})
        self.events.put({"type": "eof"})

    def _drain_stderr(self) -> None:   # keep the pipe from filling; show the tail if the agent dies
        self.stderr_tail = ""
        for line in self.proc.stderr:
            self.stderr_tail = (self.stderr_tail + line)[-3000:]

    def _send(self, **msg) -> None:
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _wait_for(self, *types: str, timeout: float | None = None) -> dict:
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            ev = self.events.get(timeout=left)
            if ev["type"] in types or ev["type"] == "eof":
                return ev

    # -- one reply ----------------------------------------------------------------------
    def _on_sigint(self, *_):
        now = time.monotonic()
        self.interrupts = [t for t in self.interrupts if now - t < 2] + [now]
        if len(self.interrupts) >= 2:
            self.quit = True               # second Ctrl-C within 2 s: end after this reply stops
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGINT)   # stops the reply in progress

    def _reply(self) -> None:
        content, reasoning = "", ""
        phase, detail, t0 = "waiting for the model", "", time.monotonic()
        first_token = None
        spinner = Spinner("dots", style="dim")

        def view():
            parts = []
            if self.show_thinking and reasoning:
                parts.append(Text(reasoning[-1200:], style="dim italic"))
            if content:
                parts.append(Text(f"{self.name} ›", style="bold cyan"))
                parts.append(Markdown(content, code_theme="monokai"))
            if phase:
                secs = time.monotonic() - t0
                speed = ""
                if first_token and content:
                    speed = f" · {len(content) / 4 / max(time.monotonic() - first_token, 0.1):.0f} tok/s"
                spinner.text = Text(f"{phase}{' · ' + detail if detail else ''} · {secs:.0f}s{speed}", style="dim")
                parts.append(spinner)
            return Group(*parts)

        old = signal.signal(signal.SIGINT, self._on_sigint)
        saved_tty = self._quiet_tty()   # keystrokes and ^C must not be echoed into the streaming reply
        try:
            with Live(view(), console=self.console, refresh_per_second=10, transient=False) as live:
                while True:
                    try:
                        ev = self.events.get(timeout=0.1)
                    except queue.Empty:
                        live.update(view())
                        continue
                    t = ev["type"]
                    if t == "status":
                        phase, detail = ev["phase"], ev.get("detail", "")
                    elif t == "reasoning":
                        reasoning += ev["text"]
                        if phase != "thinking" or not self.show_thinking:
                            phase, detail = "thinking", f"~{ev.get('total_chars', len(reasoning)) / 4000:.1f}k tokens"
                    elif t == "content":
                        if first_token is None:
                            first_token = time.monotonic()
                        content += ev["text"]
                        if "</think>" in content:   # thinking that ran past the budget, closed late: not the answer
                            late, _, content = content.rpartition("</think>")
                            reasoning += late
                            content = content.lstrip()
                        phase, detail = "", ""
                    elif t == "tool_start":
                        if content:                      # text before a tool call stays on screen
                            live.console.print(Text(f"{self.name} ›", style="bold cyan"), Markdown(content))
                            content = ""
                        phase, detail = f"running {ev['name']}", ev.get("args", "")[:60]
                    elif t == "tool_end":
                        mark = "[green]✓[/green]" if ev["ok"] else "[red]✗[/red]"
                        live.console.print(f"  {mark} [dim]{ev['name']}  {ev.get('args', '')[:90]}[/dim]")
                        phase, detail, reasoning = "waiting for the model", "", ""
                    elif t == "retry":
                        live.console.print(f"  [dim]reply cut off at {ev['old']} tokens — asking again with "
                                           f"{ev['new']}[/dim]")
                        content, reasoning, first_token = "", "", None
                    elif t in ("turn_end", "interrupted", "error", "eof"):
                        phase = ""
                        live.update(view())
                        if t == "turn_end" and ev.get("ended_by") != "answer":
                            live.console.print(f"  [dim]stopped after {ev['steps']} tool rounds without a "
                                               f"final answer[/dim]")
                        elif t == "interrupted":
                            live.console.print("  [dim]stopped — that reply is discarded[/dim]")
                        elif t == "error":
                            live.console.print(f"  [red]error:[/red] {ev['message']}\n  [dim]that message was "
                                               f"not kept; try again[/dim]")
                        elif t == "eof":
                            live.console.print("  [red]the agent stopped unexpectedly[/red]\n"
                                               f"[dim]{getattr(self, 'stderr_tail', '')[-800:]}[/dim]")
                            self.quit = True
                        break
                    live.update(view())
        finally:
            signal.signal(signal.SIGINT, old)
            self._restore_tty(saved_tty)
        self.console.print()

    @staticmethod
    def _quiet_tty():
        try:
            fd = sys.stdin.fileno()
            saved = termios.tcgetattr(fd)
            quiet = termios.tcgetattr(fd)
            quiet[3] &= ~(termios.ECHO | termios.ECHOCTL)
            termios.tcsetattr(fd, termios.TCSANOW, quiet)
            return saved
        except (termios.error, ValueError, OSError):
            return None

    @staticmethod
    def _restore_tty(saved) -> None:
        if saved is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, saved)   # also drops typed-ahead junk
            except (termios.error, ValueError, OSError):
                pass

    # -- commands --------------------------------------------------------------------------
    def _command(self, text: str) -> bool:
        """Handle a /command; returns False when the conversation should end."""
        cmd = text.split()[0].lower()
        if cmd in ("/exit", "/quit", "/bye"):
            return False
        if cmd == "/help":
            self.console.print(Markdown(HELP))
        elif cmd == "/thinking":
            self.show_thinking = not self.show_thinking
            self.console.print(f"[dim]thinking {'shown' if self.show_thinking else 'hidden'}[/dim]")
        elif cmd == "/clear":
            self.console.clear()
        elif cmd == "/inbox":
            pending = [m for m in messages.pending() if m["from"] == self.name] or messages.pending()
            if not pending:
                self.console.print("[dim]no messages from instances[/dim]")
            for m in pending:
                self.console.print(f"[bold]{m['kind']}[/bold] from {m['from']} · {m['date']}")
                self.console.print(Markdown(m["body"]))
        elif cmd == "/version":
            self.console.print(version.describe(self.name))
        elif cmd == "/status":
            from .__main__ import cmd_status
            cmd_status([self.name])
        else:
            self.console.print(f"[dim]unknown command {cmd} — /help[/dim]")
        return True

    # -- the conversation --------------------------------------------------------------------
    def drive(self, proc, turn) -> None:
        """turn(): the runner's hold on the instance — taken for start-up and for each reply only."""
        self.proc = proc
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        with turn():
            ready = self._wait_for("ready", timeout=120)
        if ready["type"] != "ready":
            time.sleep(0.5)
            self.console.print(f"[red]the agent did not start[/red]\n{getattr(self, 'stderr_tail', '')[-1500:]}")
            return
        self.info = ready
        self.console.print(f"[bold cyan]{self.name}[/bold cyan] [dim]· {ready['model'].split('/')[-1]} · "
                           f"{ready['journal_entries']} journal entries · {ready['tools']} tools · /help[/dim]")
        self.console.print(f"[dim]{version.describe(self.name)}[/dim]\n")
        threading.Thread(target=self._idle_watch, daemon=True).start()
        while not self.quit:
            self.prompting, self.last_activity = True, time.monotonic()
            try:
                text = self.session.prompt(HTML("<ansicyan><b>you ›</b></ansicyan> "))
            except (EOFError, KeyboardInterrupt):
                break
            finally:
                self.prompting = False
            text = text.strip()
            if not text:
                continue
            if text.startswith("/") and "\n" not in text:
                if not self._command(text):
                    break
                continue
            try:
                with turn(on_wait=lambda who: self.console.print(
                        f"[dim]{self.name} is busy ({who}) — your message goes as soon as it is free…[/dim]")):
                    self._send(type="user", text=text)
                    self._reply()
            except KeyboardInterrupt:
                self.console.print("[dim]not sent[/dim]")
        if self.idle_closed:
            self.console.print(f"\n[dim]closed after {IDLE_MINUTES} quiet minutes (SHAFT_IDLE_MINUTES).[/dim]")
        self.quit = True
        self._send(type="exit")
        bye = self._wait_for("bye", timeout=20) if proc.poll() is None else {}
        if bye.get("journal_pending"):
            self.console.print("[dim]bye — I'll write my journal entry about this in the background.[/dim]")
        try:
            proc.stdin.close()
        except OSError:
            pass


def main(argv: list[str]) -> int:
    name = argv[0] if argv and not argv[0].startswith("-") else "shaft"
    show_thinking = "--show-thinking" in argv
    instance_root(name)
    client = Client(name, show_thinking)
    talk_id = uuid.uuid4().hex[:12]
    code = run_conversation(name, "talk", ["talk", "--json", "--talk-id", talk_id], talk_id, client.drive)
    spawn_recover(name)   # after the conversation is closed: it is no longer "open", so it gets written up
    return 0 if code in (0, None) else code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

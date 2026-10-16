"""The keys `shaft talk` reads itself — no prompt_toolkit or rich here, so it is tested on plain bytes.

Shift-Enter: a terminal sends Enter and Shift-Enter as the same byte (CR) unless the app asks it to report
modifiers. talk asks with xterm's modifyOtherKeys level 1 (REPORT_KEYS_ON) — measured on iTerm2 3.7.6, 2026-10-16:
Shift-Enter then sends ESC[27;2;13~ while Ctrl-C stays ^C (so it still stops a reply) and Esc stays ESC; the
kitty protocol would also re-encode Ctrl-C and Esc. Terminals already in kitty mode send ESC[13;<mod>u.

Cmd-Enter: terminals keep it for themselves (iTerm2 and Ghostty: full screen) and send nothing. iTerm2 sends
it once a key binding maps ⌘↩ to the escape sequence [13;9u — the kitty encoding of Super+Enter (QUEUE_SEQ).

Draft: what you type while a reply streams. Enter or Cmd-Enter queues the message — it is locked, sent as soon as the
reply ends, and what you type next is a new one. Shift-Enter or Ctrl-J adds a line, Backspace deletes, Ctrl-U clears
the one you are typing, Esc takes the queued ones back to edit. What is not queued is put back at the prompt.
"""
from __future__ import annotations

import codecs
import re

REPORT_KEYS_ON, REPORT_KEYS_OFF = "\x1b[>4;1m", "\x1b[>4;0m"      # xterm modifyOtherKeys 1 / off
PASTE_ON, PASTE_OFF = "\x1b[?2004h", "\x1b[?2004l"                 # bracketed paste: a pasted CR is a new line
QUEUE_SEQ = "\x1b[13;9u"
SUPER = 8                                                          # modifier bits: shift 1, alt 2, ctrl 4, super 8


def enter_sequences() -> dict[str, str]:
    """Every modified Enter a terminal may send, as "newline" or "queue" (any combination with Cmd/Super)."""
    out = {"\x1b\r": "newline"}                                    # Option-Enter where Option sends Esc+
    for mod in range(2, 17):
        kind = "queue" if (mod - 1) & SUPER else "newline"
        out[f"\x1b[27;{mod};13~"] = out[f"\x1b[13;{mod}u"] = kind
    return out


def other_modified_keys() -> list[str]:
    """modifyOtherKeys sequences that are not Enter (Ctrl-Tab, Ctrl-1, …): talk has no use for them, and an
    unknown sequence would otherwise be typed into the message as text."""
    return [f"\x1b[27;{mod};{c}~" for mod in range(2, 17) for c in (9, 27, 127, *range(32, 127))]


ENTER = enter_sequences()
CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"


def _lines(pasted: str) -> str:
    return pasted.replace("\r\n", "\n").replace("\r", "\n")


class Draft:
    def __init__(self):
        self.text, self.queue = "", []                             # being typed · locked, to send
        self._pending = ""                                         # an escape sequence cut by a read
        self._paste = False
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, data: bytes) -> None:
        s = self._pending + self._utf8.decode(data)
        self._pending = ""
        i = 0
        while i < len(s):
            if self._paste:
                end = s.find(PASTE_END, i)
                if end < 0:                                        # keep a tail that may be the end marker, cut
                    cut = next((k for k in range(len(PASTE_END) - 1, 0, -1) if s.endswith(PASTE_END[:k])), 0)
                    self.text += _lines(s[i:len(s) - cut])
                    self._pending = s[len(s) - cut:] if cut else ""
                    return
                self.text += _lines(s[i:end])
                self._paste, i = False, end + len(PASTE_END)
                continue
            c = s[i]
            if c == "\x1b":
                if i + 1 == len(s):                                # a lone ESC at the end of a read: Esc, or the
                    self._pending = c                              # start of a sequence the next read completes
                    return
                if s[i + 1] == "\r":
                    self._enter("newline")
                    i += 2
                    continue
                if s[i + 1] != "[":
                    self.unqueue()                                 # Esc
                    i += 1
                    continue
                m = CSI.match(s, i)
                if not m:                                          # a sequence still arriving
                    self._pending = s[i:]
                    return
                seq = m.group()
                if seq == PASTE_START:
                    self._paste = True
                elif seq in ENTER:
                    self._enter(ENTER[seq])
                elif seq == "\x1b[27u":                            # Esc in kitty mode
                    self.unqueue()
                i = m.end()
                continue
            if c == "\r":
                self._enter("queue")
            elif c == "\n":
                self._enter("newline")
            elif c in ("\x7f", "\x08"):
                self.text = self.text[:-1]
            elif c == "\x15":                                      # Ctrl-U: clear the one being typed
                self.text = ""
            elif c >= " " or c == "\t":
                self.text += c
            i += 1

    def flush(self) -> None:
        """At the end of the reply: a lone ESC still waiting is the Esc key."""
        if self._pending == "\x1b":
            self.unqueue()
        self._pending = ""

    @property
    def queued(self) -> bool:
        return bool(self.queue)

    def unqueue(self) -> None:
        """The queued messages back into the one being typed, to edit or to send from the prompt."""
        self.text = "\n\n".join(self.queue + ([self.text] if self.text.strip() else []))
        self.queue = []

    def _enter(self, kind: str) -> None:
        if kind == "newline":
            self.text += "\n"
        elif self.text.strip():
            self.queue.append(self.text.strip("\n"))
            self.text = ""

    def take(self) -> tuple[str, bool]:
        """(the queued messages as one, True) — what is still being typed stays for the next take; or, with
        nothing queued, (what is being typed, False) for the prompt. Either way it is gone from the draft."""
        if self.queue:
            text, self.queue = "\n\n".join(self.queue), []
            return text, True
        text, self.text = self.text.strip("\n"), ""
        return text, False

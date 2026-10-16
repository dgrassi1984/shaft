"""The talk client's keys (Daniele 2026-10-16: "Shift-enter should add a new line to the message, not submit ·
Command-enter should queue the message").

Regression: iTerm2 sends Shift-Enter as CR — the very byte Enter sends — unless the app asks it to report
modifiers, so Shift-Enter submitted. The byte sequences below are the ones measured on iTerm2 3.7.6 (talk_input.py).
The prompt and client tests need prompt_toolkit and rich: .venv-ui/bin/python -m unittest supervisor.tests.test_talk_input
"""
from __future__ import annotations

import io
import json
import threading
import unittest
from unittest import mock

from supervisor import talk_input
from supervisor.talk_input import QUEUE_SEQ, Draft

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from rich.console import Console

    from supervisor import talk_ui
except ModuleNotFoundError:
    talk_ui = None

SHIFT_ENTER = b"\x1b[27;2;13~"          # iTerm2 with modifyOtherKeys 1 (what talk turns on)
SHIFT_ENTER_KITTY = b"\x1b[13;2u"
CMD_ENTER = QUEUE_SEQ.encode()           # iTerm2 once ⌘↩ is bound to "Send Escape Sequence" [13;9u


def fed(*chunks: bytes) -> Draft:
    d = Draft()
    for c in chunks:
        d.feed(c)
    return d


class DraftKeys(unittest.TestCase):
    def test_shift_enter_adds_a_line_and_does_not_queue(self):
        for seq in (SHIFT_ENTER, SHIFT_ENTER_KITTY, b"\n", b"\x1b\r"):
            d = fed(b"hi", seq, b"there")
            self.assertEqual((d.text, d.queued), ("hi\nthere", False), seq)

    def test_enter_and_cmd_enter_queue(self):
        for seq in (b"\r", CMD_ENTER, b"\x1b[27;9;13~"):
            d = fed(b"next question", seq)
            self.assertEqual(d.take(), ("next question", True), seq)
            self.assertEqual((d.text, d.queued), ("", False))          # take() empties it

    def test_enter_on_nothing_queues_nothing(self):
        self.assertEqual(fed(b"\r").take(), ("", False))
        self.assertEqual(fed(b"\n\n", CMD_ENTER).take(), ("", False))

    def test_esc_unqueues_and_backspace_and_ctrl_u_edit(self):
        d = fed(b"abc\r", b"\x1b", b"x")                               # Esc (cut from the next read) unqueues
        self.assertEqual((d.text, d.queued), ("abcx", False))
        d = fed(b"abc\r")
        d.feed(b"\x1b")
        d.flush()                                                      # a lone ESC at the end is the Esc key
        self.assertFalse(d.queued)
        self.assertEqual(fed(b"abcd\x7f\x7f").text, "ab")
        d = fed(b"abc\rdef\x15")                                      # Ctrl-U clears the one being typed only
        self.assertEqual((d.take(), d.take()), (("abc", True), ("", False)))

    def test_typing_after_queueing_is_a_new_message_and_only_queued_ones_are_sent(self):
        """Found driving the real client in iTerm2: a message typed after queueing was glued onto the queued one
        ("queued by enterqueued by cmd") and, half typed, would have gone out when the reply ended."""
        d = fed(b"first\r", b"second, half typed")
        self.assertEqual(d.take(), ("first", True))
        self.assertEqual(d.take(), ("second, half typed", False))      # back at the prompt, not sent
        self.assertEqual(fed(b"a\r", CMD_ENTER, b"b", CMD_ENTER).take(), ("a\n\nb", True))
        d = fed(b"a\rb\rc", b"\x1b", b"!")                            # Esc: the queued ones back, to edit
        self.assertEqual(d.take(), ("a\n\nb\n\nc!", False))

    def test_a_sequence_or_a_character_cut_between_reads_is_whole(self):
        d = fed(b"hi\x1b[27;", b"2;13~ok")
        self.assertEqual((d.text, d.queued), ("hi\nok", False))
        euro = "€".encode()
        self.assertEqual(fed(euro[:1], euro[1:]).text, "€")

    def test_a_pasted_line_break_is_a_new_line_not_a_queue(self):
        d = fed(b"see:\x1b[200~line one\rline two\r\n", b"line three\x1b[20", b"1~!")
        self.assertEqual((d.text, d.queued), ("see:line one\nline two\nline three!", False))

    def test_other_keys_and_sequences_are_not_typed(self):
        d = fed(b"a\x1b[A\x1b[27;5;9~\x04b")                         # an arrow, Ctrl-Tab, Ctrl-D
        self.assertEqual(d.text, "ab")

    def test_every_cmd_enter_queues_and_no_other_enter_does(self):
        kinds = talk_input.ENTER
        self.assertEqual(kinds["\x1b[27;2;13~"], "newline")
        self.assertEqual(kinds[QUEUE_SEQ], "queue")
        self.assertEqual(kinds["\x1b[13;10u"], "queue")              # Cmd-Shift-Enter
        self.assertEqual(kinds["\x1b[13;5u"], "newline")             # Ctrl-Enter
        self.assertFalse(set(kinds) & set(talk_input.other_modified_keys()))


@unittest.skipIf(talk_ui is None, "needs .venv-ui (prompt_toolkit, rich)")
class PromptKeys(unittest.TestCase):
    """The prompt itself, through prompt_toolkit's own parser and the client's key bindings."""

    def submit(self, data: bytes) -> str:
        with create_pipe_input() as inp:
            inp.send_bytes(data)
            inp.close()                     # a key that does not submit is an EOFError, not a test that hangs
            client = object.__new__(talk_ui.Client)
            session = PromptSession(input=inp, output=DummyOutput(), multiline=True, key_bindings=client._keys())
            return session.prompt("you › ")

    def test_shift_enter_is_a_new_line_at_the_prompt(self):
        self.assertEqual(self.submit(b"hi" + SHIFT_ENTER + b"there\r"), "hi\nthere")
        self.assertEqual(self.submit(b"hi" + SHIFT_ENTER_KITTY + b"there\r"), "hi\nthere")

    def test_cmd_enter_sends_at_the_prompt_and_other_modified_keys_are_not_typed(self):
        self.assertEqual(self.submit(b"hi" + CMD_ENTER), "hi")
        self.assertEqual(self.submit(b"a\x1b[27;5;9~b\r"), "ab")


    def test_the_conversation_asks_the_terminal_to_report_shift_enter_and_stops_on_every_exit(self):
        """The bug itself: without REPORT_KEYS_ON iTerm2 sends Shift-Enter as CR, the byte Enter sends."""
        client = object.__new__(talk_ui.Client)
        at_prompt = []
        for outcome in (None, RuntimeError("the agent died")):
            out = io.StringIO()

            def drive(proc, turn, outcome=outcome):
                at_prompt.append(out.getvalue())
                if outcome:
                    raise outcome

            with mock.patch.object(talk_ui.sys, "stdout", out), mock.patch.object(client, "_drive", drive):
                try:
                    client.drive(None, None)
                except RuntimeError:
                    pass
            self.assertEqual(at_prompt[-1], talk_input.REPORT_KEYS_ON)
            self.assertTrue(out.getvalue().endswith(talk_input.REPORT_KEYS_OFF))

    def test_while_it_replies_enter_reaches_the_draft_as_cr_and_ctrl_c_still_stops_it(self):
        """Found driving the real client in iTerm2: with ICRNL left on, the tty turned Enter's CR into LF — a new
        line — so two queued messages went out as one ("queued by enter\\nqueued by cmd")."""
        import os
        import pty
        import termios
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        before = termios.tcgetattr(slave)
        stdin, out = mock.Mock(fileno=lambda: slave), io.StringIO()
        with mock.patch.object(talk_ui.sys, "stdin", stdin), mock.patch.object(talk_ui.sys, "stdout", out):
            saved = talk_ui.Client._quiet_tty()
            os.write(master, b"one\rtwo" + CMD_ENTER)
            d = Draft()
            d.feed(os.read(slave, 64))
            mode = termios.tcgetattr(slave)
            talk_ui.Client._restore_tty(saved)
        self.assertEqual(d.take(), ("one\n\ntwo", True))                # two messages, not one with a line break
        self.assertTrue(mode[3] & termios.ISIG)                        # Ctrl-C is still SIGINT
        self.assertFalse(mode[3] & (termios.ECHO | termios.ICANON))
        after = termios.tcgetattr(slave)
        after[3] &= ~getattr(termios, "PENDIN", 0)                     # set by the kernel itself on the way back
        self.assertEqual(after, before)                                # and the prompt gets its tty back
        self.assertEqual(out.getvalue(), talk_input.PASTE_ON + talk_input.PASTE_OFF)

class _Proc:
    def __init__(self, events):
        self.stdout = iter(json.dumps(e) + "\n" for e in events)
        self.stderr = iter(())
        self.stdin = mock.MagicMock()
        self.pid = 0

    def poll(self):
        return 0


@unittest.skipIf(talk_ui is None, "needs .venv-ui (prompt_toolkit, rich)")
class QueuedMessages(unittest.TestCase):
    def client(self, events):
        c = object.__new__(talk_ui.Client)
        c.name, c.show_thinking, c.info, c.interrupts = "wren", False, {}, []
        c.console = Console(file=io.StringIO(), width=100)
        c.events, c.proc, c.quit = talk_ui.queue.Queue(), None, False
        c.prompting, c.idle_closed, c.last_activity = False, False, 0.0
        c.stats, c.draft = talk_ui.Stats(), Draft()
        c.session = mock.Mock()
        c.session.prompt.side_effect = EOFError
        c.sent = []
        c._send = lambda **m: c.sent.append(m)
        c._idle_watch = lambda: None
        proc = _Proc([{"type": "ready", "model": "m", "journal_entries": 0, "tools": 1}, *events])
        return c, proc

    def drive(self, c, proc):
        turn = mock.MagicMock()
        with mock.patch.object(talk_ui.version, "describe", return_value=""):
            c._drive(proc, turn)

    def test_a_message_queued_during_a_reply_is_sent_when_it_ends_without_a_prompt(self):
        c, proc = self.client([{"type": "turn_end", "ended_by": "answer", "steps": 1}])
        c.draft.feed(b"and the second thing?" + CMD_ENTER)              # typed while the last reply streamed
        self.drive(c, proc)
        self.assertEqual(c.sent[0], {"type": "user", "text": "and the second thing?"})
        c.session.history.append_string.assert_called_once_with("and the second thing?")
        self.assertIn("you › and the second thing?", c.console.file.getvalue())

    def test_stopping_the_reply_puts_the_queued_text_back_at_the_prompt(self):
        c, _ = self.client([])
        sent = threading.Event()
        c._send = lambda **m: (c.sent.append(m), sent.set())

        def agent():                                                   # you queue "then this" mid-reply, then stop it
            yield json.dumps({"type": "ready", "model": "m", "journal_entries": 0, "tools": 1}) + "\n"
            sent.wait(5)
            c.draft.feed(b"then this\r")
            yield json.dumps({"type": "interrupted"}) + "\n"

        proc = _Proc([])
        proc.stdout = agent()
        c.session.prompt.side_effect = ["first", EOFError]
        self.drive(c, proc)
        self.assertEqual(c.sent[0], {"type": "user", "text": "first"})
        self.assertNotIn({"type": "user", "text": "then this"}, c.sent)
        self.assertEqual(c.session.prompt.call_args.kwargs["default"], "then this")

if __name__ == "__main__":
    unittest.main()

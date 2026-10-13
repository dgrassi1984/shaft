"""`shaft top` shows the messages between instances and starts a cycle for an idle one (Daniele, 2026-10-13:
"see messages exchanges between instances (and their content) and browse them"; "support triggering the
cycle for a idle instance").

Before, the only supervisor-side trace of a message was a 160-char line in broker.log; the text lived only in
the instances' own (instance-controlled) inbox/ and memory/messages/ folders.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import autonomy, core, messages
from supervisor.broker import Broker

try:
    from supervisor import top
except ModuleNotFoundError:   # rich lives in .venv-ui
    top = None


class MessageLogTest(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-proj-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        self.sup = self.project / ".supervisor"
        for name in ("wren", "shaft"):
            (self.project / "instances" / name / "shaft").mkdir(parents=True)
            (self.project / "instances" / name / ".git").write_text("gitdir: x")
        for p in (mock.patch.object(messages, "SUP_DIR", self.sup),
                  mock.patch.object(messages, "INSTANCES", self.project / "instances"),
                  mock.patch.object(messages, "OPERATOR_INBOX", self.sup / "operator-inbox"),
                  mock.patch.object(core, "INSTANCES", self.project / "instances"),
                  mock.patch.object(messages, "log", lambda *_: None),
                  mock.patch("supervisor.broker.notify", lambda *_: None, create=True)):
            p.start()
            self.addCleanup(p.stop)
        messages._cache.update(key=None, rows=[])

    def test_broker_logs_the_full_text_of_every_delivery(self):
        long = "Two things.\n" + "x" * 3000
        b = Broker("wren", self.project, {}, self.project / "broker.log")
        self.assertTrue(b.call("send_message", {"to": "shaft", "text": long})["ok"])
        self.assertTrue(b.call("send_message", {"to": "operator", "text": "a question?", "kind": "question"})["ok"])
        h = messages.history()
        self.assertEqual([(m["from"], m["to"], m["kind"]) for m in h],
                         [("wren", "shaft", "note"), ("wren", "operator", "question")])
        self.assertEqual(h[0]["text"], long)                       # not the 160-char broker.log line
        b.call("send_message", {"to": "shaft", "text": "x", "kind": "\x1b[2J anything"})
        self.assertEqual(messages.history()[-1]["kind"], "note")
        self.assertEqual([m["read"] for m in h], [False, False])

    def test_read_once_the_recipient_moved_it_out_of_its_inbox(self):
        b = Broker("wren", self.project, {}, self.project / "broker.log")
        b.call("send_message", {"to": "shaft", "text": "hello"})
        inbox = self.project / "instances" / "shaft" / "inbox"
        (f,) = inbox.glob("*.md")
        self.assertFalse(messages.history()[0]["read"])
        (inbox.parent / "memory" / "messages").mkdir(parents=True)
        f.rename(inbox.parent / "memory" / "messages" / f.name)
        self.assertTrue(messages.history()[0]["read"])

    def test_operator_messages_are_logged_too(self):
        messages.send("wren", "please look at X")
        (m,) = messages.history()
        self.assertEqual((m["from"], m["to"], m["kind"], m["text"]), ("operator", "wren", "message", "please look at X"))

    def test_a_torn_line_does_not_hide_the_rest(self):
        messages.record(self.sup, Path("a.md"), "wren", "shaft", "note", datetime(2026, 10, 13, 9), "one")
        with (self.sup / messages.LOG_NAME).open("a") as f:
            f.write('{"broken\n')
        messages.record(self.sup, Path("b.md"), "shaft", "wren", "note", datetime(2026, 10, 13, 10), "two")
        self.assertEqual([m["text"] for m in messages.history()], ["one", "two"])


class StartWakeTest(unittest.TestCase):
    def test_only_an_idle_unpaused_instance_is_started(self):
        with mock.patch.object(autonomy, "paused", return_value=True), \
                mock.patch.object(autonomy.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "paused"):
                autonomy.start_wake("wren")
            popen.assert_not_called()
        with mock.patch.object(autonomy, "paused", return_value=False), \
                mock.patch.object(autonomy, "_awake", return_value="live since 13:47"), \
                mock.patch.object(autonomy.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "already awake"):
                autonomy.start_wake("wren")
            popen.assert_not_called()

    def test_an_idle_instance_gets_a_detached_scheduled_style_wake(self):
        with mock.patch.object(autonomy, "paused", return_value=False), \
                mock.patch.object(autonomy, "_awake", return_value=""), \
                mock.patch.object(autonomy, "log"), \
                mock.patch.object(autonomy.subprocess, "Popen") as popen:
            self.assertEqual(autonomy.start_wake("wren"), "started a cycle for wren")
        args, kw = popen.call_args
        self.assertEqual(args[0][-4:], ["-m", "supervisor", "wake", "wren"])   # skip-if-awake, records the wake
        self.assertTrue(kw["start_new_session"])                               # outlives the TUI


def _snap(states=("idle", "in a cycle"), msgs=None):
    inst = [{"name": n, "state": s, "since": "", "doing": "", "goal": "", "model": "m", "every": "30 min",
             "next_wake": "due", "conversations": 0, "calls_today": 0, "tokens_today": 0, "usd_today": 0.0,
             "judged": 0, "success": None, "tools_off": []} for n, s in zip(("shaft", "wren"), states)]
    ms = msgs if msgs is not None else [
        {"ts": f"2026-10-13T1{k}:00:00", "from": f, "to": t, "kind": "note", "file": f"{k}.md",
         "text": f"message {k}\nmore", "read": k < 2}
        for k, (f, t) in enumerate([("wren", "shaft"), ("shaft", "wren"), ("operator", "wren")])]
    return {"at": "15:00:00", "services": [], "instances": inst, "events": [], "messages": ms}


@unittest.skipIf(top is None, "needs rich (.venv-ui/bin/python -m unittest supervisor.tests.test_messages_view)")
class TopKeysTest(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(top.keys("\x1b[Aj\r\x1b\x1b[6~q\x1b[1;5A"), ["up", "j", "enter", "esc", "pgdn", "q"])

    def test_c_asks_then_starts_a_cycle_only_for_an_idle_instance(self):
        ui, snap, started = top.UI(), _snap(), []

        def start(name):
            started.append(name)
            return f"started a cycle for {name}"
        top.handle(ui, "c", snap, 10, start)
        self.assertEqual((ui.confirm, started), ("shaft", []))      # asks first
        top.handle(ui, "y", snap, 10, start)
        self.assertEqual((ui.confirm, started, ui.flash), ("", ["shaft"], "started a cycle for shaft"))
        top.handle(ui, "down", snap, 10, start)
        top.handle(ui, "c", snap, 10, start)                        # wren is in a cycle
        self.assertEqual((ui.confirm, started), ("", ["shaft"]))
        self.assertIn("only when it is idle", ui.flash)
        ui.inst = 0
        top.handle(ui, "c", snap, 10, start)
        self.assertFalse(top.handle(ui, "q", snap, 10, start))     # q answers "no" here, it does not quit
        self.assertEqual(started, ["shaft"])

    def test_a_refused_start_is_shown_not_raised(self):
        ui = top.UI()
        top.handle(ui, "c", _snap(), 10, lambda n: (_ for _ in ()).throw(RuntimeError(f"{n} is already awake")))
        top.handle(ui, "y", _snap(), 10, lambda n: (_ for _ in ()).throw(RuntimeError(f"{n} is already awake")))
        self.assertEqual(ui.flash, "not started: shaft is already awake")

    def test_browse_filter_and_read(self):
        ui, snap = top.UI(), _snap()
        top.handle(ui, "m", snap, 10, None)
        self.assertEqual([m["file"] for m in top.shown(snap, ui)], ["2.md", "1.md", "0.md"])   # newest first
        top.handle(ui, "f", snap, 10, None)                          # filters cycle through participants
        self.assertEqual(ui.who, "operator")
        top.handle(ui, "f", snap, 10, None)
        self.assertEqual(ui.who, "shaft")
        self.assertEqual([m["file"] for m in top.shown(snap, ui)], ["1.md", "0.md"])
        top.handle(ui, "down", snap, 10, None)
        top.handle(ui, "enter", snap, 10, None)
        self.assertEqual((ui.view, top.shown(snap, ui)[ui.msg]["file"]), ("message", "0.md"))
        top.handle(ui, "n", snap, 10, None)                          # newer
        self.assertEqual(top.shown(snap, ui)[ui.msg]["file"], "1.md")
        top.handle(ui, "esc", snap, 10, None)
        top.handle(ui, "esc", snap, 10, None)
        self.assertEqual(ui.view, "main")

    def test_selection_follows_its_message_when_a_new_one_arrives(self):
        ui, old = top.UI(view="list", msg=1), _snap()
        new = _snap(msgs=old["messages"] + [{**old["messages"][0], "file": "3.md", "ts": "2026-10-13T16:00:00"}])
        top.follow(ui, old, new)
        self.assertEqual(top.shown(new, ui)[ui.msg]["file"], "1.md")

    def test_instance_written_text_cannot_drive_the_terminal(self):
        evil = "hi\x1b]52;c;cGF5bG9hZA==\x07\x1b[2J there"
        snap = _snap(msgs=[{"ts": "2026-10-13T10:00:00", "from": "wren", "to": "shaft", "kind": "note",
                            "file": "x.md", "text": evil, "read": False}])
        snap["instances"][0]["model"] = "[red]x"
        for ui in (top.UI(), top.UI(view="list"), top.UI(view="message")):
            from rich.console import Console
            c = Console(width=100, height=30, record=True, color_system=None, force_terminal=False)
            c.print(top.render(snap, ui, 100, 30))
            out = c.export_text()
            self.assertNotIn("\x1b", out)
            self.assertIn("hi]52;c;cGF5bG9hZA==[2J there", out)
        self.assertNotIn("\x1b", top.plain({**snap, "events": ["13:00 x\x1b[2J"]}))

    def test_reading_view_scrolls_a_long_message(self):
        snap = _snap(msgs=[{"ts": "2026-10-13T10:00:00", "from": "wren", "to": "shaft", "kind": "note",
                            "file": "x.md", "text": "\n".join(f"line {k}" for k in range(100)), "read": True}])
        ui = top.UI(view="message")
        top.handle(ui, "pgdn", snap, 20, None)
        from rich.console import Console
        c = Console(width=80, height=30, record=True, color_system=None)
        c.print(top.render(snap, ui, 80, 30))
        out = c.export_text()
        self.assertIn("line 20", out)
        self.assertNotIn("line 19\n", out)
        self.assertIn("lines 21–46 of 100", out)

    def test_list_and_reading_views_fit_the_screen_even_when_narrow(self):
        # the footer once wrapped at 80 columns, pushing the screen one line past its height
        from rich.console import Console
        snap = _snap(msgs=[{"ts": "2026-10-13T10:00:00", "from": "wren", "to": "shaft", "kind": "note",
                            "file": f"{k}.md", "text": "word " * 400, "read": False} for k in range(60)])
        for width in (50, 80, 140):
            for view in ("list", "message"):
                c = Console(width=width, height=24, record=True, color_system=None)
                c.print(top.render(snap, top.UI(view=view), width, 24))
                self.assertLessEqual(len(c.export_text().splitlines()), 24, (view, width))


if __name__ == "__main__":
    unittest.main()

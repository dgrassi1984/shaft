"""Attachments (the operator, 2026-10-14: "attach files to their messages to each other and on Telegram"): an instance
attaches files from its folder to send_message — another instance finds them in its attachments/, the operator gets them
from the instance's bot — and files the operator sends a bot are saved in that instance's attachments/."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from supervisor import broker, messages, pushes

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


class BrokerAttachmentsTest(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-att-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        (self.project / "instances" / "b" / "shaft").mkdir(parents=True)
        self.a = self.project / "instances" / "a"
        (self.a / "notes").mkdir(parents=True)
        (self.a / "notes" / "report.md").write_text("# Report\n")
        (self.a / "notes" / "chart.png").write_bytes(PNG)
        p = mock.patch.object(messages, "PROJECT", self.project)
        p.start()
        self.addCleanup(p.stop)
        self.b = broker.Broker("a", self.project, {}, self.project / "broker.log")
        self.url = self.b.start()
        self.addCleanup(self.b.stop)

    def post(self, args):
        req = urllib.request.Request(f"{self.url}/tools/send_message", data=json.dumps(args).encode(),
                                     headers={"X-Shaft-Token": self.b.token})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def log(self):
        p = self.project / ".supervisor" / messages.LOG_NAME
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_to_another_instance(self):
        out = self.post({"to": "b", "text": "Here it is", "attachments": ["notes/report.md", "notes/chart.png"]})
        self.assertTrue(out["ok"], out)
        self.assertIn("with 2 attachment(s)", out["result"])
        (got,) = (self.project / "instances" / "b" / "attachments" / "from-a").iterdir()
        self.assertEqual((got / "report.md").read_text(), "# Report\n")
        self.assertEqual((got / "chart.png").read_bytes(), PNG)
        (msg,) = (self.project / "instances" / "b" / "inbox").glob("*.md")
        body = msg.read_text()
        self.assertIn(f"- attachments/from-a/{got.name}/report.md (9 bytes)", body)   # where the reader finds it
        (row,) = self.log()
        self.assertEqual([a["name"] for a in row["attachments"]], ["report.md", "chart.png"])
        self.assertEqual(row["attachments"][1]["path"], f"instances/b/attachments/from-a/{got.name}/chart.png")

    def test_to_the_operator(self):
        with mock.patch.object(broker, "notify"):
            out = self.post({"to": "operator", "text": "The chart", "attachments": ["notes/chart.png"]})
        self.assertTrue(out["ok"], out)
        (row,) = self.log()
        (a,) = row["attachments"]
        self.assertTrue(a["path"].startswith(".supervisor/operator-inbox/attachments/"))
        self.assertEqual((self.project / a["path"]).read_bytes(), PNG)
        self.assertIn(f"- {a['path']} (72 bytes)", row["text"])

    def test_refused_and_nothing_delivered(self):
        (self.a / "secrets").mkdir()
        (self.a / "secrets" / "keys.json").write_text("{}")
        outside = self.project / "outside.txt"
        outside.write_text("not yours")
        os.symlink(outside, self.a / "notes" / "link.txt")
        for bad, why in (("../b/shaft/x", "in your folder"), (str(outside), "in your folder"),
                         ("notes/link.txt", "in your folder"), ("secrets/keys.json", "cannot be attached"),
                         ("notes/missing.md", "no such file"), ("notes", "no such file")):
            with self.subTest(bad=bad):
                out = self.post({"to": "b", "text": "x", "attachments": ["notes/report.md", bad]})
                self.assertFalse(out["ok"])
                self.assertIn(why, out["error"])
        with mock.patch.object(messages, "ATTACH_MAX_BYTES", 10):
            self.assertIn("per file", self.post({"to": "b", "text": "x", "attachments": ["notes/chart.png"]})["error"])
        self.assertIn("at most 10", self.post({"to": "b", "text": "x", "attachments": ["notes/report.md"] * 11})["error"])
        self.assertEqual(self.log(), [])                                   # all checked before anything moved
        self.assertFalse((self.project / "instances" / "b" / "attachments").exists())

    def test_a_replay_checks_but_writes_nothing(self):
        stored = broker.ReplayBroker._store(self.b, self.project / "instances" / "b", "attachments/from-a/x",
                                            [("chart.png", PNG)])
        self.assertEqual(stored[0]["rel"], "attachments/from-a/x/chart.png")
        self.assertFalse((self.project / "instances" / "b" / "attachments").exists())

    def test_names_are_made_safe_and_unique(self):
        atts = messages.store_attachments(self.project / "instances" / "b", "attachments/t",
                                          [("../../evil name?.txt", b"1"), ("evil name_.txt", b"2")])
        self.assertEqual([a["name"] for a in atts], ["evil name_.txt", "2-evil name_.txt"])
        self.assertTrue(all((self.project / "instances" / "b" / a["rel"]).exists() for a in atts))


class TelegramAttachmentsTest(unittest.TestCase):
    """Against test_telegram's fake Bot API, extended with uploads and downloads."""

    def setUp(self):
        self.enterContext(mock.patch.object(pushes, "QUIET_HOURS", None))
        from supervisor import telegram
        from supervisor.tests.test_telegram import FakeBotAPI, TOK
        self.telegram, self.tok = telegram, TOK
        self.project = Path(tempfile.mkdtemp(prefix="shaft-tga-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        self.sup = self.project / ".supervisor"
        self.sup.mkdir()
        self.api = FakeBotAPI()
        self.addCleanup(self.api.server.shutdown)
        env = {telegram.token_key(n): t for n, t in TOK.items()}
        for p in (mock.patch.object(telegram, "SUP_DIR", self.sup), mock.patch.object(telegram, "API_BASE", self.api.url),
                  mock.patch.object(telegram, "secrets_env", lambda: env),
                  mock.patch.object(telegram, "instance_names", lambda: ["shaft", "wren"]),
                  mock.patch.object(telegram, "log", lambda *_: None),
                  mock.patch.object(messages, "SUP_DIR", self.sup), mock.patch.object(messages, "PROJECT", self.project),
                  mock.patch.object(messages, "OPERATOR_INBOX", self.sup / "operator-inbox"),
                  mock.patch.object(messages, "INSTANCES", self.project / "instances"),
                  mock.patch("supervisor.core.PROJECT", self.project),
                  mock.patch("supervisor.core.INSTANCES", self.project / "instances")):
            p.start()
            self.addCleanup(p.stop)
        messages._cache.update(key=None, rows=[])
        telegram.update(lambda s: s.__setitem__("paired", {"user_id": 42}))

    def test_an_instances_attachments_reach_operator_after_its_text(self):
        from datetime import datetime
        t = self.telegram
        t.forward_new()                                                    # cursor: now
        inbox = self.sup / "operator-inbox"
        atts = messages.store_attachments(inbox, "attachments/m1", [("chart.png", PNG), ("data.csv", b"a,b\n1,2\n")])
        path = inbox / "20991231-235959-000001-from-wren.md"
        path.write_text("x")
        messages.record(self.sup, path, "wren", "operator", "note", datetime(2099, 12, 31, 23, 59, 59), "see files", atts,
                        ("telegram", "test"))
        self.assertEqual(t.forward_new(), 1)
        methods = [(m, p) for tok, m, p in self.api.calls if tok == self.tok["wren"] and m.startswith("send")]
        self.assertEqual([m for m, _ in methods], ["sendMessage", "sendPhoto", "sendDocument"])   # picture inline
        self.assertIn('filename="chart.png"', self.api.uploads[0])
        self.assertIn('filename="data.csv"', self.api.uploads[1])

    def test_operator_sends_a_file_it_lands_in_the_instance_and_the_turn_says_where(self):
        t = self.telegram
        asked = []
        chat = t.Chat("wren", self.tok["wren"])
        msg = {"message_id": 7, "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "caption": "look at this",
               "document": {"file_id": "F1", "file_name": "paper.pdf"}}
        with mock.patch.object(t, "ask_api",
                               lambda name, history, request_id="": asked.append(history[-1]["content"]) or ("ok", 0)):
            t.handle_update("wren", self.tok["wren"], chat, {"update_id": 1, "message": msg})
        (saved,) = (self.project / "instances" / "wren" / "attachments" / "from-operator").glob("*/paper.pdf")
        self.assertEqual(saved.read_bytes(), self.api.FILE)
        self.assertTrue(asked[0].startswith("look at this"))
        self.assertIn(f"- {saved.relative_to(self.project / 'instances' / 'wren')} (", asked[0])

    def test_a_photo_without_text_and_an_unsupported_message(self):
        t = self.telegram
        asked = []
        chat = t.Chat("wren", self.tok["wren"])
        photo = {"message_id": 8, "from": {"id": 42}, "chat": {"id": 42, "type": "private"},
                 "photo": [{"file_id": "small"}, {"file_id": "F1"}]}
        sticker = {"message_id": 9, "from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "sticker": {}}
        with mock.patch.object(t, "ask_api",
                               lambda name, history, request_id="": asked.append(history[-1]["content"]) or ("ok", 0)):
            t.handle_update("wren", self.tok["wren"], chat, {"update_id": 2, "message": photo})
            t.handle_update("wren", self.tok["wren"], chat, {"update_id": 3, "message": sticker})
        self.assertTrue(asked[0].startswith("(no text, only files)"))
        self.assertIn("photo-8.jpg", asked[0])
        self.assertEqual(len(asked), 1)
        self.assertIn("text and files", self.api.sent()[-1][1]["text"])


if __name__ == "__main__":
    unittest.main()


class SameSecondTest(unittest.TestCase):
    def test_two_sends_in_one_second_keep_both_files(self):
        """Codex review: one folder per second and an overwriting write — the second image.png replaced the first."""
        import tempfile
        from pathlib import Path
        from supervisor import messages
        root = Path(tempfile.mkdtemp(prefix="shaft-same-second-")).resolve()
        a = messages.store_attachments(root, "attachments/from-operator/t", [("image.png", b"first")])
        b = messages.store_attachments(root, "attachments/from-operator/t", [("image.png", b"second")])
        self.assertNotEqual(a[0]["rel"], b[0]["rel"])
        self.assertEqual((root / a[0]["rel"]).read_bytes(), b"first")
        self.assertEqual((root / b[0]["rel"]).read_bytes(), b"second")
        self.assertEqual(b[0]["name"], "2-image.png")

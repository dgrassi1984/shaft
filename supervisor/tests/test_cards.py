"""Outcome cards from scratch records, including the held-call retry in the real loop."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import agent, cards
from shaft.llm import Client
from shaft.tools import Toolbox
from supervisor.tests.fakellm import FakeLLM


class FamilyTest(unittest.TestCase):
    def test_families_and_reads(self):
        cases = [
            ("write_file", {"path": "self/goals.md"}, "write_file self/"),
            ("append_file", {"path": "memory/journal.md"}, "append_file memory/"),
            ("write_file", {"path": "plugins/web_research/main.py"}, "write_file plugins/web_research/"),
            ("write_file", {"path": "skills/checking/a.md"}, "write_file skills/checking/"),
            ("append_file", {"path": "notes/work/a.md"}, "append_file notes/work/"),
            ("write_file", {"path": "tests/unit/test_a.py"}, "write_file tests/unit/"),
            ("run", {"command": "pytest -q"}, "run pytest"),
            ("run", {"command": "A=1 B='two words' pytest -q"}, "run pytest"),
            ("run", {"command": "cd x && pytest"}, "run pytest"),
            ("run", {"command": 'A=1 cd "some dir" && B=2 python3 test.py'}, "run python3 test.py"),
            ("delegate_coding", {}, "delegate_coding"),
            ("send_message", {"to": "wisp"}, "send_message → wisp"),
            ("http_request", {"method": "post", "url": "https://example.org:443/path"},
             "http_request POST example.org"),
            ("http_request", {"method": "DELETE", "url": "https://example.org/path"},
             "http_request DELETE example.org"),
            ("http_request", {"method": "GET", "url": "https://example.org"}, None),
            ("http_request", {"method": "HEAD", "url": "https://example.org"}, None),
            ("http_request", {"url": "https://example.org"}, None),
            ("set_config", {}, "set_config"), ("secret_set", {}, "secret_set"),
            ("secret_delete", {}, "secret_delete"), ("read_file", {"path": "self/a"}, None),
            ("search_memory", {"query": "x"}, None),
        ]
        for tool, args, expected in cases:
            with self.subTest(tool=tool, args=args):
                self.assertEqual(cards.family(tool, json.dumps(args)), expected)

    def test_outcome_cards_grouped_every_run_by_first_word_cd_echo_wc(self):
        cases = [
            ("cd /a && python3 -m pytest -q", "run pytest"),
            ("cd /a && python3 shaft/x.py", "run python3 shaft/x.py"),
            ("VAR=1 env FOO=2 git commit", "run git"),
            ('bash -lc "cd x && make test"', "run make"),
            ("echo hi", None), ("wc -l f", None), ("cd /a && grep x f", None),
            ("cd /a;cd /b; python -m pip install x", "run pip"),
            ("A=1; exec env -u HOME nice -n 5 sudo -u root time -p git commit", "run git"),
            ("sh -c 'make test | cat'", "run make"),
            ("make test && git commit", "run make"),
            ("echo hi && make test", None),
            ("sed -n '1p' f", None), ("sed -i 's/a/b/' f", "run sed"),
            ("python3 -W ignore -m pytest", "run pytest"),
            ("python3 -c 'print(1)'", "run python3"),
            ('make "unfinished', "run make"),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(cards.family("run", json.dumps({"command": command})), expected)

    def test_inspection_allowlist_and_wrapped_programs(self):
        for command in ("echo hi", "printf hi", "cat f", "ls", "head f", "tail f", "wc f",
                        "grep x f", "rg x", "find .", "sed -n '1p' f", "pwd", "true", "test -f f", "which git"):
            with self.subTest(command=command):
                self.assertIsNone(cards.family("run", json.dumps({"command": command})))
        for command in ("env -u HOME FOO=2 git commit", "nice --adjustment=5 git commit",
                        "sudo -n -uroot git commit", "doas -u root git commit",
                        "exec -a name git commit", "nohup git commit", "/usr/bin/git commit"):
            with self.subTest(command=command):
                self.assertEqual(cards.family("run", json.dumps({"command": command})), "run git")

    def test_cut_arguments(self):
        for tool, raw, expected in [
            ("write_file", '{"path":"self/a", "content":"cut', "write_file self/"),
            ("append_file", '{"path":"notes/work/a', "append_file notes/work/"),
            ("run", '{"command":"cd x && pytest', "run pytest"),
            ("send_message", '{"to":"wisp", "text":"cut', "send_message → wisp"),
            ("http_request", '{"method":"POST", "url":"https://example.org/p',
             "http_request POST example.org"),
            ("run", '{"command":"A=1', None),
        ]:
            with self.subTest(tool=tool, raw=raw):
                self.assertEqual(cards.family(tool, raw), expected)


class CardsTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {}, clear=True))
        tmp = tempfile.TemporaryDirectory(prefix="shaft-cards-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "memory/episodes").mkdir(parents=True)
        self.now = datetime(2026, 10, 26, 12)

    def episode(self, day, result="ERROR: refused", goal="repair fence", tool="write_file", args=None,
                **entry_fields):
        ts = f"2026-10-{day:02d}T12:00:00"
        entry = {"tool": tool, "args": json.dumps(args or {"path": "self/a"}), "result": result,
                 "seconds": 2.5, **entry_fields}
        row = {"ts": ts, "kind": "live", "intent": {"goal": goal}, "tools": [entry]}
        self.write_episode(row)
        return ts

    def write_episode(self, row):
        with (self.root / "memory/episodes/test.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")

    def make(self, mode="on"):
        return cards.Cards(self.root, mode, self.now)

    def check(self, instance, goal="repair fence"):
        return instance.check("write_file", '{"path":"self/a"}', goal)

    def test_load_window_failures_skips_and_verdicts(self):
        self.episode(11)
        edge = self.episode(12, goal="g" * 220, result="ERROR: " + "e" * 180 + "\nsecond")
        flagged = self.episode(13, "exit code 1\ntrace", flag="command_failed")
        good = self.episode(14, "wrote file")
        self.episode(15, agent.NOT_RUN)
        self.episode(16, cards.HELD + " evidence")
        self.episode(27)
        self.write_episode({"ts": "2026-10-25T12:00:00", "kind": "sleep", "tools": []})
        self.write_episode({"ts": "bad", "kind": "live", "tools": []})
        (self.root / "memory/verdicts.jsonl").write_text(json.dumps({"episode_ts": edge,
                                                                 "criterion_met": "no"}) + "\n")
        loaded = cards.load(self.root, self.now)
        self.assertEqual([(c["ts"], c["ok"], c["verdict"]) for c in loaded],
                         [(edge, False, "no"), (flagged, False, None), (good, True, None)])
        self.assertEqual(loaded[0]["error"], ("ERROR: " + "e" * 180)[:160])
        self.assertEqual(loaded[0]["goal"], "g" * 200)
        self.assertEqual(loaded[1]["error"], "exit code 1")
        self.assertEqual((loaded[2]["seconds"], loaded[2]["step"], loaded[2]["error"]), (2.5, 1, ""))
        self.assertEqual(cards.load(self.root, self.now.astimezone(timezone.utc)), loaded)

    def test_old_episode_families_are_recomputed_and_inspections_never_hold(self):
        self.episode(20, tool="run", args={"command": "cd /a && python3 -m pytest"}, family="run cd")
        self.episode(21, tool="run", args={"command": "wc -l f"}, family="run wc")
        self.episode(22, tool="run", args={"command": "cd /a && grep x f"},
                     family="run cd", flag="command_failed")
        self.assertEqual([c["family"] for c in cards.load(self.root, self.now)], ["run pytest"])
        instance = self.make()
        self.assertTrue(instance.check("run", '{"command":"pytest -q"}', "test").startswith(cards.HELD))
        for command in ("echo hi", "wc -l f", "cd /a && grep x f"):
            self.assertIsNone(instance.check("run", json.dumps({"command": command}), "inspect"))

    def test_latest_outcome_once_per_family_and_modes(self):
        self.episode(20)
        instance = self.make()
        with mock.patch.object(cards, "load", wraps=cards.load) as reader:
            self.assertTrue(self.check(instance).startswith(cards.HELD))
            self.assertIsNone(self.check(instance))
            self.assertEqual(reader.call_count, 1)
        self.episode(21, "wrote file")
        self.assertIsNone(self.check(self.make()))
        self.episode(22)
        self.assertTrue(self.check(self.make()).startswith(cards.HELD))
        for mode in ("off", "shuffled", "recall", "bad mode"):
            instance = self.make(mode)
            with mock.patch.object(cards, "load", side_effect=AssertionError("must stay lazy")):
                self.assertIsNone(self.check(instance))
            self.assertEqual(instance.record, {"mode": "off", "held": []})
        with mock.patch.object(cards, "load", side_effect=AssertionError("reads need no cards")):
            self.assertIsNone(self.make().check("read_file", '{}', "repair"))

    def test_step_order_and_independent_family_holds(self):
        self.write_episode({"ts": "2026-10-22T12:00:00", "kind": "live", "intent": None,
                            "tools": [
                                {"tool": "read_file", "args": '{}', "result": "read"},
                                {"tool": "write_file", "args": '{"path":"self/a"}', "result": "ERROR: refused"},
                                {"tool": "write_file", "args": '{"path":"self/b"}', "result": "wrote"}]})
        self.episode(21, tool="delegate_coding")
        self.episode(20)
        self.assertEqual([(card["ts"], card["step"]) for card in cards.load(self.root, self.now)],
                         [("2026-10-20T12:00:00", 1), ("2026-10-21T12:00:00", 1),
                          ("2026-10-22T12:00:00", 2), ("2026-10-22T12:00:00", 3)])
        self.assertIsNone(self.check(self.make()))
        self.episode(23)
        instance = self.make()
        self.assertTrue(self.check(instance).startswith(cards.HELD))
        self.assertTrue(instance.check("delegate_coding", '{}', "repair").startswith(cards.HELD))
        self.assertIsNone(self.check(instance))
        self.assertIsNone(instance.check("delegate_coding", '{}', "repair"))

    def test_counts_ranking_record_and_three_cards(self):
        self.episode(20, "wrote", "repair fence")
        self.episode(21, goal="repair fence")
        self.episode(22, goal="fence")
        self.episode(23, goal="repair")
        self.episode(24, goal="orchard")
        instance = self.make()
        text = self.check(instance)
        self.assertIn("4 failed, 1 succeeded (last failure 2026-10-24 12:00: ERROR: refused)", text)
        lines = [line for line in text.splitlines() if line.startswith("- ")]
        self.assertEqual(len(lines), 3)
        self.assertIn('2026-10-21 12:00 · goal "repair fence" · failed: ERROR: refused · 2s · not judged', lines[0])
        self.assertIn('2026-10-20 12:00 · goal "repair fence" · ok · 2s · not judged', lines[1])
        self.assertIn('2026-10-23 12:00 · goal "repair"', lines[2])
        self.assertEqual(instance.record, {"mode": "on", "held": [{"family": "write_file self/",
                         "tool": "write_file", "cards": ["2026-10-21T12:00:00#1", "2026-10-20T12:00:00#1",
                         "2026-10-23T12:00:00#1"], "failed": 4, "succeeded": 1}]})

    def test_a_second_call_in_the_same_reply_is_held_too(self):
        """Regression (Grok's review): the second call of a held kind in the same reply ran before I read the cards."""
        self.episode(20)
        c = self.make()
        self.assertTrue(c.check("write_file", '{"path":"self/a"}', "g", turn=3).startswith(cards.HELD))
        again = c.check("write_file", '{"path":"self/b"}', "g", turn=3)
        self.assertTrue(again.startswith(cards.HELD))
        self.assertIn("earlier in this reply", again)
        self.assertIsNone(c.check("write_file", '{"path":"self/b"}', "g", turn=4))   # next reply: it runs
        self.assertEqual(len(c.record["held"]), 1)

    def test_one_family_per_place_however_spelled(self):
        """Regression (Grok's review): ./self/a.md was a family of its own, apart from self/a.md."""
        for path in ("self/a.md", "./self/a.md", "self/x/../a.md"):
            self.assertEqual(cards.family("write_file", json.dumps({"path": path})), "write_file self/")
        self.assertIsNone(cards.family("write_file", json.dumps({"path": "../outside.md"})))

    def test_held_call_can_run_on_identical_second_call(self):
        fake = FakeLLM([{"tool_calls": [("write_file", {"path": "self/a"})]},
                        {"tool_calls": [("write_file", {"path": "self/a"})]}, {"content": "Done."}])
        self.addCleanup(fake.close)
        client = Client(fake.url, "fake", "", 30, self.root / "usage.jsonl")
        box = Toolbox(self.root)

        def run(tool, args):
            result = cards.HELD + " evidence" if not box.log else "wrote file"
            box.log.append({"tool": tool, "args": args, "result": result})
            return result

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(box, "run", side_effect=run) as dispatch, \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            outcome = agent.run(client, [], box, max_tokens=200, purpose="live", max_steps=4, ledger=False)
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual([e["result"] for e in outcome.transcript], [cards.HELD + " evidence", "wrote file"])
        self.assertEqual((outcome.ended_by, outcome.reply.content), ("answer", "Done."))


if __name__ == "__main__":
    unittest.main()

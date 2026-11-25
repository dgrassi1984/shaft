"""Cited tool-log steps are checked against the fingerprints the broker kept of what it served (#8)."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import broker, commitments, core, judge, predictions, served

START = datetime(2026, 1, 2, 14, 50)
END = datetime(2026, 1, 2, 15, 0)
LONG = "a" * 9000


def cut(text: str) -> str:
    """The template's tool log keeps a long result like this (shaft/tools.py Toolbox.run)."""
    return (text[:served.EPISODE_CUT] + f"\n[... truncated, {len(text)} chars total; saved to .tmp/outputs/x.txt. "
            "Read the rest with read_file('.tmp/outputs/x.txt', offset=5600)]")


def episode(tool="fetch_page", result="Reading: 42"):
    return {"ts": END.isoformat(), "run_started": START.isoformat(), "kind": "live",
            "tools": [{"tool": tool, "args": {"url": "https://example.com"}, "result": result}]}


class ServedTest(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="shaft-served-"))
        self.sup = Path(tmp)
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(core, "log"))
        served.begin(START - timedelta(hours=5))

    def keep(self, tool="fetch_page", result="Reading: 42", at=START + timedelta(minutes=5), name="ember"):
        served.keep(name, tool, result, at=at)

    def test_fingerprint_keeps_no_text_and_matches_whole_and_cut_results(self):
        self.keep(result="Reading: 42")
        self.keep(result=LONG)
        text = (self.sup / "served/ember.jsonl").read_text()
        self.assertNotIn("Reading", text)
        self.assertNotIn("aaaa", text)
        short, long = (json.loads(line) for line in text.splitlines())
        self.assertEqual(set(short), {"ts", "tool", "len", "sha256", "cut"})
        self.assertTrue(served.matches(short, "Reading: 42"))
        self.assertFalse(served.matches(short, "Reading: 43"))
        self.assertTrue(served.matches(long, cut(LONG)))
        self.assertFalse(served.matches(long, cut("b" + LONG[1:])))          # a changed kept part
        self.assertFalse(served.matches(long, cut(LONG).replace("9000", "9001")))   # a changed length
        self.assertFalse(served.matches(short, None))

    def test_broker_keeps_a_fingerprint_of_successful_calls_only_and_never_in_a_replay(self):
        self.enterContext(mock.patch.object(core, "disabled_tools", return_value=set()))
        self.enterContext(mock.patch.object(broker.Broker, "_shared_folder", return_value=None))
        root = self.sup / "agents/ember"
        root.mkdir(parents=True)
        box = broker.Broker("ember", self.sup, {}, self.sup / "broker.log", root=root, network=False)
        with mock.patch.object(broker.Broker, "_fetch_page", create=True, return_value="Reading: 42"):
            self.assertTrue(box.call("fetch_page", {"url": "https://example.com"})["ok"])
        with mock.patch.object(broker.Broker, "_fetch_page", create=True, side_effect=RuntimeError("down")):
            self.assertFalse(box.call("fetch_page", {"url": "https://example.com"})["ok"])
        rows = [json.loads(l) for l in (self.sup / "served/ember.jsonl").read_text().splitlines()]
        self.assertEqual([r["tool"] for r in rows], ["fetch_page"])
        self.assertTrue(served.matches(rows[0], "Reading: 42"))
        replay = broker.ReplayBroker("ember", self.sup, {}, self.sup / "trials/broker.log", state_root=root,
                                     start_commit="a" * 40, as_of=END, cache={}, cache_lock=threading.Lock(),
                                     billed="ember@trial:example", tools_log=self.sup / "tools.log")
        with mock.patch.object(broker.ReplayBroker, "_fetch_page", create=True, return_value="Reading: 42"):
            self.assertTrue(replay.call("fetch_page", {"url": "https://example.com"})["ok"])
        self.assertFalse((self.sup / "trials/served").exists())
        self.assertEqual(len((self.sup / "served/ember.jsonl").read_text().splitlines()), 1)

    def test_only_a_served_matching_call_in_the_cycles_window_counts(self):
        self.keep()                                                   # the honest call
        self.keep(tool="web_search", at=END + timedelta(hours=1))     # outside the window
        self.keep(name="wisp", tool="mail_read")                      # another instance's
        record = served.Record("ember")
        self.assertEqual(record.check(episode(), episode()["tools"][0]), "")
        for ep, reason in [(episode(result="Reading: 41"), "result differs from the supervisor's record"),
                           (episode(tool="web_search"), "the supervisor served no successful web_search call in that cycle"),
                           (episode(tool="mail_read"), "the supervisor served no successful mail_read call in that cycle")]:
            with self.subTest(reason=reason):
                self.assertEqual(record.check(ep, ep["tools"][0]), reason)
        built_in = episode(tool="write_file", result="wrote notes/x.md")
        self.assertIsNone(record.check(built_in, built_in["tools"][0]))   # never reaches the broker: not checkable
        before = {**episode(result="anything"), "ts": "2026-01-01T10:00:00", "run_started": "2026-01-01T09:50:00"}
        self.assertIsNone(record.check(before, before["tools"][0]))       # before the record began

    def test_kept_commitment_evidence_needs_the_served_call(self):
        item = {"made_at": (START - timedelta(hours=1)).isoformat()}
        self.keep()
        self.keep(tool="web_search", result=LONG)
        record = served.Record("ember")
        cases = [(episode(), ""), (episode(tool="web_search", result=cut(LONG)), ""),
                 (episode(result="Reading: 41"), "result differs from the supervisor's record"),
                 (episode(tool="mail_read", result="Invoice paid"),
                  "the supervisor served no successful mail_read call in that cycle"),
                 (episode(tool="write_file", result="wrote notes/x.md"), "")]
        for ep, reason in cases:
            with self.subTest(reason=reason or ep["tools"][0]["tool"]):
                self.assertEqual(commitments._evidence([ep], item, END.isoformat() + "#1", record)[0], reason)
                self.assertEqual(commitments._evidence([ep], item, END.isoformat() + "#1")[0], "")   # no record: as before

    def test_prediction_scored_from_a_forged_step_is_not_checked_and_never_judged(self):
        ask = self.enterContext(mock.patch.object(judge, "ask", return_value={"result": "held", "reason": "42"}))
        self.enterContext(mock.patch.object(judge, "available", return_value=True))
        self.enterContext(mock.patch.object(judge, "pick", return_value="openrouter/example/judge"))
        self.enterContext(mock.patch.object(predictions.metrics, "own_models", return_value=[]))
        episodes = self.enterContext(mock.patch.object(predictions.commitments, "_episodes"))
        self.keep(result="Reading: 41")
        now = START - timedelta(hours=2)
        for result, expected in [("Reading: 42", "not checked"), ("Reading: 41", "held")]:
            with self.subTest(result=result):
                event = predictions.add("ember", text="The reading will be 41", due=(now + timedelta(hours=2))
                                        .isoformat(), how_to_check="Read the source", now=now)
                predictions.report("ember", id=event["id"], outcome="held", evidence=END.isoformat() + "#1",
                                   now=END + timedelta(minutes=1))
                episodes.return_value = [episode(result=result)]
                predictions.score("ember", END + timedelta(minutes=2))
                score = predictions.fold("ember")[event["id"]]["score"]
                self.assertEqual(score["result"], expected)
                if expected == "not checked":
                    self.assertIn("result differs from the supervisor's record", score["reason"])
        ask.assert_called_once()


if __name__ == "__main__":
    unittest.main()

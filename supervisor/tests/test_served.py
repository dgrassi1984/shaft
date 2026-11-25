"""Cited tool-log steps are checked against the fingerprints the broker kept of what it served (#8)."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import broker, commitments, core, judge, jsonl, predictions, served

START = datetime(2026, 1, 2, 14, 50)
END = datetime(2026, 1, 2, 15, 0)
RUN = START.isoformat(timespec="seconds")
LONG = "a" * 9000
TEMPLATE_TOOLS = Path(__file__).resolve().parents[2] / "template" / "shaft" / "tools.py"


def cut(text: str, rel: str = ".tmp/outputs/output-ab12cd34.txt") -> str:
    """The template's tool log keeps a long result like this (shaft/tools.py Toolbox.run)."""
    return (text[:served.EPISODE_CUT] + f"\n[... truncated, {len(text)} chars total; saved to {rel}. "
            f"Read the rest with read_file({rel!r}, offset={served.EPISODE_CUT})]")


def episode(tool="fetch_page", result="Reading: 42", run_started=RUN, ts=END, kind="live", tools=None):
    ep = {"ts": ts.isoformat(), "kind": kind,
          "tools": tools if tools is not None else [{"tool": tool, "args": {"url": "https://example.com"},
                                                     "result": result}]}
    if run_started is not None:
        ep["run_started"] = run_started
    return ep


class ServedTest(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="shaft-served-"))
        self.sup = Path(tmp)
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(core, "log"))
        served.begin(START - timedelta(hours=5))
        self.add_run()

    def add_run(self, name="ember", start=START, end=END + timedelta(seconds=1), kind="live"):
        run = start.isoformat(timespec="seconds")
        served.run_event(name, run, kind, "start", at=start)
        if end is not None:
            served.run_event(name, run, kind, "end", at=end)
        return run

    def keep(self, tool="fetch_page", result="Reading: 42", at=START + timedelta(minutes=5), name="ember", run=RUN):
        served.keep(name, tool, result, run=run, at=at)

    # -- the fingerprint ---------------------------------------------------------------------------------------------
    def test_fingerprint_keeps_no_text_and_matches_whole_and_cut_results(self):
        self.keep(result="Reading: 42")
        self.keep(result=LONG)
        text = (self.sup / "served/ember.jsonl").read_text()
        self.assertNotIn("Reading", text)
        self.assertNotIn("aaaa", text)
        short, long = (json.loads(line) for line in text.splitlines())
        self.assertEqual(set(short), {"ts", "tool", "run", "kind", "talk", "len", "sha256", "cut", "args"})
        self.assertTrue(served.matches(short, "Reading: 42"))
        self.assertFalse(served.matches(short, "Reading: 43"))
        self.assertTrue(served.matches(long, cut(LONG)))
        self.assertTrue(served.matches(long, LONG[:served.EPISODE_CUT] + f"\n[... truncated, 9000 chars total; could "
                                       "not save output: OSError: [Errno 28] No space left on device]"))
        self.assertFalse(served.matches(long, cut("b" + LONG[1:])))          # a changed kept part
        self.assertFalse(served.matches(long, cut(LONG).replace("9000", "9001")))   # a changed length
        self.assertFalse(served.matches(short, None))

    def test_text_after_a_cut_or_a_cut_the_template_never_makes_does_not_match(self):
        """Regression (#8 review): a 5800-char result rewritten to its first 5600 chars + a marker + invented text
        matched, and anything could follow the marker of a cut result."""
        mid = "m" * 5800                                   # kept whole by the template: it cuts only over 6000
        row = served.fingerprint(mid)
        self.assertIsNone(row["cut"])
        self.assertFalse(served.matches(row, mid[:served.EPISODE_CUT] + "\n[... truncated, 5800 chars total] FAKE: paid"))
        self.assertFalse(served.matches(row, cut(mid)))
        long = served.fingerprint(LONG)
        for forged in (cut(LONG) + " FAKE: invoice paid", cut(LONG)[:-1],
                       LONG[:served.EPISODE_CUT] + "\n[... truncated, 9000 chars total; payment succeeded]",
                       cut(LONG).replace("offset=5600", "offset=0"),
                       cut(LONG, rel="notes/forged.txt")):
            with self.subTest(forged=forged[served.EPISODE_CUT:][:60]):
                self.assertFalse(served.matches(long, forged))

    def test_constants_are_the_templates(self):
        """The cut is the template's (read by parsing its source, never importing agent code): if it changes, every
        honest long citation would stop matching."""
        values = {}
        for node in ast.parse(TEMPLATE_TOOLS.read_text()).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in ("MAX_RESULT_CHARS", "READ_MARKER_ROOM", "MAX_OUTPUT_CHARS"):
                    values[node.targets[0].id] = ast.literal_eval(node.value)
        self.assertEqual(values, {"MAX_RESULT_CHARS": served.MAX_RESULT_CHARS,
                                  "READ_MARKER_ROOM": served.READ_MARKER_ROOM,
                                  "MAX_OUTPUT_CHARS": served.MAX_OUTPUT_CHARS})
        self.assertEqual(served.EPISODE_CUT, values["MAX_RESULT_CHARS"] - values["READ_MARKER_ROOM"])
        source = TEMPLATE_TOOLS.read_text()
        self.assertIn('marker = (f"\\n[... truncated, {total} chars total{cap}; saved to {rel}. "', source)
        self.assertIn('f"Read the rest with read_file({rel!r}, offset={MAX_RESULT_CHARS - READ_MARKER_ROOM})]")', source)
        self.assertIn('marker = f"\\n[... truncated, {total} chars total; could not save output: '
                      '{type(e).__name__}: {str(e)[:200]}]"', source)

    # -- the broker --------------------------------------------------------------------------------------------------
    def box(self, cls=broker.Broker, **kw):
        self.enterContext(mock.patch.object(core, "disabled_tools", return_value=set()))
        self.enterContext(mock.patch.object(broker.Broker, "_shared_folder", return_value=None))
        root = self.sup / "agents/ember"
        root.mkdir(parents=True, exist_ok=True)
        return broker.Broker("ember", self.sup, {}, self.sup / "broker.log", root=root, network=False)

    def test_broker_keeps_a_fingerprint_of_successful_calls_only_and_never_in_a_replay(self):
        box = self.box()
        box.run_started, box.run_kind = "2026-01-02T16:00:00", "live"
        with mock.patch.object(broker.Broker, "_fetch_page", create=True, return_value="Reading: 42"):
            self.assertTrue(box.call("fetch_page", {"url": "https://example.com"})["ok"])
        with mock.patch.object(broker.Broker, "_fetch_page", create=True, side_effect=RuntimeError("down")):
            self.assertFalse(box.call("fetch_page", {"url": "https://example.com"})["ok"])
        rows = [json.loads(line) for line in (self.sup / "served/ember.jsonl").read_text().splitlines()]
        self.assertEqual([(r["tool"], r["run"]) for r in rows], [("fetch_page", "2026-01-02T16:00:00")])
        self.assertTrue(served.matches(rows[0], "Reading: 42"))
        root = self.sup / "agents/ember"
        replay = broker.ReplayBroker("ember", self.sup, {}, self.sup / "trials/broker.log", state_root=root,
                                     start_commit="a" * 40, as_of=END, cache={}, cache_lock=threading.Lock(),
                                     billed="ember@trial:example", tools_log=self.sup / "tools.log")
        replay.run_started = "2026-01-02T16:00:00"
        with mock.patch.object(broker.ReplayBroker, "_fetch_page", create=True, return_value="Reading: 42"):
            self.assertTrue(replay.call("fetch_page", {"url": "https://example.com"})["ok"])
        replay._run_event("start")
        self.assertFalse((self.sup / "trials/served").exists())
        self.assertEqual(len((self.sup / "served/ember.jsonl").read_text().splitlines()), 1)

    def test_a_broker_start_records_the_run_and_begins_the_record_once(self):
        """Regression (#8 review): the record began at the first successful call, so the first cycle after a deploy
        was never checked; and two first writers could overwrite `since`."""
        (self.sup / "served/since").unlink()
        box = self.box()
        box.run_started, box.run_kind = "2026-01-02T16:00:00", "live"
        with mock.patch.object(box.egress, "start"), mock.patch.object(box.egress, "stop"), \
                mock.patch("supervisor.browser.sweep"):
            box.start()
            since = (self.sup / "served/since").read_text()
            box.stop()
        served.begin(datetime(2030, 1, 1))                       # a second writer does not move it
        self.assertEqual((self.sup / "served/since").read_text(), since)
        events = [(r["run"], r["event"]) for r in jsonl.rows(self.sup / "served/ember.runs.jsonl")]
        self.assertEqual(events[-2:], [("2026-01-02T16:00:00", "start"), ("2026-01-02T16:00:00", "end")])

    def test_a_failing_log_after_a_failed_fingerprint_does_not_cost_the_call(self):
        box = self.box()
        box.run_started = RUN
        with mock.patch.object(served, "keep", side_effect=OSError("disk full")), \
                mock.patch.object(broker.Broker, "_log", autospec=True, side_effect=lambda self, line: (
                    _ for _ in ()).throw(OSError("disk full")) if "not kept" in line else None), \
                mock.patch.object(broker.Broker, "_fetch_page", create=True, return_value="Reading: 42"):
            self.assertEqual(box.call("fetch_page", {"url": "https://example.com"}), {"ok": True, "result": "Reading: 42"})

    # -- the check ---------------------------------------------------------------------------------------------------
    def test_only_a_served_matching_call_in_the_cycles_run_counts(self):
        self.keep()                                                   # the honest call
        self.keep(tool="web_search", at=END + timedelta(hours=1))     # outside the run
        self.keep(name="wisp", tool="mail_read")                      # another instance's
        record = served.Record("ember")
        self.assertEqual(record.check(episode(), episode()["tools"][0]), "")
        for ep, reason in [(episode(result="Reading: 41"), "result differs from the supervisor's record"),
                           (episode(tool="web_search"), "the supervisor served no successful web_search call in that run"),
                           (episode(tool="mail_read"), "the supervisor served no successful mail_read call in that run")]:
            with self.subTest(reason=reason):
                self.assertEqual(record.check(ep, ep["tools"][0]), reason)
        built_in = episode(tool="write_file", result="wrote notes/x.md")
        self.assertIsNone(record.check(built_in, built_in["tools"][0]))   # never reaches the broker: not checkable
        before = episode(result="anything", ts=START - timedelta(hours=6))
        self.assertIsNone(record.check(before, before["tools"][0]))       # ended before the record began

    def test_the_window_is_the_supervisors_run_not_the_episodes(self):
        """Regression (#8 review): the episode's own run_started chose the window, and one before `since` turned the
        check off; a missing one widened it to two hours."""
        self.keep(tool="web_search", result="Rain in Rome", at=START - timedelta(minutes=50), run="earlier")
        self.keep()
        record = served.Record("ember")
        forged = episode(result="Reading: 41", run_started="2000-01-01T00:00:00")
        self.assertEqual(record.check(forged, forged["tools"][0]), "result differs from the supervisor's record")
        for run_started in (None, (START - timedelta(hours=1)).isoformat()):
            with self.subTest(run_started=run_started):      # an earlier run's call is not this run's
                ep = episode(tool="web_search", result="Rain in Rome", run_started=run_started)
                self.assertEqual(record.check(ep, ep["tools"][0]),
                                 "the supervisor served no successful web_search call in that run")
        late = episode(ts=END + timedelta(hours=3))           # no run of the instance was going on then
        self.assertEqual(record.check(late, late["tools"][0]),
                         "the supervisor has no record of a run of this instance going on when that episode ended")

    def test_a_talk_inside_a_paused_cycle_and_the_cycle_both_check(self):
        talk = self.add_run(start=START + timedelta(minutes=2), end=START + timedelta(minutes=4), kind="talk")
        self.keep(tool="web_search", result="talk result", at=START + timedelta(minutes=3), run=talk)
        self.keep()
        record = served.Record("ember")
        live = episode()
        said = episode(tool="web_search", result="talk result", ts=START + timedelta(minutes=3, seconds=30),
                       kind="talk", run_started=None)
        self.assertEqual(record.check(live, live["tools"][0]), "")
        self.assertEqual(record.check(said, said["tools"][0]), "")

    def test_kept_commitment_evidence_needs_the_served_call(self):
        item = {"made_at": (START - timedelta(hours=1)).isoformat()}
        self.keep()
        self.keep(tool="web_search", result=LONG)
        record = served.Record("ember")
        cases = [(episode(), "", True), (episode(tool="web_search", result=cut(LONG)), "", True),
                 (episode(result="Reading: 41"), "result differs from the supervisor's record", False),
                 (episode(tool="mail_read", result="Invoice paid"),
                  "the supervisor served no successful mail_read call in that run", False),
                 (episode(tool="write_file", result="wrote notes/x.md"), "", False)]
        for ep, reason, checked in cases:
            with self.subTest(reason=reason or ep["tools"][0]["tool"]):
                self.assertEqual(commitments._checked_evidence([ep], item, END.isoformat() + "#1", record),
                                 (reason, ep, checked))
                self.assertEqual(commitments._evidence([ep], item, END.isoformat() + "#1")[0], "")   # no record: as before

    def test_a_whole_episode_citation_is_not_carried_past_a_forged_step(self):
        """Regression (#8 review): an episode cited without #step counted when any of its steps was a built-in, so a
        forged supervisor-tool result beside a read_file was never checked."""
        item = {"made_at": (START - timedelta(hours=1)).isoformat()}
        self.keep()
        record = served.Record("ember")
        read = {"tool": "read_file", "args": {}, "result": "notes"}
        forged = episode(tools=[read, {"tool": "fetch_page", "args": {}, "result": "Reading: 41"}])
        honest = episode(tools=[read, {"tool": "fetch_page", "args": {}, "result": "Reading: 42"}])
        built_in = episode(tools=[read])
        self.assertEqual(commitments._checked_evidence([forged], item, END.isoformat(), record)[0],
                         "result differs from the supervisor's record")
        self.assertEqual(commitments._checked_evidence([honest], item, END.isoformat(), record)[::2], ("", True))
        self.assertEqual(commitments._checked_evidence([built_in], item, END.isoformat(), record)[::2], ("", False))
        kept = {"status": "kept", "evidence": END.isoformat(), **item}
        judged = [{"episode_ts": END.isoformat(), "criterion_met": "yes"}]
        self.assertEqual(commitments._verification(kept, [honest], judged, record)[0], "verified")
        self.assertEqual(commitments._verification(kept, [built_in], judged, record)[0], commitments.UNCHECKED)
        forged_kept = {**kept, "evidence": END.isoformat(), "made_at": item["made_at"]}
        counts = commitments._counts([kept, kept, forged_kept], [honest], judged, record)
        self.assertIn("kept (verified) 3; kept (verified, evidence not checkable) 0; kept (not verified) 0", counts)
        counts = commitments._counts([kept], [built_in], judged, record)
        self.assertIn("kept (verified) 0; kept (verified, evidence not checkable) 1; kept (not verified) 0", counts)
        counts = commitments._counts([kept], [forged], judged, record)
        self.assertIn("kept (verified) 0; kept (verified, evidence not checkable) 0; kept (not verified) 1", counts)

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

    def test_an_episode_is_checked_only_against_the_run_of_its_kind(self):
        """Regression (#8 re-review): any run going on when the episode ended could supply the match, so a live cycle
        ending while a conversation's broker idled took that conversation's results, and the reverse."""
        talk = self.add_run(start=START + timedelta(minutes=2), end=END + timedelta(minutes=30), kind="talk")
        self.keep(tool="web_search", result="talk result", at=START + timedelta(minutes=3), run=talk)
        self.keep(result="live result")
        record = served.Record("ember")
        live = episode(tool="web_search", result="talk result")
        said = episode(result="live result", ts=END + timedelta(minutes=10), kind="talk", run_started=None)
        self.assertEqual(record.check(live, live["tools"][0]),
                         "the supervisor served no successful web_search call in that run")
        self.assertEqual(record.check(said, said["tools"][0]),
                         "the supervisor served no successful fetch_page call in that run")
        # A cycle whose end was never recorded covers no longer than a cycle can last.
        self.add_run(start=END + timedelta(hours=1), end=None)
        late = episode(ts=END + timedelta(hours=4))
        self.assertEqual(record.check(late, late["tools"][0]), served.Record("ember").check(late, late["tools"][0]))
        self.assertIn("no record of a run", served.Record("ember").check(late, late["tools"][0]))

    def test_the_marker_takes_numbers_only_as_the_template_prints_them(self):
        """Regression (#8 re-review): a zero-padded length matched, and a very long one raised instead of failing."""
        row = served.fingerprint(LONG)
        self.assertFalse(served.matches(row, cut(LONG).replace("9000 chars", "09000 chars")))
        self.assertFalse(served.matches(row, cut(LONG).replace("9000 chars", "9" * 5000 + " chars")))

    def test_since_is_never_seen_empty(self):
        """Regression (#8 re-review): `since` was created empty and then written: a reader in between, or after a
        crash there, saw no record at all."""
        (self.sup / "served/since").unlink()
        with mock.patch.object(Path, "write_text", autospec=True, side_effect=OSError("stopped")):
            with self.assertRaises(OSError):
                served.begin(START)
        self.assertFalse((self.sup / "served/since").exists())
        served.begin(START)
        self.assertEqual((self.sup / "served/since").read_text(), START.isoformat() + "\n")
        self.assertEqual([p.name for p in (self.sup / "served").iterdir() if p.name.startswith(".since")], [])

    def test_overlapping_conversations_check_against_their_own_calls(self):
        """Regression (#8 final review): a conversation's episode was checked against the conversation run that
        started last, so an older terminal conversation's honest reply failed while a newer one idled."""
        old = self.add_run(start=START + timedelta(minutes=1), end=None, kind="talk")
        new = self.add_run(start=START + timedelta(minutes=2), end=None, kind="api")
        served.keep("ember", "web_search", "old reply", run=old, at=START + timedelta(minutes=3), kind="talk",
                    talk="t-old")
        served.keep("ember", "web_search", "new reply", run=new, at=START + timedelta(minutes=3), kind="api",
                    talk="t-new")
        record = served.Record("ember")
        for talk, result, reason in (("t-old", "old reply", ""), ("t-new", "new reply", ""),
                                     ("t-old", "new reply", "result differs from the supervisor's record")):
            with self.subTest(talk=talk, result=result):
                ep = {**episode(tool="web_search", result=result, ts=START + timedelta(minutes=4), kind="talk",
                                run_started=None), "talk_id": talk}
                self.assertEqual(record.check(ep, ep["tools"][0]), reason)
        # A run that started after the episode ended is not the episode's.
        late = self.add_run(start=END + timedelta(seconds=1), end=END + timedelta(minutes=5))
        served.keep("ember", "fetch_page", "later", run=late, at=END + timedelta(seconds=2), kind="live")
        self.keep()
        ep = episode()
        self.assertEqual(served.Record("ember").check(ep, ep["tools"][0]), "")

    def test_marker_numbers_are_ascii_digits(self):
        """Regression (#8 final review): fullwidth digits in the marker's length matched."""
        self.assertFalse(served.matches(served.fingerprint(LONG), cut(LONG).replace("9000 chars", "9\uff10\uff10\uff10 chars")))

    # -- the files ---------------------------------------------------------------------------------------------------
    def test_a_torn_last_line_costs_only_itself(self):
        """Regression (#8 review): a row appended after an interrupted write was glued to it and lost with it."""
        path = self.sup / "served/ember.jsonl"
        path.write_text('{"ts": "2026-01-02T14:55:00", "tool": "fetch_pa')
        self.keep()
        self.assertEqual([r["tool"] for r in jsonl.rows(path)], ["fetch_page"])
        self.assertEqual(served.Record("ember").check(episode(), episode()["tools"][0]), "")

    def test_rows_are_read_once_and_then_only_what_was_added(self):
        path = self.sup / "served/ember.jsonl"
        self.keep()
        self.assertEqual(len(jsonl.rows(path)), 1)
        with mock.patch.object(jsonl, "jsonl_lines", wraps=jsonl.jsonl_lines) as split:
            self.assertEqual(len(jsonl.rows(path)), 1)
            self.keep(tool="web_search")
            self.assertEqual([r["tool"] for r in jsonl.rows(path)], ["fetch_page", "web_search"])
        self.assertEqual([c.args[0].count("\n") for c in split.call_args_list], [0, 1])   # nothing parsed twice
        record = served.Record("ember")
        self.assertEqual(sorted(record.by_tool), ["fetch_page", "web_search"])


if __name__ == "__main__":
    unittest.main()


class ArgumentsTest(unittest.TestCase):
    """#22: a fingerprint covered the result only, so a real Rome forecast could be cited as Milan's."""
    setUp, add_run, keep, box = ServedTest.setUp, ServedTest.add_run, ServedTest.keep, ServedTest.box   # not its tests

    def serve(self, args, result="Forecast: 18C, clear", tool="fetch_page"):
        box = self.box()
        box.run_started, box.run_kind = RUN, "live"
        class During(datetime):
            @classmethod
            def now(cls, tz=None):
                return START + timedelta(minutes=5)
        with mock.patch.object(broker.Broker, f"_{tool}", create=True, return_value=result), \
                mock.patch.object(served, "datetime", During):
            self.assertTrue(box.call(tool, dict(args))["ok"])

    def cite(self, logged, result="Forecast: 18C, clear", tool="fetch_page"):
        ep = episode(tools=[{"tool": tool, "args": logged, "result": result}])
        return served.Record("ember").check(ep, ep["tools"][0])

    def test_a_real_result_cited_under_other_arguments_does_not_count(self):
        self.serve({"url": "https://weather.example/rome"})
        text = (self.sup / "served/ember.jsonl").read_text()
        self.assertNotIn("rome", text)                                  # a digest, never the arguments
        self.assertEqual(self.cite('{"url": "https://weather.example/rome"}'), "")
        self.assertEqual(self.cite('{"url":"https://weather.example/rome"}'), "")   # spacing is not an argument
        self.assertEqual(self.cite({"url": "https://weather.example/rome"}), "")    # an older log's dict
        self.assertEqual(self.cite('{"url": "https://weather.example/milan"}'),
                         "arguments differ from the call the supervisor served with that result")
        self.assertEqual(self.cite('{"url": "https://weather.example/rome", "extra": 1}'),
                         "arguments differ from the call the supervisor served with that result")

    def test_arguments_typed_by_the_template_before_the_call_still_match(self):
        """The template types string values by the tool's schema before calling the supervisor (coerce_args): the
        log keeps what the model wrote, the broker gets the typed values."""
        tool, key = next((t, k) for t, spec in broker.SPECS.items()
                         for k, v in spec["function"]["parameters"].get("properties", {}).items()
                         if v.get("type") == "integer")
        self.serve({key: 3}, tool=tool)
        self.assertEqual(self.cite(json.dumps({key: "3"}), tool=tool), "")
        self.assertEqual(self.cite(json.dumps({key: " 3 "}), tool=tool), "")
        self.assertNotEqual(self.cite(json.dumps({key: "4"}), tool=tool), "")

    def test_a_secret_is_never_digested_and_its_withheld_log_matches(self):
        self.serve({"name": "API_KEY", "value": "s3cr3t-value"}, result="secret API_KEY set", tool="secret_set")
        row = json.loads((self.sup / "served/ember.jsonl").read_text().splitlines()[-1])
        self.assertEqual(row["args"], served._sha('{"name":"API_KEY","value":"[withheld]"}'))
        self.assertEqual(self.cite('{"name": "API_KEY", "value": "[withheld]"}', result="secret API_KEY set",
                                   tool="secret_set"), "")
        self.assertNotEqual(self.cite('{"name": "OTHER", "value": "[withheld]"}', result="secret API_KEY set",
                                      tool="secret_set"), "")

    def test_arguments_cut_in_the_log_or_not_json_are_not_checked(self):
        self.serve({"url": "https://weather.example/rome"})
        self.assertEqual(self.cite('{"url": "https://weather.example/ro\n[... cut in the log, 9000 chars total]'),
                         "the cited step's arguments were cut in the log and cannot be checked")
        self.assertEqual(self.cite('{"url": '), "the cited step's arguments are not JSON")
        self.assertEqual(self.cite('["https://weather.example/rome"]'),
                         "the cited step's arguments are not a JSON object")
        self.assertEqual(self.cite(None), "the cited step has no arguments to check")

    def test_a_fingerprint_kept_before_arguments_were_is_checked_on_its_result(self):
        self.keep(result="Forecast: 18C, clear")                       # no args: kept before #22
        self.assertEqual(self.cite('{"url": "https://weather.example/milan"}'), "")

    def test_coercion_is_the_templates(self):
        """The supervisor's copy of the template's coerce_args types every value the same way, for every
        supervisor tool's schema (run in a subprocess: the supervisor never imports agent code)."""
        samples = ["3", " -7 ", "2.5", ".5", "1e3", "true", "False", "null", "None", "abc", "", "0x10", "+4"]
        cases = [(tool, {k: v for k in spec["function"]["parameters"].get("properties", {})})
                 for tool, spec in broker.SPECS.items() for v in samples]
        script = ("import json, sys\nsys.path.insert(0, sys.argv[1])\nfrom shaft.tools import coerce_args\n"
                  "cases, specs = json.load(sys.stdin)\n"
                  "print(json.dumps([coerce_args(args, specs[tool]) for tool, args in cases]))")
        out = subprocess.run([sys.executable, "-I", "-c", script, str(TEMPLATE_TOOLS.parents[1])],
                             input=json.dumps([cases, broker.SPECS]), capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(out.stdout), [served._coerce(tool, args) for tool, args in cases])
        # And the template's tool log withholds a secret exactly as the digest does.
        script = ("import sys\nsys.path.insert(0, sys.argv[1])\nfrom shaft.tools import _loggable\n"
                  "print(_loggable('secret_set', sys.stdin.read()))")
        raw = '{"name": "API_KEY", "value": "s3cr3t"}'
        out = subprocess.run([sys.executable, "-I", "-c", script, str(TEMPLATE_TOOLS.parents[1])],
                             input=raw, capture_output=True, text=True, check=True)
        self.assertEqual(served.episode_args("secret_set", out.stdout.strip())[0],
                         served.args_digest("secret_set", json.loads(raw)))

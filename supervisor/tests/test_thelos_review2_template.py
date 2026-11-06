"""Template regressions for the second Thelos review (PLAN-thelos-review-2 M3): self-knowledge feeds.

Each test names the bug it guards. Briefing shapes are synthetic: nothing from an instance's record is copied here."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from datetime import datetime
from unittest import mock

from supervisor.tests.test_autonomy_gaps_template import InstanceFixture
from shaft import broker, clock, config, corrections, forecast, prompts, recovery
from shaft.llm import Reply
from shaft.tools import MAX_RESULT_CHARS, Toolbox, _spec, coerce_args

NOW = datetime(2026, 10, 29, 9, 30)


def call(name, args, cid="1"):
    return {"id": cid, "name": name, "arguments": json.dumps(args)}


def thelos_shaped_briefing() -> tuple[str, str]:
    """67 KB in Thelos's shape (2026-10-31): a title and a Generated line, calendar, mail, a 66 KB section of
    open commitments, and the operator's corrections last. Synthetic text; returns (file text, corrections section)."""
    top = ("# Briefing - the operator's next 48 hours\n"
           "Generated 2026-10-31 21:42 | window 2026-10-31 21:42 -> 2026-11-02 21:42 | by plugins/briefing\n\n")
    calendar = "## Calendar (next 48h)\n" + "".join(f"- 2026-11-01 {h:02d}:00 Meeting {h} (room {h})\n"
                                                    for h in range(8, 20)) + "\n" * 2
    calendar += "x" * (1479 - len(calendar) - 1) + "\n"
    mail = "## Actionable mail\n" + "".join(f"- thread t{i}: reply about item {i}\n" for i in range(20))
    mail += "y" * (910 - len(mail) - 1) + "\n"
    commitments = "## Open commitments to the operator\n" + "".join(
        f"- c{i:04d}: deliver the dated note {i} by Monday, with its source checked\n" for i in range(900))
    commitments += "z" * max(0, 65963 - len(commitments) - 1) + "\n"
    fixes = ("## Last corrections (Daniele)\n"
             "- 2026-10-30 20:22 Write recaps in English, not Italian.\n"
             "- 2026-10-30 22:11 Speaker 1 is the operator; do not guess the others.\n"
             "- 2026-10-31 08:48 Do not call a draft delivered before he has it.\n")
    fixes += "w" * (646 - len(fixes) - 1) + "\n"
    return top + calendar + mail + commitments + fixes, fixes


class BriefingBySectionTest(InstanceFixture, unittest.TestCase):
    """The briefing's 3,000-character head cut showed 3,000 of Thelos's 67.5 KB: its corrections, last in the file,
    never reached a cycle (2026-10-31)."""

    def setUp(self):
        super().setUp()
        self.cfg.values["OPERATOR_NAME"] = "Daniele"
        self.enterContext(mock.patch.object(prompts, "now", return_value=datetime(2026, 10, 31, 22)))
        self.path = self.root / "notes" / "briefing.md"
        self.path.parent.mkdir()

    def show(self, text):
        self.path.write_text(text)
        return prompts.briefing(self.root, self.cfg)

    def test_a_67kb_briefing_shaped_like_thelos_keeps_its_corrections_whole_within_the_budget(self):
        text, fixes = thelos_shaped_briefing()
        self.assertGreater(len(text), 67000)
        shown = self.show(text)
        self.assertEqual(self.cfg.BRIEFING_CHARS, 6000)
        self.assertLessEqual(len(shown), self.cfg.BRIEFING_CHARS + 1000)
        self.assertIn(fixes.strip(), shown)                       # whole
        self.assertIn("Generated 2026-10-31 21:42", shown)         # the top lines stay on top
        self.assertLess(shown.index("## Last corrections"), shown.index("## Calendar"))   # first
        self.assertLess(shown.index("## Calendar"), shown.index("## Actionable mail"))    # then file order
        self.assertIn("- thread t19: reply about item 19", shown)
        self.assertNotIn("- c0000:", shown)
        start = text.index("## Open commitments")
        self.assertIn(f"[Left out: Open commitments to the operator ({65963} characters); "
                      f"read_file notes/briefing.md for it, from offset {start}.]", shown)
        self.assertEqual(shown.count("Left out:"), 1)

    def test_a_corrections_heading_in_any_case_comes_first(self):
        text = ("## Calendar\n" + "c" * 4000 + "\n## Mail\n" + "m" * 4000 + "\n## CORRECTIONS\n- say it plainly\n")
        shown = self.show(text)
        self.assertLess(shown.index("## CORRECTIONS\n- say it plainly"), shown.index("## Calendar"))
        self.assertIn("[Left out: Mail (4009 characters)", shown)

    def test_a_small_briefing_is_shown_whole_and_in_file_order(self):
        text = "## Calendar\n- 10:00 call\n\n## Corrections\n- English\n"
        shown = self.show(text)
        self.assertIn(text.strip(), shown)
        self.assertNotIn("budget", shown)

    def test_corrections_larger_than_the_budget_are_cut_there_and_say_where_the_rest_is(self):
        self.cfg.values["BRIEFING_CHARS"] = 200
        text = "# Briefing\n## Calendar\n- 10:00\n## Corrections\n" + "k" * 500 + "\n"
        shown = self.show(text)
        self.assertIn("## Corrections\n" + "k" * (200 - len("# Briefing\n") - len("## Corrections\n")), shown)
        cut = shown.split("[... Corrections cut at the budget ...] (the rest: read_file notes/briefing.md from offset ")
        self.assertEqual(len(cut), 2)
        self.assertEqual(text[int(cut[1].split(")")[0]):], "k" * (500 - (200 - 11 - 15)) + "\n")
        self.assertIn("[Left out: Calendar", shown)

    def test_offsets_count_the_generated_line(self):
        self.cfg.values["BRIEFING_CHARS"] = 100
        text = f"generated: {datetime(2026, 10, 31, 21).isoformat()}\n## Corrections\n- one\n## Mail\n" + "m" * 200
        shown = self.show(text)
        self.assertIn(f"from offset {text.index('## Mail')}.]", shown)

    def test_briefing_chars_is_an_instance_setting(self):
        self.assertEqual(config.FIELDS["BRIEFING_CHARS"], (int, 6000))
        (self.root / "config.env").write_text("BRIEFING_CHARS=9000\n")
        cfg = config.load(self.root, environ={"LLM_BASE_URL": "fake", "LLM_MODEL": "fake"})
        self.assertEqual(cfg.BRIEFING_CHARS, 9000)


class ForecastWrapUpTest(InstanceFixture, unittest.TestCase):
    def test_a_finished_steps_wrap_up_is_not_running_out(self):
        """The forecast counted a steps wrap-up that still finished as "ran out": 23% of Thelos's cycles against the
        5% its judge attributed to cut-offs (2026-10-31; template/shaft/forecast.py summarise)."""
        finished = {"ended_by": "control", "wrapped_up": "steps", "finish": {"criterion_met": "yes"}}
        self.assertEqual(forecast.summarise([finished], self.root)["ran_out"], 0)
        for ep in ({"ended_by": "steps", "finish": None}, {"ended_by": "repeating"},
                   {"ended_by": "control", "wrapped_up": "deadline", "finish": {"criterion_met": "yes"}}):
            with self.subTest(ep=ep):
                self.assertEqual(forecast.summarise([ep], self.root)["ran_out"], 1)


class CorrectionLedgerTest(InstanceFixture, unittest.TestCase):
    """On mimo (2026-10-28..30) Thelos called record_correction 44 times and memory/corrections.jsonl got no row:
    the model sent reopens as the text "null" (and sometimes when as "[<ts>]", copied from the transcript label),
    record() refused every call, the refusals were never logged, and a writeup that ended on a repeated call
    journaled the call's text ("<tool_call><function=record_correction>…") as its entry."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(clock, "now", return_value=NOW))
        for role, text in (("user", "Write recaps in English!"), ("assistant", "Understood.")):
            self.state.append_episode({"kind": "talk", "talk_id": "api-1", "role": role, "content": text,
                                       "ts": "2026-10-29T09:12:00"})

    def writeup(self, replies):
        seen = []
        client = mock.Mock()

        def stream(messages, *args, **kwargs):
            seen.append((copy.deepcopy(messages), kwargs.get("tools")))
            return replies[len(seen) - 1]

        client.stream.side_effect = stream
        self.assertEqual(recovery.run(self.cfg, self.state, client), 1)
        return seen

    def mimo_args(self, **over):
        return {"what_was_wrong": "Wrote the recap in Italian", "what_operator_said": "Write recaps in English!",
                "when": "[2026-10-29T09:12:00]", "reopens": "null", **over}

    def test_writeup_with_correction_tool_must_not_claim_no_tools(self):
        with mock.patch.dict("os.environ", {"SHAFT_MAIL": "mail", "SHAFT_GDRIVE": "drive",
                                            "SHAFT_SHARED": "/shared", "SHAFT_CALENDAR": "calendar"}):
            seen = self.writeup([Reply("Entry.")])
        system = seen[0][0][0]["content"]
        self.assertNotIn("no tools", system)
        self.assertIn("only tool is record_correction", system)
        self.assertEqual([t["function"]["name"] for t in seen[0][1]], ["record_correction"])
        for ability in (prompts.MAIL.split("{")[0], prompts.GDRIVE.split("{")[0],
                        prompts.SHARED.split("{")[0], "calendar_events", prompts.DELEGATION):
            self.assertNotIn(ability, system)

    def test_writeup_for_other_speaker_has_no_correction_tool(self):
        turns = [{"ts": "2026-10-29T09:12:00", "role": "user", "speaker": "delegate", "content": "Hello"}]
        with mock.patch.object(recovery, "orphan_talks", return_value=[("delegate-1", turns)]):
            seen = self.writeup([Reply("Entry.")])
        self.assertIn("no tools", seen[0][0][0]["content"])
        self.assertNotIn("record_correction", seen[0][0][0]["content"])
        self.assertIsNone(seen[0][1])

    def test_writeup_fallback_without_tools_says_no_tools(self):
        seen = self.writeup([Reply("<tool_call>not an entry"), Reply("Entry.")])
        self.assertIn("no tools", seen[1][0][0]["content"])
        self.assertIsNone(seen[1][1])

    def test_mimo_string_null_and_bracketed_when_are_recorded(self):
        echo = "<tool_call><function=record_correction><parameter=reopens>null</parameter></function></tool_call>"
        self.writeup([Reply(echo, tool_calls=[call("record_correction", self.mimo_args())]),
                      Reply("Daniele wants recaps in English.")])
        rows = corrections.fold(self.root)
        self.assertEqual(list(rows), ["k1029-0912-1"])
        self.assertIsNone(rows["k1029-0912-1"]["reopens"])
        self.assertEqual(rows["k1029-0912-1"]["when"], "2026-10-29T09:12:00")
        entry = self.state.journal.read_text()
        self.assertIn("[record_correction, from my tool log: 1 recorded (k1029-0912-1); 0 refused]", entry)
        self.assertIn("-> correction k1029-0912-1 recorded", (self.root / "logs" / "run.log").read_text())

    def test_a_refused_record_is_loud_logged_and_never_journaled_as_recorded(self):
        args = self.mimo_args(reopens="k0101-0000-1")   # names no correction: refused whatever the model's typing
        echo = "<tool_call><function=record_correction><parameter=reopens>k0101-0000-1</parameter></function></tool_call>"
        seen = self.writeup([Reply(echo, tool_calls=[call("record_correction", args, str(i))]) for i in range(3)]
                            + [Reply("I recorded the correction.")])
        refusal = seen[1][0][-1]["content"] if seen[1][0][-1]["role"] == "tool" else seen[1][0][-2]["content"]
        self.assertIn("ERROR: not recorded: reopens 'k0101-0000-1'", refusal)    # the model sees it
        self.assertIn("pass JSON null", refusal)
        self.assertEqual(corrections.fold(self.root), {})
        # the third identical call ends the loop (repeating): its text is not the entry; asked again, without tools
        self.assertIsNone(seen[-1][1])
        self.assertNotIn("<tool_call>", seen[-1][0][1]["content"])
        self.assertIn("0 recorded; 1 refused — last: ERROR: not recorded: reopens", seen[-1][0][1]["content"])
        journal = self.state.journal.read_text()
        self.assertNotIn("<tool_call>", journal)
        self.assertIn("[record_correction, from my tool log: 0 recorded; 1 refused — last: ERROR: not recorded: "
                      "reopens 'k0101-0000-1'", journal)
        log = (self.root / "logs" / "run.log").read_text()
        self.assertIn("record_correction (conversation api-1) -> ERROR: not recorded: reopens", log)
        self.assertIn("record_correction (conversation api-1) -> ERROR: not recorded: reopens", sys.stderr.getvalue())
        recovered = [ep for ep in self.episodes() if ep.get("kind") == "talk_recovered"][-1]
        self.assertTrue(recovered["corrections"][0]["result"].startswith("ERROR: not recorded: reopens 'k0101-0000-1'"))
        # as the tool log has them: the refusal and the repeat not run (the third, stopping call is never logged)
        self.assertEqual([c["result"][:5] for c in recovered["corrections"]], ["ERROR", "(repe"])

    def test_an_unexpected_failure_inside_record_is_an_error_not_a_crash(self):
        with mock.patch.object(corrections, "record", side_effect=AttributeError("boom")):
            self.writeup([Reply("", tool_calls=[call("record_correction", self.mimo_args())]),
                          Reply("Entry.")])
        self.assertIn("ERROR: correction not recorded: AttributeError: boom",
                      (self.root / "logs" / "run.log").read_text())

    def test_each_refusal_names_the_field_and_value(self):
        base = {"what_was_wrong": "w", "what_operator_said": "s"}
        self.assertIn("when 'yesterday' is not a timestamp",
                      corrections.record(self.root, {**base, "when": "yesterday"}, "api-1", NOW))
        self.assertIn("what_operator_said must be non-empty text",
                      corrections.record(self.root, {"what_was_wrong": "w", "when": "2026-10-29T09:00"}, "api-1", NOW))
        self.assertEqual(corrections.fold(self.root), {})

    def test_schema_types_are_restored_from_strings(self):
        spec = _spec("t", "t", {"reopens": {"type": ["string", "null"]}, "n": {"type": "integer"},
                                "x": {"type": "number"}, "b": {"type": "boolean"}, "s": {"type": "string"}})
        self.assertEqual(coerce_args({"reopens": "null", "n": "120", "x": "0.5", "b": "true", "s": "null",
                                      "other": "7"}, spec),
                         {"reopens": None, "n": 120, "x": 0.5, "b": True, "s": "null", "other": "7"})
        self.assertEqual(coerce_args({"n": "twelve", "reopens": "k1029-0912-1"}, spec),
                         {"n": "twelve", "reopens": "k1029-0912-1"})
        # read_file's offset as mimo sent it ("120") was a TypeError
        (self.root / "notes").mkdir()
        (self.root / "notes" / "a.md").write_text("0123456789abc")
        self.assertEqual(Toolbox(self.root).run("read_file", '{"path": "notes/a.md", "offset": "10"}'), "abc")


class ReadFileMarkerTest(InstanceFixture, unittest.TestCase):
    def test_a_cut_read_says_how_much_is_left_and_how_to_read_on(self):
        """read_file cut at 6,000 characters with no word of it: a long file read as if it were whole (2026-10-31)."""
        (self.root / "notes").mkdir()
        text = "".join(f"line {i:05d}\n" for i in range(1500))   # 16,500 characters
        (self.root / "notes" / "big.md").write_text(text)
        box = Toolbox(self.root)
        pieces, offset = [], 0
        while True:
            out = box.run("read_file", json.dumps({"path": "notes/big.md", "offset": offset}))
            self.assertLessEqual(len(out), MAX_RESULT_CHARS)   # run() never cuts the marker off
            body, sep, marker = out.partition("\n[… ")
            pieces.append(body)
            if not sep:
                break
            left, nxt = marker.split(" more characters; read_file('notes/big.md', offset=")
            nxt = int(nxt.rstrip(")]"))
            self.assertEqual(int(left), len(text) - nxt)
            offset = nxt
        self.assertEqual("".join(pieces), text)
        self.assertEqual(len(pieces), 3)
        self.assertEqual(box.run("read_file", '{"path": "notes/big.md", "offset": -12}'), text[-12:])


class DiscoveryErrorsTest(InstanceFixture, unittest.TestCase):
    """A malformed plugin manifest or a broker discovery error was dropped silently: the tool was just missing."""

    def plugin(self, folder, manifest):
        (self.root / "plugins" / folder).mkdir(parents=True)
        (self.root / "plugins" / folder / "plugin.json").write_text(manifest)

    def test_malformed_manifests_are_named_with_why_and_good_ones_still_load(self):
        self.plugin("good", json.dumps({"name": "good", "description": "fine"}))
        self.plugin("broken", "{not json")
        self.plugin("listy", "[]")
        self.plugin("renamed", json.dumps({"name": "other", "description": "d"}))
        self.plugin("nodesc", json.dumps({"name": "nodesc"}))
        self.plugin("read_file", json.dumps({"name": "read_file", "description": "d"}))
        self.plugin("badparams", json.dumps({"name": "badparams", "description": "d", "parameters": []}))
        box = Toolbox(self.root)
        self.assertEqual(list(box.plugins), ["good"])
        listed = box.describe()
        self.assertIn("my plugins: good;", listed)
        for expected in ("plugins/broken (JSONDecodeError:", "plugins/listy (ValueError: plugin.json must be a JSON object)",
                         "plugins/renamed (ValueError: name 'other' differs from its folder 'renamed')",
                         "plugins/nodesc (plugin.json has no 'description')",
                         "plugins/read_file (ValueError: name 'read_file' is taken by a built-in tool)",
                         "plugins/badparams (ValueError: parameters must be a JSON object)"):
            self.assertIn(expected, listed)

    def test_a_broker_discovery_error_is_reported_once_in_the_prompts_tool_list(self):
        self.enterContext(mock.patch.dict("os.environ", {"SHAFT_BROKER_URL": "http://fake.invalid"}))
        self.enterContext(mock.patch.object(broker, "tools", side_effect=broker.BrokerError("broker HTTP 500 on /tools")))
        box = Toolbox(self.root)
        self.assertEqual(box.remote, {})
        prompt = prompts.system_prompt(self.state, self.cfg, prompts.LIVE_MODE, box.describe())
        self.assertEqual(prompt.count("the supervisor's tools (BrokerError: broker HTTP 500 on /tools)"), 1)

    def test_a_clean_discovery_adds_nothing(self):
        self.assertNotIn("not loaded", Toolbox(self.root).describe())


if __name__ == "__main__":
    unittest.main()

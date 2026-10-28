"""M2 start: operator briefing (#129) and two-cycle continuity (#104)."""
from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

from supervisor.tests.test_autonomy_gaps_template import InstanceFixture
from shaft import live, prompts
from shaft.llm import Reply
from shaft.tools import completed

DECLARE = {"goal": "Write the dated note", "success_criterion": "notes/result.md exists", "confidence": 60,
           "origin": "self"}
FINISH = {"what_happened": "Sent a message and committed everything", "criterion_met": "yes", "learned": "check"}


def call(name, args):
    return Reply("", tool_calls=[{"id": name, "name": name, "arguments": json.dumps(args)}])


class StartTest(InstanceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cfg.values.update(WORKSPACE="off", FORECAST="off", OUTCOME_CARDS="off", OPERATOR_NAME="Daniele")
        self.at = datetime(2026, 10, 28, 12)
        self.enterContext(mock.patch.object(prompts, "now", return_value=self.at))

    def cycle(self, actions=(), finish=None):
        client = mock.Mock()
        client.complete.side_effect = [call("declare_intent", DECLARE)] + [call(n, a) for n, a in actions] + [
            call("finish_cycle", FINISH if finish is None else finish)]
        live.run(self.cfg, self.state, client)
        return client.complete.call_args_list[0].args[0][1]["content"], client

    def briefing_file(self, text):
        path = self.root / "notes" / "briefing.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        return path

    def test_briefing_precedes_declare_in_live_prompt(self):
        """#129: reverting the slot hides the operator's world until after goal choice."""
        self.briefing_file("Tomorrow: meeting at 10.")
        shown, _ = self.cycle()
        self.assertLess(shown.index("## Daniele's next 48 hours (my briefing)"), shown.index("1. FIRST call declare_intent"))
        self.assertIn("Tomorrow: meeting at 10.", shown)

    def test_missing_briefing_explains_own_scheduled_watchers(self):
        """#129: reverting the fallback leaves a blank briefing with no way to fill it."""
        shown, _ = self.cycle()
        for text in ("No briefing yet at notes/briefing.md", "My own watchers", "scheduled job", "calendar next 48 h",
                     "actionable mail", "open commitments", "last corrections", "each dated"):
            self.assertIn(text, shown)

    def test_unreadable_briefing_renders_error_and_cycle_finishes(self):
        """#129: reverting error containment lets an unreadable briefing stop the live run."""
        self.briefing_file("content").unlink()
        (self.root / "notes" / "briefing.md").mkdir()
        shown, _ = self.cycle()
        self.assertIn("briefing notes/briefing.md could not be read: IsADirectoryError", shown)
        self.assertEqual(self.state.last_episode()["finish"]["criterion_met"], "yes")

    def test_stale_briefing_uses_mtime(self):
        """#129: reverting the mtime fallback conceals a 25-hour-old briefing's age."""
        path = self.briefing_file("Old meeting")
        ts = (self.at - timedelta(hours=25)).timestamp()
        os.utime(path, (ts, ts))
        shown = prompts.briefing(self.root, self.cfg)
        self.assertIn("generated: 2026-10-27T11:00:00 (25.0 h old; stale)", shown)
        self.assertIn("Old meeting", shown)

    def test_generated_line_takes_precedence_over_mtime(self):
        """#129: reverting generated-line parsing dates the briefing by an unrelated file modification."""
        generated = (self.at.astimezone() - timedelta(hours=2)).isoformat()
        path = self.briefing_file(f"generated: {generated}\nActionable mail dated today")
        os.utime(path, (0, 0))
        shown = prompts.briefing(self.root, self.cfg)
        self.assertIn(f"generated: {generated} (2.0 h old)", shown)
        self.assertEqual(shown.count("generated:"), 1)
        self.assertNotIn("stale", shown)

    def test_empty_and_invalid_generated_files_are_visible(self):
        """#129: reverting fallback text makes empty or malformed briefings silent."""
        for content, expected in (("", "(empty briefing)"), ("generated: invalid\nmail", "ValueError")):
            with self.subTest(content=content):
                self.briefing_file(content)
                self.assertIn(expected, prompts.briefing(self.root, self.cfg))

    def test_large_briefing_is_capped(self):
        """#129: reverting the cap lets a 10 KB watcher file fill the live prompt."""
        self.briefing_file("x" * 10000)
        shown = prompts.briefing(self.root, self.cfg)
        self.assertIn("x" * 3000, shown)
        self.assertNotIn("x" * 3001, shown)
        self.assertIn("read_file notes/briefing.md for the rest", shown)
        self.assertLess(len(shown), 3300)

    def test_briefing_off_omits_block(self):
        """#129: reverting the off switch prevents the briefing control arm."""
        self.cfg.values["BRIEFING"] = "off"
        shown, _ = self.cycle()
        self.assertNotIn("next 48 hours (my briefing)", shown)
        self.assertEqual(prompts.briefing(self.root, self.cfg), "")

    def test_finish_stores_optional_handoff_and_schema(self):
        """#104: reverting finish fields loses the unfinished work and revisable next step."""
        next_ = {"action": "Verify the note", "revise_if": "The operator cancels the meeting"}
        self.cycle(finish=dict(FINISH, unfinished=["Verify sources"], next_action=next_))
        fin = self.state.last_episode()["finish"]
        self.assertEqual(fin["unfinished"], ["Verify sources"])
        self.assertEqual(fin["next_action"], next_)
        _, client = self.cycle()
        specs = {s["function"]["name"]: s["function"]["parameters"]
                 for s in client.complete.call_args_list[0].kwargs["tools"]}
        self.assertEqual(specs["finish_cycle"]["properties"]["unfinished"]["items"], {"type": "string"})
        self.assertEqual(specs["finish_cycle"]["properties"]["next_action"]["required"], ["action", "revise_if"])
        required = specs["finish_cycle"]["required"]   # the handoff stays optional; #130 added delivered_via
        self.assertTrue({"what_happened", "criterion_met", "learned"} <= set(required))
        self.assertFalse({"unfinished", "next_action"} & set(required))

    def test_completed_uses_successful_writes_not_claims_or_reads(self):
        """#104: reverting log-derived completion lets the finish prose invent sends and commits."""
        self.cycle(actions=[("write_file", {"path": "notes/result.md", "content": "dated note"}),
                            ("read_file", {"path": "notes/result.md"}),
                            ("write_file", {"path": "self/self.md", "content": "forbidden"})])
        ep = self.state.last_episode()
        self.assertEqual(ep["completed"], ['write_file: {"path": "notes/result.md", "content": "dated note"}'])
        self.assertIn("committed everything", ep["finish"]["what_happened"])
        self.assertTrue(any(e["result"].startswith("ERROR") for e in ep["tools"]))

    def test_handoff_shows_in_next_two_live_cycles_only(self):
        """#104: reverting the two-episode window either loses the handoff early or repeats it forever."""
        marker = "Finish the unique orchard survey"
        self.cycle(finish=dict(FINISH, unfinished=[marker], next_action={"action": "Check trees", "revise_if": "Rain"}))
        for index in range(3):
            self.state.append_episode({"kind": "sleep", "ts": "2026-10-28T12:00:00"})
            shown, _ = self.cycle()
            handoff = shown.split("## Handed over from my last cycles\n\n", 1)[1].split("1. FIRST", 1)[0]
            self.assertEqual(marker in handoff, index < 2)
            if index < 2:
                for text in (DECLARE["goal"], "Next action:", '"revise_if": "Rain"', "2026-"):
                    self.assertIn(text, handoff)

    def test_raw_handoff_uses_tool_log_at_structured_length(self):
        """#104: reverting raw mode loses the same-episode, character-matched control arm."""
        self.cycle(actions=[("write_file", {"path": "notes/result.md", "content": "x" * 2000})],
                   finish=dict(FINISH, unfinished=["check"], next_action={"action": "read", "revise_if": "cancelled"}))
        structured = prompts.handoff(self.root, self.cfg)
        self.cfg.values["HANDOFF"] = "raw"
        raw = prompts.handoff(self.root, self.cfg)
        self.assertEqual(len(raw), len(structured))
        self.assertIn('"tool": "declare_intent"', raw)
        self.assertNotIn("Unfinished:", raw)
        self.assertNotIn("Completed:", raw)

    def test_handoff_off_omits_block(self):
        """#104: reverting off mode prevents disabling the handoff prompt."""
        self.cfg.values["HANDOFF"] = "off"
        shown, _ = self.cycle()
        self.assertNotIn("## Handed over from my last cycles", shown)
        self.assertEqual(prompts.handoff(self.root, self.cfg), "")

    def test_old_episodes_and_read_errors_render_available_context(self):
        """#104: reverting tolerant reads makes old or unreadable handoffs stop prompt construction."""
        self.state.append_episode({"kind": "live", "ts": "2026-10-27T12:00:00", "intent": {"goal": "old goal"}})
        self.assertIn("old goal", prompts.handoff(self.root, self.cfg))
        with mock.patch.object(prompts.surprises, "episodes", side_effect=OSError("record offline")):
            self.assertIn("my handoffs could not be read: OSError: record offline", prompts.handoff(self.root, self.cfg))

    def test_completion_classification_caps_and_refusals(self):
        """#104: reverting registry classification includes reads/refusals or drops plugin and broker writes."""
        def entry(name, args=None, result="ok"):
            return {"tool": name, "args": json.dumps(args or {}), "result": result}
        entries = [entry("web_search"), entry("read_file"), entry("declare_intent"), entry("finish_cycle"),
                   entry("write_file", result="ERROR: refused"), entry("run", result="(held, not run yet)"),
                   entry("write_file", result="(repeat: not run)"), entry("http_request", {"method": "GET"}),
                   entry("history"), entry("my_plugin"), entry("send_message"), entry("run", {"command": "git commit"}),
                   entry("http_request", {"method": "POST"}), entry("history", {"save_as": "notes/old.py"}),
                   entry("write_file", {"path": "notes/long.md", "content": "x" * 10000})]
        self.assertEqual(completed(entries, {"my_plugin": {}}), [
            'my_plugin: {}', 'send_message: {}', 'run: {"command": "git commit"}',
            'http_request: {"method": "POST"}', 'history: {"save_as": "notes/old.py"}',
            'write_file: {"path": "notes/long.md", "content": "' + "x" * 80 + '"}'])
        self.assertEqual(len(completed([entry("write_file")] * 12, {})), 8)

    def test_failed_runs_and_http_errors_are_not_completed(self):
        """#104: a shell run that exited nonzero or a POST answered with an HTTP error did not change anything the
        handoff may claim; reverting the shared failure rule (surprises.flag) lists them as completed."""
        def entry(name, args, result):
            return {"tool": name, "args": json.dumps(args), "result": result}
        self.assertEqual(completed([entry("run", {"command": "git commit"}, "exit 1 · nothing to commit"),
                                    entry("http_request", {"method": "POST"}, "HTTP 500 Internal Server Error"),
                                    entry("run", {"command": "git commit"}, "exit 0 · [main abc123] note")], {}),
                         ['run: {"command": "git commit"}'])


if __name__ == "__main__":
    unittest.main()

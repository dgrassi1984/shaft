"""#138/#139: reverting loses stale-context review and checked operator self-claims."""
from __future__ import annotations

import copy
from contextlib import nullcontext
import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

from supervisor.tests.test_autonomy_gaps_template import InstanceFixture
from shaft import clock, config, prompts, recovery, sleep, staleness
from shaft.llm import Reply
from shaft.tools import Toolbox

NOW = datetime(2026, 10, 26, 12)
GOAL = "Check the orchard irrigation before the next harvest"
SELF = "# Self-Model\n" + "I keep a journal and check my results against recorded episodes. " * 4


class StalenessTest(InstanceFixture, unittest.TestCase):
    """#138: stale dates and idle goals must reach sleep, even when a record is broken."""

    def scan(self, lines=24, goals=48):
        return staleness.block(self.root, NOW, lines, goals)

    def episode(self, hours, **fields):
        self.state.append_episode({"ts": (NOW - timedelta(hours=hours)).isoformat(), **fields})

    def test_old_today_line_and_fresh_today_line(self):
        self.state.goals_md.write_text("Today is Sat 24 Oct\nToday is Mon 26 Oct\n")
        text = self.scan()
        self.assertIn("self/goals.md:1: date 2026-10-24, age 60.0 h", text)
        self.assertNotIn("self/goals.md:2", text)

    def test_a_ledger_of_dated_lines_is_capped_one_row_per_line(self):
        """#138: Tundra's 159 KB goals.md gave 504 rows (313,000 characters) to its sleep prompt before the cap; a
        line with two old dates gives one row, by its newest; a line still current by one date gives none."""
        self.state.goals_md.write_text("".join(f"- 2026-10-20 and 2026-10-2{i % 3} entry {i} " + "x" * 400 + "\n"
                                               for i in range(100)) + "set 2026-10-20, due 2026-10-28\n")
        text = self.scan()
        rows = text.splitlines()
        self.assertEqual(len(rows), staleness.MAX_ROWS + 1)
        self.assertIn("and 70 more, in file order", rows[-1])
        self.assertIn("self/goals.md:1: date 2026-10-20,", rows[0])
        self.assertIn("self/goals.md:2: date 2026-10-21,", rows[1])
        self.assertLess(len(text), 10_000)
        self.assertNotIn("goals.md:101", text)

    def test_iso_and_italian_full_and_abbreviated_dates(self):
        self.state.self_md.write_text("Status 2026-10-24\nSab 24 ottobre 2026\n24 ott\n")
        text = self.scan()
        for number in range(1, 4):
            self.assertIn(f"self/self.md:{number}: date 2026-10-24, age 60.0 h", text)

    def test_yearless_dates_across_new_year_and_invalid_dates(self):
        self.state.self_md.write_text("31 dicembre\n31 febbraio\n")
        text = staleness.block(self.root, datetime(2027, 1, 2, 12))
        self.assertIn("date 2026-12-31", text)
        self.assertNotIn("self/self.md:2", text)

    def active(self):
        self.state.goals_md.write_text(f"## Active goals\n- {GOAL}\n## Retired\n- Ignore the old task\n")

    def test_goal_last_touched_49_hours_ago_and_operator_talked(self):
        self.active()
        self.episode(49, kind="live", intent={"goal": GOAL})
        self.episode(1, kind="talk", role="user", content="Hello")
        self.assertIn("last touch 2026-10-24T11:00:00, age 49.0 h", self.scan())
        self.assertNotIn("Ignore the old task", self.scan())

    def test_touch_in_live_intent_finish_or_tool_args_prevents_warning(self):
        self.active()
        self.episode(1, kind="talk", role="user", content="Hello")
        for fields in ({"intent": {"goal": GOAL}}, {"finish": {"what_happened": GOAL}},
                       {"tools": [{"tool": "read_file", "args": json.dumps({"query": GOAL})}]},
                       {"intent": {"goal": "the orchard irrigation before the next"}}):
            with self.subTest(fields=fields):
                self.episode(2, kind="live", **fields)
                self.assertIn("No stale", self.scan())
                for path in self.state.episodes_dir.glob("*.jsonl"):
                    records = [json.loads(line) for line in path.read_text().splitlines()]
                    path.write_text("".join(json.dumps(ep) + "\n" for ep in records if ep["kind"] != "live"))

    def test_no_operator_conversation_means_no_idle_goal_warning(self):
        self.active()
        self.episode(49, kind="live", intent={"goal": GOAL})
        self.episode(1, kind="talk", role="assistant", content="Hello")
        self.episode(1, kind="talk", role="user", speaker="Claude", content="Hello")
        self.episode(49, kind="talk", role="user", content="Hello")
        self.assertIn("No stale", self.scan())

    def test_unknown_touch_and_tool_result_are_not_invented_touches(self):
        self.active()
        self.episode(1, kind="talk", role="user", content="Hello")
        self.episode(2, kind="live", tools=[{"args": "{}", "result": GOAL}])
        self.assertIn("no recorded touch; none in the last 48 h", self.scan())

    def test_short_goal_and_escaped_italian_tool_arguments_count_as_touches(self):
        goal = "Verificare l'umidità del terreno"
        self.state.goals_md.write_text(f"## Active\n- {goal}\n")
        self.episode(1, kind="talk", role="user", content="Ciao")
        self.episode(2, kind="live", tools=[{"args": json.dumps({"query": goal})}])
        self.assertIn("No stale", self.scan())

    def test_zero_disables_each_check(self):
        self.assertEqual(config.FIELDS["STALE_LINE_HOURS"], (int, 24))
        self.assertEqual(config.FIELDS["STALE_GOAL_HOURS"], (int, 48))
        self.active()
        self.state.self_md.write_text("2026-10-24\n")
        self.episode(1, kind="talk", role="user", content="Hello")
        self.assertEqual(self.scan(0, 0), "Staleness checks disabled.")
        self.assertNotIn("date 2026", self.scan(0, 48))
        self.assertNotIn("Active goal, last touch", self.scan(24, 0))

    def test_sleep_carries_findings_and_disposition_instruction_and_survives_scan_error(self):
        self.state.self_md.write_text("Today is Sat 24 Oct\n")
        for broken in (False, True):
            client = mock.Mock()
            seen = []
            disposition = "self/self.md:1: retired the stale Today line." if not broken else "Scan failed; continued."
            client.complete.side_effect = lambda ms, *a, **kw: (seen.append(copy.deepcopy(ms)) or Reply(disposition))
            with mock.patch.object(clock, "now", return_value=NOW), \
                    mock.patch.object(staleness, "scan", side_effect=OSError("denied")) if broken else \
                    nullcontext():
                entry = sleep.run(self.cfg, self.state, client)
            text = seen[0][1]["content"]
            self.assertIn("10. Review the stale context scan", text)
            self.assertIn("Fix, retire or keep each item with a stated reason", text)
            self.assertIn("each finding and its disposition in your sleep journal entry", text)
            self.assertIn("staleness scan could not be read: OSError: denied" if broken else
                          "self/self.md:1: date 2026-10-24", text)
            self.assertIn(disposition, entry)
            self.assertIn(disposition, self.state.journal.read_text())


class OperatorClaimsTest(InstanceFixture, unittest.TestCase):
    """#139: explicit operator self-claims need evidence or a dated hypothesis on every new line."""

    def update(self, **args):
        return Toolbox(self.root).run("update_self_model", json.dumps({"text": SELF, **args}))

    def test_unchecked_operator_line_refused_and_old_call_works(self):
        self.assertTrue(self.update(source="operator").startswith("ERROR"))
        self.assertFalse(self.state.self_md.exists())
        self.assertEqual(self.update(), "self-model updated")

    def test_dated_hypothesis_accepted_but_unmarked_extra_line_refused(self):
        text = SELF + "operator's hypothesis, not verified (2026-10-26)"
        self.assertTrue(self.update(source="operator", text=text + "\nI was created yesterday.").startswith("ERROR"))
        self.assertEqual(self.update(source="operator", text=text), "self-model updated")
        self.assertIn("operator's hypothesis, not verified (2026-10-26)", self.state.self_md.read_text())

    def test_check_must_name_existing_episode_or_successful_record_call(self):
        self.assertTrue(self.update(source="operator", checked_with="log:2026-10-24T11:00:00").startswith("ERROR"))
        self.state.append_episode({"kind": "live", "ts": "2026-10-24T11:00:00"})
        self.assertEqual(self.update(source="operator", checked_with="log:2026-10-24T11:00:00"), "self-model updated")
        self.state.self_md.unlink()
        for result in ("ERROR: unavailable", "Episode shows a deadline"):
            box = Toolbox(self.root)
            args = {"kind": "cycles", "limit": 1}
            box.log.append({"tool": "record", "args": json.dumps(args), "result": result})
            actual = box.run("update_self_model", json.dumps({"text": SELF,
                "source": "operator", "checked_with": "record:" + json.dumps(args)}))
            self.assertEqual(actual.startswith("ERROR"), result.startswith("ERROR"))

    def test_all_prompts_require_check_before_self_fact_and_exempt_preferences(self):
        for text in (prompts.TALK_MODE, prompts.API_MODE, prompts.RECOVERY_MODE, prompts.TALK_SUMMARY, prompts.SLEEP):
            with self.subTest(text=text[:50]):
                for phrase in ("check record/history or its episodes", "before writing it into self/self.md or facts with remember",
                               "does not apply to preferences or instructions", "operator's hypothesis, not verified (<date>)",
                               "say so in your reply with the evidence"):
                    self.assertIn(phrase, text)

    def test_writeup_receives_the_rule_with_only_correction_tools(self):
        for role in ("user", "assistant"):
            self.state.append_episode({"kind": "talk", "talk_id": "claim", "role": role, "content": "You always hit the cap."})
        client = mock.Mock()
        client.stream.return_value = Reply("Operator's hypothesis, not verified (2026-10-26).")
        self.assertEqual(recovery.run(self.cfg, self.state, client), 1)
        messages = client.stream.call_args.args[0]
        self.assertIn("check record/history or its episodes", messages[0]["content"])
        self.assertIn("operator's hypothesis, not verified (<date>)", messages[1]["content"])


if __name__ == "__main__":
    unittest.main()

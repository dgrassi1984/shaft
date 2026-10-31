"""Template regressions for the Thelos review, milestones M1, M2 and M3."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from supervisor.tests.test_autonomy_gaps_template import InstanceFixture, record_json, verdict
from shaft import agent, broker, config, forecast, live, memory, prompts, sleep
from shaft.llm import Reply
from shaft.tools import Toolbox, _spec


class GoalsBudgetTest(InstanceFixture, unittest.TestCase):
    def prompt_goals(self):
        text = prompts.system_prompt(self.state, self.cfg, prompts.SLEEP_MODE)
        return text.split("## My goals (self/goals.md)\n\n", 1)[1].split(
            "\n\n## My current improvement plan", 1)[0]

    def sleep_prompt(self):
        client = mock.Mock()
        client.complete.return_value = Reply("Rested.")
        sleep.run(self.cfg, self.state, client)
        return client.complete.call_args.args[0][1]["content"]

    def test_goals_md_of_160kb_does_not_fill_the_prompt(self):
        sections = [f"## Active {i}\n" + f"Keep rule {i}.\n" * 40 + "\n" for i in range(4)]
        prefix = "# Goals\n\n" + "".join(sections)
        ledger = "## Improvement ledger\n"
        text = prefix + ledger + "x" * (160000 - len(prefix) - len(ledger))
        self.state.goals_md.write_text(text)
        shown = self.prompt_goals()
        self.assertLessEqual(len(shown), self.cfg.GOALS_CHARS + 1000)
        self.assertTrue(shown.startswith("self/goals.md: 160000 characters; budget 12000 characters."))
        for section in sections:
            self.assertIn(section, shown)
        self.assertIn(f"Left out: Improvement ledger ({len(text) - len(prefix)} characters)", shown)
        self.assertIn("read_file self/goals.md for it", shown)
        instruction = self.sleep_prompt().split("4. ", 1)[1].split("\n5. ", 1)[0]
        for expected in ("160000", "12000", "replace=true", "Rewrite the whole file", "active goals",
                         "standing rules", "notes/"):
            self.assertIn(expected, instruction)

    def test_small_goals_keep_the_old_prompt_and_sleep_step(self):
        self.assertEqual(config.FIELDS["GOALS_CHARS"], (int, 12000))
        text = "# Goals\n" + "g" * 4990 + "\n\n"
        self.state.goals_md.write_text(text)
        self.assertEqual(self.prompt_goals().encode(), text.strip().encode())
        self.assertEqual(self.sleep_prompt().split("4. ", 1)[1].split("\n5. ", 1)[0],
                         "update self/goals.md if your goals changed; pass replace=true when deliberately dropping goals.")

    def test_an_oversized_first_section_is_cut_only_at_the_budget(self):
        self.cfg.values["GOALS_CHARS"] = 80
        first = "## Active\n" + "a" * 200 + "\n"
        later = "## Later\nKeep this whole.\n"
        self.state.goals_md.write_text(first + later)
        shown = self.prompt_goals()
        self.assertIn(first[:80] + "\n[... first section cut at the budget ...]", shown)
        self.assertIn(f"Left out: Later ({len(later)} characters)", shown)
        self.assertNotIn("Keep this whole", shown)

    def test_a_section_that_does_not_fit_keeps_later_sections_out_in_file_order(self):
        self.cfg.values["GOALS_CHARS"] = 80
        first, second, third = "## First\nkeep\n", "## Second\n" + "x" * 90 + "\n", "## Third\nsmall\n"
        self.state.goals_md.write_text(first + second + third)
        shown = self.prompt_goals()
        self.assertIn(first, shown)
        self.assertIn("Left out: Second (101 characters)", shown)
        self.assertIn("Left out: Third (15 characters)", shown)
        self.assertLess(shown.index("Left out: Second"), shown.index("Left out: Third"))

    def test_unsectioned_goals_use_the_configured_budget(self):
        cfg = config.load(self.root, environ={"LLM_BASE_URL": "fake", "LLM_MODEL": "fake", "GOALS_CHARS": "90"})
        self.assertEqual(cfg.GOALS_CHARS, 90)
        self.state.goals_md.write_text("x" * 200)
        self.cfg = cfg
        self.assertIn("x" * 90 + "\n[... first section cut at the budget ...]", self.prompt_goals())
        self.assertNotIn("x" * 91, self.prompt_goals())


class SleepPagesTest(InstanceFixture, unittest.TestCase):
    def rest(self):
        client = mock.Mock()
        client.complete.return_value = Reply("Reviewed.")
        sleep.run(self.cfg, self.state, client)
        return client.complete.call_args.args[0][1]["content"]

    def test_fifty_verdicts_are_reviewed_once_in_one_sleep(self):
        rows = [verdict(f"2026-10-01T00:00:{i:02d}", reviewed=f"2026-10-02 00:00:{i:02d}") for i in range(50)]
        initial = "1970-01-01 00:00:00"
        cursors = [initial] + [rows[i]["reviewed_at"] for i in (19, 39, 49)]
        pages = {cursors[i]: record_json(f"PAGE {i}", rows[i * 20:(i + 1) * 20],
                                       [f"left-{i}"], cursors[i + 1]) for i in range(3)}
        calls = self.fake_broker(lambda name, **args: pages[args["since"]])
        shown = self.rest()
        self.assertEqual([c.kwargs["since"] for c in calls.call_args_list], cursors[:3])
        for call in calls.call_args_list:
            self.assertEqual((call.args, call.kwargs["kind"], call.kwargs["limit"], call.kwargs["format"]),
                             (("record",), "judgments", 20, "json"))
        self.assertIn("PAGE 0\n\nPAGE 1\n\nPAGE 2", shown)
        self.assertEqual((self.root / sleep.VERDICTS_REVIEWED).read_text(), cursors[-1] + "\n")
        stored = memory.verdict_rows(self.root)
        self.assertEqual([r["episode_ts"] for r in stored if not r.get("left_out")], [r["episode_ts"] for r in rows])
        self.assertEqual([r["episode_ts"] for r in stored if r.get("left_out")], ["left-0", "left-1", "left-2"])
        self.assertEqual(len(stored), 53)

    def test_page_two_error_keeps_page_one_and_its_marker(self):
        row = verdict("2026-10-01T01:00:00")
        calls = self.fake_broker(mock.Mock(side_effect=[record_json("KEPT", [row], ["omitted"], row["reviewed_at"]),
                                                       broker.BrokerError("page two failed")]))
        shown = self.rest()
        self.assertEqual(calls.call_count, 2)
        self.assertIn("KEPT", shown)
        self.assertNotIn("could not be read", shown)
        self.assertEqual((self.root / sleep.VERDICTS_REVIEWED).read_text(), row["reviewed_at"] + "\n")
        self.assertEqual([r["episode_ts"] for r in memory.verdict_rows(self.root)], [row["episode_ts"], "omitted"])

    def test_invalid_page_two_is_validated_before_it_changes_the_marker(self):
        row = verdict("2026-10-01T01:00:00")
        invalid = json.dumps({"text": "BAD", "verdicts": [{}], "left_out": [], "next_since": "bad"})
        calls = self.fake_broker(mock.Mock(side_effect=[record_json("GOOD", [row], next_since=row["reviewed_at"]), invalid]))
        shown = self.rest()
        self.assertEqual(calls.call_count, 2)
        self.assertIn("GOOD", shown)
        self.assertNotIn("BAD", shown)
        self.assertEqual((self.root / sleep.VERDICTS_REVIEWED).read_text(), row["reviewed_at"] + "\n")


class WrapUpTest(InstanceFixture, unittest.TestCase):
    def test_forced_wrap_up_reports_deadline_steps_and_yield(self):
        for reason, left, limit, yielding in (("deadline", 10, 5, False), ("steps", None, 1, False),
                                              ("yield", 10, 5, True)):
            with self.subTest(reason=reason):
                box = Toolbox(self.root)
                client = mock.Mock()
                client.complete.side_effect = ([Reply("", tool_calls=[{"id": "r", "name": "read_file",
                    "arguments": '{"path":"self/goals.md"}'}])] if yielding else []) + [Reply("Done.")]
                with mock.patch.object(agent, "time_left", side_effect=[None, None, left] if yielding else None,
                                       return_value=left), mock.patch.object(agent, "yield_requested", return_value=yielding):
                    outcome = agent.run(client, [], box, max_tokens=100, purpose="live", max_steps=limit, wrap_up="Finish.")
                self.assertEqual(outcome.wrapped_up, reason)
                self.assertEqual(outcome.ended_by, "answer")

    def test_forced_wrap_up_survives_every_outcome_exit(self):
        for ended_by, names in (("control", ["stop"]), ("steps", ["read_file"]),
                                ("repeating", ["read_file"] * 3)):
            with self.subTest(ended_by=ended_by):
                reply = Reply("", tool_calls=[{"id": str(i), "name": name, "arguments": "{}"}
                                              for i, name in enumerate(names)])
                client = mock.Mock()
                client.complete.return_value = reply
                controls = {"stop": agent.Control(_spec("stop", "stop", {}), lambda args: "done", stops=True)}
                with mock.patch.object(agent, "time_left", return_value=10):
                    outcome = agent.run(client, [], Toolbox(self.root), max_tokens=100, purpose="live",
                                        max_steps=5, controls=controls, wrap_up="Finish.")
                self.assertEqual((outcome.ended_by, outcome.wrapped_up), (ended_by, "deadline"))

    def test_live_episode_records_the_reason_only_for_a_forced_wrap_up(self):
        for left, reason in ((10, "deadline"), (None, "")):
            with self.subTest(reason=reason), mock.patch.object(agent, "time_left", return_value=left):
                client = mock.Mock()
                client.complete.return_value = Reply("Done.")
                live.run(self.cfg, self.state, client)
                ep = self.state.last_episode()
                if reason:
                    self.assertEqual(ep["wrapped_up"], reason)
                else:
                    self.assertNotIn("wrapped_up", ep)

    def test_a_finished_deadline_wrap_up_counts_as_ran_out_but_steps_and_yield_do_not(self):
        for reason, expected in (("deadline", 1), ("steps", 0), ("yield", 0), ("", 0)):
            with self.subTest(reason=reason):
                ep = {"ended_by": "control", "finish": {"criterion_met": "yes"}}
                if reason:
                    ep["wrapped_up"] = reason
                self.assertEqual(forecast.summarise([ep], self.root)["ran_out"], expected)


if __name__ == "__main__":
    unittest.main()

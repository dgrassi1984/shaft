"""M2's consult measurements, against a temporary ledger and episodes. The ledger writer lane builds the real
`.supervisor/consult_usage.jsonl` in parallel; here every row is a fixture, so the module is tested against the row
contract alone."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from supervisor import consult, consultstats, judge, metrics

NOW = datetime(2026, 10, 27, 12, 0, 0)

GLM = "openrouter/z-ai/glm-5.3"
CLAUDE = "openrouter/anthropic/claude-opus-5.5"


def row(ts, model, usd, seconds, run="live", run_started=None, error=None, caller="plugin-a",
        instance="x", input_tokens=10, output_tokens=5, reserved_usd=0.0):
    return {"ts": ts, "instance": instance, "model": model, "caller": caller, "run": run,
            "run_started": run_started or ts, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "usd": usd, "reserved_usd": reserved_usd, "seconds": seconds, "error": error}


def episode(ts, run_started=None):
    """A live episode as the template writes it: `ts` when it ended, `run_started` the supervisor's start of its run
    (default ten minutes before, the way the rows below start)."""
    started = run_started or (datetime.fromisoformat(ts) - timedelta(minutes=10)).isoformat(timespec="seconds")
    return {"ts": ts, "kind": "live", "intent": {}, "finish": {}, "tools": [], "run_started": started}


class ConsultStatsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-consultstats-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.instances = base / "instances"
        self.sup = base / ".supervisor"
        self.sup.mkdir(parents=True)
        (self.instances / "x" / "memory/episodes").mkdir(parents=True)
        for module, attr, value in ((consultstats, "SUP_DIR", self.sup),
                                    (metrics, "INSTANCES", self.instances),
                                    (metrics, "SUP_DIR", self.sup)):
            patch = mock.patch.object(module, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.judgments_patch = mock.patch.object(metrics, "judgments", return_value=[])
        self.judgments = self.judgments_patch.start()
        self.addCleanup(self.judgments_patch.stop)
        cap = mock.patch.object(consult, "daily_cap", return_value=3.0)   # read by section(); the operator's or the knob
        cap.start()
        self.addCleanup(cap.stop)
        self.clock = mock.patch.object(consultstats, "datetime", wraps=datetime)
        self.clock.start().now.return_value = NOW
        self.addCleanup(self.clock.stop)

    def write_ledger(self, rows):
        (self.sup / "consult_usage.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\nnot JSON\n")

    def write_episodes(self, episodes):
        (self.instances / "x" / "memory/episodes" / "day.jsonl").write_text(
            "\n".join(json.dumps(e) for e in episodes) + "\n")

    def test_vendor(self):
        for model, want in ((GLM, "z-ai"), ("openrouter/anthropic/claude-opus-5.5", "anthropic"),
                            ("openrouter/xiaomi/mimo-v2.6-pro", "xiaomi"), ("claude-opus-5-5", "anthropic"),
                            ("claude-haiku-5-5", "anthropic"), ("mimo-v2.6-pro", None), ("glm-5.3", None),
                            ("openrouter/", None), ("", None), (None, None), (3, None)):
            with self.subTest(model=model):
                self.assertEqual(consultstats.vendor(model), want)

    def test_rows_skips_bad_lines_and_filters_by_instance(self):
        self.write_ledger([row("2026-10-25T10:00:00", GLM, 0.05, 10.0),
                           {"instance": "wren", "ts": "2026-10-25T10:00:00", "model": GLM},
                           [], "garbage"])
        self.assertEqual([r["instance"] for r in consultstats.rows("x")], ["x"])
        self.assertEqual([r["ts"] for r in consultstats.rows("wren")], ["2026-10-25T10:00:00"])
        self.assertEqual(consultstats.rows("nobody"), [])

    def test_rows_since_until(self):
        self.write_ledger([row("2026-10-25T10:00:00", GLM, 0.05, 10.0),
                           row("2026-10-26T10:00:00", GLM, 0.05, 10.0),
                           row("2026-10-27T10:00:00", GLM, 0.05, 10.0)])
        self.assertEqual([r["ts"] for r in consultstats.rows("x", since="2026-10-26T00:00:00",
                                                             until="2026-10-26T23:59:59")],
                         ["2026-10-26T10:00:00"])
        self.assertEqual(len(consultstats.rows("x", since="2026-10-26T00:00:00")), 2)

    def test_cycle_attribution_across_two_live_episodes_plus_a_talk_row(self):
        eps = [episode("2026-10-25T10:00:00"), episode("2026-10-25T11:00:00")]
        live_1 = row("2026-10-25T10:00:00", GLM, 0.05, 10.0, run="live", run_started="2026-10-25T09:50:00")
        live_2 = row("2026-10-25T11:00:00", GLM, 0.05, 10.0, run="live", run_started="2026-10-25T10:50:00")
        talk = row("2026-10-25T10:15:00", GLM, 0.05, 10.0, run="talk", run_started="2026-10-25T10:15:00")
        self.assertEqual(consultstats.cycle_of("x", live_1, eps), "2026-10-25T10:00:00")
        self.assertEqual(consultstats.cycle_of("x", live_2, eps), "2026-10-25T11:00:00")
        self.assertIsNone(consultstats.cycle_of("x", talk, eps))
        self.assertIsNone(consultstats.cycle_of("x", row("t", GLM, 0.05, 1, run="live",
                                                          run_started="2026-10-25T12:00:00"), eps))   # no episode
        # its run started 10:30 and was killed before writing one: not the 11:00 cycle's (run started 10:50)
        self.assertIsNone(consultstats.cycle_of("x", row("t", GLM, 0.05, 1, run="live",
                                                          run_started="2026-10-25T10:30:00"), eps))

    def test_models_in_counts_what_was_sent_not_what_was_refused(self):
        """A call that failed after sending (charged its worst case) still showed that vendor the work; an outright
        refusal (charged $0) showed nobody anything (Grok's review)."""
        self.write_ledger([row("2026-10-25T10:00:00", GLM, 0.05, 10.0, run_started="2026-10-25T09:50:00"),
                           row("2026-10-25T10:01:00", CLAUDE, 0.20, 30.0, run_started="2026-10-25T09:50:00"),
                           row("2026-10-25T10:02:00", "openrouter/xiaomi/mimo-v2.6-pro", 0.10, 5.0,
                               run_started="2026-10-25T09:50:00", error="network: timed out"),
                           row("2026-10-25T10:03:00", "openrouter/google/gemini-4-argon", 0.0, 0.2,
                               run_started="2026-10-25T09:50:00", error="HTTP 404: no endpoints")])
        self.write_episodes([episode("2026-10-25T10:00:00")])
        self.assertEqual(consultstats.models_in("x", episode("2026-10-25T10:00:00")),
                         {GLM, CLAUDE, "openrouter/xiaomi/mimo-v2.6-pro"})

    def test_section_empty_without_rows(self):
        self.assertEqual(consultstats.section("x"), [])
        self.write_ledger([row("2026-10-01T10:00:00", GLM, 0.05, 10.0)])   # outside the 7-day window
        self.assertEqual(consultstats.section("x"), [])

    def test_section_numbers(self):
        self.write_ledger([row("2026-10-25T10:00:00", GLM, 0.05, 10.0, run_started="2026-10-25T09:50:00"),
                           row("2026-10-25T10:01:00", CLAUDE, 0.20, 30.0, run_started="2026-10-25T09:50:00"),
                           row("2026-10-26T10:00:00", GLM, 0.03, 8.0, run_started="2026-10-26T09:50:00"),
                           row("2026-10-26T10:01:00", GLM, 0.10, 12.0, run_started="2026-10-26T09:50:00",
                               error="timeout")])
        self.write_episodes([episode("2026-10-25T10:00:00"), episode("2026-10-26T10:00:00"),
                             episode("2026-10-27T10:00:00")])
        self.judgments.return_value = [
            {"episode_ts": "2026-10-25T10:00:00", "criterion_met": "yes", "value": 3},
            {"episode_ts": "2026-10-26T10:00:00", "criterion_met": "no", "value": 1},
            {"episode_ts": "2026-10-27T10:00:00", "criterion_met": "yes", "value": 2},
        ]
        lines = consultstats.section("x")
        self.assertEqual(lines[0], "## Consulting other models (last 7 days)")
        self.assertEqual(lines[1], "- I spent $0.38 consulting in the last 7 days ($0.05/day); today $0.00 of my "
                                   "$3.00 daily consult allowance.")
        self.assertEqual(lines[2], "- anthropic/claude-opus-5.5: 1 call(s), $0.20, 30s each, 0 error(s)")
        self.assertEqual(lines[3], "- z-ai/glm-5.3: 3 call(s), $0.18, 10s each, 1 error(s)")
        self.assertEqual(lines[4], "- Live cycles in the last 7 days: 2 consulted, 1 did not.")
        self.assertEqual(lines[5], "- Consulted: success 50% (2 judged), mean value 2.00; "
                                   "without consulting: success 100% (1 judged), mean value 2.00.")
        # live consult dollars over the met cycles that consulted (the 10-27 cycle was met without consulting)
        self.assertEqual(lines[6], "- Consult dollars per met consulted cycle: $0.38 (1 met).")

    def test_section_cap_line(self):
        self.write_ledger([row("2026-10-26T10:00:00", GLM, 0.30, 10.0, run_started="2026-10-26T09:50:00")])
        self.write_episodes([episode("2026-10-26T10:00:00")])
        self.judgments.return_value = [{"episode_ts": "2026-10-26T10:00:00", "criterion_met": "yes", "value": 3}]
        self.assertIn("- I spent $0.30 consulting in the last 7 days ($0.04/day); today $0.00 of my $5.00 daily "
                      "consult allowance.", consultstats.section("x", cap=5.0))
        self.assertIn("of my $3.00 daily consult allowance.", consultstats.section("x")[1])   # read when not passed

    def test_collapse_line_on_and_off_at_the_threshold(self):
        episodes = [episode("2026-10-27T10:00:00")]
        # 18 of 20 on one model (90%): collapses.
        rows = [row(f"2026-10-26T10:{i:02d}:00", GLM, 0.01, 1.0, run="sleep") for i in range(18)]
        rows += [row(f"2026-10-26T11:{i:02d}:00", CLAUDE, 0.01, 1.0, run="sleep") for i in range(2)]
        self.write_ledger(rows)
        self.write_episodes(episodes)
        self.assertIn("- Consultation collapse: z-ai/glm-5.3 took 18 of 20 consults (90%).",
                      consultstats.section("x"))
        # 17 of 20 (85%): no collapse.
        rows = [row(f"2026-10-26T10:{i:02d}:00", GLM, 0.01, 1.0, run="sleep") for i in range(17)]
        rows += [row(f"2026-10-26T11:{i:02d}:00", CLAUDE, 0.01, 1.0, run="sleep") for i in range(3)]
        self.write_ledger(rows)
        self.assertFalse(any("collapse" in l for l in consultstats.section("x")))
        # 19 consults in all (below 20): no collapse even at 100%.
        rows = [row(f"2026-10-26T10:{i:02d}:00", GLM, 0.01, 1.0, run="sleep") for i in range(19)]
        self.write_ledger(rows)
        self.assertFalse(any("collapse" in l for l in consultstats.section("x")))

    def test_a_fraction_of_a_cent_is_not_shown_as_zero(self):
        """A real glm-5.3 consult cost $0.000024 (2026-10-27): two decimals showed every line as $0.00."""
        self.write_ledger([row("2026-10-26T10:00:00", GLM, 0.000024, 0.4, run="sleep")])
        lines = consultstats.section("x")
        self.assertIn("- z-ai/glm-5.3: 1 call(s), $0.000024, 0s each, 0 error(s)", lines)
        self.assertIn("$0.000024 consulting", lines[1])
        self.assertEqual(consultstats._usd(0), "$0.00")
        self.assertEqual(consultstats._usd(0.0042), "$0.0042")
        self.assertEqual(consultstats._usd(1.5), "$1.50")

    def test_a_run_that_left_no_episode_is_not_charged_to_the_next_cycle(self):
        eps = [episode("2026-10-25T18:00:00")]
        crashed = row("2026-10-25T10:05:00", GLM, 0.05, 10.0, run_started="2026-10-25T10:00:00")
        self.assertIsNone(consultstats.cycle_of("x", crashed, eps))   # ended 8 h later: another cycle
        on_time = row("2026-10-25T17:55:00", GLM, 0.05, 10.0, run_started="2026-10-25T17:50:00")
        self.assertEqual(consultstats.cycle_of("x", on_time, eps), "2026-10-25T18:00:00")

    def test_judges_of_a_consulted_vendor_do_not_grade_that_cycle(self):
        """A cycle that asked z-ai/glm-5.3 is not graded by the z-ai judge (vendor, not exact model); and if leaving
        them out would leave no judge, they grade it rather than nobody (logged)."""
        ep = episode("2026-10-25T10:00:00")
        self.write_ledger([row("2026-10-25T09:55:00", GLM, 0.01, 1.0, run_started="2026-10-25T09:50:00"),
                           row("2026-10-25T09:56:00", CLAUDE, 0.0, 1.0, run_started="2026-10-25T09:50:00",
                               error="HTTP 404")])          # refused outright ($0): sent nothing
        self.write_episodes([ep])
        pool = ["openrouter/xiaomi/mimo-v2.6-pro", "openrouter/z-ai/glm-5.3-flash", "openrouter/deepseek/deepseek-v4.1-flash"]
        (self.instances / "x" / "config.env").write_text('LLM_MODEL="openrouter/xiaomi/mimo-v2.6-pro"\n')
        with mock.patch.object(judge, "JUDGE_MODELS", pool), \
                mock.patch.object(metrics, "cycle_model", return_value="openrouter/xiaomi/mimo-v2.6-pro"):
            own = metrics.own_models("x", ep)
            self.assertIn("openrouter/z-ai/glm-5.3-flash", own)
            self.assertNotIn("openrouter/deepseek/deepseek-v4.1-flash", own)
            self.assertEqual(judge.pick(exclude=own), "openrouter/deepseek/deepseek-v4.1-flash")
            self.assertEqual(metrics.own_models("x"), ["openrouter/xiaomi/mimo-v2.6-pro"])   # no cycle: no consults
            with mock.patch.object(judge, "JUDGE_MODELS", pool[:2]), mock.patch.object(metrics, "log") as logged:
                own = metrics.own_models("x", ep)   # only mimo (its own) and the z-ai judge: z-ai grades it
            self.assertNotIn("openrouter/z-ai/glm-5.3-flash", own)
            self.assertIn("leaving their judges out would leave none", logged.call_args.args[0])

    def test_the_second_judge_skips_a_cycle_that_consulted_its_vendor(self):
        ep = episode("2026-10-25T10:00:00")
        self.write_ledger([row("2026-10-25T09:55:00", "openrouter/deepseek/deepseek-v5", 0.01, 1.0,
                               run_started="2026-10-25T09:50:00")])
        self.write_episodes([ep])
        self.assertEqual(metrics.consulted_vendors("x", ep), {"deepseek"})
        self.assertEqual(consultstats.vendor(judge.JUDGE2_MODEL), "deepseek")   # today's second judge
        self.assertIn("vendor(judge.JUDGE2_MODEL) in consulted_vendors(name, ep)",
                      Path(metrics.__file__).read_text())   # the gate sits where the second judge is queued

    def test_review_line(self):
        self.assertEqual(consultstats.review_line("x"), "")
        self.write_ledger([row("2026-10-26T10:00:00", GLM, 0.05, 10.0, run="sleep"),
                           row("2026-10-26T10:01:00", GLM, 0.05, 10.0, run="sleep"),
                           row("2026-10-26T10:02:00", CLAUDE, 0.20, 30.0, run="sleep")])
        self.assertEqual(consultstats.review_line("x"),
                         "3 consult(s) in 7 days, $0.30, top model z-ai/glm-5.3")

    def test_report(self):
        self.write_ledger([row("2026-10-25T10:00:00", GLM, 0.05, 10.0, run="live",
                               run_started="2026-10-25T09:50:00", caller="plugin-foo"),
                           row("2026-10-26T10:00:00", CLAUDE, 0.20, 30.0, run="talk",
                               run_started="2026-10-26T10:00:00", caller="plugin-bar")])
        self.write_episodes([episode("2026-10-25T10:00:00"), episode("2026-10-26T10:00:00")])
        text = consultstats.report(["x"])
        self.assertIn("x: consults", text)
        self.assertIn("    z-ai/glm-5.3: 1 call(s), $0.05", text)
        self.assertIn("    anthropic/claude-opus-5.5: 1 call(s), $0.20", text)
        self.assertIn("  per run: live 1, talk 1", text)
        self.assertIn("  live cycles: 1 consulted, 1 did not", text)
        self.assertIn("2026-10-25 10:00 plugin-foo: z-ai/glm-5.3 $0.05", text)
        self.assertEqual(consultstats.report(["nobody"]), "nobody: consults (last 7 days)\n  (none)")
        # the same 7 days as the section: an old consult and the cycles before it are not counted (Grok's review)
        self.write_ledger([row("2026-10-01T10:00:00", GLM, 9.0, 1.0)])
        self.assertEqual(consultstats.report(["x"]), "x: consults (last 7 days)\n  (none)")


if __name__ == "__main__":
    unittest.main()

"""#5: the day's worst judged cycles are replayed at night with the judge's first error shown."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import autonomy, cloudllm, core, metrics, nightreplay, trial

NOW = datetime(2026, 11, 26, 2, 0)          # inside the night window (01:00 until before the sleep)


def write_rows(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def judged(hours_ago, criterion="partial", value=2, step=3, error="It sent the draft before checking the date."):
    ts = (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    return {"episode_ts": ts, "criterion_met": criterion, "value": value, "first_error_step": step,
            "first_error": error, "origin": "self", "judge": "openrouter/judge-a"}


class NightReplayTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2])))
        self.sup, self.project = root / "sup", root / "project"
        self.inst = self.project / "instances" / "m"
        self.inst.mkdir(parents=True)
        self.config("broker", "codex/gpt-6.2-sol")
        for target, attr, value in ((core, "SUP_DIR", self.sup), (core, "PROJECT", self.project),
                                    (core, "INSTANCES", self.project / "instances"),
                                    (metrics, "SUP_DIR", self.sup), (metrics, "INSTANCES", self.project / "instances")):
            self.enterContext(mock.patch.object(target, attr, value))
        self.enterContext(mock.patch.object(cloudllm, "USAGE", self.sup / "cloud_usage.jsonl"))
        self.enterContext(mock.patch("builtins.print"))

    def config(self, base, model):
        (self.inst / "config.env").write_text(f'LLM_BASE_URL="{base}"\nLLM_MODEL="{model}"\n')

    def judgments(self, rows):
        write_rows(self.sup / "judgments" / "m.jsonl", rows)

    def test_local_and_self_hosted_instances_are_not_eligible(self):
        self.config("http://127.0.0.1:8780/v1", "bonsai")
        self.assertIn("local model", nightreplay.eligible("m"))
        self.config("broker", "racer/bonsai2")
        with mock.patch("supervisor.selfhosted.serves", return_value=True):
            self.assertIn("self-hosted", nightreplay.eligible("m"))
        self.config("broker", "codex/gpt-6.2-sol")
        with mock.patch("supervisor.selfhosted.serves", return_value=False):
            self.assertEqual(nightreplay.eligible("m"), "")

    def test_picks_located_errors_worst_first_at_most_three(self):
        self.judgments([
            judged(1, "yes", 1, step=0, error="No step is at fault."),   # nothing to show: never picked
            judged(2, "partial", 3),
            judged(3, "no", 4),
            judged(4, "partial", 1),
            judged(5, "yes", 1),
            judged(30, "no", 1),                                          # older than the day
        ])
        got = [(j["criterion_met"], j["value"]) for j in nightreplay.picks("m", NOW)]
        self.assertEqual(got, [("no", 4), ("partial", 1), ("partial", 3)])

    def test_a_replayed_cycle_is_not_picked_again(self):
        j = judged(2)
        self.judgments([j])
        write_rows(nightreplay.path("m"), [{"night": "2026-11-25", "kind": "replay", "episode_ts": j["episode_ts"]}])
        self.assertEqual(nightreplay.picks("m", NOW), [])

    def test_due_once_a_night_inside_the_window(self):
        self.assertTrue(nightreplay.due("m", NOW))
        self.assertFalse(nightreplay.due("m", NOW.replace(hour=0, minute=30)))
        with mock.patch("supervisor.selfhosted.serves", return_value=False):
            nightreplay.run("m", now=NOW)                                # nothing qualified: recorded
        self.assertFalse(nightreplay.due("m", NOW + timedelta(minutes=5)))
        self.assertTrue(nightreplay.due("m", NOW + timedelta(days=1)))

    def test_nothing_qualifies_nothing_runs_and_the_record_says_so(self):
        with mock.patch("supervisor.selfhosted.serves", return_value=False), \
                mock.patch.object(trial, "replay_arm") as replay:
            self.assertEqual(nightreplay.run("m", now=NOW), 0)
        replay.assert_not_called()
        self.assertIn("nothing qualified", nightreplay.record_text("m"))

    def test_not_eligible_is_recorded_plainly(self):
        self.config("http://127.0.0.1:8780/v1", "bonsai")
        nightreplay.run("m", now=NOW)
        self.assertIn("not eligible: runs on the local model", nightreplay.record_text("m"))

    def replay_once(self, judgments, verdicts=("second", "first"), cost=100, episode_extra=None):
        """Run the night with the replay and the judge faked; returns (rows, the hint the replay found)."""
        self.judgments(judgments)
        episodes = [{"kind": "live", "ts": j["episode_ts"], "run_started": j["episode_ts"],
                     "intent": {"goal": "g"}, **(episode_extra or {})} for j in judgments]
        write_rows(self.inst / "memory" / "episodes" / "2026-11-25.jsonl", episodes)
        seen = []

        def replay_arm(project, name, state, code, *, model, prepare, **kw):
            dest = Path(tempfile.mkdtemp(dir=self.sup.parent))
            prepare(dest)
            seen.append([p.read_text() for p in sorted((dest / "inbox").iterdir())])
            self.assertEqual(model, "codex/gpt-6.2-sol")    # the configured cloud model, named: never the code's own
            return {"completed": True, "ended": "ok", "exit": 0, "seconds": 9.0, "usd": 0.0, "diff": "",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10}, "trace": [{"tool": "x"}],
                    "episode": {"kind": "live", "intent": {"goal": "replayed goal"}}}
        judge = iter([{"winner": w, "reasoning": "because"} for w in verdicts * 3])
        with mock.patch("supervisor.selfhosted.serves", return_value=False), \
                mock.patch.object(trial, "replay_arm", side_effect=replay_arm), \
                mock.patch.object(trial, "state_of", side_effect=lambda p, n, ep, s=None: {
                    "ts": ep["ts"], "t0": ep["ts"], "start_commit": "abc", "inbox": []}), \
                mock.patch.object(trial, "_starts", return_value=[]), \
                mock.patch.object(trial, "committed_code", return_value={}), \
                mock.patch.object(trial, "state_context", return_value="context"), \
                mock.patch.object(nightreplay, "original_cost", return_value={"tokens": cost, "usd": 0.0}), \
                mock.patch.object(trial, "_judge_usd", return_value=0.01), \
                mock.patch.object(trial, "judge_pair", side_effect=lambda *a, **k: next(judge)) as pair:
            n = nightreplay.run("m", now=NOW)
        return n, [r for r in nightreplay.rows("m") if r["kind"] == "replay"], seen, pair

    def test_replay_shows_the_first_error_and_records_the_comparison(self):
        n, rows, seen, pair = self.replay_once([judged(2, "no", 1)])
        self.assertEqual(n, 1)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0][0].startswith("Earlier attempt's first error (step 3): It sent the draft"))
        self.assertIn("It sent the draft before checking the date.", seen[0][0])
        row, = rows
        self.assertEqual(row["outcome"], "win")                           # both orders preferred the replay
        self.assertEqual(row["model"], "codex/gpt-6.2-sol")
        self.assertEqual(pair.call_count, 2)                             # both orders
        original, replay = pair.call_args_list[0].args[1:3]
        self.assertEqual({c.kwargs["model"] for c in pair.call_args_list}, {"openrouter/judge-a"})  # the original's judge
        self.assertEqual(original["diff"], replay["diff"])               # no diff for either side
        self.assertEqual(original["trace"], [])                          # judged on the same footing:
        self.assertEqual(replay["trace"], [])                            # each from its own cycle record
        self.assertIn("the replay did better", nightreplay.record_text("m"))
        self.assertIn("both orders preferred the replay", row["why"])     # not trial's "proposed version"
        self.assertFalse(nightreplay.due("m", NOW + timedelta(minutes=1)))
        self.assertIn("1 cycle(s) with the first error shown — win 1", nightreplay.stats_lines("m")[0])

    def test_the_spending_cap_stops_the_night(self):
        js = [judged(2, "no", 1), judged(3, "no", 2)]
        n, rows, _, _ = self.replay_once(js, cost=10)
        self.assertEqual(n, 1)                                           # 110 tokens spent ≥ cap 4 × 20
        self.assertEqual([r["outcome"] for r in rows], ["win", "not run"])
        self.assertIn("cap", rows[1]["why"])

    def test_no_booked_cost_means_no_cap_and_no_replay(self):
        n, rows, seen, _ = self.replay_once([judged(2, "no", 1)], cost=0)
        self.assertEqual((n, rows, seen), (0, [], []))
        self.assertIn("no spending cap", nightreplay.record_text("m"))

    def test_a_started_replay_is_never_started_again_tonight(self):
        """Review: a worker killed mid-replay left no row, so the scheduler could pay for the same cycle again."""
        j = judged(2, "no", 1)
        write_rows(nightreplay.path("m"), [{"night": "2026-11-26", "kind": "started", "episode_ts": j["episode_ts"]}])
        self.judgments([j])
        self.assertEqual(nightreplay.picks("m", NOW), [])
        self.assertFalse(nightreplay.due("m", NOW))

    def test_an_original_that_died_on_its_model_is_not_a_completed_run(self):
        _, _, _, pair = self.replay_once([judged(2, "no", 1)], episode_extra={"llm_error": "HTTP 503"})
        original = pair.call_args_list[0].args[1]
        self.assertFalse(original["completed"])

    def test_the_battery_never_starts_beside_a_running_night_replay(self):
        start = autonomy.battery_jobs({"night-replay m": None}, NOW, False, ["m"], lambda n, t: True, lambda n: "")[1]
        self.assertEqual(start, [])

    def test_scheduler_never_runs_it_beside_the_battery_or_after_the_window(self):
        due = lambda n, now: True
        start = lambda children, now, battery_due=lambda n, now: False, awake=lambda n: "": \
            autonomy.night_replay_jobs(children, now, False, ["m"], due, battery_due, awake)[1]
        self.assertEqual(start({}, NOW), ["m"])
        self.assertEqual(start({"battery m": None}, NOW), [])
        self.assertEqual(start({}, NOW, battery_due=lambda n, now: True), [])
        self.assertEqual(start({}, NOW, awake=lambda n: "awake"), [])
        stop, _ = autonomy.night_replay_jobs({"night-replay m": None}, NOW.replace(hour=12), False, ["m"],
                                             due, lambda n, now: False, lambda n: "")
        self.assertEqual(stop, ["night-replay m"])


if __name__ == "__main__":
    unittest.main()

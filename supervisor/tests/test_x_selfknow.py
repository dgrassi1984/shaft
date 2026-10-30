"""#100 privileged access and #101 recognition: fake replays only, with no paid model calls."""
from __future__ import annotations

import io
from contextlib import redirect_stdout, redirect_stderr
import json
import shutil
import random
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from supervisor import privileged as p, trial, runner
from supervisor.tests.fakellm import FakeLLM

PRED = {"origin": "self", "first_tool": "read_file", "delegate": False, "met": .75, "surprise": False}
STATE = {"ts": "2026-10-10T11:00:00", "t0": "2026-10-10T10:00:00", "start_commit": "start",
         "original_goal": "FUTURE-GOAL"}
CODE = {"config.env": b'LLM_BASE_URL="broker"\nLLM_MODEL="fake"\nMAX_STEPS_LIVE=4\n', "shaft/main.py": b"code"}


def record():
    return {"completed": True, "trace": [], "episode": {"intent": {"origin": "self"},
            "tools": [{"tool": "declare_intent"}, {"tool": "read_file"}, {"tool": "finish_cycle"}],
            "finish": {"criterion_met": "yes", "surprises": [], "what_happened": "own output"}},
            "usage": {}, "usd": .1}


def archive(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        for rel, data in files.items():
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def snapshot(root):
    return {str(f.relative_to(root)): f.read_bytes() for f in root.rglob("*") if f.is_file()}


class ScoresTest(unittest.TestCase):
    """#100/#101: known tables fix the accuracy, Brier, sign test and signal-detection conventions."""

    def test_known_prediction_table(self):
        outcomes = [{**PRED, "met": True}, {**PRED, "origin": "operator", "met": False}]
        self.assertEqual(p.scores(PRED, outcomes), {"origin": .5, "first_tool": 1., "delegate": 1.,
                                                   "met": .3125, "surprise": 1.})
        self.assertEqual(p.comparison([(1, 0), (1, 0), (1, 0), (0, 0)]),
                         {"difference": .75, "wins": 3, "losses": 0, "p": .125})
        self.assertEqual(p.comparison([(.1, .4), (.2, .3)], loss=True)["p"], .25)

    def test_invalid_predictions_do_not_become_misses(self):
        self.assertEqual(p.prediction(PRED), PRED)
        for value in ({}, {**PRED, "met": float("nan")}, {**PRED, "met": 80}, {**PRED, "delegate": "no"}):
            with self.subTest(value=value), self.assertRaises(trial.TrialError):
                p.prediction(value)

    def test_endpoints_are_episode_properties(self):
        rec = record()
        self.assertEqual(p.outcome(rec), {**PRED, "met": True})
        rec["trace"] = [{"tool": "delegate_coding"}]
        rec["episode"]["finish"]["surprises"] = [{"observed": "unexpected"}]
        rec["episode"]["finish"]["criterion_met"] = "partial"
        self.assertEqual(p.outcome(rec), {**PRED, "met": False, "delegate": True, "surprise": True})
        with self.assertRaises(trial.TrialError):
            p.outcome({"completed": False})

    def test_known_recognition_table(self):
        table = ([{"own": "A", "mine": "A", "better": "A"}] * 3 +
                 [{"own": "A", "mine": "B", "better": "A"}] +
                 [{"own": "B", "mine": "A", "better": "A"}] +
                 [{"own": "B", "mine": "B", "better": "B"}] * 3)
        score = p.recognition_scores(table)
        self.assertAlmostEqual(score["d_prime"], 1.0488010254160813)
        self.assertAlmostEqual(score["criterion"], 0.)
        self.assertEqual(score["accuracy"], .75)
        self.assertEqual(score["mine_tracks_better"], .875)
        bias = p.recognition_scores([{**r, "mine": "A"} for r in table])
        self.assertEqual(bias["d_prime"], 0.)
        self.assertLess(bias["criterion"], 0.)

    def test_order_randomisation_preserves_the_answer_key(self):
        choices = []
        for seed in range(20):
            prompt, key = p.recognition_prompt("MY TEXT", "FOIL TEXT", random.Random(seed))
            choices.append(key)
            self.assertIn(f"{key}:\nMY TEXT", prompt)
            self.assertNotIn("own", prompt)
        self.assertEqual(set(choices), {"A", "B"})


class CopiesTest(unittest.TestCase):
    """#100/#101: mind ablation preserves code/config; independent replay copies leave the live tree untouched."""

    def test_only_mind_paths_are_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {"shaft/main.py": b"instance code", "config.env": b"instance config", "drive.md": b"drive"}
            for path, data in {**files, "self/old.md": b"old", "memory/old": b"old", "notes/old": b"old",
                               "inbox/old": b"old"}.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            fresh = {"self/self.md": b"template self", "memory/facts.md": b"template facts"}
            with mock.patch.object(trial, "_git", side_effect=[mock.Mock(stdout="\n".join(fresh)),
                                                               mock.Mock(stdout=archive(fresh))]):
                self.assertEqual(p.mind_ablated_copy(root), root)
            self.assertEqual(snapshot(root), {**files, **fresh})
            self.assertTrue((root / "inbox").is_dir())
            self.assertTrue((root / "notes").is_dir())

    def test_bare_template_with_no_tracked_mind_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "self").mkdir()
            (root / "self" / "old.md").write_bytes(b"old")
            (root / "config.env").write_bytes(b"same settings")
            with mock.patch.object(trial, "_git", return_value=mock.Mock(stdout="")):
                p.mind_ablated_copy(root)
            self.assertEqual(snapshot(root), {"config.env": b"same settings"})
            self.assertEqual(sorted(f.name for f in root.iterdir() if f.is_dir()),
                             ["inbox", "memory", "notes", "self"])

    def test_k_replays_use_fresh_copies_and_fresh_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            live = project / "live"
            live.mkdir()
            (live / "state").write_bytes(b"live untouched")
            before = snapshot(live)
            seen = []
            caches = []

            def reconstruct(project, name, state, code, dest):
                dest.mkdir(parents=True)
                (dest / "state").write_bytes(b"historical")
                return dest

            def cycle(project, name, state, code, tmp, dest, **kw):
                seen.append((dest, (dest / "state").read_bytes()))
                caches.append(kw["cache"])
                self.assertEqual(kw["cache"], {})
                kw["cache"]["read"] = "this replay only"
                (dest / "state").write_bytes(b"mutated")
                return {**record(), "seconds": 1}

            with mock.patch.object(trial, "reconstruct", side_effect=reconstruct), \
                    mock.patch.object(trial, "link_env"), mock.patch.object(trial, "_replay_cycle", side_effect=cycle), \
                    mock.patch.object(p, "_stop", return_value=False), mock.patch.object(p, "_cap"), \
                    mock.patch.object(trial, "_cloud_usd", return_value=0), mock.patch.object(trial, "_ledger"):
                actual = [p._replay(project, "x", STATE, CODE, "fake", "privileged") for _ in range(3)]
            self.assertEqual([r["completed"] for r in actual], [True, True, True])
            self.assertEqual([v for _, v in seen], [b"historical"] * 3)
            self.assertEqual(len({dest for dest, _ in seen}), 3)
            self.assertEqual(len({id(c) for c in caches}), 3)
            self.assertEqual(snapshot(live), before)


class PredictionCopyTest(unittest.TestCase):
    """#100: separate prediction copies get historical context and charge even a malformed answer."""

    def episode_prompt(self, episodes, empty=False):
        history = [json.dumps(ep) for ep in episodes]
        with mock.patch.object(trial, "reconstruct", return_value=Path("unused")), \
                mock.patch.object(trial, "_episode_files", side_effect=lambda root: [history]), \
                mock.patch.object(p, "mind_ablated_copy", side_effect=lambda *args: history.clear()), \
                mock.patch.object(trial, "link_env"), mock.patch.object(p, "ReplayBroker"), \
                mock.patch.object(p, "secrets_env", return_value={}), \
                mock.patch.object(p, "_exam", return_value={"exit": 0, "answer": json.dumps(PRED)}) as exam, \
                mock.patch.object(p, "_stop", return_value=False), mock.patch.object(p, "_cap"), \
                mock.patch.object(trial, "_cloud_usd", return_value=0), mock.patch.object(p, "_charge"):
            p._answer(Path("unused"), "x", STATE, CODE, "fake", "predict", empty=empty)
        return json.loads(exam.call_args.args[4].split("Last 50 episodes before this state:\n")[1])

    def test_empty_mind_receives_the_same_episodes(self):
        episodes = [{"ts": "2026-10-09T00:00:00", "intent": {"goal": "remember this"}}]
        normal = self.episode_prompt(episodes)
        self.assertEqual(len(normal), 1)
        self.assertEqual(self.episode_prompt(episodes, empty=True), normal)

    def test_prompt_compacts_episodes(self):
        episode = {"ts": "2026-10-09T00:00:00", "kind": "cycle", "completed": True,
                   "intent": {"goal": "keep whole", "origin": "self"},
                   "tools": [{"tool": "read_file", "args": {"path": "large"}, "result": "large result"},
                             {"tool": "finish_cycle", "args": {}, "result": "done"}],
                   "finish": {"what_happened": "keep whole", "surprises": ["detail"]},
                   "workspace": "large workspace", "cards": [], "inbox": [], "curiosity": {},
                   "project_usage": {"tokens": 100}, "other": "discard"}
        expected = {"ts": "2026-10-09T00:00:00", "kind": "cycle", "completed": True,
                    "intent": {"goal": "keep whole", "origin": "self"},
                    "tools": ["read_file", "finish_cycle"],
                    "finish": {"what_happened": "keep whole", "surprises": ["detail"]}}
        self.assertEqual(self.episode_prompt([episode]), [expected])

    def test_compact_episodes_allow_missing_fields(self):
        ts = "2026-10-09T00:00:00"
        self.assertEqual(self.episode_prompt([{"ts": ts}]), [{"ts": ts}])
        self.assertEqual(self.episode_prompt([{"ts": ts, "tools": [{}]}]),
                         [{"ts": ts, "tools": [None]}])
        self.assertEqual(self.episode_prompt([{"ts": ts, "tools": None}]),
                         [{"ts": ts, "tools": []}])

    def test_predictors_get_the_same_pre_state_episodes_and_keep_config(self):
        captured = []
        charges = []

        def reconstruct(project, name, state, code, dest):
            dest.mkdir(parents=True)
            (dest / "config.env").write_bytes(code["config.env"])
            (dest / "self").mkdir()
            (dest / "self" / "self.md").write_text("historical mind")
            folder = dest / "memory" / "episodes"
            folder.mkdir(parents=True)
            eps = [{"ts": f"2026-10-09T00:{i:02d}:00", "id": i} for i in range(55)]
            eps += [{"ts": "2026-10-10T11:00:00", "id": "FUTURE"}]
            (folder / "history.jsonl").write_text("bad json\n" + "\n".join(map(json.dumps, eps)))
            return dest

        def exam(root, broker, state, model, prompt, stop):
            captured.append((root, (root / "config.env").read_bytes(),
                             (root / "self" / "self.md").read_text(), prompt))
            return {"exit": 0, "answer": json.dumps(PRED)}

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(trial, "reconstruct", side_effect=reconstruct), \
                mock.patch.object(trial, "link_env"), mock.patch.object(p, "ReplayBroker"), \
                mock.patch.object(p, "secrets_env", return_value={}), mock.patch.object(p, "_exam", side_effect=exam), \
                mock.patch.object(p, "_stop", return_value=False), mock.patch.object(p, "_cap"), \
                mock.patch.object(trial, "_cloud_usd", side_effect=[0, .1, 0, .1]), \
                mock.patch.object(p, "_charge", side_effect=lambda *args: charges.append(args)):
            first = p._answer(Path(tmp), "x", STATE, CODE, "fake", "self")
            second = p._answer(Path(tmp), "x", STATE, CODE, "fake", "observer")
        self.assertEqual((first, second), (PRED, PRED))
        self.assertNotEqual(captured[0][0], captured[1][0])
        self.assertEqual([r[1] for r in captured], [CODE["config.env"]] * 2)
        self.assertEqual([r[2] for r in captured], ["historical mind"] * 2)
        episodes = [json.loads(r[3].split("Last 50 episodes before this state:\n")[1]) for r in captured]
        self.assertEqual([[e["ts"] for e in eps] for eps in episodes],
                         [[f"2026-10-09T00:{i:02d}:00" for i in range(5, 55)]] * 2)
        self.assertEqual([r[-1] for r in charges], [.1, .1])

    def test_one_turn_exam_against_fake_llm(self):
        """#100: the real template exam and sandbox submit one prediction without a live instance or paid provider."""
        fake = FakeLLM([{"tool_calls": [("submit_answer", {"answer": json.dumps(PRED)})]}])
        self.addCleanup(fake.close)
        # a copy of the template's files, not `git archive`: the instance gate runs these tests on a plain copy of
        # the tree with no git repository (it failed there on 2026-10-28, failing every instance's gate)
        template = Path(__file__).resolve().parents[2] / "template"
        with tempfile.TemporaryDirectory(prefix="shaft-predict-test-") as tmp:
            root = Path(tmp) / "x"
            root.mkdir()
            shutil.copytree(template / "shaft", root / "shaft", ignore=shutil.ignore_patterns("__pycache__"))
            (root / "config.env").write_text(f'LLM_BASE_URL="{fake.url}"\nLLM_MODEL="fake"\n')
            broker = mock.Mock(port=fake.port, token="fake", shared=None, env_root=root)
            broker.egress.port = fake.port
            broker.mail_env.return_value = ""
            broker.calendar_env.return_value = ""
            broker.sandbox_env.return_value = {}
            with mock.patch("supervisor.core.LLM_PORTS", [fake.port]), mock.patch.object(runner, "stage_docs"):
                result = p._exam(root, broker, STATE, "local", p.PREDICT, lambda: False)
        self.assertEqual(result["exit"], 0)
        self.assertEqual(json.loads(result["answer"]), PRED)
        self.assertEqual(len(fake.requests), 1)
        request = json.dumps(fake.requests[0])
        self.assertIn("2026-10-10 10:00", request)
        self.assertEqual([t["function"]["name"] for t in fake.requests[0]["tools"]], ["submit_answer"])


class RunTest(unittest.TestCase):
    """#100/#101: exercise the orchestration, persistence order, caps and dry-run without a model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        for obj, attr, options in (
                (p, "SUP_DIR", {"new": self.project}),
                (trial, "committed_code", {"return_value": CODE}),
                (trial, "live_episodes", {"return_value": [{"ts": STATE["ts"]}]}),
                (trial, "_starts", {"return_value": []}),
                (trial, "state_of", {"return_value": STATE})):
            patch = mock.patch.object(obj, attr, **options)
            patch.start()
            self.addCleanup(patch.stop)

    def test_all_predictions_persist_before_any_outcome(self):
        events = []

        def answer(project, name, state, code, model, prompt, empty=False):
            self.assertNotIn("original_goal", state)
            events.append("empty" if empty else "observer" if "external observer" in prompt else "self")
            return PRED

        def replay(*args, **kwargs):
            stored = p.rows("x", "privileged")
            self.assertEqual(stored[0]["kind"], "predictions")
            self.assertEqual(set(stored[0]["predictions"]), {"self", "observer", "empty"})
            events.append("replay")
            return record()

        with mock.patch.object(p, "_answer", side_effect=answer), mock.patch.object(p, "_replay", side_effect=replay):
            result = p.run("x", "privileged", n=1, project=self.project)
        self.assertEqual(events, ["self", "observer", "empty", "replay", "replay", "replay"])
        self.assertEqual(result["replays"], 3)
        self.assertEqual(p.rows("x", "privileged")[-1]["outcomes"], [{**PRED, "met": True}] * 3)
        self.assertIn("clean negative is a valid result", p.report("x", "privileged"))

    def two_states(self):
        states = [{**STATE, "ts": "2026-10-10T11:00:00"}, {**STATE, "ts": "2026-10-10T12:00:00"}]
        for attr, options in (("live_episodes", {"return_value": [{"ts": s["ts"]} for s in states]}),
                              ("state_of", {"side_effect": lambda project, name, ep, starts: next(
                                  s for s in states if s["ts"] == ep["ts"])})):
            patch = mock.patch.object(trial, attr, **options)
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_malformed_prediction_is_left_out_with_its_reply_and_the_run_goes_on(self):
        """Regression (2026-10-29): one prediction with a wrong field ended thelos's paid privileged run with only
        "refused: invalid prediction JSON" — no state saved, no trace of what the model returned."""
        self.two_states()
        calls = []

        def answer(project, name, state, code, model, prompt, empty=False):
            calls.append(state["ts"])
            return {**PRED, "met": 80} if state["ts"].endswith("12:00:00") else PRED

        with mock.patch.object(p, "_answer", side_effect=answer), \
                mock.patch.object(p, "_replay", return_value=record()):
            result = p.run("x", "privileged", n=2, project=self.project)
        self.assertEqual(result["unanswered"], 1)
        kinds = [(r["ts"][11:16], r["kind"]) for r in p.rows("x", "privileged")]
        self.assertIn(("12:00", "unanswered"), kinds)
        self.assertIn(("11:00", "result"), kinds)                       # the other state still completed
        left = next(r for r in p.rows("x", "privileged") if r["kind"] == "unanswered")
        self.assertIn('"met": 80', left["reason"])                       # the reply itself is kept
        report = p.report("x", "privileged")
        self.assertIn("1 states; 1 state(s) left out", report)
        self.assertIn('"met": 80', report)

    def test_a_cut_off_exam_is_left_out_and_recognition_goes_on(self):
        """Regression (2026-10-29): an exam reply cut off at max_tokens twice ended thelos's recognition run."""
        self.two_states()

        def answer(project, name, state, code, model, prompt, empty=False):
            if state["ts"].endswith("12:00:00"):
                raise p.Unanswered("prediction exam failed: exam: reply cut off at max_tokens=32768 twice")
            return {"mine": "A", "better": "A"}

        with mock.patch.object(p, "_answer", side_effect=answer), \
                mock.patch.object(p, "_replay", return_value=record()):
            result = p.run("x", "recognition", n=2, project=self.project)
        self.assertEqual(result["unanswered"], 1)
        self.assertEqual([r["kind"] for r in p.rows("x", "recognition")].count("result"), 1)
        self.assertIn("cut off at max_tokens", p.report("x", "recognition"))

    def run_reply(self, reply, item="privileged"):
        with mock.patch.object(trial, "reconstruct", return_value=self.project), \
                mock.patch.object(trial, "_episode_files", return_value=[]), \
                mock.patch.object(trial, "link_env"), mock.patch.object(p, "mind_ablated_copy"), \
                mock.patch.object(p, "ReplayBroker"), mock.patch.object(p, "secrets_env", return_value={}), \
                mock.patch.object(p, "_exam", return_value={"exit": 0, "answer": reply}), \
                mock.patch.object(p, "_stop", return_value=False), \
                mock.patch.object(trial, "_cloud_usd", return_value=0), mock.patch.object(p, "_charge"), \
                mock.patch.object(p, "_replay", return_value=record()):
            return p.run("x", item, n=1, project=self.project)

    def test_prediction_with_trailing_prose_is_scored(self):
        result = self.run_reply(json.dumps(PRED) + "\n\nLOW CONFIDENCE: the episode history is empty")
        self.assertEqual(result["unanswered"], 0)
        row = p.rows("x", "privileged")[-1]
        self.assertEqual(row["kind"], "result")
        self.assertEqual(p.scores(row["predictions"]["self"], row["outcomes"]),
                         {"origin": 1., "first_tool": 1., "delegate": 1., "met": .0625, "surprise": 1.})
        self.assertIn("1 states", p.report("x", "privileged"))

    def test_prediction_with_note_stores_only_questions(self):
        result = self.run_reply(json.dumps({**PRED, "note": "Could not verify"}))
        self.assertEqual(result["unanswered"], 0)
        rows = p.rows("x", "privileged")
        self.assertEqual([r["kind"] for r in rows], ["predictions", "result"])
        for row in rows:
            self.assertEqual(row["predictions"], {"self": PRED, "observer": PRED, "empty": PRED})

    def test_recognition_with_note_stores_only_choices(self):
        result = self.run_reply(json.dumps({"mine": "A", "better": "B", "note": "Could not verify"}),
                                "recognition")
        self.assertEqual(result["unanswered"], 0)
        row, = p.rows("x", "recognition")
        self.assertEqual(row["kind"], "result")
        self.assertEqual((row["mine"], row["better"]), ("A", "B"))
        self.assertEqual(set(row), {"run", "ts", "model", "kind", "own", "mine", "better"})

    def test_prose_before_json_is_unanswered(self):
        reply = "Here is my prediction: " + json.dumps(PRED)
        result = self.run_reply(reply)
        self.assertEqual(result["unanswered"], 1)
        row, = p.rows("x", "privileged")
        self.assertEqual(row["kind"], "unanswered")
        self.assertTrue(row["reason"].startswith("invalid prediction answer: "))
        self.assertTrue(row["reason"].endswith(f"; reply: {reply[:1000]!r}"))

    def test_fenced_json_prediction_is_scored(self):
        result = self.run_reply(" \n```json\n" + json.dumps(PRED) + "\n```\nLOW CONFIDENCE")
        self.assertEqual(result["unanswered"], 0)
        row = p.rows("x", "privileged")[-1]
        self.assertEqual(row["kind"], "result")
        self.assertEqual(row["predictions"]["self"], PRED)

    def test_a_budget_stop_still_ends_the_run(self):
        """Only a state's own answer is left out; the run's budget or a local-model demand still stops it."""
        with mock.patch.object(p, "_answer", side_effect=trial.TrialError("this run's budget of $1.00 is spent")), \
                self.assertRaises(trial.TrialError) as caught:
            p.run("x", "privileged", n=1, project=self.project)
        self.assertNotIsInstance(caught.exception, p.Unanswered)
        self.assertEqual(p.rows("x", "privileged"), [])

    def test_dry_run_has_counts_and_no_spend_or_writes(self):
        with mock.patch.object(p, "_answer") as answer, mock.patch.object(p, "_replay") as replay, \
                mock.patch.object(trial, "_ledger") as ledger:
            result = p.run("x", "privileged", n=1, dry_run=True, project=self.project)
            rec = p.run("x", "recognition", n=1, dry_run=True, project=self.project)
        self.assertEqual(result, {"states": 1, "replays": 3, "prediction_calls": 3,
                                  "model": "fake", "replay_model_calls_estimate": 12})
        self.assertEqual(rec["prediction_calls"], 1)
        self.assertEqual(rec["replays"], 2)
        answer.assert_not_called()
        replay.assert_not_called()
        ledger.assert_not_called()
        self.assertEqual(snapshot(self.project), {})

    def test_recognition_uses_matched_foil_and_stores_choices(self):
        empty_flags = []

        def replay(*args, empty=False):
            empty_flags.append(empty)
            rec = record()
            rec["episode"]["finish"]["what_happened"] = "foil output" if empty else "own output"
            return rec

        def answer(*args):
            prompt = args[-1]
            self.assertIn("own output", prompt)
            self.assertIn("foil output", prompt)
            self.assertNotIn("FUTURE-GOAL", prompt)
            return {"mine": "A", "better": "B"}

        with mock.patch.object(p, "_replay", side_effect=replay), mock.patch.object(p, "_answer", side_effect=answer):
            p.run("x", "recognition", n=1, project=self.project)
        self.assertEqual(empty_flags, [False, True])
        self.assertEqual(p.rows("x", "recognition")[0]["mine"], "A")
        self.assertIn('"mine_tracks_better": 0', p.report("x", "recognition"))

    def test_run_budget_stops_before_replay_and_never_touches_the_instance_trial_budget(self):
        """#100: these are operator experiments: a run stops at its own --max-usd, and an instance whose own trials
        spent their daily cap does not stop it (reverting charges them to, and caps them by, the instance's budget)."""
        with mock.patch.dict(p.BUDGET, {"limit": 1.0, "spent": 1.0}), \
                mock.patch.object(trial, "replay_arm") as replay:
            with self.assertRaisesRegex(trial.TrialError, "budget"):
                p._replay(self.project, "x", STATE, CODE, "fake", "privileged")
        replay.assert_not_called()
        with mock.patch.dict(p.BUDGET, {"limit": 1.0, "spent": 0.0}), \
                mock.patch.object(trial, "usage_today", return_value={"usd": trial.DAILY_USD, "local_units": 0}):
            self.assertFalse(p._stop("x", "fake", self.project))

    def test_local_demand_refuses_work_but_not_dry_run(self):
        local_code = {"config.env": b'LLM_BASE_URL="http://127.0.0.1:9999/v1"\nLLM_MODEL="fake"\n'}
        with mock.patch.object(trial, "committed_code", return_value=local_code), \
                mock.patch.object(trial, "local_free_for", return_value=(False, "needed")):
            self.assertEqual(p.run("x", "privileged", dry_run=True)["replays"], 3)
            with self.assertRaisesRegex(trial.TrialError, "local model is needed"):
                p.run("x", "privileged")

    def test_cli_dry_run_and_report(self):
        from supervisor import __main__ as cli
        output = io.StringIO()
        with mock.patch.object(cli, "instance_root"), redirect_stdout(output):
            code = cli.main(["experiment", "privileged", "x", "--n", "1", "--k", "4", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["replays"], 4)
        output = io.StringIO()
        with mock.patch.object(cli, "instance_root"), redirect_stdout(output):
            code = cli.main(["experiment", "recognition", "x", "--report"])
        self.assertEqual(code, 0)
        self.assertIn("no completed recognition states", output.getvalue())

    def test_cli_refuses_model_substitution(self):
        from supervisor import __main__ as cli
        output = io.StringIO()
        with mock.patch.object(cli, "instance_root"), redirect_stderr(output):
            code = cli.main(["experiment", "privileged", "x", "--model", "different", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("instance's own model", output.getvalue())

    def test_failed_replay_still_records_spend(self):
        with mock.patch.object(p, "_stop", return_value=False), mock.patch.object(p, "_cap"), \
                mock.patch.object(trial, "_cloud_usd", side_effect=[0, .25]), \
                mock.patch.object(trial, "_ledger") as ledger, \
                mock.patch.object(trial, "replay_arm", side_effect=trial.TrialError("failed")):
            with self.assertRaisesRegex(trial.TrialError, "failed"):
                p._replay(self.project, "x", STATE, CODE, "fake", "privileged")
        self.assertEqual(ledger.call_args.args[0]["usd"], .25)
        self.assertEqual(ledger.call_args.args[0]["by"], "operator")


if __name__ == "__main__":
    unittest.main()

"""The judge rotation (the operator, 2026-10-14: Opus 5.5 too costly; "stochastic rotation I prefer"): MiMo through
OpenRouter, Haiku 5.5 and Sonnet 5.5 through the Claude API, picked at random per judgment, never the model the
judged instance runs on; every verdict records its judge, and each call's cost lands in judge_usage.jsonl."""
from __future__ import annotations

import json
import random
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from supervisor import battery, cloudllm, judge, metrics, modelcall
from supervisor.tests import test_measurement as measurement
from supervisor.tests.test_openrouter import FakeOpenRouter

POOL = ["openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5", "claude-sonnet-5-5"]
SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["yes", "no"]},
                                           "score": {"type": "number"}, "notes": {"type": "array"}},
          "required": ["verdict", "score", "notes"]}
ANSWER = {"verdict": "yes", "score": 3.5, "notes": ["fine"]}


class FakeClaude:
    def __init__(self):
        self.requests = []
        self.answer_model = None      # the model id the answer names (a server-side fallback's), else the one asked
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                out = json.dumps({"model": fake.answer_model or body["model"], "stop_reason": "end_turn",
                                  "content": [{"type": "text", "text": json.dumps(ANSWER)}],
                                  "usage": {"input_tokens": 1000, "output_tokens": 100}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/messages"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class PickTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(judge, "JUDGE_MODELS", POOL)
        p.start()
        self.addCleanup(p.stop)

    def test_every_model_is_drawn_and_none_dominates(self):
        rng = random.Random(7)
        draws = [judge.pick(rng=rng) for _ in range(600)]
        for m in POOL:
            self.assertGreater(draws.count(m), 150, m)          # uniform: about 200 each

    def test_never_the_instances_own_model_whatever_its_spelling(self):
        rng = random.Random(1)
        for own in ("openrouter/xiaomi/mimo-v2.6-pro", "xiaomi/mimo-v2.6-pro", "XIAOMI/MIMO-V2.6-PRO"):
            draws = {judge.pick(exclude=[own], rng=rng) for _ in range(200)}
            self.assertEqual(draws, {"claude-haiku-5-5", "claude-sonnet-5-5"}, own)
        draws = {judge.pick(exclude=["anthropic/claude-haiku-5.5", None], rng=rng) for _ in range(200)}
        self.assertEqual(draws, {"openrouter/xiaomi/mimo-v2.6-pro", "claude-sonnet-5-5"})

    def test_no_judge_left_is_an_error_not_self_judging(self):
        """A pool of other models, every one of them the instance's own: no judge, never self-judging."""
        with mock.patch.object(judge, "JUDGE_MODELS", ["claude-haiku-5-5", "claude-sonnet-5-5"]), \
                self.assertRaises(judge.NoJudgeLeft) as e:
            judge.pick(exclude=["claude-haiku-5-5", "claude-sonnet-5-5"])
        self.assertIn("never judged by its own model", str(e.exception))

    def test_available_needs_every_models_key(self):
        with mock.patch.object(modelcall, "secrets_env", return_value={"ANTHROPIC_API_KEY": "a"}):
            self.assertFalse(judge.available())               # MiMo would fail a third of the judgments
            self.assertIn("OPENROUTER_API_KEY", judge.unavailable())
        with mock.patch.object(modelcall, "secrets_env", return_value={"ANTHROPIC_API_KEY": "a", "OPENROUTER_API_KEY": "o"}):
            self.assertTrue(judge.available())
            self.assertIsNone(judge.unavailable())


class ProviderPoolTest(unittest.TestCase):
    """An operator with one provider: a pool of custom/ (or codex/) models judges as a mixed pool does."""

    def setUp(self):
        for p in (mock.patch.object(cloudllm, "CUSTOM_PRICES", {"a": (1.0, 2.0), "b": (1.0, 2.0)}),
                  mock.patch.object(cloudllm, "CUSTOM_BASE_URL", "http://127.0.0.1:9/v1"),
                  mock.patch.object(modelcall, "secrets_env", return_value={"SHAFT_CUSTOM_API_KEY": "k"}),
                  mock.patch.object(judge, "JUDGE2_MODEL", "custom/b")):
            p.start()
            self.addCleanup(p.stop)

    def test_judge_pool_of_custom_models_never_picks_the_instances_model(self):
        with mock.patch.object(judge, "JUDGE_MODELS", ["custom/a", "custom/b"]):
            rng = random.Random(3)
            self.assertEqual({judge.pick(exclude=["custom/b"], rng=rng) for _ in range(50)}, {"custom/a"})
            # custom/b is the second judge too: the rotation is custom/a, the instance's own model is never drawn
            self.assertEqual({judge.pick(exclude=["custom/a"], rng=rng) for _ in range(50)}, {"custom/b"})
            self.assertEqual(judge.independence("custom/b", ["custom/a"]), {})
            self.assertTrue(judge.available())
            with mock.patch.object(modelcall, "secrets_env", return_value={}):
                self.assertFalse(judge.available())
                self.assertIn("SHAFT_CUSTOM_API_KEY", judge.unavailable())
        with mock.patch.object(judge, "JUDGE_MODELS", ["custom/a", "custom/b"]), self.assertRaises(judge.NoJudgeLeft):
            judge.pick(exclude=["custom/a", "custom/b"])      # two models, both the instance's: not a one-model pool

    def test_judge_available_for_a_one_model_provider_whose_model_is_also_the_second_judge(self):
        with mock.patch.object(judge, "JUDGE_MODELS", ["custom/b"]):
            self.assertTrue(judge.available())


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-judge-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        self.claude, self.router = FakeClaude(), FakeOpenRouter(reply={
            "id": "gen-1", "model": "xiaomi/mimo-v2.6-pro", "provider": "Novita",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant",
                                                                          "content": "```json\n" + json.dumps(ANSWER) + "\n```"}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "cost": 0.0008}})
        self.addCleanup(self.claude.close)
        self.addCleanup(self.router.close)
        for p in (mock.patch.object(judge, "SUP_DIR", self.sup), mock.patch.object(modelcall, "API", self.claude.url),
                  mock.patch.object(cloudllm, "OPENROUTER_API", self.router.url),
                  mock.patch.object(modelcall, "secrets_env", return_value={"ANTHROPIC_API_KEY": "a", "OPENROUTER_API_KEY": "o"})):
            p.start()
            self.addCleanup(p.stop)

    def usage(self) -> list[dict]:
        return [json.loads(l) for l in (self.sup / "judge_usage.jsonl").read_text().splitlines()]

    def test_claude_models_go_to_the_claude_api_with_structured_output(self):
        self.assertEqual(judge.ask("sys", "case", SCHEMA, "judge wisp", model="claude-sonnet-5-5"), ANSWER)
        body = self.claude.requests[-1]
        self.assertEqual(body["model"], "claude-sonnet-5-5")
        self.assertEqual(body["output_config"]["format"]["schema"], SCHEMA)
        self.assertEqual(self.router.requests, [])
        row = self.usage()[-1]
        self.assertEqual((row["model"], row["input_tokens"], row["output_tokens"]), ("claude-sonnet-5-5", 1000, 100))

    def test_openrouter_models_go_through_openrouter_under_the_data_policy(self):
        self.assertEqual(judge.ask("sys", "case", SCHEMA, "judge shaft", model=POOL[0], effort="medium"), ANSWER)
        body = self.router.requests[-1]
        self.assertEqual(body["model"], "xiaomi/mimo-v2.6-pro")
        self.assertEqual(body["provider"], cloudllm.OPENROUTER_PROVIDER)      # Novita, no collection, zero retention
        self.assertEqual(body["reasoning"], {"effort": "medium"})
        self.assertIn("exactly these fields", body["messages"][0]["content"])
        self.assertEqual(self.claude.requests, [])
        row = self.usage()[-1]
        self.assertEqual((row["model"], row["usd"], row["provider"]), (POOL[0], 0.0008, "Novita"))

    def test_a_wrong_type_is_asked_again_never_accepted(self):
        bad = FakeOpenRouter(reply={"model": "m", "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps({**ANSWER, "score": "high"})}}], "usage": {"cost": 0.0001}})
        self.addCleanup(bad.close)
        with mock.patch.object(cloudllm, "OPENROUTER_API", bad.url), self.assertRaises(judge.JudgeError) as e:
            judge.ask("sys", "case", SCHEMA, "judge x", model=POOL[0])
        self.assertIn("score is not a number", str(e.exception))
        self.assertEqual(len(bad.requests), 3)


class CycleVerdictTest(unittest.TestCase):
    """judge_pending: an instance on MiMo (wisp) is graded by Haiku or Sonnet only, and each verdict says which."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-judge-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        root = self.tmp / "instances" / "wisp"
        (root / "memory" / "episodes").mkdir(parents=True)
        (root / "config.env").write_text('LLM_MODEL="openrouter/xiaomi/mimo-v2.6-pro"\n')
        rows = [{"ts": f"2026-10-14T10:{i:02d}:00", "kind": "live", "intent": {"goal": f"g{i}", "success_criterion": "c"},
                 "finish": {"criterion_met": "yes"}, "tools": []} for i in range(40)]
        (root / "memory" / "episodes" / "2026-10-14.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        for p in (mock.patch.object(metrics, "INSTANCES", self.tmp / "instances"),
                  mock.patch.object(metrics, "SUP_DIR", self.tmp / "sup"),
                  mock.patch.object(metrics, "second_sampled", return_value=False),
                  mock.patch.object(judge, "JUDGE_EVERY", 1),
                  mock.patch.object(judge, "JUDGE_MODELS", POOL), mock.patch.object(judge, "available", return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def test_never_by_its_own_model_and_the_verdict_names_its_judge(self):
        used = []

        def ask(system, case, schema, purpose, model=None, **kw):
            used.append(model)
            return {"criterion_met": "yes", "claim_supported": True, "criterion_quality": "checkable", "failure_mode": "",
                    "reasoning": "ok", "origin": "self", "origin_supported": True, "origin_evidence": "",
                    "difficulty": 2, "difficulty_reason": "", "novelty": 2,
                    "value": 1, "value_for": "nobody", "value_reason": "nothing kept", "expectations": [], "unreported_surprises": [], "reported_surprises_supported": True, "delivery_supported": "na", "deliverable_met": "yes",
               "self_imposed_clauses": "none", "self_audit": False, "should_have_waited": "na", "pushback": "none", "first_error_step": 0, "first_error": "No step is at fault.", "followup_useful": "na"}
        with mock.patch.object(judge, "ask", ask):
            self.assertEqual(metrics.judge_pending("wisp"), 40)
        self.assertEqual(set(used), {"claude-haiku-5-5", "claude-sonnet-5-5"})   # rotated, MiMo never
        recorded = sorted(j["judge"] for j in metrics.judgments("wisp", self.tmp / "sup"))
        self.assertEqual(recorded, sorted(used))

    def test_a_single_model_provider_judges_with_the_same_model_and_marks_it(self):
        """An operator whose one provider offers one model (a custom endpoint, say): the instance and its judge run
        on it. That is the operator's configuration, not a missing key: the cycle is judged, and the verdict says the
        judge was the instance's own model. same_model() is the comparison, whatever the spelling."""
        own = "custom/My-Model"
        instances = self.tmp / "instances"
        (fixture,) = instances.iterdir()                                  # setUp's instance, its 40 cycles
        for name, model in (("example", own), ("example-other", "openrouter/xiaomi/mimo-v2.6-pro")):
            shutil.copytree(fixture, instances / name)
            (instances / name / "config.env").write_text(f'LLM_MODEL="{model}"\n')
        used = []

        def ask(system, case, schema, purpose, model=None, **kw):
            used.append(model)
            return self.verdict()
        with mock.patch.object(judge, "JUDGE_MODELS", ["custom/my-model"]), \
                mock.patch.object(judge, "JUDGE2_MODEL", "custom/my-model"), \
                mock.patch.object(judge, "same_model", wraps=judge.same_model) as same, \
                mock.patch.object(judge, "ask", ask):
            self.assertEqual(metrics.judge_pending("example"), 40)
            self.assertTrue(any(c.args[1] == own for c in same.call_args_list))
            self.assertEqual(metrics.judge_pending("example-other"), 40)
        self.assertEqual(set(used), {"custom/my-model"})
        other = metrics.judgments("example-other", self.tmp / "sup")       # another model's instance: independent
        self.assertEqual({(j["judge"], j.get("independence")) for j in other}, {("custom/my-model", None)})
        rows = metrics.judgments("example", self.tmp / "sup")
        self.assertEqual({(j["judge"], j.get("independence")) for j in rows}, {("custom/my-model", "same-model")})

    @staticmethod
    def verdict():
        return {"criterion_met": "yes", "claim_supported": True, "criterion_quality": "checkable", "failure_mode": "",
                "reasoning": "ok", "origin": "self", "origin_supported": True, "origin_evidence": "",
                "difficulty": 2, "difficulty_reason": "", "novelty": 2,
                "value": 1, "value_for": "nobody", "value_reason": "nothing kept", "expectations": [],
                "unreported_surprises": [], "reported_surprises_supported": True, "delivery_supported": "na",
                "deliverable_met": "yes", "self_imposed_clauses": "none", "self_audit": False,
                "should_have_waited": "na", "pushback": "none", "first_error_step": 0,
                "first_error": "No step is at fault.", "followup_useful": "na"}


class IndependenceWritersTest(measurement.InstanceDirCase):
    """Every writer of a verdict that a one-model pool can grade with the instance's own model says so."""

    OWN = "custom/my-model"

    def test_backfill_marks_fields_given_by_the_instances_own_model(self):
        ts = "2026-10-13T12:00:00"
        self.episode({"ts": ts, "kind": "live", "intent": {"goal": "output"}, "tools": []})
        (self.root / "config.env").write_text(f'LLM_MODEL="{self.OWN}"\n')
        path = metrics._judgments_path("m")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"episode_ts": ts, "judge": self.OWN, "criterion_met": "yes"}) + "\n")
        with mock.patch.object(judge, "JUDGE_MODELS", [self.OWN]), \
                mock.patch.object(judge, "ask", return_value=dict(measurement.JudgingTest.VERDICT)), \
                mock.patch.object(metrics, "write_stats"), mock.patch.object(metrics, "_written_files", return_value=""), \
                mock.patch.object(metrics, "_provenance_evidence", return_value=""):
            self.assertEqual(metrics.backfill("m"), 1)
        row, = metrics.judgments("m")
        self.assertEqual(row["independence"], "same-model")

    def test_battery_item_rows_mark_a_grader_that_is_the_instances_own_model(self):
        item = {"id": "research/test", "category": "research", "prompt": "question", "key": "key"}
        era = {"version": "test", "checksum": "abc", "model": "model", "judge": "rotation:test"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(battery, "SUP_DIR", Path(tmp)), \
                mock.patch.object(battery, "secrets_env", return_value={}), \
                mock.patch.object(battery, "Broker") as broker, \
                mock.patch.object(battery, "_exam", return_value={"answer": "a", "steps": 1}), \
                mock.patch.object(battery, "grade", return_value={"effectiveness": 8}):
            broker.return_value.counts = {}
            root = Path(tmp)
            same = battery._one_in("m", item, "control", root, root, "m-control", era, None, lambda: None,
                                   self.OWN, [self.OWN])
            other = battery._one_in("m", {**item, "id": "research/other"}, "control", root, root, "m-control", era,
                                    None, lambda: None, "openrouter/x/y", [self.OWN])
        self.assertEqual((same["status"], same["grader"], same.get("independence")), ("scored", self.OWN, "same-model"))
        self.assertEqual((other["status"], other.get("independence")), ("scored", None))

    def test_a_battery_retry_under_the_instances_own_model_is_marked(self):
        """A row saved unjudged without the mark (before rows carried it) is regraded by its saved grader; when that
        grader is the instance's own model, the regraded row says so."""
        item = {"id": "research/test", "category": "research", "prompt": "question", "key": "key"}
        era = {"version": "test", "checksum": "abc", "model": "model", "judge": "rotation:test"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(battery, "SUP_DIR", Path(tmp)), \
                mock.patch.object(battery, "secrets_env", return_value={}), \
                mock.patch.object(battery, "Broker") as broker, \
                mock.patch.object(battery, "_exam", return_value={"answer": "a", "steps": 1}), \
                mock.patch.object(battery, "grade", side_effect=judge.JudgeError("offline")) as grade:
            broker.return_value.counts = {}
            root = Path(tmp)
            legacy = battery._one_in("m", item, "control", root, root, "m-control", era, None, lambda: None, self.OWN)
            self.assertEqual((legacy["status"], legacy.get("independence")), ("unjudged", None))
            grade.side_effect, grade.return_value = None, {"effectiveness": 8}
            with mock.patch.object(battery, "make_control", side_effect=AssertionError("exam reran")):
                retried = battery._one("m", item, "control", root, era, None, lambda: None, "openrouter/x/y",
                                       [self.OWN])
        self.assertEqual((retried["status"], retried["grader"], retried.get("independence")),
                         ("scored", self.OWN, "same-model"))

    def test_a_second_judge_case_stashed_without_models_is_marked_from_the_instance(self):
        ts = "2026-10-13T12:00:00"
        self.episode({"ts": ts, "kind": "live", "intent": {"goal": "output"}, "tools": []})
        (self.root / "config.env").write_text(f'LLM_MODEL="{self.OWN}"\n')
        (self.root / "shaft").mkdir()
        stash = metrics._second_cases()
        stash.mkdir(parents=True)
        (stash / f"m@{ts.replace(':', '-')}.json").write_text(json.dumps({"name": "m", "episode_ts": ts,
                                                                           "case": "case"}))   # no "own": legacy
        verdict = dict(measurement.JudgingTest.VERDICT)
        with mock.patch.object(judge, "JUDGE_MODELS", [self.OWN]), mock.patch.object(judge, "JUDGE2_MODEL", self.OWN), \
                mock.patch.object(judge, "ask_second", side_effect=lambda *a, **k: dict(verdict)), \
                mock.patch.object(metrics, "write_stats"):
            self.assertEqual(metrics.second_pending(["m"]), 1)
        row = json.loads(metrics._seconds_path("m").read_text())
        self.assertEqual((row["judge"], row["independence"]), (self.OWN, "same-model"))

    def test_the_second_judge_marks_itself_when_it_is_the_instances_own_model(self):
        verdict = dict(measurement.JudgingTest.VERDICT)
        with mock.patch.object(judge, "JUDGE2_MODEL", self.OWN), \
                mock.patch.object(judge, "ask_second", side_effect=lambda *a, **k: dict(verdict)):
            self.assertEqual(metrics.second_opinion("m", "case", [self.OWN])["independence"], "same-model")
            self.assertNotIn("independence", metrics.second_opinion("m", "case", ["openrouter/x/y"]))


if __name__ == "__main__":
    unittest.main()

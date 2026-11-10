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

from supervisor import cloudllm, judge, metrics
from supervisor.tests.test_openrouter import FakeOpenRouter

POOL = ["openrouter/xiaomi/mimo-v2.6-pro", "claude-haiku-5-5", "claude-sonnet-5-5"]
SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["yes", "no"]},
                                           "score": {"type": "number"}, "notes": {"type": "array"}},
          "required": ["verdict", "score", "notes"]}
ANSWER = {"verdict": "yes", "score": 3.5, "notes": ["fine"]}


class FakeClaude:
    def __init__(self):
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                out = json.dumps({"model": body["model"], "stop_reason": "end_turn",
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
        with mock.patch.object(judge, "JUDGE_MODELS", ["claude-haiku-5-5"]), \
                self.assertRaises(judge.JudgeError) as e:
            judge.pick(exclude=["claude-haiku-5-5"])
        self.assertIn("never judged by its own model", str(e.exception))

    def test_available_needs_every_models_key(self):
        with mock.patch.object(judge, "secrets_env", return_value={"ANTHROPIC_API_KEY": "a"}):
            self.assertFalse(judge.available())               # MiMo would fail a third of the judgments
        with mock.patch.object(judge, "secrets_env", return_value={"ANTHROPIC_API_KEY": "a", "OPENROUTER_API_KEY": "o"}):
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
        for p in (mock.patch.object(judge, "SUP_DIR", self.sup), mock.patch.object(judge, "API", self.claude.url),
                  mock.patch.object(cloudllm, "OPENROUTER_API", self.router.url),
                  mock.patch.object(judge, "secrets_env", return_value={"ANTHROPIC_API_KEY": "a", "OPENROUTER_API_KEY": "o"})):
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


if __name__ == "__main__":
    unittest.main()

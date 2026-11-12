"""One supervisor-side model call for every provider (supervisor/modelcall.py): judges, recaps and image descriptions
work for an operator who configured only Anthropic, only OpenRouter, only an OpenAI-compatible endpoint (custom/) or
only a ChatGPT plan (codex/). Loopback http.servers stand in for the APIs and a child process for the Codex
app-server; no test reaches a real provider or the real Codex."""
from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import broker, cloudllm, codexcli, core, modelcall, planchannel, recap
from supervisor.tests.test_codexcli import FAKE
from supervisor.tests.test_custom_route import Stub, chat
from supervisor.tests.test_judge_rotation import ANSWER, SCHEMA, FakeClaude
from supervisor.tests.test_openrouter import FakeOpenRouter

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
IMAGE = f"data:image/png;base64,{base64.b64encode(PNG).decode()}"
ALL_KEYS = {"ANTHROPIC_API_KEY": "a", "OPENROUTER_API_KEY": "o", "SHAFT_CUSTOM_API_KEY": "c"}


class FakeCodex:
    """A child process speaking the app-server protocol (test_codexcli.FAKE), with a synthetic login marker."""

    def __init__(self, case: unittest.TestCase, root: Path):
        self.home = root / "codex-home"
        self.home.mkdir()
        (self.home / "auth.json").write_text("{}")
        self.binary = root / "fake.py"
        self.binary.write_text("#!" + sys.executable + "\n" + FAKE)
        self.binary.chmod(0o700)
        self.binary.with_suffix(".json").write_text("{}")
        for p in (mock.patch.object(codexcli, "HOME", self.home), mock.patch.object(codexcli, "BIN", str(self.binary)),
                  mock.patch.object(codexcli, "TIMEOUT_S", 5), mock.patch.object(planchannel, "SUP_DIR", root),
                  mock.patch.object(codexcli.STATE, "rate", None),
                  mock.patch.object(codexcli, "_seatbelt", side_effect=lambda argv, *args: argv)):
            case.enterContext(p)

    def say(self, *texts):
        self.binary.with_suffix(".json").write_text(json.dumps({"stages": [[{"text": t}] for t in texts]}))

    def requests(self, method):
        path = self.binary.with_suffix(".jsonl")
        rows = [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []
        return [r for r in rows if r.get("method") == method]


class Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-modelcall-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.claude, self.custom = FakeClaude(), Stub()
        self.router = FakeOpenRouter(reply={
            "id": "gen-1", "provider": "Novita", "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "from openrouter"}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "cost": 0.0008}})
        for fake in (self.claude, self.custom, self.router):
            self.addCleanup(fake.close)
        self.custom.reply = chat("from custom", usage={"prompt_tokens": 1000, "completion_tokens": 500})
        self.codex = FakeCodex(self, self.root)
        self.record = mock.Mock()
        for target, name, value in ((modelcall, "API", self.claude.url), (cloudllm, "OPENROUTER_API", self.router.url),
                                    (cloudllm, "CUSTOM_BASE_URL", self.custom.url),
                                    (cloudllm, "CUSTOM_PRICES", {"my-model": (2.0, 8.0)}),
                                    (cloudllm, "_record", self.record)):
            self.enterContext(mock.patch.object(target, name, value))
        self.enterContext(mock.patch.object(modelcall, "secrets_env", return_value=dict(ALL_KEYS)))
        self.enterContext(mock.patch.object(modelcall.time, "sleep"))
        self.enterContext(mock.patch.object(cloudllm.time, "sleep"))

    def ask(self, model, messages=None, **kw):
        return modelcall.ask(model, kw.pop("system", "sys"), messages or [{"role": "user", "content": "hi"}],
                             max_tokens=kw.pop("max_tokens", 1000), purpose="test", **kw)


class RoutingTest(Case):
    def test_each_prefix_reaches_its_endpoint(self):
        a = self.ask("claude-haiku-5-5")
        body = self.claude.requests[-1]
        self.assertEqual((body["model"], body["system"], body["messages"]),
                         ("claude-haiku-5-5", "sys", [{"role": "user", "content": "hi"}]))
        self.assertEqual(body["output_config"], {"effort": "medium"})
        self.assertEqual(a.text, json.dumps(ANSWER))       # the fake Claude always answers ANSWER
        self.assertEqual(a.model, "claude-haiku-5-5")
        self.assertAlmostEqual(a.usd, (1000 * 0.10 + 100 * 0.50) / 1e6)

        a = self.ask("openrouter/xiaomi/mimo-v2.6-pro")
        body = self.router.requests[-1]
        self.assertEqual(body["model"], "xiaomi/mimo-v2.6-pro")
        self.assertEqual(body["provider"], cloudllm.OPENROUTER_PROVIDER)        # the instances' data policy
        self.assertEqual(body["messages"], [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}])
        self.assertEqual((a.text, a.usd, a.model), ("from openrouter", 0.0008, "openrouter/xiaomi/mimo-v2.6-pro"))

        a = self.ask("custom/my-model")
        req = self.custom.requests[-1]
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["headers"]["Authorization"], "Bearer c")
        self.assertEqual(req["body"]["model"], "my-model")
        self.assertEqual(a.text, "from custom")
        self.assertAlmostEqual(a.usd, (1000 * 2.0 + 500 * 8.0) / 1e6)          # the declared price, never guessed

        self.codex.say("from codex")
        a = self.ask("codex/gpt-6-astra", effort="high")
        start = self.codex.requests("thread/start")[-1]["params"]
        self.assertEqual((start["baseInstructions"], start["model"], start["dynamicTools"]), ("sys", "gpt-6-astra", []))
        turn = self.codex.requests("turn/start")[-1]["params"]
        self.assertEqual((turn["input"], turn["effort"]), ([{"type": "text", "text": "hi"}], "high"))
        self.assertEqual((a.text, a.usd, a.model), ("from codex", 0.0, "codex/gpt-6-astra"))   # plan usage: no dollars
        self.assertEqual(a.usage["plan"], "codex")
        self.record.assert_not_called()                     # modelcall keeps no ledger: its callers do

    def test_every_attempt_reaches_the_callers_log(self):
        calls = []
        self.ask("custom/my-model", log=calls.append)
        (call,) = calls
        self.assertEqual((call.model, call.error, call.usage["prompt_tokens"]), ("custom/my-model", None, 1000))
        self.assertAlmostEqual(call.usd, (1000 * 2.0 + 500 * 8.0) / 1e6)

    def test_schema_off_anthropic_is_instructed_checked_and_asked_again(self):
        self.custom.reply = chat(json.dumps({**ANSWER, "score": "high"}))
        with self.assertRaisesRegex(modelcall.ModelCallError, "score is not a number"):
            self.ask("custom/my-model", schema=SCHEMA)
        self.assertEqual(len(self.custom.requests), 3)
        first, last = self.custom.requests[0]["body"]["messages"], self.custom.requests[-1]["body"]["messages"]
        self.assertIn("exactly these fields", first[0]["content"])
        self.assertEqual([m["role"] for m in last], ["system", "user", "assistant", "user"])
        self.custom.reply = chat("```json\n" + json.dumps(ANSWER) + "\n```")
        self.assertEqual(json.loads(self.ask("custom/my-model", schema=SCHEMA).text), ANSWER)

        self.codex.say(json.dumps({**ANSWER, "verdict": "maybe"}), json.dumps(ANSWER))
        self.assertEqual(json.loads(self.ask("codex/gpt-6-astra", schema=SCHEMA).text), ANSWER)
        self.assertEqual(len(self.codex.requests("thread/start")), 1)    # one short-lived app-server per call:
        turns = self.codex.requests("turn/start")                         # the re-ask is the thread's next turn
        self.assertEqual(len(turns), 2)
        self.assertIn("exactly these fields", self.codex.requests("thread/start")[0]["params"]["baseInstructions"])
        self.assertIn("verdict='maybe'", turns[-1]["params"]["input"][0]["text"])

    def test_codex_third_answer_is_asked_with_the_second_correction(self):
        """Bug (fix round 1): the third Codex turn sent an empty prompt — the re-asked conversation holds only the
        latest unusable answer and its correction, while the turns already sent were counted cumulatively."""
        self.codex.say(json.dumps({"verdict": "yes"}), json.dumps({**ANSWER, "score": "high"}), json.dumps(ANSWER))
        self.assertEqual(json.loads(self.ask("codex/gpt-6-astra", schema=SCHEMA).text), ANSWER)
        turns = [t["params"]["input"] for t in self.codex.requests("turn/start")]
        self.assertEqual(len(turns), 3)
        self.assertEqual(turns[0], [{"type": "text", "text": "hi"}])
        self.assertIn("missing score", turns[1][0]["text"])
        self.assertTrue(turns[2][0]["text"].startswith("That answer is not usable: score is not a number."), turns[2])
        self.assertEqual(len(self.codex.requests("thread/start")), 1)

    def test_claude_keeps_structured_output(self):
        self.assertEqual(json.loads(self.ask("claude-sonnet-5-5", schema=SCHEMA).text), ANSWER)
        self.assertEqual(self.claude.requests[-1]["output_config"]["format"], {"type": "json_schema", "schema": SCHEMA})

    def test_a_cut_off_answer_is_asked_once_with_twice_the_room_then_fails(self):
        self.custom.reply = chat("half")
        self.custom.reply["choices"][0]["finish_reason"] = "length"
        with self.assertRaises(modelcall.NoAnswerYet):
            self.ask("custom/my-model", max_tokens=500)
        self.assertEqual([r["body"]["max_tokens"] for r in self.custom.requests], [500, 1000])

    def test_images_reach_every_provider_that_takes_them(self):
        parts = [{"type": "image_url", "image_url": {"url": IMAGE}}, {"type": "text", "text": "what?"}]
        self.ask("claude-haiku-5-5", [{"role": "user", "content": parts}])
        blocks = self.claude.requests[-1]["messages"][0]["content"]
        self.assertEqual(blocks[0]["source"], {"type": "base64", "media_type": "image/png",
                                               "data": base64.b64encode(PNG).decode()})
        self.ask("custom/my-model", [{"role": "user", "content": parts}])
        self.assertEqual(self.custom.requests[-1]["body"]["messages"][1]["content"], parts)
        self.codex.say("a pixel")
        self.ask("codex/gpt-6-astra", [{"role": "user", "content": parts}])
        self.assertEqual(self.codex.requests("turn/start")[-1]["params"]["input"],
                         [{"type": "text", "text": "what?"}, {"type": "image", "url": IMAGE}])


class AvailableTest(Case):
    def test_available_names_the_missing_credential(self):
        for model in ("claude-haiku-5-5", "openrouter/x/y", "custom/my-model", "codex/gpt-6-astra"):
            self.assertIsNone(modelcall.available(model), model)
        with mock.patch.object(modelcall, "secrets_env", return_value={}):
            self.assertIn("ANTHROPIC_API_KEY", modelcall.available("claude-haiku-5-5"))
            self.assertEqual(modelcall.available("openrouter/x/y"), "no OPENROUTER_API_KEY in the supervisor's .env")
            self.assertIn("SHAFT_CUSTOM_API_KEY", modelcall.available("custom/my-model"))
            with self.assertRaisesRegex(modelcall.ModelCallError, "OPENROUTER_API_KEY"):
                self.ask("openrouter/x/y")
        self.assertIn("SHAFT_CUSTOM_PRICES", modelcall.available("custom/unpriced"))
        with mock.patch.object(cloudllm, "CUSTOM_BASE_URL", ""):
            self.assertIn("SHAFT_CUSTOM_BASE_URL", modelcall.available("custom/my-model"))
        self.assertIn("SHAFT_CODEX_MODELS", modelcall.available("codex/gpt-0"))
        (self.codex.home / "auth.json").unlink()
        self.assertEqual(modelcall.available("codex/gpt-6-astra"), "Codex login missing: run shaft codex login")
        self.assertIn("not a model", modelcall.available("grok/grok-5"))
        self.assertEqual(self.claude.requests + self.router.requests + self.custom.requests, [])

    def test_any_model_takes_every_provider_the_supervisor_can_call(self):
        for model in ("claude-haiku-5-5", "openrouter/x/y", "custom/my-model", "codex/gpt-6-astra"):
            self.assertEqual(core.any_model(model), model)
        for model, said in (("custom/unpriced", "SHAFT_CUSTOM_PRICES"), ("codex/gpt-0", "SHAFT_CODEX_MODELS"),
                            ("grok/grok-5", "codex/"), ("gpt-6", "claude-")):
            with self.assertRaisesRegex(ValueError, said):
                core.any_model(model)
        self.assertEqual(core.any_models("codex/gpt-6-astra, custom/my-model"), ["codex/gpt-6-astra", "custom/my-model"])
        for name in ("SHAFT_JUDGE_MODELS", "SHAFT_JUDGE2_MODEL", "SHAFT_RECAP_MODEL"):
            self.assertIn(core.KNOBS[name].parse, (core.any_model, core.any_models), name)
        self.assertIs(core.KNOBS["SHAFT_DESCRIBE_MODEL"].parse, core.fallback_model)    # any_model, or none: off


class CallersTest(Case):
    def test_recap_and_describe_use_modelcall(self):
        usage = self.root / "recap_usage.jsonl"
        self.custom.reply = chat(json.dumps({"now": "Idle", "last_hour": "Nothing recorded"}),
                                 usage={"prompt_tokens": 100, "completion_tokens": 10})
        with mock.patch.object(recap, "MODEL", "custom/my-model"), mock.patch.object(recap, "USAGE", usage), \
                mock.patch.object(recap, "SUP_DIR", self.root):
            self.assertEqual(recap.ask("records"), "Idle · last hour: Nothing recorded")
        row = json.loads(usage.read_text())
        self.assertEqual((row["model"], row["error"]), ("custom/my-model", None))
        self.assertAlmostEqual(row["usd"], (100 * 2.0 + 10 * 8.0) / 1e6)

        self.codex.say("A red rectangle.")
        inst = self.root / "example"
        inst.mkdir()
        b = broker.Broker("example", self.root, {}, self.root / "b.log", root=inst, usage_name="example")
        with mock.patch.object(broker, "DESCRIBE_MODEL", "codex/gpt-6-astra"), \
                mock.patch.object(cloudllm, "spent_today", return_value=0.0):
            said = b._describe("image/png", PNG, "what colour?")
        self.assertEqual(said, "A red rectangle.")
        sent = self.codex.requests("turn/start")[-1]["params"]["input"]
        self.assertEqual(sent[1], {"type": "image", "url": IMAGE})
        self.assertIn("what colour?", sent[0]["text"])
        (args, _), = self.record.call_args_list                  # billed to the instance, in its cloud ledger
        self.assertEqual((args[0], args[1], args[4]), ("example", "codex/gpt-6-astra", 0.0))

    def test_describe_refuses_with_the_reason_when_its_model_is_unavailable(self):
        b = broker.Broker("example", self.root, {}, self.root / "b.log", root=self.root, usage_name="example")
        (self.codex.home / "auth.json").unlink()
        with mock.patch.object(broker, "DESCRIBE_MODEL", "codex/gpt-6-astra"), \
                self.assertRaisesRegex(RuntimeError, "Codex login missing"):
            b._describe("image/png", PNG, "what?")
        self.assertEqual(self.codex.requests("thread/start"), [])

    def test_a_custom_describe_model_must_be_declared_able_to_read_images(self):
        b = broker.Broker("example", self.root, {}, self.root / "b.log", root=self.root, usage_name="example")
        with mock.patch.object(broker, "DESCRIBE_MODEL", "custom/my-model"), \
                mock.patch.object(cloudllm, "spent_today", return_value=0.0):
            with mock.patch.object(cloudllm, "CUSTOM_VISION", []), self.assertRaisesRegex(
                    RuntimeError, "custom/my-model is not declared able to read images: add it to SHAFT_CUSTOM_VISION"):
                b._describe("image/png", PNG, "what?")
            self.assertEqual(self.custom.requests, [])                   # refused before any request
            self.custom.reply = chat("A pixel.", usage={"prompt_tokens": 10, "completion_tokens": 3})
            with mock.patch.object(cloudllm, "CUSTOM_VISION", ["my-model"]):
                self.assertEqual(b._describe("image/png", PNG, "what?"), "A pixel.")
        self.assertEqual(self.custom.requests[-1]["body"]["messages"][0]["content"][0]["image_url"]["url"], IMAGE)
        self.assertIn("SHAFT_CUSTOM_VISION", core.KNOBS)
        self.assertEqual(core.KNOBS["SHAFT_CUSTOM_VISION"].parse("a, b"), ["a", "b"])

    def test_an_empty_recap_is_logged_as_a_failed_recap(self):
        usage = self.root / "recap_usage.jsonl"
        with mock.patch.object(recap, "MODEL", "claude-haiku-5-5"), mock.patch.object(recap, "USAGE", usage), \
                mock.patch.object(recap, "SUP_DIR", self.root), \
                mock.patch.object(modelcall, "_claude", return_value=modelcall.Answer(
                    json.dumps({"now": " ", "last_hour": "x"}), {}, 0.0001, "claude-haiku-5-5")) as claude:
            claude.side_effect = lambda *a: (a[-1](modelcall.Call("claude-haiku-5-5", {}, 0.0001, 0.1, None)),
                                             claude.return_value)[1]
            with self.assertRaisesRegex(recap.RecapError, "empty recap"):
                recap.ask("records")
        row = json.loads(usage.read_text())
        self.assertTrue(row["error"].startswith("unusable recap: empty recap"), row)
        self.custom.reply = chat(json.dumps({"now": "", "last_hour": "x"}))     # off Anthropic: asked again
        with mock.patch.object(recap, "MODEL", "custom/my-model"), mock.patch.object(recap, "USAGE", usage), \
                mock.patch.object(recap, "SUP_DIR", self.root), self.assertRaisesRegex(recap.RecapError, "empty recap"):
            recap.ask("records")
        self.assertEqual(len(self.custom.requests), 3)


if __name__ == "__main__":
    unittest.main()

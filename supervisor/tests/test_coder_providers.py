"""The coder (coding hand-offs) on the provider the operator has: Anthropic, OpenRouter, an OpenAI-compatible endpoint
(custom/) or a ChatGPT plan (codex/). One tool loop in OpenAI format, the same tools and the same contract —
(files, summary, usage), the daily cap, the coder ledger — whatever the provider. Loopback http.servers stand in for
the APIs and a child process for the Codex app-server; no test reaches a real provider or the real Codex."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from supervisor import cloudllm, coder, core
from supervisor.tests.test_modelcall import FakeCodex

FILE = "print('old')\n"
NEW = [{"path": "shaft/x.py", "content": "print('new')\n"}]


class Loopback:
    """A loopback endpoint answering each POST with the next of `replies`, recording what it was sent."""

    def __init__(self, replies: list[dict]):
        self.requests, self.replies = [], list(replies)
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)})
                payload = json.dumps(fake.replies.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def openai_reply(calls=(), content="", usage=None, finish=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"id": f"call_{i}", "type": "function", "function": {
            "name": name, "arguments": json.dumps(args)}} for i, (name, args) in enumerate(calls)]
    return {"id": "gen", "choices": [{"index": 0, "message": message,
                                      "finish_reason": finish or ("tool_calls" if calls else "stop")}],
            "usage": usage or {}}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-coder-providers-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "kiln"
        (self.root / "shaft").mkdir(parents=True)
        (self.root / "shaft" / "x.py").write_text(FILE)
        self.usage = self.tmp / "coder_usage.jsonl"
        self.enterContext(mock.patch.object(coder, "USAGE", self.usage))
        self.enterContext(mock.patch.object(cloudllm.time, "sleep"))
        self.enterContext(mock.patch.object(coder.time, "sleep"))
        self.secrets = {}
        self.enterContext(mock.patch.object(coder, "secrets_env", side_effect=lambda: dict(self.secrets)))

    def use(self, model):
        self.enterContext(mock.patch.object(coder, "CODER_MODEL", model))

    def rows(self):
        return [json.loads(line) for line in self.usage.read_text().splitlines()] if self.usage.exists() else []


class OpenAIToolsTest(Case):
    def test_coder_on_openrouter_writes_files_with_openai_tools(self):
        api = Loopback([
            openai_reply([("read_file", {"path": "shaft/x.py"})],
                         usage={"prompt_tokens": 900, "completion_tokens": 40, "cost": 0.001}),
            openai_reply([("submit", {"files": NEW, "summary": "printed new"})],
                         usage={"prompt_tokens": 1000, "completion_tokens": 60, "cost": 0.002})])
        self.addCleanup(api.close)
        self.enterContext(mock.patch.object(cloudllm, "OPENROUTER_API", api.url + "/api/v1/chat/completions"))
        self.secrets = {"OPENROUTER_API_KEY": "o"}
        self.use("openrouter/z-ai/glm-5.3")
        files, summary, usage = coder.write(self.root, "kiln", "make it print new")
        self.assertEqual((files, summary), (NEW, "printed new"))
        first, second = (r["body"] for r in api.requests)
        self.assertEqual(api.requests[0]["headers"]["Authorization"], "Bearer o")
        self.assertEqual(first["model"], "z-ai/glm-5.3")
        self.assertEqual(first["provider"], cloudllm.OPENROUTER_PROVIDER)      # the instances' data policy
        self.assertEqual(first["reasoning"], {"effort": "high"})
        self.assertEqual([t["type"] for t in first["tools"]], ["function"] * 3)
        self.assertEqual([t["function"]["name"] for t in first["tools"]], ["list_files", "read_file", "submit"])
        self.assertEqual(first["messages"][0], {"role": "system", "content": coder.SYSTEM})
        self.assertIn("make it print new", first["messages"][1]["content"])
        tool = second["messages"][-1]
        self.assertEqual((tool["role"], tool["tool_call_id"], tool["content"]), ("tool", "call_0", FILE))
        self.assertEqual(second["messages"][-2]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual((usage["turns"], usage["input_tokens"], usage["output_tokens"]), (2, 1900, 100))
        self.assertAlmostEqual(usage["usd"], 0.003)
        row = self.rows()[-1]
        self.assertEqual((row["model"], row["instance"]), ("openrouter/z-ai/glm-5.3", "kiln"))
        self.assertAlmostEqual(row["usd"], 0.003)
        self.assertAlmostEqual(coder.spent_today("kiln"), 0.003)

    def test_a_malformed_submit_is_sent_back_not_applied(self):
        """Not every provider's model keeps to submit's schema: files that delegate() could not apply go back to the
        model as the tool's error, and it submits again."""
        api = Loopback([openai_reply([("submit", {"files": "shaft/x.py", "summary": "s"})], usage={"cost": 0.001}),
                        openai_reply([("submit", {"files": NEW, "summary": "s"})], usage={"cost": 0.001})])
        self.addCleanup(api.close)
        self.enterContext(mock.patch.object(cloudllm, "OPENROUTER_API", api.url + "/api/v1/chat/completions"))
        self.secrets = {"OPENROUTER_API_KEY": "o"}
        self.use("openrouter/z-ai/glm-5.3")
        files, _, usage = coder.write(self.root, "kiln", "x")
        self.assertEqual((files, usage["turns"]), (NEW, 2))
        told = api.requests[1]["body"]["messages"][-1]
        self.assertEqual(told["role"], "tool")
        self.assertIn("files must be a list", told["content"])

    def openrouter(self, *replies):
        api = Loopback(list(replies))
        self.addCleanup(api.close)
        self.enterContext(mock.patch.object(cloudllm, "OPENROUTER_API", api.url + "/api/v1/chat/completions"))
        self.secrets = {"OPENROUTER_API_KEY": "o"}
        self.use("openrouter/z-ai/glm-5.3")
        return api

    def test_an_openrouter_reply_without_a_cost_is_booked_at_the_dearest_catalogue_price(self):
        """Bug guarded: a reply without usage.cost was booked at nothing ($0), so the daily cap never saw it."""
        self.openrouter(openai_reply([("list_files", {})], usage={"prompt_tokens": 1000, "completion_tokens": 100}),
                        openai_reply([("submit", {"files": NEW, "summary": "s"})], usage={"cost": 0.001}))
        catalogue = {"models": [{"id": "z-ai/glm-5.3", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}],
                     "endpoints": [{"model_id": "z-ai/glm-5.3", "tag": "novita",
                                    "pricing": {"prompt": "0.000003", "completion": "0.000004"}}]}
        from supervisor import consult
        with mock.patch.object(consult, "_catalogue", return_value=catalogue):
            files, _, usage = coder.write(self.root, "kiln", "x")
        self.assertEqual(files, NEW)
        booked = (1000 * 3.0 + 100 * 4.0) / 1e6                  # the dearest qualifying provider (novita)
        self.assertGreater(usage["usd"], 0.001)
        self.assertEqual(usage["usd"], round(booked + 0.001, 4))
        self.assertEqual(self.rows()[-1]["estimated"], "catalogue price")

    def test_an_unpriceable_reply_books_the_rest_of_the_allowance_and_the_next_hand_off_is_refused(self):
        """No cost and no price to be had: the rest of today's allowance is booked, so the cap still holds."""
        api = self.openrouter(openai_reply([("list_files", {})], usage={"prompt_tokens": 5, "completion_tokens": 5}))
        from supervisor import consult
        with mock.patch.object(consult, "_catalogue", side_effect=consult.ConsultError(503, "offline")):
            with self.assertRaisesRegex(coder.CoderError, "rest of today's coding allowance"):
                coder.write(self.root, "kiln", "x")
            self.assertEqual(len(api.requests), 1)
            self.assertGreaterEqual(coder.spent_today("kiln"), coder.DAILY_USD)
            with self.assertRaisesRegex(coder.CoderError, "already spent"):
                coder.write(self.root, "kiln", "again")
        self.assertEqual(len(api.requests), 1)                   # refused before any call

    def test_coder_on_custom_is_priced_at_the_declared_price(self):
        api = Loopback([openai_reply([("submit", {"files": NEW, "summary": "s"})],
                                     usage={"prompt_tokens": 1000, "completion_tokens": 500})])
        self.addCleanup(api.close)
        for name, value in (("CUSTOM_BASE_URL", api.url + "/v1"), ("CUSTOM_PRICES", {"my-model": (2.0, 8.0)})):
            self.enterContext(mock.patch.object(cloudllm, name, value))
        self.secrets = {"SHAFT_CUSTOM_API_KEY": "c"}
        self.use("custom/my-model")
        files, _, usage = coder.write(self.root, "kiln", "x")
        self.assertEqual(files, NEW)
        req = api.requests[0]
        self.assertEqual((req["path"], req["headers"]["Authorization"], req["body"]["model"]),
                         ("/v1/chat/completions", "Bearer c", "my-model"))
        self.assertNotIn("provider", req["body"])
        self.assertEqual(req["body"]["tools"][2]["function"]["name"], "submit")
        self.assertAlmostEqual(usage["usd"], (1000 * 2.0 + 500 * 8.0) / 1e6)
        self.assertAlmostEqual(self.rows()[-1]["usd"], (1000 * 2.0 + 500 * 8.0) / 1e6)

    def test_coder_on_claude_translates_the_openai_loop_and_keeps_cache_and_effort(self):
        thinking = {"type": "thinking", "thinking": "look first", "signature": "sig"}
        usage = {"input_tokens": 10, "output_tokens": 20}
        api = Loopback([
            {"id": "m1", "model": "claude-opus-5-5", "stop_reason": "tool_use", "usage": usage, "content": [
                thinking, {"type": "tool_use", "id": "tu1", "name": "read_file", "input": {"path": "shaft/x.py"}}]},
            {"id": "m2", "model": "claude-opus-5-5", "stop_reason": "tool_use", "usage": usage, "content": [
                {"type": "tool_use", "id": "tu2", "name": "submit", "input": {"files": NEW, "summary": "s"}}]}])
        self.addCleanup(api.close)
        self.enterContext(mock.patch.object(coder, "API", api.url + "/v1/messages"))
        self.secrets = {"ANTHROPIC_API_KEY": "k"}
        self.use("claude-opus-5-5")
        files, summary, got = coder.write(self.root, "kiln", "x")
        self.assertEqual((files, summary), (NEW, "s"))
        first, second = (r["body"] for r in api.requests)
        self.assertEqual(first["system"], coder.SYSTEM)
        self.assertEqual(first["output_config"], {"effort": "high"})
        self.assertEqual(first["cache_control"], cloudllm.CACHE)
        self.assertEqual(first["fallbacks"], "default")
        self.assertEqual([t["name"] for t in first["tools"]], ["list_files", "read_file", "submit"])
        self.assertIn("input_schema", first["tools"][0])
        self.assertEqual(second["messages"][1], {"role": "assistant", "content": [      # verbatim, thinking included
            thinking, {"type": "tool_use", "id": "tu1", "name": "read_file", "input": {"path": "shaft/x.py"}}]})
        self.assertEqual(second["messages"][2]["content"][0],
                         {"type": "tool_result", "tool_use_id": "tu1", "content": FILE})
        self.assertEqual(got["usd"], round(2 * (10 * 4 + 20 * 20) / 1e6, 4))   # the ledger keeps 4 places

    def test_coder_parser_takes_any_provider_but_never_an_unpriced_model(self):
        parse = core.KNOBS["SHAFT_CODER_MODEL"].parse
        with mock.patch.object(cloudllm, "CUSTOM_PRICES", {"my-model": (1.0, 2.0)}):
            for model in ("claude-opus-5-5", "openrouter/z-ai/glm-5.3", "custom/my-model", "codex/gpt-6-astra"):
                self.assertEqual(parse(model), model)
            for model in ("claude-opus-9", "custom/unpriced"):
                with self.assertRaisesRegex(ValueError, "price"):
                    parse(model)


class CodexTest(Case):
    def test_coder_on_codex_returns_files_summary_usage(self):
        codex = FakeCodex(self, self.tmp)
        codex.binary.with_suffix(".json").write_text(json.dumps({"stages": [
            [{"usage": 100}, {"call": 1, "tool": "read_file", "arguments": {"path": "shaft/x.py"}}],
            [{"usage": 50}, {"call": 2, "tool": "submit", "arguments": {"files": NEW, "summary": "via codex"}}]]}))
        self.use("codex/gpt-6-astra")
        with mock.patch.object(cloudllm, "_record") as cloud_row:
            files, summary, usage = coder.write(self.root, "kiln", "make it print new")
        self.assertEqual((files, summary), (NEW, "via codex"))
        start = codex.requests("thread/start")[-1]["params"]
        self.assertEqual(start["baseInstructions"], coder.SYSTEM)
        self.assertEqual([t["name"] for t in start["dynamicTools"]], ["list_files", "read_file", "submit"])
        self.assertEqual(codex.requests("turn/start")[0]["params"]["effort"], "high")
        log = [json.loads(s) for s in codex.binary.with_suffix(".jsonl").read_text().splitlines()]
        answer = next(r for r in log if r.get("id") == 1001 and "result" in r)
        self.assertEqual(answer["result"]["contentItems"][0]["text"], FILE)      # read_file's result reached Codex
        self.assertEqual((usage["usd"], usage["plan"]), (0.0, "codex"))
        self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (150, 4))
        cloud_row.assert_not_called()                  # the coder's ledger, not the instances' cloud ledger
        row = self.rows()[-1]
        self.assertEqual((row["model"], row["usd"]), ("codex/gpt-6-astra", 0.0))


class CredentialTest(Case):
    def test_coder_without_any_credential_says_which(self):
        no_login = self.tmp / "no-codex-home"
        no_login.mkdir()
        from supervisor import codexcli
        self.enterContext(mock.patch.object(codexcli, "HOME", no_login))
        self.enterContext(mock.patch.object(cloudllm, "CUSTOM_PRICES", {"my-model": (1.0, 2.0)}))
        self.enterContext(mock.patch.object(cloudllm, "CUSTOM_BASE_URL", "http://127.0.0.1:9/v1"))
        urlopen = self.enterContext(mock.patch("urllib.request.urlopen", side_effect=AssertionError("no HTTP")))
        for model, missing in (("claude-opus-5-5", "ANTHROPIC_API_KEY"), ("openrouter/z-ai/glm-5.3", "OPENROUTER_API_KEY"),
                               ("custom/my-model", "SHAFT_CUSTOM_API_KEY"), ("codex/gpt-6-astra", "Codex login missing")):
            with self.subTest(model=model), mock.patch.object(coder, "CODER_MODEL", model):
                with self.assertRaisesRegex(coder.CoderError, missing) as caught:
                    coder.write(self.root, "kiln", "x")
                self.assertIn(model, str(caught.exception))
        urlopen.assert_not_called()
        self.assertEqual(self.rows(), [])               # refused before any call: nothing spent, nothing recorded


if __name__ == "__main__":
    unittest.main()

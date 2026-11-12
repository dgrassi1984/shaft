"""`shaft provider use|check` (supervisor/provider.py): one switch for the Mac app's Model step. Loopback stubs stand in
for the providers and a child process for the Codex app-server (test_modelcall.Case); no test reaches a real provider,
and the settings file is a throwaway."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from supervisor import __main__ as cli
from supervisor import cloudllm, codexcli, core, modelcall, provider, settings
from supervisor.tests.test_custom_route import chat
from supervisor.tests.test_modelcall import ALL_KEYS, Case as ProviderCase

MODEL_KNOBS = provider.MODEL_KNOBS


class Case(ProviderCase):
    def setUp(self):
        super().setUp()
        self.file = self.root / "settings.env"
        for target, name, value in ((core, "SETTINGS_FILE", self.file), (core, "SUP_DIR", self.root),
                                    (settings, "log", mock.Mock()), (cloudllm, "CUSTOM_PRICES", {}),
                                    (cloudllm, "CUSTOM_BASE_URL", ""), (cloudllm, "CUSTOM_VISION", [])):
            self.enterContext(mock.patch.object(target, name, value))
        core._file_cache.update(stamp=None, values={})
        self.addCleanup(core._file_cache.update, stamp=None, values={})

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["provider", *argv, "--json"])
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        return code, lines, err.getvalue()

    def written(self) -> dict:
        return settings.file_values() if self.file.exists() else {}

    def assert_consistent(self, result):
        """Every model the preset wrote passes modelcall.available, or its function is listed under off."""
        w = self.written()      # a new process would load the declared custom/ endpoint and prices from the file
        with provider._Declared(w.get("SHAFT_CUSTOM_BASE_URL"), core.custom_prices(w.get("SHAFT_CUSTOM_PRICES", "{}")),
                                core.model_names(w.get("SHAFT_CUSTOM_VISION", ""))):
            for fn, knobs in provider.FUNCTIONS.items():
                for knob in knobs:
                    if knob not in w or fn in result["off"]:
                        continue
                    for m in provider._models(knob, w[knob]):
                        self.assertIsNone(modelcall.available(m), f"{knob}={m} written but unavailable, not off")


class UseTest(Case):
    def test_use_writes_only_consistent_knobs(self):
        code, (r,), _ = self.run_cli("use", "openrouter")
        self.assertEqual(code, 0)
        self.assertEqual(r["off"].keys() & {"judge", "judge2", "consult", "trial", "instances"}, set())
        self.assertEqual(r["model"], "openrouter/xiaomi/mimo-v2.6-pro")   # a new instance's model (the Mac app)
        self.assert_consistent(r)

        code, (r,), _ = self.run_cli("use", "codex")
        self.assertEqual(code, 0)
        self.assertEqual(r["off"].get("describe"), None)            # the Codex channel reads images
        self.assertEqual(self.written()["SHAFT_CODER_MODEL"], "codex/gpt-6-astra")
        self.assertEqual(self.written()["SHAFT_JUDGE_MODELS"], "codex/gpt-6.1-sol")     # not the instance's own model
        self.assertEqual(self.written()["SHAFT_JUDGE2_MODEL"], "codex/gpt-6.1-sol")
        self.assertEqual(self.written()["SHAFT_CODEX_FALLBACK_MODEL"], "none")
        self.assertEqual(self.written()["SHAFT_TRIAL_FALLBACK_MODEL"], "codex/gpt-6-astra")
        self.assertEqual(self.written()["SHAFT_CONSULT_MODELS"], ",".join(codexcli.MODELS))
        self.assertEqual(sorted(r["covers"]), sorted(set(provider.FUNCTIONS) - {"instances"}))
        self.assertEqual(r["model"], "codex/gpt-6-astra")
        self.assert_consistent(r)

        code, (r,), _ = self.run_cli("use", "custom", "--model", "my-model", "--base-url", self.custom.url,
                                     "--price-in", "2", "--price-out", "8")
        self.assertEqual(code, 0)
        self.assertEqual(r["off"].keys(), {"describe"})
        self.assertIn("SHAFT_CUSTOM_VISION", r["off"]["describe"])
        self.assertEqual(json.loads(self.written()["SHAFT_CUSTOM_PRICES"]), {"my-model": [2.0, 8.0]})
        self.assertEqual(self.written()["SHAFT_CLOUD_MODELS"], "custom/my-model")
        self.assertEqual(r["model"], "custom/my-model")
        self.assertEqual(self.written()["SHAFT_TRIAL_FALLBACK_MODEL"], "none")
        self.assertEqual(self.written()["SHAFT_DESCRIBE_MODEL"], "none")   # off for real: unset would be claude-haiku
        self.assert_consistent(r)

    def test_openrouter_is_openrouter_alone_and_covers_everything_with_only_its_key(self):
        with mock.patch.object(modelcall, "secrets_env", return_value={"OPENROUTER_API_KEY": "o"}):
            code, (r,), _ = self.run_cli("use", "openrouter", "--model", "openrouter/z-ai/glm-5.3")
        self.assertEqual(code, 0)
        self.assertEqual(r["model"], "openrouter/z-ai/glm-5.3")          # the model asked for, not the first default
        self.assertEqual(r["off"], {})
        w = self.written()
        self.assertEqual(w["SHAFT_CODER_MODEL"], "openrouter/anthropic/claude-opus-5.5")
        self.assertEqual((w["SHAFT_DESCRIBE_MODEL"], w["SHAFT_RECAP_MODEL"]), ("openrouter/anthropic/claude-sonnet-5.5",) * 2)
        self.assertEqual(w["SHAFT_CLOUD_MODELS"], "openrouter/xiaomi/mimo-v2.6-pro,openrouter/anthropic/claude-sonnet-5.5,"
                                                  "openrouter/anthropic/claude-opus-5.5,openrouter/z-ai/glm-5.3")
        for knob in MODEL_KNOBS:
            self.assertTrue(w[knob], knob)
            for m in w[knob].split(","):
                self.assertTrue(m.startswith("openrouter/"), f"{knob}={m} is not on OpenRouter")

    def test_openrouter_without_its_key_is_off_with_the_missing_key(self):
        with mock.patch.object(modelcall, "secrets_env", return_value={}):
            code, (r,), _ = self.run_cli("use", "openrouter")
        self.assertEqual(code, 0)
        self.assertEqual(r["covers"], [])
        self.assertIn("OPENROUTER_API_KEY", r["off"]["judge"])

    def test_describe_none_sends_nothing_even_with_an_anthropic_key(self):
        from supervisor import broker
        self.assertEqual(core.KNOBS["SHAFT_DESCRIBE_MODEL"].parse("none"), "none")
        b = broker.Broker("example", self.root, {}, self.root / "b.log", root=self.root, usage_name="example")
        with mock.patch.object(broker, "DESCRIBE_MODEL", "none"), \
                self.assertRaisesRegex(RuntimeError, r"image description is off \(SHAFT_DESCRIBE_MODEL=none\)"):
            b._describe("image/png", b"x", "what?")
        self.assertEqual(self.claude.requests, [])
        self.assertEqual(self.router.requests, [])

    def test_a_failed_settings_write_is_one_json_line_with_the_real_error(self):
        with mock.patch.object(settings.os, "replace", side_effect=OSError(28, "No space left on device")):
            code, lines, _ = self.run_cli("use", "codex")
        self.assertEqual(code, 1)
        (r,) = lines
        self.assertFalse(r["ok"])
        self.assertIn("No space left on device", r["error"])

    def test_a_single_codex_model_judges_itself(self):
        with mock.patch.object(codexcli, "MODELS", ["codex/gpt-6-astra"]):
            code, (r,), _ = self.run_cli("use", "codex")
        self.assertEqual(code, 0)
        self.assertEqual((self.written()["SHAFT_JUDGE_MODELS"], self.written()["SHAFT_JUDGE2_MODEL"]),
                         ("codex/gpt-6-astra", "codex/gpt-6-astra"))

    def test_codex_without_login_is_off_with_the_login_reason(self):
        (self.codex.home / "auth.json").unlink()
        code, (r,), _ = self.run_cli("use", "codex")
        self.assertEqual(code, 0)
        self.assertEqual(r["covers"], [])
        # --json is the Mac app's Model step: the sign-in is named there, not a terminal command (final review I6)
        self.assertEqual(r["off"]["judge"], "Codex is not signed in: sign in with ChatGPT on the Model step")
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["provider", "use", "codex"])
        self.assertIn("shaft codex login", out.getvalue())                  # the terminal keeps its own words

    def test_custom_prices_and_the_models_that_need_them_go_in_one_write(self):
        """The custom/ validators read SHAFT_CUSTOM_PRICES as loaded at process start: a first `use custom` writes the
        prices and SHAFT_CLOUD_MODELS=custom/x together, which would refuse if validation ignored the pending prices."""
        self.assertEqual(cloudllm.CUSTOM_PRICES, {})
        code, (r,), err = self.run_cli("use", "custom", "--model", "x", "--base-url", self.custom.url,
                                       "--price-in", "0", "--price-out", "0", "--judge-model", "j",
                                       "--judge-price-in", "1.5", "--judge-price-out", "3")
        self.assertEqual(code, 0, r)
        self.assertEqual(json.loads(self.written()["SHAFT_CUSTOM_PRICES"]), {"x": [0.0, 0.0], "j": [1.5, 3.0]})
        self.assertEqual((self.written()["SHAFT_JUDGE_MODELS"], self.written()["SHAFT_JUDGE2_MODEL"]),
                         ("custom/j", "custom/j"))
        self.assertEqual(cloudllm.CUSTOM_PRICES, {})                 # the process is left as it was found

    def test_custom_vision_makes_describe_available(self):
        code, (r,), _ = self.run_cli("use", "custom", "--model", "x", "--base-url", self.custom.url,
                                     "--price-in", "1", "--price-out", "2", "--vision")
        self.assertEqual(code, 0)
        self.assertEqual(r["off"], {})
        self.assertEqual(self.written()["SHAFT_DESCRIBE_MODEL"], "custom/x")
        self.assertEqual(self.written()["SHAFT_CUSTOM_VISION"], "x")

    def test_use_never_touches_an_install_that_did_not_ask(self):
        self.run_cli("check", "openrouter")
        self.assertFalse(self.file.exists())                        # check writes nothing
        self.assertEqual(self.run_cli("use", "custom")[0], 2)       # no --model: usage
        self.assertEqual(self.run_cli("use", "custom", "--model", "x", "--base-url", self.custom.url)[0], 2)  # no price
        self.assertEqual(self.run_cli("use", "openrouter", "--model", "x")[0], 2)
        self.assertEqual(self.run_cli("use", "openrouter", "--judge-price-in", "1")[0], 2)
        self.assertEqual(self.run_cli("use", "codex", "--judge-price-out", "1")[0], 2)
        self.assertEqual(self.run_cli("use", "codex", "--model", "codex/gpt-9")[0], 1)          # refused by any_model
        self.assertFalse(self.file.exists())
        env = self.root / ".env"
        with mock.patch.object(core, "PROJECT", self.root):
            self.run_cli("use", "codex")
        self.assertFalse(env.exists())                              # the verbs never write keys

    def test_a_refused_value_leaves_the_file_as_it_was(self):
        self.run_cli("use", "codex")
        before = self.file.read_text()
        code, (r,), _ = self.run_cli("use", "custom", "--model", "x", "--base-url", "not a url",
                                     "--price-in", "1", "--price-out", "2")
        self.assertEqual((code, r["ok"]), (1, False))
        self.assertEqual(self.file.read_text(), before)


class CheckTest(Case):
    def declare(self):
        self.enterContext(mock.patch.object(cloudllm, "CUSTOM_BASE_URL", self.custom.url))
        self.enterContext(mock.patch.object(cloudllm, "CUSTOM_PRICES", {"my-model": (2.0, 8.0)}))

    def test_check_asks_each_configured_model_once_with_a_tiny_ceiling(self):
        self.declare()
        with mock.patch("supervisor.coder.CODER_MODEL", "custom/my-model"), \
                mock.patch("supervisor.recap.MODEL", "custom/my-model"), \
                mock.patch("supervisor.judge.JUDGE_MODELS", ["custom/my-model", "claude-haiku-5-5"]):
            code, rows, _ = self.run_cli("check", "custom")
        self.assertEqual((code, rows), (0, [{"ok": True, "model": "custom/my-model", "error": None}]))
        self.assertEqual(len(self.custom.requests), 1)
        self.assertEqual(self.custom.requests[0]["body"]["max_tokens"], 16)

    def test_check_reports_the_real_error_on_a_bad_key(self):
        self.declare()
        self.custom.status = 401
        with mock.patch("supervisor.coder.CODER_MODEL", "custom/my-model"):
            code, rows, _ = self.run_cli("check", "custom")
        self.assertEqual(code, 1)
        (row,) = rows
        self.assertEqual((row["ok"], row["model"]), (False, "custom/my-model"))
        self.assertIn("401", row["error"])
        self.assertIn("nope", row["error"])                         # the provider's own words, not a generic failure

    def test_check_names_a_missing_key_without_a_call(self):
        self.declare()
        with mock.patch.object(modelcall, "secrets_env", return_value={}), \
                mock.patch("supervisor.coder.CODER_MODEL", "custom/my-model"):
            code, (row,), _ = self.run_cli("check", "custom")
        self.assertEqual(code, 1)
        self.assertIn("SHAFT_CUSTOM_API_KEY", row["error"])
        self.assertEqual(self.custom.requests, [])

    def test_check_codex_asks_the_app_server(self):
        self.codex.say("ok")
        with mock.patch("supervisor.coder.CODER_MODEL", "codex/gpt-6-astra"), \
                mock.patch("supervisor.consult.MODELS", ["codex/gpt-6-astra", "codex/gpt-6.1-sol"]):
            code, rows, _ = self.run_cli("check", "codex")
        self.assertEqual((code, [r["model"] for r in rows]), (0, ["codex/gpt-6-astra", "codex/gpt-6.1-sol"]))
        self.assertTrue(all(r["ok"] for r in rows))
        self.assertEqual(len(self.codex.requests("turn/start")), 2)

    def test_check_with_nothing_configured_for_the_provider_fails_loudly(self):
        code, (row,), _ = self.run_cli("check", "custom")
        self.assertEqual((code, row["ok"], row["model"]), (1, False, None))
        self.assertEqual(row["error"], "no custom/ model is configured yet: Save the provider first")


class StaleServiceTest(Case):
    """Final review C2 (bug on aa5a7b51): the running API reads its model knobs when it starts and runs conversations
    in-process, so after the Model step saved the custom preset it still refused custom/ (403, SHAFT_CLOUD_MODELS) and
    the welcome failed. `provider use` names every service holding another value of what it wrote (also when a repeated
    Save changes nothing: the restart after a failed one is still due), and `api restart` / `web restart` are what pick
    the preset up: a process started after the save accepts custom/."""

    def held_defaults(self):
        settings.load_all()
        return {"knobs": {k.name: {"raw": k.default, "source": "default"} for k in settings.KNOBS.values()}}

    def test_a_service_started_before_the_save_refuses_custom_until_it_restarts(self):
        import subprocess
        import sys
        from pathlib import Path
        api_record = self.held_defaults()
        args = ("use", "custom", "--model", "my-model", "--base-url", self.custom.url, "--price-in", "2", "--price-out", "8")
        with mock.patch("supervisor.version.service", side_effect=lambda s, stale=False: api_record if s == "api" else None):
            code, (first,), _ = self.run_cli(*args)
            self.assertEqual(code, 0)
            code, (again,), _ = self.run_cli(*args)           # Try again after a failed restart: nothing changes
        self.assertIn("SHAFT_CLOUD_MODELS", first["changed"])
        self.assertEqual(again["changed"], [])
        for r in (first, again):
            self.assertEqual(sorted({s for svcs in r["not_live"].values() for s in svcs}), ["api"])
            self.assertIn("api", r["not_live"]["SHAFT_CLOUD_MODELS"])

        # This process stands for the API started before the save: its knob is the default, and custom/ is refused.
        with mock.patch.object(cloudllm, "CLOUD_MODELS", core.cloud_models(settings.KNOBS["SHAFT_CLOUD_MODELS"].default)):
            with self.assertRaises(cloudllm.CloudError) as refused:
                cloudllm.complete("wisp", {"model": "custom/my-model", "messages": []}, {}, {})
        self.assertEqual(refused.exception.status, 403)

        # The restart path the wizard runs is accepted (launchd stubbed) …
        from supervisor import __main__ as main
        from supervisor import runner
        restarted = []
        out = io.StringIO()
        with mock.patch("supervisor.autonomy.installed", return_value=True), \
                mock.patch("supervisor.autonomy.running", return_value=True), \
                mock.patch("supervisor.autonomy.service", side_effect=lambda *a: restarted.append(a) or 0), \
                mock.patch.object(runner, "replying", return_value=[]), \
                mock.patch("supervisor.api.instance_names", return_value=["wisp"]), \
                mock.patch("supervisor.actions.cli_guard", return_value=""), redirect_stdout(out):
            self.assertEqual(main.main(["api", "restart", "--json"]), 0)
            self.assertEqual(main.main(["web", "restart", "--json"]), 0)
        self.assertEqual(restarted, [("api", "on"), ("web", "on")])
        self.assertEqual([json.loads(line) for line in out.getvalue().splitlines()], [{"ok": True}, {"ok": True}])

        # … and the process it starts reads the saved preset: custom/ is allowed and priced.
        project = Path(__file__).resolve().parents[2]
        probe = ("import json; from supervisor import cloudllm as c; "
                 "print(json.dumps({'allowed': 'custom/my-model' in c.CLOUD_MODELS, "
                 "'price': list(c._custom_price_or_refuse('custom/my-model'))}))")
        r = subprocess.run([sys.executable, "-E", "-s", "-c", probe], cwd=project, capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin", "HOME": str(self.root), "SHAFT_SUPERVISOR_DIR": str(self.root)},
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]), {"allowed": True, "price": [2.0, 8.0]})


class StreamProbeTest(Case):
    """Final review I5: `provider check custom` made only a buffered call, so an endpoint whose stream an instance
    cannot read passed Test and the welcome failed. A custom/ model the instances run on is also asked as a stream."""

    declare = CheckTest.declare

    def instances_on(self):
        return mock.patch.object(cloudllm, "CLOUD_MODELS", ["custom/my-model"])

    def test_check_also_asks_the_instance_model_as_a_stream(self):
        self.declare()
        self.custom.reply = chat("ok", usage={"prompt_tokens": 5, "completion_tokens": 1})
        with self.instances_on(), mock.patch.object(cloudllm, "USAGE", self.root / "cloud_usage.jsonl"):
            code, rows, _ = self.run_cli("check", "custom")
        self.assertEqual((code, rows), (0, [{"ok": True, "model": "custom/my-model", "error": None}]))
        self.assertEqual([r["body"].get("stream") for r in self.custom.requests], [None, True])

    def test_a_stream_with_no_text_fails_the_check(self):
        self.declare()
        self.custom.reply = chat("ok", usage={"prompt_tokens": 5, "completion_tokens": 1})
        self.custom.sse = [{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}, "[DONE]"]
        # the buffered call answers (as it did on the endpoint this bug was found with); only the stream is empty
        with self.instances_on(), mock.patch.object(cloudllm, "USAGE", self.root / "cloud_usage.jsonl"), \
                mock.patch.object(modelcall, "ask"):
            code, (row,), _ = self.run_cli("check", "custom")
        self.assertEqual((code, row["ok"]), (1, False))
        self.assertIn("answered a stream request (how instances call it) with no text", row["error"])


if __name__ == "__main__":
    unittest.main()

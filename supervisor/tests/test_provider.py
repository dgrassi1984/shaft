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
        self.assertIn("shaft codex login", r["off"]["judge"])

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
        self.assertIn("shaft provider use custom", row["error"])


if __name__ == "__main__":
    unittest.main()

"""Codex M2 wiring through real handlers, with in-memory HTTP and fake providers (no sockets or CLI)."""
from __future__ import annotations

import io
import json
import os
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from supervisor import (bakeoff, broker, cloudllm, codexcli, consult, consultstats, core, experiments,
                        judge, ledger, localmodel, opconfig, panel, planchannel, runner, settings, trial, version, vision)
from supervisor import __main__ as cli

MODEL = "codex/gpt-6-astra"
FALLBACK = "openrouter/test/fallback"
LABEL = f"{MODEL} (fallback {FALLBACK})"


def reply(model=MODEL):
    return {"model": model, "choices": [{"message": {"role": "assistant", "content": "hello"},
                                        "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3}}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for module in (core, planchannel, panel):
            self.enterContext(mock.patch.object(module, "SUP_DIR", self.root))
        self.enterContext(mock.patch.object(core, "SETTINGS_FILE", self.root / "settings.env"))
        self.enterContext(mock.patch.object(cloudllm, "USAGE", self.root / "cloud_usage.jsonl"))
        # panel.costs reads this import-time path independently of panel.SUP_DIR.
        self.enterContext(mock.patch.object(trial, "LEDGER", self.root / "trial_usage.jsonl"))
        self.enterContext(mock.patch.object(codexcli.STATE, "rate", None))
        self.enterContext(mock.patch.object(codexcli, "FALLBACK_MODEL", FALLBACK))


class BrokerTest(Case):
    def test_same_turn_flag_survives_broker_json_and_stream(self):
        self.channel.complete.return_value = {**reply(), "shaft_same_turn": True}
        h, _ = self.call()
        self.assertIs(json.loads(h.wfile.getvalue())["shaft_same_turn"], True)
        h, _ = self.call(stream=True)
        chunks = [json.loads(line[6:]) for line in h.wfile.getvalue().splitlines()
                  if line.startswith(b"data: ") and line != b"data: [DONE]"]
        self.assertEqual([c["shaft_same_turn"] for c in chunks if "shaft_same_turn" in c], [True])

    def setUp(self):
        super().setUp()
        # Capture the nested production Handler without binding any socket or reading instance configuration.
        self.b = broker.Broker.__new__(broker.Broker)
        self.b.name, self.b.usage_name = "bird", "bird@trial:test"
        self.b.channel, self.b.channel_down, self.b._channel_rate = None, None, None
        self.b._channel_lock = threading.Lock()
        self.b.root, self.b.egress = self.root, mock.Mock()
        self.b._log, self.b.secrets, self.b.remembered = mock.Mock(), {}, {}
        self.b.model_allowed = True
        self.desktop = self.enterContext(mock.patch.object(broker, "notify"))
        self.phone = self.enterContext(mock.patch("supervisor.webpush.notify"))
        self.channel_class = self.enterContext(mock.patch.object(codexcli, "Channel"))
        self.channel = self.channel_class.return_value
        self.channel.complete.return_value = reply()
        self.channel.stream.side_effect = lambda body: iter_stream(*cloudllm.as_sse(
            self.channel.complete.return_value).splitlines(keepends=True))
        def server(addr, handler):
            self.handler = handler
            return mock.Mock(server_address=("127.0.0.1", 1))
        with mock.patch.object(broker, "ThreadingHTTPServer", side_effect=server), \
                mock.patch.object(broker.threading, "Thread"):
            self.b.start()

    def call(self, stream=False, writer=None, content="same body"):
        h = self.handler.__new__(self.handler)
        body = {"model": MODEL, "messages": [{"role": "user", "content": content}], "stream": stream}
        data = json.dumps(body).encode()
        h.rfile, h.wfile = io.BytesIO(data), writer if writer is not None else io.BytesIO()
        h.headers = {"Content-Length": str(len(data))}
        h.send_response, h.send_header, h.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        h._llm()
        return h, body

    def cloud(self, instance, body, secrets, remembered):
        cloudllm._record(instance, body["model"], 12, 3, .125, time.monotonic(), "stop", None)
        return reply(body["model"])

    def test_channel_stream_and_nonstream_routes(self):
        """Guard against buffering a channel stream through _llm_reply."""
        delivered = []
        def consume(lines, model):
            delivered.append((model, b"".join(lines)))
        with mock.patch.object(self.handler, "_llm_stream", side_effect=consume):
            self.call(stream=True)
        self.assertEqual(delivered, [(MODEL, cloudllm.as_sse(reply()))])
        self.channel.complete.assert_not_called()
        with mock.patch.object(self.handler, "_llm_reply") as buffered:
            _, body = self.call()
            buffered.assert_called_once_with(body, reply())

    def test_channel_stream_initial_failure_fallback_or_502(self):
        """Guard fallback before streaming headers are sent on channel failure."""
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                self.b.channel_down = None
                self.b.local_fallback = fallback
                self.channel.stream.side_effect = codexcli.CodexFailure("offline")
                with mock.patch.object(cloudllm, "openrouter_stream",
                        side_effect=lambda *args: iter_stream(*cloudllm.as_sse(reply(LABEL)).splitlines(keepends=True))):
                    h, _ = self.call(stream=True)
                h.send_response.assert_called_once_with(200 if fallback else 502)
                if fallback:
                    self.assertEqual(h.wfile.getvalue(), cloudllm.as_sse(reply(LABEL)))
                else:
                    self.assertIn("offline", json.loads(h.wfile.getvalue())["error"]["message"])

    def test_channel_midstream_failure_is_error_not_fallback(self):
        """Guard against accepting partial text or mixing fallback text into a failed answer."""
        def broken(body):
            yield b'data: {"choices": [{"delta": {"content": "hello"}}]}\n'
            yield b'\n'
            raise codexcli.CodexFailure("Codex agent text disagrees with deltas")
        self.channel.stream.side_effect = broken
        with mock.patch.object(cloudllm, "openrouter_stream") as fallback:
            h, _ = self.call(stream=True)
        h.send_response.assert_called_once_with(200)
        data = [json.loads(line[5:]) for line in h.wfile.getvalue().splitlines() if line.startswith(b'data:')]
        self.assertEqual(data[-1], {"error": {"message": "Codex agent text disagrees with deltas"}})
        self.assertNotIn(b'[DONE]', h.wfile.getvalue())
        self.assertEqual(self.b.channel_down, "Codex agent text disagrees with deltas")
        fallback.assert_not_called()

    def test_lazy_one_channel_success_and_close_before_shutdown(self):
        self.assertIsNone(self.b.channel)
        (self.root / "codex_outage.json").write_text('{"reason":"offline","security":false}')
        h, body = self.call()
        self.call()
        self.assertEqual(json.loads(h.wfile.getvalue())["model"], MODEL)
        self.channel_class.assert_called_once_with(self.b.name, self.b.usage_name, self.b._log)
        self.channel.complete.assert_called_with(body)
        self.assertIsNone(codexcli.outage())
        self.desktop.assert_called_once_with("Codex recovered", "Codex calls are succeeding again.")
        self.phone.assert_called_once()
        order = []
        self.channel.close.side_effect = lambda: order.append("close")
        self.b.server.shutdown.side_effect = lambda: order.append("shutdown")
        with mock.patch("supervisor.browser.sweep"):
            self.b.stop()
        self.assertEqual(order, ["close", "shutdown"])

    def test_codex_failure_and_security_reasons_reach_both_logs_even_during_outage(self):
        self.b.log_path = self.root / "broker.log"
        self.b._log = broker.Broker._log.__get__(self.b)
        failures = [codexcli.CodexFailure("RPC timeout"),
                    codexcli.CodexSecurity("Codex native item forbidden: mcpToolCall; tool: exec"),
                    codexcli.CodexFailure("later\nretry; stderr: private message")]
        for failure in failures:
            self.b._channel_report(failure)
        for path in (self.root / "supervisor.log", self.b.log_path):
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertIn("bird: Codex failure: 'RPC timeout'", lines[0])
            self.assertIn("bird: Codex security event: 'Codex native item forbidden: mcpToolCall; tool: exec'",
                          lines[1])
            self.assertIn("later\\nretry", lines[2])
            self.assertNotIn("private message", path.read_text())
        self.desktop.assert_called_once()

    def test_failure_falls_back_same_body_and_stays_down_priced_and_labelled(self):
        self.channel.complete.side_effect = codexcli.CodexSecurity("native tool")
        with mock.patch.object(cloudllm, "complete", side_effect=self.cloud) as cloud:
            h, body = self.call()
            self.call()
        self.channel.complete.assert_called_once_with(body)
        self.assertEqual(cloud.call_count, 2)
        self.assertEqual(cloud.call_args.args[1], {**body, "model": FALLBACK})
        self.assertEqual(json.loads(h.wfile.getvalue())["model"], LABEL)
        row = json.loads(cloudllm.USAGE.read_text().splitlines()[-1])
        self.assertEqual((row["model"], row["usd"]), (LABEL, .125))
        self.assertEqual(self.b.channel_down, "native tool")
        self.assertTrue(codexcli.outage()["security"])
        self.desktop.assert_called_once_with("Codex security event", "native tool")
        self.phone.assert_called_once()

    def test_fallback_without_vision_removes_images_and_stays_sticky(self):
        self.channel.complete.side_effect = codexcli.CodexFailure("offline")
        content = [{"type": "text", "text": "look"},
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64,cHJpdmF0ZQ=="}}]
        with mock.patch.object(vision, "prepare_bytes", return_value=("image/png", b"private")), \
                mock.patch.object(vision, "_openrouter_modalities", return_value={"test/fallback": ["text"]}), \
                mock.patch.object(cloudllm, "complete", side_effect=self.cloud) as cloud:
            for _ in range(2):
                h, _ = self.call(content=content)
                h.send_response.assert_called_once_with(200)
                sent = cloud.call_args.args[1]
                self.assertEqual(vision.count(sent), 0)
                text = sent["messages"][0]["content"]
                self.assertTrue(text.startswith("look[image]"))
                self.assertIn("could not be shown on the fallback model " + FALLBACK, text)
                self.assertIn("I have not seen it", text)
        self.channel.complete.assert_called_once()
        self.assertEqual(cloud.call_count, 2)
        for output in (str(self.b._log.call_args_list), cloudllm.USAGE.read_text()):
            self.assertNotIn("cHJpdmF0ZQ==", output)
            self.assertNotIn("data:image", output)

    def test_fallback_with_vision_preserves_images(self):
        self.channel.complete.side_effect = codexcli.CodexFailure("offline")
        content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,cHJpdmF0ZQ=="}}]
        with mock.patch.object(vision, "prepare_bytes", return_value=("image/png", b"private")), \
                mock.patch.object(vision, "_openrouter_modalities", return_value={"test/fallback": ["text", "image"]}), \
                mock.patch.object(cloudllm, "complete", side_effect=self.cloud) as cloud:
            for _ in range(2):
                h, original = self.call(content=content)
                h.send_response.assert_called_once_with(200)
                self.assertEqual(cloud.call_args.args[1], {**original, "model": FALLBACK})
        self.channel.complete.assert_called_once()
        for output in (str(self.b._log.call_args_list), cloudllm.USAGE.read_text()):
            self.assertNotIn("cHJpdmF0ZQ==", output)
            self.assertNotIn("data:image", output)

    def test_fallback_unknown_vision_strips_images_in_stream_instead_of_http_400(self):
        self.b.channel_down = "offline"
        content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,cHJpdmF0ZQ=="}}]
        with mock.patch.object(vision, "prepare_bytes", return_value=("image/png", b"private")), \
                mock.patch.object(vision, "_openrouter_modalities", side_effect=vision.VisionError("no cache")), \
                mock.patch.object(cloudllm, "openrouter_stream", return_value=iter_stream(b"data: [DONE]\n\n")) as cloud:
            h, _ = self.call(content=content, stream=True)
        h.send_response.assert_called_once_with(200)
        self.assertEqual(vision.count(cloud.call_args.args[1]), 0)
        self.assertIn("could not be shown", cloud.call_args.args[1]["messages"][0]["content"])

    def test_replay_with_images_fails_without_fallback(self):
        self.b.local_fallback = broker.ReplayBroker.local_fallback
        self.channel.complete.side_effect = codexcli.CodexFailure("offline")
        content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,cHJpdmF0ZQ=="}}]
        with mock.patch.object(vision, "prepare_bytes", return_value=("image/png", b"private")), \
                mock.patch.object(cloudllm, "complete") as cloud:
            h, _ = self.call(content=content)
        h.send_response.assert_called_once_with(502)
        cloud.assert_not_called()

    def test_replay_failure_is_http_error_and_never_falls_back(self):
        self.b.local_fallback = broker.ReplayBroker.local_fallback
        self.channel.complete.side_effect = codexcli.CodexFailure("weekly cap")
        with mock.patch.object(cloudllm, "complete") as cloud:
            h, _ = self.call()
        cloud.assert_not_called()
        h.send_response.assert_called_once_with(502)
        self.assertIn("weekly cap", json.loads(h.wfile.getvalue())["error"]["message"])

    def test_replay_stream_failure_is_http_error_without_fallback(self):
        """Guard replay HTTP errors before a channel emits its first byte."""
        self.b.local_fallback = broker.ReplayBroker.local_fallback
        self.channel.stream.side_effect = codexcli.CodexFailure("weekly cap")
        with mock.patch.object(cloudllm, "openrouter_stream") as cloud:
            h, _ = self.call(stream=True)
        cloud.assert_not_called()
        h.send_response.assert_called_once_with(502)
        self.assertIn("weekly cap", json.loads(h.wfile.getvalue())["error"]["message"])

    def test_fallback_none_ends_the_run_with_the_channel_error(self):
        """SHAFT_CODEX_FALLBACK_MODEL=none (an operator with a ChatGPT plan only): a live run's channel failure is its
        error — never a call to some other model, never a silent continue — and every later call of the run gets
        the same error at once (the channel stays down), so the instance's retries end the run."""
        self.enterContext(mock.patch.object(codexcli, "FALLBACK_MODEL", "none"))
        for streaming in (False, True):
            with self.subTest(stream=streaming):
                self.b.channel_down, self.b._log = None, mock.Mock()
                self.channel.complete.reset_mock()
                self.channel.stream.reset_mock()
                self.channel.complete.side_effect = codexcli.CodexFailure("offline")
                self.channel.stream.side_effect = codexcli.CodexFailure("offline")
                with mock.patch.object(cloudllm, "complete") as cloud, \
                        mock.patch.object(cloudllm, "openrouter_stream") as cloud_stream:
                    first, _ = self.call(stream=streaming)
                    again, _ = self.call(stream=streaming)
                cloud.assert_not_called()
                cloud_stream.assert_not_called()
                for h in (first, again):
                    h.send_response.assert_called_once_with(502)
                    message = json.loads(h.wfile.getvalue())["error"]["message"]
                    self.assertIn("Codex failed: offline", message)
                    self.assertIn("SHAFT_CODEX_FALLBACK_MODEL=none", message)
                self.assertEqual((self.channel.stream if streaming else self.channel.complete).call_count, 1)
                self.assertEqual(self.b.channel_down, "offline")
                logged = [c.args[0] for c in self.b._log.call_args_list]
                self.assertTrue(any("ERROR" in line and "Codex failed: offline" in line for line in logged), logged)

    def test_codex_closed_is_shutdown_without_false_outage_or_paid_fallback(self):
        for streaming in (False, True):
            with self.subTest(stream=streaming):
                self.channel.complete.side_effect = codexcli.CodexClosed("Codex channel closed")
                self.channel.stream.side_effect = codexcli.CodexClosed("Codex channel closed")
                with mock.patch.object(planchannel, "report_failure") as report, \
                        mock.patch.object(planchannel, "report_success") as success, \
                        mock.patch.object(cloudllm, "complete") as cloud, \
                        mock.patch.object(cloudllm, "openrouter_stream") as cloud_stream:
                    h, _ = self.call(stream=streaming)
                h.send_response.assert_called_once_with(503)
                self.assertEqual(json.loads(h.wfile.getvalue()), {"error": {"message": "Codex channel closed"}})
                report.assert_not_called()
                success.assert_not_called()
                cloud.assert_not_called()
                cloud_stream.assert_not_called()
                self.assertIsNone(self.b.channel_down)
                self.assertIsNone(codexcli.outage())

    def test_notifications_fail_independently_without_breaking_fallback(self):
        self.desktop.side_effect = RuntimeError("desktop unavailable")
        self.phone.side_effect = RuntimeError("phone unavailable")
        self.channel.complete.side_effect = codexcli.CodexFailure("offline")
        with mock.patch.object(cloudllm, "complete", side_effect=self.cloud):
            h, _ = self.call()
        self.assertEqual(json.loads(h.wfile.getvalue())["model"], LABEL)
        self.phone.assert_called_once()

    def test_fallback_uses_normal_cloud_policy_pricing_and_daily_cap(self):
        self.channel.complete.side_effect = codexcli.CodexFailure("offline")
        self.b.secrets = {"OPENROUTER_API_KEY": "test-key"}
        data = reply("test/fallback")
        data["usage"]["cost"] = .08
        with mock.patch.object(cloudllm, "CLOUD_MODELS", []), \
                mock.patch.object(cloudllm, "daily_cap", return_value=1), \
                mock.patch.object(cloudllm, "_send", return_value=(data, None)) as send:
            h, body = self.call()
            request = json.loads(send.call_args.args[0].data)
            self.assertEqual(request["messages"], body["messages"])
            self.assertEqual(request["provider"], cloudllm.OPENROUTER_PROVIDER)
            self.assertEqual(json.loads(h.wfile.getvalue())["model"], LABEL)
            row = json.loads(cloudllm.USAGE.read_text())
            self.assertEqual((row["model"], row["usd"]), (LABEL, .08))
            with mock.patch.object(cloudllm, "spent_today", return_value=1):
                capped, _ = self.call()
            capped.send_response.assert_called_once_with(429)
            send.assert_called_once()

    def test_codex_stream_failure_falls_back_before_any_reply_bytes(self):
        """Guard fallback before any channel stream bytes reach the client."""
        writer = io.BytesIO()
        def fail(body):
            self.assertEqual(writer.getvalue(), b"")
            raise codexcli.CodexFailure("failed before first delta")
        self.channel.stream.side_effect = fail
        with mock.patch.object(cloudllm, "openrouter_stream", return_value=iter_stream(b"data: [DONE]\n\n")) as cloud:
            h, body = self.call(stream=True, writer=writer)
        self.channel.stream.assert_called_once_with(body)
        self.assertEqual(cloud.call_args.args[1], {**body, "model": FALLBACK})
        h.send_response.assert_called_once_with(200)
        self.assertEqual(h.wfile.getvalue(), b"data: [DONE]\n\n")
        self.assertEqual(self.b.channel_down, "failed before first delta")

    def test_codex_stream_is_relayed_as_sse_and_reports_recovery(self):
        """Guard recovery reporting after a successful channel stream."""
        with mock.patch.object(planchannel, "report_success") as success:
            h, body = self.call(stream=True)
        self.channel.stream.assert_called_once_with(body)
        self.assertEqual(h.wfile.getvalue(), cloudllm.as_sse(reply()))
        success.assert_called_once()

    def test_non_codex_upstream_stream_broke_wording_and_generator_closing_are_preserved(self):
        h, _ = self.call()
        h.wfile = io.BytesIO()
        closed = []
        def stream():
            try:
                yield b'data: {"choices":[]}\n\n'
                raise OSError("upstream disconnected")
            finally:
                closed.append(True)
        h._llm_stream(stream(), "openrouter/test")
        event = json.loads(h.wfile.getvalue().split(b"\n\n")[1][5:])
        self.assertEqual(event, {"error": {"message": "the model's stream broke after 1 lines: OSError('upstream disconnected')"}})
        self.b._log.assert_called_with("bird llm openrouter/test -> ERROR upstream stream broke after 1 lines: OSError('upstream disconnected')")
        self.assertEqual(closed, [True])

    def test_dead_clients_do_not_escape_buffered_codex_replies(self):
        for error in (BrokenPipeError, ConnectionResetError):
            for streaming in (False, True):
                with self.subTest(error=error, streaming=streaming):
                    writer = mock.Mock()
                    writer.write.side_effect = error()
                    self.call(streaming, writer)

    def test_non_codex_stream_closes_generator_when_client_disconnects(self):
        h, _ = self.call()
        for error in (BrokenPipeError, ConnectionResetError):
            with self.subTest(error=error):
                closed = []
                def stream():
                    try:
                        yield b"data: hello\n\n"
                    finally:
                        closed.append(True)
                h.wfile = mock.Mock()
                h.wfile.write.side_effect = error()
                h._llm_stream(stream(), "openrouter/test")
                self.assertEqual(closed, [True])

    def test_rate_is_persisted_on_change_for_other_processes(self):
        rate = {"used_percent": 42, "window_mins": 10080, "resets_at": 999}
        with mock.patch.object(planchannel, "rate_limit", return_value=rate):
            self.call()
            path = self.root / "codex_rate.json"
            before = path.stat().st_mtime_ns
            self.call()
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(panel.codex_rate(), rate)


def iter_stream(*chunks):
    yield from chunks


class ModelsTest(Case):
    def test_instance_validator_accepts_codex_but_supervisor_validators_refuse_it(self):
        self.assertEqual(core.instance_model(MODEL), MODEL)
        for parse in (core.cloud_model, core.claude_model, core.openrouter_model):
            with self.assertRaisesRegex(ValueError, "model"):
                parse(MODEL)

    def test_supervisor_side_calls_accept_codex_models(self):
        """modelcall runs judges, recaps and describe_image on a Codex plan too (a standalone app-server), and the
        coder runs a hand-off there."""
        for name in ("SHAFT_JUDGE2_MODEL", "SHAFT_RECAP_MODEL", "SHAFT_DESCRIBE_MODEL"):
            self.assertIs(core.KNOBS[name].parse, core.any_model, name)
            self.assertEqual(core.KNOBS[name].parse(MODEL), MODEL)
        self.assertEqual(core.KNOBS["SHAFT_CODER_MODEL"].parse(MODEL), MODEL)
        self.assertEqual(core.KNOBS["SHAFT_JUDGE_MODELS"].parse(MODEL), [MODEL])
        with self.assertRaisesRegex(ValueError, "SHAFT_CODEX_MODELS"):
            core.any_model("codex/not-offered")

    def test_operator_choices_and_setting_choices(self):
        with mock.patch.object(opconfig, "_template_text", return_value=""), \
                mock.patch.object(opconfig, "model_choices", wraps=opconfig.model_choices):
            self.assertIn({"model": MODEL, "base_url": "broker", "label": f"{MODEL} (Codex plan)"}, opconfig.model_choices())
            self.assertEqual(opconfig.base_url_for(MODEL), "broker")
            opconfig._check("bird", self.root, "LLM_MODEL", MODEL)
        settings.load_all()
        with mock.patch.object(settings, "model_choices", return_value=[MODEL, "claude-haiku-5-5"]), \
                mock.patch.object(settings, "not_live", return_value={}):
            fields = {f["name"]: f for f in settings.listing()}
        self.assertNotIn(MODEL, fields["SHAFT_TRIAL_FALLBACK_MODEL"]["choices"])
        for name in ("SHAFT_JUDGE_MODELS", "SHAFT_JUDGE2_MODEL", "SHAFT_DESCRIBE_MODEL", "SHAFT_RECAP_MODEL"):
            self.assertIn(MODEL, fields[name]["choices"], name)       # modelcall calls Codex plan models too
        self.assertIn("codexcli", settings.MODULES)

    def test_bakeoff_arms_accept_codex_and_reject_same_judge_across_providers(self):
        self.assertEqual(bakeoff.parse_arm(MODEL + "@high", ("high",))["model"], MODEL)
        with mock.patch.object(bakeoff, "parse_arm", side_effect=[{"model": MODEL, "label": "a"},
                {"model": "claude-haiku-5-5", "label": "b"}]), \
                mock.patch.object(opconfig, "effort_choices", return_value=None):
            with self.assertRaisesRegex(bakeoff.BakeoffError, "also a candidate"):
                bakeoff.create("bird", ["a", "b"], [], judge="openrouter/openai/gpt-6-astra", bid="test")
        redact = bakeoff._redactor({"arms": []})
        self.assertEqual(redact("ran codex/gpt-6-astra with Codex"), "ran [model] with [model]")

    def test_model_normalization_and_vendor(self):
        self.assertTrue(judge.same_model(MODEL, "openrouter/openai/gpt-6-astra"))
        self.assertEqual(consultstats.vendor(MODEL), "openai")
        self.assertTrue(vision.sees_images(MODEL))

    def test_replay_model_image_capability_overrides_config(self):
        # Regression: replay override used to advertise images from the original vision model.
        (self.root / "config.env").write_text('LLM_MODEL="claude-haiku-5-5"\n')
        self.assertTrue(runner._sees_images(self.root))
        self.assertTrue(runner._sees_images(self.root, {"LLM_MODEL": MODEL}))
        with mock.patch.object(vision, "_openrouter_modalities", return_value={"test/text": ["text"]}):
            self.assertFalse(runner._sees_images(self.root, {"LLM_MODEL": "openrouter/test/text"}))

    def test_codex_has_no_local_server_even_with_a_selfhosted_prefix_collision(self):
        cfg = {"LLM_BASE_URL": "broker", "LLM_MODEL": MODEL}
        with mock.patch.object(runner, "instance_config", return_value=cfg), \
                mock.patch("supervisor.selfhosted.SERVERS", {"codex": {"url": "http://127.0.0.1:8780/v1"}}):
            self.assertIsNone(runner.model_server("bird"))
            self.assertEqual(localmodel.pinned_by(names=["bird"], config_of=lambda name: cfg), [])

    def test_trial_codex_is_nonlocal_and_explicit_trial_model_is_used(self):
        model = trial.model_of({"config.env": f'LLM_BASE_URL="broker"\nLLM_MODEL="{MODEL}"'.encode()})
        self.assertEqual(model, {"local": False, "model": MODEL, "port": None})
        self.assertEqual(trial._pair_model({"model": MODEL, "llm": {"local": True}}, {}, self.root), MODEL)
        with mock.patch.object(trial, "local_free_for", side_effect=AssertionError("must not use local server")):
            self.assertEqual(trial._pair_model({"model": "auto", "llm": model}, {}, self.root), "own")
        with mock.patch.object(trial, "_replay", return_value={"completed": True}) as replay:
            result = trial.replay_arm(self.root, "bird", {}, {}, model=MODEL, cache={},
                                      cache_lock=threading.Lock(), billed="test")
        self.assertEqual(result, {"completed": True})
        self.assertEqual(replay.call_args.kwargs["model"], MODEL)

    def test_replay_arm_does_not_validate_internal_model_placeholders(self):
        for model in ("cloud", "fake"):
            with self.subTest(model=model), mock.patch.object(trial, "_replay", return_value={"completed": True}) as replay:
                result = trial.replay_arm(self.root, "bird", {}, {}, model=model, cache={},
                                          cache_lock=threading.Lock(), billed="test")
                self.assertEqual(result, {"completed": True})
                self.assertEqual(replay.call_args.kwargs["model"], model)

    def test_trial_creation_accepts_codex(self):
        with mock.patch.object(trial, "committed_code", return_value={}), \
                mock.patch.object(trial, "_new_id", return_value="test"), \
                mock.patch.object(trial, "_choose", return_value=[{"ts": "one"}] * trial.N_MIN), \
                mock.patch.object(trial, "_freeze"), mock.patch.object(trial, "save"), \
                mock.patch.object(trial, "log"):
            t = trial.create("bird", {}, "test", by="operator", allow_empty=True, model=MODEL, project=self.root)
        self.assertEqual(t["model"], MODEL)
        self.assertEqual(trial._pair_model(t, {}, self.root), MODEL)

    def test_resource_shock_refuses_a_codex_instance_clearly(self):
        code = {"shaft/config.py": b'"MAX_STEPS_LIVE": (int, 6), "FORECAST": (str, "on")',
                "config.env": f'LLM_BASE_URL="broker"\nLLM_MODEL="{MODEL}"'.encode()}
        with mock.patch.object(trial, "committed_code", return_value=code):
            with self.assertRaisesRegex(experiments.ExperimentError, "codex/ is not supported"):
                experiments.resource_shock("bird", dry_run=True, project=self.root)

    def test_runner_environment_uses_replay_model_for_image_capability(self):
        (self.root / "config.env").write_text('LLM_MODEL="claude-haiku-5-5"\n')
        b = mock.Mock(port=123, shared=None, env_root=self.root)
        b.egress.port = 124
        b.mail_env.return_value = b.calendar_env.return_value = b.gdrive_env.return_value = ""
        b.sandbox_env.return_value = {}
        with mock.patch.object(runner.sandbox, "profile", return_value=""), \
                mock.patch.object(runner.sandbox, "wrap", side_effect=lambda profile, args: args), \
                mock.patch.object(runner, "stage_docs"):
            _, env = runner.agent_command(self.root, b, ["live"], self.root / "profile.sb", [], {"LLM_MODEL": MODEL})
        self.assertEqual(env["SHAFT_VISION"], "1")
        self.assertEqual(env["LLM_MODEL"], MODEL)

    def test_trial_budget_records_codex_as_cloud_without_local_units(self):
        record = {"ts": datetime.now().isoformat(), "instance": "bird", "kind": "arm", "model": MODEL,
                  "usd": None, "by": "instance"}
        path = self.root / "trial_usage.jsonl"
        path.write_text(json.dumps(record) + "\n")
        with mock.patch.object(trial, "LEDGER", path), mock.patch.object(trial, "all_trials", return_value=[]):
            self.assertEqual(trial.usage_today("bird"), {"usd": 0.0, "local_units": 0, "started": 0})

    def test_panel_instance_label_names_the_codex_plan(self):
        from supervisor import messages, metrics
        replacements = {"instance_root": self.root, "instance_config": {"LLM_BASE_URL": "broker", "LLM_MODEL": MODEL},
                        "safe_read": "", "journal_entries": [], "paused": False, "disabled_tools": [],
                        "shared_folder": None, "mail_grants": [], "calendar_grants": [], "_jobs_summary": "",
                        "changes": [], "template_merges": [], "traits": {}}
        for name, value in replacements.items():
            self.enterContext(mock.patch.object(panel, name, return_value=value))
        with mock.patch.object(messages, "pending", return_value=[]), \
                mock.patch.object(metrics, "judgments", return_value=[]), \
                mock.patch.object(panel.pyenv, "describe", return_value=""), \
                mock.patch.object(panel.browser, "describe", return_value=""), \
                mock.patch.object(panel.egress, "recent", return_value=[]):
            self.assertEqual(panel.instance_detail("bird")["via"], "broker (Codex plan)")

    def test_version_names_the_codex_plan(self):
        (self.root / "config.env").write_text(f'LLM_BASE_URL="broker"\nLLM_MODEL="{MODEL}"\n')
        with mock.patch.object(core, "instance_root", return_value=self.root), \
                mock.patch.object(version, "instance", return_value={"commit": "x", "template_behind": 0}), \
                mock.patch.object(version, "supervisor_commit", return_value="abc"):
            self.assertIn(f"model {MODEL} via the broker (Codex plan)", version.describe("bird"))

    def test_unsupported_consult_and_probes_refuse_plans_clearly(self):
        """Probes still need an OpenRouter model; a consult runs on a Codex plan (below) but not on Grok's."""
        with self.assertRaisesRegex(experiments.ExperimentError, "openrouter/"):
            experiments._probe_model(MODEL)
        with self.assertRaisesRegex(consult.ConsultError, "grok/ is not supported"):
            consult.complete("bird", {"model": "grok/grok-4.7"}, {}, None, None, None)
        with mock.patch.object(consult, "MODELS", ["grok/grok-4.7"]), \
                mock.patch.object(consult, "_catalogue", return_value={"models": [], "endpoints": []}):
            item = consult.menu("bird")[0]
        self.assertFalse(item["available"])
        self.assertIn("grok/ is not supported", item["why"])

    def test_consult_on_codex_is_plan_priced(self):
        """A ChatGPT-plan install consults the plan's other models: no dollars (usd 0, plan: true), its own model
        left off the menu, the answer through a short-lived Codex app-server of the supervisor's own."""
        from supervisor.tests.test_modelcall import FakeCodex
        fake = FakeCodex(self, self.root)
        fake.say("from codex")
        other = "codex/gpt-6.1-sol"
        self.enterContext(mock.patch.object(consult, "USAGE", self.root / "consult_usage.jsonl"))
        self.enterContext(mock.patch.object(consult, "SUP_DIR", self.root))
        self.enterContext(mock.patch.object(consult, "MODELS", [MODEL, other]))
        self.enterContext(mock.patch.object(core, "instance_config", return_value={"LLM_MODEL": MODEL}))
        self.enterContext(mock.patch.object(consult, "_catalogue", side_effect=AssertionError("no catalogue")))
        self.assertEqual(consult.menu("bird"), [{"model": other, "plan": True, "in_per_m": 0.0, "out_per_m": 0.0,
                                                 "reserve_in_per_m": 0.0, "reserve_out_per_m": 0.0,
                                                 "available": True, "why": ""}])
        body = {"model": other, "messages": [{"role": "system", "content": "be brief"},
                                             {"role": "user", "content": "hello"}], "max_tokens": 100}
        reply = consult.complete("bird", body, {}, "plugin", "live", "2026-11-12T10:00:00")
        self.assertEqual(reply["choices"][0]["message"]["content"], "from codex")
        self.assertEqual(reply["shaft"]["usd"], 0.0)
        self.assertEqual(fake.requests("thread/start")[-1]["params"]["model"], "gpt-6.1-sol")
        self.assertEqual(fake.requests("thread/start")[-1]["params"]["baseInstructions"], "be brief")
        row = json.loads(consult.USAGE.read_text())
        self.assertEqual((row["model"], row["usd"], row["reserved_usd"], row["plan"]), (other, 0.0, 0.0, "codex"))
        self.assertEqual(consult._reservations(), [])
        with self.assertRaisesRegex(consult.ConsultError, "own model"):
            consult.complete("bird", {**body, "model": MODEL}, {}, "plugin", "live", None)

    def test_fallback_knobs_take_any_supervisor_model_or_none(self):
        for name in ("SHAFT_CODEX_FALLBACK_MODEL", "SHAFT_GROK_FALLBACK_MODEL", "SHAFT_TRIAL_FALLBACK_MODEL"):
            parse = core.KNOBS[name].parse
            for value in ("none", "claude-haiku-5-5", "openrouter/x/y"):
                self.assertEqual(parse(value), value, name)
            with self.assertRaises(ValueError):
                parse("codex/not-offered")
        self.assertEqual(core.KNOBS["SHAFT_TRIAL_FALLBACK_MODEL"].parse(MODEL), MODEL)
        for name in ("SHAFT_CODEX_FALLBACK_MODEL", "SHAFT_GROK_FALLBACK_MODEL"):   # a failed channel's fallback is
            with self.assertRaisesRegex(ValueError, "none"):                     # called directly, not on a plan
                core.KNOBS[name].parse(MODEL)


class AccountingTest(Case):
    def test_codex_has_known_zero_run_spend_fallback_has_priced_run_spend(self):
        charges = []
        token = ledger.charges.set(charges)
        self.addCleanup(ledger.charges.reset, token)
        cloudllm._record("bird", MODEL, 20, 5, None, time.monotonic(), "stop", None)
        self.assertEqual(charges, [])
        with cloudllm.fallback_label(LABEL):
            cloudllm._record("bird", FALLBACK, 20, 5, .25, time.monotonic(), "stop", None)
        self.assertEqual(charges, [.25])
        rows = list(map(json.loads, cloudllm.USAGE.read_text().splitlines()))
        self.assertEqual([(r["model"], r["usd"]) for r in rows], [(MODEL, None), (LABEL, .25)])

    def test_fallback_stream_has_label_and_priced_usage(self):
        data = {"model": "test/fallback", "choices": [{"finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "cost": .05}}
        upstream = io.BytesIO(b"data: " + json.dumps(data).encode() + b"\n\ndata: [DONE]\n\n")
        with cloudllm.fallback_label(LABEL):
            chunks = list(cloudllm._relay("bird", LABEL, upstream, time.monotonic()))
        self.assertEqual(json.loads(chunks[0][5:])["model"], LABEL)
        row = json.loads(cloudllm.USAGE.read_text())
        self.assertEqual((row["model"], row["usd"]), (LABEL, .05))

    def test_panel_excludes_plan_rows_from_unpriced_and_keeps_fallback_dollars(self):
        cloudllm._record("bird", MODEL, 2, 1, None, time.monotonic(), "stop", None)
        cloudllm._record("bird", LABEL, 2, 1, .2, time.monotonic(), "stop", None)
        with mock.patch.object(panel, "instance_names", return_value=[]):
            costs = panel.costs()
        self.assertEqual(costs["unpriced"], 0)
        self.assertEqual(costs["today"]["total"], .2)

    def test_panel_outage_is_stable_and_security_wording_is_explicit(self):
        with mock.patch.object(panel, "_log_failures", return_value=[]), \
                mock.patch.object(panel, "provider_alerts", return_value=[]), \
                mock.patch.object(core, "long_pauses", return_value=[]), \
                mock.patch.object(panel, "CORRECTION_ALERT_CYCLES", 0):
            for security in (False, True):
                (self.root / "codex_outage.json").write_text(json.dumps({"security": security, "reason": "failed"}))
                alerts = panel.alerts(cost={"caps": [], "judge_today_total": 0, "judge_alert": 1})
                self.assertEqual(alerts, [{"id": "provider:codex:outage", "text":
                    ("Codex security event" if security else "Codex unavailable") + ": failed"}])

    def test_fallback_buffered_stream_names_the_model(self):
        chunks = cloudllm.as_sse(reply(LABEL)).split(b"\n\n")
        for chunk in chunks:
            if chunk.startswith(b"data: {"):
                self.assertEqual(json.loads(chunk[5:])["model"], LABEL)

    def test_web_costs_show_persisted_weekly_percentage(self):
        from supervisor import web_views
        (self.root / "codex_rate.json").write_text('{"used_percent":42}')
        with mock.patch.object(panel, "instance_names", return_value=[]):
            costs = panel.costs()
        self.assertIn("Codex plan: 42% of weekly allowance used", str(web_views.costs_page(costs, [], "csrf")))

    def test_terminal_costs_show_persisted_weekly_percentage(self):
        try:
            from rich.console import Console
            from supervisor import top, top_views
        except ModuleNotFoundError as e:
            self.skipTest(str(e))
        (self.root / "codex_rate.json").write_text('{"used_percent":42}')
        with mock.patch.object(panel, "instance_names", return_value=[]):
            costs = panel.costs()
        out = io.StringIO()
        with mock.patch.object(panel, "costs", return_value=costs):
            Console(file=out, width=120).print(top_views.render_costs({}, top.UI(tab="costs"), 120, 60))
        self.assertIn("Codex plan: 42% of weekly allowance used", out.getvalue())

    def test_provider_alerts_recognize_codex(self):
        cloudllm.USAGE.write_text(json.dumps({"ts": datetime.now().isoformat(), "model": MODEL,
                                              "error": "HTTP 429: limit [HTTP 429 responses: 5]"}) + "\n")
        self.assertEqual([r["id"] for r in panel.provider_alerts()], ["provider:codex:rate-limit"])


class CLITest(Case):
    def test_login_inherits_terminal_and_uses_private_home_mode_700(self):
        home = self.root / "private-home"
        with mock.patch.object(codexcli, "HOME", home), \
                mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(cli.main(["codex", "login"]), 0)
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        self.assertEqual(run.call_args.args, ([codexcli.BIN, "login"],))
        self.assertEqual(set(run.call_args.kwargs), {"env"})
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], str(home.resolve()))

    def test_trial_run_cli_accepts_codex(self):
        with mock.patch.object(cli, "instance_root", return_value=self.root), \
                mock.patch.object(trial, "create", return_value={"id": "test", "states": []}) as create, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["trial", "run", "bird", "--model", MODEL, "--aa"]), 0)
        self.assertEqual(create.call_args.kwargs["model"], MODEL)

    def test_trial_replay_cli_accepts_codex(self):
        rec = {"episode": None, "exit": 0, "ended": "ok", "seconds": 1, "completed": True,
               "usage": {}, "usd": 0, "trace": [], "diff": ""}
        with mock.patch.object(cli, "instance_root", return_value=self.root), \
                mock.patch.object(trial, "live_episodes", return_value=[{"ts": "one"}]), \
                mock.patch.object(trial, "state_of", return_value={"start_commit": "abc", "t0": "one", "inbox": []}), \
                mock.patch.object(trial, "committed_code", return_value={}), \
                mock.patch.object(trial, "replay_arm", return_value=rec) as replay, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["trial", "replay", "bird", "one", "--model", MODEL]), 0)
        self.assertEqual(replay.call_args.kwargs["model"], MODEL)

    def test_status_reports_login_weekly_models_and_outage_without_running_cli(self):
        (self.root / "codex_rate.json").write_text('{"used_percent":43}')
        (self.root / "codex_outage.json").write_text('{"reason":"native tool","security":true}')
        with mock.patch.object(codexcli, "HOME", self.root / "absent-home"), \
                mock.patch.object(cli.subprocess, "run") as run, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["codex", "status"]), 0)
        run.assert_not_called()
        for text in ("login: missing", "43% used", MODEL, "Codex security event: native tool"):
            self.assertIn(text, out.getvalue())

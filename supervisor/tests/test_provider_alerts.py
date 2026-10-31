"""M4: provider limits must reach the operator once and clear on recovery."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import cloudllm, core, panel, web_alerts, webpush


class ProviderAlertsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.usage = self.root / "cloud_usage.jsonl"
        for patch in (mock.patch.object(core, "SUP_DIR", self.root),
                      mock.patch.object(panel, "SUP_DIR", self.root),
                      mock.patch.object(cloudllm, "USAGE", self.usage),
                      mock.patch.object(cloudllm.time, "sleep"),
                      mock.patch.object(webpush, "notify", return_value=1)):
            patch.start()
            self.addCleanup(patch.stop)
        self.body = {"model": "openrouter/test", "messages": []}

    def failure(self, status, body):
        error = urllib.error.HTTPError("https://openrouter.ai", status, "failed", {}, io.BytesIO(body.encode()))
        with mock.patch.object(cloudllm.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(cloudllm.CloudError):
                cloudllm._openrouter("wren", self.body, "key")

    def test_a_fault_older_than_a_day_raises_nothing(self):
        # a 402 from weeks ago, with the provider unused since, must not stand as an open alert forever
        old = (datetime.now() - timedelta(hours=25)).isoformat()
        self.usage.write_text(json.dumps({"ts": old, "model": "openrouter/test", "error": "HTTP 402: credits"}) + "\n")
        self.assertEqual(panel.provider_alerts(), [])

    def success(self):
        data = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"cost": 0}}
        with mock.patch.object(cloudllm.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
            cloudllm._openrouter("wren", self.body, "key")

    def test_403_key_limit_streak_alerts_and_pushes_once_then_success_clears(self):
        for _ in range(3):
            self.failure(403, "key credit limit exceeded")
        self.assertEqual(panel.provider_alerts(), [{"id": "provider:openrouter:limit",
                         "text": "openrouter: HTTP 403: provider limit, credit or quota exhausted"}])
        with mock.patch.object(panel, "_log_failures", return_value=[]), \
                mock.patch.object(core, "long_pauses", return_value=[]), \
                mock.patch.object(panel, "CORRECTION_ALERT_CYCLES", 0):
            alerts = panel.alerts(cost={"caps": [], "judge_today_total": 0, "judge_alert": 1})
        self.assertEqual([a["id"] for a in alerts], ["provider:openrouter:limit"])
        self.assertEqual(web_alerts.forward_providers(), 1)
        self.assertEqual(web_alerts.forward_providers(), 0)
        webpush.notify.assert_called_once()
        self.success()
        self.assertEqual(panel.provider_alerts(), [])
        self.assertEqual(web_alerts.forward_providers(), 0)
        self.failure(403, "quota exceeded")
        self.assertEqual(web_alerts.forward_providers(), 1)

    def test_402_alerts_but_unrelated_403_does_not(self):
        self.failure(403, "access denied")
        self.assertEqual(panel.provider_alerts(), [])
        self.failure(402, "payment required")
        self.assertEqual([a["id"] for a in panel.provider_alerts()], ["provider:openrouter:credit"])

    def test_429_threshold_counts_http_retries_not_just_failed_calls(self):
        self.failure(429, "slow down")
        self.assertEqual(panel.provider_alerts(), [])
        self.failure(429, "slow down")
        self.assertTrue(all(r["error"].endswith("[HTTP 429 responses: 3]")
                            for r in map(json.loads, self.usage.read_text().splitlines())))
        self.assertEqual([a["id"] for a in panel.provider_alerts()], ["provider:openrouter:rate-limit"])
        self.success()
        self.assertEqual(panel.provider_alerts(), [])

    def test_429_spread_over_more_than_ten_minutes_does_not_alert(self):
        now = datetime.now()
        rows = [{"ts": (now - timedelta(minutes=m)).isoformat(), "model": "claude-test", "error": "HTTP 429: wait"}
                for m in (44, 33, 22, 11, 0)]
        self.usage.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(panel.provider_alerts(), [])

    def test_failed_provider_push_is_retried(self):
        self.failure(403, "limit exceeded")
        webpush.notify.return_value = 0
        self.assertEqual(web_alerts.forward_providers(), 0)
        webpush.notify.return_value = 1
        self.assertEqual(web_alerts.forward_providers(), 1)
        self.assertEqual(web_alerts.forward_providers(), 0)

    def test_streaming_key_limit_alert_clears_after_successful_stream(self):
        with mock.patch.object(cloudllm, "CLOUD_MODELS", [self.body["model"]]), \
                mock.patch.object(cloudllm, "spent_today", return_value=0), \
                mock.patch.object(cloudllm, "daily_cap", return_value=8):
            error = urllib.error.HTTPError("https://openrouter.ai", 403, "failed", {}, io.BytesIO(b"key limit"))
            with mock.patch.object(cloudllm.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(cloudllm.CloudError):
                    cloudllm.openrouter_stream("wren", self.body, {"OPENROUTER_API_KEY": "key"})
            self.assertEqual([a["id"] for a in panel.provider_alerts()], ["provider:openrouter:limit"])
            chunk = {"choices": [{"finish_reason": "stop"}], "usage": {"cost": 0}}
            response = io.BytesIO(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n").encode())
            with mock.patch.object(cloudllm.urllib.request, "urlopen", return_value=response):
                list(cloudllm.openrouter_stream("wren", self.body, {"OPENROUTER_API_KEY": "key"}))
        self.assertEqual(panel.provider_alerts(), [])

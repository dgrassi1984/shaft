"""Cross-module delivery-control regression from the mind review."""
from unittest import mock

from supervisor import judge, judge_probe, metrics
from supervisor.tests.test_idle_exit import IsolatedCase, NOW
from supervisor.tests.test_judge_links import verdict


class ReviewMindTest(IsolatedCase):
    def test_planted_delivery_probe_keeps_schema_without_main_links(self):
        """The blind delivery control must not inherit main-judge link questions."""
        ep = {"kind": "live", "ts": NOW.isoformat(), "intent": {"goal": "Send report"},
              "finish": {"delivered_via": "message"}, "tools": [{"tool": "send_message",
              "args": '{"to": "operator"}', "result": "sent"}]}
        self.write(self.root / "memory/episodes/day.jsonl", [ep])
        self.write(self.sup / "judgments/ember.jsonl", [{"episode_ts": ep["ts"], "delivery_supported": "yes"}])
        with mock.patch.object(judge_probe, "datetime", mock.Mock(now=lambda: NOW)), \
             mock.patch.object(judge, "available", return_value=True), \
             mock.patch.object(judge, "pick", return_value="example-judge"), \
             mock.patch.object(metrics, "own_models", return_value=[]), \
             mock.patch.object(metrics, "_written_files", return_value=""), \
             mock.patch.object(metrics, "_provenance_evidence", return_value=""), \
             mock.patch.object(metrics, "_followed_surprise", return_value=""), \
             mock.patch.object(judge, "ask", return_value=verdict()) as ask:
            self.assertEqual(judge_probe.run("ember"), 1)
        ask.assert_called_once()
        self.assertIs(ask.call_args.args[2], metrics.JUDGE_SCHEMA)

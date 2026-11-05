"""Provider-neutral message identity and persistent channel state."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from supervisor import planchannel


class MessageTest(unittest.TestCase):
    def test_anchor_hashes_images_and_ignores_old_images_rewritten_to_text(self):
        url = "data:image/png;base64," + "A" * 1_000_000
        old = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}
        messages = [old, {"role": "assistant", "content": "anchor"}, old]
        real_hash = planchannel.hashlib.sha256
        with mock.patch.object(planchannel.hashlib, "sha256", wraps=real_hash) as digest:
            anchor = planchannel._anchor(messages)
            self.assertEqual(digest.call_args_list[0].args[0], url.encode())
            self.assertLess(len(digest.call_args_list[1].args[0]), 300)
        messages[0] = {"role": "user", "content": "[image shown earlier, no longer in view: photo.png]"}
        self.assertEqual(planchannel._anchor(messages), anchor)
        changed_url = url[:-1] + "B"
        self.assertEqual(len(changed_url), len(url))
        messages[-1] = {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": changed_url}}]}
        self.assertNotEqual(planchannel._anchor(messages), anchor)


class OutageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enterContext(mock.patch.object(planchannel, "SUP_DIR", Path(self.temp.name)))

    def test_outage_persists_and_only_notifies_on_transitions(self):
        state = planchannel.ProviderState("codex", "Codex")
        notify = mock.Mock()
        state.report_failure(planchannel.ChannelSecurity("native item"), notify)
        self.assertEqual(state.outage()["security"], True)
        self.assertEqual(state.outage()["reason"], "native item")
        state.report_failure(planchannel.ChannelFailure("later"), notify)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args.args, ("Codex security event", "native item"))
        state.report_success(notify)
        state.report_success(notify)
        self.assertIsNone(state.outage())
        self.assertEqual(notify.call_count, 2)

    def test_provider_outages_and_rates_are_independent(self):
        codex = planchannel.ProviderState("codex", "Codex")
        bridge = planchannel.ProviderState("bridge", "Bridge")
        notify = mock.Mock()
        codex.report_failure(planchannel.ChannelFailure("offline"), notify)
        bridge.report_failure(planchannel.ChannelSecurity("native call"), notify)
        self.assertEqual(codex.outage()["reason"], "offline")
        self.assertEqual(bridge.outage()["reason"], "native call")
        codex.report_success(notify)
        self.assertIsNone(codex.outage())
        self.assertTrue(bridge.outage()["security"])
        codex.rate = {"used_percent": 42}
        self.assertIsNone(bridge.rate_limit())
        rate = codex.rate_limit()
        rate["used_percent"] = 100
        self.assertEqual(codex.rate_limit(), {"used_percent": 42})
        self.assertEqual(notify.call_args.args, ("Codex recovered", "Codex calls are succeeding again."))


class BridgeChannel(planchannel.Channel):
    """In-memory backend: user deltas go into answers, without steering or RPC."""
    prefix, label = "bridge", "Bridge"
    timeout_s = 3

    def __init__(self, *args):
        super().__init__(*args)
        self.sessions, self.turns, self.answers, self.cancels = [], [], [], []
        self.handle = object()   # pending handles are opaque to the selector

    def _start(self, model, effort):
        self._process = mock.Mock()

    def _open_session(self, model, system, tools, effort):
        self.sessions.append((model, system, tools, effort))
        return f"session-{len(self.sessions)}"

    def _start_turn(self, text, images, effort):
        self.turns.append((text, images, effort))
        step = self._step
        step.calls.append({"id": "call", "type": "function", "function": {"name": "echo", "arguments": "{}"}})
        step.pending.append(self.handle)
        self._finish(step, "tool_calls")
        return f"turn-{len(self.turns)}"

    def _answer_calls(self, pending, results, text, images):
        self.answers.append((pending, results, text, images))
        self._step.text = "answered"
        self._finish(self._step, "stop")
        return True

    def _cancel_turn(self, turn):
        self.cancels.append(turn)

    def _kill(self):
        pass


class SelectorTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(planchannel.PROVIDERS, {"bridge": ".fake_bridge"}))
        self.record = self.enterContext(mock.patch.object(planchannel.cloudllm, "_record"))
        self.channel = BridgeChannel("broker", "usage", lambda s: None)
        self.addCleanup(self.channel.close)
        self.body = {"model": "bridge/model", "messages": [
            {"role": "system", "content": "rules"}, {"role": "user", "content": "begin"}], "tools": []}

    def test_new_repeat_and_bridge_answer_with_user_text_and_images(self):
        first = self.channel.complete(self.body)
        self.assertEqual(self.channel.complete(self.body), first)
        self.assertEqual(self.channel.sessions, [("bridge/model", "rules", [], None)])
        delta = {**self.body, "messages": self.body["messages"] + [first["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "call", "content": "result"},
            {"role": "user", "content": [{"type": "text", "text": "also look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,synthetic"}}]}]}
        reply = self.channel.complete(delta)
        self.assertEqual(reply["choices"][0]["message"], {"role": "assistant", "content": "answered"})
        self.assertEqual(self.channel.answers, [([self.channel.handle], {"call": "result"},
            "[message]\nalso look", ["data:image/png;base64,synthetic"])])
        self.assertEqual(len(self.channel.turns), 1)
        self.assertEqual(self.channel.stats["extends"], 1)
        self.assertEqual(self.channel.stats["repeats"], 1)
        self.assertEqual(self.record.call_count, 2)

    def test_tools_change_cancels_and_rebuilds_with_transcript(self):
        self.channel.complete(self.body)
        changed = {**self.body, "tools": [{"type": "function", "function": {"name": "echo"}}]}
        self.channel.complete(changed)
        self.assertEqual(self.channel.cancels, ["turn-1"])
        self.assertEqual(len(self.channel.sessions), 2)
        self.assertEqual(self.channel.stats["rebuilds"], 2)
        self.assertEqual(json.loads(self.channel.turns[1][0].splitlines()[1]), self.body["messages"][1:])

    def test_session_effort_change_rebuilds_for_process_bound_backends(self):
        self.channel.complete({**self.body, "reasoning_effort": "low"})
        self.channel.complete({**self.body, "reasoning_effort": "high"})
        self.assertEqual([s[3] for s in self.channel.sessions], ["low", "high"])
        self.assertEqual(self.channel.cancels, ["turn-1"])

    def test_registry_factory_and_metadata_select_backend_by_prefix(self):
        import sys
        import types
        module = types.ModuleType("supervisor.fake_bridge")
        module.Channel = BridgeChannel
        module.models = lambda: ["bridge/model"]
        module.LABEL, module.VENDOR, module.SEES_IMAGES = "Bridge", "vendor", True
        module.FALLBACK_MODEL = "openrouter/fallback"
        module.STATE = planchannel.ProviderState("bridge", "Bridge")
        with mock.patch.dict(sys.modules, {"supervisor.fake_bridge": module}):
            channel = planchannel.channel_for("bridge/model", "b", "u", lambda s: None)
            self.addCleanup(channel.close)
            self.assertIsInstance(channel, BridgeChannel)
            self.assertIn("bridge/model", planchannel.models())
            self.assertEqual(planchannel.label("bridge/model"), "Bridge")
            self.assertEqual(planchannel.vendor("bridge/model"), "vendor")
            self.assertEqual(planchannel.fallback_model("bridge/model"), "openrouter/fallback")
            self.assertTrue(planchannel.sees_images("bridge/model"))
        self.assertTrue(planchannel.serves("bridge/model"))
        self.assertFalse(planchannel.serves("bridge/"))
        self.assertFalse(planchannel.serves(None))
        self.assertFalse(planchannel.serves("unknown/model"))

    def continuation(self):
        first = self.channel.complete(self.body)
        return {**self.body, 'messages': self.body['messages'] + [first['choices'][0]['message'],
            {'role':'tool', 'tool_call_id':'call', 'content':'critical result'},
            {'role':'user', 'content':'continue'}]}

    def test_bug2_ended_held_grok_call_rebuilds_full_transcript_under_lock(self):
        self.channel.calls_need_turn = True
        continuation = self.continuation()
        self.channel._step.turn_ended = True
        self.channel.complete(continuation)
        self.assertEqual(self.channel.answers, [])
        self.assertEqual(self.channel.cancels, [])
        self.assertEqual(len(self.channel.sessions), 2)
        self.assertIn('critical result', self.channel.turns[-1][0])
        self.assertIn('continue', self.channel.turns[-1][0])

    def test_bug2_grok_nothing_delivered_rebuilds_but_codex_keeps_sent_results(self):
        for needs_turn in (True, False):
            with self.subTest(needs_turn=needs_turn):
                self.channel.calls_need_turn = needs_turn
                continuation = self.continuation()
                count = len(self.channel.sessions)
                with mock.patch.object(self.channel, '_answer_calls', return_value=False):
                    self.channel.complete(continuation)
                text = self.channel.turns[-1][0]
                if needs_turn:
                    self.assertEqual(len(self.channel.sessions), count + 1)
                    self.assertIn('critical result', text)
                else:
                    self.assertEqual(len(self.channel.sessions), count)
                    self.assertEqual(text, '[message]\ncontinue')
                self.body = {**self.body, 'messages':[{'role':'user', 'content':'fresh'}]}

    def test_bug3_held_step_recorded_exactly_once_even_if_cancel_raises(self):
        self.channel.complete(self.body)
        old = self.channel._step
        self.assertFalse(old.recorded)
        with mock.patch.object(self.channel, '_cancel_turn', side_effect=planchannel.ChannelFailure('cancel failed')):
            with self.assertRaisesRegex(planchannel.ChannelFailure, 'cancel failed'):
                self.channel.complete({**self.body, 'tools':[{'type':'function', 'function':{'name':'changed'}}]})
        self.assertTrue(old.recorded)
        self.assertEqual(self.record.call_count, 1)
        self.channel.close()
        self.assertEqual(self.record.call_count, 1)

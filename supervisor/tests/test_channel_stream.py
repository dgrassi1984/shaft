"""Channel streaming through the existing fake Codex app-server, without network."""
import json
import unittest
from unittest import mock

from supervisor import codexcli, planchannel
from supervisor.tests import test_codexcli as fixtures


def chunks(lines):
    return [json.loads(line[5:]) for line in lines
            if line.startswith(b'data:') and line.strip() != b'data: [DONE]']


class ChannelStreamTest(unittest.TestCase):
    setUp = fixtures.ChannelTest.setUp
    use = fixtures.ChannelTest.use

    def test_text_arrives_before_turn_completed_and_replays(self):
        """Guard M0: Thelos's first words were held until its step ended."""
        self.use([[{'text': 'hello world', 'deltas': ['hello', ' world'], 'gap': .15}, {'usage': 12}]])
        stream = self.channel.stream(fixtures.body())
        first = next(stream)
        self.assertEqual(chunks([first])[0]['choices'][0]['delta'], {'content': 'hello'})
        self.assertFalse(self.channel._step.event.is_set())
        replay = self.channel.stream(fixtures.body())
        self.assertEqual(next(replay), first)
        rest = list(stream)
        self.assertEqual(list(replay), rest)
        self.assertEqual(chunks([first, *rest]), [
            {'choices': [{'index': 0, 'delta': {'content': 'hello'}, 'finish_reason': None}]},
            {'choices': [{'index': 0, 'delta': {'content': ' world'}, 'finish_reason': None}]},
            {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
            {'choices': [], 'usage': {'prompt_tokens': 12, 'completion_tokens': 2}, 'shaft_same_turn': False}])
        self.assertEqual(rest[-2:], [b'data: [DONE]\n', b'\n'])
        self.record.assert_called_once_with('usage-label', 'codex/gpt-6-astra', 12, 2, None,
            self.channel._step.started, 'stop', None, cache={'cached_input_tokens': 3})

    def test_disagreement_after_forwarded_delta_fails(self):
        """Guard against silently replacing text already sent to the instance."""
        self.use([[{'text': 'different', 'deltas': ['hello'], 'gap': .15}]])
        stream = self.channel.stream(fixtures.body())
        self.assertEqual(chunks([next(stream)])[0]['choices'][0]['delta'], {'content': 'hello'})
        with self.assertRaisesRegex(codexcli.CodexFailure, 'Codex agent text disagrees with deltas'):
            list(stream)
        with self.assertRaisesRegex(codexcli.CodexFailure, 'Codex agent text disagrees with deltas'):
            self.channel.complete(fixtures.body())
        self.assertEqual(self.record.call_args.args[7], 'CodexFailure')

    def test_tool_text_suffix_calls_usage_and_complete_unchanged(self):
        """Guard against lost final suffixes, changed tool arguments, or counted held turns."""
        self.use([[{'text': 'checking now', 'deltas': ['checking']}, {'call': 1}]])
        request = fixtures.body()
        data = chunks(list(self.channel.stream(request)))
        call = {'id': 'call-1', 'type': 'function', 'function': {'name': 'echo', 'arguments': '{"n": 1}'}}
        self.assertEqual(data, [
            {'choices': [{'index': 0, 'delta': {'content': 'checking'}, 'finish_reason': None}]},
            {'choices': [{'index': 0, 'delta': {'content': ' now'}, 'finish_reason': None}]},
            {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, **call}]}, 'finish_reason': None}]},
            {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]},
            {'choices': [], 'usage': {'prompt_tokens': 0, 'completion_tokens': 0}, 'shaft_same_turn': True}])
        expected = {'id': f'codex-{id(self.channel._step)}', 'object': 'chat.completion', 'model': 'codex/gpt-6-astra',
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'checking now',
                    'tool_calls': [call]}, 'finish_reason': 'tool_calls'}],
                    'usage': {'prompt_tokens': 0, 'completion_tokens': 0}, 'shaft_same_turn': True}
        self.assertEqual(json.dumps(self.channel.complete(request)), json.dumps(expected))
        self.record.assert_not_called()
        self.channel.close()
        self.record.assert_called_once_with('usage-label', 'codex/gpt-6-astra', 0, 0, None,
            self.channel._step.started, 'tool_calls', None, cache={'cached_input_tokens': 0})

    def test_complete_plain_result_unchanged(self):
        """Guard the non-stream completion wire format while introducing streaming."""
        self.use([[{'text': 'hello', 'deltas': ['hel', 'lo']}, {'usage': 12}]])
        reply = self.channel.complete(fixtures.body())
        expected = {'id': f'codex-{id(self.channel._step)}', 'object': 'chat.completion', 'model': 'codex/gpt-6-astra',
                    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'hello'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 12, 'completion_tokens': 2}, 'shaft_same_turn': False}
        self.assertEqual(json.dumps(reply).encode(), json.dumps(expected).encode())

    def test_provider_without_deltas_emits_final_text(self):
        """Guard Grok's buffered text when the shared channel gains streaming."""
        channel = planchannel.Channel('broker', 'usage', lambda _: None)
        step = planchannel.Step('grok/example', 'anchor', text='hello', finish='stop')
        step.event.set()
        with mock.patch.object(channel, '_select', return_value=step):
            data = chunks(list(channel.stream({})))
        self.assertEqual(data, [
            {'choices': [{'index': 0, 'delta': {'content': 'hello'}, 'finish_reason': None}]},
            {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
            {'choices': [], 'usage': {'prompt_tokens': 0, 'completion_tokens': 0}, 'shaft_same_turn': False}])

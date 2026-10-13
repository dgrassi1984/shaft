"""The thinking budget forces </think> — runs only where MLX is installed (.venv-bonsai)."""
import unittest

try:
    import mlx.core as mx
except ImportError:          # the supervisor's own test run uses the system Python, without MLX
    mx = None


@unittest.skipIf(mx is None, "needs MLX (.venv-bonsai/bin/python -m unittest supervisor.tests.test_bonsai_budget)")
class ThinkingBudgetTest(unittest.TestCase):
    def setUp(self):
        from supervisor.bonsai_server import THINK_END, THINK_START, ThinkingBudget
        self.B, self.START, self.END = ThinkingBudget, THINK_START, THINK_END
        self.logits = mx.zeros((1, 248100))

    def run_tokens(self, prompt, generated, budget):
        b = self.B(budget)
        seq = list(prompt)
        out = b(mx.array(seq), self.logits)
        for t in generated:
            seq.append(t)
            out = b(mx.array(seq), self.logits)
        return out

    def test_forces_end_of_thinking_at_the_budget(self):
        out = self.run_tokens([1, 2, self.START], [5] * 10, budget=10)
        self.assertEqual(int(mx.argmax(out, axis=-1).item()), self.END)
        self.assertTrue(bool(mx.isinf(out[0, 5]).item()))

    def test_leaves_short_thinking_alone(self):
        out = self.run_tokens([1, 2, self.START], [5] * 9, budget=10)
        self.assertEqual(float(mx.max(out).item()), 0.0)
        self.assertFalse(bool(mx.any(mx.isinf(out)).item()))

    def test_no_effect_when_thinking_is_off_or_already_closed(self):
        off = self.run_tokens([1, self.START, 3, self.END, 4], [5] * 50, budget=10)
        self.assertFalse(bool(mx.any(mx.isinf(off)).item()))
        closed = self.run_tokens([1, 2, self.START], [5, 5, self.END] + [6] * 50, budget=10)
        self.assertFalse(bool(mx.any(mx.isinf(closed)).item()))


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(mx is None, "needs MLX (.venv-bonsai)")
class WrapUpTest(unittest.TestCase):
    def test_wrap_up_phrase_then_end_of_thinking(self):
        # Regression: a bare </think> mid-thought made Bonsai go on thinking in its answer (4,435 chars of
        # it stored as shaft's reply). Now the early-stopping phrase is forced first, then </think>.
        from supervisor.bonsai_server import THINK_END, THINK_START, ThinkingBudget
        b, logits = ThinkingBudget(3, wrap_up=[7, 8]), mx.zeros((1, 248100))
        seq = [1, THINK_START]
        b(mx.array(seq), logits)
        forced = []
        for _ in range(8):
            out = b(mx.array(seq), logits)
            tok = int(mx.argmax(out, axis=-1).item()) if bool(mx.isinf(out).any().item()) else 5
            forced.append(tok)
            seq.append(tok)
            if tok == THINK_END:
                break
        self.assertEqual(forced[-3:], [7, 8, THINK_END])
        after = b(mx.array(seq + [9]), logits)
        self.assertFalse(bool(mx.isinf(after).any().item()))   # the answer is free again


class RequestBudgetTest(unittest.TestCase):   # plain Python: no MLX needed
    def test_a_request_may_ask_for_less_never_more(self):
        from types import SimpleNamespace as NS
        from supervisor.bonsai_server import request_budget
        ask = lambda kw: NS(chat_template_kwargs=kw)   # noqa: E731
        self.assertEqual(request_budget(ask({"thinking_budget": 1024}), 4096), 1024)
        self.assertEqual(request_budget(ask({"thinking_budget": 99999}), 4096), 4096)
        self.assertEqual(request_budget(ask(None), 4096), 4096)
        self.assertEqual(request_budget(ask({"thinking_budget": "1"}), 4096), 4096)
        self.assertEqual(request_budget(ask({"thinking_budget": 1024}), 0), 0)


@unittest.skipIf(mx is None, "needs the pack's tokenizer (.venv-bonsai)")
class TemplateReasoningTest(unittest.TestCase):
    def test_bonsai_template_shows_this_replys_earlier_thinking_only(self):
        from mlx_lm.utils import load_tokenizer
        from supervisor import bonsai_lora
        tok = load_tokenizer(bonsai_lora.PACK)
        call = {"id": "1", "type": "function", "function": {"name": "list_files", "arguments": {}}}
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "old answer", "reasoning_content": "OLD-THOUGHT"},
                {"role": "user", "content": "build something"},
                {"role": "assistant", "content": "Let me look.", "reasoning_content": "PLAN-STEP-1",
                 "tool_calls": [call]},
                {"role": "tool", "tool_call_id": "1", "content": "plugins/"}]
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, preserve_thinking=False)
        self.assertIn("PLAN-STEP-1", text)        # this reply's earlier step: kept
        self.assertNotIn("OLD-THOUGHT", text)     # a finished turn's thinking: dropped


@unittest.skipIf(mx is None, "needs MLX (.venv-bonsai)")
class BudgetWithoutSyncTest(unittest.TestCase):
    def test_thinking_closed_early_is_never_forced(self):
        from supervisor.bonsai_server import THINK_END, THINK_START, ThinkingBudget
        b, logits = ThinkingBudget(3, wrap_up=[7, 8]), mx.zeros((1, 248100))
        seq = [1, THINK_START]
        b(mx.array(seq), logits)
        for tok in (5, THINK_END, 9, 9, 9, 9, 9):   # closes after 2 tokens, then answers well past the budget
            seq.append(tok)
            out = b(mx.array(seq), logits)
            self.assertFalse(bool(mx.isinf(out).any().item()), f"forced after the model closed thinking ({seq})")


@unittest.skipIf(mx is None, "needs MLX and the pack (.venv-bonsai); loads the 8 GB model")
class Float16NormsTest(unittest.TestCase):
    def test_served_model_has_no_float32_except_what_must_stay(self):
        # Regression (2026-10-13): 353 float32 norm weights promoted the whole forward pass and the KV cache to
        # float32 — decode fell from 20 to 17 tok/s by 16k context and prompt reading stayed ~250 tok/s.
        from mlx.utils import tree_flatten
        from supervisor import bonsai_lora
        model = bonsai_lora.load_text_model(float16_norms=True)
        f32 = [k for k, v in tree_flatten(model.parameters()) if v.dtype == mx.float32]
        self.assertTrue(f32)   # A_log / dt_bias / signs stay float32 on purpose
        self.assertEqual([k for k in f32 if not k.endswith(("A_log", "dt_bias", ".signs"))], [])
        h = model(mx.array([[1, 2, 3]]))
        self.assertEqual(h.dtype, mx.float16)


class ToolProgressTest(unittest.TestCase):   # plain Python: a stand-in for mlx_lm's server module
    def test_tool_calls_are_announced_while_written_at_most_once_a_second(self):
        import io as _io
        from types import SimpleNamespace as NS
        from unittest import mock
        from supervisor import bonsai_server

        class Base:
            def handle_completion(self, request, stop_words):
                ctx, response = self.response_generator.generate(request)
                return list(response)

        handler = bonsai_server.tool_progress_handler(NS(APIHandler=Base)).__new__(
            bonsai_server.tool_progress_handler(NS(APIHandler=Base)))
        gens = [NS(state="reasoning", text="plan"), NS(state="tool", text="<tool_call>\n<function=write_file>\n"),
                NS(state="tool", text="<parameter=path>notes/a.md"), NS(state="tool", text="x" * 500),
                NS(state="normal", text="")]
        handler.response_generator = NS(generate=lambda req: ("ctx", iter(gens)), cli_args=None)
        handler.stream, handler.wfile = True, _io.BytesIO()
        clock = iter([100.0, 100.4, 101.6])
        with mock.patch.object(bonsai_server.time, "monotonic", lambda: next(clock)):
            out = handler.handle_completion("req", [])
        self.assertEqual(len(out), 5)   # every generated piece still reaches mlx_lm's own loop
        lines = handler.wfile.getvalue().decode().split("\n\n")
        self.assertEqual(lines[0], ": tool write_file 34")
        self.assertTrue(lines[1].startswith(": tool write_file "))
        self.assertEqual(len([l for l in lines if l]), 2)   # 100.0 and 101.6; not 100.4
        self.assertIsNot(handler.response_generator.generate, None)

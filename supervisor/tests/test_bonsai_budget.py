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


@unittest.skipIf(mx is None, "needs MLX (.venv-bonsai)")
class PromptCacheCapTest(unittest.TestCase):
    def test_cap_holds_right_after_a_reply_is_cached(self):
        # Regression: mlx_lm trimmed only when the next request started; the cache sat at 6.0 GB against 4 GB.
        from supervisor.bonsai_server import capped_prompt_cache

        class Layer:
            def __init__(self, n):
                self.nbytes = n

            def is_trimmable(self):
                return False

        cache = capped_prompt_cache(100)(10)          # how mlx_lm's server builds it: size only
        for i in range(5):
            cache.insert_cache("m", [i, i + 1, i + 2], [Layer(40)])
        self.assertLessEqual(cache.nbytes, 100)
        self.assertGreater(cache.nbytes, 0)


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

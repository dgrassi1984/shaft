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

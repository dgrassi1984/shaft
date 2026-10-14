"""Decoding keeps the number of live Metal buffers flat — runs only where MLX is installed (.venv-bonsai).

Regression (2026-10-14 01:48): after a fresh prompt, mlx_lm's Gated DeltaNet caches (ArraysCache) carry a
`left_padding` array that every layer decrements, lazily, on every decoded token; nothing evaluated it while decoding,
so each token added scalar Metal buffers (about one per DeltaNet layer) to an ever-longer graph. The production server hit
Metal's buffer-count limit, `[metal::malloc] Resource limit (499000) exceeded`, and its generation thread died."""
import ctypes
import gc
import importlib
import unittest
from pathlib import Path
from unittest import mock

try:
    import mlx.core as mx
except ImportError:          # the supervisor's own test run uses the system Python, without MLX
    mx = None


def live_buffers() -> int:
    """MLX's own count of Metal buffers (MetalAllocator::num_resources_, the number checked against Metal's
    limit), read through ctypes: it sits right before resource_limit_ (mlx/backend/metal/allocator.h)."""
    lib = ctypes.CDLL(str(Path(mx.__file__).parent / "lib/libmlx.dylib"))
    alloc = lib._ZN3mlx4core9allocator9allocatorEv
    alloc.restype = ctypes.c_void_p
    words = (ctypes.c_size_t * 64).from_address(alloc())
    at = [i for i in range(64) if words[i] == mx.device_info()["resource_limit"]]
    assert len(at) == 1, "MetalAllocator layout changed: cannot find resource_limit_"
    gc.collect()       # arrays held by per-step garbage cycles otherwise go at random moments: +-200 of noise
    mx.synchronize()
    mx.clear_cache()   # count live buffers only
    return words[at[0] - 1]


def tiny_qwen35():
    """Bonsai's architecture (mlx_lm qwen3_5, 3 DeltaNet layers to 1 attention layer), random and tiny."""
    from mlx_lm.models.qwen3_5 import TextModel, TextModelArgs
    mx.random.seed(0)
    model = TextModel(TextModelArgs(
        model_type="qwen3_5_text", hidden_size=64, intermediate_size=128, num_hidden_layers=8,
        num_attention_heads=4, num_key_value_heads=2, head_dim=16, vocab_size=256, linear_num_value_heads=4,
        linear_num_key_heads=2, linear_key_head_dim=16, linear_value_head_dim=16, full_attention_interval=4))
    mx.eval(model.parameters())
    return model


@unittest.skipIf(mx is None, "needs MLX (.venv-bonsai/bin/python -m unittest supervisor.tests.test_bonsai_buffers)")
class DecodeBuffersTest(unittest.TestCase):
    STEPS = 300

    def growth(self, patched: bool) -> int:
        """Live buffers added between decode step 20 and step 20 + STEPS of one fresh request."""
        G = importlib.import_module("mlx_lm.generate")   # `import mlx_lm.generate as G` gives the function
        from supervisor import bonsai_server
        with mock.patch.object(G.GenerationBatch, "_step", G.GenerationBatch._step):
            if patched:
                bonsai_server.bound_cache_graphs(G)
            gen = G.BatchGenerator(tiny_qwen35(), max_tokens=10_000)
            gen.insert([list(range(1, 40))], max_tokens=[10_000])   # fresh prompt: empty caches, as in production
            for _ in range(20):
                gen.next()
            before = live_buffers()
            for _ in range(self.STEPS):
                gen.next()
            after = live_buffers()
            gen.close()
        return after - before

    def test_decoding_does_not_accumulate_buffers(self):
        self.assertEqual(self.growth(patched=True), 0)

    def test_control_unpatched_mlx_lm_leaks_buffers_on_every_token(self):
        # The bug this guards against, measured on mlx_lm as installed. If this starts failing, mlx_lm has fixed it
        # upstream and bound_cache_graphs can go. Measured: exactly 1,500 for 300 tokens (5 per token with these 6
        # DeltaNet layers, every run); ~45 per token on Bonsai's 48.
        self.assertGreaterEqual(self.growth(patched=False), 3 * self.STEPS)


if __name__ == "__main__":
    unittest.main()

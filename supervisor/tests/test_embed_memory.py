"""The embedding service's memory stays bounded — runs only where MLX and the model are installed (.venv-bonsai).

Bug (2026-10-14): MLX's buffer cache had no cap, so every new input length left freed buffers behind and the
service grew to 30 GB in 20 h; with two model servers it ran the 64 GB Mac out of memory (watchdog panic)."""
import random
import unittest

try:
    import mlx.core as mx
except ImportError:          # the supervisor's own test run uses the system Python, without MLX
    mx = None

from supervisor.embed_server import CACHE_BYTES, MODEL_DIR


@unittest.skipIf(mx is None or not (MODEL_DIR / "model.safetensors").exists(),
                 "needs MLX and the model (.venv-bonsai/bin/python -m unittest supervisor.tests.test_embed_memory)")
class EmbedCacheBoundTest(unittest.TestCase):
    def test_cache_stays_under_the_cap_across_many_input_lengths(self):
        from supervisor.embed_server import Embedder
        e = Embedder()
        rng = random.Random(0)
        words = "memoria journal agent plugin tool nota scheduler model cache".split()
        # MLX trims its cache when a buffer is returned, so it can sit one buffer over the cap: the largest one
        # here is an MLP activation (1024 tokens x 3072 x float32 = 12.6 MB). Uncapped it grew by gigabytes.
        one_buffer = 1024 * 3072 * 4
        for _ in range(40):
            e.embed([" ".join(rng.choice(words) for _ in range(rng.randint(5, 700))) for _ in range(4)], query=False)
            self.assertLessEqual(mx.get_cache_memory(), CACHE_BYTES + one_buffer)
        # Uncapped, this same workload left ~3 GB in the cache: the work did allocate past the cap
        self.assertGreater(mx.get_peak_memory(), mx.get_active_memory() + CACHE_BYTES // 2)


if __name__ == "__main__":
    unittest.main()

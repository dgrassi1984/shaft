"""The template's model client, against the wire format of SHAFT's own model server (mlx_lm)."""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft.llm import Client, Reply  # noqa: E402


class StreamTest(unittest.TestCase):
    def test_prompt_reading_progress_from_mlx_keepalives(self):
        # Regression: mlx_lm reports prompt reading only as SSE comments (": keepalive N/M"); the client
        # skipped every non-"data:" line, so `shaft talk` sat on "waiting for the model" for ~30 s.
        wire = (b": keepalive 2048/8850\n\n: keepalive 8850/8850\n\n"
                b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        seen, reply = [], Reply(content="", max_tokens=10)
        Client._read_stream(io.BytesIO(wire), reply, None, None, lambda d, t: seen.append((d, t)))
        self.assertEqual(seen, [(2048, 8850), (8850, 8850)])
        self.assertEqual(reply.content, "hi")


if __name__ == "__main__":
    unittest.main()

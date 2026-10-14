"""SHAFT's local embedding service: Qwen3-Embedding-0.6B (MLX) behind an OpenAI-style /v1/embeddings.

Instances' memory search combines its word index (BM25) with these vectors, so a memory written in other words
(or in the other language: the journals mix Italian and English) is still found.

Run with the Bonsai venv:  .venv-bonsai/bin/python -m supervisor.embed_server [--port 8782]
Normally started by launchd (shaft embed on). POST /v1/embeddings {"input": [texts], "input_type": "query"|
"document"} -> {"data": [{"index": i, "embedding": [1024 floats]}], "model": ...}. Queries get the model's
instruction prefix; documents do not (the model card's recipe). Vectors are L2-normalised: cosine = dot.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
MODEL_DIR = PROJECT / ".models" / "qwen3-embedding-0.6b"
PORT = 8782
TASK = "Given a question or topic, retrieve memories (journal entries, facts, notes, conversations) about it"
MAX_TOKENS = 1024          # a memory chunk is ~900 characters; longer inputs are cut, not refused
MAX_INPUTS = 64
# MLX keeps freed buffers for reuse, up to its default limit (tens of GB here). Every input length frees
# differently sized buffers, so the cache only grew: 30 GB after 20 h on 2026-10-14, one of three processes
# whose memory ran the 64 GB Mac into a watchdog panic. A forward pass needs < 2 GB including the 1.2 GB model.
CACHE_BYTES = 512 * 1024 ** 2


class Embedder:
    def __init__(self, model_dir: Path = MODEL_DIR):
        import mlx.core as mx
        from mlx_lm.models import qwen3
        from tokenizers import Tokenizer
        self.mx = mx
        mx.set_cache_limit(CACHE_BYTES)
        # The checkpoint is the bare transformer (sentence-transformers layout: no lm_head, no "model." prefix)
        args = qwen3.ModelArgs.from_dict(json.loads((model_dir / "config.json").read_text()))
        self.model = qwen3.Qwen3Model(args)
        self.model.load_weights(list(mx.load(str(model_dir / "model.safetensors")).items()), strict=True)
        self.model.eval()
        mx.eval(self.model.parameters())
        self.tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.eos = self.tok.token_to_id("<|endoftext|>")
        self.lock = threading.Lock()   # one forward pass at a time: MLX state is not shared across threads

    def _ids(self, text: str) -> list[int]:
        ids = self.tok.encode(text).ids[:MAX_TOKENS - 1]
        return ids if ids and ids[-1] == self.eos else ids + [self.eos]   # last-token pooling reads the EOS

    def embed(self, texts: list[str], query: bool) -> list[list[float]]:
        mx = self.mx
        out = []
        with self.lock:
            for text in texts:
                if query:
                    text = f"Instruct: {TASK}\nQuery:{text}"
                h = self.model(mx.array([self._ids(text)]))[0, -1]
                v = h.astype(mx.float32)
                v = v / mx.maximum(mx.linalg.norm(v), 1e-12)
                out.append(v.tolist())
        return out


def make_handler(embedder: Embedder):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.rstrip("/") == "/health":
                return self._reply(200, {"ok": True, "model": MODEL_DIR.name})
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path.rstrip("/") != "/v1/embeddings":
                return self._reply(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                texts = body.get("input")
                texts = [texts] if isinstance(texts, str) else texts
                if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
                    raise ValueError("input must be a string or a non-empty list of strings")
                if len(texts) > MAX_INPUTS:
                    raise ValueError(f"at most {MAX_INPUTS} inputs per request")
                vectors = embedder.embed(texts, query=body.get("input_type") == "query")
            except ValueError as e:
                return self._reply(400, {"error": str(e)})
            self._reply(200, {"object": "list", "model": MODEL_DIR.name,
                              "data": [{"object": "embedding", "index": i, "embedding": v}
                                       for i, v in enumerate(vectors)]})

    return Handler


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="embed_server")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args(argv)
    embedder = Embedder()
    from .version import record_service
    record_service("embed", {"model": MODEL_DIR.name, "port": args.port})
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(embedder))
    print(f"embedding service on 127.0.0.1:{args.port} ({MODEL_DIR.name})", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

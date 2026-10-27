"""Memory beyond the context window (the operator, 2026-10-13: the review's two criticisms, and "also embeddings").

- the cycle's improvement plan comes from the last sleep (reflections are not scheduled) and is always in view;
- recall is automatic: what memory holds about a message is added without the model thinking to search;
- the sleep consolidates facts (dropped ones archived, still searchable) and writes day digests;
- search fuses words (BM25) with meaning (embeddings), so other words or the other language still find it.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

TEMPLATE = Path(__file__).resolve().parents[2] / "template"
sys.path.insert(0, str(TEMPLATE))
from shaft import memory, prompts  # noqa: E402
from shaft.state import State  # noqa: E402
from shaft.tools import Toolbox  # noqa: E402

CONCEPTS = [{"weather", "forecast", "meteo", "previsioni", "rain", "pioggia"},
            {"work", "job", "lavoro", "example", "employer", "lavora"},
            {"sleep", "dormito", "consolidated", "sonno"}]


def concept_vector(text: str) -> list[float]:
    words = set(text.lower().replace("?", " ").replace(".", " ").split())
    v = [float(len(words & c)) for c in CONCEPTS] + [0.1]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


class FakeEmbedder:
    def __init__(self):
        self.calls = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append(body)
                out = json.dumps({"data": [{"index": i, "embedding": concept_vector(t)}
                                           for i, t in enumerate(body["input"])]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/embeddings"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def instance(entries: list[str], facts: list[str] = ()) -> Path:
    root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
    (root / "memory").mkdir()
    (root / "memory" / "journal.md").write_text("".join(f"\n---\n{e}\n" for e in entries))
    (root / "memory" / "facts.md").write_text("# Facts I have learned\n\n" + "".join(f"{f}\n" for f in facts))
    return root


class ImprovementPlanTest(unittest.TestCase):
    def test_the_plan_comes_from_the_last_sleep_and_the_cycle_is_pointed_to_it(self):
        root = instance(["## 2026-10-12 16:53 — Reflection\n\n## How I will improve\n\nold reflection plan",
                         "## 2026-10-13 05:05 — Sleep\n\nI consolidated.\n\n**How I will improve**\nTest: a confidence "
                         "line in every entry.\n\n**Next cycle.** Rewrite self.md.\n\n*Consolidation actions:* remember"])
        plan = prompts.improvement_plan(State(root))
        self.assertIn("From my sleep of 2026-10-13 05:05", plan)
        self.assertIn("a confidence line in every entry", plan)
        self.assertIn("Rewrite self.md", plan)                  # the next concrete step comes with it
        self.assertNotIn("Consolidation actions", plan)
        self.assertNotIn("old reflection plan", plan)
        self.assertIn("current improvement plan", prompts.LIVE)
        self.assertNotIn("last reflection", prompts.LIVE)      # reflections are not scheduled: never point there

    def test_none_yet(self):
        self.assertIn("my first sleep will set one", prompts.improvement_plan(State(instance(["## 2026-10-13 10:00 — A\n\nx"]))))


class RecallTest(unittest.TestCase):
    def test_recall_adds_what_the_context_does_not_show(self):
        root = instance(["## 2026-10-10 09:00 — Conversation with the operator\n\nthe operator works at Example Company on sample "
                         "projects.", "## 2026-10-13 10:00 — Cycle\n\nI read the weather."])
        shown = "## 2026-10-13 10:00 — Cycle\n\nI read the weather."
        with mock.patch.dict(os.environ, {"SHAFT_EMBED_URL": ""}):
            block = memory.recall(root, "where does the operator work, Example Company?", shown=shown)
        self.assertIn("Recalled automatically", block)
        self.assertIn("Example Company", block)
        self.assertNotIn("I read the weather", block)          # already in view: not repeated

    def test_a_conversation_message_reaches_the_model_with_its_recall(self):
        from supervisor.tests.fakellm import FakeLLM
        fake = FakeLLM([{"content": "ok"}])
        self.addCleanup(fake.close)
        root = instance(["## 2026-10-01 09:00 — Conversation with the operator\n\nthe operator's son is called Jordan."])
        (root / "config.env").write_text(f'LLM_BASE_URL="{fake.url}"\nLLM_MODEL="m"\nCONTEXT_JOURNAL_CHARS=10\n')
        subprocess.run([sys.executable, "-E", "-s", "-m", "shaft", "talk", "--json"], cwd=TEMPLATE, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "SHAFT_ROOT": str(root)}, capture_output=True, text=True,
                       input=json.dumps({"type": "user", "text": "what is my son called? Jordan?"}) + "\n")
        user = [m for m in fake.requests[0]["messages"] if m["role"] == "user"][-1]["content"]
        self.assertIn("Jordan", user.split("Recalled automatically")[-1])
        episode = (root / "memory" / "episodes").glob("*.jsonl")
        first = json.loads(next(episode).read_text().splitlines()[0])
        self.assertEqual(first["content"], "what is my son called? Jordan?")   # the record keeps what was said


class SleepConsolidationTest(unittest.TestCase):
    def setUp(self):
        self.root = instance([], ["- [2026-10-12 17:00] A fact", "- [2026-10-12 18:00] A duplicate fact",
                                  "- [2026-10-12 19:00] Superseded"])
        self.box = Toolbox(self.root)

    def run_tool(self, name, **args):
        return self.box.run(name, json.dumps(args))

    def test_rewrite_facts_archives_what_it_drops_and_keeps_it_searchable(self):
        out = self.run_tool("rewrite_facts", facts="- [2026-10-12 17:00] A fact\n- [2026-10-13 05:00] Merged fact")
        self.assertIn("3 -> 2", out)
        facts = (self.root / "memory" / "facts.md").read_text()
        self.assertIn("Merged fact", facts)
        self.assertNotIn("Superseded", facts)
        self.assertIn("Superseded", (self.root / "memory" / "facts-archive.md").read_text())
        with mock.patch.dict(os.environ, {"SHAFT_EMBED_URL": ""}):
            source = memory.search(self.root, "superseded")[0].source   # with its src and age (PLAN-consciousness C3)
        self.assertTrue(source.startswith("memory/facts-archive.md, src unknown, "), source)

    def test_bad_facts_and_direct_writes_are_refused(self):
        self.assertIn("ERROR", self.run_tool("rewrite_facts", facts="just prose without a date"))
        self.assertIn("ERROR", self.run_tool("rewrite_facts", facts="- [2026-10-12 17:00] " + "x" * 7000))
        self.assertIn("rewrite_facts", self.run_tool("write_file", path="memory/facts.md", content="- gone"))
        self.assertIn("Superseded", (self.root / "memory" / "facts.md").read_text())

    def test_digests_are_kept_shown_and_searchable(self):
        self.assertEqual(self.run_tool("write_digest", text="Built the weather plugin; slept well."), "digest written")
        cfg = mock.Mock(NAME="shaft", OPERATOR_NAME="the operator", CONTEXT_JOURNAL_CHARS=1000)
        self.assertIn("Built the weather plugin", prompts.system_prompt(State(self.root), cfg, "m"))
        with mock.patch.dict(os.environ, {"SHAFT_EMBED_URL": ""}):
            self.assertEqual(memory.search(self.root, "weather plugin")[0].source, "memory/digests.md")
        self.assertIn("rewrite_facts", prompts.SLEEP)
        self.assertIn("write_digest", prompts.SLEEP)


class SemanticSearchTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEmbedder()
        self.addCleanup(self.fake.close)
        self.root = instance(["## 2026-10-12 17:00 — Cycle\n\nI built a plugin that reads tomorrow's rain forecast.",
                              "## 2026-10-12 18:00 — Conversation with the operator\n\nthe operator's employer is Example Company.",
                              "## 2026-10-12 19:00 — Cycle\n\nI fetched three pages about scheduling."])

    def search(self, q):
        with mock.patch.dict(os.environ, {"SHAFT_EMBED_URL": self.fake.url}):
            return memory.search(self.root, q, k=2)

    def test_other_words_and_the_other_language_are_found(self):
        hits = self.search("che previsioni meteo ho letto?")         # no word in common with the entry
        self.assertIn("rain forecast", hits[0].text)
        self.assertEqual(memory.semantic_error, "")
        hits = self.search("dove lavora?")
        self.assertIn("Example Company", hits[0].text)

    def test_vectors_are_cached_and_only_new_chunks_are_embedded(self):
        self.search("weather")
        documents = [c for c in self.fake.calls if c["input_type"] == "document"]
        self.assertEqual(sum(len(c["input"]) for c in documents), 3)
        self.search("job")
        self.assertEqual(sum(len(c["input"]) for c in self.fake.calls if c["input_type"] == "document"), 3)
        self.assertTrue((self.root / ".tmp" / "memory-index.json").exists())

    def test_with_the_service_it_says_so_too(self):
        # Regression: the label appeared only when meaning search failed, so shaft used it daily and still wrote
        # "No embeddings, no vector DB. Just text." in its self-model (2026-10-13).
        with mock.patch.dict(os.environ, {"SHAFT_EMBED_URL": self.fake.url}):
            out = Toolbox(self.root).run("search_memory", json.dumps({"query": "dove lavora?"}))
        self.assertTrue(out.startswith("(searched by words + meaning)"), out[:200])
        self.assertIn("Example Company", out)

    def test_nothing_searched_claims_nothing(self):
        with mock.patch.dict(os.environ, {"SHAFT_EMBED_URL": self.fake.url}):
            out = Toolbox(self.root).run("search_memory", json.dumps({"query": "   "}))
        self.assertEqual(out, "nothing relevant found")

    def test_without_the_service_it_says_words_only(self):
        with mock.patch.dict(os.environ, {"SHAFT_EMBED_URL": "http://127.0.0.1:9/v1/embeddings"}), \
             mock.patch("sys.stderr"):
            out = Toolbox(self.root).run("search_memory", json.dumps({"query": "Example Company"}))
        self.assertTrue(out.startswith("(searched by words only: semantic search is unavailable"), out[:200])
        self.assertIn("Example Company", out)


if __name__ == "__main__":
    unittest.main()

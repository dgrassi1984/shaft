"""#11: a quarter of the operator's corrections reopened one the instance had closed, and it asked him for things it
could have looked up. Before this change a closed correction was in no prompt and no search, a message to him went out
without any look at the ledger, and sleep closed a reopening correction like any other. Each test fails on the template
before that change; its docstring says what reverting breaks."""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from unittest import mock

from supervisor.tests.test_autonomy_gaps_template import InstanceFixture
from shaft import corrections, prompts, talk
from shaft.tools import Toolbox

if not hasattr(corrections, "found"):
    raise unittest.SkipTest("template/ predates #11 (corrections searched before asking): merge it into template first")

NOW = datetime(2026, 11, 20, 9, 0)
PRINTER = {"what_was_wrong": "asked the operator which printer queue to use for the office printer",
           "what_operator_said": "the printer queue is in notes/printer.md, look it up instead of asking me",
           "when": "2026-11-14T10:00:00"}
CITATIONS = {"what_was_wrong": "summary had no links to the documents it cited",
             "what_operator_said": "always include links in document citations",
             "when": "2026-11-15T11:00:00"}


class CorrectionsFirstTest(InstanceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        # words only: no embedding service in tests, so ranking is the lexical half of search_memory's
        self.enterContext(mock.patch("shaft.memory._embed", side_effect=OSError("no embedding service in tests")))

    def ledger(self):
        first = corrections.record(self.root, PRINTER, "api-1", NOW)
        corrections.record(self.root, CITATIONS, "api-2", NOW)
        printer_id = first.split()[1]
        self.assertIn("closed", corrections.close(self.root, {"id": printer_id, "closed_by": "fact",
                                                               "evidence": "fact 2026-11-14 printer queue"}, NOW))
        return printer_id

    def test_a_closed_correction_is_still_found_by_topic(self):
        """Reverting: search_corrections does not exist, and search_memory never reads memory/corrections.jsonl, so
        a closed correction is in no prompt and no search — the next mistake of its kind meets nothing."""
        printer_id = self.ledger()
        out = Toolbox(self.root).run("search_corrections", json.dumps({"query": "which printer queue should I use?"}))
        self.assertTrue(out.splitlines()[1].startswith(f"- {printer_id} "), out)
        self.assertIn("closed by fact: fact 2026-11-14 printer queue", out)
        memory_hits = Toolbox(self.root).run("search_memory", json.dumps({"query": "printer queue"}))
        self.assertIn(f"[correction {printer_id}]", memory_hits)

    def test_search_says_when_nothing_is_recorded(self):
        """The instance must say what it found or that it found nothing: an empty ledger answers so, not with ''."""
        out = Toolbox(self.root).run("search_corrections", json.dumps({"query": "printer"}))
        self.assertEqual(out, corrections.NONE_RECORDED)

    def test_a_message_to_the_operator_is_held_until_the_corrections_were_searched(self):
        """Reverting: send_message to the operator goes straight to the broker with no look at the ledger — the
        question the operator already answered is asked again. The hold shows the closest corrections once; sending
        again (or searching first) sends; messages to other instances are never held."""
        printer_id = self.ledger()
        sent = []
        self.fake_broker(lambda name, **args: sent.append(args) or "delivered")
        box = Toolbox(self.root)
        question = json.dumps({"to": "operator", "text": "Which printer queue should I use?", "kind": "question"})
        held = box.run("send_message", question)
        self.assertTrue(held.startswith("(held, not run yet)"), held)
        self.assertIn(printer_id, held)
        self.assertEqual(sent, [])
        # a second send in the same reply was written before the hold was read: held too
        self.assertTrue(box.run("send_message", json.dumps({"to":"operator","text":"Other?"})).startswith("(held"))
        box.turn += 1
        self.assertEqual(box.run("send_message", question), "delivered")   # read in a later reply: the retry sends
        self.assertEqual(len(sent), 1)
        # the next message to him needs a new look; a search first lets it through at once
        self.assertTrue(box.run("send_message", json.dumps({"to": "operator", "text": "Printer done."}))
                        .startswith("(held, not run yet)"))
        box.run("search_corrections", json.dumps({"query": "printer report"}))
        self.assertEqual(box.run("send_message", json.dumps({"to": "operator", "text": "Printer report."})),
                         "delivered")
        self.assertEqual(box.run("send_message", json.dumps({"to": "example", "text": "printer?"})), "delivered")

    def test_another_roots_corrections_never_leak_into_search(self):
        """Codex review: the corpus cache kept one root's correction documents for a root with no ledger."""
        import tempfile
        from pathlib import Path
        from shaft import memory
        self.ledger()
        self.assertTrue(any(src.startswith("correction ") for src, _ in memory.corpus(self.root)))
        with tempfile.TemporaryDirectory(prefix="shaft-other-") as other:
            (Path(other) / "memory").mkdir()
            self.assertFalse(any(src.startswith("correction ") for src, _ in memory.corpus(Path(other))))

    def test_no_hold_without_corrections(self):
        """A hold with nothing to show would only cost a step."""
        self.fake_broker(lambda name, **args: "delivered")
        self.assertEqual(Toolbox(self.root).run("send_message", json.dumps({"to": "operator", "text": "Hi?"})),
                         "delivered")

    def test_each_operator_message_carries_the_closest_corrections(self):
        """Reverting: an operator message reaches the model with automatic recall only, which never held the
        corrections — the search would run only when the model thought of it."""
        printer_id = self.ledger()
        added = corrections.closest(self.root, "Can you print the report on the office printer?")
        self.assertIn(printer_id, added)
        self.assertIn("say what you found", added)
        self.assertIn("corrections.closest(state.root, user)", open(talk.__file__).read())
        self.assertIn("corrections.closest(state.root, asked)", open(talk.__file__).read())

    def test_prompt_requires_the_search_before_asking_and_dated_state(self):
        """The rule the operator asked for, pinned in the prompt that offers search_corrections."""
        self.assertIn("search_corrections" + " on its topic", prompts.CORRECTIONS_FIRST)
        self.assertIn("last read", prompts.CORRECTIONS_FIRST)
        system = prompts.system_prompt(self.state, self.cfg, "", tools="read_file, search_corrections, send_message")
        self.assertIn(prompts.CORRECTIONS_FIRST.strip(), system)

    def test_a_reopening_correction_is_shown_with_the_earlier_one_and_closes_only_with_what_failed(self):
        """Reverting: sleep sees the new correction alone and closes it like any other, so the earlier fix that did
        not hold is never looked at (#11: 6 of 23 corrections in a week reopened an earlier one)."""
        printer_id = self.ledger()
        again = corrections.record(self.root, {**PRINTER, "when": "2026-11-17T09:00:00", "reopens": printer_id,
                                               "what_was_wrong": "asked again for the printer queue"}, "api-3", NOW)
        new_id = again.split()[1]
        block = corrections.block(self.root, NOW)
        self.assertIn(f"  - reopens {printer_id} (", block)
        self.assertIn("closed by fact: fact 2026-11-14 printer queue", block)
        refused = corrections.close(self.root, {"id": new_id, "closed_by": "fact", "evidence": "fact again"}, NOW)
        self.assertTrue(refused.startswith("ERROR: not closed"), refused)
        self.assertEqual(corrections.fold(self.root)[new_id]["status"], "open")
        corrections.close(self.root, {"id": new_id, "closed_by": "code", "evidence": "commit abc",
                                      "what_failed": "a fact nobody read before asking; now a plugin checks"}, NOW)
        closed = corrections.fold(self.root)[new_id]
        self.assertEqual((closed["status"], closed["what_failed"]),
                         ("closed", "a fact nobody read before asking; now a plugin checks"))
        self.assertEqual(corrections.fold(self.root)[printer_id]["reopened_by"], [new_id])
        self.assertIn("what_failed", prompts.SLEEP)


if __name__ == "__main__":
    unittest.main()

"""Review I7: forced curiosity follows the world, not the instance's machinery."""
import unittest

from supervisor import surprise
from supervisor.tests.test_surprise import Fixture, DECLARE, FINISH
from shaft import prompts, surprises


def item(sid, expected, observed="", so_what=""):
    return {"id": sid, "expected": expected, "observed": observed, "so_what": so_what}


INWARD = ("my tools work", "our scheduled jobs ran", "the plugin returned null", "my code passes",
          "the gate rejected this change", "memory/commitments.jsonl is empty", "shaft/live.py changed",
          "plugins/weather/main.py crashed", "jobs/weather.json is missing", "my ledger has no entries",
          # Grok's review: builtins and own paths were missed
          "read_file returned an empty page", "search_memory found nothing on it", "`run` timed out",
          "run(python3 x.py) printed nothing", "self/self.md still says I run nightly", "memory/facts.md lacks it",
          "the test gate kept my change", "my weather plugin times out", "the sandbox refused the import")
OUTWARD = ("Rain arrived before the forecast", "The operator prefers early meetings",
           "https://example.org/weather changed", "The orchard has twelve rows",
           # Grok's review: bare English senses of ambiguous words were inward
           "the broker's margin rule changed overnight", "The WordPress plugin on the shop site broke",
           "the garden gate was open", "the bank ledger showed a different balance", "the job market cooled",
           "my prediction about the rain was wrong", "the operator replied to my message within an hour",
           "send_message to the operator got no reply", "notes/briefing.md says the meeting moved")


class OutwardTest(unittest.TestCase):
    def test_I7_shared_deterministic_classifier(self):
        for text in INWARD:
            for field in ("expected", "observed", "so_what"):
                row = item("in", "") | {field: text}
                with self.subTest(field=field, text=text):
                    self.assertTrue(surprises.inward(row))
                    self.assertTrue(surprise.inward(row))
        for text in OUTWARD:
            with self.subTest(text=text):
                self.assertFalse(surprises.inward(item("out", text)))
                self.assertFalse(surprise.inward(item("out", text)))
        self.assertEqual(surprises.INWARD.pattern, surprise.INWARD.pattern)

    def test_I7_the_goal_a_surprise_was_met_under_does_not_make_it_inward(self):
        """Grok's review: the goal field is not read — a tooling cycle can still be surprised by the world."""
        self.assertFalse(surprises.inward(item("out", "the API now needs a key") | {"goal": "Fix my weather plugin"}))

    def test_I7_filters_inward_and_weights_relevance_reproducibly(self):
        rows = [item("in", "my tools work", so_what="orchard survey rainfall forecast"),
                item("relevant", "orchard survey rainfall forecast"), item("other", "train timetable")]
        context = "orchard survey rainfall forecast"
        self.assertEqual(surprises.relevance(rows[1], context), 5)
        self.assertEqual(surprises.relevance(rows[2], context), 1)
        chosen = [surprises.choose(rows, context, str(i))["id"] for i in range(500)]
        self.assertNotIn("in", chosen)
        self.assertGreater(chosen.count("relevant"), 350)
        self.assertEqual(surprises.choose(rows, context, "replay"), surprises.choose(rows, context, "replay"))
        self.assertIsNone(surprises.choose(rows[:1], context, "replay"))
        self.assertIsNone(surprises.choose([], context, "replay"))

    def test_I7_closed_class_words_do_not_weight_against_a_briefing_sized_context(self):
        """Grok's review: 'that', 'with', 'from', 'have' counted, so a 6,000-character briefing weighted every
        surprise alike."""
        filler = ("This week there will be more that they have done with them, which would also have been from "
                  "those other people about whom we were told before, after and during their visits. ")
        context = (filler * 40)[:5800] + " orchard rainfall"
        self.assertGreater(len(context), 5000)
        chatty = item("chatty", "that they have done with them which would also have been from those",
                      "there will be more about whom we were, before after during")
        relevant = item("relevant", "orchard rainfall was low")
        self.assertEqual(surprises.relevance(chatty, context), 1)
        self.assertEqual(surprises.relevance(relevant, context), 3)
        chosen = [surprises.choose([chatty, relevant], context, str(i))["id"] for i in range(400)]
        self.assertGreater(chosen.count("relevant"), 250)


class OutwardCycleTest(Fixture, unittest.TestCase):
    def test_I7_no_outward_surprise_leaves_goal_free_and_says_so(self):
        self.seed(1, text="my tools work ")
        fake = self.live([{"tool_calls": [("declare_intent", DECLARE)]}, {"content": "ok"}], every=1)
        self.assertIn(prompts.NO_OUTWARD_SURPRISE.strip(), fake.requests[0]["messages"][1]["content"])
        self.assertNotIn(prompts.CURIOSITY.strip(), fake.requests[0]["messages"][1]["content"])
        self.assertEqual(self.state.last_episode()["curiosity"], {"drawn": False, "skipped_for": "no_outward_surprise"})
        self.assertEqual(self.state.last_episode()["intent"]["goal"], DECLARE["goal"])

    def test_I7_undrawn_cycle_says_nothing_about_outward_surprises(self):
        self.seed(1, text="my tools work ")
        fake = self.live([{"content": "ok"}], every=10 ** 9)
        self.assertNotIn(prompts.NO_OUTWARD_SURPRISE.strip(), fake.requests[0]["messages"][1]["content"])
        self.assertEqual(self.state.last_episode()["curiosity"], {"drawn": False, "skipped_for": None})

    def test_I7_selected_outward_surprise_is_enforced_before_acting(self):
        self.seed(1, text="my tools ")
        self.seed(1, ts="2026-10-12T10:00:00", text="orchard rainfall ")
        sid = "s1012-1000-1"
        fake = self.live([
            {"tool_calls": [("declare_intent", {**DECLARE, "origin_ref": "surprise:s1012-0900-1"})]},
            {"tool_calls": [("declare_intent", {**DECLARE, "goal": "Check orchard rainfall", "origin_ref": f"surprise:{sid}"})]},
            {"tool_calls": [("finish_cycle", FINISH)]}], every=1)
        self.assertEqual(self.state.last_episode()["curiosity"]["selected"], sid)
        self.assertEqual(self.state.last_episode()["intent"]["origin_ref"], f"surprise:{sid}")
        results = [m["content"] for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
        self.assertTrue(results[0].startswith(f"ERROR: this curiosity cycle must follow surprise:{sid}"), results)

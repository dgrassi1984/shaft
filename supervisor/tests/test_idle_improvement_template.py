"""#13 and #15 in the template: idle time goes to the instance's improvement list, and each sleep turns its "How I will
improve" into items with ids and ends with a self-model line. Each test fails on the template before them; its name or
docstring says which acceptance point it pins."""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
# No skip on a missing module: a template without the list must fail here, not pass by skipping. Merge the template
# branch with (or before) this file.
from shaft import clock, improvements, live, prompts, slate, sleep   # noqa: E402
from shaft.llm import Reply   # noqa: E402

from supervisor.tests.test_autonomy_gaps_template import InstanceFixture   # noqa: E402
from supervisor.tests.test_prompt_budget_template import NOW, Fixture   # noqa: E402

SLEPT = datetime(2026, 11, 25, 3, 0)
ITEM = {"action": "write a regression test for the brief's freshness brake", "kind": "test",
        "evidence": "judgment 2026-11-24 11:01: the brake was never called"}
COMPLETE = ("Self-model: unchanged — the three corrections and the verdicts since my last sleep did not "
            "contradict it")


def tool(name, args, cid="1"):
    return {"id": cid, "name": name, "arguments": json.dumps(args)}


class ImprovementListTest(Fixture, unittest.TestCase):
    def test_add_close_and_refusals_name_what_to_send(self):
        self.assertIn("kind must be one of", improvements.add(self.root, {**ITEM, "kind": "idea"}, NOW, "cycle"))
        self.assertIn("concretely", improvements.add(self.root, {**ITEM, "action": "be better"}, NOW, "cycle"))
        self.assertIn("evidence must", improvements.add(self.root, {**ITEM, "evidence": " "}, NOW, "cycle"))
        self.assertEqual(improvements.add(self.root, ITEM, NOW, "cycle"), "improvement i1103-1000-1 added")
        self.assertEqual(improvements.add(self.root, ITEM, NOW, "cycle"), "already on my list as i1103-1000-1 (open)")
        self.assertIn("is not an open item", improvements.close(self.root, {"id": "i0101-0000-1", "outcome": "done",
                                                                            "evidence": "x"}, NOW))
        self.assertIn("outcome must be", improvements.close(self.root, {"id": "i1103-1000-1", "outcome": "kept",
                                                                        "evidence": "x"}, NOW))
        self.assertEqual(improvements.close(self.root, {"id": "i1103-1000-1", "outcome": "done",
                                                        "evidence": "commit 96211fd4"}, NOW),
                         "closed improvement i1103-1000-1 (done)")
        self.assertEqual(improvements.open_items(self.root), [])
        (self.root / improvements.FILE).open("a").write("not json\n")
        self.assertEqual(improvements.fold(self.root)["i1103-1000-1"]["outcome"], "done")   # a bad line is skipped

    def test_at_most_two_added_per_run(self):
        """#15 acceptance: each sleep turns its block into at most two items."""
        run = improvements.Limited(self.root, lambda: NOW, "sleep")
        results = [run.plan({**ITEM, "action": f"{ITEM['action']} number {n}"}) for n in range(3)]
        self.assertEqual([r.split()[0] for r in results], ["improvement", "improvement", "ERROR:"])
        self.assertIn("at most 2 items per sleep", results[2])
        self.assertEqual(len(improvements.open_items(self.root)), 2)

    def test_idle_slate_offers_the_backlog_first(self):
        """#15 acceptance: an empty inbox, nothing due and a non-empty list — the slate leads with its items, ranked
        above the re-check of an unchanged matter (the last cycle's next action)."""
        self.episode(2, "Check the calendar again", finish={"next_action": {"action": "check the calendar again",
                                                                            "revise_if": "none"}})
        improvements.add(self.root, ITEM, NOW, "sleep")
        improvements.add(self.root, {**ITEM, "action": "build a plugin that lists stale queue rows", "kind": "tool"},
                         NOW.replace(minute=5), "cycle")
        text = slate.text(self.root, live.read_inbox(self.state), NOW)
        rows = [r for r in text.splitlines() if r.startswith("- [")]
        self.assertEqual([r.split(";")[0] for r in rows], ["- [improvement", "- [improvement", "- [next action"])
        self.assertIn("origin_ref improvement:i1103-1005-1] tool: build a plugin", rows[0])   # newest first
        self.assertIn("idle time", text)
        self.assertIn("sends the operator nothing unless something is due", text)

    def test_with_an_operator_message_the_inbox_leads_and_the_list_follows(self):
        (self.root / "inbox" / "20261103-090000-000000-from-operator.md").write_text(
            "From: operator\nTo: wisp\nKind: message\nDate: 2026-11-03 09:00\n\nPlease survey the orchard rows")
        improvements.add(self.root, ITEM, NOW, "sleep")
        text = slate.text(self.root, live.read_inbox(self.state), NOW)
        rows = [r for r in text.splitlines() if r.startswith("- [")]
        self.assertEqual([r.split(";")[0] for r in rows], ["- [inbox", "- [improvement"])
        self.assertNotIn("idle time", text)

    def test_idle_shows_at_most_three_items_and_a_failed_read_is_not_idle(self):
        for n in range(6):
            improvements.add(self.root, {**ITEM, "action": f"{ITEM['action']} number {n}"}, NOW.replace(minute=n), "cycle")
        rows = [r for r in slate.text(self.root, [], NOW).splitlines() if r.startswith("- [")]
        self.assertEqual(len(rows), slate.IDLE_IMPROVEMENTS)
        with mock.patch.object(slate.commitments, "overdue", side_effect=OSError("disk gone")):
            text = slate.text(self.root, [], NOW)
        self.assertNotIn("idle time", text)
        self.assertIn("(not read: commitment: OSError: disk gone)", text)

    def test_live_cycle_offers_the_list_tools_and_shows_the_open_items(self):
        improvements.add(self.root, ITEM, NOW, "sleep")
        request = self.live([{"content": "ok"}]).requests[0]
        names = [t["function"]["name"] for t in request["tools"]]
        self.assertIn("plan_improvement", names)
        self.assertIn("close_improvement", names)
        self.assertIn("i1103-1000-1 [test, added 2026-11-03T10:00 by sleep]", request["messages"][0]["content"])
        self.assertIn("origin_ref \\\nimprovement:<id>".replace("\\\n", ""), prompts.LIVE.replace("\\\n", ""))


class SleepEntryCheckTest(InstanceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(clock, "now", return_value=SLEPT))

    def problems(self, entry, log=()):
        return sleep.entry_problems(self.state, entry, list(log))

    def test_entry_problems_model_free(self):
        """#13 acceptance: no self-model line, a bare one, or an improvement block with no id is refused."""
        improvements.add(self.root, ITEM, SLEPT, "sleep")
        good = "## How I will improve\nPassed: yes. Item i1125-0300-1 carries it.\n\n" + COMPLETE
        self.assertEqual(self.problems(good), [])
        self.assertEqual(self.problems(good.replace(COMPLETE, "**Self-model:** unchanged — " + COMPLETE[26:])), [])
        self.assertIn("no self-model line", self.problems(good.replace(COMPLETE, "Self-model invariato."))[0])
        self.assertIn("gives no evidence", self.problems(good.replace(COMPLETE, "Self-model: unchanged."))[0])
        self.assertIn("no update_self_model succeeded",
                      self.problems(good.replace("unchanged —", "changed — line 6 resolved, because"))[0])
        updated = [{"tool": "update_self_model", "result": "self-model updated"}]
        self.assertIn("says unchanged, but update_self_model succeeded", self.problems(good, updated)[0])
        self.assertIn("names no item", self.problems(good.replace("i1125-0300-1", "the freshness test"))[0])
        self.assertIn("names no item", self.problems(good.replace("i1125-0300-1", "i1125-0300-9"))[0])   # unknown id
        improvements.close(self.root, {"id": "i1125-0300-1", "outcome": "done", "evidence": "commit abc"}, SLEPT)
        self.assertIn("names no item", self.problems(good)[0])   # a closed item is no next action

    def test_sleep_offers_the_list_tools_and_refuses_a_third_item(self):
        client = mock.Mock()
        client.complete.side_effect = [Reply("", tool_calls=[
            tool("plan_improvement", {**ITEM, "action": f"{ITEM['action']} part {n}"}, str(n)) for n in range(3)]),
            Reply("## How I will improve\nItems i1125-0300-1 and i1125-0300-2.\n\n" + COMPLETE)]
        sleep.run(self.cfg, self.state, client)
        offered = [t["function"]["name"] for t in client.complete.call_args_list[0].kwargs["tools"]]
        self.assertIn("plan_improvement", offered)
        self.assertIn("close_improvement", offered)
        self.assertIn("## My open improvement items (step 14)", client.complete.call_args_list[0].args[0][1]["content"])
        results = [e["result"] for e in self.episodes()[-1]["tools"]]
        self.assertEqual([r.split()[0] for r in results], ["improvement", "improvement", "ERROR:"])
        self.assertEqual(client.complete.call_count, 2)   # complete: not sent back
        self.assertNotIn("check", self.episodes()[-1])

    def test_an_incomplete_entry_is_sent_back_once_and_what_it_adds_is_kept(self):
        """#13 acceptance: a sleep with no self-model line and an improvement block naming no id is sent back once."""
        client, seen = mock.Mock(), []
        replies = iter([Reply("## How I will improve\nI will test the freshness brake.\n\n## Next\nTomorrow."),
                        Reply("", tool_calls=[tool("plan_improvement", ITEM)]),
                        Reply("That is item i1125-0300-1.\n" + COMPLETE)])
        client.complete.side_effect = lambda ms, *a, **k: (seen.append([dict(m) for m in ms]), next(replies))[1]
        sleep.run(self.cfg, self.state, client)
        self.assertEqual(len(seen), 3)
        sent_back = seen[1][-1]["content"]
        self.assertTrue(sent_back.startswith("[supervisor] Your sleep entry is not finished:"))
        self.assertIn("no self-model line", sent_back)
        self.assertIn("names no item", sent_back)
        ep = self.episodes()[-1]
        self.assertEqual(len(ep["check"]["sent_back"]), 2)
        self.assertEqual(ep["check"]["after"], [])
        journal = (self.root / "memory" / "journal.md").read_text()
        self.assertIn("I will test the freshness brake.", journal)
        self.assertIn("That is item i1125-0300-1.", journal)
        self.assertNotIn("*Sleep check:*", journal)
        self.assertIn("i1125-0300-1", prompts.improvement_plan(self.state))

    def test_a_second_miss_is_kept_with_a_note_not_sent_back_again(self):
        client = mock.Mock()
        client.complete.side_effect = [Reply("## How I will improve\nBetter tests."), Reply("Self-model: unchanged.")]
        sleep.run(self.cfg, self.state, client)
        self.assertEqual(client.complete.call_count, 2)
        journal = (self.root / "memory" / "journal.md").read_text()
        self.assertIn("*Sleep check:* still missing after it was sent back once:", journal)
        self.assertIn("gives no evidence", journal)
        self.assertTrue(self.episodes()[-1]["check"]["after"])

    def test_the_check_reads_the_right_parts(self):
        """Codex's review: a bare verdict with prose on the next line passed, and so did an id under another
        heading after an empty plan."""
        improvements.add(self.root, ITEM, SLEPT, "sleep")
        bare = "## How I will improve\nItem i1125-0300-1.\n\nSelf-model: unchanged\nThe weather was fine all night long."
        self.assertIn("gives no evidence", self.problems(bare)[0])
        archived = "## How I will improve\n\n## Archives\nOld plan i1125-0300-1.\n\n" + COMPLETE
        self.assertIn("names no item", self.problems(archived)[0])
        twice = "## How I will improve\nTests.\n\n## Notes\nx\n\n## How I will improve (again)\ni1125-0300-1\n" + COMPLETE
        self.assertEqual(self.problems(twice), [])
        missing = "## How I will improve\nTests.\n\n" + COMPLETE
        self.assertEqual(sleep.entry_problems(self.state, missing, [], "The item is i1125-0300-1."), [])
        (self.root / improvements.FILE).write_bytes(b"\xff\xfe broken\n")
        self.assertIn("names no item", self.problems(missing)[0])   # an unreadable list never stops the sleep

    def test_a_failed_send_back_keeps_the_entry(self):
        from shaft.llm import LLMError
        client = mock.Mock()
        client.complete.side_effect = [Reply("## How I will improve\nBetter tests.\n\n" + COMPLETE),
                                       LLMError("provider down")]
        sleep.run(self.cfg, self.state, client)
        self.assertIn("Better tests.", (self.root / "memory" / "journal.md").read_text())
        check = self.episodes()[-1]["check"]
        self.assertTrue(check["ended_by"].startswith("llm_error: provider down"))
        self.assertIn("names no item", check["after"][0])

    def test_an_open_conversation_cannot_reach_the_list(self):
        """Codex's review: the list's evidence is kept for good, so the open-transcript guard covers it too."""
        quote = "the operator said the quarterly figures for the northern orchard must stay private until friday"
        client = mock.Mock()
        client.complete.side_effect = [Reply("", tool_calls=[tool("plan_improvement", {**ITEM, "evidence": quote})]),
                                       Reply("## How I will improve\nnone\n\n" + COMPLETE), Reply("ok")]
        with mock.patch.object(sleep.recovery, "unwritten_transcripts", return_value=("transcript", [quote])):
            sleep.run(self.cfg, self.state, client)
        self.assertIn("awareness only", self.episodes()[-1]["tools"][0]["result"])
        self.assertEqual(improvements.fold(self.root), {})

    def test_the_sleep_prompt_asks_for_the_line_and_the_contradiction_check(self):
        text = prompts.SLEEP.replace("\\\n", "")
        self.assertIn("Self-model: unchanged — <the evidence you checked> did not contradict it", text)
        self.assertIn("two of its lines contradict each other", text)
        self.assertIn("plan_improvement for at most {per_sleep}", text)


if __name__ == "__main__":
    unittest.main()

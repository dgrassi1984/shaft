"""The supervisor's copy of commitments survives changes to the instance's files."""
import json
from datetime import timedelta
from unittest import mock

from supervisor import commitments, core, metrics
from supervisor.tests.test_idle_exit import IsolatedCase, NOW


class LedgerMirrorTest(IsolatedCase):
    def write_ledger(self, rel, rows):
        self.write(self.root / rel, rows)

    def promises(self):
        return [{"event": "made", "id": f"c1111-1200-{i}", "at": (NOW - timedelta(days=1)).isoformat(),
                 "source": "cycle", "promise": f"Send report {i}", "criterion": "Report delivered",
                 "made_to": "operator", "due": (NOW + timedelta(days=1)).isoformat()} for i in (1, 2)]

    def test_first_copy_takes_the_mind_stores_history_and_commit_state_keeps_it(self):
        from supervisor import ledger_mirror, mind
        made = self.promises()
        mind.init("ember")
        self.write_ledger(commitments.FILE, made)
        self.assertTrue(mind.commit("ember", "ember: live", self.root))
        self.write_ledger(commitments.FILE, [])
        with mock.patch.object(core, "git_wt", return_value=mock.Mock(stdout="")):
            self.assertTrue(core.commit_state("ember", "live", [], root=self.root))
        self.assertEqual(len(ledger_mirror.rows("ember", "commitments")), 2)
        self.assertIn("To the operator: made 2;", "\n".join(commitments.section("ember")))

    def test_a_recorded_promise_keeps_the_source_it_had_when_copied(self):
        """Regression (#7 review): a mirrored operator promise was re-checked against the current episodes on every
        read, so removing the conversation it was made in turned it into a promise to itself."""
        from supervisor import ledger_mirror
        made = {"event": "made", "id": "c1111-1200-1", "at": (NOW - timedelta(days=1)).isoformat(), "source": "talk",
                "promise": "Send report", "criterion": "Report delivered", "made_to": "operator", "sourced": True,
                "talk_id": "t1", "due": (NOW + timedelta(days=1)).isoformat()}
        talk = {"ts": (NOW - timedelta(days=1, minutes=1)).isoformat(), "kind": "talk", "role": "user",
                "talk_id": "t1", "content": "please"}
        self.write_ledger("memory/episodes/day.jsonl", [talk])
        self.write_ledger(commitments.FILE, [made])
        ledger_mirror.sync("ember", self.root)
        self.write_ledger("memory/episodes/day.jsonl", [])          # the conversation is gone from the instance's files
        items = commitments.ledger("ember", NOW, commitments.recorded("ember")[0])
        self.assertEqual([item["id"] for item in items.values() if item["made_to"] == "operator"],
                         [made["id"]])
        # A promise whose source never held is copied as the instance's own, and stays so.
        self.write_ledger(commitments.FILE, [made, {**made, "id": "c1111-1200-2", "talk_id": "never"}])
        ledger_mirror.sync("ember", self.root)
        self.write_ledger("memory/episodes/day.jsonl", [{**talk, "talk_id": "never"}])
        items = commitments.ledger("ember", NOW, commitments.recorded("ember")[0])
        self.assertEqual([item["id"] for item in items.values() if item["made_to"] == "operator"],
                         [made["id"]])

    def test_backfill_is_marked_done_and_retried_if_torn(self):
        """Regression (#7 review): backfill was judged done by the copy being non-empty: an empty ledger walked git
        at every commit, and a torn first copy never walked it again."""
        from supervisor import ledger_mirror
        made = self.promises()
        with mock.patch.object(ledger_mirror, "_history", return_value=[]) as history:
            ledger_mirror.sync("ember", self.root)
            ledger_mirror.sync("ember", self.root)
        self.assertEqual(history.call_count, 1)
        path = ledger_mirror.path("ember", "commitments")
        path.write_text('{"seen": "2026-11-11T10:00:00", "row": {"event": "mad')   # an interrupted first copy
        path.with_name("commitments.backfilled").unlink()
        text = "".join(json.dumps(r) + "\n" for r in made)
        with mock.patch.object(ledger_mirror, "_history", return_value=[(text, None)]):
            self.assertEqual(ledger_mirror.sync("ember", self.root), 2)
        self.assertEqual(ledger_mirror.rows("ember", "commitments"), made)

    def test_stats_and_report_include_a_removed_settle(self):
        """The supervisor's copy preserves the settle even after its removal from the instance's file."""
        from supervisor import ledger_mirror
        made = self.promises()
        settled = {"event": "settled", "id": made[0]["id"], "at": (NOW - timedelta(hours=1)).isoformat(),
                   "status": "kept", "evidence": (NOW - timedelta(minutes=30)).isoformat() + "#1"}
        ep = {"ts": (NOW - timedelta(minutes=30)).isoformat(), "run_started": (NOW - timedelta(hours=2)).isoformat(),
              "kind": "live", "finish": {}, "tools": [{"tool": "fetch_page", "args": {}, "result": "x"}]}
        self.write_ledger("memory/episodes/day.jsonl", [ep])
        self.write_ledger(commitments.FILE, made + [settled])
        ledger_mirror.sync("ember", self.root)
        self.write_ledger(commitments.FILE, made[1:])
        with mock.patch.object(metrics, "judgments", return_value=[]):
            section = "\n".join(commitments.section("ember"))
            report = commitments.report(["ember"])
        self.assertIn("made 2; kept (verified) 0; kept (verified, evidence not checkable) 0; kept (not verified) 1",
                      section)
        self.assertIn(f"{made[0]['id']}: kept; cited cycle not judged", report)
        self.assertIn("removed from its ledger since recorded", report)

    def test_a_row_cannot_claim_it_was_checked_when_copied(self):
        """Regression (#7 re-review): a ledger row carrying "_checked": true skipped the source check."""
        from supervisor import ledger_mirror
        made = {"event": "made", "id": "c1111-1200-1", "at": (NOW - timedelta(days=1)).isoformat(), "source": "talk",
                "promise": "Send report", "criterion": "Report delivered", "made_to": "operator", "sourced": True,
                "talk_id": "never", "due": (NOW + timedelta(days=1)).isoformat(), "_checked": True}
        self.write_ledger(commitments.FILE, [made])
        items = commitments.ledger("ember", NOW, commitments.recorded("ember")[0])
        self.assertEqual(items[made["id"]]["made_to"], "self")
        ledger_mirror.sync("ember", self.root)
        items = commitments.ledger("ember", NOW, commitments.recorded("ember")[0])
        self.assertEqual(items[made["id"]]["made_to"], "self")

    def test_a_lone_surrogate_does_not_stop_the_copy(self):
        """A row with an escaped lone surrogate must not prevent copying the rest of the batch."""
        from supervisor import ledger_mirror
        made = self.promises()
        path = self.root / commitments.FILE
        path.write_text(json.dumps({**made[0], "promise": "bad \ud800 text"}) + "\n" + json.dumps(made[1]) + "\n")
        self.assertEqual(ledger_mirror.sync("ember", self.root), 2)
        self.assertEqual(ledger_mirror.rows("ember", "commitments")[0]["promise"], "bad \ud800 text")

    def test_an_old_blob_that_is_not_utf8_does_not_stop_the_backfill(self):
        """A malformed line in old history must not block the backfill."""
        from supervisor import ledger_mirror, mind
        made = self.promises()
        mind.init("ember")
        (self.root / commitments.FILE).write_bytes(json.dumps(made[0]).encode() + b"\n\xff\xfe broken\n")
        self.assertTrue(mind.commit("ember", "ember: live", self.root))
        self.write_ledger(commitments.FILE, made)
        self.assertEqual(ledger_mirror.sync("ember", self.root), 2)
        self.assertTrue(ledger_mirror.path("ember", "commitments").with_name("commitments.backfilled").exists())

    def test_a_new_operator_promise_without_sourced_is_still_source_checked(self):
        """Regression (#7 final review): a row with no `sourced` field kept made_to=operator unchecked, as rows older
        than the source rule are, and the copy kept that verdict."""
        from supervisor import ledger_mirror
        made = self.promises()
        self.write_ledger(commitments.FILE, made[:1])
        ledger_mirror.sync("ember", self.root)         # the backfill: rows from before the rule keep their word
        self.write_ledger(commitments.FILE, made)               # a row after it, with no `sourced` and no source
        ledger_mirror.sync("ember", self.root)
        items = commitments.ledger("ember", NOW, commitments.recorded("ember")[0])
        self.assertEqual({i: it["made_to"] for i, it in items.items()},
                         {made[0]["id"]: "operator", made[1]["id"]: "self"})

    def test_a_cycle_a_removed_settle_cites_is_still_judged(self):
        """Regression (#7 final review): judging found commitment-cited cycles in the instance's file only, so
        removing a kept settle left its cited cycle unjudged."""
        from supervisor import judge, ledger_mirror
        made = self.promises()
        ep = {"ts": (NOW - timedelta(minutes=30)).isoformat(), "run_started": (NOW - timedelta(hours=1)).isoformat(),
              "kind": "live", "tools": [{"tool": "fetch_page", "args": {}, "result": "x"}]}
        settled = {"event": "settled", "id": made[0]["id"], "at": (NOW - timedelta(minutes=20)).isoformat(),
                   "status": "kept", "evidence": ep["ts"]}
        self.write_ledger("memory/episodes/day.jsonl", [ep])
        self.write_ledger(commitments.FILE, made + [settled])
        ledger_mirror.sync("ember", self.root)
        self.write_ledger(commitments.FILE, made)
        with mock.patch.object(metrics, "unsampled", return_value={ep["ts"]}), \
                mock.patch.object(metrics, "judgments", return_value=[]), \
                mock.patch.object(metrics, "change_outcomes", return_value=[]), \
                mock.patch.object(judge, "ask") as ask:
            self.assertEqual(metrics.judge_pending("ember"), 1)    # judged by rule: it declared no intent
        ask.assert_not_called()

    def test_a_backfilled_row_is_seen_when_it_was_committed(self):
        """Regression (#7 final review): the backfill stamped every historical row with the time of the first sync,
        so earlier reads omitted promises already committed."""
        from supervisor import ledger_mirror
        made = self.promises()
        text = "".join(json.dumps(r) + "\n" for r in made)
        committed = (NOW - timedelta(hours=3)).isoformat(timespec="seconds")
        with mock.patch.object(ledger_mirror, "_history", return_value=[(text, committed)]):
            ledger_mirror.sync("ember", self.root, now=NOW + timedelta(days=1))
        self.assertEqual({r["seen"] for r in ledger_mirror.records("ember", "commitments")}, {committed})
        rows, _ = commitments.recorded("ember", as_of=NOW)
        self.assertEqual([row["id"] for row in rows], [row["id"] for row in made])

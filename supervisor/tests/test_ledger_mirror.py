"""The supervisor's copy of commitments survives changes to the instance's files."""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import timedelta
from unittest import mock

from supervisor import broker, commitments, core, metrics
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


class WithinARunTest(IsolatedCase):
    """#21: a row written and taken back within one run never reached the copy, which synced only at the mind commit."""

    def made(self, i=1, talk="t1", **extra):
        return {"event": "made", "id": f"c1111-1200-{i}", "at": (NOW - timedelta(minutes=5)).isoformat(),
                "source": "talk", "promise": f"Send report {i}", "criterion": "Report delivered", "made_to": "operator",
                "sourced": True, "talk_id": talk, "due": (NOW + timedelta(days=1)).isoformat(), **extra}

    def talk(self, talk="t1"):
        return {"ts": (NOW - timedelta(minutes=6)).isoformat(), "kind": "talk", "role": "user", "talk_id": talk,
                "content": "please"}

    def write(self, path, rows):
        super().write(path, rows)
        st = path.stat()   # a later write in the same test must look changed to the broker's stat check
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000 * (1 + getattr(self, "_writes", 0))))
        self._writes = getattr(self, "_writes", 0) + 1

    def test_a_promise_taken_back_in_its_run_is_copied_and_judged_at_the_commit(self):
        from supervisor import ledger_mirror
        ledger_mirror.sync("ember", self.root)                    # an earlier commit: the copy is backfilled
        made = self.made()
        self.write(self.root / commitments.FILE, [made])          # commit_to, mid-run: the conversation's episode
        self.assertEqual(ledger_mirror.observe("ember", self.root), 1)   # is not written yet
        self.write(self.root / commitments.FILE, [])              # taken back before the run's commit
        self.write(self.root / "memory/episodes/day.jsonl", [self.talk()])   # the run ends: its episode is written
        ledger_mirror.sync("ember", self.root)                    # the mind commit judges it now, with its source
        rows, changed = commitments.recorded("ember")
        items = commitments.ledger("ember", NOW, rows)
        self.assertEqual(items[made["id"]]["made_to"], "operator")   # judged mid-run it would read "self"
        self.assertEqual(changed["removed"], {made["id"]})
        self.assertIn("To the operator: made 1;", "\n".join(commitments.section("ember")))
        # Judged once: a second commit adds nothing, and the verdict stays when the episode goes.
        size = ledger_mirror.path("ember", "commitments").stat().st_size
        ledger_mirror.sync("ember", self.root)
        self.assertEqual(ledger_mirror.path("ember", "commitments").stat().st_size, size)
        self.write(self.root / "memory/episodes/day.jsonl", [])
        self.assertEqual(commitments.ledger("ember", NOW, commitments.recorded("ember")[0])[made["id"]]["made_to"],
                         "operator")

    def test_a_promise_copied_in_its_run_is_source_checked_strictly(self):
        """Copied during a run, a promise first appears after the backfill: no source, or no `sourced`, is no source."""
        from supervisor import ledger_mirror
        ledger_mirror.sync("ember", self.root)
        sourceless, unsourced = self.made(1, talk="never"), self.made(2, talk="never")
        del unsourced["sourced"]
        self.write(self.root / commitments.FILE, [sourceless, unsourced])
        ledger_mirror.observe("ember", self.root)
        self.write(self.root / commitments.FILE, [])
        self.write(self.root / "memory/episodes/day.jsonl", [self.talk()])
        ledger_mirror.sync("ember", self.root)
        items = commitments.ledger("ember", NOW, commitments.recorded("ember")[0])
        self.assertEqual({i: it["made_to"] for i, it in items.items()},
                         {sourceless["id"]: "self", unsourced["id"]: "self"})

    def test_a_fresh_instances_first_run_is_copied_as_it_goes(self):
        """Regression (#21 review): observe copied nothing until a first commit had backfilled the copy, so a promise
        made and taken back in a fresh instance's first run was lost."""
        from supervisor import ledger_mirror
        made = self.made()
        self.write(self.root / commitments.FILE, [made])
        self.assertEqual(ledger_mirror.observe("ember", self.root), 1)
        self.assertTrue(ledger_mirror.path("ember", "commitments").with_name("commitments.backfilled").exists())
        self.assertTrue(all(r.get("judge") for r in ledger_mirror.records("ember", "commitments")))
        self.write(self.root / commitments.FILE, [])
        self.write(self.root / "memory/episodes/day.jsonl", [self.talk()])
        ledger_mirror.sync("ember", self.root)
        items = commitments.ledger("ember", NOW, commitments.recorded("ember")[0])
        self.assertEqual(items[made["id"]]["made_to"], "operator")

    def test_a_pause_commit_leaves_the_promise_to_the_commit_that_ends_the_run(self):
        """Regression (#21 review): a cycle paused for a conversation is committed before its episode is written; its
        promises were judged then, without their source, and recorded as the instance's own for good."""
        from supervisor import ledger_mirror, mind
        mind.init("ember")
        ledger_mirror.sync("ember", self.root)
        made, never = self.made(1), self.made(2, talk="never")
        self.write(self.root / commitments.FILE, [made, never])
        with mock.patch.object(core, "git_wt", return_value=mock.Mock(stdout="")):
            core.commit_state("ember", "live", [], root=self.root, judge_ledger=False)   # the pause: no episode yet
        self.assertTrue(all("checked" not in r for r in ledger_mirror.records("ember", "commitments")))
        self.write(self.root / commitments.FILE, [])
        rows = commitments.recorded("ember")[0]
        self.assertTrue(all(r.get("sourced") for r in rows))                       # read strictly meanwhile
        self.write(self.root / "memory/episodes/day.jsonl", [self.talk()])         # the cycle ends: its episode
        with mock.patch.object(core, "git_wt", return_value=mock.Mock(stdout="")):
            core.commit_state("ember", "live", [], root=self.root)
        recs = {r["row"]["id"]: r for r in ledger_mirror.records("ember", "commitments")}
        self.assertEqual((recs[made["id"]]["checked"]["made_to"], recs[never["id"]]["checked"]["made_to"]),
                         ("operator", "self"))
        self.write(self.root / "memory/episodes/day.jsonl", [self.talk("never")])   # judged once: kept as judged
        items = commitments.ledger("ember", NOW, commitments.recorded("ember")[0])
        self.assertEqual(items[never["id"]]["made_to"], "self")

    def test_a_pause_hands_its_commit_to_the_ledger_unjudged(self):
        from supervisor import runner
        (self.root / ".tmp").mkdir(exist_ok=True)
        proc = mock.Mock(pid=4242)
        proc.poll.return_value = None
        (self.root / ".tmp/pausable-4242").write_text("")        # its code handles the pause
        (self.root / ".tmp/paused-4242").write_text("")          # and it acknowledged it
        broker_ = mock.Mock(handoffs=[], config_writes=[])
        run = {"before": {}, "seen": 0, "cseen": 0}
        with mock.patch.object(runner, "_settle") as settle, mock.patch.object(runner, "replying", return_value=False), \
                mock.patch.object(runner, "_reconcile"), mock.patch.object(runner, "_snapshot"), \
                mock.patch("os.kill"), mock.patch.object(runner, "log"):
            runner._pause("ember", self.root, "live", proc, mock.Mock(), run, broker_)
        self.assertTrue(settle.call_args.kwargs["pausing"])
        before = {"journal": None, "core": None, "code": None}
        with mock.patch.object(runner, "commit_state", return_value=True) as commit, \
                mock.patch.object(runner, "enforce_journal", return_value=""), \
                mock.patch.object(runner, "enforce_core", return_value=""), \
                mock.patch.object(runner.gate, "snapshot", return_value=None), \
                mock.patch.object(runner, "_commit_outside_config"), mock.patch.object(runner, "_commit_outside_drive"), \
                mock.patch.object(runner, "_check_kept"), mock.patch.object(runner, "log"):
            for pausing in (True, False):
                runner._settle("ember", self.root, "live", before, [], pausing=pausing)
        self.assertEqual([c.kwargs["judge_ledger"] for c in commit.call_args_list], [False, True])

    def test_a_verdict_judged_later_is_not_read_by_an_earlier_replay(self):
        """Regression (#21 review): the verdict record kept the row's `seen`, so a replay as of a moment before the
        judging read the verdict."""
        from supervisor import ledger_mirror
        ledger_mirror.sync("ember", self.root, now=NOW - timedelta(minutes=10))
        made = {**self.made(talk="never"), "at": (NOW - timedelta(minutes=9)).isoformat()}
        self.write(self.root / commitments.FILE, [made])
        ledger_mirror.observe("ember", self.root, now=NOW - timedelta(minutes=8))
        ledger_mirror.sync("ember", self.root, now=NOW)                          # judged at NOW: no source, self
        self.assertEqual(commitments.ledger("ember", NOW, commitments.recorded("ember")[0])[made["id"]]["made_to"],
                         "self")
        rows = commitments.recorded("ember", as_of=NOW - timedelta(minutes=5))[0]
        self.assertEqual(len(rows), 1)
        self.assertNotIn(ledger_mirror.CHECKED, rows[0])                          # still waiting then
        self.assertTrue(rows[0].get("sourced"))                                   # and read strictly

    def test_a_same_size_rewrite_with_its_mtime_put_back_is_still_seen(self):
        from supervisor import ledger_mirror
        ledger_mirror.sync("ember", self.root)
        box = self.box()
        path = self.root / commitments.FILE
        self.write(path, [self.made(1)])
        box._observe_ledger()
        st = path.stat()
        path.write_text(path.read_text().replace("c1111-1200-1", "c1111-1200-9"))   # same size
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        box._observe_ledger()
        self.assertEqual([r["id"] for r in ledger_mirror.rows("ember", "commitments")],
                         ["c1111-1200-1", "c1111-1200-9"])

    def test_the_broker_copies_on_each_request_and_between_requests(self):
        """Regression (#21) through the broker of a real run: a POST it serves (a model step, a tool call) and its
        poll each copy what the ledger holds; a trial replay and a battery copy never do."""
        from supervisor import ledger_mirror
        ledger_mirror.sync("ember", self.root)
        box = self.box()
        box.egress = mock.Mock()
        with mock.patch.object(ledger_mirror, "POLL_S", 3600), mock.patch("supervisor.browser.sweep"):
            url = box.start()
            try:
                self.write(self.root / commitments.FILE, [self.made(1)])
                req = urllib.request.Request(url + "/tools/no_such_tool", data=b"{}", method="POST",
                                             headers={"X-Shaft-Token": box.token})
                with self.assertRaises(urllib.error.HTTPError):   # refused, but the step that led here is copied
                    urllib.request.urlopen(req, timeout=10)
                self.write(self.root / commitments.FILE, [])
                self.assertEqual([r["id"] for r in ledger_mirror.rows("ember", "commitments")], ["c1111-1200-1"])
                self.write(self.root / commitments.FILE, [self.made(2)])   # the run's last step
            finally:
                box.stop()
        self.assertEqual([r["id"] for r in ledger_mirror.rows("ember", "commitments")],
                         ["c1111-1200-1", "c1111-1200-2"])
        # The poll, with no request at all.
        box = self.box()
        box.egress = mock.Mock()
        with mock.patch.object(ledger_mirror, "POLL_S", 0.05), mock.patch("supervisor.browser.sweep"):
            box.start()
            try:
                self.write(self.root / commitments.FILE, [self.made(3)])
                for _ in range(100):
                    if len(ledger_mirror.rows("ember", "commitments")) == 3:
                        break
                    time.sleep(0.05)
                self.write(self.root / commitments.FILE, [])
            finally:
                box.stop()
        self.assertEqual(len(ledger_mirror.rows("ember", "commitments")), 3)
        # Not a trial replay's, nor a run outside the instance's own folder.
        self.write(self.root / commitments.FILE, [self.made(4)])
        replay = self.box(replay=True)
        replay._observe_ledger()
        elsewhere = self.box()
        elsewhere.root = self.base / "battery/ember"
        self.write(elsewhere.root / commitments.FILE, [self.made(5)])   # a row it would copy, were it the instance's
        elsewhere._observe_ledger()
        self.assertEqual(len(ledger_mirror.rows("ember", "commitments")), 3)
        self.box()._observe_ledger()
        self.assertEqual(len(ledger_mirror.rows("ember", "commitments")), 4)

    def test_a_failing_copy_never_costs_the_request(self):
        from supervisor import ledger_mirror
        box = self.box()
        self.write(self.root / commitments.FILE, [self.made()])
        with mock.patch.object(ledger_mirror, "observe", side_effect=OSError("disk full")), \
                mock.patch.object(broker.Broker, "_log", autospec=True, side_effect=OSError("disk full")):
            box._observe_ledger()          # neither the copy nor its log line raises

    def test_the_conversation_a_pause_made_room_for_does_not_judge_the_cycles_promises(self):
        """Regression (#21 review): the conversation's own commit, while the cycle waited, judged the cycle's promises
        before its episode existed; and holding every verdict let the resumed cycle relabel the conversation's own."""
        from supervisor import ledger_mirror
        ledger_mirror.sync("ember", self.root)
        cycles, talks = self.made(1, talk="later"), self.made(2)
        self.write(self.root / commitments.FILE, [cycles, talks])
        ledger_mirror.observe("ember", self.root)
        self.write(self.root / "memory/episodes/day.jsonl", [self.talk()])          # the conversation's episode
        with ledger_mirror.holding("ember"):
            ledger_mirror.sync("ember", self.root)                     # the conversation ends: its commit
        recs = {r["row"]["id"]: r for r in ledger_mirror.records("ember", "commitments")}
        self.assertEqual(recs[talks["id"]]["checked"]["made_to"], "operator")      # its source is there: recorded
        self.assertNotIn("checked", recs[cycles["id"]])                            # the cycle's waits
        self.write(self.root / "memory/episodes/day.jsonl", [self.talk("later")])  # the cycle ends, the talk's gone
        ledger_mirror.sync("ember", self.root)
        recs = {r["row"]["id"]: r for r in ledger_mirror.records("ember", "commitments")}
        self.assertEqual((recs[cycles["id"]]["checked"]["made_to"], recs[talks["id"]]["checked"]["made_to"]),
                         ("operator", "operator"))

    def test_a_hold_whose_runner_is_gone_holds_nothing(self):
        """The hold is a lock, not a pid: a runner that died (its pid reused or not) holds nothing."""
        import subprocess
        import sys
        from supervisor import ledger_mirror
        ledger_mirror.sync("ember", self.root)
        mark = ledger_mirror._hold_path("ember", None)
        script = ("import fcntl, os, sys, time\nfd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
                  "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(60)")
        proc = subprocess.Popen([sys.executable, "-c", script, str(mark)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "held")
            self.assertTrue(ledger_mirror._held("ember", None))
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()
        self.assertTrue(mark.exists())
        self.assertFalse(ledger_mirror._held("ember", None))

    def test_a_row_or_episode_of_the_wrong_shape_never_stops_the_check(self):
        """Regression (#21 review): a copied row whose time overflowed the local clock, or a live episode whose inbox
        was not a list, raised in the source check at every commit (so no promise was ever judged again) and in every
        reader, and the copy kept the row after the instance took it back."""
        from supervisor import ledger_mirror
        ledger_mirror.sync("ember", self.root)
        bad = {**self.made(1), "source": "cycle", "at": "0001-01-01T00:00:00+23:59",
               "due": "0001-01-01T00:00:00+23:59"}
        del bad["talk_id"]
        cycle = {**self.made(3), "source": "cycle", "at": (NOW - timedelta(minutes=5)).isoformat()}
        del cycle["talk_id"]
        good = self.made(2)
        self.write(self.root / commitments.FILE, [bad, good, cycle])
        ledger_mirror.observe("ember", self.root)
        self.write(self.root / commitments.FILE, [bad])               # one taken back, one still there
        odd = {"ts": (NOW - timedelta(minutes=1)).isoformat(), "kind": "live", "inbox": 1, "tools": "x"}
        self.write(self.root / "memory/episodes/day.jsonl", [self.talk(), odd])
        ledger_mirror.sync("ember", self.root)
        recs = {r["row"]["id"]: r for r in ledger_mirror.records("ember", "commitments")}
        self.assertEqual({i: r["checked"]["made_to"] for i, r in recs.items()},
                         {bad["id"]: "self", good["id"]: "operator", cycle["id"]: "self"})
        items = commitments.ledger("ember", NOW, commitments.recorded("ember")[0])   # no reader raises
        self.assertEqual(set(items), {good["id"], cycle["id"]})                       # a row with no time folds out
        commitments.recorded("ember", as_of=NOW - timedelta(minutes=1))
        "\n".join(commitments.section("ember"))

    def test_a_held_commit_defers_a_reopened_legacy_promise_without_its_source(self):
        """Regression (#21 review): a legacy row recorded as self (note RELABELLED) is reopened by the source check;
        without its cycle's episode it came back self, unchanged, and a held commit recorded that for good."""
        from supervisor import ledger_mirror
        ledger_mirror.sync("ember", self.root)
        legacy = {**self.made(1), "source": "cycle", "made_to": "self", "note": commitments.RELABELLED}
        del legacy["talk_id"]
        self.write(self.root / commitments.FILE, [legacy])
        ledger_mirror.observe("ember", self.root)
        with ledger_mirror.holding("ember"):
            ledger_mirror.sync("ember", self.root)
        self.assertNotIn("checked", ledger_mirror.records("ember", "commitments")[0])
        live = {"ts": (NOW - timedelta(minutes=1)).isoformat(), "kind": "live",
                "inbox": ["20261111-115000-000000-from-operator.md"]}
        self.write(self.root / "memory/episodes/day.jsonl", [live])       # the cycle ends, answering the operator
        ledger_mirror.sync("ember", self.root)
        self.assertEqual(ledger_mirror.records("ember", "commitments")[0]["checked"]["made_to"], "operator")

    def test_a_first_backfill_by_the_broker_keeps_history_verdicts_at_their_seen(self):
        """Regression (#21 review): a history row backfilled by observe got its verdict stamped at the judging, so a
        replay of a moment between the two read it against the current episodes."""
        from supervisor import ledger_mirror
        made = self.made()
        committed = (NOW - timedelta(days=2)).isoformat(timespec="seconds")
        text = json.dumps(made) + "\n"
        self.write(self.root / "memory/episodes/day.jsonl", [self.talk()])
        with mock.patch.object(ledger_mirror, "_history", return_value=[(text, committed)]):
            ledger_mirror.observe("ember", self.root, now=NOW)
        ledger_mirror.sync("ember", self.root, now=NOW)
        rec = ledger_mirror.records("ember", "commitments", as_of=NOW - timedelta(days=1))[0]
        self.assertEqual(rec["checked"]["made_to"], "operator")

    def test_relabelled_is_the_commitments_note(self):
        from supervisor import ledger_mirror
        self.assertEqual(ledger_mirror.RELABELLED, commitments.RELABELLED)


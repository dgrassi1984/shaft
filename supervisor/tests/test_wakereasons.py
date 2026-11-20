"""Standing wakes follow real project and commitment records and pace each item."""
import json
from datetime import timedelta
from unittest import mock

from supervisor import autonomy, battery, commitments, delete, jobs, projects, wakereasons
from supervisor.tests.test_idle_exit import IsolatedCase, NOW


class WakeReasonsTest(IsolatedCase):
    def project(self, pid="p1111-0001-1", deadline=None):
        return {"event": "opened", "id": pid, "at": (NOW - timedelta(days=4)).isoformat(),
                "name": "Example work", "success": "Report checked", "deadline": (deadline or NOW - timedelta(hours=1)).isoformat()}

    def test_past_boundary_project_wakes_while_on_track_does_not(self):
        self.write(self.root / projects.FILE, [self.project(), self.project("p1111-0001-2", NOW + timedelta(days=4))])
        rows = wakereasons.reasons("ember", NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "project:p1111-0001-1")
        self.assertIn("deadline", rows[0][1])

    def test_only_stale_open_operator_orders_from_talk_or_message_wake(self):
        base = {"event": "made", "id": "c1110-0001-1", "at": (NOW - timedelta(hours=24)).isoformat(),
                "due": (NOW + timedelta(days=3)).isoformat(), "promise": "Check report", "criterion": "Report checked",
                "made_to": "operator", "source": "talk", "talk_id": "example"}
        cases = [({}, True), ({"talk_id": None, "message_file": "example.md", "source": "cycle"}, True),
                 ({"at": (NOW - timedelta(hours=23, minutes=59)).isoformat()}, False),
                 ({"made_to": "self"}, False),
                 # a row from before the source rule is still the operator's in the ledger (#238): it wakes too
                 ({"talk_id": None, "source": "cycle"}, True),
                 ({"talk_id": None, "source": "cycle", "sourced": True}, False)]   # no source: recorded as self
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.write(self.root / commitments.FILE, [{**base, **changes}])
                self.assertEqual(bool(wakereasons.reasons("ember", NOW)), expected)
        settled = {"event": "settled", "id": base["id"], "at": NOW.isoformat(), "status": "kept", "evidence": "Report checked"}
        self.write(self.root / commitments.FILE, [base, settled])
        self.assertEqual(wakereasons.reasons("ember", NOW), [])

    def test_242_order_wake_waits_until_the_promise_is_due_within_three_days(self):
        """#242: an operator promise made 11-10 and due 12-10 woke the instance every day from 11-11 (13 wakes for
        two December promises in one week), with nothing to deliver yet. It wakes nothing until 3 days before due,
        and the wake says the due date. Reverting DUE_WINDOW makes the 11-19 case wake again."""
        from datetime import datetime
        row = {"event": "made", "id": "c1110-1043-1", "at": "2026-11-10T10:43:00", "due": "2026-12-10T23:59:59.999999",
               "promise": "Remind about the opening", "criterion": "Reminder sent", "made_to": "operator",
               "source": "talk", "talk_id": "example"}
        self.write(self.root / commitments.FILE, [row])
        with mock.patch.object(commitments, "datetime", wraps=datetime) as clock:
            for now, wakes in ((datetime(2026, 11, 19, 9), False), (datetime(2026, 12, 7, 9), False),
                               (datetime(2026, 12, 8, 9), True), (datetime(2026, 12, 10, 9), True)):
                with self.subTest(now=now):
                    clock.now.return_value = now
                    rows = wakereasons.reasons("ember", now)
                    self.assertEqual([k for k, _ in rows], ["order:c1110-1043-1"] if wakes else [])
                    if wakes:
                        self.assertIn("due 2026-12-10 23:59", rows[0][1])

    def test_mark_paces_each_item_expires_at_pace_and_prunes_closed_items(self):
        path = self.sup / "wake_reasons/ember.json"
        self.write(self.root / projects.FILE, [self.project(), self.project("p1111-0001-2")])
        wakereasons.mark("ember", "not a current reason", NOW)
        self.assertFalse(path.exists())
        first, second = wakereasons.reasons("ember", NOW)
        wakereasons.mark("ember", first[1], NOW)
        self.assertEqual(wakereasons.due("ember", NOW), second)
        wakereasons.mark("ember", second[1], NOW)
        self.assertEqual(wakereasons.due("ember", NOW + wakereasons.PACE - timedelta(seconds=1)), ("", ""))
        self.assertEqual(wakereasons.due("ember", NOW + wakereasons.PACE), first)
        self.write(self.root / projects.FILE, [self.project("p1111-0001-2")])
        wakereasons.mark("ember", second[1], NOW + wakereasons.PACE)
        self.assertEqual(set(json.loads(path.read_text())), {second[0]})

    def test_gate_standing_keeps_unread_due_result_priority_and_idle_first(self):
        def gate(facts, since=NOW - timedelta(minutes=1)):
            return autonomy.wake_gate(NOW, since, NOW + timedelta(minutes=5), timedelta(minutes=30), lambda: facts)
        standing = "project p1111-0001-1 is past its boundary"
        self.assertEqual(gate(autonomy.WakeFacts(standing=standing)), (True, standing))
        for fields, why in [({"unread": 1, "due": "c1", "results": 1}, "1 unread message(s) in its inbox"),
                            ({"due": "c1", "results": 1}, "commitment c1"),
                            ({"results": 1}, "1 job result(s) since its last cycle")]:
            self.assertEqual(gate(autonomy.WakeFacts(standing=standing, **fields)), (True, why))
        never = mock.Mock(side_effect=AssertionError("idle must decide first"))
        self.assertEqual(autonomy.wake_gate(NOW, NOW - timedelta(hours=1), NOW, timedelta(minutes=30), never),
                         (True, "no cycle for 60 min"))
        never.assert_not_called()

    def test_broken_projects_do_not_hide_unread_message(self):
        (self.root / "inbox/example-from-operator.md").write_text("Please check the report")
        (self.root / projects.FILE).write_text('{"broken":\n')
        with mock.patch.object(jobs, "declared", return_value=([], [])):
            facts = autonomy.wake_facts("ember", NOW - timedelta(minutes=5), NOW)
        self.assertEqual(facts.unread, 1)
        self.assertEqual(facts.error, "")

    def test_order_text_stays_stable_across_clock_change_so_mark_paces_it(self):
        """Elapsed hours in order text must not make a scheduler mark miss its reason."""
        self.write(self.root / commitments.FILE, [{"event": "made", "id": "c1110-0001-1",
            "at": (NOW - timedelta(hours=24, minutes=30)).isoformat(),
            "due": (NOW + timedelta(days=3)).isoformat(), "promise": "Check report",
            "criterion": "Report checked", "made_to": "operator", "source": "talk", "talk_id": "example"}])
        first, = wakereasons.reasons("ember", NOW)
        later = NOW + timedelta(minutes=59)
        self.assertEqual(wakereasons.reasons("ember", later), [first])
        wakereasons.mark("ember", first[1], later)
        self.assertEqual(wakereasons.due("ember", later), ("", ""))
        self.assertEqual(json.loads((self.sup / "wake_reasons/ember.json").read_text()),
                         {first[0]: later.isoformat()})

    def test_delete_lists_both_wake_files_but_not_another_instances(self):
        """Deleting an instance must remove pacing state and a leftover atomic-write temp file."""
        folder = self.sup / "wake_reasons"
        folder.mkdir()
        own = [folder / "ember.json", folder / "ember.tmp"]
        peer = folder / "example.json"
        for path in [*own, peer]:
            path.write_text("{}")
        with mock.patch.object(delete, "SUP_DIR", self.sup), \
             mock.patch.object(battery, "BACKUP", self.sup / "battery-backup"):
            found = delete.own_files("ember")
        self.assertEqual(set(found), set(own))

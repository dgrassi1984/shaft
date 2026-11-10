"""Regressions for live controls, exam isolation, and historical withdrawals."""
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import attention, battery, broker, core, gate_held, surprise


class ReviewFollowupsTest(unittest.TestCase):
    def test_attention_does_not_count_live_settlement_as_off_target(self):
        ep = {"intent": {"attention": [{"source": "mail"}]},
              "tools": [{"tool": "mail_search", "args": "{}"},
                        {"tool": "settle_commitment", "args": "{}"}]}
        score = attention.score(ep)
        self.assertEqual((score["calls"], score["on_named"], score["share"]), (1, 1, 1.0))

    def test_exam_disables_withdrawal_and_standing_orders_on_live_instance(self):
        self.assertTrue({"withdraw_held", "record_order"} <= battery.EXAM_OFF)

    def test_withdrawal_is_inward_in_supervisor_mirror(self):
        self.assertTrue(surprise.inward({"observed": "withdraw_held accepted the request"}))

    def test_replay_withdrawal_uses_historical_gate_record_and_never_current_pending(self):
        rows = [
            {"ts": "2026-10-01T09:00:00", "outcome": "held", "held": "old", "files": ["shaft/old.py", "config.env"]},
            {"ts": "2026-10-01T09:01:00", "outcome": "held", "held": "gone", "files": ["shaft/gone.py"]},
            {"ts": "2026-10-01T09:02:00", "outcome": "re-gated", "held": "gone", "result": "withdrawn"},
            {"ts": "2026-10-01T09:03:00", "outcome": "held", "held": "kept", "files": ["shaft/kept.py", "config.env"]},
            # Successful re-gates currently log their files without a held id.
            {"ts": "2026-10-01T09:04:00", "outcome": "re-gated", "result": "kept", "files": ["shaft/kept.py"]},
            {"ts": "2026-10-01T09:05:00", "outcome": "re-gated", "held": "old", "result": "held",
             "files": ["shaft/old.py"]},
            {"ts": "2026-10-01T11:00:00", "outcome": "re-gated", "held": "old", "result": "withdrawn"},
            {"ts": "2026-10-01T11:01:00", "outcome": "held", "held": "future", "files": ["shaft/future.py"]},
        ]
        with tempfile.TemporaryDirectory(prefix="shaft-replay-withdraw-") as tmp:
            root = Path(tmp)
            path = root / gate_held.RUNS / "kiln.jsonl"
            path.parent.mkdir()
            text = "".join(json.dumps(row) + "\n" for row in rows)
            path.write_text(text)
            with mock.patch.object(core, "SUP_DIR", root), \
                 mock.patch.object(gate_held, "pending", side_effect=AssertionError("current state read")):
                replay = broker.ReplayBroker.__new__(broker.ReplayBroker)
                replay.name = "kiln"
                replay.as_of = datetime(2026, 10, 1, 10)
                replay.sim_withdrawn = set()
                replay._tags = threading.local()
                self.assertEqual(replay._withdraw_held("old", "obsolete"),
                                 "withdrawn held change old: shaft/old.py; obsolete")
                for hid in ("old", "gone", "kept", "future"):
                    with self.subTest(hid=hid), self.assertRaisesRegex(ValueError, "no held change"):
                        replay._withdraw_held(hid, "obsolete")
                self.assertEqual(path.read_text(), text)
                self.assertFalse((root / gate_held.HELD).exists())


class WakeReviewFollowupsTest(unittest.TestCase):
    def test_unknown_job_mail_and_results_wake_with_one_declaration_scan(self):
        from supervisor import autonomy, commitments, jobs
        now = datetime(2026, 11, 6, 12)
        own = [{'plugin': 'quiet', 'can_wake': False}]
        files = [f'20261106-120000-00000{i}-from-job-{p}.md' for i, p in enumerate(('quiet', 'ci', 'edited'))]
        rows = [{'ts': now.isoformat(), 'plugin': p, 'delivered': True} for p in ('quiet', 'ci', 'edited')]
        with mock.patch.object(jobs, 'declared', return_value=(own, [])) as declared, \
                mock.patch.object(jobs, '_rows', return_value=rows), \
                mock.patch.object(autonomy, 'list_beneath', return_value=files), \
                mock.patch.object(commitments, 'coming_due', return_value=[]), \
                mock.patch.object(commitments, 'overdue_reminders', return_value=[]), \
                mock.patch.object(commitments, 'ledger', return_value={}):
            facts = autonomy.wake_facts('m', datetime(2026, 11, 6, 11), now)
        self.assertEqual((facts.unread, facts.results, facts.error), (2, 2, ''))
        self.assertEqual(declared.call_count, 1)
        with mock.patch.object(jobs, 'declared', side_effect=OSError('git failed')), \
                mock.patch.object(jobs, '_rows', return_value=rows):
            self.assertEqual(jobs.quiet_jobs('m'), set())
            self.assertEqual(jobs.results_since('m', datetime(2026, 11, 6, 11)), 3)

    def test_failed_warm_start_does_not_block_immediate_retry(self):
        from supervisor import autonomy
        st, children = autonomy.WarmState(), {}
        child = object()
        with mock.patch.object(autonomy.gate, 'commits_id', return_value='pair'), mock.patch.object(autonomy, 'log'):
            autonomy.gate_warm(children, st, mock.Mock(side_effect=OSError('fork failed')), now=10)
            self.assertEqual((st.pair, st.started, st.timed_out), (None, float('-inf'), False))
            autonomy.gate_warm(children, st, lambda args: child, now=11)
        self.assertEqual((st.pair, st.started, children['gate warm'][0]), ('pair', 11, child))

    def test_regate_same_second_pass_monotonic_retry_and_failed_start(self):
        from supervisor import autonomy
        st, children = autonomy.WarmState(), {}
        rec = {'passed_at': '2026-11-06T12:00:00'}
        child = object()
        with mock.patch.object(autonomy, 'restart_request', return_value=None), \
                mock.patch.object(gate_held, 'waiting', return_value=True), \
                mock.patch.object(autonomy.gate, 'commits_id', return_value='pair'), \
                mock.patch.object(autonomy.gate, 'shared_key', return_value='key'), \
                mock.patch.object(autonomy.gate, 'recorded_pass', return_value=rec), \
                mock.patch.object(autonomy.time, 'monotonic', return_value=10) as clock, \
                mock.patch.object(autonomy.time, 'time', side_effect=AssertionError('wall clock')), \
                mock.patch.object(autonomy, 'log'):
            autonomy.regate_recorded(children, st, mock.Mock(side_effect=OSError('fork failed')))
            self.assertEqual((st.regated_pass, st.regated_at), (None, float('-inf')))
            autonomy.regate_recorded(children, st, lambda args: child)
            self.assertEqual((st.regated_pass, st.regated_at, children['gate regate'][0]), (rec['passed_at'], 10, child))
            children.clear()
            clock.return_value = 10.5
            rec['passed_at'] = '2026-11-06T12:00:01'
            autonomy.regate_recorded(children, st, lambda args: child)
            self.assertEqual((st.regated_pass, st.regated_at), ('2026-11-06T12:00:01', 10.5))
            children.clear()
            clock.return_value = 11
            autonomy.regate_recorded(children, st, lambda args: child)
            self.assertEqual(children, {})
            clock.return_value = 10.5 + autonomy.WARM_RETRY_S
            autonomy.regate_recorded(children, st, lambda args: child)
            self.assertEqual(children['gate regate'][0], child)


class CitedGradeReviewFollowupsTest(unittest.TestCase):
    def test_cited_grades_do_not_leak_into_battery_broker_panel_or_projects(self):
        from supervisor import metrics, panel, projects
        from supervisor.tests import test_m7_m9_regressions as fixtures
        rows = [{'episode_ts': '2026-11-06T11:00:00', 'criterion_met': 'yes', 'should_have_waited': 'no'},
                {'episode_ts': '2026-11-06T11:01:00', 'criterion_met': 'no', 'should_have_waited': 'yes', 'cited': True}]
        with mock.patch.object(metrics, 'judgments', return_value=rows), \
                mock.patch.object(metrics, 'cycle_value', return_value=1), \
                mock.patch.object(panel, 'cached', side_effect=lambda key, paths, read: read()):
            self.assertEqual(battery.truth('m', 'judged_counts'), '1 autonomous cycles judged: 1 yes, 0 partial, 0 no.')
            b = broker.Broker.__new__(broker.Broker)
            b.name, b.project = 'm', Path('/unused')
            self.assertEqual(b._waited_share(datetime(2026, 11, 6, 12)), '0/1 (0%)')
            self.assertEqual([r['episode_ts'] for r in panel.judgments('m')], ['2026-11-06T11:00:00'])
            item = {'status': 'open', 'name': 'report', 'success': 'sent',
                    'at': '2026-11-01T00:00:00', 'deadline': '2027-01-01T00:00:00'}
            eps = [{'kind': 'live', 'ts': row['episode_ts'], 'intent': {'project': 'p'},
                    'project_usage': {'calls': 0, 'usd': 0}} for row in rows]
            with mock.patch.object(core, 'safe_read', return_value=''), \
                    mock.patch.object(projects, '_fold', return_value={'p': item}), \
                    mock.patch.object(metrics, 'live_episodes', return_value=eps), \
                    mock.patch.object(projects, 'datetime', fixtures.Clock):
                self.assertIn('judged no/partial streak 0', '\n'.join(projects.section('m')))

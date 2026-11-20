"""#232: slow order judges must not serialize one instance's outward writes."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from supervisor import broker, metrics, orders
from supervisor.tests import test_calendar as calendar_read
from supervisor.tests import test_calendar_write as calendars
from supervisor.tests import test_mail_draft as drafts


class OrdersOutsideLocksTest(unittest.TestCase):
    def fixture(self, kind):
        f = kind()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.addCleanup(f.tearDown)
        orders.add('wisp', 'Keep notes short.', 'operator', 'cli')
        self.enterContext(mock.patch.object(metrics, 'own_models', return_value=[]))
        return f

    def overlap(self, f, tool, args, during=None, second_args=None):
        """Hold only the first judge; always release and join workers, including on assertion failure."""
        entered, release = threading.Event(), threading.Event()
        calls, guard = 0, threading.Lock()

        def judge(*args, **kwargs):
            nonlocal calls
            with guard:
                calls += 1
                first = calls == 1
            if first:
                entered.set()
                if not release.wait(5):
                    raise AssertionError('test did not release the first judge')
            return {'violates': False, 'order_id': '', 'reason': '', 'judge': 'stub'}

        first_broker, second_broker = f.broker(), f.broker()
        with mock.patch.object(orders, 'check', side_effect=judge), ThreadPoolExecutor(2) as pool:
            first = pool.submit(first_broker.call, tool, args)
            try:
                self.assertTrue(entered.wait(2), 'first call did not reach judge')
                if during:
                    during()
                second = pool.submit(second_broker.call, tool, second_args or args)
                result = second.result(timeout=2)
                self.assertFalse(first.done(), 'first judge should still be blocked')
            finally:
                release.set()
            return first.result(timeout=2), result

    def test_second_draft_finishes_during_first_judge(self):
        """#232: reverting the draft split blocks the second call behind the first judge."""
        f = self.fixture(drafts.DraftTest)
        results = self.overlap(f, 'mail_draft', f.args)
        self.assertEqual([r['ok'] for r in results], [True, True], results)
        self.assertEqual(len(f.gmail.uploads), 2)

    def test_second_calendar_finishes_during_first_judge(self):
        """#232: reverting the audit unlock blocks a second calendar call behind the judge."""
        f = self.fixture(calendars.WriteCase)
        results = self.overlap(f, 'calendar_create', f.args)
        self.assertEqual([r['ok'] for r in results], [True, True], results)
        self.assertEqual(len(f.cal.writes), 2)

    def test_calendar_cap_is_rechecked_after_judge(self):
        """#232: dropping the locked cap recheck lets both checked calls spend the last slot."""
        f = self.fixture(calendars.WriteCase)
        with mock.patch.object(broker, 'CALENDAR_WRITES_PER_DAY', 1):
            first, second = self.overlap(f, 'calendar_create', f.args)
        self.assertTrue(second['ok'], second)
        self.assertFalse(first['ok'], first)
        self.assertIn('at most 1 calendar writes per day', first['error'])
        self.assertEqual(len(f.cal.writes), 1)
        self.assertEqual(sum(r['submitted'] for r in f.rows()), 1)

    def test_draft_update_rechecks_registry_ownership(self):
        """#232: dropping the registry recheck lets a stale update replace a concurrent update."""
        f = self.fixture(drafts.DraftTest)
        f.draft()
        args = {'id': 'work/draft/d1', **f.update}
        first, second = self.overlap(f, 'mail_draft_update', args,
                                     second_args={**args, 'body': 'second update'})
        self.assertTrue(second['ok'], second)
        self.assertFalse(first['ok'], first)
        self.assertEqual(len(f.gmail.uploads), 2)  # creation and exactly one update
        self.assertEqual(f.gmail.uploads[-1][2].get_content().strip(), 'second update')

    def test_operator_takes_draft_during_judge(self):
        """#232: dropping the Google ownership recheck overwrites the operator's draft."""
        f = self.fixture(drafts.DraftTest)
        f.draft()
        first, second = self.overlap(
            f, 'mail_draft_update', {'id': 'work/draft/d1', **f.update},
            during=lambda: f.gmail.drafts['d1']['message'].update(id='operator-edit'))
        self.assertFalse(first['ok'], first)
        self.assertFalse(second['ok'], second)
        self.assertEqual(len(f.gmail.uploads), 1)

    def test_changed_event_is_refused_after_judge(self):
        """#232: dropping the ETag recheck permits a proposal built from unchecked event content."""
        for tool in ('calendar_move', 'calendar_move_proposal', 'calendar_respond'):
            with self.subTest(tool=tool):
                f = self.fixture(calendars.WriteCase)
                eid = 'external1' if tool == 'calendar_respond' else 'own1'
                args = (f.move_args(eid, **({'attendees': [{'email': 'guest@example.com'}]}
                                           if tool == 'calendar_move_proposal' else {}))
                        if tool.startswith('calendar_move') else
                        {'id': f.handle(calendar_read.PRIMARY, eid), 'response': 'yes'})
                b = f.broker()

                entered, release = threading.Event(), threading.Event()

                def changed(*a, **kw):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('test did not release the judge')
                    return {'violates': False, 'order_id': '', 'reason': '', 'judge': 'stub'}

                with mock.patch.object(orders, 'check', side_effect=changed), ThreadPoolExecutor(1) as pool:
                    future = pool.submit(b.call, tool.removesuffix('_proposal'), args)
                    try:
                        self.assertTrue(entered.wait(2), 'call did not reach judge')
                        f.cal.events[calendar_read.PRIMARY][eid].update(etag='"v2"', description='changed')
                    finally:
                        release.set()
                    result = future.result(timeout=2)
                self.assertFalse(result['ok'], result)
                self.assertIn('changed', result['error'])
                self.assertEqual(f.cal.writes, [])
                self.assertFalse((f.sup / 'calendar/proposals.jsonl').exists())
                # Each fixture patches global paths; restore it before creating the next.
                f.doCleanups()

    def test_refusal_writes_nothing(self):
        """#232: submitting before judging would write content refused by a standing order."""
        for kind, tool in ((drafts.DraftTest, 'mail_draft'), (calendars.WriteCase, 'calendar_create')):
            with self.subTest(tool=tool):
                f = self.fixture(kind)
                with mock.patch.object(orders, 'check', return_value={
                        'violates': True, 'order_id': 'o1', 'reason': 'too long', 'judge': 'stub'}):
                    result = f.broker().call(tool, f.args)
                self.assertFalse(result['ok'], result)
                self.assertIn('standing order o1', result['error'])
                self.assertEqual(f.gmail.uploads if tool == 'mail_draft' else f.cal.writes, [])
                f.doCleanups()

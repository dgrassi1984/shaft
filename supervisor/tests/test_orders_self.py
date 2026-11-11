"""Self rules and outward checks through broker tools, with temporary state and fake transports."""
from __future__ import annotations

import io
import json
import threading
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from unittest import mock

from supervisor import broker, core, judge, metrics, orders
from supervisor import __main__ as cli
from supervisor.tests import test_orders as existing
from supervisor.tests import test_mail_draft as drafts
from supervisor.tests import test_calendar_write as calendars


class SelfRulesTest(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.OrdersTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.b = self.fixture.b
        self.b.name = 'ember'
        self.now = existing.NOW
        self.enterContext(mock.patch.object(orders, "datetime", wraps=existing.datetime)).now.return_value = self.now
        self.ts = (self.now - timedelta(hours=1)).isoformat(timespec='seconds')
        folder = self.b.root / 'memory/episodes'
        folder.mkdir(parents=True)
        self.episodes = folder / '2026-10.jsonl'
        self.episodes.write_text(json.dumps({'ts': self.ts, 'kind': 'live'}) + '\n')

    def test_own_source_is_a_real_past_episode_with_unique_minute_prefix(self):
        """Missing, foreign, ambiguous and future sources cannot support a self rule."""
        for source in ('', 'made-up', '2027-01-01 12:00'):
            with self.assertRaisesRegex(ValueError, 'past live cycles'):
                self.b._record_order('Keep notes short.', source, own=True)
        result = self.b.call('record_order', {'text': 'Keep notes short.',
                            'source': self.ts[:16].replace('T', ' '), 'own': True})
        self.assertTrue(result['ok'], result)
        self.assertEqual([(o['by'], o['source']) for o in orders.listing('ember')], [('self', self.ts)])
        self.b._record_order('Check evidence.', self.ts, own=True)
        self.episodes.write_text(self.episodes.read_text() + json.dumps({'ts': self.ts[:-2] + '30', 'kind': 'live'}) + '\n')
        with self.assertRaisesRegex(ValueError, 'unique'):
            self.b._record_order('Ambiguous.', self.ts[:16], own=True)
        self.b.as_of = self.now - timedelta(hours=2)
        with self.assertRaisesRegex(ValueError, 'past live cycles'):
            self.b._record_order('Future.', self.ts, own=True)

    def test_only_self_rules_can_be_dropped_by_instance_operator_can_drop_any(self):
        """Ownership is enforced for both kinds of operator order, even with a known id."""
        for by in ('operator', 'instance'):
            o, _ = orders.add('ember', by, by, 'conversation')
            with self.assertRaisesRegex(ValueError, 'only the operator drops his orders'):
                self.b._record_order(drop=o['id'])
        self.b._record_order('Own rule.', self.ts, own=True)
        self.assertIn('dropped own rule o3', self.b._record_order(drop='o3'))
        self.b._record_order('Another own rule.', self.ts, own=True)
        orders.drop('ember', 'o4')
        self.assertEqual([o['by'] for o in orders.listing('ember')], ['operator', 'instance'])
        with self.assertRaisesRegex(ValueError, 'already dropped'):
            orders.drop_own('ember', 'o3')

    def test_self_and_operator_caps_both_hold(self):
        """F11: self rules cannot crowd out operator additions; instance additions count all rows."""
        for n in range(orders.SELF_MAX):
            self.b._record_order(f'Own {n}', self.ts, own=True)
        with self.assertRaisesRegex(ValueError, '10 self rules.*drop'):
            self.b._record_order('Too many.', self.ts, own=True)
        for n in range(orders.ACTIVE_MAX):
            orders.add('ember', f'Operator {n}', 'operator', 'cli')
        self.assertEqual(len(orders.listing('ember')), 50)
        with self.assertRaisesRegex(ValueError, '40 standing orders'):
            orders.add('ember', 'Full.', 'operator', 'cli')
        orders.drop('ember', 'o1')
        with self.assertRaisesRegex(ValueError, '40 standing orders'):
            self.b._record_order('Also full.', self.ts, own=True)
        with self.assertRaisesRegex(ValueError, '40 standing orders'):
            orders.add('ember', 'Heard rule.', 'instance', 'conversation')

    def test_f4_own_rule_requires_live_cycle(self):
        """F4: conversation and sleep episodes are not a cycle source for a self rule."""
        for kind in ('talk', 'talk_recovered', 'sleep'):
            with self.subTest(kind=kind):
                self.episodes.write_text(json.dumps({'ts': self.ts, 'kind': kind}) + '\n')
                with self.assertRaisesRegex(ValueError, 'past live cycles'):
                    self.b._order_source(self.ts, own=True)

    def test_self_violation_is_refused_quoted_counted_and_listed(self):
        """Self rules use the same judge and refusal path, and counts include all tools and cutoffs."""
        self.b._record_order('Keep notes short.', self.ts, own=True)
        self.fixture.verdicts = [{'violates': True, 'order_id': 'o1', 'reason': 'too long'}]
        with self.assertRaisesRegex(PermissionError, 'my own rule.*Keep notes short'):
            self.b._send_message('operator', 'Long note.')
        self.assertFalse((self.fixture.sup / 'operator-inbox').exists())
        self.assertEqual(orders.violations('ember'), {'o1': 1})
        self.assertEqual(orders.violations('ember', as_of=self.now - timedelta(days=1)), {})
        extra = [{'ts': self.now.isoformat(), 'tool': 'calendar_create', 'violates': True, 'order_id': 'o1'}]
        self.assertEqual(orders.violations('ember', extra=extra), {'o1': 2})
        self.assertEqual(orders.stats_lines('ember'), ['## My own rules',
            '- o1 (since 2026-10-28): Keep notes short. — 1 message(s)/action(s) refused against it', ''])
        self.assertIn("agent's own rule", self.fixture.prompts[0])
        out = io.StringIO()
        with mock.patch.object(cli, 'instance_root', return_value=self.b.root), redirect_stdout(out):
            self.assertEqual(cli.cmd_orders('ember', None, []), 0)
        self.assertIn('by self', out.getvalue())
        self.assertIn('1 message(s)/action(s) refused', out.getvalue())
        orders.drop('ember', 'o1')
        self.assertEqual(orders.stats_lines('ember'), [])

    def test_replay_add_drop_and_readd_are_visible_without_order_writes(self):
        """Replay sources come from its state root and simulated drops affect subsequent checks and ids."""
        r = self.fixture.replay()
        r.name, r.sim_dropped = 'ember', set()
        r.root = self.fixture.project / 'replay'
        folder = r.root / 'memory/episodes'
        folder.mkdir(parents=True)
        (folder / '2026-10.jsonl').write_text(self.episodes.read_text())
        self.episodes.unlink()  # only the reconstructed replay state retains this source
        r._record_order('Keep notes short.', self.ts, own=True)
        self.assertEqual(r._orders_in_force()[0]['by'], 'self')
        self.fixture.verdicts = [{'violates': True, 'order_id': 'o1', 'reason': 'too long'}]
        with self.assertRaises(PermissionError):
            r._send_message('operator', 'Long note.')
        self.assertEqual(len(r.sim_checks), 1)
        r._record_order(drop='o1')
        self.assertEqual(r._orders_in_force(), [])
        self.fixture.ask.reset_mock()
        self.assertEqual(r._check_orders('anything', 'mail_draft', self.now, 'mail draft'), (None, ''))
        self.fixture.ask.assert_not_called()
        self.assertIn('o2', r._record_order('Keep notes short.', self.ts, own=True))
        self.assertEqual([o['id'] for o in r._orders_in_force()], ['o2'])
        r.talk_id = 'api-replay'
        r._record_order('An operator order.')
        with self.assertRaisesRegex(ValueError, 'only the operator drops his orders'):
            r._record_order(drop='o3')
        self.assertFalse((core.SUP_DIR / 'orders').exists())
        self.assertIn('simulated: not recorded', r._tags.value)

    def test_non_operator_checks_neither_advance_nor_restart_correction_passes(self):
        """A passing draft and a refused calendar change leave two operator passes intact."""
        self.fixture.correction()
        cid = 'k1028-1100-1'
        self.b.name = 'm'
        passed = {'violates': False, 'order_id': '', 'reason': '', 'judge': 'stub'}
        with mock.patch.object(orders, 'check', return_value=passed):
            for _ in range(2):
                row, _ = self.b._check_orders('Note.', 'note.md', self.now)
                self.b._log_check(row)
            row, _ = self.b._check_orders('Draft.', 'mail_draft', self.now, 'mail draft')
            self.b._log_check(row)
        self.assertEqual(self.b._corrections_held()[0]['passes'], 2)
        with mock.patch.object(orders, 'check', return_value={**passed, 'violates': True, 'order_id': cid}):
            with self.assertRaises(PermissionError):
                self.b._check_orders('Change.', 'calendar_move', self.now, 'calendar change')
        self.assertEqual(self.b._corrections_held()[0]['passes'], 2)
        # Old rows count as operator messages; an explicit message to another instance does not.
        orders.log_check('m', {'ts': self.now.isoformat(), 'tool': 'send_message', 'to': 'instance',
                              'corrections': [cid], **passed})
        self.assertEqual(self.b._corrections_held()[0]['passes'], 2)
        orders.log_check('m', {'ts': self.now.isoformat(), 'corrections': [cid], **passed})
        self.assertEqual(self.b._corrections_held(), [])


class OutwardToolsTest(unittest.TestCase):
    def exercise(self, fixture_type, tool, args_for, effects):
        """F5: successful draft/calendar writes survive failed check logging; refusals still precede writes."""
        for mode in ('violate', 'pass', 'error', 'none', 'log_error'):
            with self.subTest(tool=tool, mode=mode):
                f = fixture_type()
                try:
                    f.setUp()
                    b = f.broker()
                    args = args_for(f, b)
                    if mode != 'none':
                        orders.add(b.name, 'Keep notes short.', 'operator', 'cli')
                    before = effects(f)
                    verdict = {'violates': mode == 'violate', 'order_id': 'o1' if mode == 'violate' else '',
                               'reason': 'too long', 'judge': 'stub'}
                    with mock.patch.object(b, '_draft_submit', wraps=b._draft_submit) as submit, \
                            mock.patch.object(b, '_calendar_propose', wraps=b._calendar_propose) as propose, \
                            mock.patch.object(metrics, 'own_models', return_value=[]), \
                            mock.patch.object(orders, 'check', return_value=verdict,
                                              side_effect=judge.JudgeError('unavailable') if mode == 'error' else None) as check, \
                            mock.patch.object(b, '_log_check', wraps=b._log_check,
                                              side_effect=OSError('disk error') if mode == 'log_error' else None), \
                            mock.patch.object(core, 'log') as logged:
                        result = b.call(tool, args)
                    if mode == 'violate':
                        self.assertFalse(result['ok'], result)
                        self.assertIn('standing order o1', result['error'])
                        self.assertIn('Rewrite it', result['error'])
                        self.assertEqual(effects(f), before)
                        submit.assert_not_called()
                        propose.assert_not_called()
                    else:
                        self.assertTrue(result['ok'], result)
                        if mode in ('pass', 'log_error'):
                            self.assertIn('; checked against your standing orders: it keeps to them', result['result'])
                        elif mode == 'error':
                            self.assertIn('NOT checked', result['result'])
                        else:
                            check.assert_not_called()
                        self.assertNotEqual(effects(f), before)
                    if mode != 'none':
                        check.assert_called_once()
                        content = check.call_args.args[1]
                        self.assertTrue(content)
                        rows = orders._lines(orders.checks_path(b.name))
                        if mode == 'log_error':
                            self.assertEqual(rows, [])
                            logged.assert_called_once()
                            self.assertIn('action succeeded', logged.call_args.args[0])
                        else:
                            self.assertEqual(rows[-1]['tool'], tool)
                        if tool.startswith('mail_'):
                            self.assertIn('Subject:', content)
                            self.assertIn('To:', content)
                        if tool == 'calendar_respond':
                            self.assertIn('Response: yes', content)
                finally:
                    f.doCleanups()

    @staticmethod
    def draft_effects(f):
        return len(f.gmail.uploads)

    @staticmethod
    def calendar_effects(f):
        proposal = f.sup / 'calendar/proposals.jsonl'
        return len(f.cal.writes), proposal.read_bytes() if proposal.exists() else b''

    def test_mail_draft(self):
        """New drafts are checked before Gmail submission."""
        self.exercise(drafts.DraftTest, 'mail_draft', lambda f, b: f.args, self.draft_effects)

    def test_mail_draft_update(self):
        """Replacement drafts are checked independently of the earlier accepted draft."""
        def args(f, b):
            f.draft(b)
            return {'id': 'work/draft/d1', **{k: v for k, v in f.args.items() if k != 'account'}}
        self.exercise(drafts.DraftTest, 'mail_draft_update', args, self.draft_effects)

    def test_calendar_create_direct_and_proposal(self):
        """Both direct calendar writes and attendee proposals are checked first."""
        self.exercise(calendars.WriteCase, 'calendar_create', lambda f, b: f.args, self.calendar_effects)
        self.exercise(calendars.WriteCase, 'calendar_create',
                      lambda f, b: {**f.args, 'attendees': [{'email': 'guest@example.com'}]}, self.calendar_effects)

    def test_calendar_move(self):
        """Calendar moves are checked before PATCH."""
        self.exercise(calendars.WriteCase, 'calendar_move', lambda f, b: f.move_args(), self.calendar_effects)

    def test_calendar_respond(self):
        """RSVP proposals cannot bypass the check."""
        from supervisor.tests import test_calendar as read
        self.exercise(calendars.WriteCase, 'calendar_respond',
                      lambda f, b: {'id': f.handle(read.PRIMARY, 'external1'), 'response': 'yes'}, self.calendar_effects)

    def test_replay_drafts_check_in_memory_without_writes(self):
        """Both replay draft tools log into sim_checks and never upload or write the checks ledger."""
        from supervisor.tests import test_mail as read
        f = drafts.DraftTest()
        try:
            f.setUp()
            state = f.project / 'replay'
            state.mkdir()
            b = broker.ReplayBroker('wisp', f.project, f.secrets, f.sup / 'broker.log', state_root=state,
                                    start_commit='0', as_of=read.T0.replace(hour=12), cache={},
                                    cache_lock=threading.Lock(), billed='wisp@trial:t', tools_log=f.sup / 't.log')
            b.sim_orders.append({'id': 'o1', 'text': 'Keep notes short.', 'by': 'self',
                                 'source': read.T0.isoformat(), 'added_at': read.T0.isoformat(), 'dropped_at': None})
            with mock.patch.object(metrics, 'own_models', return_value=[]), \
                    mock.patch.object(orders, 'check', return_value={'violates': False, 'order_id': '',
                                                                  'reason': '', 'judge': 'stub'}):
                result = b.call('mail_draft', {'reply_to': 'work/aaa111', 'body': 'Short.'})
                self.assertTrue(result['ok'], result)
                result = b.call('mail_draft_update', {'id': 'work/draft/simulated-1', 'body': 'Shorter.'})
                self.assertTrue(result['ok'], result)
            self.assertEqual([r['tool'] for r in b.sim_checks], ['mail_draft', 'mail_draft_update'])
            self.assertEqual(f.gmail.uploads, [])
            self.assertFalse((f.sup / 'orders').exists())
            self.assertFalse((f.sup / 'mail-drafts').exists())
        finally:
            f.doCleanups()

    def test_http_and_run_do_not_call_order_judge(self):
        """Valid HTTP writes and shell commands remain outside the standing-order check."""
        from supervisor import sandbox
        f = drafts.DraftTest()
        try:
            f.setUp()
            b = f.broker()
            orders.add(b.name, 'Keep notes short.', 'operator', 'cli')
            with mock.patch.object(orders, 'check') as check, \
                    mock.patch.object(broker, 'check_public'), \
                    mock.patch.object(b, '_send', return_value=(200, {}, b'accepted')) as send:
                result = b.call('http_request', {'url': 'https://example.com/change', 'method': 'POST', 'body': 'change'})
                self.assertTrue(result['ok'], result)
                self.assertIn('accepted', result['result'])
                send.assert_called_once()
                check.assert_not_called()
            process = mock.Mock(returncode=0)
            process.communicate.return_value = ('done', '')
            with mock.patch.object(orders, 'check') as check, \
                    mock.patch.object(sandbox, 'run_profile', return_value=''), \
                    mock.patch.object(b, 'sandbox_env', return_value={}), \
                    mock.patch.object(broker.subprocess, 'Popen', return_value=process) as popen, \
                    mock.patch('supervisor.gate.stop_group'):
                result = b.call('run', {'command': 'printf done'})
                self.assertTrue(result['ok'], result)
                self.assertIn('done', result['result'])
                popen.assert_called_once()
                check.assert_not_called()
        finally:
            f.doCleanups()

    def test_message_to_another_instance(self):
        """A refused peer message creates neither an inbox nor stored attachments."""
        for mode in ('violate', 'pass', 'error', 'none', 'log_error'):
            with self.subTest(mode=mode):
                f = existing.OrdersTest()
                try:
                    f.setUp()
                    b = f.b
                    b.name = 'ember'
                    target = f.project / 'instances/ash'
                    (target / 'shaft').mkdir(parents=True)
                    if mode != 'none':
                        orders.add('ember', 'Keep notes short.', 'operator', 'cli')
                    with mock.patch.object(orders, 'check', return_value={'violates': mode == 'violate',
                            'order_id': 'o1', 'reason': 'too long', 'judge': 'stub'},
                            side_effect=judge.JudgeError('unavailable') if mode == 'error' else None) as check, \
                            mock.patch('supervisor.autonomy.event_wake'):
                        if mode == 'violate':
                            with self.assertRaisesRegex(PermissionError, 'message to another instance'):
                                b._send_message('ash', 'Long note.')
                            self.assertFalse((target / 'inbox').exists())
                            self.assertFalse((target / 'attachments').exists())
                        else:
                            result = b._send_message('ash', 'Long note.')
                            self.assertTrue(list((target / 'inbox').glob('*.md')))
                            if mode in ('pass', 'log_error'):
                                self.assertIn('checked against your standing orders', result)
                            elif mode == 'error':
                                self.assertIn('NOT checked', result)
                            else:
                                check.assert_not_called()
                finally:
                    f.doCleanups()


if __name__ == '__main__':
    unittest.main()

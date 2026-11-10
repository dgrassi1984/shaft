"""Frozen requests through the fake Google, real inbox files and protected web actions.
All projects, grants and notifications belong to fakes; no operator service is contacted.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest import mock

from supervisor import broker, calendar_proposals as proposals, core, messages, telegram, web, webpush
from supervisor.tests import test_calendar_write as write
from supervisor.tests import test_calendar as read
from supervisor.tests import test_web as webtest


class ProposalTest(write.WriteCase):
    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(messages, 'instance_root', return_value=self.root),
                      mock.patch.object(messages, 'SUP_DIR', self.sup)):
            patch.start()
            self.addCleanup(patch.stop)

    def propose(self, tool='calendar_move', args=None):
        text = self.ok(self.broker(), tool, self.move_args('external1') if args is None else args)
        self.assertIn("waiting for the operator's approval; you will be told in your inbox", text)
        return proposals.listing()[0]

    def decide(self, row, approve=True):
        return proposals.decide(row['id'], approve, secrets=self.secrets)

    def inbox(self):
        return '\n'.join(p.read_text() for p in (self.root / 'inbox').glob('*.md'))

    def test_frozen_request_and_exactly_one_approval(self):
        row = self.propose()
        self.assertEqual(self.cal.writes, [])
        self.assertEqual(row['state'], 'pending')
        self.assertEqual(row['instance'], 'wisp')
        self.assertEqual(row['target'], self.move_args('external1')['id'])
        self.assertEqual(row['attendees'], ['bob@example.com', 'm@evil.example'])
        self.assertIn('Partnership', row['summary'])  # original time and changed title are both reviewable
        self.assertEqual(datetime.fromisoformat(row['expires_at']) - datetime.fromisoformat(row['created_at']),
                         timedelta(hours=24))
        before = list(self.cal.requests)
        result = self.decide(row)
        self.assertEqual(result['state'], 'approved')
        self.assertEqual(self.cal.requests[len(before):][0][0], 'POST')  # token refresh, no event re-read
        self.assertFalse(any(r[0] == 'GET' for r in self.cal.requests[len(before):]))
        self.assertEqual(self.cal.writes, [('PATCH', ['calendars', read.PRIMARY, 'events', 'external1'],
                                           {'sendUpdates': 'all'}, row['request']['body'])])
        self.assertEqual(self.cal.conditions, ['"v1"'])
        self.assertIn('approved', self.inbox())
        record = self.ok(self.broker(), 'record', {'kind': 'calendar'})
        self.assertIn('"approved_by": "operator"', record)
        with self.assertRaisesRegex(ValueError, 'already approved'):
            self.decide(row)
        self.assertEqual(len(self.cal.writes), 1)

    def test_create_attendees_propose_then_approve(self):
        args = {**self.args, 'attendees': [{'email': 'guest@example.com'}]}
        row = self.propose('calendar_create', args)
        self.assertEqual(self.cal.writes, [])
        self.assertEqual(row['attendees'], ['guest@example.com'])
        args['attendees'][0]['email'] = 'changed@example.com'
        self.assertEqual(self.decide(row)['state'], 'approved')
        self.assertEqual(self.cal.writes, [('POST', ['calendars', read.PRIMARY, 'events'],
                                           {'sendUpdates': 'all'}, row['request']['body'])])

    def test_adding_attendees_to_a_move_keeps_the_existing_ones_and_sends_only_addresses(self):
        # Review of M2: a move's attendee list replaced the event's (Google's PATCH replaces arrays), so "adding" one
        # person could drop every existing invitee; and whole objects passed through any field Google accepts.
        before = [a['email'] for a in self.cal.events[read.PRIMARY]['external1']['attendees']]
        row = self.propose('calendar_move', self.move_args('external1', attendees=[
            {'email': ' new@example.com ', 'responseStatus': 'accepted', 'optional': True},
            {'email': before[0].upper()}]))                            # already there: not added twice
        sent = row['request']['body']['attendees']
        self.assertEqual([a['email'] for a in sent], before + ['new@example.com'])
        self.assertEqual(sent[-1], {'email': 'new@example.com'})       # the address, nothing else
        self.assertIn('new@example.com', row['attendees'])

    def test_adding_attendees_to_an_event_whose_list_google_cut_is_refused(self):
        self.cal.events[read.PRIMARY]['external1']['attendeesOmitted'] = True
        error = self.refused(self.broker(), 'calendar_move',
                             self.move_args('external1', attendees=[{'email': 'new@example.com'}]))
        self.assertIn('attendee list', error)
        self.assertEqual(proposals.listing(), [])
        self.assertEqual(self.cal.writes, [])

    def test_decline_sends_nothing_and_delivers_and_logs(self):
        row = self.propose()
        before = list(self.cal.requests)
        self.assertEqual(self.decide(row, False)['state'], 'declined')
        self.assertEqual(self.cal.requests, before)
        self.assertIn('declined', self.inbox())
        self.assertEqual(self.rows()[-1]['outcome'], 'declined')

    def test_expiry_lazy_and_action_sends_nothing(self):
        with mock.patch.object(proposals, 'PROPOSAL_HOURS', 0):
            self.ok(self.broker(), 'calendar_move', self.move_args('external1'))
        before = list(self.cal.requests)
        row = proposals.listing()[0]
        self.assertEqual(row['state'], 'expired')
        self.assertIn('expired', self.inbox())
        self.assertEqual(self.rows()[-1]['outcome'], 'expired')
        with self.assertRaisesRegex(ValueError, 'already expired'):
            self.decide(row)
        proposals.listing()
        self.assertEqual(len(list((self.root / 'inbox').glob('*.md'))), 1)
        self.assertEqual(self.cal.requests, before)

    def test_scheduler_expires_without_instances_even_while_paused(self):
        from supervisor import autonomy
        with mock.patch.object(proposals, 'PROPOSAL_HOURS', 0):
            self.ok(self.broker(), 'calendar_move', self.move_args('external1'))
        (self.sup / 'paused').touch()
        class Stop(Exception):
            pass
        with mock.patch.object(autonomy, 'SUP_DIR', self.sup), mock.patch.object(autonomy, 'log'), \
             mock.patch.object(autonomy, 'stop_children_on_signal'), \
             mock.patch('supervisor.version.record_service'), \
             mock.patch('supervisor.browser.startup_note', return_value=''), \
             mock.patch.object(autonomy, 'instance_names', return_value=[]), \
             mock.patch.object(autonomy, 'update_jobs', return_value=[]), \
             mock.patch.object(autonomy.time, 'sleep', side_effect=Stop):
            with self.assertRaises(Stop):
                autonomy.serve(poll_s=0)
        self.assertEqual(proposals.listing()[0]['state'], 'expired')
        self.assertIn('expired', self.inbox())
        self.assertEqual(self.cal.writes, [])

    def test_the_scheduler_tick_never_raises_and_one_bad_line_hides_nothing(self):
        # Review of M2: listing() ran unguarded in the scheduler loop; a corrupt journal line, or an expired
        # proposal whose instance is gone, raised there and would have stopped every wake, sleep and job.
        row = self.propose()
        journal = self.sup / 'calendar/proposals.jsonl'
        with journal.open('a') as f:
            f.write('{"id": "half-writ\n')
        with mock.patch.object(core, 'log') as log:
            self.assertEqual([r['id'] for r in proposals.listing()], [row['id']])   # the good one still shown
            with mock.patch.object(proposals, 'listing', side_effect=OSError('disk full')):
                proposals.expire()
                proposals.expire()                                     # said once, not every 30 s tick
        logged = [c.args[0] for c in log.call_args_list]
        self.assertTrue(any('unreadable line' in l for l in logged), logged)
        self.assertEqual(sum('disk full' in l for l in logged), 1)

    def test_an_outcome_that_cannot_reach_the_inbox_is_still_decided_and_audited(self):
        row = self.propose()
        with mock.patch.object(messages, 'deliver_background', side_effect=RuntimeError('no such instance')), \
                mock.patch.object(core, 'log') as log:
            self.assertEqual(self.decide(row, approve=False)['state'], 'declined')
        self.assertEqual(proposals.listing()[0]['state'], 'declined')
        self.assertTrue(any('no such instance' in c.args[0] for c in log.call_args_list))
        self.assertEqual(self.rows()[-1]['outcome'], 'declined')

    def test_revoked_write_and_read_grants_fail_without_google(self):
        for grants in [[{'account': 'work', 'calendar': read.PRIMARY, 'name': 'Operator'}], []]:
            self.grant_write()
            row = self.propose()
            core.set_calendar_grants('wisp', grants)
            before = list(self.cal.requests)
            self.assertEqual(self.decide(row)['state'], 'failed')
            self.assertEqual(self.cal.requests, before)
            self.assertIn('no longer granted', self.inbox())

    def test_changed_event_fails_verbatim_without_reread_or_retry(self):
        row = self.propose()
        self.cal.events[read.PRIMARY]['external1']['etag'] = '"v2"'
        before = list(self.cal.requests)
        result = self.decide(row)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['outcome'], 'ApiError: Google answered HTTP 412: Precondition Failed')
        self.assertIn(result['outcome'], self.inbox())
        self.assertFalse(any(r[0] == 'GET' for r in self.cal.requests[len(before):]))
        self.assertEqual(len(self.cal.writes), 1)
        with self.assertRaisesRegex(ValueError, 'already failed'):
            self.decide(row)

    def test_concurrent_double_approval_sends_once(self):
        row = self.propose()
        def act(_):
            try:
                return self.decide(row)['state']
            except ValueError:
                return 'already decided'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(act, range(2))), ['already decided', 'approved'])
        self.assertEqual(len(self.cal.writes), 1)

    def test_proposals_consume_cap_and_outcomes_do_not(self):
        with mock.patch.object(broker, 'CALENDAR_WRITES_PER_DAY', 1):
            row = self.propose()
            before = list(self.cal.requests)
            self.assertIn('at most 1', self.refused(self.broker(), 'calendar_create', self.args))
            self.assertEqual(self.cal.requests, before)
            self.decide(row)
            self.assertEqual(sum(r.get('submitted', False) for r in self.rows()), 1)

    def test_respond_always_proposes_and_preserves_other_responses(self):
        for response, status in [('yes', 'accepted'), ('no', 'declined'), ('maybe', 'tentative')]:
            row = self.propose('calendar_respond', {'id': self.handle(read.PRIMARY, 'external1'), 'response': response})
            self.assertEqual(len(self.cal.writes), ['yes', 'no', 'maybe'].index(response))
            attendees = row['request']['body']['attendees']
            self.assertEqual(attendees[0]['responseStatus'], status)
            self.assertEqual(attendees[1]['responseStatus'], 'accepted')
            self.assertEqual(self.decide(row)['state'], 'approved')
            self.assertEqual(self.cal.conditions[-1], '"v1"')
            self.assertEqual(self.cal.writes[-1][2], {'sendUpdates': 'all'})
            self.assertEqual(self.cal.writes[-1][3], {'attendees': attendees})

    def test_respond_grant_version_series_and_own_attendee_guards(self):
        for eid, reason in [('standup', 'never the series'), ('own1', "operator's own attendee")]:
            self.assertIn(reason, self.refused(self.broker(), 'calendar_respond',
                                              {'id': self.handle(read.PRIMARY, eid), 'response': 'yes'}))
        event = self.cal.events[read.PRIMARY]['external1']
        del event['etag']
        self.assertIn('no version', self.refused(self.broker(), 'calendar_respond',
                      {'id': self.handle(read.PRIMARY, 'external1'), 'response': 'yes'}))
        self.assertEqual(self.cal.writes, [])
        self.assertEqual(proposals.listing(), [])

    def test_telegram_link_and_notification_failures_keep_proposal(self):
        with mock.patch.object(telegram, 'tokens', return_value={'wisp': 'fake-token'}), \
             mock.patch.object(telegram, 'paired_id', return_value=123), \
             mock.patch.object(telegram, 'send') as send:
            row = self.propose()
            self.assertEqual(send.call_args.args[:2], ('fake-token', 123))
            self.assertIn(row['summary'], send.call_args.args[2])
            self.assertIn('https://shaft.test/calendar/proposals', send.call_args.args[2])
            with mock.patch.object(web, 'url', return_value='(tailscale is not running)'):
                self.propose()
                self.assertIn('No web URL is configured', send.call_args.args[2])
            send.side_effect = RuntimeError('Telegram offline')
            with mock.patch.object(webpush, 'notify', side_effect=RuntimeError('push offline')):
                row = self.propose()
            self.assertEqual(row['state'], 'pending')
            self.assertEqual(len(proposals.listing()), 3)
        self.assertEqual(self.cal.writes, [])

    def test_trial_replays_never_propose(self):
        b = self.broker()
        b.as_of = datetime.now()
        for tool, args in [('calendar_create', {**self.args, 'attendees': [{'email': 'guest@example.com'}]}),
                           ('calendar_move', self.move_args('external1')),
                           ('calendar_respond', {'id': self.handle(read.PRIMARY, 'external1'), 'response': 'yes'})]:
            self.assertIn('trial replays', self.refused(b, tool, args))
        self.assertEqual(proposals.listing(), [])
        self.assertEqual(self.cal.requests, [])


class ProposalWebTest(webtest.WebTest):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(core, 'SUP_DIR', self.sup)
        patch.start()
        self.addCleanup(patch.stop)

    def test_list_and_actions_use_csrf_and_tailnet(self):
        row = {'id': 'p1', 'instance': 'wisp', 'state': 'pending', 'summary': '<move meeting>',
               'attendees': ['guest@example.com'], 'expires_at': 'tomorrow', 'outcome': None}
        cookie = self.paired()
        with mock.patch.object(proposals, 'listing', return_value=[row]):
            page = self.req('/calendar/proposals', cookie=cookie)
        self.assertEqual(page.status, 200)
        self.assertIn(b'data-poll="/fragment/calendar-proposals"', page.body)
        with mock.patch.object(proposals, 'listing', return_value=[row, {**row, 'state': 'declined', 'outcome': 'No'}]):
            fragment = self.req('/fragment/calendar-proposals', cookie=cookie)
        self.assertEqual(fragment.status, 200)
        self.assertIn(b'Recent decisions', fragment.body)
        self.assertIn(b'declined: No', fragment.body)
        for value in [b'&lt;move meeting&gt;', b'guest@example.com', b'tomorrow',
                      b'/action/calendar-approve', b'/action/calendar-decline']:
            self.assertIn(value, page.body)
        with mock.patch.object(proposals, 'decide', return_value={**row, 'state': 'declined'}) as decide:
            for verb in ['approve', 'decline']:
                path = '/action/calendar-' + verb
                headers = {'sec-fetch-site': 'same-origin'}
                self.assertEqual(self.req(path, 'POST', cookie=cookie, body=b'id=p1', headers=headers).status, 403)
                self.assertEqual(self.req(path, 'POST', cookie=cookie, body=b'id=p1', login=None,
                                          headers={**headers, 'x-csrf-token': self.csrf(cookie)}).status, 403)
                decide.assert_not_called()
            for verb in ['approve', 'decline']:
                self.assertEqual(self.req('/action/calendar-' + verb, 'POST', cookie=cookie, body=b'id=p1',
                                 headers={'sec-fetch-site': 'same-origin', 'x-csrf-token': self.csrf(cookie)}).status, 200)
            self.assertEqual(decide.call_args_list, [mock.call('p1', True), mock.call('p1', False)])

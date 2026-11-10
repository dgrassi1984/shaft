"""Through Broker.call and the same fake Google door as the read tests.
No account or instance outside CalendarCase's temporary project is used."""
import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest import mock

from supervisor import broker, core, google, telegram, web, webpush
from supervisor.tests import test_calendar as read


class FakeWriteCalendar(read.FakeCalendar):
    def __init__(self):
        super().__init__()
        self.writes = []
        self.failure = None
        self.conditions = []
        self.before_patch = None
        for events in self.events.values():
            for event in events.values():
                event["etag"] = '"v1"'

    def __call__(self, req, timeout=0):
        if req.full_url == google.TOKEN_URL:
            return super().__call__(req, timeout)
        url = urllib.parse.urlsplit(req.full_url)
        path = url.path.removeprefix(urllib.parse.urlsplit(read.gcal.API).path).strip('/')
        parts = [urllib.parse.unquote(p) for p in path.split('/')]
        if req.get_method() in ('POST', 'PATCH'):
            body = json.loads(req.data)
            self.conditions.append(req.get_header("If-match"))
            params = dict(urllib.parse.parse_qsl(url.query))
            self.writes.append((req.get_method(), parts, params, body))
            self.requests.append((req.get_method(), path, params))
            if self.failure:
                raise self._error(req.full_url, 403, self.failure)
            cal = self.events[parts[1]]
            if req.get_method() == 'PATCH':
                if self.before_patch:
                    self.before_patch(cal[parts[3]])
                if req.get_header('If-match') != cal[parts[3]]['etag']:
                    raise self._error(req.full_url, 412, 'Precondition Failed')
            eid = parts[3] if len(parts) == 4 else 'created1'
            before = cal.get(eid, {'id': eid, 'organizer': {'email': read.ME, 'self': True}})
            cal[eid] = {**before, **body}
            return self._answer(cal[eid])
        if len(parts) == 2 and parts[0] == 'calendars':
            self.requests.append(('GET', path, {}))
            return self._answer({'timeZone': 'Europe/Rome'})
        return super().__call__(req, timeout)


class WriteCase(read.CalendarCase):
    def setUp(self):
        super().setUp()
        self.cal = FakeWriteCalendar()
        patch = mock.patch.object(google, 'urlopen', self.cal)
        patch.start()
        self.addCleanup(patch.stop)
        saved = google.account('work')
        saved['scope'] += ' ' + google.CALENDAR_EVENTS
        google._save_account('work', saved)
        for patch in (mock.patch.object(telegram, 'tokens', return_value={}),
                      mock.patch.object(telegram, 'paired_id', return_value=None),
                      mock.patch.object(telegram, 'send'), mock.patch.object(webpush, 'notify'),
                      mock.patch.object(web, 'url', return_value='https://shaft.test')):
            patch.start()
            self.addCleanup(patch.stop)
        self.grant_write()
        now = datetime.now().astimezone() + timedelta(days=1)
        self.args = {'calendar': 'work/Operator', 'title': 'Focus', 'start': now.isoformat(),
                     'end': (now + timedelta(hours=1)).isoformat(), 'timezone': 'Europe/Rome'}

    def grant_write(self):
        core.set_calendar_grants('wisp', [{'account': 'work', 'calendar': read.PRIMARY, 'name': 'Operator', 'write': True}])

    def move_args(self, eid='own1', **extra):
        return {k: v for k, v in {**self.args, 'id': self.handle(read.PRIMARY, eid), **extra}.items() if k != 'calendar'}

    def rows(self):
        return [json.loads(l) for l in (self.sup / 'calendar/wisp.jsonl').read_text().splitlines()]

    def deny(self, tool='calendar_create', args=None, contains=''):
        before = len(self.cal.writes)
        error = self.refused(self.broker(), tool, self.args if args is None else args)
        self.assertIn(contains, error)
        self.assertEqual(len(self.cal.writes), before)
        return error


class WriteTest(WriteCase):
    def test_create_move_and_record(self):
        b = self.broker()
        self.ok(b, 'calendar_create', {**self.args, 'location': 'Office', 'description': 'Quiet'})
        self.ok(b, 'calendar_move', self.move_args())
        self.assertEqual([r[0] for r in self.cal.writes], ['POST', 'PATCH'])
        for method, parts, params, body in self.cal.writes:
            self.assertEqual(params, {'sendUpdates': 'none'})
            self.assertEqual(parts[:3], ['calendars', read.PRIMARY, 'events'])
            self.assertEqual(body['start']['dateTime'], self.args['start'])
            self.assertEqual(body['end']['dateTime'], self.args['end'])
            self.assertEqual(body['summary'], 'Focus')
        self.assertEqual(self.cal.writes[0][3]['location'], 'Office')
        self.assertEqual(self.cal.writes[0][3]['description'], 'Quiet')
        rows = [json.loads(l) for l in self.ok(b, 'record', {'kind': 'calendar'}).splitlines()]
        self.assertEqual(rows, self.rows())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['before']['title'], 'Planning')
        self.assertEqual(rows[1]['id'], self.handle(read.PRIMARY, 'own1'))
        self.assertEqual(self.cal.conditions, [None, '"v1"'])
        self.assertEqual(rows[1]['after']['title'], 'Focus')
        self.assertEqual(rows[0]['answer']['id'], 'created1')
        self.assertEqual(rows[0]['outcome'], 'ok')
        self.assertIn('calendar', broker.SPECS['record']['function']['parameters']['properties']['kind']['enum'])

    def test_read_grant_and_revoke_mid_run(self):
        b = self.broker()
        core.set_calendar_grants('wisp', [{'account': 'work', 'calendar': read.PRIMARY, 'name': 'Operator'}])
        for tool, args in [('calendar_create', self.args), ('calendar_move', self.move_args()),
                           ('calendar_respond', {'id': self.handle(read.PRIMARY, 'external1'), 'response': 'yes'})]:
            self.assertIn('no calendar granted for writing', self.refused(b, tool, args))
        self.assertEqual(self.cal.requests, [])
        self.assertFalse(set(broker.CALENDAR_WRITE_TOOLS) & {s['function']['name'] for s in b.tool_specs()})
        self.grant_write()
        self.assertTrue(set(broker.CALENDAR_WRITE_TOOLS) <= {s['function']['name'] for s in b.tool_specs()})
        self.assertIn('[writable]', b.calendar_env())
        core.set_calendar_grants('wisp', [])
        self.deny(contains='no calendar granted')
        self.assertEqual(len(self.rows()), 4)

    def test_missing_write_scope_names_exact_login_without_google(self):
        saved = google.account('work')
        saved['scope'] = google.CALENDAR
        google._save_account('work', saved)
        self.deny(contains=f'`shaft google login work {read.ME}` again')
        self.assertEqual(self.cal.requests, [])

    def test_forged_other_calendar_and_raw_ids_refused_without_google(self):
        for target in ['work/deadbeef/own1', self.handle(read.TEAM, 'secret1'), read.PRIMARY,
                       'work/../../x', self.handle(read.PRIMARY, '../x')]:
            self.deny('calendar_move', self.move_args(id=target), 'not granted for writing')
        self.deny(args={**self.args, 'calendar': 'work/Team'}, contains='not granted for writing')
        self.deny(args={**self.args, 'calendar': read.PRIMARY}, contains='not granted for writing')
        self.assertEqual(self.cal.requests, [])

    def test_invalid_times_refused_without_google(self):
        now = datetime.now().astimezone()
        cases = [{'end': self.args['start']}, {'start': self.args['end'], 'end': self.args['start']},
                 {'start': (now + timedelta(days=93)).isoformat(), 'end': (now + timedelta(days=94)).isoformat()},
                 {'start': (now - timedelta(days=94)).isoformat(), 'end': (now - timedelta(days=93)).isoformat()},
                 {'start': 'oops'}, {'timezone': 'Not/AZone'}]
        for changes in cases:
            self.deny(args={**self.args, **changes})
        self.assertEqual(self.cal.requests, [])

    def test_adding_attendees_proposes_without_google_write(self):
        for attendees in [[{'email': 'other@example.com'}], [{'email': read.ME}]]:
            for tool, args in [('calendar_create', {**self.args, 'attendees': attendees}),
                               ('calendar_move', self.move_args(attendees=attendees))]:
                self.assertIn('proposed (id ', self.ok(self.broker(), tool, args))
        self.assertEqual(self.cal.writes, [])

    def test_other_attendee_invitation_and_omitted_attendees_propose(self):
        self.assertIn('proposed', self.ok(self.broker(), 'calendar_move', self.move_args('external1')))
        own = self.cal.events[read.PRIMARY]['own1']
        for fields in [{'attendees': [{'email': 'other@example.com', 'self': True}]},
                       {'attendees': [{'email': read.ME}, {'email': 'other@example.com'}]},
                       {'attendeesOmitted': True}]:
            self.cal.events[read.PRIMARY]['own1'] = {**own, **fields}
            self.assertIn('proposed', self.ok(self.broker(), 'calendar_move', self.move_args()))
        self.assertEqual(self.cal.writes, [])
        self.assertIn('proposal', self.rows()[0]['answer'])
        self.assertIn('[written by Mallory', self.rows()[0]['written_by'])

    def test_operator_address_casefold_and_calendar_timezone(self):
        self.cal.events[read.PRIMARY]['own1']['attendees'] = [{'email': read.ME.upper()}]
        day = (datetime.now() + timedelta(days=2)).date().isoformat()
        args = self.move_args(start=day + 'T09:00:00', end=day + 'T10:00:00')
        del args['timezone']
        self.ok(self.broker(), 'calendar_move', args)
        self.assertEqual(self.cal.writes[0][3]['start']['timeZone'], 'Europe/Rome')
        self.assertEqual(datetime.fromisoformat(self.cal.writes[0][3]['start']['dateTime']).hour, 9)

    def test_mixed_offset_and_naive_time_uses_calendar_zone_before_comparison(self):
        day = (datetime.now() + timedelta(days=2)).date().isoformat()
        args = {**self.args, 'start': day + 'T09:00:00', 'end': day + 'T08:30:00Z'}
        del args['timezone']
        self.ok(self.broker(), 'calendar_create', args)
        body = self.cal.writes[0][3]
        self.assertLess(datetime.fromisoformat(body['start']['dateTime']),
                        datetime.fromisoformat(body['end']['dateTime']))

    def test_recurring_occurrence_only(self):
        original = dict(self.cal.events[read.PRIMARY]['standup'])
        self.ok(self.broker(), 'calendar_move', self.move_args('standup_2026-10-14'))
        self.assertEqual(self.cal.writes[0][1][-1], 'standup_2026-10-14')
        self.assertEqual(self.cal.events[read.PRIMARY]['standup'], original)
        self.deny('calendar_move', self.move_args('standup'), 'never the series')
        self.deny('calendar_move', {**self.move_args(), 'recurrence': ['RRULE:FREQ=DAILY']}, 'bad arguments')

    def test_daily_cap_across_runs_before_google(self):
        with mock.patch.object(broker, 'CALENDAR_WRITES_PER_DAY', 1):
            self.ok(self.broker(), 'calendar_create', self.args)
            before = list(self.cal.requests)
            self.deny(contains='at most 1 calendar writes per day')
            self.assertEqual(self.cal.requests, before)
        self.assertEqual(len(self.rows()), 2)

    def test_per_run_cap_counts_reads_and_writes(self):
        b = self.broker()
        b.counts['calendar_events'] = broker.CALENDAR_CALLS_PER_RUN
        self.assertIn('calendar calls per run', self.refused(b, 'calendar_create', self.args))
        self.assertEqual(self.cal.requests, [])
        self.assertEqual(self.rows()[0]['outcome'], 'refused')

    def test_google_refusal_verbatim_and_counted(self):
        self.cal.failure = 'Forbidden: exact Google wording <with punctuation>.'
        error = self.refused(self.broker(), 'calendar_create', self.args)
        self.assertEqual(error, 'ApiError: Google answered HTTP 403: ' + self.cal.failure)
        self.assertEqual(self.rows()[0]['answer'], error)
        self.assertTrue(self.rows()[0]['submitted'])

    def test_attendee_added_between_read_and_patch_is_not_moved(self):
        old_start = dict(self.cal.events[read.PRIMARY]['own1']['start'])
        def change(event):
            event.update(etag='"v2"', attendees=[{'email': 'other@example.com'}])
        self.cal.before_patch = change
        error = self.refused(self.broker(), 'calendar_move', self.move_args())
        self.assertEqual(error, 'ApiError: Google answered HTTP 412: Precondition Failed')
        self.assertEqual(self.cal.events[read.PRIMARY]['own1']['start'], old_start)

    def test_disabled_write_hidden_refused_and_logged(self):
        b = self.broker()
        b.disabled.update(broker.CALENDAR_WRITE_TOOLS)
        self.assertFalse(set(broker.CALENDAR_WRITE_TOOLS) & {s['function']['name'] for s in b.tool_specs()})
        self.assertIn('switched it off', self.refused(b, 'calendar_create', self.args))
        self.assertEqual(self.cal.requests, [])
        self.assertEqual(self.rows()[0]['outcome'], 'refused')
        self.assertIn('calendar_respond', broker.SPECS)

    def test_missing_event_version_and_wrong_calendar_event_never_written(self):
        del self.cal.events[read.PRIMARY]['own1']['etag']
        self.deny('calendar_move', self.move_args(), 'no version for a conditional move')
        self.deny('calendar_move', self.move_args('secret1'), 'not visible')

    def test_cap_serializes_concurrent_runs_and_zero_disables(self):
        first, second = self.broker(), self.broker()
        with mock.patch.object(broker, 'CALENDAR_WRITES_PER_DAY', 1):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda b: b.call('calendar_create', self.args), [first, second]))
        self.assertEqual(sorted(r['ok'] for r in results), [False, True])
        self.assertEqual(len(self.cal.writes), 1)
        self.assertEqual(len(self.rows()), 2)
        with mock.patch.object(broker, 'CALENDAR_WRITES_PER_DAY', 0):
            self.deny(contains='at most 0 calendar writes per day')

    def test_write_scope_login_includes_nondefault_client(self):
        saved = google.account('work')
        saved.update(scope=google.CALENDAR, client='personal')
        google._save_account('work', saved)
        self.deny(contains=f'`shaft google login work {read.ME} --client personal` again')
        self.assertEqual(self.cal.requests, [])

    def test_post_and_patch_use_get_refresh_and_quota_handling(self):
        for method in ('post', 'patch'):
            for status, message in [(401, 'Invalid credentials'), (429, 'Rate Limit Exceeded')]:
                with self.subTest(method=method, status=status):
                    acct = google.Account('work', self.secrets)
                    attempts = []
                    def flaky(req, timeout=0):
                        if req.full_url == google.TOKEN_URL:
                            return self.cal(req, timeout)
                        attempts.append((req.get_method(), json.loads(req.data)))
                        if len(attempts) == 1:
                            raise self.cal._error(req.full_url, status, message)
                        return self.cal._answer({'id': 'ok'})
                    with mock.patch.object(google, 'urlopen', flaky), mock.patch.object(google, 'sleep') as sleep:
                        answer = getattr(acct, method)(read.gcal.API + '/calendars/x/events', {'summary': 'Focus'},
                                                       sendUpdates='none')
                    self.assertEqual(answer, {'id': 'ok'})
                    self.assertEqual(attempts, [(method.upper(), {'summary': 'Focus'})] * 2)
                    self.assertEqual(sleep.call_count, int(status == 429))

    def test_replay_refused_before_google(self):
        b = self.broker()
        b.as_of = datetime.now()
        self.assertIn('trial replays', self.refused(b, 'calendar_create', self.args))
        self.assertEqual(self.cal.requests, [])


class WriteCliTest(read.CalendarCase):
    grant = False
    cli = read.CliTest.cli

    def test_write_grant_implies_read_listing_revoke_write_and_off(self):
        saved = google.account('work')
        saved['scope'] += ' ' + google.CALENDAR_EVENTS
        google._save_account('work', saved)
        code, out = self.cli('calendar', 'wisp', 'grant-write', 'work', 'primary')
        self.assertEqual(code, 0, out)
        self.assertIn('[writable]', out)
        self.assertTrue(core.calendar_grants('wisp')[0]['write'])
        on_disk = json.loads((self.sup / 'tools/wisp.json').read_text())
        self.assertTrue(on_disk['calendar'][0]['write'])
        core.set_tool('wisp', 'decide', False)
        self.assertTrue(core.calendar_grants('wisp')[0]['write'])
        self.assertIn('[writable]', self.cli('calendar')[1])
        self.cli('calendar', 'wisp', 'grant', 'work', 'primary')
        self.assertTrue(core.calendar_grants('wisp')[0]['write'])
        self.assertEqual(len(core.calendar_grants('wisp')), 1)
        self.assertEqual(self.cli('calendar', 'wisp', 'revoke-write', 'work', 'primary')[0], 0)
        self.assertNotIn('write', core.calendar_grants('wisp')[0])
        self.cli('calendar', 'wisp', 'grant-write', 'work', 'primary')
        self.assertEqual(self.cli('calendar', 'wisp', 'off')[0], 0)
        self.assertEqual(core.calendar_grants('wisp'), [])

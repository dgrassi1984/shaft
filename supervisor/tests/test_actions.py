"""#142: conservative action classes through the broker, shared proposal queue, CLI and web panel.
All credentials, calendars, mail and instance folders are temporary fakes.
"""
import copy
import io
import json
import shutil
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime
from unittest import mock

from supervisor import actions, broker, calendar_proposals as proposals, channel, core, google, messages, web_actions, web_views
from supervisor.tests import test_calendar_write as write, test_calendar as read, test_mail as mail


class DefaultSelectionTest(unittest.TestCase):
    """Bug: SHAFT_ACTION_INSTANCES defaulted to one operator's live instance name, so a fresh install carried a
    stranger's instance in its code and docs. The default selects nobody; an operator names instances in settings."""

    def test_the_code_default_names_no_instance(self):
        self.assertEqual(core.KNOBS['SHAFT_ACTION_INSTANCES'].default, '')
        self.assertEqual(actions._names(''), ())


class ClassificationTest(unittest.TestCase):
    """#142: each existing operator tool has an explicit classification, including reads."""

    def test_table(self):
        self.assertEqual(set(actions.OPERATOR_TOOLS),
                         set(broker.MAIL_ALL_TOOLS + broker.CALENDAR_TOOLS + broker.DRIVE_TOOLS + broker.GCHAT_TOOLS))
        for tool in broker.MAIL_TOOLS + broker.DRIVE_TOOLS + broker.GCHAT_TOOLS + ('calendar_events', 'calendar_event'):
            self.assertIsNone(actions.classify(tool, {}))
        self.assertIsNone(actions.classify('send_message', {'to': 'operator'}))
        self.assertIsNone(actions.classify('send_message', {'to': 'shaft'}))
        self.assertEqual(actions.classify('calendar_create', {}), 'reversible')
        self.assertEqual(actions.classify('calendar_move', {}, event={}, address=read.ME), 'reversible')
        for tool in ('calendar_create', 'calendar_move'):
            self.assertEqual(actions.classify(tool, {'attendees': [{'email': read.ME}]}), 'staged')
        for event in ({'attendees': [{'email': 'someone@example.com'}]}, {'attendeesOmitted': True},
                      {'organizer': {'email': 'other@example.com'}}):
            self.assertEqual(actions.classify('calendar_move', {}, event=event, address=read.ME), 'staged')
        self.assertEqual(actions.classify('calendar_respond', {}), 'staged')
        self.assertIsNone(actions.classify('http_request', {'method': 'DELETE'}))  # instance-owned credentials


class ActionTest(write.WriteCase):
    """#142: live broker calls, class state and shared approval decisions against fake Google."""

    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(actions, 'ACTION_INSTANCES', ('wisp',)),
                      mock.patch.object(core, 'PROJECT', self.project),
                      mock.patch.object(messages, 'deliver_background')):
            patch.start()
            self.addCleanup(patch.stop)

    def last_action(self):
        return [r for r in actions.status('wisp')['history'] if r['kind'] == 'call'][-1]

    def test_direct_staged_irreversible_and_restore(self):
        self.ok(self.broker(), 'calendar_move', self.move_args())
        self.assertEqual(self.cal.writes[-1][2], {'sendUpdates': 'none'})
        action = self.last_action()
        self.assertEqual((action['class'], action['mode']), ('reversible', 'direct'))
        actions.overreach('wisp', action['id'])
        self.ok(self.broker(), 'calendar_move', self.move_args())
        self.assertEqual(len(self.cal.writes), 1)
        row = proposals.listing()[0]
        self.assertEqual(row['action_class'], 'reversible')
        self.assertEqual(row['request']['sendUpdates'], 'none')
        self.assertEqual(proposals.decide(row['id'], True, secrets=self.secrets)['state'], 'approved')
        self.assertEqual(self.cal.writes[-1][2], {'sendUpdates': 'none'})
        actions.restore('wisp', 'reversible')
        actions.overreach('wisp', action['id'])  # the same incident cannot undo a restore
        self.assertEqual(actions.status('wisp')['classes']['reversible'], 'direct')
        self.ok(self.broker(), 'calendar_create', self.args)
        self.assertEqual(len(self.cal.writes), 3)
        for _ in range(2):
            self.assertEqual(actions.begin('wisp', 'future-send', 'irreversible')['mode'], 'staged')
            actions.restore('wisp', 'irreversible')
        action = self.last_action()
        actions.overreach('wisp', action['id'])
        self.assertEqual(actions.begin('wisp', 'future-send', 'irreversible')['mode'], 'refused')
        actions.restore('wisp', 'irreversible')
        self.assertEqual(actions.begin('wisp', 'future-send', 'irreversible')['mode'], 'staged')

    def test_staged_rejection_demotes_and_restore(self):
        self.ok(self.broker(), 'calendar_move', self.move_args('external1'))
        row = proposals.listing()[0]
        self.assertEqual(row['action_class'], 'staged')
        proposals.reject(row['id'], 'over-reach')
        self.assertEqual(actions.status('wisp')['classes']['staged'], 'refused')
        self.assertIn('refused after over-reach', self.refused(self.broker(), 'calendar_move', self.move_args('external1')))
        self.assertEqual(len(proposals.listing()), 1)
        self.assertEqual(self.cal.writes, [])
        actions.restore('wisp', 'staged')
        self.ok(self.broker(), 'calendar_move', self.move_args('external1'))
        self.assertEqual(len(proposals.listing()), 2)

    def test_ordinary_decline_and_bad_arguments_do_not_demote(self):
        self.ok(self.broker(), 'calendar_move', self.move_args('external1'))
        proposals.reject(proposals.listing()[0]['id'], 'wrong time')
        self.refused(self.broker(), 'calendar_create', {**self.args, 'start': 'oops'})
        self.assertEqual(actions.status('wisp')['classes'],
                         {'reversible': 'direct', 'staged': 'staged', 'irreversible': 'staged'})

    def test_pending_proposal_cannot_bypass_later_demotion(self):
        self.ok(self.broker(), 'calendar_move', self.move_args('external1'))
        row = proposals.listing()[0]
        actions.overreach('wisp', row['action'])
        answer = proposals.decide(row['id'], True, secrets=self.secrets)
        self.assertEqual(answer['state'], 'failed')
        self.assertIn('action class is refused', answer['outcome'])
        self.assertEqual(self.cal.writes, [])

    def test_missing_write_entitlement_logs_without_demotion(self):
        before = actions.status('wisp')['classes']
        for grants in ([], [{'account': 'work', 'calendar': read.PRIMARY, 'name': 'Operator'}]):
            core.set_calendar_grants('wisp', grants)
            self.assertIn('no calendar granted', self.refused(self.broker(), 'calendar_create', self.args))
            self.assertEqual(actions.status('wisp')['classes'], before)
            self.assertEqual(self.last_action()['mode'], 'refused')
            self.assertIn('no calendar granted', self.last_action()['reason'])
        self.assertIn('class=reversible', (self.sup / 'broker.log').read_text())
        self.grant_write()
        self.ok(self.broker(), 'calendar_create', self.args)
        self.assertEqual(len(self.cal.writes), 1)
        self.assertEqual(proposals.listing(), [])

    def test_disabled_and_class_refusals_log_without_demotion(self):
        b = self.broker()
        b.disabled.add('calendar_create')
        before = actions.status('wisp')['classes']
        self.refused(b, 'calendar_create', self.args)
        self.assertEqual(actions.status('wisp')['classes'], before)
        self.assertIn('switched it off', self.last_action()['reason'])
        self.ok(self.broker(), 'calendar_move', self.move_args('external1'))
        proposals.reject(proposals.listing()[0]['id'], 'over-reach')
        before = actions.status('wisp')
        self.refused(self.broker(), 'calendar_move', self.move_args('external1'))
        after = actions.status('wisp')
        self.assertEqual(after['classes'], before['classes'])
        self.assertEqual(len(after['history']), len(before['history']) + 1)
        self.assertEqual(self.last_action()['mode'], 'refused')
        self.assertIn('class entitlement refused', self.last_action()['reason'])

    def test_messages_match_inside_and_outside_setting_after_demotion(self):
        """#142: instance messages are never operator actions, even after operator classes are demoted."""
        for tool, action_class in [('calendar_create', 'reversible'), ('calendar_respond', 'staged')]:
            action = actions.begin('wisp', tool, action_class)
            actions.overreach('wisp', action['id'])
        history = actions.status('wisp')['history']
        (self.root / 'notes').mkdir()
        (self.root / 'notes/a.txt').write_bytes(b'original attachment')
        recipient = self.project / 'instances/shaft'
        (recipient / 'shaft').mkdir(parents=True)
        destinations = [self.sup / 'operator-inbox', recipient / 'inbox', recipient / 'attachments']
        log = self.sup / messages.LOG_NAME
        with mock.patch.object(broker, 'datetime', wraps=datetime) as clock, \
                mock.patch.object(messages, 'PROJECT', self.project), mock.patch.object(messages, 'SUP_DIR', self.sup), \
                mock.patch.object(channel, 'route', return_value=('web', 'test')), mock.patch.object(broker, 'notify'), \
                mock.patch.object(broker.Broker, '_answer_comments'):
            clock.now.return_value = datetime(2026, 10, 28, 12)
            for to in ('operator', 'shaft'):
                results = []
                for enabled in ((), ('wisp',)):
                    with mock.patch.object(actions, 'ACTION_INSTANCES', enabled):
                        response = self.broker().call('send_message',
                            {'to': to, 'text': 'Please review', 'attachments': ['notes/a.txt']})
                    self.assertTrue(response['ok'], response)
                    files = {str(p.relative_to(self.project)): p.read_bytes()
                             for folder in destinations for p in folder.rglob('*') if p.is_file()}
                    self.assertEqual(len(files), 2)  # delivered inbox text and the attachment
                    self.assertIn(b'original attachment', files.values())
                    results.append((json.dumps(response).encode(), files, log.read_bytes()))
                    for folder in destinations:
                        if folder.exists():
                            shutil.rmtree(folder)
                    log.unlink()
                self.assertEqual(results[0], results[1])
        self.assertEqual(actions.status('wisp')['history'], history)
        self.assertEqual(proposals.listing(), [])

    def test_web_reject_and_cli_state_restore_and_approval(self):
        from supervisor import __main__ as cli
        self.ok(self.broker(), 'calendar_create', {**self.args, 'attendees': [{'email': 'guest@example.com'}]})
        row = proposals.listing()[0]
        self.assertIn('calendar-overreach', str(web_views.calendar_proposals_body([row])))
        self.assertEqual(web_actions.run('calendar-overreach', {'id': row['id']}, {}, [], [])[0], 200)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(['actions', 'wisp']), 0)
        self.assertEqual(json.loads(out.getvalue())['classes']['staged'], 'refused')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['actions', 'wisp', 'restore', 'staged']), 0)
        self.ok(self.broker(), 'calendar_create', {**self.args, 'attendees': [{'email': 'guest@example.com'}]})
        row = proposals.listing()[0]
        with mock.patch.object(core, 'secrets_env', return_value=self.secrets), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['actions', 'wisp', 'approve', row['id']]), 0)
        self.assertEqual(len(self.cal.writes), 1)

    def test_concurrent_operator_decisions_demote_once(self):
        action = actions.begin('wisp', 'calendar_create', 'reversible')
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: actions.overreach('wisp', action['id']), range(2)))
        state = actions.status('wisp')
        self.assertEqual(state['classes']['reversible'], 'staged')
        self.assertEqual(sum(r['kind'] == 'overreach' for r in state['history']), 1)

    def test_outside_setting_calendar_move_and_mail_read_match_legacy_bytes(self):
        with mock.patch.object(actions, 'ACTION_INSTANCES', ('tundra',)):
            original = copy.deepcopy(self.cal.events)
            old = self.broker()
            with mock.patch.object(old, '_action_route', return_value=None):
                expected = old.call('calendar_move', self.move_args())
            writes = copy.deepcopy(self.cal.writes)
            self.cal.events, self.cal.writes = original, []
            self.assertEqual(json.dumps(self.broker().call('calendar_move', self.move_args())), json.dumps(expected))
            self.assertEqual(self.cal.writes, writes)
            gmail = mail.FakeGmail()
            core.set_mail_grants('wisp', [{'account': 'work', 'label': 'AssistantAccess'}])
            with mock.patch.object(google, 'urlopen', gmail):
                old = self.broker()
                with mock.patch.object(old, '_action_route', return_value=None):
                    expected = old.call('mail_read', {'id': 'work/aaa111'})
                actual = self.broker().call('mail_read', {'id': 'work/aaa111'})
            self.assertTrue(actual['ok'], actual)
            self.assertEqual(json.dumps(actual), json.dumps(expected))
            self.assertFalse((self.sup / 'actions/wisp.jsonl').exists())


class RunNowInputTest(unittest.TestCase):
    """The setup routes hand a secret to a CLI verb on its stdin (`input`), never in argv or a log line; the JSON verbs
    answer through run_json, one parsed object per stdout line. A real subprocess stands in for the CLI."""

    SECRET = 'sk-never-in-a-log-4f1d9c'

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.sup = Path(tempfile.mkdtemp(prefix='shaft-actions-'))
        self.addCleanup(shutil.rmtree, self.sup, True)
        for p in (mock.patch.object(core, 'SUP_DIR', self.sup), mock.patch.object(actions, 'SUP_DIR', self.sup)):
            p.start()
            self.addCleanup(p.stop)

    def cli(self, script):
        import sys
        return mock.patch.object(actions, '_cli', lambda args: [sys.executable, '-c', script, *args])

    def logged(self):
        path = self.sup / 'supervisor.log'
        return path.read_text() if path.exists() else ''

    def test_run_now_passes_input_on_stdin_and_never_logs_it(self):
        echo = 'import sys; line = sys.stdin.readline().strip(); print("got", len(line), "chars")'
        with self.cli(echo):
            ok, last = actions.run_now(['secrets', 'set', 'X'], who='app mac', input=self.SECRET + '\n')
        self.assertTrue(ok)
        self.assertEqual(last, f'got {len(self.SECRET)} chars')
        self.assertIn('app mac: operator ran shaft secrets set X', self.logged())
        self.assertNotIn(self.SECRET, self.logged())

    def test_run_now_without_input_keeps_todays_stdin(self):
        # input=None changes nothing for the existing callers: no input and no stdin argument reach subprocess.run
        with self.cli('print("ok")'), mock.patch.object(actions.subprocess, 'run', wraps=actions.subprocess.run) as run:
            ok, last = actions.run_now(['x'])
        self.assertEqual((ok, last), (True, 'ok'))
        self.assertIsNone(run.call_args.kwargs.get('input'))
        self.assertNotIn('stdin', run.call_args.kwargs)

    def test_run_json_parses_every_line(self):
        script = 'import json; print(json.dumps({"ok": True, "model": "a"})); print(json.dumps({"ok": False, "model": "b"}))'
        with self.cli(script):
            code, rows = actions.run_json(['provider', 'check', 'custom', '--json'], who='app mac')
        self.assertEqual(code, 0)
        self.assertEqual(rows, [{'ok': True, 'model': 'a'}, {'ok': False, 'model': 'b'}])

    def test_run_json_passes_input_and_keeps_the_exit_code(self):
        script = ('import json, sys; v = sys.stdin.readline().strip(); '
                  'print(json.dumps({"ok": False, "error": "no " + str(len(v))})); sys.exit(1)')
        with self.cli(script):
            code, rows = actions.run_json(['secrets', 'set', 'X', '--json'], input=self.SECRET + '\n')
        self.assertEqual(code, 1)
        self.assertEqual(rows, [{'ok': False, 'error': f'no {len(self.SECRET)}'}])
        self.assertNotIn(self.SECRET, self.logged())

    def test_run_json_a_line_that_is_not_json_is_a_failure_with_its_text(self):
        with self.cli('print("Traceback: boom")'):
            code, rows = actions.run_json(['new', 'x', '--json'])
        self.assertNotEqual(code, 0)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['ok'])
        self.assertIn('Traceback: boom', rows[0]['error'])

    def test_run_json_no_output_names_stderr_or_the_exit(self):
        with self.cli('import sys; print("bad flag", file=sys.stderr); sys.exit(2)'):
            code, rows = actions.run_json(['new'])
        self.assertEqual(code, 2)
        self.assertEqual(rows, [{'ok': False, 'error': 'bad flag'}])

    def test_run_json_timeout_is_a_failure(self):
        with self.cli('import time; time.sleep(5)'):
            code, rows = actions.run_json(['provider', 'check', 'custom'], timeout=0.5)
        self.assertNotEqual(code, 0)
        self.assertIn('still running after', rows[0]['error'])

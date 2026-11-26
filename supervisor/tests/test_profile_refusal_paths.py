"""Profile lookup refusals are infrastructure, never evidence against changed code."""
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import broker, core, gate, jobs, runner, sandbox

MESSAGE = 'cannot build sandbox: /usr/bin/getconf DARWIN_USER_TEMP_DIR failed: lookup failed'


class ProfileRefusalPathsTest(unittest.TestCase):
    def test_gate_refusal_is_baseline_and_runs_no_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(gate, 'committed_tree', return_value=(root, 'test')), \
                 mock.patch.object(sandbox, 'user_folder', side_effect=runner.RunRefused(MESSAGE)), \
                 mock.patch.object(gate, '_supervisor_tests') as checks, \
                 mock.patch.object(gate, '_log') as log:
                ok, output = gate.run_tests(root, root)
            self.assertFalse(ok)
            self.assertEqual(output.cause, 'baseline')
            self.assertTrue(output.refused)
            self.assertIn(MESSAGE, output.why)
            checks.assert_not_called()
            self.assertIn(MESSAGE, log.call_args.args[0])

    def test_settlement_leaves_refused_change_unsettled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'shaft').mkdir()
            changed = root / 'shaft/example.py'
            changed.write_text('changed = True\n')
            before = {'code': {'shaft/example.py': b'changed = False\n'}, 'journal': '', 'core': ''}
            with mock.patch.object(runner, 'enforce_journal'), mock.patch.object(runner, 'enforce_core'), \
                 mock.patch.object(runner, 'log'), mock.patch.object(runner, 'journal_note'), \
                 mock.patch.object(runner.protected, 'copy', return_value=contextlib.nullcontext(root)), \
                 mock.patch.object(gate, 'committed_tree', return_value=(root, 'test')), \
                 mock.patch.object(sandbox, 'user_folder', side_effect=runner.RunRefused(MESSAGE)), \
                 mock.patch.object(gate, '_log'), mock.patch.object(runner, '_put_back') as restore, \
                 mock.patch.object(runner, '_hold') as hold, \
                 mock.patch('supervisor.metrics.start_probation') as probation:
                with self.assertRaisesRegex(runner.RunRefused, 'getconf.*lookup failed'):
                    runner._settle('example', root, 'live', before, [])
            self.assertEqual(changed.read_text(), 'changed = True\n')
            restore.assert_not_called()
            hold.assert_not_called()
            probation.assert_not_called()

    def test_job_refusal_stops_broker_clears_running_and_keeps_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = {'plugin': 'example', 'long': False, 'hash': 'test', 'args': {}}
            from supervisor import __main__ as cli
            with mock.patch.object(cli, 'instance_root', return_value=root), \
                 mock.patch.object(jobs, 'instance_root', return_value=root), \
                 mock.patch.object(jobs, 'declared', return_value=([job], [])), \
                 mock.patch.object(jobs, 'entry', return_value={}), \
                 mock.patch.object(jobs, '_update') as update, mock.patch.object(jobs, '_log_row') as row, \
                 mock.patch.object(jobs, 'log') as log, mock.patch.object(core, 'secrets_env', return_value={}), \
                 mock.patch.object(broker, 'Broker') as factory, \
                 mock.patch.object(sandbox, 'user_folder', side_effect=runner.RunRefused(MESSAGE)), \
                 mock.patch.object(jobs.subprocess, 'Popen') as launch:
                instance = factory.return_value
                instance.shared, instance.env_root = None, root
                instance.egress.port, instance.port = 1234, 5678
                self.assertEqual(cli.main(['job', 'run', 'example', 'example']), runner.REFUSED)
            instance.stop.assert_called_once()
            launch.assert_not_called()
            self.assertIn(MESSAGE, str(log.call_args_list))
            self.assertEqual(row.call_args.args[1]['refused'], MESSAGE)
            self.assertTrue(any(c.kwargs.get('status') == 'refused' and c.kwargs.get('running') is None
                                for c in update.call_args_list))
            self.assertFalse((root / '.jobs/example/.job.sb').exists())
            state = jobs._entry_then([row.call_args.args[1]], 'example', 'test')
            self.assertEqual(state['fails'], 0)
            self.assertEqual(state['status'], 'refused')
            self.assertEqual(state['last_error'], MESSAGE)


class EntryPointRefusalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for patch in (mock.patch.object(sandbox, 'user_folder', side_effect=runner.RunRefused(MESSAGE)),
                      mock.patch.object(core, 'SUP_DIR', self.root / 'state'),
                      mock.patch.object(runner, 'log'), mock.patch.object(gate, '_log'),
                      mock.patch('sys.stderr', new_callable=io.StringIO)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_broker_run_returns_tool_error_and_alert(self):
        b = broker.Broker('example', self.root, {}, self.root / 'broker.log', root=self.root)
        with mock.patch.object(b, '_log'), mock.patch.object(runner, '_refused') as alert, \
             mock.patch('tempfile.mkstemp') as profile_file, mock.patch.object(broker.subprocess, 'Popen') as launch:
            answer = b.call('run', {'command': 'echo example'})
        self.assertFalse(answer['ok'])
        self.assertIn(MESSAGE, answer['error'])
        alert.assert_called_once()
        profile_file.assert_not_called()
        launch.assert_not_called()

    def test_plan_startup_translates_refusal_and_removes_scratch(self):
        from supervisor import codexcli, grokcli, planchannel
        for backend in (codexcli, grokcli):
            with self.subTest(backend=backend.__name__):
                home = self.root / backend.__name__
                home.mkdir()
                (home / 'auth.json').write_text('{}')
                channel = backend.Channel('example', 'example', mock.Mock())
                prune = '_prune_logs' if backend is codexcli else '_prune_sessions'
                with mock.patch.object(backend, 'HOME', str(home)), mock.patch.object(backend, prune), \
                     mock.patch.object(backend.subprocess, 'Popen') as launch, \
                     mock.patch.object(runner, '_refused') as alert:
                    with self.assertRaisesRegex(planchannel.ChannelFailure, 'getconf.*lookup failed'):
                        channel._start('example/model', None)
                self.assertFalse(channel._folder.exists())
                launch.assert_not_called()
                alert.assert_called_once()

    def test_battery_exam_returns_refusal_without_launching(self):
        from supervisor import battery
        b = mock.Mock(port=1234, env_root=None)
        b.egress.port = 5678
        with mock.patch.object(battery, 'base', return_value=self.root), \
             mock.patch.object(battery.subprocess, 'Popen') as launch:
            result = battery._exam('example', self.root, b, {}, None, lambda: '')
        self.assertEqual(result, {'error': MESSAGE, 'refused': True})
        launch.assert_not_called()

    def test_battery_hidden_test_refusal_is_unscored_and_stops_broker(self):
        from supervisor import battery
        item = {'id': 'example', 'category': 'coding', 'prompt': 'example', 'dir': str(self.root)}
        with mock.patch.object(battery, 'ExamBroker') as factory, \
             mock.patch.object(battery, 'secrets_env', return_value={}), \
             mock.patch.object(battery, 'base', return_value=self.root), \
             mock.patch.object(battery, '_exam', return_value={}), \
             mock.patch.object(battery, '_append') as append, mock.patch.object(battery, 'log'), \
             mock.patch.object(battery, '_grade_saved') as grade:
            factory.return_value.disabled = set()
            row = battery._one_in('example', item, 'instance', self.root, self.root, 'example', {}, None, lambda: '', None)
        self.assertEqual(row['status'], 'refused')
        self.assertEqual(row['error'], MESSAGE)
        factory.return_value.stop.assert_called_once()
        grade.assert_not_called()
        append.assert_called_once()

    def test_replay_refusal_is_incomplete_and_stops_broker(self):
        from datetime import datetime
        from supervisor import autonomy, trial
        def command(*args):
            sandbox.profile(self.root, self.root, [])
        with mock.patch.object(broker, 'ReplayBroker') as factory, \
             mock.patch.object(trial, 'secrets_env', return_value={}), \
             mock.patch.object(trial, '_cloud_usd', return_value=0), \
             mock.patch.object(trial, '_config', return_value={}), \
             mock.patch.object(autonomy, 'describe_schedule', return_value=''), \
             mock.patch.object(runner, 'agent_command', side_effect=command), \
             mock.patch.object(trial.subprocess, 'Popen') as launch:
            factory.return_value.trace = []
            rec = trial._replay_cycle(self.root, 'example', {'t0': '2026-11-08T12:00:00', 'start_commit': 'test'}, {},
                                      self.root, self.root, kind='live', clock=datetime(2026, 11, 8, 12),
                                      model='example', cache={}, cache_lock=None, billed='example', should_stop=lambda: False)
        self.assertEqual(rec['ended'], 'refused')
        self.assertEqual(rec['exit'], runner.REFUSED)
        self.assertEqual(rec['output_tail'], MESSAGE)
        self.assertFalse(rec['completed'])
        factory.return_value.stop.assert_called_once()
        launch.assert_not_called()

    def test_trial_gate_refusal_remains_testing(self):
        from supervisor import trial
        output = gate.Output(MESSAGE, 'baseline', MESSAGE)
        output.refused = True
        t = {'instance': 'example', 'id': 'test', 'status': 'testing'}
        with mock.patch.object(trial, 'gate_check', return_value=(False, output)), \
             mock.patch.object(trial, 'load', return_value=t), mock.patch.object(trial, 'save'), \
             mock.patch.object(trial, 'deliver') as deliver:
            trial._gate_job(self.root, t)
        self.assertEqual(t['status'], 'testing')
        self.assertIn(MESSAGE, t['gate']['output'])
        deliver.assert_not_called()

    def test_prediction_exam_returns_refused_exit(self):
        from supervisor import privileged
        with mock.patch.object(runner, 'agent_command', side_effect=lambda *a: sandbox.profile(self.root, self.root, [])):
            result = privileged._exam(self.root, mock.Mock(name='example'), {'t0': '2026-11-08T12:00:00'},
                                      'example', 'example', lambda: False)
        self.assertEqual(result, {'exit': runner.REFUSED, 'error': MESSAGE})

    def test_check_cli_returns_refused_without_fail_verdict(self):
        from supervisor import __main__ as cli
        with mock.patch.object(cli, 'instance_root', return_value=self.root), \
             mock.patch.object(gate, 'committed_tree', return_value=(self.root, 'test')), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(cli.main(['check', 'example']), runner.REFUSED)
        self.assertIn(MESSAGE, stdout.getvalue())
        self.assertNotIn('FAIL', stdout.getvalue())

    def test_coder_refusal_applies_nothing(self):
        from supervisor import coder
        output = gate.Output(MESSAGE, 'baseline', MESSAGE)
        output.refused = True
        files = [{'path': 'shaft/example.py', 'content': 'changed = True\n'}]
        with mock.patch.object(coder, 'write', return_value=(files, 'example', {'usd': 0})), \
             mock.patch.object(coder, 'test_in_scratch', return_value=(False, output)):
            result = coder.delegate(self.root, 'example', 'example')
        self.assertFalse(result['applied'])
        self.assertIn(MESSAGE, result['error'])
        self.assertFalse((self.root / 'shaft/example.py').exists())

    def test_probation_restore_refusal_keeps_current_code(self):
        from supervisor import metrics
        code = self.root / 'shaft/example.py'
        code.parent.mkdir()
        code.write_bytes(b'current\n')
        with mock.patch.object(metrics, 'INSTANCES', self.root.parent), \
             mock.patch.object(metrics, 'git_wt', return_value=mock.Mock(returncode=0)), \
             mock.patch.object(metrics, '_blob', side_effect=lambda root, rev, rel: b'earlier\n' if rev.endswith('^') else b'current\n'), \
             mock.patch.object(gate, 'committed_tree', return_value=(self.root, 'test')):
            reason = metrics.restore_change(self.root.name, {'commit': 'test', 'files': ['shaft/example.py']})
        self.assertIn(MESSAGE, reason)
        self.assertIn('refused', reason)
        self.assertEqual(code.read_bytes(), b'current\n')

    def test_refused_trial_pair_stays_pending_without_judgment(self):
        from supervisor import trial
        state = {'status': 'pending', 'ts': 'example', 't0': '2026-11-08T12:00:00', 'start_commit': 'test', 'preempted': 0}
        t = {'instance': 'example', 'id': 'test', 'by': 'operator', 'states': [state],
             'llm': {'local': False, 'model': 'example'}}
        rec = {'ended': 'refused', 'completed': False, 'output_tail': MESSAGE, 'seconds': 0, 'usd': 0,
               'usage': {'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0}, 'trace': []}
        with mock.patch.object(trial, '_files', return_value={}), \
             mock.patch.object(trial, '_dir', return_value=self.root), \
             mock.patch.object(trial, '_read_json', return_value=None), \
             mock.patch.object(trial, 'replay_arm', side_effect=lambda *a, **k: dict(rec)), \
             mock.patch.object(trial, '_ledger'), mock.patch.object(trial, 'log'), \
             mock.patch.object(trial, 'save'), mock.patch.object(trial, 'state_context') as judge_context:
            trial.run_pair(self.root, t, 0, 'example')
        self.assertEqual(state['status'], 'pending')
        self.assertEqual(state['preempted'], 0)
        self.assertEqual(state['why'], MESSAGE)
        judge_context.assert_not_called()

    def test_breeding_refusal_keeps_child_and_does_not_retry(self):
        from supervisor import evolution
        child = self.root / 'example'
        child.mkdir()
        (child / 'shaft').mkdir()
        (child / 'shaft/example.py').write_text('changed = True\n')
        with mock.patch.object(evolution, 'INSTANCES', self.root), mock.patch.object(evolution, 'git') as git, \
             mock.patch.object(evolution, 'git_wt'), mock.patch.object(evolution, 'journal_note'), \
             mock.patch.object(gate, 'committed_tree', return_value=(self.root, 'test')):
            ok, output = evolution._build_child('parenta', 'parentb', 'example', [], 'example')
        self.assertFalse(ok)
        self.assertTrue(output.refused)
        self.assertEqual((child / 'shaft/example.py').read_text(), 'changed = True\n')
        self.assertFalse(any('remove' in call.args or '-D' in call.args for call in git.call_args_list))
        with mock.patch.object(evolution, 'INSTANCES', self.root / 'unused'), \
             mock.patch.object(evolution, 'SUP_DIR', self.root / 'state'), \
             mock.patch.object(evolution, 'instance_root'), mock.patch.object(evolution.mind, 'exists', return_value=False), \
             mock.patch.object(evolution, '_gene_diff', return_value=[]), \
             mock.patch.object(evolution, '_build_child', return_value=(False, output)) as build:
            self.assertEqual(evolution.breed('parenta', 'parentb', 'example'), runner.REFUSED)
        build.assert_called_once()

    def test_conversation_profile_refusal_is_logged_and_returned(self):
        (self.root / '.git').mkdir()
        with mock.patch.object(runner, 'PROJECT', self.root), \
             mock.patch.object(runner, 'instance_root', return_value=self.root), \
             mock.patch.object(runner, '_trap_runs'), mock.patch.object(runner, 'Presence'), \
             mock.patch.object(runner, '_conversation', side_effect=lambda *a: sandbox.profile(self.root, self.root, [])), \
             mock.patch.object(runner, 'log') as log:
            self.assertEqual(runner.run_conversation('example', 'talk', [], 'test', mock.Mock()), runner.REFUSED)
        self.assertIn(MESSAGE, log.call_args.args[0])

    def test_template_update_refusal_keeps_merge_files_uncommitted(self):
        from supervisor import __main__ as cli
        project, root = self.root / 'project', self.root / 'example'
        project.mkdir()
        git_root = project
        def git(*args):
            return subprocess.run(['git', '-C', str(git_root), '-c', 'user.name=test', '-c', 'user.email=test@example.test',
                                   *args], check=True, capture_output=True, text=True).stdout.strip()
        git('init', '-q', '-b', 'main')
        code = project / 'shaft/example.py'
        code.parent.mkdir()
        code.write_text('before\n')
        git('add', '.')
        git('commit', '-qm', 'before')
        before = git('rev-parse', 'HEAD')
        git('checkout', '-qb', 'template')
        code.write_text('changed\n')
        git('commit', '-qam', 'changed')
        git('checkout', '-q', 'main')
        git('worktree', 'add', '-qb', 'instance/example', str(root), 'main')
        git_root, code = root, root / 'shaft/example.py'
        with mock.patch.object(cli, 'instance_root', return_value=root), \
             mock.patch.object(core, 'PROJECT', project), \
             mock.patch.object(cli, 'run_lock', return_value=contextlib.nullcontext()), \
             mock.patch.object(cli, 'set_update_queued'), mock.patch.object(runner, '_commit_outside_config'), \
             mock.patch.object(runner, '_commit_outside_drive'), \
             mock.patch.object(gate, 'committed_tree', return_value=(self.root, 'test')):
            self.assertEqual(cli.cmd_update(['example']), runner.REFUSED)
        self.assertEqual(code.read_text(), 'changed\n')
        self.assertEqual(git('rev-parse', 'HEAD'), before)
        self.assertIn('shaft/example.py', git('diff', '--name-only'))

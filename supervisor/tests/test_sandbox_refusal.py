"""A sandbox cannot start without locating the operator's per-user folder."""
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import runner, sandbox


class UserFolderRefusalTest(unittest.TestCase):
    def setUp(self):
        sandbox.user_folder.cache_clear()
        self.addCleanup(sandbox.user_folder.cache_clear)

    def profiles(self):
        root, home = Path('/tmp/example'), Path('/Users/example')
        return (
            lambda: sandbox.profile(root, home, []),
            lambda: sandbox.run_profile(root, home),
            lambda: sandbox.job_profile(root, home, root / '.jobs/example'),
        )

    def test_getconf_failure_refuses_profile(self):
        for result in (subprocess.CompletedProcess([], 1, 'unexpected output', 'getconf failed'),
                       subprocess.CompletedProcess([], 0, '', '')):
            for build in self.profiles():
                with self.subTest(result=result, build=build), \
                     mock.patch.object(sandbox.subprocess, 'run', return_value=result):
                    sandbox.user_folder.cache_clear()
                    with self.assertRaises(runner.RunRefused) as caught:
                        build()
                    message = str(caught.exception)
                    self.assertIn('/usr/bin/getconf DARWIN_USER_TEMP_DIR', message)
                    self.assertIn(f'stdout={result.stdout!r}', message)
                    self.assertIn(f'stderr={result.stderr!r}', message)

    def test_getconf_launch_error_refuses_profile(self):
        with mock.patch.object(sandbox.subprocess, 'run', side_effect=OSError('cannot execute')):
            with self.assertRaisesRegex(runner.RunRefused, '/usr/bin/getconf DARWIN_USER_TEMP_DIR.*cannot execute'):
                self.profiles()[0]()

    def test_run_reports_getconf_refusal_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.git').mkdir()
            stderr = io.StringIO()
            with mock.patch.object(runner, 'PROJECT', root), \
                 mock.patch.object(runner, 'instance_root', return_value=root), \
                 mock.patch.object(runner, '_Held'), mock.patch.object(runner, 'log'), \
                 mock.patch.object(runner, '_run', side_effect=lambda *a, **k: self.profiles()[0]()), \
                 mock.patch.object(sandbox.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'failed')), \
                 contextlib.redirect_stderr(stderr):
                self.assertEqual(runner.run_agent('example', 'talk', [], False), runner.REFUSED)
            self.assertIn('run refused', stderr.getvalue())
            self.assertIn('/usr/bin/getconf DARWIN_USER_TEMP_DIR', stderr.getvalue())
            self.assertIn("stderr='failed'", stderr.getvalue())
            self.assertNotIn('Traceback', stderr.getvalue())

    def test_success_closes_folder_and_keeps_image_exception(self):
        result = subprocess.CompletedProcess([], 0, '/private/var/folders/ab/example/T/\n', '')
        with mock.patch.object(sandbox.subprocess, 'run', return_value=result) as command:
            for build in self.profiles():
                profile = build()
                self.assertIn('(deny file-read* file-write* (subpath "/private/var/folders/ab/example")', profile)
                self.assertIn('/private/var/folders/ab/example/T/', profile)
            self.assertEqual(sandbox.user_folder(), Path('/private/var/folders/ab/example'))
            command.assert_called_once_with(['/usr/bin/getconf', 'DARWIN_USER_TEMP_DIR'], capture_output=True, text=True)

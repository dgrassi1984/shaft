"""A sandbox must refuse to start when the operator's per-user folder is unknown."""
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import runner, sandbox


class UserFolderLookupTest(unittest.TestCase):
    def setUp(self):
        sandbox.user_folder.cache_clear()
        self.addCleanup(sandbox.user_folder.cache_clear)

    def test_getconf_failure_refuses_profile_and_run(self):
        result = subprocess.CompletedProcess([], 1, "", "getconf: lookup failed\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            broker = mock.Mock(port=1234, shared=None, env_root=None, token="test")
            broker.gdrive_env.return_value = ""
            broker.mail_env.return_value = ""
            broker.calendar_env.return_value = ""
            broker.sandbox_env.return_value = {}
            broker.egress.port = 5678
            def launch(*args, **kwargs):
                runner.agent_command(root, broker, [], root / "agent.sb", [], {})
                return 0
            with mock.patch.object(sandbox.subprocess, "run", return_value=result), \
                    mock.patch.object(runner, "PROJECT", root), \
                    mock.patch.object(runner, "instance_root", return_value=root), \
                    mock.patch.object(runner, "_Held"), \
                    mock.patch.object(runner, "stage_docs"), \
                    mock.patch.object(runner, "_sees_images", return_value=False), \
                    mock.patch.object(runner, "_run", side_effect=launch), \
                    mock.patch.object(runner, "log") as log, contextlib.redirect_stderr(io.StringIO()) as err:
                code = runner.run_agent("example", "test", [], False)
            self.assertEqual(code, runner.REFUSED)
            self.assertIn("run refused", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())
            self.assertIn("/usr/bin/getconf DARWIN_USER_TEMP_DIR", err.getvalue())
            self.assertIn("getconf: lookup failed", err.getvalue())
            self.assertIn("getconf: lookup failed", log.call_args.args[0])
            self.assertFalse((root / "agent.sb").exists())


if __name__ == "__main__":
    unittest.main()

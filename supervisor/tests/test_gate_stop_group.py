"""Process group cleanup with an unreapable leader."""
import signal
import subprocess
import unittest
from unittest import mock

from supervisor import gate


class StopGroupTest(unittest.TestCase):
    def test_stuck_group_leader_does_not_block_cleanup(self):
        child = mock.Mock(spec=subprocess.Popen, pid=1234, returncode=None)
        child.poll.return_value = None

        def stuck_wait(timeout=None):
            if timeout is None:
                self.fail("wait without a timeout would hang")
            raise subprocess.TimeoutExpired("stuck leader", timeout)

        child.wait.side_effect = stuck_wait
        with mock.patch.object(gate.os, "killpg") as killpg, \
                mock.patch("time.monotonic", side_effect=[0, 0, 0, 11]), \
                mock.patch("time.sleep"), mock.patch("supervisor.core.log") as log:
            gate.stop_group(child, grace_s=0)

        self.assertEqual(killpg.call_args_list, [
            mock.call(child.pid, signal.SIGTERM),
            mock.call(child.pid, signal.SIGCONT),
            mock.call(child.pid, signal.SIGKILL),
        ])
        log.assert_called_once()
        self.assertIn("could not be reaped", log.call_args.args[0])

import os
import tempfile
from pathlib import Path

# Keep test runs out of the real supervisor state (logs, locks, judgments, stamps).
os.environ.setdefault("SHAFT_SUPERVISOR_DIR", tempfile.mkdtemp(prefix="shaft-sup-test-"))

# And out of the real instances: a test that patched one module's INSTANCES while the code under it read
# core.INSTANCES checked wisp's 575 real facts against its real episodes, 72 s in the main checkout (test_judge2,
# 2026-10-29). Set before any supervisor module copies the path with `from .core import INSTANCES`.
import socketserver   # noqa: E402

# A server stops at shutdown() only when serve_forever next polls: every 0.5 s by default, so each test that started
# one (fake LLMs, the broker, the egress proxy, OAuth's loopback) waited about half a second to end it — a cluster of
# 0.50 s tests (Codex's review of the suite's timings, 2026-10-29). Test runs only; production keeps the default.
_serve_forever = socketserver.BaseServer.serve_forever


def _serve_forever_quickly(self, poll_interval=0.01):
    return _serve_forever(self, poll_interval)


socketserver.BaseServer.serve_forever = _serve_forever_quickly

from supervisor import core   # noqa: E402

core.INSTANCES = Path(tempfile.mkdtemp(prefix="shaft-instances-test-"))


def _import_the_supervisor() -> None:
    """Every supervisor module, now: one first imported inside a test that patched core.SUP_DIR kept that test's
    temporary folder in its own `from .core import SUP_DIR` for the rest of the run. Alphabetical order hid it; with
    the gate's workers, trial.SUP_DIR froze on test_browser's folder and test_kill_delete's trials vanished
    (2026-10-29). The terminal clients' optional libraries may be missing
    (CONTRIBUTING.md): only such a module is left out."""
    import importlib
    import pkgutil
    import supervisor
    for info in pkgutil.iter_modules(supervisor.__path__):
        if info.ispkg:                   # __main__ included: the CLI's guard is `__name__ == "__main__"`
            continue
        try:
            importlib.import_module(f"supervisor.{info.name}")
        except ModuleNotFoundError as e:
            if (e.name or "supervisor").partition(".")[0] == "supervisor":
                raise


_import_the_supervisor()

# A test's outcome must not depend on the hour it runs at: with push holding on, every test that sends the operator a
# message between 22:00 and 07:00 saw it held for the morning digest (seven Telegram and delivery tests failed at
# 22:30, 2026-10-31). Holding is off for the run; test_push_hold turns it on with its own clock and quiet hours.
from supervisor import pushes   # noqa: E402

pushes.PUSH_HOLD = 0

import os
import tempfile
from pathlib import Path

# Keep test runs out of the real supervisor state (logs, locks, judgments, stamps).
os.environ.setdefault("SHAFT_SUPERVISOR_DIR", tempfile.mkdtemp(prefix="shaft-sup-test-"))

# And out of the real instances: a test that patched one module's INSTANCES while the code under it read
# core.INSTANCES checked wren's 575 real facts against its real episodes, 72 s in the main checkout (test_judge2,
# 2026-10-29). Set before any supervisor module copies the path with `from .core import INSTANCES`.
from supervisor import core   # noqa: E402

core.INSTANCES = Path(tempfile.mkdtemp(prefix="shaft-instances-test-"))

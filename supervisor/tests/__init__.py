import os
import tempfile

# Keep test runs out of the real supervisor state (logs, locks, judgments, stamps).
os.environ.setdefault("SHAFT_SUPERVISOR_DIR", tempfile.mkdtemp(prefix="shaft-sup-test-"))

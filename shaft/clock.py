"""Agent time, advanced from SHAFT_NOW during a replay."""
import os
import time
from datetime import datetime, timedelta

STARTED = time.monotonic()
STARTED_AT = datetime.now()


def now() -> datetime:
    """The time as my prompts state it. A trial replay sets SHAFT_NOW to its cycle's start (PLAN-trials F3): it then
    runs from there, while the supervisor's record is cut at that moment too."""
    try:
        at = datetime.fromisoformat(os.environ.get("SHAFT_NOW", ""))
    except ValueError:
        return datetime.now()
    at = at.astimezone().replace(tzinfo=None) if at.tzinfo else at   # naive local, as the journal's times are
    return at + timedelta(seconds=time.monotonic() - STARTED)


def started() -> datetime:
    """When this run started, as my prompts date it, fixed for the whole run: a replay's SHAFT_NOW exactly (both arms of
    a trial replay see the same moment, however long each takes to get going), else the moment I was started."""
    try:
        at = datetime.fromisoformat(os.environ.get("SHAFT_NOW", ""))
    except ValueError:
        return STARTED_AT
    return at.astimezone().replace(tzinfo=None) if at.tzinfo else at

"""Speaker names suggested by instances and decided by the operator."""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime

from . import core, meetings, speakers


def _root():
    return core.SUP_DIR / "meeting-proposals"


def _save(path, row):
    with meetings._atomic_output(path) as out:
        out.write(meetings._json_bytes(row))


def create(instance, metadata, label, name, why):
    if not isinstance(why, str) or not why.strip() or len(why) > 2_000:
        raise meetings.Invalid("why must be 1–2,000 characters.")
    name = speakers._name(name)
    root = _root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    pid = secrets.token_hex(16)
    row = {"id": pid, "instance": instance, "meeting": metadata["id"], "speaker": label, "name": name,
           "why": why, "created": datetime.now().astimezone().isoformat(), "state": "pending",
           "revision": metadata.get("ready_revision")}
    try:
        _save(root / f"{pid}.json", row)
    except UnicodeError:
        raise meetings.Invalid("why must contain valid Unicode.") from None
    _notify(row, metadata)
    return {"id": pid, "state": "pending"}


def _notify(row, metadata):
    """A push to the operator: the tap opens the meeting, where the proposal waits beside its speaker."""
    from . import webpush
    title = " ".join(str(metadata.get("title") or metadata["id"]).split())[:80]
    try:
        webpush.notify({"title": f"{row['instance']}: name a speaker in {title}",
                        "body": f"Speaker {row['speaker']} is {row['name']}? {row['why']}"[:300],
                        "url": f"/meetings/{metadata['id']}", "tag": f"meeting-proposal-{row['id']}", "cid": None})
    except Exception as e:
        core.log(f"meeting proposal {row['id']}: push to the operator failed ({type(e).__name__}: {e})")


def pending():
    rows = (json.loads(path.read_bytes()) for path in _root().glob("*.json"))
    return sorted((row for row in rows if row["state"] == "pending"), key=lambda row: row["created"], reverse=True)


def decide(pid, approve):
    if type(approve) is not bool:
        raise meetings.Invalid("approve must be true or false.")
    if not isinstance(pid, str) or not re.fullmatch(r"[0-9a-f]{32}", pid):
        raise meetings.Missing("No such speaker proposal.")
    path = _root() / f"{pid}.json"
    if not path.is_file():
        raise meetings.Missing("No such speaker proposal.")
    with core.config_lock(path):
        row = json.loads(path.read_bytes())
        state = "confirmed" if approve else "rejected"
        if row["state"] == state:
            return row
        if row["state"] != "pending":
            raise meetings.Conflict("This proposal has already been decided.")
        if approve:
            speakers._name_label(row["meeting"], row["speaker"], row["name"], expected_revision=row["revision"])
        row["state"] = state
        _save(path, row)
    return row

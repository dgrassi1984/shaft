"""Operator decisions on sleep's drive proposals (#105), using the drive compare-and-set."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from . import core, opconfig

DIRECTORY = "notes/drive-proposals"
ID = re.compile(r"d\d{4}-\d{4}-[1-9]\d*")


def _proposal(root, pid):
    if not isinstance(pid, str) or not ID.fullmatch(pid):
        raise ValueError("invalid proposal id")
    text = core.safe_read(root, f"{DIRECTORY}/{pid}.md", "")
    match = re.search(r"\n```json\n(.*)\n```\n?\Z", text, re.S)
    if not match:
        raise ValueError("proposal missing or malformed")
    rec = json.loads(match[1])
    if (not isinstance(rec, dict) or rec.get("id") != pid or
            any(not isinstance(rec.get(k), str) or not rec[k].strip()
                for k in ("current_text", "proposed_text", "evidence")) or
            not isinstance(rec.get("predictions"), list) or not 1 <= len(rec["predictions"]) <= 3 or
            any(not isinstance(p, str) or not 0 < len(p) < 300 for p in rec["predictions"])):
        raise ValueError("malformed proposal fields")
    return rec, hashlib.sha256(text.encode()).hexdigest()


def command(name, action, pid=None, expect_sha=None, reason=None) -> int:
    root = core.instance_root(name)
    try:
        if action == "proposals":
            for fname in sorted(core.list_beneath(root, DIRECTORY)):
                if fname.endswith(".md") and ID.fullmatch(fname[:-3]):
                    rec, _ = _proposal(root, fname[:-3])
                    decision = core.safe_read(root, f"{DIRECTORY}/{rec['id']}.decision.json", "")
                    status = json.loads(decision)["status"] if decision else "pending"
                    print(f"{rec['id']}: {status}")
            return 0
        if action not in ("accept", "reject"):
            raise ValueError("unknown proposal action")
        rec, digest = _proposal(root, pid)
        path = f"{DIRECTORY}/{pid}.decision.json"
        with core.config_lock(root / path):
            if core.safe_read(root, path, ""):
                raise ValueError("proposal already decided")
            if action == "accept":
                if not expect_sha:
                    raise ValueError("--expect-sha is required (shaft drive <name> prints it)")
                current, _ = opconfig.drive(name)
                if current is not None and current.strip() != rec["current_text"].strip():
                    raise ValueError("proposal's current_text no longer matches drive.md")
                opconfig.set_drive(name, rec["proposed_text"], expect_sha)
            elif not isinstance(reason, str) or not reason.strip():
                raise ValueError("--reason is required for reject")
            core.safe_write(root, path, json.dumps({"id": pid, "status": "accepted" if action == "accept" else "rejected",
                            "at": datetime.now().isoformat(), "reason": reason, "proposal_sha": digest,
                            "predictions": rec["predictions"]}, ensure_ascii=False, indent=2) + "\n")
        print(f"{pid}: {'accepted' if action == 'accept' else 'rejected'}")
        return 0
    except (OSError, ValueError, opconfig.Refused, core.UnsafePath) as e:
        print(f"refused: {e}")
        return 1

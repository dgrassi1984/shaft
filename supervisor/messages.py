"""Messages between the operator and instances (instance→instance goes through the broker).

Every delivery — instance→instance, instance→operator, operator→instance — is also appended to the
supervisor's own log (.supervisor/messages.jsonl), which `shaft top` browses. The log, not the instances'
inbox/ and memory/messages/ folders, is the record: those are instance-controlled, the log is not.
"""
from __future__ import annotations

import fcntl
import json
import shutil
from datetime import datetime
from pathlib import Path

from .core import INSTANCES, SUP_DIR, instance_root, log, safe_write

OPERATOR_INBOX = SUP_DIR / "operator-inbox"
LOG_NAME = "messages.jsonl"


def record(sup_dir: Path, path: Path, sender: str, to: str, kind: str, at: datetime, text: str) -> None:
    """Append one delivered message to the log. One locked write per line: brokers of several instances
    deliver concurrently."""
    line = json.dumps({"ts": at.isoformat(timespec="seconds"), "from": sender, "to": to, "kind": kind,
                       "file": path.name, "text": text.strip()}, ensure_ascii=False) + "\n"
    sup_dir.mkdir(parents=True, exist_ok=True)
    with (sup_dir / LOG_NAME).open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)


_cache: dict = {"key": None, "rows": []}


def _unread(to: str) -> set[str]:
    """File names still waiting in the recipient's inbox (it moves them out once it has read them)."""
    inbox = OPERATOR_INBOX if to == "operator" else INSTANCES / to / "inbox"
    return {p.name for p in inbox.glob("*.md")} if inbox.is_dir() else set()


def history() -> list[dict]:
    """Every logged message, oldest first, each with `read` (False while it sits in the recipient's inbox)."""
    path = SUP_DIR / LOG_NAME
    try:
        st = path.stat()
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size)
    if _cache["key"] != key:
        rows = []
        for line in path.read_text(errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        rows.sort(key=lambda m: (m["ts"], m["file"]))   # file names carry microseconds: ties keep their order
        _cache.update(key=key, rows=rows)
    waiting = {to: _unread(to) for to in {m["to"] for m in _cache["rows"]}}
    return [{**m, "read": m["file"] not in waiting[m["to"]]} for m in _cache["rows"]]


def _parse(path: Path) -> dict:
    head, _, body = path.read_text().partition("\n\n")
    meta = dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)
    return {"path": path, "from": meta.get("From", "?"), "kind": meta.get("Kind", "note"),
            "date": meta.get("Date", ""), "body": body.strip()}


def pending() -> list[dict]:
    if not OPERATOR_INBOX.is_dir():
        return []
    msgs = [_parse(p) for p in sorted(OPERATOR_INBOX.glob("*.md"))]
    return sorted(msgs, key=lambda m: (m["kind"] != "question", m["date"]))


def show_inbox() -> int:
    msgs = pending()
    if not msgs:
        print("no messages from instances.")
        return 0
    for m in msgs:
        mark = "QUESTION" if m["kind"] == "question" else "note"
        print(f"── {mark} from {m['from']} · {m['date']} · {m['path'].name}\n{m['body']}\n")
    print("reply with: shaft reply <instance> \"…\"   (notes are archived with: shaft inbox --clear)")
    return 0


def _deliver(name: str, text: str, kind: str, in_reply_to: str | None = None) -> Path:
    root = instance_root(name)
    stamp = datetime.now()
    ref = f"In-Reply-To: {in_reply_to}\n" if in_reply_to else ""
    path = safe_write(root, f"inbox/{stamp:%Y%m%d-%H%M%S-%f}-from-operator.md",
                      f"From: operator (Daniele)\nTo: {name}\nKind: {kind}\n{ref}Date: {stamp:%Y-%m-%d %H:%M}\n\n"
                      f"{text.strip()}\n")
    record(SUP_DIR, path, "operator", name, kind, stamp, text)
    return path


def reply(name: str, text: str) -> int:
    """Answer the oldest open question from `name` (or just send, if none is open)."""
    open_q = [m for m in pending() if m["from"] == name and m["kind"] == "question"]
    ref = open_q[0]["path"].name if open_q else None
    quoted = f"> {open_q[0]['body'][:600]}\n\n" if open_q else ""
    path = _deliver(name, quoted + text, "reply" if ref else "message", ref)
    if open_q:
        _archive(open_q[0]["path"])
    log(f"operator -> {name}: {'reply to ' + ref if ref else 'message'}")
    print(f"delivered to {name} ({path.name}); it will read it in its next cycle or conversation.")
    return 0


def send(name: str, text: str) -> int:
    path = _deliver(name, text, "message")
    log(f"operator -> {name}: message")
    print(f"delivered to {name} ({path.name}).")
    return 0


def clear_notes() -> int:
    n = 0
    for m in pending():
        if m["kind"] != "question":
            _archive(m["path"])
            n += 1
    print(f"archived {n} note(s); open questions stay until you reply.")
    return 0


def _archive(path: Path) -> None:
    done = OPERATOR_INBOX / "read"
    done.mkdir(parents=True, exist_ok=True)
    shutil.move(path, done / path.name)

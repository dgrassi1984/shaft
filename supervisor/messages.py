"""Messages between the operator and instances (instance→instance goes through the broker)."""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from .core import SUP_DIR, instance_root, log

OPERATOR_INBOX = SUP_DIR / "operator-inbox"


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
    inbox = root / "inbox"
    inbox.mkdir(exist_ok=True)
    stamp = datetime.now()
    path = inbox / f"{stamp:%Y%m%d-%H%M%S-%f}-from-operator.md"
    ref = f"In-Reply-To: {in_reply_to}\n" if in_reply_to else ""
    path.write_text(f"From: operator (Daniele)\nTo: {name}\nKind: {kind}\n{ref}Date: {stamp:%Y-%m-%d %H:%M}\n\n"
                    f"{text.strip()}\n")
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

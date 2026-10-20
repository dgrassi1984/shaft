"""Document comments live in the supervisor's store, away from files the instance rewrites.

Each instance has a thread lock and a file lock. Changes replace its JSON file atomically; no chat lock or chat
module is involved. The Markdown in notes/comments/ is an explicit, rebuildable copy of sent posts only.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from . import core, docs

MAX_TEXT = 20000
PID = re.compile(r"p-[0-9a-f]{16}")
THREAD = re.compile(r"c[1-9][0-9]*")
_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


class Refused(Exception):
    pass


def _id(value: str, pattern: re.Pattern, kind: str) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise Refused(f"invalid {kind}")


def _rel(doc: str) -> str:
    try:
        return docs._rel(doc)
    except docs.Refused as e:
        raise Refused(str(e)) from None


def _text(text: str) -> None:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise Refused(f"a comment needs text, at most {MAX_TEXT} characters")


def _anchor(anchor: dict | None) -> None:
    if anchor is None:
        return
    if not isinstance(anchor, dict):
        raise Refused("invalid anchor")
    for key, limit in (("exact", 2000), ("prefix", 32), ("suffix", 32)):
        value = anchor.get(key)
        if not isinstance(value, str) or len(value) > limit or (key == "exact" and not value):
            raise Refused(f"anchor {key} must be {'non-empty and ' if key == 'exact' else ''}at most {limit} characters")
    if type(anchor.get("start")) is not int or anchor["start"] < 0:
        raise Refused("anchor start must be a non-negative integer")


def _path(name: str) -> Path:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise Refused("invalid instance name")
    return core.SUP_DIR / "web" / "comments" / f"{name}.json"


@contextlib.contextmanager
def _store(name: str, write: bool = False):
    path = _path(name)
    with _locks_lock:
        lock = _locks.setdefault(str(path), threading.Lock())
    with lock, core.config_lock(path):
        try:
            store = json.loads(path.read_text())
        except FileNotFoundError:
            store = {"next": 1, "threads": {}, "notices": []}
        except (OSError, ValueError) as e:
            raise Refused(f"cannot read comments: {e}") from None
        before = json.dumps(store, ensure_ascii=False)
        yield store
        if write and json.dumps(store, ensure_ascii=False) != before:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=path.parent)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(store, f, ensure_ascii=False, indent=1)
                os.replace(tmp, path)                 # mkstemp creates mode 0600, retained by rename
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)


def _find(store: dict, pid: str) -> tuple[str, dict, dict]:
    _id(pid, PID, "post id")
    for tid, thread in store["threads"].items():
        for post in thread["posts"]:
            if post["pid"] == pid:
                return tid, thread, post
    raise Refused("no such post")


def _thread(store: dict, tid: str) -> dict:
    _id(tid, THREAD, "thread id")
    if tid not in store["threads"]:
        raise Refused("no such thread")
    return store["threads"][tid]


def _post(pid: str, text: str, by: str, turn: str | None = None) -> dict:
    post = {"pid": pid, "by": by, "text": text, "ts": datetime.now().isoformat(timespec="seconds"), "sent": None}
    if turn is not None:
        post["turn"] = turn
    return post


def _new(store: dict, pid: str, doc: str, anchor: dict | None, sha: str, text: str, by: str,
         turn: str | None = None) -> str:
    tid = f"c{store['next']}"
    store["next"] += 1
    post = _post(pid, text, by, turn)
    store["threads"][tid] = {"doc": doc, "by": by, "state": "open", "sha": sha, "anchor": anchor,
                             "created": post["ts"], "posts": [post], "unsectioned": []}
    return tid


def add_thread(name: str, pid: str, doc: str, anchor: dict | None, sha: str, text: str,
               by: str = "daniele") -> str:
    """Create a thread, or return the thread already holding this post id."""
    _id(pid, PID, "post id")
    with _store(name, True) as store:
        for tid, thread in store["threads"].items():
            if any(p["pid"] == pid for p in thread["posts"]):
                return tid
        doc = _rel(doc)
        _anchor(anchor)
        _text(text)
        return _new(store, pid, doc, anchor, sha, text, by)


def reply(name: str, pid: str, thread: str, text: str, by: str) -> str:
    """Append an unsent post; retries do not append again."""
    _id(pid, PID, "post id")
    _id(thread, THREAD, "thread id")
    with _store(name, True) as store:
        for tid, item in store["threads"].items():
            if any(p["pid"] == pid for p in item["posts"]):
                return tid
        item = _thread(store, thread)
        _text(text)
        item["posts"].append(_post(pid, text, by))
        return thread


def _unsent(post: dict) -> None:
    if post.get("sent") or post.get("turn"):
        raise Refused("a sent post cannot be edited or deleted; reply with a correction")


def edit(name: str, pid: str, text: str) -> None:
    _text(text)
    with _store(name, True) as store:
        _, _, post = _find(store, pid)
        _unsent(post)
        post["text"] = text


def delete(name: str, pid: str) -> None:
    with _store(name, True) as store:
        tid, thread, post = _find(store, pid)
        _unsent(post)
        thread["posts"].remove(post)
        if not thread["posts"]:
            del store["threads"][tid]


def mark_sent(name: str, pid: str, ref: str) -> None:
    if not isinstance(ref, str) or not ref.strip():
        raise Refused("a sent post needs a chat turn reference")
    with _store(name, True) as store:
        _, _, post = _find(store, pid)
        if not post.get("turn") and not post.get("sent"):
            post["sent"] = ref


def set_state(name: str, thread: str, state: str) -> None:
    if state not in ("open", "resolved"):
        raise Refused("state must be open or resolved")
    with _store(name, True) as store:
        item = _thread(store, thread)
        if item["state"] != state:
            item["state"] = state
            verb = "resolved" if state == "resolved" else "reopened"
            store["notices"].append(f"[Daniele {verb} {thread} on notes/docs/{item['doc']}]")


def take_notices(name: str) -> list[str]:
    with _store(name, True) as store:
        notices = store["notices"]
        store["notices"] = []
        return notices


def put_back_notices(name: str, notices: list[str]) -> None:
    with _store(name, True) as store:
        store["notices"] = list(notices) + store["notices"]


def threads(name: str, doc: str | None = None) -> list[dict]:
    """Open threads first, then resolved; creation order within each group."""
    doc = _rel(doc) if doc is not None else None
    with _store(name) as store:
        items = [{"id": tid, **{k: v for k, v in t.items() if k != "source"}}
                 for tid, t in store["threads"].items() if doc is None or t["doc"] == doc]
        return sorted(items, key=lambda t: (t["state"] != "open", int(t["id"][1:])))


def open_counts(name: str) -> dict[str, int]:
    counts = {}
    for t in threads(name):
        if t["state"] == "open":
            counts[t["doc"]] = counts.get(t["doc"], 0) + 1
    return counts


def get_post(name: str, pid: str) -> dict:
    """A post with its owning thread id and document path, for sending it later."""
    with _store(name) as store:
        tid, thread, post = _find(store, pid)
        return {**post, "thread": tid, "doc": thread["doc"]}


def _fence(text: str) -> str:
    """A fence longer than every backtick run in the quoted text."""
    return "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", text)), default=0))


def _quote(text: str) -> str:
    if "\n" not in text and "\r" not in text:
        return json.dumps(text, ensure_ascii=False)
    fence = _fence(text)
    return f"\n{fence}\n{text}\n{fence}\n"


def block(name: str, thread_id: str, pid: str) -> str:
    """One comment's message, including the answer convention on every send."""
    with _store(name) as store:
        thread = _thread(store, thread_id)
        tid, _, post = _find(store, pid)
        if tid != thread_id:
            raise Refused("post belongs to another thread")
        prior = thread["posts"][:thread["posts"].index(post)]
    who = "Daniele" if post["by"] == "daniele" else post["by"]
    where = f"notes/docs/{thread['doc']} ({tid})"
    if prior:
        intro = f"[{who} replied on {where}:]"
        last = next((p for p in reversed(prior) if p.get("turn")), None)
        if last:
            intro += f"\n[Last answer:]\n{_quote(last['text'])}"
    elif thread["anchor"] is None:
        intro = f"[{who} commented on {where}, on the whole document:]"
    else:
        anchor = thread["anchor"]
        exact = anchor["exact"]
        intro = f"[{who} commented on {where}, on the text {_quote(exact)}:]"
        try:
            raw, _ = docs.read(name, thread["doc"])
        except docs.Refused as e:
            raise Refused(str(e)) from None
        raw = raw or ""
        first = raw.find(exact)
        if first < 0:
            intro += "\n[This is the text as shown on screen, not a literal quote from the raw file.]"
        elif raw.find(exact, first + 1) >= 0:
            intro += f"\n[Neighbours: before {_quote(anchor['prefix'])}; after {_quote(anchor['suffix'])}.]"
    body = "> " + post["text"] if "\n" not in post["text"] else _quote(post["text"])
    contract = (f'[Answer it in your final reply under a line "### {tid}". Open a thread of your own with a line '
                '\'### comment on notes/docs/<doc>.md: "exact text from it"\' followed by your comment.]')
    return f"{intro}\n{body}\n{contract}"


def _sections(answer: str) -> list[tuple[str, str]]:
    """Level 1–3 headings delimit sections, except inside backtick or tilde fences."""
    sections = []
    heading = None
    body = []
    fence = None
    for line in answer.splitlines():
        run = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if run and run[1][0] == fence[0] and len(run[1]) >= len(fence) and not run[2].strip():
                fence = None
        elif run and (run[1][0] == "~" or "`" not in run[2]):
            fence = run[1]
        elif re.match(r"^ {0,3}#{1,3}(?:\s|$)", line):
            if heading is not None:
                sections.append((heading, "\n".join(body).strip()))
            heading, body = line.rstrip(), []
            continue
        if heading is not None:
            body.append(line)
    if heading is not None:
        sections.append((heading, "\n".join(body).strip()))
    return sections


def parse_answer(name: str, answer: str, sent_ids: list, started_by: str | None = None,
                 chat_turn_ref: str = "web-x#5") -> dict:
    """Attach sections to any open thread with a sent post, once per turn.

    sent_ids are only the threads this reply heard: only they receive unsectioned references or qualify for
    the started_by whole-answer fallback. The caller admits only normally completed replies.
    """
    result = {"answered": [], "opened": [], "unsectioned": []}
    sent = {i["id"] if isinstance(i, dict) else i for i in sent_ids}
    with _store(name, True) as store:
        eligible = {tid: t for tid, t in store["threads"].items()
                    if t["state"] == "open"
                    and any(p.get("sent") or p.get("turn") for p in t["posts"])}
        found = {}
        for index, (heading, body) in enumerate(_sections(answer)):
            match = re.fullmatch(r"### (c[1-9][0-9]*)", heading)
            if match and match[1] in eligible:
                found.setdefault(match[1], body)
            opened = re.fullmatch(r'### comment on notes/docs/(.+\.md): "(.+)"', heading)
            if not opened:
                continue
            # The section ordinal identifies an instance-opened thread even if its state later changes.
            source = f"{chat_turn_ref}:{index}"
            if any(t.get("source") == source for t in store["threads"].values()):
                continue
            anchor = {"exact": opened[2], "prefix": "", "suffix": "", "start": 0}
            try:
                doc = _rel(opened[1])
                _anchor(anchor)
                _text(body)
                _, sha = docs.read(name, doc)
            except (Refused, docs.Refused):
                continue
            tid = _new(store, "p-" + secrets.token_hex(8), doc, anchor, sha, body, name, chat_turn_ref)
            store["threads"][tid]["source"] = source
            result["opened"].append({"id": tid, "doc": doc})
        if started_by in eligible and sent == {started_by} and started_by not in found:
            found[started_by] = answer.strip()
        for tid, thread in eligible.items():
            if (any(p.get("turn") == chat_turn_ref for p in thread["posts"])
                    or chat_turn_ref in thread["unsectioned"]):
                continue
            item = {"id": tid, "doc": thread["doc"]}
            if tid in found:
                thread["posts"].append(_post("p-" + secrets.token_hex(8), found[tid], name, chat_turn_ref))
                result["answered"].append(item)
            elif tid in sent:
                thread["unsectioned"].append(chat_turn_ref)
                result["unsectioned"].append(item)
    return result


def _copy(items: list[dict], doc: str) -> str:
    out = [f"# Comments on notes/docs/{doc}"]
    for thread in items:
        posts = [p for p in thread["posts"] if p.get("sent") or p.get("turn")]
        if not posts:
            continue
        out.append(f"## {thread['id']} ({thread['state']})")
        out.append("On " + _quote(thread["anchor"]["exact"]) if thread["anchor"] else "On the whole document.")
        for post in posts:
            out.append(f"### {post['by']} · {post['ts']} · {post.get('turn') or post['sent']}\n\n{post['text']}")
        for ref in thread["unsectioned"]:
            out.append(f"Answered in the chat, without a section for {thread['id']}: {ref}")
    return "\n\n".join(out) + "\n"


def copy_text(name: str, doc: str) -> str:
    doc = _rel(doc)
    return _copy(threads(name, doc), doc)


def sync_copy(name: str, doc: str) -> None:
    """Rebuild the copy under the store lock so concurrent syncs cannot restore an older snapshot."""
    doc = _rel(doc)
    with _store(name) as store:
        items = [{"id": tid, **t} for tid, t in store["threads"].items() if t["doc"] == doc]
        items.sort(key=lambda t: (t["state"] != "open", int(t["id"][1:])))
        try:
            root = core.instance_root(name)
            rel = f"notes/comments/{doc}"
            path = core.contained(root, *Path(rel).parts)
            with core.config_lock(path):
                core.safe_write_bytes(root, rel, _copy(items, doc).encode())
        except (core.UnsafePath, OSError) as e:
            raise Refused(f"notes/comments/{doc}: {e}") from None

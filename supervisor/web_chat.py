"""Chat with an instance in the browser. Stdlib only.

Each web chat is a conversation of SHAFT's own API, as Telegram's are: the web server calls 127.0.0.1's
/v1/chat/completions (streamed) with SHAFT_API_KEY and the operator's tailnet login, naming the conversation with
X-Shaft-Conversation (the chat's id). So the reply is the API's — a normal sandboxed run with the run lock, the gate
and the commit; its narration rule; waiting while the instance is busy — and the key never reaches the browser.

A reply keeps going when the browser drops: it is relayed by a thread of the web server, which
saves its activity as it streams and the answer when it ends, so a reload, a phone that locked or a second device
shows it. Only Stop stops it (the relay closes the API connection; the API then stops the agent). A message sent while
that thread replies is an interjection (/v1/chat/interject): the instance reads it at its next step; one it did not hear in
time is answered by a reply of its own once this one ends, as on Telegram. End asks for the journal write-up now
(/v1/chat/end does not wait for the entry to be written) instead of after SHAFT_IDLE_MINUTES.

Files go in with a message: saved in the instance's folder by messages.from_operator (as Telegram's are), and the
message says where. Messages sent during a reply belong to that thread. Cycle messages belong to the From feed.

Chats are kept in .supervisor/web/chats/<id>.json, written only by this process.

The feed and rail share last_from; their internal last_count distinguishes instance items in the same second.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .core import SUP_DIR, UnsafePath, log
from .web_md import first_line

SAVE_EVERY_S = 1.0                 # how often a streaming reply's activity is written for the page to read
STREAM_KEEP_S = 30.0               # a reply's stream events are kept this long after it ends: a reconnect gets its end
MAX_TEXT = 20000                   # a message, at most


# Added, unseen by the operator, to the first message of each web chat: how the instance shares a document with them
#. In the message rather than its prompts, so every instance gets it whatever its own code says.
DOCS_NOTE = ("[Web chat: a Markdown file you write under notes/docs/ appears beside this chat as a document the operator sees "
             "live and can edit. Write it whole (write_file with replace=true when you drop lines). His edits reach you "
             "as a diff in their next message. The operator's comments on notes/docs/X.md, sent or not, are in "
             "notes/comments/X.md.]")


class Refused(Exception):
    pass


def _docs_now(name: str) -> dict[str, str]:
    """The instance's documents as they are ({path: sha}); a reply's cards are what changed. Never fails a reply."""
    from . import docs
    try:
        return docs.snapshot(name)
    except (Exception, SystemExit) as e:          # noqa: BLE001 — a folder it broke must not stop the chat; SystemExit
        #                                           is core.instance_root's way to say the instance is not there
        log(f"web: could not read {name}'s documents: {type(e).__name__}: {e}")
        return {}


def _dir() -> Path:
    return SUP_DIR / "web" / "chats"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _lock(cid: str) -> threading.Lock:
    with _locks_lock:
        return _locks.setdefault(cid, threading.Lock())


_inst_locks: dict[str, threading.RLock] = {}


def _inst_lock(name: str) -> threading.RLock:
    """One instance's chat lifecycle at a time — which chat is open, a new one, End, Clear — so two devices never open
    two chats at once and End never closes a reply that a message started meanwhile (Codex review, 2026-10-19). Taken
    before a chat's own lock, never after it; re-entrant, as a save during a reply sends through send()."""
    with _locks_lock:
        return _inst_locks.setdefault(name, threading.RLock())


def load(cid: str) -> dict | None:
    if not cid.startswith("web-") or not cid[4:].isalnum():
        return None
    try:
        return json.loads((_dir() / f"{cid}.json").read_text())
    except (OSError, ValueError):
        return None


def save(chat: dict) -> None:
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{chat['id']}.{os.getpid()}.{threading.get_ident()}"
    tmp.write_text(json.dumps(chat, ensure_ascii=False, indent=1))
    os.replace(tmp, d / f"{chat['id']}.json")
    with chat_saved:                                # a feed held by a long-poll answers now (app_api, wait_change)
        chat_saved.notify_all()
    # The persisted user turn is the delivery receipt, including after a restart.
    ids = {fid for t in chat.get("turns", []) for fid in t.get("feedback", [])}
    if ids:
        from . import feedback
        for item in feedback.items(chat["instance"]):
            if item["id"] in ids and not item["sent"]:
                feedback.mark_sent(item["id"], "chat", chat["id"])


_parsed: dict[str, tuple[tuple, dict]] = {}     # path -> ((inode, mtime_ns, size), chat): read again only when it changes


def chats(name: str) -> list[dict]:
    """This instance's web chats, newest first. Read-only: a change goes through load() and save(). Every feed, panel
    and rail refresh asks for them (every chat file of every instance was parsed each time)."""
    out = []
    for p in _dir().glob("web-*.json") if _dir().is_dir() else []:
        try:
            st = p.stat()
            stamp = (st.st_ino, st.st_mtime_ns, st.st_size)       # save() replaces the file: a new inode each time
            hit = _parsed.get(str(p))
            if hit and hit[0] == stamp:
                c = hit[1]
            else:
                c = json.loads(p.read_text())
                _parsed[str(p)] = (stamp, c)
        except (OSError, ValueError):
            continue
        if c.get("instance") == name:
            out.append(c)
    return sorted(out, key=lambda c: c.get("created", ""), reverse=True)


def _activity(chat: dict) -> tuple[str, str]:
    turns = chat.get("turns") or []
    return (turns[-1]["ts"] if turns else chat.get("created", ""), chat.get("created", ""))


def threads(name: str) -> list[dict]:
    """Open conversations, most recently active first."""
    return sorted((c for c in chats(name) if c.get("state") != "ended"), key=_activity, reverse=True)


def current(name: str) -> dict | None:
    return next(iter(threads(name)), None)


def thread_of(name: str, cid: str, *, writing: bool = True) -> dict:
    chat = load(cid)
    if not chat or chat.get("instance") != name:
        raise Refused(f"not a chat of {name}")
    if writing and chat.get("state") == "ended":
        raise Refused("that thread has ended: start a new one")
    return chat


def _title(text: str) -> str:
    return " ".join(text.split())[:80]


def title(chat: dict) -> str:
    return chat.get("title") or excerpt(chat)[0][:80] or "New thread"


def rename(name: str, cid: str, title: str) -> str:
    with _inst_lock(name):
        thread_of(name, cid)
        value = _title(title)
        if not value:
            raise Refused("a thread needs a title")
        with _lock(cid):
            chat = thread_of(name, cid)
            chat["title"] = value
            save(chat)
    return "thread renamed"


# A chat being looked at, per paired device: its feed is refreshed every 1.5 s while its page is visible (a hidden
# page does not refresh it). A reply that ends while a device looks at it needs no notification on that device — only
# on that one: a desktop tab left open silenced every push to the phone (W9, review 2026-10-24). A page that goes
# hidden (the phone locks, the app goes to the background) says so (?away=1), so the next 6 s are not silent either.
# Both say when the page sent them, on its own clock (`at`, ms): a poll sent before the latest away but arriving after
# it does not make the device watching again (Grok review of 8735c62).
WATCH_S = 6.0
_watched: dict[tuple[str, str, str], float] = {}
_away: dict[tuple[str, str, str], float] = {}


def watching(name: str, device: str, at: float | None = None, cid: str | None = None) -> None:
    cid = cid if cid is not None else (shown(name) or {}).get("id", "")
    if at is not None and at <= _away.get((name, cid, device), float("-inf")):
        return
    _watched[(name, cid, device)] = time.monotonic()


def unwatch(name: str, device: str, at: float | None = None, cid: str | None = None) -> None:
    cid = cid if cid is not None else (shown(name) or {}).get("id", "")
    _watched.pop((name, cid, device), None)
    if at is not None:
        _away[(name, cid, device)] = max(at, _away.get((name, cid, device), float("-inf")))


def watchers(name: str, cid: str | None = None) -> set[str]:
    """Only devices looking at this thread suppress its push."""
    cid = cid if cid is not None else (shown(name) or {}).get("id", "")
    now = time.monotonic()
    return {d for (n, c, d), at in list(_watched.items()) if n == name and c == cid and now - at < WATCH_S}


def reply_event(chat: dict) -> dict:
    """What the push for this chat's reply shows: its first line. A reply that failed says so (a stopped one is never
    pushed): on 2026-10-29 tundra's upstream timeout reached the phone as "tundra replied"."""
    name = chat["instance"]
    t = next((t for t in reversed(chat.get("turns") or []) if t["role"] == "assistant"), {})
    first = first_line(t.get("content") or "") or (t.get("note") or "")          # the text, not its Markdown
    title = f"{name}'s reply failed" if t.get("note") else f"{name} replied"
    return {"title": title, "body": first[:160], "url": f"/i/{name}/chat?id={chat['id']}", "cid": chat["id"], "tag": name}


def _instance_stamps(chat: dict | None, sent: list[dict]) -> list[str]:
    return ([t["ts"] for t in (chat or {}).get("turns") or [] if t["role"] == "assistant"]
            + [m["ts"] for m in sent if m.get("to") == "operator"])


def last_count(chat: dict | None, sent: list[dict]) -> int:
    """How many instance items share `last`: another in the same second must make the chat unread again."""
    return _instance_stamps(chat, sent).count(last_from(chat, sent))


def last_from(chat: dict | None, sent: list[dict]) -> str | None:
    """The newest thing from the instance: an answer in `chat`, or a message to the operator among `sent` — what the
    rail's `last` and a feed's `last` both are, so the apps' unread mark (web_seen) never drifts from the rail. None
    when there is neither."""
    return max(_instance_stamps(chat, sent), default="") or None


def _short_line(line: str) -> str:
    return line[:80] + ("…" if len(line) > 80 else "")


def summary(name: str, history: list[dict] = ()) -> dict:
    """For the rail: the last line of the chat its page shows (the most recently active open thread, else the newest
    chat — the one `shown` and the feed's `last` read, so the apps' unread mark never compares two chats), when the
    instance last answered in it or sent the operator a message (`history`: messages.history(); its send_message cards
    show in the chat over the span sent_during gives, and one never lit the rail: W8, review 2026-10-24), and whether
    any of its threads is replying."""
    c = current(name) or next(iter(chats(name)), None)
    since = (c.get("cleared") or c["created"]) if c else ""
    sent_all = [m for m in history if m["from"] == name and m["to"] == "operator" and m["ts"] >= since
                and (since or not m.get("read"))]
    sent = next(iter(reversed(sent_all)), None)
    sent_line = first_line((sent or {}).get("text", ""))
    if not c or c.get("cleared"):
        return {"line": _short_line(sent_line), "last": last_from(None, sent_all),
                "last_count": last_count(None, sent_all)} if sent else {}
    turns = c.get("turns") or []
    text = (turns[-1].get("shown", turns[-1].get("content")) or "") if turns else ""
    who = "you: " if turns and turns[-1]["role"] == "user" else ""
    # The instance's reply as the page shows it, not its Markdown (the rail read "Finished the **smoke** reply.");
    # the operator's own text as they wrote it, as the chat shows it, without the lines they quoted
    first = (next((l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith(">")), "") if who
             else first_line(text, quotes=False))
    if sent and sent["ts"] >= (turns[-1]["ts"] if turns else ""):
        who, first = "", sent_line
    return {"line": _short_line(who + first), "last": last_from(c, sent_all) or "",
            "last_count": last_count(c, sent_all),
            "replying": any(c.get("state") == "replying" for c in threads(name))}


def shown(name: str) -> dict | None:
    """The chat its page shows: the one going on, else the last one (ended: the next message starts a new one) — unless
    the operator cleared it: then none, until they write again."""
    c = current(name) or next(iter(chats(name)), None)
    return None if c and c.get("cleared") else c


def sent_during(name: str, chat: dict | None, archived: bool = False) -> list[dict]:
    """Messages from this thread's talks. Legacy rows retain their time-span display."""
    from . import messages
    if chat:
        start, end = chat["created"], (chat.get("ended") or "9999") if archived else "9999"
    else:
        last = next(iter(chats(name)), None)
        start, end = (last.get("cleared") or last.get("ended") or last["created"]) if last else "", "9999"
    talks = (chat or {}).get("talks", [])
    rows = [m for m in messages.history() if (
        (m.get("talk") in talks and m.get("talk") is not None and m.get("via") != messages.BACKGROUND)
        if "talk" in m else start <= m["ts"] <= end) and (
        (m["from"] == name and m["to"] == "operator") or
        (m["from"] == "operator" and m["to"] == name and m.get("via") == messages.BACKGROUND))]
    return rows if chat or start else [m for m in rows if not m["read"]]


def thread_of_talk(name: str, talk: str | None) -> str | None:
    """The web thread whose replies ran as conversation `talk`, or None: a cycle (no talk), or a conversation that
    is not a web thread (a terminal talk, Telegram). The one rule the From feed and its pushes (web_alerts) share."""
    if not talk:
        return None
    return next((c["id"] for c in chats(name) if talk in (c.get("talks") or [])), None)


def from_feed(name: str) -> dict:
    """What the instance sent outside any web thread — from a cycle, or during a terminal or Telegram conversation,
    which has a talk id but no web thread (it showed nowhere on the web before this rule) — and the operator's
    next-cycle notes, with revisions over the whole feed. Rows without the `talk` key are legacy: they keep showing by
    time span in the chats (sent_during)."""
    from . import messages
    own = {t for c in chats(name) for t in c.get("talks") or []}
    rows = sorted((m for m in messages.history() if "talk" in m and (
        (m["from"] == name and m["to"] == "operator" and m["talk"] not in own) or
        (m["from"] == "operator" and m["to"] == name and m.get("via") == messages.BACKGROUND))),
        key=lambda m: m["ts"])
    model = build(name, None, rows)
    model["earlier"] = max(0, len(rows) - CHAT_WINDOW)
    model["items"] = model["items"][-CHAT_WINDOW:]
    return model


def excerpt(chat: dict) -> tuple[str, int]:
    """(the first line of the operator's first message, at most 80 characters; how many messages they sent in it): how
    Earlier chats lists a chat."""
    users = [t for t in chat.get("turns") or [] if t["role"] == "user"]
    text = users[0].get("shown", users[0].get("content", "")) if users else ""
    first = (text or "").splitlines()
    line = first[0].strip() if first else ""
    return _short_line(line), len(users)


# -- what a chat shows (docs/app-api.md §13): one model for the HTML feed and the apps' JSON --------------------------
# web_views.chat_feed renders build(); app_api serialises it. A fix to one cannot miss the other.
CHAT_WINDOW = 80          # turns a chat shows (review C13/M21); "Show the N earlier messages" asks for them all

# Feeds held by a long-poll (app_api) wait on this: every save() of a chat — a turn, a reply's activity as it streams
# (the relay saves it every SAVE_EVERY_S) — wakes them at once. What changes elsewhere (a message it sent, a rating,
# its inbox read) is found by looking again every half second.
chat_saved = threading.Condition()


def wait_change(timeout: float) -> None:
    """Until a chat is saved, or `timeout` seconds."""
    with chat_saved:
        chat_saved.wait(timeout)


def _rev(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                          .encode()).hexdigest()[:16]


FAILURE_WHY = {"connection": "The connection to the instance failed.",
               "service": "The service could not finish the reply.",
               "model": "The model could not finish the reply."}
RETRY_CONFIRM = "Actions may already have run. Send it again anyway?"


def failure(name: str, turn: dict, archived: bool, k: int = -1, asked: dict | None = None) -> dict | None:
    """Why a reply failed and how to recover, in words (raw errors stay available as `detail`). Retry is offered whenever
    there is a message to send again: in one tap when the failed request ran no tools (note_retry), folding the failed
    try (`retry_of`); else after a confirm, as a new message — the failed try stays in view and in the conversation,
    since what it ran may matter. `asked`: the turn before, for a failure saved when retry_text was kept only for a safe
    retry. None for a reply that ended normally."""
    note = turn.get("note") or ""
    if not note:
        return None
    kind = turn.get("note_kind", "stopped" if note == "stopped by you" else "unknown")
    if kind == "stopped":
        return {"kind": "stopped", "why": "You stopped this reply.", "action": None, "detail": note, "retry": None,
                "links": []}
    safe = bool(turn.get("note_retry") and turn.get("retry_text"))
    # An interjection before the failure means one message cannot stand for the request: nothing to fall back on.
    text = turn.get("retry_text") or (
        asked.get("shown", asked.get("content", "")) if asked and asked.get("role") == "user"
        and not asked.get("interjected") else "")
    text = "" if archived else text
    links = [{"label": "Activity", "url": f"/i/{name}/activity"}, {"label": "Services", "url": "/services"}]
    if safe and text:
        action, retry, links = "No external actions ran: Retry sends it again.", {
            "text": text, "retry_of": k, "confirm": None}, []
    elif text.strip():
        action, retry = ("Actions may already have run. Check Activity and Services before you retry.",
                         {"text": text, "retry_of": None, "confirm": RETRY_CONFIRM})
    else:
        action, retry = "Actions may already have run. Check Activity and Services before sending again.", None
    return {"kind": kind, "why": FAILURE_WHY.get(kind, "The reply failed."), "action": action, "detail": note,
            "retry": retry, "links": links}


def _attachments(atts: list[dict] | None, where: str) -> list[dict]:
    """A message's files: `path` names it for the HTML (/chatfiles/, /files/) and the apps (/app/v1/file/)."""
    from .web_views import IMAGE_TYPES, TEXT_TYPES
    return [{"name": a["name"], "bytes": a.get("bytes", 0), "image": a["name"].lower().endswith(IMAGE_TYPES),
             "text": a["name"].lower().endswith(TEXT_TYPES), "path": f"{where}/{j}"} for j, a in enumerate(atts or [])]


def _rating(item: dict | None, pending: set[str]) -> dict | None:
    if not item:
        return None
    return {"id": item["id"], "thumb": item["thumb"], "reason": item.get("reason") or "", "sent": item.get("sent"),
            "pending": item["id"] in pending}


def message_item(name: str, m: dict, ratings: dict, pending: set[str] = frozenset()) -> dict:
    """A logged message in the chat: one the instance sent the operator (`sent`), or one the operator left for its next
    cycle (`later`, messages.BACKGROUND). `ratings`: feedback.by_target(name)."""
    from urllib.parse import quote
    from . import feedback
    f = quote(m["file"], safe="")
    atts = _attachments(m.get("attachments"), f"message/{m['file']}")
    if m.get("from") == "operator":
        return {"key": f"b{f}", "type": "later", "file": m["file"], "ts": m["ts"], "text": m.get("shown", m["text"]),
                "mid": m.get("mid") or None, "read": bool(m.get("read")), "attachments": atts,
                "comments": [{"id": c["id"], "doc": c["doc"]} for c in m.get("comments") or []]}
    return {"key": f"s{f}", "type": "sent", "file": m["file"], "ts": m["ts"],
            "kind": "question" if m.get("kind") == "question" else "message", "read": bool(m.get("read")),
            "text": m["text"], "attachments": atts,
            "rating": _rating(ratings.get(feedback.key({"kind": "message", "file": m["file"]})), pending)}


def _turn_items(name: str, chat: dict, archived: bool, ratings: dict, pending: set[str]) -> list[dict]:
    from . import feedback
    turns = chat["turns"]
    last_answer = max((k for k, t in enumerate(turns) if t["role"] != "user" and not t.get("retried")), default=-1)
    items = []
    for k, t in enumerate(turns):
        if t.get("retried"):                     # a try that failed and was sent again (Retry): one quiet line
            if t["role"] != "user":
                items.append({"key": f"a{k}", "type": "retried", "k": k, "ts": t["ts"]})
            continue
        if t["role"] == "user":
            items.append({"key": f"u{k}", "type": "user", "k": k, "ts": t["ts"],
                          "text": t.get("shown", t.get("content", "")), "mid": t.get("mid") or None,
                          "interjected": bool(t.get("interjected")),
                          "via": "voice" if t.get("via") == "voice" else None,
                          "attachments": _attachments(t.get("attachments"), f"chat/{chat['id']}/{k}"),
                          "edits": list(t.get("edits") or []), "deleted": list(t.get("deleted") or []),
                          "comments": [{"id": c["id"], "doc": c["doc"]} for c in t.get("comments") or []],
                          "feedback": list(t.get("feedback") or [])})
            continue
        items.append({"key": f"a{k}", "type": "assistant", "k": k, "ts": t["ts"], "content": t.get("content") or "",
                      "activity": list(t.get("activity") or []), "incomplete": bool(t.get("content") and t.get("note")),
                      "failure": failure(name, t, archived, k, turns[k - 1] if k else None),
                      "docs": [{"path": d["path"], "created": bool(d.get("created"))} for d in t.get("docs") or []],
                      "answered": [{"id": c["id"], "doc": c["doc"]} for c in t.get("answered") or []],
                      "opened": [{"id": c["id"], "doc": c["doc"]} for c in t.get("opened") or []],
                      "rating": _rating(ratings.get(feedback.key({"kind": "chat", "chat": chat["id"], "k": k})), pending),
                      "last": k == last_answer})
    return items


def reply_of(chat: dict | None) -> dict | None:
    """The reply running now in `chat`: when it began, what it has done so far, the answer as it streams. None when
    none runs."""
    if not chat or chat.get("state") != "replying":
        return None
    r = chat.get("reply") or {}
    return {"started": r.get("started") or "", "activity": list(r.get("activity") or []), "answer": r.get("answer") or ""}


def sent_mids(chat: dict | None, sent: list[dict]) -> list[str]:
    """Every message id this chat holds — its sent turns, its queued ones, the Next-cycle messages over its span: a
    client's "sending" copy whose id is here has arrived."""
    c = chat or {}
    return list(dict.fromkeys(c.get("mids", []) + [t["mid"] for t in c.get("turns", []) + c.get("queued", [])
                                                   if t.get("mid")]
                              + [m["mid"] for m in sent if m.get("from") == "operator" and m.get("mid")]))


def build(name: str, chat: dict | None, sent: list[dict], archived: bool = False, whole: bool = False) -> dict:
    """What a chat shows (docs/app-api.md §4.2, before app_api.to_json stamps its times and turns each file's `path`
    into a URL): its turns, its talk messages (`sent`: sent_during), the reply running now, what is queued, the
    thumbs down waiting for the next message. `archived`: a past chat's page, where nothing acts on the current chat.
    The last CHAT_WINDOW turns unless `whole`; `version` and `live` are computed over every turn, before the window."""
    from . import feedback
    ratings = feedback.by_target(name)
    current = {r["id"]: r for r in ratings.values() if not r["sent"]}
    pending = [] if archived or not chat else [
        {"key": f"queue-feedback-{p['id']}", "id": p["id"], "reply_ts": current[p["id"]]["reply_ts"]}
        for p in chat.get("pending_feedback", []) if p["id"] in current]
    ids = {p["id"] for p in pending}
    items = [message_item(name, m, ratings, ids) for m in sent]
    reply = reply_of(chat)
    if chat:
        items = _turn_items(name, chat, archived, ratings, ids) + items
        if chat["state"] == "ended":         # in time order: what it sent you after the chat ended shows below the line
            items.append({"key": "ended", "type": "ended", "ts": chat.get("ended", "")})
        items.sort(key=lambda i: i["ts"])
    model = {"instance": name, "partial": False, "version": "", "live": "", "reply": reply,
             "chat": {"id": chat["id"], "title": title(chat), "state": chat["state"], "created": chat.get("created", ""),
                      "ended": chat.get("ended") or None} if chat else None,
             "archived": archived, "earlier": 0, "items": items,
             "queued": [{"key": f"queue-{t['mid']}", "mid": t["mid"], "text": t.get("shown", ""),
                         "attachments": [{"name": a["name"], "bytes": a.get("bytes", 0)} for a in t.get("attachments", [])]}
                        for t in (chat or {}).get("queued", [])],
             "pending_feedback": pending,
             "pending_edits": sorted(chat.get("pending_edits") or {}) if chat and not archived else [],
             "mids": sent_mids(chat, sent), "last": last_from(chat, sent),
             "can_stop": reply is not None and not archived,
             "can_end": bool(chat and not archived and chat["state"] not in ("replying", "ended") and chat["turns"]),
             "can_clear": bool(chat) and not archived}
    model["version"] = _rev({**model, "reply": reply and {"started": reply["started"]}})
    model["live"] = _rev({"activity": reply["activity"], "answer": reply["answer"]}) if reply else ""
    # Internal seen tie-breaker, over the whole feed; keep it out of the public JSON and its revisions.
    model["last_count"] = last_count(chat, sent)
    first = 0 if whole or not chat else max(0, len(chat["turns"]) - CHAT_WINDOW)
    if first:
        since = chat["turns"][first]["ts"]
        model["earlier"] = first
        model["items"] = [i for i in items if (i["k"] >= first if "k" in i else
                                               i["type"] == "ended" or i["ts"] >= since)]
    return model


def feed(name: str, cid: str | None = None, whole: bool = False) -> dict | None:
    """What `name`'s chat shows now (build): the current one (`shown`), or its chat `cid` — read-only when that one has
    ended. None when `cid` is not a chat of `name`."""
    if cid is None:
        chat, archived = shown(name), False
    else:
        chat = load(cid)
        if not chat or chat.get("instance") != name:
            return None
        archived = chat.get("state") == "ended"
    return build(name, chat, sent_during(name, chat, archived), archived, whole)


def new_thread(name: str, login: str, title: str = "") -> dict:
    chat = {"id": f"web-{secrets.token_hex(8)}", "instance": name, "login": login, "created": _now(), "state": "idle",
            "turns": [], "reply": None}
    if value := _title(title):
        chat["title"] = value
    with _inst_lock(name):
        _adopt_edits(name, chat)
        save(chat)
    return chat


def _adopt_edits(name: str, chat: dict) -> None:
    """Edits to its documents saved but not yet sent, left on a chat that was ended or cleared since: the new chat
    takes them, so the instance still hears of them with the next message (they were lost with the old chat)."""
    for old in chats(name):
        if (old["id"] == chat["id"] or not old.get("pending_edits")
                or (old.get("state") != "ended" and not old.get("cleared"))):
            continue
        with _lock(old["id"]):
            o = load(old["id"])
            pend = o.pop("pending_edits", None) or {}
            save(o)
        for path, e in pend.items():
            chat.setdefault("pending_edits", {}).setdefault(path, e)


def pending_feedback(chat: dict) -> list[dict]:
    """Only current, unsent ratings can accompany another user turn."""
    from . import feedback
    current = {r["id"] for r in feedback.items(chat["instance"]) if not r["sent"]}
    return [p for p in chat.get("pending_feedback", []) if p["id"] in current]


def send_feedback(name: str, item: dict, text: str) -> str:
    """Called under the instance lock, after validating the recorded reply."""
    from . import feedback
    target = item["target"]
    chat = load(target["chat"]) if target["kind"] == "chat" else None   # a chat gone or not this instance's: delivered
    if item["sent"]:
        raise ValueError("That feedback was already delivered.")
    if chat and chat.get("instance") == name and chat.get("state") != "ended" and not chat.get("cleared"):
        with _lock(chat["id"]):
            chat = load(chat["id"])
            if any(p["id"] == item["id"] for p in pending_feedback(chat)):
                raise ValueError("That feedback is already waiting for your next message.")
            if item["thumb"] == "down":
                chat["pending_feedback"] = pending_feedback(chat) + [{"id": item["id"], "note": text}]
                save(chat)
                return "Feedback goes with your next message."
    feedback.deliver(item, text)
    return "Feedback delivered to its inbox."


def unpend_feedback(name: str, item: dict) -> None:
    target = item["target"]
    if target["kind"] != "chat":
        raise ValueError("That feedback is not pending in a chat.")
    with _lock(target["chat"]):
        chat = load(target["chat"])
        if chat:
            chat["pending_feedback"] = [p for p in pending_feedback(chat) if p["id"] != item["id"]]
            save(chat)


def _take_feedback(chat: dict, turn: dict) -> None:
    pending = pending_feedback(chat)
    chat.pop("pending_feedback", None)
    if pending:
        turn["content"] += "\n\n" + "\n\n".join(p["note"] for p in pending)
        turn["feedback"] = [p["id"] for p in pending]


def _deliver_pending_feedback(chat: dict) -> None:
    from . import feedback
    current = {r["id"]: r for r in feedback.items(chat["instance"])}
    for p in pending_feedback(chat):
        feedback.deliver(current[p["id"]], p["note"])
    chat.pop("pending_feedback", None)


def _flush_queue(chat: dict) -> bool:
    """#70: the queued messages become one user turn (in order, attachments combined), with the document edits and
    comment notices that were waiting, taken now; the caller starts the reply. True when there was something."""
    from . import doc_comments
    queued = chat.get("queued") or []
    if not queued:
        return False
    turn = {"role": "user", "content": "\n\n".join(t["content"] for t in queued),
            "shown": "\n\n".join(t.get("shown", "") for t in queued), "ts": _now(),
            "attachments": [a for t in queued for a in t.get("attachments", [])]}
    if queued[-1].get("via") == "voice":           # the reply is heard as the latest message was said
        turn["via"] = "voice"
    _take_feedback(chat, turn)
    edits, paths, deleted = _take_edits(chat)
    if edits:
        turn["content"] = (turn["content"] + "\n\n" + edits).strip()
        if paths:
            turn["edits"] = paths
        if deleted:
            turn["deleted"] = deleted
    notices = doc_comments.take_notices(chat["instance"])
    if notices:
        turn["content"] += "\n\n" + "\n\n".join(notices)
    chat["turns"].append(turn)
    chat["queued"] = []
    return True


def reap() -> None:
    """At start: a chat whose relay died with the previous web server is not replying any more (the API stopped that
    reply when its connection went), and says so."""
    for p in _dir().glob("web-*.json") if _dir().is_dir() else []:
        try:
            c = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if c.get("state") == "replying":
            r = c.get("reply") or {}
            # The request may have reached the API and run tools, so this is not a safe retry: Retry asks first.
            asked = next((t for t in reversed(c["turns"]) if t["role"] == "user"), {})
            c["turns"].append({"role": "assistant", "content": r.get("answer") or "", "ts": _now(),
                               "activity": r.get("activity", []),
                               "note": "interrupted: the web service restarted while it replied",
                               "note_kind": "service", "note_retry": False,
                               "retry_text": "" if r.get("interjections") else asked.get("shown", asked.get("content", ""))})
            c["state"], c["reply"] = "idle", None
            if _flush_queue(c):                     # #70: what was queued for after that reply goes now
                c["state"], c["reply"] = "replying", {"started": _now(), "activity": [], "answer": "",
                                                      "interjections": 0, "docs_before": _docs_now(c["instance"]),
                                                      "turn_start": len(c["turns"])}
                save(c)
                _start(c["id"])
                continue
            save(c)


# -- the API, as the operator -----------------------------------------------------------------------------------------
def _api_conn(timeout: float) -> http.client.HTTPConnection:
    from . import api
    return http.client.HTTPConnection("127.0.0.1", api.PORT, timeout=timeout)


def _headers(chat: dict) -> dict:
    from .core import secrets_env
    key = secrets_env().get("SHAFT_API_KEY")
    if not key:
        raise Refused("the API is not set up: shaft api on")
    return {"Content-Type": "application/json", "Authorization": f"Bearer {key}", "Tailscale-User-Login": chat["login"],
            "X-Shaft-Conversation": chat["id"], "X-Shaft-Thread-Title": quote(title(chat), safe=""),
            "X-Shaft-Channel": "web", "User-Agent": "SHAFT web UI"}


def _post(chat: dict, route: str, body: dict, timeout: float = 15) -> tuple[int, dict]:
    conn = _api_conn(timeout)
    try:
        conn.request("POST", f"/v1/chat/{route}", json.dumps(body), _headers(chat))
        r = conn.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw or b"{}")
        except ValueError:
            return r.status, {"error": {"message": raw.decode(errors="replace")[:300]}}
    except OSError as e:
        raise Refused(f"the API is not reachable ({e}): shaft api status") from None
    finally:
        conn.close()


def spoken(chat: dict) -> bool:
    """The operator's last message in the conversation was spoken (`via: voice`): the API is told so."""
    last = next((t for t in reversed(chat.get("turns") or []) if t["role"] == "user" and not t.get("retried")), {})
    return last.get("via") == "voice"


def _api_messages(chat: dict) -> list[dict]:
    """The conversation for the API — without a try that failed and was retried (mark_retried): the model would read the
    question twice and a failed answer between. A short digest names the other open threads."""
    others = [c for c in threads(chat["instance"]) if c["id"] != chat["id"]]
    digest = []
    for c in others:
        last = {role: next((t.get("content", "").split("\n", 1)[0] for t in reversed(c["turns"])
                           if t["role"] == role and not t.get("retried")), "") for role in ("user", "assistant")}
        digest.append(f"- {c['id']} «{title(c)}» (last moved {_activity(c)[0]}): "
                      f"you: {last['user']} / me: {last['assistant']}")
    prefix = [{"role": "system", "content": "Other open threads:\n" + "\n".join(digest)
               + "\nread_thread reads any of them."}] if digest else []
    return prefix + [{"role": t["role"], "content": t["content"]} for t in chat["turns"]
            if not t.get("retried") and (t.get("content") or t["role"] == "user")]


def mark_retried(chat: dict, k: int, text: str) -> bool:
    """Retry in place (iPad review P9): the failed reply `k` and the question it answered are kept, marked retried —
    the feed folds them into one line, the API no longer sees them. Only a failure that offered Retry (note_retry: no
    tools ran), the question being the text sent again. False: nothing marked, the message is sent as any other."""
    turns = chat.get("turns") or []
    if not 0 < k < len(turns):
        return False
    failed, asked = turns[k], turns[k - 1]
    if (failed.get("role") != "assistant" or not failed.get("note_retry") or failed.get("retried")
            or asked.get("role") != "user" or (failed.get("retry_text") or "").strip() != text.strip()):
        return False
    failed["retried"] = asked["retried"] = True
    return True


# -- why a reply failed ----------------------------------------------------------------------------------------------
_STREAM_ERROR = re.compile(r"^\[error: (.*)\]$", re.DOTALL)  # older APIs put failures in content


def classify_reply(*, stopped: bool, status: int | None, exc: BaseException | None, text: str,
                   posted: bool, tools: bool | None) -> tuple[str, bool]:
    """Retry requires evidence that the request could not have run tools. Unknown means inspect first."""
    if stopped:
        return "stopped", False
    if isinstance(exc, (OSError, http.client.HTTPException)):
        return "connection", isinstance(exc, ConnectionRefusedError) and not posted
    if (status and status != 200) or isinstance(exc, Refused):
        return "service", False
    if "CloudError" in text or "OpenRouter" in text:
        code = re.search(r"\bHTTP (\d{3})\b", text)
        return "model", bool(code and 500 <= int(code[1]) < 600 and tools is False)
    return "unknown", False


# -- the relay: one reply, streamed, saved as it goes ----------------------------------------------------------------
# What the stream endpoint (app_api, GET /app/v1/chat/<n>/stream; docs/app-api.md §6.1) passes on from the API's chunks:
# its `shaft_event`, only these types and only these fields. Then `answer` and `end` when the reply ends.
STREAM_EVENTS = {"draft": ("text",), "step_end": ("answer",), "status": ("phase", "detail"),
                 "tool_start": ("name",), "tool_end": ("name", "ok")}


class Relay:
    """One reply: the API connection it reads, and its events in order for the stream endpoint, held in memory as the
    chunks arrive — never through the chat file, which is written every SAVE_EVERY_S and read by a 0.5 s poll (up to
    1.5 s added before each piece of a spoken reply)."""

    def __init__(self, cid: str):
        self.cid, self.conn, self.sock, self.stopped = cid, None, None, False
        self.id = secrets.token_hex(8)                 # the reply, as its events name it
        self.events: list[dict] = []                   # {seq, type, reply, …}; seq = index + 1
        self.changed = threading.Condition()
        self.ended: float | None = None                # time.monotonic() when `end` went in

    def emit(self, kind: str, **fields) -> None:
        """Buffer one event and wake every stream holding this reply. Nothing goes in after `end`."""
        with self.changed:
            if self.ended is not None:
                return
            self.events.append({"seq": len(self.events) + 1, "type": kind, "reply": self.id, **fields})
            if kind == "end":
                self.ended = time.monotonic()
            self.changed.notify_all()

    def events_after(self, after: int, timeout: float) -> list[dict]:
        """The events with seq > `after`, waiting up to `timeout` seconds for one when there are none yet and the reply
        has not ended."""
        with self.changed:
            if len(self.events) <= after and self.ended is None:
                self.changed.wait(timeout)
            return self.events[max(0, after):]

    def close_stream(self, answer: str, failure: str | None) -> None:
        """`answer` (the answer as the chat keeps it) and `end`, once: a stream ends when its reply does."""
        if self.ended is None:
            self.emit("answer", text=answer)
            self.emit("end", failure=failure)

    def stop(self) -> None:
        """Close the connection under the reading thread. The socket is kept from before the response: the API answers
        HTTP/1.0, and http.client then hands the socket to the response and sets conn.sock to None (Stop did nothing,
        found by its test)."""
        self.stopped = True
        sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)           # unblocks the read: the API sees its client go
            except OSError:
                pass


_relays: dict[str, Relay] = {}
_streams: dict[str, Relay] = {}            # the chat's latest reply, kept STREAM_KEEP_S after it ended


def replying(cid: str) -> bool:
    return cid in _relays


def stream_of(cid: str) -> Relay | None:
    """The chat's reply for the stream endpoint: the running one, or the last one for STREAM_KEEP_S after it ended."""
    relay = _streams.get(cid)
    if relay is not None and relay.ended is not None and time.monotonic() - relay.ended > STREAM_KEEP_S:
        _streams.pop(cid, None)
        return None
    return relay


def _start(cid: str) -> None:
    relay = Relay(cid)
    for old in [c for c, r in _streams.items() if r.ended is not None and time.monotonic() - r.ended > STREAM_KEEP_S]:
        _streams.pop(old, None)
    _relays[cid] = _streams[cid] = relay
    threading.Thread(target=_run, args=(relay,), daemon=True, name=f"chat-{cid}").start()


def _run(relay: Relay) -> None:
    cid = relay.cid
    answer, activity, error, heard, last_save = "", [], None, None, 0.0   # heard: None until the API says
    status, posted, exc, tools = None, False, None, None
    complete = False
    with _lock(cid):
        chat = load(cid)
        sent = chat["reply"].get("interjections", 0)
    try:
        relay.conn = conn = _api_conn(3600)
        body = {"model": chat["instance"], "messages": _api_messages(chat), "stream": True}
        if spoken(chat):                            # the API tells the instance it is heard aloud
            body["voice"] = True
        conn.request("POST", "/v1/chat/completions", json.dumps(body), _headers(chat))
        posted = True
        relay.sock = conn.sock
        if relay.stopped:                           # Stop came before the socket was there
            relay.stop()
        resp = conn.getresponse()
        status = resp.status
        if talk := resp.getheader("X-Shaft-Talk"):
            with _lock(cid):
                chat = load(cid)
                chat["talks"] = list(dict.fromkeys([*chat.get("talks", []), talk]))
                save(chat)
        if resp.status != 200:
            try:
                error = json.loads(resp.read())["error"]["message"]
            except (ValueError, KeyError, TypeError):
                error = f"API HTTP {resp.status}"
        else:
            for raw in resp:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                if line == "data: [DONE]":
                    complete = True
                    break
                try:
                    ev = json.loads(line[6:])
                except ValueError:
                    continue
                if ev.get("reply_error"):
                    failure = ev["reply_error"]
                    error = failure["message"]
                    tools = failure.get("tools_ran")
                if "interjections_heard" in ev:
                    heard = int(ev["interjections_heard"])
                se = ev.get("shaft_event")
                if isinstance(se, dict) and se.get("type") == "status":
                    phase = se.get("phase", "")
                    if phase == "started" or phase.startswith("waiting:"):
                        with _lock(cid):
                            chat = load(cid)
                            chat["reply"]["waiting"] = None if phase == "started" else phase
                            if phase == "started":
                                chat["reply"]["docs_before"] = _docs_now(chat["instance"])
                            save(chat)
                if isinstance(se, dict) and se.get("type") in STREAM_EVENTS:     # at once: no file, no poll between
                    relay.emit(se["type"], **{k: se.get(k) for k in STREAM_EVENTS[se["type"]]})
                delta = (ev.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("reasoning_content"):
                    activity.append(delta["reasoning_content"].strip())
                if delta.get("content"):
                    answer += delta["content"]
                if time.monotonic() - last_save >= SAVE_EVERY_S:
                    last_save = time.monotonic()
                    with _lock(cid):
                        chat = load(cid)
                        chat["reply"].update(activity=activity[-200:], answer=answer)
                        sent = chat["reply"].get("interjections", 0)
                        save(chat)
            if not complete and not error and not relay.stopped:
                raise ConnectionResetError("API stream ended before [DONE]")
    except (OSError, http.client.HTTPException, Refused) as e:
        if not relay.stopped:
            exc = e
            error = f"{type(e).__name__}: {e}"
    finally:
        try:
            if relay.conn:
                relay.conn.close()
        except OSError:
            pass
    # Older APIs put an error in the answer. Keep partial content, but remove the whole-answer error wrapper.
    if not relay.stopped:
        wrapped = _STREAM_ERROR.fullmatch(answer.strip())
        if wrapped:
            error = wrapped.group(1).strip()
            answer = ""
    # A comment send must mark its post sent before even a very fast reply is attached.
    with _inst_lock(chat["instance"]):
        docs_to_sync = _finish(relay, answer, activity, error, heard, status=status, posted=posted, exc=exc, tools=tools)
    # _finish ends the stream once the answer is saved; a reply it had nothing to save for (a chat cleared, or taken
    # over by a newer relay meanwhile) ends it here
    relay.close_stream(answer.strip(), "stopped by you" if relay.stopped else f"error: {error}" if error else None)
    _sync_copies(chat["instance"], docs_to_sync)


def _finish(relay: Relay, answer: str, activity: list, error: str | None, heard: int | None, *,
            status: int | None = None, posted: bool = True, exc: BaseException | None = None,
            tools: bool | None = None) -> list[str]:
    docs_to_sync = []
    cid = relay.cid
    with _lock(cid):
        chat = load(cid)
        # an old relay finishing twice, or after a newer one took the chat over, changes nothing (#70)
        if not chat or chat.get("cleared") or chat["state"] != "replying" or _relays.get(cid) not in (None, relay):
            return docs_to_sync
        reply = chat["reply"]
        sent = reply.get("interjections", 0)
        interjected = [t for t in chat["turns"][reply.get("turn_start", len(chat["turns"])):]
                       if t["role"] == "user" and t.get("interjected")]
        heard_ids = reply.get("comment_ids", []) + _comment_ids(interjected[:max(0, heard or 0)])
        started_by = None if sent else reply.get("started_by")
        assistant_index = len(chat["turns"])
        note = ("stopped by you" if relay.stopped else f"error: {error}" if error else "")
        cause = {}
        if note:
            kind, retry = classify_reply(stopped=relay.stopped, status=status if status and status != 200 else None,
                                         exc=exc, text=error or "", posted=posted, tools=tools)
            # Interjections and attachment-only requests cannot be faithfully retried by prefilling one message.
            original = next((t for t in reversed(chat["turns"]) if t["role"] == "user"), {})
            request = original.get("shown", original.get("content", ""))
            # retry_text: what Retry sends again; note_retry: that it is safe to (no tools ran), else Retry asks first.
            cause = {"note_kind": kind, "note_retry": retry and not sent and bool(request),
                     "retry_text": request if not sent else ""}
        from . import docs
        made = [] if reply.get("waiting") else docs.changed(reply.get("docs_before") or {}, _docs_now(chat["instance"]))
        chat["turns"].append({"role": "assistant", "content": answer.strip(), "ts": _now(),
                              "activity": activity[-200:], "note": note, **cause,
                              **({"docs": made} if made else {})})
        chat["state"], chat["reply"] = "idle", None
        # An API older than the web UI does not say what it heard: then no second reply, rather than one per message.
        unheard = max(0, sent - heard) if heard is not None and not relay.stopped and not error else 0
        queued = _flush_queue(chat)
        if unheard or queued:                     # sent too late for this reply: answered by one of their own
            chat["state"], chat["reply"] = "replying", {"started": _now(), "activity": [], "answer": "",
                                                        "interjections": 0, "followup": unheard,
                                                        "docs_before": _docs_now(chat["instance"]),
                                                        **_reply_comments(interjected[-unheard:] if unheard else []),
                                                        "turn_start": len(chat["turns"])}
        save(chat)
    relay.close_stream(answer.strip(), note or None)   # after the save: a client that reads the feed on `end` sees it
    if not note:
        from . import doc_comments
        try:
            result = doc_comments.parse_answer(chat["instance"], answer.strip(), heard_ids, started_by,
                                               f"{cid}#{assistant_index}")
            with _lock(cid):
                chat = load(cid)
                for key in ("answered", "opened"):
                    if result[key]:
                        chat["turns"][assistant_index][key] = result[key]
                save(chat)
            docs_to_sync = [item["doc"] for items in result.values() for item in items]
        except (Exception, SystemExit) as e:
            log(f"web: could not attach {cid}'s comments: {type(e).__name__}: {e}")
    if _relays.get(cid) is relay:                  # never a newer relay of the same chat
        del _relays[cid]
    if not unheard and not queued and not relay.stopped:
        from . import webpush                       # each device not looking is told
        webpush.notify(reply_event(chat), skip=watchers(chat["instance"], chat["id"]))
    log(f"web: chat {cid} with {chat['instance']}: reply {'stopped' if relay.stopped else 'failed' if error else 'done'}"
        + (f" ({unheard} interjection(s) answered next)" if unheard else ""))
    if unheard or queued:
        _start(cid)
    return docs_to_sync


# -- what the page does ----------------------------------------------------------------------------------------------
# -- the operator's edits to its documents ---------------------------------------------------
EDIT_DIFF_MAX = 12000                  # a diff longer than this is summarised: the instance reads the file itself


def _fence(text: str) -> str:
    """A code fence longer than any run of backticks in the text: a document's own ``` must not close it."""
    return "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", text)), default=0))


def edit_diff(path: str, old: str | None, new: str | None) -> str:
    """What the operator changed in a document, as the instance is told it."""
    import difflib
    rel = f"notes/docs/{path}"
    if new is None:
        return f"[The operator deleted {rel}.]"
    if old is None:
        body = new if len(new) <= EDIT_DIFF_MAX else new[:EDIT_DIFF_MAX] + "\n…(cut: read the file)"
        f = _fence(body)
        return f"[The operator created {rel}:]\n{f}markdown\n{body}\n{f}"
    diff = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{rel}", f"b/{rel}", lineterm="", n=2))
    if not diff:
        return f"[The operator saved {rel} unchanged.]"
    if len(diff) > EDIT_DIFF_MAX:
        added = sum(l.startswith("+") for l in diff.splitlines()[2:])
        dropped = sum(l.startswith("-") for l in diff.splitlines()[2:])
        return f"[The operator edited {rel}: +{added} −{dropped} lines, too long to show here — read the file.]"
    f = _fence(diff)
    return f"[The operator edited {rel}:]\n{f}diff\n{diff}\n{f}"


def doc_saved(name: str, login: str, path: str, old: str | None, new: str | None, sha: str, cid: str = "") -> str:
    """After the operator saved a document: the instance hears of it — at once (an interjection) while it replies, else
    with their next message; several saves before then are one diff (from what it last saw). The chat also keeps their
    version, so the panel can say when a later write of the instance dropped lines they wrote. (what to tell them)"""
    with _inst_lock(name):
        return _doc_saved(name, login, path, old, new, sha, cid=cid)


def _doc_saved(name: str, login: str, path: str, old: str | None, new: str | None, sha: str, cid: str = "") -> str:
    chat = thread_of(name, cid) if cid else current(name) or new_thread(name, login)
    with _lock(chat["id"]):
        chat = load(chat["id"])
        pend = chat.setdefault("pending_edits", {})
        # Folded into one diff only while nobody else wrote it in between: if the instance wrote over their last save,
        # it knows its own version, and the diff starts there (folding from the first save called its text theirs).
        prev = pend.get(path)
        first = prev["old"] if prev and prev["new"] == old else old
        pend[path] = {"old": first, "new": new}
        if new is None:
            chat.setdefault("saved_docs", {}).pop(path, None)
        else:
            added = sorted({l.strip() for l in new.splitlines() if l.strip()} - {l.strip() for l in (old or "").splitlines()})
            chat.setdefault("saved_docs", {})[path] = {"sha": sha, "text": new, "ts": _now(), "added": added[:400]}
        save(chat)
        replying_now = chat["state"] == "replying"
    verb = "deleted" if new is None else "saved"
    if replying_now:
        _, said = send(name, login, "", [], cid=chat["id"])
        return f"{verb} · {said}"
    return f"{verb} · {name} gets the change with your next message"


def _take_edits(chat: dict) -> tuple[str, list[str], list[str]]:
    """The pending edits as the message's extra text, the paths edited and the paths deleted; cleared from the chat (the
    caller saves it)."""
    pend = chat.pop("pending_edits", None) or {}
    blocks = [edit_diff(p, e["old"], e["new"]) for p, e in sorted(pend.items())]
    return ("\n\n".join(blocks), sorted(p for p, e in pend.items() if e["new"] is not None),
            sorted(p for p, e in pend.items() if e["new"] is None))


MIDS_KEPT = 50                       # the last message ids a chat remembers


def _sync_copies(name: str, docs: list[str]) -> None:
    """The CLI owns writes into the instance; an unavailable verb must not fail a chat."""
    from . import actions
    for doc in sorted(set(docs)):
        try:
            ok, last = actions.run_now(["docs", name, "comments", doc, "--sync"], who="web")
            if not ok:
                log(f"web: could not sync {name}'s comments on {doc}: {last}")
        except (Exception, SystemExit) as e:
            log(f"web: could not sync {name}'s comments on {doc}: {type(e).__name__}: {e}")


def _comment_ids(turns: list[dict]) -> list[str]:
    return [c["id"] for t in turns for c in t.get("comments", [])]


def _reply_comments(turns: list[dict]) -> dict:
    only = turns[0] if len(turns) == 1 else {}
    ids = _comment_ids(turns)
    sole = (len(only.get("comments", [])) == 1 and not only.get("shown")
            and not only.get("attachments") and not only.get("edits"))
    return {"comment_ids": ids, "started_by": ids[0] if sole else None}


def send_comment(name: str, login: str, pid: str, cid: str = "") -> tuple[dict, str]:
    """Send a stored post once; its post id also deduplicates retries before mark_sent succeeds."""
    from . import doc_comments
    with _inst_lock(name):
        try:
            if cid:
                thread_of(name, cid)
            post = doc_comments.get_post(name, pid)
            if post.get("seen"):
                return load(post["seen"].split("#", 1)[0]) or {}, f"{name} already answered it"
            if post.get("sent") or post.get("turn"):
                cid = (post.get("sent") or post["turn"]).split("#", 1)[0]
                return load(cid) or {}, f"already sent to {name}"
            # Sent before, but mark_sent failed and the chat has ended since: its mid is no longer on the open chat, so
            # send() would not catch the retry. The turn that carried it says it went (Grok review, 2026-10-20).
            for c in chats(name):
                index = next((i for i, t in enumerate(c["turns"]) if t.get("mid") == pid), None)
                if index is not None:
                    doc_comments.mark_sent(name, pid, f"{c['id']}#{index}")
                    return c, f"already sent to {name}"
            block = doc_comments.block(name, post["thread"], pid)
            chat, said = send(name, login, "", [], mid=pid, comment_block=block,
                              comments=[{"id": post["thread"], "doc": post["doc"]}], cid=cid)
            index = next(i for i, t in enumerate(chat["turns"]) if t.get("mid") == pid)
            doc_comments.mark_sent(name, pid, f"{chat['id']}#{index}")
        except (Exception, SystemExit) as e:
            raise Refused(str(e)) from e
    _sync_copies(name, [post["doc"]])
    return chat, said


def _in_chat(chat: dict | None, mid: str) -> bool:
    """The page's message id is already in this chat: sent as a turn, or queued for after the reply."""
    return bool(mid and chat and (mid in (chat.get("mids") or []) or any(t.get("mid") == mid for t in chat.get("queued", []))))


def background(name: str, login: str, text: str, files: list[tuple[str, bytes]], mid: str = "", *,
               comment_block: str = "", comments: list[dict] | None = None) -> tuple[str, str]:
    """Next cycle: the message goes to the instance's inbox now and is read in
    its next cycle — no chat turn, no reply, nothing holds the instance. Its answer, if any, is a send_message: a card
    in the chat. Files and a quoted question as send()'s. (inbox file, what to say)"""
    from . import autonomy, messages
    text = text.replace("\r\n", "\n").strip()        # a browser form's line ends: the inbox file is the instance's
    if not text and not files and not comment_block:
        raise Refused("an empty message is not sent")
    if len(text) > MAX_TEXT:
        raise Refused(f"a message is at most {MAX_TEXT} characters ({len(text)})")
    with _inst_lock(name):
        if (row := messages.sent_mid(name, mid)):
            return row["file"], f"already sent to {name}"
        if any(_in_chat(c, mid) for c in threads(name)):        # sent now (or queued) before its answer was lost: not twice
            return "", f"already sent to {name} in the chat"
        atts, lines = [], ""
        if files:
            try:
                atts, lines = messages.from_operator(name, files, "web")
            except ValueError as e:
                raise Refused(str(e)) from None
        asked = messages.quoted_questions(name, text)
        body = "\n\n".join(x for x in ((text + lines).strip(), comment_block) if x)
        try:
            path = messages.deliver_background(name, body, shown=text, attachments=atts, mid=mid, comments=comments,
                                               in_reply_to=asked[0]["path"].name if asked else None)
        except (OSError, UnsafePath) as e:            # its inbox/ is its own folder: say what stopped it, not a 500
            raise Refused(f"could not put it in {name}'s inbox: {e}") from None
    said = autonomy.next_cycle(name)
    if (answered := messages.answer_quoted(name, text)):
        log(f"web: {name}'s question {', '.join(answered)} answered by a message for its next cycle")
        said += " · answers its question"
    log(f"web: {login or 'operator'} -> {name}: for its next cycle ({path.name})")
    return path.name, said


def send_comment_background(name: str, login: str, pid: str) -> str:
    """A stored comment, sent for the instance's next cycle: its block goes to the inbox (background), the post is
    marked sent there (frozen, and a retry is refused), and the answer comes as a send_message with a "### c<n>"
    section (broker: doc_comments.parse_answer)."""
    from . import doc_comments
    with _inst_lock(name):
        try:
            post = doc_comments.get_post(name, pid)
            if post.get("sent") or post.get("turn"):
                return f"already sent to {name}"
            block = doc_comments.block(name, post["thread"], pid, cycle=True)
            # mid: the post id — after a mark_sent that failed, a retry finds the delivered message, not a second one
            file, said = background(name, login, "", [], mid=pid, comment_block=block,
                                    comments=[{"id": post["thread"], "doc": post["doc"]}])
            if not file:                          # it went in the chat already (send_comment): not to the inbox too
                return said
            doc_comments.mark_sent(name, pid, f"inbox/{file}")
        except Refused:
            raise
        except (Exception, SystemExit) as e:
            raise Refused(str(e)) from e
    _sync_copies(name, [post["doc"]])
    return said


def send(name: str, login: str, text: str, files: list[tuple[str, bytes]], mid: str = "", *,
         comment_block: str = "", comments: list[dict] | None = None, retry_of: str = "",
         voice: bool = False, cid: str = "") -> tuple[dict, str]:
    """A message from the operator: a new reply if none is running, an interjection otherwise. (chat, what to say)
    `mid`: the page's id for this message — sent again after an answer that never arrived, it is not sent twice.
    `retry_of`: the index of the failed reply this sends again (Retry): it and its question fold (mark_retried).
    `voice`: spoken and transcribed on the device: the turn is `via: voice`, and the API is told."""
    from . import messages
    with _inst_lock(name):
        # A message sent again (its answer was lost) is found in whichever thread it landed in, before the thread
        # named is checked: that one may have ended since.
        if found := next((t for t in threads(name) if _in_chat(t, mid)), None):
            return found, f"already sent to {name}"
        c = thread_of(name, cid) if cid else current(name)
        if messages.sent_mid(name, mid):          # the same message, sent for its next cycle before its answer was lost
            return current(name) or {}, f"already sent to {name} for its next cycle"
        if retry_of.isdigit() and c and c.get("state") != "replying" and not files:
            with _lock(c["id"]):
                c = load(c["id"])
                if mark_retried(c, int(retry_of), text):
                    save(c)
        chat, said = _send(name, login, text, files, comment_block=comment_block, comments=comments, mid=mid,
                           voice=voice, cid=cid)
    # a question it quotes is answered, as a Telegram reply answers it
    if (answered := messages.answer_quoted(name, text)):
        log(f"web: {name}'s question {', '.join(answered)} answered in the web chat")
        said += " · answers its question"
    return chat, said


def queue(name: str, login: str, text: str, files: list[tuple[str, bytes]], mid: str = "",
          voice: bool = False, cid: str = "") -> tuple[dict, str]:
    """Keep a message for the next reply; an idle chat sends it now."""
    from . import messages
    with _inst_lock(name):
        chat = thread_of(name, cid) if cid else current(name)
        if not chat or chat["state"] != "replying":
            return send(name, login, text, files, mid=mid, voice=voice, cid=cid)
        if messages.sent_mid(name, mid):
            return chat, f"already sent to {name} for its next cycle"
        if found := next((t for t in threads(name) if _in_chat(t, mid)), None):
            return found, f"already sent to {name}"
        return _send(name, login, text, files, mid=mid or secrets.token_hex(16), queued=True, voice=voice, cid=chat["id"])


def unqueue(name: str, mid: str, cid: str = "") -> str:
    with _inst_lock(name):
        chat = thread_of(name, cid) if cid else current(name)
        if chat:
            with _lock(chat["id"]):
                chat = load(chat["id"])
                chat["queued"] = [t for t in chat.get("queued", []) if t.get("mid") != mid]
                save(chat)
    return "queued message cancelled"


def _send(name: str, login: str, text: str, files: list[tuple[str, bytes]], *,
          comment_block: str = "", comments: list[dict] | None = None, mid: str = "", queued: bool = False,
          voice: bool = False, cid: str = "") -> tuple[dict, str]:
    from . import messages, doc_comments
    text = text.strip()
    chat = thread_of(name, cid) if cid else current(name)
    if not text and not files and not comment_block and not (chat or {}).get("pending_edits"):
        raise Refused("an empty message is not sent")
    if len(text) > MAX_TEXT:
        raise Refused(f"a message is at most {MAX_TEXT} characters ({len(text)})")
    chat = chat or new_thread(name, login)
    cid = chat["id"]
    atts, lines = ([], "")
    if files:
        try:
            atts, lines = messages.from_operator(name, files, "web")
        except ValueError as e:
            raise Refused(str(e)) from None
    turn = {"role": "user", "content": (text + lines).strip(), "shown": text, "ts": _now(),
            "attachments": [{k: a[k] for k in ("name", "path", "bytes")} for a in atts]}
    if comment_block:
        turn["content"] = (turn["content"] + "\n\n" + comment_block).strip()
    if comments:
        turn["comments"] = comments
    if mid:
        turn["mid"] = mid
    if voice:
        turn["via"] = "voice"
    if queued:                                       # #70: kept raw; edits and notices go with it when it is sent
        with _lock(cid):
            chat = load(cid)
            if chat["state"] == "replying":
                if mid:
                    chat["mids"] = ((chat.get("mids") or []) + [mid])[-MIDS_KEPT:]
                chat.setdefault("queued", []).append(turn)
                save(chat)
                return chat, "queued — sent when this reply ends"
    notices = doc_comments.take_notices(name)
    try:
        waiting = doc_comments.waiting(name, exclude=mid)
        with _lock(cid):                               # the relay's last save takes it too: the chat's state is the truth
            chat = load(cid)
            chat["login"] = login
            if mid:
                chat["mids"] = ((chat.get("mids") or []) + [mid])[-MIDS_KEPT:]
            edits, paths, deleted = _take_edits(chat)
            if edits:
                turn["content"] = (turn["content"] + "\n\n" + edits).strip()
                if paths:
                    turn["edits"] = paths
                if deleted:
                    turn["deleted"] = deleted
            if notices:
                turn["content"] += "\n\n" + "\n\n".join(notices)
            if waiting:
                turn["content"] += "\n\n" + waiting
            if not any(t["role"] == "user" for t in chat["turns"]):
                turn["content"] += "\n\n" + DOCS_NOTE
            if chat["state"] == "replying":
                status, body = _post(chat, "interject", {"model": name, "messages": _api_messages(chat) + [
                    {"role": "user", "content": turn["content"]}], **({"voice": True} if voice else {})})
                if status not in (202, 409):
                    raise Refused(body.get("error", {}).get("message") or f"API HTTP {status}")
                turn["interjected"] = True
                chat["turns"].append(turn)
                # Counted as sent either way: one the API took but the reply did not hear in time, or one it refused (409:
                # the reply was ending), is answered by a reply of its own when this one ends.
                chat["reply"]["interjections"] = chat["reply"].get("interjections", 0) + 1
                save(chat)
                return chat, (f"{name} reads it at its next step" if status == 202 else
                              f"{name} was just finishing: it answers this next")
            _take_feedback(chat, turn)
            chat["turns"].append(turn)
            last_answer = max((i for i, t in enumerate(chat["turns"]) if t["role"] == "assistant"), default=-1)
            starting = [t for t in chat["turns"][last_answer + 1:]
                        if t["role"] == "user" and not t.get("interjected")]
            chat["state"], chat["reply"] = "replying", {"started": _now(), "activity": [], "answer": "", "interjections": 0,
                                                        "docs_before": _docs_now(name),
                                                        **_reply_comments(starting),
                                                        "turn_start": len(chat["turns"])}
            save(chat)
        _start(cid)
        return chat, f"sent to {name}"
    except BaseException:
        doc_comments.put_back_notices(name, notices)
        raise


def stop(cid: str) -> str:
    relay = _relays.get(cid)
    if not relay:
        raise Refused("no reply is running")
    relay.stop()
    return "stopping the reply"


def end(cid: str) -> str:
    chat = load(cid)
    if not chat:
        raise Refused("this chat is already over")
    with _inst_lock(chat["instance"]):
        return _end(cid)


def _end(cid: str) -> str:
    chat = load(cid)                                # again, under the lock: a message may have come meanwhile
    if not chat or chat.get("state") == "ended":
        raise Refused("this chat is already over")
    if replying(cid) or chat.get("state") == "replying":
        raise Refused("it is still replying: stop the reply first")
    status, body = _post(chat, "end", {"model": chat["instance"]})
    err = body.get("error") or {}
    if status == 404 and err.get("type") != "not_found":   # "no route": an API older than the web UI
        raise Refused("the running API is older than the web UI and cannot end a conversation: restart it "
                      "(shaft api on); until then it is written up after SHAFT_IDLE_MINUTES of silence")
    if status not in (202, 404):                    # 404 not_found: idle already closed it and scheduled a write-up
        raise Refused(err.get("message") or f"API HTTP {status}")
    with _lock(cid):
        chat = load(cid)
        if chat.get("state") == "replying":         # a reply of its own began meanwhile (a message heard too late)
            raise Refused("a reply began meanwhile: it is written up when that reply ends and the chat is quiet")
        _deliver_pending_feedback(chat)
        chat["state"], chat["ended"] = "ended", _now()
        save(chat)
    log(f"web: chat {cid} with {chat['instance']} ended by the operator")
    return "ended: journal write-up requested" if status == 202 else "ended"


def clear(name: str, cid: str = "") -> str:
    """Clear one thread from view. It stays in Earlier web chats and Transcripts. An open conversation is ended
    first, requesting a journal write-up. A thread that is replying cannot be cleared.
    `cid`: only this thread is cleared. An ended chat must still be the one shown."""
    with _inst_lock(name):
        return _clear(name, cid)


def _clear(name: str, cid: str) -> str:
    chat = thread_of(name, cid, writing=False)
    if chat.get("cleared"):
        raise Refused("there is nothing to clear")
    if chat.get("state") == "ended" and (shown(name) or {}).get("id") != cid:
        raise Refused("that is not the chat on this page any more: reload it")
    if replying(chat["id"]) or chat.get("state") == "replying":
        raise Refused("it is still replying: stop the reply first")
    said = end(chat["id"]) if chat.get("state") != "ended" else ""
    with _lock(chat["id"]):
        c = load(chat["id"])
        c["cleared"] = _now()                 # edits still unsent stay: the next chat takes them (_adopt_edits)
        save(c)
    log(f"web: chat {chat['id']} with {name} cleared by the operator")
    return "cleared" + (f" · {said}" if said else "") + " · it is kept in Earlier web chats"


def parse_multipart(content_type: str, body: bytes) -> tuple[dict, list[tuple[str, bytes]]]:
    """(text fields, [(file name, bytes)]) of a multipart/form-data body."""
    from email.parser import BytesParser
    from email.policy import HTTP
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    if not msg.is_multipart():
        raise Refused("expected a multipart form")
    fields, files = {}, []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        data = part.get_payload(decode=True) or b""
        if filename:
            if data:
                files.append((filename, data))
        elif name:
            fields[name] = data.decode(errors="replace")
    return fields, files

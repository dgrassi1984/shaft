"""Chat with an instance in the browser (docs/plans/PLAN-webui.md M4). Stdlib only.

Each web chat is a conversation of SHAFT's own API, as Telegram's are: the web server calls 127.0.0.1's
/v1/chat/completions (streamed) with SHAFT_API_KEY and the operator's tailnet login, naming the conversation with
X-Shaft-Conversation (the chat's id). So the reply is the API's — a normal sandboxed run with the run lock, the gate
and the commit; its narration rule; waiting while the instance is busy — and the key never reaches the browser.

A reply keeps going when the browser drops: it is relayed by a thread of the web server, which
saves its activity as it streams and the answer when it ends, so a reload, a phone that locked or a second device
shows it. Only Stop stops it (the relay closes the API connection; the API then stops the agent). A message sent while
it replies is an interjection (/v1/chat/interject): the instance reads it at its next step; one it did not hear in
time is answered by a reply of its own once this one ends, as on Telegram. End asks for the journal write-up now
(/v1/chat/end does not wait for the entry to be written) instead of after SHAFT_IDLE_MINUTES.

Files go in with a message: saved in the instance's folder by messages.from_operator (as Telegram's are), and the
message says where. Files it sends the operator meanwhile (send_message) are shown in the chat, as in Messages.

Chats are kept in .supervisor/web/chats/<id>.json, written only by this process.
"""
from __future__ import annotations

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

from .core import SUP_DIR, log

SAVE_EVERY_S = 1.0                 # how often a streaming reply's activity is written for the page to read
MAX_TEXT = 20000                   # a message, at most


# Added, unseen by the operator, to the first message of each web chat: how the instance shares a document with them
# (docs/plans/PLAN-webui-chat §3). In the message rather than its prompts, so every instance gets it whatever its own code says.
DOCS_NOTE = ("[Web chat: a Markdown file you write under notes/docs/ appears beside this chat as a document the operator sees "
             "live and can edit. Write it whole (write_file with replace=true when you drop lines). His edits reach you "
             "as a diff in their next message.]")


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


_parsed: dict[str, tuple[tuple, dict]] = {}     # path -> ((inode, mtime_ns, size), chat): read again only when it changes


def chats(name: str) -> list[dict]:
    """This instance's web chats, newest first. Read-only: a change goes through load() and save(). Every feed, panel
    and rail refresh asks for them (docs/plans/PLAN-pwa P3: every chat file of every instance was parsed each time)."""
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


def open_chat(name: str) -> dict | None:
    """The instance's web chat still going on (the newest not ended), if any."""
    return next((c for c in chats(name) if c.get("state") != "ended"), None)


# A chat being looked at: its feed is refreshed every 1.5 s while its page is visible (a hidden page does not
# refresh it). A reply that ends while someone looks at it needs no notification.
WATCH_S = 6.0
_watched: dict[str, float] = {}


def watching(name: str) -> None:
    _watched[name] = time.monotonic()


def watched(name: str) -> bool:
    return time.monotonic() - _watched.get(name, -1e9) < WATCH_S


def reply_event(chat: dict) -> dict:
    """What the push for this chat's reply shows: its first line."""
    name = chat["instance"]
    t = next((t for t in reversed(chat.get("turns") or []) if t["role"] == "assistant"), {})
    first = next((l.strip() for l in (t.get("content") or "").splitlines() if l.strip()), "") or (t.get("note") or "")
    return {"title": f"{name} replied", "body": first[:160], "url": f"/i/{name}/chat", "tag": name}


def summary(name: str) -> dict:
    """For the rail: the newest chat's last line, when the instance last answered in it, and whether it is replying."""
    c = next(iter(chats(name)), None)
    if not c or c.get("cleared"):
        return {}
    turns = c.get("turns") or []
    last = next((t for t in reversed(turns) if t["role"] == "assistant"), None)
    text = (turns[-1].get("shown", turns[-1].get("content")) or "") if turns else ""
    who = "you: " if turns and turns[-1]["role"] == "user" else ""
    first = next((l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith(">")), "")
    return {"line": (who + first)[:80], "last": last["ts"] if last else "", "replying": c.get("state") == "replying"}


def shown(name: str) -> dict | None:
    """The chat its page shows: the one going on, else the last one (ended: the next message starts a new one) — unless
    the operator cleared it: then none, until they write again."""
    c = open_chat(name) or next(iter(chats(name)), None)
    return None if c and c.get("cleared") else c


def new_chat(name: str, login: str) -> dict:
    chat = {"id": f"web-{secrets.token_hex(8)}", "instance": name, "login": login, "created": _now(), "state": "idle",
            "turns": [], "reply": None}
    _adopt_edits(name, chat)
    save(chat)
    return chat


def _adopt_edits(name: str, chat: dict) -> None:
    """Edits to its documents saved but not yet sent, left on a chat that was ended or cleared since: the new chat
    takes them, so the instance still hears of them with the next message (they were lost with the old chat)."""
    for old in chats(name):
        if old["id"] == chat["id"] or not old.get("pending_edits"):
            continue
        with _lock(old["id"]):
            o = load(old["id"])
            pend = o.pop("pending_edits", None) or {}
            save(o)
        for path, e in pend.items():
            chat.setdefault("pending_edits", {}).setdefault(path, e)


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
            # The request may have reached the API and run tools, so this is not a safe retry.
            c["turns"].append({"role": "assistant", "content": r.get("answer") or "", "ts": _now(),
                               "activity": r.get("activity", []),
                               "note": "interrupted: the web service restarted while it replied",
                               "note_kind": "service", "note_retry": False})
            c["state"], c["reply"] = "idle", None
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
            "X-Shaft-Conversation": chat["id"], "X-Shaft-Channel": "web", "User-Agent": "SHAFT web UI"}


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


def _api_messages(chat: dict) -> list[dict]:
    return [{"role": t["role"], "content": t["content"]} for t in chat["turns"] if t.get("content") or t["role"] == "user"]


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
class Relay:
    def __init__(self, cid: str):
        self.cid, self.conn, self.sock, self.stopped = cid, None, None, False

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


def replying(cid: str) -> bool:
    return cid in _relays


def _start(cid: str) -> None:
    relay = Relay(cid)
    _relays[cid] = relay
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
        conn.request("POST", "/v1/chat/completions",
                     json.dumps({"model": chat["instance"], "messages": _api_messages(chat), "stream": True}),
                     _headers(chat))
        posted = True
        relay.sock = conn.sock
        if relay.stopped:                           # Stop came before the socket was there
            relay.stop()
        resp = conn.getresponse()
        status = resp.status
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
    _sync_copies(chat["instance"], docs_to_sync)


def _finish(relay: Relay, answer: str, activity: list, error: str | None, heard: int | None, *,
            status: int | None = None, posted: bool = True, exc: BaseException | None = None,
            tools: bool | None = None) -> list[str]:
    docs_to_sync = []
    cid = relay.cid
    with _lock(cid):
        chat = load(cid)
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
            cause = {"note_kind": kind, "note_retry": retry and not sent and bool(request),
                     "retry_text": request if retry and not sent else ""}
        from . import docs
        made = docs.changed(chat["reply"].get("docs_before") or {}, _docs_now(chat["instance"]))
        chat["turns"].append({"role": "assistant", "content": answer.strip(), "ts": _now(),
                              "activity": activity[-200:], "note": note, **cause,
                              **({"docs": made} if made else {})})
        chat["state"], chat["reply"] = "idle", None
        # An API older than the web UI does not say what it heard: then no second reply, rather than one per message.
        unheard = max(0, sent - heard) if heard is not None and not relay.stopped and not error else 0
        if unheard:                               # sent too late for this reply: answered by one of their own
            chat["state"], chat["reply"] = "replying", {"started": _now(), "activity": [], "answer": "",
                                                        "interjections": 0, "followup": unheard,
                                                        "docs_before": _docs_now(chat["instance"]),
                                                        **_reply_comments(interjected[-unheard:]),
                                                        "turn_start": len(chat["turns"])}
        save(chat)
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
    if not unheard and not relay.stopped and not watched(chat["instance"]):
        from . import webpush                       # nobody is looking: the phone is told (docs/plans/PLAN-pwa P4)
        webpush.notify(reply_event(chat))
    log(f"web: chat {cid} with {chat['instance']}: reply {'stopped' if relay.stopped else 'failed' if error else 'done'}"
        + (f" ({unheard} interjection(s) answered next)" if unheard else ""))
    if unheard:
        _start(cid)
    return docs_to_sync


# -- what the page does ----------------------------------------------------------------------------------------------
# -- the operator's edits to its documents (docs/plans/PLAN-webui-chat §3 D2) ---------------------------------------------------
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


def doc_saved(name: str, login: str, path: str, old: str | None, new: str | None, sha: str) -> str:
    """After the operator saved a document: the instance hears of it — at once (an interjection) while it replies, else
    with their next message; several saves before then are one diff (from what it last saw). The chat also keeps their
    version, so the panel can say when a later write of the instance dropped lines they wrote. (what to tell them)"""
    with _inst_lock(name):
        return _doc_saved(name, login, path, old, new, sha)


def _doc_saved(name: str, login: str, path: str, old: str | None, new: str | None, sha: str) -> str:
    chat = open_chat(name) or new_chat(name, login)
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
        _, said = send(name, login, "", [])
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


def send_comment(name: str, login: str, pid: str) -> tuple[dict, str]:
    """Send a stored post once; its post id also deduplicates retries before mark_sent succeeds."""
    from . import doc_comments
    with _inst_lock(name):
        try:
            post = doc_comments.get_post(name, pid)
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
                              comments=[{"id": post["thread"], "doc": post["doc"]}])
            index = next(i for i, t in enumerate(chat["turns"]) if t.get("mid") == pid)
            doc_comments.mark_sent(name, pid, f"{chat['id']}#{index}")
        except (Exception, SystemExit) as e:
            raise Refused(str(e)) from e
    _sync_copies(name, [post["doc"]])
    return chat, said


def send(name: str, login: str, text: str, files: list[tuple[str, bytes]], mid: str = "", *,
         comment_block: str = "", comments: list[dict] | None = None) -> tuple[dict, str]:
    """A message from the operator: a new reply if none is running, an interjection otherwise. (chat, what to say)
    `mid`: the page's id for this message — sent again after an answer that never arrived, it is not sent twice."""
    with _inst_lock(name):
        if mid and (c := open_chat(name)) and mid in (c.get("mids") or []):
            return c, f"already sent to {name}"
        chat, said = _send(name, login, text, files, comment_block=comment_block, comments=comments, mid=mid)
    from . import messages                         # a question it quotes is answered, as a Telegram reply answers it
    if (answered := messages.answer_quoted(name, text)):
        log(f"web: {name}'s question {', '.join(answered)} answered in the web chat")
        said += " · answers its question"
    return chat, said


def _send(name: str, login: str, text: str, files: list[tuple[str, bytes]], *,
          comment_block: str = "", comments: list[dict] | None = None, mid: str = "") -> tuple[dict, str]:
    from . import messages, doc_comments
    text = text.strip()
    if not text and not files and not comment_block and not (open_chat(name) or {}).get("pending_edits"):
        raise Refused("an empty message is not sent")
    if len(text) > MAX_TEXT:
        raise Refused(f"a message is at most {MAX_TEXT} characters ({len(text)})")
    chat = open_chat(name) or new_chat(name, login)
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
    notices = doc_comments.take_notices(name)
    try:
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
            if not any(t["role"] == "user" for t in chat["turns"]):
                turn["content"] += "\n\n" + DOCS_NOTE
            if chat["state"] == "replying":
                status, body = _post(chat, "interject", {"model": name, "messages": _api_messages(chat) + [
                    {"role": "user", "content": turn["content"]}]})
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
        chat["state"], chat["ended"] = "ended", _now()
        save(chat)
    log(f"web: chat {cid} with {chat['instance']} ended by the operator")
    return "ended: journal write-up requested" if status == 202 else "ended"


def clear(name: str, cid: str = "") -> str:
    """Clear the chat from view: the page and the rail start empty. Nothing is deleted — the chat stays in Earlier web
    chats and Transcripts. A conversation still open is ended first (a journal write-up is requested, as End does);
    one replying is not.
    `cid`: the chat the page showed — refused if that is no longer the one shown (it cleared the current one instead,
    from a past chat's page: Codex review)."""
    with _inst_lock(name):
        return _clear(name, cid)


def _clear(name: str, cid: str) -> str:
    if not cid:
        raise Refused("that is not the chat on this page any more: reload it")
    chat = shown(name)
    if not chat:
        raise Refused("there is nothing to clear")
    if chat["id"] != cid:
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

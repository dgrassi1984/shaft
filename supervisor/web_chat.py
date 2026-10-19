"""Chat with an instance in the browser (PLAN-webui.md M4). Stdlib only.

Each web chat is a conversation of SHAFT's own API, as Telegram's are: the web server calls 127.0.0.1's
/v1/chat/completions (streamed) with SHAFT_API_KEY and the operator's tailnet login, naming the conversation with
X-Shaft-Conversation (the chat's id). So the reply is the API's — a normal sandboxed run with the run lock, the gate
and the commit; its narration rule; waiting while the instance is busy — and the key never reaches the browser.

A reply keeps going when the browser drops (Daniele, 2026-10-19): it is relayed by a thread of the web server, which
saves its activity as it streams and the answer when it ends, so a reload, a phone that locked or a second device
shows it. Only Stop stops it (the relay closes the API connection; the API then stops the agent). A message sent while
it replies is an interjection (/v1/chat/interject): the instance reads it at its next step; one it did not hear in
time is answered by a reply of its own once this one ends, as on Telegram. End writes the conversation up now
(/v1/chat/end) instead of after SHAFT_IDLE_MINUTES.

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


# Added, unseen by the operator, to the first message of each web chat: how the instance shares a document with him
# (PLAN-webui-chat §3). In the message rather than its prompts, so every instance gets it whatever its own code says.
DOCS_NOTE = ("[Web chat: a Markdown file you write under notes/docs/ appears beside this chat as a document Daniele sees "
             "live and can edit. Write it whole (write_file with replace=true when you drop lines). His edits reach you "
             "as a diff in his next message.]")


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


def chats(name: str) -> list[dict]:
    """This instance's web chats, newest first."""
    out = []
    for p in _dir().glob("web-*.json") if _dir().is_dir() else []:
        try:
            c = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if c.get("instance") == name:
            out.append(c)
    return sorted(out, key=lambda c: c.get("created", ""), reverse=True)


def open_chat(name: str) -> dict | None:
    """The instance's web chat still going on (the newest not ended), if any."""
    return next((c for c in chats(name) if c.get("state") != "ended"), None)


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
    the operator cleared it: then none, until he writes again."""
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
            c["turns"].append({"role": "assistant", "content": r.get("answer") or "", "ts": _now(),
                               "activity": r.get("activity", []),
                               "note": "interrupted: the web service restarted while it replied"})
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
            "X-Shaft-Conversation": chat["id"], "User-Agent": "SHAFT web UI"}


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
    with _lock(cid):
        chat = load(cid)
        sent = chat["reply"].get("interjections", 0)
    try:
        relay.conn = conn = _api_conn(3600)
        conn.request("POST", "/v1/chat/completions",
                     json.dumps({"model": chat["instance"], "messages": _api_messages(chat), "stream": True}),
                     _headers(chat))
        relay.sock = conn.sock
        if relay.stopped:                           # Stop came before the socket was there
            relay.stop()
        resp = conn.getresponse()
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
                    break
                try:
                    ev = json.loads(line[6:])
                except ValueError:
                    continue
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
    except (OSError, http.client.HTTPException, Refused) as e:
        if not relay.stopped:
            error = f"{type(e).__name__}: {e}"
    finally:
        try:
            if relay.conn:
                relay.conn.close()
        except OSError:
            pass
    with _lock(cid):
        chat = load(cid)
        sent = chat["reply"].get("interjections", 0)
        note = ("stopped by you" if relay.stopped else f"error: {error}" if error else "")
        from . import docs
        made = docs.changed(chat["reply"].get("docs_before") or {}, _docs_now(chat["instance"]))
        chat["turns"].append({"role": "assistant", "content": answer.strip(), "ts": _now(), "activity": activity[-200:],
                              "note": note, **({"docs": made} if made else {})})
        chat["state"], chat["reply"] = "idle", None
        # An API older than the web UI does not say what it heard: then no second reply, rather than one per message.
        unheard = max(0, sent - heard) if heard is not None and not relay.stopped and not error else 0
        if unheard:                               # sent too late for this reply: answered by one of their own
            chat["state"], chat["reply"] = "replying", {"started": _now(), "activity": [], "answer": "",
                                                        "interjections": 0, "followup": unheard,
                                                        "docs_before": _docs_now(chat["instance"])}
        save(chat)
    if _relays.get(cid) is relay:                  # never a newer relay of the same chat
        del _relays[cid]
    log(f"web: chat {cid} with {chat['instance']}: reply {'stopped' if relay.stopped else 'failed' if error else 'done'}"
        + (f" ({unheard} interjection(s) answered next)" if unheard else ""))
    if unheard:
        _start(cid)


# -- what the page does ----------------------------------------------------------------------------------------------
# -- the operator's edits to its documents (PLAN-webui-chat §3 D2) ---------------------------------------------------
EDIT_DIFF_MAX = 12000                  # a diff longer than this is summarised: the instance reads the file itself


def _fence(text: str) -> str:
    """A code fence longer than any run of backticks in the text: a document's own ``` must not close it."""
    return "`" * max(3, 1 + max((len(m) for m in re.findall(r"`+", text)), default=0))


def edit_diff(path: str, old: str | None, new: str) -> str:
    """What the operator changed in a document, as the instance is told it."""
    import difflib
    rel = f"notes/docs/{path}"
    if old is None:
        body = new if len(new) <= EDIT_DIFF_MAX else new[:EDIT_DIFF_MAX] + "\n…(cut: read the file)"
        f = _fence(body)
        return f"[Daniele created {rel}:]\n{f}markdown\n{body}\n{f}"
    diff = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{rel}", f"b/{rel}", lineterm="", n=2))
    if not diff:
        return f"[Daniele saved {rel} unchanged.]"
    if len(diff) > EDIT_DIFF_MAX:
        added = sum(l.startswith("+") for l in diff.splitlines()[2:])
        dropped = sum(l.startswith("-") for l in diff.splitlines()[2:])
        return f"[Daniele edited {rel}: +{added} −{dropped} lines, too long to show here — read the file.]"
    f = _fence(diff)
    return f"[Daniele edited {rel}:]\n{f}diff\n{diff}\n{f}"


def doc_saved(name: str, login: str, path: str, old: str | None, new: str, sha: str) -> str:
    """After the operator saved a document: the instance hears of it — at once (an interjection) while it replies, else
    with his next message; several saves before then are one diff (from what it last saw). The chat also keeps his
    version, so the panel can say when a later write of the instance dropped lines he wrote. (what to tell him)"""
    chat = open_chat(name) or new_chat(name, login)
    with _lock(chat["id"]):
        chat = load(chat["id"])
        pend = chat.setdefault("pending_edits", {})
        # Folded into one diff only while nobody else wrote it in between: if the instance wrote over his last save,
        # it knows its own version, and the diff starts there (folding from the first save called its text his).
        prev = pend.get(path)
        first = prev["old"] if prev and prev["new"] == old else old
        pend[path] = {"old": first, "new": new}
        added = sorted({l.strip() for l in new.splitlines() if l.strip()} - {l.strip() for l in (old or "").splitlines()})
        chat.setdefault("saved_docs", {})[path] = {"sha": sha, "text": new, "ts": _now(), "added": added[:400]}
        save(chat)
        replying_now = chat["state"] == "replying"
    if replying_now:
        _, said = send(name, login, "", [])
        return f"saved · {said}"
    return f"saved · {name} gets the change with your next message"


def _take_edits(chat: dict) -> tuple[str, list[str]]:
    """The pending edits as the message's extra text, and their paths; cleared from the chat (the caller saves it)."""
    pend = chat.pop("pending_edits", None) or {}
    blocks = [edit_diff(p, e["old"], e["new"]) for p, e in sorted(pend.items())]
    return ("\n\n".join(blocks), sorted(pend))


def send(name: str, login: str, text: str, files: list[tuple[str, bytes]]) -> tuple[dict, str]:
    """A message from the operator: a new reply if none is running, an interjection otherwise. (chat, what to say)"""
    from . import messages
    text = text.strip()
    if not text and not files and not (open_chat(name) or {}).get("pending_edits"):
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
    with _lock(cid):                               # the relay's last save takes it too: the chat's state is the truth
        chat = load(cid)
        chat["login"] = login
        edits, paths = _take_edits(chat)
        if edits:
            turn["content"] = (turn["content"] + "\n\n" + edits).strip()
            turn["edits"] = paths
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
        chat["state"], chat["reply"] = "replying", {"started": _now(), "activity": [], "answer": "", "interjections": 0,
                                                    "docs_before": _docs_now(name)}
        save(chat)
    _start(cid)
    return chat, f"sent to {name}"


def stop(cid: str) -> str:
    relay = _relays.get(cid)
    if not relay:
        raise Refused("no reply is running")
    relay.stop()
    return "stopping the reply"


def end(cid: str) -> str:
    chat = load(cid)
    if not chat or chat.get("state") == "ended":
        raise Refused("this chat is already over")
    if replying(cid):
        raise Refused("it is still replying: stop the reply first")
    status, body = _post(chat, "end", {"model": chat["instance"]})
    err = body.get("error") or {}
    if status == 404 and err.get("type") != "not_found":   # "no route": an API older than the web UI
        raise Refused("the running API is older than the web UI and cannot end a conversation: restart it "
                      "(shaft api on); until then it is written up after SHAFT_IDLE_MINUTES of silence")
    if status not in (202, 404):                    # 404 not_found: the API had already written it up (idle)
        raise Refused(err.get("message") or f"API HTTP {status}")
    with _lock(cid):
        chat = load(cid)
        chat["state"], chat["ended"] = "ended", _now()
        save(chat)
    log(f"web: chat {cid} with {chat['instance']} ended by the operator")
    return "ended: it writes the conversation up in its journal now" if status == 202 else "ended"


def clear(name: str) -> str:
    """Clear the chat from view: the page and the rail start empty. Nothing is deleted — the chat stays in Earlier web
    chats and Transcripts. A conversation still open is ended first (written up, as End does); one replying is not."""
    chat = shown(name)
    if not chat:
        raise Refused("there is nothing to clear")
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

"""A throwaway SHAFT web service for driving the native apps without real data (apps/shaft/README.md, "Try it against a
fixture server").

It runs the real `/app/v1` (supervisor/app_api.py, web_chat, docs, comments, feedback, the push fan-out) on 127.0.0.1,
over a temporary SUP_DIR and instances folder it seeds itself, never the real `.supervisor/` or `instances/`. What it
fakes is what lies outside the web service, as supervisor/tests/test_app_api.py does:

- the tailnet: every request gets the login `tailscale serve` would add, and the host is 127.0.0.1:<port>;
- the API a reply streams from: a send starts a scripted reply here (activity, an answer streamed in pieces, then the
  real `web_chat._finish`); a message containing "fail" fails as a model 502 does, "slow" streams for a minute;
- APNs: a push is written to <dir>/pushes/ with the payload supervisor/apns.py would send, and with `--simctl`
  delivered to that simulator by `xcrun simctl push`;
- the instances' live state (`web._live`) and Next cycle's hint: from <dir>/state.json.

    python3 apps/shaft/DevServer/fixture_server.py serve --dir /tmp/shaft-fixture --port 8899 [--simctl <udid>]
    python3 apps/shaft/DevServer/fixture_server.py pair  --dir /tmp/shaft-fixture --port 8899 [--name iphone-app]
    python3 apps/shaft/DevServer/fixture_server.py act   --dir /tmp/shaft-fixture --port 8899 <what> [text]

`act` (run while `serve` runs, on the same folder; pass the same --simctl): `message <instance> [text]` and
`question <instance> [text]` (the instance sends you one, with a push), `state <instance> <state>` (its state in the
rail), `doc <instance> <path> [text]` (it writes a document), `long <instance> [turns]` (a chat past the 80-turn
window).

Run it from the repository root with the python the supervisor uses (3.12). Never point --dir at the real data.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
ME = "operator@example.com"
TOPIC = "io.github.dgrassi1984.shaft"
INSTANCES = ("thelos", "wren", "ada")


def setup(base: Path, port: int):
    """Import the supervisor over `base` and patch what lies outside the web service. Returns the modules."""
    base = base.resolve()
    real = (ROOT / ".supervisor").resolve()
    if base == real or real in base.parents or base == (ROOT / "instances").resolve():
        raise SystemExit(f"refusing to use {base}: that is the real data")
    (base / "sup").mkdir(parents=True, exist_ok=True)
    os.environ["SHAFT_SUPERVISOR_DIR"] = str(base / "sup")       # before the supervisor is imported: SUP_DIR follows it
    sys.path.insert(0, str(ROOT))
    import supervisor
    from supervisor import autonomy, core, docs, messages, web, web_chat, webpush

    inst = base / "instances"
    for name in INSTANCES:
        for d in ("shaft", ".git", "notes/docs", "inbox"):
            (inst / name / d).mkdir(parents=True, exist_ok=True)
    apns = _stub_apns(base)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    real_respond = web.respond

    def respond(req):                                  # what `tailscale serve` adds to every request
        req.headers = {**req.headers, "tailscale-user-login": ME}
        return real_respond(req)

    def run_now(args, timeout=60, who="top"):           # the docs CLI verb, in this process
        out = []
        name, action, path = args[1:4]
        with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            code = docs.command(name, action, path, args[5] if action == "set" else None, args[-1])
        return code == 0, out[-1] if out else ""

    for p in (mock.patch.object(web, "respond", respond),
              mock.patch.object(web, "allowed_logins", return_value={ME}),
              mock.patch.object(web, "allowed_hosts", return_value=hosts),
              mock.patch.object(web, "url", return_value=f"http://127.0.0.1:{port}"),
              mock.patch.object(web, "_names", return_value=list(INSTANCES)),
              mock.patch.object(web, "_live", side_effect=lambda n: _state(base).get(n, {})),
              mock.patch.object(web, "_next_cycle", side_effect=lambda n: _next(base, n)),
              mock.patch.object(autonomy, "next_cycle", side_effect=lambda n: _next(base, n)),
              mock.patch.object(autonomy, "event_wake", lambda *a, **k: None),
              mock.patch.object(core, "INSTANCES", inst), mock.patch.object(messages, "INSTANCES", inst),
              mock.patch.object(docs, "instance_root", lambda n: inst / n),
              mock.patch("supervisor.actions.run_now", side_effect=run_now),
              mock.patch.object(web_chat, "_start", side_effect=lambda cid: _start(web_chat, cid)),
              mock.patch.object(web_chat, "_post", side_effect=lambda chat, route, body, timeout=15: (202, {})),
              mock.patch.object(web_chat, "_sync_copies", lambda *a, **k: None),
              mock.patch.dict(sys.modules, {"supervisor.apns": apns}),
              mock.patch.object(supervisor, "apns", apns, create=True)):
        p.start()
    return web, web_chat, messages, webpush


# -- the instances' state, Next cycle's hint ---------------------------------------------------------------------------
def _state(base: Path) -> dict:
    try:
        return json.loads((base / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def _next(base: Path, name: str) -> str:
    return _state(base).get(name, {}).get("next_cycle") or f"for {name}'s next cycle, which starts at 18:30"


# -- APNs, written down (and with --simctl handed to the simulator) ----------------------------------------------------
def _stub_apns(base: Path) -> ModuleType:
    apns = ModuleType("supervisor.apns")
    path = base / "sup" / "web" / "apns.json"
    pushes = base / "pushes"

    def devices():
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def register(device, token, env, topic, platform):
        if not token or any(c not in "0123456789abcdefABCDEF" for c in token) or len(token) % 2:
            raise ValueError("APNs token must be nonempty hex bytes")
        if env not in ("sandbox", "production") or platform not in ("ios", "macos"):
            raise ValueError("APNs env must be sandbox or production, platform ios or macos")
        if topic != TOPIC:
            raise ValueError(f"APNs topic must equal SHAFT_APNS_TOPIC={TOPIC!r}")
        data = devices()
        data[device] = {"token": token.lower(), "env": env, "topic": topic, "platform": platform}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1))

    def unregister(device):
        data = devices()
        gone = data.pop(device, None) is not None
        path.write_text(json.dumps(data, indent=1))
        return gone

    def status(device):
        sub = devices().get(device)
        return {"on": bool(sub), "env": sub and sub["env"], "reason": "" if sub else "device has no APNs token"}

    def wake(native: dict, event: dict):
        from supervisor.web_seen import badge
        parts = event["url"].split("/")
        instance = parts[2] if len(parts) > 2 and parts[1] == "i" else ""
        for device in native:
            payload = {"aps": {"alert": {"title": event["title"]}, "badge": badge(device), "sound": "default",
                               "thread-id": instance, "mutable-content": 1}, "instance": instance}
            pushes.mkdir(parents=True, exist_ok=True)
            out = pushes / f"{time.time():.3f}-{device}.json"
            out.write_text(json.dumps(payload))
            print(f"push to {device}: {event['title']} -> {out}", flush=True)
            for udid in SIMCTL:
                subprocess.run(["xcrun", "simctl", "push", udid, TOPIC, str(out)], check=False)

    apns.configured, apns.devices, apns.register, apns.unregister, apns.status = (
        lambda: None, devices, register, unregister, status)
    apns._wake = wake
    return apns


SIMCTL: list[str] = []


# -- a reply, scripted --------------------------------------------------------------------------------------------------
ANSWER = """Here is what I found for **{ask}**.

- The ledger balances to the cent.
- Two accounts were rounded twice; both are fixed.

| Account | Before | After |
|---|---:|---:|
| Operating | 12,401.20 | 12,401.19 |
| Reserve | 3,050.00 | 3,050.00 |

- [x] reconcile October
- [ ] reconcile November

```python
def net_daily(rows):
    return round(sum(r.amount for r in rows), 2)
```

The summary is in `notes/docs/ledger.md`."""


def _start(web_chat, cid: str) -> None:
    relay = web_chat.Relay(cid)
    web_chat._relays[cid] = relay
    threading.Thread(target=_run, args=(web_chat, relay), daemon=True, name=f"fake-{cid}").start()


def _run(web_chat, relay) -> None:
    cid = relay.cid
    with web_chat._lock(cid):
        chat = web_chat.load(cid)
    ask = next((t.get("shown") or t["content"] for t in reversed(chat["turns"]) if t["role"] == "user"), "")
    slow = "slow" in ask.lower()
    steps = ["read_file notes/docs/ledger.md", "run tests: 412 passed", "write notes/docs/ledger.md",
             "check the October totals"]
    answer = ANSWER.format(ask=(ask.splitlines() or ["your message"])[-1][:60])
    for c in chat["reply"].get("comment_ids", []):      # it answers the comments it was sent, as an instance does
        answer += f"\n\n### {c}\nDone: I reworded that passage."
    words = answer.split(" ")
    activity, said = [], ""
    ticks = 60 if slow else 10
    for i in range(ticks):
        time.sleep(1.0)
        if relay.stopped:
            break
        if i < len(steps):
            activity.append(steps[i])
        if i >= 3:
            said = " ".join(words[: int(len(words) * (i - 2) / (ticks - 3))])
        with web_chat._lock(cid):
            chat = web_chat.load(cid)
            if chat.get("state") != "replying":
                return
            chat["reply"].update(activity=activity, answer=said)
            heard = chat["reply"].get("interjections", 0)
            web_chat.save(chat)
    error = None if relay.stopped or "fail" not in ask.lower() else "CloudError: OpenRouter HTTP 502 (tools_ran=false)"
    final = "" if error else (said if relay.stopped else answer)
    with web_chat._inst_lock(chat["instance"]):
        web_chat._finish(relay, final, activity, error, heard, status=502 if error else 200, tools=False)


# -- the seed -----------------------------------------------------------------------------------------------------------
def _png(path: Path) -> int:
    """A real 320x200 PNG (a gradient) made with the stdlib, so the app's thumbnail has something to show."""
    import struct
    import zlib
    w, h = 320, 200
    rows = b"".join(b"\0" + bytes(v for x in range(w) for v in (40 + x * 180 // w, 90 + y * 120 // h, 160))
                    for y in range(h))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


def seed(base: Path, web, web_chat, messages) -> None:
    from supervisor import doc_comments, feedback
    if (base / "seeded").exists():
        return
    inst = base / "instances"
    now = datetime.now().replace(microsecond=0)

    def ts(minutes_ago: float) -> str:
        return (now - timedelta(minutes=minutes_ago)).isoformat()

    def msg(t, file, text, frm, to, read, **extra):
        row = {"ts": t, "from": frm, "to": to, "kind": extra.pop("kind", "message"), "file": file, "text": text, **extra}
        with (base / "sup" / "messages.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")
        if not read:
            box = base / "sup" / "operator-inbox" if to == "operator" else inst / to / "inbox"
            box.mkdir(parents=True, exist_ok=True)
            (box / file).write_text(text)

    size = _png(inst / "thelos" / "attachments" / "ci-run.png")
    (inst / "thelos" / "attachments" / "totals.csv").write_text("account,total\noperating,12401.19\nreserve,3050.00\n")
    docs_dir = inst / "thelos" / "notes" / "docs"
    (docs_dir / "ledger.md").write_text(
        "# Ledger\n\nNet daily totals are rounded once, at the end.\n\n## October\n\n| Account | Total |\n|---|---:|\n"
        "| Operating | 12,401.19 |\n| Reserve | 3,050.00 |\n\n- [x] reconcile\n- [ ] publish\n")
    (docs_dir / "plans").mkdir(exist_ok=True)
    (docs_dir / "plans" / "q4.md").write_text("# Q4 plan\n\nShip the nightly report.\n")
    (inst / "wren" / "notes" / "docs" / "garden.md").write_text("# Garden\n\nWater the basil.\n")

    cid = "web-3f9a1c0d7e2b4a65"
    yesterday = "web-77ab02c4e9d15f30"
    web_chat.save({"id": yesterday, "instance": "thelos", "login": ME, "created": ts(60 * 26), "state": "ended",
                   "ended": ts(60 * 25), "reply": None, "turns": [
                       {"role": "user", "ts": ts(60 * 26), "content": "What did the October close look like?",
                        "shown": "What did the October close look like?\nAnd September?"},
                       {"role": "assistant", "ts": ts(60 * 25.9), "content": "October closed **clean**.",
                        "activity": ["read_file notes/docs/ledger.md"]}]})
    turns = [
        {"role": "user", "ts": ts(60 * 24), "content": "Morning: is the nightly job green?",
         "shown": "Morning: is the nightly job green?", "mid": "seed-0"},
        {"role": "assistant", "ts": ts(60 * 23.9), "content": "Yes — all **412** tests pass.",
         "activity": ["run tests: 412 passed"]},
        {"role": "user", "ts": ts(30), "content": "x",
         "shown": "> Ship the ledger fix today?\n\nYes, once the tests pass. This is the failing run:",
         "mid": "seed-1", "attachments": [{"name": "ci-run.png", "path": "instances/thelos/attachments/ci-run.png",
                                            "bytes": size}]},
        {"role": "assistant", "ts": ts(28), "content": ANSWER.format(ask="the failing run"),
         "activity": ["read_file notes/docs/ledger.md", "run tests: 412 passed"],
         "docs": [{"path": "ledger.md", "created": False}], "answered": [{"id": "c1", "doc": "ledger.md"}]},
        {"role": "user", "ts": ts(20), "content": "again", "shown": "Rerun the report for October.", "retried": True},
        {"role": "assistant", "ts": ts(19.5), "content": "", "note": "error: CloudError: OpenRouter HTTP 502",
         "note_kind": "model", "note_retry": True, "retry_text": "Rerun the report for October.", "retried": True},
        {"role": "user", "ts": ts(19), "content": "Rerun the report for October.",
         "shown": "Rerun the report for October.", "mid": "seed-2", "edits": ["ledger.md"]},
        {"role": "assistant", "ts": ts(18), "content": "Done: October is rerun and **214** accounts reconciled.",
         "activity": ["run report --month 2026-10"]},
        {"role": "user", "ts": ts(10), "content": "And November?", "shown": "And November?", "mid": "seed-3"},
        {"role": "assistant", "ts": ts(9.5), "content": "", "note": "error: CloudError: OpenRouter HTTP 502",
         "note_kind": "model", "note_retry": True, "retry_text": "And November?"},
    ]
    web_chat.save({"id": cid, "instance": "thelos", "login": ME, "created": ts(60 * 24), "state": "idle",
                   "reply": None, "turns": turns})
    msg(ts(15), "seed-from-thelos-q.md", "Should the nightly report keep the **per-account** lines?", "thelos",
        "operator", False, kind="question")
    msg(ts(14), "seed-from-thelos-m.md", "The totals, as a file:", "thelos", "operator", True,
        attachments=[{"name": "totals.csv", "path": "instances/thelos/attachments/totals.csv", "bytes": 51}])
    msg(ts(12), "seed-from-operator-b.md", "Look at the March reconciliation when you have time.", "operator",
        "thelos", False, via=messages.BACKGROUND, mid="seed-b")
    with feedback.FILE.open("a") as f:
        f.write(json.dumps({"ts": ts(17), "event": "rated", "id": "f-" + "6a0c2e4b8d1f3a5c7e9b0d2f", "instance": "thelos",
                            "login": ME, "target": {"kind": "chat", "chat": cid, "k": 7}, "thumb": "up",
                            "reason": "Exactly the summary I wanted", "reply_ts": turns[7]["ts"],
                            "reply_sha": "0" * 64}) + "\n")
    doc_comments.add_thread("thelos", "p-1e2d3c4b5a697887", "ledger.md",
                            {"exact": "rounded once, at the end", "prefix": "Net daily totals are ", "suffix": ".",
                             "start": 28},
                            __import__("hashlib").sha256((docs_dir / "ledger.md").read_bytes()).hexdigest(),
                            "Per account or overall?")
    doc_comments.mark_sent("thelos", "p-1e2d3c4b5a697887", f"{cid}#2")
    doc_comments.reply("thelos", "p-9a8b7c6d5e4f3021", "c1", "Per account, then summed.", "thelos")

    web_chat.save({"id": "web-00000000000000a1", "instance": "wren", "login": ME, "created": ts(90), "state": "idle",
                   "reply": None, "turns": [
                       {"role": "user", "ts": ts(90), "content": "Water the basil?", "shown": "Water the basil?",
                        "mid": "seed-w"},
                       {"role": "assistant", "ts": ts(89), "content": "Watered. The soil was *dry*."}]})
    (base / "state.json").write_text(json.dumps({
        "thelos": {"state": "in a cycle", "since": now.strftime("%H:%M"), "model": "cloud claude-sonnet-5",
                   "usd_today": 0.42,
                   "next_cycle": "a cycle is running and read its inbox when it began: this starts the next one once it ends"},
        "wren": {"state": "idle", "model": "qwen3.6-35b", "next_cycle": "for wren's next cycle, which this starts now"},
        "ada": {"state": "sleeping", "since": "until 18:30", "model": "qwen3.6-35b"}}, indent=1))
    (base / "seeded").write_text(now.isoformat())


# -- the commands -------------------------------------------------------------------------------------------------------
def act(base: Path, web, web_chat, messages, webpush, what: str, args: list[str]) -> None:
    name = args[0] if args else "thelos"
    text = " ".join(args[1:])
    now = datetime.now().replace(microsecond=0).isoformat()
    if what in ("message", "question"):
        file = f"{datetime.now():%Y%m%d-%H%M%S-%f}-from-{name}.md"
        text = text or ("Should I archive the September notes?" if what == "question" else "The nightly report is ready.")
        row = {"ts": now, "from": name, "to": "operator", "kind": what, "file": file, "text": text}
        with (base / "sup" / "messages.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")
        box = base / "sup" / "operator-inbox"
        box.mkdir(parents=True, exist_ok=True)
        (box / file).write_text(text)
        webpush.notify({"title": f"{name} asks" if what == "question" else f"{name} says", "body": text,
                        "url": f"/i/{name}/chat", "tag": name}, skip=web_chat.watchers(name))
        with web_chat.chat_saved:
            web_chat.chat_saved.notify_all()
    elif what == "state":
        state = _state(base)
        state.setdefault(name, {})["state"] = text or "idle"
        (base / "state.json").write_text(json.dumps(state, indent=1))
    elif what == "doc":
        path, _, body = text.partition(" ")
        target = base / "instances" / name / "notes" / "docs" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body or f"# {path}\n\nWritten by {name} at {now}.\n")
    elif what == "long":                               # a chat longer than the 80-turn window (Show the N earlier)
        start = datetime.now().replace(microsecond=0) - timedelta(minutes=200)
        turns = [{"role": "user" if k % 2 == 0 else "assistant",
                  "ts": (start + timedelta(minutes=2 * k)).isoformat(), "content": f"turn {k}", "shown": f"turn {k}",
                  **({"mid": f"long-{k}"} if k % 2 == 0 else {})} for k in range(int(text or 90))]
        web_chat.save({"id": "web-" + os.urandom(8).hex(), "instance": name, "login": ME,
                       "created": turns[0]["ts"], "state": "idle", "reply": None, "turns": turns})
    else:
        raise SystemExit(f"unknown act {what!r}: message, question, state, doc, long")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=("serve", "pair", "act"))
    p.add_argument("rest", nargs="*")
    p.add_argument("--dir", required=True, type=Path)
    p.add_argument("--port", required=True, type=int)
    p.add_argument("--name", default="iphone-app")
    p.add_argument("--simctl", action="append", default=[], help="a simulator UDID to hand each push to")
    a = p.parse_args()
    if a.port in (8444, 8785, 8781):
        raise SystemExit(f"port {a.port} belongs to a running SHAFT service: choose another")
    web, web_chat, messages, webpush = setup(a.dir, a.port)
    SIMCTL.extend(a.simctl)
    if a.command == "pair":
        print(web.new_pairing(a.name))
        return 0
    if a.command == "act":
        act(a.dir.resolve(), web, web_chat, messages, webpush, a.rest[0], a.rest[1:])
        return 0
    seed(a.dir.resolve(), web, web_chat, messages)
    server = ThreadingHTTPServer(("127.0.0.1", a.port), web.Handler)
    server.daemon_threads = True
    print(f"SHAFT fixture server on http://127.0.0.1:{a.port} over {a.dir.resolve()}", flush=True)
    print(f"pair: {web.new_pairing(a.name)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

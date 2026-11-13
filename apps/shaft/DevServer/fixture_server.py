"""A throwaway SHAFT web service for driving the native apps without real data (apps/shaft/README.md, "Try it against a
fixture server").

It runs the real `/app/v1` (supervisor/app_api.py, web_chat, docs, comments, feedback, the push fan-out) on 127.0.0.1,
over a temporary SUP_DIR and instances folder it seeds itself, never the real `.supervisor/` or `instances/`. What it
fakes is what lies outside the web service, as supervisor/tests/test_app_api.py does:

- the tailnet: every request gets the login `tailscale serve` would add, and the host is 127.0.0.1:<port>;
- the API a reply streams from: a send starts a scripted reply here (activity, an answer streamed in pieces, then the
  real `web_chat._finish`); a message containing "fail" fails as a model 502 does, "slow" streams for a minute,
  "wide" answers with a 12-column table and a 400-character line of code;
- APNs: a push is written to <dir>/pushes/ with the payload supervisor/apns.py would send, and with `--simctl`
  delivered to that simulator by `xcrun simctl push`;
- the instances' live state (`web._live`) and Next cycle's hint: from <dir>/state.json;
- settings: config and tool settings belong to the fixture's instances, and writes run in process;
- services: fake records start with this process's live pid; restarts change only those records. No CLI or launchd
  is called. Only docs, limits, channel, pause, resume, api on and autonomy restart are allowed by the command stub.

    python3 apps/shaft/DevServer/fixture_server.py serve --dir /tmp/shaft-fixture --port 8899 [--simctl <udid>] [--slow-seen 5]
    python3 apps/shaft/DevServer/fixture_server.py pair  --dir /tmp/shaft-fixture --port 8899 [--name iphone-app]
    python3 apps/shaft/DevServer/fixture_server.py act   --dir /tmp/shaft-fixture --port 8899 <what> [text]

`act` (run while `serve` runs, on the same folder; pass the same --simctl): `message <instance> [text]` and
`question <instance> [text]` (the instance sends you one, with a push), `state <instance> <state>` (its state in the
rail), `doc <instance> <path> [text]` (it writes a document), `long <instance> [turns]` (a chat past the 80-turn
window). `services busy <text>` sets the API refusal and the scheduler's waiting list (empty clears it);
`services drain-end` finishes a pending scheduler drain and changes its fake pid.

`--slow-seen SECONDS` holds every "mark seen" answer that long, so leaving a chat at once cancels the request in flight.

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
TOPIC = os.environ.get("SHAFT_APNS_TOPIC", "")
INSTANCES = ("tundra", "wisp", "ada")


def setup(base: Path, port: int):
    """Import the supervisor over `base` and patch what lies outside the web service. Returns the modules."""
    base = base.resolve()
    real = (ROOT / ".supervisor").resolve()
    if base == ROOT or base in ROOT.parents or base == real or real in base.parents or base == (ROOT / "instances").resolve():
        raise SystemExit(f"refusing to use {base}: that is the real data")
    (base / "sup").mkdir(parents=True, exist_ok=True)
    os.environ["SHAFT_SUPERVISOR_DIR"] = str(base / "sup")       # before the supervisor is imported: SUP_DIR follows it
    sys.path.insert(0, str(ROOT))
    import supervisor
    from supervisor import (actions, autonomy, channel, cloudllm, commitments, core, dashboard, docs, messages, metrics,
                            opconfig, version, web, web_chat, webpush, web_settings)

    # setup can be called again in the same test process after supervisor imports are cached.
    for module_name, module in list(sys.modules.items()):
        if module_name.startswith("supervisor.") and hasattr(module, "SUP_DIR"):
            mock.patch.object(module, "SUP_DIR", base / "sup").start()

    mock.patch.object(version, "SERVICES", base / "sup" / "services").start()
    mock.patch.object(core, "SETTINGS_FILE", base / "sup" / "settings.env").start()

    inst = base / "instances"
    for name in INSTANCES:
        for d in ("shaft", ".git", "notes/docs", "inbox"):
            (inst / name / d).mkdir(parents=True, exist_ok=True)
        config = inst / name / "config.env"
        if not config.exists():
            config.write_text(f'LLM_MODEL="{cloudllm.CLOUD_MODELS[0]}"\nLLM_BASE_URL="broker"\n'
                              'LLM_REASONING_EFFORT="medium"\nWAKE_INTERVAL_MINUTES="5"\nOPERATOR_NAME="Operator"\n')
        choices = inst / name / "shaft" / "config.py"
        if not choices.exists():
            choices.write_text('CHOICES = {"LLM_REASONING_EFFORT": ("low", "medium", "high", "xhigh", "max", "ultra")}\n')
    for name in ("api", "scheduler"):
        if not (base / "sup" / "services" / f"{name}.json").exists():
            _service_record(base, name, os.getpid())
    apns = _stub_apns(base)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    real_respond = web.respond

    def respond(req):                                  # what `tailscale serve` adds to every request
        req.headers = {**req.headers, "tailscale-user-login": ME}
        return real_respond(req)

    def run_now(args, timeout=60, who="top"):
        # Never fall through to actions' real subprocess runner, including for an unknown verb.
        if not args:
            raise ValueError("fixture command needs a verb")
        verb = args[0]
        if verb == "docs":
            out = []
            name, action, path = args[1:4]
            with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
                code = docs.command(name, action, path, args[5] if action == "set" else None, args[-1])
            return code == 0, out[-1] if out else ""
        if verb in ("limits", "channel", "pause", "resume"):
            name = args[1]
            if name not in INSTANCES:
                raise ValueError(f"unknown fixture instance: {name}")
            # HTML web_config actions still use these commands; app settings write in process.
            if verb == "limits":
                if len(args) != 4 or args[2] != "cloud-cap":
                    raise ValueError(f"unsupported fixture limits: {args}")
                try:
                    amount = core.limit_value("cloud-cap", args[3])
                except ValueError as e:
                    return False, str(e)
                core._update_tool_settings(name, cloud_daily_usd=amount)
            elif verb == "channel":
                try:
                    channel.set_preference(name, args[2])
                except ValueError as e:
                    return False, str(e)
            else:
                core.set_paused(name, verb == "pause", reason=who)
            return True, "saved in fixture"
        if args == ["api", "on"]:
            time.sleep(0.2)
            _restart_service(base, "api")
            return True, "fixture API restarted"
        if args in (["autonomy", "restart"], ["autonomy", "restart", "--cancel"]):
            request = base / "sup" / "scheduler-restart.json"
            drain = base / "sup" / "scheduler-drain.json"
            if "--cancel" in args:
                previous = autonomy.restart_request() or autonomy.drain_status() or {}
                request.unlink(missing_ok=True)
                _write_json(drain, {**previous, "state": "cancelled", "waiting": []})
            else:
                rec = _read_json(base / "sup" / "services" / "scheduler.json")
                now = time.time()
                pending = {"pid": rec["pid"], "at": now, "deadline": now + 3600,
                           "requested": datetime.now().isoformat(), "by": who}
                _write_json(request, pending)
                busy = _read_json(base / "services.json").get("busy", "")
                _write_json(drain, {**pending, "state": "draining", "waiting": [busy] if busy else []})
            return True, "fixture scheduler drain updated"
        raise ValueError(f"unsupported fixture command: {args}")

    def forbidden_launchctl(*args):
        raise AssertionError(f"fixture must not query launchd: {args}")

    real_alive = version._alive

    def alive(pid):
        # The initial pid really lives. Later fake pids model a replacement without spawning any process.
        return real_alive(pid) or any(
            (rec := _read_json(base / "sup" / "services" / f"{name}.json")).get("fixture")
            and rec.get("pid") == pid for name in ("api", "scheduler"))

    try:
        run_now(["unknown-fixture-verb"])
    except ValueError:
        pass
    else:
        raise AssertionError("unknown fixture commands must raise")

    for p in (mock.patch.object(dashboard, "_scheduler_running", return_value=True),
              mock.patch.object(core, "secrets_env", return_value={}),
              mock.patch.object(web_settings, "secrets_env", return_value={}),
              mock.patch.object(web, "respond", respond),
              mock.patch.object(web, "allowed_logins", return_value={ME}),
              mock.patch.object(web, "allowed_hosts", return_value=hosts),
              mock.patch.object(web, "url", return_value=f"http://127.0.0.1:{port}"),
              mock.patch.object(web, "_names", return_value=list(INSTANCES)),
              mock.patch.object(web, "_live", side_effect=lambda n: _state(base).get(n, {})),
              mock.patch.object(web, "_next_cycle", side_effect=lambda n: _next(base, n)),
              mock.patch.object(autonomy, "next_cycle", side_effect=lambda n: _next(base, n)),
              mock.patch.object(autonomy, "next_wake",
                                side_effect=lambda n, now=None, interval=None: _state(base).get(n, {}).get("next_check", "never")),
              mock.patch.object(autonomy, "event_wake", lambda *a, **k: None),
              mock.patch.object(core, "INSTANCES", inst), mock.patch.object(messages, "INSTANCES", inst),
              mock.patch.object(commitments, "INSTANCES", inst), mock.patch.object(metrics, "INSTANCES", inst),
              mock.patch.object(docs, "instance_root", lambda n: inst / n),
              mock.patch.object(actions, "run_now", side_effect=run_now),
              mock.patch.object(actions, "service_guard", side_effect=lambda name, action:
                                _read_json(base / "services.json").get("busy", "") if name == "api" else ""),
              mock.patch.object(autonomy, "_launchctl", side_effect=forbidden_launchctl),
              mock.patch.object(autonomy, "launchd_pid", side_effect=lambda name:
                                _read_json(base / "sup" / "services" / f"{name}.json").get("pid")),
              mock.patch.object(autonomy, "running", side_effect=lambda name:
                                bool(version.service(name, stale=False))),
              mock.patch.object(version, "_alive", side_effect=alive),
              mock.patch.object(opconfig, "_commit", return_value=False),
              mock.patch.object(web_chat, "_api_conn", side_effect=lambda timeout: _VoiceAPI(web_chat)),
              mock.patch.object(web_chat, "_headers", side_effect=lambda chat: {"X-Shaft-Conversation": chat["id"]}),
              mock.patch.object(web_chat, "_start", side_effect=lambda cid: _start(web_chat, cid)),
              mock.patch.object(web_chat, "_post", side_effect=lambda chat, route, body, timeout=15: (202, {})),
              mock.patch.object(web_chat, "_sync_copies", lambda *a, **k: None),
              mock.patch.dict(sys.modules, {"supervisor.apns": apns}),
              mock.patch.object(supervisor, "apns", apns, create=True)):
        p.start()
    from supervisor import voices
    if "macos" not in voices.available():
        voices.report("macos", [
            {"identifier": "fixture.voice.en", "name": "Example English", "language": "en-US", "quality": "default"},
            {"identifier": "fixture.voice.it", "name": "Example Italian", "language": "it-IT", "quality": "default"},
        ])
    return web, web_chat, messages, webpush


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, indent=1))
    os.replace(temporary, path)


def _service_record(base: Path, name: str, pid: int) -> None:
    _write_json(base / "sup" / "services" / f"{name}.json",
                {"pid": pid, "started": datetime.now().isoformat(), "commit": "fixture", "describe": "fixture", "fixture": True,
                 "settings": {"drain": True} if name == "scheduler" else {}})


def _restart_service(base: Path, name: str) -> None:
    previous = _read_json(base / "sup" / "services" / f"{name}.json")
    _service_record(base, name, max(previous.get("pid", os.getpid()), os.getpid()) + 1)


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
        if TOPIC and topic != TOPIC:
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
        from supervisor import webpush
        from supervisor.web_seen import badge
        for device in native:
            payload = webpush.apns_payload(event, badge(device))   # the real payload: instance and thread
            pushes.mkdir(parents=True, exist_ok=True)
            out = pushes / f"{time.time():.3f}-{device}.json"
            out.write_text(json.dumps(payload))
            print(f"push to {device}: {event['title']} -> {out}", flush=True)
            for udid in SIMCTL:
                subprocess.run(["xcrun", "simctl", "push", udid, native[device]["topic"], str(out)], check=False)

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


# What must scroll and never widen the column (Review Focus 1), and every language that gets
# syntax colours, in one answer. Ask for "wide" to have it streamed; the seed has it as a finished answer too.
WIDE_LINE = "totals = " + " + ".join(f"account_{i:02d}.net_daily(rounded=True)" for i in range(12))
WIDE = f"""A table of twelve columns scrolls inside its block, and so does a long line of code.

| Account | Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Note |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Operating | 12,401.19 | 12,388.02 | 12,512.77 | 12,640.11 | 12,702.40 | 12,655.93 | 12,811.08 | 12,790.64 | 12,902.15 | 13,011.72 | rounded once, at the end |
| Reserve | 3,050.00 | 3,050.00 | 3,050.00 | 3,100.00 | 3,100.00 | 3,100.00 | 3,150.00 | 3,150.00 | 3,150.00 | 3,200.00 | topped up each quarter |

Short tables hug their text, and numbers line up on the right without being told to:

| Run | Tests | Failed |
|---|---|---|
| nightly | 412 | 0 |
| weekly | 1,208 | 3 |

```python
{WIDE_LINE}  # {len(WIDE_LINE)} characters
```

```swift
struct Ledger {{
    let name = "operating" // the main account
    func net(_ rows: [Row]) -> Int {{ rows.reduce(0) {{ $0 + $1.amount }} }}
}}
```

```bash
# rerun the report
for month in 09 10; do shaft report --month "2026-$month"; done
```

```sql
SELECT account, SUM(amount) FROM ledger WHERE month = 10 GROUP BY account; -- the close
```

```diff
@@ -1,2 +1,2 @@
-rounded twice
+rounded once
```

```json
{{"account": "reserve", "total": 3050.0, "closed": true}}
```

```
no language: plain text stays plain
```

> A quoted line from the plan, with `inline code` in it.

---

#### A small heading

Some `inline code`, a [link](https://example.com), **bold** and *emphasis* in one paragraph, which runs long enough to wrap across two lines at least on a phone.

1. first
2. second
"""


VOICE_ANSWER = "The garden gate is open. The lantern is beside the door."
VOICE_LONG = " ".join(f"Lantern number {i} lights the garden path." for i in range(1, 11))


class _VoiceAPI:
    """The fake upstream connection; the real web_chat._run reads its API-shaped SSE chunks."""

    status = 200

    def __init__(self, web_chat):
        self.chat = web_chat
        self.sock = self
        self.closed = threading.Event()

    def request(self, method, path, body, headers):
        self.body = json.loads(body)
        self.cid = headers["X-Shaft-Conversation"]

    def getresponse(self):
        return self

    def getheader(self, name, default=None):
        """The one header the relay reads: the conversation id the real API answers with (X-Shaft-Talk), so the
        thread records it as a real one does and its messages route to it."""
        return f"api-{self.cid[4:14]}" if name.lower() == "x-shaft-talk" else default

    def shutdown(self, how):
        self.closed.set()

    def close(self):
        self.closed.set()

    def __iter__(self):
        from supervisor.api import _chunk, _note

        def chunk(delta=None, **extra):
            return _chunk(self.cid, self.body["model"], delta or {}, **extra)

        def event(kind, **fields):
            return chunk(shaft_event={"type": kind, **fields})

        def take_interjections():
            # Like the agent, take messages at a step boundary, not while drafting its answer.
            with self.chat._lock(self.cid):
                reply = self.chat.load(self.cid)["reply"]
                return reply.get("interjections", 0)

        ask = next(m["content"] for m in reversed(self.body["messages"]) if m["role"] == "user").lower()
        heard = take_interjections()
        yield chunk({"role": "assistant", "content": ""})
        yield chunk({"reasoning_content": "model · thinking\n"})
        yield event("status", phase="model", detail="thinking")
        if "slow" in ask and self.closed.wait(4.5):      # a long think with nothing to say: the waiting sound
            return
        if "tool" in ask:
            narration = "I will check the garden notes."
            yield event("draft", text=narration)
            yield event("step_end", answer=False)
            yield chunk({"reasoning_content": narration + "\n"})
            for i, name in enumerate(("read_file", "web_search")):
                if i and self.closed.wait(0.3):
                    return
                yield chunk({"reasoning_content": _note({"type": "tool_start", "name": name})})
                yield event("tool_start", name=name)
                if self.closed.wait(1.5):
                    return
                yield chunk({"reasoning_content": _note({"type": "tool_end", "name": name, "ok": True})})
                yield event("tool_end", name=name, ok=True)
            heard = take_interjections()
        answer = VOICE_LONG if "long" in ask else VOICE_ANSWER
        pieces = [answer[i:i + 8] for i in range(0, len(answer), 8)]
        delay = 6 / (len(pieces) - 1) if "long" in ask else 0.08
        for i, piece in enumerate(pieces):
            if self.closed.is_set() or (i and self.closed.wait(delay)):
                return
            yield event("draft", text=piece)
        yield event("step_end", answer=True)
        yield chunk({"content": answer})
        yield chunk(finish="stop", interjections_heard=heard)
        yield b"data: [DONE]\n\n"


def _start(web_chat, cid: str) -> None:
    relay = web_chat.Relay(cid)
    web_chat._relays[cid] = web_chat._streams[cid] = relay
    if web_chat.spoken(web_chat.load(cid)):
        target, args = web_chat._run, (relay,)
    else:
        target, args = _run, (web_chat, relay)
    threading.Thread(target=target, args=args, daemon=True, name=f"fake-{cid}").start()


def _run(web_chat, relay) -> None:
    cid = relay.cid
    with web_chat._lock(cid):
        chat = web_chat.load(cid)
    ask = next((t.get("shown") or t["content"] for t in reversed(chat["turns"]) if t["role"] == "user"), "")
    slow = "slow" in ask.lower()
    steps = ["read_file notes/docs/ledger.md", "run tests: 412 passed", "write notes/docs/ledger.md",
             "check the October totals"]
    answer = WIDE if "wide" in ask.lower() else ANSWER.format(ask=(ask.splitlines() or ["your message"])[-1][:60])
    for c in chat["reply"].get("comment_ids", []):      # it answers the comments it was sent, as an instance does
        answer += f"\n\n### {c}\nDone: I reworded that passage."
    words = answer.split(" ")
    activity, said = [], ""
    heard = chat["reply"].get("interjections", 0)
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


def _seed_cycles(base: Path, now: datetime) -> None:
    """Only Tundra has cycle records; Wisp keeps the Cycles empty states."""
    root = base / "instances" / "tundra"
    episodes = root / "memory" / "episodes"
    episodes.mkdir(parents=True, exist_ok=True)
    end = now.replace(hour=9, minute=0, second=0)
    records = [
        {"kind": "live", "ts": end.isoformat(),
         "run_started": (end - timedelta(seconds=90)).isoformat(),
         "intent": {"goal": "Riconciliare il registro", "success_criterion": "I totali coincidono",
                    "origin": "operator", "confidence": 80, "predictions": ["Nessuna differenza"]},
         "finish": {"criterion_met": "yes", "what_happened": "Ho corretto due arrotondamenti.",
                    "delivered_via": "web_document",
                    "doubts": [{"step": 1, "what": "Manca la verifica di novembre."}],
                    "unfinished": ["Pubblicare novembre"],
                    "next_action": {"action": "Controllare novembre", "revise_if": "Arrivano nuovi dati"}},
         "project_usage": {"usd": 0.04}, "ended_by": "finish_cycle", "tools": []},
        {"kind": "live", "ts": (end - timedelta(hours=1)).isoformat(),
         "intent": {"goal": "Controllare i conti", "origin": "self"},
         "finish": {"criterion_met": "partial", "what_happened": "Mancano due ricevute."}, "tools": []},
        {"kind": "live", "ts": (end - timedelta(days=1)).isoformat(),
         "intent": {"goal": "Leggere le ricevute"}, "ended_by": "limit", "tools": []},
        {"kind": "sleep", "ts": (end - timedelta(days=1, hours=1)).isoformat(),
         "content": "## Riposo\n\nHo rivisto il lavoro di ieri.", "ended_by": "sleep", "tools": []},
    ]
    for days_ago in range(2, 8):
        records.append({
            "kind": "live", "ts": (end - timedelta(days=days_ago)).isoformat(),
            "intent": {"goal": f"Verificare il registro, giorno {days_ago}"},
            "ended_by": "limit", "tools": [],
        })
    by_day = {}
    for record in records:
        by_day.setdefault(record["ts"][:10], []).append(record)
    for day, rows in by_day.items():
        (episodes / f"{day}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in reversed(rows)))
    (root / "self").mkdir(exist_ok=True)
    (root / "self" / "goals.md").write_text("# Obiettivi\n\nConsegnare un registro verificabile.\n")
    (root / "notes" / "queue.json").write_text(json.dumps([
        {"id": 1, "task": "Controllare novembre", "due_at": end.date().isoformat(), "status": "open"},
        {"id": 2, "task": "Inviare il riepilogo", "due_at": "", "status": "open"},
        {"id": 3, "task": "Rivedere le ricevute", "due_at": "dopo la revisione", "status": "open"},
    ]))
    (root / "memory" / "commitments.jsonl").write_text(json.dumps({
        "event": "made", "id": "c1105-0900-1", "at": end.isoformat(), "source": "cycle",
        "promise": "Inviare il registro", "criterion": "Registro consegnato", "made_to": "operator",
        "due": (end + timedelta(days=1)).isoformat(),
    }) + "\n")
    (root / "memory" / "journal.md").write_text(
        f"## {end:%Y-%m-%d %H:%M} — Sleep\n\n**How I will improve:** controllare prima i totali.\n\n"
        "*Consolidation actions:* nessuna.\n")
    judgments = base / "sup" / "judgments"
    judgments.mkdir(exist_ok=True)
    (judgments / "tundra.jsonl").write_text(json.dumps({
        "episode_ts": end.isoformat(), "criterion_met": "partial", "claim_supported": False,
        "first_error": "Manca la verifica di novembre.",
        "reasoning": "Manca la verifica di novembre.", "judge": "fixture judge",
    }) + "\n")


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

    _seed_cycles(base, now)

    size = _png(inst / "tundra" / "attachments" / "ci-run.png")
    (inst / "tundra" / "attachments" / "totals.csv").write_text("account,total\noperating,12401.19\nreserve,3050.00\n")
    report = "# October report\n\n**Net** totals, rounded *once*.\n\n- [x] operating\n- [ ] reserve\n\n| account | total |\n" \
             "| --- | ---: |\n| operating | 12401.19 |\n\n```python\nprint(sum(totals))\n```\n"
    (inst / "tundra" / "attachments" / "report.md").write_text(report)
    docs_dir = inst / "tundra" / "notes" / "docs"
    (docs_dir / "ledger.md").write_text(
        "# Ledger\n\nNet daily totals are rounded once, at the end.\n\n## October\n\n| Account | Total |\n|---|---:|\n"
        "| Operating | 12,401.19 |\n| Reserve | 3,050.00 |\n\n- [x] reconcile\n- [ ] publish\n")
    (docs_dir / "plans").mkdir(exist_ok=True)
    (docs_dir / "plans" / "q4.md").write_text("# Q4 plan\n\nShip the nightly report.\n")
    (inst / "wisp" / "notes" / "docs" / "garden.md").write_text("# Garden\n\nWater the basil.\n")

    cid = "web-3f9a1c0d7e2b4a65"
    yesterday = "web-77ab02c4e9d15f30"
    web_chat.save({"id": yesterday, "instance": "tundra", "login": ME, "created": ts(60 * 26), "state": "ended",
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
         "mid": "seed-1", "attachments": [{"name": "ci-run.png", "path": "instances/tundra/attachments/ci-run.png",
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
        {"role": "user", "ts": ts(11), "content": "Show me everything that has to scroll.",
         "shown": "Show me everything that has to scroll.", "mid": "seed-w"},
        {"role": "assistant", "ts": ts(10.5), "content": WIDE, "activity": ["read_file notes/docs/ledger.md"]},
        {"role": "user", "ts": ts(10), "content": "And November?", "shown": "And November?", "mid": "seed-3"},
        {"role": "assistant", "ts": ts(9.5), "content": "", "note": "error: CloudError: OpenRouter HTTP 502",
         "note_kind": "model", "note_retry": True, "retry_text": "And November?"},
    ]
    web_chat.save({"id": cid, "instance": "tundra", "login": ME, "created": ts(60 * 24), "state": "idle",
                   "reply": None, "turns": turns})
    msg(ts(15), "seed-from-tundra-q.md", "Should the nightly report keep the **per-account** lines?", "tundra",
        "operator", False, kind="question")
    # Spaces distinguish the raw sent.file identity from the URL-encoded feed key.
    msg(ts(14), "seed from tundra m.md", "The totals, as a file:", "tundra", "operator", True,
        attachments=[{"name": "totals.csv", "path": "instances/tundra/attachments/totals.csv", "bytes": 51},
                     {"name": "report.md", "path": "instances/tundra/attachments/report.md", "bytes": len(report)}])
    # The real feed derives cancellable/cancelled from the inbox file and cancellation ledger.
    msg(ts(12), "seed-from-operator-b.md", "Look at the March reconciliation when you have time.", "operator",
        "tundra", False, via=messages.BACKGROUND, mid="seed-b")
    msg(ts(11), "seed-next-cycle-cancel.md", "Recheck the summary next cycle.", "operator",
        "tundra", False, via=messages.BACKGROUND, thread=cid)
    # Its cycle's answer to work left in this thread, as the broker records one (`thread`, no talk): "⏾ from its cycle"
    msg(ts(8), "seed-from-tundra-cycle.md", "March reconciles: 3 lines moved.", "tundra", "operator", True,
        talk=None, thread=cid)
    # An answer for a thread that had ended: in From, "re: «title»" opening that earlier chat
    msg(ts(7), "seed-from-tundra-re.md", "September closed clean too.", "tundra", "operator", True,
        talk=None, re=yesterday)
    with feedback.FILE.open("a") as f:
        f.write(json.dumps({"ts": ts(17), "event": "rated", "id": "f-" + "6a0c2e4b8d1f3a5c7e9b0d2f", "instance": "tundra",
                            "login": ME, "target": {"kind": "chat", "chat": cid, "k": 7}, "thumb": "up",
                            "reason": "Exactly the summary I wanted", "reply_ts": turns[7]["ts"],
                            "reply_sha": "0" * 64}) + "\n")
    doc_comments.add_thread("tundra", "p-1e2d3c4b5a697887", "ledger.md",
                            {"exact": "rounded once, at the end", "prefix": "Net daily totals are ", "suffix": ".",
                             "start": 28},
                            __import__("hashlib").sha256((docs_dir / "ledger.md").read_bytes()).hexdigest(),
                            "Per account or overall?")
    doc_comments.mark_sent("tundra", "p-1e2d3c4b5a697887", f"{cid}#2")
    doc_comments.reply("tundra", "p-9a8b7c6d5e4f3021", "c1", "Per account, then summed.", "tundra")

    web_chat.save({"id": "web-00000000000000a1", "instance": "wisp", "login": ME, "created": ts(90), "state": "idle",
                   "reply": None, "turns": [
                       {"role": "user", "ts": ts(90), "content": "Water the basil?", "shown": "Water the basil?",
                        "mid": "seed-wisp"},
                       {"role": "assistant", "ts": ts(89), "content": "Watered. The soil was *dry*."}]})
    (base / "state.json").write_text(json.dumps({
        "tundra": {"state": "in a cycle", "since": now.strftime("%H:%M"), "model": "cloud claude-sonnet-5",
                   "usd_today": 0.42, "goal": "Controllare novembre", "next_check": "18:30",
                   "next_cycle": "a cycle is running and read its inbox when it began: this starts the next one once it ends"},
        "wisp": {"state": "idle", "model": "qwen3.6-35b", "next_cycle": "for wisp's next cycle, which this starts now"},
        "ada": {"state": "sleeping", "since": "until 18:30", "model": "qwen3.6-35b"}}, indent=1))
    (base / "seeded").write_text(now.isoformat())


# -- the commands -------------------------------------------------------------------------------------------------------
def act(base: Path, web, web_chat, messages, webpush, what: str, args: list[str]) -> None:
    name = args[0] if args else "tundra"
    text = " ".join(args[1:])
    now = datetime.now().replace(microsecond=0).isoformat()
    if what == "services":
        if name == "busy":
            _write_json(base / "services.json", {"busy": text})
            drain = base / "sup" / "scheduler-drain.json"
            if (record := _read_json(drain)).get("state") == "draining":
                _write_json(drain, {**record, "waiting": [text] if text else []})
        elif name == "drain-end":
            request = base / "sup" / "scheduler-restart.json"
            if request.exists():
                _restart_service(base, "scheduler")
                _write_json(base / "sup" / "scheduler-drain.json",
                            {**_read_json(request), "state": "drained", "waiting": []})
                request.unlink()
        else:
            raise SystemExit("services needs busy <text> or drain-end")
    elif what in ("message", "question"):
        file = f"{datetime.now():%Y%m%d-%H%M%S-%f}-from-{name}.md"
        text = text or ("Should I archive the September notes?" if what == "question" else "The nightly report is ready.")
        # a message from a cycle, as the broker records one: `talk` null (no conversation), so it shows in From
        row = {"ts": now, "from": name, "to": "operator", "kind": what, "file": file, "text": text, "talk": None}
        with (base / "sup" / "messages.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")
        box = base / "sup" / "operator-inbox"
        box.mkdir(parents=True, exist_ok=True)
        (box / file).write_text(text)
        from supervisor import web_alerts             # the real event: it opens the thread or From the row belongs to
        event = web_alerts.event(row)
        webpush.notify(event, skip=web_chat.watchers(name, event["cid"]))
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
    p.add_argument("--slow-seen", type=float, default=0, help="seconds to hold each mark-seen answer")
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
    if a.slow_seen > 0:
        from supervisor import web_seen
        real_seen = web_seen.seen

        def slow_seen(*args, **kwargs):
            time.sleep(a.slow_seen)
            return real_seen(*args, **kwargs)
        web_seen.seen = slow_seen
    from supervisor.version import record_service
    record_service("web", {"port": a.port})             # as web.serve does: the version the app's Settings shows
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

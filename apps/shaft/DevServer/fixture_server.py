"""A throwaway SHAFT web service for driving the native apps without real data (apps/shaft/README.md, "Try it against a
fixture server").

It runs the real `/app/v1` (supervisor/app_api.py, web_chat, docs, comments, feedback, the push fan-out) on 127.0.0.1,
over a temporary SUP_DIR and instances folder it seeds itself, never the real `.supervisor/` or `instances/`. What it
fakes is what lies outside the web service, as supervisor/tests/test_app_api.py does:

- the tailnet: every request gets the login `tailscale serve` would add, and the host is 127.0.0.1:<port>; but a
  request of a device paired as a Mac app (platform `macos`) to /app/v1 comes as the Mac app on the server's own Mac
  does, on loopback without that header: the setup routes that only loopback may use (docs/app-api.md §18) answer it;
- the API a reply streams from: a send starts a scripted reply here (activity, an answer streamed in pieces, then the
  real `web_chat._finish`); a message containing "fail" fails as a model 502 does, "slow" streams for a minute,
  "wide" answers with a 12-column table and a 400-character line of code;
- APNs: a push is written to <dir>/pushes/ with the payload supervisor/apns.py would send, and with `--simctl`
  delivered to that simulator by `xcrun simctl push`;
- the instances' live state (`web._live`) and Next cycle's hint: from <dir>/state.json;
- settings: config and tool settings belong to the fixture's instances, and writes run in process;
- services: fake records start with this process's live pid; restarts change only those records. No CLI or launchd
  is called. Only docs, limits, channel, pause, resume, api on, api restart, web restart and autonomy restart are
  allowed by the command stub;
- the setup verbs behind /app/v1/setup/* (docs/app-api.md §18) the Mac app's wizard calls: `new` makes the instance's
  folders here (paused, with its first-reply marker), `config set` and `drive set` write them, `first-reply` is the real
  verb on this folder, `secrets set` keeps only the key's name in <dir>/setup.json (never its value), and `provider use`
  and `provider check` answer as the real verbs would: OpenRouter covers everything once its key was set, and is off
  with the missing key before; a check fails for a model whose name contains "fail". The drives come from a fixture
  template (<dir>/template/drive.md). Access (§18.6): `share` is the real check and setting on this folder, `browser
  install` only records a fake Chromium version in <dir>/setup.json, `tools <n> on|off <tool>` writes the instance's
  tool settings here, and `telegram token` keeps the instance's name (never the token), refusing a token without ":"
  or containing "bad"; web search reads as set once SCRAPEBADGER_API_KEY was saved. `telegram on` writes a live
  service record (never launchd); `telegram pair --json` issues a real code on this folder, and the fixture "sends" it
  to the bot PAIR_AFTER_S (3 s) later, so the wizard sees the pairing land.
- Google (§18.6–18.7): never Google. The app runs `shaft google client|login` itself, against the installed server, which
  a fixture run has not: `act google [address]` stands in for that login, writing an account `google` (a fake token) on
  this folder. Its labels and calendars are GOOGLE_LABELS and GOOGLE_CALENDARS; `mail|calendar|gdrive|gchat <n> grant`
  and their revokes run the real grant setters here, refusing a label or calendar the account does not have, and a
  label whose name contains "Refused" (to drive the wizard's first-failure stop).

    python3 apps/shaft/DevServer/fixture_server.py serve --dir /tmp/shaft-fixture --port 8899 [--simctl <udid>] [--slow-seen 5]
    python3 apps/shaft/DevServer/fixture_server.py pair  --dir /tmp/shaft-fixture --port 8899 [--name iphone-app]
    python3 apps/shaft/DevServer/fixture_server.py act   --dir /tmp/shaft-fixture --port 8899 <what> [text]

`act` (run while `serve` runs, on the same folder; pass the same --simctl): `google [address]` (the operator's Google
account `google` signed in, as the app's local `shaft google login` would), `message <instance> [text]` and
`question <instance> [text]` (the instance sends you one, with a push; `choice` and `choices` the same with
single- and multiple-choice options), `state <instance> <state>` (its state in the
rail), `doc <instance> <path> [text]` (it writes a document), `long <instance> [turns]` (a chat past the 80-turn
window), `rich <instance> [turns]` (Main grows by turns with answers the size of real ones, for timing the chat
screen). `services busy <text>` sets the API refusal and the scheduler's waiting list (empty clears it);
`services drain-end` finishes a pending scheduler drain and changes its fake pid. `version <api> [server]` makes
GET /app/v1/version answer as a server of that API version, `version missing` as one older than the route (404), and
`version` alone the real answer again (docs/app-api.md §19).

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
PAIR_AFTER_S = 3.0          # a pairing code "sent to the bot" this long after the wizard asked for it (tests shorten it)
TOPIC = os.environ.get("SHAFT_APNS_TOPIC", "")
INSTANCES = ("tundra", "wisp", "ada")
GOOGLE_LABELS = ["Assistant", "Receipts", "Refused here", "Travel"]
GOOGLE_CALENDARS = [{"id": ME, "title": ME, "summary": ME, "primary": True, "access": "owner"},
                    {"id": "team@group.calendar.google.com", "title": "Team", "summary": "Team", "primary": False,
                     "access": "reader"}]


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
    from supervisor import __main__ as shaft_cli       # the real `first-reply` verb, on this folder (SUP_DIR below)
    from supervisor import setup as setup_access       # GET/POST /access answers (§18.6)
    from supervisor import calendar as calendar_mod, google as google_mod, mail as mail_mod   # never Google here
    from supervisor import telegram as telegram_mod    # the real pairing state, on this folder (SUP_DIR above)

    # setup can be called again in the same test process after supervisor imports are cached: move SUP_DIR and every
    # module-level path built from it at import (feedback.FILE = SUP_DIR / "feedback.jsonl"), or a second test file
    # writes into the first one's deleted folder.
    old_sup, new_sup = core.SUP_DIR.resolve(), (base / "sup").resolve()
    for module_name, module in list(sys.modules.items()):
        if not module_name.startswith("supervisor."):
            continue
        for attr, value in list(vars(module).items()):
            if isinstance(value, Path) and old_sup != new_sup and (value == old_sup or old_sup in value.parents):
                mock.patch.object(module, attr, new_sup / value.relative_to(old_sup)).start()
        if hasattr(module, "SUP_DIR"):
            mock.patch.object(module, "SUP_DIR", base / "sup").start()

    mock.patch.object(version, "SERVICES", base / "sup" / "services").start()
    mock.patch.object(core, "SETTINGS_FILE", base / "sup" / "settings.env").start()

    inst = base / "instances"
    template = base / "template"
    template.mkdir(exist_ok=True)
    if not (template / "drive.md").exists():
        (template / "drive.md").write_text(TEMPLATE_DRIVE)

    def make_instance(name: str) -> Path:
        root = inst / name
        for d in ("shaft", ".git", "notes/docs", "inbox"):
            (root / d).mkdir(parents=True, exist_ok=True)
        config = root / "config.env"
        if not config.exists():
            config.write_text(f'LLM_MODEL="{cloudllm.CLOUD_MODELS[0]}"\nLLM_BASE_URL="broker"\n'
                              'LLM_REASONING_EFFORT="medium"\nWAKE_INTERVAL_MINUTES="5"\nOPERATOR_NAME="Operator"\n')
        choices = root / "shaft" / "config.py"
        if not choices.exists():
            choices.write_text('CHOICES = {"LLM_REASONING_EFFORT": ("low", "medium", "high", "xhigh", "max", "ultra")}\n')
        return root

    for name in INSTANCES:
        make_instance(name)

    def names() -> list[str]:
        """The seeded instances, then those the setup wizard created here (`new`)."""
        made = sorted(d.name for d in inst.iterdir() if d.name not in INSTANCES and (d / "shaft").is_dir())
        return [*INSTANCES, *made]
    for name in ("api", "scheduler", "web"):        # web: the release GET /app/v1/version names (serve records its own)
        if not (base / "sup" / "services" / f"{name}.json").exists():
            _service_record(base, name, os.getpid())
    apns = _stub_apns(base)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    real_respond = web.respond

    mock.patch.object(web, "LOCAL", True).start()     # the Mac app's loopback path (SHAFT_WEB_LOCAL=on), on this port
    mock.patch.object(web, "local_hosts", return_value=hosts).start()

    def mac_app(req) -> bool:
        """A request of a device paired as the Mac app, to /app/v1: it comes on loopback, as on the server's own Mac."""
        _, _, cred = (req.headers.get("authorization") or "").partition(" ")
        dev = cred.strip().partition(".")[0]
        return req.path.startswith("/app/v1/") and (web.devices().get(dev) or {}).get("platform") == "macos"

    def respond(req):                                  # what `tailscale serve` adds to every request
        if not mac_app(req):
            req.headers = {**req.headers, "tailscale-user-login": ME}
        response = real_respond(req)
        # `act version`: answer as an older server would (§19), once the real route has checked the device.
        old = _read_json(base / "version.json")
        if old and response.status == 200 and req.path.split("?")[0] == "/app/v1/version":
            from supervisor import app_api
            if old.get("missing"):
                return app_api.error(404, "not_found", f"Nothing at {req.path}.")
            return app_api._json(200, {"server": old.get("server", "v0.0.1"), "api": old["api"]})
        return response

    setup_state = base / "setup.json"            # the key names set (never their values) and the provider in use
    from supervisor import app_api
    # The instances `new` finished (the real check reads the mind store's creation commit, which the fake `new` makes
    # no git store for): a retry of the wizard finishes those, and refuses one `new` left halfway.
    mock.patch.object(app_api, "_unfinished", side_effect=lambda name: None if name in _read_json(setup_state).get(
        "created", []) else "its memory store was never made").start()

    def run_json(args, timeout=60, who="top", input=None):
        # The setup verbs (docs/app-api.md §18), faked over this folder: never the real subprocess runner, which would
        # run them against this checkout's own .env and instances. An unknown verb raises.
        if not args:
            raise ValueError("fixture command needs a verb")
        state = _read_json(setup_state)
        if args[0] == "new" and len(args) >= 2:
            name = args[1]
            then = args[args.index("--paused-until-first-reply") + 1] if "--paused-until-first-reply" in args else None
            if not core.NAME_RE.match(name) or name == "operator":
                return 2, [{"ok": False, "error": "instance names: lowercase letters, digits and '-', starting with a "
                                                  "letter (not 'operator')"}]
            if (inst / name).exists():
                return 2, [{"ok": False, "error": f"instance {name!r} already exists"}]
            if then:                                   # as `shaft new`: paused before its folders exist
                core.set_paused(name, True, reason="waiting for the operator's first reply", note=False)
                shaft_cli._write_first_reply(name, then)
            root = make_instance(name)
            (root / "drive.md").write_text((template / "drive.md").read_text())
            state["created"] = sorted({*state.get("created", []), name})      # `new` finished: its mind store's commit
            _write_json(setup_state, state)
            return 0, [{"ok": True, "name": name, "branch": f"instance/{name}", "worktree": str(root),
                        "paused": bool(then)}]
        if args[0] == "first-reply" and len(args) >= 3:
            out: list[str] = []
            with mock.patch.object(shaft_cli, "print", create=True,
                                   side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
                code = shaft_cli.cmd_first_reply(args[1], args[2], True)
            return code, [json.loads(line) for line in out]
        if args[:2] == ["secrets", "set"] and len(args) >= 3:
            if not (input or "").strip():
                return 1, [{"ok": False, "error": f"no value for {args[2]} on stdin"}]
            state["secrets"] = sorted({*state.get("secrets", []), args[2]})
            _write_json(setup_state, state)
            return 0, [{"ok": True, "name": args[2]}]
        if args == ["secrets", "list", "--json"]:
            return 0, [{"ok": True, "names": state.get("secrets", [])}]
        if args == ["telegram", "pair", "--json"]:          # the real code on this folder; "sent" PAIR_AFTER_S later
            try:
                code = telegram_mod.new_pairing_code()
            except telegram_mod.TelegramError as e:
                return 1, [{"ok": False, "error": str(e)}]
            timer = threading.Timer(PAIR_AFTER_S, telegram_mod.try_pair, ({"id": 1, "username": "fixture_operator"}, code))
            timer.daemon = True
            timer.start()
            return 0, [{"ok": True, "code": code, "minutes": telegram_mod.PAIR_MINUTES}]
        if args[:2] == ["provider", "use"] and len(args) >= 3:
            return provider_use(args[2], _flags(args[3:]), state)
        if args[:2] == ["provider", "check"] and len(args) >= 3:
            return provider_check(args[2], state)
        raise ValueError(f"the fixture server has no fake for {' '.join(args[:2])}")

    def provider_use(provider: str, flags: dict, state: dict):
        if provider == "custom":
            for need, why in (("model", "custom needs --model (the model name on the endpoint)"),
                              ("price_in", "custom needs --price-in and --price-out (dollars per million tokens): an "
                                           "unpriced model would spend money the daily cap cannot count"),
                              ("price_out", "custom needs --price-in and --price-out (dollars per million tokens): an "
                                            "unpriced model would spend money the daily cap cannot count"),
                              ("base_url", "custom needs --base-url (the OpenAI-compatible endpoint, e.g. https://host/v1)")):
                if need not in flags:
                    return 2, [{"ok": False, "error": why}]
            model = "custom/" + flags["model"].removeprefix("custom/")
        elif "model" in flags and provider == "openrouter" and not flags["model"].startswith("openrouter/"):
            return 2, [{"ok": False, "error": f"--model for openrouter is an openrouter/… model, not {flags['model']!r}"}]
        else:
            model = flags.get("model") or {"openrouter": "openrouter/xiaomi/mimo-v2.6-pro",
                                           "codex": "codex/gpt-6-astra"}[provider]
        functions = ["instances", "judge", "judge2", "coder", "consult", "describe", "recap", "trial"]
        off: dict[str, str] = {}
        if provider == "openrouter" and "OPENROUTER_API_KEY" not in state.get("secrets", []):
            off = {fn: "OPENROUTER_API_KEY is not set (shaft secrets set OPENROUTER_API_KEY)" for fn in functions}
        if provider == "codex":
            functions.remove("instances")          # an instance's own model is chosen per instance
        if provider == "custom":
            functions.remove("trial")              # `none`: a choice, not a gap
            if not flags.get("vision"):
                off["describe"] = f"{model} does not read images: mark it with SHAFT_CUSTOM_VISION"
        judge = "custom/" + flags["judge_model"].removeprefix("custom/") if flags.get("judge_model") else None
        state["provider"] = {"name": provider, "models": [m for m in (model, judge) if m]}
        _write_json(setup_state, state)
        return 0, [{"ok": True, "provider": provider, "model": model, "changed": ["SHAFT_JUDGE_MODELS"], "written": {},
                    "covers": [fn for fn in functions if fn not in off], "off": off, "not_live": {}}]

    def provider_check(provider: str, state: dict):
        used = state.get("provider") or {}
        if used.get("name") != provider:
            return 1, [{"ok": False, "model": None,
                        "error": f"no {provider}/ model is configured: run shaft provider use {provider}"}]
        rows = []
        for model in used["models"]:
            error = None
            if provider == "openrouter" and "OPENROUTER_API_KEY" not in state.get("secrets", []):
                error = "OpenRouter HTTP 401: No auth credentials found"
            elif "fail" in model:
                error = f"{model}: HTTP 404: the fixture's endpoint has no such model"
            rows.append({"ok": error is None, "model": model, "error": error})
        return (0 if all(r["ok"] for r in rows) else 1), rows

    def run_now(args, timeout=60, who="top", input=None):
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
        if verb == "config" and len(args) >= 4 and args[2] == "set":
            name = args[1]
            if name not in names():
                return False, f"no instance named {name!r}"
            config = inst / name / "config.env"
            lines = config.read_text().splitlines()
            for pair in args[3:]:
                key, _, value = pair.partition("=")
                lines = [l for l in lines if not l.startswith(f"{key}=")] + [f'{key}="{value}"']
            config.write_text("\n".join(lines) + "\n")
            return True, f"{name}: saved {', '.join(p.partition('=')[0] for p in args[3:])}"
        if verb == "drive" and len(args) >= 7 and args[2] == "set":          # the real verb, on this folder
            out = []
            with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
                code = opconfig.drive_command(args[1], "set", args[args.index("--file") + 1],
                                              args[args.index("--expect-sha") + 1])
            return code == 0, out[-1] if out else ""
        if verb == "share" and len(args) == 3:             # the real check and setting, on this folder
            name = args[1]
            if name not in names():
                return False, f"no instance named {name!r}"
            try:
                path = core.set_shared_folder(name, None if args[2] == "off" else args[2])
            except ValueError as e:
                return False, str(e)
            return True, f"{name} may read {path} as shared/ (read-only)" if path else f"{name} has no shared folder"
        if args == ["browser", "install"]:                # never Playwright: a version recorded here
            state = _read_json(setup_state)
            state["browser"] = "1.58.0-fixture"
            _write_json(setup_state, state)
            return True, "Chromium 1.58.0-fixture installed"
        if verb == "tools" and len(args) == 4 and args[2] in ("on", "off"):
            name, tool = args[1], args[3]
            if name not in names():
                return False, f"no instance named {name!r}"
            off = core.disabled_tools(name)
            core._update_tool_settings(name, off=sorted(off - {tool} if args[2] == "on" else off | {tool}))
            return True, f"{name}: {tool} {args[2]}"
        if verb in ("mail", "calendar", "gdrive", "gchat") and len(args) >= 4 and args[2].startswith(("grant", "revoke")):
            return google_grant(args)
        if args[:2] == ["telegram", "token"] and len(args) == 3:
            name, token = args[2], (input or "").strip()
            if ":" not in token or "bad" in token:
                return False, "not saved: Telegram refused the token (Unauthorized)"
            state = _read_json(setup_state)
            state["telegram"] = sorted({*state.get("telegram", []), name})
            _write_json(setup_state, state)
            telegram_mod.remember_bot(name, f"{name}_fixture_bot")
            return True, f"saved: {name} answers on Telegram as @{name}_fixture_bot"
        if args == ["telegram", "on"]:                    # never launchd: a live record of this process
            if not _read_json(setup_state).get("telegram"):
                return False, "no bot tokens yet: shaft telegram token <instance>"
            _service_record(base, "telegram", os.getpid())
            return True, "telegram installed (fixture): starts at login, restarted if it stops"
        if verb in ("limits", "channel", "pause", "resume"):
            name = args[1]
            if name not in names():
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
        if args in (["api", "on"], ["api", "restart"]):
            time.sleep(0.2)
            _restart_service(base, "api")
            return True, "fixture API restarted"
        if args == ["web", "restart"]:
            _restart_service(base, "web")
            return True, "fixture web service restarted"
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

    def google_grant(args):
        # The grant verbs over this folder's account and lists: the real setters, never Google.
        verb, name, op, alias = args[:4]
        what = args[4] if len(args) > 4 else ""
        if name not in names():
            return False, f"no instance named {name!r}"
        if not google_mod.account(alias):
            return False, f"no Google account {alias!r} (logged in: {', '.join(google_mod.accounts()) or 'none'})"
        if op.startswith("revoke"):                     # never contacts Google: the stored grant only
            return google_revoke(verb, name, op, alias, what)
        if verb == "mail":
            if what not in GOOGLE_LABELS or "Refused" in what:
                return False, f"not granted: Gmail has no label {what!r} for {alias} (fixture)"
            core.set_mail_grants(name, core.mail_grants(name) + [{"account": alias, "label": what,
                                                                  **({"draft": True} if op == "grant-draft" else {})}])
            return True, f"{name}: mail {op} {alias}/{what}"
        if verb == "calendar":
            cal = next((c for c in GOOGLE_CALENDARS if what in (c["id"], c["title"]) or (what == "primary" and c["primary"])),
                       None)
            if not cal:
                return False, f"not granted: {alias} has no calendar {what!r} (fixture)"
            grants = [g for g in core.calendar_grants(name) if (g["account"], g["calendar"]) != (alias, cal["id"])]
            prior = next((g for g in core.calendar_grants(name) if (g["account"], g["calendar"]) == (alias, cal["id"])), {})
            write = op == "grant-write" or prior.get("write") is True
            if op == "grant-write" and cal["access"] not in ("owner", "writer"):
                return False, f"not granted: {alias} may only read {cal['title']} (fixture)"
            core.set_calendar_grants(name, grants + [{"account": alias, "calendar": cal["id"], "name": cal["title"],
                                                      **({"write": True} if write else {})}])
            return True, f"{name}: calendar {op} {alias}/{cal['id']}"
        if verb == "gdrive":
            core.set_gdrive_grants(name, [g for g in core.gdrive_grants(name) if g["account"] != alias]
                                   + [{"account": alias, "reach": "all" if "--all" in args else "own"}])
            return True, f"{name}: Drive {alias}"
        core.set_gchat_grants(name, [g for g in core.gchat_grants(name) if g["account"] != alias] + [{"account": alias}])
        return True, f"{name}: Chat {alias}"

    def google_revoke(verb, name, op, alias, what):
        if verb == "mail":
            grants = core.mail_grants(name)
            hits = [g for g in grants if (g["account"], g["label"]) == (alias, what)]
            if not hits:
                return False, f"{name} has no grant {alias}/{what}"
            rest = [g for g in grants if g not in hits]
            if op == "revoke-draft":
                rest += [{k: v for k, v in g.items() if k != "draft"} for g in hits]
            core.set_mail_grants(name, rest)
        elif verb == "calendar":
            grants = core.calendar_grants(name)
            hits = [g for g in grants if g["account"] == alias and what in (g["calendar"], g["name"])]
            if not hits or (op == "revoke-write" and not any(g.get("write") for g in hits)):
                return False, f"{name} has no calendar {op} grant {alias}/{what}"
            rest = [g for g in grants if g not in hits]
            if op == "revoke-write":
                rest += [{k: v for k, v in g.items() if k != "write"} for g in hits]
            core.set_calendar_grants(name, rest)
        elif verb == "gdrive":
            core.set_gdrive_grants(name, [g for g in core.gdrive_grants(name) if g["account"] != alias])
        else:
            core.set_gchat_grants(name, [g for g in core.gchat_grants(name) if g["account"] != alias])
        return True, f"{name}: {verb} {op} {alias} {what}".strip()

    def forbidden_launchctl(*args):
        raise AssertionError(f"fixture must not query launchd: {args}")

    real_alive = version._alive

    def alive(pid):
        # The initial pid really lives. Later fake pids model a replacement without spawning any process.
        return real_alive(pid) or any(
            (rec := _read_json(base / "sup" / "services" / f"{name}.json")).get("fixture")
            and rec.get("pid") == pid for name in ("api", "scheduler"))

    real_access = setup_access.access

    def access(name):
        # secrets_env is empty here: the key and the bot this fixture recorded (by name only) say what is set
        state = _read_json(setup_state)
        return {**real_access(name), "web_search": "SCRAPEBADGER_API_KEY" in state.get("secrets", []),
                "telegram": name in state.get("telegram", [])}

    for fake in (run_now, run_json):
        try:
            fake(["unknown-fixture-verb"])
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
              mock.patch.object(web, "_names", side_effect=names),
              mock.patch.object(core, "TEMPLATE", template),
              # never the Mac's own Playwright cache: the version `browser install` recorded here
              mock.patch("supervisor.browser.version", side_effect=lambda: _read_json(setup_state).get("browser")),
              mock.patch.object(setup_access, "access", side_effect=access),
              # the pickers' lists (§18.7) and a writable calendar's id: this fixture's, never Google's
              mock.patch.object(mail_mod, "labels",
                                side_effect=lambda acct: {n: f"Label_{i}" for i, n in enumerate(GOOGLE_LABELS)}),
              mock.patch.object(calendar_mod, "calendars", side_effect=lambda acct: [dict(c) for c in GOOGLE_CALENDARS]),
              mock.patch.object(google_mod, "urlopen", side_effect=AssertionError("the fixture never calls Google")),
              # secrets_env is empty here: the bots are those `telegram token` recorded (never the real .env)
              mock.patch.object(telegram_mod, "tokens",
                                side_effect=lambda: {n: "fixture" for n in _read_json(setup_state).get("telegram", [])}),
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
              mock.patch.object(actions, "run_json", side_effect=run_json),
              mock.patch.object(actions, "service_guard", side_effect=lambda name, action:
                                _read_json(base / "services.json").get("busy", "") if name == "api" else ""),
              mock.patch.object(autonomy, "_launchctl", side_effect=forbidden_launchctl),
              mock.patch.object(autonomy, "service_problem",
                                side_effect=lambda name: f"the {name} service is not running in this fixture"),
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


TEMPLATE_DRIVE = "You were created with one innate drive: to improve yourself.\n"   # the fixture template's drive.md


def _flags(args: list[str]) -> dict:
    """`provider use`'s flags as the route passes them (--json dropped): {"model": …, "price_in": …, "vision": True}."""
    out: dict = {}
    rest = [a for a in args if a != "--json"]
    while rest:
        flag = rest.pop(0).removeprefix("--").replace("-", "_")
        if flag == "vision":
            out["vision"] = True
        elif rest:
            value = rest.pop(0)
            out[flag] = float(value) if flag.startswith(("price", "judge_price")) else value
    return out


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
            if not chat or chat.get("state") != "replying":   # gone (deleted, or its test's folder removed) or done
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
        "operator", False, kind="question",
        choices={"options": [{"label": "Keep them", "detail": "Show each account in the report"},
                             {"label": "Totals only", "detail": None}], "multiple": False})
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


def _rich_answer(k: int) -> str:
    """An answer the size and shape of a real one (about 2 KB: a heading, paragraphs, a list, every fourth a code block,
    every sixth a table), for `act rich`."""
    parts = [f"## Step {k}", "The ledger was read again from the start, and the totals now match the bank's own "
             f"statement for run {k}. **Two** rows had swapped dates; they are fixed in `notes/docs/ledger.md`, and the "
             "[summary](notes/docs/summary.md) says what moved and why.",
             "\n".join(f"- Item {i}: the {('first', 'second', 'third', 'fourth', 'fifth')[i]} check passed after the "
                       "rounding moved to the end of the sum, where it belongs." for i in range(5)),
             "What is left is the reserve account, which still rounds each month. That one waits for your word, since "
             "it changes the numbers you already sent on. " * 2]
    if k % 4 == 1:
        parts.append("```python\n" + "\n".join(f"total_{i} = sum(row.amount for row in rows if row.day <= day_{i})"
                                                for i in range(8)) + "\n```")
    if k % 6 == 1:
        parts.append("| Account | Before | After | Note |\n|---|---:|---:|---|\n" + "\n".join(
            f"| acct-{i} | {1000 + i * 37:,}.00 | {1000 + i * 37 + 3:,}.00 | rounded once |" for i in range(6)))
    return "\n\n".join(parts)


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
    elif what == "version":                            # an older server for the app's version gate (§19)
        if not args:
            (base / "version.json").unlink(missing_ok=True)
        elif name == "missing":
            _write_json(base / "version.json", {"missing": True})
        elif name.isdigit():
            _write_json(base / "version.json", {"api": int(name), "server": text or "v0.0.1"})
        else:
            raise SystemExit("version needs <api> [server], missing, or nothing (the real answer again)")
    elif what in ("message", "question", "choice", "choices"):
        file = f"{datetime.now():%Y%m%d-%H%M%S-%f}-from-{name}.md"
        text = text or {"question": "Should I archive the September notes?",
                        "choice": "When should I run the October reconciliation?",
                        "choices": "Which accounts go in the weekly summary?"}.get(what, "The nightly report is ready.")
        # a message from a cycle, as the broker records one: `talk` null (no conversation), so it shows in From
        row = {"ts": now, "from": name, "to": "operator", "kind": "message" if what == "message" else "question",
               "file": file, "text": text, "talk": None}
        if what in ("choice", "choices"):              # as send_message's choices records them (supervisor/questions.py)
            from supervisor import questions
            row["choices"] = questions.validate_choices(
                {"options": [{"label": "Tonight", "detail": "After the 22:00 import"}, {"label": "Tomorrow morning"},
                             {"label": "Skip October"}]} if what == "choice" else
                {"options": [{"label": "Operating"}, {"label": "Reserve", "detail": "Only if it moved"},
                             {"label": "Payroll"}, {"label": "Tax"}], "multiple": True})
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
    elif what == "rich":                               # Main grows by real-sized answers (timing the chat screen)
        start = datetime.now().replace(microsecond=0) - timedelta(minutes=400)
        turns = [{"role": "user" if k % 2 == 0 else "assistant", "ts": (start + timedelta(minutes=2 * k)).isoformat(),
                  **({"content": f"Question {k}: what changed?", "shown": f"Question {k}: what changed?",
                      "mid": f"rich-{k}"} if k % 2 == 0 else {"content": _rich_answer(k)})}
                 for k in range(int(text or 160))]
        chat = web_chat.from_chat(name, ME, create=True)     # Main, which the chat list opens first
        web_chat.save({**chat, "turns": chat["turns"] + turns})
    elif what == "google":                             # the app's local `shaft google login`, which a fixture run has not
        from supervisor import google
        address = args[0] if args else ME
        google._save_account("google", {"address": address, "client": "google", "refresh_token": "fixture",
                                        "scope": " ".join(google.SCOPES), "created": now})
        print(f"Google account google is {address} (fixture: every scope, a fake token)")
    else:
        raise SystemExit(f"unknown act {what!r}: google, message, question, state, doc, long, rich")


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

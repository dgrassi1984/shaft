"""The native apps' JSON API, /app/v1 (docs/app-api.md): who may use it (the tailnet login, the
host, a paired device's bearer — never the cookie), the feed the HTML draws (same items, same keys), the long-poll,
sending by `mid` once, unread marks and the badge, and every route. It also writes the JSON fixtures the Swift side
decodes (supervisor/tests/app_api_fixtures/, SHAFT_UPDATE_FIXTURES=1 rewrites them)."""
from __future__ import annotations

import json
from datetime import datetime
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from supervisor import apns, app_api, autonomy, commitments, core, docs, feedback, messages, panel, web
from supervisor import web_chat, web_seen, webpush, reply_links, outcomes
from supervisor.tests.test_web import HOST, ME, WebTest, snap

FIXTURES = Path(__file__).resolve().parent / "app_api_fixtures"
CID = "web-3f9a1c0d7e2b4a65"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
NEXT = "for wisp's next cycle, which this starts now"


class StubApns:
    """The APNs functions used by the API, with tokens kept in memory."""

    def __init__(self):
        self.problem, self.tokens = None, {}

    def configured(self):
        return self.problem

    def register(self, device, token, env, topic, platform):
        if not re.fullmatch(r"[0-9a-f]{16,200}", token) or env not in ("sandbox", "production"):
            raise ValueError(f"not an APNs token or env: {token!r} {env!r}")
        self.tokens[device] = {"token": token, "env": env, "topic": topic, "platform": platform}

    def unregister(self, device):
        return self.tokens.pop(device, None) is not None

    def status(self, device):
        t = self.tokens.get(device)
        return {"on": bool(t), "env": t["env"] if t else None, "reason": "" if t else "no APNs token from this device"}

    def devices(self):
        return dict(self.tokens)


def turn(role: str, ts: str, content: str = "", **kw) -> dict:
    return {"role": role, "ts": ts, "content": content, **kw}


class AppTest(WebTest):
    """A temporary SUP_DIR and instances folder (never the real ones), one instance, wisp, and an app paired through
    the API itself."""

    def setUp(self):
        super().setUp()
        self.inst = Path(tempfile.mkdtemp(prefix="shaft-app-inst-")).resolve()
        self.addCleanup(shutil.rmtree, self.inst, True)
        for d in ("shaft", ".git", "notes/docs", "inbox"):
            (self.inst / "wisp" / d).mkdir(parents=True)
        self.apns = StubApns()
        self.ran = []

        def run_now(args, timeout=60, who="top"):        # the docs CLI verb, in this process (with this test's folders)
            self.ran.append(args)
            name, action, path = args[1:4]
            out = []
            with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
                code = docs.command(name, action, path, args[5] if action == "set" else None, args[-1])
            return code == 0, out[-1] if out else ""
        for p in (mock.patch.object(web, "_names", return_value=["wisp"]),
                  mock.patch.object(web, "_next_cycle", return_value=NEXT),
                  mock.patch.object(web_chat, "SUP_DIR", self.sup), mock.patch.object(core, "SUP_DIR", self.sup),
                  mock.patch.object(core, "INSTANCES", self.inst),
                  mock.patch.object(messages, "SUP_DIR", self.sup), mock.patch.object(messages, "INSTANCES", self.inst),
                  mock.patch.object(messages, "OPERATOR_INBOX", self.sup / "operator-inbox"),
                  mock.patch.object(messages, "log", lambda *_: None),
                  mock.patch.object(feedback, "FILE", self.sup / "feedback.jsonl"),
                  mock.patch.object(docs, "instance_root", lambda n: self.inst / n), mock.patch.object(docs, "log"),
                  mock.patch("supervisor.actions.run_now", side_effect=run_now),
                  mock.patch.object(web_chat, "log"), mock.patch.object(app_api, "log"),
                  mock.patch.object(web_chat, "_start", side_effect=lambda cid: None),      # no relay: no API
                  mock.patch.object(web_chat, "_sync_copies"), mock.patch.object(web_chat, "_docs_now", return_value={}),
                  mock.patch.object(autonomy, "next_cycle", lambda n: NEXT),
                  mock.patch.object(autonomy, "event_wake", lambda *a, **k: None),
                  mock.patch.object(webpush, "notify"),
                  *(mock.patch.object(apns, name, getattr(self.apns, name))
                    for name in ("configured", "register", "unregister", "status", "devices"))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(web_chat._watched.clear)
        self.addCleanup(web_chat._away.clear)
        self.device, self.secret = self.pair_app()

    # -- requests -----------------------------------------------------------------------------------------------------
    def pair_app(self, name="iphone-app", platform="ios") -> tuple[str, str]:
        link = web.new_pairing(name)
        r = self.api("/app/v1/pair", "POST", {"link": link, "platform": platform}, bearer=False)
        self.assertEqual(r.status, 200, r.body)
        got = json.loads(r.body)
        return got["device"], got["secret"]

    def api(self, path, method="GET", data=None, bearer=True, body=None, headers=None, **kw):
        h = dict(headers or {})
        if bearer is True:
            h["authorization"] = f"Bearer {self.device}.{self.secret}"
        elif bearer:
            h["authorization"] = bearer
        if data is not None:
            body, h["content-type"] = json.dumps(data).encode(), "application/json"
        return self.req(path, method, body=body or b"", headers=h, **kw)

    def get(self, path, **kw) -> dict:
        r = self.api(path, **kw)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(r.headers["Content-Type"], "application/json; charset=utf-8")
        return json.loads(r.body)

    def send(self, mode="now", text="hi", mid="m-1", files=(), **fields):
        parts = {"text": text, "mid": mid, "mode": mode, **fields}
        body = b"".join(f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                        for k, v in parts.items() if v is not None)
        for name, data in files:
            body += (f'--B\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                     f'Content-Type: application/octet-stream\r\n\r\n').encode() + data + b"\r\n"
        return self.api("/app/v1/chat/wisp/send", "POST", body=body + b"--B--\r\n",
                        headers={"content-type": "multipart/form-data; boundary=B"})

    def error(self, r, status: int, code: str) -> dict:
        self.assertEqual(r.status, status, r.body)
        err = json.loads(r.body)["error"]
        self.assertEqual(err["code"], code, err)
        self.assertTrue(err["message"])
        return err

    # -- data -----------------------------------------------------------------------------------------------------------
    def chat(self, turns: list[dict], state: str = "idle", cid: str = CID, created="2026-11-03T13:58:40", **extra) -> dict:
        chat = {"id": cid, "instance": "wisp", "login": ME, "created": created, "state": state, "turns": turns,
                "reply": None, **extra}
        web_chat.save(chat)
        return chat

    def message(self, ts: str, file: str, text: str, frm="wisp", to="operator", read=True, **extra) -> dict:
        row = {"ts": ts, "from": frm, "to": to, "kind": extra.pop("kind", "message"), "file": file, "text": text, **extra}
        with (self.sup / "messages.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")
        if not read:
            box = self.sup / "operator-inbox" if to == "operator" else self.inst / to / "inbox"
            box.mkdir(parents=True, exist_ok=True)
            (box / file).write_text(text)
        return row

    def rating(self, fid: str, target: dict, thumb: str, reply_ts: str, reason="", sent=None) -> None:
        rows = [{"ts": "2026-11-03T14:02:00.000000", "event": "rated", "id": fid, "instance": "wisp", "login": ME,
                 "target": target, "thumb": thumb, "reason": reason, "reply_ts": reply_ts, "reply_sha": "0" * 64}]
        if sent:
            rows.append({"ts": sent, "event": "sent", "id": fid, "via": "inbox", "message": "f.md"})
        with feedback.FILE.open("a") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)

    def cycles_scenario(self) -> None:
        for p in (mock.patch.object(panel, "SUP_DIR", self.sup),
                  mock.patch.object(commitments, "INSTANCES", self.inst),
                  mock.patch.object(commitments, "SUP_DIR", self.sup),
                  mock.patch.object(autonomy, "SUP_DIR", self.sup),
                  mock.patch.object(autonomy, "next_wake", return_value="12:05"),
                  mock.patch.object(autonomy, "last_skip", return_value="")):
            p.start()
            self.addCleanup(p.stop)
        root = self.inst / "wisp"
        eps = root / "memory/episodes"
        eps.mkdir(parents=True)
        records = [
            {"kind": "live", "ts": "2026-11-03T13:58:40", "run_started": "2026-11-03T13:50:00",
             "intent": {"goal": "Ship report", "origin": "operator", "success_criterion": "Report sent",
                        "predictions": ["It passes", 7], "attention": [{"source": "report", "why": "check totals"}]},
             "finish": {"criterion_met": "yes", "what_happened": "Sent report", "delivered_via": "message",
                        "surprises": [{"expected": "ten", "observed": "eleven", "so_what": "recount"}],
                        "doubts": [{"step": 1, "what": "Check again"}], "unfinished": ["Review"],
                        "next_action": {"action": "Review totals", "revise_if": "Already checked"}},
             "tools": [{"tool": "run", "args": "SECRET", "result": "SECRET"}]},
            {"kind": "live", "ts": "2026-11-03T14:00:00", "intent": {"goal": "Review"}},
            {"kind": "sleep", "ts": "2026-11-03T15:00:00", "content": "Rested"},
        ]
        (eps / "2026-11-03.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
        (self.sup / "judgments").mkdir()
        (self.sup / "judgments/wisp.jsonl").write_text(json.dumps(
            {"episode_ts": "2026-11-03T13:58:40", "criterion_met": "partial", "judge": "test",
             "first_error": "The totals were not checked before sending."}) + "\n")
        (root / "notes/queue.json").write_text(json.dumps([
            {"id": 1, "task": "Review report", "due_at": "2099-11-04T12:00:00"},
            {"id": 2, "task": "Ask operator", "due_at": "when ready"},
            {"id": 3, "task": "Check date", "due_at": "2099-11-05"}]))

    def paired_since(self, when: str) -> None:
        ds = web.devices()
        ds[self.device]["created"] = when
        web._write("devices.json", ds)


class CyclesTest(AppTest):
    def setUp(self):
        super().setUp()
        self.cycles_scenario()

    def test_repeated_plan_requests_keep_cached_next_step_and_improvement_timestamps(self):
        (self.inst / "wisp/memory/journal.md").write_text(
            "## 2026-11-05 04:00 — Sleep\n\nHow I will improve: check")
        first = self.get("/app/v1/plan/wisp")
        second = self.get("/app/v1/plan/wisp")
        self.assertEqual(first, second)
        self.assertEqual(first["improvement_error"], "")
        self.assertEqual(first["improvement"]["at"], app_api.stamp("2026-11-05T04:00:00"))

    def test_now_since_is_time_of_day_sent_as_given_not_an_offset_timestamp(self):
        with mock.patch.object(web, "_live", return_value={"state": "in a cycle", "since": "18:06"}):
            self.assertEqual(self.get("/app/v1/plan/wisp")["now"]["since"], "18:06")

    def test_fixture_queue_rows_use_real_status_instead_of_done(self):
        from apps.shaft.DevServer import fixture_server
        base = self.sup / "fixture-queue"
        (base / "sup").mkdir(parents=True)
        root = base / "instances" / "tundra"
        (root / "notes").mkdir(parents=True)
        fixture_server._seed_cycles(base, datetime(2026, 11, 5, 12))
        rows = json.loads(core.safe_read(root, "notes/queue.json"))
        self.assertEqual([r["status"] for r in rows], ["open", "open", "open"])
        self.assertTrue(all("done" not in r for r in rows))

    def test_the_plan_cycles_and_a_cycle(self):
        plan = self.get("/app/v1/plan/wisp")
        self.assertEqual(plan["now"], {"state": "idle", "since": None, "goal": None})
        self.assertEqual(plan["check"], {"next": "12:05", "skipped": "", "paused": False})
        self.assertEqual(plan["next_step"]["action"], "Review totals")
        self.assertEqual(plan["next_step"]["at"], app_api.stamp("2026-11-03T13:58:40"))
        self.assertEqual(plan["queue"]["rows"][2]["due_at_text"], "when ready")
        self.assertIsNone(plan["queue"]["rows"][2]["due_at"])
        self.assertEqual(plan["queue"]["rows"][0]["due_at"], app_api.stamp("2099-11-04T12:00:00"))
        self.assertEqual(plan["queue"]["rows"][1]["due_at"], "2099-11-05")
        days = self.get("/app/v1/cycles/wisp")
        self.assertEqual(days["days"][0]["date"], "2026-11-03")
        self.assertIsNone(days["next"])
        rows = days["days"][0]["rows"]
        self.assertEqual([r["kind"] for r in rows], ["sleep", "cycle", "cycle"])
        self.assertEqual((rows[2]["claim"], rows[2]["verdict"]), ("yes", "partial"))
        self.assertEqual(rows[2]["id"], "2026-11-03T13:58:40")
        self.assertEqual(rows[2]["at"], app_api.stamp(rows[2]["id"]))
        detail = self.get("/app/v1/cycles/wisp/2026-11-03T13:58:40")
        self.assertNotIn("where", detail)
        self.assertEqual(detail["id"], "2026-11-03T13:58:40")
        self.assertEqual(detail["at"], app_api.stamp(detail["id"]))
        self.assertEqual(detail["planned"]["predictions"], ["It passes"])
        self.assertEqual(detail["planned"]["attention"], ["source: report; why: check totals"])
        self.assertEqual(detail["result"]["surprises"], ["expected: ten; observed: eleven; so what: recount"])
        self.assertEqual(detail["result"]["doubts"], ["step: 1; what: Check again"])
        self.assertEqual(detail["result"]["judge"]["first_error"],
                         "The totals were not checked before sending.")
        self.assertNotIn("{'", " ".join(detail["result"]["doubts"]))
        unjudged = self.get("/app/v1/cycles/wisp/2026-11-03T14:00:00")
        self.assertEqual(unjudged["result"]["surprises"], [])
        self.assertEqual(unjudged["planned"]["predictions"], [])
        self.assertIsNone(unjudged["did"]["usd"])
        self.assertEqual(len(self.get("/app/v1/cycles/wisp?filter=short")["days"][0]["rows"]), 2)
        self.assertEqual(self.get("/app/v1/cycles/wisp?before=2026-11-02")["days"], [])
        self.assertEqual(len(self.get("/app/v1/cycles/wisp?before=2026-11-03")["days"]), 1)

    def test_a_repeated_etag_is_a_304(self):
        for path in ("/app/v1/plan/wisp", "/app/v1/cycles/wisp",
                     "/app/v1/cycles/wisp/2026-11-03T13:58:40"):
            first = self.api(path)
            self.assertEqual(first.status, 200, first.body)
            again = self.api(path, headers={"if-none-match": first.headers["ETag"]})
            self.assertEqual((again.status, again.body), (304, b""))
            self.assertEqual(again.headers["ETag"], first.headers["ETag"])

    def test_the_next_cursor_is_a_plain_inclusive_date(self):
        eps = self.inst / "wisp/memory/episodes"
        for day in range(4, 11):
            ts = f"2026-11-{day:02}T12:00:00"
            (eps / f"{ts[:10]}.jsonl").write_text(json.dumps({"kind": "sleep", "ts": ts}) + "\n")
        first = self.get("/app/v1/cycles/wisp")
        self.assertEqual(first["next"], "2026-11-03")
        second = self.get("/app/v1/cycles/wisp?before=" + first["next"])
        self.assertEqual([day["date"] for day in second["days"]], ["2026-11-03"])
        self.assertIsNone(second["next"])

    def test_a_bad_filter_or_before_is_a_400(self):
        for query in ("filter=x", "before=x", "before=2026-02-30", "before=20261103", "before=2026-1-03"):
            self.error(self.api("/app/v1/cycles/wisp?" + query), 400, "bad_request")

    def test_an_unknown_cycle_or_instance_is_a_404(self):
        for path in ("plan/ghost", "cycles/ghost", "cycles/ghost/2026-11-03T13:58:40",
                     "cycles/wisp/2026-11-03T00:00:00", "cycles/wisp/2026-11-03",
                     "cycles/wisp/2026-11-03T13:58:40+01:00"):
            self.error(self.api("/app/v1/" + path), 404, "not_found")

    def test_no_payload_carries_tool_results_or_doing(self):
        with mock.patch.object(web, "overview", return_value=snap(state="in a cycle", doing="run SECRET")):
            for path in ("/app/v1/plan/wisp", "/app/v1/cycles/wisp",
                         "/app/v1/cycles/wisp/2026-11-03T13:58:40"):
                self.assertNotIn("SECRET", json.dumps(self.get(path)))

    def test_it_needs_a_paired_device(self):
        for path in ("/app/v1/plan/wisp", "/app/v1/cycles/wisp",
                     "/app/v1/cycles/wisp/2026-11-03T13:58:40"):
            self.error(self.api(path, bearer=False), 401, "unpaired")

    def test_plan_times_and_due_dates(self):
        ledger = {"c1": {"id": "c1", "promise": "Report", "due": "2099-11-04T12:00:00",
                         "status": "open"},
                  "c2": {"id": "c2", "promise": "Review", "due": "2099-11-05", "status": "open"}}
        with mock.patch.object(commitments, "ledger", return_value=ledger):
            for since in ("12:34", "2026-11-03T12:34:00"):
                with mock.patch.object(web, "overview", return_value=snap(since=since)):
                    plan = self.get("/app/v1/plan/wisp")
                self.assertEqual(plan["now"]["since"], since if since == "12:34" else app_api.stamp(since))
                self.assertEqual([c["due"] for c in plan["commitments"]],
                                 [app_api.stamp("2099-11-04T12:00:00"), "2099-11-05"])


# -- who may use it (§1.2) -----------------------------------------------------------------------------------------------
class AuthTest(AppTest):
    def test_no_bearer_is_refused_unpaired(self):
        self.error(self.api("/app/v1/instances", bearer=False), 401, "unpaired")

    def test_a_wrong_secret_is_refused(self):
        self.error(self.api("/app/v1/instances", bearer=f"Bearer {self.device}.{'x' * 43}"), 401, "unpaired")
        self.error(self.api("/app/v1/instances", bearer=f"Bearer {self.device}"), 401, "unpaired")
        self.error(self.api("/app/v1/instances", bearer=f"Basic {self.device}.{self.secret}"), 401, "unpaired")

    def test_an_unpaired_device_is_refused_at_once(self):
        self.assertEqual(web.unpair(self.device), 1)
        self.error(self.api("/app/v1/instances"), 401, "unpaired")

    def test_a_login_not_in_the_list_or_none_is_refused_first(self):
        self.error(self.api("/app/v1/instances", login="someone@example.com"), 403, "login")
        self.error(self.api("/app/v1/instances", login=None), 403, "login")
        self.error(self.api("/app/v1/pair", "POST", {"token": "x", "platform": "ios"}, bearer=False, login=None),
                   403, "login")

    def test_another_host_is_refused(self):
        self.error(self.api("/app/v1/instances", host="evil.example.com"), 403, "host")

    def test_the_api_reads_no_cookie_and_the_web_routes_take_no_bearer(self):
        cookie = self.paired()
        self.error(self.api("/app/v1/instances", bearer=False, cookie=cookie), 401, "unpaired")
        r = self.req("/overview", headers={"authorization": f"Bearer {self.device}.{self.secret}"})
        self.assertEqual(r.status, 403)
        self.assertEqual(r.headers.get("X-Shaft-Pair"), "required")

    def test_last_seen_is_refreshed_as_the_cookie_refreshes_it(self):
        ds = web.devices()
        ds[self.device]["last_seen"] = "2026-01-01T00:00:00"
        web._write("devices.json", ds)
        self.get("/app/v1/instances")
        self.assertGreater(web.devices()[self.device]["last_seen"], "2026-01-01T00:00:00")

    def test_answers_carry_the_security_headers_and_no_store(self):
        r = self.api("/app/v1/instances")
        self.assertIn("Content-Security-Policy", r.headers)
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)

    def test_unknown_routes_methods_and_instances(self):
        self.error(self.api("/app/v1/nothing"), 404, "not_found")
        self.error(self.api("/app/v1/instances", "POST", {}), 405, "method")
        self.error(self.api("/app/v1/chat/ghost"), 404, "not_found")
        self.error(self.api("/app/v1/pair"), 405, "method")

    def test_anything_that_breaks_is_said_never_an_empty_body(self):
        with mock.patch.object(web_chat, "feed", side_effect=RuntimeError("boom")):
            err = self.error(self.api("/app/v1/chat/wisp"), 500, "internal")
        self.assertEqual(err["message"], "/app/v1/chat/wisp failed: RuntimeError: boom")


class PairingTest(AppTest):
    def test_pairing_spends_the_link_once_and_records_the_platform(self):
        rec = web.devices()[self.device]
        self.assertEqual((rec["name"], rec["platform"], rec["login"]), ("iphone-app", "ios", ME))
        self.assertNotIn(self.secret, (self.sup / "web" / "devices.json").read_text())
        link = web.new_pairing("mac-app")
        token = link.split("t=")[1]
        r = self.api("/app/v1/pair", "POST", {"token": token, "platform": "macos"}, bearer=False)
        self.assertEqual(json.loads(r.body)["name"], "mac-app")
        self.error(self.api("/app/v1/pair", "POST", {"link": link, "platform": "macos"}, bearer=False), 403,
                   "pair_expired")

    def test_an_expired_link_and_bad_requests(self):
        link = web.new_pairing("phone")
        self.error(self.api("/app/v1/pair", "POST", {"link": link, "platform": "android"}, bearer=False), 400,
                   "bad_request")
        self.error(self.api("/app/v1/pair", "POST", {"platform": "ios"}, bearer=False), 400, "bad_request")
        self.error(self.api("/app/v1/pair", "POST", body=b"{nope", bearer=False), 400, "bad_request")
        with mock.patch.object(web.time, "time", return_value=web.time.time() + web.PAIR_MINUTES * 60 + 1):
            self.error(self.api("/app/v1/pair", "POST", {"link": link, "platform": "ios"}, bearer=False), 403,
                       "pair_expired")

    def test_unpair_from_the_app_keeps_shared_marks_and_drops_token_and_bearer(self):
        self.apns.register(self.device, "ab" * 16, "sandbox", "io.github.dgrassi1984.shaft", "ios")
        web._write(web_seen.STORE, {self.device: {"wisp": {"version": "x", "last": None, "at": "2026-11-03T14:00:00"}}})
        self.assertEqual(self.get("/app/v1/unpair", method="POST", data={})["said"], "This device is unpaired.")
        self.assertNotIn(self.device, self.apns.tokens)
        self.assertNotIn(self.device, web._read(web_seen.STORE))
        self.assertEqual(web._read(web_seen.STORE)["operator"]["wisp"]["from"]["at"],
                         "2026-11-03T14:00:00")
        self.error(self.api("/app/v1/instances"), 401, "unpaired")

    def test_web_status_lists_the_apps_with_their_push(self):
        import contextlib
        import io
        self.apns.register(self.device, "ab" * 16, "sandbox", "io.github.dgrassi1984.shaft", "ios")
        self.apns.problem = None
        out = io.StringIO()
        with mock.patch.object(autonomy, "service"), contextlib.redirect_stdout(out):
            web.command("status")
        self.assertIn("native apps: 1  ·  native push: ready", out.getvalue())
        self.assertRegex(out.getvalue(), rf"{self.device}  iphone-app \(ios\).*push on \(sandbox\)")
        self.apns.problem = "SHAFT_APNS_KEY_ID is not set"
        out = io.StringIO()
        with mock.patch.object(autonomy, "service"), contextlib.redirect_stdout(out):
            web.command("status")
        self.assertIn("native push: off: SHAFT_APNS_KEY_ID is not set", out.getvalue())


# -- the feed (§4) and its parity with the HTML (§14) --------------------------------------------------------------------
def html_keys(html: str) -> list[str]:
    """The data-k of every item of an HTML feed, in order: not the day separators nor the feed's own furniture."""
    return [k for k in re.findall(r'data-k="([^"]*)"', html)
            if not k.startswith("day-") and k not in ("more", "empty", "live", "act", "wait", "ans", "acts", "pend")]


def json_keys(feed: dict) -> list[str]:
    return [i["key"] for i in feed["items"]] + [q["key"] for q in feed["queued"]] + \
           [p["key"] for p in feed["pending_feedback"]]


class FeedTest(AppTest):
    def rich_chat(self, state="idle", **extra) -> dict:
        turns = [
            turn("user", "2026-11-03T13:58:40", "hi", shown="> a quote\n\nYes, look:", mid="m-0",
                 attachments=[{"name": "ci-run.png", "path": "instances/wisp/a/ci-run.png", "bytes": 72}]),
            turn("assistant", "2026-11-03T14:01:12", "Fixed in **ledger.md**.", activity=["read_file x"],
                 docs=[{"path": "ledger.md", "created": False}], answered=[{"id": "c3", "doc": "ledger.md"}]),
            turn("user", "2026-11-03T14:04:00", "again", shown="again", retried=True),
            turn("assistant", "2026-11-03T14:04:30", "", note="error: CloudError: OpenRouter HTTP 502",
                 note_kind="model", note_retry=True, retry_text="again", retried=True),
            turn("user", "2026-11-03T14:05:00", "again", shown="again"),
            turn("user", "2026-11-03T14:05:50", "x", shown="Rerun it.", mid="m-1", edits=["ledger.md"],
                 comments=[{"id": "c4", "doc": "ledger.md"}], interjected=True),
            turn("assistant", "2026-11-03T14:06:40", "", note="error: ConnectionResetError: gone",
                 note_kind="connection", note_retry=False, retry_text="Rerun it."),
        ]
        self.message("2026-11-03T14:02:30", "20261103-140230-118204-from-wisp.md", "Keep the lines?", kind="question",
                     read=False)
        self.message("2026-11-03T14:03:01", "20261103-140301-552019-from-operator.md", "March, later.", frm="operator",
                     to="wisp", via=messages.BACKGROUND, mid="m-b", read=False)
        return self.chat(turns, state, **extra)

    def html(self, query="") -> str:
        cookie = self.paired()
        r = self.req(f"/fragment/chat/wisp{query}", cookie=cookie)
        self.assertEqual(r.status, 200, r.body)
        return r.body.decode()

    def test_the_json_and_the_html_show_the_same_items_with_the_same_keys(self):
        self.rich_chat(pending_edits={"ledger.md": {"old": "a", "new": "b"}})
        feed = self.get("/app/v1/chat/wisp")
        html = self.html()
        self.assertEqual(html_keys(html), json_keys(feed))
        self.assertEqual([i["type"] for i in feed["items"]],
                         ["user", "assistant", "sent", "later", "retried", "user", "user", "assistant"])
        self.assertEqual(re.findall(r'data-k="u\d+" data-raw="[^"]*" data-mid="([^"]+)"', html),
                         [i["mid"] for i in feed["items"] if i["type"] == "user" and i["mid"]])
        self.assertIn('data-k="acts"', html)
        self.assertTrue(feed["can_end"])
        self.assertIn('data-k="pend"', html)
        self.assertEqual(feed["pending_edits"], ["ledger.md"])

    def test_parity_while_replying_with_queued_messages_and_pending_feedback(self):
        self.rating("f-" + "a" * 24, {"kind": "chat", "chat": CID, "k": 1}, "down", "2026-11-03T14:01:12")
        self.rich_chat("replying", reply={"started": "2026-11-03T14:07:02", "activity": ["one"], "answer": "so"},
                       queued=[{"role": "user", "mid": "q-1", "shown": "and then", "content": "and then",
                                "attachments": [{"name": "x.png", "path": "p", "bytes": 3}]}],
                       pending_feedback=[{"id": "f-" + "a" * 24, "note": "n"}])
        feed = self.get("/app/v1/chat/wisp")
        html = self.html()
        self.assertEqual(html_keys(html), json_keys(feed))
        self.assertEqual(json_keys(feed)[-2:], ["queue-q-1", "queue-feedback-f-" + "a" * 24])
        self.assertIn('data-k="live"', html)
        self.assertTrue(feed["can_stop"])
        self.assertEqual(feed["reply"]["activity"], ["one"])
        self.assertEqual(feed["chat"]["state"], "replying")

    def test_parity_of_an_ended_chat_read_as_a_past_one(self):
        self.rich_chat("ended", ended="2026-11-03T14:10:00")
        self.chat([turn("user", "2026-11-03T15:00:00", "new", shown="new")], cid="web-0000000000000001",
                  created="2026-11-03T15:00:00")
        feed = self.get(f"/app/v1/chat/wisp/{CID}")
        self.assertTrue(feed["archived"])
        self.assertEqual(html_keys(self.html(f"?id={CID}")), json_keys(feed))
        self.assertEqual(feed["items"][-1], {"key": "ended", "type": "ended", "ts": app_api.stamp("2026-11-03T14:10:00")})
        self.assertEqual((feed["can_end"], feed["can_clear"], feed["can_stop"]), (False, False, False))
        retry = [i for i in feed["items"] if i["type"] == "assistant"][-1]["failure"]["retry"]
        self.assertIsNone(retry)                                  # an archived chat sends nothing again
        self.error(self.api("/app/v1/chat/wisp/web-ffffffffffffffff"), 404, "not_found")

    def test_the_window_matches_the_html_and_all_shows_every_turn(self):
        turns = [turn("user" if k % 2 == 0 else "assistant", f"2026-11-03T{10 + k // 60:02d}:{k % 60:02d}:00", f"t{k}",
                      shown=f"t{k}") for k in range(90)]
        self.chat(turns)
        feed = self.get("/app/v1/chat/wisp")
        self.assertEqual((feed["earlier"], feed["items"][0]["key"]), (10, "u10"))
        html = self.html()
        self.assertIn("Show the 10 earlier messages", html)
        self.assertEqual(html_keys(html), json_keys(feed))
        whole = self.get("/app/v1/chat/wisp?all=1")
        self.assertEqual((whole["earlier"], len(whole["items"])), (0, 90))
        self.assertEqual(html_keys(self.html("?all=1")), json_keys(whole))
        self.assertEqual(whole["version"], feed["version"])      # the window changes neither revision

    def test_no_chat_shows_what_the_instance_sent_since(self):
        self.message("2026-11-03T14:02:30", "20261103-140230-1-from-wisp.md", "Unread", read=False)
        feed = self.get("/app/v1/chat/wisp")
        self.assertIsNone(feed["chat"])
        self.assertEqual([i["type"] for i in feed["items"]], ["sent"])
        self.assertEqual(html_keys(self.html()), json_keys(feed))

    def test_times_carry_the_offset_in_effect_then(self):
        with mock.patch.dict(os.environ, {"TZ": "Europe/Rome"}):
            time.tzset()
            try:
                self.assertEqual(app_api.stamp("2026-11-03T14:02:11"), "2026-11-03T14:02:11+01:00")
                self.assertEqual(app_api.stamp("2026-07-03T14:02:11.123456"), "2026-07-03T14:02:11+02:00")
                self.assertIsNone(app_api.stamp(""))
                self.assertIsNone(app_api.stamp(None))
                self.assertEqual(app_api.stamp(0.0), "1970-01-01T01:00:00+01:00")
            finally:
                time.tzset()

    def test_files_are_served_by_their_url_and_unknown_ones_are_not_found(self):
        f = self.inst / "wisp" / "a" / "ci-run.png"
        f.parent.mkdir(parents=True)
        f.write_bytes(PNG)
        self.rich_chat()
        att = self.get("/app/v1/chat/wisp")["items"][0]["attachments"][0]
        self.assertEqual(att, {"name": "ci-run.png", "bytes": 72, "image": True, "text": False,
                               "url": f"/app/v1/file/chat/{CID}/0/0"})
        r = self.api(att["url"])
        self.assertEqual((r.status, r.body, r.headers["Content-Type"]), (200, PNG, "image/png"))
        self.error(self.api(f"/app/v1/file/chat/{CID}/0/5"), 404, "not_found")
        self.error(self.api("/app/v1/file/message/nothing.md/0"), 404, "not_found")
        (self.inst / "wisp" / "b.csv").write_text("a,b\n")
        self.message("2026-11-03T14:02:30", "20261103-140230-1-from-wisp.md", "csv", read=True,
                     attachments=[{"name": "b.csv", "path": "instances/wisp/b.csv", "bytes": 4}])
        sent = next(i for i in self.get("/app/v1/chat/wisp")["items"] if i.get("file") == "20261103-140230-1-from-wisp.md")
        self.assertEqual(sent["attachments"][0]["url"], "/app/v1/file/message/20261103-140230-1-from-wisp.md/0")
        r = self.api(sent["attachments"][0]["url"])
        self.assertEqual((r.status, r.body), (200, b"a,b\n"))

    def test_earlier_chats_newest_first(self):
        self.chat([turn("user", "2026-11-02T21:40:03", "x", shown="What did October look like?\nmore")],
                  state="ended", cid="web-77ab02c4e9d15f30", created="2026-11-02T21:40:03", ended="2026-11-02T22:12:44")
        self.chat([turn("user", "2026-11-03T13:58:40", "now", shown="now")])
        got = self.get("/app/v1/chats/wisp")["chats"]
        self.assertEqual(got, [{"id": "web-77ab02c4e9d15f30", "created": got[0]["created"], "ended": got[0]["ended"],
                                "excerpt": "What did October look like?", "messages": 1}])


class LongPollTest(AppTest):
    def test_held_poll_without_at_does_not_undo_away(self):
        """A held poll without at re-marked watching after away and suppressed native push."""
        self.chat([turn("assistant", "2026-11-03T14:00:00", "hello")])
        first = self.get("/app/v1/chat/wisp")
        for at in (None, 100):
            with self.subTest(at=at):
                clock, observed = [0.0], []
                def wait(seconds):
                    if not observed:
                        observed.append(web_chat.watchers("wisp"))
                        self.assertEqual(self.api("/app/v1/chat/wisp/away", "POST", {"at": 200}).status, 200)
                    clock[0] += seconds
                web_chat._away.clear()
                with mock.patch.object(app_api.time, "monotonic", side_effect=lambda: clock[0]), \
                        mock.patch.object(web_chat, "wait_change", side_effect=wait):
                    q = {} if at is None else {"at": at}
                    status, body, _ = self.feed(since=first["version"], live=first["live"], wait=4, **q)
                    self.assertEqual((status, body), (304, None))
                    self.assertEqual(observed, [{self.device}])
                    self.assertEqual(web_chat.watchers("wisp"), set())

    def feed(self, **q) -> tuple[int, dict | None, float]:
        start = time.monotonic()
        r = self.api("/app/v1/chat/wisp?" + urllib.parse.urlencode(q))
        return r.status, (json.loads(r.body) if r.body else None), time.monotonic() - start

    def test_nothing_new_answers_304_after_wait(self):
        self.chat([turn("user", "2026-11-03T13:58:40", "hi", shown="hi")])
        first = self.get("/app/v1/chat/wisp")
        status, body, took = self.feed(since=first["version"], live=first["live"], wait=1)
        self.assertEqual((status, body), (304, None))
        self.assertGreaterEqual(took, 0.9)
        self.assertLess(took, 3)

    def test_a_new_turn_answers_at_once_with_the_whole_feed(self):
        chat = self.chat([turn("user", "2026-11-03T13:58:40", "hi", shown="hi")])
        first = self.get("/app/v1/chat/wisp")

        def later():
            time.sleep(0.3)
            chat["turns"].append(turn("assistant", "2026-11-03T13:59:00", "hello"))
            web_chat.save(chat)
        threading.Thread(target=later, daemon=True).start()
        status, body, took = self.feed(since=first["version"], live=first["live"], wait=10)
        self.assertEqual(status, 200)
        self.assertFalse(body["partial"])
        self.assertNotEqual(body["version"], first["version"])
        self.assertEqual(body["items"][-1]["content"], "hello")
        self.assertLess(took, 3)

    def test_only_the_reply_moving_answers_partial(self):
        chat = self.chat([turn("user", "2026-11-03T13:58:40", "hi", shown="hi")], state="replying",
                         reply={"started": "2026-11-03T13:58:41", "activity": ["one"], "answer": ""})
        first = self.get("/app/v1/chat/wisp")
        self.assertTrue(first["live"])

        def later():
            time.sleep(0.3)
            chat["reply"].update(activity=["one", "two"], answer="so far")
            web_chat.save(chat)
        threading.Thread(target=later, daemon=True).start()
        status, body, _ = self.feed(since=first["version"], live=first["live"], wait=10)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"instance": "wisp", "partial": True, "version": first["version"],
                                "live": body["live"], "reply": {"started": body["reply"]["started"],
                                                                "activity": ["one", "two"], "answer": "so far"}})
        self.assertNotEqual(body["live"], first["live"])

    def test_since_without_live_and_a_wait_out_of_range_are_bad_requests(self):
        self.error(self.api("/app/v1/chat/wisp?since=abc"), 400, "bad_request")
        self.error(self.api("/app/v1/chat/wisp?wait=26"), 400, "bad_request")
        self.error(self.api("/app/v1/chat/wisp?wait=x"), 400, "bad_request")

    def test_a_held_poll_marks_the_device_watching_and_watch_0_does_not(self):
        self.chat([turn("user", "2026-11-03T13:58:40", "hi", shown="hi")])
        self.get("/app/v1/chat/wisp")
        self.assertIn(self.device, web_chat.watchers("wisp"))
        self.get("/app/v1/chat/wisp?watch=0")
        self.assertNotIn(self.device, web_chat.watchers("wisp"))
        self.get(f"/app/v1/chat/wisp/{CID}")                    # an explicit open thread is on screen too
        self.assertIn(self.device, web_chat.watchers("wisp"))
        self.get("/app/v1/chat/wisp?at=2000")
        self.assertEqual(self.get("/app/v1/chat/wisp/away", method="POST", data={"at": 3000}), {"ok": True})
        self.assertNotIn(self.device, web_chat.watchers("wisp"))
        self.get("/app/v1/chat/wisp?at=2500")                  # sent before it went away: not watching again
        self.assertNotIn(self.device, web_chat.watchers("wisp"))


# -- sending (§6) and the chat's actions (§7) ----------------------------------------------------------------------------
class SendTest(AppTest):
    def test_reply_identity_is_stored_once_and_counts_without_a_quote(self):
        ts = datetime.now().isoformat(timespec="seconds")
        self.message(ts, "stable.md", "Keep this?", read=True)
        before = (self.sup / "messages.jsonl").read_bytes()
        r = self.send(text="Yes", in_reply_to="stable.md")
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(reply_links.records()[0]["in_reply_to"], "stable.md")
        self.assertEqual((self.sup / "messages.jsonl").read_bytes(), before)
        self.assertEqual(outcomes.records("wisp")[0]["outcome"], "followed_up")
        r = self.send(text="Yes", in_reply_to="stable.md")
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(len(reply_links.records()), 1)

    def test_same_mid_over_app_and_web_routes_records_one_reply_link(self):
        self.message(datetime.now().isoformat(), "stable.md", "Keep this?")
        response = self.send(mid="cross-route", text="Yes", in_reply_to="stable.md")
        self.assertEqual(response.status, 200, response.body)
        chat_id = json.loads(response.body)["chat"]
        fields = {"text": "Yes", "mid": "cross-route", "in_reply_to": "stable.md"}
        body = b"".join(f'--B\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
                        for key, value in fields.items()) + b"--B--\r\n"
        cookie = self.paired()
        response = self.req("/chat/wisp/send", "POST", body=body,
                            headers={"content-type": "multipart/form-data; boundary=B",
                                     "sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(cookie)}, cookie=cookie)
        self.assertEqual(response.status, 200, response.body)
        rows = reply_links.records()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["instance"], rows[0]["key"], rows[0]["in_reply_to"]),
                         ("wisp", "send:wisp:cross-route", "stable.md"))
        self.assertEqual(len(web_chat.load(chat_id)["turns"]), 1)

    def test_absent_or_invalid_reply_identity_does_not_change_the_turn(self):
        self.message(datetime.now().isoformat(), "other.md", "Hi", frm="other")
        for target in (None, "", "unknown.md", "../stable.md", "other.md", "x" * 256, "bad\nname"):
            with self.subTest(target=target), mock.patch.object(core, "log") as log:
                self.chat([])
                r = self.send(mid="same", text="Yes", in_reply_to=target)
                self.assertEqual(r.status, 200, r.body)
                self.assertFalse((self.sup / "reply-links.jsonl").exists())
                self.assertEqual(log.call_count, int(target is not None))
                chat = web_chat.load(json.loads(r.body)["chat"])
                self.assertEqual(chat["turns"][0]["shown"], "Yes")
                self.assertNotIn("in_reply_to", chat["turns"][0])

    def test_reply_identity_all_modes_and_refused_send(self):
        self.message(datetime.now().isoformat(), "stable.md", "Keep this?")
        r = self.send(text="", in_reply_to="stable.md")
        self.assertEqual(r.status, 409, r.body)
        self.assertEqual(reply_links.records(), [])
        for mode in ("now", "queue", "background"):
            r = self.send(mode=mode, mid=mode, text="Yes", in_reply_to="stable.md")
            self.assertEqual(r.status, 200, r.body)
        self.assertEqual(len(reply_links.records()), 3)
        with mock.patch.object(core, "log") as log:
            for target in (42, [], {}, "bad\x7fname"):
                reply_links.record("wisp", target, "malformed")
        self.assertEqual(log.call_count, 4)
        self.assertEqual(len(reply_links.records()), 3)

    def test_web_reply_button_and_send_use_raw_identity(self):
        from supervisor import web_views
        row = self.message(datetime.now().isoformat(), "raw identity.md", "Hi")
        item = web_chat.message_item("wisp", row, {})
        self.assertIsInstance(item["read"], bool)
        self.assertEqual(item["kind"], "message")
        self.assertEqual(item["file"], "raw identity.md")
        html = str(web_views._message_item("wisp", item, False))
        self.assertIn('data-reply-message="raw identity.md"', html)
        body = b'--B\r\nContent-Disposition: form-data; name="text"\r\n\r\nYes\r\n'
        body += b'--B\r\nContent-Disposition: form-data; name="in_reply_to"\r\n\r\nraw identity.md\r\n--B--\r\n'
        # Exercise the authenticated HTML route with the same fields as its composer.
        cookie = self.paired()
        response = self.req("/chat/wisp/send", "POST", body=body,
                            headers={"content-type": "multipart/form-data; boundary=B",
                                     "sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(cookie)}, cookie=cookie)
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(reply_links.records()[0]["in_reply_to"], "raw identity.md")

    def test_reply_identity_optional_store_failure_does_not_refuse(self):
        self.message(datetime.now().isoformat(), "stable.md", "Keep this?")
        original = Path.open
        def opening(path, *args, **kwargs):
            if path.name == "reply-links.jsonl":
                raise PermissionError("test denial")
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "open", opening), mock.patch.object(core, "log") as log:
            r = self.send(text="Yes", in_reply_to="stable.md")
        self.assertEqual(r.status, 200, r.body)
        log.assert_called_once_with("reply link: could not store optional evidence")

    def test_the_same_mid_is_sent_once_whatever_the_mode(self):
        r = self.send(text="hello", mid="m-1")
        got = json.loads(r.body)
        self.assertEqual((r.status, got["said"], got["file"]), (200, "sent to wisp", None))
        chat = web_chat.load(got["chat"])
        for mode in ("now", "queue", "background"):
            again = json.loads(self.send(mode, text="hello", mid="m-1").body)
            self.assertIn("already sent to wisp", again["said"])
        self.assertEqual([t["shown"] for t in web_chat.load(chat["id"])["turns"]], ["hello"])
        self.assertEqual(list((self.inst / "wisp" / "inbox").iterdir()), [])
        self.assertIn("m-1", self.get("/app/v1/chat/wisp")["mids"])

    def test_next_cycle_goes_to_its_inbox(self):
        got = json.loads(self.send("background", text="later please", mid="m-2").body)
        self.assertEqual((got["said"], got["chat"]), (NEXT, None))
        self.assertTrue((self.inst / "wisp" / "inbox" / got["file"]).is_file())
        self.assertFalse([i for i in self.get("/app/v1/chat/wisp")["items"] if i["type"] == "later"])
        later = [i for i in web_chat.from_feed("wisp")["items"] if i["type"] == "later"]
        self.assertEqual([(i["file"], i["mid"], i["text"]) for i in later], [(got["file"], "m-2", "later please")])

    def test_quotes_become_the_text_as_the_web_composer_writes_it(self):
        q = app_api.quote_text
        self.assertEqual(q("Rerun it", []), "Rerun it")
        self.assertEqual(q(" and also ", [{"quote": " one\r\ntwo  \n", "answer": "", "doc": ""}]),
                         "> one\n> two\n\nand also")
        self.assertEqual(q("first", [{"quote": "a\n\nb", "answer": " yes ", "doc": "plan.md"},
                                     {"quote": "c", "answer": "", "doc": ""}]),
                         "first\n\n> (from notes/docs/plan.md)\n> a\n>\n> b\n\nyes\n\n> c")
        self.assertEqual(q("", [{"quote": "only", "answer": "", "doc": ""}]), "> only")
        r = self.send(text="Rerun it", mid="m-3",
                      quotes=json.dumps([{"quote": "Keep the lines?", "answer": "Yes", "doc": ""}]))
        self.assertEqual(r.status, 200, r.body)
        chat = web_chat.load(json.loads(r.body)["chat"])
        self.assertEqual(chat["turns"][-1]["shown"], "Rerun it\n\n> Keep the lines?\n\nYes")

    def test_bad_sends_are_refused(self):
        self.error(self.send(mid=""), 400, "bad_request")
        self.error(self.send(mid="no spaces"), 400, "bad_request")
        self.error(self.send(mode="later"), 400, "bad_request")
        self.error(self.send(quotes="{}"), 400, "bad_request")
        self.error(self.send(quotes='[{"answer": "x"}]'), 400, "bad_request")
        self.error(self.send("queue", retry_of="3"), 400, "bad_request")
        self.error(self.send(text="  ", mid="m-9"), 409, "refused")
        r = self.api("/app/v1/chat/wisp/send", "POST", {"text": "json"})
        self.error(r, 400, "bad_request")

    def test_retry_of_folds_the_failed_try(self):
        self.chat([turn("user", "2026-11-03T13:58:40", "ask", shown="ask"),
                   turn("assistant", "2026-11-03T13:59:00", "", note="error: CloudError: OpenRouter HTTP 502",
                        note_kind="model", note_retry=True, retry_text="ask")])
        failure = self.get("/app/v1/chat/wisp")["items"][-1]["failure"]
        self.assertEqual(failure["retry"], {"text": "ask", "retry_of": 1, "confirm": None})
        r = self.send(text=failure["retry"]["text"], mid="m-4", retry_of=str(failure["retry"]["retry_of"]))
        self.assertEqual(r.status, 200, r.body)
        types = [i["type"] for i in self.get("/app/v1/chat/wisp")["items"]]
        self.assertEqual(types, ["retried", "user"])

    def test_web_thread_actions_and_composer_keep_the_named_thread(self):
        cookie = self.paired()
        headers = {"sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(cookie)}
        response = self.req("/chat/wisp/new", "POST", body=b"title=Ledger", headers=headers, cookie=cookie)
        self.assertEqual(response.status, 200, response.body)
        a = web_chat.current("wisp")["id"]
        self.assertIn(f'data-cid="{a}"'.encode(), response.body)
        self.assertNotIn("HX-Redirect", response.headers)
        b = web_chat.new_thread("wisp", "operator")
        b["created"] = "2099-01-01T00:00:00"
        web_chat.save(b)
        self.assertEqual(web_chat.current("wisp")["id"], b["id"])
        page = self.req(f"/i/wisp/chat?id={a}", cookie=cookie).body.decode()
        composer = page.split('<form class="composer"', 1)[1].split("</form>", 1)[0]
        self.assertIn(f'name="cid" value="{a}"', composer)
        response = self.req("/chat/wisp/rename", "POST", body=f"cid={a}&title=Mail".encode(),
                            headers=headers, cookie=cookie)
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(web_chat.title(web_chat.load(a)), "Mail")
        response = self.req("/chat/wisp/rename", "POST", body=f"cid={a}&title=".encode(),
                            headers=headers, cookie=cookie)
        self.assertEqual(response.status, 409)
        response = self.req("/chat/missing/new", "POST", headers=headers, cookie=cookie)
        self.assertEqual(response.status, 404)

    def test_new_and_rename_threads(self):
        a = self.get("/app/v1/chat/wisp/new", method="POST", data={})
        b = self.get("/app/v1/chat/wisp/new", method="POST", data={"title": "  October\nclose  "})
        self.assertEqual(a["chat"]["title"], "New thread")
        self.assertEqual(b["chat"]["title"], "October close")
        self.assertTrue(a["said"])
        self.assertEqual({c["id"] for c in web_chat.threads("wisp")}, {a["chat"]["id"], b["chat"]["id"]})
        cid = a["chat"]["id"]
        got = self.get("/app/v1/chat/wisp/rename", method="POST", data={"cid": cid, "title": " Ledger "})
        self.assertEqual(got, {"said": "thread renamed"})
        self.assertEqual(self.get(f"/app/v1/chat/wisp/{cid}")["chat"]["title"], "Ledger")
        self.error(self.api("/app/v1/chat/wisp/rename", "POST", {"cid": cid, "title": " "}), 409, "refused")
        self.error(self.api("/app/v1/chat/wisp/rename", "POST", {"cid": "web-missing", "title": "x"}),
                   404, "not_found")
        self.error(self.api("/app/v1/chat/missing/new", "POST", {}), 404, "not_found")
        self.error(self.api("/app/v1/chat/wisp/new", "POST", {"title": 3}), 400, "bad_request")
        chat = web_chat.load(cid)
        chat["state"] = "ended"
        web_chat.save(chat)
        self.error(self.api("/app/v1/chat/wisp/rename", "POST", {"cid": cid, "title": "x"}), 409, "refused")

    def test_send_queue_and_unqueue_target_the_named_thread(self):
        a = self.get("/app/v1/chat/wisp/new", method="POST", data={})["chat"]["id"]
        b = self.get("/app/v1/chat/wisp/new", method="POST", data={})["chat"]["id"]
        response = self.send(text="alpha", cid=a)
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(json.loads(response.body)["chat"], a)
        self.assertEqual(web_chat.load(a)["turns"][0]["shown"], "alpha")
        self.assertEqual(web_chat.load(b)["turns"], [])
        response = self.send(mode="queue", text="after", mid="queued", cid=a)
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual([t["mid"] for t in web_chat.load(a)["queued"]], ["queued"])
        self.get("/app/v1/chat/wisp/unqueue", method="POST", data={"cid": a, "mid": "queued"})
        self.assertEqual(web_chat.load(a)["queued"], [])
        self.error(self.send(cid="web-missing", mid="missing"), 409, "refused")

    def test_the_actions(self):
        self.chat([turn("user", "2026-11-03T13:58:40", "hi", shown="hi"),
                   turn("assistant", "2026-11-03T13:59:00", "hello")], state="replying",
                  reply={"started": "2026-11-03T13:59:01", "activity": [], "answer": ""},
                  queued=[{"role": "user", "mid": "q-1", "shown": "x", "content": "x", "attachments": []}])
        self.assertEqual(self.get("/app/v1/chat/wisp/unqueue", method="POST", data={"mid": "q-1"})["said"],
                         "queued message cancelled")
        self.assertEqual(web_chat.load(CID)["queued"], [])
        self.error(self.api("/app/v1/chat/wisp/stop", "POST", {"cid": CID}), 409, "refused")       # no relay runs
        self.error(self.api("/app/v1/chat/wisp/end", "POST", {"cid": CID}), 409, "refused")        # still replying
        self.error(self.api("/app/v1/chat/wisp/clear", "POST", {"cid": CID}), 409, "refused")
        self.error(self.api("/app/v1/chat/wisp/end", "POST", {"cid": "web-ffffffffffffffff"}), 404, "not_found")
        self.error(self.api("/app/v1/chat/wisp/stop", "POST", {}), 400, "bad_request")
        self.error(self.api("/app/v1/chat/wisp/stop", "POST", body=b"[1]"), 400, "bad_request")

    def test_a_send_too_large_is_refused_in_json_before_it_is_read(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        heads = {}
        for path, size in (("/app/v1/chat/wisp/send", messages.ATTACH_MAX_TOTAL + 2_000_000),
                           ("/app/v1/chat/wisp/seen", 2_000_000)):
            with socket.create_connection(server.server_address, timeout=5) as s:
                s.sendall(f"POST {path} HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: {size}\r\n\r\n".encode())
                raw = b""
                while chunk := s.recv(65536):
                    raw += chunk
            head, _, body = raw.partition(b"\r\n\r\n")
            self.assertIn(b" 413 ", head.split(b"\r\n")[0])
            self.assertIn(b"Content-Type: application/json; charset=utf-8", head)
            heads[path] = json.loads(body)["error"]
        self.assertEqual(heads["/app/v1/chat/wisp/send"],
                         {"code": "too_large", "message": "Too large: at most 53 MB can be sent at once."})
        self.assertEqual(heads["/app/v1/chat/wisp/seen"]["message"], "Too large: at most 1 MB can be sent at once.")


# -- unread marks and the badge (§12) ------------------------------------------------------------------------------------
class SeenTest(AppTest):
    def test_same_second_instance_items_make_seen_chat_unread(self):
        """Unread was lost when an assistant turn or operator-directed message shared the seen last stamp."""
        for kind in ("assistant", "message", "message"):
            feed = self.get("/app/v1/chat/wisp")
            self.assertEqual(self.get("/app/v1/chat/wisp/seen", method="POST", data={"version": feed["version"]}),
                             {"unread": False, "badge": 0})
            if kind == "assistant":
                self.c["turns"].append(turn("assistant", "2026-11-03T14:00:00", "another answer"))
                web_chat.save(self.c)
            else:
                self.message("2026-11-03T14:00:00", f"same-second-{len(messages.history())}.md", "another message")
            self.assertEqual(self.rail(), (True, 1))
            self.assertEqual(web_seen.badge(self.device), 1)
            self.assertEqual(web_chat.feed("wisp")["last"], web_chat.summary("wisp", messages.history())["last"])
        # Old seen.json entries still use their stamp; a later second remains unread.
        web._write(web_seen.STORE, {self.device: {"wisp": {"last": "2026-11-03T14:00:00"}}})
        self.assertEqual(self.rail(), (False, 0))
        self.message("2026-11-03T14:00:01", "later.md", "later")
        self.assertEqual(self.rail(), (True, 1))

    def setUp(self):
        super().setUp()
        self.paired_since("2026-11-03T12:00:00")
        self.c = self.chat([turn("user", "2026-11-03T13:58:40", "hi", shown="hi"),
                            turn("assistant", "2026-11-03T14:00:00", "hello")])

    def rail(self) -> tuple[bool, int]:
        got = self.get("/app/v1/instances")
        return got["instances"][0]["unread"], got["badge"]

    def test_unread_follows_the_instances_own_last_and_seen_clears_it(self):
        self.assertEqual(self.rail(), (True, 1))
        self.assertEqual(web_seen.badge(self.device), 1)
        feed = self.get("/app/v1/chat/wisp")
        self.assertEqual(self.get("/app/v1/chat/wisp/seen", method="POST", data={"version": feed["version"]}),
                         {"unread": False, "badge": 0})
        self.assertEqual(self.rail(), (False, 0))
        self.c["turns"].append(turn("user", "2026-11-03T14:05:00", "thanks", shown="thanks"))     # the operator's own
        web_chat.save(self.c)
        self.assertEqual(self.rail(), (False, 0))
        self.message("2026-11-03T14:06:00", "20261103-140600-1-from-wisp.md", "One more thing")     # the instance's
        self.assertEqual(self.rail(), (True, 1))

    def test_a_stale_version_is_refused_with_the_current_one(self):
        feed = self.get("/app/v1/chat/wisp")
        self.c["turns"].append(turn("assistant", "2026-11-03T14:01:00", "and more"))
        web_chat.save(self.c)
        err = self.error(self.api("/app/v1/chat/wisp/seen", "POST", {"version": feed["version"]}), 409,
                         "stale_version")
        self.assertEqual(err["version"], self.get("/app/v1/chat/wisp")["version"])
        self.assertEqual(self.rail(), (True, 1))

    def test_what_came_before_pairing_is_not_unread(self):
        self.paired_since("2026-11-03T15:00:00")
        self.assertEqual(self.rail(), (False, 0))

    def test_the_rail_and_the_feed_take_last_from_one_helper(self):
        self.message("2026-11-03T14:03:00", "20261103-140300-1-from-wisp.md", "a message after the answer")
        self.message("2026-11-03T14:04:00", "20261103-140400-1-from-operator.md", "mine, for its next cycle",
                     frm="operator", to="wisp", via=messages.BACKGROUND)
        last = web_chat.summary("wisp", messages.history())["last"]
        self.assertEqual(last, "2026-11-03T14:03:00")
        self.assertEqual(web_chat.feed("wisp")["last"], last)
        self.assertEqual(self.get("/app/v1/instances")["instances"][0]["last"], app_api.stamp(last))


# -- feedback (§9) ---------------------------------------------------------------------------------------------------------
class FeedbackTest(AppTest):
    def test_rate_send_and_clear(self):
        self.chat([turn("user", "2026-11-03T13:58:40", "hi", shown="hi"),
                   turn("assistant", "2026-11-03T14:00:00", "hello")])
        got = self.get("/app/v1/feedback/wisp", method="POST",
                       data={"op": "rate", "kind": "chat", "cid": CID, "k": 1, "thumb": "down", "reason": "wrong"})
        fid = got["id"]
        rating = self.get("/app/v1/chat/wisp")["items"][-1]["rating"]
        self.assertEqual(rating, {"id": fid, "thumb": "down", "reason": "wrong", "sent": None, "pending": False})
        got = self.get("/app/v1/feedback/wisp", method="POST", data={"op": "send", "id": fid})
        self.assertEqual(got, {"said": "Feedback goes with your next message.", "id": fid})
        feed = self.get("/app/v1/chat/wisp")
        self.assertTrue(feed["items"][-1]["rating"]["pending"])
        self.assertEqual([p["id"] for p in feed["pending_feedback"]], [fid])
        self.assertEqual(self.get("/app/v1/feedback/wisp", method="POST", data={"op": "clear", "id": fid}),
                         {"said": "Feedback cleared.", "id": None})
        self.error(self.api("/app/v1/feedback/wisp", "POST", {"op": "rate", "kind": "chat", "cid": CID, "k": 0,
                                                              "thumb": "up"}), 409, "refused")
        self.error(self.api("/app/v1/feedback/wisp", "POST", {"op": "like"}), 400, "bad_request")


# -- documents and comments (§10) ----------------------------------------------------------------------------------------
class DocsTest(AppTest):
    def doc(self, path: str, text: str, mtime: float = 1793710800.0) -> str:
        p = self.inst / "wisp" / "notes" / "docs" / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        os.utime(p, (mtime, mtime))
        return docs.sha(text.encode())

    def test_list_read_and_download(self):
        sha = self.doc("ledger.md", "# Ledger\n")
        self.doc("big.md", "x" * (docs.MAX_BYTES + 1))
        self.chat([turn("assistant", "2026-11-03T14:01:12", "made", docs=[{"path": "ledger.md", "created": True}])])
        listing = self.get("/app/v1/docs/wisp")
        self.assertEqual((listing["dir"], listing["max_bytes"]), ("notes/docs", 262144))
        rows = {d["path"]: d for d in listing["docs"]}
        self.assertEqual((rows["ledger.md"]["sha"], rows["ledger.md"]["in_chat"], rows["ledger.md"]["too_big"]),
                         (sha, True, False))
        self.assertEqual((rows["big.md"]["sha"], rows["big.md"]["too_big"]), ("too-big", True))
        doc = self.get("/app/v1/docs/wisp/doc?path=ledger.md")
        self.assertEqual((doc["text"], doc["sha"], doc["threads"], doc["overwritten"]), ("# Ledger\n", sha, [], None))
        self.error(self.api("/app/v1/docs/wisp/doc?path=big.md"), 409, "too_big")
        self.error(self.api("/app/v1/docs/wisp/doc?path=gone.md"), 404, "not_found")
        self.error(self.api("/app/v1/docs/wisp/doc?path=../x.md"), 409, "refused")
        self.error(self.api("/app/v1/docs/wisp/doc"), 400, "bad_request")
        r = self.api("/app/v1/docs/wisp/raw?path=big.md")
        self.assertEqual((r.status, len(r.body), r.headers["Content-Type"]), (200, docs.MAX_BYTES + 1,
                                                                             "text/markdown; charset=utf-8"))
        self.assertIn("attachment; filename*=UTF-8''big.md", r.headers["Content-Disposition"])

    def test_save_create_and_a_stale_save(self):
        got = self.get("/app/v1/docs/wisp/save", method="POST", data={"path": "plan", "text": "one\n", "sha": "none"})
        self.assertEqual((got["path"], got["sha"]), ("plan.md", docs.sha(b"one\n")))
        self.assertIn("wisp gets the change with your next message", got["said"])
        err = self.error(self.api("/app/v1/docs/wisp/save", "POST", {"path": "plan.md", "text": "two\n", "sha": "old"}),
                         409, "stale")
        self.assertEqual(err["sha"], docs.sha(b"one\n"))
        self.assertIn("+one", err["diff"])
        err = self.error(self.api("/app/v1/docs/wisp/save", "POST", {"path": "plan.md", "text": "x", "sha": "none"}),
                         409, "stale")
        self.assertEqual((err["sha"], err["diff"]), (None, None))
        self.error(self.api("/app/v1/docs/wisp/save", "POST", {"path": "../x", "text": "x", "sha": "none"}), 409,
                   "refused")
        self.assertEqual(self.get("/app/v1/docs/wisp/save", method="POST",
                                  data={"path": "plan.md", "text": "two\n", "sha": err["sha"] or got["sha"]})["sha"],
                         docs.sha(b"two\n"))

    def test_delete(self):
        sha = self.doc("plan.md", "one")
        err = self.error(self.api("/app/v1/docs/wisp/delete", "POST", {"path": "plan.md", "sha": "old"}), 409, "stale")
        self.assertEqual((err["sha"], err["diff"]), (sha, None))
        self.assertIn("deleted", self.get("/app/v1/docs/wisp/delete", method="POST",
                                          data={"path": "plan.md", "sha": sha})["said"])
        self.error(self.api("/app/v1/docs/wisp/delete", "POST", {"path": "plan.md", "sha": sha}), 409, "refused")

    def test_comments_threads_and_their_state(self):
        sha = self.doc("plan.md", "Net totals are rounded once.\n")
        anchor = {"exact": "rounded once", "prefix": "Net totals are ", "suffix": ".", "start": 15}
        got = self.get("/app/v1/docs/wisp/comment", method="POST",
                       data={"pid": "p-0123456789abcdef", "doc": "plan.md", "anchor": anchor, "sha": sha,
                             "text": "Per account?", "send": ""})
        self.assertEqual(got["thread"], "c1")
        self.assertIn("kept, not sent", got["said"])
        again = self.get("/app/v1/docs/wisp/comment", method="POST",
                         data={"pid": "p-0123456789abcdef", "doc": "plan.md", "anchor": anchor, "sha": sha,
                               "text": "Per account?", "send": ""})
        self.assertEqual(again["thread"], "c1")                  # the same pid again: the thread that holds it
        self.get("/app/v1/docs/wisp/comment", method="POST",
                 data={"pid": "p-1111111111111111", "thread": "c1", "text": "Or overall?", "send": ""})
        self.get("/app/v1/docs/wisp/comment/edit", method="POST", data={"pid": "p-1111111111111111", "text": "Overall?"})
        self.get("/app/v1/docs/wisp/comment/state", method="POST", data={"thread": "c1", "state": "resolved"})
        thread = self.get("/app/v1/docs/wisp/doc?path=plan.md")["threads"][0]
        self.assertEqual((thread["state"], thread["anchor"], thread["doc_changed"]), ("resolved", anchor, False))
        self.assertEqual([(p["text"], p["unsent"]) for p in thread["posts"]], [("Per account?", True), ("Overall?", True)])
        self.get("/app/v1/docs/wisp/comment/delete", method="POST", data={"pid": "p-1111111111111111"})
        self.assertEqual(len(self.get("/app/v1/docs/wisp/doc?path=plan.md")["threads"][0]["posts"]), 1)
        self.error(self.api("/app/v1/docs/wisp/comment", "POST", {"pid": "p-2222222222222222", "doc": "plan.md",
                                                                  "anchor": "text", "sha": sha, "text": "x"}),
                   400, "bad_request")
        self.error(self.api("/app/v1/docs/wisp/comment/send", "POST", {"pid": "p-0123456789abcdef", "when": "soon"}),
                   400, "bad_request")
        self.error(self.api("/app/v1/docs/wisp/comment", "POST", {"pid": "bad", "doc": "plan.md", "anchor": None,
                                                                  "sha": sha, "text": "x"}), 409, "refused")


# -- push (§11) ------------------------------------------------------------------------------------------------------------
class PushTest(AppTest):
    def test_register_status_and_unregister(self):
        data = {"token": "ab" * 32, "env": "sandbox", "topic": "io.github.dgrassi1984.shaft", "platform": "ios"}
        self.assertEqual(self.get("/app/v1/push/register", method="POST", data=data),
                         {"push": {"on": True, "env": "sandbox", "reason": ""}})
        self.assertEqual(self.get("/app/v1/push/status"), {"on": True, "env": "sandbox", "reason": "", "problem": None})
        self.error(self.api("/app/v1/push/register", "POST", {**data, "token": "not hex"}), 400, "bad_request")
        self.assertEqual(self.get("/app/v1/push/register", method="DELETE"), {"removed": True})
        self.assertEqual(self.get("/app/v1/push/register", method="DELETE"), {"removed": False})
        self.apns.problem = "SHAFT_APNS_KEY_ID is not set (key: .supervisor/web/apns/AuthKey_<id>.p8)"
        err = self.error(self.api("/app/v1/push/register", "POST", data), 503, "push_off")
        self.assertEqual(err["message"], self.apns.problem)
        self.assertEqual(self.get("/app/v1/push/status")["problem"], self.apns.problem)

    def test_latest_takes_this_devices_events_with_the_chat_to_open(self):
        webpush._save_queue({self.device: {"pending": [
            {"title": "wisp replied", "body": "Done.", "url": "/i/wisp/chat", "tag": "wisp", "at": time.time()}]}})
        got = self.get("/app/v1/push/latest")
        self.assertEqual(got["events"], [{"title": "wisp replied", "body": "Done.", "url": "/i/wisp/chat", "tag": "wisp",
                                          "instance": "wisp", "cid": None}])
        self.assertEqual(got["badge"], 0)
        self.assertEqual(self.get("/app/v1/push/latest")["events"][0]["title"], "wisp replied")   # shown again
        self.assertEqual(self.get("/app/v1/push/latest", method="GET")["events"][0]["instance"], "wisp")
        webpush._save_queue({})
        self.assertEqual(self.get("/app/v1/push/latest")["events"], [{**webpush.NOTHING, "instance": None}])

    def test_latest_takes_only_the_requested_instance_and_keeps_its_own_fallback(self):
        webpush._save_queue({self.device: {"pending": [
            {"title": "wisp replied", "body": "Done.", "url": "/i/wisp/chat", "tag": "wisp", "at": time.time()},
            {"title": "tundra asks", "body": "Which one?", "url": "/i/tundra/chat", "tag": "tundra-message",
             "at": time.time()}]}})
        self.assertEqual(self.get("/app/v1/push/latest?instance=wisp")["events"][0]["body"], "Done.")
        self.assertEqual(self.get("/app/v1/push/latest?instance=tundra")["events"][0]["body"], "Which one?")
        self.assertEqual(self.get("/app/v1/push/latest?instance=wisp")["events"][0]["body"], "Done.")
        self.assertEqual(self.get("/app/v1/push/latest?instance=ghost")["events"],
                         [{**webpush.NOTHING, "instance": None}])

    def test_latest_rejects_an_invalid_instance_before_taking_events(self):
        queued = {self.device: {"pending": [
            {"title": "wisp replied", "body": "Done.", "url": "/i/wisp/chat", "tag": "wisp", "at": time.time()}]}}
        webpush._save_queue(queued)
        for name in ("", "../wisp", "wisp/chat", "wisp space", "Wisp", "wisp_name", "a" * 32):
            self.error(self.api("/app/v1/push/latest?" + urllib.parse.urlencode({"instance": name})),
                       400, "bad_request")
        self.assertEqual(webpush._queue(), queued)


# -- the fixtures the Swift side decodes (§13.5) -------------------------------------------------------------------------
class FixturesTest(AppTest):
    """Representative answers, serialised by the server itself: SHAFTKit's tests decode every one. A checked-in
    fixture that differs fails here; SHAFT_UPDATE_FIXTURES=1 rewrites them (then the Swift tests must decode them)."""

    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Rome"
        time.tzset()
        self.addCleanup(self._restore_tz)
        super().setUp()
        self.paired_since("2026-11-03T12:00:00")
        self.out: dict[str, object] = {}

    def _restore_tz(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def keep(self, name: str, value) -> None:
        self.out[name] = value

    def scenario(self) -> None:
        """The contract's own example chat (docs/app-api.md §4.9), with a few more of each kind."""
        self.message("2026-11-03T14:02:30", "20261103-140230-118204-from-wisp.md",
                     "Should the nightly report keep the per-account lines?", kind="question", read=False)
        self.message("2026-11-03T14:03:01", "20261103-140301-552019-from-operator.md",
                     "Look at the March reconciliation when you have time.", frm="operator", to="wisp",
                     via=messages.BACKGROUND, mid="5e2d9c1b-7a3f-4e60-8b2d-1c9f0a7e3d54", read=False)
        self.rating("f-6a0c2e4b8d1f3a5c7e9b0d2f", {"kind": "chat", "chat": CID, "k": 1}, "up", "2026-11-03T14:01:12")

    def base_turns(self) -> list[dict]:
        return [
            turn("user", "2026-11-03T13:58:40", "...", shown=app_api.quote_text(
                "Yes, once the tests pass. This is the failing run:",
                [{"quote": "Ship the ledger fix today?", "answer": "", "doc": ""}]),
                 mid="0b8e6c3a-5d1f-4c2e-9a7b-2f4d6e8a1c3b",
                 attachments=[{"name": "ci-run.png", "path": "instances/wisp/attachments/from-operator/x/ci-run.png",
                               "bytes": 48213}]),
            turn("assistant", "2026-11-03T14:01:12",
                 "The failure is the rounding in `net_daily`. Fixed and pushed; the summary is in **ledger.md**.",
                 activity=["read_file notes/docs/ledger.md", "run tests: 412 passed"],
                 docs=[{"path": "ledger.md", "created": False}], answered=[{"id": "c3", "doc": "ledger.md"}]),
            turn("user", "2026-11-03T14:05:50", "Rerun the report for October.", shown="Rerun the report for October.",
                 mid="a41c7e09-3b2d-4f8e-9c61-0d5b7a2e4f13", via="voice"),          # spoken (via)
        ]

    def test_write_or_check_the_fixtures(self):
        self.scenario()
        turns = self.base_turns()
        mids = ["0b8e6c3a-5d1f-4c2e-9a7b-2f4d6e8a1c3b", "a41c7e09-3b2d-4f8e-9c61-0d5b7a2e4f13"]
        # idle: a plain answered chat
        idle = self.chat(turns + [turn("assistant", "2026-11-03T14:06:40", "October is done: 214 accounts.",
                                       activity=["run report --month 2026-10"])], mids=mids)
        self.keep("idle", self.get("/app/v1/chat/wisp"))
        # failed: the contract's failed reply with a safe Retry, a stopped one and an unsafe one
        failed = turns + [
            turn("assistant", "2026-11-03T14:06:40", "", note="error: CloudError: OpenRouter HTTP 502 (tools_ran=false)",
                 note_kind="model", note_retry=True, retry_text="Rerun the report for October."),
            turn("user", "2026-11-03T14:07:00", "Try the summary.", shown="Try the summary."),
            turn("assistant", "2026-11-03T14:07:30", "Half an answer", note="error: ConnectionResetError: reset",
                 note_kind="connection", note_retry=False, retry_text="Try the summary."),
            turn("user", "2026-11-03T14:08:00", "Long job.", shown="Long job."),
            turn("assistant", "2026-11-03T14:08:20", "", note="stopped by you", note_kind="stopped"),
        ]
        self.chat(failed, mids=mids)
        self.keep("failed", self.get("/app/v1/chat/wisp"))
        # live, then a partial when only the reply moved; queued while it runs
        live = self.chat(turns, state="replying", mids=mids,
                         reply={"started": "2026-11-03T14:07:02", "activity": ["read_file notes/docs/ledger.md",
                                                                                 "run report --month 2026-10"],
                                "answer": "October's report is running; so far"})
        first = self.get("/app/v1/chat/wisp")
        self.keep("live", first)
        live["reply"].update(activity=live["reply"]["activity"] + ["write notes/docs/october.md"],
                             answer="October's report is running; so far 214 accounts reconciled.")
        web_chat.save(live)
        self.keep("live-partial", self.get(f"/app/v1/chat/wisp?since={first['version']}&live={first['live']}&wait=0"))
        live["queued"] = [{"role": "user", "content": "And send me the totals.", "shown": "And send me the totals.",
                           "mid": "9d3e0f2a-6b1c-4a7d-8e5f-3c2b1a0d9e8f", "attachments": [
                               {"name": "totals.csv", "path": "instances/wisp/attachments/from-operator/y/totals.csv",
                                "bytes": 1200}]}]
        live["mids"] = mids + ["9d3e0f2a-6b1c-4a7d-8e5f-3c2b1a0d9e8f"]
        web_chat.save(live)
        self.keep("queued", self.get("/app/v1/chat/wisp"))
        # quotes: several Reply cards, one from a document, made into the text on the server
        self.chat([turn("user", "2026-11-03T14:10:00", "...", mid="c1a2b3c4-0000-4000-8000-000000000001",
                        shown=app_api.quote_text("Thanks.", [
                            {"quote": "Should the nightly report keep the per-account lines?", "answer": "Yes.",
                             "doc": ""},
                            {"quote": "Net daily totals are rounded once,\nat the end.", "answer": "Per account.",
                             "doc": "ledger.md"}]))])
        self.keep("quotes", self.get("/app/v1/chat/wisp"))
        # attachments both ways
        self.message("2026-11-03T14:11:00", "20261103-141100-000001-from-wisp.md", "The chart and the data.",
                     attachments=[{"name": "chart.png", "path": "instances/wisp/out/chart.png", "bytes": 20480},
                                  {"name": "data.csv", "path": "instances/wisp/out/data.csv", "bytes": 512},
                                  {"name": "report.pdf", "path": "instances/wisp/out/report.pdf", "bytes": 90000}])
        self.chat([turn("user", "2026-11-03T14:10:30", "...", shown="Two files.", mid="c1a2b3c4-0000-4000-8000-000000000002",
                        attachments=[{"name": "ci-run.png", "path": "instances/wisp/a/ci-run.png", "bytes": 48213},
                                     {"name": "notes.md", "path": "instances/wisp/a/notes.md", "bytes": 300}])])
        self.keep("attachments", self.get("/app/v1/chat/wisp"))
        # sent: what it sent (a message, an answered question), what was left for its next cycle
        self.message("2026-11-03T14:12:00", "20261103-141200-000001-from-wisp.md", "Answered question?",
                      kind="question", read=True)
        # ... and its cycle's answer to work queued in this thread (send_message's conversation): part of the thread
        self.message("2026-11-03T14:13:00", "20261103-141300-000001-from-wisp.md", "March reconciles: 3 lines moved.",
                     talk=None, thread=CID)
        self.chat([turn("user", "2026-11-03T14:01:00", "hi", shown="hi")])
        self.keep("sent", self.get("/app/v1/chat/wisp"))
        # from: an answer for a thread that had ended shows in From, linked to it (re)
        self.chat([turn("user", "2026-11-03T09:00:00", "Check the fees", shown="Check the fees")],
                  state="ended", cid="web-00000000000000e1", created="2026-11-03T09:00:00", title="Fees",
                  ended="2026-11-03T09:30:00")                       # web_chat.end always stamps it
        self.message("2026-11-03T14:14:00", "20261103-141400-000001-from-wisp.md", "The fees match the contract.",
                     talk=None, re="web-00000000000000e1")
        self.keep("from", self.get("/app/v1/from/wisp"))
        # feedback: a rating sent, one pending with the next message, one on a message; a turn that carried one
        self.rating("f-111111111111111111111111", {"kind": "chat", "chat": CID, "k": 3}, "down", "2026-11-03T14:06:40",
                    reason="Wrong month")
        self.rating("f-222222222222222222222222", {"kind": "message", "file": "20261103-140230-118204-from-wisp.md"},
                    "up", "2026-11-03T14:02:30", reason="Good question", sent="2026-11-03T14:09:00")
        self.chat(turns + [turn("assistant", "2026-11-03T14:06:40", "September's report."),
                           turn("user", "2026-11-03T14:20:00", "...", shown="", feedback=["f-333333333333333333333333"])],
                  mids=mids, pending_feedback=[{"id": "f-111111111111111111111111", "note": "n"}])
        self.keep("feedback", self.get("/app/v1/chat/wisp"))
        # ended, and earlier: a long chat's window
        self.chat(turns + [turn("assistant", "2026-11-03T14:06:40", "Done.")], state="ended", ended="2026-11-03T14:30:00",
                  mids=mids)
        self.keep("ended", self.get("/app/v1/chat/wisp"))
        self.keep("chats", self.get("/app/v1/chats/wisp"))
        many = [turn("user" if k % 2 == 0 else "assistant", f"2026-11-02T{9 + k // 60:02d}:{k % 60:02d}:00",
                     f"Answer {k}." if k % 2 else "", shown=f"Message {k}.") for k in range(84)]
        self.chat(many, cid="web-0000000000000084", created="2026-11-02T09:00:00")
        self.keep("earlier", self.get("/app/v1/chat/wisp/web-0000000000000084"))
        # the chat list, documents and comments, push
        self.chat(turns + [turn("assistant", "2026-11-03T14:06:40", "Done.")], mids=mids,
                  saved_docs={"ledger.md": {"sha": "old", "text": "# Ledger\n\nRounded per account.\n",
                                            "ts": "2026-11-03T13:40:00", "added": ["Rounded per account."]}})
        self.keep("instances", self.get("/app/v1/instances"))
        ledger = "# Ledger\n\nNet daily totals are rounded once, at the end.\n"
        for path, text in (("ledger.md", ledger), ("reports/october.md", "# October\n")):
            p = self.inst / "wisp" / "notes" / "docs" / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            os.utime(p, (1793710800.0, 1793710800.0))
        store = {"next": 4, "notices": [], "threads": {
            "c3": {"doc": "ledger.md", "by": "operator", "state": "open", "sha": "0" * 64,
                   "anchor": {"exact": "rounded once, at the end", "prefix": "Net daily totals are ", "suffix": ".",
                              "start": 30},
                   "created": "2026-11-03T13:50:12", "unsectioned": [], "posts": [
                       {"pid": "p-1e2d3c4b5a697887", "by": "operator", "text": "Per account or overall?",
                        "ts": "2026-11-03T13:50:12", "sent": None, "seen": f"{CID}#1"},
                       {"pid": "p-9a8b7c6d5e4f3021", "by": "wisp", "text": "Per account, then summed.",
                        "ts": "2026-11-03T14:01:12", "sent": None, "turn": f"{CID}#1"}]},
            "c2": {"doc": "ledger.md", "by": "wisp", "state": "resolved", "sha": docs.sha(ledger.encode()),
                   "anchor": None, "created": "2026-11-03T13:00:00", "unsectioned": [f"{CID}#1"], "posts": [
                       {"pid": "p-0000000000000002", "by": "wisp", "text": "Is this the right place?",
                        "ts": "2026-11-03T13:00:00", "sent": None, "turn": "message:20261103-130000-1-from-wisp.md"}]},
            "c1": {"doc": "reports/october.md", "by": "operator", "state": "open", "sha": "1" * 64, "anchor": None,
                   "created": "2026-11-03T12:30:00", "unsectioned": [], "posts": [
                       {"pid": "p-0000000000000001", "by": "operator", "text": "Add the totals.",
                        "ts": "2026-11-03T12:30:00", "sent": "inbox/20261103-123000-1-from-operator.md"}]}}}
        (self.sup / "web" / "comments").mkdir(parents=True, exist_ok=True)
        (self.sup / "web" / "comments" / "wisp.json").write_text(json.dumps(store))
        self.keep("docs", self.get("/app/v1/docs/wisp"))
        self.keep("doc-comments", self.get("/app/v1/docs/wisp/doc?path=ledger.md"))
        webpush._save_queue({self.device: {"pending": [
            {"title": "wisp replied", "body": "Done.", "url": "/i/wisp/chat", "tag": "wisp", "at": time.time()},
            {"title": "wisp asks", "body": "Should the nightly report keep the per-account lines?",
             "url": "/i/wisp/chat", "tag": "wisp-ask", "at": time.time()}]}})
        self.keep("push-latest", self.get("/app/v1/push/latest"))
        self.keep("errors", self.errors(first))
        link = web.new_pairing("fixture-app")
        with mock.patch.object(web.secrets, "token_hex", return_value="0123456789abcdef"), \
                mock.patch.object(web.secrets, "token_urlsafe", return_value="fixture-secret"):
            self.keep("pair", self.get("/app/v1/pair", method="POST", bearer=False,
                                       data={"link": link, "platform": "ios"}))
        self.keep("send", json.loads(self.send(text="Fixture message.").body))
        with mock.patch.object(feedback.secrets, "token_hex", return_value="0" * 24):
            self.keep("feedback-response", self.get("/app/v1/feedback/wisp", method="POST",
                      data={"op": "rate", "kind": "chat", "cid": CID, "k": 1, "thumb": "up"}))
        self.chat([turn("user", "2026-11-03T14:05:50", "Again.", shown="Again.", retried=True),
                   turn("assistant", "2026-11-03T14:06:40", "", retried=True),
                   turn("user", "2026-11-03T14:07:02", "Again.", shown="Again.")])
        self.keep("retried", self.get("/app/v1/chat/wisp"))
        self.cycles_scenario()
        self.keep("plan", self.get("/app/v1/plan/wisp"))
        self.keep("cycles", self.get("/app/v1/cycles/wisp"))
        self.keep("cycle", self.get("/app/v1/cycles/wisp/2026-11-03T13:58:40"))
        self.keep("stream", self.stream_events())
        self.check()

    def stream_events(self) -> list[dict]:
        """A voice turn's stream (§6.1) as the server writes it: every event type, decoded from its SSE lines."""
        chat = self.chat([turn("user", "2026-11-03T14:30:00", "Weather?", shown="Weather?", via="voice")],
                         state="replying", reply={"started": "2026-11-03T14:30:01", "activity": [], "answer": ""})
        relay = web_chat.Relay(chat["id"])
        relay.id = "3f0c9e21a4b7d658"
        web_chat._streams[chat["id"]] = relay
        self.addCleanup(web_chat._streams.clear)
        for kind, fields in (("status", {"phase": "model", "detail": "thinking"}), ("draft", {"text": "Let me look."}),
                             ("step_end", {"answer": False}), ("tool_start", {"name": "read_file"}),
                             ("tool_end", {"name": "read_file", "ok": True}),
                             ("draft", {"text": "It is sunny in Milan."}), ("step_end", {"answer": True})):
            relay.emit(kind, **fields)
        relay.close_stream("It is sunny in Milan.", None)
        r = self.api("/app/v1/chat/wisp/stream")
        self.assertEqual(r.status, 200, r.body)
        raw = b"".join(r.stream).decode()
        return [json.loads(line[6:]) for line in raw.split("\n") if line.startswith("data: ")]

    def errors(self, feed: dict) -> dict:
        """One answer of each error code, as the server gives it."""
        out = {}

        def grab(name, r):
            self.assertGreaterEqual(r.status, 400, (name, r.body))
            out[name] = {"status": r.status, "body": json.loads(r.body)}
        grab("unpaired", self.api("/app/v1/instances", bearer=False))
        grab("login", self.api("/app/v1/instances", login="someone@example.com"))
        grab("host", self.api("/app/v1/instances", host="evil.example.com"))
        grab("pair_expired", self.api("/app/v1/pair", "POST", {"token": "used", "platform": "ios"}, bearer=False))
        grab("bad_request", self.api("/app/v1/chat/wisp?wait=99"))
        grab("not_found", self.api("/app/v1/chat/ghost"))
        grab("method", self.api("/app/v1/instances", "POST", {}))
        grab("refused", self.api("/app/v1/chat/wisp/stop", "POST", {"cid": CID}))
        grab("stale", self.api("/app/v1/docs/wisp/save", "POST", {"path": "ledger.md", "text": "mine\n", "sha": "old"}))
        p = self.inst / "wisp" / "notes" / "docs" / "huge.md"
        p.write_text("x" * (docs.MAX_BYTES + 1))
        grab("too_big", self.api("/app/v1/docs/wisp/doc?path=huge.md"))
        grab("stale_version", self.api("/app/v1/chat/wisp/seen", "POST", {"version": feed["version"]}))
        grab("not_replying", self.api("/app/v1/chat/wisp/stream"))
        out["too_large"] = {"status": 413, "body": json.loads(app_api.error(
            413, "too_large", f"Too large: at most {(messages.ATTACH_MAX_TOTAL + 1_000_000) // 1_000_000} MB can be "
                              "sent at once.").body)}
        with mock.patch.object(web_chat, "feed", side_effect=RuntimeError("boom")):
            grab("internal", self.api("/app/v1/chat/wisp"))
        self.apns.problem = "SHAFT_APNS_KEY_ID is not set"
        grab("push_off", self.api("/app/v1/push/register", "POST", {"token": "ab", "env": "sandbox", "topic": "t",
                                                                     "platform": "ios"}))
        self.assertEqual({v["body"]["error"]["code"] for v in out.values()},
                         {"unpaired", "login", "host", "pair_expired", "bad_request", "not_found", "method", "refused",
                          "stale", "too_big", "stale_version", "too_large", "internal", "push_off", "not_replying"})
        return out

    def check(self) -> None:
        names = {"idle", "live", "live-partial", "queued", "quotes", "attachments", "sent", "feedback", "failed",
                 "earlier", "ended", "instances", "docs", "doc-comments", "push-latest", "errors", "pair", "chats",
                 "send", "feedback-response", "retried", "plan", "cycles", "cycle", "stream", "from"}
        self.assertEqual(set(self.out), names)
        if os.environ.get("SHAFT_UPDATE_FIXTURES") == "1":
            FIXTURES.mkdir(exist_ok=True)
            for name, value in self.out.items():
                (FIXTURES / f"{name}.json").write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
            return
        for name, value in self.out.items():
            path = FIXTURES / f"{name}.json"
            self.assertTrue(path.is_file(), f"{path} is missing: SHAFT_UPDATE_FIXTURES=1 writes it")
            self.assertEqual(json.loads(path.read_text()), value,
                             f"{path.name} is not what the server answers now: if the change is meant, rewrite the "
                             "fixtures (SHAFT_UPDATE_FIXTURES=1) and make the Swift side decode them")


if __name__ == "__main__":
    unittest.main()

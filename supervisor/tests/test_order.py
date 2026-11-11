"""Manual order guards the jumping-thread bug and concurrent device moves."""
import html
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import core, web_chat
from supervisor.tests import test_app_api as app_tests


class OrderTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sup = Path(tmp.name)
        for target in (core, web_chat):
            patch = mock.patch.object(target, "SUP_DIR", self.sup)
            patch.start()
            self.addCleanup(patch.stop)

    def thread(self, cid, created, **extra):
        chat = dict(id=cid, instance="wisp", created=created, state="idle", turns=[], reply=None, **extra)
        web_chat.save(chat)
        return chat

    def test_reply_does_not_jump_a_thread_to_the_top(self):
        a = self.thread("web-a", "2026-01-01")
        self.thread("web-b", "2026-01-02")
        a["turns"] = [dict(role="assistant", content="reply", ts="2026-01-03")]
        web_chat.save(a)
        self.assertEqual([c["id"] for c in web_chat.threads("wisp")], ["web-b", "web-a"])

    def test_new_thread_is_first_even_when_creation_timestamps_tie(self):
        from supervisor import order
        with mock.patch.object(web_chat, "_now", return_value="2026-01-01"):
            a = web_chat.new_thread("wisp", "operator")
            b = web_chat.new_thread("wisp", "operator")
        self.assertEqual([c["id"] for c in web_chat.threads("wisp")], [b["id"], a["id"]])
        self.assertEqual(order.thread_rev("wisp"), 2)
        self.assertEqual((self.sup / "web/thread-order/wisp.json").stat().st_mode & 0o777, 0o600)

    def test_moves_apply_to_latest_order_from_two_devices_and_prune_ended_threads(self):
        from supervisor import order
        a = self.thread("web-a", "2026-01-01")
        self.thread("web-b", "2026-01-02")
        self.thread("web-c", "2026-01-03")
        self.assertEqual(order.move_thread("wisp", "web-a", None),
                         {"rev": 1, "ids": ["web-a", "web-c", "web-b"]})
        self.assertEqual(order.move_thread("wisp", "web-b", "web-a"),
                         {"rev": 2, "ids": ["web-a", "web-b", "web-c"]})
        self.assertEqual(order.move_thread("wisp", "web-b", "web-b"),
                         {"rev": 2, "ids": ["web-a", "web-b", "web-c"]})
        self.assertEqual(order.move_thread("wisp", "web-c", "web-missing"),
                         {"rev": 3, "ids": ["web-c", "web-a", "web-b"]})
        a["state"] = "ended"
        web_chat.save(a)
        self.assertEqual(order.move_thread("wisp", "web-b", "web-a"),
                         {"rev": 4, "ids": ["web-b", "web-c"]})
        self.assertEqual(json.loads((self.sup / "web/thread-order/wisp.json").read_text()),
                         {"rev": 4, "ids": ["web-b", "web-c"]})
        for cid in ("from", "web-a", "web-missing"):
            with self.assertRaises(order.Refused):
                order.move_thread("wisp", cid, None)
        self.assertEqual(order.thread_rev("wisp"), 4)
        self.thread("web-d", "2026-01-04")
        self.thread("web-e", "2026-01-05")
        self.assertEqual([c["id"] for c in web_chat.threads("wisp")],
                         ["web-e", "web-d", "web-b", "web-c"])

    def test_invalid_instance_names_cannot_escape_the_order_directory(self):
        from supervisor import order
        for name in ("../example", "/example", "Example", ""):
            with self.assertRaises(order.Refused):
                order.threads(name, [])
            with self.assertRaises(order.Refused):
                order.add_thread(name, "web-a")
            with self.assertRaises(order.Refused):
                order.move_thread(name, "web-a", None)
            with self.assertRaises(order.Refused):
                order.thread_rev(name)

    def test_instance_order_new_names_last_and_move_semantics(self):
        from supervisor import order, web
        with mock.patch.object(core, "instance_names", return_value=["wisp", "kiln"]):
            self.assertEqual(web._names(), ["wisp", "kiln"])
            self.assertEqual(order.move_instance("kiln", None), {"rev": 1, "ids": ["kiln", "wisp"]})
            self.assertEqual(web._names(), ["kiln", "wisp"])
            self.assertEqual(order.move_instance("kiln", "kiln"), {"rev": 1, "ids": ["kiln", "wisp"]})
            self.assertEqual(order.move_instance("kiln", "wisp"), {"rev": 2, "ids": ["wisp", "kiln"]})
            self.assertEqual(order.move_instance("kiln", "missing"), {"rev": 3, "ids": ["kiln", "wisp"]})
            with self.assertRaises(order.Refused):
                order.move_instance("missing", None)
        self.assertEqual(order.instances(["wisp", "example"]), ["wisp", "example"])
        self.assertEqual(order.instances(["wisp", "kiln", "example"]), ["kiln", "wisp", "example"])
        self.assertEqual(order.instance_rev(), 3)
        self.assertEqual((self.sup / "web/instance-order.json").stat().st_mode & 0o777, 0o600)


class OrderRoutesTest(app_tests.AppTest):
    def test_web_moves_have_unqueue_auth_and_csrf_and_return_authoritative_order(self):
        from supervisor import order
        self.chat([], cid="web-a")
        self.chat([], cid="web-b", created="2026-11-04")
        cookie = self.paired()
        routes = [("/chat/wisp/move", b"id=web-a&after=web-b"),
                  ("/instances/move", b"name=wisp&after=")]
        headers = {"sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(cookie),
                   "content-type": "application/x-www-form-urlencoded"}
        for path, body in [("/chat/wisp/unqueue", b"mid=x"), *routes]:
            for bad_headers in ({}, {"sec-fetch-site": "same-origin"},
                                {**headers, "sec-fetch-site": "cross-site", "origin": "https://example.com"}):
                self.assertEqual(self.req(path, "POST", cookie=cookie, body=body, headers=bad_headers).status, 403)
            self.assertEqual(self.req(path, "POST", body=body, headers=headers).status, 403)
        for (path, body), ids in zip(routes, (["web-b", "web-a"], ["wisp"])):
            r = self.req(path, "POST", cookie=cookie, body=body, headers=headers)
            self.assertEqual(r.status, 200, r.body)
            self.assertIn(b'data-rev="1"', r.body)
            stored = re.search(r'data-ids="([^"]*)"', r.body.decode())[1]
            self.assertEqual(json.loads(html.unescape(stored)), ids)
        self.assertEqual(order.thread_rev("wisp"), 1)
        for path, body in [("/chat/wisp/move", b"id=from"), ("/instances/move", b"name=missing")]:
            self.assertEqual(self.req(path, "POST", cookie=cookie, body=body, headers=headers).status, 409)

    def test_app_moves_return_order_refuse_unknown_items_and_require_bearer(self):
        self.chat([], cid="web-a")
        self.chat([], cid="web-b", created="2026-11-04")
        self.assertEqual(self.get("/app/v1/threads/wisp/move", method="POST", data={"id": "web-a", "after": None}),
                         {"rev": 1, "ids": ["web-a", "web-b"]})
        self.assertEqual(self.get("/app/v1/instances/move", method="POST", data={"name": "wisp", "after": None}),
                         {"rev": 1, "ids": ["wisp"]})
        self.chat([], cid="web-ended", state="ended")
        web_chat.save({**web_chat.load("web-a"), "id": "web-foreign", "instance": "kiln"})
        for cid in ("from", "web-unknown", "web-ended", "web-foreign"):
            self.error(self.api("/app/v1/threads/wisp/move", "POST", {"id": cid}), 409, "refused")
        self.error(self.api("/app/v1/instances/move", "POST", {"name": "missing"}), 409, "refused")
        self.error(self.api("/app/v1/threads/wisp/move", "POST", {"id": "web-a", "after": 5}), 400, "bad_request")
        for path in ("/app/v1/threads/wisp/move", "/app/v1/instances/move"):
            self.error(self.req(path, "POST"), 401, "unpaired")

    def test_threads_revision_changes_on_rename_order_create_close_and_state_only(self):
        from supervisor import order
        a = self.chat([], cid="web-a", title="A")
        self.chat([], cid="web-b", title="B", created="2026-11-04")

        def revision():
            threads = self.get("/app/v1/threads/wisp")
            rows = self.get("/app/v1/instances")
            rev = threads["threads_rev"]
            self.assertEqual(rows["instances"][0]["threads_rev"], rev)
            self.assertEqual(rows["instances_rev"], order.instance_rev())
            return rev

        rev = revision()
        self.assertEqual(revision(), rev)
        web_chat.rename("wisp", "web-a", "Renamed")
        renamed = revision()
        self.assertNotEqual(renamed, rev)
        order.move_thread("wisp", "web-a", None)
        moved = revision()
        self.assertNotEqual(moved, renamed)
        a = web_chat.load("web-a")
        for state, reply in (("replying", {"started": "2026-11-04"}),
                             ("replying", {"started": "2026-11-04", "waiting": "waiting"}), ("idle", None)):
            a.update(state=state, reply=reply)
            web_chat.save(a)
            changed = revision()
            self.assertNotEqual(changed, moved)
            moved = changed
        a["turns"] = [app_tests.turn("assistant", "2026-11-04", "Answer")]
        web_chat.save(a)
        self.assertEqual(revision(), moved)  # Activity alone does not invalidate the row order/title/state.
        a["state"] = "ended"
        web_chat.save(a)
        closed = revision()
        self.assertNotEqual(closed, moved)
        web_chat.new_thread("wisp", "operator", "New")
        self.assertNotEqual(revision(), closed)

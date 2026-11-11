"""One operator read mark across native apps and browsers, including old stores."""
import json
from unittest import mock
from urllib.parse import urlencode

from supervisor import core, web, web_chat, web_pages, web_seen
from supervisor.tests.test_web import WebTest

OLD = "2026-11-03T13:00:00"
LAST = "2026-11-03T14:00:00"
LATER = "2026-11-03T15:00:00"


def model(cid, at=LAST, count=1):
    return {"version": at + str(count), "chat": {"id": cid}, "last": at, "last_count": count,
            "items": [{"type": "assistant", "ts": at}] * count}


class SeenSyncTest(WebTest):
    def setUp(self):
        super().setUp()
        self.models = {"web-a": model("web-a"), "web-b": model("web-b"), "from": model("from")}
        web._write("devices.json", {"A": {"created": OLD}, "B": {"created": LATER}})
        for patch in (
            mock.patch.object(web_chat, "feed", side_effect=lambda n, cid=None, **kw: self.models.get(cid or "web-a")),
            mock.patch.object(web_chat, "from_feed", side_effect=lambda n, **kw: self.models["from"]),
            mock.patch.object(web_chat, "public_id", side_effect=lambda n, cid: cid),
            mock.patch.object(web_chat, "shown", return_value={"id": "web-a"}),
            mock.patch.object(web_chat, "threads", return_value=[{"id": "web-a"}, {"id": "web-b"}]),
            mock.patch.object(core, "instance_names", return_value=["example"]),
            mock.patch.object(web, "_names", return_value=["example"]),
            mock.patch.object(web, "log"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def flags(self, device="B"):
        return web_seen.unread_feeds(device, "example", self.models)

    def test_reading_on_device_a_clears_unread_on_device_b(self):
        self.assertTrue(self.flags()["web-a"])
        self.assertTrue(web_seen.seen("A", "example", self.models["web-a"]["version"], "web-a"))
        self.assertFalse(self.flags()["web-a"])
        self.assertTrue(self.flags()["web-b"])

    def test_stale_device_cannot_move_timestamp_or_same_second_count_back(self):
        web_seen.seen("A", "example", self.models["web-a"]["version"], "web-a")
        self.models["web-a"] = model("web-a", LATER, 2)
        web_seen.seen("A", "example", self.models["web-a"]["version"], "web-a")
        self.models["web-a"] = model("web-a", LATER, 1)
        web_seen.seen("B", "example", self.models["web-a"]["version"], "web-a")
        self.models["web-a"] = model("web-a", LAST)
        web_seen.seen("B", "example", self.models["web-a"]["version"], "web-a")
        web_seen.seen_until("B", "example", "web-a", LAST)
        mark = web._read(web_seen.STORE)["operator"]["example"]["web-a"]
        self.assertEqual((mark["last"], mark["last_count"]), (LATER, 2))

    def test_device_migration_uses_newest_then_highest_count_and_is_idempotent(self):
        web._write(web_seen.STORE, {
            "A": {"example": {"web-a": {"last": LAST, "last_count": 1}, "web-b": {"last": LATER}}},
            "B": {"example": {"web-a": {"last": LAST, "last_count": 3}, "web-b": {"last": OLD}}},
            "operator": {"example": {"from": {"last": LAST}}},
        })
        self.assertEqual(self.flags(), {"web-a": False, "web-b": False, "from": False})
        expected = {"operator": {"example": {"web-a": {"last": LAST, "last_count": 3},
                                             "web-b": {"last": LATER}, "from": {"last": LAST}}}}
        self.assertEqual(web._read(web_seen.STORE), expected)
        self.flags("A")
        self.assertEqual(web._read(web_seen.STORE), expected)

    def test_legacy_single_mark_migrates_to_shown_conversation_and_main(self):
        web._write(web_seen.STORE, {"A": {"example": {"last": LAST}}})
        self.assertEqual(self.flags(), {"web-a": False, "web-b": True, "from": False})
        self.assertEqual(web._read(web_seen.STORE), {
            "operator": {"example": {"web-a": {"last": LAST}, "from": {"last": LAST}}}})

    def test_unpair_keeps_shared_marks(self):
        web_seen.seen("A", "example", self.models["web-a"]["version"], "web-a")
        before = web._read(web_seen.STORE)
        with mock.patch("supervisor.apns.unregister"), mock.patch("supervisor.webpush.unsubscribe"):
            self.assertEqual(web.unpair("A"), 1)
        self.assertEqual(web._read(web_seen.STORE), before)
        self.assertFalse(self.flags()["web-a"])

    def test_unmarked_baseline_is_earliest_pairing_for_every_device(self):
        self.assertTrue(self.flags("B")["web-a"])
        self.models["web-a"] = model("web-a", "2026-11-03T12:00:00")
        self.assertFalse(self.flags("A")["web-a"])
        self.assertFalse(self.flags("B")["web-a"])

    def test_instance_api_and_rail_distinguish_main_threads_and_aggregate(self):
        from supervisor import app_api
        with mock.patch("supervisor.messages.history", return_value=[]), \
                mock.patch.object(web_chat, "summary", return_value={}), \
                mock.patch.object(web_chat, "open_chats", return_value=[]), \
                mock.patch.object(web, "_live", return_value={}), \
                mock.patch.object(web, "_next_cycle", return_value=None), \
                mock.patch.object(core, "instance_config", return_value={}), \
                mock.patch("supervisor.web_views.busy_note", return_value=None):
            for cid, expected in ((None, (True, 2, True, 1)), ("web-a", (True, 1, True, 1)),
                                  ("web-b", (True, 0, True, 1)), ("from", (False, 0, False, 0))):
                if cid:
                    web_seen.seen("A", "example", self.models[cid]["version"], cid)
                req = web.Request("GET", "/app/v1/instances", {}, b"")
                req.device = "B"
                got = json.loads(app_api._instances(req).body)
                row = got["instances"][0]
                self.assertEqual((row["unread_main"], row["unread_threads"], row["unread"], got["badge"]), expected)
                rail = str(web_pages.rail(["example"], "i:example", chats={"example": {"threads": [
                    {"id": k, "title": k, "state": "idle"} for k in ("web-a", "web-b")]}}))
                self.assertIn(f'data-unread-main="{int(expected[0])}" data-unread-threads="{expected[1]}"', rail)
                self.assertIn(f'data-seen-key="from" data-last="" data-unread="{int(expected[0])}"', rail)

    def post_seen(self, last, token=True):
        cookie = self.cookie
        headers = {"sec-fetch-site": "same-origin", "content-type": "application/x-www-form-urlencoded"}
        if token:
            headers["x-csrf-token"] = self.csrf(cookie)
        return self.req("/seen", "POST", cookie=cookie, headers=headers,
                        body=urlencode({"inst": "example", "key": "web-a", "last": last}).encode())

    def test_web_post_seen_requires_csrf_and_advances_only_visible_conversation(self):
        self.cookie = self.paired()
        self.assertEqual(self.post_seen(LAST, token=False).status, 403)
        self.assertTrue(self.flags()["web-a"])
        self.assertEqual(self.post_seen(LAST).status, 200)
        self.assertEqual(self.flags(), {"web-a": False, "web-b": True, "from": True})

    def test_web_future_stamp_is_bounded_so_unseen_later_items_remain_unread(self):
        self.cookie = self.paired()
        self.assertEqual(self.post_seen("2099-01-01T00:00:00").status, 200)
        self.assertEqual(web._read(web_seen.STORE)["operator"]["example"]["web-a"]["last"], LAST)
        self.models["web-a"] = model("web-a", LATER)
        self.assertTrue(self.flags()["web-a"])

    def test_web_partial_feed_and_same_second_counts(self):
        self.cookie = self.paired()
        self.models["web-a"] = model("web-a", LATER, 2)
        self.models["web-a"]["items"].insert(0, {"type": "assistant", "ts": LAST})
        self.assertEqual(self.post_seen(LAST).status, 200)
        self.assertEqual(web._read(web_seen.STORE)["operator"]["example"]["web-a"]["last"], LAST)
        self.assertTrue(self.flags()["web-a"])
        self.assertEqual(self.post_seen(LATER).status, 200)
        self.assertFalse(self.flags()["web-a"])
        self.models["web-a"]["last_count"] += 1
        self.assertTrue(self.flags()["web-a"])

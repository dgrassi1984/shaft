"""Thread identity survives feed reads, seen marks and notification delivery."""
import time
from unittest import mock

from supervisor import apns, app_api, messages, web, web_alerts, web_chat, web_seen, webpush
from supervisor.tests.test_app_api import AppTest, turn


NOTIFY = webpush.notify


class ThreadsAppTest(AppTest):
    def setUp(self):
        super().setUp()
        self.paired_since("2026-11-03T12:00:00")
        self.a = self.chat([turn("assistant", "2026-11-03T14:00:00", "Answer A")],
                           cid="web-a", title="Topic A")
        self.b = self.chat([turn("assistant", "2026-11-03T14:01:00", "Answer B")],
                           cid="web-b", title="Topic B", created="2026-11-03T13:59:00", state="replying",
                           reply={"started": "2026-11-03T14:01:00", "waiting": "waiting for Topic A"})
        self.from_model = web_chat.build("wisp", None, [{
            "from": "wisp", "to": "operator", "file": "note.md", "text": "From a cycle",
            "ts": "2026-11-03T14:02:00", "attachments": [{"name": "a.txt", "bytes": 3}]}])
        patch = mock.patch.object(web_chat, "from_feed", create=True, return_value=self.from_model)
        patch.start()
        self.addCleanup(patch.stop)

    def seen(self, cid):
        model = self.from_model if cid == "from" else web_chat.feed("wisp", cid)
        return self.get("/app/v1/chat/wisp/seen", method="POST", data={"cid": cid, "version": model["version"]})

    def test_threads_and_rail_include_each_open_thread_and_from(self):
        self.chat([], state="ended", cid="web-ended")
        got = self.get("/app/v1/threads/wisp")
        self.assertRegex(got.pop("threads_rev"), r"^[0-9a-f]{16}$")
        self.assertEqual(got, {"threads": [
            {"id": "web-b", "title": "Topic B", "created": app_api.stamp(self.b["created"]),
             "last": app_api.stamp("2026-11-03T14:01:00"), "line": "Answer B", "state": "replying",
             "waiting": "waiting for Topic A", "unread": True},
            {"id": "web-a", "title": "Topic A", "created": app_api.stamp(self.a["created"]),
             "last": app_api.stamp("2026-11-03T14:00:00"), "line": "Answer A", "state": "idle",
             "waiting": None, "unread": True}],
            # From's state is its conversation's: idle before its first message (2026-11-11)
            "from": {"line": "From a cycle", "last": app_api.stamp("2026-11-03T14:02:00"), "unread": True,
                     "state": "idle", "waiting": None}})
        rail = self.get("/app/v1/instances")
        self.assertEqual((rail["instances"][0]["threads"], rail["instances"][0]["replying"], rail["badge"]), (2, True, 1))

    def test_from_uses_the_feed_serializer_and_marks_only_from_watching(self):
        got = self.get("/app/v1/from/wisp")
        self.assertIsNone(got["chat"])
        self.assertEqual(got["items"][0]["ts"], app_api.stamp("2026-11-03T14:02:00"))
        self.assertEqual(got["items"][0]["attachments"][0]["url"], "/app/v1/file/message/note.md/0")
        self.assertNotIn("last_count", got)
        self.assertEqual(web_chat.watchers("wisp", "from"), {self.device})
        self.assertEqual(web_chat.watchers("wisp", "web-a"), set())

    def test_seen_marks_only_its_thread_and_badge_counts_instances(self):
        self.assertEqual(self.seen("web-a"), {"unread": False, "badge": 1})
        got = self.get("/app/v1/threads/wisp")
        self.assertEqual([c["unread"] for c in got["threads"]], [True, False])
        self.assertTrue(got["from"]["unread"])
        self.assertEqual(self.seen("web-b")["badge"], 1)
        self.assertEqual(self.seen("from")["badge"], 0)
        self.from_model["last_count"] += 1
        self.assertEqual(web_seen.badge(self.device), 1)

    def test_legacy_marks_merge_other_devices_into_shared_threads(self):
        mark = {"version": "old", "last": "2026-11-03T14:02:00", "last_count": 1}
        other = {"wisp": {"web-a": {"version": "other", "last": "2026-11-03T13:00:00"}}}
        web._write(web_seen.STORE, {self.device: {"wisp": mark}, "other-device": other})
        got = self.get("/app/v1/threads/wisp")
        self.assertEqual([c["unread"] for c in got["threads"]], [False, True])
        self.assertFalse(got["from"]["unread"])
        self.assertEqual(web._read(web_seen.STORE), {
            "operator": {"wisp": {"web-b": mark, "from": mark, **other["wisp"]}}})
        self.seen("web-a")
        self.assertEqual(set(web._read(web_seen.STORE)["operator"]["wisp"]), {"web-a", "web-b", "from"})
        self.assertNotIn("other-device", web._read(web_seen.STORE))

    def test_away_and_late_polls_are_scoped_to_the_thread(self):
        for cid in ("web-a", "web-b"):
            self.get(f"/app/v1/chat/wisp/{cid}?at=1000")
        self.get("/app/v1/chat/wisp/away", method="POST", data={"cid": "web-a", "at": 2000})
        self.get("/app/v1/chat/wisp/web-a?at=1500")
        self.assertEqual(web_chat.watchers("wisp", "web-a"), set())
        self.assertEqual(web_chat.watchers("wisp", "web-b"), {self.device})

    def test_reply_in_b_pushes_to_a_viewer_and_opens_b(self):
        self.get("/app/v1/chat/wisp/web-a")
        other, _ = self.pair_app("second")
        web_chat.watching("wisp", other, cid="web-b")
        relay = web_chat.Relay("web-b")
        with mock.patch.object(webpush, "notify", wraps=NOTIFY), \
                mock.patch.object(webpush, "subscriptions", return_value={self.device: {}, other: {}}), \
                mock.patch.object(webpush, "_wake"), mock.patch.object(apns, "_wake"):
            web_chat._finish(relay, "Done B", [], "test failure", 0)
        events = self.get("/app/v1/push/latest")["events"]
        self.assertEqual([(e["url"], e["cid"]) for e in events], [("/i/wisp/chat?id=web-b", "web-b")])
        self.assertNotIn(other, webpush._queue())

    def test_from_message_push_uses_from_watchers_and_routing_hook(self):
        row = {"from": "wisp", "to": "operator", "file": "one.md", "text": "Hello", "alert": "web", "talk": "talk-a"}
        web_chat.watching("wisp", self.device, cid="web-a")
        web_chat.watching("wisp", "other", cid="from")
        with mock.patch.object(messages, "appended", return_value=([(row, 1)], 1)), \
                mock.patch.object(webpush, "subscriptions", return_value={}):
            web_alerts.forward_new()
        event = webpush.notify.call_args.args[0]
        self.assertEqual((event["url"], event["cid"]), ("/i/wisp/from", "from"))
        self.assertEqual(webpush.notify.call_args.kwargs["skip"], {"other"})

    def test_latest_filters_both_url_forms_and_retains_cid_and_other_instances(self):
        events = [web_chat.reply_event(self.a), web_alerts.event({"from": "wisp", "text": "Cycle", "file": "one.md"}),
                  web_chat.reply_event({**self.b, "instance": "other"})]
        webpush._save_queue({self.device: {"pending": [{**e, "at": time.time()} for e in events]}})
        got = self.get("/app/v1/push/latest?instance=wisp")
        self.assertEqual([(e["instance"], e["cid"]) for e in got["events"]], [("wisp", "web-a"), ("wisp", "from")])
        self.assertEqual(self.get("/app/v1/push/latest?instance=wisp")["events"][0]["cid"], "from")
        self.assertEqual(self.get("/app/v1/push/latest?instance=other")["events"][0]["cid"], "web-b")

    def test_seen_rejects_foreign_or_missing_threads_and_stale_from(self):
        web_chat.save({**self.a, "id": "web-foreign", "instance": "other"})
        for cid in ("web-missing", "web-foreign"):
            self.error(self.api("/app/v1/chat/wisp/seen", "POST", {"cid": cid, "version": "x"}), 404, "not_found")
        err = self.error(self.api("/app/v1/chat/wisp/seen", "POST", {"cid": "from", "version": "old"}), 409, "stale_version")
        self.assertEqual(err["version"], self.from_model["version"])

    def test_stream_selects_the_requested_thread(self):
        relay = web_chat.Relay("web-a")
        relay.emit("answer", text="A")
        relay.close_stream("A", None)
        with mock.patch.object(web_chat, "stream_of", side_effect=lambda cid: relay if cid == "web-a" else None):
            response = self.api("/app/v1/chat/wisp/stream?cid=web-a")
            self.assertEqual(response.status, 200, response.body)
            self.assertIn(b'"text": "A"', b"".join(response.stream))
        self.assertEqual(web_chat.watchers("wisp", "web-a"), {self.device})
        self.assertEqual(web_chat.watchers("wisp", "web-b"), set())

    def test_web_fragment_watches_only_the_explicit_thread(self):
        cookie = self.paired()
        device = web.device_from_cookie(cookie)[0]
        response = self.req("/fragment/chat/wisp?id=web-a", cookie=cookie)
        self.assertEqual(response.status, 200)
        self.assertEqual(web_chat.watchers("wisp", "web-a"), {device})
        self.assertEqual(web_chat.watchers("wisp", "web-b"), set())
        self.req("/fragment/chat/wisp?id=web-a&away=1&at=2000", cookie=cookie)
        self.assertEqual(web_chat.watchers("wisp", "web-a"), set())

    def test_omitted_cid_marks_only_the_shown_chat(self):
        feed = self.get("/app/v1/chat/wisp")
        self.get("/app/v1/chat/wisp/seen", method="POST", data={"version": feed["version"]})
        self.assertEqual(set(web._read(web_seen.STORE)["operator"]["wisp"]), {"web-b"})

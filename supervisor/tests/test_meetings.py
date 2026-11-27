"""Meeting uploads through the app API and CLI, with a temporary supervisor directory."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
from email.message import Message
from unittest import mock

from supervisor import meetings, messages, web
from supervisor.tests.test_app_api import AppTest
from supervisor.tests.test_web import HOST, ME

MEETINGS = "/app/v1/meetings"
CHUNK_HEADERS = {"Content-Type": "application/octet-stream"}


class MeetingTest(AppTest):
    def create(self, **fields):
        response = self.api(MEETINGS, "POST", {
            "mode": "online", "started": "2026-01-01T12:00:00Z", "device": "example recorder", "ext": "caf",
            **fields,
        })
        self.assertEqual(response.status, 201, response.body)
        row = json.loads(response.body)
        self.assertEqual(row, {"id": row["id"], "chunk_size": meetings.CHUNK_SIZE, "received": []})
        self.assertRegex(row["id"], r"^m-[0-9]{8}-[0-9]{4}-[0-9a-f]{4}$")
        return MEETINGS + "/" + row["id"], self.sup / "meetings" / row["id"]

    def chunk(self, path, index, body):
        return self.api(f"{path}/chunk/{index}", "PUT", body=body, headers=CHUNK_HEADERS)

    def finish(self, path, body, count, **fields):
        return self.api(path + "/finish", "POST", {
            "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "chunks": count, **fields,
        })

    def assert_error(self, response, status, code, detail=None):
        self.assertEqual(response.status, status, response.body)
        error = json.loads(response.body)["error"]
        self.assertEqual(error["code"], code)
        if detail is not None:
            self.assertIn(detail, error["message"])

    def test_out_of_order_resend_and_finish(self):
        sidecar = {"channels": ["microphone", "system"], "gaps": [{"start": 13.5}]}
        path, folder = self.create(sidecar=sidecar, title="Example", account="example")
        self.assertEqual(json.loads((folder / "sidecar.json").read_bytes()), sidecar)
        first = b"abcd" * (512 * 1024)
        second = b"tail"
        self.assertEqual(json.loads(self.chunk(path, 1, b"old").body), {"received": [1]})
        self.assertEqual(json.loads(self.chunk(path, 0, first).body), {"received": [0, 1]})
        self.assertEqual(json.loads(self.chunk(path, 1, second).body), {"received": [0, 1]})
        self.assertEqual(self.get(path + "/upload"), {"received": [0, 1], "chunk_size": 8_388_608})
        response = self.finish(path, first + second, 2)
        self.assertEqual(response.status, 200, response.body)
        row = json.loads(response.body)
        self.assertEqual(row, self.get(path))
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["channels"], sidecar["channels"])
        self.assertEqual(row["account"], "example")
        self.assertEqual(row["chunks_received"], [0, 1])
        self.assertEqual(row["sha256"], hashlib.sha256(first + second).hexdigest())
        self.assertEqual((folder / "audio.caf").read_bytes(), first + second)
        self.assertFalse((folder / "upload").exists())
        self.assertEqual(self.finish(path, first + second, 2).status, 200)
        self.assert_error(self.chunk(path, 1, second), 409, "refused")

    def test_repeat_create_recovers_the_same_meeting_and_chunk_receipt(self):
        client_id = "8f5ab4c1-f51f-4f5e-bac7-5a732202c2b0"
        path, folder = self.create(client_id=client_id, title="Original")
        self.assertEqual(self.chunk(path, 0, b"audio").status, 200)
        retry = self.api(MEETINGS, "POST", {
            "mode": "online", "started": "2026-01-01T12:00:00Z", "device": "example recorder", "ext": "caf",
            "client_id": client_id.upper(), "title": "Retry",
        })
        self.assertEqual(retry.status, 201, retry.body)
        self.assertEqual(json.loads(retry.body), {
            "id": folder.name, "chunk_size": meetings.CHUNK_SIZE, "received": [0],
        })
        self.assertEqual(json.loads((folder / "meeting.json").read_bytes())["client_id"], client_id)
        self.assertEqual([(row["id"], row["title"]) for row in self.get(MEETINGS)["meetings"]],
                         [(folder.name, "Original")])

    def test_finish_mismatch_leaves_upload_resumable(self):
        path, folder = self.create()
        self.assertEqual(self.chunk(path, 1, b"b").status, 200)
        self.assert_error(self.finish(path, b"ab", 2), 409, "refused", "Chunk 0 is missing")
        self.assertEqual(self.get(path + "/upload")["received"], [1])
        self.assertEqual(self.chunk(path, 0, b"a").status, 200)
        self.assert_error(self.finish(path, b"ab", 2, size=1), 409, "refused", "Expected 1")
        self.assert_error(self.finish(path, b"ab", 2, sha256="0" * 64), 409, "refused", "SHA-256")
        self.assertEqual(self.get(path)["state"], "uploading")
        self.assertEqual(self.get(path + "/upload")["received"], [0, 1])
        self.assertFalse((folder / "audio.caf").exists())
        self.assertEqual(self.finish(path, b"ab", 2).status, 200)

    def test_many_chunks_simulate_long_recording(self):
        with mock.patch.object(meetings, "CHUNK_SIZE", 32):
            path, folder = self.create()
            parts = [index.to_bytes(32, "little") for index in range(256)]
            for index in reversed(range(len(parts))):
                response = self.chunk(path, index, parts[index])
                self.assertEqual(response.status, 200, response.body)
            self.assertEqual(self.get(path + "/upload"), {"received": list(range(256)), "chunk_size": 32})
            expected = b"".join(parts)
            response = self.finish(path, expected, len(parts))
            self.assertEqual(response.status, 200, response.body)
            self.assertEqual((folder / "audio.caf").read_bytes(), expected)

    def test_sidecar_validation_and_tailnet_bearer(self):
        self.assert_error(self.api(MEETINGS, bearer=False), 401, "unpaired")
        for sidecar, status, code in (([], 400, "bad_request"), ({"gaps": "x" * 65_536}, 413, "too_large")):
            with self.subTest(status=status):
                response = self.api(MEETINGS, "POST", {
                    "mode": "inperson", "started": "2026-01-01T12:00:00Z", "device": "example",
                    "ext": "wav", "sidecar": sidecar,
                })
                self.assert_error(response, status, code)
        self.assertEqual(self.get(MEETINGS), {"meetings": []})
        with mock.patch.object(web, "LOCAL", True):
            host = f"127.0.0.1:{web.PORT}"
            self.assertEqual(self.get(MEETINGS, login=None, host=host), {"meetings": []})
            self.assert_error(self.api(MEETINGS, login=None, host=host, bearer=False), 401, "unpaired")

    def test_list_delete_and_worker_states(self):
        first, folder = self.create()
        second, _ = self.create()
        self.assertEqual(self.finish(first, b"", 0).status, 200)
        meetings.set_state(folder.name, "processing")
        meetings.set_state(folder.name, "failed", error="Example error", processed_with={"engine": "example"})
        row = self.get(first)
        self.assertEqual((row["state"], row["error"], row["processed_with"]),
                         ("failed", "Example error", {"engine": "example"}))
        self.assertEqual([m["id"] for m in self.get(MEETINGS)["meetings"]], [second.split("/")[-1], folder.name])
        response = self.api(first, "DELETE")
        self.assertEqual(json.loads(response.body), {"id": folder.name, "deleted": True})
        self.assertFalse(folder.exists())
        self.assert_error(self.api(first), 404, "not_found")

    def test_a_meetings_own_vocabulary_is_saved_and_checked(self):
        # Review 2026-11-27: names and jargon of one meeting ("Foresight", a client's name) had nowhere to go but the
        # global SHAFT_LISTEN_VOCABULARY. The app sets them per meeting and reprocesses.
        path, folder = self.create()
        self.assertEqual(self.get(path, method="POST", data={"vocabulary": "Foresight, BPER"})["vocabulary"],
                         "Foresight, BPER")
        self.assert_error(self.api(path, "POST", {"vocabulary": "a\nb"}), 400, "bad_request")
        self.assert_error(self.api(path, "POST", {"vocabulary": "x" * 501}), 400, "bad_request")

    def test_cli_import_list_show_and_delete(self):
        from supervisor.__main__ import main

        def cli(*args):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["meetings", *args, "--json"]), 0)
            return json.loads(out.getvalue())

        source = self.sup / "example.wav"
        body = b"example audio"
        source.write_bytes(body)
        row = cli("import", str(source), "--title", "Example", "--mode", "online")
        self.assertEqual((row["title"], row["mode"], row["state"], row["account"]), ("Example", "online", "queued", None))
        self.assertEqual(row["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(cli("list"), {"meetings": [row]})
        self.assertEqual(cli("show", row["id"]), row)
        source.unlink()
        folder = self.sup / "meetings" / row["id"]
        self.assertEqual((folder / "audio.wav").read_bytes(), body)
        self.assertEqual(cli("delete", row["id"]), {"id": row["id"], "deleted": True})
        self.assertFalse(folder.exists())

    def handler(self, method, path, length, stream):
        handler = object.__new__(web.Handler)
        handler.headers = Message()
        for key, value in {
            "Host": HOST, "Tailscale-User-Login": ME, "Authorization": f"Bearer {self.device}.{self.secret}",
            "Content-Length": str(length), **CHUNK_HEADERS,
        }.items():
            handler.headers[key] = value
        handler.path = path
        handler.rfile = stream
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.close_connection = False
        getattr(handler, f"do_{method}")()
        return handler.send_response.call_args.args[0], handler.wfile.getvalue()

    def test_chunk_body_limits_and_interrupted_stream(self):
        path, folder = self.create()
        endpoint = f"{path}/chunk/0"
        unread = mock.Mock()
        for length in (8_388_609, 8_454_144, 8_454_145):
            status, body = self.handler("PUT", endpoint, length, unread)
            self.assertEqual(status, 413, body)
            self.assertEqual(json.loads(body)["error"]["code"], "too_large")
            unread.read.assert_not_called()
        self.assertEqual(list((folder / "upload").iterdir()), [])

        with mock.patch.object(web, "respond", wraps=web.respond) as respond:
            self.handler("PUT", endpoint, 8_454_144, unread)
            respond.assert_called_once()
            respond.reset_mock()
            self.handler("PUT", endpoint, 8_454_145, unread)
            respond.assert_not_called()

        data = b"a" * 8_388_608
        stream = mock.Mock(wraps=io.BytesIO(data))
        self.assertEqual(self.handler("PUT", endpoint + "?retry=1", len(data), stream), (200, b'{"received": [0]}'))
        self.assertGreater(stream.read.call_count, 1)
        self.assertLess(max(c.args[0] for c in stream.read.call_args_list), len(data))
        self.assertEqual(self.handler("PUT", endpoint, 4, io.BytesIO(b"ab"))[0], 400)
        self.assertEqual(self.get(path + "/upload")["received"], [0])
        self.assertEqual((folder / "upload" / "0").read_bytes(), data)
        self.assertEqual([p.name for p in (folder / "upload").iterdir()], ["0"])

    def test_existing_body_limits(self):
        self.assertEqual(messages.ATTACH_MAX_TOTAL + 1_000_000, 53_428_800)
        cases = (
            ("POST", MEETINGS, 1_000_000),
            ("POST", MEETINGS + "/m-20260101-1200-abcd/chunk/0", 1_000_000),
            ("PUT", MEETINGS + "/m-20260101-1200-abcd/chunk/0/extra", 1_000_000),
            ("POST", "/app/v1/chat/example/send?retry=1", 53_428_800),
            ("POST", "/chat/example/send", 53_428_800),
        )
        for method, path, limit in cases:
            with self.subTest(method=method, path=path):
                with mock.patch.object(web, "respond", return_value=web.Response(200, b"")):
                    stream = mock.Mock()
                    self.assertEqual(self.handler(method, path, limit + 1, stream)[0], 413)
                    stream.read.assert_not_called()
                    self.assertEqual(self.handler(method, path, limit, stream)[0], 200)
                    stream.read.assert_called_once_with(limit)

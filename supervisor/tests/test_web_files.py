"""#57: browse and download current instance files through the paired web routes."""
import html
import os
from urllib.parse import quote
from unittest import mock

from supervisor import core, web, web_views
from supervisor.tests.test_web import WebTest


class FilesTest(WebTest):
    def setUp(self):
        super().setUp()
        self.root = self.sup / "files-fixture"
        self.root.mkdir()
        for patch in (mock.patch.object(web, "_names", return_value=["wisp"]),
                      mock.patch.object(core, "instance_root", return_value=self.root)):
            patch.start()
            self.addCleanup(patch.stop)
        self.cookie = self.paired()

    def get(self, tab="files", path="", extra=""):
        return self.req(f"/i/wisp/{tab}?path={quote(path, safe='')}{extra}", cookie=self.cookie)

    def test_issue_57_download_bytes_and_utf8_filename(self):
        """Reverting #57 loses raw nested downloads, UTF-8 names and successful empty files."""
        (self.root / "notes" / "docs").mkdir(parents=True)
        for filename, data in (("caffè.md", b"# current\n"), ("empty.bin", b"")):
            path = "notes/docs/" + filename
            (self.root / path).write_bytes(data)
            r = self.get("file", path)
            self.assertEqual((r.status, r.body), (200, data))
            self.assertEqual(r.headers["Content-Disposition"], "attachment; filename*=UTF-8''" + quote(filename))
            self.assertEqual(r.headers["Content-Type"], "application/octet-stream")

    def test_issue_57_text_open_and_binary_download(self):
        """Reverting #57 prevents safe plain-text reading; active markup must never execute."""
        for filename, data in (("page.html", b"<script>alert(1)</script>"), ("image.png", b"\x89PNG\r\n\x1a\n")):
            (self.root / filename).write_bytes(data)
            r = self.get("file", filename, "&open=1")
            self.assertEqual((r.status, r.body), (200, data))
            self.assertEqual(r.headers["Content-Type"], "text/plain; charset=utf-8" if filename.endswith("html")
                             else "application/octet-stream")
            self.assertEqual(r.headers["Content-Security-Policy"], web.CSP)
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    def test_issue_57_exclusions_traversal_and_links(self):
        """Reverting #57 removes explicit refusals and the filtered, link-safe listing."""
        for name in ("secrets", ".git", "mail", "gdrive", ".browser"):
            # Synthetic fixtures only, never the workspace's runtime directories.
            (self.root / name).mkdir()
            (self.root / name / "private.txt").write_text("private")
        (self.root / "config.env").write_text("private")
        (self.root / "escape").symlink_to(self.sup, target_is_directory=True)
        (self.root / "visible.txt").write_text("ok")
        (self.root / "alias").symlink_to(self.root / "visible.txt")
        (self.root / "nested").mkdir()
        (self.root / "nested" / "CONFIG.ENV").write_text("private")
        for path in ("secrets/private.txt", ".git/private.txt", "mail/private.txt", "gdrive/private.txt", ".browser/private.txt",
                     "config.env", "../outside", str(self.sup), "escape/outside", "alias", "x/../visible.txt",
                     "nested/CONFIG.ENV", "SECRETS/private.txt", "MAIL/private.txt", "GDRIVE/private.txt", ".GIT/private.txt",
                     ".BROWSER/private.txt"):
            for tab in ("file", "files"):
                with self.subTest(path=path, tab=tab):
                    r = self.get(tab, path)
                    self.assertEqual(r.status, 403)
                    self.assertTrue(r.body)
        listing = self.get().body.decode()
        self.assertIn("visible.txt", listing)
        for name in ("secrets", ".git", "mail", "gdrive", ".browser", "config.env", "escape", "alias"):
            self.assertNotIn(f'>{name}', listing)
        self.assertNotIn("CONFIG.ENV", self.get(path="nested/").body.decode())

    def test_issue_57_paging_navigation_and_metadata(self):
        """Reverting #57 loses bounded listings, folder-first order and bookmarkable navigation."""
        (self.root / "notes").mkdir()
        (self.root / "notes" / "z-folder").mkdir()
        for n in range(201):
            (self.root / "notes" / f"f{n:03}.txt").write_text("abc")
        first = self.get(path="notes/")
        self.assertEqual(first.status, 200)
        text = html.unescape(first.body.decode())
        self.assertIn("202 entries", text)
        self.assertEqual(text.count('>Download</a>'), 199)
        self.assertLess(text.index('>z-folder/</a>'), text.index('>f000.txt'))
        self.assertIn(">3 B<", text)                  # shorthand, not "3 bytes"
        self.assertIn(">—<", text)                    # a folder has no size of its own to show, not its inode's 4096
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
        self.assertIn('/i/wisp/files?path=notes%2F&page=2', text)
        self.assertIn('>Up</a>', text)
        second = self.get(path="notes/", extra="&page=2").body.decode()
        self.assertEqual(second.count('>Download</a>'), 2)
        self.assertIn('>Previous</a>', second)
        self.assertIn('>Open</a>', second)

    def test_issue_57_unreadable_file_explains_refusal(self):
        """Reverting #57 loses a readable explanation when opening a file fails."""
        (self.root / "blocked.txt").write_text("x")
        with mock.patch.object(os, "open", side_effect=PermissionError(13, "Permission denied")):
            r = self.get("file", "blocked.txt")
        self.assertEqual(r.status, 403)
        self.assertIn(b"Permission denied", r.body)

    def test_issue_57_routes_keep_authentication(self):
        """Reverting #57 loses authorized access; both new routes must still reject unpaired visitors."""
        (self.root / "ok.txt").write_text("ok")
        for route in ("files", "file?path=ok.txt"):
            url = f"/i/wisp/{route}"
            self.assertEqual(self.req(url, cookie=self.cookie).status, 200)
            self.assertEqual(self.req(url).status, 403)
            self.assertEqual(self.req(url, cookie=self.cookie, login=None).status, 403)

    def test_issue_57_document_download_link(self):
        """Reverting #57 removes Download from current and archived open documents."""
        for archived in (False, True):
            text = str(web_views.docs_panel("wisp", [], "folder/a b.md", "saved", "abc", set(), archived=archived))
            self.assertIn('href="/i/wisp/file?path=notes%2Fdocs%2Ffolder%2Fa%20b.md"', text)
            self.assertIn('title="Download" aria-label="Download"><svg', text)


    # -- added after browser verification --
    def test_issue_57_a_file_over_the_cap_is_refused_in_words_not_read(self):
        """Downloads are read whole into the web service; wisp holds 100 MB+ binaries (Playwright's node). Without the
        cap one click could exhaust the service's memory."""
        (self.root / "big.bin").write_bytes(b"x" * 2048)
        with mock.patch.object(web, "FILE_MAX", 1024):
            r = self.get("file", "big.bin")
        self.assertEqual(r.status, 413)
        self.assertIn(b"copy it in a terminal", r.body)
        self.assertNotIn(b"xxxx", r.body)

    def test_issue_57_breadcrumb_is_not_a_nav_element(self):
        """Found in the browser: a <nav> breadcrumb took the sidebar's nav styles (100vh, sticky) and pushed the
        listing a screen down."""
        (self.root / "notes").mkdir()
        body = self.get("files", "notes/").body.decode()
        main = body.split("<main>", 1)[1]
        self.assertNotIn("<nav", main)
        self.assertIn('role="navigation" aria-label="File path"', main)

    def test_issue_57_a_refusal_names_no_host_path(self):
        """Found in the browser: a symlink refusal printed the instance folder's absolute path on the host."""
        (self.root / "notes").mkdir()
        os.symlink("/etc/hosts", self.root / "notes" / "escape")
        r = self.get("file", "notes/escape")
        self.assertEqual(r.status, 403)
        self.assertNotIn(str(self.root).encode(), r.body)
        self.assertNotIn(b"/etc", r.body)
        self.assertIn(b"Cannot open notes/escape:", r.body)

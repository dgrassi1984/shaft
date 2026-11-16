"""Guard: an everyday build offered itself the official download; an unsigned or mis-keyed appcast stranded installed apps."""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "apps" / "shaft"
RELEASE = APP / "Scripts" / "release-mac.sh"
PUBLISH = APP / "AppStore" / "publish-site.sh"


class AppUpdatesTest(unittest.TestCase):
    def test_only_mac_release_builds_offer_updates(self):
        spec = (APP / "project.yml").read_text()
        targets = spec.split("\ntargets:\n", 1)[1].split("\nschemes:\n", 1)[0]
        blocks = dict(re.findall(r"(?ms)^  ([\w-]+):\n(.*?)(?=^  \S|^\S|\Z)", targets))
        mac = blocks["SHAFT-macOS"]
        self.assertRegex(mac, r'(?m)^        SHAFT_UPDATE_FEED: ""$')
        self.assertRegex(mac, r"(?m)^        SHAFT_UPDATE_KEY: [A-Za-z0-9+/]+={0,2}$")
        for setting in ("SUFeedURL: $(SHAFT_UPDATE_FEED)", "SUPublicEDKey: $(SHAFT_UPDATE_KEY)",
                        "SUVerifyUpdateBeforeExtraction: true", "SURequireSignedFeed: true",
                        "SUEnableSystemProfiling: false", "SUAutomaticallyUpdate: false"):
            self.assertIn("        " + setting + "\n", mac)
        phase = mac.split("    postBuildScripts:\n", 1)[1].split("    entitlements:", 1)[0]
        self.assertIn("      - name: Drop an empty update feed\n", phase)
        self.assertEqual([name for name, block in blocks.items() if "package: Sparkle" in block], ["SHAFT-macOS"])
        self.assertEqual(spec.count("package: Sparkle"), 1)

    @unittest.skipUnless(sys.platform == "darwin", "Release helpers require macOS plutil")
    def test_release_self_test(self):
        done = subprocess.run(["bash", str(RELEASE), "--self-test"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("self-test passed.", done.stdout)

    @unittest.skipUnless(sys.platform == "darwin", "Release plan requires macOS tools")
    def test_release_plan_and_disallowed_feed(self):
        args = ["bash", str(RELEASE), "--dry-run", "--allow-untagged", "--notary-profile", "x"]
        done = subprocess.run(args, capture_output=True, text=True)
        archive_lines = [line for line in done.stdout.splitlines() if "xcodebuild -project" in line]
        self.assertTrue(archive_lines, done.stdout + done.stderr)
        archive = archive_lines[0]
        self.assertIn("SHAFT_UPDATE_FEED=https://dgrassi1984.github.io/shaft-app/appcast.xml", archive)
        done = subprocess.run([*args, "--feed-url", "http://example.com/a.xml"], capture_output=True, text=True)
        self.assertIn("PRECONDITION FAILED: --feed-url is not an allowed URL: 'http://example.com/a.xml'", done.stderr)

    def test_publish_preserves_signed_feed_bytes_and_substitutes_pages(self):
        with tempfile.TemporaryDirectory(prefix="shaft-app-updates-") as directory:
            root = Path(directory)
            repo = root / "site"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
            feed = root / "feed.xml"
            args = ["bash", str(PUBLISH), "--email", "support@example.invalid", "--to", str(repo),
                    "--appcast", str(feed)]
            feed.write_bytes(b"<rss/>\n")
            done = subprocess.run(args, capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("not a signed appcast: run release-mac.sh", done.stderr)
            self.assertFalse((repo / "shaft-app" / "appcast.xml").exists())
            content = b'<!-- warning -->\r\n<rss>SUPPORT_EMAIL &amp; untouched</rss>\r\n<!-- sparkle-signatures: synthetic -->\r\n'
            feed.write_bytes(content)
            done = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertEqual((repo / "shaft-app" / "appcast.xml").read_bytes(), content)
            pages = list((APP / "AppStore" / "site").glob("*.html"))
            self.assertTrue(pages)
            for page in pages:
                self.assertEqual((repo / "shaft-app" / page.name).read_text(),
                                 page.read_text().replace("SUPPORT_EMAIL", "support@example.invalid"))
            self.assertEqual((repo / "shaft-app" / "style.css").read_bytes(),
                             (APP / "AppStore" / "site" / "style.css").read_bytes())
            self.assertIn("https://<owner>.github.io/shaft-app/appcast.xml", done.stdout)

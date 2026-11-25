"""Guard: an everyday build offered itself the official download; an unsigned or mis-keyed appcast stranded installed apps."""
from __future__ import annotations

import re
import shlex
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

    def test_signer_checks_read_the_authority_lines(self):
        """Bug 2026-11-16: the release read the signer with `codesign -dv`, which prints no Authority= lines, so it
        died with "not signed with Developer ID Application" on every correctly signed app."""
        script = RELEASE.read_text()
        self.assertIn("Authority=Developer ID Application", script)
        self.assertNotRegex(script, r"codesign -dv[^v]", "use codesign -dvv: -dv does not print the signing authority")

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
        # The safety steps must stay, in this order: the archived key checked against the signing key, the DMG
        # stapled before its EdDSA signature (stapling changes its bytes), then the feed written, signed and verified.
        plan = done.stdout
        steps = ["keys_match signing and archived public keys", "stapler staple", "sign_update " ,
                 "sign_update --verify ", "appcast_xml ", "/sign_update " + str(APP / "build/release/appcast.xml"),
                 "sign_update --verify " + str(APP / "build/release/appcast.xml")]
        at = -1
        for step in steps:
            found = plan.find(step, at + 1)
            self.assertGreater(found, at, f"release plan lost or reordered: {step!r}\n{plan}")
            at = found
        done = subprocess.run([*args, "--feed-url", "http://example.com/a.xml"], capture_output=True, text=True)
        self.assertIn("PRECONDITION FAILED: --feed-url is not an allowed URL: 'http://example.com/a.xml'", done.stderr)

    @unittest.skipUnless(sys.platform == "darwin", "Release plan requires macOS tools")
    def test_release_plan_notary_credentials(self):
        # A stored notary profile is unreadable while the Mac's screen is locked.
        args = ["bash", str(RELEASE), "--dry-run", "--allow-untagged",
                "--download-base", "https://example.com/"]
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "AuthKey_KEYID.p8"
            key_path.touch()
            key = ["--api-key-path", str(key_path), "--api-key-id", "KEYID", "--api-issuer", "ISSUER"]
            done = subprocess.run([*args, *key], capture_output=True, text=True)
            for operation in ("history", "submit", "log"):
                with self.subTest(operation=operation):
                    lines = [line for line in done.stdout.splitlines() if f"xcrun notarytool {operation} " in line]
                    self.assertEqual(len(lines), 1, done.stdout + done.stderr)
                    command = shlex.split(lines[0])
                    start = command.index("--key")
                    self.assertEqual(command[start:start + 6],
                                     ["--key", str(key_path), "--key-id", "KEYID", "--issuer", "ISSUER"])
                    self.assertNotIn("--keychain-profile", lines[0])
            cases = (
                (["--notary-profile", "x", *key], "pass either --notary-profile or the API key, not both"),
                (key[:-2], "Pass --api-key-path, --api-key-id and --api-issuer together."),
                ([], "Pass --notary-profile NAME or --api-key-path PATH --api-key-id ID --api-issuer ID."),
                (["--api-key-path", directory, *key[2:]], "API key path is not a file."),
            )
            for credentials, message in cases:
                with self.subTest(message=message):
                    done = subprocess.run([*args, *credentials], capture_output=True, text=True)
                    self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                    self.assertIn(f"PRECONDITION FAILED: {message}", done.stderr.splitlines())

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

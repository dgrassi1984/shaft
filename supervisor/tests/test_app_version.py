"""Regression: apps shipped as 0.1.0 forever instead of using the git release tag."""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "apps" / "shaft"
SCRIPT = APP / "Scripts" / "stamp-version.sh"


class AppVersionTest(unittest.TestCase):
    def test_all_bundle_targets_stamp_versions_without_literal_settings(self):
        spec = (APP / "project.yml").read_text()
        for value in re.findall(r"(?m)^[ \t]*(?:MARKETING_VERSION|CURRENT_PROJECT_VERSION):[ \t]*(.*)$", spec):
            self.assertRegex(value.strip().strip("\"'"), r"^\$\([^)]+\)$", "Version settings must not be literals")
        for target in ("SHAFT-iOS", "SHAFT-macOS", "NotificationService-iOS", "NotificationService-macOS"):
            with self.subTest(target=target):
                block = re.search(rf"(?ms)^  {re.escape(target)}:\n(.*?)(?=^  \S|^\S|\Z)", spec)[1]
                phase = block.split("    postBuildScripts:\n", 1)[1].split("    entitlements:", 1)[0]
                self.assertIn('"$SRCROOT/Scripts/stamp-version.sh"', phase)
                stamp = phase.split("      - name: Stamp git version\n", 1)[1]
                self.assertIn("basedOnDependencyAnalysis: false", stamp)
                self.assertIn("$(TARGET_BUILD_DIR)/$(INFOPLIST_PATH)", stamp)
                self.assertIn("outputFiles: [$(DERIVED_FILE_DIR)/stamp-version.stamp]", stamp)
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    @unittest.skipUnless(sys.platform == "darwin", "Version stamping requires macOS PlistBuddy")
    def test_processed_plist_uses_git_versions(self):
        """Apps shipped as 0.1.0 forever; every build must overwrite processed bundle versions.

        On a throwaway repository of its own (one commit, tagged v1.2.3): the gate runs the suite on a copy of the
        commit with no tags, so the checkout's own tags cannot be relied on."""
        with tempfile.TemporaryDirectory(prefix="shaft-app-version-") as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            def git(*args):
                subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
                               check=True, capture_output=True, text=True)
            git("init", "-q")
            (repo / "file").write_text("one\n")
            git("add", "file")
            git("commit", "-q", "-m", "one")
            git("tag", "-a", "v1.2.3", "-m", "v1.2.3")
            plist = root / "Info.plist"
            env = dict(os.environ, SRCROOT=str(repo), TARGET_BUILD_DIR=directory,
                       INFOPLIST_PATH="Info.plist", DERIVED_FILE_DIR=directory)
            env.pop("GIT_DIR", None)
            env.pop("GIT_WORK_TREE", None)

            def stamp(initial: dict) -> dict:
                with plist.open("wb") as stream:
                    plistlib.dump({**initial, "Unrelated": "kept"}, stream)
                subprocess.run([str(SCRIPT)], env=env, check=True, capture_output=True, text=True)
                with plist.open("rb") as stream:
                    return plistlib.load(stream)

            # Exercise both Add on absent keys and Set on keys from an earlier build.
            for initial in ({}, {"CFBundleShortVersionString": "0.1.0", "CFBundleVersion": "1",
                                 "SHAFTVersion": "stale"}):
                info = stamp(initial)
                self.assertEqual(info["CFBundleShortVersionString"], "1.2.3")
                self.assertEqual(info["CFBundleVersion"], "101")            # one commit + 100
                self.assertEqual(info["SHAFTVersion"], "v1.2.3")
                self.assertEqual(info["Unrelated"], "kept")
                self.assertTrue((root / "stamp-version.stamp").is_file())

            (repo / "file").write_text("two\n")
            self.assertEqual(stamp({})["SHAFTVersion"], "v1.2.3+changes")

            git("tag", "-d", "v1.2.3")
            with plist.open("wb") as stream:
                plistlib.dump({}, stream)
            done = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0, "a build with no release tag must fail, not ship 0.1.0")
            self.assertIn("No reachable v[0-9]* git tag", done.stderr)


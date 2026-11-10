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
        """Apps shipped as 0.1.0 forever; every build must overwrite processed bundle versions."""
        def git(*args):
            return subprocess.check_output(["git", "-C", str(APP), *args], text=True).strip()

        version = git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*")[1:]
        build = str(int(git("rev-list", "--count", "HEAD")) + 100)
        label = git("describe", "--tags", "--always", "HEAD")
        if git("diff", "--name-only", "HEAD", "--"):
            label += "+changes"
        with tempfile.TemporaryDirectory(prefix="shaft-app-version-") as directory:
            plist = Path(directory) / "Info.plist"
            env = dict(os.environ, SRCROOT=str(APP), TARGET_BUILD_DIR=directory,
                       INFOPLIST_PATH="Info.plist", DERIVED_FILE_DIR=directory)
            # Exercise both Add on absent keys and Set on keys from an earlier build.
            for initial in ({}, {"CFBundleShortVersionString": "0.1.0", "CFBundleVersion": "1",
                                 "SHAFTVersion": "stale"}):
                with plist.open("wb") as stream:
                    plistlib.dump({**initial, "Unrelated": "kept"}, stream)
                subprocess.run([str(SCRIPT)], env=env, check=True, capture_output=True, text=True)
                with plist.open("rb") as stream:
                    info = plistlib.load(stream)
                self.assertEqual(info["CFBundleShortVersionString"], version)
                self.assertEqual(info["CFBundleVersion"], build)
                self.assertEqual(info["SHAFTVersion"], label)
                self.assertTrue(info["SHAFTVersion"].startswith("v"))
                self.assertEqual(info["Unrelated"], "kept")
                self.assertTrue((Path(directory) / "stamp-version.stamp").is_file())

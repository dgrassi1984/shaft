"""Regression: the TestFlight upload was refused because the watch app had no icon (no CFBundleIconName).

Every application target in apps/shaft/project.yml must compile an asset catalog, name its AppIcon set, and that set
must hold an image for the target's platform. A watchOS icon must be opaque: App Store Connect refuses one with alpha.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "apps" / "shaft"
ICONSET = APP / "Shared" / "Assets.xcassets" / "AppIcon.appiconset"


def _targets(spec: str) -> dict[str, str]:
    targets = spec.split("\ntargets:\n", 1)[1].split("\nschemes:\n", 1)[0]
    return {m[1]: m[2] for m in re.finditer(r"(?ms)^  ([A-Za-z][\w-]*):\n(.*?)(?=^  \S|\Z)", targets)}


class AppIconsTest(unittest.TestCase):
    def test_every_application_target_has_an_icon_for_its_platform(self):
        spec = (APP / "project.yml").read_text()
        images = json.loads((ICONSET / "Contents.json").read_text())["images"]
        apps = {name: block for name, block in _targets(spec).items() if "    type: application\n" in block}
        self.assertEqual(set(apps), {"SHAFT-iOS", "SHAFT-macOS", "SHAFT-watchOS"})
        for name, block in apps.items():
            with self.subTest(target=name):
                platform = re.search(r"(?m)^    platform: (\w+)$", block)[1]
                # The whole Shared folder (iOS, macOS) or just its asset catalog (watchOS) is a source.
                self.assertRegex(block, r"(?m)^      - path: Shared(/Assets\.xcassets)?$")
                self.assertIn("ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon", block)
                if platform == "macOS":
                    found = [i for i in images if i.get("idiom") == "mac"]
                else:
                    found = [i for i in images if i.get("platform") == platform.lower()]
                self.assertTrue(found, f"AppIcon has no image for {platform}")
                for image in found:
                    self.assertTrue((ICONSET / image["filename"]).is_file(), image["filename"])

    @unittest.skipUnless(sys.platform == "darwin", "sips is macOS only")
    def test_watch_icon_is_opaque(self):
        images = json.loads((ICONSET / "Contents.json").read_text())["images"]
        for image in (i for i in images if i.get("platform") == "watchos"):
            out = subprocess.run(["sips", "-g", "hasAlpha", str(ICONSET / image["filename"])],
                                 check=True, capture_output=True, text=True).stdout
            self.assertIn("hasAlpha: no", out, image["filename"])


if __name__ == "__main__":
    unittest.main()

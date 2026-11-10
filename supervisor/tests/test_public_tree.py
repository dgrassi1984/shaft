"""The published tree describes SHAFT, not one operator's use of it. Bug 2026-11-10: the public docs and code named the
maintainer's live instances, cited design plans that recorded their deployment, and carried his workflow bindings.
The plans and bake-off results now live in the gitignored private/ folder; this keeps them from coming back."""
from __future__ import annotations

import hashlib
import re
import subprocess
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
# sha256 prefixes of live instance names, so this file does not publish them itself
LIVE_NAMES = {"d3030abdf1f07f95", "b1e3c2ec1a80a8cc", "f59c0081df305093", "26690867e270987f"}
TEXT = {".md", ".py", ".swift", ".js", ".css", ".html", ".json", ".yml", ".yaml", ".cff", ".txt", ".sh", ".toml",
        ".drive", ".plist", ".entitlements", ".xcconfig", ""}
PLANS_DIR = "docs/" + "plans"


def tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=PROJECT, capture_output=True, check=True).stdout.decode()
    return [n for n in out.split("\0") if n and not n.startswith(("template/", "instances/"))]


class PublicTreeTest(unittest.TestCase):
    def setUp(self):
        self.files = tracked()

    def test_no_private_records_are_tracked(self):
        for prefix in (PLANS_DIR + "/", "private/", "agent-overlay.yaml"):
            self.assertEqual([n for n in self.files if n.startswith(prefix)], [], prefix)

    def test_no_live_instance_name_and_no_plan_citation(self):
        token = re.compile(r"[a-z0-9]+")
        found = []
        for name in self.files:
            path = PROJECT / name
            if Path(name).suffix not in TEXT or not path.is_file():
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            if PLANS_DIR + "/PLAN" in text:
                found.append(f"{name}: cites {PLANS_DIR}/")
            for word in set(token.findall(text.lower())):
                if hashlib.sha256(word.encode()).hexdigest()[:16] in LIVE_NAMES:
                    found.append(f"{name}: a live instance's name")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()

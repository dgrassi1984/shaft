"""PLAN-pwa P0: the web UI on a phone — fields iOS does not zoom into, finger-sized targets, nothing behind hover,
the phone layout held sideways too, and Return as a new line where there is no Shift key."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from supervisor import web_views

STATIC = Path(web_views.__file__).parent / "web_static"
CSS = (STATIC / "web.css").read_text()
JS = (STATIC / "web.js").read_text()


def block(query: str) -> str:
    """Every rule inside the @media blocks with exactly this query."""
    out, at = [], 0
    while (i := CSS.find(f"@media {query} {{", at)) >= 0:
        depth, j = 0, CSS.index("{", i)
        for k in range(j, len(CSS)):
            depth += CSS[k] == "{"
            depth -= CSS[k] == "}"
            if depth == 0:
                out.append(CSS[j + 1:k])
                at = k
                break
    return "\n".join(out)


class TouchTest(unittest.TestCase):
    def test_no_field_ios_zooms_into(self):
        """iOS Safari zooms the page into any field under 16 px when it is tapped (the composer was 15 px)."""
        coarse = block("(pointer: coarse)")
        for sel in ("input", "select", "textarea", "form.composer textarea", "form.docedit textarea"):
            self.assertRegex(coarse, rf"(^|[\s,]){re.escape(sel)}[\s,][^{{]*\{{[^}}]*font-size: 16px", sel)
        self.assertIn("text-size-adjust: 100%", CSS)

    def test_finger_sized_targets(self):
        coarse = block("(pointer: coarse)")
        self.assertRegex(coarse, r"button, button\.small, button\.act[^{]*\{[^}]*min-height: 44px")
        self.assertRegex(coarse, r"\.chip \.unpick \{[^}]*min-width: 44px; min-height: 44px")

    def test_nothing_waits_for_a_hover(self):
        self.assertRegex(block("(hover: none)"), r"\.turn \.who \.copy, \.turn\.it pre > \.copy, \.docview pre > \.copy "
                                                 r"\{ opacity: 1; \}")

    def test_the_phone_layout_holds_sideways(self):
        """A phone held sideways is wider than 720 px: it got the desktop's three columns."""
        self.assertNotIn("@media (max-width: 720px) {", CSS)
        self.assertGreaterEqual(CSS.count("@media (max-width: 720px), (pointer: coarse) and (max-height: 520px)"), 4)

    def test_the_menu_stays_on_screen(self):
        phone = block("(max-width: 720px), (pointer: coarse) and (max-height: 520px)")
        self.assertRegex(phone, r"\.menubody \{ position: fixed; left: \.5rem; right: \.5rem;")

    def test_return_is_a_new_line_where_there_is_no_shift_key(self):
        self.assertIn('window.matchMedia("(hover: none) and (pointer: coarse)")', JS)
        self.assertRegex(JS, r'ev\.key === "Enter" && !ev\.shiftKey && !ev\.isComposing\s*&& !touchOnly\(\)')
        composer = str(web_views.chat_page("wren", None, [], [], {}, ["wren"], "t"))
        self.assertIn('placeholder="Message wren"', composer)              # no "Enter sends" in the field itself
        self.assertIn('<span class="dim keys">Enter sends · Shift-Enter new line', composer)


if __name__ == "__main__":
    unittest.main()


class PhoneLayoutTest(unittest.TestCase):
    """PLAN-pwa P2: on a phone the chat is the whole screen."""
    PHONE = "(max-width: 720px), (pointer: coarse) and (max-height: 520px)"

    def test_the_page_is_as_tall_as_the_keyboard_leaves(self):
        phone = block(self.PHONE)
        self.assertIn("body:has(.chatgrid) { height: var(--vvh, 100dvh); overflow: hidden; }", phone)
        self.assertIn('style.setProperty("--vvh", `${Math.round(vv.height)}px`)', JS)
        self.assertIn('window.visualViewport.addEventListener("resize", fitViewport)', JS)
        self.assertNotIn("100dvh - 19rem", CSS)                    # the guess the feed was sized by

    def test_the_rail_and_the_documents_are_sheets(self):
        phone = block(self.PHONE)
        self.assertIn("body:has(.chatgrid) nav { display: none; }", phone)
        self.assertRegex(phone, r"body\.navopen:has\(\.chatgrid\) nav \{ display: flex;[^}]*position: fixed; inset: 0;")
        page = str(web_views.chat_page("wren", None, [], [], {}, ["wren"], "t"))
        self.assertIn('class="navbtn" data-toggle-nav aria-label="Chats and pages"', page)
        self.assertIn("data-close-docs", str(web_views.docs_panel("wren", [], "", None, "none", set())))
        self.assertIn('class="small closenav" data-toggle-nav aria-label="Close the chats"', page)  # no Escape key
        self.assertIn('if (grid) grid.classList.add("showdocs");', JS)   # a card opens the sheet

    def test_the_header_is_one_line(self):
        phone = block(self.PHONE)
        self.assertIn(".chathead { flex-wrap: nowrap;", phone)
        self.assertIn(".chathead .facts, .docslabel { display: none; }", phone)
        page = str(web_views.chat_page("wren", None, [], [], {"model": "m", "state": "idle"}, ["wren"], "t"))
        self.assertIn('<p class="dim menufacts">m · idle</p>', page)        # the facts move into ⋯

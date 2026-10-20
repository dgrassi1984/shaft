"""docs/plans/PLAN-pwa P0: the web UI on a phone — fields iOS does not zoom into, finger-sized targets, nothing behind hover,
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
    """docs/plans/PLAN-pwa P2: on a phone the chat is the whole screen."""
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
        self.assertIn("showDocs(true);", JS)                             # a card opens the sheet

    def test_the_header_is_one_line(self):
        phone = block(self.PHONE)
        self.assertIn(".chathead { flex-wrap: nowrap;", phone)
        self.assertIn(".chathead .facts, .docslabel { display: none; }", phone)
        page = str(web_views.chat_page("wren", None, [], [], {"model": "m", "state": "idle"}, ["wren"], "t"))
        self.assertIn('<p class="dim menufacts">m · idle</p>', page)        # the facts move into ⋯


class DocumentsCloseTest(unittest.TestCase):
    """Bug (2026-10-20): beside the chat the Documents panel could not be closed — once an instance had a document,
    .chatgrid:has(.doclist a) held it open, 📄 only toggled .showdocs (no effect then) and ✕ was hidden off a phone."""

    def desktop(self) -> str:
        return re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", CSS)    # the rules outside any @media

    def test_closed_beats_the_documents_holding_it_open(self):
        css = self.desktop()
        self.assertIn(".chatgrid.hidedocs > aside.docs { display: none; }", css)
        self.assertIn(".chatgrid.hidedocs { grid-template-columns: minmax(0, 1fr); }", css)
        # same specificity as the rule holding it open: it must come after it to win
        self.assertGreater(css.index(".chatgrid.hidedocs {"), css.index(".chatgrid:has(.docview), .chatgrid:has(.doclist a)"))

    def test_the_close_button_shows_beside_the_chat(self):
        hidden = re.search(r"^([^{\n]*)\{ display: none; \}", self.desktop().split(".navbtn.news")[0].split("PLAN-pwa P2:")[1],
                           re.M)
        self.assertNotIn("closedocs", hidden.group(1))
        self.assertIn("data-close-docs", str(web_views.docs_panel("wren", [{"path": "plan.md", "bytes": 3}], "", None,
                                                                    "none", set())))

    def test_documents_and_close_really_close(self):
        self.assertNotIn('grid.classList.toggle("showdocs");', JS)       # the toggle that could not close it
        self.assertIn("showDocs(!docsOn())", JS)                          # 📄 closes what is on screen
        self.assertRegex(JS, r'matches\("\[data-close-docs\]"\)\) \{\s*showDocs\(false\);')
        self.assertIn('grid.classList.toggle("hidedocs", !on);', JS)
        self.assertIn('localStorage.getItem("shaft.docs.closed")', JS)  # closed stays closed on the next page

    def test_the_documents_button_says_whether_the_panel_is_on_screen(self):
        """Found while checking the fix: Escape closed the phone's sheet and left 📄 saying aria-expanded="true"."""
        self.assertRegex(JS, r'grid\.classList\.remove\("showdocs"\); docsAria\(\);')       # Escape
        self.assertRegex(JS, r'"shaft:swapped", \(ev\) => \{[^}]*ev\.target\.id === "docs"\) docsAria\(\);')


class NetworkClientTest(unittest.TestCase):
    """docs/plans/PLAN-pwa P3, in the page: what it sends and what it does when the network fails."""

    def test_a_refresh_asks_for_what_changed_and_gives_up_in_time(self):
        self.assertIn('{ "If-None-Match": el.dataset.etag }', JS)
        self.assertIn("if (r.status === 304) { fails = 0; fresh(el); return; }", JS)
        self.assertIn("const POLL_LIMIT_MS = 15000;", JS)
        self.assertIn("signal: ctl.signal", JS)

    def test_a_failure_backs_off_is_said_once_and_ends_when_the_network_returns(self):
        self.assertIn("later(Math.min(every * slow * 2 ** Math.min(fails, 4), 15000 * slow));", JS)
        self.assertIn('netNote(navigator.onLine === false ? "Offline', JS)
        self.assertIn('window.addEventListener("online", () => { fails = 0; tick(); });', JS)
        self.assertIn("if (r.status >= 502 && r.status <= 504) {", JS)     # Tailscale's 503: unreachable, said once
        self.assertIn('window.addEventListener("pageshow", () => tick());', JS)

    def test_a_send_carries_its_id_and_is_checked_before_uploading(self):
        self.assertIn("if (mid && !mid.value) mid.value = crypto.randomUUID", JS)
        self.assertIn("if (r.ok && mid) mid.value = \"\";", JS)
        self.assertIn("const tooBig = filesRefused(form);", JS)

    def test_the_feed_is_updated_item_by_item(self):
        self.assertIn("if (el.dataset.keyed) morph(el, body);", JS)
        self.assertIn("if (cur && cur.dataset.k && cur.dataset.k === n.dataset.k && cur._src === src) return;", JS)


class AccessibilityTest(unittest.TestCase):
    """docs/plans/PLAN-pwa P5."""

    def test_a_new_answer_and_every_toast_are_read_out(self):
        self.assertIn('region("toast", "status");', JS)                    # empty from the start: then it speaks
        self.assertIn('region("announce", "status");', JS)
        self.assertIn('box.setAttribute("role", bad ? "alert" : "status");', JS)
        self.assertIn('announce(`${who ? who.textContent.trim() : "It"} replied:', JS)
        self.assertIn('note.setAttribute("role", "status");', JS)            # a panel not refreshed

    def test_icon_only_controls_and_state_dots_have_names(self):
        from supervisor import web_pages
        page = str(web_views.chat_page("wren", None, [], [], {"model": "m", "state": "idle"}, ["wren"], "t"))
        for name in ('aria-label="Chats and pages"', 'aria-label="Documents"', 'aria-label="wren: pages and actions"',
                     'aria-label="Message wren"'):
            self.assertIn(name, page)
        rail = str(web_pages.rail(["wren"], "", {"instances": [{"name": "wren", "state": "in a cycle"}]}))
        self.assertIn('role="img" aria-label="in a cycle"', rail)
        self.assertIn('x.setAttribute("aria-label", `remove ${f.name}`);', JS)

    def test_the_keyboard_reaches_attach_and_reply(self):
        self.assertIn("label.pick input { position: absolute; width: 1px; height: 1px; opacity: 0; }", CSS)
        self.assertNotIn("label.pick input { display: none; }", CSS)
        self.assertIn('b.addEventListener("click", () => quoteSelection());', JS)

    def test_text_follows_the_phones_text_size_and_motion_can_be_off(self):
        coarse = block("(pointer: coarse)")
        self.assertIn("@supports (font: -apple-system-body) { html { font: -apple-system-body; } }", coarse)
        self.assertIn("font-size: max(16px, 1rem)", coarse)
        self.assertIn("body { font: 1rem/1.45", CSS)                         # rem: it follows the root's size
        self.assertIn("transition: none !important", block("(prefers-reduced-motion: reduce)"))

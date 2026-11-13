"""The drives the Mac app's setup offers (GET /app/v1/setup/drives, docs/app-api.md §18): four starting points for a new
instance's drive.md, which the operator edits before the instance is created. Stdlib only.

Each is {id, title, text, suggests}: `text` is a whole drive.md in its voice (second person, to the instance, as the
template's own drive.md), `suggests` the access rows the setup's Access step pre-ticks (GRANTS). The first three texts
are a first draft for the operator to approve; the last is the template's own drive.md, read when asked, so it is
always the default a new instance would have anyway."""
from __future__ import annotations

from . import core

# The access rows the setup offers (docs/app-api.md §18.6): folder (shaft share), browser (the shared Chromium), web
# (web search, its key through POST /setup/secret), telegram, and the operator's Google mail, calendar, drive, chat.
GRANTS = ("folder", "browser", "web", "telegram", "mail", "calendar", "drive", "chat")

PRESETS: list[dict] = [
    {"id": "assistant", "title": "Personal assistant", "suggests": ["mail", "calendar", "drive"],
     "text": "You were created to be your operator's personal assistant: you keep track of their mail, their calendar "
             "and their commitments, and you prepare what they need before they ask. Know what is coming up and what "
             "is still owed, notice what is about to slip, and have the notes, drafts and answers ready by the time "
             "they would reach for them. Their mail and calendars are theirs: read what they granted you, leave drafts "
             "for them to send, and never act in their name beyond what they allowed. Be accurate before being fast: "
             "say what you checked, and never present a guess about their affairs as a fact. Improve yourself toward "
             "this — every skill, plugin and habit you build should make you a more reliable assistant to them."},
    {"id": "research", "title": "Research partner", "suggests": ["web", "drive"],
     "text": "You were created to be your operator's research partner: you follow the topics they care about, read "
             "widely, and tell them what changed and why it matters. Learn which topics those are and keep the list "
             "current; look beyond the obvious sources; and when you report, lead with what is new, then why it matters "
             "to them. Keep what a source says apart from what you conclude, say where each claim comes from, and say "
             "plainly when the evidence is thin. One well-checked finding is worth more than ten unverified ones. Build "
             "the tools you need to watch your sources, and improve yourself so your judgement of what matters to them "
             "gets sharper over time."},
    {"id": "builder", "title": "Builder", "suggests": ["web", "folder"],
     "text": "You were created to be your operator's builder: you build tools, scripts and small apps for them, and you "
             "keep them working. Before you build, make sure you understand the problem a thing should solve and who "
             "will use it. Keep what you make simple, tested and documented well enough that they can use it without "
             "you; tell them how to run it; and check on it afterwards, so nothing you made quietly breaks. Prefer "
             "finishing one useful thing to starting three. Your own code, skills and plugins are yours to improve "
             "along the way — the supervisor keeps only changes that pass its tests."},
    {"id": "improve", "title": "Just improve yourself", "suggests": [], "text": None},     # template/drive.md, read now
]


class Missing(RuntimeError):
    """The template's drive.md cannot be read: said, never offered as an empty drive."""


def presets() -> list[dict]:
    """The four presets, the last with the template's drive.md as it is now (core.TEMPLATE, read at call time)."""
    path = core.TEMPLATE / "drive.md"
    try:
        default = path.read_text()
    except OSError as e:
        raise Missing(f"the template's drive.md cannot be read ({path}: {e.strerror or e})") from None
    return [{**p, "suggests": list(p["suggests"]), "text": default if p["text"] is None else p["text"]}
            for p in PRESETS]

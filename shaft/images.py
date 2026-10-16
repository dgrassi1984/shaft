"""Images I can see (PLAN-vision.md), when my model accepts them — the supervisor says so with SHAFT_VISION=1.

- A photo Daniele attaches (on Telegram, it is saved under attachments/ and listed in his message) goes to my model
  with the message it came with: `with_attached`.
- view_image(path) shows me an image in my folder: the tool answers in text, and the image follows in a user message
  right after the tool results (`image_message`): a tool result cannot carry an image in the OpenAI format.
- Only the newest MAX_SHOWN images stay in what I send; older ones become a line naming the file (`keep_newest`), so
  a long conversation does not resend every photo on every turn.
- On a model that cannot see images, the attachment gets a note saying so: I am never left believing I saw it.

Image bytes live only in the request sent to the model: never in episodes, the journal, logs or transcripts.
"""
from __future__ import annotations

import base64
import os
import re
from pathlib import Path

MAX_SHOWN = 4                   # the supervisor refuses more than this per call (SHAFT_MAX_IMAGES)
MAX_BYTES = 30 * 1024 ** 2      # the supervisor downscales anything big; this only stops a runaway read
TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}
LISTED = re.compile(r"^- (\S+\.(?:jpe?g|png|gif|webp)) \(", re.I | re.M)   # an attachment line: "- <path> (<size>)"
NO_VISION = ("\n[My model cannot see images, so I have not seen {what}. describe_image can tell me what is in "
             "{them}.]")


def vision() -> bool:
    return os.environ.get("SHAFT_VISION") == "1"


def is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in TYPES


def part(path: Path) -> dict:
    """One image as an OpenAI content part (a data: URL); the supervisor checks and prepares it."""
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise ValueError(f"{path.name} is larger than {MAX_BYTES // 1024 ** 2} MB")
    media = TYPES.get(path.suffix.lower(), "image/jpeg")
    return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{base64.b64encode(data).decode()}"},
            "shaft_path": str(path)}


def attached(text: str, root: Path) -> list[Path]:
    """The image files a message lists as attachments, inside my folder (anything else is not mine to read)."""
    root = Path(root).resolve()
    out = []
    for rel in LISTED.findall(text or ""):
        path = (root / rel).resolve()
        if root in path.parents and path.is_file() and path not in out:
            out.append(path)
    return out


def with_attached(text: str, root: Path, listed_in: str | None = None):
    """A user message's content: with its attached images when my model sees them (the newest MAX_SHOWN), else the
    text with a note that I have not seen them. A message with no image attached is returned as it is. Attachments
    are looked for in `listed_in` (default: the text): only the message itself — never memory recalled into it, which
    can quote old attachment lines."""
    found = attached(text if listed_in is None else listed_in, root)
    if not found:
        return text
    if not vision():
        many = len(found) > 1
        return text + NO_VISION.format(what="these images" if many else "this image", them="them" if many else "it")
    parts = []
    for path in found[-MAX_SHOWN:]:
        try:
            parts.append(part(path))
        except (OSError, ValueError) as e:
            text += f"\n[{path.name} could not be shown: {e}]"
    return [{"type": "text", "text": text}, *parts] if parts else text


def image_message(paths: list[Path]) -> dict:
    """The user message that carries the images view_image showed, right after the tool results."""
    names = ", ".join(p.name for p in paths)
    return {"role": "user", "content": [{"type": "text", "text": f"[view_image: {names}]"},
                                        *(part(p) for p in paths)]}


def text_of(content) -> str:
    """A message's content as text, images named rather than carried (for anything stored, counted or shown)."""
    if not isinstance(content, list):
        return content or ""
    return "".join(p.get("text", "") if p.get("type") == "text" else f"[image: {Path(p.get('shaft_path', '')).name}]"
                   for p in content if isinstance(p, dict))


def keep_newest(messages: list[dict]) -> None:
    """Keep only the newest MAX_SHOWN images in `messages` (in place); each older one becomes a line naming its file,
    which view_image can show again."""
    seen = 0
    for m in reversed(messages):
        content = m.get("content")
        if not isinstance(content, list):
            continue
        kept = []
        for p in reversed(content):
            if isinstance(p, dict) and p.get("type") == "image_url":
                seen += 1
                if seen > MAX_SHOWN:
                    name = Path(p.get("shaft_path", "image")).name
                    p = {"type": "text", "text": f"\n[image shown earlier, no longer in view: {name}]"}
            kept.append(p)
        m["content"] = list(reversed(kept))


def for_model(messages: list[dict]) -> list[dict]:
    """The messages as sent: image parts without SHAFT's own key (shaft_path), which no provider expects."""
    out = []
    for m in messages:
        if isinstance(m.get("content"), list):
            m = {**m, "content": [{k: v for k, v in p.items() if k != "shaft_path"} if isinstance(p, dict) else p
                                  for p in m["content"]]}
        out.append(m)
    return out

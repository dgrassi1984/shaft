"""Images for instances (docs/plans/PLAN-vision.md): which models see them, and every image prepared in one place.

An image travels in the OpenAI chat format, as a user message part {"type": "image_url", "image_url": {"url":
"data:<type>;base64,<data>"}}. The broker's /llm proxy runs `prepare` on every request before any provider sees it:
  - a model that cannot see images gets a refusal naming the model, never an image silently dropped;
  - only data: URLs (an instance never makes a provider fetch a URL for it, and a replay stays offline);
  - JPEG, PNG, GIF or WebP, recognised from the bytes themselves, not from what the URL claims;
  - anything with a long edge over MAX_EDGE px or over MAX_BYTES is downscaled with macOS `sips`, here in the
    supervisor (outside the sandbox);
  - at most MAX_IMAGES per call.
`cloudllm.to_anthropic` turns the parts into Claude image blocks; OpenRouter takes them as they are.

Which models see images is learned, not listed: Claude models do; an OpenRouter model does if OpenRouter's model
list says "image" among its input modalities (cached for a day in .supervisor/models.json); a self-hosted model
(gpu/bonsai2) does not.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from .core import NETWORK_ERRORS, SUP_DIR, knob, positive_int, log

MAX_EDGE = 1568                 # Claude's advice: larger images are scaled down anyway, at the same token cost
MAX_BYTES = 3_750_000           # under Claude's 5 MB per image once base64-encoded
MAX_IMAGES, _ = knob(
    "SHAFT_MAX_IMAGES", "4", positive_int, "images one model call may carry", group="Runs and conversations",
    services=("runs", "api"))
MODELS_URL = "https://openrouter.ai/api/v1/models"
MODELS_TTL_S = 86400
TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
SUFFIXES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
            ".webp": "image/webp"}
urlopen = urllib.request.urlopen     # the tests put a fake OpenRouter behind it


class VisionError(ValueError):
    """An image the call cannot carry; the message says which and why."""


# -- which models see images ---------------------------------------------------------------------------------------
def _cache() -> Path:
    return SUP_DIR / "models.json"   # read at call time: tests move SUP_DIR


def _openrouter_modalities() -> dict[str, list[str]]:
    """{model id: input modalities}, from OpenRouter's public model list, cached for a day. A failed refresh keeps
    the cache it has (logged); with no cache at all it raises."""
    path = _cache()
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        cached = None
    if cached and time.time() - cached.get("at", 0) < MODELS_TTL_S:
        return cached["models"]
    try:
        with urlopen(MODELS_URL, timeout=30) as resp:
            data = json.load(resp)["data"]
        models = {m["id"]: list((m.get("architecture") or {}).get("input_modalities") or []) for m in data}
    except (*NETWORK_ERRORS, ValueError, KeyError, TypeError) as e:
        if cached:
            log(f"vision: OpenRouter's model list could not be refreshed ({type(e).__name__}: {e}); "
                f"using the one from {time.strftime('%Y-%m-%d %H:%M', time.localtime(cached.get('at', 0)))}")
            return cached["models"]
        raise VisionError(f"cannot tell which OpenRouter models see images: {MODELS_URL} failed "
                          f"({type(e).__name__}: {e}) and there is no cached list") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": time.time(), "models": models}))
    return models


def sees_images(model: str | None) -> bool:
    """Whether `model`, as an instance's config.env names it, accepts images. Raises VisionError when it cannot be
    known (OpenRouter's list unreachable and never cached)."""
    from . import selfhosted
    model = str(model or "")
    if selfhosted.serves(model):
        return False
    if model.startswith("openrouter/"):
        return "image" in _openrouter_modalities().get(model.removeprefix("openrouter/"), [])
    return model.startswith("claude-")


def sees_images_or_no(model: str | None) -> bool:
    """For the run's environment (SHAFT_VISION): unknown counts as no, and is logged — the instance is then told it
    cannot see images, and the proxy refuses any image it sends anyway."""
    try:
        return sees_images(model)
    except VisionError as e:
        log(f"vision: {model}: {e}; treated as not seeing images for this run")
        return False


# -- preparing one image ---------------------------------------------------------------------------------------------
def sniff(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _sips(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/usr/bin/sips", *args], capture_output=True, text=True, timeout=60)


def _edge(path: str) -> int:
    r = _sips("-g", "pixelWidth", "-g", "pixelHeight", path)
    sizes = [int(line.split(":")[1]) for line in r.stdout.splitlines() if "pixel" in line and ":" in line
             and line.split(":")[1].strip().isdigit()]
    if r.returncode or len(sizes) != 2:
        raise VisionError(f"the image could not be read (sips: {(r.stderr or r.stdout).strip()[:200]})")
    return max(sizes)


def prepare_bytes(data: bytes, name: str = "the image") -> tuple[str, bytes]:
    """(media type, bytes) ready to send: a supported type, recognised from its bytes; downscaled to MAX_EDGE (as
    JPEG) when larger or over MAX_BYTES."""
    media = sniff(data)
    if media is None:
        raise VisionError(f"{name} is not a JPEG, PNG, GIF or WebP image")
    with tempfile.TemporaryDirectory(prefix="shaft-vision-") as tmp:
        src = os.path.join(tmp, "in" + {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                                        "image/webp": ".webp"}[media])
        Path(src).write_bytes(data)
        if _edge(src) <= MAX_EDGE and len(data) <= MAX_BYTES:
            return media, data
        out = os.path.join(tmp, "out.jpg")
        r = _sips("-Z", str(MAX_EDGE), "-s", "format", "jpeg", "-s", "formatOptions", "85", src, "--out", out)
        if r.returncode or not Path(out).is_file():
            raise VisionError(f"{name} could not be downscaled (sips: {(r.stderr or r.stdout).strip()[:200]})")
        small = Path(out).read_bytes()
    if len(small) > MAX_BYTES:
        raise VisionError(f"{name} is still {len(small)} bytes after downscaling to {MAX_EDGE} px")
    return "image/jpeg", small


def data_url(media: str, data: bytes) -> str:
    return f"data:{media};base64,{base64.b64encode(data).decode()}"


def split_data_url(url: str) -> tuple[str, bytes]:
    """(claimed media type, bytes) of a data: URL; anything else is refused."""
    head, comma, payload = str(url or "").partition(",")
    if not (url.startswith("data:") and comma and head.endswith(";base64")):
        raise VisionError("an image must be sent as a base64 data: URL (no links to fetch)")
    try:
        return head[5:-7], base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise VisionError("an image's data: URL is not valid base64") from None


def _parts(body: dict):
    for m in body.get("messages") or []:
        if isinstance(m.get("content"), list):
            for part in m["content"]:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    yield m, part


def count(body: dict) -> int:
    return sum(1 for _ in _parts(body))


def prepare(body: dict, model: str | None = None) -> dict:
    """The request with every image checked and prepared, or VisionError. A request without images is returned as
    it is (and costs nothing here)."""
    images = list(_parts(body))
    if not images:
        return body
    model = model or body.get("model")
    if images and not sees_images(model):
        raise VisionError(f"model {model!r} cannot see images; this request carries {len(images)} "
                          "(describe_image can tell you what is in one)")
    if len(images) > MAX_IMAGES:
        raise VisionError(f"{len(images)} images in one call; at most {MAX_IMAGES} (SHAFT_MAX_IMAGES)")
    for n, (_, part) in enumerate(images, 1):
        url = (part.get("image_url") or {}).get("url") if isinstance(part.get("image_url"), dict) else None
        _, raw = split_data_url(url or "")
        media, data = prepare_bytes(raw, f"image {n}")
        part["image_url"] = {**part["image_url"], "url": data_url(media, data)}
    return body


def without_images(content):
    """`content` with each image part replaced by a short text, for anything that is stored or logged."""
    if not isinstance(content, list):
        return content
    return "".join(p.get("text", "") if p.get("type") == "text" else "[image]" for p in content
                   if isinstance(p, dict))

"""Regenerate both native app icons using the PWA's stdlib renderer."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from supervisor.web_icons import png


def main() -> None:
    catalog = Path(__file__).resolve().parent / "Assets.xcassets"
    appicon = catalog / "AppIcon.appiconset"
    appicon.mkdir(parents=True, exist_ok=True)
    info = {"author": "xcode", "version": 1}
    (catalog / "Contents.json").write_text(json.dumps({"info": info}, indent=2) + "\n")
    filename = "ios-1024.png"
    (appicon / filename).write_bytes(png(1024, bleed=True))
    images = [{"filename": filename, "idiom": "universal", "platform": "ios", "size": "1024x1024"}]
    rendered = set()
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = size * scale
            filename = f"mac-{pixels}.png"
            if pixels not in rendered:
                # The original tile's 3.125% margin plus this inset gives 10.625% per side.
                (appicon / filename).write_bytes(png(pixels, inset=.08))
                rendered.add(pixels)
            images.append({"filename": filename, "idiom": "mac", "size": f"{size}x{size}", "scale": f"{scale}x"})
    (appicon / "Contents.json").write_text(json.dumps({"images": images, "info": info}, indent=2) + "\n")


if __name__ == "__main__":
    main()

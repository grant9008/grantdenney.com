"""
Turn raw screenshots into web-ready project images and wire them into the page.

How to use:
  1. Drop your screenshots into  assets/img/work/incoming/
     Name each file after the project it belongs to:
         pocketge.png      -> the PocketGE card
         fliptracker.png   -> the Flip Tracker card
         iracehud.png      -> the iRaceHUD card
         dalecarnegie.png  -> the Dale Carnegie block in the Sales section
     (.png, .jpg and .webp all work.)
  2. Double-click  Add-Screenshots.bat
  3. Refresh the site.

Anything you don't provide keeps its placeholder panel, so it's fine to add
them one at a time.
"""
import os
import re
import io
import glob

from PIL import Image, ImageOps

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX = os.path.join(ROOT, "assets", "img", "work", "incoming")
OUT = os.path.join(ROOT, "assets", "img", "work")
PAGE = os.path.join(ROOT, "index.html")

# data-shot value -> human label used in the alt text
SHOTS = {
    "pocketge": "PocketGE running in the browser",
    "fliptracker": "The PocketGE Flip Tracker panel inside RuneLite",
    "iracehud": "iRaceHUD overlaid on an iRacing session",
    "dalecarnegie": "Dale Carnegie course in Los Angeles",
}

WIDTHS = [1200, 700]


def find_incoming(key):
    for ext in ("png", "jpg", "jpeg", "webp", "PNG", "JPG", "JPEG", "WEBP"):
        hits = glob.glob(os.path.join(INBOX, key + "." + ext))
        if hits:
            return hits[0]
    return None


def build(key):
    """Optimize one screenshot. Returns True if it produced files."""
    src = find_incoming(key)
    if not src:
        return False
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    for w in WIDTHS:
        scaled = im.copy()
        scaled.thumbnail((w, w * 3), Image.LANCZOS)
        scaled.save(os.path.join(OUT, key + "-" + str(w) + ".webp"), "WEBP", quality=84, method=6)
        scaled.save(os.path.join(OUT, key + "-" + str(w) + ".jpg"), "JPEG", quality=82, optimize=True, progressive=True)
    print("  built " + key + "  (" + str(im.width) + "x" + str(im.height) + ")")
    return True


def panel_markup(key, alt, wrapper):
    p = "assets/img/work/" + key
    return (
        '<div class="' + wrapper + '" data-shot="' + key + '">\n'
        '          <picture>\n'
        '            <source type="image/webp" srcset="' + p + '-700.webp 700w, ' + p + '-1200.webp 1200w" sizes="(max-width: 900px) 100vw, 560px">\n'
        '            <img src="' + p + '-1200.jpg" srcset="' + p + '-700.jpg 700w, ' + p + '-1200.jpg 1200w" sizes="(max-width: 900px) 100vw, 560px" alt="' + alt + '" loading="lazy" decoding="async">\n'
        '          </picture>\n'
        '        </div>'
    )


def main():
    os.makedirs(INBOX, exist_ok=True)

    built = []
    print("Looking in " + INBOX)
    for key in SHOTS:
        if build(key):
            built.append(key)

    if not built:
        print("\nNo screenshots found. Drop files named "
              + ", ".join(k + ".png" for k in SHOTS) + " into:\n  " + INBOX)
        return 0

    html = io.open(PAGE, encoding="utf-8").read()
    for key in built:
        # Replace whichever panel is currently there — placeholder or a previous image.
        pattern = re.compile(
            r'<div class="(card__shot|training__shot)[^"]*" data-shot="' + re.escape(key) + r'">.*?</div>',
            re.S,
        )
        # Keep whichever wrapper class the panel already uses (card vs training).
        html, n = pattern.subn(
            lambda m: panel_markup(key, SHOTS[key], m.group(1)), html, count=1
        )
        if not n:
            print("  ! could not find the " + key + " panel in index.html")
    io.open(PAGE, "w", encoding="utf-8", newline="\n").write(html)

    print("\nWired " + str(len(built)) + " screenshot(s) into index.html: " + ", ".join(built))
    print("Refresh the site to see them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

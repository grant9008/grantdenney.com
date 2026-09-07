"""
Optimize source photos into web-ready images.

Usage:  python tools/build-images.py
Reads:  tools/showrun-picks.txt  (one source filename per line, optional "| Caption")
Writes: assets/img/showrun/<name>-{1600,900,480}.{webp,jpg}

Re-run this any time you add or change photos.
"""
import os, sys, glob

from PIL import Image, ImageOps

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.environ.get("SHOWRUN_SRC", r"C:\Users\gdenn\Desktop\Showrun Photos")
OUT_DIR = os.path.join(ROOT, "assets", "img", "showrun")
PICKS = os.path.join(ROOT, "tools", "showrun-picks.txt")

WIDTHS = [1600, 900, 480]

os.makedirs(OUT_DIR, exist_ok=True)


def picks():
    if not os.path.exists(PICKS):
        print(f"no picks file at {PICKS}", file=sys.stderr)
        return []
    out = []
    with open(PICKS, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name, _, caption = line.partition("|")
            out.append((name.strip(), caption.strip()))
    return out


def slug(filename):
    return os.path.splitext(filename)[0].lower()


def main():
    items = picks()
    if not items:
        return 1
    made = 0
    for filename, caption in items:
        src = os.path.join(SRC_DIR, filename)
        if not os.path.exists(src):
            print(f"MISSING  {filename}", file=sys.stderr)
            continue
        im = Image.open(src)
        im = ImageOps.exif_transpose(im).convert("RGB")
        base = slug(filename)
        for w in WIDTHS:
            if im.width <= w and w != WIDTHS[0]:
                continue
            scaled = im.copy()
            scaled.thumbnail((w, w * 3), Image.LANCZOS)
            scaled.save(os.path.join(OUT_DIR, f"{base}-{w}.webp"), "WEBP", quality=82, method=6)
            scaled.save(os.path.join(OUT_DIR, f"{base}-{w}.jpg"), "JPEG", quality=80, optimize=True, progressive=True)
            made += 1
        print(f"ok  {filename}  ({im.width}x{im.height})")
    print(f"\nwrote {made} renditions to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

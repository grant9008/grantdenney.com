"""Emit the <figure> markup for the Showrun slider from tools/showrun-picks.txt."""
import os, sys, html, io

# Force UTF-8 out; the default console codec mangles dashes on Windows.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="
")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICKS = os.path.join(ROOT, "tools", "showrun-picks.txt")

rows = []
with open(PICKS, encoding="utf-8") as fh:
    for raw in fh:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, caption = line.partition("|")
        rows.append((os.path.splitext(name.strip())[0].lower(), caption.strip()))

for i, (base, caption) in enumerate(rows, 1):
    p = f"assets/img/showrun/{base}"
    cap = html.escape(caption)
    print(f'''        <figure class="slide" data-full="{p}-1600.jpg" data-cap="{cap}" tabindex="0" role="button" aria-label="Open larger: {cap}">
          <picture>
            <source type="image/webp" srcset="{p}-480.webp 480w, {p}-900.webp 900w, {p}-1600.webp 1600w" sizes="(max-width: 700px) 82vw, 620px">
            <img src="{p}-900.jpg" srcset="{p}-480.jpg 480w, {p}-900.jpg 900w, {p}-1600.jpg 1600w" sizes="(max-width: 700px) 82vw, 620px" alt="{cap}" loading="lazy" decoding="async" width="900" height="600">
          </picture>
          <figcaption><b>{i:02d}</b> {cap}</figcaption>
        </figure>''')

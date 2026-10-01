"""
Splice the generated slide markup into index.html.

Replaces everything between the SLIDES-START and SLIDES-END comment markers
inside the Showrun slider. Run tools/gen-slides.py first (or just use
Rebuild-Photos.bat, which runs all three steps in order).
"""
import os
import io
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "index.html")
SLIDES = os.path.join(ROOT, "tools", "_slides.html")

START = "<!-- SLIDES-START -->"
END = "<!-- SLIDES-END -->"


def main():
    if not os.path.exists(SLIDES):
        print("No generated slides at " + SLIDES + ", run tools/gen-slides.py first.")
        return 1

    slides = io.open(SLIDES, encoding="utf-8").read().rstrip("\n")
    html = io.open(PAGE, encoding="utf-8").read()

    if START not in html or END not in html:
        print("Could not find the SLIDES-START / SLIDES-END markers in index.html.")
        return 1

    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
    html, n = pattern.subn(START + "\n" + slides + "\n        " + END, html, count=1)
    io.open(PAGE, "w", encoding="utf-8", newline="\n").write(html)

    count = slides.count("<figure")
    print("Spliced " + str(count) + " slides into index.html.")

    # The counter in the slider bar is server-rendered so it reads correctly
    # before the script boots; keep it in step with the real slide count.
    html = io.open(PAGE, encoding="utf-8").read()
    html, m = re.subn(
        r'(<span class="slider__count" data-count>)\d+ / \d+(</span>)',
        lambda mo: mo.group(1) + "01 / " + ("%02d" % count) + mo.group(2),
        html,
        count=1,
    )
    if m:
        io.open(PAGE, "w", encoding="utf-8", newline="\n").write(html)
        print("Updated the slide counter to 01 / " + ("%02d" % count) + ".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

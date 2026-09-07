"""
Emit the <figure> markup for the Showrun slider from tools/showrun-picks.txt.

Writes tools/_slides.html, which then gets spliced into index.html between the
SLIDES-START / SLIDES-END markers by tools/apply-slides.py.

Run tools/build-images.py first so the image renditions exist.
"""
import os
import io
import html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICKS = os.path.join(ROOT, "tools", "showrun-picks.txt")
OUT = os.path.join(ROOT, "tools", "_slides.html")


def picks():
    rows = []
    with io.open(PICKS, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name, _, caption = line.partition("|")
            rows.append((os.path.splitext(name.strip())[0].lower(), caption.strip()))
    return rows


def figure(i, base, caption):
    p = "assets/img/showrun/" + base
    cap = html.escape(caption)
    sizes = "(max-width: 700px) 82vw, 620px"
    return (
        '        <figure class="slide" data-full="' + p + '-1600.jpg" data-cap="' + cap + '"'
        ' tabindex="0" role="button" aria-label="Open larger: ' + cap + '">\n'
        '          <picture>\n'
        '            <source type="image/webp" srcset="' + p + '-480.webp 480w, '
        + p + '-900.webp 900w, ' + p + '-1600.webp 1600w" sizes="' + sizes + '">\n'
        '            <img src="' + p + '-900.jpg" srcset="' + p + '-480.jpg 480w, '
        + p + '-900.jpg 900w, ' + p + '-1600.jpg 1600w" sizes="' + sizes + '"'
        ' alt="' + cap + '" loading="lazy" decoding="async" width="900" height="600">\n'
        '          </picture>\n'
        '          <figcaption><b>' + ("%02d" % i) + '</b> ' + cap + '</figcaption>\n'
        '        </figure>'
    )


def main():
    rows = picks()
    out = "\n".join(figure(i, base, cap) for i, (base, cap) in enumerate(rows, 1))
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(out + "\n")
    print("wrote " + str(len(rows)) + " slides to " + OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

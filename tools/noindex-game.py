"""Keep the published game build out of search results.

Vite rewrites ebike/index.html on every build, so the robots meta tag has to be
put back each time. Run from Publish-Game.bat rather than by hand.
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "ebike", "index.html")

TAG = '<meta name="robots" content="noindex, nofollow">'

s = io.open(PAGE, encoding="utf-8").read()
if "noindex" in s:
    print("  noindex already present")
else:
    s = s.replace("<head>", "<head>\n" + TAG, 1)
    io.open(PAGE, "w", encoding="utf-8", newline="\n").write(s)
    print("  noindex added")

"""Keep the published game build out of search results.

Vite rewrites ebike/index.html on every build, so the robots meta tag has to be
put back each time. Run from Publish-Game.bat rather than by hand.
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# optional argument: the folder to mark (default "ebike"; Publish-Game2.bat passes "ebike2")
FOLDER = sys.argv[1] if len(sys.argv) > 1 else "ebike"
PAGE = os.path.join(ROOT, FOLDER, "index.html")

TAG = '<meta name="robots" content="noindex, nofollow">'

s = io.open(PAGE, encoding="utf-8").read()
if "noindex" in s:
    print("  noindex already present")
else:
    s = s.replace("<head>", "<head>\n" + TAG, 1)
    io.open(PAGE, "w", encoding="utf-8", newline="\n").write(s)
    print("  noindex added")

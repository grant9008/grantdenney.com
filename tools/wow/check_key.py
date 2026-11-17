"""Checks the saved Battle.net key works and lists every WoW Forever auction
house with how many auctions it holds, so the right house ID can be picked.
Exit code 0 = the key works, 1 = Blizzard refused it or could not be reached."""
import sys
import urllib.error

import fetch_ah as f

try:
    f.TOKEN = f.get_token()
except urllib.error.HTTPError as e:
    if e.code in (400, 401, 403):
        print("Blizzard did not accept the Client ID and Client Secret (error %d)." % e.code)
    else:
        print("Blizzard's login server answered with error %d. Try again in a few minutes." % e.code)
    sys.exit(1)
except (urllib.error.URLError, TimeoutError, OSError) as e:
    print("Could not reach Blizzard (%s). Check the internet connection and try again." % e)
    sys.exit(1)

print("Key works. Blizzard gave us an access token.\n")
try:
    idx, _ = f.api(f"/data/wow/ruleset/{f.RULESET}/auctions/", f.DYN)
except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
    print("The key works, but the list of auction houses could not be read (%s, %s)." % (e, f.DYN))
    sys.exit(0)
houses = (idx or {}).get("auctions", [])
if not houses:
    print("No auction houses came back for", f.DYN)
for h in houses:
    try:
        data, _ = f.api(f"/data/wow/ruleset/{f.RULESET}/auctions/{h['id']}", f.DYN)
        count = "%7d auctions" % len((data or {}).get("auctions") or [])
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        count = "  (could not read: %s)" % e
    mark = "   <- the one the site uses" if h["id"] == f.HOUSE else ""
    print(f"  house {h['id']:>4}  {h.get('name', ''):<26} {count}{mark}")

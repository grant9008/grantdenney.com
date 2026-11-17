"""Checks the saved Battle.net key works and lists every WoW Forever auction
house with how many auctions it holds, so the right house ID can be picked."""
import fetch_ah as f

f.TOKEN = f.get_token()
print("Key works. Blizzard gave us an access token.\n")
idx, _ = f.api(f"/data/wow/ruleset/{f.RULESET}/auctions/", f.DYN)
houses = (idx or {}).get("auctions", [])
if not houses:
    print("No auction houses came back for", f.DYN)
for h in houses:
    data, _ = f.api(f"/data/wow/ruleset/{f.RULESET}/auctions/{h['id']}", f.DYN)
    n = len((data or {}).get("auctions") or [])
    mark = "   <- the one the site uses" if h["id"] == f.HOUSE else ""
    print(f"  house {h['id']:>4}  {h.get('name', ''):<26} {n:>7} auctions{mark}")

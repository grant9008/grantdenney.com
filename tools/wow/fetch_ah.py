"""
Hourly WoW Forever auction-house collector for grantdenney.com/wow.

Pulls one auction house from Blizzard's API (namespace dynamic-forever-<region>)
and writes files shaped exactly like the OSRS Wiki price API, so the PocketGE
app on /wow/ can read them with almost no changes:

  mapping.json   items: id, name, icon, vendor price ("highalch"), quality
  latest.json    {data: {id: {high, highTime, low, lowTime}}}
                 low  = cheapest buyout per unit (what you can buy at right now)
                 high = market value: mean of the cheapest ~15% of units,
                        outliers cut (TSM DBMarket style), i.e. where it resells
  24h.json       {data: {id: {avgHighPrice, highPriceVolume, avgLowPrice, lowPriceVolume}}}
  volumes.json   {timestamp, data: {id: estimated units sold in 24h}}
  hist/<id>.json {h: [[t, low, high, listed, sold], ...] hourly, last 35 days,
                  d: [[t, low, high, listed, sold], ...] daily, kept forever}
  houses.json    every house the API lists, with auction counts (for checking IDs)
  status.json    when this ran and what it saw

"Sold" is an estimate. The API only shows what is listed, never sales. An
auction that was listed with hours left and is gone an hour later was most
likely bought (it could also have been cancelled), and a stack whose quantity
went down sold the difference. Auctions that were about to expire anyway are
not counted. That is the same idea TSM and Saddlebag use.

Prices are in copper (1g = 10000c), like the game.

Credentials: BNET_CLIENT_ID / BNET_CLIENT_SECRET env vars, or the file
%USERPROFILE%\\.wow-ah\\bnet.txt (id on line 1, secret on line 2) that
Setup-WoW-Key.bat writes.

Usage: python fetch_ah.py <output dir>
"""
import base64
import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REGION = os.environ.get("WOW_REGION", "us").lower()
HOUSE = int(os.environ.get("WOW_HOUSE", "110"))   # Alliance AH, PvP ruleset (see houses.json)
RULESET = int(os.environ.get("WOW_RULESET", "1"))  # 1 = PvP; the API ignores it for auctions
API = f"https://{REGION}.api.blizzard.com"
DYN = f"dynamic-forever-{REGION}"
STATIC_NS = [f"static-forever-{REGION}", f"static-classic1x-{REGION}", f"static-classic-{REGION}"]
HOURLY_KEEP = 35 * 24
SOLD_TIME_LEFT = {"LONG", "VERY_LONG"}   # gone before it could have expired => count as sold


# ── HTTP ─────────────────────────────────────────────────────────────────────
def creds():
    cid, sec = os.environ.get("BNET_CLIENT_ID"), os.environ.get("BNET_CLIENT_SECRET")
    if cid and sec:
        return cid.strip(), sec.strip()
    path = os.path.join(os.path.expanduser("~"), ".wow-ah", "bnet.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = [l.strip() for l in fh.read().splitlines() if l.strip()]
        if len(lines) >= 2:
            return lines[0], lines[1]
    sys.exit("No Battle.net API key. Run Setup-WoW-Key.bat first.")


def get_token():
    cid, sec = creds()
    req = urllib.request.Request(
        "https://oauth.battle.net/token",
        data=b"grant_type=client_credentials",
        headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(),
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


TOKEN = None


def api(path, ns, tries=4):
    """GET an API path. Returns (json or None on 404, headers)."""
    sep = "&" if "?" in path else "?"
    url = f"{API}{path}{sep}namespace={ns}&locale=en_US"
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + TOKEN,
                                                   "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r), dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, dict(e.headers or {})
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise


def load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(tmp, path)


# ── pricing ──────────────────────────────────────────────────────────────────
def market_value(units):
    """units: sorted list of (unit_price, qty). TSM-DBMarket-like: take the
    cheapest 15% of units (at least the cheapest 1), drop anything more than
    1.5x the running cheapest-15% average, and return the weighted mean."""
    total = sum(q for _, q in units)
    if total <= 0:
        return 0
    want = max(1, int(round(total * 0.15)))
    picked, n = [], 0
    for p, q in units:
        if n >= want:
            break
        take = min(q, want - n)
        picked.append((p, take))
        n += take
    # outlier cut: stop where the price jumps past 1.5x the mean so far
    kept, s, c = [], 0, 0
    for p, q in picked:
        if c and p > 1.5 * (s / c):
            break
        kept.append((p, q))
        s += p * q
        c += q
    return int(round(s / c)) if c else units[0][0]


# ── item metadata ────────────────────────────────────────────────────────────
def fetch_item(iid):
    for ns in STATIC_NS:
        try:
            item, _ = api(f"/data/wow/item/{iid}", ns)
        except Exception:
            item = None
        if not item:
            continue
        icon = ""
        try:
            media, _ = api(f"/data/wow/media/item/{iid}", ns)
            for a in (media or {}).get("assets", []):
                if a.get("key") == "icon":
                    icon = a.get("value", "")
        except Exception:
            pass
        return iid, {
            "name": item.get("name") or f"Item {iid}",
            "icon": icon,
            "quality": ((item.get("quality") or {}).get("type") or "COMMON"),
            "sell": int(item.get("sell_price") or 0),
            "buy": int(item.get("purchase_price") or 0),
            "cls": ((item.get("item_class") or {}).get("name") or ""),
            "sub": ((item.get("item_subclass") or {}).get("name") or ""),
            "lvl": int(item.get("required_level") or 0),
            "stack": int(item.get("max_count") or 0),
        }
    return iid, None


def main():
    global TOKEN
    out = sys.argv[1] if len(sys.argv) > 1 else "wowdata"
    os.makedirs(out, exist_ok=True)
    TOKEN = get_token()
    now = int(time.time())
    hour = now - now % 3600
    state = load(os.path.join(out, "state.json"), {})

    # every house, so the IDs can be checked against what the game shows
    idx, _ = api(f"/data/wow/ruleset/{RULESET}/auctions/", DYN)
    houses = [{"id": h.get("id"), "name": h.get("name")} for h in (idx or {}).get("auctions", [])]
    save(os.path.join(out, "houses.json"), {"region": REGION, "ruleset": RULESET,
                                            "using": HOUSE, "houses": houses})

    data, hdr = api(f"/data/wow/ruleset/{RULESET}/auctions/{HOUSE}", DYN)
    if data is None:
        sys.exit(f"House {HOUSE} not found in {DYN}")
    modified = hdr.get("Last-Modified") or hdr.get("last-modified") or ""
    if modified and modified == state.get("modified"):
        print("Blizzard has not refreshed this house since the last run:", modified)
        return
    auctions = list(data.get("auctions") or [])
    try:
        com, _ = api(f"/data/wow/ruleset/{RULESET}/auctions/{HOUSE}/commodities", DYN)
        auctions += list((com or {}).get("auctions") or [])
    except Exception as e:
        print("commodities endpoint skipped:", e)

    # aggregate the snapshot
    per = {}        # item id -> list of (unit price, qty)
    snap = {}       # auction id -> [item, qty, unit, time_left]
    for a in auctions:
        it = a.get("item") or {}
        iid = it.get("id")
        q = int(a.get("quantity") or 1)
        if not iid or q <= 0:
            continue
        unit = a.get("unit_price")
        if unit is None:
            bo = a.get("buyout")
            if not bo:
                continue          # bid-only: no price anyone can act on
            unit = bo / q
        unit = int(round(unit))
        if unit <= 0:
            continue
        per.setdefault(iid, []).append((unit, q))
        snap[str(a.get("id"))] = [iid, q, unit, a.get("time_left", "")]

    # estimated sales since the previous snapshot
    sold = {}
    prev = state.get("snap") or {}
    if prev:
        for aid, (iid, q, unit, tl) in prev.items():
            cur = snap.get(aid)
            if cur is None:
                if tl in SOLD_TIME_LEFT:
                    sold[iid] = sold.get(iid, 0) + q
            elif cur[1] < q:
                sold[iid] = sold.get(iid, 0) + (q - cur[1])

    # item metadata (cached; only new ids cost requests)
    items = load(os.path.join(out, "items.json"), {})
    missing = [i for i in per if str(i) not in items]
    if missing:
        print(f"looking up {len(missing)} new items")
        with cf.ThreadPoolExecutor(12) as ex:
            for iid, meta in ex.map(fetch_item, missing):
                items[str(iid)] = meta or {"name": f"Item {iid}", "icon": "", "quality": "COMMON",
                                           "sell": 0, "buy": 0, "cls": "", "sub": "", "lvl": 0, "stack": 0,
                                           "unknown": True}
        save(os.path.join(out, "items.json"), items)

    # history
    hist_dir = os.path.join(out, "hist")
    os.makedirs(hist_dir, exist_ok=True)
    latest, day, vols = {}, {}, {}
    seen = set(str(i) for i in per) | set(str(i) for i in sold)
    # items that vanished entirely still get a point so the chart shows the gap close
    for f in os.listdir(hist_dir):
        if f.endswith(".json"):
            seen.add(f[:-5])

    for sid in seen:
        iid = int(sid)
        units = sorted(per.get(iid, []))
        listed = sum(q for _, q in units)
        lo = units[0][0] if units else 0
        hi = market_value(units) if units else 0
        s = int(sold.get(iid, 0))
        path = os.path.join(hist_dir, sid + ".json")
        h = load(path, {"h": [], "d": []})
        if not units and not s and not h["h"]:
            continue
        pt = [hour, lo, hi, listed, s]
        if h["h"] and h["h"][-1][0] == hour:
            h["h"][-1] = pt
        else:
            h["h"].append(pt)
        h["h"] = h["h"][-HOURLY_KEEP:]
        # roll today's hours into one daily point
        dstart = hour - hour % 86400
        today = [p for p in h["h"] if p[0] >= dstart]
        priced = [p for p in today if p[1] > 0]
        dpt = [dstart,
               int(sum(p[1] for p in priced) / len(priced)) if priced else 0,
               int(sum(p[2] for p in priced) / len(priced)) if priced else 0,
               int(sum(p[3] for p in today) / len(today)) if today else 0,
               sum(p[4] for p in today)]
        if h["d"] and h["d"][-1][0] == dstart:
            h["d"][-1] = dpt
        else:
            h["d"].append(dpt)
        if not units and all(p[3] == 0 for p in h["h"][-72:]) and not any(p[4] for p in h["h"][-72:]):
            # nothing listed or sold for 3 days: keep the file, stop updating latest
            save(path, h)
            continue
        save(path, h)

        if units:
            latest[sid] = {"high": hi, "highTime": now, "low": lo, "lowTime": now}
        else:
            # nothing listed right now: show the last seen prices, with their real age
            last = next((p for p in reversed(h["h"]) if p[1] > 0), None)
            if last:
                latest[sid] = {"high": last[2], "highTime": last[0], "low": last[1], "lowTime": last[0]}
        win = [p for p in h["h"] if p[0] > hour - 86400]
        wp = [p for p in win if p[1] > 0]
        sold24 = sum(p[4] for p in win)
        if wp:
            day[sid] = {"avgHighPrice": int(sum(p[2] for p in wp) / len(wp)),
                        "highPriceVolume": sold24 - sold24 // 2,
                        "avgLowPrice": int(sum(p[1] for p in wp) / len(wp)),
                        "lowPriceVolume": sold24 // 2,
                        "listed": listed}
        vols[sid] = sold24

    mapping = []
    for sid, m in items.items():
        if sid not in latest:
            continue
        mapping.append({"id": int(sid), "name": m["name"], "examine": m.get("sub") or m.get("cls") or "",
                        "members": True, "value": m["sell"], "lowalch": m["sell"], "highalch": m["sell"],
                        "limit": None, "icon": m["icon"], "quality": m["quality"],
                        "cls": m.get("cls", ""), "sub": m.get("sub", ""), "lvl": m.get("lvl", 0)})
    mapping.sort(key=lambda x: x["name"])

    save(os.path.join(out, "mapping.json"), mapping)
    save(os.path.join(out, "latest.json"), {"data": latest})
    save(os.path.join(out, "24h.json"), {"data": day, "timestamp": hour})
    save(os.path.join(out, "volumes.json"), {"timestamp": now * 1000, "data": vols})
    save(os.path.join(out, "status.json"), {
        "updated": now, "blizzardModified": modified, "region": REGION, "house": HOUSE,
        "auctions": len(auctions), "items": len(latest), "soldUnitsThisHour": sum(sold.values()),
        "firstSnapshot": not prev})
    state.update({"modified": modified, "snap": snap, "at": now})
    save(os.path.join(out, "state.json"), state)
    print(f"house {HOUSE}: {len(auctions)} auctions, {len(latest)} items, "
          f"~{sum(sold.values())} units sold since last snapshot")


if __name__ == "__main__":
    main()

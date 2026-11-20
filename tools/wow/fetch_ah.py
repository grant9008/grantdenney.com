"""
WoW Forever auction-house collector for Goblin Ledger (grantdenney.com/goblin-ledger/prices).

Pulls one auction house from Blizzard's API (namespace dynamic-forever-<region>)
and writes files shaped exactly like the OSRS Wiki price API, so the PocketGE
app on /wow/ can read them with almost no changes:

  mapping.json   items: id, name ("" when Blizzard has no name for it yet),
                 icon, vendor price ("highalch"), quality
  latest.json    {data: {id: {high, highTime, low, lowTime, n}}}
                 low  = cheapest buyout per unit (what you can buy at right now)
                 high = market value (see market_value), i.e. where it resells
                 n    = how many separate auctions are listed right now
  24h.json       {data: {id: {avgHighPrice, highPriceVolume, avgLowPrice, lowPriceVolume}}}
  volumes.json   {timestamp, data: {id: estimated units sold in 24h}}
  hist/<id>.json {h: [[t, low, high, listed, sold], ...] hourly, last 35 days,
                  d: [[t, low, high, listed, sold], ...] daily, kept forever,
                  v: the variant key, only in a variant's file}
  flips.json     {updated, flips: [...]} flip ideas found in this snapshot,
                 best first (gap, vendor and deal flips; see write_flips)
  details/<n>.json
                 {"<item id>": [[text, colour], ...]} the item's in-game
                 tooltip lines for the site, items id // 1000 == n (see
                 tooltip_lines and update_details)
  suffix_names.json
                 a copy of tools/wow/suffix_names.json (see below)
  houses.json    every house the API lists, with auction counts (for checking IDs)
  status.json    when Blizzard made the data, when this ran and what it saw
  ForeverAH_Data.lua
                 the same prices packed for the Goblin Ledger in-game addon
                 (wow-addon/GoblinLedger, formerly Forever AH). Addons cannot
                 use the internet, so the price updater (update_prices.ps1)
                 downloads this file into the addon folder as Data.lua. The
                 file keeps its old name and header line: installed updaters
                 check both. Format: see write_addon_data below.

"Sold" is an estimate. The API only shows what is listed, never sales. An
auction that was listed with hours left and is gone at the next snapshot was
most likely bought, and a stack whose quantity went down sold the difference.
Auctions that were about to expire anyway are not counted. That is the same
idea TSM and Saddlebag use. A gone auction is not counted either when a
cheaper auction of the same item (the same version, for a version) from the
previous snapshot is still listed: buyers take the cheapest first, so that
one was cancelled or reposted, not bought (see estimate_sold). The estimate
is skipped (and that time does not count as measured) when the previous
snapshot is too old, because then vanished auctions say nothing reliable
about sales. Hours measured before that cheaper-listing rule came in
(SOLD_FLOOR_FROM) counted pulled and reposted stacks as sales, so they no
longer count as measured: sold24, salesHours and the sold per day that sizes
flips use only the hours from then on (the hist files keep their sold).

A download that looks incomplete (a list failed, or the auction count fell
sharply) is not published at all: the next run downloads it again. Only when
the next download looks the same is it used, and then neither it nor the
comparison after it gets a sales estimate.

status.json salesHours (also in the addon file) is how many hours of sales
the last 24 hourly slots' estimates cover, from the minutes each comparison
really spans (salesMinutes), so readers can scale sold24 up to a full day.

Times: "updated" everywhere is when Blizzard made the snapshot (the
Last-Modified header), "fetched" is when this script ran.

Prices are in copper (1g = 10000c), like the game.

Versions of the same item (variants). Green gear comes with a random suffix
("of the Bear", "of the Eagle"), and each suffix is really a different item
with a different price. The API marks it in the auction's "bonus_lists". The
variant key of an auction is its bonus ids sorted ascending and joined with
":" ("12722"; "" when there are none). Every (item id, non-empty key) seen gets
a stable index 1..999 for that item, in order of first appearance, kept in
state.json "variants" and never reused, and a variant id
vid = item id * 1000 + index. A vid is published like any item (history,
latest, 24h, volumes, mapping, addon line); the plain item id keeps meaning
"all versions together". hist/<vid>.json also carries "v": the key, so the
indexes can be rebuilt from the files if state.json is ever lost.
Modifiers and context are NOT part of the key. Checked on a real snapshot
(18 Nov 2026, 113k item auctions): the only modifier is type 28, on 19k
auctions of 2,979 items, recipes included; 32 distinct values that look like
a content or drop tag. Within the same item and suffix, the median prices of
the different type 28 values differ by 1.03x (median over 224 groups), and
price against value has a rank correlation of -0.02 (250 groups), while
different suffixes of one item differ by 3.8x. It does not change the item.
Suffix names come from tools/wow/suffix_names.json {bonus id: "of the Bear"},
made from the game's own tables for this client (wago.tools DB2 exports,
product wowf, build 1.60.1.70959: ItemBonus rows of type 5 give an
ItemNameDescription id, whose text is the suffix). All 84 bonus ids on the
real snapshot are in it. A bonus id that is not in it is named
"<item> (version N)". The file is looked for next to this script; a copy of
this script installed on its own (the PC collector) uses the same table built
into it (_SUFFIX_CODE), plus any newer copy in the data folder.

Flip ideas (flips.json, and "flips" in the addon file) come from the listings
of one snapshot. Three kinds: "gap" (buy the cheap listings, relist under the
next one, see find_gap_flip) and "vendor" (listings under what a vendor pays,
see find_vendor_flip) work from the first hour; "deal" (a green, blue or epic
whose cheapest listings sit well under its own 7-day median, see
find_deal_flip) needs FLIP_HIST_MIN_POINTS hours of that item's (or
version's) history first, and is only looked for where there is no gap flip.
See the FLIP_ and DEAL_ limits below. Every flip, whatever its kind, carries
"usual": that item's (or version's) own 7-day median market value
(history_prices), in whole copper, 0 with fewer than FLIP_HIST_MIN_POINTS
hours of history (see add_flips in main). The addon file gets the best
FLIPS_ADDON_MAX plus extra deals and cheap flips (pick_addon_flips), so a
character with little gold and gear hunters always have ideas; flips.json
the best FLIPS_SITE_MAX.

Credentials: BNET_CLIENT_ID / BNET_CLIENT_SECRET env vars, or the file
%USERPROFILE%\\.wow-ah\\bnet.txt (id on line 1, secret on line 2) that
Setup-WoW-Key.bat writes.

Usage: python fetch_ah.py <output dir>
In GitHub Actions it writes changed=true or changed=false to $GITHUB_OUTPUT,
so the workflow only publishes when Blizzard had new data (or when a held back
download has to be remembered: then only state.json changed).
"""
import base64
import bisect
import concurrent.futures as cf
import email.utils
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone

REGION = os.environ.get("WOW_REGION", "us").lower()
HOUSE = int(os.environ.get("WOW_HOUSE", "110"))   # Alliance AH, PvP ruleset (see houses.json)
RULESET = int(os.environ.get("WOW_RULESET", "1"))  # 1 = PvP; the API ignores it for auctions
API = f"https://{REGION}.api.blizzard.com"
DYN = f"dynamic-forever-{REGION}"
STATIC_NS = [f"static-forever-{REGION}", f"static-classic1x-{REGION}", f"static-classic-{REGION}"]
HOURLY_KEEP = 35 * 24
SOLD_TIME_LEFT = {"LONG", "VERY_LONG"}   # gone before it could have expired => sold (see estimate_sold)
SOLD_MAX_GAP = 90 * 60     # older previous snapshot: vanished auctions may just have expired
SOLD_MAX_DROP = 0.30       # auction count fell more than this: probably an incomplete download
SOLD_DROP_MIN = 100        # per-endpoint drop check only for endpoints this big
# The first hour slot measured with the sold floor (estimate_sold): 2026-11-19 00:00 UTC. Older
# copies counted pulled and reposted stacks as sales, so measured hours before it are dropped
# from state.json "measured" (they would size flips for 7 days). It should be the whole UTC hour
# when both collectors (GitHub and the PC) first run this rule, never later: until it comes, no
# hour counts as measured.
SOLD_FLOOR_FROM = 1795046400
THIN_UNITS = 10            # fewer units listed than this: market value is the second cheapest unit
UNKNOWN_RETRY_SECS = 6 * 3600   # look an unnamed item up again at most this often
UNKNOWN_RETRY_MAX = 300         # and at most this many of them per run
LOOKUP_THREADS = 12
UNKNOWN_NAME = re.compile(r"^Item \d+$")   # placeholder names written by older versions
ADDON_FILE = "ForeverAH_Data.lua"
ADDON_HEADER = "-- ForeverAH price data. Generated by grantdenney.com/wow. Do not edit."
ADDON_HOURS = 168    # hourly points in the addon file (1 week)
ADDON_DAYS = 400     # daily points in the addon file
ADDON_PRICE_CAP = 2 ** 31 - 1   # the gold cap in copper; WoW reads base-36 numbers through a 32-bit strtoul
ADDON_BRIDGE_H = 6   # a run of up to 6 missing hours between two snapshots repeats the last prices
ADDON_BRIDGE_D = 1   # same for one missing day
ADDON_SOFT_MAX = 40 * 1024 * 1024   # above this the quietest items get a shorter daily history
ADDON_HARD_MAX = 90 * 1024 * 1024   # never write more (GitHub refuses files over 100 MB)
RULESET_NAMES = {1: "PvP"}

# versions of an item (see the notes at the top)
VID_MULT = 1000            # vid = item id * VID_MULT + index
VARIANT_MAX = 999          # indexes per item; later versions are only counted in the item itself
VARIANT_IDLE_HOURS = 72    # a version with nothing listed or sold for this long stops getting hourly points
ADDON_VERSION_HOURLY = 72  # ... and its addon line gets ADDON_NO_HOURLY as H, its hourly prices packed after it
# Two empty hours: "nothing listed in the last 2 hours", which is true for such a version. Two, not
# one: Goblin Ledger 1.0.0 then says "Nothing was listed in this timeframe. Try a longer one." on its
# 1D, 5D and 1W charts (one point would say it is still collecting prices). Its daily points stay.
ADDON_NO_HOURLY = "~,~;~,~;,"
SUFFIX_FILE = "suffix_names.json"
# item details (slot, item level, class ids) added to items.json; older entries are filled in
META_VERSION = 2
META_BACKFILL_MAX = 1500   # at most this many older items are looked up again per run, most listed first
# the tooltip lines the site shows under an item's name (details/<id // DETAILS_CHUNK>.json, see
# tooltip_lines and update_details)
DETAILS_DIR = "details"
DETAILS_CHUNK = 1000             # item ids per file
DETAILS_BACKFILL_PER_RUN = 400   # items without lines looked up per run, most traded first
DETAILS_THREADS = 4
DETAILS_PER_SEC = 40.0           # request pace of those lookups (Blizzard allows 100 a second)
# Those lookups stop for the run after DETAILS_MAX_SECS, or after DETAILS_MAX_FAILS failed ones in
# a row (Blizzard slow or answering 429 / 5xx), so they can never run the GitHub job past its time
# limit before the publish step; what they did not reach is still missing and comes next run.
DETAILS_MAX_SECS = 120
DETAILS_MAX_FAILS = 10
DETAILS_MAX_LINES = 40
DETAILS_MAX_CHARS = 300          # per line
TIP_KEY = "_tip"                 # the lines a lookup brought, taken out before items.json is saved

# flip ideas from one snapshot (all prices in copper)
FLIPS_FILE = "flips.json"
FLIP_AH_CUT_PCT = 5            # the auction house keeps 5% of a sale
FLIP_MIN_ROI_PERMILLE = 150    # a gap flip must make at least 15% on what it costs
FLIP_MIN_PROFIT_COMMODITY = 2000   # 20 silver, for stackable goods (the commodities list)
FLIP_MIN_PROFIT_ITEM = 5000        # 50 silver, for gear and every other single item
FLIP_MIN_LEFT = 2              # listings that must stay above what is bought
FLIP_COVER_MIN_UNITS = 5       # "thin" unless the listings above hold at least this many units
FLIP_COVER_MULT = 2            # and at least this many times the units bought
FLIP_HIST_MIN_POINTS = 4       # hourly points of history needed before the 7-day cap applies
FLIP_HIST_PCT = 0.90           # the relist price never goes above this percentile of the 7-day market
FLIP_SALES_DAYS = 7            # sold per day comes from the measured hours of this many days
FLIP_SALES_MIN_SECS = 3600     # less measured time than this: sales count as unknown
FLIP_SALES_FULL_SECS = 86400   # sales are never scaled up to a day: under a day measured, the rate is what sold
FLIP_UNKNOWN_SHARE = 0.20      # sales unknown: buy at most this share of the units listed (at least 1)
FLIP_THIN_LEFT = 5             # fewer listings than this left above: flagged "thin"
FLIP_SPIKE_RATIO = 1.5         # relist above this times the usual price: flagged "spike"
FLIP_SPIKE_GAP = 3             # no 7-day history: relist above this times the buy price is "spike"
FLIP_SPIKE_NEAR = 1.25         # unless at least FLIP_SPIKE_CONFIRM listings sit within this of the next one
FLIP_SPIKE_CONFIRM = 3
FLIP_VENDOR_MIN_PROFIT = 100   # 1 silver, the least a vendor flip must make
FLIP_VENDOR_MIN_MARGIN_PERMILLE = 100   # each listing bought must make 10% at the vendor (not 1c on 5s)
FLIP_CHEAP_COST = 50000        # 5 gold: a flip costing at most this is "cheap" (see pick_addon_flips)
FLIPS_ADDON_MAX = 60           # flips in the addon file: the best this many by rank,
FLIPS_ADDON_DEALS = 40         # plus up to this many deals that are not in yet,
FLIPS_ADDON_CHEAP = 40         # plus up to this many flips costing at most FLIP_CHEAP_COST not in yet
FLIPS_SITE_MAX = 400           # flips in flips.json
# deals: a green, blue or epic priced well under its own usual price (see find_deal_flip)
DEAL_QUALITIES = ("UNCOMMON", "RARE", "EPIC", "LEGENDARY")   # item qualities a deal is looked for in
DEAL_MAX_SHARE = 0.70          # a deal buys only listings priced at most this share of the usual price
DEAL_MIN_PROFIT = 2500         # 25 silver, the least a deal must make
DEAL_UNSURE_LISTED = 2         # 7-day median units listed under this: flagged "unsure"


# -- HTTP ---------------------------------------------------------------------
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


def header(hdr, name):
    """A response header, whatever its capitalisation."""
    low = name.lower()
    for k, v in (hdr or {}).items():
        if str(k).lower() == low:
            return v
    return ""


def parse_http_date(s):
    """'Tue, 17 Nov 2026 22:25:03 GMT' -> epoch seconds, or None if it is not a date."""
    if not s:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(str(s))
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def tell_workflow(changed):
    """GitHub Actions: changed=true/false for the publish step. Does nothing elsewhere."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("changed=%s\n" % ("true" if changed else "false"))


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


# -- pricing ------------------------------------------------------------------
def copper(v):
    """A computed price -> whole copper, rounded half up, never below 1c."""
    return max(1, int(v + 0.5))


def _trim(units):
    """Drops units more than 1.5 standard deviations from the quantity
    weighted mean (a lone cheap or silly listing at either end). Keeps order."""
    n = sum(q for _, q in units)
    mean = sum(p * q for p, q in units) / n
    sd = (sum(q * (p - mean) ** 2 for p, q in units) / n) ** 0.5
    if sd > 0:
        kept = [(p, q) for p, q in units if abs(p - mean) <= 1.5 * sd]
        if kept:
            return kept, mean
    return list(units), mean


def _nth(units, k):
    """The price of the k-th unit (0 = the cheapest) of sorted (price, qty)."""
    acc = 0
    for p, q in units:
        acc += q
        if k < acc:
            return p
    return units[-1][0]


def market_value(units):
    """units: list of (unit_price, qty) sorted by price. Prices may have
    fractions of a copper (a stack of 200 for 80c is 0.4c each); they are only
    rounded at the end, and the result is never below 1c.

    Deep markets (THIN_UNITS units or more), TSM-DBMarket-like:
      1. always take the cheapest 15% of units (at least the cheapest 1),
      2. keep going up to 30% while the price does not jump more than 20%
         over the previous listing,
      3. drop units more than 1.5 standard deviations from the mean of that
         sample (this removes a lone cheap or silly listing at either end),
      4. return the quantity-weighted mean of what is left.
    One troll listing at 1c in front of a deep stack does not drag the market
    value down to 1c.

    Thin markets (fewer than THIN_UNITS units): step 1 would be just the
    single cheapest unit, so instead the market value is the middle one of
    the cheapest 3 units, that is the second cheapest unit (with only 2 units
    the cheaper one, with 1 that one). It is always a price someone really
    listed at, it stays with the cheap end of the list where buyers actually
    buy, and pricier listings further up can never pull it up. One cheap or
    silly listing among 3 or more cannot set it alone. At 9 units it is the
    same few cheapest units the deep rule looks at from 10 units, so one sale
    at the boundary does not make it jump. With only 1 or 2 listings the
    auctions count (latest.json "n", the addon's auctions field) tells the
    site and the addon how thin it is. Nothing is hidden here."""
    units = [(float(p), int(q)) for p, q in units if q > 0]
    total = sum(q for _, q in units)
    if total <= 0:
        return 0
    if total < THIN_UNITS:
        return copper(_nth(units, (min(3, total) - 1) // 2))
    lower = max(1, int(round(total * 0.15)))
    upper = max(lower, int(round(total * 0.30)))
    picked, n, prev = [], 0, None
    for p, q in units:
        if n >= upper:
            break
        if n >= lower and prev is not None and p > 1.2 * prev:
            break
        take = min(q, upper - n)
        picked.append((p, take))
        n += take
        prev = p
    kept, mean = _trim(picked)
    c = sum(q for _, q in kept)
    if not c:
        return copper(mean)
    return copper(sum(p * q for p, q in kept) / c)


def variant_key(item):
    """An auction item's variant key: its bonus ids sorted ascending, joined
    with ":" ("" when it has none). Modifiers and context are left out on
    purpose (see the notes at the top)."""
    ids = []
    for b in (item or {}).get("bonus_lists") or []:
        try:
            ids.append(int(b))
        except (TypeError, ValueError):
            continue
    return ":".join(str(b) for b in sorted(ids))


def variant_id(registry, iid, key, add=True):
    """The vid of (item id, key), or None (no key, or the item already has
    VARIANT_MAX versions). registry: {str(item id): {key: index}}; a new key
    gets the next index (changed in place) unless add is False."""
    if not key:
        return None
    m = registry.get(str(iid))
    idx = m.get(key) if m else None
    if idx is None:
        if not add:
            return None
        m = registry.setdefault(str(iid), {})
        top = max((int(v) for v in m.values()), default=0)
        if top >= VARIANT_MAX:
            return None
        idx = top + 1
        m[key] = idx
    return int(iid) * VID_MULT + int(idx)


def registry_vids(registry):
    """{vid: (item id, key, index)} for every version in the registry."""
    out = {}
    for sid, keys in (registry or {}).items():
        for key, idx in (keys or {}).items():
            out[int(sid) * VID_MULT + int(idx)] = (int(sid), str(key), int(idx))
    return out


def aggregate(auctions, registry=None):
    """Raw API auctions -> (per, snap, vper).
    per:  item id -> list of (unit price, qty), one entry per auction. The unit
          price keeps fractions of a copper; an auction with a buyout is never
          dropped, however cheap per unit.
    vper: vid -> the same list for the auctions of that version only (the
          auctions whose item has bonus ids). They are in `per` as well.
    snap: auction id -> [item, qty, unit (whole copper, for reference), time left]
          plus the vid as a 5th entry when the auction is a version.
    registry: state.json "variants" (changed in place: new keys get the next
          index, in the order they first appear). None = a throwaway one.
    Bid-only auctions (no buyout) are skipped: no price anyone can act on."""
    if registry is None:
        registry = {}
    per, snap, vper = {}, {}, {}
    for a in auctions:
        it = a.get("item") or {}
        iid = it.get("id")
        try:
            q = a.get("quantity")
            q = 1 if q is None else int(q)      # no quantity field = a single item
        except (TypeError, ValueError):
            continue
        if not iid or q <= 0:
            continue
        unit = a.get("unit_price")
        if not unit:
            bo = a.get("buyout")
            if not bo:
                continue
            unit = bo / q
        unit = float(unit)
        if unit <= 0:
            continue
        per.setdefault(iid, []).append((unit, q))
        vid = variant_id(registry, iid, variant_key(it))
        if vid:
            vper.setdefault(vid, []).append((unit, q))
        aid = a.get("id")
        if aid is not None:
            e = [iid, q, copper(unit), a.get("time_left", "")]
            if vid:
                e.append(vid)
            snap[str(aid)] = e
    return per, snap, vper


# -- estimated sales ----------------------------------------------------------
def _floor_key(e, cur=None):
    """Which listings share a sold floor: (item id, vid) for a version, (item id, 0)
    otherwise, so two versions (or a version and the plain item) never share one. An
    auction from an older snapshot without vids takes its vid from the new snapshot
    (cur) when it is still listed."""
    vid = e[4] if len(e) > 4 else None
    if not vid and cur is not None and len(cur) > 4:
        vid = cur[4]
    return e[0], vid or 0


def estimate_sold(prev, snap, by_variant=False):
    """Units that most likely sold between two snapshots, per item id (or per
    vid when by_variant: auctions that are not a version are left out). The
    previous snapshot may come from an older state file without vids.

    A stack that got smaller sold the difference. An auction that is gone and
    had LONG or VERY_LONG left (so it cannot have expired) sold all of it, but
    only when no cheaper auction of the same item (of the same version, for a
    version) from the previous snapshot is still listed. Buyers always take the
    cheapest first, so a listing that is gone while a cheaper one stayed up was
    pulled or reposted, not bought. The floor is the cheapest unit price among
    the previous snapshot's auctions still listed now (whole or partly bought);
    a gone auction priced at the floor or under counts (ties count)."""
    floor = {}
    for aid, e in prev.items():
        cur = snap.get(aid)
        if cur is None:
            continue
        try:
            unit = float(e[2])
        except (TypeError, ValueError, IndexError):
            continue
        key = _floor_key(e, cur)
        if unit < floor.get(key, math.inf):
            floor[key] = unit
    sold = {}
    for aid, e in prev.items():
        try:
            iid, q, unit, tl = e[0], int(e[1]), float(e[2]), e[3]
        except (TypeError, ValueError, IndexError):
            continue
        sid = iid
        if by_variant:
            sid = e[4] if len(e) > 4 else None
            if not sid:
                continue
        cur = snap.get(aid)
        if cur is None:
            if tl in SOLD_TIME_LEFT and unit <= floor.get(_floor_key(e), math.inf):
                sold[sid] = sold.get(sid, 0) + q
        elif cur[1] < q:
            sold[sid] = sold.get(sid, 0) + (q - cur[1])
    return sold


def incomplete_download(prev, snap, endpoints=None, prev_endpoints=None, failed=()):
    """Why this download looks incomplete next to the previous snapshot, or None.
    A list that did not download only counts when it had auctions last time
    (or last time is not known): a list that is always empty, or always
    fails, loses nothing."""
    for name in failed:
        if prev_endpoints is None or int(prev_endpoints.get(name) or 0) > 0:
            return f"the {name} list did not download"
    if prev and len(snap) < (1 - SOLD_MAX_DROP) * len(prev):
        drop = int(round(100 * (1 - len(snap) / len(prev))))
        return f"the auction count fell {drop}% since the previous snapshot (probably an incomplete download)"
    for name, n in (endpoints or {}).items():
        p = int((prev_endpoints or {}).get(name) or 0)
        if p >= SOLD_DROP_MIN and n < (1 - SOLD_MAX_DROP) * p:
            drop = int(round(100 * (1 - n / p)))
            return f"the {name} count fell {drop}% since the previous snapshot (probably an incomplete download)"
    return None


def sold_check(prev, snap, prev_time, data_time, endpoints=None, prev_endpoints=None, failed=()):
    """Whether this snapshot can be compared with the previous one for sales.
    Returns (reason it cannot or None, minutes since the previous snapshot or None)."""
    if not prev:
        return "first snapshot, nothing to compare with yet", None
    if not prev_time:
        return "the previous snapshot has no time", None
    gap = data_time - prev_time
    gap_min = int(round(gap / 60))
    if gap > SOLD_MAX_GAP:
        return (f"the previous snapshot is {gap_min} minutes older (more than {SOLD_MAX_GAP // 60}), "
                "so sales cannot be told apart from expired auctions"), gap_min
    return incomplete_download(prev, snap, endpoints, prev_endpoints, failed), gap_min


def measured_secs(state):
    """state.json "measured": {hour slot: seconds of sales measured in it}. A
    slot with two snapshots adds both comparisons up. Older files kept a plain
    list of hour slots: each of those counts as one hour."""
    m = state.get("measured") if isinstance(state, dict) else None
    if isinstance(m, dict):
        return {int(k): max(0, int(v or 0)) for k, v in m.items()}
    return {int(t): 3600 for t in (m or [])}


def sales_seconds(measured, hour):
    """Seconds of measured sales in the 24 hourly slots ending at `hour`.
    `measured` is {slot: seconds}, or a set of slots (one hour each)."""
    lo = hour - 23 * 3600
    pairs = measured.items() if isinstance(measured, dict) else ((t, 3600) for t in set(measured))
    return sum(int(s) for t, s in pairs if lo <= t <= hour and (hour - t) % 3600 == 0)


def sales_hours(measured, hour):
    """How many hours of sales the sold estimates of the last 24 hourly slots
    cover, rounded to whole hours (0..24). The site and the addon scale
    sold24 by 24 / salesHours, so this counts the minutes the comparisons
    really cover: a slot with a 2 minute comparison is not a full hour."""
    return max(0, min(24, int(sales_seconds(measured, hour) / 3600 + 0.5)))


# -- item metadata ------------------------------------------------------------
def is_unknown(meta):
    """No usable name: never looked up, lookup failed, or an old 'Item 123' placeholder."""
    if not meta or meta.get("unknown"):
        return True
    name = str(meta.get("name") or "")
    return not name or bool(UNKNOWN_NAME.match(name))


def item_name(meta):
    """The name to publish: "" when Blizzard has none for the item yet."""
    return "" if is_unknown(meta) else str(meta["name"])


def unknown_entry(tried):
    return {"name": "", "icon": "", "quality": "COMMON", "sell": 0, "buy": 0, "cls": "", "sub": "",
            "lvl": 0, "stack": 0, "ilvl": 0, "slot": "", "slotType": "", "clsId": None, "subId": None,
            "unknown": True, "tried": int(tried)}


def _id_or_none(obj):
    """An API reference's numeric id (0 is a real class id: Consumable), None when missing."""
    try:
        v = (obj or {}).get("id")
        return None if v is None else int(v)
    except (TypeError, ValueError, AttributeError):
        return None


def _meta(iid, item, icon):
    inv = item.get("inventory_type") or {}
    slot_type = str(inv.get("type") or "")
    return {
        "name": str(item.get("name") or ""),
        "icon": icon,
        "quality": ((item.get("quality") or {}).get("type") or "COMMON"),
        "sell": int(item.get("sell_price") or 0),
        "buy": int(item.get("purchase_price") or 0),
        "cls": ((item.get("item_class") or {}).get("name") or ""),
        "sub": ((item.get("item_subclass") or {}).get("name") or ""),
        "lvl": int(item.get("required_level") or 0),
        "stack": int(item.get("max_count") or 0),
        "ilvl": int(item.get("level") or 0),
        # "Chest", "One-Hand"...; "" for things you cannot wear
        "slot": "" if slot_type in ("", "NON_EQUIP") else str(inv.get("name") or ""),
        "slotType": slot_type,
        "clsId": _id_or_none(item.get("item_class")),
        "subId": _id_or_none(item.get("item_subclass")),
        "mv": META_VERSION,
    }


DETAIL_KEYS = ("cls", "sub", "lvl", "ilvl", "slot", "slotType", "clsId", "subId", "mv")


def fetch_details(iid):
    """The details older items.json entries lack (slot, item level, class ids),
    from the first static namespace that knows the item; no icon request.
    Returns (iid, fields): None when a request failed (tried again next run),
    {} when no namespace knows the item. fields also carries the item's
    tooltip lines under TIP_KEY ([] when no namespace knows it), which main
    takes out again before items.json is saved (take_tips)."""
    failed = False
    for ns in STATIC_NS:
        try:
            item, _ = api(f"/data/wow/item/{iid}", ns)
        except Exception:
            failed = True
            continue
        if item:
            m = _meta(iid, item, "")
            fields = {k: m[k] for k in DETAIL_KEYS}
            fields[TIP_KEY] = safe_tooltip_lines(item)
            return iid, fields
    return iid, (None if failed else {TIP_KEY: []})


def backfill_meta(items, counts, fetch=None, limit=META_BACKFILL_MAX, threads=LOOKUP_THREADS):
    """Fills in the details of named items cached by an older version, at most
    `limit` per run, the most listed (counts: item id -> auctions now) first.
    Changes `items` in place. Returns (filled in, still missing)."""
    fetch = fetch or fetch_details
    todo = [s for s, m in items.items() if not is_unknown(m) and int(m.get("mv") or 0) < META_VERSION]
    todo.sort(key=lambda s: (-int(counts.get(int(s), 0)), int(s)))
    batch = [int(s) for s in todo[:max(0, int(limit))]]
    done = 0
    if batch:
        with cf.ThreadPoolExecutor(max(1, threads)) as ex:
            for iid, fields in ex.map(fetch, batch):
                if fields is None:
                    continue
                m = items[str(iid)]
                for k, v in (("ilvl", 0), ("slot", ""), ("slotType", ""), ("clsId", None), ("subId", None)):
                    m.setdefault(k, v)
                for k, v in fields.items():
                    if k in ("cls", "sub") and not v:
                        continue            # keep a class name we already had
                    m[k] = v
                m["mv"] = META_VERSION
                done += 1
    return done, len(todo) - done


# The same table as suffix_names.json (bonus id -> suffix name), built in, so a copy of this
# script on its own (the PC collector installs just this file) still names versions. Each entry
# is <id minus the previous id>.<index into _SUFFIX_WORDS>. test_datafile.py checks it matches.
_SUFFIX_WORDS = (
    "of Intellect", "of Spirit", "of Stamina", "of the Owl", "of the Eagle", "of the Whale",
    "of the Monkey", "of the Bear", "of the Gorilla", "of the Falcon", "of the Boar", "of the Wolf",
    "of the Tiger", "of Agility", "of Strength", "of Power", "of Magic", "of Healing", "of Defense",
    "of Regeneration", "of Concentration", "of Eluding", "of Arcane Protection",
    "of Fire Protection", "of Frost Protection", "of Nature Protection", "of Shadow Protection",
    "of the Sorcerer", "of the Elder", "of the Channeler", "of the Physician", "of the Prophet",
    "of the Hierophant", "of the Bandit", "of the Beast", "of the Champion", "of the Defender",
    "of the Knight", "of Arcane Wrath", "of Fiery Wrath", "of Frozen Wrath", "of Holy Wrath",
    "of Nature's Wrath", "of Shadow Wrath", "of Arcane Resistance", "of Frost Resistance",
    "of Nature Resistance", "of Shadow Resistance", "of Fire Resistance",
)
_SUFFIX_CODE = (
    "12688.0 1.1 1.2 1.3 27.4 1.5 1.3 1.6 1.7 1.8 1.9 1.10 1.11 1.12 1.13 1.0 1.1 1.2 1.14 1.15 1.16 "
    "1.17 1.18 1.19 1.20 1.21 1.22 1.23 1.24 1.25 1.26 1.27 1.28 1.29 1.30 1.31 1.32 1.33 1.34 1.35 "
    "1.36 1.37 1.38 1.39 1.40 1.41 1.42 1.43 144.27 1.27 1.28 1.28 1.29 1.29 1.30 1.30 1.31 1.31 1.32 "
    "1.32 1.16 1.16 1.17 1.17 3.27 1.28 1.30 1.31 1.32 1.27 1.29 1.28 1.29 1.30 1.31 1.32 1.33 1.34 "
    "1.35 1.36 1.37 1.33 1.34 1.35 1.36 1.37 1.4 1.5 1.3 1.6 1.7 1.8 1.9 1.10 1.11 1.12 1.0 1.13 1.1 "
    "1.2 1.14 1.15 1.16 1.17 1.44 1.23 1.24 1.25 1.26 1.41 1.30 1.31 1.32 1.34 1.37 1.18 1.35 1.36 "
    "1.38 1.39 1.40 1.43 1.27 1.28 1.29 1.42 1.33 1.45 1.46 1.47 1.48 3.44 1.45 1.46 1.47 1.48 1.44 "
    "1.45 1.46 1.47 1.48 1.44 1.45 1.46 1.47 1.48 1.44 1.45 1.47 1.46 1.48 1.4 1.5 1.6 1.7 1.10 1.11 "
    "1.12 1.13 1.2 1.14 1.15 1.34 1.33 1.41 1.35 1.36 1.4 1.5 1.6 1.7 1.10 1.11 1.12 1.13 1.2 1.14 "
    "1.15 1.34 1.33 1.41 1.35 1.36 1.3 1.0 1.1 1.30 1.31 1.32 1.27 1.28 1.29 1.16 1.17 1.16 1.17 1.38 "
    "1.39 1.40 1.41 1.42 1.43 1.19 1.20 1.4 1.5 1.3 1.0 1.1 1.30 1.31 1.32 1.27 1.28 1.29 1.16 1.17 "
    "1.38 1.39 1.40 1.41 1.42 1.43 1.38 1.39 1.40 1.42 1.43 1.3 1.1 1.0 1.27 1.28 1.29 1.31 1.30 1.32 "
    "1.44 1.8 1.9 1.16 1.17 1.18 1.27 1.28 1.29 1.30 1.31 1.32 1.35 1.36 1.18 1.4 1.5 1.3 1.0 1.1 1.2 "
    "1.22 1.23 1.24 1.25 1.26 1.30 1.31 1.32 1.27 1.28 1.29 1.16 1.17 1.38 1.39 1.40 1.41 1.42 1.43 "
    "1.4 1.5 1.3 1.6 1.7 1.9 1.8 1.10 1.11 1.12 1.13 1.0 1.1 1.2 1.14 1.15 1.16 1.17 1.18 1.44 1.23 "
    "1.24 1.25 1.26 1.38 1.42 1.27 1.28 1.29 1.30 1.31 1.32 1.33 1.34 1.35 5.22 1.23 1.24 1.25 1.26 "
    "1.37 6.4 1.5 1.6 1.7 1.8 1.10 1.12 1.34 1.37 1.18 1.35 1.36 1.4 1.5 1.3 1.0 1.1 1.21 572.8 1.9 "
    "1.23 1.24 1.25 1.26 1.42 1.37"
)


def builtin_suffix_names():
    """{bonus id: "of the Bear"} from the table built into this script."""
    names, bid = {}, 0
    for tok in _SUFFIX_CODE.split():
        delta, idx = tok.split(".")
        bid += int(delta)
        names[bid] = _SUFFIX_WORDS[int(idx)]
    return names


def _read_suffix_file(path):
    raw = load(path, None)
    names = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                if isinstance(v, str) and v.strip():
                    names[int(k)] = v.strip()
            except (TypeError, ValueError):
                continue
    return raw, names


def load_suffix_names(out):
    """{bonus id: "of the Bear"} from suffix_names.json next to this script.
    Without that file (the PC collector installs this script on its own), the
    table built into this script, plus anything newer a copy in the data folder
    has (written there by a collector that had the file). A copy is written to
    the data folder so every collector finds it."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), SUFFIX_FILE)
    there = os.path.join(out, SUFFIX_FILE)
    raw, names = _read_suffix_file(here)
    if names:
        if load(there, None) != raw:
            try:
                save(there, raw)
            except OSError:
                pass
        return names
    names = builtin_suffix_names()
    names.update(_read_suffix_file(there)[1])
    return names


def variant_name(base_meta, key, index, suffixes):
    """'Archer's Cloak of the Bear', or 'Archer's Cloak (version 3)' when a bonus
    id has no known suffix name; "" when the item itself has no name yet."""
    base = item_name(base_meta)
    if not base:
        return ""
    parts = []
    for b in str(key).split(":"):
        try:
            n = suffixes.get(int(b))
        except (TypeError, ValueError):
            n = None
        if n:
            parts.append(n)
    if parts:
        return base + " " + " ".join(parts)
    return f"{base} (version {int(index)})"


def fetch_item(iid):
    """Looks the item up in each static namespace in turn. The first one that
    has a name wins. If one knows the item but has no name for it, its other
    details are kept and the item stays unknown (it is retried later).
    Returns (iid, meta or None). meta also carries the item's tooltip lines
    under TIP_KEY, from the same response (no extra request), which main
    takes out again before items.json is saved (take_tips)."""
    partial = None
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
        meta = _meta(iid, item, icon)
        meta[TIP_KEY] = safe_tooltip_lines(item)
        if meta["name"] and not UNKNOWN_NAME.match(meta["name"]):
            return iid, meta
        if partial is None:
            partial = meta
    if partial is not None:
        partial["name"] = ""
        partial["unknown"] = True
        return iid, partial
    return iid, None


def refresh_items(items, listed_ids, now, fetch=None, retry_max=UNKNOWN_RETRY_MAX, threads=LOOKUP_THREADS):
    """Looks up items seen for the first time, and retries items that still
    have no name (each at most every UNKNOWN_RETRY_SECS, at most `retry_max` per
    run, the ones listed right now and the longest waiting first). Changes
    `items` in place. Unknown items keep a "" name and their prices are still
    published. Returns (new looked up, retried, names found on retry)."""
    fetch = fetch or fetch_item
    for m in items.values():            # older files cached "Item 123" placeholders
        if is_unknown(m):
            m["name"] = ""
            m["unknown"] = True
    listed = set(str(i) for i in listed_ids)
    new = sorted(int(s) for s in listed if s not in items)
    due = [s for s, m in items.items() if m.get("unknown") and now - int(m.get("tried") or 0) >= UNKNOWN_RETRY_SECS]
    due.sort(key=lambda s: (s not in listed, int(items[s].get("tried") or 0), int(s)))
    due = [int(s) for s in due[:max(0, int(retry_max))]]
    todo = new + due
    found = 0
    if not todo:
        return 0, 0, 0
    retried = set(due)
    with cf.ThreadPoolExecutor(max(1, threads)) as ex:
        for iid, meta in ex.map(fetch, todo):
            sid = str(iid)
            if meta and not is_unknown(meta):
                items[sid] = meta
                if iid in retried:
                    found += 1
                continue
            entry = dict(items.get(sid) or unknown_entry(now))
            if meta:                    # Blizzard knows it but has no name: keep the details
                entry.update(meta)
            entry.update({"name": "", "unknown": True, "tried": int(now)})
            items[sid] = entry
    return len(new), len(due), found


# -- item details for the site (the tooltip block under the item name) -------
# details/<id // DETAILS_CHUNK>.json = {"<item id>": [[text, colour], ...]}:
# the lines of the item's in-game tooltip under its name, built from Blizzard's
# item API response (tooltip_lines). A line with a right-hand part (slot and
# type, damage and speed) is [text, colour, right]. Colours: w white, g green,
# y gold, q grey, r red, b blue. [] = Blizzard has nothing to show for it.
# Versions use their item's lines (the site adds the version's name). A new
# item gets its lines from the lookup that names it; older items are filled in
# DETAILS_BACKFILL_PER_RUN a run, most traded first. What is missing is read
# from the published chunk files themselves, so the GitHub job and the PC
# collector, which publish to the same branch, share that work with no state
# of their own, and state.json and every other file stay as they were.
_TIP_COLOURS = (("w", (255, 255, 255)), ("g", (0, 255, 0)), ("y", (255, 209, 0)), ("q", (157, 157, 157)),
                ("r", (255, 32, 32)), ("b", (102, 187, 255)))
_TIP_CODES = re.compile(r"\|c[0-9a-fA-F]{8}|\|r|\|n")      # WoW colour codes and line breaks
_TIP_REQS = ("playable_classes", "playable_races", "level", "skill", "ability", "reputation", "faction")
_TIP_UNWORN = ("", "NON_EQUIP", "BAG", "QUIVER", "AMMO")    # slots that get no computed item level


def _tip_dict(v):
    return v if isinstance(v, dict) else {}


def _tip_list(v):
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _tip_text(v):
    """One line's text: colour codes out, whitespace collapsed, at most DETAILS_MAX_CHARS."""
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        return ""
    return " ".join(_TIP_CODES.sub(" ", str(v)).split())[:DETAILS_MAX_CHARS]


def _tip_colour(c, default="w"):
    """An API colour {r, g, b, a} -> the nearest tooltip colour key."""
    c = _tip_dict(c)
    try:
        rgb = (int(c["r"]), int(c["g"]), int(c["b"]))
    except (KeyError, TypeError, ValueError):
        return default
    return min(_TIP_COLOURS, key=lambda kv: sum((a - b) ** 2 for a, b in zip(kv[1], rgb)))[0]


def _tip_shown(part):
    """(text, colour) of an API part shaped {display_string} or {display: {display_string, color}}."""
    part = _tip_dict(part)
    d = _tip_dict(part.get("display"))
    if d.get("display_string"):
        return d["display_string"], _tip_colour(d.get("color"))
    return part.get("display_string"), "w"


def tooltip_lines(item):
    """The lines of an item's in-game tooltip under its name, from Blizzard's
    item API response (its preview_item; the top-level fields when there is
    none), in the game's order: item level, binding, unique, quest, slot and
    type (or bag size), damage and speed, armor, block, stats, durability,
    requirements, Use / Equip / Chance on hit (green), recipe reagents, set
    and set bonuses, flavor text (gold, in quotes). The name, quality and sell
    price are left out: the site shows those itself. A part that is missing
    or odd is skipped; the other lines still come."""
    item = _tip_dict(item)
    pv = _tip_dict(item.get("preview_item")) or item
    out = []

    def add(text, colour="w", right=None):
        t = _tip_text(text)
        if t and len(out) < DETAILS_MAX_LINES:
            r = _tip_text(right) if right is not None else ""
            out.append([t, colour, r] if r else [t, colour])

    inv = _tip_dict(pv.get("inventory_type")) or _tip_dict(item.get("inventory_type"))
    slot = str(inv.get("type") or "")
    spells = _tip_list(pv.get("spells"))
    desc = pv.get("description")

    def level():
        lv = pv.get("level")
        if isinstance(lv, dict):             # Blizzard shows it: so does the site
            add(lv.get("display_string"), "y")
        elif slot not in _TIP_UNWORN and int(item.get("level") or 0) > 0:
            add("Item Level %d" % int(item["level"]), "y")

    def head():
        add(_tip_dict(pv.get("binding")).get("name"))
        for k in ("unique_equipped", "limit_category"):
            if isinstance(pv.get(k), str):
                add(pv[k])
        add(_tip_shown(pv.get("item_starts_quest"))[0])
        if isinstance(pv.get("crafting_reagent"), str):
            add(pv["crafting_reagent"], "b")

    def kind():
        bag = _tip_shown(pv.get("container_slots"))[0]
        if bag:
            add(bag)
        elif slot not in ("", "NON_EQUIP"):
            sub = "" if pv.get("is_subclass_hidden") else \
                _tip_dict(pv.get("item_subclass") or item.get("item_subclass")).get("name")
            if inv.get("name"):
                add(inv["name"], "w", sub)
            else:
                add(sub)

    def weapon():
        w = _tip_dict(pv.get("weapon"))
        dmg = _tip_dict(w.get("damage")).get("display_string")
        speed = _tip_dict(w.get("attack_speed")).get("display_string")
        if dmg:
            add(dmg, "w", speed)
        else:
            add(speed)
        extra = w.get("additional_damage")
        for x in _tip_list(extra if isinstance(extra, list) else [extra]):
            add(x.get("display_string"))
        add(_tip_dict(w.get("dps")).get("display_string"))

    def defence():
        add(*_tip_shown(pv.get("armor")))
        add(*_tip_shown(pv.get("shield_block")))

    def stats():
        for s in _tip_list(pv.get("stats")):
            text, colour = _tip_shown(s)
            name, v = _tip_dict(s.get("type")).get("name"), s.get("value")
            if not text and name and isinstance(v, (int, float)) and not isinstance(v, bool):
                text = "%+d %s" % (int(v), name)
            add(text, colour)

    def durability():
        add(_tip_dict(pv.get("durability")).get("display_string"))

    def requirements():
        req = _tip_dict(pv.get("requirements"))
        for k in [k for k in _TIP_REQS if k in req] + sorted(k for k in req if k not in _TIP_REQS):
            add(_tip_dict(req[k]).get("display_string"))
        if pv is item and int(item.get("required_level") or 0) > 1:     # no preview_item at all
            add("Requires Level %d" % int(item["required_level"]))

    def effects():
        nonlocal desc
        for s in spells:
            add(s.get("description"), _tip_colour(s.get("display_color"), "g"))
        # a recipe's "Teaches you how to make..." is its Use line in the game
        if not spells and desc and _id_or_none(pv.get("item_class") or item.get("item_class")) == 9:
            add("Use: " + _tip_text(desc), "g")
            desc = None
        reagents = _tip_dict(pv.get("recipe")).get("reagents_display_string")
        if _tip_text(reagents):
            add("Requires " + _tip_text(reagents))

    def item_set():
        st = _tip_dict(pv.get("set"))
        add(st.get("display_string") or _tip_dict(st.get("item_set")).get("name"), "y")
        for e in _tip_list(st.get("effects")):
            add(e.get("display_string"), "q")

    def flavor():
        t = _tip_text(desc).strip('"').strip()[:DETAILS_MAX_CHARS - 2].strip()
        if t:
            add('"%s"' % t, "y")

    for part in (level, head, kind, weapon, defence, stats, durability, requirements, effects, item_set, flavor):
        try:
            part()
        except Exception:                    # an odd field never costs the other lines
            continue
    return out


def safe_tooltip_lines(item):
    """tooltip_lines that never raises ([] if it ever did), so a lookup is never lost to it."""
    try:
        return tooltip_lines(item)
    except Exception:
        return []


def take_tips(items):
    """Takes the tooltip lines that fetch_item / fetch_details left in item
    entries (TIP_KEY) out again, so items.json stays exactly as it was.
    Returns {item id: lines}."""
    tips = {}
    for sid, m in items.items():
        if isinstance(m, dict) and TIP_KEY in m:
            lines = m.pop(TIP_KEY)
            try:
                if isinstance(lines, list):
                    tips[int(sid)] = lines
            except (TypeError, ValueError):
                continue
    return tips


_PACE = {"lock": threading.Lock(), "next": 0.0}


def _pace(per_sec=None):
    """Spaces the details lookups at most `per_sec` a second over all threads."""
    gap = 1.0 / max(1.0, float(per_sec or DETAILS_PER_SEC))
    with _PACE["lock"]:
        now = time.time()
        if _PACE["next"] - now > 5:      # the clock went back: start again
            _PACE["next"] = now
        t = max(now, _PACE["next"])
        _PACE["next"] = t + gap
    if t > now:
        time.sleep(min(1.0, t - now))


def fetch_tip(iid, stop=None):
    """One older item's tooltip lines for the details backfill, from the first
    static namespace that knows it (no icon request, paced to DETAILS_PER_SEC).
    Returns (iid, lines): None when a request failed, or when stop() says the
    run's lookups are over before a namespace answered (tried again next run),
    [] when no namespace knows the item."""
    failed = False
    for ns in STATIC_NS:
        if stop is not None and stop():
            return iid, None
        _pace()
        try:
            item, _ = api(f"/data/wow/item/{iid}", ns)
        except Exception:
            failed = True
            continue
        if item:
            return iid, safe_tooltip_lines(item)
    return iid, (None if failed else [])


def details_chunk(iid):
    """The chunk file number an item's lines go in."""
    return int(iid) // DETAILS_CHUNK


def load_details(out):
    """{chunk number: {str(item id): lines}} from the published chunk files."""
    d = os.path.join(out, DETAILS_DIR)
    chunks = {}
    if not os.path.isdir(d):
        return chunks
    for f in os.listdir(d):
        if not f.endswith(".json"):
            continue
        try:
            n = int(f[:-5])
        except ValueError:
            continue
        raw = load(os.path.join(d, f), None)
        if isinstance(raw, dict):
            chunks[n] = {str(k): v for k, v in raw.items() if isinstance(v, list)}
    return chunks


def write_details(out, chunks, changed):
    """Writes the chunk files in `changed`, ids in order (a file that did not change is not touched)."""
    for n in sorted(changed):
        c = chunks.get(n) or {}
        save(os.path.join(out, DETAILS_DIR, f"{n}.json"),
             {k: c[k] for k in sorted(c, key=lambda s: (len(s), s))})


def details_todo(items, chunks, latest, vols, vinfo=None, limit=None):
    """The items to look up for their lines this run (at most `limit`,
    default DETAILS_BACKFILL_PER_RUN): named items the site lists (in latest;
    a version counts as its item) that no published chunk has, the most gold
    traded a day first (market value x sold24), then the most sold, the most
    listed, the lowest id. Returns (ids, how many miss)."""
    limit = DETAILS_BACKFILL_PER_RUN if limit is None else limit
    have = set()
    for c in chunks.values():
        have.update(c)
    vinfo = vinfo or {}
    cand = {}
    for sid in latest:
        try:
            eid = int(sid)
        except (TypeError, ValueError):
            continue
        iid = vinfo[eid][0] if eid in vinfo else eid
        if iid in cand or str(iid) in have or is_unknown(items.get(str(iid))):
            continue
        node = latest.get(str(iid)) or latest.get(sid) or {}
        price = int(node.get("high") or node.get("low") or 0)
        sold = int(vols.get(str(iid)) or 0)
        cand[iid] = (-price * sold, -sold, -int(node.get("n") or 0), iid)
    todo = sorted(cand, key=cand.get)
    return todo[:max(0, int(limit))], len(todo)


def update_details(out, items, tips, latest, vols, vinfo=None, fetch=None, limit=None, threads=None,
                   max_secs=None, max_fails=None):
    """details/<n>.json: the lines this run's item lookups brought (tips:
    item id -> lines, see take_tips), plus up to `limit` (default
    DETAILS_BACKFILL_PER_RUN) items no published chunk has yet
    (details_todo), looked up now. A failed lookup is left for the next run.
    The lookups stop for the run once `max_secs` (default DETAILS_MAX_SECS)
    have passed or `max_fails` (default DETAILS_MAX_FAILS) of them failed in
    a row; the items they did not reach are still missing from the chunks,
    so the next run, of either collector, looks them up. The lines of this
    run's own lookups are always written. Only chunk files that changed are
    written.
    Returns (lines from this run's lookups, filled in, still missing)."""
    threads = DETAILS_THREADS if threads is None else threads
    max_fails = max(1, int(DETAILS_MAX_FAILS if max_fails is None else max_fails))
    deadline = time.time() + (DETAILS_MAX_SECS if max_secs is None else max_secs)
    run = {"lock": threading.Lock(), "fails": 0, "why": ""}

    def stop():
        with run["lock"]:
            if not run["why"] and time.time() >= deadline:
                run["why"] = "out of time"
            return bool(run["why"])

    fetch = fetch or (lambda i: fetch_tip(i, stop))

    def lookup(iid):
        if stop():
            return iid, None                 # not reached this run: still missing, so next run
        iid, lines = fetch(iid)
        with run["lock"]:
            if lines is not None:
                run["fails"] = 0
            else:
                run["fails"] += 1
                if run["fails"] >= max_fails and not run["why"]:
                    run["why"] = "%d failed lookups in a row" % run["fails"]
        return iid, lines

    chunks = load_details(out)
    changed = set()

    def put(iid, lines):
        n, k = details_chunk(iid), str(int(iid))
        c = chunks.setdefault(n, {})
        if c.get(k) != lines:
            c[k] = lines
            changed.add(n)

    for iid, lines in tips.items():
        put(iid, lines)
    todo, missing = details_todo(items, chunks, latest, vols, vinfo, limit)
    done = 0
    if todo:
        with cf.ThreadPoolExecutor(max(1, threads)) as ex:
            for iid, lines in ex.map(lookup, todo):
                if lines is None:
                    continue
                put(iid, lines)
                done += 1
    write_details(out, chunks, changed)
    if run["why"] and missing - done > 0:
        print(f"item details: lookups stopped for this run ({run['why']}), the rest come next run")
    return len(tips), done, missing - done


# -- flip ideas from one snapshot ---------------------------------------------
def percentile(sorted_vals, p):
    """Linear interpolation between the closest ranks (the site's method)."""
    n = len(sorted_vals)
    if not n:
        return None
    if n == 1:
        return sorted_vals[0]
    x = p * (n - 1)
    lo = int(math.floor(x))
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (x - lo)


def measured_index(measured):
    """(sorted hour slots, running total of their seconds) for sold_per_day."""
    slots = sorted(measured)
    pre = [0]
    for t in slots:
        pre.append(pre[-1] + int(measured[t]))
    return slots, pre


def sales_window(h, measured, hour, index=None):
    """(units of this item or version seen selling, seconds of sales measured)
    over the measured hours of the last FLIP_SALES_DAYS days. Only hours after
    its first hourly point count: the comparison that made that point could
    not see its sales (new items, and versions just after the upgrade that
    started tracking them). None = not measured (less than FLIP_SALES_MIN_SECS)."""
    pts = h.get("h") or []
    if not pts:
        return None
    lo = max(int(pts[0][0]), hour - FLIP_SALES_DAYS * 86400)    # hours after lo count
    slots, pre = index or measured_index(measured)
    a, b = bisect.bisect_right(slots, lo), bisect.bisect_right(slots, hour)
    secs = pre[b] - pre[a]
    if secs < FLIP_SALES_MIN_SECS:
        return None
    sold = 0
    for p in reversed(pts):
        if p[0] <= lo:
            break
        if p[0] <= hour and p[0] in measured:
            sold += int(p[4] or 0)
    return sold, secs


def sold_per_day(h, measured, hour, index=None):
    """Units sold per day, for sizing flips: never scaled up from part of a
    day (one vanished auction in one measured hour is 1 a day, not 24).
    Under FLIP_SALES_FULL_SECS measured, it is simply what was seen selling.
    None = not measured (see sales_window)."""
    w = sales_window(h, measured, hour, index)
    if w is None:
        return None
    return w[0] * 86400.0 / max(w[1], FLIP_SALES_FULL_SECS)


def history_prices(h, hour):
    """(cap, usual) from the last 7 days of hourly market values: cap = the
    FLIP_HIST_PCT percentile, usual = the median. (None, None) with fewer than
    FLIP_HIST_MIN_POINTS points."""
    vals = sorted(int(p[2]) for p in (h.get("h") or []) if hour - 168 * 3600 < p[0] <= hour and p[2] > 0)
    if len(vals) < FLIP_HIST_MIN_POINTS:
        return None, None
    return int(percentile(vals, FLIP_HIST_PCT)), percentile(vals, 0.5)


def history_listed(h, hour):
    """The median units listed over the same hourly points history_prices
    reads (the last 7 days, the hours that had a market value), so a deal can
    tell when its usual price was mostly one listing (one seller's asking
    price). None with fewer than FLIP_HIST_MIN_POINTS points."""
    vals = sorted(int(p[3]) for p in (h.get("h") or []) if hour - 168 * 3600 < p[0] <= hour and p[2] > 0)
    if len(vals) < FLIP_HIST_MIN_POINTS:
        return None
    return percentile(vals, 0.5)


def after_cut(price):
    """What a sale at `price` pays after the auction house cut (whole copper, rounded down)."""
    return int(price) * (100 - FLIP_AH_CUT_PCT) // 100


def find_gap_flip(units, commodity=False, sold_day=None, hist_cap=None, usual=0, early=False):
    """The best "gap" flip in one item's (or version's) listings, or None.
    units: [(unit price, qty)] one per listing, sorted by price.
    Buy every listing up to price level k (all listings at the cheapest k
    prices), relist at R = the next listing's price - 1c, and with
    FLIP_HIST_MIN_POINTS+ hourly points never above hist_cap (the 7-day
    FLIP_HIST_PCT market). profit = units * R after the 5% cut - cost.
    The k with the best profit wins, among those with: ROI >= 15%, profit >=
    20s (commodities) or 50s (everything else), the dearest listing bought
    (buyMax) itself making 15% at R after the cut (so every listing you buy
    pays, even if the cheaper ones are gone when you get there), units bought
    <= sold per day (sold_day, see sold_per_day; when it is None, sales are
    unknown: <= 20% of the units listed, at least 1), and at least
    FLIP_MIN_LEFT listings left above.
    Risk flags: thin (few listings left above, or the listings above hold
    fewer than max(FLIP_COVER_MIN_UNITS, FLIP_COVER_MULT x the units bought)
    units), slow (sold per day below the units bought, sales unknown, or
    `early`: measured for less than a day), spike (R above FLIP_SPIKE_RATIO x
    usual; or, with no 7-day history (hist_cap None), R above FLIP_SPIKE_GAP x
    buyMax with fewer than FLIP_SPIKE_CONFIRM listings within FLIP_SPIKE_NEAR
    of the next one: a few high listings may be one player's guess).
    usual is only what the spike flag is judged against (add_flips passes the
    7-day median, else today's market value). The flip record's own "usual"
    field is not set here: add_flips gives every flip the 7-day median, or 0."""
    n = len(units)
    if n < 1 + FLIP_MIN_LEFT:
        return None
    listed = sum(q for _, q in units)
    if sold_day is None:
        limit = max(1, int(listed * FLIP_UNKNOWN_SHARE))
    else:
        limit = int(sold_day + 1e-9)
        if limit < 1:
            return None
    min_profit = FLIP_MIN_PROFIT_COMMODITY if commodity else FLIP_MIN_PROFIT_ITEM
    cum, acc = [], 0
    for _, q in units:
        acc += q
        cum.append(acc)
    best = None
    i, cost, bought = 0, 0.0, 0
    while i < n:
        price = units[i][0]
        j = i
        while j < n and units[j][0] == price:
            cost += units[j][0] * units[j][1]
            bought += units[j][1]
            j += 1
        if bought > limit:
            break
        left = n - j
        if left < FLIP_MIN_LEFT:
            break
        nxt = units[j][0]
        relist = int(math.ceil(nxt)) - 1
        if hist_cap:
            relist = min(relist, int(hist_cap))
        c = max(1, int(round(cost)))
        profit = bought * after_cut(relist) - c if relist > 0 else -c
        if (profit >= min_profit and profit * 1000 >= FLIP_MIN_ROI_PERMILLE * c
                and after_cut(relist) * 1000 >= price * (1000 + FLIP_MIN_ROI_PERMILLE)
                and (best is None or profit > best["profit"])):
            risk = []
            covered = listed - cum[j - 1] >= max(FLIP_COVER_MIN_UNITS, FLIP_COVER_MULT * bought)
            if left < FLIP_THIN_LEFT or not covered:
                risk.append("thin")
            if sold_day is None or early or sold_day < bought:
                risk.append("slow")
            spike = bool(usual) and relist > FLIP_SPIKE_RATIO * usual
            if not spike and not hist_cap and relist > FLIP_SPIKE_GAP * price:
                near, top = 0, nxt * FLIP_SPIKE_NEAR
                for k in range(j, min(n, j + FLIP_SPIKE_CONFIRM)):
                    if units[k][0] <= top:
                        near += 1
                spike = near < FLIP_SPIKE_CONFIRM
            if spike:
                risk.append("spike")
            best = {"kind": "gap", "buyMax": int(math.ceil(price)), "units": bought, "cost": c,
                    "relist": relist, "profit": profit, "roi": profit * 1000 // c, "risk": risk,
                    "next": int(math.ceil(nxt))}
        i = j
    return best


def find_deal_flip(units, usual, sold_day=None, hist_cap=None, early=False, listed_median=None):
    """A "deal" in one item's (or version's) listings, or None: the cheapest
    listings sit well under the usual price, so buy them and relist near that
    price. For greens, blues and epics (see add_flips in main: never a
    commodity, never an item whose listings are versions), and only where
    find_gap_flip found nothing, so the same listings never show twice.
    units: [(unit price, qty)] one per listing, sorted by price.
    usual: the series' own 7-day median market value (history_prices); no
    usual (fewer than FLIP_HIST_MIN_POINTS hours of history) = no deal.
    Buys the listings priced at most DEAL_MAX_SHARE x usual, cheapest first,
    at most max(1, whole sold per day) units: 1 when sales are unknown
    (sold_day None) or under 1 a day. A listing never splits, so a stack
    bigger than that is not bought.
    Relist R = min(int(usual), hist_cap (the 7-day FLIP_HIST_PCT market),
    the next listing above what is bought - 1c when there is one).
    profit = units * R after the 5% cut - cost. Needs profit >=
    DEAL_MIN_PROFIT, ROI >= 15% and the dearest listing bought itself making
    15% at R after the cut. Whenever buying all of them passes, all of them
    are bought. Only when that fails (mostly the dearest one: the next
    listing sits just above it, so R leaves it under 15%), the cheapest k of
    them that pass every check are bought instead, k fewer than all: the k
    with the most profit (the most listings on a tie), relisted just under
    the next listing, which is one of the cheap ones left. No deal when no k
    passes. This fallback is on purpose: without it a listing far under its
    usual price would get no buy at all, just because a second cheap one
    sits right under the next listing.
    Risk flags: thin (fewer than FLIP_MIN_LEFT listings left above), slow
    (sales unknown, `early`: measured for less than a day, or sold per day
    under the units bought), unsure (listed_median, the 7-day median units
    listed (history_listed), under DEAL_UNSURE_LISTED or unknown: the usual
    price may be one seller's asking price). add_flips adds "suffix" for
    versions. "usual" in the result is int(usual)."""
    if not usual or usual <= 0 or not units:
        return None
    share = int(round(DEAL_MAX_SHARE * 1000))      # whole permille: 0.7 * 41000 is 28699.999... in floats
    limit = 1 if sold_day is None else max(1, int(sold_day + 1e-9))
    n = len(units)
    m, got = 0, 0
    while m < n and units[m][0] * 1000 <= share * usual and got + units[m][1] <= limit:
        got += units[m][1]
        m += 1
    if not m:
        return None
    usual_c = int(usual)

    def judge(k):
        """Buy the cheapest k listings, or None when that is no deal."""
        bought = sum(q for _, q in units[:k])
        c = max(1, int(round(sum(p * q for p, q in units[:k]))))
        dearest = units[k - 1][0]
        nxt = units[k][0] if k < n else None
        relist = usual_c
        if hist_cap:
            relist = min(relist, int(hist_cap))
        if nxt is not None:
            relist = min(relist, int(math.ceil(nxt)) - 1)
        if relist <= 0:
            return None
        profit = bought * after_cut(relist) - c
        if (profit < DEAL_MIN_PROFIT or profit * 1000 < FLIP_MIN_ROI_PERMILLE * c
                or after_cut(relist) * 1000 < dearest * (1000 + FLIP_MIN_ROI_PERMILLE)):
            return None
        risk = []
        if n - k < FLIP_MIN_LEFT:
            risk.append("thin")
        if sold_day is None or early or sold_day < bought:
            risk.append("slow")
        if listed_median is None or listed_median < DEAL_UNSURE_LISTED:
            risk.append("unsure")
        return {"kind": "deal", "buyMax": int(math.ceil(dearest)), "units": bought, "cost": c,
                "relist": relist, "profit": profit, "roi": profit * 1000 // c, "risk": risk,
                "next": int(math.ceil(nxt)) if nxt is not None else 0, "usual": usual_c}

    best = judge(m)                    # all of them (within a day of sales), whenever that passes
    if best is None:                   # else the cheapest few that pass: the most profit, then the most listings
        for k in range(m - 1, 0, -1):
            f = judge(k)
            if f and (best is None or f["profit"] > best["profit"]):
                best = f
    return best


def find_vendor_flip(units, vendor):
    """Listings priced under what a vendor pays (units sorted by price): buy
    them and sell them to a vendor. No auction house cut, no relisting. Only
    listings that each make FLIP_VENDOR_MIN_MARGIN_PERMILLE at the vendor
    count (hundreds of leggings at 1c under the vendor price are no flip).
    None when there are none, or they make less than FLIP_VENDOR_MIN_PROFIT.
    add_flips gives the flip its "usual" field (the 7-day median, or 0)."""
    vendor = int(vendor or 0)
    if vendor <= 0:
        return None
    take = [(p, q) for p, q in units if p * (1000 + FLIP_VENDOR_MIN_MARGIN_PERMILLE) <= vendor * 1000]
    if not take:
        return None
    n = sum(q for _, q in take)
    c = max(1, int(round(sum(p * q for p, q in take))))
    profit = n * vendor - c
    if profit < FLIP_VENDOR_MIN_PROFIT:
        return None
    rest = units[len(take):]
    return {"kind": "vendor", "buyMax": int(math.ceil(take[-1][0])), "units": n, "cost": c, "relist": 0,
            "profit": profit, "roi": profit * 1000 // c, "risk": [],
            "next": int(math.ceil(rest[0][0])) if rest else 0}


def rank_flips(flips):
    """Best first: most profit, then best ROI, then the lower id."""
    return sorted(flips, key=lambda f: (-f["profit"], -f["roi"], f["id"], f["kind"]))


def pick_addon_flips(flips, has_line):
    """The flips the addon file carries, from ranked `flips` (best first),
    only those whose id has an item line (`has_line`: the addon lines, or any
    container of ids): the best FLIPS_ADDON_MAX, plus up to FLIPS_ADDON_DEALS
    deals and then up to FLIPS_ADDON_CHEAP flips costing at most
    FLIP_CHEAP_COST that are not in yet, so a character with little gold and
    gear hunters always have ideas. Kept in rank order, never twice."""
    cand = [f for f in flips if f["id"] in has_line]
    pick = set(range(min(len(cand), FLIPS_ADDON_MAX)))
    for wanted, room in ((lambda f: f["kind"] == "deal", FLIPS_ADDON_DEALS),
                         (lambda f: int(f["cost"]) <= FLIP_CHEAP_COST, FLIPS_ADDON_CHEAP)):
        for i, f in enumerate(cand):
            if room <= 0:
                break
            if i not in pick and wanted(f):
                pick.add(i)
                room -= 1
    return [f for i, f in enumerate(cand) if i in pick]


def flip_line(f):
    """One flip as the addon file carries it:
    id|kind|buyMax|units|cost|relist|profit|roi|risk|usual
    (usual: the 7-day median market value, 0 when not known; older addon
    copies read only the first 9 fields and skip kinds they do not know)."""
    return "|".join(str(x) for x in (int(f["id"]), f["kind"], int(f["buyMax"]), int(f["units"]), int(f["cost"]),
                                     int(f["relist"]), int(f["profit"]), int(f["roi"]), ",".join(f["risk"]),
                                     int(f.get("usual") or 0)))


# -- in-game addon data file (ForeverAH_Data.lua) -----------------------------
# One Lua string per item, fields split on "|":
#   name ("" = unknown) | cheapest now | market now | units listed now |
#   est. sold last 24h (measured hours only) | avg7 (mean daily market, last
#   7 days) | vendor sell | H | D | auctions now (separate listings)
#   [| P: idle versions only, see below]
# H (hourly, <= 168 points) and D (daily, <= 400 points) are each
#   "cheapest;market;sold", comma-separated tokens, right-aligned so the last
#   token is hourEnd (H) or dayEnd (D) and the k-th from the end is k steps
#   earlier. Price tokens: "~" = nothing listed, otherwise a base-36 delta from
#   the previous non-null value ("" = no change, leading "-" = down; the first
#   one is the absolute price). Sold tokens: base-36 count, "" = 0.
# A version (vid) with nothing listed or sold in the last ADDON_VERSION_HOURLY
#   hours gets ADDON_NO_HOURLY as H (two empty hours) and an 11th field, P: its
#   hourly prices packed (pack_runs). A version is mostly one listing that
#   sells and is gone for days, and a week of empty hours costs 5 bytes an
#   hour; packed, the whole week is about 60 bytes. Goblin Ledger 1.0.0 reads
#   only the first 10 fields (it sees two empty hours); newer copies read P,
#   so their prices, advice and charts are the same as with H written out.
# P: H with every run of 2 or more equal tokens of a sub-series written once
#   as "<token>*<count>" (count in base 36), when that is shorter ("~*4" is
#   "~,~,~,~", "*5" is five empty tokens). Expanding it gives H exactly.
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_B36_PAD = [a + b + c for a in _B36 for b in _B36 for c in _B36]   # 3 digits, zero padded
_B36_SHORT = [s.lstrip("0") or "0" for s in _B36_PAD]
_B36_BASE = 36 ** 3


def b36(n):
    """Non-negative int -> lowercase base 36. Table driven: this runs millions of times."""
    if n < _B36_BASE:
        return _B36_SHORT[n]
    parts = []
    while n >= _B36_BASE:
        n, r = divmod(n, _B36_BASE)
        parts.append(_B36_PAD[r])
    parts.append(_B36_SHORT[n])
    return "".join(reversed(parts))


def lua_str(s):
    """Python str -> Lua 5.1 double-quoted string literal (UTF-8 bytes kept as is)."""
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif o < 32 or o == 127:
            out.append("\\%03d" % o)
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _addon_series(points, end, step, count, bridge=0):
    """points: [[t, low, high, listed, sold], ...] sorted by t. Returns the
    'cheap;market;sold' string on the grid end - k*step (k < count), starting
    at the first grid point that has a price or a sale.
    A grid point with no snapshot at all (a skipped or late hourly run) is not
    the same as "nothing listed": a run of up to `bridge` such points between
    two snapshots repeats the earlier snapshot's prices with 0 sold. Longer
    gaps stay "~". A snapshot that saw nothing listed is always "~"."""
    start = end - (count - 1) * step
    slots = {}
    for p in reversed(points):
        t = p[0]
        if t < start:
            break
        if t <= end and (end - t) % step == 0:
            k = (t - start) // step
            if k not in slots:          # keep the newest if a time repeats
                slots[k] = p
    first = None
    for k in sorted(slots):
        p = slots[k]
        if p[1] > 0 or p[2] > 0 or p[4] > 0:
            first = k
            break
    if first is None:
        return "~;~;"                    # one empty point at the grid end
    if bridge > 0:
        last = None
        for k in range(first, count):
            if k in slots:
                if last is not None and 1 < k - last <= bridge + 1:
                    p = slots[last]
                    for g in range(last + 1, k):
                        slots[g] = [start + g * step, p[1], p[2], p[3], 0]
                last = k
    cheap, mkt, sold = [], [], []
    pc = pm = None
    for k in range(first, count):
        p = slots.get(k)
        if p is None:
            cheap.append("~")
            mkt.append("~")
            sold.append("")
            continue
        c, m, s = min(int(p[1]), ADDON_PRICE_CAP), min(int(p[2]), ADDON_PRICE_CAP), int(p[4])
        if c > 0:
            d = c - pc if pc is not None else c
            cheap.append("" if d == 0 else (b36(d) if d > 0 else "-" + b36(-d)))
            pc = c
        else:
            cheap.append("~")
        if m > 0:
            d = m - pm if pm is not None else m
            mkt.append("" if d == 0 else (b36(d) if d > 0 else "-" + b36(-d)))
            pm = m
        else:
            mkt.append("~")
        sold.append(b36(s) if s > 0 else "")
    return ",".join(cheap) + ";" + ",".join(mkt) + ";" + ",".join(sold)


def pack_runs(series):
    """A 'cheap;market;sold' series with each run of 2 or more equal tokens of a
    sub-series written once as "<token>*<count>" (count in base 36), where that
    is shorter. Core.lua (unpackRuns) expands it back to exactly `series`."""
    out = []
    for sub in series.split(";"):
        toks = sub.split(",") if sub != "" else []
        res, i = [], 0
        while i < len(toks):
            j = i + 1
            while j < len(toks) and toks[j] == toks[i]:
                j += 1
            run = j - i
            packed = toks[i] + "*" + b36(run)
            if run >= 2 and len(packed) < run * len(toks[i]) + run - 1:
                res.append(packed)
            else:
                res.extend(toks[i:j])
            i = j
        out.append(",".join(res))
    return ";".join(out)


def unpack_runs(packed):
    """pack_runs backwards (what Core.lua does; for tests and checks)."""
    out = []
    for sub in packed.split(";"):
        toks = []
        for tok in (sub.split(",") if sub != "" else []):
            if "*" in tok:
                t, n = tok.split("*", 1)
                toks.extend([t] * int(n, 36))
            else:
                toks.append(tok)
        out.append(",".join(toks))
    return ";".join(out)


def addon_item_line(iid, meta, h, lo, hi, listed, hour, days=ADDON_DAYS, auctions=0, measured=None,
                    sold24=None, hourly_idle=None):
    """One `[id] = "...",` line for ForeverAH_Data.items, or None when the item
    had no listing and no estimated sale in the last 7 days. `days` is how many
    daily points to keep (fewer only when the file would be too big).
    `auctions` is how many separate auctions are listed now. `measured` is the
    set of hours with a measured sold estimate (None = every hour); sold24
    only adds up those hours, unless `sold24` gives it (a version just after
    the upgrade that started tracking versions). `hourly_idle` (hours, for
    versions: ADDON_VERSION_HOURLY): with nothing listed or sold for that
    long, H is ADDON_NO_HOURLY and the hourly prices follow packed (P)."""
    hstart = hour - (ADDON_HOURS - 1) * 3600
    recent = False
    last_active = None
    given, sold24 = sold24, 0
    for p in reversed(h["h"]):
        if p[0] < hstart:
            break
        if p[0] > hour:
            continue
        if p[1] > 0 or p[3] > 0 or p[4] > 0:
            recent = True
            if last_active is None:
                last_active = p[0]
        if p[0] > hour - 86400 and (measured is None or p[0] in measured):
            sold24 += int(p[4])
    if not recent:
        return None
    if given is not None:
        sold24 = max(0, int(given))
    day_end = hour - hour % 86400
    week = [int(p[2]) for p in h["d"] if day_end - 6 * 86400 <= p[0] <= day_end and p[2] > 0]
    avg7 = int(sum(week) / len(week) + 0.5) if week else 0
    hs = _addon_series(h["h"], hour, 3600, ADDON_HOURS, ADDON_BRIDGE_H)
    packed = None
    if hourly_idle is not None and last_active <= hour - int(hourly_idle) * 3600:
        hs, packed = ADDON_NO_HOURLY, pack_runs(hs)
    ds = _addon_series(h["d"], day_end, 86400, max(1, min(int(days), ADDON_DAYS)), ADDON_BRIDGE_D)
    name = item_name(meta).replace("|", "/")
    lo, hi = min(int(lo), ADDON_PRICE_CAP), min(int(hi), ADDON_PRICE_CAP)
    fields = [name, str(lo), str(hi), str(int(listed)), str(sold24), str(avg7),
              str(int((meta or {}).get("sell") or 0)), hs, ds, str(max(0, int(auctions or 0)))]
    if packed is not None:
        fields.append(packed)
    body = "|".join(fields)
    return f"    [{int(iid)}] = {lua_str(body)},\n"


def _line_sold24(line):
    """sold24 field of an addon line (names never contain "|": it is replaced)."""
    try:
        return int(line.split("|")[4])
    except (IndexError, ValueError):
        return 0


def fit_addon_lines(lines, rebuild, limit=ADDON_SOFT_MAX, vids=(), extra=0):
    """Keeps the addon file under `limit` bytes. When it is too big, the
    quietest lines (fewest sold in 24h) get a shorter daily history until it
    fits: versions of items (`vids`) first, 90, 30, 7 and then 1 day, and only
    after that the items themselves, 90 then 30 days. No line is ever left
    out. rebuild(iid, days) returns the shorter line (or None to leave it).
    `extra` = bytes of the rest of the file. Changes `lines` in place.
    Returns (bytes, shortened)."""
    sizes = {k: len(v.encode("utf-8")) for k, v in lines.items()}
    total = sum(sizes.values()) + int(extra)
    shortened = 0
    if total <= limit:
        return total, shortened
    vids = set(vids)
    quiet_first = sorted(lines, key=lambda k: (_line_sold24(lines[k]), k))
    quiet_vids = [k for k in quiet_first if k in vids]
    quiet_items = [k for k in quiet_first if k not in vids]
    passes = [(quiet_vids, d) for d in (90, 30, 7, 1)] + [(quiet_items, d) for d in (90, 30)]
    for group, days in passes:
        for iid in group:
            if total <= limit:
                return total, shortened
            try:
                new = rebuild(iid, days)
            except Exception:
                new = None
            if not new:
                continue
            n = len(new.encode("utf-8"))
            if n < sizes[iid]:
                total += n - sizes[iid]
                sizes[iid] = n
                lines[iid] = new
                shortened += 1
    return total, shortened


def addon_tables(variants=None, flips=None):
    """The variants and flips tables of the addon file (before items, so
    nothing that scans for item lines after "items = {" ever sees them).
    variants: {item id: [(key, vid), ...]} -> [itemId] = "key=vid,key=vid"
    flips:    flip dicts, best first -> "id|kind|buyMax|units|cost|relist|profit|roi|risk|usual"
              (see flip_line; kind is gap, vendor or deal; main picks them with pick_addon_flips)
    None leaves a table out (older callers)."""
    parts = []
    if variants is not None:
        parts.append("  variants = {\n")
        for iid in sorted(variants):
            body = ",".join(f"{k}={int(v)}" for k, v in sorted(variants[iid], key=lambda kv: kv[1]))
            parts.append(f"    [{int(iid)}] = {lua_str(body)},\n")
        parts.append("  },\n")
    if flips is not None:
        parts.append("  flips = {\n")
        for f in flips:
            parts.append(f"    {lua_str(flip_line(f))},\n")
        parts.append("  },\n")
    return "".join(parts)


def write_addon_data(out, lines, updated, hour, house_name, fetched=None, sales_hours=0,
                     variants=None, flips=None):
    """lines: {item id or vid: line from addon_item_line}. Written atomically.
    updated = when Blizzard made the snapshot, fetched = when this ran,
    sales_hours = how many of the last 24 hours have a measured sold estimate,
    variants / flips: see addon_tables (left out when None)."""
    path = os.path.join(out, ADDON_FILE)
    tmp = path + ".tmp"
    head = (f"{ADDON_HEADER}\n"
            "ForeverAH_Data = {\n"
            "  v = 1,\n"
            f"  updated = {int(updated)},\n"
            f"  fetched = {int(updated if fetched is None else fetched)},\n"
            f"  salesHours = {max(0, min(24, int(sales_hours or 0)))},\n"
            f"  region = {lua_str(REGION)}, house = {HOUSE}, houseName = {lua_str(house_name)}, "
            f"ruleset = {lua_str(RULESET_NAMES.get(RULESET, f'Ruleset {RULESET}'))},\n"
            f"  hourEnd = {int(hour)},\n"
            f"  dayEnd = {int(hour - hour % 86400)},\n"
            + addon_tables(variants, flips) +
            "  items = {\n")
    with open(tmp, "w", encoding="utf-8", errors="replace", newline="\n") as fh:
        fh.write(head)
        fh.writelines(lines[k] for k in sorted(lines))
        fh.write("  },\n}\n")
    os.replace(tmp, path)
    return os.path.getsize(path)


# -- the run ------------------------------------------------------------------
def _state_time(state):
    """When Blizzard made the previous snapshot (older state files only have the header text)."""
    t = state.get("dataTime") or parse_http_date(state.get("modified"))
    return int(t) if t else (int(state["at"]) if state.get("at") else None)


def _state_hour(state):
    """The hourly slot the previous snapshot went into."""
    if state.get("hour"):
        return int(state["hour"])
    if state.get("at"):                  # older versions used the run time's hour
        return int(state["at"]) - int(state["at"]) % 3600
    return None


_HIST_V = re.compile(r'^\s*\{\s*"v"\s*:\s*"([0-9:]*)"')


def load_registry(state, hist_dir):
    """state.json "variants" ({str(item id): {key: index}}). When it is missing
    (state from an older version, or state.json was lost) it is rebuilt from
    the "v" key at the start of the versions' hist files, so indexes are never
    handed out twice. State from before versions existed simply starts empty."""
    reg = state.get("variants")
    if isinstance(reg, dict):
        out = {}
        for sid, keys in reg.items():
            try:
                out[str(int(sid))] = {str(k): int(v) for k, v in (keys or {}).items()}
            except (TypeError, ValueError, AttributeError):
                continue
        return out
    reg = {}
    if not os.path.isdir(hist_dir):
        return reg
    for f in os.listdir(hist_dir):
        if not f.endswith(".json"):
            continue
        try:
            vid = int(f[:-5])
        except ValueError:
            continue
        if vid < VID_MULT:
            continue
        try:
            with open(os.path.join(hist_dir, f), encoding="utf-8") as fh:
                m = _HIST_V.match(fh.read(200))
        except OSError:
            continue
        base, idx = divmod(vid, VID_MULT)
        if m and m.group(1) and 1 <= idx <= VARIANT_MAX:
            reg.setdefault(str(base), {})[m.group(1)] = idx
    return reg


def info_fields(m):
    """The item details every mapping.json and flips.json entry carries."""
    m = m or {}
    return {"quality": m.get("quality", "COMMON"), "cls": m.get("cls", ""), "sub": m.get("sub", ""),
            "clsName": m.get("cls", ""), "subName": m.get("sub", ""),
            "clsId": m.get("clsId"), "subId": m.get("subId"),
            "slot": m.get("slot", ""), "ilvl": int(m.get("ilvl") or 0), "reqlvl": int(m.get("lvl") or 0)}


def mapping_entry(eid, m, name, extra=None):
    m = m or {}
    e = {"id": int(eid), "name": name, "examine": m.get("sub") or m.get("cls") or "",
         "members": True, "value": m.get("sell", 0), "lowalch": m.get("sell", 0),
         "highalch": m.get("sell", 0), "limit": None, "icon": m.get("icon", ""),
         "lvl": m.get("lvl", 0)}
    e.update(info_fields(m))
    if extra:
        e.update(extra)
    return e


def write_flips(out, flips, data_time, meta_of, limit=FLIPS_SITE_MAX):
    """flips.json: {updated, flips: [{id, base, variant, name, quality, cls,
    sub, clsName, subName, clsId, subId, slot, reqlvl, ilvl, kind, buyMax,
    units, cost, relist, profit, roi, risk: [...], listed, auctions, market,
    sold24, next, usual}, ...]} best first. kind: gap, vendor or deal; usual:
    the item's (or version's) 7-day median market value (whole copper, 0
    when not known).
    meta_of(id) -> (meta, name)."""
    rows = []
    for f in flips[:limit]:
        m, name = meta_of(f["id"])
        r = {"id": f["id"], "base": f["base"], "variant": f["variant"], "name": name}
        r.update(info_fields(m))
        for k in ("kind", "buyMax", "units", "cost", "relist", "profit", "roi", "risk",
                  "listed", "auctions", "market", "sold24", "next"):
            r[k] = f[k]
        r["usual"] = int(f.get("usual") or 0)
        rows.append(r)
    save(os.path.join(out, FLIPS_FILE), {"updated": int(data_time), "flips": rows})
    return len(rows)


DATA_FORMAT = 3   # state.json layout; raise it when the layout changes
# 3: snap entries may carry a vid as a 5th entry; "variants" = the version indexes.
#    Format 1 and 2 state is read as it is (no vids yet) and written back as 3.


def main(out=None):
    global TOKEN
    if out is None:
        out = sys.argv[1] if len(sys.argv) > 1 else "wowdata"
    os.makedirs(out, exist_ok=True)
    TOKEN = get_token()
    now = int(time.time())
    state = load(os.path.join(out, "state.json"), {})
    # Two writers share this history (the GitHub job and the user's PC). If an
    # older copy of this script ever runs on data a newer one wrote, it stops
    # instead of mixing state formats.
    if int(state.get("format") or 1) > DATA_FORMAT:
        sys.exit("wow-data was written by a newer fetch_ah.py (state format %s); update this copy "
                 "(on the PC: run Setup-PC-Collector.bat)." % state.get("format"))

    # every house, so the IDs can be checked against what the game shows
    idx, _ = api(f"/data/wow/ruleset/{RULESET}/auctions/", DYN)
    houses = [{"id": h.get("id"), "name": h.get("name")} for h in (idx or {}).get("auctions", [])]

    data, hdr = api(f"/data/wow/ruleset/{RULESET}/auctions/{HOUSE}", DYN)
    if data is None:
        sys.exit(f"House {HOUSE} not found in {DYN}")
    modified = str(header(hdr, "Last-Modified") or "")
    if modified and modified == state.get("modified"):
        print("Blizzard has not refreshed this house since the last run:", modified)
        tell_workflow(False)
        return
    prev = state.get("snap") or {}
    prev_time = _state_time(state)
    data_time = parse_http_date(modified)
    if data_time is None:
        print(f"no usable Last-Modified time ({modified!r}), using the run time")
        data_time = now
    data_time = min(int(data_time), now)
    if prev and prev_time and data_time <= prev_time:
        print(f"Blizzard sent a snapshot that is not newer than the last one ({modified}), nothing to do")
        tell_workflow(False)
        return
    save(os.path.join(out, "houses.json"), {"region": REGION, "ruleset": RULESET,
                                            "using": HOUSE, "houses": houses})

    auctions = list(data.get("auctions") or [])
    endpoints = {"auctions": len(auctions), "commodities": 0}
    failed = []
    com_list = []
    try:
        com, _ = api(f"/data/wow/ruleset/{RULESET}/auctions/{HOUSE}/commodities", DYN)
        com_list = list((com or {}).get("auctions") or [])
        endpoints["commodities"] = len(com_list)
        auctions += com_list
    except Exception as e:
        print("commodities endpoint skipped:", e)
        failed.append("commodities")
    commodity_ids = {(a.get("item") or {}).get("id") for a in com_list}

    hist_dir = os.path.join(out, "hist")
    registry = load_registry(state, hist_dir)
    per, snap, vper = aggregate(auctions, registry)

    # A download that looks incomplete (a list failed, or far fewer auctions
    # than last time) is held back: nothing is published from it and the
    # previous snapshot stays the one the next run compares with. Only
    # state.json changes, so the next run (15 to 30 minutes later) knows it
    # downloads again. When that download looks the same, the drop is real
    # and it is used, but without a sales estimate for it or for the next
    # comparison (sales of anything missing from it would be invisible).
    short = incomplete_download(prev, snap, endpoints, state.get("endpoints"), failed)
    if short and not state.get("held"):
        state["held"] = {"modified": modified, "auctions": len(snap), "reason": short, "at": now}
        save(os.path.join(out, "state.json"), state)
        print(f"NOT published: {short}. The next run downloads it again, "
              "and uses it if that download looks the same.")
        tell_workflow(True)     # only state.json changed, so the next run remembers this
        return
    state.pop("held", None)

    # the hourly slot: Blizzard's data hour, never before the previous slot
    hour = data_time - data_time % 3600
    last_hour = _state_hour(state)
    if prev and last_hour and hour < last_hour:
        hour = last_hour

    # estimated sales since the previous snapshot, only when that comparison means something
    skipped, gap_min = sold_check(prev, snap, prev_time, data_time, endpoints, state.get("endpoints"), failed)
    if short:
        skipped = f"{short}; the download after it looked the same, so it is used, but its sales are not counted"
    elif not skipped and state.get("incompleteBase"):
        skipped = "the previous snapshot looked incomplete, so sales are counted again from the next snapshot"
    sold = {} if skipped else estimate_sold(prev, snap)
    vsold = {} if skipped else estimate_sold(prev, snap, by_variant=True)
    if skipped:
        print("sold estimate skipped:", skipped)
    measured = measured_secs(state)
    if not skipped:
        measured[hour] = measured.get(hour, 0) + max(0, int(data_time - prev_time))
    # the last HOURLY_KEEP hours, none from before the sold floor (see SOLD_FLOOR_FROM)
    measured = {t: s for t, s in measured.items()
                if hour - HOURLY_KEEP * 3600 < t <= hour and t >= SOLD_FLOOR_FROM}
    sales_h = sales_hours(measured, hour)
    sales_min = int(sales_seconds(measured, hour) / 60 + 0.5)
    # Versions: the run that upgrades older state compares with a snapshot that has no
    # versions in it, so that comparison (and every hour before it) measured no sales of
    # versions. Their sold24 counts only the hours after it, scaled to the measured hours
    # everything else counts (sales_hours), for the day after the upgrade.
    vfrom = state.get("versionsFrom") if isinstance(state.get("versionsFrom"), dict) else None
    if prev and int(state.get("format") or 1) < 3:
        vfrom = {"hour": hour, "secs": 0 if skipped else max(0, int(data_time - prev_time))}
    if vfrom and int(vfrom.get("hour") or 0) <= hour - 86400:
        vfrom = None                     # a full day of version sales is measured now

    # item metadata (cached; new ids and a few unnamed ones cost requests, and
    # up to META_BACKFILL_MAX older entries get their slot and item level)
    items = load(os.path.join(out, "items.json"), {})
    n_new, n_retry, n_found = refresh_items(items, per.keys(), now)
    if n_new or n_retry:
        print(f"looked up {n_new} new items, retried {n_retry} unnamed items ({n_found} names found)")
    n_fill, n_fill_left = backfill_meta(items, {iid: len(u) for iid, u in per.items()})
    if n_fill:
        print(f"added slot and item level to {n_fill} older items ({n_fill_left} still to do)")
    new_tips = take_tips(items)     # the tooltip lines those lookups brought: details/, not items.json
    save(os.path.join(out, "items.json"), items)
    suffixes = load_suffix_names(out)

    # versions: a vid that happens to be a real item id is never published
    vinfo = registry_vids(registry)
    clash = sorted(v for v in vinfo if str(v) in items or v in per)
    for v in clash:
        vinfo.pop(v, None)
        vper.pop(v, None)
        vsold.pop(v, None)
    if clash:
        print(f"{len(clash)} version ids are also real item ids and are left out, first: {clash[0]}")
    vmeta_cache = {}

    def vmeta(vid):
        if vid not in vmeta_cache:
            iid, key, vidx = vinfo[vid]
            base = items.get(str(iid)) or {}
            vmeta_cache[vid] = dict(base, name=variant_name(base, key, vidx, suffixes))
        return vmeta_cache[vid]

    # history
    os.makedirs(hist_dir, exist_ok=True)
    latest, day, vols = {}, {}, {}
    addon, addon_now, addon_meta, addon_errors = {}, {}, {}, []
    flips = []
    midx = measured_index(measured)
    with_versions = {vinfo[v][0] for v in vper}      # items whose listings right now are versions
    seen_items = set(per) | set(sold)
    seen_vids = set(vper) | set(vsold)
    # items that vanished entirely still get a point so the chart shows the gap close
    for f in os.listdir(hist_dir):
        if f.endswith(".json"):
            try:
                n = int(f[:-5])
            except ValueError:
                continue
            (seen_vids if n in vinfo else seen_items).add(n)

    def version_sold24(h, key):
        """sold24 of a version while the upgrade is under a day old (None: count it as usual)."""
        if key is None or not vfrom:
            return None
        u, skip = int(vfrom.get("hour") or 0), int(vfrom.get("secs") or 0)

        def secs(t):
            v = measured.get(t, 0)
            return 0 if t < u else (max(0, v - skip) if t == u else v)
        raw = sum(int(p[4] or 0) for p in h["h"] if hour - 86400 < p[0] <= hour and p[0] in measured and secs(p[0]) > 0)
        glob = sales_seconds(measured, hour)
        own = sales_seconds({t: secs(t) for t in measured}, hour)
        if own < FLIP_SALES_MIN_SECS or own >= glob:
            return raw
        return int(raw * glob / own + 0.5)

    vsold24 = {}     # vid -> its sold24 when version_sold24 changed it (for the addon file's rebuilds)

    def series(sid, units, s, meta, key=None):
        """Adds this snapshot to one item's (key None) or version's hist file,
        its addon line and its latest / 24h / volumes entries. Returns
        (hist, now) or None. now = (cheapest, market, listed, auctions, sold24)."""
        path = os.path.join(hist_dir, f"{sid}.json")
        h = load(path, None)
        if not isinstance(h, dict) or not isinstance(h.get("h"), list) or not isinstance(h.get("d"), list):
            h = None
        if key is None:
            if h is not None and h.get("v"):
                return None              # a version's file whose index is not known (any more)
            h = h or {"h": [], "d": []}
        elif h is None or h.get("v") != key:
            if h is not None and (h["h"] or h["d"]):
                print(f"hist/{sid}.json belonged to another version ({h.get('v')!r}), started again")
            h = {"v": key, "h": [], "d": []}
        listed = sum(q for _, q in units)
        nauc = len(units)
        lo = copper(units[0][0]) if units else 0
        hi = market_value(units) if units else 0
        if not units and not s and not h["h"]:
            return None
        if key is not None and not units and not s:
            # a version nobody listed or bought for VARIANT_IDLE_HOURS: its file is left as it is
            # (most versions are one listing that sells and is gone for weeks)
            cut = hour - VARIANT_IDLE_HOURS * 3600
            active = False
            for p in reversed(h["h"]):
                if p[0] <= cut:
                    break
                if p[1] > 0 or p[3] > 0 or p[4] > 0:
                    active = True
                    break
            if not active:
                try:
                    vs = version_sold24(h, key)
                    if vs is not None:
                        vsold24[sid] = vs
                    line = addon_item_line(sid, meta, h, 0, 0, 0, hour, auctions=0, measured=measured,
                                           sold24=vs, hourly_idle=ADDON_VERSION_HOURLY)
                    if line:
                        addon[sid], addon_now[sid], addon_meta[sid] = line, (0, 0, 0, 0), meta
                except Exception as e:
                    addon_errors.append(f"{sid}: {e!r}")
                return None
        pt = [hour, lo, hi, listed, s]
        if h["h"] and h["h"][-1][0] == hour:
            # a second snapshot in the same hour: newest prices, sales add up
            pt[4] += int(h["h"][-1][4] or 0)
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
        try:   # the addon file must never stop the site's own files being written
            vs = version_sold24(h, key)
            if vs is not None:
                vsold24[sid] = vs
            line = addon_item_line(sid, meta, h, lo, hi, listed, hour, auctions=nauc, measured=measured,
                                   sold24=vs, hourly_idle=None if key is None else ADDON_VERSION_HOURLY)
            if line:
                addon[sid], addon_now[sid], addon_meta[sid] = line, (lo, hi, listed, nauc), meta
        except Exception as e:
            addon_errors.append(f"{sid}: {e!r}")
        save(path, h)
        win = [p for p in h["h"] if p[0] > hour - 86400]
        sold24 = vsold24[sid] if sid in vsold24 else sum(p[4] for p in win if p[0] in measured)
        if not units and all(p[3] == 0 for p in h["h"][-72:]) and not any(p[4] for p in h["h"][-72:]):
            # nothing listed or sold for 3 days: keep the file, stop updating latest
            return h, (lo, hi, listed, nauc, sold24)
        k = str(sid)
        if units:
            latest[k] = {"high": hi, "highTime": data_time, "low": lo, "lowTime": data_time, "n": nauc}
        else:
            # nothing listed right now: show the last seen prices, with their real age
            last = next((p for p in reversed(h["h"]) if p[1] > 0), None)
            if last:
                latest[k] = {"high": last[2], "highTime": last[0], "low": last[1], "lowTime": last[0], "n": 0}
        wp = [p for p in win if p[1] > 0]
        if wp:
            day[k] = {"avgHighPrice": int(sum(p[2] for p in wp) / len(wp)),
                      "highPriceVolume": sold24 - sold24 // 2,
                      "avgLowPrice": int(sum(p[1] for p in wp) / len(wp)),
                      "lowPriceVolume": sold24 // 2,
                      "listed": listed}
        vols[k] = sold24
        return h, (lo, hi, listed, nauc, sold24)

    def add_flips(fid, base, key, units, h, now_, vendor=0, gap=True, commodity=False, quality=""):
        """Flips of one series. gap: look for a gap flip, and a deal when there is none (False for
        an item whose listings right now are versions: its prices mix them, its versions are
        checked on their own). quality: the item's (a version's: its item's) quality.
        Every flip found, whatever its kind, gets "usual": this series' own 7-day median market
        value in whole copper, 0 with fewer than FLIP_HIST_MIN_POINTS hours of it (never today's
        market value, never another series' price), so the field means one thing everywhere."""
        lo, hi, listed, nauc, sold24 = now_
        cap, usual = history_prices(h, hour)
        found = []
        if vendor:
            found.append(find_vendor_flip(units, vendor))
        if gap:
            # a gap flip's spike flag is judged against this series' own 7-day median, else today's
            # market value. Without 7 days of history find_gap_flip also checks the relist against
            # what is bought (in a thin market today's market value is the next listing itself, so
            # it never looks far above). A version never borrows the whole item's price: another
            # version sets that.
            w = sales_window(h, measured, hour, midx)
            spd = None if w is None else w[0] * 86400.0 / max(w[1], FLIP_SALES_FULL_SECS)
            early = w is not None and w[1] < FLIP_SALES_FULL_SECS
            g = find_gap_flip(units, commodity, spd, cap, usual or hi, early=early)
            if g is None and usual and not commodity and quality in DEAL_QUALITIES:
                # no gap flip: a green, blue or epic whose cheapest listings sit well under its own
                # 7-day median is still a buy (deals need that history: no borrowed prices)
                g = find_deal_flip(units, usual, spd, cap, early=early, listed_median=history_listed(h, hour))
            if g and key:
                g["risk"].append("suffix")
            found.append(g)
        for f in found:
            if f:
                f["usual"] = int(usual) if usual else 0
                f.update({"id": int(fid), "base": int(base), "variant": key or "", "listed": listed,
                          "auctions": nauc, "market": hi, "sold24": sold24})
                flips.append(f)

    for iid in seen_items:
        units = sorted(per.get(iid, []))
        meta = items.get(str(iid))
        r = series(iid, units, int(sold.get(iid, 0)), meta)
        if r and units:
            add_flips(iid, iid, None, units, r[0], r[1], vendor=int((meta or {}).get("sell") or 0),
                      gap=iid not in with_versions, commodity=iid in commodity_ids,
                      quality=(meta or {}).get("quality") or "")
    for vid in seen_vids:
        iid, key, _ = vinfo[vid]
        units = sorted(vper.get(vid, []))
        r = series(vid, units, int(vsold.get(vid, 0)), vmeta(vid), key)
        if r and units:
            add_flips(vid, iid, key, units, r[0], r[1], gap=True, commodity=iid in commodity_ids,
                      quality=vmeta(vid).get("quality") or "")
    flips = rank_flips(flips)

    def meta_of(eid):
        if eid in vinfo:
            m = vmeta(eid)
            return m, item_name(m)
        m = items.get(str(eid))
        return m, item_name(m)

    mapping = []
    for sid, m in items.items():
        if sid in latest:
            mapping.append(mapping_entry(sid, m, item_name(m)))
    for vid, (iid, key, vidx) in vinfo.items():
        if str(vid) in latest:
            m = vmeta(vid)
            mapping.append(mapping_entry(vid, m, item_name(m), {"base": iid, "variant": key, "vindex": vidx}))
    mapping.sort(key=lambda x: (x["name"] == "", x["name"], x["id"]))   # unnamed items last

    save(os.path.join(out, "mapping.json"), mapping)
    save(os.path.join(out, "latest.json"), {"data": latest})
    save(os.path.join(out, "24h.json"), {"data": day, "timestamp": hour})
    save(os.path.join(out, "volumes.json"), {"timestamp": data_time * 1000, "data": vols})
    n_flips = write_flips(out, flips, data_time, meta_of)
    n_vlatest = sum(1 for v in vinfo if str(v) in latest)
    save(os.path.join(out, "status.json"), {
        "updated": data_time, "fetched": now, "blizzardModified": modified, "region": REGION, "house": HOUSE,
        "auctions": len(auctions), "auctionsPerEndpoint": endpoints, "items": len(latest) - n_vlatest,
        "versions": n_vlatest, "itemsWithVersions": len({vinfo[v][0] for v in vinfo if str(v) in latest}),
        "flips": n_flips, "suffixNames": len(suffixes), "detailsMissing": n_fill_left,
        "soldUnitsThisHour": sum(sold.values()), "firstSnapshot": not prev,
        "salesHours": sales_h, "salesMinutes": sales_min, "gapMinutes": gap_min, "soldSkipped": skipped,
        "unnamedItems": sum(1 for sid in latest if int(sid) not in vinfo and is_unknown(items.get(sid)))})
    state.update({"format": DATA_FORMAT, "modified": modified, "dataTime": data_time, "hour": hour, "snap": snap,
                  "at": now, "measured": {str(t): s for t, s in sorted(measured.items())}, "endpoints": endpoints,
                  "variants": registry})
    if vfrom:
        state["versionsFrom"] = vfrom
    else:
        state.pop("versionsFrom", None)
    if short:
        state["incompleteBase"] = True      # the next comparison is not measured
    else:
        state.pop("incompleteBase", None)
    save(os.path.join(out, "state.json"), state)
    print(f"house {HOUSE}: {len(auctions)} auctions, {len(latest) - n_vlatest} items and {n_vlatest} versions, "
          f"~{sum(sold.values())} units sold since last snapshot, {sales_h} of the last 24 hours measured, "
          f"{len(flips)} flip ideas")

    # in-game addon file
    if addon_errors:
        print(f"addon file: skipped {len(addon_errors)} items, first: {addon_errors[0]}")
    try:
        hname = next((h.get("name") for h in houses if h.get("id") == HOUSE and h.get("name")), None)
        hname = hname or ("Alliance Auction House" if HOUSE == 110 else f"Auction House {HOUSE}")
        vid_lines = {v for v in addon if v in vinfo}
        vtable = {}
        for vid in vid_lines:
            iid, key, _ = vinfo[vid]
            if iid in addon:
                vtable.setdefault(iid, []).append((key, vid))
        addon_flips = pick_addon_flips(flips, addon)

        def rebuild(eid, days):
            hh = load(os.path.join(hist_dir, f"{eid}.json"), None)
            if not hh:
                return None
            lo_, hi_, listed_, nauc_ = addon_now[eid]
            return addon_item_line(eid, addon_meta.get(eid), hh, lo_, hi_, listed_, hour, days,
                                   sold24=vsold24.get(eid), auctions=nauc_, measured=measured,
                                   hourly_idle=ADDON_VERSION_HOURLY if eid in vid_lines else None)
        extra = len(addon_tables(vtable, addon_flips).encode("utf-8")) + 1024
        est, shortened = fit_addon_lines(addon, rebuild, vids=vid_lines, extra=extra)
        if shortened:
            print(f"{ADDON_FILE}: shortened the daily history of {shortened} quiet lines to keep it small")
        if est > ADDON_HARD_MAX:
            # a file this big would make the push fail and stop the site updating too
            print(f"{ADDON_FILE} NOT written: {est / 1048576:.0f} MB is too big, the previous file is kept")
        else:
            size = write_addon_data(out, addon, data_time, hour, str(hname), fetched=now, sales_hours=sales_h,
                                    variants=vtable, flips=addon_flips)
            print(f"{ADDON_FILE}: {len(addon) - len(vid_lines)} items, {len(vid_lines)} versions, "
                  f"{len(addon_flips)} flips, {size / 1024:.0f} KB ({size} bytes)")
    except Exception as e:
        print(f"{ADDON_FILE} NOT written: {e!r}")

    # the site's item details (tooltip lines, details/): never stops anything else being published
    try:
        n_tip, n_tip_fill, n_tip_left = update_details(out, items, new_tips, latest, vols, vinfo)
        if n_tip or n_tip_fill or n_tip_left:
            print(f"item details: {n_tip} from this run's lookups, {n_tip_fill} older items filled in "
                  f"({max(0, n_tip_left)} still to do)")
    except Exception as e:
        print(f"item details NOT written: {e!r}")
    tell_workflow(True)


if __name__ == "__main__":
    main()

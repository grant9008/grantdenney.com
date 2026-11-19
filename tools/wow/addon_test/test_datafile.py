"""Checks ForeverAH_Data.lua (the in-game addon's price file) against the
history files it was built from, and the collector's own rules.

  python tools/wow/addon_test/test_datafile.py [data dir]

data dir defaults to wow/_localdata (simulated test data from sim_fetch.py).
It must hold ForeverAH_Data.lua, hist/<id>.json, items.json and status.json.

What it does:
  1. loads the file in real Lua 5.1 (lupa) to prove it parses,
  2. decodes EVERY item twice, once with a Lua decoder written the way the
     addon must do it (tonumber(s, 36), "-" stripped by hand, "" = 0) and once
     in Python, and requires both to agree,
  3. compares every decoded point (cheapest, market, sold) with the hist JSON
     value for the same timestamp, plus the header fields (updated, fetched,
     salesHours), the "now" numbers, sold24 (measured hours only), avg7,
     vendor price, the auctions count, the name ("" when unknown) and which
     items are included; versions of items (vids): their names (suffix or
     "(version N)"), the variants table, that the item covers its versions,
     the flips table (numbers, flags, order, the 10th "usual" field, which
     flips are picked: the best 60 plus extra deals and cheap flips) against
     flips.json, every flip's usual price (any kind) against its own 7-day
     history (the median, 0 with under 4 hours of it), and the new
     mapping.json fields; simulated data (sim_fetch.py) also has
     sim_sales.json, the true sales of a market with a seller who pulls and
     reposts: every hour's sold must be what the buyers really bought (and a
     snapshot can show), never a pulled stack,
  4. builds a second, hand-made data set with awkward cases (quotes,
     backslashes, "|" and newlines in names, gaps, sold-only hours, prices at
     the gold cap, items just inside and just outside the 7-day window) and
     runs the same checks on it,
  5. unit tests for the collector: market value (deep and thin markets,
     fractions of a copper), the sold estimate safety rules, the sold floor
     (a listing gone while a cheaper one of the same item, or version, from
     the last snapshot stayed up was pulled, not bought; the floor is the
     cheapest one still up, with no slack: 1c over it is not a sale; and
     random books against the rule written out on its own, model_sold),
     salesHours, unknown item names and their retries, Blizzard's data
     time, the changed=true/false signal for the workflow, the size guard
     (versions first), snapshot flips (a 1c troll, a silly high listing, a
     deep cheap wall, 1-unit gear, the history cap, thresholds, vendor
     flips), deals (greens, blues and epics well under their usual price: the 70% line,
     profit, ROI, units sized by sales, the relist caps, each risk flag, all
     of them or else the cheapest few that pay, against the rule written out
     on its own (model_deal) on random books),
     which flips the addon file gets, variant keys, indexes and names, the
     suffix table and the item details backfill,
  6. runs the real collector (fetch_ah.main) on hand-made snapshots through a
     fake Blizzard API (no network): first snapshot, no change, an older
     snapshot, a normal hour (with a listing pulled and listed again
     cheaper: not a sale), a long gap, a collapsed auction count (held
     back, then downloaded again), a commodities download that fails twice
     (held, then used without sales), three snapshots in one hour (only the
     measured minutes count), unknown names that get a name later, and the
     upgrade from the first real snapshot's files,
  7. runs it again with suffix gear: versions vs the item lumped together,
     sold per version (other versions never set a version's sold floor),
     idle versions, the registry rebuilt from hist files,
     the upgrade from format 2 state, and an older copy refusing format 3,
  8. runs it over six hours of a steady market and then cheap listings:
     deals for a blue, an epic and a green version (with their flags), and
     for a green whose two cheap listings do not both pay (the cheaper one
     alone), none for a common item, a commodity, an item whose listings are
     versions, an item with no history yet, or one that has a gap flip; and a
     vendor flip's usual price hour by hour (0, then the 7-day median),
  9. runs it across the sold floor's first hour (SOLD_FLOOR_FROM) on state
     an older copy measured (it counted pulled stacks as sales): hours from
     before it no longer count (sold24, salesHours, the site's volumes, how
     many units a flip buys), the hist files keep their sold and state.json
     keeps its layout.
The generated data (step 1) may come from an older collector (9-field flip
lines, no "usual", no deals): that is checked by the rules of its own format.
Exit code 0 = everything passed.
"""
import email.utils
import json
import math
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter

from lupa import lua51

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(TOOLS))
sys.path.insert(0, TOOLS)
import fetch_ah  # noqa: E402

HEADER = "-- ForeverAH price data. Generated by grantdenney.com/wow. Do not edit."
H_MAX, D_MAX = 168, 400
TOKEN_MAX = 2 ** 32 - 1   # Lua 5.1 tonumber(s, 36) uses a 32-bit strtoul on Windows
FIELDS = 10               # fields in a line written now (old files have 9: no auctions count)

LUA_DECODER = r"""
local function split(s, sep)
  local t, n, i = {}, 0, 1
  while true do
    local j = string.find(s, sep, i, true)
    n = n + 1
    if not j then t[n] = string.sub(s, i); return t, n end
    t[n] = string.sub(s, i, j - 1)
    i = j + 1
  end
end

local function num36(tok)
  if tok == "" then return 0 end
  local neg = false
  if string.sub(tok, 1, 1) == "-" then neg = true; tok = string.sub(tok, 2) end
  assert(not string.find(tok, "[^0-9a-z]"), "bad token " .. tok)
  local v = tonumber(tok, 36)
  assert(v, "bad token " .. tok)
  if neg then return -v end
  return v
end

local function prices(str)
  local toks, n = split(str, ",")
  local out, prev = {}, nil
  for i = 1, n do
    local tok = toks[i]
    if tok == "~" then
      out[i] = false
    else
      local d = num36(tok)
      if prev then prev = prev + d else prev = d end
      out[i] = prev
    end
  end
  return out, n
end

local function solds(str)
  local toks, n = split(str, ",")
  local out = {}
  for i = 1, n do
    local tok = toks[i]
    assert(string.sub(tok, 1, 1) ~= "-", "negative sold")
    out[i] = num36(tok)
  end
  return out, n
end

local function series(str)
  local parts, np = split(str, ";")
  assert(np == 3, "series needs 3 parts, got " .. np)
  local c, nc = prices(parts[1])
  local m, nm = prices(parts[2])
  local s, ns = solds(parts[3])
  assert(nc == nm and nm == ns, "sub-series lengths differ")
  return { n = nc, cheap = c, market = m, sold = s }
end

function FAH_Decode(str)
  local f, nf = split(str, "|")
  assert(nf == 9 or nf == 10, "expected 9 or 10 fields, got " .. nf)
  local it = { name = f[1], nfields = nf }
  local keys = { "cheapest", "market", "listed", "sold24", "avg7", "vendor" }
  for i, k in ipairs(keys) do
    assert(string.find(f[i + 1], "^%d+$"), k .. " is not a base-10 number")
    it[k] = tonumber(f[i + 1])
  end
  it.H = series(f[8])
  it.D = series(f[9])
  if nf >= 10 then
    assert(string.find(f[10], "^%d+$"), "auctions is not a base-10 number")
    it.auctions = tonumber(f[10])
  end
  return it
end
"""


# -- Python reference decoder -------------------------------------------------
def py_num36(tok):
    if tok == "":
        return 0
    neg = tok.startswith("-")
    body = tok[1:] if neg else tok
    assert body and all(c in "0123456789abcdefghijklmnopqrstuvwxyz" for c in body), f"bad token {tok!r}"
    v = int(body, 36)
    assert v <= TOKEN_MAX, f"token {tok!r} is above what Lua 5.1 tonumber(s, 36) can read on Windows"
    return -v if neg else v


def py_series(s):
    parts = s.split(";")
    assert len(parts) == 3, f"series needs 3 parts: {s[:60]!r}"
    out = []
    for idx, part in enumerate(parts):
        toks = part.split(",")
        vals, prev = [], None
        for tok in toks:
            if idx < 2:
                if tok == "~":
                    vals.append(None)
                else:
                    d = py_num36(tok)
                    prev = d if prev is None else prev + d
                    vals.append(prev)
            else:
                assert not tok.startswith("-") and tok != "~", f"bad sold token {tok!r}"
                vals.append(py_num36(tok))
        out.append(vals)
    assert len(out[0]) == len(out[1]) == len(out[2]), "sub-series lengths differ"
    return out


def py_decode(s):
    f = s.split("|")
    assert len(f) in (9, 10), f"expected 9 or 10 fields, got {len(f)}"
    nums = [int(x) for x in f[1:7]]
    out = {"name": f[0], "cheapest": nums[0], "market": nums[1], "listed": nums[2],
           "sold24": nums[3], "avg7": nums[4], "vendor": nums[5],
           "H": py_series(f[7]), "D": py_series(f[8]), "nfields": len(f), "auctions": None}
    if len(f) == 10:
        assert re.match(r"^\d+$", f[9]), f"auctions {f[9]!r} is not a base-10 number"
        out["auctions"] = int(f[9])
    return out


def expected_name(meta):
    """Contract: "" when there is no usable name (missing, flagged unknown, or an old 'Item 123')."""
    if not meta or meta.get("unknown"):
        return ""
    name = str(meta.get("name") or "")
    if not name or re.match(r"^Item \d+$", name):
        return ""
    return name.replace("|", "/")


def load_suffixes(data_dir=None):
    """bonus id (str) -> suffix name, read independently of the collector."""
    for path in ([os.path.join(data_dir, "suffix_names.json")] if data_dir else []) + \
            [os.path.join(TOOLS, "suffix_names.json")]:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return {str(k): v for k, v in json.load(fh).items()}
    return {}


def expected_vid_name(base_meta, key, idx, suffixes):
    """Contract C2a: the item's name plus the suffix when known, else plus " (version N)"; "" when unnamed."""
    base = expected_name(base_meta)
    if not base:
        return ""
    names = [suffixes[b] for b in key.split(":") if b in suffixes]
    return base + " " + " ".join(names) if names else f"{base} (version {idx})"


def registry_of(state):
    """state.json "variants" -> {vid: (item id, key, index)}"""
    out = {}
    for sid, keys in ((state or {}).get("variants") or {}).items():
        for key, idx in keys.items():
            out[int(sid) * 1000 + int(idx)] = (int(sid), key, int(idx))
    return out


RISKS = {"thin", "slow", "spike", "suffix", "unsure"}
KINDS = ("gap", "vendor", "deal")
FLIP_FIELDS = ("id", "base", "variant", "name", "quality", "cls", "sub", "clsName", "subName", "slot", "reqlvl",
               "ilvl", "kind", "buyMax", "units", "cost", "relist", "profit", "roi", "risk", "listed", "auctions",
               "market", "sold24", "next")
INFO_FIELDS = ("quality", "cls", "sub", "lvl", "slot", "ilvl", "reqlvl", "clsName", "subName")
# the shared contract for deals and for which flips the addon file carries
DEAL_QUALITIES = {"UNCOMMON", "RARE", "EPIC", "LEGENDARY"}
DEAL_MIN_PROFIT, DEAL_SHARE_PERMILLE = 2500, 700
ADDON_TOP, ADDON_DEALS, ADDON_CHEAP, CHEAP_COST = 60, 40, 40, 50000
SITE_MAX = 400


def check_flip_numbers(tag, f, vendor):
    """C2c arithmetic: profit after the 5% cut (gap, deal) or against the vendor (vendor), roi per
    mille, and the flags each kind may carry. "usual" is checked when the flip has it."""
    check(f["kind"] in KINDS, f"{tag}: kind {f['kind']!r}")
    check(all(isinstance(f[k], int) and f[k] >= 0 for k in ("buyMax", "units", "cost", "relist", "roi"))
          and f["units"] >= 1 and f["cost"] >= 1, f"{tag}: numbers {f!r}")
    if f["kind"] in ("gap", "deal"):
        check(f["profit"] == f["units"] * (f["relist"] * 95 // 100) - f["cost"], f"{tag}: {f['kind']} profit {f!r}")
        check(f["relist"] >= 1 and f["roi"] >= 150, f"{tag}: relist price or roi under 15% {f!r}")
        # buyMax is the dearest listing's price rounded up: that listing itself makes 15% at the relist
        check((f["relist"] * 95 // 100) * 1000 >= (f["buyMax"] - 1) * 1150, f"{tag}: dearest listing under 15% {f!r}")
    else:
        check(f["relist"] == 0 and f["profit"] == f["units"] * vendor - f["cost"], f"{tag}: vendor profit {f!r}")
        check("suffix" not in f["risk"], f"{tag}: vendor flips are for the whole item")
    if f["kind"] == "deal":
        check(f["profit"] >= DEAL_MIN_PROFIT and "spike" not in f["risk"], f"{tag}: deal under 25s or spiked {f!r}")
    else:
        check("unsure" not in f["risk"], f"{tag}: only deals are unsure {f!r}")
    if "usual" in f:
        # every kind: the 7-day median or 0 (checked against the history in run_checks)
        check(isinstance(f["usual"], int) and f["usual"] >= 0, f"{tag}: usual {f!r}")
        if f["kind"] == "deal":
            check(f["usual"] >= 1 and f["relist"] <= f["usual"]
                  and (f["buyMax"] - 1) * 1000 < DEAL_SHARE_PERMILLE * (f["usual"] + 1),
                  f"{tag}: a deal buys under 70% of its usual price and relists at most at it {f!r}")
    check(f["profit"] > 0 and f["roi"] == f["profit"] * 1000 // f["cost"], f"{tag}: roi {f!r}")
    check(set(f["risk"]) <= RISKS and len(set(f["risk"])) == len(f["risk"]), f"{tag}: risk flags {f['risk']!r}")


def parse_flip(s):
    """An addon flip line: 10 fields now (id|kind|buyMax|units|cost|relist|profit|roi|risk|usual),
    9 in files from before "usual" (then the result has no "usual")."""
    p = s.split("|")
    check(len(p) in (9, 10), f"flip line {s!r} has {len(p)} fields, not 9 or 10")
    check(all(re.match(r"^\d+$", x) for x in p[:1] + p[2:8] + p[9:]), f"flip line {s!r}: numbers")
    nums = [int(x) for x in (p[0], p[2], p[3], p[4], p[5], p[6], p[7])]
    out = {"id": nums[0], "kind": p[1], "buyMax": nums[1], "units": nums[2], "cost": nums[3], "relist": nums[4],
           "profit": nums[5], "roi": nums[6], "risk": [r for r in p[8].split(",") if r], "nfields": len(p)}
    if len(p) == 10:
        out["usual"] = int(p[9])
    return out


def py_median(vals):
    """The middle of sorted values (the mean of the two middle ones for an even count)."""
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def week_points(h, hour):
    """The hourly points a usual price comes from: the last 7 days, with a market value."""
    return [p for p in h["h"] if hour - 168 * 3600 < p[0] <= hour and p[2] > 0]


def model_deal(book, usual, sold_day=None, cap=None):
    """The deal rule written out on its own, to hold find_deal_flip to it (book: [(unit price,
    qty)] sorted). Take the listings at most 70% of the usual price, cheapest first, while they fit
    in a day of sales (at least 1; 1 when sales are unknown). Buying the first k of them: relist at
    the lowest of the usual price, the 7-day cap and 1c under listing k+1; it must make 25s and
    15%, and listing k on its own 15% after the 5% cut. All of them whenever that passes ("all");
    only when it does not, the k fewer than all that passes with the most profit, the most
    listings on a tie ("fewer"). (units, buyMax, relist, profit, how) or None."""
    if not usual or usual <= 0 or not book:
        return None
    room = 1 if sold_day is None else max(1, int(sold_day + 1e-9))
    take, got = 0, 0
    for p, q in book:
        if p * 1000 > 700 * usual or got + q > room:
            break
        take += 1
        got += q

    def buy_first(k):
        units = sum(q for _, q in book[:k])
        cost = max(1, int(round(sum(p * q for p, q in book[:k]))))
        relist = min([int(usual)] + ([int(cap)] if cap else []) + ([math.ceil(book[k][0]) - 1] if k < len(book) else []))
        paid = relist * 95 // 100
        profit = units * paid - cost
        if relist < 1 or profit < 2500 or profit * 1000 < 150 * cost or paid * 1000 < book[k - 1][0] * 1150:
            return None
        return units, math.ceil(book[k - 1][0]), relist, profit

    if not take:
        return None
    whole = buy_first(take)
    if whole:
        return whole + ("all",)
    fewer = [f for f in (buy_first(k) for k in range(take - 1, 0, -1)) if f]
    if not fewer:
        return None
    best = max(fewer, key=lambda f: (f[3], f[0]))
    return best + ("fewer",)


def model_sold(prev, snap, by_variant=False):
    """The sold rule written out on its own, to hold estimate_sold to it (prev, snap: auction id ->
    [item id, qty, unit price, time left], plus a vid for a version). A stack still listed sold
    what it got smaller by. A gone one sold all of it when it had LONG or VERY_LONG left and no
    auction of the same item and version from prev that is still listed is cheaper (a tie is no
    veto). An auction without a vid (an older snapshot) has the vid it has now, none when it is
    gone. Per item id, or per vid when by_variant (auctions that are not a version left out)."""
    def version(aid, e):
        if len(e) > 4 and e[4]:
            return e[4]
        now = snap.get(aid)
        return now[4] if now is not None and len(now) > 4 and now[4] else 0

    out = {}
    for aid, e in prev.items():
        who = (e[4] if len(e) > 4 else 0) if by_variant else e[0]
        if not who:
            continue
        now = snap.get(aid)
        if now is not None:
            n = max(0, e[1] - now[1])
        elif e[3] in ("LONG", "VERY_LONG"):
            mine = (e[0], version(aid, e))
            cheaper = [a for a, x in prev.items() if a in snap and (x[0], version(a, x)) == mine and x[2] < e[2]]
            n = 0 if cheaper else e[1]
        else:
            n = 0
        if n:
            out[who] = out.get(who, 0) + n
    return out


def expected_addon_pick(cand):
    """Contract: the best 60 (cand is best first and only ids with a line), plus up to 40 deals
    and then up to 40 flips costing at most 5g that are not in yet, in rank order."""
    pick = set(range(min(len(cand), ADDON_TOP)))
    for wanted, room in ((lambda r: r["kind"] == "deal", ADDON_DEALS), (lambda r: r["cost"] <= CHEAP_COST, ADDON_CHEAP)):
        for i, r in enumerate(cand):
            if room and i not in pick and wanted(r):
                pick.add(i)
                room -= 1
    return [r for i, r in enumerate(cand) if i in pick]


# -- checks -------------------------------------------------------------------
class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


def lua_list(t, n):
    return [t[i + 1] for i in range(n)]


def load_lua(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    check(b"\r" not in raw, "file has CR characters (must be LF only)")
    text = raw.decode("utf-8")          # raises if not UTF-8
    first = text.split("\n", 1)[0]
    check(first == HEADER, f"first line is {first!r}")
    check("ForeverAH_Data = {" in text, "no 'ForeverAH_Data = {'")
    # Update-WoW-Prices.bat (update_prices.ps1) finds the stamp with this exact pattern
    check(re.search(r"(?m)^\s*updated\s*=\s*(\d+)\s*,", text), "the updater cannot find 'updated = <n>,'")
    lua = lua51.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(LUA_DECODER)
    lua.execute(text)                   # raises on any Lua syntax error
    db = lua.globals().ForeverAH_Data
    check(db is not None, "ForeverAH_Data is not set")
    return lua, db, raw


PRICE_CAP = 2 ** 31 - 1


def expected_point(pt, sold=True):
    """hist point -> (cheap, market, sold) as the file should carry it"""
    if pt is None:
        return None, None, 0

    def price(v):
        return min(v, PRICE_CAP) if v > 0 else None
    return price(pt[1]), price(pt[2]), (pt[4] if sold else 0)


def expected_series(pts, end, step, n, bridge):
    """What the n grid points ending at `end` should hold. A missing grid point
    between two snapshots, in a run of at most `bridge` missing points, repeats
    the earlier snapshot's prices with 0 sold; anything else missing is null."""
    by_t = {p[0]: p for p in pts}
    times = [end - (n - 1 - i) * step for i in range(n)]
    snap = [i for i, t in enumerate(times) if t in by_t]
    out = []
    for i, t in enumerate(times):
        if t in by_t:
            out.append(expected_point(by_t[t]))
            continue
        before = [j for j in snap if j < i]
        after = [j for j in snap if j > i]
        if before and after and after[0] - before[-1] - 1 <= bridge:
            out.append(expected_point(by_t[times[before[-1]]], sold=False))
        else:
            out.append((None, None, 0))
    return out


def check_series(label, dec, pts, end, step, cap):
    cheap, market, sold = dec
    n = len(cheap)
    check(1 <= n <= cap, f"{label}: length {n} not in 1..{cap}")
    bridge = fetch_ah.ADDON_BRIDGE_H if step == 3600 else fetch_ah.ADDON_BRIDGE_D
    wants = expected_series(pts, end, step, n, bridge)
    nonnull = 0
    for i in range(n):
        t = end - (n - 1 - i) * step
        want = wants[i]
        got = (cheap[i], market[i], sold[i])
        check(got == want, f"{label} t={t} (index {i}/{n}): file {got} != hist {want}")
        if got[0] is not None or got[1] is not None or got[2] > 0:
            nonnull += 1
    first = (cheap[0], market[0], sold[0])
    has_any = any(p for p in pts if end - (cap - 1) * step <= p[0] <= end
                  and (p[1] > 0 or p[2] > 0 or p[4] > 0))
    if has_any:
        check(first[0] is not None or first[1] is not None or first[2] > 0,
              f"{label}: series does not start at a non-null point")
    else:
        check(n == 1 and nonnull == 0, f"{label}: empty window must be one null point")
    # nothing with data was cut off before the start of the series
    start_t = end - (n - 1) * step
    lost = [p for p in pts if end - (cap - 1) * step <= p[0] < start_t
            and (p[1] > 0 or p[2] > 0 or p[4] > 0)]
    check(not lost, f"{label}: dropped {len(lost)} non-null points before the series start")
    return n


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def run_checks(data_dir, label, expect_status=True, exact_hour=True, quiet=False, current=True):
    """current: the data comes from this collector (10-field flip lines, "usual", deals, the
    addon's extra flips). None = tell from the files (the generated data may come from an older
    collector: 9-field flip lines, no "usual", the best 60 flips only)."""
    path = os.path.join(data_dir, "ForeverAH_Data.lua")
    lua, db, raw = load_lua(path)
    items_meta = load_json(os.path.join(data_dir, "items.json"), {})
    hist_dir = os.path.join(data_dir, "hist")
    state = load_json(os.path.join(data_dir, "state.json"))
    latest = (load_json(os.path.join(data_dir, "latest.json")) or {}).get("data")
    mapping = load_json(os.path.join(data_dir, "mapping.json"))
    vinfo = registry_of(state)            # vid -> (item id, key, index)
    suffixes = load_suffixes(data_dir)

    def meta_for(eid):
        """(metadata the line's vendor price comes from, expected name)"""
        if eid in vinfo:
            base, key, idx = vinfo[eid]
            bm = items_meta.get(str(base)) or {}
            return bm, expected_vid_name(bm, key, idx, suffixes)
        m = items_meta.get(str(eid)) or {}
        return m, expected_name(m)

    updated, hour_end, day_end = int(db.updated), int(db.hourEnd), int(db.dayEnd)
    check(db.v == 1, "v is not 1")
    check(hour_end % 3600 == 0, "hourEnd is not a whole hour")
    if exact_hour:
        check(hour_end == updated - updated % 3600, "hourEnd is not the snapshot hour")
    else:
        check(hour_end >= updated - updated % 3600, "hourEnd is before the snapshot hour")
    check(day_end == hour_end - hour_end % 86400, "dayEnd is not that day's UTC midnight")
    check(db.region == "us" and db.house == 110 and db.ruleset == "PvP"
          and db.houseName == "Alliance Auction House", "header fields wrong")
    check(db.fetched is not None and int(db.fetched) >= updated, "fetched is missing or before updated")
    sales_h = db.salesHours
    check(sales_h is not None and 0 <= int(sales_h) <= 24, f"salesHours {sales_h!r} not in 0..24")
    sales_h = int(sales_h)
    measured = None
    win_secs = None
    if state is not None:
        # state.json "measured": {hour slot: seconds of sales measured in it} (older files: a list of slots)
        m = state.get("measured") or {}
        secs = {int(k): int(v) for k, v in m.items()} if isinstance(m, dict) else {int(t): 3600 for t in m}
        measured = set(secs)
        win_secs = sum(s for t, s in secs.items()
                       if hour_end - 23 * 3600 <= t <= hour_end and (hour_end - t) % 3600 == 0)
        want_h = min(24, int(win_secs / 3600 + 0.5))
        check(sales_h == want_h, f"salesHours {sales_h} != {want_h} hours ({win_secs}s) measured in state.json")
    if expect_status:
        status = load_json(os.path.join(data_dir, "status.json"))
        check(updated == status["updated"], "updated != status.json updated")
        check(int(db.fetched) == status["fetched"], "fetched != status.json fetched")
        check(sales_h == status["salesHours"], "salesHours != status.json salesHours")
        if win_secs is not None:
            check(status.get("salesMinutes") == int(win_secs / 60 + 0.5),
                  f"salesMinutes {status.get('salesMinutes')!r} != {win_secs}s measured")
        check(status.get("soldSkipped") is None or isinstance(status["soldSkipped"], str), "soldSkipped type")
        check(status.get("gapMinutes") is None or isinstance(status["gapMinutes"], int), "gapMinutes type")
        ape = status.get("auctionsPerEndpoint") or {}
        check(set(ape) == {"auctions", "commodities"} and all(isinstance(v, int) for v in ape.values()),
              f"auctionsPerEndpoint {ape!r}")
        check(status["auctions"] == ape["auctions"] + ape["commodities"], "auctions != sum of endpoints")

    # which items must be in the file: any listing or estimated sale in the last 168 hours
    want_ids = set()
    hists = {}
    for fn in os.listdir(hist_dir):
        if not fn.endswith(".json"):
            continue
        iid = int(fn[:-5])
        h = load_json(os.path.join(hist_dir, fn))
        hists[iid] = h
        times = [p[0] for p in h["h"]]
        check(times == sorted(set(times)), f"hist {iid}: hourly times not strictly increasing")
        if any(hour_end - 167 * 3600 <= p[0] <= hour_end and (p[1] > 0 or p[3] > 0 or p[4] > 0)
               for p in h["h"]):
            want_ids.add(iid)

    lua_items = db["items"]
    got_ids = set(int(k) for k in lua_items.keys())
    check(got_ids == want_ids, f"included items differ: missing {sorted(want_ids - got_ids)}, "
                               f"extra {sorted(got_ids - want_ids)}")

    decode = lua.globals().FAH_Decode
    points = 0
    for iid in sorted(got_ids):
        s = lua_items[iid]
        tag = f"{label} item {iid}"
        py = py_decode(s)
        lt = decode(s)
        check(py["nfields"] == FIELDS, f"{tag}: {py['nfields']} fields, a new file must have {FIELDS}")
        # Lua and Python decoders agree
        for k in ("name", "cheapest", "market", "listed", "sold24", "avg7", "vendor", "auctions", "nfields"):
            check(lt[k] == py[k], f"{tag}: Lua {k}={lt[k]!r} Python {k}={py[k]!r}")
        for key in ("H", "D"):
            ls = lt[key]
            n = int(ls.n)
            lua_dec = [[(None if v is False else v) for v in lua_list(ls.cheap, n)],
                       [(None if v is False else v) for v in lua_list(ls.market, n)],
                       lua_list(ls.sold, n)]
            check(lua_dec == py[key], f"{tag} {key}: Lua and Python decoders disagree")

        h = hists[iid]
        meta, want_name = meta_for(iid)
        check(py["name"] == want_name, f"{tag}: name {py['name']!r}, want {want_name!r}")
        check((h.get("v") or None) == (vinfo[iid][1] if iid in vinfo else None),
              f"{tag}: hist 'v' {h.get('v')!r} does not match the version registry")
        check(not re.match(r"^Item \d+$", py["name"]), f"{tag}: placeholder name {py['name']!r}")
        check(py["vendor"] == int(meta.get("sell") or 0), f"{tag}: vendor price")
        now_pt = next((p for p in h["h"] if p[0] == hour_end), None)
        check(py["cheapest"] == (min(now_pt[1], PRICE_CAP) if now_pt else 0), f"{tag}: cheapest now")
        check(py["market"] == (min(now_pt[2], PRICE_CAP) if now_pt else 0), f"{tag}: market now")
        check(py["listed"] == (now_pt[3] if now_pt else 0), f"{tag}: listed now")
        sold24 = sum(p[4] for p in h["h"] if hour_end - 86400 < p[0] <= hour_end
                     and (measured is None or p[0] in measured))
        check(py["sold24"] == sold24, f"{tag}: sold24 {py['sold24']} != {sold24}")
        week = [p[2] for p in h["d"] if day_end - 6 * 86400 <= p[0] <= day_end and p[2] > 0]
        avg7 = int(sum(week) / len(week) + 0.5) if week else 0
        check(py["avg7"] == avg7, f"{tag}: avg7 {py['avg7']} != {avg7}")
        if latest is not None:
            want_n = int((latest.get(str(iid)) or {}).get("n") or 0)
            check(py["auctions"] == want_n, f"{tag}: auctions {py['auctions']} != latest.json n {want_n}")
            check((py["auctions"] > 0) == (py["listed"] > 0), f"{tag}: auctions and listed disagree")
            if py["listed"] > 0:
                check(py["cheapest"] >= 1 and py["market"] >= 1, f"{tag}: listed but a price below 1c")
        points += check_series(f"{tag} H", py["H"], h["h"], hour_end, 3600, H_MAX)
        points += check_series(f"{tag} D", py["D"], h["d"], day_end, 86400, D_MAX)

    if latest is not None:
        for sid, e in latest.items():
            check(isinstance(e.get("n"), int) and e["n"] >= 0, f"{label}: latest.json {sid} has no auctions count")
            if e["n"] > 0:
                check(e["highTime"] == updated and e["lowTime"] == updated,
                      f"{label}: latest.json {sid} time is not Blizzard's data time")
    if mapping is not None:
        for m in mapping:
            check(m["name"] == meta_for(m["id"])[1], f"{label}: mapping name {m!r}")
            check(all(k in m for k in INFO_FIELDS) and isinstance(m["ilvl"], int) and isinstance(m["reqlvl"], int)
                  and m["clsName"] == m["cls"] and m["subName"] == m["sub"], f"{label}: mapping fields {m!r}")
            if m["id"] in vinfo:
                base, key, idx = vinfo[m["id"]]
                check((m.get("base"), m.get("variant"), m.get("vindex")) == (base, key, idx),
                      f"{label}: mapping version fields {m!r}")
            else:
                check("base" not in m and "variant" not in m, f"{label}: item {m['id']} has version fields")

    # C2b: variants = { [itemId] = "key=vid,..." } lists exactly the versions that have a line
    vids_here = {i for i in got_ids if i in vinfo}
    listed_v = {}
    vt = db["variants"]
    if vt is not None:
        for k in vt.keys():
            base = int(k)
            check(base in got_ids, f"{label}: variants entry for {base}, which has no line")
            for part in str(vt[k]).split(","):
                key, vid = part.rsplit("=", 1)
                vid = int(vid)
                check(vinfo.get(vid, (None, None))[:2] == (base, key), f"{label}: variants {base}: {part!r}")
                check(re.match(r"^\d+(:\d+)*$", key) and key.split(":") == [str(b) for b in sorted(int(x) for x in key.split(":"))],
                      f"{label}: variant key {key!r} is not sorted bonus ids")
                listed_v[vid] = base
    check(set(listed_v) == vids_here, f"{label}: variants table and version lines differ: "
                                      f"{sorted(vids_here ^ set(listed_v))[:5]}")
    # the item itself covers all its versions
    for base in set(listed_v.values()):
        mine = [py_decode(lua_items[v]) for v in listed_v if listed_v[v] == base]
        it = py_decode(lua_items[base])
        check(it["listed"] >= sum(d["listed"] for d in mine) and it["auctions"] >= sum(d["auctions"] for d in mine),
              f"{label}: item {base} lists fewer units than its versions")

    # C2c: flips = { "id|kind|buyMax|units|cost|relist|profit|roi|risk|usual", ... } best first
    ft = db["flips"]
    flips = []
    if ft is not None:
        n = 0
        while ft[n + 1] is not None:
            n += 1
            flips.append(parse_flip(str(ft[n])))
    site = load_json(os.path.join(data_dir, "flips.json"))
    rows = site["flips"] if site is not None else []
    nfields = {f["nfields"] for f in flips}
    check(len(nfields) <= 1, f"{label}: flip lines with {sorted(nfields)} fields in one file")
    if current is None:                 # an older collector wrote 9 fields and no "usual"
        current = nfields == {10} or (not flips and any("usual" in r for r in rows))
    if current:
        check(nfields <= {10} and all("usual" in r for r in rows),
              f"{label}: this collector writes 10-field flip lines and flips.json rows with a usual price")
    else:
        check(nfields <= {9} and not any(r["kind"] == "deal" or "usual" in r for r in rows + flips),
              f"{label}: an older file has no deals and no usual prices")
    most = ADDON_TOP + ADDON_DEALS + ADDON_CHEAP if current else ADDON_TOP
    check(len(flips) <= most, f"{label}: {len(flips)} flips in the addon file (at most {most})")
    for i, fl in enumerate(flips):
        tag = f"{label} flip {i + 1}"
        check(fl["id"] in got_ids, f"{tag}: id {fl['id']} has no line")
        check(("suffix" in fl["risk"]) == (fl["id"] in vinfo and fl["kind"] in ("gap", "deal")),
              f"{tag}: suffix flag {fl!r}")
        check(not (fl["kind"] in ("gap", "deal") and fl["id"] in listed_v.values()),
              f"{tag}: a {fl['kind']} flip on item {fl['id']} lumps its versions together")
        check(fl["kind"] != "vendor" or fl["id"] not in vinfo, f"{tag}: vendor flips are for items")
        check_flip_numbers(tag, fl, py_decode(lua_items[fl["id"]])["vendor"])
        if i:
            check(flips[i - 1]["profit"] >= fl["profit"], f"{tag}: not sorted by profit")
    if site is not None:
        check(site["updated"] == updated and len(rows) <= SITE_MAX, f"{label}: flips.json header")
        for i, r in enumerate(rows):
            tag = f"{label} flips.json {i}"
            check(all(k in r for k in FLIP_FIELDS) and isinstance(r["risk"], list), f"{label}: flips.json row {r!r}")
            base_meta, want = meta_for(r["id"])
            check(r["name"] == want.replace("/", "|") or r["name"] == want, f"{label}: flips.json name {r['name']!r}")
            check(r["base"] == (vinfo[r["id"]][0] if r["id"] in vinfo else r["id"])
                  and r["variant"] == (vinfo[r["id"]][1] if r["id"] in vinfo else ""), f"{label}: flips.json base {r!r}")
            check_flip_numbers(tag, r, int(base_meta.get("sell") or 0))
            if i:
                check(rows[i - 1]["profit"] >= r["profit"], f"{label}: flips.json not sorted")
            if r["kind"] == "deal":
                check(base_meta.get("quality") in DEAL_QUALITIES and r["quality"] == base_meta.get("quality"),
                      f"{tag}: deals are for greens, blues and epics {r!r}")
            if not current or r["id"] not in hists:
                continue
            # the usual price of every flip, whatever its kind, against the series' own 7-day
            # history: the median market value, 0 with fewer than 4 hours of it (never today's
            # market value, which a gap flip without history is only judged with)
            week = week_points(hists[r["id"]], hour_end)
            if r["kind"] == "deal":
                check(len(week) >= fetch_ah.FLIP_HIST_MIN_POINTS, f"{tag}: a deal without 4 hours of history {r!r}")
            if len(week) >= fetch_ah.FLIP_HIST_MIN_POINTS:
                med = py_median(sorted(p[2] for p in week))
                check(r["usual"] == int(med), f"{tag}: usual {r['usual']} is not the 7-day median {med}")
                if r["kind"] == "deal":
                    check((r["buyMax"] - 1) * 1000 < DEAL_SHARE_PERMILLE * med,
                          f"{tag}: bought above 70% of the usual price {r!r}")
                    lmed = py_median(sorted(p[3] for p in week))
                    check(("unsure" in r["risk"]) == (lmed < 2),
                          f"{tag}: unsure flag {r['risk']!r}, 7-day median listed {lmed}")
            else:
                check(r["usual"] == 0, f"{tag}: under 4 hours of history: no usual price, 0 ({len(week)} points) {r!r}")

        def key(f):
            return (f["id"], f["kind"], f["buyMax"], f["units"], f["cost"], f["relist"], f["profit"], f["roi"],
                    f["risk"], f.get("usual"))
        cand = [r for r in rows if r["id"] in got_ids]
        got_keys = [key(f) for f in flips]
        if not current:
            check(got_keys == [key(r) for r in cand[:ADDON_TOP]], f"{label}: the addon flips are not the top of flips.json")
        elif len(rows) < SITE_MAX:
            check(got_keys == [key(r) for r in expected_addon_pick(cand)],
                  f"{label}: the addon flips are not the best 60 plus the extra deals and cheap flips of flips.json")
        else:   # flips.json stops at 400: extra deals and cheap flips further down are only in the addon file
            check(got_keys[:ADDON_TOP] == [key(r) for r in cand[:ADDON_TOP]]
                  and all(f["kind"] == "deal" or f["cost"] <= CHEAP_COST for f in flips[ADDON_TOP:]),
                  f"{label}: the addon flips are not the best 60 of flips.json plus deals and cheap flips")

    if not quiet:
        print(f"  {label}: {len(got_ids)} lines ({len(vids_here)} versions), {points} points checked, "
              f"{len(flips)} flips, file {len(raw)} bytes, Lua and Python decoders agree")
    return got_ids, lua_items, decode


def sim_sales_checks(data_dir):
    """sim_fetch.py's seller who pulls and reposts (sim_sales.json, only in simulated data):
    hour by hour the collector's sold must be exactly what the buyers really bought and the
    collector can see (a stack that got smaller, a whole one that had LONG or VERY_LONG left),
    never the units the seller pulled. Returns None when the data has no sim_sales.json."""
    truth = load_json(os.path.join(data_dir, "sim_sales.json"))
    if truth is None:
        return None
    h = load_json(os.path.join(data_dir, "hist", "%d.json" % truth["item"]))
    check(h is not None, f"simulated sales: no hist file for {truth['item']}")
    sold = {p[0]: p[4] for p in h["h"]}
    rows = truth["hours"]          # [hour, bought, seen, pulled, pulled with LONG or VERY_LONG left]
    bad = [(r, sold.get(r[0])) for r in rows if sold.get(r[0]) != r[2]]
    check(rows and not bad, f"simulated sales: sold is not what the buyers bought in {len(bad)} hours "
                            f"([hour, bought, seen, pulled, pulled long], sold): {bad[:3]}")
    check(all(r[1] >= r[2] for r in rows), "simulated sales: more seen than bought")
    pulls = [r for r in rows if r[4] > 0]
    check(len(pulls) >= 10, f"simulated sales: the seller pulled a LONG or VERY_LONG stack in only {len(pulls)} hours")
    return len(rows), sum(r[2] for r in rows), sum(r[4] for r in rows), len(pulls), truth.get("name", "")


def sim_sales_report(data_dir):
    got = sim_sales_checks(data_dir)
    if got is None:
        print("  simulated sales: no sim_sales.json (not simulated data, or made by an older sim_fetch.py), skipped")
        return
    n, seen, pulled, hours, name = got
    print(f"  simulated sales: {name}, {n} hours, sold = the {seen} units buyers bought that a snapshot shows; "
          f"the {pulled} units pulled in {hours} hours while a cheaper one stayed up never count")


# -- hand-made edge cases -----------------------------------------------------
def edge_case_dir(tmp):
    hour = 1794934800                    # a whole hour (2026-11-17 17:00 UTC)
    day = hour - hour % 86400
    H = 3600
    D = 86400
    CAP = 2 ** 31 - 1                    # Classic gold cap in copper
    items = {
        "1": {"name": 'Quote " and back\\slash', "sell": 7},
        "2": {"name": "Pipe | name\nwith newline\tand tab", "sell": 0},
        "3": {"name": "Gold cap thing \u00e9\u00e8 \u2603", "sell": 12345},
        "4": {"name": "Sold only", "sell": 1},
        "5": {"name": "Edge of window", "sell": 2},
        "6": {"name": "Just outside window", "sell": 3},
        "7": {"name": "Listed but never priced zero sold", "sell": 4},
        "8": {"name": "]] [[ -- ; , ~", "sell": 5},
        "10": {"name": "Silly price", "sell": 1},
        "11": {"name": "Gappy", "sell": 1},
        "12": {"name": "Item 12", "sell": 9, "unknown": True},   # placeholder from an older version
        # 9 has no items.json entry on purpose -> ""
    }
    hist = {
        # gaps, up and down moves, unchanged prices, a null hour in the middle
        1: {"h": [[hour - 30 * H, 100, 110, 5, 0], [hour - 29 * H, 100, 110, 5, 2],
                  [hour - 27 * H, 90, 120, 4, 1], [hour - 26 * H, 0, 0, 0, 3],
                  [hour - 3 * H, 95, 120, 4, 0], [hour, 50, 60, 9, 1]],
            "d": [[day - 2 * D, 100, 112, 5, 3], [day - D, 0, 0, 0, 0], [day, 72, 90, 6, 4]]},
        # far older hourly history (outside 168 h) plus a recent point
        2: {"h": [[hour - 300 * H, 5, 6, 1, 0], [hour - 167 * H + H, 7, 8, 1, 0], [hour - 5 * H, 7, 8, 1, 0]],
            "d": [[day - 450 * D, 1, 1, 1, 0], [day - 399 * D, 3, 4, 1, 0], [day - 13 * D, 7, 8, 1, 0],
                  [day, 7, 8, 1, 0]]},
        # gold-cap prices and the biggest possible drops
        3: {"h": [[hour - 2 * H, CAP, CAP, 1, 0], [hour - H, 1, 1, 1, 1], [hour, CAP, CAP - 1, 1, 0]],
            "d": [[day, CAP // 2, CAP // 2, 1, 1]]},
        # sold but nothing listed at all in the window
        4: {"h": [[hour - 10 * H, 0, 0, 0, 4], [hour, 0, 0, 0, 0]],
            "d": [[day, 0, 0, 0, 4]]},
        # only activity exactly 167 hours ago: the first grid point, included
        5: {"h": [[hour - 167 * H, 40, 41, 2, 0], [hour, 0, 0, 0, 0]],
            "d": [[day - 7 * D, 40, 41, 2, 0], [day, 0, 0, 0, 0]]},
        # only activity 168 hours ago: just outside, excluded
        6: {"h": [[hour - 168 * H, 40, 41, 2, 0], [hour, 0, 0, 0, 0]],
            "d": [[day - 7 * D, 40, 41, 2, 0], [day, 0, 0, 0, 0]]},
        # avg7 must skip zero days and days older than 7
        7: {"h": [[hour - 1 * H, 0, 0, 3, 0], [hour, 300, 333, 3, 0]],
            "d": [[day - 7 * D, 999999, 999999, 1, 0], [day - 6 * D, 10, 11, 1, 0], [day - 3 * D, 0, 0, 0, 0],
                  [day - D, 20, 20, 1, 0], [day, 300, 333, 3, 0]]},
        8: {"h": [[hour, 1, 1, 1, 0]], "d": [[day, 1, 1, 1, 0]]},
        # a silly price far above the gold cap (would saturate a 32-bit strtoul in game)
        10: {"h": [[hour - 2 * H, 50, 60, 1, 0], [hour - H, 99999990000, 99999990000, 1, 0], [hour, 50, 60, 1, 0]],
             "d": [[day, 99999990000, 99999990000, 1, 0]]},
        # one missing hour, then a run of 7 missing hours (too long to bridge)
        11: {"h": [[hour - 10 * H, 80, 90, 2, 1], [hour - 8 * H, 85, 95, 2, 0], [hour, 70, 75, 2, 0]],
             "d": [[day - 3 * D, 80, 90, 2, 1], [day - D, 85, 95, 2, 0], [day, 70, 75, 2, 0]]},
        9: {"h": [[hour - 4 * H, 36, 1296, 1, 46656]], "d": [[day, 36, 1296, 1, 46656]]},
        12: {"h": [[hour, 400, 410, 3, 0]], "d": [[day, 400, 410, 3, 0]]},
    }
    auctions = {1: 3, 3: 1, 12: 2}       # separate auctions listed now
    os.makedirs(os.path.join(tmp, "hist"))
    with open(os.path.join(tmp, "items.json"), "w", encoding="utf-8") as fh:
        json.dump(items, fh)
    lines = {}
    for iid, h in hist.items():
        with open(os.path.join(tmp, "hist", f"{iid}.json"), "w", encoding="utf-8") as fh:
            json.dump(h, fh)
        now_pt = next((p for p in h["h"] if p[0] == hour), [hour, 0, 0, 0, 0])
        line = fetch_ah.addon_item_line(iid, items.get(str(iid)), h, now_pt[1], now_pt[2], now_pt[3], hour,
                                        auctions=auctions.get(iid, 0))
        if line:
            lines[iid] = line
    # every hour's sold counts here (no measured set), so the whole day is measured
    fetch_ah.write_addon_data(tmp, lines, hour + 1234, hour, "Alliance Auction House",
                              fetched=hour + 1300, sales_hours=24)
    check(not os.path.exists(os.path.join(tmp, "ForeverAH_Data.lua.tmp")), "temp file left behind")
    return hour


def edge_cases():
    tmp = tempfile.mkdtemp(prefix="fah_edge_")
    try:
        edge_case_dir(tmp)
        ids, items, decode = run_checks(tmp, "edge cases", expect_status=False)
        check(6 not in ids and 5 in ids and 4 in ids and 9 in ids and 10 in ids and 11 in ids, "7-day window edges wrong")
        names = {i: decode(items[i]).name for i in ids}
        check(names[1] == 'Quote " and back\\slash', f"name 1 {names[1]!r}")
        check(names[2] == "Pipe / name\nwith newline\tand tab", f"name 2 {names[2]!r}")
        check(names[3] == "Gold cap thing \u00e9\u00e8 \u2603", f"name 3 {names[3]!r}")
        check(names[8] == "]] [[ -- ; , ~", f"name 8 {names[8]!r}")
        check(names[9] == "", f"name 9 (no metadata) should be empty, is {names[9]!r}")
        check(names[12] == "", f"name 12 (old 'Item 12' placeholder) should be empty, is {names[12]!r}")
        d12 = py_decode(items[12])
        check(d12["cheapest"] == 400 and d12["market"] == 410 and d12["vendor"] == 9, "unnamed item keeps its prices")
        check(py_decode(items[1])["auctions"] == 3 and py_decode(items[3])["auctions"] == 1
              and py_decode(items[4])["auctions"] == 0, "auctions field")
        d7 = py_decode(items[7])
        check(d7["avg7"] == int((11 + 20 + 333) / 3 + 0.5), f"avg7 for item 7 is {d7['avg7']}")
        d4 = py_decode(items[4])
        check(d4["H"][0][0] is None and d4["H"][2][0] == 4, "sold-only series wrong")
        d2 = py_decode(items[2])
        check(len(d2["D"][0]) == 400, f"D for item 2 should be capped at 400, is {len(d2['D'][0])}")
        check(len(d2["H"][0]) == 167, f"H for item 2 should be 167 long, is {len(d2['H'][0])}")
        d5 = py_decode(items[5])
        check(len(d5["H"][0]) == 168, "H for item 5 should be the full 168")
        raw = items[1]
        check(",," in raw or ";;" in raw or "|," in raw or ",;" in raw, "zero deltas should be empty tokens")
        # item 1: hour-28 was not snapshotted, between hour-29 and hour-27: it repeats hour-29
        h1 = py_decode(items[1])["H"]
        n1 = len(h1[0])
        i28 = n1 - 1 - 28
        check((h1[0][i28], h1[1][i28], h1[2][i28]) == (100, 110, 0), "one missing hour is bridged")
        i10 = n1 - 1 - 10
        check(h1[0][i10] is None, "a long gap after a nothing-listed hour stays null")
        d10 = py_decode(items[10])
        check(d10["cheapest"] == 50 and max(v for v in d10["H"][0] if v) == PRICE_CAP, "prices clamped to the gold cap")
        check(d10["D"][0] == [PRICE_CAP], "daily price clamped to the gold cap")
        d11 = py_decode(items[11])["H"]
        check(d11[0][1] == 80 and d11[2][1] == 0, "item 11: missing hour bridged with 0 sold")
        check(all(v is None for v in d11[0][3:10]), "item 11: 7 missing hours stay null")
        dd11 = py_decode(items[11])["D"]
        check(dd11[0] == [80, 80, 85, 70], "item 11: one missing day bridged")
        # an old 9-field line (no auctions count) still decodes in both decoders
        old = "Old line|5|6|7|0|0|1|5;6;|5;6;"
        lt = decode(old)
        check(lt.nfields == 9 and lt.auctions is None and py_decode(old)["auctions"] is None, "old 9-field line")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def line_body(line):
    """The Lua string inside an `[id] = "...",` line (these test names need no unescaping)."""
    return line.split('= "', 1)[1].rsplit('",', 1)[0]


# -- collector unit tests -----------------------------------------------------
def market_tests():
    mv = fetch_ah.market_value
    # deep markets: TSM style, as before
    check(mv([(1000, 1), (1650, 1200)]) == 1650, "one cheap unit before a deep stack does not set the market")
    check(mv([(1, 1), (1650, 1200)]) == 1650, "a 1c troll listing does not set the market")
    check(mv([(1650, 1200)]) == 1650, "single price")
    check(mv([(100, 50), (200, 50)]) == 100, "the cheapest 15% decides")
    check(mv([(100, 10), (110, 10), (120, 10), (500, 70)]) == 110, "15-30% sample, the jump to 500 is cut")
    check(mv([]) == 0, "nothing listed")
    # thin markets (fewer than 10 units): the second cheapest unit (the middle of the cheapest 3)
    check(mv([(5, 1)]) == 5, "single unit")
    check(mv([(100000000, 1)]) == 100000000, "a single 10,000g listing is the market (the auctions count says it is thin)")
    check(mv([(1, 1), (5000, 1), (5100, 1)]) == 5000, "a 1c troll among 3 listings does not set a thin market")
    check(mv([(50000, 1), (52000, 1), (100000000, 1)]) == 52000, "one silly listing does not set a thin market")
    check(mv([(1, 1), (5000, 3)]) == 5000, "a cheap outlier does not set it")
    check(mv([(5000, 1), (5200, 1)]) == 5000, "two units: the cheaper one, a price someone really listed at")
    check(mv([(10000, 1), (100000000, 1)]) == 10000, "two units: a silly listing next to a normal one does not set it")
    check(mv([(100, 1), (110, 1), (999999, 7)]) == 110, "pricier listings further up never pull a thin market up")
    check(mv([(100, 2), (200, 7)]) == 100, "9 units is thin: the second cheapest unit")
    check(mv([(100, 2), (200, 8)]) == 100, "10 units is deep: the cheapest 15-30%")
    check(fetch_ah.THIN_UNITS == 10, "thin market limit is 10 units")
    # Sentry's Cape as listed in the first real snapshot: the 3 capes at 10s 33c are what
    # buyers take. Selling one of them must not lift the market to the 1g+ listings.
    cape = [(1033, 1)] * 3 + [(10000, 1), (10000, 1), (11494, 1), (14364, 1), (15770, 1), (15770, 1), (20725, 1)]
    check((mv(cape), mv(cape[1:]), mv(cape[2:])) == (1033, 1033, 10000),
          f"Sentry's Cape: 3 cheap capes, then 2, then a lone one ({mv(cape)}, {mv(cape[1:])}, {mv(cape[2:])})")

    # one unit selling at the 10 / 9 unit boundary moves the market less than 2x
    def sell_cheapest(u):
        p, q = u[0]
        return ([(p, q - 1)] if q > 1 else []) + list(u[1:])
    for u in (cape, [(1, 1)] + [(10000, 1)] * 9, [(100, 3), (105, 7)], [(900, 1), (950, 1), (1000, 1), (5000, 7)],
              [(858, 1), (899, 1), (900, 1), (3858, 1), (7464, 1), (9899, 1), (9900, 3), (16458, 1)]):
        a, b = mv(u), mv(sell_cheapest(u))
        check(sum(q for _, q in u) == 10 and max(a, b) < 2 * min(a, b), f"10 -> 9 units moves the market {a} -> {b}")
    # fractions of a copper stay inside, outputs are whole copper and never below 1c
    check(mv([(0.4, 200)]) == 1, "0.4c per unit is shown as 1c, not 0")
    check(mv([(0.4, 3)]) == 1, "thin 0.4c market is 1c")
    check(mv([(2.4, 15), (2.6, 15), (90, 70)]) == 3, "fractions are averaged before rounding (2.5c -> 3c)")
    check(isinstance(mv([(2.4, 15), (2.6, 15)]), int), "market value is a whole number")
    check(fetch_ah.copper(0.4) == 1 and fetch_ah.copper(2.5) == 3 and fetch_ah.copper(2.49) == 2, "copper rounding")

    # aggregation keeps every auction with a buyout
    per, snap, vper = fetch_ah.aggregate([
        {"id": 1, "item": {"id": 5}, "quantity": 200, "buyout": 80, "time_left": "LONG"},    # 0.4c each
        {"id": 2, "item": {"id": 5}, "quantity": 1, "bid": 50, "time_left": "LONG"},         # bid only
        {"id": 3, "item": {"id": 6}, "quantity": 20, "unit_price": 180, "time_left": "SHORT"},
        {"id": 4, "item": {"id": 6}, "quantity": 0, "unit_price": 180},
        {"id": 5, "item": {}, "quantity": 1, "buyout": 10},
        {"id": 6, "item": {"id": 7}, "quantity": 3, "buyout": 10},                           # 3.33c each
    ])
    check(per[5] == [(0.4, 200)], f"a 0.4c-per-unit stack is kept: {per.get(5)!r}")
    check(per[6] == [(180.0, 20)], "commodity unit_price used")
    check(abs(per[7][0][0] - 10 / 3) < 1e-9, "fractional unit price kept")
    check(set(snap) == {"1", "3", "6"}, f"snapshot ids {sorted(snap)}")
    check(snap["1"] == [5, 200, 1, "LONG"], f"snapshot entry {snap['1']!r}")


def sold_tests():
    prev = {"1": [5, 10, 100, "LONG"], "2": [5, 4, 100, "SHORT"], "3": [6, 20, 50, "VERY_LONG"],
            "4": [6, 5, 50, "MEDIUM"], "5": [7, 3, 9, "LONG"]}
    snap = {"3": [6, 12, 50, "LONG"], "4": [6, 5, 50, "SHORT"], "5": [7, 3, 9, "MEDIUM"], "9": [8, 1, 1, "LONG"]}
    check(fetch_ah.estimate_sold(prev, snap) == {5: 10, 6: 8}, f"sold {fetch_ah.estimate_sold(prev, snap)}")

    sc = fetch_ah.sold_check
    big = {str(i): [1, 1, 1, "LONG"] for i in range(1000)}
    t = 1794936300
    r, g = sc({}, big, None, t)
    check(r and "first snapshot" in r and g is None, f"first snapshot: {r!r} {g!r}")
    r, g = sc(big, big, t - 90 * 60, t)
    check(r is None and g == 90, "a 90 minute gap is still measured")
    r, g = sc(big, big, t - 91 * 60, t)
    check(r and "91 minutes" in r and g == 91, f"a 91 minute gap is skipped: {r!r}")
    r, g = sc(big, big, t - 26 * 3600, t)
    check(r and g == 26 * 60, "a day-long gap is skipped")
    small = dict(list(big.items())[:690])
    r, g = sc(big, small, t - 3600, t)
    check(r and "31%" in r and g == 60, f"auction count fell 31%: {r!r}")
    ok = dict(list(big.items())[:710])
    check(sc(big, ok, t - 3600, t)[0] is None, "a 29% fall is still measured")
    r, _ = sc(big, big, t - 3600, t, failed=["commodities"])
    check(r and "commodities" in r, f"failed download: {r!r}")
    r, _ = sc(big, big, t - 3600, t, {"auctions": 1000, "commodities": 100}, {"auctions": 900, "commodities": 400})
    check(r and "commodities count fell 75%" in r, f"one endpoint collapsed: {r!r}")
    r, _ = sc(big, big, t - 3600, t, {"auctions": 1000, "commodities": 0}, {"auctions": 1000, "commodities": 50})
    check(r is None, "tiny endpoints are not checked on their own")
    check(fetch_ah.SOLD_MAX_GAP == 90 * 60 and fetch_ah.SOLD_MAX_DROP == 0.30, "limits are 90 minutes and 30%")
    # a list that fails only matters when it had auctions last time (or last time is not known)
    inc = fetch_ah.incomplete_download
    ep0 = {"auctions": 1000, "commodities": 0}
    check(inc(big, big, ep0, ep0, ["commodities"]) is None, "a list that was empty last time loses nothing when it fails")
    check(sc(big, big, t - 3600, t, ep0, ep0, ["commodities"])[0] is None, "so the hour is still measured")
    r = inc(big, big, ep0, {"auctions": 900, "commodities": 100}, ["commodities"])
    check(r and "commodities" in r, f"a list that had auctions last time and failed: {r!r}")
    r = inc({}, big, ep0, None, ["commodities"])
    check(r and "commodities" in r, f"first snapshot with a failed list: {r!r}")
    check(inc({}, big) is None and inc(big, big) is None, "nothing to compare, or nothing missing")
    check("31%" in (inc(big, small) or ""), "the count check is the same one sold_check uses")

    hour = 1794934800
    hs = fetch_ah.sales_hours
    check(hs(set(), hour) == 0, "no measured hours")
    check(hs({hour, hour - 23 * 3600}, hour) == 2, "both ends of the 24 hour window count")
    check(hs({hour - 24 * 3600, hour + 3600, hour - 1800}, hour) == 0, "outside the window or off the grid")
    check(hs({hour - k * 3600 for k in range(40)}, hour) == 24, "at most 24")
    # measured minutes, not slots: Blizzard at 00:05, 00:50, 01:59, 02:01 measures 45 + 69 + 2 minutes
    lm = {hour - 7200: 45 * 60, hour - 3600: 69 * 60, hour: 2 * 60}
    check(fetch_ah.sales_seconds(lm, hour) == 116 * 60 and hs(lm, hour) == 2,
          f"116 measured minutes are 2 hours, not 3 slots ({hs(lm, hour)})")
    check(hs({hour - k * 3600: 88 * 60 for k in range(24)}, hour) == 24, "long comparisons never make more than 24")
    check(hs({hour: 29 * 60}, hour) == 0 and hs({hour: 31 * 60}, hour) == 1, "rounded to whole hours")
    ms = fetch_ah.measured_secs
    check(ms({"measured": [hour, hour - 3600]}) == {hour: 3600, hour - 3600: 3600}, "older state: a list of hour slots")
    check(ms({"measured": {str(hour): 120}}) == {hour: 120} and ms({}) == {}, "state: seconds per hour slot")

    # sold24 in the addon line only adds up measured hours
    h = {"h": [[hour - 2 * 3600, 10, 10, 5, 7], [hour - 3600, 10, 10, 5, 4], [hour, 10, 10, 5, 2]],
         "d": [[hour - hour % 86400, 10, 10, 5, 13]]}
    line = fetch_ah.addon_item_line(1, {"name": "X", "sell": 1}, h, 10, 10, 5, hour, measured={hour, hour - 3600})
    check(py_decode(line_body(line))["sold24"] == 6, "sold24 = measured hours only")
    line = fetch_ah.addon_item_line(1, {"name": "X", "sell": 1}, h, 10, 10, 5, hour)
    check(py_decode(line_body(line))["sold24"] == 13, "sold24 without a measured set counts every hour")


def sold_floor_tests():
    """A gone LONG/VERY_LONG auction counts only when it is priced at or under the floor: the
    cheapest of the previous snapshot's auctions of the same item (the same version, for a
    version) that are still listed, whole or partly bought. Buyers take the cheapest first."""
    es = fetch_ah.estimate_sold
    # the audit's cases: a 10 @1s LONG and b 5 @90c
    prev = {"a": [1, 10, 100, "LONG"], "b": [1, 5, 90, "LONG"]}
    check(es(prev, {"b": [1, 5, 90, "LONG"]}) == {}, "a gone while the cheaper b stayed up: pulled, not sold")
    check(es(prev, {"b": [1, 3, 90, "LONG"]}) == {1: 2}, "b partly bought (a shrink) is still the floor: only its 2")
    check(es(prev, {}) == {1: 15}, "both gone: no floor, both count")
    tie = {"a": [1, 10, 90, "LONG"], "b": [1, 5, 90, "LONG"]}
    check(es(tie, {"b": [1, 5, 90, "LONG"]}) == {1: 10}, "a tie at the floor counts")
    # the floor is the cheapest listing still up, not any of them: a gone 95c one next to a 90c and a
    # 1s 20c one that stayed (whole or bought from) was pulled
    prev = {"a": [1, 10, 95, "LONG"], "b": [1, 5, 90, "LONG"], "c": [1, 5, 120, "LONG"]}
    check(es(prev, {"b": [1, 5, 90, "LONG"], "c": [1, 5, 120, "LONG"]}) == {}, "the floor is the cheapest one still up")
    check(es(prev, {"b": [1, 2, 90, "LONG"], "c": [1, 5, 120, "LONG"]}) == {1: 3},
          "the cheapest one still up is the floor when it was bought from too")
    # no slack over the floor: 1c (or 0.5%) over it, or a fraction of a copper a unit, is not a sale
    check(es({"a": [1, 10, 10001, "LONG"], "b": [1, 5, 10000, "LONG"]}, {"b": [1, 5, 10000, "LONG"]}) == {},
          "1c over the floor is not a sale")
    check(es({"a": [1, 10, 10050, "LONG"], "b": [1, 5, 10000, "LONG"]}, {"b": [1, 5, 10000, "LONG"]}) == {},
          "0.5% over the floor is not a sale")
    stacks = {"a": [1, 2, 3.5, "LONG"], "b": [1, 3, 10 / 3, "LONG"]}        # 2 for 7c, 3 for 10c
    check(es(stacks, {"b": [1, 3, 10 / 3, "LONG"]}) == {} and es(stacks, {"a": [1, 2, 3.5, "LONG"]}) == {1: 3},
          "stacks a fraction of a copper a unit over the floor are not a sale, under it they are")
    # a version's floor is the cheapest of that version still up
    prev = {"e": [1, 1, 400, "LONG", 1001], "e2": [1, 1, 300, "LONG", 1001], "e3": [1, 1, 900, "LONG", 1001]}
    now = {"e2": [1, 1, 300, "LONG", 1001], "e3": [1, 1, 900, "LONG", 1001]}
    check(es(prev, now) == {} and es(prev, now, by_variant=True) == {}, "a version's floor is its cheapest one still up")
    # two versions never set each other's floor: an Eagle at 5s is gone while a Monkey at 50c stays
    prev = {"e": [1, 1, 500, "LONG", 1001], "m": [1, 1, 50, "LONG", 1002]}
    now = {"m": [1, 1, 50, "LONG", 1002]}
    check(es(prev, now) == {1: 1} and es(prev, now, by_variant=True) == {1001: 1},
          f"versions never veto each other ({es(prev, now)}, {es(prev, now, by_variant=True)})")
    # ... and a cheaper Eagle that stayed up does
    prev["e2"] = [1, 1, 300, "LONG", 1001]
    now["e2"] = [1, 1, 300, "LONG", 1001]
    check(es(prev, now) == {} and es(prev, now, by_variant=True) == {}, "the same version: the 3s Eagle sets the floor")
    # a version and the plain item never set each other's floor either
    prev = {"p": [1, 1, 10, "LONG"], "p2": [1, 1, 20, "LONG"], "v": [1, 1, 500, "LONG", 1001]}
    check(es(prev, {"p": [1, 1, 10, "LONG"]}) == {1: 1}, "the plain 10c item vetoes the plain 20c one, not the version")
    # a commodity: the cheapest stack partly bought, dearer stacks pulled and one listed again cheaper
    prev = {"1": [2, 20, 180, "LONG"], "2": [2, 20, 175, "VERY_LONG"], "3": [2, 10, 190, "VERY_LONG"],
            "4": [2, 50, 175, "VERY_LONG"], "5": [3, 7, 60, "LONG"]}
    now = {"2": [2, 5, 175, "VERY_LONG"], "4": [2, 50, 175, "VERY_LONG"], "9": [2, 30, 174, "VERY_LONG"]}
    check(es(prev, now) == {2: 15, 3: 7}, f"commodity: only the shrink, not the pulled stacks ({es(prev, now)})")
    # single items (quantity 1, no versions): a dearer one gone while a cheaper one stayed up
    prev = {"1": [4, 1, 5000, "LONG"], "2": [4, 1, 4000, "VERY_LONG"], "3": [4, 1, 3000, "LONG"],
            "4": [4, 1, 4000, "LONG"]}
    now = {"2": [4, 1, 4000, "VERY_LONG"]}
    check(es(prev, now) == {4: 2}, f"single items: the 30s and the tied 40s one count, not the 50s one ({es(prev, now)})")
    # the floor comes from any listing still up, whatever its time left; SHORT and MEDIUM gone never count
    prev = {"1": [5, 1, 100, "SHORT"], "2": [5, 4, 300, "LONG"], "3": [5, 2, 50, "MEDIUM"], "4": [5, 3, 90, "LONG"]}
    check(es(prev, {"1": [5, 1, 100, "SHORT"]}) == {5: 3}, "a SHORT listing still up sets the floor too")
    check(es(prev, {}) == {5: 7}, "nothing still up (an empty floor): every LONG one counts, never SHORT or MEDIUM")
    # a new listing (a repost) never sets the floor: only the previous snapshot's auctions do
    check(es({"1": [6, 2, 100, "LONG"]}, {"2": [6, 2, 1, "VERY_LONG"]}) == {6: 2}, "a new cheaper listing is no floor")
    # another item never sets the floor
    check(es({"1": [7, 2, 100, "LONG"], "2": [8, 2, 1, "LONG"]}, {"2": [8, 2, 1, "LONG"]}) == {7: 2}, "per item")
    # an older snapshot without vids (the upgrade run): a listing still up takes its vid from
    # the new snapshot, so a gone one (its version unknown) is only held against plain listings
    prev = {"b": [1, 1, 15000, "LONG"], "e": [1, 1, 9000, "LONG"]}
    check(es(prev, {"e": [1, 1, 9000, "LONG", 1002]}) == {1: 1}, "upgrade: a cheaper version still up is no floor")
    check(es(prev, {"e": [1, 1, 9000, "LONG"]}) == {}, "upgrade: a cheaper plain listing still up is a floor")
    check(es(prev, {"e": [1, 1, 9000, "LONG", 1002]}, by_variant=True) == {}, "upgrade: no version sales yet")
    # random books against the rule written out on its own (model_sold): two items, versions, prices
    # tied, 1c and 0.5% apart and in fractions of a copper, stacks that go, shrink or stay with any
    # time left, new listings, and older snapshots without vids
    rnd = random.Random(23)
    prices = (89, 90, 90, 91, 100, 120, 10 / 3, 3.5, 10000, 10001, 10050)
    left = ("SHORT", "MEDIUM", "LONG", "VERY_LONG")
    vetoed = ties = 0
    for _ in range(4000):
        old = rnd.random() < 0.2
        prev, now = {}, {}
        for n in range(rnd.randint(1, 8)):
            iid = rnd.choice((1, 1, 2))
            vid = rnd.choice((0, 1001, 1002) if iid == 1 else (0, 2001))
            q = rnd.choice((1, 1, 2, 5, 20))
            e = [iid, q, rnd.choice(prices), rnd.choice(left + ("VERY_LONG",))]
            prev[str(n)] = e + ([vid] if vid and not old else [])
            fate = rnd.random()
            if fate >= 0.35:                                   # still listed: bought from, or not
                kept = rnd.randint(1, q - 1) if q > 1 and fate < 0.55 else q
                now[str(n)] = [iid, kept, e[2], rnd.choice(left)] + ([vid] if vid else [])
        for n in range(rnd.randint(0, 2)):                     # new listings never set the floor
            iid = rnd.choice((1, 2))
            now[f"n{n}"] = [iid, rnd.choice((1, 5)), rnd.choice(prices), "VERY_LONG"] + (
                [rnd.choice((1001, 1002))] if iid == 1 and rnd.random() < 0.5 else [])
        for bv in (False, True):
            got, want = es(prev, now, by_variant=bv), model_sold(prev, now, bv)
            check(got == want, f"random book (by_variant={bv}): {got} != {want} for {prev} -> {now}")
        gone = [e for a, e in prev.items() if a not in now and e[3] in fetch_ah.SOLD_TIME_LEFT]
        shrunk = sum(e[1] - now[a][1] for a, e in prev.items() if a in now)
        vetoed += sum(model_sold(prev, now).values()) < shrunk + sum(e[1] for e in gone)
        ties += any(x[2] == e[2] for e in gone for a, x in prev.items() if a in now and x[0] == e[0])
    check(vetoed >= 500 and ties >= 400, f"random books: the floor mattered in {vetoed}, ties in {ties} of 4000")


def name_and_time_tests():
    iu, nm = fetch_ah.is_unknown, fetch_ah.item_name
    check(nm({"name": "Linen Cloth"}) == "Linen Cloth" and not iu({"name": "Linen Cloth"}), "real name")
    check(nm({"name": "Item 282712", "unknown": True}) == "", "old placeholder with the flag")
    check(nm({"name": "Item 282712"}) == "", "old placeholder without the flag")
    check(nm({"name": "Item 5 of a set"}) == "Item 5 of a set", "only the exact placeholder pattern is unknown")
    check(nm(None) == "" and nm({}) == "" and nm({"name": ""}) == "", "missing names")

    # retries: new ids always, unknown ones every 6 hours at most, bounded per run
    now = 1794936300
    calls = []
    lock = threading.Lock()

    def fake(iid):
        with lock:
            calls.append(iid)
        if iid in (1, 21):
            return iid, {"name": "Found %d" % iid, "icon": "", "quality": "COMMON", "sell": 3, "buy": 0,
                         "cls": "", "sub": "", "lvl": 0, "stack": 0}
        if iid == 22:     # Blizzard knows it but has no name: keep the vendor price
            return iid, {"name": "", "icon": "i", "quality": "COMMON", "sell": 77, "buy": 0,
                         "cls": "", "sub": "", "lvl": 0, "stack": 0, "unknown": True}
        return iid, None
    items = {
        "10": {"name": "Known", "sell": 1},
        "20": {"name": "Item 20", "unknown": True, "tried": now - 3600, "sell": 0},       # tried 1h ago
        "21": {"name": "Item 21", "unknown": True, "tried": now - 7 * 3600, "sell": 0},   # due
        "22": {"name": "Item 22", "unknown": True, "sell": 0},                            # never tried: due
        "23": {"name": "Item 23", "unknown": True, "tried": now - 9 * 3600, "sell": 0},   # due, not listed
        "24": {"name": "Item 24", "unknown": True, "tried": now - 8 * 3600, "sell": 0},   # due, not listed
    }
    n_new, n_retry, found = fetch_ah.refresh_items(items, [1, 2, 10, 20, 21, 22], now, fetch=fake,
                                                   retry_max=3, threads=2)
    check((n_new, n_retry, found) == (2, 3, 1), f"refresh counts {(n_new, n_retry, found)}")
    check(sorted(calls) == [1, 2, 21, 22, 23], f"looked up {sorted(calls)}: new ids, then listed due ones, then the oldest")
    check(items["1"]["name"] == "Found 1" and "unknown" not in items["1"], "new item found")
    check(items["2"] == dict(fetch_ah.unknown_entry(now)), f"new unknown item {items['2']!r}")
    check(items["21"]["name"] == "Found 21" and "unknown" not in items["21"], "retry found the name")
    check(items["22"]["name"] == "" and items["22"]["sell"] == 77 and items["22"]["tried"] == now, "partial details kept")
    check(items["20"]["tried"] == now - 3600 and items["20"]["name"] == "", "tried 1h ago: not retried, placeholder cleared")
    check(items["23"]["tried"] == now and items["24"]["tried"] == now - 8 * 3600, "bounded: the oldest unlisted one first")
    check(fetch_ah.UNKNOWN_RETRY_SECS == 6 * 3600 and fetch_ah.UNKNOWN_RETRY_MAX == 300, "retry limits")

    # Blizzard's data time
    ph = fetch_ah.parse_http_date
    check(ph("Tue, 17 Nov 2026 22:25:03 GMT") == 1794954303, "HTTP date parsed")
    check(ph(email.utils.formatdate(1794936300, usegmt=True)) == 1794936300, "round trip")
    check(ph("") is None and ph(None) is None and ph("garbage") is None and ph("1794953729") is None, "not dates")
    check(fetch_ah.header({"last-modified": "x"}, "Last-Modified") == "x", "header lookup ignores case")

    # changed=true/false for the workflow
    tmp = tempfile.mkdtemp(prefix="fah_gh_")
    old = os.environ.get("GITHUB_OUTPUT")
    try:
        os.environ.pop("GITHUB_OUTPUT", None)
        fetch_ah.tell_workflow(True)            # nothing to write to: does nothing
        path = os.path.join(tmp, "out.txt")
        os.environ["GITHUB_OUTPUT"] = path
        fetch_ah.tell_workflow(False)
        fetch_ah.tell_workflow(True)
        with open(path, encoding="utf-8") as fh:
            check(fh.read() == "changed=false\nchanged=true\n", "GITHUB_OUTPUT lines")
    finally:
        if old is None:
            os.environ.pop("GITHUB_OUTPUT", None)
        else:
            os.environ["GITHUB_OUTPUT"] = old
        shutil.rmtree(tmp, ignore_errors=True)


def size_tests():
    """the size guard: shrink the quietest items first, 90 then 30 days"""
    tmp = tempfile.mkdtemp(prefix="fah_size_")
    try:
        hour = 1794934800
        day = hour - hour % 86400
        hist = {}
        lines = {}
        for iid in range(1, 6):
            h = {"h": [[hour - k * 3600, 1000 + k * 7 + iid, 1100 + k * 5, 10, iid] for k in range(167, -1, -1)],
                 "d": [[day - k * 86400, 900 + k * 3 + iid, 950 + k * 2, 10, iid * 3] for k in range(399, -1, -1)]}
            hist[iid] = h
            lines[iid] = fetch_ah.addon_item_line(iid, {"name": "Thing %d" % iid, "sell": 1}, h, 1000 + iid, 1100, 10,
                                                  hour, auctions=iid)
        full = sum(len(v) for v in lines.values())

        def rebuild(iid, days):
            return fetch_ah.addon_item_line(iid, {"name": "Thing %d" % iid, "sell": 1}, hist[iid], 1000 + iid, 1100, 10,
                                            hour, days, auctions=iid)
        total, short = fetch_ah.fit_addon_lines(dict(lines), rebuild, limit=full + 10)
        check(short == 0 and total == full, "size guard leaves a small file alone")
        work = dict(lines)
        total, short = fetch_ah.fit_addon_lines(work, rebuild, limit=full - len(lines[1]) // 2)
        check(short >= 1 and total <= full - len(lines[1]) // 2, f"size guard shortened {short} items to {total}")
        check(len(py_decode(line_body(work[1]))["D"][0]) == 90, "quietest item shortened to 90 days first")
        check(len(py_decode(line_body(work[5]))["D"][0]) == 400, "busiest item kept its full daily history")
        work = dict(lines)
        total, short = fetch_ah.fit_addon_lines(work, rebuild, limit=1)
        check(all(len(py_decode(line_body(v))["D"][0]) == 30 for v in work.values()), "everything down to 30 days when needed")
        for iid, v in work.items():
            d = py_decode(line_body(v))
            check(d["auctions"] == iid and d["nfields"] == FIELDS, "shortened lines keep the auctions field")
        # header: updated, fetched, salesHours
        fetch_ah.write_addon_data(tmp, work, hour + 1500, hour, "Alliance Auction House",
                                  fetched=hour + 1800, sales_hours=17)
        lua, db, _ = load_lua(os.path.join(tmp, "ForeverAH_Data.lua"))
        check(int(db.updated) == hour + 1500 and int(db.fetched) == hour + 1800 and int(db.salesHours) == 17,
              "header updated / fetched / salesHours")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def flip_tests():
    gap, ven = fetch_ah.find_gap_flip, fetch_ah.find_vendor_flip
    check(fetch_ah.after_cut(100) == 95 and fetch_ah.after_cut(19999) == 18999 and fetch_ah.after_cut(1) == 0,
          "5% cut in whole copper (100 * 0.95 is 94.999... in floating point)")

    # a 1c troll in front of a normal market is a real flip: buy it, relist under the next one
    f = gap([(1, 1), (50000, 1), (50500, 1), (51000, 1), (52000, 1)], sold_day=None, usual=50000)
    check(f and (f["buyMax"], f["units"], f["cost"], f["relist"], f["profit"]) == (1, 1, 1, 49999, 47498),
          f"1c troll in front: {f!r}")
    check(f["risk"] == ["thin", "slow"] and f["next"] == 50000 and f["roi"] == 47498000, f"troll flags {f!r}")
    f = gap([(1, 1), (50000, 1), (50500, 1), (51000, 1), (52000, 1)], sold_day=3.0, usual=50000, early=True)
    check(f and f["risk"] == ["thin", "slow"], f"sales measured for under a day: slow even within the day's sales ({f!r})")
    f = gap([(1, 1), (50000, 1), (50500, 1), (51000, 1), (52000, 1)], sold_day=3.0, usual=50000)
    check(f and f["risk"] == ["thin"], f"a full day measured, 3 a day: not slow ({f!r})")
    # a silly high listing far above never sets the relist price (it sorts to the end)
    check(gap([(1000, 1), (1010, 1), (1020, 1), (99999999, 1)], sold_day=5) is None,
          "a 9,999g listing above a tight market is no flip")
    check(gap([(1000, 1), (99999999, 1), (99999999, 1)], sold_day=5, hist_cap=1500) is None,
          "history caps the relist price: buying at 10s to relist at 15s is under 50s profit")
    f = gap([(1000, 1), (99999999, 1), (99999999, 1)], sold_day=5)
    check(f and f["relist"] == 99999998 and f["risk"] == ["thin", "spike"],
          f"no history: the next listing sets it, flagged thin and spike (2 listings far above) ({f!r})")
    f = gap([(1000, 1), (99999999, 1), (99999999, 1)], sold_day=5, usual=1000)
    check(f and "spike" in f["risk"], f"far above the usual price: spike ({f!r})")
    # troll listings above one cheap one: a new item (no history) priced 5g, 500g, 500g. Its market
    # value is the 500g listing itself, so only the check against what is bought can see it
    f = gap([(50000, 1), (5000000, 1), (5000000, 1)], sold_day=1.0, usual=5000000)
    check(f and f["relist"] == 4999999 and "spike" in f["risk"], f"troll pair above a new item: spike ({f!r})")
    f = gap([(50000, 1)] * 6 + [(5000000, 1)] * 2, sold_day=12.0, usual=50000)
    check(f and "spike" in f["risk"], f"real 5g market, two 500g trolls: spike ({f!r})")
    f = gap([(50000, 1), (5000000, 1), (5000000, 1)], sold_day=1.0, hist_cap=6000000, usual=4800000)
    check(f and "spike" not in f["risk"], f"with 7 days of history at 480g the same relist is normal ({f!r})")
    f = gap([(5000, 1), (50000, 1), (51000, 1), (52000, 1), (53000, 1)], sold_day=1.0, usual=50000)
    check(f and "spike" not in f["risk"], f"no history, but 3 listings near the next one back it up ({f!r})")

    # every listing bought pays on its own: never "buy up to X, relist at X"
    f = gap([(80, 1000), (100, 100), (150, 50), (151, 50)], commodity=True, sold_day=5000, hist_cap=100, usual=100)
    check(f and (f["buyMax"], f["units"], f["relist"]) == (80, 1000, 99),
          f"the 1s level is not bought when the cap makes its relist 1s: it would lose the cut ({f!r})")
    f = gap([(80, 1000), (100, 100), (150, 50), (151, 50)], commodity=True, sold_day=5000, usual=100)
    check(f and f["buyMax"] == 100 and fetch_ah.after_cut(f["relist"]) * 1000 >= 100 * 1150,
          f"without the cap the 1s level pays 15% at 1s49c: bought ({f!r})")
    rnd = random.Random(7)
    worst = None
    for _ in range(4000):
        book = sorted((rnd.choice((rnd.randint(1, 60), rnd.randint(50, 70), rnd.randint(1, 500)))
                       + rnd.random() * (rnd.random() < 0.2), rnd.randint(1, 300))
                      for _ in range(rnd.randint(3, 14)))
        f = gap(book, commodity=True, sold_day=rnd.choice((None, 10, 500, 5000)),
                hist_cap=rnd.choice((None, None, rnd.randint(30, 400))), usual=rnd.randint(0, 200))
        if f and fetch_ah.after_cut(f["relist"]) * 1000 < (f["buyMax"] - 1) * 1150:
            worst = (book, f)
            break
    check(worst is None, f"random books: the dearest listing bought always makes 15% at the relist ({worst!r})")

    # a deep cheap wall: more than a day of sales is never bought; within a day of sales it is
    wall = [(100, 20)] * 50 + [(300, 20)] * 5
    check(gap(wall, commodity=True, sold_day=200) is None, "a 1000-unit wall when 200 sell a day: no flip")
    f = gap(wall, commodity=True, sold_day=5000)
    check(f and (f["units"], f["cost"], f["relist"], f["profit"]) == (1000, 100000, 299, 1000 * 284 - 100000)
          and f["risk"] == ["thin"], f"wall within a day of sales: {f!r}")
    check(gap(wall, commodity=True, sold_day=None) is None, "sales unknown: at most 20% of the units listed")

    # 1-unit gear: buy the cheap one, relist under the next
    gear = [(5000, 1), (20000, 1), (21000, 1), (22000, 1)]
    f = gap(gear, sold_day=1.0, usual=20000)
    check(f and (f["units"], f["relist"], f["profit"], f["risk"]) == (1, 19999, 13999, ["thin"]), f"1-unit gear {f!r}")
    check(gap(gear, sold_day=0.5) is None, "half a sale a day: not even one unit")
    check(gap(gear, sold_day=1.0, hist_cap=8000) is None, "history cap 80s: profit 26s is under 50s")
    f = gap(gear, sold_day=1.0, hist_cap=12000)
    check(f and f["relist"] == 12000 and f["profit"] == 11400 - 5000, f"history cap 1.2g: {f!r}")
    check(gap([(5000, 1), (20000, 1)], sold_day=3) is None, "only 1 listing left above: no flip")
    check(gap([(5000, 1), (20000, 1), (20000, 1)], sold_day=3) is not None, "2 listings left above: flip")

    # thresholds: 20s for commodities, 50s for gear, 15% ROI
    two = [(1000, 1), (4000, 1), (4000, 1), (4000, 1)]
    check(gap(two, commodity=True, sold_day=5) is not None and gap(two, commodity=False, sold_day=5) is None,
          "27s profit: enough for a commodity, not for gear")
    check(gap([(10000, 10), (11000, 5), (11100, 5)], commodity=True, sold_day=100) is None,
          "45s profit on 10g is under 15% ROI")
    # the best level wins: buying the first two price levels beats buying only the first
    f = gap([(100, 5), (120, 5), (900, 5), (905, 5), (1000, 5)], commodity=True, sold_day=50)
    check(f and f["units"] == 10 and f["buyMax"] == 120 and f["relist"] == 899, f"best level {f!r}")
    check(f["profit"] == 10 * (899 * 95 // 100) - 1100, f"best level profit {f!r}")
    # fractions of a copper (a stack of 3 for 10c) keep their exact cost
    f = gap([(10 / 3, 3), (5000, 1), (5000, 1), (5000, 1)], commodity=True, sold_day=10)
    check(f and f["cost"] == 10 and f["buyMax"] == 4 and f["relist"] == 4999, f"fractional prices {f!r}")

    # vendor flips: listings under what a vendor pays
    f = ven([(8, 20), (9, 20), (14, 100)], 13)
    check(f and (f["kind"], f["units"], f["cost"], f["profit"], f["buyMax"], f["relist"], f["next"]) ==
          ("vendor", 40, 340, 180, 9, 0, 14) and f["risk"] == [], f"vendor flip {f!r}")
    check(ven([(12, 1)], 13) is None and ven([(8, 20)], 0) is None and ven([(20, 5)], 13) is None,
          "vendor flips: under 1s profit, no vendor price, or nothing under it")
    check(ven([(570, 285)], 577) is None, "285 leggings 7c under the vendor price each: no flip (under 10% each)")
    f = ven([(500, 2), (590, 50)], 600)
    check(f and (f["units"], f["cost"], f["profit"], f["buyMax"], f["next"]) == (2, 1000, 200, 500, 590),
          f"only listings that make 10% at the vendor count: {f!r}")

    # sold per day: measured hours after the series' first point, last 7 days
    hour = 1794934800
    H = 3600
    h = {"h": [[hour - 3 * H, 10, 10, 5, 4], [hour - 2 * H, 10, 10, 5, 2], [hour - H, 10, 10, 5, 0], [hour, 10, 10, 5, 6]]}
    meas = {hour - 3 * H: 3600, hour - 2 * H: 3600, hour - H: 1800, hour: 1800}
    spd = fetch_ah.sold_per_day(h, meas, hour)
    check(fetch_ah.sales_window(h, meas, hour) == (8, 7200), "sold and measured seconds after the first point")
    check(abs(spd - 8) < 1e-9, f"under a day measured: what was seen selling, never scaled up to a day ({spd})")
    h1 = {"h": [[hour - H, 10, 10, 5, 0], [hour, 10, 10, 5, 1]]}
    check(fetch_ah.sold_per_day(h1, {hour: 3600}, hour) == 1.0,
          "one vanished auction in one measured hour is 1 a day, not 24")
    h2 = {"h": [[hour - k * H, 10, 10, 5, 1 if k % 5 == 0 else 0] for k in range(60, -1, -1)]}
    m2 = {hour - k * H: 3600 for k in range(48)}
    w2 = fetch_ah.sales_window(h2, m2, hour)
    check(w2 == (10, 48 * 3600) and abs(fetch_ah.sold_per_day(h2, m2, hour) - 5.0) < 1e-9,
          f"two days measured: 10 sold is 5 a day ({w2})")
    check(fetch_ah.sold_per_day({"h": [[hour, 1, 1, 1, 0]]}, meas, hour) is None, "a series' first point: unknown")
    check(fetch_ah.sold_per_day(h, {}, hour) is None, "nothing measured: unknown")
    check(fetch_ah.sold_per_day(h, {hour: 1800}, hour) is None, "less than an hour measured: unknown")
    pts = {"h": [[hour - k * H, 100, 1000 + k, 5, 0] for k in range(170, -1, -1)]}
    cap, usual = fetch_ah.history_prices(pts, hour)
    vals = sorted(1000 + k for k in range(168))
    check(cap == int(fetch_ah.percentile(vals, 0.9)) and usual == fetch_ah.percentile(vals, 0.5),
          f"7-day cap and usual ({cap}, {usual})")
    check(fetch_ah.history_prices({"h": pts["h"][-3:]}, hour) == (None, None), "fewer than 4 points: no cap")
    check(fetch_ah.percentile([1, 2, 3, 4], 0.5) == 2.5 and fetch_ah.percentile([7], 0.9) == 7, "percentile")
    # ranking and the addon's line format
    fl = [{"id": 2, "kind": "gap", "profit": 500, "roi": 200}, {"id": 1, "kind": "vendor", "profit": 500, "roi": 300},
          {"id": 3, "kind": "gap", "profit": 900, "roi": 160}]
    check([x["id"] for x in fetch_ah.rank_flips(fl)] == [3, 1, 2], "flips: most profit, then best ROI")
    line = fetch_ah.flip_line({"id": 9860003, "kind": "gap", "buyMax": 7000, "units": 1, "cost": 7000,
                               "relist": 23583, "profit": 15403, "roi": 2200, "risk": ["thin", "suffix"],
                               "usual": 23900})
    check(line == "9860003|gap|7000|1|7000|23583|15403|2200|thin,suffix|23900", f"flip line {line!r}")
    check(parse_flip(line)["risk"] == ["thin", "suffix"] and parse_flip(line)["usual"] == 23900, "flip line parses")
    line = fetch_ah.flip_line({"id": 5, "kind": "vendor", "buyMax": 9, "units": 40, "cost": 340, "relist": 0,
                               "profit": 180, "roi": 529, "risk": []})
    check(line == "5|vendor|9|40|340|0|180|529||0", f"no usual price: 0 in the 10th field, an empty risk field ({line!r})")
    line = fetch_ah.flip_line({"id": 7, "kind": "deal", "buyMax": 12000, "units": 1, "cost": 12000, "relist": 39999,
                               "profit": 25999, "roi": 2166, "risk": ["slow", "unsure"], "usual": 41000.5})
    check(line == "7|deal|12000|1|12000|39999|25999|2166|slow,unsure|41000", f"deal line, usual in whole copper {line!r}")
    old = "9860003|gap|7000|1|7000|23583|15403|2200|thin,suffix"
    check(line.split("|")[:9] == "7|deal|12000|1|12000|39999|25999|2166|slow,unsure".split("|")
          and "usual" not in parse_flip(old) and parse_flip(old)["nfields"] == 9,
          "an older addon copy reads the same 9 fields first; an older file's 9-field line still parses")
    for name in dir(fetch_ah):
        if name.startswith(("FLIP_", "FLIPS_", "DEAL_")) and name != "DEAL_QUALITIES":
            check(isinstance(getattr(fetch_ah, name), (int, float, str)), f"{name} is a named constant")


def deal_tests():
    """Deals: a green, blue or epic whose cheapest listings sit well under its usual price."""
    deal = fetch_ah.find_deal_flip
    check((fetch_ah.DEAL_MAX_SHARE, fetch_ah.DEAL_MIN_PROFIT, fetch_ah.DEAL_UNSURE_LISTED) == (0.70, 2500, 2)
          and set(fetch_ah.DEAL_QUALITIES) == DEAL_QUALITIES and fetch_ah.FLIP_HIST_MIN_POINTS == 4
          and fetch_ah.FLIP_MIN_LEFT == 2 and fetch_ah.FLIP_MIN_ROI_PERMILLE == 150, "deal limits (the shared contract)")
    # a clear deal: a blue that usually goes for 4.1g listed at 1.2g, three normal ones above it
    book = [(12000, 1), (40000, 1), (41000, 1), (42000, 1)]
    f = deal(book, 41000, sold_day=2.0, hist_cap=45000, listed_median=3)
    check(f == {"kind": "deal", "buyMax": 12000, "units": 1, "cost": 12000, "relist": 39999, "profit": 25999,
                "roi": 2166, "risk": [], "next": 40000, "usual": 41000}, f"a clear deal {f!r}")
    check(deal(book, None, sold_day=2.0, listed_median=3) is None and deal(book, 0, sold_day=2.0) is None
          and deal([], 41000) is None, "no usual price (under 4 hours of history) or nothing listed: no deal")
    # the 70% line, in whole copper (0.7 x 41000 is 28699.999... in floating point)
    f = deal([(28700, 1), (41000, 1), (42000, 1)], 41000, sold_day=1.0, listed_median=3)
    check(f and f["buyMax"] == 28700 and f["relist"] == 40999, f"exactly 70% of the usual price is a deal ({f!r})")
    check(deal([(28701, 1), (41000, 1), (42000, 1)], 41000, sold_day=1.0, listed_median=3) is None,
          "1c above 70% of the usual price: no deal")
    check(deal([(70001, 1), (100000, 1), (101000, 1)], 100000, sold_day=1.0, listed_median=3) is None
          and deal([(70000, 1), (100000, 1), (101000, 1)], 100000, sold_day=1.0, listed_median=3) is not None,
          "just above 70%: none, at 70%: a deal")
    # at least 25s profit: 9,499c after the cut on a 9,999c relist
    check(deal([(6999, 1), (10000, 1), (10100, 1)], 10000, sold_day=1.0, listed_median=3)["profit"] == 2500,
          "25s profit is enough")
    check(deal([(7000, 1), (10000, 1), (10100, 1)], 10000, sold_day=1.0, listed_median=3) is None,
          "24s 99c profit: no deal")
    check(deal([(1000, 1), (3000, 1), (3100, 1)], 3000, sold_day=1.0, listed_median=3) is None,
          "a cheap green that makes 18s: no deal")
    # ROI 15%, and the dearest listing bought makes 15% on its own: 4g bought, relisted at 4.8422g
    f = deal([(40000, 1), (48423, 1), (49000, 1)], 60000, sold_day=1.0, listed_median=3)
    check(f and (f["relist"], f["profit"], f["roi"]) == (48422, 6000, 150), f"exactly 15% is a deal ({f!r})")
    check(deal([(40000, 1), (48422, 1), (49000, 1)], 60000, sold_day=1.0, listed_median=3) is None,
          "14.99% (the next listing 1c lower) is no deal")
    # units: at most a day of sales (at least 1), cheapest first
    book = [(10000, 1), (30000, 1), (60000, 1), (100000, 1), (101000, 1)]
    f = deal(book, 100000, sold_day=0.4, listed_median=3)
    check(f and (f["units"], f["relist"], f["next"], f["risk"]) == (1, 29999, 30000, ["slow"]),
          f"under 1 sold a day: 1 unit, relisted under the next listing, slow ({f!r})")
    f = deal(book, 100000, sold_day=2.5, listed_median=3)
    check(f and (f["units"], f["cost"], f["relist"], f["profit"], f["risk"]) == (2, 40000, 59999, 73998, []),
          f"2.5 sold a day: the 2 cheapest ({f!r})")
    f = deal(book, 100000, sold_day=5.0, listed_median=3)
    check(f and (f["units"], f["cost"], f["buyMax"], f["relist"], f["next"]) == (3, 100000, 60000, 99999, 100000),
          f"5 sold a day: every listing under 70% (3), not the ones above ({f!r})")
    f = deal(book, 100000, sold_day=None, listed_median=3)
    check(f and f["units"] == 1 and f["risk"] == ["slow"], f"sales unknown: 1 unit, slow ({f!r})")
    f = deal(book, 100000, sold_day=3.0, listed_median=3, early=True)
    check(f and f["units"] == 3 and f["risk"] == ["slow"], f"measured for under a day: slow ({f!r})")
    f = deal(book, 100000, sold_day=2.0, listed_median=3)
    check(f and f["units"] == 2 and "slow" not in f["risk"], f"2 a day, 2 bought: not slow ({f!r})")
    check(deal([(10000, 3), (50000, 1), (51000, 1)], 50000, sold_day=1.0, listed_median=3) is None,
          "a stack of 3 when 1 sells a day: a listing is never split, so no deal")
    check(deal([(10000, 1), (10000, 1), (50000, 1)], 50000, sold_day=None, listed_median=3) is None,
          "two equal cheap listings, 1 can be bought: relisting under the other one loses, so no deal")
    # the relist price: under the next listing, never above the usual price or the 7-day 90th percentile
    f = deal([(10000, 1), (200000, 1), (210000, 1)], 50000, sold_day=1.0, listed_median=3)
    check(f and f["relist"] == 50000 and f["next"] == 200000, f"a far higher next listing: relist at the usual price ({f!r})")
    f = deal([(10000, 1), (200000, 1), (210000, 1)], 50000.5, sold_day=1.0, listed_median=3)
    check(f and f["relist"] == 50000 and f["usual"] == 50000, f"usual price in whole copper ({f!r})")
    f = deal([(10000, 1), (200000, 1), (210000, 1)], 50000, sold_day=1.0, hist_cap=45000, listed_median=3)
    check(f and f["relist"] == 45000 and f["usual"] == 50000, f"capped by the 7-day 90th percentile ({f!r})")
    f = deal([(10000, 1), (30000, 1), (210000, 1)], 50000, sold_day=1.0, hist_cap=45000, listed_median=3)
    check(f and f["relist"] == 29999, f"capped by the next listing ({f!r})")
    f = deal([(10000, 1)], 50000, sold_day=1.0, listed_median=3)
    check(f and (f["relist"], f["next"], f["risk"]) == (50000, 0, ["thin"]),
          f"nothing listed above: relist at the usual price, thin ({f!r})")
    # buying all of them would not pay (the next listing sits right above the dearer one): the cheaper one alone
    f = deal([(20000, 1), (68000, 1), (72000, 1), (100000, 1), (101000, 1)], 100000, sold_day=3.0, listed_median=3)
    check(f and (f["units"], f["buyMax"], f["relist"], f["profit"]) == (1, 20000, 67999, 44599),
          f"the cheapest few that pay ({f!r})")
    # on purpose (the decided rule, not only what buying all of them gives): both under 7g would be
    # relisted at 7.1999g, which pays the 6.8g one 6.8399g after the cut, under 15%. Without the
    # fallback the 2g listing of a 10g item would get no buy at all
    bk = [(20000, 1), (68000, 1), (72000, 1)]
    f = deal(bk, 100000, sold_day=3.0, hist_cap=100000, listed_median=3)
    check(fetch_ah.after_cut(71999) * 1000 < 68000 * 1150 and model_deal(bk, 100000, 3.0, 100000)
          == (1, 20000, 67999, 44599, "fewer") and f and (f["units"], f["buyMax"], f["relist"], f["profit"],
                                                         f["next"], f["risk"]) == (1, 20000, 67999, 44599, 68000, []),
          f"buying both fails the 15% rule: the 2g one alone, relisted under the 6.8g one ({f!r})")
    # whenever buying all of them passes, all of them are bought
    bk = [(20000, 1), (60000, 1), (90000, 1), (95000, 1)]
    f = deal(bk, 100000, sold_day=3.0, listed_median=3)
    check(model_deal(bk, 100000, 3.0) == (2, 60000, 89999, 2 * 85499 - 80000, "all")
          and f and (f["units"], f["buyMax"], f["relist"]) == (2, 60000, 89999), f"all of them when that passes ({f!r})")
    # all 3 under 7g fail (the 6g one relisted at 6.5999g), so fewer: the 2 cheapest make the most (1 alone less)
    bk = [(10000, 1), (20000, 1), (60000, 1), (66000, 1), (90000, 1)]
    f = deal(bk, 100000, sold_day=3.0, listed_median=3)
    check(model_deal(bk, 100000, 3.0) == (2, 20000, 59999, 2 * 56999 - 30000, "fewer")
          and f and (f["units"], f["buyMax"], f["relist"], f["profit"]) == (2, 20000, 59999, 83998),
          f"the fewer with the most profit ({f!r})")
    # risk flags
    f = deal([(10000, 1), (60000, 1)], 50000, sold_day=1.0, listed_median=3)
    check(f and f["risk"] == ["thin"], f"one listing left above: thin ({f!r})")
    f = deal([(10000, 1), (60000, 1), (61000, 1)], 50000, sold_day=1.0, listed_median=3)
    check(f and f["risk"] == [], f"two listings left above: not thin ({f!r})")
    f = deal([(10000, 1), (60000, 1), (61000, 1)], 50000, sold_day=1.0, listed_median=1.5)
    check(f and f["risk"] == ["unsure"], f"usually 1 listed: the usual price may be one seller's ask ({f!r})")
    f = deal([(10000, 1), (60000, 1), (61000, 1)], 50000, sold_day=1.0, listed_median=2)
    check(f and f["risk"] == [], f"usually 2 listed: sure ({f!r})")
    f = deal([(10000, 1), (60000, 1)], 50000, sold_day=None, listed_median=None)
    check(f and f["risk"] == ["thin", "slow", "unsure"], f"every flag at once ({f!r})")
    rnd = random.Random(11)
    for _ in range(3000):
        usual = rnd.randint(3000, 400000)
        bk = sorted((rnd.choice((rnd.randint(1, usual), rnd.randint(usual // 2, usual * 2))), rnd.choice((1, 1, 1, 2)))
                    for _ in range(rnd.randint(1, 8)))
        spd = rnd.choice((None, 0.0, 0.5, 1.0, 2.0, 4.5))
        cap = rnd.choice((None, rnd.randint(usual // 2, usual * 2)))
        f = deal(bk, usual, sold_day=spd, hist_cap=cap, listed_median=rnd.choice((None, 1, 3)))
        if not f:
            continue
        lim = 1 if spd is None else max(1, int(spd))
        k = next(i for i in range(len(bk) + 1) if sum(q for _, q in bk[:i]) == f["units"])
        ok = (all(p * 1000 <= 700 * usual for p, _ in bk[:k]) and f["units"] <= lim and f["relist"] <= usual
              and (cap is None or f["relist"] <= cap) and (k == len(bk) or f["relist"] < bk[k][0])
              and f["profit"] >= 2500 and f["profit"] * 1000 >= 150 * f["cost"]
              and fetch_ah.after_cut(f["relist"]) * 1000 >= bk[k - 1][0] * 1150)
        check(ok, f"random books: a deal breaks the rules {bk!r} usual {usual} spd {spd} cap {cap}: {f!r}")
    # the whole rule, against model_deal on random books: fractions of a copper (stacks), x.5 usual
    # prices, listings on and just above the 70% line, every kind of sales (unknown, none, under 1 a
    # day, fractions) and 7-day caps. Both ways must come up: all of them, and fewer
    rnd = random.Random(29)
    ways = Counter()
    for _ in range(20000):
        base = rnd.randint(3000, 400000)
        usual = base + rnd.choice((0, 0, 0.5))
        line = base * 7 // 10
        bk = []
        for _ in range(rnd.randint(1, 8)):
            q = rnd.choice((1, 1, 1, 2, 3))
            p = rnd.choice((rnd.randint(1, base), rnd.randint(base // 2, base), rnd.randint(line - 2, line + 2),
                            rnd.randint(line, line * 6 // 5), rnd.randint(base, base * 2)))
            if q > 1 and rnd.random() < 0.3:
                p += rnd.choice((1, 2)) / q              # a stack whose unit price is a fraction of a copper
            bk.append((p, q))
        bk.sort()
        spd = rnd.choice((None, 0.0, 0.5, 1.0, 1.999999, 2.0, 2.5, 3.0, 4.5, 8.0))
        cap = rnd.choice((None, None, 0, rnd.randint(base // 2, base * 2)))
        f = deal(bk, usual, sold_day=spd, hist_cap=cap, listed_median=3)
        want = model_deal(bk, usual, spd, cap)
        got = f and (f["units"], f["buyMax"], f["relist"], f["profit"])
        check(got == (want[:4] if want else None),
              f"the deal rule: {bk!r} usual {usual} spd {spd} cap {cap}: got {f!r}, the rule says {want!r}")
        ways[want[4] if want else "none"] += 1
    check(ways["all"] >= 1000 and ways["fewer"] >= 300, f"random books: both ways tested {dict(ways)}")
    # the usual price, and how many were listed, over the 7-day hourly points with a market value
    hour = 1794934800
    H = 3600
    h = {"h": [[hour - 200 * H, 1, 99999, 9, 0]] + [[hour - k * H, 10, 41000 + k, 1 + k % 2, 0] for k in range(5, 0, -1)]
         + [[hour - 3 * H + 1800, 0, 0, 0, 0], [hour - 2 * H + 1800, 0, 0, 0, 0], [hour, 12000, 40000, 4, 0]]}
    check(fetch_ah.history_listed(h, hour) == 2 and fetch_ah.history_prices(h, hour)[1] == 41002.5,
          "listed and usual: the same 7-day points with a market value (older and empty points left out: "
          "with the 2 empty hours the median listed would be 1.5)")
    check(fetch_ah.history_listed({"h": h["h"][-4:]}, hour) is None, "under 4 points with a price: unknown")


def pick_tests():
    """Which flips the addon file carries: the best 60, plus deals and cheap flips not in yet."""
    check((fetch_ah.FLIPS_ADDON_MAX, fetch_ah.FLIPS_ADDON_DEALS, fetch_ah.FLIPS_ADDON_CHEAP, fetch_ah.FLIP_CHEAP_COST)
          == (ADDON_TOP, ADDON_DEALS, ADDON_CHEAP, CHEAP_COST), "addon flip limits (the shared contract)")
    fl = []

    def add(n, kind, cost, line=True):
        for _ in range(n):
            fl.append({"id": (len(fl) + 1) if line else 99999, "kind": kind, "cost": cost,
                       "profit": 10 ** 7 - len(fl), "roi": 100})
    add(1, "gap", 1000, line=False)     # no item line: never carried, takes no place
    add(50, "gap", 200000)              # ids 2..51 \
    add(5, "deal", 300000)              # 52..56     > the best 60 (their deals and cheap flips use no extra room)
    add(5, "gap", 10000)                # 57..61    /
    add(30, "gap", 300000)              # 62..91: dear, not deals: left out
    add(3, "deal", 20000)               # 92..94: deals (cheap too): the first 3 extra deals
    add(42, "deal", 100000)             # 95..136: 37 more extra deals (95..131), then no room: left out
    add(45, "gap", 50000)               # 137..181: exactly 5g is cheap: the 40 cheap flips (137..176)
    add(5, "gap", 50001)                # 182..186: 1c over 5g: left out
    random.Random(3).shuffle(fl)
    fl = fetch_ah.rank_flips(fl)
    lines = set(range(1, 200))
    got = fetch_ah.pick_addon_flips(fl, lines)
    want = list(range(2, 62)) + list(range(92, 132)) + list(range(137, 177))
    check([f["id"] for f in got] == want, f"picked {[f['id'] for f in got]}")
    check(len(got) == 140 and len({id(f) for f in got}) == 140, "60 + 40 + 40, none twice")
    check(got == [f for f in fl if f in got], "kept in rank order")
    check(got == expected_addon_pick([f for f in fl if f["id"] in lines]), "the same as the test's own copy of the rule")
    few = fl[:20]
    check(fetch_ah.pick_addon_flips(few, lines) == [f for f in few if f["id"] in lines], "fewer than 60: all of them")
    check(fetch_ah.pick_addon_flips(fl, {}) == [], "no item lines: none")
    # flips.json: every row has its usual price, 0 when the flip has none
    tmp = tempfile.mkdtemp(prefix="fah_flipsjson_")
    try:
        base = {"base": 7, "variant": "", "listed": 4, "auctions": 4, "market": 40000, "sold24": 0, "next": 40000,
                "buyMax": 12000, "units": 1, "cost": 12000, "relist": 39999, "profit": 25999, "roi": 2166}
        rows = [dict(base, id=7, kind="deal", risk=["slow"], usual=41000),
                dict(base, id=8, kind="gap", risk=[], usual=40000.5), dict(base, id=9, kind="vendor", risk=[])]
        n = fetch_ah.write_flips(tmp, rows, 1794934800, lambda i: ({"name": "N%d" % i, "quality": "RARE"}, "N%d" % i))
        out = load_json(os.path.join(tmp, "flips.json"))
        check(n == 3 and [(r["kind"], r.get("usual")) for r in out["flips"]] == [("deal", 41000), ("gap", 40000), ("vendor", 0)]
              and all(isinstance(r["usual"], int) for r in out["flips"]), f"flips.json usual {out['flips']!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def variant_tests():
    vk = fetch_ah.variant_key
    check(vk({"id": 1, "bonus_lists": [12722]}) == "12722" and vk({"id": 1}) == "" and vk(None) == "", "keys")
    check(vk({"bonus_lists": [12722, 1500, 99]}) == "99:1500:12722", "bonus ids sorted as numbers, not as text")
    check(vk({"bonus_lists": [12722], "modifiers": [{"type": 28, "value": 5240}], "context": 1}) == "12722",
          "modifiers and context are not part of the key")
    reg = {}
    vid = fetch_ah.variant_id
    check(vid(reg, 9860, "12722") == 9860001 and vid(reg, 9860, "12718") == 9860002 and vid(reg, 9860, "12722") == 9860001
          and vid(reg, 9860, "") is None and vid(reg, 4564, "12722") == 4564001, f"indexes in order of appearance {reg}")
    check(vid(reg, 9860, "1", add=False) is None and reg == {"9860": {"12722": 1, "12718": 2}, "4564": {"12722": 1}},
          "add=False never registers")
    reg["5"] = {"k%d" % i: i for i in range(1, 1000)}
    check(vid(reg, 5, "new") is None and "new" not in reg["5"] and vid(reg, 5, "k7") == 5007, "at most 999 per item")
    reg = {"9860": {"12722": 1, "12718": 3}}           # index 2 was handed out before: never reused
    check(vid(reg, 9860, "12726") == 9860004, "a new version gets the next index after the highest")
    check(fetch_ah.registry_vids({"9860": {"12722": 1}}) == {9860001: (9860, "12722", 1)}, "registry_vids")
    # aggregate: versions next to the item, snapshot entries carry the vid
    reg = {}
    per, snap, vper = fetch_ah.aggregate([
        {"id": 1, "item": {"id": 800, "bonus_lists": [12722], "modifiers": [{"type": 28, "value": 1}]}, "quantity": 1,
         "buyout": 50000, "time_left": "LONG"},
        {"id": 2, "item": {"id": 800, "bonus_lists": [12722], "modifiers": [{"type": 28, "value": 2}]}, "quantity": 1,
         "buyout": 51000, "time_left": "LONG"},
        {"id": 3, "item": {"id": 800, "bonus_lists": [12718]}, "quantity": 1, "buyout": 9000, "time_left": "LONG"},
        {"id": 4, "item": {"id": 5}, "quantity": 20, "unit_price": 7, "time_left": "LONG"},
    ], reg)
    check(sorted(per[800]) == [(9000.0, 1), (50000.0, 1), (51000.0, 1)] and per[5] == [(7.0, 20)], "per item lumps all")
    check(vper == {800001: [(50000.0, 1), (51000.0, 1)], 800002: [(9000.0, 1)]}, f"per version {vper!r}")
    check(snap["1"] == [800, 1, 50000, "LONG", 800001] and snap["4"] == [5, 20, 7, "LONG"], f"snap entries {snap!r}")
    # sold per version; an older snapshot without vids still counts for the item
    prev = {"1": [800, 1, 50000, "LONG", 800001], "3": [800, 1, 9000, "LONG", 800002], "9": [800, 1, 1, "LONG"],
            "4": [5, 20, 7, "LONG"]}
    now = {"4": [5, 15, 7, "LONG"]}
    check(fetch_ah.estimate_sold(prev, now) == {800: 3, 5: 5}, "sold per item")
    check(fetch_ah.estimate_sold(prev, now, by_variant=True) == {800001: 1, 800002: 1}, "sold per version")
    # names
    sfx = {12722: "of the Bear", 12718: "of the Eagle"}
    vn = fetch_ah.variant_name
    check(vn({"name": "Ridge Cleaver"}, "12722", 1, sfx) == "Ridge Cleaver of the Bear", "suffix name")
    check(vn({"name": "Ridge Cleaver"}, "99001", 3, sfx) == "Ridge Cleaver (version 3)", "unknown bonus id")
    check(vn({"name": "Ridge Cleaver"}, "99001:12718", 4, sfx) == "Ridge Cleaver of the Eagle", "one of two known")
    check(vn({"name": "", "unknown": True}, "12722", 1, sfx) == "" and vn(None, "12722", 1, sfx) == "",
          "an unnamed item's versions are unnamed too")
    real = load_suffixes()
    check(real.get("12722") == "of the Bear" and real.get("12718") == "of the Eagle" and real.get("12751") == "of the Bandit"
          and all(v.startswith("of ") for v in real.values()), "suffix_names.json")
    raw = open(os.path.join(TOOLS, "suffix_names.json"), "rb").read()
    check(all(b < 128 for b in raw) and b"\r" not in raw, "suffix_names.json is ASCII with LF line ends")
    tmp = tempfile.mkdtemp(prefix="fah_sfx_")
    try:
        names = fetch_ah.load_suffix_names(tmp)
        check(names.get(12722) == "of the Bear" and os.path.exists(os.path.join(tmp, "suffix_names.json")),
              "the table is found next to the script and copied to the data folder")
        old_file = fetch_ah.__file__
        built = fetch_ah.builtin_suffix_names()
        check(built == {int(k): v for k, v in real.items()},
              "the table built into fetch_ah.py (_SUFFIX_CODE) is the same as suffix_names.json: "
              "rebuild it when the file changes")
        try:   # a copy of the script installed on its own (the PC collector) uses the built-in table
            fetch_ah.__file__ = os.path.join(tmp, "elsewhere", "fetch_ah.py")
            check(fetch_ah.load_suffix_names(os.path.join(tmp, "empty")) == built and
                  not os.path.exists(os.path.join(tmp, "empty", "suffix_names.json")),
                  "no table next to the script: the built-in one")
            with open(os.path.join(tmp, "suffix_names.json"), "w", encoding="utf-8") as fh:
                json.dump({"12722": "of the Bear", "99999": "of Testing"}, fh)
            got = fetch_ah.load_suffix_names(tmp)
            check(got.get(99999) == "of Testing" and got.get(12751) == "of the Bandit",
                  "a newer data folder copy adds to the built-in table")
        finally:
            fetch_ah.__file__ = old_file
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def meta_tests():
    item = {"name": "Wild Leather Helmet", "quality": {"type": "UNCOMMON"}, "sell_price": 6230, "level": 45,
            "required_level": 40, "item_class": {"name": "Armor", "id": 4}, "item_subclass": {"name": "Leather", "id": 2},
            "inventory_type": {"type": "HEAD", "name": "Head"}}
    m = fetch_ah._meta(8214, item, "i")
    check((m["slot"], m["slotType"], m["ilvl"], m["lvl"], m["clsId"], m["subId"], m["mv"]) ==
          ("Head", "HEAD", 45, 40, 4, 2, fetch_ah.META_VERSION), f"item details {m!r}")
    m = fetch_ah._meta(2589, {"name": "Linen Cloth", "item_class": {"name": "Consumable", "id": 0},
                              "inventory_type": {"type": "NON_EQUIP", "name": "Non-equippable"}}, "")
    check(m["slot"] == "" and m["clsId"] == 0 and m["subId"] is None, f"non-equippable, class id 0 kept {m!r}")
    now = 1794936300
    items = {"1": {"name": "A", "sell": 1}, "2": {"name": "B", "sell": 1}, "3": {"name": "C", "sell": 1},
             "4": {"name": "D", "sell": 1, "mv": fetch_ah.META_VERSION, "slot": "Head"},
             "5": dict(fetch_ah.unknown_entry(now)), "6": {"name": "F", "sell": 1, "cls": "Armor"}}
    calls = []
    lock = threading.Lock()

    def fake(iid):
        with lock:
            calls.append(iid)
        if iid == 2:
            return iid, None                                   # a failed request: tried again next run
        if iid == 6:
            return iid, {}                                     # no namespace knows it: marked done
        return iid, {"cls": "Armor", "sub": "Cloth", "lvl": 20, "ilvl": 25, "slot": "Chest", "slotType": "CHEST",
                     "clsId": 4, "subId": 1, "mv": fetch_ah.META_VERSION}
    done, left = fetch_ah.backfill_meta(items, {3: 50, 1: 7, 2: 9}, fetch=fake, limit=3, threads=2)
    check(sorted(calls) == [1, 2, 3] and (done, left) == (2, 2), f"most listed first, at most 3 ({calls}, {done}, {left})")
    check(items["3"]["slot"] == "Chest" and items["3"]["ilvl"] == 25 and items["3"]["name"] == "C"
          and items["3"]["mv"] == fetch_ah.META_VERSION, f"filled in {items['3']!r}")
    check("mv" not in items["2"], "a failed request is not marked done")
    calls.clear()
    done, left = fetch_ah.backfill_meta(items, {}, fetch=fake, limit=10, threads=2)
    check(sorted(calls) == [2, 6] and items["6"]["mv"] == fetch_ah.META_VERSION and items["6"]["slot"] == ""
          and items["6"]["cls"] == "Armor" and left == 1, f"unknown to Blizzard: defaults, class kept ({items['6']!r})")
    check(5 not in calls and "mv" not in items["5"], "unnamed items are left to the name retries")
    check(fetch_ah.META_BACKFILL_MAX == 1500, "backfill limit")


def size_vid_tests():
    """the size guard shortens versions before items, and never drops a line"""
    hour = 1794934800
    day = hour - hour % 86400
    hist, lines = {}, {}
    for iid in (1, 2, 1001, 1002):
        h = {"h": [[hour - k * 3600, 1000 + k * 7 + iid, 1100 + k * 5, 10, iid % 7] for k in range(167, -1, -1)],
             "d": [[day - k * 86400, 900 + k * 3 + iid, 950 + k * 2, 10, 3] for k in range(399, -1, -1)]}
        hist[iid] = h
        lines[iid] = fetch_ah.addon_item_line(iid, {"name": "T%d" % iid, "sell": 1}, h, 1000, 1100, 10, hour, auctions=1)

    def rebuild(iid, days):
        return fetch_ah.addon_item_line(iid, {"name": "T%d" % iid, "sell": 1}, hist[iid], 1000, 1100, 10, hour, days,
                                        auctions=1)
    full = sum(len(v) for v in lines.values())
    work = dict(lines)
    total, short = fetch_ah.fit_addon_lines(work, rebuild, limit=full - 100, vids={1001, 1002})
    dl = {k: len(py_decode(line_body(v))["D"][0]) for k, v in work.items()}
    check(dl[1] == 400 and dl[2] == 400 and min(dl[1001], dl[1002]) < 400, f"versions shortened first {dl}")
    work = dict(lines)
    total, short = fetch_ah.fit_addon_lines(work, rebuild, limit=1, vids={1001, 1002})
    dl = {k: len(py_decode(line_body(v))["D"][0]) for k, v in work.items()}
    check(dl == {1: 30, 2: 30, 1001: 1, 1002: 1} and set(work) == set(lines), f"last resort: versions 1 day, items 30 {dl}")
    work = dict(lines)
    total, short = fetch_ah.fit_addon_lines(work, rebuild, limit=full + 150, extra=100)
    check(total == full + 100 and short == 0 and work == lines, "fits with the extra bytes: left alone")
    total, short = fetch_ah.fit_addon_lines(work, rebuild, limit=full + 50, extra=100)
    check(short >= 1 and total <= full + 50, "extra bytes count towards the limit")
    # the tables go before items; leaving them out (older callers) writes no tables
    tmp = tempfile.mkdtemp(prefix="fah_tbl_")
    try:
        fetch_ah.write_addon_data(tmp, dict(lines), hour + 60, hour, "Alliance Auction House")
        text = open(os.path.join(tmp, "ForeverAH_Data.lua"), encoding="utf-8").read()
        check("variants" not in text and "flips" not in text, "no tables unless given")
        fl = [{"id": 1001, "kind": "gap", "buyMax": 5, "units": 1, "cost": 5, "relist": 99, "profit": 89, "roi": 17800,
               "risk": ["suffix"]}]
        fetch_ah.write_addon_data(tmp, dict(lines), hour + 60, hour, "Alliance Auction House",
                                  variants={1: [("12722", 1001), ("12718", 1002)]}, flips=fl)
        lua, db, raw = load_lua(os.path.join(tmp, "ForeverAH_Data.lua"))
        text = raw.decode("utf-8")
        check(text.index("variants = {") < text.index("flips = {") < text.index("items = {"), "tables before items")
        check(db.variants[1] == "12722=1001,12718=1002" and db.flips[1] == "1001|gap|5|1|5|99|89|17800|suffix|0"
              and db.flips[2] is None and db["items"][1001] is not None, "tables read back in Lua")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def unit_tests():
    market_tests()
    sold_tests()
    sold_floor_tests()
    name_and_time_tests()
    size_tests()
    flip_tests()
    deal_tests()
    pick_tests()
    variant_tests()
    meta_tests()
    size_vid_tests()


# -- the real collector on hand-made snapshots --------------------------------
LINEN, UNNAMED, AXE, ARROW, THIN, ORE, NEVER = 100, 200, 300, 400, 500, 600, 700


class FakeBlizzard:
    """Stands in for fetch_ah.api / get_token / time while main() runs."""

    def __init__(self):
        self.now = 0
        self.modified = ""
        self.auctions = {}        # id -> auction (auctions endpoint)
        self.commodities = {}     # id -> auction (commodities endpoint)
        self.com_fail = False
        self.names = {LINEN: "Linen Cloth", AXE: "Double Axe", ARROW: "Rough Arrow", THIN: "Thin Thing",
                      ORE: "Copper Ore"}
        self.lookups = Counter()
        self.lock = threading.Lock()
        self.sell = {}            # item id -> vendor price (default: 1400 for the axe, else 5)
        self.details = {}         # item id -> extra item API fields (level, inventory_type, ...)

    def time(self):
        return self.now

    def sleep(self, s):
        pass

    def api(self, path, ns, tries=4):
        if path.endswith("/auctions/"):
            return {"auctions": [{"id": 110, "name": "Alliance Auction House"},
                                 {"id": 113, "name": "Horde Auction House"}]}, {}
        if path.endswith("/commodities"):
            if self.com_fail:
                raise OSError("simulated download failure")
            return {"auctions": list(self.commodities.values())}, {}
        if "/auctions/" in path:
            return {"auctions": list(self.auctions.values())}, {"last-modified": self.modified}
        if path.startswith("/data/wow/media/item/"):
            return {"assets": [{"key": "icon", "value": "icon.jpg"}]}, {}
        if path.startswith("/data/wow/item/"):
            iid = int(path.rsplit("/", 1)[1])
            with self.lock:
                self.lookups[iid] += 1
            name = self.names.get(iid)
            if name is None:
                return None, {}            # 404 in this namespace
            out = {"name": name, "sell_price": self.sell.get(iid, 1400 if iid == AXE else 5),
                   "quality": {"type": "COMMON"}}
            out.update(self.details.get(iid, {}))
            return out, {}
        return None, {}


def start_book(fb):
    def add(book, aid, iid, qty, tl, buyout=None, unit=None):
        a = {"id": aid, "item": {"id": iid}, "quantity": qty, "time_left": tl}
        if buyout is not None:
            a["buyout"] = buyout
        if unit is not None:
            a["unit_price"] = unit
        book[aid] = a
    for i in range(1, 21):            # 20 linen auctions of 5, 1-10 LONG, 11-20 SHORT
        add(fb.auctions, i, LINEN, 5, "LONG" if i <= 10 else "SHORT", buyout=(45 + i) * 5)
    add(fb.auctions, 31, UNNAMED, 1, "VERY_LONG", buyout=3598)
    add(fb.auctions, 32, UNNAMED, 1, "VERY_LONG", buyout=3600)
    add(fb.auctions, 41, AXE, 1, "VERY_LONG", buyout=100000000)          # a 10,000g joke listing
    add(fb.auctions, 51, ARROW, 200, "LONG", buyout=80)                   # 0.4c per arrow
    add(fb.auctions, 61, THIN, 1, "LONG", buyout=1)                       # 1c troll
    add(fb.auctions, 62, THIN, 1, "LONG", buyout=50000)
    add(fb.auctions, 63, THIN, 1, "LONG", buyout=52000)
    add(fb.auctions, 71, NEVER, 2, "LONG", buyout=900)
    for i in range(1000, 1200):       # 200 commodity auctions
        add(fb.commodities, i, ORE, 10, "VERY_LONG", unit=180)


def pipeline_tests():
    tmp = tempfile.mkdtemp(prefix="fah_main_")
    out = os.path.join(tmp, "data")
    gh = os.path.join(tmp, "gh_output.txt")
    fb = FakeBlizzard()
    saved = {k: getattr(fetch_ah, k) for k in ("api", "get_token", "time", "SOLD_FLOOR_FROM")}
    old_env = os.environ.get("GITHUB_OUTPUT")
    T0 = 1794934800 + 25 * 60          # Blizzard data at 17:25 UTC
    M = 60

    def run(data_t, run_delay=5 * M):
        fb.now = data_t + run_delay
        fb.modified = email.utils.formatdate(data_t, usegmt=True)
        open(gh, "w").close()
        fetch_ah.main(out)
        with open(gh, encoding="utf-8") as fh:
            return fh.read().strip()

    def status():
        return load_json(os.path.join(out, "status.json"))

    def hist(iid):
        return load_json(os.path.join(out, "hist", f"{iid}.json"))

    def line(iid):
        _, db, _ = load_lua(os.path.join(out, "ForeverAH_Data.lua"))
        return py_decode(db["items"][iid])

    import io
    import contextlib
    log = io.StringIO()
    try:
        fetch_ah.api = fb.api
        fetch_ah.get_token = lambda: "fake"
        fetch_ah.time = fb
        fetch_ah.SOLD_FLOOR_FROM = 0       # a collector that always had the sold floor: every hour counts
        os.environ["GITHUB_OUTPUT"] = gh
        start_book(fb)
        with contextlib.redirect_stdout(log):
            # 1. first snapshot
            check(run(T0) == "changed=true", "first run: changed=true")
            st = status()
            check(st["updated"] == T0 and st["fetched"] == T0 + 5 * M, "updated is Blizzard's time, fetched the run time")
            check(st["firstSnapshot"] and "first snapshot" in st["soldSkipped"] and st["salesHours"] == 0
                  and st["gapMinutes"] is None, f"first snapshot status {st!r}")
            check(st["auctionsPerEndpoint"] == {"auctions": 28, "commodities": 200}, f"per endpoint {st['auctionsPerEndpoint']}")
            run_checks(out, "pipeline run 1", quiet=True)
            lat = load_json(os.path.join(out, "latest.json"))["data"]
            check(lat[str(AXE)]["high"] == 100000000 and lat[str(AXE)]["n"] == 1, "joke axe: its own price, n=1")
            check(line(AXE)["auctions"] == 1 and line(AXE)["vendor"] == 1400, "axe auctions field and vendor price")
            check(lat[str(ARROW)]["low"] == 1 and lat[str(ARROW)]["high"] == 1 and line(ARROW)["listed"] == 200,
                  "0.4c arrows are listed at 1c, not dropped")
            check(lat[str(THIN)]["low"] == 1 and lat[str(THIN)]["high"] == 50000 and lat[str(THIN)]["n"] == 3,
                  f"thin market with a 1c troll: {lat[str(THIN)]!r}")
            check(lat[str(LINEN)]["n"] == 20 and lat[str(ORE)]["n"] == 200, "auction counts")
            check(line(UNNAMED)["name"] == "" and line(UNNAMED)["cheapest"] == 3598, "unknown item: empty name, prices kept")
            check(line(NEVER)["name"] == "", "never-named item: empty name")
            mp = {m["id"]: m for m in load_json(os.path.join(out, "mapping.json"))}
            check(mp[UNNAMED]["name"] == "" and mp[LINEN]["name"] == "Linen Cloth", "mapping names")
            check(load_json(os.path.join(out, "mapping.json"))[-1]["name"] == "", "unnamed items sorted last")
            check(fb.lookups[UNNAMED] == 3, f"looked up in all 3 static namespaces ({fb.lookups[UNNAMED]})")
            items = load_json(os.path.join(out, "items.json"))
            check(items[str(UNNAMED)]["unknown"] and items[str(UNNAMED)]["tried"] == T0 + 5 * M, "unknown cached with a time")
            before = open(os.path.join(out, "status.json"), "rb").read()

            # 2. Blizzard has nothing new: same Last-Modified, then an older one
            check(run(T0, 20 * M) == "changed=false", "same Last-Modified: changed=false")
            check(run(T0 - 30 * M, 50 * M) == "changed=false", "older Last-Modified: changed=false")
            check(open(os.path.join(out, "status.json"), "rb").read() == before, "unchanged runs write nothing")

            # 3. a normal hour: LONG auctions gone = sold, SHORT gone = expired, a stack shrank
            #    (buyers take the cheapest first, here and below). A seller pulled the 5.2g
            #    Thin Thing while the cheaper ones stayed up and listed it again at 4.9999g:
            #    not a sale
            for aid in (1, 2, 3, 11, 12, 41):
                del fb.auctions[aid]
            fb.auctions[4]["quantity"] = 2
            fb.auctions[4]["buyout"] = 49 * 2
            del fb.auctions[63]
            fb.auctions[64] = {"id": 64, "item": {"id": THIN}, "quantity": 1, "time_left": "VERY_LONG", "buyout": 49999}
            check(run(T0 + 60 * M) == "changed=true", "new data: changed=true")
            st = status()
            check(st["soldSkipped"] is None and st["gapMinutes"] == 60 and st["salesHours"] == 1
                  and not st["firstSnapshot"], f"hour 2 status {st!r}")
            check(hist(LINEN)["h"][-1][4] == 18 and hist(AXE)["h"][-1][4] == 1, "sold: 15 + 3 linen, 1 axe")
            check(hist(THIN)["h"][-1][3:5] == [3, 0], f"pulled and listed again: not sold {hist(THIN)['h'][-1]}")
            check(line(LINEN)["sold24"] == 18, "sold24 after one measured hour")
            check(fb.lookups[UNNAMED] == 3, "unknown item not retried after 1 hour")
            run_checks(out, "pipeline run 3", quiet=True)

            # 4. a 150 minute gap: vanished auctions are not counted as sold (the last 2 linen
            #    at 49c went first, then the 50c and 51c stacks)
            for aid in (4, 5, 6):
                del fb.auctions[aid]
            check(run(T0 + 210 * M) == "changed=true", "after a gap: changed=true")
            st = status()
            check(st["soldSkipped"] and "150 minutes" in st["soldSkipped"] and st["gapMinutes"] == 150
                  and st["salesHours"] == 1 and st["soldUnitsThisHour"] == 0, f"gap status {st!r}")
            check(hist(LINEN)["h"][-1][4] == 0 and line(LINEN)["sold24"] == 18, "gap hour has 0 sold, sold24 kept")
            run_checks(out, "pipeline run 4", quiet=True)

            # 5. the commodities list comes back empty: the auction count collapses. Nothing
            #    is published from it; only state.json notes it, so the next run tries again.
            saved_com = dict(fb.commodities)
            fb.commodities.clear()
            files = ("status.json", "latest.json", "mapping.json", "ForeverAH_Data.lua", os.path.join("hist", f"{ORE}.json"))
            before = {fn: open(os.path.join(out, fn), "rb").read() for fn in files}
            check(run(T0 + 270 * M) == "changed=true", "collapsed count: changed=true (state.json remembers it)")
            check(all(open(os.path.join(out, fn), "rb").read() == before[fn] for fn in files),
                  "collapsed count: nothing is published from it")
            sj = load_json(os.path.join(out, "state.json"))
            check(sj.get("held") and "fell" in sj["held"]["reason"] and sj["dataTime"] == T0 + 210 * M
                  and len(sj["snap"]) == 219, f"collapsed count: held, the old snapshot kept ({sj.get('held')!r})")
            # the next run downloads the same data again, complete this time: a normal hour
            fb.commodities.update(saved_com)
            check(run(T0 + 270 * M, 20 * M) == "changed=true", "complete download on the next run")
            st = status()
            check(st["soldSkipped"] is None and st["gapMinutes"] == 60 and st["salesHours"] == 2,
                  f"after a held download status {st!r}")
            check(hist(ORE)["h"][-1][4] == 0 and hist(ORE)["h"][-1][3] == 2000,
                  "the 200 commodity auctions were never counted as sold or missing")
            check("held" not in load_json(os.path.join(out, "state.json")), "held note cleared")
            run_checks(out, "pipeline run 5", quiet=True)

            # 6. normal (the unknown item now has a name, but 6 hours have not passed)
            fb.names[UNNAMED] = "Mystery Bolt"
            del fb.auctions[7]
            check(run(T0 + 330 * M) == "changed=true", "normal again")
            st = status()
            check(st["soldSkipped"] is None and st["salesHours"] == 3, f"normal again status {st!r}")
            check(hist(LINEN)["h"][-1][4] == 5 and hist(ORE)["h"][-1][4] == 0, "sold in a normal hour")
            check(fb.lookups[UNNAMED] == 3 and line(UNNAMED)["name"] == "", "not retried before 6 hours")

            # 7. the commodities download fails twice in a row: the first run is held, the
            #    second is used without a sold estimate; the unknown item is retried (6.75h)
            fb.com_fail = True
            check(run(T0 + 390 * M) == "changed=true", "failed commodities: held")
            check(load_json(os.path.join(out, "state.json")).get("held") and status()["updated"] == T0 + 330 * M
                  and fb.lookups[UNNAMED] == 3, "failed commodities: nothing published, nothing looked up")
            check(run(T0 + 390 * M, 20 * M) == "changed=true", "failed again: used")
            st = status()
            check(st["soldSkipped"] and "commodities" in st["soldSkipped"] and "looked the same" in st["soldSkipped"]
                  and st["auctionsPerEndpoint"]["commodities"] == 0 and st["salesHours"] == 3, f"failed status {st!r}")
            check(hist(ORE)["h"][-1][3] == 0 and hist(ORE)["h"][-1][4] == 0, "missing commodities are not sales")
            check(fb.lookups[UNNAMED] == 4 and line(UNNAMED)["name"] == "Mystery Bolt", "retry found the name")
            check(fb.lookups[NEVER] == 6 and line(NEVER)["name"] == "", "never-named item retried, still empty")
            mp = {m["id"]: m for m in load_json(os.path.join(out, "mapping.json"))}
            check(mp[UNNAMED]["name"] == "Mystery Bolt", "mapping has the found name")
            sj = load_json(os.path.join(out, "state.json"))
            check(sj.get("incompleteBase") and "held" not in sj, "the used snapshot is marked incomplete")
            run_checks(out, "pipeline run 7", quiet=True)

            # 8. three snapshots in the same hour (00:05, 00:25, 00:45, the next day). The first
            #    compares with the incomplete one, so it is not measured; the other two are,
            #    and the hour counts only their 40 minutes.
            fb.com_fail = False
            del fb.auctions[8]
            check(run(T0 + 400 * M, 1 * M) == "changed=true", "00:05 snapshot")
            st = status()
            check(st["soldSkipped"] and "previous snapshot looked incomplete" in st["soldSkipped"]
                  and st["gapMinutes"] == 10 and st["salesHours"] == 3 and hist(LINEN)["h"][-1][4] == 0,
                  f"after an incomplete snapshot {st!r}")
            check("incompleteBase" not in load_json(os.path.join(out, "state.json")), "incomplete mark cleared")
            del fb.auctions[9]
            check(run(T0 + 420 * M, 1 * M) == "changed=true", "00:25 snapshot")
            del fb.auctions[10]
            check(run(T0 + 440 * M, 1 * M) == "changed=true", "00:45 snapshot")
            st = status()
            h8 = (T0 + 440 * M) - (T0 + 440 * M) % 3600
            check(hist(LINEN)["h"][-1][0] == h8 and hist(LINEN)["h"][-2][0] == h8 - 3600
                  and hist(LINEN)["h"][-1][4] == 10, f"same hour merged: {hist(LINEN)['h'][-2:]}")
            check(st["soldSkipped"] is None and st["gapMinutes"] == 20, f"third snapshot in the hour {st!r}")
            # measured: 60 + 60 + 60 minutes, then 20 + 20 in this hour = 220 minutes = 4 hours (not 4 slots)
            check(st["salesMinutes"] == 220 and st["salesHours"] == 4 and line(LINEN)["sold24"] == 18 + 5 + 10,
                  f"salesMinutes {st['salesMinutes']}, salesHours {st['salesHours']}, sold24 {line(LINEN)['sold24']}")
            sj = load_json(os.path.join(out, "state.json"))
            check(sj["measured"].get(str(h8)) == 40 * 60, f"this hour holds 40 measured minutes: {sj['measured']!r}")
            check(fb.lookups[NEVER] == 6, "not retried again within 6 hours")
            run_checks(out, "pipeline run 9", quiet=True)

        # 9. upgrade: state.json as the first real snapshot wrote it (no dataTime, hour, measured)
        shutil.rmtree(out, ignore_errors=True)
        fb2 = FakeBlizzard()
        start_book(fb2)
        fetch_ah.api, fetch_ah.time = fb2.api, fb2
        fb = fb2
        with contextlib.redirect_stdout(log):
            run(T0, 17 * M)
            sp = os.path.join(out, "state.json")
            st0 = load_json(sp)
            old = {"modified": st0["modified"], "snap": st0["snap"], "at": T0 + 17 * M}
            with open(sp, "w", encoding="utf-8") as fh:
                json.dump(old, fh)
            del fb.auctions[1]
            check(run(T0 + 58 * M) == "changed=true", "upgrade run")
            st = status()
            check(st["soldSkipped"] is None and st["gapMinutes"] == 58 and st["salesHours"] == 1,
                  f"upgrade status {st!r}")
            check([p[0] for p in hist(LINEN)["h"]] == [1794934800, 1794938400] and hist(LINEN)["h"][-1][4] == 5,
                  f"upgrade hours {hist(LINEN)['h']}")
            run_checks(out, "pipeline upgrade", quiet=True)

            # the older version filed points under the run time's hour, which can be after the
            # data hour: the next point must never go before it
            H4 = 1794945600                    # 20:00 UTC
            st0 = load_json(sp)
            late = {"modified": st0["modified"], "snap": st0["snap"], "at": H4 + 5 * M}   # a 20:05 run
            with open(sp, "w", encoding="utf-8") as fh:
                json.dump(late, fh)
            for fn in os.listdir(os.path.join(out, "hist")):
                hp = os.path.join(out, "hist", fn)
                h = load_json(hp)
                h["h"][-1][0] = H4
                with open(hp, "w", encoding="utf-8") as fh:
                    json.dump(h, fh)
            del fb.auctions[2]
            check(run(T0 + 98 * M) == "changed=true", "data at 19:03 after a point filed at 20:00")
            hl = hist(LINEN)["h"]
            check([p[0] for p in hl] == [1794934800, H4] and hl[-1][4] == 5 + 5, f"never goes back: {hl}")
            check(status()["salesHours"] == 1 and status()["gapMinutes"] == 40, f"late hour status {status()!r}")
            run_checks(out, "pipeline late hour", exact_hour=False, quiet=True)
        print("  pipeline: 13 collector runs on hand-made snapshots (fake Blizzard API) and the upgrade from old files")
    except Exception:
        sys.stdout.write(log.getvalue()[-3000:])
        raise
    finally:
        for k, v in saved.items():
            setattr(fetch_ah, k, v)
        if old_env is None:
            os.environ.pop("GITHUB_OUTPUT", None)
        else:
            os.environ["GITHUB_OUTPUT"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)


CLEAVER, SHIELD = 800, 900
BEAR, EAGLE, ODD, WOLF = 12722, 12718, 99001, 12726      # 99001 has no known suffix name


def start_gear_book(fb):
    """Green gear with random suffixes, a cheap commodity dump and a listing under the vendor price."""
    fb.names.update({CLEAVER: "Ridge Cleaver", SHIELD: "Old Shield"})
    fb.sell.update({CLEAVER: 2000, SHIELD: 3000, ORE: 5})
    fb.details[CLEAVER] = {"quality": {"type": "UNCOMMON"}, "level": 30, "required_level": 25,
                           "item_class": {"name": "Weapon", "id": 2}, "item_subclass": {"name": "Axe", "id": 1},
                           "inventory_type": {"type": "TWOHWEAPON", "name": "Two-Hand"}}

    def gear(aid, bonus, price, tl="VERY_LONG", mod=5240):
        fb.auctions[aid] = {"id": aid, "item": {"id": CLEAVER, "context": 1, "bonus_lists": [bonus],
                                                "modifiers": [{"type": 28, "value": mod}]},
                            "quantity": 1, "buyout": price, "time_left": tl}
    for n, p in enumerate((50000, 51000, 52000, 53000, 54000)):
        gear(2000 + n, BEAR, p, mod=(5240, 6874)[n % 2])     # modifiers differ: still one version
    gear(2010, BEAR, 15000, tl="LONG")                        # a cheap Bear: the real flip
    gear(2020, EAGLE, 9000)                                   # the Eagle is cheaper, and that is normal
    for n, p in enumerate((30000, 31000, 32000)):
        gear(2030 + n, ODD, p)
    for aid, p in ((2100, 1000), (2101, 5000), (2102, 5000)):
        fb.auctions[aid] = {"id": aid, "item": {"id": SHIELD}, "quantity": 1, "buyout": p, "time_left": "VERY_LONG"}
    for i in range(3000, 3200):
        fb.commodities[i] = {"id": i, "item": {"id": ORE}, "quantity": 10, "unit_price": 180, "time_left": "VERY_LONG"}
    for i in range(3200, 3203):                               # 30 ore dumped at 60c
        fb.commodities[i] = {"id": i, "item": {"id": ORE}, "quantity": 10, "unit_price": 60, "time_left": "VERY_LONG"}


def variant_pipeline_tests():
    tmp = tempfile.mkdtemp(prefix="fah_var_")
    out = os.path.join(tmp, "data")
    fb = FakeBlizzard()
    saved = {k: getattr(fetch_ah, k) for k in ("api", "get_token", "time", "DATA_FORMAT", "SOLD_FLOOR_FROM")}
    old_env = os.environ.pop("GITHUB_OUTPUT", None)
    T0 = 1794934800 + 25 * 60
    M = 60
    import io
    import contextlib
    log = io.StringIO()

    def run(data_t, run_delay=5 * M):
        fb.now = data_t + run_delay
        fb.modified = email.utils.formatdate(data_t, usegmt=True)
        fetch_ah.main(out)

    def js(name):
        return load_json(os.path.join(out, name))

    def line(iid):
        _, db, _ = load_lua(os.path.join(out, "ForeverAH_Data.lua"))
        s = db["items"][iid]
        return py_decode(s) if s is not None else None

    def db():
        return load_lua(os.path.join(out, "ForeverAH_Data.lua"))[1]
    V1, V2, V3, V4 = (CLEAVER * 1000 + k for k in (1, 2, 3, 4))
    try:
        fetch_ah.api, fetch_ah.get_token, fetch_ah.time = fb.api, (lambda: "fake"), fb
        fetch_ah.SOLD_FLOOR_FROM = 0       # a collector that always had the sold floor: every hour counts
        start_gear_book(fb)
        with contextlib.redirect_stdout(log):
            # 1. first snapshot: three versions of the cleaver, in order of first appearance
            run(T0)
            sj = js("state.json")
            check(sj["format"] == 3 and sj["variants"] == {str(CLEAVER): {str(BEAR): 1, str(EAGLE): 2, str(ODD): 3}},
                  f"version registry {sj.get('variants')!r}")
            check(sj["snap"]["2000"][4] == V1 and sj["snap"]["2020"][4] == V2 and len(sj["snap"]["2100"]) == 4,
                  "snapshot entries carry the vid of versions only")
            hv = load_json(os.path.join(out, "hist", f"{V1}.json"))
            check(hv["v"] == str(BEAR) and hv["h"][-1][1:4] == [15000, 50000, 6], f"Bear history {hv!r}")
            check(open(os.path.join(out, "hist", f"{V1}.json"), encoding="utf-8").read().startswith('{"v":'),
                  "the version key is at the start of its hist file")
            check(line(V1)["name"] == "Ridge Cleaver of the Bear" and line(V2)["name"] == "Ridge Cleaver of the Eagle"
                  and line(V3)["name"] == "Ridge Cleaver (version 3)", "version names")
            check(line(V1)["vendor"] == 2000 and line(V1)["listed"] == 6 and line(CLEAVER)["listed"] == 10
                  and line(CLEAVER)["auctions"] == 10, "version lines; the item line covers every version")
            check(db().variants[CLEAVER] == f"{BEAR}={V1},{EAGLE}={V2},{ODD}={V3}", f"variants table {db().variants[CLEAVER]!r}")
            mp = {m["id"]: m for m in js("mapping.json")}
            check(mp[V1]["base"] == CLEAVER and mp[V1]["variant"] == str(BEAR) and mp[V1]["vindex"] == 1
                  and mp[V1]["slot"] == "Two-Hand" and mp[V1]["ilvl"] == 30 and mp[V1]["reqlvl"] == 25
                  and mp[V1]["quality"] == "UNCOMMON" and mp[V1]["clsName"] == "Weapon", f"mapping version {mp[V1]!r}")
            check(mp[CLEAVER]["slot"] == "Two-Hand" and "base" not in mp[CLEAVER] and mp[ORE]["slot"] == "", "mapping items")
            lat = js("latest.json")["data"]
            check(lat[str(V1)]["low"] == 15000 and lat[str(V2)]["n"] == 1 and lat[str(CLEAVER)]["n"] == 10, "latest")
            # flips: the cheap Bear (a version), the ore dump, the shield under the vendor price;
            # never the cleaver lumped together (buy two cheap Eagles and Bears, relist at 2.99g)
            lumped = sorted(u for a in fb.auctions.values() if a["item"]["id"] == CLEAVER for u in [(a["buyout"], 1)])
            check(fetch_ah.find_gap_flip([(float(p), q) for p, q in lumped]) is not None,
                  "lumped together, the cleaver looks like a flip")
            fl = js("flips.json")["flips"]
            got = [(r["id"], r["kind"], r["units"], r["cost"], r["relist"], r["profit"], r["risk"]) for r in fl]
            # the Bear has no history of its own yet; five Bears at 5g to 5.4g back up the relist at 4.99g
            # (a version never borrows the whole cleaver's price: the cheap Eagle sets that)
            check(got == [(V1, "gap", 1, 15000, 49999, 32499, ["slow", "suffix"]),
                          (ORE, "gap", 30, 1800, 179, 3300, ["slow"]),
                          (SHIELD, "vendor", 1, 1000, 0, 2000, [])], f"flips {got}")
            check(fl[0]["name"] == "Ridge Cleaver of the Bear" and fl[0]["base"] == CLEAVER and fl[0]["variant"] == str(BEAR)
                  and fl[0]["slot"] == "Two-Hand" and fl[0]["next"] == 50000 and fl[0]["listed"] == 6
                  and fl[0]["market"] == 50000 and fl[0]["sold24"] == 0, f"flip row {fl[0]!r}")
            # no history yet: no usual price (0) on any flip. The Bear's gap flip was judged with
            # today's market value (5g), but that is not its usual price
            check(db().flips[1] == f"{V1}|gap|15000|1|15000|49999|32499|2166|slow,suffix|0",
                  f"addon flip {db().flips[1]!r}")
            check([r["usual"] for r in fl] == [0, 0, 0] and fl[0]["market"] == 50000 and fl[1]["market"] == 180,
                  f"usual prices {[r['usual'] for r in fl]}")
            st = js("status.json")
            check(st["versions"] == 3 and st["itemsWithVersions"] == 1 and st["flips"] == 3 and st["items"] == 3
                  and st["suffixNames"] > 300, f"status {st!r}")
            check(js("suffix_names.json").get(str(BEAR)) == "of the Bear", "suffix table copied to the data folder")
            run_checks(out, "versions run 1", quiet=True)

            # 2. an hour later the cheap Bear and the Eagle sold
            del fb.auctions[2010]
            del fb.auctions[2020]
            run(T0 + 60 * M)
            check(load_json(os.path.join(out, "hist", f"{V1}.json"))["h"][-1][4] == 1
                  and load_json(os.path.join(out, "hist", f"{V2}.json"))["h"][-1][3:5] == [0, 1]
                  and load_json(os.path.join(out, "hist", f"{CLEAVER}.json"))["h"][-1][4] == 2, "sold per version and item")
            check(line(V1)["sold24"] == 1 and line(V2)["sold24"] == 1 and line(CLEAVER)["sold24"] == 2, "sold24")
            check(not any(r["id"] in (V1, CLEAVER) for r in js("flips.json")["flips"]), "no cleaver flips left")
            run_checks(out, "versions run 2", quiet=True)

            # 3. three and a half days later nobody listed or bought an Eagle: its file is left alone,
            #    its line stays for the rest of the week, it leaves latest.json
            before = open(os.path.join(out, "hist", f"{V2}.json"), "rb").read()
            run(T0 + 84 * 60 * M)
            check(open(os.path.join(out, "hist", f"{V2}.json"), "rb").read() == before, "idle version file not rewritten")
            check(line(V2) is not None and line(V2)["listed"] == 0 and str(V2) not in js("latest.json")["data"],
                  "idle version: line kept, not in latest")
            check(len(load_json(os.path.join(out, "hist", f"{CLEAVER}.json"))["h"]) == 3, "items still get every point")
            run_checks(out, "versions idle", quiet=True)

            # 4. state.json lost its registry: rebuilt from the hist files, indexes never handed out twice
            sp = os.path.join(out, "state.json")
            sj = js("state.json")
            sj.pop("variants")
            with open(sp, "w", encoding="utf-8") as fh:
                json.dump(sj, fh)
            fb.auctions[2200] = {"id": 2200, "item": {"id": CLEAVER, "bonus_lists": [WOLF]}, "quantity": 1,
                                 "buyout": 40000, "time_left": "VERY_LONG"}
            run(T0 + 85 * 60 * M)
            check(js("state.json")["variants"][str(CLEAVER)] == {str(BEAR): 1, str(EAGLE): 2, str(ODD): 3, str(WOLF): 4},
                  f"registry rebuilt {js('state.json')['variants']!r}")
            check(line(V4)["name"] == "Ridge Cleaver of the Wolf", "new version after the rebuild")
            run_checks(out, "versions rebuilt", quiet=True)

            # 5. an older copy of the collector (state format 2) refuses format 3 data
            fetch_ah.DATA_FORMAT = 2
            try:
                run(T0 + 86 * 60 * M)
                check(False, "an older collector ran on newer data")
            except SystemExit as e:
                check("newer fetch_ah.py" in str(e), f"older collector message {e}")
            fetch_ah.DATA_FORMAT = saved["DATA_FORMAT"]

        # 6. upgrade from format 2 (what the collector wrote before versions existed)
        shutil.rmtree(out, ignore_errors=True)
        fb = FakeBlizzard()
        fetch_ah.api, fetch_ah.time = fb.api, fb
        start_gear_book(fb)
        with contextlib.redirect_stdout(log):
            run(T0)
            sp = os.path.join(out, "state.json")
            sj = js("state.json")
            sj["format"] = 2
            sj.pop("variants")
            sj["snap"] = {k: v[:4] for k, v in sj["snap"].items()}
            with open(sp, "w", encoding="utf-8") as fh:
                json.dump(sj, fh)
            for fn in os.listdir(os.path.join(out, "hist")):
                if int(fn[:-5]) >= CLEAVER * 1000:
                    os.remove(os.path.join(out, "hist", fn))
            os.remove(os.path.join(out, "flips.json"))
            del fb.auctions[2010]                 # the cheap Bear sold
            run(T0 + 60 * M)
            sj = js("state.json")
            check(sj["format"] == 3 and sj["variants"][str(CLEAVER)] == {str(BEAR): 1, str(EAGLE): 2, str(ODD): 3},
                  f"format 2 upgraded {sj.get('variants')!r}")
            check(load_json(os.path.join(out, "hist", f"{CLEAVER}.json"))["h"][-1][4] == 1
                  and load_json(os.path.join(out, "hist", f"{V1}.json"))["h"] == [[T0 - 25 * M + 3600, 50000, 51000, 5, 0]],
                  "the item counts the sale; the new version starts now without one")
            check(js("status.json")["salesHours"] == 1 and js("status.json")["versions"] == 3, "upgrade status")
            hv = load_json(os.path.join(out, "hist", f"{V1}.json"))
            idx = fetch_ah.measured_index(fetch_ah.measured_secs(sj))
            check(fetch_ah.sold_per_day(hv, fetch_ah.measured_secs(sj), sj["hour"], idx) is None,
                  "a version tracked from this hour on has unknown sales")
            U = sj["hour"]
            check(sj["versionsFrom"] == {"hour": U, "secs": 3600}, f"the upgrade hour is remembered {sj.get('versionsFrom')!r}")
            check(line(V1)["sold24"] == 0 and js("volumes.json")["data"][str(V1)] == 0, "upgrade run: no version sales yet")
            run_checks(out, "versions upgrade from format 2", quiet=True)
            # an hour later a Bear sells: 1 hour of version sales next to 2 measured hours for
            # everything else, so its sold24 is scaled to those 2 hours (the site and the addon
            # divide by salesHours): 2, not 1 (which would read as half the real rate). The
            # cheaper Eagle and odd ones that stayed up are other versions: never a Bear's floor.
            # The 5.4g Bear was pulled while cheaper Bears stayed up: not a sale
            del fb.auctions[2000]
            del fb.auctions[2004]
            run(T0 + 120 * M)
            sj = js("state.json")
            check(js("status.json")["salesHours"] == 2 and sj["versionsFrom"]["hour"] == U, "second run after the upgrade")
            d24 = js("24h.json")["data"][str(V1)]
            check(line(V1)["sold24"] == 2 and js("volumes.json")["data"][str(V1)] == 2
                  and d24["highPriceVolume"] + d24["lowPriceVolume"] == 2,
                  f"version sold24 scaled to the measured hours ({line(V1)['sold24']})")
            check(line(CLEAVER)["sold24"] == 2, "the item counts its two sales as they are")
            # a day later the upgrade is forgotten and versions count like everything else
            run(T0 + (25 * 60 + 120) * M)
            check("versionsFrom" not in js("state.json"), "the upgrade hour is forgotten after a day")
            run_checks(out, "versions a day after the upgrade", quiet=True)
        print("  versions: 7 collector runs with suffix gear (names, registry, sold per version, idle versions, "
              "rebuild, format 2 upgrade, older copy refused) and snapshot flips")
    except Exception:
        sys.stdout.write(log.getvalue()[-3000:])
        raise
    finally:
        for k, v in saved.items():
            setattr(fetch_ah, k, v)
        if old_env is not None:
            os.environ["GITHUB_OUTPUT"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)


BLUE, GREEN, PLAIN, MATS, ARMOR, LONE, NEWBIE, FEW, JUNK = 5000, 5100, 5200, 5300, 5400, 5500, 5600, 5700, 5800


def deal_pipeline_tests():
    """Six hours of a steady market, then cheap listings: which series get a deal. Also every
    flip's usual price: 0 until 4 hours of history, then the 7-day median (a vendor flip too)."""
    tmp = tempfile.mkdtemp(prefix="fah_deal_")
    out = os.path.join(tmp, "data")
    fb = FakeBlizzard()
    saved = {k: getattr(fetch_ah, k) for k in ("api", "get_token", "time", "SOLD_FLOOR_FROM")}
    old_env = os.environ.pop("GITHUB_OUTPUT", None)
    T0 = 1794934800 + 25 * 60
    M = 60
    import io
    import contextlib
    log = io.StringIO()
    nid = [9000]

    def add(iid, price, book=None, bonus=None, unit=False):
        nid[0] += 1
        a = {"id": nid[0], "item": {"id": iid}, "quantity": 1, "time_left": "VERY_LONG"}
        if bonus:
            a["item"]["bonus_lists"] = [bonus]
        if unit:
            a["unit_price"] = price
        else:
            a["buyout"] = price
        (fb.commodities if unit else fb.auctions)[nid[0]] = a
        return nid[0]

    def run(data_t):
        fb.now = data_t + 5 * M
        fb.modified = email.utils.formatdate(data_t, usegmt=True)
        fetch_ah.main(out)

    def js(name):
        return load_json(os.path.join(out, name))

    AV = ARMOR * 1000 + 1                 # Iron Armor of the Bear, the item's first version
    try:
        fetch_ah.api, fetch_ah.get_token, fetch_ah.time = fb.api, (lambda: "fake"), fb
        fetch_ah.SOLD_FLOOR_FROM = 0       # a collector that always had the sold floor: every hour counts
        fb.names.update({BLUE: "Blue Ring", GREEN: "Green Boots", PLAIN: "Plain Shirt", MATS: "Green Dust",
                         ARMOR: "Iron Armor", LONE: "Lone Epic", NEWBIE: "New Blue", FEW: "Green Gloves",
                         JUNK: "Heavy Junk"})
        for iid, q in ((BLUE, "RARE"), (GREEN, "UNCOMMON"), (PLAIN, "COMMON"), (MATS, "UNCOMMON"),
                       (ARMOR, "UNCOMMON"), (LONE, "EPIC"), (NEWBIE, "RARE"), (FEW, "UNCOMMON"), (JUNK, "COMMON")):
            fb.details[iid] = {"quality": {"type": q}}
        fb.sell[JUNK] = 1000
        for p in (40000, 41000, 42000):
            add(BLUE, p)                      # a blue nobody buys: its usual price is 4.1g
            add(PLAIN, p)                     # the same, but a common item
            add(MATS, p, unit=True)           # the same, but on the commodities list
            add(ARMOR, p, bonus=BEAR)         # the same, but a version of the item
        green = {p: add(GREEN, p) for p in (30000, 31000, 32000, 33000, 34000)}
        add(LONE, 50000)                      # an epic, only ever one listed
        gloves = {p: add(FEW, p) for p in (10000, 10500, 11000)}   # 3 listed: the market value is 1.05g
        for p in (500, 1500, 1600, 1700):
            add(JUNK, p)                      # a vendor pays 10s, one is listed at 5s: a vendor flip every hour
        with contextlib.redirect_stdout(log):
            for k in range(6):
                if k:                         # one pair of Green Boots sells every hour (and is listed again)
                    del fb.auctions[green[30000]]
                    green[30000] = add(GREEN, 30000)
                if k in (1, 2):               # two pairs of Green Gloves sell in these hours: 2 a day
                    del fb.auctions[gloves[10000]]
                    gloves[10000] = add(FEW, 10000)
                if k == 5:                    # then the cheap listings show up
                    # (a new blue: 30s next to three at 65s to 67s. Today's market value is 65s and 30s
                    # makes 31s at 64.99s, too little for a gap flip; with no history it is no deal either)
                    for iid, p in ((BLUE, 12000), (PLAIN, 12000), (GREEN, 9000), (LONE, 15000), (NEWBIE, 3000),
                                   (NEWBIE, 6500), (NEWBIE, 6600), (NEWBIE, 6700), (FEW, 3000), (FEW, 6500),
                                   (FEW, 7600)):
                        add(iid, p)
                    add(MATS, 12000, unit=True)
                    add(ARMOR, 12000, bonus=BEAR)
                run(T0 + k * 60 * M)
                if k < 5:
                    check(not any(r["kind"] == "deal" for r in js("flips.json")["flips"]), f"hour {k}: no deal yet")
                # the usual price is the 7-day median, whatever the kind: 0 until 4 hours of it
                junk = [(r["kind"], r["usual"], r["market"]) for r in js("flips.json")["flips"] if r["id"] == JUNK]
                check(junk == [("vendor", 1500 if k >= 3 else 0, 1500)],
                      f"hour {k}: a vendor flip's usual price, 0 until 4 hours of history, then the median ({junk})")
            fl = js("flips.json")["flips"]
            got = [(r["id"], r["kind"], r["buyMax"], r["units"], r["cost"], r["relist"], r["profit"], r["risk"],
                    r["usual"]) for r in fl]
            check(got == [(LONE, "deal", 15000, 1, 15000, 49999, 32499, ["thin", "slow", "unsure"], 50000),
                          (BLUE, "deal", 12000, 1, 12000, 39999, 25999, ["slow"], 41000),
                          (AV, "deal", 12000, 1, 12000, 39999, 25999, ["slow", "suffix"], 41000),
                          (GREEN, "gap", 9000, 1, 9000, 29999, 19499, ["slow"], 31000),
                          (FEW, "deal", 3000, 1, 3000, 6499, 3174, ["slow"], 10500),
                          (JUNK, "vendor", 500, 1, 500, 0, 500, [], 1500)],
                  f"deals: the blue, the epic (one listing, so unsure) and the version; the green boots sell, "
                  f"so theirs is a gap flip (no deal on top); the green gloves at 30s (usually 1.05g); none for "
                  f"the common shirt, the dust (a commodity), the armor itself (its listings are versions) or the "
                  f"new blue (no history yet); the junk's vendor flip with its usual price: {got}")
            # the gloves: 2 sell a day, so both cheap ones under 70% (30s and 65s) would be bought, but
            # relisted at 75.99s (under the 76s one) the 65s one makes under 15%, and no gap flip makes 50s.
            # The 30s one alone (relisted at 64.99s, 31s 74c profit) is still a buy: the rule on purpose
            book = [(3000, 1), (6500, 1), (7600, 1), (10000, 1), (10500, 1), (11000, 1)]
            check(model_deal(book, 10500, 2.0, 10500) == (1, 3000, 6499, 3174, "fewer")
                  and fetch_ah.find_gap_flip(book, False, 2.0, 10500, 10500) is None,
                  "the gloves' deal is the cheaper one alone, and there is no gap flip")
            row = fl[1]
            check(row["name"] == "Blue Ring" and row["quality"] == "RARE" and row["next"] == 40000
                  and row["market"] == 40000 and row["listed"] == 4 and row["auctions"] == 4 and row["sold24"] == 0,
                  f"deal row {row!r}")
            check(fl[2]["name"] == "Iron Armor of the Bear" and fl[2]["base"] == ARMOR and fl[2]["variant"] == str(BEAR),
                  f"version deal row {fl[2]!r}")
            _, db, _ = load_lua(os.path.join(out, "ForeverAH_Data.lua"))
            lines = [db.flips[i + 1] for i in range(6)]
            check(lines == [f"{LONE}|deal|15000|1|15000|49999|32499|2166|thin,slow,unsure|50000",
                            f"{BLUE}|deal|12000|1|12000|39999|25999|2166|slow|41000",
                            f"{AV}|deal|12000|1|12000|39999|25999|2166|slow,suffix|41000",
                            f"{GREEN}|gap|9000|1|9000|29999|19499|2166|slow|31000",
                            f"{FEW}|deal|3000|1|3000|6499|3174|1058|slow|10500",
                            f"{JUNK}|vendor|500|1|500|0|500|1000||1500"] and db.flips[7] is None,
                  f"addon flip lines {lines}")
            run_checks(out, "deals", quiet=True)
            # which flips the addon file gets, with tiny limits: the best 1, 1 more deal, 1 more flip
            # costing at most 90s (the armor deal costs 1.2g: left out; the boots come before the
            # cheaper gloves and junk, which find no room)
            limits = {k: getattr(fetch_ah, k) for k in ("FLIPS_ADDON_MAX", "FLIPS_ADDON_DEALS", "FLIPS_ADDON_CHEAP",
                                                        "FLIP_CHEAP_COST")}
            try:
                fetch_ah.FLIPS_ADDON_MAX, fetch_ah.FLIPS_ADDON_DEALS, fetch_ah.FLIPS_ADDON_CHEAP = 1, 1, 1
                fetch_ah.FLIP_CHEAP_COST = 9000
                run(T0 + 5 * 60 * M + 20 * M)  # another snapshot in the same hour, nothing changed
            finally:
                for k, v in limits.items():
                    setattr(fetch_ah, k, v)
            _, db, _ = load_lua(os.path.join(out, "ForeverAH_Data.lua"))
            picked = [int(str(db.flips[i + 1]).split("|")[0]) if db.flips[i + 1] else None for i in range(3)]
            check(picked == [LONE, BLUE, GREEN] and db.flips[4] is None
                  and [r["id"] for r in js("flips.json")["flips"]] == [LONE, BLUE, AV, GREEN, FEW, JUNK],
                  f"addon file: the best one, then a deal, then a cheap flip ({picked}); flips.json keeps all 6")
            # the cheap ring sells: no deal left for it
            del fb.auctions[next(a for a, x in fb.auctions.items() if x["item"]["id"] == BLUE and x["buyout"] == 12000)]
            run(T0 + 6 * 60 * M)
            check(not any(r["id"] == BLUE for r in js("flips.json")["flips"]), "the cheap ring sold: no deal")
            run_checks(out, "deals after a sale", quiet=True)
        print("  deals: 8 collector runs, deals for a blue, an epic and a green version, and a green whose two "
              "cheap listings do not both pay (the cheaper one alone); none for a common item, a commodity, an "
              "item made of versions, an item with no history or one with a gap flip; the addon file's best "
              "flips plus extra deals and cheap flips; usual prices (0, then the 7-day median, a vendor flip's too)")
    except Exception:
        sys.stdout.write(log.getvalue()[-3000:])
        raise
    finally:
        for k, v in saved.items():
            setattr(fetch_ah, k, v)
        if old_env is not None:
            os.environ["GITHUB_OUTPUT"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)


def floor_cutover_tests():
    """The sold floor's first hour (SOLD_FLOOR_FROM), on state an older copy measured (it counted
    pulled and reposted stacks as sales): the measured hours before it no longer count, so sold24,
    salesHours, the site's volumes and how many units a flip buys come from the hours from it on.
    The hist files keep their sold (the charts) and state.json keeps its layout."""
    sf = fetch_ah.SOLD_FLOOR_FROM
    check(sf % 3600 == 0 and 1795039200 <= sf <= time.time() + 3 * 86400,
          f"SOLD_FLOOR_FROM {sf}: a whole hour, not before the floor was written (2026-11-18 22:00 UTC) "
          "and not days ahead (no hour before it counts as measured)")
    tmp = tempfile.mkdtemp(prefix="fah_cut_")
    out = os.path.join(tmp, "data")
    fb = FakeBlizzard()
    saved = {k: getattr(fetch_ah, k) for k in ("api", "get_token", "time", "SOLD_FLOOR_FROM")}
    old_env = os.environ.pop("GITHUB_OUTPUT", None)
    H, M = 3600, 60
    T0 = 1794934800 + 25 * M               # data at 17:25 UTC
    CUT = 1794934800 + 2 * H               # the floor's first hour: 19:00 UTC
    import io
    import contextlib
    log = io.StringIO()

    def run(data_t):
        fb.now = data_t + 5 * M
        fb.modified = email.utils.formatdate(data_t, usegmt=True)
        fetch_ah.main(out)

    def js(name):
        return load_json(os.path.join(out, name))

    def line(iid):
        _, db, _ = load_lua(os.path.join(out, "ForeverAH_Data.lua"))
        return py_decode(db["items"][iid])

    def ore(aid, qty, unit):
        fb.commodities[aid] = {"id": aid, "item": {"id": ORE}, "quantity": qty, "time_left": "VERY_LONG",
                               "unit_price": unit}

    def ore_flips():
        return [(r["kind"], r["units"]) for r in js("flips.json")["flips"] if r["id"] == ORE]

    try:
        fetch_ah.api, fetch_ah.get_token, fetch_ah.time = fb.api, (lambda: "fake"), fb
        for i in range(1, 11):             # 10 stacks of 5 linen at 50c each
            fb.auctions[i] = {"id": i, "item": {"id": LINEN}, "quantity": 5, "time_left": "LONG", "buyout": 250}
        for i in range(100, 200):          # 100 stacks of 10 ore at 18s each
            ore(i, 10, 1800)
        with contextlib.redirect_stdout(log):
            # an older copy: the first snapshot, then an hour that sold 15 linen and 200 ore
            fetch_ah.SOLD_FLOOR_FROM = 0
            run(T0)
            for i in (1, 2, 3):
                del fb.auctions[i]
            for i in range(100, 120):
                del fb.commodities[i]
            run(T0 + 60 * M)
            sj = js("state.json")
            check(sj["measured"] == {str(CUT - H): 3600} and js("status.json")["salesHours"] == 1
                  and line(LINEN)["sold24"] == 15, f"the older copy measured 18:00: {sj['measured']!r}")
            keys = set(sj)
            # this copy, from 19:00: 5 linen and 10 ore sell, and cheap ore shows up: 10 at 10s, then
            # 20 at 14s. A flip buys the 10 (relist at 13s 99c) or all 30 (relist at 17s 99c), as many
            # as sell in a day: 10 since 19:00, 210 with the older copy's hour
            fetch_ah.SOLD_FLOOR_FROM = CUT
            del fb.auctions[4]
            del fb.commodities[120]
            for i, p in ((300, 1000), (301, 1000), (302, 1400), (303, 1400), (304, 1400), (305, 1400)):
                ore(i, 5, p)
            run(T0 + 120 * M)
            sj, st = js("state.json"), js("status.json")
            check(sj["measured"] == {str(CUT): 3600},
                  f"only the hours from SOLD_FLOOR_FROM are measured: {sj['measured']!r}")
            check(st["soldSkipped"] is None and st["salesHours"] == 1 and st["salesMinutes"] == 60,
                  f"salesHours counts the hours from SOLD_FLOOR_FROM: {st!r}")
            check(line(LINEN)["sold24"] == 5 and js("volumes.json")["data"][str(LINEN)] == 5,
                  f"sold24 (addon and site) counts the hours from SOLD_FLOOR_FROM: {line(LINEN)['sold24']}")
            check([p[4] for p in js(f"hist/{LINEN}.json")["h"]] == [0, 15, 5]
                  and [p[4] for p in js(f"hist/{ORE}.json")["h"]] == [0, 200, 10],
                  "the hist files keep the older copy's sold")
            check(fetch_ah.sales_window(js(f"hist/{ORE}.json"), fetch_ah.measured_secs(sj), CUT) == (10, 3600),
                  "sold per day for flips: the hours from SOLD_FLOOR_FROM")
            check(ore_flips() == [("gap", 10)], f"the ore flip buys the 10 that sell in a day, not 30 ({ore_flips()})")
            check(set(sj) == keys and sj["format"] == 3, f"state.json layout changed: {sorted(set(sj) ^ keys)}")
            run_checks(out, "sold floor cutover", quiet=True)
            # the next hour adds up as usual
            del fb.auctions[5]
            run(T0 + 180 * M)
            sj, st = js("state.json"), js("status.json")
            check(sj["measured"] == {str(CUT): 3600, str(CUT + H): 3600} and st["salesHours"] == 2
                  and line(LINEN)["sold24"] == 10, f"the hour after: {sj['measured']!r}, {st['salesHours']} hours")
            run_checks(out, "sold floor cutover, the hour after", quiet=True)
        print("  sold floor cutover: 4 collector runs; hours an older copy measured before SOLD_FLOOR_FROM "
              "no longer count (sold24, salesHours, volumes, a flip's units), the hist files and the state "
              "layout stay")
    except Exception:
        sys.stdout.write(log.getvalue()[-3000:])
        raise
    finally:
        for k, v in saved.items():
            setattr(fetch_ah, k, v)
        if old_env is not None:
            os.environ["GITHUB_OUTPUT"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)


# -- item details: the site's tooltip block (details/<n>.json) ---------------
# Real Blizzard item API responses (static-forever-us and static-classic1x-us, 18 Nov 2026), cut
# down to the fields a tooltip uses. A classic1x preview_item has no item level of its own.
def _rgb(r, g, b):
    return {"r": r, "g": g, "b": b, "a": 1.0}


_W, _G = _rgb(255, 255, 255), _rgb(0, 255, 0)


def _stat(name, v, colour=_W, bonus=False):
    s = {"type": {"type": name.upper().replace(" ", "_"), "name": name}, "value": v,
         "display": {"display_string": "%+d %s" % (v, name), "color": colour}}
    if bonus:
        s["is_equip_bonus"] = True
    return s


def _sell(v):
    return {"value": v, "display_strings": {"header": "Sell Price:", "gold": "0", "silver": "0", "copper": str(v)}}


TIP_ROBE = {"name": "Robe of the Magi", "level": 40, "required_level": 35, "is_equippable": True,
            "inventory_type": {"type": "ROBE", "name": "Chest"},
            "preview_item": {
                "name": "Robe of the Magi", "quality": {"type": "RARE", "name": "Rare"},
                "item_class": {"name": "Armor", "id": 4}, "item_subclass": {"name": "Cloth", "id": 1},
                "inventory_type": {"type": "ROBE", "name": "Chest"},
                "binding": {"type": "ON_EQUIP", "name": "Binds when equipped"},
                "armor": {"value": 58, "display": {"display_string": "58 Armor", "color": _W}},
                "stats": [_stat("Intellect", 6), _stat("Spirit", 5)],
                "spells": [{"spell": {"name": "Increase Spell Dam 22", "id": 15714}, "description":
                            "Equip: Increases damage and healing done by magical spells and effects by up to 22."}],
                "sell_price": _sell(5067), "requirements": {"level": {"value": 35, "display_string": "Requires Level 35"}},
                "durability": {"value": 80, "display_string": "Durability 80 / 80"}}}
TIP_EDWARD = {"name": "Hand of Edward the Odd", "level": 62, "required_level": 57, "is_equippable": True,
              "preview_item": {
                  "name": "Hand of Edward the Odd", "quality": {"type": "EPIC", "name": "Epic"},
                  "item_class": {"name": "Weapon", "id": 2}, "item_subclass": {"name": "Mace", "id": 4},
                  "inventory_type": {"type": "WEAPON", "name": "One-Hand"},
                  "binding": {"type": "ON_EQUIP", "name": "Binds when equipped"}, "unique_equipped": "Unique",
                  "weapon": {"damage": {"min_value": 50, "max_value": 94, "display_string": "50 - 94 Damage",
                                        "damage_class": {"type": "PHYSICAL", "name": "Physical"}},
                             "attack_speed": {"value": 1600, "display_string": "Speed 1.60"},
                             "dps": {"value": 45.0, "display_string": "(45.0 damage per second)"}},
                  "stats": [_stat("Intellect", 13), _stat("Spirit", 10)],
                  "spells": [{"spell": {"name": "Focus", "id": 18803},
                              "description": "Chance on hit: Next spell cast within 4 sec will cast instantly."}],
                  "sell_price": _sell(70554), "requirements": {"level": {"value": 57, "display_string": "Requires Level 57"}},
                  "durability": {"value": 105, "display_string": "Durability 105 / 105"}}}
TIP_POTION = {"name": "Healing Potion", "level": 22, "required_level": 12,
              "preview_item": {
                  "name": "Healing Potion", "quality": {"type": "COMMON", "name": "Common"},
                  "item_class": {"name": "Consumable", "id": 0}, "item_subclass": {"name": "Potions", "id": 1},
                  "inventory_type": {"type": "NON_EQUIP", "name": "Non-equippable"},
                  "spells": [{"spell": {"name": "Healing Potion", "id": 441},
                              "description": "Use: Restores 280 to 360 health. (2 Min Cooldown)"}],
                  "sell_price": _sell(75), "requirements": {"level": {"value": 12, "display_string": "Requires Level 12"}},
                  "is_subclass_hidden": True}}
TIP_CODEX = {"name": "Codex of Defense", "description": "The tome is magically sealed.",
             "preview_item": {
                 "name": "Codex of Defense", "quality": {"type": "RARE", "name": "Rare"},
                 "item_class": {"name": "Quest", "id": 12}, "item_subclass": {"name": "Quest", "id": 0},
                 "inventory_type": {"type": "NON_EQUIP", "name": "Non-equippable"}, "unique_equipped": "Unique",
                 "requirements": {"level": {"value": 54, "display_string": "Requires Level 54"},
                                  "playable_classes": {"display_string": "Classes: Warrior"}},
                 "description": "The tome is magically sealed.",
                 "item_starts_quest": {"quest": {"name": "Codex of Defense", "id": 7499},
                                       "display_string": "This Item Begins a Quest"},
                 "is_subclass_hidden": True}}
TIP_BELT = {"name": "Belt of Valor", "level": 58, "required_level": 53, "is_equippable": True,
            "preview_item": {
                "name": "Belt of Valor", "quality": {"type": "RARE", "name": "Rare"},
                "item_class": {"name": "Armor", "id": 4}, "item_subclass": {"name": "Plate", "id": 4},
                "inventory_type": {"type": "WAIST", "name": "Waist"},
                "binding": {"type": "ON_EQUIP", "name": "Binds when equipped"},
                "armor": {"value": 341, "display": {"display_string": "341 Armor", "color": _W}},
                "stats": [_stat("Strength", 14), _stat("Agility", 7), _stat("Stamina", 8), _stat("Spirit", 4)],
                "sell_price": _sell(9295), "requirements": {"level": {"value": 53, "display_string": "Requires Level 53"}},
                "set": {"item_set": {"name": "Battlegear of Valor", "id": 189},
                        "items": [{"item": {"name": "Belt of Valor", "id": 16736}},
                                  {"item": {"name": "Boots of Valor", "id": 16734}}],
                        "effects": [{"display_string": "(2) Set: +200 Armor.", "required_count": 2},
                                    {"display_string": "(4) Set: +40 Attack Power.", "required_count": 4},
                                    {"display_string": "(6) Set: Chance on melee attack to heal you for 88 to 132.",
                                     "required_count": 6},
                                    {"display_string": "(8) Set: +8 All Resistances.", "required_count": 8}],
                        "display_string": "Battlegear of Valor (0/8)"},
                "description": "Worn by the champions of Valor.",     # not on the real belt: a set AND flavor text
                "durability": {"value": 45, "display_string": "Durability 45 / 45"}}}
TIP_PLANS = {"name": "Plans: Arcanite Reaper", "description": "Teaches you how to make an Arcanite Reaper.",
             "preview_item": {
                 "name": "Plans: Arcanite Reaper", "quality": {"type": "RARE", "name": "Rare"},
                 "item_class": {"name": "Recipe", "id": 9}, "item_subclass": {"name": "Blacksmithing", "id": 4},
                 "inventory_type": {"type": "NON_EQUIP", "name": "Non-equippable"}, "sell_price": _sell(20000),
                 "requirements": {"skill": {"profession": {"name": "Blacksmithing", "id": 164}, "level": 300,
                                            "display_string": "Requires Blacksmithing (300)"},
                                  "ability": {"spell": {"name": "Master Axesmith", "id": 17041},
                                              "display_string": "Requires Master Axesmith"}},
                 "description": "Teaches you how to make an Arcanite Reaper.",
                 "recipe": {"item": {"name": "Arcanite Reaper", "stats": [_stat("Attack Power", 62, _G, True)]},
                            "reagents_display_string": "Arcanite Bar (20), Enchanted Leather (6), Dense Grinding Stone (2)"},
                 "is_subclass_hidden": True}}
TIP_FANG = {"name": "Blackfang", "level": 25,
            "preview_item": {
                "item_class": {"name": "Weapon", "id": 2}, "item_subclass": {"name": "Dagger", "id": 15},
                "inventory_type": {"type": "WEAPON", "name": "One-Hand"},
                "binding": {"type": "ON_EQUIP", "name": "Binds when equipped"}, "unique_equipped": "Unique",
                "weapon": {"damage": {"display_string": "17 - 32 Damage"}, "attack_speed": {"display_string": "Speed 1.50"},
                           "dps": {"display_string": "(16.3 damage per second)"}},
                "stats": [_stat("Shadow Resistance", 5, _G), _stat("Spirit", 2, _G, True)],
                "requirements": {"level": {"value": 20, "display_string": "Requires Level 20"}},
                "level": {"value": 25, "display_string": "Item Level 25"},
                "durability": {"value": 50, "display_string": "Durability 50 / 50"}}}
TIP_SHIELD = {"name": "Lambent Scale Shield", "level": 27,
              "preview_item": {
                  "item_class": {"name": "Armor", "id": 4}, "item_subclass": {"name": "Shield", "id": 6},
                  "inventory_type": {"type": "SHIELD", "name": "Off Hand"},
                  "armor": {"value": 528, "display": {"display_string": "528 Armor", "color": _W}},
                  "shield_block": {"value": 10, "display": {"display_string": "10 Block", "color": _W}}}}
TIP_BAG = {"name": "Woolen Bag", "level": 15, "is_equippable": True, "inventory_type": {"type": "BAG", "name": "Bag"},
           "preview_item": {"item_class": {"name": "Container", "id": 1}, "item_subclass": {"name": "Bag", "id": 0},
                            "inventory_type": {"type": "BAG", "name": "Bag"},
                            "container_slots": {"value": 8, "display_string": "8 Slot Bag"}}}
TIP_ARROW = {"name": "Feathered Arrow", "level": 35,
             "preview_item": {"item_class": {"name": "Projectile", "id": 6}, "item_subclass": {"name": "Arrow", "id": 2},
                              "inventory_type": {"type": "AMMO", "name": "Ammo"},
                              "weapon": {"damage": {"display_string": "Adds 9.5 damage per second"}},
                              "level": {"value": 35, "display_string": "Item Level 35"}}}
TIP_LINEN = {"name": "Linen Cloth", "level": 5,
             "preview_item": {"item_class": {"name": "Trade Goods", "id": 7}, "item_subclass": {"name": "Cloth", "id": 5},
                              "inventory_type": {"type": "NON_EQUIP", "name": "Non-equippable"},
                              "sell_price": _sell(13), "is_subclass_hidden": True,
                              "crafting_reagent": "Crafting Reagent"}}


def tooltip_tests():
    T = fetch_ah.tooltip_lines
    w = lambda t, *r: [t, "w"] + list(r)     # noqa: E731
    got = T(TIP_ROBE)
    check(got == [["Item Level 40", "y"], w("Binds when equipped"), w("Chest", "Cloth"), w("58 Armor"),
                  w("+6 Intellect"), w("+5 Spirit"), w("Durability 80 / 80"), w("Requires Level 35"),
                  ["Equip: Increases damage and healing done by magical spells and effects by up to 22.", "g"]],
          f"armor with stats: {got!r}")
    got = T(TIP_EDWARD)
    check(got == [["Item Level 62", "y"], w("Binds when equipped"), w("Unique"), w("One-Hand", "Mace"),
                  w("50 - 94 Damage", "Speed 1.60"), w("(45.0 damage per second)"), w("+13 Intellect"),
                  w("+10 Spirit"), w("Durability 105 / 105"), w("Requires Level 57"),
                  ["Chance on hit: Next spell cast within 4 sec will cast instantly.", "g"]], f"weapon: {got!r}")
    got = T(TIP_POTION)
    check(got == [w("Requires Level 12"), ["Use: Restores 280 to 360 health. (2 Min Cooldown)", "g"]],
          f"consumable with Use: {got!r}")
    got = T(TIP_CODEX)
    check(got == [w("Unique"), w("This Item Begins a Quest"), w("Classes: Warrior"), w("Requires Level 54"),
                  ['"The tome is magically sealed."', "y"]], f"quest item: {got!r}")
    got = T(TIP_BELT)
    check(got[-6:] == [["Battlegear of Valor (0/8)", "y"], ["(2) Set: +200 Armor.", "q"],
                       ["(4) Set: +40 Attack Power.", "q"],
                       ["(6) Set: Chance on melee attack to heal you for 88 to 132.", "q"],
                       ["(8) Set: +8 All Resistances.", "q"], ['"Worn by the champions of Valor."', "y"]]
          and got[:3] == [["Item Level 58", "y"], w("Binds when equipped"), w("Waist", "Plate")]
          and w("+4 Spirit") in got and "Belt of Valor" not in json.dumps(got), f"set and flavor text: {got!r}")
    got = T(TIP_PLANS)
    check(got == [w("Requires Blacksmithing (300)"), w("Requires Master Axesmith"),
                  ["Use: Teaches you how to make an Arcanite Reaper.", "g"],
                  w("Requires Arcanite Bar (20), Enchanted Leather (6), Dense Grinding Stone (2)")],
          f"recipe: its text is the Use line, then the reagents: {got!r}")
    got = T(TIP_FANG)
    check(got[0] == ["Item Level 25", "y"] and ["+5 Shadow Resistance", "g"] in got and ["+2 Spirit", "g"] in got,
          f"green stats keep their colour, preview item level: {got!r}")
    check(T(TIP_SHIELD) == [["Item Level 27", "y"], w("Off Hand", "Shield"), w("528 Armor"), w("10 Block")],
          f"shield block {T(TIP_SHIELD)!r}")
    check(T(TIP_BAG) == [w("8 Slot Bag")], f"a bag: its size, no item level {T(TIP_BAG)!r}")
    check(T(TIP_ARROW) == [["Item Level 35", "y"], w("Ammo", "Arrow"), w("Adds 9.5 damage per second")],
          f"ammo {T(TIP_ARROW)!r}")
    check(T(TIP_LINEN) == [["Crafting Reagent", "b"]], f"trade goods {T(TIP_LINEN)!r}")
    # no preview_item: the top-level fields
    got = T({"name": "Old Sword", "level": 20, "required_level": 15, "description": '"Sharp."',
             "inventory_type": {"type": "WEAPON", "name": "One-Hand"}, "item_subclass": {"name": "Sword"}})
    check(got == [["Item Level 20", "y"], w("One-Hand", "Sword"), w("Requires Level 15"), ['"Sharp."', "y"]],
          f"no preview_item {got!r}")
    # odd data never raises and never costs the other lines
    odd = {"preview_item": {"binding": "Binds", "stats": [None, 5, {"type": {"name": "Stamina"}, "value": 3},
                                                         {"display": {"display_string": "+1 Spirit",
                                                                      "color": {"r": "x"}}}],
                            "armor": {"display": "58 Armor"}, "spells": "Use: x",
                            "durability": {"display_string": "Durability 9 / 9"},
                            "description": "|cffffd100Gold|r line\r\nwith  breaks " + "z" * 500,
                            "requirements": ["Requires Level 3"], "weapon": {"damage": 7, "dps": {"display_string": 5.5}}}}
    got = T(odd)
    check(got[:4] == [w("5.5"), w("+3 Stamina"), w("+1 Spirit"), w("Durability 9 / 9")] and len(got) == 5
          and got[4][1] == "y" and got[4][0].startswith('"Gold line with breaks zzz') and got[4][0].endswith('z"')
          and len(got[4][0]) == fetch_ah.DETAILS_MAX_CHARS, f"odd data {got!r}")
    for bad in (None, 5, "x", [], {"preview_item": "x"}, {"preview_item": {"inventory_type": "CHEST"}}):
        check(fetch_ah.safe_tooltip_lines(bad) == [], f"nothing to show: [] ({bad!r})")
    many = {"preview_item": {"stats": [_stat("Stamina", k) for k in range(1, 80)]}}
    check(len(T(many)) == fetch_ah.DETAILS_MAX_LINES, "at most DETAILS_MAX_LINES lines")
    check(fetch_ah._tip_colour(_rgb(250, 30, 30)) == "r" and fetch_ah._tip_colour(_rgb(160, 160, 160)) == "q"
          and fetch_ah._tip_colour(_rgb(255, 210, 10)) == "y" and fetch_ah._tip_colour(None) == "w",
          "API colours to the nearest tooltip colour")
    raw = json.dumps([T(x) for x in (TIP_ROBE, TIP_EDWARD, TIP_POTION, TIP_CODEX, TIP_BELT, TIP_PLANS)])
    check(all(ord(ch) < 128 for ch in raw), "ASCII lines")


def details_unit_tests():
    """chunk files, merging, the backfill budget and order, failures and the hand-off from fetch_item"""
    # the lines ride along with the lookup that names an item, and are taken out before items.json is saved
    saved_api = fetch_ah.api
    tmp = tempfile.mkdtemp(prefix="fah_tipu_")
    try:
        def api(path, ns, tries=4):
            if path.startswith("/data/wow/media/item/"):
                return {"assets": []}, {}
            iid = int(path.rsplit("/", 1)[1])
            if iid == 1716 and ns == fetch_ah.STATIC_NS[1]:
                return dict(TIP_ROBE, item_class={"name": "Armor", "id": 4}), {}
            if iid == 929:
                return dict(TIP_POTION), {}
            if iid == 5:
                raise OSError("simulated failure")
            return None, {}
        fetch_ah.api = api
        iid, meta = fetch_ah.fetch_item(1716)
        check(meta["name"] == "Robe of the Magi" and meta[fetch_ah.TIP_KEY] == fetch_ah.tooltip_lines(TIP_ROBE),
              "fetch_item: the lines from the same response")
        _, fields = fetch_ah.fetch_details(929)
        check(fields[fetch_ah.TIP_KEY] == fetch_ah.tooltip_lines(TIP_POTION) and fields["ilvl"] == 22,
              "fetch_details: the lines from the same response")
        check(fetch_ah.fetch_details(4) == (4, {fetch_ah.TIP_KEY: []}) and fetch_ah.fetch_details(5) == (5, None),
              "fetch_details: unknown everywhere = no lines, a failed request = try again")
        check(fetch_ah.fetch_tip(929) == (929, fetch_ah.tooltip_lines(TIP_POTION)) and fetch_ah.fetch_tip(4) == (4, [])
              and fetch_ah.fetch_tip(5) == (5, None), "fetch_tip: lines, [] or None")
        items = {"1716": dict(meta), "929": {"name": "Healing Potion", "sell": 75}, "3": {"name": "C"}}
        items["929"].update(fields)
        tips = fetch_ah.take_tips(items)
        check(set(tips) == {1716, 929} and all(fetch_ah.TIP_KEY not in m for m in items.values())
              and items["1716"]["name"] == "Robe of the Magi", f"take_tips {sorted(tips)}")

        # chunk files: one per 1000 ids, merged, only the changed ones written
        out = os.path.join(tmp, "data")
        written = []
        real_save = fetch_ah.save

        def spy(path, obj):
            written.append(os.path.relpath(path, out).replace("\\", "/"))
            real_save(path, obj)
        fetch_ah.save = spy

        def no_fetch(i):
            raise AssertionError("looked up %s" % i)
        try:
            r = fetch_ah.update_details(out, {}, {5: [["A", "w"]], 999: [["B", "w"]], 1500: [["C", "g"]]}, {}, {},
                                        fetch=no_fetch)
            check(r == (3, 0, 0) and sorted(written) == ["details/0.json", "details/1.json"], f"two chunks {r} {written}")
            check(load_json(os.path.join(out, "details", "0.json")) == {"5": [["A", "w"]], "999": [["B", "w"]]}
                  and load_json(os.path.join(out, "details", "1.json")) == {"1500": [["C", "g"]]}, "chunk contents")
            check(open(os.path.join(out, "details", "0.json"), encoding="utf-8").read().startswith('{"5":'),
                  "ids in number order")
            written.clear()
            fetch_ah.update_details(out, {}, {7: [["D", "w"]], 1500: [["C", "g"]]}, {}, {}, fetch=no_fetch)
            check(written == ["details/0.json"] and load_json(os.path.join(out, "details", "0.json")) ==
                  {"5": [["A", "w"]], "7": [["D", "w"]], "999": [["B", "w"]]}, f"merged; unchanged chunk 1 left alone {written}")
            written.clear()
            fetch_ah.update_details(out, {}, {5: [["A2", "w"]]}, {}, {}, fetch=no_fetch)
            check(load_json(os.path.join(out, "details", "0.json"))["5"] == [["A2", "w"]], "a newer lookup replaces lines")
            written.clear()
            fetch_ah.update_details(out, {}, {}, {}, {}, fetch=no_fetch)
            check(written == [], "nothing new: nothing written")
        finally:
            fetch_ah.save = real_save

        # the backfill: named items the site lists, missing from every chunk, most gold traded a day first
        out = os.path.join(tmp, "bf")
        named = {str(i): {"name": "I%d" % i, "sell": 1} for i in (10, 11, 12, 13, 14, 15, 2001, 5000)}
        named["16"] = dict(fetch_ah.unknown_entry(1794936300))           # no name yet: the name retries come first
        latest = {"10": {"high": 100, "low": 90, "n": 1}, "11": {"high": 5000, "n": 3}, "12": {"high": 5000, "n": 9},
                  "13": {"high": 7, "n": 50}, "14": {"high": 0, "low": 300, "n": 2}, "15": {"high": 40, "n": 4},
                  "16": {"high": 99999, "n": 9}, "2001": {"high": 1, "n": 1}, "12001": {"high": 9000, "n": 1}}
        vols = {"10": 50, "11": 2, "12": 2, "13": 0, "14": 1, "15": 5, "16": 50, "2001": 0, "12001": 2}
        vinfo = {12001: (12, "12722", 1)}
        # gold a day: 10 -> 5000, 11 -> 10000, 12 -> 10000 (more listed than 11), 14 -> 300, 15 -> 200,
        # 13 and 2001 -> 0 (13 more listed); 5000 is not listed; 16 has no name; 12001 is a version of 12
        order = [12, 11, 10, 14, 15, 13, 2001]
        calls = []
        tries = Counter()
        lock = threading.Lock()

        def fake(i):
            with lock:
                calls.append(i)
                tries[i] += 1
            if i == 15 and tries[i] == 1:
                return i, None                                      # failed once: tried again next run
            if i == 13:
                return i, []                                        # Blizzard has nothing: never asked again
            return i, [["L%d" % i, "w"]]
        todo, miss = fetch_ah.details_todo(named, {}, latest, vols, vinfo, limit=99)
        check(todo == order and miss == 7, f"order {todo} ({miss} missing)")
        r = fetch_ah.update_details(out, named, {}, latest, vols, vinfo, fetch=fake, limit=2, threads=2)
        check(sorted(calls) == [11, 12] and r == (0, 2, 5), f"first run: the 2 most traded ({calls}, {r})")
        calls.clear()
        r = fetch_ah.update_details(out, named, {}, latest, vols, vinfo, fetch=fake, limit=3, threads=2)
        check(sorted(calls) == [10, 14, 15] and r == (0, 2, 3), f"second run: the next 3, one failed ({calls}, {r})")
        calls.clear()
        # the other collector works on the published files: it starts where this one stopped
        other = os.path.join(tmp, "bf_other")
        shutil.copytree(out, other)
        r = fetch_ah.update_details(other, named, {}, latest, vols, vinfo, fetch=fake, limit=400, threads=2)
        check(sorted(calls) == [13, 15, 2001] and r == (0, 3, 0), f"the rest, the failed one again ({calls}, {r})")
        ch = load_json(os.path.join(other, "details", "0.json"))
        check(ch == {"10": [["L10", "w"]], "11": [["L11", "w"]], "12": [["L12", "w"]], "13": [], "14": [["L14", "w"]],
                     "15": [["L15", "w"]]} and load_json(os.path.join(other, "details", "2.json")) == {"2001": [["L2001", "w"]]},
              f"backfilled chunks {ch}")
        calls.clear()
        r = fetch_ah.update_details(other, named, {}, latest, vols, vinfo, fetch=fake, limit=400, threads=2)
        check(calls == [] and r == (0, 0, 0), "all done: nothing looked up, [] is never asked again")
        check(fetch_ah.DETAILS_BACKFILL_PER_RUN == 400 and fetch_ah.DETAILS_CHUNK == 1000, "backfill limit, chunk size")
        # a broken chunk file is read as empty and rewritten
        with open(os.path.join(other, "details", "2.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        calls.clear()
        fetch_ah.update_details(other, named, {}, latest, vols, vinfo, fetch=fake, limit=400, threads=2)
        check(calls == [2001] and load_json(os.path.join(other, "details", "2.json")) == {"2001": [["L2001", "w"]]},
              f"broken chunk: looked up again ({calls})")
    finally:
        fetch_ah.api = saved_api
        shutil.rmtree(tmp, ignore_errors=True)


def details_budget_tests():
    """The backfill's time budget and failure breaker: a slow or failing Blizzard (429 / 5xx) can
    never hold the run, and the publish step after it, past the job's time limit. What the lookups
    did not reach is still missing from the chunks, so the next run goes on from there."""
    import io
    import contextlib
    tmp = tempfile.mkdtemp(prefix="fah_tipb_")
    saved_time, saved_api = fetch_ah.time, fetch_ah.api
    log = io.StringIO()

    class Clock:
        """stands in for the time module: only the lookups move it"""
        def __init__(self):
            self.now = 5000.0

        def time(self):
            return self.now

        def sleep(self, s):
            self.now += s
    try:
        check(fetch_ah.DETAILS_MAX_SECS == 120 and fetch_ah.DETAILS_MAX_FAILS == 10,
              "budget: 120 seconds, 10 failed lookups in a row")
        named = {str(i): {"name": "I%d" % i, "sell": 1} for i in range(1, 61)}
        latest = {str(i): {"high": 1000 - i, "n": 1} for i in range(1, 61)}
        vols = {str(i): 1 for i in range(1, 61)}
        order = fetch_ah.details_todo(named, {}, latest, vols, limit=400)[0]
        check(order == list(range(1, 61)), f"budget: the order {order[:5]}")
        calls = []
        lock = threading.Lock()

        # every lookup fails: none after the 10th failure in a row
        def failing(i):
            with lock:
                calls.append(i)
            return i, None
        with contextlib.redirect_stdout(log):
            r = fetch_ah.update_details(os.path.join(tmp, "a"), named, {}, latest, vols, fetch=failing, threads=1)
        check(calls == order[:10] and r == (0, 0, 60), f"breaker: 10 failed lookups, then none ({len(calls)} made, {r})")
        check("10 failed lookups in a row" in log.getvalue(), "breaker: said in the log")
        calls.clear()
        with contextlib.redirect_stdout(log):
            r = fetch_ah.update_details(os.path.join(tmp, "a"), named, {}, latest, vols, fetch=failing, threads=4)
        check(10 <= len(calls) <= 13 and r == (0, 0, 60), f"breaker on 4 threads: at most the 3 under way on top ({len(calls)})")

        # a lookup that works resets the count
        calls.clear()

        def flaky(i):
            with lock:
                calls.append(i)
            return (i, [["L%d" % i, "w"]]) if i % 10 == 0 else (i, None)
        r = fetch_ah.update_details(os.path.join(tmp, "b"), named, {}, latest, vols, fetch=flaky, threads=1)
        check(len(calls) == 60 and r == (0, 6, 54), f"9 failures then one that works: all looked up ({len(calls)}, {r})")

        # a slow Blizzard: out of time after DETAILS_MAX_SECS
        clock = Clock()
        fetch_ah.time = clock
        calls.clear()

        def slow(i):
            with lock:
                calls.append(i)
            clock.now += 30
            return i, [["L%d" % i, "w"]]
        out = os.path.join(tmp, "c")
        log.seek(0)
        log.truncate()
        with contextlib.redirect_stdout(log):
            r = fetch_ah.update_details(out, named, {5000: [["New", "g"]]}, latest, vols, fetch=slow, threads=1)
        check(calls == order[:4] and r == (1, 4, 56), f"out of time after 120 s: 4 lookups of 30 s ({calls}, {r})")
        check("out of time" in log.getvalue(), "out of time: said in the log")
        check(load_json(os.path.join(out, "details", "0.json")) == {str(i): [["L%d" % i, "w"]] for i in (1, 2, 3, 4)}
              and load_json(os.path.join(out, "details", "5.json")) == {"5000": [["New", "g"]]},
              "out of time: what was looked up and this run's own lines are written")
        calls.clear()
        with contextlib.redirect_stdout(log):
            r = fetch_ah.update_details(out, named, {}, latest, vols, fetch=slow, threads=1)
        check(calls == order[4:8] and r == (0, 4, 52), f"the next run goes on from there ({calls}, {r})")
        calls.clear()
        with contextlib.redirect_stdout(log):
            r = fetch_ah.update_details(os.path.join(tmp, "d"), named, {7000: [["T", "w"]]}, latest, vols,
                                        fetch=slow, threads=1, max_secs=0)
        check(calls == [] and r == (1, 0, 60) and load_json(os.path.join(tmp, "d", "details", "7.json")) == {"7000": [["T", "w"]]},
              f"no time at all: no lookups, this run's own lines still written ({calls}, {r})")

        # the real lookups: every namespace answers 429, the run stops after 10 items, not 60
        asked = []

        def api_429(path, ns, tries=4):
            with lock:
                asked.append(ns)
            raise OSError("simulated 429")
        fetch_ah.api = api_429
        with contextlib.redirect_stdout(log):
            r = fetch_ah.update_details(os.path.join(tmp, "e"), named, {}, latest, vols, threads=1)
        check(len(asked) == 10 * len(fetch_ah.STATIC_NS) and r == (0, 0, 60),
              f"Blizzard answering 429: 10 items asked in every namespace, then none ({len(asked)} requests)")
        # fetch_tip asks no further namespace once the run is over
        asked.clear()
        check(fetch_ah.fetch_tip(42, lambda: len(asked) >= 1) == (42, None) and asked == fetch_ah.STATIC_NS[:1],
              f"fetch_tip: stops between namespaces {asked}")
        asked.clear()
        check(fetch_ah.fetch_tip(42, lambda: True) == (42, None) and asked == [], "fetch_tip: asks nothing once over")
        asked.clear()
        check(fetch_ah.fetch_tip(42) == (42, None) and asked == fetch_ah.STATIC_NS, "fetch_tip: no stop, every namespace")
    finally:
        fetch_ah.time, fetch_ah.api = saved_time, saved_api
        shutil.rmtree(tmp, ignore_errors=True)


def check_details_dir(data_dir, label, mapping=None, items=None):
    """Every details/<n>.json: a {"<id>": lines} object whose ids belong in that file, each line
    [text, colour] or [text, colour, right] with a known colour; every named item the site lists
    (and every version's item) has an entry when `items` is given. Returns how many items have one."""
    d = os.path.join(data_dir, "details")
    have = {}
    for fn in sorted(os.listdir(d)):
        check(re.match(r"^\d+\.json$", fn), f"{label}: details file name {fn}")
        raw = load_json(os.path.join(d, fn))
        check(isinstance(raw, dict), f"{label}: {fn} is an object")
        n = int(fn[:-5])
        for k, lines in raw.items():
            check(re.match(r"^\d+$", k) and int(k) // 1000 == n, f"{label}: {k} is in {fn}")
            check(isinstance(lines, list), f"{label}: {k} lines are a list")
            for ln in lines:
                check(isinstance(ln, list) and len(ln) in (2, 3) and all(isinstance(x, str) and x for x in ln)
                      and ln[1] in ("w", "g", "y", "q", "r", "b"), f"{label}: {k} line {ln!r}")
            have[int(k)] = lines
    if mapping is not None and items is not None:
        for m in mapping:
            base = int(m.get("base") or m["id"])
            if fetch_ah.is_unknown(items.get(str(base))):
                continue
            check(base in have, f"{label}: {m['id']} ({m['name']}) has details")
    return len(have)


def details_pipeline_tests():
    """The real collector with item details and with them switched off, on the same snapshots:
    every other file comes out byte for byte the same, state.json keeps its format and items.json
    gets no lines. Then the backfill: a details/ folder lost (or written before this existed)
    comes back DETAILS_BACKFILL_PER_RUN items a run, most traded first, from a second collector
    working on the published files too, with no item looked up twice."""
    import io
    import contextlib
    tmp = tempfile.mkdtemp(prefix="fah_tipp_")
    names = ("api", "get_token", "time", "update_details", "safe_tooltip_lines", "DETAILS_BACKFILL_PER_RUN")
    saved = {k: getattr(fetch_ah, k) for k in names}
    old_env = os.environ.pop("GITHUB_OUTPUT", None)
    T0 = 1794934800 + 25 * 60
    M = 60
    log = io.StringIO()

    def make_fb():
        fb = FakeBlizzard()
        start_book(fb)
        start_gear_book(fb)
        fb.details[AXE] = {"preview_item": TIP_EDWARD["preview_item"], "level": 62}
        fb.details[LINEN] = {"preview_item": TIP_LINEN["preview_item"]}
        fb.details[CLEAVER]["preview_item"] = {"binding": {"name": "Binds when equipped"},
                                               "inventory_type": {"type": "TWOHWEAPON", "name": "Two-Hand"},
                                               "item_subclass": {"name": "Axe"},
                                               "weapon": {"damage": {"display_string": "30 - 46 Damage"},
                                                          "attack_speed": {"display_string": "Speed 3.10"}}}
        return fb

    def step(fb, k):
        """the same market moves for both collectors"""
        for aid in {1: (1, 2, 3, 41), 2: (11, 2000), 3: (4, 5)}.get(k, ()):
            fb.auctions.pop(aid, None)

    def run(out, fb, data_t):
        fb.now = data_t + 5 * M
        fb.modified = email.utils.formatdate(data_t, usegmt=True)
        fetch_ah.main(out)

    def files(out):
        got = {}
        for root, _, fns in os.walk(out):
            for fn in fns:
                rel = os.path.relpath(os.path.join(root, fn), out).replace("\\", "/")
                if not rel.startswith("details/"):
                    with open(os.path.join(root, fn), "rb") as fh:
                        got[rel] = fh.read()
        return got
    try:
        fetch_ah.get_token = lambda: "fake"
        with contextlib.redirect_stdout(log):
            a, b = os.path.join(tmp, "with"), os.path.join(tmp, "without")
            fa, fb_ = make_fb(), make_fb()
            for k in range(4):
                fetch_ah.api, fetch_ah.time = fa.api, fa
                fetch_ah.update_details, fetch_ah.safe_tooltip_lines = saved["update_details"], saved["safe_tooltip_lines"]
                step(fa, k)
                run(a, fa, T0 + k * 60 * M)
                fetch_ah.api, fetch_ah.time = fb_.api, fb_
                fetch_ah.update_details = lambda *x, **y: (0, 0, 0)
                fetch_ah.safe_tooltip_lines = lambda item: None
                step(fb_, k)
                run(b, fb_, T0 + k * 60 * M)
                fa_files, fb_files = files(a), files(b)
                diff = sorted(set(fa_files) ^ set(fb_files)) + sorted(f for f in fa_files if f in fb_files
                                                                      and fa_files[f] != fb_files[f])
                check(not diff and len(fa_files) > 20, f"snapshot {k}: other files identical with and without details {diff[:5]}")
                check(fa.lookups == fb_.lookups and sum(fa.lookups.values()) >= 9,
                      f"snapshot {k}: no item API request more than without details ({dict(fa.lookups)} vs {dict(fb_.lookups)})")
            fetch_ah.update_details, fetch_ah.safe_tooltip_lines = saved["update_details"], saved["safe_tooltip_lines"]
            fetch_ah.api, fetch_ah.time = fa.api, fa
            st = load_json(os.path.join(a, "state.json"))
            check(st["format"] == fetch_ah.DATA_FORMAT == 3 and set(st) == set(load_json(os.path.join(b, "state.json"))),
                  "state.json format unchanged")
            items = load_json(os.path.join(a, "items.json"))
            check(all(fetch_ah.TIP_KEY not in m for m in items.values()), "items.json carries no lines")
            ch = load_json(os.path.join(a, "details", "0.json"))
            check(ch[str(AXE)] == fetch_ah.tooltip_lines(dict(TIP_EDWARD, name="Double Axe"))
                  and ch[str(LINEN)] == [["Crafting Reagent", "b"]]
                  and ch[str(CLEAVER)][:2] == [["Item Level 30", "y"], ["Binds when equipped", "w"]]
                  and ch[str(ARROW)] == [] and str(UNNAMED) not in ch and str(NEVER) not in ch,
                  f"new items: lines from the lookup that named them {sorted(ch)}")
            mp = load_json(os.path.join(a, "mapping.json"))
            n = check_details_dir(a, "pipeline", mp, items)
            check(n == len(ch) == 7, f"every listed named item has details ({n})")
            vinfo = fetch_ah.registry_vids(fetch_ah.load_registry(st, os.path.join(a, "hist")))
            check(len(vinfo) == 3 and not any(int(k) in vinfo for k in ch), "versions have no lines of their own")

            def ref_order(out, have):
                """the backfill order worked out on its own: gold a day, sold, listed, id"""
                lat = load_json(os.path.join(out, "latest.json"))["data"]
                vol = load_json(os.path.join(out, "volumes.json"))["data"]
                rows = []
                for s, node in lat.items():
                    if int(s) in vinfo or int(s) in have or fetch_ah.is_unknown(items.get(s)):
                        continue
                    p, v = node.get("high") or node.get("low") or 0, vol.get(s, 0)
                    rows.append((-p * v, -v, -node.get("n", 0), int(s)))
                return [r[3] for r in sorted(rows)]

            # the details folder is lost: the backfill brings it back, 2 a run, most traded first
            shutil.rmtree(os.path.join(a, "details"))
            fetch_ah.DETAILS_BACKFILL_PER_RUN = 2
            fa.lookups.clear()
            run(a, fa, T0 + 4 * 60 * M)
            want = ref_order(a, ())
            got1 = sorted(int(k) for k in load_json(os.path.join(a, "details", "0.json")))
            looked = sorted(i for i, n in fa.lookups.items() if n)
            check(len(want) == 7 and got1 == sorted(want[:2]) and looked == got1,
                  f"run 1: the 2 most traded {got1} (looked up {looked}, wanted {want[:2]})")
            other = os.path.join(tmp, "other")
            shutil.copytree(a, other)                        # the PC collector, on the published files
            fa.lookups.clear()
            run(other, fa, T0 + 5 * 60 * M)
            want = ref_order(other, set(got1))
            got2 =sorted(int(k) for k in load_json(os.path.join(other, "details", "0.json")))
            looked = sorted(i for i, n in fa.lookups.items() if n)
            check(len(got2) == 4 and set(got1) < set(got2) and looked == sorted(set(got2) - set(got1))
                  == sorted(want[:2]), f"run 2 (the other collector): the next 2, nothing twice ({got2}, looked up {looked})")
            fetch_ah.DETAILS_BACKFILL_PER_RUN = 400
            fa.lookups.clear()
            run(other, fa, T0 + 6 * 60 * M)
            items2 = load_json(os.path.join(other, "items.json"))
            check_details_dir(other, "backfilled", load_json(os.path.join(other, "mapping.json")), items2)
            check(load_json(os.path.join(other, "details", "0.json")) == ch, "backfilled = written on first sight")
            fa.lookups.clear()
            run(other, fa, T0 + 7 * 60 * M)
            check(not any(fa.lookups.values()), "all there: nothing looked up")
        print("  item details: tooltip lines (armor, weapon, consumable, quest item, set and flavor text, recipe, "
              "bag, ammo, odd data), chunk files and merging, the backfill budget and order across two collectors, "
              "its time limit and failure breaker, and every other file identical with and without details")
    except Exception:
        sys.stdout.write(log.getvalue()[-3000:])
        raise
    finally:
        for k, v in saved.items():
            setattr(fetch_ah, k, v)
        if old_env is not None:
            os.environ["GITHUB_OUTPUT"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)


def details_all_tests(data_dir):
    tooltip_tests()
    details_unit_tests()
    details_budget_tests()
    details_pipeline_tests()
    d = os.path.join(data_dir, "details")
    if os.path.isdir(d):
        n = check_details_dir(data_dir, os.path.relpath(data_dir, REPO), load_json(os.path.join(data_dir, "mapping.json")),
                              load_json(os.path.join(data_dir, "items.json")))
        print(f"  {os.path.relpath(data_dir, REPO)}: details for {n} items, every listed named item has them")
    else:
        print(f"  {os.path.relpath(data_dir, REPO)}: no details folder (data from before item details)")


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "wow", "_localdata")
    print("ForeverAH_Data.lua check")
    ok = True
    for name, fn in (("generated data", lambda: run_checks(data_dir, os.path.relpath(data_dir, REPO), current=None)),
                     ("simulated sales", lambda: sim_sales_report(data_dir)),
                     ("edge cases", edge_cases), ("unit tests", unit_tests), ("pipeline", pipeline_tests),
                     ("versions", variant_pipeline_tests), ("deals", deal_pipeline_tests),
                     ("sold floor cutover", floor_cutover_tests),
                     ("item details", lambda: details_all_tests(data_dir))):
        try:
            fn()
            if name == "unit tests":
                print("  unit tests: market value, sold estimate safety, the sold floor (pulled and reposted "
                      "listings, no slack over it, 4000 random books), salesHours, names, data time, size guard, "
                      "flips, deals, addon flip picks, versions, suffix names, item details")
        except Fail as e:
            ok = False
            print(f"  FAIL ({name}): {e}")
    print("PASS" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

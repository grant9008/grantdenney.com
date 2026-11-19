"""
Tests for the Goblin Ledger addon's Core.lua and Recommend.lua.

Runs the real addon files in Lua 5.1 (the same Lua as WoW) through the
python package lupa, with the WoW functions they use replaced by small fakes.

Usage: python test_core.py
Exit code 0 when everything passes.

The price fixture below is hand-written test data, not real prices.
"""
import json
import math
import os
import re
import sys

from lupa import lua51

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rec_model  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
ADDON = os.path.join(REPO, "wow-addon", "GoblinLedger")
LOCALDATA = os.path.join(REPO, "goblin-ledger", "prices", "_localdata")

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def b36(n):
    assert n >= 0
    if n == 0:
        return "0"
    s = ""
    while n:
        n, r = divmod(n, 36)
        s = B36[r] + s
    return s


def enc_prices(vals):
    """Contract encoding: "~" null, base-36 delta from previous non-null, "" = 0."""
    out, prev = [], None
    for v in vals:
        if v is None:
            out.append("~")
            continue
        d = v - prev if prev is not None else v
        out.append("" if d == 0 else (b36(d) if d > 0 else "-" + b36(-d)))
        prev = v
    return ",".join(out)


def enc_sold(vals):
    return ",".join("" if not v else b36(v) for v in vals)


def enc_series(cheap, market, sold):
    return enc_prices(cheap) + ";" + enc_prices(market) + ";" + enc_sold(sold)


def lua_str(s):
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


FAILS = []
PASSES = [0]


def check(cond, label, detail=""):
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(label + (": " + str(detail) if detail != "" else ""))
        print("  FAIL", label, detail)


def eq(got, want, label):
    check(got == want, label, "got %r, want %r" % (got, want))


def lua_list(t, n=None):
    if t is None:
        return None
    if n is None:
        n = len(t)
    out = []
    for i in range(1, int(n) + 1):
        v = t[i]
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# Fixture (hand-written, fake prices)
# ---------------------------------------------------------------------------
HOUR_END = 1794916800          # 2026-11-17 12:00 UTC
DAY_END = 1794873600           # 2026-11-17 00:00 UTC
UPDATED = HOUR_END + 600       # snapshot taken 12:10

# Linen Cloth: 30 hourly points (more than 1D, fewer than 5D), 10 daily points.
LINEN_HC = [50, 48, 48, None, 47, 52, 45, 45, 44, None, None, 46, 47, 49, 40,
            41, 41, 43, 44, 45, 46, 45, 47, 47, 48, 46, 45, 44, 45, 45]
LINEN_HM = [55, 54, 54, None, 53, 56, 52, 52, 51, None, None, 52, 53, 54, 50,
            50, 51, 51, 52, 52, 53, 52, 53, 53, 54, 52, 51, 50, 47, 47]
LINEN_HS = [3, 0, 5, 0, 2, 1, 0, 4, 6, 0, 0, 2, 3, 5, 9, 4, 4, 3, 2, 6, 5, 4, 7, 8, 3, 6, 5, 4, 6, 5]
LINEN_DC = [60, 58, None, 55, 50, 48, 47, 46, 45, 45]
LINEN_DM = [65, 62, None, 60, 57, 55, 52, 50, 49, 47]
LINEN_DS = [100, 120, 0, 130, 90, 110, 140, 125, 118, 120]

# Black Lotus: typed by hand. 5 hourly points (much shorter than the grid), big prices.
#   cheap  230m8 = 3500000, -2s = -100, ~ = null, 1kw = +2048, "" = no change
#   market 230m8 = 3500000, 1kw = +2048, -1kw = -2048, ~ = null, 4 = +4
#   sold   "", 1, "", 2, 1
LOTUS_H = "230m8,-2s,~,1kw,;230m8,1kw,-1kw,~,4;,1,,2,1"
LOTUS_D = "230m8;230m8;3"
LOTUS_HC = [3500000, 3499900, False, 3501948, 3501948]
LOTUS_HM = [3500000, 3502048, 3500000, False, 3500004]
LOTUS_HS = [0, 1, 0, 2, 1]

# Runecloth: typed by hand. 6 hourly points, trailing nulls in market.
#   cheap  1z = 71, a = +10, -5, "" , ~, 3  -> 71 81 76 76 null 79
#   market 2s = 100, "", "", -1, ~, ~       -> 100 100 100 99 null null
#   sold   5 a "" 1 "" z                    -> 5 10 0 1 0 35
RUNE_H = "1z,a,-5,,~,3;2s,,,-1,~,~;5,a,,1,,z"
RUNE_D = "1z;2s;3"
RUNE_HC = [71, 81, 76, 76, False, 79]
RUNE_HM = [100, 100, 100, 99, False, False]
RUNE_HS = [5, 10, 0, 1, 0, 35]

ICE_NAME = "Tigule and Foror's Strawberry Ice Cream"
ICE_HC = [None, 900, 950, 1000, None, 1000, 980]
ICE_HM = [None, 1000, 1000, 1050, None, 1040, 1000]
ICE_HS = [0, 1, 0, 2, 0, 1, 0]

QUOTE_NAME = 'Test "Quoted" Back\\slash'

ITEMS = {
    2589: "Linen Cloth|45|47|900|120|46|13|%s|%s" % (
        enc_series(LINEN_HC, LINEN_HM, LINEN_HS), enc_series(LINEN_DC, LINEN_DM, LINEN_DS)),
    # vendor 2g: the market is 175x that, under the 200x "not reliable" line
    13468: "Black Lotus|3501948|3500004|4|3|3500500|20000|%s|%s" % (LOTUS_H, LOTUS_D),
    14047: "Runecloth|79|99|12|2|100|15|%s|%s" % (RUNE_H, RUNE_D),
    7228: "%s|980|1000|5|4|1010|25|%s|%s" % (
        ICE_NAME, enc_series(ICE_HC, ICE_HM, ICE_HS), enc_series([1000], [1030], [4])),
    99001: "%s|0|0|0|1|0|7|~;~;|~;~;" % QUOTE_NAME,
    4306: "Silk Cloth|0|0|0|0|0|25|;;",        # empty hourly series and no D field
}


def fixture_lua(items=None, sales_hours=None):
    """sales_hours None leaves the field out, like the data files made before it existed."""
    items = ITEMS if items is None else items
    lines = ["-- ForeverAH price data. Generated by grantdenney.com/wow. Do not edit.",
             "ForeverAH_Data = {",
             "  v = 1,",
             "  updated = %d," % UPDATED]
    if sales_hours is not None:
        lines += ["  fetched = %d," % (UPDATED + 300), "  salesHours = %d," % sales_hours]
    lines += ['  region = "us", house = 110, houseName = "Alliance Auction House", ruleset = "PvP",',
             "  hourEnd = %d," % HOUR_END,
             "  dayEnd = %d," % DAY_END,
             "  items = {"]
    for iid in sorted(items):
        lines.append("    [%d] = %s," % (iid, lua_str(items[iid])))
    lines += ["  },", "}", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fake WoW environment
# ---------------------------------------------------------------------------
MOCKS = r"""
MOCK = { now = 0, shift = false, prints = {}, frames = {}, postcalls = {}, bag = {}, faction = "Alliance",
         chartShown = {}, chartToggles = 0 }

function GetServerTime() return MOCK.now end
function IsShiftKeyDown() return MOCK.shift end
function UnitFactionGroup(unit) return MOCK.faction end
date = os.date
DEFAULT_CHAT_FRAME = { AddMessage = function(self, msg) table.insert(MOCK.prints, msg) end }
SlashCmdList = {}
C_Container = { GetContainerItemInfo = function(bag, slot) return MOCK.bag[bag .. ":" .. slot] end }
Enum = { TooltipDataType = { Item = 0 } }
if MOCK_PROCESSOR then
  TooltipDataProcessor = { AddTooltipPostCall = function(t, fn) MOCK.postcalls[t] = fn end }
end

function CreateFrame(kind, name, parent, template)
  local f = { events = {}, scripts = {} }
  function f:RegisterEvent(e) self.events[e] = true end
  function f:SetScript(s, fn) self.scripts[s] = fn end
  table.insert(MOCK.frames, f)
  return f
end

function MOCK.FireEvent(event, ...)
  for _, f in ipairs(MOCK.frames) do
    if f.events[event] and f.scripts.OnEvent then f.scripts.OnEvent(f, event, ...) end
  end
end

local function newTooltip(name)
  local tt = { name = name, lines = {}, scripts = {}, forbidden = false, shown = 0 }
  function tt:GetName() return self.name end
  function tt:IsForbidden() return self.forbidden end
  function tt:AddLine(text, r, g, b) table.insert(self.lines, { left = text, r = r, g = g, b = b }) end
  function tt:AddDoubleLine(l, rt, lr, lg, lb, rr, rg, rb)
    table.insert(self.lines, { left = l, right = rt, r = lr, g = lg, b = lb, rr = rr, rg = rg, rb = rb })
  end
  function tt:HookScript(s, fn)
    self.scripts[s] = self.scripts[s] or {}
    table.insert(self.scripts[s], fn)
  end
  function tt:HasScript(s) return true end
  function tt:Show() self.shown = self.shown + 1 end
  function tt:GetItem() return self.itemName, self.itemLink end
  function tt:Fire(s) for _, fn in ipairs(self.scripts[s] or {}) do fn(self) end end
  function tt:Clear() self.lines = {}; self:Fire("OnTooltipCleared") end
  return tt
end
MOCK.newTooltip = newTooltip
GameTooltip = newTooltip("GameTooltip")
ItemRefTooltip = newTooltip("ItemRefTooltip")
ShoppingTooltip1 = newTooltip("ShoppingTooltip1")
ShoppingTooltip2 = newTooltip("ShoppingTooltip2")
EmbeddedItemTooltip = newTooltip("EmbeddedItemTooltip")

-- Simulates the game showing an item tooltip through TooltipDataProcessor.
function MOCK.ShowItem(tt, id, info, clear)
  if clear ~= false then tt:Clear() end
  tt.processingInfo = info
  local fn = MOCK.postcalls[Enum.TooltipDataType.Item]
  if fn then fn(tt, { id = id }) end
  return tt.lines
end

NS = {}
-- Python callables are userdata in Lua; the addon expects real Lua functions.
function WRAP(f) return function(...) return f(...) end end
function LOAD(src, name)
  local f, e = loadstring(src, "@" .. name)
  if not f then error(e) end
  return f("GoblinLedger", NS)
end
"""


def new_env(data_src, processor=True):
    L = lua51.LuaRuntime(unpack_returned_tuples=True)
    L.globals().MOCK_PROCESSOR = processor
    L.execute(MOCKS)
    G = L.globals()
    G.LOAD(data_src, "Data.lua")
    G.LOAD(read(os.path.join(ADDON, "Core.lua")), "Core.lua")
    G.LOAD(read(os.path.join(ADDON, "Recommend.lua")), "Recommend.lua")
    return L, G, G.NS


def lua_hex(c):
    """Core.lua ns.Hex: "ff" + rrggbb, for |c colour codes."""
    return "ff" + "".join("%02x" % math.floor(max(0.0, min(1.0, v)) * 255 + 0.5) for v in c)


POS, NEG, MUTED = (0.25, 0.80, 0.45), (0.90, 0.33, 0.33), (0.55, 0.51, 0.45)
WARN = (1.00, 0.86, 0.25)   # Core.lua ns.COLORS.warn


def vs(pct_text):
    """The AH value's badge as Core.lua writes it: "+3% vs avg", yellow from 25% either
    way, grey under that (never green or red)."""
    p = abs(int(pct_text.rstrip("%")))
    return "|c%s%s vs avg|r" % (lua_hex(WARN if p >= 25 else MUTED), pct_text)


def old_hint(text):
    """Compact mode's short grey age note for prices over 80 minutes old ("2h" -> "  (2h old - /reload)")."""
    return "  |c%s(%s old - /reload)|r" % (lua_hex(MUTED), text)


def lines_of(tt_lines):
    out = []
    for i in range(1, len(tt_lines) + 1):
        ln = tt_lines[i]
        out.append((ln.left, ln.right) if ln.right is not None else (ln.left,))
    return out


def prints_since(G, start):
    p = G.MOCK.prints
    return [p[i] for i in range(start + 1, len(p) + 1)]


# ---------------------------------------------------------------------------
# Independent Python version of the recommendation spec
# ---------------------------------------------------------------------------
def py_recommend(ns, iid, tf, live=None):
    """rec_model.py (independent Python version of the advice rules) fed from
    the addon's own decoded windows. The decoder itself is checked separately."""
    item = ns.GetItem(iid)
    it = {"cheapest": item.cheapest, "market": item.market, "sold24": item.sold24, "vendor": item.vendor,
          "listed": item.listed, "auctions": item.auctions}

    def windows(key):
        s = ns.GetSeries(iid, key)
        return [v or None for v in lua_list(s.cheap, s.n)], [v or None for v in lua_list(s.market, s.n)]
    return rec_model.recommend(it, windows, tf, live, sales_hours=ns.db.salesHours)


# Review findings: hand-made markets, each with the answer worked out by hand.
FLAT = [10000] * 24
NEAR_CAP = 99999990000          # 9,999,999g: above what a 32-bit strtoul can read
RULE_ITEMS = {
    # perfectly flat: 1g cheapest, 1g 20s market every hour
    9001: "Flat Thing|10000|12000|500|100|12000|0|%s|%s" % (
        enc_series(FLAT, [12000] * 24, [5] * 24), enc_series([10000] * 5, [12000] * 5, [100] * 5)),
    # 23 hours at 350g, then the only listing is 2000g
    9002: "Silly Lotus|20000000|20000000|1|5|3500000|0|%s|%s" % (
        enc_series([3500000] * 23 + [20000000], [3500000] * 23 + [20000000], [1] * 24),
        enc_series([3500000] * 5, [3500000] * 5, [5] * 5)),
    # only 2 hours of history
    9003: "Sparse|10000|10400|3|4|0|0|%s|%s" % (
        enc_series([10000, 10000], [10400, 10400], [2, 2]), enc_series([10000], [10400], [4])),
    # listed 3 to 4 days ago, nothing in the last 24 hours
    9004: "Quiet Lately|0|0|0|0|0|0|%s|%s" % (
        enc_series([20000 + 10 * i for i in range(11)] + [None] * 72, [21000 + 10 * i for i in range(11)] + [None] * 72,
                   [1] * 11 + [0] * 72),
        enc_series([20000, 20050, None, None, None], [21000, 21050, None, None, None], [5, 6, 0, 0, 0])),
    # an AH price under what a vendor pays
    9005: "Vendor Trash|4000|4200|50|20|4200|5000|%s|%s" % (
        enc_series([4000 + (i % 3) * 10 for i in range(24)], [4200] * 24, [1] * 24),
        enc_series([4000] * 5, [4200] * 5, [20] * 5)),
    # a troll price far above the gold cap, then normal prices again
    9006: "Troll Price|500|600|10|10|600|0|%s|%s" % (
        enc_series([500, NEAR_CAP, 500, 510, 520, 500], [600, NEAR_CAP, 600, 610, 620, 600], [1] * 6),
        enc_series([500], [600], [5])),
    # only 2 hours of history, and a vendor pays 1g: no advice, but the vendor note stays
    9007: "Sparse Vendor|10000|10400|30|4|0|10000|%s|%s" % (
        enc_series([10000, 10000], [10400, 10400], [2, 2]), enc_series([10000], [10400], [4])),
    # the same with only 2 listings (10th field): Use List At only follows the cheapest
    9008: "Thin Vendor|10000|10400|30|4|0|10000|%s|%s|2" % (
        enc_series([10000, 10000], [10400, 10400], [2, 2]), enc_series([10000], [10400], [4])),
}


def test_recommend_rules():
    print("Recommend: review cases")
    L, G, ns = new_env(fixture_lua(RULE_ITEMS))
    G.MOCK.now = UPDATED + 100
    r = ns.Recommend(9001, "1D")
    # cheapest (1g) is under the market's 40th percentile (1g 20s), so it lists at the sell target
    eq((r.verdict, r.pos, r.buy, r.sell, r.listAt), ("Hold", 0.5, 10000, 12000, 12000), "flat market is a Hold, not Strong Buy")
    eq(lua_list(r.notes), [], "flat market: no buy-it note")
    r = ns.Recommend(9002, "1D")
    eq((r.sell, r.listAt), (3500000, 3500000), "a lone 2000g listing does not set the sell or list price")
    check("Cheapest now is far above recent prices" in lua_list(r.notes), "far-above note", lua_list(r.notes))
    r = ns.Recommend(9003, "1D")
    notes = lua_list(r.notes)
    check("Not enough price history yet" in notes and "Cheapest is below the buy target: buy it" not in notes,
          "sparse item: no buy-it note, says history is short", notes)
    eq(r.verdict, "Hold", "sparse item is a Hold")
    r = ns.Recommend(9004, "1D")
    eq((r.tf, r.askedTF), ("5D", "1D"), "empty 1D window falls back to 5D")
    eq((r.buy > 0, r.sell > 0), (True, True), "fallback window gives real targets")
    check("Too few prices in 1D: advice uses 5D" in lua_list(r.notes), "says which window was used", lua_list(r.notes))
    check("Spread does not beat the 5% AH cut" not in lua_list(r.notes) or r.margin <= 0, "no false spread note")
    r = ns.Recommend(9005, "1D")
    check("A vendor pays more (50s)" in lua_list(r.notes) and r.vendorBetter, "vendor note", lua_list(r.notes))
    # no advice yet: the vendor note still checks the price a player would post at
    r = ns.Recommend(9007, "1D")
    eq((r.advice, r.listAt, r.followFloor, r.postAt), (False, 0, 10400, 10400),
       "short history: no List At, the button would use the market price")
    check("A vendor pays more (1g)" in lua_list(r.notes) and r.vendorBetter, "short history: vendor note", lua_list(r.notes))
    r = ns.Recommend(9008, "1D")
    eq((r.advice, r.thinListings, r.postAt), (False, True, 9999), "2 listings: the button would follow the cheapest")
    check("A vendor pays more (1g)" in lua_list(r.notes) and r.vendorBetter, "2 listings: vendor note", lua_list(r.notes))
    for iid in (9007, 9008):
        py, lr = py_recommend(ns, iid, "1D"), ns.Recommend(iid, "1D")
        eq((lr.postAt, lr.followFloor, lua_list(lr.notes)), (py["postAt"], py["followFloor"], py["notes"]),
           "item %d matches the Python model" % iid)
    # live cheapest replaces the snapshot's
    r = ns.Recommend(9001, "1D", 16000)
    eq((r.cheapestNow, r.listAt, r.live), (16000, 15999, True), "live cheapest 1g 60s: undercut it, not the old 1g")
    r = ns.Recommend(9001, "1D", 50000)
    eq((r.listAt, r.far), (12000, True), "live cheapest far above history: list at the sell target")
    r = ns.Recommend(9001, "1D", 9000)
    eq(r.listAt, 12000, "live cheapest under the market's 40th percentile (a dump): wait at the sell target")
    # base 36 above 2^32 decodes exactly (WoW's tonumber(s, 36) saturates there)
    s = ns.GetSeries(9006, "1D")
    eq(lua_list(s.cheap, s.n), [500, NEAR_CAP, 500, 510, 520, 500], "prices above 2^32 copper decode exactly")
    eq(L.eval("tonumber('19xtf1tr', 36)"), 4294967295,
       "this Lua's tonumber(s, 36) really does saturate (so the chunked decode matters)")
    # every rule item against the Python model too
    for iid in sorted(RULE_ITEMS):
        for tf in ("1D", "5D", "1W", "1M", "1Y"):
            for live in (None, 16000):
                py = py_recommend(ns, iid, tf, live)
                r = ns.Recommend(iid, tf, live)
                for k in ("buy", "sell", "listAt", "margin", "verdict", "lo", "hi", "tf"):
                    eq(getattr(r, k), py.get(k), "rule item %d %s live=%s %s" % (iid, tf, live, k))
                eq(lua_list(r.notes), py["notes"], "rule item %d %s live=%s notes" % (iid, tf, live))


# A drop that lasts (the settled rule): when the last 6 hourly market prices,
# at least 4 of them, are all at or under 80% of the window's 70th percentile,
# the sell target is the highest of them. Each market is worked out by hand.
# Tender Wolf Steak (18045) is the real line from the 2026-11-18 6PM CST
# snapshot: 17s 74c on Tuesday evening, then 3s 95c to 4s 98c since Wednesday 8AM.
STEAK_LINE = ("Tender Wolf Steak|398|399|3131|150|534|300|cs,9,109,-g8,-3,-2h,-2r,,,-5a,,,,,,-34,-6q,,-1,-6,-1,-1,-2,"
              "-1,-1s,k,-1;cs,9,109,-fy,-c,-2,-3j,,,-6e,,,,,,-3m,-1i,-43,-17,-2,-5,-1,-2,,-1e,n,-j;,2z,46,3,4v,1p,7d,,,,,,,"
              ",,,3y,2q,4g,9z,4b,75,1s,3s,84,5k,46|cw,6x,-8r;cw,7o,-9h;2z,1xx,46|98")
# a day: 16 hours at the base price, then 8 evening hours at the evening price
EVENING_DAY = lambda base, eve: [base] * 16 + [eve] * 8  # noqa: E731


def settled_line(name, mkt, cheap=None, vendor=0, auctions=None, daily=None):
    """An item line from hourly market prices (None = nothing listed); the
    cheapest is 1 copper under the market unless given. daily: the daily
    market prices (5 days at today's market unless given)."""
    cheap = cheap or [None if m is None else m - 1 for m in mkt]
    now_c, now_m = cheap[-1] or 0, mkt[-1] or 0
    day = enc_series([d - 1 for d in daily], daily, [100] * len(daily)) if daily else \
        enc_series([now_c] * 5, [now_m] * 5, [100] * 5)
    line = "%s|%d|%d|500|100|%d|%d|%s|%s" % (
        name, now_c, now_m, now_m, vendor, enc_series(cheap, mkt, [5] * len(mkt)), day)
    return line + ("|%d" % auctions if auctions else "")


SETTLED_PHASE = 9400       # 9400 + p: the evening item of TC5 ending at hour p of its day
SETTLED_ITEMS = {
    18045: STEAK_LINE,
    # an evening premium of 20%: 120 for 8 hours a day, 100 the rest; now the 6 morning hours at 100
    9301: settled_line("Evening Fish", (EVENING_DAY(100, 120) * 3)[6:54]),
    # the same with 25%: 100 is exactly 80% of 125, so the cap applies (at or under)
    9302: settled_line("Evening Fish 25", (EVENING_DAY(100, 125) * 3)[6:54]),
    # a rise that lasts: 18 hours at 100, then 6 hours at 3s to 3s 40c, now 3s
    9303: settled_line("Rising Ore", [100] * 18 + [300, 340, 330, 320, 310, 300]),
    # up for 10 hours, then back at 100 for 6
    9304: settled_line("Fallen Ore", [100] * 8 + [300] * 10 + [100] * 6),
    # 18 hours at 1000, then 400 with gaps: only 3 prices in the last 6 hours
    9305: settled_line("Patchy Herb", [1000] * 18 + [400, None, None, None, 400, 400]),
    # the same with 4 prices in the last 6 hours
    9306: settled_line("Patchy Herb 4", [1000] * 18 + [400, None, None, 400, 400, 400]),
    # only 6 hours of history: 2 at 1000, 4 at 400
    9307: settled_line("Young Herb", [1000] * 2 + [400] * 4),
    # 9306's prices behind only 2 listings: no advice at all
    9308: settled_line("Thin Herb", [1000] * 18 + [400, None, None, 400, 400, 400], auctions=2),
    # a one-hour dip, a 5-hour dip and a 6-hour one from 1000 to 500
    9309: settled_line("Dipped Leather", [1000] * 23 + [500]),
    9310: settled_line("Dipped Leather 5", [1000] * 19 + [500] * 5),
    9311: settled_line("Dipped Leather 6", [1000] * 18 + [500] * 6),
    # one auction at 3g 39s, then one at 1g 68s 74c for 6 hours, then nothing listed for 2 hours (vendor
    # 1g 66s 40c): the shape of the real Monk's Staff of Strength
    9312: settled_line("Gone Staff", [33900] * 16 + [16874] * 6 + [None, None], vendor=16640),
    # 9311's hours, with 5 daily prices at 1000
    9313: settled_line("Daily Bar", [1000] * 18 + [500] * 6, daily=[1000] * 5),
    # a month of daily prices: 22 days at 1000, then 8 days at 700 (a quarter of the 30); the hours are 700
    9314: settled_line("Daily Slide", [700] * 24, daily=[1000] * 22 + [700] * 8),
    # a full week at 1000, then 700 for the last 24, 29, 41 or 42 hours
    9320: settled_line("Week Dip 24", [1000] * 144 + [700] * 24),
    9321: settled_line("Week Dip 29", [1000] * 139 + [700] * 29),
    9322: settled_line("Week Dip 41", [1000] * 127 + [700] * 41),
    9323: settled_line("Week Drop 42", [1000] * 126 + [700] * 42),
}
for _p in range(24):
    SETTLED_ITEMS[SETTLED_PHASE + _p] = settled_line("Evening Fish %d" % _p, (EVENING_DAY(100, 120) * 3)[_p + 1:_p + 49])


def test_settled_drop():
    print("Recommend: a drop that lasts (the settled sell target)")
    L, G, ns = new_env(fixture_lua(SETTLED_ITEMS))
    G.MOCK.now = UPDATED + 100

    def rec(iid, tf="1D", live=None):
        r = ns.Recommend(iid, tf, live)
        return r.sell, r.listAt, r.settled
    # TC1 the steak: the 1D 70th percentile is 8s 29c (Wednesday 1AM and the 6 hours bridged after it);
    # the last 6 hours are 4s 47c, 4s 45c, 4s 45c, 3s 95c, 4s 18c, 3s 99c: all under 80% of 8s 29c (6s 63c),
    # so the target is 4s 47c. List At is the target (the cheapest 3s 98c is under the window's 40th
    # percentile), never under 1 copper below the cheapest.
    eq(rec(18045, "1D"), (447, 447, True), "TC1 steak 1D: sell and List At 4s 47c (was 8s 29c)")
    # 5D and 1W hold all 27 hourly prices: a quarter is 7 hours, 4s 48c and the 6 above; the 70th
    # percentile of the 27 is 8s 29c too, 80% of it 6s 63c: the target is 4s 48c
    for tf in ("5D", "1W"):
        eq(rec(18045, tf), (448, 448, True), "TC1 steak %s: sell and List At 4s 48c (was 8s 29c)" % tf)
        eq(ns.Recommend(18045, tf).settledHours, 7, "TC1 steak %s: over the last 7 hours" % tf)
    r = ns.Recommend(18045, "1D")
    # the margin after the cut: 447 x 0.95 = 424 less the buy target 399 (capped at the market) = 25, not 388;
    # the cheapest sits at the bottom of the old range (Strong Buy), but that range is the old one: Hold.
    # The buy target is from the old range too: no "buy it" note.
    eq((r.buy, r.margin, r.verdict, r.score, r.settledHours), (399, 25, "Hold", 24, 6),
       "TC1 steak: margin 25c and a Hold, not Strong Buy")
    eq(lua_list(r.notes), ["Price fell and stayed down: the sell target uses the last 6 hours"], "TC1 steak: notes")
    P = ns.SellPrices(18045, "1D")
    eq((P.now, P.best), (397, 447), "TC1 steak: Sell now 3s 97c (unchanged), Suggested 4s 47c")
    V = ns.SellVerdict(ns.GetItem(18045), P)
    eq((V.left, V.right, V.unit), ("Auction it", "+26% vs vendor", 399), "TC1 steak: the verdict is unchanged")
    # the AH shows the steak back at 6s: the target follows the live listing (1 copper under it). List At
    # is not the 6 hours' high then, so no note. The 1D cheapest prices run 3s 79c to 11s 90c: 6s sits
    # at (600 - 379) / 811 = 0.27 of that range, a Buy score of 45 held to a Hold (24)
    eq(rec(18045, "1D", 600), (447, 599, True), "TC1 steak live 6s: List At 5s 99c")
    eq(ns.SellPrices(18045, "1D", 600).best, 599, "TC1 steak live 6s: Suggested 5s 99c")
    r = ns.Recommend(18045, "1D", 600)
    eq((r.verdict, r.score, lua_list(r.notes)), ("Hold", 24, []), "TC1 steak live 6s: a Hold and no note")
    # live 12s is over the old range's top (11s 90c): Strong Sell, as before the settled rule; selling
    # into a listing that high is right however far the price fell
    r = ns.Recommend(18045, "1D", 1200)
    eq((r.listAt, r.verdict, r.score, lua_list(r.notes)), (1199, "Strong Sell", -100, []),
       "TC1 steak live 12s: Strong Sell, not held to a Hold")
    # TC2 a real evening premium: 16 x 100 and 8 x 120 in the window, the 70th percentile is 120; the 6
    # morning hours at 100 are over 80% of it (96), so 120 is kept
    eq(rec(9301), (120, 120, False), "TC2 evening +20%: the 1s 20c evening price is kept in the morning")
    for p in range(24):
        r = ns.Recommend(SETTLED_PHASE + p, "1D")
        eq((r.sell, r.settled), (120, False), "TC2 evening +20%% at hour %d of the day: sell 120, no cap" % p)
    # 25%: the 70th percentile is 125 and 100 is exactly 80% of it: the cap applies
    eq(rec(9302), (100, 100, True), "TC2 evening +25%: at exactly 80% the cap applies")
    # TC3 a rise that lasts: the 70th percentile is 100, the market floor lifts it to today's 300 and the
    # last 6 hours (up to 340) are far over 80%: no cap, and no chasing the 340 peak either
    for tf in ("1D", "5D", "1W"):
        eq(rec(9303, tf)[0::2], (300, False), "TC3 rise %s: sell follows the market up to 300" % tf)
    eq(ns.SellPrices(9303, "1D").best, 300, "TC3 rise: Suggested 300")
    # up for 10 hours (the 70th percentile 300), back at 100 for 6: the target settles at 100
    eq(rec(9304), (100, 100, True), "TC3 fallen back: sell 100, not the 300 of the rise")
    eq(rec(9304, "1D", 250), (100, 249, True), "TC3 fallen back, live 2s 50c: List At follows the listing")
    # TC4 thin data: 3 prices in the last 6 hours are not enough (4 needed): sell stays at the 70th
    # percentile of 18 x 1000 and 3 x 400 (1000); with 4 prices the target is 400
    eq(rec(9305), (1000, 1000, False), "TC4 3 prices in 6 hours: no cap")
    eq(rec(9306), (400, 400, True), "TC4 4 prices in 6 hours: sell 400")
    # 6 hours of history: the last 6 still hold the 1000s (70th percentile 700): no cap
    eq(rec(9307), (700, 700, False), "TC4 6 hours of history: no cap")
    r = ns.Recommend(9308, "1D")
    eq((r.advice, r.sell, r.settled, r.postAt), (False, 0, False, 398), "TC4 2 listings: no advice, no cap")
    check(not any("stayed down" in n for n in lua_list(r.notes)), "TC4 2 listings: no settled note", lua_list(r.notes))
    # TC5 dips: one hour or 5 hours at 500 leave a 1000 in the last 6 hours: no cap; 6 hours: 500
    eq(rec(9309), (1000, 1000, False), "TC5 a one-hour dip does not drag the target down")
    eq(rec(9310), (1000, 1000, False), "TC5 a 5-hour dip does not either")
    eq(rec(9311), (500, 500, True), "TC5 6 hours at 500: the target is 500")
    # TC6 a patient window waits a quarter of its hours: a full 5D 30, a full 1W 42. A week at 1000, then
    # 700 (the cheapest 699): the 5D and 1W 70th percentiles stay 1000, 80% of it is 800.
    # 24 hours at 700: the last 30 and 42 hours still hold a 1000, so the patient target is kept: 1W buy
    # 700 (the 20th percentile 999, held to the market), margin 950 - 700 = 250 (36%), and 6s 99c is the
    # bottom of the week's range: Strong Buy, the dip the sidebar's price-history ideas look for
    r = ns.Recommend(9320, "1W")
    eq((r.sell, r.buy, r.margin, r.verdict, r.settled), (1000, 700, 250, "Strong Buy", False),
       "TC6 a 24-hour dip in a steady week: 1W sell 1000 and Strong Buy")
    eq(rec(9320, "5D"), (1000, 1000, False), "TC6 a 24-hour dip: 5D sell 1000")
    eq(rec(9321, "5D"), (1000, 1000, False), "TC6 a 29-hour dip: 5D sell 1000 (one 1000 in its last 30 hours)")
    eq(rec(9322, "5D"), (700, 700, True), "TC6 41 hours at 700: 5D settles at 700 after 30 hours")
    eq(ns.Recommend(9322, "5D").settledHours, 30, "TC6 41 hours: 5D over the last 30 hours")
    eq(rec(9322, "1W"), (1000, 1000, False), "TC6 41 hours at 700: 1W still 1000 (one 1000 in its last 42)")
    r = ns.Recommend(9323, "1W")
    eq((r.sell, r.listAt, r.settled, r.settledHours), (700, 700, True, 42), "TC6 42 hours at 700: 1W settles at 700")
    eq(lua_list(r.notes), ["Price fell and stayed down: the sell target uses the last 42 hours",
                           "Spread does not beat the 5% AH cut"], "TC6 42 hours: 1W notes")
    # TC7 the daily windows never cap: 1M and 1Y use the 5 daily prices at 1000, so sell 1000 (and List
    # At 1000: the cheapest 4s 99c is under the window's 40th percentile) though the last 6 hours are 500
    for tf in ("1M", "1Y"):
        eq(rec(9313, tf), (1000, 1000, False), "TC7 daily %s: no cap" % tf)
    eq(rec(9313, "1D"), (500, 500, True), "TC7 the same item on 1D: 500")
    # 8 of 30 days at 700 after 22 at 1000: the 1M 70th percentile is 1000 and stays the target (no cap
    # on a daily window, though a quarter of it is down)
    for tf in ("1M", "1Y"):
        eq(rec(9314, tf), (1000, 1000, False), "TC7 a month that slid for 8 days, %s: sell 1000" % tf)
    # TC8 nothing listed now: no cheaper listing holds the old price back, so no cap. The 70th percentile
    # of 16 x 3g 39s and 6 x 1g 68s 74c is 3g 39s; the verdict judges that Suggested price: maybe, as
    # nothing is listed (with the cap it was 1g 68s 74c and "Vendor it +3% vs AH")
    r = ns.Recommend(9312, "1D")
    eq((r.sell, r.settled), (33900, False), "TC8 nothing listed now: sell 3g 39s, no cap")
    eq(lua_list(r.notes), ["No listings right now"], "TC8 nothing listed now: notes")
    P = ns.SellPrices(9312, "1D")
    eq((P.now, P.best), (None, 33900), "TC8 nothing listed now: no Sell now, Suggested 3g 39s")
    V = ns.SellVerdict(ns.GetItem(9312), P)
    eq((V.left, V.right), ("Maybe auction", "none listed now"), "TC8 nothing listed now: the verdict")
    # every settled item against the Python model, on every window, with and without a live listing
    for iid in sorted(SETTLED_ITEMS):
        for tf in ("1D", "5D", "1W", "1M", "1Y"):
            for live in (None, 600, 1200):
                py = py_recommend(ns, iid, tf, live)
                r = ns.Recommend(iid, tf, live)
                for k in ("buy", "sell", "listAt", "postAt", "margin", "verdict", "score", "settled", "settledHours",
                          "tf", "advice"):
                    eq(getattr(r, k), py.get(k), "settled item %d %s live=%s %s" % (iid, tf, live, k))
                eq(lua_list(r.notes), py["notes"], "settled item %d %s live=%s notes" % (iid, tf, live))
                sp = rec_model.sell_prices(py, live, False, ns.GetItem(iid).vendor)
                P = ns.SellPrices(iid, tf, live)
                eq((P.now, P.best), (sp["now"], sp["best"]), "settled item %d %s live=%s SellPrices" % (iid, tf, live))
    # the steak's tooltip: Sell now 3s 97c and the verdict as before, Suggested price 4s 47c (was 8s 29c);
    # the 3-day average 5s 34c, the AH value 3s 99c is 25% under it
    L, G, ns = font_env(items=SETTLED_ITEMS)
    tc(G, G.GameTooltip, 18045, [H(), (("Auction it", "+26% vs vendor"), "pos", "pos"), AHV("3s 99c", "-25%"),
                                 ROW("3-day average", "5s 34c"), ROW("Sell now", "3s 97c"), ROW("Suggested price", "4s 47c")],
       True, "TC1 steak tooltip")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_files():
    print("files")
    want_toc = ("## Interface: 16001\r\n## Title: Goblin Ledger\r\n"
                "## Notes: Auction house prices, charts and price advice. Price data from Blizzard Entertainment.\r\n"
                "## X-Credits: Price data from Blizzard Entertainment\r\n"
                "## X-Website: https://grantdenney.com/goblin-ledger/\r\n"
                "## SavedVariables: ForeverAH_Settings\r\n## Version: X.Y.Z\r\n"
                "Data.lua\r\nLatest.lua\r\nCore.lua\r\nRecommend.lua\r\nChart.lua\r\nAH.lua\r\nSidebar.lua\r\nBags.lua\r\nEnchants.lua\r\n"
                "Consumes.lua\r\n")
    # Git may check these out with either line ending; WoW reads both.
    def text(name):
        with open(os.path.join(ADDON, name), "rb") as fh:
            return fh.read().decode("ascii").replace("\r\n", "\n")
    # The version changes with every release (make_release.ps1 checks that the
    # two TOCs agree): any X.Y.Z, the rest exactly as here.
    for name in ("GoblinLedger.toc", "GoblinLedger_Forever.toc"):
        got = text(name)
        check(re.search(r"(?m)^## Version: \d{1,5}\.\d{1,5}\.\d{1,5}$", got), name + " has a ## Version: X.Y.Z line")
        eq(re.sub(r"(?m)^## Version: \d{1,5}\.\d{1,5}\.\d{1,5}$", "## Version: X.Y.Z", got),
           want_toc.replace("\r\n", "\n"), name + " content")
    if True:
        eq(text("Data.lua"),
           "-- Goblin Ledger price data placeholder. Run Install-Goblin-Ledger.bat.\n"
           "ForeverAH_Data = ForeverAH_Data or { v = 1, updated = 0, items = {} }",
           "Data.lua placeholder")
    eq(text("Latest.lua"),
       "-- Goblin Ledger: the newest version the updater found, and the one it installed.\n"
       "-- Written by update_prices.ps1 (the \"Goblin Ledger Prices\" task and Update-Goblin-Ledger.bat). Do not edit.\n"
       "GoblinLedger_Latest = {}\n",
       "Latest.lua placeholder (the Windows updater rewrites it)")
    for name in ("Core.lua", "Recommend.lua", "Data.lua", "Latest.lua", "GoblinLedger.toc", "GoblinLedger_Forever.toc"):
        with open(os.path.join(ADDON, name), "rb") as fh:
            raw = fh.read()
        check(b"\xe2\x80\x94" not in raw and b"\xe2\x80\x93" not in raw, name + " has no long dashes")
        check(all(b < 128 for b in raw), name + " is plain ASCII")
    for name in ("Core.lua", "Recommend.lua"):
        check(read(os.path.join(ADDON, name)).startswith("local ADDON_NAME, ns = ..."),
              name + " starts with the shared header")


def test_lazy_and_items(G, ns):
    print("lazy decode, GetItem, FindItemID")
    eq(ns.stats["items"], 0, "nothing decoded at load (items)")
    eq(ns.stats.series, 0, "nothing decoded at load (series)")
    eq(ns.db.updated, UPDATED, "ns.db is ForeverAH_Data")
    check(G.rawequal(ns.db, G.ForeverAH_Data), "ns.db is the same table as ForeverAH_Data")
    eq(ns.ItemCount(), len(ITEMS), "item count")

    it = ns.GetItem(2589)
    eq((it.id, it.name, it.cheapest, it.market, it.listed, it.sold24, it.avg7, it.vendor),
       (2589, "Linen Cloth", 45, 47, 900, 120, 46, 13), "GetItem Linen Cloth fields")
    eq(ns.stats["items"], 1, "one item decoded")
    eq(ns.stats.series, 0, "GetItem does not decode series")
    check(G.rawequal(ns.GetItem(2589), it), "GetItem is cached")
    eq(ns.GetItem("2589").name, "Linen Cloth", "GetItem accepts a string id")
    eq(ns.GetItem(99999), None, "GetItem unknown -> nil")
    eq(ns.GetItem(None), None, "GetItem(nil) -> nil")
    eq(ns.GetItem(13468).cheapest, 3501948, "big price parsed")
    eq(ns.GetItem(7228).name, ICE_NAME, "apostrophe name")
    eq(ns.GetItem(99001).name, 'Test "Quoted" Back\\slash', "escaped quote and backslash name")

    eq(ns.FindItemID("Linen Cloth"), 2589, "FindItemID exact")
    eq(ns.FindItemID("  lInEn ClOtH "), 2589, "FindItemID case-insensitive and trimmed")
    eq(ns.FindItemID("tigule and foror's strawberry ice cream"), 7228, "FindItemID apostrophe")
    eq(ns.FindItemID("Linen"), None, "FindItemID is exact, not partial")
    eq(ns.FindItemID(""), None, "FindItemID empty")
    eq(ns.stats["items"], 4, "FindItemID index does not decode items")


def test_series(G, ns):
    print("GetSeries")
    before = ns.stats.series
    s = ns.GetSeries(2589, "1D")
    eq(ns.stats.series, before + 1, "1D decodes hourly once")
    eq((s.n, s.step, s.tf), (24, 3600, "1D"), "1D size/step")
    eq(lua_list(s.cheap, s.n), [v if v is not None else False for v in LINEN_HC[-24:]], "1D cheap")
    eq(lua_list(s.market, s.n), [v if v is not None else False for v in LINEN_HM[-24:]], "1D market")
    eq(lua_list(s.sold, s.n), LINEN_HS[-24:], "1D sold")
    eq(s.times[24], HOUR_END, "1D last time is hourEnd")
    eq(s.times[1], HOUR_END - 23 * 3600, "1D first time")
    check(G.rawequal(ns.GetSeries(2589, "1D"), s), "1D window cached")

    s5 = ns.GetSeries(2589, "5D")
    eq(ns.stats.series, before + 1, "5D reuses the decoded hourly series")
    eq(s5.n, 30, "5D on a 30-hour item has 30 points (no padding)")
    eq(s5.times[1], HOUR_END - 29 * 3600, "5D first time right-aligned")
    eq(lua_list(s5.cheap, 30), [v if v is not None else False for v in LINEN_HC], "5D cheap all")
    eq(lua_list(s5.market, 30), [v if v is not None else False for v in LINEN_HM], "5D market all (nulls)")
    eq(ns.GetSeries(2589, "1W").n, 30, "1W on a 30-hour item")
    eq(ns.GetSeries(2589, "1d").tf, "1D", "lower-case timeframe accepted")
    eq(ns.GetSeries(2589, "nonsense").tf, "1D", "unknown timeframe falls back to 1D")

    m = ns.GetSeries(2589, "1M")
    eq(ns.stats.series, before + 2, "1M decodes the daily series")
    eq((m.n, m.step), (10, 86400), "1M size/step")
    eq(m.times[10], DAY_END, "1M last time is dayEnd")
    eq(m.times[1], DAY_END - 9 * 86400, "1M first time")
    eq(lua_list(m.cheap, 10), [v if v is not None else False for v in LINEN_DC], "1M cheap")
    eq(lua_list(m.sold, 10), LINEN_DS, "1M sold")
    eq(ns.GetSeries(2589, "1Y").n, 10, "1Y on a 10-day item")

    bl = ns.GetSeries(13468, "1W")
    eq(bl.n, 5, "Black Lotus 1W shorter than grid")
    eq(lua_list(bl.cheap, 5), LOTUS_HC, "Black Lotus cheap (big values, negative delta, null, zero delta)")
    eq(lua_list(bl.market, 5), LOTUS_HM, "Black Lotus market")
    eq(lua_list(bl.sold, 5), LOTUS_HS, "Black Lotus sold")
    eq(bl.times[5], HOUR_END, "Black Lotus right-aligned end")
    eq(bl.times[1], HOUR_END - 4 * 3600, "Black Lotus right-aligned start")
    bd = ns.GetSeries(13468, "1M")
    eq((bd.n, bd.cheap[1], bd.market[1], bd.sold[1]), (1, 3500000, 3500000, 3), "Black Lotus daily one point")

    rc = ns.GetSeries(14047, "1D")
    eq(rc.n, 6, "Runecloth 6 points")
    eq(lua_list(rc.cheap, 6), RUNE_HC, "Runecloth cheap (delta after null uses last non-null)")
    eq(lua_list(rc.market, 6), RUNE_HM, "Runecloth market (trailing nulls)")
    eq(lua_list(rc.sold, 6), RUNE_HS, "Runecloth sold")

    ic = ns.GetSeries(7228, "5D")
    eq(lua_list(ic.cheap, ic.n), [v if v is not None else False for v in ICE_HC], "Ice cream leading null")

    q = ns.GetSeries(99001, "1D")
    eq((q.n, q.cheap[1], q.market[1], q.sold[1], q.times[1]), (1, False, False, 0, HOUR_END),
       "all-null series '~;~;' is one empty point")
    sk = ns.GetSeries(4306, "1D")
    eq(sk.n, 0, "';;' is an empty series")
    eq(ns.GetSeries(4306, "1M").n, 0, "missing D field is an empty series")
    eq(ns.GetSeries(99999, "1D"), None, "unknown item -> nil")

    # Decoding check against Python's own base-36 reading of the hand-typed tokens.
    eq((int("230m8", 36), int("2s", 36), int("1kw", 36), int("1z", 36)), (3500000, 100, 2048, 71),
       "hand-typed tokens mean what the fixture comments say")


def test_money(G, ns):
    print("MoneyText / MoneyIcons / AgeText")
    cases = [(0, "0c"), (5, "5c"), (100, "1s"), (123405, "12g 34s 5c"), (120005, "12g 5c"),
             (-123405, "-12g 34s 5c"), (3500000, "350g"), (3501948, "350g 19s"),
             (1000001, "100g"), (1000150, "100g 1s"), (999999, "99g 99s 99c"),
             (1000000, "100g"), (-5, "-5c"), (10000, "1g"), (-0.2, "0c"), (12345678, "1234g 56s")]
    for c, want in cases:
        eq(ns.MoneyText(c), want, "MoneyText(%r)" % c)
    eq(ns.MoneyIcons(123405), "12g 34s 5c", "MoneyIcons falls back to MoneyText")
    G.GetCoinTextureString = G.loadstring("return function(c) return '[' .. c .. ']' end")()
    eq(ns.MoneyIcons(123405), "[123405]", "MoneyIcons uses GetCoinTextureString")
    eq(ns.MoneyIcons(3501948), "[3501900]", "MoneyIcons drops copper above 100g")
    eq(ns.MoneyIcons(-500), "-[500]", "MoneyIcons negative")
    G.GetCoinTextureString = None
    G.C_CurrencyInfo = G.loadstring("return { GetCoinTextureString = function(c) return '<' .. c .. '>' end }")()
    eq(ns.MoneyIcons(500), "<500>", "MoneyIcons uses C_CurrencyInfo.GetCoinTextureString")
    G.C_CurrencyInfo = None
    eq(ns.Num(1234567), "1,234,567", "Num thousands")
    eq(ns.Num(450), "450", "Num small")
    eq(ns.AgeText(30), "just now", "AgeText seconds")
    eq(ns.AgeText(25 * 60), "25m ago", "AgeText minutes")
    eq(ns.AgeText(2 * 3600 + 100), "2h ago", "AgeText hours")
    eq(ns.AgeText(3 * 86400), "3d ago", "AgeText days")


def test_recommend(G, ns):
    print("Recommend")
    # Hand-checked Black Lotus 1D numbers (see fixture comments).
    r = ns.Recommend(13468, "1D")
    # lo/hi come from the cheapest prices only (3499900, 3500000, 3501948, 3501948).
    # listAt: the sell target (3500208) is under the cheapest listing, so it is
    # raised to 1c under it (3501947) instead of selling for less than needed.
    eq((r.buy, r.sell, r.listAt, r.margin, r.verdict, r.lo, r.hi, r.cheapestNow, r.marketNow, r.sold24),
       (3499960, 3500208, 3501947, -174763, "Hold", 3499900, 3501948, 3501948, 3500004, 3),
       "Black Lotus 1D hand-checked")
    # The 2048c range is under 10% of the median (3500974), so it is widened to
    # 350097.4c around its middle (3500924): pos = (3501948 - 3325875.3) / 350097.4.
    want_pos = (3501948 - (3500924 - 350097.4 / 2)) / 350097.4
    check(abs(r.pos - want_pos) < 1e-9, "Black Lotus pos (narrow range widened)", r.pos)
    check(abs(r.score - (0.5 - want_pos) * 200) < 1e-9 and r.verdict == "Hold", "a 0.06% wiggle is a Hold, not an extreme")
    eq(lua_list(r.notes), ["Spread does not beat the 5% AH cut"], "Black Lotus notes")

    # Every item and timeframe against the independent Python version of the rules.
    for iid in sorted(ITEMS):
        for tf in ("1D", "5D", "1W", "1M", "1Y"):
            for live in (None, 60, 3600000):
                py = py_recommend(ns, iid, tf, live)
                r = ns.Recommend(iid, tf, live)
                tag = "Recommend %d %s live=%s" % (iid, tf, live)
                for k in ("buy", "sell", "listAt", "margin", "verdict", "lo", "hi", "cheapestNow", "tf"):
                    eq(getattr(r, k), py.get(k), "%s %s" % (tag, k))
                check(abs(r.score - py["score"]) < 1e-9, tag + " score", (r.score, py["score"]))
                check(abs(r.pos - py["pos"]) < 1e-9, tag + " pos", (r.pos, py["pos"]))
                eq(lua_list(r.notes), py["notes"], tag + " notes")
                check(abs(r.marginPct - py["marginPct"]) < 1e-12, tag + " marginPct")

    # Linen 1D: cheapest 45 is at the bottom of the window, so a buy signal.
    r = ns.Recommend(2589)
    eq(r.tf, "1D", "Recommend defaults to 1D")
    print("    Linen 1D:", r.verdict, "buy", r.buy, "sell", r.sell, "listAt", r.listAt,
          "margin", r.margin, "notes", lua_list(r.notes))
    # Nothing listed: neutral, and the right notes.
    r = ns.Recommend(99001, "1D")
    eq((r.verdict, r.pos, r.buy, r.sell, r.marginPct), ("Hold", 0.5, 0, 0, 0), "no listings item")
    eq(lua_list(r.notes), ["Thin market", "No listings right now", "Not enough price history yet"],
       "no listings notes (no spread note when there is no buy or sell price)")
    r = ns.Recommend(14047, "1D")
    check("Thin market" in lua_list(r.notes), "Runecloth thin market note")
    eq(ns.Recommend(99999, "1D"), None, "Recommend unknown -> nil")

    # Percentile helper matches PocketGE's interpolation.
    t = G.loadstring("return {10, 20, 30, 40, 50}")()
    eq(ns.Percentile(t, 0.2), 18, "percentile 20 of 10..50")
    eq(ns.Percentile(t, 0.7), 38, "percentile 70 of 10..50")
    eq(ns.Percentile(G.loadstring("return {}")(), 0.5), None, "percentile of nothing")


def test_tooltip(G, ns):
    print("tooltip")
    M = G.MOCK
    tt = G.GameTooltip
    eq(ns.tooltipMode, "processor", "uses TooltipDataProcessor")
    M.now = UPDATED + 2 * 3600 + 100
    M.bag["0:1"] = G.loadstring("return { stackCount = 20 }")()
    bag_info = G.loadstring("return { getterName = 'GetBagItem', getterArgs = { 0, 1 } }")()

    got = lines_of(M.ShowItem(tt, 2589, bag_info))
    # AH value: the market (47c), 2% over the 7-day average (46c: 1c over is 2.17%,
    # rounded down). Sell now: 1c under the cheapest (44c). Suggested price: the 1D sell
    # target. The verdict judges List At (52c) capped at the AH value: 47c x 95 = 4,465
    # against a vendor's 13c x 100 = 1,300, 3.4x. This stack of 20 at that price after
    # the 5% cut: 47c x 20 x 95 / 100 = 8s 93c, in white.
    up2 = vs("+2%")
    eq(got, [("Goblin Ledger", "2h ago - /reload"), ("Auction it", "3.4x vendor"), ("AH value", "47c  " + up2),
             ("7-day average", "46c"), ("Sell now", "44c"), ("Suggested price", "52c"),
             ("This stack (20), after AH cut", "8s 93c")], "full tooltip, known item")
    sp = rec_model.sell_prices(py_recommend(ns, 2589, "1D"), None, False, 13)
    eq((sp["now"], sp["best"]), (44, 52), "Sell now and Suggested price match the Python model")
    stk = tt.lines[len(tt.lines)]
    eq((round(stk.rr, 2), round(stk.rg, 2), round(stk.rb, 2)), (1, 1, 1), "the stack's value is white")
    hdr = tt.lines[1]
    eq((round(hdr.r, 3), round(hdr.g, 3), round(hdr.b, 3)), (0.22, 0.74, 0.97), "header in the sell colour")
    vl = tt.lines[2]
    eq((round(vl.r, 2), round(vl.g, 2), round(vl.rr, 2), round(vl.rg, 2)), (0.25, 0.8, 0.25, 0.8), "Auction it in green")

    M.ShowItem(tt, 2589, bag_info, False)     # same show fires again without a clear
    eq(len(tt.lines), 7, "no duplicate lines on a second post call")

    M.shift = True
    got = lines_of(M.ShowItem(tt, 2589, bag_info))
    eq(got, [("Goblin Ledger", "2h ago - /reload"), ("Auction it", "3.4x vendor"), ("AH value (x20)", "9s 40c  " + up2),
             ("7-day average (x20)", "9s 20c"), ("Sell now (x20)", "8s 80c"), ("Suggested price (x20)", "10s 40c"),
             ("This stack (20), after AH cut", "8s 93c")], "Shift shows stack values (the verdict and stack line stay)")
    got = lines_of(M.ShowItem(tt, 2589, None))
    eq(got[2], ("AH value", "47c  " + up2), "Shift without a stack shows unit price")
    M.shift = False

    got = lines_of(M.ShowItem(tt, 99999, None))
    eq(got, [("Goblin Ledger: no recent listings",)], "unknown item: one muted line")
    eq(round(tt.lines[1].r, 2), 0.55, "unknown item line is muted")

    # no-data line only for items that could be on the auction house
    G.C_Item = G.loadstring("""return {
      GetItemInfo = function(id)
        local bind = ({ [6948] = 1, [5000] = 4 })[id] or 0
        return "Item", "link", 1, 1, 0, "Misc", "Misc", 1, "", 134400, 0, 15, 0, bind
      end,
      GetItemLocation = function(guid) return { guid = guid } end,
      IsBound = function(loc) return loc.guid == "bound" end,
    }""")()
    eq(lines_of(M.ShowItem(tt, 6948, None)), [], "bind-on-pickup item (Hearthstone): no line")
    eq(lines_of(M.ShowItem(tt, 5000, None)), [], "quest item: no line")
    eq(lines_of(M.ShowItem(G.ShoppingTooltip1, 99998, None)), [], "comparison tooltip: no no-data line")
    eq(lines_of(M.ShowItem(tt, 99998, None)), [("Goblin Ledger: no recent listings",)], "tradeable item: no-data line kept")
    fn = M.postcalls[G.Enum.TooltipDataType.Item]
    tt.Clear(tt)
    fn(tt, G.loadstring("return { id = 99997, guid = 'bound' }")())
    eq(lines_of(tt.lines), [], "soulbound bag item (C_Item.IsBound): no line")
    G.C_Item = None
    # random suffix gear: the price covers every version
    tt.itemLink = "|cff1eff00|Hitem:2589::::::1034::60:::::|h[Linen Cloth of the Monkey]|h|r"
    got = lines_of(M.ShowItem(tt, 2589, None))
    eq(got[-1], ("Price covers all versions of this item",), "suffix link gets the versions note")
    tt.itemLink = "|cffffffff|Hitem:2589::::::::60:::::|h[Linen Cloth]|h|r"
    eq(len(lines_of(M.ShowItem(tt, 2589, None))), 6, "plain link: no versions note")
    tt.itemLink = "|cff1eff00|Hitem:2589::::::::60:::1:1:12718:1:28:6874:::::|h[Linen Cloth of the Bear]|h|r"
    eq(lines_of(M.ShowItem(tt, 2589, None))[-1], ("Price covers all versions of this item",),
       "bonus ids with no versions in the data: the versions note")
    tt.itemLink = None
    eq(ns.LinkHasSuffix("item:9789:0:0:0:0:0:-1034:0"), True, "negative suffix id counts")
    eq(ns.LinkHasSuffix("item:9789:0:0:0:0:0:0:0"), False, "zero suffix id")

    got = lines_of(M.ShowItem(tt, 99001, None))
    eq(got, [("Goblin Ledger", "2h ago - /reload"), ("AH value", "none listed")],
       "item with nothing listed: no prices (the game's own line says what a vendor pays)")

    got = lines_of(M.ShowItem(tt, 13468, None))
    eq(got[1:3], [("Auction it", "over 100x vendor"), ("AH value", "350g  " + vs("0%"))],
       "big price in tooltip, under 1% from its average: a grey 0%")

    ns.settings.compact = True
    eq(lines_of(M.ShowItem(tt, 2589, bag_info)), [("Auction it: 3.4x vendor", "AH 47c  " + up2 + old_hint("2h"))],
       "compact one line (2h old: a short grey note)")
    M.shift = True
    eq(lines_of(M.ShowItem(tt, 2589, bag_info)), [("Auction it: 3.4x vendor", "AH (x20) 9s 40c  " + up2 + old_hint("2h"))],
       "compact with Shift")
    M.shift = False
    eq(lines_of(M.ShowItem(tt, 99001, None)), [("AH value: none listed now" + old_hint("2h"),)], "compact nothing listed")
    ns.settings.compact = False

    M.now = UPDATED + 7 * 3600 + 5
    got = lines_of(M.ShowItem(tt, 2589, None))
    eq(got[0], ("Goblin Ledger", "7h ago - /reload"), "stale header age (with /reload)")
    eq(got[-1], ("Prices are 7h old: run Install-Goblin-Ledger.bat, then /reload",), "stale warning line")
    M.now = UPDATED + 2 * 3600 + 100

    eq(len(M.ShowItem(G.ShoppingTooltip1, 2589, None)), 6, "comparison tooltip gets lines")
    eq(len(M.ShowItem(G.EmbeddedItemTooltip, 2589, None)), 0, "other tooltips are skipped")
    tt.forbidden = True
    eq(len(M.ShowItem(tt, 2589, None)), 0, "forbidden tooltip is skipped")
    tt.forbidden = False
    excl = G.loadstring("return { getterName = 'GetBagItem', getterArgs = { 0, 1 }, excludeLines = true }")()
    eq(len(M.ShowItem(tt, 2589, excl)), 0, "excludeLines is respected")
    ns.settings.tooltip = False
    eq(len(M.ShowItem(tt, 2589, None)), 0, "tooltip setting off")
    ns.settings.tooltip = True
    eq(len(M.ShowItem(tt, 2589, None)), 6, "tooltip setting back on")
    eq(ns.lastError, None, "no tooltip errors")


def test_settings_and_slash(G, ns):
    print("settings and slash commands")
    M = G.MOCK
    eq(ns.settings.chartTF, "1D", "defaults before ADDON_LOADED")
    G.ForeverAH_Settings = G.loadstring("return { compact = true, chartTF = 'bogus', tooltip = 'yes', extra = 5 }")()
    M.FireEvent("ADDON_LOADED", "SomeOtherAddon")
    eq(ns.settingsLoaded, False, "other addon's ADDON_LOADED ignored")
    old = ns.settings
    M.FireEvent("ADDON_LOADED", "GoblinLedger")
    check(G.rawequal(ns.settings, old), "ns.settings table stays the same table")
    check(G.rawequal(G.ForeverAH_Settings, ns.settings), "ForeverAH_Settings is ns.settings")
    eq((ns.settings.compact, ns.settings.chartTF, ns.settings.tooltip, ns.settings.chart, ns.settings.extra),
       (True, "1D", True, True, 5), "saved values merged and fixed")
    ns.settings.compact = False

    slash = G.SlashCmdList["GOBLINLEDGER"]
    eq((G.SLASH_GOBLINLEDGER1, G.SLASH_GOBLINLEDGER2, G.SLASH_GOBLINLEDGER3, G.SLASH_GOBLINLEDGER4),
       ("/gl", "/goblinledger", "/fah", "/foreverah"), "slash names (/fah and /foreverah from before the rename)")

    n = len(M.prints)
    slash("")
    out = prints_since(G, n)
    check(any("prices for 6 items from the Alliance Auction House (US, PvP), updated 2h ago" in p for p in out),
          "/gl status line", out)
    check(any("/gl help" in p for p in out), "/gl points at help")

    n = len(M.prints)
    M.faction = "Horde"
    slash("status")
    check(any("may not match" in p for p in prints_since(G, n)), "Horde gets a data warning")
    M.faction = "Alliance"

    shown = []
    G.NS.ShowChart = G.WRAP(lambda iid, src: shown.append((int(iid), src)))
    toggles = []
    G.NS.ToggleChart = G.WRAP(lambda: toggles.append(1))
    slash("chart linen cloth")
    slash("chart |cffffffff|Hitem:13468::::::::60:::::|h[Black Lotus]|h|r")
    slash("chart 14047")
    slash("CHART [Runecloth]")
    slash("chart " + ICE_NAME.upper())
    eq(shown, [(2589, "slash"), (13468, "slash"), (14047, "slash"), (14047, "slash"), (7228, "slash")],
       "/gl chart <item> opens the chart")
    n = len(M.prints)
    slash("chart Nonexistent Thing")
    check(any("no price data for Nonexistent Thing" in p for p in prints_since(G, n)), "unknown chart item message")
    slash("chart")
    eq(len(toggles), 1, "/gl chart toggles")

    slash("tooltip")
    eq(ns.settings.tooltip, False, "/gl tooltip turns off")
    slash("tooltip")
    eq(ns.settings.tooltip, True, "/gl tooltip turns on")
    slash("compact")
    eq(ns.settings.compact, True, "/gl compact on")
    slash("compact")
    eq(ns.settings.compact, False, "/gl compact off")
    n = len(M.prints)
    slash("help")
    check(any("/gl chart Linen Cloth" in p for p in prints_since(G, n)), "/gl help lists commands")
    n = len(M.prints)
    slash("wibble")
    check(any("unknown command" in p for p in prints_since(G, n)), "unknown command message")
    for p in M.prints.values():
        check("\u2014" not in p and "\u2013" not in p, "chat text has no long dashes", p)

    eq(ns.DataAge(), 2 * 3600 + 100, "DataAge")
    M.now = UPDATED - 50
    eq(ns.DataAge(), 0, "DataAge never negative")
    M.now = UPDATED + 2 * 3600 + 100


def test_placeholder():
    print("placeholder data (no prices yet)")
    L, G, ns = new_env(read(os.path.join(ADDON, "Data.lua")))
    M = G.MOCK
    M.now = 1794917400
    eq(ns.db.updated, 0, "placeholder updated = 0")
    eq(ns.DataAge(), None, "DataAge nil without data")
    eq(ns.GetItem(2589), None, "GetItem nil")
    eq(ns.GetSeries(2589, "1D"), None, "GetSeries nil")
    eq(ns.FindItemID("Linen Cloth"), None, "FindItemID nil")
    eq(ns.Recommend(2589, "1D"), None, "Recommend nil")
    eq(len(M.ShowItem(G.GameTooltip, 2589, None)), 0, "no tooltip lines without data")
    M.FireEvent("ADDON_LOADED", "GoblinLedger")
    n = len(M.prints)
    M.FireEvent("PLAYER_LOGIN")
    check(any("no price data yet" in p for p in prints_since(G, n)), "login notice without data")
    n = len(M.prints)
    G.SlashCmdList["GOBLINLEDGER"]("")
    out = prints_since(G, n)
    check(any("Install-Goblin-Ledger.bat" in p and "/reload" in p for p in out), "/gl explains how to get data", out)
    shown = []
    G.NS.ShowChart = G.WRAP(lambda iid, src: shown.append(iid))
    G.SlashCmdList["GOBLINLEDGER"]("chart Linen Cloth")
    eq(shown, [], "no chart without data")


def test_fallback_hooks():
    print("fallback tooltip hooks (no TooltipDataProcessor)")
    L, G, ns = new_env(fixture_lua(), processor=False)
    M = G.MOCK
    M.now = UPDATED + 3600
    eq(ns.tooltipMode, "script", "falls back to OnTooltipSetItem")
    for name in ("GameTooltip", "ItemRefTooltip", "ShoppingTooltip1", "ShoppingTooltip2"):
        check(G[name].scripts["OnTooltipSetItem"] is not None, name + " hooked")
    check(G.EmbeddedItemTooltip.scripts["OnTooltipSetItem"] is None, "other tooltips not hooked")
    tt = G.GameTooltip
    tt.itemLink = "|cffffffff|Hitem:2589::::::::60:::::|h[Linen Cloth]|h|r"
    tt.Fire(tt, "OnTooltipSetItem")
    eq(lines_of(tt.lines)[:3], [("Goblin Ledger", "1h ago"), ("Auction it", "3.4x vendor"), ("AH value", "47c  " + vs("+2%"))],
       "fallback adds lines")
    eq(tt.shown, 1, "fallback resizes the tooltip with Show")
    tt.Fire(tt, "OnTooltipSetItem")
    eq(len(tt.lines), 6, "fallback no duplicates")
    tt.Clear(tt)
    tt.Fire(tt, "OnTooltipSetItem")
    eq(len(tt.lines), 6, "fallback adds again after a clear")
    # a bound copy (bind on pickup): the prices, but no verdict
    G.C_Item = G.loadstring("""return { GetItemInfo = function(id)
        return "Item", "link", 1, 1, 0, "Misc", "Misc", 1, "", 134400, 0, 15, 0, 1 end }""")()
    tt.Clear(tt)
    tt.Fire(tt, "OnTooltipSetItem")
    eq([ln[0] for ln in lines_of(tt.lines)], ["Goblin Ledger", "AH value", "7-day average", "Sell now", "Suggested price"],
       "fallback: a bound item gets no verdict")
    G.C_Item = None
    eq(ns.lastError, None, "no fallback errors")


def test_quest_item_hook():
    """Both tooltip handlers ask Bags.lua (ns.QuestItemLine) for a quest line,
    only for an item with no prices, after the price lines and once a show."""
    print("quest item hook (Bags.lua's QuestItemLine)")
    for processor in (True, False):
        mode = "processor" if processor else "script"
        L, G, ns = new_env(fixture_lua(), processor=processor)
        M = G.MOCK
        M.now = UPDATED + 3600
        ns.QuestItemLine = G.loadstring("""return function(tt, id)
            QCALLS = (QCALLS or 0) + 1
            if id == 5000 then tt:AddLine("Done: you can drop it (Find the Gems)", 0.25, 0.8, 0.45) return true end
            return false
          end""")()
        G.C_Item = G.loadstring("""return { GetItemInfo = function(id)
            local bind = ({ [5000] = 4 })[id] or 0
            return "Item", "link", 1, 1, 0, "Misc", "Misc", 1, "", 134400, 0, 12, 0, bind end }""")()
        tt = G.GameTooltip

        def show(iid):
            if processor:
                return lines_of(M.ShowItem(tt, iid, None))
            tt.Clear(tt)
            tt.itemLink = "|cffffffff|Hitem:%d::::::::60:::::|h[Item]|h|r" % iid
            tt.Fire(tt, "OnTooltipSetItem")
            return lines_of(tt.lines)
        shown0 = tt.shown
        eq(show(5000), [("Done: you can drop it (Find the Gems)",)], mode + ": a quest item gets the quest line alone")
        eq(G.QCALLS, 1, mode + ": asked once")
        if not processor:
            eq(tt.shown, shown0 + 1, mode + ": Show lays the tooltip out again for the quest line")
        else:
            M.ShowItem(tt, 5000, None, False)
            eq(len(tt.lines), 1, mode + ": no second quest line when the same show fires again")
            eq(G.QCALLS, 1, mode + ": and not asked again")
        got = show(2589)
        check(len(got) > 1 and all("Find the Gems" not in (ln[0] or "") for ln in got), mode + ": a priced item gets its prices only")
        eq(G.QCALLS, 1, mode + ": not asked for a priced item")
        eq(show(99998), [("Goblin Ledger: no recent listings",)], mode + ": an unpriced item the hook has nothing for")
        eq(G.QCALLS, 2, mode + ": asked for an unpriced item")
        G.C_Item = None
        eq(ns.lastError, None, mode + ": no tooltip errors")


def test_enchant_hook():
    """Both tooltip handlers ask Enchants.lua (ns.EnchantLines) for the enchant lines once a
    show, after the price and quest lines, with the base item id, the item's link and where
    it is (the processor's info; nil on the script path). Also for unpriced soulbound gear,
    where the price lines add nothing; the script path lays the tooltip out again for them."""
    print("enchant hook (Enchants.lua's EnchantLines)")
    for processor in (True, False):
        mode = "processor" if processor else "script"
        L, G, ns = new_env(fixture_lua(), processor=processor)
        M = G.MOCK
        M.now = UPDATED + 3600
        G.ECALLS = G.loadstring("return {}")()
        ns.QuestItemLine = G.loadstring("""return function(tt, id)
            if id == 5000 then tt:AddLine("Done: you can drop it", 0.25, 0.8, 0.45) return true end
            return false
          end""")()
        ns.EnchantLines = G.loadstring("""return function(tt, id, link, info)
            ECALLS[#ECALLS + 1] = { id = id, link = link, info = info, at = #tt.lines, tt = tt:GetName() }
            if id == 7777 or id == 2589 then tt:AddLine("Best enchant: Crusader", 0.9, 0.88, 0.84) return true end
            return false
          end""")()
        G.C_Item = G.loadstring("""return { GetItemInfo = function(id)
            local bind = ({ [7777] = 1, [5000] = 4 })[id] or 0
            return "Item", "link", 2, 1, 0, "Weapon", "Two-Hand", 1, "INVTYPE_2HWEAPON", 134400, 0, 2, 8, bind end }""")()
        tt = G.GameTooltip
        bag_info = G.loadstring("return { getterName = 'GetBagItem', getterArgs = { 0, 1 } }")()

        def show(iid, tip=None, info=None, enchant=""):
            tip = tip or tt
            link = "|cffffffff|Hitem:%d:%s:::::::60:::::|h[Item]|h|r" % (iid, enchant)
            tip.itemLink = link
            if processor:
                return lines_of(M.ShowItem(tip, iid, info)), link
            tip.Clear(tip)
            tip.Fire(tip, "OnTooltipSetItem")
            return lines_of(tip.lines), link

        same = G.loadstring("return function(a, b) return rawequal(a, b) end")()

        def calls():
            c = G.ECALLS
            return [(c[i].id, c[i].link, "bag_info" if same(c[i].info, bag_info) else c[i].info, c[i].at, c[i].tt)
                    for i in range(1, len(c) + 1)]
        shown0 = tt.shown
        got, link = show(7777, info=bag_info, enchant="1900")
        eq(got, [("Best enchant: Crusader",)], mode + ": unpriced soulbound gear gets the enchant line alone")
        want_info = "bag_info" if processor else None
        eq(calls(), [(7777, link, want_info, 0, "GameTooltip")], mode + ": called once with the id, the link and where it is")
        if processor:
            M.ShowItem(tt, 7777, bag_info, False)
            eq(len(calls()), 1, mode + ": not called again when the same show fires again")
        else:
            eq(tt.shown, shown0 + 1, mode + ": Show lays the tooltip out again for the enchant line")
        got, link = show(2589, info=bag_info)
        check(len(got) > 2 and got[0][0] == "Goblin Ledger" and got[-1] == ("Best enchant: Crusader",),
              mode + ": a priced item: the price lines first, the enchant line last (%s)" % got)
        eq(calls()[-1][3], len(got) - 1, mode + ": asked after every price line")
        got, link = show(5000, info=bag_info)
        eq(got, [("Done: you can drop it",)], mode + ": a quest item: the quest line, then nothing from the hook")
        eq(calls()[-1][:4], (5000, link, want_info, 1), mode + ": asked after the quest line")
        n = len(calls())
        if processor:
            excl = G.loadstring("return { excludeLines = { 1 } }")()
            show(7777, info=excl)
            eq(len(calls()), n, mode + ": not for a tooltip that leaves lines out")
        show(7777, tip=G.EmbeddedItemTooltip)
        eq(len(calls()), n, mode + ": not for tooltips the addon does not hook")
        got, _ = show(7777, tip=G.ShoppingTooltip1)
        eq(calls()[-1][4], "ShoppingTooltip1", mode + ": comparison tooltips ask too (Enchants.lua says no)")
        ns.EnchantLines = None
        got, _ = show(7777, info=bag_info)
        eq(got, [], mode + ": no hook, no line and no error")
        G.C_Item = None
        eq(ns.lastError, None, mode + ": no tooltip errors")


def test_bare_environment():
    print("bare environment (most WoW functions missing)")
    L = lua51.LuaRuntime(unpack_returned_tuples=True)
    L.execute("NS = {}; function LOAD(src, name) local f, e = loadstring(src, '@' .. name); "
              "if not f then error(e) end; return f('GoblinLedger', NS) end")
    G = L.globals()
    try:
        G.LOAD(fixture_lua(), "Data.lua")
        G.LOAD(read(os.path.join(ADDON, "Core.lua")), "Core.lua")
        G.LOAD(read(os.path.join(ADDON, "Recommend.lua")), "Recommend.lua")
        ok = True
    except Exception as e:  # noqa: BLE001
        ok = False
        print("   ", e)
    check(ok, "loads with no WoW API at all")
    if ok:
        ns = G.NS
        eq(ns.GetSeries(2589, "1D").n, 24, "series works without WoW API")
        eq(ns.Recommend(2589, "1D").verdict is not None, True, "Recommend works without WoW API")
        eq(ns.MoneyIcons(123405), "12g 34s 5c", "MoneyIcons without WoW API")


def test_localdata():
    """Smoke test against the simulated file the fetcher writes (if present).
    It is simulated, not real data. Checks the decoder against hist/<id>.json."""
    path = os.path.join(LOCALDATA, "ForeverAH_Data.lua")
    if not os.path.exists(path):
        print("simulated local data: not found, skipped")
        return
    print("simulated local data (goblin-ledger/prices/_localdata, simulated, not real)")
    L, G, ns = new_env(read(path))
    db = ns.db
    hour_end, day_end = int(db.hourEnd), int(db.dayEnd)
    count = 0
    mismatches = 0
    for iid in list(db["items"].keys()):
        iid = int(iid)
        raw = db["items"][iid]
        fields = raw.split("|")
        # 10 since the listings count was added; older files have 9
        check(len(fields) in (9, 10), "item %d has 9 or 10 fields" % iid, len(fields))
        if len(fields) == 10:
            check(fields[9] == "" or fields[9].isdigit(), "item %d listings count is a whole number" % iid, fields[9])
            eq(ns.GetItem(iid).auctions, int(fields[9]) if fields[9] else None, "item %d GetItem().auctions" % iid)
        for idx, limit in ((7, 168), (8, 400)):
            parts = fields[idx].split(";")
            check(len(parts) == 3, "item %d series has 3 parts" % iid)
            lens = [0 if p == "" else p.count(",") + 1 for p in parts]
            check(max(lens) <= limit, "item %d series within %d points" % (iid, limit), lens)
            check(lens[0] == lens[1], "item %d cheap/market same length" % iid, lens)
        for tf in ("1D", "5D", "1W", "1M", "1Y"):
            s = ns.GetSeries(iid, tf)
            r = ns.Recommend(iid, tf)
            check(s is not None and r is not None, "item %d %s decodes" % (iid, tf))
            if s.n > 0:
                eq(int(s.times[s.n]), hour_end if tf in ("1D", "5D", "1W") else day_end,
                   "item %d %s ends on the grid" % (iid, tf))
        hist_path = os.path.join(LOCALDATA, "hist", "%d.json" % iid)
        if os.path.exists(hist_path):
            with open(hist_path, encoding="utf-8") as fh:
                hist = json.load(fh)
            for key, tf in (("h", "1W"), ("d", "1Y")):
                by_t = {int(p[0]): p for p in hist.get(key, [])}
                s = ns.GetSeries(iid, tf)
                for i in range(1, int(s.n) + 1):
                    t = int(s.times[i])
                    p = by_t.get(t)
                    want_c = (int(p[1]) if p and p[1] > 0 else False)
                    want_m = (int(p[2]) if p and p[2] > 0 else False)
                    want_s = int(p[4]) if p else 0
                    got = (s.cheap[i], s.market[i], int(s.sold[i]))
                    got = tuple(int(v) if v is not False else False for v in got)
                    if got != (want_c, want_m, want_s):
                        mismatches += 1
                        if mismatches <= 5:
                            print("    mismatch item", iid, tf, t, "got", got, "hist", (want_c, want_m, want_s))
        count += 1
    eq(mismatches, 0, "decoded series match hist/<id>.json")
    verdict_parity(ns, sorted(int(k) for k in db["items"].keys()), "simulated local data", tfs=("1D", "1W"),
                   lives=((None, False),))
    print("    checked %d simulated items" % count)


def series24(price, mkt, sold=1):
    return enc_series([price] * 24, [mkt] * 24, [sold] * 24) + "|" + enc_series([price] * 5, [mkt] * 5, [24 * sold] * 5)


# Hand-made items for the fields added to the data file: listings count
# (10th field), "" and "Item 12345" names, and names shared by two items.
NEW_ITEMS = dict(ITEMS)
NEW_ITEMS.update({
    9101: "Two Listings|1000|1100|5|30|1050|10|%s|2" % series24(1000, 1100),
    9102: "One Listing|1000|1100|1|30|1050|10|%s|1" % series24(1000, 1100),
    9103: "Plenty Listed|1000|1100|50|30|1050|10|%s|40" % series24(1000, 1100),
    9104: "Vendor Junk|1000|1100|5|30|1050|5|%s|5" % series24(1000, 1100),     # 220x the vendor price, 5 units
    9105: "Just Under|1000|1000|5|30|1000|5|%s|5" % series24(1000, 1000),      # exactly 200x: fine
    9107: "Enchanting Dust|1000|1100|50|30|1050|1|%s|40" % series24(1000, 1100),   # 1100x vendor, 50 units: real
    9108: "Ten Units|1000|1100|10|30|1050|5|%s|4" % series24(1000, 1100),      # 220x vendor, 10 units: trusted
    9109: "Nine Units|1000|1100|9|30|1050|5|%s|4" % series24(1000, 1100),      # 220x vendor, 9 units: not
    9106: "Nothing Now|0|0|0|30|1000|10|%s|0" % series24(1000, 1100),         # 0 listings: not "thin"
    9201: "|500|520|10|5|0|0|%s|10" % series24(500, 520),                     # new files: no name
    9202: "Item 9202|500|520|10|5|0|0|%s|" % series24(500, 520),             # older files' placeholder
    9301: "Warbringer's Sabatons |5000|5200|3|1|0|100|%s|3" % series24(5000, 5200),   # a real name ends in a space
    45: "Squire's Shirt|900|1033|11|5|0|1|%s|11" % series24(900, 1033),
    6117: "Squire's Shirt|950|1000|3|5|0|1|%s|3" % series24(950, 1000),
})


def test_new_fields():
    print("listings count, unreliable prices, unknown and shared names")
    L, G, ns = new_env(fixture_lua(NEW_ITEMS))
    G.MOCK.now = UPDATED + 100
    eq(ns.GetItem(9101).auctions, 2, "10th field: listings count")
    eq(ns.GetItem(2589).auctions, None, "9-field item: listings count unknown")
    eq(ns.GetItem(9202).auctions, None, "empty 10th field: unknown")
    eq(ns.SalesHours(), 24, "no salesHours in the file: a full day")
    for iid, note, advice in ((9101, "Only 2 listings: price may not be reliable", False),
                              (9102, "Only 1 listing: price may not be reliable", False),
                              (9103, None, True),
                              (9104, "Listed far above what a vendor pays: price may not be reliable", False),
                              (9105, None, True),
                              (9107, None, True),
                              (9108, None, True),
                              (9109, "Listed far above what a vendor pays: price may not be reliable", False)):
        r = ns.Recommend(iid, "1D")
        notes = lua_list(r.notes)
        eq(bool(r.history), True, "%d has history" % iid)
        eq(bool(r.advice), advice, "%d advice" % iid)
        if note:
            check(note in notes, "%d note %r" % (iid, note), notes)
            eq((r.buy, r.sell, r.listAt, r.margin, r.verdict), (0, 0, 0, 0, "Hold"), "%d: no targets, Hold" % iid)
        else:
            check(not any("reliable" in n for n in notes), "%d: no reliability note" % iid, notes)
            check(r.buy > 0 and r.listAt > 0, "%d: targets" % iid)
    r = ns.Recommend(9106, "1D")
    check(not any("listing" in n and "reliable" in n for n in lua_list(r.notes)), "0 listings is not 'thin'", lua_list(r.notes))
    # live cheapest does not bring advice back
    r = ns.Recommend(9101, "1D", 1200)
    eq((r.advice, r.listAt, r.cheapestNow), (False, 0, 1200), "thin item with a live price: still no List At")
    # too little history: no targets either (the chart says 'Needs more history')
    r = ns.Recommend(14047, "1D")
    check(r.history and r.advice and r.listAt > 0, "Runecloth 1D (5 prices) has history and advice")
    L2, G2, ns2 = new_env(fixture_lua(RULE_ITEMS))
    r = ns2.Recommend(9003, "1D")
    eq((r.history, r.advice, r.buy, r.sell, r.listAt, r.margin), (False, False, 0, 0, 0, 0),
       "2 hours of history: no Target Buy, List At or margin")
    for iid in sorted(NEW_ITEMS):
        for tf in ("1D", "1M"):
            for live in (None, 1200):
                py = py_recommend(ns, iid, tf, live)
                r = ns.Recommend(iid, tf, live)
                for k in ("buy", "sell", "listAt", "margin", "verdict", "advice", "history", "soldPerDay"):
                    eq(getattr(r, k), py.get(k), "new item %d %s live=%s %s" % (iid, tf, live, k))
                eq(lua_list(r.notes), py["notes"], "new item %d %s live=%s notes" % (iid, tf, live))

    # Names. No game API yet: unknown names are "" and show as "Item 123".
    eq(ns.GetItem(9201).name, "", "'' name stays empty without the game's name")
    eq(ns.GetItem(9202).name, "", "'Item 9202' is treated as unknown")
    eq(ns.ItemName(9202), "Item 9202", "ItemName falls back to Item <id>")
    eq(ns.ItemName(2589), "Linen Cloth", "ItemName uses the data name")
    eq(ns.FindItemID("Item 9202"), None, "the placeholder name is not searchable")
    eq(ns.FindItemID("Squire's Shirt"), 45, "shared name: FindItemID gives the lowest id")
    eq(lua_list(ns.FindItemIDs("squire's shirt")), [45, 6117], "FindItemIDs lists every id, lowest first")
    eq(lua_list(ns.FindItemIDs("Linen Cloth")), [2589], "FindItemIDs for a single item")
    eq(lua_list(ns.FindItemIDs("No Such Thing")), [], "FindItemIDs for nothing is empty")
    eq(ns.FindItemID("Warbringer's Sabatons"), 9301, "a data name ending in a space is still found")
    eq(ns.FindItemID("warbringer's sabatons "), 9301, "with or without the space")
    eq(ns.FindItemID("Mystery Dagger"), None, "game name unknown yet: not found")
    # The game loads names later. They are asked for only on a miss, and only
    # for the items still without a name.
    G.C_Item = G.loadstring("""return { GetItemNameByID = function(id)
        CALLS = (CALLS or 0) + 1
        if id == 9201 then return "Mystery Dagger" end
        if id == 9202 then return LATE_NAME end
      end }""")()
    eq(ns.FindItemID("Linen Cloth"), 2589, "a hit")
    eq(G.CALLS, None, "a hit never asks the game for names")
    eq(ns.FindItemID("mystery dagger"), 9201, "game name found on a miss")
    eq(G.CALLS, 2, "one miss asks once for each unnamed item")
    eq(ns.FindItemID("Nope"), None, "another miss")
    eq(G.CALLS, 3, "only the item still without a name is asked again")
    G.LATE_NAME = "Item 9202"
    eq(ns.FindItemID("Nope"), None, "a game name that is a placeholder too")
    G.LATE_NAME = "Late Gloves"
    eq(ns.FindItemID("late gloves"), 9202, "a name the game loaded later")
    eq(ns.GetItem(9201).name, "Mystery Dagger", "GetItem name comes from the game when the data has none")
    eq(ns.ItemName(9202), "Late Gloves", "ItemName uses the game name")
    n0 = G.CALLS
    eq(ns.FindItemID("still nothing"), None, "every unnamed item found: a miss asks nothing")
    eq(G.CALLS, n0, "no more name requests")
    G.C_Item = None
    eq(ns.lastError, None, "no errors")


def test_sales_hours():
    print("salesHours: measuring, early estimate, full day")
    for hours, linen, rune_thin in ((0, "sales: measuring", False), (6, "~480 sold/day (early estimate)", False),
                                    (1, "~2,880 sold/day (early estimate)", False), (24, "~120 sold/day", True)):
        L, G, ns = new_env(fixture_lua(sales_hours=hours))
        M = G.MOCK
        M.now = UPDATED + 100
        eq(ns.SalesHours(), hours, "salesHours %d read" % hours)
        eq(ns.SoldText(ns.GetItem(2589)), linen, "salesHours %d: Linen sold text" % hours)
        per, early = ns.SoldPerDay(ns.GetItem(2589))
        want_per, want_early = rec_model.sold_per_day(120, hours)
        eq((per, early), (want_per, want_early), "salesHours %d: SoldPerDay matches the model" % hours)
        got = lines_of(M.ShowItem(G.GameTooltip, 2589, None))
        # the tooltip is kept short: sales show on the chart, not in the tooltip
        eq([g for g in got if g[0].startswith("Listed")], [], "salesHours %d: no sold line in the tooltip" % hours)
        # Runecloth sold 2 in the hours measured: thin over a full day, not when scaled from 6 hours
        notes = lua_list(ns.Recommend(14047, "1D").notes)
        eq("Thin market" in notes, rune_thin, "salesHours %d: Runecloth thin market note" % hours)
        r = ns.Recommend(2589, "1D")
        eq((r.soldPerDay, bool(r.salesMeasuring), bool(r.soldEarly)), (want_per, hours == 0, hours < 24),
           "salesHours %d: Recommend sales fields" % hours)
        for iid in sorted(ITEMS):
            py = py_recommend(ns, iid, "1D")
            eq(lua_list(ns.Recommend(iid, "1D").notes), py["notes"], "salesHours %d item %d notes" % (hours, iid))
    L, G, ns = new_env(fixture_lua(sales_hours=0))
    r = ns.Recommend(99001, "1D")
    check("Thin market" not in lua_list(r.notes), "measuring: never a Thin market note", lua_list(r.notes))
    L, G, ns = new_env(fixture_lua(sales_hours=99))
    eq(ns.SalesHours(), 24, "salesHours above 24 is a full day")


def test_daily_points():
    print("the average's days, AH value against it, and WeekRange (6 hourly prices)")
    L, G, ns = new_env(fixture_lua())
    M = G.MOCK
    M.now = UPDATED + 100
    eq((ns.DailyPoints(2589), ns.DailyPoints(13468), ns.DailyPoints(4306), ns.DailyPoints(424242)), (10, 1, 0, 0),
       "DailyPoints")
    eq(ns.stats.series, 0, "DailyPoints does not unpack the series")
    for iid, cheap in ((2589, LINEN_HC), (13468, LOTUS_HC), (14047, RUNE_HC), (7228, ICE_HC)):
        lo, hi, n = ns.WeekRange(iid)
        wl, wh = rec_model.week_range([v or None for v in cheap])
        eq((lo, hi, n), (wl, wh, len([v for v in cheap if v])), "WeekRange %d matches the model" % iid)
        left = [ln[0] for ln in lines_of(M.ShowItem(G.GameTooltip, iid, None))]
        check(not any(x.startswith(("7-day low", "7-day high", "Market", "Cheapest", "Best price", "Listed ")) for x in left),
              "%d: none of the old tooltip lines" % iid, left)
    # the average says how many days of prices it covers, up to 7 (the data's own count)
    for iid, want in ((2589, "7-day average"), (13468, "1-day average"), (7228, "1-day average"), (4306, None)):
        left = [ln[0] for ln in lines_of(M.ShowItem(G.GameTooltip, iid, None))]
        eq([x for x in left if x.endswith("-day average")], [want] if want else [],
           "%d: the average line says how many days it covers" % iid)
    eq(ns.WeekRange(424242), (None, None, 0), "WeekRange of an unknown item")
    # 6 prices is enough, 5 is not
    items = dict(ITEMS)
    items[9401] = "Six|100|110|5|5|0|0|%s|%s" % (enc_series([None, 100, 101, 99, 120, 98, 97], [110] * 7, [1] * 7),
                                                enc_series([100], [110], [5]))
    items[9402] = "Five|100|110|5|5|0|0|%s|%s" % (enc_series([None, None, 101, 99, 120, 98, 97], [110] * 7, [1] * 7),
                                                 enc_series([100], [110], [5]))
    # 3 days of prices, up 4%; 9 days, down 10%; under 1% off: a grey 0%
    items[9403] = "Three|100|110|5|5|105|0|%s|%s" % (enc_series([100] * 3, [110] * 3, [1] * 3),
                                                   enc_series([100, 101, 102], [104, 105, 106], [5] * 3))
    items[9404] = "Down|80|90|5|5|100|0|%s|%s" % (enc_series([80] * 3, [90] * 3, [1] * 3),
                                                enc_series([95] * 9, [100] * 9, [5] * 9))
    items[9405] = "Flat|990|1000|5|5|1005|0|%s|%s" % (enc_series([990] * 3, [1000] * 3, [1] * 3),
                                                    enc_series([1000] * 9, [1005] * 9, [5] * 9))
    L2, G2, ns2 = new_env(fixture_lua(items))
    eq(ns2.WeekRange(9401), (97, 120, 6), "6 hourly prices: low and high")
    eq(ns2.WeekRange(9402), (None, None, 5), "5 hourly prices: not yet")
    M2 = G2.MOCK
    M2.now = UPDATED + 100

    def ah_and_avg(iid):
        got = lines_of(M2.ShowItem(G2.GameTooltip, iid, None))
        return [x for x in got if x[0] == "AH value"][0], [x for x in got if x[0].endswith("-day average")]
    eq(ah_and_avg(9403), (("AH value", "1s 10c  " + vs("+4%")), [("3-day average", "1s 5c")]),
       "3 days of prices: a 3-day average, and the AH value 4% over it (grey: under 25%)")
    eq(ah_and_avg(9404), (("AH value", "90c  " + vs("-10%")), [("7-day average", "1s")]),
       "9 days of prices: a 7-day average, the AH value 10% under it (grey, never red)")
    eq(ah_and_avg(9405), (("AH value", "10s  " + vs("0%")), [("7-day average", "10s 5c")]),
       "under 1% from the average: a grey 0%")
    ns2.settings.compact = True
    eq(lines_of(M2.ShowItem(G2.GameTooltip, 9404, None)), [("Auction it: can't vendor", "AH 90c  " + vs("-10%"))],
       "compact: the verdict, the AH value and its % too")
    ns2.settings.compact = False


# ---------------------------------------------------------------------------
# Versions of gear (variants, vids) and the collector's flips (hand-made data)
# ---------------------------------------------------------------------------
CLOAK = 9860
CLOAK_ITEMS = dict(ITEMS)
CLOAK_ITEMS.update({
    CLOAK: "Archer's Cloak|9000|14000|22|3|14000|2382|%s|22" % series24(9000, 14000),
    9860001: "Archer's Cloak of the Bear|14000|15000|6|1|15000|2382|%s|6" % series24(14000, 15000),
    9860002: "|9000|9500|5|1|9500|2382|%s|5" % series24(9000, 9500),          # no name in the data
    9860003: "Archer's Cloak of the Eagle|22000|23000|7|1|23000|2382|%s|7" % series24(22000, 23000),
})
# 12719:12722 is a version with two bonus ids; 12730:m28=6874 one where a modifier matters
CLOAK_VARIANTS = {CLOAK: "12718=9860001,12719:12722=9860002,12730:m28=6874=9860003",
                  9861: 5, "junk": "1=2", 9862: "=,abc=1,=5,12=9862"}
FLIPS = [
    "9860001|gap|12000|2|24000|15500|5450|227|thin,Suffix",
    "2589|vendor|9|40|360|0|160|444|",
    "14047|gap|70|5|350|98|115|328| slow , spike ",
    "13468|bogus|1|1|1|1|1|1|",          # unknown kind: skipped
    "7228|gap|900|1|900|0|50|55|",        # a gap flip with no relist price: skipped
    "4306|vendor|0|5|0|0|20|1|",          # buy price 0: skipped
    "4306|vendor|20|0|0|0|20|1|",         # no units: skipped
    "4306|vendor|20|3|0|0|-5|1|",         # no profit: skipped
    "garbage",
    "4306|vendor|20|3|0|0|15|250|",       # cost 0 is worked out: 60
    # newer files: a 10th field, the usual price (the 7-day middle), and "deal" flips
    "9860002|deal|7000|1|7000|9999|2499|357|suffix,unsure|10000",   # a deal on a version
    "4338|deal|300|4|1100|480|724|658||520",
    "2592|gap|90|6|500|140|298|596|thin|133",                       # a gap flip with a usual price
    "2447|deal|60|2|0|99|68|566||",                                 # no usual price: 0; cost 0 worked out: 120
    "765|deal|50|1|50|90|35|700||-4",                               # a broken usual price: 0
    "10620|deal|50|1|50|90|35|700||12.5",                           # not a whole number: 0
    "2772|deal|900|1|900|0|50|55||1500",                            # a deal with no relist price: skipped
    "3858|deal|0|1|0|1500|50|55||1500",                             # a deal with no buy price: skipped
    "4304|deal|900|1|900|1500|0|0||1500",                           # a deal with no profit: skipped
    "10940|sniper|900|1|900|1500|300|333||1500",                    # a kind this addon does not know: skipped
    "14047|vendor|12|5|60|0|40|666||0|extra",                       # more fields than it knows: the rest ignored
]


# Core.lua's flips reader as the copies of the addon players already have it
# (commit b41d8bc5, before deals and the usual price: 9 fields, gap and vendor
# only), with the helpers it uses, frozen here word for word, so the newer data
# files are checked against the old reader itself. Called with the data table;
# returns the old DataFlips function.
OLD_DATAFLIPS_LUA = r'''
local db = ...
local type, tonumber = type, tonumber
local floor = math.floor
local sfind, ssub, slower = string.find, string.sub, string.lower

-- Split on a single plain character, keeping empty fields.
local function split(s, sep)
  local out, n, start = {}, 0, 1
  while true do
    local i = sfind(s, sep, start, true)
    n = n + 1
    if not i then
      out[n] = ssub(s, start)
      return out, n
    end
    out[n] = ssub(s, start, i - 1)
    start = i + 1
  end
end

local function wholeNumber(s)
  local v = tonumber(s)
  if v and v == floor(v) then return v end
  return nil
end

local flipList
local function DataFlips()
  if flipList then return flipList end
  flipList = {}
  local raw = db.flips
  if type(raw) ~= "table" then return flipList end
  for i = 1, #raw do
    local s = raw[i]
    if type(s) == "string" then
      local f = split(s, "|")
      local id, kind = wholeNumber(f[1]), f[2]
      local buyMax, units = wholeNumber(f[3]), wholeNumber(f[4])
      local cost, relist, profit = wholeNumber(f[5]), wholeNumber(f[6]) or 0, wholeNumber(f[7])
      if id and id > 0 and (kind == "vendor" or (kind == "gap" and relist > 0)) and buyMax and buyMax > 0
        and units and units > 0 and profit and profit > 0 then
        local risk = {}
        for w in string.gmatch(f[9] or "", "[^,%s]+") do risk[#risk + 1] = slower(w) end
        flipList[#flipList + 1] = {
          id = id, kind = kind, buyMax = buyMax, units = units,
          cost = (cost and cost > 0) and cost or buyMax * units,
          relist = (kind == "gap") and relist or 0,
          profit = profit, roi = wholeNumber(f[8]) or 0, risk = risk,
        }
      end
    end
  end
  return flipList
end
return DataFlips
'''


def old_data_flips(lines):
    """The flips an older copy of the addon reads from these flip lines (the frozen
    b41d8bc5 reader): (True, [(id, kind, buyMax, units, cost, relist, profit, roi,
    risk)]), or (False, the Lua error) when the reader fails."""
    L = lua51.LuaRuntime(unpack_returned_tuples=True)
    db = L.execute("return { flips = { " + ", ".join(lua_str(s) for s in lines) + " } }")
    reader = L.execute(OLD_DATAFLIPS_LUA, db)
    ok, df = L.globals().pcall(reader)
    if not ok:
        return False, df
    return True, [(df[i].id, df[i].kind, df[i].buyMax, df[i].units, df[i].cost, df[i].relist, df[i].profit, df[i].roi,
                   lua_list(df[i].risk)) for i in range(1, len(df) + 1)]


def lua_value(v):
    return lua_str(v) if isinstance(v, str) else str(v)


def versions_lua(items=None, variants=None, flips=None):
    """The fixture with the variants and flips tables the collector writes before items."""
    src = fixture_lua(items if items is not None else CLOAK_ITEMS)
    extra = ""
    if variants is not None:
        extra += "  variants = {\n" + "".join(
            "    [%s] = %s,\n" % (lua_str(k) if isinstance(k, str) else k, lua_value(v)) for k, v in variants.items()) + "  },\n"
    if flips is not None:
        extra += "  flips = {\n" + "".join("    %s,\n" % lua_str(f) for f in flips) + "  },\n"
    return src.replace("  items = {", extra + "  items = {", 1)


def link(bonus, mods="1:28:6874", iid=CLOAK):
    """A link as the modern client writes one: item:id:enchant:4 gems:suffix:unique:
    level:spec:modifierMask:context:numBonus:bonus...:numModifiers:type:value..."""
    b = (str(len(bonus)) + ":" + ":".join(str(x) for x in bonus)) if bonus else ""
    return "|cff1eff00|Hitem:%d::::::::34:::1:%s:%s:::::|h[Archer's Cloak]|h|r" % (iid, b, mods)


def test_versions():
    print("gear versions: bonus ids in links, variants, vids")
    L, G, ns = new_env(versions_lua(variants=CLOAK_VARIANTS, flips=FLIPS))
    M = G.MOCK
    M.now = UPDATED + 100
    eq(ns.lastError, None, "odd entries in the variants table do not break loading")
    eq((ns.HasVariants(CLOAK), ns.HasVariants(2589), ns.HasVariants(9860001)), (True, False, False), "HasVariants")
    eq((ns.IsVariant(9860002), ns.IsVariant(CLOAK), ns.IsVariant(9862)), (True, False, False), "IsVariant")
    eq((ns.BaseItemID(9860003), ns.BaseItemID(2589), ns.BaseItemID(None)), (CLOAK, 2589, None), "BaseItemID")
    eq((ns.VariantNumber(9860002), ns.VariantNumber(CLOAK)), (2, None), "VariantNumber")
    eq(ns.VariantCount(), 3, "VariantCount: the junk entries are skipped")
    eq(ns.ItemCount(), len(ITEMS) + 1, "ItemCount counts the cloak once")
    eq(ns.GetItem(9860001).base, CLOAK, "GetItem(vid).base")
    eq(ns.GetItem(2589).base, None, "GetItem(item).base is nil")
    # bonus ids and modifiers read from real-looking links
    cases = [
        (link([12718]), [12718], {28: 6874}),
        (link([12722, 12719]), [12719, 12722], {28: 6874}),
        (link([12730], ""), [12730], {}),
        (link([]), [], {28: 6874}),          # no bonus ids: the next field counts the modifiers
        ("|cffffffff|Hitem:2589::::::::60:::::|h[Linen Cloth]|h|r", [], {}),
        ("item:9860:0:0:0:0:0:0:0:34:0:0:1:1:12718:2:28:6874:9:5", [12718], {28: 6874, 9: 5}),
        ("|Hitem:9860::::::::34:::1:5:12718|h[cut short]|h", [], {}),
        ("|Hitem:9860::::::::34:::1:999:1:2:3|h[silly count]|h", [], {2: 3}),
        ("|Hitem:9860::::::::34:::1:2:abc:12718|h[junk]|h", [], {}),   # the link ends at the junk
        ("|Hitem:9860::::::-1034::34:::::::|h[old suffix]|h", [], {}),
    ]
    for ln, want_b, want_m in cases:
        r = ns.LinkBonus(ln)
        bonus, mods = (r[0], r[1]) if isinstance(r, tuple) else (r, None)
        eq((lua_list(bonus), {int(k): int(v) for k, v in mods.items()} if mods is not None else None),
           (want_b, want_m), "LinkBonus %r" % ln)
        eq(ns.VariantKey(ln), ":".join(str(x) for x in want_b), "VariantKey %r" % ln)
    for bad in ("not a link", "", "item:", None, 12345):
        eq(ns.LinkBonus(bad), None, "LinkBonus(%r) is nil" % (bad,))
        eq(ns.VariantKey(bad), "", "VariantKey(%r) is empty" % (bad,))
    # finding the version
    eq(ns.FindVariant(CLOAK, link([12718])), 9860001, "FindVariant: one bonus id")
    eq(ns.FindVariant(CLOAK, link([12722, 12719])), 9860002, "FindVariant: two bonus ids in any order")
    eq(ns.FindVariant(CLOAK, link([12730])), 9860003, "FindVariant: a key with a modifier, link has it")
    eq(ns.FindVariant(CLOAK, link([12730], "1:28:1")), None, "FindVariant: the modifier differs")
    eq(ns.FindVariant(CLOAK, link([12730], "")), None, "FindVariant: the modifier is missing")
    eq(ns.FindVariant(CLOAK, link([55555])), None, "FindVariant: an unknown version")
    eq(ns.FindVariant(CLOAK, link([])), None, "FindVariant: no bonus ids")
    eq(ns.FindVariant(2589, link([12718], iid=2589)), None, "FindVariant: an item without versions")
    eq(ns.ResolveItem(link([12718])), 9860001, "ResolveItem: a version's link")
    eq(ns.ResolveItem(link([55555])), CLOAK, "ResolveItem: an unknown version's link gives the item")
    eq(ns.PriceIDForLink(CLOAK, link([12718])), (9860001, "version"), "PriceIDForLink: version")
    eq(ns.PriceIDForLink(CLOAK, link([55555])), (CLOAK, "all"), "PriceIDForLink: unknown version")
    eq(ns.PriceIDForLink(CLOAK, link([])), (CLOAK, "all"), "PriceIDForLink: plain copy of an item with versions")
    eq(ns.PriceIDForLink(2589, "|Hitem:2589::::::::60:::::|h[x]|h"), (2589, None), "PriceIDForLink: plain item")
    eq(ns.PriceIDForLink(2589, "|Hitem:2589::::::1034::60:::::|h[x]|h"), (2589, "all"), "PriceIDForLink: old suffix link")
    for vid, key in ((9860001, [12718]), (9860002, [12719, 12722]), (9860003, [12730])):
        vl = ns.VariantLink(vid)
        r = ns.LinkBonus(vl)
        eq(lua_list(r[0]), key, "VariantLink(%d) carries its bonus ids" % vid)
        eq(ns.FindVariant(CLOAK, vl), vid, "VariantLink(%d) finds it again" % vid)
    eq(ns.VariantLink(CLOAK), None, "no VariantLink for a plain item")
    # names
    eq(ns.ItemName(9860001), "Archer's Cloak of the Bear", "a version's name from the data")
    eq(ns.ItemName(9860002), "Item 9860 (version 2)", "a version with no name and no game name")
    eq(ns.FindItemID("archer's cloak of the eagle"), 9860003, "a version can be found by its name")
    eq(ns.FindItemID("Archer's Cloak"), CLOAK, "the item by its own name")
    G.C_Item = G.loadstring("return { GetItemNameByID = function(id) if id == 9860 then return \"Archer's Cloak\" end end }")()
    eq(ns.ItemName(9860002), "Archer's Cloak (version 2)", "a version with no name gets the game's name and its number")
    eq(ns.GetItem(9860002).name, "Archer's Cloak (version 2)", "and GetItem's name too")
    G.C_Item = None
    # tooltips
    tt = G.GameTooltip
    tt.itemLink = link([12718])
    got = lines_of(M.ShowItem(tt, CLOAK, None))
    # the verdict judges that version's prices: 1g 50s x 95 against a vendor's 23s 82c x 100
    eq(got[1:3], [("Auction it", "5.9x vendor"), ("AH value", "1g 50s  " + vs("0%"))],
       "version tooltip: that version's verdict and market price")
    eq(got[-1], ("Prices for this version only",), "version tooltip: says so")
    tt.itemLink = link([55555])
    got = lines_of(M.ShowItem(tt, CLOAK, None))
    eq((got[1], got[-1]), (("AH value", "1g 40s  " + vs("0%")), ("Price covers all versions of this item",)),
       "unknown version: the whole item's prices, covering all versions")
    tt.itemLink = link([])
    eq(lines_of(M.ShowItem(tt, CLOAK, None))[-1], ("Price covers all versions of this item",), "plain copy: all versions")
    tt.itemLink = None
    eq(lines_of(M.ShowItem(tt, CLOAK, None))[-1], ("Price covers all versions of this item",), "no link: all versions")
    eq(ns.lastError, None, "no tooltip errors")
    # an older data file: no variants table
    L2, G2, ns2 = new_env(versions_lua())
    eq((ns2.HasVariants(CLOAK), ns2.VariantCount(), ns2.FindVariant(CLOAK, link([12718]))), (False, 0, None),
       "older file: no versions")
    eq(ns2.ItemCount(), len(CLOAK_ITEMS), "older file: every line is an item")
    eq(ns2.PriceIDForLink(CLOAK, link([12718])), (CLOAK, "all"), "older file: a version link covers all versions")
    eq(len(ns2.DataFlips()), 0, "older file: no collector flips")
    # a variants table that is not a table at all
    L3, G3, ns3 = new_env(fixture_lua().replace("  items = {", "  variants = 5,\n  flips = \"x\",\n  items = {", 1))
    eq((ns3.VariantCount(), len(ns3.DataFlips()), ns3.lastError), (0, 0, None), "variants and flips of the wrong type are ignored")


def test_data_flips():
    print("the collector's flips (Data.lua flips)")
    L, G, ns = new_env(versions_lua(variants=CLOAK_VARIANTS, flips=FLIPS))
    df = ns.DataFlips()
    got = [(df[i].id, df[i].kind, df[i].buyMax, df[i].units, df[i].cost, df[i].relist, df[i].profit, df[i].roi,
            lua_list(df[i].risk), df[i].usual) for i in range(1, len(df) + 1)]
    eq(got, [(9860001, "gap", 12000, 2, 24000, 15500, 5450, 227, ["thin", "suffix"], 0),
             (2589, "vendor", 9, 40, 360, 0, 160, 444, [], 0),
             (14047, "gap", 70, 5, 350, 98, 115, 328, ["slow", "spike"], 0),
             (4306, "vendor", 20, 3, 60, 0, 15, 250, [], 0),
             (9860002, "deal", 7000, 1, 7000, 9999, 2499, 357, ["suffix", "unsure"], 10000),
             (4338, "deal", 300, 4, 1100, 480, 724, 658, [], 520),
             (2592, "gap", 90, 6, 500, 140, 298, 596, ["thin"], 133),
             (2447, "deal", 60, 2, 120, 99, 68, 566, [], 0),
             (765, "deal", 50, 1, 50, 90, 35, 700, [], 0),
             (10620, "deal", 50, 1, 50, 90, 35, 700, [], 0),
             (14047, "vendor", 12, 5, 60, 0, 40, 666, [], 0)],
       "DataFlips keeps the valid flips in order (gap, vendor and deal; 9 or 10 fields), skips the broken ones, "
       "reads the risks and the usual price (0 when missing or not a whole number of copper)")
    check(G.rawequal(ns.DataFlips(), df), "DataFlips is read once")
    # The copies of the addon players already have (the frozen 9-field reader): the very same lines,
    # deals and 10- and 11-field lines included, read without an error; the deals and the kinds it does
    # not know are skipped, and every gap and vendor flip reads exactly as this copy reads it (no usual).
    kinds = [s.split("|")[1] for s in FLIPS if "|" in s]
    check("deal" in kinds and any(len(s.split("|")) >= 10 and s.split("|")[1] in ("gap", "vendor") for s in FLIPS),
          "the flip lines have deals and newer gap or vendor lines to read the old way")
    ok, old = old_data_flips(FLIPS)
    check(ok, "an older copy of the addon reads the newer flip lines without an error", old if not ok else "")
    if ok:
        eq(old, [g[:9] for g in got if g[1] in ("gap", "vendor")],
           "an older copy reads the same gap and vendor flips, skips the deals")
    # the old reader itself, on the lines it was written for, so the frozen copy is known to work
    ok9, old9 = old_data_flips(FLIPS[:10])
    eq((ok9, [o[:2] for o in old9] if ok9 else old9), (True, [(9860001, "gap"), (2589, "vendor"), (14047, "gap"), (4306, "vendor")]),
       "the frozen older reader reads the older lines")


def test_sell_prices():
    print("Sell now and Best price (ns.SellPrices)")
    L, G, ns = new_env(fixture_lua(NEW_ITEMS))
    L2, G2, ns2 = new_env(fixture_lua(RULE_ITEMS))
    for env, items in ((ns, NEW_ITEMS), (ns2, RULE_ITEMS)):
        for iid in sorted(items):
            for tf in ("1D", "1W", "1M"):
                for live, own in ((None, False), (1200, False), (1200, True), (16000, False), (1, False)):
                    item = env.GetItem(iid)
                    py = rec_model.sell_prices(py_recommend(env, iid, tf, live), live, own, item.vendor)
                    r = env.SellPrices(iid, tf, live, own)
                    got = {k: r[k] for k in ("now", "best", "nowWhy", "bestWhy", "live", "own", "nowVendor", "bestVendor")}
                    want = {k: py[k] for k in got}
                    eq(got, want, "SellPrices %d %s live=%s own=%s" % (iid, tf, live, own))
    eq(ns.SellPrices(424242, "1D"), None, "SellPrices of an unknown item")
    # hand-checked cases
    r = ns2.SellPrices(9001, "1D")
    eq((r.now, r.best), (9999, 12000), "flat market: Sell now 1c under 1g, Best price the 1g 20s sell target")
    r = ns2.SellPrices(9001, "1D", 16000)
    eq((r.now, r.best), (15999, 15999), "live 1g 60s: Best price never under a normal cheapest listing")
    r = ns2.SellPrices(9001, "1D", 16000, True)
    eq((r.now, r.best), (16000, 16000), "your own cheapest listing is matched")
    r = ns2.SellPrices(9001, "1D", 50000)
    eq((r.now, r.best), (49999, 12000), "cheapest far above history: Best price is the sell target")
    r = ns2.SellPrices(9003, "1D")
    eq((r.now, r.best, r.bestWhy), (9999, None, "history"), "too little history: Sell now only")
    r = ns2.SellPrices(9008, "1D")
    eq((r.now, r.best, r.bestWhy, r.nowVendor), (9999, None, "history", True), "a vendor pays more: flagged")
    r = ns.SellPrices(9101, "1D")
    eq((r.now, r.bestWhy), (999, "unreliable"), "2 listings: Sell now, no Best price")
    r = ns.SellPrices(9104, "1D")
    eq((r.now, r.nowWhy, r.best), (None, "unreliable", None), "a few units far above the vendor price: neither")
    r = ns.SellPrices(9106, "1D")
    eq((r.now, r.nowWhy), (None, "none"), "nothing listed: no Sell now")


def test_stack_verdicts():
    # The old "This stack" line (at Sell now, green or red) and the trailing red
    # "Vendor it: a vendor pays more / the same" line are replaced by the sell
    # verdict: the stack line is worth the verdict's price, in white, and only
    # shows when the AH pays more ("Auction it") or about the same ("Close call").
    print("This stack: at the verdict's price, only when the AH pays more")
    items = dict(ITEMS)
    items.update({
        9501: "Vendor Pays More|4000|4200|50|20|4200|5000|%s|20" % series24(4000, 4200),
        9502: "No Vendor|1000|1100|50|20|1050|0|%s|20" % series24(1000, 1100),
        9503: "Even|21|22|50|20|22|19|%s|20" % series24(21, 22),
        9504: "Nothing Now|0|0|0|20|0|25|%s|0" % series24(30, 32),
        9505: "Junk|1|1|5|20|1|0|%s|5" % series24(1, 1),
    })
    L, G, ns = new_env(fixture_lua(items))
    M = G.MOCK
    M.now = UPDATED + 100
    M.bag["0:1"] = G.loadstring("return { stackCount = 20 }")()
    bag = G.loadstring("return { getterName = 'GetBagItem', getterArgs = { 0, 1 } }")()
    tt = G.GameTooltip
    VERDICTS = ("Auction it", "Vendor it", "Close call", "Maybe auction")

    def stack(iid, info=bag):
        return [ln for ln in lines_of(M.ShowItem(tt, iid, info)) if ln[0].startswith(("This stack", "Vendor it:")) or ln[0] in VERDICTS]
    # a vendor pays 50s, the AH 42s after the cut (4200 x 95 = 399,000 against 500,000): +25%, no stack line
    eq(stack(9501), [("Vendor it", "+25% vs AH")], "a vendor pays more: Vendor it, no stack line")
    vl = tt.lines[2]
    eq((round(vl.r, 2), round(vl.g, 2), round(vl.rr, 2), round(vl.rg, 2)), (0.9, 0.33, 0.9, 0.33), "Vendor it is red")
    # no vendor price: the stack at the verdict's price, 1100 x 20 x 95 / 100 = 2g 9s
    eq(stack(9502), [("Auction it", "can't vendor"), ("This stack (20), after AH cut", "2g 9s")],
       "no vendor price: Auction it and the stack's AH value")
    sl = tt.lines[len(tt.lines)]
    eq((round(sl.rr, 2), round(sl.rg, 2), round(sl.rb, 2)), (1, 1, 1), "and the stack's value is white")
    eq(stack(9504), [("Maybe auction", "none listed now")], "nothing listed now: a maybe, no stack line")
    eq(stack(9505), [], "a 1c listing: no verdict and no stack line")
    eq(stack(9503, None), [("Close call", "tiny gain")], "one item, 22c against 19c: a close call, no stack line")
    eq(stack(9503), [("Close call", "tiny gain"), ("This stack (20), after AH cut", "4s 18c")],
       "a close call on a stack: the stack line at 22c (22 x 20 x 95 / 100 = 418)")
    for iid in sorted(items):
        it = ns.GetItem(iid)
        rec = py_recommend(ns, iid, "1D")
        sp = rec_model.sell_prices(rec, None, False, it.vendor)
        V = rec_model.sell_verdict(py_item(it), sp, rec, ns.db.salesHours)
        lines = [ln for ln in stack(iid) if ln[0].startswith("This stack")]
        if V and V["kind"] in ("auction", "close"):
            eq(lines, [("This stack (20), after AH cut", ns.MoneyText(rec_model.stack_after_cut(V["unit"], 20)))],
               "stack %d: the stack at the verdict's price" % iid)
        else:
            eq(lines, [], "stack %d: no stack line for %s" % (iid, V and V["kind"]))
    ns.settings.compact = True
    eq(lines_of(M.ShowItem(tt, 9502, bag)), [("Auction it: can't vendor", "AH 11s  " + vs("+4%"))],
       "compact tooltips are one line, no stack line")
    ns.settings.compact = False


# ---------------------------------------------------------------------------
# The sell verdict (Recommend.lua ns.SellVerdict, the tooltip's call) with a
# tooltip mock that has real fonts: TC1 to TC28 of the spec
# ---------------------------------------------------------------------------
VERDICT_ITEMS = dict(ITEMS)
VERDICT_ITEMS.update({
    # vendor 1s 50c; the cheapest (1s 58c) is a lone dip under the usual 1s 90c, the AH value 2s
    4306: "Silk Cloth|158|200|11850|14202|199|150|%s|%s|418" % (
        enc_series([190] * 23 + [158], [200] * 24, [600] * 24), enc_series([190, 158], [198, 200], [14000, 14202])),
    2838: "Heavy Stone|389|389|862|13417|367|60|%s|%s|39" % (
        enc_series([340] * 12 + [389] * 12, [370] * 12 + [389] * 12, [500] * 24), enc_series([350] * 7, [367] * 7, [13000] * 7)),
    17057: "Shiny Fish Scales|7|7|5000|7254|7|7|%s|323" % series24(7, 7, 300),
    12365: "Dense Stone|278|288|4000|6976|288|250|%s|209" % series24(278, 288, 300),
    9501: "Vendor Pays More|4000|4200|50|20|4200|5000|%s|20" % series24(4000, 4200),
    9502: "No Vendor|1000|1100|50|20|1050|0|%s|20" % series24(1000, 1100),
    9102: "One Listing|1000|1100|1|30|1050|10|%s|1" % series24(1000, 1100),
    9104: "Vendor Junk|1000|1100|5|30|1050|5|%s|5" % series24(1000, 1100),
    9504: "Nothing Now|0|0|0|20|0|25|%s|0" % series24(30, 32),
    9503: "Even|21|22|50|20|22|19|%s|20" % series24(21, 22),
    9601: "Jumped|3000|3000|20|50|1000|1000|%s|%s|8" % (
        enc_series([1000] * 23 + [3000], [1010] * 23 + [3000], [2] * 24), enc_series([1000] * 5, [1010] * 5, [50] * 5)),
    9602: "Boundary|44|44|500|100|44|38|%s|30" % series24(44, 44, 5),
    9603: "Vendor One|100|100|500|100|100|96|%s|30" % series24(100, 100, 5),
    9604: "Double|200|200|500|100|200|95|%s|30" % series24(200, 200, 5),
    9605: "Almost Double|199|199|500|100|199|95|%s|30" % series24(199, 199, 5),
    9704: "Ten Times|1100|1100|500|100|1100|100|%s|30" % series24(1100, 1100, 5),
    9505: "Junk|1|1|5|20|1|0|%s|5" % series24(1, 1),
    9701: "Slow Seller|500|520|8|0|510|400|%s|8" % series24(500, 520, 0),
    9702: "Slow Huge|500|520|8|0|510|100|%s|8" % series24(500, 520, 0),
    9703: "Tiny Gain|4|4|500|100|4|3|%s|30" % series24(4, 4, 5),
    9705: "Thin Vendor|50|50|1|10|50|60|%s|1" % series24(50, 50),
    9706: "Thin No Vendor|30000|30000|2|0|30000|0|%s|2" % series24(30000, 30000),
    # the rule edges: nothing listed, a Suggested price 300x / 150x the vendor price (over / under 200x);
    # the AH nets exactly as much as a vendor, then 1% more; exactly 2 copper more; nothing listed, vendor ahead
    9801: "Old Far|0|0|0|20|0|1|%s|0" % series24(300, 300),
    9802: "Old Near|0|0|0|20|0|2|%s|0" % series24(300, 300),
    9803: "Tie|100|100|500|100|100|95|%s|30" % series24(100, 100, 5),
    9804: "Tie Plus|101|101|500|100|101|95|%s|30" % series24(101, 101, 5),
    9805: "Two Copper|20|20|500|100|20|17|%s|30" % series24(20, 20, 5),
    9806: "Nothing Vendor|0|0|0|20|0|50|%s|0" % series24(30, 32),
})

# A tooltip with the engine's font facts: Font objects (CreateFont counted),
# 8 template lines (line 1 on the header font), more lines made on demand
# copying the Font of the line above, SetFontObject taking the Font's colour,
# GetLeftLine / GetRightLine and _G[name .. "TextLeft" .. i]. Clear fires
# OnTooltipCleared; Hide fires OnHide, then clears.
MOCKS_FONT = r"""
MOCK.fontsCreated = 0
local function newFont(name, file, size, flags, r, g, b)
  local f = { _name = name, _file = file, _size = size, _flags = flags, _c = { r or 1, g or 1, b or 1 }, _shadow = { 1, -1 } }
  function f:GetFont() return self._file, self._size, self._flags end
  function f:SetFont(p, s, fl) self._file, self._size, self._flags = p, s, fl; return true end
  function f:CopyFontObject(o)
    self._file, self._size, self._flags = o._file, o._size, o._flags
    self._c = { o._c[1], o._c[2], o._c[3] }; self._shadow = { o._shadow[1], o._shadow[2] }
  end
  function f:GetTextColor() return self._c[1], self._c[2], self._c[3] end
  function f:GetName() return self._name end
  return f
end
MOCK.newFont = newFont
GameTooltipText = newFont("GameTooltipText", "Fonts\\PTSansNarrow.ttf", 12, "", 1, 1, 1)
GameTooltipHeaderText = newFont("GameTooltipHeaderText", "Fonts\\PTSansNarrow.ttf", 13, "", 1, 1, 1)
function CreateFont(name)
  if MOCK.noCreateFont then return nil end
  MOCK.fontsCreated = MOCK.fontsCreated + 1
  local f = newFont(name, nil, nil, nil, 1, 1, 1); _G[name] = f; return f
end

local function newFS(name, font)
  local fs = { _name = name, _font = font, _c = { font._c[1], font._c[2], font._c[3] } }
  -- SetFontObject takes the Font's colour, like the game
  function fs:SetFontObject(o) self._font = o; self._c = { o._c[1], o._c[2], o._c[3] } end
  function fs:GetFontObject() return self._font end
  function fs:GetFont() return self._font:GetFont() end
  function fs:SetFont(p, s, fl) MOCK.lineSetFont = (MOCK.lineSetFont or 0) + 1; error("SetFont on a tooltip line") end
  function fs:SetTextColor(r, g, b) self._c = { r, g, b } end
  function fs:GetTextColor() return self._c[1], self._c[2], self._c[3] end
  function fs:SetText(t) self._text = t end
  return fs
end

local function newTooltip(name)
  local tt = { name = name, lines = {}, scripts = {}, forbidden = false, shown = 0, nfs = 0 }
  local function ensure(self, i)
    while self.nfs < i do
      local k = self.nfs + 1
      local fl, fr
      if k == 1 then fl, fr = GameTooltipHeaderText, GameTooltipHeaderText
      elseif k <= 8 then fl, fr = GameTooltipText, GameTooltipText
      else fl, fr = _G[name .. "TextLeft" .. (k - 1)]._font, _G[name .. "TextRight" .. (k - 1)]._font end
      _G[name .. "TextLeft" .. k] = newFS(name .. "TextLeft" .. k, fl)
      _G[name .. "TextRight" .. k] = newFS(name .. "TextRight" .. k, fr)
      self.nfs = k
    end
  end
  ensure(tt, 8)
  function tt:GetName() return self.name end
  function tt:IsForbidden() return self.forbidden end
  function tt:AddLine(text, r, g, b)
    local i = #self.lines + 1; ensure(self, i)
    table.insert(self.lines, { left = text, r = r, g = g, b = b })
    local L = _G[name .. "TextLeft" .. i]; L:SetText(text); L:SetTextColor(r or 1, g or 1, b or 1)
    _G[name .. "TextRight" .. i]:SetText(nil)
  end
  function tt:AddDoubleLine(l, rt, lr, lg, lb, rr, rg, rb)
    local i = #self.lines + 1; ensure(self, i)
    table.insert(self.lines, { left = l, right = rt, r = lr, g = lg, b = lb, rr = rr, rg = rg, rb = rb })
    local L, R = _G[name .. "TextLeft" .. i], _G[name .. "TextRight" .. i]
    L:SetText(l); L:SetTextColor(lr or 1, lg or 1, lb or 1); R:SetText(rt); R:SetTextColor(rr or 1, rg or 1, rb or 1)
  end
  function tt:NumLines() return #self.lines end
  function tt:GetLeftLine(i) return _G[name .. "TextLeft" .. i] end
  function tt:GetRightLine(i) return _G[name .. "TextRight" .. i] end
  function tt:HookScript(s, fn)
    if self.failHook then error("HookScript refused") end
    self.scripts[s] = self.scripts[s] or {}
    table.insert(self.scripts[s], fn)
  end
  function tt:HasScript(s) return true end
  function tt:Show() self.shown = self.shown + 1 end
  function tt:GetItem() return self.itemName, self.itemLink end
  function tt:Fire(s) for _, fn in ipairs(self.scripts[s] or {}) do fn(self) end end
  function tt:Clear() self.lines = {}; self:Fire("OnTooltipCleared") end
  function tt:Hide() self:Fire("OnHide"); self:Clear() end
  return tt
end
MOCK.newTooltip = newTooltip
GameTooltip = newTooltip("GameTooltip")
ItemRefTooltip = newTooltip("ItemRefTooltip")
ShoppingTooltip1 = newTooltip("ShoppingTooltip1")
ShoppingTooltip2 = newTooltip("ShoppingTooltip2")
EmbeddedItemTooltip = newTooltip("EmbeddedItemTooltip")
-- The game's own lines first (the item's name, ElvUI's "Sell Price" text line), then the post calls.
function MOCK.ShowGameItem(tt, id, info, blz)
  tt:Clear()
  for _, t in ipairs(blz or { "Item " .. id, "Sell Price: x" }) do tt:AddLine(t, 1, 1, 1) end
  tt.blz = #(blz or { 1, 2 })
  tt.processingInfo = info
  local fn = MOCK.postcalls[Enum.TooltipDataType.Item]
  if fn then fn(tt, { id = id }) end
  return tt.lines
end
function MOCK.Font(tt, i, side)
  local fs = _G[tt.name .. side .. i]
  if not fs then return nil end
  local _, size = fs._font:GetFont()   -- the Font object's (a test may break fs:GetFont)
  return fs._font._name, size
end
function MOCK.Color(tt, i, side)
  local fs = _G[tt.name .. side .. i]
  if not fs then return nil end
  return fs:GetTextColor()
end
"""


def font_env(items=None, now_offset=100, sales_hours=None):
    L = lua51.LuaRuntime(unpack_returned_tuples=True)
    L.globals().MOCK_PROCESSOR = True
    L.execute(MOCKS)
    L.execute(MOCKS_FONT)
    G = L.globals()
    G.LOAD(fixture_lua(VERDICT_ITEMS if items is None else items, sales_hours), "Data.lua")
    G.LOAD(read(os.path.join(ADDON, "Core.lua")), "Core.lua")
    G.LOAD(read(os.path.join(ADDON, "Recommend.lua")), "Recommend.lua")
    G.MOCK.now = UPDATED + now_offset
    return L, G, G.NS


COLOR_NAMES = {"sell": (0.22, 0.74, 0.97), "muted": (0.55, 0.51, 0.45), "warn": (1.00, 0.86, 0.25), "text": (0.90, 0.88, 0.84), "white": (1, 1, 1),
               "pos": (0.25, 0.80, 0.45), "neg": (0.90, 0.33, 0.33), "buy": (0.898, 0.722, 0.259)}


def cname(c):
    for k, v in COLOR_NAMES.items():
        if all(abs(a - b) < 0.001 for a, b in zip(c, v)):
            return k
    return str(c)


def show(G, tt, iid, count=1, shift=False):
    """Shows an item as the game does (2 game lines, then the post calls). A count > 1
    is a bag stack. Returns our lines as [(text, left colour, right colour)]."""
    M = G.MOCK
    M.shift = shift
    info = None
    if count > 1:
        M.bag["0:1"] = G.loadstring("return { stackCount = %d }" % count)()
        info = G.loadstring("return { getterName = 'GetBagItem', getterArgs = { 0, 1 } }")()
    M.ShowGameItem(tt, iid, info)
    M.shift = False
    return rows_of(G, tt)


def rows_of(G, tt):
    M = G.MOCK
    out = []
    for i in range(int(tt.blz or 0) + 1, len(tt.lines) + 1):
        ln = tt.lines[i]
        txt = (ln.left, ln.right) if ln.right is not None else (ln.left,)
        out.append((txt, cname(M.Color(tt, i, "TextLeft")), cname(M.Color(tt, i, "TextRight")) if ln.right is not None else None))
    return out


def big_lines(G, tt):
    """Every FontString of the tooltip (shown or not) on one of our Fonts: [(line, side, font, size)]."""
    out = []
    for i in range(1, int(tt.nfs) + 1):
        for side in ("TextLeft", "TextRight"):
            name, size = G.MOCK.Font(tt, i, side)
            if str(name).startswith("ForeverAH"):
                out.append((i, side, name, size))
    return out


def normal_fonts(G, tt, skip=(), size=12):
    """True when every line from 2 on (but skip) is on GameTooltipText at its size."""
    bad = []
    for i in range(2, int(tt.nfs) + 1):
        if i in skip:
            continue
        for side in ("TextLeft", "TextRight"):
            f = G.MOCK.Font(tt, i, side)
            if f != ("GameTooltipText", size):
                bad.append((i, side, f))
    return bad


BIG4 = [(4, "TextLeft", "ForeverAHBigFont1", 15), (4, "TextRight", "ForeverAHBigFont1", 15)]
# the verdict (line 4) and the price under it (line 5: the AH value, or Vendor pays)
BIG45 = BIG4 + [(5, "TextLeft", "ForeverAHBigFont1", 15), (5, "TextRight", "ForeverAHBigFont1", 15)]


def tc(G, tt, iid, want, big, label, count=1, shift=False):
    """One spec case: the lines with their colours, and whether the verdict (line 4) and the
    price under it (line 5: the AH value under Auction it, Vendor pays under Vendor it) are bigger."""
    got = show(G, tt, iid, count, shift)
    eq(got, want, label)
    eq(big_lines(G, tt), BIG45 if big else [],
       label + ": " + ("the verdict and its price are bigger" if big else "nothing is bigger"))
    eq(normal_fonts(G, tt, skip=(4, 5) if big else ()), [], label + ": every other line at the normal size")


def H(age="1m ago"):
    """The header: the age in grey, or over 80 minutes old "7h ago - /reload" in yellow."""
    return (("Goblin Ledger", age), "sell", "warn" if age.endswith(" - /reload") else "muted")


def AHV(money, pct=None, tag=""):
    return (("AH value" + tag, money + ("  " + vs(pct) if pct else "")), "text", "white")


def ROW(left, right, rc="white"):
    return ((left, right), "text", rc)


def py_item(it):
    return {"vendor": int(it.vendor or 0), "market": int(it.market or 0), "sold24": int(it.sold24 or 0)}


SILK_LINES = [H(), (("Auction it", "+26% vs vendor"), "pos", "pos"), AHV("2s", "0%"), ROW("2-day average", "1s 99c"),
              ROW("Sell now", "1s 57c", "neg"), ROW("Suggested price", "2s")]


def test_verdict_tooltips():
    print("sell verdict: the tooltip lines (TC1 to TC17, TC28)")
    L, G, ns = font_env()
    tt = G.GameTooltip
    stack = lambda v: ROW("This stack (%d), after AH cut" % v[0], v[1])  # noqa: E731
    # TC1 Silk x20: the cheapest (1s 58c) is a lone dip, the verdict judges List At, the AH value:
    # 200 x 95 = 19,000 against a vendor's 150 x 100 = 15,000, 26% more; the stack 200 x 20 x 95 / 100 = 38s
    tc(G, tt, 4306, SILK_LINES + [stack((20, "38s"))], True, "TC1 Silk x20", count=20)
    check(not any(r[0][0].startswith("Vendor it") for r in show(G, tt, 4306, 20)), "TC1: no 'Vendor it' line")
    tc(G, tt, 4306, SILK_LINES, True, "TC2 Silk by id")
    tc(G, tt, 4306, [H(), (("Auction it", "+26% vs vendor"), "pos", "pos"), AHV("40s", "0%", tag=" (x20)"),
                     ROW("2-day average (x20)", "39s 80c"), ROW("Sell now (x20)", "31s 40c", "neg"),
                     ROW("Suggested price (x20)", "40s"), stack((20, "38s"))], True, "TC3 Silk x20 with Shift",
       count=20, shift=True)
    tc(G, tt, 4306, SILK_LINES + [stack((20, "38s"))], True, "TC3 Shift released: TC1 again", count=20)
    eq(len(big_lines(G, tt)), 4, "TC3: exactly four big FontStrings (two lines) after the rebuild")
    tc(G, tt, 2838, [H(), (("Auction it", "6.1x vendor"), "pos", "pos"), AHV("3s 89c", "+5%"),
                     ROW("7-day average", "3s 67c"), ROW("Sell now", "3s 88c"), ROW("Suggested price", "3s 89c"),
                     stack((17, "62s 66c"))], True, "TC4 Heavy Stone x17", count=17)
    tc(G, tt, 2838, [H(), (("Auction it", "6.1x vendor"), "pos", "pos"), AHV("66s 13c", "+5%", tag=" (x17)"),
                     ROW("7-day average (x17)", "62s 39c"), ROW("Sell now (x17)", "65s 96c"),
                     ROW("Suggested price (x17)", "66s 13c"), stack((17, "62s 66c"))], True, "TC4 Heavy Stone x17 Shift",
       count=17, shift=True)
    tc(G, tt, 13468, [H(), (("Auction it", "over 100x vendor"), "pos", "pos"), AHV("350g", "0%"),
                      ROW("1-day average", "350g 5s"), ROW("Sell now", "350g 19s"), ROW("Suggested price", "350g 19s")],
       True, "TC5 Black Lotus")
    tc(G, tt, 17057, [H(), (("Vendor it", "+5% vs AH"), "neg", "neg"), ROW("Vendor pays", "7c"), AHV("7c", "0%"),
                      ROW("5-day average", "7c"),
                      ROW("Sell now", "6c", "neg"), ROW("Suggested price", "7c", "neg")], True,
       "TC6 Shiny Fish Scales x20 (no stack line)", count=20)
    tc(G, tt, 12365, [H(), (("Close call", "AH +9%"), "muted", "muted"), AHV("2s 88c", "0%"),
                      ROW("5-day average", "2s 88c"), ROW("Sell now", "2s 77c"), ROW("Suggested price", "2s 88c"),
                      stack((20, "54s 72c"))], False, "TC7 Dense Stone x20", count=20)
    tc(G, tt, 9501, [H(), (("Vendor it", "+25% vs AH"), "neg", "neg"), ROW("Vendor pays", "50s"), AHV("42s", "0%"),
                     ROW("5-day average", "42s"),
                     ROW("Sell now", "39s 99c", "neg"), ROW("Suggested price", "42s", "neg")], True,
       "TC8 Vendor Pays More x20 (no stack line)", count=20)
    # with Shift on the stack: Vendor pays for the stack, with the same (x20) tag as the other lines
    tc(G, tt, 9501, [H(), (("Vendor it", "+25% vs AH"), "neg", "neg"), ROW("Vendor pays (x20)", "10g"),
                     AHV("8g 40s", "0%", tag=" (x20)"), ROW("5-day average (x20)", "8g 40s"),
                     ROW("Sell now (x20)", "7g 99s 80c", "neg"), ROW("Suggested price (x20)", "8g 40s", "neg")], True,
       "TC8 Vendor Pays More x20 with Shift", count=20, shift=True)
    tc(G, tt, 9502, [H(), (("Auction it", "can't vendor"), "pos", "muted"), AHV("11s", "+4%"),
                     ROW("5-day average", "10s 50c"), ROW("Sell now", "9s 99c"), ROW("Suggested price", "11s"),
                     stack((20, "2g 9s"))], True, "TC9 No Vendor x20", count=20)
    tc(G, tt, 9102, [H(), (("Maybe auction", "few listed"), "buy", "muted"), AHV("11s", "+4%"),
                     ROW("5-day average", "10s 50c"), ROW("Sell now", "9s 99c")], False,
       "TC10 One Listing (no 'Few listed' line, no Suggested price)")
    tc(G, tt, 9104, [H(), AHV("11s", "+4%"), ROW("5-day average", "10s 50c"),
                     (("Few listed: the price may not be reliable",), "muted", None)], False, "TC11 vendorFar: no verdict")
    tc(G, tt, 9504, [H(), (("Maybe auction", "none listed now"), "buy", "muted"), ROW("AH value", "none listed", "muted"),
                     ROW("Suggested price", "32c")], False, "TC12 Nothing Now")
    # TC13 the thresholds
    tc(G, tt, 9503, [H(), (("Close call", "tiny gain"), "muted", "muted"), AHV("22c", "0%"), ROW("5-day average", "22c"),
                     ROW("Sell now", "20c", "neg"), ROW("Suggested price", "22c")], False, "TC13 9503 Even")
    for iid, verdict, big, rows in (
            (9602, ("Auction it", "+10% vs vendor"), True, [AHV("44c", "0%"), ROW("5-day average", "44c"),
                                                             ROW("Sell now", "43c"), ROW("Suggested price", "44c")]),
            (9603, ("Vendor it", "+1% vs AH"), True, [ROW("Vendor pays", "96c"), AHV("1s", "0%"), ROW("5-day average", "1s"),
                                                       ROW("Sell now", "99c", "neg"), ROW("Suggested price", "1s", "neg")]),
            (9604, ("Auction it", "2x vendor"), True, [AHV("2s", "0%"), ROW("5-day average", "2s"),
                                                        ROW("Sell now", "1s 99c"), ROW("Suggested price", "2s")]),
            (9605, ("Auction it", "+99% vs vendor"), True, [AHV("1s 99c", "0%"), ROW("5-day average", "1s 99c"),
                                                             ROW("Sell now", "1s 98c"), ROW("Suggested price", "1s 99c")]),
            (9704, ("Auction it", "10x vendor"), True, [AHV("11s", "0%"), ROW("5-day average", "11s"),
                                                         ROW("Sell now", "10s 99c"), ROW("Suggested price", "11s")])):
        c = ("pos", "pos") if verdict[0] == "Auction it" else ("neg", "neg")
        tc(G, tt, iid, [H(), (verdict, c[0], c[1])] + rows, big, "TC13 %d %s" % (iid, verdict[1]))
    for hi, lo, what, want in ((99940, 1000, "vendor", "99x vendor"), (10000, 100, "vendor", "100x vendor"),
                               (10001, 100, "vendor", "over 100x vendor"), (36860, 6000, "vendor", "6.1x vendor"),
                               (999, 100, "AH", "9.9x AH"), (209, 100, "AH", "2x AH"), (210, 100, "AH", "2.1x AH"),
                               (100, 100, "AH", "same as AH"), (19000, 15000, "vendor", "+26% vs vendor")):
        eq(ns.BetterText(hi, lo, what), want, "TC13 BetterText(%d, %d, %s)" % (hi, lo, what))
        eq(rec_model.better_text(hi, lo, what), want, "TC13 the Python twin's better_text(%d, %d, %s)" % (hi, lo, what))
    tc(G, tt, 9601, [H(), (("Maybe auction", "price jumped"), "buy", "muted"), AHV("30s", "+200%"),
                     ROW("5-day average", "10s"), ROW("Sell now", "29s 99c"), ROW("Suggested price", "10s 10c", "neg")],
       False, "TC14 Jumped")
    slow_rows = [AHV("5s 20c", "+1%"), ROW("5-day average", "5s 10c"), ROW("Sell now", "4s 99c"),
                 ROW("Suggested price", "5s 20c")]
    tc(G, tt, 9701, [H(), (("Maybe auction", "sells slowly"), "buy", "muted")] + slow_rows, False, "TC15 Slow Seller")
    tc(G, tt, 9702, [H(), (("Auction it", "4.9x vendor"), "pos", "pos")] + slow_rows, True, "TC15 Slow Huge")
    L3, G3, ns3 = font_env(sales_hours=3)
    tc(G3, G3.GameTooltip, 9701, [H(), (("Auction it", "+23% vs vendor"), "pos", "pos")] + slow_rows, True,
       "TC15 Slow Seller with 3 hours of sales measured (speed unknown)")
    tc(G, tt, 9703, [H(), (("Close call", "tiny gain"), "muted", "muted"), AHV("4c", "0%"), ROW("5-day average", "4c"),
                     ROW("Sell now", "3c", "neg"), ROW("Suggested price", "4c", "neg"), stack((20, "76c"))], False,
       "TC16 Tiny Gain x20", count=20)
    tc(G, tt, 9705, [H(), (("Vendor it", "+28% vs AH"), "neg", "neg"), ROW("Vendor pays", "60c"), AHV("50c", "0%"),
                     ROW("5-day average", "50c"),
                     ROW("Sell now", "49c", "neg"), (("Few listed: the price may not be reliable",), "muted", None)],
       True, "TC17 Thin Vendor")
    tc(G, tt, 9706, [H(), (("Maybe auction", "few listed"), "buy", "muted"), AHV("3g", "0%"), ROW("5-day average", "3g"),
                     ROW("Sell now", "2g 99s 99c")], False, "TC17 Thin No Vendor")
    # TC13 the rule edges, one fixture each: OLD_FAR (200x), VENDOR_PCT (101%), MIN_GAIN (2 copper)
    tc(G, tt, 9801, [H(), ROW("AH value", "none listed", "muted"), ROW("Suggested price", "3s")], False,
       "TC13 Old Far: nothing listed, a Suggested price 300x the vendor price, no verdict", count=20)
    tc(G, tt, 9802, [H(), (("Maybe auction", "none listed now"), "buy", "muted"), ROW("AH value", "none listed", "muted"),
                     ROW("Suggested price", "3s")], False, "TC13 Old Near: 150x the vendor price is used", count=20)
    # 100 x 95 = 9,500 against 95 x 100 = 9,500: the same, so the vendor (instant, no deposit)
    tc(G, tt, 9803, [H(), (("Vendor it", "same as AH"), "neg", "neg"), ROW("Vendor pays", "95c"), AHV("1s", "0%"),
                     ROW("5-day average", "1s"),
                     ROW("Sell now", "99c", "neg"), ROW("Suggested price", "1s", "neg")], True,
       "TC13 Tie: the AH nets the same as a vendor (no stack line)", count=20)
    # 101 x 95 = 9,595: exactly 101% of 9,500
    tc(G, tt, 9804, [H(), (("Close call", "AH +1%"), "muted", "muted"), AHV("1s 1c", "0%"), ROW("5-day average", "1s 1c"),
                     ROW("Sell now", "1s", "neg"), ROW("Suggested price", "1s 1c", "neg"),
                     stack((20, "19s 19c"))], False, "TC13 Tie Plus: 1% more is a close call", count=20)
    # 20 x 95 = 1,900 against 17 x 100 = 1,700: 11% more and exactly 2 copper (200) more
    tc(G, tt, 9805, [H(), (("Auction it", "+11% vs vendor"), "pos", "pos"), AHV("20c", "0%"), ROW("5-day average", "20c"),
                     ROW("Sell now", "19c"), ROW("Suggested price", "20c"), stack((20, "3s 80c"))], True,
       "TC13 Two Copper: exactly 2 copper more each is enough", count=20)
    tc(G, tt, 9806, [H(), (("Vendor it", "+64% vs AH"), "neg", "neg"), ROW("Vendor pays", "50c"),
                     ROW("AH value", "none listed", "muted"),
                     ROW("Suggested price", "32c", "neg")], True, "TC13 Nothing Vendor: none listed, a vendor ahead",
       count=20)
    # TC28 unchanged lines and no verdict
    tc(G, tt, 9505, [H(), AHV("1c", "0%"), ROW("5-day average", "1c"), ROW("Suggested price", "1c")], False,
       "TC28 Junk: 1 copper, no verdict")
    tc(G, tt, 99001, [H(), ROW("AH value", "none listed", "muted")], False, "TC28 nothing listed, no history")
    tc(G, tt, 99999, [(("Goblin Ledger: no recent listings",), "muted", None)], False, "TC28 not in the data")
    eq((ns.lastError, G.MOCK.lineSetFont), (None, None), "no tooltip errors, no SetFont on a line")


def test_verdict_cases():
    print("sell verdict: versions, bound, other markets, live, stale, compact (TC18 to TC23)")
    L, G, ns = font_env()
    M = G.MOCK
    tt = G.GameTooltip
    # TC18 a suffix link: the prices mix versions, so no verdict and nothing to sell at
    tt.itemLink = "|cff1eff00|Hitem:4306::::::1034::60:::::|h[Silk Cloth of the Monkey]|h|r"
    tc(G, tt, 4306, [H(), AHV("2s", "0%"), ROW("2-day average", "1s 99c"),
                     (("Price covers all versions of this item",), "muted", None)], False, "TC18 all versions")
    tt.itemLink = None
    # TC19 bound: bind on pickup, then a soulbound bag copy (C_Item.IsBound)
    no_verdict = [H(), AHV("2s", "0%"), ROW("2-day average", "1s 99c"), ROW("Sell now", "1s 57c", "neg"),
                  ROW("Suggested price", "2s")]
    G.C_Item = G.loadstring("""return {
      GetItemInfo = function(id)
        local bind = ({ [4306] = 1 })[id] or 0
        return "Item", "link", 1, 1, 0, "Misc", "Misc", 1, "", 134400, 0, 15, 0, bind
      end }""")()
    tc(G, tt, 4306, no_verdict, False, "TC19 bound Silk x20 (bind on pickup): no verdict, no stack line", count=20)
    G.C_Item = G.loadstring("""return {
      GetItemInfo = function(id) return "Item", "link", 1, 1, 0, "Misc", "Misc", 1, "", 134400, 0, 15, 0, 0 end,
      IsBound = function(loc) return loc.bag == 0 and loc.slot == 1 end }""")()
    G.ItemLocation = G.loadstring("""return { CreateFromBagAndSlot = function(self, bag, slot)
      return { bag = bag, slot = slot } end }""")()
    tc(G, tt, 4306, no_verdict, False, "TC19 soulbound Silk x20 in a bag: no verdict, no stack line", count=20)
    G.C_Item, G.ItemLocation = None, None
    # TC20 another market
    M.faction = "Horde"
    tc(G, tt, 4306, no_verdict, False, "TC20 Horde (the prices are the Alliance's)", count=20)
    M.faction = "Alliance"
    G.C_GameRules = G.loadstring("return { IsHardcoreActive = function() return true end }")()
    tc(G, tt, 4306, no_verdict, False, "TC20 Hardcore", count=20)
    G.C_GameRules = None
    tc(G, tt, 4306, SILK_LINES + [ROW("This stack (20), after AH cut", "38s")], True, "TC20 back home", count=20)
    # TC21 the auction house open: the live cheapest feeds List At and Sell now
    for live, own, verdict, now, best, stk in ((195, "false", "+26% vs vendor", "1s 94c", "2s", "38s"),
                                               (210, "false", "+32% vs vendor", "2s 9c", "2s 9c", "39s 71c"),
                                               (196, "true", "+26% vs vendor", "1s 96c", "2s", "38s")):
        ns.LiveCheapest = G.loadstring("return function(id) if id == 4306 then return %d, %s end end" % (live, own))()
        tc(G, tt, 4306, [H(), (("Auction it", verdict), "pos", "pos"), AHV("2s", "0%"), ROW("2-day average", "1s 99c"),
                         ROW("Sell now (live)", now), ROW("Suggested price", best),
                         ROW("This stack (20), after AH cut", stk)], True, "TC21 live %d %s" % (live, own), count=20)
    ns.LiveCheapest = None
    # TC22 prices 7h old: the verdict at the normal size, the stale line last
    L2, G2, ns2 = font_env(now_offset=7 * 3600 + 120)
    tc(G2, G2.GameTooltip, 4306, [H("7h ago - /reload")] + SILK_LINES[1:] +
       [(("Prices are 7h old: run Install-Goblin-Ledger.bat, then /reload",), "muted", None)], False, "TC22 stale Silk")
    # exactly 6h old (ns.STALE_SECONDS) is still fresh: bigger, no stale line
    L2, G2, ns2 = font_env(now_offset=6 * 3600)
    tc(G2, G2.GameTooltip, 4306, [H("6h ago - /reload")] + SILK_LINES[1:], True, "TC22 Silk exactly 6h old")
    # TC23 compact: one line, never bigger
    ns.settings.compact = True
    for iid, count, shift, want in (
            (4306, 20, False, (("Auction it: +26% vs vendor", "AH 2s  " + vs("0%")), "pos", "white")),
            (4306, 20, True, (("Auction it: +26% vs vendor", "AH (x20) 40s  " + vs("0%")), "pos", "white")),
            (2838, 17, False, (("Auction it: 6.1x vendor", "AH 3s 89c  " + vs("+5%")), "pos", "white")),
            (17057, 20, False, (("Vendor it: +5% vs AH", "AH 7c  " + vs("0%")), "neg", "white")),
            (12365, 1, False, (("Close call: AH +9%", "AH 2s 88c  " + vs("0%")), "muted", "white")),
            (9102, 1, False, (("Maybe auction: few listed", "AH 11s  " + vs("+4%")), "buy", "white")),
            (9504, 1, False, (("Maybe auction: none listed now", "AH: none listed"), "buy", "muted")),
            (9502, 1, False, (("Auction it: can't vendor", "AH 11s  " + vs("+4%")), "pos", "white")),
            (9104, 1, False, (("AH value: 11s  " + vs("+4%"),), "text", None)),
            (99001, 1, False, (("AH value: none listed now",), "text", None))):
        tc(G, tt, iid, [want], False, "TC23 compact %d x%d%s" % (iid, count, " Shift" if shift else ""), count, shift)
    ns.settings.compact = False
    eq(ns.lastError, None, "no tooltip errors")


def test_verdict_fonts():
    print("sell verdict: the bigger line's font (TC24 to TC26)")
    L, G, ns = font_env()
    M = G.MOCK
    tt = G.GameTooltip
    show(G, tt, 4306, 20)
    eq(big_lines(G, tt), BIG45, "TC24 Silk: lines 4 (Auction it) and 5 (AH value) on ForeverAHBigFont1 at 15")
    eq([cname(G.GameTooltipTextLeft4.GetTextColor(G.GameTooltipTextLeft4)),
        cname(G.GameTooltipTextRight4.GetTextColor(G.GameTooltipTextRight4)),
        cname(G.GameTooltipTextLeft5.GetTextColor(G.GameTooltipTextLeft5)),
        cname(G.GameTooltipTextRight5.GetTextColor(G.GameTooltipTextRight5))], ["pos", "pos", "text", "white"],
       "TC24: their colours are set again after SetFontObject (green, the AH value's own, not the Font's white)")
    eq(normal_fonts(G, tt, skip=(4, 5)), [], "TC24: every other line is GameTooltipText 12")
    eq(M.fontsCreated, 1, "TC24: one Font made")
    tt.Clear(tt)
    eq((big_lines(G, tt), M.Font(tt, 4, "TextLeft"), M.Font(tt, 5, "TextRight")),
       ([], ("GameTooltipText", 12), ("GameTooltipText", 12)), "TC24: a clear puts lines 4 and 5 back")
    show(G, tt, 4306, 20)
    tt.Hide(tt)
    eq(big_lines(G, tt), [], "TC24: Hide puts it back too")
    show(G, tt, 4306, 20)
    tt.Fire(tt, "OnHide")         # a hide that does not clear the lines (a parent hid, as with Alt+Z)
    eq(big_lines(G, tt), [], "TC24: OnHide alone puts it back")
    eq(rows_of(G, tt), SILK_LINES + [ROW("This stack (20), after AH cut", "38s")],
       "TC24: after OnHide alone the lines stay, the verdict still green (SetFontObject took its colour)")
    show(G, tt, 9104)
    eq(big_lines(G, tt), [], "TC24: the next item (no verdict) has nothing bigger")
    for _ in range(50):
        show(G, tt, 4306, 20)
    eq((M.fontsCreated, big_lines(G, tt)), (1, BIG45), "TC24: 50 more hovers make no new Font")
    G.GameTooltipText._size = 14
    show(G, tt, 4306, 20)
    eq((big_lines(G, tt), M.fontsCreated),
       ([(k, side, "ForeverAHBigFont2", 17) for k in (4, 5) for side in ("TextLeft", "TextRight")], 2),
       "TC24: GameTooltipText at 14 gets a new Font at 17")
    G.GameTooltipText._size = 12
    tt.Clear(tt)
    ir = G.ItemRefTooltip
    show(G, ir, 4306)
    eq(big_lines(G, ir), BIG45, "TC24: ItemRefTooltip's lines 4 and 5 are bigger too")
    eq(M.fontsCreated, 2, "TC24: the 12-point Font is reused for ItemRefTooltip")
    # TC24 line 4 on another addon's Font (a bold outline, not GameTooltipText): our Font is a
    # copy of it (face, flags, shadow), and the restore puts back that very Font object
    L5, G5, ns5 = font_env()
    t5 = G5.GameTooltip
    G5.loadstring("""
      OTHER = MOCK.newFont("OtherAddonBold", "Fonts\\\\PTSansNarrow.ttf", 12, "THICKOUTLINE", 1, 1, 1)
      OTHER._shadow = { 3, -3 }
      GameTooltipTextLeft4:SetFontObject(OTHER); GameTooltipTextRight4:SetFontObject(OTHER)""")()
    on_other = G5.loadstring("""return function()
      return GameTooltipTextLeft4:GetFontObject() == OTHER and GameTooltipTextRight4:GetFontObject() == OTHER end""")()
    show(G5, t5, 4306, 20)
    f = G5.ForeverAHBigFont1
    eq((big_lines(G5, t5), f._flags, f._shadow[1], f._shadow[2]),
       (BIG4 + [(5, "TextLeft", "ForeverAHBigFont2", 15), (5, "TextRight", "ForeverAHBigFont2", 15)], "THICKOUTLINE", 3, -3),
       "TC24 another addon's Font: ours keeps its flags and shadow, 3 points bigger (line 5 on GameTooltipText's copy)")
    t5.Clear(t5)
    check(on_other(), "TC24 another addon's Font: a clear puts back that very Font object")
    show(G5, t5, 4306, 20)
    t5.Fire(t5, "OnHide")
    check(on_other(), "TC24 another addon's Font: OnHide puts back that very Font object")
    # ElvUI's shadow on GameTooltipText is kept on our bigger copy
    L6, G6, ns6 = font_env()
    G6.GameTooltipText._shadow = G6.loadstring("return { 2, -2 }")()
    show(G6, G6.GameTooltip, 4306, 20)
    f = G6.ForeverAHBigFont1
    eq((f._shadow[1], f._shadow[2]), (2, -2), "TC24: our Font keeps GameTooltipText's shadow (ElvUI's)")
    # TC25 lines made after ours copy a normal line: 8 game lines, Silk, then ElvUI's own post call
    L2, G2, ns2 = font_env()
    tt2 = G2.GameTooltip
    blz = G2.loadstring("return { 'Heavy Thing', 'stat 2', 'stat 3', 'stat 4', 'stat 5', 'stat 6', 'stat 7', 'Sell Price: 1s' }")()
    G2.MOCK.ShowGameItem(tt2, 4306, None, blz)
    tt2.AddLine(tt2, " ")
    tt2.AddDoubleLine(tt2, "ID: 4306", "Bags: 30")
    eq([tt2.lines[i].left for i in range(9, 17)], ["Goblin Ledger", "Auction it", "AH value", "2-day average", "Sell now",
                                                   "Suggested price", " ", "ID: 4306"], "TC25 lines 9 to 16")
    eq(big_lines(G2, tt2), [(k, side, "ForeverAHBigFont1", 15) for k in (10, 11) for side in ("TextLeft", "TextRight")],
       "TC25: only lines 10 and 11 (the verdict, the AH value) are bigger")
    eq(normal_fonts(G2, tt2, skip=(10, 11)), [], "TC25: lines 12 to 16, made on demand, are GameTooltipText 12")
    # TC26 fallbacks: the same text and colours at the normal size, no error. In the last two the
    # left side is already bigger when the right side fails: both go back, still green
    for label, setup in (("ShoppingTooltip1", None), ("HookScript errors", "hook"), ("CreateFont missing", "nofont"),
                         ("CreateFont gives nothing", "nilfont"), ("left side's GetFont gives a secret size", "secret"),
                         ("right side's GetFont gives nothing", "nogetfont"), ("right side refuses our Font", "refuse")):
        L3, G3, ns3 = font_env()
        t3 = G3.ShoppingTooltip1 if setup is None else G3.GameTooltip
        if setup == "hook":
            t3.failHook = True
        elif setup == "nofont":
            G3.CreateFont = None
        elif setup == "nilfont":
            G3.MOCK.noCreateFont = True
        elif setup == "secret":
            # a secret keeps its type (a number here) but must not be used
            G3.loadstring("""
              issecretvalue = function(v) return v == 12.5 end
              GameTooltipTextLeft4.GetFont = function() return "Fonts\\\\PTSansNarrow.ttf", 12.5, "" end""")()
        elif setup == "nogetfont":
            G3.loadstring("GameTooltipTextRight4.GetFont = function() return nil end")()
        elif setup == "refuse":
            G3.loadstring("""
              local fs = GameTooltipTextRight4
              local set = fs.SetFontObject
              fs.SetFontObject = function(self, o)
                if type(o) == "table" and tostring(o._name):find("ForeverAH", 1, true) then error("refused") end
                return set(self, o)
              end""")()
        n0 = len(G3.MOCK.prints)
        tc(G3, t3, 4306, SILK_LINES, False, "TC26 " + label)
        eq((ns3.lastError, len(G3.MOCK.prints) - n0, G3.MOCK.lineSetFont), (None, 0, None),
           "TC26 %s: no error, no chat print" % label)
    # TC26 the second line (line 5: the AH value, or Vendor pays) fails: that line stays at the normal size
    # (a side already swapped goes back), the verdict stays bigger, and a clear puts it all back
    vendor_lines = [H(), (("Vendor it", "+25% vs AH"), "neg", "neg"), ROW("Vendor pays", "50s"), AHV("42s", "0%"),
                    ROW("5-day average", "42s"), ROW("Sell now", "39s 99c", "neg"), ROW("Suggested price", "42s", "neg")]
    for label, iid, want, code in (
            ("the AH value's right side refuses our Font", 4306, SILK_LINES, """
              local fs = GameTooltipTextRight5
              local set = fs.SetFontObject
              fs.SetFontObject = function(self, o)
                if type(o) == "table" and tostring(o._name):find("ForeverAH", 1, true) then error("refused") end
                return set(self, o)
              end"""),
            ("the AH value's left side's GetFont gives nothing", 4306, SILK_LINES,
             "GameTooltipTextLeft5.GetFont = function() return nil end"),
            ("Vendor pays' right side's GetFont gives nothing", 9501, vendor_lines,
             "GameTooltipTextRight5.GetFont = function() return nil end")):
        L7, G7, ns7 = font_env()
        t7 = G7.GameTooltip
        G7.loadstring(code)()
        eq(show(G7, t7, iid), want, "TC26 %s: the lines and colours" % label)
        eq(big_lines(G7, t7), BIG4, "TC26 %s: only the verdict is bigger" % label)
        eq(normal_fonts(G7, t7, skip=(4,)), [], "TC26 %s: line 5 at the normal size" % label)
        t7.Fire(t7, "OnHide")
        eq((big_lines(G7, t7), rows_of(G7, t7)), ([], want), "TC26 %s: OnHide puts it back, colours kept" % label)
        eq((ns7.lastError, G7.MOCK.lineSetFont), (None, None), "TC26 %s: no error, no SetFont on a line" % label)
    # the light tooltip mock (no NumLines, no GetLeftLine, no TextLeftN)
    L4, G4, ns4 = new_env(fixture_lua(VERDICT_ITEMS))
    G4.MOCK.now = UPDATED + 100
    got = lines_of(G4.MOCK.ShowItem(G4.GameTooltip, 4306, None))
    eq(got, [r[0] for r in SILK_LINES], "TC26 light tooltip mock: the same lines")
    eq((ns4.lastError, len(G4.MOCK.prints)), (None, 0), "TC26 light tooltip mock: no error, no chat print")


def verdict_tuple(v):
    if v is None:
        return None
    if isinstance(v, dict):
        return (v["kind"], v["unit"], v["left"], v["right"], v["lc"], v["rc"])
    return (v.kind, int(v.unit) if v.unit is not None else None, v.left, v.right, v.lc, v.rc)


def verdict_parity(ns, iids, label, tfs=("1D", "5D", "1M"), lives=((None, False), (1200, False), (1200, True), (2, False))):
    """TC27: rec_model.sell_verdict (the Python twin) equals ns.SellVerdict for every item."""
    bad = 0
    for iid in iids:
        it = ns.GetItem(iid)
        if it is None:
            continue
        for tf in tfs:
            for live, own in lives:
                P = ns.SellPrices(iid, tf, live, own)
                rec = py_recommend(ns, iid, tf, live)
                sp = rec_model.sell_prices(rec, live, own, it.vendor)
                py = verdict_tuple(rec_model.sell_verdict(py_item(it), sp, rec, ns.db.salesHours))
                lu = verdict_tuple(ns.SellVerdict(it, P))
                if py != lu:
                    bad += 1
                    if bad <= 5:
                        print("    verdict mismatch", label, iid, tf, live, own, py, lu)
                u = ns.SellVerdictPrice(it, P)
                pu = rec_model.sell_verdict_price(py_item(it), sp, rec)
                if (int(u) if u is not None else None) != pu:
                    bad += 1
    eq(bad, 0, "TC27 %s: the Python twin matches ns.SellVerdict and ns.SellVerdictPrice" % label)


def test_verdict_parity():
    print("sell verdict: the Python twin (TC27)")
    for hours in (None, 0, 3, 6, 24):
        L, G, ns = font_env(sales_hours=hours)
        verdict_parity(ns, sorted(VERDICT_ITEMS), "fixtures, salesHours %s" % hours)
    L, G, ns = new_env(fixture_lua(NEW_ITEMS))
    verdict_parity(ns, sorted(NEW_ITEMS), "new fields")
    L, G, ns = new_env(fixture_lua(RULE_ITEMS))
    verdict_parity(ns, sorted(RULE_ITEMS), "rule items")
    L, G, ns = new_env(versions_lua(variants=CLOAK_VARIANTS))
    verdict_parity(ns, sorted(CLOAK_ITEMS), "versions")
    L, G, ns = font_env()
    eq(ns.SellVerdict(None, None), None, "SellVerdict with nothing")
    eq(ns.SellVerdictPrice(ns.GetItem(4306), G.loadstring("return { rec = 5 }")()), None, "SellVerdictPrice with no advice table")
    eq(ns.SellVerdictPrice(ns.GetItem(4306), ns.SellPrices(4306, "1D")), 200, "Silk's verdict price is the AH value 2s")
    eq(ns.SellVerdictPrice(ns.GetItem(2838), ns.SellPrices(2838, "1D")), 388, "Heavy Stone's is List At, 3s 88c")
    eq(ns.SellVerdictPrice(ns.GetItem(9504), ns.SellPrices(9504, "1D")), 32, "nothing listed: the Suggested price")
    eq(ns.SellVerdictPrice(ns.GetItem(9104), ns.SellPrices(9104, "1D")), None, "vendorFar: none")


def test_chart_line_setting():
    print("chartLine setting")
    for saved, want in (("buy", "buy"), ("sell", "sell"), ("both", None), (5, None), (None, None)):
        L, G, ns = new_env(fixture_lua())
        src = "return { chartLine = %s }" % (lua_str(saved) if isinstance(saved, str) else ("nil" if saved is None else saved))
        G.ForeverAH_Settings = G.loadstring(src)()
        G.MOCK.FireEvent("ADDON_LOADED", "GoblinLedger")
        eq(ns.settings.chartLine, want, "saved chartLine %r" % (saved,))


def main():
    test_files()
    L, G, ns = new_env(fixture_lua())
    G.MOCK.now = UPDATED + 2 * 3600 + 100
    test_lazy_and_items(G, ns)
    test_series(G, ns)
    test_money(G, ns)
    test_recommend(G, ns)
    test_recommend_rules()
    test_settled_drop()
    test_settings_and_slash(G, ns)
    test_tooltip(G, ns)
    test_placeholder()
    test_fallback_hooks()
    test_quest_item_hook()
    test_enchant_hook()
    test_bare_environment()
    test_new_fields()
    test_sales_hours()
    test_daily_points()
    test_versions()
    test_data_flips()
    test_sell_prices()
    test_stack_verdicts()
    test_verdict_tooltips()
    test_verdict_cases()
    test_verdict_fonts()
    test_verdict_parity()
    test_chart_line_setting()
    test_localdata()
    print()
    if FAILS:
        print("FAILED: %d of %d checks" % (len(FAILS), len(FAILS) + PASSES[0]))
        for f in FAILS:
            print("  -", f)
        return 1
    print("ALL PASSED: %d checks" % PASSES[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())

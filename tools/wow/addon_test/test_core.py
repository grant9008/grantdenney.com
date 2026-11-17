"""
Tests for the Forever AH addon's Core.lua and Recommend.lua.

Runs the real addon files in Lua 5.1 (the same Lua as WoW) through the
python package lupa, with the WoW functions they use replaced by small fakes.

Usage: python test_core.py
Exit code 0 when everything passes.

The price fixture below is hand-written test data, not real prices.
"""
import json
import os
import sys

from lupa import lua51

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rec_model  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
ADDON = os.path.join(REPO, "wow-addon", "ForeverAH")
LOCALDATA = os.path.join(REPO, "wow", "_localdata")

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
    13468: "Black Lotus|3501948|3500004|4|3|3500500|1000|%s|%s" % (LOTUS_H, LOTUS_D),
    14047: "Runecloth|79|99|12|2|100|15|%s|%s" % (RUNE_H, RUNE_D),
    7228: "%s|980|1000|5|4|1010|25|%s|%s" % (
        ICE_NAME, enc_series(ICE_HC, ICE_HM, ICE_HS), enc_series([1000], [1030], [4])),
    99001: "%s|0|0|0|1|0|7|~;~;|~;~;" % QUOTE_NAME,
    4306: "Silk Cloth|0|0|0|0|0|25|;;",        # empty hourly series and no D field
}


def fixture_lua(items=None):
    items = ITEMS if items is None else items
    lines = ["-- ForeverAH price data. Generated by grantdenney.com/wow. Do not edit.",
             "ForeverAH_Data = {",
             "  v = 1,",
             "  updated = %d," % UPDATED,
             '  region = "us", house = 110, houseName = "Alliance Auction House", ruleset = "PvP",',
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
    table.insert(self.lines, { left = l, right = rt, r = lr, g = lg, b = lb })
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
  return f("ForeverAH", NS)
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
    it = {"cheapest": item.cheapest, "market": item.market, "sold24": item.sold24, "vendor": item.vendor}

    def windows(key):
        s = ns.GetSeries(iid, key)
        return [v or None for v in lua_list(s.cheap, s.n)], [v or None for v in lua_list(s.market, s.n)]
    return rec_model.recommend(it, windows, tf, live)


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
                    eq(getattr(r, k), py[k], "rule item %d %s live=%s %s" % (iid, tf, live, k))
                eq(lua_list(r.notes), py["notes"], "rule item %d %s live=%s notes" % (iid, tf, live))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_files():
    print("files")
    want_toc = ("## Interface: 16001\r\n## Title: Forever AH\r\n"
                "## Notes: Auction house prices, charts and price advice from grantdenney.com/wow\r\n"
                "## SavedVariables: ForeverAH_Settings\r\n## Version: 1.0.0\r\n"
                "Data.lua\r\nCore.lua\r\nRecommend.lua\r\nChart.lua\r\nAH.lua\r\nSidebar.lua\r\n")
    for name in ("ForeverAH.toc", "ForeverAH_Forever.toc"):
        with open(os.path.join(ADDON, name), "rb") as fh:
            eq(fh.read().decode("ascii"), want_toc, name + " content")
    with open(os.path.join(ADDON, "Data.lua"), "rb") as fh:
        eq(fh.read().decode("ascii"),
           "-- ForeverAH price data placeholder. Run Update-WoW-Prices.bat.\n"
           "ForeverAH_Data = ForeverAH_Data or { v = 1, updated = 0, items = {} }",
           "Data.lua placeholder")
    for name in ("Core.lua", "Recommend.lua", "Data.lua", "ForeverAH.toc", "ForeverAH_Forever.toc"):
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
                    eq(getattr(r, k), py[k], "%s %s" % (tag, k))
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
    eq(got, [("Forever AH", "2h ago"), ("Market", "47c"), ("Cheapest", "45c"), ("7-day avg", "46c"),
             ("Listed 900, ~120 sold/day",)], "full tooltip, known item")
    hdr = tt.lines[1]
    eq((round(hdr.r, 3), round(hdr.g, 3), round(hdr.b, 3)), (0.22, 0.74, 0.97), "header in the sell colour")

    M.ShowItem(tt, 2589, bag_info, False)     # same show fires again without a clear
    eq(len(tt.lines), 5, "no duplicate lines on a second post call")

    M.shift = True
    got = lines_of(M.ShowItem(tt, 2589, bag_info))
    eq(got, [("Forever AH", "2h ago"), ("Market (x20)", "9s 40c"), ("Cheapest (x20)", "9s"),
             ("7-day avg (x20)", "9s 20c"), ("Listed 900, ~120 sold/day",)], "Shift shows stack values")
    got = lines_of(M.ShowItem(tt, 2589, None))
    eq(got[1], ("Market", "47c"), "Shift without a stack shows unit price")
    M.shift = False

    got = lines_of(M.ShowItem(tt, 99999, None))
    eq(got, [("Forever AH: no recent listings",)], "unknown item: one muted line")
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
    eq(lines_of(M.ShowItem(tt, 99998, None)), [("Forever AH: no recent listings",)], "tradeable item: no-data line kept")
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
    eq(len(lines_of(M.ShowItem(tt, 2589, None))), 5, "plain link: no versions note")
    tt.itemLink = None
    eq(ns.LinkHasSuffix("item:9789:0:0:0:0:0:-1034:0"), True, "negative suffix id counts")
    eq(ns.LinkHasSuffix("item:9789:0:0:0:0:0:0:0"), False, "zero suffix id")

    got = lines_of(M.ShowItem(tt, 99001, None))
    eq(got, [("Forever AH", "2h ago"), ("Market", "none listed"), ("Cheapest", "none listed"),
             ("Listed 0, ~1 sold/day",)], "item with nothing listed")

    got = lines_of(M.ShowItem(tt, 13468, None))
    eq(got[1], ("Market", "350g"), "big price in tooltip")

    ns.settings.compact = True
    eq(lines_of(M.ShowItem(tt, 2589, bag_info)), [("AH: 47c (cheapest 45c)",)], "compact one line")
    M.shift = True
    eq(lines_of(M.ShowItem(tt, 2589, bag_info)), [("AH (x20): 9s 40c (cheapest 9s)",)], "compact with Shift")
    M.shift = False
    eq(lines_of(M.ShowItem(tt, 99001, None)), [("AH: none listed now",)], "compact nothing listed")
    ns.settings.compact = False

    M.now = UPDATED + 7 * 3600 + 5
    got = lines_of(M.ShowItem(tt, 2589, None))
    eq(got[0], ("Forever AH", "7h ago"), "stale header age")
    eq(got[-1], ("Prices are 7h old: run Update-WoW-Prices.bat, then /reload",), "stale warning line")
    M.now = UPDATED + 2 * 3600 + 100

    eq(len(M.ShowItem(G.ShoppingTooltip1, 2589, None)), 5, "comparison tooltip gets lines")
    eq(len(M.ShowItem(G.EmbeddedItemTooltip, 2589, None)), 0, "other tooltips are skipped")
    tt.forbidden = True
    eq(len(M.ShowItem(tt, 2589, None)), 0, "forbidden tooltip is skipped")
    tt.forbidden = False
    excl = G.loadstring("return { getterName = 'GetBagItem', getterArgs = { 0, 1 }, excludeLines = true }")()
    eq(len(M.ShowItem(tt, 2589, excl)), 0, "excludeLines is respected")
    ns.settings.tooltip = False
    eq(len(M.ShowItem(tt, 2589, None)), 0, "tooltip setting off")
    ns.settings.tooltip = True
    eq(len(M.ShowItem(tt, 2589, None)), 5, "tooltip setting back on")
    eq(ns.lastError, None, "no tooltip errors")


def test_settings_and_slash(G, ns):
    print("settings and slash commands")
    M = G.MOCK
    eq(ns.settings.chartTF, "1D", "defaults before ADDON_LOADED")
    G.ForeverAH_Settings = G.loadstring("return { compact = true, chartTF = 'bogus', tooltip = 'yes', extra = 5 }")()
    M.FireEvent("ADDON_LOADED", "SomeOtherAddon")
    eq(ns.settingsLoaded, False, "other addon's ADDON_LOADED ignored")
    old = ns.settings
    M.FireEvent("ADDON_LOADED", "ForeverAH")
    check(G.rawequal(ns.settings, old), "ns.settings table stays the same table")
    check(G.rawequal(G.ForeverAH_Settings, ns.settings), "ForeverAH_Settings is ns.settings")
    eq((ns.settings.compact, ns.settings.chartTF, ns.settings.tooltip, ns.settings.chart, ns.settings.extra),
       (True, "1D", True, True, 5), "saved values merged and fixed")
    ns.settings.compact = False

    slash = G.SlashCmdList["FOREVERAH"]
    eq((G.SLASH_FOREVERAH1, G.SLASH_FOREVERAH2), ("/fah", "/foreverah"), "slash names")

    n = len(M.prints)
    slash("")
    out = prints_since(G, n)
    check(any("prices for 6 items from the Alliance Auction House (US, PvP), updated 2h ago" in p for p in out),
          "/fah status line", out)
    check(any("/fah help" in p for p in out), "/fah points at help")

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
       "/fah chart <item> opens the chart")
    n = len(M.prints)
    slash("chart Nonexistent Thing")
    check(any("no price data for Nonexistent Thing" in p for p in prints_since(G, n)), "unknown chart item message")
    slash("chart")
    eq(len(toggles), 1, "/fah chart toggles")

    slash("tooltip")
    eq(ns.settings.tooltip, False, "/fah tooltip turns off")
    slash("tooltip")
    eq(ns.settings.tooltip, True, "/fah tooltip turns on")
    slash("compact")
    eq(ns.settings.compact, True, "/fah compact on")
    slash("compact")
    eq(ns.settings.compact, False, "/fah compact off")
    n = len(M.prints)
    slash("help")
    check(any("/fah chart Linen Cloth" in p for p in prints_since(G, n)), "/fah help lists commands")
    n = len(M.prints)
    slash("wibble")
    check(any("unknown command" in p for p in prints_since(G, n)), "unknown command message")
    for p in M.prints.values():
        check("—" not in p and "–" not in p, "chat text has no long dashes", p)

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
    M.FireEvent("ADDON_LOADED", "ForeverAH")
    n = len(M.prints)
    M.FireEvent("PLAYER_LOGIN")
    check(any("no price data yet" in p for p in prints_since(G, n)), "login notice without data")
    n = len(M.prints)
    G.SlashCmdList["FOREVERAH"]("")
    out = prints_since(G, n)
    check(any("Update-WoW-Prices.bat" in p and "/reload" in p for p in out), "/fah explains how to get data", out)
    shown = []
    G.NS.ShowChart = G.WRAP(lambda iid, src: shown.append(iid))
    G.SlashCmdList["FOREVERAH"]("chart Linen Cloth")
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
    eq(lines_of(tt.lines)[:2], [("Forever AH", "1h ago"), ("Market", "47c")], "fallback adds lines")
    eq(tt.shown, 1, "fallback resizes the tooltip with Show")
    tt.Fire(tt, "OnTooltipSetItem")
    eq(len(tt.lines), 5, "fallback no duplicates")
    tt.Clear(tt)
    tt.Fire(tt, "OnTooltipSetItem")
    eq(len(tt.lines), 5, "fallback adds again after a clear")
    eq(ns.lastError, None, "no fallback errors")


def test_bare_environment():
    print("bare environment (most WoW functions missing)")
    L = lua51.LuaRuntime(unpack_returned_tuples=True)
    L.execute("NS = {}; function LOAD(src, name) local f, e = loadstring(src, '@' .. name); "
              "if not f then error(e) end; return f('ForeverAH', NS) end")
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
    print("simulated local data (wow/_localdata, simulated, not real)")
    L, G, ns = new_env(read(path))
    db = ns.db
    hour_end, day_end = int(db.hourEnd), int(db.dayEnd)
    count = 0
    mismatches = 0
    for iid in list(db["items"].keys()):
        iid = int(iid)
        raw = db["items"][iid]
        fields = raw.split("|")
        check(len(fields) == 9, "item %d has 9 fields" % iid, len(fields))
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
    print("    checked %d simulated items" % count)


def main():
    test_files()
    L, G, ns = new_env(fixture_lua())
    G.MOCK.now = UPDATED + 2 * 3600 + 100
    test_lazy_and_items(G, ns)
    test_series(G, ns)
    test_money(G, ns)
    test_recommend(G, ns)
    test_recommend_rules()
    test_settings_and_slash(G, ns)
    test_tooltip(G, ns)
    test_placeholder()
    test_fallback_hooks()
    test_bare_environment()
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

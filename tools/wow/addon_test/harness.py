"""Integration harness for the Goblin Ledger addon (wow-addon/GoblinLedger).

Loads the five addon files in TOC order inside Lua 5.1 (lupa), with a mock of
the WoW API surface the addon touches, and the real generated (simulated)
price file goblin-ledger/prices/_localdata/ForeverAH_Data.lua as Data.lua. Then it plays the
addon the way a player would: login, slash commands, item tooltips, the
standalone chart, the modern auction house (search, select, sell, Use List At,
close), the legacy auction house and Auctionator's tabs.

Every price the addon shows is checked against an independent Python decode
of the data file and a Python version of the shared-contract formulas.

    python tools/wow/addon_test/harness.py           # run every scenario
    python tools/wow/addon_test/harness.py -v        # also list passing checks
    python tools/wow/addon_test/harness.py chart     # only scenarios whose name contains "chart"

Any data file can be used instead of the simulated one, for example a real
snapshot downloaded from the wow-data branch (with its items.json for names):

    python tools/wow/addon_test/harness.py --data ForeverAH_Data.lua --items items.json

(or set FAH_DATA_FILE and FAH_ITEMS_JSON). The test items are then picked from
that data, and big files are checked on a fixed sample of items.

Exit code 0 means every scenario passed. Nothing here touches the game folder.
"""
import math
import os
import re
import subprocess
import sys
import time

from lupa import lua51

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rec_model  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
# FAH_ADDON_DIR lets a copy of the addon be tested (used for mutation checks of this harness).
ADDON = os.environ.get("FAH_ADDON_DIR") or os.path.join(ROOT, "wow-addon", "GoblinLedger")
# The addon folder name the game gives the addon (its ADDON_LOADED name).
ADDON_NAME = "GoblinLedger"
LOCAL_DIR = os.path.join(ROOT, "goblin-ledger", "prices", "_localdata")
SIM_DATA_FILE = os.path.join(LOCAL_DIR, "ForeverAH_Data.lua")
SIM_ITEMS_FILE = os.path.join(LOCAL_DIR, "items.json")
# The data under test: the simulated file unless --data / FAH_DATA_FILE says otherwise (see main).
DATA_FILE = SIM_DATA_FILE
ITEMS_FILE = SIM_ITEMS_FILE
SIM = os.path.join(HERE, "sim_fetch.py")  # makes the simulated test data in goblin-ledger/prices/_localdata
SAMPLE = 250   # data files with more items than this are checked on a fixed sample of them

TOC_FILES = ["Data.lua", "Core.lua", "Recommend.lua", "Chart.lua", "AH.lua", "Sidebar.lua", "Bags.lua"]
TOC_HEADER = [
    "## Interface: 16001",
    "## Title: Goblin Ledger",
    "## Notes: Auction house prices, charts and price advice. Price data from Blizzard Entertainment.",
    "## X-Credits: Price data from Blizzard Entertainment",
    "## X-Website: https://grantdenney.com/goblin-ledger/",
    "## SavedVariables: ForeverAH_Settings",
    "## Version: 1.0.0",
]
PLACEHOLDER = ("-- Goblin Ledger price data placeholder. Run Install-Goblin-Ledger.bat.\n"
               "ForeverAH_Data = ForeverAH_Data or { v = 1, updated = 0, items = {} }")
TFS = ["1D", "5D", "1W", "1M", "1Y"]
TF_SPEC = {"1D": ("H", 24, 3600), "5D": ("H", 120, 3600), "1W": ("H", 168, 3600),
           "1M": ("D", 30, 86400), "1Y": ("D", 365, 86400)}
AUCTIONEER = 21
# The gold on the character: GetMoney() in the mock (123g 45s 67c) unless a
# scenario sets another. None = the game does not say (no GetMoney): no sizing.
MOCK_GOLD = 1234567
RICH_GOLD = 2000000000      # 200,000g: covers every idea

# ===========================================================================
# Independent Python model of the data file and the contract formulas
# ===========================================================================


def lua_unescape(s):
    out, i = [], 0
    simple = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v",
              "\\": "\\", '"': '"', "'": "'", "\n": "\n"}
    while i < len(s):
        ch = s[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        e = s[i]
        if e.isdigit():
            j = i
            while j < len(s) and j < i + 3 and s[j].isdigit():
                j += 1
            out.append(chr(int(s[i:j])))
            i = j
            continue
        out.append(simple[e])
        i += 1
    return "".join(out)


def b36(tok):
    neg = tok.startswith("-")
    body = tok[1:] if neg else tok
    v = int(body, 36) if body else 0
    return -v if neg else v


def decode_prices(sub):
    toks = sub.split(",") if sub != "" else []
    out, prev = [], None
    for t in toks:
        if t == "~":
            out.append(None)
            continue
        d = 0 if t == "" else b36(t)
        v = d if prev is None else prev + d
        prev = v
        out.append(v if v > 0 else None)   # a price of 0 or less is not a real price
    return out


def decode_sold(sub):
    toks = sub.split(",") if sub != "" else []
    return [int(t, 36) if t else 0 for t in toks]


def decode_series(s):
    parts = (s or "").split(";")
    parts += [""] * (3 - len(parts))
    c, m, so = decode_prices(parts[0]), decode_prices(parts[1]), decode_sold(parts[2])
    n = max(len(c), len(m), len(so))
    c = [None] * (n - len(c)) + c
    m = [None] * (n - len(m)) + m
    so = [0] * (n - len(so)) + so
    return {"n": n, "cheap": c, "market": m, "sold": so}


def known_name(n):
    """False for the names the data uses when it has none: "" and "Item 12345"
    (and, for a version, "Item 12345 (version 2)" or " (version 2)")."""
    return (isinstance(n, str) and n != "" and re.match(r"^Item \d+$", n) is None
            and re.match(r"^\s*\(version \d+\)$", n) is None and re.match(r"^Item \d+ \(version \d+\)$", n) is None)


def table_block(raw, name):
    """The text inside a top-level table of the data file (items, variants,
    flips): from its "name = {" line to the line that closes it, or None."""
    m = re.search(r"(?m)^[ \t]*" + name + r"[ \t]*=[ \t]*\{[ \t]*\r?$", raw)
    if not m:
        return None
    end = re.search(r"(?m)^[ \t]*\},?[ \t]*\r?$", raw[m.end():])
    return raw[m.end():m.end() + end.start()] if end else raw[m.end():]


def block_span(raw, name):
    """(start, end) of the inside of a top-level table, or None."""
    m = re.search(r"(?m)^[ \t]*" + name + r"[ \t]*=[ \t]*\{[ \t]*\r?$", raw)
    if not m:
        return None
    end = re.search(r"(?m)^[ \t]*\},?[ \t]*\r?$", raw[m.end():])
    return (m.end(), m.end() + end.start()) if end else (m.end(), len(raw))


def parse_flip(s):
    """One "id|kind|buyMax|units|cost|relist|profit|roi|risk|usual" flip (older
    files stop after risk: usual 0), checked the way Core.lua's DataFlips checks
    it, or None. Kinds: gap and deal (relisted, so they need a relist price) and vendor."""
    f = s.split("|")
    f += [""] * (10 - len(f))

    def whole(x):
        return int(x) if re.match(r"^-?\d+$", x or "") else None
    iid, kind, buy, units, cost, relist, profit = (whole(f[0]), f[1], whole(f[2]), whole(f[3]), whole(f[4]),
                                                   whole(f[5]) or 0, whole(f[6]))
    relists = kind in ("gap", "deal")
    if not (iid and iid > 0 and (kind == "vendor" or (relists and relist > 0)) and buy and buy > 0
            and units and units > 0 and profit and profit > 0):
        return None
    return {"id": iid, "kind": kind, "buyMax": buy, "units": units,
            "cost": cost if cost and cost > 0 else buy * units, "relist": relist if relists else 0,
            "profit": profit, "roi": whole(f[7]) or 0, "risk": [w.lower() for w in re.findall(r"[^,\s]+", f[8])],
            "usual": max(0, whole(f[9]) or 0)}


class RefData:
    """Reads ForeverAH_Data.lua with plain Python (no Lua)."""

    def __init__(self, path=None, text=None):
        raw = text if text is not None else open(path, "rb").read().decode("utf-8")
        self.text = raw
        self.first_line = raw.split("\n", 1)[0].rstrip("\r")
        head = raw.split("items = {", 1)[0]   # header fields only, never an item's text

        def num(key):
            m = re.search(r"\b" + key + r"\s*=\s*(-?\d+)", head)
            return int(m.group(1)) if m else None

        def strv(key):
            m = re.search(r"\b" + key + r'\s*=\s*"((?:[^"\\]|\\.)*)"', head)
            return lua_unescape(m.group(1)) if m else None

        self.updated = num("updated")
        self.fetched = num("fetched")          # newer files only
        self.salesHours = num("salesHours")    # newer files only (None = a full day)
        self.hourEnd = num("hourEnd")
        self.dayEnd = num("dayEnd")
        self.house = num("house")
        self.houseName = strv("houseName")
        self.region = strv("region")
        self.ruleset = strv("ruleset")
        # Versions of items (newer files): base id -> [(key, vid)], and vid -> base id.
        self.variants, self.base_of = {}, {}
        vblock = table_block(raw, "variants")
        for m in re.finditer(r'\[(\d+)\]\s*=\s*"((?:[^"\\\n]|\\.)*)"', vblock or ""):
            base = int(m.group(1))
            lst = []
            for entry in lua_unescape(m.group(2)).split(","):
                mm = re.match(r"^\s*(.*?)=(\d+)\s*$", entry)
                # a vid is base * 1000 + 1..999; each key part a bonus id or m<type>=<value>
                if (mm and mm.group(1) != "" and base * 1000 < int(mm.group(2)) < base * 1000 + 1000
                        and all(re.match(r"^(\d+|m-?\d+=-?\d+)$", t) for t in mm.group(1).split(":") if t != "")):
                    lst.append((mm.group(1), int(mm.group(2))))
                    self.base_of[int(mm.group(2))] = base
            if lst:
                self.variants[base] = lst
        # Flip ideas the collector found (newer files), best first, as Core.lua keeps them.
        self.flips = []
        fblock = table_block(raw, "flips")
        for m in re.finditer(r'"((?:[^"\\\n]|\\.)*)"', fblock or ""):
            fl = parse_flip(lua_unescape(m.group(1)))
            if fl:
                self.flips.append(fl)
        self.items = {}
        for m in re.finditer(r'\[(\d+)\]\s*=\s*"((?:[^"\\\n]|\\.)*)"', table_block(raw, "items") or ""):
            iid = int(m.group(1))
            f = lua_unescape(m.group(2)).split("|")
            f += [""] * (10 - len(f))

            def n10(x):
                try:
                    return int(x)
                except ValueError:
                    return 0
            self.items[iid] = {
                "id": iid, "name": f[0], "cheapest": n10(f[1]), "market": n10(f[2]),
                "listed": n10(f[3]), "sold24": n10(f[4]), "avg7": n10(f[5]), "vendor": n10(f[6]),
                "H": decode_series(f[7]), "D": decode_series(f[8]),
                # separate listings behind today's prices (10th field, newer files; None = unknown)
                "auctions": int(f[9]) if f[9].isdigit() else None,
            }

    def base_count(self):
        """Items, not counting versions (Core.lua's ItemCount)."""
        return sum(1 for i in self.items if i not in self.base_of)

    def find_variant(self, iid, bonus, mods=None):
        """Core.lua's FindVariant for an item and a link's bonus ids (and modifiers)."""
        key = ":".join(str(b) for b in sorted(bonus))
        mods = mods or {}
        best, best_n = None, None
        for k, vid in self.variants.get(iid, []):
            bon, km = [], {}
            for tok in k.split(":"):
                mm = re.match(r"^m(-?\d+)=(-?\d+)$", tok)
                if mm:
                    km[int(mm.group(1))] = int(mm.group(2))
                elif re.match(r"^-?\d+$", tok):
                    bon.append(int(tok))
            if ":".join(str(b) for b in sorted(bon)) != key or (key == "" and not km):
                continue
            if all(mods.get(t) == v for t, v in km.items()) and (best_n is None or len(km) > best_n):
                best, best_n = vid, len(km)
        return best if best in self.items else None

    def window(self, iid, tf):
        key, n, step = TF_SPEC[tf]
        full = self.items[iid][key]
        m = min(n, full["n"])
        end = self.hourEnd if key == "H" else self.dayEnd
        first = full["n"] - m
        return {
            "n": m, "step": step,
            "times": [end - (m - 1 - i) * step for i in range(m)],
            "cheap": full["cheap"][first:], "market": full["market"][first:], "sold": full["sold"][first:],
        }


def pct(sorted_vals, p):
    """Percentile with linear interpolation (PocketGE app.js method), rounded to whole copper.
    Written with 1-based indices so the floating point steps match Lua exactly."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    idx = p * (n - 1) + 1
    lo = math.floor(idx)
    if lo < 1:
        lo = 1
    if lo >= n:
        return sorted_vals[n - 1]
    w = idx - lo
    v = sorted_vals[lo - 1] * (1 - w) + sorted_vals[lo] * w
    return math.floor(v + 0.5)


def verdict_for(score):
    if score >= 60:
        return "Strong Buy"
    if score >= 25:
        return "Buy"
    if score > -25:
        return "Hold"
    if score > -60:
        return "Sell"
    return "Strong Sell"


def ref_recommend(ref, iid, tf, live=None):
    """Recommend.lua rules (shared-contract section 4 as revised after review:
    pos over the cheapest series with a 10% minimum range, window fallback,
    far-above-history and vendor notes). The model lives in rec_model.py."""
    it = ref.items[iid]

    def windows(key):
        w = ref.window(iid, key)
        return w["cheap"], w["market"]
    r = rec_model.recommend(it, windows, tf, live, sales_hours=ref.salesHours)
    r["points"] = ref.window(iid, r["tf"])["n"]
    return r


def no_advice_text(rec):
    """What the chart's Target Buy / List At boxes say instead of a price, or None."""
    if not rec["history"]:
        return "Needs more history"
    if not rec["advice"]:
        return "Price not reliable"
    return None


def collecting_msg(ref, iid, tf):
    """The chart's message for a timeframe with fewer than 2 prices."""
    win = ref.window(iid, tf)
    n = win["n"]
    daily = win["step"] >= 86400
    msg = "Collecting price history. This chart fills in as %s prices arrive (%s so far)." % (
        "daily" if daily else "hourly", str(n) if n > 0 else "none")
    if daily and ref.window(iid, "5D")["n"] >= 2:
        msg += "\nThe 1D, 5D and 1W charts already have hourly prices."
    return msg


def name_key(nm):
    """Core.lua's index key: trimmed (some real names end in a space), lower case."""
    return nm.strip(" \t\r\n\v\f").lower()


def name_index(ref):
    """Index key -> item ids, lowest first (Core.lua's FindItemIDs)."""
    idx = {}
    for iid in sorted(ref.items):
        nm = ref.items[iid]["name"]
        if known_name(nm) and name_key(nm):
            idx.setdefault(name_key(nm), []).append(iid)
    return idx


def sample_ids(ref, limit=None):
    """Every item for small data files; a fixed spread of items plus the unusual
    ones (shared or missing names, the priciest, thin or odd prices) for big ones."""
    limit = limit or SAMPLE
    ids = sorted(ref.items)
    if len(ids) <= limit:
        return ids
    step = len(ids) / float(limit)
    picked = {ids[int(i * step)] for i in range(limit)}
    picked |= {i for i in ROLE_IDS() if i in ref.items}
    for lst in name_index(ref).values():
        if len(lst) > 1:
            picked |= set(lst)
    picked |= set([i for i in ids if not known_name(ref.items[i]["name"])][:8])
    picked |= set(sorted(ids, key=lambda i: -ref.items[i]["market"])[:6])
    picked |= set([i for i in ids if (ref.items[i]["auctions"] or 3) < 3][:6])
    picked |= set([i for i in ids if ref.items[i]["vendor"] > 0 and ref.items[i]["market"] > 200 * ref.items[i]["vendor"]][:6])
    return sorted(picked)


def money_parts(c):
    v = math.floor(abs(c) + 0.5)
    g, s, k = v // 10000, (v % 10000) // 100, v % 100
    if v > 1000000:
        k = 0
    return c < 0, g, s, k


def money_text(c):
    neg, g, s, k = money_parts(c)
    parts = []
    if g:
        parts.append("%dg" % g)
    if s:
        parts.append("%ds" % s)
    if k:
        parts.append("%dc" % k)
    if not parts:
        return "0c"
    out = " ".join(parts)
    return "-" + out if neg else out


GOLD_I = "|TInterface\\MoneyFrame\\UI-GoldIcon:0:0:2:0|t"
SILVER_I = "|TInterface\\MoneyFrame\\UI-SilverIcon:0:0:2:0|t"
COPPER_I = "|TInterface\\MoneyFrame\\UI-CopperIcon:0:0:2:0|t"


def coin_string(money):
    """Python twin of the mock C_CurrencyInfo.GetCoinTextureString."""
    g, s, c = money // 10000, (money % 10000) // 100, money % 100
    parts = []
    if g > 0:
        parts.append("{:,}".format(g) + GOLD_I)
    if s > 0:
        parts.append("%d%s" % (s, SILVER_I))
    if c > 0 or money == 0:
        parts.append("%d%s" % (c, COPPER_I))
    return " ".join(parts)


def money_icons(c):
    neg, g, s, k = money_parts(c)
    amount = g * 10000 + s * 100 + k
    txt = coin_string(amount)
    if neg and amount > 0:
        txt = "-" + txt
    return txt


def commas(n):
    n = math.floor(n + 0.5)
    return ("-" if n < 0 else "") + "{:,}".format(abs(n))


def age_text(sec):
    if sec < 60:
        return "just now"
    if sec < 3600:
        return "%dm ago" % (sec // 60)
    if sec < 48 * 3600:
        return "%dh ago" % (sec // 3600)
    return "%dd ago" % (sec // 86400)


def parse_coins(s):
    """Copper value of a coin-icon string (as built by the mock) or a 'Xg Ys Zc' string."""
    if s is None:
        return None
    s = str(s)
    neg = s.startswith("-")
    total, found = 0, False
    for num, kind in re.findall(r"([\d,]+)\|TInterface\\MoneyFrame\\UI-(Gold|Silver|Copper)Icon", s):
        found = True
        total += int(num.replace(",", "")) * {"Gold": 10000, "Silver": 100, "Copper": 1}[kind]
    if not found:
        for num, kind in re.findall(r"(\d+)([gsc])\b", s):
            found = True
            total += int(num) * {"g": 10000, "s": 100, "c": 1}[kind]
    if not found:
        return None
    return -total if neg else total


def display_copper(c):
    """What MoneyText/MoneyIcons show for c: whole copper, copper dropped above 100g."""
    neg, g, s, k = money_parts(c)
    v = g * 10000 + s * 100 + k
    return -v if neg else v


def price_for_frame(copper, is_commodity, copper_ok=True):
    copper = math.floor(copper + 0.5)
    if copper <= 0:
        return None
    if is_commodity is False or not copper_ok:
        copper = max(100, (copper // 100) * 100)
    return copper


# ===========================================================================
# Lua mock of the WoW API (loaded as chunk @MOCK/wow.lua)
# ===========================================================================
MOCK_LUA = r'''
local M = {}
MOCK = M
M.errors, M.chat, M.timers, M.eventFrames, M.frames = {}, {}, {}, {}, {}
M.dash, M.violations, M.postCalls, M.loaded, M.counts = {}, {}, {}, {}, {}
M.cah = { browse = {}, search = {}, sell = {}, closed = 0, posted = {} }
M.browseResults, M.bags, M.items, M.knownEvents, M.sizeQueue = {}, {}, {}, {}, {}
M.commodityResults, M.guids = {}, {}
M.timerSeq, M.now, M.t, M.cursorX, M.cursorY = 0, 0, 1000, 0, 0
M.shift, M.faction, M.hardcore = false, "Alliance", false
M.screenH, M.screenW, M.uiParentScale = 768, 768 * 16 / 9, 1
M.client, M.copperValues, M.setOwnerClears, M.bareCoinGlobal = "modern", true, true, false
M.hookCount = 0

local dbg = debug
local os_date, os_time = os.date, os.time
local traceback = debug.traceback
local unpack, select, type, tostring, pairs, ipairs, error = unpack, select, type, tostring, pairs, ipairs, error
local floor = math.floor

local function finite(v) return type(v) == "number" and v == v and v ~= math.huge and v ~= -math.huge end
M.finite = finite
local function apiError(msg) error(msg, 3) end

local EM, EN = "\226\128\148", "\226\128\147"
function M.seeText(s)
  if type(s) == "string" and (s:find(EM, 1, true) or s:find(EN, 1, true)) then M.dash[#M.dash + 1] = s end
end

function M.err(where, e) M.errors[#M.errors + 1] = tostring(where) .. ": " .. tostring(e) end
function M.call(where, fn, ...)
  local n, args = select("#", ...), { ... }
  local ok, e = xpcall(function() return fn(unpack(args, 1, n)) end, traceback)
  if not ok then M.err(where, e) end
  return ok
end

-- Who called a restricted or query API? The first Lua frame that is not mock
-- plumbing. The addon (GoblinLedger) must never be that frame.
local function guard(name, kind)
  for lvl = 3, 60 do
    local info = dbg.getinfo(lvl, "S")
    if not info then return end
    local src = info.source or ""
    if info.what ~= "C" and src:sub(1, 6) ~= "@MOCK/" then
      if src:find("/AddOns/GoblinLedger/", 1, true) then
        M.violations[#M.violations + 1] = kind .. " " .. name .. " called from " .. src .. ":" .. tostring(info.linedefined)
      end
      return
    end
  end
end
M.guard = guard

local function checkColor(where, ...)
  for i = 1, select("#", ...) do
    local v = select(i, ...)
    if v ~= nil and not finite(v) then apiError(where .. ": bad colour value " .. tostring(v)) end
  end
end

---------------------------------------------------------------------------
-- Widgets
---------------------------------------------------------------------------
local function isRegion(v) return type(v) == "table" and rawget(v, "_otype") ~= nil end
M.isRegion = isRegion

local Methods = {}
local function class(name, base)
  local t = {}
  if base then setmetatable(t, { __index = Methods[base] }) end
  Methods[name] = t
  return t
end
local Base = class("Base")
local Region = class("Region", "Base")
local Frame = class("Frame", "Region")
local Button = class("Button", "Frame")
local EditBox = class("EditBox", "Frame")
local Tooltip = class("GameTooltip", "Frame")
local Texture = class("Texture", "Region")
local Line = class("Line", "Base")
local FontString = class("FontString", "Region")
local MTs = {}
for k, v in pairs(Methods) do
  MTs[k] = { __index = v, __tostring = function(o) return k .. "<" .. tostring(rawget(o, "_name")) .. ">" end }
end

local function newObject(otype, parent, name)
  local o = setmetatable({ _otype = otype, _parent = parent, _points = {}, _shown = true,
                           _w = 0, _h = 0, _scale = 1, _alpha = 1 }, MTs[otype])
  if name then o._name = name; _G[name] = o end
  if parent then
    local ch = rawget(parent, "_children")
    if not ch then ch = {}; parent._children = ch end
    ch[#ch + 1] = o
  end
  M.counts[otype] = (M.counts[otype] or 0) + 1
  return o
end

local SCRIPTS = {}
SCRIPTS.Frame = { OnEvent = 1, OnUpdate = 1, OnShow = 1, OnHide = 1, OnEnter = 1, OnLeave = 1, OnMouseDown = 1,
  OnMouseUp = 1, OnMouseWheel = 1, OnDragStart = 1, OnDragStop = 1, OnSizeChanged = 1, OnLoad = 1,
  OnReceiveDrag = 1, OnAttributeChanged = 1 }
local function extend(base, extra) local t = {}; for k in pairs(base) do t[k] = 1 end; for _, k in ipairs(extra) do t[k] = 1 end; return t end
SCRIPTS.Button = extend(SCRIPTS.Frame, { "OnClick", "OnDoubleClick", "PreClick", "PostClick" })
SCRIPTS.EditBox = extend(SCRIPTS.Frame, { "OnEnterPressed", "OnEscapePressed", "OnTextChanged", "OnEditFocusGained",
  "OnEditFocusLost", "OnTabPressed", "OnChar" })
SCRIPTS.GameTooltip = extend(SCRIPTS.Frame, { "OnTooltipCleared", "OnTooltipSetDefaultAnchor", "OnTooltipAddMoney" })
SCRIPTS.GameTooltipLegacy = extend(SCRIPTS.GameTooltip, { "OnTooltipSetItem", "OnTooltipSetUnit", "OnTooltipSetSpell" })

local function scriptsFor(f)
  if f._otype == "GameTooltip" and M.client == "legacy" then return SCRIPTS.GameTooltipLegacy end
  return SCRIPTS[f._otype] or SCRIPTS.Frame
end

function M.fireScript(f, name, ...)
  local s = f._scripts and f._scripts[name]
  if s then M.call(name .. " script on " .. tostring(f), s, f, ...) end
  local hs = f._hooks and f._hooks[name]
  if hs then for _, h in ipairs(hs) do M.call(name .. " hook on " .. tostring(f), h, f, ...) end end
end

-- Base ---------------------------------------------------------------------
function Base:GetObjectType() return self._otype end
function Base:IsObjectType(t) return self._otype == t or (t == "Frame" and Methods[self._otype] and self._scripts ~= nil) or t == "Region" end
function Base:GetName() return self._name end
function Base:GetParent() return self._parent end
function Base:IsForbidden() return self._forbidden == true end
function Base:SetAlpha(a) if not finite(a) then apiError("SetAlpha: bad alpha") end; self._alpha = a end
function Base:GetAlpha() return self._alpha end
function Base:IsShown() return self._shown end
function Base:IsVisible()
  if not self._shown then return false end
  local p = self._parent
  if p then return p:IsVisible() end
  return true
end
function Base:Show()
  if self._shown then return end
  self._shown = true
  if self._scripts and self:IsVisible() then M.fireScript(self, "OnShow") end
end
function Base:Hide()
  if not self._shown then return end
  local wasVisible = self:IsVisible()
  self._shown = false
  if self._scripts and wasVisible then M.fireScript(self, "OnHide") end
end
function Base:SetShown(v) if v then self:Show() else self:Hide() end end
function Base:SetIgnoreParentAlpha() end
function Base:SetIgnoreParentScale() end

-- Region geometry ------------------------------------------------------------
local POINTS = { TOPLEFT = 1, TOP = 1, TOPRIGHT = 1, LEFT = 1, CENTER = 1, RIGHT = 1, BOTTOMLEFT = 1, BOTTOM = 1, BOTTOMRIGHT = 1 }

local function dependsOn(a, b, depth)
  depth = (depth or 0) + 1
  if depth > 60 then return true end
  if a == b then return true end
  for _, p in ipairs(rawget(a, "_points") or {}) do
    local rel = p[2] or a._parent
    if rel and dependsOn(rel, b, depth) then return true end
  end
  return false
end

function Region:SetPoint(point, a, b, c, d)
  if not POINTS[point] then apiError("SetPoint: invalid point " .. tostring(point)) end
  local rel, relPoint, x, y
  if type(a) == "number" then
    x, y = a, b
  else
    rel = a
    if type(b) == "number" then x, y, relPoint = b, c, nil else relPoint, x, y = b, c, d end
  end
  if type(rel) == "string" then
    local r = _G[rel]
    if not isRegion(r) then apiError("SetPoint: unknown region name " .. rel) end
    rel = r
  end
  if rel ~= nil and not isRegion(rel) then apiError("SetPoint: relativeTo is not a region") end
  if relPoint == nil then relPoint = point end
  if not POINTS[relPoint] then apiError("SetPoint: invalid relative point " .. tostring(relPoint)) end
  x, y = x or 0, y or 0
  if not finite(x) or not finite(y) then apiError("SetPoint: bad offset " .. tostring(x) .. "," .. tostring(y)) end
  if rel == self then apiError("SetPoint: cannot anchor to itself") end
  if rel and dependsOn(rel, self) then apiError("SetPoint: anchor family connection would create a loop") end
  for i, p in ipairs(self._points) do
    if p[1] == point then table.remove(self._points, i); break end
  end
  table.insert(self._points, { point, rel or false, relPoint, x, y })
end
function Region:ClearAllPoints() self._points = {} end
function Region:GetNumPoints() return #self._points end
function Region:GetPoint(i)
  local p = self._points[i or 1]
  if p then return p[1], p[2] or nil, p[3], p[4], p[5] end
end
function Region:SetAllPoints(rel)
  rel = rel or self._parent
  if type(rel) == "string" then rel = _G[rel] end
  self._points = {}
  self:SetPoint("TOPLEFT", rel, "TOPLEFT", 0, 0)
  self:SetPoint("BOTTOMRIGHT", rel, "BOTTOMRIGHT", 0, 0)
end
function M.sizeChanged(o)
  if o._scripts and (o._scripts.OnSizeChanged or (o._hooks and o._hooks.OnSizeChanged)) and not o._sizeQueued then
    o._sizeQueued = true
    M.sizeQueue[#M.sizeQueue + 1] = o
  end
end
function Region:SetWidth(w)
  if not finite(w) or w < 0 then apiError("SetWidth: bad width " .. tostring(w)) end
  local old = self._w; self._w = w
  if old ~= w then M.sizeChanged(self) end
end
function Region:SetHeight(h)
  if not finite(h) or h < 0 then apiError("SetHeight: bad height " .. tostring(h)) end
  local old = self._h; self._h = h
  if old ~= h then M.sizeChanged(self) end
end
function Region:SetSize(w, h)
  if not finite(w) or not finite(h) or w < 0 or h < 0 then apiError("SetSize: bad size " .. tostring(w) .. "x" .. tostring(h)) end
  local ow, oh = self._w, self._h
  self._w, self._h = w, h
  if ow ~= w or oh ~= h then M.sizeChanged(self) end
end

function M.eff(o)
  if o == M.UIParent then return o._scale end
  local s = o._scale or 1
  if o._otype == "Texture" or o._otype == "FontString" or o._otype == "Line" then s = 1 end
  if o._parent then return s * M.eff(o._parent) end
  return s
end

local function baseSize(o)
  local w, h = o._w, o._h
  if o._otype == "FontString" then
    if w == 0 then w = #(o._text or "") * 6 end
    if h == 0 then h = 12 end
  end
  return w, h
end

-- Screen rectangle (L, B, R, T) in effective-scale-1 units, or nil.
function M.rect(o, depth)
  depth = (depth or 0) + 1
  if depth > 60 then return nil end
  if o == M.UIParent or o == M.WorldFrame then return 0, 0, M.screenW, M.screenH end
  local pts = o._points
  if not pts or #pts == 0 then return nil end
  local es = M.eff(o)
  local L, R, CX, B, T, CY
  for _, p in ipairs(pts) do
    local rel = p[2] or o._parent or M.UIParent
    local rl, rb, rr, rt = M.rect(rel, depth)
    if not rl then return nil end
    local rp = p[3]
    local ax = rp:find("LEFT") and rl or (rp:find("RIGHT") and rr or (rl + rr) / 2)
    local ay = rp:find("TOP") and rt or (rp:find("BOTTOM") and rb or (rb + rt) / 2)
    ax, ay = ax + p[4] * es, ay + p[5] * es
    local pp = p[1]
    if pp:find("LEFT") then L = ax elseif pp:find("RIGHT") then R = ax else CX = ax end
    if pp:find("TOP") then T = ay elseif pp:find("BOTTOM") then B = ay else CY = ay end
  end
  local bw, bh = baseSize(o)
  local w, h = bw * es, bh * es
  if L and R then elseif L then R = L + w elseif R then L = R - w elseif CX then L, R = CX - w / 2, CX + w / 2 else return nil end
  if B and T then elseif B then T = B + h elseif T then B = T - h elseif CY then B, T = CY - h / 2, CY + h / 2 else return nil end
  return L, B, R, T
end
function Region:GetEffectiveScale() return M.eff(self) end
function Region:GetLeft() local l = M.rect(self); if l then return l / M.eff(self) end end
function Region:GetRight() local _, _, r = M.rect(self); if r then return r / M.eff(self) end end
function Region:GetBottom() local _, b = M.rect(self); if b then return b / M.eff(self) end end
function Region:GetTop() local _, _, _, t = M.rect(self); if t then return t / M.eff(self) end end
function Region:GetCenter()
  local l, b, r, t = M.rect(self)
  if l then local es = M.eff(self); return (l + r) / 2 / es, (b + t) / 2 / es end
end
function Region:GetRect()
  local l, b, r, t = M.rect(self)
  if l then local es = M.eff(self); return l / es, b / es, (r - l) / es, (t - b) / es end
end
function Region:GetWidth()
  local hasL, hasR = false, false
  for _, p in ipairs(self._points) do
    if p[1]:find("LEFT") then hasL = true elseif p[1]:find("RIGHT") then hasR = true end
  end
  if hasL and hasR then
    local l, _, r = M.rect(self)
    if l then return (r - l) / M.eff(self) end
  end
  return (baseSize(self))
end
function Region:GetHeight()
  local hasT, hasB = false, false
  for _, p in ipairs(self._points) do
    if p[1]:find("TOP") then hasT = true elseif p[1]:find("BOTTOM") then hasB = true end
  end
  if hasT and hasB then
    local _, b, _, t = M.rect(self)
    if b then return (t - b) / M.eff(self) end
  end
  local _, h = baseSize(self)
  return h
end
function Region:GetSize() return self:GetWidth(), self:GetHeight() end
function Region:IsMouseOver() return false end

-- Textures -------------------------------------------------------------------
local LAYERS = { BACKGROUND = 1, BORDER = 1, ARTWORK = 1, OVERLAY = 1, HIGHLIGHT = 1 }
local TextureBase = {}
function TextureBase:SetTexture(path)
  if path ~= nil and type(path) ~= "string" and type(path) ~= "number" then apiError("SetTexture: bad texture") end
  self._texture = path; self._color = nil
  return true
end
function TextureBase:GetTexture() return self._texture end
function TextureBase:SetColorTexture(r, g, b, a)
  if not finite(r) or not finite(g) or not finite(b) then apiError("SetColorTexture: bad colour") end
  checkColor("SetColorTexture", a)
  self._color = { r, g, b, a or 1 }
end
function TextureBase:SetVertexColor(r, g, b, a)
  if not finite(r) or not finite(g) or not finite(b) then apiError("SetVertexColor: bad colour") end
  checkColor("SetVertexColor", a)
  self._vertex = { r, g, b, a or 1 }
end
function TextureBase:SetTexCoord(...)
  local n = select("#", ...)
  if n ~= 4 and n ~= 8 then apiError("SetTexCoord: needs 4 or 8 numbers") end
  checkColor("SetTexCoord", ...)
end
function TextureBase:SetDrawLayer(layer, sub)
  if not LAYERS[layer] then apiError("SetDrawLayer: bad layer") end
  self._layer, self._sub = layer, sub
end
function TextureBase:SetBlendMode(m) self._blend = m end
function TextureBase:SetDesaturated(v) end
function TextureBase:SetAtlas(a) self._atlas = a end
function TextureBase:SetMask(m) end
for k, v in pairs(TextureBase) do Texture[k] = v; Line[k] = v end

function Line:SetStartPoint(relPoint, relTo, x, y)
  if not POINTS[relPoint] then apiError("SetStartPoint: bad point " .. tostring(relPoint)) end
  if not isRegion(relTo) then apiError("SetStartPoint: relativeTo must be a region") end
  if not finite(x or 0) or not finite(y or 0) then apiError("SetStartPoint: bad offset") end
  self._start = { relPoint, relTo, x or 0, y or 0 }
end
function Line:SetEndPoint(relPoint, relTo, x, y)
  if not POINTS[relPoint] then apiError("SetEndPoint: bad point " .. tostring(relPoint)) end
  if not isRegion(relTo) then apiError("SetEndPoint: relativeTo must be a region") end
  if not finite(x or 0) or not finite(y or 0) then apiError("SetEndPoint: bad offset") end
  self._end = { relPoint, relTo, x or 0, y or 0 }
end
function Line:GetStartPoint() local p = self._start; if p then return p[1], p[2], p[3], p[4] end end
function Line:GetEndPoint() local p = self._end; if p then return p[1], p[2], p[3], p[4] end end
function Line:SetThickness(t) if not finite(t) or t <= 0 then apiError("SetThickness: bad") end; self._thick = t end
function Line:GetThickness() return self._thick or 1 end
function Line:SetHitRectThickness(t) end
function Line:ClearAllPoints() self._start, self._end = nil, nil end

-- Font objects -------------------------------------------------------------------
-- A Font (the game's named fonts and CreateFont's) has a face, size, flags, a
-- colour and a shadow (CopyFontObject copies them all). The tooltip fonts are
-- ElvUI's (PT Sans Narrow 12, header 13, with its shadow), as the player has them.
local FONTS = {}
local Font = {}
local FontMT = { __index = Font, __tostring = function(o) return "Font<" .. tostring(o._name) .. ">" end }
local function newFont(name, file, size, flags, r, g, b)
  local f = setmetatable({ _isFont = true, _name = name, _file = file, _size = size, _flags = flags or "",
                           _color = { r or 1, g or 1, b or 1 }, _shadowOffset = { 0, 0 }, _shadowColor = { 0, 0, 0, 0 } },
                         FontMT)
  if name then _G[name] = f end
  return f
end
function Font:GetName() return self._name end
function Font:GetObjectType() return "Font" end
function Font:IsObjectType(t) return t == "Font" end
function Font:GetFont() return self._file, self._size, self._flags end
function Font:SetFont(file, size, flags)
  if type(file) ~= "string" or file == "" or not finite(size) or size <= 0 then apiError("Font:SetFont: bad arguments") end
  self._file, self._size, self._flags = file, size, flags or ""
  return true
end
function Font:CopyFontObject(src)
  if type(src) == "string" then src = _G[src] end
  if type(src) ~= "table" or not src._isFont then apiError("Font:CopyFontObject: not a font") end
  self._file, self._size, self._flags = src._file, src._size, src._flags
  self._color = { src._color[1], src._color[2], src._color[3] }
  self._shadowOffset = { src._shadowOffset[1], src._shadowOffset[2] }
  self._shadowColor = { src._shadowColor[1], src._shadowColor[2], src._shadowColor[3], src._shadowColor[4] }
end
function Font:SetTextColor(r, g, b)
  if not finite(r) or not finite(g) or not finite(b) then apiError("Font:SetTextColor: bad colour") end
  self._color = { r, g, b }
end
function Font:GetTextColor() return self._color[1], self._color[2], self._color[3], 1 end
function Font:SetShadowColor(r, g, b, a)
  if not finite(r) or not finite(g) or not finite(b) then apiError("Font:SetShadowColor: bad colour") end
  self._shadowColor = { r, g, b, a or 1 }
end
function Font:GetShadowColor() local c = self._shadowColor; return c[1], c[2], c[3], c[4] end
function Font:SetShadowOffset(x, y)
  if not finite(x) or not finite(y) then apiError("Font:SetShadowOffset: bad offset") end
  self._shadowOffset = { x, y }
end
function Font:GetShadowOffset() return self._shadowOffset[1], self._shadowOffset[2] end
function Font:SetJustifyH() end
function Font:SetJustifyV() end
for _, n in ipairs({ "GameFontNormal", "GameFontNormalSmall", "GameFontNormalLarge", "GameFontHighlight",
  "GameFontHighlightSmall", "GameFontHighlightLarge", "GameFontDisable", "GameFontDisableSmall", "NumberFontNormal",
  "GameTooltipTextSmall", "GameFontWhite", "GameFontNormalHuge", "SystemFont_Small" }) do
  FONTS[n] = newFont(n, "Fonts\\FRIZQT__.TTF", 12, "")
end
FONTS.GameTooltipText = newFont("GameTooltipText", "Fonts\\PTSansNarrow.ttf", 12, "")
FONTS.GameTooltipHeaderText = newFont("GameTooltipHeaderText", "Fonts\\PTSansNarrow.ttf", 13, "")
for _, n in ipairs({ "GameTooltipText", "GameTooltipHeaderText" }) do   -- ElvUI's SHADOW style
  FONTS[n]:SetShadowOffset(1, -1)
  FONTS[n]:SetShadowColor(0, 0, 0, 1)
end
M.fontsCreated = 0
function CreateFont(name)
  if name ~= nil and type(name) ~= "string" then apiError("CreateFont: bad name") end
  M.fontsCreated = M.fontsCreated + 1
  return newFont(name, "Fonts\\FRIZQT__.TTF", 12, "")
end

-- Font strings -----------------------------------------------------------------
function FontString:SetText(t)
  if t ~= nil and type(t) ~= "string" and type(t) ~= "number" then apiError("FontString:SetText: bad text " .. type(t)) end
  self._text = t ~= nil and tostring(t) or nil
  M.seeText(self._text)
end
function FontString:SetFormattedText(fmt, ...) self:SetText(string.format(fmt, ...)) end
function FontString:GetText() return self._text end
function FontString:SetTextColor(r, g, b, a)
  if not finite(r) or not finite(g) or not finite(b) then apiError("SetTextColor: bad colour") end
  checkColor("SetTextColor", a)
  self._textColor = { r, g, b, a or 1 }
end
function FontString:GetTextColor() local c = self._textColor or { 1, 1, 1, 1 }; return c[1], c[2], c[3], c[4] end
local JH = { LEFT = 1, CENTER = 1, RIGHT = 1 }
local JV = { TOP = 1, MIDDLE = 1, BOTTOM = 1 }
function FontString:SetJustifyH(j) if not JH[j] then apiError("SetJustifyH: bad value " .. tostring(j)) end; self._jh = j end
function FontString:SetJustifyV(j) if not JV[j] then apiError("SetJustifyV: bad value " .. tostring(j)) end; self._jv = j end
function FontString:SetWordWrap(v) self._wrap = v end
function FontString:SetNonSpaceWrap(v) end
function FontString:SetMaxLines(n) end
-- A tooltip line's FontString (_ttLine) is modelled closely: SetFontObject
-- takes the Font's colour (as in the game, so a colour must be set again
-- after it), and SetFont on one is a violation (the addon must never do it).
function FontString:SetFontObject(f)
  if self._ttLine then
    if type(f) == "string" then f = _G[f] end
    if type(f) ~= "table" or not f._isFont then apiError("FontString:SetFontObject: not a font") end
    self._font, self._ownFont = f, nil
    self._textColor = { f._color[1], f._color[2], f._color[3], 1 }
    return
  end
  self._font = f
end
function FontString:GetFontObject()
  local f = self._font
  if type(f) == "string" then f = _G[f] end
  return f
end
function FontString:GetFont()
  if self._ownFont then return self._ownFont[1], self._ownFont[2], self._ownFont[3] end
  local f = self:GetFontObject()
  if type(f) == "table" and f._isFont then return f:GetFont() end
  return "Fonts\\FRIZQT__.TTF", 12, ""
end
function FontString:SetFont(path, size, flags)
  if self._ttLine then
    M.violations[#M.violations + 1] = "SetFont on tooltip line " .. tostring(self._name)
    self._ownFont = { path, size, flags or "" }
  end
  self._fontPath = path
  return true
end
function FontString:SetShadowColor() end
function FontString:SetShadowOffset() end
function FontString:SetSpacing() end
function FontString:GetStringWidth() local s = (self._text or ""):gsub("|c%x%x%x%x%x%x%x%x", ""):gsub("|r", ""); return #s * 6 end
function FontString:GetStringHeight() return 12 end
function FontString:SetDrawLayer(layer, sub) if not LAYERS[layer] then apiError("bad layer") end end

-- Frames -------------------------------------------------------------------------
local STRATA = { BACKGROUND = 1, LOW = 1, MEDIUM = 1, HIGH = 1, DIALOG = 1, FULLSCREEN = 1, FULLSCREEN_DIALOG = 1, TOOLTIP = 1 }
function Frame:SetScript(name, fn)
  if not scriptsFor(self)[name] then apiError(self._otype .. ':SetScript(): Unknown script "' .. tostring(name) .. '"') end
  if fn ~= nil and type(fn) ~= "function" then apiError("SetScript: handler must be a function") end
  self._scripts[name] = fn
  -- Like the game: SetScript also drops the hooks HookScript added to it.
  self._hooks[name] = nil
end
function Frame:GetScript(name) return self._scripts[name] end
function Frame:HasScript(name) return scriptsFor(self)[name] ~= nil end
function Frame:HookScript(name, fn)
  if not scriptsFor(self)[name] then apiError(self._otype .. ':HookScript(): Unknown script "' .. tostring(name) .. '"') end
  if type(fn) ~= "function" then apiError("HookScript: handler must be a function") end
  self._hooks[name] = self._hooks[name] or {}
  table.insert(self._hooks[name], fn)
end
function Frame:RegisterEvent(ev)
  if type(ev) ~= "string" then apiError("RegisterEvent: Usage") end
  if not M.knownEvents[ev] then apiError('Frame:RegisterEvent(): Attempt to register unknown event "' .. ev .. '"') end
  self._events[ev] = true
  if not self._inEventList then self._inEventList = true; table.insert(M.eventFrames, self) end
  return true
end
function Frame:RegisterUnitEvent(ev) return self:RegisterEvent(ev) end
function Frame:UnregisterEvent(ev) self._events[ev] = nil end
function Frame:UnregisterAllEvents() self._events = {} end
function Frame:IsEventRegistered(ev) return self._events[ev] == true end
function Frame:CreateTexture(name, layer, template, sub)
  if layer ~= nil and not LAYERS[layer] then apiError("CreateTexture: bad layer " .. tostring(layer)) end
  if sub ~= nil and (not finite(sub) or sub < -8 or sub > 7) then apiError("CreateTexture: bad sublevel " .. tostring(sub)) end
  local t = newObject("Texture", self, name)
  t._layer, t._sub = layer or "ARTWORK", sub
  return t
end
function Frame:CreateLine(name, layer, template, sub)
  if layer ~= nil and not LAYERS[layer] then apiError("CreateLine: bad layer " .. tostring(layer)) end
  if sub ~= nil and (not finite(sub) or sub < -8 or sub > 7) then apiError("CreateLine: bad sublevel " .. tostring(sub)) end
  local l = newObject("Line", self, name)
  l._layer, l._sub = layer or "ARTWORK", sub
  return l
end
function Frame:CreateFontString(name, layer, font)
  if layer ~= nil and not LAYERS[layer] then apiError("CreateFontString: bad layer " .. tostring(layer)) end
  if font ~= nil and not FONTS[font] then apiError('CreateFontString: Couldn\'t find inherited node "' .. tostring(font) .. '"') end
  local fs = newObject("FontString", self, name)
  fs._font = font
  return fs
end
function Frame:SetFrameStrata(s) if not STRATA[s] then apiError("SetFrameStrata: bad strata " .. tostring(s)) end; self._strata = s end
function Frame:GetFrameStrata() return self._strata or "MEDIUM" end
function Frame:SetFrameLevel(l) if not finite(l) or l < 0 or l > 10000 then apiError("SetFrameLevel: bad level " .. tostring(l)) end; self._level = l end
function Frame:GetFrameLevel() return self._level or 0 end
function Frame:SetToplevel(v) end
function Frame:Raise() end
function Frame:Lower() end
function Frame:EnableMouse(v) self._mouse = v and true or false end
function Frame:IsMouseEnabled() return self._mouse == true end
function Frame:EnableMouseWheel(v) end
function Frame:EnableKeyboard(v) end
function Frame:SetMovable(v) self._movable = v and true or false end
function Frame:IsMovable() return self._movable == true end
function Frame:SetResizable(v) end
function Frame:RegisterForDrag(...) self._drag = { ... } end
function Frame:StartMoving()
  if not self._movable then apiError("StartMoving: frame is not movable") end
  self._moving = true
end
function Frame:StopMovingOrSizing()
  if self._moving and M.dragTo then
    self._points = {}
    self:SetPoint(M.dragTo[1], M.UIParent, M.dragTo[2], M.dragTo[3], M.dragTo[4])
  end
  self._moving = false
end
function Frame:SetUserPlaced(v) end
function Frame:SetDontSavePosition(v) end
function Frame:SetClampedToScreen(v) self._clamped = v end
function Frame:SetHitRectInsets() end
function Frame:SetScale(s)
  if not finite(s) or s <= 0 then apiError("SetScale: scale must be > 0, got " .. tostring(s)) end
  self._scale = s
end
function Frame:GetScale() return self._scale end
function Frame:SetID(i) self._id = i end
function Frame:GetID() return self._id or 0 end
function Frame:GetChildren() local out = {}; for _, c in ipairs(self._children or {}) do if c._scripts then out[#out + 1] = c end end; return unpack(out) end
function Frame:SetAttribute(k, v) self._attr = self._attr or {}; self._attr[k] = v end
function Frame:GetAttribute(k) return self._attr and self._attr[k] end

local Backdrop = {}
function Backdrop:SetBackdrop(b) if b ~= nil and type(b) ~= "table" then apiError("SetBackdrop: bad backdrop") end; self._backdrop = b end
function Backdrop:SetBackdropColor(r, g, b, a) if not finite(r) or not finite(g) or not finite(b) then apiError("SetBackdropColor: bad colour") end end
function Backdrop:SetBackdropBorderColor(r, g, b, a) if not finite(r) or not finite(g) or not finite(b) then apiError("SetBackdropBorderColor: bad colour") end end
BackdropTemplateMixin = Backdrop

-- Buttons -----------------------------------------------------------------------
function Button:SetText(t)
  if t ~= nil and type(t) ~= "string" and type(t) ~= "number" then apiError("Button:SetText: bad text") end
  self._text = t ~= nil and tostring(t) or nil
  M.seeText(self._text)
  if self._fontString then self._fontString:SetText(t) end
end
function Button:GetText() return self._text end
function Button:SetFontString(fs) if not isRegion(fs) or fs._otype ~= "FontString" then apiError("SetFontString: bad font string") end; self._fontString = fs end
function Button:GetFontString() return self._fontString end
function Button:SetNormalFontObject(f) end
function Button:SetHighlightFontObject(f) end
function Button:SetDisabledFontObject(f) end
function Button:SetNormalTexture(t) end
function Button:SetHighlightTexture(t) end
function Button:SetPushedTexture(t) end
function Button:SetDisabledTexture(t) end
function Button:RegisterForClicks(...) end
function Button:Enable() self._disabled = false end
function Button:Disable() self._disabled = true end
function Button:IsEnabled() return not self._disabled end
function Button:SetEnabled(v) self._disabled = not v end
function Button:LockHighlight() end
function Button:UnlockHighlight() end
function Button:Click(btn)
  if self._disabled then return end
  M.fireScript(self, "PreClick", btn or "LeftButton", false)
  M.fireScript(self, "OnClick", btn or "LeftButton", false)
  M.fireScript(self, "PostClick", btn or "LeftButton", false)
end

-- Edit boxes --------------------------------------------------------------------
function EditBox:SetText(t) self._text = t ~= nil and tostring(t) or ""; M.fireScript(self, "OnTextChanged", false) end
function EditBox:GetText() return self._text or "" end
function EditBox:GetNumber() return tonumber(self._text or "") or 0 end
function EditBox:SetNumber(n) self:SetText(tostring(n)) end
function EditBox:SetFocus() self._focus = true end
function EditBox:ClearFocus() self._focus = false end
function EditBox:HasFocus() return self._focus == true end
function EditBox:SetAutoFocus(v) end
function EditBox:SetMaxLetters(n) end
function EditBox:SetNumeric(v) end
function EditBox:SetCursorPosition(n) end
function EditBox:Insert(t) self:SetText((self._text or "") .. t) end
function EditBox:SetFontObject(f) end
function EditBox:SetTextInsets() end

-- Tooltips ------------------------------------------------------------------------
-- Each line has a left and a right FontString (_G[name .. "TextLeft" .. i], and
-- GetLeftLine / GetRightLine). GameTooltipTemplate makes 8: line 1 on
-- GameTooltipHeaderText, 2 to 8 on GameTooltipText. A line past those is made
-- when first needed and copies the Font of the line above it. Line
-- FontStrings are reused: clearing the tooltip never resets their Font.
M.TT_TEMPLATE_LINES = 8
local function newLineFS(tt, side, k, font)
  local fs = setmetatable({ _otype = "FontString", _parent = tt, _points = {}, _shown = true, _w = 0, _h = 0,
                            _scale = 1, _alpha = 1, _ttLine = true, _font = font,
                            _textColor = { font._color[1], font._color[2], font._color[3], 1 } }, MTs.FontString)
  if tt._name then
    fs._name = tt._name .. "Text" .. side .. k
    _G[fs._name] = fs
  end
  M.counts.TooltipLine = (M.counts.TooltipLine or 0) + 1
  return fs
end
local function ttLine(tt, i)
  local L, R = tt._L, tt._R
  if not L then return nil end
  for k = #L + 1, i do
    local lf, rf
    if k == 1 then
      lf, rf = FONTS.GameTooltipHeaderText, FONTS.GameTooltipHeaderText
    elseif k <= M.TT_TEMPLATE_LINES then
      lf, rf = FONTS.GameTooltipText, FONTS.GameTooltipText
    else
      lf, rf = L[k - 1]._font, R[k - 1]._font
    end
    L[k], R[k] = newLineFS(tt, "Left", k, lf), newLineFS(tt, "Right", k, rf)
  end
  return L[i], R[i]
end
M.ttLine = ttLine
function Tooltip:GetLeftLine(i) return self._L and self._L[i] end
function Tooltip:GetRightLine(i) return self._R and self._R[i] end
-- Font name, size and colour of each side of line i (for the checks). The
-- FontString methods themselves, not a test's override on one line.
function M.lineStyle(tt, i)
  local L, R = tt._L and tt._L[i], tt._R and tt._R[i]
  if not L then return nil end
  local function one(fs)
    local f = FontString.GetFontObject(fs)
    local _, size = FontString.GetFont(fs)
    local c = fs._textColor
    return { font = type(f) == "table" and f._name or tostring(f), size = size, r = c[1], g = c[2], b = c[3] }
  end
  return { left = one(L), right = one(R) }
end
function Tooltip:_Clear()
  local n = #(self._lines or {})
  self._lines = {}
  self._blz = 0
  if self._L then
    for i = 1, n do
      if self._L[i] then self._L[i]:SetText(nil); self._R[i]:SetText(nil) end
    end
  end
  M.fireScript(self, "OnTooltipCleared")
end
function Tooltip:SetOwner(owner, anchor)
  if not isRegion(owner) then apiError("GameTooltip:SetOwner(): owner must be a frame") end
  self._owner, self._anchor = owner, anchor
  if M.setOwnerClears then self:_Clear() end
  self._shown = false
end
function Tooltip:GetOwner() return self._owner end
function Tooltip:SetAnchorType(a) self._anchor = a end
function Tooltip:ClearLines() self:_Clear() end
function Tooltip:AddLine(text, r, g, b, wrap)
  checkColor("GameTooltip:AddLine", r, g, b)
  if text ~= nil and type(text) ~= "string" and type(text) ~= "number" then apiError("AddLine: bad text") end
  local s = text ~= nil and tostring(text) or ""
  M.seeText(s)
  table.insert(self._lines, { left = s, lr = r, lg = g, lb = b })
  local L, R = ttLine(self, #self._lines)
  if L then
    L:SetText(s); L:SetTextColor(r or 1, g or 0.82, b or 0)   -- no colour: the game's gold
    R:SetText(nil)
  end
end
function Tooltip:AddDoubleLine(lt, rt, lr, lg, lb, rr, rg, rb)
  checkColor("GameTooltip:AddDoubleLine", lr, lg, lb, rr, rg, rb)
  local l = lt ~= nil and tostring(lt) or ""
  local r = rt ~= nil and tostring(rt) or ""
  M.seeText(l); M.seeText(r)
  table.insert(self._lines, { left = l, right = r, lr = lr, lg = lg, lb = lb, rr = rr, rg = rg, rb = rb })
  local L, R = ttLine(self, #self._lines)
  if L then
    L:SetText(l); L:SetTextColor(lr or 1, lg or 0.82, lb or 0)
    R:SetText(r); R:SetTextColor(rr or 1, rg or 0.82, rb or 0)
  end
end
function Tooltip:SetText(t, r, g, b, a, wrap) self:_Clear(); self:AddLine(t, r, g, b, wrap) end
function Tooltip:NumLines() return #self._lines end
function Tooltip:SetMinimumWidth() end
function Tooltip:SetPadding() end
function Tooltip:FadeOut() end
function Tooltip:Hide()
  if self._shown then Base.Hide(self) end
  self:_Clear()
end
function Tooltip:GetItem()
  local info = self.processingInfo
  local d = info and info.tooltipData
  if d and d.id then
    local it = M["items"][d.id]
    return it and it.name or "", d.link or M.ItemLink(d.id), d.id
  end
  if self._legacyItem then
    local id = self._legacyItem
    local it = M["items"][id]
    return it and it.name or "", M.ItemLink(id), id
  end
  return nil, nil, nil
end

-- What C_TooltipInfo would return for a getter.
function M.TooltipData(getterName, args)
  local id, guid, count, link
  if getterName == "GetBagItem" then
    local slot = M.bags[args[1]] and M.bags[args[1]][args[2]]
    if not slot then return nil end
    id = slot.itemID
    link = M.SlotLink(slot)
    guid = "Item-1-0-" .. args[1] .. "-" .. args[2]
    M.guids[guid] = ItemLocation:CreateFromBagAndSlot(args[1], args[2])
  elseif getterName == "GetItemByID" then
    id = args[1]
  elseif getterName == "GetHyperlink" then
    id = tonumber(tostring(args[1]):match("item:(%d+)"))
    link = tostring(args[1])   -- the very link, bonus ids and all
  elseif getterName == "GetInventoryItem" then
    id = M.equipped and M.equipped[args[2]]
  end
  if not id then return nil end
  local it = M["items"][id]
  local lines = { { type = 0, leftText = it and it.name or "Retrieving item information" } }
  if it and it.class then lines[2] = { type = 0, leftText = it.class } end
  -- more of the game's own lines (stats, "Sell Price") when a scenario asks for them
  for _, t in ipairs(M.extraGameLines or {}) do lines[#lines + 1] = { type = 0, leftText = t } end
  return { type = Enum.TooltipDataType and Enum.TooltipDataType.Item or 0, id = id, guid = guid,
           hyperlink = (not M.noHyperlink) and (link or M.ItemLink(id)) or nil, link = link or M.ItemLink(id), lines = lines }
end

-- Modern TooltipDataHandlerMixin:ProcessInfo, as in the Forever FrameXML.
function Tooltip:ProcessInfo(info)
  if M.client == "legacy" then return self:_LegacyProcess(info) end
  if not info.tooltipData then info.tooltipData = M.TooltipData(info.getterName, info.getterArgs or {}) end
  local d = info.tooltipData
  if not d then
    if not info.append then self:Hide() end
    return false
  end
  if not info.append then
    self.infoList = {}
    self:ClearLines()
  end
  table.insert(self.infoList, info)
  self.processingInfo = info
  for _, ln in ipairs(d.lines) do self:AddLine(ln.leftText, 1, 1, 1) end
  self._blz = #self._lines
  for _, key in ipairs({ d.type, "ALL" }) do
    for _, fn in ipairs(M.postCalls[key] or {}) do M.call("TooltipDataProcessor post call", fn, self, d) end
  end
  Base.Show(self)
  return true
end
function Tooltip:_LegacyProcess(info)
  local d = M.TooltipData(info.getterName, info.getterArgs or {})
  self:ClearLines()
  if not d then return false end
  self._legacyItem = d.id
  self.legacyInfo = info
  for _, ln in ipairs(d.lines) do self:AddLine(ln.leftText, 1, 1, 1) end
  self._blz = #self._lines
  M.fireScript(self, "OnTooltipSetItem")
  Base.Show(self)
  return true
end
function Tooltip:RebuildFromTooltipInfo()
  local list = self.infoList
  if not list or not list[1] then
    if self.legacyInfo then return self:_LegacyProcess(self.legacyInfo) end
    return
  end
  self.infoList = {}
  for i, info in ipairs(list) do
    if info.getterName then info.tooltipData = nil end
    if i == 1 then info.append = false end
    self:ProcessInfo(info)
  end
end
function Tooltip:RefreshData() self:RebuildFromTooltipInfo() end
function Tooltip:SetBagItem(bag, slot) return self:ProcessInfo({ getterName = "GetBagItem", getterArgs = { bag, slot } }) end
function Tooltip:SetItemByID(id) return self:ProcessInfo({ getterName = "GetItemByID", getterArgs = { id } }) end
function Tooltip:SetHyperlink(link) return self:ProcessInfo({ getterName = "GetHyperlink", getterArgs = { link } }) end
function Tooltip:SetInventoryItem(unit, slot) return self:ProcessInfo({ getterName = "GetInventoryItem", getterArgs = { unit, slot } }) end

---------------------------------------------------------------------------
-- CreateFrame and templates
---------------------------------------------------------------------------
local TEMPLATES = {}
TEMPLATES.BackdropTemplate = function(f) for k, v in pairs(Backdrop) do f[k] = v end end
TEMPLATES.UIPanelButtonTemplate = function(f)
  if f._otype ~= "Button" then apiError("UIPanelButtonTemplate needs a Button") end
  f.Left = f:CreateTexture(nil, "BACKGROUND")
  f.Middle = f:CreateTexture(nil, "BACKGROUND")
  f.Right = f:CreateTexture(nil, "BACKGROUND")
  f.Text = f:CreateFontString(nil, "ARTWORK", "GameFontNormal")
  f.Text:SetPoint("CENTER", f, "CENTER", 0, 0)
  f:SetFontString(f.Text)
end
TEMPLATES.UIPanelCloseButton = function(f) end
TEMPLATES.GameTooltipTemplate = function(f)
  if f._otype ~= "GameTooltip" then apiError("GameTooltipTemplate needs a GameTooltip") end
  f._lines, f._blz = {}, 0
  f._L, f._R = {}, {}
  ttLine(f, M.TT_TEMPLATE_LINES)
end
TEMPLATES.SearchBoxTemplate = function(f) end
TEMPLATES.InputBoxTemplate = function(f) end

local FRAME_TYPES = { frame = "Frame", button = "Button", checkbutton = "Button", editbox = "EditBox",
  gametooltip = "GameTooltip", statusbar = "Frame", scrollframe = "Frame", slider = "Frame" }

function CreateFrame(ftype, name, parent, template, id)
  if type(ftype) ~= "string" then apiError("CreateFrame(): Usage: CreateFrame(frameType [, name, parent, template, id])") end
  local ot = FRAME_TYPES[ftype:lower()]
  if not ot then apiError('CreateFrame(): Unknown frame type "' .. ftype .. '"') end
  if name ~= nil and type(name) ~= "string" then apiError("CreateFrame(): bad name") end
  if type(parent) == "string" then parent = _G[parent] end
  if parent ~= nil and (not isRegion(parent) or parent._scripts == nil) then apiError("CreateFrame(): parent must be a frame") end
  local tlist = {}
  if template ~= nil then
    if type(template) ~= "string" then apiError("CreateFrame(): bad template") end
    for t in template:gmatch("[^,%s]+") do
      if not TEMPLATES[t] then apiError('CreateFrame(): Couldn\'t find inherited node "' .. t .. '"') end
      tlist[#tlist + 1] = t
    end
  end
  local f = newObject(ot, parent, name)
  f._scripts, f._hooks, f._events = {}, {}, {}
  f._level = parent and ((parent._level or 0) + 1) or 0
  f._strata = parent and parent._strata or "MEDIUM"
  f._id = id
  M.frames[#M.frames + 1] = f
  for _, t in ipairs(tlist) do TEMPLATES[t](f) end
  return f
end

UIParent = CreateFrame("Frame", "UIParent")
UIParent._scale = M.uiParentScale
UIParent._w, UIParent._h = M.screenW, M.screenH
M.UIParent = UIParent
WorldFrame = CreateFrame("Frame", "WorldFrame")
M.WorldFrame = WorldFrame
function M.SetScreen(w, h, uiScale)
  M.screenW, M.screenH, M.uiParentScale = w, h, uiScale
  UIParent._scale = uiScale
  UIParent._w, UIParent._h = w / uiScale, h / uiScale
end
UISpecialFrames = {}

for _, name in ipairs({ "GameTooltip", "ItemRefTooltip", "ShoppingTooltip1", "ShoppingTooltip2",
  "ItemRefShoppingTooltip1", "ItemRefShoppingTooltip2", "OtherAddonTooltip", "ForbiddenTooltip" }) do
  local tt = CreateFrame("GameTooltip", name, UIParent, "GameTooltipTemplate")
  tt._shown = false
  tt._strata = "TOOLTIP"
  -- Blizzard's tooltip template has its own handlers; the data handler state is dropped on hide.
  tt:SetScript("OnTooltipCleared", function(self) end)
  tt:SetScript("OnHide", function(self) self.infoList = nil; self.processingInfo = nil end)
end
ForbiddenTooltip._forbidden = true

DEFAULT_CHAT_FRAME = { AddMessage = function(self, msg, r, g, b)
  M.seeText(msg)
  M.chat[#M.chat + 1] = tostring(msg)
end }
print = function(...)
  local parts = {}
  for i = 1, select("#", ...) do parts[i] = tostring((select(i, ...))) end
  DEFAULT_CHAT_FRAME:AddMessage(table.concat(parts, " "))
end

---------------------------------------------------------------------------
-- Globals and C_ namespaces
---------------------------------------------------------------------------
-- Post-hook: the original runs first, then the hook, and the original's returns are kept.
function hooksecurefunc(a, b, c)
  local tbl, name, fn
  if type(a) == "string" then tbl, name, fn = _G, a, b else tbl, name, fn = a, b, c end
  if type(tbl) ~= "table" or type(name) ~= "string" or type(fn) ~= "function" then
    apiError('Usage: hooksecurefunc([table,] "functionName", hookfunc)')
  end
  local orig = tbl[name]
  if type(orig) ~= "function" then apiError("hooksecurefunc(): " .. name .. " is not a function") end
  tbl[name] = function(...)
    local res = { orig(...) }
    fn(...)
    return unpack(res)
  end
  M.hookCount = M.hookCount + 1
end

C_Timer = {}
function C_Timer.After(sec, fn)
  if not finite(sec) or sec < 0 then apiError("C_Timer.After: bad seconds " .. tostring(sec)) end
  if type(fn) ~= "function" then apiError("C_Timer.After: callback must be a function") end
  M.timerSeq = M.timerSeq + 1
  M.timers[#M.timers + 1] = { at = M.t + sec, fn = fn, seq = M.timerSeq }
end
function C_Timer.NewTimer(sec, fn)
  local h = { cancelled = false }
  function h:Cancel() self.cancelled = true end
  function h:IsCancelled() return self.cancelled end
  C_Timer.After(sec, function() if not h.cancelled then fn(h) end end)
  return h
end
function C_Timer.NewTicker(sec, fn, n)
  local h = { cancelled = false, left = n }
  function h:Cancel() self.cancelled = true end
  function h:IsCancelled() return self.cancelled end
  local function tick()
    if h.cancelled then return end
    fn(h)
    if h.left then h.left = h.left - 1; if h.left <= 0 then return end end
    C_Timer.After(sec, tick)
  end
  C_Timer.After(sec, tick)
  return h
end

function M.tick(dt)
  dt = dt or 0.02
  M.t = M.t + dt
  local q = M.sizeQueue
  M.sizeQueue = {}
  for _, o in ipairs(q) do
    o._sizeQueued = nil
    M.fireScript(o, "OnSizeChanged", o._w, o._h)
  end
  local due, keep = {}, {}
  for _, tm in ipairs(M.timers) do
    if tm.at <= M.t + 1e-9 then due[#due + 1] = tm else keep[#keep + 1] = tm end
  end
  M.timers = keep
  table.sort(due, function(a, b) if a.at ~= b.at then return a.at < b.at end return a.seq < b.seq end)
  for _, tm in ipairs(due) do M.call("C_Timer callback", tm.fn) end
  local list = {}
  for i, f in ipairs(M.frames) do list[i] = f end
  for _, f in ipairs(list) do
    local s = f._scripts and f._scripts.OnUpdate
    if s and f:IsVisible() then M.call("OnUpdate on " .. tostring(f), s, f, dt) end
  end
end
function M.settle(sec)
  local n = math.max(1, math.ceil((sec or 0.5) / 0.05))
  for _ = 1, n do M.tick(0.05) end
end

function M.fire(ev, ...)
  if not M.knownEvents[ev] then M.err("harness", "fired unknown event " .. ev) end
  local list = {}
  for i, f in ipairs(M.eventFrames) do list[i] = f end
  for _, f in ipairs(list) do
    if f._events[ev] then
      local s = f._scripts.OnEvent
      if s then M.call("OnEvent " .. ev .. " on " .. tostring(f), s, f, ev, ...) end
      local hs = f._hooks.OnEvent
      if hs then for _, h in ipairs(hs) do M.call("OnEvent hook " .. ev, h, f, ev, ...) end end
    end
  end
end

function M.slash(text)
  local cmd, rest = text:match("^(/%S+)%s*(.-)$")
  if not cmd then M.err("harness", "bad slash text " .. text); return false end
  cmd = cmd:lower()
  for key, fn in pairs(SlashCmdList) do
    local i = 1
    while true do
      local s = _G["SLASH_" .. key .. i]
      if not s then break end
      if s:lower() == cmd then
        M.call("slash " .. text, fn, rest, nil)
        return true
      end
      i = i + 1
    end
  end
  M.chat[#M.chat + 1] = "Type /help for a listing of a few commands."
  return false
end
SlashCmdList = {}

-- A click from the player is a hardware event while it runs (M.hw); timers,
-- events, OnUpdate, menu callbacks and Lua calls to :Click() are not.
function M.click(btn, mouse)
  if not isRegion(btn) then M.err("harness", "click on a non-button"); return false end
  if not btn:IsVisible() then M.err("harness", "clicked a button that is not visible: " .. tostring(btn)); return false end
  local prev = M.hw
  M.hw = { button = btn, deletes = 0 }
  btn:Click(mouse)
  M.hw = prev
  return true
end
function M.enter(f) M.fireScript(f, "OnEnter", true) end
function M.leave(f) M.fireScript(f, "OnLeave", true) end
function M.pressEscape()
  for _, name in ipairs(UISpecialFrames) do
    local f = _G[name]
    if isRegion(f) and f:IsShown() then f:Hide(); return name end
  end
end
function M.drag(f, point, relPoint, x, y)
  M.dragTo = { point, relPoint, x, y }
  M.fireScript(f, "OnDragStart", "LeftButton")
  M.fireScript(f, "OnDragStop")
  M.dragTo = nil
end
function M.lineCount(tt) return #tt._lines end
function M.addonLines(tt)
  local out = {}
  for i = (tt._blz or 0) + 1, #tt._lines do out[#out + 1] = tt._lines[i] end
  return out
end

function GetCursorPosition() return M.cursorX, M.cursorY end
function GetTime() return M.t end
function GetServerTime() return M.now end
function time(t) if t then return os_time(t) end return M.now end
function date(fmt, t) return os_date(fmt, t or M.now) end
function difftime(a, b) return a - b end
function IsShiftKeyDown() return M.shift end
function IsControlKeyDown() return false end
function IsAltKeyDown() return false end
function IsModifiedClick() return false end
function UnitFactionGroup(unit)
  if unit == "player" then return M.faction, M.faction end
  if unit == "npc" and M.npc then return M.npc.faction, M.npc.faction end
end
-- Race and class: name, file name and id (quest items: which quests are yours).
M.race, M.class = { "Human", "Human", 1 }, { "Warrior", "WARRIOR", 1 }
function UnitRace(unit) if unit == "player" then return M.race[1], M.race[2], M.race[3] end end
function UnitClass(unit) if unit == "player" then return M.class[1], M.class[2], M.class[3] end end
function UnitExists(unit) return unit == "player" or (unit == "npc" and M.npc ~= nil and M.ahOpenNPC == true) end
function GetSubZoneText() return M.subzone or "" end
function GetMinimapZoneText() return M.subzone or M.zone or "" end
function GetZoneText() return M.zone or "" end
function UnitName(unit) return "Player" end
function GetBuildInfo() return "1.60.1", "70959", "Nov 17 2026", 16001 end
WOW_PROJECT_CAMELOT = 18
WOW_PROJECT_ID = 18
function issecretvalue(v) return false end
function issecure() return false end
function securecallfunction(f, ...) return f(...) end
function geterrorhandler() return function(e) M.err("error handler", e) end end
function seterrorhandler() end
function PlaySound() end
-- The gold on the character (123g 45s 67c unless a scenario says otherwise).
M.playerMoney = 1234567
function GetMoney() return M.playerMoney end
function strtrim(s) return (s:gsub("^%s+", ""):gsub("%s+$", "")) end
function strsplit(sep, s) local out = {}; for p in (s .. sep):gmatch("(.-)" .. sep) do out[#out + 1] = p end; return unpack(out) end
function wipe(t) for k in pairs(t) do t[k] = nil end; return t end
tinsert, tremove = table.insert, table.remove
function BreakUpLargeNumbers(n)
  local s = tostring(floor(n))
  local out = s:reverse():gsub("(%d%d%d)", "%1,"):reverse()
  if out:sub(1, 1) == "," then out = out:sub(2) end
  return out
end
local GOLD_I = "|TInterface\\MoneyFrame\\UI-GoldIcon:0:0:2:0|t"
local SILVER_I = "|TInterface\\MoneyFrame\\UI-SilverIcon:0:0:2:0|t"
local COPPER_I = "|TInterface\\MoneyFrame\\UI-CopperIcon:0:0:2:0|t"
local function coinString(money, fontHeight)
  if not finite(money) or money < 0 or money ~= floor(money) then apiError("GetCoinTextureString: bad amount " .. tostring(money)) end
  local g, s, c = floor(money / 10000), floor((money % 10000) / 100), money % 100
  local parts = {}
  if g > 0 then parts[#parts + 1] = BreakUpLargeNumbers(g) .. GOLD_I end
  if s > 0 then parts[#parts + 1] = s .. SILVER_I end
  if c > 0 or money == 0 then parts[#parts + 1] = c .. COPPER_I end
  return table.concat(parts, " ")
end
C_CurrencyInfo = { GetCoinTextureString = coinString, GetCoinText = function(m) return tostring(m) end }
function GetMoneyString(money, sep) return coinString(money) end
function M.InstallBareCoinGlobal() GetCoinTextureString = coinString end

ITEM_QUALITY_COLORS = {}
local QC = { [0] = { 0.62, 0.62, 0.62 }, [1] = { 1, 1, 1 }, [2] = { 0.12, 1, 0 }, [3] = { 0, 0.44, 0.87 },
  [4] = { 0.64, 0.21, 0.93 }, [5] = { 1, 0.5, 0 } }
for q, c in pairs(QC) do ITEM_QUALITY_COLORS[q] = { r = c[1], g = c[2], b = c[3] } end

Enum = {
  TooltipDataType = { Item = 0, Spell = 1, Unit = 2, Corpse = 3, Object = 4 },
  PlayerInteractionType = { None = 0, TradePartner = 1, Item = 2, Gossip = 3, QuestGiver = 4, Merchant = 5,
    TaxiNode = 6, Trainer = 7, Banker = 8, AlliedRaceDetailsGiver = 9, GuildBanker = 10, Registrar = 11,
    Vendor = 12, PetitionVendor = 13, GuildTabardVendor = 14, TalentMaster = 15, SpecializationMaster = 16,
    MailInfo = 17, SpiritHealer = 18, AreaSpiritHealer = 19, Binder = 20, Auctioneer = 21 },
  ItemCommodityStatus = { Unknown = 0, Item = 1, Commodity = 2 },
}

TooltipDataProcessor = { AllTypes = "ALL" }
function TooltipDataProcessor.AddTooltipPostCall(tooltipType, func)
  if type(func) ~= "function" then apiError("AddTooltipPostCall: func must be a function") end
  M.postCalls[tooltipType] = M.postCalls[tooltipType] or {}
  table.insert(M.postCalls[tooltipType], func)
end
function TooltipDataProcessor.AddTooltipPreCall() end
function TooltipDataProcessor.AddLinePreCall() end
function TooltipDataProcessor.AddLinePostCall() end
C_TooltipInfo = { GetBagItem = function(b, s) return M.TooltipData("GetBagItem", { b, s }) end,
  GetItemByID = function(id) return M.TooltipData("GetItemByID", { id }) end,
  GetHyperlink = function(l) return M.TooltipData("GetHyperlink", { l }) end }
TooltipUtil = {}

-- Items, bags, locations ---------------------------------------------------------------
-- Links as the modern client writes them: item:id:enchant:gem1:gem2:gem3:gem4:
-- suffix:unique:level:spec:modifierMask:context:numBonus[:bonus...]:numModifiers
-- [:type:value...]:relic fields. Gear with a random suffix carries its bonus
-- ids (and, like the real API, a type 28 modifier).
function M.ItemLink(id, suffix, bonus)
  local it = M["items"][id]
  local name = it and it.name or ("item:" .. id)
  if bonus and #bonus > 0 then
    local f = { "item:" .. id, "", "", "", "", "", suffix or "", "", "60", "", "", "1", tostring(#bonus) }
    for _, b in ipairs(bonus) do f[#f + 1] = tostring(b) end
    f[#f + 1] = "1"; f[#f + 1] = "28"; f[#f + 1] = "6874"
    f[#f + 1] = ""; f[#f + 1] = ""; f[#f + 1] = ""; f[#f + 1] = ""
    return "|cff1eff00|H" .. table.concat(f, ":") .. "|h[" .. name .. (M.suffixName or " of the Bear") .. "]|h|r"
  end
  if suffix then
    return "|cff1eff00|Hitem:" .. id .. "::::::" .. suffix .. "::60:::::::::|h[" .. name .. " of the Monkey]|h|r"
  end
  return "|cffffffff|Hitem:" .. id .. "::::::::60:::::::::|h[" .. name .. "]|h|r"
end
function M.SlotLink(s) return M.ItemLink(s.itemID, s.suffix, s.bonus) end
M.cursorSet = {}
function SetCursor(c) M.cursorSet[#M.cursorSet + 1] = c or false; M.cursor = c; return true end
function ResetCursor() M.cursor = nil end
local ItemLocationMixin = {}
ItemLocation = {}
function ItemLocation:CreateFromBagAndSlot(bag, slot)
  return setmetatable({ bagID = bag, slotIndex = slot }, { __index = ItemLocationMixin })
end
function ItemLocation:CreateEmpty() return setmetatable({}, { __index = ItemLocationMixin }) end
function ItemLocationMixin:IsBagAndSlot() return self.bagID ~= nil and self.slotIndex ~= nil end
function ItemLocationMixin:GetBagAndSlot() return self.bagID, self.slotIndex end
function ItemLocationMixin:IsEquipmentSlot() return false end
function ItemLocationMixin:IsValid() return self.bagID ~= nil and M.bags[self.bagID] ~= nil and M.bags[self.bagID][self.slotIndex] ~= nil end
function ItemLocationMixin:HasAnyLocation() return self.bagID ~= nil end
local function locSlot(loc)
  if type(loc) ~= "table" or type(loc.IsValid) ~= "function" then error("Invalid item location", 3) end
  return M.bags[loc.bagID] and M.bags[loc.bagID][loc.slotIndex]
end
M.locSlot = locSlot

C_Item = {}
function C_Item.GetItemID(loc) local s = locSlot(loc); return s and s.itemID end
function C_Item.GetItemLink(loc) local s = locSlot(loc); return s and M.SlotLink(s) end
function C_Item.IsBound(loc) local s = locSlot(loc); return s and s.bound == true or false end
function C_Item.GetStackCount(loc) local s = locSlot(loc); return s and s.count or 0 end
function C_Item.DoesItemExist(loc) return locSlot(loc) ~= nil end
function C_Item.GetItemLocation(guid) return M.guids[guid] end
local function idOf(v)
  if type(v) == "number" then return v end
  if type(v) == "string" then return tonumber(v:match("item:(%d+)")) or tonumber(v) end
end
function C_Item.GetItemInfo(v)
  local id = idOf(v)
  local it = id and M["items"][id]
  if not it then return nil end
  return it.name, M.ItemLink(id), it.quality, 1, it.minLevel or 0, it.class or "Trade Goods", "Trade Goods",
    it.maxStack or 20, it.equipLoc or "", it.icon, it.vendor or 0, it.classID or 7, it.subclassID or 0, it.bind or 0, 0, nil, true
end
function C_Item.GetItemInfoInstant(v)
  local id = idOf(v)
  if not id then return nil end
  local it = M["items"][id] or {}
  return id, "Trade Goods", "Trade Goods", it.equipLoc or "", it.icon or 134400, it.classID or 7, it.subclassID or 0
end
function C_Item.GetItemNameByID(id) local it = M["items"][id]; return it and it.name end
function C_Item.GetItemIconByID(id) local it = M["items"][id]; return it and it.icon or 134400 end
function C_Item.GetItemQualityByID(id) local it = M["items"][id]; return it and it.quality end
function C_Item.GetItemQualityColor(q) local c = QC[q] or QC[1]; return c[1], c[2], c[3], "ffffffff" end
function C_Item.IsItemDataCachedByID(id) return M["items"][id] ~= nil end
-- The Use effect of an item (useSpell in the mock's items): its name and a spell id, nil without one.
function C_Item.GetItemSpell(id)
  local it = M["items"][id]
  if it and it.useSpell then return it.useSpell, 90000 + (id % 1000) end
  return nil
end
GetItemSpell = C_Item.GetItemSpell

C_Container = {}
function C_Container.GetContainerItemInfo(bag, slot)
  local s = M.bags[bag] and M.bags[bag][slot]
  if not s then return nil end
  local it = M["items"][s.itemID]
  return { iconFileID = it and it.icon or 134400, stackCount = s.count, isLocked = s.locked == true or (M.held ~= nil and M.held.bag == bag and M.held.slot == slot), quality = it and it.quality or 1,
           isReadable = false, hasLoot = false, hyperlink = M.SlotLink(s), isFiltered = false,
           hasNoValue = not it or (it.vendor or 0) == 0, itemID = s.itemID, isBound = s.bound == true }
end
function C_Container.GetContainerItemLink(bag, slot) local s = M.bags[bag] and M.bags[bag][slot]; return s and M.SlotLink(s) end
function C_Container.GetContainerNumSlots(bag) return M.bagSlots[bag] or 16 end
function GetInventoryItemCount(unit, slot) return 1 end

-- Bag cleanup: free slots, quest info, the cursor, selling at a vendor.
M.bagSlots, M.bagFamily, M.pickups, M.used, M.sold, M.money = {}, {}, {}, {}, {}, 0
M.quests, M.questDone, M.questActive, M.level = {}, {}, {}, 60
function C_Container.GetContainerNumFreeSlots(bag)
  local n, used = C_Container.GetContainerNumSlots(bag), 0
  for slot = 1, n do if M.bags[bag] and M.bags[bag][slot] then used = used + 1 end end
  return n - used, M.bagFamily[bag] or 0
end
function C_Container.GetContainerItemQuestInfo(bag, slot)
  local s = M.bags[bag] and M.bags[bag][slot]
  if not s then return { isQuestItem = false, isActive = false } end
  local it = M["items"][s.itemID] or {}
  local q = it.startsQuest
  return { isQuestItem = it.classID == 12, questID = q, isActive = q ~= nil and M.questActive[q] == true }
end
local function bagChanged(bag)
  M.fire("BAG_UPDATE", bag)
  M.fire("BAG_UPDATE_DELAYED")
end
function C_Container.PickupContainerItem(bag, slot)
  M.pickups[#M.pickups + 1] = { bag, slot }
  local here = M.bags[bag] and M.bags[bag][slot]
  local held = M.held
  if not held then
    -- M.pickupRefused: the game will not pick it up (the cursor stays empty).
    if here and not here.locked and not M.pickupRefused then M.held = { bag = bag, slot = slot } end
    return
  end
  M.held = nil
  local src = M.bags[held.bag] and M.bags[held.bag][held.slot]
  if not src or (held.bag == bag and held.slot == slot) then return end
  M.bags[bag] = M.bags[bag] or {}
  if not here then
    M.bags[bag][slot], M.bags[held.bag][held.slot] = src, nil
  elseif here.itemID == src.itemID then
    local maxStack = (M["items"][src.itemID] or {}).maxStack or 20
    local moved = math.min(src.count, maxStack - here.count)
    here.count, src.count = here.count + moved, src.count - moved
    if src.count <= 0 then M.bags[held.bag][held.slot] = nil end
  else
    M.bags[bag][slot], M.bags[held.bag][held.slot] = src, here
  end
  bagChanged(bag)
end
function C_Container.UseContainerItem(bag, slot)
  M.used[#M.used + 1] = { bag, slot }
  if not (MerchantFrame and MerchantFrame:IsShown()) then
    M.violations[#M.violations + 1] = "UseContainerItem away from a vendor (uses or equips the item)"
    return
  end
  local s = M.bags[bag] and M.bags[bag][slot]
  if not s then return end
  local it = M["items"][s.itemID] or {}
  if (it.vendor or 0) <= 0 then return end
  M.bags[bag][slot] = nil
  M.sold[#M.sold + 1] = s.itemID
  M.money = M.money + it.vendor * s.count
  bagChanged(bag)
end
function CursorHasItem() return M.held ~= nil or M.cursorItem ~= nil end
function GetCursorInfo()
  if M.held then local s = M.bags[M.held.bag][M.held.slot]; return "item", s and s.itemID, s and M.SlotLink(s) end
  if M.cursorItem then return "item", M.cursorItem.itemID end
end
function ClearCursor() M.held = nil; M.cursorItem = nil end
-- DeleteCursorItem needs a hardware event and destroys one item per event:
-- The addon may call it only inside a player click on the drop box's Drop it
-- button, once. deleteMode: "ok" (destroyed: the slot locks, then empties
-- after deleteDelay seconds, the server's round trip), "blocked" (the game
-- refuses, the item stays on the cursor), "refused" (the cursor is cleared
-- but the server keeps the item).
M.destroyed, M.deleteMode, M.dropButtonName, M.deleteDelay = {}, "ok", "ForeverAHBagConfirmDrop", 0.2
function DeleteCursorItem()
  local hw = M.hw
  if not hw then
    M.violations[#M.violations + 1] = "DeleteCursorItem outside a click (the game blocks it)"
    return
  end
  local bname = hw.button and hw.button:GetName()
  if bname ~= M.dropButtonName then
    M.violations[#M.violations + 1] = "DeleteCursorItem from a click on " .. tostring(bname) .. ", not the drop box's Drop it"
    return
  end
  hw.deletes = hw.deletes + 1
  if hw.deletes > 1 then
    M.violations[#M.violations + 1] = "DeleteCursorItem twice in one click (the game allows one)"
    return
  end
  if M.deleteMode == "blocked" then return end
  local held = M.held
  if not held then return end
  M.held = nil
  local s = M.bags[held.bag] and M.bags[held.bag][held.slot]
  if not s or M.deleteMode == "refused" then return end
  s.locked = true
  M.destroyed[#M.destroyed + 1] = { bag = held.bag, slot = held.slot, itemID = s.itemID, count = s.count }
  C_Timer.After(M.deleteDelay or 0.2, function()
    if M.bags[held.bag][held.slot] == s then M.bags[held.bag][held.slot] = nil end
    bagChanged(held.bag)
  end)
end
function UnitLevel(unit) return M.level end
C_QuestLog = {}
function C_QuestLog.GetNumQuestLogEntries() return #M.quests, #M.quests end
function C_QuestLog.GetInfo(i)
  local q = M.quests[i]
  if not q then return nil end
  return { title = q.title, questID = q.questID or 0, isHeader = q.isHeader == true, isCollapsed = q.isCollapsed == true }
end
function C_QuestLog.GetQuestObjectives(questID)
  -- Some clients give an empty list here: the quest log's own lines (below) still have them.
  if M.questObjectivesEmpty then return {} end
  for _, q in ipairs(M.quests) do
    if q.questID == questID and not q.isHeader then
      local out = {}
      for k, t in ipairs(q.objectives or {}) do out[k] = { text = t, type = "item", finished = false } end
      return out
    end
  end
  return {}
end
function C_QuestLog.IsQuestFlaggedCompleted(id) return M.questDone[id] == true end
-- The quest log's objective lines by log index (the older way to read them).
function GetNumQuestLeaderBoards(index)
  local q = M.quests[index]
  return (q and not q.isHeader and q.objectives) and #q.objectives or 0
end
function GetQuestLogLeaderBoard(j, index)
  local q = M.quests[index]
  local t = q and q.objectives and q.objectives[j]
  return t, "item", false
end

-- The game's bag windows (ContainerFrame1 is the backpack) and the vendor window.
NUM_CONTAINER_FRAMES = 13
for i = 1, NUM_CONTAINER_FRAMES do
  local f = CreateFrame("Frame", "ContainerFrame" .. i, UIParent)
  f:SetSize(180, 170)
  f:Hide()
  for k = 1, 16 do
    local b = CreateFrame("Button", "ContainerFrame" .. i .. "Item" .. k, f)
    b:SetSize(37, 37)
    b:SetID(17 - k)
    b:SetPoint("TOPLEFT", f, "TOPLEFT", 8 + ((k - 1) % 4) * 41, -30 - math.floor((k - 1) / 4) * 34)
  end
end
-- One bag window (1 = the backpack), in columns of 3 from the bottom right.
local function openBagWindow(i)
  local f = _G["ContainerFrame" .. i]
  f:SetID(i - 1)
  f:ClearAllPoints()
  local col, row = math.floor((i - 1) / 3), (i - 1) % 3
  f:SetPoint("BOTTOMRIGHT", UIParent, "BOTTOMRIGHT", -20 - col * 190, 100 + row * 180)
  f:Show()
end
-- How many bag windows to open (1 = the backpack).
function M.OpenBags(n)
  for i = 1, (n or 5) do openBagWindow(i) end
end
function M.CloseBags()
  for i = 1, NUM_CONTAINER_FRAMES do _G["ContainerFrame" .. i]:Hide() end
end
MerchantFrame = CreateFrame("Frame", "MerchantFrame", UIParent)
MerchantFrame:Hide()
function M.OpenMerchant() MerchantFrame:Show(); M.fire("MERCHANT_SHOW") end
function M.CloseMerchant() MerchantFrame:Hide(); M.fire("MERCHANT_CLOSED") end

-- The game's bag keys: open or close the game's bags, or ElvUI's when it is in.
function ToggleAllBags()
  if M.elvBags then
    local f = ElvUI_ContainerFrame
    if f:IsShown() then f:Hide() else f:Show() end
  elseif ContainerFrame1:IsShown() then M.CloseBags() else M.OpenBags(5) end
end
function ToggleBackpack() ToggleAllBags() end
-- One bag (the bag bar's buttons): that bag's window, or ElvUI's one window.
function ToggleBag(id)
  if M.elvBags then ToggleAllBags() return end
  local f = _G["ContainerFrame" .. ((id or 0) + 1)]
  if f:IsShown() then f:Hide() else openBagWindow((id or 0) + 1) end
end
function OpenAllBags() if M.elvBags then ElvUI_ContainerFrame:Show() else M.OpenBags(5) end end
function CloseAllBags() if M.elvBags then ElvUI_ContainerFrame:Hide() else M.CloseBags() end end
-- ElvUI's bags: one window (ElvUI_ContainerFrame, under ElvUI's own scaled
-- parent) with frame.Bags[bagID][slotID] item buttons. ElvUI builds it when
-- you log in, after the other addons loaded.
function M.InstallElvUIBags(scale)
  local parent = CreateFrame("Frame", "ElvUIParent", UIParent)
  parent:SetAllPoints(UIParent)
  parent:SetScale(scale or 0.8)
  local f = CreateFrame("Button", "ElvUI_ContainerFrame", parent)
  f:SetFrameStrata("HIGH")
  f:SetSize(420, 380)
  f:SetPoint("BOTTOMRIGHT", parent, "BOTTOMRIGHT", -10, 200)
  f:Hide()
  f.Bags = {}
  for bagID = 0, 4 do
    local bag = CreateFrame("Frame", "ElvUI_ContainerFrameBag" .. bagID, f)
    bag:SetID(bagID)
    f.Bags[bagID] = bag
    for slot = 1, 16 do
      local b = CreateFrame("Button", "ElvUI_ContainerFrameBag" .. bagID .. "Slot" .. slot, bag)
      b:SetSize(30, 30)
      b:SetID(slot)
      local i = bagID * 16 + slot - 1
      b:SetPoint("TOPLEFT", f, "TOPLEFT", 4 + (i % 12) * 34, -4 - math.floor(i / 12) * 34)
      bag[slot] = b
    end
  end
  M.elvBags = true
  return f
end

-- ElvUI v16.01's real bag window (Game/Shared/Modules/Bags/Bags.lua:
-- ConstructContainerFrame 2448-2757, PositionButtons 3024-3047, Layout
-- 1284-1447, Initialize 3850-3893), laid out with the player's profile
-- (bagWidth 474, bagSize 42, spacing 1, bags 1-4 split with 7 more spacing,
-- keyring on, bag bar off so the backpack button shows). E.UIParent has the
-- same scale as UIParent; ElvUI scales UIParent itself (UIScale).
function M.InstallElvUIBagsReal(o)
  o = o or {}
  -- size: E:Scale(42) at the player's UIScale 0.62 on 1080 lines (E.mult 1.147) is 42.44, so 10 columns
  local width, size, spacing = o.width or 474, o.size or 42.44, o.spacing or 1
  local parent = CreateFrame("Frame", "ElvUIParent", UIParent)
  parent:SetAllPoints(UIParent)
  local chat = CreateFrame("Frame", "RightChatPanel", parent)
  chat:SetSize(472, 236)
  chat:SetPoint("BOTTOMRIGHT", parent, "BOTTOMRIGHT", -4, 4)
  local mover = CreateFrame("Frame", "ElvUIBagMover", parent)
  mover:SetSize(200, 22)
  mover:SetPoint("BOTTOMRIGHT", chat, "BOTTOMRIGHT", 0, 26)
  mover.POINT = "BOTTOMRIGHT"
  if o.details ~= false then
    -- Details! window behind the bags (the player's saved position, strata LOW).
    local d = CreateFrame("Frame", "DetailsBaseFrame1", UIParent)
    d:SetFrameStrata("LOW")
    d:SetSize(480, 231.8)
    d:SetPoint("BOTTOMRIGHT", UIParent, "BOTTOMRIGHT", -11.47, 32.14)
  end
  local f = CreateFrame("Button", "ElvUI_ContainerFrame", parent)
  f:SetFrameStrata("HIGH")
  f:SetMovable(true)
  f:RegisterForDrag("LeftButton", "RightButton")
  f:SetScript("OnDragStart", function(self) if IsShiftKeyDown and IsShiftKeyDown() then self:StartMoving() end end)
  f:SetScript("OnDragStop", function(self) self:StopMovingOrSizing() end)
  f:Hide()
  f.topOffset, f.bottomOffset = 50, 8
  f.mover = mover
  f:SetPoint(mover.POINT, mover)
  f.closeButton = CreateFrame("Button", "ElvUI_ContainerFrameCloseButton", f)
  f.closeButton:SetSize(24, 24)
  f.closeButton:SetPoint("TOPRIGHT", 5, 5)
  f.helpButton = CreateFrame("Button", "ElvUI_ContainerFrameHelpButton", f)
  f.helpButton:SetSize(16, 16)
  f.helpButton:SetPoint("RIGHT", f.closeButton, "LEFT", 0, 0)
  f.holderFrame = CreateFrame("Frame", nil, f)
  f.holderFrame:SetPoint("TOP", f, "TOP", 0, -f.topOffset)
  f.holderFrame:SetPoint("BOTTOM", f, "BOTTOM", 0, 8)
  f.ContainerHolder = CreateFrame("Button", "ElvUI_ContainerFrameContainerHolder", f)
  f.ContainerHolder:SetPoint("BOTTOMLEFT", f, "TOPLEFT", 0, 1)
  f.ContainerHolder:SetSize(5 * 34 + 24, 42)
  f.ContainerHolder:Hide()
  f.stackButton = CreateFrame("Button", "ElvUI_ContainerFrameStackButton", f.holderFrame)
  f.stackButton:SetSize(20, 20)
  f.stackButton:SetPoint("BOTTOMRIGHT", f.holderFrame, "TOPRIGHT", 0, 3)
  f.sortButton = CreateFrame("Button", "ElvUI_ContainerFrameSortButton", f)
  f.sortButton:SetSize(20, 20)
  f.sortButton:SetPoint("RIGHT", f.stackButton, "LEFT", -5, 0)
  f.bagsButton = CreateFrame("Button", "ElvUI_ContainerFrameBagsButton", f)
  f.bagsButton:SetSize(20, 20)
  f.bagsButton:SetPoint("RIGHT", f.sortButton, "LEFT", -5, 0)
  f.bagsButton:SetScript("OnClick", function()
    if f.ContainerHolder:IsShown() then f.ContainerHolder:Hide() else f.ContainerHolder:Show() end
  end)
  f.keyButton = CreateFrame("Button", "ElvUI_ContainerFrameKeyButton", f)
  f.keyButton:SetSize(20, 20)
  f.keyButton:SetPoint("RIGHT", f.bagsButton, "LEFT", -5, 0)
  f.vendorGraysButton = CreateFrame("Button", nil, f.holderFrame)
  f.vendorGraysButton:SetSize(20, 20)
  f.vendorGraysButton:SetPoint("RIGHT", f.keyButton, "LEFT", -5, 0)
  f.editBox = CreateFrame("EditBox", "ElvUI_ContainerFrameEditBox", f)
  f.editBox:SetHeight(19)
  f.editBox:SetFrameLevel(10)
  f.editBox:SetPoint("BOTTOMLEFT", f.holderFrame, "TOPLEFT", 1, 4)
  f.editBox:SetPoint("RIGHT", f.vendorGraysButton, "LEFT", -5, 0)
  f.goldText = f:CreateFontString(nil, "OVERLAY")
  f.goldText:SetPoint("RIGHT", f.helpButton, "LEFT", -10, -2)
  -- 13 characters at the mock's 6 per character: about the width of
  -- "5 [gold] 24 [silver] 33 [copper]" with ElvUI's coin icons.
  f.goldText:SetText(o.gold or "5g 24s 33c   ")
  f.pickupGold = CreateFrame("Button", nil, f)
  f.pickupGold:SetAllPoints(f.goldText)
  local cols = math.floor(width / (size + spacing))
  f.holderFrame:SetWidth(cols * (size + spacing) - spacing)
  f.Bags = {}
  local col, row, gap = 0, 0, 0
  for bagID = 0, 4 do
    local bag = CreateFrame("Frame", "ElvUI_ContainerFrameBag" .. bagID, f.holderFrame)
    bag:SetID(bagID)
    f.Bags[bagID] = bag
    if bagID > 0 and o.split ~= false then
      if col > 0 then row = row + 1; col = 0 end
      gap = gap + 7
    end
    for slot = 1, 16 do
      local b = CreateFrame("Button", "ElvUI_ContainerFrameBag" .. bagID .. "Slot" .. slot, bag)
      b:SetSize(size, size)
      b:SetID(slot)
      b:SetPoint("TOPLEFT", f.holderFrame, "TOPLEFT", col * (size + spacing), -(row * (size + spacing) + gap))
      bag[slot] = b
      col = col + 1
      if col >= cols then col = 0; row = row + 1 end
    end
  end
  if col > 0 then row = row + 1 end
  f:SetSize(width, row * (size + spacing) - spacing + f.topOffset + f.bottomOffset + gap)
  table.insert(UISpecialFrames, "ElvUI_ContainerFrame")
  M.elvBags = true
  return f
end

-- The game's own bag functions, shaped like Blizzard's ContainerFrame.lua:
-- they open and close ContainerFrameN only; a bag addon post-hooks them to
-- open its own window. Replaces the shortcuts above, so call it before the
-- addon loads (World before_addon).
function M.InstallBlizzardBags()
  local function frameFor(id)
    for i = 1, NUM_CONTAINER_FRAMES do
      local cf = _G["ContainerFrame" .. i]
      if cf:IsShown() and cf:GetID() == id then return cf end
    end
  end
  function IsBagOpen(id) return frameFor(id) ~= nil end
  function UpdateContainerFrameAnchors()
    local k = 0
    for i = 1, NUM_CONTAINER_FRAMES do
      local cf = _G["ContainerFrame" .. i]
      if cf:IsShown() then
        cf:ClearAllPoints()
        cf:SetPoint("BOTTOMRIGHT", UIParent, "BOTTOMRIGHT", -20 - math.floor(k / 3) * 190, 100 + (k % 3) * 180)
        k = k + 1
      end
    end
  end
  function OpenBag(id)
    if IsBagOpen(id) then return end
    for i = 1, NUM_CONTAINER_FRAMES do
      local cf = _G["ContainerFrame" .. i]
      if not cf:IsShown() then cf:SetID(id); cf:Show(); UpdateContainerFrameAnchors(); return end
    end
  end
  function CloseBag(id)
    local cf = frameFor(id)
    if cf then cf:Hide(); UpdateContainerFrameAnchors() end
  end
  function OpenBackpack() OpenBag(0) end
  function CloseBackpack() CloseBag(0) end
  function ToggleBag(id) if IsBagOpen(id) then CloseBag(id) else OpenBag(id) end end
  function ToggleBackpack() if IsBagOpen(0) then CloseBackpack() else OpenBackpack() end end
  function ToggleAllBags()
    local open = 0
    for id = 0, 4 do if IsBagOpen(id) then open = open + 1 end end
    for id = 0, 4 do if open == 5 then CloseBag(id) else OpenBag(id) end end
  end
  function OpenAllBags(frame) for id = 0, 4 do OpenBag(id) end end
  function CloseAllBags(frame) for id = 0, 4 do CloseBag(id) end end
end

-- ElvUI the addon (v16.01), loaded before GoblinLedger as in the game: the
-- engine table _G.ElvUI (ElvUI[1] is E) and its Bags module. B:Initialize
-- runs when you log in (o.buildOn: a later event instead). It builds the bag
-- window (M.InstallElvUIBagsReal, o.window), parks the game's bags the way
-- B:DisableBlizzard does (OnShow/OnHide set to nil, which drops HookScript
-- hooks; no position, SetPoint turned into ClearAllPoints) and post-hooks
-- the game's bag functions to open and close its own window (Bags.lua
-- 3918-3922). B:OpenBags and B:CloseAllBags are also what ElvUI calls by
-- itself (at a vendor, the auction house, a trade). Use with
-- M.InstallBlizzardBags.
function M.InstallElvUIAddon(o)
  o = o or {}
  local B = {}
  local E = { modules = { Bags = B } }
  function E:GetModule(name, silent)
    local m = self.modules[name]
    if not m and not silent then error("Cannot find a module named " .. tostring(name), 2) end
    return m
  end
  ElvUI = { E, {}, {}, {}, {} }
  function B:OpenBags()
    local f = B.BagFrame
    if not f or f:IsShown() then return end
    f:Show()
  end
  function B:CloseAllBags()
    local f = B.BagFrame
    if not f or not f:IsShown() then return true end
    f:Hide()
  end
  local function toggleAll()
    if IsBagOpen(0) then B:OpenBags() else B:CloseAllBags() end
  end
  function B:Initialize()
    local f = M.InstallElvUIBagsReal(o.window)
    M.elvBags = false   -- the game's own functions do not know ElvUI: ElvUI hooks them
    B.BagFrame = f
    -- Container_OnHide: closing the window closes the game's bags too.
    f:SetScript("OnHide", function() CloseBackpack(); for i = 1, 4 do CloseBag(i) end end)
    for i = 1, NUM_CONTAINER_FRAMES do
      local cf = _G["ContainerFrame" .. i]
      cf:SetScript("OnShow", nil)
      cf:SetScript("OnHide", nil)
      cf:UnregisterAllEvents()
      cf:ClearAllPoints()
      hooksecurefunc(cf, "SetPoint", cf.ClearAllPoints)
    end
    hooksecurefunc("ToggleBag", function() if f:IsShown() then B:CloseAllBags() else B:OpenBags() end end)
    hooksecurefunc("ToggleBackpack", toggleAll)
    hooksecurefunc("ToggleAllBags", toggleAll)
    hooksecurefunc("CloseAllBags", function() B:CloseAllBags() end)
    hooksecurefunc("OpenAllBags", function(frame) if frame then B:OpenBags() end end)
    M.elvBuilt = true
  end
  local ace = CreateFrame("Frame")
  ace:RegisterEvent(o.buildOn or "PLAYER_LOGIN")
  ace:SetScript("OnEvent", function(self) self:UnregisterAllEvents(); B:Initialize() end)
  return E, B
end

-- Console variables the addon may read.
M.cvars = { scriptErrors = "0" }
C_CVar = {}
function C_CVar.GetCVar(name) return M.cvars[name] end

C_GameRules = { IsHardcoreActive = function() return M.hardcore end }
C_AddOns = {}
function C_AddOns.IsAddOnLoaded(name) return M.loaded[name] == true, M.loaded[name] == true end
-- TOC fields of other addons a scenario installs (QuestieDB's Version and X-BUILD-COMMIT).
M.addonMeta = {}
function C_AddOns.GetAddOnMetadata(name, key)
  if name == "GoblinLedger" and key == "Version" then return "1.0.0" end
  local t = M.addonMeta[name]
  if t then return t[key] end
end
function C_AddOns.LoadAddOn(name)
  if M.loaded[name] then return true end
  local loader = M.loadOnDemand and M.loadOnDemand[name]
  if not loader then return false, "MISSING" end
  loader()
  M.loaded[name] = true
  M.fire("ADDON_LOADED", name)
  return true
end
EventUtil = {}
function EventUtil.ContinueOnAddOnLoaded(name, fn)
  if M.loaded[name] then fn() return end
  local f = CreateFrame("Frame")
  f:RegisterEvent("ADDON_LOADED")
  f:SetScript("OnEvent", function(self, ev, n) if n == name then self:UnregisterEvent("ADDON_LOADED"); fn() end end)
end

-- C_AuctionHouse ------------------------------------------------------------------------
C_AuctionHouse = {}
local CAH = C_AuctionHouse
local function q(name, fn) CAH[name] = function(...) guard("C_AuctionHouse." .. name, "query"); return fn(...) end end
local function restricted(name) CAH[name] = function(...) guard("C_AuctionHouse." .. name, "restricted"); M.cah.posted[#M.cah.posted + 1] = name end end
q("SendBrowseQuery", function(query)
  if type(query) ~= "table" then error("SendBrowseQuery: query must be a table", 2) end
  table.insert(M.cah.browse, query.searchString)
end)
q("SendSearchQuery", function(itemKey, sorts, separate) table.insert(M.cah.search, itemKey and itemKey.itemID or false) end)
q("SendSellSearchQuery", function(itemKey, sorts, separate) table.insert(M.cah.sell, itemKey and itemKey.itemID or false) end)
q("RefreshCommoditySearchResults", function() end)
q("RefreshItemSearchResults", function() end)
q("SearchForFavorites", function() end)
q("QueryOwnedAuctions", function() end)
q("QueryBids", function() end)
q("RequestMoreBrowseResults", function() end)
q("ReplicateItems", function() end)
for _, n in ipairs({ "PostItem", "PostCommodity", "PlaceBid", "StartCommoditiesPurchase", "ConfirmCommoditiesPurchase",
  "CancelAuction", "ConfirmPostItem", "ConfirmPostCommodity", "CancelCommoditiesPurchase", "CloseAuctionHouse_Restricted" }) do
  restricted(n)
end
function CAH.CloseAuctionHouse() M.cah.closed = M.cah.closed + 1 end
function CAH.GetBrowseResults()
  local out = {}
  for i, r in ipairs(M.browseResults) do
    out[i] = { itemKey = { itemID = r.itemID, itemLevel = 0, itemSuffix = 0, battlePetSpeciesID = 0 },
               minPrice = r.minPrice or 0, totalQuantity = r.quantity or 1, appearanceLink = nil,
               containsOwnerItem = false, containsAccountItem = false, itemLevel = 0 }
  end
  return out
end
function CAH.HasBrowseResults() return #M.browseResults > 0 end
function CAH.MakeItemKey(itemID, itemLevel, itemSuffix, pet) return { itemID = itemID, itemLevel = itemLevel or 0, itemSuffix = itemSuffix or 0, battlePetSpeciesID = pet or 0 } end
function CAH.GetItemKeyInfo(itemKey)
  if type(itemKey) ~= "table" then error("GetItemKeyInfo: bad itemKey", 2) end
  local it = M["items"][itemKey.itemID]
  if not it then return nil end
  return { itemID = itemKey.itemID, itemName = it.name, quality = it.quality, iconFileID = it.icon,
           isPet = false, isCommodity = it.commodity ~= false, isEquipment = it.commodity == false }
end
function CAH.GetItemCommodityStatus(loc)
  local s = locSlot(loc)
  if not s then return Enum.ItemCommodityStatus.Unknown end
  local it = M["items"][s.itemID]
  if it and it.commodity == false then return Enum.ItemCommodityStatus.Item end
  return Enum.ItemCommodityStatus.Commodity
end
function CAH.GetItemKeyFromItem(loc) local s = locSlot(loc); if s then return CAH.MakeItemKey(s.itemID, 0, s.suffix or 0) end end
function CAH.IsSellItemValid(loc, displayError) return locSlot(loc) ~= nil end
function CAH.GetAvailablePostCount(loc) local s = locSlot(loc); return s and s.count or 0 end
function CAH.SupportsCopperValues() return M.copperValues end
function CAH.ShouldAutoPopulatePrice() return true end
function CAH.HasFavorites() return false end
function CAH.HasSearchResults() return false end
function CAH.GetNumCommoditySearchResults(itemID) return #(M.commodityResults[itemID] or {}) end
function CAH.GetCommoditySearchResultInfo(itemID, i)
  local r = (M.commodityResults[itemID] or {})[i]
  if r then return { itemID = itemID, quantity = r.quantity, unitPrice = r.unitPrice, auctionID = i, owners = r.own and { "player" } or {}, totalNumberOfOwners = 1, numOwnerItems = r.own and 1 or 0, containsOwnerItem = r.own == true, containsAccountItem = false } end
end
M.itemResults = {}
function CAH.GetNumItemSearchResults(itemKey) return #(M.itemResults[itemKey.itemID] or {}) end
function CAH.GetItemSearchResultInfo(itemKey, i)
  local r = (M.itemResults[itemKey.itemID] or {})[i]
  if r then return { itemKey = itemKey, owners = {}, totalNumberOfOwners = 1, timeLeft = 2, auctionID = 100 + i, quantity = r.quantity or 1,
                     itemLink = M.ItemLink(itemKey.itemID, nil, r.bonus), containsOwnerItem = r.own == true, containsAccountItem = false,
                     containsSocketedItem = false, buyoutAmount = r.buyout, bidAmount = nil } end
end
function CAH.CalculateCommodityDeposit(itemID, duration, quantity) return 15 * quantity end
function CAH.CalculateItemDeposit(loc, duration, quantity) return 30 end
function CAH.GetCancelCost() return 0 end
function CAH.GetNumBids() return 0 end

-- Legacy (old AuctionFrame) API, only installed on the legacy client profile.
function M.InstallLegacyAPI()
  M.legacyList, M.legacySell = {}, nil
  function QueryAuctionItems(name, minLevel, maxLevel, page, usable, rarity, getAll, exactMatch, filterData)
    guard("QueryAuctionItems", "query")
    M.cah.browse[#M.cah.browse + 1] = name
  end
  function SetSelectedAuctionItem(listType, index) M.legacySelected = { listType, index } end
  function GetNumAuctionItems(listType) if listType == "list" then return #M.legacyList, #M.legacyList end return 0, 0 end
  function GetAuctionItemLink(listType, i)
    local r = listType == "list" and M.legacyList[i]
    if r then return M.ItemLink(r.itemID) end
  end
  function GetAuctionItemInfo(listType, i)
    local r = listType == "list" and M.legacyList[i]
    if not r then return nil end
    local it = M["items"][r.itemID] or {}
    return it.name, it.icon, r.count, it.quality, true, 0, "", 1, 1, r.buyout, 0, nil, nil, "Seller", nil, 0, r.itemID, true
  end
  function GetAuctionSellItemInfo()
    local s = M.legacySell
    if not s then return nil end
    local it = M["items"][s.itemID] or {}
    return it.name, it.icon, s.count, it.quality, true, it.vendor or 0, it.vendor or 0, s.count, s.count, s.itemID
  end
  function ClickAuctionSellItemButton()
    M.legacySell = M.cursorItem
    M.cursorItem = nil
    M.fire("NEW_AUCTION_UPDATE")
  end
  function PostAuction() guard("PostAuction", "restricted"); M.cah.posted[#M.cah.posted + 1] = "PostAuction" end
  function StartAuction() guard("StartAuction", "restricted"); M.cah.posted[#M.cah.posted + 1] = "StartAuction" end
  function PlaceAuctionBid() guard("PlaceAuctionBid", "restricted") end
  function CancelAuction() guard("CancelAuction", "restricted") end
  function CloseAuctionHouse() end
  PRICE_TYPE_UNIT, PRICE_TYPE_STACK = 1, 2
  function MoneyInputFrame_SetCopper(frame, copper)
    if not isRegion(frame) then error("MoneyInputFrame_SetCopper: bad frame", 2) end
    if not finite(copper) or copper < 0 or copper ~= floor(copper) then error("MoneyInputFrame_SetCopper: bad copper " .. tostring(copper), 2) end
    frame._copper = copper
  end
  function MoneyInputFrame_GetCopper(frame) return frame._copper or 0 end
end

M.MODERN_EVENTS = { "ADDON_LOADED", "PLAYER_LOGIN", "PLAYER_ENTERING_WORLD", "PLAYER_LOGOUT", "VARIABLES_LOADED",
  "AUCTION_HOUSE_SHOW", "AUCTION_HOUSE_CLOSED", "PLAYER_INTERACTION_MANAGER_FRAME_SHOW",
  "PLAYER_INTERACTION_MANAGER_FRAME_HIDE", "AUCTION_HOUSE_BROWSE_RESULTS_UPDATED", "AUCTION_HOUSE_BROWSE_RESULTS_ADDED",
  "AUCTION_HOUSE_BROWSE_FAILURE", "COMMODITY_SEARCH_RESULTS_UPDATED", "COMMODITY_SEARCH_RESULTS_ADDED",
  "ITEM_SEARCH_RESULTS_UPDATED", "ITEM_SEARCH_RESULTS_ADDED", "ITEM_KEY_ITEM_INFO_RECEIVED", "MODIFIER_STATE_CHANGED",
  "TOOLTIP_DATA_UPDATE", "GET_ITEM_INFO_RECEIVED", "PLAYER_MONEY", "BAG_UPDATE", "AUCTION_HOUSE_POST_WARNING",
  "AUCTION_HOUSE_POST_ERROR", "AUCTION_HOUSE_THROTTLED_SYSTEM_READY", "OWNED_AUCTIONS_UPDATED" , "BAG_UPDATE_DELAYED", "QUEST_LOG_UPDATE", "PLAYER_LEVEL_UP", "MERCHANT_SHOW", "MERCHANT_CLOSED"}
M.LEGACY_EVENTS = { "ADDON_LOADED", "PLAYER_LOGIN", "PLAYER_ENTERING_WORLD", "PLAYER_LOGOUT", "VARIABLES_LOADED",
  "AUCTION_HOUSE_SHOW", "AUCTION_HOUSE_CLOSED", "AUCTION_ITEM_LIST_UPDATE", "NEW_AUCTION_UPDATE",
  "AUCTION_OWNED_LIST_UPDATE", "AUCTION_BIDDER_LIST_UPDATE", "PLAYER_INTERACTION_MANAGER_FRAME_SHOW",
  "PLAYER_INTERACTION_MANAGER_FRAME_HIDE", "MODIFIER_STATE_CHANGED", "GET_ITEM_INFO_RECEIVED", "BAG_UPDATE" , "BAG_UPDATE_DELAYED", "QUEST_LOG_UPDATE", "PLAYER_LEVEL_UP", "MERCHANT_SHOW", "MERCHANT_CLOSED", "PLAYER_MONEY"}
function M.SetClient(kind)
  M.client = kind
  M.knownEvents = {}
  for _, e in ipairs(kind == "legacy" and M.LEGACY_EVENTS or M.MODERN_EVENTS) do M.knownEvents[e] = true end
  if kind == "legacy" then
    TooltipDataProcessor = nil
    Enum.TooltipDataType = nil
    M.InstallLegacyAPI()
  end
end
M.SetClient("modern")

function ShowUIPanel(f)
  if not isRegion(f) then return end
  f:ClearAllPoints()
  f:SetPoint("TOPLEFT", UIParent, "TOPLEFT", M.panelX or 16, M.panelY or -116)
  f:Show()
end
function HideUIPanel(f) if isRegion(f) then f:Hide() end end

-- Loader for addon files: runs src as chunk `name` with the addon varargs.
function M.LoadChunk(src, name, ...)
  local f, e = loadstring(src, name)
  if not f then M.err("load " .. name, e); return false end
  return M.call("run " .. name, f, ...)
end

-- WoW has no os/io/require/dofile for addons.
M._sandbox = function()
  os, io, require, dofile, loadfile, package, module = nil, nil, nil, nil, nil, nil, nil
  debug = nil
end
'''

# ===========================================================================
# Blizzard_AuctionHouseUI mock (shaped like the Forever FrameXML)
# ===========================================================================
BLIZZ_MODERN_LUA = r'''
local M = MOCK
AuctionHouseFrameDisplayMode = {
  Buy = { "SearchBar", "CategoriesList", "BrowseResultsFrame" },
  ItemBuy = { "SearchBar", "ItemBuyFrame" },
  CommoditiesBuy = { "SearchBar", "CommoditiesBuyFrame" },
  WoWTokenBuy = { "WoWTokenResults" },
  ItemSell = { "ItemSellFrame", "ItemSellList" },
  CommoditiesSell = { "CommoditiesSellFrame", "CommoditiesSellList" },
  WoWTokenSell = { "WoWTokenSellFrame" },
  Auctions = { "AuctionsFrame" },
}

local function makeMoneyInput(parent)
  local p = CreateFrame("Frame", nil, parent)
  p:SetSize(240, 32)
  p.MoneyInputFrame = CreateFrame("Frame", nil, p)
  p.MoneyInputFrame:SetAllPoints(p)
  p.MoneyInputFrame.GoldBox = CreateFrame("EditBox", nil, p.MoneyInputFrame)
  p.MoneyInputFrame.SilverBox = CreateFrame("EditBox", nil, p.MoneyInputFrame)
  p.MoneyInputFrame.CopperBox = CreateFrame("EditBox", nil, p.MoneyInputFrame)
  p.amount = 0
  function p:SetAmount(amount)
    if type(amount) ~= "number" or amount ~= amount or amount < 0 or amount ~= math.floor(amount) then
      error("PriceInput:SetAmount: bad amount " .. tostring(amount), 2)
    end
    if not C_AuctionHouse.SupportsCopperValues() then amount = amount - amount % 100 end
    self.amount = amount
    self.MoneyInputFrame.GoldBox:SetText(math.floor(amount / 10000))
    self.MoneyInputFrame.SilverBox:SetText(math.floor(amount / 100) % 100)
    self.MoneyInputFrame.CopperBox:SetText(amount % 100)
  end
  function p:GetAmount() return self.amount end
  function p:Clear() self:SetAmount(0) end
  return p
end

local function makeSellFrame(ah, key, isCommodity)
  local sf = CreateFrame("Frame", nil, ah)
  sf:SetPoint("TOPLEFT", ah, "TOPLEFT", 4, -60)
  sf:SetSize(363, 450)
  sf:Hide()
  sf.isCommodity = isCommodity
  sf.ItemDisplay = CreateFrame("Frame", nil, sf)
  sf.ItemDisplay.ItemButton = CreateFrame("Button", nil, sf.ItemDisplay)
  sf.QuantityInput = CreateFrame("Frame", nil, sf)
  sf.QuantityInput.InputBox = CreateFrame("EditBox", nil, sf.QuantityInput)
  sf.QuantityInput.MaxButton = CreateFrame("Button", nil, sf.QuantityInput)
  sf.PriceInput = makeMoneyInput(sf)
  sf.PriceInput:SetPoint("TOPLEFT", sf, "TOPLEFT", 0, -150)
  sf.Duration = CreateFrame("Frame", nil, sf)
  sf.Duration.Dropdown = CreateFrame("Frame", nil, sf.Duration)
  sf.PostButton = CreateFrame("Button", nil, sf, "UIPanelButtonTemplate")
  sf.PostButton:SetSize(194, 22)
  sf.PostButton:SetPoint("BOTTOM", sf, "BOTTOM", 0, 40)
  sf.PostButton:SetText("Post")
  sf.PostButton:SetScript("OnClick", function()
    local loc = sf.itemLocation
    if not loc then return end
    if isCommodity then
      C_AuctionHouse.PostCommodity(loc, 2, C_Item.GetStackCount(loc), sf.PriceInput:GetAmount())
    else
      C_AuctionHouse.PostItem(loc, 2, 1, nil, sf.PriceInput:GetAmount())
    end
  end)
  if not isCommodity then sf.SecondaryPriceInput = makeMoneyInput(sf) end
  sf.postStateUpdates = 0
  function sf:GetDefaultPrice()
    local loc = self:GetItem()
    if loc and loc:IsValid() then
      local vendor = select(11, C_Item.GetItemInfo(C_Item.GetItemLink(loc))) or 0
      local p = vendor * 2
      p = p + (100 - p % 100)
      return math.max(p, 100)
    end
    return 0
  end
  function sf:GetItem() return self.itemLocation end
  function sf:UpdateTotalPrice()
    local qty = self.itemLocation and C_Item.GetStackCount(self.itemLocation) or 0
    self.totalPrice = self.PriceInput:GetAmount() * (isCommodity and qty or 1)
  end
  function sf:UpdatePostState()
    self.postStateUpdates = self.postStateUpdates + 1
    self:UpdateTotalPrice()
  end
  function sf:UpdatePriceSelection()
    if self:GetItem() and self.PriceInput:GetAmount() == self:GetDefaultPrice() and C_AuctionHouse.ShouldAutoPopulatePrice() then
      local first = C_AuctionHouse.GetCommoditySearchResultInfo(C_Item.GetItemID(self:GetItem()), 1)
      if first then self.PriceInput:SetAmount(first.unitPrice); self:UpdatePostState() end
    end
  end
  function sf:SetItem(itemLocation, fromItemDisplay, refreshListWithPreviousItem)
    if itemLocation ~= nil and not C_AuctionHouse.IsSellItemValid(itemLocation) then return false end
    self.itemLocation = itemLocation
    self.PriceInput:SetAmount(self:GetDefaultPrice())
    self:UpdatePostState()
    local itemKey = itemLocation and C_AuctionHouse.GetItemKeyFromItem(itemLocation) or nil
    if itemKey then ah:QueryItem("sell", itemKey) end
    if isCommodity then self:UpdatePriceSelection() end
    return true
  end
  ah[key] = sf
  return sf
end

M.loadOnDemand = M.loadOnDemand or {}
M.loadOnDemand["Blizzard_AuctionHouseUI"] = function()
  local f = CreateFrame("Frame", "AuctionHouseFrame", UIParent)
  f:SetSize(M.ahWidth or 800, 538)
  f:SetFrameStrata("MEDIUM")
  f:SetFrameLevel(10)
  f:Hide()
  f:SetToplevel(true)
  f:EnableMouse(true)
  local function sub(key, ftype)
    local s = CreateFrame(ftype or "Frame", nil, f)
    s:SetPoint("TOPLEFT", f, "TOPLEFT", 4, -60)
    s:SetPoint("BOTTOMRIGHT", f, "BOTTOMRIGHT", -4, 26)
    s:Hide()
    f[key] = s
    return s
  end
  sub("CategoriesList")
  local br = sub("BrowseResultsFrame")
  br.ItemList = CreateFrame("Frame", nil, br)
  function br:Reset() M.browseResults = {} end
  local sb = CreateFrame("Frame", nil, f)
  sb:SetPoint("TOPLEFT", f, "TOPLEFT", 60, -30)
  sb:SetSize(700, 24)
  sb:Hide()
  f.SearchBar = sb
  sb.SearchBox = CreateFrame("EditBox", nil, sb, "SearchBoxTemplate")
  sb.SearchButton = CreateFrame("Button", nil, sb, "UIPanelButtonTemplate")
  sb.FavoritesSearchButton = CreateFrame("Button", nil, sb)
  sb.FilterButton = CreateFrame("Button", nil, sb)
  function sb.SearchBox:GetSearchString() return self:GetText() end
  function sb:GetAuctionHouseFrame() return f end
  function sb:StartSearch()
    local s = self.SearchBox:GetSearchString()
    self:GetAuctionHouseFrame():SendBrowseQuery(s, 0, 0, {})
  end
  sb.SearchBox:SetScript("OnEnterPressed", function(self) self:ClearFocus(); sb:StartSearch() end)
  sb.SearchButton:SetScript("OnClick", function() sb:StartSearch() end)

  local cb = sub("CommoditiesBuyFrame")
  cb.BuyDisplay = CreateFrame("Frame", nil, cb)
  cb.BuyDisplay.ItemDisplay = CreateFrame("Frame", nil, cb.BuyDisplay)
  cb.BuyDisplay.ItemDisplay.ItemButton = CreateFrame("Button", nil, cb.BuyDisplay.ItemDisplay)
  cb.BuyDisplay.QuantityInput = CreateFrame("Frame", nil, cb.BuyDisplay)
  cb.BuyDisplay.QuantityInput.InputBox = CreateFrame("EditBox", nil, cb.BuyDisplay.QuantityInput)
  cb.BuyDisplay.BuyButton = CreateFrame("Button", nil, cb.BuyDisplay, "UIPanelButtonTemplate")
  cb.ItemList = CreateFrame("Frame", nil, cb)
  cb.BackButton = CreateFrame("Button", nil, cb)
  function cb:SetItemIDAndPrice(itemID, price)
    self.itemID, self.price = itemID, price
    f:QueryItem("buyCommodities", C_AuctionHouse.MakeItemKey(itemID))
  end
  function cb:GetItemID() return self.itemID end

  local ib = sub("ItemBuyFrame")
  ib.ItemDisplay = CreateFrame("Frame", nil, ib)
  ib.ItemList = CreateFrame("Frame", nil, ib)
  ib.BuyoutFrame = CreateFrame("Frame", nil, ib)
  ib.BuyoutFrame.BuyoutButton = CreateFrame("Button", nil, ib.BuyoutFrame, "UIPanelButtonTemplate")
  ib.BidFrame = CreateFrame("Frame", nil, ib)
  function ib:SetItemKey(itemKey)
    self.itemKey = itemKey
    f:QueryItem("buyItems", itemKey)
  end

  makeSellFrame(f, "CommoditiesSellFrame", true)
  makeSellFrame(f, "ItemSellFrame", false)
  sub("CommoditiesSellList")
  f.CommoditiesSellList.SetItemID = function(self, id) self.itemID = id end
  sub("ItemSellList")
  sub("WoWTokenResults")
  local wts = sub("WoWTokenSellFrame")
  function wts:SetItem(loc) self.itemLocation = loc end
  function wts:GetItem() return self.itemLocation end
  sub("AuctionsFrame")

  f.Tabs = {}
  for i, name in ipairs({ "Buy", "Sell", "Auctions" }) do
    local tab = CreateFrame("Button", "AuctionHouseFrame" .. name .. "Tab", f)
    tab:SetSize(110, 32)
    tab:SetPoint("TOPLEFT", f, "BOTTOMLEFT", 20 + (i - 1) * 100, 2)
    f.Tabs[i] = tab
    f[name .. "Tab"] = tab
  end
  f.CloseButton = CreateFrame("Button", nil, f, "UIPanelCloseButton")
  f.CloseButton:SetSize(24, 24)
  f.CloseButton:SetPoint("TOPRIGHT", f, "TOPRIGHT", 0, 0)
  f.CloseButton:SetScript("OnClick", function() HideUIPanel(f) end)

  function f:SetDisplayMode(displayMode)
    if displayMode == AuctionHouseFrameDisplayMode.ItemSell or displayMode == AuctionHouseFrameDisplayMode.CommoditiesSell then
      if self.ItemSellFrame:GetItem() then displayMode = AuctionHouseFrameDisplayMode.ItemSell
      elseif self.CommoditiesSellFrame:GetItem() then displayMode = AuctionHouseFrameDisplayMode.CommoditiesSell end
    end
    if self.displayMode == displayMode then return end
    self.displayMode = displayMode
    local show = {}
    for _, mode in pairs(AuctionHouseFrameDisplayMode) do
      for _, key in ipairs(mode) do show[key] = show[key] or mode == displayMode end
    end
    for key, s in pairs(show) do if not s then self[key]:Hide() end end
    for _, key in ipairs(displayMode) do self[key]:Show() end
  end
  function f:GetDisplayMode() return self.displayMode end
  function f:QueryItem(ctx, itemKey)
    if ctx == "sell" then
      C_AuctionHouse.SendSellSearchQuery(itemKey, {}, true)
    else
      C_AuctionHouse.SendSearchQuery(itemKey, {}, false)
    end
  end
  function f:SendBrowseQuery(searchString, minLevel, maxLevel, filtersArray)
    local query = { searchString = searchString, minLevel = minLevel, maxLevel = maxLevel,
                    filters = filtersArray or {}, itemClassFilters = {}, sorts = { { sortOrder = 0, reverseSort = false } } }
    C_AuctionHouse.SendBrowseQuery(query)
    self:SetDisplayMode(AuctionHouseFrameDisplayMode.Buy)
  end
  function f:SelectBrowseResult(browseResult)
    local itemKey = browseResult.itemKey
    local info = C_AuctionHouse.GetItemKeyInfo(itemKey)
    if info.isCommodity then
      self.CommoditiesBuyFrame:SetItemIDAndPrice(itemKey.itemID, browseResult.minPrice)
      self:SetDisplayMode(AuctionHouseFrameDisplayMode.CommoditiesBuy)
    else
      self.ItemBuyFrame:SetItemKey(itemKey)
      self:SetDisplayMode(AuctionHouseFrameDisplayMode.ItemBuy)
    end
  end
  function f:ClearPostItem()
    self.WoWTokenSellFrame:SetItem(nil)
    self.CommoditiesSellFrame:SetItem(nil)
    self.CommoditiesSellList:SetItemID(nil)
    self.ItemSellFrame:SetItem(nil)
  end
  function f:SetPostItem(itemLocation)
    if not itemLocation:IsValid() or not C_AuctionHouse.IsSellItemValid(itemLocation) then return end
    local status = C_AuctionHouse.GetItemCommodityStatus(itemLocation)
    if status == Enum.ItemCommodityStatus.Unknown then return end
    self:ClearPostItem()
    if status == Enum.ItemCommodityStatus.Commodity then
      self:SetDisplayMode(AuctionHouseFrameDisplayMode.CommoditiesSell)
      self.CommoditiesSellFrame:SetItem(itemLocation)
      self.CommoditiesSellList:SetItemID(C_Item.GetItemID(itemLocation))
    else
      self:SetDisplayMode(AuctionHouseFrameDisplayMode.ItemSell)
      self.ItemSellFrame:SetItem(itemLocation)
    end
  end
  function f:SetSearchText(text) self.SearchBar.SearchBox:SetText(text) end
  f:SetScript("OnShow", function(self)
    self.displayMode = nil
    self:SetDisplayMode(AuctionHouseFrameDisplayMode.Buy)
  end)
  f:SetScript("OnHide", function(self)
    self.BrowseResultsFrame:Reset()
    self:ClearPostItem()
    C_AuctionHouse.CloseAuctionHouse()
  end)
  f:RegisterEvent("COMMODITY_SEARCH_RESULTS_UPDATED")
  f:SetScript("OnEvent", function(self, event, ...)
    if event == "COMMODITY_SEARCH_RESULTS_UPDATED" and self.CommoditiesSellFrame:IsShown() then
      self.CommoditiesSellFrame:UpdatePriceSelection()
    end
  end)
end

-- PlayerInteractionFrameManager: opens and closes the AH like the real UI does.
local mgr = CreateFrame("Frame", "PlayerInteractionFrameManager", UIParent)
mgr:RegisterEvent("PLAYER_INTERACTION_MANAGER_FRAME_SHOW")
mgr:RegisterEvent("PLAYER_INTERACTION_MANAGER_FRAME_HIDE")
mgr:SetScript("OnEvent", function(self, event, t)
  if t ~= Enum.PlayerInteractionType.Auctioneer then return end
  if event == "PLAYER_INTERACTION_MANAGER_FRAME_SHOW" then
    M.ahOpenNPC = true
    if not C_AddOns.IsAddOnLoaded("Blizzard_AuctionHouseUI") then C_AddOns.LoadAddOn("Blizzard_AuctionHouseUI") end
    ShowUIPanel(AuctionHouseFrame)
    if M.tsmShrink then AuctionHouseFrame:SetScale(0.001) end
  else
    M.ahOpenNPC = false
    HideUIPanel(AuctionHouseFrame)
  end
end)
'''

# ===========================================================================
# Blizzard_AuctionUI mock (old AuctionFrame), legacy client profile only
# ===========================================================================
BLIZZ_LEGACY_LUA = r'''
local M = MOCK
M.loadOnDemand = M.loadOnDemand or {}
M.loadOnDemand["Blizzard_AuctionUI"] = function()
  local f = CreateFrame("Frame", "AuctionFrame", UIParent)
  f:SetSize(832, 447)
  f:Hide()
  CreateFrame("Frame", "AuctionFrameBrowse", f)
  local auctions = CreateFrame("Frame", "AuctionFrameAuctions", f)
  auctions:SetAllPoints(f)
  auctions.priceType = PRICE_TYPE_STACK
  local name = CreateFrame("EditBox", "BrowseName", AuctionFrameBrowse)
  local search = CreateFrame("Button", "BrowseSearchButton", AuctionFrameBrowse, "UIPanelButtonTemplate")
  function AuctionFrameBrowse_Search()
    QueryAuctionItems(BrowseName:GetText(), nil, nil, 0, false, -1, false, false, nil)
  end
  search:SetScript("OnClick", AuctionFrameBrowse_Search)
  for i = 1, 8 do
    local b = CreateFrame("Button", "BrowseButton" .. i, AuctionFrameBrowse)
    b:SetID(i)
    b:SetScript("OnClick", function(self) SetSelectedAuctionItem("list", self:GetID()) end)
  end
  CreateFrame("Button", "AuctionsItemButton", auctions)
  AuctionsItemButton:SetScript("OnClick", function() ClickAuctionSellItemButton() end)
  local stack = CreateFrame("EditBox", "AuctionsStackSizeEntry", auctions)
  stack:SetText("1")
  CreateFrame("EditBox", "AuctionsNumStacksEntry", auctions):SetText("1")
  local sp = CreateFrame("Frame", "StartPrice", auctions); sp:SetSize(160, 20); sp:SetPoint("TOPLEFT", auctions, "TOPLEFT", 20, -200)
  local bp = CreateFrame("Frame", "BuyoutPrice", auctions); bp:SetSize(160, 20); bp:SetPoint("TOPLEFT", sp, "BOTTOMLEFT", 0, -20)
  local create = CreateFrame("Button", "AuctionsCreateAuctionButton", auctions, "UIPanelButtonTemplate")
  create:SetSize(191, 20)
  create:SetPoint("BOTTOMLEFT", auctions, "BOTTOMLEFT", 18, 39)
  create:SetScript("OnClick", function()
    PostAuction(MoneyInputFrame_GetCopper(StartPrice), MoneyInputFrame_GetCopper(BuyoutPrice), 2, AuctionsStackSizeEntry:GetNumber(), 1)
  end)
end

local mgr = CreateFrame("Frame", "AuctionUILoader", UIParent)
mgr:RegisterEvent("AUCTION_HOUSE_SHOW")
mgr:RegisterEvent("AUCTION_HOUSE_CLOSED")
mgr:SetScript("OnEvent", function(self, event)
  if event == "AUCTION_HOUSE_SHOW" then
    if not C_AddOns.IsAddOnLoaded("Blizzard_AuctionUI") then C_AddOns.LoadAddOn("Blizzard_AuctionUI") end
    M.panelX, M.panelY = 0, -104
    ShowUIPanel(AuctionFrame)
  else
    HideUIPanel(AuctionFrame)
  end
end)
'''

# ===========================================================================
# Auctionator mock: only the public-ish names AH.lua uses (EventBus, events, frames)
# ===========================================================================
ATR_LUA = r'''
local M = MOCK
Auctionator = {
  Buying = { Events = { ShowCommodityBuy = "buying_show_commodity_buy", ShowItemBuy = "buying_show_item_buy" } },
  Selling = { Events = { BagItemClicked = "selling_bag_item_clicked", SellSearchStart = "selling_sell_search_start",
                         PriceSelected = "selling_price_selected" } },
  AH = { Events = { CommoditySearchResultsReady = "ah_commodity_results_ready", ItemSearchResultsReady = "ah_item_results_ready" } },
  EventBus = { listeners = {}, sources = {} },
}
local bus = Auctionator.EventBus
function bus:Register(listener, events)
  if type(listener) ~= "table" or type(listener.ReceiveEvent) ~= "function" then error("Missing ReceiveEvent on listener", 2) end
  for _, e in ipairs(events) do
    if type(e) ~= "string" then error("Event names must be strings", 2) end
    self.listeners[e] = self.listeners[e] or {}
    table.insert(self.listeners[e], listener)
  end
  return self
end
function bus:RegisterSource(source, name) self.sources[source] = name; return self end
function bus:Fire(source, eventName, ...)
  if self.sources[source] == nil then error("All sources must be registered (" .. tostring(eventName) .. ")", 2) end
  M.atrFired = M.atrFired or {}
  table.insert(M.atrFired, { name = self.sources[source], event = eventName })
  for _, l in ipairs(self.listeners[eventName] or {}) do l:ReceiveEvent(eventName, ...) end
  return self
end
M.atrSource = {}
bus:RegisterSource(M.atrSource, "Auctionator mock")

local ev = CreateFrame("Frame")
ev:RegisterEvent("PLAYER_INTERACTION_MANAGER_FRAME_SHOW")
ev:SetScript("OnEvent", function(_, event, t)
  if t ~= Enum.PlayerInteractionType.Auctioneer or AuctionatorAHFrame or AuctionHouseFrame == nil then return end
  CreateFrame("Frame", "AuctionatorAHFrame", AuctionHouseFrame)
  local sell = CreateFrame("Frame", "AuctionatorSellingFrame", AuctionHouseFrame)
  sell:SetAllPoints(AuctionHouseFrame)
  sell:Hide()
  sell.SaleItemFrame = CreateFrame("Frame", nil, sell)
  sell.SaleItemFrame:SetPoint("TOPLEFT", sell, "TOPLEFT", 10, -80)
  sell.SaleItemFrame:SetSize(300, 200)
  -- like AuctionatorConfigurationMoneyInputAlternate: spans the sale frame, money box at x=120
  local price = CreateFrame("Frame", nil, sell.SaleItemFrame)
  price:SetHeight(7)
  price:SetPoint("TOPLEFT", sell.SaleItemFrame, "TOPLEFT", 70, -68)
  price:SetPoint("RIGHT", sell.SaleItemFrame, "RIGHT", 0, 0)
  price.MoneyInput = CreateFrame("Frame", nil, price)
  price.MoneyInput:SetSize(150, 20)
  price.MoneyInput:SetPoint("TOPLEFT", price, "BOTTOMLEFT", 120, 0)
  price.amount = 0
  function price:SetAmount(v) self.amount = v end
  function price:GetAmount() return self.amount end
  sell.SaleItemFrame.Price = price
  local bid = CreateFrame("Frame", nil, sell.SaleItemFrame)
  bid:SetHeight(7)
  bid:SetPoint("TOPLEFT", price, "BOTTOMLEFT", 0, -30)
  bid:SetPoint("RIGHT", sell.SaleItemFrame, "RIGHT", 0, 0)
  bid.MoneyInput = CreateFrame("Frame", nil, bid)
  bid.MoneyInput:SetSize(150, 20)
  bid.MoneyInput:SetPoint("TOPLEFT", bid, "BOTTOMLEFT", 120, 0)
  bid:Hide()
  sell.SaleItemFrame.BidPrice = bid
  local listener = {}
  function listener:ReceiveEvent(name, info)
    -- SaleItem.lua handles PriceSelected only while it has an item (self.itemInfo)
    if name == Auctionator.Selling.Events.PriceSelected and M.atrItem ~= nil then price:SetAmount(info.buyout) end
  end
  bus:Register(listener, { Auctionator.Selling.Events.PriceSelected })
  -- Auctionator's own search result arrives: it fills its undercut price, then the event fires.
  function M.atrResults(itemID, undercut)
    if M.atrItem == itemID then price:SetAmount(undercut) end
    bus:Fire(M.atrSource, Auctionator.AH.Events.CommoditySearchResultsReady, itemID)
  end
end)
'''


# ===========================================================================
# World: one fresh Lua state with the mock and the addon
# ===========================================================================
class World:
    def __init__(self, ref, items_json, client="modern", data_src=None, now=None, saved=None,
                 faction="Alliance", screen=None, auctionator=False, bare_coin=False, ah_width=800,
                 set_owner_clears=True, copper_values=True, gold=MOCK_GOLD, before_addon=None):
        self.ref = ref
        self.items_json = items_json
        self.lua = lua51.LuaRuntime(unpack_returned_tuples=True)
        self.g = self.lua.globals()
        self.loader = self.lua.eval(
            "function(src, name, ...) local f, e = loadstring(src, name); if not f then error(e) end; return f(...) end")
        self.loader(MOCK_LUA, "@MOCK/wow.lua")
        self.same = self.lua.eval("function(a, b) return rawequal(a, b) end")
        self.M = self.g.MOCK
        M = self.M
        M.SetClient(client)
        self.set_gold(gold)
        M.faction = faction
        M.setOwnerClears = set_owner_clears
        M.copperValues = copper_values
        M.ahWidth = ah_width
        if screen:
            M.SetScreen(*screen)
        if bare_coin:
            M.InstallBareCoinGlobal()
        self.now = now if now is not None else ref.updated + 25 * 60
        M.now = self.now
        self._register_items(items_json)
        if client == "modern":
            self.loader(BLIZZ_MODERN_LUA, "@Interface/AddOns/Blizzard_AuctionHouseUI/Mock.lua")
        else:
            self.loader(BLIZZ_LEGACY_LUA, "@Interface/AddOns/Blizzard_AuctionUI/Mock.lua")
        M._sandbox()
        if before_addon:   # e.g. ElvUI, which loads before GoblinLedger
            before_addon(self)
        if auctionator:   # loads before GoblinLedger (alphabetical), its ADDON_LOADED fires before ours
            self.loader(ATR_LUA, "@Interface/AddOns/Auctionator/Mock.lua")
            M.loaded["Auctionator"] = True
            M.fire("ADDON_LOADED", "Auctionator")
        self.ns = self.lua.table()
        self.load_errors = []
        for fname in TOC_FILES:
            if fname == "Data.lua":
                src = data_src if data_src is not None else open(DATA_FILE, "rb").read().decode("utf-8")
            else:
                src = open(os.path.join(ADDON, fname), "rb").read().decode("utf-8")
            ok = M.LoadChunk(src, "@Interface/AddOns/%s/%s" % (ADDON_NAME, fname), ADDON_NAME, self.ns)
            if not ok:
                self.load_errors.append(fname)
        # SavedVariables are restored after the addon's files run and before ADDON_LOADED.
        if saved is not None:
            self.g.ForeverAH_Settings = self.to_lua(saved)
        M.loaded[ADDON_NAME] = True
        M.fire("ADDON_LOADED", ADDON_NAME)
        M.fire("VARIABLES_LOADED")
        M.fire("PLAYER_LOGIN")
        M.fire("PLAYER_ENTERING_WORLD", True, False)
        M.settle(0.2)

    def to_lua(self, v):
        if isinstance(v, dict):
            t = self.lua.table()
            for k, x in v.items():
                t[k] = self.to_lua(x)
            return t
        if isinstance(v, (list, tuple)):
            t = self.lua.table()
            for i, x in enumerate(v):
                t[i + 1] = self.to_lua(x)
            return t
        return v

    def _register_items(self, items_json):
        qmap = {"POOR": 0, "COMMON": 1, "UNCOMMON": 2, "RARE": 3, "EPIC": 4, "LEGENDARY": 5}
        M = self.M
        for sid, it in items_json.items():
            iid = int(sid)
            t = self.lua.table()
            t.name = it.get("name")
            t.quality = qmap.get(str(it.get("quality", "COMMON")).upper(), 1)
            t.icon = 130000 + iid
            t.vendor = int(it.get("sell") or 0)
            t["class"] = it.get("cls") or "Trade Goods"
            t.commodity = True
            M["items"][iid] = t
        # Items that are not in the price data (ids picked from the data, see pick_roles).
        for iid, name, commodity in [(R.NO_GEAR, R.NO_GEAR_NAME, False), (R.NO_BOLT, R.NO_BOLT_NAME, True),
                                     (R.NO_BAG, R.NO_BAG_NAME, False)]:
            if iid in self.ref.items:
                continue
            t = self.lua.table()
            t.name, t.quality, t.icon, t.vendor, t.commodity = name, 1, 130000 + iid, 10, commodity
            t["class"] = "Trade Goods"
            M["items"][iid] = t

    def game_name(self, iid):
        """The name the mock game gives an item, when it is a real name."""
        it = self.M["items"][iid]
        n = it.name if it is not None else None
        return n if known_name(n) else None

    def display_name(self, iid):
        """What the addon should show for an item: the data's name, else the game's, else Item <id>."""
        it = self.ref.items.get(iid)
        if it and known_name(it["name"]):
            return it["name"]
        return self.game_name(iid) or "Item %d" % iid

    def name_field(self, iid):
        """GetItem(id).name: the data's name, else the game's, else ""."""
        it = self.ref.items.get(iid)
        if it and known_name(it["name"]):
            return it["name"]
        return self.game_name(iid) or ""

    # helpers ---------------------------------------------------------------
    def set_gold(self, gold):
        """The gold on the character (copper) GetMoney() gives; None = no GetMoney at all
        (an older client): the flip ideas are then not sized. No event is fired."""
        self.gold = gold
        if gold is None:
            self.g.GetMoney = None
        else:
            if self.g.GetMoney is None:
                self.g.GetMoney = self.lua.eval("function() return MOCK.playerMoney end")
            self.M.playerMoney = gold

    def tick(self, n=1, dt=0.05):
        for _ in range(n):
            self.M.tick(dt)

    def settle(self, sec=0.5):
        self.M.settle(sec)

    def slash(self, text):
        mark = len(self.M.chat)
        self.M.slash(text)
        self.settle(0.1)
        return self.chat(mark)

    def chat(self, mark=0):
        return [self.M.chat[i] for i in range(mark + 1, len(self.M.chat) + 1)]

    def chat_mark(self):
        return len(self.M.chat)

    def errors(self):
        return [self.M.errors[i] for i in range(1, len(self.M.errors) + 1)]

    def lines(self, tt):
        out = []
        al = self.M.addonLines(tt)
        for i in range(1, len(al) + 1):
            ln = al[i]
            out.append((ln.left, ln.right))
        return out

    def all_lines(self, tt):
        return [(tt._lines[i].left, tt._lines[i].right) for i in range(1, len(tt._lines) + 1)]

    def put_bag(self, bag, slot, iid, count, suffix=None, bonus=None):
        M = self.M
        if M.bags[bag] is None:
            M.bags[bag] = self.lua.table()
        s = self.lua.table()
        s.itemID, s.count = iid, count
        if suffix:
            s.suffix = suffix
        if bonus:
            s.bonus = self.to_lua(list(bonus))
        M.bags[bag][slot] = s

    def set_commodity_results(self, iid, rows):
        """rows: [(unitPrice, quantity, own)]"""
        t = self.lua.table()
        for i, (price, qty, own) in enumerate(rows):
            t[i + 1] = self.to_lua({"unitPrice": price, "quantity": qty, "own": own})
        self.M.commodityResults[iid] = t

    def live(self, iid):
        r = self.ns.LiveCheapest(iid)
        if isinstance(r, tuple):
            return r[0]
        return r

    def set_browse(self, rows):
        t = self.lua.table()
        for i, (iid, minp, qty) in enumerate(rows):
            r = self.lua.table()
            r.itemID, r.minPrice, r.quantity = iid, minp, qty
            t[i + 1] = r
        self.M.browseResults = t

    def lua_list(self, t, n):
        out = []
        for i in range(1, n + 1):
            v = t[i]
            out.append(None if v is False or v is None else v)
        return out

    def open_ah_modern(self):
        self.M.fire("PLAYER_INTERACTION_MANAGER_FRAME_SHOW", AUCTIONEER)
        self.M.fire("AUCTION_HOUSE_SHOW")
        self.settle(0.3)

    def close_ah_modern(self):
        self.M.fire("PLAYER_INTERACTION_MANAGER_FRAME_HIDE", AUCTIONEER)
        self.M.fire("AUCTION_HOUSE_CLOSED")
        self.settle(0.3)

    def rect(self, o):
        r = self.M.rect(o)
        if not r or r[0] is None:
            return None
        return r  # L, B, R, T in screen units


# ===========================================================================
# Checks
# ===========================================================================
class Scenario:
    def __init__(self, name, verbose=False):
        self.name, self.verbose = name, verbose
        self.passed, self.failed = 0, []

    def check(self, cond, msg):
        if cond:
            self.passed += 1
            if self.verbose:
                print("    ok   %s" % msg)
        else:
            self.failed.append(msg)
        return bool(cond)

    def eq(self, got, want, msg):
        return self.check(got == want, "%s: got %r, want %r" % (msg, got, want))

    def clean(self, w, label="end", ok_chat=()):
        """No Lua errors anywhere, no swallowed addon errors, no forbidden calls, no long dashes.
        ok_chat: chat lines containing one of these may say "error" (a diagnostic naming none)."""
        errs = w.errors()
        self.check(not errs, "%s: no Lua errors (%s)" % (label, "; ".join(e.splitlines()[0] for e in errs[:5])))
        if errs and self.verbose:
            for e in errs[:3]:
                print(e)
        ns = w.ns
        self.check(ns.lastError is None, "%s: ns.lastError is nil (%s)" % (label, ns.lastError))
        ahs = ns.AHState
        if ahs is not None:
            self.check(ahs.lastError is None, "%s: AHState.lastError is nil (%s)" % (label, ahs.lastError))
        cp = ns.ChartPanel
        if cp is not None:
            self.check(cp.lastError is None, "%s: ChartPanel.lastError is nil (%s)" % (label, cp.lastError))
        bp = ns.BagPanel
        if bp is not None:
            self.check(bp.lastError is None, "%s: BagPanel.lastError is nil (%s)" % (label, bp.lastError))
        v = [w.M.violations[i] for i in range(1, len(w.M.violations) + 1)]
        self.check(not v, "%s: addon never sends AH queries or restricted calls (%s)" % (label, v[:3]))
        d = [w.M.dash[i] for i in range(1, len(w.M.dash) + 1)]
        self.check(not d, "%s: no long dash in any visible text (%s)" % (label, d[:2]))
        bad = [c for c in w.chat() if "error" in c.lower() and not any(o in c for o in ok_chat)]
        self.check(not bad, "%s: no error messages in chat (%s)" % (label, bad[:2]))


def ref_sell_prices(ref, iid, tf="1D", live=None, own=False):
    """rec_model.sell_prices for an item: what ns.SellPrices gives (Sell now, Best price)."""
    rec = ref_recommend(ref, iid, tf, live)
    return rec_model.sell_prices(rec, live, own, ref.items[iid]["vendor"])


def week_range(ref, iid):
    return rec_model.week_range(ref.window(iid, "1W")["cheap"])


def lua_hex(c):
    """Core.lua ns.Hex: "ff" + rrggbb, for |c colour codes."""
    return "ff" + "".join("%02x" % math.floor(max(0.0, min(1.0, v)) * 255 + 0.5) for v in c)


TT_POS_RGB, TT_NEG_RGB, TT_MUTED_RGB = (0.25, 0.80, 0.45), (0.90, 0.33, 0.33), (0.55, 0.51, 0.45)
TT_POS, TT_NEG, TT_MUTED = lua_hex(TT_POS_RGB), lua_hex(TT_NEG_RGB), lua_hex(TT_MUTED_RGB)


def vs_average(value, avg):
    """Core.lua vsAverage: how far the AH value is from the average, "+3%" green,
    "-5%" red (whole percent, rounded toward 0), a grey "0%" under 1%, None with
    no average."""
    if value <= 0 or avg <= 0:
        return None
    p = math.floor(abs(value - avg) * 100 / avg)
    if p < 1:
        return "|c%s0%%|r" % TT_MUTED
    if value > avg:
        return "|c%s+%d%%|r" % (TT_POS, p)
    return "|c%s-%d%%|r" % (TT_NEG, p)


def average_label(ref, iid):
    """Core.lua averageLabel: "7-day average", or "3-day average" with prices for fewer days."""
    days = ref.items[iid]["D"]["n"]
    if days < 1 or days > 7:
        days = 7
    return "%d-day average" % days


def ref_verdict(ref, iid, tf="1D", live=None, own=False):
    """rec_model.sell_verdict for an item: what ns.SellVerdict gives (None = no verdict)."""
    rec = ref_recommend(ref, iid, tf, live)
    it = ref.items[iid]
    sp = rec_model.sell_prices(rec, live, own, it["vendor"])
    return rec_model.sell_verdict(it, sp, rec, ref.salesHours)


# Core.lua ns.COLORS, by name; "white" is the plain 1, 1, 1 and "gold" the
# game's colour for a line added without one.
TT_RGB = {"sell": (0.22, 0.74, 0.97), "muted": (0.55, 0.51, 0.45), "text": (0.90, 0.88, 0.84), "white": (1, 1, 1),
          "pos": (0.25, 0.80, 0.45), "neg": (0.90, 0.33, 0.33), "buy": (0.898, 0.722, 0.259), "gold": (1, 0.82, 0)}


def expected_tooltip(ref, iid, age, mult=1, compact=False, count=None, live=None, own=False, tf="1D", cover="auto",
                     verdict=True, styles=False):
    """The Goblin Ledger lines of an item tooltip. mult > 1: Shift held on a stack of
    that many (count defaults to it). cover: "version", "all", None, or "auto"
    (plain link: "all" for an item whose versions the data lists). verdict=False:
    a bound item or another market (no verdict, no stack line).
    styles=True gives (lines, [(left colour, right colour, big)]) with TT_RGB
    names; big is True for the verdict line shown bigger (fresh data, full mode)."""
    it = ref.items.get(iid)
    if it is None:
        lines, st = [("Goblin Ledger: no recent listings", None)], [("muted", None, False)]
        return (lines, st) if styles else lines
    if count is None:
        count = mult if mult > 1 else 1
    if cover == "auto":
        cover = "all" if iid in ref.variants else None
    tag = " (x%d)" % mult if mult > 1 else ""

    def m(v):
        return money_icons(v * mult) if v > 0 else None
    out = []

    def add(left, right, lc, rc, big=False):
        out.append(((left, right), (lc, rc, big)))
    vs = vs_average(it["market"], it["avg7"])
    # the prices mix versions that sell for different prices: nothing to sell at, no verdict
    P = None if cover == "all" else ref_sell_prices(ref, iid, tf, live, own)
    V = ref_verdict(ref, iid, tf, live, own) if (P is not None and verdict) else None
    if compact:
        mk = m(it["market"])
        if V:
            if mk:
                add("%s: %s" % (V["left"], V["right"]), "AH%s %s" % (tag, mk) + ("  " + vs if vs else ""), V["lc"], "white")
            else:
                add("%s: %s" % (V["left"], V["right"]), "AH: none listed", V["lc"], "muted")
        else:
            add(("AH value%s: %s" % (tag, mk) + ("  " + vs if vs else "")) if mk else "AH value: none listed now",
                None, "text", None)
    else:
        add("Goblin Ledger", age_text(age), "sell", "muted")
        if V:
            add(V["left"], V["right"], V["lc"], V["rc"], V["kind"] in ("auction", "vendor") and age <= 6 * 3600)
        mk = m(it["market"])
        if mk:
            add("AH value" + tag, mk + ("  " + vs if vs else ""), "text", "white")
        else:
            add("AH value", "none listed", "text", "muted")
        if m(it["avg7"]):
            add(average_label(ref, iid) + tag, m(it["avg7"]), "text", "white")
        if P is not None:
            if P["now"] is not None:
                add(("Sell now (live)" if P["live"] else "Sell now") + tag, m(P["now"]), "text",
                    "neg" if P["nowVendor"] else "white")
            if P["best"] is not None:
                add("Suggested price" + tag, m(P["best"]), "text", "neg" if P["bestVendor"] else "white")
            if (P["nowWhy"] == "unreliable" or P["bestWhy"] == "unreliable") and not (V and V["right"] == "few listed"):
                add("Few listed: the price may not be reliable", None, "muted", None)
            if V and V["unit"] and count > 1 and V["kind"] in ("auction", "close"):
                add("This stack (%d), after AH cut" % count, money_icons(rec_model.stack_after_cut(V["unit"], count)),
                    "text", "white")
    if cover == "version":
        add("Prices for this version only", None, "muted", None)
    elif cover == "all":
        add("Price covers all versions of this item", None, "muted", None)
    if age > 6 * 3600:
        add("Prices are %dh old: run Install-Goblin-Ledger.bat, then /reload" % (age // 3600), None, "muted", None)
    lines = [x[0] for x in out]
    return (lines, [x[1] for x in out]) if styles else lines


def rgb_name(r, g, b):
    for k, c in TT_RGB.items():
        if abs(r - c[0]) < 0.006 and abs(g - c[1]) < 0.006 and abs(b - c[2]) < 0.006:
            return k
    return "(%.3f, %.3f, %.3f)" % (r, g, b)


def tooltip_styles(w, tt):
    """For each line of a tooltip: (left colour, right colour or None, left font, left size,
    right font, right size) as the line's FontStrings have them now."""
    out = []
    for i in range(1, len(tt._lines) + 1):
        s = w.M.lineStyle(tt, i)
        if s is None:
            out.append(None)
            continue
        L, R = s.left, s.right
        has_right = tt._lines[i].right is not None
        out.append((rgb_name(L.r, L.g, L.b), rgb_name(R.r, R.g, R.b) if has_right else None,
                    L.font, L.size, R.font, R.size))
    return out


def tooltip_style_ok(sc, w, tt, want_styles, label, base_size=12):
    """Our lines' final colours, and fonts: the verdict line (when big) on our
    bigger Font (both sides, base + 3 points), every other line of the whole
    tooltip at its normal size on the game's own Font."""
    got = tooltip_styles(w, tt)
    first = int(tt._blz or 0)
    ours = got[first:]
    sc.eq([(x[0], x[1]) if x else None for x in ours], [(lc, rc) for lc, rc, _ in want_styles],
          "%s: line colours" % label)
    big_rows = [i for i, x in enumerate(got) if x and (x[2].startswith("ForeverAH") or x[4].startswith("ForeverAH"))]
    want_big = [first + i for i, (_, _, big) in enumerate(want_styles) if big]
    sc.eq(big_rows, want_big, "%s: only the verdict line is bigger (%s)" % (label, "yes" if want_big else "none"))
    for i in want_big:
        x = got[i]
        sc.check(x[2].startswith("ForeverAHBigFont") and x[4] == x[2] and x[3] == base_size + 3 and x[5] == base_size + 3,
                 "%s: the verdict line's both sides on our Font, %d points (%s)" % (label, base_size + 3, x))
    others = [(i + 1, x) for i, x in enumerate(got) if x and i not in want_big
              and not ((x[2] == "GameTooltipText" and x[3] == base_size) or (i == 0 and x[2] == "GameTooltipHeaderText"))]
    sc.eq(others, [], "%s: every other line at the normal size" % label)


def verdict_line_ok(sc, ref, iid, lines, label, tf="1D", live=None, own=False, cover="auto", verdict=True):
    """The verdict and the stack line against the model (wording aside, the price the
    verdict judges and the stack's value after the cut). No verdict for prices that
    mix versions (cover "all"; "auto": an item whose versions the data lists) or
    with verdict=False (a bound item, another market)."""
    if cover == "auto":
        cover = "all" if iid in ref.variants else None
    V = ref_verdict(ref, iid, tf, live, own) if (verdict and cover != "all") else None
    texts = [l for l, _ in lines]
    if V is None:
        sc.check(not any(t in ("Auction it", "Vendor it", "Close call", "Maybe auction") for t in texts),
                 "%s: no verdict line (%s)" % (label, texts))
        return
    sc.check((V["left"], V["right"]) in lines, "%s: verdict %r (%s)" % (label, (V["left"], V["right"]), lines[:3]))
    for l, r in lines:
        if l.startswith("This stack ("):
            n = int(l[len("This stack ("):].split(")")[0])
            sc.eq(parse_coins(r), display_copper(V["unit"] * n * 95 // 100), "%s: the stack at the verdict's price" % label)


def tooltip_values_ok(sc, ref, iid, lines, mult, label, live=None, tf="1D", own=False):
    """Value-level check, independent of wording: every price on the tooltip
    equals the data file (AH value, the average) or the model (Sell now,
    Suggested price, and the stack at the verdict's price after the 5% cut,
    never multiplied by Shift)."""
    it = ref.items[iid]
    P = ref_sell_prices(ref, iid, tf, live, own)
    want = {"AH value": it["market"], average_label(ref, iid): it["avg7"],
            "Sell now": P["now"], "Sell now (live)": P["now"], "Suggested price": P["best"]}
    seen = set()
    for left, right in lines:
        base = (left or "").split(" (x")[0]
        coins = (right or "").split("  ")[0]        # the AH value's "+3%" comes after two spaces
        if base in want and coins and parse_coins(coins) is not None:
            seen.add(base)
            sc.eq(parse_coins(coins), display_copper((want[base] or 0) * mult), "%s %s value" % (label, base))
        elif (left or "").startswith("This stack (") and left.endswith("), after AH cut"):
            V = ref_verdict(ref, iid, tf, live, own)
            n = int(left[len("This stack ("):-len("), after AH cut")])
            sc.check(V is not None and V["unit"] is not None and V["kind"] in ("auction", "close"),
                     "%s: a stack line only under Auction it or Close call (%s)" % (label, V and V["kind"]))
            if V and V["unit"]:
                sc.eq(parse_coins(right), display_copper(rec_model.stack_after_cut(V["unit"], n)),
                      "%s stack of %d at the verdict's price" % (label, n))
    if it["market"] > 0:
        sc.check("AH value" in seen, "%s: AH value shown (%s)" % (label, sorted(seen)))
    if it["avg7"] > 0:
        sc.check(average_label(ref, iid) in seen, "%s: the average shown (%s)" % (label, sorted(seen)))


def check_series_and_recommend(sc, w, ref, ids=None, tfs=TFS):
    ns = w.ns
    ids = ids or sample_ids(ref, SAMPLE * 2)
    for iid in ids:
        for tf in tfs:
            s = ns.GetSeries(iid, tf)
            want = ref.window(iid, tf)
            if not sc.check(s is not None, "GetSeries(%d,%s) not nil" % (iid, tf)):
                continue
            n = int(s.n)
            sc.eq(n, want["n"], "GetSeries(%d,%s).n" % (iid, tf))
            sc.eq(s.step, want["step"], "GetSeries(%d,%s).step" % (iid, tf))
            sc.eq(w.lua_list(s.times, n), want["times"], "GetSeries(%d,%s).times" % (iid, tf))
            sc.eq(w.lua_list(s.cheap, n), want["cheap"], "GetSeries(%d,%s).cheap" % (iid, tf))
            sc.eq(w.lua_list(s.market, n), want["market"], "GetSeries(%d,%s).market" % (iid, tf))
            sc.eq([x or 0 for x in w.lua_list(s.sold, n)], want["sold"], "GetSeries(%d,%s).sold" % (iid, tf))
            r = ns.Recommend(iid, tf)
            want_r = ref_recommend(ref, iid, tf)
            if not sc.check(r is not None, "Recommend(%d,%s) not nil" % (iid, tf)):
                continue
            for k in ("buy", "sell", "listAt", "margin", "verdict", "lo", "hi", "cheapestNow", "marketNow", "sold24",
                      "soldPerDay", "auctions"):
                sc.eq(r[k], want_r[k], "Recommend(%d,%s).%s" % (iid, tf, k))
            for k in ("history", "advice", "thinListings", "vendorFar", "salesMeasuring", "soldEarly"):
                sc.eq(bool(r[k]), bool(want_r[k]), "Recommend(%d,%s).%s" % (iid, tf, k))
            for k in ("marginPct", "score", "pos"):
                sc.check(abs(float(r[k]) - want_r[k]) < 1e-9, "Recommend(%d,%s).%s: got %r want %r" % (iid, tf, k, r[k], want_r[k]))
            notes = [r.notes[i] for i in range(1, len(r.notes) + 1)]
            sc.eq(notes, want_r["notes"], "Recommend(%d,%s).notes" % (iid, tf))


def expected_mismatch(w, ref):
    """The note the chart shows when the data is from another market, or None."""
    ns, M = w.ns, w.M
    if M.hardcore:
        return "Prices are from the normal realms, not Hardcore"
    ahs = ns.AHState
    if ahs is not None and ahs.open and ahs.otherHouse:
        return "Prices are from the " + ref.houseName + ", not this one"
    if M.faction not in ("Alliance", "Neutral"):
        return "Prices are from the " + ref.houseName
    return None


def chart_check(sc, w, ref, iid, tf, label):
    """Checks everything the chart panel shows for (iid, tf) against the Python model."""
    ns, M = w.ns, w.M
    C = ns.ChartPanel
    F = C.frame
    sc.check(F.IsShown(F), "%s: chart shown" % label)
    sc.eq(ns.ChartItem(), iid, "%s: chart item" % label)
    sc.eq(ns.settings.chartTF, tf, "%s: settings.chartTF" % label)
    it = ref.items[iid]
    live = w.live(iid)
    want = ref_recommend(ref, iid, tf, live)
    win = ref.window(iid, tf)
    shown_name = w.display_name(iid)
    sc.eq(C.name._text, shown_name if len(shown_name) <= 34 else shown_name[:33] + "...", "%s: header name" % label)
    nowp = it["market"] if it["market"] > 0 else it["cheapest"]
    sc.eq(C.price._text, money_icons(nowp) if nowp > 0 else "", "%s: header price" % label)
    mk = [v for v in win["market"] if v is not None]
    if win["n"] > 1 and mk:
        p = (mk[-1] - mk[0]) / mk[0] * 100
        sc.eq(C.change._text, "%+.1f%% %s" % (p, tf), "%s: header change" % label)
    ahs0 = ns.AHState
    mixed = iid not in ref.base_of and (iid in ref.variants or (ahs0 is not None and bool(ahs0.suffix[iid])))
    if mixed:
        # the whole item of gear whose versions differ: its targets would mix them
        want = dict(want, buy=0, listAt=0, sell=0, margin=0, notes=[])
    why = "Depends on the version" if mixed else no_advice_text(want)
    sc.eq(C.buyBox.value._text, why or (money_icons(want["buy"]) if want["buy"] > 0 else "-"), "%s: Target Buy box" % label)
    sc.eq(C.listBox.value._text, why or (money_icons(want["listAt"]) if want["listAt"] > 0 else "-"), "%s: List At box" % label)
    muted = (abs(C.buyBox.value._textColor[1] - 0.55) < 0.01) if C.buyBox.value._textColor else False
    sc.eq(muted, why is not None, "%s: target boxes grey only without advice" % label)
    cheap_now = live if live else it["cheapest"]
    sc.eq(C.buyBox.sub._text, "Cheapest: " + ((money_icons(cheap_now) + (" (live)" if live else "")) if cheap_now > 0 else "none listed"),
          "%s: Target Buy sub" % label)
    sc.eq(C.listBox.sub._text, "Market: " + (money_icons(it["market"]) if it["market"] > 0 else "none listed"),
          "%s: List At sub" % label)
    mismatch = expected_mismatch(w, ref)
    if mismatch:
        sc.eq(C.verdict._text or "", "", "%s: no verdict for another market" % label)
        sc.eq(C.margin._text or "", "", "%s: no margin for another market" % label)
        sc.check(not C.meter._shown, "%s: no meter for another market" % label)
    elif why:
        sc.eq(C.verdict._text, "No advice yet", "%s: no verdict without advice" % label)
        sc.check(abs(C.verdict._textColor[1] - 0.55) < 0.01, "%s: that verdict line is grey" % label)
        sc.eq(C.margin._text or "", "", "%s: no margin without advice" % label)
        sc.check(not C.meter._shown, "%s: no verdict meter without advice" % label)
    else:
        sc.eq(C.verdict._text, want["verdict"], "%s: verdict" % label)
        mtxt = "Margin after 5% cut: " + ("+" if want["margin"] >= 0 else "") + money_text(want["margin"]) + \
            (" (%+.1f%%)" % (want["marginPct"] * 100))
        sc.eq(C.margin._text, mtxt, "%s: margin text" % label)
    sc.eq(C.volume._text, "%s \u00b7 %s listed" % (rec_model.sold_text(it["sold24"], ref.salesHours), commas(it["listed"])),
          "%s: volume text" % label)
    notes = [mismatch] if mismatch else list(want["notes"])
    ahs = ns.AHState
    if (ahs is not None and ahs.suffix[iid]) or iid in ref.variants:
        notes.append("Price covers all versions of this item")
    sc.eq(C.notes._text or "", ". ".join(notes), "%s: notes" % label)
    narrow = C.frame._w < 650
    sc.check(C.change._shown != narrow and C.volume._shown != narrow,
             "%s: change and volume %s at width %s" % (label, "hidden" if narrow else "shown", C.frame._w))
    compact = (C.panelH or 260) < 230
    sc.check(C.buyBox._shown and C.listBox._shown, "%s: boxes shown at height %s" % (label, C.panelH))
    sc.check(C.buyBox._h == (18 if compact else 40) and C.listBox._h == C.buyBox._h,
             "%s: boxes %s at height %s (%s)" % (label, "are one-line chips" if compact else "full", C.panelH, C.buyBox._h))
    sc.check(C.plot._points[1][5] == -(58 if compact else 86), "%s: the plot starts under the boxes" % label)
    sc.eq(C.age._text, "Data: %s \u00b7 %s" % (age_text(w.now - ref.updated), ref.houseName), "%s: age" % label)
    pools = C.pools
    # The box whose dashed line is on is lit (tinted background, border), the other is not.
    line = ns.settings.chartLine
    for box, which in ((C.buyBox, "buy"), (C.listBox, "sell")):
        lit = line == which
        sc.eq(bool(box.lit), lit, "%s: %s box lit" % (label, which))
        sc.check(all(bool(box.edges[k]._shown) == lit for k in range(1, len(box.edges) + 1)),
                 "%s: %s box border %s" % (label, which, "shown" if lit else "hidden"))
    # Timeframe buttons: active one highlighted
    for k in TFS:
        btn = C.tfButtons[k]
        col = btn.bg._color
        active = abs(col[4] - 0.35) < 1e-9
        sc.check(active == (k == tf), "%s: %s button highlight" % (label, k))
    if win["n"] < 2:
        # One snapshot (a new data set): a message instead of a near-empty plot.
        sc.check(C.msg._shown, "%s: collecting message shown" % label)
        sc.eq(C.msg._text, collecting_msg(ref, iid, tf), "%s: collecting message" % label)
        for key in ("dot", "vol", "ylab", "xlab", "tag", "dash", "ring"):
            sc.eq(int(pools[key].used), 0, "%s: nothing drawn (%s)" % (label, key))
            pool = pools[key]
            items_ = [pool["items"][k] for k in range(1, len(pool["items"]) + 1)]
            extra = [o for o in items_ if (o.outer._shown if key == "ring" else o._shown)]
            sc.check(not extra, "%s: old %s marks hidden" % (label, key))
        sc.check(C.view is None, "%s: no plot to hover" % label)
        return
    sc.check(not C.msg._shown, "%s: no empty-state message (%s)" % (label, C.msg._text))

    # Dots: one per non-null price, placed on one straight price scale.
    dots = pools.dot
    used = int(dots.used)
    pts = [(i, v, "m") for i, v in enumerate(win["market"]) if v is not None] + \
          [(i, v, "c") for i, v in enumerate(win["cheap"]) if v is not None]
    sc.eq(used, len(pts), "%s: dot count" % label)
    plot = C.plot
    plotW, plotH = plot._w, plot._h
    n = win["n"]
    stepX = plotW / (n - 1) if n > 1 else 0
    xy = []
    for k in range(1, min(used, len(pts)) + 1):
        d = dots["items"][k]
        p = d._points[1]
        xy.append((p[4], p[5]))
    if len(xy) == len(pts) and pts:
        xok = all(abs(xy[j][0] - ((pts[j][0]) * stepX if n > 1 else plotW / 2)) < 1e-6 for j in range(len(pts)))
        sc.check(xok, "%s: dot x positions follow the time grid" % label)
        vals = [p[1] for p in pts]
        if max(vals) > min(vals):
            j1 = vals.index(min(vals))
            j2 = vals.index(max(vals))
            b = (xy[j2][1] - xy[j1][1]) / (vals[j2] - vals[j1])
            a = xy[j1][1] - b * vals[j1]
            lin = all(abs(xy[j][1] - (a + b * vals[j])) < 1e-6 for j in range(len(pts)))
            sc.check(b > 0 and lin, "%s: dot heights are linear in price" % label)
            sc.check(all(-0.5 <= y <= plotH + 0.5 for _, y in xy), "%s: dots inside the plot" % label)
            # Axis labels sit on the same scale.
            ylab = pools.ylab
            ok = True
            for k in range(1, int(ylab.used) + 1):
                fs = ylab["items"][k]
                v = parse_coins(fs._text)
                y = fs._points[1][5]
                if v is None or abs(y - (a + b * v)) > 1e-6:
                    ok = False
            sc.check(ok and int(ylab.used) >= 2, "%s: y-axis labels on the price scale (%d labels)" % (label, int(ylab.used)))
    # Dashed target lines: only the one picked with the boxes (none by default),
    # on a short panel too (its chips switch it). Labels carry the advice numbers.
    show_buy = line == "buy" and want["buy"] > 0
    show_list = line == "sell" and want["listAt"] > 0
    tags = [pools.tag["items"][k]._text for k in range(1, int(pools.tag.used) + 1)]
    sc.eq(sorted(t.split(" ")[0] for t in tags), sorted((["Target"] if show_buy else []) + (["List"] if show_list else [])),
          "%s: target lines shown for chartLine=%s (%s)" % (label, line, tags))
    if show_buy:
        sc.check("Target Buy " + money_text(want["buy"]) in tags, "%s: buy line label (%s)" % (label, tags))
    if show_list:
        sc.check("List At " + money_text(want["listAt"]) in tags, "%s: list line label (%s)" % (label, tags))
    sc.eq(int(pools.dash.used) > 0, show_buy or show_list, "%s: dashes drawn only for a shown line" % label)
    # Sold bars
    sold = win["sold"]
    sc.eq(int(pools.vol.used), sum(1 for v in sold if v > 0) if max(sold or [0]) > 0 else 0, "%s: sold bar count" % label)
    # Hidden pool leftovers
    for key in ("dot", "vol", "ylab", "xlab"):
        pool = pools[key]
        extra = [k for k in range(int(pool.used) + 1, len(pool["items"]) + 1) if pool["items"][k]._shown]
        sc.check(not extra, "%s: unused %s textures are hidden" % (label, key))
    # X labels: the newest point is labelled first
    xl = pools.xlab
    if int(xl.used) >= 1:
        t = win["times"][-1]
        if win["step"] >= 86400:
            wantx = time.strftime("%b %d", time.gmtime(t))
        elif tf == "1D":
            wantx = time.strftime("%H:%M", time.localtime(t))
        else:
            wantx = time.strftime("%a %H:%M", time.localtime(t))
        sc.eq(xl["items"][1]._text, wantx, "%s: newest x label" % label)


def hover_check(sc, w, ref, iid, tf, idx, label):
    """Moves the cursor over point idx (0-based) and reads the chart's hover tooltip."""
    ns, M = w.ns, w.M
    C = ns.ChartPanel
    plot = C.plot
    win = ref.window(iid, tf)
    n = win["n"]
    plotW = plot._w
    stepX = plotW / (n - 1) if n > 1 else 0
    left = plot.GetLeft(plot)
    es = plot.GetEffectiveScale(plot)
    if not sc.check(left is not None, "%s: plot has a screen position" % label):
        return
    tt = w.g.GameTooltip
    if n < 2:
        # No plot (the collecting message): hovering must show nothing and break nothing.
        M.cursorX = (left + plotW / 2) * es
        M.enter(plot)
        w.tick(2)
        sc.check(not tt._shown or not w.same(tt.GetOwner(tt), plot), "%s: no hover tooltip without a plot" % label)
        M.leave(plot)
        w.tick(1)
        return
    idx = max(0, min(n - 1, idx))
    M.cursorX = (left + idx * stepX) * es
    M.cursorY = 100
    M.enter(plot)
    w.tick(1)
    lines = w.all_lines(tt)
    t = win["times"][idx]
    if win["step"] >= 86400:
        tl = time.strftime("%a %b %d", time.gmtime(t))
    else:
        tl = time.strftime("%a %b %d, %H:%M", time.localtime(t))
    c, m = win["cheap"][idx], win["market"][idx]
    want = [(tl, None),
            ("Cheapest", money_icons(c) if c is not None else "nothing listed"),
            ("Market", money_icons(m) if m is not None else "nothing listed"),
            ("Est. sold that day" if win["step"] >= 86400 else "Est. sold that hour", commas(win["sold"][idx]))]
    sc.eq(lines, want, "%s: hover tooltip at point %d" % (label, idx))
    sc.check(w.same(tt.GetOwner(tt), plot), "%s: hover tooltip owner" % label)
    # Move to the next point: tooltip is rebuilt, not appended to.
    if idx + 1 < n:
        M.cursorX = (left + (idx + 1) * stepX) * es
        w.tick(1)
        sc.eq(len(w.all_lines(tt)), 4, "%s: hover tooltip rebuilt on move (no stacked lines)" % label)
    M.leave(plot)
    w.tick(1)
    sc.check(not tt._shown, "%s: hover tooltip hidden on leave" % label)


# ===========================================================================
# Test items. The scenarios were written for the simulated data (Linen Cloth,
# Runecloth, Black Lotus, Arcane Crystal and three items with no prices). With
# another data file, pick_roles fills the same roles from that data.
# ===========================================================================
class _Roles(object):
    LINEN, RUNE, LOTUS, ARCANE = 2589, 14047, 13468, 12363
    NO_GEAR, NO_BOLT, NO_BAG = 99901, 2996, 4238
    NO_GEAR_NAME, NO_BOLT_NAME, NO_BAG_NAME = "Recruit's Shirt", "Bolt of Linen Cloth", "Linen Bag"
    SIM = True          # the simulated data: a few checks only make sense there
    names = {}


R = _Roles()


def ROLE_IDS():
    return [R.LINEN, R.RUNE, R.LOTUS, R.ARCANE]


def N(iid):
    """The data name of a role item (they always have one)."""
    return R.names[iid]


def pick_roles(ref, items, simulated):
    idx = name_index(ref)

    def usable(i):
        it = ref.items.get(i)
        if it is None or str(i) not in items or it["cheapest"] <= 0 or it["market"] <= 0:
            return False
        nm = it["name"]
        return (known_name(nm) and len(idx.get(name_key(nm), [])) == 1 and all(ord(c) < 128 for c in nm)
                and not any(c in nm for c in '";|'))
    taken = set()

    def pick(default, order):
        if usable(default):
            taken.add(default)
            return default
        cands = sorted((i for i in ref.items if usable(i) and i not in taken), key=lambda i: (order(i), i))
        if not cands:
            raise RuntimeError("the data has no item with a name and prices to test with")
        taken.add(cands[0])
        return cands[0]
    R.SIM = simulated
    R.LINEN = pick(2589, lambda i: -ref.items[i]["listed"])
    R.RUNE = pick(14047, lambda i: -ref.items[i]["listed"])
    R.LOTUS = pick(13468, lambda i: -ref.items[i]["market"])
    R.ARCANE = pick(12363, lambda i: -ref.items[i]["market"])
    used = set()

    def free(default):
        if default not in ref.items and str(default) not in items:
            used.add(default)
            return default
        i = 99902
        while i in ref.items or str(i) in items or i in used or i in (99901, 2996, 4238):
            i += 1
        used.add(i)
        return i
    R.NO_GEAR, R.NO_BOLT, R.NO_BAG = free(99901), free(2996), free(4238)
    R.names = {i: ref.items[i]["name"] for i in ROLE_IDS()}


def partial_term(ref, name):
    """The first word of a name, made sure not to be a whole item name itself
    (so a search for it waits for the browse results)."""
    word = name.split(" ")[0].lower()
    idx = name_index(ref)
    while word in idx and len(word) > 1:
        word = word[:-1]
    return word


# ===========================================================================
# Scenarios
# ===========================================================================
def sc_static(sc, ref, items):
    """Source checks for text and calls the scenarios cannot reach."""
    files = TOC_FILES + ["GoblinLedger.toc", "GoblinLedger_Forever.toc"]
    for fname in files:
        raw = open(os.path.join(ADDON, fname), "rb").read()
        sc.check(all(b < 128 for b in raw), "%s is plain ASCII" % fname)
        txt = raw.decode("latin-1")
        # Lua escapes that would print a long dash (em or en) in game.
        for esc in (r"\226\128\148", r"\226\128\147", r"\u{2014}", r"\u{2013}"):
            sc.check(esc not in txt, "%s has no escaped long dash (%s)" % (fname, esc))
        if fname.endswith(".lua") and fname != "Data.lua":
            code = re.sub(r"--[^\n]*", "", txt)
            for api in ("PostItem", "PostCommodity", "PlaceBid", "StartCommoditiesPurchase", "ConfirmCommoditiesPurchase",
                        "CancelAuction", "SendSearchQuery", "SendBrowseQuery", "SendSellSearchQuery", "QueryAuctionItems",
                        "PostAuction", "StartAuction", "PlaceAuctionBid"):
                calls = re.findall(r"\b" + api + r"\s*\(", code)
                sc.check(not calls, "%s never calls %s" % (fname, api))
            sc.check('SetScript("OnTooltipSetItem"' not in code, "%s does not replace tooltip scripts" % fname)
            # Items are destroyed only from the drop box's Drop it (Bags.lua, once).
            n_del = len(re.findall(r"\bDeleteCursorItem\b", code))
            sc.eq(n_del, 1 if fname == "Bags.lua" else 0, "%s: DeleteCursorItem appears %s" % (
                fname, "once (the drop box)" if fname == "Bags.lua" else "nowhere"))
            for api in ("DeleteItem", "ConfirmDeleteItem", "RunScript"):
                sc.check(not re.search(r"\b" + api + r"\b", code), "%s never uses %s" % (fname, api))
    check_rename_leftovers(sc)


# The addon was called Forever AH before it was renamed Goblin Ledger. Text a
# player can see must use the new names; comments may keep the history.
OLD_NAMES = ("Forever AH", "Update-WoW-Prices.bat", "Install-WoW-Addon.bat", "Uninstall-WoW-Addon.bat",
             "grantdenney.com/wow")


LONG_OPEN = re.compile(r"\[(=*)\[")


def lua_strings(code):
    """The string literals of Lua source, in order. One pass that knows
    comments and strings apart, so a "--" inside a string is not taken for a
    comment and a quote inside a comment is not taken for a string."""
    out = []
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        if code.startswith("--", i):
            m = LONG_OPEN.match(code, i + 2)
            if m:
                end = code.find("]" + m.group(1) + "]", m.end())
                i = n if end < 0 else end + len(m.group(1)) + 2
            else:
                end = code.find("\n", i)
                i = n if end < 0 else end + 1
        elif c in "\"'":
            j, buf = i + 1, []
            while j < n and code[j] != c and code[j] != "\n":
                if code[j] == "\\" and j + 1 < n:
                    buf.append(code[j:j + 2])
                    j += 2
                else:
                    buf.append(code[j])
                    j += 1
            out.append("".join(buf))
            i = j + 1
        elif c == "[" and LONG_OPEN.match(code, i):
            m = LONG_OPEN.match(code, i)
            end = code.find("]" + m.group(1) + "]", m.end())
            out.append(code[m.end():] if end < 0 else code[m.end():end])
            i = n if end < 0 else end + len(m.group(1)) + 2
        else:
            i += 1
    return out


# "Forever" on its own is the old name, also when it is split over two
# strings ("Forever" .. " AH"). The game's own name, WoW Forever, is fine;
# identifiers such as ForeverAH_Settings or ForeverAHSidebar never match.
OLD_WORD = re.compile(r"(?<!WoW )\bForever\b")


def old_names_in(strings):
    bad = [(old, s[:80]) for s in strings for old in OLD_NAMES if old in s]
    bad += [("Forever", s[:80]) for s in strings if OLD_WORD.search(s) and not any(old in s for old in OLD_NAMES)]
    return bad


def check_rename_leftovers(sc):
    """No old name in anything a player sees: strings in the Lua, the TOC
    files and the price file placeholder. /fah only as the old alias."""
    for fname in TOC_FILES + ["GoblinLedger.toc", "GoblinLedger_Forever.toc"]:
        txt = open(os.path.join(ADDON, fname), "rb").read().decode("latin-1")
        strings = lua_strings(txt) if fname.endswith(".lua") else [txt]
        sc.eq(old_names_in(strings), [], "%s: no player-visible old name (%s, Forever)" % (fname, ", ".join(OLD_NAMES)))
        fah = [s for s in strings if fname.endswith(".lua") and re.search(r"/fah\b", s) and s != "/fah"]
        sc.eq(fah, [], "%s: /fah only as the old slash alias, never in help or messages" % fname)
    toc = open(os.path.join(ADDON, "GoblinLedger.toc"), "rb").read().decode("ascii")
    sc.check("## Title: Goblin Ledger" in toc, "TOC title is Goblin Ledger")
    sc.check("## SavedVariables: ForeverAH_Settings" in toc, "TOC keeps the ForeverAH_Settings saved variable")
    # The string finding above really sees strings, and only strings.
    probe = lua_strings('P("a /fah b") x = \'Forever AH\' y = [[Update-WoW-Prices.bat]] -- "not this"\n'
                        'z = "see -- Install-WoW-Addon.bat" --[==[ "nor [[this]]" ]==] w = [=[a]]b]=] '
                        'v = "q\\"Forever\\" AH" u = \'it\\\'s\'')
    sc.eq(probe, ["a /fah b", "Forever AH", "Update-WoW-Prices.bat", "see -- Install-WoW-Addon.bat", "a]]b",
                  'q\\"Forever\\" AH', "it\\'s"], "rename check finds every kind of Lua string")
    probe = lua_strings('T:SetText(Paint("Forever", "buy") .. Paint(" AH", "sell")) A("WoW Forever") B("ForeverAHSidebar")')
    sc.eq(old_names_in(probe), [("Forever", "Forever")], "rename check: the old name split over two strings is caught")


def sc_load(sc, ref, items):
    toc = open(os.path.join(ADDON, "GoblinLedger.toc"), "rb").read()
    toc2 = open(os.path.join(ADDON, "GoblinLedger_Forever.toc"), "rb").read()
    sc.eq(toc, toc2, "GoblinLedger_Forever.toc identical to GoblinLedger.toc")
    sc.check(not os.path.exists(os.path.join(ADDON, "ForeverAH.toc")), "no old ForeverAH.toc left in the addon folder")
    lines = [l.strip() for l in toc.decode("ascii").splitlines()]
    sc.eq([l for l in lines if l and not l.startswith("#")], TOC_FILES, "TOC file order")
    sc.eq([l for l in lines if l.startswith("##")], TOC_HEADER, "TOC header lines")
    sc.eq(ref.first_line, "-- ForeverAH price data. Generated by grantdenney.com/wow. Do not edit.", "data file first line")
    w = World(ref, items)
    sc.eq(w.load_errors, [], "all five files load")
    ns = w.ns
    sc.eq(ns.db.updated, ref.updated, "ns.db.updated")
    sc.eq(ns.db.hourEnd, ref.hourEnd, "ns.db.hourEnd")
    sc.eq(ns.db.dayEnd, ref.dayEnd, "ns.db.dayEnd")
    sc.eq(ns.ItemCount(), ref.base_count(), "item count (versions of an item not counted)")
    sc.eq(ns.DataAge(), 25 * 60, "DataAge")
    sc.eq(ns.tooltipMode, "processor", "tooltips use TooltipDataProcessor")
    S = ns.settings
    sc.check(S.tooltip is True and S.chart is True and S.chartTF == "1D" and S.compact is False,
             "default settings")
    sc.check(w.same(w.g.ForeverAH_Settings, S), "ForeverAH_Settings points at ns.settings")
    sc.eq([w.g["SLASH_GOBLINLEDGER%d" % i] for i in range(1, 6)], ["/gl", "/goblinledger", "/fah", "/foreverah", None],
          "slash names: /gl, /goblinledger, and the old /fah and /foreverah")
    sc.check(w.g.SLASH_FOREVERAH1 is None and w.g.SlashCmdList["FOREVERAH"] is None, "no second slash command list entry")
    sc.check(not w.g.ForeverAHChartFrame, "chart is not built at login (lazy)")
    sc.eq(ns.stats.series, 0, "no series decoded at login")
    sc.check(w.g.AuctionHouseFrame is None, "Blizzard AH not loaded at login")
    sc.eq(ns.SalesHours(), rec_model.sales_hours_of(ref.salesHours), "SalesHours (a full day when the file does not say)")
    idx = name_index(ref)
    unnamed = 0
    for iid, it in ref.items.items():
        g = ns.GetItem(iid)
        for k in ("cheapest", "market", "listed", "sold24", "avg7", "vendor", "auctions"):
            sc.eq(g[k], it[k], "GetItem(%d).%s" % (iid, k))
        sc.eq(g.name, w.name_field(iid), "GetItem(%d).name" % iid)
        nm = it["name"]
        if known_name(nm):
            lowest = idx[name_key(nm)][0]   # a name some items share gives the lowest id
            if all(ord(c) < 128 for c in nm):
                sc.eq(ns.FindItemID(nm.upper()), lowest, "FindItemID(%r upper)" % nm)
            sc.eq(ns.FindItemID("  " + nm.lower() + " "), lowest, "FindItemID(%r lower, padded)" % nm)
            ids = ns.FindItemIDs(nm)
            sc.eq([ids[i] for i in range(1, len(ids) + 1)], idx[name_key(nm)], "FindItemIDs(%r)" % nm)
        elif unnamed < 20:
            unnamed += 1
            game = w.game_name(iid)
            sc.eq(ns.FindItemID(nm) if nm else None, None, "unknown name %r is not searchable" % nm)
            if game:
                sc.eq(ns.FindItemID(game), min(idx.get(name_key(game), [iid])[0], iid),
                      "unnamed %d found by the game's name" % iid)
            sc.eq(ns.ItemName(iid), w.display_name(iid), "ItemName(%d) for an unknown name" % iid)
    sc.check(ns.GetItem(424242) is None, "GetItem(unknown) is nil")
    sc.check(ns.GetSeries(424242, "1D") is None, "GetSeries(unknown) is nil")
    for c in (0, 1, 99, 100, 101, 12345, 999999, 1000000, 1000001, 1234567, 35000099, -5, -12345):
        sc.eq(ns.MoneyText(c), money_text(c), "MoneyText(%d)" % c)
        sc.eq(ns.MoneyIcons(c), money_icons(c), "MoneyIcons(%d)" % c)
    sc.clean(w)


def sc_bare_coin(sc, ref, items):
    w = World(ref, items, bare_coin=True)
    sc.eq(w.ns.MoneyIcons(123456), money_icons(123456), "MoneyIcons with the bare GetCoinTextureString global")
    sc.clean(w)


def sc_decode(sc, ref, items):
    w = World(ref, items)
    check_series_and_recommend(sc, w, ref)
    n1 = int(w.ns.stats.series)
    check_series_and_recommend(sc, w, ref, ids=[R.LINEN], tfs=["1D"])
    sc.eq(int(w.ns.stats.series), n1, "series decoded once per item (cached)")
    sc.clean(w)


def sc_status(sc, ref, items):
    w = World(ref, items)
    out = w.slash("/gl")
    txt = "\n".join(out)
    # The count is written the addon's way: 6,641
    sc.check(("prices for %s items from the %s" % (commas(ref.base_count()), ref.houseName)) in txt,
             "status names item count and house (%s)" % out[:1])
    sc.check("(US, PvP)" in txt, "status names region and ruleset")
    sc.check("updated 25m ago" in txt, "status shows data age")
    sc.check("Type /gl help" in txt, "status points at help")
    nv = len(ref.base_of)
    sc.eq(any(("%s versions of gear" % commas(nv)) in l for l in out), nv > 0,
          "status says how many gear versions have their own prices (%d)" % nv)
    sc.check("old" not in txt, "no stale warning for fresh data")
    sc.eq(w.slash("/goblinledger status"), out, "/goblinledger gives the same status")
    sc.eq(w.slash("/fah"), out, "/fah (the old name) still works")
    sc.eq(w.slash("/foreverah status"), out, "/foreverah (the old name) still works")
    h = w.slash("/gl help")
    sc.check(len(h) >= 6 and any("/gl chart" in l for l in h), "help lists commands")
    u = w.slash("/gl bogus")
    sc.check(any("unknown command" in l for l in u), "unknown command message")
    sc.clean(w)


def sc_tooltip(sc, ref, items):
    w = World(ref, items)
    tt = w.g.GameTooltip
    age = w.now - ref.updated
    # Linen Cloth from a bag slot, stack of 20, shift up
    w.put_bag(0, 1, R.LINEN, 20)
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetBagItem(tt, 0, 1)
    lines = w.lines(tt)
    want, want_st = expected_tooltip(ref, R.LINEN, age, count=20, styles=True)
    sc.eq(lines, want, "2589 bag tooltip lines")
    tooltip_values_ok(sc, ref, R.LINEN, lines, 1, "2589")
    verdict_line_ok(sc, ref, R.LINEN, lines, "2589")
    tooltip_style_ok(sc, w, tt, want_st, "2589 bag")
    # Post call fires a second time without a clear: no duplicates
    for fn_i in range(1, len(w.M.postCalls[0]) + 1):
        w.M.postCalls[0][fn_i](tt, tt.processingInfo.tooltipData)
    sc.eq(w.lines(tt), lines, "2589 second post call adds nothing")
    tooltip_style_ok(sc, w, tt, want_st, "2589 second post call")
    # Rebuild (modifier change / TOOLTIP_DATA_UPDATE): cleared and rebuilt once
    tt.RefreshData(tt)
    sc.eq(w.lines(tt), lines, "2589 rebuild gives one copy")
    tooltip_style_ok(sc, w, tt, want_st, "2589 rebuild")
    heads = [l for l in w.all_lines(tt) if l[0] == "Goblin Ledger"]
    sc.eq(len(heads), 1, "one Goblin Ledger header")
    # Black Lotus by id
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetItemByID(tt, R.LOTUS)
    lines = w.lines(tt)
    want, want_st = expected_tooltip(ref, R.LOTUS, age, styles=True)
    sc.eq(lines, want, "13468 tooltip lines")
    tooltip_values_ok(sc, ref, R.LOTUS, lines, 1, "13468")
    tooltip_style_ok(sc, w, tt, want_st, "13468")
    if min(ref.items[R.LOTUS]["market"], ref.items[R.LOTUS]["cheapest"]) > 1000000:
        sc.check(all("Copper" not in (r or "") for _, r in lines[1:5]), "13468 above 100g: copper dropped")
    # Unknown item
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetItemByID(tt, R.NO_GEAR)
    sc.eq(w.lines(tt), [("Goblin Ledger: no recent listings", None)], "unknown item tooltip")
    tooltip_style_ok(sc, w, tt, [("muted", None, False)], "unknown item")
    # Shift held on a stack of 20
    w.M.shift = True
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetBagItem(tt, 0, 1)
    lines = w.lines(tt)
    want, want_st = expected_tooltip(ref, R.LINEN, age, mult=20, styles=True)
    sc.eq(lines, want, "2589 shift x20 tooltip")
    tooltip_values_ok(sc, ref, R.LINEN, lines, 20, "2589 x20")
    verdict_line_ok(sc, ref, R.LINEN, lines, "2589 x20 (Shift changes neither the verdict nor the stack line)")
    tooltip_style_ok(sc, w, tt, want_st, "2589 shift x20")
    # Shift held on a single item (by id): no multiplier
    tt.SetItemByID(tt, R.RUNE)
    sc.eq(w.lines(tt), expected_tooltip(ref, R.RUNE, age), "14047 shift, count 1: unit prices")
    # Shift released, MODIFIER_STATE_CHANGED rebuild on the bag item
    tt.SetBagItem(tt, 0, 1)
    w.M.shift = False
    tt.RefreshData(tt)
    want, want_st = expected_tooltip(ref, R.LINEN, age, count=20, styles=True)
    sc.eq(w.lines(tt), want, "2589 after shift released")
    tooltip_style_ok(sc, w, tt, want_st, "2589 after shift released")
    # Every item (a sample of a big file), full and compact
    ids = sample_ids(ref, SAMPLE * 4)
    for iid in ids:
        tt.SetItemByID(tt, iid)
        want, want_st = expected_tooltip(ref, iid, age, styles=True)
        lines = w.lines(tt)
        sc.eq(lines, want, "tooltip %d" % iid)
        verdict_line_ok(sc, ref, iid, lines, "tooltip %d" % iid)
        tooltip_style_ok(sc, w, tt, want_st, "tooltip %d" % iid)
    # The chart's timeframe (S.chartTF) picks the price window: the verdict, its stack
    # line and the Suggested price follow it
    differ = 0
    for tf in ("1M", "5D"):
        w.ns.SetChartTF(tf)
        for iid in ids:
            tt.SetItemByID(tt, iid)
            want, want_st = expected_tooltip(ref, iid, age, tf=tf, styles=True)
            differ += want != expected_tooltip(ref, iid, age)
            lines = w.lines(tt)
            sc.eq(lines, want, "tooltip %d %s" % (iid, tf))
            verdict_line_ok(sc, ref, iid, lines, "tooltip %d %s" % (iid, tf), tf=tf)
            tooltip_style_ok(sc, w, tt, want_st, "tooltip %d %s" % (iid, tf))
        tt.SetBagItem(tt, 0, 1)
        want, want_st = expected_tooltip(ref, R.LINEN, age, count=20, tf=tf, styles=True)
        lines = w.lines(tt)
        sc.eq(lines, want, "2589 bag x20 %s" % tf)
        tooltip_values_ok(sc, ref, R.LINEN, lines, 1, "2589 bag x20 %s" % tf, tf=tf)
        verdict_line_ok(sc, ref, R.LINEN, lines, "2589 bag x20 %s (the stack line)" % tf, tf=tf)
        tooltip_style_ok(sc, w, tt, want_st, "2589 bag x20 %s" % tf)
    w.ns.SetChartTF("1D")
    if R.SIM:
        sc.check(differ > 0, "the test data has tooltips that change with the timeframe (%d)" % differ)
    out = w.slash("/gl compact")
    sc.check(any("one line" in l for l in out), "/gl compact message")
    for iid in ids:
        tt.SetItemByID(tt, iid)
        want, want_st = expected_tooltip(ref, iid, age, compact=True, styles=True)
        sc.eq(w.lines(tt), want, "compact tooltip %d" % iid)
        tooltip_style_ok(sc, w, tt, want_st, "compact tooltip %d" % iid)
    w.M.shift = True
    tt.SetBagItem(tt, 0, 1)
    sc.eq(w.lines(tt), expected_tooltip(ref, R.LINEN, age, mult=20, compact=True), "compact x20 tooltip")
    w.M.shift = False
    w.slash("/gl compact")
    tt.SetItemByID(tt, R.LINEN)
    sc.eq(w.lines(tt), expected_tooltip(ref, R.LINEN, age), "full tooltip again after second /gl compact")
    # Tooltip off and on
    out = w.slash("/gl tooltip")
    sc.check(any("off" in l for l in out), "/gl tooltip off message")
    tt.SetItemByID(tt, R.LINEN)
    sc.eq(w.lines(tt), [], "tooltip off: no lines")
    w.slash("/gl tooltip")
    tt.SetItemByID(tt, R.LINEN)
    sc.eq(w.lines(tt), expected_tooltip(ref, R.LINEN, age), "tooltip on again")
    # Other tooltips: a chat link's ItemRefTooltip gets the bigger verdict too,
    # a comparison tooltip never does
    ir = w.g.ItemRefTooltip
    ir.SetHyperlink(ir, w.M.ItemLink(R.ARCANE))
    want, want_st = expected_tooltip(ref, R.ARCANE, age, styles=True)
    sc.eq(w.lines(ir), want, "ItemRefTooltip from a chat link")
    tooltip_style_ok(sc, w, ir, want_st, "ItemRefTooltip")
    st = w.g.ShoppingTooltip1
    st.SetItemByID(st, R.RUNE)
    want, want_st = expected_tooltip(ref, R.RUNE, age, styles=True)
    sc.eq(w.lines(st), want, "comparison tooltip")
    tooltip_style_ok(sc, w, st, [(lc, rc, False) for lc, rc, _ in want_st], "comparison tooltip (normal size)")
    ot = w.g.OtherAddonTooltip
    ot.SetItemByID(ot, R.LINEN)
    sc.eq(w.lines(ot), [], "unlisted tooltip gets nothing")
    ft = w.g.ForbiddenTooltip
    ft.SetItemByID(ft, R.LINEN)
    sc.eq(w.lines(ft), [], "forbidden tooltip gets nothing")
    info = w.lua.table()
    info.getterName = "GetItemByID"
    info.getterArgs = w.to_lua([R.LINEN])
    info.excludeLines = w.to_lua([1])
    tt.ProcessInfo(tt, info)
    sc.eq(w.lines(tt), [], "excludeLines tooltip gets nothing")
    sc.clean(w)
    # The average says how many days of prices it covers (up to 7), and the AH value how far
    # it sits from it: green when up, red when down, grey under 1%.
    cases = {R.LINEN: (3, 1250, 1000, "+25%"), R.RUNE: (9, 900, 1000, "-10%"), R.ARCANE: (9, 1000, 1004, "0%")}

    def fn(iid, f):
        if iid in cases:
            f = f + [""] * (10 - len(f))
            days, mk, avg, _ = cases[iid]
            f[2], f[5] = str(mk), str(avg)
            f[8] = enc_series([avg] * days, [avg] * days, [1] * days)
        return f
    text = rewrite_items(ref.text, fn)
    r2 = RefData(text=text)
    w2 = World(r2, items, data_src=text)
    tt2 = w2.g.GameTooltip
    for iid, (days, mk, avg, pct_txt) in cases.items():
        tt2.SetItemByID(tt2, iid)
        lines = w2.lines(tt2)
        sc.eq(lines, expected_tooltip(r2, iid, w2.now - r2.updated), "average test %d" % iid)
        label = "%d-day average" % min(days, 7)
        sc.check(any(a == label for a, _ in lines), "%d: a %r line (%s)" % (iid, label, lines))
        col = {"+": TT_POS, "-": TT_NEG}.get(pct_txt[0], TT_MUTED)
        ahv = [x for x in lines if x[0] == "AH value"]
        sc.check(len(ahv) == 1 and (ahv[0][1] or "").endswith("  |c%s%s|r" % (col, pct_txt)),
                 "%d: AH value %s from the average, in its colour (%s)" % (iid, pct_txt, ahv))
        sc.check(not any(a.startswith(("7-day low", "7-day high", "Cheapest", "Market", "Listed ", "Best price"))
                         for a, _ in lines), "%d: none of the old lines (%s)" % (iid, lines))
    sc.clean(w2, "average")


def big_fontstrings(w, tt):
    """Every line FontString of a tooltip (in use or not) on one of our Fonts: [(line, side, font, size)]."""
    out = []
    for i in range(1, len(tt._L) + 1):
        s = w.M.lineStyle(tt, i)
        for side, x in (("left", s.left), ("right", s.right)):
            if str(x.font).startswith("ForeverAH"):
                out.append((i, side, x.font, x.size))
    return out


def sc_tooltip_legacy_hooks(sc, ref, items):
    w = World(ref, items, client="legacy")
    sc.eq(w.ns.tooltipMode, "script", "legacy client: OnTooltipSetItem fallback")
    tt = w.g.GameTooltip
    age = w.now - ref.updated
    record_big_at_show(w)
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetItemByID(tt, R.LINEN)
    want, want_st = expected_tooltip(ref, R.LINEN, age, styles=True)
    sc.eq(w.lines(tt), want, "legacy tooltip lines")
    tooltip_style_ok(sc, w, tt, want_st, "legacy tooltip")
    big_at_show_ok(sc, w, tt, want_st, "legacy")
    w.M.fireScript(tt, "OnTooltipSetItem")
    sc.eq(w.lines(tt), want, "legacy second OnTooltipSetItem adds nothing")
    tooltip_style_ok(sc, w, tt, want_st, "legacy second OnTooltipSetItem")
    tt.ClearLines(tt)
    sc.eq(big_fontstrings(w, tt), [], "legacy: a clear puts the normal font back")
    sc.clean(w)
    # Silk Cloth by link (count 1): the spec's lines, the verdict bigger before tt:Show()
    text, items2, ids = verdict_data(ref, items)
    r2 = RefData(text=text)
    w2 = World(r2, items2, client="legacy", data_src=text)
    t2 = w2.g.GameTooltip
    silk = ids["silk"]
    record_big_at_show(w2)
    t2.SetOwner(t2, w2.g.UIParent, "ANCHOR_RIGHT")
    t2.SetHyperlink(t2, w2.M.ItemLink(silk))
    want, want_st = expected_tooltip(r2, silk, w2.now - r2.updated, styles=True)
    lines = w2.lines(t2)
    sc.eq(lines, want, "legacy Silk by link")
    sc.eq(lines[1:6], [("Auction it", "+26% vs vendor"), ("AH value", money_icons(200) + "  " + vs_average(200, 199)),
                       ("2-day average", money_icons(199)), ("Sell now", money_icons(157)), ("Suggested price", money_icons(200))],
          "legacy Silk by link: the spec's lines")
    tooltip_style_ok(sc, w2, t2, want_st, "legacy Silk by link")
    big_at_show_ok(sc, w2, t2, want_st, "legacy Silk by link")
    t2.ClearLines(t2)
    sc.eq(big_fontstrings(w2, t2), [], "legacy Silk: a clear puts the normal font back")
    sc.clean(w2, "legacy Silk")


def record_big_at_show(w):
    """Notes which lines of GameTooltip are on our bigger Font when tt:Show() is
    called (the legacy path calls it to lay the tooltip out again)."""
    w.M.bigAtShow = None
    w.lua.execute("""
      local tt = GameTooltip
      local show = tt.Show
      tt.Show = function(self, ...)
        local big = {}
        for i = 1, #self._L do
          local s = MOCK.lineStyle(self, i)
          if tostring(s.left.font):find("ForeverAH", 1, true) then big[#big + 1] = i end
        end
        MOCK.bigAtShow = big
        return show(self, ...)
      end""")


def big_at_show_ok(sc, w, tt, want_st, label):
    got = [w.M.bigAtShow[i] for i in range(1, len(w.M.bigAtShow) + 1)] if w.M.bigAtShow else None
    want = [int(tt._blz) + 1 + k for k, (_, _, big) in enumerate(want_st) if big]
    sc.eq(got, want, "%s: the verdict is bigger before the addon's tt:Show()" % label)


def sc_stale(sc, ref, items):
    w = World(ref, items, now=ref.updated + 7 * 3600 + 120)
    age = w.now - ref.updated
    tt = w.g.GameTooltip
    tt.SetItemByID(tt, R.LINEN)
    lines = w.lines(tt)
    want, want_st = expected_tooltip(ref, R.LINEN, age, styles=True)
    sc.eq(lines, want, "stale tooltip lines")
    sc.check(lines[-1][0] == "Prices are 7h old: run Install-Goblin-Ledger.bat, then /reload", "stale line text")
    sc.check(not any(big for _, _, big in want_st), "stale: the model never makes the verdict bigger")
    tooltip_style_ok(sc, w, tt, want_st, "stale (the verdict at the normal size)")
    for iid in sample_ids(ref, SAMPLE):
        tt.SetItemByID(tt, iid)
        want, want_st = expected_tooltip(ref, iid, age, styles=True)
        sc.eq(w.lines(tt), want, "stale tooltip %d" % iid)
        tooltip_style_ok(sc, w, tt, want_st, "stale tooltip %d" % iid)
    w.slash("/gl compact")
    tt.SetItemByID(tt, R.LINEN)
    sc.eq(w.lines(tt), expected_tooltip(ref, R.LINEN, age, compact=True), "stale compact tooltip keeps the stale line")
    out = "\n".join(w.slash("/gl"))
    sc.check("updated 7h ago" in out, "status shows 7h")
    sc.check("Prices are 7h old: run Install-Goblin-Ledger.bat, then /reload." in out, "status stale warning")
    w.slash("/gl chart " + N(R.LINEN))
    sc.eq(w.ns.ChartPanel.age._text, "Data: 7h ago \u00b7 " + ref.houseName, "chart footer shows 7h")
    sc.clean(w)


def sc_placeholder(sc, ref, items):
    src = open(os.path.join(ADDON, "Data.lua"), "rb").read().decode("ascii")
    # Git may check it out with either line ending; WoW reads both.
    sc.eq(src.replace("\r\n", "\n"), PLACEHOLDER, "repo Data.lua is the contract placeholder")
    w = World(ref, items, data_src=src)
    ns = w.ns
    sc.eq(w.load_errors, [], "loads with the placeholder")
    sc.check(ns.DataAge() is None, "DataAge nil")
    sc.eq(ns.ItemCount(), 0, "no items")
    sc.check(any("no price data yet" in c for c in w.chat()), "login hint about missing data")
    out = w.slash("/gl")
    sc.check(any("Install-Goblin-Ledger.bat" in l for l in out) and any("/reload" in l for l in out), "status explains the updater")
    tt = w.g.GameTooltip
    tt.SetItemByID(tt, R.LINEN)
    sc.eq(w.lines(tt), [], "no tooltip lines without data")
    out = w.slash("/gl chart Linen Cloth")
    sc.check(any("no price data" in l for l in out), "/gl chart item: no data message")
    w.slash("/gl chart")
    C = ns.ChartPanel
    sc.check(C.frame.IsShown(C.frame), "/gl chart opens the empty chart")
    sc.check(C.msg._shown and "No price data yet" in (C.msg._text or ""), "empty chart message (%s)" % C.msg._text)
    sc.eq(C.age._text, "Data: none yet", "empty chart age")
    w.slash("/gl chart")
    sc.check(not C.frame.IsShown(C.frame), "/gl chart toggles it off")
    # Whole AH flow with no data
    w.put_bag(0, 1, R.LINEN, 20)
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    sc.check(C.frame.IsShown(C.frame) and C.msg._shown, "AH open: chart shows the no-data message")
    ah.SearchBar.SearchBox.SetText(ah.SearchBar.SearchBox, "Linen Cloth")
    ah.SearchBar.StartSearch(ah.SearchBar)
    w.set_browse([(R.LINEN, 47, 900)])
    w.M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.5)
    r = w.lua.table()
    r.itemKey = w.to_lua({"itemID": R.LINEN, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0})
    r.minPrice = 47
    ah.SelectBrowseResult(ah, r)
    w.settle(0.2)
    sc.check(C.msg._shown, "still the no-data message after a search")
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1))
    w.settle(0.2)
    # The sell buttons stay hidden without data
    buttons_ok(sc, find_sell_buttons(ah.CommoditiesSellFrame), (None, None), "no data: sell buttons")
    w.close_ah_modern()
    sc.check(not C.frame.IsShown(C.frame), "chart hidden on AH close")
    sc.clean(w)


def sc_saved_settings(sc, ref, items):
    w = World(ref, items, saved={"chartTF": "1w", "compact": True, "tooltip": "yes", "chart": False,
                                  "chartPoint": ["TOPLEFT", "TOPLEFT", 100, -200], "lastItem": R.RUNE})
    S = w.ns.settings
    sc.eq(S.chartTF, "1W", "saved chartTF normalised")
    sc.eq(S.compact, True, "saved compact kept")
    sc.eq(S.tooltip, True, "bad saved tooltip type falls back to the default")
    sc.eq(S.chart, False, "saved chart=false kept")
    sc.check(w.same(w.g.ForeverAH_Settings, S), "ForeverAH_Settings is ns.settings")
    w.slash("/gl chart")
    C = w.ns.ChartPanel
    sc.eq(w.ns.ChartItem(), R.RUNE, "/gl chart reopens the last item")
    p = C.frame._points[1]
    sc.check(p[1] == "TOPLEFT" and w.same(p[2], w.g.UIParent) and p[4] == 100 and p[5] == -200, "saved chart position used")
    chart_check(sc, w, ref, R.RUNE, "1W", "saved 1W")
    sc.clean(w)


def sc_chart_standalone(sc, ref, items):
    w = World(ref, items)
    ns = w.ns
    out = w.slash("/gl chart " + N(R.RUNE))
    C = ns.ChartPanel
    F = C.frame
    sc.check(F.IsShown(F), "/gl chart Runecloth shows the chart")
    sc.eq(ns.ChartSource(), "slash", "chart source")
    sc.check(not ns.ChartIsDocked(), "standalone chart is not docked")
    sc.eq(C.placement, "float", "placement float")
    sc.check(F._movable, "standalone chart is movable")
    r = w.rect(F)
    sc.check(r is not None and r[0] >= 0 and r[2] <= w.M.screenW and r[1] >= 0 and r[3] <= w.M.screenH,
             "standalone chart on screen (%s)" % (r,))
    chart_check(sc, w, ref, R.RUNE, "1D", "Runecloth 1D")
    hover_check(sc, w, ref, R.RUNE, "1D", 5, "Runecloth 1D")
    hover_check(sc, w, ref, R.RUNE, "1D", ref.window(R.RUNE, "1D")["n"] - 1, "Runecloth 1D last")
    # Link and id forms
    w.slash("/gl chart " + w.M.ItemLink(R.LOTUS))
    sc.eq(ns.ChartItem(), R.LOTUS, "/gl chart with an item link")
    w.slash("/gl chart %d" % R.LINEN)
    sc.eq(ns.ChartItem(), R.LINEN, "/gl chart with an item id")
    out = w.slash("/gl chart Not A Real Item")
    sc.check(any("no price data for Not A Real Item" in l for l in out), "unknown name message")
    sc.eq(ns.ChartItem(), R.LINEN, "unknown name leaves the chart alone")
    # Drag saves the position, Escape closes
    w.M.drag(F, "CENTER", "CENTER", 40, 30)
    cp = ns.settings.chartPoint
    sc.check(cp is not None and cp[1] == "CENTER" and cp[3] == 40 and cp[4] == 30, "drag saves chartPoint")
    sc.check("ForeverAHChartFrame" in [w.g.UISpecialFrames[i] for i in range(1, len(w.g.UISpecialFrames) + 1)],
             "chart closes with Escape")
    sc.eq(w.M.pressEscape(), "ForeverAHChartFrame", "Escape hides the chart")
    sc.check(not F.IsShown(F), "chart hidden after Escape")
    w.slash("/gl chart")
    sc.check(F.IsShown(F) and ns.ChartItem() == R.LINEN, "/gl chart reopens the last item")
    # Close button while standalone keeps settings.chart
    M = w.M
    M.click(C.closeButton)
    sc.check(not F.IsShown(F), "close button hides the chart")
    sc.eq(ns.settings.chart, True, "closing a standalone chart keeps the AH chart setting")
    sc.clean(w)


def sc_chart_timeframes(sc, ref, items):
    w = World(ref, items)
    ns, M = w.ns, w.M
    w.slash("/gl chart " + N(R.RUNE))
    C = ns.ChartPanel
    for tf in TFS:
        M.click(C.tfButtons[tf])
        w.settle(0.1)
        chart_check(sc, w, ref, R.RUNE, tf, "Runecloth %s" % tf)
        win = ref.window(R.RUNE, tf)
        hover_check(sc, w, ref, R.RUNE, tf, win["n"] // 2, "Runecloth %s mid" % tf)
    # Every item (a sample of a big file), every timeframe
    ids = sample_ids(ref)
    for iid in ids:
        for tf in TFS:
            ns.SetChartTF(tf)
            ns.ShowChart(iid, "slash")
            chart_check(sc, w, ref, iid, tf, "%d %s" % (iid, tf))
    # Pools: a second full pass must not create any new textures, lines or font strings
    before = {k: int(w.M.counts[k] or 0) for k in ("Texture", "Line", "FontString", "Frame", "Button")}
    for iid in ids:
        for tf in TFS:
            ns.SetChartTF(tf)
            ns.ShowChart(iid, "slash")
    after = {k: int(w.M.counts[k] or 0) for k in before}
    sc.eq(after, before, "second pass over every chart reuses pooled widgets")
    # Lower case and bad timeframe input
    ns.SetChartTF("5d")
    sc.eq(ns.settings.chartTF, "5D", "SetChartTF lower case")
    ns.SetChartTF("2D")
    sc.eq(ns.settings.chartTF, "5D", "SetChartTF ignores a bad timeframe")
    # Chart must not have changed the cached series
    check_series_and_recommend(sc, w, ref)
    sc.clean(w)


def sc_chart_no_setowner_clear(sc, ref, items):
    """If SetOwner does not clear the tooltip on this client, hover lines must still not stack."""
    w = World(ref, items, set_owner_clears=False)
    w.slash("/gl chart " + N(R.RUNE))
    hover_check(sc, w, ref, R.RUNE, "1D", 3, "no-clear SetOwner")
    sc.clean(w)


def find_sell_buttons(sf):
    """(Sell now, Best price) buttons on a Blizzard sell frame."""
    now = best = None
    for i in range(1, len(sf._children) + 1):
        c = sf._children[i]
        if c._otype == "Button" and c.isCommodity is not None:
            if c.kind == "now":
                now = c
            elif c.kind == "best":
                best = c
    return now, best


def ref_buttons(ref, iid, tf="1D", live=None, own=False, is_commodity=True, copper_ok=True, stale=False):
    """The prices the Sell now and Best price buttons show and fill (rounded for
    the frame), as (now, best); None = that button is hidden. stale: the data is
    over 6 hours old, so without a live listing there are no buttons."""
    if stale and not (live and live > 0):
        return None, None
    P = ref_sell_prices(ref, iid, tf, live, own)
    now = price_for_frame(P["now"], is_commodity, copper_ok) if P["now"] else None
    best = price_for_frame(P["best"], is_commodity, copper_ok) if P["best"] else None
    return now, best


def buttons_ok(sc, pair, want, label):
    """The two buttons show exactly the model's prices (or are hidden)."""
    nb, bb = pair
    for b, w_, word in ((nb, want[0], "Sell now"), (bb, want[1], "Suggested")):
        if not sc.check(b is not None, "%s: %s button exists" % (label, word)):
            continue
        sc.eq(bool(b.IsVisible(b)), w_ is not None, "%s: %s button %s" % (label, word, "shown" if w_ is not None else "hidden"))
        if w_ is not None:
            sc.eq(b._text, "%s: %s" % (word, money_icons(w_)), "%s: %s text" % (label, word))
            sc.eq(b.price, w_, "%s: %s fills that price" % (label, word))


def sc_ah_modern(sc, ref, items, screen=None, ah_width=800, label="default screen", placement=None):
    w = World(ref, items, screen=screen, ah_width=ah_width)
    ns, M = w.ns, w.M
    w.put_bag(0, 1, R.RUNE, 20)
    w.put_bag(0, 2, R.ARCANE, 3)
    w.put_bag(0, 3, R.NO_GEAR, 1)
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    C = ns.ChartPanel
    sc.check(ah is not None and ah._shown, "%s: AuctionHouseFrame shown" % label)
    sc.check(ns.AH_IsOpen(), "%s: AH_IsOpen" % label)
    sc.check(C is not None and C.frame.IsShown(C.frame), "%s: chart opens with the AH" % label)
    sc.check(ns.ChartIsDocked(), "%s: chart docked" % label)
    sc.eq(ns.ChartSource(), "ah", "%s: chart source" % label)
    sc.check(C.msg._shown and "Search for an item" in (C.msg._text or ""), "%s: no item yet message" % label)
    # Placement
    ra, rc = w.rect(ah), w.rect(C.frame)
    sc.eq(C.frame._strata, "MEDIUM", "%s: chart is in the MEDIUM strata (same as the AH and bags)" % label)
    if placement:
        sc.eq(C.placement, placement, "%s: placement" % label)
    if C.placement == "below":
        F = C.frame
        sc.check(abs(F.GetTop(F) - (ah.GetBottom(ah) - 32)) < 1e-6 and abs(F.GetLeft(F) - ah.GetLeft(ah)) < 1e-6,
                 "%s: chart sits 32 below the AH (top %s, AH bottom %s)" % (label, F.GetTop(F), ah.GetBottom(ah)))
        sc.eq(C.frame._w, ah_width, "%s: chart width follows the AH" % label)
        tabs = w.rect(ah.BuyTab)
        sc.check(rc[3] <= tabs[1] + 1e-6, "%s: chart clears the AH tabs" % label)
        room = ah.GetBottom(ah) - 36
        sc.eq(C.frame._h, min(260, int(room)), "%s: chart height fits under the AH (room %s)" % (label, room))
        sc.check(rc[1] >= -1e-6, "%s: chart bottom on screen" % label)
    else:
        sc.eq(C.placement, "right", "%s: placement right when there is no room below" % label)
        sc.check(rc[0] >= ra[2] and abs(rc[3] - ra[3]) < 1e-6, "%s: chart sits right of the AH" % label)
    sc.check(rc[0] >= -1e-6 and rc[2] <= w.M.screenW + 1e-6 and rc[1] >= -1e-6 and rc[3] <= w.M.screenH + 1e-6,
             "%s: docked chart on screen (%s, screen %sx%s)" % (label, rc, w.M.screenW, w.M.screenH))
    # Chart toggle button exists on the AH frame
    toggle = None
    for i in range(1, len(ah._children) + 1):
        c = ah._children[i]
        if c._otype == "Button" and c._text == "Chart":
            toggle = c
    sc.check(toggle is not None and toggle.IsVisible(toggle), "%s: Chart toggle button on the AH" % label)

    # Search "Linen Cloth" from the Blizzard search bar
    sb = ah.SearchBar
    sb.SearchBox.SetText(sb.SearchBox, N(R.LINEN))
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    sc.eq(w.M.cah.browse[len(w.M.cah.browse)], N(R.LINEN), "%s: Blizzard sent the browse query" % label)
    w.set_browse([(R.NO_BOLT, 3000, 40), (R.LINEN, 46, 900), (R.NO_BAG, 2500, 3)])
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.4)
    chart_check(sc, w, ref, R.LINEN, "1D", "%s: search Linen Cloth" % label)
    # Partial search picks the first result with data
    sb.SearchBox.SetText(sb.SearchBox, partial_term(ref, N(R.LINEN)))
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), R.LINEN, "%s: partial search waits for results" % label)
    w.set_browse([(R.NO_BAG, 2500, 3), (R.LINEN, 46, 900)])
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), R.LINEN, "%s: partial search picks the first result with prices" % label)
    # Empty search string (full scan / category browse) is ignored
    sb.SearchBox.SetText(sb.SearchBox, "")
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.set_browse([(R.RUNE, 100, 10)])
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), R.LINEN, "%s: empty search ignored" % label)
    # Auctionator-style search text
    sb.SearchBox.SetText(sb.SearchBox, '"%s";;0;0;0;0;0;0;0;0' % N(R.LOTUS))
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), R.LOTUS, "%s: Auctionator search syntax" % label)
    # Burst of searches redraws once, last one wins
    for name in (N(R.RUNE), N(R.ARCANE), N(R.LINEN)):
        sb.SearchBox.SetText(sb.SearchBox, name)
        M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), R.LINEN, "%s: burst of searches ends on the last" % label)

    # Select a browse result (commodity)
    r = w.lua.table()
    r.itemKey = w.to_lua({"itemID": R.RUNE, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0})
    r.minPrice = 1200
    ah.SelectBrowseResult(ah, r)
    sc.eq(ns.ChartItem(), R.RUNE, "%s: selecting a result switches the chart at once" % label)
    sc.eq(ah.CommoditiesBuyFrame.GetItemID(ah.CommoditiesBuyFrame), R.RUNE, "%s: commodity buy frame shows it" % label)
    sc.check(ah.CommoditiesBuyFrame._shown, "%s: commodity buy display mode" % label)
    chart_check(sc, w, ref, R.RUNE, "1D", "%s: selected Runecloth" % label)
    # A pending search does not override the explicit pick
    sb.SearchBox.SetText(sb.SearchBox, N(R.LOTUS))
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    ah.SelectBrowseResult(ah, r)
    w.settle(0.4)
    sc.eq(ns.ChartItem(), R.RUNE, "%s: explicit pick beats a pending search" % label)
    # Select a gear result we have no data for
    r2 = w.lua.table()
    r2.itemKey = w.to_lua({"itemID": R.NO_GEAR, "itemLevel": 5, "itemSuffix": 0, "battlePetSpeciesID": 0})
    r2.minPrice = 500
    ah.SelectBrowseResult(ah, r2)
    sc.check(ah.ItemBuyFrame._shown and ah.ItemBuyFrame.itemKey.itemID == R.NO_GEAR, "%s: item buy frame" % label)
    sc.check(C.msg._shown and ("No recent listings for " + R.NO_GEAR_NAME) in (C.msg._text or ""),
             "%s: no-data message for an item without prices (%s)" % (label, C.msg._text))
    sc.eq(C.buyBox.value._text, "-", "%s: empty Target Buy box" % label)

    # Timeframe switch while docked
    M.click(C.tfButtons["1M"])
    ah.SelectBrowseResult(ah, r)
    chart_check(sc, w, ref, R.RUNE, "1M", "%s: docked 1M" % label)
    M.click(C.tfButtons["1D"])

    # Sell frame: Runecloth (commodity), Blizzard fills its default then results arrive
    lowest = ref.items[R.RUNE]["cheapest"]
    res = w.lua.table()
    res[1] = w.to_lua({"unitPrice": lowest, "quantity": 50})
    w.M.commodityResults[R.RUNE] = res
    loc = w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1)
    ah.SetPostItem(ah, loc)
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.RUNE)
    w.settle(0.2)
    csf = ah.CommoditiesSellFrame
    sc.check(csf._shown, "%s: commodity sell frame shown" % label)
    sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), lowest, "%s: Blizzard auto-filled the lowest price first" % label)
    sc.eq(ns.ChartItem(), R.RUNE, "%s: sell item switches the chart" % label)
    pair = find_sell_buttons(csf)
    b, bb = pair
    # the live listing is loaded: Sell now undercuts it, Best price from the history
    want = ref_buttons(ref, R.RUNE, "1D", lowest)
    buttons_ok(sc, pair, want, label + ": commodity sell")
    sc.check(want[0] is not None, "%s: Sell now shown with a live listing" % label)
    if b is not None and want[0] is not None:
        r_b, r_p = w.rect(b), w.rect(csf.PostButton)
        sc.check(r_b and r_p and r_b[3] <= r_p[1] + 1e-6, "%s: Sell now sits under Post" % label)
        if want[1] is not None:
            r_bb = w.rect(bb)
            sc.check(r_bb and r_bb[3] <= r_p[1] + 1e-6 and r_bb[0] >= r_b[2] - 1e-6 and abs(r_bb[3] - r_b[3]) < 1e-6,
                     "%s: Best price sits beside it, also under Post (%s, %s)" % (label, r_b, r_bb))
            sc.check(r_b[0] >= w.rect(csf)[0] - 1e-6 and r_bb[2] <= w.rect(csf)[2] + 1e-6, "%s: both inside the sell frame" % label)
        else:
            sc.check(abs((r_b[0] + r_b[2]) / 2 - (r_p[0] + r_p[2]) / 2) < 1e-6, "%s: a lone Sell now is centred under Post" % label)
        before = csf.postStateUpdates
        M.click(b)
        sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want[0], "%s: Sell now fills the exact copper price" % label)
        sc.check(csf.postStateUpdates > before, "%s: post state refreshed" % label)
        sc.eq(csf.totalPrice, want[0] * 20, "%s: total price refreshed" % label)
        M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.RUNE)
        sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want[0], "%s: later results do not overwrite our price" % label)
        if want[1] is not None:
            M.click(bb)
            sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want[1], "%s: Best price fills its price" % label)
            sc.check(want[1] >= want[0] or ref_recommend(ref, R.RUNE, "1D", lowest)["far"],
                     "%s: Best price is never under a normal cheapest listing" % label)
    sc.eq(len(w.M.cah.posted), 0, "%s: nothing posted" % label)

    # Sell frame: an item auction (gear path) rounds down to whole silver
    w.M["items"][R.ARCANE].commodity = False
    loc2 = w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 2)
    ah.SetPostItem(ah, loc2)
    w.settle(0.2)
    isf = ah.ItemSellFrame
    want2 = ref_buttons(ref, R.ARCANE, "1D", w.live(R.ARCANE), is_commodity=False)
    pair2 = find_sell_buttons(isf)
    b2 = pair2[0]
    buttons_ok(sc, pair2, want2, label + ": item sell (whole silver)")
    sc.check(not b.IsVisible(b) and not bb.IsVisible(bb), "%s: commodity buttons hidden once that frame is cleared" % label)
    for k, wv in enumerate(want2):
        if wv is not None:
            M.click(pair2[k])
            sc.eq(isf.PriceInput.GetAmount(isf.PriceInput), wv, "%s: item price rounded to silver (%d)" % (label, k))
            sc.eq(wv % 100, 0, "%s: item price is whole silver (%d)" % (label, k))
    w.M["items"][R.ARCANE].commodity = True
    # An item we have no advice for: buttons hidden
    loc3 = w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 3)
    ah.SetPostItem(ah, loc3)
    w.settle(0.2)
    sc.check(not any(x.IsVisible(x) for x in pair2), "%s: buttons hidden for an item without data" % label)

    # Close button on the docked chart turns the AH chart off; toggle brings it back
    M.click(C.closeButton)
    sc.check(not C.frame.IsShown(C.frame), "%s: close button hides the docked chart" % label)
    sc.eq(ns.settings.chart, False, "%s: closing the docked chart sets chart=false" % label)
    ah.SelectBrowseResult(ah, r)
    sc.check(not C.frame.IsShown(C.frame), "%s: chart stays hidden while turned off" % label)
    if toggle is not None:
        M.click(toggle)
        sc.check(C.frame.IsShown(C.frame) and ns.ChartItem() == R.RUNE, "%s: Chart button shows it again with the last item" % label)
        sc.eq(ns.settings.chart, True, "%s: Chart button turns the setting back on" % label)
    # /gl chart at the AH after closing it with x: follows searches again
    M.click(C.closeButton)
    w.slash("/gl chart")
    sc.check(C.frame.IsShown(C.frame), "%s: /gl chart at the AH shows the chart" % label)
    sb.SearchBox.SetText(sb.SearchBox, N(R.LOTUS))
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), R.LOTUS, "%s: chart opened with /gl chart at the AH follows searches" % label)
    w.slash("/gl chart")
    sc.check(not C.frame.IsShown(C.frame), "%s: /gl chart hides the docked chart" % label)
    sc.eq(ns.settings.chart, False, "%s: /gl chart hide at the AH turns the AH chart off" % label)
    sb.SearchBox.SetText(sb.SearchBox, N(R.RUNE))
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.check(not C.frame.IsShown(C.frame), "%s: hidden chart stays hidden on search" % label)
    w.slash("/gl chart " + N(R.LINEN))
    sc.check(C.frame.IsShown(C.frame) and ns.ChartIsDocked() and ns.ChartItem() == R.LINEN,
             "%s: /gl chart Linen Cloth at the AH shows it docked" % label)
    sb.SearchBox.SetText(sb.SearchBox, N(R.LOTUS))
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), R.LOTUS, "%s: /gl chart <item> at the AH follows searches afterwards" % label)

    # Close the AH
    w.close_ah_modern()
    sc.check(not ns.AH_IsOpen(), "%s: AH closed" % label)
    sc.check(not C.frame.IsShown(C.frame), "%s: docked chart hidden on close" % label)
    sc.check(not any(x._shown for x in pair2), "%s: item sell buttons hidden on close" % label)
    sc.check(not any(x._shown for x in pair), "%s: commodity sell buttons hidden on close" % label)
    # Searches after close are ignored
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.3)
    sc.check(not C.frame.IsShown(C.frame), "%s: events after close do not reopen the chart" % label)
    # Reopen: chart comes back docked with the last item
    w.open_ah_modern()
    sc.check(C.frame.IsShown(C.frame) and ns.ChartIsDocked() and ns.ChartItem() == R.LOTUS, "%s: reopen restores the docked chart" % label)
    w.close_ah_modern()
    # Standalone chart after the AH is closed floats again
    w.slash("/gl chart " + N(R.RUNE))
    sc.check(C.frame.IsShown(C.frame) and not ns.ChartIsDocked(), "%s: standalone chart after close floats" % label)
    chart_check(sc, w, ref, R.RUNE, "1D", "%s: standalone after AH" % label)
    sc.clean(w, label)


def sc_ah_modern_tall(sc, ref, items):
    # 4K-ish screen with a small UI scale: there is room under the AH.
    sc_ah_modern(sc, ref, items, screen=(768 * 16 / 9, 768, 0.64), label="tall screen", placement="below")


def sc_ah_modern_short(sc, ref, items):
    # A UI scale where there is some room under the AH, but not 260: the chart
    # still docks under it, shorter, without the two boxes.
    sc_ah_modern(sc, ref, items, screen=(768 * 16 / 9, 768, 0.85), label="short room", placement="below")


def sc_ah_modern_elvui(sc, ref, items):
    sc_ah_modern(sc, ref, items, screen=(768 * 16 / 9, 768, 0.64), ah_width=810, label="ElvUI width 810")


def sc_ah_tsm(sc, ref, items):
    """TSM's own AH UI shrinks AuctionHouseFrame. The chart must float instead of shrinking with it."""
    w = World(ref, items)
    ns, M = w.ns, w.M
    M.tsmShrink = True
    w.open_ah_modern()
    C = ns.ChartPanel
    sc.check(C.frame.IsShown(C.frame), "TSM: chart shown")
    sc.check(not ns.ChartIsDocked(), "TSM: chart floats when the AH frame is shrunk")
    rc = w.rect(C.frame)
    sc.check(rc[2] - rc[0] > 400, "TSM: chart has a usable size (%s)" % (rc,))
    r = w.lua.table()
    r.itemKey = w.to_lua({"itemID": R.LINEN, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0})
    r.minPrice = 46
    w.g.AuctionHouseFrame.SelectBrowseResult(w.g.AuctionHouseFrame, r)
    chart_check(sc, w, ref, R.LINEN, "1D", "TSM select")
    w.close_ah_modern()
    sc.check(not C.frame.IsShown(C.frame), "TSM: chart hidden on close")
    sc.clean(w)


def sc_ah_legacy(sc, ref, items):
    w = World(ref, items, client="legacy")
    ns, M = w.ns, w.M
    M.fire("AUCTION_HOUSE_SHOW")
    w.settle(0.3)
    af = w.g.AuctionFrame
    C = ns.ChartPanel
    sc.check(af is not None and af._shown, "legacy: AuctionFrame shown")
    sc.eq(ns.AHState.kind, "legacy", "legacy: kind")
    sc.check(C.frame.IsShown(C.frame) and ns.ChartIsDocked(), "legacy: chart docked")
    # Search
    w.g.BrowseName.SetText(w.g.BrowseName, N(R.LINEN))
    M.click(w.g.BrowseSearchButton)
    sc.eq(M.cah.browse[len(M.cah.browse)], N(R.LINEN), "legacy: Blizzard sent QueryAuctionItems")
    lst = w.lua.table()
    for i, (iid, cnt, bo) in enumerate([(R.NO_BOLT, 5, 3000), (R.LINEN, 20, 940)]):
        lst[i + 1] = w.to_lua({"itemID": iid, "count": cnt, "buyout": bo})
    M.legacyList = lst
    M.fire("AUCTION_ITEM_LIST_UPDATE")
    w.settle(0.4)
    chart_check(sc, w, ref, R.LINEN, "1D", "legacy search")
    # Partial search, then click a row
    w.g.BrowseName.SetText(w.g.BrowseName, partial_term(ref, "cloth"))
    M.click(w.g.BrowseSearchButton)
    lst = w.lua.table()
    for i, (iid, cnt, bo) in enumerate([(R.NO_BAG, 1, 3000), (R.RUNE, 20, 9000), (R.LINEN, 20, 940)]):
        lst[i + 1] = w.to_lua({"itemID": iid, "count": cnt, "buyout": bo})
    M.legacyList = lst
    M.fire("AUCTION_ITEM_LIST_UPDATE")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), R.RUNE, "legacy: partial search picks the first row with prices")
    M.click(w.g.BrowseButton3)
    sc.eq(ns.ChartItem(), R.LINEN, "legacy: clicking a row switches the chart")
    # Sell: put 20 Runecloth in the sell slot
    M.cursorItem = w.to_lua({"itemID": R.RUNE, "count": 20})
    M.click(w.g.AuctionsItemButton)
    w.settle(0.2)
    sc.eq(ns.ChartItem(), R.RUNE, "legacy: sell item switches the chart")
    want = ref_buttons(ref, R.RUNE, "1D")   # the old AH has no live listings to follow
    auc = w.g.AuctionFrameAuctions
    pair = find_sell_buttons(auc)
    buttons_ok(sc, pair, want, "legacy")
    for k, unit in enumerate(want):
        btn = pair[k]
        if btn is None or unit is None:
            continue
        w.g.AuctionsStackSizeEntry.SetText(w.g.AuctionsStackSizeEntry, "20")
        auc.priceType = 2
        M.click(btn)
        sc.eq(w.g.BuyoutPrice._copper, unit * 20, "legacy %d: per-stack buyout" % k)
        sc.eq(w.g.StartPrice._copper, unit * 20, "legacy %d: per-stack start price" % k)
        auc.priceType = 1
        M.click(btn)
        sc.eq(w.g.BuyoutPrice._copper, unit, "legacy %d: per-unit buyout" % k)
    if pair[0] is not None and want[0] is not None:
        r_b, r_c = w.rect(pair[0]), w.rect(w.g.AuctionsCreateAuctionButton)
        sc.check(r_b and r_c and abs(r_b[1] - (r_c[3] + 4)) < 1e-6, "legacy: buttons sit just above Create Auction")
    sc.eq(len(M.cah.posted), 0, "legacy: nothing posted")
    M.fire("AUCTION_HOUSE_CLOSED")
    w.settle(0.3)
    sc.check(not af._shown and not C.frame.IsShown(C.frame), "legacy: closed and chart hidden")
    sc.check(not any(b is not None and b._shown for b in pair), "legacy: buttons hidden on close")
    sc.clean(w)


def sc_auctionator(sc, ref, items):
    w = World(ref, items, auctionator=True)
    ns, M = w.ns, w.M
    sc.check(ns.AHState.hookedAtr, "Auctionator EventBus hooked at login")
    w.put_bag(0, 1, R.RUNE, 20)
    w.open_ah_modern()
    # the auction house has loaded the item's listings (cheapest = the snapshot's)
    lowest = ref.items[R.RUNE]["cheapest"]
    w.set_commodity_results(R.RUNE, [(lowest, 50, False)])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.RUNE)
    w.settle(0.2)
    C = ns.ChartPanel
    A = w.g.Auctionator
    bus = A.EventBus
    row = w.to_lua({"itemKey": {"itemID": R.ARCANE, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}})
    bus.Fire(bus, M.atrSource, A.Buying.Events.ShowCommodityBuy, row, w.to_lua({"isCommodity": True}))
    chart_check(sc, w, ref, R.ARCANE, "1D", "Auctionator shopping: commodity buy")
    # Selling tab
    sell = w.g.AuctionatorSellingFrame
    sell.Show(sell)
    loc = w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1)
    info = w.lua.table()
    info.itemLink = M.ItemLink(R.RUNE)
    info.location = loc
    info.count = 20
    M.atrItem = R.RUNE
    bus.Fire(bus, M.atrSource, A.Selling.Events.BagItemClicked, info)
    w.g.C_AuctionHouse.SendSellSearchQuery(w.to_lua({"itemID": R.RUNE, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}), w.lua.table(), True)
    w.settle(0.4)
    sc.eq(ns.ChartItem(), R.RUNE, "Auctionator selling: bag item switches the chart")
    price_box = sell.SaleItemFrame.Price
    sale = sell.SaleItemFrame
    pair = [None, None]
    for i in range(1, len(sale._children) + 1):
        c = sale._children[i]
        if c._otype == "Button" and c.kind in ("now", "best"):
            pair[0 if c.kind == "now" else 1] = c
    btn, bbtn = pair
    sc.check(btn is not None and bbtn is not None and not btn.IsVisible(btn) and not bbtn.IsVisible(bbtn),
             "Auctionator selling: no buttons until Auctionator has filled its own price (it would overwrite ours)")
    # Auctionator's search comes back: it fills its undercut, then our buttons appear
    M.atrResults(R.RUNE, 1234)
    sc.check(btn is not None and not btn.IsVisible(btn), "Auctionator selling: waits a moment after its results")
    w.settle(0.5)
    want = ref_buttons(ref, R.RUNE, "1D", lowest)
    buttons_ok(sc, pair, want, "Auctionator selling")
    if btn is not None and want[0] is not None:
        rb, rm, rah = w.rect(btn), w.rect(price_box.MoneyInput), w.rect(w.g.AuctionHouseFrame)
        sc.check(rb and rm and abs(rb[0] - rm[0]) < 1e-6 and rb[3] <= rm[1] + 1e-6,
                 "Auctionator selling: Sell now sits under the buyout money box (%s, %s)" % (rb, rm))
        sc.check(rb and rah and rb[2] <= rah[2] + 1e-6, "Auctionator selling: button inside the AH frame (%s)" % (rb,))
        if want[1] is not None:
            rbb = w.rect(bbtn)
            sc.check(rbb and abs(rbb[3] - rb[3]) < 1e-6 and rbb[0] >= rb[2] - 1e-6 and rbb[2] <= rah[2] + 1e-6,
                     "Auctionator selling: Best price beside it, inside the AH frame (%s)" % (rbb,))
        M.click(btn)
        sc.eq(price_box.GetAmount(price_box), want[0], "Auctionator selling: price set through PriceSelected")
        fired = [M.atrFired[i].name for i in range(1, len(M.atrFired) + 1)]
        sc.check("ForeverAH" in fired, "Auctionator selling: fired as source ForeverAH")
        if want[1] is not None:
            M.click(bbtn)
            sc.eq(price_box.GetAmount(price_box), want[1], "Auctionator selling: Best price set too")
        # Auctionator ignoring PriceSelected (no item yet): we notice and fill the box ourselves
        M.atrItem = None
        price_box.SetAmount(price_box, 777)
        M.click(btn)
        sc.eq(price_box.GetAmount(price_box), want[0], "Auctionator selling: read back, filled directly when PriceSelected was ignored")
        M.atrItem = R.RUNE
        # with the bid box shown the buttons move under it
        bidbox = sale.BidPrice
        bidbox.Show(bidbox)
        ns.AH_RefreshButtons()
        rb, rbid = w.rect(btn), w.rect(bidbox.MoneyInput)
        sc.check(rb and rbid and rb[3] <= rbid[1] + 1e-6, "Auctionator selling: buttons under the bid box when it is shown")
        bidbox.Hide(bidbox)
    # Auctionator's undercut scan while the Selling tab is hidden must not move the chart
    sell.Hide(sell)
    w.g.C_AuctionHouse.SendSellSearchQuery(w.to_lua({"itemID": R.LINEN, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}), w.lua.table(), True)
    w.settle(0.4)
    sc.eq(ns.ChartItem(), R.RUNE, "Auctionator background sell search ignored")
    w.close_ah_modern()
    sc.check(not C.frame.IsShown(C.frame), "Auctionator: chart hidden on close")
    sc.check(not any(b is not None and b._shown for b in pair), "Auctionator: buttons hidden on close")
    sc.clean(w)


def sc_horde(sc, ref, items):
    w = World(ref, items, faction="Horde")
    out = "\n".join(w.slash("/gl"))
    sc.check("may not match the auction house you use" in out, "Horde: status warns about the Alliance prices")
    w.slash("/gl chart " + N(R.RUNE))
    chart_check(sc, w, ref, R.RUNE, "1D", "Horde chart")
    w.M.hardcore = True
    out = "\n".join(w.slash("/gl"))
    sc.check("not Hardcore" in out, "Hardcore note")
    sc.clean(w)


def sc_live_prices(sc, ref, items):
    """Sell now and Best price follow the live listings the sell frame loaded, not only the hourly data."""
    w = World(ref, items)
    ns, M = w.ns, w.M
    w.put_bag(0, 1, R.RUNE, 20)
    w.put_bag(0, 2, R.LINEN, 20)
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    csf = ah.CommoditiesSellFrame
    pair = find_sell_buttons(csf)
    b, bb = pair
    snap = ref.items[R.RUNE]["cheapest"]
    # the cheap stock in the snapshot was bought: the live cheapest is 60% higher
    livep = int(snap * 1.6)
    w.set_commodity_results(R.RUNE, [(livep, 50, False), (livep + 40, 10, False)])
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1))
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.RUNE)
    w.settle(0.3)
    sc.eq(w.live(R.RUNE), livep, "live: cheapest listing read from the loaded results")
    want = ref_buttons(ref, R.RUNE, "1D", livep)
    sc.eq(want[0], livep - 1, "live: Sell now is 1 copper under the live listing")
    sc.check(want[1] is None or want[1] >= livep - 1 or ref_recommend(ref, R.RUNE, "1D", livep)["far"],
             "live: Best price does not undercut a listing that is no longer there (%s vs live %s)" % (want[1], livep))
    buttons_ok(sc, pair, want, "live: labels use the live price")
    sc.check("cheapest listing right now" in (b.tipBody or ""), "live: tooltip says the live price was used (%s)" % b.tipBody)
    M.click(b)
    sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want[0], "live: Sell now fills exactly the label's price")
    if want[1] is not None:
        M.click(bb)
        sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want[1], "live: Best price fills exactly its label's price")
    chart_check(sc, w, ref, R.RUNE, "1D", "live chart")
    # the cheapest listing is the player's own: match it, do not undercut yourself
    w.set_commodity_results(R.RUNE, [(livep, 5, True)])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.RUNE)
    w.settle(0.3)
    own = ref_buttons(ref, R.RUNE, "1D", livep, own=True)
    sc.eq(own[0], livep, "own listing: the model matches it")
    buttons_ok(sc, pair, own, "own listing: matched, not undercut")
    sc.check("That listing is yours" in (b.tipBody or ""), "own listing: tooltip says so (%s)" % b.tipBody)
    # no live listing: back to the hourly data, and the timeframe changes Best price only
    w.set_commodity_results(R.RUNE, [])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.RUNE)
    w.settle(0.3)
    base = ref_buttons(ref, R.RUNE, "1D")
    buttons_ok(sc, pair, base, "no live listing: back to the hourly data")
    other = next((tf for tf in TFS if ref_buttons(ref, R.RUNE, tf) != base), None)
    if R.SIM:
        sc.check(other is not None, "test data has a timeframe with a different Best price")
    if other:
        ns.SetChartTF(other)
        want_o = ref_buttons(ref, R.RUNE, other)
        sc.eq(want_o[0], base[0], "%s: Sell now does not depend on the timeframe" % other)
        buttons_ok(sc, pair, want_o, "%s: labels refreshed on timeframe change" % other)
        if want_o[1] is not None:
            M.click(bb)
            sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want_o[1], "%s: fill equals the label" % other)
        ns.SetChartTF("1D")
        buttons_ok(sc, pair, base, "back to 1D: labels follow")
    # timeframe change with a live listing: labels and fills follow the chart's timeframe together
    w.set_commodity_results(R.RUNE, [(livep, 50, False)])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.RUNE)
    w.settle(0.3)
    ns.SetChartTF("1Y")
    want_y = ref_buttons(ref, R.RUNE, "1Y", livep)
    buttons_ok(sc, pair, want_y, "1Y: labels refreshed on timeframe change")
    for k, v in enumerate(want_y):
        if v is not None:
            M.click(pair[k])
            sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), v, "1Y: fill equals the label (%d)" % k)
    ns.SetChartTF("1D")
    # stale data and no live results yet: no buttons; live results arrive: buttons
    w.M.commodityResults[R.LINEN] = None
    M.now = ref.updated + 9 * 3600
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 2))
    w.settle(0.3)
    sc.check(not b.IsVisible(b) and not bb.IsVisible(bb), "stale data with no live listing: no buttons")
    lp2 = ref.items[R.LINEN]["cheapest"] + 3
    w.set_commodity_results(R.LINEN, [(lp2, 100, False)])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.LINEN)
    w.settle(0.3)
    want2 = ref_buttons(ref, R.LINEN, "1D", lp2)
    sc.eq(want2[0], lp2 - 1, "stale data: Sell now from the live listing")
    buttons_ok(sc, pair, want2, "stale data: buttons come back once the live listing loads")
    M.now = w.now
    # live prices are only for the open AH
    w.close_ah_modern()
    sc.eq(w.live(R.RUNE), None, "live prices dropped when the AH closes")
    sc.eq(len(M.cah.posted), 0, "live: nothing posted")
    sc.clean(w)


def sc_other_house(sc, ref, items):
    """The neutral goblin auction house, the other faction's, and Hardcore are other markets."""
    w = World(ref, items)
    ns, M = w.ns, w.M
    w.put_bag(0, 1, R.RUNE, 20)
    loc = w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1)
    for label, npc, zone, other in (("Booty Bay goblin", None, "Booty Bay", True),
                                    ("Horde auctioneer", "Horde", "Orgrimmar", True),
                                    ("Alliance auctioneer", "Alliance", "Stormwind City", False),
                                    ("no faction outside a goblin town", None, "Ironforge", False)):
        M.npc = w.to_lua({"faction": npc} if npc else {})
        M.subzone = zone
        w.open_ah_modern()
        ah = w.g.AuctionHouseFrame
        sc.eq(bool(ns.AHState.otherHouse), other, "%s: other house detected" % label)
        r = w.lua.table()
        r.itemKey = w.to_lua({"itemID": R.RUNE, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0})
        r.minPrice = 1200
        ah.SelectBrowseResult(ah, r)
        chart_check(sc, w, ref, R.RUNE, "1D", label)
        ah.SetPostItem(ah, loc)
        w.settle(0.2)
        want = (None, None) if other else ref_buttons(ref, R.RUNE, "1D", w.live(R.RUNE))
        buttons_ok(sc, find_sell_buttons(ah.CommoditiesSellFrame), want, "%s: sell buttons" % label)
        w.close_ah_modern()
    M.npc, M.subzone = None, None
    M.hardcore = True
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    ah.SetPostItem(ah, loc)
    w.settle(0.2)
    buttons_ok(sc, find_sell_buttons(ah.CommoditiesSellFrame), (None, None), "Hardcore: sell buttons hidden")
    chart_check(sc, w, ref, R.RUNE, "1D", "Hardcore")
    w.close_ah_modern()
    M.hardcore = False
    sc.clean(w)


def sc_suffix_gear(sc, ref, items):
    """Old-style random-suffix links (a suffix id, no bonus ids) cannot be told
    apart: no sell buttons for them, and a note. A plain copy still gets them."""
    w = World(ref, items)
    ns, M = w.ns, w.M
    w.M["items"][R.ARCANE].commodity = False
    w.put_bag(0, 1, R.ARCANE, 1, suffix=1034)
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1))
    w.settle(0.2)
    pair = find_sell_buttons(ah.ItemSellFrame)
    buttons_ok(sc, pair, (None, None), "suffix item: no sell buttons")
    chart_check(sc, w, ref, R.ARCANE, "1D", "suffix item chart")
    sc.check("Price covers all versions of this item" in (ns.ChartPanel.notes._text or ""), "suffix item: chart note")
    # the same item without a suffix still gets the buttons (the note stays: it is about the item ID)
    w.put_bag(0, 2, R.ARCANE, 1)
    w.set_commodity_results(R.ARCANE, [])
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 2))
    w.settle(0.2)
    versioned = R.ARCANE in ref.variants   # then a plain copy is a version the data has no prices for
    plain = (None, None) if versioned else ref_buttons(ref, R.ARCANE, "1D", is_commodity=False)
    buttons_ok(sc, pair, plain, "plain copy")
    # live item results feed the item sell buttons too (whole silver)
    t = w.lua.table()
    t[1] = w.to_lua({"buyout": 2512345, "quantity": 1})
    w.M.itemResults[R.ARCANE] = t
    M.fire("ITEM_SEARCH_RESULTS_UPDATED", w.to_lua({"itemID": R.ARCANE, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}))
    w.settle(0.3)
    if versioned:
        sc.eq(w.live(R.ARCANE), None, "item results: no all-versions live price for an item whose versions differ")
        want = (price_for_frame(2512344, False), None)
    else:
        sc.eq(w.live(R.ARCANE), 2512345, "item results: live cheapest buyout")
        want = ref_buttons(ref, R.ARCANE, "1D", 2512345, is_commodity=False)
    buttons_ok(sc, pair, want, "item sell buttons use the live buyout, rounded to silver")
    w.M["items"][R.ARCANE].commodity = True
    w.close_ah_modern()
    sc.clean(w)


def sc_chart_drag_docked(sc, ref, items):
    """The docked chart can be dragged; it stays attached to the AH at that offset."""
    w = World(ref, items, screen=(768 * 16 / 9, 768, 0.64))
    ns, M = w.ns, w.M
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    C = ns.ChartPanel
    F = C.frame
    sc.eq(C.placement, "below", "drag: docked below")
    left0, top0 = F.GetLeft(F), F.GetTop(F)
    M.drag(F, "TOPLEFT", "BOTTOMLEFT", left0 + 40, top0 - 25)
    sc.check(C.docked, "drag: still docked after a drag")
    p = F._points[1]
    sc.check(w.same(p[2], ah), "drag: re-anchored to the AH")
    sc.check(abs(F.GetLeft(F) - (left0 + 40)) < 1e-6 and abs(F.GetTop(F) - (top0 - 25)) < 1e-6,
             "drag: stays where it was dropped (%s, %s)" % (F.GetLeft(F), F.GetTop(F)))
    off = ns.settings.chartDock.below
    sc.eq((off[1], off[2]), (40, -25), "drag: offset saved")
    w.close_ah_modern()
    w.open_ah_modern()
    sc.check(abs(F.GetLeft(F) - (left0 + 40)) < 1e-6, "drag: offset kept when the AH opens again")
    out = "\n".join(w.slash("/gl chart reset"))
    sc.check("normal place" in out and ns.settings.chartDock is None, "drag: /gl chart reset clears it")
    sc.check(abs(F.GetLeft(F) - left0) < 1e-6 and abs(F.GetTop(F) - top0) < 1e-6, "drag: back under the AH")
    w.close_ah_modern()
    sc.clean(w)


def sc_ah_no_copper(sc, ref, items):
    """If the client says copper prices are not supported, the Use button rounds to whole silver."""
    w = World(ref, items, copper_values=False)
    ns, M = w.ns, w.M
    w.put_bag(0, 1, R.LINEN, 20)
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    lowest = ref.items[R.LINEN]["cheapest"]
    w.set_commodity_results(R.LINEN, [(lowest, 50, False)])
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1))
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.LINEN)
    w.settle(0.2)
    csf = ah.CommoditiesSellFrame
    pair = find_sell_buttons(csf)
    want = ref_buttons(ref, R.LINEN, "1D", lowest, copper_ok=False)
    sc.check(want[0] is not None, "no copper: Sell now with a live listing")
    buttons_ok(sc, pair, want, "no copper: buttons in whole silver")
    for k, v in enumerate(want):
        if v is not None:
            M.click(pair[k])
            sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), v, "no copper: price box gets whole silver (%d)" % k)
            sc.eq(v % 100, 0, "no copper: whole silver (%d)" % k)
    w.close_ah_modern()
    sc.clean(w)


# ---------------------------------------------------------------------------
# Auction house sidebar (Sidebar.lua): flip ideas and the watchlist
# ---------------------------------------------------------------------------
SIDEBAR_WATCH = [14047, 4338, 4306, 2592, 2589, 2772, 3858, 10620, 2447, 765, 4304, 10940]
CODES = re.compile(r"\|c[0-9a-fA-F]{8}|\|r")


def strip_codes(s):
    return CODES.sub("", s or "")


def short_money(c):
    c = math.floor(c + 0.5)
    neg = c < 0
    c = abs(c)
    g, s, k = c // 10000, (c // 100) % 100, c % 100
    if g >= 100:
        out = "%dg" % g
    elif g > 0:
        out = ("%dg %ds" % (g, s)) if s > 0 else "%dg" % g
    elif s > 0:
        out = ("%ds %dc" % (s, k)) if k > 0 else "%ds" % s
    else:
        out = "%dc" % k
    return ("-" if neg else "") + out


def count_text(n):
    n = math.floor(n + 0.5)
    if n >= 10000:
        return "%dk" % (n // 1000)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)


FLIP_MESSAGES = {
    "history": "Collecting price history. Flip ideas show up after a few hourly price updates.",
    "nosales": "Collecting sales history. Flip ideas show up after a few hourly price updates.",
    "none": "No flips likely to sell within a day right now. Check back after the next update.",
    "gold": "Not enough gold on this character for the current flip ideas.",
}
FLIP_SLASH = {
    "history": "Collecting price history. Flip ideas show up after a few hourly price updates.",
    "nosales": "still collecting sales history: flip ideas show up after a few hourly price updates.",
    "none": "no good flips right now.",
    "gold": "not enough gold on this character for the current flip ideas",
}
FLIP_SLASH_HEAD = "flip ideas, most profit within a day first (after the 5% auction cut):"
FLIP_SLASH_GOLD_HEAD = "flip ideas sized to your gold (%s), most profit within a day first, after the 5%% auction cut:"
VOL_CAP = 200               # Sidebar.lua: a day's flip is at most this many units
MIN_SOLD_FAST = 65          # a fast seller, just over Sidebar.lua's MIN_SOLD (60 a day)


def fit_flip(f, gold):
    """Sidebar.lua FitFlip: what the gold buys of a collector flip, or None."""
    units, cost, profit = f["units"], f["cost"], f["profit"]
    if gold >= cost:
        return {"units": units, "cost": cost, "profit": profit, "limited": False, "gold": gold, "whole": units}
    can = min(gold * units // cost, units - 1)       # floor(gold / (cost / units))
    if can < 1:
        return None
    return {"units": can, "cost": -(-(can * cost) // units), "profit": profit * can // units, "limited": True,
            "gold": gold, "whole": units}


def fit_history(x, gold):
    """Sidebar.lua FitHistory: a day's flip of a price-history idea, or what the gold buys of it, or None."""
    whole = max(1, int(math.floor(min(x["sold"] or 0, VOL_CAP))))
    can = gold // x["buy"]
    if can < 1:
        return None
    n = min(can, whole)
    return {"units": n, "cost": n * x["buy"], "profit": n * x["profit"], "limited": can < whole, "gold": gold,
            "whole": whole}


def deal_under(x):
    """Sidebar.lua DealUnder: whole percent the buy-up-to price is under the usual price (rounded down), or None."""
    usual, buy = x.get("usual") or 0, x.get("buyMax") or x.get("buy") or 0
    if usual <= 0 or buy <= 0 or buy >= usual:
        return None
    pct = 100 * (usual - buy) // usual
    return pct if pct >= 1 else None


def price_doubt(ref, iid):
    """Recommend.lua priceDoubt: only 1 or 2 listings, or a few units far above what a vendor pays."""
    it = ref.items.get(iid)
    if not it:
        return False
    auctions = it["auctions"]
    thin = auctions is not None and 1 <= auctions < 3
    far = it["vendor"] > 0 and it["market"] > 200 * it["vendor"] and it["listed"] < 10
    return thin or far


# ---------------------------------------------------------------------------
# What a collector flip can make within about a day (Sidebar.lua DayStats,
# ExpectedSold and DayValue): the reference model, ported from the ranking
# spec's own Python model (written apart from the Lua), not from the Lua.
# ---------------------------------------------------------------------------
DAY_SHARE = 0.5             # one seller can expect half of a day's (damped) sales
DAY_HOUR_CAP = 3            # no hour counts for more than 3x a typical hour (and at least 1)
DAY_TRIM = 0.2              # a typical hour: the measured hours without the busiest 20%
DAY_SALE_HOURS = 2          # sales must show in at least 2 different hours of the last day
DAY_PRICE_CAP = 1.25        # the relist counts at most 25% over the price it sold at
DAY_RECENT = 6              # gap flips: the median of the last 6 market prices is its price now
DAY_SPIKE = 1.5             # gap flips: never counted over 1.5x the usual price
DAY_DEPOSIT = 0.30          # a unit left unsold loses a deposit of 30% of the vendor price
DAY_DEPOSIT_NOTE = 1000     # the hover says so only when the unsold ones risk 10s or more
DAY_MIN_PROFIT = 2000       # less than 20s expected in a day: not a flip idea
DAY_EXACT = 60              # expected sales are summed exactly up to this many units (and rate)
SUFFIX_TEXT = ('Versions: this gear comes in versions (like "of the Bear") that sell for different prices. '
               "Check the version before you buy.")


def day_stats(ref, iid):
    """The last day (the 1D window, at most 24 hourly slots) of an item's sales,
    or None while sales are not measured (salesHours 0) or the item is not in
    the data: S (units sold, no hour over DAY_HOUR_CAP x a typical hour, never
    scaled up and never over sold24), hours (slots with a sale), soldAt (the
    weighted lower median of the prices those sales were at: each slot's sales
    at the cheapest listing of the slot before, else its own cheapest, else its
    market price) and recent (the median of the last DAY_RECENT market prices).
    Sums run left to right like the game's (Python's sum() compensates floats)."""
    sh = rec_model.sales_hours_of(ref.salesHours)
    it = ref.items.get(iid)
    if it is None or sh <= 0:
        return None
    win = ref.window(iid, "1D")
    cheap, market, sold = win["cheap"], win["market"], win["sold"]
    xs = sorted(sold, reverse=True)
    t = int(sh * DAY_TRIM)
    cap = max(1.0, DAY_HOUR_CAP * (sum(xs) - sum(xs[:t])) / max(1, sh - t))
    w = [min(x, cap) for x in sold]
    s = 0.0
    for v in w:
        s += v
    pairs, wsum = [], 0.0
    for i, x in enumerate(sold):
        if x <= 0:
            continue
        p = (cheap[i - 1] if i > 0 else None) or cheap[i] or market[i]
        if not p:
            continue
        pairs.append((p, w[i]))
        wsum += w[i]
    pairs.sort()
    sold_at, acc = None, 0.0
    for p, v in pairs:
        acc += v
        if acc * 2 >= wsum:
            sold_at = p
            break
    rec = sorted([p for p in market if p][-DAY_RECENT:])
    k = len(rec)
    recent = None
    if k:
        recent = rec[k // 2] if k % 2 else (rec[k // 2 - 1] + rec[k // 2]) / 2
    return {"S": min(s, float(it["sold24"])), "hours": sum(1 for x in sold if x > 0), "soldAt": sold_at,
            "recent": recent}


def expected_sold(rate, n, ahead):
    """Sidebar.lua ExpectedSold: your units expected to sell in a day when buyers
    take `rate` a day from the cheapest up and `ahead` cheaper units sell first:
    the sum over j = ahead .. ahead + n - 1 of P(X > j), X Poisson(rate). Over
    DAY_EXACT units (or rate): the rate less the units ahead, between 0 and n."""
    if ahead + n > DAY_EXACT or rate > DAY_EXACT:
        return max(0.0, min(float(n), rate - ahead))
    p = math.exp(-rate)
    cdf, tot = p, 0.0
    for j in range(ahead + n):
        if j >= ahead:
            tot += 1.0 - cdf
        p = p * rate / (j + 1)
        cdf += p
    return tot


def whole_fit(f):
    """A collector flip bought whole (no gold sizing), as DayValue takes it."""
    return {"units": f["units"], "cost": f["cost"], "profit": f["profit"]}


def day_value(ref, f, fit):
    """Sidebar.lua DayValue for a collector flip bought as `fit` (units, cost,
    profit): (r, None) or (None, why) with why "nosales" (no measured sales or
    no price they sold at), "onehour" (sales in fewer than DAY_SALE_HOURS
    slots), "loses" (the dearest unit bought loses at countAt) or "small"
    (under DAY_MIN_PROFIT in a day). r: countAt (the relist, at most
    DAY_PRICE_CAP x the price it sold at; a gap flip also at most that x its
    price now and DAY_SPIKE x its usual price), refPrice (that price), rate
    (DAY_SHARE x S), day (your units expected to sell in a day, after the
    cheaper units the gold could not buy), deposit, dayValue, dayProfit and
    stats. A vendor flip sells at once: its profit."""
    n, whole = fit["units"], f["units"]
    if f["kind"] == "vendor":
        return {"countAt": 0, "refPrice": None, "day": n, "dayValue": fit["profit"], "dayProfit": fit["profit"],
                "rate": None, "deposit": None, "stats": None}, None
    st = day_stats(ref, f["id"])
    if not st or st["S"] <= 0 or not st["soldAt"]:
        return None, "nosales"
    if st["hours"] < DAY_SALE_HOURS:
        return None, "onehour"
    price = st["soldAt"]
    if f["kind"] == "gap" and st["recent"] and st["recent"] < price:
        price = st["recent"]
    at = min(f["relist"], int(math.floor(DAY_PRICE_CAP * price)))
    if f["kind"] == "gap" and f["usual"] > 0:
        at = min(at, int(math.floor(DAY_SPIKE * f["usual"])))
    net = at * 95 // 100
    if net <= f["buyMax"]:
        return None, "loses"
    rate = DAY_SHARE * st["S"]
    day = expected_sold(rate, n, whole - n)
    dep = DAY_DEPOSIT * ref.items[f["id"]]["vendor"]
    v = day * (net - fit["cost"] / n) - (n - day) * dep
    if v < DAY_MIN_PROFIT:
        return None, "small"
    return {"countAt": at, "refPrice": price, "day": day, "dayValue": v, "dayProfit": int(math.floor(v)),
            "rate": rate, "deposit": dep, "stats": st}, None


DEAL_EVERY = 5              # Sidebar.lua: one place in every 5 flip ideas is kept for a solid deal


def keep_deal_places(ranked, limit):
    """The best `limit` of the ranked collector flips, with one place in every
    DEAL_EVERY kept for solid deals: when fewer of them than that are among the
    best `limit`, the best ones after them replace the lowest-ranked other
    flips, and the list stays in rank order."""
    want = min(sum(1 for x in ranked if x["solid"]), limit // DEAL_EVERY)
    top = ranked[:limit]
    short = want - sum(1 for x in top if x["solid"])
    if short <= 0:
        return top
    extra = [x for x in ranked[limit:] if x["solid"]][:short]
    drop = set([i for i, x in enumerate(top) if not x["solid"]][-short:])
    kept = [x for i, x in enumerate(top) if i not in drop] + extra
    rank = dict((id(x), i) for i, x in enumerate(ranked))
    return sorted(kept, key=lambda x: rank[id(x)])


def ref_collector_flips(ref, limit=5, hidden=(), gold=MOCK_GOLD, dropped=None):
    """The collector's part of ns.FlipIdeas: (ideas, seen, gone, poor). With gold
    (copper; None = the game does not say: no sizing) each flip gets a fit and
    what the gold cannot buy is left out; poor when one of those would show with
    enough gold (day_value keeps it bought whole). A flip stays only when
    day_value keeps it (on what the gold buys); a dropped one (its id in gone)
    is not shown and its item gets no history idea, but another flip of the
    same item is still judged (dropped, when given a dict, gets id -> why).
    They rank by dayValue, then the share of the units bought that sells in a
    day, then the collector's order, and the best `limit` are kept with one
    place in every 5 for solid deals (keep_deal_places): a deal without the
    collector's "unsure", counted at its own relist price."""
    ranked, seen, gone, poor = [], set(), set(), False
    for f in ref.flips:
        if f["id"] in hidden or f["id"] in seen:
            continue
        fit = fit_flip(f, gold) if gold is not None else None
        if gold is not None and fit is None:
            poor = poor or day_value(ref, f, whole_fit(f))[0] is not None
            continue
        r, why = day_value(ref, f, fit or whole_fit(f))
        if r is None:
            gone.add(f["id"])
            if dropped is not None:
                dropped[f["id"]] = why
            continue
        seen.add(f["id"])
        x = dict(f, data=True, buy=f["buyMax"], risk=list(f["risk"]), fit=fit, order=len(ranked), **r)
        x["solid"] = f["kind"] == "deal" and "unsure" not in f["risk"] and r["countAt"] == f["relist"]
        ranked.append(x)
    ranked.sort(key=lambda x: (-x["dayValue"], -x["day"] / (x["fit"] or x)["units"], x["order"]))
    return keep_deal_places(ranked, limit), seen, gone, poor


def ref_flips_why(ref, limit=5, hidden=(), gold=MOCK_GOLD, dropped=None):
    """Python model of ns.FlipIdeas: the collector's flips from the data file
    first (ref_collector_flips), then the history-based ideas, each item once,
    hidden ones left out, and none for an item whose collector flip was
    dropped. Returns (ideas, why) where why says why there are none."""
    if not ref.items or not ref.updated:
        return [], "nodata"
    out, seen, gone, poor = ref_collector_flips(ref, limit, hidden, gold, dropped)
    if len(out) >= limit:
        return out, None
    # every history idea: the places left take the best ones not shown or dropped
    hist, why = ref_history_flips_why(ref, None, hidden, gold)
    for x in hist:
        if len(out) >= limit:
            break
        if x["id"] in seen or x["id"] in gone:
            continue
        seen.add(x["id"])
        out.append(x)
    if out:
        return out, None
    if poor:
        return out, "gold"
    return out, why


def ref_history_flips_why(ref, limit=5, hidden=(), gold=MOCK_GOLD):
    """Python model of the history-based ideas (Sidebar.lua HistoryIdeas): rank
    by money traded, full advice on the top 60, filter, rank by a day's profit.
    An item whose versions differ is left out (its prices mix them). With gold,
    each idea gets a fit (a day's flip, or what the gold buys of it), the ones
    the gold cannot buy are left out before the cut to `limit`, and they rank
    by the profit on what the gold buys, best first (the contract's item 4),
    however fast they sell. Without gold, items selling 60+ a day come first,
    each group by a day's profit. "thin": sells under 60 a day. limit None: all."""
    if rec_model.sales_hours_of(ref.salesHours) < 3:
        return [], "history"
    cand = []
    for iid, it in ref.items.items():
        if iid in ref.variants:
            continue
        sold, _ = rec_model.sold_per_day(it["sold24"], ref.salesHours)
        thin = it["auctions"] is not None and it["auctions"] < 3
        vfar = it["vendor"] > 0 and it["market"] > 200 * it["vendor"] and it["listed"] < 10
        if (sold is not None and it["cheapest"] > 0 and it["market"] > 0 and sold >= 15 and it["listed"] >= 2
                and not thin and not vfar):
            cand.append((-(it["market"] * min(sold, 200)), iid))
    cand.sort()
    if not cand:
        return [], "nosales"
    ideas = []
    tried = no_history = poor = 0
    for _, iid in cand[:60]:
        if iid in hidden:
            continue
        tried += 1
        r = ref_recommend(ref, iid, "1W")
        if not r["history"]:
            no_history += 1
        if (r["history"] and r["advice"] and r["margin"] > 0 and r["buy"] > 0 and r["marginPct"] >= 0.03
                and not r["far"] and not r["vendorBetter"] and r["verdict"] not in ("Sell", "Strong Sell")):
            sold = r["soldPerDay"] or 0
            idea = {"id": iid, "buy": r["buy"], "sell": r["sell"], "profit": r["margin"],
                    "perDay": r["margin"] * min(sold, 200), "sold": sold, "early": r["soldEarly"], "fit": None,
                    "thin": sold < 60}
            if gold is not None:
                idea["fit"] = fit_history(idea, gold)
                if idea["fit"] is None:
                    poor += 1
                    continue
            ideas.append(idea)
    if gold is not None:
        out = sorted(ideas, key=lambda x: (-x["fit"]["profit"], x["id"]))[:limit]
    else:
        out = sorted(ideas, key=lambda x: (x["thin"], -x["perDay"], x["id"]))[:limit]
    if out:
        return out, None
    if poor:
        return out, "gold"
    if tried > 0 and no_history == tried:
        return out, "history"
    return out, "none"


def ref_flips(ref, limit=5, hidden=(), gold=MOCK_GOLD):
    return ref_flips_why(ref, limit, hidden, gold)[0]


def items_of(w):
    return w.items_json


def flip_units_text(x):
    """How many a collector flip row buys: "13", or "2 of 13" when the gold covers only some."""
    fit = x.get("fit")
    if fit and fit["limited"]:
        return "%s of %s" % (count_text(fit["units"]), count_text(x["units"]))
    return count_text(x["units"])


def flip_note(x):
    """The small grey note on a collector flip row: how many one seller can expect
    to sell in a day ("~N/day", "<1/day"), nothing for a vendor flip."""
    rate = x.get("rate")
    if rate is None:
        return ""
    return ("~%s/day" % count_text(rate)) if rate >= 1 else "<1/day"


def flip_profit(x):
    """The profit a flip row shows: a collector flip's profit within a day (a vendor
    flip's whole profit on what the gold buys), else a history idea's on what the
    gold buys (or, not sized, its profit each)."""
    if x.get("data"):
        return x["dayProfit"]
    return x["fit"]["profit"] if x.get("fit") else x["profit"]


def flip_line_text(x, name):
    """Sidebar.lua FlipLine: one /gl flips chat line (without the colour codes)."""
    fit = x.get("fit")
    if x.get("data"):
        head = "%s: buy up to %s (%s), " % (name, money_text(x["buyMax"]), flip_units_text(x))
        if x["kind"] == "vendor":
            return head + "sell to vendor, +" + money_text(fit["profit"] if fit else x["profit"])
        sells = ("sells ~%s/day" % count_text(x["rate"])) if x["rate"] >= 1 else "sells under 1 a day"
        return head + "relist %s, +%s in a day (%s)" % (money_text(x["countAt"]), money_text(x["dayProfit"]), sells)
    if fit:
        return "%s: buy %s at %s, sell %s, +%s (+%s each), ~%s/day" % (
            name, count_text(fit["units"]), money_text(x["buy"]), money_text(x["sell"]), money_text(fit["profit"]),
            money_text(x["profit"]), count_text(x["sold"]))
    return "%s: buy %s, sell %s, +%s each, ~%s/day" % (name, money_text(x["buy"]), money_text(x["sell"]),
                                                      money_text(x["profit"]), count_text(x["sold"]))


def gold_line(x):
    """Sidebar.lua GoldLine as a flattened tooltip line ("left | right"), or None (not
    sized). A relisted collector flip says its profit in its "Within a day" lines,
    so only vendor flips and history ideas add ", making +X"."""
    fit = x.get("fit")
    if not fit:
        return None
    if fit["limited"]:
        s = "With your gold (%s): buy %s of %s, costing %s" % (
            money_text(fit["gold"]), count_text(fit["units"]), count_text(fit["whole"]), money_text(fit["cost"]))
        if not (x.get("data") and x["kind"] != "vendor"):
            s += ", making +%s" % money_text(fit["profit"])
    else:
        s = "Your gold covers all of it."
    return s + " | "


def slash_head(gold):
    """The /gl flips header: sized to your gold when the game says."""
    return (FLIP_SLASH_GOLD_HEAD % money_text(gold)) if gold is not None else FLIP_SLASH_HEAD


def slash_flips_ok(sc, out, ideas, ref, items, gold, label):
    """/gl flips: the header (sized to your gold when the game says), then one exact line per idea."""
    head = slash_head(gold)
    sc.eq(len(out), len(ideas) + 1, "%s: /gl flips prints a header and a line per idea" % label)
    if out:
        sc.check(strip_codes(out[0]).endswith(head), "%s: /gl flips header (%s)" % (label, strip_codes(out[0])))
    for i, (line, x) in enumerate(zip(out[1:], ideas), start=1):
        want = "  %d. %s" % (i, flip_line_text(x, item_name(ref, items, x["id"])))
        sc.check(strip_codes(line).endswith(want), "%s: /gl flips line %d: got %r, want %r" % (label, i, strip_codes(line), want))


def flip_row_ok(sc, r, x, ref, items, lab):
    """One flip row shows the idea: name, profit (a collector flip's within a day, on
    what your gold buys), what to buy and what to do with it."""
    sc.eq(r.itemID, x["id"], lab + ": item")
    sc.eq(strip_codes(r.right._text), "+" + short_money(flip_profit(x)), lab + ": profit")
    sc.eq(r.name._text, item_name(ref, items, x["id"]), lab + ": name")
    if x.get("data"):
        sc.eq(strip_codes(r.sub._text), "Buy up to %s (%s)" % (short_money(x["buyMax"]), flip_units_text(x)),
              lab + ": buy up to (units)")
        sc.eq(strip_codes(r.sub2._text), "Sell to vendor" if x["kind"] == "vendor" else "Relist " + short_money(x["countAt"]),
              lab + ": relist (at the price it is counted at) or vendor")
        sc.eq(r.right2._text, flip_note(x), lab + ": sales per day note")
    else:
        sc.eq(strip_codes(r.sub._text), ("Buy %s at %s" % (count_text(x["fit"]["units"]), short_money(x["buy"])))
              if x.get("fit") else "Buy at %s" % short_money(x["buy"]), lab + ": buy")
        sc.eq(strip_codes(r.sub2._text), "Sell at %s" % short_money(x["sell"]), lab + ": sell")
        sc.eq(r.right2._text, "~%s/day" % count_text(x["sold"]), lab + ": sold per day")
    base = ref.base_of.get(x["id"], x["id"])
    sc.eq(r.icon._texture, 130000 + base if str(base) in items else r.icon._texture, lab + ": icon of the game's item")


def lua_num_text(v):
    """Lua 5.1 tostring() of a number (%.14g)."""
    return "%.14g" % v


def expected_change(ref, iid):
    win = ref.window(iid, "1D")
    vals = [v for v in win["market"] if v]
    if win["n"] < 2 or not vals:
        return ""
    first, last = vals[0], vals[-1]
    ch = (last - first) / float(first)
    pct = math.floor(abs(ch) * 1000 + 0.5) / 10.0
    if pct < 0.05:
        return "0%"
    return ("+" if ch > 0 else "-") + lua_num_text(pct) + "%"


def shown_rows(SB, key):
    rows = SB[key]
    out = []
    for i in range(1, len(rows) + 1):
        r = rows[i]
        if r is not None and r._shown:
            out.append(r)
    return out


def watch_ids(w):
    wl = w.ns.GetWatchlist()
    return [wl[i] for i in range(1, len(wl) + 1)]


def install_menu_mock(w):
    w.lua.execute(r'''
      MenuUtil = { CreateContextMenu = function(owner, gen)
        local root = { items = {} }
        function root:CreateTitle(t) self.title = t end
        function root:CreateButton(t, fn) self.items[#self.items + 1] = { text = t, fn = fn } end
        gen(owner, root)
        MOCK.lastMenu = root
        return root
      end }
    ''')


def menu_texts(w):
    m = w.M.lastMenu
    return [m["items"][i].text for i in range(1, len(m["items"]) + 1)] if m is not None else []


def menu_pick(w, text):
    m = w.M.lastMenu
    for i in range(1, len(m["items"]) + 1):
        if m["items"][i].text == text:
            m["items"][i].fn()
            return True
    return False


def item_name(ref, items, iid):
    """The name the addon shows: the data's, else the game's (items.json in the mock), else Item <id>."""
    if iid in ref.items and known_name(ref.items[iid]["name"]):
        return ref.items[iid]["name"]
    it = items.get(str(iid))
    return it["name"] if it and known_name(it.get("name")) else "Item %d" % iid


def sc_sidebar(sc, ref, items):
    w = World(ref, items)
    ns, M = w.ns, w.M
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    SB = ns.SidebarPanel
    F = w.g.ForeverAHSidebar
    sc.check(F is not None and F.IsVisible(F), "sidebar opens with the auction house")
    if F is None:
        return
    sc.check(w.same(F._parent, ah), "sidebar is a child of the game's AH window (no window of its own)")
    ra, rs = w.rect(ah), w.rect(F)
    sc.check(rs[0] > ra[2] and rs[0] - ra[2] < 10, "sidebar sits just right of the AH (%s vs %s)" % (rs, ra))
    sc.check(abs(rs[3] - ra[3]) < 1e-6 and abs((rs[3] - rs[1]) - (ra[3] - ra[1])) < 1e-6,
             "sidebar is top-aligned and as tall as the AH")
    sc.check(rs[2] <= M.screenW + 1e-6, "sidebar on screen")
    C = ns.ChartPanel
    if C is not None and C.frame._shown and C.placement == "right":
        rc = w.rect(C.frame)
        sc.check(rc[0] >= rs[2] - 1e-6, "chart docked right goes past the sidebar, not over it (%s vs %s)" % (rc, rs))
        sc.check(rc[2] <= M.screenW + 1e-6, "chart beside the sidebar stays on screen")
    sc.check(SB.lastError is None, "sidebar: no swallowed error (%s)" % SB.lastError)

    # Header
    sc.eq(strip_codes(SB.title._text), "Goblin Ledger", "sidebar header shows the addon's name")
    sc.check("ago" in strip_codes(SB.age._text) or "just now" in strip_codes(SB.age._text),
             "header shows the data age (%r)" % SB.age._text)

    # Flip ideas match the Python model (sized to the mock's 123g 45s 67c)
    want, why = ref_flips_why(ref, gold=w.gold)
    got = shown_rows(SB, "flipRows")
    sc.eq(len(got), len(want), "flip ideas shown")
    for r, x in zip(got, want):
        flip_row_ok(sc, r, x, ref, items, "flip %s" % x["id"])
    sc.eq([r.itemID for r in got], [x["id"] for x in want], "flip ideas in order (collector flips first)")
    sc.check(len(set(r.itemID for r in got)) == len(got), "each item once")
    sc.eq(SB.flipGold._text, "for your 123g", "FLIP IDEAS says the gold the ideas are sized to")
    sc.check(all(x["fit"] is not None for x in want), "every idea is sized to the gold")
    if not want:
        sc.check(SB.flipMsg._shown, "no ideas: says why")
        sc.eq(SB.flipMsg._text, FLIP_MESSAGES[why], "no ideas: the message for %s" % why)
    else:
        sc.check(not SB.flipMsg._shown, "ideas shown: no message")
    if R.SIM:
        sc.check(len(want) > 0, "the simulated data gives at least one flip idea (%d)" % len(want))

    # Starter watchlist
    sc.eq(watch_ids(w), SIDEBAR_WATCH, "starter watchlist is the website's default favorites")
    rows = shown_rows(SB, "watchRows")
    sc.check(len(rows) >= 8, "at least 8 watchlist rows fit (%d)" % len(rows))
    for i, r in enumerate(rows):
        iid = SIDEBAR_WATCH[i]
        lab = "watch row %d (%d)" % (i + 1, iid)
        sc.eq(r.itemID, iid, lab + ": item")
        sc.eq(r.name._text, item_name(ref, items, iid), lab + ": name")
        it = ref.items.get(iid)
        if it:
            p = it["market"] if it["market"] > 0 else it["cheapest"]
            sc.eq(strip_codes(r.right._text), short_money(p) if p > 0 else "no data", lab + ": price")
            sc.eq(strip_codes(r.change._text), expected_change(ref, iid), lab + ": 24h change")
            rec = ref_recommend(ref, iid, "1D")
            v = rec["verdict"] if rec["history"] and rec["advice"] else None
            sc.eq(bool(r.accent._shown), v in ("Strong Buy", "Buy", "Sell", "Strong Sell"), lab + ": edge colour shown")
        else:
            sc.eq(strip_codes(r.right._text), "no data", lab + ": no data")
    more = len(SIDEBAR_WATCH) - len(rows)
    if more > 0:
        sc.eq(SB.more._text, "%d more (scroll)" % more, "watchlist says how many more")
        M.fireScript(SB.watchBox, "OnMouseWheel", -1)
        rows2 = shown_rows(SB, "watchRows")
        sc.eq(rows2[0].itemID, SIDEBAR_WATCH[1], "scrolling down moves the list one row")
        sc.check("above" in (SB.more._text or ""), "after scrolling it says rows are above")
        M.fireScript(SB.watchBox, "OnMouseWheel", 5)
        sc.eq(shown_rows(SB, "watchRows")[0].itemID, SIDEBAR_WATCH[0], "scrolling up stops at the top")

    # Hover: the game's item tooltip with the Goblin Ledger lines, plus the flip numbers
    tt = w.g.GameTooltip
    if got:
        M.enter(got[0])
        lines = w.all_lines(tt)
        flat = [strip_codes(a or "") + " | " + strip_codes(b or "") for a, b in lines]
        sc.check(any("Goblin Ledger" in f for f in flat), "flip hover: Goblin Ledger price lines are there")
        x0 = want[0]
        if x0.get("data"):
            pl, pv = (("Profit over the cost", x0["profit"]) if x0["kind"] == "vendor"
                      else ("Profit in a day, after the 5% cut", x0["dayProfit"]))
        else:
            pl, pv = "Profit each, after the 5% cut", x0["profit"]
        sc.check(any(f.startswith(pl) and ("+" + ns.MoneyText(pv)) in f
                     for f in flat), "flip hover: profit line (%s)" % pl)
        sc.check(any("Click: open it in the auction house" in f for f in flat), "flip hover: says what a click does")
        sc.check(gold_line(want[0]) in flat, "flip hover: what your gold buys of it (%s)" % gold_line(want[0]))
        M.leave(got[0])
        # a price-history idea says what a day's flip is, and what your gold buys of it
        hx = next((k for k, x in enumerate(want) if not x.get("data")), None)
        if hx is not None:
            x = want[hx]
            M.enter(got[hx])
            flat = [strip_codes(a or "") + " | " + strip_codes(b or "") for a, b in w.all_lines(tt)]
            whole = x["fit"]["whole"]
            sc.check("For a day: buy %s, costing | %s" % (count_text(whole), ns.MoneyText(whole * x["buy"])) in flat,
                     "history idea hover: a day's flip and its cost (%s)" % flat)
            sc.check("Profit on those, after the cut | +%s" % ns.MoneyText(whole * x["profit"]) in flat,
                     "history idea hover: a day's flip profit")
            sc.check(gold_line(x) in flat, "history idea hover: what your gold buys (%s)" % gold_line(x))
            M.leave(got[hx])

    # Click a watch row: the game's own search runs, then the exact item opens
    target = next((i for i in SIDEBAR_WATCH if i in ref.items and str(i) in items), None)
    sc.check(target is not None, "a starter item with data to click")
    if target is not None:
        row = next(r for r in shown_rows(SB, "watchRows") if r.itemID == target)
        nbefore = len(M.cah.browse)
        M.click(row)
        w.settle(0.1)
        name = item_name(ref, items, target)
        sc.eq(ah.SearchBar.SearchBox.GetText(ah.SearchBar.SearchBox), name, "row click: name in the AH search box")
        sc.eq(len(M.cah.browse), nbefore + 1, "row click: one search sent through the AH's search bar")
        sc.eq(M.cah.browse[len(M.cah.browse)], name, "row click: searched the item name")
        w.set_browse([(R.NO_GEAR, 500, 1), (target, ref.items[target]["cheapest"], 400)])
        M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
        w.settle(0.4)
        sc.check(ah.displayMode is not None and ah.CommoditiesBuyFrame._shown,
                 "row click: the item's listings open in the game's AH")
        sc.eq(ah.CommoditiesBuyFrame.itemID, target, "row click: the exact item opened")
        sc.eq(ns.ChartItem(), target, "row click: chart follows")
        # A second results event does not reopen anything
        M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
        w.settle(0.1)

        # Right-click without context menus toggles the watchlist
        row = next(r for r in shown_rows(SB, "watchRows") if r.itemID == target)
        M.click(row, "RightButton")
        sc.check(target not in watch_ids(w), "right-click (no menus) removes it")
        mark = w.chat_mark()
        M.click(SB.addButton)
        sc.eq(watch_ids(w)[0], target, "+ Watch adds the item you are looking at, on top")
        sc.check(any("watching " + name in c for c in w.chat(mark)), "+ Watch says so in chat")
        mark = w.chat_mark()
        M.click(SB.addButton)
        sc.check(any("already on your watchlist" in c for c in w.chat(mark)), "+ Watch twice: already there")
        sc.eq(watch_ids(w).count(target), 1, "no duplicates")

    # Type a name in the add box
    idx = name_index(ref)
    extra = next((i for i in sorted(ref.items) if i not in SIDEBAR_WATCH and known_name(ref.items[i]["name"])
                  and len(idx.get(name_key(ref.items[i]["name"]), [])) == 1), None)
    box = SB.addBox
    if extra is not None:
        box.SetFocus(box)
        box.SetText(box, ref.items[extra]["name"].lower())
        M.fireScript(box, "OnEnterPressed")
        sc.eq(watch_ids(w)[0], extra, "typed name (any case) is added on top")
        sc.eq(box.GetText(box), "", "add box clears")
        sc.check(not box.HasFocus(box), "add box lets go of the keyboard")
    mark = w.chat_mark()
    box.SetText(box, "Definitely Not An Item")
    M.fireScript(box, "OnEnterPressed")
    sc.check(any("no price data for Definitely Not An Item" in c for c in w.chat(mark)), "unknown name: says so")
    # Shift-click an item link while the box has the cursor
    w.lua.execute("ChatEdit_InsertLink = function(link) return false end")
    SB.hookedLink = None
    SB.HookInsertLink()
    sc.check(SB.hookedLink, "shift-click hook installed")
    w.ns.RemoveWatch(2589)
    box.SetFocus(box)
    w.g.ChatEdit_InsertLink("|cffffffff|Hitem:2589::::::::60:::::|h[Linen Cloth]|h|r")
    sc.eq(watch_ids(w)[0], 2589, "shift-clicked link is added")
    w.g.ChatEdit_InsertLink("|cffffffff|Hitem:4306::::::::60:::::|h[Silk Cloth]|h|r")
    sc.eq(watch_ids(w)[0], 2589, "links are ignored when the box does not have the cursor")

    # Context menus (MenuUtil)
    install_menu_mock(w)
    wl = watch_ids(w)
    row = shown_rows(SB, "watchRows")[0]
    M.click(row, "RightButton")
    sc.eq(menu_texts(w), ["Search in auction house", "Show price chart", "Move up", "Move down", "Remove from watchlist"],
          "watch row menu")
    menu_pick(w, "Move down")
    sc.eq(watch_ids(w)[:2], [wl[1], wl[0]], "Move down swaps with the next one")
    sc.eq(shown_rows(SB, "watchRows")[1].itemID, wl[0], "the rows redraw after a move")
    row = shown_rows(SB, "watchRows")[1]
    M.click(row, "RightButton")
    menu_pick(w, "Move up")
    sc.eq(watch_ids(w)[:2], wl[:2], "Move up puts it back")
    row = shown_rows(SB, "watchRows")[0]
    M.click(row, "RightButton")
    menu_pick(w, "Remove from watchlist")
    sc.check(wl[0] not in watch_ids(w), "Remove from watchlist")
    saved = w.g.ForeverAH_Settings.watchlist
    sc.eq([saved[i] for i in range(1, len(saved) + 1)], watch_ids(w), "watchlist is in the saved settings")

    if got:
        frow = shown_rows(SB, "flipRows")[0]
        fid = frow.itemID
        M.click(frow, "RightButton")
        texts = menu_texts(w)
        sc.check("Not interested (hide for a day)" in texts and "Search in auction house" in texts, "flip row menu")
        was = fid in watch_ids(w)
        sc.check(("Remove from watchlist" if was else "Add to watchlist") in texts, "flip menu offers watch/unwatch")
        # The star toggles the watchlist too
        M.click(frow.star)
        sc.eq(fid in watch_ids(w), not was, "star toggles the watchlist")
        sc.eq(frow.star.tex._atlas, "auctionhouse-icon-favorite" if not was else "auctionhouse-icon-favorite-off",
              "star shows filled when watched")
        M.click(frow.star)
        sc.eq(fid in watch_ids(w), was, "star toggles back")
        M.click(frow, "RightButton")
        menu_pick(w, "Not interested (hide for a day)")
        want2 = ref_flips(ref, hidden={fid}, gold=w.gold)
        got2 = shown_rows(SB, "flipRows")
        sc.eq([r.itemID for r in got2], [x["id"] for x in want2], "hidden idea is replaced by the next best")
        hid = w.g.ForeverAH_Settings.flipHidden
        sc.check(hid is not None and hid[fid] is not None and hid[fid] > M.now, "hidden idea saved with an expiry")

    # Tuck away and bring back
    M.click(SB.hideButton)
    sc.check(not F.IsVisible(F) and SB.tab.IsVisible(SB.tab), "< tucks the sidebar away, the tab stays")
    sc.eq(w.g.ForeverAH_Settings.sidebar, False, "tucked away is saved")
    w.close_ah_modern()
    sc.check(not SB.tab.IsVisible(SB.tab), "tab closes with the AH")
    w.open_ah_modern()
    sc.check(not F.IsVisible(F) and SB.tab.IsVisible(SB.tab), "reopening the AH keeps it tucked away")
    M.click(SB.tab)
    sc.check(F.IsVisible(F) and not SB.tab.IsVisible(SB.tab), "the tab brings it back")
    sc.eq(w.g.ForeverAH_Settings.sidebar, True, "shown again is saved")

    # Slash commands
    rune = N(R.RUNE)
    out = w.slash("/gl watch " + rune)
    sc.check(any(("watching " + rune) in c or "already on your watchlist" in c for c in out), "/gl watch Runecloth")
    sc.eq(watch_ids(w).count(R.RUNE), 1, "/gl watch: on the list once")
    out = w.slash("/gl unwatch " + rune.lower())
    sc.check(any(("stopped watching " + rune) in c for c in out), "/gl unwatch")
    sc.check(R.RUNE not in watch_ids(w), "/gl unwatch removed it")
    out = w.slash("/gl unwatch " + rune)
    sc.check(any("not on your watchlist" in c for c in out), "/gl unwatch twice")
    out = w.slash("/gl watch Nope Nothing")
    sc.check(any("no price data for Nope Nothing" in c for c in out), "/gl watch unknown")
    out = w.slash("/gl watchlist")
    sc.check(any(("watching %d items:" % len(watch_ids(w))) in c for c in out), "/gl watchlist lists it")
    out = w.slash("/gl watchlist reset")
    sc.eq(watch_ids(w), SIDEBAR_WATCH, "/gl watchlist reset brings back the starter list")
    out = w.slash("/gl flips")
    ideas_now = ref_flips(ref, limit=10, hidden={fid} if got else (), gold=w.gold)
    sc.eq(len(out), len(ideas_now) + 1 if ideas_now else 1, "/gl flips prints a line per idea (up to 10)")
    if ideas_now:
        slash_flips_ok(sc, out, ideas_now, ref, items, w.gold, "sidebar")
    out = w.slash("/gl help")
    sc.check(any("/gl watch" in c for c in out) and any("/gl chart" in c for c in out), "/gl help has both lists")
    out = w.slash("/gl sidebar")
    sc.check(any("now off" in c for c in out) and not F.IsVisible(F), "/gl sidebar tucks it away")
    out = w.slash("/gl sidebar")
    sc.check(any("now on" in c for c in out) and F.IsVisible(F), "/gl sidebar shows it")
    out = w.slash("/gl nonsense")
    sc.check(any("unknown command" in c for c in out), "other commands still reach Core")

    w.close_ah_modern()
    sc.check(not F.IsVisible(F), "sidebar closes with the AH")
    sc.check(SB.lastError is None, "sidebar: no swallowed error at the end (%s)" % SB.lastError)
    sc.clean(w)


def sc_sidebar_saved(sc, ref, items):
    """Saved settings come back: your own list, tucked away state."""
    w = World(ref, items, saved={"watchlist": [R.LINEN, "junk", R.RUNE, R.LINEN, -3], "sidebar": False})
    w.open_ah_modern()
    SB = w.ns.SidebarPanel
    sc.eq(watch_ids(w), [R.LINEN, R.RUNE], "saved watchlist is used (bad entries and repeats dropped)")
    sc.check(not w.g.ForeverAHSidebar.IsVisible(w.g.ForeverAHSidebar) and SB.tab.IsVisible(SB.tab),
             "saved tucked-away state is kept")
    w2 = World(ref, items, saved={"watchlist": []})
    w2.open_ah_modern()
    SB2 = w2.ns.SidebarPanel
    sc.eq(watch_ids(w2), [], "an emptied watchlist stays empty (no starter list forced back)")
    sc.check(SB2.watchMsg._shown, "empty watchlist explains how to add")
    w3 = World(ref, items, faction="Horde")
    w3.open_ah_modern()
    SB3 = w3.ns.SidebarPanel
    sc.eq(strip_codes(SB3.age._text), "Prices may not match this AH", "other faction: header warns in a few words")
    sc.check(SB3.why is not None and "Alliance" in SB3.why, "other faction: hover gives the full reason (%r)" % SB3.why)
    sc.clean(w, "saved")
    sc.clean(w2, "empty")
    sc.clean(w3, "horde")


def sc_sidebar_nodata(sc, ref, items):
    """No price data yet: the sidebar still opens and says what to do."""
    w = World(ref, items, data_src=PLACEHOLDER)
    w.open_ah_modern()
    SB = w.ns.SidebarPanel
    F = w.g.ForeverAHSidebar
    sc.check(F is not None and F.IsVisible(F), "no data: sidebar opens")
    sc.check(SB.flipMsg._shown and "No price data yet" in SB.flipMsg._text, "no data: flip area says so")
    sc.eq(strip_codes(SB.age._text), "No price data yet", "no data: header says so")
    rows = shown_rows(SB, "watchRows")
    sc.check(len(rows) > 0 and all(strip_codes(r.right._text) == "no data" for r in rows),
             "no data: watch rows show no data")
    out = w.slash("/gl flips")
    sc.check(any("no price data yet" in c for c in out), "no data: /gl flips says so")
    sc.check(SB.lastError is None, "no data: no swallowed error (%s)" % SB.lastError)
    sc.clean(w)


def sc_sidebar_wide(sc, ref, items):
    """Room under the AH: the chart docks below, the sidebar to the right, no overlap."""
    w = World(ref, items, screen=(768 * 16 / 9, 768, 0.64))
    w.open_ah_modern()
    C, F = w.ns.ChartPanel, w.g.ForeverAHSidebar
    sc.eq(C.placement, "below", "wide: chart below the AH")
    rc, rs = w.rect(C.frame), w.rect(F)
    sc.check(rc[3] <= rs[1] + 1e-6 or rc[2] <= rs[0] + 1e-6, "wide: chart and sidebar do not overlap (%s, %s)" % (rc, rs))
    sc.check(rs[2] <= w.M.screenW + 1e-6, "wide: sidebar on screen")
    sc.clean(w)


def sc_sidebar_first_snapshot(sc, ref, items):
    """The very first real snapshot has no sales yet (sold is measured between
    snapshots): the sidebar says it is collecting history, not 'no good flips'."""
    src = open(DATA_FILE, "rb").read().decode("utf-8")

    def zero_sold(iid, f):
        f = f + [""] * (10 - len(f))
        f[4] = "0"
        return f
    # The collector's flips work from one snapshot: this scenario is about the
    # history-based ideas, so the file has none (like an older file).
    first = drop_table(rewrite_items(src, zero_sold), "flips")
    sc.check(first != src or all(it["sold24"] == 0 for it in ref.items.values()), "first snapshot: test data made")
    ref = RefData(text=first)
    _, why = ref_flips_why(ref)
    sc.check(why in ("nosales", "history"), "first snapshot: the model says history is still being collected (%s)" % why)
    w = World(ref, items, data_src=first)
    w.open_ah_modern()
    SB = w.ns.SidebarPanel
    sc.check(SB.flipMsg._shown and SB.flipMsg._text == FLIP_MESSAGES.get(why),
             "first snapshot: flip area says it is collecting history (%r)" % SB.flipMsg._text)
    sc.eq(len(shown_rows(SB, "flipRows")), 0, "first snapshot: no flip rows")
    out = w.slash("/gl flips")
    sc.check(any(FLIP_SLASH.get(why, "?") in c for c in out), "first snapshot: /gl flips says so (%s)" % out)
    rows = shown_rows(SB, "watchRows")
    sc.check(len(rows) > 0 and all(strip_codes(r.right._text) != "no data" for r in rows if r.itemID in ref.items),
             "first snapshot: watchlist still shows prices")
    sc.clean(w)


def build_edge_data():
    """A second data file made by the real generator (fetch_ah.write_addon_data) from
    test_datafile.py's hand-made edge cases: gaps, nothing listed now, sold-only items,
    gold-cap prices, quotes, pipes, tabs and unicode in names. Synthetic, not real prices."""
    import json
    import tempfile
    sys.path.insert(0, HERE)
    import test_datafile
    tmp = tempfile.mkdtemp(prefix="fah_harness_edge_")
    test_datafile.edge_case_dir(tmp)
    items = json.load(open(os.path.join(tmp, "items.json"), encoding="utf-8"))
    src = open(os.path.join(tmp, "ForeverAH_Data.lua"), "rb").read().decode("utf-8")
    ref = RefData(os.path.join(tmp, "ForeverAH_Data.lua"))
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return ref, items, src


def sc_edge(sc, ref_unused, items_unused):
    ref, items, src = build_edge_data()
    sc.check(len(ref.items) >= 6, "edge file has items (%d)" % len(ref.items))
    w = World(ref, items, data_src=src)
    ns, M = w.ns, w.M
    sc.eq(w.load_errors, [], "edge file loads")
    sc.eq(ns.ItemCount(), ref.base_count(), "edge item count")
    idx = name_index(ref)
    for iid, it in ref.items.items():
        g = ns.GetItem(iid)
        for k in ("cheapest", "market", "listed", "sold24", "avg7", "vendor", "auctions"):
            sc.eq(g[k], it[k], "edge GetItem(%d).%s" % (iid, k))
        sc.eq(g.name, w.name_field(iid), "edge GetItem(%d).name" % iid)
        if known_name(it["name"]):
            sc.eq(ns.FindItemID(it["name"]), idx[name_key(it["name"])][0], "edge FindItemID(%r)" % it["name"])
    check_series_and_recommend(sc, w, ref)
    age = w.now - ref.updated
    tt = w.g.GameTooltip
    for compact in (False, True):
        if compact:
            w.slash("/gl compact")
        for iid in sorted(ref.items):
            tt.SetItemByID(tt, iid)
            sc.eq(w.lines(tt), expected_tooltip(ref, iid, age, compact=compact),
                  "edge tooltip %d%s" % (iid, " compact" if compact else ""))
    w.slash("/gl compact")
    # Chart for every item and timeframe, including windows with nothing listed
    for iid in sorted(ref.items):
        for tf in TFS:
            ns.SetChartTF(tf)
            ns.ShowChart(iid, "slash")
            win = ref.window(iid, tf)
            label = "edge %d %s" % (iid, tf)
            C = ns.ChartPanel
            if win["n"] < 2 or any(v is not None for v in win["cheap"] + win["market"]):
                chart_check(sc, w, ref, iid, tf, label)
                for idx in sorted({0, win["n"] // 2, max(0, win["n"] - 1)}):
                    hover_check(sc, w, ref, iid, tf, idx, label)
            else:
                want = ref_recommend(ref, iid, tf)
                why = no_advice_text(want)
                sc.check(C.msg._shown and C.msg._text == "Nothing was listed in this timeframe. Try a longer one.",
                         "%s: empty window message (%s)" % (label, C.msg._text))
                sc.eq(C.verdict._text, "No advice yet" if why else want["verdict"], "%s: verdict" % label)
                sc.eq(C.buyBox.value._text, why or (money_icons(want["buy"]) if want["buy"] > 0 else "-"), "%s: buy box" % label)
                sc.eq(C.listBox.value._text, why or (money_icons(want["listAt"]) if want["listAt"] > 0 else "-"),
                      "%s: list box" % label)
                sc.eq(int(C.pools.dot.used), 0, "%s: no dots" % label)
                M.enter(C.plot)
                w.tick(1)
                sc.check(not w.g.GameTooltip._shown, "%s: no hover tooltip on an empty chart" % label)
                M.leave(C.plot)
    # AH: Use button for every edge item that is in a bag
    ns.SetChartTF("1D")
    for slot, iid in enumerate(sorted(ref.items), start=1):
        w.put_bag(1, slot, iid, 5)
        t = w.lua.table()
        t.name, t.quality, t.icon, t.vendor, t.commodity = ref.items[iid]["name"], 1, 1000 + iid, 1, True
        t["class"] = "Trade Goods"
        M["items"][iid] = t
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    for slot, iid in enumerate(sorted(ref.items), start=1):
        ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 1, slot))
        w.settle(0.2)
        csf = ah.CommoditiesSellFrame
        pair = find_sell_buttons(csf)
        want = ref_buttons(ref, iid, "1D")
        buttons_ok(sc, pair, want, "edge %d" % iid)
        for k, v in enumerate(want):
            if v is not None and pair[k] is not None and pair[k].IsVisible(pair[k]):
                M.click(pair[k])
                sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), v, "edge %d: button %d fills the price" % (iid, k))
    w.close_ah_modern()
    sc.clean(w)


# ---------------------------------------------------------------------------
# Variants of the data under test, for the fields added to the data file and
# the first days of a new data set. Made by editing the data file's text.
# ---------------------------------------------------------------------------
B36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def b36_enc(n):
    if n == 0:
        return "0"
    s = ""
    while n:
        n, r = divmod(n, 36)
        s = B36_DIGITS[r] + s
    return s


def enc_prices(vals):
    """Data file price encoding: "~" nothing listed, base-36 change from the last price, "" no change."""
    out, prev = [], None
    for v in vals:
        if v is None:
            out.append("~")
            continue
        d = v if prev is None else v - prev
        out.append("" if (d == 0 and prev is not None) else (b36_enc(d) if d >= 0 else "-" + b36_enc(-d)))
        prev = v
    return ",".join(out)


def enc_series(cheap, market, sold):
    return enc_prices(cheap) + ";" + enc_prices(market) + ";" + ",".join("" if not v else b36_enc(v) for v in sold)


def rewrite_items(text, fn):
    """Calls fn(id, fields) for every item line (only inside the items table);
    fn returns the new field list (or None to keep it)."""
    def one(m):
        f = m.group(2).split("|")
        new = fn(int(m.group(1)[m.group(1).index("[") + 1:m.group(1).index("]")]), f)
        return m.group(1) + "|".join(new if new is not None else f) + '"'
    span = block_span(text, "items")
    if not span:
        return text
    a, b = span
    return text[:a] + re.sub(r'(\[\d+\]\s*=\s*")((?:[^"\\\n]|\\.)*)"', one, text[a:b]) + text[b:]


def drop_table(text, name):
    """The data text without a top-level table (variants or flips): an older data file."""
    m = re.search(r"(?m)^[ \t]*" + name + r"[ \t]*=[ \t]*\{[ \t]*\r?\n", text)
    if not m:
        return text
    end = re.search(r"(?m)^[ \t]*\},?[ \t]*\r?\n", text[m.end():])
    return text[:m.start()] + text[m.end() + end.end():]


def set_table(text, name, lines):
    """The data text with a top-level table (variants or flips) replaced by
    these entry lines, put before items = { like the collector writes it."""
    text = drop_table(text, name)
    body = "".join("    %s,\n" % ln for ln in lines)
    return re.sub(r"(?m)^([ \t]*)items([ \t]*=[ \t]*\{)", lambda m: "  %s = {\n%s  },\n%sitems%s" % (
        name, body, m.group(1), m.group(2)), text, count=1)


def lua_quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_header(text, key, value):
    """Sets a header field (salesHours = 6), adding it after updated = ... when missing."""
    head, sep, rest = text.partition("items = {")
    line = "%s = %s," % (key, value)
    if re.search(r"\b" + key + r"\s*=", head):
        head = re.sub(r"\b" + key + r"\s*=\s*[^,\n]*,", line, head, count=1)
    else:
        head = re.sub(r"(\bupdated\s*=\s*-?\d+,)", r"\1 " + line, head, count=1)
    return head + sep + rest


def last_points(series, k):
    """The newest k points of a decoded series, encoded again."""
    n = series["n"]
    if n == 0:
        return ";;"
    a = max(0, n - k)
    return enc_series(series["cheap"][a:], series["market"][a:], series["sold"][a:])


def other_items(ref, items, count, avoid=()):
    """Items with a unique plain name and prices that no other part of a scenario uses."""
    idx = name_index(ref)
    out = []
    for iid in sorted(ref.items):
        it = ref.items[iid]
        nm = it["name"]
        if (iid in ROLE_IDS() or iid in avoid or str(iid) not in items or not known_name(nm)
                or len(idx.get(name_key(nm), [])) != 1 or not all(ord(c) < 128 for c in nm) or it["cheapest"] <= 0):
            continue
        out.append(iid)
        if len(out) == count:
            break
    return out


def gold_title(gold):
    """What the sidebar says next to FLIP IDEAS: the gold the ideas are sized to."""
    return ("for your " + short_money(gold)) if gold is not None else ""


def check_sidebar_flips(sc, w, ref, label):
    """The flip area shows exactly the model's ideas (sized to the world's gold),
    or the model's reason for none."""
    SB = w.ns.SidebarPanel
    want, why = ref_flips_why(ref, gold=w.gold)
    got = shown_rows(SB, "flipRows")
    sc.eq([r.itemID for r in got], [x["id"] for x in want], "%s: flip ideas" % label)
    for r, x in zip(got, want):
        flip_row_ok(sc, r, x, ref, items_of(w), "%s: flip %d" % (label, x["id"]))
    sc.eq(SB.flipGold._text or "", gold_title(w.gold), "%s: the gold next to FLIP IDEAS" % label)
    if not want:
        sc.check(SB.flipMsg._shown and SB.flipMsg._text == FLIP_MESSAGES[why],
                 "%s: flip message for %s (%r)" % (label, why, SB.flipMsg._text))
        out = w.slash("/gl flips")
        sc.check(any(FLIP_SLASH[why] in c for c in out), "%s: /gl flips says the same (%s)" % (label, out))
    else:
        sc.check(not SB.flipMsg._shown, "%s: ideas shown, no message" % label)
    return want, why


def sc_sales_hours(sc, ref, items):
    """salesHours: 0 = still measuring (no sold number, no Thin market), 1 to 23 =
    an early estimate scaled to a day, 24 = a full day. Price-history flip ideas
    wait for 3 hours; the collector's gap and deal flips need sales in 2 hours of
    the last day (never scaled up), so at 0 only its vendor flips show."""
    for hours in (0, 1, 6, 24):
        text = set_header(ref.text, "salesHours", hours)
        r2 = RefData(text=text)
        label = "salesHours %d" % hours
        sc.eq(r2.salesHours, hours, "%s: test data made" % label)
        w = World(r2, items, data_src=text)
        ns = w.ns
        sc.eq(ns.SalesHours(), hours, "%s: read by the addon" % label)
        tt = w.g.GameTooltip
        age = w.now - r2.updated
        for iid in sample_ids(r2, 60):
            tt.SetItemByID(tt, iid)
            lines = w.lines(tt)
            sc.eq(lines, expected_tooltip(r2, iid, age), "%s: tooltip %d" % (label, iid))
            r = ns.Recommend(iid, "1D")
            want = ref_recommend(r2, iid, "1D")
            notes = [r.notes[i] for i in range(1, len(r.notes) + 1)]
            sc.eq(notes, want["notes"], "%s: notes %d" % (label, iid))
            sc.eq(r.soldPerDay, want["soldPerDay"], "%s: sold per day %d" % (label, iid))
            if hours == 0:
                sc.check("Thin market" not in notes, "%s: no Thin market note while measuring (%d)" % (label, iid))
                # (the tooltip is kept short: sales show on the chart)
                sc.eq(ns.SoldText(ns.GetItem(iid)), "sales: measuring", "%s: the sold text says measuring (%d)" % (label, iid))
                sc.check(not any((a or "").startswith("Listed ") for a, _ in lines), "%s: no sold line in the tooltip (%d)" % (label, iid))
        w.slash("/gl chart " + N(R.LINEN))
        chart_check(sc, w, r2, R.LINEN, "1D", label + " chart")
        w.open_ah_modern()
        want, why = check_sidebar_flips(sc, w, r2, label)
        hist, hwhy = ref_history_flips_why(r2)
        if hours == 0:
            sc.check(all(x.get("data") and x["kind"] == "vendor" for x in want),
                     "%s: only the collector's vendor flips (gap and deal flips wait for sales)" % label)
        relisted = [r for r in shown_rows(w.ns.SidebarPanel, "flipRows") if r.idea.data and r.idea.kind != "vendor"]
        if relisted:
            # a collector flip's numbers are from the hours measured so far, never scaled up
            w.M.enter(relisted[0])
            flat = [strip_codes(a or "") for a, _ in w.all_lines(tt)]
            early = ("Sales measured for %d %s so far, so these are early numbers." % (hours, "hour" if hours == 1 else "hours"))
            sc.eq(early in flat, hours < 24, "%s: collector flip hover says the sales are early numbers or not" % label)
            w.M.leave(relisted[0])
        if hours < 3:
            sc.eq((len(hist), hwhy), (0, "history"), "%s: no history ideas from under 3 hours of sales" % label)
            sc.check(all(x.get("data") for x in want), "%s: only the collector's flips" % label)
        elif 0 < hours < 24 and any(not x.get("data") for x in want):
            # a history idea (the collector's flips do not use the sales estimate)
            got = [r for r in shown_rows(w.ns.SidebarPanel, "flipRows") if not r.idea.data]
            w.M.enter(got[0])
            flat = [strip_codes(a or "") for a, _ in w.all_lines(tt)]
            sc.check(any(f.startswith("Early estimate") for f in flat), "%s: flip hover says the sales are an early estimate" % label)
            w.M.leave(got[0])
        w.close_ah_modern()
        sc.clean(w, label)


def one_snapshot_text(ref):
    """The data as the very first snapshot of a new data set: one price per item,
    no sales measured yet (salesHours 0)."""
    def fn(iid, f):
        it = ref.items[iid]
        f = f + [""] * (10 - len(f))
        f[4] = "0"
        f[7], f[8] = last_points(it["H"], 1), last_points(it["D"], 1)
        return f
    return set_header(rewrite_items(ref.text, fn), "salesHours", 0)


def sc_one_snapshot(sc, ref, items):
    """A brand new data set (like the first real snapshot): the chart says it is
    collecting history instead of drawing one dot, there are no targets from one
    price, sales are still being measured, and the sidebar says so (only the
    collector's vendor flips show: gap and deal flips wait for sales)."""
    text = one_snapshot_text(ref)
    r2 = RefData(text=text)
    sc.check(all(it["H"]["n"] <= 1 and it["D"]["n"] <= 1 for it in r2.items.values()), "one snapshot: test data made")
    w = World(r2, items, data_src=text)
    ns, M = w.ns, w.M
    C = None
    for iid in sample_ids(r2, 40):
        for tf in TFS:
            ns.SetChartTF(tf)
            ns.ShowChart(iid, "slash")
            chart_check(sc, w, r2, iid, tf, "one snapshot %d %s" % (iid, tf))
        hover_check(sc, w, r2, iid, "1Y", 0, "one snapshot %d" % iid)
    ns.SetChartTF("1D")
    ns.ShowChart(R.LINEN, "slash")
    C = ns.ChartPanel
    sc.eq((C.buyBox.value._text, C.listBox.value._text), ("Needs more history", "Needs more history"),
          "one snapshot: the target boxes say Needs more history")
    sc.eq((C.verdict._text, C.margin._text or "", bool(C.meter._shown)), ("No advice yet", "", False),
          "one snapshot: no verdict, margin or meter")
    sc.check(C.msg._shown and C.msg._text.startswith("Collecting price history. This chart fills in as hourly prices arrive (1 so far)"),
             "one snapshot: the plot says it is collecting history (%r)" % C.msg._text)
    sc.check((C.volume._text or "").startswith("sales: measuring"), "one snapshot: footer says sales are measuring")
    tt = w.g.GameTooltip
    age = w.now - r2.updated
    for iid in sample_ids(r2, 40):
        tt.SetItemByID(tt, iid)
        lines = w.lines(tt)
        sc.eq(lines, expected_tooltip(r2, iid, age), "one snapshot: tooltip %d" % iid)
        sc.check(not any((a or "").startswith("7-day avg") for a, _ in lines), "one snapshot: no 7-day avg on day one (%d)" % iid)
    # Sell now from one snapshot: 1 copper under its cheapest listing; no Best price yet
    w.put_bag(0, 1, R.LINEN, 20)
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    csf = ah.CommoditiesSellFrame
    w.M.commodityResults[R.LINEN] = None
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1))
    w.settle(0.2)
    pair = find_sell_buttons(csf)
    b, bb = pair
    snap = ref_buttons(r2, R.LINEN, "1D")
    sc.eq(snap, (r2.items[R.LINEN]["cheapest"] - 1 if r2.items[R.LINEN]["cheapest"] > 1 else None, None),
          "one snapshot: the model has Sell now from the snapshot and no Best price")
    buttons_ok(sc, pair, snap, "one snapshot, no live listing")
    sc.check("not enough price history yet for a suggested price" in (b.tipBody or ""),
             "one snapshot: Sell now tooltip says why there is no Best price (%s)" % b.tipBody)
    sc.check("live listings have not loaded yet" in (b.tipBody or ""), "one snapshot: and that it used the price data")
    # a live listing above today's market price: 1 copper under it
    lp = max(r2.items[R.LINEN]["cheapest"], r2.items[R.LINEN]["market"]) + 7
    w.set_commodity_results(R.LINEN, [(lp, 40, False)])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.LINEN)
    w.settle(0.3)
    want = ref_buttons(r2, R.LINEN, "1D", lp)
    sc.eq(want, (lp - 1, None), "one snapshot: the model follows the live listing")
    buttons_ok(sc, pair, want, "one snapshot, live listing")
    M.click(b)
    sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want[0], "one snapshot: fills that price")
    chart_check(sc, w, r2, R.LINEN, "1D", "one snapshot docked")
    # a live listing far under today's market price: Sell now still undercuts it
    # (that is what selling now means), and its tooltip says the market is higher
    mk = r2.items[R.RUNE]["market"]
    w.put_bag(0, 2, R.RUNE, 20)
    w.M.commodityResults[R.RUNE] = None
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 2))
    w.settle(0.2)
    lp2 = max(2, mk // 4)
    w.set_commodity_results(R.RUNE, [(lp2, 5, False)])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.RUNE)
    w.settle(0.3)
    rec2 = ref_recommend(r2, R.RUNE, "1D", lp2)
    want2 = ref_buttons(r2, R.RUNE, "1D", lp2)
    sc.check(not rec2["advice"] and not rec2["history"] and want2 == (lp2 - 1, None),
             "one snapshot: the model undercuts the cheap listing, no Best price (%s, market %d)" % (want2, mk))
    buttons_ok(sc, pair, want2, "one snapshot, cheap live listing")
    sc.check("Today's market price is higher" in (b.tipBody or ""), "one snapshot: tooltip says the market is higher (%s)" % b.tipBody)
    M.click(b)
    sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want2[0], "one snapshot: fills 1 copper under the listing")
    r = ns.Recommend(R.RUNE, "1D", lp2)
    sc.eq((r.postAt, r.followFloor, r.listAt), (rec2["postAt"], rec2["followFloor"], 0),
          "one snapshot: postAt and followFloor match the model, no List At advice")
    chart_check(sc, w, r2, R.RUNE, "1D", "one snapshot docked, cheap listing")
    want_f, why_f = check_sidebar_flips(sc, w, r2, "one snapshot")
    # the collector's vendor flips show from the first snapshot; its gap and deal
    # flips wait for sales in 2 hours, and the price-history ideas for 3
    vend = [f["id"] for f in r2.flips if f["kind"] == "vendor" and fit_flip(f, w.gold)]
    sc.check(all(x.get("data") and x["kind"] == "vendor" for x in want_f),
             "one snapshot: only the collector's vendor flips (%s)" % [x["id"] for x in want_f])
    sc.eq(len(want_f) > 0, len(vend) > 0, "one snapshot: a vendor flip the gold buys shows from the first snapshot")
    if not any(f["kind"] == "vendor" for f in r2.flips):
        # nothing can show yet, and more gold would not change that
        sc.eq(ns.SidebarPanel.flipMsg._text, FLIP_MESSAGES["history"], "one snapshot: sidebar says it is collecting price history")
    w.close_ah_modern()
    sc.clean(w)


def sc_short_history(sc, ref, items):
    """Sales are measured, but every item that trades has only 2 hourly prices:
    the sidebar says it is collecting price history, not 'no good flips'."""
    def fn(iid, f):
        it = ref.items[iid]
        f = f + [""] * (10 - len(f))
        f[7], f[8] = last_points(it["H"], 2), last_points(it["D"], 1)
        return f
    text = set_header(rewrite_items(ref.text, fn), "salesHours", 24)
    r2 = RefData(text=text)
    w = World(r2, items, data_src=text)
    w.slash("/gl chart " + N(R.RUNE))
    chart_check(sc, w, r2, R.RUNE, "1D", "two prices")
    hover_check(sc, w, r2, R.RUNE, "1D", 1, "two prices")
    w.open_ah_modern()
    want, why = check_sidebar_flips(sc, w, r2, "two prices")
    if R.SIM:
        hist, hwhy = ref_history_flips_why(r2)
        sc.eq((len(hist), hwhy), (0, "history"), "two prices: every history candidate waits for history")
    w.close_ah_modern()
    sc.clean(w)


def sc_unreliable(sc, ref, items):
    """Prices behind 1 or 2 listings, or a few units far above what a vendor
    pays, get no advice: grey boxes, a note, Use List At only follows a live
    listing, and they are never flip ideas. Plenty of units far above the vendor
    price (enchanting dust: vendor 1c) is a real price and keeps its advice."""
    # an item with 10 or more units listed, made to sell for 300x its vendor
    # price; a flip idea when there is one, so the sidebar's own filter is tested too
    was_ideas = [x["id"] for x in ref_history_flips_why(ref)[0]]
    dust = next((i for i in was_ideas + other_items(ref, items, 400, avoid=SIDEBAR_WATCH)
                 if i not in ROLE_IDS() and ref.items[i]["listed"] >= 10 and ref.items[i]["market"] >= 300
                 and (ref.items[i]["auctions"] is None or ref.items[i]["auctions"] >= 3)), None)

    def fn(iid, f):
        f = f + [""] * (10 - len(f))
        if iid == R.LINEN:
            f[9] = "2"
        elif iid == R.RUNE:
            f[9] = "1"
            f[6] = str(int(f[1]) + 50)         # a vendor pays more than 1 copper under its listing
        elif iid == R.ARCANE:
            f[6] = str(max(1, int(f[2]) // 300))
            f[3] = "4"                          # a few units: the vendor rule applies
            if f[9] != "":
                f[9] = "4"
        elif iid == dust:
            f[6] = str(max(1, int(f[2]) // 300))
        return f
    text = rewrite_items(ref.text, fn)
    r2 = RefData(text=text)
    sc.eq((r2.items[R.LINEN]["auctions"], r2.items[R.RUNE]["auctions"]), (2, 1), "unreliable: test data made")
    w = World(r2, items, data_src=text)
    ns, M = w.ns, w.M
    if sc.check(dust is not None, "unreliable: an item with 10+ units listed to test the vendor rule"):
        d = r2.items[dust]
        sc.check(d["vendor"] > 0 and d["market"] > 200 * d["vendor"] and d["listed"] >= 10,
                 "unreliable: %d sells far above its vendor price with %d units listed" % (dust, d["listed"]))
        for tf in ("1D", "1W"):
            ns.SetChartTF(tf)
            ns.ShowChart(dust, "slash")
            chart_check(sc, w, r2, dust, tf, "vendor-far deep %d %s" % (dust, tf))
            want = ref_recommend(r2, dust, tf)
            sc.check(not want["vendorFar"] and not any("vendor pays" in n for n in want["notes"]),
                     "vendor-far deep %d %s: the model trusts it (%s)" % (dust, tf, want["notes"]))
            sc.check("vendor pays" not in (ns.ChartPanel.notes._text or ""), "vendor-far deep %d %s: no vendor note" % (dust, tf))
            sc.eq(bool(want["advice"]), bool(want["history"]), "vendor-far deep %d %s: advice when there is history" % (dust, tf))
        check_series_and_recommend(sc, w, r2, ids=[dust])
    for iid, note in ((R.LINEN, "Only 2 listings: price may not be reliable"),
                      (R.RUNE, "Only 1 listing: price may not be reliable"),
                      (R.ARCANE, "Listed far above what a vendor pays: price may not be reliable")):
        for tf in ("1D", "1W", "1M"):
            ns.SetChartTF(tf)
            ns.ShowChart(iid, "slash")
            chart_check(sc, w, r2, iid, tf, "unreliable %d %s" % (iid, tf))
            want = ref_recommend(r2, iid, tf)
            sc.check(note in want["notes"] and not want["advice"], "unreliable %d %s: the model agrees (%s)" % (iid, tf, want["notes"]))
            sc.check(note in (ns.ChartPanel.notes._text or ""), "unreliable %d %s: chart note" % (iid, tf))
            sc.eq(ns.ChartPanel.listBox.value._text, no_advice_text(want), "unreliable %d %s: no List At" % (iid, tf))
    ns.SetChartTF("1D")
    check_series_and_recommend(sc, w, r2, ids=[R.LINEN, R.RUNE, R.ARCANE, R.LOTUS])
    w.put_bag(0, 1, R.LINEN, 20)
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    csf = ah.CommoditiesSellFrame
    w.M.commodityResults[R.LINEN] = None
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1))
    w.settle(0.2)
    pair = find_sell_buttons(csf)
    b, bb = pair
    # 2 listings: Sell now from the snapshot's cheapest, no Best price (not reliable)
    want0 = ref_buttons(r2, R.LINEN, "1D")
    sc.eq(want0[1], None, "unreliable: the model has no Best price")
    buttons_ok(sc, pair, want0, "unreliable, no live listing")
    lp = r2.items[R.LINEN]["cheapest"] + 5
    w.set_commodity_results(R.LINEN, [(lp, 40, False)])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.LINEN)
    w.settle(0.3)
    buttons_ok(sc, pair, (lp - 1, None), "unreliable: follows the live listing")
    reason = "not enough price history" if not ref_recommend(r2, R.LINEN, "1D", lp)["history"] else "may not be reliable"
    sc.check(reason in (b.tipBody or ""), "unreliable: button tooltip says why there is no Best price (%s)" % b.tipBody)
    # following a listing a vendor pays more for: the button and the chart notes still say so
    w.put_bag(0, 2, R.RUNE, 20)
    w.M.commodityResults[R.RUNE] = None
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 2))
    w.settle(0.2)
    lp_r = r2.items[R.RUNE]["cheapest"] + 5
    w.set_commodity_results(R.RUNE, [(lp_r, 1, False)])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.RUNE)
    w.settle(0.3)
    want_r = ref_recommend(r2, R.RUNE, "1D", lp_r)
    vend = r2.items[R.RUNE]["vendor"]
    P_r = ref_sell_prices(r2, R.RUNE, "1D", lp_r)
    sc.check(not want_r["advice"] and want_r["vendorBetter"] and P_r["nowVendor"] and P_r["now"] == lp_r - 1
             and (lp_r - 1) * 95 // 100 <= vend, "vendor beats the listing: the model warns (%s)" % want_r["notes"])
    buttons_ok(sc, pair, (lp_r - 1, None), "vendor beats the listing: Sell now follows it")
    sc.check("A vendor pays more than this after the 5% cut." in (b.tipBody or ""),
             "vendor beats the listing: button tooltip warns (%s)" % b.tipBody)
    r = ns.Recommend(R.RUNE, "1D", lp_r)
    notes = [r.notes[i] for i in range(1, len(r.notes) + 1)]
    sc.eq(notes, want_r["notes"], "vendor beats the listing: notes match the model")
    sc.check("A vendor pays more (%s)" % rec_model.money_text(vend) in notes and r.vendorBetter
             and r.postAt == lp_r - 1, "vendor beats the listing: note and postAt (%s, %s)" % (notes, r.postAt))
    # a few units far above the vendor price: the live listing is that same silly price, so no buttons
    w.put_bag(0, 3, R.ARCANE, 1)
    w.M.commodityResults[R.ARCANE] = None
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 3))
    w.settle(0.2)
    lp_a = r2.items[R.ARCANE]["cheapest"]
    w.set_commodity_results(R.ARCANE, [(lp_a, 1, False)])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.ARCANE)
    w.settle(0.3)
    want_a = ref_recommend(r2, R.ARCANE, "1D", lp_a)
    sc.check(want_a["vendorFar"] and ref_buttons(r2, R.ARCANE, "1D", lp_a) == (None, None), "vendor-far: the model has no buttons")
    buttons_ok(sc, pair, (None, None), "vendor-far: no buttons, even with a live listing")
    want, why = check_sidebar_flips(sc, w, r2, "unreliable")
    hist = ns.HistoryFlipIdeas(5)
    if isinstance(hist, tuple):   # (ideas, why) when there are none
        hist = hist[0]
    hist_ids = {hist[i].id for i in range(1, len(hist) + 1)}
    sc.eq(hist_ids, {x["id"] for x in ref_history_flips_why(r2)[0]}, "unreliable: history ideas match the model")
    sc.check(not (hist_ids & {R.LINEN, R.RUNE, R.ARCANE}), "unreliable: never a history flip idea")
    if dust is not None and dust in was_ideas:
        sc.check(dust in hist_ids, "vendor-far deep %d: still a flip idea" % dust)
    w.close_ah_modern()
    sc.clean(w)


def sc_names(sc, ref, items):
    """Items the data has no name for ("" or "Item 123") show and find the game's
    name; names two items share find both, and the AH search picks the listed one."""
    picks = other_items(ref, items, 3, avoid=SIDEBAR_WATCH)
    watched = [i for i in SIDEBAR_WATCH if i in ref.items and i not in ROLE_IDS() and str(i) in items
               and known_name(ref.items[i]["name"])]
    if not sc.check(len(picks) == 3, "names: enough items to test with (%s)" % picks):
        return
    x = watched[0] if watched else picks[2]       # the data has "" (newer files); the game knows it
    y, z = picks[0], picks[1]                      # "Item <id>" (older files), no game name yet; a second Linen Cloth
    x_name, y_name = ref.items[x]["name"], ref.items[y]["name"]

    def fn(iid, f):
        if iid == x:
            f[0] = ""
        elif iid == y:
            f[0] = "Item %d" % iid
        elif iid == z:
            f[0] = N(R.LINEN)
        return f
    text = rewrite_items(ref.text, fn)
    r2 = RefData(text=text)
    w = World(r2, items, data_src=text)
    ns, M = w.ns, w.M
    M["items"][y].name = None          # the game has not loaded it yet
    M["items"][z].name = N(R.LINEN)
    sc.eq(ns.GetItem(x).name, x_name, "names: GetItem name comes from the game when the data has none")
    sc.eq(ns.GetItem(y).name, "", "names: 'Item <id>' is an unknown name")
    sc.eq(ns.ItemName(y), "Item %d" % y, "names: shown as Item <id> until the game has it")
    sc.eq(ns.FindItemID("Item %d" % y), None, "names: the placeholder is not searchable")
    sc.eq(ns.FindItemID(x_name.upper()), x, "names: found by the game's name")
    out = w.slash("/gl chart " + x_name)
    sc.eq(ns.ChartItem(), x, "names: /gl chart with the game's name")
    sc.eq(ns.ChartPanel.name._text, x_name if len(x_name) <= 34 else x_name[:33] + "...", "names: chart header uses the game's name")
    chart_check(sc, w, r2, x, "1D", "names: unnamed item chart")
    ns.ShowChart(y, "slash")
    sc.eq(ns.ChartPanel.name._text, "Item %d" % y, "names: header for an item nobody has a name for")
    # typing that shown name back works like typing the number
    ns.ShowChart(R.LINEN, "slash")
    out = [strip_codes(o) for o in w.slash("/gl chart Item %d" % y)]
    sc.check(ns.ChartItem() == y and not any("no price data" in o for o in out), "names: /gl chart Item <id> (%s)" % out)
    out = [strip_codes(o) for o in w.slash("/gl watch [item %d]" % y)]
    sc.check(y in watch_ids(w) and any("watching Item %d" % y in o for o in out), "names: /gl watch [item <id>] (%s)" % out)
    w.slash("/gl unwatch Item %d" % y)
    sc.check(y not in watch_ids(w), "names: /gl unwatch Item <id>")
    sc.eq(ns.ResolveItem("Item 999999999"), None, "names: Item <id> without price data is not found")
    sc.eq(ns.ResolveItem("item %d" % y), y, "names: ResolveItem takes Item <id> in any case")
    M["items"][y].name = y_name        # the game loads it
    sc.eq(ns.ItemName(y), y_name, "names: the game's name once it has loaded")
    sc.eq(ns.FindItemID(y_name), y, "names: and it can be found by it")
    # names two items share
    pair = sorted([R.LINEN, z])
    ids = ns.FindItemIDs(N(R.LINEN))
    sc.eq([ids[i] for i in range(1, len(ids) + 1)], pair, "names: FindItemIDs lists both, lowest first")
    sc.eq(ns.FindItemID(N(R.LINEN).lower()), pair[0], "names: FindItemID gives the lowest")
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    sb = ah.SearchBar
    sb.SearchBox.SetText(sb.SearchBox, N(R.LINEN))
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), pair[0], "names: a shared name shows the lowest id first")
    sc.check(ns.AHState.pendingSearch == N(R.LINEN).lower(), "names: and waits for the search results")
    w.set_browse([(R.NO_BAG, 100, 1), (pair[1], 50, 20)])
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), pair[1], "names: the one in the results is shown")
    sb.SearchBox.SetText(sb.SearchBox, N(R.LINEN))
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    w.set_browse([(R.NO_BAG, 100, 1), (R.RUNE, 50, 20)])
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), pair[0], "names: results with neither do not switch to another item")
    sb.SearchBox.SetText(sb.SearchBox, x_name)
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), x, "names: an AH search by the game's name")
    # the sidebar's "Search in auction house" for the higher id of a shared name keeps that item
    # (neither listed comes last: then nothing gets clicked and the item is only remembered briefly)
    for listed, what in (([(pair[0], 50, 20)], "only the other one listed"),
                         ([(pair[0], 50, 20), (pair[1], 60, 20)], "both listed"), ([], "neither listed")):
        ns.SearchAH(pair[1])
        w.settle(0.4)
        sc.eq(ns.ChartItem(), pair[1], "names: sidebar search for %d shows it (%s)" % (pair[1], what))
        w.set_browse(listed)
        M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
        w.settle(1.2)
        sc.eq(ns.ChartItem(), pair[1], "names: and keeps it once the results arrive (%s)" % what)
    # a search typed afterwards starts fresh: the lowest id first
    sb.SearchBox.SetText(sb.SearchBox, N(R.LINEN))
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), pair[0], "names: a typed search is not held to an earlier sidebar search")
    # sidebar rows use the game's name too
    SB = ns.SidebarPanel
    ns.AddWatch(y)
    if x not in watch_ids(w):
        ns.AddWatch(x)
    rows = {r.itemID: r for r in shown_rows(SB, "watchRows")}
    sc.check(x in rows and rows[x].name._text == x_name, "names: watch row shows the game's name")
    sc.check(y in rows and rows[y].name._text == y_name, "names: watch row for the item named late")
    w.close_ah_modern()
    sc.clean(w)


def sc_y_axis(sc, ref, items):
    """Over 100g with prices less than a silver apart: the y-axis still has two labels."""
    p = max(1500000, ref.items[R.LOTUS]["market"])
    cheap = [p + (k % 5) * 10 for k in range(24)]
    market = [c + 5 for c in cheap]

    def fn(iid, f):
        if iid == R.LOTUS:
            f = f + [""] * (10 - len(f))
            f[1], f[2], f[5] = str(cheap[-1]), str(market[-1]), str(market[-1])
            f[7] = enc_series(cheap, market, [1] * 24)
        return f
    text = rewrite_items(ref.text, fn)
    r2 = RefData(text=text)
    sc.eq(r2.items[R.LOTUS]["H"]["cheap"], cheap, "y-axis: test data made")
    w = World(r2, items, data_src=text)
    ns = w.ns
    for tf in ("1D", "5D", "1W"):
        ns.SetChartTF(tf)
        ns.ShowChart(R.LOTUS, "slash")
        chart_check(sc, w, r2, R.LOTUS, tf, "y-axis %s" % tf)
        yl = ns.ChartPanel.pools.ylab
        labels = [yl["items"][k]._text for k in range(1, int(yl.used) + 1)]
        sc.check(len(labels) >= 2 and len(set(labels)) == len(labels), "y-axis %s: at least two different labels (%s)" % (tf, labels))
    sc.clean(w)


def sc_chart_lines(sc, ref, items):
    """The Target Buy and List At boxes are toggles: no dashed line by default;
    a click lights a box and shows only its line; the other box switches; the
    lit box again hides it. The choice is kept in the settings."""
    w = World(ref, items)
    ns, M = w.ns, w.M
    w.slash("/gl chart " + N(R.RUNE))
    C = ns.ChartPanel
    panel = [0.07, 0.08, 0.10]
    sc.eq(ns.settings.chartLine, None, "lines: none by default")
    chart_check(sc, w, ref, R.RUNE, "1D", "lines: default")
    sc.eq(int(C.pools.tag.used), 0, "lines: no dashed line by default")
    sc.check(C.buyBox._otype == "Button" and C.listBox._otype == "Button", "lines: the boxes are buttons")
    # hover: a hand cursor and a tooltip that says what a click does
    tt = w.g.GameTooltip
    M.enter(C.buyBox)
    sc.eq(M.cursor, "INTERACT_CURSOR", "lines: hand cursor over the Target Buy box")
    flat = [a for a, _ in w.all_lines(tt)]
    sc.check(w.same(tt.GetOwner(tt), C.buyBox) and flat and flat[0] == "Target Buy"
             and "Click to show the gold Target Buy line on the chart." in flat, "lines: Target Buy hover tooltip (%s)" % flat)
    sc.check(C.buyBox.highlight is not None and C.buyBox.highlight._layer == "HIGHLIGHT", "lines: the box lights up under the mouse")
    rec = ref_recommend(ref, R.RUNE, "1D")
    # click Target Buy: gold line only, box lit
    M.click(C.buyBox)
    sc.eq(ns.settings.chartLine, "buy", "lines: Target Buy click picks the gold line")
    chart_check(sc, w, ref, R.RUNE, "1D", "lines: buy")
    flat = [a for a, _ in w.all_lines(tt)]
    sc.check("Click to hide the gold Target Buy line." in flat, "lines: the tooltip now says a click hides it (%s)" % flat)
    if rec["buy"] > 0 and ref.window(R.RUNE, "1D")["n"] >= 2:
        d = C.pools.dash["items"][1]
        sc.eq([round(d._color[k], 3) for k in (1, 2, 3)], [0.898, 0.722, 0.259], "lines: the dashes are gold")
    bg = C.buyBox.bg._color
    sc.check(any(abs(bg[k + 1] - panel[k]) > 0.02 for k in range(3)) and abs(C.listBox.bg._color[2] - panel[1]) < 1e-6,
             "lines: the lit box is brighter, the other is not (%s)" % (bg,))
    M.leave(C.buyBox)
    sc.eq(M.cursor, None, "lines: cursor back to normal on leave")
    sc.check(not tt._shown, "lines: tooltip hidden on leave")
    # click List At: switches to the blue line only
    M.click(C.listBox)
    sc.eq(ns.settings.chartLine, "sell", "lines: List At click switches to the blue line")
    chart_check(sc, w, ref, R.RUNE, "1D", "lines: sell")
    if rec["listAt"] > 0 and ref.window(R.RUNE, "1D")["n"] >= 2:
        d = C.pools.dash["items"][1]
        sc.eq([round(d._color[k], 3) for k in (1, 2, 3)], [0.22, 0.74, 0.97], "lines: the dashes are blue")
    # the line stays across timeframes
    M.click(C.tfButtons["1W"])
    chart_check(sc, w, ref, R.RUNE, "1W", "lines: sell, 1W")
    M.click(C.tfButtons["1D"])
    # clicking the lit box again: no line
    M.click(C.listBox)
    sc.eq(ns.settings.chartLine, None, "lines: clicking the lit box turns the line off")
    chart_check(sc, w, ref, R.RUNE, "1D", "lines: off again")
    # the API does the same
    ns.SetChartLine("buy")
    chart_check(sc, w, ref, R.RUNE, "1D", "lines: SetChartLine buy")
    ns.SetChartLine("nonsense")
    sc.eq(ns.settings.chartLine, None, "lines: SetChartLine with a bad value turns the line off")
    # dragging a box still moves the panel
    M.drag(C.buyBox, "CENTER", "CENTER", 30, 20)
    cp = ns.settings.chartPoint
    sc.check(cp is not None and cp[1] == "CENTER" and cp[3] == 30 and cp[4] == 20, "lines: dragging a box moves the chart")
    sc.eq(ns.settings.chartLine, None, "lines: a drag is not a click")
    # other items keep the choice; items without advice show no line but keep it
    ns.SetChartLine("sell")
    for iid in sample_ids(ref, 30):
        ns.ShowChart(iid, "slash")
        chart_check(sc, w, ref, iid, "1D", "lines: sell %d" % iid)
    sc.clean(w, "lines")
    # saved settings: the choice comes back after a reload; a bad value is dropped
    w2 = World(ref, items, saved={"chartLine": "sell"})
    sc.eq(w2.ns.settings.chartLine, "sell", "lines: saved choice kept")
    w2.slash("/gl chart " + N(R.RUNE))
    chart_check(sc, w2, ref, R.RUNE, "1D", "lines: saved sell")
    w3 = World(ref, items, saved={"chartLine": "both"})
    sc.eq(w3.ns.settings.chartLine, None, "lines: a bad saved value is dropped")
    sc.clean(w2, "lines saved")
    sc.clean(w3, "lines bad saved")
    # a short panel under the AH: the boxes are one-line chips that work the same way,
    # and the saved choice (or no line) holds there too; both lines are never forced on
    w4 = World(ref, items, screen=(768 * 16 / 9, 768, 0.85), saved={"chartLine": "buy"})
    w4.open_ah_modern()
    w4.ns.ShowChart(R.RUNE, "ah")
    C4 = w4.ns.ChartPanel
    sc.check((C4.panelH or 260) < 230, "lines: short panel test set up (%s)" % C4.panelH)
    chart_check(sc, w4, ref, R.RUNE, "1D", "lines: short panel, saved buy")
    w4.M.click(C4.buyBox)
    sc.eq(w4.ns.settings.chartLine, None, "lines: short panel, the lit chip turns the line off")
    chart_check(sc, w4, ref, R.RUNE, "1D", "lines: short panel, no line")
    sc.eq(int(C4.pools.tag.used), 0, "lines: short panel shows no line by default")
    w4.M.click(C4.listBox)
    sc.eq(w4.ns.settings.chartLine, "sell", "lines: short panel, the List At chip picks the blue line")
    chart_check(sc, w4, ref, R.RUNE, "1D", "lines: short panel, sell")
    sc.check(int(C4.pools.tag.used) <= 1, "lines: short panel shows at most one line")
    w4.close_ah_modern()
    sc.clean(w4, "lines short")
    # a client where SetOwner does not clear: the box help does not stack up with each click
    w5 = World(ref, items, set_owner_clears=False)
    w5.slash("/gl chart " + N(R.RUNE))
    C5 = w5.ns.ChartPanel
    tt5 = w5.g.GameTooltip
    w5.M.enter(C5.buyBox)
    n0 = len(w5.all_lines(tt5))
    w5.M.click(C5.buyBox)
    w5.M.click(C5.buyBox)
    sc.eq(len(w5.all_lines(tt5)), n0, "lines: box help does not stack up after clicks (no-clear tooltip)")
    w5.M.leave(C5.buyBox)
    sc.clean(w5, "lines no-clear")


# ---------------------------------------------------------------------------
# Versions of gear (C1/C2: variants and vids), the collector's flips (C2c),
# and data files from before them.
# ---------------------------------------------------------------------------
def link_bonus_model(link):
    """Python model of Core.lua's LinkBonus: (sorted bonus ids, {type: value}) or None."""
    m = re.search(r"item:([-\d:]*)", link or "")
    if not m or m.group(1) == "":
        return None
    f = m.group(1).split(":")
    n = len(f)

    def whole(i):
        return int(f[i - 1]) if 1 <= i <= n and re.match(r"^-?\d+$", f[i - 1]) else None
    nb = whole(13) or 0
    if nb < 0 or nb > 40 or 13 + nb > n:
        nb = 0
    bonus = [v for v in (whole(13 + k) for k in range(1, nb + 1)) if v and v > 0]
    i = 14 + nb
    nm = whole(i) or 0
    mods = {}
    if 0 < nm <= 40 and i + 2 * nm <= n:
        for k in range(1, nm + 1):
            t, v = whole(i + 2 * k - 1), whole(i + 2 * k)
            if t is not None and v is not None:
                mods[t] = v
    return sorted(bonus), mods


def gear_link(iid, bonus, mods=((28, 6874),), level=30, suffix=""):
    """A link the way the modern client writes one for random-suffix gear."""
    f = [str(iid), "", "", "", "", "", suffix, "", str(level), "", "", "1", str(len(bonus))] + [str(b) for b in bonus]
    f += [str(len(mods))] + [str(x) for tm in mods for x in tm] + ["", "", "", ""]
    return "|cff1eff00|Hitem:" + ":".join(f) + "|h[Gear]|h|r"


def versioned_data(ref, items):
    """(text, ref, base, [vid1, vid2], [bonus1, bonus2]) with versions of one item.
    The data's own versions when it has them; otherwise two versions of a role
    item are written into the file (its prices x0.5 and x2), like the collector does."""
    text = ref.text
    for base in sorted(ref.variants):
        vids = [(k, v) for k, v in ref.variants[base] if v in ref.items and re.match(r"^\d+$", k)]
        if base in ref.items and str(base) in items and len(vids) >= 2:
            return (text, ref, base, [vids[0][1], vids[1][1]], [[int(vids[0][0])], [int(vids[1][0])]])
    base = R.ARCANE
    it = ref.items[base]
    keys = [[91001], [91002, 91003]]
    lines, vlines = [], []
    for n, (key, mult, suffix) in enumerate(((keys[0], 0.5, " of the Bear"), (keys[1], 2.0, " of the Owl")), start=1):
        vid = base * 1000 + n

        def sc_(vals):
            return [int(v * mult) if v else None for v in vals]
        h, d = it["H"], it["D"]
        f = [it["name"] + suffix, str(int(it["cheapest"] * mult)), str(int(it["market"] * mult)), str(it["listed"]),
             str(it["sold24"]), str(int(it["avg7"] * mult)), str(it["vendor"]),
             enc_series(sc_(h["cheap"]), sc_(h["market"]), h["sold"]),
             enc_series(sc_(d["cheap"]), sc_(d["market"]), d["sold"]), str(it["auctions"] or "")]
        lines.append("    [%d] = %s,\n" % (vid, lua_quote("|".join(f))))
        vlines.append("%s=%d" % (":".join(str(b) for b in sorted(key)), vid))
    text = set_table(text, "variants", ["[%d] = %s" % (base, lua_quote(",".join(vlines)))])
    span = block_span(text, "items")
    text = text[:span[1]] + "".join(lines) + text[span[1]:]
    r2 = RefData(text=text)
    return text, r2, base, [base * 1000 + 1, base * 1000 + 2], keys


def sc_versions(sc, ref, items):
    """Gear versions: links are read for their bonus ids; a version the data has
    prices for uses them (tooltip, chart, sell buttons), anything else falls
    back to the whole item's prices and says they cover all versions."""
    text, r2, base, vids, keys = versioned_data(ref, items)
    v1, v2 = vids
    sc.check(r2.base_of.get(v1) == base and r2.base_of.get(v2) == base, "versions: test data has two versions of %d" % base)
    w = World(r2, items, data_src=text)
    ns, M = w.ns, w.M
    age = w.now - r2.updated
    # the version lookups
    sc.check(ns.HasVariants(base) and not ns.HasVariants(v1), "versions: HasVariants")
    sc.check(ns.IsVariant(v1) and not ns.IsVariant(base), "versions: IsVariant")
    sc.eq((ns.BaseItemID(v1), ns.BaseItemID(base)), (base, base), "versions: BaseItemID")
    sc.eq(ns.VariantNumber(v1), v1 - base * 1000, "versions: VariantNumber")
    sc.eq(ns.VariantCount(), len(r2.base_of), "versions: VariantCount")
    sc.eq(ns.ItemCount(), r2.base_count(), "versions: ItemCount leaves versions out")
    sc.eq(ns.GetItem(v1).base, base, "versions: GetItem(vid).base")
    # link parsing, against the Python model
    links = [gear_link(base, keys[0]), gear_link(base, list(reversed(keys[1]))), gear_link(base, keys[0], mods=()),
             gear_link(base, [55555]), gear_link(base, []), M.ItemLink(base), M.ItemLink(base, 1034),
             "|cff1eff00|Hitem:%d::::::::30:::1:5:%d|h[cut short]|h|r" % (base, keys[0][0]),
             "|Hitem:%d::::::::30:::1:999:1:2:3|h[silly count]|h" % base,
             "|Hitem:%d::::::::30:::1:1:abc:1:28:6874|h[junk]|h" % base,
             "item:%d:0:0:0:0:0:0:0:30:0:0:1:1:%d:2:28:6874:9:5" % (base, keys[0][0]),
             "not a link", "", "item:", "|Hitem:%d|h[short]|h" % base,
             gear_link(base, [70002, 70001, 70003]), gear_link(base, keys[1][::-1] + [70009])]
    for ln in links:
        want = link_bonus_model(ln)
        got = ns.LinkBonus(ln)
        if isinstance(got, tuple):
            gb, gm = got
            gbl = [gb[i] for i in range(1, len(gb) + 1)]
            gml = {int(k): int(v) for k, v in gm.items()}
            sc.eq((gbl, gml), want, "versions: LinkBonus %r" % ln)
        else:
            sc.eq(got, want, "versions: LinkBonus %r" % ln)
        wkey = ":".join(str(b) for b in want[0]) if want else ""
        sc.eq(ns.VariantKey(ln), wkey, "versions: VariantKey %r" % ln)
        wv = r2.find_variant(base, want[0], want[1]) if want else None
        sc.eq(ns.FindVariant(base, ln), wv, "versions: FindVariant %r" % ln)
    sc.eq(ns.FindVariant(base, gear_link(base, keys[0])), v1, "versions: the first version's link finds it")
    sc.eq(ns.FindVariant(base, gear_link(base, list(reversed(keys[1])))), v2, "versions: bonus ids in any order")
    sc.eq(ns.FindVariant(base, gear_link(base, [55555])), None, "versions: an unknown version is not found")
    # VariantLink: a link the game can show, carrying that version's bonus ids
    for vid, key in ((v1, keys[0]), (v2, keys[1])):
        vl = ns.VariantLink(vid)
        sc.check(vl is not None and link_bonus_model(vl)[0] == sorted(key), "versions: VariantLink(%d) carries its bonus ids (%s)" % (vid, vl))
        sc.eq(ns.FindVariant(base, vl), vid, "versions: VariantLink(%d) finds it again" % vid)
    sc.eq(ns.VariantLink(base), None, "versions: no VariantLink for a plain id")
    sc.eq(ns.ResolveItem(gear_link(base, keys[0])), v1, "versions: ResolveItem of a version's link")
    sc.eq(ns.ResolveItem(gear_link(base, [55555])), base, "versions: ResolveItem of an unknown version: the item")
    # tooltips: bag items of each kind
    tt = w.g.GameTooltip
    cases = [("version", keys[0], v1, "version"), ("other version", keys[1], v2, "version"),
             ("unknown version", [55555], base, "all"), ("plain copy", None, base, "all"),
             ("old suffix link", "suffix", base, "all")]
    for slot, (what, key, pid, cover) in enumerate(cases, start=1):
        if key == "suffix":
            w.put_bag(2, slot, base, 3, suffix=1034)
        else:
            w.put_bag(2, slot, base, 3, bonus=key)
        for no_hl in (False, True):
            M.noHyperlink = no_hl
            tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
            tt.SetBagItem(tt, 2, slot)
            sc.eq(w.lines(tt), expected_tooltip(r2, pid, age, count=3, cover=cover),
                  "versions: %s tooltip%s" % (what, " (link from GetItem)" if no_hl else ""))
        M.noHyperlink = False
    ir = w.g.ItemRefTooltip
    ir.SetHyperlink(ir, gear_link(base, keys[1]))
    sc.eq(w.lines(ir), expected_tooltip(r2, v2, age, cover="version"), "versions: chat link of a version")
    tt.SetItemByID(tt, base)
    sc.eq(w.lines(tt), expected_tooltip(r2, base, age, cover="all"), "versions: the plain item by id covers all versions")
    # chart for a version: its own prices, the game's icon
    ns.ShowChart(v1, "slash")
    chart_check(sc, w, r2, v1, "1D", "versions: chart of a version")
    C = ns.ChartPanel
    if str(base) in items:
        sc.eq(C.icon._texture, 130000 + base, "versions: the chart shows the game item's icon")
    ns.ShowChart(base, "slash")
    chart_check(sc, w, r2, base, "1D", "versions: chart of the item")
    sc.check("Price covers all versions of this item" in (C.notes._text or ""), "versions: the item's chart says it covers all versions")
    sc.check(C.buyBox.value._text == "Depends on the version" and C.listBox.value._text == "Depends on the version"
             and C.verdict._text == "No advice yet", "versions: the whole item's chart gives no targets (they would mix versions)")
    for line in ("buy", "sell"):
        ns.SetChartLine(line)
        chart_check(sc, w, r2, base, "1D", "versions: chart of the item, %s line" % line)
        sc.eq(int(C.pools.tag.used), 0, "versions: no %s line for the whole item" % line)
    ns.SetChartLine(None)
    w.slash("/gl chart " + gear_link(base, keys[0]))
    sc.eq(ns.ChartItem(), v1, "versions: /gl chart with a version's link")
    # names: a version without a name in the data
    nm_text = rewrite_items(text, lambda iid, f: ([""] + f[1:]) if iid == v2 else None)
    w5 = World(RefData(text=nm_text), items, data_src=nm_text)
    gname = w5.game_name(base)
    want_nm = (gname + " (version %d)" % (v2 - base * 1000)) if gname else "Item %d (version %d)" % (base, v2 - base * 1000)
    sc.eq(w5.ns.ItemName(v2), want_nm, "versions: a version with no name in the data")
    sc.clean(w5, "versions names")
    # selling: the version's own prices; live gear listings per version
    M["items"][base].commodity = False
    w.put_bag(0, 1, base, 1, bonus=keys[0])
    w.put_bag(0, 2, base, 1, bonus=[55555])
    w.put_bag(0, 3, base, 1)
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    isf = ah.ItemSellFrame
    pair = find_sell_buttons(isf)
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1))
    w.settle(0.2)
    sc.eq(ns.ChartItem(), v1, "versions: a version in the sell slot shows its chart")
    buttons_ok(sc, pair, ref_buttons(r2, v1, "1D", is_commodity=False), "versions: sell buttons use the version's prices")
    sc.check("Prices for this version only" in (pair[0].tipBody or "") or not pair[0].IsVisible(pair[0]),
             "versions: the button tooltip says it is this version (%s)" % pair[0].tipBody)
    # the sell search came back with several versions
    cheap1 = max(200, r2.items[v1]["cheapest"] + 300)
    cheap2 = max(150, cheap1 // 3)
    t = w.lua.table()
    for k, row in enumerate([{"buyout": cheap2, "quantity": 1, "bonus": keys[1]},
                             {"buyout": cheap1, "quantity": 1, "bonus": keys[0]},
                             {"buyout": cheap1 + 900, "quantity": 1, "bonus": keys[0]},
                             {"buyout": 4321, "quantity": 1, "bonus": [55555]},
                             {"buyout": 9999, "quantity": 1}], start=1):
        t[k] = w.to_lua(row)
    M.itemResults[base] = t
    M.fire("ITEM_SEARCH_RESULTS_UPDATED", w.to_lua({"itemID": base, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}))
    w.settle(0.3)
    sc.eq(w.live(v1), cheap1, "versions: live price of this version (not the cheaper other one)")
    sc.eq(w.live(v2), cheap2, "versions: the other version's live price too")
    sc.eq(w.live(base), None, "versions: no all-versions live price when versions differ")
    buttons_ok(sc, pair, ref_buttons(r2, v1, "1D", cheap1, is_commodity=False), "versions: live listing of this version")
    # a version the data does not have: only its own live listing
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 2))
    w.settle(0.2)
    sc.eq(ns.ChartItem(), base, "versions: an unknown version shows the item's chart")
    buttons_ok(sc, pair, (price_for_frame(4320, False), None), "versions: unknown version follows its own live listing")
    sc.check("does not have this version" in (pair[0].tipBody or ""), "versions: and says so (%s)" % pair[0].tipBody)
    # a plain copy of an item whose versions differ: its own (no bonus ids) listings only
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 3))
    w.settle(0.2)
    buttons_ok(sc, pair, (price_for_frame(9998, False), None), "versions: plain copy follows plain listings")
    M.itemResults[base] = w.lua.table()
    M.fire("ITEM_SEARCH_RESULTS_UPDATED", w.to_lua({"itemID": base, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}))
    w.settle(0.3)
    buttons_ok(sc, pair, (None, None), "versions: no listing of that version: no buttons")
    sc.eq(w.live(v1), None, "versions: a version's live price is dropped with its listings")
    # a version with prices of its own, after the listings loaded with none of it: nothing to undercut,
    # so no Sell now from the gone listing (Best price, from the history, stays)
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1))
    w.settle(0.2)
    want_best = ref_buttons(r2, v1, "1D", is_commodity=False)[1]
    buttons_ok(sc, pair, (None, want_best), "versions: version with no listing up: no Sell now")
    if want_best:
        sc.check("No listing of this version is up right now" in (pair[1].tipBody or ""),
                 "versions: the Best price tooltip says no listing is up (%s)" % pair[1].tipBody)
    # the sidebar searches a version by its item's name and keeps the version on the chart
    ns.SearchAH(v1)
    w.settle(0.2)
    sc.eq(ah.SearchBar.SearchBox.GetText(ah.SearchBar.SearchBox), item_name(r2, items, base), "versions: search uses the item's name")
    sc.eq(ns.ChartItem(), v1, "versions: chart shows the version")
    w.set_browse([(base, 500, 3)])
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.5)
    sc.eq(ns.ChartItem(), v1, "versions: and keeps it when the item's listings open")
    w.settle(11)
    r = w.lua.table()
    r.itemKey = w.to_lua({"itemID": base, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0})
    r.minPrice = 500
    ah.SelectBrowseResult(ah, r)
    sc.eq(ns.ChartItem(), base, "versions: a later click on the item shows the item again")
    # an auction house that lists each version as its own row (item keys with a suffix)
    v1name, v2name = item_name(r2, items, v1), item_name(r2, items, v2)
    w.lua.execute("""
      MOCK.splitNames = {}
      local CAH = C_AuctionHouse
      MOCK.origKeyInfo = MOCK.origKeyInfo or CAH.GetItemKeyInfo
      CAH.GetItemKeyInfo = function(key)
        local info = MOCK.origKeyInfo(key)
        local n = key and MOCK.splitNames[key.itemSuffix or 0]
        if info and n then info.itemName = n end
        return info
      end
      MOCK.origBrowse = MOCK.origBrowse or CAH.GetBrowseResults
      CAH.GetBrowseResults = function()
        if not MOCK.splitRows then return MOCK.origBrowse() end
        return MOCK.splitRows
      end
    """)

    def split_rows(names):
        w.lua.execute("MOCK.splitNames = {}; MOCK.splitRows = {}")
        for k, (sfx, nm) in enumerate(names, start=1):
            w.M.splitNames[sfx] = nm
            w.M.splitRows[k] = w.to_lua({"itemKey": {"itemID": base, "itemLevel": 0, "itemSuffix": sfx,
                                                     "battlePetSpeciesID": 0}, "minPrice": 400 + k, "totalQuantity": 1})

    w.lua.execute("""
      local f = AuctionHouseFrame
      MOCK.openedRows = {}
      if not MOCK.origSelect then
        MOCK.origSelect = f.SelectBrowseResult
        f.SelectBrowseResult = function(self, row)
          MOCK.openedRows[#MOCK.openedRows + 1] = row.itemKey.itemSuffix
          return MOCK.origSelect(self, row)
        end
      end
    """)
    # rows that do not carry the version's name: none is opened, the player picks, and chat
    # says once that the searched version is not listed (it sold or was taken down)
    split_rows([(1017, "Odd Name One"), (1034, "Odd Name Two")])
    mark = len(w.chat())
    ns.SearchAH(v1)
    w.settle(0.2)
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_ADDED")
    w.settle(0.5)
    sc.eq(len(w.M.openedRows), 0, "versions: split rows with no matching name: none opened for the player")
    sc.eq(ns.ChartItem(), v1, "versions: and the chart stays on the version that was searched")
    said = [strip_codes(c) for c in w.chat()[mark:]]
    gone = "%s: none listed right now (it may have sold). These are its other versions." % v1name
    sc.eq(sum(1 for c in said if c.endswith(gone)), 1, "versions: says once that the version is not listed (%s)" % said)
    # the player clicks a row of a version the data cannot name: the item, never the searched version
    rk = w.M.splitRows[1]
    ah.SelectBrowseResult(ah, rk)
    sc.eq(ns.ChartItem(), base, "versions: clicking another version's row does not keep the searched version")
    w.settle(11)
    # rows named after the versions: the searched one is opened, clicks show each row's own version
    split_rows([(1017, v2name), (1034, v1name)])
    w.lua.execute("MOCK.openedRows = {}")
    ns.SearchAH(v1)
    w.settle(0.2)
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.5)
    sc.eq(list(w.M.openedRows.values()), [1034], "versions: the row with the version's name is the one opened")
    sc.eq(ns.ChartItem(), v1, "versions: chart of that version")
    ah.SelectBrowseResult(ah, w.M.splitRows[1])
    sc.eq(ns.ChartItem(), v2, "versions: clicking the other version's row shows that version")
    w.settle(11)
    # only ANOTHER version listed (one split row): never opened as if it were the searched one
    split_rows([(1017, v2name)])
    w.lua.execute("MOCK.openedRows = {}")
    mark = len(w.chat())
    ns.SearchAH(v1)
    w.settle(0.2)
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.5)
    sc.eq(len(w.M.openedRows), 0, "versions: one row of another version: not opened")
    said = [strip_codes(c) for c in w.chat()[mark:]]
    sc.check(any(c.endswith(gone) for c in said), "versions: and chat says the searched version is not listed (%s)" % said)
    # the version itself is the only row: opened, nothing said
    split_rows([(1034, v1name)])
    w.lua.execute("MOCK.openedRows = {}")
    mark = len(w.chat())
    ns.SearchAH(v1)
    w.settle(0.2)
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.5)
    sc.eq(list(w.M.openedRows.values()), [1034], "versions: the version's own single row is opened")
    sc.check(not any("none listed right now" in strip_codes(c) for c in w.chat()[mark:]), "versions: and nothing is said")
    w.settle(11)
    w.lua.execute("MOCK.splitRows = nil; MOCK.splitNames = {}")
    # watchlist: a shift-clicked version link is watched as that version
    SB = ns.SidebarPanel
    box = SB.addBox
    w.lua.execute("ChatEdit_InsertLink = function(link) return false end")
    SB.hookedLink = None
    SB.HookInsertLink()
    box.SetFocus(box)
    w.g.ChatEdit_InsertLink(gear_link(base, keys[1]))
    sc.eq(watch_ids(w)[0], v2, "versions: a shifted-clicked version link is watched as that version")
    row = shown_rows(SB, "watchRows")[0]
    sc.eq(row.name._text, item_name(r2, items, v2), "versions: watch row shows the version's name")
    if str(base) in items:
        sc.eq(row.icon._texture, 130000 + base, "versions: watch row shows the item's icon")
    # the old auction house frame gives no item link: gear whose versions differ gets no buttons
    # there (never the mixed all-versions price)
    wl = World(r2, items, data_src=text, client="legacy")
    wl.M.fire("AUCTION_HOUSE_SHOW")
    wl.settle(0.3)
    wl.M.cursorItem = wl.to_lua({"itemID": base, "count": 1})
    wl.M.click(wl.g.AuctionsItemButton)
    wl.settle(0.2)
    sc.eq(wl.ns.ChartItem(), base, "versions: old AH frame, gear in the sell slot")
    buttons_ok(sc, find_sell_buttons(wl.g.AuctionFrameAuctions), (None, None),
               "versions: old AH frame, gear whose versions differ: no buttons")
    wl.M.fire("AUCTION_HOUSE_CLOSED")
    wl.settle(0.3)
    sc.clean(wl, "versions legacy")
    # shift-click on a version's row puts a link in chat (not a search)
    w.lua.execute("MOCK.inserted = {}; ChatFrameUtil = nil; "
                  "ChatEdit_InsertLink = function(ln) table.insert(MOCK.inserted, ln) return true end")
    w.lua.execute("IsModifiedClick = function(k) return k == 'CHATLINK' end")
    bar = ah.SearchBar.SearchBox
    bar.SetText(bar, "")
    M.click(row)
    w.lua.execute("IsModifiedClick = function() return false end")
    inserted = list(w.M.inserted.values())
    sc.check(len(inserted) == 1 and "item:%d" % base in str(inserted[0]),
             "versions: shift-click on a version row links it in chat (%s)" % inserted)
    sc.eq(bar.GetText(bar), "", "versions: and does not search")
    M["items"][base].commodity = True
    w.close_ah_modern()
    sc.clean(w)


def with_series(text, series, sales_hours=None):
    """The data text with a last day of hourly prices and sales made by hand for
    some items: series[id] = (cheap, sold, sold24, vendor), oldest first (None in
    cheap: nothing listed that hour; vendor None: the item's own vendor price).
    The market price follows the cheapest, today's prices are the last one
    listed, 5 units are listed. salesHours is set when given."""
    def fn(iid, f):
        if iid not in series:
            return None
        ch, so, s24, vend = series[iid]
        f = list(f) + [""] * (10 - len(f))
        f[1] = f[2] = str(next(x for x in reversed(ch) if x))
        f[3], f[4] = "5", str(s24)
        if vend is not None:
            f[6] = str(vend)
        f[7] = enc_series(ch, ch, so)
        return f
    text = rewrite_items(text, fn)
    return set_header(text, "salesHours", sales_hours) if sales_hours is not None else text


def steady_series(price, per_hour=11, hours=11, vendor=None):
    """A last day (24 hourly slots) listed at `price` every hour, selling per_hour in
    each of the last `hours` hours: what it sold at is `price`. With 11 hours of
    sales no hour is capped whatever salesHours says (S = hours x per_hour), and 11
    a hour makes a rate over DAY_EXACT: a flip of up to 60 units then sells all of
    it within a day, so its profit in a day is its whole profit."""
    return ([price] * 24, [0] * (24 - hours) + [per_hour] * hours, per_hour * hours, vendor)


def synth_flips(ref, items):
    """(lines, series): flip lines like the collector writes ("id|kind|buyMax|units|
    cost|relist|profit|roi|risk"), made from the data's prices, plus a broken one
    and a repeat that must be skipped, and hourly sales that back their relists."""
    out = []
    it = ref.items[R.RUNE]
    buy = max(2, it["cheapest"])
    rel = max(buy * 2, it["market"])
    units = min(60, max(7, 3000 // max(1, rel * 95 // 100 - buy) + 1))
    cost = buy * units
    profit = rel * 95 // 100 * units - cost
    out.append("%d|gap|%d|%d|%d|%d|%d|%d|thin,slow" % (R.RUNE, buy, units, cost, rel, profit, profit * 1000 // cost))
    lin = ref.items[R.LINEN]
    vend = max(1, lin["vendor"])
    out.append("%d|vendor|%d|40|%d|0|%d|%d|" % (R.LINEN, max(1, vend - 3), max(1, vend - 3) * 40, 3 * 40, 3 * 40 * 1000 // (max(1, vend - 3) * 40)))
    lot = max(2, ref.items[R.LOTUS]["cheapest"])
    out.append("%d|gap|%d|1|%d|%d|%d|%d|spike" % (R.LOTUS, lot, lot, lot * 2, lot, 1000))
    out.append("%d|bogus|1|1|1|1|1|1|" % R.ARCANE)         # unknown kind: skipped
    out.append("garbage")                                  # not a flip: skipped
    out.append("%d|gap|5|1|5|0|3|600|" % R.ARCANE)         # a gap flip with no relist price: skipped
    out.append("%d|gap|%d|2|%d|%d|50|10|" % (R.RUNE, buy, buy * 2, rel))   # the same item again: shown once
    return out, {R.RUNE: steady_series(rel, vendor=it["vendor"]),
                 R.LOTUS: steady_series(lot * 2, vendor=ref.items[R.LOTUS]["vendor"])}


def flips_data(ref, items):
    """(text, ref) with the collector's flips: the data's own (judged on its own
    sales), else made up from its prices (with sales that back them)."""
    if ref.flips:
        return ref.text, ref
    lines, series = synth_flips(ref, items)
    text = set_table(with_series(ref.text, series), "flips", [lua_quote(s) for s in lines])
    return text, RefData(text=text)


def data_flip_tip(x, ref):
    """Sidebar.lua DataFlipTooltip as flattened tooltip lines ("left | right"): what
    to do, the price a gap or deal flip is counted at (and what it has been selling
    for), the usual price of a deal, what the gold buys, then what it can make
    within a day; a risk only for gear that comes in versions."""
    usual = x.get("usual") or 0
    vendor = x["kind"] == "vendor"
    capped = not vendor and x["countAt"] < x["relist"]
    out = ["Flip idea from the latest prices | "]
    if vendor:
        out.append("These listings cost less than a vendor pays for them: buy them and sell them to a vendor. | ")
    elif x["kind"] == "deal":
        out.append("Listed well under its usual price%s%s | " % (
            (" (usual %s)" % money_text(usual)) if usual > 0 else "",
            ", but it has been selling for less: buy it and relist it at the price below." if capped
            else ": buy it and relist it at the price below, about what it has been selling for."))
    else:
        out.append("Buy the cheap listings, then relist them at about what it has been selling for. | ")
    out.append("Buy up to (each) | " + money_text(x["buyMax"]))
    out.append("That buys %s, costing | %s" % (count_text(x["units"]), money_text(x["cost"])))
    if vendor:
        out += ["Then | sell to a vendor", "Profit over the cost | +" + money_text(x["profit"])]
    else:
        out += ["Relist at (each) | " + money_text(x["countAt"]), "Selling lately for about | " + money_text(x["refPrice"])]
        if capped:
            out.append("You could ask up to %s, but it has not been selling that high. | " % money_text(x["relist"]))
    if x["kind"] == "deal" and usual > 0:
        out.append("Usual price (last 7 days) | " + money_text(usual))
        if deal_under(x):
            out.append("Under the usual price by | %d%% or more" % deal_under(x))
    if gold_line(x):
        out.append(gold_line(x))
    if not vendor:
        n = x["fit"]["units"] if x.get("fit") else x["units"]
        day, rate = x["day"], x["rate"]
        out += ["  | ", "Within a day | ",
                "You can expect to sell | %s a day" % (("~" + count_text(rate)) if rate >= 1 else "under 1")]
        if n == 1:
            out.append("Chance yours sells in a day | %d%%" % math.floor(100 * day + 0.5))
        else:
            out.append("Of your %s, likely to sell | %s" % (count_text(n), ("~" + count_text(day)) if day >= 0.5 else "under 1"))
        out.append("Profit in a day, after the 5% cut | +" + money_text(x["dayProfit"]))
        if n - day >= 1 and (n - day) * x["deposit"] >= DAY_DEPOSIT_NOTE:
            out.append("The rest may take longer. Each one that expires unsold loses its deposit (about %s). | "
                       % money_text(x["deposit"]))
        h = rec_model.sales_hours_of(ref.salesHours)
        if h < 24:
            out.append("Sales measured for %d %s so far, so these are early numbers. | " % (h, "hour" if h == 1 else "hours"))
    if "suffix" in x["risk"]:
        out += ["  | ", "Watch out | ", SUFFIX_TEXT + " | "]
    return out


# Hover lines a collector flip no longer has: risk words other than versions (the
# day economics count what they warn of), the return on spend and the profit if
# every unit sold at the relist price.
GONE_FLIP_LINES = ("Thin:", "Slow:", "Spike:", "Unsure:", "Risk:", "Return on what you spend", "Profit, after the 5% cut")


def data_flip_hover_ok(sc, w, row, x, ref, lab):
    """Hovering a collector flip row: the game's item tooltip, then exactly the
    flip's lines (data_flip_tip), and none of the lines a flip no longer has."""
    tt = w.g.GameTooltip
    w.M.enter(row)
    flat = [strip_codes(a or "") + " | " + strip_codes(b or "") for a, b in w.all_lines(tt)]
    want = data_flip_tip(x, ref)
    i = flat.index(want[0]) if want[0] in flat else -1
    sc.check(i >= 0, lab + ": title")
    sc.eq(flat[i:i + len(want)] if i >= 0 else flat, want, lab + ": the flip's lines")
    sc.check(not any(f.startswith(GONE_FLIP_LINES) for f in flat), lab + ": no risk words but versions, no return or whole profit")
    sc.eq(sum(1 for f in flat if f.startswith("Watch out")), 1 if "suffix" in x["risk"] else 0,
          lab + ": a Watch out only for gear versions")
    if x["id"] in ref.base_of:
        sc.check(any(f.startswith("Prices for this version only") for f in flat), lab + ": the item tooltip is the version's")
    sc.check(any("Goblin Ledger" in f for f in flat[:max(0, i)]), lab + ": the Goblin Ledger price lines come first")
    sc.check(any("Click: open it in the auction house" in f for f in flat), lab + ": says what a click does")
    w.M.leave(row)
    return flat


def data_flips_of(ns):
    """ns.DataFlips() as the model's dicts (parse_flip)."""
    df = ns.DataFlips()
    return [{"id": df[i].id, "kind": df[i].kind, "buyMax": df[i].buyMax, "units": df[i].units, "cost": df[i].cost,
             "relist": df[i].relist, "profit": df[i].profit, "roi": df[i].roi,
             "risk": [df[i].risk[k] for k in range(1, len(df[i].risk) + 1)], "usual": df[i].usual}
            for i in range(1, len(df) + 1)]


def sc_data_flips(sc, ref, items):
    """The collector's flips show first in the sidebar (vendor flips from the
    first snapshot, gap and deal flips once sales back them), each row: icon,
    name, Buy up to X (N), Relist Y (the price its profit is counted at) or
    Sell to vendor, +the profit within a day and the sales per day, explained
    on hover."""
    text, r2 = flips_data(ref, items)
    sc.check(len(r2.flips) > 0, "collector flips: test data has flips (%d)" % len(r2.flips))
    w = World(r2, items, data_src=text)
    ns, M = w.ns, w.M
    sc.eq(data_flips_of(ns), r2.flips, "collector flips: DataFlips reads every valid flip, in order")
    w.open_ah_modern()
    SB = ns.SidebarPanel
    want, why = check_sidebar_flips(sc, w, r2, "collector flips")
    rows = shown_rows(SB, "flipRows")
    sc.check(bool(want) and bool(want[0].get("data")) and len(rows) == len(want), "collector flips: a collector flip comes first")
    for r, x in zip(rows, want):
        if x.get("data"):
            data_flip_hover_ok(sc, w, r, x, r2, "collector flip %d hover" % x["id"])
    # clicking a collector flip searches its item and shows its chart
    first = rows[0]
    fid = first.itemID
    M.click(first)
    w.settle(0.2)
    base = r2.base_of.get(fid, fid)
    sc.eq(w.g.AuctionHouseFrame.SearchBar.SearchBox.GetText(w.g.AuctionHouseFrame.SearchBar.SearchBox),
          item_name(r2, items, base), "collector flips: click searches the item's name")
    sc.eq(ns.ChartItem(), fid, "collector flips: and shows its chart")
    # not interested: the next one moves up
    install_menu_mock(w)
    M.click(first, "RightButton")
    menu_pick(w, "Not interested (hide for a day)")
    want2 = ref_flips(r2, hidden={fid}, gold=w.gold)
    sc.eq([r.itemID for r in shown_rows(SB, "flipRows")], [x["id"] for x in want2], "collector flips: a hidden flip is replaced")
    out = w.slash("/gl flips")
    ideas = ref_flips(r2, limit=10, hidden={fid}, gold=w.gold)
    sc.eq(len(out), len(ideas) + 1, "collector flips: /gl flips prints them")
    slash_flips_ok(sc, out, ideas, r2, items, w.gold, "collector flips")
    for line, x in zip(out[1:], ideas):
        s = strip_codes(line)
        if x.get("data"):
            sc.check("(risk:" not in s and "(usual" not in s, "collector flips: no risks or usual price in a chat line (%s)" % s)
    w.close_ah_modern()
    sc.clean(w)
    # a collector flip on a price that may be one player's guess (a few units far above
    # what a vendor pays): the addon adds no "unsure" of its own any more, the flip is
    # judged by what it has been selling for (here: at its relist price, every hour)
    lot = ref.items[R.LOTUS]
    if lot["market"] > 0 and lot["cheapest"] > 0:
        vend = max(1, lot["market"] // 300)
        buy = max(10000, lot["cheapest"], lot["market"] // 2)
        rel = buy * 2
        doubt = with_series(ref.text, {R.LOTUS: steady_series(rel, vendor=vend)})
        doubt = rewrite_items(doubt, lambda iid, f: (f[:3] + ["4"] + f[4:9] + ["4"]) if iid == R.LOTUS else None)
        doubt = set_table(doubt, "flips", [lua_quote("%d|gap|%d|1|%d|%d|%d|%d|" % (R.LOTUS, buy, buy, rel, rel * 95 // 100 - buy,
                                                                                (rel * 95 // 100 - buy) * 1000 // buy))])
        r4 = RefData(text=doubt)
        sc.check(price_doubt(r4, R.LOTUS), "collector flips: test data with a doubtful price made")
        # enough gold for it (a Black Lotus at half its market price is more than the mock's 123g)
        w4 = World(r4, items, data_src=doubt, gold=RICH_GOLD)
        w4.open_ah_modern()
        want4, _ = check_sidebar_flips(sc, w4, r4, "collector flips, doubtful price")
        sc.check(bool(want4) and want4[0]["id"] == R.LOTUS and want4[0]["risk"] == [],
                 "collector flips: no unsure of the addon's own on a doubtful price (%s)" % (want4[:1],))
        rows4 = shown_rows(w4.ns.SidebarPanel, "flipRows")
        if rows4 and want4 and want4[0].get("data"):
            risk = rows4[0].idea.risk
            sc.eq([risk[i] for i in range(1, len(risk) + 1)], [], "collector flips: the addon's idea has no risk added")
            data_flip_hover_ok(sc, w4, rows4[0], want4[0], r4, "collector flips, doubtful price hover")
        w4.close_ah_modern()
        sc.clean(w4, "doubtful flip")
    # from a single snapshot (no sales measured yet) only the vendor flips show: gap
    # and deal flips wait for sales in 2 hours
    one = one_snapshot_text(r2)
    r3 = RefData(text=one)
    w2 = World(r3, items, data_src=one)
    w2.open_ah_modern()
    want3, _ = check_sidebar_flips(sc, w2, r3, "collector flips, one snapshot")
    sc.check(all(x.get("data") and x["kind"] == "vendor" for x in want3),
             "collector flips: only vendor flips from the very first snapshot (%s)" % [x["id"] for x in want3])
    sc.eq(len(want3) > 0, any(f["kind"] == "vendor" and fit_flip(f, w2.gold) for f in r3.flips),
          "collector flips: a vendor flip the gold buys still shows from the very first snapshot")
    w2.close_ah_modern()
    sc.clean(w2, "one snapshot")


# ---------------------------------------------------------------------------
# Flip ideas sized to the gold on the character, and deals (items listed well
# under their usual price). Hand-made flip lines on items from the data under
# test, written the way the collector writes them (best first by profit), on
# items given hourly sales that back their relist prices (steady_series: they
# sell fast, so a flip's profit within a day is its whole profit on what the
# gold buys, and the old by-hand numbers still hold).
# ---------------------------------------------------------------------------
def gold_flip_data(ref, items):
    """(text, ref, [a, b, c, d]): flips to size to a little gold. a is too dear for
    2g, 2g buys 2 of b's 20, c (cheap, all of it for 2g) then ranks first, d is a
    vendor flip, and b has a cheap vendor flip too (shown when b's gap is out of reach)."""
    picks = [i for i in other_items(ref, items, 60, avoid=SIDEBAR_WATCH) if i not in ref.variants][:4]
    if len(picks) < 4:
        return None, None, picks
    a, b, c, d = picks
    lines = [
        "%d|gap|400000|1|400000|600000|170000|425||600000" % a,
        "%d|gap|11000|20|200000|14000|66000|330|thin|14500" % b,
        "%d|gap|4000|4|15001|6500|9699|646||7000" % c,          # 1 of 4 costs 37s 51c (rounded up)
        "%d|vendor|70|200|12000|0|6000|500||0" % d,
        "%d|vendor|40|50|1900|0|600|315|" % b,     # an older 9-field line
    ]
    text = with_series(ref.text, {a: steady_series(600000), b: steady_series(14000), c: steady_series(6500)})
    text = set_table(text, "flips", [lua_quote(s) for s in lines])
    return text, RefData(text=text), picks


def sized_order_ok(sc, ideas, gold, label):
    """The ranking, checked on a list of ideas without the model's own sorting:
    the collector's flips before the price-history ideas; the collector's flips
    by the profit expected within a day (on what the gold buys), then the share
    of the units bought that sells in a day (the solid deals kept a place come
    from further down the same ranking, so this holds for them too); the history
    ideas with gold best profit on what the gold buys first, without gold the
    ones selling 60+ a day first, each group by a day's profit."""
    kinds = [bool(x.get("data")) for x in ideas]
    sc.check(kinds == sorted(kinds, reverse=True), "%s: the collector's flips come before the history ideas" % label)
    data = [x for x in ideas if x.get("data")]
    hist = [x for x in ideas if not x.get("data")]
    keys = [(x["dayValue"], x["day"] / (x["fit"] or x)["units"]) for x in data]
    sc.check(keys == sorted(keys, reverse=True), "%s: collector flips, most expected within a day first (%s)" % (label, keys))
    if gold is not None:
        p = [x["fit"]["profit"] for x in hist]
        sc.check(p == sorted(p, reverse=True), "%s: history ideas, best profit on your gold first (%s)" % (label, p))
    else:
        sc.check(all(y["thin"] or not x["thin"] for x, y in zip(hist, hist[1:]))
                 and all(x["perDay"] >= y["perDay"] for x, y in zip(hist, hist[1:]) if x["thin"] == y["thin"]),
                 "%s: history ideas, fast sellers first, each by a day's profit (%s)"
                 % (label, [(x["id"], x["thin"], x["perDay"]) for x in hist]))


def sized_rows_ok(sc, w, ref, label):
    """The sidebar's ideas, rows and hovers for the world's gold; returns the model's ideas."""
    want, why = check_sidebar_flips(sc, w, ref, label)
    sized_order_ok(sc, want, w.gold, label)
    rows = shown_rows(w.ns.SidebarPanel, "flipRows")
    tt = w.g.GameTooltip
    for r, x in zip(rows, want):
        if x.get("data"):
            data_flip_hover_ok(sc, w, r, x, ref, "%s: hover %d" % (label, x["id"]))
        else:
            w.M.enter(r)
            flat = [strip_codes(a or "") + " | " + strip_codes(b or "") for a, b in w.all_lines(tt)]
            if x.get("fit"):
                whole = x["fit"]["whole"]
                sc.check("For a day: buy %s, costing | %s" % (count_text(whole), money_text(whole * x["buy"])) in flat
                         and gold_line(x) in flat, "%s: history hover %d: a day's flip and what your gold buys" % (label, x["id"]))
            else:
                sc.check(not any(f.startswith(("For a day", "With your gold", "Your gold covers")) for f in flat),
                         "%s: history hover %d: nothing about gold" % (label, x["id"]))
            sc.eq("Slower seller: may take a while to sell. | " in flat, x["thin"],
                  "%s: history hover %d: slower seller (under 60 a day) or not" % (label, x["id"]))
            w.M.leave(r)
    return want, why


def sc_flip_gold(sc, ref, items):
    """Flip ideas are sized to the gold on the character (GetMoney): with plenty,
    the same ideas in the same order with the same numbers as without sizing;
    with a little, what it cannot buy is left out, what it buys part of says
    "n of m", and the ideas rank by the profit on what you can buy (here they
    sell fast: all of it within a day); with none, the sidebar says so. A gold
    change (PLAYER_MONEY) works them out again."""
    text, r2, picks = gold_flip_data(ref, items)
    if not sc.check(text is not None, "gold: enough items to test with (%s)" % picks):
        return
    a, b, c, d = picks
    sc.eq([(f["id"], f["kind"], f["usual"]) for f in r2.flips],
          [(a, "gap", 600000), (b, "gap", 14500), (c, "gap", 7000), (d, "vendor", 0), (b, "vendor", 0)], "gold: test data made")
    # Plenty of gold: nothing changes but the words about it
    w = World(r2, items, data_src=text, gold=RICH_GOLD)
    w.open_ah_modern()
    rich, _ = sized_rows_ok(sc, w, r2, "gold rich")
    plain, _ = ref_flips_why(r2, gold=None)
    rdata, pdata = [x for x in rich if x.get("data")], [x for x in plain if x.get("data")]
    sc.eq([x["id"] for x in rdata], [x["id"] for x in pdata], "gold rich: the collector's flips are the same, in the same order")
    sc.check(rich and all(x["fit"] and not x["fit"]["limited"] for x in rich), "gold rich: the gold covers all of every idea")
    sc.eq([(x["fit"]["units"], x["fit"]["cost"], x["fit"]["profit"]) for x in rdata],
          [(x["units"], x["cost"], x["profit"]) for x in pdata], "gold rich: the collector's flips keep their numbers")
    sc.eq([x["dayProfit"] for x in rdata], [x["profit"] for x in pdata],
          "gold rich: fast sellers make their whole profit within a day")
    # Then the price-history ideas, each a whole day's flip (a day's sales, at most VOL_CAP,
    # at least 1), the biggest first however fast they sell: sized to the gold they rank
    # together, while without sizing the items selling 60+ a day come first.
    seen = {x["id"] for x in rdata}
    allh = [x for x in ref_history_flips_why(r2, limit=60, gold=None)[0] if x["id"] not in seen]

    def day(x):
        return max(1, math.floor(min(x["sold"], VOL_CAP))) * x["profit"]
    byday = sorted(allh, key=lambda x: (-day(x), x["id"]))
    rhist = [x for x in rich if not x.get("data")]
    sc.eq([x["id"] for x in rhist], [x["id"] for x in byday[:len(rhist)]],
          "gold rich: then the price-history ideas, the biggest day's profit first however fast they sell")
    sc.eq([x["fit"]["profit"] for x in rhist], [day(x) for x in byday[:len(rhist)]],
          "gold rich: a history idea makes a day's profit")
    sc.eq(w.ns.SidebarPanel.flipGold._text, "for your 200000g", "gold rich: FLIP IDEAS says the gold")
    slash_flips_ok(sc, w.slash("/gl flips"), ref_flips(r2, limit=10, gold=w.gold), r2, items, w.gold, "gold rich")
    w.close_ah_modern()
    sc.clean(w, "rich")

    # 2g: a is out of reach, 2g buys 2 of b's 20, c (all of it) now ranks first
    w = World(r2, items, data_src=text, gold=20000)
    w.open_ah_modern()
    SB = w.ns.SidebarPanel
    want, _ = sized_rows_ok(sc, w, r2, "gold 2g")
    data = [x for x in want if x.get("data")]
    sc.eq([(x["id"], x["kind"]) for x in data], [(c, "gap"), (b, "gap"), (d, "vendor")],
          "gold 2g: the dear flip left out, the rest ranked by what 2g makes")
    bx = next((x for x in data if x["id"] == b), None)
    if bx:
        sc.eq((bx["fit"]["units"], bx["fit"]["cost"], bx["fit"]["profit"], bx["fit"]["limited"]), (2, 20000, 6600, True),
              "gold 2g: 2 of 20 at the average price, costing 2g, making 66s")
        row = next(r for r in shown_rows(SB, "flipRows") if r.itemID == b)
        sc.eq((strip_codes(row.sub._text), strip_codes(row.right._text)), ("Buy up to 1g 10s (2 of 20)", "+66s"),
              "gold 2g: the row says 2 of 20 and the profit on those")
        w.M.enter(row)
        flat = [strip_codes(l or "") for l, _ in w.all_lines(w.g.GameTooltip)]
        sc.check("With your gold (2g): buy 2 of 20, costing 2g" in flat and "Profit in a day, after the 5% cut" in flat,
                 "gold 2g: the hover says what 2g buys, and its profit in a day (%s)" % flat[-12:])
        w.M.leave(row)
    hist = [x for x in want if not x.get("data")]
    sc.check(all(x["fit"]["cost"] <= 20000 for x in want), "gold 2g: every idea costs at most 2g")
    if R.SIM:
        sc.check(any(x["fit"]["limited"] for x in hist), "gold 2g: a history idea 2g buys only part of (%s)" % hist)
    out = w.slash("/gl flips")
    slash_flips_ok(sc, out, ref_flips(r2, limit=10, gold=20000), r2, items, 20000, "gold 2g")
    # the ideas stay put until something they depend on changes ...
    w.set_gold(5000)
    SB.Refresh(SB)
    sc.eq([r.itemID for r in shown_rows(SB, "flipRows")], [x["id"] for x in want], "gold: the ideas are kept until the gold changes")
    # ... like the gold: PLAYER_MONEY works them out again. 50s: b's gap is out of reach, its vendor flip shows
    w.M.fire("PLAYER_MONEY")
    w.settle(0.5)
    want50, _ = sized_rows_ok(sc, w, r2, "gold 50s")
    sc.eq([(x["id"], x["kind"]) for x in want50 if x.get("data")], [(d, "vendor"), (c, "gap"), (b, "vendor")],
          "gold 50s: d's vendor flip (83 of 200) makes more than 1 of c's 4, b's cheap vendor flip instead of its gap")
    cx = next((x for x in want50 if x["id"] == c), None)
    sc.check(cx is not None and (cx["fit"]["units"], cx["fit"]["cost"], cx["fit"]["profit"]) == (1, 3751, 2424),
             "gold 50s: 1 of c's 4 costs 37s 51c (rounded up), making 24s 24c (rounded down)")
    sc.eq(SB.flipGold._text, "for your 50s", "gold 50s: FLIP IDEAS says the new gold")
    # no gold at all: the sidebar and /gl flips say so
    w.set_gold(0)
    w.M.fire("PLAYER_MONEY")
    w.settle(0.5)
    want0, why0 = check_sidebar_flips(sc, w, r2, "gold 0")
    sc.eq((len(want0), why0), (0, "gold"), "gold 0: no ideas, because of the gold")
    sc.eq(SB.flipGold._text, "for your 0c", "gold 0: FLIP IDEAS says the gold")
    out = [strip_codes(o) for o in w.slash("/gl flips")]
    sc.eq(out, ["Goblin Ledger: not enough gold on this character for the current flip ideas (you have 0c)."], "gold 0: /gl flips says so")
    # a gold change while the auction house is closed is picked up when it opens again
    w.close_ah_modern()
    w.set_gold(RICH_GOLD)
    w.M.fire("PLAYER_MONEY")
    w.settle(0.5)
    w.open_ah_modern()
    check_sidebar_flips(sc, w, r2, "gold back up")
    w.close_ah_modern()
    sc.clean(w, "2g")
    # Too little gold for the collector's flips while the price history is still
    # being collected: the sidebar says it is the gold, not the history
    early = set_header(text, "salesHours", 1)
    r3 = RefData(text=early)
    w = World(r3, items, data_src=early, gold=30)
    w.open_ah_modern()
    sc.eq(ref_history_flips_why(r3, gold=30), ([], "history"), "gold 30c, early sales: the history ideas wait")
    want30, why30 = check_sidebar_flips(sc, w, r3, "gold 30c, early sales")
    sc.eq((len(want30), why30), (0, "gold"), "gold 30c, early sales: no ideas, because of the gold")
    w.close_ah_modern()
    sc.clean(w, "30c")
    # The legacy auction house knows PLAYER_MONEY too
    w = World(r2, items, data_src=text, client="legacy", gold=20000)
    sc.check(w.ns.SidebarPanel.eventFrame.IsEventRegistered(w.ns.SidebarPanel.eventFrame, "PLAYER_MONEY"),
             "gold: the sidebar listens for PLAYER_MONEY")
    sc.clean(w, "legacy")


def deal_data(ref, items):
    """(text, ref, ids): deals as the collector writes them (10 fields, the last the
    usual price) next to a gap flip with a usual price and an older 9-field one,
    each item selling every hour at its relist price (steady_series).
    Flips A (shown in the sidebar, best first): a deal the mock's 123g buys 2 of 3 of
    (thin), a deal on a version (suffix and slow), a deal the collector calls unsure
    on an item with only 2 listings, a deal on an item with 1 listing (the addon
    adds no unsure of its own), a deal with no risk (35% under its usual price).
    Flips B (another world): a deal with no usual price, one with a broken (negative)
    usual, a gap flip with a usual price, an older 9-field gap flip, and broken
    lines (skipped)."""
    # plain items (no versions: the collector gives those no deal, their versions get them)
    p = [i for i in other_items(ref, items, 60, avoid=SIDEBAR_WATCH) if i not in ref.variants][:6]
    vid = next((v for v in sorted(ref.base_of) if v in ref.items), None)
    if len(p) < 6 or vid is None:
        return None
    ids = {"big": p[0], "ver": vid, "both": p[1], "doubt": p[2], "clean": p[3],
           "nousual": p[0], "negusual": p[3], "gap10": p[4], "gap9": p[5], "broken": p[5]}

    def fn(iid, f):
        f = f + [""] * (10 - len(f))
        if iid == ids["both"]:
            f[9] = "2"           # 2 listings: the addon doubts the price (and says nothing in a flip)
        elif iid == ids["doubt"]:
            f[9] = "1"           # 1 listing
        elif iid in (ids["big"], ids["clean"], ids["gap10"], ids["gap9"]):
            if f[9] in ("1", "2"):
                f[9] = "5"       # no doubt about these prices
        return f
    base = rewrite_items(ref.text, fn)
    flips_a = [
        "%d|deal|520000|3|1500000|900000|1065000|710|thin|950000" % ids["big"],
        "%d|deal|20000|2|38000|33000|24700|650|suffix,slow|33000" % ids["ver"],
        "%d|deal|14000|1|14000|21000|5950|425|unsure|21000" % ids["both"],
        "%d|deal|7000|1|7000|12000|4400|628||12700" % ids["doubt"],    # 44.9% under: says 44%
        "%d|deal|6500|1|6500|9999|2999|461||10000" % ids["clean"],
    ]
    # no vendor price: no deposit, and no doubt about the prices but the ones made above
    series_a = {ids["big"]: steady_series(900000, vendor=0), ids["ver"]: steady_series(33000, vendor=0),
                ids["both"]: steady_series(21000, vendor=0), ids["doubt"]: steady_series(12000, vendor=0),
                ids["clean"]: steady_series(9999, vendor=0)}
    flips_b = [
        "%d|gap|900|10|8500|1400|4800|564||1500" % ids["gap10"],
        "%d|deal|5000|1|5000|8758|3320|664||-5" % ids["negusual"],    # the same profit as the next one:
        "%d|deal|3000|2|5800|4800|3320|572||" % ids["nousual"],       # they keep this order
        "%d|gap|600|10|5600|900|2950|526|slow" % ids["gap9"],
        "%d|deal|5000|1|5000|0|100|20||8000" % ids["broken"],      # no relist price: skipped
        "%d|deal|0|1|0|9000|100|20||8000" % ids["broken"],         # no buy price: skipped
        "%d|deal|5000|1|5000|9000|0|0||8000" % ids["broken"],      # no profit: skipped
        "%d|sniper|5000|1|5000|9000|100|20||8000" % ids["broken"],  # a kind this addon does not know: skipped
    ]
    series_b = {ids["gap10"]: steady_series(1400, vendor=0), ids["negusual"]: steady_series(8758, vendor=0),
                ids["nousual"]: steady_series(4800, vendor=0), ids["gap9"]: steady_series(900, vendor=0)}
    out = []
    for lines, series in ((flips_a, series_a), (flips_b, series_b)):
        text = set_table(with_series(base, series), "flips", [lua_quote(s) for s in lines])
        out.append((text, RefData(text=text)))
    return out, ids


def sc_flip_deals(sc, ref, items):
    """Deals: green, blue or epic items the collector found listed well under their
    usual price. Rows: Buy up to X (n), Relist R, +the profit within a day and the
    sales per day (never a risk note or how far under); the hover explains it with
    the usual price; /gl flips too. A deal is solid (it can take the kept place)
    unless the collector is unsure of its usual price; the addon's own doubt about
    today's listings adds nothing."""
    made = deal_data(ref, items)
    if not sc.check(made is not None, "deals: enough items and a version to test with"):
        return
    (text_a, ra), (text_b, rb) = made[0]
    ids = made[1]
    sc.eq([(f["id"], f["kind"], f["usual"], f["risk"]) for f in ra.flips],
          [(ids["big"], "deal", 950000, ["thin"]), (ids["ver"], "deal", 33000, ["suffix", "slow"]),
           (ids["both"], "deal", 21000, ["unsure"]), (ids["doubt"], "deal", 12700, []), (ids["clean"], "deal", 10000, [])],
          "deals: test data A made")
    sc.eq(deal_under({"usual": 12700, "buyMax": 7000}), 44, "deals: the model rounds the percent down")
    sc.check(price_doubt(ra, ids["both"]) and price_doubt(ra, ids["doubt"]) and not price_doubt(ra, ids["clean"]),
             "deals: the doubtful prices are made")
    w = World(ra, items, data_src=text_a)
    ns, M = w.ns, w.M
    sc.eq(data_flips_of(ns), ra.flips, "deals: DataFlips reads them with their usual price")
    w.open_ah_modern()
    SB = ns.SidebarPanel
    want, _ = check_sidebar_flips(sc, w, ra, "deals")
    keys = ("big", "ver", "both", "doubt", "clean")
    sc.eq([x["id"] for x in want], [ids[k] for k in keys], "deals: all five shown, the most within a day first")
    by = {x["id"]: x for x in want}
    rows = {r.itemID: r for r in shown_rows(SB, "flipRows")}
    sc.eq(by[ids["both"]]["risk"], ["unsure"], "deals: the collector's unsure is kept as it is")
    sc.eq(by[ids["doubt"]]["risk"], [], "deals: the addon adds no unsure of its own (1 listing today)")
    sc.eq([(by[ids[k]]["solid"], bool(rows[ids[k]].idea.solid)) for k in keys if ids[k] in rows],
          [(True, True), (True, True), (False, False), (True, True), (True, True)],
          "deals: solid (sold at the relist price) unless the collector is unsure")
    for key, sub, sub2, right in (
            ("big", "Buy up to 52g (2 of 3)", "Relist 90g", "+71g"),
            ("ver", "Buy up to 2g (2)", "Relist 3g 30s", "+2g 47s"),
            ("both", "Buy up to 1g 40s (1)", "Relist 2g 10s", "+59s 50c"),
            ("doubt", "Buy up to 70s (1)", "Relist 1g 20s", "+44s"),
            ("clean", "Buy up to 65s (1)", "Relist 99s 99c", "+29s 99c")):
        r = rows.get(ids[key])
        if sc.check(r is not None, "deals: a row for the %s deal" % key):
            sc.eq((strip_codes(r.sub._text), strip_codes(r.sub2._text), r.right2._text, strip_codes(r.right._text)),
                  (sub, sub2, "~61/day", right), "deals: the %s deal's row" % key)
    for x in want:
        data_flip_hover_ok(sc, w, rows[x["id"]], x, ra, "deals: hover %d" % x["id"])
    M.enter(rows[ids["clean"]])
    flat = [strip_codes(l or "") + " | " + strip_codes(r_ or "") for l, r_ in w.all_lines(w.g.GameTooltip)]
    sc.check("Listed well under its usual price (usual 1g): buy it and relist it at the price below, about what it "
             "has been selling for. | " in flat and "Relist at (each) | 99s 99c" in flat
             and "Usual price (last 7 days) | 1g" in flat and "Under the usual price by | 35% or more" in flat
             and "Your gold covers all of it. | " in flat and "Chance yours sells in a day | 100%" in flat,
             "deals: the clean deal's hover, by hand (%s)" % flat[-16:])
    M.leave(rows[ids["clean"]])
    M.enter(rows[ids["big"]])
    flat = [strip_codes(l or "") for l, _ in w.all_lines(w.g.GameTooltip)]
    sc.check("With your gold (123g 45s): buy 2 of 3, costing 100g" in flat and "Of your 2, likely to sell" in flat,
             "deals: the big deal's hover says what 123g buys (%s)" % flat[-12:])
    M.leave(rows[ids["big"]])
    M.enter(rows[ids["ver"]])
    flat = [strip_codes(l or "") for l, _ in w.all_lines(w.g.GameTooltip)]
    sc.check("Watch out" in flat and SUFFIX_TEXT in flat and SUFFIX_TEXT == ns.RISK_TEXT.suffix,
             "deals: the version's hover warns about versions (%s)" % flat[-6:])
    M.leave(rows[ids["ver"]])
    out = w.slash("/gl flips")
    ideas = ref_flips(ra, limit=10, gold=w.gold)
    slash_flips_ok(sc, out, ideas, ra, items, w.gold, "deals")
    s = [strip_codes(o) for o in out]
    nm = item_name(ra, items, ids["clean"])
    sc.check(any(o.endswith(". %s: buy up to 65s (1), relist 99s 99c, +29s 99c in a day (sells ~61/day)" % nm) for o in s),
             "deals: the clean deal's chat line, by hand (%s)" % s)
    w.close_ah_modern()
    sc.clean(w, "deals A")

    sc.eq([(f["id"], f["kind"], f["usual"]) for f in rb.flips],
          [(ids["gap10"], "gap", 1500), (ids["negusual"], "deal", 0), (ids["nousual"], "deal", 0), (ids["gap9"], "gap", 0)],
          "deals: test data B made (the broken deals and the unknown kind skipped)")
    w = World(rb, items, data_src=text_b)
    sc.eq(data_flips_of(w.ns), rb.flips, "deals B: DataFlips reads them (usual 0 when missing or broken)")
    w.open_ah_modern()
    want, _ = check_sidebar_flips(sc, w, rb, "deals B")
    sc.eq([x["id"] for x in want if x.get("data")], [ids["gap10"], ids["negusual"], ids["nousual"], ids["gap9"]],
          "deals B: all four shown (the two deals making the same keep the collector's order)")
    rows = {r.itemID: r for r in shown_rows(w.ns.SidebarPanel, "flipRows")}
    for key in ("nousual", "negusual"):
        r = rows.get(ids[key])
        if sc.check(r is not None, "deals B: a row for the %s deal" % key):
            sc.eq(r.right2._text, "~61/day", "deals B: the note is the sales per day (%s)" % key)
            flat = data_flip_hover_ok(sc, w, r, next(x for x in want if x["id"] == ids[key]), rb, "deals B: hover %s" % key)
            sc.check("Listed well under its usual price: buy it and relist it at the price below, about what it has "
                     "been selling for. | " in flat and not any(f.startswith("Usual price") for f in flat),
                     "deals B: the hover without a usual price (%s)" % key)
    r = rows.get(ids["gap10"])
    if r is not None:
        flat = data_flip_hover_ok(sc, w, r, next(x for x in want if x["id"] == ids["gap10"]), rb, "deals B: hover gap")
        sc.check(not any(f.startswith(("Usual price", "Listed well under")) for f in flat), "deals B: a gap flip says nothing about a usual price")
    slash_flips_ok(sc, w.slash("/gl flips"), ref_flips(rb, limit=10, gold=w.gold), rb, items, w.gold, "deals B")
    w.close_ah_modern()
    sc.clean(w, "deals B")


def flip_test_items(ref, items, n):
    """n plain items (no versions) with a name and prices to hang hand-made flip
    lines on: the ones no other part of a scenario uses first, then the watched
    ones (never the role items)."""
    first = [i for i in other_items(ref, items, 200, avoid=SIDEBAR_WATCH) if i not in ref.variants]
    more = [i for i in other_items(ref, items, 200) if i not in ref.variants and i not in first]
    return (first + more)[:n]


def no_doubt(text, ids):
    """The data with no price doubt on these items (5 separate listings, 10+ units
    listed): the addon would not doubt their prices."""
    def fn(iid, f):
        if iid not in ids:
            return None
        f = f + [""] * (10 - len(f))
        f[9] = "5"
        f[3] = str(max(int(f[3] or 0), 10))
        return f
    return rewrite_items(text, fn)


def relist_for(profit, cost):
    """A relist price (1 unit) making about `profit` after the 5% cut, and the exact profit."""
    relist = -(-(profit + cost) * 100 // 95)
    return relist, relist * 95 // 100 - cost


def deal_room_data(ref, items, deal_profit=9000):
    """(text, ref, gaps, deals): 9 gap flips making 6g down to 2g 80s (costing 1g to
    1g 40s), each more than 3 deals (making 90s, 80s and 70s, costing 1g 50s, 1g 20s
    and 90s; usual price 3g, or the relist price when that is more), 1 unit each,
    written best first like the collector writes them, each item selling every
    hour at its relist price (so a flip's profit within a day is its profit, and
    the deals are solid). deal_profit: the best deal's profit (50000 puts it among
    the gap flips)."""
    picks = flip_test_items(ref, items, 12)
    if len(picks) < 12:
        return None
    gaps, deals = picks[:9], picks[9:]
    lines, series = [], {}
    for k, iid in enumerate(gaps):
        cost = 10000 + 500 * k
        relist, profit = relist_for(60000 - 4000 * k, cost)
        lines.append((profit, "%d|gap|%d|1|%d|%d|%d|%d||" % (iid, cost, cost, relist, profit, profit * 1000 // cost)))
        series[iid] = steady_series(relist)
    for iid, cost, want in zip(deals, (15000, 12000, 9000), (deal_profit, 8000, 7000)):
        relist, profit = relist_for(want, cost)
        lines.append((profit, "%d|deal|%d|1|%d|%d|%d|%d||%d" % (iid, cost, cost, relist, profit, profit * 1000 // cost,
                                                              max(30000, relist))))
        series[iid] = steady_series(relist)
    lines.sort(key=lambda p: -p[0])
    text = set_table(with_series(ref.text, series), "flips", [lua_quote(s) for _, s in lines])
    return text, RefData(text=text), gaps, deals


def sc_flip_deal_room(sc, ref, items):
    """One place in every 5 flip ideas is kept for a solid deal (a green, blue or
    epic item listed well under its usual price, counted at its own relist price
    and without the collector's unsure), so one shows on every scan that has one:
    when bigger flips fill the list, the best solid deal the gold buys takes the
    sidebar's last row and the best two the last of the 10 /gl flips lines, in
    rank order. A hidden deal, or one the gold cannot buy, gives its place to the
    next one; a deal already among the best keeps its own place."""
    made = deal_room_data(ref, items)
    if not sc.check(made is not None, "deal room: enough items to test with"):
        return
    text, r2, g, d = made
    sc.eq([(f["id"], f["kind"]) for f in r2.flips], [(i, "gap") for i in g] + [(i, "deal") for i in d],
          "deal room: test data made (the gap flips all make more than the deals)")
    for gold in (MOCK_GOLD, None):
        lab = "deal room, %s" % ("123g" if gold else "no gold")
        w = World(r2, items, data_src=text, gold=gold)
        M = w.M
        w.open_ah_modern()
        SB = w.ns.SidebarPanel
        want, _ = sized_rows_ok(sc, w, r2, lab)
        sc.eq([x["id"] for x in want], g[:4] + d[:1], lab + ": the 4 best flips, then the best deal in the last row")
        sc.check(all(x["solid"] for x in want if x["kind"] == "deal"), lab + ": the deals are solid")
        rows = shown_rows(SB, "flipRows")
        if not sc.check(len(rows) == 5, lab + ": 5 rows"):
            w.close_ah_modern()
            continue
        sc.eq((rows[4].itemID, strip_codes(rows[4].sub._text), strip_codes(rows[4].sub2._text), rows[4].right2._text,
               strip_codes(rows[4].right._text)), (d[0], "Buy up to 1g 50s (1)", "Relist 2g 52s", "~61/day", "+90s"),
              lab + ": the deal's row, by hand")
        M.enter(rows[4])
        flat = [strip_codes(a or "") + " | " + strip_codes(b or "") for a, b in w.all_lines(w.g.GameTooltip)]
        sc.check("Listed well under its usual price (usual 3g): buy it and relist it at the price below, about what it "
                 "has been selling for. | " in flat, lab + ": the deal's hover says where to relist it (%s)" % flat[-12:])
        M.leave(rows[4])
        ideas = ref_flips(r2, limit=10, gold=gold)
        sc.eq([x["id"] for x in ideas], g[:8] + d[:2], lab + ": /gl flips keeps 2 of its 10 lines for the best deals")
        slash_flips_ok(sc, w.slash("/gl flips"), ideas, r2, items, gold, lab)
        # not interested in the best deal: the next one takes its place
        install_menu_mock(w)
        M.click(rows[4], "RightButton")
        menu_pick(w, "Not interested (hide for a day)")
        hid = ref_flips(r2, hidden={d[0]}, gold=gold)
        sc.eq([x["id"] for x in hid], g[:4] + d[1:2], lab + ", best deal hidden: the next deal takes the last row")
        sc.eq([r.itemID for r in shown_rows(SB, "flipRows")], [x["id"] for x in hid], lab + ", best deal hidden: the rows")
        ideas = ref_flips(r2, limit=10, hidden={d[0]}, gold=gold)
        sc.eq([x["id"] for x in ideas], g[:8] + d[1:3], lab + ", best deal hidden: /gl flips, the next two deals")
        slash_flips_ok(sc, w.slash("/gl flips"), ideas, r2, items, gold, lab + ", best deal hidden")
        w.close_ah_modern()
        sc.clean(w, lab)
    # 1g 30s: the best deal (1g 50s) and the two dearest gap flips are out of reach
    w = World(r2, items, data_src=text, gold=13000)
    w.open_ah_modern()
    want, _ = sized_rows_ok(sc, w, r2, "deal room, 1g 30s")
    sc.eq([x["id"] for x in want], g[:4] + d[1:2], "deal room, 1g 30s: the best deal 1g 30s buys takes the last row")
    ideas = ref_flips(r2, limit=10, gold=13000)
    sc.eq([x["id"] for x in ideas if x.get("data")], g[:7] + d[1:],
          "deal room, 1g 30s: /gl flips: every flip 1g 30s buys fits, each in its place")
    slash_flips_ok(sc, w.slash("/gl flips"), ideas, r2, items, 13000, "deal room, 1g 30s")
    w.close_ah_modern()
    sc.clean(w, "deal room, 1g 30s")
    # a deal that makes more than most of the gap flips keeps its own place
    text_b, rb, g, d = deal_room_data(ref, items, deal_profit=50000)
    w = World(rb, items, data_src=text_b)
    w.open_ah_modern()
    want, _ = sized_rows_ok(sc, w, rb, "deal room B")
    sc.eq([x["id"] for x in want], g[:3] + d[:1] + g[3:4], "deal room B: the deal in its own place, no other deal pulled in")
    ideas = ref_flips(rb, limit=10, gold=w.gold)
    sc.eq([x["id"] for x in ideas], g[:3] + d[:1] + g[3:8] + d[1:2], "deal room B: /gl flips: the next deal takes the last line")
    slash_flips_ok(sc, w.slash("/gl flips"), ideas, rb, items, w.gold, "deal room B")
    w.close_ah_modern()
    sc.clean(w, "deal room B")


def sc_flip_deal_words(sc, ref, items):
    """What a deal's hover says. Where to relist it: when the price it sold at backs
    its relist price (DAY_PRICE_CAP x that price reaches it), "at the price below,
    about what it has been selling for"; else that it has been selling for less,
    the lower price it is counted at and what you could ask. Neither the
    collector's "unsure" (its usual price may be one listing) nor the addon's own
    doubt about today's listings adds a line any more; the unsure deal is not
    solid. 1.25 x 7g 20s is exactly 9g (the full relist); 1.25 x 7g 19s 99c is
    8g 99s 98c, 1 copper under a relist of 8g 99s 99c."""
    picks = [i for i in flip_test_items(ref, items, 60) if ref.items[i]["vendor"] > 0][:5]
    if not sc.check(len(picks) == 5, "deal words: enough items with a vendor price to test with"):
        return
    far, own, nxt, at90, under90 = picks
    # far and own: 5 separate listings of 5 units, the market far over what a vendor pays
    big = {i: max(300 * ref.items[i]["vendor"], 100000) for i in (far, own)}
    series = {far: steady_series(big[far] - 1, vendor=ref.items[far]["vendor"]),
              own: steady_series(big[own], vendor=ref.items[own]["vendor"]),
              nxt: steady_series(28000, vendor=ref.items[nxt]["vendor"]),
              at90: steady_series(72000, vendor=ref.items[at90]["vendor"]),
              under90: steady_series(71999, vendor=ref.items[under90]["vendor"])}
    text = no_doubt(with_series(ref.text, series), [nxt, at90, under90])
    text = rewrite_items(text, lambda iid, f: (f[:9] + ["5"]) if iid in (far, own) else None)
    lines = [
        "%d|deal|%d|1|%d|%d|%d|%d||%d" % (far, big[far] // 2, big[far] // 2, big[far] - 1,
                                         (big[far] - 1) * 95 // 100 - big[far] // 2, 900, big[far]),
        "%d|deal|%d|1|%d|%d|%d|%d|unsure|%d" % (own, big[own] // 2, big[own] // 2, big[own],
                                               big[own] * 95 // 100 - big[own] // 2, 900, big[own]),
        # the collector's relist, just under the next listing (3g 70s), but it sold at 2g 80s
        "%d|deal|30000|1|30000|36999|5149|171|slow|100000" % nxt,
        "%d|deal|60000|1|60000|90000|25500|425||100000" % at90,
        "%d|deal|60000|1|60000|89999|25499|424||100000" % under90,
    ]
    text = set_table(text, "flips", [lua_quote(s) for s in lines])
    r2 = RefData(text=text)
    sc.check(price_doubt(r2, far) and price_doubt(r2, own) and r2.items[far]["auctions"] == 5
             and not any(price_doubt(r2, i) for i in (nxt, at90, under90)), "deal words: test data made")
    w = World(r2, items, data_src=text, gold=RICH_GOLD)
    w.open_ah_modern()
    want, _ = sized_rows_ok(sc, w, r2, "deal words")
    by = {x["id"]: x for x in want}
    sc.eq(sorted(by), sorted(picks), "deal words: all five deals shown")
    sc.eq([(by[i]["risk"], by[i]["countAt"], by[i]["solid"]) for i in picks],
          [([], big[far] - 1, True), (["unsure"], big[own], False), (["slow"], 35000, False), ([], 90000, True),
           ([], 89998, False)], "deal words: no unsure of the addon's own; counted at the relist only when sales back it")
    rows = {r.itemID: r for r in shown_rows(w.ns.SidebarPanel, "flipRows")}
    tips = {}
    for i in picks:
        w.M.enter(rows[i])
        tips[i] = [strip_codes(a or "") + " | " + strip_codes(b or "") for a, b in w.all_lines(w.g.GameTooltip)]
        w.M.leave(rows[i])
    backed = ": buy it and relist it at the price below, about what it has been selling for. | "
    less = ", but it has been selling for less: buy it and relist it at the price below. | "
    for i in (far, own):
        sc.check(("Listed well under its usual price (usual %s)%s" % (money_text(big[i]), backed)) in tips[i]
                 and not any(t.startswith(("Unsure", "Watch out")) or "asking price" in t or "guess" in t for t in tips[i]),
                 "deal words: %s: no unsure words, relist where it sells (%s)" % ("the addon's doubt" if i == far else
                                                                                 "the collector's unsure", tips[i][-14:]))
    sc.check("Listed well under its usual price (usual 10g)" + less in tips[nxt] and "Relist at (each) | 3g 50s" in tips[nxt]
             and "Selling lately for about | 2g 80s" in tips[nxt]
             and "You could ask up to 3g 69s 99c, but it has not been selling that high. | " in tips[nxt],
             "deal words: relist 3g 69s 99c that sold at 2g 80s: counted at 3g 50s (%s)" % tips[nxt][-16:])
    sc.check("Listed well under its usual price (usual 10g)" + backed in tips[at90] and "Relist at (each) | 9g" in tips[at90]
             and not any(t.startswith("You could ask") for t in tips[at90]),
             "deal words: sold at 7g 20s, 1.25x is the full 9g relist (%s)" % tips[at90][-14:])
    sc.check("Listed well under its usual price (usual 10g)" + less in tips[under90]
             and "Relist at (each) | 8g 99s 98c" in tips[under90]
             and "You could ask up to 8g 99s 99c, but it has not been selling that high. | " in tips[under90],
             "deal words: sold at 7g 19s 99c, 1 copper short of the relist: says so (%s)" % tips[under90][-14:])
    slash_flips_ok(sc, w.slash("/gl flips"), ref_flips(r2, limit=10, gold=w.gold), r2, items, w.gold, "deal words")
    w.close_ah_modern()
    sc.clean(w, "deal words")


def sc_flip_history_rank(sc, ref, items):
    """Sized to the gold, the price-history ideas rank by the profit on what the gold
    buys, however fast they sell (a slower seller's day of sales is already a
    smaller flip): a slower seller that makes more comes first. Not sized, the items
    selling 60+ a day still come first. A slower seller says so on hover."""
    base = drop_table(ref.text, "flips")      # only the price-history ideas
    r0 = RefData(text=base)
    found = None
    # an item selling 60+ a day, made to sell only 30 to 50: does it still out-earn a faster seller?
    # When the data has no such pair as it is, another fast seller is also made to sell just
    # over 60 a day (still a fast seller, but a smaller day's flip), so the case is built
    # from the data's own prices instead of hoping the data happens to have one.
    fast = [h for h in ref_history_flips_why(r0, limit=60, gold=MOCK_GOLD)[0] if not h["thin"]][:12]
    for y in [None] + fast:
        for x in fast:
            if found:
                break
            if y is not None and y["id"] == x["id"]:
                continue
            for n in (30, 40, 50):
                def fn(i, f, x=x, y=y, n=n):
                    if i == x["id"]:
                        return f[:4] + [str(n)] + f[5:]
                    if y is not None and i == y["id"]:
                        return f[:4] + [str(MIN_SOLD_FAST)] + f[5:]
                    return None
                t = rewrite_items(base, fn)
                r = RefData(text=t)
                hist = ref_history_flips_why(r, limit=5, gold=MOCK_GOLD)[0]     # the sidebar's 5 rows
                at = next((k for k, h in enumerate(hist) if h["id"] == x["id"]), None)
                if at is not None and hist[at]["thin"] and any(not h["thin"] for h in hist[at + 1:]):
                    found = (x["id"], n, t, r)
                    break
        if found:
            break
    if not found:
        sc.check(not R.SIM, "history rank: the simulated data has an item that out-earns a faster seller at 30 to 50 a day")
        return
    iid, n, t, r = found
    for gold in (MOCK_GOLD, None):
        lab = "history rank, %s, %s selling %d a day" % ("123g" if gold else "no gold", item_name(r, items, iid), n)
        w = World(r, items, data_src=t, gold=gold)
        w.open_ah_modern()
        want, _ = sized_rows_ok(sc, w, r, lab)
        res = w.ns.HistoryFlipIdeas(60, gold or False)
        hist = res[0] if isinstance(res, tuple) else res
        got = [(hist[k].id, bool(hist[k].thin), hist[k].fit.profit if gold else hist[k].perDay) for k in range(1, len(hist) + 1)]
        at = next((k for k, h in enumerate(got) if h[0] == iid), None)
        sc.check(at is not None and got[at][1], lab + ": a slower seller (%s)" % got)
        if gold:
            sc.check(at is not None and any(not h[1] for h in got[at + 1:]), lab + ": it comes before a faster seller that makes less")
            sc.eq([h[2] for h in got], sorted([h[2] for h in got], reverse=True), lab + ": best profit on your gold first")
            sc.check(iid in [x["id"] for x in want], lab + ": in the sidebar")
        else:
            flags = [h[1] for h in got]
            sc.check(flags == sorted(flags) and any(not f for f in flags),
                     lab + ": it and every slower seller after every item that sells 60+ a day (%s)" % got)
        w.close_ah_modern()
        sc.clean(w, lab)


def sc_flip_no_gold(sc, ref, items):
    """A client with no GetMoney (or one that gives no number): the flip ideas are
    not sized to any gold, exactly as before gold sizing: the same rows, profits
    and hovers, no gold next to FLIP IDEAS, the /gl flips header without gold."""
    text, r2, picks = gold_flip_data(ref, items)
    made = deal_data(ref, items)
    if not sc.check(text is not None and made is not None, "no gold: enough items to test with"):
        return
    for label, t, rr in (("gold flips", text, r2), ("deals", made[0][0][0], made[0][0][1]), ("own flips", ref.text, ref)):
        w = World(rr, items, data_src=t, gold=None)
        ns = w.ns
        sc.eq(ns.PlayerGold(), None, "no gold %s: PlayerGold says nothing" % label)
        w.open_ah_modern()
        want, why = sized_rows_ok(sc, w, rr, "no gold " + label)
        sc.check(all(x.get("fit") is None for x in want), "no gold %s: nothing is sized" % label)
        rows = shown_rows(ns.SidebarPanel, "flipRows")
        sc.check(all(r.idea.fit is None for r in rows), "no gold %s: the addon sizes nothing either" % label)
        sc.check(all(" of " not in strip_codes(r.sub._text) for r in rows), "no gold %s: no 'n of m'" % label)
        slash_flips_ok(sc, w.slash("/gl flips"), ref_flips(rr, limit=10, gold=None), rr, items, None, "no gold " + label)
        # PLAYER_MONEY changes nothing
        w.M.fire("PLAYER_MONEY")
        w.settle(0.5)
        check_sidebar_flips(sc, w, rr, "no gold %s, after PLAYER_MONEY" % label)
        w.close_ah_modern()
        sc.clean(w, "no gold " + label)
    # what GetMoney gives that is not a gold amount: no sizing
    w = World(r2, items, data_src=text)
    ns = w.ns
    for src, want in (("return 1234567", 1234567), ("return 0", 0), ("return 12.7", 12), ("return -1", None),
                      ("return 'lots'", None), ("return nil", None), ("error('no money today')", None),
                      ("return 0/0", None), ("return 1/0", None)):
        w.g.GetMoney = w.lua.eval("function() %s end" % src)
        sc.eq(ns.PlayerGold(), want, "PlayerGold when GetMoney does %s" % src)
    w.g.GetMoney = w.lua.eval("function() return 'lots' end")
    w.gold = None
    w.open_ah_modern()
    check_sidebar_flips(sc, w, r2, "GetMoney gives words")
    w.close_ah_modern()
    sc.clean(w, "GetMoney gives words")


# ---------------------------------------------------------------------------
# The day economics (DayStats, ExpectedSold, DayValue) on the ranking spec's
# hand-made hourly series, with salesHours 10 as the spec's numbers assume.
# Every number below is the spec's own (worked out there by hand and by its
# model); the scenarios check the addon and the reference model against them.
# ---------------------------------------------------------------------------
# A: listed at 1g, then climbing to 1g 40s, 15 sold over its last 6 hours (3 x a
# typical hour, 2.25, caps the 4 and the 5, so 10.5 count): sold at 1g 20s, its
# market price lately 1g 25s. C: listed at 22g, 1 sold, later 2 more, nothing
# listed for an hour and 15g now; a vendor pays 1g 26s 20c.
DAY_A = ([10000] * 5 + [11000, 12000, 12000, 13000, 13000, 14000], [0] * 5 + [1, 1, 2, 2, 4, 5], 15, 0)
DAY_C = ([220000] * 9 + [None, 150000], [0, 1] + [0] * 7 + [2, 0], 3, 12620)
DAY_FA = "%d|gap|9000|4|34000|20000|42000|1235|thin,slow|12500"         # on A: 4 for 3g 40s, relist 2g
DAY_FC = "%d|deal|150000|2|300000|220000|118000|393|thin,slow|220000"   # on C: 2 at 15g, relist 22g
DAY_FV = "%d|vendor|50|10|500|0|300|600||0"                             # 10 at 50c a vendor pays more for


def day_picks(ref, items):
    """Nine plain items (no versions) to hang the spec's series on: A, B, C, U, then five more."""
    p = [i for i in other_items(ref, items, 80, avoid=SIDEBAR_WATCH) if i not in ref.variants][:9]
    return p if len(p) == 9 else None


def day_data(ref, flips, series, sales_hours=10):
    """(text, ref): the data under test with these flip lines, these items' hourly
    series (with_series) and salesHours."""
    text = set_table(with_series(ref.text, series, sales_hours), "flips", [lua_quote(s) for s in flips])
    return text, RefData(text=text)


def lua_ideas(w, limit, gold):
    """ns.FlipIdeas(limit, gold) as a list (gold None: no sizing)."""
    res = w.ns.FlipIdeas(limit, gold if gold is not None else False)
    ideas = res[0] if isinstance(res, tuple) else res
    return [ideas[i] for i in range(1, len(ideas) + 1)]


def lua_list(t):
    return [t[i] for i in range(1, len(t) + 1)] if t is not None else []


def row_texts(r):
    """A flip row as (name, buy line, relist line, profit, note), without colour codes."""
    return (strip_codes(r.name._text), strip_codes(r.sub._text), strip_codes(r.sub2._text), strip_codes(r.right._text),
            strip_codes(r.right2._text))


def hover_flat(w, row):
    """The tooltip lines ("left | right") while the mouse is over a row."""
    w.M.enter(row)
    flat = [strip_codes(a or "") + " | " + strip_codes(b or "") for a, b in w.all_lines(w.g.GameTooltip)]
    w.M.leave(row)
    return flat


def flip_lines(flat):
    """The collector flip's own lines of a row's tooltip: from its title to the end
    of its lines (before the "Click" help)."""
    i = flat.index("Flip idea from the latest prices | ") if "Flip idea from the latest prices | " in flat else len(flat)
    out = []
    for f in flat[i:]:
        if f.startswith(("Click", "Right-click", "Shift-click")):
            break
        out.append(f)
    while out and out[-1] == "  | ":
        out.pop()
    return out


def sc_flip_day_stats(sc, ref, items):
    """DayStats on the spec's item A (salesHours 10): 15 sold in 6 hours, the 4 and
    the 5 capped at 2.25 (3 x a typical hour), so 10.5 count; sold at 1g 20s (the
    price half of them sold at or under, each hour's at the cheapest listing of
    the hour before); its last 6 market prices' median is 1g 25s. Never more than
    sold24; nothing while sales are not measured (salesHours 0). Two sale hours
    of the same weight: the lower price (1g, not 1g 20s). A sale hour with no
    price at all counts as sold but not in that median."""
    p = day_picks(ref, items)
    if not sc.check(p is not None, "day stats: enough items to test with"):
        return
    A = p[0]
    fit = {"units": 4, "cost": 34000, "profit": 42000}
    for label, series, hours, want in (
            ("sold24 15", DAY_A, 10, (10.5, 6, 12000, 12500)),
            ("sold24 8", DAY_A[:2] + (8, 0), 10, (8, 6, 12000, 12500)),
            ("salesHours 0", DAY_A, 0, None),
            ("two equal hours", ([10000] * 5 + [12000] * 6, [0] * 5 + [1] + [0] * 4 + [1], 2, 0), 10, (2, 2, 10000, 12000)),
            ("a sale hour with no price", ([10000] * 5 + [None, None] + [12000] * 4, [0] * 5 + [1, 1, 0, 0, 0, 1], 3, 0), 10,
             (3, 3, 10000, 12000))):
        lab = "day stats, " + label
        text, r2 = day_data(ref, [DAY_FA % A], {A: series}, hours)
        st = day_stats(r2, A)
        sc.eq(st and (st["S"], st["hours"], st["soldAt"], st["recent"]), want, lab + ": the model")
        w = World(r2, items, data_src=text, gold=None)
        f = w.ns.DataFlips()[1]
        res = w.ns.DayValue(f, w.to_lua(fit))
        if want is None:
            sc.eq(res, (None, "nosales"), lab + ": DayValue says no sales yet")
        elif sc.check(not isinstance(res, tuple), lab + ": DayValue keeps the flip (%s)" % (res,)):
            got = res.stats
            sc.eq((got.sold, got.hours, got.soldAt, got.recent), want, lab + ": DayValue's stats")
        sc.clean(w, lab)


def sc_flip_day_value(sc, ref, items):
    """DayValue on the spec's gap flip on A (4 for 3g 40s, buy up to 90s, relist 2g,
    usual 1g 25s): counted at 1g 50s (1.25 x the 1g 20s it sold at), one seller can
    expect 5.25 a day. All 4 bought (no gold API): 3.625171 likely to sell, +2g 8s
    44c in a day. 2g buys 2 of the 4 for 1g 70s, behind the 2 cheaper ones: 1.663216,
    +95s 63c. 80s buys none: not shown."""
    p = day_picks(ref, items)
    if not sc.check(p is not None, "day value: enough items to test with"):
        return
    A = p[0]
    text, r2 = day_data(ref, [DAY_FA % A], {A: DAY_A})
    for gold, want in ((None, (4, 34000, 15000, 12000, 5.25, 3.625171, 20844)),
                       (20000, (2, 17000, 15000, 12000, 5.25, 1.663216, 9563)),
                       (8000, None)):
        lab = "day value, %s" % (("gold " + money_text(gold)) if gold is not None else "no gold API")
        w = World(r2, items, data_src=text, gold=gold)
        mine = [x for x in ref_flips(r2, gold=gold) if x.get("data")]
        got = [x for x in lua_ideas(w, 5, gold) if x.data]
        if want is None:
            sc.eq((len(mine), len(got)), (0, 0), lab + ": none of it is affordable, so it is not shown")
        elif sc.check(len(got) == 1 and len(mine) == 1, lab + ": shown (%d, model %d)" % (len(got), len(mine))):
            x, m = got[0], mine[0]
            units, cost = (x.fit.units, x.fit.cost) if x.fit else (x.units, x.cost)
            sc.eq((units, cost, x.countAt, x.ref, x.rate, round(x.day, 6), x.dayProfit), want, lab + ": the addon's numbers")
            mu, mc = (m["fit"]["units"], m["fit"]["cost"]) if m["fit"] else (m["units"], m["cost"])
            sc.eq((mu, mc, m["countAt"], m["refPrice"], m["rate"], round(m["day"], 6), m["dayProfit"]), want,
                  lab + ": the model's numbers")
            sc.check(abs(x.day - want[5]) < 1e-6 and abs(x.dayValue - m["dayValue"]) < 1e-9,
                     lab + ": the expected sales to 1e-6 (%r), the value as the model's (%r, %r)" % (x.day, x.dayValue, m["dayValue"]))
        w.open_ah_modern()
        check_sidebar_flips(sc, w, r2, lab)
        w.close_ah_modern()
        sc.clean(w, lab)


def sc_flip_day_drops(sc, ref, items):
    """A gap or deal flip is left out (of the rows, /gl flips, and its item's
    price-history idea) when the dearest unit bought loses at the price it is
    counted at, when its sales show in only one hour or none, or when it makes
    under 20s in a day. Another collector flip of the same item (a vendor flip)
    is still judged and shows. The control, counted at 1.5 x its usual price of
    90s (1g 35s), shows with +1g 56s; a flip making exactly 20s in a day shows.
    Dropped items among the best price-history ideas cost no places: the next
    price-history ideas fill them."""
    p = day_picks(ref, items)
    if not sc.check(p is not None, "day drops: enough items to test with"):
        return
    A, B, M, D1, D2, D3, P, E = p[0], p[1], p[2], p[4], p[5], p[6], p[7], p[8]
    one_hour = (DAY_A[0], [0] * 10 + [15], 15, 0)
    no_sales = (DAY_A[0], [0] * 11, 0, 0)
    flips = [
        "%d|gap|14300|4|34000|20000|42000|1235|thin,slow|12500" % A,   # net at 1g 50s is 1g 42s 50c: not over 1g 43s
        DAY_FV % A,                                                     # the same item's vendor flip still counts
        "%d|gap|9000|4|34000|20000|42000|1235|thin,slow|6000" % B,      # 1.5 x 60s = 90s, after the cut 85s 50c
        DAY_FA % D1,                                                    # every sale in the last hour
        DAY_FA % D2,                                                    # no sales
        "%d|gap|9000|1|9000|10000|500|55|thin,slow|12500" % D3,         # 4s 97c in a day
        "%d|gap|9000|4|34000|20000|42000|1235|thin,slow|9000" % P,      # the control
        "%d|gap|14250|4|34000|20000|42000|1235|thin,slow|12500" % E,   # makes nothing on the dearest unit
        "%d|gap|1000|1|1000|3158|2000|2000||0" % M,                     # 1 that sells at once: 30s - 10s = 20s
    ]
    series = {A: DAY_A, B: DAY_A, D1: one_hour, D2: no_sales, D3: DAY_A, P: DAY_A, E: DAY_A, M: steady_series(3158, vendor=0)}
    text, r2 = day_data(ref, flips, series)
    want_why = {A: "loses", B: "loses", D1: "onehour", D2: "nosales", D3: "small", E: "loses"}
    # an item shown as a price-history idea, then given a collector flip that cannot pay
    hist = [x["id"] for x in ref_flips(r2, gold=None) if not x.get("data")]
    H = hist[0] if hist else None
    sc.check(H is not None or not R.SIM, "day drops: a price-history idea to drop with its item (%s)" % hist)
    if H is not None:
        hb = max(1, ref.items[H]["cheapest"], ref.items[H]["market"])
        flips.append("%d|gap|%d|1|%d|%d|1|1||0" % (H, hb, hb, hb))      # relist at the buy price: it cannot pay
        text, r2 = day_data(ref, flips, series)
    dropped = {}
    want, why = ref_flips_why(r2, gold=None, dropped=dropped)
    if H is not None:
        sc.check(dropped.get(H) in ("nosales", "onehour", "loses"), "day drops: the model drops %d (%s)" % (H, dropped.get(H)))
        want_why[H] = dropped.get(H)
    sc.eq(dropped, want_why, "day drops: the model drops each, for its reason")
    sc.eq([(x["id"], x["kind"]) for x in want if x.get("data")], [(P, "gap"), (M, "gap"), (A, "vendor")],
          "day drops: only the control, the 20s flip and the vendor flip of a dropped item are shown")
    w = World(r2, items, data_src=text, gold=None)
    for f in lua_list(w.ns.DataFlips()):
        if f.id in want_why and f.kind != "vendor":
            res = w.ns.DayValue(f, w.to_lua({"units": f.units, "cost": f.cost, "profit": f.profit}))
            sc.eq(res, (None, want_why[f.id]), "day drops: DayValue leaves out %d (%s)" % (f.id, want_why[f.id]))
    w.open_ah_modern()
    check_sidebar_flips(sc, w, r2, "day drops")
    rows = shown_rows(w.ns.SidebarPanel, "flipRows")
    gone = set(want_why) - {A}
    sc.check(not any(r.itemID in gone for r in rows), "day drops: no row for a dropped flip's item")
    ctrl = next((r for r in rows if r.itemID == P), None)
    if sc.check(ctrl is not None, "day drops: the control has a row"):
        sc.eq(row_texts(ctrl)[1:], ("Buy up to 90s (4)", "Relist 1g 35s", "+1g 56s", "~5/day"), "day drops: the control's row")
        sc.eq((ctrl.idea.countAt, ctrl.idea.dayProfit), (13500, 15678), "day drops: the control, at 1.5 x its usual price")
    vend = next((r for r in rows if r.itemID == A), None)
    if sc.check(vend is not None, "day drops: the dropped gap flip's item still shows its vendor flip"):
        sc.eq(row_texts(vend)[2:], ("Sell to vendor", "+3s", ""), "day drops: the vendor flip's row")
    edge = next((r for r in rows if r.itemID == M), None)
    if sc.check(edge is not None, "day drops: a flip making exactly 20s in a day shows"):
        sc.eq((edge.idea.dayValue, row_texts(edge)[3]), (2000, "+20s"), "day drops: the 20s flip's profit in a day")
    out = [strip_codes(o) for o in w.slash("/gl flips")]
    ideas = ref_flips(r2, limit=10, gold=None)
    slash_flips_ok(sc, w.slash("/gl flips"), ideas, r2, items, None, "day drops")
    names = [item_name(r2, items, i) for i in gone]
    sc.check(not any(n and (". " + n + ":") in o for n in names for o in out), "day drops: /gl flips names none of them")
    w.close_ah_modern()
    sc.clean(w, "day drops")
    # flips that cannot pay on the best price-history items (and no other collector
    # flip): every place still fills, with the next price-history ideas
    best = [x["id"] for x in ref_history_flips_why(ref, None, (), None)[0]]
    d = 2 if len(best) >= 4 else 1
    if not sc.check(len(best) > d, "day drops: price-history ideas to drop some of (%d)" % len(best)):
        return
    lines = []
    for i in best[:d]:
        hb = max(1, ref.items[i]["cheapest"], ref.items[i]["market"])
        lines.append("%d|gap|%d|1|%d|%d|1|1||0" % (i, hb, hb, hb))       # relist at the buy price: it cannot pay
    text = set_table(ref.text, "flips", [lua_quote(s) for s in lines])
    r2, rest = RefData(text=text), best[d:]
    lab = "day drops, %d of the best price-history items" % d
    w = World(r2, items, data_src=text, gold=None)
    for limit in sorted(set(min(k, len(rest)) for k in (2, 5, 10))):
        sc.eq([int(x.id) for x in lua_ideas(w, limit, None)], rest[:limit], "%s: FlipIdeas(%d) fills every place" % (lab, limit))
        sc.eq([x["id"] for x in ref_flips(r2, limit, gold=None)], rest[:limit], "%s: the model's %d" % (lab, limit))
    w.open_ah_modern()
    check_sidebar_flips(sc, w, r2, lab)
    w.close_ah_modern()
    sc.clean(w, lab)


def sc_flip_day_expected(sc, ref, items):
    """ExpectedSold (ns.ExpectedSold): the expected number of your n units sold in a
    day when buyers take a Poisson(rate) number from the cheapest up and `ahead`
    cheaper units go first; over 60 units (or a rate over 60) the plain rate less
    the units ahead, between 0 and n (0 when more are ahead than sell). The last
    pair pins that switch."""
    w = World(ref, items)
    for args, want in (((1, 1, 0), 0.632121), ((0.5, 1, 0), 0.393469), ((1, 1, 1), 0.264241), ((5.25, 4, 0), 3.625171),
                       ((5.25, 2, 2), 1.663216), ((70, 40, 60), 10), ((70, 5, 100), 0), ((30, 30, 30), 2.179035),
                       ((30, 31, 30), 0)):
        got = w.ns.ExpectedSold(*args)
        sc.check(abs(got - want) < 5e-7, "ExpectedSold%s = %.6f, want %s" % (args, got, want))
        sc.check(abs(expected_sold(*args) - want) < 5e-7, "the model's expected_sold%s = %.6f" % (args, expected_sold(*args)))
        sc.eq(got, expected_sold(*args), "ExpectedSold%s: the addon and the model agree exactly" % (args,))
    sc.clean(w)


def sc_flip_day_deals(sc, ref, items):
    """Deals on the spec's item C (2 sold in 2 hours at 22g, a vendor pays 1g 26s
    20c): relisted at 22g it is solid (sold there), one seller can expect 1 a day,
    0.896362 of the 2 likely to sell, each unsold loses a 37s 86c deposit: +4g 87s.
    Relisted at 30g it is counted at 27g 50s (1.25 x 22g) and is not solid. A deal
    the collector is unsure of is never solid."""
    p = day_picks(ref, items)
    if not sc.check(p is not None, "day deals: enough items to test with"):
        return
    C = p[2]
    for label, line, want, row in (
            ("solid", DAY_FC % C, (220000, True, 1, 0.896362, 3786, 48706),
             ("Buy up to 15g (2)", "Relist 22g", "+4g 87s", "~1/day")),
            ("capped", "%d|deal|150000|2|300000|300000|270000|900|thin,slow|300000" % C, (275000, False, 1, 0.896362, 3786, 95541),
             ("Buy up to 15g (2)", "Relist 27g 50s", "+9g 55s", "~1/day")),
            ("unsure", "%d|deal|150000|2|300000|220000|118000|393|thin,slow,unsure|220000" % C,
             (220000, False, 1, 0.896362, 3786, 48706), ("Buy up to 15g (2)", "Relist 22g", "+4g 87s", "~1/day"))):
        lab = "day deals, " + label
        text, r2 = day_data(ref, [line], {C: DAY_C})
        w = World(r2, items, data_src=text, gold=None)
        w.open_ah_modern()
        mine, _ = check_sidebar_flips(sc, w, r2, lab)
        rows = shown_rows(w.ns.SidebarPanel, "flipRows")
        if sc.check(rows and rows[0].itemID == C and mine and mine[0]["id"] == C, lab + ": shown first"):
            x, m = rows[0].idea, mine[0]
            sc.eq((x.countAt, bool(x.solid), x.rate, round(x.day, 6), round(x.deposit, 6), x.dayProfit), want,
                  lab + ": the addon's numbers")
            sc.eq((m["countAt"], m["solid"], m["rate"], round(m["day"], 6), round(m["deposit"], 6), m["dayProfit"]), want,
                  lab + ": the model's numbers")
            sc.eq(row_texts(rows[0])[1:], row, lab + ": the row")
        w.close_ah_modern()
        sc.clean(w, lab)


def sc_flip_day_place(sc, ref, items):
    """The kept deal place goes to a solid deal only. Five gap flips (+20g 84s down to
    +19g 39s in a day), an unsure deal U (+7g 55s) and the solid deal C (+4g 87s): the
    sidebar shows gaps 1 to 4, then C; U is left out. /gl flips (10): the 5 gaps, U,
    C, then price-history ideas. With C hidden the 5 gaps fill the sidebar, and with
    C gone from the data U is not given its place."""
    p = day_picks(ref, items)
    if not sc.check(p is not None, "deal place: enough items to test with"):
        return
    C, U, gaps = p[2], p[3], p[4:9]
    a10 = ([v * 10 for v in DAY_A[0]], DAY_A[1], 15, 0)
    gl = ["%d|gap|90000|4|%d|200000|%d|0|thin,slow|125000" % (g, cost, 4 * 190000 - cost)
          for g, cost in zip(gaps, (340000, 344000, 348000, 352000, 356000))]
    fu = "%d|deal|120000|2|240000|220000|178000|741|thin,slow,unsure|220000" % U
    series = dict((g, a10) for g in gaps)
    series[C] = series[U] = DAY_C
    text, r2 = day_data(ref, gl + [fu, DAY_FC % C], series)
    w = World(r2, items, data_src=text, gold=None)
    w.open_ah_modern()
    want, _ = check_sidebar_flips(sc, w, r2, "deal place")
    sc.eq([x["id"] for x in want], gaps[:4] + [C], "deal place: gaps 1 to 4, then the solid deal C (U is left out)")
    top = [(x["id"], x["dayProfit"], x["solid"]) for x in ref_collector_flips(r2, 10, gold=None)[0]]
    sc.eq(top, [(g, v, False) for g, v in zip(gaps, (208447, 204822, 201197, 197571, 193946))] + [(U, 75597, False), (C, 48706, True)],
          "deal place: the model's profits in a day, by hand")
    sc.eq([(int(x.id), x.dayProfit, bool(x.solid)) for x in lua_ideas(w, 10, None) if x.data], top, "deal place: the addon's")
    ideas = ref_flips(r2, limit=10, gold=None)
    sc.eq([x["id"] for x in ideas][:7], gaps + [U, C], "deal place: /gl flips, the 5 gaps, U, then C")
    sc.check(all(not x.get("data") for x in ideas[7:]), "deal place: then price-history ideas")
    slash_flips_ok(sc, w.slash("/gl flips"), ideas, r2, items, None, "deal place")
    w.ns.HideIdea(C)
    sc.eq([r.itemID for r in shown_rows(w.ns.SidebarPanel, "flipRows")], gaps, "deal place, C hidden: the 5 gaps")
    sc.eq([x["id"] for x in ref_flips(r2, hidden={C}, gold=None)], gaps, "deal place, C hidden: the model too")
    w.close_ah_modern()
    sc.clean(w, "deal place")
    text, r2 = day_data(ref, gl + [fu], series)
    w = World(r2, items, data_src=text, gold=None)
    w.open_ah_modern()
    want, _ = check_sidebar_flips(sc, w, r2, "deal place, no solid deal")
    sc.eq([x["id"] for x in want], gaps, "deal place, no solid deal: nothing is kept for the unsure one")
    w.close_ah_modern()
    sc.clean(w, "deal place, no solid deal")
    # ties: 25s buys 2 of Y's 61 behind 59 cheaper ones, 1.5 likely to sell, +30s in a day (no
    # vendor price, so no deposit); X and Z make +30s on 1 that sells at once; Q +30s on 60 of
    # its 100 (all bought). The same profit: the one that sells more of what you buy first (Y's
    # 1.5 of 2 before Q's 60 of 100, though Q sells more of its whole flip), then the collector's
    # order. Y2 is Y at 1999 for its 2: 3/4 of a copper more in a day, so it comes first.
    X, Y, Z, Q, Y2 = p[0], p[1], p[2], p[3], p[4]
    text, r2 = day_data(ref, ["%d|gap|25|100|2500|79|5000|2000||0" % Q, "%d|gap|1000|61|61000|3158|122000|2000||0" % Y,
                              "%d|gap|1000|1|1000|4211|3000|3000||0" % X, "%d|gap|1000|1|1000|4211|3000|3000||0" % Z,
                              "%d|gap|1000|61|60940|3158|122060|2002||0" % Y2],
                        {X: steady_series(4211, vendor=0), Y: steady_series(3158, vendor=0), Z: steady_series(4211, vendor=0),
                         Q: steady_series(79, per_hour=12, hours=10, vendor=0), Y2: steady_series(3158, vendor=0)})
    w = World(r2, items, data_src=text, gold=2500)
    w.open_ah_modern()
    want, _ = check_sidebar_flips(sc, w, r2, "ties")
    sc.eq([(x["id"], x["dayValue"], x["day"]) for x in want if x.get("data")],
          [(Y2, 3000.75, 1.5), (X, 3000, 1), (Z, 3000, 1), (Y, 3000, 1.5), (Q, 3000, 60)],
          "ties: Y2 first by 3/4 of a copper; then +30s each: the two that sell all they buy, in the collector's order, "
          "then 1.5 of 2, then 60 of 100")
    w.close_ah_modern()
    sc.clean(w, "ties")


def sc_flip_day_prices(sc, ref, items):
    """The price a flip is counted at: never over its relist or 1.25 x the price it
    sold at; a gap flip also never over 1.25 x its market price lately (a price
    that fell: sold at 2g, 1g 60s for the last 6 hours, so 2g) nor 1.5 x its usual
    price (1g 20s: 1g 80s). A deal keeps the price it sold at (2g 50s, the relist)
    whatever its market price lately or its usual price."""
    p = day_picks(ref, items)
    if not sc.check(p is not None, "day prices: enough items to test with"):
        return
    G1, D1, G2, D2 = p[0], p[1], p[2], p[3]
    fell = ([20000] * 18 + [16000] * 6, [0] * 7 + [3] * 11 + [0] * 6, 33, 0)
    lines = ["%d|gap|10000|1|10000|25000|13750|1375||0" % G1, "%d|deal|10000|1|10000|25000|13750|1375||30000" % D1,
             "%d|gap|10000|1|10000|25000|13750|1375||12000" % G2, "%d|deal|10000|1|10000|25000|13750|1375||12000" % D2]
    text, r2 = day_data(ref, lines, {G1: fell, D1: fell, G2: steady_series(20000), D2: steady_series(20000)})
    sc.eq([(day_stats(r2, i)["soldAt"], day_stats(r2, i)["recent"]) for i in (G1, G2)], [(20000, 16000), (20000, 20000)],
          "day prices: sold at 2g; lately 1g 60s, or 2g")
    w = World(r2, items, data_src=text, gold=None)
    w.open_ah_modern()
    check_sidebar_flips(sc, w, r2, "day prices")
    rows = {r.itemID: r for r in shown_rows(w.ns.SidebarPanel, "flipRows")}
    for iid, at, sub2, solid, what in ((G1, 20000, "Relist 2g", False, "a gap flip whose price fell: 1.25 x 1g 60s"),
                                       (D1, 25000, "Relist 2g 50s", True, "a deal: 1.25 x the 2g it sold at"),
                                       (G2, 18000, "Relist 1g 80s", False, "a gap flip: 1.5 x its usual 1g 20s"),
                                       (D2, 25000, "Relist 2g 50s", True, "a deal: its usual price does not cap it")):
        r = rows.get(iid)
        if sc.check(r is not None, "day prices: a row for %s" % what):
            sc.eq((r.idea.countAt, strip_codes(r.sub2._text), bool(r.idea.solid)), (at, sub2, solid), "day prices: " + what)
    w.close_ah_modern()
    sc.clean(w, "day prices")


def sc_flip_day_rows(sc, ref, items):
    """A collector flip's row: "Buy up to X (n)" (or "k of n"), "Relist <the price its
    profit is counted at>", "+<profit within a day>" and how many one seller can
    expect to sell a day ("~5/day", "<1/day"); a vendor flip: "Sell to vendor", its
    profit and no note. Never a risk count or how far under its usual price."""
    p = day_picks(ref, items)
    if not sc.check(p is not None, "day rows: enough items to test with"):
        return
    A, C, V = p[0], p[2], p[5]
    text, r2 = day_data(ref, [DAY_FA % A, DAY_FC % C, DAY_FV % V], {A: DAY_A, C: DAY_C[:2] + (1, DAY_C[3])})
    for gold, want in ((None, [(C, "Buy up to 15g (2)", "Relist 22g", "+2g 27s", "<1/day"),
                               (A, "Buy up to 90s (4)", "Relist 1g 50s", "+2g 8s", "~5/day"),
                               (V, "Buy up to 50c (10)", "Sell to vendor", "+3s", "")]),
                       (20000, [(A, "Buy up to 90s (2 of 4)", "Relist 1g 50s", "+95s 63c", "~5/day"),
                                (V, "Buy up to 50c (10)", "Sell to vendor", "+3s", "")])):
        lab = "day rows, %s" % (("gold " + money_text(gold)) if gold is not None else "no gold API")
        w = World(r2, items, data_src=text, gold=gold)
        w.open_ah_modern()
        check_sidebar_flips(sc, w, r2, lab)
        rows = shown_rows(w.ns.SidebarPanel, "flipRows")
        data = [r for r in rows if r.idea.data]
        sc.eq([(r.itemID,) + row_texts(r)[1:] for r in data], want, lab + ": the rows, by hand")
        sc.check(not any("risk" in strip_codes(r.right2._text) or re.search(r"-\d+%", strip_codes(r.right2._text))
                         for r in rows), lab + ": no risk note and no percent under on a row")
        c = next((r.idea for r in data if r.itemID == C), None)
        if c is not None:
            sc.eq((c.rate, c.dayProfit), (0.5, 22795), lab + ": C selling 1 a day: one seller 0.5 a day, +2g 27s 95c")
        w.close_ah_modern()
        sc.clean(w, lab)


def sc_flip_day_hover(sc, ref, items):
    """A collector flip's hover, by hand: what to do, the price it is counted at and
    what it has been selling for (and what you could ask), what your gold buys, then
    "Within a day": how many sell a day, how many of yours (or the chance yours
    does; "under 1" for a slow seller), the profit in a day, the deposit at risk
    when the unsold ones could cost 10s or more, and that the numbers are early
    while under 24 hours are measured. A risk is said only for gear versions."""
    p = day_picks(ref, items)
    if not sc.check(p is not None, "day hover: enough items to test with"):
        return
    A, B, C = p[0], p[1], p[2]
    text, r2 = day_data(ref, [DAY_FA % A], {A: DAY_A})
    w = World(r2, items, data_src=text, gold=20000)
    w.open_ah_modern()
    want, _ = check_sidebar_flips(sc, w, r2, "day hover, 2g")
    rows = shown_rows(w.ns.SidebarPanel, "flipRows")
    if sc.check(rows and rows[0].itemID == A, "day hover, 2g: A's row"):
        flat = hover_flat(w, rows[0])
        sc.eq(flip_lines(flat), [
            "Flip idea from the latest prices | ",
            "Buy the cheap listings, then relist them at about what it has been selling for. | ",
            "Buy up to (each) | 90s",
            "That buys 4, costing | 3g 40s",
            "Relist at (each) | 1g 50s",
            "Selling lately for about | 1g 20s",
            "You could ask up to 2g, but it has not been selling that high. | ",
            "With your gold (2g): buy 2 of 4, costing 1g 70s | ",
            "  | ",
            "Within a day | ",
            "You can expect to sell | ~5 a day",
            "Of your 2, likely to sell | ~2",
            "Profit in a day, after the 5% cut | +95s 63c",
            "Sales measured for 10 hours so far, so these are early numbers. | "], "day hover, 2g: A's lines, by hand")
        data_flip_hover_ok(sc, w, rows[0], want[0], r2, "day hover, 2g")
    w.close_ah_modern()
    sc.clean(w, "day hover, 2g")
    # a full day measured: no early-numbers line; 3 x a typical hour (now 0.1) caps every hour at 1
    text, r2 = day_data(ref, [DAY_FA % A], {A: DAY_A}, 24)
    w = World(r2, items, data_src=text, gold=20000)
    w.open_ah_modern()
    want, _ = check_sidebar_flips(sc, w, r2, "day hover, 24 hours")
    rows = shown_rows(w.ns.SidebarPanel, "flipRows")
    if sc.check(rows and rows[0].itemID == A, "day hover, 24 hours: A's row"):
        x = rows[0].idea
        sc.eq((x.stats.sold, x.rate, x.dayProfit), (6, 3, 5345), "day hover, 24 hours: 6 count, 3 a day, +53s 45c")
        flat = hover_flat(w, rows[0])
        sc.check(not any(f.startswith("Sales measured") for f in flat), "day hover, 24 hours: no early-numbers line")
        data_flip_hover_ok(sc, w, rows[0], want[0], r2, "day hover, 24 hours")
    w.close_ah_modern()
    sc.clean(w, "day hover, 24 hours")
    # deal C: the deposit at risk; a 1-unit deal on C: the chance it sells, no deposit line
    for label, line, dep, chance, profit in (
            ("deal C", DAY_FC % C, True, None, 48706),
            ("1-unit deal C", "%d|deal|150000|1|150000|220000|59000|393|thin,slow|220000" % C, False, "63%", 35902)):
        lab = "day hover, " + label
        text, r2 = day_data(ref, [line], {C: DAY_C})
        w = World(r2, items, data_src=text, gold=None)
        w.open_ah_modern()
        want, _ = check_sidebar_flips(sc, w, r2, lab)
        rows = shown_rows(w.ns.SidebarPanel, "flipRows")
        if sc.check(rows and rows[0].itemID == C, lab + ": C's row"):
            flat = flip_lines(hover_flat(w, rows[0]))
            sc.eq("The rest may take longer. Each one that expires unsold loses its deposit (about 37s 86c). | " in flat, dep,
                  lab + ": the deposit line (%s)" % flat[-6:])
            if chance:
                sc.check(("Chance yours sells in a day | " + chance) in flat, lab + ": the chance it sells (%s)" % flat[-6:])
            sc.eq(rows[0].idea.dayProfit, profit, lab + ": profit in a day")
            data_flip_hover_ok(sc, w, rows[0], want[0], r2, lab)
        w.close_ah_modern()
        sc.clean(w, lab)
    # a slow seller (C selling 1 a day, one seller 0.5): under 1 a day, and of your 2 under 1
    # (0.48) likely to sell, not "~0" next to the profit
    text, r2 = day_data(ref, [DAY_FC % C], {C: DAY_C[:2] + (1, DAY_C[3])})
    w = World(r2, items, data_src=text, gold=None)
    w.open_ah_modern()
    want, _ = check_sidebar_flips(sc, w, r2, "day hover, slow seller")
    rows = shown_rows(w.ns.SidebarPanel, "flipRows")
    if sc.check(rows and rows[0].itemID == C, "day hover, slow seller: C's row"):
        sc.eq(flip_lines(hover_flat(w, rows[0]))[-6:], [
            "Within a day | ",
            "You can expect to sell | under 1 a day",
            "Of your 2, likely to sell | under 1",
            "Profit in a day, after the 5% cut | +2g 27s 95c",
            "The rest may take longer. Each one that expires unsold loses its deposit (about 37s 86c). | ",
            "Sales measured for 10 hours so far, so these are early numbers. | "], "day hover, slow seller: its lines, by hand")
        data_flip_hover_ok(sc, w, rows[0], want[0], r2, "day hover, slow seller")
    w.close_ah_modern()
    sc.clean(w, "day hover, slow seller")
    # the deposit line needs 10s or more at risk: 1.103638 unsold x 9s 90c says so, x 8s 40c does not
    text, r2 = day_data(ref, [DAY_FC % B, DAY_FC % C], {B: DAY_C[:3] + (3300,), C: DAY_C[:3] + (2800,)})
    w = World(r2, items, data_src=text, gold=None)
    w.open_ah_modern()
    check_sidebar_flips(sc, w, r2, "day hover, deposits")
    rows = {r.itemID: r for r in shown_rows(w.ns.SidebarPanel, "flipRows")}
    if sc.check(B in rows and C in rows, "day hover, deposits: both rows"):
        sc.check("The rest may take longer. Each one that expires unsold loses its deposit (about 9s 90c). | "
                 in flip_lines(hover_flat(w, rows[B])), "day hover, deposits: 10s 93c at risk is said")
        sc.check(not any(f.startswith("The rest may take longer") for f in flip_lines(hover_flat(w, rows[C]))),
                 "day hover, deposits: 9s 27c at risk is not")
    w.close_ah_modern()
    sc.clean(w, "day hover, deposits")
    # early numbers while under 24 hours of sales are measured ("hour" for 1)
    for hours, line in ((1, "Sales measured for 1 hour so far, so these are early numbers. | "),
                        (23, "Sales measured for 23 hours so far, so these are early numbers. | ")):
        text, r2 = day_data(ref, [DAY_FA % A], {A: DAY_A}, hours)
        w = World(r2, items, data_src=text, gold=None)
        w.open_ah_modern()
        want, _ = check_sidebar_flips(sc, w, r2, "day hover, %d hours" % hours)
        rows = shown_rows(w.ns.SidebarPanel, "flipRows")
        if sc.check(rows and rows[0].itemID == A, "day hover, %d hours: A's row" % hours):
            sc.check(line in flip_lines(hover_flat(w, rows[0])), "day hover, %d hours: %s" % (hours, line))
            data_flip_hover_ok(sc, w, rows[0], want[0], r2, "day hover, %d hours" % hours)
        w.close_ah_modern()
        sc.clean(w, "day hover, %d hours" % hours)
    # the chance is rounded: 1 sold in each of 3 hours, 1.5 a day for one seller: 77.69%, said 78%
    text, r2 = day_data(ref, ["%d|deal|150000|1|150000|220000|59000|393||220000" % C],
                        {C: ([220000] * 11, [0] * 8 + [1, 1, 1], 3, 0)})
    w = World(r2, items, data_src=text, gold=None)
    w.open_ah_modern()
    check_sidebar_flips(sc, w, r2, "day hover, 1.5 a day")
    rows = shown_rows(w.ns.SidebarPanel, "flipRows")
    if sc.check(rows and rows[0].itemID == C, "day hover, 1.5 a day: C's row"):
        sc.check("Chance yours sells in a day | 78%" in flip_lines(hover_flat(w, rows[0])), "day hover, 1.5 a day: 78%")
    w.close_ah_modern()
    sc.clean(w, "day hover, 1.5 a day")
    # risk words: only "suffix" (gear versions) is said
    text, r2 = day_data(ref, ["%d|gap|9000|4|34000|20000|42000|1235|suffix|12500" % A,
                              "%d|gap|9000|4|34000|20000|42000|1235|thin,slow,spike,unsure|12500" % B], {A: DAY_A, B: DAY_A})
    w = World(r2, items, data_src=text, gold=None)
    w.open_ah_modern()
    want, _ = check_sidebar_flips(sc, w, r2, "day hover, risks")
    rows = {r.itemID: r for r in shown_rows(w.ns.SidebarPanel, "flipRows")}
    sc.eq(w.ns.RISK_TEXT.suffix, SUFFIX_TEXT, "day hover: the versions words")
    if sc.check(A in rows and B in rows, "day hover, risks: both rows"):
        fa, fb = flip_lines(hover_flat(w, rows[A])), flip_lines(hover_flat(w, rows[B]))
        sc.eq(fa[-3:], ["  | ", "Watch out | ", SUFFIX_TEXT + " | "], "day hover, risks: suffix says Watch out and why")
        sc.check(not any(f.startswith(("Watch out",) + GONE_FLIP_LINES) for f in fb),
                 "day hover, risks: thin, slow, spike and unsure say nothing (%s)" % fb[-4:])
        for x in want:
            if x.get("data"):
                data_flip_hover_ok(sc, w, rows[x["id"]], x, r2, "day hover, risks %d" % x["id"])
    w.close_ah_modern()
    sc.clean(w, "day hover, risks")


def sc_flip_day_slash(sc, ref, items):
    """/gl flips: "most profit within a day first" in the header; a gap or deal line
    says where to relist (the price counted at), the profit in a day and the sales a
    day ("sells under 1 a day" under one); a vendor line its profit on what the gold
    buys. No risks and no usual price in any line; price-history lines as before."""
    p = day_picks(ref, items)
    if not sc.check(p is not None, "day slash: enough items to test with"):
        return
    A, C, V, V2 = p[0], p[2], p[5], p[6]
    text, r2 = day_data(ref, [DAY_FA % A, DAY_FC % C, DAY_FV % V, "%d|vendor|5000|10|50000|0|3000|600||0" % V2],
                        {A: DAY_A, C: DAY_C[:2] + (1, DAY_C[3])})
    na, nc, nv, nv2 = (item_name(r2, items, i) for i in (A, C, V, V2))
    for gold, head, lines in (
            (20000, "flip ideas sized to your gold (2g), most profit within a day first, after the 5% auction cut:",
             ["%s: buy up to 90s (2 of 4), relist 1g 50s, +95s 63c in a day (sells ~5/day)" % na,
              "%s: buy up to 50s (4 of 10), sell to vendor, +12s" % nv2,
              "%s: buy up to 50c (10), sell to vendor, +3s" % nv]),
            (None, "flip ideas, most profit within a day first (after the 5% auction cut):",
             ["%s: buy up to 15g (2), relist 22g, +2g 27s 95c in a day (sells under 1 a day)" % nc,
              "%s: buy up to 90s (4), relist 1g 50s, +2g 8s 44c in a day (sells ~5/day)" % na,
              "%s: buy up to 50s (10), sell to vendor, +30s" % nv2,
              "%s: buy up to 50c (10), sell to vendor, +3s" % nv])):
        lab = "day slash, %s" % (("gold " + money_text(gold)) if gold is not None else "no gold API")
        w = World(r2, items, data_src=text, gold=gold)
        out = [strip_codes(o).replace("Goblin Ledger: ", "", 1) for o in w.slash("/gl flips")]
        sc.eq(out[0] if out else None, head, lab + ": the header")
        sc.eq(slash_head(gold), head, lab + ": the model's header")
        sc.eq(out[1:1 + len(lines)], ["  %d. %s" % (i, s) for i, s in enumerate(lines, 1)], lab + ": the collector's lines, by hand")
        sc.check(not any("(risk:" in o or "(usual" in o for o in out), lab + ": no risks and no usual price")
        slash_flips_ok(sc, w.slash("/gl flips"), ref_flips(r2, limit=10, gold=gold), r2, items, gold, lab)
        sc.clean(w, lab)


def flip_help(w):
    """The FLIP IDEAS title's hover (the help tip): its lines, or None."""
    SB, F = w.ns.SidebarPanel, w.g.ForeverAHSidebar
    for c in lua_list(F._children):
        pts = c._points
        if c._scripts is not None and pts is not None and len(pts) and w.same(pts[1][2], SB.flipTitle):
            w.M.enter(c)
            lines = [strip_codes(a or "") for a, _ in w.all_lines(w.g.GameTooltip)]
            w.M.leave(c)
            return lines
    return None


FLIP_HELP = ("Flips your gold can buy now, ranked by the profit you can expect within a day: only as many as have "
             "really been selling count, at about the price they have really been selling for. The best green, blue "
             "or epic listed well under the price it really sells for keeps a row. Then items to buy under the Target "
             "Buy price and sell at the sell price, from the price history. What your gold cannot afford is left out, "
             "and a row says how many you can buy. Profits are after the 5% auction cut. Prices move and sales are "
             "estimates, so treat these as ideas, not promises. Hover a row for the details.")


def sc_flip_day_first_snapshot(sc, ref, items):
    """No sales measured yet (salesHours 0, a first snapshot or one after a break):
    the collector's gap and deal flips are left out, its vendor flips still show.
    With no idea at all the sidebar and /gl flips say price history is still being
    collected, whatever the gold (not enough gold only for a flip that would show
    with more of it). The words for no ideas and the help tip say "within a day"."""
    p = day_picks(ref, items)
    if not sc.check(p is not None, "first snapshot: enough items to test with"):
        return
    A, C, V, V2 = p[0], p[2], p[5], p[6]
    series = {A: DAY_A, C: DAY_C}
    text, r2 = day_data(ref, [DAY_FA % A, DAY_FC % C, DAY_FV % V], series, 0)
    w = World(r2, items, data_src=text, gold=None)
    w.open_ah_modern()
    want, _ = check_sidebar_flips(sc, w, r2, "salesHours 0")
    sc.eq([(x["id"], x["kind"]) for x in want], [(V, "vendor")], "salesHours 0: only the vendor flip")
    sc.eq(w.ns.FLIP_MESSAGES.none, "No flips likely to sell within a day right now. Check back after the next update.",
          "the no-ideas words")
    sc.eq(FLIP_MESSAGES["none"], w.ns.FLIP_MESSAGES.none, "the model's no-ideas words")
    sc.eq(flip_help(w), ["Flip ideas", FLIP_HELP], "the FLIP IDEAS help tip")
    sc.check(FLIP_HELP.find("ranked by the profit you can expect within a day") > 0, "the help tip says how they rank")
    w.close_ah_modern()
    sc.clean(w, "salesHours 0")
    # 2g buys 2 of A's 4 and none of C's deal: neither could show with any gold yet,
    # so it is still the history being collected, not a lack of gold
    text, r2 = day_data(ref, [DAY_FA % A, DAY_FC % C], series, 0)
    for gold in (None, 20000):
        lab = "salesHours 0, no vendor flip, %s" % (("gold " + money_text(gold)) if gold is not None else "no gold API")
        w = World(r2, items, data_src=text, gold=gold)
        w.open_ah_modern()
        want, why = check_sidebar_flips(sc, w, r2, lab)
        sc.eq((len(want), why), (0, "history"), lab + ": no ideas, history is being collected")
        sc.eq(w.ns.SidebarPanel.flipMsg._text, FLIP_MESSAGES["history"], lab + ": the sidebar says so")
        out = w.slash("/gl flips")
        sc.check(any(FLIP_SLASH["history"] in o for o in out), lab + ": /gl flips says so (%s)" % out)
        w.close_ah_modern()
        sc.clean(w, lab)
    # a vendor flip more gold would buy (10 at 50s, with 40s on hand) is a lack of gold
    text, r2 = day_data(ref, [DAY_FA % A, DAY_FC % C, "%d|vendor|5000|10|50000|0|3000|600||0" % V2], series, 0)
    w = World(r2, items, data_src=text, gold=4000)
    w.open_ah_modern()
    want, why = check_sidebar_flips(sc, w, r2, "salesHours 0, 40s")
    sc.eq((len(want), why, w.ns.SidebarPanel.flipMsg._text), (0, "gold", FLIP_MESSAGES["gold"]),
          "salesHours 0, 40s: not enough gold for the vendor flip")
    w.close_ah_modern()
    sc.clean(w, "salesHours 0, 40s")


def sc_flip_day_model(sc, ref, items):
    """The reference model (day_value and ref_collector_flips, ported from the
    ranking spec's own model, not from the Lua) gives the same collector ideas as
    ns.FlipIdeas on the data under test: the same ids in the same order, the same
    profit in a day (whole copper), price counted at and solid deals, for 1g to
    5000g and with no sizing, at the sidebar's 5 and /gl's 10: 30 of 30."""
    w = World(ref, items)
    same = cases = shown = 0
    for g in (1, 3, 5, 10, 20, 35, 50, 75, 100, 150, 300, 600, 1000, 5000, None):
        gold = g * 10000 if g is not None else None
        for limit in (5, 10):
            cases += 1
            got = [(int(x.id), int(x.dayProfit), int(x.countAt), bool(x.solid)) for x in lua_ideas(w, limit, gold) if x.data]
            mine = [(x["id"], x["dayProfit"], x["countAt"], x["solid"]) for x in ref_collector_flips(ref, limit, gold=gold)[0]]
            same += sc.eq(got, mine, "model: %s, limit %d" % (("%dg" % g) if g else "no gold API", limit))
            shown += len(got)
    sc.eq(same, cases, "model: the same collector ideas in %d of %d cases" % (same, cases))
    sc.check(shown > 0 or not ref.flips, "model: some collector ideas to compare (%d)" % shown)
    sc.clean(w)


# The live snapshot the ranking spec was checked on (2026-11-18 19:25 UTC,
# salesHours 10): its exact rows, and the flips it names that never show.
LIVE_SNAPSHOT = 1795029903
LIVE_ROWS = {
    10: [("Draught of Water Walking", "Buy up to 19s 99c (62 of 287)", "Relist 22s 49c", "+3g 33s", "~662/day"),
         ("Jasper Link of the Bandit", "Buy up to 1g 99s (2)", "Relist 3g 67s", "+1g 10s", "~1/day"),
         ("Pattern: Blue Suede Hat", "Buy up to 2g 11s (4 of 7)", "Relist 2g 64s", "+82s 33c", "~7/day"),
         ("The Arcanist's Cookbook", "Buy up to 7g (1 of 2)", "Relist 10g", "+72s 66c", "~1/day"),
         ("Formula: Enchant Shield - Lesser Block", "Buy up to 8g 90s (1 of 5)", "Relist 10g 7s", "+71s 14c", "~5/day")],
    50: [("Draught of Water Walking", "Buy up to 19s 99c (287)", "Relist 22s 49c", "+15g 43s", "~662/day"),
         ("Twig of the World Tree", "Buy up to 21g 49s (2 of 3)", "Relist 26g 86s", "+10g 49s", "~4/day"),
         ("Steaming Stag Steak", "Buy up to 19s 95c (256 of 514)", "Relist 24s 91c", "+8g 59s", "~466/day"),
         ("Formula: Enchant Shield - Lesser Block", "Buy up to 8g 90s (5)", "Relist 10g 7s", "+5g 31s", "~5/day"),
         ("Blessed Plate Bracers", "Buy up to 15g (2)", "Relist 22g", "+4g 87s", "~1/day")],
    300: [("Elemental Mage Staff", "Buy up to 220g (1)", "Relist 280g", "+45g 7s", "~4/day"),
          ("Harnessing Shadows", "Buy up to 100g (1)", "Relist 160g", "+44g 92s", "~2/day"),
          ("Ace of Beasts", "Buy up to 77g (3)", "Relist 93g 99s", "+30g 72s", "~4/day"),
          ("Libram of Voracity", "Buy up to 22g (8)", "Relist 27g 50s", "+24g 99s", "~5/day"),
          ("Blessed Plate Bracers", "Buy up to 15g (2)", "Relist 22g", "+4g 87s", "~1/day")],
}
LIVE_NEVER = ("Magus Long Staff of the Physician", "Rockscale Cod", "Glowstar Rod of the Prophet", "Razor's Edge",
              "Demon Blade of the Sorcerer", "Meaty Bat Wing", "Wintersbite", "Torbernite", "Sharpening Wheel",
              "Aquadynamic Fish Attractor", "Snickerfang Jowl", "Engraved Helm of the Bandit", "Wanderer's Belt of the Bandit")
LIVE_GOLDS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 17, 20, 25, 30, 35, 40, 45, 50, 60, 75, 90, 100, 120, 150, 175, 200,
              250, 300, 350, 400, 500, 600, 750, 1000, 1500, 2000, 3000, 4000, 5000)


def sc_flip_day_live(sc, ref, items):
    """Live data only (--data/--items): every collector idea shown with 1g to 5000g
    (the sidebar's 5 and /gl's 10) has sales in 2 or more hours, is counted at no
    more than its relist, 1.25 x the price it sold at (and 1.5 x the usual price
    for a gap flip), still pays after the cut on the dearest unit, makes 20s or
    more in a day, and is solid only as a deal sold at its relist without the
    collector's unsure. On the spec's own snapshot: its exact rows at 10g, 50g and
    300g, the 13 flips it names never show, no unsure deal shows, and 34 pass with
    no sizing (31 gap, 3 deal)."""
    if R.SIM:
        sc.check(True, "live data only: nothing to check on the simulated data")
        return
    w = World(ref, items)
    names, unsure_deals = set(), 0
    for g in LIVE_GOLDS:
        for limit in (5, 10):
            for x in lua_ideas(w, limit, g * 10000):
                if not x.data:
                    continue
                nm = item_name(ref, items, int(x.id))
                names.add(nm)
                if x.kind == "vendor":
                    continue
                lab = "live %dg, %s" % (g, nm)
                st, risk = x.stats, lua_list(x.risk)
                cap = math.floor(DAY_PRICE_CAP * x.ref)
                sc.check(st.hours >= DAY_SALE_HOURS, lab + ": sales in %d hours" % st.hours)
                sc.check(x.countAt <= x.relist and x.countAt <= cap, lab + ": counted at %s, at most the relist %s and 1.25 x %s"
                         % (x.countAt, x.relist, x.ref))
                if x.kind == "gap" and x.usual > 0:
                    sc.check(x.countAt <= math.floor(DAY_SPIKE * x.usual), lab + ": at most 1.5 x the usual price")
                sc.check(int(x.countAt) * 95 // 100 > x.buy, lab + ": the dearest unit still pays at %s" % x.countAt)
                sc.check(x.dayValue >= DAY_MIN_PROFIT and x.dayProfit == math.floor(x.dayValue),
                         lab + ": %s in a day" % x.dayValue)
                sc.check(x.rate == DAY_SHARE * st.sold, lab + ": one seller's share of the sales")
                sc.check(not x.solid or (x.kind == "deal" and x.countAt == x.relist and "unsure" not in risk),
                         lab + ": solid only as a deal at its relist, not unsure")
                unsure_deals += x.kind == "deal" and "unsure" in risk
    sc.check(len(names) > 0, "live: collector ideas shown (%d items)" % len(names))
    if ref.updated != LIVE_SNAPSHOT:
        sc.check(True, "live: not the ranking spec's snapshot (updated %s): its exact rows are not checked" % ref.updated)
        sc.clean(w, "live")
        return
    sc.eq(unsure_deals, 0, "live: no deal the collector is unsure of shows")
    sc.eq(sorted(n for n in LIVE_NEVER if n in names), [], "live: the flips the spec drops never show")
    unsized = [x for x in lua_ideas(w, 1000, None) if x.data]
    kinds = [x.kind for x in unsized]
    sc.eq((len(unsized), kinds.count("gap"), kinds.count("deal")), (34, 31, 3), "live: unsized, 34 collector ideas (31 gap, 3 deal)")
    mine = ref_collector_flips(ref, 1000, gold=None)[0]
    sc.eq([x["id"] for x in mine], [int(x.id) for x in unsized], "live: unsized, the model's 34")
    sc.clean(w, "live")
    for g, want in sorted(LIVE_ROWS.items()):
        lab = "live sidebar %dg" % g
        w = World(ref, items, gold=g * 10000)
        w.open_ah_modern()
        check_sidebar_flips(sc, w, ref, lab)
        sc.eq([row_texts(r) for r in shown_rows(w.ns.SidebarPanel, "flipRows")], want, lab + ": the spec's rows")
        w.close_ah_modern()
        sc.clean(w, lab)


def old_format_text(ref):
    """The data as an older collector wrote it: no variants, no flips, no version lines."""
    text = drop_table(drop_table(ref.text, "variants"), "flips")
    span = block_span(text, "items")
    if span and ref.base_of:
        a, b = span
        body = re.sub(r'(?m)^[ \t]*\[(\d+)\][ \t]*=[ \t]*"(?:[^"\\\n]|\\.)*",?[ \t]*\r?\n',
                      lambda m: "" if int(m.group(1)) in ref.base_of else m.group(0), text[a:b])
        text = text[:a] + body + text[b:]
    return text


def sc_old_format(sc, ref, items):
    """A data file from before versions and flips: every item counts, the
    sidebar shows the history ideas as before, gear links say "all versions",
    and gear with bonus ids only follows a live listing of that version."""
    text = old_format_text(ref)
    r2 = RefData(text=text)
    sc.check(not r2.variants and not r2.flips and not r2.base_of, "old format: test data made")
    w = World(r2, items, data_src=text)
    ns, M = w.ns, w.M
    sc.eq((ns.ItemCount(), ns.VariantCount()), (len(r2.items), 0), "old format: item count, no versions")
    sc.eq(ns.DataFlips() and len(ns.DataFlips()), 0, "old format: no collector flips")
    out = "\n".join(w.slash("/gl"))
    sc.check("versions of gear" not in out, "old format: status has no versions line")
    w.open_ah_modern()
    SB = ns.SidebarPanel
    want, why = check_sidebar_flips(sc, w, r2, "old format")
    sc.check(all(not x.get("data") for x in want), "old format: only history ideas")
    rows = shown_rows(SB, "flipRows")
    tt = w.g.GameTooltip
    if rows:
        M.enter(rows[0])
        flat = [strip_codes(a or "") + " | " + strip_codes(b or "") for a, b in w.all_lines(tt)]
        sc.check(any(f.startswith("Profit each, after the 5% cut") for f in flat), "old format: history idea hover")
        M.leave(rows[0])
    if R.SIM:
        sc.check(len(want) > 0, "old format: the simulated data still gives history ideas")
    # gear links carrying bonus ids: the whole item's prices, said to cover all versions
    age = w.now - r2.updated
    w.put_bag(3, 1, R.ARCANE, 2, bonus=[12718])
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetBagItem(tt, 3, 1)
    sc.eq(w.lines(tt), expected_tooltip(r2, R.ARCANE, age, count=2, cover="all"), "old format: a version's tooltip covers all versions")
    w.put_bag(3, 2, R.ARCANE, 2)
    tt.SetBagItem(tt, 3, 2)
    sc.eq(w.lines(tt), expected_tooltip(r2, R.ARCANE, age, count=2, cover=None), "old format: a plain copy is a plain item")
    M["items"][R.ARCANE].commodity = False
    ah = w.g.AuctionHouseFrame
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 3, 1))
    w.settle(0.2)
    pair = find_sell_buttons(ah.ItemSellFrame)
    buttons_ok(sc, pair, (None, None), "old format: no buttons for a version without its own listing")
    t = w.lua.table()
    t[1] = w.to_lua({"buyout": 77700, "quantity": 1, "bonus": [12718]})
    M.itemResults[R.ARCANE] = t
    M.fire("ITEM_SEARCH_RESULTS_UPDATED", w.to_lua({"itemID": R.ARCANE, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}))
    w.settle(0.3)
    buttons_ok(sc, pair, (price_for_frame(77699, False), None), "old format: Sell now from that version's live listing")
    M["items"][R.ARCANE].commodity = True
    w.close_ah_modern()
    sc.clean(w)


def series24(price, mkt, sold=1):
    """24 hourly and 5 daily prices at one level (the series fields of an item line)."""
    return enc_series([price] * 24, [mkt] * 24, [sold] * 24) + "|" + enc_series([price] * 5, [mkt] * 5, [24 * sold] * 5)


def sc_stack_verdicts(sc, ref, items):
    """The sell verdict on stacks: "This stack (N), after AH cut" is worth the
    verdict's price, in white, only when the AH pays more ("Auction it") or
    about the same ("Close call"); never for "Vendor it" or "Maybe auction".
    Shift multiplies the prices but changes neither the verdict nor the stack
    line. There is no "Vendor it: a vendor pays more" line any more."""
    picks = [i for i in other_items(ref, items, 80, avoid=SIDEBAR_WATCH)
             if ref.items[i]["cheapest"] >= 3 and ref.items[i]["market"] > 0 and i not in ref.variants]
    if not sc.check(len(picks) >= 4, "stack: enough items to test with"):
        return
    a, b_, c, d = picks[:4]

    # Hand-made prices (the item keeps its name): fields after the name are
    # cheapest|market|listed|sold24|avg7|vendor|hourly|daily|listings
    made = {
        a: "4000|4200|50|20|4200|5000|%s|20" % series24(4000, 4200),        # a vendor pays 50s: "Vendor it", "+25% vs AH"
        b_: "1000|1100|50|20|1050|0|%s|20" % series24(1000, 1100),          # a vendor will not buy it: "Auction it", "can't vendor"
        c: "278|288|4000|6976|288|250|%s|209" % series24(278, 288, 300),    # 2s 88c against 2s 50c: "Close call", "AH +9%"
        d: "0|0|0|20|0|25|%s|0" % series24(30, 32),                         # nothing listed now: "Maybe auction"
    }

    def fn(iid, f):
        return [f[0]] + made[iid].split("|") if iid in made else None
    text = rewrite_items(ref.text, fn)
    r2 = RefData(text=text)
    w = World(r2, items, data_src=text)
    ns, M = w.ns, w.M
    tt = w.g.GameTooltip
    age = w.now - r2.updated
    kinds = {a: ("vendor",), b_: ("auction",), c: ("close",), d: ("maybe",)}
    for slot, (iid, count) in enumerate(((a, 20), (b_, 5), (c, 20), (d, 4), (R.LINEN, 20), (R.RUNE, 1)), start=1):
        w.put_bag(4, slot, iid, count)
        for shift in (False, True):
            M.shift = shift
            tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
            tt.SetBagItem(tt, 4, slot)
            lines = w.lines(tt)
            mult = count if shift and count > 1 else 1
            want, want_st = expected_tooltip(r2, iid, age, mult=mult, count=count, styles=True)
            lab = "stack %d x%d%s" % (iid, count, " Shift" if shift else "")
            sc.eq(lines, want, lab + " tooltip")
            tooltip_values_ok(sc, r2, iid, lines, mult, lab)
            verdict_line_ok(sc, r2, iid, lines, lab)
            tooltip_style_ok(sc, w, tt, want_st, lab)
            V = ref_verdict(r2, iid)
            texts = [l for l, _ in lines]
            stack = [r for l, r in lines if l.startswith("This stack")]
            if iid in kinds:
                sc.check(V is not None and V["kind"] in kinds[iid], "%s: verdict kind %s (%s)" % (lab, kinds[iid], V))
            if V and V["kind"] in ("auction", "close") and count > 1:
                sc.eq(stack, [money_icons(rec_model.stack_after_cut(V["unit"], count))],
                      "%s: the stack at the verdict's price, the same with Shift" % lab)
            else:
                sc.eq(stack, [], "%s: no stack line for %s" % (lab, V and V["kind"]))
            sc.check(not any(t.startswith("Vendor it:") for t in texts), "%s: no old 'Vendor it:' line (%s)" % (lab, texts))
        M.shift = False
    # c is the hand-made close call: 288 x 95 = 27,360 against 250 x 100 = 25,000 (9% more)
    tt.SetBagItem(tt, 4, 3)
    lines = w.lines(tt)
    sc.check(("Close call", "AH +9%") in lines and ("This stack (20), after AH cut", money_icons(5472)) in lines,
             "stack: Dense Stone's close call and its stack (54s 72c) (%s)" % lines)
    # with the auction house open, the live listing is used
    w.open_ah_modern()
    lp = r2.items[R.LINEN]["cheapest"] + 11
    w.set_commodity_results(R.LINEN, [(lp, 30, False)])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.LINEN)
    w.settle(0.3)
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetBagItem(tt, 4, 5)
    want, want_st = expected_tooltip(r2, R.LINEN, age, count=20, live=lp, styles=True)
    sc.eq(w.lines(tt), want, "stack: live listing used for Sell now and the verdict")
    tooltip_values_ok(sc, r2, R.LINEN, w.lines(tt), 1, "stack live", live=lp)
    verdict_line_ok(sc, r2, R.LINEN, w.lines(tt), "stack live", live=lp)
    tooltip_style_ok(sc, w, tt, want_st, "stack live")
    w.close_ah_modern()
    # compact tooltips stay one line
    w.slash("/gl compact")
    for slot, iid in ((1, a), (2, b_), (3, c), (4, d)):
        tt.SetBagItem(tt, 4, slot)
        want, want_st = expected_tooltip(r2, iid, age, compact=True, styles=True)
        sc.eq(w.lines(tt), want, "stack %d: compact tooltip is one line, no stack line" % iid)
        tooltip_style_ok(sc, w, tt, want_st, "stack %d compact" % iid)
    sc.clean(w)


# The spec's hand-made items for the sell verdict: (key, fields after the name,
# the verdict as (left, right) or None, a stack and its value after the cut or None).
VERDICT_FIXTURES = [
    # Silk Cloth: the cheapest (1s 58c) is a lone dip under the usual 1s 90c; the AH value is 2s, a vendor pays 1s 50c
    ("silk", "158|200|11850|14202|199|150|%s|%s|418" % (
        enc_series([190] * 23 + [158], [200] * 24, [600] * 24), enc_series([190, 158], [198, 200], [14000, 14202])),
     ("Auction it", "+26% vs vendor"), (20, 3800)),
    ("heavy", "389|389|862|13417|367|60|%s|%s|39" % (
        enc_series([340] * 12 + [389] * 12, [370] * 12 + [389] * 12, [500] * 24), enc_series([350] * 7, [367] * 7, [13000] * 7)),
     ("Auction it", "6.1x vendor"), (17, 6266)),
    ("shiny", "7|7|5000|7254|7|7|%s|323" % series24(7, 7, 300), ("Vendor it", "+5% vs AH"), (20, None)),
    ("dense", "278|288|4000|6976|288|250|%s|209" % series24(278, 288, 300), ("Close call", "AH +9%"), (20, 5472)),
    ("vendormore", "4000|4200|50|20|4200|5000|%s|20" % series24(4000, 4200), ("Vendor it", "+25% vs AH"), (20, None)),
    ("novendor", "1000|1100|50|20|1050|0|%s|20" % series24(1000, 1100), ("Auction it", "can't vendor"), (20, 20900)),
    ("onelisting", "1000|1100|1|30|1050|10|%s|1" % series24(1000, 1100), ("Maybe auction", "few listed"), None),
    ("vendorjunk", "1000|1100|5|30|1050|5|%s|5" % series24(1000, 1100), None, None),
    ("nothingnow", "0|0|0|20|0|25|%s|0" % series24(30, 32), ("Maybe auction", "none listed now"), None),
    ("even", "21|22|50|20|22|19|%s|20" % series24(21, 22), ("Close call", "tiny gain"), None),
    ("jumped", "3000|3000|20|50|1000|1000|%s|%s|8" % (
        enc_series([1000] * 23 + [3000], [1010] * 23 + [3000], [2] * 24), enc_series([1000] * 5, [1010] * 5, [50] * 5)),
     ("Maybe auction", "price jumped"), None),
    ("boundary", "44|44|500|100|44|38|%s|30" % series24(44, 44, 5), ("Auction it", "+10% vs vendor"), None),
    ("vendorone", "100|100|500|100|100|96|%s|30" % series24(100, 100, 5), ("Vendor it", "+1% vs AH"), None),
    ("double", "200|200|500|100|200|95|%s|30" % series24(200, 200, 5), ("Auction it", "2x vendor"), None),
    ("almost", "199|199|500|100|199|95|%s|30" % series24(199, 199, 5), ("Auction it", "+99% vs vendor"), None),
    ("ten", "1100|1100|500|100|1100|100|%s|30" % series24(1100, 1100, 5), ("Auction it", "10x vendor"), None),
    ("junk", "1|1|5|20|1|0|%s|5" % series24(1, 1), None, None),
    ("slow", "500|520|8|0|510|400|%s|8" % series24(500, 520, 0), ("Maybe auction", "sells slowly"), None),
    ("slowhuge", "500|520|8|0|510|100|%s|8" % series24(500, 520, 0), ("Auction it", "4.9x vendor"), None),
    ("tiny", "4|4|500|100|4|3|%s|30" % series24(4, 4, 5), ("Close call", "tiny gain"), (20, 76)),
    ("thinvendor", "50|50|1|10|50|60|%s|1" % series24(50, 50), ("Vendor it", "+28% vs AH"), None),
    ("thinnovendor", "30000|30000|2|0|30000|0|%s|2" % series24(30000, 30000), ("Maybe auction", "few listed"), None),
    # the rule edges: nothing listed and a Suggested price 300x the vendor price (over OLD_FAR, 200x), then 150x
    ("oldfar", "0|0|0|20|0|1|%s|0" % series24(300, 300), None, None),
    ("oldnear", "0|0|0|20|0|2|%s|0" % series24(300, 300), ("Maybe auction", "none listed now"), None),
    # the AH nets the same as a vendor (9,500 each), then exactly 1% more (9,595): VENDOR_PCT 101
    ("tie", "100|100|500|100|100|95|%s|30" % series24(100, 100, 5), ("Vendor it", "same as AH"), (20, None)),
    ("tieplus", "101|101|500|100|101|95|%s|30" % series24(101, 101, 5), ("Close call", "AH +1%"), (20, 1919)),
    # 1,900 against 1,700: exactly 2 copper more (MIN_GAIN)
    ("twocopper", "20|20|500|100|20|17|%s|30" % series24(20, 20, 5), ("Auction it", "+11% vs vendor"), (20, 380)),
    ("nothingvendor", "0|0|0|20|0|50|%s|0" % series24(30, 32), ("Vendor it", "+64% vs AH"), (20, None)),
]


def verdict_data(ref, items):
    """The data under test plus the hand-made verdict items, under new ids (and the
    mock game knows them, with the same vendor price). -> (text, items, {key: id})."""
    ids, iid = {}, 970001
    for key, _, _, _ in VERDICT_FIXTURES:
        while iid in ref.items or str(iid) in items:
            iid += 1
        ids[key] = iid
        iid += 1
    lines = "".join('    [%d] = "Verdict %s|%s",\n' % (ids[k], k.capitalize(), f) for k, f, _, _ in VERDICT_FIXTURES)
    a, _ = block_span(ref.text, "items")
    nl = ref.text.index("\n", a) + 1 if ref.text[a] != "\n" else a + 1
    text = ref.text[:nl] + lines + ref.text[nl:]
    items2 = dict(items)
    for key, f, _, _ in VERDICT_FIXTURES:
        items2[str(ids[key])] = {"name": "Verdict " + key.capitalize(), "sell": int(f.split("|")[5]), "quality": "COMMON"}
    return text, items2, ids


def sc_tooltip_verdicts(sc, ref, items):
    """The sell verdict on the spec's hand-made items: the words, the colours, the
    bigger line, the stack line, Shift, compact, no verdict for a bound item or
    another market, the live listing, and the bag panel's matching price."""
    text, items2, ids = verdict_data(ref, items)
    r2 = RefData(text=text)
    sc.check(all(ids[k] in r2.items for k in ids), "verdicts: the hand-made items are in the data")
    w = World(r2, items2, data_src=text)
    M, ns = w.M, w.ns
    tt = w.g.GameTooltip
    age = w.now - r2.updated
    for slot, (key, _, want, stack) in enumerate(VERDICT_FIXTURES, start=1):
        iid = ids[key]
        V = ref_verdict(r2, iid)
        sc.eq((V["left"], V["right"]) if V else None, want, "verdicts %s: the model's verdict is the spec's" % key)
        tt.SetItemByID(tt, iid)
        lines = w.lines(tt)
        want_lines, want_st = expected_tooltip(r2, iid, age, styles=True)
        sc.eq(lines, want_lines, "verdicts %s: tooltip" % key)
        verdict_line_ok(sc, r2, iid, lines, "verdicts %s" % key)
        tooltip_values_ok(sc, r2, iid, lines, 1, "verdicts %s" % key)
        tooltip_style_ok(sc, w, tt, want_st, "verdicts %s" % key)
        count = stack[0] if stack else 20
        w.put_bag(0, slot, iid, count)
        for shift in (False, True):
            M.shift = shift
            tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
            tt.SetBagItem(tt, 0, slot)
            lab = "verdicts %s x%d%s" % (key, count, " Shift" if shift else "")
            lines = w.lines(tt)
            want_lines, want_st = expected_tooltip(r2, iid, age, mult=count if shift else 1, count=count, styles=True)
            sc.eq(lines, want_lines, lab)
            verdict_line_ok(sc, r2, iid, lines, lab)
            tooltip_style_ok(sc, w, tt, want_st, lab)
            got_stack = [parse_coins(r) for l, r in lines if l.startswith("This stack")]
            if stack and stack[1] is not None:
                sc.eq(got_stack, [stack[1]], "%s: the spec's stack value" % lab)
            elif want is not None and want[0] in ("Vendor it", "Maybe auction"):
                sc.eq(got_stack, [], "%s: no stack line" % lab)
        M.shift = False
    # compact: one line, never bigger
    w.slash("/gl compact")
    for key, _, want, _ in VERDICT_FIXTURES:
        tt.SetItemByID(tt, ids[key])
        want_lines, want_st = expected_tooltip(r2, ids[key], age, compact=True, styles=True)
        sc.eq(w.lines(tt), want_lines, "verdicts %s: compact" % key)
        tooltip_style_ok(sc, w, tt, want_st, "verdicts %s compact" % key)
    w.slash("/gl compact")
    silk = ids["silk"]
    # TC19 a bound copy: bind on pickup, or a soulbound bag slot: the prices, no verdict, no stack line
    M["items"][silk].bind = 1
    tt.SetItemByID(tt, silk)
    want_lines, want_st = expected_tooltip(r2, silk, age, verdict=False, styles=True)
    sc.eq(w.lines(tt), want_lines, "verdicts: bind on pickup Silk has no verdict")
    tooltip_style_ok(sc, w, tt, want_st, "verdicts: bind on pickup Silk")
    M["items"][silk].bind = 0
    w.put_bag(1, 1, silk, 20)
    M.bags[1][1].bound = True
    tt.SetBagItem(tt, 1, 1)
    sc.eq(w.lines(tt), expected_tooltip(r2, silk, age, count=20, verdict=False), "verdicts: soulbound Silk x20, no verdict")
    tt.SetBagItem(tt, 0, 1)
    sc.eq(w.lines(tt), expected_tooltip(r2, silk, age, count=20), "verdicts: the tradeable stack still has it")
    # TC20 another market: the other faction, Hardcore, a goblin auction house
    for label, setup, undo in (("Horde", lambda: setattr(M, "faction", "Horde"), lambda: setattr(M, "faction", "Alliance")),
                               ("Hardcore", lambda: setattr(M, "hardcore", True), lambda: setattr(M, "hardcore", False))):
        setup()
        tt.SetBagItem(tt, 0, 1)
        want_lines, want_st = expected_tooltip(r2, silk, age, count=20, verdict=False, styles=True)
        sc.eq(w.lines(tt), want_lines, "verdicts: %s, no verdict" % label)
        tooltip_style_ok(sc, w, tt, want_st, "verdicts: %s" % label)
        undo()
    M.npc, M.subzone = w.to_lua({}), "Booty Bay"
    w.open_ah_modern()
    sc.check(bool(ns.AHState.otherHouse), "verdicts: a goblin auction house is another house")
    tt.SetBagItem(tt, 0, 1)
    sc.eq(w.lines(tt), expected_tooltip(r2, silk, age, count=20, verdict=False), "verdicts: a goblin auction house, no verdict")
    w.close_ah_modern()
    M.npc, M.subzone = None, None
    # TC21 the live cheapest feeds List At and Sell now
    w.open_ah_modern()
    for lp, own, want in ((195, False, "+26% vs vendor"), (210, False, "+32% vs vendor"), (196, True, "+26% vs vendor")):
        w.set_commodity_results(silk, [(lp, 30, own)])
        M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", silk)
        w.settle(0.3)
        sc.eq(w.live(silk), lp, "verdicts: live %d read" % lp)
        tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
        tt.SetBagItem(tt, 0, 1)
        lines = w.lines(tt)
        want_lines, want_st = expected_tooltip(r2, silk, age, count=20, live=lp, own=own, styles=True)
        sc.eq(lines, want_lines, "verdicts: Silk with a live listing at %d%s" % (lp, " (yours)" if own else ""))
        sc.check(("Auction it", want) in lines, "verdicts: live %d gives %r (%s)" % (lp, want, lines[:2]))
        tooltip_style_ok(sc, w, tt, want_st, "verdicts: live %d" % lp)
    w.close_ah_modern()
    # TC29 the bag panel values Silk at the verdict's price: 200 x 0.95 = 190 each, 3,800 for 20,
    # an auction item (at Sell now it was 157 x 0.95 = 149 against a vendor's 150)
    w.put_bag(1, 2, ids["vendorjunk"], 1)
    s = ns.BagScan()
    rows = {(s.drop[i].bag, s.drop[i].slot): s.drop[i] for i in range(1, len(s.drop) + 1)}
    e = rows.get((0, 1))
    got = (e.source, e.auction, e.value) if e else None
    sc.check(got == ("auction", 190, 3800), "verdicts: the bag panel calls Silk x20 an auction item worth 3,800 (%r)" % (got,))
    e = rows.get((1, 2))
    got = (e.source, e.auction) if e else None
    sc.check(got == ("vendor", None), "verdicts: a few listed far above the vendor price get no auction value (%r)" % (got,))
    for (bag, slot), e in sorted(rows.items()):
        value, unsure, source = bag_value(w, r2, int(e.id), int(e.count), bound=(bag, slot) == (1, 1))
        sc.eq((e.value, bool(e.unsure), e.source), (value, unsure, source),
              "verdicts: bag panel %d in %d:%d (model)" % (int(e.id), bag, slot))
    sc.clean(w)


def sc_tooltip_fonts(sc, ref, items):
    """The bigger verdict line: on our own Font (one per face, size and flags,
    a copy of the line's Font with its shadow), its colour set again, restored
    on clear and hide (the very Font object it had, its colour kept), only
    GameTooltip and ItemRefTooltip, lines made after it at the normal size,
    and the fallbacks."""
    text, items2, ids = verdict_data(ref, items)
    r2 = RefData(text=text)
    silk, junk = ids["silk"], ids["vendorjunk"]
    w = World(r2, items2, data_src=text)
    M, g = w.M, w.g
    tt = g.GameTooltip
    age = w.now - r2.updated
    gtt = g.GameTooltipText
    gtt.SetShadowOffset(gtt, 2, -2)    # a shadow our bigger copy must keep
    w.put_bag(0, 1, silk, 20)
    tt.SetOwner(tt, g.UIParent, "ANCHOR_RIGHT")
    tt.SetBagItem(tt, 0, 1)
    want_lines, want_st = expected_tooltip(r2, silk, age, count=20, styles=True)
    sc.eq(w.lines(tt), want_lines, "fonts: Silk x20")
    n = int(tt._blz) + 2           # the verdict: the game's 2 lines, our header, then it
    sc.eq(big_fontstrings(w, tt), [(n, "left", "ForeverAHBigFont1", 15), (n, "right", "ForeverAHBigFont1", 15)],
          "fonts: the verdict's both sides on ForeverAHBigFont1 at 15")
    tooltip_style_ok(sc, w, tt, want_st, "fonts: Silk x20 (green after the swap)")
    sc.eq(M.fontsCreated, 1, "fonts: one Font made")
    f = g.ForeverAHBigFont1
    sc.eq(f.GetFont(f), ("Fonts\\PTSansNarrow.ttf", 15, ""), "fonts: the line's own face and flags, 3 points bigger")
    sc.eq((f.GetShadowOffset(f), f.GetShadowColor(f)), ((2, -2), (0, 0, 0, 1)),
          "fonts: our Font is a copy of GameTooltipText (ElvUI's shadow kept)")
    tt.ClearLines(tt)
    sc.eq(big_fontstrings(w, tt), [], "fonts: ClearLines puts the Font back")
    s = M.lineStyle(tt, n)
    sc.eq((s.left.font, s.left.size, s.right.font), ("GameTooltipText", 12, "GameTooltipText"), "fonts: the exact Font object")
    tt.SetBagItem(tt, 0, 1)
    tt.Show(tt)
    tt.Hide(tt)
    sc.eq(big_fontstrings(w, tt), [], "fonts: Hide puts it back")
    tt.SetBagItem(tt, 0, 1)
    sc.eq(len(big_fontstrings(w, tt)), 2, "fonts: bigger again")
    M.fireScript(tt, "OnHide")     # a hide that does not clear the lines (a parent hid, as with Alt+Z)
    sc.eq(big_fontstrings(w, tt), [], "fonts: OnHide alone puts it back")
    sc.eq(w.lines(tt), want_lines, "fonts: OnHide alone keeps the lines")
    tooltip_style_ok(sc, w, tt, [(lc, rc, False) for lc, rc, _ in want_st],
                     "fonts: OnHide alone (the verdict still green, at the normal size)")
    tt.ClearLines(tt)
    tt.SetBagItem(tt, 0, 1)
    tt.SetOwner(tt, g.UIParent, "ANCHOR_RIGHT")
    sc.eq(big_fontstrings(w, tt), [], "fonts: SetOwner (a clear) puts it back")
    tt.SetBagItem(tt, 0, 1)
    tt.SetItemByID(tt, junk)
    sc.eq(big_fontstrings(w, tt), [], "fonts: the next item, with no verdict, has nothing bigger")
    for _ in range(50):
        tt.SetBagItem(tt, 0, 1)
    sc.eq((M.fontsCreated, len(big_fontstrings(w, tt))), (1, 2), "fonts: 50 more hovers make no new Font")
    w.M.shift = True
    tt.RefreshData(tt)
    w.M.shift = False
    tt.RefreshData(tt)
    sc.eq(big_fontstrings(w, tt), [(n, "left", "ForeverAHBigFont1", 15), (n, "right", "ForeverAHBigFont1", 15)],
          "fonts: Shift down and up (rebuilds): still exactly one bigger line")
    gtt = g.GameTooltipText
    gtt.SetFont(gtt, "Fonts\\PTSansNarrow.ttf", 14, "")
    tt.SetBagItem(tt, 0, 1)
    sc.eq((big_fontstrings(w, tt), M.fontsCreated),
          ([(n, "left", "ForeverAHBigFont2", 17), (n, "right", "ForeverAHBigFont2", 17)], 2),
          "fonts: GameTooltipText at 14 gets a new Font at 17")
    tooltip_style_ok(sc, w, tt, want_st, "fonts: at 14 points", base_size=14)
    gtt.SetFont(gtt, "Fonts\\PTSansNarrow.ttf", 12, "")
    tt.ClearLines(tt)
    ir = g.ItemRefTooltip
    ir.SetHyperlink(ir, M.ItemLink(silk))
    want_lines, want_st = expected_tooltip(r2, silk, age, styles=True)
    tooltip_style_ok(sc, w, ir, want_st, "fonts: ItemRefTooltip from a chat link")
    sc.eq(len(big_fontstrings(w, ir)), 2, "fonts: ItemRefTooltip's verdict is bigger")
    sc.eq(M.fontsCreated, 2, "fonts: the 12-point Font is reused")
    for name in ("ShoppingTooltip1", "ShoppingTooltip2", "ItemRefShoppingTooltip1"):
        st = g[name]
        st.SetItemByID(st, silk)
        sc.eq(w.lines(st), want_lines, "fonts: %s lines" % name)
        sc.eq(big_fontstrings(w, st), [], "fonts: %s is never bigger" % name)
    # Lines made after ours copy a normal line: 8 game lines, Silk, then an ElvUI-like
    # post call adding a blank line and the ID line
    M.extraGameLines = w.to_lua(["stat %d" % i for i in range(3, 8)] + ["Sell Price: 1s"])
    w.lua.execute("""TooltipDataProcessor.AddTooltipPostCall(Enum.TooltipDataType.Item, function(tt, d)
        tt:AddLine(" ")
        tt:AddDoubleLine("ID: " .. tostring(d.id), "Bags: 30")
      end)""")
    tt.SetOwner(tt, g.UIParent, "ANCHOR_RIGHT")
    tt.SetItemByID(tt, silk)
    al = w.all_lines(tt)
    sc.eq([l for l, _ in al[8:]], ["Goblin Ledger", "Auction it", "AH value", "2-day average", "Sell now", "Suggested price",
                                   " ", "ID: %d" % silk], "fonts: lines 9 to 16")
    sc.eq(big_fontstrings(w, tt), [(10, "left", "ForeverAHBigFont1", 15), (10, "right", "ForeverAHBigFont1", 15)],
          "fonts: only line 10 is bigger")
    others = []
    for i in range(2, len(tt._L) + 1):
        s = M.lineStyle(tt, i)
        if i != 10:
            others += [(i, side, x.font, x.size) for side, x in (("left", s.left), ("right", s.right))]
    sc.eq([x for x in others if x[2:] != ("GameTooltipText", 12)], [], "fonts: lines 11 to 16, made on demand, stay at 12")
    M.extraGameLines = None
    sc.clean(w, "fonts")
    # The verdict's line on another addon's Font (a bold outline, not GameTooltipText):
    # our Font is a copy of it, and clear and hide put back that very Font object
    w4 = World(r2, items2, data_src=text)
    t4 = w4.g.GameTooltip
    w4.lua.execute("""
      local o = CreateFont("OtherAddonBold")
      o:SetFont("Fonts\\\\PTSansNarrow.ttf", 12, "THICKOUTLINE")
      o:SetShadowOffset(3, -3)
      MOCK.otherFont = o
      GameTooltipTextLeft4:SetFontObject(o)
      GameTooltipTextRight4:SetFontObject(o)""")
    other = w4.M.otherFont

    def on_other(label):
        got = [w4.same(fs.GetFontObject(fs), other) for fs in (w4.g.GameTooltipTextLeft4, w4.g.GameTooltipTextRight4)]
        sc.eq(got, [True, True], "fonts: %s puts back the very Font object the line had (another addon's)" % label)
    t4.SetItemByID(t4, silk)
    sc.eq(w4.lines(t4), expected_tooltip(r2, silk, age), "fonts on another addon's Font: the lines")
    sc.eq(big_fontstrings(w4, t4), [(4, "left", "ForeverAHBigFont1", 15), (4, "right", "ForeverAHBigFont1", 15)],
          "fonts on another addon's Font: the verdict is bigger")
    f4 = w4.g.ForeverAHBigFont1
    sc.eq((f4.GetFont(f4), f4.GetShadowOffset(f4)), (("Fonts\\PTSansNarrow.ttf", 15, "THICKOUTLINE"), (3, -3)),
          "fonts on another addon's Font: ours keeps its face, flags and shadow")
    t4.ClearLines(t4)
    on_other("a clear")
    for label, hide in (("OnHide", lambda: w4.M.fireScript(t4, "OnHide")), ("Hide", lambda: t4.Hide(t4))):
        t4.SetItemByID(t4, silk)
        sc.eq(len(big_fontstrings(w4, t4)), 2, "fonts on another addon's Font: bigger again before %s" % label)
        hide()
        on_other(label)
    sc.clean(w4, "fonts on another addon's Font")
    # Fallbacks: the same lines and colours at the normal size, no error and no chat line. In the
    # last two the left side is already bigger when the right side fails: both go back, still green
    for label in ("hook refused", "no CreateFont", "CreateFont gives nothing", "right side's GetFont gives nothing",
                  "right side refuses our Font"):
        w3 = World(r2, items2, data_src=text)
        t3 = w3.g.GameTooltip
        if label == "hook refused":
            w3.lua.execute("GameTooltip.HookScript = function() error('HookScript refused') end")
        elif label == "no CreateFont":
            w3.g.CreateFont = None
        elif label == "CreateFont gives nothing":
            w3.lua.execute("CreateFont = function() return nil end")
        elif label == "right side's GetFont gives nothing":
            w3.lua.execute("GameTooltipTextRight4.GetFont = function() return nil end")
        else:
            w3.lua.execute("""
              local fs = GameTooltipTextRight4
              local set = fs.SetFontObject
              fs.SetFontObject = function(self, o)
                if type(o) == "table" and tostring(o._name):find("ForeverAH", 1, true) then error("refused") end
                return set(self, o)
              end""")
        t3.SetItemByID(t3, silk)
        want_lines, want_st = expected_tooltip(r2, silk, age, styles=True)
        sc.eq(w3.lines(t3), want_lines, "fonts %s: the lines" % label)
        tooltip_style_ok(sc, w3, t3, [(lc, rc, False) for lc, rc, _ in want_st], "fonts %s: normal size" % label)
        sc.clean(w3, "fonts " + label)


def sc_verdict_parity(sc, ref, items):
    """rec_model.sell_verdict (the Python twin) equals ns.SellVerdict, and
    sell_verdict_price ns.SellVerdictPrice, for every item of the data (a
    sample for the longer timeframes and live prices of a big file)."""
    w = World(ref, items)
    ns = w.ns

    def tup(v):
        if v is None:
            return None
        if isinstance(v, dict):
            return (v["kind"], v["unit"], v["left"], v["right"], v["lc"], v["rc"])
        return (v.kind, v.unit, v.left, v.right, v.lc, v.rc)
    cases = [(iid, "1D", None, False) for iid in sorted(ref.items)]
    for iid in sample_ids(ref, SAMPLE * 2):
        lp = ref.items[iid]["cheapest"] + 7
        cases += [(iid, tf, None, False) for tf in ("5D", "1M", "1Y")] + [(iid, "1D", lp, False), (iid, "1D", lp, True)]
    bad, kinds = [], {}
    for iid, tf, live, own in cases:
        rec = ref_recommend(ref, iid, tf, live)
        it = ref.items[iid]
        sp = rec_model.sell_prices(rec, live, own, it["vendor"])
        want = tup(rec_model.sell_verdict(it, sp, rec, ref.salesHours))
        g = ns.GetItem(iid)
        P = ns.SellPrices(iid, tf, live, own)
        got = tup(ns.SellVerdict(g, P))
        pu, lu = rec_model.sell_verdict_price(it, sp, rec), ns.SellVerdictPrice(g, P)
        if got != want or pu != lu:
            bad.append((iid, tf, live, own, got, want, pu, lu))
        k = want[0] if want else None
        kinds[k] = kinds.get(k, 0) + 1
    sc.eq(bad[:5], [], "parity: the Python twin matches the addon on %d item cases (%d differ)" % (len(cases), len(bad)))
    sc.check(kinds.get("auction") and len(kinds) >= 2, "parity: the data has auction calls and other kinds (%s)" % kinds)
    sc.clean(w)


# ===========================================================================
# Bag cleanup (Bags.lua): what to drop or sell first, old quest items, merges
# ===========================================================================
def bag_add_item(w, iid, name, quality=1, vendor=0, **fields):
    """An item the mock game knows (not in the price data)."""
    t = w.lua.table()
    t.name, t.quality, t.icon, t.vendor = name, quality, 140000 + iid % 1000, vendor
    t["class"] = "Miscellaneous"
    for k, v in fields.items():
        t[k] = v
    w.M["items"][iid] = t


def bag_put(w, bag, slot, iid, count=1, **flags):
    w.put_bag(bag, slot, iid, count)
    s = w.M.bags[bag][slot]
    for k, v in flags.items():
        s[k] = v


def bag_quests(w, quests):
    t = w.lua.table()
    for i, q in enumerate(quests, start=1):
        t[i] = w.to_lua(q)
    w.M.quests = t


def bag_auction_unit(ref, iid):
    """Bags.lua AuctionUnit: listed now, the tooltip verdict's price
    (rec_model.sell_verdict_price); else Sell now; else the market price
    (rough) when none is listed."""
    it = ref.items.get(iid)
    if not it:
        return None, False
    rec = ref_recommend(ref, iid, "1D")
    sp = rec_model.sell_prices(rec, None, False, it["vendor"])
    u = rec_model.sell_verdict_price(it, sp, rec) if it["market"] > 0 else None
    if u or sp["now"]:
        return u or sp["now"], price_doubt(ref, iid)
    if sp["nowWhy"] == "none" and it["market"] > 0:
        return it["market"], True
    return None, False


def bag_value(w, ref, iid, count, bound=False):
    vendor = int(w.M["items"][iid].vendor or 0)
    unit, unsure = (None, False) if bound else bag_auction_unit(ref, iid)
    v_total, a_total = vendor * count, (rec_model.stack_after_cut(unit, count) if unit else 0)
    return max(v_total, a_total), (unsure and a_total > v_total), ("auction" if a_total > v_total else "vendor")


def bag_scan_lists(w):
    s = w.ns.BagScan()

    def rows(t, fields):
        return [tuple(t[i][f] for f in fields) for i in range(1, len(t) + 1)]
    return s, rows


def sc_bag_cleanup(sc, ref, items):
    """Bag cleanup: stacks worth the least first, old quest items, partial stacks
    to merge; the tab and panel beside the open bags; nothing is used, sold or
    destroyed without a click (and selling only at an open vendor)."""
    priced = [i for i in other_items(ref, items, 60, avoid=SIDEBAR_WATCH)
              if ref.items[i]["cheapest"] >= 3 and ref.items[i]["market"] > 0
              and str(items[str(i)].get("quality", "COMMON")).upper() != "POOR"
              and not any(c in ref.items[i]["name"] for c in "():/")]
    if not sc.check(len(priced) >= 3, "bags: enough priced items to test with"):
        return
    p1, p2, p3 = priced[:3]
    w = World(ref, items)
    M, ns, g = w.M, w.ns, w.g
    fake = [6948, 2901, 900101, 900102, 900103, 900104, 900105, 900106, 900107, 900108, 900109, 12840,
            900110, 900111, 900112, 900113, 900114]
    sc.check(not any(i in ref.items for i in fake if i > 900000), "bags: the made-up items have no price data")
    bag_add_item(w, 6948, "Hearthstone", 1, 0, classID=15)
    bag_add_item(w, 2901, "Mining Pick", 1, 40, classID=2, subclassID=14, maxStack=1)
    bag_add_item(w, 900101, "Broken Fang", 0, 12, classID=15)
    bag_add_item(w, 900102, "Tattered Cloth Scrap", 0, 50, classID=15, maxStack=1)
    bag_add_item(w, 900103, "Tough Jerky", 1, 1, classID=0, minLevel=1)
    bag_add_item(w, 900104, "Morning Glory Dew", 1, 100, classID=0, minLevel=55)
    bag_add_item(w, 900105, "Light Feather", 1, 2, classID=5)
    bag_add_item(w, 900106, "Dusty Journal", 1, 0, classID=12, startsQuest=501, maxStack=1)
    bag_add_item(w, 900107, "Gnoll Paw", 1, 0, classID=12)
    bag_add_item(w, 900108, "Murloc Fin", 1, 0, classID=12)
    bag_add_item(w, 900109, "Old Map", 1, 0, classID=12, startsQuest=502, maxStack=1)
    bag_add_item(w, 12840, "Minion's Scourgestone", 1, 0, classID=12)
    bag_add_item(w, 900110, "Rough Arrow", 1, 1, classID=6, maxStack=200)
    bag_add_item(w, 900111, "Worn Dagger", 1, 30, classID=2, subclassID=15, maxStack=1, bind=1)
    bag_add_item(w, 900112, "Wool Bolt", 1, 20, classID=7)
    bag_add_item(w, 900113, "Coarse Stone", 1, 3, classID=7)
    bag_add_item(w, 900114, "Moving Thing", 1, 5, classID=15)
    for iid in (p1, p2, p3):
        sc.check(w.M["items"][iid] is not None, "bags: the game knows priced item %d" % iid)
    layout = [
        (0, 1, 6948, 1, {}), (0, 2, 2901, 1, {}), (0, 3, 900101, 3, {}), (0, 4, 900102, 1, {}),
        (0, 5, p1, 20, {}), (0, 6, 900103, 5, {}), (0, 7, 900104, 5, {}), (0, 8, 900105, 10, {}),
        (0, 9, 900106, 1, {}), (0, 10, 900107, 3, {}), (0, 11, 900108, 2, {}), (0, 12, 900109, 1, {}),
        (0, 13, 12840, 4, {}), (0, 14, 900110, 200, {}), (0, 15, p2, 1, {}), (0, 16, 900111, 1, {"bound": True}),
        (1, 1, 900112, 5, {}), (1, 2, 900112, 7, {}), (1, 3, 900113, 15, {}), (1, 4, 900113, 15, {}),
        (1, 5, p3, 2, {}), (1, 6, 900114, 1, {"locked": True}),
    ]
    for bag, slot, iid, count, flags in layout:
        bag_put(w, bag, slot, iid, count, **flags)
    p3name = ref.items[p3]["name"]
    bag_quests(w, [
        {"title": "Elwynn Forest", "isHeader": True},
        {"title": "Gnoll Hunt", "questID": 601, "objectives": ["Gnoll Paw: 3/8"]},
        {"title": "Report to Goldshire", "questID": 602, "objectives": []},
        {"title": "Supplies", "questID": 603, "objectives": ["2/10 " + p3name]},
    ])
    M.questDone[501] = True

    # Expected drop list: everything but the hearthstone (no value), tools,
    # reagents, ammo, food near your level, quest items, the locked slot and
    # the item a quest asks for; cheapest first.
    want = []
    for bag, slot, iid, count, flags in layout:
        if iid in (6948, 2901, 900104, 900105, 900106, 900107, 900108, 900109, 12840, 900110, 900114, p3):
            continue
        value, unsure, source = bag_value(w, ref, iid, count, bound=flags.get("bound", False))
        junk = int(w.M["items"][iid].quality) == 0
        name = w.M["items"][iid].name
        want.append((value, 0 if junk else 1, name, bag, slot, iid, unsure, source))
    want.sort()
    s = ns.BagScan()
    got = [(s.drop[i].bag, s.drop[i].slot, s.drop[i].id, s.drop[i].value, bool(s.drop[i].unsure), s.drop[i].source)
           for i in range(1, len(s.drop) + 1)]
    sc.eq(got, [(b, sl, iid, v, u, src) for v, _, _, b, sl, iid, u, src in want], "bags: worth the least, cheapest first")
    sc.eq([(s.quest[i].id, s.quest[i].why, bool(s.quest[i].sure)) for i in range(1, len(s.quest) + 1)],
          [(900106, "done", True), (900108, "none", False)],
          "bags: old quest items (finished quest's journal; a fin no quest lists, unsure while a quest lists nothing)")
    sc.eq([s.junk[i].id for i in range(1, len(s.junk) + 1)], [900101, 900102], "bags: grey junk")
    sc.eq(len(s.merge), 1, "bags: one item can merge")
    if len(s.merge) == 1:
        m = s.merge[1]
        sc.eq((m.id, m.stacks, m.frees, m["from"].slot, m.to.slot), (900112, 2, 1, 1, 2),
              "bags: Wool Bolt 5 + 7 merge into one slot")
    sc.eq((s.free, s.slots), (80 - len(layout), 80), "bags: free and total slots")
    sc.check(not s.pending and not s.questPartial, "bags: everything loaded, quest log fully open")

    # Food: outgrown at 60, still yours at level 10.
    M.level = 10
    s10 = ns.BagScan()
    sc.check(900103 not in [s10.drop[i].id for i in range(1, len(s10.drop) + 1)], "bags: level 10 keeps the low food")
    M.level = 60

    # Chat report (bags closed).
    out = w.slash("/gl bags")
    txt = [strip_codes(x) for x in out]
    sc.check(any(("bags: %d of 80 slots free." % (80 - len(layout))) in t for t in txt), "bags slash: free slots (%s)" % txt[:2])
    sc.check(any("merge stacks" in t and "Wool Bolt" in t for t in txt), "bags slash: merge line")
    sc.check(any("old quest items" in t and "Dusty Journal" in t and "Murloc Fin" in t for t in txt), "bags slash: quest line")
    sc.check(any(t.endswith("junk: 2 grey items, 86c at a vendor.") for t in txt), "bags slash: junk line (%s)" % txt)
    first = want[0]
    sc.check(any(" 1. " in t and ("[%s]" % w.M["items"][first[5]].name) in t for t in txt), "bags slash: cheapest first (%s)" % txt)
    sc.check(any("Open your bags" in t for t in txt), "bags slash: says to open the bags")
    helpout = [strip_codes(x) for x in w.slash("/gl help")]
    sc.check(any(t.endswith("/gl bags : what to drop or sell first, old quest items and stacks to merge (also beside your open bags)")
                 for t in helpout), "bags: help lists /gl bags")
    sc.check(any("/gl watch" in t for t in helpout), "bags: help still has the sidebar commands")
    sc.check(any("/gl bagcheck : what the bag cleanup sees" in t for t in helpout), "bags: help lists /gl bagcheck")

    # Tab beside the open bags.
    BP = ns.BagPanel
    tab, F = g.ForeverAHBagTab, None
    sc.check(not BP.built or not BP.tab.IsShown(BP.tab), "bags: nothing shows while the bags are closed")
    M.OpenBags(5)
    w.settle(0.2)
    sc.check(BP.built and BP.tab.IsVisible(BP.tab), "bags: tab shows when the bags open")
    F = g.ForeverAHBagPanel
    sc.check(F is not None and not F.IsShown(F), "bags: the panel starts tucked away")
    sc.eq(strip_codes(BP.tab.text._text), "Bag cleanup  %d free" % (80 - len(layout)), "bags: tab text")
    lefts = [g["ContainerFrame%d" % i].GetLeft(g["ContainerFrame%d" % i]) for i in range(1, 6)]
    bottoms = [g["ContainerFrame%d" % i].GetBottom(g["ContainerFrame%d" % i]) for i in range(1, 6)]
    sc.check(abs(BP.tab.GetRight(BP.tab) - (min(lefts) - 4)) < 0.01 and abs(BP.tab.GetBottom(BP.tab) - min(bottoms)) < 0.01,
             "bags: tab sits left of the leftmost bag, level with the lowest")
    M.click(BP.tab)
    w.settle(0.1)
    sc.check(F.IsVisible(F) and not BP.tab.IsShown(BP.tab), "bags: clicking the tab opens the panel")
    sc.check(w.g.ForeverAH_Settings.bagPanel is True, "bags: the open panel is remembered")
    sc.check(F.GetRight(F) <= min(lefts) - 3.99, "bags: panel does not cover the bags")
    sc.check(F.GetTop(F) <= w.g.UIParent.GetTop(w.g.UIParent) + 0.01, "bags: panel fits on screen")

    def shown_rows():
        out = []
        for i in range(1, len(BP.rows) + 1):
            r = BP.rows[i]
            if r.IsShown(r):
                out.append(r)
        return out
    rows = shown_rows()
    sc.eq(len(rows), 1 + 2 + min(8, len(want)), "bags: one merge, two quest and up to 8 cheapest rows")
    if len(rows) >= 3:
        sc.eq((rows[0].kind, strip_codes(rows[0].name._text), rows[0].right._text), ("merge", "Wool Bolt (2 stacks)", "frees 1"),
              "bags: merge row")
        sc.eq([(r.kind, r.right._text) for r in rows[1:3]], [("quest", "quest done"), ("quest", "check log")], "bags: quest rows")
        drop_rows = rows[3:]
        sc.eq([(r.entry.id, r.right._text) for r in drop_rows],
              [(x[5], ("~" if x[6] else "") + short_money(x[0])) for x in want[:8]], "bags: drop rows show the stack's worth")
    sc.check(BP.mergeTitle.IsShown(BP.mergeTitle) and BP.questTitle.IsShown(BP.questTitle), "bags: section titles")
    sc.check(not BP.sellButton.IsShown(BP.sellButton), "bags: no Sell junk button away from a vendor")

    # Hover: the game's tooltip for the slot plus ours, and the slot lights up.
    tt = g.GameTooltip
    if len(rows) >= 4:
        r = rows[3]
        e = r.entry
        # Below Rare and not a quest item: the drop box asks. Anything else
        # (a Rare priced item can be the cheapest in real data) is picked up,
        # so the game's own check applies.
        droppable = int(e.quality) <= 2 and not e.questItem
        M.enter(r)
        lines = [strip_codes(l or "") for l, _ in w.all_lines(tt)]
        sc.check("Worth the least in your bags" in lines, "bags hover: our heading (%s)" % lines)
        if droppable:
            sc.check("Click: drop it (asks first)" in lines, "bags hover: says a click asks before dropping (%s)" % lines)
        else:
            sc.check(any(l.startswith("Click: pick it up") for l in lines), "bags hover: says a click picks it up (%s)" % lines)
        btn = g["ContainerFrame%dItem%d" % (e.bag + 1, 17 - e.slot)]
        sc.check(BP.glow.IsShown(BP.glow) and BP.glow._points[1][2] is not None and w.same(BP.glow._points[1][2], btn),
                 "bags hover: the bag slot is outlined")
        M.leave(r)
        sc.check(not BP.glow.IsShown(BP.glow), "bags hover: outline goes on leave")
        # Click away from a vendor: asks first (or picks a Rare item up), never uses it.
        n_pick = len(M.pickups)
        mark = w.chat_mark()
        M.click(r, "LeftButton")
        C = g.ForeverAHBagConfirm
        if droppable:
            sc.check(C.IsVisible(C), "bags click: opens the drop box")
            sc.eq(len(M.pickups), n_pick, "bags click: nothing picked up while it asks")
            M.click(g.ForeverAHBagConfirmCancel)
            sc.check(not C.IsShown(C) and BP.confirm is None, "bags click: Cancel closes the drop box")
        else:
            p = M.pickups[len(M.pickups)]
            sc.eq((p[1], p[2]), (e.bag, e.slot), "bags click: picks the item up")
            sc.check(any("Click the ground to destroy it (the game asks first)" in c for c in w.chat(mark)), "bags click: says how to destroy")
            w.lua.execute("ClearCursor()")
        sc.eq(len(M.used), 0, "bags click: never uses an item away from a vendor")
        sc.eq(len(M.destroyed), 0, "bags click: nothing destroyed")
    # Merge row.
    if len(rows) >= 1:
        M.click(rows[0], "LeftButton")
        w.settle(0.5)
        sc.check(M.bags[1][1] is None and M.bags[1][2].count == 12, "bags merge: 5 + 7 Wool Bolt in one slot")
        sc.check(not BP.mergeTitle.IsShown(BP.mergeTitle), "bags merge: section goes once merged")
        sc.check(not w.lua.eval("CursorHasItem()"), "bags merge: cursor is empty after")
    # Right-click menu: always keep.
    install_menu_mock(w)
    rows = shown_rows()
    dr = [r for r in rows if r.kind == "drop"]
    if dr:
        kid = dr[0].entry.id
        M.click(dr[0], "RightButton")
        texts = menu_texts(w)
        sc.check("Always keep (never suggest it)" in texts and "Pick it up" in texts, "bags menu: keep and pick up (%s)" % texts)
        menu_pick(w, "Always keep (never suggest it)")
        w.settle(0.1)
        s2 = ns.BagScan()
        sc.check(kid not in [s2.drop[i].id for i in range(1, len(s2.drop) + 1)], "bags menu: a kept item is no longer listed")
        sc.check(w.g.ForeverAH_Settings.bagKeep[kid] is True, "bags menu: keep is saved")
        w.slash("/gl keep %d" % kid)
    # /gl keep by name, list, undo.
    p2name = ref.items[p2]["name"]
    out = [strip_codes(x) for x in w.slash("/gl keep " + p2name)]
    sc.check(any(("never suggest " + p2name) in t for t in out), "bags keep: by name (%s)" % out)
    s3 = ns.BagScan()
    sc.check(p2 not in [s3.drop[i].id for i in range(1, len(s3.drop) + 1)], "bags keep: not listed")
    out = [strip_codes(x) for x in w.slash("/gl keep")]
    sc.check(any(p2name in t for t in out), "bags keep: listed (%s)" % out)
    w.slash("/gl keep " + p2name)
    s4 = ns.BagScan()
    sc.check(p2 in [s4.drop[i].id for i in range(1, len(s4.drop) + 1)], "bags keep: again undoes it")

    # At a vendor: Sell junk, and a click sells.
    M.OpenMerchant()
    w.settle(0.5)
    sb = BP.sellButton
    sc.check(sb.IsVisible(sb), "bags vendor: Sell junk button shows")
    sc.eq(sb.text._text, "Sell junk (2, 86c)", "bags vendor: button says how many and for how much")
    M.click(sb)
    w.settle(0.5)
    sc.eq(sorted(M.sold[i] for i in range(1, len(M.sold) + 1)), [900101, 900102], "bags vendor: the grey items are sold")
    sc.eq(M.money, 86, "bags vendor: for what a vendor pays")
    sc.check(not sb.IsShown(sb), "bags vendor: button goes when no junk is left")
    rows = shown_rows()
    dr = [r for r in rows if r.kind == "drop" and (r.entry.vendor or 0) > 0]
    if dr:
        e = dr[0].entry
        n_used = len(M.used)
        M.click(dr[0], "LeftButton")
        w.settle(0.5)
        sc.eq(len(M.used), n_used + 1, "bags vendor: a click on a row sells it")
        sc.check(M.bags[e.bag][e.slot] is None, "bags vendor: sold out of the bag")
    M.CloseMerchant()
    w.settle(0.5)
    # Collapsed quest log header: only sure verdicts are listed.
    M.quests[1].isCollapsed = True
    s5 = ns.BagScan()
    sc.check(s5.questPartial and [s5.quest[i].id for i in range(1, len(s5.quest) + 1)] == [900106],
             "bags: with a header closed only the finished quest's journal is listed")
    M.fire("QUEST_LOG_UPDATE")
    w.settle(0.5)
    sc.check(BP.note.IsShown(BP.note) and "quest log headers are closed" in BP.note._text, "bags: panel says the quest log is partly closed")
    # Tuck away, then bags closed.
    M.click(BP.closeButton)
    w.settle(0.1)
    sc.check(not F.IsShown(F) and BP.tab.IsVisible(BP.tab), "bags: > tucks the panel back into the tab")
    M.CloseBags()
    w.settle(0.1)
    sc.check(not BP.tab.IsShown(BP.tab) and not F.IsShown(F), "bags: closing the bags hides it all")
    sc.clean(w)


def sc_bag_cleanup_nodata(sc, ref, items):
    """No price data and the legacy client: the bag cleanup still works from vendor prices."""
    for client, data in (("modern", PLACEHOLDER), ("legacy", None)):
        w = World(ref, items, client=client, data_src=data)
        bag_add_item(w, 900201, "Cracked Bone", 0, 7, classID=15)
        bag_add_item(w, 900202, "Spider Silk Scrap", 1, 25, classID=7)
        w.put_bag(0, 1, 900201, 2)
        w.put_bag(0, 2, 900202, 1)
        s = w.ns.BagScan()
        sc.eq([(s.drop[i].id, s.drop[i].value) for i in range(1, len(s.drop) + 1)], [(900201, 14), (900202, 25)],
              "bags %s %s: vendor prices" % (client, "no data" if data else "data"))
        out = [strip_codes(x) for x in w.slash("/gl bags")]
        sc.check(any("worth the least" in t for t in out), "bags %s: slash works" % client)
        w.M.OpenBags(1)
        w.settle(0.2)
        BP = w.ns.BagPanel
        sc.check(BP.built and BP.tab.IsVisible(BP.tab), "bags %s: tab shows with the backpack" % client)
        sc.clean(w, "bags " + client)


def sc_bag_cleanup_elvui(sc, ref, items):
    """ElvUI's bags (built after login, under ElvUI's own scaled parent): the
    tab and panel hang off ElvUI's bag window and follow it, slots light up."""
    w = World(ref, items)
    M, ns, g = w.M, w.ns, w.g
    elv = M.InstallElvUIBags(0.8)
    bag_add_item(w, 900301, "Chipped Claw", 0, 9, classID=15)
    bag_add_item(w, 900302, "Coyote Meat", 1, 4, classID=7)
    w.put_bag(0, 3, 900301, 2)
    w.put_bag(2, 5, 900302, 3)
    BP = ns.BagPanel
    w.lua.execute("ToggleAllBags()")
    w.settle(0.2)
    sc.check(elv.IsVisible(elv) and not g.ContainerFrame1.IsShown(g.ContainerFrame1), "elvui: ElvUI's bag window opens, not the game's")
    sc.check(BP.built and BP.tab.IsVisible(BP.tab), "elvui: tab shows beside ElvUI's bags")
    p = BP.tab._points[1]
    sc.check(p[1] == "BOTTOMRIGHT" and w.same(p[2], elv) and p[3] == "BOTTOMLEFT", "elvui: tab hangs off ElvUI's bag window")
    tabE, elvE = BP.tab.GetEffectiveScale(BP.tab), elv.GetEffectiveScale(elv)
    sc.check(abs(BP.tab.GetRight(BP.tab) * tabE - (elv.GetLeft(elv) * elvE - 4 * tabE)) < 0.01, "elvui: tab right edge is 4 left of the bags")
    sc.check(abs(BP.tab.GetBottom(BP.tab) * tabE - elv.GetBottom(elv) * elvE) < 0.01, "elvui: tab level with the bottom of the bags")
    sc.eq(BP.tab.GetFrameStrata(BP.tab), "HIGH", "elvui: same strata as ElvUI's bags (drawn above the game's frames)")
    M.click(BP.tab)
    w.settle(0.1)
    F = g.ForeverAHBagPanel
    sc.check(F.IsVisible(F), "elvui: panel opens")
    rows = [BP.rows[i] for i in range(1, len(BP.rows) + 1) if BP.rows[i].IsShown(BP.rows[i])]
    want = sorted([(18, 900301), (12, 900302)])   # Chipped Claw 2 x 9c, Coyote Meat 3 x 4c
    sc.eq([r.entry.id for r in rows if r.kind == "drop"], [i for _, i in want], "elvui: drop rows cheapest first")
    # Hover lights the ElvUI slot button.
    for r in rows:
        if r.entry.id == 900302:
            M.enter(r)
            btn = elv.Bags[2][5]
            sc.check(BP.glow.IsShown(BP.glow) and w.same(BP.glow._points[1][2], btn), "elvui: hover outlines ElvUI's slot button")
            M.leave(r)
    # ElvUI's window is dragged: the panel follows.
    elv.ClearAllPoints(elv)
    elv.SetPoint(elv, "BOTTOMRIGHT", g.ElvUIParent, "BOTTOMRIGHT", -300, 120)
    sc.check(abs(F.GetRight(F) * F.GetEffectiveScale(F) - (elv.GetLeft(elv) * elvE - 4 * F.GetEffectiveScale(F))) < 0.01,
             "elvui: panel follows when ElvUI's bags move")
    w.lua.execute("ToggleAllBags()")
    w.settle(0.2)
    sc.check(not F.IsShown(F) and not BP.tab.IsShown(BP.tab), "elvui: closing ElvUI's bags hides the panel")
    w.lua.execute("OpenAllBags()")
    w.settle(0.2)
    sc.check(F.IsVisible(F), "elvui: reopening brings the panel back (it was left open)")
    sc.clean(w, "elvui")


def sc_bag_cleanup_parked(sc, ref, items):
    """A bag addon that parks the game's bag windows shown but with no position:
    no tab, and /gl bags still works."""
    w = World(ref, items)
    w.lua.execute("ContainerFrame1:SetID(0); ContainerFrame1:ClearAllPoints(); ContainerFrame1:Show()")
    w.settle(0.2)
    BP = w.ns.BagPanel
    sc.check(not BP.built or not BP.tab.IsShown(BP.tab), "parked: no tab for a bag window with no position")
    bag_add_item(w, 900303, "Brittle Bone", 0, 3, classID=15)
    w.put_bag(0, 1, 900303, 1)
    out = [strip_codes(x) for x in w.slash("/gl bags")]
    sc.check(any("Brittle Bone" in t for t in out), "parked: /gl bags lists the bags")
    sc.clean(w, "parked")


def _overlap(a, b):
    return a[0] < b[2] - 1e-6 and b[0] < a[2] - 1e-6 and a[1] < b[3] - 1e-6 and b[1] < a[3] - 1e-6


ELV_PARTS = ["closeButton", "helpButton", "goldText", "pickupGold", "editBox", "vendorGraysButton", "keyButton",
             "bagsButton", "sortButton", "stackButton"]


def sc_bag_tab_header(sc, ref, items):
    """ElvUI's real bag window (v16.01 layout, the player's profile: UIScale 0.62,
    474 wide, 42 slots, bags split, Details behind it): the Bag cleanup button
    sits in the empty left of the window's top row, clear of every ElvUI part,
    above the window, and follows it; the panel opens beside the window's top."""
    w = World(ref, items, screen=(768 * 16 / 9, 768, 0.62))
    M, ns, g = w.M, w.ns, w.g
    elv = M.InstallElvUIBagsReal()
    bag_add_item(w, 900301, "Chipped Claw", 0, 9, classID=15)
    bag_add_item(w, 900302, "Coyote Meat", 1, 4, classID=7)
    w.put_bag(0, 3, 900301, 2)
    w.put_bag(2, 5, 900302, 3)
    BP = ns.BagPanel
    w.lua.execute("ToggleAllBags()")
    w.settle(0.2)
    sc.check(BP.lastError is None, "header: no error placing the button (%s)" % BP.lastError)
    tab = BP.tab
    sc.check(BP.built and tab.IsVisible(tab), "header: button shows with ElvUI's bags")
    tr, fr, er = w.rect(tab), w.rect(elv), w.rect(elv.editBox)
    es = elv.GetEffectiveScale(elv)
    print("      [geometry] screen units (x 1/0.62 = UIParent units): window %s, button %s, search %s" % (
        tuple(round(v / es, 1) for v in fr), tuple(round(v / es, 1) for v in tr), tuple(round(v / es, 1) for v in er)))
    sc.check(tr[0] >= fr[0] and tr[2] <= fr[2] and tr[1] >= fr[1] and tr[3] <= fr[3], "header: inside ElvUI's window")
    sc.check(tr[1] >= er[3] + 2 * es - 1e-6, "header: above the search row (button bottom %s, search top %s)" % (tr[1], er[3]))
    sc.check(abs(tr[0] - er[0]) < 1e-6, "header: lined up with the search box's left edge")
    sc.check(abs((fr[3] - tr[3]) / es - 4) < 1e-6 and abs((tr[3] - tr[1]) / es - 20) < 1e-6, "header: 4 below the window top, 20 tall")
    sc.check(abs((tr[2] - tr[0]) / es - 150) < 1e-6, "header: 150 wide (%s)" % ((tr[2] - tr[0]) / es))
    for name in ELV_PARTS:
        r = w.rect(elv[name])
        sc.check(r is not None and not _overlap(tr, r), "header: clear of ElvUI's %s" % name)
    hit = [(b, s) for b in range(0, 5) for s in range(1, 17) if _overlap(tr, w.rect(elv.Bags[b][s]))]
    sc.check(not hit, "header: clear of every bag slot (%s)" % hit[:3])
    sc.eq(tab.GetFrameStrata(tab), elv.GetFrameStrata(elv), "header: same strata as ElvUI's window")
    sc.check(tab.GetFrameLevel(tab) > elv.GetFrameLevel(elv), "header: drawn above the window it sits on")
    txt = strip_codes(tab.text._text)
    sc.check(txt.startswith("Bag cleanup"), "header: says Bag cleanup (%s)" % txt)
    sc.check(tab.text.GetStringWidth(tab.text) <= (tr[2] - tr[0]) / es - 10, "header: the label fits")
    # ElvUI's bag bar (its backpack button) opens above the window: no overlap.
    M.click(elv.bagsButton)
    sc.check(elv.ContainerHolder.IsVisible(elv.ContainerHolder) and not _overlap(tr, w.rect(elv.ContainerHolder)),
             "header: clear of ElvUI's bag bar when it opens")
    M.click(elv.bagsButton)
    # Details sits behind the bags: the button is not over it.
    d = w.rect(g.DetailsBaseFrame1)
    sc.check(not _overlap(tr, d) or tr[1] > d[3], "header: not over the Details window")
    # The window grows (a new bag): the button stays at its top left.
    dx, dy = tr[0] - fr[0], fr[3] - tr[3]
    elv.SetHeight(elv, elv.GetHeight(elv) + 86)
    tr2, fr2 = w.rect(tab), w.rect(elv)
    sc.check(abs(tr2[0] - fr2[0] - dx) < 1e-6 and abs(fr2[3] - tr2[3] - dy) < 1e-6, "header: follows when the window grows")
    # Shift-drag the window: the button follows.
    elv.ClearAllPoints(elv)
    elv.SetPoint(elv, "TOPLEFT", g.UIParent, "TOPLEFT", 600, -120)
    tr3, fr3 = w.rect(tab), w.rect(elv)
    sc.check(abs(tr3[0] - fr3[0] - dx) < 1e-6 and abs(fr3[3] - tr3[3] - dy) < 1e-6, "header: follows when the window moves")
    elv.ClearAllPoints(elv)
    elv.SetPoint(elv, "BOTTOMRIGHT", g.ElvUIBagMover, "BOTTOMRIGHT", 0, 0)
    # Click: the panel opens beside the window's top, the button stays (a toggle).
    M.click(tab)
    w.settle(0.1)
    F = g.ForeverAHBagPanel
    sc.check(F.IsVisible(F) and tab.IsVisible(tab), "header: panel opens and the button stays")
    pr, fr4 = w.rect(F), w.rect(elv)
    pe = F.GetEffectiveScale(F)
    sc.check(abs(pr[2] - (fr4[0] - 4 * pe)) < 1e-6 and abs(pr[3] - fr4[3]) < 1e-6,
             "header: panel's top right is 4 left of the window's top left (%s vs %s)" % (pr, fr4))
    sc.check(not _overlap(pr, fr4), "header: panel does not cover the bags")
    sc.check(pr[0] >= 0 and pr[1] >= 0 and pr[3] <= w.M.screenH + 1e-6, "header: panel on screen (%s)" % (pr,))
    print("      [geometry] panel (UIParent units) %s" % (tuple(round(v / pe, 1) for v in pr),))
    # Bags at the left edge of the screen: the panel opens on the right.
    elv.ClearAllPoints(elv)
    elv.SetPoint(elv, "TOPLEFT", g.UIParent, "TOPLEFT", 4, -100)
    BP.Place(BP)
    pr, fr5 = w.rect(F), w.rect(elv)
    sc.check(abs(pr[0] - (fr5[2] + 4 * pe)) < 1e-6 and abs(pr[3] - fr5[3]) < 1e-6,
             "header: panel on the right when the bags sit at the left edge (%s vs %s)" % (pr, fr5))
    elv.ClearAllPoints(elv)
    elv.SetPoint(elv, "BOTTOMRIGHT", g.ElvUIBagMover, "BOTTOMRIGHT", 0, 0)
    BP.Place(BP)
    # Click again: the panel closes, the button stays.
    M.click(tab)
    w.settle(0.1)
    sc.check(not F.IsShown(F) and tab.IsVisible(tab), "header: a second click closes the panel")
    w.lua.execute("ToggleAllBags()")
    w.settle(0.2)
    sc.check(not tab.IsShown(tab) and not F.IsShown(F), "header: closing the bags hides it all")
    sc.check(BP.lastError is None, "header: no error (%s)" % BP.lastError)
    sc.clean(w, "header")

    # A narrow window and a lot of gold: no room in the header, so the tab
    # goes beside the window, still clear of everything ElvUI shows.
    w2 = World(ref, items, screen=(768 * 16 / 9, 768, 0.62))
    elv2 = w2.M.InstallElvUIBagsReal(w2.lua.eval('{ width = 220, gold = "1234567g 89s 10c  " }'))
    w2.lua.execute("ToggleAllBags()")
    w2.settle(0.2)
    BP2 = w2.ns.BagPanel
    t2, f2 = w2.rect(BP2.tab), w2.rect(elv2)
    sc.check(BP2.tab.IsVisible(BP2.tab) and not _overlap(t2, f2), "narrow: no room in the header, the tab goes beside the window")
    for name in ELV_PARTS:
        r = w2.rect(elv2[name])
        sc.check(r is None or not _overlap(t2, r), "narrow: clear of ElvUI's %s" % name)
    sc.check(BP2.lastError is None, "narrow: no error (%s)" % BP2.lastError)
    sc.clean(w2, "narrow")


# ---------------------------------------------------------------------------
# Bag cleanup: dropping a stack (asks first), and how the button keeps up with
# the bags (hooks, unreadable positions, /gl bagcheck)
# ---------------------------------------------------------------------------
def bag_rows(BP):
    return [BP.rows[i] for i in range(1, len(BP.rows) + 1) if BP.rows[i].IsShown(BP.rows[i])]


def bag_row_for(BP, iid):
    for r in bag_rows(BP):
        if r.entry is not None and r.entry.id == iid:
            return r
    return None


LINK_TEXT = re.compile(r"[|]H[^|]*[|]h(.*?)[|]h")


def plain(s):
    """Chat or tooltip text without colour codes, item links as their [Name]."""
    return LINK_TEXT.sub(lambda m: m.group(1), strip_codes(s or ""))


def destroyed_list(M):
    return [(M.destroyed[i].bag, M.destroyed[i].slot, M.destroyed[i].itemID, M.destroyed[i].count)
            for i in range(1, len(M.destroyed) + 1)]


def violations(w):
    return [w.M.violations[i] for i in range(1, len(w.M.violations) + 1)]


def sc_bag_drop(sc, ref, items):
    """Drop with confirmation: a click on a WORTH THE LEAST row (or Drop cheapest,
    or the menu) only opens the drop box, which names the stack and its worth and
    outlines the slot; Drop it, inside that click, checks the stack again, picks
    it up and calls DeleteCursorItem once; chat confirms. Drop it ignores clicks
    that come too soon (a double-click, a burst) and asks again when the stack
    is now worth more than the box said. Rare and better items and quest items
    are only picked up (the game's own check). The mock only lets
    DeleteCursorItem through inside a player click on Drop it, once."""
    w = World(ref, items)
    M, ns, g = w.M, w.ns, w.g
    bag_add_item(w, 900401, "Linen Scrap", 1, 4, classID=7, maxStack=20)
    bag_add_item(w, 900402, "Broken Fang", 0, 20, classID=15)
    bag_add_item(w, 900403, "Glinting Band", 3, 5, classID=4, maxStack=1)
    bag_add_item(w, 900405, "Spider Leg", 1, 9, classID=7, maxStack=20)
    w.put_bag(0, 1, 900401, 3)   # 12c
    w.put_bag(0, 2, 900402, 1)   # 20c, grey
    w.put_bag(0, 3, 900403, 1)   # 5c, Rare: never one-click
    w.put_bag(0, 4, 900405, 2)   # 18c
    BP = ns.BagPanel
    M.OpenBags(1)
    w.settle(0.2)
    M.click(BP.tab)
    w.settle(0.1)
    F, C = g.ForeverAHBagPanel, g.ForeverAHBagConfirm
    sc.check(F.IsVisible(F), "drop: panel open")
    sc.eq([r.entry.id for r in bag_rows(BP) if r.kind == "drop"], [900403, 900401, 900405, 900402], "drop: cheapest first")
    db = BP.dropButton
    sc.check(db.IsVisible(db) and db.text._text == "Drop cheapest", "drop: Drop cheapest shows away from a vendor")
    sc.check(not BP.sellButton.IsShown(BP.sellButton), "drop: no Sell junk away from a vendor")
    sc.check(not C.IsShown(C), "drop: the drop box starts closed")

    tt = g.GameTooltip
    r = bag_row_for(BP, 900401)
    M.enter(r)
    lines = [plain(l) for l, _ in w.all_lines(tt)]
    sc.check("Click: drop it (asks first)" in lines, "drop hover: says a click asks first (%s)" % lines)
    M.leave(r)
    rb = bag_row_for(BP, 900403)
    M.enter(rb)
    lines = [plain(l) for l, _ in w.all_lines(tt)]
    sc.check(any(l.startswith("Click: pick it up") for l in lines), "drop hover: a Rare item is only picked up")
    M.leave(rb)
    # The help on WORTH THE LEAST says what a click does, away from a vendor
    # and at one (checked against what a click does below).
    M.enter(BP.dropHelp)
    help_txt = " ".join(plain(l) for l, _ in w.all_lines(tt))
    M.leave(BP.dropHelp)
    sc.check("At a vendor, a click sells the stack if the vendor buys it" in help_txt
             and "Otherwise a click on a row (or Drop cheapest) asks before it drops" in help_txt
             and "Rare and better items and quest items are only picked up" in help_txt,
             "drop help: says what a click does, at a vendor and away (%s)" % help_txt)

    # A click asks; nothing moves yet.
    n_pick = len(M.pickups)
    M.click(r)
    sc.check(C.IsVisible(C), "drop: a click on a row opens the drop box")
    sc.eq((C.head._text, C.name._text, C.worth._text, C.note._text),
          ("Drop this for good?", "Linen Scrap x3", "Worth 12c at a vendor.", "There is no undo."), "drop box texts")
    sc.eq((g.ForeverAHBagConfirmDrop.text._text, g.ForeverAHBagConfirmCancel.text._text), ("Drop it", "Cancel"),
          "drop box buttons")
    sc.eq(len(M.pickups), n_pick, "drop: asking picks nothing up")
    sc.check(not w.lua.eval("CursorHasItem()"), "drop: cursor empty while it asks")
    btn = g["ContainerFrame1Item%d" % (17 - 1)]
    sc.check(BP.glow.IsShown(BP.glow) and w.same(BP.glow._points[1][2], btn), "drop: the bag slot is outlined while it asks")
    M.leave(r)
    sc.check(BP.glow.IsShown(BP.glow), "drop: the outline stays while it asks")
    sc.check(F.GetHeight(F) >= 120 - 1e-6, "drop: the panel is tall enough for the box (%s)" % F.GetHeight(F))
    sc.eq(C.GetFrameStrata(C), F.GetFrameStrata(F), "drop: the box is on the panel's layer")
    sc.check(C.GetFrameLevel(C) > max(BP.rows[i].GetFrameLevel(BP.rows[i]) for i in range(1, len(BP.rows) + 1)),
             "drop: the box is drawn over the rows")
    # The second click of a double-click lands on Drop it: ignored.
    M.click(g.ForeverAHBagConfirmDrop)
    sc.eq(len(M.destroyed), 0, "drop: a double-click never drops")
    sc.check(C.IsVisible(C), "drop: box still asks after the double-click")
    M.click(g.ForeverAHBagConfirmCancel)
    sc.check(not C.IsShown(C) and BP.confirm is None, "drop: Cancel closes it")
    sc.check(not BP.glow.IsShown(BP.glow), "drop: outline goes with Cancel")
    sc.eq((len(M.destroyed), len(M.pickups)), (0, n_pick), "drop: Cancel drops and moves nothing")

    # Ask again. Drop it sits over rows 2 and 3 of the list, so the second
    # click of a double-click on such a row lands on it: a slow double-click
    # (the OS counts up to 0.5 s) never drops, nor does a burst of clicks
    # (every click that comes too soon starts the wait again).
    M.click(r)
    w.settle(0.45)
    M.click(g.ForeverAHBagConfirmDrop)
    sc.check(len(M.destroyed) == 0 and C.IsShown(C), "drop: a slow double-click (0.45 s apart) never drops")
    w.settle(0.3)
    M.click(g.ForeverAHBagConfirmDrop)
    sc.check(len(M.destroyed) == 0 and C.IsShown(C),
             "drop: a burst of clicks never drops (0.75 s after the box opened, 0.3 s after the last click)")
    # Wait, Drop it.
    w.settle(0.6)
    mark = w.chat_mark()
    M.click(g.ForeverAHBagConfirmDrop)
    sc.eq(destroyed_list(M), [(0, 1, 900401, 3)], "drop: Drop it destroys exactly that stack")
    sc.eq([(M.pickups[i][1], M.pickups[i][2]) for i in range(n_pick + 1, len(M.pickups) + 1)], [(0, 1)],
          "drop: one pickup, of that slot, in the same click")
    sc.check(not w.lua.eval("CursorHasItem()") and not C.IsShown(C), "drop: cursor empty and box closed after")
    w.settle(1.0)
    txt = [plain(x) for x in w.chat(mark)]
    sc.check(any(t.endswith("dropped [Linen Scrap] x3 (worth 12c).") for t in txt), "drop: chat says what went (%s)" % txt)
    sc.check(M.bags[0][1] is None and bag_row_for(BP, 900401) is None, "drop: gone from the bag and the list")

    # Drop cheapest: skips the Rare band, names the next. A double-click on it never drops.
    M.click(db)
    sc.eq(C.name._text, "Spider Leg x2", "drop cheapest: names the cheapest stack it may drop")
    M.click(g.ForeverAHBagConfirmDrop)
    sc.check(len(M.destroyed) == 1 and C.IsShown(C), "drop cheapest: a double-click does not drop")
    w.settle(0.6)
    M.click(g.ForeverAHBagConfirmDrop)
    w.settle(1.0)
    sc.eq(destroyed_list(M)[-1], (0, 4, 900405, 2), "drop cheapest: dropped it")

    def ask(iid):
        rr = bag_row_for(BP, iid)
        M.click(rr)
        w.settle(0.6)
        return rr

    def no_drop(label, n_destroyed, must):
        mark = w.chat_mark()
        M.click(g.ForeverAHBagConfirmDrop)
        w.settle(0.6)
        sc.eq(len(M.destroyed), n_destroyed, label + ": nothing destroyed")
        txt = [plain(x) for x in w.chat(mark)]
        sc.check(any(must in t for t in txt), "%s: chat says so (%s)" % (label, txt))

    nd = len(M.destroyed)
    # Changed between the box and Drop it (no event in between).
    ask(900402)
    M.bags[0][2].count = 2
    no_drop("drop changed count", nd, "moved or changed, so nothing was dropped")
    M.bags[0][2].count = 1
    M.fire("BAG_UPDATE", 0)
    w.settle(0.5)
    ask(900402)
    M.bags[0][2].locked = True
    no_drop("drop locked", nd, "moved or changed, so nothing was dropped")
    M.bags[0][2].locked = None
    M.fire("BAG_UPDATE", 0)
    w.settle(0.5)
    # Same item and count with another random suffix: not the stack it asked about.
    ask(900402)
    M.bags[0][2].suffix = "777"
    no_drop("drop suffix swap", nd, "moved or changed, so nothing was dropped")
    M.bags[0][2].suffix = None
    M.fire("BAG_UPDATE", 0)
    w.settle(0.5)
    # Moved, with the bag events: the box closes by itself.
    ask(900402)
    mark = w.chat_mark()
    M.bags[0][9] = M.bags[0][2]
    M.bags[0][2] = None
    M.fire("BAG_UPDATE", 0)
    M.fire("BAG_UPDATE_DELAYED")
    w.settle(0.6)
    sc.check(not C.IsShown(C) and BP.confirm is None, "drop: the box closes when its stack moves")
    sc.check(any("moved or changed" in t for t in [plain(x) for x in w.chat(mark)]), "drop moved: chat says so")
    sc.eq(len(M.destroyed), nd, "drop moved: nothing destroyed")
    # Kept meanwhile: the fresh check at Drop it leaves it.
    ask(900402)
    w.slash("/gl keep Broken Fang")
    w.settle(0.5)
    if sc.check(C.IsShown(C), "drop kept: box still open to test"):
        no_drop("drop kept", nd, "is not on the list any more, so nothing was dropped")
    w.slash("/gl keep Broken Fang")
    w.settle(0.5)
    # Something on the cursor already: left alone.
    ask(900402)
    M.cursorItem = w.to_lua({"itemID": 4306, "count": 1})
    n_pick = len(M.pickups)
    no_drop("drop cursor busy", nd, "put down the item on your cursor first")
    sc.check(M.cursorItem is not None and len(M.pickups) == n_pick, "drop cursor busy: leaves your cursor alone")
    w.lua.execute("ClearCursor()")
    # The game refuses the delete: the item stays on the cursor.
    M.deleteMode = "blocked"
    ask(900402)
    no_drop("drop blocked", nd, "It is on your cursor")
    sc.check(M.held is not None and M.held.slot == 9, "drop blocked: left on the cursor")
    w.lua.execute("ClearCursor()")
    M.deleteMode = "ok"
    # The game will not pick it up: nothing is deleted (the cursor holds
    # nothing to delete), and chat says why instead of waiting for the slot.
    M.pickupRefused = True
    ask(900402)
    no_drop("drop pickup refused", nd, "could not pick up [Broken Fang]. Nothing was dropped.")
    sc.check(not w.lua.eval("CursorHasItem()") and M.bags[0][9] is not None, "drop pickup refused: cursor empty, still in the bag")
    M.pickupRefused = None
    # The server keeps it.
    M.deleteMode = "refused"
    w.settle(0.5)
    ask(900402)
    mark = w.chat_mark()
    M.click(g.ForeverAHBagConfirmDrop)
    w.settle(1.5)
    txt = [plain(x) for x in w.chat(mark)]
    sc.check(any(t.endswith("[Broken Fang] is still in your bags: the game did not destroy it.") for t in txt),
             "drop refused: says so (%s)" % txt)
    sc.check(M.bags[0][9] is not None, "drop refused: still in the bag")
    M.deleteMode = "ok"
    # Rare: a click only picks it up (the game asks to type DELETE), and chat
    # says how to finish (this is the first pick-up, so the hint shows).
    mark = w.chat_mark()
    M.click(bag_row_for(BP, 900403))
    sc.check(not C.IsShown(C), "drop rare: no drop box")
    sc.eq((M.pickups[len(M.pickups)][1], M.pickups[len(M.pickups)][2]), (0, 3), "drop rare: picked up")
    txt = [plain(x) for x in w.chat(mark)]
    sc.check(any("Click the ground to destroy it (the game asks first)" in t for t in txt),
             "drop rare: chat says how to destroy it (%s)" % txt)
    w.lua.execute("ClearCursor()")
    w.settle(0.5)
    # The menu: Drop it (asks first) for a stack it may drop, never for the Rare one.
    install_menu_mock(w)
    M.click(bag_row_for(BP, 900402), "RightButton")
    texts = menu_texts(w)
    sc.check("Drop it (asks first)" in texts and "Pick it up" in texts, "drop menu: asks-first entry (%s)" % texts)
    menu_pick(w, "Drop it (asks first)")
    sc.check(C.IsShown(C) and C.name._text == "Broken Fang", "drop menu: opens the drop box")
    M.click(g.ForeverAHBagConfirmCancel)
    M.click(bag_row_for(BP, 900403), "RightButton")
    sc.check("Drop it (asks first)" not in menu_texts(w), "drop menu: none for a Rare item")
    # Escape closes the box.
    ask(900402)
    M.pressEscape()
    sc.check(not C.IsShown(C) and BP.confirm is None, "drop: Escape closes the box")
    # Closing the bags closes the box.
    ask(900402)
    M.CloseBags()
    w.settle(0.2)
    sc.check(not C.IsVisible(C) and BP.confirm is None, "drop: closing the bags closes the box")
    M.OpenBags(1)
    w.settle(0.3)
    sc.check(F.IsVisible(F) and not C.IsShown(C), "drop: reopening shows the list, not the box")
    # The menu still open when the bags close: picking Drop it (asks first)
    # then does nothing, so reopening shows the list, not a box waiting with
    # Drop it over the rows.
    M.click(bag_row_for(BP, 900402), "RightButton")
    M.CloseBags()
    w.settle(0.3)
    sc.check(menu_pick(w, "Drop it (asks first)"), "drop menu after the bags closed: the entry was there")
    sc.check(BP.confirm is None and not C.IsShown(C), "drop menu after the bags closed: nothing asks")
    w.settle(1.0)
    M.OpenBags(1)
    w.settle(0.3)
    sc.check(F.IsVisible(F) and not C.IsShown(C) and BP.confirm is None,
             "drop menu after the bags closed: reopening shows the list, not the box")
    # At a vendor: Sell junk instead of Drop cheapest, and a click on a row
    # sells it, a Rare one too (as the help says).
    M.OpenMerchant()
    w.settle(0.5)
    sc.check(not BP.dropButton.IsShown(BP.dropButton) and BP.sellButton.IsShown(BP.sellButton),
             "drop vendor: Sell junk, no Drop cheapest")
    n_used = len(M.used)
    M.click(bag_row_for(BP, 900403))
    w.settle(0.5)
    sc.check(not C.IsShown(C) and len(M.used) == n_used + 1 and 900403 in [M.sold[i] for i in range(1, len(M.sold) + 1)],
             "drop vendor: a click on a row sells it, no drop box")
    M.CloseMerchant()
    w.settle(0.5)
    sc.eq(len(M.destroyed), nd, "drop: nothing else was destroyed")
    sc.clean(w, "drop")

    # The mock guard itself: a Drop it that is not a player click is blocked.
    w2 = World(ref, items)
    bag_add_item(w2, 900401, "Linen Scrap", 1, 4, classID=7, maxStack=20)
    w2.put_bag(0, 1, 900401, 3)
    w2.M.OpenBags(1)
    w2.settle(0.2)
    w2.M.click(w2.ns.BagPanel.tab)
    w2.settle(0.1)
    w2.M.click(bag_row_for(w2.ns.BagPanel, 900401))
    w2.settle(0.6)
    w2.lua.execute("ForeverAHBagConfirmDrop:Click()")
    v = violations(w2)
    sc.check(any("outside a click" in x for x in v), "mock: Drop it clicked from code is blocked (%s)" % v)
    sc.check(len(w2.M.destroyed) == 0 and w2.M.held is not None, "mock: nothing destroyed, item left on the cursor")

    # A quest item the auction house trades stays on the list (valued like any
    # other) but is never dropped in one click; at a vendor, a stack a vendor
    # will not buy still opens the drop box.
    priced = [i for i in other_items(ref, items, 80, avoid=SIDEBAR_WATCH)
              if ref.items[i]["cheapest"] >= 3 and ref.items[i]["market"] > 0]
    if not sc.check(len(priced) >= 2, "drop: two priced items to test with"):
        return
    q, nv = priced[0], priced[1]
    w3 = World(ref, items)
    M3, g3 = w3.M, w3.g
    M3["items"][q].classID = 12
    M3["items"][q].quality = 1
    M3["items"][nv].quality = 1
    M3["items"][nv].vendor = 0
    w3.put_bag(0, 1, q, 1)
    w3.put_bag(0, 2, nv, 1)
    BP3 = w3.ns.BagPanel
    M3.OpenBags(1)
    w3.settle(0.2)
    M3.click(BP3.tab)
    w3.settle(0.1)
    rq = bag_row_for(BP3, q)
    C3 = g3.ForeverAHBagConfirm
    if sc.check(rq is not None and rq.kind == "drop", "drop quest: a quest item the AH trades is on the list"):
        sc.check(rq.entry.questItem is True, "drop quest: marked a quest item")
        M3.enter(rq)
        lines = [plain(l) for l, _ in w3.all_lines(g3.GameTooltip)]
        sc.check(any(l.startswith("Click: pick it up") for l in lines), "drop quest: hover says it is picked up")
        M3.leave(rq)
        M3.click(rq)
        sc.check(not C3.IsShown(C3), "drop quest: no drop box")
        sc.eq((M3.pickups[len(M3.pickups)][1], M3.pickups[len(M3.pickups)][2]), (0, 1), "drop quest: picked up")
        w3.lua.execute("ClearCursor()")
        w3.settle(0.5)
    M3.OpenMerchant()
    w3.settle(0.5)
    rn = bag_row_for(BP3, nv)
    if sc.check(rn is not None and (rn.entry.vendor or 0) == 0, "drop vendor: a stack no vendor buys is listed"):
        n_used = len(M3.used)
        M3.click(rn)
        sc.check(C3.IsShown(C3) and len(M3.used) == n_used, "drop vendor: its click opens the drop box, sells nothing")
        # The name in its link fills in later (the item part stays): still the same stack.
        M3["items"][nv].name = M3["items"][nv].name + " Fresh"
        w3.settle(0.6)
        M3.click(g3.ForeverAHBagConfirmDrop)
        w3.settle(1.0)
        sc.eq([d[2] for d in destroyed_list(M3)], [nv], "drop vendor: Drop it drops it")
    M3.CloseMerchant()
    w3.settle(0.5)
    sc.clean(w3, "drop quest/vendor")

    # A short list: the panel grows to fit the box, stays that tall when the
    # list is laid out again while the box asks, and shrinks back after Cancel.
    w4 = World(ref, items)
    M4, g4 = w4.M, w4.g
    bag_add_item(w4, 900401, "Linen Scrap", 1, 4, classID=7, maxStack=20)
    bag_add_item(w4, 6948, "Hearthstone", 1, 0, classID=15)
    w4.put_bag(0, 1, 900401, 3)
    BP4 = w4.ns.BagPanel
    M4.OpenBags(1)
    w4.settle(0.2)
    M4.click(BP4.tab)
    w4.settle(0.1)
    F4, C4 = g4.ForeverAHBagPanel, g4.ForeverAHBagConfirm
    short = BP4.layoutH
    sc.check(short is not None and short < 120 and abs(F4.GetHeight(F4) - short) < 1e-6,
             "drop short list: the list is shorter than the box (%s)" % short)
    M4.click(bag_row_for(BP4, 900401))
    sc.check(C4.IsShown(C4) and F4.GetHeight(F4) >= 120 - 1e-6, "drop short list: the panel grows to fit the box")
    # Something else lands in the bags (no row of its own): the list is laid out again.
    free_before = BP4.sub._text
    w4.put_bag(1, 1, 6948, 1)
    M4.fire("BAG_UPDATE", 1)
    M4.fire("BAG_UPDATE_DELAYED")
    w4.settle(0.5)
    sc.check(BP4.sub._text != free_before and BP4.layoutH == short, "drop short list: laid out again (%s, %s)" % (
        BP4.sub._text, BP4.layoutH))
    sc.check(C4.IsShown(C4) and F4.GetHeight(F4) >= 120 - 1e-6,
             "drop short list: still tall enough for the box after that (%s)" % F4.GetHeight(F4))
    M4.click(g4.ForeverAHBagConfirmCancel)
    sc.check(not C4.IsShown(C4) and abs(F4.GetHeight(F4) - short) < 1e-6,
             "drop short list: back to the list's height after Cancel (%s)" % F4.GetHeight(F4))
    # A slow server: the slot stays locked for 1.2 s after Drop it. Chat waits
    # for the slot and says dropped, once (never "still in your bags").
    M4.deleteDelay = 1.2
    M4.click(bag_row_for(BP4, 900401))
    w4.settle(0.6)
    mark = w4.chat_mark()
    M4.click(g4.ForeverAHBagConfirmDrop)
    w4.settle(0.6)
    sc.check(M4.bags[0][1] is not None and M4.bags[0][1].locked is True, "drop slow server: the slot is still locked after 0.6 s")
    w4.settle(2.4)
    txt = [plain(x) for x in w4.chat(mark)]
    sc.eq(destroyed_list(M4), [(0, 1, 900401, 3)], "drop slow server: dropped")
    sc.check(len([t for t in txt if t.endswith("dropped [Linen Scrap] x3 (worth 12c).")]) == 1
             and not any("still in your bags" in t for t in txt),
             "drop slow server: chat waits for the slot and says dropped once (%s)" % txt)
    M4.deleteDelay = 0.2
    sc.clean(w4, "drop short list")

    # The worth goes up while the box asks: Drop it then shows the new worth
    # and asks again instead of dropping. In game that is a live auction price
    # coming in (at the auction house the bags are open too). First the vendor
    # price goes up, which tests that check in Drop it however auction prices
    # are read; then a live listing comes in for an item valued at auction.
    def worth_text(e):
        if e.source == "auction":
            return "Worth %s%s at auction now." % ("about " if e.unsure else "", money_text(e.value))
        return "Worth %s at a vendor." % money_text(e.value)

    w5 = World(ref, items)
    M5, g5, ns5 = w5.M, w5.g, w5.ns
    hi = None
    for iid in priced:
        M5["items"][iid].quality = 1
        M5["items"][iid].classID = 7
        w5.put_bag(0, 2, iid, 1)
        s5 = ns5.BagScan()
        if len(s5.drop) == 1 and s5.drop[1].source == "auction":
            hi = iid
            break
        M5.bags[0][2] = None
    bag_add_item(w5, 900406, "Gold Dust", 1, 4, classID=7, maxStack=20)
    w5.put_bag(0, 1, 900406, 3)   # 12c at a vendor
    w5.open_ah_modern()
    BP5 = ns5.BagPanel
    M5.OpenBags(1)
    w5.settle(0.2)
    M5.click(BP5.tab)
    w5.settle(0.1)
    C5, yes5 = g5.ForeverAHBagConfirm, g5.ForeverAHBagConfirmDrop
    M5.click(bag_row_for(BP5, 900406))
    sc.eq(C5.worth._text, "Worth 12c at a vendor.", "drop worth: the box says what it is worth")
    M5["items"][900406].vendor = 500
    w5.settle(0.6)
    mark = w5.chat_mark()
    M5.click(yes5)
    sc.eq(len(M5.destroyed), 0, "drop worth: nothing dropped while it is worth more than the box said")
    sc.check(C5.IsShown(C5) and C5.worth._text == "Worth 15s at a vendor.", "drop worth: the box shows the new worth (%s)" % C5.worth._text)
    txt = [plain(x) for x in w5.chat(mark)]
    sc.check(any(t.endswith("[Gold Dust] x3 is worth 15s at a vendor now. Nothing was dropped: check the box again.") for t in txt),
             "drop worth: chat says why (%s)" % txt)
    # Asked again, so the wait starts again too; then Drop it drops it.
    M5.click(yes5)
    sc.eq(len(M5.destroyed), 0, "drop worth: a click right after it asks again does nothing")
    w5.settle(0.6)
    mark = w5.chat_mark()
    M5.click(yes5)
    w5.settle(1.0)
    sc.eq(destroyed_list(M5), [(0, 1, 900406, 3)], "drop worth: Drop it on the new worth drops it")
    txt = [plain(x) for x in w5.chat(mark)]
    sc.check(any(t.endswith("dropped [Gold Dust] x3 (worth 15s).") for t in txt),
             "drop worth: chat names the worth it was dropped at (%s)" % txt)
    # A live listing far above the usual price comes in while the box asks.
    # Whether that raises the stack's worth depends on how auction prices are
    # read; either way Drop it never destroys it for more than the box said.
    r5 = bag_row_for(BP5, hi) if hi is not None else None
    if sc.check(r5 is not None, "drop worth live: an item valued at its auction price to test with"):
        old = (r5.entry.value, r5.entry.source)
        M5.click(r5)
        sc.eq(C5.worth._text, worth_text(r5.entry), "drop worth live: the box says what it is worth")
        w5.set_commodity_results(hi, [(ref.items[hi]["cheapest"] * 3 + 100, 5, False)])
        M5.fire("COMMODITY_SEARCH_RESULTS_UPDATED", hi)
        w5.settle(0.6)
        s5 = ns5.BagScan()
        new = next((s5.drop[i] for i in range(1, len(s5.drop) + 1) if s5.drop[i].id == hi), None)
        n5 = len(M5.destroyed)
        mark = w5.chat_mark()
        M5.click(yes5)
        w5.settle(1.0)
        txt = [plain(x) for x in w5.chat(mark)]
        if new is not None and (new.value > old[0] or new.source != old[1]):
            sc.check(len(M5.destroyed) == n5 and C5.IsShown(C5) and C5.worth._text == worth_text(new)
                     and any(" is worth %s " % money_text(new.value) in t for t in txt),
                     "drop worth live: worth more now (%s to %s), so it asks again (%s, %s)" % (
                         old[0], new.value, C5.worth._text, txt))
        else:
            sc.check(len(M5.destroyed) == n5 + 1, "drop worth live: worth no more than the box said, so it drops (%s)" % txt)
    w5.close_ah_modern()
    w5.settle(0.5)
    sc.clean(w5, "drop worth")


def sc_bag_drop_elvui(sc, ref, items):
    """The drop box with ElvUI's bags: the ElvUI slot is outlined while it asks,
    Drop it destroys the stack."""
    w = World(ref, items)
    M, ns, g = w.M, w.ns, w.g
    elv = M.InstallElvUIBags(0.8)
    bag_add_item(w, 900302, "Coyote Meat", 1, 4, classID=7)
    w.put_bag(2, 5, 900302, 3)
    BP = ns.BagPanel
    w.lua.execute("ToggleAllBags()")
    w.settle(0.2)
    M.click(BP.tab)
    w.settle(0.1)
    r = bag_row_for(BP, 900302)
    M.click(r)
    sc.check(w.same(BP.glow._points[1][2], elv.Bags[2][5]), "elvui drop: ElvUI's slot outlined while it asks")
    w.settle(0.6)
    M.click(g.ForeverAHBagConfirmDrop)
    w.settle(1.0)
    sc.eq(destroyed_list(M), [(2, 5, 900302, 3)], "elvui drop: dropped")
    sc.check(M.bags[2][5] is None, "elvui drop: gone from the bag")
    sc.clean(w, "elvui drop")


def elvui_world(ref, items, build_on=None, window=None):
    """The game's own bag functions and ElvUI the addon, loaded before GoblinLedger,
    at the player's UI scale."""
    def setup(w):
        w.M.InstallBlizzardBags()
        o = {}
        if build_on:
            o["buildOn"] = build_on
        if window:
            o["window"] = window
        w.M.InstallElvUIAddon(w.to_lua(o))
    return World(ref, items, screen=(768 * 16 / 9, 768, 0.62), before_addon=setup)


def tab_on(w, BP, frame):
    """The button is shown and hangs on frame."""
    tab = BP.tab
    if not BP.built or not tab.IsVisible(tab):
        return False
    p = tab._points[1]
    return p is not None and w.same(p[2], frame)


def sc_bag_elvui_hooks(sc, ref, items):
    """How the button learns that bags opened or closed, with ElvUI v16.01 as it
    runs in the game: ElvUI builds its window at login (before our login
    handler), clears the scripts of the game's bag windows (which drops our
    script hooks), hooks the game's bag functions, and also opens its window by
    itself. Every way in shows the button, every way out hides it."""
    w = elvui_world(ref, items)
    M, ns, g = w.M, w.ns, w.g
    BP = ns.BagPanel
    st = BP.stats
    elv = g.ElvUI_ContainerFrame
    B = w.lua.eval("ElvUI[1]:GetModule('Bags')")
    sc.check(M.elvBuilt is True and elv is not None, "elvui hooks: ElvUI built its bag window at login")
    sc.check(st.elv.OpenBags is True and st.elv.CloseAllBags is True, "elvui hooks: ElvUI's OpenBags and CloseAllBags hooked")
    sc.check(all(st.toggles[fn] is True for fn in ("ToggleBag", "ToggleBackpack", "ToggleAllBags", "OpenAllBags",
                                                    "CloseAllBags", "OpenBackpack", "CloseBackpack")),
             "elvui hooks: every bag function of the game hooked")
    cf1 = g.ContainerFrame1
    sc.check(cf1._hooks.OnShow is None and cf1._scripts.OnShow is None,
             "elvui hooks: ElvUI cleared the game's bag scripts, and our script hooks with them")
    n_hooks = M.hookCount
    M.fire("PLAYER_ENTERING_WORLD", False, True)
    w.settle(0.2)
    sc.eq(M.hookCount, n_hooks, "elvui hooks: entering the world again hooks nothing twice")

    def opened(label):
        w.settle(0.2)
        sc.check(elv.IsVisible(elv) and tab_on(w, BP, elv), "%s: the button shows on ElvUI's window" % label)
        sc.check(BP.inHeader is True and BP.tab.GetFrameStrata(BP.tab) == "HIGH",
                 "%s: in the window's header, on its layer" % label)
        sc.check(BP.lastError is None, "%s: no error (%s)" % (label, BP.lastError))

    def closed(label):
        w.settle(0.2)
        sc.check(not elv.IsShown(elv) and not BP.tab.IsShown(BP.tab), "%s: closed, the button goes" % label)

    w.lua.execute("ToggleAllBags()")
    opened("B key")
    sc.check(cf1.IsShown(cf1) and cf1.GetNumPoints(cf1) == 0, "B key: the game's backpack is open but parked (no position)")
    w.lua.execute("ToggleAllBags()")
    closed("B key again")
    w.lua.execute("ElvUI[1]:GetModule('Bags'):OpenBags()")
    opened("ElvUI opens by itself (vendor, auction house)")
    w.lua.execute("ElvUI[1]:GetModule('Bags'):CloseAllBags()")
    closed("ElvUI closes by itself")
    w.lua.execute("ToggleBackpack()")
    opened("ToggleBackpack")
    w.lua.execute("ToggleBackpack()")
    closed("ToggleBackpack again")
    w.lua.execute("ToggleBag(0)")
    opened("ToggleBag (the bag bar)")
    w.lua.execute("ToggleBag(0)")
    closed("ToggleBag again")
    w.lua.execute("OpenAllBags(true)")
    opened("OpenAllBags")
    w.lua.execute("CloseAllBags()")
    closed("CloseAllBags")
    w.lua.execute("ToggleAllBags()")
    opened("B key before Escape")
    sc.eq(M.pressEscape(), "ElvUI_ContainerFrame", "Escape: closes ElvUI's window")
    closed("Escape")
    # Shown straight from code (no bag function, no ElvUI module call).
    w.lua.execute("ElvUI_ContainerFrame:Show()")
    opened("window shown from code")
    w.lua.execute("ElvUI_ContainerFrame:Hide()")
    closed("window hidden from code")
    sc.check(BP.lastError is None, "elvui hooks: no error (%s)" % BP.lastError)
    sc.clean(w, "elvui hooks")

    # ElvUI builds its window late (after login and entering the world) and
    # first opens it by itself: only the module hook can tell.
    w2 = elvui_world(ref, items, build_on="BAG_UPDATE_DELAYED")
    BP2 = w2.ns.BagPanel
    sc.check(w2.g.ElvUI_ContainerFrame is None, "late: no ElvUI window at login")
    sc.check(BP2.stats.elv.OpenBags is True, "late: ElvUI's module hooked at login anyway")
    w2.M.fire("BAG_UPDATE_DELAYED")
    w2.settle(0.2)
    elv2 = w2.g.ElvUI_ContainerFrame
    sc.check(elv2 is not None and not elv2.IsShown(elv2), "late: ElvUI built its window")
    w2.lua.execute("ElvUI[1]:GetModule('Bags'):OpenBags()")
    w2.settle(0.5)
    sc.check(tab_on(w2, BP2, elv2), "late: ElvUI opening its window by itself shows the button")
    w2.lua.execute("ElvUI[1]:GetModule('Bags'):CloseAllBags()")
    w2.settle(0.2)
    sc.check(not BP2.tab.IsShown(BP2.tab), "late: and closing it hides the button")
    sc.clean(w2, "late elvui")

    # The game's bags with their scripts cleared by another addon: the Show and
    # Hide hooks still see them open and close.
    w3 = World(ref, items)
    M3, g3 = w3.M, w3.g
    BP3 = w3.ns.BagPanel
    w3.lua.execute('ContainerFrame1:SetScript("OnShow", nil); ContainerFrame1:SetScript("OnHide", nil)')
    sc.check(g3.ContainerFrame1._hooks.OnShow is None, "cleared scripts: our script hooks are gone")
    asked = BP3.stats.asked
    M3.OpenBags(1)
    w3.settle(0.2)
    sc.check(BP3.built and tab_on(w3, BP3, g3.ContainerFrame1), "cleared scripts: the button still shows with the backpack")
    sc.check(BP3.stats.asked > asked, "cleared scripts: the update was asked for")
    M3.CloseBags()
    w3.settle(0.2)
    sc.check(not BP3.tab.IsShown(BP3.tab), "cleared scripts: and goes when it closes")
    # A bag function that appears after login is hooked the next time we look.
    sc.check(BP3.stats.toggles.OpenBackpack is None, "late function: OpenBackpack is not there at first")
    w3.lua.execute("function OpenBackpack() MOCK.OpenBags(1) end")
    M3.fire("PLAYER_ENTERING_WORLD", False, True)
    sc.check(BP3.stats.toggles.OpenBackpack is True, "late function: hooked on entering the world")
    w3.lua.execute('ContainerFrame1.Show = nil')   # only the function hook is left to tell
    asked = BP3.stats.asked
    w3.lua.execute("OpenBackpack()")
    w3.settle(0.2)
    sc.check(BP3.stats.asked > asked and tab_on(w3, BP3, g3.ContainerFrame1), "late function: opening through it shows the button")
    M3.CloseBags()
    w3.settle(0.2)
    sc.clean(w3, "cleared scripts")


SECRET = -987654.25


def secret_rect(w, name):
    """A frame whose position comes back as secret values (the mock's issecretvalue knows them)."""
    w.lua.execute("""
        local S = %r
        issecretvalue = function(v) return v == S end
        local f = %s
        f.GetLeft = function() return S end
        f.GetBottom = function() return S end
        f.GetRight = function() return S end
        f.GetTop = function() return S end
        f.GetRect = function() return S, S, S, S end
    """ % (SECRET, name))


def sc_bag_window_unreadable(sc, ref, items):
    """A bag addon's window counts as open when it is shown, even when its
    position cannot be read: no position (GetLeft nil) or secret values. The
    button still shows: in the header when the window's rectangle can be read,
    else hung off the window's bottom left corner. The game's own bag windows
    still need a position (bag addons park them shown with none)."""
    w = World(ref, items, screen=(768 * 16 / 9, 768, 0.62))
    M, ns, g = w.M, w.ns, w.g
    elv = M.InstallElvUIBagsReal()
    bag_add_item(w, 900302, "Coyote Meat", 1, 4, classID=7)
    w.put_bag(2, 5, 900302, 3)
    BP = ns.BagPanel
    w.lua.execute("ElvUI_ContainerFrame.GetLeft = function() return nil end")
    w.lua.execute("ToggleAllBags()")
    w.settle(0.2)
    sc.check(tab_on(w, BP, elv), "no GetLeft: the button shows on ElvUI's window")
    sc.check(BP.inHeader is True and BP.tab._points[1][1] == "TOPLEFT", "no GetLeft: still in the header (its rectangle reads)")
    w.lua.execute("ToggleAllBags()")
    w.settle(0.2)
    sc.check(not BP.tab.IsShown(BP.tab), "no GetLeft: closing hides it")

    secret_rect(w, "ElvUI_ContainerFrame")
    w.lua.execute("ToggleAllBags()")
    w.settle(0.2)
    tab = BP.tab
    sc.check(tab_on(w, BP, elv), "secret position: the button shows on ElvUI's window")
    p = tab._points[1]
    sc.check(not BP.inHeader and p[1] == "BOTTOMRIGHT" and p[3] == "BOTTOMLEFT" and p[4] == -4 and p[5] == 0,
             "secret position: hung off the window's bottom left corner, no numbers needed (%s)" % (list(p.values()) if p else p,))
    sc.eq(tab.GetFrameStrata(tab), "HIGH", "secret position: on the window's layer")
    tr, er = w.rect(tab), w.rect(elv)
    sc.check(tr is not None and abs(tr[2] - (er[0] - 4 * tab.GetEffectiveScale(tab))) < 1e-6,
             "secret position: the button's right edge 4 left of the window")
    M.click(tab)
    w.settle(0.1)
    F = g.ForeverAHBagPanel
    pf = F._points[1]
    sc.check(F.IsVisible(F) and w.same(pf[2], elv) and pf[1] == "BOTTOMRIGHT", "secret position: the panel opens beside it")
    out = [plain(x) for x in w.slash("/gl bagcheck")]
    sc.check(any("1. ElvUI bags: open yes, secret position" in t for t in out), "secret position: bagcheck says so (%s)" % out[:3])
    sc.check(BP.lastError is None, "secret position: no error (%s)" % BP.lastError)
    w.lua.execute("issecretvalue = function() return false end")
    sc.clean(w, "unreadable window", ok_chat=BAGCHECK_LINES)

    # The game's own bag window, shown with no position (parked): never used.
    w2 = World(ref, items)
    w2.lua.execute("ContainerFrame1:SetID(0); ContainerFrame1:ClearAllPoints(); ContainerFrame1:Show()")
    w2.settle(0.2)
    BP2 = w2.ns.BagPanel
    sc.check(not BP2.built or not BP2.tab.IsShown(BP2.tab), "parked game bags: no button")
    sc.eq(BP2.stats.result, "no bag window open", "parked game bags: the update says why")
    sc.clean(w2, "parked game bags")


# /gl bagcheck lines that name errors only to say there are none, or that
# the game hides them: not error messages (Scenario.clean ok_chat).
BAGCHECK_LINES = ("7. last error: none", "9. Lua errors on screen: ")


def bagcheck(sc, w, label, cmd="/gl bagcheck"):
    """Runs /gl bagcheck; checks its form; returns {number: text} and all lines."""
    out = [plain(x) for x in w.slash(cmd)]
    body = [t.split("Goblin Ledger: ", 1)[-1] for t in out]
    sc.check(1 <= len(out) <= 12, "%s: at most 12 lines (%d)" % (label, len(out)))
    sc.check(all(all(ord(c) < 128 for c in t) for t in out), "%s: plain ASCII" % label)
    sc.check(body and body[0] == "bag check, Bags.lua 2026-11-18g", "%s: names the Bags.lua build (%s)" % (label, body[:1]))
    num = {}
    for t in body:
        m = re.match(r"^(\d+)\. (.*)$", t)
        if m:
            num[int(m.group(1))] = m.group(2)
    sc.eq(sorted(num), list(range(1, 11)), "%s: lines 1 to 10" % label)
    return num, body


def sc_bag_check(sc, ref, items):
    """/gl bagcheck (also /gl bags check): what the bag cleanup sees, in plain
    short lines for one screenshot, with the counters behind it; the button
    flashes gold on top for 10 seconds, then gets its layer and colour back."""
    w = World(ref, items)
    M, ns, g = w.M, w.ns, w.g
    BP = ns.BagPanel
    bag_add_item(w, 900303, "Brittle Bone", 0, 3, classID=15)
    w.put_bag(0, 1, 900303, 2)
    n, body = bagcheck(sc, w, "bagcheck closed")
    sc.eq(n.get(1), "ElvUI bags: not found (ElvUI is not loaded)", "bagcheck closed: no ElvUI")
    sc.eq(n.get(2), "game bag windows open: none", "bagcheck closed: no bag window")
    sc.eq(n.get(3), "windows the button uses: none (open your bags, then type /gl bagcheck again)", "bagcheck closed: none used")
    sc.eq(n.get(4), "key hooks: ToggleBag yes, ToggleBackpack yes, ToggleAllBags yes, OpenAllBags yes, CloseAllBags yes, "
          "OpenBackpack no, CloseBackpack no, ElvUI not loaded", "bagcheck closed: hooks")
    sc.check((n.get(5) or "").endswith("last: no bag window open"), "bagcheck closed: last update (%s)" % n.get(5))
    sc.eq(n.get(6), "button: not made yet", "bagcheck closed: no button yet")
    sc.eq(n.get(7), "last error: none", "bagcheck closed: no error")
    sc.eq(n.get(8), "scan: 79 of 80 slots free, 1 to clear, 1 cheap stack", "bagcheck closed: the scan")
    sc.eq(n.get(9), "Lua errors on screen: off", "bagcheck closed: Lua errors off")
    sc.eq(n.get(10), "button update now: no bag window open", "bagcheck closed: update now")
    sc.eq(body[-1], "Open your bags, then type /gl bagcheck again and take a screenshot.", "bagcheck closed: what to do")
    M.cvars.scriptErrors = "1"
    n, _ = bagcheck(sc, w, "bags check alias", "/gl bags check")
    sc.eq(n.get(9), "Lua errors on screen: on", "bags check alias: Lua errors on")

    # The game's backpack open.
    M.OpenBags(1)
    w.settle(0.2)
    st = BP.stats
    sc.check(st.asked >= 1 and st.ran >= 1, "bagcheck: updates asked %d, run %d" % (st.asked, st.ran))
    sc.eq(st.result, "button shown", "bagcheck: the last update showed the button")
    tab = BP.tab
    closed_bg = list(tab.bg._color.values())
    n, body = bagcheck(sc, w, "bagcheck open")
    sc.eq(n.get(2), "game bag windows open: ContainerFrame1 85% from left, 13% up, 180x170 px", "bagcheck open: the backpack")
    sc.eq(n.get(3), "windows the button uses: ContainerFrame1", "bagcheck open: hangs on the backpack")
    sc.check((n.get(5) or "").startswith("button updates asked ") and (n.get(5) or "").endswith("last: button shown"),
             "bagcheck open: counters (%s)" % n.get(5))
    sc.check(re.match(r"^button: shown yes, on screen yes, \d+% from left, 13% up, 170x20 px, MEDIUM level \d+, "
                      r"hangs on ContainerFrame1$", n.get(6) or "") is not None, "bagcheck open: the button (%s)" % n.get(6))
    sc.eq(n.get(10), "button update now: button shown", "bagcheck open: update now")
    sc.eq(body[-1], "The Bag cleanup button flashes gold for 10 seconds. Take a screenshot of your whole screen.",
          "bagcheck open: flash line")
    sc.eq(tab.GetFrameStrata(tab), "TOOLTIP", "flash: the button comes to the front")
    c = list(tab.bg._color.values())
    sc.check(c != closed_bg and c[3] == 1, "flash: the button turns gold (%s)" % c)
    w.settle(10.5)
    sc.eq(tab.GetFrameStrata(tab), "MEDIUM", "flash: its layer comes back after 10 seconds")
    sc.eq(list(tab.bg._color.values()), closed_bg, "flash: its colour comes back after 10 seconds")
    # With the panel open (the tab hides beside the game's bags).
    M.click(tab)
    w.settle(0.1)
    n, body = bagcheck(sc, w, "bagcheck panel")
    sc.eq(n.get(10), "button update now: panel shown", "bagcheck panel: update now")
    sc.eq(body[-1], "The cleanup panel is open beside your bags. Take a screenshot of your whole screen.", "bagcheck panel: last line")
    M.click(BP.closeButton)
    w.settle(0.1)
    # No place to hang the button: hidden, and the counters say why.
    w.lua.execute("ContainerFrame1.GetBottom = function() return nil end")
    BP.UpdateSoon(BP)
    w.settle(0.1)
    sc.eq(st.result, "button made, no place for it", "no place: the update says why")
    sc.check(not tab.IsShown(tab), "no place: the button is hidden, not left where it was")
    w.lua.execute("ContainerFrame1.GetBottom = nil")
    BP.UpdateSoon(BP)
    w.settle(0.1)
    sc.check(tab.IsVisible(tab) and st.result == "button shown", "no place: back once the bag has a position")
    M.CloseBags()
    w.settle(0.2)
    sc.clean(w, "bagcheck", ok_chat=BAGCHECK_LINES)

    # ElvUI as it runs in the game, open and closed.
    we = elvui_world(ref, items)
    we.lua.execute("ToggleAllBags()")
    we.settle(0.2)
    n, body = bagcheck(sc, we, "bagcheck elvui")
    sc.check(re.match(r"^ElvUI bags: open yes, \d+% from left, \d+% up, \d+x\d+ px, HIGH, hooked yes$", n.get(1) or "") is not None,
             "bagcheck elvui: ElvUI's window (%s)" % n.get(1))
    sc.eq(n.get(2), "game bag windows open: ContainerFrame1 no position, ContainerFrame2 no position, "
          "ContainerFrame3 no position, ContainerFrame4 no position and 1 more", "bagcheck elvui: the game's bags, parked")
    sc.eq(n.get(3), "windows the button uses: ElvUI_ContainerFrame", "bagcheck elvui: hangs on ElvUI's window")
    sc.check((n.get(4) or "").endswith("OpenBackpack yes, CloseBackpack yes, ElvUI yes"), "bagcheck elvui: hooks (%s)" % n.get(4))
    sc.check((n.get(6) or "").endswith("hangs on ElvUI_ContainerFrame (in its header)"), "bagcheck elvui: button (%s)" % n.get(6))
    we.settle(10.5)
    we.lua.execute("ToggleAllBags()")
    we.settle(0.2)
    n, _ = bagcheck(sc, we, "bagcheck elvui closed")
    sc.check((n.get(1) or "").startswith("ElvUI bags: open no, "), "bagcheck elvui closed: says closed (%s)" % n.get(1))
    sc.clean(we, "bagcheck elvui", ok_chat=BAGCHECK_LINES)

    # The tab cannot be made the first time: the next update makes it, and the
    # problem is said once in chat and kept for /gl bagcheck.
    wf = World(ref, items)
    wf.lua.execute("""
        local orig = CreateFrame
        local failed = false
        CreateFrame = function(t, name, ...)
            if name == "ForeverAHBagPanel" and not failed then failed = true; error("no frames today") end
            return orig(t, name, ...)
        end
    """)
    BPf = wf.ns.BagPanel
    mark = wf.chat_mark()
    wf.M.OpenBags(1)
    wf.settle(0.2)
    sc.check(not BPf.built and BPf.stats.result == "could not make the button", "build fails: not made, and it says so")
    sc.check("no frames today" in str(BPf.lastError), "build fails: the error is kept (%s)" % BPf.lastError)
    wf.M.CloseBags()
    wf.settle(0.2)
    wf.M.OpenBags(1)
    wf.settle(0.2)
    sc.check(BPf.built and BPf.tab.IsVisible(BPf.tab), "build fails once: the next update makes the button")
    said = [plain(x) for x in wf.chat(mark)]
    sc.eq(len([t for t in said if "/gl bagcheck" in t]), 1, "build fails: said once in chat (%s)" % said)
    n, _ = bagcheck(sc, wf, "bagcheck after a failure")
    sc.check("no frames today" in (n.get(7) or ""), "bagcheck after a failure: line 7 has the error (%s)" % n.get(7))

    # /gl bags when the scan breaks: one line saying so, not silence.
    ws = World(ref, items)
    ws.ns.BagScan = ws.lua.eval("function() error('scan broke') end")
    out = [plain(x) for x in ws.slash("/gl bags")]
    sc.check(len(out) == 1 and "bag scan error: " in out[0] and "scan broke" in out[0], "bags scan error: said in chat (%s)" % out)
    n, _ = bagcheck(sc, ws, "bagcheck scan error")
    sc.check((n.get(8) or "").startswith("scan error: ") and "scan broke" in n.get(8), "bagcheck: line 8 has the scan error (%s)" % n.get(8))

    # Without Bags.lua, the command is unknown (so that line alone says Bags.lua did not load).
    saved = TOC_FILES[:]
    try:
        TOC_FILES.remove("Bags.lua")
        wn = World(ref, items)
    finally:
        TOC_FILES[:] = saved
    out = [plain(x) for x in wn.slash("/gl bagcheck")]
    sc.check(any(t.endswith("unknown command. Type /gl help for the list.") for t in out), "no Bags.lua: unknown command (%s)" % out)


# ===========================================================================
# Quest items and QuestieDB: whether a quest item can go
# ===========================================================================
QDB_ALLY = 77 + 2 ** 32        # Human, Dwarf, Night Elf, Gnome, and Forever's Skyborne Alliance at bit 32
QDB_HORDE = 178 + 2 ** 33      # Orc, Undead, Tauren, Troll, and Skyborne Horde at bit 33
QDB_RACES = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16, 6: 32, 7: 64, 8: 128, 95: 2 ** 32, 96: 2 ** 33}
QDB_COMMIT = "e1652ce62113c152da682baf2d6fb85f1396bcfb"
QDB_STAMP = "1.1.2|%s|4|1" % QDB_COMMIT


def qobj(*ids):
    """A quest's objectives as QuestieDB stores them: the item list is group 3, {itemId, text, 0} each."""
    return {3: [[i, None, 0] for i in ids]}


# Quests and items as this player's QuestieDB 1.1.2 has them (decoded from its
# QuestieDB_Forever.toc): the Uldaman necklace chain (the necklace starts
# 2198 and is its source item too; the phial is given by 2200 and used in
# 2201, which is handed in at Talvash's scrying bowl, object 112877, where
# 2204 starts; the phial is also the reward of 3375), both factions'
# Badlands Reagent Run, and the Undead paladin lessons (the Symbol of Life is
# the reward of 94435 and needed for 94438, which comes after 94436). Made
# up: a letter that starts a quest, a repeatable turn-in, two quests that
# exclude each other, a Warrior quest, and a lamp for a quest that comes
# after another (a negative id in preQuestGroup is that quest too).
QDB_UNDEAD, QDB_PALADIN = 16, 2
QDB_QUESTS = {
    2198: {"name": "The Shattered Necklace", "requiredRaces": QDB_ALLY, "sourceItemId": 7666, "nextQuestInChain": 2199,
           "startedBy": {3: [7666]}, "finishedBy": {1: [6826]}},
    2199: {"name": "Lore for a Price", "requiredRaces": QDB_ALLY, "nextQuestInChain": 2200, "objectives": qobj(2842),
           "preQuestSingle": [2198], "startedBy": {1: [6826]}, "finishedBy": {1: [6826]}},
    2200: {"name": "Back to Uldaman", "requiredRaces": QDB_ALLY, "sourceItemId": 7667, "nextQuestInChain": 2201,
           "preQuestSingle": [2199], "startedBy": {1: [6826]}, "finishedBy": {1: [6912]}},
    2201: {"name": "Find the Gems", "requiredRaces": QDB_ALLY, "sourceItemId": 7668, "nextQuestInChain": 2204,
           "objectives": qobj(7669, 7670, 7671), "preQuestSingle": [2200], "startedBy": {1: [6912]},
           "finishedBy": {2: [112877]}},
    2204: {"name": "Restoring the Necklace", "requiredRaces": QDB_ALLY, "nextQuestInChain": 2361, "objectives": qobj(7672),
           "preQuestSingle": [2201], "startedBy": {2: [112877]}, "finishedBy": {1: [6826]}},
    2361: {"name": "Restoring the Necklace", "requiredRaces": QDB_ALLY, "preQuestSingle": [2204],
           "startedBy": {1: [6826]}, "finishedBy": {1: [6826]}},
    3375: {"name": "Replacement Phial", "requiredRaces": QDB_ALLY, "specialFlags": 1, "availableUntilCompleted": 2361,
           "objectives": qobj(3827, 3857, 1708), "preQuestSingle": [2201]},
    2500: {"name": "Badlands Reagent Run", "requiredRaces": QDB_ALLY, "nextQuestInChain": 17, "objectives": qobj(7847, 7846, 7848)},
    2258: {"name": "Badlands Reagent Run", "requiredRaces": QDB_HORDE, "nextQuestInChain": 2202,
           "objectives": qobj(7847, 7846, 7848)},
    94435: {"name": "A Lesson in Divinity", "requiredRaces": QDB_UNDEAD, "requiredClasses": QDB_PALADIN,
            "nextQuestInChain": 94436, "preQuestSingle": [94434]},
    94436: {"name": "A Lesson in Divinity", "requiredRaces": QDB_UNDEAD, "requiredClasses": QDB_PALADIN,
            "nextQuestInChain": 94438, "preQuestSingle": [94435]},
    94438: {"name": "A Lesson in Divinity", "requiredRaces": QDB_UNDEAD, "requiredClasses": QDB_PALADIN,
            "nextQuestInChain": 94440, "preQuestSingle": [94436], "requiredSourceItems": [6866]},
    950001: {"name": "A Sealed Letter"},
    950002: {"name": "Minion's Scourgestones", "specialFlags": 1, "objectives": qobj(12840)},
    950010: {"name": "Ogre Toll", "exclusiveTo": [950011], "objectives": qobj(900503)},
    950011: {"name": "Teeth for Thelsamar", "exclusiveTo": [950010], "objectives": qobj(900503)},
    950020: {"name": "The Brutal Way", "requiredClasses": 1, "requiredSourceItems": [900504]},
    950030: {"name": "Old Debts", "availableUntilCompleted": 950031, "objectives": qobj(900505)},
    950031: {"name": "New Debts"},
    950040: {"name": "Into the Mine"},
    950041: {"name": "Deeper Down", "preQuestGroup": [950039, -950040], "requiredSourceItems": [900506]},
}
QDB_ITEMS = {
    6866: {"name": "Symbol of Life", "class": 12, "flags": 64, "questRewards": [1789, 1790, 94435]},
    7666: {"name": "Shattered Necklace", "class": 12, "flags": 2048, "startQuest": 2198},
    7667: {"name": "Talvash's Phial of Scrying", "class": 12, "flags": 64, "questRewards": [3375]},
    7668: {"name": "Bloodstained Journal", "class": 15, "flags": 2048},
    7846: {"name": "Crag Coyote Fang", "class": 12},
    12840: {"name": "Minion's Scourgestone", "class": 12},
    900501: {"name": "Sealed Letter", "class": 12, "startQuest": 950001},
    900503: {"name": "Ogre Tooth", "class": 12},
    900504: {"name": "Brutal Token", "class": 12},
    900506: {"name": "Miner's Lamp", "class": 12},
}
# The bags: (bag, slot, item, count, name, fields for the mock game).
QUEST_BAG = [
    (0, 1, 7667, 1, "Talvash's Phial of Scrying", {"classID": 12, "bind": 4, "maxStack": 1, "useSpell": "Create Scrying Bowl"}),
    (0, 2, 7846, 4, "Crag Coyote Fang", {"classID": 12, "bind": 4, "maxStack": 20}),
    (0, 3, 900501, 1, "Sealed Letter", {"classID": 12, "bind": 4, "maxStack": 1, "startsQuest": 950001}),
    (0, 4, 12840, 3, "Minion's Scourgestone", {"classID": 12, "maxStack": 20}),
    (0, 5, 900502, 2, "Gnarled Root", {"classID": 12, "maxStack": 20}),
    (0, 6, 900503, 1, "Ogre Tooth", {"classID": 12, "maxStack": 20}),
    (0, 7, 900504, 1, "Brutal Token", {"classID": 12, "bind": 4, "maxStack": 1}),
    (0, 8, 900505, 1, "Faded Ledger", {"classID": 12, "bind": 4, "maxStack": 1}),
]
LOG_FIND_GEMS = [
    {"title": "Uldaman", "isHeader": True},
    {"title": "Find the Gems", "questID": 2201,
     "objectives": ["Shattered Necklace Ruby: 0/1", "Shattered Necklace Sapphire: 0/1", "Shattered Necklace Topaz: 0/1"]},
]
# The player's log after Find the Gems: Restoring the Necklace mentions
# Talvash, so the quest log's words alone keep the phial.
LOG_NECKLACE = [
    {"title": "Uldaman", "isHeader": True},
    {"title": "Restoring the Necklace", "questID": 2204,
     "objectives": ["Shattered Necklace Power Source: 0/1", "Bring it to Talvash del Kissel"]},
]
QDB_COLORS = {"pos": (0.25, 0.8, 0.45), "buy": (0.898, 0.722, 0.259), "muted": (0.55, 0.51, 0.45)}

QDB_MOCK_LUA = r'''
-- LibQuestieDB as QuestieDB 1.1.2 shows it to other addons (src/api.lua,
-- contract 4): Quest and Item with Get, GetAll, GetAllIds and Exists over the
-- rows a scenario gives; number fields read 0 and objectives {} for a known
-- id, table fields come back as fresh copies, an unknown id reads nil.
local M = MOCK
local data = ...
M.profileMs = M.profileMs or 0
-- reads: every Get; objectives: how many quests' objectives were read (the
-- index reads each quest's once); fail: every read errors; cost: fake
-- milliseconds a read takes (for debugprofilestop).
M.qdb = { reads = 0, objectives = 0, fail = false, cost = nil, contractFails = data.contractFails == true }
local function copy(v)
  if type(v) ~= "table" then return v end
  local o = {}
  for k, x in pairs(v) do o[k] = copy(x) end
  return o
end
local QUEST_NUM = { requiredLevel = 1, questLevel = 1, requiredRaces = 1, requiredClasses = 1, sourceItemId = 1,
  zoneOrSort = 1, nextQuestInChain = 1, questFlags = 1, specialFlags = 1, parentQuest = 1, breadcrumbForQuestId = 1,
  requiredSpell = 1, requiredSpecialization = 1, requiredMaxLevel = 1, availableUntilCompleted = 1,
  availableStartingWith = 1, disabledByQuest = 1 }
local QUEST_TABLES = { startedBy = 1, finishedBy = 1, objectivesText = 1, triggerEnd = 1, objectives = 1,
  preQuestGroup = 1, preQuestSingle = 1, childQuests = 1, inGroupWith = 1, exclusiveTo = 1, requiredSkill = 1,
  requiredMinRep = 1, requiredMaxRep = 1, requiredSourceItems = 1, reputationReward = 1, breadcrumbs = 1,
  extraObjectives = 1, requiredRanks = 1 }
local NEVER_NIL = { startedBy = 1, finishedBy = 1, objectives = 1 }
local ITEM_NUM = { startQuest = 1, flags = 1, foodType = 1, itemLevel = 1, requiredLevel = 1, ammoType = 1,
  class = 1, subClass = 1, teachesSpell = 1 }
local ITEM_TABLES = { npcDrops = 1, objectDrops = 1, itemDrops = 1, questRewards = 1, vendors = 1, relatedQuests = 1 }
local function entity(rows, nums, tables, neverNil, kind)
  local e, ids, map = {}, {}, {}
  for id in pairs(rows) do ids[#ids + 1] = id end
  table.sort(ids)
  for _, id in ipairs(ids) do map[id] = true end
  local function get(id, key)
    local st = M.qdb
    if st.fail then error("QuestieDB read failed") end
    st.reads = st.reads + 1
    if st.cost then M.profileMs = M.profileMs + st.cost end
    if type(key) ~= "string" or not (key == "name" or nums[key] or tables[key]) then return nil end
    local r = rows[id]
    if not r then return nil end
    if kind == "Quest" and key == "objectives" then st.objectives = st.objectives + 1 end
    local v = r[key]
    if v == nil then
      if nums[key] then return 0 end
      if neverNil[key] then return {} end
      return nil
    end
    return copy(v)
  end
  e.Get = get
  function e.GetAll(id, keys)
    if not map[id] then return nil end
    local out = { n = #keys }
    for i, k in ipairs(keys) do out[i] = get(id, k) end
    return out
  end
  function e.GetAllIds(hashmap) if hashmap == true then return map end return ids end
  function e.Exists(id) return map[id] == true end
  return e
end
local L = {
  Quest = entity(data.quests, QUEST_NUM, QUEST_TABLES, NEVER_NIL, "Quest"),
  Item = entity(data.items, ITEM_NUM, ITEM_TABLES, {}, "Item"),
  contractVersion = 4, minSupportedContract = 1, addonName = "QuestieDB", readMode = "baked",
  Enum = { raceMaskById = data.races, factionRaceMasks = { Alliance = data.ally, Horde = data.horde } },
}
function L.RequireContract(required)
  if M.qdb.contractFails then return false, "QuestieDB contract mismatch: this consumer needs version 4" end
  if type(required) == "number" and required >= L.minSupportedContract and required <= L.contractVersion then return true end
  return false, "QuestieDB contract mismatch"
end
if data.noQuestList then L.Quest.GetAllIds = nil end
LibQuestieDB, QuestDB, ItemDB = L, L.Quest, L.Item
M.loaded.QuestieDB = true
M.addonMeta.QuestieDB = { Version = data.version, ["X-BUILD-COMMIT"] = data.commit, ["X-Contract-Version"] = "4" }
'''


def install_questie(w, version="1.1.2", commit=QDB_COMMIT, filler=0, contract_fails=False, no_quest_list=False):
    """QuestieDB loaded (as LibQuestieDB), with QDB_QUESTS and QDB_ITEMS plus
    `filler` more quests (every 7th asks for one of 50 made-up items)."""
    quests = dict(QDB_QUESTS)
    for k in range(filler):
        row = {"name": "Filler Quest %d" % (k + 1), "requiredRaces": QDB_ALLY if k % 2 else 0}
        if k % 7 == 0:
            row["objectives"] = qobj(880000 + k % 50)
        quests[100001 + k] = row
    data = {"quests": quests, "items": QDB_ITEMS, "version": version, "commit": commit, "races": QDB_RACES,
            "ally": QDB_ALLY, "horde": QDB_HORDE, "contractFails": contract_fails, "noQuestList": no_quest_list}
    w.loader(QDB_MOCK_LUA, "@MOCK/QuestieDB.lua", w.to_lua(data))
    return quests


def quest_bags(w, log=None, done=()):
    for bag, slot, iid, count, name, fields in QUEST_BAG:
        bag_add_item(w, iid, name, 1, 0, **fields)
        bag_put(w, bag, slot, iid, count)
    bag_quests(w, LOG_FIND_GEMS if log is None else log)
    for q in done:
        w.M.questDone[q] = True


def lua_to_py(v):
    """A Lua table as Python dicts (whole-number keys as int)."""
    if lua51.lua_type(v) != "table":
        return v
    out = {}
    for k, x in v.items():
        if isinstance(k, float) and k == int(k):
            k = int(k)
        out[k] = lua_to_py(x)
    return out


# --- an independent model of Bags.lua's QuestieDB verdicts ---------------------------
QDB_RANK = {"use": 1, "nextq": 2, "start": 3, "give": 4}
QDB_REPEATABLE = {12840, 12841, 12843, 12844, 22523, 22524, 22525, 22526, 22527, 22528, 22529, 21377, 21383, 20404,
                  20513, 20514, 20515} | set(range(19698, 19716))


def ref_quest_index(quests):
    """item -> quest * 4 + role (0 asks for it, 1 gives it): one code, or a list in quest order."""
    idx = {}

    def add(it, code):
        lst = idx.setdefault(it, [])
        if code not in lst:
            lst.append(code)
    for q in sorted(quests):
        row = quests[q]
        for o in (row.get("objectives") or {}).get(3, []):
            add(o[0], q * 4)
        for it in row.get("requiredSourceItems", []):
            add(it, q * 4)
        if row.get("sourceItemId"):
            add(row["sourceItemId"], q * 4 + 1)
    return {k: (v[0] if len(v) == 1 else v) for k, v in idx.items()}


def ref_quest_verdict(quests, iid, in_log, done, race=1, cls=1, log_known=True, start_q=None, usable=True):
    """(kind, quest name, repeatable) or None, as Bags.lua QuestieVerdict decides it.
    in_log: the quests known to be in the log; log_known: whether every other
    quest is known not to be (False: closed headers and no IsOnQuest).
    usable: the item has a Use effect (a tool); only then does the quest after
    the one that gave it count (a note you only read is done with its quest)."""
    idx = ref_quest_index(quests)
    item = QDB_ITEMS.get(iid, {})
    starts = [q for q in (item.get("startQuest", 0), start_q) if q]
    R, order, given_for, givers = {}, [], {}, []

    def mine(row):
        races, classes = row.get("requiredRaces", 0), row.get("requiredClasses", 0)
        rb, cb = QDB_RACES[race], 2 ** (cls - 1)
        return (not races or (races // rb) % 2 == 1) and (not classes or (classes // cb) % 2 == 1)

    def add(q, role):
        if not q or q <= 0:
            return False
        if q in R:
            return True
        row = quests.get(q)
        if not row or not mine(row):
            return False
        R[q] = role
        order.append(q)
        return True
    # The quest it starts first: QuestieDB also lists a starter as the item its
    # quest gives, and it stays the quest it starts (no quest after it counts).
    for s in starts:
        add(s, "start")
    e = idx.get(iid)
    for c in (e if isinstance(e, list) else ([] if e is None else [e])):
        q, role = c // 4, c % 4
        if role == 0:
            add(q, "use")
        elif role == 1 and q not in starts:
            givers.append(q)
            if add(q, "give") and usable:
                n = quests[q].get("nextQuestInChain", 0)
                if add(n, "nextq"):
                    given_for.setdefault(n, q)
    if not order:
        return None

    def pick(pred):
        best = None
        for q in order:
            if pred(q) and (best is None or QDB_RANK[R[q]] < QDB_RANK[R[best]]):
                best = q
        return best

    def closed(q):
        return (q in done or quests[q].get("availableUntilCompleted") in done
                or any(x in done for x in quests[q].get("exclusiveTo", [])))

    def logged(q):
        return True if q in in_log else (False if log_known else None)
    name = lambda q: quests[q]["name"]  # noqa: E731
    q = pick(lambda x: x in in_log)
    if q:
        return ("keep", name(q), False)
    q = pick(lambda x: R[x] == "start" and not closed(x))
    if q:
        return ("starts", name(q), False)
    q = pick(lambda x: quests[x].get("specialFlags", 0) % 2 == 1)
    if q or iid in QDB_REPEATABLE:
        return ("keep", name(q or pick(lambda x: True)), True)
    q = pick(lambda x: not closed(x))
    if not q:
        # The quest it was given for is handed in at an object where the next
        # quest starts: kept until that one is taken.
        unsure = False
        for x in order:
            n = quests[x].get("nextQuestInChain", 0)
            row = quests.get(n)
            if x not in given_for or x not in done or not row or not mine(row) or closed(n):
                continue
            if set(quests[x].get("finishedBy", {}).get(2, [])) & set(row.get("startedBy", {}).get(2, [])):
                s = logged(n)
                if s is False:
                    return ("keep", name(n), False)
                unsure = unsure or s is None
        if unsure:
            return None
        return ("done", name(pick(lambda x: True)), False)
    # Halfway along a chain: a quest done gave it or rewarded it, or one that
    # needs it comes after a quest in the log.
    got = any(g in done for g in givers) or any(r in done for r in item.get("questRewards", []))

    def after_log(x):
        pre = list(quests[x].get("preQuestSingle", [])) + [abs(p) for p in quests[x].get("preQuestGroup", [])]
        return any(p in in_log for p in pre)
    q2 = pick(lambda x: not closed(x) and (got or after_log(x)))
    if q2:
        return ("keep", name(q2), False)
    if not log_known:
        return None
    return ("open", name(q), False)


def quest_line_text(v):
    """The tooltip line and its colour key for a model verdict."""
    kind, n, rep = v
    if kind == "done":
        return "Can drop: done with %s" % n, "pos"
    if kind == "open":
        return "Only for %s, not in your quest log" % n, "muted"
    if kind == "starts":
        return "Starts %s" % n, "buy"
    if rep:
        return "Keep: for %s (repeatable)" % n, "buy"
    return "Keep: needed for %s" % n, "buy"


def lua_verdict(w, iid):
    v = w.ns.QuestItemVerdict(iid)
    return None if v is None else (v.kind, v.name, bool(v.rep))


def quest_tip(w, bag, slot):
    """Our lines on the bag slot's item tooltip: (text, colour) each."""
    tt = w.g.GameTooltip
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetBagItem(tt, bag, slot)
    al = w.M.addonLines(tt)
    out = []
    for i in range(1, len(al) + 1):
        ln = al[i]
        c = None if ln.lr is None else (round(ln.lr, 3), round(ln.lg, 3), round(ln.lb, 3))
        out.append((ln.left, c))
    tt.Hide(tt)
    return out


def quest_rows(BP):
    """The cleanup panel's shown OLD QUEST ITEMS rows: (item, right text, right colour)."""
    out = []
    for i in range(1, len(BP.rows) + 1):
        r = BP.rows[i]
        if r.IsShown(r) and r.kind == "quest":
            c = r.right._textColor
            out.append((r.entry.id, r.right._text, (round(c[1], 3), round(c[2], 3), round(c[3], 3))))
    return out


NO_LISTINGS = ("Goblin Ledger: no recent listings", QDB_COLORS["muted"])


def plain_tip(fields):
    """Our lines on a quest item's tooltip without QuestieDB: none, or for one
    that is not bound (it might go on the auction house) the no-listings line."""
    return [] if fields.get("bind") in (1, 4) else [NO_LISTINGS]


def quest_checks(sc, w, quests, in_log, done, label, race=1, cls=1, log_known=True, ids=None, bag_list=None):
    """Every bag item's verdict, tooltip line and colour against the model."""
    for bag, slot, iid, count, name, fields in (bag_list or QUEST_BAG):
        if ids and iid not in ids:
            continue
        want = ref_quest_verdict(quests, iid, in_log, done, race, cls, log_known, fields.get("startsQuest"),
                                 usable="useSpell" in fields)
        sc.eq(lua_verdict(w, iid), want, "%s: %s verdict" % (label, name))
        tip = quest_tip(w, bag, slot)
        if want is None:
            sc.eq(tip, plain_tip(fields), "%s: %s tooltip has no quest line" % (label, name))
        else:
            text, key = quest_line_text(want)
            sc.eq(tip, plain_tip(fields) + [(text, QDB_COLORS[key])], "%s: %s tooltip, one quest line last" % (label, name))


def sc_bag_questie(sc, ref, items):
    """With QuestieDB: a quest item's tooltip says whether it can go (keep,
    starts, done, not started), the cleanup panel lists the done and not
    started ones with the quest in the row tooltip, /gl bags says the same,
    and the phial the player asked about says done once its quests are."""
    w = World(ref, items)
    M, ns, g = w.M, w.ns, w.g
    quests = install_questie(w)
    quest_bags(w, LOG_FIND_GEMS, done=(2198, 2199, 2200))
    QI = ns.QuestIndex
    sc.eq(QI.state, "idle", "questie: the index waits a few seconds after login")
    # First need: the phial's tooltip. Nothing yet, the index starts.
    sc.eq(quest_tip(w, 0, 1), [], "questie: no quest line while the index is being made")
    sc.eq(QI.state, "building", "questie: a quest item's tooltip starts the index")
    w.settle(0.5)
    sc.eq(QI.state, "ready", "questie: the index is ready a few frames later")
    sc.eq(QI.size, len(ref_quest_index(quests)), "questie: every item a quest asks for or gives is indexed")
    # On Find the Gems (2200 done): the phial is needed.
    in_log, done = {2201}, {2198, 2199, 2200}
    sc.eq(lua_verdict(w, 7667), ("keep", "Find the Gems", False), "questie: phial while on Find the Gems: keep")
    sc.eq(lua_verdict(w, 7846), ("open", "Badlands Reagent Run", False),
          "questie: Crag Coyote Fang, Badlands Reagent Run not in the log: not started")
    sc.eq(lua_verdict(w, 900501), ("starts", "A Sealed Letter", False), "questie: the letter starts a quest you have not done")
    sc.eq(lua_verdict(w, 12840), ("keep", "Minion's Scourgestones", True), "questie: a repeatable turn-in is kept")
    sc.eq(lua_verdict(w, 900502), None, "questie: an item QuestieDB does not know has no verdict")
    quest_checks(sc, w, quests, in_log, done, "on Find the Gems")
    tip = quest_tip(w, 0, 1)
    sc.eq(tip, [("Keep: needed for Find the Gems", QDB_COLORS["buy"])], "questie: phial tooltip, gold keep line")
    sc.eq(quest_tip(w, 0, 2), [("Only for Badlands Reagent Run, not in your quest log", QDB_COLORS["muted"])],
          "questie: fang tooltip, grey not started line")
    # The panel: not started ones listed (the quest in the row tooltip), kept ones not.
    M.OpenBags(1)
    w.settle(0.2)
    BP = ns.BagPanel
    M.click(BP.tab)
    w.settle(0.2)
    mu, pos = QDB_COLORS["muted"], QDB_COLORS["pos"]
    sc.eq(quest_rows(BP), [(900504, "not started", mu), (7846, "not started", mu), (900505, "not started", mu),
                           (900503, "not started", mu), (900502, "not needed", mu)],
          "questie panel: not started items, then the one QuestieDB does not know (its quest log verdict)")
    row = bag_row_for(BP, 7846)
    if sc.check(row is not None, "questie panel: the fang has a row"):
        M.enter(row)
        lines = [plain(l) for l, _ in w.all_lines(g.GameTooltip)]
        sc.check("Old quest item" in lines and "Only for Badlands Reagent Run, not in your quest log." in lines,
                 "questie panel: the row tooltip names the quest (%s)" % lines)
        sc.check("Only for Badlands Reagent Run, not in your quest log" not in lines,
                 "questie panel: the item tooltip's own quest line is left out of the row's (%s)" % lines)
        M.leave(row)
    out = [plain(x) for x in w.slash("/gl bags")]
    sc.check(any(t.endswith("old quest items: [Brutal Token] (not started: The Brutal Way), [Crag Coyote Fang] x4 "
                            "(not started: Badlands Reagent Run), [Faded Ledger] (not started: Old Debts), "
                            "[Ogre Tooth] (not started: Ogre Toll), [Gnarled Root] x2 (no quest needs it)") for t in out),
             "questie /gl bags: quest line (%s)" % out)
    sc.check(not any("still reading" in t for t in out), "questie /gl bags: nothing about reading once ready")

    # Find the Gems done, Restoring the Necklace (it mentions Talvash) in the log: the phial can go.
    bag_quests(w, LOG_NECKLACE)
    M.questDone[2201] = True
    M.fire("QUEST_LOG_UPDATE")
    w.settle(0.5)
    in_log, done = {2204}, {2198, 2199, 2200, 2201}
    sc.eq(lua_verdict(w, 7667), ("done", "Find the Gems", False), "questie: phial after Find the Gems, Restoring the Necklace in the log: done")
    sc.eq(quest_tip(w, 0, 1), [("Can drop: done with Find the Gems", QDB_COLORS["pos"])], "questie: phial tooltip, green done line")
    quest_checks(sc, w, quests, in_log, done, "after Find the Gems")
    rows = quest_rows(BP)
    sc.eq(rows[:1], [(7667, "done", pos)], "questie panel: the phial is listed first, done in green")
    row = bag_row_for(BP, 7667)
    if sc.check(row is not None, "questie panel: the phial has a row"):
        M.enter(row)
        lines = [plain(l) for l, _ in w.all_lines(g.GameTooltip)]
        sc.check("Can drop: done with Find the Gems." in lines, "questie panel: the phial's row tooltip (%s)" % lines)
        sc.check(not any("check your quest log" in l for l in lines), "questie panel: no unsure note for a QuestieDB verdict")
        M.leave(row)
        # The drop flow is as before for quest items: picked up, the game asks.
        n_pick = len(M.pickups)
        M.click(row, "LeftButton")
        C = g.ForeverAHBagConfirm
        sc.check(C is None or not C.IsShown(C), "questie panel: a quest item gets no drop box")
        sc.eq(len(M.pickups), n_pick + 1, "questie panel: a click picks the phial up")
        if len(M.pickups) > n_pick:
            p = M.pickups[len(M.pickups)]
            sc.eq((p[1], p[2]), (0, 1), "questie panel: the phial's slot")
        w.lua.execute("ClearCursor()")
        sc.eq(len(M.destroyed), 0, "questie panel: nothing destroyed")
    out = [plain(x) for x in w.slash("/gl bags")]
    sc.check(any("old quest items: [Talvash's Phial of Scrying] (done: Find the Gems), " in t for t in out),
             "questie /gl bags: the phial is done (%s)" % out)

    # The letter's quest taken, then done; the scourgestone's quest done; the
    # Alliance reagent run done (the Horde one does not count); one of the two
    # ogre quests done (the other is shut).
    M.quests[len(M.quests) + 1] = w.to_lua({"title": "A Sealed Letter", "questID": 950001, "objectives": []})
    M.questActive[950001] = True
    sc.eq(lua_verdict(w, 900501), ("keep", "A Sealed Letter", False), "questie: the letter while its quest is in the log: keep")
    M.quests[len(M.quests)] = None
    M.questActive[950001] = None
    for q in (950001, 950002, 2500, 950010, 950031):
        M.questDone[q] = True
    done = done | {950001, 950002, 2500, 950010, 950031}
    sc.eq(lua_verdict(w, 900501), ("done", "A Sealed Letter", False), "questie: the letter once its quest is done: done")
    sc.eq(lua_verdict(w, 12840), ("keep", "Minion's Scourgestones", True), "questie: a repeatable turn-in is never done")
    sc.eq(lua_verdict(w, 7846), ("done", "Badlands Reagent Run", False),
          "questie: fang with the Alliance reagent run done: done (the Horde one is not yours)")
    sc.eq(lua_verdict(w, 900503), ("done", "Ogre Toll", False), "questie: a quest shut by the one you did counts as done")
    sc.eq(lua_verdict(w, 900505), ("done", "Old Debts", False),
          "questie: a quest only available until one you did counts as done")
    quest_checks(sc, w, quests, in_log, done, "later")

    # Closed quest log headers and no IsOnQuest: "not started" cannot be told,
    # done still can; not for the phial, though: whether Restoring the
    # Necklace was taken at the scrying bowl cannot be told either.
    M.questDone[2500] = None
    M.questDone[950010] = None
    done = done - {2500, 950010}
    bag_quests(w, [{"title": "Uldaman", "isHeader": True, "isCollapsed": True}])
    quest_checks(sc, w, quests, set(), done, "log headers closed", log_known=False)
    sc.eq(lua_verdict(w, 7667), None, "questie: with a header closed the phial has no verdict (Restoring the Necklace may be under it)")
    s = ns.BagScan()
    sc.eq([s.quest[i].id for i in range(1, len(s.quest) + 1)], [900505, 900501],
          "questie: with a header closed only the done ones are listed (by name)")
    # C_QuestLog.IsOnQuest knows every quest, closed headers or not.
    w.lua.execute("MOCK.onQuest = {}; C_QuestLog.IsOnQuest = function(q) return MOCK.onQuest[q] == true end")
    quest_checks(sc, w, quests, set(), done, "IsOnQuest")
    sc.eq(lua_verdict(w, 7667), ("keep", "Restoring the Necklace", False),
          "questie: IsOnQuest says Restoring the Necklace is not taken: the phial still calls up the bowl it starts at")
    w.lua.execute("MOCK.onQuest[2500] = true; MOCK.onQuest[2204] = true")
    sc.eq(lua_verdict(w, 7846), ("keep", "Badlands Reagent Run", False), "questie: IsOnQuest finds a quest under a closed header")
    sc.eq(lua_verdict(w, 7667), ("done", "Find the Gems", False), "questie: IsOnQuest finds Restoring the Necklace: the phial is done")
    w.lua.execute("C_QuestLog.IsOnQuest = nil")
    n, _ = bagcheck(sc, w, "questie bagcheck")
    sc.check((n.get(8) or "").endswith(", quests from QuestieDB (%d items)" % len(ref_quest_index(quests))),
             "questie bagcheck: line 8 says the quest data is QuestieDB's (%s)" % n.get(8))
    w.settle(10.5)
    M.CloseBags()
    w.settle(0.2)
    sc.clean(w, "questie", ok_chat=BAGCHECK_LINES)

    # Horde (an Orc mage): the Alliance chain is not theirs, the Warrior quest neither.
    wh = World(ref, items, faction="Horde")
    wh.M.race = wh.to_lua(["Orc", "Orc", 2])
    wh.M["class"] = wh.to_lua(["Mage", "MAGE", 8])
    quests = install_questie(wh)
    quest_bags(wh, LOG_FIND_GEMS, done=(2198, 2199, 2200, 2500))
    wh.ns.BagScan()
    wh.settle(0.5)
    sc.eq(wh.ns.QuestIndex.state, "ready", "questie horde: the bag scan started the index")
    hdone = {2198, 2199, 2200, 2500}
    sc.eq(lua_verdict(wh, 7846), ("open", "Badlands Reagent Run", False),
          "questie horde: fang with only the Alliance reagent run done: not started")
    sc.eq(lua_verdict(wh, 7667), None, "questie horde: the Alliance phial chain is not theirs (quest log verdict)")
    sc.eq(lua_verdict(wh, 900504), None, "questie horde: a Warrior quest is not a mage's")
    quest_checks(sc, wh, quests, {2201}, hdone, "horde mage", race=2, cls=8)
    wh.M.questDone[2258] = True
    quest_checks(sc, wh, quests, {2201}, hdone | {2258}, "horde mage, their run done", race=2, cls=8)
    sc.eq(lua_verdict(wh, 7846), ("done", "Badlands Reagent Run", False), "questie horde: fang once their own run is done")
    sc.clean(wh, "questie horde")

    # The legacy client's tooltip script gets the line too.
    wl = World(ref, items, client="legacy")
    quests = install_questie(wl)
    quest_bags(wl, LOG_NECKLACE, done=(2198, 2199, 2200, 2201))
    wl.ns.BagScan()
    wl.settle(0.5)
    sc.eq(quest_tip(wl, 0, 1), [("Can drop: done with Find the Gems", QDB_COLORS["pos"])], "questie legacy: phial tooltip")
    sc.eq(quest_tip(wl, 0, 5), [NO_LISTINGS], "questie legacy: no quest line for an item QuestieDB does not know")
    # /gl tooltip off: no quest line either.
    wl.slash("/gl tooltip")
    sc.eq(quest_tip(wl, 0, 1), [], "questie: /gl tooltip off hides the quest line too")
    sc.clean(wl, "questie legacy")


# Along quest chains (bag-questie-chain): the necklace that starts its quest,
# the phial, and a lamp for a quest that comes after another; the Symbol of
# Life goes in an Undead paladin's bags.
CHAIN_BAG = [
    (0, 1, 7666, 1, "Shattered Necklace", {"classID": 12, "bind": 4, "maxStack": 1, "startsQuest": 2198}),
    (0, 2, 7667, 1, "Talvash's Phial of Scrying", {"classID": 12, "bind": 4, "maxStack": 1, "useSpell": "Create Scrying Bowl"}),
    (0, 3, 900506, 1, "Miner's Lamp", {"classID": 12, "bind": 4, "maxStack": 1}),
]
SYMBOL_BAG = [(0, 1, 6866, 1, "Symbol of Life", {"classID": 12, "bind": 4, "maxStack": 1})]
LOG_ELSEWHERE = [
    {"title": "Elwynn Forest", "isHeader": True},
    {"title": "A Threat Within", "questID": 783, "objectives": ["Speak with Marshal McBride: 0/1"]},
]


def qlog(quest_id, title):
    """A quest log with one quest (and one objective) under a header."""
    return [{"title": "Quests", "isHeader": True}, {"title": title, "questID": quest_id, "objectives": ["Task: 0/1"]}]


def chain_state(sc, w, quests, bag_list, log, done, label, race=1, cls=1):
    """The quest log and the quests done; every bag item against the model;
    returns the old quest items listed: (item, why, quest)."""
    bag_quests(w, log)
    w.M.questDone = w.to_lua({q: True for q in done})
    w.M.fire("QUEST_LOG_UPDATE")
    w.settle(0.5)
    in_log = {q["questID"] for q in log if q.get("questID")}
    quest_checks(sc, w, quests, in_log, set(done), label, race, cls, bag_list=bag_list)
    s = w.ns.BagScan()
    return [(s.quest[i].id, s.quest[i].why, s.quest[i].quest) for i in range(1, len(s.quest) + 1)]


def sc_bag_questie_chain(sc, ref, items):
    """Along quest chains (the player's real QuestieDB rows, and a made-up
    lamp): a quest starter says Starts until its quest is done (QuestieDB
    lists it as the item its quest gives too), then Done; an item stays Keep
    while the next step still needs it, where a dropped one is gone for good:
    the phial after Back to Uldaman, and after Find the Gems until Restoring
    the Necklace is taken at the scrying bowl; the lamp while the quest it
    comes after is in the log; the Symbol of Life between the paladin's
    lessons."""
    w = World(ref, items)
    M, ns = w.M, w.ns
    quests = install_questie(w)
    for bag, slot, iid, count, name, fields in CHAIN_BAG:
        bag_add_item(w, iid, name, 1, 0, **fields)
        bag_put(w, bag, slot, iid, count)
    bag_quests(w, LOG_ELSEWHERE)
    ns.BagScan()
    w.settle(0.5)
    sc.eq(ns.QuestIndex.state, "ready", "chain: the index is ready")
    buy, pos, mu = QDB_COLORS["buy"], QDB_COLORS["pos"], QDB_COLORS["muted"]

    def state(log, done, label):
        return chain_state(sc, w, quests, CHAIN_BAG, log, done, label)

    # Nothing of the chain taken: the necklace starts it.
    rows = state(LOG_ELSEWHERE, (), "chain, nothing taken")
    sc.eq(lua_verdict(w, 7666), ("starts", "The Shattered Necklace", False), "chain: the necklace starts its quest")
    sc.eq(quest_tip(w, 0, 1), [("Starts The Shattered Necklace", buy)], "chain: necklace tooltip, gold Starts line")
    sc.check(7666 not in [r[0] for r in rows], "chain: a quest starter is no old quest item (%s)" % rows)
    out = [plain(x) for x in w.slash("/gl bags")]
    sc.check(not any("Shattered Necklace" in t for t in out), "chain /gl bags: the necklace is not listed (%s)" % out)
    sc.eq(lua_verdict(w, 6866), None, "chain: the Undead paladin's lessons are not a human warrior's")
    # Carrying it to Talvash, then handed in (a second necklace is just an old one).
    state(qlog(2198, "The Shattered Necklace"), (), "chain, necklace taken")
    sc.eq(lua_verdict(w, 7666), ("keep", "The Shattered Necklace", False), "chain: the necklace while its quest is in the log")
    rows = state(qlog(2199, "Lore for a Price"), (2198,), "chain, necklace handed in")
    sc.eq(lua_verdict(w, 7666), ("done", "The Shattered Necklace", False),
          "chain: the necklace once its quest is done (not kept for Lore for a Price)")
    sc.eq(quest_tip(w, 0, 1), [("Can drop: done with The Shattered Necklace", pos)], "chain: necklace tooltip, green done line")
    sc.check((7666, "done", "The Shattered Necklace") in rows, "chain: the necklace is listed as done (%s)" % rows)

    # Back to Uldaman done, Find the Gems not taken (a full log): the phial is
    # still needed, and no new one is given before Find the Gems is done.
    rows = state(LOG_ELSEWHERE, (2198, 2199, 2200), "chain, Find the Gems not taken")
    sc.eq(lua_verdict(w, 7667), ("keep", "Find the Gems", False), "chain: the phial after Back to Uldaman, Find the Gems not taken: keep")
    sc.eq(quest_tip(w, 0, 2), [("Keep: needed for Find the Gems", buy)], "chain: phial tooltip, gold keep line")
    sc.check(7667 not in [r[0] for r in rows], "chain: the phial is not listed (%s)" % rows)
    state(LOG_FIND_GEMS, (2198, 2199, 2200), "chain, on Find the Gems")
    sc.eq(lua_verdict(w, 7667), ("keep", "Find the Gems", False), "chain: the phial on Find the Gems")

    # Find the Gems handed in at the scrying bowl, Restoring the Necklace (it
    # starts at the bowl) not taken there: the phial calls the bowl up again.
    M.OpenBags(1)
    w.settle(0.2)
    BP = ns.BagPanel
    M.click(BP.tab)
    w.settle(0.2)
    rows = state(LOG_ELSEWHERE, (2198, 2199, 2200, 2201), "chain, Restoring the Necklace not taken")
    sc.eq(lua_verdict(w, 7667), ("keep", "Restoring the Necklace", False),
          "chain: the phial until Restoring the Necklace is taken at the bowl: keep")
    sc.eq(quest_tip(w, 0, 2), [("Keep: needed for Restoring the Necklace", buy)], "chain: phial tooltip names Restoring the Necklace")
    sc.check(7667 not in [r[0] for r in rows], "chain: the phial is still not listed (%s)" % rows)
    sc.eq(quest_rows(BP), [(7666, "done", pos), (900506, "not started", mu)],
          "chain panel: the old necklace, the lamp not started; no phial")
    out = [plain(x) for x in w.slash("/gl bags")]
    sc.check(any(t.endswith("old quest items: [Shattered Necklace] (done: The Shattered Necklace), "
                            "[Miner's Lamp] (not started: Deeper Down)") for t in out),
             "chain /gl bags: the same, no phial (%s)" % out)
    # Taken (the player's case): the phial can go.
    rows = state(LOG_NECKLACE, (2198, 2199, 2200, 2201), "chain, Restoring the Necklace taken")
    sc.eq(lua_verdict(w, 7667), ("done", "Find the Gems", False), "chain: Restoring the Necklace in the log: the phial is done")
    sc.eq(quest_tip(w, 0, 2), [("Can drop: done with Find the Gems", pos)], "chain: phial tooltip, green done line")
    sc.eq(quest_rows(BP), [(7666, "done", pos), (7667, "done", pos), (900506, "not started", mu)],
          "chain panel: the phial is listed as done")
    out = [plain(x) for x in w.slash("/gl bags")]
    sc.check(any("[Talvash's Phial of Scrying] (done: Find the Gems)" in t for t in out),
             "chain /gl bags: the phial is done (%s)" % out)
    state(qlog(2361, "Restoring the Necklace"), (2198, 2199, 2200, 2201, 2204), "chain, Restoring the Necklace done")
    sc.eq(lua_verdict(w, 7667), ("done", "Find the Gems", False), "chain: Restoring the Necklace done: the phial is done")
    # Closed headers and no IsOnQuest: whether it was taken cannot be told, so no verdict.
    bag_quests(w, [{"title": "Uldaman", "isHeader": True, "isCollapsed": True}])
    M.questDone = w.to_lua({q: True for q in (2198, 2199, 2200, 2201)})
    quest_checks(sc, w, quests, set(), {2198, 2199, 2200, 2201}, "chain, headers closed", log_known=False, bag_list=CHAIN_BAG)
    sc.eq(lua_verdict(w, 7667), None, "chain: headers closed, Restoring the Necklace may be under one: no verdict")
    sc.eq(quest_tip(w, 0, 2), [], "chain: and no quest line")

    # The lamp: Deeper Down comes after Into the Mine (all of 950039 and 950040,
    # the second as a negative id). In the log: keep; done and Deeper Down not
    # taken: a lamp you can find again is not started, as the fang is.
    state(qlog(950040, "Into the Mine"), (), "chain, on Into the Mine")
    sc.eq(lua_verdict(w, 900506), ("keep", "Deeper Down", False), "chain: the lamp while Into the Mine is in the log: keep")
    sc.eq(quest_tip(w, 0, 3), [("Keep: needed for Deeper Down", buy)], "chain: lamp tooltip, gold keep line")
    rows = state(LOG_ELSEWHERE, (950040,), "chain, Into the Mine done")
    sc.eq(lua_verdict(w, 900506), ("open", "Deeper Down", False), "chain: Into the Mine done, Deeper Down not taken: not started")
    sc.check((900506, "open", "Deeper Down") in rows, "chain: the lamp is listed as not started (%s)" % rows)
    state(qlog(950041, "Deeper Down"), (950040,), "chain, on Deeper Down")
    sc.eq(lua_verdict(w, 900506), ("keep", "Deeper Down", False), "chain: the lamp on Deeper Down")
    M.CloseBags()
    w.settle(0.2)
    sc.clean(w, "chain")

    # An Undead paladin: the Symbol of Life is the reward of 94435 and needed for
    # 94438, which comes after 94436.
    wp = World(ref, items, faction="Horde")
    wp.M.race = wp.to_lua(["Undead", "Scourge", 5])
    wp.M["class"] = wp.to_lua(["Paladin", "PALADIN", 2])
    quests = install_questie(wp)
    for bag, slot, iid, count, name, fields in SYMBOL_BAG:
        bag_add_item(wp, iid, name, 1, 0, **fields)
        bag_put(wp, bag, slot, iid, count)
    bag_quests(wp, LOG_ELSEWHERE)
    wp.ns.BagScan()
    wp.settle(0.5)

    def pstate(log, done, label):
        return chain_state(sc, wp, quests, SYMBOL_BAG, log, done, label, race=5, cls=2)
    rows = pstate(qlog(94436, "A Lesson in Divinity"), (94434, 94435), "paladin, on 94436")
    sc.eq(lua_verdict(wp, 6866), ("keep", "A Lesson in Divinity", False),
          "paladin: the Symbol of Life on the lesson before the one that needs it: keep")
    sc.eq(quest_tip(wp, 0, 1), [("Keep: needed for A Lesson in Divinity", buy)], "paladin: symbol tooltip, gold keep line")
    sc.eq(rows, [], "paladin: nothing listed on 94436")
    rows = pstate(LOG_ELSEWHERE, (94434, 94435, 94436), "paladin, 94438 not taken")
    sc.eq(lua_verdict(wp, 6866), ("keep", "A Lesson in Divinity", False),
          "paladin: 94436 done, 94438 not taken: a reward you cannot get again is kept")
    sc.eq(rows, [], "paladin: nothing listed before 94438")
    pstate(qlog(94438, "A Lesson in Divinity"), (94434, 94435, 94436), "paladin, on 94438")
    sc.eq(lua_verdict(wp, 6866), ("keep", "A Lesson in Divinity", False), "paladin: the symbol on 94438")
    rows = pstate(LOG_ELSEWHERE, (94434, 94435, 94436, 94438), "paladin, 94438 done")
    sc.eq(lua_verdict(wp, 6866), ("done", "A Lesson in Divinity", False), "paladin: 94438 done: done")
    sc.eq(rows, [(6866, "done", "A Lesson in Divinity")], "paladin: then the symbol is listed as done")
    sc.clean(wp, "chain paladin")


def sc_bag_questie_slicing(sc, ref, items):
    """The index is made over many frames: at most 150 quests a frame, or about
    3 ms when debugprofilestop exists; a few seconds after login, or at once
    when the bag cleanup first needs it."""
    TICK_LOG = r'''
      local M = MOCK
      M.tickReads = {}
      local orig = M.tick
      M.tick = function(dt)
        local r0, ms0 = M.qdb.objectives, M.profileMs
        orig(dt)
        local n = M.qdb.objectives - r0
        if n > 0 then M.tickReads[#M.tickReads + 1] = { n = n, ms = M.profileMs - ms0 } end
      end
    '''
    filler = 2000
    w = World(ref, items)
    quests = install_questie(w, filler=filler)
    total = len(quests)
    w.lua.execute(TICK_LOG)
    QI = w.ns.QuestIndex
    w.settle(4.0)
    sc.eq(QI.state, "idle", "slicing: nothing read in the first seconds after login")
    sc.eq(w.M.qdb.objectives, 0, "slicing: no quest read yet")
    w.settle(0.9)
    sc.eq(QI.state, "building", "slicing: started 5 seconds after login")
    w.settle(5.0)
    sc.eq(QI.state, "ready", "slicing: ready")
    ticks = [(w.M.tickReads[i].n, w.M.tickReads[i].ms) for i in range(1, len(w.M.tickReads) + 1)]
    sc.eq(sum(n for n, _ in ticks), total, "slicing: each quest read once (%d)" % total)
    sc.check(max(n for n, _ in ticks) <= 150, "slicing: at most 150 quests a frame (%d)" % max(n for n, _ in ticks))
    sc.eq(len(ticks), -(-total // 150), "slicing: %d quests over %d frames" % (total, -(-total // 150)))
    sc.eq((QI.ticks, QI.most), (len(ticks), 150), "slicing: the index counts its frames and the most in one")
    sc.eq(QI.size, len(ref_quest_index(quests)), "slicing: every item indexed")
    sc.clean(w, "slicing")

    # debugprofilestop: each read takes 0.02 ms here, so a quest (3 reads)
    # 0.06 ms, and about 50 fit in the 3 ms a frame may take.
    w2 = World(ref, items)
    w2.lua.execute("debugprofilestop = function() return MOCK.profileMs end")
    quests = install_questie(w2, filler=filler)
    w2.lua.execute(TICK_LOG)
    w2.M.qdb.cost = 0.02
    quest_bags(w2, LOG_FIND_GEMS, done=(2198, 2199, 2200))
    # The bag cleanup's first need starts it at once, and /gl bags says it is still reading.
    out = [plain(x) for x in w2.slash("/gl bags")]
    sc.eq(w2.ns.QuestIndex.state, "building", "slicing ms: /gl bags started the index before the login delay")
    sc.check(any("still reading Questie's quests: type /gl bags again in a moment" in t for t in out),
             "slicing ms: /gl bags says it is still reading (%s)" % out)
    w2.settle(6.0)
    sc.eq(w2.ns.QuestIndex.state, "ready", "slicing ms: ready")
    ticks = [(w2.M.tickReads[i].n, w2.M.tickReads[i].ms) for i in range(1, len(w2.M.tickReads) + 1)]
    per = 3 * 0.02
    sc.eq(sum(n for n, _ in ticks), len(quests), "slicing ms: each quest read once")
    sc.check(all(ms <= 3 + per + 1e-6 for _, ms in ticks), "slicing ms: no frame over 3 ms and one quest (%s)"
             % max(ms for _, ms in ticks))
    sc.check(all(n <= 51 for n, _ in ticks) and max(n for n, _ in ticks) >= 49,
             "slicing ms: about 50 quests a frame (%s)" % sorted(set(n for n, _ in ticks)))
    out = [plain(x) for x in w2.slash("/gl bags")]
    sc.check(any("(not started: Badlands Reagent Run)" in t for t in out), "slicing ms: QuestieDB verdicts once ready (%s)" % out)
    sc.check(not any("still reading" in t for t in out), "slicing ms: no reading note once ready")
    sc.clean(w2, "slicing ms")


def sc_bag_questie_saved(sc, ref, items):
    """The index is saved with QuestieDB's version, used again as long as that
    stays, and made again when QuestieDB changes (or our format does)."""
    w = World(ref, items)
    quests = install_questie(w)
    want = ref_quest_index(quests)
    w.settle(6.0)
    sc.eq(w.ns.QuestIndex.state, "ready", "saved: built after login")
    saved = lua_to_py(w.g.ForeverAH_Settings)
    qi = saved.get("questIndex") or {}
    sc.eq(qi.get("stamp"), QDB_STAMP, "saved: the stamp is QuestieDB's version, commit, contract and our format")
    raw = qi.get("items") or {}
    sc.check(all(isinstance(v, (int, float)) or (isinstance(v, str) and re.match(r"^\d+(,\d+)+$", v)) for v in raw.values()),
             "saved: compact, a code or a string of codes per item")
    got = {k: (sorted(int(c) for c in v.split(",")) if isinstance(v, str) else v) for k, v in raw.items()}
    sc.eq(got, {k: (sorted(v) if isinstance(v, list) else v) for k, v in want.items()}, "saved: the index item -> quest codes")
    sc.eq(got.get(7667), 2200 * 4 + 1, "saved: the phial is given by Back to Uldaman")
    sc.eq(raw.get(7846), "9032,10000", "saved: the fang is asked for by both reagent runs (2258, 2500)")

    def again(label, version="1.1.2", commit=QDB_COMMIT, sv=saved):
        w2 = World(ref, items, saved=sv)
        install_questie(w2, version=version, commit=commit)
        quest_bags(w2, LOG_NECKLACE, done=(2198, 2199, 2200, 2201))
        w2.ns.BagScan()
        return w2
    w2 = again("same")
    sc.eq(w2.ns.QuestIndex.state, "ready", "saved: the same QuestieDB uses the saved index at once")
    sc.check(w2.ns.QuestIndex.saved is True, "saved: and says so")
    w2.settle(6.0)
    sc.eq(w2.M.qdb.objectives, 0, "saved: no quest is read again")
    sc.eq(lua_verdict(w2, 7667), ("done", "Find the Gems", False), "saved: the phial's verdict from the saved index")
    sc.eq(lua_verdict(w2, 7846), ("open", "Badlands Reagent Run", False), "saved: the fang's (two quests in one saved string)")
    n, _ = bagcheck(sc, w2, "saved bagcheck")
    sc.check((n.get(8) or "").endswith(", quests from QuestieDB (%d items, saved)" % len(want)), "saved bagcheck: line 8 (%s)" % n.get(8))
    sc.clean(w2, "saved", ok_chat=BAGCHECK_LINES)
    for label, kw in (("new QuestieDB version", {"version": "1.1.3"}), ("new QuestieDB commit", {"commit": "0123abc"})):
        w3 = again(label, **kw)
        sc.eq(w3.ns.QuestIndex.state, "building", "saved, %s: made again" % label)
        w3.settle(1.0)
        sc.eq(w3.M.qdb.objectives, len(quests), "saved, %s: every quest read again" % label)
        st = lua_to_py(w3.g.ForeverAH_Settings).get("questIndex") or {}
        stamp = "%s|%s|4|1" % (kw.get("version", "1.1.2"), kw.get("commit", QDB_COMMIT))
        sc.eq(st.get("stamp"), stamp, "saved, %s: saved with the new stamp" % label)
        sc.eq(lua_verdict(w3, 7667), ("done", "Find the Gems", False), "saved, %s: verdicts" % label)
        sc.clean(w3, "saved " + label)
    old = dict(saved)
    old["questIndex"] = dict(qi, stamp=QDB_STAMP[:-1] + "0")
    w4 = again("older format", sv=old)
    sc.eq(w4.ns.QuestIndex.state, "building", "saved: an index of an older format is made again")
    w4.settle(1.0)
    sc.eq(w4.ns.QuestIndex.state, "ready", "saved: older format, ready again")
    # QuestieDB that names no version: made each session, not saved.
    w5 = World(ref, items)
    install_questie(w5, version=None, commit=None)
    w5.settle(7.0)
    sc.eq(w5.ns.QuestIndex.state, "ready", "saved: no version stamp, still made")
    sc.check(w5.g.ForeverAH_Settings.questIndex is None, "saved: but not saved without a version stamp")
    sc.clean(w5, "saved no stamp")


def sc_bag_questie_fallback(sc, ref, items):
    """Without QuestieDB, with one whose contract does not match, or one that
    breaks: quest items are judged from the quest log exactly as before."""
    def world(setup=None):
        w = World(ref, items)
        if setup:
            setup(w)
        quest_bags(w, LOG_NECKLACE, done=(2198, 2199, 2200, 2201))
        return w

    def snapshot(w):
        s = w.ns.BagScan()
        w.settle(0.5)
        s = w.ns.BagScan()
        quest = [(s.quest[i].id, s.quest[i].why, bool(s.quest[i].sure), s.quest[i].qdb) for i in range(1, len(s.quest) + 1)]
        tips = [quest_tip(w, bag, slot) for bag, slot, _, _, _, _ in QUEST_BAG]
        return quest, tips
    base = world()
    bq, btips = snapshot(base)
    sc.check(7667 not in [q[0] for q in bq], "no questie: the phial is kept (Restoring the Necklace mentions Talvash): today's silence")
    sc.check(all(q[3] is None for q in bq), "no questie: every verdict is the quest log's")
    sc.eq(btips, [plain_tip(f) for _, _, _, _, _, f in QUEST_BAG], "no questie: no quest line on any tooltip")
    sc.eq(base.ns.QuestIndex.state, "absent", "no questie: the index knows QuestieDB is not loaded")
    base.settle(6.0)
    sc.eq(base.ns.QuestIndex.state, "absent", "no questie: and stays so after the login delay")
    n, _ = bagcheck(sc, base, "no questie bagcheck")
    sc.check(", quests" not in (n.get(8) or "") and "QuestieDB" not in (n.get(8) or ""), "no questie bagcheck: line 8 as before (%s)" % n.get(8))
    sc.clean(base, "no questie", ok_chat=BAGCHECK_LINES)
    cases = [
        ("contract mismatch", lambda w: install_questie(w, contract_fails=True), "contract mismatch"),
        ("no quest list", lambda w: install_questie(w, no_quest_list=True), "no quest reads"),
        ("reads fail", lambda w: (install_questie(w), w.lua.execute("MOCK.qdb.fail = true")), "QuestieDB read failed"),
    ]
    for label, setup, why in cases:
        w = world(setup)
        q, tips = snapshot(w)
        sc.eq(q, bq, "%s: the old quest items are today's" % label)
        sc.eq(tips, btips, "%s: no quest line on any tooltip" % label)
        sc.eq(w.ns.QuestIndex.state, "off", "%s: QuestieDB is not used this session" % label)
        sc.check(why in str(w.ns.QuestIndex.err), "%s: and why (%s)" % (label, w.ns.QuestIndex.err))
        n, _ = bagcheck(sc, w, label + " bagcheck")
        sc.check("QuestieDB not used (" in (n.get(8) or ""), "%s bagcheck: line 8 says QuestieDB is not used (%s)" % (label, n.get(8)))
        sc.clean(w, label, ok_chat=BAGCHECK_LINES + ("QuestieDB not used",))
    # Reads that break after the index was made: back to the quest log, the saved index dropped.
    w = World(ref, items)
    install_questie(w)
    quest_bags(w, LOG_NECKLACE, done=(2198, 2199, 2200, 2201))
    w.settle(6.0)
    sc.check(w.g.ForeverAH_Settings.questIndex is not None, "breaks later: the index was saved")
    w.lua.execute("MOCK.qdb.fail = true")
    q, tips = snapshot(w)
    sc.eq(q, bq, "breaks later: the old quest items are today's")
    sc.eq(tips, btips, "breaks later: no quest line")
    sc.eq(w.ns.QuestIndex.state, "off", "breaks later: QuestieDB is not used")
    sc.check(w.g.ForeverAH_Settings.questIndex is None, "breaks later: the saved index is dropped (made again next time)")
    sc.clean(w, "breaks later")


# A tool and a note from the same chain: the phial (Use: it creates the scrying bowl) and the
# Bloodstained Journal, which Find the Gems gives you: readable, soulbound, no price, and not
# marked as a quest item.
JOURNAL_BAG = [
    (0, 1, 7667, 1, "Talvash's Phial of Scrying", {"classID": 12, "bind": 4, "maxStack": 1, "useSpell": "Create Scrying Bowl"}),
    (0, 2, 7668, 1, "Bloodstained Journal", {"classID": 15, "bind": 1, "maxStack": 1}),
]


def sc_bag_questie_journal(sc, ref, items):
    """With QuestieDB, an item a quest gave you that is not marked as a quest item (the
    Bloodstained Journal) gets the same tooltip line and cleanup row as a quest item, and
    only a tool you use (the phial) counts for the quest after the one that gave it: the
    journal can go once Find the Gems is done, the phial waits for Restoring the Necklace."""
    w = World(ref, items)
    M, ns, g = w.M, w.ns, w.g
    quests = install_questie(w)
    for bag, slot, iid, count, name, fields in JOURNAL_BAG:
        bag_add_item(w, iid, name, 1, 0, **fields)
        bag_put(w, bag, slot, iid, count)
    pos, buy = QDB_COLORS["pos"], QDB_COLORS["buy"]

    def state(log, done):
        bag_quests(w, log)
        for q in (2198, 2199, 2200, 2201):
            M.questDone[q] = q in done
        M.fire("QUEST_LOG_UPDATE")
        w.settle(0.5)
    state(LOG_FIND_GEMS, (2198, 2199, 2200))
    quest_tip(w, 0, 2)                     # the first need starts the index
    w.settle(0.5)
    sc.eq(ns.QuestIndex.state, "ready", "journal: the index is ready")
    # On Find the Gems: both kept for it.
    sc.eq(lua_verdict(w, 7668), ("keep", "Find the Gems", False), "journal: while on Find the Gems: keep")
    sc.eq(lua_verdict(w, 7667), ("keep", "Find the Gems", False), "journal: the phial too")
    sc.eq(quest_tip(w, 0, 2), [("Keep: needed for Find the Gems", buy)], "journal: tooltip, gold keep line")
    quest_checks(sc, w, quests, {2201}, {2198, 2199, 2200}, "journal on Find the Gems", bag_list=JOURNAL_BAG)
    # Find the Gems done, Restoring the Necklace not taken: the journal can go, the phial is
    # how you start the next quest (its bowl), so it stays.
    state([{"title": "Uldaman", "isHeader": True}], (2198, 2199, 2200, 2201))
    sc.eq(lua_verdict(w, 7668), ("done", "Find the Gems", False), "journal: after Find the Gems: can drop")
    sc.eq(lua_verdict(w, 7667), ("keep", "Restoring the Necklace", False), "journal: the phial waits for the next quest")
    sc.eq(quest_tip(w, 0, 2), [("Can drop: done with Find the Gems", pos)], "journal: tooltip, green can drop line")
    sc.eq(quest_tip(w, 0, 1), [("Keep: needed for Restoring the Necklace", buy)], "journal: phial tooltip, gold keep line")
    quest_checks(sc, w, quests, set(), {2198, 2199, 2200, 2201}, "journal after Find the Gems", bag_list=JOURNAL_BAG)
    M.OpenBags(1)
    w.settle(0.2)
    BP = ns.BagPanel
    M.click(BP.tab)
    w.settle(0.2)
    sc.eq(quest_rows(BP), [(7668, "done", pos)], "journal panel: listed as done, the phial is not")
    row = bag_row_for(BP, 7668)
    if sc.check(row is not None, "journal panel: the journal has a row"):
        sc.check(row.entry.questItem is True, "journal panel: a click only picks it up (never the drop box)")
        M.enter(row)
        lines = [plain(l) for l, _ in w.all_lines(g.GameTooltip)]
        sc.check("Can drop: done with Find the Gems." in lines, "journal panel: the row tooltip says it can go (%s)" % lines)
        M.leave(row)
    out = [plain(x) for x in w.slash("/gl bags")]
    sc.check(any("[Bloodstained Journal] (done: Find the Gems)" in t for t in out), "journal /gl bags (%s)" % out)
    # On Restoring the Necklace: both can go.
    state(LOG_NECKLACE, (2198, 2199, 2200, 2201))
    sc.eq(lua_verdict(w, 7668), ("done", "Find the Gems", False), "journal: on Restoring the Necklace: can drop")
    sc.eq(lua_verdict(w, 7667), ("done", "Find the Gems", False), "journal: the phial too")
    quest_checks(sc, w, quests, {2204}, {2198, 2199, 2200, 2201}, "journal on Restoring the Necklace", bag_list=JOURNAL_BAG)
    M.CloseBags()
    w.settle(0.2)
    sc.clean(w, "journal")


def sc_bag_quest_log_fallback(sc, ref, items):
    """Without QuestieDB: when the client gives no objective texts through
    C_QuestLog.GetQuestObjectives, the quest log's own lines are read, so a note its quest
    names is kept; and a quest item no quest names, while a quest in the log lists nothing,
    says it is not sure (never "no quest in your log asks for it" as a fact)."""
    w = World(ref, items)
    M, ns, g = w.M, w.ns, w.g
    bag_add_item(w, 904649, "Bonegrip's Note", 1, 0, classID=12, bind=4, maxStack=1)
    bag_put(w, 0, 1, 904649, 1)
    bag_add_item(w, 904650, "Dusty Key", 1, 0, classID=12, bind=4, maxStack=1)
    bag_put(w, 0, 2, 904650, 1)
    bag_quests(w, [{"title": "Burning Steppes", "isHeader": True},
                   {"title": "The Star, the Hand and the Heart", "questID": 950735, "objectives": ["Bonegrip's Note: 1/1"]},
                   {"title": "Talk to the Smith", "questID": 950736, "objectives": []}])
    M.questObjectivesEmpty = True
    M.OpenBags(1)
    w.settle(0.2)
    BP = ns.BagPanel
    M.click(BP.tab)
    w.settle(0.2)
    ids = [r[0] for r in quest_rows(BP)]
    sc.check(904649 not in ids, "log fallback: the note its quest names (from the quest log lines) is kept (%s)" % ids)
    sc.check(904650 in ids, "log fallback: the key no quest names is listed (%s)" % ids)
    row = bag_row_for(BP, 904650)
    if sc.check(row is not None, "log fallback: the key has a row"):
        sc.eq(row.right._text, "check log", "log fallback: a quest that lists nothing makes it a 'check log'")
        M.enter(row)
        lines = [plain(l) for l, _ in w.all_lines(g.GameTooltip)]
        sc.check(any(l.startswith("Not sure: no quest in your log names it") for l in lines),
                 "log fallback: the row tooltip says it is not sure (%s)" % lines)
        sc.check(not any(l.startswith("This ") or l == "No quest in your log asks for it." for l in lines),
                 "log fallback: and never states it as a fact (%s)" % lines)
        M.leave(row)
    M.CloseBags()
    w.settle(0.2)
    sc.clean(w, "log fallback")


SCENARIOS = [
    ("static-source", sc_static),
    ("load", sc_load),
    ("coin-global", sc_bare_coin),
    ("decode+recommend", sc_decode),
    ("status-slash", sc_status),
    ("tooltips", sc_tooltip),
    ("tooltips-legacy-hooks", sc_tooltip_legacy_hooks),
    ("stale-data", sc_stale),
    ("placeholder-data", sc_placeholder),
    ("saved-settings", sc_saved_settings),
    ("chart-standalone", sc_chart_standalone),
    ("chart-timeframes", sc_chart_timeframes),
    ("chart-hover-no-clear", sc_chart_no_setowner_clear),
    ("ah-modern", sc_ah_modern),
    ("ah-modern-tall", sc_ah_modern_tall),
    ("ah-modern-short-room", sc_ah_modern_short),
    ("ah-live-prices", sc_live_prices),
    ("ah-other-house", sc_other_house),
    ("ah-suffix-gear", sc_suffix_gear),
    ("chart-drag-docked", sc_chart_drag_docked),
    ("ah-modern-elvui", sc_ah_modern_elvui),
    ("ah-tsm-shrink", sc_ah_tsm),
    ("ah-no-copper", sc_ah_no_copper),
    ("ah-legacy", sc_ah_legacy),
    ("ah-auctionator", sc_auctionator),
    ("horde+hardcore", sc_horde),
    ("edge-data", sc_edge),
    ("sidebar", sc_sidebar),
    ("sidebar-saved", sc_sidebar_saved),
    ("sidebar-no-data", sc_sidebar_nodata),
    ("sidebar-wide", sc_sidebar_wide),
    ("sidebar-first-snapshot", sc_sidebar_first_snapshot),
    ("sales-hours", sc_sales_hours),
    ("one-snapshot", sc_one_snapshot),
    ("short-history", sc_short_history),
    ("unreliable-prices", sc_unreliable),
    ("unknown-and-shared-names", sc_names),
    ("y-axis-tiny-range", sc_y_axis),
    ("chart-line-toggles", sc_chart_lines),
    ("gear-versions", sc_versions),
    ("collector-flips", sc_data_flips),
    ("flip-gold-sizing", sc_flip_gold),
    ("flip-deals", sc_flip_deals),
    ("flip-deal-room", sc_flip_deal_room),
    ("flip-deal-words", sc_flip_deal_words),
    ("flip-history-rank", sc_flip_history_rank),
    ("flip-no-gold-api", sc_flip_no_gold),
    ("flip-day-stats", sc_flip_day_stats),
    ("flip-day-value", sc_flip_day_value),
    ("flip-day-drops", sc_flip_day_drops),
    ("flip-day-expected", sc_flip_day_expected),
    ("flip-day-deals", sc_flip_day_deals),
    ("flip-day-place", sc_flip_day_place),
    ("flip-day-prices", sc_flip_day_prices),
    ("flip-day-rows", sc_flip_day_rows),
    ("flip-day-hover", sc_flip_day_hover),
    ("flip-day-slash", sc_flip_day_slash),
    ("flip-day-first-snapshot", sc_flip_day_first_snapshot),
    ("flip-day-model", sc_flip_day_model),
    ("flip-day-live", sc_flip_day_live),
    ("old-data-format", sc_old_format),
    ("stack-verdicts", sc_stack_verdicts),
    ("tooltip-verdicts", sc_tooltip_verdicts),
    ("tooltip-fonts", sc_tooltip_fonts),
    ("verdict-parity", sc_verdict_parity),
    ("bag-cleanup", sc_bag_cleanup),
    ("bag-cleanup-no-data", sc_bag_cleanup_nodata),
    ("bag-cleanup-elvui", sc_bag_cleanup_elvui),
    ("bag-cleanup-parked", sc_bag_cleanup_parked),
    ("bag-tab-header", sc_bag_tab_header),
    ("bag-drop", sc_bag_drop),
    ("bag-drop-elvui", sc_bag_drop_elvui),
    ("bag-elvui-hooks", sc_bag_elvui_hooks),
    ("bag-window-unreadable", sc_bag_window_unreadable),
    ("bag-check", sc_bag_check),
    ("bag-questie", sc_bag_questie),
    ("bag-questie-chain", sc_bag_questie_chain),
    ("bag-questie-slicing", sc_bag_questie_slicing),
    ("bag-questie-saved", sc_bag_questie_saved),
    ("bag-questie-fallback", sc_bag_questie_fallback),
    ("bag-questie-journal", sc_bag_questie_journal),
    ("bag-quest-log-fallback", sc_bag_quest_log_fallback),
]


def ensure_data():
    if os.path.exists(DATA_FILE):
        return True
    if DATA_FILE != SIM_DATA_FILE:
        print("Data file not found:", DATA_FILE)
        return False
    if not os.path.exists(SIM):
        print("Simulated data file is missing and sim_fetch.py was not found:", SIM)
        return False
    print("Generating simulated data with sim_fetch.py ...")
    subprocess.run([sys.executable, SIM], check=False)
    return os.path.exists(DATA_FILE)


def parse_args(argv):
    """-v, scenario name filters, --data <file> and --items <json> (or FAH_DATA_FILE / FAH_ITEMS_JSON)."""
    opts = {"verbose": False, "only": [], "data": os.environ.get("FAH_DATA_FILE") or None,
            "items": os.environ.get("FAH_ITEMS_JSON") or None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-v", "--verbose"):
            opts["verbose"] = True
        elif a in ("--data", "--items"):
            if i + 1 >= len(argv):
                raise SystemExit("%s needs a file name" % a)
            opts[a[2:]] = argv[i + 1]
            i += 1
        elif a.startswith("--data=") or a.startswith("--items="):
            key, _, val = a[2:].partition("=")
            opts[key] = val
        elif a.startswith("-"):
            raise SystemExit("unknown option %s (use -v, --data <file>, --items <json>)" % a)
        else:
            opts["only"].append(a)
        i += 1
    return opts


def load_items(path, ref):
    """items.json (names, quality, vendor price for the mock game). Without one, the
    names and vendor prices in the data file are used."""
    import json
    if path and os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    if path:
        print("items file not found, using the names in the data file:", path)
    return {str(iid): {"name": it["name"], "sell": it["vendor"], "quality": "COMMON"} for iid, it in ref.items.items()}


def main(argv):
    global DATA_FILE, ITEMS_FILE
    opts = parse_args(argv)
    verbose, only = opts["verbose"], opts["only"]
    simulated = not opts["data"]
    if opts["data"]:
        DATA_FILE = os.path.abspath(opts["data"])
        ITEMS_FILE = os.path.abspath(opts["items"]) if opts["items"] else os.path.join(os.path.dirname(DATA_FILE), "items.json")
    elif opts["items"]:
        ITEMS_FILE = os.path.abspath(opts["items"])
    if not ensure_data():
        return 2
    ref = RefData(DATA_FILE)
    items = load_items(ITEMS_FILE, ref)
    pick_roles(ref, items, simulated)
    fmt = "new" if ref.salesHours is not None else "older (no salesHours, no listings count)"
    print("Data: %d items, updated %d, %s data file format, %s" % (
        len(ref.items), ref.updated, fmt,
        "SIMULATED test data, not real prices" if simulated else "from " + DATA_FILE))
    if not simulated:
        print("Test items: %s; no-data items %s; big files are checked on a sample of %d items" % (
            ", ".join("%s %d" % (N(i), i) for i in ROLE_IDS()), [R.NO_GEAR, R.NO_BOLT, R.NO_BAG], SAMPLE))
    results = []
    for name, fn in SCENARIOS:
        if only and not any(o in name for o in only):
            continue
        sc = Scenario(name, verbose)
        t0 = time.time()
        try:
            fn(sc, ref, items)
        except Exception as e:  # a crash in the harness or a Lua error raised into Python
            import traceback
            sc.failed.append("scenario crashed: %s" % e)
            if verbose:
                traceback.print_exc()
        results.append(sc)
        status = "PASS" if not sc.failed else "FAIL"
        print("%-24s %s  %5d checks passed, %d failed  (%.1fs)" % (name, status, sc.passed, len(sc.failed), time.time() - t0))
        for f in sc.failed[:12]:
            print("      - " + f[:400])
        if len(sc.failed) > 12:
            print("      ... and %d more" % (len(sc.failed) - 12))
    bad = [s for s in results if s.failed]
    total = sum(s.passed for s in results)
    print("\n%d scenarios, %d checks passed, %d scenarios failed" % (len(results), total, len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

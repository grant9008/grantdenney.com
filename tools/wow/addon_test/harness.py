"""Integration harness for the ForeverAH addon.

Loads the five addon files in TOC order inside Lua 5.1 (lupa), with a mock of
the WoW API surface the addon touches, and the real generated (simulated)
price file wow/_localdata/ForeverAH_Data.lua as Data.lua. Then it plays the
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
ADDON = os.environ.get("FAH_ADDON_DIR") or os.path.join(ROOT, "wow-addon", "ForeverAH")
LOCAL_DIR = os.path.join(ROOT, "wow", "_localdata")
SIM_DATA_FILE = os.path.join(LOCAL_DIR, "ForeverAH_Data.lua")
SIM_ITEMS_FILE = os.path.join(LOCAL_DIR, "items.json")
# The data under test: the simulated file unless --data / FAH_DATA_FILE says otherwise (see main).
DATA_FILE = SIM_DATA_FILE
ITEMS_FILE = SIM_ITEMS_FILE
SIM = os.path.join(HERE, "sim_fetch.py")  # makes the simulated test data in wow/_localdata
SAMPLE = 250   # data files with more items than this are checked on a fixed sample of them

TOC_FILES = ["Data.lua", "Core.lua", "Recommend.lua", "Chart.lua", "AH.lua", "Sidebar.lua", "Bags.lua"]
TOC_HEADER = [
    "## Interface: 16001",
    "## Title: Forever AH",
    "## Notes: Auction house prices, charts and price advice from grantdenney.com/wow",
    "## SavedVariables: ForeverAH_Settings",
    "## Version: 1.0.0",
]
PLACEHOLDER = ("-- ForeverAH price data placeholder. Run Update-WoW-Prices.bat.\n"
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
-- plumbing. ForeverAH must never be that frame.
local function guard(name, kind)
  for lvl = 3, 60 do
    local info = dbg.getinfo(lvl, "S")
    if not info then return end
    local src = info.source or ""
    if info.what ~= "C" and src:sub(1, 6) ~= "@MOCK/" then
      if src:find("ForeverAH", 1, true) then
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

-- Font strings -----------------------------------------------------------------
local FONTS = {}
for _, n in ipairs({ "GameFontNormal", "GameFontNormalSmall", "GameFontNormalLarge", "GameFontHighlight",
  "GameFontHighlightSmall", "GameFontHighlightLarge", "GameFontDisable", "GameFontDisableSmall", "NumberFontNormal",
  "GameTooltipText", "GameTooltipTextSmall", "GameFontWhite", "GameFontNormalHuge", "SystemFont_Small" }) do
  local f = { _font = n }
  function f:GetFont() return "Fonts\\FRIZQT__.TTF", 12, "" end
  _G[n] = f
  FONTS[n] = f
end
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
function FontString:SetFontObject(f) self._font = f end
function FontString:SetFont(path, size, flags) self._fontPath = path; return true end
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
function Tooltip:_Clear()
  self._lines = {}
  self._blz = 0
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
end
function Tooltip:AddDoubleLine(lt, rt, lr, lg, lb, rr, rg, rb)
  checkColor("GameTooltip:AddDoubleLine", lr, lg, lb, rr, rg, rb)
  local l = lt ~= nil and tostring(lt) or ""
  local r = rt ~= nil and tostring(rt) or ""
  M.seeText(l); M.seeText(r)
  table.insert(self._lines, { left = l, right = r, lr = lr, lg = lg, lb = lb, rr = rr, rg = rg, rb = rb })
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

function M.click(btn, mouse)
  if not isRegion(btn) then M.err("harness", "click on a non-button"); return false end
  if not btn:IsVisible() then M.err("harness", "clicked a button that is not visible: " .. tostring(btn)); return false end
  btn:Click(mouse)
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

C_Container = {}
function C_Container.GetContainerItemInfo(bag, slot)
  local s = M.bags[bag] and M.bags[bag][slot]
  if not s then return nil end
  local it = M["items"][s.itemID]
  return { iconFileID = it and it.icon or 134400, stackCount = s.count, isLocked = s.locked == true, quality = it and it.quality or 1,
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
    if here then M.held = { bag = bag, slot = slot } end
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
  if M.held then local s = M.bags[M.held.bag][M.held.slot]; return "item", s and s.itemID end
  if M.cursorItem then return "item", M.cursorItem.itemID end
end
function ClearCursor() M.held = nil; M.cursorItem = nil end
function DeleteCursorItem()
  M.violations[#M.violations + 1] = "DeleteCursorItem (destroys an item without the game asking)"
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
-- How many bag windows to open (1 = the backpack); columns of 3 from the bottom right.
function M.OpenBags(n)
  for i = 1, (n or 5) do
    local f = _G["ContainerFrame" .. i]
    f:SetID(i - 1)
    f:ClearAllPoints()
    local col, row = math.floor((i - 1) / 3), (i - 1) % 3
    f:SetPoint("BOTTOMRIGHT", UIParent, "BOTTOMRIGHT", -20 - col * 190, 100 + row * 180)
    f:Show()
  end
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

C_GameRules = { IsHardcoreActive = function() return M.hardcore end }
C_AddOns = {}
function C_AddOns.IsAddOnLoaded(name) return M.loaded[name] == true, M.loaded[name] == true end
function C_AddOns.GetAddOnMetadata(name, key) if name == "ForeverAH" and key == "Version" then return "1.0.0" end end
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
                 set_owner_clears=True, copper_values=True, gold=MOCK_GOLD):
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
        if auctionator:   # loads before ForeverAH (alphabetical), its ADDON_LOADED fires before ours
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
            ok = M.LoadChunk(src, "@Interface/AddOns/ForeverAH/" + fname, "ForeverAH", self.ns)
            if not ok:
                self.load_errors.append(fname)
        # SavedVariables are restored after the addon's files run and before ADDON_LOADED.
        if saved is not None:
            self.g.ForeverAH_Settings = self.to_lua(saved)
        M.loaded["ForeverAH"] = True
        M.fire("ADDON_LOADED", "ForeverAH")
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

    def clean(self, w, label="end"):
        """No Lua errors anywhere, no swallowed addon errors, no forbidden calls, no long dashes."""
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
        v = [w.M.violations[i] for i in range(1, len(w.M.violations) + 1)]
        self.check(not v, "%s: addon never sends AH queries or restricted calls (%s)" % (label, v[:3]))
        d = [w.M.dash[i] for i in range(1, len(w.M.dash) + 1)]
        self.check(not d, "%s: no long dash in any visible text (%s)" % (label, d[:2]))
        bad = [c for c in w.chat() if "error" in c.lower()]
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


def stack_lines(it, unit_now, count):
    """The tooltip's stack lines (Core.lua addStackLines), only with a Sell now
    price: for a stack, what it brings after the AH cut; then "Vendor it" when a
    vendor pays as much or more."""
    auction, vend, verdict = rec_model.stack_values(unit_now, count, it["vendor"])
    if auction is None:
        return []
    out = []
    if count > 1:
        out.append(("This stack (%d), after AH cut" % count, money_icons(auction)))
    if verdict == "same":
        out.append(("Vendor it: a vendor pays the same", None))
    elif verdict == "vendor":
        out.append(("Vendor it: a vendor pays more", None))
    return out


def expected_tooltip(ref, iid, age, mult=1, compact=False, count=None, live=None, own=False, tf="1D", cover="auto"):
    """The Forever AH lines of an item tooltip. mult > 1: Shift held on a stack of
    that many (count defaults to it). cover: "version", "all", None, or "auto"
    (plain link: "all" for an item whose versions the data lists)."""
    it = ref.items.get(iid)
    if it is None:
        return [("Forever AH: no recent listings", None)]
    if count is None:
        count = mult if mult > 1 else 1
    if cover == "auto":
        cover = "all" if iid in ref.variants else None
    tag = " (x%d)" % mult if mult > 1 else ""

    def m(v):
        return money_icons(v * mult) if v > 0 else None
    out = []
    vs = vs_average(it["market"], it["avg7"])
    if compact:
        mk = m(it["market"])
        line = ("AH value%s: %s" % (tag, mk) + ("  " + vs if vs else "")) if mk else "AH value: none listed now"
        out.append((line, None))
    else:
        out.append(("Forever AH", age_text(age)))
        mk = m(it["market"])
        out.append(("AH value" + tag, mk + ("  " + vs if vs else "")) if mk else ("AH value", "none listed"))
        if m(it["avg7"]):
            out.append((average_label(ref, iid) + tag, m(it["avg7"])))
        if cover == "all":
            # the prices mix versions that sell for different prices: nothing to sell at
            P = {"now": None}
        else:
            P = ref_sell_prices(ref, iid, tf, live, own)
            if P["now"] is not None:
                out.append((("Sell now (live)" if P["live"] else "Sell now") + tag, m(P["now"])))
            if P["best"] is not None:
                out.append(("Suggested price" + tag, m(P["best"])))
            if P["nowWhy"] == "unreliable" or P["bestWhy"] == "unreliable":
                out.append(("Few listed: the price may not be reliable", None))
        out += stack_lines(it, P["now"], count)
    if cover == "version":
        out.append(("Prices for this version only", None))
    elif cover == "all":
        out.append(("Price covers all versions of this item", None))
    if age > 6 * 3600:
        out.append(("Prices are %dh old: run Update-WoW-Prices.bat, then /reload" % (age // 3600), None))
    return out


def tooltip_values_ok(sc, ref, iid, lines, mult, label, live=None, tf="1D"):
    """Value-level check, independent of wording: every price on the tooltip
    equals the data file (AH value, the average) or the model (Sell now,
    Suggested price)."""
    it = ref.items[iid]
    P = ref_sell_prices(ref, iid, tf, live)
    want = {"AH value": it["market"], average_label(ref, iid): it["avg7"],
            "Sell now": P["now"], "Sell now (live)": P["now"], "Suggested price": P["best"]}
    seen = set()
    for left, right in lines:
        base = (left or "").split(" (x")[0]
        coins = (right or "").split("  ")[0]        # the AH value's "+3%" comes after two spaces
        if base in want and coins and parse_coins(coins) is not None:
            seen.add(base)
            sc.eq(parse_coins(coins), display_copper((want[base] or 0) * mult), "%s %s value" % (label, base))
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
    files = TOC_FILES + ["ForeverAH.toc", "ForeverAH_Forever.toc"]
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


def sc_load(sc, ref, items):
    toc = open(os.path.join(ADDON, "ForeverAH.toc"), "rb").read()
    toc2 = open(os.path.join(ADDON, "ForeverAH_Forever.toc"), "rb").read()
    sc.eq(toc, toc2, "ForeverAH_Forever.toc identical to ForeverAH.toc")
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
    sc.eq(w.g.SLASH_FOREVERAH1, "/fah", "SLASH_FOREVERAH1")
    sc.eq(w.g.SLASH_FOREVERAH2, "/foreverah", "SLASH_FOREVERAH2")
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
    out = w.slash("/fah")
    txt = "\n".join(out)
    # The count is written the addon's way: 6,641
    sc.check(("prices for %s items from the %s" % (commas(ref.base_count()), ref.houseName)) in txt,
             "status names item count and house (%s)" % out[:1])
    sc.check("(US, PvP)" in txt, "status names region and ruleset")
    sc.check("updated 25m ago" in txt, "status shows data age")
    sc.check("Type /fah help" in txt, "status points at help")
    nv = len(ref.base_of)
    sc.eq(any(("%s versions of gear" % commas(nv)) in l for l in out), nv > 0,
          "status says how many gear versions have their own prices (%d)" % nv)
    sc.check("old" not in txt, "no stale warning for fresh data")
    sc.eq(w.slash("/foreverah status"), out, "/foreverah alias gives the same status")
    h = w.slash("/fah help")
    sc.check(len(h) >= 6 and any("/fah chart" in l for l in h), "help lists commands")
    u = w.slash("/fah bogus")
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
    sc.eq(lines, expected_tooltip(ref, R.LINEN, age, count=20), "2589 bag tooltip lines")
    tooltip_values_ok(sc, ref, R.LINEN, lines, 1, "2589")
    # Post call fires a second time without a clear: no duplicates
    for fn_i in range(1, len(w.M.postCalls[0]) + 1):
        w.M.postCalls[0][fn_i](tt, tt.processingInfo.tooltipData)
    sc.eq(w.lines(tt), lines, "2589 second post call adds nothing")
    # Rebuild (modifier change / TOOLTIP_DATA_UPDATE): cleared and rebuilt once
    tt.RefreshData(tt)
    sc.eq(w.lines(tt), lines, "2589 rebuild gives one copy")
    heads = [l for l in w.all_lines(tt) if l[0] == "Forever AH"]
    sc.eq(len(heads), 1, "one Forever AH header")
    # Black Lotus by id
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetItemByID(tt, R.LOTUS)
    lines = w.lines(tt)
    sc.eq(lines, expected_tooltip(ref, R.LOTUS, age), "13468 tooltip lines")
    tooltip_values_ok(sc, ref, R.LOTUS, lines, 1, "13468")
    if min(ref.items[R.LOTUS]["market"], ref.items[R.LOTUS]["cheapest"]) > 1000000:
        sc.check(all("Copper" not in (r or "") for _, r in lines[1:4]), "13468 above 100g: copper dropped")
    # Unknown item
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetItemByID(tt, R.NO_GEAR)
    sc.eq(w.lines(tt), [("Forever AH: no recent listings", None)], "unknown item tooltip")
    # Shift held on a stack of 20
    w.M.shift = True
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetBagItem(tt, 0, 1)
    lines = w.lines(tt)
    sc.eq(lines, expected_tooltip(ref, R.LINEN, age, mult=20), "2589 shift x20 tooltip")
    tooltip_values_ok(sc, ref, R.LINEN, lines, 20, "2589 x20")
    # Shift held on a single item (by id): no multiplier
    tt.SetItemByID(tt, R.RUNE)
    sc.eq(w.lines(tt), expected_tooltip(ref, R.RUNE, age), "14047 shift, count 1: unit prices")
    # Shift released, MODIFIER_STATE_CHANGED rebuild on the bag item
    tt.SetBagItem(tt, 0, 1)
    w.M.shift = False
    tt.RefreshData(tt)
    sc.eq(w.lines(tt), expected_tooltip(ref, R.LINEN, age, count=20), "2589 after shift released")
    # Every item (a sample of a big file), full and compact
    ids = sample_ids(ref, SAMPLE * 4)
    for iid in ids:
        tt.SetItemByID(tt, iid)
        sc.eq(w.lines(tt), expected_tooltip(ref, iid, age), "tooltip %d" % iid)
    out = w.slash("/fah compact")
    sc.check(any("one line" in l for l in out), "/fah compact message")
    for iid in ids:
        tt.SetItemByID(tt, iid)
        sc.eq(w.lines(tt), expected_tooltip(ref, iid, age, compact=True), "compact tooltip %d" % iid)
    w.M.shift = True
    tt.SetBagItem(tt, 0, 1)
    sc.eq(w.lines(tt), expected_tooltip(ref, R.LINEN, age, mult=20, compact=True), "compact x20 tooltip")
    w.M.shift = False
    w.slash("/fah compact")
    tt.SetItemByID(tt, R.LINEN)
    sc.eq(w.lines(tt), expected_tooltip(ref, R.LINEN, age), "full tooltip again after second /fah compact")
    # Tooltip off and on
    out = w.slash("/fah tooltip")
    sc.check(any("off" in l for l in out), "/fah tooltip off message")
    tt.SetItemByID(tt, R.LINEN)
    sc.eq(w.lines(tt), [], "tooltip off: no lines")
    w.slash("/fah tooltip")
    tt.SetItemByID(tt, R.LINEN)
    sc.eq(w.lines(tt), expected_tooltip(ref, R.LINEN, age), "tooltip on again")
    # Other tooltips
    ir = w.g.ItemRefTooltip
    ir.SetHyperlink(ir, w.M.ItemLink(R.ARCANE))
    sc.eq(w.lines(ir), expected_tooltip(ref, R.ARCANE, age), "ItemRefTooltip from a chat link")
    st = w.g.ShoppingTooltip1
    st.SetItemByID(st, R.RUNE)
    sc.eq(w.lines(st), expected_tooltip(ref, R.RUNE, age), "comparison tooltip")
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
        sc.check(lines[1][0] == "AH value" and (lines[1][1] or "").endswith("  |c%s%s|r" % (col, pct_txt)),
                 "%d: AH value %s from the average, in its colour (%s)" % (iid, pct_txt, lines[1]))
        sc.check(not any(a.startswith(("7-day low", "7-day high", "Cheapest", "Market", "Listed ", "Best price"))
                         for a, _ in lines), "%d: none of the old lines (%s)" % (iid, lines))
    sc.clean(w2, "average")


def sc_tooltip_legacy_hooks(sc, ref, items):
    w = World(ref, items, client="legacy")
    sc.eq(w.ns.tooltipMode, "script", "legacy client: OnTooltipSetItem fallback")
    tt = w.g.GameTooltip
    age = w.now - ref.updated
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetItemByID(tt, R.LINEN)
    sc.eq(w.lines(tt), expected_tooltip(ref, R.LINEN, age), "legacy tooltip lines")
    w.M.fireScript(tt, "OnTooltipSetItem")
    sc.eq(w.lines(tt), expected_tooltip(ref, R.LINEN, age), "legacy second OnTooltipSetItem adds nothing")
    sc.clean(w)


def sc_stale(sc, ref, items):
    w = World(ref, items, now=ref.updated + 7 * 3600 + 120)
    age = w.now - ref.updated
    tt = w.g.GameTooltip
    tt.SetItemByID(tt, R.LINEN)
    lines = w.lines(tt)
    sc.eq(lines, expected_tooltip(ref, R.LINEN, age), "stale tooltip lines")
    sc.check(lines[-1][0] == "Prices are 7h old: run Update-WoW-Prices.bat, then /reload", "stale line text")
    w.slash("/fah compact")
    tt.SetItemByID(tt, R.LINEN)
    sc.eq(w.lines(tt), expected_tooltip(ref, R.LINEN, age, compact=True), "stale compact tooltip keeps the stale line")
    out = "\n".join(w.slash("/fah"))
    sc.check("updated 7h ago" in out, "status shows 7h")
    sc.check("Prices are 7h old: run Update-WoW-Prices.bat, then /reload." in out, "status stale warning")
    w.slash("/fah chart " + N(R.LINEN))
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
    out = w.slash("/fah")
    sc.check(any("Update-WoW-Prices.bat" in l for l in out) and any("/reload" in l for l in out), "status explains the updater")
    tt = w.g.GameTooltip
    tt.SetItemByID(tt, R.LINEN)
    sc.eq(w.lines(tt), [], "no tooltip lines without data")
    out = w.slash("/fah chart Linen Cloth")
    sc.check(any("no price data" in l for l in out), "/fah chart item: no data message")
    w.slash("/fah chart")
    C = ns.ChartPanel
    sc.check(C.frame.IsShown(C.frame), "/fah chart opens the empty chart")
    sc.check(C.msg._shown and "No price data yet" in (C.msg._text or ""), "empty chart message (%s)" % C.msg._text)
    sc.eq(C.age._text, "Data: none yet", "empty chart age")
    w.slash("/fah chart")
    sc.check(not C.frame.IsShown(C.frame), "/fah chart toggles it off")
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
    w.slash("/fah chart")
    C = w.ns.ChartPanel
    sc.eq(w.ns.ChartItem(), R.RUNE, "/fah chart reopens the last item")
    p = C.frame._points[1]
    sc.check(p[1] == "TOPLEFT" and w.same(p[2], w.g.UIParent) and p[4] == 100 and p[5] == -200, "saved chart position used")
    chart_check(sc, w, ref, R.RUNE, "1W", "saved 1W")
    sc.clean(w)


def sc_chart_standalone(sc, ref, items):
    w = World(ref, items)
    ns = w.ns
    out = w.slash("/fah chart " + N(R.RUNE))
    C = ns.ChartPanel
    F = C.frame
    sc.check(F.IsShown(F), "/fah chart Runecloth shows the chart")
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
    w.slash("/fah chart " + w.M.ItemLink(R.LOTUS))
    sc.eq(ns.ChartItem(), R.LOTUS, "/fah chart with an item link")
    w.slash("/fah chart %d" % R.LINEN)
    sc.eq(ns.ChartItem(), R.LINEN, "/fah chart with an item id")
    out = w.slash("/fah chart Not A Real Item")
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
    w.slash("/fah chart")
    sc.check(F.IsShown(F) and ns.ChartItem() == R.LINEN, "/fah chart reopens the last item")
    # Close button while standalone keeps settings.chart
    M = w.M
    M.click(C.closeButton)
    sc.check(not F.IsShown(F), "close button hides the chart")
    sc.eq(ns.settings.chart, True, "closing a standalone chart keeps the AH chart setting")
    sc.clean(w)


def sc_chart_timeframes(sc, ref, items):
    w = World(ref, items)
    ns, M = w.ns, w.M
    w.slash("/fah chart " + N(R.RUNE))
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
    w.slash("/fah chart " + N(R.RUNE))
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
    # /fah chart at the AH after closing it with x: follows searches again
    M.click(C.closeButton)
    w.slash("/fah chart")
    sc.check(C.frame.IsShown(C.frame), "%s: /fah chart at the AH shows the chart" % label)
    sb.SearchBox.SetText(sb.SearchBox, N(R.LOTUS))
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), R.LOTUS, "%s: chart opened with /fah chart at the AH follows searches" % label)
    w.slash("/fah chart")
    sc.check(not C.frame.IsShown(C.frame), "%s: /fah chart hides the docked chart" % label)
    sc.eq(ns.settings.chart, False, "%s: /fah chart hide at the AH turns the AH chart off" % label)
    sb.SearchBox.SetText(sb.SearchBox, N(R.RUNE))
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.check(not C.frame.IsShown(C.frame), "%s: hidden chart stays hidden on search" % label)
    w.slash("/fah chart " + N(R.LINEN))
    sc.check(C.frame.IsShown(C.frame) and ns.ChartIsDocked() and ns.ChartItem() == R.LINEN,
             "%s: /fah chart Linen Cloth at the AH shows it docked" % label)
    sb.SearchBox.SetText(sb.SearchBox, N(R.LOTUS))
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), R.LOTUS, "%s: /fah chart <item> at the AH follows searches afterwards" % label)

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
    w.slash("/fah chart " + N(R.RUNE))
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
    out = "\n".join(w.slash("/fah"))
    sc.check("may not match the auction house you use" in out, "Horde: status warns about the Alliance prices")
    w.slash("/fah chart " + N(R.RUNE))
    chart_check(sc, w, ref, R.RUNE, "1D", "Horde chart")
    w.M.hardcore = True
    out = "\n".join(w.slash("/fah"))
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
    out = "\n".join(w.slash("/fah chart reset"))
    sc.check("normal place" in out and ns.settings.chartDock is None, "drag: /fah chart reset clears it")
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
    "none": "No good flips right now. Prices are steady; check back after the next update.",
    "gold": "Not enough gold on this character for the current flip ideas.",
}
FLIP_SLASH = {
    "history": "Collecting price history. Flip ideas show up after a few hourly price updates.",
    "nosales": "still collecting sales history: flip ideas show up after a few hourly price updates.",
    "none": "no good flips right now.",
    "gold": "not enough gold on this character for the current flip ideas",
}
FLIP_SLASH_HEAD = "flip ideas (profits are after the 5% auction cut):"
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


DEAL_EVERY = 5              # Sidebar.lua: one place in every 5 flip ideas is kept for a deal


def keep_deal_places(ranked, limit):
    """The best `limit` of the ranked collector flips, with one place in every
    DEAL_EVERY kept for deals: when fewer deals than that are among the best
    `limit`, the best deals after them replace the lowest-ranked other flips,
    and the list stays in rank order."""
    want = min(sum(1 for x in ranked if x["kind"] == "deal"), limit // DEAL_EVERY)
    top = ranked[:limit]
    short = want - sum(1 for x in top if x["kind"] == "deal")
    if short <= 0:
        return top
    extra = [x for x in ranked[limit:] if x["kind"] == "deal"][:short]
    drop = set([i for i, x in enumerate(top) if x["kind"] != "deal"][-short:])
    kept = [x for i, x in enumerate(top) if i not in drop] + extra
    rank = dict((id(x), i) for i, x in enumerate(ranked))
    return sorted(kept, key=lambda x: rank[id(x)])


def ref_flips_why(ref, limit=5, hidden=(), gold=MOCK_GOLD):
    """Python model of ns.FlipIdeas: the collector's flips from the data file
    first (best first, hidden ones left out), then the history-based ideas,
    each item once. Returns (ideas, why) where why says why there are none.
    With gold (copper; None = the game does not say: no sizing) each idea gets
    a fit, what the gold cannot buy is left out, and the collector's flips rank
    by the profit on what the gold buys (ties keep the collector's order).
    One place in every 5 is kept for deals (see keep_deal_places). A flip's
    "doubt" is True when the addon added "unsure" itself (today's listings)."""
    if not ref.items or not ref.updated:
        return [], "nodata"
    out, seen, poor = [], set(), False
    for f in ref.flips:
        if f["id"] in hidden or f["id"] in seen:
            continue
        fit = fit_flip(f, gold) if gold is not None else None
        if gold is not None and fit is None:
            poor = True
            continue
        seen.add(f["id"])
        x = dict(f, data=True, buy=f["buyMax"], risk=list(f["risk"]), fit=fit, doubt=False)
        if f["kind"] in ("gap", "deal") and "unsure" not in x["risk"] and price_doubt(ref, f["id"]):
            x["risk"].append("unsure")
            x["doubt"] = True
        out.append(x)
    if gold is not None:
        out = sorted(out, key=lambda x: -x["fit"]["profit"])   # a stable sort: ties keep the order
    out = keep_deal_places(out, limit)
    if len(out) >= limit:
        return out, None
    hist, why = ref_history_flips_why(ref, limit + len(out), hidden, gold)
    for x in hist:
        if len(out) >= limit:
            break
        if x["id"] in seen:
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
    each group by a day's profit. "thin": sells under 60 a day."""
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


def risk_short(risk):
    """The small grey risk note on a collector flip row."""
    if not risk:
        return ""
    return risk[0] if len(risk) == 1 else "%d risks" % len(risk)


def flip_units_text(x):
    """How many a collector flip row buys: "13", or "2 of 13" when the gold covers only some."""
    fit = x.get("fit")
    if fit and fit["limited"]:
        return "%s of %s" % (count_text(fit["units"]), count_text(x["units"]))
    return count_text(x["units"])


def flip_note(x):
    """The small grey note on a collector flip row: the risks, else for a deal how far under its usual price."""
    note = risk_short(x["risk"])
    if note == "" and x["kind"] == "deal":
        u = deal_under(x)
        if u:
            note = "-%d%%" % u
    return note


def flip_profit(x):
    """The profit a flip row shows: on what the gold buys, or (no sizing) the flip's own / the profit each."""
    return x["fit"]["profit"] if x.get("fit") else x["profit"]


def flip_line_text(x, name):
    """Sidebar.lua FlipLine: one /fah flips chat line (without the colour codes)."""
    fit = x.get("fit")
    if x.get("data"):
        risk = (" (risk: " + ", ".join(x["risk"]) + ")") if x["risk"] else ""
        if x["kind"] == "vendor":
            what = "sell to vendor"
        else:
            what = "relist " + money_text(x["relist"])
            if x["kind"] == "deal" and x.get("usual", 0) > 0:
                what += " (usual %s)" % money_text(x["usual"])
        return "%s: buy up to %s (%s), %s, +%s%s" % (name, money_text(x["buyMax"]), flip_units_text(x), what,
                                                    money_text(flip_profit(x)), risk)
    if fit:
        return "%s: buy %s at %s, sell %s, +%s (+%s each), ~%s/day" % (
            name, count_text(fit["units"]), money_text(x["buy"]), money_text(x["sell"]), money_text(fit["profit"]),
            money_text(x["profit"]), count_text(x["sold"]))
    return "%s: buy %s, sell %s, +%s each, ~%s/day" % (name, money_text(x["buy"]), money_text(x["sell"]),
                                                      money_text(x["profit"]), count_text(x["sold"]))


def gold_line(x):
    """Sidebar.lua GoldLine as a flattened tooltip line ("left | right"), or None (not sized)."""
    fit = x.get("fit")
    if not fit:
        return None
    if fit["limited"]:
        s = "With your gold (%s): buy %s of %s, costing %s, making +%s" % (
            money_text(fit["gold"]), count_text(fit["units"]), count_text(fit["whole"]), money_text(fit["cost"]),
            money_text(fit["profit"]))
    else:
        s = "Your gold covers all of it."
    return s + " | "


def slash_flips_ok(sc, out, ideas, ref, items, gold, label):
    """/fah flips: the header (sized to your gold when the game says), then one exact line per idea."""
    head = (("flip ideas sized to your gold (%s), profits after the 5%% auction cut:" % money_text(gold))
            if gold is not None else FLIP_SLASH_HEAD)
    sc.eq(len(out), len(ideas) + 1, "%s: /fah flips prints a header and a line per idea" % label)
    if out:
        sc.check(strip_codes(out[0]).endswith(head), "%s: /fah flips header (%s)" % (label, strip_codes(out[0])))
    for i, (line, x) in enumerate(zip(out[1:], ideas), start=1):
        want = "  %d. %s" % (i, flip_line_text(x, item_name(ref, items, x["id"])))
        sc.check(strip_codes(line).endswith(want), "%s: /fah flips line %d: got %r, want %r" % (label, i, strip_codes(line), want))


def flip_row_ok(sc, r, x, ref, items, lab):
    """One flip row shows the idea: name, profit (on what your gold buys), what to buy and what to do with it."""
    sc.eq(r.itemID, x["id"], lab + ": item")
    sc.eq(strip_codes(r.right._text), "+" + short_money(flip_profit(x)), lab + ": profit")
    sc.eq(r.name._text, item_name(ref, items, x["id"]), lab + ": name")
    if x.get("data"):
        sc.eq(strip_codes(r.sub._text), "Buy up to %s (%s)" % (short_money(x["buyMax"]), flip_units_text(x)),
              lab + ": buy up to (units)")
        sc.eq(strip_codes(r.sub2._text), "Sell to vendor" if x["kind"] == "vendor" else "Relist " + short_money(x["relist"]),
              lab + ": relist or vendor")
        sc.eq(r.right2._text, flip_note(x), lab + ": risk note")
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

    # Hover: the game's item tooltip with the Forever AH lines, plus the flip numbers
    tt = w.g.GameTooltip
    if got:
        M.enter(got[0])
        lines = w.all_lines(tt)
        flat = [strip_codes(a or "") + " | " + strip_codes(b or "") for a, b in lines]
        sc.check(any("Forever AH" in f for f in flat), "flip hover: Forever AH price lines are there")
        if want[0].get("data"):
            pl = "Profit over the cost" if want[0]["kind"] == "vendor" else "Profit, after the 5% cut"
        else:
            pl = "Profit each, after the 5% cut"
        sc.check(any(f.startswith(pl) and ("+" + ns.MoneyText(want[0]["profit"])) in f
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
    out = w.slash("/fah watch " + rune)
    sc.check(any(("watching " + rune) in c or "already on your watchlist" in c for c in out), "/fah watch Runecloth")
    sc.eq(watch_ids(w).count(R.RUNE), 1, "/fah watch: on the list once")
    out = w.slash("/fah unwatch " + rune.lower())
    sc.check(any(("stopped watching " + rune) in c for c in out), "/fah unwatch")
    sc.check(R.RUNE not in watch_ids(w), "/fah unwatch removed it")
    out = w.slash("/fah unwatch " + rune)
    sc.check(any("not on your watchlist" in c for c in out), "/fah unwatch twice")
    out = w.slash("/fah watch Nope Nothing")
    sc.check(any("no price data for Nope Nothing" in c for c in out), "/fah watch unknown")
    out = w.slash("/fah watchlist")
    sc.check(any(("watching %d items:" % len(watch_ids(w))) in c for c in out), "/fah watchlist lists it")
    out = w.slash("/fah watchlist reset")
    sc.eq(watch_ids(w), SIDEBAR_WATCH, "/fah watchlist reset brings back the starter list")
    out = w.slash("/fah flips")
    ideas_now = ref_flips(ref, limit=10, hidden={fid} if got else (), gold=w.gold)
    sc.eq(len(out), len(ideas_now) + 1 if ideas_now else 1, "/fah flips prints a line per idea (up to 10)")
    if ideas_now:
        slash_flips_ok(sc, out, ideas_now, ref, items, w.gold, "sidebar")
    out = w.slash("/fah help")
    sc.check(any("/fah watch" in c for c in out) and any("/fah chart" in c for c in out), "/fah help has both lists")
    out = w.slash("/fah sidebar")
    sc.check(any("now off" in c for c in out) and not F.IsVisible(F), "/fah sidebar tucks it away")
    out = w.slash("/fah sidebar")
    sc.check(any("now on" in c for c in out) and F.IsVisible(F), "/fah sidebar shows it")
    out = w.slash("/fah nonsense")
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
    out = w.slash("/fah flips")
    sc.check(any("no price data yet" in c for c in out), "no data: /fah flips says so")
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
    out = w.slash("/fah flips")
    sc.check(any(FLIP_SLASH.get(why, "?") in c for c in out), "first snapshot: /fah flips says so (%s)" % out)
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
            w.slash("/fah compact")
        for iid in sorted(ref.items):
            tt.SetItemByID(tt, iid)
            sc.eq(w.lines(tt), expected_tooltip(ref, iid, age, compact=compact),
                  "edge tooltip %d%s" % (iid, " compact" if compact else ""))
    w.slash("/fah compact")
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
        out = w.slash("/fah flips")
        sc.check(any(FLIP_SLASH[why] in c for c in out), "%s: /fah flips says the same (%s)" % (label, out))
    else:
        sc.check(not SB.flipMsg._shown, "%s: ideas shown, no message" % label)
    return want, why


def sc_sales_hours(sc, ref, items):
    """salesHours: 0 = still measuring (no sold number, no Thin market), 1 to 23 =
    an early estimate scaled to a day, 24 = a full day. Flip ideas wait for 3 hours."""
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
        w.slash("/fah chart " + N(R.LINEN))
        chart_check(sc, w, r2, R.LINEN, "1D", label + " chart")
        w.open_ah_modern()
        want, why = check_sidebar_flips(sc, w, r2, label)
        hist, hwhy = ref_history_flips_why(r2)
        if hours < 3:
            sc.eq((len(hist), hwhy), (0, "history"), "%s: no history ideas from under 3 hours of sales" % label)
            sc.check(all(x.get("data") for x in want) and (len(want) > 0) == (len(r2.flips) > 0),
                     "%s: the collector's flips still show (they need no sales history)" % label)
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
    price, sales are still being measured, and the sidebar says so."""
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
    if r2.flips:
        sc.check(len(want_f) > 0 and all(x.get("data") for x in want_f),
                 "one snapshot: the collector's flips show from the first snapshot")
    else:
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
    w.slash("/fah chart " + N(R.RUNE))
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
    out = w.slash("/fah chart " + x_name)
    sc.eq(ns.ChartItem(), x, "names: /fah chart with the game's name")
    sc.eq(ns.ChartPanel.name._text, x_name if len(x_name) <= 34 else x_name[:33] + "...", "names: chart header uses the game's name")
    chart_check(sc, w, r2, x, "1D", "names: unnamed item chart")
    ns.ShowChart(y, "slash")
    sc.eq(ns.ChartPanel.name._text, "Item %d" % y, "names: header for an item nobody has a name for")
    # typing that shown name back works like typing the number
    ns.ShowChart(R.LINEN, "slash")
    out = [strip_codes(o) for o in w.slash("/fah chart Item %d" % y)]
    sc.check(ns.ChartItem() == y and not any("no price data" in o for o in out), "names: /fah chart Item <id> (%s)" % out)
    out = [strip_codes(o) for o in w.slash("/fah watch [item %d]" % y)]
    sc.check(y in watch_ids(w) and any("watching Item %d" % y in o for o in out), "names: /fah watch [item <id>] (%s)" % out)
    w.slash("/fah unwatch Item %d" % y)
    sc.check(y not in watch_ids(w), "names: /fah unwatch Item <id>")
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
    w.slash("/fah chart " + N(R.RUNE))
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
    w2.slash("/fah chart " + N(R.RUNE))
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
    w5.slash("/fah chart " + N(R.RUNE))
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
    w.slash("/fah chart " + gear_link(base, keys[0]))
    sc.eq(ns.ChartItem(), v1, "versions: /fah chart with a version's link")
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
    # rows that do not carry the version's name: none is opened, the player picks
    split_rows([(1017, "Odd Name One"), (1034, "Odd Name Two")])
    ns.SearchAH(v1)
    w.settle(0.2)
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.5)
    sc.eq(len(w.M.openedRows), 0, "versions: split rows with no matching name: none opened for the player")
    sc.eq(ns.ChartItem(), v1, "versions: and the chart stays on the version that was searched")
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


def synth_flips(ref, items):
    """Flip lines like the collector writes ("id|kind|buyMax|units|cost|relist|profit|roi|risk"),
    made from the data's prices, plus a broken one and a repeat that must be skipped."""
    out = []
    it = ref.items[R.RUNE]
    buy = max(2, it["cheapest"])
    rel = max(buy + 10, it["market"])
    units = 7
    cost = buy * units
    profit = int(rel * units * 0.95) - cost
    out.append("%d|gap|%d|%d|%d|%d|%d|%d|thin,slow" % (R.RUNE, buy, units, cost, rel, max(1, profit), max(1, profit) * 1000 // cost))
    lin = ref.items[R.LINEN]
    vend = max(1, lin["vendor"])
    out.append("%d|vendor|%d|40|%d|0|%d|%d|" % (R.LINEN, max(1, vend - 3), max(1, vend - 3) * 40, 3 * 40, 3 * 40 * 1000 // (max(1, vend - 3) * 40)))
    out.append("%d|gap|%d|1|%d|%d|%d|%d|spike" % (R.LOTUS, ref.items[R.LOTUS]["cheapest"], ref.items[R.LOTUS]["cheapest"],
                                                   ref.items[R.LOTUS]["cheapest"] * 2, ref.items[R.LOTUS]["cheapest"], 1000))
    out.append("%d|bogus|1|1|1|1|1|1|" % R.ARCANE)         # unknown kind: skipped
    out.append("garbage")                                  # not a flip: skipped
    out.append("%d|gap|5|1|5|0|3|600|" % R.ARCANE)         # a gap flip with no relist price: skipped
    out.append("%d|gap|%d|2|%d|%d|50|10|" % (R.RUNE, buy, buy * 2, rel))   # the same item again: shown once
    return out


def flips_data(ref, items):
    """(text, ref) with the collector's flips: the data's own, else made up from its prices."""
    if ref.flips:
        return ref.text, ref
    text = set_table(ref.text, "flips", [lua_quote(s) for s in synth_flips(ref, items)])
    return text, RefData(text=text)


# What a collector flip's hover says for "unsure": the addon's own doubt (today's
# listings: only 1 or 2 of them, or a few units far above what a vendor pays), and
# a deal's flag from the collector (its usual price was mostly one listing).
UNSURE_TEXT = ("Unsure: only a few are listed, far above what a vendor pays (or only 1 or 2 listings), "
               "so the price may be one player's guess.")
DEAL_UNSURE_TEXT = ("Unsure: most of the time only one of these is listed, so its usual price may be one "
                    "seller's asking price.")


def deal_relist_words(x):
    """How a deal's hover says to relist it: near the usual price only when the
    relist price is at least 90% of it; else just under the next listing (the
    relist price is never above the usual price); at the price below when the
    usual price is not known."""
    usual = x.get("usual") or 0
    if usual <= 0:
        return "relist it at the price below."
    if x["relist"] * 10 >= usual * 9:
        return "relist it near the usual price."
    return "relist it just under the next listing, below the usual price."


def data_flip_tip(x):
    """Sidebar.lua DataFlipTooltip up to the risks, as flattened tooltip lines ("left | right")."""
    usual = x.get("usual") or 0
    out = ["Flip idea from the latest prices | "]
    if x["kind"] == "vendor":
        out.append("These listings cost less than a vendor pays for them: buy them and sell them to a vendor. | ")
    elif x["kind"] == "deal":
        out.append("Listed well under its usual price%s: buy it and %s | "
                   % ((" (usual %s)" % money_text(usual)) if usual > 0 else "", deal_relist_words(x)))
    else:
        out.append("Buy the cheap listings, then relist them just under the next listing. | ")
    out.append("Buy up to (each) | " + money_text(x["buyMax"]))
    out.append("That buys %s, costing | %s" % (count_text(x["units"]), money_text(x["cost"])))
    if x["kind"] == "vendor":
        out += ["Then | sell to a vendor", "Profit over the cost | +" + money_text(x["profit"])]
    else:
        out += ["Relist at (each) | " + money_text(x["relist"]), "Profit, after the 5% cut | +" + money_text(x["profit"])]
    if x["kind"] == "deal" and usual > 0:
        out.append("Usual price (last 7 days) | " + money_text(usual))
        if deal_under(x):
            out.append("Under the usual price by | %d%% or more" % deal_under(x))
    if x["roi"] > 0:
        out.append("Return on what you spend | %.1f%%" % (x["roi"] / 10.0))
    if gold_line(x):
        out.append(gold_line(x))
    if x["risk"]:
        out += ["  | ", "Watch out | "]
    return out


def data_flip_hover_ok(sc, w, row, x, ref, lab):
    """Hovering a collector flip row: the game's item tooltip, then exactly the
    flip's lines (deal, usual price, what your gold buys), each risk explained."""
    tt = w.g.GameTooltip
    w.M.enter(row)
    flat = [strip_codes(a or "") + " | " + strip_codes(b or "") for a, b in w.all_lines(tt)]
    want = data_flip_tip(x)
    i = flat.index(want[0]) if want[0] in flat else -1
    sc.check(i >= 0, lab + ": title")
    sc.eq(flat[i:i + len(want)] if i >= 0 else flat, want, lab + ": the flip's lines")
    risk_lines = flat[i + len(want):i + len(want) + len(x["risk"])] if i >= 0 else []
    for k, flag in enumerate(x["risk"]):
        got = risk_lines[k] if k < len(risk_lines) else ""
        sc.check(got.lower().startswith(("versions" if flag == "suffix" else flag) + ":"),
                 lab + ": explains the %s risk (%s)" % (flag, got))
        if flag == "unsure":
            # a deal's own flag (from the collector) is about its usual price; the
            # addon's (x["doubt"]) is about today's listings, for every kind
            own = x["kind"] == "deal" and not x.get("doubt")
            sc.eq(got, (DEAL_UNSURE_TEXT if own else UNSURE_TEXT) + " | ",
                  lab + ": unsure, in the words for %s" % ("the collector's flag" if own else "the addon's own doubt"))
    if x["id"] in ref.base_of:
        sc.check(any(f.startswith("Prices for this version only") for f in flat), lab + ": the item tooltip is the version's")
    sc.check(any("Forever AH" in f for f in flat[:max(0, i)]), lab + ": the Forever AH price lines come first")
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
    """The collector's flips show first in the sidebar (they work from one
    snapshot), each row: icon, name, Buy up to X (N), Relist Y or Sell to
    vendor, +profit and a small risk note explained on hover."""
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
    out = w.slash("/fah flips")
    ideas = ref_flips(r2, limit=10, hidden={fid}, gold=w.gold)
    sc.eq(len(out), len(ideas) + 1, "collector flips: /fah flips prints them")
    slash_flips_ok(sc, out, ideas, r2, items, w.gold, "collector flips")
    for line, x in zip(out[1:], ideas):
        s = strip_codes(line)
        if x.get("data"):
            sc.check((("risk: " + ", ".join(x["risk"])) in s) == bool(x["risk"]), "collector flips: chat line risks (%s)" % s)
    w.close_ah_modern()
    sc.clean(w)
    # a collector flip on a price that may be one player's guess (a few units far above what a
    # vendor pays): the addon gives no Sell now or Best price for it, so the flip warns too
    lot = ref.items[R.LOTUS]
    if lot["market"] > 0 and lot["cheapest"] > 0:
        vend = max(1, lot["market"] // 300)
        doubt = rewrite_items(ref.text, lambda iid, f: (f[:3] + ["4"] + f[4:6] + [str(vend)] + f[7:9] + ["4"])
                              if iid == R.LOTUS else None)
        buy = max(100, lot["cheapest"], lot["market"] // 2)
        rel = buy * 2
        doubt = set_table(doubt, "flips", [lua_quote("%d|gap|%d|1|%d|%d|%d|%d|" % (R.LOTUS, buy, buy, rel, rel * 95 // 100 - buy,
                                                                                (rel * 95 // 100 - buy) * 1000 // buy))])
        r4 = RefData(text=doubt)
        sc.check(price_doubt(r4, R.LOTUS), "collector flips: test data with a doubtful price made")
        # enough gold for it (a Black Lotus at half its market price is more than the mock's 123g)
        w4 = World(r4, items, data_src=doubt, gold=RICH_GOLD)
        w4.open_ah_modern()
        want4, _ = check_sidebar_flips(sc, w4, r4, "collector flips, doubtful price")
        sc.check(bool(want4) and want4[0]["id"] == R.LOTUS and want4[0]["risk"] == ["unsure"],
                 "collector flips: the doubtful flip says unsure (%s)" % (want4[:1],))
        rows4 = shown_rows(w4.ns.SidebarPanel, "flipRows")
        if rows4:
            w4.M.enter(rows4[0])
            flat = [strip_codes(a or "") for a, _ in w4.all_lines(w4.g.GameTooltip)]
            sc.check("Watch out" in flat and any(f.startswith("Unsure:") for f in flat),
                     "collector flips: the hover explains it (%s)" % flat[-8:])
            w4.M.leave(rows4[0])
        w4.close_ah_modern()
        sc.clean(w4, "doubtful flip")
    # they work from a single snapshot (no history, no sales yet)
    one = one_snapshot_text(r2)
    r3 = RefData(text=one)
    w2 = World(r3, items, data_src=one)
    w2.open_ah_modern()
    want3, _ = check_sidebar_flips(sc, w2, r3, "collector flips, one snapshot")
    sc.check(len(want3) > 0 and all(x.get("data") for x in want3), "collector flips: shown from the very first snapshot")
    w2.close_ah_modern()
    sc.clean(w2, "one snapshot")


# ---------------------------------------------------------------------------
# Flip ideas sized to the gold on the character, and deals (items listed well
# under their usual price). Hand-made flip lines on items from the data under
# test, written the way the collector writes them (best first by profit).
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
    text = set_table(ref.text, "flips", [lua_quote(s) for s in lines])
    return text, RefData(text=text), picks


def sized_order_ok(sc, ideas, gold, label):
    """The contract's ranking (item 4), checked on a list of ideas without the
    model's own sorting: the collector's flips before the price-history ideas;
    with gold, each part best profit on what the gold buys first (the deals kept
    a place come last of the collector's flips, so this holds for them too);
    without gold, the history ideas selling 60+ a day first, each group by a
    day's profit."""
    kinds = [bool(x.get("data")) for x in ideas]
    sc.check(kinds == sorted(kinds, reverse=True), "%s: the collector's flips come before the history ideas" % label)
    data = [x for x in ideas if x.get("data")]
    hist = [x for x in ideas if not x.get("data")]
    if gold is not None:
        for part, what in ((data, "collector flips"), (hist, "history ideas")):
            p = [x["fit"]["profit"] for x in part]
            sc.check(p == sorted(p, reverse=True), "%s: %s, best profit on your gold first (%s)" % (label, what, p))
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
    "n of m", and the ideas rank by the profit on what you can buy; with none,
    the sidebar says so. A gold change (PLAYER_MONEY) works them out again."""
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
    slash_flips_ok(sc, w.slash("/fah flips"), ref_flips(r2, limit=10, gold=w.gold), r2, items, w.gold, "gold rich")
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
        sc.check("With your gold (2g): buy 2 of 20, costing 2g, making +66s" in flat, "gold 2g: the hover says what 2g buys (%s)" % flat[-12:])
        w.M.leave(row)
    hist = [x for x in want if not x.get("data")]
    sc.check(all(x["fit"]["cost"] <= 20000 for x in want), "gold 2g: every idea costs at most 2g")
    if R.SIM:
        sc.check(any(x["fit"]["limited"] for x in hist), "gold 2g: a history idea 2g buys only part of (%s)" % hist)
    out = w.slash("/fah flips")
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
    # no gold at all: the sidebar and /fah flips say so
    w.set_gold(0)
    w.M.fire("PLAYER_MONEY")
    w.settle(0.5)
    want0, why0 = check_sidebar_flips(sc, w, r2, "gold 0")
    sc.eq((len(want0), why0), (0, "gold"), "gold 0: no ideas, because of the gold")
    sc.eq(SB.flipGold._text, "for your 0c", "gold 0: FLIP IDEAS says the gold")
    out = [strip_codes(o) for o in w.slash("/fah flips")]
    sc.eq(out, ["Forever AH: not enough gold on this character for the current flip ideas (you have 0c)."], "gold 0: /fah flips says so")
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
    usual price) next to a gap flip with a usual price and an older 9-field one.
    Flips A (shown in the sidebar, best first): a deal the mock's 123g buys 2 of 3 of
    (thin), a deal on a version (suffix and slow), a deal the collector calls unsure
    on an item with only 2 listings (one unsure, not two), a deal on an item with 1
    listing (the addon adds unsure), a deal with no risk (35% under its usual price).
    Flips B (another world, on the items with no price doubt): a deal with no usual
    price, one with a broken (negative) usual, a gap flip with a usual price, an older
    9-field gap flip, and broken lines (skipped)."""
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
            f[9] = "2"           # 2 listings: the addon doubts the price too
        elif iid == ids["doubt"]:
            f[9] = "1"           # 1 listing
        elif iid in (ids["big"], ids["clean"], ids["gap10"], ids["gap9"]):
            if f[9] in ("1", "2"):
                f[9] = "5"       # no doubt about these prices
            f[3] = str(max(int(f[3] or 0), 10))
        return f
    base = rewrite_items(ref.text, fn)
    flips_a = [
        "%d|deal|520000|3|1500000|900000|1065000|710|thin|950000" % ids["big"],
        "%d|deal|20000|2|38000|33000|24700|650|suffix,slow|33000" % ids["ver"],
        "%d|deal|14000|1|14000|21000|5950|425|unsure|21000" % ids["both"],
        "%d|deal|7000|1|7000|12000|4400|628||12700" % ids["doubt"],    # 44.9% under: says 44%
        "%d|deal|6500|1|6500|9999|2999|461||10000" % ids["clean"],
    ]
    flips_b = [
        "%d|gap|900|10|8500|1400|4800|564||1500" % ids["gap10"],
        "%d|deal|5000|1|5000|8758|3320|664||-5" % ids["negusual"],    # the same profit as the next one:
        "%d|deal|3000|2|5800|4800|3320|572||" % ids["nousual"],       # sized, they keep this order
        "%d|gap|600|5|2800|900|1475|526|slow" % ids["gap9"],
        "%d|deal|5000|1|5000|0|100|20||8000" % ids["broken"],      # no relist price: skipped
        "%d|deal|0|1|0|9000|100|20||8000" % ids["broken"],         # no buy price: skipped
        "%d|deal|5000|1|5000|9000|0|0||8000" % ids["broken"],      # no profit: skipped
        "%d|sniper|5000|1|5000|9000|100|20||8000" % ids["broken"],  # a kind this addon does not know: skipped
    ]
    out = []
    for lines in (flips_a, flips_b):
        text = set_table(base, "flips", [lua_quote(s) for s in lines])
        out.append((text, RefData(text=text)))
    return out, ids


def sc_flip_deals(sc, ref, items):
    """Deals: green, blue or epic items the collector found listed well under their
    usual price. Rows: Buy up to X (n), Relist R, the risk note or how far under the
    usual price; the hover explains it with the usual price; /fah flips too."""
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
    sc.eq([x["id"] for x in want], [ids["big"], ids["ver"], ids["both"], ids["doubt"], ids["clean"]], "deals: all five shown, best first")
    by = {x["id"]: x for x in want}
    rows = {r.itemID: r for r in shown_rows(SB, "flipRows")}
    sc.eq(by[ids["both"]]["risk"], ["unsure"], "deals: unsure from the collector and the addon is said once")
    sc.eq(by[ids["doubt"]]["risk"], ["unsure"], "deals: the addon's own price doubt applies to deals")
    for key, sub, sub2, note, right in (
            ("big", "Buy up to 52g (2 of 3)", "Relist 90g", "thin", "+71g"),
            ("ver", "Buy up to 2g (2)", "Relist 3g 30s", "3 risks" if price_doubt(ra, ids["ver"]) else "2 risks", "+2g 47s"),
            ("both", "Buy up to 1g 40s (1)", "Relist 2g 10s", "unsure", "+59s 50c"),
            ("doubt", "Buy up to 70s (1)", "Relist 1g 20s", "unsure", "+44s"),
            ("clean", "Buy up to 65s (1)", "Relist 99s 99c", "-35%", "+29s 99c")):
        r = rows.get(ids[key])
        if sc.check(r is not None, "deals: a row for the %s deal" % key):
            sc.eq((strip_codes(r.sub._text), strip_codes(r.sub2._text), r.right2._text, strip_codes(r.right._text)),
                  (sub, sub2, note, right), "deals: the %s deal's row" % key)
    for x in want:
        data_flip_hover_ok(sc, w, rows[x["id"]], x, ra, "deals: hover %d" % x["id"])
    M.enter(rows[ids["clean"]])
    flat = [strip_codes(l or "") + " | " + strip_codes(r_ or "") for l, r_ in w.all_lines(w.g.GameTooltip)]
    sc.check("Listed well under its usual price (usual 1g): buy it and relist it near the usual price. | " in flat
             and "Usual price (last 7 days) | 1g" in flat and "Under the usual price by | 35% or more" in flat
             and "Your gold covers all of it. | " in flat, "deals: the clean deal's hover, by hand (%s)" % flat[-14:])
    M.leave(rows[ids["clean"]])
    M.enter(rows[ids["big"]])
    flat = [strip_codes(l or "") for l, _ in w.all_lines(w.g.GameTooltip)]
    sc.check("With your gold (123g 45s): buy 2 of 3, costing 100g, making +71g" in flat,
             "deals: the big deal's hover says what 123g buys (%s)" % flat[-12:])
    M.leave(rows[ids["big"]])
    out = w.slash("/fah flips")
    ideas = ref_flips(ra, limit=10, gold=w.gold)
    slash_flips_ok(sc, out, ideas, ra, items, w.gold, "deals")
    s = [strip_codes(o) for o in out]
    nm = item_name(ra, items, ids["clean"])
    sc.check(any(o.endswith(". %s: buy up to 65s (1), relist 99s 99c (usual 1g), +29s 99c" % nm) for o in s),
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
    rows = {r.itemID: r for r in shown_rows(w.ns.SidebarPanel, "flipRows")}
    for key in ("nousual", "negusual"):
        r = rows.get(ids[key])
        if sc.check(r is not None, "deals B: a row for the %s deal" % key):
            sc.eq(r.right2._text, "", "deals B: no usual price, no note (%s)" % key)
            flat = data_flip_hover_ok(sc, w, r, next(x for x in want if x["id"] == ids[key]), rb, "deals B: hover %s" % key)
            sc.check("Listed well under its usual price: buy it and relist it at the price below. | " in flat
                     and not any(f.startswith("Usual price") for f in flat), "deals B: the hover without a usual price (%s)" % key)
    r = rows.get(ids["gap10"])
    if r is not None:
        flat = data_flip_hover_ok(sc, w, r, next(x for x in want if x["id"] == ids["gap10"]), rb, "deals B: hover gap")
        sc.check(not any(f.startswith(("Usual price", "Listed well under")) for f in flat), "deals B: a gap flip says nothing about a usual price")
    slash_flips_ok(sc, w.slash("/fah flips"), ref_flips(rb, limit=10, gold=w.gold), rb, items, w.gold, "deals B")
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
    listed), so the addon adds no "unsure" of its own to their flips."""
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
    written best first like the collector writes them, on items with no price doubt.
    deal_profit: the best deal's profit (50000 puts it among the gap flips)."""
    picks = flip_test_items(ref, items, 12)
    if len(picks) < 12:
        return None
    gaps, deals = picks[:9], picks[9:]
    lines = []
    for k, iid in enumerate(gaps):
        cost = 10000 + 500 * k
        relist, profit = relist_for(60000 - 4000 * k, cost)
        lines.append((profit, "%d|gap|%d|1|%d|%d|%d|%d||" % (iid, cost, cost, relist, profit, profit * 1000 // cost)))
    for iid, cost, want in zip(deals, (15000, 12000, 9000), (deal_profit, 8000, 7000)):
        relist, profit = relist_for(want, cost)
        lines.append((profit, "%d|deal|%d|1|%d|%d|%d|%d||%d" % (iid, cost, cost, relist, profit, profit * 1000 // cost,
                                                              max(30000, relist))))
    lines.sort(key=lambda p: -p[0])
    text = set_table(no_doubt(ref.text, picks), "flips", [lua_quote(s) for _, s in lines])
    return text, RefData(text=text), gaps, deals


def sc_flip_deal_room(sc, ref, items):
    """One place in every 5 flip ideas is kept for a deal (a green, blue or epic item
    listed well under its usual price), so a deal shows on every scan: when bigger
    flips fill the list, the best deal the gold buys takes the sidebar's last row and
    the best two the last of the 10 /fah flips lines, in rank order. A hidden deal,
    or one the gold cannot buy, gives its place to the next one; a deal already
    among the best keeps its own place."""
    made = deal_room_data(ref, items)
    if not sc.check(made is not None, "deal room: enough items to test with"):
        return
    text, r2, g, d = made
    sc.eq([(f["id"], f["kind"]) for f in r2.flips], [(i, "gap") for i in g] + [(i, "deal") for i in d],
          "deal room: test data made (the gap flips all make more than the deals)")
    sc.check(not any(price_doubt(r2, i) for i in g + d), "deal room: no price doubt on the test items")
    for gold in (MOCK_GOLD, None):
        lab = "deal room, %s" % ("123g" if gold else "no gold")
        w = World(r2, items, data_src=text, gold=gold)
        M = w.M
        w.open_ah_modern()
        SB = w.ns.SidebarPanel
        want, _ = sized_rows_ok(sc, w, r2, lab)
        sc.eq([x["id"] for x in want], g[:4] + d[:1], lab + ": the 4 best flips, then the best deal in the last row")
        rows = shown_rows(SB, "flipRows")
        if not sc.check(len(rows) == 5, lab + ": 5 rows"):
            w.close_ah_modern()
            continue
        sc.eq((rows[4].itemID, strip_codes(rows[4].sub._text), strip_codes(rows[4].sub2._text), rows[4].right2._text,
               strip_codes(rows[4].right._text)), (d[0], "Buy up to 1g 50s (1)", "Relist 2g 52s", "-50%", "+90s"),
              lab + ": the deal's row, by hand")
        M.enter(rows[4])
        flat = [strip_codes(a or "") + " | " + strip_codes(b or "") for a, b in w.all_lines(w.g.GameTooltip)]
        sc.check("Listed well under its usual price (usual 3g): buy it and relist it just under the next listing, "
                 "below the usual price. | " in flat, lab + ": the deal's hover says where to relist it (%s)" % flat[-12:])
        M.leave(rows[4])
        ideas = ref_flips(r2, limit=10, gold=gold)
        sc.eq([x["id"] for x in ideas], g[:8] + d[:2], lab + ": /fah flips keeps 2 of its 10 lines for the best deals")
        slash_flips_ok(sc, w.slash("/fah flips"), ideas, r2, items, gold, lab)
        # not interested in the best deal: the next one takes its place
        install_menu_mock(w)
        M.click(rows[4], "RightButton")
        menu_pick(w, "Not interested (hide for a day)")
        hid = ref_flips(r2, hidden={d[0]}, gold=gold)
        sc.eq([x["id"] for x in hid], g[:4] + d[1:2], lab + ", best deal hidden: the next deal takes the last row")
        sc.eq([r.itemID for r in shown_rows(SB, "flipRows")], [x["id"] for x in hid], lab + ", best deal hidden: the rows")
        ideas = ref_flips(r2, limit=10, hidden={d[0]}, gold=gold)
        sc.eq([x["id"] for x in ideas], g[:8] + d[1:3], lab + ", best deal hidden: /fah flips, the next two deals")
        slash_flips_ok(sc, w.slash("/fah flips"), ideas, r2, items, gold, lab + ", best deal hidden")
        w.close_ah_modern()
        sc.clean(w, lab)
    # 1g 30s: the best deal (1g 50s) and the two dearest gap flips are out of reach
    w = World(r2, items, data_src=text, gold=13000)
    w.open_ah_modern()
    want, _ = sized_rows_ok(sc, w, r2, "deal room, 1g 30s")
    sc.eq([x["id"] for x in want], g[:4] + d[1:2], "deal room, 1g 30s: the best deal 1g 30s buys takes the last row")
    ideas = ref_flips(r2, limit=10, gold=13000)
    sc.eq([x["id"] for x in ideas if x.get("data")], g[:7] + d[1:],
          "deal room, 1g 30s: /fah flips: every flip 1g 30s buys fits, each in its place")
    slash_flips_ok(sc, w.slash("/fah flips"), ideas, r2, items, 13000, "deal room, 1g 30s")
    w.close_ah_modern()
    sc.clean(w, "deal room, 1g 30s")
    # a deal that makes more than most of the gap flips keeps its own place
    text_b, rb, g, d = deal_room_data(ref, items, deal_profit=50000)
    w = World(rb, items, data_src=text_b)
    w.open_ah_modern()
    want, _ = sized_rows_ok(sc, w, rb, "deal room B")
    sc.eq([x["id"] for x in want], g[:3] + d[:1] + g[3:4], "deal room B: the deal in its own place, no other deal pulled in")
    ideas = ref_flips(rb, limit=10, gold=w.gold)
    sc.eq([x["id"] for x in ideas], g[:3] + d[:1] + g[3:8] + d[1:2], "deal room B: /fah flips: the next deal takes the last line")
    slash_flips_ok(sc, w.slash("/fah flips"), ideas, rb, items, w.gold, "deal room B")
    w.close_ah_modern()
    sc.clean(w, "deal room B")


def sc_flip_deal_words(sc, ref, items):
    """What a deal's hover says. Where to relist it: near the usual price only when
    the relist price is at least 90% of it, else just under the next listing (the
    collector caps it there). Unsure: the collector's flag is about the usual price
    (mostly one listing); the addon's own doubt (only 1 or 2 listings today, or a
    few units far above what a vendor pays) is said in its own words."""
    picks = [i for i in flip_test_items(ref, items, 60) if ref.items[i]["vendor"] > 0][:5]
    if not sc.check(len(picks) == 5, "deal words: enough items with a vendor price to test with"):
        return
    far, own, nxt, at90, under90 = picks
    text = no_doubt(ref.text, [nxt, at90, under90])

    def vendor_far(iid, f):
        # 5 separate listings of 5 units, the market 300 times what a vendor pays
        if iid in (far, own):
            f = f + [""] * (10 - len(f))
            f[2], f[3], f[9] = str(300 * int(f[6])), "5", "5"
            return f
        return None
    text = rewrite_items(text, vendor_far)
    usual = {far: 300 * ref.items[far]["vendor"], own: 300 * ref.items[own]["vendor"]}
    lines = [
        "%d|deal|%d|1|%d|%d|%d|%d||%d" % (far, usual[far] // 2, usual[far] // 2, usual[far] - 1,
                                         (usual[far] - 1) * 95 // 100 - usual[far] // 2, 900, usual[far]),
        "%d|deal|%d|1|%d|%d|%d|%d|unsure|%d" % (own, usual[own] // 2, usual[own] // 2, usual[own],
                                               usual[own] * 95 // 100 - usual[own] // 2, 900, usual[own]),
        # the collector's own deal: listings 3g, 3g 70s, 10g, 10g, 10g 50s with a usual price of 10g
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
    sc.eq([(by[i]["risk"], by[i]["doubt"]) for i in (far, own, nxt)], [(["unsure"], True), (["unsure"], False), (["slow"], False)],
          "deal words: the addon's own unsure, the collector's (said once), none")
    rows = {r.itemID: r for r in shown_rows(w.ns.SidebarPanel, "flipRows")}
    tips = {}
    for i in picks:
        w.M.enter(rows[i])
        tips[i] = [strip_codes(a or "") + " | " + strip_codes(b or "") for a, b in w.all_lines(w.g.GameTooltip)]
        w.M.leave(rows[i])
    sc.check(UNSURE_TEXT + " | " in tips[far] and not any("seller's asking price" in t for t in tips[far]),
             "deal words: 5 separate listings far above what a vendor pays: the addon's words, not 'mostly one listing'")
    sc.check(DEAL_UNSURE_TEXT + " | " in tips[own] and UNSURE_TEXT + " | " not in tips[own],
             "deal words: the collector's unsure is about the usual price")
    sc.check("Listed well under its usual price (usual 10g): buy it and relist it just under the next listing, below "
             "the usual price. | " in tips[nxt] and "Relist at (each) | 3g 69s 99c" in tips[nxt]
             and not any("near the usual price" in t for t in tips[nxt]),
             "deal words: relist 3g 69s 99c with a usual price of 10g: just under the next listing (%s)" % tips[nxt][-14:])
    sc.check("Listed well under its usual price (usual 10g): buy it and relist it near the usual price. | " in tips[at90],
             "deal words: relist at 90% of the usual price: near it")
    sc.check("Listed well under its usual price (usual 10g): buy it and relist it just under the next listing, below "
             "the usual price. | " in tips[under90], "deal words: relist 1c under 90% of the usual price: just under the next listing")
    slash_flips_ok(sc, w.slash("/fah flips"), ref_flips(r2, limit=10, gold=w.gold), r2, items, w.gold, "deal words")
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
    and hovers, no gold next to FLIP IDEAS, the old /fah flips header."""
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
        slash_flips_ok(sc, w.slash("/fah flips"), ref_flips(rr, limit=10, gold=None), rr, items, None, "no gold " + label)
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
    out = "\n".join(w.slash("/fah"))
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


def sc_stack_verdicts(sc, ref, items):
    """"This stack (N), after AH cut": what it brings at auction (Sell now, after
    the 5% cut), green when that beats a vendor and red when not, and a red
    "Vendor it" (also for one item) when a vendor pays as much or more."""
    picks = [i for i in other_items(ref, items, 60, avoid=SIDEBAR_WATCH) if ref.items[i]["cheapest"] >= 3 and ref.items[i]["market"] > 0]
    if not sc.check(len(picks) >= 4, "stack: enough items to test with"):
        return
    a, b_, c, d = picks[:4]

    def fn(iid, f):
        f = f + [""] * (10 - len(f))
        if iid == a:                      # a vendor pays more than the auction would
            f[6] = str(max(int(f[1]), 1) * 3)
        elif iid == b_:                   # a vendor will not buy it
            f[6] = "0"
        elif iid == c:                    # the same either way: (21 - 1) * 0.95 = 19
            f[1], f[6] = "21", "19"
            f[3] = "50"
        elif iid == d:                    # nothing listed now
            f[1] = "0"
            f[6] = "25"
        return f
    text = rewrite_items(ref.text, fn)
    r2 = RefData(text=text)
    w = World(r2, items, data_src=text)
    ns, M = w.ns, w.M
    tt = w.g.GameTooltip
    age = w.now - r2.updated
    pos, neg = TT_POS_RGB, TT_NEG_RGB
    for slot, (iid, count, verdict) in enumerate(((a, 20, "Vendor it: a vendor pays more"), (b_, 5, None),
                                                   (c, 1, "Vendor it: a vendor pays the same"), (d, 4, None),
                                                   (R.LINEN, 20, None), (R.RUNE, 1, None)), start=1):
        w.put_bag(4, slot, iid, count)
        tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
        tt.SetBagItem(tt, 4, slot)
        lines = w.lines(tt)
        want = expected_tooltip(r2, iid, age, count=count)
        sc.eq(lines, want, "stack %d x%d tooltip" % (iid, count))
        texts = [l for l, _ in lines]
        if verdict:
            sc.check(any(t == verdict for t in texts), "stack %d: verdict %r (%s)" % (iid, verdict, texts))
        if iid == d:
            sc.check(not any(t.startswith(("This stack", "Vendor it")) for t in texts),
                     "stack %d: nothing listed: no stack line (the game's own line says what a vendor pays)" % iid)
        if iid == b_:
            sc.check("This stack (5), after AH cut" in texts and not any(t.startswith("Vendor it") for t in texts),
                     "stack %d: a vendor will not buy it: the stack's AH value, no 'Vendor it' (%s)" % (iid, texts))
        # the model's verdict for the stack's colour: green when the auction house pays more
        # than a vendor (or a vendor will not buy it), red when not
        _, _, mv = rec_model.stack_values(ref_sell_prices(r2, iid)["now"], count, r2.items[iid]["vendor"])
        al = M.addonLines(tt)
        for k in range(1, len(al) + 1):
            ln = al[k]
            if (ln.left or "").startswith("This stack"):
                good = mv in ("auction", "novendor")
                sc.eq((round(ln.rr, 2), round(ln.rg, 2), round(ln.rb, 2)), pos if good else neg,
                      "stack %d: the stack's value is %s" % (iid, "green" if good else "red"))
            elif (ln.left or "").startswith("Vendor it"):
                sc.eq((round(ln.lr, 2), round(ln.lg, 2), round(ln.lb, 2)), neg, "stack %d: vendor verdict is red" % iid)
    # with the auction house open, the live listing is used
    w.open_ah_modern()
    lp = r2.items[R.LINEN]["cheapest"] + 11
    w.set_commodity_results(R.LINEN, [(lp, 30, False)])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", R.LINEN)
    w.settle(0.3)
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetBagItem(tt, 4, 5)
    sc.eq(w.lines(tt), expected_tooltip(r2, R.LINEN, age, count=20, live=lp), "stack: live listing used for Sell now and the stack")
    tooltip_values_ok(sc, r2, R.LINEN, w.lines(tt), 1, "stack live", live=lp)
    w.close_ah_modern()
    # compact tooltips stay one line
    w.slash("/fah compact")
    tt.SetBagItem(tt, 4, 1)
    sc.eq(w.lines(tt), expected_tooltip(r2, a, age, compact=True), "stack: compact tooltip has no stack lines")
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
    """Bags.lua AuctionUnit: Sell now, else the market price (rough) when none is listed."""
    it = ref.items.get(iid)
    if not it:
        return None, False
    sp = ref_sell_prices(ref, iid, "1D")
    if sp["now"]:
        return sp["now"], price_doubt(ref, iid)
    if sp["nowWhy"] == "none" and it["market"] > 0:
        return it["market"], True
    return None, False


def bag_value(w, ref, iid, count, bound=False):
    vendor = int(w.M["items"][iid].vendor or 0)
    unit, unsure = (None, False) if bound else bag_auction_unit(ref, iid)
    a_each = math.floor(unit * 0.95) if unit else 0
    v_total, a_total = vendor * count, a_each * count
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
    out = w.slash("/fah bags")
    txt = [strip_codes(x) for x in out]
    sc.check(any(("bags: %d of 80 slots free." % (80 - len(layout))) in t for t in txt), "bags slash: free slots (%s)" % txt[:2])
    sc.check(any("merge stacks" in t and "Wool Bolt" in t for t in txt), "bags slash: merge line")
    sc.check(any("old quest items" in t and "Dusty Journal" in t and "Murloc Fin" in t for t in txt), "bags slash: quest line")
    sc.check(any(t.endswith("junk: 2 grey items, 86c at a vendor.") for t in txt), "bags slash: junk line (%s)" % txt)
    first = want[0]
    sc.check(any(" 1. " in t and ("[%s]" % w.M["items"][first[5]].name) in t for t in txt), "bags slash: cheapest first (%s)" % txt)
    sc.check(any("Open your bags" in t for t in txt), "bags slash: says to open the bags")
    helpout = [strip_codes(x) for x in w.slash("/fah help")]
    sc.check(any(t.endswith("/fah bags : what to drop or sell first, old quest items and stacks to merge (also beside your open bags)")
                 for t in helpout), "bags: help lists /fah bags")
    sc.check(any("/fah watch" in t for t in helpout), "bags: help still has the sidebar commands")

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
        M.enter(r)
        lines = [strip_codes(l or "") for l, _ in w.all_lines(tt)]
        sc.check("Worth the least in your bags" in lines, "bags hover: our heading (%s)" % lines)
        sc.check(any(l.startswith("Click: pick it up") for l in lines), "bags hover: says what a click does")
        btn = g["ContainerFrame%dItem%d" % (e.bag + 1, 17 - e.slot)]
        sc.check(BP.glow.IsShown(BP.glow) and BP.glow._points[1][2] is not None and w.same(BP.glow._points[1][2], btn),
                 "bags hover: the bag slot is outlined")
        M.leave(r)
        sc.check(not BP.glow.IsShown(BP.glow), "bags hover: outline goes on leave")
        # Click away from a vendor: picks it up, never uses it.
        mark = w.chat_mark()
        M.click(r, "LeftButton")
        p = M.pickups[len(M.pickups)]
        sc.eq((p[1], p[2]), (e.bag, e.slot), "bags click: picks the item up")
        sc.eq(len(M.used), 0, "bags click: never uses an item away from a vendor")
        sc.check(any("Click the ground to destroy it (the game asks first)" in c for c in w.chat(mark)), "bags click: says how to destroy")
        w.lua.execute("ClearCursor()")
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
        w.slash("/fah keep %d" % kid)
    # /fah keep by name, list, undo.
    p2name = ref.items[p2]["name"]
    out = [strip_codes(x) for x in w.slash("/fah keep " + p2name)]
    sc.check(any(("never suggest " + p2name) in t for t in out), "bags keep: by name (%s)" % out)
    s3 = ns.BagScan()
    sc.check(p2 not in [s3.drop[i].id for i in range(1, len(s3.drop) + 1)], "bags keep: not listed")
    out = [strip_codes(x) for x in w.slash("/fah keep")]
    sc.check(any(p2name in t for t in out), "bags keep: listed (%s)" % out)
    w.slash("/fah keep " + p2name)
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
        out = [strip_codes(x) for x in w.slash("/fah bags")]
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
    no tab, and /fah bags still works."""
    w = World(ref, items)
    w.lua.execute("ContainerFrame1:SetID(0); ContainerFrame1:ClearAllPoints(); ContainerFrame1:Show()")
    w.settle(0.2)
    BP = w.ns.BagPanel
    sc.check(not BP.built or not BP.tab.IsShown(BP.tab), "parked: no tab for a bag window with no position")
    bag_add_item(w, 900303, "Brittle Bone", 0, 3, classID=15)
    w.put_bag(0, 1, 900303, 1)
    out = [strip_codes(x) for x in w.slash("/fah bags")]
    sc.check(any("Brittle Bone" in t for t in out), "parked: /fah bags lists the bags")
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
    ("old-data-format", sc_old_format),
    ("stack-verdicts", sc_stack_verdicts),
    ("bag-cleanup", sc_bag_cleanup),
    ("bag-cleanup-no-data", sc_bag_cleanup_nodata),
    ("bag-cleanup-elvui", sc_bag_cleanup_elvui),
    ("bag-cleanup-parked", sc_bag_cleanup_parked),
    ("bag-tab-header", sc_bag_tab_header),
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

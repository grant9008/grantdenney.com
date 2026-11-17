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
DATA_FILE = os.path.join(LOCAL_DIR, "ForeverAH_Data.lua")
SIM = os.path.join(HERE, "sim_fetch.py")  # makes the simulated test data in wow/_localdata

TOC_FILES = ["Data.lua", "Core.lua", "Recommend.lua", "Chart.lua", "AH.lua", "Sidebar.lua"]
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


class RefData:
    """Reads ForeverAH_Data.lua with plain Python (no Lua)."""

    def __init__(self, path):
        raw = open(path, "rb").read().decode("utf-8")
        self.first_line = raw.split("\n", 1)[0].rstrip("\r")

        def num(key):
            m = re.search(r"\b" + key + r"\s*=\s*(-?\d+)", raw)
            return int(m.group(1)) if m else None

        def strv(key):
            m = re.search(r"\b" + key + r'\s*=\s*"((?:[^"\\]|\\.)*)"', raw)
            return lua_unescape(m.group(1)) if m else None

        self.updated = num("updated")
        self.hourEnd = num("hourEnd")
        self.dayEnd = num("dayEnd")
        self.house = num("house")
        self.houseName = strv("houseName")
        self.region = strv("region")
        self.ruleset = strv("ruleset")
        self.items = {}
        for m in re.finditer(r'\[(\d+)\]\s*=\s*"((?:[^"\\\n]|\\.)*)"', raw):
            iid = int(m.group(1))
            f = lua_unescape(m.group(2)).split("|")
            f += [""] * (9 - len(f))

            def n10(x):
                try:
                    return int(x)
                except ValueError:
                    return 0
            self.items[iid] = {
                "id": iid, "name": f[0], "cheapest": n10(f[1]), "market": n10(f[2]),
                "listed": n10(f[3]), "sold24": n10(f[4]), "avg7": n10(f[5]), "vendor": n10(f[6]),
                "H": decode_series(f[7]), "D": decode_series(f[8]),
            }

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
    r = rec_model.recommend(it, windows, tf, live)
    r["points"] = ref.window(iid, r["tf"])["n"]
    return r


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
function FontString:GetStringWidth() return #(self._text or "") * 6 end
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
  table.insert(self._lines, { left = l, right = r, lr = lr, lg = lg, lb = lb })
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
    return it and it.name or "", M.ItemLink(d.id), d.id
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
  local id, guid, count
  if getterName == "GetBagItem" then
    local slot = M.bags[args[1]] and M.bags[args[1]][args[2]]
    if not slot then return nil end
    id = slot.itemID
    guid = "Item-1-0-" .. args[1] .. "-" .. args[2]
    M.guids[guid] = ItemLocation:CreateFromBagAndSlot(args[1], args[2])
  elseif getterName == "GetItemByID" then
    id = args[1]
  elseif getterName == "GetHyperlink" then
    id = tonumber(tostring(args[1]):match("item:(%d+)"))
  elseif getterName == "GetInventoryItem" then
    id = M.equipped and M.equipped[args[2]]
  end
  if not id then return nil end
  local it = M["items"][id]
  local lines = { { type = 0, leftText = it and it.name or "Retrieving item information" } }
  if it and it.class then lines[2] = { type = 0, leftText = it.class } end
  return { type = Enum.TooltipDataType and Enum.TooltipDataType.Item or 0, id = id, guid = guid,
           hyperlink = M.ItemLink(id), lines = lines }
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
function GetMoney() return 1234567 end
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
function M.ItemLink(id, suffix)
  local it = M["items"][id]
  local name = it and it.name or ("item:" .. id)
  if suffix then
    return "|cff1eff00|Hitem:" .. id .. "::::::" .. suffix .. "::60:::::::::|h[" .. name .. " of the Monkey]|h|r"
  end
  return "|cffffffff|Hitem:" .. id .. "::::::::60:::::::::|h[" .. name .. "]|h|r"
end
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
function C_Item.GetItemLink(loc) local s = locSlot(loc); return s and M.ItemLink(s.itemID, s.suffix) end
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
  return it.name, M.ItemLink(id), it.quality, 1, 0, it.class or "Trade Goods", "Trade Goods", 20, "",
    it.icon, it.vendor or 0, 7, 0, 0, 0, nil, true
end
function C_Item.GetItemInfoInstant(v) local id = idOf(v); if id then return id, "Trade Goods", "Trade Goods", "", 134400, 7, 0 end end
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
  return { iconFileID = it and it.icon or 134400, stackCount = s.count, isLocked = false, quality = it and it.quality or 1,
           isReadable = false, hasLoot = false, hyperlink = M.ItemLink(s.itemID), isFiltered = false,
           hasNoValue = false, itemID = s.itemID, isBound = false }
end
function C_Container.GetContainerItemLink(bag, slot) local s = M.bags[bag] and M.bags[bag][slot]; return s and M.ItemLink(s.itemID) end
function C_Container.GetContainerNumSlots(bag) return 16 end
function GetInventoryItemCount(unit, slot) return 1 end

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
                     itemLink = M.ItemLink(itemKey.itemID), containsOwnerItem = r.own == true, containsAccountItem = false,
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
  "AUCTION_HOUSE_POST_ERROR", "AUCTION_HOUSE_THROTTLED_SYSTEM_READY", "OWNED_AUCTIONS_UPDATED" }
M.LEGACY_EVENTS = { "ADDON_LOADED", "PLAYER_LOGIN", "PLAYER_ENTERING_WORLD", "PLAYER_LOGOUT", "VARIABLES_LOADED",
  "AUCTION_HOUSE_SHOW", "AUCTION_HOUSE_CLOSED", "AUCTION_ITEM_LIST_UPDATE", "NEW_AUCTION_UPDATE",
  "AUCTION_OWNED_LIST_UPDATE", "AUCTION_BIDDER_LIST_UPDATE", "PLAYER_INTERACTION_MANAGER_FRAME_SHOW",
  "PLAYER_INTERACTION_MANAGER_FRAME_HIDE", "MODIFIER_STATE_CHANGED", "GET_ITEM_INFO_RECEIVED", "BAG_UPDATE" }
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
                 set_owner_clears=True, copper_values=True):
        self.ref = ref
        self.lua = lua51.LuaRuntime(unpack_returned_tuples=True)
        self.g = self.lua.globals()
        self.loader = self.lua.eval(
            "function(src, name, ...) local f, e = loadstring(src, name); if not f then error(e) end; return f(...) end")
        self.loader(MOCK_LUA, "@MOCK/wow.lua")
        self.same = self.lua.eval("function(a, b) return rawequal(a, b) end")
        self.M = self.g.MOCK
        M = self.M
        M.SetClient(client)
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
        # Items that are not in the price data.
        for iid, name, commodity in [(99901, "Recruit's Shirt", False), (2996, "Bolt of Linen Cloth", True),
                                     (4238, "Linen Bag", False)]:
            if iid in self.ref.items:
                continue
            t = self.lua.table()
            t.name, t.quality, t.icon, t.vendor, t.commodity = name, 1, 130000 + iid, 10, commodity
            t["class"] = "Trade Goods"
            M["items"][iid] = t

    # helpers ---------------------------------------------------------------
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

    def put_bag(self, bag, slot, iid, count, suffix=None):
        M = self.M
        if M.bags[bag] is None:
            M.bags[bag] = self.lua.table()
        s = self.lua.table()
        s.itemID, s.count = iid, count
        if suffix:
            s.suffix = suffix
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


def expected_tooltip(ref, iid, age, mult=1, compact=False):
    it = ref.items.get(iid)
    if it is None:
        return [("Forever AH: no recent listings", None)]
    tag = " (x%d)" % mult if mult > 1 else ""

    def m(v):
        return money_icons(v * mult) if v > 0 else None
    out = []
    if compact:
        mk, ch = m(it["market"]), m(it["cheapest"])
        if mk and ch:
            line = "AH%s: %s (cheapest %s)" % (tag, mk, ch)
        elif mk:
            line = "AH%s: %s (none listed now)" % (tag, mk)
        elif ch:
            line = "AH%s: cheapest %s" % (tag, ch)
        else:
            line = "AH: none listed now"
        out.append((line, None))
    else:
        out.append(("Forever AH", age_text(age)))
        out.append(("Market" + tag, m(it["market"])) if it["market"] > 0 else ("Market", "none listed"))
        out.append(("Cheapest" + tag, m(it["cheapest"])) if it["cheapest"] > 0 else ("Cheapest", "none listed"))
        if it["avg7"] > 0:
            out.append(("7-day avg" + tag, m(it["avg7"])))
        out.append(("Listed %s, ~%s sold/day" % (commas(it["listed"]), commas(it["sold24"])), None))
    if age > 6 * 3600:
        out.append(("Prices are %dh old: run Update-WoW-Prices.bat, then /reload" % (age // 3600), None))
    return out


def tooltip_values_ok(sc, ref, iid, lines, mult, label):
    """Value-level check, independent of wording: every price on the tooltip equals the data file."""
    it = ref.items[iid]
    want = {"Market": it["market"], "Cheapest": it["cheapest"], "7-day avg": it["avg7"]}
    for left, right in lines:
        base = (left or "").split(" (x")[0]
        if base in want and right and right != "none listed":
            sc.eq(parse_coins(right), display_copper(want[base] * mult), "%s %s value" % (label, base))


def check_series_and_recommend(sc, w, ref, ids=None, tfs=TFS):
    ns = w.ns
    ids = ids or sorted(ref.items)
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
            for k in ("buy", "sell", "listAt", "margin", "verdict", "lo", "hi", "cheapestNow", "marketNow", "sold24"):
                sc.eq(r[k], want_r[k], "Recommend(%d,%s).%s" % (iid, tf, k))
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
    sc.eq(C.name._text, it["name"] if len(it["name"]) <= 34 else it["name"][:33] + "...", "%s: header name" % label)
    nowp = it["market"] if it["market"] > 0 else it["cheapest"]
    sc.eq(C.price._text, money_icons(nowp) if nowp > 0 else "", "%s: header price" % label)
    mk = [v for v in win["market"] if v is not None]
    if win["n"] > 1 and mk:
        p = (mk[-1] - mk[0]) / mk[0] * 100
        sc.eq(C.change._text, "%+.1f%% %s" % (p, tf), "%s: header change" % label)
    sc.eq(C.buyBox.value._text, money_icons(want["buy"]) if want["buy"] > 0 else "-", "%s: Target Buy box" % label)
    sc.eq(C.listBox.value._text, money_icons(want["listAt"]) if want["listAt"] > 0 else "-", "%s: List At box" % label)
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
    else:
        sc.eq(C.verdict._text, want["verdict"], "%s: verdict" % label)
        mtxt = "Margin after 5% cut: " + ("+" if want["margin"] >= 0 else "") + money_text(want["margin"]) + \
            (" (%+.1f%%)" % (want["marginPct"] * 100))
        sc.eq(C.margin._text, mtxt, "%s: margin text" % label)
    sc.eq(C.volume._text, "~%s sold/day \u00b7 %s listed" % (commas(want["sold24"]), commas(it["listed"])),
          "%s: volume text" % label)
    notes = [mismatch] if mismatch else list(want["notes"])
    ahs = ns.AHState
    if ahs is not None and ahs.suffix[iid]:
        notes.append("Price covers all versions of this item")
    sc.eq(C.notes._text or "", ". ".join(notes), "%s: notes" % label)
    narrow = C.frame._w < 650
    sc.check(C.change._shown != narrow and C.volume._shown != narrow,
             "%s: change and volume %s at width %s" % (label, "hidden" if narrow else "shown", C.frame._w))
    compact = (C.panelH or 260) < 230
    sc.check(C.buyBox._shown != compact and C.listBox._shown != compact,
             "%s: boxes %s at height %s" % (label, "hidden" if compact else "shown", C.panelH))
    sc.eq(C.age._text, "Data: %s \u00b7 %s" % (age_text(w.now - ref.updated), ref.houseName), "%s: age" % label)
    sc.check(not C.msg._shown, "%s: no empty-state message (%s)" % (label, C.msg._text))

    # Dots: one per non-null price, placed on one straight price scale.
    pools = C.pools
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
            # Dashed target tags show the advice numbers.
            tags = [pools.tag["items"][k]._text for k in range(1, int(pools.tag.used) + 1)]
            if want["buy"] > 0:
                sc.check("Target Buy " + money_text(want["buy"]) in tags, "%s: buy line label (%s)" % (label, tags))
            if want["listAt"] > 0:
                sc.check("List At " + money_text(want["listAt"]) in tags, "%s: list line label (%s)" % (label, tags))
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
    # Timeframe buttons: active one highlighted
    for k in TFS:
        btn = C.tfButtons[k]
        col = btn.bg._color
        active = abs(col[4] - 0.35) < 1e-9
        sc.check(active == (k == tf), "%s: %s button highlight" % (label, k))


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
    M.cursorX = (left + idx * stepX) * es
    M.cursorY = 100
    M.enter(plot)
    w.tick(1)
    tt = w.g.GameTooltip
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
    sc.eq(ns.ItemCount(), len(ref.items), "item count")
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
    for iid, it in ref.items.items():
        g = ns.GetItem(iid)
        for k in ("name", "cheapest", "market", "listed", "sold24", "avg7", "vendor"):
            sc.eq(g[k], it[k], "GetItem(%d).%s" % (iid, k))
        sc.eq(ns.FindItemID(it["name"].upper()), iid, "FindItemID(%r upper)" % it["name"])
        sc.eq(ns.FindItemID("  " + it["name"].lower() + " "), iid, "FindItemID(%r lower, padded)" % it["name"])
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
    check_series_and_recommend(sc, w, ref, ids=[2589], tfs=["1D"])
    sc.eq(int(w.ns.stats.series), n1, "series decoded once per item (cached)")
    sc.clean(w)


def sc_status(sc, ref, items):
    w = World(ref, items)
    out = w.slash("/fah")
    txt = "\n".join(out)
    sc.check(("prices for %d items from the %s" % (len(ref.items), ref.houseName)) in txt, "status names item count and house (%s)" % out[:1])
    sc.check("(US, PvP)" in txt, "status names region and ruleset")
    sc.check("updated 25m ago" in txt, "status shows data age")
    sc.check("Type /fah help" in txt, "status points at help")
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
    w.put_bag(0, 1, 2589, 20)
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetBagItem(tt, 0, 1)
    lines = w.lines(tt)
    sc.eq(lines, expected_tooltip(ref, 2589, age), "2589 bag tooltip lines")
    tooltip_values_ok(sc, ref, 2589, lines, 1, "2589")
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
    tt.SetItemByID(tt, 13468)
    lines = w.lines(tt)
    sc.eq(lines, expected_tooltip(ref, 13468, age), "13468 tooltip lines")
    tooltip_values_ok(sc, ref, 13468, lines, 1, "13468")
    sc.check(all("Copper" not in (r or "") for _, r in lines[1:4]), "13468 above 100g: copper dropped")
    # Unknown item
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetItemByID(tt, 99901)
    sc.eq(w.lines(tt), [("Forever AH: no recent listings", None)], "unknown item tooltip")
    # Shift held on a stack of 20
    w.M.shift = True
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetBagItem(tt, 0, 1)
    lines = w.lines(tt)
    sc.eq(lines, expected_tooltip(ref, 2589, age, mult=20), "2589 shift x20 tooltip")
    tooltip_values_ok(sc, ref, 2589, lines, 20, "2589 x20")
    # Shift held on a single item (by id): no multiplier
    tt.SetItemByID(tt, 14047)
    sc.eq(w.lines(tt), expected_tooltip(ref, 14047, age), "14047 shift, count 1: unit prices")
    # Shift released, MODIFIER_STATE_CHANGED rebuild on the bag item
    tt.SetBagItem(tt, 0, 1)
    w.M.shift = False
    tt.RefreshData(tt)
    sc.eq(w.lines(tt), expected_tooltip(ref, 2589, age), "2589 after shift released")
    # Every item, full and compact
    for iid in sorted(ref.items):
        tt.SetItemByID(tt, iid)
        sc.eq(w.lines(tt), expected_tooltip(ref, iid, age), "tooltip %d" % iid)
    out = w.slash("/fah compact")
    sc.check(any("one line" in l for l in out), "/fah compact message")
    for iid in sorted(ref.items):
        tt.SetItemByID(tt, iid)
        sc.eq(w.lines(tt), expected_tooltip(ref, iid, age, compact=True), "compact tooltip %d" % iid)
    w.M.shift = True
    tt.SetBagItem(tt, 0, 1)
    sc.eq(w.lines(tt), expected_tooltip(ref, 2589, age, mult=20, compact=True), "compact x20 tooltip")
    w.M.shift = False
    w.slash("/fah compact")
    tt.SetItemByID(tt, 2589)
    sc.eq(w.lines(tt), expected_tooltip(ref, 2589, age), "full tooltip again after second /fah compact")
    # Tooltip off and on
    out = w.slash("/fah tooltip")
    sc.check(any("off" in l for l in out), "/fah tooltip off message")
    tt.SetItemByID(tt, 2589)
    sc.eq(w.lines(tt), [], "tooltip off: no lines")
    w.slash("/fah tooltip")
    tt.SetItemByID(tt, 2589)
    sc.eq(w.lines(tt), expected_tooltip(ref, 2589, age), "tooltip on again")
    # Other tooltips
    ir = w.g.ItemRefTooltip
    ir.SetHyperlink(ir, w.M.ItemLink(12363))
    sc.eq(w.lines(ir), expected_tooltip(ref, 12363, age), "ItemRefTooltip from a chat link")
    st = w.g.ShoppingTooltip1
    st.SetItemByID(st, 14047)
    sc.eq(w.lines(st), expected_tooltip(ref, 14047, age), "comparison tooltip")
    ot = w.g.OtherAddonTooltip
    ot.SetItemByID(ot, 2589)
    sc.eq(w.lines(ot), [], "unlisted tooltip gets nothing")
    ft = w.g.ForbiddenTooltip
    ft.SetItemByID(ft, 2589)
    sc.eq(w.lines(ft), [], "forbidden tooltip gets nothing")
    info = w.lua.table()
    info.getterName = "GetItemByID"
    info.getterArgs = w.to_lua([2589])
    info.excludeLines = w.to_lua([1])
    tt.ProcessInfo(tt, info)
    sc.eq(w.lines(tt), [], "excludeLines tooltip gets nothing")
    sc.clean(w)


def sc_tooltip_legacy_hooks(sc, ref, items):
    w = World(ref, items, client="legacy")
    sc.eq(w.ns.tooltipMode, "script", "legacy client: OnTooltipSetItem fallback")
    tt = w.g.GameTooltip
    age = w.now - ref.updated
    tt.SetOwner(tt, w.g.UIParent, "ANCHOR_RIGHT")
    tt.SetItemByID(tt, 2589)
    sc.eq(w.lines(tt), expected_tooltip(ref, 2589, age), "legacy tooltip lines")
    w.M.fireScript(tt, "OnTooltipSetItem")
    sc.eq(w.lines(tt), expected_tooltip(ref, 2589, age), "legacy second OnTooltipSetItem adds nothing")
    sc.clean(w)


def sc_stale(sc, ref, items):
    w = World(ref, items, now=ref.updated + 7 * 3600 + 120)
    age = w.now - ref.updated
    tt = w.g.GameTooltip
    tt.SetItemByID(tt, 2589)
    lines = w.lines(tt)
    sc.eq(lines, expected_tooltip(ref, 2589, age), "stale tooltip lines")
    sc.check(lines[-1][0] == "Prices are 7h old: run Update-WoW-Prices.bat, then /reload", "stale line text")
    w.slash("/fah compact")
    tt.SetItemByID(tt, 2589)
    sc.eq(w.lines(tt), expected_tooltip(ref, 2589, age, compact=True), "stale compact tooltip keeps the stale line")
    out = "\n".join(w.slash("/fah"))
    sc.check("updated 7h ago" in out, "status shows 7h")
    sc.check("Prices are 7h old: run Update-WoW-Prices.bat, then /reload." in out, "status stale warning")
    w.slash("/fah chart Linen Cloth")
    sc.eq(w.ns.ChartPanel.age._text, "Data: 7h ago \u00b7 " + ref.houseName, "chart footer shows 7h")
    sc.clean(w)


def sc_placeholder(sc, ref, items):
    src = open(os.path.join(ADDON, "Data.lua"), "rb").read().decode("ascii")
    sc.eq(src, PLACEHOLDER, "repo Data.lua is the contract placeholder")
    w = World(ref, items, data_src=src)
    ns = w.ns
    sc.eq(w.load_errors, [], "loads with the placeholder")
    sc.check(ns.DataAge() is None, "DataAge nil")
    sc.eq(ns.ItemCount(), 0, "no items")
    sc.check(any("no price data yet" in c for c in w.chat()), "login hint about missing data")
    out = w.slash("/fah")
    sc.check(any("Update-WoW-Prices.bat" in l for l in out) and any("/reload" in l for l in out), "status explains the updater")
    tt = w.g.GameTooltip
    tt.SetItemByID(tt, 2589)
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
    w.put_bag(0, 1, 2589, 20)
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    sc.check(C.frame.IsShown(C.frame) and C.msg._shown, "AH open: chart shows the no-data message")
    ah.SearchBar.SearchBox.SetText(ah.SearchBar.SearchBox, "Linen Cloth")
    ah.SearchBar.StartSearch(ah.SearchBar)
    w.set_browse([(2589, 47, 900)])
    w.M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.5)
    r = w.lua.table()
    r.itemKey = w.to_lua({"itemID": 2589, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0})
    r.minPrice = 47
    ah.SelectBrowseResult(ah, r)
    w.settle(0.2)
    sc.check(C.msg._shown, "still the no-data message after a search")
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1))
    w.settle(0.2)
    b = ns.AHState and None
    # Use button stays hidden without advice
    shown = [k for k in ("CommoditiesSellFrame", "ItemSellFrame")]
    sf = ah.CommoditiesSellFrame
    btns = [c for c in (sf._children[i] for i in range(1, len(sf._children) + 1))
            if c._otype == "Button" and (c._text or "").startswith("Use List At")]
    sc.check(all(not b._shown for b in btns), "Use List At hidden without data")
    w.close_ah_modern()
    sc.check(not C.frame.IsShown(C.frame), "chart hidden on AH close")
    sc.clean(w)


def sc_saved_settings(sc, ref, items):
    w = World(ref, items, saved={"chartTF": "1w", "compact": True, "tooltip": "yes", "chart": False,
                                  "chartPoint": ["TOPLEFT", "TOPLEFT", 100, -200], "lastItem": 14047})
    S = w.ns.settings
    sc.eq(S.chartTF, "1W", "saved chartTF normalised")
    sc.eq(S.compact, True, "saved compact kept")
    sc.eq(S.tooltip, True, "bad saved tooltip type falls back to the default")
    sc.eq(S.chart, False, "saved chart=false kept")
    sc.check(w.same(w.g.ForeverAH_Settings, S), "ForeverAH_Settings is ns.settings")
    w.slash("/fah chart")
    C = w.ns.ChartPanel
    sc.eq(w.ns.ChartItem(), 14047, "/fah chart reopens the last item")
    p = C.frame._points[1]
    sc.check(p[1] == "TOPLEFT" and w.same(p[2], w.g.UIParent) and p[4] == 100 and p[5] == -200, "saved chart position used")
    chart_check(sc, w, ref, 14047, "1W", "saved 1W")
    sc.clean(w)


def sc_chart_standalone(sc, ref, items):
    w = World(ref, items)
    ns = w.ns
    out = w.slash("/fah chart Runecloth")
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
    chart_check(sc, w, ref, 14047, "1D", "Runecloth 1D")
    hover_check(sc, w, ref, 14047, "1D", 5, "Runecloth 1D")
    hover_check(sc, w, ref, 14047, "1D", ref.window(14047, "1D")["n"] - 1, "Runecloth 1D last")
    # Link and id forms
    w.slash("/fah chart " + w.M.ItemLink(13468))
    sc.eq(ns.ChartItem(), 13468, "/fah chart with an item link")
    w.slash("/fah chart 2589")
    sc.eq(ns.ChartItem(), 2589, "/fah chart with an item id")
    out = w.slash("/fah chart Not A Real Item")
    sc.check(any("no price data for Not A Real Item" in l for l in out), "unknown name message")
    sc.eq(ns.ChartItem(), 2589, "unknown name leaves the chart alone")
    # Drag saves the position, Escape closes
    w.M.drag(F, "CENTER", "CENTER", 40, 30)
    cp = ns.settings.chartPoint
    sc.check(cp is not None and cp[1] == "CENTER" and cp[3] == 40 and cp[4] == 30, "drag saves chartPoint")
    sc.check("ForeverAHChartFrame" in [w.g.UISpecialFrames[i] for i in range(1, len(w.g.UISpecialFrames) + 1)],
             "chart closes with Escape")
    sc.eq(w.M.pressEscape(), "ForeverAHChartFrame", "Escape hides the chart")
    sc.check(not F.IsShown(F), "chart hidden after Escape")
    w.slash("/fah chart")
    sc.check(F.IsShown(F) and ns.ChartItem() == 2589, "/fah chart reopens the last item")
    # Close button while standalone keeps settings.chart
    M = w.M
    M.click(C.closeButton)
    sc.check(not F.IsShown(F), "close button hides the chart")
    sc.eq(ns.settings.chart, True, "closing a standalone chart keeps the AH chart setting")
    sc.clean(w)


def sc_chart_timeframes(sc, ref, items):
    w = World(ref, items)
    ns, M = w.ns, w.M
    w.slash("/fah chart Runecloth")
    C = ns.ChartPanel
    for tf in TFS:
        M.click(C.tfButtons[tf])
        w.settle(0.1)
        chart_check(sc, w, ref, 14047, tf, "Runecloth %s" % tf)
        win = ref.window(14047, tf)
        hover_check(sc, w, ref, 14047, tf, win["n"] // 2, "Runecloth %s mid" % tf)
    # Every item, every timeframe
    for iid in sorted(ref.items):
        for tf in TFS:
            ns.SetChartTF(tf)
            ns.ShowChart(iid, "slash")
            chart_check(sc, w, ref, iid, tf, "%d %s" % (iid, tf))
    # Pools: a second full pass must not create any new textures, lines or font strings
    before = {k: int(w.M.counts[k] or 0) for k in ("Texture", "Line", "FontString", "Frame", "Button")}
    for iid in sorted(ref.items):
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
    w.slash("/fah chart Runecloth")
    hover_check(sc, w, ref, 14047, "1D", 3, "no-clear SetOwner")
    sc.clean(w)


def find_use_button(sf):
    for i in range(1, len(sf._children) + 1):
        c = sf._children[i]
        if c._otype == "Button" and c.isCommodity is not None:
            return c
    return None


def sc_ah_modern(sc, ref, items, screen=None, ah_width=800, label="default screen", placement=None):
    w = World(ref, items, screen=screen, ah_width=ah_width)
    ns, M = w.ns, w.M
    w.put_bag(0, 1, 14047, 20)
    w.put_bag(0, 2, 12363, 3)
    w.put_bag(0, 3, 99901, 1)
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
    sb.SearchBox.SetText(sb.SearchBox, "Linen Cloth")
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    sc.eq(w.M.cah.browse[len(w.M.cah.browse)], "Linen Cloth", "%s: Blizzard sent the browse query" % label)
    w.set_browse([(2996, 3000, 40), (2589, 46, 900), (4238, 2500, 3)])
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.4)
    chart_check(sc, w, ref, 2589, "1D", "%s: search Linen Cloth" % label)
    # Partial search picks the first result with data
    sb.SearchBox.SetText(sb.SearchBox, "linen")
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), 2589, "%s: partial search waits for results" % label)
    w.set_browse([(4238, 2500, 3), (2589, 46, 900)])
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), 2589, "%s: partial search picks the first result with prices" % label)
    # Empty search string (full scan / category browse) is ignored
    sb.SearchBox.SetText(sb.SearchBox, "")
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.set_browse([(14047, 100, 10)])
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), 2589, "%s: empty search ignored" % label)
    # Auctionator-style search text
    sb.SearchBox.SetText(sb.SearchBox, '"Black Lotus";;0;0;0;0;0;0;0;0')
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), 13468, "%s: Auctionator search syntax" % label)
    # Burst of searches redraws once, last one wins
    for name in ("Runecloth", "Arcane Crystal", "Linen Cloth"):
        sb.SearchBox.SetText(sb.SearchBox, name)
        M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), 2589, "%s: burst of searches ends on the last" % label)

    # Select a browse result (commodity)
    r = w.lua.table()
    r.itemKey = w.to_lua({"itemID": 14047, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0})
    r.minPrice = 1200
    ah.SelectBrowseResult(ah, r)
    sc.eq(ns.ChartItem(), 14047, "%s: selecting a result switches the chart at once" % label)
    sc.eq(ah.CommoditiesBuyFrame.GetItemID(ah.CommoditiesBuyFrame), 14047, "%s: commodity buy frame shows it" % label)
    sc.check(ah.CommoditiesBuyFrame._shown, "%s: commodity buy display mode" % label)
    chart_check(sc, w, ref, 14047, "1D", "%s: selected Runecloth" % label)
    # A pending search does not override the explicit pick
    sb.SearchBox.SetText(sb.SearchBox, "Black Lotus")
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    ah.SelectBrowseResult(ah, r)
    w.settle(0.4)
    sc.eq(ns.ChartItem(), 14047, "%s: explicit pick beats a pending search" % label)
    # Select a gear result we have no data for
    r2 = w.lua.table()
    r2.itemKey = w.to_lua({"itemID": 99901, "itemLevel": 5, "itemSuffix": 0, "battlePetSpeciesID": 0})
    r2.minPrice = 500
    ah.SelectBrowseResult(ah, r2)
    sc.check(ah.ItemBuyFrame._shown and ah.ItemBuyFrame.itemKey.itemID == 99901, "%s: item buy frame" % label)
    sc.check(C.msg._shown and "No recent listings for Recruit's Shirt" in (C.msg._text or ""),
             "%s: no-data message for an item without prices (%s)" % (label, C.msg._text))
    sc.eq(C.buyBox.value._text, "-", "%s: empty Target Buy box" % label)

    # Timeframe switch while docked
    M.click(C.tfButtons["1M"])
    ah.SelectBrowseResult(ah, r)
    chart_check(sc, w, ref, 14047, "1M", "%s: docked 1M" % label)
    M.click(C.tfButtons["1D"])

    # Sell frame: Runecloth (commodity), Blizzard fills its default then results arrive
    rec = ref_recommend(ref, 14047, "1D")
    lowest = ref.items[14047]["cheapest"]
    res = w.lua.table()
    res[1] = w.to_lua({"unitPrice": lowest, "quantity": 50})
    w.M.commodityResults[14047] = res
    loc = w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1)
    ah.SetPostItem(ah, loc)
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", 14047)
    w.settle(0.2)
    csf = ah.CommoditiesSellFrame
    sc.check(csf._shown, "%s: commodity sell frame shown" % label)
    sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), lowest, "%s: Blizzard auto-filled the lowest price first" % label)
    sc.eq(ns.ChartItem(), 14047, "%s: sell item switches the chart" % label)
    b = find_use_button(csf)
    want = price_for_frame(rec["listAt"], True)
    sc.check(b is not None and b.IsVisible(b), "%s: Use List At button visible" % label)
    if b is not None:
        sc.eq(b._text, "Use List At: " + money_icons(want), "%s: Use List At button text" % label)
        r_b, r_p = w.rect(b), w.rect(csf.PostButton)
        sc.check(r_b and r_p and r_b[3] <= r_p[1] + 1e-6, "%s: Use button sits under Post" % label)
        before = csf.postStateUpdates
        M.click(b)
        sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want, "%s: Use List At fills the exact copper price" % label)
        sc.check(csf.postStateUpdates > before, "%s: post state refreshed" % label)
        sc.eq(csf.totalPrice, want * 20, "%s: total price refreshed" % label)
        M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", 14047)
        sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want, "%s: later results do not overwrite our price" % label)
    sc.eq(len(w.M.cah.posted), 0, "%s: nothing posted" % label)

    # Sell frame: an item auction (gear path) rounds down to whole silver
    w.M["items"][12363].commodity = False
    loc2 = w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 2)
    ah.SetPostItem(ah, loc2)
    w.settle(0.2)
    isf = ah.ItemSellFrame
    rec2 = ref_recommend(ref, 12363, "1D")
    want2 = price_for_frame(rec2["listAt"], False)
    b2 = find_use_button(isf)
    sc.check(b2 is not None and b2.IsVisible(b2), "%s: item sell Use button visible" % label)
    sc.check(b is None or not b.IsVisible(b), "%s: commodity Use button hidden once that frame is cleared" % label)
    if b2 is not None:
        sc.eq(b2._text, "Use List At: " + money_icons(want2), "%s: item sell button text (silver)" % label)
        M.click(b2)
        sc.eq(isf.PriceInput.GetAmount(isf.PriceInput), want2, "%s: item price rounded to silver" % label)
        sc.eq(want2 % 100, 0, "%s: item price is whole silver" % label)
    w.M["items"][12363].commodity = True
    # An item we have no advice for: button hidden
    loc3 = w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 3)
    ah.SetPostItem(ah, loc3)
    w.settle(0.2)
    sc.check(b2 is None or not b2.IsVisible(b2), "%s: Use button hidden for an item without data" % label)

    # Close button on the docked chart turns the AH chart off; toggle brings it back
    M.click(C.closeButton)
    sc.check(not C.frame.IsShown(C.frame), "%s: close button hides the docked chart" % label)
    sc.eq(ns.settings.chart, False, "%s: closing the docked chart sets chart=false" % label)
    ah.SelectBrowseResult(ah, r)
    sc.check(not C.frame.IsShown(C.frame), "%s: chart stays hidden while turned off" % label)
    if toggle is not None:
        M.click(toggle)
        sc.check(C.frame.IsShown(C.frame) and ns.ChartItem() == 14047, "%s: Chart button shows it again with the last item" % label)
        sc.eq(ns.settings.chart, True, "%s: Chart button turns the setting back on" % label)
    # /fah chart at the AH after closing it with x: follows searches again
    M.click(C.closeButton)
    w.slash("/fah chart")
    sc.check(C.frame.IsShown(C.frame), "%s: /fah chart at the AH shows the chart" % label)
    sb.SearchBox.SetText(sb.SearchBox, "Black Lotus")
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), 13468, "%s: chart opened with /fah chart at the AH follows searches" % label)
    w.slash("/fah chart")
    sc.check(not C.frame.IsShown(C.frame), "%s: /fah chart hides the docked chart" % label)
    sc.eq(ns.settings.chart, False, "%s: /fah chart hide at the AH turns the AH chart off" % label)
    sb.SearchBox.SetText(sb.SearchBox, "Runecloth")
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.check(not C.frame.IsShown(C.frame), "%s: hidden chart stays hidden on search" % label)
    w.slash("/fah chart Linen Cloth")
    sc.check(C.frame.IsShown(C.frame) and ns.ChartIsDocked() and ns.ChartItem() == 2589,
             "%s: /fah chart Linen Cloth at the AH shows it docked" % label)
    sb.SearchBox.SetText(sb.SearchBox, "Black Lotus")
    M.fireScript(sb.SearchBox, "OnEnterPressed")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), 13468, "%s: /fah chart <item> at the AH follows searches afterwards" % label)

    # Close the AH
    w.close_ah_modern()
    sc.check(not ns.AH_IsOpen(), "%s: AH closed" % label)
    sc.check(not C.frame.IsShown(C.frame), "%s: docked chart hidden on close" % label)
    sc.check(b2 is None or not b2._shown, "%s: Use buttons hidden on close" % label)
    sc.check(b is None or not b._shown, "%s: commodity Use button hidden on close" % label)
    # Searches after close are ignored
    M.fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
    w.settle(0.3)
    sc.check(not C.frame.IsShown(C.frame), "%s: events after close do not reopen the chart" % label)
    # Reopen: chart comes back docked with the last item
    w.open_ah_modern()
    sc.check(C.frame.IsShown(C.frame) and ns.ChartIsDocked() and ns.ChartItem() == 13468, "%s: reopen restores the docked chart" % label)
    w.close_ah_modern()
    # Standalone chart after the AH is closed floats again
    w.slash("/fah chart Runecloth")
    sc.check(C.frame.IsShown(C.frame) and not ns.ChartIsDocked(), "%s: standalone chart after close floats" % label)
    chart_check(sc, w, ref, 14047, "1D", "%s: standalone after AH" % label)
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
    r.itemKey = w.to_lua({"itemID": 2589, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0})
    r.minPrice = 46
    w.g.AuctionHouseFrame.SelectBrowseResult(w.g.AuctionHouseFrame, r)
    chart_check(sc, w, ref, 2589, "1D", "TSM select")
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
    w.g.BrowseName.SetText(w.g.BrowseName, "Linen Cloth")
    M.click(w.g.BrowseSearchButton)
    sc.eq(M.cah.browse[len(M.cah.browse)], "Linen Cloth", "legacy: Blizzard sent QueryAuctionItems")
    lst = w.lua.table()
    for i, (iid, cnt, bo) in enumerate([(2996, 5, 3000), (2589, 20, 940)]):
        lst[i + 1] = w.to_lua({"itemID": iid, "count": cnt, "buyout": bo})
    M.legacyList = lst
    M.fire("AUCTION_ITEM_LIST_UPDATE")
    w.settle(0.4)
    chart_check(sc, w, ref, 2589, "1D", "legacy search")
    # Partial search, then click a row
    w.g.BrowseName.SetText(w.g.BrowseName, "cloth")
    M.click(w.g.BrowseSearchButton)
    lst = w.lua.table()
    for i, (iid, cnt, bo) in enumerate([(4238, 1, 3000), (14047, 20, 9000), (2589, 20, 940)]):
        lst[i + 1] = w.to_lua({"itemID": iid, "count": cnt, "buyout": bo})
    M.legacyList = lst
    M.fire("AUCTION_ITEM_LIST_UPDATE")
    w.settle(0.4)
    sc.eq(ns.ChartItem(), 14047, "legacy: partial search picks the first row with prices")
    M.click(w.g.BrowseButton3)
    sc.eq(ns.ChartItem(), 2589, "legacy: clicking a row switches the chart")
    # Sell: put 20 Runecloth in the sell slot
    M.cursorItem = w.to_lua({"itemID": 14047, "count": 20})
    M.click(w.g.AuctionsItemButton)
    w.settle(0.2)
    sc.eq(ns.ChartItem(), 14047, "legacy: sell item switches the chart")
    rec = ref_recommend(ref, 14047, "1D")
    unit = price_for_frame(rec["listAt"], True)
    btn = None
    auc = w.g.AuctionFrameAuctions
    for i in range(1, len(auc._children) + 1):
        c = auc._children[i]
        if c._otype == "Button" and (c._text or "").startswith("Use List At"):
            btn = c
    sc.check(btn is not None and btn.IsVisible(btn), "legacy: Use List At button visible")
    if btn is not None:
        sc.eq(btn._text, "Use List At: " + money_icons(unit), "legacy: button text")
        w.g.AuctionsStackSizeEntry.SetText(w.g.AuctionsStackSizeEntry, "20")
        auc.priceType = 2
        M.click(btn)
        sc.eq(w.g.BuyoutPrice._copper, unit * 20, "legacy: per-stack buyout")
        sc.eq(w.g.StartPrice._copper, unit * 20, "legacy: per-stack start price")
        auc.priceType = 1
        M.click(btn)
        sc.eq(w.g.BuyoutPrice._copper, unit, "legacy: per-unit buyout")
    sc.eq(len(M.cah.posted), 0, "legacy: nothing posted")
    M.fire("AUCTION_HOUSE_CLOSED")
    w.settle(0.3)
    sc.check(not af._shown and not C.frame.IsShown(C.frame), "legacy: closed and chart hidden")
    sc.check(btn is None or not btn._shown, "legacy: Use button hidden on close")
    sc.clean(w)


def sc_auctionator(sc, ref, items):
    w = World(ref, items, auctionator=True)
    ns, M = w.ns, w.M
    sc.check(ns.AHState.hookedAtr, "Auctionator EventBus hooked at login")
    w.put_bag(0, 1, 14047, 20)
    w.open_ah_modern()
    C = ns.ChartPanel
    A = w.g.Auctionator
    bus = A.EventBus
    row = w.to_lua({"itemKey": {"itemID": 12363, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}})
    bus.Fire(bus, M.atrSource, A.Buying.Events.ShowCommodityBuy, row, w.to_lua({"isCommodity": True}))
    chart_check(sc, w, ref, 12363, "1D", "Auctionator shopping: commodity buy")
    # Selling tab
    sell = w.g.AuctionatorSellingFrame
    sell.Show(sell)
    loc = w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1)
    info = w.lua.table()
    info.itemLink = M.ItemLink(14047)
    info.location = loc
    info.count = 20
    M.atrItem = 14047
    bus.Fire(bus, M.atrSource, A.Selling.Events.BagItemClicked, info)
    w.g.C_AuctionHouse.SendSellSearchQuery(w.to_lua({"itemID": 14047, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}), w.lua.table(), True)
    w.settle(0.4)
    sc.eq(ns.ChartItem(), 14047, "Auctionator selling: bag item switches the chart")
    price_box = sell.SaleItemFrame.Price
    btn = None
    sale = sell.SaleItemFrame
    for i in range(1, len(sale._children) + 1):
        c = sale._children[i]
        if c._otype == "Button" and c._text != "Post":
            btn = c
    sc.check(btn is not None and not btn.IsVisible(btn),
             "Auctionator selling: no button until Auctionator has filled its own price (it would overwrite ours)")
    # Auctionator's search comes back: it fills its undercut, then our button appears
    M.atrResults(14047, 1234)
    sc.check(btn is not None and not btn.IsVisible(btn), "Auctionator selling: waits a moment after its results")
    w.settle(0.5)
    want = price_for_frame(ref_recommend(ref, 14047, "1D")["listAt"], True)
    sc.check(btn is not None and btn.IsVisible(btn), "Auctionator selling: Use List At button visible once ready")
    if btn is not None:
        sc.eq(btn._text, "Use List At: " + money_icons(want), "Auctionator selling: button text")
        rb, rm, rah = w.rect(btn), w.rect(price_box.MoneyInput), w.rect(w.g.AuctionHouseFrame)
        sc.check(rb and rm and abs(rb[0] - rm[0]) < 1e-6 and rb[3] <= rm[1] + 1e-6,
                 "Auctionator selling: button sits under the buyout money box (%s, %s)" % (rb, rm))
        sc.check(rb and rah and rb[2] <= rah[2] + 1e-6, "Auctionator selling: button inside the AH frame (%s)" % (rb,))
        M.click(btn)
        sc.eq(price_box.GetAmount(price_box), want, "Auctionator selling: price set through PriceSelected")
        fired = [M.atrFired[i].name for i in range(1, len(M.atrFired) + 1)]
        sc.check("ForeverAH" in fired, "Auctionator selling: fired as source ForeverAH")
        # Auctionator ignoring PriceSelected (no item yet): we notice and fill the box ourselves
        M.atrItem = None
        price_box.SetAmount(price_box, 777)
        M.click(btn)
        sc.eq(price_box.GetAmount(price_box), want, "Auctionator selling: read back, filled directly when PriceSelected was ignored")
        M.atrItem = 14047
        # with the bid box shown the button moves under it
        bidbox = sale.BidPrice
        bidbox.Show(bidbox)
        ns.AH_RefreshButtons()
        rb, rbid = w.rect(btn), w.rect(bidbox.MoneyInput)
        sc.check(rb and rbid and rb[3] <= rbid[1] + 1e-6, "Auctionator selling: button under the bid box when it is shown")
        bidbox.Hide(bidbox)
    # Auctionator's undercut scan while the Selling tab is hidden must not move the chart
    sell.Hide(sell)
    w.g.C_AuctionHouse.SendSellSearchQuery(w.to_lua({"itemID": 2589, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}), w.lua.table(), True)
    w.settle(0.4)
    sc.eq(ns.ChartItem(), 14047, "Auctionator background sell search ignored")
    w.close_ah_modern()
    sc.check(not C.frame.IsShown(C.frame), "Auctionator: chart hidden on close")
    sc.check(btn is None or not btn._shown, "Auctionator: button hidden on close")
    sc.clean(w)


def sc_horde(sc, ref, items):
    w = World(ref, items, faction="Horde")
    out = "\n".join(w.slash("/fah"))
    sc.check("may not match the auction house you use" in out, "Horde: status warns about the Alliance prices")
    w.slash("/fah chart Runecloth")
    chart_check(sc, w, ref, 14047, "1D", "Horde chart")
    w.M.hardcore = True
    out = "\n".join(w.slash("/fah"))
    sc.check("not Hardcore" in out, "Hardcore note")
    sc.clean(w)


def sc_live_prices(sc, ref, items):
    """Use List At follows the live listings the sell frame loaded, not only the hourly data."""
    w = World(ref, items)
    ns, M = w.ns, w.M
    w.put_bag(0, 1, 14047, 20)
    w.put_bag(0, 2, 2589, 20)
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    csf = ah.CommoditiesSellFrame
    b = find_use_button(csf)
    snap = ref.items[14047]["cheapest"]
    # the cheap stock in the snapshot was bought: the live cheapest is 60% higher
    livep = int(snap * 1.6)
    w.set_commodity_results(14047, [(livep, 50, False), (livep + 40, 10, False)])
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1))
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", 14047)
    w.settle(0.3)
    sc.eq(w.live(14047), livep, "live: cheapest listing read from the loaded results")
    rec = ref_recommend(ref, 14047, "1D", livep)
    want = price_for_frame(rec["listAt"], True)
    sc.check(want >= livep - 1, "live: the advice does not undercut a listing that is no longer there (%s vs live %s)" % (want, livep))
    sc.eq(b._text, "Use List At: " + money_icons(want), "live: button label uses the live price")
    sc.check("cheapest listing right now" in (b.tipBody or ""), "live: tooltip says the live price was used (%s)" % b.tipBody)
    M.click(b)
    sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want, "live: fills exactly the label's price")
    chart_check(sc, w, ref, 14047, "1D", "live chart")
    # the cheapest listing is the player's own: match it, do not undercut yourself
    w.set_commodity_results(14047, [(livep, 5, True)])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", 14047)
    w.settle(0.3)
    rec = ref_recommend(ref, 14047, "1D", livep)
    want_own = livep if rec["listAt"] == livep - 1 else price_for_frame(rec["listAt"], True)
    sc.eq(b._text, "Use List At: " + money_icons(want_own), "own listing: matched, not undercut")
    # timeframe change without live listings: pick a timeframe whose advice differs from 1D
    w.set_commodity_results(14047, [])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", 14047)
    w.settle(0.3)
    base = price_for_frame(ref_recommend(ref, 14047, "1D")["listAt"], True)
    sc.eq(b._text, "Use List At: " + money_icons(base), "no live listing: back to the hourly data")
    other = next((tf for tf in TFS if price_for_frame(ref_recommend(ref, 14047, tf)["listAt"], True) != base), None)
    sc.check(other is not None, "test data has a timeframe with different advice")
    if other:
        ns.SetChartTF(other)
        want_o = price_for_frame(ref_recommend(ref, 14047, other)["listAt"], True)
        sc.eq(b._text, "Use List At: " + money_icons(want_o), "%s: label refreshed on timeframe change" % other)
        M.click(b)
        sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want_o, "%s: fill equals the label" % other)
        ns.SetChartTF("1D")
        sc.eq(b._text, "Use List At: " + money_icons(base), "back to 1D: label follows")
    # timeframe change: label and fill follow the chart's timeframe together
    w.set_commodity_results(14047, [(livep, 50, False)])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", 14047)
    w.settle(0.3)
    ns.SetChartTF("1Y")
    want_y = price_for_frame(ref_recommend(ref, 14047, "1Y", livep)["listAt"], True)
    sc.eq(b._text, "Use List At: " + money_icons(want_y), "1Y: label refreshed on timeframe change")
    M.click(b)
    sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want_y, "1Y: fill equals the label")
    ns.SetChartTF("1D")
    # stale data and no live results yet: no button; live results arrive: button
    w.M.commodityResults[2589] = None
    M.now = ref.updated + 9 * 3600
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 2))
    w.settle(0.3)
    sc.check(not b.IsVisible(b), "stale data with no live listing: no Use List At button")
    lp2 = ref.items[2589]["cheapest"] + 3
    w.set_commodity_results(2589, [(lp2, 100, False)])
    M.fire("COMMODITY_SEARCH_RESULTS_UPDATED", 2589)
    w.settle(0.3)
    want2 = price_for_frame(ref_recommend(ref, 2589, "1D", lp2)["listAt"], True)
    sc.check(b.IsVisible(b) and b._text == "Use List At: " + money_icons(want2),
             "stale data: button comes back once the live listing loads (%s)" % b._text)
    M.now = w.now
    # live prices are only for the open AH
    w.close_ah_modern()
    sc.eq(w.live(14047), None, "live prices dropped when the AH closes")
    sc.eq(len(M.cah.posted), 0, "live: nothing posted")
    sc.clean(w)


def sc_other_house(sc, ref, items):
    """The neutral goblin auction house, the other faction's, and Hardcore are other markets."""
    w = World(ref, items)
    ns, M = w.ns, w.M
    w.put_bag(0, 1, 14047, 20)
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
        r.itemKey = w.to_lua({"itemID": 14047, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0})
        r.minPrice = 1200
        ah.SelectBrowseResult(ah, r)
        chart_check(sc, w, ref, 14047, "1D", label)
        ah.SetPostItem(ah, loc)
        w.settle(0.2)
        b = find_use_button(ah.CommoditiesSellFrame)
        sc.check(b is not None and b.IsVisible(b) != other, "%s: Use List At %s" % (label, "hidden" if other else "shown"))
        w.close_ah_modern()
    M.npc, M.subzone = None, None
    M.hardcore = True
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    ah.SetPostItem(ah, loc)
    w.settle(0.2)
    b = find_use_button(ah.CommoditiesSellFrame)
    sc.check(b is not None and not b.IsVisible(b), "Hardcore: Use List At hidden")
    chart_check(sc, w, ref, 14047, "1D", "Hardcore")
    w.close_ah_modern()
    M.hardcore = False
    sc.clean(w)


def sc_suffix_gear(sc, ref, items):
    """Random-suffix gear shares an item ID: no List At fill, and a note."""
    w = World(ref, items)
    ns, M = w.ns, w.M
    w.M["items"][12363].commodity = False
    w.put_bag(0, 1, 12363, 1, suffix=1034)
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1))
    w.settle(0.2)
    b = find_use_button(ah.ItemSellFrame)
    sc.check(b is not None and not b.IsVisible(b), "suffix item: no Use List At")
    chart_check(sc, w, ref, 12363, "1D", "suffix item chart")
    sc.check("Price covers all versions of this item" in (ns.ChartPanel.notes._text or ""), "suffix item: chart note")
    # the same item without a suffix still gets the button (the note stays: it is about the item ID)
    w.put_bag(0, 2, 12363, 1)
    w.set_commodity_results(12363, [])
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 2))
    w.settle(0.2)
    sc.check(b.IsVisible(b), "plain copy: Use List At shown")
    # live item results feed the item sell button too (whole silver)
    t = w.lua.table()
    t[1] = w.to_lua({"buyout": 2512345, "quantity": 1})
    w.M.itemResults[12363] = t
    M.fire("ITEM_SEARCH_RESULTS_UPDATED", w.to_lua({"itemID": 12363, "itemLevel": 0, "itemSuffix": 0, "battlePetSpeciesID": 0}))
    w.settle(0.3)
    sc.eq(w.live(12363), 2512345, "item results: live cheapest buyout")
    want = price_for_frame(ref_recommend(ref, 12363, "1D", 2512345)["listAt"], False)
    sc.eq(b._text, "Use List At: " + money_icons(want), "item sell button uses the live buyout, rounded to silver")
    w.M["items"][12363].commodity = True
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
    w.put_bag(0, 1, 2589, 20)
    w.open_ah_modern()
    ah = w.g.AuctionHouseFrame
    ah.SetPostItem(ah, w.g.ItemLocation.CreateFromBagAndSlot(w.g.ItemLocation, 0, 1))
    w.settle(0.2)
    csf = ah.CommoditiesSellFrame
    b = find_use_button(csf)
    want = price_for_frame(ref_recommend(ref, 2589, "1D")["listAt"], True, copper_ok=False)
    sc.check(b is not None and b.IsVisible(b), "no copper: Use button visible")
    if b is not None:
        sc.eq(b._text, "Use List At: " + money_icons(want), "no copper: button text in whole silver")
        M.click(b)
        sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want, "no copper: price box gets whole silver")
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


def ref_flips(ref, limit=5, hidden=()):
    """Python model of ns.FlipIdeas: rank by money traded, full advice on the top 60, filter, rank by a day's profit."""
    cand = []
    for iid, it in ref.items.items():
        if it["cheapest"] > 0 and it["market"] > 0 and it["sold24"] >= 15 and it["listed"] >= 2:
            cand.append((-(it["market"] * min(it["sold24"], 200)), iid))
    cand.sort()
    strict, loose = [], []
    for _, iid in cand[:60]:
        if iid in hidden:
            continue
        r = ref_recommend(ref, iid, "1W")
        if (r["history"] and r["margin"] > 0 and r["buy"] > 0 and r["marginPct"] >= 0.03 and not r["far"]
                and not r["vendorBetter"] and r["verdict"] not in ("Sell", "Strong Sell")):
            sold = r["sold24"]
            idea = {"id": iid, "buy": r["buy"], "sell": r["sell"], "profit": r["margin"],
                    "perDay": r["margin"] * min(sold, 200), "sold": sold}
            (strict if sold >= 60 else loose).append(idea)
    strict.sort(key=lambda x: (-x["perDay"], x["id"]))
    loose.sort(key=lambda x: (-x["perDay"], x["id"]))
    return (strict + loose)[:limit]


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
    if iid in ref.items and ref.items[iid]["name"]:
        return ref.items[iid]["name"]
    it = items.get(str(iid))
    return it["name"] if it and it.get("name") else "Item %d" % iid


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

    # Flip ideas match the Python model
    want = ref_flips(ref)
    got = shown_rows(SB, "flipRows")
    sc.eq(len(got), len(want), "flip ideas shown")
    for r, x in zip(got, want):
        lab = "flip %s" % x["id"]
        sc.eq(r.itemID, x["id"], lab + ": item (ranked by a day's profit)")
        sc.eq(strip_codes(r.right._text), "+" + short_money(x["profit"]), lab + ": profit each after the cut")
        sc.eq(strip_codes(r.sub._text), "Buy %s  Sell %s" % (short_money(x["buy"]), short_money(x["sell"])),
              lab + ": buy and sell")
        sc.eq(r.right2._text, "~%s/day" % count_text(x["sold"]), lab + ": sold per day")
        sc.eq(r.name._text, item_name(ref, items, x["id"]), lab + ": name")
    if not want:
        sc.check(SB.flipMsg._shown and "No good flips" in (SB.flipMsg._text or ""), "no ideas: says so")
    else:
        sc.check(not SB.flipMsg._shown, "ideas shown: no message")
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
            v = rec["verdict"] if rec["history"] else None
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
        sc.check(any(f.startswith("Profit each, after the 5% cut") and ("+" + ns.MoneyText(want[0]["profit"])) in f
                     for f in flat), "flip hover: profit line")
        sc.check(any("Click: open it in the auction house" in f for f in flat), "flip hover: says what a click does")
        M.leave(got[0])

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
        w.set_browse([(99901, 500, 1), (target, ref.items[target]["cheapest"], 400)])
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
    extra = next((i for i in sorted(ref.items) if i not in SIDEBAR_WATCH and ref.items[i]["name"]), None)
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
        want2 = ref_flips(ref, hidden={fid})
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
    out = w.slash("/fah watch Runecloth")
    sc.check(any("watching Runecloth" in c or "already on your watchlist" in c for c in out), "/fah watch Runecloth")
    sc.eq(watch_ids(w).count(14047), 1, "/fah watch: on the list once")
    out = w.slash("/fah unwatch runecloth")
    sc.check(any("stopped watching Runecloth" in c for c in out), "/fah unwatch")
    sc.check(14047 not in watch_ids(w), "/fah unwatch removed it")
    out = w.slash("/fah unwatch Runecloth")
    sc.check(any("not on your watchlist" in c for c in out), "/fah unwatch twice")
    out = w.slash("/fah watch Nope Nothing")
    sc.check(any("no price data for Nope Nothing" in c for c in out), "/fah watch unknown")
    out = w.slash("/fah watchlist")
    sc.check(any(("watching %d items:" % len(watch_ids(w))) in c for c in out), "/fah watchlist lists it")
    out = w.slash("/fah watchlist reset")
    sc.eq(watch_ids(w), SIDEBAR_WATCH, "/fah watchlist reset brings back the starter list")
    out = w.slash("/fah flips")
    ideas_now = ref_flips(ref, hidden={fid} if got else ())
    sc.eq(len(out), len(ideas_now) + 1 if ideas_now else 1, "/fah flips prints a line per idea")
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
    w = World(ref, items, saved={"watchlist": [2589, "junk", 14047, 2589, -3], "sidebar": False})
    w.open_ah_modern()
    SB = w.ns.SidebarPanel
    sc.eq(watch_ids(w), [2589, 14047], "saved watchlist is used (bad entries and repeats dropped)")
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
    sc.eq(ns.ItemCount(), len(ref.items), "edge item count")
    for iid, it in ref.items.items():
        g = ns.GetItem(iid)
        for k in ("name", "cheapest", "market", "listed", "sold24", "avg7", "vendor"):
            sc.eq(g[k], it[k], "edge GetItem(%d).%s" % (iid, k))
        sc.eq(ns.FindItemID(it["name"]), iid, "edge FindItemID(%r)" % it["name"])
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
            if any(v is not None for v in win["cheap"] + win["market"]):
                chart_check(sc, w, ref, iid, tf, label)
                for idx in sorted({0, win["n"] // 2, win["n"] - 1}):
                    hover_check(sc, w, ref, iid, tf, idx, label)
            else:
                want = ref_recommend(ref, iid, tf)
                sc.check(C.msg._shown and C.msg._text == "Nothing was listed in this timeframe. Try a longer one.",
                         "%s: empty window message (%s)" % (label, C.msg._text))
                sc.eq(C.verdict._text, want["verdict"], "%s: verdict" % label)
                sc.eq(C.buyBox.value._text, money_icons(want["buy"]) if want["buy"] > 0 else "-", "%s: buy box" % label)
                sc.eq(C.listBox.value._text, money_icons(want["listAt"]) if want["listAt"] > 0 else "-", "%s: list box" % label)
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
        b = find_use_button(csf)
        want = price_for_frame(ref_recommend(ref, iid, "1D")["listAt"], True)
        if want:
            sc.check(b is not None and b.IsVisible(b) and b._text == "Use List At: " + money_icons(want),
                     "edge %d: Use button shows %s (%s)" % (iid, want, b and b._text))
            if b is not None and b.IsVisible(b):
                M.click(b)
                sc.eq(csf.PriceInput.GetAmount(csf.PriceInput), want, "edge %d: Use button fills the price" % iid)
        else:
            sc.check(b is None or not b.IsVisible(b), "edge %d: no Use button without advice" % iid)
    w.close_ah_modern()
    sc.clean(w)



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
]


def ensure_data():
    if os.path.exists(DATA_FILE):
        return True
    if not os.path.exists(SIM):
        print("Simulated data file is missing and sim_fetch.py was not found:", SIM)
        return False
    print("Generating simulated data with sim_fetch.py ...")
    subprocess.run([sys.executable, SIM], check=False)
    return os.path.exists(DATA_FILE)


def main(argv):
    verbose = "-v" in argv
    only = [a for a in argv if not a.startswith("-")]
    if not ensure_data():
        return 2
    import json
    items = json.load(open(os.path.join(LOCAL_DIR, "items.json"), encoding="utf-8"))
    ref = RefData(DATA_FILE)
    print("Data: %d items, updated %d (SIMULATED test data, not real prices)" % (len(ref.items), ref.updated))
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

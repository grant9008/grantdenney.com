"""Offline tests for the Goblin Ledger chart panel (Chart.lua) and auction house
integration (AH.lua).

Runs the real addon files inside Lua 5.1 (lupa) against a small mock of the
WoW widget API. Every scenario gets a fresh Lua state.

    python tools/wow/addon_test/test_chart.py

Exit code 0 means every check passed.
"""
import os
import sys

from lupa import lua51

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
ADDON = os.path.join(ROOT, "wow-addon", "GoblinLedger")
LOCAL_DATA = os.path.join(ROOT, "goblin-ledger", "prices", "_localdata", "ForeverAH_Data.lua")

# ---------------------------------------------------------------------------
# Mock WoW API (Lua)
# ---------------------------------------------------------------------------
MOCK = r'''
MOCK = { results = {}, chat = {}, timers = {}, events = {}, unknownEvents = {},
         created = { frame = 0, texture = 0, line = 0, fontstring = 0 },
         cursorX = 0, cursorY = 0, now = 1794936946, atrFired = {}, loaded = {} }
local M = MOCK

function check(cond, msg)
  M.results[#M.results + 1] = { ok = cond and true or false, msg = tostring(msg), section = M.section or "?" }
end
function section(name) M.section = name end

local function finite(v) return type(v) == "number" and v == v and v ~= math.huge and v ~= -math.huge end
M.finite = finite

local Widget = {}
local WidgetMT = { __index = function(t, k)
  if k == "CreateLine" and M.noLines then return nil end
  return Widget[k]
end }

local function fire(self, name, ...)
  local s = self._scripts and self._scripts[name]
  if s then s(self, ...) end
  local hs = self._hooks and self._hooks[name]
  if hs then for _, h in ipairs(hs) do h(self, ...) end end
end
M.fireScript = fire

local function new(kind, parent, name)
  local o = setmetatable({ _kind = kind, _parent = parent, _points = {}, _shown = true, _w = 0, _h = 0,
                           _scripts = {}, _scale = 1, _children = {} }, WidgetMT)
  if type(parent) == "table" and rawget(parent, "_children") then table.insert(parent._children, o) end
  M.created[kind] = (M.created[kind] or 0) + 1
  if name then _G[name] = o; o._name = name end
  return o
end
M.new = new

function Widget:SetPoint(point, rel, relPoint, x, y)
  if type(rel) == "number" then x, y, rel, relPoint = rel, relPoint, nil, nil end
  assert(type(point) == "string", "SetPoint: point must be a string")
  if x ~= nil then assert(finite(x), "SetPoint: bad x " .. tostring(x)) end
  if y ~= nil then assert(finite(y), "SetPoint: bad y " .. tostring(y)) end
  table.insert(self._points, { point, rel, relPoint or point, x or 0, y or 0 })
end
function Widget:ClearAllPoints() self._points = {} end
function Widget:GetPoint(i) local p = self._points[i or 1]; if p then return p[1], p[2], p[3], p[4], p[5] end end
function Widget:GetNumPoints() return #self._points end
function Widget:SetAllPoints(rel)
  rel = rel or self._parent
  self._points = { { "TOPLEFT", rel, "TOPLEFT", 0, 0 }, { "BOTTOMRIGHT", rel, "BOTTOMRIGHT", 0, 0 } }
end
local function sizeChanged(self, old)
  if old ~= self._w and self._scripts.OnSizeChanged then
    M.pendingSize = M.pendingSize or {}
    M.pendingSize[self] = true
  end
end
function Widget:SetSize(w, h)
  assert(finite(w) and finite(h) and w >= 0 and h >= 0, "SetSize: bad size " .. tostring(w) .. "x" .. tostring(h))
  local old = self._w; self._w, self._h = w, h; sizeChanged(self, old)
end
function Widget:SetWidth(w) assert(finite(w) and w >= 0, "SetWidth: bad " .. tostring(w)); local old = self._w; self._w = w; sizeChanged(self, old) end
function Widget:SetHeight(h) assert(finite(h) and h >= 0, "SetHeight: bad " .. tostring(h)); self._h = h end
function Widget:GetWidth() return self._w end
function Widget:GetHeight() return self._h end
function Widget:GetSize() return self._w, self._h end
function Widget:Show() if not self._shown then self._shown = true; fire(self, "OnShow") end end
function Widget:Hide() if self._shown then self._shown = false; fire(self, "OnHide") end end
function Widget:SetShown(v) if v then self:Show() else self:Hide() end end
function Widget:IsShown() return self._shown end
function Widget:IsVisible()
  if not self._shown then return false end
  local p = self._parent
  if type(p) == "table" and p.IsVisible then return p:IsVisible() end
  return true
end
function Widget:SetScript(name, fn) assert(fn == nil or type(fn) == "function", "SetScript: bad handler"); self._scripts[name] = fn end
function Widget:GetScript(name) return self._scripts[name] end
function Widget:HookScript(name, fn)
  self._hooks = self._hooks or {}
  self._hooks[name] = self._hooks[name] or {}
  table.insert(self._hooks[name], fn)
end
function Widget:SetFrameStrata(s) self._strata = s end
function Widget:SetFrameLevel(l) assert(finite(l), "SetFrameLevel: bad"); self._level = l end
function Widget:GetFrameLevel() return self._level or 1 end
function Widget:SetClampedToScreen(v) end
function Widget:EnableMouse(v) self._mouse = v end
function Widget:SetMovable(v) self._movable = v end
function Widget:RegisterForDrag(...) end
function Widget:StartMoving() assert(self._movable, "StartMoving on a frame that is not movable"); self._moving = true end
function Widget:StopMovingOrSizing() self._moving = false; if M.dragTo then self._points = { M.dragTo } end end
function Widget:SetUserPlaced(v) end
function Widget:SetDontSavePosition(v) end
function Widget:SetBackdrop(b) self._backdrop = b end
function Widget:SetBackdropColor(r, g, b, a) assert(finite(r) and finite(g) and finite(b)) end
function Widget:SetBackdropBorderColor(r, g, b, a) assert(finite(r) and finite(g) and finite(b)) end
function Widget:SetScale(s) assert(finite(s) and s > 0, "SetScale: bad " .. tostring(s)); self._scale = s end
function Widget:GetScale() return self._scale end
function Widget:GetEffectiveScale()
  local p = self._parent
  return self._scale * ((type(p) == "table" and p.GetEffectiveScale and p:GetEffectiveScale()) or 1)
end
function Widget:GetLeft() return self._left end
function Widget:GetRight() return self._right end
function Widget:GetTop() return self._top end
function Widget:GetBottom() return self._bottom end
function Widget:CreateTexture(name, layer, template, sub)
  assert(sub == nil or (sub >= -8 and sub <= 7), "CreateTexture: bad sublevel " .. tostring(sub))
  local t = new("texture", self, name); t._layer = layer; t._sub = sub; return t
end
function Widget:CreateFontString(name, layer, font) local fs = new("fontstring", self, name); fs._font = font; return fs end
function Widget:CreateLine(name, layer, template, sub)
  assert(sub == nil or (sub >= -8 and sub <= 7), "CreateLine: bad sublevel")
  local l = new("line", self, name); l._layer = layer; return l
end
function Widget:RegisterEvent(e)
  if M.unknownEvents[e] then error('Attempt to register unknown event "' .. e .. '"') end
  M.events[e] = M.events[e] or {}
  table.insert(M.events[e], self)
end
function Widget:UnregisterEvent(e) end
function Widget:SetText(t) self._text = t end
function Widget:GetText() return self._text end
function Widget:SetFontString(fs) self._fs = fs end
function Widget:GetFontString() return self._fs end
function Widget:SetTextColor(r, g, b, a) assert(finite(r) and finite(g) and finite(b), "SetTextColor: bad"); self._color = { r, g, b, a } end
function Widget:SetJustifyH(j) end
function Widget:SetJustifyV(j) end
function Widget:SetWordWrap(v) end
function Widget:GetStringWidth() return #(self._text or "") * 6 end
function Widget:SetTexture(p) self._tex = p end
function Widget:SetColorTexture(r, g, b, a)
  assert(finite(r) and finite(g) and finite(b) and (a == nil or finite(a)), "SetColorTexture: bad"); self._color = { r, g, b, a }
end
function Widget:SetVertexColor(r, g, b, a)
  assert(finite(r) and finite(g) and finite(b) and (a == nil or finite(a)), "SetVertexColor: bad"); self._vcolor = { r, g, b, a }
end
function Widget:SetTexCoord(...) end
function Widget:SetAlpha(a) end
function Widget:SetThickness(t) self._thick = t end
function Widget:SetStartPoint(p, rel, x, y) assert(finite(x) and finite(y), "SetStartPoint: bad"); self._start = { p, rel, x, y } end
function Widget:SetEndPoint(p, rel, x, y) assert(finite(x) and finite(y), "SetEndPoint: bad"); self._finish = { p, rel, x, y } end
function Widget:SetEnabled(v) end
function Widget:GetParent() return self._parent end
function Widget:GetName() return self._name end
function Widget:IsForbidden() return false end
function Widget:IsMouseOver() return false end

function CreateFrame(ftype, name, parent, template)
  local f = new("frame", parent, name)
  f._ftype = ftype
  f._template = template
  return f
end

function hooksecurefunc(a, b, c)
  local tbl, name, fn
  if type(a) == "string" then tbl, name, fn = _G, a, b else tbl, name, fn = a, b, c end
  local orig = tbl[name]
  assert(type(orig) == "function", "hooksecurefunc: no function " .. tostring(name))
  rawset(tbl, name, function(...)
    local r = { orig(...) }
    fn(...)
    return unpack(r)
  end)
end

C_Timer = { After = function(sec, fn) assert(type(fn) == "function", "C_Timer.After: no function"); table.insert(M.timers, fn) end }
function M.RunTimers()
  for _ = 1, 50 do
    local ps = M.pendingSize; M.pendingSize = nil
    if ps then for f in pairs(ps) do fire(f, "OnSizeChanged", f._w, f._h) end end
    local list = M.timers; M.timers = {}
    if #list == 0 and not ps then return end
    for _, fn in ipairs(list) do fn() end
  end
end

function M.Fire(e, ...)
  for _, f in ipairs(M.events[e] or {}) do fire(f, "OnEvent", e, ...) end
end

function M.FindChild(parent, pred)
  for _, c in ipairs(parent._children or {}) do
    if pred(c) then return c end
  end
end

UIParent = new("frame", nil, "UIParent")
UIParent._w, UIParent._h = 1365, 768
UISpecialFrames = {}
BackdropTemplateMixin = {}
date = os.date
time = os.time
GetServerTime = function() return M.now end
GetTime = function() return 1000 end
GetCursorPosition = function() return M.cursorX, M.cursorY end
SetCursor = function(c) M.cursor = c; return true end
ResetCursor = function() M.cursor = nil end
issecretvalue = function(v) return false end
UnitFactionGroup = function() return M.faction or "Alliance", M.faction or "Alliance" end
IsShiftKeyDown = function() return false end
SlashCmdList = {}
DEFAULT_CHAT_FRAME = { AddMessage = function(self, m) table.insert(M.chat, tostring(m)) end }
TooltipDataProcessor = { AddTooltipPostCall = function() end }
Enum = { ItemCommodityStatus = { Unknown = 0, Item = 1, Commodity = 2 }, TooltipDataType = { Item = 0 } }
C_AddOns = { IsAddOnLoaded = function(n) return M.loaded[n] or false end }

GameTooltip = CreateFrame("GameTooltip", "GameTooltip", UIParent)
GameTooltip._shown = false
GameTooltip._lines = {}
function GameTooltip:SetOwner(owner, anchor) self._owner = owner; self._lines = {}; self._anchor = anchor end
function GameTooltip:GetOwner() return self._owner end
function GameTooltip:AddLine(t, ...) table.insert(self._lines, tostring(t)) end
function GameTooltip:AddDoubleLine(l, r, ...)
  for i = 1, select("#", ...) do assert(finite((select(i, ...))), "AddDoubleLine: bad colour") end
  table.insert(self._lines, tostring(l) .. " = " .. tostring(r))
end
function GameTooltip:ClearLines() self._lines = {} end
function GameTooltip:Hide() self._shown = false; self._owner = nil; self._lines = {} end
function GameTooltip:Show() self._shown = true end

C_Item = {
  GetItemIconByID = function(id) return 100000 + id end,
  GetItemQualityByID = function(id) return 1 end,
  GetItemQualityColor = function(q) return 1, 1, 1, "ffffffff" end,
  GetItemNameByID = function(id) local f = M.fixtures and M.fixtures[id]; return f and f.name or nil end,
  GetItemInfo = function(id) local f = M.fixtures and M.fixtures[id]; if f then return f.name, nil, 1 end end,
  GetItemID = function(loc) return loc and loc.itemID end,
  GetItemLink = function(loc) return loc and ("|Hitem:" .. loc.itemID .. "::|h[x]|h") end,
}
C_AuctionHouse = {
  SendBrowseQuery = function(q) M.lastBrowse = q end,
  SendSellSearchQuery = function(key, sorts, sep) M.lastSellSearch = key end,
  SendSearchQuery = function() end,
  GetBrowseResults = function() return M.browseResults or {} end,
  GetItemKeyInfo = function(key)
    local f = M.fixtures and M.fixtures[key.itemID]
    return { isCommodity = not (f and f.commodity == false) }
  end,
  SupportsCopperValues = function() return M.copper ~= false end,
  GetItemCommodityStatus = function(loc) return loc.commodity and 2 or 1 end,
}

function M.LoadSource(src, name, ns)
  local chunk, err = loadstring(src, "@" .. name)
  if not chunk then error(err) end
  chunk("GoblinLedger", ns)
end

function M.RunScenario(code)
  local f, err = loadstring(code, "=scenario")
  if not f then check(false, "scenario does not compile: " .. tostring(err)); return end
  local ok, e = xpcall(f, debug.traceback)
  if not ok then check(false, "scenario crashed: " .. tostring(e)) end
end

function M.Report()
  local out = {}
  for _, r in ipairs(M.results) do
    out[#out + 1] = (r.ok and "1" or "0") .. "\t" .. r.section .. "\t" .. r.msg:gsub("\n", " / ")
  end
  return table.concat(out, "\n")
end

function M.Texts(pool)
  local t = {}
  for i = 1, pool.used do t[i] = pool.items[i]:GetText() end
  return t
end

-------------------------------------------------------------------------------
-- Stub of the shared ns API (Core.lua / Recommend.lua are tested separately)
-------------------------------------------------------------------------------
function M.MakeStubNS(opts)
  opts = opts or {}
  local floor = math.floor
  local ns = {}
  ns.COLORS = { buy = { 0.898, 0.722, 0.259 }, sell = { 0.22, 0.74, 0.97 }, pos = { 0.25, 0.80, 0.45 },
    neg = { 0.90, 0.33, 0.33 }, muted = { 0.55, 0.51, 0.45 }, text = { 0.90, 0.88, 0.84 },
    bg = { 0.043, 0.055, 0.078, 0.96 }, panel = { 0.07, 0.08, 0.10, 0.96 } }
  ns.settings = { tooltip = true, chart = true, chartTF = "1D", compact = false }
  local HOUR, DAY = 3600, 86400
  local hourEnd, dayEnd = 1794934800, 1794873600
  local items = {
    [2589] = { name = "Linen Cloth", base = 45, kind = "wave" },
    [14047] = { name = "Runecloth", base = 1200, kind = "wave" },
    [12363] = { name = "Arcane Crystal", base = 780000, kind = "wave" },
    [1] = { name = "One Point Item", base = 500, kind = "one" },
    [2] = { name = "Flat Item", base = 300, kind = "flat" },
    [3] = { name = "Gap Item", base = 2500, kind = "gaps" },
    [4] = { name = "Empty Window Item", base = 100, kind = "empty" },
    [5] = { name = "Expensive Item", base = 1534567, kind = "wave" },
    [6] = { name = "Gear Item", base = 123456, kind = "wave", commodity = false },
    [7] = { name = "Squire's Shirt", base = 900, kind = "wave" },
    [8] = { name = "Squire's Shirt", base = 950, kind = "wave" },
    [9] = { name = "New Item", base = 700, kind = "nohist" },
    [10] = { name = "Tiny Range Item", base = 1500000, kind = "tiny" },
    [11] = { name = "Thin Item", base = 800, kind = "thin" },
  }
  M.fixtures = items
  if opts.nodata then
    ns.db = { v = 1, updated = 0, items = {} }
  else
    ns.db = { v = 1, updated = 1794936946, region = "us", house = 110, houseName = "Alliance Auction House",
              ruleset = "PvP", hourEnd = hourEnd, dayEnd = dayEnd, items = {} }
    for id in pairs(items) do ns.db.items[id] = "stub" end
  end
  local TFN = { ["1D"] = { 24, HOUR }, ["5D"] = { 120, HOUR }, ["1W"] = { 168, HOUR }, ["1M"] = { 30, DAY }, ["1Y"] = { 365, DAY } }
  function ns.GetItem(id)
    if opts.nodata then return nil end
    local it = items[id]
    if not it then return nil end
    return { id = id, name = it.name, cheapest = it.base, market = floor(it.base * 1.04), listed = 900,
             sold24 = 120, avg7 = it.base, vendor = 10 }
  end
  function ns.GetSeries(id, tf)
    if opts.nodata then return nil end
    local it = items[id]
    if not it then return nil end
    local spec = TFN[tf]
    assert(spec, "GetSeries: bad timeframe " .. tostring(tf))
    M.seriesCalls = (M.seriesCalls or 0) + 1
    local n, step = spec[1], spec[2]
    if it.kind == "one" then n = 1 end
    local endT = (step == HOUR) and hourEnd or dayEnd
    local s = { n = n, step = step, times = {}, cheap = {}, market = {}, sold = {} }
    for k = 1, n do
      s.times[k] = endT - (n - k) * step
      local c, m
      if it.kind == "flat" then
        c, m = it.base, it.base
      elseif it.kind == "tiny" then
        -- over 100g, all within 45 copper
        c = it.base + (k % 5) * 10
        m = c + 5
      elseif it.kind == "empty" then
        c, m = false, false
      else
        local w = math.sin(k / 5) * 0.08 + math.cos(k / 17) * 0.05
        c = floor(it.base * (1 + w) + 0.5)
        m = floor(c * 1.05 + 0.5)
        if it.kind == "gaps" and k % 3 == 0 then c, m = false, false end
        if it.kind == "gaps" and k % 7 == 0 then c = false end
      end
      s.cheap[k], s.market[k] = c, m
      s.sold[k] = c and ((k % 4) * 3) or 0
    end
    return s
  end
  function ns.Recommend(id, tf)
    local it = ns.GetItem(id)
    if not it then return nil end
    local b = it.cheapest
    local kind = items[id].kind
    if kind == "nohist" or kind == "thin" then
      -- Recommend.lua's no-advice shape: no targets, a Hold, and a note saying why
      return { buy = 0, sell = 0, listAt = 0, margin = 0, marginPct = 0, verdict = "Hold", score = 0, pos = 0.5,
               lo = b, hi = b, cheapestNow = it.cheapest, marketNow = it.market, sold24 = it.sold24,
               history = kind ~= "nohist", advice = false, usual = (kind == "thin") and it.market or nil,
               thinListings = kind == "thin",
               notes = { kind == "nohist" and "Not enough price history yet" or "Only 2 listings: price may not be reliable" } }
    end
    local flat = kind == "flat"
    local buy = flat and b or floor(b * 0.95)
    local listAt = flat and b or (b + floor(b * 0.03) + 7) -- odd copper on purpose
    if kind == "tiny" then buy, listAt = b, b + 20 end
    local margin = floor(listAt * 0.95) - buy
    -- Runecloth's usual price is 20s: its 12s listing is a dump (Sell now under 70% of it)
    return { buy = buy, sell = listAt, listAt = listAt, margin = margin, marginPct = buy > 0 and margin / buy or 0,
             verdict = flat and "Hold" or "Low now: still falling", score = flat and 0 or 40, pos = 0.3, lo = b * 0.9, hi = b * 1.1,
             cheapestNow = it.cheapest, marketNow = it.market, sold24 = it.sold24, history = true, advice = true,
             usual = (id == 14047) and 2000 or it.market,
             notes = flat and { "Spread does not beat the 5% AH cut" } or {} }
  end
  -- Recommend.lua's ns.SellPrices rules on the stub's advice (never the
  -- timeframe): Sell now 1 copper under the cheapest (live) listing, matched
  -- when it is yours; at = Sell now, or the usual price when Sell now is under
  -- 70% of it (under = how far), or with nothing listed.
  function ns.SellPrices(id, tf, live, own)
    local rec = ns.Recommend(id, "1D")
    if not rec then return nil end
    local isLive = type(live) == "number" and live > 0
    local cheap = isLive and live or rec.cheapestNow
    local out = { rec = rec, live = isLive, own = isLive and own == true, usual = rec.usual }
    if cheap > 1 then
      out.now = out.own and cheap or (cheap - 1)
      if rec.usual and out.now * 100 < rec.usual * 70 then
        out.at, out.under = rec.usual, floor((rec.usual - out.now) * 100 / rec.usual)
      else
        out.at = out.now
      end
    else
      out.nowWhy = "none"
      out.at = rec.usual
    end
    return out
  end
  -- name -> ids, lowest first (Core.lua's index: a name can belong to several items)
  local names = {}
  for id, it in pairs(items) do
    local key = it.name:lower()
    names[key] = names[key] or {}
    table.insert(names[key], id)
  end
  for _, list in pairs(names) do table.sort(list) end
  function ns.FindItemID(name)
    if type(name) ~= "string" then return nil end
    local list = names[name:lower()]
    return list and list[1] or nil
  end
  function ns.FindItemIDs(name)
    local out = {}
    for i, id in ipairs(type(name) == "string" and names[name:lower()] or {}) do out[i] = id end
    return out
  end
  function ns.MoneyText(c)
    c = floor(c + 0.5)
    local neg = c < 0
    if neg then c = -c end
    local g, s, k = floor(c / 10000), floor(c / 100) % 100, c % 100
    local p = {}
    if g > 0 then p[#p + 1] = g .. "g" end
    if s > 0 then p[#p + 1] = s .. "s" end
    if k > 0 and g < 100 then p[#p + 1] = k .. "c" end
    local out = #p > 0 and table.concat(p, " ") or "0c"
    return neg and ("-" .. out) or out
  end
  ns.MoneyIcons = ns.MoneyText
  function ns.DataAge() if opts.nodata then return nil end return 1800 end
  -- Core.lua's ns.MarketMismatch, reduced to the faction check
  function ns.MarketMismatch()
    local f = UnitFactionGroup and UnitFactionGroup("player")
    if f and f ~= "Alliance" and f ~= "Neutral" then return "Prices are from the Alliance Auction House" end
    return nil
  end
  function ns.Print(m) table.insert(M.chat, tostring(m)) end
  return ns
end

-------------------------------------------------------------------------------
-- Auction house mocks
-------------------------------------------------------------------------------
function M.MakeModernAH()
  local f = CreateFrame("Frame", "AuctionHouseFrame", UIParent)
  f:SetSize(800, 538)
  f:Hide()
  f._left, f._right, f._top, f._bottom = 16, 816, 652, 300
  function f:SendBrowseQuery(searchString)
    C_AuctionHouse.SendBrowseQuery({ searchString = searchString, sorts = {}, filters = {} })
  end
  function f:SelectBrowseResult(r)
    local info = C_AuctionHouse.GetItemKeyInfo(r.itemKey)
    if info.isCommodity then
      self.CommoditiesBuyFrame:SetItemIDAndPrice(r.itemKey.itemID, r.minPrice)
    else
      self.ItemBuyFrame:SetItemKey(r.itemKey)
    end
  end
  f.CommoditiesBuyFrame = CreateFrame("Frame", nil, f)
  function f.CommoditiesBuyFrame:SetItemIDAndPrice(id, price) self.itemID = id end
  f.ItemBuyFrame = CreateFrame("Frame", nil, f)
  function f.ItemBuyFrame:SetItemKey(key) self.itemKey = key end
  local function sell(name)
    local sf = CreateFrame("Frame", nil, f)
    sf.PostButton = CreateFrame("Button", nil, sf, "UIPanelButtonTemplate")
    sf.PostButton:SetFrameLevel(350)
    sf.PriceInput = { amount = 0 }
    function sf.PriceInput:SetAmount(a) assert(type(a) == "number" and a == math.floor(a), "SetAmount: whole copper only"); self.amount = a end
    function sf.PriceInput:GetAmount() return self.amount end
    function sf:SetItem(loc) self.loc = loc; self.PriceInput:SetAmount(loc and 100 or 0) end
    function sf:GetItem() return self.loc end
    function sf:UpdatePostState() self.posts = (self.posts or 0) + 1 end
    f[name] = sf
  end
  sell("CommoditiesSellFrame")
  sell("ItemSellFrame")
  function f:SetPostItem(loc)
    self.CommoditiesSellFrame:SetItem(nil)
    self.ItemSellFrame:SetItem(nil)
    if loc.commodity then self.CommoditiesSellFrame:SetItem(loc) else self.ItemSellFrame:SetItem(loc) end
  end
  return f
end

function M.MakeAuctionator()
  Auctionator = {
    Buying = { Events = { ShowCommodityBuy = "buy show commodity", ShowItemBuy = "buy show item" } },
    Selling = { Events = { BagItemClicked = "bag_item_clicked", SellSearchStart = "sell_search_start",
                           PriceSelected = "price_selected" } },
    EventBus = { listeners = {}, sources = {} },
  }
  local bus = Auctionator.EventBus
  function bus:Register(l, evs)
    assert(type(l.ReceiveEvent) == "function", "listener needs ReceiveEvent")
    for _, e in ipairs(evs) do
      self.listeners[e] = self.listeners[e] or {}
      table.insert(self.listeners[e], l)
    end
    return self
  end
  function bus:RegisterSource(s, n) self.sources[s] = n; return self end
  function bus:Fire(src, e, ...)
    assert(self.sources[src], "All sources must be registered")
    table.insert(M.atrFired, { event = e, args = { ... } })
    for _, l in ipairs(self.listeners[e] or {}) do l:ReceiveEvent(e, ...) end
  end
  local f = CreateFrame("Frame", "AuctionatorSellingFrame", UIParent)
  f.SaleItemFrame = CreateFrame("Frame", nil, f)
  f.SaleItemFrame.Price = CreateFrame("Frame", nil, f.SaleItemFrame)
  function f.SaleItemFrame.Price:SetAmount(a) self.amount = a end
end

function M.MakeLegacyAH()
  local f = CreateFrame("Frame", "AuctionFrame", UIParent)
  f:SetSize(832, 447)
  f:Hide()
  f._left, f._right, f._top, f._bottom = 0, 832, 744, 340
  local auc = CreateFrame("Frame", "AuctionFrameAuctions", f)
  auc.priceType = 2
  CreateFrame("Frame", "BuyoutPrice", auc)
  CreateFrame("Frame", "StartPrice", auc)
  CreateFrame("Button", "AuctionsCreateAuctionButton", auc)
  local entry = CreateFrame("EditBox", "AuctionsStackSizeEntry", auc)
  function entry:GetNumber() return M.stack or 5 end
  MoneyInputFrame_SetCopper = function(frame, c) assert(type(c) == "number"); frame.copper = c end
  QueryAuctionItems = function(name, ...) M.lastQuery = name end
  SetSelectedAuctionItem = function(t, i) M.selected = i end
  M.legacyList = { "|cffffffff|Hitem:12363::::::::60:::::|h[Arcane Crystal]|h|r", "|Hitem:2589::|h[Linen Cloth]|h" }
  GetNumAuctionItems = function(t) return #M.legacyList, #M.legacyList end
  GetAuctionItemLink = function(t, i) return M.legacyList[i] end
  GetAuctionSellItemInfo = function() if M.legacySell then return unpack(M.legacySell) end end
  return f
end
'''

# ---------------------------------------------------------------------------
# Scenarios (Lua). Each runs with globals: TESTNS (the ns table), MOCK.
# ---------------------------------------------------------------------------
CHART_TEST = r'''
local ns, M = TESTNS, MOCK
local function distinct(list) for i = 2, #list do if list[i] == list[i - 1] then return false end end return true end

section("chart: normal item, standalone")
ns.ShowChart(2589, "slash")
M.RunTimers()
local F = ForeverAHChartFrame
local C = ns.ChartPanel
check(F ~= nil, "chart frame exists")
check(F:IsShown(), "chart shown")
check(C.lastError == nil, "no draw error: " .. tostring(C.lastError))
check(not C.docked and C.placement == "float", "standalone when the AH is closed")
local p, rel = F:GetPoint(1)
check(p == "CENTER" and rel == UIParent, "default standalone position")
check(F:GetWidth() == 800 and F:GetHeight() == 260, "800 x 260")
check(C.name:GetText() == "Linen Cloth", "header name")
check(C.icon._tex == 100000 + 2589, "header icon")
check(C.price:GetText() == ns.MoneyText(math.floor(45 * 1.04)), "header market price " .. tostring(C.price:GetText()))
local rec = ns.Recommend(2589, "1D")
check(C.buyBox.value:GetText() == ns.MoneyText(rec.buy), "Target Buy value " .. tostring(C.buyBox.value:GetText()))
local spl = ns.SellPrices(2589)
check(C.listBox.value:GetText() == ns.MoneyText(spl.at), "Sell at value: the sell price, " .. tostring(C.listBox.value:GetText()))
check(C.buyBox.sub:GetText() == "Cheapest: " .. ns.MoneyText(45), "cheapest label " .. tostring(C.buyBox.sub:GetText()))
check(C.listBox.sub:GetText() == "Usual: " .. ns.MoneyText(46), "usual label " .. tostring(C.listBox.sub:GetText()))
local s = ns.GetSeries(2589, "1D")
local nn = 0
for i = 1, s.n do if s.cheap[i] then nn = nn + 1 end if s.market[i] then nn = nn + 1 end end
check(C.pools.dot.used == nn, "one dot per price: " .. C.pools.dot.used .. " vs " .. nn)
check(C.pools.trend.used == s.n - 1 and C.pools.casing.used == s.n - 1, "trend line segments " .. C.pools.trend.used)
check(C.pools.ring.used == 2, "period high and low rings")
check(C.pools.tag.used == 0 and C.pools.dash.used == 0, "no dashed target line until a box is clicked")
check(C.buyBox._ftype == "Button" and C.listBox._ftype == "Button", "the boxes are buttons")
check(not C.buyBox.lit and not C.listBox.lit, "no box lit at first")
M.fireScript(C.buyBox, "OnEnter")
check(M.cursor == "INTERACT_CURSOR", "a hand cursor over a box: " .. tostring(M.cursor))
local function ttHas(text)
  for _, l in ipairs(GameTooltip._lines) do if l:find(text, 1, true) then return true end end
  return false
end
check(GameTooltip._owner == C.buyBox and GameTooltip._lines[1] == "Target Buy", "box tooltip title")
check(ttHas("Click to show the gold Target Buy line on the chart."), "box tooltip says what a click does")
M.fireScript(C.buyBox, "OnClick")
check(ns.settings.chartLine == "buy" and C.buyBox.lit and not C.listBox.lit, "Target Buy click: its line on, its box lit")
local tags = M.Texts(C.pools.tag)
check(#tags == 1 and tags[1]:find("^Target Buy ") ~= nil, "only the Target Buy line: " .. table.concat(tags, ", "))
check(C.pools.dash.used > 20 and C.pools.dash.items[1]._color[1] == ns.COLORS.buy[1], "gold dashes")
check(C.buyBox.edges[1]:IsShown() and not C.listBox.edges[1]:IsShown(), "the lit box has a border")
check(C.buyBox.bg._color[1] > C.listBox.bg._color[1] + 0.05, "and a brighter background")
check(ttHas("Click to hide the gold Target Buy line."), "the tooltip now says a click hides it")
M.fireScript(C.buyBox, "OnLeave")
check(M.cursor == nil and not GameTooltip._shown, "cursor and tooltip back to normal on leave")
M.fireScript(C.listBox, "OnClick")
check(ns.settings.chartLine == "sell" and C.listBox.lit and not C.buyBox.lit, "Sell at click switches lines")
tags = M.Texts(C.pools.tag)
check(#tags == 1 and tags[1]:find("^Sell at ") ~= nil, "only the Sell at line: " .. table.concat(tags, ", "))
check(C.pools.dash.items[1]._color[1] == ns.COLORS.sell[1], "blue dashes")
check(not C.buyBox.edges[1]:IsShown() and C.listBox.edges[1]:IsShown(), "the border moved")
M.fireScript(C.listBox, "OnClick")
check(ns.settings.chartLine == nil and C.pools.tag.used == 0 and C.pools.dash.used == 0 and not C.listBox.lit,
  "clicking the lit box hides the line")
ns.SetChartLine("sell")
check(ns.ChartLine() == "sell" and C.pools.tag.used == 1, "SetChartLine")
ns.SetChartLine("both")
check(ns.ChartLine() == nil and C.pools.tag.used == 0, "SetChartLine with a bad value: no line")
check(C.pools.vol.used > 0, "est. sold bars")
check(C.pools.xlab.used >= 3, "x labels")
check(C.pools.ylab.used >= 2, "y labels")
local yl = M.Texts(C.pools.ylab)
check(distinct(yl), "y labels differ: " .. table.concat(yl, ", "))
check(C.verdict:GetText() == "Low now: still falling", "verdict words: low, never a buy")
check(C.verdict._color[1] == ns.COLORS.text[1], "a low call is plain, not a buy colour")
check(C.meter:IsShown(), "meter shown")
local _, _, _, nx = C.meter.needle:GetPoint(1)
check(math.abs(nx - (140 / 200 * 99)) < 0.01, "needle at score")
-- the margin between the boxes: Sell at after the cut, less Target Buy (the % only near Target Buy)
local mg = math.floor(spl.at * 0.95) - rec.buy
check((C.margin:GetText() or ""):find("Margin after 5% cut: " .. (mg >= 0 and "+" or "") .. ns.MoneyText(mg), 1, true) == 1,
  "margin text " .. tostring(C.margin:GetText()))
check((C.volume:GetText() or ""):find("~120 sold/day", 1, true) == 1, "volume text " .. tostring(C.volume:GetText()))
check((C.volume:GetText() or ""):find("900 listed", 1, true) ~= nil, "listed count")
check(C.age:GetText():find("^Data: ") ~= nil and C.age:GetText():find("Alliance Auction House", 1, true) ~= nil, "data age " .. C.age:GetText())
check((C.change:GetText() or ""):find("%% 1D$") ~= nil, "change over timeframe " .. tostring(C.change:GetText()))
check(C.tfButtons["1D"].bg._color[4] == 0.35 and C.tfButtons["5D"].bg._color[4] == 1, "1D highlighted")
check(not C.msg:IsShown(), "no message for a normal item")
-- every drawn mark sits inside the plot
local okInside = true
for i = 1, C.pools.dot.used do
  local _, _, _, x, y = C.pools.dot.items[i]:GetPoint(1)
  if x < -0.01 or x > C.plotW + 0.01 or y < 0 or y > C.plotH then okInside = false end
end
check(okInside, "dots inside the plot")

section("chart: timeframes")
local expectN = { ["1D"] = 24, ["5D"] = 120, ["1W"] = 168, ["1M"] = 30, ["1Y"] = 365 }
for _, tf in ipairs({ "5D", "1W", "1M", "1Y", "1D" }) do
  ns.SetChartTF(tf)
  check(ns.settings.chartTF == tf, "timeframe saved " .. tf)
  check(C.lastError == nil, "no error on " .. tf .. ": " .. tostring(C.lastError))
  check(C.view and C.view.n == expectN[tf], "points for " .. tf)
  check(C.tfButtons[tf].bg._color[4] == 0.35, "button highlighted " .. tf)
  local xl = M.Texts(C.pools.xlab)
  check(#xl >= 2 and #xl <= 12, "x label count " .. tf .. " = " .. #xl)
  if tf == "1M" or tf == "1Y" then check(xl[1]:match("^%a%a%a %d%d$") ~= nil, "daily x label " .. tostring(xl[1])) end
  if tf == "1D" then check(xl[1]:match("^%d%d:%d%d$") ~= nil, "hourly x label " .. tostring(xl[1])) end
  if tf == "5D" or tf == "1W" then check(xl[1]:match("^%a%a%a %d%d:%d%d$") ~= nil, "multi-day x label " .. tostring(xl[1])) end
  check(distinct(M.Texts(C.pools.ylab)), "y labels differ on " .. tf)
  check((C.change:GetText() or ""):find(tf, 1, true) ~= nil, "change label names " .. tf)
end
ns.SetChartTF("bogus")
check(ns.settings.chartTF == "1D", "invalid timeframe ignored")
ns.SetChartTF("1m")
check(ns.settings.chartTF == "1M", "lower-case timeframe accepted")
ns.SetChartTF("1D")
M.fireScript(C.tfButtons["1W"], "OnClick")
check(ns.settings.chartTF == "1W" and C.view.n == 168, "clicking a timeframe button")
ns.SetChartTF("1D")

section("chart: pooling and redraw rules")
ns.SetChartTF("1Y")
local before = { M.created.texture, M.created.line, M.created.fontstring, M.created.frame }
ns.SetChartTF("1D"); ns.SetChartTF("5D"); ns.SetChartTF("1Y")
check(M.created.texture == before[1] and M.created.line == before[2] and M.created.fontstring == before[3]
      and M.created.frame == before[4], "redraws reuse pooled textures, lines and labels")
local calls = M.seriesCalls
ns.ShowChart(2589, "slash")
check(M.seriesCalls == calls, "showing the same item again does not redraw")
M.RunTimers()
check(M.seriesCalls == calls, "no redraw from the size event of an unchanged panel")
F:SetWidth(700); M.RunTimers()
check(M.seriesCalls == calls + 1 and C.plotW == 700 - 20 - 64, "resize redraws once")
F:SetWidth(800); M.RunTimers()
ns.SetChartTF("1D")

section("chart: item not in data")
ns.ShowChart(999, "slash")
check(C.lastError == nil, "no error")
check(C.msg:IsShown() and (C.msg:GetText() or ""):find("No recent listings for item 999", 1, true), "message: " .. tostring(C.msg:GetText()))
check(C.pools.dot.used == 0 and C.pools.trend.used == 0 and C.pools.ylab.used == 0, "no chart marks")
local anyShown = false
for _, d in ipairs(C.pools.dot.items) do if d:IsShown() then anyShown = true end end
for _, d in ipairs(C.pools.trend.items) do if d:IsShown() then anyShown = true end end
check(not anyShown, "old marks hidden")
check(C.buyBox.value:GetText() == "-" and C.listBox.value:GetText() == "-", "empty boxes")
check(C.verdict:GetText() == "" and not C.meter:IsShown(), "empty footer")

section("chart: one point")
ns.ShowChart(1, "slash")
check(C.lastError == nil, "no error: " .. tostring(C.lastError))
check(C.view == nil and C.pools.dot.used == 0, "no near-empty plot for one point")
check(C.msg:IsShown() and C.msg:GetText() == "Collecting price history. This chart fills in as hourly prices arrive (1 so far).",
  "collecting message: " .. tostring(C.msg:GetText()))
check(C.pools.trend.used == 0 and C.pools.ring.used == 0 and C.pools.ylab.used == 0 and C.pools.xlab.used == 0,
  "no marks or axis labels")
check(C.name:GetText() == "One Point Item" and C.buyBox.value:GetText() ~= "", "header and boxes still filled in")
M.cursorX = 300
M.fireScript(C.plot, "OnEnter"); M.fireScript(C.plot, "OnUpdate", 0.01)
check(C.lastError == nil and not GameTooltip._shown and C.hoverIdx == nil, "hovering a one-point chart shows nothing")
M.fireScript(C.plot, "OnLeave")
C:HoverAt(1); C:HoverAt(0)
check(not GameTooltip._shown, "HoverAt without a plot does nothing")
ns.SetChartTF("1M")
check(C.msg:GetText() == "Collecting price history. This chart fills in as daily prices arrive (1 so far).",
  "daily wording on 1M: " .. tostring(C.msg:GetText()))
ns.SetChartTF("1D")

section("chart: not enough history")
ns.ShowChart(9, "slash")
check(C.lastError == nil, "no error: " .. tostring(C.lastError))
check(C.buyBox.value:GetText() == "Needs more history" and C.listBox.value:GetText() == ns.MoneyText(699),
  "Target Buy says Needs more history, Sell at the tooltip's Sell now: " .. tostring(C.listBox.value:GetText()))
check(C.buyBox.value._color[1] == ns.COLORS.muted[1] and C.listBox.value._color[1] == 1, "Target Buy grey, Sell at white")
check(C.verdict:GetText() == "No advice yet" and C.verdict._color[1] == ns.COLORS.muted[1], "grey verdict, no colour")
check(not C.meter:IsShown(), "no verdict meter")
check((C.margin:GetText() or "") == "", "no green margin")
check(C.pools.tag.used == 0 and C.pools.dash.used == 0, "no dashed target lines")
check((C.notes:GetText() or ""):find("Not enough price history yet", 1, true), "note says why: " .. tostring(C.notes:GetText()))
check(C.pools.dot.used > 0, "the prices are still plotted")
ns.ShowChart(11, "slash")
check(C.buyBox.value:GetText() == "Price not reliable" and C.verdict:GetText() == "No advice yet", "thin listings: Price not reliable")
check((C.notes:GetText() or ""):find("Only 2 listings", 1, true), "thin note")
ns.ShowChart(2589, "slash")
check(C.buyBox.value._color[1] == 1 and C.buyBox.value:GetText() == ns.MoneyText(ns.Recommend(2589, "1D").buy),
  "a normal item gets white targets again")
check(C.meter:IsShown() and C.verdict:GetText() == "Low now: still falling", "and its verdict")

section("chart: sales still being measured")
ns.SoldPerDay = function(item) return nil, true end
ns.ShowChart(14047, "slash")
check((C.volume:GetText() or ""):find("sales: measuring", 1, true) == 1, "measuring: " .. tostring(C.volume:GetText()))
ns.SoldPerDay = function(item) return 480, true end
ns.ShowChart(2589, "slash")
check((C.volume:GetText() or ""):find("~480 sold/day (early estimate)", 1, true) == 1, "early estimate: " .. tostring(C.volume:GetText()))
ns.SoldPerDay = nil
ns.ShowChart(14047, "slash")
check((C.volume:GetText() or ""):find("~120 sold/day", 1, true) == 1, "without SoldPerDay: sold24")

section("chart: over 100g with a range under 1 silver")
for _, tf in ipairs({ "1D", "1W", "1Y" }) do
  ns.SetChartTF(tf)
  ns.ShowChart(10, "slash")
  check(C.lastError == nil, "no error " .. tf .. ": " .. tostring(C.lastError))
  local yl4 = M.Texts(C.pools.ylab)
  check(#yl4 >= 2 and distinct(yl4), "at least two different y labels on " .. tf .. ": " .. table.concat(yl4, ", "))
  local inside = true
  for i = 1, C.pools.dot.used do
    local _, _, _, x, y = C.pools.dot.items[i]:GetPoint(1)
    if x < -0.01 or x > C.plotW + 0.01 or y < 0 or y > C.plotH then inside = false end
  end
  check(inside, "dots inside the plot on " .. tf)
end
ns.SetChartTF("1D")

section("chart: all prices equal")
for _, tf in ipairs({ "1D", "1Y" }) do
  ns.SetChartTF(tf)
  ns.ShowChart(2, "slash")
  check(C.lastError == nil, "no error " .. tf)
  local yl2 = M.Texts(C.pools.ylab)
  check(#yl2 >= 1 and distinct(yl2), "y labels differ: " .. table.concat(yl2, ", "))
  check(C.verdict:GetText() == "Hold", "Hold verdict")
  check((C.margin:GetText() or ""):find("Margin after 5% cut: -", 1, true) == 1, "negative margin " .. tostring(C.margin:GetText()))
  check((C.notes:GetText() or ""):find("Spread does not beat", 1, true) ~= nil, "notes shown")
end
ns.SetChartTF("1D")

section("chart: gaps")
for _, tf in ipairs({ "1D", "5D", "1W", "1M", "1Y" }) do
  ns.SetChartTF(tf)
  ns.ShowChart(3, "slash")
  check(C.lastError == nil, "no error " .. tf)
  check(C.pools.trend.used < C.view.n - 1, "trend line lifts over gaps on " .. tf)
end
ns.SetChartTF("1D")

section("chart: nothing listed in window")
ns.ShowChart(4, "slash")
check(C.lastError == nil, "no error")
check((C.msg:GetText() or ""):find("Nothing was listed", 1, true), "message: " .. tostring(C.msg:GetText()))

section("chart: expensive item labels")
for _, tf in ipairs({ "1D", "5D", "1W", "1M", "1Y" }) do
  ns.SetChartTF(tf)
  ns.ShowChart(5, "slash")
  local yl3 = M.Texts(C.pools.ylab)
  check(#yl3 >= 2 and distinct(yl3), "y labels differ on " .. tf .. ": " .. table.concat(yl3, ", "))
end
ns.SetChartTF("1D")

section("chart: y ticks helper")
for _, r in ipairs({ { 0, 3 }, { 40, 52 }, { 99, 101 }, { 9990, 10012 }, { 999000, 1001000 }, { 1500000, 1500300 }, { 1, 9000000 } }) do
  local ticks, labels = C.YTicks(r[1], r[2])
  check(#ticks >= 1 and #ticks <= 7 and distinct(labels), "ticks for " .. r[1] .. ".." .. r[2] .. ": " .. table.concat(labels, ", "))
end

section("chart: hover")
ns.ShowChart(3, "slash")
local plot = C.plot
plot._left = 10
local v = C.view
M.cursorX = 10 + v.stepX * 3 + 1
M.fireScript(plot, "OnEnter")
check(plot:GetScript("OnUpdate") ~= nil, "hover starts tracking")
M.fireScript(plot, "OnUpdate", 0.01)
check(C.hoverIdx == 4, "hover index " .. tostring(C.hoverIdx))
check(C.cross:IsShown() and C.hoverCheap:IsShown() and C.hoverMarket:IsShown(), "crosshair and hover dots")
check(GameTooltip:GetOwner() == plot and GameTooltip._shown, "tooltip shown")
local tl = table.concat(GameTooltip._lines, "\n")
check(tl:find("Cheapest = ", 1, true) and tl:find("Market = ", 1, true) and tl:find("Est. sold that hour = ", 1, true), "tooltip lines: " .. tl)
check(GameTooltip._lines[1]:match("%d%d:%d%d$") ~= nil, "tooltip time " .. GameTooltip._lines[1])
M.cursorX = 10 + v.stepX * 2
M.fireScript(plot, "OnUpdate", 0.01)
check(C.hoverIdx == 3, "moves to a null point")
check(table.concat(GameTooltip._lines, "\n"):find("Cheapest = nothing listed", 1, true), "null point says nothing listed")
check(not C.hoverCheap:IsShown(), "no hover dot for a null price")
local calls2 = M.seriesCalls
for _ = 1, 30 do M.fireScript(plot, "OnUpdate", 0.01) end
check(M.seriesCalls == calls2, "hovering never redraws")
M.cursorX = 99999; M.fireScript(plot, "OnUpdate", 0.01)
check(C.hoverIdx == v.n, "clamped at the right edge")
M.cursorX = -500; M.fireScript(plot, "OnUpdate", 0.01)
check(C.hoverIdx == 1, "clamped at the left edge")
M.fireScript(plot, "OnLeave")
check(plot:GetScript("OnUpdate") == nil and not C.cross:IsShown() and GameTooltip:GetOwner() == nil, "hover cleaned up")
ns.SetChartTF("1Y"); ns.ShowChart(2589, "slash")
M.cursorX = 10 + C.view.stepX * 100
M.fireScript(plot, "OnEnter"); M.fireScript(plot, "OnUpdate", 0.01)
check(C.hoverIdx == 101 and GameTooltip._lines[4]:find("Est. sold that day", 1, true), "daily hover")
check(GameTooltip._lines[1]:match("^%a%a%a %a%a%a %d%d$") ~= nil, "daily tooltip date " .. tostring(GameTooltip._lines[1]))
M.fireScript(plot, "OnLeave")
ns.SetChartTF("1D")

section("chart: standalone drag, close and toggle")
M.dragTo = { "TOPLEFT", UIParent, "TOPLEFT", 120.4, -200.2 }
M.fireScript(F, "OnDragStart"); M.fireScript(F, "OnDragStop")
local cp = ns.settings.chartPoint
check(type(cp) == "table" and cp[1] == "TOPLEFT" and cp[3] == 120 and cp[4] == -200, "position saved")
ns.HideChart()
check(not F:IsShown(), "HideChart hides")
ns.ShowChart(2589, "slash")
local p1, r1, rp1, x1, y1 = F:GetPoint(1)
check(p1 == "TOPLEFT" and r1 == UIParent and x1 == 120 and y1 == -200, "saved position reused")
M.fireScript(C.closeButton, "OnClick")
check(not F:IsShown(), "close button hides")
check(ns.settings.chart ~= false, "closing the standalone chart keeps the AH auto-show setting")
check(ns.ToggleChart() == true and F:IsShown() and ns.ChartItem() == 2589, "toggle shows the last item")
check(ns.ToggleChart() == false and not F:IsShown(), "toggle hides")
check(#UISpecialFrames == 1 and UISpecialFrames[1] == "ForeverAHChartFrame", "Escape closes the window")

section("chart: nothing selected yet")
ns.ShowChart(nil, "slash")
check(ns.ChartItem() == 2589, "nil keeps the last item")
check(ns.settings.lastItem == 2589, "last item remembered")

section("chart: faction note")
M.faction = "Horde"
ns.ShowChart(14047, "slash")
check((C.notes:GetText() or ""):find("Prices are from the Alliance Auction House", 1, true), "Horde player is told the data is Alliance")
check((C.verdict:GetText() or "") == "" and not C.meter:IsShown(), "no verdict for another market")
M.faction = nil
ns.ShowChart(2589, "slash")
check(C.lastError == nil, "no draw errors in this scenario")
'''

NOITEM_TEST = r'''
local ns, M = TESTNS, MOCK
local C = ns.ChartPanel
section("chart: no item chosen")
ns.ShowChart(nil, "slash")
check(C.lastError == nil, "no error")
check(ForeverAHChartFrame:IsShown(), "shown")
check((C.msg:GetText() or ""):find("Search for an item", 1, true), "hint: " .. tostring(C.msg:GetText()))
check(C.name:GetText() == "Goblin Ledger", "neutral header")
'''

NOLINES_TEST = r'''
local ns, M = TESTNS, MOCK
local C = ns.ChartPanel
section("chart: no CreateLine (fallback trend)")
for _, tf in ipairs({ "1D", "1Y" }) do
  ns.SetChartTF(tf)
  ns.ShowChart(2589, "slash")
  check(C.lastError == nil, "no error " .. tf .. ": " .. tostring(C.lastError))
  check(C.hasLines == false and C.pools.trendDot and C.pools.trendDot.used > 0, "trend drawn from small squares")
  check(M.created.line == 0, "no lines created")
end
'''

NODATA_TEST = r'''
local ns, M = TESTNS, MOCK
local C = ns.ChartPanel
section("chart: no data at all")
ns.ShowChart(2589, "slash")
check(C.lastError == nil, "no error")
check((C.msg:GetText() or ""):find("Install-Goblin-Ledger.bat", 1, true), "tells the user what to run: " .. tostring(C.msg:GetText()))
check(C.age:GetText() == "Data: none yet", "age says none")
for _, tf in ipairs({ "1D", "5D", "1W", "1M", "1Y" }) do ns.SetChartTF(tf); check(C.lastError == nil, "no error " .. tf) end
check(C.pools.dot.used == 0, "nothing drawn")
'''

MODERN_TEST = r'''
local ns, M = TESTNS, MOCK
local AHS = ns.AHState

section("modern AH: hooks")
check(AHS.hookedModern == false, "waits for Blizzard_AuctionHouseUI")
check(AHS.hookedC == true, "C_AuctionHouse hooked at load")
local ahf = M.MakeModernAH()
M.Fire("ADDON_LOADED", "Blizzard_AuctionHouseUI")
check(AHS.hookedModern == true, "hooked on ADDON_LOADED")
local toggle = M.FindChild(ahf, function(c) return c._text == "Chart" end)
check(toggle ~= nil, "Chart button on the AH frame")
local csf, isf = ahf.CommoditiesSellFrame, ahf.ItemSellFrame
local cUse = M.FindChild(csf, function(c) return c._ftype == "Button" and c.kind == "now" end)
local iUse = M.FindChild(isf, function(c) return c._ftype == "Button" and c.kind == "now" end)
local cBest = M.FindChild(csf, function(c) return c._ftype == "Button" and c.kind == "best" end)
local iBest = M.FindChild(isf, function(c) return c._ftype == "Button" and c.kind == "best" end)
check(cUse and iUse and cBest and iBest and not cUse:IsShown() and not iUse:IsShown() and not cBest:IsShown(),
  "Sell now and Best price buttons created hidden")
local p0, r0, rp0 = cUse:GetPoint(1)
check(p0 == "TOPRIGHT" and r0 == csf.PostButton and rp0 == "BOTTOM", "Sell now sits under Post, left of the middle")
local p1, r1, rp1 = cBest:GetPoint(1)
check(p1 == "TOPLEFT" and r1 == csf.PostButton and rp1 == "BOTTOM", "Best price sits under Post, right of the middle")
check(cUse:GetFrameLevel() > 350 and cBest:GetFrameLevel() > 350, "buttons above the sell frame overlay")
M.Fire("ADDON_LOADED", "Blizzard_AuctionHouseUI")
local nButtons = 0
for _, c in ipairs(ahf._children) do if c._text == "Chart" then nButtons = nButtons + 1 end end
check(nButtons == 1, "hooks install once")

section("modern AH: open")
ahf:Show()
M.Fire("PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 21)
M.RunTimers()
local F, C = ForeverAHChartFrame, ns.ChartPanel
check(ns.AH_IsOpen(), "AH open")
check(F and F:IsShown(), "chart shown when the AH opens")
check(C.docked and C.placement == "below", "docked below the AH")
local p, rel, rp, x, y = F:GetPoint(1)
check(p == "TOPLEFT" and rel == ahf and rp == "BOTTOMLEFT" and x == 0 and y == -32, "anchored under the AH and its tabs")
check(F:GetWidth() == 800, "same width as the AH")
check((C.msg:GetText() or ""):find("Search for an item", 1, true), "empty state hint")
M.Fire("AUCTION_HOUSE_SHOW"); M.RunTimers()
check(ns.ChartItem() == nil, "second open signal is harmless")

section("modern AH: exact search")
ahf:SendBrowseQuery("linen cloth")
check(ns.ChartItem() ~= 2589, "search is debounced")
M.RunTimers()
check(ns.ChartItem() == 2589 and C.name:GetText() == "Linen Cloth", "exact search shows the item")

section("modern AH: partial search and browse results")
ahf:SendBrowseQuery("rune")
M.RunTimers()
check(AHS.pendingSearch == "rune", "partial search waits for results")
M.browseResults = { { itemKey = { itemID = 99999 } }, { itemKey = { itemID = 14047 } } }
M.Fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
M.RunTimers()
check(ns.ChartItem() == 14047, "first result with prices is shown")
check(AHS.pendingSearch == nil, "search consumed")
M.browseResults = { { itemKey = { itemID = 2589 } } }
M.Fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED"); M.Fire("AUCTION_HOUSE_BROWSE_RESULTS_ADDED"); M.RunTimers()
check(ns.ChartItem() == 14047, "later refresh events do not switch the item")

section("modern AH: a name two items share")
ahf:SendBrowseQuery("Squire's Shirt")
M.RunTimers()
check(ns.ChartItem() == 7, "shows the lowest id at once")
check(AHS.pendingSearch == "squire's shirt" and type(AHS.pendingIDs) == "table" and AHS.pendingIDs[7] and AHS.pendingIDs[8],
  "keeps waiting for the results to say which one is listed")
M.browseResults = { { itemKey = { itemID = 2589 } }, { itemKey = { itemID = 8 } } }
M.Fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
M.RunTimers()
check(ns.ChartItem() == 8, "the one in the results is shown")
check(AHS.pendingSearch == nil and AHS.pendingIDs == nil, "search consumed")
ahf:SendBrowseQuery("squire's shirt")
M.RunTimers()
M.browseResults = { { itemKey = { itemID = 2589 } }, { itemKey = { itemID = 14047 } } }
M.Fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
M.RunTimers()
check(ns.ChartItem() == 7, "results with neither shirt do not switch to another item")
ahf:SendBrowseQuery("Runecloth")
M.RunTimers()
check(ns.ChartItem() == 14047 and AHS.pendingSearch == nil and AHS.pendingIDs == nil, "a one-item name needs no results")

section("modern AH: selecting results")
ahf:SelectBrowseResult({ itemKey = { itemID = 12363 }, minPrice = 780000 })
check(ns.ChartItem() == 12363, "commodity row")
ahf:SelectBrowseResult({ itemKey = { itemID = 6, itemLevel = 40 }, minPrice = 1 })
check(ns.ChartItem() == 6, "gear row")
ahf:SendBrowseQuery("linen cloth")
ahf:SelectBrowseResult({ itemKey = { itemID = 12363 } })
M.RunTimers()
check(ns.ChartItem() == 12363, "a click after a search wins over the debounced search")

section("modern AH: other search sources")
C_AuctionHouse.SendBrowseQuery({ searchString = '"Runecloth";Trade Goods;0;0' })
M.RunTimers()
check(ns.ChartItem() == 14047, "Auctionator exact search syntax")
C_AuctionHouse.SendBrowseQuery({ searchString = "" })
M.RunTimers()
check(ns.ChartItem() == 14047 and AHS.pendingSearch == nil, "empty full-scan search ignored")
for _, term in ipairs({ "Runecloth", "Linen Cloth", "Arcane Crystal" }) do C_AuctionHouse.SendBrowseQuery({ searchString = term }) end
local calls = M.seriesCalls
M.RunTimers()
check(ns.ChartItem() == 12363 and M.seriesCalls == calls + 1, "a burst of searches redraws once, on the last one")
ahf:SelectBrowseResult({ itemKey = { itemID = 14047 } })

section("modern AH: sell frames and the List At button")
ahf:SetPostItem({ itemID = 14047, commodity = true })
check(ns.ChartItem() == 14047, "sell item shown")
local rec = ns.Recommend(14047, ns.settings.chartTF)
local sp = ns.SellPrices(14047, ns.settings.chartTF)
check(cUse:IsShown() and cUse:GetText() == "Sell now: " .. ns.MoneyText(sp.now), "Sell now label " .. tostring(cUse:GetText()))
check(cBest:IsShown() and cBest:GetText() == "Sell at: " .. ns.MoneyText(sp.at), "Sell at label " .. tostring(cBest:GetText()))
check(sp.now == 1199 and sp.at == 2000 and sp.under == 40, "Sell now 1c under the cheapest, 40% under the usual 20s: Sell at 20s")
local posts = csf.posts or 0
M.fireScript(cUse, "OnClick")
check(csf.PriceInput.amount == sp.now, "commodity price is exact copper: " .. tostring(csf.PriceInput.amount) .. " vs " .. sp.now)
check((csf.posts or 0) == posts + 1, "post state refreshed")
M.fireScript(cBest, "OnClick")
check(csf.PriceInput.amount == sp.at, "Sell at fills its price: " .. tostring(csf.PriceInput.amount))
ahf:SetPostItem({ itemID = 6, commodity = false })
check(ns.ChartItem() == 6, "gear sell item shown")
check(not cUse:IsShown() and not cBest:IsShown() and iUse:IsShown() and not iBest:IsShown(),
  "only the active sell frame shows a button, and one price: Sell now")
local sp6 = ns.SellPrices(6, ns.settings.chartTF)
M.fireScript(iUse, "OnClick")
check(isf.PriceInput.amount == math.floor(sp6.now / 100) * 100, "gear price rounded down to whole silver: " .. tostring(isf.PriceInput.amount))
check(iUse:GetText() == "Sell now: " .. ns.MoneyText(math.floor(sp6.now / 100) * 100), "gear label shows the rounded price")
check(sp6.at == sp6.now and sp6.under == nil, "gear at its usual price: no second price")
M.copper = false
ahf:SetPostItem({ itemID = 14047, commodity = true })
M.fireScript(cUse, "OnClick")
check(csf.PriceInput.amount == math.max(100, math.floor(sp.now / 100) * 100), "silver when the client has no copper box")
M.copper = nil
ns.SetChartTF("1W")
ahf:SetPostItem({ itemID = 14047, commodity = true })
M.fireScript(cBest, "OnClick")
check(csf.PriceInput.amount == sp.at and cBest:GetText() == "Sell at: " .. ns.MoneyText(sp.at), "the chart's timeframe never changes the price")
ns.SetChartTF("1D")
ahf:SetPostItem({ itemID = 999, commodity = true })
check(not cUse:IsShown() and not cBest:IsShown(), "no buttons without price data")
check(ns.ChartItem() == 999 and (C.msg:GetText() or ""):find("No recent listings", 1, true), "chart says no listings")
M.fireScript(cUse, "OnClick")
check(csf.PriceInput.amount == 100, "clicking without advice leaves Blizzard's price alone")

section("modern AH: no advice yet: Sell now only")
ahf:SetPostItem({ itemID = 9, commodity = true })
M.RunTimers()
check(cUse:IsShown() and cUse:GetText() == "Sell now: " .. ns.MoneyText(699) and not cBest:IsShown(),
  "too little history: Sell now from the price data, one price: " .. tostring(cUse:GetText()))
check((cUse.tipBody or ""):find("suggested", 1, true) == nil, "no suggested price mentioned: " .. tostring(cUse.tipBody))
AHS.StoreLive(9, 650, false)
M.RunTimers()
check(cUse:IsShown() and cUse:GetText() == "Sell now: " .. ns.MoneyText(649), "1 copper under the live listing: " .. tostring(cUse:GetText()))
check((cUse.tipBody or ""):find("cheapest listing right now", 1, true) ~= nil, "tooltip says the live listing was used")
M.fireScript(cUse, "OnClick")
check(csf.PriceInput.amount == 649, "fills that price")
AHS.StoreLive(9, 650, true)
M.RunTimers()
check(cUse:GetText() == "Sell now: " .. ns.MoneyText(650), "your own listing is matched, not undercut")
AHS.StoreLive(9, nil)
M.RunTimers()
check(cUse:GetText() == "Sell now: " .. ns.MoneyText(699), "live listing gone: back to the price data")
ahf:SetPostItem({ itemID = 11, commodity = true })
AHS.StoreLive(11, 800, false)
M.RunTimers()
check(cUse:IsShown() and not cBest:IsShown() and (cUse.tipBody or ""):find("may not be reliable", 1, true) ~= nil,
  "thin listings: Sell now only, says why")
AHS.StoreLive(11, nil)
M.RunTimers()

section("modern AH: Chart button and close X")
M.fireScript(toggle, "OnClick")
check(not F:IsShown() and ns.settings.chart == false, "Chart button hides and remembers")
ahf:SelectBrowseResult({ itemKey = { itemID = 2589 } })
check(not F:IsShown() and AHS.itemID == 2589, "stays hidden but tracks the item")
M.fireScript(toggle, "OnClick")
check(F:IsShown() and ns.settings.chart == true and ns.ChartItem() == 2589, "Chart button shows the tracked item")
M.fireScript(C.closeButton, "OnClick")
check(not F:IsShown() and ns.settings.chart == false, "close X under the AH turns auto-show off")
M.fireScript(toggle, "OnClick")
check(F:IsShown() and ns.settings.chart == true, "and the Chart button turns it back on")

section("modern AH: placement")
ahf:SetScale(0.1); ns.ChartRefreshPlacement()
check(not C.docked and C.placement == "float", "floats when TSM shrinks the AH")
local _, frel = F:GetPoint(1)
check(frel == UIParent, "floating panel anchored to the screen")
ahf:SetScale(1); ns.ChartRefreshPlacement()
check(C.docked and C.placement == "below", "docks again")
ahf._bottom = 100; ns.ChartRefreshPlacement()
check(C.placement == "right" and F:GetWidth() == 539, "goes right of the AH when there is no room below: " .. tostring(F:GetWidth()))
local _, rrel, rrp = F:GetPoint(1)
check(rrel == ahf and rrp == "TOPRIGHT", "anchored to the AH's right edge")
check(C.lastError == nil and C.plotW == 539 - 20 - 64, "narrow layout draws")
ahf._right = 1200; ns.ChartRefreshPlacement()
check(C.placement == "float", "floats when there is no room anywhere")
ahf._bottom, ahf._right = 300, 816; ns.ChartRefreshPlacement()
check(C.placement == "below" and F:GetWidth() == 800, "back under the AH")
ahf:SetWidth(810); ns.ChartRefreshPlacement()
check(F:GetWidth() == 810, "follows a wider (ElvUI) AH frame")
ahf:SetWidth(800); ns.ChartRefreshPlacement()

section("Auctionator: event bus")
M.MakeAuctionator()
M.Fire("ADDON_LOADED", "Auctionator")
check(AHS.hookedAtr, "listening on Auctionator's event bus")
local src = {}
Auctionator.EventBus:RegisterSource(src, "test")
Auctionator.EventBus:Fire(src, Auctionator.Buying.Events.ShowCommodityBuy, { itemKey = { itemID = 12363 } }, {})
check(ns.ChartItem() == 12363, "Auctionator buy view switches the chart")
Auctionator.EventBus:Fire(src, Auctionator.Buying.Events.ShowItemBuy, { itemKey = { itemID = 6 } }, {})
check(ns.ChartItem() == 6, "Auctionator item buy view")
Auctionator.EventBus:Fire(src, Auctionator.Selling.Events.BagItemClicked,
  { itemLink = "|cffffffff|Hitem:14047::::::::60:::::|h[Runecloth]|h|r", location = { itemID = 14047, commodity = true }, count = 20 })
check(ns.ChartItem() == 14047, "Auctionator selling item switches the chart")
local aUse = M.FindChild(AuctionatorSellingFrame.SaleItemFrame, function(c) return c._ftype == "Button" end)
check(aUse and aUse:IsShown(), "Auctionator use button shown")
M.fireScript(aUse, "OnClick")
local fired = M.atrFired[#M.atrFired]
check(fired and fired.event == "price_selected" and fired.args[1].buyout == sp.now and fired.args[2] == false,
  "fires PriceSelected with the Sell now copper")
local aBest = M.FindChild(AuctionatorSellingFrame.SaleItemFrame, function(c) return c.kind == "best" end)
check(aUse.kind == "now" and aBest and aBest:IsShown(), "Auctionator Sell at button shown too (Runecloth is dumped)")
M.fireScript(aBest, "OnClick")
fired = M.atrFired[#M.atrFired]
check(fired and fired.args[1].buyout == sp.at, "Sell at through PriceSelected")
AuctionatorSellingFrame:Hide()
C_AuctionHouse.SendSellSearchQuery({ itemID = 2589, itemLevel = 0, itemSuffix = 0, battlePetSpeciesID = 0 }, {}, false)
M.RunTimers()
check(ns.ChartItem() == 14047, "sell searches ignored while the Selling tab is hidden")
AuctionatorSellingFrame:Show()
C_AuctionHouse.SendSellSearchQuery({ itemID = 2589, itemLevel = 0, itemSuffix = 0, battlePetSpeciesID = 0 }, {}, false)
M.RunTimers()
check(ns.ChartItem() == 2589 and AHS.atrSellLocation == nil, "sell search followed while the Selling tab is visible")
Auctionator.EventBus.Fire = function() error("boom") end
M.fireScript(aUse, "OnClick")
check(AuctionatorSellingFrame.SaleItemFrame.Price.amount == ns.SellPrices(2589, "1D").now, "falls back to Auctionator's price box")

section("modern AH: close")
M.Fire("PLAYER_INTERACTION_MANAGER_FRAME_HIDE", 21)
check(not ns.AH_IsOpen() and not F:IsShown(), "chart hidden on close")
check(not cUse:IsShown() and not iUse:IsShown() and not aUse:IsShown() and not cBest:IsShown() and not aBest:IsShown(),
  "sell buttons hidden on close")
M.Fire("AUCTION_HOUSE_CLOSED")
ahf:SendBrowseQuery("Runecloth"); M.RunTimers()
check(not F:IsShown() and ns.ChartItem() == 2589, "searches while closed do nothing")
M.Fire("PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 5)
check(not ns.AH_IsOpen(), "other NPC windows are ignored")

section("modern AH: reopen")
ns.settings.chart = false
M.Fire("AUCTION_HOUSE_SHOW"); M.RunTimers()
check(ns.AH_IsOpen() and not F:IsShown(), "respects the hidden setting")
ahf:SelectBrowseResult({ itemKey = { itemID = 12363 } })
check(not F:IsShown(), "still hidden")
M.Fire("AUCTION_HOUSE_CLOSED")
ns.settings.chart = true
M.Fire("AUCTION_HOUSE_SHOW"); M.RunTimers()
check(F:IsShown() and C.docked and ns.ChartItem() == 12363, "reopens on the last item")
M.Fire("AUCTION_HOUSE_CLOSED")
ns.ShowChart(2589, "slash")
check(F:IsShown() and not C.docked, "standalone again after the AH closed")
M.Fire("AUCTION_HOUSE_SHOW"); M.RunTimers()
check(C.docked, "docks when the AH opens")
M.Fire("AUCTION_HOUSE_CLOSED")
check(not F:IsShown(), "and hides when it closes")

section("modern AH: late frame show")
ahf:Hide()
M.Fire("AUCTION_HOUSE_SHOW"); M.RunTimers()
check(F:IsShown() and not C.docked, "floats while the AH frame is not shown yet")
ahf:Show(); M.RunTimers()
check(C.docked and C.placement == "below", "docks once the AH frame shows")
M.Fire("AUCTION_HOUSE_CLOSED")

section("modern AH: TSM window in use")
ahf:SetScale(0.1)
M.Fire("AUCTION_HOUSE_SHOW"); M.RunTimers()
check(F:IsShown() and not C.docked, "floats beside TSM's own window")
M.Fire("AUCTION_HOUSE_CLOSED")
check(not F:IsShown(), "still hides when the AH closes")
ahf:SetScale(1)
ns.ShowChart(2589, "slash")
check(ns.ChartSource() == "slash", "slash command source")
M.Fire("AUCTION_HOUSE_SHOW"); M.RunTimers()
M.Fire("AUCTION_HOUSE_CLOSED")
check(not F:IsShown(), "a chart the AH docked hides on close")

check(AHS.lastError == nil, "no errors inside hooks: " .. tostring(AHS.lastError))
check(C.lastError == nil, "no draw errors: " .. tostring(C.lastError))
'''

LEGACY_TEST = r'''
local ns, M = TESTNS, MOCK
local AHS = ns.AHState
section("legacy AH")
check(AHS.hookedLegacy == false, "waits for Blizzard_AuctionUI")
local af = M.MakeLegacyAH()
M.Fire("ADDON_LOADED", "Blizzard_AuctionUI")
check(AHS.hookedLegacy == true, "hooked")
check(M.FindChild(af, function(c) return c._text == "Chart" end) ~= nil, "Chart button on AuctionFrame")
af:Show()
M.Fire("AUCTION_HOUSE_SHOW")
M.RunTimers()
local F, C = ForeverAHChartFrame, ns.ChartPanel
check(F:IsShown() and C.docked, "chart docked")
local _, rel = F:GetPoint(1)
check(rel == af and F:GetWidth() == 832, "under AuctionFrame, same width")
QueryAuctionItems("Linen Cloth", 0, 0, 0, false)
M.RunTimers()
check(ns.ChartItem() == 2589, "search by name")
QueryAuctionItems("arcane")
M.RunTimers()
check(AHS.pendingSearch == "arcane", "partial search waits")
M.Fire("AUCTION_ITEM_LIST_UPDATE")
M.RunTimers()
check(ns.ChartItem() == 12363, "first listed item with prices")
SetSelectedAuctionItem("list", 2)
check(ns.ChartItem() == 2589, "clicked row")
M.legacySell = { "Runecloth", "tex", 20, 1, true, 0, 0, 20, 20, 14047 }
M.Fire("NEW_AUCTION_UPDATE")
check(ns.ChartItem() == 14047, "sell slot item")
local lUse = M.FindChild(AuctionFrameAuctions, function(c) return c.kind == "now" end)
local lBest = M.FindChild(AuctionFrameAuctions, function(c) return c.kind == "best" end)
check(lUse and lUse:IsShown() and lBest and lBest:IsShown(), "legacy Sell now and Sell at buttons shown")
local sp = ns.SellPrices(14047, "1D")
check(lUse:GetText() == "Sell now: " .. ns.MoneyText(sp.now) and lBest:GetText() == "Sell at: " .. ns.MoneyText(sp.at), "legacy labels")
M.fireScript(lUse, "OnClick")
check(BuyoutPrice.copper == sp.now * 5 and StartPrice.copper == sp.now * 5, "per-stack price = Sell now x stack size: " .. tostring(BuyoutPrice.copper))
AuctionFrameAuctions.priceType = 1
M.fireScript(lUse, "OnClick")
check(BuyoutPrice.copper == sp.now, "per-unit price")
M.fireScript(lBest, "OnClick")
check(BuyoutPrice.copper == sp.at, "per-unit Sell at")
M.legacySell = { "Linen Cloth", "tex", 10, 1, true, 0, 0, 10, 10 }
M.Fire("NEW_AUCTION_UPDATE")
check(ns.ChartItem() == 2589, "sell item found by name when no itemID is returned")
M.legacySell = nil
M.Fire("NEW_AUCTION_UPDATE")
check(not lUse:IsShown() and not lBest:IsShown(), "buttons hide when the slot is emptied")
M.Fire("AUCTION_HOUSE_CLOSED")
check(not F:IsShown() and not ns.AH_IsOpen(), "closed")
check(AHS.lastError == nil, "no hook errors: " .. tostring(AHS.lastError))
check(C.lastError == nil, "no draw errors")
'''

INTEGRATION_TEST = r'''
local ns, M = TESTNS, MOCK
section("integration: real Core.lua + Recommend.lua")
M.Fire("ADDON_LOADED", "GoblinLedger")
M.Fire("PLAYER_LOGIN")
check(type(ns.GetItem) == "function" and type(ns.Recommend) == "function", "contract API present")
local item = ns.GetItem(2589)
check(item ~= nil, "Linen Cloth in the data")
local C = ns.ChartPanel
for _, tf in ipairs({ "1D", "5D", "1W", "1M", "1Y" }) do
  ns.SetChartTF(tf)
  ns.ShowChart(2589, "slash")
  check(C.lastError == nil, "draws " .. tf .. ": " .. tostring(C.lastError))
  local s = ns.GetSeries(2589, tf)
  check(s and C.view and C.view.n == s.n, "uses the real series for " .. tf)
  check(C.pools.dot.used > 0 or (C.msg:GetText() or ""):find("Nothing", 1, true), "draws dots " .. tf)
  local yl = M.Texts(C.pools.ylab)
  local ok = true
  for i = 2, #yl do if yl[i] == yl[i - 1] then ok = false end end
  check(ok, "y labels differ " .. tf .. ": " .. table.concat(yl, ", "))
  local spr = ns.SellPrices(2589, tf)
  check(spr and C.listBox.value:GetText() == ns.MoneyIcons(spr.at), "Sell at box matches SellPrices on " .. tf)
end
ns.SetChartTF("1D")
for id in pairs(ns.db.items) do
  ns.ShowChart(id, "slash")
  if C.lastError then break end
end
check(C.lastError == nil, "every item in the data draws: " .. tostring(C.lastError))
ns.ShowChart(424242, "slash")
check((C.msg:GetText() or ""):find("No recent listings", 1, true), "unknown item")
local ahf = M.MakeModernAH()
M.Fire("ADDON_LOADED", "Blizzard_AuctionHouseUI")
ahf:Show()
M.Fire("PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 21)
M.RunTimers()
ahf:SendBrowseQuery("runecloth")
M.RunTimers()
check(ns.ChartItem() == 14047, "search finds Runecloth through ns.FindItemID")
ahf:SetPostItem({ itemID = 14047, commodity = true })
local cUse = M.FindChild(ahf.CommoditiesSellFrame, function(c) return c.kind == "now" end)
local cBest = M.FindChild(ahf.CommoditiesSellFrame, function(c) return c.kind == "best" end)
local sp = ns.SellPrices(14047, "1D")
M.fireScript(cUse, "OnClick")
check(ahf.CommoditiesSellFrame.PriceInput.amount == sp.now, "Sell now writes the real Sell now price")
if sp.best then
  M.fireScript(cBest, "OnClick")
  check(ahf.CommoditiesSellFrame.PriceInput.amount == sp.best, "Best price writes the real Best price")
end
ns.ShowChart(14047, "slash")
local C = ns.ChartPanel
M.fireScript(C.buyBox, "OnClick")
check(ns.settings.chartLine == "buy" and C.lastError == nil, "the real chart: Target Buy toggle")
M.fireScript(C.buyBox, "OnClick")
check(ns.settings.chartLine == nil, "the real chart: toggled off")
M.Fire("AUCTION_HOUSE_CLOSED")
check(ns.AHState.lastError == nil, "no hook errors: " .. tostring(ns.AHState.lastError))
'''


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def run_scenario(name, code, opts):
    lua = lua51.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(MOCK)
    G = lua.globals()
    M = G.MOCK
    if opts.get("noLines"):
        M.noLines = True
    for e in opts.get("unknownEvents", []):
        M.unknownEvents[e] = True
    if opts.get("interactionEnum", True):
        lua.execute("Enum.PlayerInteractionType = { Auctioneer = 21, Merchant = 5 }")
    try:
        if opts.get("real"):
            ns = lua.table()
            data_path = LOCAL_DATA if os.path.exists(LOCAL_DATA) else os.path.join(ADDON, "Data.lua")
            M.LoadSource(read(data_path), "Data.lua", ns)
            for fname in ("Core.lua", "Recommend.lua", "Chart.lua", "AH.lua"):
                M.LoadSource(read(os.path.join(ADDON, fname)), fname, ns)
            lua.execute("MOCK.fixtures = { [14047] = { name = 'Runecloth' }, [2589] = { name = 'Linen Cloth' } }")
        else:
            make = lua.eval("function(nodata) return MOCK.MakeStubNS({ nodata = nodata }) end")
            ns = make(bool(opts.get("nodata")))
            for fname in ("Chart.lua", "AH.lua"):
                M.LoadSource(read(os.path.join(ADDON, fname)), fname, ns)
        G.TESTNS = ns
        M.RunScenario(code)
    except Exception as exc:  # LuaError from loading the addon files
        lua.globals().check(False, "%s: %s" % (type(exc).__name__, exc))
    report = M.Report() or ""
    rows = []
    for line in report.split("\n"):
        if not line:
            continue
        ok, sec, msg = line.split("\t", 2)
        rows.append((ok == "1", sec, msg))
    return rows


def static_checks():
    rows = []
    for fname in ("Chart.lua", "AH.lua"):
        raw = open(os.path.join(ADDON, fname), "rb").read()
        rows.append((all(b < 128 for b in raw), "static", fname + " is plain ASCII"))
        text = raw.decode("utf-8", "replace")
        rows.append((chr(0x2014) not in text and chr(0x2013) not in text, "static", fname + " has no long dashes"))
        rows.append((text.startswith("local ADDON_NAME, ns = ..."), "static", fname + " starts with the ns line"))
        for bad in ("PostItem(", "PostCommodity(", "PlaceBid(", "StartCommoditiesPurchase(", "CancelAuction(",
                    "SendSearchQuery(", "SetScript(\"OnEvent\", nil)"):
            rows.append((bad not in text, "static", fname + " never calls " + bad))
    return rows


def main():
    scenarios = [
        ("chart", CHART_TEST, {}),
        ("chart-no-item", NOITEM_TEST, {}),
        ("chart-no-lines", NOLINES_TEST, {"noLines": True}),
        ("chart-no-data", NODATA_TEST, {"nodata": True}),
        ("ah-modern", MODERN_TEST, {"unknownEvents": ["AUCTION_ITEM_LIST_UPDATE", "NEW_AUCTION_UPDATE"]}),
        ("ah-legacy", LEGACY_TEST, {"interactionEnum": False,
                                    "unknownEvents": ["PLAYER_INTERACTION_MANAGER_FRAME_SHOW",
                                                      "PLAYER_INTERACTION_MANAGER_FRAME_HIDE",
                                                      "AUCTION_HOUSE_BROWSE_RESULTS_UPDATED",
                                                      "AUCTION_HOUSE_BROWSE_RESULTS_ADDED"]}),
    ]
    if all(os.path.exists(os.path.join(ADDON, f)) for f in ("Core.lua", "Recommend.lua")):
        scenarios.append(("integration", INTEGRATION_TEST, {"real": True}))
    else:
        print("note: Core.lua or Recommend.lua missing, integration scenario skipped")

    total = failed = 0
    all_rows = [("static", r) for r in static_checks()]
    for name, code, opts in scenarios:
        rows = run_scenario(name, code, opts)
        bad = sum(1 for r in rows if not r[0])
        print("%-15s %3d checks, %d failed" % (name, len(rows), bad))
        for r in rows:
            all_rows.append((name, r))
    for name, (ok, sec, msg) in all_rows:
        total += 1
        if not ok:
            failed += 1
            print("FAIL [%s] %s: %s" % (name, sec, msg))
    print("%d checks, %d failed" % (total, failed))
    if not os.path.exists(LOCAL_DATA):
        print("note: simulated data not found, integration used the placeholder Data.lua")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

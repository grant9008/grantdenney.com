local ADDON_NAME, ns = ...

-- Forever AH: the price chart panel.
-- A PocketGE-style panel built from plain frames, textures and font strings:
--   header   : icon, name, market price, change over the timeframe, 1D 5D 1W 1M 1Y
--   boxes    : Target Buy (gold) and List At (blue)
--   chart    : cheapest dots (gold), market dots (blue), white trend line,
--              dashed target lines, period high/low rings, est. sold bars
--   footer   : analyst verdict and meter, margin, volume, notes, data age
-- Every texture is pooled. The chart is redrawn only when the item, the
-- timeframe or the panel size changes. Hovering only moves a crosshair.

local floor, ceil, max, min, abs, sqrt = math.floor, math.ceil, math.max, math.min, math.abs, math.sqrt
local huge = math.huge
local tconcat = table.concat
local sformat = string.format

local TIMEFRAMES = { "1D", "5D", "1W", "1M", "1Y" }
local TF_VALID = { ["1D"] = true, ["5D"] = true, ["1W"] = true, ["1M"] = true, ["1Y"] = true }
-- How many points apart the x-axis labels sit, before the minimum pixel gap applies.
local TF_LABEL_STEP = { ["1D"] = 4, ["5D"] = 24, ["1W"] = 24, ["1M"] = 5, ["1Y"] = 61 }

local PANEL_H = 260
local COMPACT_H = 230   -- below this height the Target Buy / List At boxes are left out
local MIN_H = 150       -- shortest panel that still fits under the AH
local NARROW_W = 650    -- below this width the change and volume texts are left out
local DEFAULT_W = 800
local MIN_W = 440
local PAD = 10
local AXIS_W = 64       -- right-hand column for the y-axis labels
local HEADER_Y = -7
local BOX_Y = -38
local BOX_H = 40
local PLOT_TOP = 86     -- distance from the panel top to the plot top
local PLOT_TOP_COMPACT = 38
local PLOT_BOTTOM = 52  -- room under the plot for the x labels and the footer
local DOCK_GAP = 32     -- the AH tabs hang below the AH frame: leave room for them
local DASH, DASH_GAP = 6, 4

local WHITE = "Interface\\Buttons\\WHITE8X8"
local CIRCLE = "Interface\\CHARACTERFRAME\\TempPortraitAlphaMask"
local QUESTION = "Interface\\Icons\\INV_Misc_QuestionMark"
local DOT = " \194\183 " -- middle dot separator

local FALLBACK_COLORS = {
  buy = { 0.898, 0.722, 0.259 }, sell = { 0.22, 0.74, 0.97 },
  pos = { 0.25, 0.80, 0.45 }, neg = { 0.90, 0.33, 0.33 },
  muted = { 0.55, 0.51, 0.45 }, text = { 0.90, 0.88, 0.84 },
  bg = { 0.043, 0.055, 0.078, 0.96 }, panel = { 0.07, 0.08, 0.10, 0.96 },
}
local TREND = { 0.85, 0.83, 0.78 }
local CASING = { 0.03, 0.04, 0.055, 0.55 }

local VERDICT_COLOR = {
  ["Strong Buy"] = "pos", ["Buy"] = "pos", ["Hold"] = "text", ["Sell"] = "neg", ["Strong Sell"] = "neg",
}

---------------------------------------------------------------------------
-- Small helpers
---------------------------------------------------------------------------
local function Col(key)
  local c = ns.COLORS and ns.COLORS[key]
  if type(c) == "table" then return c end
  return FALLBACK_COLORS[key] or FALLBACK_COLORS.text
end

local function IsSecret(v)
  local f = _G.issecretvalue
  if type(f) ~= "function" then return false end
  local ok, r = pcall(f, v)
  return ok and r == true
end

-- A plain, finite, non-secret number, or nil.
local function Num(v)
  if type(v) ~= "number" or IsSecret(v) then return nil end
  if v ~= v or v == huge or v == -huge then return nil end
  return v
end

local function Clamp(v, lo, hi)
  if v < lo then return lo end
  if v > hi then return hi end
  return v
end

local function MoneyT(c)
  c = floor((Num(c) or 0) + 0.5)
  if type(ns.MoneyText) == "function" then return ns.MoneyText(c) end
  local neg = c < 0
  if neg then c = -c end
  local g, s, k = floor(c / 10000), floor(c / 100) % 100, c % 100
  local parts = {}
  if g > 0 then parts[#parts + 1] = g .. "g" end
  if s > 0 then parts[#parts + 1] = s .. "s" end
  if k > 0 and g < 100 then parts[#parts + 1] = k .. "c" end
  local out = #parts > 0 and tconcat(parts, " ") or "0c"
  return neg and ("-" .. out) or out
end

local function MoneyI(c)
  c = floor((Num(c) or 0) + 0.5)
  if type(ns.MoneyIcons) == "function" then return ns.MoneyIcons(c) end
  return MoneyT(c)
end

local function Commas(n)
  n = floor((Num(n) or 0) + 0.5)
  local s = tostring(abs(n))
  local out = s:reverse():gsub("(%d%d%d)", "%1,"):reverse()
  if out:sub(1, 1) == "," then out = out:sub(2) end
  if n < 0 then out = "-" .. out end
  return out
end

local function Print(msg)
  if type(ns.Print) == "function" then return ns.Print(msg) end
  local cf = _G.DEFAULT_CHAT_FRAME
  if type(cf) == "table" and cf.AddMessage then cf:AddMessage("Forever AH: " .. tostring(msg)) end
end

local function AgeText(sec)
  if type(ns.AgeText) == "function" then return ns.AgeText(sec) end
  if type(sec) ~= "number" then return "no data" end
  if sec < 60 then return "just now" end
  if sec < 3600 then return floor(sec / 60) .. "m ago" end
  if sec < 48 * 3600 then return floor(sec / 3600) .. "h ago" end
  return floor(sec / 86400) .. "d ago"
end

local function DateFn()
  local d = _G.date
  if type(d) == "function" then return d end
  if type(_G.os) == "table" and type(_G.os.date) == "function" then return _G.os.date end
  return nil
end

local function FormatTime(fmt, t)
  local d = DateFn()
  if not d or not Num(t) then return "" end
  local ok, s = pcall(d, fmt, t)
  if ok and type(s) == "string" then return s end
  return ""
end

-- Daily points sit on UTC midnight, so print them in UTC or US players see the day before.
local function AxisTimeLabel(t, step, tf)
  if (step or 0) >= 86400 then return FormatTime("!%b %d", t) end
  if tf == "1D" then return FormatTime("%H:%M", t) end
  return FormatTime("%a %H:%M", t)
end

local function TooltipTime(t, step)
  if (step or 0) >= 86400 then return FormatTime("!%a %b %d", t) end
  return FormatTime("%a %b %d, %H:%M", t)
end

local function CurrentTF()
  local tf = ns.settings and ns.settings.chartTF
  if type(tf) == "string" then tf = tf:upper() end
  if TF_VALID[tf] then return tf end
  return "1D"
end

local function HasData()
  local db = ns.db
  if type(db) ~= "table" then return false end
  if (tonumber(db.updated) or 0) <= 0 then return false end
  return type(db.items) == "table" and next(db.items) ~= nil
end

-- "" and "Item 12345" are what the data says when it has no name for an item.
local function KnownName(n)
  return type(n) == "string" and n ~= "" and not IsSecret(n) and n:match("^Item %d+$") == nil
end

local function GameItemName(id)
  if type(ns.GameItemName) == "function" then
    local ok, n = pcall(ns.GameItemName, id)
    if ok and KnownName(n) then return n end
  end
  local CI = _G.C_Item
  if type(CI) == "table" then
    if type(CI.GetItemNameByID) == "function" then
      local ok, n = pcall(CI.GetItemNameByID, id)
      if ok and KnownName(n) then return n end
    end
    if type(CI.GetItemInfo) == "function" then
      local ok, n = pcall(CI.GetItemInfo, id)
      if ok and KnownName(n) then return n end
    end
  end
  return nil
end

-- The data's name, else the game's, else "Item 12345".
local function DisplayName(id, item)
  local nm = type(item) == "table" and item.name or nil
  if KnownName(nm) then return nm end
  return GameItemName(id) or ("Item " .. tostring(id))
end

-- Why there are no buy and sell targets, or nil when there are.
local function NoAdviceText(rec)
  if type(rec) ~= "table" then return nil end
  if rec.history == false then return "Needs more history" end
  if rec.advice == false then return "Price not reliable" end
  return nil
end

-- "~120 sold/day", with "(early estimate)" while only part of a day is
-- measured, or "sales: measuring" before any sales are measured.
local function SoldText(item)
  if type(ns.SoldPerDay) == "function" then
    local ok, per, early = pcall(ns.SoldPerDay, item)
    if ok then
      if per == nil then return "sales: measuring" end
      return "~" .. Commas(per) .. " sold/day" .. (early and " (early estimate)" or "")
    end
  end
  return "~" .. Commas(item and item.sold24) .. " sold/day"
end

local function ItemIcon(id)
  local CI = _G.C_Item
  if type(CI) == "table" and type(CI.GetItemIconByID) == "function" then
    local ok, ic = pcall(CI.GetItemIconByID, id)
    if ok and ic then return ic end
  end
  if type(_G.GetItemIcon) == "function" then
    local ok, ic = pcall(_G.GetItemIcon, id)
    if ok and ic then return ic end
  end
  return QUESTION
end

local function ItemQualityRGB(id)
  local CI = _G.C_Item
  local q
  if type(CI) == "table" then
    if type(CI.GetItemQualityByID) == "function" then
      local ok, v = pcall(CI.GetItemQualityByID, id)
      if ok and type(v) == "number" then q = v end
    end
    if not q and type(CI.GetItemInfo) == "function" then
      local ok, _, _, v = pcall(CI.GetItemInfo, id)
      if ok and type(v) == "number" then q = v end
    end
    if q and type(CI.GetItemQualityColor) == "function" then
      local ok, r, g, b = pcall(CI.GetItemQualityColor, q)
      if ok and Num(r) and Num(g) and Num(b) then return r, g, b end
    end
  end
  local qc = q and type(_G.ITEM_QUALITY_COLORS) == "table" and _G.ITEM_QUALITY_COLORS[q]
  if type(qc) == "table" and Num(qc.r) then return qc.r, qc.g, qc.b end
  local t = Col("text")
  return t[1], t[2], t[3]
end

-- A note when the data's market is not the one in front of the player
-- (Core.lua decides: other faction, a neutral goblin auction house, Hardcore).
local function MarketMismatch()
  if type(ns.MarketMismatch) ~= "function" then return nil end
  local ok, why = pcall(ns.MarketMismatch)
  if ok and type(why) == "string" and why ~= "" then return why end
  return nil
end

---------------------------------------------------------------------------
-- Pools
---------------------------------------------------------------------------
local Pool = {}
Pool.__index = Pool

local function NewPool(factory, isLine)
  return setmetatable({ factory = factory, items = {}, used = 0, isLine = isLine }, Pool)
end

function Pool:Begin()
  self.used = 0
end

function Pool:Get()
  local i = self.used + 1
  self.used = i
  local o = self.items[i]
  if not o then
    o = self.factory()
    self.items[i] = o
  end
  if not self.isLine then o:ClearAllPoints() end
  o:Show()
  return o
end

function Pool:Finish()
  for i = self.used + 1, #self.items do
    self.items[i]:Hide()
  end
end

---------------------------------------------------------------------------
-- Panel
---------------------------------------------------------------------------
local Chart = {
  built = false, itemID = nil, source = nil, docked = false,
  lastW = nil, lastH = nil, view = nil, hoverIdx = nil, lastError = nil,
  panelH = PANEL_H, dockFrame = nil,
}
ns.ChartPanel = Chart

local function NewTex(parent, layer, sub, path)
  local t = parent:CreateTexture(nil, layer, nil, sub)
  if path then t:SetTexture(path) end
  return t
end

local function NewFS(parent, font, layer)
  local fs = parent:CreateFontString(nil, layer or "OVERLAY", font or "GameFontHighlightSmall")
  fs:SetJustifyH("LEFT")
  return fs
end

local function SetFSColor(fs, c, a)
  fs:SetTextColor(c[1], c[2], c[3], a or 1)
end

local function SimpleTooltip(owner, title, body)
  owner:SetScript("OnEnter", function(self)
    local tt = _G.GameTooltip
    if not tt then return end
    tt:SetOwner(self, "ANCHOR_TOP")
    tt:AddLine(title, 1, 1, 1)
    if body then tt:AddLine(body, nil, nil, nil, true) end
    tt:Show()
  end)
  owner:SetScript("OnLeave", function(self)
    local tt = _G.GameTooltip
    if tt and tt.GetOwner and tt:GetOwner() == self then tt:Hide() end
  end)
end

function Chart:MakeBox(title, color)
  local F = self.frame
  local box = CreateFrame("Frame", nil, F)
  box:SetHeight(BOX_H)
  local p = Col("panel")
  box.bg = NewTex(box, "BACKGROUND", 1)
  box.bg:SetAllPoints(box)
  box.bg:SetColorTexture(p[1], p[2], p[3], 1)
  box.accent = NewTex(box, "BORDER", 0)
  box.accent:SetPoint("TOPLEFT", box, "TOPLEFT", 0, 0)
  box.accent:SetPoint("BOTTOMLEFT", box, "BOTTOMLEFT", 0, 0)
  box.accent:SetWidth(2)
  box.accent:SetColorTexture(color[1], color[2], color[3], 1)
  box.title = NewFS(box, "GameFontNormalSmall")
  box.title:SetPoint("TOPLEFT", box, "TOPLEFT", 10, -5)
  box.title:SetText(title)
  SetFSColor(box.title, color)
  box.value = NewFS(box, "GameFontHighlightLarge")
  box.value:SetPoint("BOTTOMLEFT", box, "BOTTOMLEFT", 10, 5)
  box.sub = NewFS(box, "GameFontHighlightSmall")
  box.sub:SetJustifyH("RIGHT")
  box.sub:SetPoint("RIGHT", box, "RIGHT", -10, 0)
  SetFSColor(box.sub, Col("muted"))
  return box
end

function Chart:Build()
  if self.built then return end
  self.built = true

  local template = (_G.BackdropTemplateMixin and "BackdropTemplate") or nil
  local F = CreateFrame("Frame", "ForeverAHChartFrame", UIParent, template)
  self.frame = F
  F:SetSize(DEFAULT_W, PANEL_H)
  -- Same layer as the auction house and the bags, so it never covers them for
  -- good: clicking a window brings it to the front.
  F:SetFrameStrata("MEDIUM")
  if F.SetToplevel then pcall(F.SetToplevel, F, true) end
  F:SetClampedToScreen(true)
  F:EnableMouse(true)
  F:SetMovable(true)
  if F.SetDontSavePosition then pcall(F.SetDontSavePosition, F, true) end
  F:RegisterForDrag("LeftButton")
  F:SetScript("OnDragStart", function(f) f:StartMoving() end)
  F:SetScript("OnDragStop", function(f)
    f:StopMovingOrSizing()
    if f.SetUserPlaced then pcall(f.SetUserPlaced, f, false) end
    if Chart.docked then
      -- Remember where it was dragged to, relative to the auction house, and
      -- keep it attached there (it moves with the AH).
      Chart:SaveDockOffset()
      Chart:Place()
    else
      Chart:SavePosition()
    end
  end)
  F:SetScript("OnSizeChanged", function(f, w, h)
    w, h = Num(w), Num(h)
    if f:IsShown() and ((w and abs(w - (Chart.lastW or 0)) > 0.5) or (h and abs(h - (Chart.lastH or 0)) > 0.5)) then
      Chart:Redraw()
    end
  end)
  F:SetScript("OnHide", function() Chart:EndHover() end)
  F:Hide()

  local bg, p = Col("bg"), Col("panel")
  if F.SetBackdrop then
    F:SetBackdrop({ bgFile = WHITE, edgeFile = WHITE, edgeSize = 1 })
    F:SetBackdropColor(bg[1], bg[2], bg[3], bg[4] or 0.96)
    F:SetBackdropBorderColor(0.20, 0.20, 0.24, 1)
  else
    local t = NewTex(F, "BACKGROUND", -8)
    t:SetAllPoints(F)
    t:SetColorTexture(bg[1], bg[2], bg[3], bg[4] or 0.96)
  end
  if type(_G.UISpecialFrames) == "table" then
    table.insert(_G.UISpecialFrames, "ForeverAHChartFrame")
  end

  -- Header
  self.icon = NewTex(F, "ARTWORK", 0)
  self.icon:SetSize(24, 24)
  self.icon:SetPoint("TOPLEFT", F, "TOPLEFT", PAD, HEADER_Y + 2)
  self.icon:SetTexCoord(0.07, 0.93, 0.07, 0.93)
  self.name = NewFS(F, "GameFontNormalLarge")
  self.name:SetPoint("LEFT", self.icon, "RIGHT", 8, 0)
  self.name:SetWordWrap(false)
  self.price = NewFS(F, "GameFontHighlightLarge")
  self.price:SetPoint("LEFT", self.name, "RIGHT", 12, 0)
  self.change = NewFS(F, "GameFontHighlight")
  self.change:SetPoint("LEFT", self.price, "RIGHT", 8, 0)

  local close = CreateFrame("Button", nil, F)
  close:SetSize(18, 18)
  close:SetPoint("TOPRIGHT", F, "TOPRIGHT", -6, -6)
  close.text = NewFS(close, "GameFontNormal")
  close.text:SetJustifyH("CENTER")
  close.text:SetPoint("CENTER", close, "CENTER", 0, 1)
  close.text:SetText("x")
  SetFSColor(close.text, Col("muted"))
  local ch = close:CreateTexture(nil, "HIGHLIGHT")
  ch:SetAllPoints(close)
  ch:SetColorTexture(1, 1, 1, 0.10)
  close:SetScript("OnClick", function() Chart:OnCloseClicked() end)
  SimpleTooltip(close, "Hide chart", "Bring it back with the Chart button on the auction house, or /fah chart.")
  self.closeButton = close

  self.tfButtons = {}
  local prev = close
  for i = #TIMEFRAMES, 1, -1 do
    local tf = TIMEFRAMES[i]
    local b = CreateFrame("Button", nil, F)
    b:SetSize(34, 18)
    b:SetPoint("RIGHT", prev, "LEFT", (i == #TIMEFRAMES) and -10 or -3, 0)
    b.bg = NewTex(b, "BACKGROUND", 0)
    b.bg:SetAllPoints(b)
    b.text = NewFS(b, "GameFontHighlightSmall")
    b.text:SetJustifyH("CENTER")
    b.text:SetPoint("CENTER", b, "CENTER", 0, 0)
    b.text:SetText(tf)
    local hl = b:CreateTexture(nil, "HIGHLIGHT")
    hl:SetAllPoints(b)
    hl:SetColorTexture(1, 1, 1, 0.08)
    b:SetScript("OnClick", function() ns.SetChartTF(tf) end)
    b.tf = tf
    self.tfButtons[tf] = b
    prev = b
  end

  -- Target Buy / List At boxes
  self.buyBox = self:MakeBox("Target Buy", Col("buy"))
  self.listBox = self:MakeBox("List At", Col("sell"))

  -- Plot
  local plot = CreateFrame("Frame", nil, F)
  plot:SetPoint("TOPLEFT", F, "TOPLEFT", PAD, -PLOT_TOP)
  plot:EnableMouse(true)
  plot:SetScript("OnEnter", function() Chart:BeginHover() end)
  plot:SetScript("OnLeave", function() Chart:EndHover() end)
  plot.bg = NewTex(plot, "BACKGROUND", 0)
  plot.bg:SetAllPoints(plot)
  plot.bg:SetColorTexture(p[1], p[2], p[3], 0.55)
  self.plot = plot
  self.hasLines = type(plot.CreateLine) == "function"

  self.msg = NewFS(plot, "GameFontHighlight")
  self.msg:SetJustifyH("CENTER")
  self.msg:SetPoint("CENTER", plot, "CENTER", 0, 0)
  self.msg:Hide()

  local function texFactory(layer, sub, path)
    return function() return NewTex(plot, layer, sub, path) end
  end
  local function fsFactory(font)
    return function() return NewFS(plot, font or "GameFontHighlightSmall") end
  end
  self.pools = {}
  local function add(key, pool) self.pools[key] = pool; return pool end
  add("grid", NewPool(texFactory("BORDER", 0)))
  add("vol", NewPool(texFactory("ARTWORK", -4)))
  add("dash", NewPool(texFactory("ARTWORK", 0)))
  add("dot", NewPool(texFactory("ARTWORK", 2, CIRCLE)))
  add("ring", NewPool(function()
    local r = {
      outer = NewTex(plot, "OVERLAY", 3, CIRCLE),
      inner = NewTex(plot, "OVERLAY", 4, CIRCLE),
      core = NewTex(plot, "OVERLAY", 5, CIRCLE),
    }
    r.outer:SetSize(14, 14)
    r.inner:SetSize(9, 9)
    r.core:SetSize(4, 4)
    r.core:SetVertexColor(1, 1, 1, 1)
    function r:Show() self.outer:Show(); self.inner:Show(); self.core:Show() end
    function r:Hide() self.outer:Hide(); self.inner:Hide(); self.core:Hide() end
    function r:ClearAllPoints() self.outer:ClearAllPoints(); self.inner:ClearAllPoints(); self.core:ClearAllPoints() end
    function r:Place(x, y)
      self.outer:SetPoint("CENTER", plot, "BOTTOMLEFT", x, y)
      self.inner:SetPoint("CENTER", plot, "BOTTOMLEFT", x, y)
      self.core:SetPoint("CENTER", plot, "BOTTOMLEFT", x, y)
    end
    return r
  end))
  if self.hasLines then
    add("casing", NewPool(function()
      local l = plot:CreateLine(nil, "OVERLAY", nil, 0)
      l:SetThickness(4)
      l:SetColorTexture(CASING[1], CASING[2], CASING[3], CASING[4])
      return l
    end, true))
    add("trend", NewPool(function()
      local l = plot:CreateLine(nil, "OVERLAY", nil, 1)
      l:SetThickness(2)
      return l
    end, true))
  else
    add("trendDot", NewPool(texFactory("OVERLAY", 1, WHITE)))
  end
  add("ylab", NewPool(fsFactory("GameFontHighlightSmall")))
  add("xlab", NewPool(fsFactory("GameFontHighlightSmall")))
  add("tag", NewPool(fsFactory("GameFontNormalSmall")))

  -- y labels sit outside the plot on the right, so their pool parents to the panel
  self.pools.ylab.factory = function()
    local fs = NewFS(F, "GameFontHighlightSmall")
    SetFSColor(fs, Col("muted"))
    return fs
  end
  self.pools.xlab.factory = function()
    local fs = NewFS(F, "GameFontHighlightSmall")
    fs:SetJustifyH("CENTER")
    SetFSColor(fs, Col("muted"))
    return fs
  end

  -- Hover marks
  self.cross = NewTex(plot, "OVERLAY", 6)
  self.cross:SetColorTexture(1, 1, 1, 0.35)
  self.cross:Hide()
  self.hoverCheap = NewTex(plot, "OVERLAY", 7, CIRCLE)
  self.hoverCheap:SetSize(8, 8)
  self.hoverCheap:Hide()
  self.hoverMarket = NewTex(plot, "OVERLAY", 7, CIRCLE)
  self.hoverMarket:SetSize(8, 8)
  self.hoverMarket:Hide()

  -- Footer, line 1: verdict, meter, margin, volume
  self.verdict = NewFS(F, "GameFontNormal")
  self.verdict:SetPoint("BOTTOMLEFT", F, "BOTTOMLEFT", PAD, 22)
  local meter = CreateFrame("Frame", nil, F)
  meter:SetSize(100, 8)
  meter:SetPoint("LEFT", self.verdict, "RIGHT", 8, 0)
  local neg, mut, pos = Col("neg"), Col("muted"), Col("pos")
  local function mix(a, b, t) return { a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t, a[3] + (b[3] - a[3]) * t } end
  local segColors = { neg, mix(neg, mut, 0.5), mut, mix(mut, pos, 0.5), pos }
  meter.segs = {}
  for i = 1, 5 do
    local s = NewTex(meter, "ARTWORK", 0)
    s:SetSize(19, 6)
    s:SetPoint("LEFT", meter, "LEFT", (i - 1) * 20, 0)
    local c = segColors[i]
    s:SetColorTexture(c[1], c[2], c[3], 0.85)
    meter.segs[i] = s
  end
  meter.needle = NewTex(meter, "OVERLAY", 0)
  meter.needle:SetSize(2, 12)
  meter.needle:SetColorTexture(1, 1, 1, 1)
  self.meter = meter
  self.margin = NewFS(F, "GameFontHighlightSmall")
  self.margin:SetPoint("LEFT", meter, "RIGHT", 12, 0)
  self.volume = NewFS(F, "GameFontHighlightSmall")
  self.volume:SetPoint("LEFT", self.margin, "RIGHT", 14, 0)
  SetFSColor(self.volume, Col("muted"))

  -- Footer, line 2: notes on the left, data age on the right
  self.age = NewFS(F, "GameFontHighlightSmall")
  self.age:SetJustifyH("RIGHT")
  self.age:SetPoint("BOTTOMRIGHT", F, "BOTTOMRIGHT", -PAD, 6)
  SetFSColor(self.age, Col("muted"))
  self.notes = NewFS(F, "GameFontHighlightSmall")
  self.notes:SetPoint("BOTTOMLEFT", F, "BOTTOMLEFT", PAD, 6)
  self.notes:SetPoint("RIGHT", self.age, "LEFT", -12, 0)
  self.notes:SetWordWrap(false)
  SetFSColor(self.notes, Col("muted"))
end

function Chart:SavePosition()
  local F = self.frame
  if not F or self.docked or type(ns.settings) ~= "table" then return end
  local ok, point, _, relPoint, x, y = pcall(F.GetPoint, F, 1)
  if ok and type(point) == "string" and Num(x) and Num(y) then
    ns.settings.chartPoint = { point, relPoint or point, floor(x + 0.5), floor(y + 0.5) }
  end
end

-- The spot the docked panel's top left corner goes to, before any drag.
-- The panel has the dock's effective scale, so both use the same units.
-- The sidebar (Sidebar.lua) sits right of the AH, so a chart docked to the
-- right goes past it instead of on top of it. The sidebar is a child of the
-- AH frame, so its coordinates are in the same units as the dock's.
local function RightEdgeFrame(dock)
  if type(ns.SidebarFrame) == "function" then
    local ok, sb, side = pcall(ns.SidebarFrame)
    if ok and type(sb) == "table" and side == "right" and Num(sb:GetRight()) then return sb end
  end
  return dock
end

local function DockAnchorXY(dock, how)
  if how == "below" then
    local l, b = Num(dock:GetLeft()), Num(dock:GetBottom())
    if l and b then return l, b - DOCK_GAP end
  else
    local r, t = Num(RightEdgeFrame(dock):GetRight()), Num(dock:GetTop())
    if r and t then return r + 6, t end
  end
  return nil
end

local function DockOffset(how)
  local d = type(ns.settings) == "table" and ns.settings.chartDock
  local o = type(d) == "table" and d[how]
  if type(o) == "table" and Num(o[1]) and Num(o[2]) then return o[1], o[2] end
  return 0, 0
end

function Chart:SaveDockOffset()
  local F, dock, how = self.frame, self.dockFrame, self.placement
  if not F or not dock or (how ~= "below" and how ~= "right") or type(ns.settings) ~= "table" then return end
  local bx, by = DockAnchorXY(dock, how)
  local l, t = Num(F:GetLeft()), Num(F:GetTop())
  if not (bx and l and t) then return end
  -- A shrunk panel (crowded screen) measures in its own smaller units.
  local k = self.dockK or 1
  l, t = l * k, t * k
  if type(ns.settings.chartDock) ~= "table" then ns.settings.chartDock = {} end
  ns.settings.chartDock[how] = { floor(l - bx + 0.5), floor(t - by + 0.5) }
end

-- Where the docked panel goes. Under the AH (the user's request): full height
-- when there is room, shorter when the screen is short, then to the right of
-- the AH (past the sidebar), shrunk to fit when the sidebar takes most of
-- that room, else nil (float where the user put it).
-- Returns placement, width, scale ratio, height, shrink (1 = full size).
local MIN_FIT = 0.55   -- smallest shrink before the panel floats instead
local function DockPlacement(dock)
  local ratio = 1
  local es, us = Num(dock:GetEffectiveScale()), Num(UIParent:GetEffectiveScale())
  if es and us and us > 0 then ratio = es / us end
  if ratio < 0.5 or ratio > 3 then ratio = 1 end
  local w = Num(dock:GetWidth()) or DEFAULT_W
  if w < MIN_W then w = DEFAULT_W end
  local bottom, right = Num(dock:GetBottom()), Num(RightEdgeFrame(dock):GetRight())
  if not bottom then return "below", w, ratio, PANEL_H end
  local room = bottom - DOCK_GAP - 4
  if room >= PANEL_H then return "below", w, ratio, PANEL_H end
  if room >= MIN_H then return "below", w, ratio, floor(room) end
  local screenW = Num(UIParent:GetWidth())
  if right and screenW then
    local roomR = screenW / ratio - right - 10
    if roomR >= MIN_W then return "right", min(w, roomR), ratio, PANEL_H, 1 end
    local k = floor(roomR / MIN_W * 100) / 100
    if k >= MIN_FIT then return "right", MIN_W, ratio * k, PANEL_H, k end
  end
  return nil
end

function Chart:Place()
  local F = self.frame
  local dock = type(ns.AH_GetDockFrame) == "function" and ns.AH_GetDockFrame() or nil
  if dock then
    local how, w, ratio, h, k = DockPlacement(dock)
    if how then
      local ox, oy = DockOffset(how)
      k = k or 1
      self.dockK = k
      F:SetScale(ratio)
      F:ClearAllPoints()
      -- Offsets are in the panel's own units, which are 1/k of the dock's.
      if how == "below" then
        F:SetPoint("TOPLEFT", dock, "BOTTOMLEFT", ox / k, (-DOCK_GAP + oy) / k)
      else
        F:SetPoint("TOPLEFT", RightEdgeFrame(dock), "TOPRIGHT", (6 + ox) / k, oy / k)
      end
      F:SetWidth(floor(w + 0.5))
      F:SetHeight(h)
      self.panelH = h
      self.docked = true
      self.dockFrame = dock
      self.placement = how
      return
    end
  end
  self.docked = false
  self.dockFrame = nil
  self.placement = "float"
  self.dockK = 1
  self.panelH = PANEL_H
  F:SetScale(1)
  F:SetHeight(PANEL_H)
  F:ClearAllPoints()
  local p = ns.settings and ns.settings.chartPoint
  if type(p) == "table" and type(p[1]) == "string" then
    F:SetPoint(p[1], UIParent, p[2] or p[1], tonumber(p[3]) or 0, tonumber(p[4]) or 0)
  elseif dock then
    F:SetPoint("BOTTOM", UIParent, "BOTTOM", 0, 10)
  else
    F:SetPoint("CENTER", UIParent, "CENTER", 0, -120)
  end
  F:SetWidth(DEFAULT_W)
end

function Chart:Layout(W, H)
  local F = self.frame
  H = H or PANEL_H
  self.compact = H < COMPACT_H
  self.narrow = W < NARROW_W
  local boxW = floor((W - 2 * PAD - 8) / 2)
  self.buyBox:ClearAllPoints()
  self.buyBox:SetPoint("TOPLEFT", F, "TOPLEFT", PAD, BOX_Y)
  self.buyBox:SetWidth(boxW)
  self.listBox:ClearAllPoints()
  self.listBox:SetPoint("TOPLEFT", F, "TOPLEFT", PAD + boxW + 8, BOX_Y)
  self.listBox:SetWidth(boxW)
  -- A short panel (little room under the AH) leaves the boxes out: the
  -- dashed lines on the plot still carry both prices.
  if self.compact then self.buyBox:Hide(); self.listBox:Hide() else self.buyBox:Show(); self.listBox:Show() end
  local top = self.compact and PLOT_TOP_COMPACT or PLOT_TOP
  self.plot:ClearAllPoints()
  self.plot:SetPoint("TOPLEFT", F, "TOPLEFT", PAD, -top)
  self.plotW = W - 2 * PAD - AXIS_W
  self.plotH = H - top - PLOT_BOTTOM
  self.plot:SetSize(self.plotW, self.plotH)
  self.msg:SetWidth(self.plotW - 40)
  -- A narrow panel (docked right of the AH) leaves out the change and the
  -- volume, so the header and the footer do not run into each other.
  if self.narrow then self.change:Hide(); self.volume:Hide() else self.change:Show(); self.volume:Show() end
end

function Chart:UpdateTFButtons(tf)
  if not self.tfButtons then return end
  local b, p, m = Col("buy"), Col("panel"), Col("muted")
  for key, btn in pairs(self.tfButtons) do
    if key == tf then
      btn.bg:SetColorTexture(b[1], b[2], b[3], 0.35)
      btn.text:SetTextColor(1, 1, 1, 1)
    else
      btn.bg:SetColorTexture(p[1], p[2], p[3], 1)
      btn.text:SetTextColor(m[1], m[2], m[3], 1)
    end
  end
end

function Chart:ShowMessage(text)
  self.msg:SetText(text)
  self.msg:Show()
end

function Chart:SetHeader(id, name, item, s, tf)
  self.icon:SetTexture(id and ItemIcon(id) or QUESTION)
  local shown = tostring(name or "Forever AH")
  local maxLen = self.narrow and 22 or 34
  if #shown > maxLen then shown = shown:sub(1, maxLen - 1) .. "..." end
  self.name:SetText(shown)
  if id then
    self.name:SetTextColor(ItemQualityRGB(id))
  else
    SetFSColor(self.name, Col("text"))
  end
  local now = item and ((Num(item.market) or 0) > 0 and item.market or (Num(item.cheapest) or 0))
  self.price:SetText((now and now > 0) and MoneyI(now) or "")
  self.change:SetText("")
  if s and (s.n or 0) > 1 then
    local first, last
    for i = 1, s.n do
      local v = Num(s.market[i])
      if v and v > 0 then
        first = first or v
        last = v
      end
    end
    if first and last and first > 0 then
      local pct = (last - first) / first * 100
      self.change:SetText(sformat("%+.1f%% %s", pct, tf or ""))
      SetFSColor(self.change, pct >= 0 and Col("pos") or Col("neg"))
    end
  end
end

function Chart:SetBoxes(item, rec)
  local function money(v)
    v = Num(v)
    if v and v > 0 then return MoneyI(v) end
    return "-"
  end
  local cheapNow = rec and rec.cheapestNow or (item and item.cheapest)
  local mktNow = rec and rec.marketNow or (item and item.market)
  -- Too little history (or a price that may be one player's guess): say so
  -- in grey instead of showing targets worked out from a snapshot or two.
  local why = NoAdviceText(rec)
  local valueColor = why and Col("muted") or { 1, 1, 1 }
  SetFSColor(self.buyBox.value, valueColor)
  SetFSColor(self.listBox.value, valueColor)
  self.buyBox.value:SetText(why or (rec and money(rec.buy)) or "-")
  self.listBox.value:SetText(why or (rec and money(rec.listAt)) or "-")
  if item then
    local liveTag = (rec and rec.live) and " (live)" or ""
    self.buyBox.sub:SetText("Cheapest: " .. (((Num(cheapNow) or 0) > 0) and (MoneyI(cheapNow) .. liveTag) or "none listed"))
    self.listBox.sub:SetText("Market: " .. (((Num(mktNow) or 0) > 0) and MoneyI(mktNow) or "none listed"))
  else
    self.buyBox.sub:SetText("")
    self.listBox.sub:SetText("")
  end
end

function Chart:SetFooter(item, rec, extraNote)
  local notes = {}
  -- Advice for a different market would mislead: say so instead of a verdict.
  local mismatch = item and MarketMismatch() or nil
  if mismatch then
    notes[1] = mismatch
    self.verdict:SetText("")
    self.meter:Hide()
    self.margin:SetText("")
  elseif rec and NoAdviceText(rec) then
    -- No targets yet: no verdict colour, no meter, no margin. The notes say why.
    self.verdict:SetText("No advice yet")
    SetFSColor(self.verdict, Col("muted"))
    self.meter:Hide()
    self.margin:SetText("")
    if type(rec.notes) == "table" then
      for _, n in ipairs(rec.notes) do notes[#notes + 1] = tostring(n) end
    end
  elseif rec then
    local verdict = tostring(rec.verdict or "Hold")
    self.verdict:SetText(verdict)
    SetFSColor(self.verdict, Col(VERDICT_COLOR[verdict] or "text"))
    local score = Clamp(Num(rec.score) or 0, -100, 100)
    self.meter.needle:ClearAllPoints()
    self.meter.needle:SetPoint("CENTER", self.meter, "LEFT", (score + 100) / 200 * 99, 0)
    self.meter:Show()
    local margin = Num(rec.margin) or 0
    local pctText = ""
    local pct = Num(rec.marginPct)
    if pct then pctText = sformat(" (%+.1f%%)", pct * 100) end
    self.margin:SetText("Margin after 5% cut: " .. (margin >= 0 and "+" or "") .. MoneyT(margin) .. pctText)
    SetFSColor(self.margin, margin > 0 and Col("pos") or Col("neg"))
    if type(rec.notes) == "table" then
      for _, n in ipairs(rec.notes) do notes[#notes + 1] = tostring(n) end
    end
  else
    self.verdict:SetText(item and "No advice yet" or "")
    SetFSColor(self.verdict, Col("muted"))
    self.meter:Hide()
    self.margin:SetText("")
  end
  if item then
    self.volume:SetText(SoldText(item) .. DOT .. Commas(item.listed) .. " listed")
  else
    self.volume:SetText("")
  end
  if item and type(ns.ItemNotes) == "function" then
    local ok, extra = pcall(ns.ItemNotes, item.id or self.itemID)
    if ok and type(extra) == "table" then
      for _, n in ipairs(extra) do notes[#notes + 1] = tostring(n) end
    end
  end
  if extraNote then notes[#notes + 1] = extraNote end
  self.notes:SetText(tconcat(notes, ". "))
  local age = type(ns.DataAge) == "function" and ns.DataAge() or nil
  local house = tostring((ns.db and ns.db.houseName) or "Alliance Auction House")
  if age then
    self.age:SetText("Data: " .. AgeText(age) .. DOT .. house)
  else
    self.age:SetText("Data: none yet")
  end
end

---------------------------------------------------------------------------
-- Plot drawing
---------------------------------------------------------------------------
local function NiceStep(raw)
  if raw <= 1 then return 1 end
  local mag = 10 ^ floor(math.log10(raw))
  for _, m in ipairs({ 1, 2, 5, 10 }) do
    if m * mag >= raw then return m * mag end
  end
  return 10 * mag
end

local function NextStep(step)
  local mag = 10 ^ floor(math.log10(step) + 1e-9)
  local m = step / mag
  if m < 1.5 then return 2 * mag end
  if m < 3.5 then return 5 * mag end
  return 10 * mag
end

-- Ticks for the y-axis in copper, with labels that never repeat. Steps go
-- 1, 2, 5, 10... upwards from about a sixth of the range; the first one that
-- gives 2 to 7 different labels wins, so there are always at least two.
local function YTicks(lo, hi)
  if not (hi > lo) then return {}, {} end
  local step = NiceStep((hi - lo) / 6)
  if hi >= 1000000 then step = max(step, 100) end -- money text drops copper above 100g
  step = max(1, floor(step + 0.5))
  local fallback
  for _ = 1, 24 do
    local ticks, labels, ok = {}, {}, true
    local v = ceil(lo / step) * step
    while v <= hi and #ticks < 12 do
      ticks[#ticks + 1] = v
      labels[#labels + 1] = MoneyT(v)
      if #labels > 1 and labels[#labels] == labels[#labels - 1] then ok = false end
      v = v + step
    end
    if ok and #ticks >= 2 and #ticks <= 7 then return ticks, labels end
    if ok and #ticks == 1 and not fallback then fallback = { ticks, labels } end
    if #ticks < 2 and fallback then break end
    step = NextStep(step)
  end
  if fallback then return fallback[1], fallback[2] end
  return {}, {}
end
Chart.YTicks = YTicks

function Chart:Segment(x1, y1, x2, y2, alpha)
  local plot = self.plot
  if self.hasLines then
    local cas = self.pools.casing:Get()
    cas:SetStartPoint("BOTTOMLEFT", plot, x1, y1)
    cas:SetEndPoint("BOTTOMLEFT", plot, x2, y2)
    local ln = self.pools.trend:Get()
    ln:SetColorTexture(TREND[1], TREND[2], TREND[3], 0.9 * alpha)
    ln:SetStartPoint("BOTTOMLEFT", plot, x1, y1)
    ln:SetEndPoint("BOTTOMLEFT", plot, x2, y2)
  else
    local dx, dy = x2 - x1, y2 - y1
    local steps = max(1, ceil(sqrt(dx * dx + dy * dy) / 2))
    for j = 0, steps - 1 do
      local t = j / steps
      local sq = self.pools.trendDot:Get()
      sq:SetVertexColor(TREND[1], TREND[2], TREND[3], 0.9 * alpha)
      sq:SetSize(2, 2)
      sq:SetPoint("CENTER", plot, "BOTTOMLEFT", x1 + dx * t, y1 + dy * t)
    end
  end
end

function Chart:Dashed(y, color, label, above)
  local plot, plotW = self.plot, self.plotW
  local x = 0
  while x < plotW do
    local w = min(DASH, plotW - x)
    local t = self.pools.dash:Get()
    t:SetColorTexture(color[1], color[2], color[3], 0.85)
    t:SetSize(w, 1)
    t:SetPoint("BOTTOMLEFT", plot, "BOTTOMLEFT", x, y)
    x = x + DASH + DASH_GAP
  end
  local fs = self.pools.tag:Get()
  fs:SetJustifyH("RIGHT")
  fs:SetText(label)
  SetFSColor(fs, color)
  if above then
    fs:SetPoint("BOTTOMRIGHT", plot, "BOTTOMLEFT", plotW - 2, y + 2)
  else
    fs:SetPoint("TOPRIGHT", plot, "BOTTOMLEFT", plotW - 2, y - 2)
  end
end

function Chart:DrawPlot(s, rec, tf)
  local n = s.n
  local plot, plotW, plotH = self.plot, self.plotW, self.plotH
  local cheap, market, sold, times = s.cheap or {}, s.market or {}, s.sold or {}, s.times or {}

  local vmin, vmax = huge, -huge
  local lowIdx, lowVal, highIdx, highVal = nil, huge, nil, -huge
  for i = 1, n do
    local c, m = Num(cheap[i]), Num(market[i])
    if c then
      if c < vmin then vmin = c end
      if c > vmax then vmax = c end
      if c < lowVal then lowVal, lowIdx = c, i end
    end
    if m then
      if m < vmin then vmin = m end
      if m > vmax then vmax = m end
      if m > highVal then highVal, highIdx = m, i end
    end
  end
  if vmin == huge then
    self:ShowMessage("Nothing was listed in this timeframe. Try a longer one.")
    return
  end
  local buyT = rec and Num(rec.buy)
  local listT = rec and Num(rec.listAt)
  if buyT and buyT <= 0 then buyT = nil end
  if listT and listT <= 0 then listT = nil end
  for _, v in ipairs({ buyT or vmin, listT or vmin }) do
    if v < vmin then vmin = v end
    if v > vmax then vmax = v end
  end

  local span = vmax - vmin
  local pad = span > 0 and span * 0.08 or max(1, vmax * 0.05)
  local lo, hi = max(0, vmin - pad), vmax + pad
  if hi - lo < 2 then hi = lo + 2 end
  -- Above 100g the labels leave out the copper, so a range of under a silver
  -- or two gets just one label. Widen the range around its middle until the
  -- axis has at least two.
  local ticks, labels = YTicks(lo, hi)
  for _ = 1, 16 do
    if #ticks >= 2 then break end
    local mid, half = (lo + hi) / 2, max(hi - lo, 2)
    lo, hi = max(0, mid - half), mid + half
    ticks, labels = YTicks(lo, hi)
  end

  local volH = floor(plotH * 0.20)
  local pBottom, pTop = volH + 6, plotH - 6
  local pH = pTop - pBottom
  local function Y(v) return pBottom + (v - lo) / (hi - lo) * pH end
  local stepX = n > 1 and plotW / (n - 1) or 0
  local function X(i)
    if n > 1 then return (i - 1) * stepX end
    return plotW / 2
  end
  self.view = { n = n, s = s, X = X, Y = Y, stepX = stepX, tf = tf }

  -- Grid and y-axis labels
  local muted = Col("muted")
  for k, v in ipairs(ticks) do
    local y = Y(v)
    local g = self.pools.grid:Get()
    g:SetColorTexture(muted[1], muted[2], muted[3], 0.14)
    g:SetSize(plotW, 1)
    g:SetPoint("BOTTOMLEFT", plot, "BOTTOMLEFT", 0, y)
    local fs = self.pools.ylab:Get()
    fs:SetText(labels[k])
    fs:SetPoint("LEFT", plot, "BOTTOMLEFT", plotW + 6, y)
  end

  -- X-axis labels, counted back from the newest point
  local lstep = TF_LABEL_STEP[tf] or 4
  if stepX > 0 then lstep = max(lstep, ceil(72 / stepX)) end
  local i, count = n, 0
  while i >= 1 and count < 12 do
    local t = Num(times[i])
    if t then
      local fs = self.pools.xlab:Get()
      fs:SetText(AxisTimeLabel(t, s.step, tf))
      fs:SetPoint("TOP", plot, "BOTTOMLEFT", Clamp(X(i), 24, max(24, plotW - 24)), -3)
    end
    i = i - lstep
    count = count + 1
  end

  -- Estimated sold bars along the bottom
  local maxSold = 0
  for k = 1, n do
    local v = Num(sold[k])
    if v and v > maxSold then maxSold = v end
  end
  if maxSold > 0 then
    local b = Col("buy")
    local bw = max(1, min(10, stepX > 0 and stepX * 0.6 or 6))
    for k = 1, n do
      local v = Num(sold[k])
      if v and v > 0 then
        local bar = self.pools.vol:Get()
        bar:SetColorTexture(b[1], b[2], b[3], 0.30)
        bar:SetSize(bw, max(1, v / maxSold * volH))
        bar:SetPoint("BOTTOM", plot, "BOTTOMLEFT", X(k), 0)
      end
    end
  end

  -- Dashed target lines, labelled at the right end
  local buyC, sellC = Col("buy"), Col("sell")
  if buyT or listT then
    local yb, yl = buyT and Y(buyT), listT and Y(listT)
    local listAbove = true
    if yb and yl and yl < yb then listAbove = false end
    if listT then self:Dashed(yl, sellC, "List At " .. MoneyT(listT), listAbove) end
    if buyT then self:Dashed(yb, buyC, "Target Buy " .. MoneyT(buyT), not listAbove) end
  end

  -- Dots: market (blue) first, cheapest (gold) on top. Older points fade.
  local size = n > 1 and Clamp(stepX * 0.8, 3, 5) or 6
  local function alphaAt(k)
    if n <= 1 then return 1 end
    return 0.45 + 0.55 * (k - 1) / (n - 1)
  end
  for _, pair in ipairs({ { market, sellC }, { cheap, buyC } }) do
    local arr, c = pair[1], pair[2]
    for k = 1, n do
      local v = Num(arr[k])
      if v then
        local d = self.pools.dot:Get()
        d:SetVertexColor(c[1], c[2], c[3], alphaAt(k))
        d:SetSize(size, size)
        d:SetPoint("CENTER", plot, "BOTTOMLEFT", X(k), Y(v))
      end
    end
  end

  -- Smoothed trend: EMA of the mid price. Each side carries over short gaps;
  -- a long dead stretch resets it. The drawn line lifts over any gap.
  if n > 2 then
    local period = max(2, floor(n * 0.08 + 0.5))
    local k = 2 / (period + 1)
    local carryMax = max(3, floor(n * 0.02 + 0.5))
    local ema, lastLo, lastHi, miss = nil, nil, nil, 0
    local smooth = {}
    for j = 1, n do
      local c, m = Num(cheap[j]), Num(market[j])
      if c then lastLo = c end
      if m then lastHi = m end
      if not c and not m then
        miss = miss + 1
        if miss > carryMax then lastLo, lastHi, ema = nil, nil, nil end
      else
        miss = 0
        if lastLo and lastHi then
          local mid = (lastLo + lastHi) / 2
          ema = ema and (mid * k + ema * (1 - k)) or mid
          smooth[j] = ema
        end
      end
    end
    for j = 2, n do
      if smooth[j] and smooth[j - 1] then
        self:Segment(X(j - 1), Y(smooth[j - 1]), X(j), Y(smooth[j]), alphaAt(j))
      end
    end
  end

  -- Period high (market, blue) and low (cheapest, gold) rings
  if n >= 3 then
    local bg = Col("bg")
    if highIdx then
      local r = self.pools.ring:Get()
      r.outer:SetVertexColor(sellC[1], sellC[2], sellC[3], 0.95)
      r.inner:SetVertexColor(bg[1], bg[2], bg[3], 1)
      r:Place(X(highIdx), Y(highVal))
    end
    if lowIdx then
      local r = self.pools.ring:Get()
      r.outer:SetVertexColor(buyC[1], buyC[2], buyC[3], 0.95)
      r.inner:SetVertexColor(bg[1], bg[2], bg[3], 1)
      r:Place(X(lowIdx), Y(lowVal))
    end
  end
end

function Chart:Draw(tf)
  local id = self.itemID
  if not HasData() then
    self:SetHeader(nil, "Forever AH")
    self:SetBoxes(nil, nil)
    self:SetFooter(nil, nil)
    self:ShowMessage("No price data yet.\nDouble-click Update-WoW-Prices.bat, then type /reload in game.")
    return
  end
  if not id then
    self:SetHeader(nil, "Forever AH")
    self:SetBoxes(nil, nil)
    self:SetFooter(nil, nil)
    self:ShowMessage("Search for an item at the auction house, or type /fah chart and an item name.")
    return
  end
  local item = type(ns.GetItem) == "function" and ns.GetItem(id) or nil
  if not item then
    local gameName = GameItemName(id)
    local name = gameName or ("item " .. id)
    self:SetHeader(id, gameName or ("Item " .. id))
    self:SetBoxes(nil, nil)
    self:SetFooter(nil, nil)
    self:ShowMessage("No recent listings for " .. name .. ".")
    return
  end
  local s = type(ns.GetSeries) == "function" and ns.GetSeries(id, tf) or nil
  -- The live cheapest listing (when the open AH has loaded it) beats the hourly data.
  local live = type(ns.LiveCheapest) == "function" and ns.LiveCheapest(id) or nil
  local rec = type(ns.Recommend) == "function" and ns.Recommend(id, tf, live) or nil
  self:SetHeader(id, DisplayName(id, item), item, s, tf)
  self:SetBoxes(item, rec)
  self:SetFooter(item, rec)
  local n = type(s) == "table" and (Num(s.n) or 0) or 0
  if n < 2 then
    -- A new data set has one price per item: a chart needs at least two.
    local daily = type(s) == "table" and (s.step or 0) >= 86400
    local msg = "Collecting price history. This chart fills in as " .. (daily and "daily" or "hourly")
      .. " prices arrive (" .. (n > 0 and tostring(n) or "none") .. " so far)."
    if daily and type(ns.GetSeries) == "function" then
      local hs = ns.GetSeries(id, "5D")
      if type(hs) == "table" and (Num(hs.n) or 0) >= 2 then msg = msg .. "\nThe 1D, 5D and 1W charts already have hourly prices." end
    end
    self:ShowMessage(msg)
    return
  end
  self:DrawPlot(s, rec, tf)
end

function Chart:Redraw()
  if not self.built then return end
  local F = self.frame
  local W = Num(F:GetWidth()) or DEFAULT_W
  if W < MIN_W then W = MIN_W end
  local H = self.panelH or PANEL_H
  self.lastW, self.lastH = W, H
  self:EndHover()
  self:Layout(W, H)
  for _, pool in pairs(self.pools) do pool:Begin() end
  self.msg:Hide()
  self.view = nil
  local tf = CurrentTF()
  self:UpdateTFButtons(tf)
  local ok, err = pcall(self.Draw, self, tf)
  if not ok then
    self.lastError = err
    for _, pool in pairs(self.pools) do pool:Begin() end
    self.view = nil
    self:ShowMessage("Could not draw this chart. Type /reload, and tell Grant if it keeps happening.")
    if not self.errorPrinted then
      self.errorPrinted = true
      Print("chart error: " .. tostring(err))
    end
  end
  for _, pool in pairs(self.pools) do pool:Finish() end
end

---------------------------------------------------------------------------
-- Hover
---------------------------------------------------------------------------
function Chart:BeginHover()
  if not self.view or not self.plot then return end
  self.hoverIdx = nil
  self.plot:SetScript("OnUpdate", function() Chart:HoverUpdate() end)
end

function Chart:EndHover()
  if not self.plot then return end
  self.plot:SetScript("OnUpdate", nil)
  self.hoverIdx = nil
  self.cross:Hide()
  self.hoverCheap:Hide()
  self.hoverMarket:Hide()
  local tt = _G.GameTooltip
  if tt and tt.GetOwner and tt:GetOwner() == self.plot then tt:Hide() end
end

function Chart:HoverUpdate()
  local v = self.view
  if not v or (v.n or 0) < 1 then return end
  local gcp = _G.GetCursorPosition
  if type(gcp) ~= "function" then return end
  local cx = Num((gcp()))
  local scale = Num(self.plot:GetEffectiveScale())
  local left = Num(self.plot:GetLeft())
  if not cx or not scale or scale <= 0 or not left then return end
  local idx = 1
  if v.n > 1 and v.stepX > 0 then idx = floor((cx / scale - left) / v.stepX + 0.5) + 1 end
  idx = Clamp(idx, 1, v.n)
  if idx ~= self.hoverIdx then self:HoverAt(idx) end
end

function Chart:HoverAt(idx)
  local v = self.view
  if not v or not idx or idx < 1 or idx > (v.n or 0) then return end
  self.hoverIdx = idx
  local s, plot = v.s, self.plot
  local x = v.X(idx)
  self.cross:ClearAllPoints()
  self.cross:SetSize(1, self.plotH)
  self.cross:SetPoint("BOTTOM", plot, "BOTTOMLEFT", x, 0)
  self.cross:Show()
  local c, m = Num(s.cheap[idx]), Num(s.market[idx])
  local b, sl = Col("buy"), Col("sell")
  if c then
    self.hoverCheap:ClearAllPoints()
    self.hoverCheap:SetVertexColor(b[1], b[2], b[3], 1)
    self.hoverCheap:SetPoint("CENTER", plot, "BOTTOMLEFT", x, v.Y(c))
    self.hoverCheap:Show()
  else
    self.hoverCheap:Hide()
  end
  if m then
    self.hoverMarket:ClearAllPoints()
    self.hoverMarket:SetVertexColor(sl[1], sl[2], sl[3], 1)
    self.hoverMarket:SetPoint("CENTER", plot, "BOTTOMLEFT", x, v.Y(m))
    self.hoverMarket:Show()
  else
    self.hoverMarket:Hide()
  end
  local tt = _G.GameTooltip
  if not tt then return end
  tt:SetOwner(plot, "ANCHOR_NONE")
  tt:ClearLines() -- the tooltip stays open while the mouse moves: start it fresh each time
  tt:ClearAllPoints()
  if x > self.plotW * 0.6 then
    tt:SetPoint("BOTTOMRIGHT", plot, "BOTTOMLEFT", x - 12, self.plotH * 0.35)
  else
    tt:SetPoint("BOTTOMLEFT", plot, "BOTTOMLEFT", x + 12, self.plotH * 0.35)
  end
  tt:AddLine(TooltipTime(s.times[idx], s.step), 1, 1, 1)
  tt:AddDoubleLine("Cheapest", c and MoneyI(c) or "nothing listed", b[1], b[2], b[3], 1, 1, 1)
  tt:AddDoubleLine("Market", m and MoneyI(m) or "nothing listed", sl[1], sl[2], sl[3], 1, 1, 1)
  local mu = Col("muted")
  local soldLabel = ((s.step or 0) >= 86400) and "Est. sold that day" or "Est. sold that hour"
  tt:AddDoubleLine(soldLabel, Commas(s.sold and s.sold[idx] or 0), mu[1], mu[2], mu[3], 1, 1, 1)
  tt:Show()
end

---------------------------------------------------------------------------
-- Buttons
---------------------------------------------------------------------------
function Chart:OnCloseClicked()
  if self.docked and type(ns.settings) == "table" then
    ns.settings.chart = false
    Print("chart hidden. Click Chart at the top of the auction house to show it again.")
  end
  ns.HideChart()
end

---------------------------------------------------------------------------
-- Public API (see the shared contract)
---------------------------------------------------------------------------
function ns.ShowChart(itemID, source)
  Chart:Build()
  local F = Chart.frame
  local id = tonumber(itemID)
  if not id then id = Chart.itemID or (ns.settings and tonumber(ns.settings.lastItem)) end
  local wasShown = F:IsShown()
  local wasDocked, wasPlacement = Chart.docked, Chart.placement
  local changed = id ~= Chart.itemID
  Chart.itemID = id
  Chart.source = source or (wasShown and Chart.source) or "standalone"
  if id and type(ns.settings) == "table" then ns.settings.lastItem = id end
  -- Showing the chart while the auction house is open (Chart button, /fah chart)
  -- turns the AH chart back on, so it follows searches again.
  if type(ns.AH_IsOpen) == "function" and ns.AH_IsOpen() and type(ns.settings) == "table" then
    ns.settings.chart = true
  end
  Chart:Place()
  F:Show()
  local W = Num(F:GetWidth()) or DEFAULT_W
  if changed or not wasShown or wasDocked ~= Chart.docked or wasPlacement ~= Chart.placement
    or abs(W - (Chart.lastW or 0)) > 0.5 or abs((Chart.panelH or PANEL_H) - (Chart.lastH or 0)) > 0.5 then
    Chart:Redraw()
  end
end

function ns.HideChart()
  if not Chart.built then return end
  Chart:EndHover()
  Chart.frame:Hide()
end

-- At the auction house this works like the Chart button there: hiding the
-- docked chart turns it off for the AH, showing it turns it back on (so it
-- follows your searches again).
function ns.ToggleChart()
  local atAH = type(ns.AH_IsOpen) == "function" and ns.AH_IsOpen()
  if Chart.built and Chart.frame:IsShown() then
    if atAH and Chart.docked and type(ns.settings) == "table" then ns.settings.chart = false end
    ns.HideChart()
    return false
  end
  ns.ShowChart(nil, atAH and "ah" or "standalone")
  return true
end

function ns.SetChartTF(tf)
  if type(tf) ~= "string" then return end
  tf = tf:upper()
  if not TF_VALID[tf] then return end
  if type(ns.settings) == "table" then ns.settings.chartTF = tf end
  if Chart.built then
    if Chart.frame:IsShown() then
      Chart:Redraw()
    else
      Chart:UpdateTFButtons(tf)
    end
  end
  -- The Use List At buttons follow the chart's timeframe: keep their label
  -- (and the price they fill) the same as the chart's List At box.
  if type(ns.AH_RefreshButtons) == "function" then pcall(ns.AH_RefreshButtons) end
end

-- Redraws the chart in place (new live prices for the item it shows).
function ns.ChartRefresh()
  if Chart.built and Chart.frame:IsShown() then Chart:Redraw() end
end

-- Helpers for AH.lua
function ns.ChartIsShown()
  return Chart.built and Chart.frame:IsShown() or false
end

function ns.ChartIsDocked()
  return Chart.built and Chart.frame:IsShown() and Chart.docked or false
end

function ns.ChartItem()
  return Chart.itemID
end

-- Who opened the chart last: "ah", "slash", "standalone" or nil.
function ns.ChartSource()
  return Chart.source
end

-- Re-check where the panel belongs (AH shown, moved, rescaled, closed).
function ns.ChartRefreshPlacement()
  if not Chart.built or not Chart.frame:IsShown() then return end
  local wasDocked, wasPlacement = Chart.docked, Chart.placement
  Chart:Place()
  local W = Num(Chart.frame:GetWidth()) or DEFAULT_W
  if wasDocked ~= Chart.docked or wasPlacement ~= Chart.placement or abs(W - (Chart.lastW or 0)) > 0.5
    or abs((Chart.panelH or PANEL_H) - (Chart.lastH or 0)) > 0.5 then
    Chart:Redraw()
  end
end

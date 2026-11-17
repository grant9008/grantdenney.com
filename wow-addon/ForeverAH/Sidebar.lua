local ADDON_NAME, ns = ...
-- Forever AH: the auction house sidebar.
-- A slim panel attached to the right edge of the game's own auction house
-- window, PocketGE plugin style. It opens and closes with the auction house
-- (it is a child of that window), so there is no extra window to manage.
--   Flip ideas : the best buy-low, sell-high items right now, after the 5% cut
--   Watchlist  : a starter list of staple trade goods that you add to and trim
-- Clicking a row searches that item in the game's auction house, so the
-- listings and the price chart come up as if you had typed it yourself.

ns = ns or {}

local _G = _G
local type, tonumber, tostring, pairs, ipairs, pcall, next = type, tonumber, tostring, pairs, ipairs, pcall, next
local floor, max, min, abs, huge = math.floor, math.max, math.min, math.abs, math.huge
local sort, tinsert, tremove = table.sort, table.insert, table.remove
local sformat = string.format

local W = 250                -- sidebar width
local PAD = 8
local HEADER_H = 40
local TITLE_H = 18
local FLIP_H = 34
local WATCH_H = 22
local ADD_H = 22
local MAX_FLIPS = 5
local MIN_H = 300

-- Flip idea rules
local FLIP_TF = "1W"         -- a week of hourly prices to judge buy and sell levels
local CANDIDATES = 60        -- how many quick-ranked items get the full price advice
local VOL_CAP = 200          -- a day's flip is capped at this many units
local MIN_SOLD = 60          -- est. sold per day for a confident idea
local MIN_SOLD_RELAX = 15    -- fallback when too few items pass the stricter bar
local MIN_MARGIN_PCT = 0.03  -- at least 3% profit on the buy price after the cut
local HIDE_SECONDS = 24 * 3600

-- Starter watchlist: the same staples as grantdenney.com/wow's default favorites.
local DEFAULT_WATCH = {
  14047, -- Runecloth
  4338,  -- Mageweave Cloth
  4306,  -- Silk Cloth
  2592,  -- Wool Cloth
  2589,  -- Linen Cloth
  2772,  -- Iron Ore
  3858,  -- Mithril Ore
  10620, -- Thorium Ore
  2447,  -- Peacebloom
  765,   -- Silverleaf
  4304,  -- Thick Leather
  10940, -- Strange Dust
}
ns.DEFAULT_WATCH = DEFAULT_WATCH

local WHITE = "Interface\\Buttons\\WHITE8X8"
local QUESTION = "Interface\\Icons\\INV_Misc_QuestionMark"

local FALLBACK_COLORS = {
  buy = { 0.898, 0.722, 0.259 }, sell = { 0.22, 0.74, 0.97 },
  pos = { 0.25, 0.80, 0.45 }, neg = { 0.90, 0.33, 0.33 },
  muted = { 0.55, 0.51, 0.45 }, text = { 0.90, 0.88, 0.84 },
  bg = { 0.043, 0.055, 0.078, 0.96 }, panel = { 0.07, 0.08, 0.10, 0.96 },
}

---------------------------------------------------------------------------
-- Small helpers
---------------------------------------------------------------------------
local function Col(key)
  local c = ns.COLORS and ns.COLORS[key]
  if type(c) == "table" then return c end
  return FALLBACK_COLORS[key] or FALLBACK_COLORS.text
end

local function Hex(c)
  if type(ns.Hex) == "function" then return ns.Hex(c) end
  return sformat("ff%02x%02x%02x", floor(c[1] * 255 + 0.5), floor(c[2] * 255 + 0.5), floor(c[3] * 255 + 0.5))
end

local function Paint(text, key)
  return "|c" .. Hex(Col(key)) .. tostring(text) .. "|r"
end

local function IsSecret(v)
  local f = _G.issecretvalue
  if type(f) ~= "function" then return false end
  local ok, r = pcall(f, v)
  return ok and r == true
end

local function Num(v)
  if type(v) ~= "number" or IsSecret(v) then return nil end
  if v ~= v or v == huge or v == -huge then return nil end
  return v
end

local function Print(msg)
  if type(ns.Print) == "function" then return ns.Print(msg) end
  local cf = _G.DEFAULT_CHAT_FRAME
  if type(cf) == "table" and cf.AddMessage then cf:AddMessage("Forever AH: " .. tostring(msg)) end
end

local function After(sec, fn)
  local T = _G.C_Timer
  if type(T) == "table" and type(T.After) == "function" then
    T.After(sec, fn)
  else
    fn()
  end
end

local function Now()
  if type(ns.Now) == "function" then return ns.Now() end
  return type(_G.time) == "function" and _G.time() or 0
end

-- Short money for a narrow column: "1g 20s", "123g", "20s 5c", "5c".
local function ShortMoney(c)
  c = floor((Num(c) or 0) + 0.5)
  local neg = c < 0
  if neg then c = -c end
  local g, s, k = floor(c / 10000), floor(c / 100) % 100, c % 100
  local out
  if g >= 100 then
    out = g .. "g"
  elseif g > 0 then
    out = s > 0 and (g .. "g " .. s .. "s") or (g .. "g")
  elseif s > 0 then
    out = k > 0 and (s .. "s " .. k .. "c") or (s .. "s")
  else
    out = k .. "c"
  end
  return neg and ("-" .. out) or out
end
ns.ShortMoney = ShortMoney

local function Money(c)
  if type(ns.MoneyText) == "function" then return ns.MoneyText(c) end
  return ShortMoney(c)
end

local function Count(n)
  n = floor((Num(n) or 0) + 0.5)
  if n >= 10000 then return floor(n / 1000) .. "k" end
  if n >= 1000 then return sformat("%.1fk", n / 1000) end
  return tostring(n)
end

local function HasData()
  local db = ns.db
  return type(db) == "table" and (tonumber(db.updated) or 0) > 0 and type(db.items) == "table" and next(db.items) ~= nil
end

local function GameItemName(id)
  local CI = _G.C_Item
  if type(CI) == "table" then
    if type(CI.GetItemNameByID) == "function" then
      local ok, n = pcall(CI.GetItemNameByID, id)
      if ok and type(n) == "string" and n ~= "" then return n end
    end
    if type(CI.GetItemInfo) == "function" then
      local ok, n = pcall(CI.GetItemInfo, id)
      if ok and type(n) == "string" and n ~= "" then return n end
    end
  end
  if type(_G.GetItemInfo) == "function" then
    local ok, n = pcall(_G.GetItemInfo, id)
    if ok and type(n) == "string" and n ~= "" then return n end
  end
  return nil
end

local function ItemName(id)
  local it = type(ns.GetItem) == "function" and ns.GetItem(id) or nil
  if it and type(it.name) == "string" and it.name ~= "" then return it.name end
  return GameItemName(id) or ("Item " .. tostring(id))
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

local function QualityRGB(id)
  local CI = _G.C_Item
  local q
  if type(CI) == "table" and type(CI.GetItemQualityByID) == "function" then
    local ok, v = pcall(CI.GetItemQualityByID, id)
    if ok and type(v) == "number" then q = v end
  end
  if q and type(CI) == "table" and type(CI.GetItemQualityColor) == "function" then
    local ok, r, g, b = pcall(CI.GetItemQualityColor, q)
    if ok and Num(r) and Num(g) and Num(b) then return r, g, b end
  end
  local qc = q and type(_G.ITEM_QUALITY_COLORS) == "table" and _G.ITEM_QUALITY_COLORS[q]
  if type(qc) == "table" and Num(qc.r) then return qc.r, qc.g, qc.b end
  local t = Col("text")
  return t[1], t[2], t[3]
end

local function CurrentTF()
  local tf = ns.settings and ns.settings.chartTF
  if type(ns.NormalizeTF) == "function" then tf = ns.NormalizeTF(tf) end
  return tf or "1D"
end

local function Live(id)
  if type(ns.LiveCheapest) ~= "function" then return nil end
  local ok, v = pcall(ns.LiveCheapest, id)
  return ok and Num(v) or nil
end

local function Advice(id, tf)
  if type(ns.Recommend) ~= "function" then return nil end
  local ok, r = pcall(ns.Recommend, id, tf or CurrentTF(), Live(id))
  if ok and type(r) == "table" then return r end
  return nil
end

-- Market change over the last day, as a fraction, or nil.
local function DayChange(id)
  if type(ns.GetSeries) ~= "function" then return nil end
  local ok, s = pcall(ns.GetSeries, id, "1D")
  if not ok or type(s) ~= "table" or (s.n or 0) < 2 then return nil end
  local first, last
  for i = 1, s.n do
    local v = Num(s.market[i])
    if v and v > 0 then
      first = first or v
      last = v
    end
  end
  if not first or not last or first <= 0 then return nil end
  return (last - first) / first
end

---------------------------------------------------------------------------
-- Watchlist storage (saved with the addon's settings)
---------------------------------------------------------------------------
local function S()
  return type(ns.settings) == "table" and ns.settings or nil
end

function ns.GetWatchlist()
  local st = S()
  if not st then return {} end
  if type(st.watchlist) ~= "table" then
    local list = {}
    for i, id in ipairs(DEFAULT_WATCH) do list[i] = id end
    st.watchlist = list
  end
  -- Drop anything that is not a whole item id (hand-edited saved variables).
  local clean, seen = {}, {}
  for _, id in ipairs(st.watchlist) do
    local n = tonumber(id)
    if n and n > 0 and n == floor(n) and not seen[n] then
      clean[#clean + 1] = n
      seen[n] = true
    end
  end
  st.watchlist = clean
  return clean
end

function ns.IsWatched(id)
  id = tonumber(id)
  if not id then return false end
  for _, v in ipairs(ns.GetWatchlist()) do
    if v == id then return true end
  end
  return false
end

local Refresh -- forward

function ns.AddWatch(id)
  id = tonumber(id)
  if not id or id <= 0 then return false end
  local list = ns.GetWatchlist()
  for _, v in ipairs(list) do
    if v == id then return false end
  end
  tinsert(list, 1, id)   -- newest on top, like the PocketGE watchlist
  if Refresh then Refresh() end
  return true
end

function ns.RemoveWatch(id)
  id = tonumber(id)
  local list = ns.GetWatchlist()
  for i, v in ipairs(list) do
    if v == id then
      tremove(list, i)
      if Refresh then Refresh() end
      return true
    end
  end
  return false
end

function ns.MoveWatch(id, delta)
  id = tonumber(id)
  local list = ns.GetWatchlist()
  for i, v in ipairs(list) do
    if v == id then
      local j = i + delta
      if j < 1 or j > #list then return false end
      list[i], list[j] = list[j], list[i]
      if Refresh then Refresh() end
      return true
    end
  end
  return false
end

function ns.ToggleWatch(id)
  if ns.IsWatched(id) then ns.RemoveWatch(id) return false end
  ns.AddWatch(id)
  return true
end

---------------------------------------------------------------------------
-- Flip ideas
---------------------------------------------------------------------------
local candCache = { key = nil, list = nil }

local function HiddenIdeas()
  local st = S()
  if not st then return {} end
  if type(st.flipHidden) ~= "table" then st.flipHidden = {} end
  local t = Now()
  for id, untilT in pairs(st.flipHidden) do
    if type(untilT) ~= "number" or untilT <= t then st.flipHidden[id] = nil end
  end
  return st.flipHidden
end

function ns.HideIdea(id)
  local st = S()
  id = tonumber(id)
  if not st or not id then return end
  HiddenIdeas()[id] = Now() + HIDE_SECONDS
  ns.SidebarPanel.flipsDirty = true
  if Refresh then Refresh() end
end

-- The quick pass: the items where the most money changes hands (price times
-- a day's sales), since a flip needs buyers and sellers first. The spread is
-- judged in the full pass from the price history, because the gap between
-- today's cheapest and market price is usually only a few percent.
-- Cached until the data changes.
local function Candidates()
  local db = ns.db
  if type(db) ~= "table" or type(db.items) ~= "table" or type(ns.GetItem) ~= "function" then return {} end
  local key = tostring(db.updated)
  if candCache.key == key and candCache.list then return candCache.list end
  local cand = {}
  for id in pairs(db.items) do
    local n = tonumber(id)
    local it = n and ns.GetItem(n)
    if it and it.cheapest > 0 and it.market > 0 and it.sold24 >= MIN_SOLD_RELAX and it.listed >= 2 then
      cand[#cand + 1] = { id = n, q = it.market * min(it.sold24, VOL_CAP) }
    end
  end
  sort(cand, function(a, b)
    if a.q ~= b.q then return a.q > b.q end
    return a.id < b.id
  end)
  local list = {}
  for i = 1, min(#cand, CANDIDATES) do list[i] = cand[i].id end
  candCache.key, candCache.list = key, list
  return list
end

-- The full pass: price advice for each candidate. Returns up to `limit` ideas,
-- best first: { id, buy, sell, profit, perDay, sold, verdict, rec }.
function ns.FlipIdeas(limit)
  limit = limit or MAX_FLIPS
  if not HasData() then return {} end
  local hidden = HiddenIdeas()
  local strict, loose = {}, {}
  for _, id in ipairs(Candidates()) do
    if not hidden[id] then
      local r = Advice(id, FLIP_TF)
      if r and r.history and (r.margin or 0) > 0 and (r.buy or 0) > 0 and (r.marginPct or 0) >= MIN_MARGIN_PCT
        and not r.far and not r.vendorBetter and r.verdict ~= "Sell" and r.verdict ~= "Strong Sell" then
        local sold = r.sold24 or 0
        local idea = {
          id = id, buy = r.buy, sell = r.sell, profit = r.margin,
          perDay = r.margin * min(sold, VOL_CAP), sold = sold, verdict = r.verdict, rec = r,
        }
        if sold >= MIN_SOLD then strict[#strict + 1] = idea else loose[#loose + 1] = idea end
      end
    end
  end
  local function better(a, b)
    if a.perDay ~= b.perDay then return a.perDay > b.perDay end
    return a.id < b.id
  end
  sort(strict, better)
  sort(loose, better)
  local out = {}
  for _, x in ipairs(strict) do
    if #out >= limit then break end
    out[#out + 1] = x
  end
  for _, x in ipairs(loose) do
    if #out >= limit then break end
    x.thin = true
    out[#out + 1] = x
  end
  return out
end

---------------------------------------------------------------------------
-- Searching the game's auction house
---------------------------------------------------------------------------
local pendingOpen -- { id, token, t }
local openToken = 0

local function AHFrame()
  local m = _G.AuctionHouseFrame
  if type(m) == "table" then return m, "modern" end
  local l = _G.AuctionFrame
  if type(l) == "table" then return l, "legacy" end
  return nil
end

-- Open an item's listings in the game's auction house, the same way typing
-- its name and clicking it would. The chart follows by itself (AH.lua sees
-- the search and the selected result).
function ns.SearchAH(id)
  id = tonumber(id)
  if not id then return false end
  local name = ItemName(id)
  local F, kind = AHFrame()
  if not F then return false end
  if kind == "modern" then
    local mode = _G.AuctionHouseFrameDisplayMode
    if type(F.SetDisplayMode) == "function" and type(mode) == "table" and mode.Buy then
      pcall(F.SetDisplayMode, F, mode.Buy)
    end
    local bar = F.SearchBar
    if type(bar) ~= "table" then return false end
    if type(bar.SetSearchText) == "function" then
      pcall(bar.SetSearchText, bar, name)
    elseif type(bar.SearchBox) == "table" and type(bar.SearchBox.SetText) == "function" then
      pcall(bar.SearchBox.SetText, bar.SearchBox, name)
    end
    openToken = openToken + 1
    pendingOpen = { id = id, token = openToken, t = Now() }
    local tok = openToken
    After(5, function()
      if pendingOpen and pendingOpen.token == tok then pendingOpen = nil end
    end)
    if type(bar.StartSearch) == "function" then
      pcall(bar.StartSearch, bar)
    elseif type(bar.SearchButton) == "table" and type(bar.SearchButton.Click) == "function" then
      pcall(bar.SearchButton.Click, bar.SearchButton)
    end
  else
    local box = _G.BrowseName
    if type(box) == "table" and type(box.SetText) == "function" then pcall(box.SetText, box, name) end
    if type(_G.AuctionFrameBrowse_Search) == "function" then pcall(_G.AuctionFrameBrowse_Search) end
  end
  if type(ns.ShowChart) == "function" and not (type(ns.settings) == "table" and ns.settings.chart == false) then
    pcall(ns.ShowChart, id, "ah")
  end
  return true
end

-- When the search comes back, open the exact item (a name search also finds
-- "Bolt of Silk Cloth" when you asked for "Silk Cloth").
local function OnBrowseResults()
  local p = pendingOpen
  if not p then return end
  local CAH, F = _G.C_AuctionHouse, _G.AuctionHouseFrame
  if type(CAH) ~= "table" or type(CAH.GetBrowseResults) ~= "function" or type(F) ~= "table" then return end
  local ok, list = pcall(CAH.GetBrowseResults)
  if not ok or type(list) ~= "table" then return end
  for _, r in ipairs(list) do
    local key = type(r) == "table" and r.itemKey
    if type(key) == "table" and Num(key.itemID) == p.id then
      pendingOpen = nil
      if type(F.SelectBrowseResult) == "function" then pcall(F.SelectBrowseResult, F, r) end
      return
    end
  end
end

---------------------------------------------------------------------------
-- Panel
---------------------------------------------------------------------------
local SB = { built = false, flipRows = {}, watchRows = {}, offset = 0, ideas = {}, watchShown = 0 }
ns.SidebarPanel = SB

local function NewTex(parent, layer, sub)
  return parent:CreateTexture(nil, layer, nil, sub)
end

local function NewFS(parent, font)
  local fs = parent:CreateFontString(nil, "OVERLAY", font or "GameFontHighlightSmall")
  fs:SetJustifyH("LEFT")
  if fs.SetWordWrap then fs:SetWordWrap(false) end
  return fs
end

local function SetFSColor(fs, key)
  local c = Col(key)
  fs:SetTextColor(c[1], c[2], c[3], 1)
end

local function SetStar(tex, on)
  local atlas = on and "auctionhouse-icon-favorite" or "auctionhouse-icon-favorite-off"
  local ok = type(tex.SetAtlas) == "function" and pcall(tex.SetAtlas, tex, atlas)
  if not ok then
    tex:SetTexture(WHITE)
    local c = on and Col("buy") or Col("muted")
    tex:SetVertexColor(c[1], c[2], c[3], on and 1 or 0.5)
  else
    tex:SetVertexColor(1, 1, 1, on and 1 or 0.55)
  end
end

local function HideTip(self)
  local tt = _G.GameTooltip
  if tt and tt.GetOwner and tt:GetOwner() == self then tt:Hide() end
end

local function SimpleTip(owner, title, body)
  owner:SetScript("OnEnter", function(self)
    local tt = _G.GameTooltip
    if not tt then return end
    tt:SetOwner(self, "ANCHOR_RIGHT")
    local t = type(title) == "function" and title() or title
    tt:AddLine(t, 1, 1, 1)
    local b = type(body) == "function" and body() or body
    if b then tt:AddLine(b, nil, nil, nil, true) end
    tt:Show()
  end)
  owner:SetScript("OnLeave", HideTip)
end

local function TextButton(parent, label, w, h)
  local b = CreateFrame("Button", nil, parent)
  b:SetSize(w, h)
  b.bg = NewTex(b, "BACKGROUND", 1)
  b.bg:SetAllPoints(b)
  local p = Col("panel")
  b.bg:SetColorTexture(p[1] + 0.05, p[2] + 0.05, p[3] + 0.06, 1)
  local hl = b:CreateTexture(nil, "HIGHLIGHT")
  hl:SetAllPoints(b)
  hl:SetColorTexture(1, 1, 1, 0.08)
  b.text = NewFS(b, "GameFontHighlightSmall")
  b.text:SetJustifyH("CENTER")
  b.text:SetPoint("CENTER", b, "CENTER", 0, 0)
  b.text:SetText(label)
  return b
end

-- Tooltip for a row: the game's own item tooltip (which already carries the
-- Forever AH price lines), then the flip numbers and what clicks do.
local function RowTooltip(row)
  local id = row.itemID
  local tt = _G.GameTooltip
  if not id or not tt then return end
  tt:SetOwner(row, "ANCHOR_RIGHT")
  local shown = false
  if type(tt.SetItemByID) == "function" then shown = pcall(tt.SetItemByID, tt, id) end
  if not shown then tt:AddLine(ItemName(id), QualityRGB(id)) end
  local idea = row.idea
  if idea then
    tt:AddLine(" ")
    tt:AddLine(Paint("Flip idea", "buy"))
    tt:AddDoubleLine("Buy at or under", Money(idea.buy), 0.8, 0.8, 0.8, 1, 1, 1)
    tt:AddDoubleLine("Sell at", Money(idea.sell), 0.8, 0.8, 0.8, 1, 1, 1)
    tt:AddDoubleLine("Profit each, after the 5% cut", Paint("+" .. Money(idea.profit), "pos"), 0.8, 0.8, 0.8)
    tt:AddDoubleLine("Sells per day (estimate)", "~" .. Count(idea.sold), 0.8, 0.8, 0.8, 1, 1, 1)
    if idea.thin then tt:AddLine("Slower seller: may take a while to sell.", 0.6, 0.6, 0.6, true) end
  end
  tt:AddLine(" ")
  tt:AddLine("Click: open it in the auction house", 0.55, 0.55, 0.55)
  tt:AddLine(idea and "Right-click: watch it, or hide this idea" or "Right-click: move or remove", 0.55, 0.55, 0.55)
  tt:Show()
end

local function OpenMenu(row)
  local id = row.itemID
  if not id then return end
  local MU = _G.MenuUtil
  local watched = ns.IsWatched(id)
  if type(MU) == "table" and type(MU.CreateContextMenu) == "function" then
    pcall(MU.CreateContextMenu, row, function(_, root)
      if type(root.CreateTitle) == "function" then root:CreateTitle(ItemName(id)) end
      root:CreateButton("Search in auction house", function() ns.SearchAH(id) end)
      root:CreateButton("Show price chart", function()
        if type(ns.ShowChart) == "function" then ns.ShowChart(id, "ah") end
      end)
      if row.idea then
        root:CreateButton(watched and "Remove from watchlist" or "Add to watchlist", function() ns.ToggleWatch(id) end)
        root:CreateButton("Not interested (hide for a day)", function() ns.HideIdea(id) end)
      else
        root:CreateButton("Move up", function() ns.MoveWatch(id, -1) end)
        root:CreateButton("Move down", function() ns.MoveWatch(id, 1) end)
        root:CreateButton("Remove from watchlist", function() ns.RemoveWatch(id) end)
      end
    end)
    return
  end
  -- No context menus in this client: right-click toggles the watchlist.
  ns.ToggleWatch(id)
end

local function RowClick(row, button)
  local id = row.itemID
  if not id then return end
  if button == "RightButton" then
    OpenMenu(row)
    return
  end
  if type(_G.IsModifiedClick) == "function" and _G.IsModifiedClick("CHATLINK") then
    local link
    local CI = _G.C_Item
    if type(CI) == "table" and type(CI.GetItemInfo) == "function" then
      local ok, _, l = pcall(CI.GetItemInfo, id)
      if ok then link = l end
    end
    if type(link) == "string" then
      local CFU = _G.ChatFrameUtil
      if type(CFU) == "table" and type(CFU.InsertLink) == "function" then
        pcall(CFU.InsertLink, link)
      elseif type(_G.ChatEdit_InsertLink) == "function" then
        pcall(_G.ChatEdit_InsertLink, link)
      end
      return
    end
  end
  ns.SearchAH(id)
end

local function MakeRow(parent, h, isFlip)
  local r = CreateFrame("Button", nil, parent)
  r:SetHeight(h)
  if r.RegisterForClicks then r:RegisterForClicks("LeftButtonUp", "RightButtonUp") end
  r.accent = NewTex(r, "BORDER", 0)
  r.accent:SetPoint("TOPLEFT", r, "TOPLEFT", 0, 0)
  r.accent:SetPoint("BOTTOMLEFT", r, "BOTTOMLEFT", 0, 0)
  r.accent:SetWidth(2)
  r.accent:Hide()
  local hl = r:CreateTexture(nil, "HIGHLIGHT")
  hl:SetAllPoints(r)
  hl:SetColorTexture(1, 1, 1, 0.06)
  r.icon = NewTex(r, "ARTWORK", 0)
  local isz = isFlip and 26 or 16
  r.icon:SetSize(isz, isz)
  r.icon:SetPoint("LEFT", r, "LEFT", 5, 0)
  r.icon:SetTexCoord(0.07, 0.93, 0.07, 0.93)
  r.name = NewFS(r, "GameFontHighlightSmall")
  r.right = NewFS(r, isFlip and "GameFontHighlightSmall" or "GameFontHighlightSmall")
  r.right:SetJustifyH("RIGHT")
  if isFlip then
    r.star = CreateFrame("Button", nil, r)
    r.star:SetSize(14, 14)
    r.star:SetPoint("TOPRIGHT", r, "TOPRIGHT", -3, -3)
    r.star.tex = NewTex(r.star, "ARTWORK", 0)
    r.star.tex:SetAllPoints(r.star)
    r.star:SetScript("OnClick", function()
      if r.itemID then ns.ToggleWatch(r.itemID) end
    end)
    SimpleTip(r.star, function() return ns.IsWatched(r.itemID) and "On your watchlist" or "Add to watchlist" end,
      function() return ns.IsWatched(r.itemID) and "Click to remove it." or "Keep an eye on it in the watchlist below." end)
    r.name:SetPoint("TOPLEFT", r.icon, "TOPRIGHT", 6, 1)
    r.name:SetPoint("RIGHT", r, "RIGHT", -64, 0)
    r.sub = NewFS(r, "GameFontHighlightSmall")
    r.sub:SetPoint("BOTTOMLEFT", r.icon, "BOTTOMRIGHT", 6, -1)
    r.sub:SetPoint("RIGHT", r, "RIGHT", -60, 0)
    r.right:SetPoint("TOPRIGHT", r, "TOPRIGHT", -20, -4)
    r.right2 = NewFS(r, "GameFontHighlightSmall")
    r.right2:SetJustifyH("RIGHT")
    r.right2:SetPoint("BOTTOMRIGHT", r, "BOTTOMRIGHT", -4, 4)
    SetFSColor(r.right2, "muted")
  else
    r.name:SetPoint("LEFT", r.icon, "RIGHT", 6, 0)
    r.name:SetPoint("RIGHT", r, "RIGHT", -96, 0)
    r.change = NewFS(r, "GameFontHighlightSmall")
    r.change:SetJustifyH("RIGHT")
    r.change:SetPoint("RIGHT", r, "RIGHT", -4, 0)
    r.change:SetWidth(40)
    r.right:SetPoint("RIGHT", r.change, "LEFT", -2, 0)
  end
  r:SetScript("OnClick", RowClick)
  r:SetScript("OnEnter", RowTooltip)
  r:SetScript("OnLeave", HideTip)
  return r
end

local function SectionTitle(parent, text)
  local fs = NewFS(parent, "GameFontNormalSmall")
  fs:SetText(text)
  SetFSColor(fs, "muted")
  return fs
end

local function SidebarWanted()
  local st = S()
  return not (st and st.sidebar == false)
end

function SB:Build(parent)
  if self.built then return end
  self.built = true
  local template = (_G.BackdropTemplateMixin and "BackdropTemplate") or nil
  local F = CreateFrame("Frame", "ForeverAHSidebar", parent, template)
  self.frame = F
  F:SetWidth(W)
  F:SetHeight(max(MIN_H, Num(parent:GetHeight()) or 538))
  F:EnableMouse(true)
  if F.SetClampedToScreen then F:SetClampedToScreen(true) end
  local bg = Col("bg")
  if F.SetBackdrop then
    F:SetBackdrop({ bgFile = WHITE, edgeFile = WHITE, edgeSize = 1 })
    F:SetBackdropColor(bg[1], bg[2], bg[3], bg[4] or 0.96)
    F:SetBackdropBorderColor(0.20, 0.20, 0.24, 1)
  else
    local t = NewTex(F, "BACKGROUND", -8)
    t:SetAllPoints(F)
    t:SetColorTexture(bg[1], bg[2], bg[3], bg[4] or 0.96)
  end

  -- Header
  self.title = NewFS(F, "GameFontNormal")
  self.title:SetPoint("TOPLEFT", F, "TOPLEFT", PAD, -8)
  self.title:SetText(Paint("Forever", "buy") .. Paint(" AH", "sell"))
  self.age = NewFS(F, "GameFontHighlightSmall")
  self.age:SetPoint("TOPLEFT", self.title, "BOTTOMLEFT", 0, -3)
  self.age:SetPoint("RIGHT", F, "RIGHT", -PAD, 0)
  SetFSColor(self.age, "muted")
  local ageTip = CreateFrame("Frame", nil, F)
  ageTip:SetAllPoints(self.age)
  ageTip:EnableMouse(true)
  SimpleTip(ageTip, "Price data",
    function()
      return SB.why or "Prices from the hourly snapshots on grantdenney.com/wow. Update-WoW-Prices.bat keeps them fresh; type /reload to load new ones."
    end)

  local hide = TextButton(F, "<", 18, 18)
  hide:SetPoint("TOPRIGHT", F, "TOPRIGHT", -6, -6)
  SetFSColor(hide.text, "muted")
  hide:SetScript("OnClick", function() ns.SetSidebar(false) end)
  SimpleTip(hide, "Tuck the sidebar away", "Bring it back with the Forever AH tab on the auction house, or /fah sidebar.")
  self.hideButton = hide

  -- Flip ideas
  self.flipTitle = SectionTitle(F, "FLIP IDEAS")
  self.flipTitle:SetPoint("TOPLEFT", F, "TOPLEFT", PAD, -HEADER_H)
  local flipHelp = CreateFrame("Frame", nil, F)
  flipHelp:SetAllPoints(self.flipTitle)
  flipHelp:EnableMouse(true)
  SimpleTip(flipHelp, "Flip ideas",
    "Items to buy under the Target Buy price and relist at the sell price, ranked by a day's profit after the 5% auction cut. "
    .. "Sales per day are estimates from the hourly snapshots, so treat these as ideas, not promises.")
  self.flipBox = CreateFrame("Frame", nil, F)
  self.flipBox:SetPoint("TOPLEFT", F, "TOPLEFT", 4, -(HEADER_H + TITLE_H))
  self.flipBox:SetPoint("RIGHT", F, "RIGHT", -4, 0)
  self.flipBox:SetHeight(FLIP_H * MAX_FLIPS)
  for i = 1, MAX_FLIPS do
    local r = MakeRow(self.flipBox, FLIP_H, true)
    r:SetPoint("TOPLEFT", self.flipBox, "TOPLEFT", 0, -(i - 1) * FLIP_H)
    r:SetPoint("RIGHT", self.flipBox, "RIGHT", 0, 0)
    self.flipRows[i] = r
  end
  self.flipMsg = NewFS(self.flipBox, "GameFontHighlightSmall")
  self.flipMsg:SetPoint("TOPLEFT", self.flipBox, "TOPLEFT", 4, -4)
  self.flipMsg:SetPoint("RIGHT", self.flipBox, "RIGHT", -4, 0)
  if self.flipMsg.SetWordWrap then self.flipMsg:SetWordWrap(true) end
  SetFSColor(self.flipMsg, "muted")

  -- Watchlist
  local wTop = HEADER_H + TITLE_H + FLIP_H * MAX_FLIPS + 8
  self.watchTitle = SectionTitle(F, "WATCHLIST")
  self.watchTitle:SetPoint("TOPLEFT", F, "TOPLEFT", PAD, -wTop)
  local add = TextButton(F, "+ Watch", 58, 16)
  add:SetPoint("TOPRIGHT", F, "TOPRIGHT", -6, -(wTop - 2))
  SetFSColor(add.text, "buy")
  add:SetScript("OnClick", function() SB:AddCurrent() end)
  SimpleTip(add, function()
      local id = SB:CurrentItem()
      return id and ("Watch " .. ItemName(id)) or "Watch an item"
    end,
    function()
      return SB:CurrentItem() and "Adds the item you are looking at in the auction house."
        or "Search an item in the auction house first, or type its name in the box below."
    end)
  self.addButton = add

  local box
  local ok, eb = pcall(CreateFrame, "EditBox", "ForeverAHWatchAddBox", F, "InputBoxTemplate")
  if ok and eb then box = eb else box = CreateFrame("EditBox", "ForeverAHWatchAddBox", F) end
  box:SetHeight(18)
  box:SetPoint("TOPLEFT", F, "TOPLEFT", PAD + 4, -(wTop + TITLE_H))
  box:SetPoint("RIGHT", F, "RIGHT", -PAD, 0)
  if box.SetAutoFocus then box:SetAutoFocus(false) end
  if box.SetFontObject then pcall(box.SetFontObject, box, "GameFontHighlightSmall") end
  if box.SetMaxLetters then box:SetMaxLetters(80) end
  box.hint = NewFS(box, "GameFontHighlightSmall")
  box.hint:SetPoint("LEFT", box, "LEFT", 2, 0)
  box.hint:SetText("Add item: type a name or shift-click it")
  SetFSColor(box.hint, "muted")
  local function UpdateHint()
    local txt = box.GetText and box:GetText() or ""
    local focus = box.HasFocus and box:HasFocus()
    if (txt == nil or txt == "") and not focus then box.hint:Show() else box.hint:Hide() end
  end
  box:SetScript("OnEditFocusGained", UpdateHint)
  box:SetScript("OnEditFocusLost", UpdateHint)
  box:SetScript("OnTextChanged", UpdateHint)
  box:SetScript("OnEscapePressed", function(b) b:SetText("") b:ClearFocus() end)
  box:SetScript("OnEnterPressed", function(b)
    local txt = b:GetText() or ""
    b:SetText("")
    b:ClearFocus()
    SB:AddFromText(txt)
  end)
  self.addBox = box

  self.watchBox = CreateFrame("Frame", nil, F)
  self.watchBox:SetPoint("TOPLEFT", F, "TOPLEFT", 4, -(wTop + TITLE_H + ADD_H + 2))
  self.watchBox:SetPoint("BOTTOMRIGHT", F, "BOTTOMRIGHT", -4, 18)
  if self.watchBox.EnableMouseWheel then self.watchBox:EnableMouseWheel(true) end
  self.watchBox:SetScript("OnMouseWheel", function(_, delta) SB:Scroll(delta) end)
  self.watchTop = wTop + TITLE_H + ADD_H + 2

  self.more = NewFS(F, "GameFontHighlightSmall")
  self.more:SetPoint("BOTTOMLEFT", F, "BOTTOMLEFT", PAD, 5)
  self.more:SetPoint("RIGHT", F, "RIGHT", -PAD, 0)
  SetFSColor(self.more, "muted")

  self.watchMsg = NewFS(self.watchBox, "GameFontHighlightSmall")
  self.watchMsg:SetPoint("TOPLEFT", self.watchBox, "TOPLEFT", 4, -4)
  self.watchMsg:SetPoint("RIGHT", self.watchBox, "RIGHT", -4, 0)
  if self.watchMsg.SetWordWrap then self.watchMsg:SetWordWrap(true) end
  SetFSColor(self.watchMsg, "muted")
  self.watchMsg:SetText("Your watchlist is empty. Click + Watch while looking at an item, or type a name above.")

  F:SetScript("OnShow", function() SB:Refresh() end)
  F:SetScript("OnSizeChanged", function() if F:IsShown() then SB:Refresh() end end)

  -- The tab that brings a tucked-away sidebar back.
  local tab = TextButton(parent, "Forever AH >", 86, 20)
  SetFSColor(tab.text, "buy")
  tab:SetScript("OnClick", function() ns.SetSidebar(true) end)
  SimpleTip(tab, "Forever AH sidebar", "Show flip ideas and your watchlist next to the auction house.")
  tab:Hide()
  self.tab = tab
end

-- Next to the auction house: on its right, or on its left when the screen
-- has no room on the right. The sidebar is a child of the AH window, so it
-- shares its scale and hides with it (and with TSM's shrunken AH).
function SB:Place()
  local F, parent = self.frame, self.parent
  if not F or not parent then return end
  F:ClearAllPoints()
  self.tab:ClearAllPoints()
  local right = Num(parent:GetRight())
  local sc = Num(parent:GetEffectiveScale()) or 1
  local uiW = Num(_G.UIParent and _G.UIParent:GetWidth()) or nil
  local uiS = Num(_G.UIParent and _G.UIParent:GetEffectiveScale()) or 1
  local roomRight = true
  if right and uiW and sc > 0 then
    roomRight = (right * sc + (W + 6) * sc) <= uiW * uiS + 0.5
  end
  if roomRight then
    F:SetPoint("TOPLEFT", parent, "TOPRIGHT", 4, 0)
    self.tab:SetPoint("TOPLEFT", parent, "TOPRIGHT", 4, 0)
  else
    F:SetPoint("TOPRIGHT", parent, "TOPLEFT", -4, 0)
    self.tab:SetPoint("TOPRIGHT", parent, "TOPLEFT", -4, 0)
  end
  self.side = roomRight and "right" or "left"
  F:SetHeight(max(MIN_H, Num(parent:GetHeight()) or 538))
end

function SB:CurrentItem()
  local AH = ns.AHState
  local id = type(AH) == "table" and tonumber(AH.itemID) or nil
  if not id and type(ns.ChartItem) == "function" then id = tonumber(ns.ChartItem()) end
  return id
end

function SB:AddCurrent()
  local id = self:CurrentItem()
  if not id then
    if self.addBox and self.addBox.SetFocus then self.addBox:SetFocus() end
    return
  end
  if ns.IsWatched(id) then
    Print(ItemName(id) .. " is already on your watchlist.")
    return
  end
  ns.AddWatch(id)
  Print("watching " .. ItemName(id) .. ".")
end

function SB:AddFromText(text)
  if type(text) ~= "string" then return end
  text = text:gsub("^%s+", ""):gsub("%s+$", "")
  if text == "" then return end
  local id = type(ns.ResolveItem) == "function" and ns.ResolveItem(text) or nil
  if not id then
    local linked = text:match("item:(%d+)")
    id = linked and tonumber(linked) or nil
  end
  if not id then
    Print("no price data for " .. text .. ". Check the spelling, or shift-click the item.")
    return
  end
  if ns.AddWatch(id) then
    self.offset = 0
    Print("watching " .. ItemName(id) .. ".")
  else
    Print(ItemName(id) .. " is already on your watchlist.")
  end
end

function SB:Scroll(delta)
  local list = ns.GetWatchlist()
  local maxOff = max(0, #list - (self.watchShown or 0))
  self.offset = max(0, min(maxOff, (self.offset or 0) - (delta or 0)))
  self:RefreshWatch()
end

local function SetRowItem(r, id)
  r.itemID = id
  r.icon:SetTexture(ItemIcon(id))
  r.name:SetText(ItemName(id))
  r.name:SetTextColor(QualityRGB(id))
end

-- The ideas are worked out again only when something they depend on changes
-- (the AH opened, a live price arrived, an idea was hidden). Redrawing the
-- rows (a star clicked, item names arriving) reuses them.
function SB:RefreshFlips()
  if self.flipsDirty or not self.ideas then
    self.ideas = HasData() and ns.FlipIdeas(MAX_FLIPS) or {}
    self.flipsDirty = false
  end
  local ideas = self.ideas
  for i, r in ipairs(self.flipRows) do
    local idea = ideas[i]
    if idea then
      SetRowItem(r, idea.id)
      r.idea = idea
      r.sub:SetText(Paint("Buy " .. ShortMoney(idea.buy), "buy") .. "  " .. Paint("Sell " .. ShortMoney(idea.sell), "sell"))
      r.right:SetText(Paint("+" .. ShortMoney(idea.profit), "pos"))
      r.right2:SetText("~" .. Count(idea.sold) .. "/day")
      SetStar(r.star.tex, ns.IsWatched(idea.id))
      r:Show()
    else
      r.itemID, r.idea = nil, nil
      r:Hide()
    end
  end
  if #ideas == 0 then
    if not HasData() then
      self.flipMsg:SetText("No price data yet. Run Update-WoW-Prices.bat, then /reload.")
    else
      self.flipMsg:SetText("No good flips right now. Prices are steady; check back after the next update.")
    end
    self.flipMsg:Show()
  else
    self.flipMsg:Hide()
  end
end

function SB:RefreshWatch()
  local list = ns.GetWatchlist()
  local h = Num(self.watchBox:GetHeight()) or 0
  if h <= 0 then
    local F = self.frame
    h = (Num(F:GetHeight()) or MIN_H) - self.watchTop - 18
  end
  local fit = max(1, floor(h / WATCH_H))
  local maxOff = max(0, #list - fit)
  if (self.offset or 0) > maxOff then self.offset = maxOff end
  local off = self.offset or 0
  local tf = CurrentTF()
  local shown = 0
  for i = 1, max(fit, #self.watchRows) do
    local r = self.watchRows[i]
    local id = (i <= fit) and list[off + i] or nil
    if id then
      if not r then
        r = MakeRow(self.watchBox, WATCH_H, false)
        r:SetPoint("TOPLEFT", self.watchBox, "TOPLEFT", 0, -(i - 1) * WATCH_H)
        r:SetPoint("RIGHT", self.watchBox, "RIGHT", 0, 0)
        self.watchRows[i] = r
      end
      SetRowItem(r, id)
      r.idea = nil
      local it = type(ns.GetItem) == "function" and ns.GetItem(id) or nil
      local live = Live(id)
      local price = it and ((it.market > 0 and it.market) or it.cheapest) or 0
      if live and (price <= 0) then price = live end
      r.right:SetText(price > 0 and ShortMoney(price) or Paint("no data", "muted"))
      local ch = it and DayChange(id)
      if ch then
        local pct = floor(abs(ch) * 1000 + 0.5) / 10
        if pct < 0.05 then
          r.change:SetText(Paint("0%", "muted"))
        else
          r.change:SetText(Paint((ch > 0 and "+" or "-") .. pct .. "%", ch > 0 and "pos" or "neg"))
        end
      else
        r.change:SetText("")
      end
      -- Edge colour: gold when the cheapest listing is a buy, blue when it is a sell.
      local rec = it and Advice(id, tf)
      local v = rec and rec.history and rec.verdict
      if v == "Strong Buy" or v == "Buy" then
        local c = Col("buy")
        r.accent:SetColorTexture(c[1], c[2], c[3], v == "Strong Buy" and 1 or 0.6)
        r.accent:Show()
      elseif v == "Strong Sell" or v == "Sell" then
        local c = Col("sell")
        r.accent:SetColorTexture(c[1], c[2], c[3], v == "Strong Sell" and 1 or 0.6)
        r.accent:Show()
      else
        r.accent:Hide()
      end
      r:Show()
      shown = shown + 1
    elseif r then
      r.itemID = nil
      r:Hide()
    end
  end
  self.watchShown = shown
  if #list == 0 then self.watchMsg:Show() else self.watchMsg:Hide() end
  local below = #list - off - shown
  if off > 0 and below > 0 then
    self.more:SetText(off .. " above, " .. below .. " more below (scroll)")
  elseif below > 0 then
    self.more:SetText(below .. " more (scroll)")
  elseif off > 0 then
    self.more:SetText(off .. " above (scroll up)")
  else
    self.more:SetText("")
  end
end

function SB:RefreshHeader()
  local age = type(ns.DataAge) == "function" and ns.DataAge() or nil
  local txt
  if not HasData() then
    txt = "No price data yet"
  else
    local house = type(ns.HouseLabel) == "function" and ns.HouseLabel() or "Alliance AH"
    local a = type(ns.AgeText) == "function" and ns.AgeText(age) or ""
    txt = house .. ", " .. a
    if age and type(ns.STALE_SECONDS) == "number" and age > ns.STALE_SECONDS then txt = Paint(txt, "neg") end
  end
  -- At another market (Horde, a goblin AH, Hardcore) say so in a few words;
  -- hovering the line gives the full reason.
  local why = type(ns.MarketMismatch) == "function" and ns.MarketMismatch() or nil
  if type(why) == "string" and why ~= "" then
    txt = Paint("Prices may not match this AH", "neg")
    self.why = why
  else
    self.why = nil
  end
  self.age:SetText(txt)
end

function SB:Refresh()
  if not self.built or not self.frame:IsShown() then return end
  local ok, err = pcall(function()
    self:RefreshHeader()
    self:RefreshFlips()
    self:RefreshWatch()
  end)
  if not ok then self.lastError = err end
end

Refresh = function()
  if SB.built and SB.frame:IsShown() then SB:Refresh() end
end
ns.SidebarRefresh = Refresh

-- Show the sidebar or the tab, as the setting says.
function SB:Apply()
  if not self.built then return end
  if SidebarWanted() then
    self.tab:Hide()
    self:Place()
    self.frame:Show()
    self:Refresh()
  else
    self.frame:Hide()
    self:Place()
    self.tab:Show()
  end
  if type(ns.ChartRefreshPlacement) == "function" then pcall(ns.ChartRefreshPlacement) end
end

function ns.SetSidebar(on)
  local st = S()
  if st then st.sidebar = on and true or false end
  SB:Apply()
end

function ns.SidebarIsShown()
  return SB.built and SB.frame:IsVisible() and true or false
end

-- The sidebar frame when it is on screen to the right of the AH, so the
-- chart can sit beside it instead of on top of it.
function ns.SidebarFrame()
  if not ns.SidebarIsShown() then return nil end
  return SB.frame, SB.side
end

---------------------------------------------------------------------------
-- Opening with the auction house
---------------------------------------------------------------------------
local function OnAHOpen()
  local parent = AHFrame()
  if not parent then return end
  if not SB.built then
    SB.parent = parent
    SB:Build(parent)
  elseif SB.parent ~= parent then
    -- Built for the other AH window: move both pieces over.
    SB.parent = parent
    SB.frame:SetParent(parent)
    SB.tab:SetParent(parent)
  end
  SB.offset = 0
  SB.flipsDirty = true
  SB:Apply()
end

local function OnAHClose()
  pendingOpen = nil
  if SB.built and SB.addBox and SB.addBox.ClearFocus then SB.addBox:ClearFocus() end
end

-- Shift-clicking an item while the add box has the cursor puts its name there.
local function OnInsertLink(text)
  local box = SB.addBox
  if not box or not box.HasFocus or not box:HasFocus() or type(text) ~= "string" then return end
  local id = text:match("item:(%d+)")
  if id then
    box:SetText("")
    box:ClearFocus()
    SB:AddFromText("item:" .. id)
  end
end

local function HookInsertLink()
  if SB.hookedLink or type(_G.hooksecurefunc) ~= "function" then return end
  local CFU = _G.ChatFrameUtil
  if type(CFU) == "table" and type(CFU.InsertLink) == "function" then
    SB.hookedLink = pcall(_G.hooksecurefunc, CFU, "InsertLink", OnInsertLink)
  elseif type(_G.ChatEdit_InsertLink) == "function" then
    SB.hookedLink = pcall(_G.hooksecurefunc, "ChatEdit_InsertLink", OnInsertLink)
  end
end
SB.HookInsertLink = HookInsertLink

local refreshToken = 0
local function RefreshSoon()
  if not ns.SidebarIsShown() then return end
  refreshToken = refreshToken + 1
  local tok = refreshToken
  After(0.4, function()
    if tok == refreshToken then Refresh() end
  end)
end

local function IsAuctioneer(t)
  local E = _G.Enum
  local PIT = type(E) == "table" and E.PlayerInteractionType
  return type(PIT) == "table" and PIT.Auctioneer ~= nil and t == PIT.Auctioneer
end

local handlers = {
  PLAYER_LOGIN = function() HookInsertLink() end,
  AUCTION_HOUSE_SHOW = function() After(0, OnAHOpen) end,
  PLAYER_INTERACTION_MANAGER_FRAME_SHOW = function(t) if IsAuctioneer(t) then After(0, OnAHOpen) end end,
  AUCTION_HOUSE_CLOSED = OnAHClose,
  PLAYER_INTERACTION_MANAGER_FRAME_HIDE = function(t) if IsAuctioneer(t) then OnAHClose() end end,
  AUCTION_HOUSE_BROWSE_RESULTS_UPDATED = function() OnBrowseResults() end,
  AUCTION_HOUSE_BROWSE_RESULTS_ADDED = function() OnBrowseResults() end,
  -- A live price came in: the ideas may change.
  COMMODITY_SEARCH_RESULTS_UPDATED = function() SB.flipsDirty = true RefreshSoon() end,
  ITEM_SEARCH_RESULTS_UPDATED = function() SB.flipsDirty = true RefreshSoon() end,
  -- Only names and icons arrive: redraw, no new ideas.
  GET_ITEM_INFO_RECEIVED = RefreshSoon,
}

if type(_G.CreateFrame) == "function" then
  local ok, ev = pcall(_G.CreateFrame, "Frame")
  if ok and type(ev) == "table" then
    SB.eventFrame = ev
    for name in pairs(handlers) do pcall(ev.RegisterEvent, ev, name) end
    ev:SetScript("OnEvent", function(_, event, ...)
      local h = handlers[event]
      if not h then return end
      local okH, err = pcall(h, ...)
      if not okH then SB.lastError = err end
    end)
  end
end

---------------------------------------------------------------------------
-- Slash commands: /fah watch, /fah unwatch, /fah watchlist, /fah sidebar
---------------------------------------------------------------------------
local baseSlash = ns.HandleSlash

local function WatchCommand(rest, remove)
  if rest == "" then
    local id = SB:CurrentItem()
    if not id then
      Print("say which item: /fah " .. (remove and "unwatch" or "watch") .. " Runecloth (or shift-click it).")
      return
    end
    rest = tostring(id)
  end
  local id = type(ns.ResolveItem) == "function" and ns.ResolveItem(rest) or nil
  if not id then
    local linked = rest:match("item:(%d+)")
    id = linked and tonumber(linked) or nil
  end
  if not id and remove then
    -- An item with no price data can still be on the list: match by name.
    local want = rest:lower()
    for _, v in ipairs(ns.GetWatchlist()) do
      if ItemName(v):lower() == want then id = v break end
    end
  end
  if not id then
    Print("no price data for " .. rest .. ". Check the spelling, or shift-click the item.")
    return
  end
  if remove then
    if ns.RemoveWatch(id) then Print("stopped watching " .. ItemName(id) .. ".")
    else Print(ItemName(id) .. " is not on your watchlist.") end
  else
    if ns.AddWatch(id) then Print("watching " .. ItemName(id) .. ".")
    else Print(ItemName(id) .. " is already on your watchlist.") end
  end
end

function ns.HandleSlash(msg)
  local m = type(msg) == "string" and msg:gsub("^%s+", ""):gsub("%s+$", "") or ""
  local cmd, rest = m:match("^(%S*)%s*(.-)$")
  cmd = (cmd or ""):lower()
  rest = rest or ""
  if cmd == "watch" then
    WatchCommand(rest, false)
  elseif cmd == "unwatch" then
    WatchCommand(rest, true)
  elseif cmd == "watchlist" then
    if rest:lower() == "reset" then
      local st = S()
      if st then st.watchlist = nil end
      SB.offset = 0
      ns.GetWatchlist()
      Refresh()
      Print("watchlist is back to the starter list.")
      return
    end
    local list = ns.GetWatchlist()
    if #list == 0 then
      Print("your watchlist is empty. /fah watch Runecloth adds one.")
      return
    end
    Print("watching " .. #list .. " items:")
    for i, id in ipairs(list) do
      local it = type(ns.GetItem) == "function" and ns.GetItem(id) or nil
      local p = it and ((it.market > 0 and it.market) or it.cheapest) or 0
      Print("  " .. i .. ". " .. ItemName(id) .. (p > 0 and (" - " .. Money(p)) or ""))
    end
  elseif cmd == "sidebar" then
    ns.SetSidebar(not SidebarWanted())
    Print("the auction house sidebar is now " .. (SidebarWanted() and "on" or "off") .. ".")
  elseif cmd == "flips" or cmd == "ideas" then
    local ideas = ns.FlipIdeas(MAX_FLIPS)
    if #ideas == 0 then
      Print(HasData() and "no good flips right now." or "no price data yet. Type /fah to see how to get it.")
      return
    end
    Print("flip ideas (buy, sell, profit each after the 5% cut):")
    for i, x in ipairs(ideas) do
      Print("  " .. i .. ". " .. ItemName(x.id) .. ": " .. Money(x.buy) .. " -> " .. Money(x.sell)
        .. ", +" .. Money(x.profit) .. ", ~" .. Count(x.sold) .. "/day")
    end
  else
    if cmd == "help" or cmd == "?" then
      if type(baseSlash) == "function" then baseSlash(msg) end
      Print("/fah watch Runecloth : add an item to the auction house watchlist (no name = the item you are looking at)")
      Print("/fah unwatch Runecloth : take it off")
      Print("/fah watchlist : list it (/fah watchlist reset brings back the starter list)")
      Print("/fah flips : print the current flip ideas")
      Print("/fah sidebar : show or tuck away the auction house sidebar")
      return
    end
    if type(baseSlash) == "function" then return baseSlash(msg) end
  end
end

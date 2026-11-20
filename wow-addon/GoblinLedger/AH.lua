local ADDON_NAME, ns = ...

-- Goblin Ledger: auction house integration.
-- Shows the price chart under the auction house and switches it to whatever
-- item you search for, click on, or put in the sell slot. Adds two small
-- buttons to the sell frame that fill in the price box:
--   "Sell now: X"    1 copper under the cheapest listing
--   "Suggested: X"   the price it usually sells for (from the price history;
--                    after a drop that lasts, the highest price since)
-- A search that finds nothing ("No items found") gets a few lines under
-- Blizzard's text: when the item was last listed, for how much, how many sold.
--
-- Rules this file sticks to:
--   * hooksecurefunc, HookScript and events only. No Blizzard function is replaced.
--   * Never sends auction house queries, never posts, bids, buys or cancels.
--     The price buttons only fill the price box: posting stays your click.
--   * The live listings the auction house already loaded (for a sell slot or a
--     clicked result) are read, never requested, and they beat the hourly data.
--   * Gear versions ("of the Bear") are told apart by the bonus ids in their
--     links: a version with its own prices uses them; one the data does not
--     have only follows a live listing of that same version.
--   * Works with the modern AuctionHouseFrame (WoW Forever) and, if it ever
--     shows up, the old AuctionFrame. Auctionator's own tabs are followed too.

local floor, max, min = math.floor, math.max, math.min

local AH = {
  open = false,
  frame = nil,           -- the AH frame the chart docks to
  kind = nil,            -- "modern" or "legacy"
  hookedModern = false,
  hookedLegacy = false,
  hookedAtr = false,
  hookedC = false,
  itemID = nil,          -- the item the chart should show
  pendingSearch = nil,   -- lower-case search text waiting for browse results
  pendingIDs = nil,      -- set of item ids when that name belongs to several items
  expectID = nil,        -- the item ns.SearchAH (the sidebar) is searching for
  expectAt = 0,
  atrSellItemID = nil,
  atrSellLocation = nil,
  atrReady = false,      -- Auctionator has filled its own price for the item in its Selling tab
  legacySellID = nil,
  legacySellCount = nil,
  otherHouse = false,    -- the open auction house is not the one the data comes from
  live = {},             -- item or version id -> { price = copper, own = true when that listing is yours }
  liveV = {},            -- itemID -> { [version key] = { price, own, vid } } from loaded gear listings
  liveLoaded = {},       -- itemID -> true once all its listings have loaded (gear: every version's cheapest is known)
  suffix = {},           -- itemID -> true once seen with a random suffix ("of the Monkey")
  expectVid = nil,       -- the version the sidebar searched for (its item shows as that version)
  browse = {},           -- itemID -> { price (per unit), own (you have one listed), at, gen } from its browse row (ns.LivePrice)
  browseGen = 0,         -- counts browse searches (rows of one search: the cheapest of them wins)
  lastSearch = nil,      -- the text of the latest browse search, as typed (cleaned)
  queryFiltered = false, -- that search had a category, a level range or filters that can hide items
  queryExact = false,    -- that search matched whole names only (the ExactMatch filter)
  emptyLines = nil,      -- the lines shown under "No items found", while they show
}
ns.AHState = AH

local sellIDs = setmetatable({}, { __mode = "k" })    -- Blizzard sell frame -> itemID
local sellVer = setmetatable({}, { __mode = "k" })    -- Blizzard sell frame -> version info (VersionInfo)
local useButtons = setmetatable({}, { __mode = "k" }) -- Blizzard sell frame -> { now = button, best = button }
local toggles = setmetatable({}, { __mode = "k" })    -- AH frame -> our Chart button

---------------------------------------------------------------------------
-- Helpers
---------------------------------------------------------------------------
local function IsSecret(v)
  local f = _G.issecretvalue
  if type(f) ~= "function" then return false end
  local ok, r = pcall(f, v)
  return ok and r == true
end

local function Num(v)
  if type(v) ~= "number" or IsSecret(v) or v ~= v then return nil end
  return v
end

local function Print(msg)
  if type(ns.Print) == "function" then return ns.Print(msg) end
  local cf = _G.DEFAULT_CHAT_FRAME
  if type(cf) == "table" and cf.AddMessage then cf:AddMessage("Goblin Ledger: " .. tostring(msg)) end
end

local function MoneyI(c)
  if type(ns.MoneyIcons) == "function" then return ns.MoneyIcons(c) end
  if type(ns.MoneyText) == "function" then return ns.MoneyText(c) end
  return tostring(c) .. "c"
end

local function TF()
  local tf = ns.settings and ns.settings.chartTF
  if type(tf) == "string" then return tf:upper() end
  return "1D"
end

local function ChartWanted()
  return not (type(ns.settings) == "table" and ns.settings.chart == false)
end

local function After(sec, fn)
  local T = _G.C_Timer
  if type(T) == "table" and type(T.After) == "function" then
    T.After(sec, fn)
  else
    fn()
  end
end

local function SafeHook(tbl, method, fn)
  if type(tbl) ~= "table" or type(tbl[method]) ~= "function" then return false end
  local ok = pcall(hooksecurefunc, tbl, method, function(...)
    local ok2, err = pcall(fn, ...)
    if not ok2 then AH.lastError = err end
  end)
  return ok
end

local function SafeHookGlobal(name, fn)
  if type(_G[name]) ~= "function" then return false end
  local ok = pcall(hooksecurefunc, name, function(...)
    local ok2, err = pcall(fn, ...)
    if not ok2 then AH.lastError = err end
  end)
  return ok
end

local function IDFromLink(link)
  if type(link) ~= "string" or IsSecret(link) then return nil end
  local id = link:match("item:(%d+)")
  return tonumber(id)
end

local function ItemIDFromLocation(loc)
  if loc == nil then return nil end
  local CI = _G.C_Item
  if type(CI) == "table" then
    if type(CI.GetItemID) == "function" then
      local ok, id = pcall(CI.GetItemID, loc)
      if ok and Num(id) then return id end
    end
    if type(CI.GetItemLink) == "function" then
      local ok, link = pcall(CI.GetItemLink, loc)
      if ok then
        local id = IDFromLink(link)
        if id then return id end
      end
    end
  end
  return nil
end

local function LinkFromLocation(loc)
  local CI = _G.C_Item
  if loc == nil or type(CI) ~= "table" or type(CI.GetItemLink) ~= "function" then return nil end
  local ok, link = pcall(CI.GetItemLink, loc)
  if ok and link ~= nil and not IsSecret(link) and type(link) == "string" then return link end
  return nil
end

-- What the addon knows about the version of an item (from its link):
--   mode "plain"    a plain item: its own prices
--   mode "version"  a version the data has its own prices for (vid)
--   mode "unknown"  a version the data has no prices for, or a copy without
--                   bonus ids of an item whose versions differ. The item's
--                   prices mix every version, so only a live listing of this
--                   very same version (liveKey) is used.
local function VersionInfo(id, link, suffix)
  local v = { id = id, key = "", mode = "plain" }
  if not id then return v end
  if type(ns.VariantKey) == "function" then v.key = ns.VariantKey(link) or "" end
  local vid = type(ns.FindVariant) == "function" and ns.FindVariant(id, link) or nil
  local versions = type(ns.HasVariants) == "function" and ns.HasVariants(id)
  v.suffix = (suffix and true or false) or v.key ~= ""
  if vid then
    v.vid, v.mode = vid, "version"
  elseif v.suffix or versions then
    v.mode = "unknown"
  end
  -- Old-style suffix links carry no bonus ids: those copies cannot be told apart.
  if v.key ~= "" then v.liveKey = v.key elseif not suffix then v.liveKey = "" end
  return v
end
AH.VersionInfo = VersionInfo

local function IsCommodityLocation(loc)
  local CAH, E = _G.C_AuctionHouse, _G.Enum
  if loc == nil or type(CAH) ~= "table" or type(CAH.GetItemCommodityStatus) ~= "function" then return nil end
  local ok, status = pcall(CAH.GetItemCommodityStatus, loc)
  if not ok or status == nil then return nil end
  local Commodity = type(E) == "table" and type(E.ItemCommodityStatus) == "table" and E.ItemCommodityStatus.Commodity
  if Commodity == nil then return nil end
  return status == Commodity
end

local function CopperSupported()
  local CAH = _G.C_AuctionHouse
  if type(CAH) == "table" and type(CAH.SupportsCopperValues) == "function" then
    local ok, v = pcall(CAH.SupportsCopperValues)
    if ok and v == false then return false end
  end
  return true
end

-- The price to write into a sell box. Commodities on Forever take copper.
-- Single item auctions (gear) are rounded down to whole silver, because some
-- reports say the server refuses copper there. Rounding down keeps an undercut.
local function PriceForFrame(copper, isCommodity)
  copper = floor((Num(copper) or 0) + 0.5)
  if copper <= 0 then return nil end
  if isCommodity == false or not CopperSupported() then
    copper = max(100, floor(copper / 100) * 100)
  end
  return copper
end
AH.PriceForFrame = PriceForFrame

---------------------------------------------------------------------------
-- Live listings. The auction house loads them for the item in a sell slot or
-- a clicked result (and for every search Auctionator or TSM makes). Reading
-- what it already has sends nothing to the server.
---------------------------------------------------------------------------
local function LiveCommodity(itemID)
  local CAH = _G.C_AuctionHouse
  if type(CAH) ~= "table" or type(CAH.GetNumCommoditySearchResults) ~= "function"
    or type(CAH.GetCommoditySearchResultInfo) ~= "function" then return nil end
  local ok, n = pcall(CAH.GetNumCommoditySearchResults, itemID)
  n = ok and Num(n) or 0
  local best, own
  for i = 1, min(n, 20) do
    local ok2, r = pcall(CAH.GetCommoditySearchResultInfo, itemID, i)
    if ok2 and type(r) == "table" then
      local p = Num(r.unitPrice)
      if p and p > 0 and (Num(r.quantity) or 1) > 0 and (not best or p < best) then
        best, own = p, r.containsOwnerItem == true
      end
    end
  end
  return best, own
end

local function LiveItem(itemKey)
  local CAH = _G.C_AuctionHouse
  if type(itemKey) ~= "table" or type(CAH) ~= "table" or type(CAH.GetNumItemSearchResults) ~= "function"
    or type(CAH.GetItemSearchResultInfo) ~= "function" then return nil end
  local ok, n = pcall(CAH.GetNumItemSearchResults, itemKey)
  n = ok and Num(n) or 0
  local id = Num(itemKey.itemID)
  local best, own
  local byKey, mixed = {}, false   -- version key -> { price, own, vid }
  -- complete: every listing was read (not more than 50, and the game says it sent them all)
  local complete = n <= 50
  if complete and type(CAH.HasFullItemSearchResults) == "function" then
    local okF, full = pcall(CAH.HasFullItemSearchResults, itemKey)
    if okF and full == false then complete = false end
  end
  for i = 1, min(n, 50) do
    local ok2, r = pcall(CAH.GetItemSearchResultInfo, itemKey, i)
    if ok2 and type(r) == "table" then
      local p = Num(r.buyoutAmount)
      local q = Num(r.quantity) or 1
      if p and p > 0 then
        if q > 1 then p = floor(p / q) end
        local mine = r.containsOwnerItem == true
        if not best or p < best then best, own = p, mine end
        -- Each gear listing has its own link: its bonus ids say which version it is.
        local link = r.itemLink
        if id and link ~= nil and not IsSecret(link) and type(link) == "string" and type(ns.VariantKey) == "function" then
          local k = ns.VariantKey(link) or ""
          if k ~= "" then mixed = true end
          local cur = byKey[k]
          if not cur or p < cur.price then
            local vid = type(ns.FindVariant) == "function" and ns.FindVariant(id, link) or nil
            byKey[k] = { price = p, own = mine, vid = vid }
          end
        end
      end
    end
  end
  return best, own, byKey, mixed, complete
end

local refreshPending, chartDirty = false, false
local RefreshAll -- forward

-- Live prices changed: refresh the buttons (and the chart) a moment later, once.
local function RefreshSoon()
  if refreshPending then return end
  refreshPending = true
  After(0.1, function()
    refreshPending = false
    RefreshAll()
  end)
end

-- Seconds of game time (GetTime): which of two live prices came in last.
local function Clock()
  local f = _G.GetTime
  if type(f) ~= "function" then return 0 end
  local ok, t = pcall(f)
  return ok and Num(t) or 0
end

local function StoreLive(id, price, own)
  id = Num(id)
  if not id then return end
  local old = AH.live[id]
  if price then AH.live[id] = { price = price, own = own == true, at = Clock() } else AH.live[id] = nil end
  if not old and not price then return end
  if old and price and old.price == price and old.own == (own == true) then return end
  if type(ns.ChartItem) == "function" and ns.ChartItem() == id then chartDirty = true end
  RefreshSoon()
end
AH.StoreLive = StoreLive

-- The cheapest listing the open auction house is showing for an item (or a
-- version's id), and whether that listing is yours. nil when it has not
-- loaded (or the AH is closed).
function ns.LiveCheapest(itemID)
  if not AH.open then return nil end
  local r = AH.live[tonumber(itemID) or -1]
  if r then return r.price, r.own end
  return nil
end

-- The cheapest buyout per unit the open auction house showed for an item (or
-- a version's id), or nil: its loaded listings (AH.live) or the price on its
-- browse row (AH.browse: per-unit rows without a suffix, of items whose
-- versions do not differ, from the last 10 minutes), whichever came in last.
-- nil at another market (ns.MarketMismatch: a goblin or other-faction auction
-- house, Hardcore): those prices do not mix with the data's. For prices
-- shown to the player (the tooltip's AH value, Sell now, craft costs).
-- Also returns own (that listing is yours; for a browse row: you have one of
-- them listed somewhere) and where the price is from, "listings" or
-- "browse". The sell buttons keep using ns.LiveCheapest.
local BROWSE_MAX_AGE = 600
function ns.LivePrice(itemID)
  if not AH.open then return nil end
  if type(ns.MarketMismatch) == "function" and ns.MarketMismatch() then return nil end
  local id = tonumber(itemID) or -1
  local r, b = AH.live[id], AH.browse[id]
  local p = r and Num(r.price)
  if p and p <= 0 then p = nil end
  local bp = type(b) == "table" and Num(b.price) or nil
  if bp and (bp <= 0 or Clock() - (Num(b.at) or 0) > BROWSE_MAX_AGE
             or (type(ns.PricesMixVersions) == "function" and ns.PricesMixVersions(id))) then
    bp = nil
  end
  if bp and (not p or (Num(b.at) or 0) > (Num(r.at) or 0)) then return bp, b.own == true, "browse" end
  if p then return p, r.own == true, "listings" end
  return nil
end

-- The cheapest loaded listing of one version of an item (by its version key,
-- "" for copies without bonus ids), as { price, own }, or nil.
local function LiveVersion(itemID, key)
  if not AH.open or key == nil then return nil end
  local t = AH.liveV[tonumber(itemID) or -1]
  return t and t[key] or nil
end
AH.LiveVersion = LiveVersion

-- Gear listings came in: remember the cheapest of each version, and give a
-- version with its own prices its live price too.
local function StoreLiveVersions(id, byKey)
  id = Num(id)
  if not id then return end
  local old = AH.liveV[id]
  AH.liveV[id] = byKey
  local seen, changed = {}, false
  for k, r in pairs(byKey or {}) do
    local o = old and old[k]
    if not o or o.price ~= r.price or o.own ~= r.own then changed = true end
    if r.vid then
      seen[r.vid] = true
      StoreLive(r.vid, r.price, r.own)
    end
  end
  for k, r in pairs(old or {}) do
    if not (byKey and byKey[k]) then changed = true end
    if r.vid and not seen[r.vid] then StoreLive(r.vid, nil) end
  end
  -- A version with no prices of its own follows these listings (the sell buttons).
  if changed then RefreshSoon() end
end

---------------------------------------------------------------------------
-- Prices for the sell buttons
---------------------------------------------------------------------------
-- Returns the Sell now and Suggested price prices to fill (already rounded for the
-- frame, nil when that button hides) and what they are based on, or nil, nil
-- and a reason ("market", "suffix", "stale", "history", "unreliable", "none").
-- ver is VersionInfo for the item (nil: a plain item).
local function SellAdvice(id, isCommodity, ver)
  if not id or type(ns.SellPrices) ~= "function" then return nil, nil, "none" end
  -- reasons also: "nolive" (no listing of this version is up and there is no Suggested price)
  if type(ns.MarketMismatch) == "function" and ns.MarketMismatch() then return nil, nil, "market" end
  ver = type(ver) == "table" and ver or { mode = "plain" }
  local age = type(ns.DataAge) == "function" and ns.DataAge() or nil
  if ver.mode == "unknown" then
    -- The data has no prices for this version: only its own live listing,
    -- 1 copper under it (matched when it is yours). No Suggested price.
    local r = LiveVersion(id, ver.liveKey)
    local p = r and Num(r.price)
    if not p or p <= 1 then return nil, nil, "suffix" end
    local now = PriceForFrame(r.own and p or (p - 1), isCommodity)
    if not now then return nil, nil, "suffix" end
    return now, nil, { live = p, matched = r.own == true, age = age, mode = "unknown", ver = ver }
  end
  local pid = ver.vid or id
  local lp, own = ns.LiveCheapest(pid)
  if not lp and age and age > (ns.STALE_SECONDS or 21600) then return nil, nil, "stale" end
  -- The item's listings have all loaded and none of them is this version: the
  -- cheapest one the price data saw is gone, so there is nothing to undercut.
  -- Sell now hides; Suggested price (from the history) still works.
  local noLive = ver.mode == "version" and not lp and AH.open and AH.liveLoaded[id] == true
  local ok, P = pcall(ns.SellPrices, pid, TF(), lp, own)
  if not ok or type(P) ~= "table" then return nil, nil, "none" end
  local now = (not noLive) and P.now and PriceForFrame(P.now, isCommodity) or nil
  local best = P.best and PriceForFrame(P.best, isCommodity) or nil
  if not now and not best then
    if noLive then return nil, nil, "nolive" end
    return nil, nil, (P.nowWhy == "unreliable" and "unreliable") or P.bestWhy or "none"
  end
  return now, best, { live = lp, matched = P.own == true, age = age, P = P, rec = P.rec, mode = ver.mode, ver = ver,
                      noLive = noLive }
end
AH.SellAdvice = SellAdvice

local function BasisText(info)
  if info and info.noLive then
    return "No listing of this version is up right now, so there is nothing to undercut: this uses the latest price data."
  end
  if info and info.live then
    local s = "Based on the cheapest listing right now: " .. MoneyI(info.live) .. "."
    if info.matched then s = s .. " That listing is yours, so it is matched, not undercut." end
    return s
  end
  local hours = info and info.age and floor(info.age / 3600) or nil
  return (hours and hours >= 1) and ("From price data " .. hours .. "h old: the live listings have not loaded yet.")
    or "From the latest price data: the live listings have not loaded yet."
end

local function VersionText(info)
  local mode = info and info.mode
  if mode == "version" and info.ver and info.ver.vid and type(ns.ItemName) == "function" then
    return "Prices for this version only (" .. ns.ItemName(info.ver.vid) .. ")."
  elseif mode == "unknown" then
    return "The price data does not have this version, so this only follows the cheapest listing of this same version."
  end
  return nil
end

-- The tooltip of the Sell now button.
local function NowText(info, postWord)
  local lines = { "Fills the price box with 1 copper under the cheapest listing, so yours is the cheapest." }
  lines[#lines + 1] = BasisText(info)
  lines[#lines + 1] = VersionText(info)
  local P = info and info.P
  local rec = info and info.rec
  if type(rec) == "table" and rec.far then
    lines[#lines + 1] = "The cheapest listing is far above recent prices, so it may not sell soon."
  end
  local mkt = type(rec) == "table" and Num(rec.marketNow) or nil
  local nowP = type(P) == "table" and Num(P.now) or nil
  if mkt and nowP and mkt > 0 and nowP < mkt * 0.9 then
    lines[#lines + 1] = "Today's market price is higher (" .. MoneyI(mkt) .. "): waiting may get you more."
  end
  if type(P) == "table" and P.bestWhy == "history" then
    lines[#lines + 1] = "There is not enough price history yet for a suggested price."
  elseif type(P) == "table" and P.bestWhy == "unreliable" then
    lines[#lines + 1] = "This item's price may not be reliable (see the chart notes), so there is no suggested price."
  end
  if type(P) == "table" and P.nowVendor then lines[#lines + 1] = "A vendor pays more than this after the 5% cut." end
  lines[#lines + 1] = "Nothing is posted: you still click " .. (postWord or "Post") .. "."
  return table.concat(lines, " ")
end

-- The tooltip of the Suggested (price) button.
local function BestText(info, postWord)
  local lines = { "Fills the price box with what this item usually sells for (from the price history), never under a normal cheapest listing. It can take longer to sell than Sell now." }
  lines[#lines + 1] = BasisText(info)
  lines[#lines + 1] = VersionText(info)
  local P = info and info.P
  -- Only when the button shows that price (a live listing can lift it).
  if type(P) == "table" and type(P.rec) == "table" and P.rec.settled and P.rec.settledHours
    and P.best ~= nil and P.best == P.rec.sell then
    lines[#lines + 1] = "The price fell and stayed down, so this is the highest price of the last " .. P.rec.settledHours .. " hours."
  end
  if type(P) == "table" and P.bestVendor then lines[#lines + 1] = "A vendor pays more than this after the 5% cut." end
  lines[#lines + 1] = "Nothing is posted: you still click " .. (postWord or "Post") .. "."
  return table.concat(lines, " ")
end

local function AttachTooltip(btn, title, body)
  btn.tipTitle, btn.tipBody = title, body
  btn:SetScript("OnEnter", function(self)
    local tt = _G.GameTooltip
    if not tt then return end
    tt:SetOwner(self, "ANCHOR_RIGHT")
    tt:AddLine(self.tipTitle or title, 1, 1, 1)
    if self.tipBody then tt:AddLine(self.tipBody, nil, nil, nil, true) end
    tt:Show()
  end)
  btn:SetScript("OnLeave", function(self)
    local tt = _G.GameTooltip
    if tt and tt.GetOwner and tt:GetOwner() == self then tt:Hide() end
  end)
end

local function NewButton(parent, w, h)
  local ok, b = pcall(CreateFrame, "Button", nil, parent, "UIPanelButtonTemplate")
  if not ok or not b then
    b = CreateFrame("Button", nil, parent)
    local fs = b:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
    fs:SetPoint("CENTER", b, "CENTER", 0, 0)
    b:SetFontString(fs)
    local bg = b:CreateTexture(nil, "BACKGROUND")
    bg:SetAllPoints(b)
    bg:SetColorTexture(0.12, 0.12, 0.14, 0.9)
    local hl = b:CreateTexture(nil, "HIGHLIGHT")
    hl:SetAllPoints(b)
    hl:SetColorTexture(1, 1, 1, 0.10)
  end
  b:SetSize(w, h)
  return b
end

local function RaiseAbove(btn, ref, fallbackParent)
  local lvl
  if type(ref) == "table" and ref.GetFrameLevel then lvl = Num(ref:GetFrameLevel()) end
  if not lvl and fallbackParent and fallbackParent.GetFrameLevel then
    lvl = (Num(fallbackParent:GetFrameLevel()) or 0) + 10
  end
  if lvl then btn:SetFrameLevel(lvl + 1) end
end

---------------------------------------------------------------------------
-- The pair of price buttons: "Sell now: X" and "Suggested: X"
---------------------------------------------------------------------------
local PAIR_W, PAIR_H, PAIR_GAP = 136, 20, 4

local function SmallFont(b)
  if type(b.SetNormalFontObject) == "function" then pcall(b.SetNormalFontObject, b, "GameFontNormalSmall") end
  if type(b.SetHighlightFontObject) == "function" then pcall(b.SetHighlightFontObject, b, "GameFontHighlightSmall") end
end

-- place(pair, both) anchors the buttons (both = the two are shown).
local function NewPair(parent, isCommodity, place, onClick)
  local pair = { isCommodity = isCommodity, place = place }
  for _, kind in ipairs({ "now", "best" }) do
    local b = NewButton(parent, PAIR_W, PAIR_H)
    SmallFont(b)
    b.kind, b.isCommodity, b.pair = kind, isCommodity, pair
    b:SetScript("OnClick", function() onClick(kind) end)
    b:Hide()
    pair[kind] = b
  end
  AttachTooltip(pair.now, "Sell now", "Fills the price box with 1 copper under the cheapest listing. Nothing is posted: you still click Post.")
  AttachTooltip(pair.best, "Suggested price", "Fills the price box with what this item usually sells for. Nothing is posted: you still click Post.")
  return pair
end

-- Two buttons side by side under anchor, or one in the middle.
local function PlaceUnder(pair, anchor, both, dy)
  local now, best = pair.now, pair.best
  now:ClearAllPoints()
  best:ClearAllPoints()
  if both then
    now:SetPoint("TOPRIGHT", anchor, "BOTTOM", -PAIR_GAP / 2, dy)
    best:SetPoint("TOPLEFT", anchor, "BOTTOM", PAIR_GAP / 2, dy)
  else
    now:SetPoint("TOP", anchor, "BOTTOM", 0, dy)
    best:SetPoint("TOP", anchor, "BOTTOM", 0, dy)
  end
end

local function SetPairPrices(pair, now, best, info, postWord)
  pair.now.price, pair.best.price = now, best
  if now then
    pair.now:SetText("Sell now: " .. MoneyI(now))
    pair.now.tipBody = NowText(info, postWord)
    pair.now:Show()
  else
    pair.now:Hide()
  end
  if best then
    pair.best:SetText("Suggested: " .. MoneyI(best))
    pair.best.tipBody = BestText(info, postWord)
    pair.best:Show()
  else
    pair.best:Hide()
  end
  if (now or best) and type(pair.place) == "function" then pcall(pair.place, pair, now ~= nil and best ~= nil) end
end

local function HidePair(pair)
  if not pair then return end
  pair.now.price, pair.best.price = nil, nil
  pair.now:Hide()
  pair.best:Hide()
end

---------------------------------------------------------------------------
-- Switching the chart item
---------------------------------------------------------------------------
local pendingToken = 0

local function BaseOf(id)
  if type(ns.BaseItemID) == "function" then
    local ok, b = pcall(ns.BaseItemID, id)
    if ok and Num(b) then return b end
  end
  return id
end

-- tf (optional): a timeframe for this item only (an item nothing is listed
-- for shows its last 30 days); the saved timeframe stays as it is.
local function ApplyItem(id, tf)
  id = Num(tonumber(id))
  if not id or id <= 0 then return end
  -- The sidebar searched for one version of this item: show that version.
  local v = AH.expectVid
  if v and v ~= id and BaseOf(v) == id then id = v end
  AH.itemID = id
  if not AH.open or not ChartWanted() then return end
  if type(ns.ShowChart) == "function" then ns.ShowChart(id, "ah", tf) end
end

-- An explicit choice (clicked a result, put an item in the sell slot).
-- exact = true: this very item or version (an item in the sell slot), so an
-- earlier sidebar search for a version of it no longer applies.
local function SetItemNow(id, exact)
  pendingToken = pendingToken + 1
  AH.pendingSearch, AH.pendingIDs, AH.expectID = nil, nil, nil
  if exact then AH.expectVid = nil end
  ApplyItem(id)
end

-- ns.SearchAH (the sidebar) says which item it is about to search for, so a
-- name several items share (Squire's Shirt) keeps that item, not the lowest id.
local EXPECT_SECONDS = 10
local function NowSeconds()
  if type(ns.Now) ~= "function" then return 0 end
  local ok, t = pcall(ns.Now)
  return ok and Num(t) or 0
end

local expectToken = 0
-- id may be a version's id: the search is for its item, and the chart then
-- shows that version (for a few seconds of results and clicks).
function ns.AH_ExpectItem(id)
  expectToken = expectToken + 1
  local n = Num(tonumber(id))
  local base = n and BaseOf(n) or nil
  AH.expectID = base
  AH.expectVid = (n and base ~= n) and n or nil
  AH.expectAt = NowSeconds()
  if AH.expectVid then
    local tok = expectToken
    After(EXPECT_SECONDS, function()
      if tok == expectToken then AH.expectVid = nil end
    end)
  end
end
AH.ExpectItem = ns.AH_ExpectItem

-- The expected item when this search found it (and it is recent), else nil.
local function ExpectedIn(ids)
  local want = AH.expectID
  if not want then return nil end
  if NowSeconds() - (AH.expectAt or 0) > EXPECT_SECONDS then
    AH.expectID, AH.expectVid = nil, nil
    return nil
  end
  for _, v in ipairs(ids) do
    if v == want then
      -- One search can reach two hooks: keep it a moment, then forget it,
      -- so a search typed later starts fresh.
      local tok = expectToken
      After(1, function()
        if tok == expectToken then AH.expectID = nil end
      end)
      return want
    end
  end
  AH.expectID = nil -- a search for something else
  AH.expectVid = nil
  return nil
end

-- A softer signal (search text, browse results). Waits a moment so a burst of
-- searches from a shopping list only redraws the chart once.
local function SetItemSoon(id, tf)
  pendingToken = pendingToken + 1
  local tok = pendingToken
  After(0.15, function()
    if tok == pendingToken then ApplyItem(id, tf) end
  end)
end
AH.SetItemNow, AH.SetItemSoon = SetItemNow, SetItemSoon

local function CleanSearch(text)
  if type(text) ~= "string" or IsSecret(text) then return nil end
  local s = text:match("^[^;]*") or text           -- Auctionator: "term;category;..."
  s = s:gsub("^%s+", ""):gsub("%s+$", "")
  s = s:gsub('^"', ""):gsub('"$', "")               -- Auctionator exact search: "term"
  s = s:gsub("^%s+", ""):gsub("%s+$", "")
  if s == "" then return nil end
  return s
end

---------------------------------------------------------------------------
-- A search the auction house found nothing for ("No items found"). The
-- chart shows the item anyway (its last 30 days, with the sales bars), and a
-- few short lines under Blizzard's text say when it was last listed and how
-- many sold, then the lines other files add (ns.EmptySearchLines). They go
-- as soon as a new search starts or results come in.
---------------------------------------------------------------------------
local EMPTY_MAX_LINES = 7
local emptyFS, emptyParent   -- our text under "No items found", and the frame it is on

-- An item's prices of one timeframe (ns.GetSeries), or nil without any.
local function SeriesOf(id, tf)
  local s = type(ns.GetSeries) == "function" and ns.GetSeries(id, tf) or nil
  if type(s) == "table" and (Num(s.n) or 0) > 0 and type(s.cheap) == "table" and type(s.sold) == "table" then return s end
  return nil
end

-- Of two series, the one that goes further back.
local function Longer(a, b)
  if not a then return b end
  if not b then return a end
  return (b.n * (Num(b.step) or 0) > a.n * (Num(a.step) or 0)) and b or a
end

-- How far back a series goes, for "in the last ...": "hour", "47 hours",
-- "day", "3 days" (the data only holds what the collector has seen so far).
local function SpanText(s)
  local n = Num(s.n) or 0
  if (Num(s.step) or 0) >= 86400 then return n <= 1 and "day" or (n .. " days") end
  if n <= 1 then return "hour" end
  if n < 48 then return n .. " hours" end
  return floor(n / 24) .. " days"
end

-- The price and time of the last listing the data saw (the hourly prices of
-- the last week first, then the daily ones), or nil and the series that goes
-- furthest back (for "none listed in the last ...").
local function LastListed(id)
  local db = type(ns.db) == "table" and ns.db or {}
  -- Snapshots are taken a few minutes into each hour: that many minutes after the hour's start.
  local off = (tonumber(db.updated) or 0) - (tonumber(db.hourEnd) or 0)
  if off < 0 or off >= 3600 then off = 0 end
  local longest
  for _, tf in ipairs({ "1W", "1Y" }) do
    local s = SeriesOf(id, tf)
    if s then
      longest = Longer(longest, s)
      for i = s.n, 1, -1 do
        local p = Num(s.cheap[i])
        if p and p > 0 then
          return p, (Num(s.times[i]) or 0) + (((Num(s.step) or 0) >= 86400) and 43200 or off)
        end
      end
    end
  end
  return nil, longest
end

-- How many sold: "About 45 sold a day." (ns.SoldPerDay, as the chart says
-- it), else the sales of the hours, or the days, the data has ("About 7 sold
-- in the last 3 days."), else "No sales seen in the last 3 days.".
local function SoldLine(id, item)
  local num = type(ns.Num) == "function" and ns.Num or tostring
  local per = type(ns.SoldPerDay) == "function" and ns.SoldPerDay(item) or nil
  if Num(per) and per > 0 then return "About " .. num(per) .. " sold a day." end
  local longest
  for _, tf in ipairs({ "1W", "1M" }) do
    local s = SeriesOf(id, tf)
    if s then
      local n = 0
      for i = 1, s.n do n = n + (Num(s.sold[i]) or 0) end
      if n > 0 then return "About " .. num(n) .. " sold in the last " .. SpanText(s) .. "." end
      longest = Longer(longest, s)
    end
  end
  if longest then return "No sales seen in the last " .. SpanText(longest) .. "." end
  return "No sales seen yet."
end

-- What the game knows about an item by its name (only items it has loaded):
-- its bind type (1 bind on pickup, 4 quest item) and its id, or nil.
local function InfoByName(text)
  local CI = _G.C_Item
  for _, f in ipairs({ type(CI) == "table" and CI.GetItemInfo or false, _G.GetItemInfo or false }) do
    if type(f) == "function" then
      local r = { pcall(f, text) }
      if r[1] and type(r[2]) == "string" and not IsSecret(r[2]) then return Num(r[15]), IDFromLink(r[3]) end
    end
  end
  return nil
end

-- The lines for a search with no results, and the item to chart (or nil).
-- "Not listed now" only when that search would have found the item: its name
-- is the text or holds the whole text (a name that only has every word of it
-- may not match what the server searched for: "try its full name"), and no
-- category, level range or filter could have hidden it.
local function EmptyLines(text)
  local lines, chartID = {}, nil
  local function add(l) lines[#lines + 1] = l end
  local id, how
  if type(ns.FindSearchItem) == "function" then
    local ok, a, b = pcall(ns.FindSearchItem, text)
    if ok then id, how = Num(a), b end
  end
  local item = id and type(ns.GetItem) == "function" and ns.GetItem(id) or nil
  if item then
    chartID = id
    local loose = how == "words" or (how ~= "exact" and AH.queryExact)
    local sure = not loose and not AH.queryFiltered
    if how ~= "exact" and type(ns.ItemName) == "function" then
      add("Best match: " .. ns.ItemName(id) .. (loose and " - try its full name" or ""))
    end
    if AH.queryFiltered then add("Your AH filters may hide it.") end
    local lead = sure and "Not listed now. " or ""
    local p, t = LastListed(id)
    if p then
      local ago = type(ns.AgeText) == "function" and ns.AgeText(max(0, NowSeconds() - t)) or ""
      add(lead .. "Last listed " .. ago .. " at " .. MoneyI(p) .. ".")
    elseif t then
      add(lead .. "None listed in the last " .. SpanText(t) .. ".")
    else
      add(lead .. "None listed yet.")
    end
    add(SoldLine(id, item))
  else
    id = nil
    local bind, gid = InfoByName(text)
    if bind == 1 then
      add("Bind on pickup: never on the AH.")
    elseif bind == 4 then
      add("Quest item: never on the AH.")
    else
      add("No AH prices for that name.")
    end
    chartID = gid
  end
  -- Other files' lines (craft mats ...): numbered entries first, then named ones by name.
  local ext, fns = ns.EmptySearchLines, {}
  if type(ext) == "table" then
    for _, fn in ipairs(ext) do fns[#fns + 1] = fn end
    local named = {}
    for k in pairs(ext) do
      if type(k) == "string" then named[#named + 1] = k end
    end
    table.sort(named)
    for _, k in ipairs(named) do fns[#fns + 1] = ext[k] end
  end
  for _, fn in ipairs(fns) do
    if type(fn) == "function" then
      local ok, r = pcall(fn, text, id)
      if not ok then
        AH.emptyError = r   -- that file's lines are left out
      elseif type(r) == "string" and r ~= "" and not IsSecret(r) then
        add(r)
      elseif type(r) == "table" then
        for _, l in ipairs(r) do
          if type(l) == "string" and l ~= "" and not IsSecret(l) then add(l) end
        end
      end
    end
  end
  while #lines > EMPTY_MAX_LINES do lines[#lines] = nil end
  return lines, chartID
end

local function ClearEmptySearch()
  AH.emptyLines = nil
  if emptyFS then
    emptyFS:SetText("")
    emptyFS:Hide()
  end
end
AH.ClearEmptySearch = ClearEmptySearch

-- Our lines on the browse results' list, right under Blizzard's "No items
-- found" (its ResultsText, as wide as it). They hide with the list.
local function ShowEmptyLines(lines)
  local f = _G.AuctionHouseFrame
  local br = type(f) == "table" and f.BrowseResultsFrame or nil
  local list = type(br) == "table" and br.ItemList or nil
  local parent = type(list) == "table" and list or br
  if type(parent) ~= "table" or type(parent.CreateFontString) ~= "function" then return false end
  if not emptyFS or emptyParent ~= parent then
    if emptyFS then emptyFS:Hide() end
    emptyFS = parent:CreateFontString(nil, "OVERLAY", "GameFontHighlight")
    emptyParent = parent
    AH.emptyText = emptyFS
    emptyFS:SetJustifyH("CENTER")
    emptyFS:SetJustifyV("TOP")
    if type(emptyFS.SetSpacing) == "function" then emptyFS:SetSpacing(3) end
  end
  local rt = type(list) == "table" and list.ResultsText or nil
  emptyFS:ClearAllPoints()
  if type(rt) == "table" and type(rt.GetObjectType) == "function" then
    emptyFS:SetPoint("TOPLEFT", rt, "BOTTOMLEFT", 0, -10)
    emptyFS:SetPoint("TOPRIGHT", rt, "BOTTOMRIGHT", 0, -10)
  else
    emptyFS:SetPoint("TOPLEFT", parent, "TOPLEFT", 45, -70)
    emptyFS:SetPoint("TOPRIGHT", parent, "TOPRIGHT", -67, -70)
  end
  emptyFS:SetText(table.concat(lines, "\n"))
  emptyFS:Show()
  AH.emptyLines = lines
  return true
end

local function ShowEmptySearch(text)
  local lines, chartID = EmptyLines(text)
  ShowEmptyLines(lines)
  if chartID then
    -- 30 days once its daily prices go back further than the week of hourly
    -- ones; until then the week (many more points).
    local item = type(ns.GetItem) == "function" and ns.GetItem(chartID) or nil
    local days = (item and type(ns.DailyPoints) == "function") and ns.DailyPoints(chartID) or 0
    SetItemSoon(chartID, item and (days > 7 and "1M" or "1W") or nil)
  end
end
AH.ShowEmptySearch = ShowEmptySearch

-- A browse search: a new set of rows (the cheapest per item of them wins),
-- and what shows under "No items found" was for the last one. text is nil
-- for searches nobody typed (Favorites, an addon's list of items).
local function NewBrowseSearch(text, filtered, exact)
  AH.browseGen = (AH.browseGen or 0) + 1
  AH.lastSearch = text
  AH.queryFiltered, AH.queryExact = filtered == true, exact == true
  ClearEmptySearch()
end

-- Whether a browse query could hide an item that has the text in its name:
-- a category, a level range, or filters other than Blizzard's defaults
-- (every quality; "usable only" and the like hide items). Also whether it
-- matched whole names only (ExactMatch). An empty filter list hides nothing.
local function QueryFiltered(query)
  if type(query) ~= "table" then return false, false end
  local E = type(_G.Enum) == "table" and _G.Enum.AuctionHouseFilter or nil
  local exactF = type(E) == "table" and E.ExactMatch or nil
  local filtered = false
  local cf = query.itemClassFilters
  if type(cf) == "table" and next(cf) ~= nil then filtered = true end
  if (Num(query.minLevel) or 0) > 0 or (Num(query.maxLevel) or 0) > 0 then filtered = true end
  local f, exact = query.filters, false
  if type(f) == "table" and #f > 0 then
    local quality = {}
    local D = _G.AUCTION_HOUSE_DEFAULT_FILTERS
    if type(D) == "table" then
      for k, v in pairs(D) do if v == true then quality[k] = true end end
    elseif type(E) == "table" then
      for _, n in ipairs({ "PoorQuality", "CommonQuality", "UncommonQuality", "RareQuality", "EpicQuality", "LegendaryQuality" }) do
        if E[n] ~= nil then quality[E[n]] = true end
      end
    end
    local seen, n, total = {}, 0, 0
    for _ in pairs(quality) do total = total + 1 end
    for _, v in ipairs(f) do
      if exactF ~= nil and v == exactF then
        exact = true
      elseif not quality[v] then
        filtered = true
      elseif not seen[v] then
        seen[v], n = true, n + 1
      end
    end
    if n > 0 and n < total then filtered = true end
  end
  return filtered, exact
end
AH.QueryFiltered = QueryFiltered

-- query: the browse query (C_AuctionHouse.SendBrowseQuery), which says
-- whether filters were on. Without it (the frame's own SendBrowseQuery, which
-- calls that one first) the filters stay as that query left them.
local function OnSearch(text, query)
  if not AH.open then return end
  local s = CleanSearch(text)
  -- A new search: what shows under "No items found" was for the last one.
  if type(query) == "table" then
    NewBrowseSearch(s, QueryFiltered(query))
  else
    AH.lastSearch = s
    ClearEmptySearch()
  end
  if not s then return end -- empty text is a full scan, not the player searching
  AH.pendingSearch, AH.pendingIDs = s:lower(), nil
  local ids
  if type(ns.FindItemIDs) == "function" then
    ids = ns.FindItemIDs(s)
  elseif type(ns.FindItemID) == "function" then
    ids = { ns.FindItemID(s) }
  end
  if type(ids) ~= "table" then ids = {} end
  local expected = ExpectedIn(ids)
  if expected then ids = { expected } end
  local id = ids[1]
  if id then
    if #ids > 1 then
      -- Several items share this name (Squire's Shirt): show the first now and
      -- let the search results say which one is actually listed.
      AH.pendingIDs = {}
      for _, v in ipairs(ids) do AH.pendingIDs[v] = true end
    else
      AH.pendingSearch = nil
    end
    SetItemSoon(id)
  end
end
AH.OnSearch = OnSearch

-- Pick the item to chart from a list of candidate itemIDs: an exact name match
-- for the search text first, else the first one we have prices for. When the
-- name belongs to several items, only one of those is picked.
local function PickFromIDs(ids)
  local want, only = AH.pendingSearch, AH.pendingIDs
  local first
  for _, id in ipairs(ids) do
    local it = type(ns.GetItem) == "function" and ns.GetItem(id) or nil
    if it then
      if only then
        if only[id] then return id end
      else
        local nm = it.name
        if want and type(nm) == "string" and nm:lower() == want then return id end
        first = first or id
      end
    end
  end
  return first
end

-- True when a browse row's price is per unit: a commodity's (Blizzard shows
-- its minPrice as the unit price), or an item's that never stacks. A
-- stackable item's row may be the price of a whole stack: left out.
local function PerUnitRow(key)
  local CAH = _G.C_AuctionHouse
  if type(CAH) == "table" and type(CAH.GetItemKeyInfo) == "function" then
    local ok, info = pcall(CAH.GetItemKeyInfo, key)
    if ok and type(info) == "table" and info.isCommodity == true then return true end
  end
  local CI = _G.C_Item
  local f = (type(CI) == "table" and CI.GetItemInfo) or _G.GetItemInfo
  if type(f) == "function" then
    local r = { pcall(f, key.itemID) }
    if r[1] and Num(r[9]) == 1 then return true end   -- itemStackCount is the 8th value
  end
  return false
end

-- Each browse row's price for ns.LivePrice: the cheapest buyout per unit,
-- whether you have one listed (containsOwnerItem) and when it came in. Rows
-- of a version ("of the Bear": the item key has a suffix), pets and prices
-- that may be per stack are skipped; a row with none available now drops
-- what the item had. Rows of one search (several per item, or added a page
-- at a time) keep the cheapest. added: rows added to the list (the event's
-- own list), else the whole list.
local function StoreBrowsePrices(list, added)
  if type(list) ~= "table" then return end
  local gen, t, seen = AH.browseGen, Clock(), {}
  for i, r in ipairs(list) do
    if i > 500 then break end
    local key = type(r) == "table" and r.itemKey or nil
    local id = type(key) == "table" and Num(key.itemID) or nil
    if id and (key.itemSuffix == nil or Num(key.itemSuffix) == 0)
      and (key.battlePetSpeciesID == nil or Num(key.battlePetSpeciesID) == 0) then
      local old = AH.browse[id]
      local same = seen[id] or (added and type(old) == "table" and old.gen == gen)
      local p, q = Num(r.minPrice), Num(r.totalQuantity)
      if p and p > 0 and q and q > 0 and PerUnitRow(key) then
        if not (same and type(old) == "table" and (Num(old.price) or 0) <= p) then
          AH.browse[id] = { price = p, own = r.containsOwnerItem == true, at = t, gen = gen }
        end
        seen[id] = true
      elseif not same then
        AH.browse[id] = nil
      end
    end
  end
end

-- added: AUCTION_HOUSE_BROWSE_RESULTS_ADDED's rows (only those are read for
-- prices; the whole list only while a search waits for its item).
local function OnBrowseResults(added)
  if not AH.open then return end
  local CAH = _G.C_AuctionHouse
  if type(CAH) ~= "table" then return end
  if type(added) == "table" then
    StoreBrowsePrices(added, true)
    if #added == 0 then return end
    AH.lastSearch = nil   -- this search found something
    ClearEmptySearch()
    if not AH.pendingSearch then return end
  end
  if type(CAH.GetBrowseResults) ~= "function" then return end
  local ok, list = pcall(CAH.GetBrowseResults)
  if not ok or type(list) ~= "table" then return end
  if type(added) ~= "table" then StoreBrowsePrices(list) end
  if #list == 0 then
    -- "No items found" for a search the player typed (once all results are in).
    local full = true
    if type(CAH.HasFullBrowseResults) == "function" then
      local okF, v = pcall(CAH.HasFullBrowseResults)
      if okF and v == false then full = false end
    end
    if full and AH.lastSearch then
      local text = AH.lastSearch
      AH.lastSearch, AH.pendingSearch, AH.pendingIDs = nil, nil, nil
      ShowEmptySearch(text)
    end
    return
  end
  AH.lastSearch = nil   -- this search found something
  ClearEmptySearch()
  if not AH.pendingSearch then return end
  local ids = {}
  for i, r in ipairs(list) do
    if i > 200 then break end
    local id = type(r) == "table" and type(r.itemKey) == "table" and Num(r.itemKey.itemID)
    if id then ids[#ids + 1] = id end
  end
  local pick = PickFromIDs(ids)
  if pick then
    AH.pendingSearch, AH.pendingIDs = nil, nil
    SetItemSoon(pick)
  end
end

---------------------------------------------------------------------------
-- Chart toggle button on the AH frame
---------------------------------------------------------------------------
local function ToggleClicked()
  if type(ns.ChartIsShown) == "function" and ns.ChartIsShown() then
    if type(ns.settings) == "table" then ns.settings.chart = false end
    if type(ns.HideChart) == "function" then ns.HideChart() end
  else
    if type(ns.settings) == "table" then ns.settings.chart = true end
    if type(ns.ShowChart) == "function" then ns.ShowChart(AH.itemID, "ah") end
  end
end
AH.ToggleClicked = ToggleClicked

local function CreateToggleButton(parent, kind)
  if type(parent) ~= "table" or toggles[parent] then return end
  local b = NewButton(parent, 64, 20)
  if kind == "legacy" then
    b:SetPoint("TOPRIGHT", parent, "TOPRIGHT", -64, -14)
  else
    b:SetPoint("TOPRIGHT", parent, "TOPRIGHT", -28, -1)
  end
  b:SetText("Chart")
  RaiseAbove(b, nil, parent)
  b:SetScript("OnClick", ToggleClicked)
  AttachTooltip(b, "Goblin Ledger price chart", "Show or hide the price chart under the auction house.")
  toggles[parent] = b
  return b
end

---------------------------------------------------------------------------
-- Blizzard sell frames: track the item and offer the List At price
---------------------------------------------------------------------------
-- Random-suffix gear ("of the Monkey") shares one item ID across very
-- different values, and the price data cannot tell the versions apart.
local function KeyHasSuffix(key)
  local s = type(key) == "table" and Num(key.itemSuffix) or nil
  return s ~= nil and s ~= 0
end

local function LocationHasSuffix(loc)
  if loc == nil then return false end
  local CAH, CI = _G.C_AuctionHouse, _G.C_Item
  if type(CAH) == "table" and type(CAH.GetItemKeyFromItem) == "function" then
    local ok, key = pcall(CAH.GetItemKeyFromItem, loc)
    if ok and KeyHasSuffix(key) then return true end
  end
  if type(CI) == "table" and type(CI.GetItemLink) == "function" and type(ns.LinkHasSuffix) == "function" then
    local ok, link = pcall(CI.GetItemLink, loc)
    if ok and ns.LinkHasSuffix(link) then return true end
  end
  return false
end

local function NoteSuffix(id, yes)
  id = Num(tonumber(id))
  if id and yes then AH.suffix[id] = true end
end

-- Extra notes for the chart footer about an item (Chart.lua shows them).
-- An item whose versions differ (the data lists them, or one was seen in
-- the auction house) shows prices that mix every version.
function ns.PricesMixVersions(itemID)
  local id = tonumber(itemID) or -1
  if type(ns.IsVariant) == "function" and ns.IsVariant(id) then return false end
  return (AH.suffix[id] or (type(ns.HasVariants) == "function" and ns.HasVariants(id))) and true or false
end

function ns.ItemNotes(itemID)
  if ns.PricesMixVersions(itemID) then
    return { "Price covers all versions of this item" }
  end
  return nil
end

local function LocationVersion(id, loc)
  return VersionInfo(id, LinkFromLocation(loc), LocationHasSuffix(loc))
end

local function UpdateUseButton(sf)
  local pair = useButtons[sf]
  if not pair then return end
  local now, best, info
  if AH.open then now, best, info = SellAdvice(sellIDs[sf], pair.isCommodity, sellVer[sf]) end
  SetPairPrices(pair, now, best, info, "Post")
end

-- Fills the price box with the Sell now ("now") or Suggested price ("best") price.
local function ApplySellPrice(sf, kind)
  local pair = useButtons[sf]
  if not sellIDs[sf] and type(sf.GetItem) == "function" then
    local ok, loc = pcall(sf.GetItem, sf)
    if ok and loc ~= nil then
      sellIDs[sf] = ItemIDFromLocation(loc)
      sellVer[sf] = LocationVersion(sellIDs[sf], loc)
    end
  end
  local b = pair and pair[kind == "best" and "best" or "now"]
  if b and not b.price then UpdateUseButton(sf) end
  -- Fill exactly the price the button shows.
  local price = b and b.price
  if not price then
    Print("no price advice for this item yet.")
    return false
  end
  local input = sf.PriceInput
  if type(input) ~= "table" or type(input.SetAmount) ~= "function" then
    Print("could not find the price box. Type the price in yourself: " .. MoneyI(price))
    return false
  end
  local ok = pcall(input.SetAmount, input, price)
  if not ok then
    Print("could not fill the price box. Type the price in yourself: " .. MoneyI(price))
    return false
  end
  if type(sf.UpdatePostState) == "function" then pcall(sf.UpdatePostState, sf) end
  return true
end
AH.ApplySellPrice = ApplySellPrice

-- The two buttons sit side by side just under the Post button (the same spot
-- the single button had, which stays clear of the sell frame's own boxes).
local function CreateUseButton(sf, isCommodity)
  if type(sf) ~= "table" or useButtons[sf] then return end
  local function place(pair, both)
    if type(sf.PostButton) == "table" then
      PlaceUnder(pair, sf.PostButton, both, -6)
    else
      pair.now:ClearAllPoints()
      pair.best:ClearAllPoints()
      pair.best:SetPoint("BOTTOMRIGHT", sf, "BOTTOMRIGHT", -12, 12)
      pair.now:SetPoint("BOTTOMRIGHT", sf, "BOTTOMRIGHT", -12 - (both and (PAIR_W + PAIR_GAP) or 0), 12)
    end
  end
  local pair = NewPair(sf, isCommodity, place, function(kind) ApplySellPrice(sf, kind) end)
  place(pair, true)
  RaiseAbove(pair.now, sf.PostButton, sf)
  RaiseAbove(pair.best, sf.PostButton, sf)
  useButtons[sf] = pair
end

local function OnSellItem(sf, loc)
  local id = loc ~= nil and ItemIDFromLocation(loc) or nil
  sellIDs[sf] = id
  sellVer[sf] = id ~= nil and LocationVersion(id, loc) or nil
  NoteSuffix(id, sellVer[sf] and sellVer[sf].suffix)
  UpdateUseButton(sf)
  if id then SetItemNow((sellVer[sf] and sellVer[sf].vid) or id, true) end
end

---------------------------------------------------------------------------
-- Price arrows: a tiny up and down arrow in each gold, silver and copper box
-- of the price boxes. Up adds 1 of that coin, down takes 1 away (carrying
-- over like money does: 1s 99c + 1c = 2s). Holding one keeps going. The new
-- price goes in the same way the Sell now button fills it.
---------------------------------------------------------------------------
local ARROW_W, ARROW_H = 9, 8
local HOLD_DELAY, HOLD_EVERY = 0.4, 0.08
local COIN_STEP = { gold = 10000, silver = 100, copper = 1 }
local arrowSets = setmetatable({}, { __mode = "k" })   -- money box frame -> { get, put, max, gold = { up, down }, ... }
AH.arrowSets = arrowSets
AH.HOLD_DELAY, AH.HOLD_EVERY = HOLD_DELAY, HOLD_EVERY

-- The gold, silver or copper edit box of a money box: GoldBox in the
-- auction house's own boxes, gold (or the global <name>Gold) in the older ones.
local function CoinBox(mf, coin)
  local word = coin:sub(1, 1):upper() .. coin:sub(2)
  local b = mf[word .. "Box"] or mf[coin]
  if type(b) ~= "table" and type(mf.GetName) == "function" then
    local ok, name = pcall(mf.GetName, mf)
    if ok and type(name) == "string" and not IsSecret(name) then b = _G[name .. word] end
  end
  if type(b) == "table" and type(b.GetObjectType) == "function" then return b end
  return nil
end

-- The highest price the gold box can hold (7 digits unless it says).
local function MaxCopper(goldBox)
  local digits = 7
  if goldBox and type(goldBox.GetMaxLetters) == "function" then
    local ok, n = pcall(goldBox.GetMaxLetters, goldBox)
    n = ok and Num(n) or nil
    if n and n >= 1 and n <= 9 then digits = floor(n) end
  end
  return (10 ^ digits - 1) * 10000 + 9999
end

-- One step: reads the price from the boxes (so what was just typed counts),
-- adds or takes one coin and writes it back. Does nothing when that would go
-- under 0 or past what the gold box holds, or while the box is turned off.
local function ArrowStep(b)
  local set, box = b.arrowSet, b:GetParent()
  if type(set) ~= "table" or type(box) ~= "table" then return false end
  if type(box.IsEnabled) == "function" then
    local ok, on = pcall(box.IsEnabled, box)
    if ok and on == false then return false end
  end
  local ok, now = pcall(set.get)
  now = ok and Num(now) or nil
  if not now then return false end
  now = floor(now + 0.5)
  local new = now + b.dir * (COIN_STEP[b.coin] or 1)
  if new < 0 or new > set.max then return false end
  return (pcall(set.put, new))
end
AH.ArrowStep = ArrowStep

local function StopHold(b)
  b.holding = nil
  b:SetScript("OnUpdate", nil)
end

-- While an arrow is held: after a short pause, one more step every so often
-- (at most one per frame). Stops when the mouse button is up.
local function HoldTick(b, elapsed)
  local down = _G.IsMouseButtonDown
  if type(down) == "function" then
    local ok, v = pcall(down, "LeftButton")
    if ok and not v then return StopHold(b) end
  end
  b.wait = (b.wait or 0) - (Num(elapsed) or 0)
  if b.wait > 0 then return end
  b.wait = max(0, b.wait + HOLD_EVERY)
  ArrowStep(b)
end

local function HoldUpdate(b, elapsed)
  local ok, err = pcall(HoldTick, b, elapsed)
  if not ok then
    AH.lastError = err
    pcall(StopHold, b)
  end
end

local function ArrowMouseDown(b, mouse)
  local ok, err = pcall(function()
    if mouse ~= nil and mouse ~= "LeftButton" then return end
    b.holding, b.wait = true, HOLD_DELAY
    ArrowStep(b)
    b:SetScript("OnUpdate", HoldUpdate)
  end)
  if not ok then AH.lastError = err end
end

local function ArrowStop(b)
  local ok, err = pcall(StopHold, b)
  if not ok then AH.lastError = err end
end

-- The game's small yellow arrow (pushed: Arrow-Up-Down). Its art has empty
-- space around the arrow, so only the arrow part is shown; down is the same
-- arrow turned over.
local ARROW_UP, ARROW_PUSHED = "Interface\\Buttons\\Arrow-Up-Up", "Interface\\Buttons\\Arrow-Up-Down"
local ARROW_COORD = { [1] = { 0, 0.8125, 0.1875, 0.875 }, [-1] = { 0, 0.8125, 0.875, 0.1875 } }

-- How far across a frame an anchor point is: 0 its left edge, 1 its right.
local function AcrossOf(p)
  if type(p) ~= "string" then return nil end
  if p:find("LEFT") then return 0 elseif p:find("RIGHT") then return 1 end
  return 0.5
end

-- Where a box's coin icon sits, as its left and right x measured from the
-- box's right edge, read from how the icon is anchored. nil if unknown.
local function CoinSpan(box)
  local icon = box.Icon or box.texture
  if type(icon) ~= "table" or type(icon.GetPoint) ~= "function" then return nil end
  local point, rel, relPoint, x = icon:GetPoint(1)
  if rel ~= nil and not rawequal(rel, box) then return nil end
  local own, at = AcrossOf(point), AcrossOf(relPoint or point)
  x = Num(x)
  if not own or not at or not x then return nil end
  local cw = Num(icon:GetWidth())
  if not cw or cw <= 0 then cw = 13 end
  local base = 0
  if at < 1 then
    local w = Num(box:GetWidth())
    if not w or w <= 0 then return nil end
    base = (at - 1) * w
  end
  local left = base + x - own * cw
  return left, left + cw
end

-- Where a box's arrows go: the x of their right edge from the box's right
-- edge. Normally -1, the strip right of the coin (the auction house boxes).
-- When the coin itself sits in that strip (the game's older money boxes)
-- they go just left of the coin. When the coin hangs over the box's edge
-- there is no room that does not cover it: nil.
local function ArrowSpot(box)
  local ok, left, right = pcall(CoinSpan, box)
  if not ok or not left or not right then return -1 end
  if right <= -(ARROW_W + 1) or left >= -1 then return -1 end
  if right > 0 then return nil end
  local spot = left - 1
  local okW, w = pcall(box.GetWidth, box)
  w = okW and Num(w) or nil
  if w and w > 0 and spot - ARROW_W < -w then return nil end
  return spot
end
AH.ArrowSpot = ArrowSpot

-- An arrow lives in its box (it hides with it), at the x ArrowSpot picked:
-- clear of the coin icon and past where typed digits reach. Up sits just
-- above the middle, down just below.
local function NewArrow(box, set, coin, dir, h, x)
  local b = CreateFrame("Button", nil, box)
  b:SetSize(ARROW_W, h)
  b:SetPoint(dir > 0 and "BOTTOMRIGHT" or "TOPRIGHT", box, "RIGHT", x or -1, 0)
  b:SetNormalTexture(ARROW_UP)
  b:SetPushedTexture(ARROW_PUSHED)
  pcall(b.SetHighlightTexture, b, ARROW_UP, "ADD")
  local c = ARROW_COORD[dir]
  for _, getter in ipairs({ "GetNormalTexture", "GetPushedTexture", "GetHighlightTexture" }) do
    if type(b[getter]) == "function" then
      local ok, t = pcall(b[getter], b)
      if ok and type(t) == "table" and type(t.SetTexCoord) == "function" then
        pcall(t.SetTexCoord, t, c[1], c[2], c[3], c[4])
      end
    end
  end
  b:EnableMouse(true)
  b.arrowSet, b.coin, b.dir, b.isPriceArrow = set, coin, dir, true
  b:SetScript("OnMouseDown", ArrowMouseDown)
  b:SetScript("OnMouseUp", ArrowStop)
  b:SetScript("OnHide", ArrowStop)
  return b
end

-- Adds the arrows to a money box once. get() returns its price in copper and
-- put(copper) writes one. A coin box that is missing simply gets none. If any
-- box has no room beside its coin, the whole money box gets none (never half).
local function AddPriceArrows(mf, get, put)
  if type(mf) ~= "table" or type(get) ~= "function" or type(put) ~= "function" then return nil end
  if arrowSets[mf] ~= nil then return arrowSets[mf] or nil end
  local set = { get = get, put = put, max = MaxCopper(CoinBox(mf, "gold")) }
  local spots = {}
  for _, coin in ipairs({ "gold", "silver", "copper" }) do
    local box = CoinBox(mf, coin)
    if box then
      spots[coin] = ArrowSpot(box)
      if not spots[coin] then
        arrowSets[mf] = false
        return nil
      end
    end
  end
  local any = false
  for _, coin in ipairs({ "gold", "silver", "copper" }) do
    local box, x = CoinBox(mf, coin), spots[coin]
    if box and x then
      local ok, up, down = pcall(function()
        local okH, bh = pcall(box.GetHeight, box)
        bh = okH and Num(bh) or 0
        local h = bh > 0 and max(5, min(ARROW_H, floor((bh - 2) / 2))) or ARROW_H
        return NewArrow(box, set, coin, 1, h, x), NewArrow(box, set, coin, -1, h, x)
      end)
      if ok then
        set[coin] = { up = up, down = down }
        any = true
      else
        AH.lastError = up
      end
    end
  end
  if not any then return nil end
  arrowSets[mf] = set
  return set
end
AH.AddPriceArrows = AddPriceArrows

-- The price boxes of a Blizzard sell frame: the price (buyout), and the
-- starting bid the item frame shows when buyout only is unticked.
local function AddSellArrows(sf)
  if type(sf) ~= "table" then return end
  for _, key in ipairs({ "PriceInput", "SecondaryPriceInput" }) do
    local input = sf[key]
    if type(input) == "table" and type(input.GetAmount) == "function" and type(input.SetAmount) == "function" then
      AddPriceArrows(input.MoneyInputFrame,
        function() return input:GetAmount() end,
        function(v)
          input:SetAmount(v)
          if type(sf.UpdatePostState) == "function" then sf:UpdatePostState() end
        end)
    end
  end
end
AH.AddSellArrows = AddSellArrows

---------------------------------------------------------------------------
-- Auctionator (optional): follow its Shopping and Selling tabs
---------------------------------------------------------------------------
local atrListener = {}
local atrSource = {}
local atrPair   -- { now = button, best = button } in Auctionator's Selling tab

local function AtrSellingVisible()
  local f = _G.AuctionatorSellingFrame
  if type(f) ~= "table" or type(f.IsVisible) ~= "function" then return false end
  local ok, v = pcall(f.IsVisible, f)
  return ok and v == true
end

local function AtrSaleFrame()
  local f = _G.AuctionatorSellingFrame
  local sale = type(f) == "table" and f.SaleItemFrame
  if type(sale) == "table" then return sale end
  return nil
end

local function AtrPriceAmount()
  local sale = AtrSaleFrame()
  local box = sale and sale.Price
  if type(box) ~= "table" or type(box.GetAmount) ~= "function" then return nil end
  local ok, v = pcall(box.GetAmount, box)
  return ok and Num(v) or nil
end

-- Auctionator's own search events tell us when it has filled its price for
-- the item. Before that, its search result would overwrite ours.
local function AtrHasReadyEvents()
  local A = _G.Auctionator
  local E = type(A) == "table" and type(A.AH) == "table" and A.AH.Events
  return type(E) == "table" and type(E.CommoditySearchResultsReady) == "string"
end

-- The version of the item in Auctionator's Selling tab (from its link).
local function AtrSellVersion()
  local id = AH.atrSellItemID
  if not id then return nil end
  local loc = AH.atrSellLocation
  local link = AH.atrSellLink or LinkFromLocation(loc)
  local suffix
  if loc ~= nil then suffix = LocationHasSuffix(loc) else suffix = AH.atrSellSuffix == true end
  return VersionInfo(id, link, suffix)
end

-- Puts the buttons under Auctionator's buyout money box (or the bid box when
-- that one is shown), inside the Selling tab: side by side, Sell now first.
local function PlaceAtrPair(pair, both)
  local sale = AtrSaleFrame()
  if not sale then return end
  local price, bid = sale.Price, sale.BidPrice
  local under = type(price) == "table" and price.MoneyInput or nil
  if type(bid) == "table" and type(bid.IsShown) == "function" then
    local ok, shown = pcall(bid.IsShown, bid)
    if ok and shown and type(bid.MoneyInput) == "table" then under = bid.MoneyInput end
  end
  local function at(b)
    b:ClearAllPoints()
    if type(under) == "table" then
      b:SetPoint("TOPLEFT", under, "BOTTOMLEFT", 0, -4)
    elseif type(price) == "table" then
      b:SetPoint("TOPLEFT", price, "BOTTOMLEFT", 120, -24)
    else
      b:SetPoint("TOPRIGHT", sale, "TOPRIGHT", -10, -10)
    end
  end
  at(pair.now)
  if both then
    pair.best:ClearAllPoints()
    pair.best:SetPoint("LEFT", pair.now, "RIGHT", PAIR_GAP, 0)
  else
    at(pair.best)
  end
end

local function UpdateAtrUseButton()
  if not atrPair then return end
  local now, best, info
  local ready = AH.atrReady or not AtrHasReadyEvents()
  if AH.open and ready then
    now, best, info = SellAdvice(AH.atrSellItemID, IsCommodityLocation(AH.atrSellLocation), AtrSellVersion())
  end
  SetPairPrices(atrPair, now, best, info, "Post")
end

-- Fills Auctionator's buyout box with the Sell now ("now") or Suggested price ("best") price.
local function ApplyAuctionatorPrice(kind)
  local b = atrPair and atrPair[kind == "best" and "best" or "now"]
  if b and not b.price then UpdateAtrUseButton() end
  local price = b and b.price
  if not price then
    Print("no price advice for this item yet.")
    return false
  end
  local A = _G.Auctionator
  local done = false
  if type(A) == "table" and type(A.EventBus) == "table" and type(A.Selling) == "table"
    and type(A.Selling.Events) == "table" and A.Selling.Events.PriceSelected then
    done = pcall(function()
      A.EventBus:RegisterSource(atrSource, "ForeverAH")
      A.EventBus:Fire(atrSource, A.Selling.Events.PriceSelected, { buyout = price }, false)
    end)
  end
  -- Auctionator ignores PriceSelected while it has no item: check it took.
  local got = AtrPriceAmount()
  if done and got ~= nil and got ~= price then done = false end
  if not done then
    local sale = AtrSaleFrame()
    local box = sale and sale.Price
    if type(box) == "table" and type(box.SetAmount) == "function" then
      done = pcall(box.SetAmount, box, price)
      got = AtrPriceAmount()
      if done and got ~= nil and got ~= price then done = false end
    end
  end
  if not done then
    Print("could not fill Auctionator's price box. Type the price in yourself: " .. MoneyI(price))
  end
  return done
end
AH.ApplyAuctionatorPrice = ApplyAuctionatorPrice

-- Arrows on Auctionator's buyout and bid boxes, when they are made of the
-- game's own gold, silver and copper boxes. Written the same way as its
-- price is filled above (its SetAmount).
local function AddAtrArrows()
  local sale = AtrSaleFrame()
  if not sale then return end
  for _, key in ipairs({ "Price", "BidPrice" }) do
    local box = sale[key]
    if type(box) == "table" and type(box.GetAmount) == "function" and type(box.SetAmount) == "function" then
      AddPriceArrows(box.MoneyInput,
        function() return box:GetAmount() end,
        function(v) box:SetAmount(v) end)
    end
  end
end

local function CreateAtrUseButton()
  local okA, errA = pcall(AddAtrArrows)
  if not okA then AH.lastError = errA end
  if atrPair then return end
  local sale = AtrSaleFrame()
  if not sale then return end
  atrPair = NewPair(sale, nil, PlaceAtrPair, ApplyAuctionatorPrice)
  AH.atrPair = atrPair
  PlaceAtrPair(atrPair, true)
  RaiseAbove(atrPair.now, nil, sale)
  RaiseAbove(atrPair.best, nil, sale)
end

local function AtrNewSellItem(id)
  AH.atrSellItemID = id
  AH.atrReady = false
  CreateAtrUseButton()
  UpdateAtrUseButton()
end

local function OnAuctionatorEvent(eventName, a)
  local A = _G.Auctionator
  local B = A and A.Buying and A.Buying.Events or {}
  local S = A and A.Selling and A.Selling.Events or {}
  local R = A and A.AH and A.AH.Events or {}
  if eventName == B.ShowCommodityBuy or eventName == B.ShowItemBuy then
    local key = type(a) == "table" and a.itemKey
    local id = type(key) == "table" and Num(key.itemID) or nil
    NoteSuffix(id, KeyHasSuffix(key))
    if id then SetItemNow(id) end
  elseif eventName == S.BagItemClicked then
    if type(a) ~= "table" then return end
    local id = (type(a.itemKey) == "table" and Num(a.itemKey.itemID)) or IDFromLink(a.itemLink)
      or ItemIDFromLocation(a.location)
    AH.atrSellLocation = a.location
    local link = a.itemLink
    AH.atrSellLink = (link ~= nil and not IsSecret(link) and type(link) == "string") and link or nil
    AH.atrSellSuffix = (type(ns.LinkHasSuffix) == "function" and ns.LinkHasSuffix(a.itemLink)) or KeyHasSuffix(a.itemKey)
    NoteSuffix(id, AH.atrSellSuffix or LocationHasSuffix(a.location))
    AtrNewSellItem(id)
    local ver = AtrSellVersion()
    NoteSuffix(id, ver and ver.suffix)
    if id then SetItemNow((ver and ver.vid) or id, true) end
  elseif eventName == S.SellSearchStart then
    local id = type(a) == "table" and Num(a.itemID) or nil
    if id then
      if ItemIDFromLocation(AH.atrSellLocation) ~= id then
        AH.atrSellLocation, AH.atrSellSuffix, AH.atrSellLink = nil, nil, nil
      end
      AtrNewSellItem(id)
      local ver = AtrSellVersion()
      SetItemNow((ver and ver.vid) or id, true)
    end
  elseif eventName == R.CommoditySearchResultsReady or eventName == R.ItemSearchResultsReady then
    local id = (type(a) == "table" and Num(a.itemID)) or Num(a)
    if id and id == AH.atrSellItemID and not AH.atrReady then
      -- Auctionator fills its price from these results (items after a short
      -- item-info load), so give it a moment before offering ours.
      After(0.3, function()
        if AH.atrSellItemID == id then
          AH.atrReady = true
          UpdateAtrUseButton()
        end
      end)
    end
  end
end

function atrListener:ReceiveEvent(eventName, ...)
  local ok, err = pcall(OnAuctionatorEvent, eventName, ...)
  if not ok then AH.lastError = err end
end

local function HookAuctionator()
  if AH.hookedAtr then return end
  local A = _G.Auctionator
  if type(A) ~= "table" or type(A.EventBus) ~= "table" or type(A.EventBus.Register) ~= "function" then return end
  local events = {}
  local B = A.Buying and A.Buying.Events
  local S = A.Selling and A.Selling.Events
  if type(B) == "table" then
    events[#events + 1] = B.ShowCommodityBuy
    events[#events + 1] = B.ShowItemBuy
  end
  if type(S) == "table" then
    events[#events + 1] = S.BagItemClicked
    events[#events + 1] = S.SellSearchStart
  end
  local R = type(A.AH) == "table" and A.AH.Events
  if type(R) == "table" then
    events[#events + 1] = R.CommoditySearchResultsReady
    events[#events + 1] = R.ItemSearchResultsReady
  end
  local list = {}
  for _, e in pairs(events) do
    if type(e) == "string" then list[#list + 1] = e end
  end
  if #list == 0 then return end
  local ok = pcall(A.EventBus.Register, A.EventBus, atrListener, list)
  if ok then AH.hookedAtr = true end
end

---------------------------------------------------------------------------
-- C_AuctionHouse hooks: catch searches from any UI (Blizzard or Auctionator)
---------------------------------------------------------------------------
local function HookCAuctionHouse()
  if AH.hookedC then return end
  local CAH = _G.C_AuctionHouse
  if type(CAH) ~= "table" then return end
  AH.hookedC = true
  SafeHook(CAH, "SendBrowseQuery", function(query)
    if type(query) == "table" then OnSearch(query.searchString, query) end
  end)
  -- The Favorites search and an addon's search for a list of items: new
  -- searches too, nobody typed a name (what shows under "No items found" goes).
  SafeHook(CAH, "SearchForFavorites", function() NewBrowseSearch(nil) end)
  SafeHook(CAH, "SearchForItemKeys", function() NewBrowseSearch(nil) end)
  -- Sell searches come from many places (undercut scans too), so only follow
  -- them while Auctionator's Selling tab is on screen.
  SafeHook(CAH, "SendSellSearchQuery", function(itemKey)
    if not AH.open or not AtrSellingVisible() then return end
    local id = type(itemKey) == "table" and Num(itemKey.itemID) or nil
    if id then
      if ItemIDFromLocation(AH.atrSellLocation) ~= id then
        AH.atrSellLocation, AH.atrSellSuffix, AH.atrSellLink = nil, nil, nil
      end
      if id ~= AH.atrSellItemID then
        AtrNewSellItem(id)
      else
        CreateAtrUseButton()
        UpdateAtrUseButton()
      end
      local ver = AtrSellVersion()
      SetItemSoon((ver and ver.vid) or id)
    end
  end)
end

---------------------------------------------------------------------------
-- Refreshing buttons and the chart when live prices or the timeframe change
---------------------------------------------------------------------------
local UpdateLegacyUseButton -- forward (legacy AH section)

RefreshAll = function()
  for sf in pairs(useButtons) do UpdateUseButton(sf) end
  UpdateAtrUseButton()
  if UpdateLegacyUseButton then UpdateLegacyUseButton() end
  if chartDirty then
    chartDirty = false
    if type(ns.ChartRefresh) == "function" then ns.ChartRefresh() end
  end
end

-- Chart.lua calls this when the timeframe changes, so the buttons always
-- show (and fill) the same advice as the chart.
function ns.AH_RefreshButtons()
  if not AH.open then return end
  for sf in pairs(useButtons) do UpdateUseButton(sf) end
  UpdateAtrUseButton()
  if UpdateLegacyUseButton then UpdateLegacyUseButton() end
end

local function OnCommodityResults(itemID)
  if not AH.open then return end
  local id = Num(itemID)
  if not id then return end
  StoreLive(id, LiveCommodity(id))
end

local function OnItemResults(itemKey)
  if not AH.open or type(itemKey) ~= "table" then return end
  local id = Num(itemKey.itemID)
  if not id then return end
  local suffixKey = KeyHasSuffix(itemKey)
  NoteSuffix(id, suffixKey)
  local best, own, byKey, mixed, complete = LiveItem(itemKey)
  -- A search for one suffix of the item only lists that version, so it says nothing about the others.
  AH.liveLoaded[id] = (byKey ~= nil and complete and not suffixKey) or nil
  StoreLiveVersions(id, byKey)
  NoteSuffix(id, mixed)
  -- The cheapest of a mix of versions says nothing about any one of them.
  local versions = type(ns.HasVariants) == "function" and ns.HasVariants(id)
  if mixed or suffixKey or versions then
    StoreLive(id, nil)
  else
    StoreLive(id, best, own)
  end
end

---------------------------------------------------------------------------
-- Modern auction house (AuctionHouseFrame, Blizzard_AuctionHouseUI)
---------------------------------------------------------------------------
local OnOpen -- forward

-- The version a browse row stands for, when the auction house lists versions
-- as rows of their own (the row's item key has a suffix) and the row's name
-- is the name of a version the data has. nil otherwise.
local function RowVersion(key)
  if not KeyHasSuffix(key) or type(ns.VersionByName) ~= "function" then return nil end
  local CAH = _G.C_AuctionHouse
  if type(CAH) ~= "table" or type(CAH.GetItemKeyInfo) ~= "function" then return nil end
  local ok, info = pcall(CAH.GetItemKeyInfo, key)
  local name = ok and type(info) == "table" and info.itemName or nil
  if type(name) ~= "string" or IsSecret(name) or name == "" then return nil end
  return ns.VersionByName(key.itemID, name)
end
AH.RowVersion = RowVersion

-- A browse row was opened (clicked, or opened by the sidebar).
local function OnRowKey(key)
  NoteSuffix(key.itemID, KeyHasSuffix(key))
  local vid = RowVersion(key)
  if vid then
    SetItemNow(vid, true)
    return
  end
  -- A row of one version that is not the one the sidebar searched for (or
  -- that the data cannot name): show the item, never another version's chart.
  if KeyHasSuffix(key) then AH.expectVid = nil end
  SetItemNow(key.itemID)
end

local function HookModern()
  if AH.hookedModern then return true end
  local f = _G.AuctionHouseFrame
  if type(f) ~= "table" then return false end
  AH.hookedModern = true

  SafeHook(f, "SendBrowseQuery", function(_, searchString) OnSearch(searchString) end)
  SafeHook(f, "SelectBrowseResult", function(_, r)
    local key = type(r) == "table" and r.itemKey
    if type(key) == "table" then OnRowKey(key) end
  end)
  if type(f.CommoditiesBuyFrame) == "table" then
    SafeHook(f.CommoditiesBuyFrame, "SetItemIDAndPrice", function(_, itemID) SetItemNow(itemID) end)
  end
  if type(f.ItemBuyFrame) == "table" then
    SafeHook(f.ItemBuyFrame, "SetItemKey", function(_, key)
      if type(key) == "table" then OnRowKey(key) end
    end)
  end
  local sellFrames = { CommoditiesSellFrame = true, ItemSellFrame = false }
  for key, isCommodity in pairs(sellFrames) do
    local sf = f[key]
    if type(sf) == "table" then
      CreateUseButton(sf, isCommodity)
      local okA, errA = pcall(AddSellArrows, sf)
      if not okA then AH.lastError = errA end
      SafeHook(sf, "SetItem", function(self, loc) OnSellItem(self, loc) end)
    end
  end
  if type(f.HookScript) == "function" then
    pcall(f.HookScript, f, "OnShow", function()
      if AH.open then
        After(0, function()
          if type(ns.ChartRefreshPlacement) == "function" then ns.ChartRefreshPlacement() end
        end)
      end
    end)
  end
  CreateToggleButton(f, "modern")
  return true
end

---------------------------------------------------------------------------
-- Legacy auction house (AuctionFrame, Blizzard_AuctionUI). Not expected on
-- Forever, kept so the addon still works if the old UI is ever used.
---------------------------------------------------------------------------
local legacyPair   -- { now = button, best = button } above Create Auction

local function LegacyItemIDAt(listType, index)
  if type(_G.GetAuctionItemLink) == "function" then
    local ok, link = pcall(_G.GetAuctionItemLink, listType, index)
    if ok then
      local id = IDFromLink(link)
      if id then return id end
    end
  end
  if type(_G.GetAuctionItemInfo) == "function" then
    local r = { pcall(_G.GetAuctionItemInfo, listType, index) }
    if r[1] then
      if Num(r[18]) then return r[18] end -- 17th return value is the itemID
      if type(r[2]) == "string" and type(ns.FindItemID) == "function" then return ns.FindItemID(r[2]) end
    end
  end
  return nil
end

UpdateLegacyUseButton = function()
  if not legacyPair then return end
  local now, best, info
  if AH.open then
    -- The old frame gives no item link: gear whose versions differ cannot be
    -- told apart, so it gets no buttons (never the mixed all-versions price).
    local id = AH.legacySellID
    local versions = id and ((type(ns.HasVariants) == "function" and ns.HasVariants(id)) or AH.suffix[id])
    now, best, info = SellAdvice(id, true, id and { id = id, key = "", mode = versions and "unknown" or "plain" } or nil)
  end
  SetPairPrices(legacyPair, now, best, info, "Create Auction")
end

-- Fills the buyout and starting bid (per unit or per stack, as the old frame
-- is set) with the Sell now ("now") or Suggested price ("best") price.
local function ApplyLegacyPrice(kind)
  local b = legacyPair and legacyPair[kind == "best" and "best" or "now"]
  if b and not b.price then UpdateLegacyUseButton() end
  local unit = b and b.price
  if not unit then
    Print("no price advice for this item yet.")
    return false
  end
  local stack = 1
  local entry = _G.AuctionsStackSizeEntry
  if type(entry) == "table" and type(entry.GetNumber) == "function" then
    local ok, v = pcall(entry.GetNumber, entry)
    if ok and Num(v) and v > 0 then stack = v end
  elseif Num(AH.legacySellCount) and AH.legacySellCount > 0 then
    stack = AH.legacySellCount
  end
  local auctions = _G.AuctionFrameAuctions
  local unitMode = type(auctions) == "table" and auctions.priceType ~= nil
    and auctions.priceType == (_G.PRICE_TYPE_UNIT or 1)
  local amount = unitMode and unit or unit * stack
  local setCopper = _G.MoneyInputFrame_SetCopper
  if type(setCopper) ~= "function" or type(_G.BuyoutPrice) ~= "table" then
    Print("could not find the price box. Type the price in yourself: " .. MoneyI(amount))
    return false
  end
  pcall(setCopper, _G.BuyoutPrice, amount)
  if type(_G.StartPrice) == "table" then pcall(setCopper, _G.StartPrice, amount) end
  return true
end
AH.ApplyLegacyPrice = ApplyLegacyPrice

local function HookLegacy()
  if AH.hookedLegacy then return true end
  local f = _G.AuctionFrame
  if type(f) ~= "table" then return false end
  AH.hookedLegacy = true

  SafeHookGlobal("QueryAuctionItems", function(name)
    if AH.kind == "legacy" then OnSearch(name) end
  end)
  SafeHookGlobal("SetSelectedAuctionItem", function(listType, index)
    if AH.kind == "legacy" and listType == "list" then
      local id = LegacyItemIDAt("list", index)
      if id then SetItemNow(id) end
    end
  end)
  local parent = _G.AuctionFrameAuctions
  if type(parent) == "table" and not legacyPair then
    -- Side by side just above Create Auction (or under the buyout box).
    local function place(pair, both)
      local first = (both or pair.now:IsShown()) and pair.now or pair.best
      pair.now:ClearAllPoints()
      pair.best:ClearAllPoints()
      local anchor = _G.AuctionsCreateAuctionButton
      if type(anchor) == "table" then
        first:SetPoint("BOTTOMLEFT", anchor, "TOPLEFT", 0, 4)
      elseif type(_G.BuyoutPrice) == "table" then
        first:SetPoint("TOPLEFT", _G.BuyoutPrice, "BOTTOMLEFT", 0, -4)
      else
        first:SetPoint("BOTTOMLEFT", parent, "BOTTOMLEFT", 20, 40)
      end
      if both then pair.best:SetPoint("LEFT", pair.now, "RIGHT", PAIR_GAP, 0) end
    end
    legacyPair = NewPair(parent, true, place, ApplyLegacyPrice)
    AH.legacyPair = legacyPair
    place(legacyPair, true)
    RaiseAbove(legacyPair.now, nil, parent)
    RaiseAbove(legacyPair.best, nil, parent)
    AttachTooltip(legacyPair.now, "Sell now",
      "Fills the buyout and starting bid with 1 copper under the cheapest listing. Nothing is posted: you still click Create Auction.")
    AttachTooltip(legacyPair.best, "Suggested price",
      "Fills the buyout and starting bid with what this item usually sells for. Nothing is posted: you still click Create Auction.")
  end
  -- Arrows on the starting bid and buyout boxes, written like the buttons do.
  for _, name in ipairs({ "StartPrice", "BuyoutPrice" }) do
    local mf = _G[name]
    if type(mf) == "table" and type(_G.MoneyInputFrame_GetCopper) == "function"
      and type(_G.MoneyInputFrame_SetCopper) == "function" then
      local okA, errA = pcall(AddPriceArrows, mf,
        function() return _G.MoneyInputFrame_GetCopper(mf) end,
        function(v) _G.MoneyInputFrame_SetCopper(mf, v) end)
      if not okA then AH.lastError = errA end
    end
  end
  if type(f.HookScript) == "function" then
    pcall(f.HookScript, f, "OnShow", function()
      if AH.open then
        After(0, function()
          if type(ns.ChartRefreshPlacement) == "function" then ns.ChartRefreshPlacement() end
        end)
      end
    end)
  end
  CreateToggleButton(f, "legacy")
  return true
end

local function OnLegacyResults()
  if not AH.open or AH.kind ~= "legacy" or not AH.pendingSearch then return end
  if type(_G.GetNumAuctionItems) ~= "function" then return end
  local ok, n = pcall(_G.GetNumAuctionItems, "list")
  if not ok or not Num(n) then return end
  local ids = {}
  for i = 1, math.min(n, 50) do
    local id = LegacyItemIDAt("list", i)
    if id then ids[#ids + 1] = id end
  end
  local pick = PickFromIDs(ids)
  if pick then
    AH.pendingSearch, AH.pendingIDs = nil, nil
    SetItemSoon(pick)
  end
end

local function OnLegacySellItem()
  if not AH.open or type(_G.GetAuctionSellItemInfo) ~= "function" then return end
  local r = { pcall(_G.GetAuctionSellItemInfo) }
  if not r[1] then return end
  local name, count, itemID = r[2], r[4], r[11] -- name, texture, count, ..., itemID (10th)
  local id = Num(itemID)
  if not id and type(name) == "string" and type(ns.FindItemID) == "function" then id = ns.FindItemID(name) end
  AH.legacySellID = id
  AH.legacySellCount = Num(count)
  UpdateLegacyUseButton()
  if id then SetItemNow(id) end
end

---------------------------------------------------------------------------
-- Open and close
---------------------------------------------------------------------------
local function PickFrame()
  local m, l = _G.AuctionHouseFrame, _G.AuctionFrame
  local function shown(f)
    if type(f) ~= "table" or type(f.IsShown) ~= "function" then return false end
    local ok, v = pcall(f.IsShown, f)
    return ok and v == true
  end
  if type(m) == "table" and (shown(m) or not shown(l)) then return m, "modern" end
  if type(l) == "table" then return l, "legacy" end
  return nil, nil
end

function ns.AH_IsOpen()
  return AH.open
end

-- The frame the chart may dock to, or nil (closed, hidden, or shrunk by TSM).
function ns.AH_GetDockFrame()
  if not AH.open then return nil end
  local f = AH.frame
  if type(f) ~= "table" or type(f.IsShown) ~= "function" then return nil end
  local ok, shown = pcall(f.IsShown, f)
  if not ok or not shown then return nil end
  local sc = type(f.GetScale) == "function" and Num(f:GetScale()) or nil
  if not sc or sc < 0.5 then return nil end
  return f
end

-- Which auction house is open. The data comes from one faction's house; the
-- neutral goblin houses (Booty Bay, Gadgetzan, Everlook) and the other
-- faction's are separate markets with their own prices and a bigger cut.
local NEUTRAL_TOWNS = { ["Booty Bay"] = true, ["Gadgetzan"] = true, ["Everlook"] = true }

local function InNeutralTown()
  for _, fn in ipairs({ "GetSubZoneText", "GetMinimapZoneText", "GetZoneText" }) do
    local f = _G[fn]
    if type(f) == "function" then
      local ok, z = pcall(f)
      if ok and type(z) == "string" and not IsSecret(z) and NEUTRAL_TOWNS[z] then return true end
    end
  end
  return false
end

local function ReadHouse()
  AH.otherHouse, AH.npcFaction = false, nil
  local ok, other, faction = pcall(function()
    local UE, UFG = _G.UnitExists, _G.UnitFactionGroup
    if type(UE) ~= "function" or type(UFG) ~= "function" then return false end
    local exists = UE("npc")
    if IsSecret(exists) or exists ~= true then return false end
    local f = UFG("npc")
    if IsSecret(f) then return false end
    local house = type(ns.HouseFaction) == "function" and ns.HouseFaction() or "Alliance"
    if not house then return false end
    if type(f) == "string" and f ~= "" then return f ~= house, f end
    -- No faction: a neutral auctioneer, but only trust that in a goblin town.
    return InNeutralTown(), "Neutral"
  end)
  if ok then AH.otherHouse, AH.npcFaction = other == true, faction end
end
AH.ReadHouse = ReadHouse

local function AfterOpen()
  if not AH.open then return end
  if not AH.otherHouse then ReadHouse() end   -- the auctioneer may only be readable now
  HookModern()
  HookLegacy()
  HookAuctionator()
  AH.frame, AH.kind = PickFrame()
  CreateAtrUseButton()
  if ChartWanted() and type(ns.ShowChart) == "function" then
    ns.ShowChart(AH.itemID or (ns.settings and ns.settings.lastItem), "ah")
  end
  for sf in pairs(useButtons) do UpdateUseButton(sf) end
  -- A second later, list the names a search that finds nothing looks through
  -- (a few thousand a frame), so that search does not stall the game.
  After(1, function()
    if AH.open and type(ns.PrepareSearchList) == "function" then pcall(ns.PrepareSearchList) end
  end)
end

OnOpen = function()
  if AH.open then return end
  AH.open = true
  ReadHouse()
  HookCAuctionHouse()
  HookModern()
  HookLegacy()
  HookAuctionator()
  AH.frame, AH.kind = PickFrame()
  -- Let Blizzard (and Auctionator) finish showing their frames first.
  After(0, AfterOpen)
end

local function OnClose()
  if not AH.open then return end
  AH.open = false
  AH.pendingSearch, AH.pendingIDs, AH.expectID, AH.expectVid = nil, nil, nil, nil
  pendingToken = pendingToken + 1
  AH.atrSellItemID, AH.atrSellLocation, AH.atrSellSuffix, AH.atrSellLink, AH.atrReady = nil, nil, nil, nil, false
  AH.legacySellID, AH.legacySellCount = nil, nil
  AH.otherHouse, AH.npcFaction = false, nil
  for id in pairs(AH.live) do AH.live[id] = nil end
  for id in pairs(AH.liveV) do AH.liveV[id] = nil end
  for id in pairs(AH.liveLoaded) do AH.liveLoaded[id] = nil end
  for id in pairs(AH.browse) do AH.browse[id] = nil end
  AH.lastSearch, AH.queryFiltered, AH.queryExact = nil, false, false
  ClearEmptySearch()
  chartDirty = false
  for sf in pairs(sellIDs) do sellIDs[sf] = nil end
  for sf in pairs(sellVer) do sellVer[sf] = nil end
  for _, pair in pairs(useButtons) do HidePair(pair) end
  HidePair(atrPair)
  HidePair(legacyPair)
  local docked = type(ns.ChartIsDocked) == "function" and ns.ChartIsDocked()
  local fromAH = type(ns.ChartSource) == "function" and ns.ChartSource() == "ah"
  if (docked or fromAH) and type(ns.HideChart) == "function" then
    ns.HideChart()
  elseif type(ns.ChartRefreshPlacement) == "function" then
    ns.ChartRefreshPlacement()
  end
end
AH.OnOpen, AH.OnClose = OnOpen, OnClose

local function IsAuctioneer(t)
  local E = _G.Enum
  local PIT = type(E) == "table" and E.PlayerInteractionType
  return type(PIT) == "table" and PIT.Auctioneer ~= nil and t == PIT.Auctioneer
end

---------------------------------------------------------------------------
-- Events
---------------------------------------------------------------------------
local handlers = {
  ADDON_LOADED = function(name)
    if name == "Blizzard_AuctionHouseUI" then
      HookModern()
    elseif name == "Blizzard_AuctionUI" then
      HookLegacy()
    elseif name == "Auctionator" then
      HookAuctionator()
    end
  end,
  PLAYER_LOGIN = function()
    HookCAuctionHouse()
    HookModern()
    HookLegacy()
    HookAuctionator()
  end,
  AUCTION_HOUSE_SHOW = function() OnOpen() end,
  AUCTION_HOUSE_CLOSED = function() OnClose() end,
  PLAYER_INTERACTION_MANAGER_FRAME_SHOW = function(t) if IsAuctioneer(t) then OnOpen() end end,
  PLAYER_INTERACTION_MANAGER_FRAME_HIDE = function(t) if IsAuctioneer(t) then OnClose() end end,
  AUCTION_HOUSE_BROWSE_RESULTS_UPDATED = function() OnBrowseResults() end,
  COMMODITY_SEARCH_RESULTS_UPDATED = function(itemID) OnCommodityResults(itemID) end,
  COMMODITY_SEARCH_RESULTS_ADDED = function(itemID) OnCommodityResults(itemID) end,
  ITEM_SEARCH_RESULTS_UPDATED = function(itemKey) OnItemResults(itemKey) end,
  ITEM_SEARCH_RESULTS_ADDED = function(itemKey) OnItemResults(itemKey) end,
  AUCTION_HOUSE_BROWSE_RESULTS_ADDED = function(added) OnBrowseResults(type(added) == "table" and added or nil) end,
  AUCTION_ITEM_LIST_UPDATE = function() OnLegacyResults() end,
  NEW_AUCTION_UPDATE = function() OnLegacySellItem() end,
}

local ev = CreateFrame("Frame")
AH.eventFrame = ev
for name in pairs(handlers) do
  -- Unknown events raise an error on the modern client, so register each one safely.
  pcall(ev.RegisterEvent, ev, name)
end
ev:SetScript("OnEvent", function(_, event, ...)
  local h = handlers[event]
  if not h then return end
  local ok, err = pcall(h, ...)
  if not ok then AH.lastError = err end
end)

-- In case another addon loaded the auction house UI before us.
HookCAuctionHouse()
HookModern()
HookLegacy()

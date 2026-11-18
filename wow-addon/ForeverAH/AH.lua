local ADDON_NAME, ns = ...

-- Forever AH: auction house integration.
-- Shows the price chart under the auction house and switches it to whatever
-- item you search for, click on, or put in the sell slot. Adds a "Use List At"
-- button to the sell frame that fills in the recommended price.
--
-- Rules this file sticks to:
--   * hooksecurefunc, HookScript and events only. No Blizzard function is replaced.
--   * Never sends auction house queries, never posts, bids, buys or cancels.
--     The price button only fills the price box: posting stays your click.
--   * The live listings the auction house already loaded (for a sell slot or a
--     clicked result) are read, never requested, and they beat the hourly data.
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
  live = {},             -- itemID -> { price = copper, own = true when that listing is yours }
  suffix = {},           -- itemID -> true once seen with a random suffix ("of the Monkey")
}
ns.AHState = AH

local sellIDs = setmetatable({}, { __mode = "k" })    -- Blizzard sell frame -> itemID
local sellSuffix = setmetatable({}, { __mode = "k" }) -- Blizzard sell frame -> true for a random-suffix item
local useButtons = setmetatable({}, { __mode = "k" }) -- Blizzard sell frame -> our button
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
  if type(cf) == "table" and cf.AddMessage then cf:AddMessage("Forever AH: " .. tostring(msg)) end
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
  local best, own
  for i = 1, min(n, 50) do
    local ok2, r = pcall(CAH.GetItemSearchResultInfo, itemKey, i)
    if ok2 and type(r) == "table" then
      local p = Num(r.buyoutAmount)
      local q = Num(r.quantity) or 1
      if p and p > 0 then
        if q > 1 then p = floor(p / q) end
        if not best or p < best then best, own = p, r.containsOwnerItem == true end
      end
    end
  end
  return best, own
end

local refreshPending, chartDirty = false, false
local RefreshAll -- forward

local function StoreLive(id, price, own)
  id = Num(id)
  if not id then return end
  local old = AH.live[id]
  if price then AH.live[id] = { price = price, own = own == true } else AH.live[id] = nil end
  if not old and not price then return end
  if old and price and old.price == price and old.own == (own == true) then return end
  if type(ns.ChartItem) == "function" and ns.ChartItem() == id then chartDirty = true end
  if not refreshPending then
    refreshPending = true
    After(0.1, function()
      refreshPending = false
      RefreshAll()
    end)
  end
end
AH.StoreLive = StoreLive

-- The cheapest listing the open auction house is showing for an item, and
-- whether that listing is yours. nil when it has not loaded (or the AH is closed).
function ns.LiveCheapest(itemID)
  if not AH.open then return nil end
  local r = AH.live[tonumber(itemID) or -1]
  if r then return r.price, r.own end
  return nil
end

---------------------------------------------------------------------------
-- Price advice for the sell buttons
---------------------------------------------------------------------------
-- Returns the price to fill (already rounded for the frame) and what it is
-- based on, or nil plus a reason ("market", "suffix", "stale", "history",
-- "unreliable", "none").
local function SellAdvice(id, isCommodity, hasSuffix)
  if not id or type(ns.Recommend) ~= "function" then return nil, "none" end
  if type(ns.MarketMismatch) == "function" and ns.MarketMismatch() then return nil, "market" end
  if hasSuffix then return nil, "suffix" end
  local lp, own = ns.LiveCheapest(id)
  local age = type(ns.DataAge) == "function" and ns.DataAge() or nil
  if not lp and age and age > (ns.STALE_SECONDS or 21600) then return nil, "stale" end
  local ok, rec = pcall(ns.Recommend, id, TF(), lp)
  if not ok or type(rec) ~= "table" then return nil, "none" end
  local v = Num(rec.listAt)
  local follow = false
  if rec.advice == false then
    -- No List At price yet. A few units listed far above what a vendor
    -- pays: the cheapest listing is that same silly price, so no button.
    if rec.vendorFar then return nil, "unreliable" end
    -- Otherwise only with a listing that is up right now: 1 copper under it.
    if not lp or lp <= 1 then return nil, "history" end
    v, follow = lp - 1, "cheapest"
    -- With only the history short, today's market price is fine: never post
    -- far under it (the same List At rule, from today's prices).
    local floorAt = Num(rec.followFloor)
    if not rec.thinListings and floorAt and floorAt > v then v, follow = floorAt, "market" end
  end
  if not v or v <= 0 then return nil, "none" end
  -- The cheapest listing is your own: match it instead of undercutting yourself.
  local matched = false
  if lp and own and v == lp - 1 then v, matched = lp, true end
  local price = PriceForFrame(v, isCommodity)
  if not price then return nil, "none" end
  return price, { live = lp, matched = matched, age = age, rec = rec, follow = follow }
end
AH.SellAdvice = SellAdvice

local function AdviceText(info, postWord)
  local lines = {}
  if info and info.live then
    lines[#lines + 1] = "Based on the cheapest listing right now: " .. MoneyI(info.live) .. "."
    if info.matched then lines[#lines + 1] = "That listing is yours, so it is matched, not undercut." end
  else
    local hours = info and info.age and floor(info.age / 3600) or nil
    lines[#lines + 1] = (hours and hours >= 1) and ("From price data " .. hours .. "h old: the live listings have not loaded yet.")
      or "From the latest price data: the live listings have not loaded yet."
  end
  local rec = info and info.rec
  if info and info.follow == "market" then
    lines[#lines + 1] = "There is not enough price history yet, so this uses the market price seen so far, not 1 copper under the cheapest listing."
  elseif info and info.follow then
    if type(rec) == "table" and rec.history == false then
      lines[#lines + 1] = "There is not enough price history for a List At price yet, so this only follows the cheapest listing."
    else
      lines[#lines + 1] = "This item's price may not be reliable (see the chart notes), so this only follows the cheapest listing."
    end
  elseif type(rec) == "table" and rec.far then
    lines[#lines + 1] = "The cheapest listing is far above recent prices, so this uses the recent sell price."
  end
  if type(rec) == "table" and rec.vendorBetter then lines[#lines + 1] = "A vendor pays more than this after the 5% cut." end
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
-- Switching the chart item
---------------------------------------------------------------------------
local pendingToken = 0

local function ApplyItem(id)
  id = Num(tonumber(id))
  if not id or id <= 0 then return end
  AH.itemID = id
  if not AH.open or not ChartWanted() then return end
  if type(ns.ShowChart) == "function" then ns.ShowChart(id, "ah") end
end

-- An explicit choice (clicked a result, put an item in the sell slot).
local function SetItemNow(id)
  pendingToken = pendingToken + 1
  AH.pendingSearch, AH.pendingIDs, AH.expectID = nil, nil, nil
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
function ns.AH_ExpectItem(id)
  expectToken = expectToken + 1
  AH.expectID = Num(tonumber(id))
  AH.expectAt = NowSeconds()
end
AH.ExpectItem = ns.AH_ExpectItem

-- The expected item when this search found it (and it is recent), else nil.
local function ExpectedIn(ids)
  local want = AH.expectID
  if not want then return nil end
  if NowSeconds() - (AH.expectAt or 0) > EXPECT_SECONDS then
    AH.expectID = nil
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
  return nil
end

-- A softer signal (search text, browse results). Waits a moment so a burst of
-- searches from a shopping list only redraws the chart once.
local function SetItemSoon(id)
  pendingToken = pendingToken + 1
  local tok = pendingToken
  After(0.15, function()
    if tok == pendingToken then ApplyItem(id) end
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

local function OnSearch(text)
  if not AH.open then return end
  local s = CleanSearch(text)
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

local function OnBrowseResults()
  if not AH.open or not AH.pendingSearch then return end
  local CAH = _G.C_AuctionHouse
  if type(CAH) ~= "table" or type(CAH.GetBrowseResults) ~= "function" then return end
  local ok, list = pcall(CAH.GetBrowseResults)
  if not ok or type(list) ~= "table" then return end
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
  AttachTooltip(b, "Forever AH price chart", "Show or hide the price chart under the auction house.")
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
function ns.ItemNotes(itemID)
  if AH.suffix[tonumber(itemID) or -1] then return { "Price covers all versions of this item" } end
  return nil
end

local function UpdateUseButton(sf)
  local b = useButtons[sf]
  if not b then return end
  local price, info
  if AH.open then price, info = SellAdvice(sellIDs[sf], b.isCommodity, sellSuffix[sf]) end
  b.price = price
  if price then
    b:SetText("Use List At: " .. MoneyI(price))
    b.tipBody = AdviceText(info, "Post")
    b:Show()
  else
    b:Hide()
  end
end

local function ApplyListAt(sf)
  local b = useButtons[sf]
  if not sellIDs[sf] and type(sf.GetItem) == "function" then
    local ok, loc = pcall(sf.GetItem, sf)
    if ok and loc ~= nil then
      sellIDs[sf] = ItemIDFromLocation(loc)
      sellSuffix[sf] = LocationHasSuffix(loc)
    end
  end
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
AH.ApplyListAt = ApplyListAt

local function CreateUseButton(sf, isCommodity)
  if type(sf) ~= "table" or useButtons[sf] then return end
  local b = NewButton(sf, 194, 22)
  b.isCommodity = isCommodity
  if type(sf.PostButton) == "table" then
    b:SetPoint("TOP", sf.PostButton, "BOTTOM", 0, -6)
  else
    b:SetPoint("BOTTOMRIGHT", sf, "BOTTOMRIGHT", -12, 12)
  end
  RaiseAbove(b, sf.PostButton, sf)
  b:SetScript("OnClick", function() ApplyListAt(sf) end)
  AttachTooltip(b, "Use the List At price",
    "Fills the price box with Forever AH's recommended List At price. Nothing is posted: you still click Post.")
  b:Hide()
  useButtons[sf] = b
end

local function OnSellItem(sf, loc)
  local id = loc ~= nil and ItemIDFromLocation(loc) or nil
  sellIDs[sf] = id
  sellSuffix[sf] = id ~= nil and LocationHasSuffix(loc) or nil
  NoteSuffix(id, sellSuffix[sf])
  UpdateUseButton(sf)
  if id then SetItemNow(id) end
end

---------------------------------------------------------------------------
-- Auctionator (optional): follow its Shopping and Selling tabs
---------------------------------------------------------------------------
local atrListener = {}
local atrSource = {}
local atrUseButton

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

local function AtrSellItemSuffix()
  local loc = AH.atrSellLocation
  if loc ~= nil then return LocationHasSuffix(loc) end
  return AH.atrSellSuffix == true
end

-- Puts the button under Auctionator's buyout money box (or the bid box when
-- that one is shown), inside the Selling tab.
local function PlaceAtrUseButton(b)
  local sale = AtrSaleFrame()
  if not sale then return end
  local price, bid = sale.Price, sale.BidPrice
  local under = type(price) == "table" and price.MoneyInput or nil
  if type(bid) == "table" and type(bid.IsShown) == "function" then
    local ok, shown = pcall(bid.IsShown, bid)
    if ok and shown and type(bid.MoneyInput) == "table" then under = bid.MoneyInput end
  end
  b:ClearAllPoints()
  if type(under) == "table" then
    b:SetPoint("TOPLEFT", under, "BOTTOMLEFT", 0, -4)
  elseif type(price) == "table" then
    b:SetPoint("TOPLEFT", price, "BOTTOMLEFT", 120, -24)
  else
    b:SetPoint("TOPRIGHT", sale, "TOPRIGHT", -10, -10)
  end
end

local function UpdateAtrUseButton()
  if not atrUseButton then return end
  local price, info
  local ready = AH.atrReady or not AtrHasReadyEvents()
  if AH.open and ready then
    price, info = SellAdvice(AH.atrSellItemID, IsCommodityLocation(AH.atrSellLocation), AtrSellItemSuffix())
  end
  atrUseButton.price = price
  if price then
    PlaceAtrUseButton(atrUseButton)
    atrUseButton:SetText("Use List At: " .. MoneyI(price))
    atrUseButton.tipBody = AdviceText(info, "Post")
    atrUseButton:Show()
  else
    atrUseButton:Hide()
  end
end

local function ApplyAuctionatorListAt()
  if atrUseButton and not atrUseButton.price then UpdateAtrUseButton() end
  local price = atrUseButton and atrUseButton.price
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
AH.ApplyAuctionatorListAt = ApplyAuctionatorListAt

local function CreateAtrUseButton()
  if atrUseButton then return end
  local sale = AtrSaleFrame()
  if not sale then return end
  local b = NewButton(sale, 150, 20)
  atrUseButton = b
  PlaceAtrUseButton(b)
  RaiseAbove(b, nil, sale)
  b:SetScript("OnClick", ApplyAuctionatorListAt)
  AttachTooltip(b, "Use the List At price",
    "Fills Auctionator's price box with Forever AH's recommended List At price. Nothing is posted: you still click Post.")
  b:Hide()
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
    AH.atrSellSuffix = (type(ns.LinkHasSuffix) == "function" and ns.LinkHasSuffix(a.itemLink)) or KeyHasSuffix(a.itemKey)
    NoteSuffix(id, AH.atrSellSuffix or LocationHasSuffix(a.location))
    AtrNewSellItem(id)
    if id then SetItemNow(id) end
  elseif eventName == S.SellSearchStart then
    local id = type(a) == "table" and Num(a.itemID) or nil
    if id then
      if ItemIDFromLocation(AH.atrSellLocation) ~= id then AH.atrSellLocation, AH.atrSellSuffix = nil, nil end
      AtrNewSellItem(id)
      SetItemNow(id)
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
    if type(query) == "table" then OnSearch(query.searchString) end
  end)
  -- Sell searches come from many places (undercut scans too), so only follow
  -- them while Auctionator's Selling tab is on screen.
  SafeHook(CAH, "SendSellSearchQuery", function(itemKey)
    if not AH.open or not AtrSellingVisible() then return end
    local id = type(itemKey) == "table" and Num(itemKey.itemID) or nil
    if id then
      if ItemIDFromLocation(AH.atrSellLocation) ~= id then AH.atrSellLocation, AH.atrSellSuffix = nil, nil end
      if id ~= AH.atrSellItemID then
        AtrNewSellItem(id)
      else
        CreateAtrUseButton()
        UpdateAtrUseButton()
      end
      SetItemSoon(id)
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
  NoteSuffix(id, KeyHasSuffix(itemKey))
  StoreLive(id, LiveItem(itemKey))
end

---------------------------------------------------------------------------
-- Modern auction house (AuctionHouseFrame, Blizzard_AuctionHouseUI)
---------------------------------------------------------------------------
local OnOpen -- forward

local function HookModern()
  if AH.hookedModern then return true end
  local f = _G.AuctionHouseFrame
  if type(f) ~= "table" then return false end
  AH.hookedModern = true

  SafeHook(f, "SendBrowseQuery", function(_, searchString) OnSearch(searchString) end)
  SafeHook(f, "SelectBrowseResult", function(_, r)
    local key = type(r) == "table" and r.itemKey
    if type(key) == "table" then
      NoteSuffix(key.itemID, KeyHasSuffix(key))
      SetItemNow(key.itemID)
    end
  end)
  if type(f.CommoditiesBuyFrame) == "table" then
    SafeHook(f.CommoditiesBuyFrame, "SetItemIDAndPrice", function(_, itemID) SetItemNow(itemID) end)
  end
  if type(f.ItemBuyFrame) == "table" then
    SafeHook(f.ItemBuyFrame, "SetItemKey", function(_, key)
      if type(key) == "table" then
        NoteSuffix(key.itemID, KeyHasSuffix(key))
        SetItemNow(key.itemID)
      end
    end)
  end
  local sellFrames = { CommoditiesSellFrame = true, ItemSellFrame = false }
  for key, isCommodity in pairs(sellFrames) do
    local sf = f[key]
    if type(sf) == "table" then
      CreateUseButton(sf, isCommodity)
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
local legacyUseButton

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
  if not legacyUseButton then return end
  local price, info
  if AH.open then price, info = SellAdvice(AH.legacySellID, true, false) end
  legacyUseButton.price = price
  if price then
    legacyUseButton:SetText("Use List At: " .. MoneyI(price))
    legacyUseButton.tipBody = AdviceText(info, "Create Auction")
    legacyUseButton:Show()
  else
    legacyUseButton:Hide()
  end
end

local function ApplyLegacyListAt()
  if legacyUseButton and not legacyUseButton.price then UpdateLegacyUseButton() end
  local unit = legacyUseButton and legacyUseButton.price
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
AH.ApplyLegacyListAt = ApplyLegacyListAt

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
  if type(parent) == "table" and not legacyUseButton then
    local b = NewButton(parent, 160, 22)
    local anchor = _G.AuctionsCreateAuctionButton
    if type(anchor) == "table" then
      b:SetPoint("BOTTOMLEFT", anchor, "TOPLEFT", 0, 4)
    elseif type(_G.BuyoutPrice) == "table" then
      b:SetPoint("TOPLEFT", _G.BuyoutPrice, "BOTTOMLEFT", 0, -4)
    else
      b:SetPoint("BOTTOMLEFT", parent, "BOTTOMLEFT", 20, 40)
    end
    RaiseAbove(b, nil, parent)
    b:SetScript("OnClick", ApplyLegacyListAt)
    AttachTooltip(b, "Use the List At price",
      "Fills the buyout and starting bid with Forever AH's recommended List At price. Nothing is posted: you still click Create Auction.")
    b:Hide()
    legacyUseButton = b
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
  AH.pendingSearch, AH.pendingIDs, AH.expectID = nil, nil, nil
  pendingToken = pendingToken + 1
  AH.atrSellItemID, AH.atrSellLocation, AH.atrSellSuffix, AH.atrReady = nil, nil, nil, false
  AH.legacySellID, AH.legacySellCount = nil, nil
  AH.otherHouse, AH.npcFaction = false, nil
  for id in pairs(AH.live) do AH.live[id] = nil end
  chartDirty = false
  for sf in pairs(sellIDs) do sellIDs[sf] = nil end
  for sf in pairs(sellSuffix) do sellSuffix[sf] = nil end
  for _, b in pairs(useButtons) do b.price = nil end
  if atrUseButton then atrUseButton.price = nil end
  for _, b in pairs(useButtons) do b:Hide() end
  if atrUseButton then atrUseButton:Hide() end
  if legacyUseButton then legacyUseButton:Hide() end
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
  AUCTION_HOUSE_BROWSE_RESULTS_ADDED = function() OnBrowseResults() end,
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

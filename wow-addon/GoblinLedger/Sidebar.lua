local ADDON_NAME, ns = ...
-- Goblin Ledger: the auction house sidebar.
-- A slim panel attached to the right edge of the game's own auction house
-- window, PocketGE plugin style. It opens and closes with the auction house
-- (it is a child of that window), so there is no extra window to manage.
--   Flip ideas : first the flips the price collector found in the latest
--                snapshot (Data.lua's flips: cheap listings to buy and relist,
--                green, blue or epic items listed well under their usual
--                price, or listings under what a vendor pays), ranked by the
--                profit you can expect within about a day (only as many as
--                have really been selling count, at about the price they
--                have really been selling for), then the ideas from the price
--                history (buy under Target Buy, sell at the sell target),
--                each item once. They are sized to the gold on this
--                character: what it cannot afford is left out, and an idea it
--                can afford only part of says how many it can buy. One row is
--                kept for the best solid deal it can afford (a green, blue or
--                epic item whose relist price is backed by sales), so one
--                shows whenever there is one.
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
local FLIP_H = 42
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
local MIN_SALES_HOURS = 3    -- sales measured over fewer hours than this are too rough
local MIN_AUCTIONS = 3       -- prices behind fewer separate listings are not trusted
local VENDOR_FAR = 200       -- nor a market price over 200x what a vendor pays
local VENDOR_FAR_UNITS = 10  -- ... with fewer units than this listed (dust sells far above vendor)
local DEAL_EVERY = 5         -- one place in every 5 flip ideas is kept for a solid deal (see ns.FlipIdeas)
-- What a collector flip can really make within about a day (see DayValue).
local DAY_SHARE = 0.5        -- share of a day's sales one seller can expect at the relist price
local DAY_HOUR_CAP = 3       -- an hour's sales count at most 3x a typical hour (and at least 1)
local DAY_TRIM = 0.2         -- typical hour: the mean of the measured hours without the busiest 20%
local DAY_SALE_HOURS = 2     -- sales must show in at least 2 different hours of the last day
local DAY_PRICE_CAP = 1.25   -- count the relist at most 25% above what it sold for (and, for a gap, sells for now)
local DAY_RECENT = 6         -- gap flips: the last 6 hourly market prices are its price now
local DAY_SPIKE = 1.5        -- gap flips: and never above 1.5x its usual (7-day median) price
local DAY_DEPOSIT = 0.30     -- listing deposit per unit, as a share of the vendor price, lost when it expires
local DAY_DEPOSIT_NOTE = 1000 -- the hover mentions the deposit only when the unsold ones risk at least 10 silver
local DAY_MIN_PROFIT = 2000  -- 20 silver: less expected in a day is not worth a row
local DAY_EXACT = 60         -- expected sales are worked out exactly up to this many units
local HIDE_SECONDS = 24 * 3600

-- Starter watchlist: the same staples as the price website's default favorites.
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
  if type(cf) == "table" and cf.AddMessage then cf:AddMessage("Goblin Ledger: " .. tostring(msg)) end
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

-- "" and "Item 12345" are what the data says when it has no name for an item.
local function KnownName(n)
  return type(n) == "string" and n ~= "" and not IsSecret(n) and n:match("^Item %d+$") == nil
end

-- The game's item id behind a version id (vid); the id itself otherwise.
local function BaseID(id)
  if type(ns.BaseItemID) == "function" then
    local ok, b = pcall(ns.BaseItemID, id)
    if ok and Num(b) then return b end
  end
  return id
end

local function GameItemName(id)
  if BaseID(id) ~= id then
    -- A version: Core.lua knows how to name it.
    if type(ns.GameItemName) == "function" then
      local ok, n = pcall(ns.GameItemName, id)
      if ok and KnownName(n) then return n end
    end
    return nil
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
  if type(_G.GetItemInfo) == "function" then
    local ok, n = pcall(_G.GetItemInfo, id)
    if ok and KnownName(n) then return n end
  end
  return nil
end

-- The data's name, else the game's name, else "Item 12345".
local function ItemName(id)
  if type(ns.ItemName) == "function" then
    local ok, n = pcall(ns.ItemName, id)
    if ok and type(n) == "string" and n ~= "" then return n end
  end
  local it = type(ns.GetItem) == "function" and ns.GetItem(id) or nil
  if it and KnownName(it.name) then return it.name end
  return GameItemName(id) or ("Item " .. tostring(id))
end

local function SalesHours()
  if type(ns.SalesHours) == "function" then
    local ok, h = pcall(ns.SalesHours)
    if ok and Num(h) then return h end
  end
  return 24
end

-- Estimated sales per day (nil while sales are still being measured).
local function SoldPerDay(it)
  if type(ns.SoldPerDay) == "function" then
    local ok, per, early = pcall(ns.SoldPerDay, it)
    if ok then return per, early end
  end
  return Num(it and it.sold24) or 0, false
end

local function ItemIcon(id)
  id = BaseID(id)
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
  id = BaseID(id)
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

-- The gold on this character (copper), or nil when the game does not say.
function ns.PlayerGold()
  local f = _G.GetMoney
  if type(f) ~= "function" then return nil end
  local ok, v = pcall(f)
  v = ok and Num(v) or nil
  if not v or v < 0 then return nil end
  return floor(v)
end

-- What the gold on this character buys of an idea. Each fit: units, cost,
-- profit, limited (true when the gold covers less than the whole idea), gold
-- (what it was sized to) and whole (the units in the whole idea).
-- A collector flip: all of it when the gold covers its cost, else as many as
-- the gold buys at the flip's average price per unit (the cheapest listings
-- go first in game, so at least that many are affordable). nil when not
-- even one is affordable. Worked out in whole numbers: can = floor(gold /
-- (cost / units)), costing ceil(can * cost / units), making
-- floor(profit * can / units).
local function FitFlip(f, gold)
  local units, cost, profit = f.units, f.cost, f.profit
  if gold >= cost then
    return { units = units, cost = cost, profit = profit, limited = false, gold = gold, whole = units }
  end
  local can = floor(gold * units / cost)
  if can >= units then can = units - 1 end
  if can < 1 then return nil end
  local spend = can * cost
  local c = floor(spend / units)
  if c * units < spend then c = c + 1 end
  return { units = can, cost = c, profit = floor(profit * can / units), limited = true, gold = gold, whole = units }
end

-- A price-history idea: a day's flip (a day's sales, at most VOL_CAP units,
-- at least 1), or as many as the gold buys at the buy price. nil when not
-- even one is affordable.
local function FitHistory(x, gold)
  local whole = max(1, floor(min(Num(x.sold) or 0, VOL_CAP)))
  local can = floor(gold / x.buy)
  if can < 1 then return nil end
  local n = min(can, whole)
  return { units = n, cost = n * x.buy, profit = n * x.profit, limited = can < whole, gold = gold, whole = whole }
end

local function HasWord(list, word)
  for _, w in ipairs(list) do
    if w == word then return true end
  end
  return false
end

-- How far a deal's buy-up-to price is under its usual price, in whole
-- percent (rounded down, so every listing bought is at least that far
-- under), or nil when the usual price is not known.
local function DealUnder(idea)
  local usual, buy = Num(idea.usual) or 0, Num(idea.buy) or 0
  if usual <= 0 or buy <= 0 or buy >= usual then return nil end
  local pct = floor(100 * (usual - buy) / usual)
  if pct < 1 then return nil end
  return pct
end
ns.DealUnder = DealUnder

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
  local hasVersions = type(ns.HasVariants) == "function" and ns.HasVariants or function() return false end
  -- Each line's numbers are read without unpacking or keeping the item
  -- (ns.PeekItem): an item table kept for each of tens of thousands of lines
  -- took 50 MB. Only the candidates' advice below unpacks items.
  local peek = type(ns.PeekItem) == "function" and ns.PeekItem or function(n)
    local it = ns.GetItem(n)
    if it then return it.cheapest, it.market, it.listed, it.sold24, it.vendor, tonumber(it.auctions) end
  end
  local one = {}   -- what SoldPerDay reads (sold24), for every line in turn
  for id in pairs(db.items) do
    local n = tonumber(id)
    local cheapest, market, listed, sold24, vendor, auctions
    if n then cheapest, market, listed, sold24, vendor, auctions = peek(n) end
    one.sold24 = sold24
    local sold = cheapest and SoldPerDay(one)
    if sold and cheapest > 0 and market > 0 and sold >= MIN_SOLD_RELAX and listed >= 2
      and not (auctions and auctions < MIN_AUCTIONS)
      and not ((vendor or 0) > 0 and market > VENDOR_FAR * vendor and listed < VENDOR_FAR_UNITS)
      -- An item whose versions differ: its prices mix them (each version that
      -- has its own prices is a candidate of its own).
      and not hasVersions(n) then
      cand[#cand + 1] = { id = n, q = market * min(sold, VOL_CAP) }
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
-- best first: { id, buy, sell, profit (each), perDay, sold, early, verdict,
-- rec, fit }, and, when there are none, why: "nodata", "history" (prices or
-- sales are still being collected), "nosales" (nothing sells enough yet),
-- "gold" (not enough gold on this character for any of them) or "none".
-- gold: the copper to size the ideas to (nil: the gold on this character,
-- false: no sizing). Sized ideas get a fit (see FitHistory), the ones the
-- gold cannot buy are left out before the list is cut to `limit`, and they
-- all rank by the profit on what the gold buys (a slower seller's day of
-- sales is already a smaller flip). Without sizing, the items that sell
-- plenty a day (MIN_SOLD) come before the slower ones, best day's profit
-- first. Slower sellers are marked thin either way.
local function HistoryIdeas(limit, gold)
  limit = limit or MAX_FLIPS
  if gold == nil then gold = ns.PlayerGold() end
  if not HasData() then return {}, "nodata" end
  -- Sales from the first hour or two, scaled up to a day, are too rough to rank by.
  if SalesHours() < MIN_SALES_HOURS then return {}, "history" end
  local cands = Candidates()
  if #cands == 0 then return {}, "nosales" end
  local hidden = HiddenIdeas()
  local first, later = {}, {}
  local tried, noHistory, poor = 0, 0, 0
  for _, id in ipairs(cands) do
    if not hidden[id] then
      tried = tried + 1
      local r = Advice(id, FLIP_TF)
      if r and not r.history then noHistory = noHistory + 1 end
      if r and r.history and r.advice ~= false and (r.margin or 0) > 0 and (r.buy or 0) > 0
        and (r.marginPct or 0) >= MIN_MARGIN_PCT
        and not r.far and not r.vendorBetter and r.verdict ~= ns.CALL_HIGH then
        local sold = Num(r.soldPerDay) or 0
        local idea = {
          id = id, buy = r.buy, sell = r.sell, profit = r.margin,
          perDay = r.margin * min(sold, VOL_CAP), sold = sold, early = r.soldEarly == true,
          verdict = r.verdict, rec = r,
        }
        if gold then idea.fit = FitHistory(idea, gold) end
        if gold and not idea.fit then
          poor = poor + 1
        else
          -- A slower seller: it may take a while to sell.
          idea.thin = sold < MIN_SOLD or nil
          if idea.thin and not gold then later[#later + 1] = idea else first[#first + 1] = idea end
        end
      end
    end
  end
  local function better(a, b)
    if gold then
      if a.fit.profit ~= b.fit.profit then return a.fit.profit > b.fit.profit end
    elseif a.perDay ~= b.perDay then
      return a.perDay > b.perDay
    end
    return a.id < b.id
  end
  sort(first, better)
  sort(later, better)
  local out = {}
  for _, x in ipairs(first) do
    if #out >= limit then break end
    out[#out + 1] = x
  end
  for _, x in ipairs(later) do
    if #out >= limit then break end
    out[#out + 1] = x
  end
  if #out > 0 then return out end
  -- There were ideas, but this character cannot afford any of them.
  if poor > 0 then return out, "gold" end
  -- Every item that trades enough is still waiting for price history.
  if tried > 0 and noHistory == tried then return out, "history" end
  return out, "none"
end
ns.HistoryFlipIdeas = HistoryIdeas

-- What the last day of hourly data says about selling an item (or version),
-- or nil while sales are not measured yet: sold (units seen selling in the
-- last day, no hour counting for more than DAY_HOUR_CAP times a typical hour,
-- never scaled up from part of a day and never above sold24), hours (how many
-- hours had a sale), soldAt (the price those sales happened at: each hour's
-- sales count at the cheapest listing of the hour before; the price half of
-- the capped sales were at or under) and recent (the median market price of
-- the last DAY_RECENT hourly prices). Cached until the data changes.
local dayCache = { key = nil, t = {} }
local function DayStats(id)
  local key = ns.db and tostring(ns.db.updated) or ""
  if dayCache.key ~= key then dayCache.key, dayCache.t = key, {} end
  local c = dayCache.t[id]
  if c ~= nil then return c or nil end
  local it = type(ns.GetItem) == "function" and ns.GetItem(id) or nil
  local s = it and type(ns.GetSeries) == "function" and ns.GetSeries(id, "1D") or nil
  local h = SalesHours()
  if not s or not h or h <= 0 then
    dayCache.t[id] = false
    return nil
  end
  local xs = {}
  for i = 1, s.n do xs[i] = Num(s.sold[i]) or 0 end
  local sorted = {}
  for i = 1, s.n do sorted[i] = xs[i] end
  sort(sorted, function(a, b) return a > b end)
  local trim = floor(h * DAY_TRIM)
  local total, top = 0, 0
  for i, x in ipairs(sorted) do
    total = total + x
    if i <= trim then top = top + x end
  end
  local cap = max(1, DAY_HOUR_CAP * (total - top) / max(1, h - trim))
  local sold, hours, wsum, at = 0, 0, 0, {}
  for i = 1, s.n do
    local x = xs[i]
    if x > 0 then
      hours = hours + 1
      local w = min(x, cap)
      sold = sold + w
      local p = (i > 1 and s.cheap[i - 1]) or s.cheap[i] or s.market[i]
      if p then
        wsum = wsum + w
        at[#at + 1] = { p = p, w = w }
      end
    end
  end
  -- the price half of those (capped) sales were at or under
  local soldAt
  sort(at, function(a, b) if a.p ~= b.p then return a.p < b.p end return a.w < b.w end)
  local acc = 0
  for _, e in ipairs(at) do
    acc = acc + e.w
    if acc * 2 >= wsum then soldAt = e.p break end
  end
  local out = { sold = min(sold, Num(it.sold24) or 0), hours = hours, soldAt = soldAt }
  local rec = {}
  for i = s.n, 1, -1 do
    if s.market[i] then rec[#rec + 1] = s.market[i] end
    if #rec >= DAY_RECENT then break end
  end
  sort(rec)
  local k = #rec
  if k > 0 then out.recent = (k % 2 == 1) and rec[(k + 1) / 2] or (rec[k / 2] + rec[k / 2 + 1]) / 2 end
  dayCache.t[id] = out
  return out
end

-- Expected units of yours sold in a day when buyers take about `rate` a day
-- from the cheapest up and `ahead` cheaper units sit before your n: the sum
-- over j = ahead .. ahead + n - 1 of P(X > j), X Poisson(rate). Big numbers:
-- the rate less the units ahead, between 0 and n.
local function ExpectedSold(rate, n, ahead)
  if ahead + n > DAY_EXACT or rate > DAY_EXACT then return max(0, min(n, rate - ahead)) end
  local p = math.exp(-rate)
  local cdf, tot = p, 0
  for j = 0, ahead + n - 1 do
    if j >= ahead then tot = tot + (1 - cdf) end
    p = p * rate / (j + 1)
    cdf = cdf + p
  end
  return tot
end

-- What a collector flip can really make within about a day, bought as `fit`
-- (units, cost): { countAt, day, dayValue, dayProfit, rate, stats }, or nil and
-- why ("nosales", "onehour", "loses", "small").
--   countAt  the relist price it is counted at: the relist, but at most
--            DAY_PRICE_CAP x the price it sold at in the last day (for a gap
--            flip also its market price now, and never over DAY_SPIKE x its
--            usual price). The dearest listing bought must still pay there.
--   rate     units one seller can expect to sell in a day: DAY_SHARE x sold.
--   day      your units expected to sell in a day (Poisson), after the cheaper
--            units your gold could not buy (they sell first).
--   dayValue day x (countAt after the cut - average price paid) - the
--            deposit (DAY_DEPOSIT x vendor price) of each unit not sold.
-- Vendor flips sell at once: the whole profit.
local function DayValue(f, fit)
  local n, w = fit.units, f.units
  if f.kind == "vendor" then
    return { countAt = 0, day = n, dayValue = fit.profit, dayProfit = fit.profit }
  end
  local st = DayStats(f.id)
  if not st or st.sold <= 0 or not st.soldAt then return nil, "nosales" end
  if st.hours < DAY_SALE_HOURS then return nil, "onehour" end
  local ref = st.soldAt
  if f.kind == "gap" and st.recent and st.recent < ref then ref = st.recent end
  local at = min(f.relist, floor(DAY_PRICE_CAP * ref))
  local usual = Num(f.usual) or 0
  if f.kind == "gap" and usual > 0 then at = min(at, floor(DAY_SPIKE * usual)) end
  local net = floor(at * 95 / 100)
  if net <= f.buyMax then return nil, "loses" end
  local rate = DAY_SHARE * st.sold
  local day = ExpectedSold(rate, n, w - n)
  local it = type(ns.GetItem) == "function" and ns.GetItem(f.id) or nil
  local dep = DAY_DEPOSIT * (Num(it and it.vendor) or 0)
  local v = day * (net - fit.cost / n) - (n - day) * dep
  if v < DAY_MIN_PROFIT then return nil, "small" end
  return { countAt = at, ref = ref, day = day, dayValue = v, dayProfit = floor(v), rate = rate, deposit = dep, stats = st }
end
ns.DayValue = DayValue
ns.ExpectedSold = ExpectedSold
ns.DayStats = DayStats   -- Crafts.lua: a made item counts at most DAY_PRICE_CAP x what it sold for

-- The flip ideas the sidebar shows, best first, up to `limit`: the
-- collector's flips from the latest snapshot first ({ id, data = true, kind,
-- buy = buyMax, units, cost, relist, profit, roi, risk, usual, fit, countAt,
-- ref, day, dayValue, dayProfit, rate, deposit, stats, solid }, see
-- DayValue), then the ideas from the price history, each item once. Ideas
-- hidden with "Not interested" are left out. When there are none, the second
-- value says why (see HistoryIdeas).
-- gold: the copper to size the ideas to (nil: the gold on this character,
-- false: no sizing, as when the game does not say). With gold, each idea
-- gets a fit (see FitFlip) and the ones it cannot buy are left out (the
-- reason for none is "gold" only when one of them would show with more gold).
-- A collector flip stays only when DayValue says it can make at least
-- DAY_MIN_PROFIT within about a day (on what the gold buys); they rank by
-- that (most first, then the one that sells soonest, then the collector's
-- order), still before the price-history ideas.
-- One place in every DEAL_EVERY (1 of the sidebar's 5 rows, 2 of the 10
-- /gl flips lines) is kept for the solid deals (a green, blue or epic item
-- listed well under its usual price, whose usual price is not one listing's
-- and whose relist price is backed by sales), so the best one shows on every
-- scan that has one: when bigger flips fill the list, the best solid deals
-- take the places of the last other flips. The list stays in the ranking's order.
function ns.FlipIdeas(limit, gold)
  limit = limit or MAX_FLIPS
  if not HasData() then return {}, "nodata" end
  -- Stale prices (3 hours or more, ns.AgeState): the cheap listings are often gone.
  if type(ns.AgeState) == "function" and ns.AgeState() == "stale" then return {}, "stale" end
  if gold == nil then gold = ns.PlayerGold() end
  local hidden = HiddenIdeas()
  local ranked, seen, dropped, order, poor, deals, ndropped = {}, {}, {}, {}, false, 0, 0
  local flips = type(ns.DataFlips) == "function" and ns.DataFlips() or {}
  for _, f in ipairs(flips) do
    if not hidden[f.id] and not seen[f.id] then
      local whole = { units = f.units, cost = f.cost, profit = f.profit }
      local fit = gold and FitFlip(f, gold) or nil
      local dv = (not gold or fit) and DayValue(f, fit or whole) or nil
      if gold and not fit then
        -- Not even one is affordable. The reason for no ideas is "gold" only when
        -- more gold would show it: when it pays bought whole (a flip that pays
        -- bought in part also pays bought whole). One with no sales yet would not.
        if DayValue(f, whole) then poor = true end
      elseif not dv then
        -- found, but it would not sell within a day at a price that pays: left out, and no
        -- price-history idea for it either (another collector flip of the item still counts)
        if not dropped[f.id] then ndropped = ndropped + 1 end
        dropped[f.id] = true
      else
        seen[f.id] = true
        local risk = {}
        for _, w in ipairs(f.risk or {}) do risk[#risk + 1] = w end
        local idea = {
          id = f.id, data = true, kind = f.kind, buy = f.buyMax, units = f.units, cost = f.cost,
          relist = f.relist, profit = f.profit, roi = f.roi, risk = risk, usual = Num(f.usual) or 0,
          fit = fit, countAt = dv.countAt, ref = dv.ref, day = dv.day, dayValue = dv.dayValue,
          dayProfit = dv.dayProfit, rate = dv.rate, deposit = dv.deposit, stats = dv.stats,
          -- a deal whose usual price is backed by more than one listing and whose relist is backed by sales
          solid = f.kind == "deal" and not HasWord(f.risk or {}, "unsure") and dv.countAt == f.relist,
        }
        ranked[#ranked + 1] = idea
        order[idea] = #ranked
        if idea.solid then deals = deals + 1 end
      end
    end
  end
  -- Most expected in a day first, then the one that sells soonest, then the collector's order.
  sort(ranked, function(a, b)
    if a.dayValue ~= b.dayValue then return a.dayValue > b.dayValue end
    local sa, sb = a.day / (a.fit and a.fit.units or a.units), b.day / (b.fit and b.fit.units or b.units)
    if sa ~= sb then return sa > sb end
    return order[a] < order[b]
  end)
  -- The best `limit` of them, keeping the solid deals' places: another flip is
  -- taken only while the places left still hold the solid deals that need one.
  local keep = min(deals, floor(limit / DEAL_EVERY))
  local out, got = {}, 0
  for _, x in ipairs(ranked) do
    if #out >= limit then break end
    if x.solid then
      out[#out + 1] = x
      got = got + 1
    elseif keep - got < limit - #out then
      out[#out + 1] = x
    end
  end
  if #out >= limit then return out end
  -- Enough price-history ideas to fill the places left after skipping the items
  -- already shown and the ones whose collector flip was dropped.
  local hist, why = HistoryIdeas(limit + #out + ndropped, gold or false)
  for _, x in ipairs(hist) do
    if #out >= limit then break end
    if not seen[x.id] and not dropped[x.id] then
      seen[x.id] = true
      out[#out + 1] = x
    end
  end
  if #out > 0 then return out end
  if poor then return out, "gold" end
  return out, why
end

-- Plain-English words for the collector's risk flags. A flip's hover says
-- only the suffix one now: DayValue already counts what the others warn of.
local RISK_TEXT = {
  thin = "Thin: only a few listings are left above your relist price, so one new seller can undercut you.",
  slow = "Slow: fewer sell in a day than you would buy, or sales have not been measured for a full day yet. It may take days to sell them all.",
  spike = "Spike: the relist price is far above this item's usual price (or far above what you pay, with only a few listings up there). It may not sell there.",
  unsure = "Unsure: only a few are listed, far above what a vendor pays (or only 1 or 2 listings), so the price may be one player's guess.",
  suffix = "Versions: this gear comes in versions (like \"of the Bear\") that sell for different prices. Check the version before you buy.",
}
ns.RISK_TEXT = RISK_TEXT

-- What the flip area says when there are no ideas (why from ns.FlipIdeas).
local FLIP_MESSAGES = {
  nodata = "No price data yet. Run Install-Goblin-Ledger.bat, then /reload.",
  history = "Collecting price history. Flip ideas show up after a few hourly price updates.",
  -- Sales are measured between hourly snapshots, so a new data set has none yet.
  nosales = "Collecting sales history. Flip ideas show up after a few hourly price updates.",
  none = "No flips likely to sell within a day right now. Check back after the next update.",
  gold = "Not enough gold on this character for the current flip ideas.",
  stale = "Prices are 3 hours old or more: no flip ideas until newer ones load (type /gl to see how).",
}
ns.FLIP_MESSAGES = FLIP_MESSAGES

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
  -- A version (vid) is searched by its item's name; the chart shows the version.
  local base = BaseID(id)
  local name = ItemName(base)
  local F, kind = AHFrame()
  if not F then return false end
  -- Tell AH.lua which item this is, so a name two items share (Squire's
  -- Shirt) keeps this one on the chart, not the one with the lowest id.
  if type(ns.AH_ExpectItem) == "function" then pcall(ns.AH_ExpectItem, id) end
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
    pendingOpen = { id = base, vid = (base ~= id) and id or nil, token = openToken, t = Now() }
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

-- The game's name for a browse row ("Ridge Cleaver of the Bear" when the
-- auction house lists each version as its own row), or nil.
local function RowName(r)
  local CAH = _G.C_AuctionHouse
  if type(CAH) ~= "table" or type(CAH.GetItemKeyInfo) ~= "function" then return nil end
  local ok, info = pcall(CAH.GetItemKeyInfo, r.itemKey)
  local n = ok and type(info) == "table" and info.itemName or nil
  if type(n) == "string" and not IsSecret(n) and n ~= "" then return n end
  return nil
end

-- When the search comes back, open the exact item (a name search also finds
-- "Bolt of Silk Cloth" when you asked for "Silk Cloth"). For a version: when
-- the auction house lists versions as rows of their own, only the row with
-- that version's name is opened; when none has it, the list stays for you to
-- pick (opening another version would show the wrong listings).
local function OnBrowseResults()
  local p = pendingOpen
  if not p then return end
  local CAH, F = _G.C_AuctionHouse, _G.AuctionHouseFrame
  if type(CAH) ~= "table" or type(CAH.GetBrowseResults) ~= "function" or type(F) ~= "table" then return end
  local ok, list = pcall(CAH.GetBrowseResults)
  if not ok or type(list) ~= "table" then return end
  local rows = {}
  for _, r in ipairs(list) do
    local key = type(r) == "table" and r.itemKey
    if type(key) == "table" and Num(key.itemID) == p.id then rows[#rows + 1] = r end
  end
  if #rows == 0 then return end
  pendingOpen = nil
  local pick = rows[1]
  if p.vid then
    -- A version: open only the row with its name. When the auction house
    -- lists versions as rows of their own and none is this one, say so: it
    -- sold or was taken down since the price update, and the rows shown are
    -- other versions. One row with the item's plain name lists every version
    -- together, so that one is opened.
    pick = nil
    local want, plain, split = ItemName(p.vid), ItemName(p.id), false
    for _, r in ipairs(rows) do
      local n = RowName(r)
      if n == want then pick = r break end
      if n and n ~= plain then split = true end
    end
    if not pick then
      if split then
        Print(want .. ": none listed right now (it may have sold). These are its other versions.")
      elseif #rows == 1 then
        pick = rows[1]
      end
    end
  end
  if pick and type(F.SelectBrowseResult) == "function" then pcall(F.SelectBrowseResult, F, pick) end
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

-- Shows the game's item tooltip for an id: a version through a link with its
-- bonus ids (so the game shows that version and the price lines are its own).
-- No enchant lines (Enchants.lua) on these: the row is about the price.
local function SetItemTooltip(tt, id)
  local link = type(ns.VariantLink) == "function" and ns.VariantLink(id) or nil
  if link and type(tt.SetHyperlink) == "function" then
    ns.tipQuiet = true
    local ok = pcall(tt.SetHyperlink, tt, link)
    ns.tipQuiet = false
    if ok then return true end
  end
  if type(tt.SetItemByID) == "function" then
    ns.tipQuiet = true
    local ok = pcall(tt.SetItemByID, tt, BaseID(id))
    ns.tipQuiet = false
    return ok
  end
  return false
end

-- What the gold on this character buys of an idea (only when the ideas are
-- sized to it): how many of the whole idea, or that it covers all of it. A
-- relisted collector flip's profit is said in its "Within a day" lines.
local function GoldLine(tt, idea)
  local fit = idea.fit
  if type(fit) ~= "table" then return end
  if fit.limited then
    local making = (idea.data and idea.kind ~= "vendor") and "" or (", making " .. Paint("+" .. Money(fit.profit), "pos"))
    tt:AddLine("With your gold (" .. Money(fit.gold) .. "): buy " .. Count(fit.units) .. " of " .. Count(fit.whole)
      .. ", costing " .. Money(fit.cost) .. making, 1, 0.82, 0, true)
  else
    tt:AddLine("Your gold covers all of it.", 0.8, 0.8, 0.8, true)
  end
end

local function DataFlipTooltip(tt, idea)
  tt:AddLine(Paint("Flip idea from the latest prices", "buy"))
  local usual = Num(idea.usual) or 0
  local at = Num(idea.countAt) or Num(idea.relist) or 0
  local capped = idea.kind ~= "vendor" and at < (Num(idea.relist) or 0)
  if idea.kind == "vendor" then
    tt:AddLine("These listings cost less than a vendor pays for them: buy them and sell them to a vendor.", 0.8, 0.8, 0.8, true)
  elseif idea.kind == "deal" then
    tt:AddLine("Listed well under its usual price" .. (usual > 0 and (" (usual " .. Money(usual) .. ")") or "")
      .. (capped and ", but it has been selling for less: buy it and relist it at the price below."
        or ": buy it and relist it at the price below, about what it has been selling for."), 0.8, 0.8, 0.8, true)
  else
    tt:AddLine("Buy the cheap listings, then relist them at about what it has been selling for.", 0.8, 0.8, 0.8, true)
  end
  tt:AddDoubleLine("Buy up to (each)", Money(idea.buy), 0.8, 0.8, 0.8, 1, 1, 1)
  tt:AddDoubleLine("That buys " .. Count(idea.units) .. ", costing", Money(idea.cost), 0.8, 0.8, 0.8, 1, 1, 1)
  if idea.kind == "vendor" then
    tt:AddDoubleLine("Then", "sell to a vendor", 0.8, 0.8, 0.8, 1, 1, 1)
    tt:AddDoubleLine("Profit over the cost", Paint("+" .. Money(idea.profit), "pos"), 0.8, 0.8, 0.8)
  else
    tt:AddDoubleLine("Relist at (each)", Money(at), 0.8, 0.8, 0.8, 1, 1, 1)
    if Num(idea.ref) then tt:AddDoubleLine("Selling lately for about", Money(idea.ref), 0.8, 0.8, 0.8, 1, 1, 1) end
    if capped then
      tt:AddLine("You could ask up to " .. Money(idea.relist) .. ", but it has not been selling that high.", 0.6, 0.6, 0.6, true)
    end
  end
  if idea.kind == "deal" and usual > 0 then
    tt:AddDoubleLine("Usual price (last 7 days)", Money(usual), 0.8, 0.8, 0.8, 1, 1, 1)
    local under = DealUnder(idea)
    if under then tt:AddDoubleLine("Under the usual price by", under .. "% or more", 0.8, 0.8, 0.8, 1, 1, 1) end
  end
  GoldLine(tt, idea)
  if idea.kind ~= "vendor" and Num(idea.dayProfit) then
    local n = idea.fit and idea.fit.units or idea.units
    local rate = Num(idea.rate) or 0
    tt:AddLine(" ")
    tt:AddLine(Paint("Within a day", "buy"))
    tt:AddDoubleLine("You can expect to sell", (rate >= 1 and ("~" .. Count(rate)) or "under 1") .. " a day", 0.8, 0.8, 0.8, 1, 1, 1)
    if n == 1 then
      tt:AddDoubleLine("Chance yours sells in a day", floor(100 * idea.day + 0.5) .. "%", 0.8, 0.8, 0.8, 1, 1, 1)
    else
      -- under half of one would show as "~0" next to a profit
      tt:AddDoubleLine("Of your " .. Count(n) .. ", likely to sell", idea.day >= 0.5 and ("~" .. Count(idea.day)) or "under 1",
        0.8, 0.8, 0.8, 1, 1, 1)
    end
    tt:AddDoubleLine("Profit in a day, after the 5% cut", Paint("+" .. Money(idea.dayProfit), "pos"), 0.8, 0.8, 0.8)
    if n - idea.day >= 1 and (n - idea.day) * (Num(idea.deposit) or 0) >= DAY_DEPOSIT_NOTE then
      tt:AddLine("The rest may take longer. Each one that expires unsold loses its deposit (about "
        .. Money(idea.deposit) .. ").", 0.6, 0.6, 0.6, true)
    end
    local h = SalesHours()
    if h < 24 then
      tt:AddLine("Sales measured for " .. h .. (h == 1 and " hour" or " hours") .. " so far, so these are early numbers.",
        0.6, 0.6, 0.6, true)
    end
  end
  if type(idea.risk) == "table" and HasWord(idea.risk, "suffix") then
    tt:AddLine(" ")
    tt:AddLine(Paint("Watch out", "neg"))
    tt:AddLine(RISK_TEXT.suffix, 0.6, 0.6, 0.6, true)
  end
end

-- Tooltip for a row: the game's own item tooltip (which already carries the
-- Goblin Ledger price lines), then the flip numbers and what clicks do.
local function RowTooltip(row)
  local id = row.itemID
  local tt = _G.GameTooltip
  if not id or not tt then return end
  row.glNoEnchants = true   -- Enchants.lua: no enchant lines on a tooltip this row owns
  tt:SetOwner(row, "ANCHOR_RIGHT")
  local shown = SetItemTooltip(tt, id)
  if not shown then tt:AddLine(ItemName(id), QualityRGB(id)) end
  local idea = row.idea
  if idea and idea.kind == "craft" and type(ns.CraftRowTooltip) == "function" then
    -- An idea from your own recipes (Crafts.lua): its mats and what it makes.
    tt:AddLine(" ")
    ns.CraftRowTooltip(tt, idea)
  elseif idea and idea.data then
    tt:AddLine(" ")
    DataFlipTooltip(tt, idea)
  elseif idea then
    tt:AddLine(" ")
    tt:AddLine(Paint("Flip idea", "buy"))
    tt:AddDoubleLine("Buy at or under", Money(idea.buy), 0.8, 0.8, 0.8, 1, 1, 1)
    tt:AddDoubleLine("Sell at", Money(idea.sell), 0.8, 0.8, 0.8, 1, 1, 1)
    tt:AddDoubleLine("Profit each, after the 5% cut", Paint("+" .. Money(idea.profit), "pos"), 0.8, 0.8, 0.8)
    tt:AddDoubleLine("Sells per day (estimate)", "~" .. Count(idea.sold), 0.8, 0.8, 0.8, 1, 1, 1)
    local fit = idea.fit
    if type(fit) == "table" then
      -- The whole idea: a day's sales (at most VOL_CAP), then what your gold buys of it.
      tt:AddDoubleLine("For a day: buy " .. Count(fit.whole) .. ", costing", Money(fit.whole * idea.buy), 0.8, 0.8, 0.8, 1, 1, 1)
      tt:AddDoubleLine("Profit on those, after the cut", Paint("+" .. Money(fit.whole * idea.profit), "pos"), 0.8, 0.8, 0.8)
      GoldLine(tt, idea)
    end
    if idea.early then
      tt:AddLine("Early estimate: sales measured for less than a day so far, scaled up to a full day.", 0.6, 0.6, 0.6, true)
    end
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

-- A chat link for an item, or for a version (the game builds it from the
-- version's bonus ids, so it names the version; else the item's own link).
local function ChatLink(id)
  local CI = _G.C_Item
  if type(CI) ~= "table" or type(CI.GetItemInfo) ~= "function" then return nil end
  local function linkOf(x)
    local ok, _, l = pcall(CI.GetItemInfo, x)
    if ok and type(l) == "string" and not IsSecret(l) then return l end
    return nil
  end
  local base = BaseID(id)
  if base ~= id and type(ns.VariantLink) == "function" then
    local v = ns.VariantLink(id)
    local l = v and linkOf(v)
    if l then return l end
  end
  return linkOf(base)
end

local function RowClick(row, button)
  local id = row.itemID
  if not id then return end
  if button == "RightButton" then
    OpenMenu(row)
    return
  end
  if type(_G.IsModifiedClick) == "function" and _G.IsModifiedClick("CHATLINK") then
    local link = ChatLink(id)
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
  -- A craft idea searches the mat you buy (Crafts.lua sets it).
  ns.SearchAH(row.idea and row.idea.searchID or id)
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
    -- Three lines: name and profit; what to buy (and the sales per day);
    -- what to do with it.
    local left = 5 + isz + 6
    r.name:SetPoint("TOPLEFT", r, "TOPLEFT", left, -3)
    r.name:SetPoint("RIGHT", r, "RIGHT", -64, 0)
    r.right:SetPoint("TOPRIGHT", r, "TOPRIGHT", -20, -3)
    r.right2 = NewFS(r, "GameFontHighlightSmall")
    r.right2:SetJustifyH("RIGHT")
    r.right2:SetPoint("RIGHT", r, "RIGHT", -4, 0)
    SetFSColor(r.right2, "muted")
    r.sub = NewFS(r, "GameFontHighlightSmall")
    r.sub:SetPoint("LEFT", r, "LEFT", left, 0)
    r.sub:SetPoint("RIGHT", r.right2, "LEFT", -4, 0)
    r.sub2 = NewFS(r, "GameFontHighlightSmall")
    r.sub2:SetPoint("BOTTOMLEFT", r, "BOTTOMLEFT", left, 3)
    r.sub2:SetPoint("RIGHT", r, "RIGHT", -4, 0)
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
  self.title:SetText(Paint("Goblin", "buy") .. Paint(" Ledger", "sell"))
  self.age = NewFS(F, "GameFontHighlightSmall")
  self.age:SetPoint("TOPLEFT", self.title, "BOTTOMLEFT", 0, -3)
  self.age:SetPoint("RIGHT", F, "RIGHT", -PAD, 0)
  SetFSColor(self.age, "muted")
  local ageTip = CreateFrame("Frame", nil, F)
  ageTip:SetAllPoints(self.age)
  ageTip:EnableMouse(true)
  SimpleTip(ageTip, "Price data",
    function()
      return SB.why or "Prices from Blizzard's hourly auction house data, also at grantdenney.com/goblin-ledger/prices. They refresh every 30 minutes (Install-Goblin-Ledger.bat sets that up); type /reload to load new ones."
    end)

  local hide = TextButton(F, "<", 18, 18)
  hide:SetPoint("TOPRIGHT", F, "TOPRIGHT", -6, -6)
  SetFSColor(hide.text, "muted")
  hide:SetScript("OnClick", function() ns.SetSidebar(false) end)
  SimpleTip(hide, "Tuck the sidebar away", "Bring it back with the Goblin Ledger tab on the auction house, or /gl sidebar.")
  self.hideButton = hide

  -- Flip ideas
  self.flipTitle = SectionTitle(F, "FLIP IDEAS")
  self.flipTitle:SetPoint("TOPLEFT", F, "TOPLEFT", PAD, -HEADER_H)
  -- "for your 123g": the gold the ideas are sized to (empty when the game does not say).
  self.flipGold = NewFS(F, "GameFontHighlightSmall")
  self.flipGold:SetPoint("LEFT", self.flipTitle, "RIGHT", 6, 0)
  SetFSColor(self.flipGold, "muted")
  local flipHelp = CreateFrame("Frame", nil, F)
  flipHelp:SetAllPoints(self.flipTitle)
  flipHelp:EnableMouse(true)
  local helpText = "Flips your gold can buy now, ranked by the profit you can expect within a day: only as many "
    .. "as have really been selling count, at about the price they have really been selling for. "
    .. "The best green, blue or epic listed well under the price it really sells for keeps a row. "
    .. "Then items to buy under the Target Buy price and sell at the sell price, from the price history. "
    .. "What your gold cannot afford is left out, and a row says how many you can buy. "
    .. "Profits are after the 5% auction cut. Prices move and sales are estimates, so treat "
    .. "these as ideas, not promises. Hover a row for the details."
  SimpleTip(flipHelp, "Flip ideas", helpText)
  local goldHelp = CreateFrame("Frame", nil, F)
  goldHelp:SetAllPoints(self.flipGold)
  goldHelp:EnableMouse(true)
  SimpleTip(goldHelp, "Sized to your gold",
    "Flip ideas only show what the gold on this character can buy, and how many of each it buys. "
    .. "They are worked out again when your gold changes.")
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
  local tab = TextButton(parent, "Goblin Ledger >", 104, 20)
  SetFSColor(tab.text, "buy")
  tab:SetScript("OnClick", function() ns.SetSidebar(true) end)
  SimpleTip(tab, "Goblin Ledger sidebar", "Show flip ideas and your watchlist next to the auction house.")
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
-- (the AH opened, a live price arrived, an idea was hidden, your gold
-- changed). Redrawing the rows (a star clicked, item names arriving) reuses them.
function SB:RefreshFlips()
  if self.flipsDirty or not self.ideas then
    self.gold = ns.PlayerGold()
    self.ideas, self.flipWhy = ns.FlipIdeas(MAX_FLIPS, self.gold or false)
    self.flipsDirty = false
  end
  if self.flipGold then self.flipGold:SetText(self.gold and ("for your " .. ShortMoney(self.gold)) or "") end
  local ideas = self.ideas
  for i, r in ipairs(self.flipRows) do
    local idea = ideas[i]
    if idea then
      SetRowItem(r, idea.id)
      r.idea = idea
      local fit = idea.fit
      if idea.kind == "craft" then
        -- An idea from your own recipes (Crafts.lua words it): what to buy and
        -- make, what it sells for, how many sell in a day.
        r.sub:SetText(idea.subText or "")
        r.sub2:SetText(idea.sub2Text or "")
        r.right2:SetText(idea.noteText or "")
      elseif idea.data then
        -- A flip from the latest snapshot: how much to pay and for how many
        -- (of how many, when your gold covers only some), then the relist
        -- price its profit is counted at (or sell to a vendor). The small
        -- grey note is how many one seller can expect to sell in a day.
        local n = Count(idea.units)
        if fit and fit.limited then n = Count(fit.units) .. " of " .. Count(idea.units) end
        r.sub:SetText(Paint("Buy up to " .. ShortMoney(idea.buy) .. " (" .. n .. ")", "buy"))
        if idea.kind == "vendor" then
          r.sub2:SetText(Paint("Sell to vendor", "muted"))
        else
          r.sub2:SetText(Paint("Relist " .. ShortMoney(idea.countAt or idea.relist), "sell"))
        end
        local note = ""
        if idea.rate then
          note = idea.rate >= 1 and ("~" .. Count(idea.rate) .. "/day") or "<1/day"
        end
        r.right2:SetText(note)
      else
        -- An idea from the price history: buy under Target Buy (as many as a
        -- day's flip, or your gold, allows), sell at the sell target.
        if fit then
          r.sub:SetText(Paint("Buy " .. Count(fit.units) .. " at " .. ShortMoney(idea.buy), "buy"))
        else
          r.sub:SetText(Paint("Buy at " .. ShortMoney(idea.buy), "buy"))
        end
        r.sub2:SetText(Paint("Sell at " .. ShortMoney(idea.sell), "sell"))
        r.right2:SetText("~" .. Count(idea.sold) .. "/day")
      end
      -- What it can make in a day (a collector flip), else the profit on what you can buy
      -- (the profit each for an unsized history idea).
      r.right:SetText(Paint("+" .. ShortMoney(idea.dayProfit or (fit and fit.profit) or idea.profit), "pos"))
      SetStar(r.star.tex, ns.IsWatched(idea.id))
      r:Show()
    else
      r.itemID, r.idea = nil, nil
      r:Hide()
    end
  end
  if #ideas == 0 then
    local why = (not HasData()) and "nodata" or self.flipWhy
    self.flipMsg:SetText(FLIP_MESSAGES[why] or FLIP_MESSAGES.none)
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
      -- Edge colour: blue when the price is high now (it usually fell back).
      -- None when it is low: low prices often kept falling, so that is no buy.
      local rec = it and Advice(id, tf)
      local v = rec and rec.history and rec.advice ~= false and rec.verdict
      if v == ns.CALL_HIGH then
        local c = Col("sell")
        r.accent:SetColorTexture(c[1], c[2], c[3], 0.8)
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
    if type(ns.AgeState) == "function" and ns.AgeState(age) == "stale" then txt = Paint(txt, "neg") end
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
    -- The whole link: a version with its own prices is watched as that version.
    SB:AddFromText(text)
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
  -- Your gold changed: the ideas are sized to it.
  PLAYER_MONEY = function() SB.flipsDirty = true RefreshSoon() end,
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
-- Slash commands: /gl watch, /gl unwatch, /gl watchlist, /gl sidebar
---------------------------------------------------------------------------
local baseSlash = ns.HandleSlash
local SLASH_FLIPS = 10   -- /gl flips prints this many

-- One chat line for a flip idea, with what your gold buys of it when the
-- ideas are sized to your gold.
local function FlipLine(x)
  local fit = type(x.fit) == "table" and x.fit or nil
  if x.kind == "craft" and type(x.line) == "string" then return x.line end   -- Crafts.lua words it
  if x.data then
    local n = Count(x.units)
    if fit and fit.limited then n = Count(fit.units) .. " of " .. Count(x.units) end
    local head = ItemName(x.id) .. ": buy up to " .. Money(x.buy) .. " (" .. n .. "), "
    if x.kind == "vendor" then
      return head .. "sell to vendor, +" .. Money(fit and fit.profit or x.profit)
    end
    local rate = Num(x.rate) or 0
    return head .. "relist " .. Money(x.countAt or x.relist) .. ", +" .. Money(x.dayProfit or 0) .. " in a day ("
      .. (rate >= 1 and ("sells ~" .. Count(rate) .. "/day") or "sells under 1 a day") .. ")"
  end
  if fit then
    return ItemName(x.id) .. ": buy " .. Count(fit.units) .. " at " .. Money(x.buy) .. ", sell " .. Money(x.sell)
      .. ", +" .. Money(fit.profit) .. " (+" .. Money(x.profit) .. " each), ~" .. Count(x.sold) .. "/day"
  end
  return ItemName(x.id) .. ": buy " .. Money(x.buy) .. ", sell " .. Money(x.sell)
    .. ", +" .. Money(x.profit) .. " each, ~" .. Count(x.sold) .. "/day"
end
ns.FlipLine = FlipLine

local function WatchCommand(rest, remove)
  if rest == "" then
    local id = SB:CurrentItem()
    if not id then
      Print("say which item: /gl " .. (remove and "unwatch" or "watch") .. " Runecloth (or shift-click it).")
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
      Print("your watchlist is empty. /gl watch Runecloth adds one.")
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
    local gold = ns.PlayerGold()
    local ideas, why = ns.FlipIdeas(SLASH_FLIPS, gold or false)
    if #ideas == 0 then
      if why == "nodata" then
        Print("no price data yet. Type /gl to see how to get it.")
      elseif why == "history" then
        Print(FLIP_MESSAGES.history)
      elseif why == "stale" then
        Print("no flip ideas from prices this old. " .. (type(ns.StaleText) == "function" and (ns.StaleText() or "") or "") .. ".")
      elseif why == "nosales" then
        Print("still collecting sales history: flip ideas show up after a few hourly price updates.")
      elseif why == "gold" then
        Print("not enough gold on this character for the current flip ideas (you have " .. Money(gold or 0) .. ").")
      else
        Print("no good flips right now.")
      end
      return
    end
    if gold then
      Print("flip ideas sized to your gold (" .. Money(gold) .. "), most profit within a day first, after the 5% auction cut:")
    else
      Print("flip ideas, most profit within a day first (after the 5% auction cut):")
    end
    for i, x in ipairs(ideas) do
      Print("  " .. i .. ". " .. FlipLine(x))
    end
  else
    if cmd == "help" or cmd == "?" then
      if type(baseSlash) == "function" then baseSlash(msg) end
      Print("/gl watch Runecloth : add an item to the auction house watchlist (no name = the item you are looking at)")
      Print("/gl unwatch Runecloth : take it off")
      Print("/gl watchlist : list it (/gl watchlist reset brings back the starter list)")
      Print("/gl flips : print the current flip ideas, sized to the gold on this character")
      Print("/gl sidebar : show or tuck away the auction house sidebar")
      return
    end
    if type(baseSlash) == "function" then return baseSlash(msg) end
  end
end

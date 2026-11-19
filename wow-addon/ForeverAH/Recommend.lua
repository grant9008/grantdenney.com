local ADDON_NAME, ns = ...
-- Forever AH: price advice. Pure Lua: it only reads prices through ns.GetItem
-- and ns.GetSeries, so it can be tested outside the game.
--
-- For a timeframe window it works out:
--   buy     a good price to buy at (20th percentile of the cheapest prices)
--   sell    a good price to sell at (70th percentile of the market prices;
--           after a drop that lasts, the highest market price since, see
--           settledHigh)
--   listAt  what to post at right now (undercut the cheapest by 1 copper when
--           that still beats the 5% AH cut, otherwise the sell target, and
--           never under a normal-priced cheapest listing)
--   verdict where the cheapest price sits in the window's range of cheapest
--           prices (a range under 10% of the typical price counts as flat;
--           no Buy after a drop that lasts, the range is the old one)
-- When the chosen window has fewer than 4 prices, the next longer one is used.
-- "Sold" numbers are estimates, so this is advice, not a promise.
--
-- No advice at all (advice = false: buy, sell, listAt and margin are 0, the
-- verdict is Hold) when the price cannot be trusted yet:
--   * too little history (fewer than 4 prices even in the longest window)
--   * only 1 or 2 separate listings behind today's prices
--   * a market price over 200x what a vendor pays, with fewer than 10 units
--     listed (one player's silly listing). With 10 or more units listed it is
--     a real price: enchanting dust vendors for 1 copper and sells for silver.
-- Even then postAt says what a player would post at (1 copper under the
-- cheapest listing, or today's List At rule when only the history is short)
-- and vendorBetter still warns when a vendor pays more than that.
--
-- ns.SellPrices (below) gives the sell screen's two buttons and the tooltip's
-- Sell now (1 copper under the cheapest listing) and Suggested price (the sell
-- target, never under a normal-priced cheapest listing). ns.SellVerdict (at
-- the end) gives the tooltip's call: auction it or vendor it, and by how much.

ns = ns or {}

local floor, min, max = math.floor, math.min, math.max
local sort = table.sort

local AH_CUT = 0.95    -- the auction house keeps 5% of the sale
local MIN_POINTS = 4   -- fewer prices than this in a window is not enough history
local MIN_RANGE = 0.10 -- a window range under 10% of the median price is a flat market
local FAR = 2          -- a cheapest listing over 2x the window's 90th percentile market is not normal
local MIN_AUCTIONS = 3 -- fewer separate listings than this is too thin to trust
local VENDOR_FAR = 200 -- a market price over 200x the vendor price is not trusted
local VENDOR_FAR_UNITS = 10 -- ... when fewer units than this are listed
local SETTLED_HOURS = 6 -- the last quarter of an hourly window, at least 6 hours ...
local SETTLED_PCT = 80  -- ... all at or under 80% of the sell target: the price dropped and stayed down
local CHAIN = { "1D", "5D", "1W", "1M", "1Y" }

-- Percentile of an ascending sorted list, interpolated between the two
-- nearest values (same method as PocketGE), rounded to whole copper.
local function percentile(sorted, p)
  local n = #sorted
  if n == 0 then return nil end
  if n == 1 then return sorted[1] end
  local idx = p * (n - 1) + 1
  local lo = floor(idx)
  if lo < 1 then lo = 1 end
  if lo >= n then return sorted[n] end
  local w = idx - lo
  local v = sorted[lo] * (1 - w) + sorted[lo + 1] * w
  return floor(v + 0.5)
end
ns.Percentile = percentile

local function verdictFor(score)
  if score >= 60 then return "Strong Buy" end
  if score >= 25 then return "Buy" end
  if score > -25 then return "Hold" end
  if score > -60 then return "Sell" end
  return "Strong Sell"
end
ns.VerdictFor = verdictFor

-- Sorted non-null cheapest and market prices of a series window.
local function collect(s)
  local cheaps, mkts = {}, {}
  if s then
    for i = 1, s.n do
      local c = s.cheap[i]
      if c then cheaps[#cheaps + 1] = c end
      local m = s.market[i]
      if m then mkts[#mkts + 1] = m end
    end
  end
  sort(cheaps)
  sort(mkts)
  return cheaps, mkts
end

local function enough(cheaps, mkts)
  return #cheaps >= MIN_POINTS and #mkts >= MIN_POINTS
end

local function moneyText(c)
  if type(ns.MoneyText) == "function" then return ns.MoneyText(c) end
  return tostring(c) .. "c"
end

-- Whether an item's price may be one player's guess: thin (only 1 or 2
-- separate listings), vendorFar (a few units far above what a vendor pays).
-- Recommend gives no advice for those; flip ideas warn about them.
local function priceDoubt(item)
  if type(item) ~= "table" then return false, false end
  local auctions = tonumber(item.auctions)
  local vendor = tonumber(item.vendor) or 0
  local mktNow = tonumber(item.market) or 0
  local thin = auctions ~= nil and auctions >= 1 and auctions < MIN_AUCTIONS
  local far = vendor > 0 and mktNow > VENDOR_FAR * vendor and (tonumber(item.listed) or 0) < VENDOR_FAR_UNITS
  return thin, far
end

-- After a drop that lasts, the old high hours stay in the window and its
-- 70th percentile no longer sells. The drop has to last a quarter of the
-- window, at least 6 hours (1D: 6, a full 5D: 30, a full 1W: 42), so a dip
-- of a few hours in a patient window stays a dip to buy. When the window's
-- last that many hourly market prices (2 in 3 of them at least, the newest
-- one too: with nothing listed no cheaper listing holds an old price back)
-- are all at or under 80% of target, returns the highest of them (the new
-- sell target) and the hours, else nil. Hourly windows only: the daily 1M
-- and 1Y never cap. Whole numbers only.
local function settledHigh(s, target)
  if not s or not target or target <= 0 or (tonumber(s.step) or 0) >= 86400 then return nil end
  local n = s.n or 0
  local last = s.market[n]
  if not last or last <= 0 then return nil end
  local hours = max(SETTLED_HOURS, floor((n + 3) / 4))
  local high, cnt = 0, 0
  for i = max(1, n - hours + 1), n do
    local m = s.market[i]
    if m and m > 0 then
      cnt = cnt + 1
      if m > high then high = m end
    end
  end
  if cnt * 3 >= hours * 2 and high * 100 <= target * SETTLED_PCT then return high, hours end
  return nil
end

-- true when the price of an item (or version) may be one player's guess.
function ns.PriceDoubt(itemID)
  if type(ns.GetItem) ~= "function" then return false end
  local thin, far = priceDoubt(ns.GetItem(itemID))
  return thin or far
end

-- ns.Recommend(itemID, tf [, liveCheapest])
-- liveCheapest: the cheapest listing the auction house is showing right now
-- (copper), when the addon has seen it. It replaces the snapshot's cheapest.
function ns.Recommend(itemID, tf, liveCheapest)
  if type(ns.GetItem) ~= "function" then return nil end
  local item = ns.GetItem(itemID)
  if not item then return nil end
  if type(tf) == "string" then tf = tf:upper() end
  local asked = tf
  local valid = false
  for _, k in ipairs(CHAIN) do if k == asked then valid = true end end
  if not valid then asked = "1D" end

  -- The window: the asked one, or the first longer one with enough prices.
  local s, cheaps, mkts, used
  if type(ns.GetSeries) == "function" then
    local started = false
    for _, key in ipairs(CHAIN) do
      if key == asked then started = true end
      if started then
        local ws = ns.GetSeries(itemID, key)
        local c, m = collect(ws)
        if not s or (#c + #m) > (#cheaps + #mkts) then
          s, cheaps, mkts, used = ws, c, m, key
        end
        if enough(cheaps, mkts) then break end
      end
    end
  end
  if not cheaps then cheaps, mkts = {}, {} end
  used = used or asked
  local history = enough(cheaps, mkts)

  local live = tonumber(liveCheapest)
  if live and live <= 0 then live = nil end
  local cheapNow = live or tonumber(item.cheapest) or 0
  local mktNow = tonumber(item.market) or 0
  local sold24 = tonumber(item.sold24) or 0
  local vendor = tonumber(item.vendor) or 0
  -- Sales per day (scaled up while only part of a day is measured; nil
  -- while nothing is measured yet).
  local perDay, soldEarly = sold24, false
  if type(ns.SoldPerDay) == "function" then perDay, soldEarly = ns.SoldPerDay(item) end
  -- Prices behind only 1 or 2 listings, or a few units far above what a
  -- vendor pays, may be one player's guess: no advice for those.
  local auctions = tonumber(item.auctions)
  local thinListings, vendorFar = priceDoubt(item)
  local advice = history and not thinListings and not vendorFar

  -- Window low and high of the cheapest prices.
  local lo, hi = cheaps[1], cheaps[#cheaps]
  if not lo then
    if cheapNow > 0 then lo, hi = cheapNow, cheapNow elseif mktNow > 0 then lo, hi = mktNow, mktNow end
  end
  lo, hi = lo or 0, hi or 0

  local p90 = percentile(mkts, 0.90)
  local far = history and p90 ~= nil and p90 > 0 and cheapNow > FAR * p90

  -- Buy target.
  local buy
  if #cheaps >= MIN_POINTS then
    buy = percentile(cheaps, 0.20)
  else
    buy = cheapNow
  end
  if buy <= 0 then
    -- Nothing listed right now and too little history: use what there is.
    buy = percentile(cheaps, 0.20) or mktNow
  end
  if mktNow > 0 and buy > mktNow then buy = mktNow end

  -- Sell target. A market value far above recent prices (one silly listing)
  -- is not used as the floor.
  local sell = percentile(mkts, 0.70) or mktNow
  -- After a drop that lasts the old highs no longer sell: the target is the
  -- highest market price since (settledHours: how many hours that is).
  local settled, settledHours
  if history then settled, settledHours = settledHigh(s, sell) end
  if settled then sell = settled end
  local mktFar = history and p90 ~= nil and p90 > 0 and mktNow > FAR * p90
  if sell < mktNow and not mktFar then sell = mktNow end
  if sell <= 0 then sell = cheapNow end

  -- What to post at right now.
  local mkt40 = percentile(mkts, 0.40) or mktNow
  local listAt
  if cheapNow > 0 and not far and cheapNow >= mkt40 and floor(cheapNow * AH_CUT) > buy then
    listAt = cheapNow - 1
  else
    listAt = sell
  end
  -- Never post under a normal-priced cheapest listing: it would sell for less than needed.
  if cheapNow > 1 and not far and listAt < cheapNow - 1 then listAt = cheapNow - 1 end

  -- The same rule from today's prices alone, kept even when there is no
  -- advice: with only the history short, today's market price is still
  -- fine, so a post at today's List At rule is never far under it.
  local followFloor = listAt

  -- Prices that cannot be trusted yet give no targets at all.
  if not advice then buy, sell, listAt = 0, 0, 0 end

  -- What a player would post at: the List At price. With no advice it is
  -- 1 copper under the cheapest listing,
  -- or today's List At rule when only the history is short.
  local postAt = listAt
  if not advice then
    postAt = (cheapNow > 1) and (cheapNow - 1) or 0
    if not thinListings and not vendorFar and followFloor > postAt then postAt = followFloor end
  end

  local margin = floor(sell * AH_CUT) - buy
  local marginPct = (buy > 0) and (margin / buy) or 0

  -- Where the cheapest price sits in the window's range of cheapest prices
  -- (0 = at the low, 1 = at the high). A narrow range is widened to 10% of the
  -- median around its middle, so a small wiggle reads as Hold, not as an extreme.
  -- With nothing listed, or no advice, it counts as the middle.
  local pos = 0.5
  if cheapNow > 0 and advice then
    local plo, phi = lo, hi
    local median = percentile(cheaps, 0.50) or cheapNow
    local want = MIN_RANGE * median
    if phi - plo < want then
      local mid = (plo + phi) / 2
      plo, phi = mid - want / 2, mid + want / 2
    end
    if phi > plo then
      pos = (cheapNow - plo) / (phi - plo)
      if pos < 0 then pos = 0 elseif pos > 1 then pos = 1 end
    end
  end

  local score = (0.5 - pos) * 200
  -- After a drop that lasts, the window's range is the old one: a cheapest
  -- low in it is no Buy. High in it, it is even higher now: Sell stands.
  if margin <= 0 then
    score = max(-24, min(24, score))
  elseif settled then
    score = min(24, score)
  end
  local verdict = verdictFor(score)

  -- A vendor pays more than the auction house would after its 5% cut, at
  -- the price a player would post at (also when there is no advice).
  local vendorBetter = vendor > 0 and postAt > 0 and floor(postAt * AH_CUT) <= vendor

  local notes = {}
  -- While sales are still being measured there is no sales number to judge.
  if perDay ~= nil and perDay < 3 then notes[#notes + 1] = "Thin market" end
  if cheapNow == 0 then notes[#notes + 1] = "No listings right now" end
  if not history then
    notes[#notes + 1] = "Not enough price history yet"
  elseif used ~= asked then
    notes[#notes + 1] = "Too few prices in " .. asked .. ": advice uses " .. used
  end
  if thinListings then
    notes[#notes + 1] = "Only " .. auctions .. (auctions == 1 and " listing" or " listings") .. ": price may not be reliable"
  end
  if vendorFar then notes[#notes + 1] = "Listed far above what a vendor pays: price may not be reliable" end
  if far then notes[#notes + 1] = "Cheapest now is far above recent prices" end
  -- Only when List At is that target (a live listing can lift it).
  if settled and advice and listAt == sell then
    notes[#notes + 1] = "Price fell and stayed down: the sell target uses the last " .. settledHours .. " hours"
  end
  -- The buy target comes from the old range too: no "buy it" after a drop that lasts.
  if history and cheapNow > 0 and cheapNow < buy and margin > 0 and not settled then
    notes[#notes + 1] = "Cheapest is below the buy target: buy it"
  end
  if margin <= 0 and buy > 0 and sell > 0 then notes[#notes + 1] = "Spread does not beat the 5% AH cut" end
  if vendorBetter then notes[#notes + 1] = "A vendor pays more (" .. moneyText(vendor) .. ")" end

  return {
    buy = buy,
    sell = sell,
    listAt = listAt,
    postAt = postAt,               -- what a player would post at (0 = nothing to undercut)
    followFloor = followFloor,     -- the List At rule from today's prices, even with no advice
    margin = margin,
    marginPct = marginPct,
    verdict = verdict,
    score = score,
    pos = pos,
    lo = lo,
    hi = hi,
    cheapestNow = cheapNow,
    marketNow = mktNow,
    sold24 = sold24,
    soldPerDay = perDay,           -- nil while sales are still being measured
    salesMeasuring = perDay == nil,
    soldEarly = soldEarly and true or false,
    notes = notes,
    tf = used,
    askedTF = asked,
    points = s and s.n or 0,
    history = history,
    advice = advice,
    auctions = auctions,
    thinListings = thinListings,
    vendorFar = vendorFar,
    far = far,
    settled = (settled and advice) and true or false, -- the sell target is the high since a drop that lasted
    settledHours = (settled and advice) and settledHours or nil, -- ... over this many hours
    live = live ~= nil,
    vendorBetter = vendorBetter,
  }
end

-- ns.SellPrices(itemID, tf [, liveCheapest, own])
-- The two prices the sell screen offers (and the tooltip shows), in copper:
--   now   Sell now: 1 copper under the cheapest listing (the live one when the
--         auction house showed it, else the snapshot's). When that live
--         listing is your own it is matched, not undercut. nil when nothing
--         is listed (nowWhy "none") or when a few units are listed far above
--         what a vendor pays, so the cheapest is one player's silly price
--         (nowWhy "unreliable").
--   best  Suggested price: the sell target from the price history (Recommend's
--         sell), never under a normal-priced cheapest listing. nil without
--         enough history (bestWhy "history") or when the price cannot be
--         trusted (bestWhy "unreliable").
-- Also: rec (the advice), live (a live listing was used), own, cheapest, and
-- nowVendor / bestVendor (true when a vendor pays more than that price after
-- the 5% cut).
function ns.SellPrices(itemID, tf, liveCheapest, own)
  local rec = ns.Recommend(itemID, tf, liveCheapest)
  if not rec then return nil end
  local item = ns.GetItem(itemID)
  local vendor = tonumber(item and item.vendor) or 0
  local cheap = tonumber(rec.cheapestNow) or 0
  local mine = rec.live and own == true
  local out = { rec = rec, live = rec.live, own = mine, cheapest = cheap }
  if rec.vendorFar then
    out.nowWhy = "unreliable"
  elseif cheap > 1 then
    out.now = mine and cheap or (cheap - 1)
  else
    out.nowWhy = "none"
  end
  if not rec.history then
    out.bestWhy = "history"
  elseif rec.advice == false then
    out.bestWhy = "unreliable"
  else
    local b = tonumber(rec.sell) or 0
    if cheap > 1 and not rec.far and b < cheap - 1 then b = cheap - 1 end
    if mine and b == cheap - 1 then b = cheap end
    if b > 0 then out.best = b else out.bestWhy = "history" end
  end
  out.nowVendor = vendor > 0 and out.now ~= nil and floor(out.now * AH_CUT) <= vendor
  out.bestVendor = vendor > 0 and out.best ~= nil and floor(out.best * AH_CUT) <= vendor
  return out
end

---------------------------------------------------------------------------
-- Sell verdict: the item tooltip's "Auction it" / "Vendor it" line
---------------------------------------------------------------------------
-- All in whole numbers: A = what the AH nets per item after the 5% cut and
-- B = what a vendor pays, both in 1/100 copper.
local AUCTION_PCT = 110  -- "Auction it": the AH nets at least 110% of B ...
local SLOW_PCT = 200     -- ... (200% when none sold in 6+ measured hours: an unsold listing loses its deposit) ...
local MIN_GAIN = 200     -- ... and at least 2 copper more per item
local VENDOR_PCT = 101   -- "Vendor it": the AH nets under 101% of B (a vendor is instant and needs no deposit)
local OLD_FAR = 200      -- nothing listed now: a Suggested price over 200x the vendor price is not trusted
local SLOW_HOURS = 6     -- sales measured for fewer hours than this: how fast it sells is unknown

-- ns.SellVerdictPrice(item, P): the AH price per item (copper, before the 5%
-- cut) the verdict judges, or nil. item = ns.GetItem(id), P = ns.SellPrices(id, ...).
--   With listings: the List At price (postAt: 1 copper under a normal
--   cheapest listing, a lone cheap listing under the window's usual prices
--   is a dip and ignored), never over today's AH value, or over Sell now
--   when a live listing sits above that value.
--   Nothing listed now: the Suggested price (nil without one, or when it is
--   over 200x what a vendor pays).
--   nil when a few units sit far above the vendor price (vendorFar), and for
--   1 copper or less. It is always a price the tooltip shows.
function ns.SellVerdictPrice(item, P)
  if type(item) ~= "table" or type(P) ~= "table" or type(P.rec) ~= "table" or P.rec.vendorFar then return nil end
  local V, M = tonumber(item.vendor) or 0, tonumber(item.market) or 0
  local U
  if M > 0 then
    U = tonumber(P.rec.postAt) or 0
    local cap = max(M, tonumber(P.now) or 0)
    if U > cap then U = cap end
  else
    U = tonumber(P.best)
    if not U or (V > 0 and U > OLD_FAR * V) then return nil end
  end
  if U <= 1 then return nil end
  return U
end

-- How much more hi pays than lo (whole numbers, hi >= lo > 0), rounded down
-- so it never claims more than it is: "+26% vs vendor", "same as AH" (under
-- 1%), "2.8x AH" (one decimal from 2x), "12x vendor", "over 100x vendor".
function ns.BetterText(hi, lo, what)
  if hi > lo * 100 then return "over 100x " .. what end
  if hi >= lo * 10 then return floor(hi / lo) .. "x " .. what end
  if hi >= lo * 2 then
    local t = floor(hi * 10 / lo)
    return ((t % 10 == 0) and tostring(floor(t / 10)) or (floor(t / 10) .. "." .. (t % 10))) .. "x " .. what
  end
  local p = floor((hi - lo) * 100 / lo)
  if p < 1 then return "same as " .. what end
  return "+" .. p .. "% vs " .. what
end

-- A nets at least pct% of B and at least 2 copper more.
local function wins(A, B, pct) return A * 100 >= B * pct and A - B >= MIN_GAIN end

-- ns.SellVerdict(item, P): the tooltip's call for one item, or nil (no usable
-- AH price). { kind, unit, left, right, lc, rc } with lc / rc ns.COLORS keys:
--   "auction"  "Auction it"     "+26% vs vendor", "6.1x vendor", "can't vendor" (no vendor price)
--   "vendor"   "Vendor it"      "+5% vs AH", "2.8x AH" or "same as AH" (within 1%)
--   "close"    "Close call"     "AH +9%", or "tiny gain" (10% or more, but under 2 copper more each)
--   "maybe"    "Maybe auction"  "few listed", "none listed now", "sells slowly", "price jumped"
-- unit is the price judged (ns.SellVerdictPrice); "maybe" has none. The call is
-- per item: a stack's size and Shift never change it.
function ns.SellVerdict(item, P)
  if type(item) ~= "table" or type(P) ~= "table" or type(P.rec) ~= "table" then return nil end
  local rec, U = P.rec, ns.SellVerdictPrice(item, P)
  if not U then return nil end
  local V, M = tonumber(item.vendor) or 0, tonumber(item.market) or 0
  -- 1 or 2 listings, or none right now: an AH price that looks better is a maybe.
  local unsure = (rec.thinListings and "few listed") or (M <= 0 and "none listed now") or nil
  local function maybe(why) return { kind = "maybe", left = "Maybe auction", right = why, lc = "buy", rc = "muted" } end
  if V <= 0 then
    if unsure then return maybe(unsure) end
    return { kind = "auction", unit = U, left = "Auction it", right = "can't vendor", lc = "pos", rc = "muted" }
  end
  local A, B = U * 95, V * 100
  if wins(A, B, AUCTION_PCT) then
    if unsure then return maybe(unsure) end
    -- None sold in the hours measured (6 or more): the AH must pay twice what a vendor does.
    local per = type(ns.SoldPerDay) == "function" and ns.SoldPerDay(item) or nil
    local hours = type(ns.SalesHours) == "function" and ns.SalesHours() or 24
    if per ~= nil and hours >= SLOW_HOURS and per < 1 and not wins(A, B, SLOW_PCT) then return maybe("sells slowly") end
    return { kind = "auction", unit = U, left = "Auction it", right = ns.BetterText(A, B, "vendor"), lc = "pos", rc = "pos" }
  end
  -- The history price does not win, but today's Sell now (far above it) would.
  local now = tonumber(P.now)
  if rec.far and now and wins(now * 95, B, AUCTION_PCT) then return maybe("price jumped") end
  if A * 100 < B * VENDOR_PCT then
    return { kind = "vendor", unit = U, left = "Vendor it", right = (B > A) and ns.BetterText(B, A, "AH") or "same as AH",
             lc = "neg", rc = "neg" }
  end
  local p = floor((A - B) * 100 / B)
  return { kind = "close", unit = U, left = "Close call", right = (p < 10) and ("AH +" .. p .. "%") or "tiny gain",
           lc = "muted", rc = "muted" }
end

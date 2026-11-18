local ADDON_NAME, ns = ...
-- Forever AH: price advice. Pure Lua: it only reads prices through ns.GetItem
-- and ns.GetSeries, so it can be tested outside the game.
--
-- For a timeframe window it works out:
--   buy     a good price to buy at (20th percentile of the cheapest prices)
--   sell    a good price to sell at (70th percentile of the market prices)
--   listAt  what to post at right now (undercut the cheapest by 1 copper when
--           that still beats the 5% AH cut, otherwise the sell target, and
--           never under a normal-priced cheapest listing)
--   verdict where the cheapest price sits in the window's range of cheapest
--           prices (a range under 10% of the typical price counts as flat)
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
-- Even then postAt says what the Use List At button would fill (1 copper
-- under the cheapest listing, or today's List At rule when only the history
-- is short) and vendorBetter still warns when a vendor pays more than that.

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
  local thinListings = auctions ~= nil and auctions >= 1 and auctions < MIN_AUCTIONS
  local listed = tonumber(item.listed) or 0
  local vendorFar = vendor > 0 and mktNow > VENDOR_FAR * vendor and listed < VENDOR_FAR_UNITS
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
  -- fine, so the Use List At button never posts far under it (AH.lua).
  local followFloor = listAt

  -- Prices that cannot be trusted yet give no targets at all.
  if not advice then buy, sell, listAt = 0, 0, 0 end

  -- What a player would post at: the List At price. With no advice it is
  -- what the Use List At button fills: 1 copper under the cheapest listing,
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
  if margin <= 0 then score = max(-24, min(24, score)) end
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
  if history and cheapNow > 0 and cheapNow < buy and margin > 0 then
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
    live = live ~= nil,
    vendorBetter = vendorBetter,
  }
end

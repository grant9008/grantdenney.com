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

ns = ns or {}

local floor, min, max = math.floor, math.min, math.max
local sort = table.sort

local AH_CUT = 0.95    -- the auction house keeps 5% of the sale
local MIN_POINTS = 4   -- fewer prices than this in a window is not enough history
local MIN_RANGE = 0.10 -- a window range under 10% of the median price is a flat market
local FAR = 2          -- a cheapest listing over 2x the window's 90th percentile market is not normal
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

  local margin = floor(sell * AH_CUT) - buy
  local marginPct = (buy > 0) and (margin / buy) or 0

  -- Where the cheapest price sits in the window's range of cheapest prices
  -- (0 = at the low, 1 = at the high). A narrow range is widened to 10% of the
  -- median around its middle, so a small wiggle reads as Hold, not as an extreme.
  -- With nothing listed, or too little history, it counts as the middle.
  local pos = 0.5
  if cheapNow > 0 and history then
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

  local vendorBetter = vendor > 0 and listAt > 0 and floor(listAt * AH_CUT) <= vendor

  local notes = {}
  if sold24 < 3 then notes[#notes + 1] = "Thin market" end
  if cheapNow == 0 then notes[#notes + 1] = "No listings right now" end
  if not history then
    notes[#notes + 1] = "Not enough price history yet"
  elseif used ~= asked then
    notes[#notes + 1] = "Too few prices in " .. asked .. ": advice uses " .. used
  end
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
    notes = notes,
    tf = used,
    askedTF = asked,
    points = s and s.n or 0,
    history = history,
    far = far,
    live = live ~= nil,
    vendorBetter = vendorBetter,
  }
end

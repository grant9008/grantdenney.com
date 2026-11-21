local ADDON_NAME, ns = ...
-- Goblin Ledger: price advice. Pure Lua: it only reads prices through ns.GetItem
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
--           never low after a drop that lasts, the range is the old one):
--           "High now: usually falls back", "Hold" or "Low now: still
--           falling". Not a buy call: after a low call prices fell further.
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
-- ns.SellPrices (below) gives the one price to sell at, for the tooltip, the
-- sell buttons, the chart's Sell at box and the bag panel alike: Sell now (1
-- copper under the cheapest listing), unless that is under 70% of the usual
-- price, then the usual price. ns.SellVerdict (at the end) gives the
-- tooltip's call at that price: auction it or vendor it, and by how much.

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

-- The chart's call. Backtest of the next 24 hours' middle market price after
-- each call (19 Nov data): after a high call it fell 11 to 16%; after a low
-- call it fell too (1 to 7%), so a low price is no reason to buy. Hold
-- stayed about flat.
local CALL_LOW, CALL_HIGH = "Low now: still falling", "High now: usually falls back"
ns.CALL_LOW, ns.CALL_HIGH = CALL_LOW, CALL_HIGH
local function verdictFor(score)
  if score >= 25 then return CALL_LOW end
  if score > -25 then return "Hold" end
  return CALL_HIGH
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

-- The usual price: the middle of the item's last 24 hourly market prices (a
-- slow seller's go back further, up to the 7 days of hourly prices Data.lua
-- has), at least 4 of them, else nil. Hours with nothing listed have no
-- price; the price file repeats the last prices over a gap of up to 6 hours
-- in its own updates, and those repeats count too. Backtest: within 5% of
-- the 24 real hours' median for 81-88% of item-hours, and the sell price
-- below (rule G) came out the same for 96-98% of them.
local USUAL_N = 24
local DUMP_PCT = 70   -- Sell now under 70% of the usual price: sell at the usual price instead
local function usualPrice(itemID)
  local s = type(ns.GetSeries) == "function" and ns.GetSeries(itemID, "1W") or nil
  if not s then return nil end
  local vals = {}
  for i = s.n, 1, -1 do
    local m = s.market[i]
    if m and m > 0 then
      vals[#vals + 1] = m
      if #vals >= USUAL_N then break end
    end
  end
  if #vals < MIN_POINTS then return nil end
  sort(vals)
  return percentile(vals, 0.5)
end
ns.UsualPrice = usualPrice

-- The 5-day average: the mean of the item's hourly market prices over the
-- last 5 days (the 5D chart's 120 hours), whole copper. Hours with nothing
-- listed have no price and are left out; the price file's repeats over a gap
-- of up to 6 hours count, as for the usual price. A mean, not a middle: one
-- dear day moves it, which is what an average over days should show next to
-- the usual price. Only when the prices really span 5 days, else nil (an
-- item first listed 2 days ago gets no line, never a "5-day" average of 2
-- days): at least 24 hours with a price (a few scattered listings are not an
-- average) and the oldest of them at least 108 hours (4.5 days) before the
-- window's newest hour. Half a day of slack, as nothing may have been listed
-- in the window's first hours.
local AVG5_HOURS = 24   -- hours with a price, at least
local AVG5_SPAN = 108   -- hours from the oldest of them to the window's newest hour, at least
function ns.FiveDayAverage(itemID)
  local s = type(ns.GetSeries) == "function" and ns.GetSeries(itemID, "5D") or nil
  if not s then return nil end
  local n = s.n or 0
  local sum, cnt, first = 0, 0, nil
  for i = 1, n do
    local m = s.market[i]
    if m and m > 0 then
      sum, cnt = sum + m, cnt + 1
      first = first or i
    end
  end
  if cnt < AVG5_HOURS or n - first < AVG5_SPAN then return nil end
  return floor(sum / cnt + 0.5)
end

-- Rule G, the one rule for the price to sell at (Recommend and SellPrices
-- both use it): Sell now (now; nil when nothing is listed), unless it is
-- under 70% of the usual price, then the usual price. Returns that price
-- (nil: neither) and, when it is the usual price over a listing, how many %
-- under it Sell now is.
local function ruleG(now, usual)
  if now and now > 0 then
    if usual and now * 100 < usual * DUMP_PCT then return usual, floor((usual - now) * 100 / usual) end
    return now, nil
  end
  return usual, nil
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
  -- low in it is not low. High in it, it is even higher now: high stands.
  if margin <= 0 then
    score = max(-24, min(24, score))
  elseif settled then
    score = min(24, score)
  end
  local verdict = verdictFor(score)

  -- The one price to sell at (rule G), the same in every timeframe: Sell
  -- now (1 copper under the cheapest listing), unless that is under 70% of
  -- the usual price (someone is dumping), then the usual price. Nothing
  -- listed: the usual price. 0 when a few units sit far above what a vendor
  -- pays (one player's price). Backtest, 18-19 Nov: it sold within a day
  -- 41-86% of the time and kept the most value of the rules tried
  -- (0.80-0.89 of the AH value, unsold ones relisted), and equals Sell now
  -- in 85-92% of item-hours. ns.SellPrices also matches your own listing.
  local usual = usualPrice(itemID)
  local sellAt = 0
  if not vendorFar then sellAt = ruleG(cheapNow > 1 and (cheapNow - 1) or nil, usual) or 0 end

  -- A vendor pays more than the auction house would after its 5% cut, at
  -- that price (also when there is no advice).
  local vendorBetter = vendor > 0 and sellAt > 0 and floor(sellAt * AH_CUT) <= vendor

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
  -- Only when List At is that target (a live listing can lift it). Why the call is not low.
  if settled and advice and listAt == sell then
    notes[#notes + 1] = "Price fell and stayed down for the last " .. settledHours .. " hours"
  end
  -- The buy target comes from the old range too: no such note after a drop
  -- that lasts. A fact, not a call to buy: low prices often fell further.
  if history and cheapNow > 0 and cheapNow < buy and margin > 0 and not settled then
    notes[#notes + 1] = "Cheapest is under Target Buy"
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
    usual = usual,                 -- the usual price (nil: too few prices)
    sellAt = sellAt,               -- the one price to sell at (0: none)
  }
end

-- The prices to sell at from what they rest on: the cheapest listing now
-- (live: from the open auction house), own (that live one is yours), the
-- usual price and vendorFar (Recommend's price doubt). SellPrices and
-- SellAt both give these.
local function sellOut(item, cheap, live, own, usual, vendorFar)
  local vendor = tonumber(item and item.vendor) or 0
  cheap = tonumber(cheap) or 0
  local mine = live and own == true
  usual = tonumber(usual)
  local out = { live = live, own = mine, cheapest = cheap, usual = usual }
  -- raw: Sell now before the price doubt (nil with nothing listed). Only for
  -- Crafts.lua, which counts a made item the tooltip gives no price for at
  -- it and then never claims a profit.
  if cheap > 1 then out.raw = mine and cheap or (cheap - 1) end
  if vendorFar then
    out.nowWhy, out.atWhy = "unreliable", "unreliable"
  elseif cheap > 1 then
    out.now = out.raw
    out.at, out.under = ruleG(out.now, usual)
  else
    out.nowWhy = "none"
    out.at = ruleG(nil, usual)
    if not out.at then out.atWhy = "none" end
  end
  out.nowVendor = vendor > 0 and out.now ~= nil and floor(out.now * AH_CUT) <= vendor
  out.atVendor = vendor > 0 and out.at ~= nil and floor(out.at * AH_CUT) <= vendor
  return out
end

-- ns.SellPrices(itemID [, tf, liveCheapest, own])
-- The price to sell at, the same on every surface (the tooltip, the sell
-- buttons, the chart's Sell at box, the bag panel), in copper. tf is not
-- used: the chart's timeframe changes only the chart.
--   now    Sell now: 1 copper under the cheapest listing (the live one when
--          the auction house showed it, else the snapshot's). When that live
--          listing is your own it is matched, not undercut. nil when nothing
--          is listed (nowWhy "none") or when a few units are listed far above
--          what a vendor pays, so the cheapest is one player's silly price
--          (nowWhy "unreliable").
--   usual  the usual price (ns.UsualPrice), nil with too few prices.
--   at     the price to sell at: Sell now, unless that is under 70% of the
--          usual price, then the usual price (under = how many % under it
--          Sell now is). Nothing listed: the usual price. nil (atWhy "none"
--          or "unreliable") when there is neither.
-- Also: rec (the 1D advice), live (a live listing was used), own, cheapest,
-- and nowVendor / atVendor (true when a vendor pays at least as much as that
-- price after the 5% cut).
function ns.SellPrices(itemID, tf, liveCheapest, own)
  local rec = ns.Recommend(itemID, "1D", liveCheapest)
  if not rec then return nil end
  local out = sellOut(ns.GetItem(itemID), rec.cheapestNow, rec.live, own, rec.usual, rec.vendorFar)
  out.rec = rec
  return out
end

-- ns.SellAt(itemID [, liveCheapest, own]): ns.SellPrices's prices (now, at,
-- under, usual, live, own, nowVendor, atVendor ...) without the advice
-- (no rec): the same numbers from the same rule, for many items at once
-- (Crafts.lua counts what a craft makes at `now`, the tooltip's Sell now,
-- also while someone dumps it; with nothing listed at `at`). nil without
-- the item.
function ns.SellAt(itemID, liveCheapest, own)
  if type(ns.GetItem) ~= "function" then return nil end
  local item = ns.GetItem(itemID)
  if not item then return nil end
  local live = tonumber(liveCheapest)
  if live and live <= 0 then live = nil end
  local _, vendorFar = priceDoubt(item)
  return sellOut(item, live or tonumber(item.cheapest) or 0, live ~= nil, own, usualPrice(itemID), vendorFar)
end

---------------------------------------------------------------------------
-- Sell verdict: the item tooltip's "Auction it" / "Vendor it" line
---------------------------------------------------------------------------
-- All in whole numbers: A = what the AH nets per item after the 5% cut and
-- B = what a vendor pays, both in 1/100 copper.
local VENDOR_PCT = 101   -- "Vendor it": the AH nets under 101% of B (a vendor is instant and needs no deposit)
local OLD_FAR = 200      -- nothing listed now: a usual price over 200x the vendor price is not trusted
local SLOW_HOURS = 4     -- none sold in this many measured hours or more: a slow seller (backtest: none sold
                         -- in 4-5 hours sold like one, about 40% within a day, break-even about 2x); fewer: not known
local EDGE_PCT = 5       -- "Auction it": the expected result beats vendoring by at least 5% of B ...
local TRIP_MIN = 100     -- ... and the stack gains at least 1 silver if it sells, else "not worth the trip"
-- How often a listing at that price sold within its listing time, by how
-- many sell a day (10 or more, 1 to 9, under 1), near each one's break-even
-- (backtest of 18-19 Nov, percent). Keyed by the listing hours.
local SELL_P = { [2] = { 70, 26, 13 }, [8] = { 86, 49, 26 }, [24] = { 92, 73, 39 } }
-- Someone dumping (Sell now under 70% of the usual price): the call judges
-- the usual price, and while the cheap ones were up a listing there sold far
-- less often. Measured the same way on just those item-hours: with these, no
-- price group of them called Auction it lost money on average (with the
-- chances above 4 to 9 groups did, the worst 15% under vendoring).
local DUMP_P = { [2] = { 10, 7, 3 }, [8] = { 23, 15, 6 }, [24] = { 36, 49, 18 } }

-- The deposit: per mille of the vendor price for each unit, by listing
-- time (Forever's sell tab: 2, 8 or 24 hours), and the least it takes per
-- listing. These are Classic's rates and the 1 silver players still saw on
-- 11 Nov, until the game shows its own (ns.NoteDeposit). Lost when the
-- listing does not sell.
local DEPOSIT_PM = { 50, 200, 600 }
local DEPOSIT_HOURS = { 2, 8, 24 }
local DEPOSIT_MIN = 100

local function wholeIn(v, lo, hi)
  v = tonumber(v)
  if v and v == floor(v) and v >= lo and v <= hi then return v end
  return nil
end

-- The deposit model for the listing time the player picked last: hours,
-- per mille of the vendor price, the least per listing (copper).
function ns.DepositModel()
  local S = type(ns.settings) == "table" and ns.settings or {}
  local D = type(S.deposit) == "table" and S.deposit or {}
  local d = wholeIn(D.dur, 1, 3) or 3
  local pm = type(D.rate) == "table" and wholeIn(D.rate[d], 0, 5000) or DEPOSIT_PM[d]
  local hours = type(D.hours) == "table" and wholeIn(D.hours[d], 1, 168) or DEPOSIT_HOURS[d]
  local least = wholeIn(D.min, 0, 100000) or DEPOSIT_MIN
  return hours, pm, least
end

-- A reading of the real deposit (AH.lua, while an item sits in a sell slot;
-- the game works it out itself, nothing is sent). vendor: what a vendor pays
-- for one; n: how many that listing could hold; deps[d] = { one = deposit for
-- 1, many = deposit for n } for listing time d (1 to 3); dur: the listing time
-- picked (1 to 3); hours[d]: the hours the game names for d (optional).
-- The rate comes only from a deposit of 2 silver or more (a per-listing
-- minimum cannot hide it there); a deposit for 1 above that rate is the
-- minimum, one at the rate only says the minimum is no higher (it can lower
-- the 1 silver assumed, never raise it: a 5g gear piece's 25s deposit at 2
-- hours is its rate, not a minimum).
function ns.NoteDeposit(vendor, n, deps, dur, hours)
  local S = ns.settings
  if type(S) ~= "table" then return end
  vendor, n = tonumber(vendor) or 0, tonumber(n) or 1
  if vendor <= 0 or n < 1 or type(deps) ~= "table" then return end
  local D = type(S.deposit) == "table" and S.deposit or {}
  D.rate = type(D.rate) == "table" and D.rate or {}
  D.hours = type(D.hours) == "table" and D.hours or {}
  for d = 1, 3 do
    local r = type(deps[d]) == "table" and deps[d] or {}
    local many, one = tonumber(r.many), tonumber(r.one)
    if many and many >= 200 then D.rate[d] = floor(many * 1000 / (vendor * n) + 0.5) end
    local pm = wholeIn(D.rate[d], 0, 5000)
    if one and one >= 0 and pm then
      local atRate = pm * vendor / 1000
      if one > atRate + 1 then
        D.min = one
      elseif one < (D.min or DEPOSIT_MIN) then
        D.min = floor(one)
      end
    end
    local h = type(hours) == "table" and wholeIn(hours[d], 1, 168)
    if h then D.hours[d] = h end
  end
  D.dur = wholeIn(dur, 1, 3) or D.dur
  S.deposit = D
end

-- ns.SellVerdictPrice(item, P): the AH price per item (copper, before the 5%
-- cut) the verdict judges, or nil. item = ns.GetItem(id), P = ns.SellPrices(id, ...).
-- It is the price to sell at (P.at), the one the tooltip and the buttons
-- show. nil when a few units sit far above the vendor price (vendorFar),
-- for 1 copper or less, and with nothing listed now for a usual price over
-- 200x what a vendor pays.
function ns.SellVerdictPrice(item, P)
  if type(item) ~= "table" or type(P) ~= "table" or type(P.rec) ~= "table" or P.rec.vendorFar then return nil end
  local U = tonumber(P.at)
  if not U or U <= 1 then return nil end
  local V, M = tonumber(item.vendor) or 0, tonumber(item.market) or 0
  if M <= 0 and V > 0 and U > OLD_FAR * V then return nil end
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

-- How fast an item sells: 1 (10 or more a day), 2 (1 to 9) or 3 (under 1,
-- measured for 4 hours or more). Not known yet counts as 2.
local function speedBand(item)
  local per = type(ns.SoldPerDay) == "function" and ns.SoldPerDay(item) or nil
  if per == nil then return 2 end
  if per >= 10 then return 1 end
  if per >= 1 then return 2 end
  local hours = type(ns.SalesHours) == "function" and ns.SalesHours() or 24
  return hours >= SLOW_HOURS and 3 or 2
end

-- The sell chances for a listing time: the nearest measured one (dump: the
-- usual price while someone is dumping).
local function sellChances(hours, dump)
  local T = dump and DUMP_P or SELL_P
  local best, bestD
  for h in pairs(T) do
    local d = math.abs(h - hours)
    if not bestD or d < bestD or (d == bestD and h < best) then best, bestD = h, d end
  end
  return T[best]
end

-- ns.SellVerdict(item, P [, count]): the tooltip's call for a stack of count
-- in your bags, or for one item sold as part of a stack (no count: not from
-- your bags), or nil (no usable AH price).
-- { kind, unit, left, right, lc, rc } with lc / rc ns.COLORS keys:
--   "auction"  "Auction it"     "+26% vs vendor", "6.1x vendor", "can't vendor" (no vendor price)
--   "vendor"   "Vendor it"      "+5% vs AH", "2.8x AH", "same as AH" (within 1%), or
--                               "AH +3c, not worth the trip" (it pays, but under 1 silver for the stack)
--   "close"    "Close call"     "AH +9%, vendor is safer"
--   "maybe"    "Maybe auction"  "few listed", "none listed now"
-- Auction it needs the expected result (sold at that price, or else sold to
-- a vendor after the deposit is lost) to beat vendoring by 5% of the vendor
-- price: p x (A - B) - (1 - p) x deposit >= 5% of B, p from SELL_P for the
-- item's sales speed, d the deposit over B. So A must be at least
-- 1 + (d(1 - p) + 5%) / p times B: about 1.11 / 1.29 / 2.07 at 24 hours,
-- 1.09 / 1.31 / 1.76 at 8 hours and 1.09 / 1.33 / 1.72 at 2 hours. While
-- someone is dumping (P.under) the usual price is judged with DUMP_P: about
-- 2.21 / 1.73 / 4.01 at 24 hours, 1.89 / 2.47 / 4.97 at 8, 1.95 / 2.38 / 4.28 at 2.
-- unit is the price judged (ns.SellVerdictPrice); "maybe" has none. Shift
-- never changes it; a stack in the bags only through the deposit's
-- per-listing minimum and the 1 silver trip (both left out without a count).
function ns.SellVerdict(item, P, count)
  if type(item) ~= "table" or type(P) ~= "table" or type(P.rec) ~= "table" then return nil end
  local rec, U = P.rec, ns.SellVerdictPrice(item, P)
  if not U then return nil end
  local q = tonumber(count)
  if q then q = max(1, floor(q)) end
  local V, M = tonumber(item.vendor) or 0, tonumber(item.market) or 0
  -- 1 or 2 listings, or none right now: an AH price that looks better is a maybe.
  local unsure = (rec.thinListings and "few listed") or (M <= 0 and "none listed now") or nil
  local function maybe(why) return { kind = "maybe", left = "Maybe auction", right = why, lc = "buy", rc = "muted" } end
  if V <= 0 then
    if unsure then return maybe(unsure) end
    return { kind = "auction", unit = U, left = "Auction it", right = "can't vendor", lc = "pos", rc = "muted" }
  end
  local A, B = U * 95, V * 100
  if A * 100 < B * VENDOR_PCT then
    return { kind = "vendor", unit = U, left = "Vendor it", right = (B > A) and ns.BetterText(B, A, "AH") or "same as AH",
             lc = "neg", rc = "neg" }
  end
  local hours, pm, least = ns.DepositModel()
  local p = sellChances(hours, P.under ~= nil)[speedBand(item)]
  -- The deposit for one of them, in 1/100 copper: the rate, or the
  -- per-listing minimum shared by the stack.
  local dep = floor(pm * V / 10)
  if q then dep = max(dep, floor((least * 100 + q - 1) / q)) end
  if p * (A - B) - (100 - p) * dep >= EDGE_PCT * B then
    local gain = q and floor((A - B) * q / 100) or TRIP_MIN
    if gain < TRIP_MIN then
      return { kind = "vendor", unit = U, left = "Vendor it",
               right = (gain >= 1) and ("AH +" .. moneyText(gain) .. ", not worth the trip") or "same as AH",
               lc = "neg", rc = "neg" }
    end
    if unsure then return maybe(unsure) end
    return { kind = "auction", unit = U, left = "Auction it", right = ns.BetterText(A, B, "vendor"), lc = "pos", rc = "pos" }
  end
  return { kind = "close", unit = U, left = "Close call", right = "AH +" .. floor((A - B) * 100 / B) .. "%, vendor is safer",
           lc = "muted", rc = "muted" }
end

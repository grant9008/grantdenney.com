"""FROZEN COPY for the backtest: tools/wow/addon_test/rec_model.py as it was on main
3fa1f416a8 (19 Nov 2026), before the polish wave. It keeps the "old" rules the plan
replaces (Suggested price = the sell target, "Auction it" at 110% of the vendor price)
comparable on later data. Never edit it to follow the addon: the current advice is
tools/wow/addon_test/rec_model.py."""

# Its own notes: an independent Python model of Recommend.lua (the price advice),
# written from the rules, not from the Lua.
#
# recommend(item, windows, tf, live=None, sales_hours=24)
#   item     {"cheapest", "market", "sold24", "vendor", "listed", "auctions"} in
#            copper; auctions (separate listings) may be missing or None = unknown;
#            listed is the units listed now
#   windows  function tf -> (cheap list, market list); None = nothing listed
#   tf       the timeframe asked for
#   live     the cheapest listing the AH shows right now, or None
#   sales_hours  how many of the last 24 hours have a measured sales estimate
#            (the data file's salesHours; None = an older file = 24)
#
import math

CHAIN = ["1D", "5D", "1W", "1M", "1Y"]
AH_CUT = 0.95
MIN_POINTS = 4
MIN_RANGE = 0.10
FAR = 2
MIN_AUCTIONS = 3
VENDOR_FAR = 200
VENDOR_FAR_UNITS = 10     # the vendor rule only applies with fewer units listed than this
SETTLED_HOURS = 6         # the last quarter of an hourly window, at least 6 hours ...
SETTLED_PCT = 80          # ... all at or under 80% of the sell target: the price dropped and stayed down
HOURLY = ("1D", "5D", "1W")


def settled_high(market, target):
    """A drop that lasted. market: an hourly window's market prices, oldest
    first (None = nothing listed). The drop must last a quarter of the window,
    at least 6 hours; when those last hours have a price in at least 2 of 3
    of them, the newest one included, and all are at or under 80% of target,
    returns (their highest, the hours), the new sell target. (None, None)
    otherwise. Whole numbers only."""
    if not target or target <= 0 or not market or not market[-1]:
        return None, None
    hours = max(SETTLED_HOURS, (len(market) + 3) // 4)
    recent = [v for v in market[-hours:] if v]
    if len(recent) * 3 >= hours * 2 and max(recent) * 100 <= target * SETTLED_PCT:
        return max(recent), hours
    return None, None


def sales_hours_of(h):
    """Data file salesHours -> 0..24 (None, as in older files, is a full day)."""
    if h is None:
        return 24
    h = math.floor(h)
    return max(0, min(24, h))


def sold_per_day(sold24, sales_hours=24):
    """(per day or None while measuring, early estimate?) like ns.SoldPerDay."""
    h = sales_hours_of(sales_hours)
    if h <= 0:
        return None, True
    if h >= 24:
        return sold24, False
    return math.floor(sold24 * 24 / h + 0.5), True


def sold_text(sold24, sales_hours=24):
    """Like ns.SoldText: "~1,234 sold/day", with "(early estimate)", or "sales: measuring"."""
    per, early = sold_per_day(sold24, sales_hours)
    if per is None:
        return "sales: measuring"
    return "~" + "{:,}".format(int(per)) + " sold/day" + (" (early estimate)" if early else "")


def pct(sorted_vals, p):
    """Percentile with linear interpolation (PocketGE method), rounded to whole
    copper. 1-based steps so the floating point matches Lua exactly."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    idx = p * (n - 1) + 1
    lo = math.floor(idx)
    if lo < 1:
        lo = 1
    if lo >= n:
        return sorted_vals[n - 1]
    w = idx - lo
    v = sorted_vals[lo - 1] * (1 - w) + sorted_vals[lo] * w
    return math.floor(v + 0.5)


def verdict_for(score):
    if score >= 60:
        return "Strong Buy"
    if score >= 25:
        return "Buy"
    if score > -25:
        return "Hold"
    if score > -60:
        return "Sell"
    return "Strong Sell"


def money_text(c):
    v = math.floor(abs(c) + 0.5)
    g, s, k = v // 10000, (v % 10000) // 100, v % 100
    if v > 1000000:
        k = 0
    parts = [("%dg" % g) if g else None, ("%ds" % s) if s else None, ("%dc" % k) if k else None]
    parts = [p for p in parts if p]
    out = " ".join(parts) if parts else "0c"
    return "-" + out if c < 0 and parts else out


def recommend(item, windows, tf, live=None, sales_hours=24):
    asked = tf.upper() if isinstance(tf, str) else "1D"
    if asked not in CHAIN:
        asked = "1D"
    best = None
    for key in CHAIN[CHAIN.index(asked):]:
        c, m = windows(key)
        c = sorted(v for v in c if v)
        m = sorted(v for v in m if v)
        if best is None or len(c) + len(m) > len(best[1]) + len(best[2]):
            best = (key, c, m)
        if len(best[1]) >= MIN_POINTS and len(best[2]) >= MIN_POINTS:
            break
    used, cheaps, mkts = best
    history = len(cheaps) >= MIN_POINTS and len(mkts) >= MIN_POINTS

    cheap_now = live if (live is not None and live > 0) else item["cheapest"]
    mkt_now, sold24, vendor = item["market"], item["sold24"], item.get("vendor", 0)
    per_day, sold_early = sold_per_day(sold24, sales_hours)
    auctions = item.get("auctions")
    thin_listings = auctions is not None and 1 <= auctions < MIN_AUCTIONS
    vendor_far = vendor > 0 and mkt_now > VENDOR_FAR * vendor and (item.get("listed") or 0) < VENDOR_FAR_UNITS
    advice = history and not thin_listings and not vendor_far

    if cheaps:
        lo, hi = cheaps[0], cheaps[-1]
    elif cheap_now > 0:
        lo = hi = cheap_now
    elif mkt_now > 0:
        lo = hi = mkt_now
    else:
        lo = hi = 0

    p90 = pct(mkts, 0.90)
    far = bool(history and p90 and p90 > 0 and cheap_now > FAR * p90)

    buy = pct(cheaps, 0.20) if len(cheaps) >= MIN_POINTS else cheap_now
    if buy <= 0:
        p = pct(cheaps, 0.20)
        buy = p if p is not None else mkt_now
    if mkt_now > 0 and buy > mkt_now:
        buy = mkt_now

    p70 = pct(mkts, 0.70)
    sell = p70 if p70 is not None else mkt_now
    settled, settled_hours = settled_high(windows(used)[1], sell) if history and used in HOURLY else (None, None)
    if settled:
        sell = settled
    mkt_far = bool(history and p90 and p90 > 0 and mkt_now > FAR * p90)
    if sell < mkt_now and not mkt_far:
        sell = mkt_now
    if sell <= 0:
        sell = cheap_now

    p40 = pct(mkts, 0.40)
    mkt40 = p40 if p40 is not None else mkt_now
    if cheap_now > 0 and not far and cheap_now >= mkt40 and math.floor(cheap_now * AH_CUT) > buy:
        list_at = cheap_now - 1
    else:
        list_at = sell
    if cheap_now > 1 and not far and list_at < cheap_now - 1:
        list_at = cheap_now - 1
    follow_floor = list_at          # the List At rule from today's prices, kept with no advice
    if not advice:
        buy = sell = list_at = 0
    # what a player would post at: List At, or with no advice 1c under the cheapest (or the market rule)
    post_at = list_at
    if not advice:
        post_at = cheap_now - 1 if cheap_now > 1 else 0
        if not thin_listings and not vendor_far and follow_floor > post_at:
            post_at = follow_floor

    margin = math.floor(sell * AH_CUT) - buy
    margin_pct = margin / buy if buy > 0 else 0

    pos = 0.5
    if cheap_now > 0 and advice:
        plo, phi = lo, hi
        median = pct(cheaps, 0.50)
        want = MIN_RANGE * median
        if phi - plo < want:
            mid = (plo + phi) / 2
            plo, phi = mid - want / 2, mid + want / 2
        if phi > plo:
            pos = min(1.0, max(0.0, (cheap_now - plo) / (phi - plo)))
    score = (0.5 - pos) * 200
    if margin <= 0:
        score = max(-24, min(24, score))
    elif settled:                       # after a drop that lasted the window's range is the old one: no Buy
        score = min(24, score)

    vendor_better = vendor > 0 and post_at > 0 and math.floor(post_at * AH_CUT) <= vendor
    notes = []
    if per_day is not None and per_day < 3:
        notes.append("Thin market")
    if cheap_now == 0:
        notes.append("No listings right now")
    if not history:
        notes.append("Not enough price history yet")
    elif used != asked:
        notes.append("Too few prices in %s: advice uses %s" % (asked, used))
    if thin_listings:
        notes.append("Only %d %s: price may not be reliable" % (auctions, "listing" if auctions == 1 else "listings"))
    if vendor_far:
        notes.append("Listed far above what a vendor pays: price may not be reliable")
    if far:
        notes.append("Cheapest now is far above recent prices")
    if settled and advice and list_at == sell:
        notes.append("Price fell and stayed down: the sell target uses the last %d hours" % settled_hours)
    if history and 0 < cheap_now < buy and margin > 0 and not settled:
        notes.append("Cheapest is below the buy target: buy it")
    if margin <= 0 and buy > 0 and sell > 0:
        notes.append("Spread does not beat the 5% AH cut")
    if vendor_better:
        notes.append("A vendor pays more (%s)" % money_text(vendor))
    return {"buy": buy, "sell": sell, "listAt": list_at, "postAt": post_at, "followFloor": follow_floor,
            "margin": margin, "marginPct": margin_pct,
            "verdict": verdict_for(score), "score": score, "pos": pos, "lo": lo, "hi": hi,
            "cheapestNow": cheap_now, "marketNow": mkt_now, "sold24": sold24, "notes": notes,
            "soldPerDay": per_day, "salesMeasuring": per_day is None, "soldEarly": bool(sold_early),
            "tf": used, "askedTF": asked, "history": history, "advice": advice,
            "auctions": auctions, "thinListings": thin_listings, "vendorFar": vendor_far,
            "far": far, "settled": bool(settled and advice),
            "settledHours": settled_hours if settled and advice else None, "vendorBetter": vendor_better}


def sell_prices(rec, live=None, own=False, vendor=0):
    """ns.SellPrices (Recommend.lua): the sell screen's two prices.
    now   Sell now: 1 copper under the cheapest listing (the live one when
          given; your own live listing is matched), None when nothing is
          listed (nowWhy "none") or a few units sit far above the vendor price
          (nowWhy "unreliable").
    best  Best price: the sell target, never under a normal cheapest listing,
          None without history (bestWhy "history") or without advice
          (bestWhy "unreliable").
    rec is recommend(...) for the same live price."""
    cheap = rec["cheapestNow"]
    is_live = live is not None and live > 0
    mine = is_live and bool(own)
    out = {"now": None, "best": None, "nowWhy": None, "bestWhy": None, "live": is_live, "own": mine,
           "cheapest": cheap}
    if rec["vendorFar"]:
        out["nowWhy"] = "unreliable"
    elif cheap > 1:
        out["now"] = cheap if mine else cheap - 1
    else:
        out["nowWhy"] = "none"
    if not rec["history"]:
        out["bestWhy"] = "history"
    elif not rec["advice"]:
        out["bestWhy"] = "unreliable"
    else:
        b = rec["sell"]
        if cheap > 1 and not rec["far"] and b < cheap - 1:
            b = cheap - 1
        if mine and b == cheap - 1:
            b = cheap
        if b > 0:
            out["best"] = b
        else:
            out["bestWhy"] = "history"
    out["nowVendor"] = vendor > 0 and out["now"] is not None and math.floor(out["now"] * AH_CUT) <= vendor
    out["bestVendor"] = vendor > 0 and out["best"] is not None and math.floor(out["best"] * AH_CUT) <= vendor
    return out


WEEK_MIN_POINTS = 6


def week_range(cheap_week):
    """ns.WeekRange: (lowest, highest) cheapest price over the last 168 hourly
    points (None = nothing listed), or (None, None) with fewer than 6 prices."""
    vals = [v for v in cheap_week if v]
    if len(vals) < WEEK_MIN_POINTS:
        return None, None
    return min(vals), max(vals)


# The tooltip's sell verdict (ns.SellVerdict). Whole numbers only: A = what the
# AH nets per item after the 5% cut, B = what a vendor pays, in 1/100 copper.
AUCTION_PCT = 110   # "Auction it": A >= 110% of B (200% for a slow seller) ...
SLOW_PCT = 200
MIN_GAIN = 200      # ... and A - B >= 2 copper
VENDOR_PCT = 101    # "Vendor it": A under 101% of B
OLD_FAR = 200       # nothing listed: a Suggested price over 200x the vendor price is not used
SLOW_HOURS = 6      # fewer measured sales hours: how fast it sells is unknown


def sell_verdict_price(item, sp, rec):
    """ns.SellVerdictPrice: the AH price per item the verdict judges, or None.
    item {"vendor", "market"}, sp = sell_prices(...), rec = recommend(...).
    Listed now: List At (postAt), never over the AH value (or over Sell now
    when that is higher). Nothing listed: the Suggested price, unless it is
    over 200x the vendor price. None for vendorFar and for 1 copper or less."""
    if rec["vendorFar"]:
        return None
    v, m = item["vendor"], item["market"]
    if m > 0:
        u = min(rec["postAt"] or 0, max(m, sp["now"] or 0))
    else:
        u = sp["best"]
        if u is None or (v > 0 and u > OLD_FAR * v):
            return None
    return u if u > 1 else None


def better_text(hi, lo, what):
    """ns.BetterText: how much more hi pays than lo (hi >= lo > 0), rounded down."""
    if hi > lo * 100:
        return "over 100x " + what
    if hi >= lo * 10:
        return "%dx %s" % (hi // lo, what)
    if hi >= lo * 2:
        t = hi * 10 // lo
        return ("%d" % (t // 10) if t % 10 == 0 else "%d.%d" % (t // 10, t % 10)) + "x " + what
    p = (hi - lo) * 100 // lo
    return ("same as " + what) if p < 1 else "+%d%% vs %s" % (p, what)


def sell_verdict(item, sp, rec, sales_hours=None):
    """ns.SellVerdict: {"kind", "unit", "left", "right", "lc", "rc"} or None.
    item {"vendor", "market", "sold24"}; sales_hours as in the data file (None = a full day).
    kind "auction" ("Auction it"), "vendor" ("Vendor it"), "close" ("Close call")
    or "maybe" ("Maybe auction", no unit); lc / rc are ns.COLORS keys."""
    u = sell_verdict_price(item, sp, rec)
    if u is None:
        return None
    v, m = item["vendor"], item["market"]
    unsure = "few listed" if rec["thinListings"] else ("none listed now" if m <= 0 else None)

    def maybe(why):
        return {"kind": "maybe", "unit": None, "left": "Maybe auction", "right": why, "lc": "buy", "rc": "muted"}
    if v <= 0:
        if unsure:
            return maybe(unsure)
        return {"kind": "auction", "unit": u, "left": "Auction it", "right": "can't vendor", "lc": "pos", "rc": "muted"}
    a, b = u * 95, v * 100

    def wins(x, pct):
        return x * 100 >= b * pct and x - b >= MIN_GAIN
    if wins(a, AUCTION_PCT):
        if unsure:
            return maybe(unsure)
        per, _ = sold_per_day(item["sold24"], sales_hours)
        if per is not None and sales_hours_of(sales_hours) >= SLOW_HOURS and per < 1 and not wins(a, SLOW_PCT):
            return maybe("sells slowly")
        return {"kind": "auction", "unit": u, "left": "Auction it", "right": better_text(a, b, "vendor"),
                "lc": "pos", "rc": "pos"}
    if rec["far"] and sp["now"] and wins(sp["now"] * 95, AUCTION_PCT):
        return maybe("price jumped")
    if a * 100 < b * VENDOR_PCT:
        return {"kind": "vendor", "unit": u, "left": "Vendor it",
                "right": better_text(b, a, "AH") if b > a else "same as AH", "lc": "neg", "rc": "neg"}
    p = (a - b) * 100 // b
    return {"kind": "close", "unit": u, "left": "Close call", "right": ("AH +%d%%" % p) if p < 10 else "tiny gain",
            "lc": "muted", "rc": "muted"}


def stack_after_cut(unit, count):
    """The tooltip's "This stack (N), after AH cut" value: floor(unit * N * 95 / 100)."""
    return unit * count * 95 // 100

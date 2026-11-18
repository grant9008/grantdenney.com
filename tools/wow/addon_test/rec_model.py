"""Independent Python model of Recommend.lua (the price advice), used by
harness.py and test_core.py. Written from the rules, not from the Lua.

recommend(item, windows, tf, live=None, sales_hours=24)
  item     {"cheapest", "market", "sold24", "vendor", "listed", "auctions"} in
           copper; auctions (separate listings) may be missing or None = unknown;
           listed is the units listed now
  windows  function tf -> (cheap list, market list); None = nothing listed
  tf       the timeframe asked for
  live     the cheapest listing the AH shows right now, or None
  sales_hours  how many of the last 24 hours have a measured sales estimate
           (the data file's salesHours; None = an older file = 24)
"""
import math

CHAIN = ["1D", "5D", "1W", "1M", "1Y"]
AH_CUT = 0.95
MIN_POINTS = 4
MIN_RANGE = 0.10
FAR = 2
MIN_AUCTIONS = 3
VENDOR_FAR = 200
VENDOR_FAR_UNITS = 10     # the vendor rule only applies with fewer units listed than this


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
    # what a player would post at: List At, or with no advice what Use List At fills
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
    if history and 0 < cheap_now < buy and margin > 0:
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
            "far": far, "vendorBetter": vendor_better}


def sell_mode(rec, live=None):
    """How AH.lua's Use List At button gets its price: "advice" (the List At
    price), "cheapest" (1 copper under the live listing), "market" (only the
    history is short: today's List At rule, never far under the market price)
    or None when it hides (no advice and no live listing, or a few units far
    above the vendor price, where the cheapest listing is that silly price)."""
    if rec is None:
        return None
    if rec.get("advice") is not False:
        return "advice"
    if rec.get("vendorFar"):
        return None
    if live is None or live <= 1:
        return None
    if not rec.get("thinListings") and rec.get("followFloor", 0) > live - 1:
        return "market"
    return "cheapest"


def sell_price(rec, live=None, own=False):
    """What AH.lua's Use List At button fills (before rounding for the frame),
    or None when it hides (see sell_mode). Your own cheapest listing is
    matched, not undercut."""
    mode = sell_mode(rec, live)
    if mode is None:
        return None
    if mode == "advice":
        v = rec["listAt"]
    elif mode == "market":
        v = rec["followFloor"]
    else:
        v = live - 1
    if v is None or v <= 0:
        return None
    if live is not None and own and v == live - 1:
        v = live
    return v

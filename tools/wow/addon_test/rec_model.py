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


CALL_LOW, CALL_HIGH = "Low now: still falling", "High now: usually falls back"


def verdict_for(score):
    """The chart's call: high in its range, Hold, or low (no buy claim: low prices kept falling)."""
    if score >= 25:
        return CALL_LOW
    if score > -25:
        return "Hold"
    return CALL_HIGH


USUAL_N = 24
DUMP_PCT = 70


def rule_g(now, usual):
    """Recommend.lua ruleG: Sell now, unless under 70% of the usual price, then the usual price
    (nothing listed: the usual price). Returns (price or None, % Sell now is under it or None)."""
    if now and now > 0:
        if usual and now * 100 < usual * DUMP_PCT:
            return usual, (usual - now) * 100 // usual
        return now, None
    return usual, None


def usual_price(market_week):
    """ns.UsualPrice: the middle (pct 0.5) of the last 24 market prices of the hourly 1W window
    (None = nothing listed), at least 4 of them, else None."""
    vals = [v for v in market_week if v and v > 0][-USUAL_N:]
    if len(vals) < MIN_POINTS:
        return None
    return pct(sorted(vals), 0.5)


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

    # the one price to sell at (rule G), the same in every timeframe
    usual = usual_price(windows("1W")[1])
    sell_at = 0
    if not vendor_far:
        sell_at = rule_g(cheap_now - 1 if cheap_now > 1 else None, usual)[0] or 0
    vendor_better = vendor > 0 and sell_at > 0 and math.floor(sell_at * AH_CUT) <= vendor
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
        notes.append("Price fell and stayed down for the last %d hours" % settled_hours)
    if history and 0 < cheap_now < buy and margin > 0 and not settled:
        notes.append("Cheapest is under Target Buy")
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
            "settledHours": settled_hours if settled and advice else None, "vendorBetter": vendor_better,
            "usual": usual, "sellAt": sell_at}


def sell_prices(rec, live=None, own=False, vendor=0):
    """ns.SellPrices (Recommend.lua): the one price to sell at.
    now    Sell now: 1 copper under the cheapest listing (the live one when
           given; your own live listing is matched), None when nothing is
           listed (nowWhy "none") or a few units sit far above the vendor price
           (nowWhy "unreliable").
    usual  the usual price (rec["usual"]).
    at     Sell now, unless under 70% of usual (then usual, under = % under it);
           nothing listed: usual; None (atWhy "none" / "unreliable") without.
    rec is recommend(...) on 1D for the same live price (ns.SellPrices ignores
    the chart's timeframe)."""
    cheap = rec["cheapestNow"]
    is_live = live is not None and live > 0
    mine = is_live and bool(own)
    usual = rec.get("usual")
    out = {"now": None, "at": None, "under": None, "nowWhy": None, "atWhy": None, "live": is_live, "own": mine,
           "cheapest": cheap, "usual": usual}
    if rec["vendorFar"]:
        out["nowWhy"] = out["atWhy"] = "unreliable"
    elif cheap > 1:
        out["now"] = cheap if mine else cheap - 1
        out["at"], out["under"] = rule_g(out["now"], usual)
    else:
        out["nowWhy"] = "none"
        out["at"] = rule_g(None, usual)[0]
        if not out["at"]:
            out["atWhy"] = "none"
    out["nowVendor"] = vendor > 0 and out["now"] is not None and math.floor(out["now"] * AH_CUT) <= vendor
    out["atVendor"] = vendor > 0 and out["at"] is not None and math.floor(out["at"] * AH_CUT) <= vendor
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
VENDOR_PCT = 101    # "Vendor it": A under 101% of B
OLD_FAR = 200       # nothing listed: a usual price over 200x the vendor price is not used
SLOW_HOURS = 4      # none sold in this many measured hours or more: a slow seller; fewer: not known
EDGE_PCT = 5        # "Auction it": the expected result beats vendoring by 5% of B ...
TRIP_MIN = 100      # ... and the stack gains at least 1 silver if it sells
SELL_P = {2: (70, 26, 13), 8: (86, 49, 26), 24: (92, 73, 39)}   # sold within the listing time, by speed band
DEPOSIT_PM = (50, 200, 600)     # deposit per mille of the vendor price, by listing time 1..3
DEPOSIT_HOURS = (2, 8, 24)
DEPOSIT_MIN = 100               # the least per listing (copper)


def deposit_model(deposit=None):
    """ns.DepositModel: (hours, per mille, least per listing) for the listing time picked last.
    deposit = the saved ns.settings.deposit as a dict ({"dur", "rate": {d: pm}, "hours": {d: h}, "min"}) or None."""
    D = deposit or {}

    def whole(v, lo, hi):
        return v if isinstance(v, (int, float)) and v == int(v) and lo <= v <= hi else None
    d = whole(D.get("dur"), 1, 3) or 3
    rate, hours = D.get("rate") or {}, D.get("hours") or {}
    pm = whole(rate.get(d), 0, 5000) if isinstance(rate, dict) else None
    h = whole(hours.get(d), 1, 168) if isinstance(hours, dict) else None
    least = whole(D.get("min"), 0, 100000)
    return (int(h) if h is not None else DEPOSIT_HOURS[int(d) - 1],
            int(pm) if pm is not None else DEPOSIT_PM[int(d) - 1],
            int(least) if least is not None else DEPOSIT_MIN)


def sell_verdict_price(item, sp, rec):
    """ns.SellVerdictPrice: the price to sell at (sp["at"]), or None: for vendorFar, for 1 copper or
    less, and with nothing listed for a usual price over 200x the vendor price."""
    if rec["vendorFar"]:
        return None
    u = sp["at"]
    if u is None or u <= 1:
        return None
    if item["market"] <= 0 and item["vendor"] > 0 and u > OLD_FAR * item["vendor"]:
        return None
    return u


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


def speed_band(sold24, sales_hours=None):
    """1 (10+ a day), 2 (1-9), 3 (under 1, measured 4+ hours); not known: 2."""
    per, _ = sold_per_day(sold24, sales_hours)
    if per is None:
        return 2
    if per >= 10:
        return 1
    if per >= 1:
        return 2
    return 3 if sales_hours_of(sales_hours) >= SLOW_HOURS else 2


def sell_chances(hours):
    best = min(SELL_P, key=lambda h: (abs(h - hours), h))
    return SELL_P[best]


def sell_verdict(item, sp, rec, sales_hours=None, count=None, deposit=None):
    """ns.SellVerdict: {"kind", "unit", "left", "right", "lc", "rc"} or None, for a stack of count in
    the bags (None: one item sold as part of a stack: no per-listing minimum, no 1 silver trip).
    item {"vendor", "market", "sold24"}; sales_hours as in the data file (None = a full day);
    deposit: the saved deposit readings (deposit_model). kind "auction" ("Auction it"), "vendor"
    ("Vendor it", also "AH +3c, not worth the trip"), "close" ("Close call") or "maybe"
    ("Maybe auction", no unit); lc / rc are ns.COLORS keys."""
    u = sell_verdict_price(item, sp, rec)
    if u is None:
        return None
    q = max(1, int(count)) if count is not None else None
    v, m = item["vendor"], item["market"]
    unsure = "few listed" if rec["thinListings"] else ("none listed now" if m <= 0 else None)

    def maybe(why):
        return {"kind": "maybe", "unit": None, "left": "Maybe auction", "right": why, "lc": "buy", "rc": "muted"}
    if v <= 0:
        if unsure:
            return maybe(unsure)
        return {"kind": "auction", "unit": u, "left": "Auction it", "right": "can't vendor", "lc": "pos", "rc": "muted"}
    a, b = u * 95, v * 100
    if a * 100 < b * VENDOR_PCT:
        return {"kind": "vendor", "unit": u, "left": "Vendor it",
                "right": better_text(b, a, "AH") if b > a else "same as AH", "lc": "neg", "rc": "neg"}
    hours, pm, least = deposit_model(deposit)
    p = sell_chances(hours)[speed_band(item["sold24"], sales_hours) - 1]
    dep = pm * v // 10
    if q is not None:
        dep = max(dep, (least * 100 + q - 1) // q)
    if p * (a - b) - (100 - p) * dep >= EDGE_PCT * b:
        gain = (a - b) * q // 100 if q is not None else TRIP_MIN
        if gain < TRIP_MIN:
            return {"kind": "vendor", "unit": u, "left": "Vendor it",
                    "right": ("AH +%s, not worth the trip" % money_text(gain)) if gain >= 1 else "same as AH",
                    "lc": "neg", "rc": "neg"}
        if unsure:
            return maybe(unsure)
        return {"kind": "auction", "unit": u, "left": "Auction it", "right": better_text(a, b, "vendor"),
                "lc": "pos", "rc": "pos"}
    return {"kind": "close", "unit": u, "left": "Close call", "right": "AH +%d%%, vendor is safer" % ((a - b) * 100 // b),
            "lc": "muted", "rc": "muted"}


def stack_after_cut(unit, count):
    """The tooltip's "This stack (N), after AH cut" value: floor(unit * N * 95 / 100)."""
    return unit * count * 95 // 100

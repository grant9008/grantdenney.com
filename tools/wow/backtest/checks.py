"""The rules the improvement plan rests on (wf40 plan.md, with the skeptic's
corrections in critic.md), each tested on the backtest rows with the thresholds the
plan uses. Every check returns result rows: (rule, test, what came out, status),
status PASS, FAIL, WAIT (not enough data yet), SKIP or INFO."""
import math
import statistics
from collections import defaultdict

import bt_lib as B
from fwd import Fwd

SEGS = ("stack 50+/day", "stack 5-49/day", "stack <5/day", "gear", "versions")

# Sell price, rule G (critic item 2): Sell now, unless the cheapest is under 70% of the
# median market of the last 24 real hours; then that median.
G_DUMP = 0.70
G_MIN_SOLD_FAST = 0.60        # stacks selling 50+/day must still sell within a day this often
# Auction it / Vendor it (critic item 3): the AH price, as a multiple of the vendor price,
# from which "Auction it" may show, by sales a day; at the deposit a listing loses unsold.
AUCTION_AT = (("10+/day", 10, 1.15), ("1-9/day", 1, 1.30), ("<1/day", 0, 2.00))
DEPOSITS = (0.60, 0.30)       # the Classic 24h rate (critic evidence 4), and Sidebar.lua's 30%
RATIO_BANDS = ((1.01, "1.01-1.10"), (1.10, "1.10-1.15"), (1.15, "1.15-1.20"), (1.20, "1.20-1.30"),
               (1.30, "1.30-1.60"), (1.60, "1.60-2.00"), (2.00, "2-5"), (5.00, "5+"))
MIN_ROWS = 30                 # a band with fewer item-hours is not judged
# Flip ideas (plan item 1, critic #7): what the sidebar shows must make money (counting
# unsold stock) at least this often, with the predicted day profit within 2x of the real one.
FLIP_WON = 0.60
FLIP_RATIO = 2.0
FLIP_OUT_DAYS = 2             # the out-of-sample re-test needs snapshots of this many new days ...
FLIP_OUT_MIN = 30             # ... and this many distinct flips (an item once a day) at every gold level
DEAL_HOURS = 72               # deals sell in days: judged on what happened within 72 hours
GAP_COST = 200000             # 20g: Sidebar.lua GAP_COST_CAP, the most a gap flip may cost (the filter below)
# The % badge (plan item 5): at +40% or more over the 24h median the next day falls back.
BADGE_AT = 0.40
BADGE_FALL = -0.20            # the next day's median market at least 20% under today's
BADGE_BACK = 0.50             # and at least half of them half way back to the median
BADGE_DROPPED_MAX = 0.05      # at -40% or less, no rebound worth a "buy" word (median next day <= +5%)
# Craft profit (plan items 7 and 10): a green "profit" must really pay 2 hours later.
CRAFT_REAL = 0.90
# The chart's calls (critic A): Sell calls were followed by falls; Buy calls never by a rise.
CHART_SELL_MAX = 0.0
CHART_BUY_MAX = 0.0


def seg(r):
    if r["sid"] >= 1000000:
        return "versions"
    if r["gear"]:
        return "gear"
    pd = r["perDay"] or 0
    return "stack 50+/day" if pd >= 50 else "stack 5-49/day" if pd >= 5 else "stack <5/day"


def pct(a, b):
    return "%d%%" % round(100.0 * a / b) if b else "-"


class Ctx:
    def __init__(self, data, hist):
        self.rows, self.hours, self.decision = data["rows"], data["hours"], data["decision"]
        self.hist = hist
        self.F = Fwd(hist, self.hours)
        self.last = self.hours[-1]
        self.set24 = set(t for t in self.decision if t + 24 * 3600 <= self.last)
        self.set12 = set(t for t in self.decision if t >= B.SOLD_FLOOR_FROM and t + 12 * 3600 <= self.last)

    def sets(self):
        out = []
        if self.set24:
            out.append(("24h", self.set24, 24))
        if self.set12:
            out.append(("12h", self.set12, 12))
        return out


def rule_g(r):
    now, m = r["now"], r["med24"]
    if not now or not m:
        return None
    return now if now >= G_DUMP * m else int(m)


# ---------------------------------------------------------------- 1. sell price
def check_sell(c):
    """Rule G against the old Suggested price and Sell now: one listing per item-hour,
    value = 0.95 x price if it sold within the window, else relisted at the end at the then
    cheapest - 1c (never over its first price) after the cut, less a 30% deposit; per AH
    value at T."""
    out = []
    for name, tset, H in c.sets():
        agg = defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0]))
        for r in c.rows:
            if r["T"] not in tset or r["lo"] <= 1 or not r["now"] or not r["oldBest"]:
                continue
            g = rule_g(r)
            if g is None:
                continue
            hi = r["hi"] or r["lo"]
            end_lo = c.F.lo_at_end(r["sid"], r["T"], H)
            dep = 0.30 * r["vendor"]
            for rule, P in (("G", g), ("old", r["oldBest"]), ("now", r["now"])):
                s = c.F.sold(r["sid"], r["T"], P, H)
                a = agg[seg(r)][rule]
                a[0] += 1
                a[1] += s
                a[2] += (0.95 * P if s else (max(0.0, 0.95 * (min(end_lo, P + 1) - 1) - dep) if end_lo > 1 else -dep)) / hi
        segs = [s for s in SEGS if agg[s]["G"][0]]
        if not segs:
            out.append(("Sell price (rule G)", name + " set", "no rows", "WAIT"))
            continue
        v = {s: {k: agg[s][k][2] / agg[s][k][0] for k in ("G", "old", "now")} for s in segs}
        lose_old = [s for s in segs if v[s]["G"] < v[s]["old"]]
        lose_now = [s for s in segs if v[s]["G"] < v[s]["now"] - 1e-9]
        out.append(("Sell price (rule G)", "%s: value incl. unsold >= old Suggested, every segment" % name,
                    "G %s vs %s" % ("/".join("%.2f" % v[s]["G"] for s in segs), "/".join("%.2f" % v[s]["old"] for s in segs)),
                    "FAIL" if lose_old else "PASS"))
        out.append(("Sell price (rule G)", "%s: value incl. unsold >= Sell now, every segment" % name,
                    "G %s vs %s" % ("/".join("%.2f" % v[s]["G"] for s in segs), "/".join("%.2f" % v[s]["now"] for s in segs)),
                    "FAIL" if lose_now else "PASS"))
        fast = agg["stack 50+/day"]["G"]
        if fast[0]:
            share = fast[1] / float(fast[0])
            out.append(("Sell price (rule G)", "%s: stacks 50+/day sell within %dh >= %d%%" % (name, H, 100 * G_MIN_SOLD_FAST),
                        "%s (old Suggested %s)" % (pct(fast[1], fast[0]), pct(agg["stack 50+/day"]["old"][1], agg["stack 50+/day"]["old"][0])),
                        "PASS" if share >= G_MIN_SOLD_FAST else "FAIL"))
    return out


# ---------------------------------------------------------------- 2. Auction it thresholds
def band_of(x):
    k = None
    for lo, name in RATIO_BANDS:
        if x >= lo:
            k = name
    return k


def speed_of(r):
    pd = r["perDay"] or 0
    for name, at_least, _ in AUCTION_AT:
        if pd >= at_least:
            return name
    return AUCTION_AT[-1][0]


def auction_table(c, tset, H, dep, price):
    """{(speed, band): [item-hours, mean result vs vendor]}: list one unit at price(r); unsold
    = vendored afterwards with the deposit lost."""
    b = defaultdict(lambda: [0, 0.0])
    for r in c.rows:
        if r["T"] not in tset or r["vendor"] <= 0 or r["vendorFar"]:
            continue
        U = price(r)
        if not U:
            continue
        V = r["vendor"]
        k = band_of(0.95 * U / V)
        if k is None:
            continue
        res = 0.95 * U if c.F.sold(r["sid"], r["T"], U, H) else V - dep * V
        x = b[(speed_of(r), k)]
        x[0] += 1
        x[1] += (res - V) / V
    return b


def break_even(tab, speed):
    """The lowest ratio band from which every judged band up pays at least the vendor price."""
    names = [n for _, n in RATIO_BANDS]
    ok_from = None
    for n in reversed(names):
        x = tab.get((speed, n))
        if x and x[0] >= MIN_ROWS:
            if x[1] / x[0] >= 0:
                ok_from = n
            else:
                break
    return ok_from


def check_thresholds(c):
    out = []
    lo_of = dict((n, lo) for lo, n in RATIO_BANDS)
    for name, tset, H in c.sets():
        for dep in DEPOSITS:
            tab = auction_table(c, tset, H, dep, rule_g)
            bad, ends = [], []
            for speed, _, at in AUCTION_AT:
                ends.append("%s %s" % (speed, (break_even(tab, speed) or "none").split("-")[0] + "x"))
                for lo, n in RATIO_BANDS:
                    x = tab.get((speed, n))
                    if lo >= at - 1e-9 and x and x[0] >= MIN_ROWS and x[1] / x[0] < 0:
                        bad.append("%s %s: %+.0f%%" % (speed, n, 100 * x[1] / x[0]))
            out.append(("Auction it thresholds", "%s, deposit %d%%: at G's price, every band from %s pays >= vendor"
                        % (name, 100 * dep, "/".join("%.2f" % a for _, _, a in AUCTION_AT)),
                        "; ".join(bad[:3] + ["measured break-even " + ", ".join(ends)]),
                        "FAIL" if bad else "PASS"))
        # the verdict as the addon ships it (rec_model): every band it calls "Auction it" pays
        tab = defaultdict(lambda: [0, 0.0])
        for r in c.rows:
            if r["T"] not in tset or r["vkind"] != "auction" or r["vendor"] <= 0 or not r["vunit"]:
                continue
            V, U = r["vendor"], r["vunit"]
            k = band_of(0.95 * U / V) or "<1.01"
            res = 0.95 * U if c.F.sold(r["sid"], r["T"], U, H) else V - DEPOSITS[0] * V
            x = tab[(speed_of(r), k)]
            x[0] += 1
            x[1] += (res - V) / V
        bad = ["%s %s: %+.0f%%" % (s, k, 100 * x[1] / x[0]) for (s, k), x in sorted(tab.items())
               if x[0] >= MIN_ROWS and x[1] / x[0] < 0]
        out.append(("Auction it as shipped", "%s, deposit %d%%: every band the addon calls Auction it pays >= vendor"
                    % (name, 100 * DEPOSITS[0]), "; ".join(bad[:3]) if bad else "%d bands judged" %
                    sum(1 for x in tab.values() if x[0] >= MIN_ROWS), "FAIL" if bad else "PASS"))
    return out


# ---------------------------------------------------------------- 3. flips
def wilson(k, n, z=1.96):
    """The 95% range of a share k of n (Wilson), in percent."""
    if not n:
        return 0.0, 100.0
    p = k / float(n)
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return 100 * max(0.0, c - h), 100 * min(1.0, c + h)


def distinct(xs):
    """Each (item, UTC day) once: hourly snapshots find the same flip again and
    again, so only its first showing that day counts."""
    seen = {}
    for x in sorted(xs, key=lambda x: (x["T"], x["id"])):
        seen.setdefault((x["id"], B.fmt_t(x["T"])[:6]), x)
    return list(seen.values())


def judge_ideas(xs):
    """(status, text) for one list of distinct ideas: won (counting unsold stock)
    >= FLIP_WON, and the predicted profit within FLIP_RATIO of the real one."""
    n = len(xs)
    won = sum(1 for x in xs if x["mtm"] > 0)
    real, pred = sum(x["mtm"] for x in xs), sum(x["pred"] for x in xs)
    ratio_ok = real > 0 and pred / real <= FLIP_RATIO and real / max(pred, 1e-9) <= FLIP_RATIO
    lo, hi = wilson(won, n)
    text = "%d flips, won %s (95%% range %.0f-%.0f%%), real %+.0fg vs predicted %+.0fg" % (
        n, pct(won, n), lo, hi, real / 1e4, pred / 1e4)
    return ("PASS" if n and won >= FLIP_WON * n and ratio_ok else "FAIL"), text


def gold_levels(xs):
    return sorted(set(x["gold"] for x in xs), key=lambda g: int(g[:-1]))


def flip_rows(fl):
    """The gap ideas the sidebar shows, counted once per item and day and judged at
    each gold level on its own (the levels can show the same flips). In sample
    (the snapshots the rules were fitted on) the count is small, so the range is
    wide; out of sample needs FLIP_OUT_DAYS new days and FLIP_OUT_MIN distinct
    flips at every gold level before it says PASS or FAIL. The deals the addon
    keeps hidden are judged the same way over DEAL_HOURS."""
    out = []
    gaps = [x for x in fl["shown"] if x["kind"] == "gap"]
    for label, xs in (("in sample (the 4 snapshots the rules were fitted on)", [x for x in gaps if x["inSample"]]),
                      ("out of sample (snapshots after 19 Nov 03:00)", [x for x in gaps if not x["inSample"]])):
        test = label + ": won >= %d%%, predicted within %gx, per gold, each flip once a day" % (100 * FLIP_WON, FLIP_RATIO)
        by = dict((g, distinct([x for x in xs if x["gold"] == g])) for g in gold_levels(xs))
        if not xs:
            out.append(("Flip ideas (shown)", test, "no gap ideas shown yet", "WAIT"))
            continue
        res = dict((g, judge_ideas(v)) for g, v in by.items())
        got = "; ".join("%s: %s" % (g, res[g][1]) for g in by)
        if not label.startswith("in sample"):
            days = sorted(set(B.fmt_t(x["T"])[:6] for x in xs))
            few = min(len(v) for v in by.values())
            if len(days) < FLIP_OUT_DAYS or few < FLIP_OUT_MIN:
                out.append(("Flip ideas (shown)", test, "%d new days, at least %d distinct flips per gold so far (needs %d "
                            "days, %d flips): run this again on later days. %s" % (len(days), few, FLIP_OUT_DAYS,
                                                                                  FLIP_OUT_MIN, got), "WAIT"))
                continue
        out.append(("Flip ideas (shown)", test, got,
                    "PASS" if all(r[0] == "PASS" for r in res.values()) else "FAIL"))
    # the deals the sidebar keeps hidden (DEALS_SHOWN), as they would show: they sell in days
    deals = fl.get("deals") or []
    test = ("deals (hidden in the addon until this passes): won >= %d%% within %dh, predicted within %gx, per gold"
            % (100 * FLIP_WON, DEAL_HOURS, FLIP_RATIO))
    if not deals:
        out.append(("Flip ideas (deals)", test, "no deals would have shown yet", "WAIT"))
    else:
        full = [x for x in deals if x["hz"] >= DEAL_HOURS]
        by = dict((g, distinct([x for x in full if x["gold"] == g])) for g in gold_levels(deals))
        few = min(len(v) for v in by.values())
        early = dict((g, distinct([x for x in deals if x["gold"] == g])) for g in gold_levels(deals))
        sofar = "; ".join("%s: %s" % (g, judge_ideas(v)[1]) for g, v in early.items())
        if few < FLIP_OUT_MIN:
            out.append(("Flip ideas (deals)", test, "%d distinct deals per gold with %dh after them so far (needs %d). "
                        "Within the hours they had: %s" % (few, DEAL_HOURS, FLIP_OUT_MIN, sofar), "WAIT"))
        else:
            res = dict((g, judge_ideas(v)) for g, v in by.items())
            out.append(("Flip ideas (deals)", test, "; ".join("%s: %s" % (g, res[g][1]) for g in by),
                        "PASS" if all(r[0] == "PASS" for r in res.values()) else "FAIL"))
    # the filters behind the rules, on every collector gap flip (the wf40 evidence)
    gaps = [x for x in fl["all"] if x["kind"] == "gap" and x["med24"]]
    spike = [x for x in gaps if "spike" in x["risk"]]
    rest = [x for x in gaps if "spike" not in x["risk"]]
    if spike and rest:
        a, b = sum(x["first"] for x in spike) / float(len(spike)), sum(x["first"] for x in rest) / float(len(rest))
        out.append(("Flip filters", "gap flips flagged spike sell less often than the rest (so they are hidden)",
                    "first unit sold %s of %d vs %s of %d" % (pct(a * len(spike), len(spike)), len(spike),
                                                              pct(b * len(rest), len(rest)), len(rest)),
                    "PASS" if a < b else "FAIL"))
    small = [x for x in gaps if x["relist"] <= 1.25 * x["med24"] and x["cost"] <= GAP_COST]
    if small:
        won = sum(1 for x in small if x["mtm"] > 0)
        lo, hi = wilson(won, len(small))
        out.append(("Flip filters", "gap flips relisted <= 1.25x the 24h median and costing <= %dg in all: won >= %d%%"
                    % (GAP_COST // 10000, 100 * FLIP_WON),
                    "%d flips, won %s (95%% range %.0f-%.0f%%), %+.0fg" % (len(small), pct(won, len(small)), lo, hi,
                                                                         sum(x["mtm"] for x in small) / 1e4),
                    "PASS" if won >= FLIP_WON * len(small) else "FAIL"))
    return out


# ---------------------------------------------------------------- 4. badge
def check_badge(c):
    out = []
    tset = c.set24 or c.set12
    H = 24 if c.set24 else 12
    if not tset:
        return [("Badge (spike / dropped)", "next-day market after a big move", "no rows", "WAIT")]
    bk = defaultdict(lambda: [0, [], 0])
    for r in c.rows:
        if r["T"] not in tset or r["hi"] <= 0 or not r["med24"] or r["npts"] < 4 or (not r["gear"] and (r["perDay"] or 0) < 5):
            continue
        b = (r["hi"] - r["med24"]) / r["med24"]
        k = "spike" if b >= BADGE_AT else "dropped" if b <= -BADGE_AT else None
        if not k:
            continue
        _, nxt = c.F.after(r["sid"], r["T"], H)
        his = [p[2] for p in nxt if p[2] > 0]
        if len(his) < H // 2:
            continue
        tm = statistics.median(his)
        x = bk[("gear" if r["gear"] else "stacks", k)]
        x[0] += 1
        x[1].append(tm / r["hi"] - 1)
        x[2] += abs(tm - r["med24"]) <= 0.5 * abs(r["hi"] - r["med24"])
    for g in ("stacks", "gear"):
        x = bk.get((g, "spike"))
        if x and x[0] >= MIN_ROWS:
            m, back = statistics.median(x[1]), x[2] / float(x[0])
            out.append(("Badge (spike)", "%s at +%d%% vs the 24h median: next %dh median <= %d%%, half way back >= %d%%"
                        % (g, 100 * BADGE_AT, H, 100 * BADGE_FALL, 100 * BADGE_BACK),
                        "%d item-hours, %+.1f%%, %.1f%% back" % (x[0], 100 * m, 100.0 * back),
                        "PASS" if m <= BADGE_FALL and back >= BADGE_BACK else "FAIL"))
        y = bk.get((g, "dropped"))
        if y and y[0] >= MIN_ROWS:
            m = statistics.median(y[1])
            out.append(("Badge (dropped)", "%s at -%d%%: no rebound (next %dh median <= +%d%%), so never a buy word"
                        % (g, 100 * BADGE_AT, H, 100 * BADGE_DROPPED_MAX), "%d item-hours, %+.1f%%" % (y[0], 100 * m),
                        "PASS" if m <= BADGE_DROPPED_MAX else "FAIL"))
    return out


# ---------------------------------------------------------------- 5. chart calls
def check_chart(c):
    tset = c.set24
    if not tset:
        return [("Chart calls", "next 24h after a Buy or Sell call", "needs 24h after a decision hour", "WAIT")]
    agg = defaultdict(list)
    for r in c.rows:
        if r["T"] not in tset or r["hi"] <= 0 or r["sid"] >= 1000000:
            continue
        v = str(r["verdict"])
        side = "Sell" if v in ("Sell", "Strong Sell") else "Buy" if v in ("Buy", "Strong Buy") else None
        if not side:
            continue
        _, nxt = c.F.after(r["sid"], r["T"], 24)
        his = [p[2] for p in nxt if p[2] > 0]
        if len(his) < 12:
            continue
        g = "gear" if r["gear"] else ("stacks 5+/day" if (r["perDay"] or 0) >= 5 else "stacks <5/day")
        agg[(side, g)].append(statistics.median(his) / r["hi"] - 1)
    out = []
    for side, lim, words in (("Sell", CHART_SELL_MAX, "followed by a fall (\"usually falls back\")"),
                             ("Buy", CHART_BUY_MAX, "never followed by a rise (so never \"good time to buy\")")):
        parts, bad = [], False
        for g in ("stacks 5+/day", "gear", "stacks <5/day"):
            xs = agg.get((side, g))
            if xs and len(xs) >= 100:
                m = statistics.median(xs)
                parts.append("%s %+.1f%%" % (g, 100 * m))
                bad = bad or m > lim
        if parts:
            out.append(("Chart calls", "%s calls %s" % (side, words), ", ".join(parts), "FAIL" if bad else "PASS"))
    return out


# ---------------------------------------------------------------- 6. crafts
def craft_rows(res):
    out = []
    for g in ("smelting", "blacksmithing"):
        x = res.get(g)
        if not x or x["green"] < MIN_ROWS:
            out.append(("Craft profit", "%s: green really pays 2h later >= %d%%" % (g, 100 * CRAFT_REAL),
                        "%d green recipe-hours" % (x["green"] if x else 0), "WAIT"))
            continue
        share = x["real"] / float(x["green"])
        tsm = (x["tsmReal"] / float(x["tsm"])) if x["tsm"] else None
        out.append(("Craft profit", "%s: green really pays 2h later >= %d%%, better than a TSM-style claim" % (g, 100 * CRAFT_REAL),
                    "%s of %d (TSM-style %s of %d)" % (pct(x["real"], x["green"]), x["green"],
                                                      pct(x["tsmReal"], x["tsm"]), x["tsm"]),
                    "PASS" if share >= CRAFT_REAL and (tsm is None or share >= tsm) else "FAIL"))
    return out


# ---------------------------------------------------------------- 0. the model
def check_port(src, hist, hours, log=print, per_hour=60):
    """The Python advice (rebuilt line -> rec_model) equals the addon's real Lua (the test
    harness) on per_hour items at two decision hours: the backtest tests what ships."""
    import random
    import harness as H
    smeas = B.state_measured(src)
    _, latest, _ = B.parse_data_file(src.file("ForeverAH_Data.lua"))
    items_json = src.json("items.json")
    dec = [t for t in hours if t >= B.T0]
    picks = sorted(set([dec[len(dec) // 3], dec[-1]])) if dec else []
    bad = total = 0
    for T in picks:
        meas = B.measured_at(T, hours, smeas)
        sh = B.FA.sales_hours(meas, T)
        lines, parsed = {}, {}
        for sid, h in hist.items():
            p = B.point_at(h, T)
            if p is None:
                continue
            lt = latest.get(sid)
            meta = {"name": (lt or {}).get("name") or (items_json.get(str(sid)) or {}).get("name") or "",
                    "sell": (lt or {}).get("vendor") or 0}
            line, _ = B.line_at(sid, h, T, meas, meta, B.est_auctions(sid, p[3], lt, items_json))
            if line:
                lines[sid], parsed[sid] = line, B.item_from_line(line)
        text = B.data_text(T, lines, sh)
        ref = H.RefData(text=text)
        w = H.World(ref, H.load_items(src.file("items.json"), ref), data_src=text)
        rnd = random.Random(T)
        ids = sorted(parsed)
        groups = ([i for i in ids if parsed[i]["sold24"] >= 50 and i < 1000000],
                  [i for i in ids if B.is_gear(i, items_json)], [i for i in ids if parsed[i]["cheapest"] == 0],
                  [i for i in ids if parsed[i]["vendor"] > 0 and 0 < parsed[i]["market"] < 3 * parsed[i]["vendor"]], ids)
        pick = set()
        for g in groups:
            pick |= set(rnd.sample(g, min(len(g), per_hour // len(groups))))
        for iid in sorted(pick):
            rec, sp, sv = B.advice(parsed[iid], sh)
            lr, lp = w.ns.Recommend(iid, "1D"), w.ns.SellPrices(iid, "1D")
            lv = w.ns.SellVerdict(w.ns.GetItem(iid), lp)
            got = (lr.buy, lr.sell, lr.listAt, lr.postAt, lr.verdict, bool(lr.advice), lp.now, lp.best,
                   lv and lv.kind, lv and lv.unit)
            want = (rec["buy"], rec["sell"], rec["listAt"], rec["postAt"], rec["verdict"], bool(rec["advice"]), sp["now"],
                    sp["best"], sv and sv["kind"], sv and sv["unit"])
            total += 1
            if got != want:
                bad += 1
                if bad <= 3:
                    log("  model mismatch on %d at %s: Lua %s, model %s" % (iid, B.fmt_t(T), got, want))
        log("  %s: %d items compared" % (B.fmt_t(T), len(pick)))
    return [("Backtest model", "the Python advice equals the addon's Lua (%d item-hours)" % total,
             "%d mismatches" % bad, "PASS" if total and not bad else "FAIL")]

"""The backtest rows: for every decision hour T and every item or version with something
listed (or a vendor price) at T, what the addon's data file made at T held and what the
addon advised: the advice it ships (rec_model, kept equal to Recommend.lua; checks.py
check_port compares them) and the advice of 19 Nov 2026 (baseline_rec_model: the old
Suggested price and the old 110% "Auction it"). Cached in cache/rows.pkl, rebuilt when
the data or any of this code changes (about 10s an hour of data)."""
import os
import statistics
import time

import bt_lib as B

CODE = [os.path.join(B.HERE, f) for f in ("bt_lib.py", "dataset.py", "baseline_rec_model.py")] + [
    os.path.join(B.ADDON_TEST, "rec_model.py"), os.path.join(B.TOOLS, "fetch_ah.py")]


class Row(object):
    """One item-hour (slots: a million of these fit in far less memory than dicts)."""
    __slots__ = ("T", "sid", "gear", "vendor", "lo", "hi", "perDay", "avg7", "npts", "now", "best", "oldBest",
                 "verdict", "vendorFar", "vkind", "vunit", "med24")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __getitem__(self, k):
        return getattr(self, k)

    def __getstate__(self):
        return tuple(getattr(self, k) for k in self.__slots__)

    def __setstate__(self, st):
        for k, v in zip(self.__slots__, st):
            setattr(self, k, v)


def decision_hours(hours, max_hours):
    """Every hour from T0 with at least an hour after it; spread evenly over at most max_hours."""
    dec = [t for t in hours if B.T0 <= t <= hours[-1] - 3600]
    if max_hours and len(dec) > max_hours:
        step = len(dec) / float(max_hours)
        dec = sorted(set(dec[int(i * step)] for i in range(max_hours)))
    return dec


def build(src, hist, hours, max_hours, log=print):
    smeas = B.state_measured(src)
    _, latest, _ = B.parse_data_file(src.file("ForeverAH_Data.lua"))
    items_json = src.json("items.json")
    same_model = B.code_key(os.path.join(B.ADDON_TEST, "rec_model.py")) == B.code_key(
        os.path.join(B.HERE, "baseline_rec_model.py"))
    rows = []
    t_all = time.time()
    dec = decision_hours(hours, max_hours)
    for T in dec:
        t1 = time.time()
        meas = B.measured_at(T, hours, smeas)
        sh = B.FA.sales_hours(meas, T)
        n = 0
        for sid, h in hist.items():
            p = B.point_at(h, T)
            if p is None:
                continue
            lt = latest.get(sid)
            vendor = (lt or {}).get("vendor") or 0
            if p[1] <= 0 and vendor <= 0:
                continue
            meta = {"name": (lt or {}).get("name") or "x", "sell": vendor}
            line, ht = B.line_at(sid, h, T, meas, meta, B.est_auctions(sid, p[3], lt, items_json))
            if not line:
                continue
            it = B.item_from_line(line)
            rec, sp, sv = B.advice(it, sh)
            old_sp = sp if same_model else B.advice(it, sh, B.BRM)[1]
            mk = [q[2] for q in ht["h"] if q[2] > 0]             # real hourly market values up to T
            per, _ = B.RM.sold_per_day(it["sold24"], sh)
            rows.append(Row(**{
                "T": T, "sid": sid, "gear": B.is_gear(sid, items_json), "vendor": it["vendor"],
                "lo": it["cheapest"], "hi": it["market"], "perDay": per, "avg7": it["avg7"], "npts": len(mk),
                "now": sp["now"], "best": sp["best"], "oldBest": old_sp["best"],
                "verdict": rec["verdict"], "vendorFar": rec["vendorFar"],
                "vkind": sv and sv["kind"], "vunit": sv and sv["unit"],
                "med24": statistics.median(mk[-24:]) if len(mk) >= 4 else None,
            }))
            n += 1
        log("  %s: %d items, salesHours %d (%.1fs)" % (B.fmt_t(T), n, sh, time.time() - t1))
    log("  %d rows in %.0fs" % (len(rows), time.time() - t_all))
    return {"rows": rows, "hours": hours, "decision": dec}


def load(src, hist, hours, max_hours, log=print, keep=False):
    key = "%s|%s|%s" % (src.key(), max_hours, B.code_key(*CODE))
    return B.cached("rows", key, lambda: build(src, hist, hours, max_hours, log), keep)

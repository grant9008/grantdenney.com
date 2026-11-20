"""The craft profit rule (Crafts.lua's quote) on smelting and Blacksmithing recipes at
past hours, checked 2 hours later: mats bought at T, the made item sold at T+2h's
price. A port of the quote (prices, the 10% robust share over cheapest, market and
average, the thin rule, the 1.25x sold-at cap on the market basis), with mats bought
(no own sub-recipes) and no live prices: what a player sees right after a /reload.
At the cheapest prices the made item sells at its tooltip's price to sell at (rule G:
the shipped advice model's Sell at, as Crafts.lua asks ns.SellAt), or, when the
tooltip doubts its price, at Sell now with no profit claim.
Needs Crafts.lua (the crafts build); without it the test is skipped."""
import math
import os
import re
from collections import defaultdict

import bt_lib as B

DEFAULT_CRAFTS = os.path.join(B.REPO, "wow-addon", "GoblinLedger", "Crafts.lua")
BASES = ("lo", "mkt", "avg")
SKILLS = (164, 186)            # Blacksmithing, Mining (smelting)


def recipes(path):
    src = open(path, encoding="utf-8").read()
    rblock = src.split("local R = {", 1)[1].split("\n}", 1)[0]
    vblock = src.split("local V = {", 1)[1].split("\n}", 1)[0]
    vendor = {int(a): int(b) for a, b in re.findall(r"\[(\d+)\]=(\d+)", vblock)}
    rec = {}
    for sp, raw in re.findall(r'\[(\d+)\]="([^"]*)"', rblock):
        f = raw.split("|")
        f += [""] * (12 - len(f))
        skill = int(f[0] or 0)
        if skill not in SKILLS or any(c in f[5] for c in "BUS") or "H" in f[5]:
            continue      # bind on pickup, bonding unknown, random suffix (no claim), Horde only
        rec[int(sp)] = {"skill": skill, "product": int(f[1]), "count": (int(f[2] or 1) + int(f[3] or 1)) / 2,
                        "mats": [(int(a), int(b)) for a, b in re.findall(r"(\d+):(\d+)", f[4])]}
    return rec, vendor


class View:
    def __init__(self, T, items, sh):
        self.items, self.salesHours, self.hourEnd, self.dayEnd = items, sh, T, T - T % 86400

    def window(self, iid, tf):
        return B.window(self.items[iid], tf)


def day_stats(view, iid):
    import harness as H   # the harness's port of Sidebar.lua DayStats (what the crafts build caps by)
    return H.day_stats(view, iid)


def quote(view, r, vendor):
    def prices(iid):
        it = view.items.get(iid)
        p = {"vendor": vendor.get(iid), "item": it}
        if it:
            for k, f in (("lo", "cheapest"), ("mkt", "market"), ("avg", "avg7")):
                if it[f] > 0:
                    p[k] = it[f]
        return p

    def buy_at(p, b):
        x = (p.get("lo") or p.get("mkt")) if b == "lo" else (p.get("mkt") or p.get("lo")) if b == "mkt" else p.get("avg")
        return p["vendor"] if p["vendor"] and (not x or p["vendor"] < x) else x

    def sell_at(p, b):
        if b == "lo":
            return p.get("at")
        if b == "mkt":
            s = p.get("mkt")
            return p["cap"] if s and p.get("cap") and s > p["cap"] else s
        return p.get("avg")

    cost = {b: 0 for b in BASES}
    for mid, n in r["mats"]:
        p = prices(mid)
        if buy_at(p, "lo") is None:
            return None                       # a mat with no price: no claim
        for b in BASES:
            u = buy_at(p, b)
            cost[b] = None if (u is None or cost[b] is None) else cost[b] + math.floor(u * n + 0.5)
    pp = prices(r["product"])
    if not pp["item"]:
        return None
    st = day_stats(view, r["product"])
    if st and st["soldAt"]:
        pp["cap"] = math.floor(1.25 * st["soldAt"])
    # the tooltip's price to sell at (ns.SellAt: rule G); a doubted price: Sell now, never green
    _, sp, _ = B.advice(pp["item"], view.salesHours)
    pp["at"], doubt = sp["at"], False
    if not pp["at"] and sp["atWhy"] == "unreliable" and sp.get("raw"):
        pp["at"], doubt = sp["raw"], True
    prof, net = {}, {}
    for b in BASES:
        s = sell_at(pp, b)
        if s and s > 0:
            net[b] = math.floor(s * r["count"] * 95 / 100)
            if cost[b] is not None:
                prof[b] = net[b] - cost[b]
    if "lo" not in prof:
        return None
    it = pp["item"]
    per, _ = B.RM.sold_per_day(it["sold24"], view.salesHours)
    thin = (it["auctions"] is not None and 1 <= it["auctions"] < 3) or (per or 0) < 3 or doubt
    good = all(prof.get(b) is not None and prof[b] > 0.10 * cost[b] for b in BASES)
    bad = all(prof.get(b) is None or prof[b] < 0 for b in BASES) and any(b in prof for b in BASES)
    verdict = "profit" if good and not thin else "thin" if good else "loss" if bad else "even"
    # a TSM-style claim: the cheaper of the cheapest listing and the market value (none: no claim; Sell
    # at is the usual price then, with nothing listed now)
    ref_p = [x for x in (pp.get("lo"), pp.get("mkt")) if x]
    tsm = math.floor(min(ref_p) * r["count"] * 0.95) - cost["lo"] if ref_p else None
    return {"verdict": verdict, "cost": cost["lo"], "net": net.get("lo"), "tsm": tsm}


def run(src, hist, hours, path, log=print):
    """{group: {"green": n, "real": n with real profit 2h later, "tsm": n, "tsmReal": n}}"""
    rec, vendor = recipes(path)
    smeas = B.state_measured(src)
    _, latest, raw = B.parse_data_file(src.file("ForeverAH_Data.lua"))
    head = raw.split("items = {", 1)[0]
    vb = head.split("variants = {", 1)[1].split("\n  },", 1)[0] if "variants = {" in head else ""
    has_versions = {int(x) for x in re.findall(r"\[(\d+)\] =", vb)}
    rec = {k: r for k, r in rec.items() if r["product"] not in has_versions}
    items_json = src.json("items.json")
    need = {r["product"] for r in rec.values()} | {m for r in rec.values() for m, _ in r["mats"]}
    hset = set(hours)

    def view(T):
        meas = B.measured_at(T, hours, smeas)
        sh = B.FA.sales_hours(meas, T)
        its = {}
        for sid in need:
            h = hist.get(sid)
            p = h and B.point_at(h, T)
            if not p:
                continue
            lt = latest.get(sid)
            meta = {"name": "x", "sell": (lt or {}).get("vendor") or 0}
            line, _ = B.line_at(sid, h, T, meas, meta, B.est_auctions(sid, p[3], lt, items_json))
            if line:
                its[sid] = B.item_from_line(line)
        return View(T, its, sh)

    dec = [t for t in hours if B.T0 <= t <= hours[-1] - 7200 and (t + 7200) in hset]
    views = {}
    res = defaultdict(lambda: {"green": 0, "real": 0, "tsm": 0, "tsmReal": 0})
    for T in dec:
        for t in (T, T + 7200):
            if t not in views:
                views[t] = view(t)
        for r in rec.values():
            q0, q2 = quote(views[T], r, vendor), quote(views[T + 7200], r, vendor)
            if not q0 or not q2:
                continue
            g = "smelting" if r["skill"] == 186 else "blacksmithing"
            real = (q2["net"] - q0["cost"]) if q2["net"] else -1
            x = res[g]
            if q0["verdict"] == "profit":
                x["green"] += 1
                x["real"] += real > 0
            if q0["tsm"] is not None and q0["tsm"] > 0:
                x["tsm"] += 1
                x["tsmReal"] += real > 0
        views.pop(T - 7200, None)
    log("  %d recipes, %d decision hours" % (len(rec), len(dec)))
    return dict(res)

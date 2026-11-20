"""Flip ideas at past snapshots, scored against what happened next.

The collector finds its flips in the auction listings of one snapshot, which the
history files do not keep, so the flip test needs the snapshots themselves:
snapshots/ holds a trimmed copy of each (its header, its flips and the lines of the
flipped items: what the sidebar's ns.FlipIdeas reads). The first four (18 Nov 17:00 to
19 Nov 03:00 UTC) are the ones the flip rules were fitted on; every run of the backtest
adds the collector's latest file, and the ones after 19 Nov 03:00 are the
out-of-sample re-test. The ideas come from the addon's real Lua (the test harness),
so the test follows Sidebar.lua as it ships. The deals the sidebar keeps hidden
(DEALS_SHOWN) are scored as they would show with the switch on, over DEAL_HORIZON
hours: they sell in days."""
import os
import re
import statistics
import time

import bt_lib as B

IN_SAMPLE_UNTIL = 1795057200      # 19 Nov 03:00 UTC: the last snapshot the flip rules were fitted on
GOLDS = (("50g", 500000), ("500g", 5000000), ("5000g", 50000000))
HORIZON = 24                      # hours to sell in
MIN_AFTER = 12                    # a snapshot needs this many hours of history after it to be scored
DEAL_HORIZON = 72                 # deals (hidden in the addon) are given this many hours to sell
FLIP_DEPOSIT = 0.30               # Sidebar.lua DAY_DEPOSIT: a unit left unsold loses 30% of its vendor price
NAME_RE = re.compile(r"flips-(\d{8}-\d{4})\.lua$")


# ---------------------------------------------------------------- the snapshot store
def trim(raw):
    """A Data.lua text cut down to what flip ideas read: the header, the flips, the
    versions tables of flipped versions, and the lines of the flipped items."""
    head, body = raw.replace("\r\n", "\n").split("items = {", 1)
    m = re.search(r"(?ms)^  flips = \{\n(.*?)^  \},\n", head)
    flips = m.group(0) if m else "  flips = {\n  },\n"
    ids = {int(x) for x in re.findall(r'"(\d+)\|(?:gap|deal|vendor)\|', flips)}
    bases = {i // B.FA.VID_MULT for i in ids if i >= 1000000}
    keep_ids = ids | bases
    vm = re.search(r"(?ms)^  variants = \{\n(.*?)^  \},\n", head)
    variants = ""
    if vm:
        rows = [l for l in vm.group(1).splitlines(True)
                if re.match(r"\s*\[(\d+)\]", l) and int(re.match(r"\s*\[(\d+)\]", l).group(1)) in bases]
        variants = "  variants = {\n" + "".join(rows) + "  },\n"
    top = head[:min(i for i in (head.find("  variants = {"), head.find("  flips = {"), len(head)) if i >= 0)]
    lines = [l for l in body.splitlines(True) if re.match(r"\s*\[(\d+)\]", l) and int(re.match(r"\s*\[(\d+)\]", l).group(1)) in keep_ids]
    return top + variants + flips + "  items = {\n" + "".join(lines) + "  },\n}\n"


def snapshot_name(hour_end):
    return "flips-%s.lua" % time.strftime("%Y%m%d-%H%M", time.gmtime(hour_end))


def save_latest(src, log=print):
    """Keep a trimmed copy of the collector's latest file (once per snapshot hour)."""
    p = src.file("ForeverAH_Data.lua")
    try:
        raw = open(p, "rb").read().decode("utf-8")
    except OSError:
        return None
    h = B.header_numbers(raw[:4096])
    if not h.get("hourEnd") or "items = {" not in raw:
        return None
    os.makedirs(B.SNAPSHOTS, exist_ok=True)
    out = os.path.join(B.SNAPSHOTS, snapshot_name(h["hourEnd"]))
    if not os.path.exists(out):
        with open(out, "wb") as fh:
            fh.write(trim(raw).encode("utf-8"))
        log("  saved today's flips as snapshots/%s" % os.path.basename(out))
    return out


def snapshots(extra=()):
    found = {}
    for d in (B.SNAPSHOTS,) + tuple(extra):
        if os.path.isdir(d):
            for f in os.listdir(d):
                if NAME_RE.search(f):
                    found.setdefault(f, os.path.join(d, f))
    return [found[k] for k in sorted(found)]


# ---------------------------------------------------------------- scoring
def score(F, sid, T, units, cost, relist, vendor, hz):
    """Buy `units` for `cost` at T and relist them at `relist`: (first unit sold, cash
    within hz hours, result counting the unsold ones at the end hour's cheapest - 1c
    (never over the relist) after the cut, less their deposit)."""
    at, nxt = F.after(sid, T, hz)
    first = F.sale_times(sid, T, relist, hz)[1]
    cap = F.units_sold_above(sid, T, relist, hz)
    sold = min(units, max(cap, 1)) if first is not None else 0
    end_lo = 0
    for p in reversed(nxt):
        if p[1] > 0:
            end_lo = p[1]
            break
    cash = sold * (relist * 95 // 100) - cost
    left = units - sold
    left_each = (min(relist, end_lo - 1) * 0.95 if end_lo > 1 else 0) - FLIP_DEPOSIT * vendor
    return first is not None, cash, cash + left * max(0.0, left_each)


def med24_at(hist, sid, T):
    mk = [p[2] for p in (hist.get(sid) or {}).get("h", []) if p[0] <= T and p[2] > 0]
    return statistics.median(mk[-24:]) if len(mk) >= 4 else None


def lua_list(t):
    """A Lua array as a list (ns.FlipIdeas gives (ideas, why) when there are none)."""
    if isinstance(t, tuple):
        t = t[0]
    out, i = [], 1
    while t is not None and t[i] is not None:
        out.append(t[i])
        i += 1
    return out


def run(src, hist, hours, extra=(), log=print):
    """Score every stored snapshot with enough history after it. Returns {"shown":
    [...], "deals": [...], "all": [...], "snaps": [(name, T, hours after, in sample)]}:
    the sidebar's collector ideas (from the real Lua at 50g, 500g and 5000g), the
    deals it would show with DEALS_SHOWN on (scored over up to DEAL_HORIZON hours,
    with "hz" the hours they had) and every collector flip."""
    import harness as H     # the addon's real Lua (lupa); loaded only for this test
    from fwd import Fwd
    F, last = Fwd(hist, hours), hours[-1]
    items_path = src.file("items.json")
    out = {"shown": [], "deals": [], "all": [], "snaps": []}
    for path in snapshots(extra):
        text = open(path, "rb").read().decode("utf-8")
        ref = H.RefData(text=text)
        T = ref.hourEnd
        hz = min(HORIZON, (last - T) // 3600)
        if hz < MIN_AFTER:
            out["snaps"].append((os.path.basename(path), T, hz, None))
            continue
        ins = T <= IN_SAMPLE_UNTIL
        out["snaps"].append((os.path.basename(path), T, hz, ins))
        for f in ref.flips:
            it = ref.items.get(f["id"]) or {}
            first, cash, mtm = score(F, f["id"], T, f["units"], f["cost"], f.get("relist") or 0, it.get("vendor") or 0, hz)
            out["all"].append({"T": T, "id": f["id"], "kind": f["kind"], "risk": list(f.get("risk") or []),
                               "relist": f.get("relist") or 0, "cost": f["cost"], "med24": med24_at(hist, f["id"], T),
                               "first": first, "cash": cash, "mtm": mtm, "inSample": ins})
        items = H.load_items(items_path, ref)
        dz = min(DEAL_HORIZON, (last - T) // 3600)
        for gname, gold in GOLDS:
            for deals_on in (False, True):
                if deals_on:
                    with H.deals_shown():
                        w = H.World(ref, items, data_src=text, gold=gold)
                else:
                    w = H.World(ref, items, data_src=text, gold=gold)
                for x in lua_list(w.ns.FlipIdeas(5, gold)):
                    if not x.data or str(x.kind) == "vendor" or (str(x.kind) == "deal") != deals_on:
                        continue
                    iid = int(x.id)
                    fit = x.fit
                    units = int(fit.units) if fit is not None else int(x.units)
                    cost = int(fit.cost) if fit is not None else int(x.cost)
                    vendor = (ref.items.get(iid) or {}).get("vendor") or 0
                    first, cash, mtm = score(F, iid, T, units, cost, int(x.countAt), vendor, dz if deals_on else hz)
                    pred = float(x.later or 0) if deals_on else float(x.dayProfit)
                    out["deals" if deals_on else "shown"].append({
                        "T": T, "id": iid, "kind": str(x.kind), "gold": gname, "first": first, "cash": cash,
                        "mtm": mtm, "pred": pred, "inSample": ins, "hz": dz if deals_on else hz})
        log("  %s: %d collector flips, %d hours after" % (B.fmt_t(T), len(ref.flips), hz))
    return out

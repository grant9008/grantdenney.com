"""Backtest library: rebuilds the addon's Data.lua line an item had at a past hour T
from the price collector's own history files, and runs the addon's advice model on it.

Read only on the collector's data (its wowdata folder, or a clone of the wow-data
branch): files there are only ever opened for reading. Everything this writes goes
to tools/wow/backtest/cache (not committed).

Checked against the real files (wf40, 19 Nov 2026): the rebuilt lines equal the
collector's own for cheapest, market, listed, sold24, avg7, vendor and the hourly and
daily series (check_port in checks.py checks the Python advice against the Lua).
"""
import hashlib
import json
import os
import pickle
import re
import sys
import time

sys.dont_write_bytecode = True   # never write __pycache__ next to the repo's modules
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
TOOLS = os.path.join(REPO, "tools", "wow")
ADDON_TEST = os.path.join(TOOLS, "addon_test")
for _p in (TOOLS, ADDON_TEST, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import fetch_ah as FA  # noqa: E402  (only its pure functions are used)
import rec_model as RM  # noqa: E402  (the advice the addon ships, kept equal to Recommend.lua)
import baseline_rec_model as BRM  # noqa: E402  (the advice as it was on 19 Nov 2026, for "old" rules)

CACHE = os.path.join(HERE, "cache")
SNAPSHOTS = os.path.join(HERE, "snapshots")
DEFAULT_WOWDATA = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ForeverAH", "collector", "wowdata")

TF_SPEC = {"1D": ("H", 24, 3600), "5D": ("H", 120, 3600), "1W": ("H", 168, 3600),
           "1M": ("D", 30, 86400), "1Y": ("D", 365, 86400)}
SOLD_FLOOR_FROM = FA.SOLD_FLOOR_FROM   # 19 Nov 00:00 UTC: sales counted the current way from here
OLD_MEASURED_FROM = 1794963600         # 18 Nov 01:00 UTC: the older sales slots start here
T0 = 1795006800                        # 18 Nov 13:00 UTC: the first hour after the collector's long gap
STAPLES = (2589, 2592, 4306, 2772, 3858, 10620, 12359, 14047)   # every collector run has them


def fmt_t(t):
    return time.strftime("%d %b %H:%M", time.gmtime(t))


# ---------------------------------------------------------------- the Data.lua format
def b36dec(tok):
    neg = tok.startswith("-")
    body = tok[1:] if neg else tok
    v = int(body, 36) if body else 0
    return -v if neg else v


def decode_prices(sub):
    toks = sub.split(",") if sub != "" else []
    out, prev = [], None
    for t in toks:
        if t == "~":
            out.append(None)
            continue
        d = 0 if t == "" else b36dec(t)
        v = d if prev is None else prev + d
        prev = v
        out.append(v if v > 0 else None)
    return out


def decode_sold(sub):
    toks = sub.split(",") if sub != "" else []
    return [b36dec(t) if t else 0 for t in toks]


def decode_series(s):
    parts = (s.split(";") + ["", "", ""])[:3]
    c, m, so = decode_prices(parts[0]), decode_prices(parts[1]), decode_sold(parts[2])
    n = max(len(c), len(m), len(so))
    c = [None] * (n - len(c)) + c
    m = [None] * (n - len(m)) + m
    so = [0] * (n - len(so)) + so
    return {"n": n, "cheap": c, "market": m, "sold": so}


def lua_unescape(s):
    out, i = [], 0
    simple = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v",
              "\\": "\\", '"': '"', "'": "'", "\n": "\n"}
    while i < len(s):
        ch = s[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        e = s[i]
        if e.isdigit():
            j = i
            while j < len(s) and j < i + 3 and s[j].isdigit():
                j += 1
            out.append(chr(int(s[i:j])))
            i = j
            continue
        out.append(simple[e])
        i += 1
    return "".join(out)


LINE_RE = re.compile(r'\[(\d+)\]\s*=\s*"((?:[^"\\\n]|\\.)*)"')


def parse_line_body(iid, body):
    f = body.split("|")
    f += [""] * (10 - len(f))

    def n10(x):
        try:
            return int(x)
        except ValueError:
            return 0
    return {"id": iid, "name": f[0], "cheapest": n10(f[1]), "market": n10(f[2]),
            "listed": n10(f[3]), "sold24": n10(f[4]), "avg7": n10(f[5]), "vendor": n10(f[6]),
            "H": decode_series(f[7]), "D": decode_series(f[8]),
            "auctions": int(f[9]) if f[9].isdigit() else None}


def header_numbers(head):
    def num(key):
        m = re.search(r"\b" + key + r"\s*=\s*(-?\d+)", head)
        return int(m.group(1)) if m else None
    return {k: num(k) for k in ("updated", "fetched", "salesHours", "hourEnd", "dayEnd")}


def parse_data_file(path):
    raw = open(path, "rb").read().decode("utf-8")
    head, body = raw.split("items = {", 1)
    items = {}
    for m in LINE_RE.finditer(body):
        iid = int(m.group(1))
        items[iid] = parse_line_body(iid, lua_unescape(m.group(2)))
    return header_numbers(head), items, raw


def item_from_line(line):
    m = LINE_RE.search(line)
    return parse_line_body(int(m.group(1)), lua_unescape(m.group(2)))


def window(item, tf):
    key, n, _ = TF_SPEC[tf]
    full = item[key]
    m = min(n, full["n"])
    first = full["n"] - m
    return {"n": m, "cheap": full["cheap"][first:], "market": full["market"][first:], "sold": full["sold"][first:]}


# ---------------------------------------------------------------- the collector's data (read only)
class Source:
    """The collector's data folder (or a wow-data clone), opened read only."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.hist_dir = os.path.join(self.path, "hist")
        if not os.path.isdir(self.hist_dir):
            raise SystemExit("No price history in %s (expected a hist folder). Pass the collector's "
                             "wowdata folder or a wow-data clone with --data." % self.path)

    def file(self, name):
        return os.path.join(self.path, name)

    def json(self, name):
        with open(self.file(name), "rb") as fh:
            return json.load(fh)

    def latest_header(self):
        p = self.file("ForeverAH_Data.lua")
        with open(p, "rb") as fh:
            head = fh.read(4096).decode("utf-8", "replace")
        return header_numbers(head)

    def key(self):
        """What the cached copies of this data depend on: its path and newest snapshot."""
        h = self.latest_header()
        return "%s|%s|%s" % (self.path.lower(), h.get("hourEnd"), h.get("updated"))


def code_key(*paths):
    """A hash of the code a cache was built with: a change to any of it rebuilds the cache."""
    h = hashlib.sha1()
    for p in paths:
        with open(p, "rb") as fh:
            h.update(fh.read().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


def cached(name, key, build, keep=False):
    """build() once per key, kept in cache/<name>.pkl (with its key alongside). keep: use the
    cached copy whatever its key (newer prices or code are then left out)."""
    os.makedirs(CACHE, exist_ok=True)
    pk, kf = os.path.join(CACHE, name + ".pkl"), os.path.join(CACHE, name + ".key")
    if os.path.exists(pk) and os.path.exists(kf) and (keep or open(kf, encoding="ascii").read() == key):
        with open(pk, "rb") as fh:
            return pickle.load(fh)
    obj = build()
    with open(pk, "wb") as fh:
        pickle.dump(obj, fh, protocol=pickle.HIGHEST_PROTOCOL)
    with open(kf, "w", encoding="ascii") as fh:
        fh.write(key)
    return obj


def load_all_hist(src, log=print, keep=False):
    """{sid: {"h": [...], "d": [...], "v": key?}} for every hist file, cut at the last hour
    every staple has (a run still writing its files is left out). Cached per data key."""
    def read(f):
        try:
            with open(os.path.join(src.hist_dir, f), "rb") as fh:
                return int(f[:-5]), json.loads(fh.read())
        except (ValueError, OSError):
            return None           # not an item file, or one the collector is writing right now

    def build():
        from concurrent.futures import ThreadPoolExecutor
        t = time.time()
        out = {}
        names = [f for f in os.listdir(src.hist_dir) if f.endswith(".json")]
        with ThreadPoolExecutor(max_workers=8) as pool:   # opening 40,000 small files is the slow part
            for k, got in enumerate(pool.map(read, names, chunksize=64)):
                if got:
                    out[got[0]] = got[1]
                if k and k % 10000 == 0:
                    log("  read %d of %d history files" % (k, len(names)))
        last = min((max((int(p[0]) for p in out.get(s, {}).get("h", [])), default=0) for s in STAPLES))
        for h in out.values():
            if h.get("h") and int(h["h"][-1][0]) > last:
                h["h"] = [p for p in h["h"] if int(p[0]) <= last]
        log("  %d history files read in %.0fs" % (len(out), time.time() - t))
        return out
    return cached("hist", src.key(), build, keep)


def all_hours(hist):
    hs = set()
    for sid in STAPLES:
        for p in hist.get(sid, {}).get("h", []):
            hs.add(int(p[0]))
    return sorted(hs)


def state_measured(src):
    st = src.json("state.json")
    return {int(k): int(v) for k, v in (st.get("measured") or {}).items()}


def measured_at(T, hours, smeas):
    """The hour slots the collector's file made at T counted as measured: the state's
    slots up to T from SOLD_FLOOR_FROM on; before it (older runs) every hour whose
    previous hour had a run, from OLD_MEASURED_FROM (checked against the real 18 Nov
    02:00, 17:00 and 19:00 files)."""
    if T >= SOLD_FLOOR_FROM:
        return {t: s for t, s in smeas.items() if t <= T}
    hs = set(hours)
    return {t: 3600 for t in hours if OLD_MEASURED_FROM <= t <= T and (t - 3600) in hs}


def truncate(h, T):
    """The hist file as it stood after the run for hour T: hourly points up to T, daily
    points rebuilt from those (the collector's rule), the day of T partial."""
    hh = [p for p in h.get("h", []) if p[0] <= T]
    days = {}
    for p in hh:
        days.setdefault(p[0] - p[0] % 86400, []).append(p)
    d = []
    for ds in sorted(days):
        today = days[ds]
        priced = [p for p in today if p[1] > 0]
        d.append([ds,
                  int(sum(p[1] for p in priced) / len(priced)) if priced else 0,
                  int(sum(p[2] for p in priced) / len(priced)) if priced else 0,
                  int(sum(p[3] for p in today) / len(today)) if today else 0,
                  sum(p[4] for p in today)])
    out = {"h": hh, "d": d}
    if h.get("v"):
        out["v"] = h["v"]
    return out


def point_at(h, T):
    for p in reversed(h.get("h", [])):
        if p[0] == T:
            return p
        if p[0] < T:
            return None
    return None


def line_at(sid, h, T, measured, meta, auctions):
    """The addon line the collector would have written at hour T (None = no line)."""
    ht = truncate(h, T)
    p = point_at(ht, T)
    if p is None:
        return None, ht
    lo, hi, listed = p[1], p[2], p[3]
    line = FA.addon_item_line(sid, meta, ht, lo, hi, listed, T, auctions=auctions, measured=measured)
    return line, ht


def is_gear(sid, items_json):
    if sid >= 1000000:
        return True
    return (items_json.get(str(sid)) or {}).get("cls") in ("Armor", "Weapon")


def est_auctions(sid, listed, latest_item, items_json):
    """Separate listings at a past hour (not in hist): gear and versions are one unit per
    auction; other items use today's listings-per-unit ratio (0 = not known)."""
    listed = int(listed or 0)
    if listed <= 0:
        return 0
    if is_gear(sid, items_json):
        return listed
    if latest_item and latest_item.get("auctions") and latest_item.get("listed"):
        r = latest_item["auctions"] / latest_item["listed"]
        return max(1, min(listed, int(round(listed * r))))
    return 0


def advice(item, sales_hours, model=RM, tf="1D"):
    """(rec, sell prices, sell verdict) of the addon's advice model on one rebuilt line."""
    def windows(key):
        w = window(item, key)
        return w["cheap"], w["market"]
    rec = model.recommend(item, windows, tf, None, sales_hours=sales_hours)
    sp = model.sell_prices(rec, None, False, item["vendor"])
    sv = model.sell_verdict(item, sp, rec, sales_hours)
    return rec, sp, sv


def data_text(T, lines, sales_hours, updated=None, flips=None):
    """A Data.lua text in the collector's layout, for the harness's real Lua."""
    updated = updated if updated is not None else T + 25 * 60
    head = (f"{FA.ADDON_HEADER}\n"
            "ForeverAH_Data = {\n"
            "  v = 1,\n"
            f"  updated = {int(updated)},\n"
            f"  fetched = {int(updated) + 300},\n"
            f"  salesHours = {max(0, min(24, int(sales_hours or 0)))},\n"
            '  region = "us", house = 110, houseName = "Alliance Auction House", ruleset = "PvP",\n'
            f"  hourEnd = {int(T)},\n"
            f"  dayEnd = {int(T - T % 86400)},\n"
            + FA.addon_tables({}, flips or []) +
            "  items = {\n")
    return head + "".join(lines[k] for k in sorted(lines)) + "  },\n}\n"

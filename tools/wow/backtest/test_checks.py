"""Checks of the backtest's flip verdicts (checks.flip_rows) on hand-made rows: no
price data needed.  python tools/wow/backtest/test_checks.py

- A flip is counted once per item and day: hourly snapshots and the three gold
  levels find the same flips again, and counting each showing made 19 flips look
  like 37.
- Each gold level is judged on its own, with a 95% range.
- Out of sample says WAIT until 2 new days and 30 distinct flips at every gold level.
- The hidden deals are judged over 72 hours, WAIT until 30 distinct deals have them.
- The chart calls are judged under the words the addon ships ("High now", "Low now"); no call
  at all is a FAIL row.
- The backtest's constants (20g cap, rule G's 70%) are the ones Sidebar.lua and Recommend.lua ship.
- Without the package lupa the run still prints its table (the Lua rows SKIP)."""
import os
import subprocess
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import checks as C  # noqa: E402

passes = fails = 0
DAY = 86400
T0 = 1795046400           # 19 Nov 00:00 UTC (in sample)
T1 = 1795132800           # 20 Nov 00:00 UTC (out of sample)


def check(ok, msg):
    global passes, fails
    if ok:
        passes += 1
    else:
        fails += 1
        print("FAIL: " + msg)


def row(T, iid, gold, mtm, pred, ins, kind="gap", hz=24):
    return {"T": T, "id": iid, "kind": kind, "gold": gold, "first": mtm > 0, "cash": mtm, "mtm": mtm, "pred": pred,
            "inSample": ins, "hz": hz}


def rows_of(fl):
    return dict(((r[0], r[1].split(": ")[0]), r) for r in C.flip_rows(fl))


# in sample: 3 flips, each shown at 3 hours and 3 gold levels (27 showings); 2 of 3 win
shown = []
for h in range(3):
    for g in ("50g", "500g", "5000g"):
        shown += [row(T0 + h * 3600, 1, g, 10000, 8000, True), row(T0 + h * 3600, 2, g, 10000, 8000, True),
                  row(T0 + h * 3600, 3, g, -5000, 8000, True)]
r = rows_of({"shown": shown, "all": [], "deals": []})
ins = r[("Flip ideas (shown)", "in sample (the 4 snapshots the rules were fitted on)")]
check("50g: 3 flips, won 67%" in ins[2] and "500g: 3 flips" in ins[2] and "5000g: 3 flips" in ins[2],
      "in sample: each flip once a day, per gold (%s)" % ins[2])
check("95% range" in ins[2], "in sample: the range is printed (%s)" % ins[2])
check(ins[3] == "PASS", "in sample: 2 of 3 won, +15g real vs +24g predicted: PASS (%s)" % ins[3])

# out of sample: 2 days, 12 distinct flips a day at 500g, 10 at 50g: 22 < 30, so WAIT
shown = []
for d in range(2):
    for i in range(12):
        shown.append(row(T1 + d * DAY, 100 + i, "500g", 10000, 9000, False))
        shown.append(row(T1 + d * DAY + 3600, 100 + i, "500g", 10000, 9000, False))   # the same flip an hour later
        if i < 10:
            shown.append(row(T1 + d * DAY, 100 + i, "50g", 10000, 9000, False))
r = rows_of({"shown": shown, "all": [], "deals": []})
oos = r[("Flip ideas (shown)", "out of sample (snapshots after 19 Nov 03:00)")]
check(oos[3] == "WAIT" and "at least 20 distinct flips per gold" in oos[2],
      "out of sample: 20 distinct at 50g (not 44 showings): WAIT (%s: %s)" % (oos[3], oos[2]))
# 2 days of 15 distinct flips at each gold: 30, judged
shown = [row(T1 + d * DAY, 100 + i, g, 10000 if i % 3 else -10000, 3000, False)
         for d in range(2) for i in range(15) for g in ("50g", "500g")]
r = rows_of({"shown": shown, "all": [], "deals": []})
oos = r[("Flip ideas (shown)", "out of sample (snapshots after 19 Nov 03:00)")]
check(oos[3] == "PASS" and "50g: 30 flips, won 67%" in oos[2], "out of sample: 30 distinct per gold, 67%%: PASS (%s)" % oos[2])
shown = [dict(x, mtm=-1 if x["gold"] == "50g" else x["mtm"]) for x in shown]
r = rows_of({"shown": shown, "all": [], "deals": []})
oos = r[("Flip ideas (shown)", "out of sample (snapshots after 19 Nov 03:00)")]
check(oos[3] == "FAIL", "out of sample: one gold level losing fails the row (%s)" % oos[3])

# the hidden deals: judged over 72 hours only
deals = [row(T0 + (i % 2) * DAY, 300 + i, g, 5000, 4000, True, kind="deal", hz=40) for i in range(40) for g in ("50g", "500g")]
r = rows_of({"shown": [], "all": [], "deals": deals})
dl = [x for k, x in r.items() if k[0] == "Flip ideas (deals)"][0]
check(dl[3] == "WAIT" and "0 distinct deals per gold with 72h" in dl[2], "deals: 40 hours after them: WAIT (%s)" % dl[2])
deals = [dict(x, hz=72) for x in deals]
r = rows_of({"shown": [], "all": [], "deals": deals})
dl = [x for k, x in r.items() if k[0] == "Flip ideas (deals)"][0]
check(dl[3] == "PASS" and "50g: 40 flips, won 100%" in dl[2], "deals: 72 hours after 40 distinct: judged (%s)" % dl[2])

check(abs(C.wilson(12, 19)[0] - 41.0) < 1 and abs(C.wilson(12, 19)[1] - 81.0) < 1, "the 95%% range of 12 of 19 is about 41-81%% (%s)"
      % (C.wilson(12, 19),))

# the chart calls (checks.check_chart) on hand-made rows: the shipped words, "High now: usually
# falls back" and "Low now: still falling", are on the Sell and Buy sides (they once matched no
# row and the check printed nothing); no call at all is a FAIL row, not a missing one
class FakeFwd(object):
    def __init__(self, nxt):
        self.nxt = nxt

    def after(self, sid, T, H):
        return None, [(0, 0, p) for p in self.nxt[sid]]


class FakeCtx(object):
    pass


def chart_ctx(calls, rise):
    c = FakeCtx()
    c.set24, c.rows, nxt = {T0}, [], {}
    sid = 0
    for v, mult in calls:
        for g in ("gear", "fast", "slow"):
            for _ in range(120):
                sid += 1
                c.rows.append({"T": T0, "hi": 1000, "sid": sid, "verdict": v, "gear": g == "gear",
                               "perDay": 10 if g == "fast" else 1})
                nxt[sid] = [int(1000 * (mult + rise))] * 20
    c.F = FakeFwd(nxt)
    return c


RM = C.B.RM
got = C.check_chart(chart_ctx([(RM.CALL_HIGH, 0.9), (RM.CALL_LOW, 0.98), ("Hold", 1.5)], 0))
check([x[3] for x in got] == ["PASS", "PASS"] and "High now" in got[0][1] and "Low now" in got[1][1]
      and "stacks 5+/day -10.0%" in got[0][2], "chart: High now fell 10%%, Low now fell 2%%: 2 PASS rows (%s)" % (got,))
got = C.check_chart(chart_ctx([(RM.CALL_HIGH, 0.9), (RM.CALL_LOW, 0.98)], 0.15))
check([x[3] for x in got] == ["FAIL", "FAIL"], "chart: both followed by a rise: 2 FAIL rows (%s)" % (got,))
got = C.check_chart(chart_ctx([("Hold", 1.0), ("Some new words", 0.5)], 0))
check(len(got) == 1 and got[0][3] == "FAIL" and "no chart calls" in got[0][2],
      "chart: no call the check knows: one FAIL row, never no row (%s)" % (got,))

# the shipped constants the checks measure: the 20g gap flip cap and rule G's 70% (Sidebar.lua,
# Recommend.lua, the harness and rec_model), so the backtest never quietly tests a rule that is not shipped
import re  # noqa: E402
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
ADDON = os.environ.get("FAH_ADDON_DIR") or os.path.join(ROOT, "wow-addon", "GoblinLedger")


def lua_const(fname, name):
    m = re.search(r"^local %s = ([0-9.]+)" % name, open(os.path.join(ADDON, fname), encoding="ascii").read(), re.M)
    return float(m.group(1)) if m else None


cap = lua_const("Sidebar.lua", "GAP_COST_CAP")
hcap = re.search(r"^GAP_COST_CAP = (\d+)", open(os.path.join(ROOT, "tools", "wow", "addon_test", "harness.py"),
                                                 encoding="utf-8").read(), re.M)
check(cap == C.GAP_COST and hcap and int(hcap.group(1)) == C.GAP_COST,
      "the 20g cap: checks.GAP_COST %s, Sidebar.lua GAP_COST_CAP %s, harness %s" % (C.GAP_COST, cap, hcap and hcap.group(1)))
dump = lua_const("Recommend.lua", "DUMP_PCT")
check(dump == round(C.G_DUMP * 100) == RM.DUMP_PCT,
      "rule G's 70%%: checks.G_DUMP %s, Recommend.lua DUMP_PCT %s, rec_model %s" % (C.G_DUMP, dump, RM.DUMP_PCT))
share = lua_const("Sidebar.lua", "GAP_GOLD_SHARE")
hshare = re.search(r"^GAP_GOLD_SHARE = ([0-9.]+)", open(os.path.join(ROOT, "tools", "wow", "addon_test", "harness.py"),
                                                          encoding="utf-8").read(), re.M)
check(share is not None and hshare and float(hshare.group(1)) == share,
      "the 10%% gold share: Sidebar.lua %s, harness %s" % (share, hshare and hshare.group(1)))

# no lupa: the table still prints, the Lua rows SKIP (on the cached rows; skipped without a cache)
cache = os.path.join(HERE, "cache")
if os.path.exists(os.path.join(cache, "rows.pkl")):
    # (no snapshot saved, and the last table put back: this run is only a test)
    code = ("import sys, runpy; sys.path.insert(0, %r); sys.modules['lupa'] = None\n"
            "import flips; flips.save_latest = lambda *a, **k: None\n"
            "sys.argv = ['run_backtest.py', '--keep-cache']\n"
            "runpy.run_path(%r, run_name='__main__')" % (HERE, os.path.join(HERE, "run_backtest.py")))
    last = os.path.join(cache, "last_run.txt")
    kept = open(last, "rb").read() if os.path.exists(last) else None
    try:
        out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=1800).stdout
    finally:
        if kept is not None:
            with open(last, "wb") as fh:
                fh.write(kept)
    check("needs the Python package lupa" in out and "SKIP  Backtest model" in out and "SKIP  Flip ideas (shown)" in out
          and "Sell price (rule G)" in out, "no lupa: a plain note, the Lua rows SKIP, the rest runs (%s)" % out[-400:])
else:
    print("note: no backtest cache yet, the no-lupa run is left out")

print("%d passed, %d failed" % (passes, fails))
sys.exit(1 if fails else 0)

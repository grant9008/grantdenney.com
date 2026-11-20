"""Goblin Ledger backtest: replays the addon's advice on the price collector's real
history and prints PASS / FAIL for every rule the improvement plan rests on.

    python tools/wow/backtest/run_backtest.py [--data FOLDER] [--crafts Crafts.lua]
        [--snapshots FOLDER] [--max-hours N] [--quick]

--data       the collector's wowdata folder (default %LOCALAPPDATA%\\ForeverAH\\collector\\wowdata)
             or a clone of the wow-data branch. Only ever read.
--crafts     a Crafts.lua to test the craft profit rule with (default: the addon's own, when it has one)
--snapshots  another folder of flips-*.lua snapshots to score as well
--max-hours  at most this many decision hours (spread evenly; default 48, 0 = all)
--quick      leave out the model check
--keep-cache use the cached history and rows even when newer prices are in (quick re-runs)

Writes only to tools/wow/backtest/cache (rebuilt when the data or the code changes) and
adds the collector's latest flips to tools/wow/backtest/snapshots (once per hour)."""
import argparse
import os
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bt_lib as B  # noqa: E402
import checks as C  # noqa: E402
import crafts as CR  # noqa: E402
import dataset as DS  # noqa: E402
import flips as FL  # noqa: E402

# The model check, the flip ideas and the craft quote run the addon's real Lua, which
# needs the Python package lupa. Without it those rows say SKIP and the rest still runs.
try:
    import lupa  # noqa: F401,E402
    HAVE_LUA = True
except ImportError:
    HAVE_LUA = False
NO_LUA = "needs the Python package lupa: install it with  python -m pip install lupa"


def say(msg):
    print(msg, flush=True)


def table(rows):
    w1 = max(len(r[0]) for r in rows)
    out = []
    for rule, test, got, status in rows:
        out.append("%-4s  %-*s  %s" % (status, w1, rule, test))
        out.append("      %-*s  -> %s" % (w1, "", got))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", default=B.DEFAULT_WOWDATA)
    ap.add_argument("--crafts", default=None)
    ap.add_argument("--snapshots", default=None)
    ap.add_argument("--max-hours", type=int, default=48)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--keep-cache", action="store_true")
    a = ap.parse_args(argv)
    t0 = time.time()
    src = B.Source(a.data)
    say("Goblin Ledger backtest, data from %s" % src.path)
    if not HAVE_LUA:
        say("Note: the model check, the flip ideas and the craft profit are left out: " + NO_LUA)
    say("Saving today's flips for later re-tests ...")
    FL.save_latest(src, say)
    say("Reading the price history (cached after the first run) ...")
    hist = B.load_all_hist(src, say, a.keep_cache)
    hours = B.all_hours(hist)
    if not hours:
        raise SystemExit("No hourly prices in the history.")
    say("Rebuilding the addon's data at every decision hour (cached after the first run) ...")
    data = DS.load(src, hist, hours, a.max_hours, say, a.keep_cache)
    c = C.Ctx(data, hist)
    rows = []
    if not HAVE_LUA:
        rows.append(("Backtest model", "the Python advice equals the addon's Lua", NO_LUA, "SKIP"))
    elif not a.quick:
        say("Checking the backtest's advice model against the addon's Lua ...")
        rows += C.check_port(src, hist, hours, say)
    say("Testing the rules ...")
    rows += C.check_sell(c)
    rows += C.check_thresholds(c)
    if HAVE_LUA:
        say("Scoring flip ideas at the saved snapshots (the addon's real Lua) ...")
        fl = FL.run(src, hist, hours, (a.snapshots,) if a.snapshots else (), say)
        rows += C.flip_rows(fl)
    else:
        fl = {"snaps": []}
        rows.append(("Flip ideas (shown)", "the sidebar's flip ideas made money", NO_LUA, "SKIP"))
    rows += C.check_badge(c)
    rows += C.check_chart(c)
    crafts = a.crafts or CR.DEFAULT_CRAFTS
    if not HAVE_LUA:
        rows.append(("Craft profit", "green really pays 2h later", NO_LUA, "SKIP"))
    elif os.path.exists(crafts):
        say("Testing the craft profit rule with %s ..." % crafts)
        try:
            rows += C.craft_rows(CR.run(src, hist, hours, crafts, say))
        except Exception as e:  # a Crafts.lua this port cannot read
            rows.append(("Craft profit", "green really pays 2h later", "could not read %s: %s" % (crafts, e), "SKIP"))
    else:
        rows.append(("Craft profit", "green really pays 2h later",
                     "no Crafts.lua in this checkout yet (the crafts build); --crafts FILE tests one", "SKIP"))
    snaps = fl["snaps"]
    head = ["", "Goblin Ledger backtest: data %s to %s UTC (%d hours of history)" % (
                B.fmt_t(hours[0]), B.fmt_t(hours[-1]), len(hours)),
            "Decision hours %d (24h set %d, 12h set %d); flip snapshots %d, %d scored (%d in sample)." % (
                len(c.decision), len(c.set24), len(c.set12), len(snaps), sum(1 for s in snaps if s[3] is not None),
                sum(1 for s in snaps if s[3])), ""]
    counts = dict((k, sum(1 for r in rows if r[3] == k)) for k in ("PASS", "FAIL", "WAIT", "SKIP", "INFO"))
    tail = ["", "%d PASS, %d FAIL, %d WAIT, %d SKIP, %d INFO  (%.0f s)" % (
        counts["PASS"], counts["FAIL"], counts["WAIT"], counts["SKIP"], counts["INFO"], time.time() - t0)]
    text = head + table(rows) + tail
    for line in text:
        say(line)
    os.makedirs(B.CACHE, exist_ok=True)
    with open(os.path.join(B.CACHE, "last_run.txt"), "w", encoding="ascii", errors="replace") as fh:
        fh.write("\n".join(text) + "\n")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())

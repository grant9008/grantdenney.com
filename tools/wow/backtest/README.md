# Goblin Ledger backtest

Replays the addon's advice on the real hourly price history the collector keeps, and
prints PASS or FAIL for every rule the improvement plan (wf40, 19 Nov 2026) rests on.

**Run it:** double-click `Run-Backtest.bat`. A run on new prices takes 10 to 45
minutes (it reads 40,000 history files and rebuilds 1.3 million item-hours); a run on
the same prices reuses `cache/` and takes under a minute (`--keep-cache` reuses it even
after new prices). Or: `python tools/wow/backtest/run_backtest.py [--data FOLDER]
[--crafts FILE] [--snapshots FOLDER] [--max-hours N] [--quick] [--keep-cache]`.

**Reads only:** the collector's `%LOCALAPPDATA%\ForeverAH\collector\wowdata` (or a
wow-data clone given with `--data`): `hist/`, `state.json`, `items.json`,
`ForeverAH_Data.lua`. Nothing there is ever written.
**Writes:** `cache/` (not committed: the scripts rebuild it) and one trimmed copy of
the latest flips per hour in `snapshots/` (small; commit them now and then).

## What it tests

| Rule | Pass when |
|---|---|
| Backtest model | The Python advice (`addon_test/rec_model.py`) equals the addon's Lua on about 120 item-hours |
| Sell price, rule G (Sell now, or the 24h median when the cheapest is under 70% of it) | Value counting unsold stock >= the old Suggested price and >= Sell now in every segment; stacks selling 50+/day sell within a day >= 60% (24h and 12h sets) |
| Auction it thresholds | At G's price, every band from 1.15x (10+/day), 1.30x (1-9/day), 2.00x (under 1/day) the vendor price pays at least the vendor price, at a 60% and a 30% deposit |
| Auction it as shipped | Every band the addon's own verdict calls "Auction it" pays at least the vendor price (fails until the new thresholds ship) |
| Flip ideas (shown) | The sidebar's gap ideas (real Lua, at 50g, 500g and 5000g) make money counting unsold stock >= 60% of the time, and the predicted day profit is within 2x of the real one. In sample: the 4 snapshots the rules were fitted on. Out of sample: later snapshots, WAIT until 2 new days and 10 ideas |
| Flip filters | Spike-flagged gap flips sell less often than the rest; gap flips relisted <= 1.25x the 24h median and costing <= 20g win >= 60% |
| Badge | At +40% over the 24h median the next day's median falls 20%+ and half go half way back; at -40% there is no rebound |
| Chart calls | Sell calls were followed by falls, Buy calls never by a rise |
| Craft profit | A green "profit" really pays 2h later >= 90% of the time, and at least as often as a TSM-style claim (SKIP until Crafts.lua is in this checkout, or pass `--crafts`) |

Thresholds sit at the top of `checks.py`. The old rules (Suggested price, "Auction it"
at 110%) come from `baseline_rec_model.py`, a frozen copy of the advice of 19 Nov 2026.

## Re-test at 7 days

The plan's numbers rest on 2 days of a falling launch-week market. Run it again from
26 Nov. The flip test needs saved snapshots: each run saves the collector's latest
flips, so run it once a day for 2-3 days. When "Flip ideas (shown), out of sample"
fails, turn gap flips off and keep deals and crafts (plan item 1). When it passes,
`GAP_COST_CAP` in Sidebar.lua may go up.

## Files

`run_backtest.py` (the table), `checks.py` (the rules), `dataset.py` (the rows),
`bt_lib.py` (rebuilds past Data.lua lines), `fwd.py` (would it have sold),
`flips.py` (snapshots and flip scoring), `crafts.py` (the craft quote),
`baseline_rec_model.py`, `snapshots/`.

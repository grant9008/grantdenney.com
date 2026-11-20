# Goblin Ledger backtest

Replays the addon's advice on the real hourly price history the collector keeps, and
prints PASS or FAIL for every rule the improvement plan (wf40, 19 Nov 2026) rests on.

**Needs:** Python 3 with the package `lupa` (`python -m pip install lupa`): the model
check, the flip ideas and the craft quote run the addon's real Lua in it. Without it
those rows say SKIP and the rest still runs.

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
| Flip ideas (shown) | The sidebar's gap ideas (real Lua), each item once a day and each gold level (50g, 500g, 5000g) on its own, make money counting unsold stock >= 60% of the time, and the predicted day profit is within 2x of the real one; the 95% range is printed. In sample: the 4 snapshots the rules were fitted on (few flips, a wide range). Out of sample: later snapshots, WAIT until 2 new days and 30 distinct flips at every gold level |
| Flip ideas (deals) | The deals the sidebar keeps hidden (`DEALS_SHOWN` in Sidebar.lua), as they would show with it on: the same test over 72 hours, since they sell in days. WAIT until 30 distinct deals per gold level have 72 hours after them |
| Flip filters | Spike-flagged gap flips sell less often than the rest; gap flips relisted <= 1.25x the 24h median and costing <= 20g in all win >= 60% |
| Badge | At +40% over the 24h median the next day's median falls 20%+ and half go half way back; at -40% there is no rebound |
| Chart calls | "High now: usually falls back" calls were followed by falls, "Low now: still falling" calls never by a rise (FAIL when no call is found) |
| Craft profit | A green "profit" really pays 2h later >= 90% of the time, and at least as often as a TSM-style claim (SKIP until Crafts.lua is in this checkout, or pass `--crafts`) |

Thresholds sit at the top of `checks.py`. The old rules (Suggested price, "Auction it"
at 110%) come from `baseline_rec_model.py`, a frozen copy of the advice of 19 Nov 2026.

## Re-test at 7 days

The plan's numbers rest on 2 days of a falling launch-week market. Run it again from
26 Nov. The flip test needs saved snapshots: each run saves the collector's latest
flips, so run it once a day for 2-3 days. When "Flip ideas (shown), out of sample"
fails, turn gap flips off (plan item 1). When it passes, `GAP_COST_CAP` may go up.
When "Flip ideas (deals)" passes, `DEALS_SHOWN` in Sidebar.lua may be turned on.
The price-history ideas (buy at Target Buy, sell at the sell target) are hidden too
(`HISTORY_SHOWN` in Sidebar.lua): no row here tests them, and "Low now: still falling" says
low prices kept falling over the next day. Add a row for them before turning them on.

The 20g cap is written in these places; change them together:
- `wow-addon/GoblinLedger/Sidebar.lua`: `GAP_COST_CAP` (the help tip is built from it).
- `tools/wow/addon_test/harness.py`: `GAP_COST_CAP` (its `FLIP_HELP` is built from it), and
  the hand-made flips at the 20g edge in the flip-experimental and flip-day-drops scenarios.
- `tools/wow/backtest/checks.py`: `GAP_COST` (the flip filter row).

## Files

`run_backtest.py` (the table), `checks.py` (the rules), `dataset.py` (the rows),
`bt_lib.py` (rebuilds past Data.lua lines), `fwd.py` (would it have sold),
`flips.py` (snapshots and flip scoring), `crafts.py` (the craft quote),
`baseline_rec_model.py`, `snapshots/`. `test_checks.py` checks the flip and chart verdicts on
hand-made rows (no price data needed), and that the cap and rule G constants here are the shipped ones.

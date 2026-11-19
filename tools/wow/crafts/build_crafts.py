"""Builds the recipe table in wow-addon/GoblinLedger/Crafts.lua (the block between
"-- BEGIN DATA" and "-- END DATA") from the recipe research.

Inputs (all read only):
  --recipes   recipes.json, made by tools/wow/crafts/build_recipes.py from WoW Forever's own
              client tables (wago.tools CSV exports of build 1.60.1.70959), the Classic Era
              tables, the Questie Forever database and a Goblin Ledger Data.lua snapshot.
              Every recipe that makes an item in Mining, Blacksmithing, Engineering,
              Leatherworking, Tailoring, Alchemy, Cooking, First Aid and Enchanting: mats and
              counts, product and count range, how it is learned, recipe items, vendors.
              It is 8.8 MB, so it is not committed.
  --forever-sparse  Forever ItemSparse.csv (wago.tools, wowf 1.60.1.70959): bonding,
              RequiredAbility (the specialization a recipe item needs), MinFactionID /
              MinReputation, AllowableRace, RequiredSkillRank of recipe items.
  --era-sparse      Classic Era ItemSparse.csv (wago.tools 1.15.x): the same fields when the
              Forever export has no row for an item (it has 19,327 rows against 32,008 Item
              rows), and RandomSelect / ItemRandomSuffixGroupID (random-suffix gear).
  --faction   Forever Faction.csv (wago.tools): which races can earn a faction's standing,
              so a recipe gated on Kirin Tor is Alliance only and one on Earthen Ring Horde only.
  --out       the Crafts.lua to rewrite (default: wow-addon/GoblinLedger/Crafts.lua).

    python tools/wow/crafts/build_crafts.py --recipes recipes.json --forever-sparse ItemSparse.csv \\
        --era-sparse era/ItemSparse.csv --faction Faction.csv

The fixes from the recipe check (recipes_verify.md) are applied here:
  1. availability by evidence: a product exists when it has an Item row and an ItemSparse row,
     an AH sighting or a Questie entry, and every recipe kept has at least one piece of evidence
     (product or recipe item seen on the AH, or in Questie). A missing ItemSparse row alone never
     drops a recipe; bonding then comes from the Era table.
  2. requires_spec: the recipe item's RequiredAbility (Armorsmith 9788, Weaponsmith 9787, Master
     Sword/Hammer/Axesmith 17039-17041, Gnomish 20219, Goblin 20222, the leatherworking specs).
  3. reputation and faction: the recipe item's MinFactionID / MinReputation, and the side (from
     Faction.csv race masks, the recipe item's AllowableRace and the recipe's own race mask).
  4. required skill: the recipe item's RequiredSkillRank, Era's (marked E) when Forever has no row.
  5. junk names (NYI, DEPRECATED, OLD, UNUSED, [PH], DNT) on products, mats and recipe items.
  6. vendor mats: the Alliance vendor price only when Alliance vendors with spawns sell it and
     the stock is not limited or rotating (no Darkmoon Faire only, no limited_suspect).
  7. random-suffix products are flagged (S): no profit claims for them.
  8. product bonding (B: bind on pickup or quest; U: not known and never seen on the AH).
Kept: recipes whose product or a mat is on the AH (seen in the last 7 days), plus every recipe
a recipe item (Plans, Pattern, Formula, Schematic, Recipe, Manual) teaches.
"""
import argparse
import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
DEFAULT_OUT = os.path.join(ROOT, "wow-addon", "GoblinLedger", "Crafts.lua")

PROF = {186: "Mining", 164: "Blacksmithing", 202: "Engineering", 165: "Leatherworking", 197: "Tailoring",
        171: "Alchemy", 185: "Cooking", 129: "First Aid", 333: "Enchanting"}
JUNK = re.compile(r"\bNYI\b|DEPRECATED|Deprecated|\bOLD\b|\(old\)|UNUSED|\bUnused\b|\[PH\]|\bDNT\b|\[DNT\]")
# Race mask bits (ChrRaces PlayableRaceBit): Human 0, Orc 1, Dwarf 2, Night Elf 3, Undead 4,
# Tauren 5, Gnome 6, Troll 7, and Forever's own Alliance / Horde races at 32 / 33.
ALLY_BITS = (0, 2, 3, 6, 32)
HORDE_BITS = (1, 4, 5, 7, 33)
LOCKED = -42000   # Hated with no way up: that race can never earn the standing


def I(x, d=0):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return d


def read_sparse(path, era=False):
    keep = ("Bonding", "RequiredAbility", "MinFactionID", "MinReputation", "AllowableRace_0", "AllowableRace_1",
            "RequiredSkill", "RequiredSkillRank", "RandomSelect", "ItemRandomSuffixGroupID")
    out = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            d = {k: I(row.get(k)) for k in keep}
            d["name"] = row.get("Display_lang") or None
            out[I(row["ID"])] = d
    return out


def mask64(lo, hi):
    return (I(lo) & 0xFFFFFFFF) | ((I(hi) & 0xFFFFFFFF) << 32)


def read_factions(path):
    """faction id -> "A" (only Alliance races can earn it), "H", or "" (both)."""
    out = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            masks = [(mask64(row["ReputationRaceMasks%d_0" % k], row["ReputationRaceMasks%d_1" % k]),
                      I(row["ReputationBase_%d" % k])) for k in range(4)]

            def allowed(bit):
                for m, base in masks:
                    if m & (1 << bit):
                        return base > LOCKED
                return True
            a = any(allowed(b) for b in ALLY_BITS)
            h = any(allowed(b) for b in HORDE_BITS)
            out[I(row["ID"])] = "A" if a and not h else ("H" if h and not a else "")
    return out


def race_side(mask, bits=64):
    """A race mask (-1 or 0 = everyone) -> "A", "H" or "". SkillLineAbility's RaceMasks_0 is the
    low 32 bits only, as a signed number (bits=32)."""
    if mask in (0, -1):
        return ""
    mask &= (1 << bits) - 1
    if mask == (1 << bits) - 1:
        return ""
    a = any(mask & (1 << b) for b in ALLY_BITS if b < bits)
    h = any(mask & (1 << b) for b in HORDE_BITS if b < bits)
    return "A" if a and not h else ("H" if h and not a else "")


def lua_str(s):
    out = []
    for ch in s:
        o = ord(ch)
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif 32 <= o < 127:
            out.append(ch)
        else:
            for b in ch.encode("utf-8"):
                out.append("\\%d" % b)
    return '"' + "".join(out) + '"'


def junk(name):
    """A test or retired name. No name at all is not junk: the export just has no row for it."""
    return bool(name) and bool(JUNK.search(name))


def name_of(x, fsp, esp):
    """The research's name, else the Forever or Era ItemSparse name (None: the game names it)."""
    n = x.get("name")
    if n:
        return n
    for t in (fsp, esp):
        r = t.get(x.get("id"))
        if r and r.get("name"):
            return r["name"]
    return None


def build(recipes, fsp, esp, factions):
    rows, names, vendor, ritems = {}, {}, {}, {}
    stats = {"in": len(recipes), "junk": 0, "no_evidence": 0, "not_relevant": 0, "retired": 0, "kept": 0,
             "spec": 0, "rep": 0, "side_A": 0, "side_H": 0, "bop": 0, "suffix": 0, "era_rank": 0, "vendor_mats": 0}
    for r in recipes:
        sp, sl = r["spell_id"], r["skill_line"]
        if sl not in PROF:
            continue
        p = r["product"]
        rg = r["reagents"]
        for x in [p] + rg + list(r.get("recipe_items") or []):
            x["name"] = name_of(x, fsp, esp)
        if r.get("available") == "excluded" or not r.get("name") or junk(r.get("name")) or junk(p.get("name")) \
                or any(junk(x.get("name")) for x in rg):
            stats["junk"] += 1
            continue
        if r.get("acquire_method") == 3 or r.get("available") == "retired":
            stats["retired"] += 1
            continue
        # fix 1: a product exists with an Item row and (ItemSparse, an AH sighting or Questie)
        evidence = r.get("evidence") or []
        has_item_row = p.get("class") is not None
        exists = has_item_row and (p.get("in_forever_itemsparse") or p.get("ah") or "product in Questie Forever DB" in evidence)
        if not exists or not evidence:
            stats["no_evidence"] += 1
            continue
        items = [x for x in r.get("recipe_items") or [] if not junk(x.get("name"))]
        taught = len(items) > 0
        relevant = bool(p.get("ah")) or any(x.get("ah") for x in rg)
        if not relevant and not taught:
            stats["not_relevant"] += 1
            continue
        flags = []
        learned = r.get("learned_by") or []
        if "starts_known" in learned:
            flags.append("K")
        if not taught:
            flags.append("T")   # learned from a trainer or with the skill: it may be known before a scan
        # product bonding: Forever's row, else Era's (fix 1)
        pid = p["id"]
        bond = None
        if pid in fsp:
            bond = fsp[pid]["Bonding"]
        elif pid in esp:
            bond = esp[pid]["Bonding"]
        if bond in (1, 4, 5):
            flags.append("B")
            stats["bop"] += 1
        elif bond is None and not p.get("ah"):
            flags.append("U")
        e = esp.get(pid)
        if e and (e["RandomSelect"] or e["ItemRandomSuffixGroupID"]):
            flags.append("S")
            stats["suffix"] += 1
        # gates from the recipe items: spec, reputation, side, required skill (fixes 2-4)
        spec, rep, rank, rank_era = None, None, None, False
        item_sides = []
        all_spec, all_rep = True, True
        for x in items:
            src = fsp.get(x["id"])
            era = False
            if src is None:
                src, era = esp.get(x["id"]), True
            if src is None:
                all_spec = all_rep = False
                item_sides.append("")
                continue
            ab = src["RequiredAbility"]
            if ab:
                spec = spec or ab
            else:
                all_spec = False
            mf = src["MinFactionID"]
            if mf:
                rep = rep or (mf, src["MinReputation"])
            else:
                all_rep = False
            s = factions.get(mf, "") if mf else ""
            s = s or race_side(mask64(src["AllowableRace_0"], src["AllowableRace_1"])
                               if (src["AllowableRace_0"] or src["AllowableRace_1"]) else 0)
            item_sides.append(s)
            rk = src["RequiredSkillRank"]
            if rk and (rank is None or rk < rank):
                rank, rank_era = rk, era
        if not all_spec:
            spec = None
        if not all_rep:
            rep = None
        side = ""
        if items and all(s == "A" for s in item_sides):
            side = "A"
        elif items and all(s == "H" for s in item_sides):
            side = "H"
        own = race_side(I(r.get("race_mask")), 32)
        if own:
            side = own if side in ("", own) else "X"   # both gates disagree: nobody (never kept)
        if side == "X":
            continue
        if side:
            flags.append(side)
            stats["side_" + side] += 1
        if rank is None and (r.get("skill") or {}).get("min_skill_line_rank", 1) > 1:
            rank = r["skill"]["min_skill_line_rank"]
        if rank is not None and rank_era:
            flags.append("E")
            stats["era_rank"] += 1
        if spec:
            stats["spec"] += 1
        if rep:
            stats["rep"] += 1
        cmin, cmax = int(p.get("count_min") or 1), int(p.get("count_max") or 1)
        mats = ",".join("%d:%d" % (x["id"], x["count"]) for x in rg)
        yellow = (r.get("skill") or {}).get("yellow") or 0
        rows[sp] = "|".join([str(sl), str(pid), str(cmin), str(cmax), mats, "".join(flags),
                             str(rank or ""), str(spec or ""), ("%d:%d" % rep) if rep else "",
                             ",".join(str(x["id"]) for x in items), str(yellow)])
        names[pid] = p.get("name")
        for x in rg:
            names[x["id"]] = x.get("name")
            v = x.get("vendor") or {}
            if (v.get("alliance_vendors") or 0) > 0 and not v.get("darkmoon_faire_only") and not v.get("limited_suspect") \
                    and v.get("buy_price_per_unit"):
                vendor[x["id"]] = max(1, int(round(v["buy_price_per_unit"])))
        for x in items:
            names[x["id"]] = x.get("name")
            src = fsp.get(x["id"]) or esp.get(x["id"])
            ritems[x["id"]] = "B" if (src and src["Bonding"] in (1, 4, 5)) else ""
        stats["kept"] += 1
    stats["vendor_mats"] = len(vendor)
    return rows, names, vendor, ritems, stats


def render(rows, names, vendor, ritems, meta):
    out = ["-- BEGIN DATA",
           "-- Generated by tools/wow/crafts/build_crafts.py from the recipe research (recipes.json: WoW Forever",
           "-- %s client tables, built %s). Do not edit by hand: run the script again." % (meta.get("client_build", "1.60.1.70959"), meta.get("built", "?")),
           "-- R[recipe spell id] = \"skill line|product|count min|count max|mats (id:count,...)|flags|required rank|",
           "--   spec spell|faction:standing|recipe items|yellow rank\". Flags: K known with the profession, T learned",
           "--   from a trainer (may be known before the professions window was opened), B product binds on pickup,",
           "--   U product bonding not known (never on the AH), S random-suffix product, A Alliance only, H Horde only,",
           "--   E the required rank is Classic Era's (Forever often 25 lower).",
           "local R = {"]
    for sp in sorted(rows):
        out.append("[%d]=%s," % (sp, lua_str(rows[sp])))
    out.append("}")
    out.append("-- V[item id] = Alliance vendor price per unit (copper), for mats vendors keep in stock.")
    out.append("local V = {")
    line = []
    for iid in sorted(vendor):
        line.append("[%d]=%d," % (iid, vendor[iid]))
        if len(line) == 10:
            out.append("".join(line))
            line = []
    if line:
        out.append("".join(line))
    out.append("}")
    out.append("-- RI[recipe item id] = \"B\" when it binds on pickup (it is never on the AH), else \"\".")
    out.append("local RI = {")
    line = []
    for iid in sorted(ritems):
        line.append("[%d]=%s," % (iid, lua_str(ritems[iid])))
        if len(line) == 10:
            out.append("".join(line))
            line = []
    if line:
        out.append("".join(line))
    out.append("}")
    out.append("-- N[item id] = name, for items the price data and the game have not named yet.")
    out.append("local N = {")
    for iid in sorted(names):
        if names[iid]:
            out.append("[%d]=%s," % (iid, lua_str(names[iid])))
    out.append("}")
    out.append("-- END DATA")
    return "\n".join(out)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--recipes", required=True)
    ap.add_argument("--forever-sparse", required=True)
    ap.add_argument("--era-sparse", required=True)
    ap.add_argument("--faction", required=True)
    ap.add_argument("--out", default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    with open(a.recipes, encoding="utf-8") as f:
        j = json.load(f)
    fsp = read_sparse(a.forever_sparse)
    esp = read_sparse(a.era_sparse, era=True)
    factions = read_factions(a.faction)
    rows, names, vendor, ritems, stats = build(j["recipes"], fsp, esp, factions)
    meta = {"built": (j.get("meta") or {}).get("built", "?"), "client_build": "1.60.1.70959"}
    block = render(rows, names, vendor, ritems, meta)
    with open(a.out, "rb") as f:
        src = f.read().decode("ascii")
    m = re.search(r"-- BEGIN DATA.*?-- END DATA", src, re.S)
    if not m:
        raise SystemExit("%s has no -- BEGIN DATA / -- END DATA block" % a.out)
    # the file's own line ends (CRLF in a Windows checkout, LF in git)
    if "\r\n" in src:
        block = block.replace("\n", "\r\n")
    new = src[:m.start()] + block + src[m.end():]
    with open(a.out, "wb") as f:
        f.write(new.encode("ascii"))
    print("recipes: %(in)d in, %(kept)d kept; dropped %(junk)d junk, %(retired)d retired, %(no_evidence)d without "
          "evidence, %(not_relevant)d not on the AH; %(spec)d need a spec, %(rep)d a reputation, %(side_A)d Alliance "
          "only, %(side_H)d Horde only, %(bop)d bind on pickup, %(suffix)d random suffix, %(era_rank)d Era ranks, "
          "%(vendor_mats)d vendor mats" % stats)
    print("names: %d, recipe items: %d, size %d bytes" % (len(names), len(ritems), len(block)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Shared read-only loaders for build_recipes.py (which makes recipes.json for build_crafts.py).
Forever DB2 CSVs (wago.tools, build 1.60.1.70959), Classic Era CSVs, Questie Forever dump,
GoblinLedger Data.lua prices. Writes nothing.

Inputs: GL_RECIPE_SOURCES names the folder the downloads sit in, laid out as
  wf12/web/wago/      Forever CSVs: Item, ItemEffect, ItemSparse, ItemXItemEffect, SkillLineAbility, Spell,
                      SpellEffect, SpellName, SpellReagents ...
  wf15/web/wago/      more Forever CSVs: SkillLine, SpellCategories, SpellCategory, SpellCooldowns ...
  wf20/web/wago/      SpellCastingRequirements, SpellFocusObject, SpellLearnSpell, SpellTotems, TotemCategory
  wf12/web/wago_era/, wf15/web/era/, wf20/web/era/   the same tables from Classic Era 1.15.x
  wf12/data/questie_dump.json                       the Questie Forever database as JSON
(a table is taken from the first folder that has it). GL_DATA_LUA: a Goblin Ledger Data.lua (prices,
names, sales); the installed one by default."""
import csv, json, os, re, collections

SCR = os.environ.get("GL_RECIPE_SOURCES") or os.getcwd()   # the downloads (see above)
WF20 = os.path.join(SCR, "wf20")
FOREVER_DIRS = [os.path.join(SCR, "wf12", "web", "wago"), os.path.join(SCR, "wf15", "web", "wago"),
                os.path.join(WF20, "web", "wago")]
ERA_DIRS = [os.path.join(SCR, "wf12", "web", "wago_era"), os.path.join(SCR, "wf15", "web", "era"),
            os.path.join(WF20, "web", "era")]
QUESTIE = os.path.join(SCR, "wf12", "data", "questie_dump.json")
DATA_LUA = os.environ.get("GL_DATA_LUA") or r"C:\Program Files (x86)\World of Warcraft\_forever_\Interface\AddOns\GoblinLedger\Data.lua"


def rd(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def tab(name, era=False):
    for d in (ERA_DIRS if era else FOREVER_DIRS):
        p = os.path.join(d, name + ".csv")
        if os.path.exists(p) and os.path.getsize(p) > 40:
            return rd(p)
    return None


def I(x, d=0):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return d


def load_prices(path=DATA_LUA):
    """items: id -> dict(name, lo, hi, listed, sold24, avg7, vsell, auctions); plus header fields."""
    items, hdr = {}, {}
    in_items = False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not in_items:
                for k in ("updated", "fetched", "salesHours", "hourEnd", "dayEnd", "house"):
                    m = re.match(r"\s*%s = (\d+)," % k, line)
                    if m:
                        hdr[k] = int(m.group(1))
                m = re.match(r'\s*region = "(\w+)", house = (\d+), houseName = "([^"]*)", ruleset = "(\w+)"', line)
                if m:
                    hdr.update(region=m.group(1), house=int(m.group(2)), houseName=m.group(3), ruleset=m.group(4))
                if line.startswith("  items = {"):
                    in_items = True
                continue
            if line.startswith("  }"):
                break
            m = re.match(r'\s*\[(\d+)\] = "(.*)",?\s*$', line)
            if not m:
                continue
            f_ = m.group(2).split("|")
            items[int(m.group(1))] = {"name": f_[0], "lo": I(f_[1]), "hi": I(f_[2]), "listed": I(f_[3]),
                                      "sold24": I(f_[4]), "avg7": I(f_[5]), "vsell": I(f_[6]),
                                      "auctions": I(f_[9]) if len(f_) > 9 else 0}
    return items, hdr


class DB:
    def __init__(self, era=False):
        t = lambda n: tab(n, era) or []
        self.era = era
        self.spellname = {I(r["ID"]): r["Name_lang"] for r in t("SpellName")}
        self.spell = {I(r["ID"]): r for r in t("Spell")}
        self.eff = collections.defaultdict(list)
        for r in t("SpellEffect"):
            if r.get("DifficultyID", "0") not in ("0", ""):
                continue
            self.eff[I(r["SpellID"])].append(r)
        for k in self.eff:
            self.eff[k].sort(key=lambda e: I(e["EffectIndex"]))
        self.reag = {}
        for r in t("SpellReagents"):
            out = []
            for k in range(8):
                iid, c = I(r.get("Reagent_%d" % k)), I(r.get("ReagentCount_%d" % k))
                if iid > 0 and c > 0:
                    out.append((iid, c))
            self.reag[I(r["SpellID"])] = out
        self.sla = t("SkillLineAbility")
        self.sparse = {I(r["ID"]): r for r in t("ItemSparse")}
        self.item = {I(r["ID"]): r for r in t("Item")}
        # item effects: item -> [(spell, trigger)]
        self.item_spells = collections.defaultdict(list)
        ie = t("ItemEffect")
        if ie and "ParentItemID" in ie[0]:
            for r in ie:
                self.item_spells[I(r["ParentItemID"])].append((I(r["SpellID"]), I(r["TriggerType"])))
        else:
            byid = {I(r["ID"]): r for r in ie}
            for r in t("ItemXItemEffect"):
                e = byid.get(I(r["ItemEffectID"]))
                if e:
                    self.item_spells[I(r["ItemID"])].append((I(e["SpellID"]), I(e["TriggerType"])))
        self.totems = {I(r["SpellID"]): r for r in t("SpellTotems")}
        self.castreq = {I(r["SpellID"]): r for r in t("SpellCastingRequirements")}
        self.focus = {I(r["ID"]): r["Name_lang"] for r in t("SpellFocusObject")}
        self.totemcat = {I(r["ID"]): r["Name_lang"] for r in t("TotemCategory")}
        self.skillline = {I(r["ID"]): r["DisplayName_lang"] for r in t("SkillLine")}

    def iname(self, iid):
        r = self.sparse.get(iid)
        return r["Display_lang"] if r else None


def load_questie():
    with open(QUESTIE, encoding="utf-8") as f:
        return json.load(f)

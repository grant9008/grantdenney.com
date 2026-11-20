"""Builds recipes.json, the recipe research build_crafts.py makes Crafts.lua's table from, out of
WoW Forever's own client tables (see recipe_sources.py for the inputs and GL_RECIPE_SOURCES).
Read-only on every input. Run it again when Forever patches.
Usage: GL_RECIPE_SOURCES=<downloads> python tools/wow/crafts/build_recipes.py
Output: GL_RECIPES_OUT, else <downloads>/wf20/recipes.json (8.8 MB: not committed)."""
import sys, os, json, collections, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import recipe_sources as lib
from recipe_sources import I

PROF = {186: "Mining", 164: "Blacksmithing", 202: "Engineering", 165: "Leatherworking", 197: "Tailoring",
        171: "Alchemy", 185: "Cooking", 129: "First Aid", 333: "Enchanting"}
BAD_NAME = ("[DNT]", "(OLD)", "(old)", "TEST", "Test ", "[PH]", "QA ", "Deprecated", "DEPRECATED", "NYI", "Unused", "UNUSED")
BOND = {0: "none", 1: "BoP", 2: "BoE", 3: "BoU", 4: "quest", 5: "quest"}
QUAL = {0: "poor", 1: "common", 2: "uncommon", 3: "rare", 4: "epic", 5: "legendary", 6: "artifact", 7: "heirloom"}
ACQ = {0: "trainer_or_recipe", 1: "starts_known", 2: "auto_on_skill", 3: "retired"}

F = lib.DB()
E = lib.DB(era=True)
Q = lib.load_questie()
QI, QN = Q["Item"], Q["Npc"]
P, PH = lib.load_prices()


def qfield(entry, f):
    s = entry.get("S")
    if isinstance(s, dict):
        return s.get(f)
    if isinstance(s, list):
        k = int(f) - 1
        return s[k] if 0 <= k < len(s) else None
    return None


def vendor_info(iid):
    """Questie Forever vendors (item field 14): Alliance-usable vendors = friendly to A and with spawns."""
    q = QI.get(str(iid))
    if not q or not q.get("14"):
        return None
    ids = q["14"]
    ally, horde, unknown, subs = [], 0, 0, set()
    for n in ids:
        e = QN.get(str(n))
        if not e:
            unknown += 1
            continue
        fac = qfield(e, "13") or ""
        has_spawn = bool(e.get("7"))
        if "A" in fac and has_spawn:
            ally.append(n)
            subs.add(qfield(e, "14") or "")
        elif "H" in fac:
            horde += 1
    darkmoon = bool(ally) and all("Darkmoon" in (qfield(QN[str(n)], "14") or "") for n in ally)
    return {"vendors": len(ids), "alliance_vendors": 0 if darkmoon else len(ally), "horde_only_vendors": horde,
            "unknown_npcs": unknown, "example_alliance_npcs": ally[:3], "alliance_vendor_titles": sorted(subs)[:6],
            "darkmoon_faire_only": darkmoon}


def item_basic(iid):
    s = F.sparse.get(iid)
    it = F.item.get(iid)
    d = {"id": iid, "name": (s["Display_lang"] if s else None) or (P.get(iid) or {}).get("name") or None,
         "in_forever_itemsparse": bool(s)}
    if s:
        b = I(s["Bonding"])
        d.update(bonding=BOND.get(b, str(b)), quality=QUAL.get(I(s["OverallQualityID"]), s["OverallQualityID"]),
                 stack=I(s["Stackable"]), vendor_sell=I(s["SellPrice"]))
        bp, vs = I(s["BuyPrice"]), max(1, I(s["VendorStackCount"]))
        v = vendor_info(iid)
        if v and bp > 0:
            v["buy_price_per_unit"] = round(bp / vs, 2)
            v["buy_price"] = bp
            v["vendor_stack"] = vs
            # limited / rotating stock suspicion: few vendors and the AH pays well above the vendor price,
            # or Black Market / seasonal sellers only
            pa = P.get(iid) or {}
            titles = " ".join(v.get("alliance_vendor_titles") or [])
            lim = []
            if v["alliance_vendors"] and v["alliance_vendors"] <= 4 and pa.get("listed") and pa.get("lo", 0) > 1.5 * v["buy_price_per_unit"]:
                lim.append("few vendors and AH lo %d > 1.5x vendor %g" % (pa["lo"], v["buy_price_per_unit"]))
            if v["alliance_vendors"] and all(t in ("Agent of the Black Market", "Smokywood Pastures", "Exotic Reagent Merchant", "Traveling Salesman", "")
                                             for t in (v.get("alliance_vendor_titles") or [])):
                lim.append("only special vendors: " + titles)
            if lim:
                v["limited_suspect"] = "; ".join(lim)
        d["vendor"] = v
    if it:
        d["class"] = I(it["ClassID"]); d["subclass"] = I(it["SubclassID"])
    p = P.get(iid)
    if p:
        d["ah"] = {k: p[k] for k in ("lo", "hi", "listed", "sold24", "avg7", "auctions")}
    return d


def counts_forever(e):
    bp = float(e["EffectBasePointsF"] or 0)
    v = float(e["Variance"] or 0)
    if bp <= 0:
        return 1, 1
    lo = max(1, int(math.floor(bp * (1 - v / 2) + 0.5)))
    hi = max(1, int(math.floor(bp * (1 + v / 2) + 0.5)))
    return lo, hi


def counts_era(e):
    bp, ds = I(e["EffectBasePoints"]), I(e["EffectDieSides"])
    if ds >= 1:
        return max(1, bp + 1), max(1, bp + ds)
    return max(1, bp), max(1, bp)


def creates(db, sp, era=False):
    out = []
    for e in db.eff.get(sp, []):
        if e["Effect"] == "24" and I(e["EffectItemType"]) > 0:
            mn, mx = (counts_era(e) if era else counts_forever(e))
            out.append((I(e["EffectItemType"]), mn, mx))
    return out


# --- recipe item -> spell maps (ItemEffect TriggerType 6 = learn spell; or a spell with LEARN_SPELL effect 36)
def recipe_items(db):
    m = collections.defaultdict(list)
    for iid, it in db.item.items():
        if it["ClassID"] != "9":
            continue
        for s, t in db.item_spells.get(iid, []):
            m[s].append(iid)
            for e in db.eff.get(s, []):
                if e["Effect"] == "36" and I(e["EffectTriggerSpell"]) > 0:
                    m[I(e["EffectTriggerSpell"])].append(iid)
    return m


F_RECIPE = recipe_items(F)
# spells that teach another spell via LEARN_SPELL (36), not counting recipe items (quest rewards, trainer helpers)
F_TEACH = collections.defaultdict(list)
for sp, effs in F.eff.items():
    for e in effs:
        if e["Effect"] == "36" and I(e["EffectTriggerSpell"]) > 0:
            F_TEACH[I(e["EffectTriggerSpell"])].append(sp)
SLS = collections.defaultdict(list)
for r in lib.tab("SpellLearnSpell") or []:
    SLS[I(r["LearnSpellID"])].append(I(r["SpellID"]))

# --- Classic Era index
E_SLA = {}
for r in E.sla:
    if I(r["SkillLine"]) in PROF:
        E_SLA.setdefault(I(r["Spell"]), r)
E_ALL_SLA = {I(r["Spell"]) for r in E.sla}


def tools(sp):
    out = {"tool_categories": [], "tool_items": [], "focus": None}
    t = F.totems.get(sp)
    if t:
        for k in ("RequiredTotemCategoryID_0", "RequiredTotemCategoryID_1"):
            c = I(t.get(k))
            if c:
                out["tool_categories"].append({"id": c, "name": F.totemcat.get(c)})
        for k in ("Totem_0", "Totem_1"):
            c = I(t.get(k))
            if c:
                out["tool_items"].append({"id": c, "name": F.iname(c)})
    c = F.castreq.get(sp)
    if c and I(c.get("RequiresSpellFocus")):
        fid = I(c["RequiresSpellFocus"])
        out["focus"] = {"id": fid, "name": F.focus.get(fid)}
    cd = CD.get(sp)
    if cd and max(cd) > 0:
        out["cooldown_hours"] = round(max(cd) / 3600000.0, 1)
        cat = CAT.get(sp)
        if cat:
            out["cooldown_category"] = {"id": cat, "name": CATNAME.get(cat)}
    return out


CD = {}
for r in lib.tab("SpellCooldowns") or []:
    if r.get("DifficultyID", "0") in ("0", ""):
        CD[I(r["SpellID"])] = (I(r["RecoveryTime"]), I(r["CategoryRecoveryTime"]))
ECD = {}
for r in lib.tab("SpellCooldowns", era=True) or []:
    if r.get("DifficultyID", "0") in ("0", ""):
        ECD[I(r["SpellID"])] = (I(r["RecoveryTime"]), I(r["CategoryRecoveryTime"]))
CAT = {}
for r in lib.tab("SpellCategories") or []:
    if r.get("DifficultyID", "0") in ("0", "") and I(r["Category"]):
        CAT[I(r["SpellID"])] = I(r["Category"])
CATNAME = {I(r["ID"]): r["Name_lang"] for r in lib.tab("SpellCategory") or []}


def origin(sp):
    if sp in E_ALL_SLA:
        return "classic" if sp < 100000 else "sod_in_era_client"
    return "forever_new"


recipes = []
seen = {}
for r in F.sla:
    sl = I(r["SkillLine"])
    if sl not in PROF:
        continue
    sp = I(r["Spell"])
    if sp in seen:
        seen[sp]["skill_lines"].append(sl)
        continue
    prods = creates(F, sp)
    if not prods:
        continue
    name = F.spellname.get(sp)
    excluded = None
    if not name:
        excluded = "no SpellName row in Forever (spell removed)"
    elif any(b in name for b in BAD_NAME) or name.lower().startswith("zz"):
        excluded = "test/placeholder name"
    low, high = I(r["TrivialSkillLineRankLow"]), I(r["TrivialSkillLineRankHigh"])
    acq = I(r["AcquireMethod"])
    rec = {
        "spell_id": sp, "name": name, "profession": PROF[sl], "skill_line": sl, "skill_lines": [sl],
        "skill": {"min_skill_line_rank": I(r["MinSkillLineRank"]), "yellow": low or None,
                  "green": (low + high) // 2 if high else None, "grey": high or None,
                  "num_skill_ups": I(r["NumSkillUps"])},
        "acquire_method": acq,
        "class_mask": I(r["ClassMask"]), "race_mask": I(r["RaceMasks_0"]),
        "supersedes_spell": I(r["SupercedesSpell"]) or None,
    }
    # how learned
    ritems = sorted(set(F_RECIPE.get(sp, [])))
    rlist = []
    for ri in ritems:
        b = item_basic(ri)
        s = F.sparse.get(ri)
        if s:
            b["required_skill"] = I(s["RequiredSkill"]); b["required_skill_rank"] = I(s["RequiredSkillRank"])
        q = QI.get(str(ri)) or {}
        b["questie"] = {"npc_drops": len(q.get("2") or []), "object_drops": len(q.get("3") or []),
                        "item_drops": len(q.get("4") or []), "quest_rewards": len(q.get("6") or []),
                        "vendors": len(q.get("14") or [])} if q else None
        rlist.append(b)
    teach = sorted(set(x for x in F_TEACH.get(sp, []) if x not in {s for ri in ritems for s, _ in F.item_spells.get(ri, [])}))
    learned = []
    if acq == 1:
        learned.append("starts_known")
    elif acq == 2:
        learned.append("auto_on_skill")
    elif acq == 3:
        learned.append("retired")
    if rlist:
        learned.append("recipe_item")
    if teach or SLS.get(sp):
        learned.append("taught_by_spell")
    if acq == 0 and not rlist:
        learned.append("trainer_inferred")
    rec["learned_by"] = learned
    rec["recipe_items"] = rlist
    if teach or SLS.get(sp):
        rec["taught_by_spells"] = [{"id": x, "name": F.spellname.get(x)} for x in sorted(set(teach + SLS.get(sp, [])))]
    req = [x.get("required_skill_rank") for x in rlist if x.get("required_skill_rank")]
    rec["skill"]["required_skill"] = min(req) if req else (rec["skill"]["min_skill_line_rank"] if rec["skill"]["min_skill_line_rank"] > 1 else None)
    rec["skill"]["required_skill_source"] = ("recipe item ItemSparse.RequiredSkillRank" if req else
                                             ("SkillLineAbility.MinSkillLineRank" if rec["skill"]["min_skill_line_rank"] > 1 else
                                              "unknown (trainer data is server-side; MinSkillLineRank = 1)"))
    # reagents
    rg = []
    for iid, c in F.reag.get(sp, []):
        b = item_basic(iid)
        b["count"] = c
        rg.append(b)
    rec["reagents"] = rg
    # product(s)
    pid, mn, mx = prods[0]
    pb = item_basic(pid)
    pb.update(count_min=mn, count_max=mx)
    rec["product"] = pb
    if len(prods) > 1:
        rec["extra_products"] = [dict(item_basic(a), count_min=b, count_max=c) for a, b, c in prods[1:]]
    rec.update(tools(sp))
    rec["origin"] = origin(sp)
    # Classic Era comparison
    er = E_SLA.get(sp)
    if er:
        diffs = {}
        ep = creates(E, sp, era=True)
        if ep and (ep[0][0] != pid):
            diffs["product"] = {"era": ep[0][0], "forever": pid}
        if ep and ep[0][0] == pid and (ep[0][1], ep[0][2]) != (mn, mx):
            diffs["count"] = {"era": [ep[0][1], ep[0][2]], "forever": [mn, mx]}
        erg = sorted(E.reag.get(sp, []))
        frg = sorted(F.reag.get(sp, []))
        if erg != frg:
            diffs["reagents"] = {"era": erg, "forever": frg}
        el, eh = I(er["TrivialSkillLineRankLow"]), I(er["TrivialSkillLineRankHigh"])
        if (el, eh) != (low, high):
            diffs["trivial_ranks"] = {"era": [el, eh], "forever": [low, high]}
        if I(er["AcquireMethod"]) != acq:
            diffs["acquire_method"] = {"era": I(er["AcquireMethod"]), "forever": acq}
        ecd = round(max(ECD.get(sp, (0, 0))) / 3600000.0, 1)
        fcd = round(max(CD.get(sp, (0, 0))) / 3600000.0, 1)
        if ecd != fcd and max(ecd, fcd) >= 1:
            diffs["cooldown_hours"] = {"era": ecd, "forever": fcd}
        efoc = I((E.castreq.get(sp) or {}).get("RequiresSpellFocus"))
        ffoc = I((F.castreq.get(sp) or {}).get("RequiresSpellFocus"))
        if efoc != ffoc:
            diffs["focus"] = {"era": E.focus.get(efoc) if efoc else None, "forever": F.focus.get(ffoc) if ffoc else None}
        et = E.totems.get(sp) or {}
        ft = F.totems.get(sp) or {}
        def toolnames(t, db):
            n = [F.totemcat.get(I(t.get(k)), str(t.get(k))) for k in ("RequiredTotemCategoryID_0", "RequiredTotemCategoryID_1") if I(t.get(k))]
            n += [(db.iname(I(t.get(k))) or F.iname(I(t.get(k))) or str(t.get(k))) for k in ("Totem_0", "Totem_1") if I(t.get(k))]
            return sorted(set(x for x in n))
        etc, ftc = toolnames(et, E), toolnames(ft, F)
        if etc != ftc:
            diffs["tools"] = {"era": etc, "forever": ftc}
        if I(er["SkillLine"]) != sl:
            diffs["skill_line"] = {"era": I(er["SkillLine"]), "forever": sl}
        if diffs:
            rec["era_diff"] = diffs
    # availability verdict
    prod_ok = pb["in_forever_itemsparse"]
    evidence = []
    if pid in P:
        evidence.append("product seen on AH (7d)")
    if any(x["id"] in P for x in rlist):
        evidence.append("recipe item seen on AH (7d)")
    if str(pid) in QI:
        evidence.append("product in Questie Forever DB")
    if any((x.get("questie") or {}) and any((x["questie"] or {}).values()) for x in rlist):
        evidence.append("recipe item has Questie Forever source")
    rec["evidence"] = evidence
    if excluded:
        avail = "excluded"
    elif acq == 3:
        avail = "retired"
    elif not prod_ok:
        avail = "product_missing_in_forever"
    elif rec["origin"] in ("classic", "forever_new"):
        avail = "yes"
    else:
        avail = "likely" if ("product seen on AH (7d)" in evidence or "recipe item seen on AH (7d)" in evidence) else "doubtful"
    rec["available"] = avail
    if excluded:
        rec["excluded_reason"] = excluded
    rec["ah_tradeable"] = bool(avail in ("yes", "likely") and pb.get("bonding") not in ("BoP", "quest"))
    seen[sp] = rec
    recipes.append(rec)

# --- removed in Forever: classic recipe spells (in Era SLA, CREATE_ITEM) not usable in Forever
F_SLA_SPELLS = {I(r["Spell"]) for r in F.sla if I(r["SkillLine"]) in PROF}
removed = []
for sp, er in sorted(E_SLA.items()):
    if sp >= 100000 or not creates(E, sp, era=True):
        continue
    rec = seen.get(sp)
    why = None
    if sp not in F_SLA_SPELLS:
        why = "no Forever SkillLineAbility row"
    elif rec is None:
        why = "Forever row has no CREATE_ITEM effect" if F.spellname.get(sp) else "no Forever SpellName/SpellEffect"
    elif rec["available"] not in ("yes", "likely"):
        why = rec["available"] + (" (" + rec.get("excluded_reason", "") + ")" if rec.get("excluded_reason") else "")
    if why:
        ep = creates(E, sp, era=True)[0]
        removed.append({"spell_id": sp, "era_name": E.spellname.get(sp), "profession": PROF[I(er["SkillLine"])],
                        "era_product": ep[0], "product_name": (E.sparse.get(ep[0]) or {}).get("Display_lang"),
                        "why": why})

meta = {
    "built": "2026-11-19", "client": "WoW Forever 1.60.1.70959 (wago.tools wowf CSV exports)",
    "classic_era_compare": "wago.tools Classic Era 1.15.x CSV exports (SkillLineAbility, SpellEffect, SpellReagents, SpellName from scratchpad/wf12/web/wago_era; ItemSparse, Item from wf15/web/era)",
    "prices": {"file": lib.DATA_LUA, **PH,
               "row_fields": "name|lo|hi|listed|sold24|avg7|vendorSell|H|D|auctions (copper; lo = cheapest buyout per unit now, hi = market value, listed = units listed now, sold24 = est. units sold in 24h over salesHours measured hours, avg7 = mean daily market of the last 7 days)"},
    "fields": {
        "spell_id / skill_line / acquire_method / class_mask / race_mask / supersedes_spell": "SkillLineAbility.Spell / .SkillLine / .AcquireMethod / .ClassMask / .RaceMasks_0 / .SupercedesSpell (rows with SkillLine in 186,164,202,165,197,171,185,129,333)",
        "name": "SpellName.Name_lang",
        "skill.min_skill_line_rank": "SkillLineAbility.MinSkillLineRank (1 on ~99% of trade recipes in both clients; the real learn level for trainer recipes is server-side trainer data)",
        "skill.yellow / skill.grey": "SkillLineAbility.TrivialSkillLineRankLow / TrivialSkillLineRankHigh",
        "skill.green": "floor((TrivialSkillLineRankLow + TrivialSkillLineRankHigh) / 2), the server's skill-up formula",
        "skill.required_skill": "recipe item ItemSparse.RequiredSkillRank when a recipe item teaches it, else MinSkillLineRank if > 1, else null",
        "learned_by": "starts_known = AcquireMethod 1; auto_on_skill = 2; retired = 3 (in Forever only the moved Alchemy healing potions, Purification/Poison Cleansing and non-recipe summons carry it); recipe_item = an Item with ClassID 9 whose ItemEffect (via ItemXItemEffect) has TriggerType 6 for this spell, or a spell with SpellEffect.Effect 36 LEARN_SPELL -> this spell; taught_by_spell = some other spell's Effect 36 or SpellLearnSpell row teaches it; trainer_inferred = AcquireMethod 0 and no recipe item (trainer lists are not in client tables; could also be a quest reward)",
        "recipe_items[].required_skill_rank / bonding / quality / vendor_sell": "ItemSparse.RequiredSkillRank / Bonding / OverallQualityID / SellPrice",
        "recipe_items[].questie": "QuestieDB_Forever item fields 2 npcDrops, 3 objectDrops, 4 itemDrops, 6 questRewards, 14 vendors (counts)",
        "reagents[].id / count": "SpellReagents.Reagent_0..7 / ReagentCount_0..7",
        "product.id / count_min / count_max": "SpellEffect rows with Effect 24 (CREATE_ITEM), DifficultyID 0: EffectItemType; count = EffectBasePointsF (0 means 1) with spread +-Variance/2 relative (Forever DB2 has no DieSides; checked against Era BasePoints+DieSides: Coarse Dynamite 2 +-1 = 1-3, Large Copper Bomb 3 +-0.667 = 2-4, Heavy Dynamite 3 +-1.333 = 1-5)",
        "bonding / quality / stack": "ItemSparse.Bonding (0 none, 1 BoP, 2 BoE, 3 BoU, 4 quest) / OverallQualityID / Stackable",
        "vendor": "QuestieDB_Forever item field 14 (vendor NPC ids) joined to Questie NPC field 13 friendlyToFaction and field 7 spawns; alliance_vendors = friendly to A and has spawns (0 when the only ones are Darkmoon Faire vendors, which come and go and sell limited stock: darkmoon_faire_only); buy_price_per_unit = ItemSparse.BuyPrice / VendorStackCount (no reputation discount); limited_suspect = <= 4 Alliance vendors while AH lo > 1.5x the vendor price, or only Black Market / seasonal / exotic sellers (then the profit tables price it at AH instead)",
        "ah": "GoblinLedger Data.lua items row (see prices)",
        "tool_categories": "SpellTotems.RequiredTotemCategoryID_0/1 -> TotemCategory.Name_lang (e.g. 162 Blacksmith Hammer, 14 Arclight Spanner, 169 Flint and Tinder)",
        "tool_items": "SpellTotems.Totem_0/1 (item ids)",
        "cooldown_hours / cooldown_category": "SpellCooldowns max(RecoveryTime, CategoryRecoveryTime) ms / 3.6e6 (DifficultyID 0); SpellCategories.Category -> SpellCategory.Name_lang (shared cooldowns such as transmutes)",
        "focus": "SpellCastingRequirements.RequiresSpellFocus -> SpellFocusObject.Name_lang (Anvil, Forge, Black Forge, Cooking Fire, Alchemy Lab, Moonwell...)",
        "origin": "classic = spell id < 100000 and in the Era SkillLineAbility table; sod_in_era_client = in the Era table with id >= 100000 (Season of Discovery data shipped in the Era client); forever_new = not in the Era table",
        "era_diff": "field-by-field compare with the Era tables (product, count from Era EffectBasePoints+EffectDieSides, reagents, trivial ranks, acquire method, skill line, focus (Era SpellCastingRequirements), tools (Era SpellTotems; category and tool-item names compared), cooldown from Era SpellCooldowns 1.15.9.70576)",
        "available": "yes = classic or forever_new with the product present in Forever ItemSparse; likely = SoD-era recipe whose product or recipe was seen on this AH; doubtful = SoD-era recipe, no evidence; product_missing_in_forever = no ItemSparse row for the product; retired = AcquireMethod 3; excluded = no name or test name",
        "ah_tradeable": "available yes/likely and product not BoP/quest-bound",
    },
}
out = {"meta": meta, "recipes": recipes, "removed_vs_era": removed}
with open(os.environ.get("GL_RECIPES_OUT") or os.path.join(lib.WF20, "recipes.json"), "w", encoding="ascii") as f:
    json.dump(out, f, indent=1, ensure_ascii=True)
c = collections.Counter((r["profession"], r["available"], r["ah_tradeable"]) for r in recipes)
for k in sorted(c):
    print(k, c[k])
print("removed vs era:", len(removed))

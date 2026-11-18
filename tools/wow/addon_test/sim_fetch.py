"""Runs tools/wow/fetch_ah.py against SIMULATED auctions to test the pipeline
and the page locally. Output goes to wow/_localdata (gitignored). Not real data.

What it simulates, for 240 hourly snapshots:
  - stackable goods on the commodities list (unit_price), and green gear on
    the auctions list (buyout, quantity 1) with random suffixes in
    "bonus_lists" plus "modifiers" type 28 and "context", the way the real API
    sends them. Each suffix has its own price, so the versions differ.
  - one gear version whose bonus id has no known suffix name (it is named
    "<item> (version N)"),
  - sellers who now and then dump a few listings far under the market (gap
    flips), a stack listed under the vendor price (vendor flips), a 1c troll
    listing and a silly high listing, a deep cheap wall, and buyers who take
    the cheapest units every hour.
"""
import email.utils
import math
import os
import random
import shutil
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
import fetch_ah as f  # noqa: E402

OUT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "wow", "_localdata"))
shutil.rmtree(OUT, ignore_errors=True)
I = "https://render.worldofwarcraft.com/us/icons/56/"
TG = ("Trade Goods", 7)
ITEMS = {  # id: (name, icon, base copper, units listed, sells/hour, quality, vendor, (subclass, id))
 2589: ("Linen Cloth", "inv_fabric_linen_01", 45, 900, 60, "COMMON", 13, ("Cloth", 5)),
 2592: ("Wool Cloth", "inv_fabric_wool_01", 120, 700, 40, "COMMON", 33, ("Cloth", 5)),
 4306: ("Silk Cloth", "inv_fabric_silk_01", 310, 800, 55, "COMMON", 150, ("Cloth", 5)),
 4338: ("Mageweave Cloth", "inv_fabric_mageweave_01", 520, 600, 45, "COMMON", 250, ("Cloth", 5)),
 14047: ("Runecloth", "inv_fabric_purple_01", 1650, 1200, 90, "COMMON", 400, ("Cloth", 5)),
 2770: ("Copper Ore", "inv_ore_copper_01", 180, 400, 30, "COMMON", 5, ("Metal & Stone", 7)),
 2771: ("Tin Ore", "inv_ore_tin_01", 260, 300, 20, "COMMON", 25, ("Metal & Stone", 7)),
 2772: ("Iron Ore", "inv_ore_iron_01", 1100, 250, 18, "COMMON", 150, ("Metal & Stone", 7)),
 3858: ("Mithril Ore", "inv_ore_mithril_02", 2400, 300, 25, "COMMON", 250, ("Metal & Stone", 7)),
 10620: ("Thorium Ore", "inv_ore_thorium_02", 4100, 350, 30, "COMMON", 500, ("Metal & Stone", 7)),
 2447: ("Peacebloom", "inv_misc_flower_02", 80, 500, 35, "COMMON", 4, ("Herb", 9)),
 765: ("Silverleaf", "inv_misc_herb_10", 95, 450, 30, "COMMON", 4, ("Herb", 9)),
 2449: ("Earthroot", "inv_misc_herb_07", 140, 300, 22, "COMMON", 6, ("Herb", 9)),
 785: ("Mageroyal", "inv_jewelry_talisman_03", 380, 200, 14, "COMMON", 13, ("Herb", 9)),
 4304: ("Thick Leather", "inv_misc_leatherscrap_08", 900, 400, 26, "COMMON", 150, ("Leather", 6)),
 8170: ("Rugged Leather", "inv_misc_leatherscrap_02", 1800, 500, 40, "COMMON", 250, ("Leather", 6)),
 10940: ("Strange Dust", "inv_enchant_duststrange", 650, 350, 28, "COMMON", 0, ("Enchanting", 12)),
 10938: ("Lesser Magic Essence", "inv_enchant_essencemagicsmall", 1200, 90, 8, "UNCOMMON", 0, ("Enchanting", 12)),
 12363: ("Arcane Crystal", "inv_misc_gem_crystal_01", 1850000, 12, 1, "UNCOMMON", 0, ("Metal & Stone", 7)),
 13468: ("Black Lotus", "inv_misc_herb_blacklotus", 3500000, 6, 0.4, "UNCOMMON", 0, ("Herb", 9)),
 12359: ("Thorium Bar", "inv_ingot_07", 5200, 200, 12, "COMMON", 600, ("Metal & Stone", 7)),
 3575: ("Iron Bar", "inv_ingot_iron", 1300, 150, 9, "COMMON", 200, ("Metal & Stone", 7)),
}
# green gear with random suffixes: id: (name, icon, vendor, class, subclass, slot, ilvl, reqlvl,
#   {bonus id: (price, listed, sells per hour)})
# 99001 is a bonus id the suffix table does not know: that version is "Spiked Club (version 4)"
GEAR = {
 9860: ("Archer's Cloak", "inv_misc_cape_03", 2382, ("Armor", 4), ("Cloth", 1), ("BACK", "Back"), 34, 29,
        {12718: (14000, 6, 0.25), 12719: (9000, 5, 0.15), 12722: (22000, 7, 0.3), 12726: (11000, 4, 0.1)}),
 8214: ("Wild Leather Helmet", "inv_helmet_41", 6230, ("Armor", 4), ("Leather", 2), ("HEAD", "Head"), 45, 40,
        {12752: (24000, 5, 0.2), 12751: (38000, 6, 0.25), 12732: (20000, 3, 0.1), 12723: (30000, 4, 0.12)}),
 4564: ("Spiked Club", "inv_mace_14", 610, ("Weapon", 2), ("Mace", 4), ("WEAPON", "One-Hand"), 13, 8,
        {12919: (900, 8, 0.4), 12918: (850, 6, 0.3), 12728: (1900, 4, 0.15), 99001: (1200, 3, 0.1)}),
}
T = [int(time.time()) - 240 * 3600]
f.time.time = lambda: T[0]
f.time.sleep = lambda s: None
f.get_token = lambda: "sim"
rng = random.Random(7)
book = {}   # auction id -> {"id", "iid", "bonus", "qty", "unit", "tl", "com"}
nid = [1]


def add(iid, qty, unit, bonus=None, tl=None):
    book[nid[0]] = {"id": nid[0], "iid": iid, "bonus": bonus, "qty": qty, "unit": max(1, int(unit)),
                    "tl": tl or rng.choice(["VERY_LONG", "VERY_LONG", "LONG", "MEDIUM"]), "com": iid in ITEMS}
    nid[0] += 1


def units_of(iid, bonus=None):
    return sum(a["qty"] for a in book.values() if a["iid"] == iid and a["bonus"] == bonus)


def drift(iid, t):
    return 1 + 0.12 * math.sin(t / 86400 * 2 * math.pi / 3 + iid) + 0.05 * math.sin(t / 3600 / 5 + iid)


def relist(t):
    for iid, (name, icon, base, listed, rate, q, v, _) in ITEMS.items():
        have = units_of(iid)
        while have < listed:
            qty = rng.choice([1, 5, 10, 20, 20]) if base < 100000 else 1
            add(iid, qty, base * drift(iid, t) * (1 + abs(rng.gauss(0, 0.12))))
            have += qty
        # now and then somebody dumps a few stacks far under the market: a gap flip
        if rng.random() < 0.06:
            for _ in range(rng.randint(1, 3)):
                add(iid, rng.choice([1, 5, 10]) if base < 100000 else 1, base * drift(iid, t) * rng.uniform(0.35, 0.6))
    for iid, g in GEAR.items():
        for bonus, (price, listed, rate) in g[8].items():
            have = units_of(iid, bonus)
            while have < listed:
                add(iid, 1, price * drift(iid + bonus, t) * (1 + abs(rng.gauss(0, 0.15))), bonus)
                have += 1
            if rng.random() < 0.05:   # one version listed far under its own price
                add(iid, 1, price * rng.uniform(0.25, 0.5), bonus)


FIXED = [   # situations that are put back whenever they sold or expired, so the flip lists have them
    (2589, 20, 8, None),            # Linen Cloth at 8c each, a vendor pays 13c: vendor flip
    (2589, 20, 9, None),
    (13468, 1, 1, None),            # a 1c troll Black Lotus in front of the real ones
    (12363, 1, 99999999, None),     # a silly 9,999g Arcane Crystal far above the rest
    (4306, 1000, 160, None),        # a deep cheap wall of Silk Cloth (more than a day of sales)
    (4306, 1000, 160, None),
    (8214, 1, 6000, 12751),         # Wild Leather Helmet of the Bandit at 60s, the version sells for ~3.8g
    (9860, 1, 7000, 12722),         # Archer's Cloak of the Bear at 70s (cheap for the Bear, normal for others)
    (3575, 5, 500, None),           # Iron Bar: 3 stacks of 5 at 5s, then the normal 13s market
    (3575, 5, 510, None),
    (3575, 5, 520, None),
]


def special(t):
    have = {a.get("fixed") for a in book.values()}
    for n, (iid, qty, unit, bonus) in enumerate(FIXED, 1):
        if n not in have:
            add(iid, qty, unit, bonus, tl="VERY_LONG")
            book[nid[0] - 1]["fixed"] = n


def api_auction(a, t):
    item = {"id": a["iid"]}
    out = {"id": a["id"], "item": item, "quantity": a["qty"], "time_left": a["tl"]}
    if a["com"]:
        out["unit_price"] = a["unit"]
    else:
        out["buyout"] = a["unit"] * a["qty"]
        item["context"] = 1
        item["modifiers"] = [{"type": 28, "value": rng.choice([5240, 5247, 6874])}]   # ignored by the collector
        if a["bonus"]:
            item["bonus_lists"] = [a["bonus"]]
    return out


def fake_api(path, ns, tries=4):
    if path.endswith("/auctions/"):
        return {"auctions": [{"id": 110, "name": "Alliance Auction House"}, {"id": 113, "name": "Horde Auction House"},
                             {"id": 116, "name": "Blackwater Auction House"}]}, {}
    stamp = {"Last-Modified": email.utils.formatdate(T[0], usegmt=True)}
    if path.endswith("/commodities"):
        return {"auctions": [api_auction(a, T[0]) for a in book.values() if a["com"]]}, {}
    if "/auctions/" in path:
        return {"auctions": [api_auction(a, T[0]) for a in book.values() if not a["com"]]}, stamp
    iid = int(path.rsplit("/", 1)[1])
    if path.startswith("/data/wow/media/item/"):
        icon = ITEMS[iid][1] if iid in ITEMS else GEAR[iid][1]
        return {"assets": [{"key": "icon", "value": I + icon + ".jpg"}]}, {}
    if path.startswith("/data/wow/item/"):
        if iid in ITEMS:
            n = ITEMS[iid]
            return {"name": n[0], "quality": {"type": n[5]}, "sell_price": n[6], "level": 0, "required_level": 0,
                    "item_class": {"name": TG[0], "id": TG[1]}, "item_subclass": {"name": n[7][0], "id": n[7][1]},
                    "inventory_type": {"type": "NON_EQUIP", "name": "Non-equippable"}, "is_stackable": True}, {}
        g = GEAR[iid]
        return {"name": g[0], "quality": {"type": "UNCOMMON"}, "sell_price": g[2], "level": g[6],
                "required_level": g[7], "item_class": {"name": g[3][0], "id": g[3][1]},
                "item_subclass": {"name": g[4][0], "id": g[4][1]},
                "inventory_type": {"type": g[5][0], "name": g[5][1]}, "is_stackable": False}, {}
    return None, {}


def buy(iid, n, bonus=None):
    """buyers take the cheapest n units"""
    for _ in range(max(0, int(n))):
        cands = sorted((a for a in book.values() if a["iid"] == iid and a["bonus"] == bonus), key=lambda a: a["unit"])
        if not cands:
            break
        a = cands[0]
        a["qty"] -= 1
        if a["qty"] <= 0:
            del book[a["id"]]


def sells(rate):
    return (1 if rng.random() < rate else 0) if rate < 1 else int(rng.gauss(rate, rate * 0.3))


f.api = fake_api
sys.argv = ["x", OUT]
for h in range(240):
    for iid, it in ITEMS.items():
        buy(iid, sells(it[4]))
    for iid, g in GEAR.items():
        for bonus, (price, listed, rate) in g[8].items():
            buy(iid, sells(rate), bonus)
    for a in list(book.values()):
        a["tl"] = {"VERY_LONG": "LONG", "LONG": "MEDIUM", "MEDIUM": "SHORT"}.get(a["tl"], "GONE")
        if a["tl"] == "GONE":
            del book[a["id"]]
    relist(T[0])
    special(T[0])
    f.main()
    T[0] += 3600

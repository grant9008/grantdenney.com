"""Runs tools/wow/fetch_ah.py against SIMULATED auctions to test the pipeline
and the page locally. Output goes to wow/_localdata (gitignored). Not real data."""
import sys, random, math, time, os, shutil
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
import fetch_ah as f
OUT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "wow", "_localdata"))
shutil.rmtree(OUT, ignore_errors=True)
I = "https://render.worldofwarcraft.com/us/icons/56/"
ITEMS = {  # id: (name, icon, base copper, units listed, sells/hour, quality, vendor)
 2589: ("Linen Cloth","inv_fabric_linen_01",45,900,60,"COMMON",13),
 2592: ("Wool Cloth","inv_fabric_wool_01",120,700,40,"COMMON",33),
 4306: ("Silk Cloth","inv_fabric_silk_01",310,800,55,"COMMON",150),
 4338: ("Mageweave Cloth","inv_fabric_mageweave_01",520,600,45,"COMMON",250),
 14047: ("Runecloth","inv_fabric_purple_01",1650,1200,90,"COMMON",400),
 2770: ("Copper Ore","inv_ore_copper_01",180,400,30,"COMMON",5),
 2771: ("Tin Ore","inv_ore_tin_01",260,300,20,"COMMON",25),
 2772: ("Iron Ore","inv_ore_iron_01",1100,250,18,"COMMON",150),
 3858: ("Mithril Ore","inv_ore_mithril_02",2400,300,25,"COMMON",250),
 10620: ("Thorium Ore","inv_ore_thorium_02",4100,350,30,"COMMON",500),
 2447: ("Peacebloom","inv_misc_flower_02",80,500,35,"COMMON",4),
 765: ("Silverleaf","inv_misc_herb_10",95,450,30,"COMMON",4),
 2449: ("Earthroot","inv_misc_herb_07",140,300,22,"COMMON",6),
 785: ("Mageroyal","inv_jewelry_talisman_03",380,200,14,"COMMON",13),
 4304: ("Thick Leather","inv_misc_leatherscrap_08",900,400,26,"COMMON",150),
 8170: ("Rugged Leather","inv_misc_leatherscrap_02",1800,500,40,"COMMON",250),
 10940: ("Strange Dust","inv_enchant_duststrange",650,350,28,"COMMON",0),
 10938: ("Lesser Magic Essence","inv_enchant_essencemagicsmall",1200,90,8,"UNCOMMON",0),
 12363: ("Arcane Crystal","inv_misc_gem_crystal_01",1850000,12,1,"UNCOMMON",0),
 13468: ("Black Lotus","inv_misc_herb_blacklotus",3500000,6,0.4,"UNCOMMON",0),
 12359: ("Thorium Bar","inv_ingot_07",5200,200,12,"COMMON",600),
 3575: ("Iron Bar","inv_ingot_iron",1300,150,9,"COMMON",200),
}
T = [int(time.time()) - 240*3600]
f.time.time = lambda: T[0]
f.time.sleep = lambda s: None
f.get_token = lambda: "sim"
rng = random.Random(7)
book = {}   # auction id -> dict
nid = [1]
def relist(iid, t):
    name, icon, base, listed, rate, q, v = ITEMS[iid]
    drift = 1 + 0.12*math.sin(t/86400*2*math.pi/3 + iid) + 0.05*math.sin(t/3600/5 + iid)
    while sum(a["quantity"] for a in book.values() if a["item"]["id"]==iid) < listed:
        qty = rng.choice([1,5,10,20,20]) if base < 100000 else 1
        unit = int(base*drift*(1+abs(rng.gauss(0,0.12))))
        book[nid[0]] = {"id": nid[0], "item": {"id": iid}, "quantity": qty, "buyout": unit*qty,
                        "time_left": rng.choice(["VERY_LONG","VERY_LONG","LONG","MEDIUM"])}
        nid[0] += 1
def fake_api(path, ns, tries=4):
    if path.endswith("/auctions/"):
        return {"auctions": [{"id": 110, "name": "Alliance Auction House"}, {"id": 113, "name": "Horde Auction House"}, {"id": 116, "name": "Blackwater Auction House"}]}, {}
    if path.endswith("/commodities"):
        return {"auctions": []}, {}
    if "/auctions/" in path:
        return {"auctions": list(book.values())}, {"Last-Modified": str(T[0])}
    if path.startswith("/data/wow/media/item/"):
        iid = int(path.rsplit("/",1)[1]); return {"assets": [{"key":"icon","value": I+ITEMS[iid][1]+".jpg"}]}, {}
    if path.startswith("/data/wow/item/"):
        iid = int(path.rsplit("/",1)[1]); n = ITEMS[iid]
        return {"name": n[0], "quality": {"type": n[5]}, "sell_price": n[6], "item_class": {"name": "Trade Goods"}, "item_subclass": {"name": "Trade Goods"}}, {}
    return None, {}
f.api = fake_api
sys.argv = ["x", OUT]
for h in range(240):
    # buyers take the cheapest units, some listings expire, sellers relist
    for iid, it in ITEMS.items():
        n = rng.random() < (it[4] % 1) if it[4] < 1 else int(rng.gauss(it[4], it[4]*0.3))
        for _ in range(max(0, int(n))):
            cands = sorted((a for a in book.values() if a["item"]["id"]==iid), key=lambda a: a["buyout"]/a["quantity"])
            if not cands: break
            a = cands[0]; a["quantity"] -= 1; a["buyout"] -= a["buyout"]//(a["quantity"]+1)
            if a["quantity"] <= 0: del book[a["id"]]
    for a in list(book.values()):
        a["time_left"] = {"VERY_LONG":"LONG","LONG":"MEDIUM","MEDIUM":"SHORT"}.get(a["time_left"], "GONE")
        if a["time_left"] == "GONE": del book[a["id"]]
    for iid in ITEMS: relist(iid, T[0])
    f.main()
    T[0] += 3600

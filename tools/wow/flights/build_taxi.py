"""Build taxi.json, the input of gen_flights_lua.py, from WoW Forever's own client tables.

    python -I tools/wow/flights/build_taxi.py <csv dir> <taxi.json>

<csv dir> holds four wago.tools exports for Forever's build 1.60.1.70959, each saved as <Table>.csv:
    https://wago.tools/db2/TaxiNodes/csv?build=1.60.1.70959        (node id, name, world x/y/z, map, faction flags)
    https://wago.tools/db2/TaxiPath/csv?build=1.60.1.70959         (path id, from node, to node, cost)
    https://wago.tools/db2/TaxiPathNode/csv?build=1.60.1.70959     (the points each path flies through)
    https://wago.tools/db2/UiMapAssignment/csv?build=1.60.1.70959  (where each map sits on the taxi maps)
Then: python -I tools/wow/flights/gen_flights_lua.py <taxi.json> writes Flights.lua's data block.

The time model (no other addon's flight times are used):
  * One flight (one path): 0.56 s + L / 30.32 yd/s + the path's stop delays, L the length of a uniform
    Catmull-Rom spline through the path's points (split at teleport points). 30.32 yd/s is the taxi speed
    (434% of run speed). Against 257 pre-Cataclysm warcraft.wiki.gg flight times (timed by players):
    median error 1.2 s, mean 3.3 s, 71% within 3 s, 86% within 6 s, 93% within 10 s (Alliance's 121:
    median 1.1 s, mean 2.2 s, 91% within 6 s). The worst Alliance ones: Booty Bay to Stormwind (the wiki
    26.5 s longer) and Auberdine to Theramore (14.7 s).
  * Flying on past a place (multi-hop): the server joins the two paths and cuts the corner at the place
    it flies past, as TrinityCore's FlightPathMovementGenerator::LoadPath does (worked out from Blizzard's
    servers): points within 40 yd of that place are left out and the start of the next path unwinds the
    end of the last one while they overlap. junctions["a>b>c"] = the seconds that saves at b, against
    flying a>b and b>c one after the other. Not seen in Forever yet: the addon checks it against your own
    multi-hop flights (Flights.lua, joinModel) and falls back to 0.56 s a place when they say otherwise.
  * Flights whose path changes maps (Alterac Valley's) get special: no time is worked out for them.
"""
import csv
import json
import math
import os
import statistics
import sys

SPEED = 30.32       # yards a second on a taxi (normal speed)
OVERHEAD = 0.56     # seconds a flight takes on top of its path (take-off and landing)
FF = 1.2            # Frequent Flier (spell 1225490): taxi speed +20%
SKIP2 = 40.0 * 40.0  # TrinityCore SKIP_SPLINE_POINT_DISTANCE_SQ
F_TELEPORT, F_STOP = 1, 2
TAXIMAP = {0: 1463, 1: 1464, 2991: 2665}   # continent -> the flight master's taxi map (UiMap)


def rd(d, t):
    with open(os.path.join(d, t + ".csv"), encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def xyz(r):
    return (float(r["Loc_0"]), float(r["Loc_1"]), float(r["Loc_2"]))


def d3(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def poly(ps):
    return sum(d3(ps[i], ps[i + 1]) for i in range(len(ps) - 1))


def catmull(ps, steps=16):
    """Length of a uniform Catmull-Rom spline through ps (the end points doubled)."""
    if len(ps) < 3:
        return poly(ps)
    q = [ps[0]] + ps + [ps[-1]]
    L = 0.0
    for i in range(1, len(q) - 2):
        p0, p1, p2, p3 = q[i - 1], q[i], q[i + 1], q[i + 2]
        prev = p1
        for s in range(1, steps + 1):
            t = s / steps
            t2 = t * t
            t3 = t2 * t
            c = tuple(0.5 * ((2 * p1[k]) + (-p0[k] + p2[k]) * t + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
                             + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3) for k in range(3))
            L += d3(prev, c)
            prev = c
    return L


def path_len(rows):
    """Spline length of a path's points, split where it teleports."""
    segs, cur = [], []
    for r in rows:
        cur.append(xyz(r))
        if int(r["Flags"]) & F_TELEPORT:
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    return sum(catmull(s) for s in segs)


def seconds_of(rows):
    return OVERHEAD + path_len(rows) / SPEED + sum(int(r["Delay"]) for r in rows)


def faction(fl):
    a, h = fl & 1, fl & 2
    return "both" if a and h else ("A" if a else ("H" if h else None))


def norm(asg, x, y):
    """World x, y -> 0..1 on a taxi map (from its top left, as TaxiNodePosition gives it)."""
    R0, R1, R3, R4 = (float(asg["Region_%d" % i]) for i in (0, 1, 3, 4))
    u0, u1, v0, v1 = float(asg["UiMin_0"]), float(asg["UiMin_1"]), float(asg["UiMax_0"]), float(asg["UiMax_1"])
    mx = u0 + (v0 - u0) * (R4 - y) / (R4 - R1)
    my = u1 + (v1 - u1) * (R3 - x) / (R3 - R0)
    return round(mx, 4), round(my, 4)


def included(p1, p2):
    """TrinityCore IsNodeIncludedInShortenedPath: p2 is far enough from p1 (or a teleport or a stop) to keep."""
    if int(p1["ContinentID"]) != int(p2["ContinentID"]):
        return True
    dx, dy = float(p1["Loc_0"]) - float(p2["Loc_0"]), float(p1["Loc_1"]) - float(p2["Loc_1"])
    fl = int(p2["Flags"])
    return dx * dx + dy * dy > SKIP2 or bool(fl & F_TELEPORT) or bool(fl & F_STOP and int(p2["Delay"]))


def joined(paths):
    """The points the server flies for paths flown one after the other without landing
    (TrinityCore FlightPathMovementGenerator::LoadPath)."""
    out = []
    last = len(paths) - 1
    for src, nodes in enumerate(paths):
        start, end = nodes[0], nodes[-1]
        passed = False
        for i, nd in enumerate(nodes):
            if passed or src == 0 or not out or included(out[-1], nd):
                if ((src == 0 or (included(start, nd) and i >= 2))
                        and (src == last or (included(end, nd) and (i < len(nodes) - 1 or not out)))
                        and (not int(nd["Flags"]) & F_TELEPORT or not out or not int(out[-1]["Flags"]) & F_TELEPORT)):
                    passed = True
                    out.append(nd)
            else:
                out.pop()
    return out


def build(d):
    N = {int(n["ID"]): n for n in rd(d, "TaxiNodes")}
    P = {int(p["ID"]): p for p in rd(d, "TaxiPath")}
    pts = {}
    for r in rd(d, "TaxiPathNode"):
        pts.setdefault(int(r["PathID"]), []).append(r)
    for k in pts:
        pts[k].sort(key=lambda r: int(r["NodeIndex"]))
    ua = rd(d, "UiMapAssignment")

    nodes = {}
    for i, n in sorted(N.items()):
        fl, cont = int(n["Flags"]), int(n["ContinentID"])
        x, y, z = float(n["Pos_0"]), float(n["Pos_1"]), float(n["Pos_2"])
        rec = dict(id=i, name=n["Name_lang"], continent=cont, world=[round(x, 1), round(y, 1), round(z, 1)],
                   faction=faction(fl), flags=fl, kind="flight" if fl & 3 else "other")
        if cont in TAXIMAP:
            for a in ua:
                if a["UiMapID"] == str(TAXIMAP[cont]):
                    rec["taxiMap"] = TAXIMAP[cont]
                    rec["taxiXY"] = list(norm(a, x, y))
                    break
        nodes[i] = rec

    def flight_node(i):
        return i in N and (int(N[i]["Flags"]) & 3) != 0

    edges, rows_of, secs_of = [], {}, {}
    for pid, p in sorted(P.items()):
        a, b = int(p["FromTaxiNode"]), int(p["ToTaxiNode"])
        if a == b or not (flight_node(a) and flight_node(b)) or pid not in pts:
            continue
        rows = pts[pid]
        ps = [xyz(r) for r in rows]
        conts = sorted(set(int(r["ContinentID"]) for r in rows))
        tele = [k for k, r in enumerate(rows) if int(r["Flags"]) & F_TELEPORT]
        L = path_len(rows)
        secs = seconds_of(rows)
        cum = [0.0]
        for k in range(1, len(ps)):
            jump = int(rows[k - 1]["Flags"]) & F_TELEPORT
            cum.append(cum[-1] + (0 if jump else math.dist(ps[k - 1], ps[k]) / SPEED))
        scale = (L / SPEED) / cum[-1] if cum[-1] else 1
        e = {"path": pid, "from": a, "to": b, "cost": int(p["Cost"]), "yards": round(L, 1), "seconds": round(secs, 1),
             "secondsFF": round(OVERHEAD + L / (SPEED * FF) + sum(int(r["Delay"]) for r in rows), 1),
             "continents": conts,
             "points": [[round(q[0], 1), round(q[1], 1), round(q[2], 1), round(c * scale + OVERHEAD / 2, 1)]
                        for q, c in zip(ps, cum)]}
        na, nb = N[a], N[b]
        d0 = math.dist(ps[0][:2], (float(na["Pos_0"]), float(na["Pos_1"])))
        d1 = math.dist(ps[-1][:2], (float(nb["Pos_0"]), float(nb["Pos_1"])))
        if (d0 > 100 or d1 > 100 or tele or len(conts) > 1
                or int(na["ContinentID"]) not in (0, 1) or int(nb["ContinentID"]) not in (0, 1)):
            e["special"] = "changes maps or does not start and end at its places (Alterac Valley): no time"
        edges.append(e)
        key = (a, b)
        if key not in rows_of and "special" not in e:   # the same flight twice: the first path, as gen_flights_lua.py
            rows_of[key], secs_of[key] = rows, secs

    # Junctions: every a>b>c one faction can fly (all three places on its maps), a != c.
    out_of = {}
    for (a, b) in rows_of:
        out_of.setdefault(a, []).append(b)

    def same_side(*ids):
        fs = [nodes[i]["faction"] for i in ids]
        return any(all(f in (s, "both") for f in fs) for s in ("A", "H"))

    junctions = {}
    for (a, b) in sorted(rows_of):
        for c in sorted(out_of.get(b, [])):
            if c == a or not same_side(a, b, c):
                continue
            t = seconds_of(joined([rows_of[(a, b)], rows_of[(b, c)]]))
            junctions["%d>%d>%d" % (a, b, c)] = round(secs_of[(a, b)] + secs_of[(b, c)] - t, 2)
    sv = sorted(junctions.values())
    # Three paths: is the time of the whole the legs less each junction's saving?
    err = []
    for k, s1 in junctions.items():
        a, b, c = (int(x) for x in k.split(">"))
        for dd in out_of.get(c, []):
            k2 = "%d>%d>%d" % (b, c, dd)
            if dd in (a, b) or k2 not in junctions:
                continue
            whole = seconds_of(joined([rows_of[(a, b)], rows_of[(b, c)], rows_of[(c, dd)]]))
            err.append(secs_of[(a, b)] + secs_of[(b, c)] + secs_of[(c, dd)] - s1 - junctions[k2] - whole)
    stats = dict(junctions=len(sv), median=round(statistics.median(sv), 2), mean=round(statistics.mean(sv), 2),
                 p10=round(sv[len(sv) // 10], 2), p90=round(sv[len(sv) * 9 // 10], 2), min=sv[0], max=sv[-1],
                 three_paths=len(err), three_paths_err_max=round(max(abs(x) for x in err), 2) if err else 0,
                 three_paths_err_p99=round(sorted(abs(x) for x in err)[int(len(err) * 0.99)], 2) if err else 0)
    model = dict(speed_yd_per_s=SPEED, overhead_s=OVERHEAD, frequentFlier=dict(spell=1225490, speed_mult=FF),
                 junctions="TrinityCore FlightPathMovementGenerator::LoadPath shortening (40 yd), seconds saved at b")
    src = dict(build="1.60.1.70959 (WoW Forever)", tables=["TaxiNodes", "TaxiPath", "TaxiPathNode", "UiMapAssignment"],
               url="https://wago.tools/db2/<Table>/csv?build=1.60.1.70959")
    return dict(source=src, model=model, junction_stats=stats, nodes={str(k): v for k, v in nodes.items()},
                edges=edges, junctions=junctions)


def main(argv):
    if len(argv) != 2 or argv[0].startswith("-"):
        print(__doc__)
        return 2
    taxi = build(argv[0])
    with open(argv[1], "w", encoding="utf-8") as f:
        json.dump(taxi, f, separators=(",", ":"))
    flights = sum(1 for n in taxi["nodes"].values() if n["kind"] == "flight")
    print("wrote %s: %d flight places, %d paths, %d junctions" % (argv[1], flights, len(taxi["edges"]), len(taxi["junctions"])))
    print("junctions:", taxi["junction_stats"])
    print("special:", [(e["from"], e["to"], e["path"]) for e in taxi["edges"] if e.get("special")])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

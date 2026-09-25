# -*- coding: utf-8 -*-
"""Napravlennyj poisk konnektorov.

Prezhnyaya versiya hodila po grafu BEZ ucheta odnostoronnosti, i eto dalo
asimmetriyu: Kale -> Dover 52 km est, Dover -> Kale net puti. Portovye dorogi
odnostoronnie - v Dovre oni tak i nazyvayutsya: North Return Road, South Exit
Road, North Exit Road. Vybrannaya cepochka okazyvalas proezzhaemoj tolko v
storonu pribytiya.

Poetomu ot konca paroma zapuskayutsya DVA poiska:
  VYEZD  - po razreshennomu napravleniyu, ot paroma k Lite;
  VYEZD obratno (VEZD) - protiv napravleniya, to est put ot Lite k paromu.
V nabor idet obedinenie oboih cepochek.
"""
import argparse, collections, math, urllib.parse

LITE = {"motorway", "trunk", "primary", "secondary", "tertiary",
        "motorway_link", "trunk_link", "primary_link", "secondary_link",
        "tertiary_link"}
NOT_DRIVABLE = {"footway", "path", "cycleway", "steps", "pedestrian", "bridleway",
                "corridor", "platform", "construction", "proposed", "raceway",
                "bus_guideway", "elevator", "via_ferrata"}
# Иерархия доступа OSM: motorcar уточняет motor_vehicle, тот — vehicle, тот —
# access. Побеждает САМЫЙ ЧАСТНЫЙ присутствующий тег.
#
# Прежняя версия смотрела access, motor_vehicle и motorcar и ПРОПУСКАЛА vehicle,
# который стоит НАД motor_vehicle. Из-за этого цепочка въезда в порт Дувра прошла
# по пяти путям с vehicle=no: наш поиск объявил Дувр соединённым в обе стороны,
# Valhalla эти рёбра отвергла, и все маршруты Британия <-> континент ушли через
# Нормандские острова. 24 коннектора из 5842 были выбраны так же.
ACCESS_KEYS = ("motorcar", "motor_vehicle", "vehicle", "access")
FORBIDDEN = ("no", "private")

ALLOW = ("yes", "designated", "destination", "permissive", "customers")
MAX_WAYS = 50000
MAX_CHAIN_M = 5000.0


def unesc(s):
    return urllib.parse.unquote(s.replace("%20%", " "))


def tagval(field, key):
    pref = key + "="
    for kv in field.split(","):
        if kv.startswith(pref):
            return unesc(kv[len(pref):])
    return ""


def access_ok(field):
    for k in ACCESS_KEYS:
        v = tagval(field, k)
        if v:
            return v not in FORBIDDEN
    return True


def oneway_of(field):
    """1 - tolko vpered, -1 - tolko nazad, 0 - v obe storony."""
    ow = tagval(field, "oneway")
    if ow in ("yes", "true", "1"):
        return 1
    if ow in ("-1", "reverse"):
        return -1
    if tagval(field, "junction") == "roundabout" and ow not in ("no", "false", "0"):
        return 1
    return 0


def read_ways(path, ferry=False):
    out = {}
    for line in open(path, encoding="utf-8", errors="replace"):
        if line[0] != "w":
            continue
        parts = line.rstrip("\n").split(" ")
        T = ""; refs = None
        for f in parts:
            if f[:1] == "T" and len(f) > 1: T = f[1:]
            elif f[:1] == "N" and len(f) > 1:
                refs = [int(r[1:]) for r in f[1:].split(",") if r[:1] == "n"]
        if not refs:
            continue
        if ferry:
            if tagval(T, "route") != "ferry":
                continue
            mv = tagval(T, "motor_vehicle") or tagval(T, "motorcar")
            out[int(parts[0][1:])] = (refs, mv != "no")
        else:
            hw = tagval(T, "highway")
            if not hw or hw in NOT_DRIVABLE:
                continue
            out[int(parts[0][1:])] = (refs, hw, access_ok(T), oneway_of(T))
    return out


def read_nodes(path, need):
    out = {}
    for line in open(path, encoding="utf-8", errors="replace"):
        if line[0] != "n":
            continue
        parts = line.split(" ", 1)
        nid = int(parts[0][1:])
        if nid not in need:
            continue
        x = y = None
        for f in parts[1].rstrip("\n").split(" "):
            if f[:1] == "x" and len(f) > 1: x = f[1:]
            elif f[:1] == "y" and len(f) > 1: y = f[1:]
        if x and y:
            try: out[nid] = (float(y), float(x))
            except ValueError: pass
    return out


def dist(a, b):
    la1, lo1 = map(math.radians, a); la2, lo2 = map(math.radians, b)
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 2*6371000*math.asin(min(1, math.sqrt(h)))


def main():
    global n2w, dr, nodes
    ap = argparse.ArgumentParser(description="Directed connector search: ferry ends -> first Lite road.")
    ap.add_argument("cand_opl", help="candidate small roads + Lite around the ferry ends (cand.opl)")
    ap.add_argument("ferries_opl", help="ferry ways of Europe Lite (ferries.opl)")
    ap.add_argument("out_ways", help="output: connector way ids, one wN per line")
    ap.add_argument("out_unreached", help="output: ferry ends that did not reach Lite")
    args = ap.parse_args()
    CAND, FERR, OUT, UNREACHED = args.cand_opl, args.ferries_opl, args.out_ways, args.out_unreached

    print("prohod 1: puti kandidatov")
    ways = read_ways(CAND)
    print("  proezzhih putej %d" % len(ways))
    print("prohod 1: paromy")
    fw = read_ways(FERR, ferry=True)
    car = [(w, r) for w, (r, c) in fw.items() if c]
    print("  paromov %d, avtomobilnyh %d" % (len(fw), len(car)))

    endpoints = []
    for w, refs in car:
        endpoints.append(refs[0]); endpoints.append(refs[-1])
    endpoints = list(dict.fromkeys(endpoints))
    print("  unikalnyh koncov %d" % len(endpoints))

    print("prohod 2: koordinaty")
    need = set()
    for v in ways.values():
        need.update(v[0])
    need.update(endpoints)
    nodes = read_nodes(CAND, need)
    nodes.update(read_nodes(FERR, set(endpoints)))
    print("  koordinat %d" % len(nodes))

    # node -> [(way, index)]
    n2w = collections.defaultdict(list)
    dr = {}
    for wid, (refs, hw, acc, ow) in ways.items():
        if not acc:
            continue
        dr[wid] = (refs, hw, ow)
        for i, n in enumerate(refs):
            n2w[n].append((wid, i))
    print("  proezzhih posle dostupa %d" % len(dr))


    def wlen(refs):
        s = 0.0
        for i in range(1, len(refs)):
            a, b = refs[i-1], refs[i]
            if a in nodes and b in nodes:
                s += dist(nodes[a], nodes[b])
        return s


    def search(ep, outward):
        """outward=True: ot paroma po razreshennomu napravleniyu (vyezd iz porta).
           outward=False: protiv napravleniya (put ot Lite k paromu)."""
        start = n2w.get(ep)
        if not start:
            return None, "net primykayushchej proezzhej dorogi"
        prev, seen = {}, set()
        q = collections.deque()
        for w, i in start:
            if (w, i) not in seen:
                seen.add((w, i)); prev[(w, i)] = None; q.append((w, i))
        hit = None
        while q and len(seen) < MAX_WAYS:
            w, i = q.popleft()
            refs, hw, ow = dr[w]
            if hw in LITE:
                hit = (w, i); break
            # kuda mozhno vyjti s etogo rebra, vojdya v nego v tochke i
            idxs = []
            fwd_ok = ow != -1 if outward else ow != 1
            bwd_ok = ow != 1 if outward else ow != -1
            if fwd_ok: idxs.extend(range(i + 1, len(refs)))
            if bwd_ok: idxs.extend(range(0, i))
            for j in idxs:
                for w2, i2 in n2w.get(refs[j], ()):
                    if (w2, i2) not in seen:
                        seen.add((w2, i2)); prev[(w2, i2)] = (w, i); q.append((w2, i2))
        if hit is None:
            return None, "Lite ne dostignuta"
        chain, cur = [], hit
        while cur is not None:
            chain.append(cur[0]); cur = prev[cur]
        add = [w for w in dict.fromkeys(chain) if dr[w][1] not in LITE]
        return add, None


    keep, stat, unreached = set(), collections.Counter(), []
    lens, longest = [], (0.0, None, None)
    both = 0
    for ep in endpoints:
        out_add, out_err = search(ep, True)
        in_add, in_err = search(ep, False)
        if out_add is None and in_add is None:
            stat[out_err] += 1
            if out_err == "Lite ne dostignuta":
                unreached.append(ep)
            continue
        add = []
        if out_add: add += out_add
        if in_add: add += in_add
        add = list(dict.fromkeys(add))
        if not add:
            stat["uzhe na Lite (0 putej)"] += 1
            continue
        L = sum(wlen(dr[w][0]) for w in add)
        if L > MAX_CHAIN_M * 2:
            stat["cepochka dlinnee predela"] += 1
            continue
        if out_add is not None and in_add is not None:
            both += 1
            stat["soedinen v OBE storony"] += 1
        else:
            stat["soedinen tolko v odnu storonu"] += 1
        lens.append(L)
        if L > longest[0]:
            longest = (L, ep, add)
        keep.update(add)

    print("")
    print("=== rezultat ===")
    for k, v in stat.most_common():
        print("   %-36s %d" % (k, v))

    nkeep, byclass, total = set(), collections.Counter(), 0.0
    for w in keep:
        refs, hw, ow = dr[w]
        nkeep.update(refs); byclass[hw] += 1; total += wlen(refs)
    print("")
    print("konnektorov: putej %d, uzlov %d, %.1f km" % (len(keep), len(nkeep), total/1000))
    if lens:
        lens.sort()
        p = lambda q: lens[min(len(lens)-1, int(len(lens)*q))]
        print("dlina: min %.0f m, median %.0f m, p95 %.0f m, max %.0f m"
              % (lens[0], p(0.5), p(0.95), lens[-1]))
    for k, v in byclass.most_common():
        print("   %-16s %d" % (k, v))

    with open(UNREACHED, "w") as f:
        for ep in unreached:
            la, lo = nodes.get(ep, (0.0, 0.0))
            f.write("%d %.7f %.7f" % (ep, la, lo) + chr(10))
    with open(OUT, "w") as f:
        for w in sorted(keep):
            f.write("w%d" % w + chr(10))
    print("zapisano %s" % OUT)


if __name__ == "__main__":
    main()

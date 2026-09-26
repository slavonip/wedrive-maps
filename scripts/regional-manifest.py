#!/usr/bin/env python3
"""Манифест региональных пакетов.

Схема продиктована измерением. Обновление Румынии 2026-09-01 -> 2026-09-18 сменило 636 тайлов
из 636 и 20 пар идентификаторов из 32. Значит таблица порталов принадлежит не границе, а
КОНКРЕТНОЙ ПАРЕ СБОРОК, и у каждой записи стоят обе версии.

Отсюда же правило загрузчика: обновление одной страны требует ВСЕХ её таблиц, а не только
границы с тем соседом, который тоже обновился. Идентификаторы пересобранного графа меняются
целиком и входят в каждую его таблицу.

СОВМЕСТИМОСТЬ С ПРИЛОЖЕНИЕМ (RegionalManifest.kt) — контракт, который здесь не меняется:
kind "regional"; engine — объект; у региона region_id, graph_version, package, url, bytes, sha256
(+ title, tiles, parts); у таблицы versions, file, url, bytes, sha256 (+ rows). Всё остальное —
происхождение, map, search, release — необязательные поля, которые приложение игнорирует.

ПЕРЕНЕСЁННАЯ СТРАНА (carried) не выкладывается заново: её url и parts остаются ссылками на
неизменяемый актив того релиза, в котором она опубликована. map и search переносятся из
предыдущего манифеста при любой пересборке графа — обновление графа их не теряет.

  usage:
    regional-manifest.py build <regional.json> <каталог> <url-базы> [--previous m.json] [--tag T]
                               [--engine-lock regional-engine.lock] [--pipeline-rev SHA]
                               [--run-id N] [--run-url URL]           > manifest.json
    regional-manifest.py check <manifest.json> <каталог> [--complete regional.json] [--provenance]
    regional-manifest.py assets <manifest.json>      файлы, которые выкладываются в релиз этого прогона
    regional-manifest.py urls <manifest.json>        все URL и размеры, на которые указывает манифест
    regional-manifest.py verify-assets <manifest.json> <каталог>   активы этого прогона против манифеста
    regional-manifest.py complete <manifest.json> <regional.json>  все страны и границы на месте
    regional-manifest.py plan  <manifest.json> <код-страны>
"""
import argparse
import datetime
import hashlib
import json
import os
import sys

OPTIONAL_CARRY = ("map", "search")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def keyvals(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        s = line.split("#", 1)[0].strip()
        if "=" in s:
            k, v = (x.strip() for x in s.split("=", 1))
            out[k] = v
    return out


def tag_of(url):
    return url.split("/download/", 1)[1].split("/", 1)[0] if "/download/" in url else ""


def build(cfg_path, work, base_url, previous=None, tag=None, lock=None, pipeline_rev="",
          run_id=None, run_url="", basemaps=None):
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    base_url = base_url.rstrip("/")
    tag = tag or tag_of(base_url + "/x")
    prev_regions = (previous or {}).get("regions", {})
    regions, portals = {}, {}

    for code, c in cfg["countries"].items():
        desc = os.path.join(work, "%s.package.json" % code.lower())
        if not os.path.isfile(desc):
            continue
        d = json.load(open(desc, encoding="utf-8"))
        carried = bool(d.get("carried"))
        url = d["url"] if carried else "%s/%s" % (base_url, d["package"])
        r = {
            "region_id": c["region_id"],
            "title": c["title"],
            "graph_version": d["graph_version"],
            "package": d["package"],
            "url": url,
            "bytes": d["bytes"],
            "sha256": d["sha256"],
            "tiles": d["tiles"],
            "engine": d.get("engine", "unknown"),
        }
        # Части — только когда они есть. У обычного пакета поля нет вовсе, и это не «ещё не
        # заполнено», а «одним файлом»: устройство различает эти случаи по наличию поля.
        if d.get("parts"):
            r["parts"] = d["parts"]
        # Происхождение (необязательные поля для приложения, обязательные для фабрики).
        r["release"] = tag_of(url)
        for k in ("engine_sha", "source", "timezones", "tar_bytes", "tar_sha256"):
            if d.get(k) not in (None, "", {}):
                r[k] = d[k]
        if carried:
            prev_digest = prev_regions.get(code, {}).get("engine_digest") or d.get("engine_digest")
            if prev_digest:
                r["engine_digest"] = prev_digest
        elif lock:
            r["engine_digest"] = lock["image"].split("@", 1)[1]
        # FRONTIER (schema 2): the country's own border nodes, joined with a neighbour's on the
        # device by rule D1. It names no neighbour and no neighbour's version — which is the whole
        # point: a country is updated alone. It has its own url because a carried country can get
        # its frontier in a later release than its package (migration backfill).
        fb = d.get("frontier")
        if fb:
            f = {k: fb[k] for k in ("format", "file", "bytes", "sha256", "entries", "osm_resolved") if k in fb}
            f["url"] = fb.get("url") or "%s/%s" % (base_url, fb["file"])
            r["frontier"] = f
        # NAVIGATION FEATURES: cameras, enforcement, level crossings, signals of THIS graph version,
        # travelling with the country exactly like the frontier (carried with it, own url).
        fe = d.get("features")
        if fe:
            f = {k: fe[k] for k in ("format", "file", "bytes", "sha256", "entries", "counts") if k in fe}
            f["url"] = fe.get("url") or "%s/%s" % (base_url, fe["file"])
            r["features"] = f
        # map/search: из описания переносимой страны или из предыдущего манифеста — никогда не
        # теряются из-за того, что граф пересобран.
        for k in OPTIONAL_CARRY:
            v = d.get(k) or prev_regions.get(code, {}).get(k)
            if v:
                r[k] = v
        # a map and search built in THIS run (the basemap job) replace the carried ones
        desc = os.path.join(basemaps, "%s.basemap-desc.json" % code.lower()) if basemaps else None
        if desc and os.path.isfile(desc):
            bd = json.load(open(desc, encoding="utf-8"))
            for k in OPTIONAL_CARRY:
                if bd.get(k):
                    v = dict(bd[k])
                    v["url"] = "%s/%s" % (base_url, v["file"])
                    r[k] = v
        regions[code] = r

    for border in cfg["borders"]:
        a, b = border["between"]
        if a not in regions or b not in regions:
            continue
        name = "%s-%s" % (a, b)
        f = os.path.join(work, "%s-%s.portals" % (a.lower(), b.lower()))
        if not os.path.isfile(f):
            continue
        rows = sum(1 for l in open(f, encoding="utf-8") if l.strip() and not l.startswith("#"))
        portals[name] = {
            # ОБЕ версии, а не одна: таблица годна ровно для этой пары сборок и ни для какой
            # другой. Устройство обязано сверять их перед установкой.
            "versions": ["%s-%s" % (a, regions[a]["graph_version"]),
                         "%s-%s" % (b, regions[b]["graph_version"])],
            "file": os.path.basename(f),
            "url": "%s/%s" % (base_url, os.path.basename(f)),
            "bytes": os.path.getsize(f),
            "sha256": sha256(f),
            "rows": rows,
        }
    for code, r in regions.items():
        r["portals"] = sorted(n for n in portals if code in n.split("-"))

    # schema 2 = frontiers. Pair tables stay, as the legacy path for apps that predate frontiers;
    # an app that reads frontiers never needs them and never lets their versions gate a set.
    m = {"schema": 2 if any(r.get("frontier") for r in regions.values()) else 1,
         "kind": "regional", "tag": tag,
         "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if lock:
        m["engine"] = {"version": lock["engine_ref"], "sha": lock["engine_sha"],
                       "digest": lock["image"].split("@", 1)[1], "image": lock["image"]}
        m["timezones"] = {"url": lock["timezones_url"], "sha256": lock["timezones_sha256"]}
    else:
        m["engine"] = {"version": next(iter(regions.values()))["engine"] if regions else "unknown"}
    m["pipeline"] = {"revision": pipeline_rev, "run_id": run_id, "run_url": run_url}
    m["regions"], m["portals"] = regions, portals
    return m


def here(m, r):
    return r.get("release", tag_of(r["url"])) == m.get("tag")


def frontier_here(m, r):
    f = r.get("frontier")
    return bool(f) and tag_of(f["url"]) == m.get("tag")


def features_here(m, r):
    f = r.get("features")
    return bool(f) and tag_of(f["url"]) == m.get("tag")


def side_here(m, r, key):
    f = r.get(key)
    return bool(f) and bool(f.get("url")) and "file" in f and tag_of(f["url"]) == m.get("tag")


def side_files(entry):
    """(name, bytes, sha256) of what is uploaded for a map/search entry: its parts, or the file."""
    if entry.get("parts"):
        return [(p["name"], p["bytes"], p["sha256"]) for p in entry["parts"]]
    return [(entry["file"], entry["bytes"], entry["sha256"])]


def assets(m):
    """Files uploaded into THIS run's release: packages (or their parts) built here, the frontiers
    made here (rebuilt or backfilled countries), all legacy tables."""
    out = []
    for r in m["regions"].values():
        if here(m, r):
            out += [p["name"] for p in r["parts"]] if r.get("parts") else [r["package"]]
        if frontier_here(m, r):
            out.append(r["frontier"]["file"])
        if features_here(m, r):
            out.append(r["features"]["file"])
        for k in OPTIONAL_CARRY:
            if side_here(m, r, k):
                out += [n for n, _, _ in side_files(r[k])]
    out += [t["file"] for t in m["portals"].values()]
    return out


def urls(m):
    """Every (url, bytes) the manifest points to, carried countries included."""
    out = []
    for r in m["regions"].values():
        base = r["url"].rsplit("/", 1)[0]
        out += [("%s/%s" % (base, p["name"]), p["bytes"]) for p in r["parts"]] if r.get("parts") \
            else [(r["url"], r["bytes"])]
        if r.get("frontier"):
            out.append((r["frontier"]["url"], r["frontier"]["bytes"]))
        if r.get("features"):
            out.append((r["features"]["url"], r["features"]["bytes"]))
        for k in OPTIONAL_CARRY:
            e = r.get(k)
            if e and e.get("url") and "bytes" in e and "file" in e:
                b = e["url"].rsplit("/", 1)[0]
                out += [("%s/%s" % (b, n), size) for n, size, _ in side_files(e)] if e.get("parts") else [(e["url"], e["bytes"])]
    out += [(t["url"], t["bytes"]) for t in m["portals"].values()]
    return out


def verify_assets(m, directory):
    """Every file of THIS run's release, in `directory`, against the manifest. -> problems."""
    want = {}
    for r in m["regions"].values():
        if here(m, r):
            if r.get("parts"):
                want.update({p["name"]: (p["bytes"], p["sha256"]) for p in r["parts"]})
            else:
                want[r["package"]] = (r["bytes"], r["sha256"])
        if frontier_here(m, r):
            want[r["frontier"]["file"]] = (r["frontier"]["bytes"], r["frontier"]["sha256"])
        if features_here(m, r):
            want[r["features"]["file"]] = (r["features"]["bytes"], r["features"]["sha256"])
        for k in OPTIONAL_CARRY:
            if side_here(m, r, k):
                want.update({n: (size, h) for n, size, h in side_files(r[k])})
    want.update({t["file"]: (t["bytes"], t["sha256"]) for t in m["portals"].values()})
    problems = []
    for name, (size, digest) in sorted(want.items()):
        p = os.path.join(directory, name)
        if not os.path.isfile(p):
            problems.append("missing %s" % name)
        elif os.path.getsize(p) != size or sha256(p) != digest:
            problems.append("%s does not match the manifest" % name)
        else:
            print("   ok %s" % name)
    return problems


def complete(m, cfg_path):
    """A production manifest describes EVERY country of regional.json and every border. -> problems."""
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    problems = []
    for code, c in cfg["countries"].items():
        r = m["regions"].get(code)
        if not r:
            problems.append("incomplete: no %s" % code)
        elif r["region_id"] != c["region_id"]:
            problems.append("%s: region_id %s, regional.json says %s" % (code, r["region_id"], c["region_id"]))
        elif not r.get("frontier"):
            # Without a frontier the country can only be joined by a pair table, i.e. only to the
            # exact neighbour versions that table was made for — the coupling schema 2 removes.
            problems.append("incomplete: %s has no frontier" % code)
    for border in cfg["borders"]:
        n = "%s-%s" % tuple(border["between"])
        if n not in m["portals"]:
            problems.append("incomplete: no table %s" % n)
    return problems


def plan(manifest_path, code):
    m = json.load(open(manifest_path, encoding="utf-8"))
    code = code.upper()
    if code not in m["regions"]:
        print("нет такой страны в манифесте: %s" % code)
        return 1
    r = m["regions"][code]
    print("обновилась %s (версия %s)" % (code, r["graph_version"]))
    print("\nСКАЧАТЬ:")
    print("   пакет    %-8s %s" % (code, r["package"]))
    if r.get("frontier"):
        print("   frontier %-8s %s" % (code, r["frontier"]["file"]))
    else:
        # schema 1 only: without a frontier the country joins its neighbours through pair tables,
        # each made for one exact pair of versions.
        for t in (n for n in m["portals"] if code in n.split("-")):
            print("   таблица  %-8s %s  (%s)" % (t, m["portals"][t]["file"], ", ".join(m["portals"][t]["versions"])))
    print("\nНЕ СКАЧИВАТЬ:")
    for c in m["regions"]:
        if c != code:
            print("   пакет    %-8s %s" % (c, m["regions"][c]["package"]))
    return 0


def check(m, work, complete_cfg=None, provenance=False):
    """Манифест обязан описывать то, что действительно лежит рядом, и с теми же суммами.

    Иначе публикуется описание одного набора вместе с файлами другого — и устройство узнает
    об этом, скачав полгигабайта. -> число расхождений.
    """
    bad = 0
    for code, r in m["regions"].items():
        for k in ("region_id", "graph_version", "package", "url", "bytes", "sha256"):
            if k not in r:
                print("   %-8s нет поля %s (контракт приложения)" % (code, k)); bad += 1
        if not (0 < r.get("region_id", 0) <= 255):
            print("   %-8s region_id %s вне 1..255" % (code, r.get("region_id"))); bad += 1
        if r.get("parts") and here(m, r):
            # РАЗРЕЗАННЫЙ ПАКЕТ ЭТОГО ПРОГОНА: целого файла рядом уже нет, его удалил резчик.
            # Сверяются части и сумма их размеров; сумму целого проверит устройство у склеенного.
            total = broken = 0
            for part in r["parts"]:
                p = os.path.join(work, part["name"])
                if not os.path.isfile(p):
                    print("   НЕТ ЧАСТИ %s (%s)" % (part["name"], code)); bad += 1; continue
                if sha256(p) != part["sha256"] or os.path.getsize(p) != part["bytes"]:
                    print("   %-8s %-28s ЧАСТЬ НЕ СОШЛАСЬ" % (code, part["name"])); bad += 1; broken += 1
                total += os.path.getsize(p)
            if total != r["bytes"]:
                print("   %-8s сумма размеров частей %d против %d в манифесте" % (code, total, r["bytes"])); bad += 1
            elif not broken:
                print("   %-8s %-28s ok  частей %d" % (code, r["package"], len(r["parts"])))
        else:
            # целый файл: собранный здесь или скачанный перенос (склеенный из частей)
            p = os.path.join(work, r["package"])
            if not os.path.isfile(p):
                print("   НЕТ ФАЙЛА %s (%s)" % (r["package"], code)); bad += 1
            else:
                ok = sha256(p) == r["sha256"] and os.path.getsize(p) == r["bytes"]
                print("   %-8s %-28s %s%s" % (code, r["package"], "ok" if ok else "СУММА НЕ СОШЛАСЬ",
                                             "" if here(m, r) else "  (перенос из %s)" % r.get("release")))
                bad += 0 if ok else 1
        f = r.get("frontier")
        if f:
            p = os.path.join(work, f["file"])
            if f.get("format") != "wedrive-frontier/1":
                print("   %-8s frontier format %s unknown" % (code, f.get("format"))); bad += 1
            elif not os.path.isfile(p):
                print("   НЕТ ФАЙЛА %s (frontier %s)" % (f["file"], code)); bad += 1
            elif sha256(p) != f["sha256"] or os.path.getsize(p) != f["bytes"]:
                print("   %-8s %-28s FRONTIER НЕ СОШЁЛСЯ" % (code, f["file"])); bad += 1
            else:
                print("   %-8s %-28s ok  frontier %d, OSM id %d%s" % (
                    code, f["file"], f.get("entries", 0), f.get("osm_resolved", 0),
                    "" if frontier_here(m, r) else "  (перенос)"))
        fe = r.get("features")
        if fe:
            p = os.path.join(work, fe["file"])
            if fe.get("format") != "wedrive-features/1":
                print("   %-8s features format %s unknown" % (code, fe.get("format"))); bad += 1
            elif not os.path.isfile(p):
                print("   НЕТ ФАЙЛА %s (features %s)" % (fe["file"], code)); bad += 1
            elif sha256(p) != fe["sha256"] or os.path.getsize(p) != fe["bytes"]:
                print("   %-8s %-28s FEATURES НЕ СОШЛИСЬ" % (code, fe["file"])); bad += 1
            else:
                print("   %-8s %-28s ok  features %d%s" % (code, fe["file"], fe.get("entries", 0),
                                                        "" if features_here(m, r) else "  (перенос)"))
        if provenance:
            src = r.get("source") or {}
            for k in ("md5", "sha256", "replication"):
                if not src.get(k):
                    print("   %-8s нет source.%s" % (code, k)); bad += 1
            for k in ("engine_sha", "engine_digest"):
                if not r.get(k):
                    print("   %-8s нет %s" % (code, k)); bad += 1
            if not (r.get("timezones") or {}).get("sha256"):
                print("   %-8s нет timezones.sha256" % code); bad += 1
    for name, t in m["portals"].items():
        p = os.path.join(work, t["file"])
        if not os.path.isfile(p):
            print("   НЕТ ФАЙЛА %s (%s)" % (t["file"], name)); bad += 1; continue
        ok = sha256(p) == t["sha256"] and os.path.getsize(p) == t["bytes"]
        print("   %-8s %-28s %s  строк %d" % (name, t["file"], "ok" if ok else "СУММА НЕ СОШЛАСЬ", t["rows"]))
        bad += 0 if ok else 1
        if m.get("tag") and not t["url"].endswith("/download/%s/%s" % (m["tag"], t["file"])):
            print("   %-8s таблица указывает не на релиз этого прогона" % name); bad += 1
        a, b = name.split("-")
        want = ["%s-%s" % (a, m["regions"][a]["graph_version"]),
                "%s-%s" % (b, m["regions"][b]["graph_version"])]
        if t["versions"] != want:
            print("   %-8s версии таблицы не совпадают с версиями пакетов: %s против %s" % (name, t["versions"], want))
            bad += 1
    names = [t["file"] for t in m["portals"].values()]
    if len(names) != len(set(names)):
        print("   имена таблиц повторяются: все ложатся в один каталог устройства"); bad += 1
    if complete_cfg:
        # ПОЛНОТА — условие публикации: production-манифест описывает КАЖДУЮ страну regional.json
        # и каждую их границу. Контрольный прогон на части набора не может стать указателем.
        for p in complete(m, complete_cfg):
            print("   " + p); bad += 1
    print("\nрасхождений: %d" % bad)
    return bad


def main():
    if len(sys.argv) < 3:
        print(__doc__); return 2
    cmd = sys.argv[1]
    if cmd == "build":
        ap = argparse.ArgumentParser()
        ap.add_argument("cfg"); ap.add_argument("work"); ap.add_argument("base")
        ap.add_argument("--previous"); ap.add_argument("--tag"); ap.add_argument("--engine-lock")
        ap.add_argument("--pipeline-rev", default=""); ap.add_argument("--run-id", type=int)
        ap.add_argument("--run-url", default="")
        ap.add_argument("--basemaps", help="directory of <code>.basemap-desc.json made in this run")
        a = ap.parse_args(sys.argv[2:])
        prev = json.load(open(a.previous, encoding="utf-8")) if a.previous and os.path.exists(a.previous) else None
        lock = keyvals(a.engine_lock) if a.engine_lock else None
        print(json.dumps(build(a.cfg, a.work, a.base, prev, a.tag, lock, a.pipeline_rev, a.run_id, a.run_url, a.basemaps),
                         ensure_ascii=False, indent=2))
        return 0
    if cmd == "check":
        ap = argparse.ArgumentParser()
        ap.add_argument("manifest"); ap.add_argument("work")
        ap.add_argument("--complete"); ap.add_argument("--provenance", action="store_true")
        a = ap.parse_args(sys.argv[2:])
        return 1 if check(json.load(open(a.manifest, encoding="utf-8")), a.work, a.complete, a.provenance) else 0
    if cmd == "assets":
        print("\n".join(assets(json.load(open(sys.argv[2], encoding="utf-8")))))
        return 0
    if cmd == "verify-assets":
        problems = verify_assets(json.load(open(sys.argv[2], encoding="utf-8")), sys.argv[3])
        for p in problems:
            print("   PROBLEM: " + p)
        return 1 if problems else 0
    if cmd == "complete":
        problems = complete(json.load(open(sys.argv[2], encoding="utf-8")), sys.argv[3])
        for p in problems:
            print("   PROBLEM: " + p)
        print("complete" if not problems else "INCOMPLETE")
        return 1 if problems else 0
    if cmd == "urls":
        for u, n in urls(json.load(open(sys.argv[2], encoding="utf-8"))):
            print("%s %d" % (u, n))
        return 0
    if cmd == "plan":
        return plan(sys.argv[2], sys.argv[3])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

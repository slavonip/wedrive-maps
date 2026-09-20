#!/usr/bin/env python3
"""Манифест региональных пакетов.

Схема продиктована измерением. Обновление Румынии 2026-09-01 -> 2026-09-18 сменило 636 тайлов
из 636 и 20 пар идентификаторов из 32. Значит таблица порталов принадлежит не границе, а
КОНКРЕТНОЙ ПАРЕ СБОРОК, и у каждой записи стоят обе версии.

Отсюда же правило загрузчика: обновление одной страны требует ВСЕХ её таблиц, а не только
границы с тем соседом, который тоже обновился. Идентификаторы пересобранного графа меняются
целиком и входят в каждую его таблицу.

  usage:
    regional-manifest.py build <regional.json> <каталог-сборки> <url-базы> [> manifest.json]
    regional-manifest.py plan  <manifest.json> <код-страны>
    regional-manifest.py check <manifest.json> <каталог-сборки>
"""
import hashlib
import json
import os
import sys


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(cfg_path, work, base_url):
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    regions, portals = {}, {}

    for code, c in cfg["countries"].items():
        desc = os.path.join(work, "%s.package.json" % code.lower())
        if not os.path.isfile(desc):
            continue
        d = json.load(open(desc, encoding="utf-8"))
        regions[code] = {
            "region_id": c["region_id"],
            "title": c["title"],
            "graph_version": d["graph_version"],
            "package": d["package"],
            "url": "%s/%s" % (base_url.rstrip("/"), d["package"]),
            "bytes": d["bytes"],
            "sha256": d["sha256"],
            "tiles": d["tiles"],
            "engine": d.get("engine", "unknown"),
        }
        # Части — только когда они есть. У обычного пакета поля нет вовсе, и это не «ещё не
        # заполнено», а «одним файлом»: устройство различает эти случаи по наличию поля.
        if d.get("parts"):
            regions[code]["parts"] = d["parts"]

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
            "url": "%s/%s" % (base_url.rstrip("/"), os.path.basename(f)),
            "bytes": os.path.getsize(f),
            "sha256": sha256(f),
            "rows": rows,
        }

    engine = next(iter(regions.values()))["engine"] if regions else "unknown"
    return {"schema": 1, "kind": "regional", "engine": {"version": engine},
            "regions": regions, "portals": portals}


def plan(manifest_path, code):
    m = json.load(open(manifest_path, encoding="utf-8"))
    code = code.upper()
    if code not in m["regions"]:
        print("нет такой страны в манифесте: %s" % code)
        return 1
    tables = [n for n in m["portals"] if code in n.split("-")]
    print("обновилась %s (версия %s)" % (code, m["regions"][code]["graph_version"]))
    print("\nСКАЧАТЬ:")
    print("   пакет   %-8s %s" % (code, m["regions"][code]["package"]))
    for t in tables:
        print("   таблица %-8s %s  (%s)" % (t, m["portals"][t]["file"],
                                            ", ".join(m["portals"][t]["versions"])))
    print("\nНЕ СКАЧИВАТЬ:")
    for c in m["regions"]:
        if c != code:
            print("   пакет   %-8s %s" % (c, m["regions"][c]["package"]))
    for t in m["portals"]:
        if t not in tables:
            print("   таблица %-8s %s" % (t, m["portals"][t]["file"]))
    return 0


def check(manifest_path, work):
    """Манифест обязан описывать то, что действительно лежит рядом, и с теми же суммами.

    Иначе публикуется описание одного набора вместе с файлами другого — и устройство узнает
    об этом, скачав полгигабайта.
    """
    m = json.load(open(manifest_path, encoding="utf-8"))
    bad = 0
    for code, r in m["regions"].items():
        # РАЗРЕЗАННЫЙ ПАКЕТ: целого файла рядом уже нет, его удалил резчик. Сверяются части и
        # сумма их размеров.
        #
        # Сумму ЦЕЛОГО здесь не пересчитать, не склеив обратно несколько гигабайт, — а считать её
        # заново незачем: она снята резчиком с того самого файла, который он только что разрезал.
        # Устройство всё равно проверит её у собранного файла, и вот там она и решает.
        if r.get("parts"):
            total = 0
            broken = 0
            for part in r["parts"]:
                p = os.path.join(work, part["name"])
                if not os.path.isfile(p):
                    print("   НЕТ ЧАСТИ %s (%s)" % (part["name"], code)); bad += 1; continue
                ok = sha256(p) == part["sha256"] and os.path.getsize(p) == part["bytes"]
                if not ok:
                    print("   %-8s %-28s ЧАСТЬ НЕ СОШЛАСЬ" % (code, part["name"]))
                    bad += 1; broken += 1
                total += os.path.getsize(p)
            if total != r["bytes"]:
                print("   %-8s сумма размеров частей %d против %d в манифесте"
                      % (code, total, r["bytes"])); bad += 1
            elif not broken:
                print("   %-8s %-28s ok  частей %d" % (code, r["package"], len(r["parts"])))
            continue
        p = os.path.join(work, r["package"])
        if not os.path.isfile(p):
            print("   НЕТ ФАЙЛА %s (%s)" % (r["package"], code)); bad += 1; continue
        got = sha256(p)
        ok = got == r["sha256"] and os.path.getsize(p) == r["bytes"]
        print("   %-8s %-28s %s" % (code, r["package"], "ok" if ok else "СУММА НЕ СОШЛАСЬ"))
        bad += 0 if ok else 1
    for name, t in m["portals"].items():
        p = os.path.join(work, t["file"])
        if not os.path.isfile(p):
            print("   НЕТ ФАЙЛА %s (%s)" % (t["file"], name)); bad += 1; continue
        ok = sha256(p) == t["sha256"]
        print("   %-8s %-28s %s  строк %d" % (name, t["file"],
                                              "ok" if ok else "СУММА НЕ СОШЛАСЬ", t["rows"]))
        bad += 0 if ok else 1

    # Каждая граница между УСТАНОВЛЕННЫМИ странами обязана иметь таблицу. Пакеты без неё
    # соберутся и опубликуются, а машина через границу не поедет.
    for name, t in m["portals"].items():
        a, b = name.split("-")
        want = ["%s-%s" % (a, m["regions"][a]["graph_version"]),
                "%s-%s" % (b, m["regions"][b]["graph_version"])]
        if t["versions"] != want:
            print("   %-8s версии таблицы не совпадают с версиями пакетов: %s против %s"
                  % (name, t["versions"], want))
            bad += 1
    print("\nрасхождений: %d" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "build":
        print(json.dumps(build(sys.argv[2], sys.argv[3], sys.argv[4]),
                         ensure_ascii=False, indent=2))
    elif cmd == "plan":
        sys.exit(plan(sys.argv[2], sys.argv[3]))
    elif cmd == "check":
        sys.exit(check(sys.argv[2], sys.argv[3]))
    else:
        print(__doc__); sys.exit(2)

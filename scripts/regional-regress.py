#!/usr/bin/env python3
"""Регрессия собранного набора: проверяется СВЯЗНОСТЬ, а не совпадение с прошлым разом.

Пакеты пересобираются из свежего OSM и обязаны меняться — требовать прежних километров значило
бы падать на каждом обновлении данных. Поэтому у каждого маршрута два условия:

    seams     сколько раз путь обязан сменить регион: ноль внутри страны, по одной на границу.
              ЭТО главное число. Маршрут нужной длины, прошедший в обход границы, отличается от
              правильного именно им.
    km_about  ожидаемый порядок с допуском — ловит не «стало на метр иначе», а «поехал совсем
              не туда» и «дорога исчезла».

Плюс безусловное: ни одной потери региона. Это не расхождение чисел, а признак того, что тайл
взят из чужого каталога.

  usage: regional-regress.py <regional.json> <valhalla-config> [коды стран, что установлены]
"""
import json
import os
import re
import subprocess
import sys


def route(config, a, b):
    req = json.dumps({"locations": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}],
                      "costing": "auto"})
    env = dict(os.environ, WEDRIVE_DEBUG_BIDIR="1")
    r = subprocess.run(["valhalla_service", config, "route", req],
                       capture_output=True, text=True, env=env)
    both = r.stdout + r.stderr
    lost = both.count("WEDRIVE REGION LOST")
    seams = len(re.findall(r"WEDRIVE SEAM", both))
    i = r.stdout.find('{"trip"')
    if i < 0:
        return None, seams, lost, both[-200:].replace("\n", " ")
    try:
        trip = json.JSONDecoder().raw_decode(r.stdout[i:])[0]["trip"]
        return trip["summary"]["length"], seams, lost, ""
    except Exception as e:
        return None, seams, lost, str(e)[:120]


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    config = sys.argv[2]
    have = {c.upper() for c in sys.argv[3:]} if len(sys.argv) > 3 else set(cfg["countries"])

    fail = 0
    ran = 0
    print("%-34s %10s %8s %6s" % ("маршрут", "км", "швов", ""))
    for p in cfg["probes"]["routes"]:
        needs = set(p["needs"])
        if not needs <= have:
            print("%-34s %10s   пропуск: нет %s" % (p["name"], "-", ",".join(sorted(needs - have))))
            continue
        ran += 1
        km, seams, lost, err = route(config, p["from"], p["to"])
        problems = []
        if km is None:
            problems.append("нет маршрута: " + err)
        else:
            if abs(km - p["km_about"]) > p["km_tol"]:
                problems.append("км вне допуска (ждали %s +-%s)" % (p["km_about"], p["km_tol"]))
            if seams != p["seams"]:
                problems.append("швов %d, ждали %d" % (seams, p["seams"]))
        if lost:
            problems.append("ПОТЕРЯ РЕГИОНА x%d" % lost)
        mark = "ok" if not problems else "ПРОВАЛ"
        print("%-34s %10s %8s %6s %s"
              % (p["name"], ("%.3f" % km) if km else "-", seams, mark,
                 "; ".join(problems)))
        if problems:
            fail += 1

    print()
    if ran == 0:
        # Ноль выполненных проверок — это не успех. Так выглядит набор, в котором нет ни одной
        # страны, и без этой ветки регрессия отрапортовала бы зелёным на пустом месте.
        print("НИ ОДНОЙ ПРОВЕРКИ НЕ ВЫПОЛНЕНО — проверять нечего, это отказ")
        return 1
    print("проверок %d, провалов %d" % (ran, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

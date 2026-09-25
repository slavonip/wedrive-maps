#!/usr/bin/env python3
"""Таблицы порталов для набора распакованных стран.

Таблица принадлежит не границе, а КОНКРЕТНОЙ ПАРЕ СБОРОК. Измерено на обновлении Румынии
2026-09-01 -> 2026-09-18: 636 тайлов из 636 различаются побайтово и 20 пар идентификаторов из
32 сменились. Поэтому у каждой таблицы в манифесте стоят обе версии, и поэтому обновление одной
страны требует перегенерации ВСЕХ её таблиц, а не только границы с тем соседом, который тоже
обновился: идентификаторы пересобранного графа меняются целиком.

Номера регионов берутся из regional.json и не выводятся из порядка: они записаны внутрь
идентификаторов, и сдвиг номера обесценил бы каждую таблицу, где страна участвует.

  usage: regional-portals.py <regional.json> <каталог-с-распакованными> <каталог-выхода> [коды...]
"""
import json
import os
import subprocess
import sys


def tiles_dir(root, code):
    return os.path.join(root, code.upper(), "tiles")


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    root, out = sys.argv[2], sys.argv[3]
    only = {c.upper() for c in sys.argv[4:]} if len(sys.argv) > 4 else None
    os.makedirs(out, exist_ok=True)

    made = []
    for border in cfg["borders"]:
        a, b = border["between"]
        if only is not None and not ({a, b} & only):
            continue
        da, db = tiles_dir(root, a), tiles_dir(root, b)
        if not os.path.isdir(da) or not os.path.isdir(db):
            print("   пропуск %s-%s: нет одной из стран" % (a, b))
            continue
        ra = cfg["countries"][a]["region_id"]
        rb = cfg["countries"][b]["region_id"]
        s, w, n, e = border["box"]
        path = os.path.join(out, "%s-%s.portals" % (a.lower(), b.lower()))
        rows = []
        for level in (0, 1, 2):
            r = subprocess.run(
                ["portal_border", da, db, str(s), str(w), str(n), str(e), str(level),
                 str(ra), str(rb)],
                capture_output=True, text=True)
            if r.returncode != 0:
                print("   ОШИБКА %s-%s уровень %d: %s" % (a, b, level, r.stderr.strip()[:200]))
                return 1
            rows.extend(l for l in r.stdout.splitlines() if l.strip())
            for line in r.stderr.splitlines():
                if line.startswith("#"):
                    print("   " + line)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + ("\n" if rows else ""))

        # Пустая таблица — это не «границы нет», а «границу не нашли», и молча публиковать её
        # нельзя: пакеты соберутся, а машина через границу не поедет.
        if not rows:
            print("   ПУСТАЯ таблица %s-%s — граница не найдена" % (a, b))
            return 1
        print("   %s-%s (регионы %d/%d): %d записей -> %s" % (a, b, ra, rb, len(rows), path))
        made.append(path)

    eligible = [b for b in cfg["borders"]
                if os.path.isdir(tiles_dir(root, b["between"][0])) and os.path.isdir(tiles_dir(root, b["between"][1]))]
    if not made:
        if not eligible:
            # Набор без единой пары соседей (контрольный прогон одной страны): строить нечего,
            # и это не отказ. Отказ — когда пара есть, а таблицы нет; это ловит страж ниже.
            print("   в наборе нет ни одной границы с обеими странами — таблиц не нужно")
            return 0
        print("   ни одной таблицы не построено")
        return 1

    # СТРАЖ ПОЛНОТЫ. Граница, у которой обе страны стоят в наборе, обязана иметь таблицу — иначе
    # набор молча теряет переход.
    #
    # Это не теоретическая осторожность. Скрипт зовут со списком пересобранных стран, и раньше он
    # честно делал таблицы только для их границ; остальные таблицы взять было неоткуда, потому что
    # в рабочем каталоге лежат только свежесделанные. Пересборка одной Австрии оставила бы набор
    # без MD-RO и RO-HU — то есть без того самого свойства, ради которого схема существует.
    # Заметить это можно было только по регрессии, через час после начала сборки.
    #
    # Проверяется РЕЗУЛЬТАТ на диске, а не намерение вызывающего: страж, доверяющий аргументам,
    # повторяет ошибку того, кто их передал.
    missing = []
    for border in cfg["borders"]:
        a, b = border["between"]
        if not (os.path.isdir(tiles_dir(root, a)) and os.path.isdir(tiles_dir(root, b))):
            continue
        path = os.path.join(out, "%s-%s.portals" % (a.lower(), b.lower()))
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            missing.append("%s-%s" % (a, b))
    if missing:
        print("\nОТКАЗ: обе страны в наборе, а таблицы нет: %s" % ", ".join(missing),
              file=sys.stderr)
        print("   Набор с такой дырой соберётся и опубликуется, а машина через эту границу\n"
              "   не поедет. Зовите скрипт без фильтра стран, чтобы он сделал все таблицы.",
              file=sys.stderr)
        return 1

    print("   таблиц в наборе: %d, границ с обеими странами: %d"
          % (len(made), len(made) + len(missing)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

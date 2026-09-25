# -*- coding: utf-8 -*-
"""Поднять класс путей-коннекторов, чтобы они попали на уровень иерархии 1.

ДОКАЗАННАЯ ПРИЧИНА. Паромные рёбра Valhalla кладёт на уровень 0 (classification
primary), а подъезд к терминалу остаётся `service_other` на уровне 2. Собственный
ReclassifyFerryConnections движка это и должен был исправить — и в Дувре прямо
сообщил, что не смог: шесть предупреждений «Reclassification fails inbound to
ferry» на координатах узла посадки. Из 1342 концов 386 связей провалились в обе
стороны.

Здесь коннекторам присваивается `highway=tertiary` — класс, который Valhalla
кладёт на уровень 1. Односторонность, доступ и имена остаются как в OSM, потому
что они и решают законность проезда.

ВТОРАЯ ДОКАЗАННАЯ ПРИЧИНА (2026-09-23): подъёма `highway` мало. Valhalla выводит
`use` из тега `service` НЕЗАВИСИМО от `highway` (graph.lua: `use = use[kv["service"]]`),
и для трёх значений парсер сам ставит destination_only (pbfgraphparser.cc, case
kParkingAisle / kDriveway / kDriveThru). Первый проход двунаправленного A* такие
рёбра не штрафует, а ИСКЛЮЧАЕТ (AutoCost::Allowed, `!allow_destination_only_ &&
!pred.destonly() && edge->destonly()`), а второй проход, который их разрешает,
запускается только если первый не нашёл ничего. В Дувре единственный въезд к
причалу парома на Кале — w167792665 `service=parking_aisle`: путь до парома
существовал в графе, но первый проход его не видел и уезжал через Дюнкерк.

Поэтому у коннектора — и только у коннектора — ключ `service` с этими тремя
значениями переименовывается в `wedrive:service` (исходное значение сохраняется,
Valhalla этот ключ не читает). Явный `access=*` НЕ трогается: `access=customers`,
`permit`, `delivery`, `private` по-прежнему дают destination_only через таблицу
`private` в graph.lua — это юридическое ограничение из OSM, а не вывод парсера.

Запуск: python3 promote.py [SRC.opl DST.opl]
"""
import sys, urllib.parse

SRC = sys.argv[1] if len(sys.argv) > 2 else "/w/connectors.opl"
DST = sys.argv[2] if len(sys.argv) > 2 else "/w/connectors_promoted.opl"
# tertiary попадает на уровень 1; motorway/trunk/primary/secondary/tertiary —
# это уровни 0 и 1, остальное уходит на 2.
TARGET = "tertiary"
# service, из которых Valhalla САМА выводит destination_only (use 6, 4, 8)
AUTO_DESTONLY_SERVICE = {"parking_aisle", "driveway", "drive-through"}
RENAMED_KEY = "wedrive:service"


def promote_tags(kvs):
    """kvs — список 'k=v' в OPL-кодировке. Возвращает (новый список, переименован ли service)."""
    new = []
    have_highway = False
    renamed = False
    for kv in kvs:
        if kv.startswith("highway="):
            new.append("highway=" + TARGET); have_highway = True
        elif kv.startswith("service=") and urllib.parse.unquote(kv[8:]) in AUTO_DESTONLY_SERVICE:
            new.append(RENAMED_KEY + "=" + kv[8:]); renamed = True
        else:
            new.append(kv)
    if not have_highway:
        new.append("highway=" + TARGET)
    return new, renamed


def promote_line(line):
    """Одна строка OPL. Возвращает (строка, это путь?, переименован ли service)."""
    if line[0] != "w":
        return line, False, False
    parts = line.rstrip("\n").split(" ")
    renamed = False
    for i, f in enumerate(parts):
        if f[:1] == "T" and len(f) > 1:
            new, renamed = promote_tags(f[1:].split(","))
            parts[i] = "T" + ",".join(new)
            break
    else:
        # у пути вовсе не было тегов — добавляем поле T перед N
        for i, f in enumerate(parts):
            if f[:1] == "N":
                parts.insert(i, "Thighway=" + TARGET)
                break
    return " ".join(parts) + "\n", True, renamed


if __name__ == "__main__":
    ways = renamed = 0
    out = open(DST, "w", encoding="utf-8")
    for line in open(SRC, encoding="utf-8", errors="replace"):
        l, is_way, r = promote_line(line)
        ways += is_way; renamed += r
        out.write(l)
    out.close()
    print("путей переписано: %d; service с авто-destonly переименован: %d" % (ways, renamed))

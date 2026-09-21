"""Everything a driver can search for, out of the basemap: places, streets and POIs.

    usage: build-index.py <out.json> --places <archive> --detail <archive>
                          --bbox W,S,E,N [--pmtiles path]
                          [--places-zoom 10] [--detail-zoom 15]

THREE KINDS OF ANSWER, one file. Searching only settlements is a third of what a driver needs —
the owner's words, and they are right: "а улицы? а рестораны, магазины, заправки?"

**WHY TWO ARCHIVES.** Measured 2026-09-17 over greater Chișinău, and the difference is not a
matter of degree:

    kind              z14    z15
    charging_station    0      34     <- §8b's headline category exists ONLY at 15
    fuel               10     192
    pharmacy            2     465
    parking            14     792
    atm                 0     249

z14 carries a SAMPLE of POIs, not the set. But cutting the shipped basemap at 15 doubles it —
664 MB to ~1.3 GB for md-ro — and the car pays that in traffic every month.

So the detail archive is extracted at 15 on the RUNNER, read once, and thrown away. Only the
index travels: a few megabytes against a gigabyte, and the map the car draws is unchanged.

Places come from a low zoom because a settlement appears there and scanning 16x fewer tiles for
them costs nothing in completeness (verified: villages of 2000 people are present at z10).
"""
import argparse
import gzip
import json
import math
import os
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pmtiles_archive

# ── what a driver actually asks for ─────────────────────────────────────────────────────────
#
# Protomaps has hundreds of `kind` values and a driver has about ten questions. Charging leads
# because the car is electric and §8b makes it first-class; the rest are the categories Waze
# offers, which are the categories people press.
#
# A kind not listed here is still INDEXED BY NAME — "Linella" finds the supermarket — it simply
# has no category button. Dropping it would make the text search worse to tidy a row of icons.
CATEGORIES = {
    "charging": ["charging_station"],
    "fuel": ["fuel"],
    "parking": ["parking", "parking_space", "parking_entrance"],
    "food": ["restaurant", "fast_food", "cafe", "bar", "pub", "ice_cream", "food_court"],
    "shop": ["supermarket", "convenience", "greengrocer", "bakery", "department_store",
             "marketplace", "mall"],
    "pharmacy": ["pharmacy", "chemist"],
    "money": ["atm", "bank", "bureau_de_change"],
    "hotel": ["hotel", "motel", "guest_house", "hostel"],
    "hospital": ["hospital", "clinic", "doctors"],
    "toilets": ["toilets"],
}
KIND_TO_CATEGORY = {kind: name for name, kinds in CATEGORIES.items() for kind in kinds}

# Settlements worth typing at. `country` and `region` stay because "Romania" is a reasonable
# thing to type on the way there.
PLACE_KINDS = {"country", "region", "locality", "borough", "neighbourhood", "macrohood"}

# Roads worth naming. `path` and `rail` are excluded: nobody drives to a footpath by name, and a
# railway named "Chișinău–Ungheni" in a destination list is noise.
ROAD_KINDS = {"major_road", "medium_road", "minor_road", "highway"}


# ── MVT, parsed by hand: no library that has to be reachable at 02:17 ───────────────────────

def varint(buf, i):
    value = shift = 0
    while True:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7


def fields(buf):
    i = 0
    while i < len(buf):
        key, i = varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = varint(buf, i)
            yield number, value
        elif wire == 2:
            length, i = varint(buf, i)
            yield number, buf[i:i + length]
            i += length
        elif wire == 5:
            yield number, buf[i:i + 4]
            i += 4
        elif wire == 1:
            yield number, buf[i:i + 8]
            i += 8
        else:
            raise ValueError(f"wire type {wire}")


def decode_value(buf):
    for number, payload in fields(buf):
        if number == 1:
            return payload.decode("utf-8", "replace")
        if number in (4, 5):
            return payload
        if number == 6:
            return (payload >> 1) ^ -(payload & 1)
    return None


def read_layer(buf):
    name, keys, values, features, extent = None, [], [], [], 4096
    for number, payload in fields(buf):
        if number == 1:
            name = payload.decode("utf-8", "replace")
        elif number == 2:
            features.append(payload)
        elif number == 3:
            keys.append(payload.decode("utf-8", "replace"))
        elif number == 4:
            values.append(decode_value(payload))
        elif number == 5:
            extent = payload
    return {"name": name, "keys": keys, "values": values, "features": features, "extent": extent}


def read_feature(buf, keys, values):
    tags, geometry = [], []
    for number, payload in fields(buf):
        if number == 2:
            i = 0
            while i < len(payload):
                v, i = varint(payload, i)
                tags.append(v)
        elif number == 4:
            i = 0
            while i < len(payload):
                v, i = varint(payload, i)
                geometry.append(v)
    attrs = {}
    for j in range(0, len(tags) - 1, 2):
        if tags[j] < len(keys):
            attrs[keys[tags[j]]] = values[tags[j + 1]] if tags[j + 1] < len(values) else None
    return attrs, geometry


def first_point(geometry, extent, z, x, y):
    """Where a feature is, in degrees.

    For a point that is its position; for a line it is the first vertex, which for a street is
    one end of it. Good enough to drive to and to sort by distance, and far cheaper than a
    centroid over geometry we would otherwise not decode at all.
    """
    if len(geometry) < 3:
        return None
    command = geometry[0] & 0x7
    if command != 1:                         # not a MoveTo
        return None
    dx = (geometry[1] >> 1) ^ -(geometry[1] & 1)
    dy = (geometry[2] >> 1) ^ -(geometry[2] & 1)
    n = 2 ** z
    lon = (x + dx / extent) / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + dy / extent) / n))))
    return round(lat, 5), round(lon, 5)


def bounds_in(bbox, z):
    """Границы столбцов и строк прямоугольника на уровне z: (x0, x1, y0, y1), полуоткрытые."""
    west, south, east, north = bbox
    n = 2 ** z

    def col(lon):
        return int((lon + 180.0) / 360.0 * n)

    def row(lat):
        return int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)

    return (max(col(west), 0), min(col(east) + 1, n),
            max(row(north), 0), min(row(south) + 1, n))


def tiles_in(bbox, z):
    """Каждая координата прямоугольника. Остаётся ради прежнего способа чтения (см. scan)."""
    x0, x1, y0, y1 = bounds_in(bbox, z)
    for x in range(x0, x1):
        for y in range(y0, y1):
            yield x, y


def features_of(raw, zoom, x, y, wanted_layers):
    """Признаки нужных слоёв из одного тайла — общее тело обоих способов чтения."""
    for number, payload in fields(raw):
        if number != 3:
            continue
        layer = read_layer(payload)
        if layer["name"] not in wanted_layers:
            continue
        for feature in layer["features"]:
            attrs, geometry = read_feature(feature, layer["keys"], layer["values"])
            point = first_point(geometry, layer["extent"], zoom, x, y)
            if point:
                yield layer["name"], attrs, point


def scan(archive, pmtiles, bbox, zoom, wanted_layers, legacy=False):
    """Yield (layer, attrs, position) for every feature in the wanted layers.

    **Читает архив НАПРЯМУЮ.** Прежний способ — `pmtiles tile` отдельным процессом на каждый
    тайл — был терпим на городе и оказался непроходим на стране: Германия это 962 550 запусков
    процесса, и сборка её индекса была прервана на 137-й минуте, так и не закончив. Замер на
    одинаковой работе: 64.2 мс против 0.384 мс на тайл, то есть 167x. Почему выигрыш даёт именно
    устранение fork/exec, а не пропуск пустых тайлов, — в [pmtiles_archive]; коротко: после
    `extract --bbox` архив плотный внутри коробки, пропускать нечего.

    `legacy=True` возвращает прежний способ, и он оставлен ровно для одного — доказать, что
    новый отдаёт ТОТ ЖЕ индекс, а не похожий. Выбросив медленный путь, сравнивать быстрый было
    бы не с чем.

    Прямоугольник проверяется и здесь, хотя архив уже нарезан по нему: `pmtiles extract` вправе
    захватить кромку за краем коробки, и без отсечки два способа разошлись бы по краю — на
    величину маленькую, объяснимую и оттого особенно неприятную.
    """
    x0, x1, y0, y1 = bounds_in(bbox, zoom)

    if legacy:
        for x, y in tiles_in(bbox, zoom):
            raw = subprocess.run([pmtiles, "tile", archive, str(zoom), str(x), str(y)],
                                 capture_output=True).stdout
            if not raw:
                continue
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            for item in features_of(raw, zoom, x, y, wanted_layers):
                yield item
        return

    for _, x, y, raw in pmtiles_archive.tiles(archive, zoom=zoom):
        if x0 <= x < x1 and y0 <= y < y1:
            for item in features_of(raw, zoom, x, y, wanted_layers):
                yield item


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--places", required=True, help="the shipped basemap, for settlements")
    parser.add_argument("--detail", required=True, help="a z15 archive, for POIs and streets")
    parser.add_argument("--bbox", required=True)
    parser.add_argument("--pmtiles", default="pmtiles")
    parser.add_argument("--places-zoom", type=int, default=10)
    parser.add_argument("--detail-zoom", type=int, default=15)
    parser.add_argument("--legacy", action="store_true",
                        help="прежнее чтение процессом на тайл — только для сличения")
    args = parser.parse_args()

    bbox = tuple(float(v) for v in args.bbox.split(","))

    # ── settlements ─────────────────────────────────────────────────────────────────────────
    places = {}
    for _, attrs, point in scan(args.places, args.pmtiles, bbox, args.places_zoom, {"places"},
                                legacy=args.legacy):
        name, kind = attrs.get("name"), attrs.get("kind")
        if not name or kind not in PLACE_KINDS:
            continue
        key = (name, round(point[0], 2), round(point[1], 2))
        if key in places:
            continue
        population = attrs.get("population")
        places[key] = {"n": name, "k": kind, "y": point[0], "x": point[1],
                       "p": int(population) if isinstance(population, int) else 0}

    # ── streets and POIs, from the detail archive ───────────────────────────────────────────
    streets, pois = {}, {}
    kinds_seen = Counter()
    for layer, attrs, point in scan(args.detail, args.pmtiles, bbox, args.detail_zoom,
                                    {"roads", "pois"}, legacy=args.legacy):
        name, kind = attrs.get("name"), attrs.get("kind")
        # A NAME IS REQUIRED FOR TEXT, NOT FOR A CATEGORY. Most charging points and car parks
        # carry no name in OSM, and requiring one dropped charging from 34 to 3 and parking from
        # 792 to 29 over Chișinău — measured 2026-09-17. "Charging, 1.2 km" is a complete answer;
        # insisting on a name to show it would be tidiness deleting the feature.
        if not name and KIND_TO_CATEGORY.get(kind) is None:
            continue
        if layer == "roads":
            if not name:
                continue
            if kind not in ROAD_KINDS:
                continue
            # A street is many features; one row per name per neighbourhood is what a driver
            # wants to see. Rounding to ~1 km keeps both ends of a long avenue separate without
            # listing every block.
            key = (name, round(point[0], 2), round(point[1], 2))
            # ПРЕДСТАВИТЕЛЬ ВЫБИРАЕТСЯ ДЕТЕРМИНИРОВАННО, а не «кто первый попался».
            #
            # Улица — это много отрезков, и в ячейку попадает любой из них. Прежде хранился
            # первый встреченный, то есть ответ зависел от ПОРЯДКА ОБХОДА архива. Смена чтения
            # на прямое (порядок Гильберта вместо построчного) это и вскрыла: 2347 улиц из
            # 21 069 получили другого представителя — те же имя и ячейка, сдвиг координаты на
            # 187 м в медиане и до 1124 м, и ни одного отличия в остальных полях.
            #
            # Ни один из двух порядков не был правильнее другого, поэтому чинится не обход, а
            # произвол: минимум по (широта, долгота) даёт один и тот же ответ при любом способе
            # чтения. Заодно это делает индекс воспроизводимым — тот же архив даёт те же байты.
            here = (point[0], point[1])
            known = streets.get(key)
            if known is None or here < (known["y"], known["x"]):
                streets[key] = {"n": name, "y": point[0], "x": point[1]}
        else:
            kinds_seen[kind] += 1
            category = KIND_TO_CATEGORY.get(kind)
            # Unnamed rows dedupe on position alone, which is what makes two charging points in
            # one car park stay two rows rather than collapsing into one.
            key = (name or kind, round(point[0], 4), round(point[1], 4))
            if key in pois:
                continue
            row = {"n": name or "", "y": point[0], "x": point[1]}
            # The category is what a button filters on; the kind is kept for anything that has
            # no button, so it can still be named in a result row.
            if category:
                row["c"] = category
            if kind:
                row["k"] = kind
            pois[key] = row

    index = {
        "schema": 2,
        "placesZoom": args.places_zoom,
        "detailZoom": args.detail_zoom,
        "counts": {"places": len(places), "streets": len(streets), "pois": len(pois)},
        "categories": sorted(CATEGORIES),
        # ПОРЯДОК ЗАДАН ПОЛНОСТЬЮ — имени мало. Одноимённых улиц в стране сотни, а у POI имя
        # сплошь и рядом пустое (заправка без вывески — это строка с категорией и без названия),
        # так что сортировка по одному имени оставляла тысячи связок на усмотрение устойчивости
        # сортировки, то есть на порядок обхода архива. Пока читался он одним способом, это было
        # незаметно; смена чтения на прямое дала те же записи в другом порядке и другой файл.
        # Координата в ключе делает файл воспроизводимым: тот же архив — те же байты.
        "places": sorted(places.values(), key=lambda p: (-p["p"], p["n"], p["y"], p["x"])),
        "streets": sorted(streets.values(), key=lambda s: (s["n"], s["y"], s["x"])),
        "pois": sorted(pois.values(), key=lambda p: (p["n"], p["y"], p["x"])),
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(index, handle, ensure_ascii=False, separators=(",", ":"))

    print(f"   places  {len(places):6d}", file=sys.stderr)
    print(f"   streets {len(streets):6d}", file=sys.stderr)
    print(f"   pois    {len(pois):6d}", file=sys.stderr)
    by_category = Counter(p["c"] for p in pois.values() if "c" in p)
    print(f"   by category: {dict(by_category.most_common())}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""ПРОТОТИП: поисковый индекс страны как SQLite вместо JSON.

    usage: index-to-sqlite.py <in-index.json> <out.sqlite>

**Зачем.** Германия дала `de-index.json` в 323 МБ и 5 479 150 записей, которые
`SearchIndex.loadAll()` держит в куче целиком: 866 МБ постоянно, 1389 МБ на пике — на ОДНУ страну
из пяти, при линейном проходе по всем записям на каждое нажатие клавиши (§19). Индекс обязан жить
на диске.

**FTS4, а не FTS5, и это измерено на устройстве, а не выбрано.** Проба `TEST_SEARCH_ENGINE` в
приложении отвечает:

    версия SQLite 3.50.4 · FTS5 НЕТ (no such module) · FTS4 да · R*Tree НЕТ

Из чего следует вся схема ниже:
  * префиксный поиск у FTS4 есть — то, ради чего индекс и нужен: ответ по мере набора;
  * `unicode61` снимает диакритику САМ (`chisinau` находит `Chișinău`), поэтому отдельной
    свёрнутой колонки нет — она была бы лишним столбцом и лишними мегабайтами;
  * R*Tree отсутствует, но и не нужен: план запроса для «рядом по категории» доказан
    покрывающим индексом (`SEARCH poi USING COVERING INDEX`), а не предположен.

**Кириллица токенизатором НЕ сворачивается.** «Мюнхен» не найдёт München никаким
`remove_diacritics` — для этого нужна строка-псевдонима из `name:ru`. Поэтому колонка `alias`
существует отдельно от свёртки и решает другую задачу.

**Внешнее содержимое (`content='place'`).** FTS хранит только индекс, а сами строки живут в
обычной таблице — иначе текст лежал бы дважды. Триггеров синхронизации намеренно НЕТ: файл
собирается фабрикой один раз и дальше только читается, так что синхронизировать нечего.
"""
import json
import os
import sqlite3
import sys
import time

# Порядок члена перечисления — часть формата: приложение читает kind как число.
PLACE, STREET, POI = 0, 1, 2

SCHEMA = """
PRAGMA journal_mode = OFF;
PRAGMA synchronous  = OFF;

CREATE TABLE place(
    id    INTEGER PRIMARY KEY,
    kind  INTEGER NOT NULL,            -- 0 населённый пункт · 1 улица · 2 POI
    name  TEXT    NOT NULL,
    alias TEXT,                        -- name:en, name:ru через пробел; NULL когда их нет
    lat   REAL    NOT NULL,
    lon   REAL    NOT NULL,
    cat   TEXT,                        -- категория кнопки: charging, fuel… NULL если нет
    detail TEXT,                       -- собственное слово карты, когда кнопки нет
    pop   INTEGER NOT NULL DEFAULT 0   -- только населённые пункты
);

-- Порядок столбцов не случаен: запрос «рядом по категории» читается ЦЕЛИКОМ из индекса,
-- не заглядывая в таблицу. Доказано EXPLAIN QUERY PLAN на устройстве.
CREATE INDEX place_cat_pos ON place(cat, lat, lon);

CREATE VIRTUAL TABLE place_fts USING fts4(
    name, alias, tokenize=unicode61, content='place'
);
"""


def convert(src, dst):
    if os.path.exists(dst):
        os.remove(dst)
    index = json.load(open(src, encoding="utf-8"))

    db = sqlite3.connect(dst)
    db.executescript(SCHEMA)

    rows = []
    for row in index.get("places", ()):
        rows.append((PLACE, row["n"], None, row["y"], row["x"], None, row.get("k"), row.get("p", 0)))
    for row in index.get("streets", ()):
        rows.append((STREET, row["n"], None, row["y"], row["x"], None, None, 0))
    for row in index.get("pois", ()):
        rows.append((POI, row.get("n", ""), None, row["y"], row["x"],
                     row.get("c"), row.get("k"), 0))

    db.executemany(
        "INSERT INTO place(kind, name, alias, lat, lon, cat, detail, pop)"
        " VALUES (?,?,?,?,?,?,?,?)", rows)

    # Индекс строится ОДНИМ проходом после наполнения, а не по строке за раз: внешнее содержимое
    # именно для этого и существует, и вставка в FTS построчно была бы на порядок дороже.
    db.execute("INSERT INTO place_fts(place_fts) VALUES('rebuild')")
    db.commit()
    db.execute("VACUUM")
    db.close()
    return len(rows)


def sizes(path):
    """Во что обошёлся каждый кусок — вопрос «сколько занимает FTS» без этого не ответить."""
    db = sqlite3.connect(path)
    page = db.execute("PRAGMA page_size").fetchone()[0]
    out = {}
    for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')").fetchall():
        try:
            pages = db.execute("SELECT pgsize FROM dbstat WHERE name=?", (name,)).fetchall()
            out[name] = sum(p[0] for p in pages)
        except sqlite3.OperationalError:
            out = {}                      # dbstat собран не везде; тогда только общий размер
            break
    db.close()
    return page, out


def main(argv):
    src, dst = argv[1], argv[2]
    started = time.time()
    count = convert(src, dst)
    took = time.time() - started

    src_bytes, dst_bytes = os.path.getsize(src), os.path.getsize(dst)
    print("   исходный JSON   %13d байт  %8.1f МБ" % (src_bytes, src_bytes / 1048576))
    print("   SQLite          %13d байт  %8.1f МБ   (%.0f%% от JSON)"
          % (dst_bytes, dst_bytes / 1048576, 100 * dst_bytes / src_bytes))
    print("   строк           %13d" % count)
    print("   собран за       %13.1f с" % took)

    page, parts = sizes(dst)
    if parts:
        for name, size in sorted(parts.items(), key=lambda kv: -kv[1]):
            print("      %-24s %12d байт  %7.1f МБ" % (name, size, size / 1048576))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

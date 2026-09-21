"""Читать PMTiles v3 напрямую: заголовок, каталоги, тайлы — одним процессом.

**Появилось потому, что сборка индекса Германии не кончилась за 137 минут и была прервана.**
`build-index.py` обходил прямоугольник страны и запускал ОТДЕЛЬНЫЙ процесс `pmtiles tile` на
каждый тайл. Для Германии это 962 550 запусков процесса — цифра, вычисленная из bbox и затем
подтверждённая логом самой распаковки до единицы (`Region tiles 1284564` минус `addressed
322014` на z0–14 = ровно 962 550).

Стоимость измерена, а не оценена, на одинаковой работе — чтение, gunzip, полный разбор MVT:

    один процесс на тайл   64.2 мс
    прямое чтение           0.384 мс      = 167x

Для сравнения масштаба: обе распаковки Германии заняли **52 секунды**, а индексация тех же
данных — больше двух часов и не закончилась. Узким местом была не сеть и не диск, а fork/exec.

> **Разреженность тут НИ ПРИ ЧЁМ, и это стоит знать, прежде чем «оптимизировать» дальше.**
> Напрашивается мысль обходить только реально существующие записи, чтобы цена зависела от
> объёма данных, а не от площади. Измерено на Молдове: после `pmtiles extract --bbox` архив
> ПЛОТНЫЙ внутри коробки — 34 155 записей на z14 против 34 155 тайлов прямоугольника, то есть
> 100 %. Пропускать нечего. Дедупликация тоже почти не работает: `addressed 45768` против
> `contents 45072`. Весь выигрыш даёт устранение запуска процесса, и только он.

Формат: https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md
"""
import gzip
import struct

# Заголовок v3 — ровно 127 байт фиксированной раскладки.
HEADER_BYTES = 127
_MAGIC = b"PMTiles"

COMPRESSION_NONE = 1
COMPRESSION_GZIP = 2


def _varint(buf, i):
    result = 0
    shift = 0
    while True:
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def read_header(handle):
    handle.seek(0)
    raw = handle.read(HEADER_BYTES)
    if raw[:7] != _MAGIC or raw[7] != 3:
        raise ValueError("не PMTiles v3")
    u64 = lambda offset: struct.unpack_from("<Q", raw, offset)[0]
    return {
        "root_offset": u64(8), "root_bytes": u64(16),
        "leaf_offset": u64(40), "leaf_bytes": u64(48),
        "data_offset": u64(56), "data_bytes": u64(64),
        # Три РАЗНЫХ счётчика, и путать их дорого: addressed — сколько тайлов адресуемо
        # (прогоны раскрыты), entries — сколько записей в каталогах, contents — сколько
        # различных блобов. Обход обязан давать addressed; это и есть самопроверка ниже.
        "addressed": u64(72), "entries": u64(80), "contents": u64(88),
        "clustered": raw[96],
        "internal_compression": raw[97], "tile_compression": raw[98],
        "tile_type": raw[99], "min_zoom": raw[100], "max_zoom": raw[101],
    }


def _decompress(blob, compression):
    return gzip.decompress(blob) if compression == COMPRESSION_GZIP else blob


def read_directory(blob):
    """Каталог -> [(tile_id, offset, length, run_length)].

    Сериализация — четыре столбца варинтов подряд, а не записи вперемешку: сначала все дельты
    идентификаторов, затем все длины прогонов, затем все длины, затем все смещения.
    """
    count, i = _varint(blob, 0)

    ids = []
    last = 0
    for _ in range(count):
        delta, i = _varint(blob, i)
        last += delta
        ids.append(last)

    runs = []
    for _ in range(count):
        value, i = _varint(blob, i)
        runs.append(value)

    lengths = []
    for _ in range(count):
        value, i = _varint(blob, i)
        lengths.append(value)

    # Смещение 0 — не смещение, а «сразу за предыдущим тайлом». Иначе значение сдвинуто на 1,
    # потому что ноль занят под этот признак.
    offsets = []
    for k in range(count):
        value, i = _varint(blob, i)
        if value == 0 and k > 0:
            offsets.append(offsets[k - 1] + lengths[k - 1])
        else:
            offsets.append(value - 1)

    return list(zip(ids, offsets, lengths, runs))


def tile_id_to_zxy(tile_id):
    """Обратная кривая Гильберта — та же, которой tile_id и закодирован.

    Порядок Гильберта, а не построчный: соседние по идентификатору тайлы соседние и на земле,
    отчего диапазонные запросы к удалённому архиву попадают в непрерывные куски. Нам от этого
    достаётся побочная выгода — обход каталога идёт почти линейно по файлу.
    """
    accumulated = 0
    zoom = 0
    while True:
        count = 1 << (zoom * 2)
        if accumulated + count > tile_id:
            break
        accumulated += count
        zoom += 1

    position = tile_id - accumulated
    side = 1 << zoom
    x = y = 0
    t = position
    step = 1
    while step < side:
        rx = 1 & (t // 2)
        ry = 1 & (t ^ rx)
        if ry == 0:
            if rx == 1:
                x = step - 1 - x
                y = step - 1 - y
            x, y = y, x
        x += step * rx
        y += step * ry
        t //= 4
        step *= 2
    return zoom, x, y


def tiles(path, zoom=None, verify=True):
    """Отдаёт (z, x, y, data) для каждого адресуемого тайла. Данные уже распакованы.

    `zoom` сужает до одного уровня — и сужает ЧТЕНИЕ, а не только выдачу: блоб чужого уровня
    не читается с диска вовсе.

    `verify` сверяет число пройденных тайлов с `addressed_tiles_count` из заголовка. Это две
    независимо посчитанные величины, так что расхождение означает ошибку разбора каталога, а
    молчаливо неполный индекс — ровно тот отказ, которого никто не заметит.
    """
    with open(path, "rb") as handle:
        header = read_header(handle)

        handle.seek(header["root_offset"])
        root = read_directory(
            _decompress(handle.read(header["root_bytes"]), header["internal_compression"]))

        seen = 0
        pending = [root]
        while pending:
            for tile_id, offset, length, run in pending.pop():
                if run == 0:
                    # Запись-указатель на лист, а не тайл: длина прогона нулевая.
                    handle.seek(header["leaf_offset"] + offset)
                    pending.append(read_directory(
                        _decompress(handle.read(length), header["internal_compression"])))
                    continue

                # ПРОГОН РАСКРЫВАЕТСЯ ПОШТУЧНО. Один блоб может стоять за несколькими
                # идентификаторами подряд (дедупликация одинаковых тайлов). Каждому нужны свои
                # x/y, потому что индекс переводит координаты внутри тайла в широту и долготу
                # через z/x/y — оставить только первый значило бы потерять остальные молча.
                blob = None
                for k in range(run):
                    z, x, y = tile_id_to_zxy(tile_id + k)
                    seen += 1
                    if zoom is not None and z != zoom:
                        continue
                    if blob is None:
                        handle.seek(header["data_offset"] + offset)
                        blob = _decompress(handle.read(length), header["tile_compression"])
                    if blob:
                        yield z, x, y, blob

        if verify and seen != header["addressed"]:
            raise ValueError(
                "каталог разобран неверно: пройдено %d тайлов, в заголовке %d"
                % (seen, header["addressed"]))

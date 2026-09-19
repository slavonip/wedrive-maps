#!/usr/bin/env bash
# Собрать ОДНУ страну как самостоятельный пакет.
#
# Отличие от scripts/build-graph.sh не в размере, а в модели. Тот собирает пакет из НЕСКОЛЬКИХ
# экстрактов одним проходом, потому что старая схема требовала, чтобы граница существовала
# внутри одного графа. Здесь наоборот: каждая страна собирается в одиночку и ничего не знает о
# соседях, а границу делает внешняя таблица порталов. Это и есть то, ради чего всё затевалось —
# страну можно обновить, не пересобирая соседей.
#
#   usage: regional-build.sh <код> <geofabrik-путь> <каталог-выхода>
#   пример: regional-build.sh RO europe/romania /data/out
#
# Работает внутри образа движка (см. docker/engine.Dockerfile). Всё, к чему прикасается, лежит
# под каталогом выхода.
set -euo pipefail

CODE="${1:?код страны, например RO}"
GEO="${2:?путь Geofabrik, например europe/romania}"
OUT="${3:-/data/out}"

low="$(echo "$CODE" | tr 'A-Z' 'a-z')"
WORK="$OUT/work_$low"
SRC="$OUT/src"
mkdir -p "$WORK/tiles" "$SRC" "$OUT"

PBF="$SRC/$(basename "$GEO")-latest.osm.pbf"
if [ ! -f "$PBF" ]; then
  echo "==> качаю $GEO"
  curl -fsSL -o "$PBF.part" "https://download.geofabrik.de/${GEO}-latest.osm.pbf"
  mv "$PBF.part" "$PBF"
fi
echo "==> экстракт: $(du -h "$PBF" | cut -f1)"

# Версия графа — это дата данных OSM, а не дата сборки. Две сборки из одного экстракта
# взаимозаменяемы, из разных — нет, и именно по этой дате таблица порталов привязана к паре.
DATA_DATE="$(osmium fileinfo -g header.option.osmosis_replication_timestamp "$PBF" | cut -c1-10)"
[ -n "$DATA_DATE" ] || { echo "экстракт без отметки времени — версию графа назвать нечем" >&2; exit 1; }
echo "==> дата данных: $DATA_DATE"

CONF="$WORK/valhalla.json"
valhalla_build_config \
  --mjolnir-tile-dir "$WORK/tiles" \
  --mjolnir-admin "$WORK/admins.sqlite" \
  --mjolnir-timezone "$WORK/timezones.sqlite" \
  --mjolnir-concurrency "$(nproc)" > "$CONF"
python3 - "$CONF" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
# tile_extract указывает на архив, которого ещё нет: читатель попытается его открыть и
# засорит журнал ошибкой, которая не является ошибкой.
c["mjolnir"].pop("tile_extract", None)
json.dump(c, open(sys.argv[1], "w"), indent=2)
PY

# Admin-база обязательна, и не ради формальности: именно по ней portal_border отличает
# пограничный узел от обычного — у ребра один конец в своей стране, другой без страны.
# Без неё граница не найдётся вовсе, и пакет соберётся «успешно» без единого портала.
echo "==> admin"
valhalla_build_admins -c "$CONF" "$PBF" > "$WORK/admins.log" 2>&1
test -s "$WORK/admins.sqlite" || { echo "admin-база пуста"; tail -20 "$WORK/admins.log"; exit 1; }

echo "==> часовые пояса"
valhalla_build_timezones > "$WORK/timezones.sqlite" 2>"$WORK/tz.log" || {
  echo "часовые пояса не собрались — время прибытия будет считаться по часам головного устройства" >&2
  rm -f "$WORK/timezones.sqlite"
}

echo "==> тайлы"
valhalla_build_tiles -c "$CONF" "$PBF" > "$WORK/tiles.log" 2>&1
TILES="$(find "$WORK/tiles" -name '*.gph' | wc -l)"
[ "$TILES" -gt 0 ] || { echo "тайлов не получилось"; tail -30 "$WORK/tiles.log"; exit 1; }
echo "==> тайлов: $TILES, $(du -sh "$WORK/tiles" | cut -f1)"

# -O обязателен. Без него valhalla_build_extract отказывается заменить существующий архив, а
# set -e убивает скрипт до того, как это станет заметно: рядом с новыми тайлами остаётся СТАРЫЙ
# .tar, и публикуется он. Этот репозиторий уже ловил такое на md-ro.
TAR="$OUT/${low}-${DATA_DATE}.tar"
echo "==> упаковка"
valhalla_build_extract -c "$CONF" -O -v "$TAR" > "$WORK/extract.log" 2>&1
test -s "$TAR" || { echo "архив не собрался"; tail -20 "$WORK/extract.log"; exit 1; }

SHA="$(sha256sum "$TAR" | cut -d' ' -f1)"
BYTES="$(stat -c %s "$TAR")"
echo "==> $TAR  $(du -h "$TAR" | cut -f1)  $SHA"

cat > "$OUT/${low}.package.json" <<JSON
{
  "code": "$CODE",
  "geofabrik": "$GEO",
  "graph_version": "$DATA_DATE",
  "tiles": $TILES,
  "package": "$(basename "$TAR")",
  "bytes": $BYTES,
  "sha256": "$SHA",
  "engine": "$(cat /etc/wedrive-engine-ref 2>/dev/null || echo unknown)",
  "engine_sha": "$(cat /etc/wedrive-engine-sha 2>/dev/null || echo unknown)"
}
JSON
echo "==> описание: $OUT/${low}.package.json"

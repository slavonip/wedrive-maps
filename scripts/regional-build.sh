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
URL="https://download.geofabrik.de/${GEO}-latest.osm.pbf"
# The extract is used only after its MD5 matches the one Geofabrik publishes beside it, and only
# then renamed into place: a half-written or rolled-over download never becomes the input.
#
# WHILE GEOFABRIK PUBLISHES A NEW EXTRACT ITS FILES CONTRADICT EACH OTHER. Measured 2026-09-25
# 23:07 UTC (run 36199550296): hungary-latest already redirected to hungary-260925 while
# hungary-latest.osm.pbf.md5 still named yesterday's file, twice in a row. Retrying at once cannot
# outrun that, so a mismatch waits MD5_WAIT seconds and tries again, MD5_ATTEMPTS times (default
# one hour), and only then fails the run — never builds from a file that does not match.
MD5_ATTEMPTS="${MD5_ATTEMPTS:-7}"; MD5_WAIT="${MD5_WAIT:-600}"
if [ ! -f "$PBF" ]; then
  for attempt in $(seq 1 "$MD5_ATTEMPTS"); do
    MD5_WANT="$(curl -fsSL "$URL.md5" | awk '{print $1}')"
    [ "${#MD5_WANT}" = 32 ] || { echo "no MD5 published at $URL.md5" >&2; exit 1; }
    echo "==> качаю $GEO (md5 $MD5_WANT)"
    curl -fsSL -o "$PBF.part" "$URL"
    if [ "$(md5sum "$PBF.part" | cut -d' ' -f1)" = "$MD5_WANT" ]; then
      mv "$PBF.part" "$PBF"; echo "$MD5_WANT" > "$PBF.md5"; break
    fi
    rm -f "$PBF.part"; echo "md5 не сошлась (попытка $attempt из $MD5_ATTEMPTS)" >&2
    if [ "$attempt" = "$MD5_ATTEMPTS" ]; then
      echo "Geofabrik так и не дал согласованный экстракт $GEO — не собираю" >&2; exit 1
    fi
    sleep "$MD5_WAIT"
  done
fi
MD5="$(cat "$PBF.md5" 2>/dev/null || md5sum "$PBF" | cut -d' ' -f1)"
PBF_SHA="$(sha256sum "$PBF" | cut -d' ' -f1)"
PBF_BYTES="$(stat -c %s "$PBF")"
echo "==> экстракт: $(du -h "$PBF" | cut -f1)"

# Версия графа — это дата данных OSM, а не дата сборки. Две сборки из одного экстракта
# взаимозаменяемы, из разных — нет, и именно по этой дате таблица порталов привязана к паре.
REPLICATION="$(osmium fileinfo -g header.option.osmosis_replication_timestamp "$PBF")"
SEQUENCE="$(osmium fileinfo -g header.option.osmosis_replication_sequence_number "$PBF")"
DATA_DATE="$(echo "$REPLICATION" | cut -c1-10)"
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

# Часовые пояса — ЗАКРЕПЛЁННЫЙ вход (maps-vendor, sha в regional-engine.lock), а не сборка на
# месте. Раньше здесь был valhalla_build_timezones с проглоченной ошибкой: сбой молча давал
# страну без поясов, а успех — набор данных той версии, которую скрипт выберет в тот день.
echo "==> часовые пояса"
: "${TIMEZONES_DB:?путь к закреплённой базе часовых поясов (TIMEZONES_DB)}"
: "${TIMEZONES_SHA256:?её sha256 (TIMEZONES_SHA256)}"
[ "$(sha256sum "$TIMEZONES_DB" | cut -d' ' -f1)" = "$TIMEZONES_SHA256" ] \
  || { echo "база часовых поясов не сходится с regional-engine.lock" >&2; exit 1; }
cp "$TIMEZONES_DB" "$WORK/timezones.sqlite"

echo "==> тайлы"
valhalla_build_tiles -c "$CONF" "$PBF" > "$WORK/tiles.log" 2>&1
TILES="$(find "$WORK/tiles" -name '*.gph' | wc -l)"
[ "$TILES" -gt 0 ] || { echo "тайлов не получилось"; tail -30 "$WORK/tiles.log"; exit 1; }
echo "==> тайлов: $TILES, $(du -sh "$WORK/tiles" | cut -f1)"

# FRONTIER — the border nodes of THIS graph with the OSM ids of THIS extract. It is what lets the
# country be updated alone: the device joins frontiers of any two versions (rule D1), so nothing
# here names a neighbour or a neighbour's version. A frontier that fails its gates (collision,
# impossible level, malformed) stops the country: a package without one could not be joined.
echo "==> frontier"
FRONTIER="$OUT/${low}-${DATA_DATE}.frontier"
python3 "$(dirname "$0")/regional-frontier.py" build "$WORK/tiles" "$PBF" "$CODE" "$DATA_DATE" "$FRONTIER" \
  || { echo "frontier $CODE не прошёл проверку — пакет не выпускается" >&2; exit 1; }
FR_SHA="$(sha256sum "$FRONTIER" | cut -d' ' -f1)"
FR_BYTES="$(stat -c %s "$FRONTIER")"
FR_ENTRIES="$(sed -n 's/^entries //p' "$FRONTIER")"
FR_OSM="$(sed -n 's/^osm_resolved //p' "$FRONTIER")"

# NAVIGATION FEATURES — cameras, enforcement (red light, section control), level crossings and
# traffic signals of THIS extract: the same OSM snapshot as the graph, so every way id in it names
# a way of this graph version. Updated with the country and only with it. The gates against the
# previous release (a type gone to zero, far-off coordinates) run in the factory's manifest step.
echo "==> features"
FEATURES="$OUT/${low}-${DATA_DATE}.features"
python3 "$(dirname "$0")/regional-features.py" build "$PBF" "$CODE" "$DATA_DATE" "$FEATURES" \
  --source "$REPLICATION" \
  || { echo "features $CODE не извлеклись — пакет не выпускается" >&2; exit 1; }
FE_SHA="$(sha256sum "$FEATURES" | cut -d' ' -f1)"
FE_BYTES="$(stat -c %s "$FEATURES")"
FE_ENTRIES="$(sed -n 's/^entries //p' "$FEATURES")"
FE_COUNTS="$(python3 "$(dirname "$0")/regional-features.py" counts "$FEATURES")"

# ПУТЬ АРХИВА ЗАДАЁТСЯ В КОНФИГЕ, а не аргументом: valhalla_build_extract пишет туда, куда
# указывает mjolnir.tile_extract. Позиционного аргумента у него нет вовсе, и переданный
# argparse отвергает с кодом 2. Отдельный конфиг под упаковку нужен потому, что в основном
# tile_extract снят намеренно — иначе читатель при сборке тайлов ищет несуществующий архив и
# засоряет журнал ошибкой, которая ошибкой не является.
#
# -O обязателен. Без него отказ заменить существующий архив, и рядом с новыми тайлами остаётся
# СТАРЫЙ .tar — а публикуется он. Этот репозиторий уже ловил такое на md-ro.
TAR="$OUT/${low}-${DATA_DATE}.tar"
PACKCONF="$WORK/valhalla_pack.json"
python3 - "$CONF" "$PACKCONF" "$TAR" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
c["mjolnir"]["tile_extract"] = sys.argv[3]
json.dump(c, open(sys.argv[2], "w"), indent=2)
PY
echo "==> упаковка -> $TAR"
# Без `|| { ... }` набор set -e убивает скрипт кодом самой программы, и журнал, ради которого
# он писался, остаётся непрочитанным.
valhalla_build_extract -c "$PACKCONF" -O > "$WORK/extract.log" 2>&1 || {
  echo "valhalla_build_extract упал:"; tail -25 "$WORK/extract.log"; exit 1; }
test -s "$TAR" || { echo "архив не собрался:"; tail -25 "$WORK/extract.log"; exit 1; }

SHA="$(sha256sum "$TAR" | cut -d' ' -f1)"
BYTES="$(stat -c %s "$TAR")"
echo "==> $TAR  $(du -h "$TAR" | cut -f1)  $SHA"

# Описание пакета с происхождением: откуда экстракт (URL, MD5 Geofabrik, sha256, отметка
# репликации), каким движком и какой базой поясов собран. Манифест переносит это как есть, а
# precheck следующего месяца по нему решает, нужна ли пересборка.
cat > "$OUT/${low}.package.json" <<JSON
{
  "code": "$CODE",
  "geofabrik": "$GEO",
  "graph_version": "$DATA_DATE",
  "tiles": $TILES,
  "package": "$(basename "$TAR")",
  "bytes": $BYTES,
  "sha256": "$SHA",
  "tar_bytes": $BYTES,
  "tar_sha256": "$SHA",
  "engine": "$(cat /etc/wedrive-engine-ref 2>/dev/null || echo unknown)",
  "engine_sha": "$(cat /etc/wedrive-engine-sha 2>/dev/null || echo unknown)",
  "source": {
    "url": "$URL",
    "md5": "$MD5",
    "sha256": "$PBF_SHA",
    "bytes": $PBF_BYTES,
    "replication": "$REPLICATION",
    "sequence": "$SEQUENCE"
  },
  "timezones": {"sha256": "$TIMEZONES_SHA256"},
  "frontier": {
    "format": "wedrive-frontier/1",
    "file": "$(basename "$FRONTIER")",
    "bytes": $FR_BYTES,
    "sha256": "$FR_SHA",
    "entries": $FR_ENTRIES,
    "osm_resolved": $FR_OSM
  },
  "features": {
    "format": "wedrive-features/1",
    "file": "$(basename "$FEATURES")",
    "bytes": $FE_BYTES,
    "sha256": "$FE_SHA",
    "entries": $FE_ENTRIES,
    "counts": $FE_COUNTS
  }
}
JSON
echo "==> описание: $OUT/${low}.package.json"

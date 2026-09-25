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
# then renamed into place: a half-written or rolled-over download never becomes the input. One
# retry covers Geofabrik publishing a new extract while this one was downloading.
if [ ! -f "$PBF" ]; then
  for attempt in 1 2; do
    MD5_WANT="$(curl -fsSL "$URL.md5" | awk '{print $1}')"
    [ "${#MD5_WANT}" = 32 ] || { echo "no MD5 published at $URL.md5" >&2; exit 1; }
    echo "==> качаю $GEO (md5 $MD5_WANT)"
    curl -fsSL -o "$PBF.part" "$URL"
    if [ "$(md5sum "$PBF.part" | cut -d' ' -f1)" = "$MD5_WANT" ]; then
      mv "$PBF.part" "$PBF"; echo "$MD5_WANT" > "$PBF.md5"; break
    fi
    rm -f "$PBF.part"; echo "md5 не сошлась (попытка $attempt)" >&2
    [ "$attempt" = 2 ] && exit 1
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
  "timezones": {"sha256": "$TIMEZONES_SHA256"}
}
JSON
echo "==> описание: $OUT/${low}.package.json"

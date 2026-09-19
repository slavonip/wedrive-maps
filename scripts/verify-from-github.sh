#!/usr/bin/env bash
# Проверка того, что фабрика на самом деле произвела: маршруты на артефактах, СКАЧАННЫХ ИЗ
# GitHub, а не на том, что осталось в рабочем каталоге сборки.
#
# Разница не формальная. Прогон конвейера доказывает, что сборка прошла; эта проверка
# доказывает, что ОПУБЛИКОВАННОЕ пригодно — что манифест указывает на существующие файлы, что
# суммы сходятся, что таблицы порталов соответствуют именно этим пакетам и что машина по ним
# поедет. Ровно это и делает устройство, и ровно здесь становится видно, что локальная фабрика
# больше не нужна.
#
#   usage: verify-from-github.sh <url-манифеста> [каталог]
#   пример: verify-from-github.sh https://raw.githubusercontent.com/slavonip/wedrive-maps/main/manifest.json
set -euo pipefail

MANIFEST_URL="${1:?url манифеста}"
WORK="${2:-/data/fromgithub}"
mkdir -p "$WORK/dl" "$WORK/installed"

echo "==> манифест: $MANIFEST_URL"
curl -fsSL -o "$WORK/manifest.json" "$MANIFEST_URL"
python3 -c "import json;m=json.load(open('$WORK/manifest.json'));print('    движок:',m['engine']['version']);print('    страны:',', '.join('%s %s'%(k,v['graph_version']) for k,v in m['regions'].items()));print('    границы:',', '.join(m['portals']))"

echo "==> качаю пакеты и таблицы"
python3 - "$WORK" <<'PY'
import hashlib, json, os, subprocess, sys
work = sys.argv[1]
m = json.load(open(os.path.join(work, "manifest.json"), encoding="utf-8"))

def fetch(url, dst, want_sha, want_bytes=None):
    if not os.path.isfile(dst):
        subprocess.run(["curl", "-fsSL", "-o", dst + ".part", url], check=True)
        os.replace(dst + ".part", dst)
    h = hashlib.sha256()
    with open(dst, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    got = h.hexdigest()
    size = os.path.getsize(dst)
    ok = got == want_sha and (want_bytes is None or size == want_bytes)
    print("    %-28s %10.1f МБ  %s" % (os.path.basename(dst), size / 1048576.0,
                                       "сумма сошлась" if ok else "СУММА НЕ СОШЛАСЬ"))
    if not ok:
        raise SystemExit("скачанный файл не тот, что обещал манифест: %s" % dst)

for code, r in m["regions"].items():
    fetch(r["url"], os.path.join(work, "dl", r["package"]), r["sha256"], r["bytes"])
for name, t in m["portals"].items():
    fetch(t["url"], os.path.join(work, "dl", t["file"]), t["sha256"], t["bytes"])
PY

echo "==> распаковка"
python3 - "$WORK" <<'PY'
import json, os, subprocess, sys
work = sys.argv[1]
m = json.load(open(os.path.join(work, "manifest.json"), encoding="utf-8"))
for code, r in m["regions"].items():
    d = os.path.join(work, "installed", code, "tiles")
    os.makedirs(d, exist_ok=True)
    subprocess.run(["tar", "-xf", os.path.join(work, "dl", r["package"]), "-C", d], check=True)
    n = sum(len([f for f in fs if f.endswith(".gph")]) for _, _, fs in os.walk(d))
    print("    %-4s %d тайлов" % (code, n))
PY

echo "==> конфигурация маршрутизатора"
python3 - "$WORK" <<'PY'
import json, os, subprocess, sys
work = sys.argv[1]
m = json.load(open(os.path.join(work, "manifest.json"), encoding="utf-8"))
first = sorted(m["regions"])[0]
subprocess.run("valhalla_build_config --mjolnir-tile-dir %s/installed/%s/tiles > %s/router.json"
               % (work, first, work), shell=True, check=True)
c = json.load(open(os.path.join(work, "router.json"), encoding="utf-8"))
mj = c["mjolnir"]
mj.pop("tile_extract", None)
mj["wedrive_regions"] = [{"id": r["region_id"], "dir": "%s/installed/%s/tiles" % (work, code)}
                         for code, r in sorted(m["regions"].items())]
allp = os.path.join(work, "all.portals")
with open(allp, "w", encoding="utf-8") as out:
    for t in m["portals"].values():
        out.write(open(os.path.join(work, "dl", t["file"]), encoding="utf-8").read())
mj["wedrive_portals"] = allp
json.dump(c, open(os.path.join(work, "router.json"), "w"), indent=1)
print("    регионов %d, строк порталов %d"
      % (len(mj["wedrive_regions"]),
         sum(1 for l in open(allp, encoding="utf-8") if l.strip() and not l.startswith("#"))))
PY

echo "==> маршруты на скачанном"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODES=$(python3 -c "import json;print(' '.join(json.load(open('$WORK/manifest.json'))['regions']))")
python3 "$HERE/regional-regress.py" "$HERE/../regional.json" "$WORK/router.json" $CODES

#!/usr/bin/env python3
"""Download the national charging registries (charging/nap-sources.json) for wedrive-charging/2.

    charging-nap-fetch.py <OUT dir> [--only ID,ID]

Every source is a keyless HTTPS file. A source that fails (HTTP error, a timeout, an empty or
unparseable answer) is REPORTED and skipped — the run goes on; the build then has no rows from it,
and the publish gate against the previous release (charging_db.nap_gate) decides whether that
catalogue may replace the previous one.

OUT/<id>.bin             the body as served, bytes untouched (gzip or not)
OUT/<id>.status.bin      LT only: the DATEX status publication, read for the live keys
OUT/report.json          {fetched, sources: {id: {ok, url, bytes, sha256, last_modified, etag,
                          http, seconds, error, records}}}

`fetched` is the time of THIS fetch — the only clock value the catalogue carries from it, and it is
written into the /2 meta as nap_fetched, never the build's own clock.
DE's file name carries its date and moves each month: its URL is read from the BNetzA page.

Standard library only.
"""
import hashlib, json, os, re, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import charging_nap as nap  # noqa: E402

UA = "WeDrive-charging-factory/1.0 (+https://github.com/slavonip/wedrive-maps)"
TIMEOUT = 300
ATTEMPTS = 3


def get(url, headers=None, timeout=TIMEOUT, opener=urllib.request.urlopen):
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA, "Accept-Encoding": "gzip"}, **(headers or {})))
    with opener(req, timeout=timeout) as r:
        return r.status, r.read(), dict(r.headers.items())


def fetch_one(src, out, report, opener=urllib.request.urlopen, sleep=time.sleep):
    rec = {"ok": False}
    t0 = time.time()
    last = None
    for attempt in range(ATTEMPTS):
        try:
            url = src.get("url")
            if not url and src.get("url_from_page"):
                _, page, _ = get(src["url_from_page"], src.get("headers"), opener=opener)
                found = sorted(set(re.findall(src["url_pattern"], nap.unzip(page).decode("utf-8", "replace"))))
                if not found:
                    raise ValueError("no file link matching %s on %s" % (src["url_pattern"], src["url_from_page"]))
                url = found[-1]                     # the dated names sort by date
            rec["url"] = url
            code, body, hdr = get(url, src.get("headers"), opener=opener)
            if code != 200 or not body:
                raise ValueError("HTTP %s, %d bytes" % (code, len(body or b"")))
            status = None
            if src.get("status_url"):
                c2, status, _ = get(src["status_url"], src.get("headers"), opener=opener)
                if c2 != 200:
                    status = None
            # parse once here, so a body the reader cannot read fails THIS source and not the build
            data = nap.unzip(body)
            sites = nap.READERS[src["format"]](data, nap.unzip(status) if status else None) \
                if src["format"] == "datex-table" else nap.READERS[src["format"]](data)
            if not sites:
                raise ValueError("no records in the answer")
            with open(os.path.join(out, src["id"] + ".bin"), "wb") as f:
                f.write(body)
            if status:
                with open(os.path.join(out, src["id"] + ".status.bin"), "wb") as f:
                    f.write(status)
            low = {k.lower(): v for k, v in hdr.items()}
            rec.update(ok=True, http=code, bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
                       last_modified=low.get("last-modified"), etag=low.get("etag"), records=len(sites),
                       status_bytes=len(status) if status else None)
            break
        except Exception as e:                       # noqa: BLE001 — reported, never fatal
            last = "%s: %s" % (type(e).__name__, e)
            if attempt + 1 < ATTEMPTS:
                sleep(10 * (attempt + 1))
    if not rec["ok"]:
        rec["error"] = last
    rec["seconds"] = round(time.time() - t0, 1)
    report["sources"][src["id"]] = rec
    return rec


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    out = argv[0]
    only = None
    if "--only" in argv:
        only = set(argv[argv.index("--only") + 1].split(","))
    cfg = json.load(open(os.path.join(ROOT, "charging", "nap-sources.json"), encoding="utf-8"))
    os.makedirs(out, exist_ok=True)
    report = {"fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sources": {}}
    for src in cfg["sources"]:
        if only and src["id"] not in only:
            continue
        r = fetch_one(src, out, report)
        if r["ok"]:
            print("  %-15s %10d B  %6d records  %s" % (src["id"], r["bytes"], r["records"], r["url"]))
        else:
            print("::warning::national registry %s failed: %s" % (src["id"], r["error"]))
    with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    ok = sum(1 for r in report["sources"].values() if r["ok"])
    print("national registries: %d of %d fetched" % (ok, len(report["sources"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

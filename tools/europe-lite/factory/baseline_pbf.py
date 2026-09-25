# -*- coding: utf-8 -*-
"""B1' baseline input: undo, on the BASELINE side only, the one tag change promote.py makes.

    osmium cat -f opl europe_lite_final.osm.pbf \
      | python3 baseline_pbf.py CONNECTOR_IDS \
      | osmium cat -F opl -f pbf -o europe_lite_baseline.osm.pbf -

promote.py renames `service=<v>` to `wedrive:service=<v>` on connector ways when <v> is one of the
values from which Valhalla infers destination_only. That rename is what lets the stock ferry
reclassification reach, and lift destination_only from, the ferry-access edges the transit gate
exists to examine. Left on both sides of the comparison it becomes invisible, and the transit gate
checks nothing (measured on final2: 0 pairs instead of 216). Undone here, the baseline is exactly
what final2 was accepted against: for final2 the result is byte-identical to europe_lite2.osm.pbf,
the input of t_europe2.

Only ways listed in CONNECTOR_IDS (one way id per line) are touched, and only a tag promote.py
itself writes (its RENAMED_KEY with one of its AUTO_DESTONLY_SERVICE values, both imported from
promote.py so the two can never disagree). Everything else passes through byte for byte. The
production pipeline and promote.py are not changed.

Counts go to stderr; exit code 1 if a connector carries the renamed key with a value promote.py
would never write (the input is then not what promote.py made).
"""
import os, sys, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from promote import AUTO_DESTONLY_SERVICE, RENAMED_KEY  # noqa: E402  (read-only use)

PREFIX = (RENAMED_KEY + "=").encode()
AUTO = {v.encode() for v in AUTO_DESTONLY_SERVICE}


def undo_line(line, connectors):
    """One OPL line (bytes) -> (line, renamed back?, foreign renamed key?). Bytes in, bytes out, so
    every line that is not changed passes through untouched whatever its encoding."""
    if line[:1] != b"w":
        return line, False, False
    parts = line.rstrip(b"\n").split(b" ")
    if parts[0][1:] not in connectors:
        return line, False, PREFIX in line
    for i, f in enumerate(parts):
        if f[:1] == b"T" and PREFIX in f:
            kvs = f[1:].split(b",")
            for j, kv in enumerate(kvs):
                if kv.startswith(PREFIX):
                    v = kv[len(PREFIX):]
                    if urllib.parse.unquote_to_bytes(v) not in AUTO:
                        raise ValueError("w%s carries %s%s, which promote.py never writes"
                                         % (parts[0][1:].decode(), PREFIX.decode(), v.decode(errors="replace")))
                    kvs[j] = b"service=" + v
                    parts[i] = b"T" + b",".join(kvs)
                    return b" ".join(parts) + b"\n", True, False
    return line, False, False


def main():
    ids = {l.strip().lstrip("w").encode() for l in open(sys.argv[1], encoding="utf-8") if l.strip()}
    back = foreign = 0
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        try:
            l, b, fo = undo_line(line, ids)
        except ValueError as e:
            print("FAIL:", e, file=sys.stderr); return 1
        back += b; foreign += fo
        out.write(l)
    out.flush()
    print("connector ways %d; renamed back %d; renamed key outside the connectors (left alone) %d"
          % (len(ids), back, foreign), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

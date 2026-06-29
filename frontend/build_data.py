#!/usr/bin/env python3
"""Rebuild the dashboard's window.SFD_DATA from raw/ and swap it into the bundle.

The dashboard (frontend/Design System.html) is a self-contained artifact: every
asset is gzip+base64-embedded in a __bundler/manifest blob. The only thing that
varies with the data is one JS asset that sets `window.SFD_DATA`. This script
regenerates that asset from raw/matters/*.json and rewrites it in place, so the
single-file demo keeps working (just double-click the HTML).

Scope: the current 11-member Board of Supervisors (DISTRICTS below). raw/ also
holds votes from former supervisors, but the chart needs a district per rep and
we only have it for the sitting board. ponytail: 11 reps, add the rest when we
have a full person->district map and the demo needs historical members.

Run:  python frontend/build_data.py
"""
import base64, glob, gzip, json, os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HTML = os.path.join(HERE, "Design System.html")
RAW = os.path.join(ROOT, "raw", "matters")  # raw/matters/ingest_date=*/*.json
YEAR = 2026  # the dashboard subtitle says "2026, year to date"; match it

# Current SF Board of Supervisors: person_id -> district. Reused from the
# district map already baked into the v1 dashboard (the only place we have it;
# raw/ carries person_id/name but no district).
DISTRICTS = {
    "43525": 1, "312930": 2, "312509": 3, "330759": 4, "312510": 5,
    "272292": 6, "60155": 7, "196476": 8, "312511": 9, "160445": 10, "312512": 11,
}
# raw vote_value -> (tally bucket, display label). The dashboard buckets/colors
# by Aye/Nay/Excused/Absent; raw spells nay as "No".
VOTE_MAP = {
    "Aye": ("aye", "Aye"), "No": ("nay", "Nay"),
    "Excused": ("excused", "Excused"), "Absent": ("absent", "Absent"),
}


def _date_key(s):
    try:
        return datetime.strptime(s, "%m/%d/%Y")
    except (ValueError, TypeError):
        return datetime.min  # unparseable/None sort last under reverse=True


def build_reps():
    reps = {}  # person_id -> rep dict
    for f in glob.glob(os.path.join(RAW, "ingest_date=*", "*.json")):
        m = json.load(open(f))
        for a in m.get("actions") or []:
            for v in a.get("votes") or []:
                pid = v["person_id"]
                if pid not in DISTRICTS:
                    continue  # not on the current board
                if _date_key(a.get("date")).year != YEAR:
                    continue  # match the UI's "2026, year to date" framing
                mapped = VOTE_MAP.get(v["vote_value"])
                if not mapped:
                    continue  # unknown vote value -> skip (none in current raw)
                bucket, label = mapped
                rep = reps.setdefault(pid, {
                    "id": pid, "name": v["person_name"], "district": DISTRICTS[pid],
                    "aye": 0, "nay": 0, "excused": 0, "absent": 0, "total": 0,
                    "votes": [],
                })
                rep[bucket] += 1
                rep["total"] += 1
                rep["votes"].append({
                    "file": m.get("file_number"), "name": m.get("name"),
                    "type": m.get("type"), "status": m.get("status"),
                    "date": a.get("date"), "body": a.get("body"),
                    "action": a.get("action"), "result": a.get("result"),
                    "vote": label,
                })
    for rep in reps.values():
        rep["votes"].sort(key=lambda x: _date_key(x["date"]), reverse=True)
    return sorted(reps.values(), key=lambda r: r["total"], reverse=True)


def build_payload(reps):
    dates = [_date_key(v["date"]) for r in reps for v in r["votes"]]
    dates = [d for d in dates if d != datetime.min]
    window = (f"{min(dates):%-m/%-d/%Y} – {max(dates):%-m/%-d/%Y}"
              if dates else "n/a")
    return {"generated": "2026-06-26", "window": window, "reps": reps}


def find_data_uuid(manifest):
    for uuid, e in manifest.items():
        raw = base64.b64decode(e["data"])
        if e.get("compressed"):
            raw = gzip.decompress(raw)
        if b"window.SFD_DATA" in raw:
            return uuid
    raise SystemExit("could not find the SFD_DATA asset in the manifest")


def main():
    lines = open(HTML).read().split("\n")
    # The manifest is a single line; find it rather than hardcoding the index.
    mi = next(i for i, l in enumerate(lines) if l.lstrip().startswith('{"'))
    manifest = json.loads(lines[mi])

    reps = build_reps()
    payload = build_payload(reps)
    js = ("// The SF Digestive - voting data aggregated from raw/matters/* "
          "(SF Legistar, current Board of Supervisors).\n"
          "window.SFD_DATA = " + json.dumps(payload, ensure_ascii=False) + ";")

    uuid = find_data_uuid(manifest)
    gz = gzip.compress(js.encode("utf-8"), mtime=0)  # mtime=0 -> reproducible
    manifest[uuid]["data"] = base64.b64encode(gz).decode("ascii")
    manifest[uuid]["compressed"] = True
    lines[mi] = json.dumps(manifest)
    open(HTML, "w").write("\n".join(lines))

    _verify(payload)
    tot = sum(r["total"] for r in reps)
    print(f"rebuilt SFD_DATA: {len(reps)} reps, {tot} vote rows, window {payload['window']}")
    for r in reps:
        print(f"  D{r['district']:>2} {r['name']:<20} "
              f"aye={r['aye']} nay={r['nay']} exc={r['excused']} abs={r['absent']} = {r['total']}")


def _verify(payload):
    # Re-read from disk and decode the asset we just wrote: it must round-trip.
    lines = open(HTML).read().split("\n")
    mi = next(i for i, l in enumerate(lines) if l.lstrip().startswith('{"'))
    manifest = json.loads(lines[mi])
    txt = gzip.decompress(base64.b64decode(manifest[find_data_uuid(manifest)]["data"])).decode()
    obj = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    assert obj["reps"], "no reps"
    for r in obj["reps"]:
        assert r["total"] == r["aye"] + r["nay"] + r["excused"] + r["absent"], r["id"]
        assert r["total"] == len(r["votes"]), r["id"]
        assert r["district"], r["id"]
    assert len(open(HTML).read().split("\n")) == len(lines), "line count changed"


if __name__ == "__main__":
    main()

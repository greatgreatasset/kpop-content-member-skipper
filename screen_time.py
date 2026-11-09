"""On-screen time per Hearts2Hearts video, from the skip lists' timelines.

    .venv\\Scripts\\python screen_time.py            # -> cache/screen_time.csv + cache/screen_time.html

For every scanned video with Hearts2Hearts in it: how long each member is on
screen, and how much of the video the default skips (Carmen + Ye-on alone) take out. A member
counts as on screen in every timeline frame she is recognized in, fully shown or
cut off by the frame edge; several members can be on screen at once, so the
shares don't add up to 100%. Dates come from the batch scan's channel listing
(cache/batch/), so they are approximate (YouTube shows "3 weeks ago").

The HTML page is self-contained (sortable table, no network) and only contains
titles, video ids and times.
"""
import csv
import datetime as dt
import json
import sys

from common import CACHE, ROOT, SKIPLISTS, Profile

GROUP = "hearts2hearts"
TEMPLATE = ROOT / "screen_time.html"


def dates():
    """videoId -> unix timestamp, from every batch listing."""
    out = {}
    for f in (CACHE / "batch").glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for vid, v in d.get("videos", {}).items():
            if v.get("timestamp"):
                out[vid] = v["timestamp"]
    return out


def video_row(d):
    tl = d.get("timeline")
    if not tl or not any(g["id"] == GROUP for g in d.get("groups", [])):
        return None
    keys = {m["key"]: m["member"] for m in tl["members"] if m["profile"] == GROUP}
    secs = {m: 0.0 for m in keys.values()}
    for s in tl["segments"]:
        n = s["end"] - s["start"]
        for k in (set(s.get("members", ())) | set(s.get("partial", ()))) & keys.keys():
            secs[keys[k]] += n
    dur = d.get("duration") or tl["frames"] / tl["fps"]
    skip = sum(s["end"] - s["start"] for s in d.get("skips", []))
    return {"id": d["videoId"], "title": d.get("title") or d["videoId"], "duration": round(dur),
            **{m: round(v, 1) for m, v in secs.items()},
            "skip": round(skip, 1), "skips": len(d.get("skips", []))}


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    when = dates()
    rows = []
    for f in sorted(SKIPLISTS.glob("*.json")):
        if len(f.stem) != 11:
            continue
        r = video_row(json.loads(f.read_text(encoding="utf-8")))
        if r:
            ts = when.get(r["id"])
            r["date"] = dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d") if ts else None
            rows.append(r)
    rows.sort(key=lambda r: r["date"] or "", reverse=True)

    prof = Profile(GROUP)
    members = list(prof.members)
    for r in rows:
        for m in members:
            r.setdefault(m, 0.0)
    cols = ["id", "date", "title", "duration", *members, "skip", "skips"]
    with open(CACHE / "screen_time.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(cols + [f"{c} share" for c in (*members, "skip")])
        for r in rows:
            w.writerow([r[c] for c in cols] +
                       [f"{r[c] / r['duration']:.3f}" for c in (*members, "skip")])
    data = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "members": [{"id": m, "name": prof.names.get(m, m), "choice": prof.choice(m)} for m in members],
            "videos": rows}
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*DATA*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    (CACHE / "screen_time.html").write_text(html, encoding="utf-8")
    print(f"{len(rows)} videos -> cache/screen_time.csv, cache/screen_time.html")


if __name__ == "__main__":
    main()

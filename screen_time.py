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
import itertools
import json
import sys

from common import CACHE, ROOT, SKIPLISTS, Profile

GROUP = "hearts2hearts"
TEMPLATE = ROOT / "screen_time.html"


def dates():
    """videoId -> YYYY-MM-DD: exact where fetch_dates.py got it, else approximate from the batch listing."""
    out = {}
    for f in (CACHE / "batch").glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for vid, v in d.get("videos", {}).items():
            if v.get("timestamp"):
                out[vid] = dt.datetime.fromtimestamp(v["timestamp"], dt.timezone.utc).strftime("%Y-%m-%d")
    exact = CACHE / "batch" / "upload_dates.json"  # fetch_dates.py: real upload dates
    if exact.exists():
        out.update(json.loads(exact.read_text(encoding="utf-8")))
    return out


def video_row(d):
    tl = d.get("timeline")
    if not tl or not any(g["id"] == GROUP for g in d.get("groups", [])):
        return None
    keys = {m["key"]: m["member"] for m in tl["members"] if m["profile"] == GROUP}
    secs = {m: 0.0 for m in keys.values()}
    combos = {}  # "a+b" / "a+b+c" (sorted ids) -> s with exactly those members on screen
    for s in tl["segments"]:
        n = s["end"] - s["start"]
        on = sorted(keys[k] for k in (set(s.get("members", ())) | set(s.get("partial", ()))) & keys.keys())
        for m in on:
            secs[m] += n
        if len(on) in (2, 3):
            combos["+".join(on)] = combos.get("+".join(on), 0.0) + n
    dur = d.get("duration") or tl["frames"] / tl["fps"]
    skip = sum(s["end"] - s["start"] for s in d.get("skips", []))
    return {"id": d["videoId"], "title": d.get("title") or d["videoId"], "duration": round(dur),
            **{m: round(v, 1) for m, v in secs.items()},
            "skip": round(skip, 1), "skips": len(d.get("skips", [])),
            "combos": {k: round(v, 1) for k, v in combos.items()}}


def expected(obs, members, size):
    """Time each combination of `size` members would get if who shares a shot were
    random, given how much each member is in shots of that size at all: weights
    w with expected(S) = prod(w[m] for m in S), fitted so every member's total
    matches hers (iterative proportional fitting)."""
    sets = list(itertools.combinations(members, size))
    tot = {m: sum(v for k, v in obs.items() if m in k) for m in members}
    w = {m: max(tot[m], 1e-9) ** (1 / size) for m in members}

    def prod(S):
        out = 1.0
        for m in S:
            out *= w[m]
        return out
    for _ in range(500):
        for m in members:
            cur = sum(prod(S) for S in sets if m in S)
            if cur > 0 and tot[m] > 0:
                w[m] *= (tot[m] / cur) ** (1 / size)
    return {S: prod(S) for S in sets}


def together(rows, members, size, min_video=5.0):
    """Per combination of `size` members: s on screen with exactly each other, the
    random-pairing expectation, and in how many videos they get >= min_video s."""
    obs = {S: 0.0 for S in itertools.combinations(members, size)}
    vids = dict.fromkeys(obs, 0)
    for r in rows:
        for k, v in r["combos"].items():
            S = tuple(sorted(k.split("+"), key=members.index))
            if len(S) == size and S in obs:
                obs[S] += v
                vids[S] += v >= min_video
    exp = expected(obs, members, size)
    return sorted(({"m": list(S), "secs": round(obs[S], 1), "expected": round(exp[S], 1), "videos": vids[S]}
                   for S in obs), key=lambda x: -x["secs"])


def by_month(rows, members):
    """Per upload month: every pair's duo time and its random-pairing expectation
    within that month's videos. [{month, videos, pairs: {"a+b": [secs, expected]}}]"""
    months = {}
    for r in rows:
        if r["date"]:
            months.setdefault(r["date"][:7], []).append(r)
    out = []
    for mo in sorted(months):
        t = together(months[mo], members, 2)
        out.append({"month": mo, "videos": len(months[mo]),
                    "pairs": {"+".join(x["m"]): [x["secs"], x["expected"]] for x in t}})
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    when = dates()
    rows = []
    for f in sorted(SKIPLISTS.glob("*.json")):
        if len(f.stem) != 11:
            continue
        r = video_row(json.loads(f.read_text(encoding="utf-8")))
        if r:
            r["date"] = when.get(r["id"])
            rows.append(r)
    rows.sort(key=lambda r: r["date"] or "", reverse=True)

    prof = Profile(GROUP)
    members = list(prof.members)
    for r in rows:
        for m in members:
            r.setdefault(m, 0.0)
    for r in rows:
        top = max(((v, k) for k, v in r["combos"].items() if "+" in k and k.count("+") == 1), default=None)
        r["pair"], r["pairSecs"] = (top[1], top[0]) if top else (None, 0.0)
    pairs, trios = together(rows, members, 2), together(rows, members, 3)
    cols = ["id", "date", "title", "duration", *members, "skip", "skips"]
    with open(CACHE / "screen_time.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(cols + [f"{c} share" for c in (*members, "skip")])
        for r in rows:
            w.writerow([r[c] for c in cols] +
                       [f"{r[c] / r['duration']:.3f}" for c in (*members, "skip")])
    data = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "members": [{"id": m, "name": prof.names.get(m, m), "choice": prof.choice(m)} for m in members],
            "pairs": pairs, "trios": trios, "months": by_month(rows, members),
            "videos": [{k: v for k, v in r.items() if k != "combos"} for r in rows]}
    with open(CACHE / "screen_time_together.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["members", "seconds together (only them)", "expected if random", "ratio", "videos (5 s+)"])
        for x in pairs + trios:
            w.writerow([" + ".join(prof.names.get(m, m) for m in x["m"]), x["secs"], x["expected"],
                        f"{x['secs'] / x['expected']:.2f}" if x["expected"] else "", x["videos"]])
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*DATA*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    (CACHE / "screen_time.html").write_text(html, encoding="utf-8")
    print(f"{len(rows)} videos -> cache/screen_time.csv, cache/screen_time.html")


if __name__ == "__main__":
    main()

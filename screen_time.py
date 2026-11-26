"""Screen time per member for one group's videos, from the skip lists' timelines.

    .venv\\Scripts\\python screen_time.py [--profile hearts2hearts] [--channel UC...]
        -> cache/screen_time_<profile>.html (sortable page), .csv, _together.csv

--channel limits it to the videos batch_scan.py listed for that channel
(cache/batch/<channel>.json); without it, every scanned video the group is in.

What is counted (the same rules for every group, so groups can be compared):

* On screen: a member is on screen in every timeline frame she is recognized in,
  fully shown or cut off by the frame edge. Shares are out of "members shown", the
  time at least one member of the group is recognized (shots with no member, wide
  shots with only tiny faces, and other people don't count). Several members can
  be on screen at once, so shares don't add up to 100%.
* Main focus: the member who is the main subject of the frame (largest face, at
  least 1.5x the next one, near the middle; the scanner's "focus"). Shares are out
  of the time any member is the focus. Closer to "who is featured" than on screen.
* Video kinds. Only "group" videos count by default; the page can add the others:
    whole        GROUP_SHOT+ members on screen for >= WHOLE_GROUP of the member time
                 (cheering guides, MV commentaries: nobody has to share the screen)
    unrecognized faces seen but nobody recognized for >= UNRECOGNIZED of the face
                 time (dance practices, recording booths: measures little)
    subset       an active member (one who appears in >= ACTIVE_SHARE of the videos)
                 is absent (solo vlogs, duo episodes, units: assigned, not split)
    group        everything else
* Pairs / trios: time exactly those members are on screen, against what random
  pairing would give (iterative proportional fitting on each member's total in
  shots of that size), as (together + K) / (expected + K), K = SMALL_SAMPLE_K s.
  Group videos only.
* Miss rate: share of a member's faces the recognizer probably missed: unknown
  faces (large enough, below the match threshold) whose best match is her and
  within MISS_MARGIN of the threshold, against those plus her recognized faces.
  An estimate, shown so the margin is visible. Cached in cache/screen_time_miss/.

Dates: exact upload dates from fetch_dates.py where present, else the approximate
ones from the batch listing. The page only contains titles, video ids and times.
"""
import argparse
import contextlib
import csv
import datetime as dt
import io
import itertools
import json
import math
import sys

from common import CACHE, ROOT, SKIPLISTS, Profile

TEMPLATE = ROOT / "screen_time.html"
GROUP_SHOT_FRACTION = 0.6   # GROUP_SHOT = max(3, ceil(0.6 x active members)): 5 for 8 members, 3 for 5
WHOLE_GROUP = 0.5           # H2H: the 8 whole-group videos are at 98-100%, all others <= 14%
UNRECOGNIZED = 0.5          # unknown-face time / (member time + unknown-face time)
ACTIVE_SHARE = 0.25         # members present in fewer videos than this (inactive, left, joined late) don't make a video "subset"
PRESENT_SECS = 30.0         # present in a video: on screen >= 30 s (or >= 5% of its member time)
SMALL_SAMPLE_K = 60.0       # s added to both sides of "vs random"; the page uses the same K
MISS_MARGIN = 0.1           # "near miss": score >= threshold - 0.1
KINDS = ("group", "subset", "whole", "unrecognized")


def dates():
    """videoId -> YYYY-MM-DD: exact where fetch_dates.py got it, else approximate from the batch listing."""
    out = {}
    for f in (CACHE / "batch").glob("UC*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for vid, v in d.get("videos", {}).items():
            if v.get("timestamp"):
                out[vid] = dt.datetime.fromtimestamp(v["timestamp"], dt.timezone.utc).strftime("%Y-%m-%d")
    exact = CACHE / "batch" / "upload_dates.json"
    if exact.exists():
        out.update(json.loads(exact.read_text(encoding="utf-8")))
    return out


def video_row(d, pid):
    """Raw per-video numbers for one group (kind is decided later, over all videos)."""
    tl = d.get("timeline")
    if not tl or not any(g["id"] == pid for g in d.get("groups", [])):
        return None
    keys = {m["key"]: m["member"] for m in tl["members"] if m["profile"] == pid}
    on = {m: 0.0 for m in keys.values()}
    focus = {m: 0.0 for m in keys.values()}
    combos = {}  # "a+b" / "a+b+c" (sorted ids) -> s with exactly those members on screen
    sizes = {}   # number of members on screen -> s
    seen = unknown = focus_t = 0.0
    for s in tl["segments"]:
        n = s["end"] - s["start"]
        ms = sorted(keys[k] for k in (set(s.get("members", ())) | set(s.get("partial", ()))) & keys.keys())
        for m in ms:
            on[m] += n
        if ms:
            seen += n
            sizes[len(ms)] = sizes.get(len(ms), 0.0) + n
        elif s.get("state") == "unknown":
            unknown += n
        if s.get("focus") in keys:
            focus[keys[s["focus"]]] += n
            focus_t += n
        if len(ms) in (2, 3):
            combos["+".join(ms)] = combos.get("+".join(ms), 0.0) + n
    dur = d.get("duration") or tl["frames"] / tl["fps"]
    skip = sum(s["end"] - s["start"] for s in d.get("skips", []))
    top = max(((v, k) for k, v in combos.items() if k.count("+") == 1), default=None)
    return {"id": d["videoId"], "title": d.get("title") or d["videoId"], "duration": round(dur),
            "memberTime": round(seen, 1), "focusTime": round(focus_t, 1), "unknownTime": round(unknown, 1),
            **{m: round(v, 1) for m, v in on.items()},
            **{f"f:{m}": round(v, 1) for m, v in focus.items()},
            "skip": round(skip, 1), "skips": len(d.get("skips", [])),
            "pair": top[1] if top else None, "pairSecs": top[0] if top else 0.0,
            "_combos": combos, "_sizes": sizes}


def classify_kinds(rows, members):
    """Sets each row's "kind" (and the numbers behind it); returns the cutoffs used."""
    def present(r, m):
        return r.get(m, 0.0) >= min(PRESENT_SECS, 0.05 * r["memberTime"]) and r.get(m, 0.0) > 0
    n = max(1, len(rows))
    active = [m for m in members if sum(present(r, m) for r in rows) >= ACTIVE_SHARE * n]
    group_shot = max(3, math.ceil(GROUP_SHOT_FRACTION * len(active)))
    for r in rows:
        crowd = sum(v for k, v in r["_sizes"].items() if k >= group_shot)
        r["groupShare"] = round(crowd / r["memberTime"], 3) if r["memberTime"] else 0.0
        faces = r["memberTime"] + r["unknownTime"]
        r["unknownShare"] = round(r["unknownTime"] / faces, 3) if faces else 1.0
        r["present"] = [m for m in members if present(r, m)]
        if r["memberTime"] and r["groupShare"] >= WHOLE_GROUP:
            r["kind"] = "whole"
        elif r["unknownShare"] >= UNRECOGNIZED:
            r["kind"] = "unrecognized"
        elif any(m not in r["present"] for m in active):
            r["kind"] = "subset"
        else:
            r["kind"] = "group"
    return {"active": active, "groupShot": group_shot, "wholeGroup": WHOLE_GROUP,
            "unrecognized": UNRECOGNIZED, "activeShare": ACTIVE_SHARE, "presentSecs": PRESENT_SECS}


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
        for k, v in r["_combos"].items():
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
    return [{"month": mo, "videos": len(months[mo]),
             "pairs": {"+".join(x["m"]): [x["secs"], x["expected"]] for x in together(months[mo], members, 2)}}
            for mo in sorted(months)]


def miss_counts(pid, vids):
    """vid -> {member: [recognized faces, near-miss unknown faces]}, cached per scanner version."""
    import numpy as np
    import scan
    path = CACHE / "screen_time_miss" / f"{pid}.json"
    path.parent.mkdir(exist_ok=True)
    key = f"{scan.SCANNER_VERSION}|{MISS_MARGIN}"
    cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if cache.get("key") != key:
        cache = {"key": key, "videos": {}}
    todo = [v for v in vids if v not in cache["videos"] and (CACHE / f"{v}.faces.npz").exists()]
    if todo:
        print(f"Estimating miss rates for {len(todo)} videos (cached afterwards)...", flush=True)
        with contextlib.redirect_stdout(io.StringIO()):
            gal = scan.load_gallery(None)  # all groups, like the scan itself
        p = dict(scan.DEFAULTS)
        for i, vid in enumerate(todo, 1):
            z = np.load(CACHE / f"{vid}.faces.npz")
            rows, emb = z["rows"], z["emb"]
            out = {}
            if len(rows):
                ex = {}
                with contextlib.redirect_stdout(io.StringIO()):
                    lab, score, (thr, _), _ = scan.classify(rows, emb, gal, p, ex)
                best = ex["best"]
                for c in range(len(gal.keys)):
                    prof, member = gal.keys[c].split(":", 1)
                    if prof != pid:
                        continue
                    rec = int((lab == c).sum())
                    near = int(((lab == -1) & (best == c) & (score >= thr - MISS_MARGIN)).sum())
                    if rec or near:
                        out[member] = [rec, near]
            cache["videos"][vid] = out
            if i % 25 == 0:
                print(f"  {i}/{len(todo)}", flush=True)
                path.write_text(json.dumps(cache), encoding="utf-8")
        path.write_text(json.dumps(cache), encoding="utf-8")
    return cache["videos"]


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="hearts2hearts")
    ap.add_argument("--channel", help="UC... id: only the videos batch_scan.py listed for this channel")
    ap.add_argument("--no-miss", action="store_true", help="skip the miss-rate estimate (slow the first time)")
    a = ap.parse_args()
    pid = a.profile
    prof = Profile(pid)
    members = list(prof.members)
    when = dates()
    files = sorted(f for f in SKIPLISTS.glob("*.json") if len(f.stem) == 11)
    if a.channel:
        st = json.loads((CACHE / "batch" / f"{a.channel}.json").read_text(encoding="utf-8"))
        files = [f for f in files if f.stem in set(st["order"])]
    rows = []
    for f in files:
        r = video_row(json.loads(f.read_text(encoding="utf-8")), pid)
        if r:
            for m in members:
                r.setdefault(m, 0.0)
                r.setdefault(f"f:{m}", 0.0)
            r["date"] = when.get(r["id"])
            rows.append(r)
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    cut = classify_kinds(rows, members)
    group = [r for r in rows if r["kind"] == "group"]

    miss = {}
    if not a.no_miss:
        counts = miss_counts(pid, [r["id"] for r in group])
        for m in members:
            rec = sum(counts.get(r["id"], {}).get(m, [0, 0])[0] for r in group)
            near = sum(counts.get(r["id"], {}).get(m, [0, 0])[1] for r in group)
            miss[m] = round(near / (rec + near), 4) if rec + near else None

    # diagnostics: check each cutoff falls in a clear gap for this group
    kinds = {k: sum(r["kind"] == k for r in rows) for k in KINDS}
    print(f"{prof.group}: {len(rows)} videos: " + ", ".join(f"{v} {k}" for k, v in kinds.items()))
    print(f"  active members: {', '.join(cut['active'])}; group shot = {cut['groupShot']}+ members")
    gs = sorted((r["groupShare"] for r in rows), reverse=True)
    print(f"  whole-group share, highest: {', '.join(f'{x:.2f}' for x in gs[:kinds['whole'] + 4])} (cutoff {WHOLE_GROUP})")
    us = sorted((r["unknownShare"] for r in rows), reverse=True)
    print(f"  unrecognized share, around the cutoff: "
          f"{', '.join(f'{x:.2f}' for x in us[max(0, kinds['unrecognized'] - 3):kinds['unrecognized'] + 4])} (cutoff {UNRECOGNIZED})")
    if miss:
        print("  est. miss rate: " + ", ".join(f"{m} {100 * v:.1f}%" for m, v in miss.items() if v is not None))

    sub = [r for r in rows if r["kind"] == "subset"]
    subset_by_member = [{"m": m, "videos": sum(m in r["present"] for r in sub),
                         "solo": sum(r["present"] == [m] for r in sub),
                         "secs": round(sum(r[m] for r in sub), 1)} for m in members]
    pairs = together(group, members, 2)
    trios = together(group, members, 3) if len(members) >= 4 else []
    stem = CACHE / f"screen_time_{pid}"
    cols = ["id", "date", "title", "kind", "duration", "memberTime", "focusTime", *members,
            *(f"f:{m}" for m in members), "skip", "skips", "groupShare", "unknownShare"]
    with open(f"{stem}.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow([c.replace("f:", "focus ") for c in cols] + [f"{m} share of member time" for m in members]
                   + [f"{m} share of focus time" for m in members] + ["skip share of video"])
        for r in rows:
            w.writerow([r[c] for c in cols]
                       + [f"{r[m] / r['memberTime']:.3f}" if r["memberTime"] else "" for m in members]
                       + [f"{r['f:' + m] / r['focusTime']:.3f}" if r["focusTime"] else "" for m in members]
                       + [f"{r['skip'] / r['duration']:.3f}"])
    with open(f"{stem}_together.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["members", "seconds together (only them)", "expected if random",
                    "ratio (small-sample adjusted)", "videos (5 s+)"])
        for x in pairs + trios:
            w.writerow([" + ".join(prof.names.get(m, m) for m in x["m"]), x["secs"], x["expected"],
                        f"{(x['secs'] + SMALL_SAMPLE_K) / (x['expected'] + SMALL_SAMPLE_K):.2f}", x["videos"]])
    data = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "group": prof.group, "profile": pid, "k": SMALL_SAMPLE_K, "cutoffs": cut, "miss": miss,
            "members": [{"id": m, "name": prof.names.get(m, m), "choice": prof.choice(m)} for m in members],
            "pairs": pairs, "trios": trios, "months": by_month(group, members), "subset": subset_by_member,
            "videos": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]}
    html = TEMPLATE.read_text(encoding="utf-8").replace("__GROUP__", prof.group).replace(
        "/*DATA*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    (stem.parent / f"{stem.name}.html").write_text(html, encoding="utf-8")
    print(f"-> {stem}.html, {stem.name}.csv, {stem.name}_together.csv")


if __name__ == "__main__":
    main()

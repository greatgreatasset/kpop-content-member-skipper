"""Compare a scan against hand labels, second by second.

    python eval_test.py [labels json] [--fps 3 ...any scan.py param...] [--detail]

Uses the cached detections from cache/<id>.faces.npz (run scan.py first), so
classification / smoothing parameters can be swept quickly.
"""
import argparse
import json
from pathlib import Path

import numpy as np

import scan
from common import CACHE, ROOT


def per_second(segs, n):
    """Second s counts as covered if >= 50% of [s, s+1) is inside a segment."""
    cov = np.zeros(n)
    for s in segs:
        a, b = s["start"], s["end"]
        for sec in range(max(0, int(a)), min(n, int(np.ceil(b)))):
            cov[sec] += max(0.0, min(b, sec + 1) - max(a, sec))
    return cov >= 0.5


def intervals_to_mask(iv, n):
    m = np.zeros(n, bool)
    for a, b in iv:
        m[a:b] = True
    return m


def runs(mask):
    out, i = [], 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def mmss(s):
    return f"{int(s // 60)}:{int(s % 60):02d}"


def evaluate(labels, p, detail=False, quiet=False, profile=None):
    vid = labels["videoId"]
    z = np.load(CACHE / f"{vid}.faces.npz")
    times, rows, emb = z["times"], z["rows"], z["emb"]
    kps = z["kps"] if "kps" in z else None
    fsize = tuple(int(x) for x in z["frame_size"]) if "frame_size" in z else None
    gal = scan.load_gallery(profile)  # default: every group, as scan.py does
    MEMBERS = gal.members
    skips, highlights, dbg, diag = scan.build_segments(times, rows, emb, gal, p, kps, fsize)
    # the hand labels have approximate ranges for the (first) highlighted member
    hl = highlights[0] if highlights else {"name": "-", "ranges": []}
    ians = hl["ranges"]
    n = labels["labeledUntil"]
    truth = intervals_to_mask(labels["skip"], n)
    pred = per_second(skips, n)
    # seconds within 1 s of a label boundary are ambiguous (labels are to the second)
    edge = np.zeros(n, bool)
    for a, b in labels["skip"]:
        for e in (a, b):
            edge[max(0, e - 1):min(n, e + 1)] = True
    missed = truth & ~pred
    wrong = pred & ~truth
    r = dict(
        skip_truth=int(truth.sum()), skip_pred=int(pred.sum()),
        missed=int(missed.sum()), wrong=int(wrong.sum()),
        missed_core=int((missed & ~edge).sum()), wrong_core=int((wrong & ~edge).sum()),
    )
    if quiet:
        return r, skips, ians, dbg, diag
    print(f"gallery: {', '.join(gal.ids)}; groups found: "
          f"{', '.join(g['name'] for g in dbg['groups'])}; confident faces per group: {diag['groups']}")
    print(f"thr={diag['thr']:.3f} ({diag['thr_source']})  faces={diag['faces']} small={diag['faces_small']} "
          f"unknown={diag['faces_unknown']}")
    print("faces by member:", diag["faces_by_member"])
    print(f"frames with >=1 unknown face: {diag['frames_with_unknown']}/{len(times)}; "
          f"SKIP frames that also had unknown faces: {diag['frames_skip_with_unknown']}; "
          f"frames with no recognized member: {diag['frames_no_member']}")
    print(f"\n0:00-{mmss(n)}: truth skip {r['skip_truth']} s, predicted {r['skip_pred']} s")
    print(f"  missed skip seconds : {r['missed']}  (excluding +/-1 s of label edges: {r['missed_core']})")
    print(f"  wrongly skipped secs: {r['wrong']}  (excluding +/-1 s of label edges: {r['wrong_core']})")
    print("  missed runs :", ", ".join(f"{mmss(a)}-{mmss(b)}" for a, b in runs(missed)) or "-")
    print("  wrong runs  :", ", ".join(f"{mmss(a)}-{mmss(b)}" for a, b in runs(wrong)) or "-")
    print("\ndetected skips:", ", ".join(f"{mmss(s['start'])}-{mmss(s['end'])}" for s in skips))
    print(f"detected {hl['name']:<5}:", ", ".join(f"{mmss(s['start'])}-{mmss(s['end'])}" for s in ians))
    if "ian_approx" in labels:
        it = intervals_to_mask(labels["ian_approx"], n)
        ip = per_second(ians, n)
        print(f"{hl['name']} (approx labels): truth {it.sum()} s, detected {ip.sum()} s, "
              f"overlap {(it & ip).sum()} s, detected outside labels {(ip & ~it).sum()} s")
    if detail:
        dt = 1 / p["fps"]
        print("\nper-second detail around errors (members per frame, ?=unknown, .=tiny/background, ~=partly shown, does not block):")
        err = missed | wrong
        show = np.zeros(n, bool)
        for a, b in runs(err):
            show[max(0, a - 1):min(n, b + 1)] = True
        fi_rows = rows[:, 0].astype(int)
        for sec in np.where(show)[0]:
            fr = np.where((times >= sec) & (times < sec + 1))[0]
            parts = []
            for f in fr:
                idx = np.where(fi_rows == f)[0]
                labs = []
                for i in idx:
                    l = dbg["lab"][i]
                    part = "~" if dbg["eff"][i] == scan.PARTIAL else ""
                    labs.append("." if l == -2 else ("?" + f"{dbg['score'][i]:.2f}" if l == -1
                                else f"{part}{MEMBERS[l]}{dbg['score'][i]:.2f}"))
                parts.append("[" + " ".join(labs) + "]")
            tag = "MISSED" if missed[sec] else ("WRONG" if wrong[sec] else "")
            print(f"  {mmss(sec)} truth={'S' if truth[sec] else 'k'} pred={'S' if pred[sec] else 'k'} "
                  f"{tag:6} " + " ".join(parts))
    return r, skips, ians, dbg, diag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", nargs="?", default=str(ROOT / "tests" / "labels_oVMQTIUI_2c.json"))
    for k, v in scan.DEFAULTS.items():
        ap.add_argument(f"--{k.replace('_', '-')}", type=type(v) if v is not None else float, default=v)
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--profile", help="only these groups (comma-separated), e.g. to compare with one group")
    a = ap.parse_args()
    p = {k: getattr(a, k) for k in scan.DEFAULTS}
    labels = json.loads(Path(a.labels).read_text(encoding="utf-8"))
    _, skips, ians, _, _ = evaluate(labels, p, a.detail, profile=a.profile)
    n = labels["labeledUntil"]
    print(f"\nafter {mmss(n)} (unlabeled):")
    print("  skips:", ", ".join(f"{mmss(s['start'])}-{mmss(s['end'])}" for s in skips if s["end"] > n))
    print("  highlight:", ", ".join(f"{mmss(s['start'])}-{mmss(s['end'])}" for s in ians if s["end"] > n))


if __name__ == "__main__":
    main()

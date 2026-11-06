"""Rebuild skip lists from the cached face detections, without downloading
anything (e.g. after adding a group, changing a profile or the rules).

    python rescan_cached.py              every skiplists/<videoId>.json with cache/<videoId>.faces.npz
    python rescan_cached.py <videoId> ...

Keeps each list's title, duration and onlyProfiles; prints what changed. Run
sync_skiplists.py afterwards and commit the lists.

The face boxes for blurring (timeline["faces"]) come from cache/<videoId>.blur.npz;
a video without one gets the blur pass first (GPU, the frames between the
timeline frames of its cached video file, nothing is downloaded). If the video
file is gone too, the list keeps the face boxes it had. --no-blur-pass skips
the GPU work (lists without cached blur data keep their old boxes).
"""
import json
import sys
import time

import scan
from common import SKIPLISTS


def summary(d):
    secs = round(sum(s["end"] - s["start"] for s in d.get("skips", [])))
    hl = ", ".join(f"{len(h['ranges'])} {h['name']}" for h in d.get("highlights", []))
    groups = "+".join(g["name"] for g in d.get("groups", [])) or "no group"
    return f"{groups}: {len(d.get('skips', []))} skips ({secs} s)" + (f", {hl}" if hl else "")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_pass = "--no-blur-pass" in sys.argv
    ids = args or sorted(f.stem for f in SKIPLISTS.glob("*.json") if len(f.stem) == 11)
    p = dict(scan.DEFAULTS)
    galleries = {}
    for vid in ids:
        f = SKIPLISTS / f"{vid}.json"
        old = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        cached = scan.load_cached(vid)
        if cached is None:
            print(f"{vid}: no cached detections, skipped (scan it with scan.py)")
            continue
        only = old.get("onlyProfiles")
        key = ",".join(only) if only else ""
        if key not in galleries:
            galleries[key] = scan.load_gallery(only)
        gal = galleries[key]
        times, rows, emb, kps, frame_size = cached
        bd = scan.load_blur(vid, p)
        note = ""
        if bd is None and not no_pass:
            t0 = time.perf_counter()
            bd = scan.blur_pass(vid, p)
            if bd is not None:
                note = f", blur pass {time.perf_counter() - t0:.0f} s"
        skips, highlights, dbg, _ = scan.build_segments(times, rows, emb, gal, p, kps, frame_size, bd)
        out = scan.make_list(vid, old.get("title"), old.get("duration"), skips, highlights, dbg, only)
        if bd is None and "faces" in old.get("timeline", {}):
            out["timeline"]["faces"] = old["timeline"]["faces"]  # no video file: keep the old boxes
            note = ", kept the old face boxes (no video file)"
        elif bd is None:
            note = ", no face boxes (no video file)"
        same = out["skips"] == old.get("skips") and out["highlights"] == old.get("highlights")
        scan.save_list(out)
        print(f"{vid}: {summary(out)}" + ("  (skips/highlights unchanged)" if same else f"  (was {summary(old)})")
              + note, flush=True)


if __name__ == "__main__":
    main()

"""Dump annotated frames (face boxes + top-2 matches) for given timestamps.

    python debug_frames.py <videoId> 1:46 2:18 ... [--profile illit]  -> cache/debug/<id>_<t>.jpg

Top-2 matches are among the groups found in the video (as scan.py labels them);
red = a skip-member, green = anyone else. Also prints what the frame was labelled.
"""
import subprocess
import sys

import cv2
import numpy as np

import scan
from common import CACHE


def parse_t(s):
    if ":" in s:
        m, sec = s.split(":")
        return int(m) * 60 + float(sec)
    return float(s)


def main():
    args = sys.argv[1:]
    pid = None
    if "--profile" in args:
        i = args.index("--profile")
        pid = args[i + 1]
        del args[i:i + 2]
    gal = scan.load_gallery(pid)
    vid, ts = args[0], [parse_t(t) for t in args[1:]]
    z = np.load(CACHE / f"{vid}.faces.npz")
    times, rows, emb = z["times"], z["rows"], z["emb"]
    p = dict(scan.DEFAULTS)
    skips, _, dbg, _ = scan.build_segments(times, rows, emb, gal, p, z["kps"],
                                           tuple(int(x) for x in z["frame_size"]))
    groups = [g["id"] for g in dbg["groups"]]
    S = scan.class_scores(emb, gal, "knn", 5)
    S[:, ~np.isin([gal.profiles[i].id for i in gal.cls_prof], groups)] = -1
    skip_cls = set(gal.classes("skip"))
    print("groups found:", ", ".join(g["name"] for g in dbg["groups"]))
    video = next(p for p in CACHE.glob(f"{vid}.*") if p.suffix in (".mp4", ".webm", ".mkv"))
    out = CACHE / "debug"
    out.mkdir(exist_ok=True)
    for t in ts:
        fi = int(np.argmin(np.abs(times - t)))
        tt = float(times[fi])
        img_bytes = subprocess.check_output(
            ["ffmpeg", "-v", "error", "-ss", f"{tt:.3f}", "-i", str(video), "-frames:v", "1",
             "-vf", "scale=1280:720", "-f", "image2pipe", "-vcodec", "png", "-"])
        img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), 1)
        in_skip = any(s["start"] <= tt < s["end"] for s in skips)
        print(f"t={tt:.2f}  frame state={'SKIP' if dbg['state'][fi] == scan.SKIP else 'keep/none'}"
              f"  in a skip range: {in_skip}")
        for i in np.where(rows[:, 0] == fi)[0]:
            x1, y1, x2, y2 = rows[i, 1:5].astype(int)
            o = np.argsort(-S[i])
            txt = f"{gal.name(o[0])} {S[i,o[0]]:.2f} / {gal.name(o[1])} {S[i,o[1]]:.2f}"
            l = dbg["lab"][i]
            final = {-1: "unknown", -2: "small/background"}.get(int(l)) or gal.name(l)
            if dbg["eff"][i] == scan.PARTIAL:
                final += " (partly shown)"
            print(f"  box {x1},{y1},{x2},{y2} ({x2-x1}px) det={rows[i,5]:.2f}  {txt}  -> {final}")
            col = (0, 0, 255) if o[0] in skip_cls else (0, 255, 0)
            cv2.rectangle(img, (x1, y1), (x2, y2), col, 2)
            cv2.putText(img, txt, (x1, max(15, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 2)
        cv2.imwrite(str(out / f"{vid}_{tt:07.2f}.jpg"), img)


if __name__ == "__main__":
    main()

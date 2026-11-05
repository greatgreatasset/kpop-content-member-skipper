"""Dump annotated frames (face boxes + top-2 matches) for given timestamps.

    python debug_frames.py <videoId> 1:46 2:18 ... [--profile hearts2hearts]  -> cache/debug/<id>_<t>.jpg
"""
import subprocess
import sys

import cv2
import numpy as np

import scan
from common import CACHE, DEFAULT_PROFILE, Profile


def parse_t(s):
    if ":" in s:
        m, sec = s.split(":")
        return int(m) * 60 + float(sec)
    return float(s)


def main():
    args = sys.argv[1:]
    pid = DEFAULT_PROFILE
    if "--profile" in args:
        i = args.index("--profile")
        pid = args[i + 1]
        del args[i:i + 2]
    prof = Profile(pid)
    MEMBERS = prof.members
    vid, ts = args[0], [parse_t(t) for t in args[1:]]
    z = np.load(CACHE / f"{vid}.faces.npz")
    times, rows, emb = z["times"], z["rows"], z["emb"]
    refs = scan.load_refs(prof)
    S = scan.class_scores(emb, refs, "knn", 5)
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
        print(f"t={tt:.2f}")
        for i in np.where(rows[:, 0] == fi)[0]:
            x1, y1, x2, y2 = rows[i, 1:5].astype(int)
            o = np.argsort(-S[i])
            txt = f"{MEMBERS[o[0]]} {S[i,o[0]]:.2f} / {MEMBERS[o[1]]} {S[i,o[1]]:.2f}"
            print(f"  box {x1},{y1},{x2},{y2} ({x2-x1}px) det={rows[i,5]:.2f}  {txt}")
            col = (0, 0, 255) if MEMBERS[o[0]] in prof.skip else (0, 255, 0)
            cv2.rectangle(img, (x1, y1), (x2, y2), col, 2)
            cv2.putText(img, txt, (x1, max(15, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 2)
        cv2.imwrite(str(out / f"{vid}_{tt:07.2f}.jpg"), img)


if __name__ == "__main__":
    main()

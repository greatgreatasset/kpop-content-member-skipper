"""Face boxes for blurring (blur.py): encoding, tracks split at scene cuts and
bridged over short gaps, labels filled along tracks, ffmpeg fps-filter frame
times, playback (interpolation, hold, cuts, fades); then extension/blur.js
against blur.py at random times for every skip list with face boxes.

    python tests/blur_test.py [--samples 3000]
"""
import argparse
import json
import random
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import blur  # noqa: E402

fails = 0


def check(name, ok, info=""):
    global fails
    fails += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {info}")


P = {**blur.DEFAULTS, "track_iou": 0.3, "track_sim": 0.3}


def faces(samples, cuts=(), size=(1280, 720)):
    """samples: [(t, member class, (x1, y1, x2, y2), embedding seed)] -> faces block."""
    t = np.array([s[0] for s in samples], float)
    xyxy = np.array([s[2] for s in samples], np.float32)
    rng = {}
    emb = []
    for s in samples:  # one random unit vector per seed: same seed = same person
        if s[3] not in rng:
            v = np.random.default_rng(s[3]).normal(size=512)
            rng[s[3]] = (v / np.linalg.norm(v)).astype(np.float32)
        emb.append(rng[s[3]])
    own = np.array([s[1] for s in samples], int)
    return blur.faces_block(t, xyxy, np.array(emb), own, ["g:a", "g:b"], 1 / 3, size, list(cuts), P)


def unit():
    # encoding
    b = [[0.0, 0.25, 0.5, 1.0], [0.1234, 0.9876, 0.001, 0.333]]
    f, d = blur.dec_track(blur.enc_track([(10, b[0]), (13, b[1])]))
    check("encoding round trip (12 bit)", f == [10, 13] and np.allclose(d, b, atol=1 / 4095))

    # a face moving right, sampled at 3 fps, with a cut at 1.0 s
    box = lambda x: (x, 100, x + 100, 220)  # noqa: E731
    s = [(k / 3, 0, box(100 + 10 * k), 1) for k in range(6)]
    fb = faces(s, cuts=[1.0])
    tr = fb["tracks"]["g:a"]
    check("track split at the scene cut", len(tr) == 2, f"{len(tr)} tracks")
    fr = [blur.dec_track(x)[0] for x in tr]
    check("... with the samples on each side", fr == [[0, 20, 40], [60, 80, 100]], str(fr))
    fb = faces(s)
    check("no cut: one track", len(fb["tracks"]["g:a"]) == 1)

    # a gap of 2 samples (0.67 s) is bridged, a longer one is not
    s = [(k / 3, 0, box(100), 1) for k in (0, 1, 3, 4)]
    check("gap of one missing sample bridged", len(faces(s)["tracks"]["g:a"]) == 1)
    s = [(k / 3, 0, box(100), 1) for k in (0, 1, 5, 6)]
    check("gap of 1.33 s not bridged", len(faces(s)["tracks"]["g:a"]) == 2)
    s = [(0, 0, box(100), 1), (1 / 3, 0, box(100), 1), (2 / 3, 0, box(700), 1), (1, 0, box(700), 1)]
    check("far jump: not the same track", len(faces(s)["tracks"]["g:a"]) == 2)

    # a face unknown on its own takes its track's label; a confident other label is kept
    s = [(0, 0, box(100), 1), (1 / 3, -1, box(105), 1), (2 / 3, 0, box(110), 1),
         (0, 1, box(600), 2), (1 / 3, 1, box(605), 2)]
    fb = faces(s)
    fr = [blur.dec_track(x)[0] for x in fb["tracks"]["g:a"]]
    check("unknown face filled from its track", fr == [[0, 20, 40]], str(fr))
    check("other member separate", len(fb["tracks"]["g:b"]) == 1)
    s = [(0, 0, box(100), 1), (1 / 3, 1, box(105), 1), (2 / 3, 0, box(110), 1)]
    fb = faces(s)
    check("own confident label never overridden", "g:b" in fb["tracks"] and len(fb["tracks"]["g:b"]) == 1)
    s = [(0, 0, (100, 100, 108, 108), 1), (1 / 3, 0, (100, 100, 108, 108), 1)]
    check("tiny faces get no box", faces(s)["tracks"] == {})

    # playback
    s = [(1.0, 0, box(100), 1), (4 / 3, 0, box(150), 1), (5 / 3, 0, box(200), 1)]
    fb = faces(s, cuts=[2.0])
    dec = blur.decode_tracks(fb)["g:a"][0]
    ct = blur.cut_times(fb)
    r = blur.box_at(dec, 60, ct, (1 + 4 / 3) / 2, fb["hold"])
    check("interpolated halfway", r and abs(r[0][0] - 125 / 1280) < 1e-3 and r[1] == 1, str(r))
    r = blur.box_at(dec, 60, ct, 0.7, fb["hold"])
    check("hold before, fading in", r and abs(r[1] - (0.7 - 0.66) / blur.FADE) < 1e-3, str(r))
    check("nothing before the hold", blur.box_at(dec, 60, ct, 0.6, fb["hold"]) is None)
    r = blur.box_at(dec, 60, ct, 1.99, fb["hold"])
    check("held up to the cut without fading", r and r[1] == 1, str(r))
    check("never across the cut", blur.box_at(dec, 60, ct, 2.0, fb["hold"]) is None)

    # ffmpeg's fps filter: 24 fps source -> 3 fps picks source frames 3, 11, 19, ...
    pts = list(range(0, 24 * 4))
    t3 = blur.fps_pick(pts, Fraction(1, 24), 3)
    check("fps filter emulation, 3 fps", [round(x * 24) for x in t3[:4]] == [3, 11, 19, 27], str(t3[:4]))
    t6 = blur.fps_pick(pts, Fraction(1, 24), 6, 1 / 12)
    both = [round(x * 24) for x in t6[:8]]
    check("2x fps shifted: timeline frames + the ones between", set([3, 11, 19]) <= set(both) and 7 in both,
          str(both))


def crosscheck(n):
    lists = [f for f in sorted((ROOT / "skiplists").glob("*.json"))
             if len(f.stem) == 11 and "faces" in json.loads(f.read_text(encoding="utf-8")).get("timeline", {})]
    rng = random.Random(1)
    total = bad = 0
    for f in lists:
        fc = json.loads(f.read_text(encoding="utf-8"))["timeline"]["faces"]
        dec = blur.decode_tracks(fc)
        ct = blur.cut_times(fc)
        q = []
        for _ in range(n):
            k = rng.choice(sorted(dec))
            i = rng.randrange(len(dec[k]))
            fr = dec[k][i][0]
            a, b = fr[0] / fc["fps"] - 0.5, fr[-1] / fc["fps"] + 0.5
            q.append([k, i, rng.uniform(a, b)])
        for c in ct[:50]:  # right at cuts
            for k in dec:
                for i, tr in enumerate(dec[k]):
                    if tr[0][0] / fc["fps"] - 0.5 <= c <= tr[0][-1] / fc["fps"] + 0.5:
                        q += [[k, i, c - 1e-4], [k, i, c], [k, i, c + 1e-4]]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as t:
            json.dump(q, t)
        js = json.loads(subprocess.run(["node", str(ROOT / "tests" / "blur_test.js"), "--at", str(f), t.name],
                                       capture_output=True, text=True, check=True).stdout)
        Path(t.name).unlink()
        diff = 0
        for (k, i, tt), j in zip(q, js):
            p = blur.box_at(dec[k][i], fc["fps"], ct, tt, fc["hold"])
            ok = (p is None and j is None) or (p is not None and j is not None and
                                               np.allclose([*p[0], p[1]], j, atol=1e-4))
            diff += not ok
        total += len(q)
        bad += diff
        print(f"[{'FAIL' if diff else 'PASS'}] {f.stem}: {len(q) - diff}/{len(q)} times agree "
              f"({sum(len(v) for v in dec.values())} tracks)")
    check(f"blur.js = blur.py on {len(lists)} skip lists", bad == 0 and len(lists) > 0, f"{total - bad}/{total}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=3000)
    a = ap.parse_args()
    unit()
    crosscheck(a.samples)
    print("\nall passed" if not fails else f"\n{fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

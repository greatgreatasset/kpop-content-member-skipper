"""Face boxes for "Blur skipped members when they can't be skipped" (skip list
format 6, timeline["faces"]).

When a member a user skips shares the screen with someone they don't skip, the
frame isn't skipped; the add-on can then blur her face. For that the skip list
carries, for every recognized member, where her face is in the sampled frames,
and the scene cuts, so the add-on never slides a box from one shot into the
next:

    "faces": {"fps": 60, "hold": 0.34, "cuts": [740, 4801, ...],
              "tracks": {"hearts2hearts:carmen": [[frame, "<samples>"], ...], ...}}

Times are in units of 1/fps s (1/60). The samples are the timeline's frames
(3 fps) and the frames midway between them (the scanner's blur pass, so 6 fps),
at the time each frame really shows (ffmpeg's fps filter picks the last source
frame before the middle of each step: see fps_pick). A track is one face
followed through one shot: `frame` is its first sample's time and each sample
is 9 characters: 1 for the step from the previous sample (base64url digit, 0 for
the first) and 8 for the face box x, y, w, h relative to the video frame (0..1,
top-left origin, clipped to the frame), each 2 base64url characters (12 bits,
value / 4095). Gaps of up to `max_gap_s` (a head turned away for a moment) are
inside a track. `cuts` are the times at which a new shot starts.

Every recognized member has boxes (each user skips someone else), also faces
too small or too far in the background for the timeline when they match a
member, and faces unknown on their own that are part of a recognized track.

The add-on shows a track's box from `hold` s before its first sample to `hold` s
after its last, but never across a cut, interpolating linearly between samples
and fading in / out over 0.15 s at ends that aren't a cut (`box_at` here, the
same as extension/blur.js).
"""
import re
import subprocess

import numpy as np

B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
B64_IDX = {c: i for i, c in enumerate(B64)}
Q = 4095  # 12-bit quantization
FADE = 0.15

DEFAULTS = dict(
    cut_thr=0.3,          # ffmpeg scene score above which a frame starts a new shot
    blur_min_face=12,     # px: smaller faces are not given a box
    max_gap_s=0.7,        # s: bridge gaps up to this inside one track (same shot, nearby)
    link_dist=1.0,        # max centre distance (in face widths) to bridge a gap
    hold=0.34,            # s: show a box this long before / after its track's ends
)


# ---------------------------------------------------------------- scene cuts
SCENE_FILTER = "scale=160:90,select='gt(scene,{thr})',showinfo"
_PTS = re.compile(r"Parsed_showinfo.*?pts_time:\s*([0-9.]+)")


def parse_cuts(lines):
    """Cut times from ffmpeg showinfo log lines."""
    return sorted({round(float(m.group(1)), 3) for m in map(_PTS.search, lines) if m})


def detect_cuts(path, thr=DEFAULTS["cut_thr"]):
    """Scene cut times (s) of a video file (every frame, scaled down to 160x90)."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-an",
                        "-vf", SCENE_FILTER.format(thr=thr), "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")
    if r.returncode:
        raise RuntimeError(f"ffmpeg scene detection failed: {r.stderr.strip()[-300:]}")
    return parse_cuts(r.stderr.splitlines())


def cut_between(cuts, t0, t1):
    """True if a scene cut falls in (t0, t1]."""
    i = np.searchsorted(cuts, t0 + 1e-6, side="right")
    return bool(i < len(cuts) and cuts[i] <= t1 + 1e-6)


# ---------------------------------------------------------------- encoding
def enc_val(v):
    q = int(round(min(1.0, max(0.0, v)) * Q))
    return B64[q >> 6] + B64[q & 63]


def dec_val(s, i):
    return (B64_IDX[s[i]] << 6 | B64_IDX[s[i + 1]]) / Q


def norm_boxes(xyxy, frame_size):
    """Pixel boxes (x1, y1, x2, y2) -> (x, y, w, h) relative to the frame, clipped."""
    W, H = frame_size
    x1 = np.clip(xyxy[:, 0] / W, 0, 1)
    y1 = np.clip(xyxy[:, 1] / H, 0, 1)
    x2 = np.clip(xyxy[:, 2] / W, 0, 1)
    y2 = np.clip(xyxy[:, 3] / H, 0, 1)
    return np.stack([x1, y1, np.maximum(0, x2 - x1), np.maximum(0, y2 - y1)], 1)


def enc_track(run):
    """[(frame, box), ...] -> [first frame, samples] (see module doc)."""
    f0, last, s = run[0][0], run[0][0], []
    for f, b in run:
        d = f - last
        assert 0 <= d < 64, d
        s.append(B64[d] + "".join(enc_val(v) for v in b))
        last = f
    return [int(f0), "".join(s)]


def dec_track(tr):
    """[first frame, samples] -> ([frame, ...], [[x, y, w, h], ...])."""
    f, s = int(tr[0]), tr[1]
    frames, boxes = [], []
    for i in range(0, len(s), 9):
        f += B64_IDX[s[i]]
        frames.append(f)
        boxes.append([dec_val(s, i + 1 + 2 * k) for k in range(4)])
    return frames, boxes


# ---------------------------------------------------------------- labels -> tracks
def link_faces(fi, xyxy, emb, max_step, p):
    """Track id per face: faces in consecutive sampled frames (at most max_step
    grid frames apart) linked greedily by IoU, with an embedding check (as
    scan.face_tracks, which links frames exactly 1 apart)."""
    track = -np.ones(len(fi), int)
    by_frame = {}
    for i in np.argsort(fi, kind="stable"):
        by_frame.setdefault(int(fi[i]), []).append(i)
    area = (xyxy[:, 2] - xyxy[:, 0]) * (xyxy[:, 3] - xyxy[:, 1])
    prev, last_f, n = [], None, 0
    for f in sorted(by_frame):
        cur = by_frame[f]
        cand = []
        if last_f is not None and f - last_f <= max_step:
            for i in cur:
                for j in prev:
                    iw = min(xyxy[i, 2], xyxy[j, 2]) - max(xyxy[i, 0], xyxy[j, 0])
                    ih = min(xyxy[i, 3], xyxy[j, 3]) - max(xyxy[i, 1], xyxy[j, 1])
                    inter = max(0.0, iw) * max(0.0, ih)
                    ua = area[i] + area[j] - inter
                    o = inter / ua if ua > 0 else 0.0
                    if o >= p["track_iou"] and float(emb[i] @ emb[j]) >= p["track_sim"]:
                        cand.append((o, i, j))
        used_i, used_j = set(), set()
        for o, i, j in sorted(cand, reverse=True):
            if i in used_i or j in used_j:
                continue
            track[i] = track[j]
            used_i.add(i)
            used_j.add(j)
        for i in cur:
            if track[i] < 0:
                track[i] = n
                n += 1
        prev, last_f = cur, f
    return track


def fill_from_tracks(own, track):
    """Faces with no label of their own (-1) get their track's majority label.
    A face's own match is never overridden: a track can jump between
    look-alikes, and blurring the wrong member is worse than missing a frame."""
    out = own.copy()
    if not len(own):
        return out
    order = np.argsort(track, kind="stable")
    for idx in np.split(order, np.flatnonzero(np.diff(track[order])) + 1):
        conf = own[idx][own[idx] >= 0]
        if len(conf):
            vals, cnt = np.unique(conf, return_counts=True)
            out[idx[own[idx] == -1]] = vals[np.argmax(cnt)]
    return out


def own_labels(lab, best, score, thr):
    """Each face's own identity for blurring: the timeline label (lab >= 0),
    else for faces the timeline leaves out as too small / background (-2) or
    never labelled (None) their best match if it clears the threshold."""
    own = np.where(score >= thr, best, -1) if lab is None else np.asarray(lab).copy()
    if lab is not None:
        small = own == -2
        own[small] = np.where(score[small] >= thr, best[small], -1)
    return own


def build_tracks(fi, boxes, blab, track, keys, fps, cuts, p):
    """Per member key: [[first frame, samples], ...] (see module doc).
    fi: grid frame per face, boxes: (x, y, w, h) 0..1, blab: class index or -1,
    track: face track ids (link_faces)."""
    cuts = np.asarray(cuts, float)
    max_gap = int(round(p["max_gap_s"] * fps))
    pieces = {}  # (member, face track) -> [(frame, box)]
    for i in np.argsort(fi, kind="stable"):
        if blab[i] >= 0:
            pieces.setdefault((int(blab[i]), int(track[i])), []).append((int(fi[i]), [float(v) for v in boxes[i]]))
    runs = {}  # member -> runs within one shot
    for (c, _), seq in pieces.items():
        cur = [seq[0]]
        for prev, nxt in zip(seq, seq[1:]):
            if nxt[0] - prev[0] > max_gap or cut_between(cuts, prev[0] / fps, nxt[0] / fps):
                runs.setdefault(c, []).append(cur)
                cur = []
            cur.append(nxt)
        runs.setdefault(c, []).append(cur)
    out = {}
    for c, rs in runs.items():
        rs.sort(key=lambda r: r[0][0])
        merged = []
        for r in rs:  # join runs of the same member across short gaps (same shot, nearby)
            m = None
            for cand in reversed(merged[-4:]):
                (pf, pb), (nf, nb) = cand[-1], r[0]
                if nf <= pf or nf - pf > max_gap or cut_between(cuts, pf / fps, nf / fps):
                    continue
                dist = np.hypot((pb[0] + pb[2] / 2) - (nb[0] + nb[2] / 2),
                                ((pb[1] + pb[3] / 2) - (nb[1] + nb[3] / 2)) * p["aspect"])
                if dist <= p["link_dist"] * max(pb[2], nb[2], 1e-3):
                    m = cand
                    break
            if m is None:
                merged.append(list(r))
            else:
                m += r
        out[keys[c]] = [enc_track(r) for r in sorted(merged, key=lambda r: r[0][0])]
    return dict(sorted(out.items()))


GRID = 60  # time unit of timeline["faces"] (1/60 s)


def faces_block(t, xyxy, emb, own, keys, link_step, frame_size, cuts, p):
    """timeline["faces"] from the faces of the sampled frames: t (s, what the
    frame really shows), pixel boxes, embeddings, own labels (class index or -1).
    Faces are linked into tracks across sampled frames up to link_step s apart.
    cuts: scene cut times (s)."""
    q = {**DEFAULTS, **p, "aspect": frame_size[1] / frame_size[0]}
    fi = np.round(np.asarray(t, float) * GRID).astype(int)
    cg = sorted({int(round(c * GRID)) for c in cuts})
    size = np.minimum(xyxy[:, 2] - xyxy[:, 0], xyxy[:, 3] - xyxy[:, 1]) if len(xyxy) else np.zeros(0)
    tracks = {}
    if len(fi):
        track = link_faces(fi, xyxy, emb, int(round(link_step * GRID)) + 1, q)
        blab = fill_from_tracks(np.asarray(own), track)
        blab[size < q["blur_min_face"]] = -1
        tracks = build_tracks(fi, norm_boxes(xyxy, frame_size), blab, track, keys, GRID,
                              np.array(cg, float) / GRID, q)
    return {"fps": GRID, "hold": q["hold"], "cuts": cg, "tracks": tracks}


# ---------------------------------------------------------------- playback (= extension/blur.js)
def decode_tracks(faces):
    """timeline["faces"] -> per key: [([frame, ...], [[x, y, w, h], ...]), ...]."""
    return {k: [dec_track(t) for t in tr] for k, tr in faces["tracks"].items()}


def cut_times(faces):
    return np.array(faces["cuts"], float) / faces["fps"]


def track_span(t0, t1, cuts, hold):
    """(from, to, fade in?, fade out?) of a track with first / last sample at
    t0 / t1: `hold` s beyond its ends, but never across a scene cut (no fade there)."""
    i = int(np.searchsorted(cuts, t0 + 1e-6, side="right"))  # cuts[:i] <= t0
    lo = cuts[i - 1] if i > 0 else -1e9
    j = int(np.searchsorted(cuts, t1 + 1e-6, side="right"))  # first cut > t1
    hi = cuts[j] if j < len(cuts) else 1e9
    start, end = max(lo, t0 - hold), min(hi, t1 + hold)
    return start, end, start > lo, end < hi


def box_at(track, fps, cuts, t, hold, fade=FADE):
    """(box, opacity) of a decoded track at time t, or None when not shown."""
    frames, boxes = track
    ts = [f / fps for f in frames]
    start, end, fin, fout = track_span(ts[0], ts[-1], cuts, hold)
    if t < start or t >= end:
        return None
    if t <= ts[0]:
        b = boxes[0]
    elif t >= ts[-1]:
        b = boxes[-1]
    else:
        k = int(np.searchsorted(ts, t, side="right")) - 1
        f = (t - ts[k]) / (ts[k + 1] - ts[k])
        b = [boxes[k][m] + (boxes[k + 1][m] - boxes[k][m]) * f for m in range(4)]
    op = 1.0
    if fin and fade:
        op = min(op, (t - start) / fade)
    if fout and fade:
        op = min(op, (end - t) / fade)
    return b, max(0.0, min(1.0, op))


def pad_box(b, pad):
    """Box grown by pad x its width (left / right) and height (top / bottom)."""
    x, y, w, h = b
    return [x - pad * w, y - pad * h, w * (1 + 2 * pad), h * (1 + 2 * pad)]


# ---------------------------------------------------------------- frame times
def packet_times(path):
    """(sorted presentation timestamps of the video stream, its time base)."""
    import json
    from fractions import Fraction
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=time_base:packet=pts", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    tb = Fraction(d["streams"][0]["time_base"])
    pts = sorted(int(x["pts"]) for x in d.get("packets", []) if str(x.get("pts", "")).lstrip("-").isdigit())
    return pts, tb


def _round_near_inf(x):
    """Round a Fraction half away from zero (ffmpeg AV_ROUND_NEAR_INF)."""
    from math import floor
    return floor(x + 1 / 2) if x >= 0 else -floor(-x + 1 / 2)


def fps_pick(pts, tb, fps, shift=0.0):
    """Which source frame ffmpeg's fps filter (round=near) outputs for each
    output frame, after `setpts=PTS-shift/TB`: -> list of source times (s), the
    time of what each output frame really shows. (The fps filter outputs, for
    output slot k, the last input frame whose rescaled pts rounds to <= k.)"""
    from fractions import Fraction
    F = Fraction(fps).limit_denominator(1000)
    sh = int(float(shift) / float(tb))  # setpts: double -> int64, truncated
    r = [_round_near_inf(Fraction(p - sh) * tb * F) for p in pts]
    out, i = [], 0
    for k in range(r[0], r[-1] + 1):
        while i + 1 < len(r) and r[i + 1] <= k:
            i += 1
        out.append(float(pts[i] * tb))
    return out

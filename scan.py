"""Scan a YouTube video and write skiplists/<profile>/<videoId>.json.

    python scan.py <youtube url or id> [--profile hearts2hearts] [--fps 3] [--min-face 40] ...

Any common link form works (youtube.com/watch?v=, m./music. hosts, youtu.be,
/shorts/, /live/, /embed/, extra &t= / &list= params) as does a bare 11-char
video ID; the link is always normalized to https://www.youtube.com/watch?v=<ID>.
Download problems print one short line; --debug shows the full traceback.

Pipeline: yt-dlp download (video only) -> ffmpeg decode at --fps -> InsightFace
detection + ArcFace embedding on GPU -> per-face member/unknown classification
against data/<profile>/refs.npz -> per-frame SKIP/KEEP/NONE -> gap fill + smoothing ->
segments.

Raw detections (time, bbox, embedding) are cached in cache/<id>.faces.npz so
classification/smoothing can be re-tuned without re-running the GPU pass
(use --rescan to force it).
"""
import argparse
import json
import queue
import re
import subprocess
import threading
import time
import warnings
from datetime import datetime, timezone

import numpy as np

from common import CACHE, DEFAULT_PROFILE, Profile, update_index

# harmless deprecation notice from insightface's use of scikit-image
warnings.filterwarnings("ignore", category=FutureWarning, message=".*estimate.*deprecated")

SCANNER_VERSION = "0.3.0"
SKIPLIST_FORMAT = 2
CACHE_FORMAT = "2"  # bump when the cached detection arrays change

DEFAULTS = dict(
    fps=3.0,
    height=720,
    det_size=640,
    min_face=40,          # px, short side of face box in the decoded frame
    method="knn",         # knn | centroid
    knn_k=5,
    thr=None,             # member acceptance threshold (None -> value from refs.npz)
    fill_gap=3.0,         # s: faceless/unknown-only runs up to this inherit neighbors
    median=5,             # frames: median filter window on the skip signal
    merge_gap=1.0,        # s: merge skip segments separated by less than this
    min_skip=2.0,         # s: drop skip segments shorter than this
    hl_merge_gap=2.0,     # s: merge highlight ranges separated by less than this
    hl_min=1.0,           # s: drop highlight ranges shorter than this
    track=1,              # 1: propagate identity along face tracks (IoU + embedding link)
    track_iou=0.3,
    track_sim=0.3,        # min cosine between consecutive faces of one track (guards scene cuts)
    rel_size=0.4,         # ignore faces smaller than this fraction of the frame's largest face
    # A member other than Carmen/Ye-on only blocks a skip while her face is
    # fully shown (Kirk: a half-visible member "is not part of the scene").
    full_clip=0.10,       # max fraction of the face box outside the frame
    # Off by default: profile/blurred faces fully inside the frame are still part
    # of the scene (e.g. Juun in profile at 7:40 of oVMQTIUI_2c).
    full_det=0.0,         # min detection score (0 = off)
    full_yaw=99.0,        # max |nose offset from eye midpoint| / eye distance (99 = off)
    edge_trim=0.5,        # s: trim each skip range inward at both ends ...
    min_trimmed=1.5,      # s: ... and drop it if it is then shorter than this
)


# ---------------------------------------------------------------- download
_ID = r"([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])"
_ID_PATTERNS = [
    re.compile(r"[?&#](?:v|vi)=" + _ID),                                   # watch?v=, &v=
    re.compile(r"youtu\.be/" + _ID, re.I),                                # youtu.be/<id>
    re.compile(r"youtube(?:-nocookie)?\.com/(?:shorts|live|embed|v|e)/" + _ID, re.I),
    re.compile(r"^" + _ID + r"$"),                                         # bare id
]


def video_id(text):
    """11-character video ID from a YouTube link (any common form) or a bare ID, else None."""
    t = text.strip().strip("\"'<>").strip()
    for pat in _ID_PATTERNS:
        m = pat.search(t)
        if m:
            return m.group(1)
    return None


def watch_url(vid):
    return f"https://www.youtube.com/watch?v={vid}"


class DownloadFailed(Exception):
    pass


class _YdlLogger:
    """Keeps yt-dlp quiet; its error text ends up in DownloadFailed instead."""
    def __init__(self, debug):
        self.debug_mode = debug

    def debug(self, msg):
        if self.debug_mode:
            print(msg)

    info = debug

    def warning(self, msg):
        if self.debug_mode:
            print(msg)

    def error(self, msg):
        if self.debug_mode:
            print(msg)


def _one_line(err):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(err)).strip()
    msg = msg.splitlines()[0] if msg else type(err).__name__
    msg = re.sub(r"^ERROR:\s*", "", msg)
    return msg if len(msg) <= 200 else msg[:197] + "..."


def download(url, height, debug=False, hook=None):
    import yt_dlp

    CACHE.mkdir(exist_ok=True)
    opts = {
        # prefer H.264 (cheapest decode), fall back to any codec <= height
        "format": f"bv*[height<={height}][vcodec^=avc1]/bv*[height<={height}]/b[height<={height}]",
        "outtmpl": str(CACHE / "%(id)s.%(ext)s"),
        "quiet": True,
        "noprogress": True,
        "no_warnings": not debug,
        "logger": _YdlLogger(debug),
    }
    if hook:
        opts["progress_hooks"] = [hook]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
    except Exception as e:
        if debug:
            raise
        raise DownloadFailed(_one_line(e)) from e
    return info, path


# ---------------------------------------------------------------- decode + detect
def probe_size(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", path], text=True)
    w, h = map(int, out.strip().split(",")[:2])
    return w, h


def decode_frames(path, fps, height, q, stats):
    """Producer: ffmpeg -> raw BGR frames -> queue. Records its own busy time."""
    w0, h0 = probe_size(path)
    h = min(height, h0)
    w = int(round(w0 * h / h0 / 2) * 2)
    # software decode: for 720p, dav1d/h264 on CPU beat -hwaccel cuda here
    # (240 s of AV1 at 3 fps: ~3 s CPU vs ~14 s cuda with frame copy-back)
    cmd = ["ffmpeg", "-v", "error", "-threads", "8", "-i", path,
           "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    t0 = time.perf_counter()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=w * h * 3 * 4)
    n, fsize, blocked = 0, w * h * 3, 0.0
    while True:
        buf = p.stdout.read(fsize)
        if len(buf) < fsize:
            break
        frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        tb = time.perf_counter()
        q.put((n / fps, frame))
        blocked += time.perf_counter() - tb
        n += 1
    p.wait()
    q.put(None)
    stats["decode_wall"] = time.perf_counter() - t0
    stats["decode_busy"] = stats["decode_wall"] - blocked  # time not waiting on GPU
    stats["frames"] = n
    stats["frame_size"] = (w, h)


def detect_all(path, fps, height, det_size, on_frame=None):
    from common import load_face_app

    app = load_face_app(det_size=det_size, det_thresh=0.5)
    q = queue.Queue(maxsize=32)
    stats = {}
    th = threading.Thread(target=decode_frames, args=(path, fps, height, q, stats), daemon=True)
    t0 = time.perf_counter()
    th.start()
    times, rows, embs, kps = [], [], [], []
    det_busy = 0.0
    while True:
        item = q.get()
        if item is None:
            break
        t, frame = item
        tb = time.perf_counter()
        faces = app.get(frame)
        det_busy += time.perf_counter() - tb
        fi = len(times)
        times.append(t)
        for f in faces:
            rows.append([fi, *f.bbox.tolist(), float(f.det_score)])
            embs.append(f.normed_embedding.astype(np.float32))
            kps.append(np.asarray(f.kps, np.float32).reshape(5, 2))
        if on_frame:
            on_frame(t)
    th.join()
    stats["detect_busy"] = det_busy
    stats["pipeline_wall"] = time.perf_counter() - t0
    return (np.array(times, np.float32),
            np.array(rows, np.float32).reshape(-1, 6),
            np.array(embs, np.float32).reshape(-1, 512),
            np.array(kps, np.float32).reshape(-1, 5, 2),
            stats)


# ---------------------------------------------------------------- classify
def load_refs(prof):
    z = np.load(prof.data / "refs.npz")
    assert list(z["members"]) == prof.members, "refs.npz members differ from the profile; rerun build_refs.py"
    return dict(prof=prof, members=prof.members,
                emb=z["emb"].astype(np.float32), labels=z["labels"].astype(int),
                centroids=z["centroids"], thr_knn=float(z["unknown_thr_knn"]),
                thr_centroid=float(z["unknown_thr_centroid"]))


def class_scores(emb, refs, method, k):
    if len(emb) == 0:
        return np.zeros((0, len(refs["members"])), np.float32)
    if method == "centroid":
        return emb @ refs["centroids"].T
    out = np.empty((len(emb), len(refs["members"])), np.float32)
    for s in range(0, len(emb), 4096):
        sims = emb[s:s + 4096] @ refs["emb"].T
        for c in range(len(refs["members"])):
            sc = sims[:, refs["labels"] == c]
            kk = min(k, sc.shape[1])
            out[s:s + 4096, c] = np.partition(-sc, kk - 1, axis=1)[:, :kk].mean(1) * -1
    return out


def classify(rows, emb, refs, p):
    """Returns per-face label index (-1 unknown, -2 too small) and best score."""
    thr = p["thr"] if p["thr"] is not None else refs[f"thr_{p['method']}"]
    S = class_scores(emb, refs, p["method"], p["knn_k"])
    best = S.argmax(1) if len(S) else np.zeros(0, int)
    score = S.max(1) if len(S) else np.zeros(0)
    lab = np.where(score >= thr, best, -1)
    w = rows[:, 3] - rows[:, 1]
    h = rows[:, 4] - rows[:, 2]
    size = np.minimum(w, h)
    lab = np.where(size >= p["min_face"], lab, -2)
    if p["rel_size"] > 0 and len(rows):
        fi = rows[:, 0].astype(int)
        biggest = np.zeros(fi.max() + 1)
        np.maximum.at(biggest, fi, size)
        lab = np.where(size >= p["rel_size"] * biggest[fi], lab, -2)
    if p["track"]:
        lab = track_relabel(rows, emb, lab, S, p)
    return lab, score, thr


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def track_relabel(rows, emb, lab, S, p):
    """Link faces across consecutive frames (greedy IoU, embedding check), then
    give unknown faces in a track the track's majority confident label. Carries
    identity through profile / half-out-of-frame / blurred frames. Tiny faces
    (-2) stay tiny."""
    fi = rows[:, 0].astype(int)
    track = -np.ones(len(rows), int)
    prev = []          # indices of faces in previous frame
    n_tracks = 0
    order = np.argsort(fi, kind="stable")
    by_frame = {}
    for i in order:
        by_frame.setdefault(fi[i], []).append(i)
    last_f = None
    for f in sorted(by_frame):
        cur = by_frame[f]
        cand = []
        if last_f is not None and f - last_f == 1:
            for i in cur:
                for j in prev:
                    o = iou(rows[i, 1:5], rows[j, 1:5])
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
                track[i] = n_tracks
                n_tracks += 1
        prev, last_f = cur, f
    out = lab.copy()
    for t in range(n_tracks):
        idx = np.where(track == t)[0]
        conf = lab[idx][lab[idx] >= 0]
        if len(conf) == 0:
            continue
        vals, cnt = np.unique(conf, return_counts=True)
        best = vals[np.argmax(cnt)]
        # only fill faces that are unknown on their own; never override a confident
        # match (tracks can jump between similar-looking members, e.g. Carmen/Ian)
        out[idx[lab[idx] == -1]] = best
    return out


# ---------------------------------------------------------------- frames -> segments
SKIP, KEEP, NONE = 1, 0, -1


def frame_states(n_frames, rows, lab, skip_ids):
    """Per frame: SKIP if only skip-members are recognized, KEEP if anyone else
    is, NONE if nobody is. Also returns the recognized member set per frame."""
    skip_ids = set(skip_ids)
    recog = [set() for _ in range(n_frames)]
    unknown = np.zeros(n_frames, int)
    for fi, l in zip(rows[:, 0].astype(int), lab):
        if l >= 0:
            recog[fi].add(int(l))
        elif l == -1:
            unknown[fi] += 1
    state = np.full(n_frames, NONE)
    for i, r in enumerate(recog):
        if r:
            state[i] = SKIP if (r <= skip_ids) else KEEP
    return state, unknown, recog


def fill_gaps(state, max_gap):
    """NONE runs <= max_gap frames inherit neighbors (split at midpoint if they
    disagree); longer runs, and runs at the ends, become KEEP."""
    s = state.copy()
    n = len(s)
    i = 0
    while i < n:
        if s[i] != NONE:
            i += 1
            continue
        j = i
        while j < n and s[j] == NONE:
            j += 1
        left = state[i - 1] if i > 0 else NONE
        right = state[j] if j < n else NONE
        if (j - i) <= max_gap and left != NONE and right != NONE:
            mid = (i + j) // 2
            s[i:mid] = left
            s[mid:j] = right
        elif (j - i) <= max_gap and (left != NONE or right != NONE):
            s[i:j] = left if left != NONE else right
        else:
            s[i:j] = KEEP
        i = j
    return s


def median_filter(x, w):
    if w <= 1:
        return x
    pad = w // 2
    xp = np.pad(x.astype(float), pad, mode="edge")
    return np.array([np.median(xp[i:i + w]) for i in range(len(x))]) >= 0.5


def full_face_mask(rows, kps, frame_size, p):
    """True where the face is fully shown: box (almost) inside the frame, all 5
    landmarks inside, a confident detection and not a strong profile."""
    if kps is None or frame_size is None or len(rows) == 0:
        return np.ones(len(rows), bool)
    W, H = frame_size
    x1, y1, x2, y2 = rows[:, 1], rows[:, 2], rows[:, 3], rows[:, 4]
    area = np.maximum((x2 - x1) * (y2 - y1), 1)
    inside = (np.clip(x2, 0, W) - np.clip(x1, 0, W)) * (np.clip(y2, 0, H) - np.clip(y1, 0, H))
    clip_frac = 1 - inside / area
    kx, ky = kps[:, :, 0], kps[:, :, 1]
    kps_in = ((kx >= 1) & (kx <= W - 1) & (ky >= 1) & (ky <= H - 1)).all(1)
    eye_mid = (kps[:, 0, 0] + kps[:, 1, 0]) / 2
    eye_d = np.maximum(np.hypot(*(kps[:, 1] - kps[:, 0]).T), 1)
    yaw = np.abs(kps[:, 2, 0] - eye_mid) / eye_d
    return (clip_frac <= p["full_clip"]) & kps_in & (rows[:, 5] >= p["full_det"]) & (yaw <= p["full_yaw"])


def subtract(segs, cut):
    """Remove the `cut` ranges from `segs`."""
    out = []
    for s in segs:
        pieces = [(s["start"], s["end"])]
        for c in cut:
            nxt = []
            for a, b in pieces:
                if c["end"] <= a or c["start"] >= b:
                    nxt.append((a, b))
                    continue
                if c["start"] > a:
                    nxt.append((a, c["start"]))
                if c["end"] < b:
                    nxt.append((c["end"], b))
            pieces = nxt
        out += [{"start": round(a, 2), "end": round(b, 2)} for a, b in pieces if b - a >= 0.5]
    return out


def to_segments(mask, times, dt, merge_gap, min_len, trim=0.0, min_trimmed=0.0):
    segs = []
    i, n = 0, len(mask)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < n and mask[j]:
            j += 1
        segs.append([float(times[i]), float(times[j - 1] + dt)])
        i = j
    merged = []
    for s in segs:
        if merged and s[0] - merged[-1][1] < merge_gap:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    merged = [s for s in merged if s[1] - s[0] >= min_len]
    if trim:
        merged = [[a + trim, b - trim] for a, b in merged if (b - a) - 2 * trim >= min_trimmed]
    return [{"start": round(a, 2), "end": round(b, 2)} for a, b in merged]


PARTIAL = -3  # recognized member who is not fully shown: does not block a skip


def build_segments(times, rows, emb, refs, p, kps=None, frame_size=None):
    dt = 1.0 / p["fps"]
    prof = refs["prof"]
    lab, score, thr = classify(rows, emb, refs, p)       # identity (after tracking)
    full = full_face_mask(rows, kps, frame_size, p)
    skip_ids = prof.idx(prof.skip)
    eff = lab.copy()
    eff[(lab >= 0) & ~np.isin(lab, skip_ids) & ~full] = PARTIAL
    state, unknown, recog = frame_states(len(times), rows, eff, skip_ids)
    _, _, recog_any = frame_states(len(times), rows, lab, skip_ids)  # highlights: any face
    filled = fill_gaps(state, int(round(p["fill_gap"] * p["fps"])))
    skip = median_filter(filled == SKIP, p["median"])
    skips = to_segments(skip, times, dt, p["merge_gap"], p["min_skip"],
                        p["edge_trim"], p["min_trimmed"])
    highlights, hl_masks = [], {}
    for m in prof.highlight:
        c = prof.members.index(m)
        mask = np.array([c in r for r in recog_any], bool)
        mask = median_filter(mask, 3) if p["median"] > 1 else mask
        hl_masks[m] = mask
        # highlights never overlap a skip (they would be jumped over anyway)
        rng = subtract(to_segments(mask, times, dt, p["hl_merge_gap"], p["hl_min"]), skips)
        highlights.append({"member": m, "name": prof.names[m],
                           "ranges": [s for s in rng if s["end"] - s["start"] >= p["hl_min"]]})
    diag = dict(
        thr=thr,
        faces=int(len(lab)),
        faces_small=int((lab == -2).sum()),
        faces_unknown=int((lab == -1).sum()),
        faces_partial_nonblocking=int((eff == PARTIAL).sum()),
        faces_by_member={m: int((lab == c).sum()) for c, m in enumerate(prof.members)},
        frames_with_unknown=int((unknown > 0).sum()),
        frames_skip_with_unknown=int(((state == SKIP) & (unknown > 0)).sum()),
        frames_no_member=int((state == NONE).sum()),
    )
    return skips, highlights, dict(lab=lab, eff=eff, full=full, score=score, state=state,
                                   filled=filled, skip=skip, highlight=hl_masks, recog=recog,
                                   unknown=unknown), diag


# ---------------------------------------------------------------- main
def scan(url, p, rescan=False, profile=DEFAULT_PROFILE, debug=False, progress=None):
    """progress: optional object with download_hook(d), detect_start(duration),
    frame(t), cached() (see scan_cli.py)."""
    prof = Profile(profile)
    vid = video_id(url)
    if vid:
        url = watch_url(vid)
    T = {}
    t0 = time.perf_counter()
    info, path = download(url, p["height"], debug, progress and progress.download_hook)
    T["download"] = time.perf_counter() - t0
    vid = info["id"]
    cache = CACHE / f"{vid}.faces.npz"
    key = f"{CACHE_FORMAT}|{p['fps']}|{p['height']}|{p['det_size']}"
    if cache.exists() and not rescan and str(np.load(cache)["key"]) == key:
        z = np.load(cache)
        times, rows, emb, kps = z["times"], z["rows"], z["emb"], z["kps"]
        frame_size = tuple(int(x) for x in z["frame_size"])
        st = {"cached": True}
        if progress:
            progress.cached()
    else:
        if progress:
            progress.detect_start(info.get("duration"))
        times, rows, emb, kps, st = detect_all(path, p["fps"], p["height"], p["det_size"],
                                               progress and progress.frame)
        frame_size = st["frame_size"]
        np.savez(cache, times=times, rows=rows, emb=emb, kps=kps,
                 frame_size=np.array(frame_size), key=key)
    T.update({k: v for k, v in st.items() if k.endswith(("wall", "busy"))})
    tc = time.perf_counter()
    refs = load_refs(prof)
    skips, highlights, dbg, diag = build_segments(times, rows, emb, refs, p, kps, frame_size)
    T["classify_smooth"] = time.perf_counter() - tc
    T["total"] = time.perf_counter() - t0

    out = dict(
        format=SKIPLIST_FORMAT, videoId=vid, profile=prof.id, group=prof.group,
        title=info.get("title"), duration=info.get("duration"),
        scannedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        scannerVersion=SCANNER_VERSION,
        skipMembers=[prof.names[m] for m in prof.skip],
        skips=skips, highlights=highlights,
    )
    prof.skiplists.mkdir(parents=True, exist_ok=True)
    (prof.skiplists / f"{vid}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                                encoding="utf-8")
    update_index(prof.skiplists, prof)
    # lets scan.bat / publish_skiplists.py know which list was just written
    (CACHE / "last_scan.json").write_text(json.dumps({"videoId": vid, "profile": prof.id}),
                                          encoding="utf-8")
    return out, T, st, diag, (times, rows, emb, refs, dbg)


def fmt(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def build_parser(prog=None):
    ap = argparse.ArgumentParser(prog=prog)
    # nargs="+": a link split by the shell (e.g. cmd.exe breaks unquoted links at "=")
    # arrives as several pieces and is joined back together.
    ap.add_argument("url", nargs="+", help="YouTube link (any common form) or 11-char video ID")
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--debug", action="store_true", help="show full tracebacks")
    for k, v in DEFAULTS.items():
        ap.add_argument(f"--{k.replace('_', '-')}", type=type(v) if v is not None else float, default=v)
    ap.add_argument("--rescan", action="store_true")
    return ap


def parse_link(pieces):
    """Video ID from link pieces (rejoined), or None."""
    raw = " ".join(pieces)
    return video_id(raw) or video_id(raw.replace(" ", "="))


def main():
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a = build_parser().parse_args()
    p = {k: getattr(a, k) for k in DEFAULTS}
    vid = parse_link(a.url)
    if not vid:
        print(f"Couldn't find a YouTube video ID in: {' '.join(a.url)}")
        sys.exit(2)
    try:
        out, T, st, diag, _ = scan(watch_url(vid), p, a.rescan, a.profile, a.debug)
    except DownloadFailed as e:
        print(f"Couldn't download the video: {e}. Check the link and your internet.")
        sys.exit(3)
    print(f"{out['videoId']}  {out['title']}  ({out['duration']} s)")
    print("timing (s):", {k: round(v, 1) for k, v in T.items()},
          "frames:", st.get("frames"), "size:", st.get("frame_size"))
    print("diagnostics:", json.dumps(diag))
    print("skips:", ", ".join(f"{fmt(s['start'])}-{fmt(s['end'])}" for s in out["skips"]))
    for h in out["highlights"]:
        print(f"{h['name']}:", ", ".join(f"{fmt(s['start'])}-{fmt(s['end'])}" for s in h["ranges"]))


if __name__ == "__main__":
    main()

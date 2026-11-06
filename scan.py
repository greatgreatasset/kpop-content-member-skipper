"""Scan a YouTube video and write skiplists/<videoId>.json.

    python scan.py <youtube url or id> [--profile illit[,hearts2hearts]] [--fps 3] [--min-face 40] ...

Any common link form works (youtube.com/watch?v=, m./music. hosts, youtu.be,
/shorts/, /live/, /embed/, extra &t= / &list= params) as does a bare 11-char
video ID; the link is always normalized to https://www.youtube.com/watch?v=<ID>.
Download problems print one short line; --debug shows the full traceback.

Pipeline: yt-dlp download (video only) -> ffmpeg decode at --fps -> InsightFace
detection + ArcFace embedding on GPU -> per-face member/unknown classification
against every member of every profile (data/*/refs.npz; --profile limits it) ->
which groups are in the video -> per-frame SKIP/KEEP/NONE -> gap fill + smoothing ->
segments.

Raw detections (time, bbox, embedding) are cached in cache/<id>.faces.npz so
classification/smoothing can be re-tuned without re-running the GPU pass
(use --rescan to force it).
"""
import argparse
import json
import os
import queue
import re
import subprocess
import threading
import time
import warnings
from datetime import datetime, timezone

import numpy as np

import rules
from common import CACHE, SKIPLISTS, Gallery, update_index

# harmless deprecation notice from insightface's use of scikit-image
warnings.filterwarnings("ignore", category=FutureWarning, message=".*estimate.*deprecated")

SCANNER_VERSION = "0.6.0"
SKIPLIST_FORMAT = 5
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
    wide_keep=1,          # 1: frames whose faces are all tiny (wide group shot) are KEEP, not inherited
    # A group is "in the video" when enough faces are confidently matched to its
    # members; faces matched to a group that isn't are re-matched among the
    # groups that are (a stray look-alike match never turns into a skip).
    group_min=20,         # faces: a group needs at least this many ...
    group_frac=0.05,      # ... and this share of all confidently matched faces
    # "Main focus" of a frame (for "highlight only when she's the main focus"):
    # the largest face in the frame, if it is a recognized member, at least
    # focus_ratio times the area of the next largest face (any face), and its
    # centre at most focus_center x frame width from the middle.
    focus_ratio=1.5,
    focus_center=0.3,
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


def cookie_browser():
    """Browser whose YouTube cookies yt-dlp uses (your own sign-in):
    KMS_COOKIES = "firefox" (default), "firefox:<profile>", another browser yt-dlp
    knows, or "0"/"off" for none (--no-cookies sets that). yt-dlp only reads them
    into memory; nothing is copied, logged or saved."""
    v = os.environ.get("KMS_COOKIES", "firefox").strip()
    return None if v.lower() in ("", "0", "off", "no", "none", "false") else v


def _ydl_with_cookies(yt_dlp, opts, hook=None):
    """YoutubeDL with the browser's cookies, or without them (one line printed)
    if they can't be read."""
    browser = cookie_browser()
    if browser:
        name, _, profile = browser.partition(":")
        ydl = None
        try:
            ydl = yt_dlp.YoutubeDL({**opts, "cookiesfrombrowser": (name.lower(), profile or None, None, None)})
            ydl.cookiejar  # load now, so a failure falls back instead of failing the download
            return ydl
        except Exception as e:
            if ydl is not None:
                ydl.close()
            print(("\n" if hook else "") + f"Couldn't read {name} cookies ({_one_line(e)}); "
                  "downloading without them.", flush=True)
    return yt_dlp.YoutubeDL(opts)


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
        with _ydl_with_cookies(yt_dlp, opts, hook) as ydl:
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
def load_gallery(profiles=None):
    """profiles: None (every group) or a list / comma-separated string of profile ids."""
    if isinstance(profiles, str):
        profiles = [x.strip() for x in profiles.split(",") if x.strip()]
    return Gallery(profiles or None)


def class_scores(emb, gal, method, k):
    if len(emb) == 0:
        return np.zeros((0, len(gal.keys)), np.float32)
    if method == "centroid":
        return emb @ gal.centroids.T
    out = np.empty((len(emb), len(gal.keys)), np.float32)
    cols = [np.where(gal.labels == c)[0] for c in range(len(gal.keys))]
    for s in range(0, len(emb), 4096):
        sims = emb[s:s + 4096] @ gal.emb.T
        for c, idx in enumerate(cols):
            sc = sims[:, idx]
            kk = min(k, sc.shape[1])
            out[s:s + 4096, c] = np.partition(-sc, kk - 1, axis=1)[:, :kk].mean(1) * -1
    return out


def detect_groups(S, ok, gal, thr, p):
    """Profile indexes of the groups in the video, from the faces (`ok`: big
    enough to count) confidently matched to their members. The group with the
    most is always in."""
    best, score = S.argmax(1), S.max(1)
    conf = ok & (score >= thr)
    counts = np.bincount(gal.cls_prof[best[conf]], minlength=len(gal.profiles))
    total = counts.sum()
    found = [g for g in range(len(gal.profiles))
             if counts[g] and (counts[g] == counts.max()
                               or (counts[g] >= p["group_min"] and counts[g] >= p["group_frac"] * total))]
    return found, counts


def classify(rows, emb, gal, p):
    """Returns per-face class index (-1 unknown, -2 too small / background),
    best score, (threshold, its source) and (groups found, confident faces per group).

    Pass 1 finds the groups with the joint threshold (all members of all groups);
    pass 2 matches each face only among the members of those groups, with the
    threshold for just those groups."""
    thr = p["thr"] if p["thr"] is not None else getattr(gal, f"thr_{p['method']}")
    S = class_scores(emb, gal, p["method"], p["knn_k"])
    w = rows[:, 3] - rows[:, 1]
    h = rows[:, 4] - rows[:, 2]
    size = np.minimum(w, h)
    big = size >= p["min_face"]
    if p["rel_size"] > 0 and len(rows):
        fi = rows[:, 0].astype(int)
        biggest = np.zeros(fi.max() + 1)
        np.maximum.at(biggest, fi, size)
        big &= size >= p["rel_size"] * biggest[fi]
    if len(S):
        groups, counts = detect_groups(S, big, gal, thr, p)
    else:
        groups, counts = [], np.zeros(len(gal.profiles), int)
    if groups and len(groups) < len(gal.profiles):
        S = S.copy()
        S[:, ~np.isin(gal.cls_prof, groups)] = -1.0  # members of groups not in the video
    src = "--thr"
    if p["thr"] is None:
        thr, src = gal.thr_for(groups, p["method"])
    best = S.argmax(1) if len(S) else np.zeros(0, int)
    score = S.max(1) if len(S) else np.zeros(0)
    lab = np.where(score >= thr, best, -1)
    lab = np.where(big, lab, -2)
    if p["track"]:
        lab = track_relabel(rows, emb, lab, S, p)
    return lab, score, (thr, src), (groups, counts)


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


# ---------------------------------------------------------------- frames -> timeline -> segments
# The skip / highlight rules themselves are in rules.py (and extension/rules.js).
SKIP, KEEP, NONE = rules.SKIP, rules.KEEP, rules.NONE
PARTIAL = -3  # recognized member who is not fully shown (eval/debug display)


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


def choice_of(gal, c):
    prof = gal.prof(c)
    m = gal.members[c]
    return "skip" if m in prof.skip else "highlight" if m in prof.highlight else "normal"


def frame_focus(n_frames, rows, lab, frame_size, gal, p):
    """Per frame: the key of the member who is the main subject, or None (see
    focus_ratio / focus_center)."""
    out = [None] * n_frames
    if not len(rows) or frame_size is None:
        return out
    W = frame_size[0]
    area = (rows[:, 3] - rows[:, 1]) * (rows[:, 4] - rows[:, 2])
    by_frame = {}
    for i, fi in enumerate(rows[:, 0].astype(int)):
        by_frame.setdefault(fi, []).append(i)
    for fi, idx in by_frame.items():
        idx = sorted(idx, key=lambda i: -area[i])
        top = idx[0]
        if lab[top] < 0:
            continue
        if len(idx) > 1 and area[top] < p["focus_ratio"] * area[idx[1]]:
            continue
        cx = (rows[top, 1] + rows[top, 3]) / 2
        if abs(cx / W - 0.5) > p["focus_center"]:
            continue
        out[fi] = gal.keys[lab[top]]
    return out


def build_timeline(n_frames, rows, lab, full, gal, groups, p, frame_size=None):
    """Presence timeline of the members of the groups in the video: per frame,
    who is recognized in the foreground with her face fully shown ("members")
    or cut off by the frame edge ("partial"); frames with nobody recognized are
    "wide" (all faces tiny), "unknown" (only non-members) or "none" (no face),
    and the member who is the main focus of the frame, if any ("focus").
    Run-length coded into segments (see rules.py)."""
    fps = p["fps"]
    full_set = [set() for _ in range(n_frames)]
    part_set = [set() for _ in range(n_frames)]
    faces = np.zeros(n_frames, int)
    small = np.zeros(n_frames, int)
    for i, (fi, l) in enumerate(zip(rows[:, 0].astype(int), lab)):
        faces[fi] += 1
        if l >= 0:
            (full_set if full[i] else part_set)[fi].add(gal.keys[l])
        elif l == -2:
            small[fi] += 1
    if p["wide_keep"]:
        wide = (faces > 0) & (small == faces)
    else:
        wide = np.zeros(n_frames, bool)
    focus = frame_focus(n_frames, rows, lab, frame_size, gal, p)
    tokens = []
    for i in range(n_frames):
        f, pp = sorted(full_set[i]), sorted(part_set[i] - full_set[i])
        st = "" if (f or pp) else "wide" if wide[i] else "unknown" if faces[i] else "none"
        tokens.append((tuple(f), tuple(pp), st, focus[i]))
    segs, i = [], 0
    while i < n_frames:
        j = i + 1
        while j < n_frames and tokens[j] == tokens[i]:
            j += 1
        f, pp, st, fo = tokens[i]
        s = {"start": round(i / fps, 2), "end": round(j / fps, 2)}
        if f:
            s["members"] = list(f)
        if pp:
            s["partial"] = list(pp)
        if fo:
            s["focus"] = fo
        if st:
            s["state"] = st
        segs.append(s)
        i = j
    members = [{"key": gal.keys[c], **gal.member_info(c), "default": choice_of(gal, c)}
               for c in range(len(gal.keys)) if gal.cls_prof[c] in groups]
    return {
        "fps": fps, "frames": int(n_frames), "members": members,
        "rules": {"fillGap": p["fill_gap"], "median": p["median"], "mergeGap": p["merge_gap"],
                  "minSkip": p["min_skip"], "edgeTrim": p["edge_trim"], "minTrimmed": p["min_trimmed"],
                  "hlMedian": 3 if p["median"] > 1 else 1, "hlMergeGap": p["hl_merge_gap"],
                  "hlMin": p["hl_min"]},
        # segments carry "focus" (format 5); its presence here tells the add-on so
        "focusRule": {"minRatio": p["focus_ratio"], "maxOffCenter": p["focus_center"]},
        "segments": segs,
    }


def build_segments(times, rows, emb, gal, p, kps=None, frame_size=None):
    """Classify the faces, build the presence timeline and apply the default
    choices of the profiles (rules.py) -> default skips and highlights."""
    lab, score, (thr, thr_src), (groups, counts) = classify(rows, emb, gal, p)  # identity (after tracking)
    full = full_face_mask(rows, kps, frame_size, p)
    tl = build_timeline(len(times), rows, lab, full, gal, groups, p, frame_size)
    res = rules.apply(tl, debug=True)
    skips = res["skips"]
    cls = {k: c for c, k in enumerate(gal.keys)}
    highlights = [{**gal.member_info(cls[h["key"]]), "ranges": h["ranges"]} for h in res["highlights"]]
    skip_ids = gal.classes("skip")
    eff = lab.copy()
    eff[(lab >= 0) & ~np.isin(lab, skip_ids) & ~full] = PARTIAL
    unknown = np.bincount(rows[:, 0].astype(int)[lab == -1], minlength=len(times)) if len(rows) else \
        np.zeros(len(times), int)
    state = np.array(res["debug"]["state"])
    found = [{"id": gal.profiles[g].id, "name": gal.profiles[g].group, "faces": int(counts[g])}
             for g in sorted(groups, key=lambda g: -counts[g])]
    skip_members = [gal.member_info(c) for c in skip_ids if gal.cls_prof[c] in groups]
    diag = dict(
        thr=thr, thr_source=thr_src,
        groups={gal.profiles[g].id: int(counts[g]) for g in range(len(gal.profiles))},
        faces=int(len(lab)),
        faces_small=int((lab == -2).sum()),
        faces_unknown=int((lab == -1).sum()),
        faces_partial_nonblocking=int((eff == PARTIAL).sum()),
        faces_by_member={gal.members[c]: int((lab == c).sum()) for c in range(len(gal.keys))},
        frames_with_unknown=int((unknown > 0).sum()),
        frames_skip_with_unknown=int(((state == SKIP) & (unknown > 0)).sum()),
        frames_no_member=int((state == NONE).sum()),
        timeline_segments=len(tl["segments"]),
    )
    return skips, highlights, dict(lab=lab, eff=eff, full=full, score=score, state=state,
                                   filled=np.array(res["debug"]["filled"]),
                                   skip=np.array(res["debug"]["skip"]),
                                   highlight=res["debug"]["highlight"], unknown=unknown,
                                   groups=found, skip_members=skip_members, timeline=tl), diag


# ---------------------------------------------------------------- skip list file
def make_list(vid, title, duration, skips, highlights, dbg, only_profiles=None):
    """Skip list (format 5): the presence timeline (with per-frame focus), from
    which the add-on works out each user's own skips, plus the skips and highlights for the profiles'
    default choices (read by add-on 0.5.0, and a check for the add-on's rules)."""
    out = dict(
        format=SKIPLIST_FORMAT, videoId=vid, title=title, duration=duration,
        scannedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        scannerVersion=SCANNER_VERSION,
        groups=dbg["groups"], skipMembers=dbg["skip_members"],
        skips=skips, highlights=highlights,
        timeline=dbg["timeline"],
    )
    if only_profiles:
        out["onlyProfiles"] = list(only_profiles)
    return out


def dump_list(out):
    """JSON with one timeline segment per line (readable diffs, small files)."""
    tl = out.get("timeline")
    if not tl:
        return json.dumps(out, ensure_ascii=False, indent=1)
    marker = "__SEGMENTS__"
    text = json.dumps({**out, "timeline": {**tl, "segments": marker}}, ensure_ascii=False, indent=1)
    segs = ",\n".join("   " + json.dumps(s, ensure_ascii=False, separators=(",", ":")) for s in tl["segments"])
    return text.replace(f'"{marker}"', "[\n" + segs + "\n  ]")


def save_list(out):
    SKIPLISTS.mkdir(parents=True, exist_ok=True)
    (SKIPLISTS / f"{out['videoId']}.json").write_text(dump_list(out), encoding="utf-8")
    update_index(SKIPLISTS)


def load_cached(vid):
    """Cached detections of a video: (times, rows, emb, kps, frame_size) or None."""
    f = CACHE / f"{vid}.faces.npz"
    if not f.exists():
        return None
    z = np.load(f)
    return z["times"], z["rows"], z["emb"], z["kps"], tuple(int(x) for x in z["frame_size"])


# ---------------------------------------------------------------- main
def scan(url, p, rescan=False, profile=None, debug=False, progress=None):
    """profile: None = every group, else profile id(s) to limit the scan to.
    progress: optional object with download_hook(d), detect_start(duration),
    frame(t), cached() (see scan_cli.py)."""
    gal = load_gallery(profile)
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
    skips, highlights, dbg, diag = build_segments(times, rows, emb, gal, p, kps, frame_size)
    T["classify_smooth"] = time.perf_counter() - tc
    T["total"] = time.perf_counter() - t0

    out = make_list(vid, info.get("title"), info.get("duration"), skips, highlights, dbg,
                    gal.ids if profile else None)
    save_list(out)
    # lets scan.bat / publish_skiplists.py know which list was just written
    (CACHE / "last_scan.json").write_text(json.dumps({"videoId": vid}), encoding="utf-8")
    return out, T, st, diag, (times, rows, emb, gal, dbg)


def fmt(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def build_parser(prog=None):
    ap = argparse.ArgumentParser(prog=prog)
    # nargs="+": a link split by the shell (e.g. cmd.exe breaks unquoted links at "=")
    # arrives as several pieces and is joined back together.
    ap.add_argument("url", nargs="+", help="YouTube link (any common form) or 11-char video ID")
    ap.add_argument("--profile", default=None,
                    help="only these groups (comma-separated profile ids); default: all groups")
    ap.add_argument("--debug", action="store_true", help="show full tracebacks")
    for k, v in DEFAULTS.items():
        ap.add_argument(f"--{k.replace('_', '-')}", type=type(v) if v is not None else float, default=v)
    ap.add_argument("--rescan", action="store_true")
    ap.add_argument("--no-cookies", action="store_true",
                    help="download without the browser's YouTube cookies (same as KMS_COOKIES=0)")
    return ap


def parse_link(pieces):
    """Video ID from link pieces (rejoined), or None."""
    raw = " ".join(pieces)
    return video_id(raw) or video_id(raw.replace(" ", "="))


def main():
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a = build_parser().parse_args()
    if a.no_cookies:
        os.environ["KMS_COOKIES"] = "0"
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
    print("groups:", ", ".join(f"{g['name']} ({g['faces']} faces)" for g in out["groups"]) or "none")
    print("skips:", ", ".join(f"{fmt(s['start'])}-{fmt(s['end'])}" for s in out["skips"]))
    for h in out["highlights"]:
        print(f"{h['name']}:", ", ".join(f"{fmt(s['start'])}-{fmt(s['end'])}" for s in h["ranges"]))


if __name__ == "__main__":
    main()

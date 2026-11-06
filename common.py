"""Shared helpers: profiles, face model loading, image IO."""
import json
import os
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE = ROOT / "cache"
SKIPLISTS = ROOT / "skiplists"
PROFILES = ROOT / "profiles"
DEFAULT_PROFILE = "hearts2hearts"


class Profile:
    """A group to scan for: profiles/<id>.json, optionally overridden by the
    gitignored profiles/<id>.local.json (e.g. a machine-specific photosDir) or
    the env var KMS_PHOTOS_<ID> (photo folder)."""

    def __init__(self, pid=DEFAULT_PROFILE):
        cfg = json.loads((PROFILES / f"{pid}.json").read_text(encoding="utf-8"))
        local = PROFILES / f"{pid}.local.json"
        if local.exists():
            cfg.update(json.loads(local.read_text(encoding="utf-8")))
        env = os.environ.get(f"KMS_PHOTOS_{pid.upper().replace('-', '_')}")
        if env:
            cfg["photosDir"] = env
        assert cfg["id"] == pid, f"profile id mismatch in {pid}.json"
        self.cfg = cfg
        self.id = pid
        self.group = cfg.get("group", pid)
        self.members = list(cfg["members"])
        self.names = {m: cfg.get("names", {}).get(m, m) for m in self.members}
        self.skip = list(cfg["skip"])
        self.highlight = list(cfg.get("highlight", []))
        for m in self.skip + self.highlight:
            assert m in self.members, f"{m} is not a member of {pid}"
        self.photos_dir = Path(os.path.expanduser(cfg.get("photosDir", "")))
        # optional: more photo folders per member, {"<member>": ["<folder>", ...]}
        # (machine-specific ones belong in <id>.local.json)
        self.extra_dirs = {m: [Path(os.path.expanduser(d)) for d in ds]
                           for m, ds in cfg.get("extraPhotoDirs", {}).items()}
        for m in self.extra_dirs:
            assert m in self.members, f"{m} is not a member of {pid}"
        # optional: {"<member>": "YYYY-MM-DD"}, leave out that member's older photos
        # (e.g. childhood photos that no longer look like her)
        self.since = dict(cfg.get("photosSince", {}))
        self.use_all = set(cfg.get("usePhotosOf", []))
        self.per_member = int(cfg.get("photosPerMember", 2000))
        # optional: sample this share of photosPerMember from photos dated (by the
        # YYYY-MM-DD filename prefix) on/after recentSince, the rest from older ones
        self.recent_since = cfg.get("recentSince")
        self.recent_share = float(cfg.get("recentShare", 0.6))
        self.data = DATA / pid

    def idx(self, members):
        return [self.members.index(m) for m in members]

    def summary(self):
        """Public, machine-independent description (for skip-list indexes / the extension)."""
        return {"id": self.id, "group": self.group,
                "skip": [self.names[m] for m in self.skip],
                "highlight": [self.names[m] for m in self.highlight],
                # every member with the profile's choice, the default for each add-on user
                "members": [{"id": m, "name": self.names[m], "default": self.choice(m)} for m in self.members]}

    def choice(self, m):
        return "skip" if m in self.skip else "highlight" if m in self.highlight else "normal"


def all_profiles():
    return sorted(p.stem for p in PROFILES.glob("*.json") if not p.stem.endswith(".local"))


GALLERY_INFO = DATA / "gallery.json"  # joint "unknown" threshold, written by build_refs.py


class Gallery:
    """All members of all profiles (that have data/<id>/refs.npz) as one set of
    classes, so a video is classified without picking a group first.

    Class c is member members[c] of profiles[cls_prof[c]]; keys[c] = "<profile>:<member>".
    `only` restricts it to some profiles (scan.py --profile). The "unknown"
    threshold is the joint one from data/gallery.json when it was computed for
    exactly these profiles, the profile's own one for a single profile, else the
    highest per-profile one."""

    def __init__(self, only=None):
        ids = list(only) if only else all_profiles()
        self.profiles = [Profile(pid) for pid in ids if (DATA / pid / "refs.npz").exists() or only]
        if not self.profiles:
            raise FileNotFoundError("no data/<profile>/refs.npz found; run build_refs.py first")
        embs, labs, self.keys, self.members, self.cls_prof = [], [], [], [], []
        own_thr = []
        for pi, prof in enumerate(self.profiles):
            f = prof.data / "refs.npz"
            if not f.exists():
                raise FileNotFoundError(f"{f} is missing; run build_refs.py --profile {prof.id}")
            z = np.load(f)
            assert list(z["members"]) == prof.members,                 f"{f} members differ from the profile; rerun build_refs.py --profile {prof.id}"
            embs.append(z["emb"].astype(np.float32))
            labs.append(z["labels"].astype(int) + len(self.keys))
            for m in prof.members:
                self.keys.append(f"{prof.id}:{m}")
                self.members.append(m)
                self.cls_prof.append(pi)
            own_thr.append((float(z["unknown_thr_knn"]), float(z["unknown_thr_centroid"])))
        self.emb = np.concatenate(embs)
        self.labels = np.concatenate(labs)
        self.cls_prof = np.array(self.cls_prof)
        self.centroids = np.stack([l2n(self.emb[self.labels == c].mean(0)) for c in range(len(self.keys))])
        self.own_thr = own_thr
        self.thr_source = "joint"
        info = json.loads(GALLERY_INFO.read_text(encoding="utf-8")) if GALLERY_INFO.exists() else {}
        if len(self.profiles) == 1:
            self.thr_knn, self.thr_centroid = own_thr[0]
            self.thr_source = "profile"
        elif sorted(info.get("profiles", [])) == sorted(self.ids):
            self.thr_knn, self.thr_centroid = info["thr_knn"], info["thr_centroid"]
        else:
            self.thr_knn = max(t[0] for t in own_thr)
            self.thr_centroid = max(t[1] for t in own_thr)
            self.thr_source = "max of profiles (run build_refs.py --joint)"

    def thr_for(self, groups, method):
        """("unknown" threshold, where it comes from) once the faces are matched
        only against the members of `groups` (profile indexes): one group -> its
        own threshold from build_refs, all groups -> the joint one, else the
        highest of theirs."""
        groups = sorted(set(groups))
        k = 0 if method == "knn" else 1
        if len(groups) == 1:
            return self.own_thr[groups[0]][k], f"{self.profiles[groups[0]].id}"
        if not groups or len(groups) == len(self.profiles):
            return getattr(self, f"thr_{method}"), self.thr_source
        return max(self.own_thr[g][k] for g in groups), "max of " + "+".join(self.profiles[g].id for g in groups)

    @property
    def ids(self):
        return [p.id for p in self.profiles]

    def prof(self, c):
        return self.profiles[self.cls_prof[c]]

    def name(self, c):
        return self.prof(c).names[self.members[c]]

    def classes(self, kind):
        """Class indexes of every profile's skip ("skip") or highlight ("highlight") members."""
        return [c for c in range(len(self.keys)) if self.members[c] in getattr(self.prof(c), kind)]

    def member_info(self, c):
        p = self.prof(c)
        return {"member": self.members[c], "name": p.names[self.members[c]],
                "group": p.group, "profile": p.id}


def update_index(folder):
    """Rewrite <folder>/index.json from the per-video skip lists in it."""
    ID_LEN = 11
    videos = {}
    for f in sorted(Path(folder).glob("*.json")):
        if f.name == "index.json" or len(f.stem) != ID_LEN:
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        videos[f.stem] = {"title": d.get("title"), "duration": d.get("duration"),
                          "groups": [g["name"] for g in d.get("groups", [])],
                          "scannedAt": d.get("scannedAt"), "scannerVersion": d.get("scannerVersion"),
                          "skips": len(d.get("skips", []))}
    idx = {"format": 3, "videos": videos}
    (Path(folder) / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1) + "\n",
                                             encoding="utf-8")
    return idx


def load_face_app(det_size=640, det_thresh=0.5):
    """InsightFace buffalo_l (RetinaFace det_10g + ArcFace w600k_r50) on CUDA."""
    import onnxruntime as ort
    try:
        ort.preload_dlls()  # pip-installed CUDA/cuDNN DLLs
    except Exception:
        pass
    from insightface.app import FaceAnalysis

    app = FaceAnalysis(
        name="buffalo_l",
        allowed_modules=["detection", "recognition"],
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    app.prepare(ctx_id=0, det_size=(det_size, det_size), det_thresh=det_thresh)
    return app


def imread(path, max_side=1280):
    """Unicode-safe image read (cv2.imread fails on non-ASCII Windows paths)."""
    import cv2

    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        img = None
    if img is None:
        try:
            from PIL import Image

            with Image.open(path) as im:
                img = cv2.cvtColor(np.asarray(im.convert("RGB")), cv2.COLOR_RGB2BGR)
        except Exception:
            return None
    h, w = img.shape[:2]
    s = max_side / max(h, w)
    if s < 1:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    return img


def l2n(x, axis=-1):
    return x / (np.linalg.norm(x, axis=axis, keepdims=True) + 1e-9)

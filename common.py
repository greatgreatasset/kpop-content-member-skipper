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
        self.use_all = set(cfg.get("usePhotosOf", []))
        self.per_member = int(cfg.get("photosPerMember", 2000))
        self.data = DATA / pid
        self.skiplists = SKIPLISTS / pid

    def idx(self, members):
        return [self.members.index(m) for m in members]

    def summary(self):
        """Public, machine-independent description (for skip-list indexes / the extension)."""
        return {"id": self.id, "group": self.group,
                "skip": [self.names[m] for m in self.skip],
                "highlight": [self.names[m] for m in self.highlight]}


def all_profiles():
    return sorted(p.stem for p in PROFILES.glob("*.json") if not p.stem.endswith(".local"))


def update_index(folder, prof):
    """Rewrite <folder>/index.json from the skip lists in it."""
    ID_LEN = 11
    videos = {}
    for f in sorted(Path(folder).glob("*.json")):
        if f.name == "index.json" or len(f.stem) != ID_LEN:
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        videos[f.stem] = {"title": d.get("title"), "duration": d.get("duration"),
                          "scannedAt": d.get("scannedAt"), "scannerVersion": d.get("scannerVersion"),
                          "skips": len(d.get("skips", []))}
    idx = {**prof.summary(), "videos": videos}
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

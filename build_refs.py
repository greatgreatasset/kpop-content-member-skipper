"""Build the reference face-embedding set from the member photo folders.

    python build_refs.py [--profile hearts2hearts] [--photos DIR] [--per-member N] [--all MEMBER ...]
    python build_refs.py --joint      only redo step 5 (all profiles' refs.npz -> data/gallery.json)

Members, photo folder and sampling come from profiles/<profile>.json.
Phases (embeddings are cached per member in data/<profile>/emb_<member>.npz, so reruns
only redo the cheap clean/validate steps):
  1. embed   - sample images, keep one clear/dominant face, ArcFace embedding
  2. clean   - drop embeddings far from the member centroid (wrong person / bad crop)
  3. validate- hold out ~10% grouped by shoot date, nearest-centroid vs kNN,
               confusion matrix, impostor scores -> "unknown" threshold
  4. save    - data/<profile>/refs.npz
  5. joint   - the same holdout check on all profiles together (scan.py classifies
               against every member of every group), most similar members across
               groups, joint "unknown" threshold -> data/gallery.json
"""
import argparse
import random
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

import json

from common import DATA, DEFAULT_PROFILE, GALLERY_INFO, Profile, all_profiles, imread, l2n, load_face_app

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MIN_FACE = 48          # px, short side of face box in the (<=1280px) image
MIN_DET = 0.6
DOMINANCE = 2.0        # largest face area must be >= 2x the second largest
KNN_K = 5


def list_images(folder):
    return sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in IMG_EXT)


def sample_paths(paths, n, rng, recent_since=None, recent_share=0.6):
    """n of paths; with recent_since ("YYYY-MM-DD"), recent_share of them from
    photos whose filename date is on/after it (looks change over time), the rest
    spread over the older ones."""
    if not recent_since:
        return sorted(rng.sample(paths, n))
    cut = recent_since.replace("-", "")
    recent = [p for p in paths if shoot_key(p.name)[:8] >= cut and shoot_key(p.name)[:8].isdigit()]
    rs = set(recent)
    older = [p for p in paths if p not in rs]
    n_rec = min(len(recent), round(n * recent_share))
    n_old = min(len(older), n - n_rec)
    n_rec = min(len(recent), n - n_old)
    return sorted(rng.sample(recent, n_rec) + rng.sample(older, n_old))


def pick_face(faces):
    """Return the single clear/dominant face, or None + reason."""
    if not faces:
        return None, "noface"
    areas = [(f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]) for f in faces]
    order = np.argsort(areas)[::-1]
    f = faces[order[0]]
    if len(faces) > 1 and areas[order[0]] < DOMINANCE * areas[order[1]]:
        return None, "multi"
    w, h = f.bbox[2] - f.bbox[0], f.bbox[3] - f.bbox[1]
    if min(w, h) < MIN_FACE:
        return None, "small"
    if f.det_score < MIN_DET:
        return None, "lowdet"
    return f, "ok"


def embed_member(app, member, paths, outdir):
    out = outdir / f"emb_{member}.npz"
    names = [p.name for p in paths]
    if out.exists():
        z = np.load(out, allow_pickle=True)
        if list(z["sampled"]) == names:
            print(f"  {member}: cached ({len(z['paths'])} kept)")
            return z["emb"], list(z["paths"])
    stats = defaultdict(int)
    embs, kept = [], []
    t0 = time.time()
    with ThreadPoolExecutor(12) as pool:
        for i, (p, img) in enumerate(zip(paths, pool.map(imread, paths))):
            if img is None:
                stats["unreadable"] += 1
                continue
            f, why = pick_face(app.get(img))
            stats[why] += 1
            if f is not None:
                embs.append(f.normed_embedding.astype(np.float32))
                kept.append(p.name)
            if (i + 1) % 500 == 0:
                print(f"    {member} {i+1}/{len(paths)}  {(i+1)/(time.time()-t0):.1f} img/s")
    emb = np.stack(embs)
    np.savez(out, emb=emb, paths=np.array(kept), sampled=np.array(names))
    print(f"  {member}: {len(paths)} sampled -> {dict(stats)}  ({time.time()-t0:.0f}s)")
    return emb, kept


def clean(emb, abs_min=0.30, n_mad=3.0, iters=3):
    """Iteratively drop embeddings with low cosine similarity to the centroid."""
    keep = np.ones(len(emb), bool)
    for _ in range(iters):
        c = l2n(emb[keep].mean(0))
        s = emb @ c
        med = np.median(s[keep])
        mad = np.median(np.abs(s[keep] - med)) * 1.4826
        thr = max(abs_min, med - n_mad * mad)
        keep = s >= thr
    return keep, thr, s


def shoot_key(name):
    """Group photos by shoot date so holdout faces come from different events."""
    m = re.match(r"(\d{4}-?\d{2}-?\d{2})", name)
    return m.group(1).replace("-", "") if m else name


def class_scores(q, ref_emb, ref_lab, n_cls, method):
    """(n_q, n_cls) similarity of each query to each class."""
    sims = q @ ref_emb.T
    out = np.full((len(q), n_cls), -1.0, np.float32)
    for c in range(n_cls):
        sc = sims[:, ref_lab == c]
        if method == "centroid":
            out[:, c] = q @ l2n(ref_emb[ref_lab == c].mean(0))
        else:  # mean of top-k nearest within class
            k = min(KNN_K, sc.shape[1])
            out[:, c] = -np.sort(-sc, axis=1)[:, :k].mean(1)
    return out


def validate(emb, lab, groups, members, rng_seed=0):
    MEMBERS = members
    n_cls = len(MEMBERS)
    rng = random.Random(rng_seed)
    test = np.zeros(len(lab), bool)
    for c in range(n_cls):
        g = sorted(set(groups[lab == c]))
        rng.shuffle(g)
        target, n = 0.10 * (lab == c).sum(), 0
        chosen = set()
        for gg in g:
            if n >= target:
                break
            chosen.add(gg)
            n += ((groups == gg) & (lab == c)).sum()
        test |= (lab == c) & np.isin(groups, list(chosen))
    tr, te = ~test, test
    print(f"\nholdout: {te.sum()} test / {tr.sum()} train faces (split by shoot date)")
    res = {}
    for method in ("centroid", "knn"):
        S = class_scores(emb[te], emb[tr], lab[tr], n_cls, method)
        pred = S.argmax(1)
        y = lab[te]
        acc = (pred == y).mean()
        print(f"\n== {method}: overall accuracy {acc:.4f}")
        cm = np.zeros((n_cls, n_cls), int)
        np.add.at(cm, (y, pred), 1)
        w = 8
        print(" " * w + "".join(f"{m[:6]:>7}" for m in MEMBERS) + "   acc")
        for c in range(n_cls):
            print(f"{MEMBERS[c]:<{w}}" + "".join(f"{v:>7}" for v in cm[c]) + f"  {cm[c,c]/cm[c].sum():.3f}")
        # genuine score (own class) vs impostor score (best other class, as if the
        # true person were not enrolled -> stand-in for backup dancers / crew)
        gen = S[np.arange(len(y)), y]
        S2 = S.copy()
        S2[np.arange(len(y)), y] = -1
        imp = S2.max(1)
        print("threshold  genuine-accepted  impostor-accepted")
        for t in (0.25, 0.30, 0.35, 0.40, 0.45, 0.50):
            print(f"   {t:.2f}        {(gen >= t).mean():.3f}             {(imp >= t).mean():.4f}")
        t99 = float(np.quantile(imp, 0.99))
        print(f"impostor 99th pct = {t99:.3f}; genuine median = {np.median(gen):.3f}")
        res[method] = dict(acc=float(acc), cm=cm, t99=t99)
    return res


def joint():
    """Validate all profiles' reference sets as one gallery; save the joint threshold."""
    ids = [pid for pid in all_profiles() if (DATA / pid / "refs.npz").exists()]
    embs, labs, grps, names, keys = [], [], [], [], []
    for pid in ids:
        z = np.load(DATA / pid / "refs.npz")
        embs.append(z["emb"].astype(np.float32))
        labs.append(z["labels"].astype(int) + len(keys))
        grps += [f"{pid}:{p.split('/')[0]}:{shoot_key(p.split('/', 1)[1])}" for p in z["paths"]]
        names += list(z["members"])
        keys += [f"{pid}:{m}" for m in z["members"]]
    emb, lab, grp = np.concatenate(embs), np.concatenate(labs), np.array(grps)
    prof_of = np.array([k.split(":")[0] for k in keys])
    print(f"\n== joint gallery: {', '.join(ids)} ({len(keys)} members, {len(emb)} embeddings)")
    cents = np.stack([l2n(emb[lab == c].mean(0)) for c in range(len(keys))])
    sim = cents @ cents.T
    pairs = sorted(((sim[a, b], keys[a], keys[b]) for a in range(len(keys)) for b in range(a + 1, len(keys))
                    if prof_of[a] != prof_of[b]), reverse=True)
    print("most similar members across groups (centroid cosine):")
    for v, a, b in pairs[:8]:
        print(f"  {v:.3f}  {a} ~ {b}")
    res = validate(emb, lab, grp, names)
    # how often a held-out face is given a member of another group
    if len(ids) > 1:
        info = {"profiles": ids, "thr_knn": res["knn"]["t99"], "thr_centroid": res["centroid"]["t99"],
                "accuracy_knn": res["knn"]["acc"]}
        cm = res["knn"]["cm"]
        cross = sum(cm[a, b] for a in range(len(keys)) for b in range(len(keys)) if prof_of[a] != prof_of[b])
        print(f"\nknn: {cross}/{cm.sum()} held-out faces matched to a member of another group "
              f"({100 * cross / cm.sum():.2f}%)")
        GALLERY_INFO.write_text(json.dumps(info, indent=1) + "\n", encoding="utf-8")
        print(f"saved {GALLERY_INFO}: joint knn threshold {info['thr_knn']:.3f}")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--joint", action="store_true", help="only validate all profiles together")
    ap.add_argument("--photos", help="photo folder (default: the profile's photosDir)")
    ap.add_argument("--per-member", type=int, help="photos sampled per member")
    ap.add_argument("--all", nargs="*", help="members to use every photo of")
    args = ap.parse_args()
    if args.joint:
        joint()
        return
    prof = Profile(args.profile)
    MEMBERS = prof.members
    photos = Path(args.photos) if args.photos else prof.photos_dir
    per_member = args.per_member or prof.per_member
    use_all = set(args.all) if args.all is not None else prof.use_all
    prof.data.mkdir(parents=True, exist_ok=True)
    print(f"profile {prof.id}: {len(MEMBERS)} members, photos from {photos}")

    app = load_face_app()
    all_emb, all_lab, all_grp, all_paths = [], [], [], []
    print("== embed")
    for c, m in enumerate(MEMBERS):
        paths = list_images(photos / m)
        if m not in use_all and len(paths) > per_member:
            paths = sample_paths(paths, per_member, random.Random(c), prof.recent_since, prof.recent_share)
        emb, kept = embed_member(app, m, paths, prof.data)

        keep, thr, s = clean(emb)
        print(f"  {m}: clean thr={thr:.3f} kept {keep.sum()}/{len(keep)} "
              f"(sim median {np.median(s):.3f})")
        emb = emb[keep]
        kept = [k for k, ok in zip(kept, keep) if ok]
        all_emb.append(emb)
        all_lab.append(np.full(len(emb), c))
        all_grp += [f"{m}:{shoot_key(k)}" for k in kept]
        all_paths += [f"{m}/{k}" for k in kept]

    emb = np.concatenate(all_emb)
    lab = np.concatenate(all_lab)
    grp = np.array(all_grp)

    # cross-member centroid similarity: which members look alike to ArcFace
    cents = np.stack([l2n(emb[lab == c].mean(0)) for c in range(len(MEMBERS))])
    print("\ncentroid cosine similarity between members:")
    print(" " * 8 + "".join(f"{m[:6]:>7}" for m in MEMBERS))
    for c in range(len(MEMBERS)):
        print(f"{MEMBERS[c]:<8}" + "".join(f"{v:>7.2f}" for v in cents[c] @ cents.T))

    res = validate(emb, lab, grp, MEMBERS)
    np.savez_compressed(
        prof.data / "refs.npz", profile=prof.id,
        emb=emb.astype(np.float16), labels=lab.astype(np.int8),
        members=np.array(MEMBERS), centroids=cents.astype(np.float32),
        paths=np.array(all_paths),
        unknown_thr_knn=res["knn"]["t99"], unknown_thr_centroid=res["centroid"]["t99"],
    )
    print(f"\nsaved {prof.data / 'refs.npz'}: {len(emb)} embeddings")


if __name__ == "__main__":
    main()

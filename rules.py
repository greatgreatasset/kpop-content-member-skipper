"""Skip / highlight rules applied to a video's presence timeline.

The same rules live in extension/rules.js (the add-on computes each user's own
skips from the timeline in the skip list); tests/rules_test.js checks that both
give the same result for every list in skiplists/. Change them together.

Timeline (skip list format 5, "timeline"; format 4 is the same without
"focusRule" and the segments' "focus"):
    {"fps": 3, "frames": 5015,
     "members": [{"key": "hearts2hearts:carmen", "member": "carmen", "name": "Carmen",
                  "group": "Hearts2Hearts", "profile": "hearts2hearts", "default": "skip"}, ...],
     "rules": {"fillGap": 3.0, "median": 5, "mergeGap": 1.0, "minSkip": 2.0,
               "edgeTrim": 0.5, "minTrimmed": 1.5, "hlMedian": 3, "hlMergeGap": 2.0, "hlMin": 1.0},
     "focusRule": {"minRatio": 1.5, "maxOffCenter": 0.3},
     "segments": [{"start": 4.33, "end": 5.0, "members": ["hearts2hearts:carmen"],
                   "partial": ["hearts2hearts:ian"], "focus": "hearts2hearts:carmen"},
                  {"start": 5.0, "end": 6.33, "state": "wide"}, ...]}

Segments cover every frame, back to back. Per segment:
  members  recognized foreground members whose face is fully shown
  partial  recognized foreground members whose face is cut off by the frame edge
  focus    the member who is the main subject of the frame, if any: the largest
           face is hers, it is at least minRatio x the area of the next largest
           face, and its centre is at most maxOffCenter x the frame width from
           the middle
  state   only when nobody is recognized: "wide" (all faces tiny: a wide group
           shot), "unknown" (only faces of people who aren't members), "none" (no face)

Rules for a set of skip-members S (a user's choices):
  1. A frame is SKIP when someone counts (a fully shown member, or a partly
     shown member of S) and every fully shown member is in S; KEEP when a
     fully shown member is not in S, or for a "wide" frame; else nobody (NONE).
     Partly shown members of S count (it is still her part); partly shown
     others don't block a skip.
  2. NONE runs up to fillGap s take over their neighbours (split in the
     middle); longer ones become KEEP.
  3. Median filter (median frames) on SKIP, gap merge (< mergeGap s), minimum
     length (minSkip s), then each skip is trimmed edgeTrim s inward at both
     ends and dropped if under minTrimmed s.
Highlights of a member: the frames she is recognized in (fully or partly),
median filter (hlMedian), merge (< hlMergeGap s), at least hlMin s, minus the
skips. With hl_when="focus" ("highlight only when she's the main focus") the
frames she is the focus of are used instead, with the same steps; a timeline
without focus info (format 4) falls back to "any". Skips never depend on it.
"""

SKIP, KEEP, NONE = 1, 0, -1
# Lengths and gaps are whole frames, so they often equal a limit exactly
# (a 2.0 s gap, a 1.0 s part); compare with this margin so such ties come out the
# same here and in rules.js: "< 2 s" excludes exactly 2 s, ">= 1 s" includes 1 s.
EPS = 1e-6
CHOICES = ("skip", "normal", "highlight")
HL_WHEN = ("any", "focus")  # highlight any time she's on screen / only when she's the main focus


def has_focus(tl):
    return isinstance(tl.get("focusRule"), dict)


def expand(tl):
    """Per frame: (fully shown member keys, partly shown keys, state, focus key or None)."""
    fps = tl["fps"]
    n = int(tl["frames"])
    full = [frozenset()] * n
    part = [frozenset()] * n
    state = ["none"] * n
    focus = [None] * n
    for s in tl["segments"]:
        a, b = int(round(s["start"] * fps)), min(n, int(round(s["end"] * fps)))
        f, p = frozenset(s.get("members", ())), frozenset(s.get("partial", ()))
        st = s.get("state", "") if not (f or p) else ""
        fo = s.get("focus")
        for i in range(a, b):
            full[i], part[i], state[i], focus[i] = f, p, st, fo
    return full, part, state, focus


def frame_states(full, part, state, skip):
    out = []
    for f, p, st in zip(full, part, state):
        if f or (p & skip):
            out.append(SKIP if f <= skip else KEEP)
        elif st == "wide":
            out.append(KEEP)
        else:
            out.append(NONE)
    return out


def fill_gaps(state, max_gap):
    """NONE runs <= max_gap frames inherit neighbours (split at the midpoint if
    they disagree); longer runs, and longer runs at the ends, become KEEP."""
    s = list(state)
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
            s[i:mid] = [left] * (mid - i)
            s[mid:j] = [right] * (j - mid)
        elif (j - i) <= max_gap and (left != NONE or right != NONE):
            s[i:j] = [left if left != NONE else right] * (j - i)
        else:
            s[i:j] = [KEEP] * (j - i)
        i = j
    return s


def median_filter(x, w):
    """Median of booleans over a window of w frames (edges repeated)."""
    x = [bool(v) for v in x]
    if w <= 1 or not x:
        return x
    pad = w // 2
    xp = [x[0]] * pad + x + [x[-1]] * pad
    out, c = [], sum(xp[:w])
    for i in range(len(x)):
        if i:
            c += xp[i + w - 1] - xp[i - 1]
        # median >= 0.5: more than half true (odd w), at least half (even w)
        out.append(2 * c > w if w % 2 else 2 * c >= w)
    return out


def r2(x):
    return round(x + 0.0, 2)


def to_segments(mask, fps, merge_gap, min_len, trim=0.0, min_trimmed=0.0):
    dt = 1.0 / fps
    segs, i, n = [], 0, len(mask)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < n and mask[j]:
            j += 1
        segs.append([i / fps, (j - 1) / fps + dt])
        i = j
    merged = []
    for s in segs:
        if merged and s[0] - merged[-1][1] < merge_gap - EPS:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    merged = [s for s in merged if s[1] - s[0] >= min_len - EPS]
    if trim:
        merged = [[a + trim, b - trim] for a, b in merged if (b - a) - 2 * trim >= min_trimmed - EPS]
    return [{"start": r2(a), "end": r2(b)} for a, b in merged]


def subtract(segs, cut):
    """Remove the `cut` ranges from `segs` (pieces under 0.5 s are dropped)."""
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
        out += [{"start": r2(a), "end": r2(b)} for a, b in pieces if b - a >= 0.5 - EPS]
    return out


def default_choices(tl):
    return {m["key"]: m.get("default", "normal") for m in tl["members"]}


def apply(tl, choices=None, debug=False, hl_when="any"):
    """Skips and highlights for these choices ({member key: "skip" | "normal" |
    "highlight"}; members not in it keep their default). hl_when: "any" or
    "focus" (see above).

    -> {"skips": [{start, end}], "highlights": [{"key", "ranges"}]} (+ "debug")"""
    ch = default_choices(tl)
    ch.update(choices or {})
    R = tl["rules"]
    fps = tl["fps"]
    skip = frozenset(k for k, v in ch.items() if v == "skip")
    full, part, state, focus = expand(tl)
    by_focus = hl_when == "focus" and has_focus(tl)
    st = frame_states(full, part, state, skip)
    filled = fill_gaps(st, int(round(R["fillGap"] * fps)))
    mask = median_filter([s == SKIP for s in filled], R["median"])
    skips = to_segments(mask, fps, R["mergeGap"], R["minSkip"], R["edgeTrim"], R["minTrimmed"])
    highlights, hl_masks = [], {}
    for m in tl["members"]:
        k = m["key"]
        if ch.get(k) != "highlight":
            continue
        on = [fo == k for fo in focus] if by_focus else [k in f or k in p for f, p in zip(full, part)]
        hm = median_filter(on, R["hlMedian"])
        hl_masks[k] = hm
        rng = subtract(to_segments(hm, fps, R["hlMergeGap"], R["hlMin"]), skips)
        rng = [s for s in rng if s["end"] - s["start"] >= R["hlMin"] - EPS]
        if rng:
            highlights.append({"key": k, "ranges": rng})
    out = {"skips": skips, "highlights": highlights}
    if debug:
        out["debug"] = {"state": st, "filled": filled, "skip": mask, "highlight": hl_masks}
    return out

// Face boxes of skip list format 6 (timeline.faces) for "Blur skipped members
// when they can't be skipped": decoding and where each box is at a given time.
// The same as blur.py (decode_tracks / box_at); tests/blur_crosscheck.py checks
// both agree. See blur.py for the format.
//
// Loaded as a content script (before content.js) and by node in the tests
// (module.exports).
"use strict";

(function (root) {
  const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  const IDX = {};
  for (let i = 0; i < B64.length; i++) IDX[B64[i]] = i;
  const Q = 4095;
  const FADE = 0.15; // s, fade in / out at track ends that aren't a scene cut
  const EPS = 1e-6;

  const valid = (f) => !!f && typeof f === "object" && f.fps > 0 && Array.isArray(f.cuts) &&
    f.tracks && typeof f.tracks === "object";

  // first index i with a[i] > x (a sorted)
  function upper(a, x) {
    let lo = 0, hi = a.length;
    while (lo < hi) {
      const m = (lo + hi) >> 1;
      if (a[m] > x) hi = m;
      else lo = m + 1;
    }
    return lo;
  }

  // [first frame, samples] -> {t: [s...], b: [x, y, w, h, ...]} or null if malformed
  function decTrack(tr, fps) {
    if (!Array.isArray(tr) || typeof tr[1] !== "string" || tr[1].length % 9) return null;
    let f = Number(tr[0]);
    const s = tr[1], n = s.length / 9;
    const t = new Float64Array(n), b = new Float32Array(4 * n);
    for (let i = 0; i < n; i++) {
      const o = 9 * i;
      const d = IDX[s[o]];
      if (d === undefined) return null;
      f += d;
      t[i] = f / fps;
      for (let k = 0; k < 4; k++) {
        const hi = IDX[s[o + 1 + 2 * k]], lo = IDX[s[o + 2 + 2 * k]];
        if (hi === undefined || lo === undefined) return null;
        b[4 * i + k] = ((hi << 6) | lo) / Q;
      }
    }
    return { t, b };
  }

  // A track is shown `hold` s before its first sample to `hold` s after its
  // last, but never across a scene cut; it fades in / out only at ends that
  // aren't a cut.
  function span(t0, t1, cuts, hold) {
    const i = upper(cuts, t0 + EPS);
    const lo = i > 0 ? cuts[i - 1] : -1e9;
    const j = upper(cuts, t1 + EPS);
    const hi = j < cuts.length ? cuts[j] : 1e9;
    const start = Math.max(lo, t0 - hold), end = Math.min(hi, t1 + hold);
    return { start, end, fin: start > lo, fout: end < hi };
  }

  // timeline.faces -> {tracks: {key: [track]}, ...}; track = {key, t, b, start, end, fin, fout}
  function decode(faces) {
    if (!valid(faces)) return null;
    const fps = faces.fps;
    const hold = Number(faces.hold) || 0;
    const cuts = faces.cuts.map((c) => c / fps);
    const tracks = {};
    for (const [key, list] of Object.entries(faces.tracks)) {
      if (!Array.isArray(list)) continue;
      const out = [];
      for (const tr of list) {
        const d = decTrack(tr, fps);
        if (!d || !d.t.length) continue;
        out.push({ key, ...d, ...span(d.t[0], d.t[d.t.length - 1], cuts, hold) });
      }
      tracks[key] = out;
    }
    return { fps, hold, cuts, tracks };
  }

  // {x, y, w, h, op} of a decoded track at time t, or null when not shown
  function boxAt(tr, t, fade = FADE) {
    if (t < tr.start || t >= tr.end) return null;
    const { t: ts, b } = tr;
    const n = ts.length;
    let x, y, w, h;
    if (t <= ts[0] || n === 1) {
      [x, y, w, h] = [b[0], b[1], b[2], b[3]];
    } else if (t >= ts[n - 1]) {
      const o = 4 * (n - 1);
      [x, y, w, h] = [b[o], b[o + 1], b[o + 2], b[o + 3]];
    } else {
      const k = upper(ts, t) - 1;
      const f = (t - ts[k]) / (ts[k + 1] - ts[k]);
      const o = 4 * k, p = o + 4;
      x = b[o] + (b[p] - b[o]) * f;
      y = b[o + 1] + (b[p + 1] - b[o + 1]) * f;
      w = b[o + 2] + (b[p + 2] - b[o + 2]) * f;
      h = b[o + 3] + (b[p + 3] - b[o + 3]) * f;
    }
    let op = 1;
    if (tr.fin && fade) op = Math.min(op, (t - tr.start) / fade);
    if (tr.fout && fade) op = Math.min(op, (tr.end - t) / fade);
    return { x, y, w, h, op: Math.max(0, Math.min(1, op)) };
  }

  // The tracks of some members, indexed per second for quick lookups.
  function index(dec, keys) {
    const list = [];
    for (const k of keys) for (const tr of dec.tracks[k] || []) list.push(tr);
    const buckets = new Map();
    for (const tr of list) {
      const a = Math.floor(Math.max(0, tr.start)), z = Math.floor(Math.max(0, tr.end));
      for (let s = a; s <= z; s++) {
        let arr = buckets.get(s);
        if (!arr) buckets.set(s, (arr = []));
        arr.push(tr);
      }
    }
    return { buckets, count: list.length };
  }

  // [{key, x, y, w, h, op}] of the indexed tracks at time t
  function boxesAt(idx, t, fade = FADE) {
    const out = [];
    for (const tr of idx.buckets.get(Math.floor(t)) || []) {
      const r = boxAt(tr, t, fade);
      if (r && r.op > 0) out.push({ key: tr.key, ...r });
    }
    return out;
  }

  const api = { decode, decTrack, span, boxAt, index, boxesAt, valid, FADE };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.KmsBlur = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

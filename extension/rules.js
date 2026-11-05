// Skip / highlight rules applied to a video's presence timeline (skip list
// format 5, or 4 without focus). The same rules are in rules.py (the scanner's default skips);
// tests/rules_test.js checks both give the same result for every skip list.
// Change them together. See rules.py for the timeline format and the rules.
//
// Loaded as a content script (before content.js), by the popup and settings
// pages (globalThis.KmsRules) and by node in the test (module.exports).
"use strict";

(function (root) {
  const SKIP = 1, KEEP = 0, NONE = -1;
  const EPS = 1e-6; // see rules.py: ties at a limit come out the same in both
  const CHOICES = ["skip", "normal", "highlight"];
  // highlight any time she's on screen (default) / only when she's the main focus
  const HL_WHEN = ["any", "focus"];
  const hasFocus = (tl) => !!tl && !!tl.focusRule && typeof tl.focusRule === "object";

  const r2 = (x) => Math.round(x * 100) / 100;

  function expand(tl) {
    const fps = tl.fps;
    const n = Math.floor(tl.frames);
    const empty = new Set();
    const full = new Array(n).fill(empty);
    const part = new Array(n).fill(empty);
    const state = new Array(n).fill("none");
    const focus = new Array(n).fill(null);
    for (const s of tl.segments || []) {
      const a = Math.round(s.start * fps), b = Math.min(n, Math.round(s.end * fps));
      const f = new Set(s.members || []), p = new Set(s.partial || []);
      const st = f.size || p.size ? "" : s.state || "";
      const fo = s.focus || null;
      for (let i = Math.max(0, a); i < b; i++) {
        full[i] = f;
        part[i] = p;
        state[i] = st;
        focus[i] = fo;
      }
    }
    return { full, part, state, focus };
  }

  function frameStates(full, part, state, skip) {
    const out = new Array(full.length);
    for (let i = 0; i < full.length; i++) {
      const f = full[i];
      let partSkip = false;
      for (const k of part[i]) if (skip.has(k)) partSkip = true;
      if (f.size || partSkip) {
        let all = true;
        for (const k of f) if (!skip.has(k)) all = false;
        out[i] = all ? SKIP : KEEP;
      } else {
        out[i] = state[i] === "wide" ? KEEP : NONE;
      }
    }
    return out;
  }

  function fillGaps(state, maxGap) {
    const s = state.slice();
    const n = s.length;
    let i = 0;
    while (i < n) {
      if (s[i] !== NONE) {
        i++;
        continue;
      }
      let j = i;
      while (j < n && s[j] === NONE) j++;
      const left = i > 0 ? state[i - 1] : NONE;
      const right = j < n ? state[j] : NONE;
      if (j - i <= maxGap && left !== NONE && right !== NONE) {
        const mid = Math.floor((i + j) / 2);
        s.fill(left, i, mid);
        s.fill(right, mid, j);
      } else if (j - i <= maxGap && (left !== NONE || right !== NONE)) {
        s.fill(left !== NONE ? left : right, i, j);
      } else {
        s.fill(KEEP, i, j);
      }
      i = j;
    }
    return s;
  }

  function medianFilter(x, w) {
    const n = x.length;
    if (w <= 1 || !n) return x.map(Boolean);
    const pad = Math.floor(w / 2);
    const at = (k) => (x[Math.min(n - 1, Math.max(0, k - pad))] ? 1 : 0); // padded index k
    const out = new Array(n);
    let c = 0;
    for (let k = 0; k < w; k++) c += at(k);
    for (let i = 0; i < n; i++) {
      if (i) c += at(i + w - 1) - at(i - 1);
      out[i] = w % 2 ? 2 * c > w : 2 * c >= w;
    }
    return out;
  }

  function toSegments(mask, fps, mergeGap, minLen, trim = 0, minTrimmed = 0) {
    const dt = 1 / fps;
    const segs = [];
    let i = 0;
    const n = mask.length;
    while (i < n) {
      if (!mask[i]) {
        i++;
        continue;
      }
      let j = i;
      while (j < n && mask[j]) j++;
      segs.push([i / fps, (j - 1) / fps + dt]);
      i = j;
    }
    let merged = [];
    for (const s of segs) {
      const last = merged[merged.length - 1];
      if (last && s[0] - last[1] < mergeGap - EPS) last[1] = s[1];
      else merged.push(s);
    }
    merged = merged.filter((s) => s[1] - s[0] >= minLen - EPS);
    if (trim) {
      merged = merged.filter(([a, b]) => b - a - 2 * trim >= minTrimmed - EPS).map(([a, b]) => [a + trim, b - trim]);
    }
    return merged.map(([a, b]) => ({ start: r2(a), end: r2(b) }));
  }

  function subtract(segs, cut) {
    const out = [];
    for (const s of segs) {
      let pieces = [[s.start, s.end]];
      for (const c of cut) {
        const nxt = [];
        for (const [a, b] of pieces) {
          if (c.end <= a || c.start >= b) {
            nxt.push([a, b]);
            continue;
          }
          if (c.start > a) nxt.push([a, c.start]);
          if (c.end < b) nxt.push([c.end, b]);
        }
        pieces = nxt;
      }
      for (const [a, b] of pieces) if (b - a >= 0.5 - EPS) out.push({ start: r2(a), end: r2(b) });
    }
    return out;
  }

  const defaultChoices = (tl) => Object.fromEntries((tl.members || []).map((m) => [m.key, m.default || "normal"]));

  // choices: {member key: "skip" | "normal" | "highlight"}; members not in it keep
  // their default. hlWhen: "any" (default) or "focus" (only the frames she is
  // the main focus of; a list without focus info, format 4, falls back to "any").
  // -> {skips: [{start, end}], highlights: [{key, ranges}]}
  function apply(tl, choices, hlWhen = "any") {
    const ch = { ...defaultChoices(tl), ...(choices || {}) };
    const R = tl.rules;
    const fps = tl.fps;
    const skip = new Set(Object.keys(ch).filter((k) => ch[k] === "skip"));
    const { full, part, state, focus } = expand(tl);
    const byFocus = hlWhen === "focus" && hasFocus(tl);
    const st = frameStates(full, part, state, skip);
    const filled = fillGaps(st, Math.round(R.fillGap * fps));
    const mask = medianFilter(filled.map((s) => s === SKIP), R.median);
    const skips = toSegments(mask, fps, R.mergeGap, R.minSkip, R.edgeTrim, R.minTrimmed);
    const highlights = [];
    for (const m of tl.members || []) {
      const k = m.key;
      if (ch[k] !== "highlight") continue;
      const on = byFocus ? focus.map((fo) => fo === k) : full.map((f, i) => f.has(k) || part[i].has(k));
      const hm = medianFilter(on, R.hlMedian);
      const rng = subtract(toSegments(hm, fps, R.hlMergeGap, R.hlMin), skips)
        .filter((s) => s.end - s.start >= R.hlMin - EPS);
      if (rng.length) highlights.push({ key: k, ranges: rng });
    }
    return { skips, highlights };
  }

  // ---- choices: the defaults are the profiles' (Kirk's) choices; each user's
  // own ones are stored in storage.local "choices" ({key: choice}, only changes).
  const keyOf = (profileId, memberId) => `${profileId}:${memberId}`;

  // profiles.json -> {key: default choice}
  function profileDefaults(profiles) {
    const out = {};
    for (const p of profiles || []) {
      for (const m of p.members || []) out[keyOf(p.id, m.id)] = m.default || "normal";
    }
    return out;
  }

  // The choice for every member of the timeline: the user's, else the bundled
  // profile's default, else the default the scanner wrote into the list.
  function effectiveChoices(tl, profiles, userChoices) {
    const defs = profileDefaults(profiles);
    const out = {};
    for (const m of tl.members || []) {
      const u = userChoices && userChoices[m.key];
      out[m.key] = CHOICES.includes(u) ? u : defs[m.key] || m.default || "normal";
    }
    return out;
  }

  // A format-4/5 skip list with this user's choices and hlWhen -> what the page uses:
  // {skips, highlights: [{key, member, name, group, profile, ranges, byDefault}],
  //  skipMembers: [{name, group}], choices}. Default highlights come first (so
  // they keep the first colour). null for lists without a timeline (format <= 3:
  // only their precomputed default skips can be used).
  function forList(d, profiles, userChoices, hlWhen = "any") {
    const tl = d && d.timeline;
    if (!tl || !Array.isArray(tl.members) || !Array.isArray(tl.segments) || !tl.rules) return null;
    const ch = effectiveChoices(tl, profiles, userChoices);
    const res = apply(tl, ch, HL_WHEN.includes(hlWhen) ? hlWhen : "any");
    const info = Object.fromEntries(tl.members.map((m) => [m.key, m]));
    const defs = profileDefaults(profiles);
    const order = Object.fromEntries(tl.members.map((m, i) => [m.key, i]));
    const highlights = res.highlights.map((h) => {
      const m = info[h.key];
      return { key: h.key, member: m.member, name: m.name || m.member, group: m.group || "",
               profile: m.profile || "", ranges: h.ranges,
               byDefault: (defs[h.key] || m.default) === "highlight" };
    }).sort((a, b) => (b.byDefault - a.byDefault) || order[a.key] - order[b.key]);
    const skipMembers = tl.members.filter((m) => ch[m.key] === "skip")
      .map((m) => ({ name: m.name || m.member, group: m.group || "" }));
    return { skips: res.skips, highlights, skipMembers, choices: ch };
  }

  const api = { SKIP, KEEP, NONE, CHOICES, HL_WHEN, hasFocus, apply, defaultChoices, profileDefaults, effectiveChoices, keyOf,
                forList, expand, frameStates, fillGaps, medianFilter, toSegments, subtract };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.KmsRules = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

// Content script for youtube.com: skips the ranges of the video's skip list
// (one per video, for whichever groups are in it), draws progress-bar markers, "Skipped" toast with Undo, and a
// "next highlight" button / shortcut for the highlighted member(s), the
// "Ian: 25% · Skipped: 17%" line under the video title, and progress of
// "Filter this video" scans (run by the background script on Kirk's PC).
// Skips and highlights follow the user's Skip / Normal / Highlight choice per
// member (settings page), worked out from the list's timeline by rules.js.
"use strict";

(() => {
  // one colour per highlighted member of a video; the first (Ian by default) is pink
  const HL_COLORS = ["#ff4fa3", "#ffc83d", "#3dd6ff", "#9dff5c", "#b28cff", "#ff8a3d", "#5cffc8", "#ff6b6b"];
  const st = {
    videoId: null,
    raw: null,           // the skip list as loaded (normalized), or null
    list: null,          // raw + this user's choices applied (skips, highlights, skipMembers)
    profiles: [],        // groups and their members' default choices (profiles.json)
    choices: {},         // this user's choices {"<profile>:<member>": "skip" | "normal" | "highlight"}
    hlWhen: "any",       // highlight any time she's on screen / only when she's the main focus ("focus")
    source: "none",
    loading: false,
    suppressed: new Set(), // skip indexes undone by the user (until next video)
    settings: { enabled: true, highlights: true },
    video: null,
    toastTimer: null,
    scan: null,          // {available, job} from the background script for videoId
    scanAskedAt: 0,
    bannerTimer: null,
    bannerShown: false,
    // "Blur skipped members when they can't be skipped" (off by default)
    blur: { on: false, strength: 3, size: "normal" },
    blurIdx: null,       // the skipped members' face tracks of this video (KmsBlur.index), or null
    blurVideo: null,     // video element the frame callback is registered on
    blurHandle: null,    // pending requestVideoFrameCallback / requestAnimationFrame
    blurRect: null,      // video picture inside the player: {left, top, width, height} px
  };
  const hooked = new WeakSet();
  const applied = new Set(); // videoIds whose finished scan was already applied

  const fmt = (t) => {
    t = Math.max(0, Math.round(t));
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    const ss = String(s).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
  };

  const player = () => document.querySelector("#movie_player");
  const isAd = () => !!player()?.classList.contains("ad-showing");

  function currentVideoId() {
    if (location.pathname !== "/watch") return null;
    return new URLSearchParams(location.search).get("v");
  }

  // Accepts format 3 (one list per video: groups, skipMembers [{name, group}],
  // highlights [{member, name, group, ranges}]), format 2 (one group per list,
  // skipMembers as names) and format 1 ({ian: [...]}). Highlighted members with
  // no parts in the video are dropped.
  function normalize(d) {
    if (!d || typeof d !== "object") return null;
    let hl = Array.isArray(d.highlights) ? d.highlights : [];
    if (!hl.length && Array.isArray(d.ian)) hl = [{ member: "ian", name: "Ian", ranges: d.ian }];
    hl = hl
      .filter((h) => h && Array.isArray(h.ranges) && h.ranges.length)
      .map((h, i) => ({ member: String(h.member || i), name: String(h.name || h.member || "Highlight"),
                        group: h.group ? String(h.group) : d.group ? String(d.group) : "",
                        ranges: h.ranges, color: HL_COLORS[i % HL_COLORS.length] }));
    const skipMembers = (Array.isArray(d.skipMembers) ? d.skipMembers : [])
      .map((m) => (typeof m === "string" ? { name: m, group: d.group || "" }
                   : { name: String(m?.name || m?.member || "?"), group: String(m?.group || "") }));
    const groups = Array.isArray(d.groups) ? d.groups.map((g) => String(g?.name || g))
      : d.group ? [String(d.group)] : [];
    return { ...d, skips: Array.isArray(d.skips) ? d.skips : [], highlights: hl, skipMembers, groups };
  }

  // the list with this user's choices; lists without a timeline (format <= 3)
  // keep their precomputed default skips and highlights
  function view(raw) {
    if (!raw) return null;
    const v = globalThis.KmsRules ? globalThis.KmsRules.forList(raw, st.profiles, st.choices, st.hlWhen) : null;
    if (!v) return raw;
    const highlights = v.highlights.map((h, i) => ({ ...h, color: HL_COLORS[i % HL_COLORS.length] }));
    const skipKeys = Object.keys(v.choices).filter((k) => v.choices[k] === "skip");
    return { ...raw, skips: v.skips, highlights, skipMembers: v.skipMembers, skipKeys, custom: true };
  }

  function applyChoices() {
    const old = JSON.stringify(st.list ? [st.list.skips, st.list.highlights] : null);
    st.list = view(st.raw);
    blurSetup(); // who is blurred follows the Skip choices
    if (JSON.stringify(st.list ? [st.list.skips, st.list.highlights] : null) === old) return;
    st.suppressed = new Set(); // skip indexes changed
    drawMarkers();
    ensureNextButton();
    renderStats();
  }

  const hlNames = () => (st.list?.highlights || []).filter((h) => h.ranges.length).map((h) => h.name);
  const hlLabel = () => {
    const n = hlNames();
    return n.length === 1 ? n[0] : "highlighted";
  };

  // ------------------------------------------------------------ navigation
  async function onNavigate() {
    const id = currentVideoId();
    if (id === st.videoId) return;
    st.videoId = id;
    st.raw = null;
    st.list = null;
    st.source = "none";
    st.suppressed = new Set();
    st.scan = null;
    blurSetup();
    removeMarkers();
    hideToast();
    hideBanner();
    renderStats();
    if (!id) return;
    st.loading = true;
    try {
      const r = await browser.runtime.sendMessage({ type: "getList", id });
      if (st.videoId !== id) return; // navigated away meanwhile
      const list = normalize(r && r.data);
      st.raw = list && list.videoId === id ? list : null;
      st.list = view(st.raw);
      st.source = r ? r.source : "none";
      blurSetup();
    } catch (e) {
      console.warn("[kms] could not load skip list", e);
    } finally {
      if (st.videoId === id) st.loading = false;
    }
    if (!st.list) await refreshScan(id);
    ensure();
  }

  // ------------------------------------------------------------ scanning
  async function refreshScan(id) {
    st.scanAskedAt = Date.now();
    try {
      const r = await browser.runtime.sendMessage({ type: "scanState", id });
      if (st.videoId !== id) return;
      st.scan = r || null;
      if (r && r.job) {
        await onScanUpdate(r.job);
        return;
      }
    } catch (e) {
      st.scan = null;
    }
    renderStats();
  }

  async function startScan() {
    const id = st.videoId;
    if (!id) return;
    await onScanUpdate({ videoId: id, state: "running", stage: "starting", percent: 0, eta: null });
    try {
      const r = await browser.runtime.sendMessage({ type: "startScan", id });
      if (st.videoId !== id) return;
      if (r && r.job) {
        await onScanUpdate(r.job);
      } else {
        st.scan = r || null; // scanner not installed after all
        hideBanner();
        renderStats();
      }
    } catch (e) {
      console.warn("[kms] could not start the scan", e);
    }
  }

  const etaText = (eta) => (eta ? `, about ${fmt(eta)} left` : "");
  const progressText = (j) =>
    j.stage === "upload" ? `Filtering… ${j.percent}%, uploading` : `Filtering… ${j.percent}%${etaText(j.eta)}`;

  async function onScanUpdate(job) {
    if (!job || job.videoId !== st.videoId) return;
    const wasRunning = st.scan?.job?.state === "running";
    st.scan = { available: true, ...st.scan, job };
    st.scanAskedAt = Date.now();
    if (job.state === "running") {
      showBanner(progressText(job));
    } else if (job.state === "error") {
      showBanner(`Filtering failed: ${job.error || "unknown error"}`, 8000);
    } else if (job.state === "done") {
      if (!wasRunning && (st.list || applied.has(job.videoId))) {
        renderStats(); // already applied
        return;
      }
      applied.add(job.videoId);
      const keep = st.scan;
      await reload(); // picks up the list the background script just stored
      st.scan = keep;
      const parts = statsParts();
      const text = parts.length ? parts.map((p) => `${p.label.replace(":", "")} ${p.pct}%`).join(" · ") : "nothing to skip";
      showBanner(`Done: ${text}`, 8000, job.note);
    }
    renderStats();
  }

  // ------------------------------------------------------------ stats line
  // Percent of the video's duration, per highlighted member and skipped.
  function statsParts() {
    const l = st.list;
    const d = l?.duration || duration();
    if (!l || !d) return [];
    const pct = (ranges) => Math.round((100 * ranges.reduce((a, r) => a + Math.max(0, r.end - r.start), 0)) / d);
    const parts = [];
    if (st.settings.highlights) {
      for (const h of l.highlights) {
        if (h.ranges.length) parts.push({ label: `${h.name}:`, pct: pct(h.ranges), color: h.color });
      }
    }
    if (st.settings.enabled) parts.push({ label: "Skipped:", pct: pct(l.skips) });
    return parts;
  }

  function statsAnchor() {
    return document.querySelector("ytd-watch-metadata #title");
  }

  function renderStats() {
    let el = document.querySelector(".cys-stats");
    const anchor = statsAnchor();
    const id = st.videoId;
    const job = st.scan?.job;
    let content = null;
    if (!id || !anchor || st.loading) {
      content = null;
    } else if (job && job.state === "running") {
      content = [{ text: progressText(job) }];
    } else if (st.list) {
      const parts = statsParts();
      if (parts.length) {
        content = [];
        parts.forEach((p, i) => {
          if (i) content.push({ text: " · " });
          content.push({ text: `${p.label} ${p.pct}%`, color: p.color });
        });
      }
    } else if (st.settings.enabled || st.settings.highlights) {
      const failed = job && job.state === "error";
      content = [{ text: failed ? "Filtering failed" : "Not filtered yet" }];
      if (st.scan?.available) {
        content.push({ text: " · " });
        content.push({ text: failed ? "Try again" : "Filter this video", action: startScan });
      }
    }
    if (!content) {
      el?.remove();
      return;
    }
    const key = id + JSON.stringify(content.map((c) => [c.text, c.color || "", !!c.action]));
    if (el && el.dataset.key === key && el.previousElementSibling === anchor) return;
    if (!el) {
      el = document.createElement("div");
      el.className = "cys-stats";
    }
    if (el.previousElementSibling !== anchor) anchor.after(el);
    el.dataset.key = key;
    el.dataset.videoId = id;
    el.replaceChildren();
    for (const c of content) {
      if (c.action) {
        const b = document.createElement("button");
        b.className = "cys-filter";
        b.textContent = c.text;
        b.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          c.action();
        });
        el.appendChild(b);
      } else {
        const span = document.createElement("span");
        span.textContent = c.text;
        if (c.color) {
          span.className = "cys-stat-hl";
          span.style.color = c.color;
        }
        el.appendChild(span);
      }
    }
  }

  // ------------------------------------------------------------ banner
  function showBanner(text, hideAfter, note) {
    const p = player();
    if (!p) return;
    let el = p.querySelector(".cys-banner");
    if (!el) {
      el = document.createElement("div");
      el.className = "cys-banner";
      p.appendChild(el);
    }
    el.textContent = text;
    if (note) {
      const small = document.createElement("small");
      small.textContent = note;
      el.appendChild(small);
    }
    el.classList.add("cys-show");
    st.bannerShown = true;
    clearTimeout(st.bannerTimer);
    if (hideAfter) st.bannerTimer = setTimeout(hideBanner, hideAfter);
  }

  function hideBanner() {
    clearTimeout(st.bannerTimer);
    st.bannerShown = false;
    document.querySelector(".cys-banner")?.classList.remove("cys-show");
  }

  async function reload() {
    st.videoId = null; // force a fresh load (source changed)
    await onNavigate();
  }

  // ------------------------------------------------------------ skipping
  function hookVideo() {
    const v = document.querySelector("#movie_player video.html5-main-video") ||
              document.querySelector("#movie_player video");
    if (!v || hooked.has(v)) {
      if (v) st.video = v;
      return;
    }
    hooked.add(v);
    st.video = v;
    v.addEventListener("timeupdate", check);
    v.addEventListener("seeking", check);
    v.addEventListener("durationchange", drawMarkers);
    v.addEventListener("loadedmetadata", drawMarkers);
    // the picture's size / letterboxing can change (quality, another video)
    v.addEventListener("resize", () => {
      st.blurRect = null;
    });
    v.addEventListener("seeked", () => {
      if (st.blurIdx) blurRender();
    });
    if (st.blurIdx) blurStart();
  }

  function check() {
    const v = st.video;
    if (!v || !st.settings.enabled || !st.list || isAd()) return;
    if (currentVideoId() !== st.list.videoId) return;
    const t = v.currentTime;
    const skips = st.list.skips;
    for (let i = 0; i < skips.length; i++) {
      const s = skips[i];
      if (st.suppressed.has(i)) continue;
      if (t >= s.start && t < s.end - 0.25) {
        const end = Math.min(s.end, (v.duration || s.end) - 0.1);
        v.currentTime = end;
        showToast(`Skipped ${fmt(s.start)} → ${fmt(s.end)}`, "Undo", () => {
          st.suppressed.add(i);
          v.currentTime = s.start;
          hideToast();
        });
        return;
      }
    }
  }

  function nextHighlight() {
    const v = st.video;
    const all = (st.list?.highlights || [])
      .flatMap((h) => h.ranges.map((r) => ({ ...r, name: h.name })))
      .sort((a, b) => a.start - b.start);
    if (!v || !all.length) {
      showToast(st.list ? "No highlighted parts in this video" : "Video not scanned yet");
      return;
    }
    const t = v.currentTime;
    const next = all.find((r) => r.start > t + 1);
    if (!next) {
      showToast(`No more ${hlLabel()} parts`);
      return;
    }
    v.currentTime = next.start;
    showToast(`${next.name}: ${fmt(next.start)} → ${fmt(next.end)}`);
  }

  // ------------------------------------------------------------ toast
  function showToast(text, actionLabel, action) {
    const p = player();
    if (!p) return;
    let el = p.querySelector(".cys-toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "cys-toast";
      p.appendChild(el);
    }
    el.replaceChildren();
    const span = document.createElement("span");
    span.textContent = text;
    el.appendChild(span);
    if (actionLabel) {
      const b = document.createElement("button");
      b.textContent = actionLabel;
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        action();
      });
      el.appendChild(b);
    }
    el.classList.add("cys-show");
    clearTimeout(st.toastTimer);
    st.toastTimer = setTimeout(hideToast, 5000);
  }

  function hideToast() {
    document.querySelector(".cys-toast")?.classList.remove("cys-show");
  }

  // ------------------------------------------------------------ markers
  function removeMarkers() {
    document.querySelectorAll(".cys-markers").forEach((e) => e.remove());
    document.querySelectorAll(".cys-next-btn").forEach((e) => e.remove());
  }

  function duration() {
    const v = st.video;
    if (v && !isAd() && isFinite(v.duration) && v.duration > 0) return v.duration;
    return st.list?.duration || 0;
  }

  function drawMarkers() {
    document.querySelectorAll(".cys-markers").forEach((e) => e.remove());
    if (!st.list || isAd()) return;
    const bar = player()?.querySelector(".ytp-progress-bar");
    const d = duration();
    if (!bar || !d) return;
    const box = document.createElement("div");
    box.className = "cys-markers";
    box.dataset.videoId = st.list.videoId;
    const add = (r, cls, label, color) => {
      const m = document.createElement("div");
      m.className = cls;
      m.style.left = `${(100 * r.start) / d}%`;
      m.style.width = `${(100 * Math.max(0, Math.min(r.end, d) - r.start)) / d}%`;
      if (color) {
        m.style.background = color;
        m.style.boxShadow = `0 0 4px ${color}`;
      }
      m.title = `${label} ${fmt(r.start)} – ${fmt(r.end)}`;
      box.appendChild(m);
    };
    if (st.settings.enabled) st.list.skips.forEach((r) => add(r, "cys-skip", "Skipped"));
    if (st.settings.highlights) {
      for (const h of st.list.highlights) h.ranges.forEach((r) => add(r, "cys-hl", h.name, h.color));
    }
    bar.appendChild(box);
  }

  function ensureNextButton() {
    const p = player();
    const want = st.settings.highlights && hlNames().length;
    const existing = p?.querySelector(".cys-next-btn");
    const label = `${hlNames().length === 1 ? hlNames()[0] : "Next"} ⏭`; // several: cycles through all
    if (!want) {
      existing?.remove();
      return;
    }
    if (existing) {
      if (existing.textContent !== label) existing.textContent = label;
      return;
    }
    const controls = p?.querySelector(".ytp-right-controls");
    if (!controls) return;
    const b = document.createElement("button");
    b.className = "ytp-button cys-next-btn";
    b.title = `Next ${hlLabel()} part (Alt+Shift+H)`;
    b.textContent = label;
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      nextHighlight();
    });
    controls.prepend(b);
  }

  // YouTube re-renders the player on navigation/resizes, so re-check periodically.
  function ensure() {
    if (currentVideoId() !== st.videoId) {
      onNavigate();
      return;
    }
    hookVideo();
    const box = document.querySelector(".cys-markers");
    const stale = box && (!st.list || box.dataset.videoId !== st.list.videoId);
    if (stale) box.remove();
    if (st.list && (!box || stale || !box.isConnected)) drawMarkers();
    ensureNextButton();
    renderStats();
    if (st.blurIdx) {
      st.blurRect = null; // cheap once a second; catches player moves a resize doesn't
      if (!blurLayer(false) || st.blurVideo !== st.video || !st.blurHandle) blurStart();
    } else if (blurLayer(false)) {
      blurStop();
    }
    // keep following a running scan if updates stop (e.g. the background restarted)
    if (st.scan?.job?.state === "running" && Date.now() - st.scanAskedAt > 5000) refreshScan(st.videoId);
  }

  // ------------------------------------------------------------ blur
  // When a member the user skips is on screen with someone they don't skip, the
  // frame isn't skipped; with "Blur skipped members when they can't be skipped"
  // her face is blurred instead (face boxes: timeline.faces of format-6 lists,
  // blur.js). One small canvas per face that draws that part of the current
  // video frame blurred (canvas filter; works in every Firefox and in
  // screenshots, unlike backdrop-filter over video), in a layer over
  // the video picture inside the player (so it follows fullscreen / theater
  // mode), updated on every video frame (requestVideoFrameCallback, else
  // requestAnimationFrame). Only while skipping is on, never during ads or in
  // a skipped range.
  const BLUR_SETTINGS = ["blur", "blurStrength", "blurSize"];
  const blurDecoded = new WeakMap(); // loaded list -> its decoded face tracks
  // strength 1..5 (light ... very strong): blur radius as a share of the face width
  const BLUR_K = [0, 0.04, 0.07, 0.1, 0.15, 0.22];
  // padding around the detected face box (share of its width / height):
  // left and right, top (forehead, hair), bottom (chin)
  const BLUR_PAD = { face: [0.15, 0.25, 0.1], normal: [0.3, 0.45, 0.2], large: [0.5, 0.75, 0.35] };

  function readBlurSettings(s) {
    st.blur = {
      on: s.blur === true,
      strength: Number.isInteger(s.blurStrength) && s.blurStrength >= 1 && s.blurStrength <= 5 ? s.blurStrength : 3,
      size: Object.hasOwn(BLUR_PAD, s.blurSize) ? s.blurSize : "normal",
      raw: { blur: s.blur, blurStrength: s.blurStrength, blurSize: s.blurSize },
    };
  }

  // (Re)build the index of the skipped members' face tracks; start / stop the loop.
  function blurSetup() {
    const B = globalThis.KmsBlur;
    const faces = st.list?.timeline?.faces;
    const keys = st.list?.skipKeys || [];
    st.blurIdx = null;
    if (B && st.blur.on && B.valid(faces) && keys.length) {
      // decoded once per loaded list (st.list is rebuilt when choices change)
      if (st.raw && !blurDecoded.has(st.raw)) blurDecoded.set(st.raw, B.decode(faces));
      const dec = st.raw && blurDecoded.get(st.raw);
      const idx = dec && B.index(dec, keys);
      st.blurIdx = idx && idx.count ? idx : null;
    }
    if (st.blurIdx) blurStart();
    else blurStop();
  }

  const blurWanted = () => !!(st.blurIdx && st.blur.on && st.settings.enabled && st.list &&
                              currentVideoId() === st.list.videoId);

  let blurRO = null;
  function blurLayer(create) {
    const p = player();
    let el = p?.querySelector(".cys-blur-layer");
    if (!el && create && p) {
      el = document.createElement("div");
      el.className = "cys-blur-layer";
      p.appendChild(el);
      st.blurRect = null;
      if (typeof ResizeObserver !== "undefined") {
        blurRO = blurRO || new ResizeObserver(() => {
          st.blurRect = null;
          if (st.blurIdx) blurRender();
        });
        blurRO.disconnect();
        blurRO.observe(p);
        if (st.video) blurRO.observe(st.video);
      }
    }
    return el;
  }

  // The video picture inside the player, in px: YouTube sizes and places the
  // <video> element itself, and the picture is letterboxed inside it
  // (object-fit: contain) when their aspect ratios differ.
  function blurPlace(layer) {
    const v = st.video, p = player();
    if (!v || !p || !v.videoWidth || !v.videoHeight) return false;
    if (!st.blurRect) {
      const pr = p.getBoundingClientRect(), vr = v.getBoundingClientRect();
      if (!vr.width || !vr.height) return false;
      const s = Math.min(vr.width / v.videoWidth, vr.height / v.videoHeight);
      const w = v.videoWidth * s, h = v.videoHeight * s;
      const r = { left: vr.left - pr.left + (vr.width - w) / 2, top: vr.top - pr.top + (vr.height - h) / 2,
                  width: w, height: h };
      st.blurRect = r;
      layer.style.left = `${r.left}px`;
      layer.style.top = `${r.top}px`;
      layer.style.width = `${r.width}px`;
      layer.style.height = `${r.height}px`;
    }
    return true;
  }

  const inSkip = (t) => (st.list?.skips || []).some((s) => t >= s.start && t < s.end);

  function blurRender(mediaTime) {
    const want = blurWanted();
    const layer = blurLayer(want);
    if (!layer) return;
    const v = st.video;
    const t = typeof mediaTime === "number" ? mediaTime : v?.currentTime;
    let boxes = [];
    if (want && v && !isAd() && typeof t === "number" && !inSkip(t) && blurPlace(layer)) {
      boxes = globalThis.KmsBlur.boxesAt(st.blurIdx, t);
    }
    const [px, pt, pb] = BLUR_PAD[st.blur.size];
    const k = BLUR_K[st.blur.strength];
    const W = st.blurRect?.width || 0;
    while (layer.children.length < boxes.length) {
      const c = document.createElement("canvas");
      c.className = "cys-blur";
      layer.appendChild(c);
    }
    const H = st.blurRect?.height || 0;
    for (let i = 0; i < layer.children.length; i++) {
      const c = layer.children[i];
      const b = boxes[i];
      if (!b) {
        if (c.style.display !== "none") c.style.display = "none";
        continue;
      }
      // the padded box, as a share of the picture
      const bx = b.x - px * b.w, by = b.y - pt * b.h, bw = b.w * (1 + 2 * px), bh = b.h * (1 + pt + pb);
      c.style.display = "";
      c.style.left = `${100 * bx}%`;
      c.style.top = `${100 * by}%`;
      c.style.width = `${100 * bw}%`;
      c.style.height = `${100 * bh}%`;
      c.style.opacity = String(b.op);
      c.dataset.key = b.key;
      // That part of the current video frame, blurred, drawn small (it is blurred
      // anyway) and scaled up by CSS. The radius grows with the face's size.
      const sw = Math.max(1, bw * W), sh = Math.max(1, bh * H);
      const cw = Math.max(8, Math.min(200, Math.round(sw / 2)));
      const ch = Math.max(8, Math.round((cw * sh) / sw));
      if (c.width !== cw) c.width = cw;
      if (c.height !== ch) c.height = ch;
      const s = cw / sw; // canvas px per screen px
      const r = Math.max(1, k * b.w * W * s);
      c.dataset.r = `blur(${Math.round(k * b.w * W)}px)`;
      const ctx = c.getContext("2d");
      const vw = v.videoWidth, vh = v.videoHeight;
      const m = Math.ceil(2 * r); // drawn beyond the edges so the blur doesn't fade out there
      const ex = m / cw, ey = m / ch;
      ctx.clearRect(0, 0, cw, ch);
      ctx.filter = `blur(${r}px)`;
      try {
        ctx.drawImage(v, (bx - ex * bw) * vw, (by - ey * bh) * vh, bw * (1 + 2 * ex) * vw, bh * (1 + 2 * ey) * vh,
                      -m, -m, cw + 2 * m, ch + 2 * m);
      } catch (e) {
        // no frame to draw yet: leave it empty
      }
    }
    layer.dataset.count = String(boxes.length);
  }

  function blurTick(now, meta) {
    st.blurHandle = null;
    blurRender(meta && typeof meta.mediaTime === "number" ? meta.mediaTime : undefined);
    if (blurWanted()) blurSchedule();
  }

  function blurSchedule() {
    const v = st.video;
    if (!v || st.blurHandle) return;
    st.blurVideo = v;
    st.blurHandle = typeof v.requestVideoFrameCallback === "function"
      ? { v, id: v.requestVideoFrameCallback(blurTick) }
      : { raf: requestAnimationFrame(blurTick) };
  }

  function blurCancel() {
    const h = st.blurHandle;
    st.blurHandle = null;
    if (!h) return;
    if (h.raf) cancelAnimationFrame(h.raf);
    else if (h.v.cancelVideoFrameCallback) h.v.cancelVideoFrameCallback(h.id);
  }

  function blurStart() {
    if (st.blurVideo !== st.video) blurCancel(); // YouTube swapped the <video>
    blurRender();
    if (blurWanted()) blurSchedule();
  }

  function blurStop() {
    blurCancel();
    blurLayer(false)?.remove();
    st.blurRect = null;
  }

  // ------------------------------------------------------------ wiring
  Promise.all([
    browser.storage.local.get(["enabled", "highlights", "choices", "hlWhen", ...BLUR_SETTINGS]),
    browser.runtime.sendMessage({ type: "profiles" }).catch(() => []),
  ]).then(([s, profiles]) => {
    for (const k of ["enabled", "highlights"]) if (k in s) st.settings[k] = s[k];
    st.choices = s.choices || {};
    st.hlWhen = s.hlWhen || "any";
    readBlurSettings(s);
    st.profiles = profiles || [];
    applyChoices();
    ensure();
  });
  browser.storage.onChanged.addListener((ch, area) => {
    if (area !== "local") return;
    for (const k of ["enabled", "highlights"]) if (k in ch) st.settings[k] = ch[k].newValue;
    if ("choices" in ch || "hlWhen" in ch) {
      if ("choices" in ch) st.choices = ch.choices.newValue || {};
      if ("hlWhen" in ch) st.hlWhen = ch.hlWhen.newValue || "any";
      applyChoices(); // settings page changed who is skipped / highlighted: no reload
    }
    if (BLUR_SETTINGS.some((k) => k in ch) || "enabled" in ch) {
      const v = { ...st.blur.raw };
      for (const k of BLUR_SETTINGS) if (k in ch) v[k] = ch[k].newValue;
      readBlurSettings(v);
      blurSetup();
    }
    if ("baseUrl" in ch) {
      reload();
      return;
    }
    drawMarkers();
    ensureNextButton();
    renderStats();
  });
  browser.runtime.onMessage.addListener((msg) => {
    if (msg?.type === "nextHighlight") nextHighlight();
    if (msg?.type === "scanUpdate") onScanUpdate(msg.job);
    if (msg?.type === "status") {
      return Promise.resolve({
        videoId: st.videoId,
        loading: st.loading,
        scanned: !!st.list,
        source: st.source,
        groups: st.list?.groups || [],
        skipMembers: st.list?.skipMembers || [],
        skips: st.list?.skips.length || 0,
        skipSeconds: Math.round((st.list?.skips || []).reduce((a, r) => a + r.end - r.start, 0)),
        highlights: (st.list?.highlights || []).map((h) => ({ name: h.name, count: h.ranges.length,
                                                              color: h.color })),
        scannedAt: st.list?.scannedAt || null,
        stats: statsParts().map((p) => `${p.label} ${p.pct}%`).join(" · "),
      });
    }
    return undefined;
  });
  document.addEventListener("yt-navigate-finish", () => onNavigate());
  window.addEventListener("popstate", () => onNavigate());
  setInterval(ensure, 1000);
  onNavigate();
})();

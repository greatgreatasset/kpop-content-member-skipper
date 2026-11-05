// Content script for youtube.com: skips the ranges of the video's skip list
// (one per video, for whichever groups are in it), draws progress-bar markers, "Skipped" toast with Undo, and a
// "next highlight" button / shortcut for the highlighted member(s), the
// "Ian: 25% · Skipped: 17%" line under the video title, and progress of
// "Filter this video" scans (run by the background script on Kirk's PC).
"use strict";

(() => {
  const HL_COLORS = ["#ff4fa3", "#ffc83d", "#3dd6ff", "#9dff5c"];
  const st = {
    videoId: null,
    list: null,          // normalized skip list for videoId, or null
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
    st.list = null;
    st.source = "none";
    st.suppressed = new Set();
    st.scan = null;
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
      st.list = list && list.videoId === id ? list : null;
      st.source = r ? r.source : "none";
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
    const label = `${hlNames().length === 1 ? hlNames()[0] : "Next"} ⏭`;
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
    // keep following a running scan if updates stop (e.g. the background restarted)
    if (st.scan?.job?.state === "running" && Date.now() - st.scanAskedAt > 5000) refreshScan(st.videoId);
  }

  // ------------------------------------------------------------ wiring
  browser.storage.local.get(["enabled", "highlights"]).then((s) => {
    Object.assign(st.settings, s);
    ensure();
  });
  browser.storage.onChanged.addListener((ch, area) => {
    if (area !== "local") return;
    for (const k of ["enabled", "highlights"]) if (k in ch) st.settings[k] = ch[k].newValue;
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
        highlights: (st.list?.highlights || []).map((h) => ({ name: h.name, count: h.ranges.length })),
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

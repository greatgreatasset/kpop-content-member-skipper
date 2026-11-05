// Content script for youtube.com: skips the ranges of the selected profile's
// skip list, draws progress-bar markers, "Skipped" toast with Undo, and a
// "next highlight" button / shortcut for the highlighted member(s).
"use strict";

(() => {
  const HL_COLORS = ["#ff4fa3", "#ffc83d", "#3dd6ff", "#9dff5c"];
  const st = {
    videoId: null,
    list: null,          // normalized skip list for videoId, or null
    source: "none",
    profile: null,
    loading: false,
    suppressed: new Set(), // skip indexes undone by the user (until next video)
    settings: { enabled: true, highlights: true },
    video: null,
    toastTimer: null,
  };
  const hooked = new WeakSet();

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

  // Accepts format 2 ({highlights:[{member,name,ranges}]}) and the older
  // format 1 ({ian:[...]}).
  function normalize(d) {
    if (!d || typeof d !== "object") return null;
    let hl = Array.isArray(d.highlights) ? d.highlights : [];
    if (!hl.length && Array.isArray(d.ian)) hl = [{ member: "ian", name: "Ian", ranges: d.ian }];
    hl = hl
      .filter((h) => h && Array.isArray(h.ranges))
      .map((h, i) => ({ member: String(h.member || i), name: String(h.name || h.member || "Highlight"),
                        ranges: h.ranges, color: HL_COLORS[i % HL_COLORS.length] }));
    return { ...d, skips: Array.isArray(d.skips) ? d.skips : [], highlights: hl };
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
    removeMarkers();
    hideToast();
    if (!id) return;
    st.loading = true;
    try {
      const r = await browser.runtime.sendMessage({ type: "getList", id });
      if (st.videoId !== id) return; // navigated away meanwhile
      const list = normalize(r && r.data);
      st.list = list && list.videoId === id ? list : null;
      st.source = r ? r.source : "none";
      st.profile = r ? r.profile : null;
    } catch (e) {
      console.warn("[kms] could not load skip list", e);
    } finally {
      if (st.videoId === id) st.loading = false;
    }
    ensure();
  }

  async function reload() {
    st.videoId = null; // force a fresh load (profile or source changed)
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
  }

  // ------------------------------------------------------------ wiring
  browser.storage.local.get(["enabled", "highlights"]).then((s) => {
    Object.assign(st.settings, s);
    ensure();
  });
  browser.storage.onChanged.addListener((ch, area) => {
    if (area !== "local") return;
    for (const k of ["enabled", "highlights"]) if (k in ch) st.settings[k] = ch[k].newValue;
    if ("profile" in ch || "baseUrl" in ch) {
      reload();
      return;
    }
    drawMarkers();
    ensureNextButton();
  });
  browser.runtime.onMessage.addListener((msg) => {
    if (msg?.type === "nextHighlight") nextHighlight();
    if (msg?.type === "status") {
      return Promise.resolve({
        videoId: st.videoId,
        loading: st.loading,
        scanned: !!st.list,
        source: st.source,
        profile: st.profile,
        group: st.list?.group || null,
        skips: st.list?.skips.length || 0,
        skipSeconds: Math.round((st.list?.skips || []).reduce((a, r) => a + r.end - r.start, 0)),
        highlights: (st.list?.highlights || []).map((h) => ({ name: h.name, count: h.ranges.length })),
        scannedAt: st.list?.scannedAt || null,
      });
    }
    return undefined;
  });
  document.addEventListener("yt-navigate-finish", () => onNavigate());
  window.addEventListener("popstate", () => onNavigate());
  setInterval(ensure, 1000);
  onNavigate();
})();

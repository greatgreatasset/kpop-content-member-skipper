// Loads skip lists for the selected profile (remote base URL first, then the
// bundled copy), caches them, relays the "next highlight" command, and runs
// "Filter this video" scans through the native host (only on the PC where
// install_native_host.bat was run).
"use strict";

const DEFAULT_BASE =
  "https://raw.githubusercontent.com/greatgreatasset/kpop-content-member-skipper/master/skiplists/";
const DEFAULTS = {
  enabled: true,
  highlights: true,
  profile: "hearts2hearts",
  baseUrl: DEFAULT_BASE,
  cacheMinutes: 30,
};
const ID_RE = /^[A-Za-z0-9_-]{11}$/;
const PROFILE_RE = /^[a-z0-9][a-z0-9_-]*$/;
const bundledIndex = new Map(); // profile -> Set(videoIds)
let bundledProfiles = null;

async function getSettings() {
  return { ...DEFAULTS, ...(await browser.storage.local.get(Object.keys(DEFAULTS))) };
}

async function getJson(path) {
  try {
    const r = await fetch(browser.runtime.getURL(path));
    return r.ok ? await r.json() : null;
  } catch (e) {
    return null;
  }
}

async function getProfiles() {
  if (!bundledProfiles) bundledProfiles = (await getJson("skiplists/profiles.json")) || [];
  return bundledProfiles;
}

async function getBundledIndex(profile) {
  if (!bundledIndex.has(profile)) {
    const idx = await getJson(`skiplists/${profile}/index.json`);
    bundledIndex.set(profile, new Set(Object.keys((idx && idx.videos) || {})));
  }
  return bundledIndex.get(profile);
}

async function loadBundled(profile, id) {
  if (!(await getBundledIndex(profile)).has(id)) return null;
  return getJson(`skiplists/${profile}/${id}.json`);
}

// -> {status: "ok", data} | {status: "missing"}; throws on network/server errors
async function loadRemote(baseUrl, profile, id) {
  const url = baseUrl.replace(/\/*$/, "/") + `${profile}/${encodeURIComponent(id)}.json`;
  const r = await fetch(url, { cache: "no-cache" });
  if (r.status === 404) return { status: "missing" };
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return { status: "ok", data: await r.json() };
}

async function getList(id) {
  const r = await getListOnline(id);
  const key = `scanned:${r.profile}:${id}`;
  const mine = ID_RE.test(id) ? (await browser.storage.local.get(key))[key] : null;
  // a list just scanned on this PC wins until GitHub serves the same or a newer one
  if (mine && (!r.data || String(r.data.scannedAt || "") < String(mine.scannedAt || ""))) {
    return { data: mine, source: "scanned on this PC", profile: r.profile };
  }
  return r;
}

async function getListOnline(id) {
  const s = await getSettings();
  const profile = PROFILE_RE.test(s.profile) ? s.profile : DEFAULTS.profile;
  if (!ID_RE.test(id)) return { data: null, source: "none", profile };
  const key = `cache:${profile}:${id}`;
  const bundled = async () => {
    const b = await loadBundled(profile, id);
    return { data: b, source: b ? "bundled" : "none", profile };
  };
  if (s.baseUrl) {
    const c = (await browser.storage.local.get(key))[key];
    // a cached "not scanned" expires after a minute, so a newly published
    // skip list shows up on the next page load
    const ttl = c && c.data ? s.cacheMinutes * 60000 : Math.min(60000, s.cacheMinutes * 60000);
    if (c && c.baseUrl === s.baseUrl && Date.now() - c.at < ttl) {
      return c.data ? { data: c.data, source: "online (cached)", profile } : bundled();
    }
    try {
      const r = await loadRemote(s.baseUrl, profile, id);
      const data = r.status === "ok" ? r.data : null;
      await browser.storage.local.set({ [key]: { at: Date.now(), baseUrl: s.baseUrl, data } });
      if (data) return { data, source: "online", profile };
    } catch (e) {
      console.warn("[kms] online skip list failed, using bundled copy:", e);
    }
  }
  return bundled();
}

async function clearCache() {
  const all = await browser.storage.local.get(null);
  await browser.storage.local.remove(Object.keys(all).filter((k) => k.startsWith("cache:")));
}

// ------------------------------------------------------------ scanning
const HOST = "kpop_content_member_skipper";
const jobs = new Map(); // videoId -> {videoId, profile, state, stage, percent, eta, ...}
let hostOk = null;      // null = not checked yet, true/false = native host installed
let hostCheckedAt = 0;

function broadcast(job) {
  const msg = { type: "scanUpdate", job };
  browser.runtime.sendMessage(msg).catch(() => {}); // the popup, if open
  browser.tabs.query({}).then((tabs) => {
    for (const t of tabs) browser.tabs.sendMessage(t.id, msg).catch(() => {});
  });
}

async function finishJob(job, msg) {
  const list = msg.list;
  if (list && list.videoId === job.videoId) {
    const p = list.profile || job.profile;
    await browser.storage.local.set({
      [`scanned:${p}:${list.videoId}`]: list,
      [`cache:${p}:${list.videoId}`]: { at: Date.now(), baseUrl: (await getSettings()).baseUrl, data: list },
    });
  }
  Object.assign(job, { state: "done", stage: "done", percent: 100, eta: 0, note: msg.note || "",
                       pushed: !!msg.pushed, list });
}

// One native connection per scan/attach request. Resolves with the first answer
// (hello + progress/idle), keeps relaying progress until the host finishes.
function talk(request, onFirst) {
  let answered = false;
  const vid = request.videoId;
  const answer = (v) => {
    if (!answered) {
      answered = true;
      onFirst(v);
    }
  };
  let port;
  try {
    port = browser.runtime.connectNative(HOST);
  } catch (e) {
    hostOk = false;
    hostCheckedAt = Date.now();
    answer(null);
    return;
  }
  let hello = false;
  port.onMessage.addListener(async (m) => {
    if (m.type === "hello") {
      hello = true;
      hostOk = true;
      hostCheckedAt = Date.now();
      return;
    }
    if (m.videoId !== vid) return;
    if (m.type === "idle") {
      port.disconnect();
      answer(null);
      return;
    }
    let job = jobs.get(vid);
    if (!job) {
      job = { videoId: vid, profile: request.profile || null, state: "running", stage: "starting",
              percent: 0, eta: null, startedAt: Date.now() };
      jobs.set(vid, job);
    }
    if (m.type === "progress") {
      Object.assign(job, { state: "running", stage: m.stage, percent: m.percent, eta: m.eta });
    } else if (m.type === "done") {
      await finishJob(job, m);
      port.disconnect();
    } else if (m.type === "error") {
      Object.assign(job, { state: "error", error: m.message || "Scan failed." });
      port.disconnect();
    }
    answer(job);
    broadcast(job);
  });
  port.onDisconnect.addListener((p) => {
    if (!hello) {
      // not installed (or not allowed): no button, no error shown
      hostOk = false;
      hostCheckedAt = Date.now();
      answer(null);
      return;
    }
    answer(jobs.get(vid) || null);
    const job = jobs.get(vid);
    if (job && job.state === "running") {
      Object.assign(job, { state: "error", error: "Lost the connection to the scanner." });
      if (p.error) console.warn("[kms] native host:", p.error.message);
      broadcast(job);
    }
  });
  port.postMessage(request);
}

const ask = (request) => new Promise((resolve) => talk(request, resolve));

async function hostAvailable() {
  // re-check a "not installed" answer after a minute, so installing it needs no reload
  if (hostOk === false && Date.now() - hostCheckedAt > 60000) hostOk = null;
  if (hostOk !== null) return hostOk;
  await ask({ type: "attach", videoId: "___________" }); // valid-looking id, never a job
  return !!hostOk;
}

const publicJob = (j) => j && { videoId: j.videoId, profile: j.profile, state: j.state, stage: j.stage,
                                percent: j.percent, eta: j.eta, error: j.error || null,
                                note: j.note || "", pushed: !!j.pushed };

async function scanState(id) {
  if (!ID_RE.test(id)) return { available: false, job: null };
  let job = jobs.get(id);
  if (!job && hostOk !== false) {
    // after a background restart: follow a scan that is still running on this PC
    job = await ask({ type: "attach", videoId: id });
  }
  return { available: await hostAvailable(), job: publicJob(job) };
}

async function startScan(id, rescan) {
  if (!ID_RE.test(id)) return { available: false, job: null };
  const cur = jobs.get(id);
  if (cur && cur.state === "running") return { available: true, job: publicJob(cur) };
  const s = await getSettings();
  const profile = PROFILE_RE.test(s.profile) ? s.profile : DEFAULTS.profile;
  const job = { videoId: id, profile, state: "running", stage: "starting", percent: 0, eta: null,
                startedAt: Date.now() };
  jobs.set(id, job);
  broadcast(job);
  const first = await ask({ type: "scan", videoId: id, profile, rescan: !!rescan });
  if (!first && !hostOk) {
    jobs.delete(id);
    return { available: false, job: null };
  }
  return { available: true, job: publicJob(jobs.get(id)) };
}

browser.runtime.onMessage.addListener((msg) => {
  if (!msg) return undefined;
  if (msg.type === "getList") return getList(msg.id);
  if (msg.type === "scanState") return scanState(msg.id);
  if (msg.type === "startScan") return startScan(msg.id, msg.rescan);
  if (msg.type === "clearCache") return clearCache();
  if (msg.type === "profiles") return getProfiles();
  if (msg.type === "defaults") return Promise.resolve(DEFAULTS);
  return undefined;
});

browser.commands.onCommand.addListener(async (cmd) => {
  if (cmd !== "next-highlight") return;
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (tab) browser.tabs.sendMessage(tab.id, { type: "nextHighlight" }).catch(() => {});
});

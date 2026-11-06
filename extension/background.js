// Loads skip lists (remote base URL first, then the bundled copy), caches
// them, relays the "next highlight" command, and runs "Filter this video" scans
// through the native host (only on the PC where install_native_host.bat was run).
// One skip list per video, for whichever groups are in it: <base>/<videoId>.json.
// Lists published before 0.5.0 live at <base>/<profile>/<videoId>.json and are
// still read as a fallback.
"use strict";

const DEFAULT_BASE =
  "https://raw.githubusercontent.com/greatgreatasset/kpop-content-member-skipper/master/skiplists/";
const DEFAULTS = {
  enabled: true,
  highlights: true,
  baseUrl: DEFAULT_BASE,
  cacheMinutes: 30,
};
const ID_RE = /^[A-Za-z0-9_-]{11}$/;
const PROFILE_RE = /^[a-z0-9][a-z0-9_-]*$/;
const LEGACY_PROFILES = ["hearts2hearts"]; // groups that had per-group folders before 0.5.0
let bundledIndex = null; // Set(videoIds)
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

async function getBundledProfiles() {
  if (!bundledProfiles) bundledProfiles = (await getJson("skiplists/profiles.json")) || [];
  return bundledProfiles;
}

// a usable profiles.json: [{id, group, members: [{id, name, default}]}]
const validProfiles = (p) => Array.isArray(p) && p.length > 0 && p.every((g) =>
  g && PROFILE_RE.test(String(g.id)) && Array.isArray(g.members) &&
  g.members.every((m) => m && typeof m.id === "string" && m.id));

// The groups and their members' defaults: <base>/profiles.json (so a group added
// later shows up without a new add-on version), cached like skip lists, else the
// bundled copy. Users' choices are stored per member key, so they are kept;
// members new to the list get their profile's default.
async function getProfiles() {
  const s = await getSettings();
  if (!s.baseUrl) return getBundledProfiles();
  const key = "cache:profiles";
  const c = (await browser.storage.local.get(key))[key];
  const cached = c && c.baseUrl === s.baseUrl && validProfiles(c.data) ? c : null;
  if (cached && Date.now() - cached.at < s.cacheMinutes * 60000) return cached.data;
  try {
    const r = await fetchList(s.baseUrl.replace(/\/*$/, "/") + "profiles.json");
    if (r.status === "ok" && validProfiles(r.data)) {
      await browser.storage.local.set({ [key]: { at: Date.now(), baseUrl: s.baseUrl, data: r.data } });
      return r.data;
    }
  } catch (e) {
    console.warn("[kms] online profiles.json failed, using bundled copy:", e);
  }
  return cached ? cached.data : getBundledProfiles(); // an older online copy beats the bundled one
}

async function loadBundled(id) {
  if (!bundledIndex) {
    const idx = await getJson("skiplists/index.json");
    bundledIndex = new Set(Object.keys((idx && idx.videos) || {}));
  }
  return bundledIndex.has(id) ? getJson(`skiplists/${id}.json`) : null;
}

// -> {status: "ok", data} | {status: "missing"}; throws on network/server errors
async function fetchList(url) {
  const r = await fetch(url, { cache: "no-cache" });
  if (r.status === 404) return { status: "missing" };
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return { status: "ok", data: await r.json() };
}

async function loadRemote(baseUrl, id) {
  const base = baseUrl.replace(/\/*$/, "/");
  const r = await fetchList(base + `${encodeURIComponent(id)}.json`);
  if (r.status === "ok") return r;
  // fallback: the per-group path used before 0.5.0
  for (const pid of LEGACY_PROFILES) {
    const old = await fetchList(base + `${pid}/${encodeURIComponent(id)}.json`);
    if (old.status === "ok") return old;
  }
  return r;
}

async function getList(id) {
  const r = await getListOnline(id);
  const key = `scanned:${id}`;
  const mine = ID_RE.test(id) ? (await browser.storage.local.get(key))[key] : null;
  // a list just scanned on this PC wins until GitHub serves the same or a newer one
  if (mine && (!r.data || String(r.data.scannedAt || "") < String(mine.scannedAt || ""))) {
    return { data: mine, source: "scanned on this PC" };
  }
  return r;
}

async function getListOnline(id) {
  const s = await getSettings();
  if (!ID_RE.test(id)) return { data: null, source: "none" };
  const key = `cache:${id}`;
  const bundled = async () => {
    const b = await loadBundled(id);
    return { data: b, source: b ? "bundled" : "none" };
  };
  if (s.baseUrl) {
    const c = (await browser.storage.local.get(key))[key];
    // a cached "not scanned" expires after a minute, so a newly published
    // skip list shows up on the next page load
    const ttl = c && c.data ? s.cacheMinutes * 60000 : Math.min(60000, s.cacheMinutes * 60000);
    if (c && c.baseUrl === s.baseUrl && Date.now() - c.at < ttl) {
      return c.data ? { data: c.data, source: "online (cached)" } : bundled();
    }
    try {
      const r = await loadRemote(s.baseUrl, id);
      const data = r.status === "ok" ? r.data : null;
      await browser.storage.local.set({ [key]: { at: Date.now(), baseUrl: s.baseUrl, data } });
      if (data) return { data, source: "online" };
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
    await browser.storage.local.set({
      [`scanned:${list.videoId}`]: list,
      [`cache:${list.videoId}`]: { at: Date.now(), baseUrl: (await getSettings()).baseUrl, data: list },
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

async function startScan(id, rescan, only) {
  if (!ID_RE.test(id)) return { available: false, job: null };
  const cur = jobs.get(id);
  if (cur && cur.state === "running") return { available: true, job: publicJob(cur) };
  // every group is looked for; "profile" only limits a scan (not used by the UI)
  const profile = PROFILE_RE.test(String(only || "")) ? only : null;
  const job = { videoId: id, profile, state: "running", stage: "starting", percent: 0, eta: null,
                startedAt: Date.now() };
  jobs.set(id, job);
  broadcast(job);
  const request = { type: "scan", videoId: id, rescan: !!rescan };
  if (profile) request.profile = profile;
  const first = await ask(request);
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
  if (msg.type === "startScan") return startScan(msg.id, msg.rescan, msg.profile);
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

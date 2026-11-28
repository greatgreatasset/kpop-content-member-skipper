// Loads skip lists (remote base URL first, then the bundled copy), caches
// them, relays the "next highlight" command, and runs "Filter this video" scans
// through the native host (only on the PC where install_native_host.bat was run).
// On other PCs it sends "Request filtering" to that PC through a relay topic.
// One skip list per video, for whichever groups are in it: <base>/<videoId>.json.
// Lists published before 0.5.0 live at <base>/<profile>/<videoId>.json and are
// still read as a fallback.
// Firefox runs this as a background script (compat.js is loaded first by the
// manifest); Chromium browsers run it as the service worker.
"use strict";

if (typeof importScripts === "function") importScripts("compat.js");

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

// fresh: add a query the CDN hasn't seen, so a "not there" answer it cached a
// minute ago doesn't hide a list that was just pushed
async function loadRemote(baseUrl, id, fresh) {
  const base = baseUrl.replace(/\/*$/, "/");
  const r = await fetchList(base + `${encodeURIComponent(id)}.json` + (fresh ? `?t=${Date.now()}` : ""));
  if (r.status === "ok" || fresh) return r;
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
      if (data) {
        browser.storage.local.remove(reqKey(id)); // a request for it is answered
        return { data, source: "online" };
      }
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
    // Chromium reports "host not found" here; reading it keeps that off the error list
    const lastError = browser.runtime.lastError || p.error || null;
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
      if (lastError) console.warn("[kms] native host:", lastError.message);
      broadcast(job);
    }
  });
  try {
    port.postMessage(request);
  } catch (e) {
    answer(null); // already disconnected
  }
}

const ask = (request) => new Promise((resolve) => talk(request, resolve));

async function hostAvailable() {
  // re-check a "not installed" answer after a minute, so installing it needs no reload
  if (hostOk === false && Date.now() - hostCheckedAt > 60000) hostOk = null;
  if (hostOk !== null) return hostOk;
  await ask({ type: "attach", videoId: "___________" }); // valid-looking id, never a job
  return !!hostOk;
}

const publicJob = (j) => j && { videoId: j.videoId, profile: j.profile || null, state: j.state, stage: j.stage,
                                percent: j.percent, eta: j.eta, error: j.error || null,
                                note: j.note || "", pushed: !!j.pushed,
                                remote: !!j.remote, ahead: j.ahead || 0, waited: j.waited || 0 };

// -> {available, job, request}: "available" = the scanner is on this PC ("Filter
// this video"); otherwise "request" says whether a scan can be asked for
// ({ok, hasCode}) and job is the progress of the request, if one was sent.
async function scanState(id) {
  if (!ID_RE.test(id)) return { available: false, job: null };
  const setup = await requestSetup();
  if (!setup.force) {
    let job = jobs.get(id);
    if (!job && hostOk !== false) {
      // after a background restart: follow a scan that is still running on this PC
      job = await ask({ type: "attach", videoId: id });
    }
    if (await hostAvailable()) return { available: true, job: publicJob(job) };
  }
  return { available: false, job: publicJob(await remoteJob(id, setup)), request: requestInfo(setup) };
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

// ------------------------------------------------------------ requesting a scan
// PCs without the scanner (friends' PCs) ask the PC that has it. The video ID is
// posted to a topic on a public relay (ntfy.sh); the topic's name is the
// "request code", which the scanner's owner hands out and each user types in
// once (Settings or the "Request filtering" button). It is never part of the
// add-on. request_watcher.py on the scanner PC listens on that topic, scans the
// video, pushes the skip list to GitHub as usual and posts its progress back:
//   request  {"k": "req", "v": "<videoId>"}
//   status   {"k": "st", "v": "<videoId>", "s": "queued|scanning|upload|done|error",
//             "p": percent, "e": eta seconds, "q": videos ahead in line, "m": "message"}
// Nothing but video IDs and these statuses goes through the relay.
const RELAY = "https://ntfy.sh/";
const CODE_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"; // no i l o 0 1
const CODE_LEN = 20;                // 18 random characters + 2 check characters
const REQ_KEEP = 12 * 3600e3;       // the relay keeps a request for 12 hours
const REQ_STALE = 2 * 3600e3;       // an accepted request with no news for this long is dropped
const REQ_POLL = 8000;
const REQ_STATES = ["queued", "scanning", "upload", "done", "error"];
const reqKey = (id) => `req:${id}`;
let relayPolledAt = 0;
let relayPolling = null;

const cleanCode = (c) => String(c || "").toLowerCase().replace(/[^a-z0-9]/g, "");

// the last two characters check the rest, so a mistyped code is noticed here
// instead of sending requests nobody will ever see
function validCode(code) {
  if (code.length !== CODE_LEN) return false;
  let sum = 0;
  for (let i = 0; i < CODE_LEN; i++) {
    const k = CODE_CHARS.indexOf(code[i]);
    if (k < 0) return false;
    if (i < CODE_LEN - 2) sum += (i + 1) * k;
  }
  const n = CODE_CHARS.length;
  sum %= n * n;
  return code[CODE_LEN - 2] === CODE_CHARS[Math.floor(sum / n)] && code[CODE_LEN - 1] === CODE_CHARS[sum % n];
}

async function requestSetup() {
  const s = await browser.storage.local.get(["requestCode", "relayUrl", "forceRequest", "baseUrl"]);
  const code = cleanCode(s.requestCode);
  return {
    code: validCode(code) ? code : "",
    // relayUrl / forceRequest are not on the settings page (tests; forceRequest
    // also shows the scanner PC what the other PCs see)
    relay: /^https?:\/\//.test(s.relayUrl || "") ? s.relayUrl.replace(/\/*$/, "/") : RELAY,
    force: s.forceRequest === true,
    baseUrl: "baseUrl" in s ? s.baseUrl : DEFAULT_BASE,
  };
}

// a finished scan arrives as a skip list from the base URL: no base URL, no requests
const requestInfo = (setup) => ({ ok: !!setup.baseUrl, hasCode: !!setup.code });

async function setRequestCode(code) {
  const c = cleanCode(code);
  if (!c) {
    await browser.storage.local.remove(["requestCode", "reqSince"]);
    return { ok: true, code: "" };
  }
  if (!validCode(c)) return { ok: false };
  const cur = (await browser.storage.local.get("requestCode")).requestCode;
  if (cur !== c) {
    await browser.storage.local.remove("reqSince");
    await browser.storage.local.set({ requestCode: c });
  }
  return { ok: true, code: c };
}

function requestJob(rec) {
  if (!rec) return null;
  const job = { videoId: rec.videoId, remote: true, state: "running", stage: rec.state,
                percent: rec.percent || 0, eta: rec.eta || null, ahead: rec.ahead || 0,
                waited: Math.max(0, Math.round((Date.now() - rec.at) / 1000)) };
  if (rec.state === "error") {
    Object.assign(job, { state: "error", error: rec.message || "The scanner PC couldn't filter this video." });
  }
  return job;
}

// Read the statuses posted since the last look and note them on our requests.
async function pollRelay(setup) {
  if (!setup.code) return undefined;
  if (relayPolling) return relayPolling;
  if (Date.now() - relayPolledAt < REQ_POLL) return undefined;
  relayPolledAt = Date.now();
  relayPolling = (async () => {
    const stored = (await browser.storage.local.get("reqSince")).reqSince;
    const since = /^[A-Za-z0-9]{6,24}$/.test(stored || "") ? stored : "12h";
    const r = await fetch(`${setup.relay}kms-${setup.code}/json?poll=1&since=${since}`, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const latest = new Map(); // videoId -> its newest status
    let last = null;
    for (const line of (await r.text()).split("\n")) {
      let m;
      let body;
      try {
        m = JSON.parse(line);
        if (m.event !== "message") continue;
        last = m.id;
        body = JSON.parse(m.message);
      } catch (e) {
        continue;
      }
      if (body && body.k === "st" && ID_RE.test(String(body.v)) && REQ_STATES.includes(body.s)) {
        latest.set(body.v, { ...body, time: Number(m.time) || 0 });
      }
    }
    for (const [id, u] of latest) {
      const key = reqKey(id);
      const rec = (await browser.storage.local.get(key))[key];
      if (!rec || u.time < rec.reqTime) continue; // not ours, or the answer to an earlier request
      Object.assign(rec, {
        state: u.s, updated: Date.now(),
        percent: Math.max(0, Math.min(100, Math.round(Number(u.p) || 0))),
        eta: Number(u.e) > 0 ? Math.round(Number(u.e)) : null,
        ahead: Number(u.q) > 0 ? Math.round(Number(u.q)) : 0,
        message: typeof u.m === "string" ? u.m.slice(0, 200) : "",
      });
      await browser.storage.local.set({ [key]: rec });
    }
    if (last) await browser.storage.local.set({ reqSince: last });
  })().catch((e) => console.warn("[kms] could not read the request statuses:", e))
    .finally(() => {
      relayPolling = null;
    });
  return relayPolling;
}

// The progress of this PC's request for a video, or null. Stores the skip list
// once the scanner PC has pushed it ("done").
async function remoteJob(id, setup) {
  const key = reqKey(id);
  const get = async () => (await browser.storage.local.get(key))[key] || null;
  let rec = await get();
  if (!rec) return null;
  if (rec.state !== "done" && rec.state !== "error") {
    await pollRelay(setup);
    rec = await get();
    if (!rec) return null;
  }
  const now = Date.now();
  if (rec.state === "error") {
    if (now - rec.updated < 10 * 60000) return requestJob(rec);
    await browser.storage.local.remove(key); // the button comes back
    return null;
  }
  // "done": the list is on GitHub. While still waiting, look now and then too,
  // in case a status got lost.
  if (setup.baseUrl && now - (rec.triedAt || rec.at) > (rec.state === "done" ? REQ_POLL : 60000)) {
    rec.triedAt = now;
    try {
      const r = await loadRemote(setup.baseUrl, id, true);
      if (r.status === "ok" && r.data && r.data.videoId === id) {
        await browser.storage.local.set({ [`cache:${id}`]: { at: now, baseUrl: setup.baseUrl, data: r.data } });
        await browser.storage.local.remove(key);
        return { videoId: id, remote: true, state: "done", stage: "done", percent: 100, eta: 0, note: "" };
      }
    } catch (e) {
      console.warn("[kms] could not fetch the requested skip list yet:", e);
    }
    await browser.storage.local.set({ [key]: rec });
  }
  const quiet = now - rec.updated;
  if ((rec.state === "sent" && quiet > REQ_KEEP) || (rec.state !== "sent" && quiet > REQ_STALE)) {
    await browser.storage.local.remove(key); // the request is gone; the button comes back
    return null;
  }
  return requestJob(rec.state === "done" ? { ...rec, state: "publishing" } : rec);
}

// "Request filtering". code: given when the user was just asked for it.
async function requestScan(id, code) {
  if (!ID_RE.test(id)) return { available: false, job: null };
  if (code !== undefined && code !== null) {
    const r = await setRequestCode(code);
    if (!r.ok || !r.code) {
      return { available: false, job: null, badCode: true, request: requestInfo(await requestSetup()) };
    }
  }
  const setup = await requestSetup();
  const request = requestInfo(setup);
  if (!setup.code || !request.ok) return { available: false, job: null, request };
  const cur = await remoteJob(id, setup);
  if (cur && cur.state !== "error") return { available: false, job: publicJob(cur), request };
  let rec;
  try {
    const r = await fetch(`${setup.relay}kms-${setup.code}`,
                          { method: "POST", body: JSON.stringify({ k: "req", v: id }) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const m = await r.json();
    rec = { videoId: id, at: Date.now(), updated: Date.now(), state: "sent",
            reqTime: Number(m.time) || Math.floor(Date.now() / 1000) - 5 };
    await browser.storage.local.set({ [reqKey(id)]: rec });
    relayPolledAt = 0; // look for the answer on the next ask
  } catch (e) {
    console.warn("[kms] could not send the scan request:", e);
    return { available: false, request,
             job: publicJob({ videoId: id, remote: true, state: "error", stage: "sent", percent: 0, eta: null,
                              error: "Couldn't send the request. Check your internet connection and try again." }) };
  }
  return { available: false, job: publicJob(requestJob(rec)), request };
}

kmsOnMessage((msg) => {
  if (!msg) return undefined;
  if (msg.type === "requestScan") return requestScan(msg.id, msg.code);
  if (msg.type === "setRequestCode") return setRequestCode(msg.code);
  if (msg.type === "requestSetup") return requestSetup().then((s) => ({ code: s.code, ...requestInfo(s) }));
  if (msg.type === "getList") return getList(msg.id);
  if (msg.type === "scanState") return scanState(msg.id);
  if (msg.type === "startScan") return startScan(msg.id, msg.rescan, msg.profile);
  if (msg.type === "clearCache") return clearCache().then(() => true);
  if (msg.type === "profiles") return getProfiles();
  if (msg.type === "defaults") return DEFAULTS;
  return undefined;
});

browser.commands.onCommand.addListener(async (cmd) => {
  if (cmd !== "next-highlight") return;
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (tab) browser.tabs.sendMessage(tab.id, { type: "nextHighlight" }).catch(() => {});
});

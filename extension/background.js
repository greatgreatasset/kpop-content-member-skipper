// Loads skip lists for the selected profile (remote base URL first, then the
// bundled copy), caches them, and relays the "next highlight" command.
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

browser.runtime.onMessage.addListener((msg) => {
  if (!msg) return undefined;
  if (msg.type === "getList") return getList(msg.id);
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

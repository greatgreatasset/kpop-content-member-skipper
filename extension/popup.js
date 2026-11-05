"use strict";

const $ = (id) => document.getElementById(id);
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

// [{name, group}] -> "Carmen & Ye-on (Hearts2Hearts), Yunah (ILLIT)"
function byGroup(members) {
  const groups = new Map();
  for (const m of members) {
    if (!groups.has(m.group)) groups.set(m.group, []);
    groups.get(m.group).push(m.name);
  }
  return [...groups].map(([g, names]) => `${names.join(" & ")}${g ? ` (${g})` : ""}`).join(", ");
}

async function init() {
  const defaults = await browser.runtime.sendMessage({ type: "defaults" });
  const s = { ...defaults, ...(await browser.storage.local.get(["enabled", "highlights"])) };
  for (const k of ["enabled", "highlights"]) {
    $(k).checked = s[k];
    $(k).addEventListener("change", () => browser.storage.local.set({ [k]: $(k).checked }));
  }
  const profiles = await browser.runtime.sendMessage({ type: "profiles" });
  const choices = (await browser.storage.local.get("choices")).choices || {};
  // every group's members with this user's choice (settings page), else the default
  const all = (kind) => profiles.flatMap((p) => (p.members || [])
    .filter((m) => (choices[KmsRules.keyOf(p.id, m.id)] || m.default) === kind)
    .map((m) => ({ name: m.name, group: p.group })));
  $("options").addEventListener("click", (e) => {
    e.preventDefault();
    browser.runtime.openOptionsPage();
    window.close();
  });

  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  let status = null;
  try {
    status = await browser.tabs.sendMessage(tab.id, { type: "status" });
  } catch (e) {
    /* not a YouTube tab */
  }
  // tests open popup.html in a tab of its own and name the video with ?video=
  const q = new URLSearchParams(location.search);
  if (q.get("video")) {
    const hlWhen = (await browser.storage.local.get("hlWhen")).hlWhen || "any";
    status = await testStatus(q.get("video"), q.get("rescan") === "1", profiles, choices, hlWhen);
  }
  if (status && status.videoId) initScan(status.videoId, status.scanned);
  // what is skipped: this video's groups once it is scanned, else every group's
  const scanned = status && status.videoId && status.scanned;
  const skipping = scanned ? status.skipMembers || [] : all("skip");
  const hl = scanned ? status.highlights.filter((h) => h.count) : all("highlight");
  if (scanned && !(status.groups || []).length && !skipping.length) {
    $("profile").textContent = "No known group in this video, nothing to skip.";
  } else if (!skipping.length) {
    $("profile").textContent = scanned ? "Nobody in this video is skipped." : "Nobody is skipped.";
  } else {
    $("profile").textContent = `${scanned ? "Skipping" : "Skips"} ${byGroup(skipping)}`;
  }
  $("hlLabel").textContent = hl.length ? `${[...new Set(hl.map((h) => h.name))].join(" & ")} highlights`
    : "Highlights";
  const el = $("status");
  if (!status || !status.videoId) {
    el.textContent = "Open a YouTube video to see its skip list.";
  } else if (status.loading) {
    el.textContent = "Loading skip list…";
  } else if (!status.scanned) {
    el.textContent = "This video is not scanned yet.";
  } else {
    const parts = [`${plural(status.skips, "skip")} (${status.skipSeconds} s)`];
    for (const h of status.highlights) parts.push(plural(h.count, `${h.name} part`));
    el.textContent = `${parts.join(" · ")}  [${status.source}]`;
    if (status.stats) el.textContent += `
${status.stats}`;
    const total = status.highlights.reduce((a, h) => a + h.count, 0);
    if (total) {
      const names = status.highlights.filter((h) => h.count).map((h) => h.name);
      $("nextHl").textContent = `Jump to next ${names.length === 1 ? names[0] : "highlighted"} part`;
      $("nextHl").hidden = false;
      $("nextHl").addEventListener("click", () => {
        browser.tabs.sendMessage(tab.id, { type: "nextHighlight" });
        window.close();
      });
    }
  }
}

// popup.html?video=<id> (tests): the status the content script would report
async function testStatus(videoId, rescan, profiles, choices, hlWhen) {
  const r = await browser.runtime.sendMessage({ type: "getList", id: videoId });
  let d = r && r.data;
  if (!d || rescan) return { videoId, scanned: rescan, loading: false };
  const v = KmsRules.forList(d, profiles, choices, hlWhen); // this user's choices, as the content script does
  if (v) d = { ...d, ...v };
  const hl = (d.highlights || []).filter((h) => h.ranges && h.ranges.length);
  const members = (d.skipMembers || []).map((m) => (typeof m === "string" ? { name: m, group: d.group } : m));
  return {
    videoId, scanned: true, loading: false, source: r.source, skipMembers: members,
    groups: (d.groups || []).map((g) => (g && g.name) || g),
    skips: d.skips.length, skipSeconds: Math.round(d.skips.reduce((a, s) => a + s.end - s.start, 0)),
    highlights: hl.map((h) => ({ name: h.name, count: h.ranges.length })),
  };
}

const fmtTime = (t) => {
  t = Math.max(0, Math.round(t));
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
};

// "Filter this video": only where the native scanner is installed (Kirk's PC)
async function initScan(videoId, scanned) {
  let state;
  try {
    state = await browser.runtime.sendMessage({ type: "scanState", id: videoId });
  } catch (e) {
    return;
  }
  const btn = $("scanBtn");
  const bar = $("scanBar");
  const text = $("scanText");
  $("scan").hidden = false;
  if (!state.available && !(state.job && state.job.state === "running")) {
    text.textContent = "Scanning is only available on the PC with the scanner installed.";
    return;
  }
  btn.textContent = scanned ? "Filter this video again" : "Filter this video";
  const show = (job) => {
    const running = job && job.state === "running";
    btn.hidden = running;
    bar.hidden = !running;
    if (running) {
      bar.value = job.percent || 0;
      text.textContent = job.stage === "upload" ? `Filtering… ${job.percent}%, uploading`
        : `Filtering… ${job.percent}%${job.eta ? `, about ${fmtTime(job.eta)} left` : ""}`;
    } else if (job && job.state === "done") {
      text.textContent = `Done.${job.note ? " " + job.note : ""} Skips are applied.`;
      btn.textContent = "Filter this video again";
    } else if (job && job.state === "error") {
      text.textContent = `Filtering failed: ${job.error}`;
    } else {
      text.textContent = "";
    }
  };
  show(state.job);
  browser.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "scanUpdate" && msg.job.videoId === videoId) show(msg.job);
  });
  btn.addEventListener("click", async () => {
    show({ videoId, state: "running", stage: "starting", percent: 0 });
    const r = await browser.runtime.sendMessage({ type: "startScan", id: videoId, rescan: scanned });
    if (r && r.job) show(r.job);
    else text.textContent = "Scanning is only available on the PC with the scanner installed.";
  });
}

init();

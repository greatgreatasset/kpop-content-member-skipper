"use strict";

const $ = (id) => document.getElementById(id);
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

async function init() {
  const defaults = await browser.runtime.sendMessage({ type: "defaults" });
  const s = { ...defaults, ...(await browser.storage.local.get(["enabled", "highlights", "profile"])) };
  for (const k of ["enabled", "highlights"]) {
    $(k).checked = s[k];
    $(k).addEventListener("change", () => browser.storage.local.set({ [k]: $(k).checked }));
  }
  const profiles = await browser.runtime.sendMessage({ type: "profiles" });
  const prof = profiles.find((p) => p.id === s.profile);
  if (prof) {
    $("profile").textContent = `${prof.group}: skipping ${prof.skip.join(" & ")}`;
    if (prof.highlight.length) $("hlLabel").textContent = `${prof.highlight.join(" & ")} highlights`;
  } else {
    $("profile").textContent = `Profile: ${s.profile}`;
  }
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

init();

"use strict";

const $ = (id) => document.getElementById(id);

async function init() {
  const defaults = await browser.runtime.sendMessage({ type: "defaults" });
  const s = { ...defaults, ...(await browser.storage.local.get(["baseUrl", "cacheMinutes"])) };
  const profiles = await browser.runtime.sendMessage({ type: "profiles" });

  // every group is recognized in every video; nothing to pick
  const list = $("groups");
  for (const p of profiles) {
    const li = document.createElement("li");
    li.textContent = `${p.group}: skips ${p.skip.join(", ") || "nobody"}` +
      (p.highlight.length ? `; highlights ${p.highlight.join(", ")}` : "");
    list.appendChild(li);
  }

  $("baseUrl").value = s.baseUrl;
  $("cacheMinutes").value = s.cacheMinutes;
  $("reset").addEventListener("click", (e) => {
    e.preventDefault();
    $("baseUrl").value = defaults.baseUrl;
  });

  $("save").addEventListener("click", async () => {
    const baseUrl = $("baseUrl").value.trim();
    const cacheMinutes = Math.max(0, parseInt($("cacheMinutes").value, 10) || 0);
    if (baseUrl) {
      let u;
      try {
        u = new URL(baseUrl);
      } catch (e) {
        $("msg").textContent = "That is not a valid URL.";
        return;
      }
      // raw.githubusercontent.com is pre-granted; any other host needs permission
      if (u.hostname !== "raw.githubusercontent.com") {
        const ok = await browser.permissions.request({ origins: [`${u.protocol}//${u.host}/*`] });
        if (!ok) {
          $("msg").textContent = `Permission for ${u.host} was not granted.`;
          return;
        }
      }
    }
    await browser.storage.local.set({ baseUrl, cacheMinutes });
    await browser.runtime.sendMessage({ type: "clearCache" });
    $("msg").textContent = "Saved.";
  });

  $("clear").addEventListener("click", async () => {
    await browser.runtime.sendMessage({ type: "clearCache" });
    $("msg").textContent = "Cache cleared.";
  });
}

init();

"use strict";

const $ = (id) => document.getElementById(id);

async function init() {
  const defaults = await browser.runtime.sendMessage({ type: "defaults" });
  const s = { ...defaults, ...(await browser.storage.local.get(["profile", "baseUrl", "cacheMinutes"])) };
  const profiles = await browser.runtime.sendMessage({ type: "profiles" });

  const sel = $("profile");
  for (const p of profiles) {
    const o = document.createElement("option");
    o.value = p.id;
    o.textContent = p.group;
    sel.appendChild(o);
  }
  if (!profiles.some((p) => p.id === s.profile)) {
    const o = document.createElement("option");
    o.value = s.profile;
    o.textContent = s.profile;
    sel.appendChild(o);
  }
  sel.value = s.profile;
  const showInfo = () => {
    const p = profiles.find((x) => x.id === sel.value);
    $("profileInfo").textContent = p
      ? `Skips: ${p.skip.join(", ")}. Highlights: ${p.highlight.join(", ") || "none"}. ` +
        `${p.bundled} video(s) bundled.`
      : "";
  };
  sel.addEventListener("change", showInfo);
  showInfo();

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
    await browser.storage.local.set({ profile: sel.value, baseUrl, cacheMinutes });
    await browser.runtime.sendMessage({ type: "clearCache" });
    $("msg").textContent = "Saved.";
  });

  $("clear").addEventListener("click", async () => {
    await browser.runtime.sendMessage({ type: "clearCache" });
    $("msg").textContent = "Cache cleared.";
  });
}

init();

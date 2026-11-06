"use strict";

const $ = (id) => document.getElementById(id);
const R = globalThis.KmsRules;
const LABELS = { skip: "Skip", normal: "Normal", highlight: "Highlight" };

// storage.local "choices" holds only what differs from the defaults
async function getChoices() {
  return (await browser.storage.local.get("choices")).choices || {};
}

async function setChoice(key, value, def) {
  const choices = await getChoices();
  if (value === def) delete choices[key];
  else choices[key] = value;
  await browser.storage.local.set({ choices });
}

function renderGroups(profiles, choices) {
  const box = $("groups");
  box.replaceChildren();
  for (const p of profiles) {
    const fs = document.createElement("fieldset");
    fs.className = "group";
    fs.dataset.profile = p.id;
    const lg = document.createElement("legend");
    lg.textContent = p.group;
    fs.appendChild(lg);
    for (const m of p.members || []) {
      const key = R.keyOf(p.id, m.id);
      const def = m.default || "normal";
      const cur = R.CHOICES.includes(choices[key]) ? choices[key] : def;
      const row = document.createElement("div");
      row.className = "member";
      row.dataset.key = key;
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = m.name;
      row.appendChild(name);
      const seg = document.createElement("div");
      seg.className = "seg";
      seg.setAttribute("role", "radiogroup");
      seg.setAttribute("aria-label", m.name);
      for (const c of R.CHOICES) {
        const lab = document.createElement("label");
        lab.className = `opt opt-${c}` + (c === def ? " is-default" : "");
        if (c === def) lab.title = "Default";
        const inp = document.createElement("input");
        inp.type = "radio";
        inp.name = key;
        inp.value = c;
        inp.checked = c === cur;
        inp.addEventListener("change", () => {
          if (inp.checked) setChoice(key, c, def);
        });
        lab.append(inp, document.createTextNode(LABELS[c]));
        seg.appendChild(lab);
      }
      row.appendChild(seg);
      fs.appendChild(row);
    }
    box.appendChild(fs);
  }
}

async function init() {
  const defaults = await browser.runtime.sendMessage({ type: "defaults" });
  const s = { ...defaults, ...(await browser.storage.local.get(["baseUrl", "cacheMinutes"])) };
  const profiles = await browser.runtime.sendMessage({ type: "profiles" });

  renderGroups(profiles, await getChoices());
  // another settings tab (or a test) changed them
  browser.storage.onChanged.addListener((ch, area) => {
    if (area === "local" && "choices" in ch) renderGroups(profiles, ch.choices.newValue || {});
  });
  // "Highlight when": any time she's on screen (default, not stored) / only when she's the main focus
  const renderHlWhen = (v) => {
    for (const inp of document.querySelectorAll("input[name='hlWhen']")) inp.checked = inp.value === (v || "any");
  };
  renderHlWhen((await browser.storage.local.get("hlWhen")).hlWhen);
  for (const inp of document.querySelectorAll("input[name='hlWhen']")) {
    inp.addEventListener("change", () => {
      if (!inp.checked) return;
      if (inp.value === "focus") browser.storage.local.set({ hlWhen: "focus" });
      else browser.storage.local.remove("hlWhen");
    });
  }
  browser.storage.onChanged.addListener((ch, area) => {
    if (area === "local" && "hlWhen" in ch) renderHlWhen(ch.hlWhen.newValue);
  });

  // Blur skipped members when they can't be skipped (default off, strength 3, "normal" area)
  const STRENGTH = ["", "Light", "Medium-light", "Medium", "Strong", "Very strong"];
  const renderBlur = (b) => {
    $("blur").checked = b.blur === true;
    const s = Number.isInteger(b.blurStrength) ? b.blurStrength : 3;
    $("blurStrength").value = String(s);
    $("blurStrengthText").textContent = STRENGTH[s] || "";
    $("blurSize").value = ["face", "normal", "large"].includes(b.blurSize) ? b.blurSize : "normal";
    $("blurOpts").classList.toggle("off", !$("blur").checked);
  };
  renderBlur(await browser.storage.local.get(["blur", "blurStrength", "blurSize"]));
  $("blur").addEventListener("change", () => {
    if ($("blur").checked) browser.storage.local.set({ blur: true });
    else browser.storage.local.remove("blur");
  });
  $("blurStrength").addEventListener("input", () => {
    const s = parseInt($("blurStrength").value, 10);
    $("blurStrengthText").textContent = STRENGTH[s] || "";
    if (s === 3) browser.storage.local.remove("blurStrength");
    else browser.storage.local.set({ blurStrength: s });
  });
  $("blurSize").addEventListener("change", () => {
    const v = $("blurSize").value;
    if (v === "normal") browser.storage.local.remove("blurSize");
    else browser.storage.local.set({ blurSize: v });
  });
  browser.storage.onChanged.addListener(async (ch, area) => {
    if (area === "local" && ["blur", "blurStrength", "blurSize"].some((k) => k in ch)) {
      renderBlur(await browser.storage.local.get(["blur", "blurStrength", "blurSize"]));
    }
  });

  $("resetChoices").addEventListener("click", async () => {
    await browser.storage.local.remove("choices");
    renderGroups(profiles, {});
    $("msg").textContent = "Members reset to the defaults.";
  });

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

// End-to-end check of the Chromium build (dist/chromium/, made by
// build_chromium.py) in real headless Chromium, via Playwright.
//
//   python build_chromium.py --no-zip
//   node tests/chromium_test.js [--headed] [--video oVMQTIUI_2c]
//
// Needs `npm i playwright` (+ `npx playwright install chromium`) and ffmpeg.
// It does not open YouTube: www.youtube.com is answered by a stand-in watch
// page with the elements the content script uses (#movie_player, <video>,
// .ytp-progress-bar, .ytp-right-controls, ytd-watch-metadata #title) and a
// generated test video of the skip list's length. So it checks that the add-on
// works in Chromium (service worker, messages, storage, native messaging), not
// YouTube's page; tests/browser_test.py does that in Firefox.
//
// Checks: the extension loads under its fixed ID; settings page lists the
// groups; bundled skip list -> markers, "Ian: N% · Skipped: N%" line, the skip
// + toast + Undo, the Next button, the popup, the content script's status
// answer, skipping on/off; a member choice on the settings page changes the
// open video; blur boxes; in-page navigation; a skip list only available
// online ("Skip list base URL"); "Request filtering" through a stand-in relay;
// and, on Linux/macOS, "Filter this video" through a stand-in native host
// (tests/fake_native_host.js).
"use strict";

const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");
const { chromium } = require("playwright");
const R = require("../extension/rules.js");

const ROOT = path.resolve(__dirname, "..");
const EXT = path.join(ROOT, "dist", "chromium");
const EXT_ID = "fddakmaaelefbchjakeceijlhdjpfejg"; // build_chromium.CHROMIUM_ID
const HOST = "kpop_content_member_skipper";
const args = process.argv.slice(2);
const opt = (name, def) => (args.includes(name) ? args[args.indexOf(name) + 1] : def);
const VIDEO = opt("--video", "oVMQTIUI_2c");
const ONLINE_ID = "onlineOnly0"; // 11 characters, in no bundle
const NATIVE_ID = "nativeScan0";
const REQUEST_ID = "requestMe00";
const readJson = (f) => JSON.parse(fs.readFileSync(f, "utf8"));

const results = [];
function check(name, ok, info = "") {
  results.push([name, !!ok]);
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name} ${info}`);
}

async function wait(fn, timeout = 15000, step = 200) {
  const t0 = Date.now();
  for (;;) {
    try {
      const v = await fn();
      if (v) return v;
    } catch (e) {
      /* not there yet */
    }
    if (Date.now() - t0 > timeout) return null;
    await new Promise((r) => setTimeout(r, step));
  }
}

// a silent test video of the list's length (tiny: 2 frames a second)
function testVideo(seconds) {
  const f = path.join(os.tmpdir(), `kms-test-${Math.round(seconds)}.webm`);
  if (!fs.existsSync(f)) {
    execFileSync("ffmpeg", ["-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=2",
                            "-t", String(seconds), "-c:v", "libvpx", "-b:v", "30k", "-g", "4", f]);
  }
  return fs.readFileSync(f);
}

const WATCH_PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>stand-in</title></head>
<body style="margin:0;background:#111;color:#eee">
<div id="movie_player" style="position:relative;width:960px;height:540px;background:#000">
  <video class="html5-main-video" src="/video.webm" muted style="width:100%;height:100%"></video>
  <div class="ytp-progress-bar" style="position:absolute;left:12px;right:12px;bottom:44px;height:5px;background:#555"></div>
  <div class="ytp-right-controls" style="position:absolute;right:12px;bottom:4px;height:36px"></div>
</div>
<ytd-watch-metadata><div id="title"><h1>Stand-in watch page</h1></div></ytd-watch-metadata>
</body></html>`;

// request code as request_watcher.py makes them: 18 characters + 2 check characters
function newCode() {
  const C = "abcdefghjkmnpqrstuvwxyz23456789";
  let code = "", sum = 0;
  for (let i = 0; i < 18; i++) {
    const k = Math.floor(Math.random() * C.length);
    code += C[k];
    sum += (i + 1) * k;
  }
  sum %= C.length * C.length;
  return code + C[Math.floor(sum / C.length)] + C[sum % C.length];
}

async function main() {
  if (!fs.existsSync(path.join(EXT, "manifest.json"))) {
    console.error("dist/chromium/ is missing: run  python build_chromium.py --no-zip  first.");
    return 2;
  }
  const list = readJson(path.join(ROOT, "skiplists", `${VIDEO}.json`));
  const profiles = readJson(path.join(EXT, "skiplists", "profiles.json"));
  const video = testVideo(list.duration);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "kms-chromium-"));

  // ---- stand-in servers: skip lists "online" + the request relay (CORS, like the real ones)
  const online = new Map(); // "<id>.json" -> text
  const relay = { posts: [], status: null };
  const code = newCode();
  const server = http.createServer((req, res) => {
    const u = new URL(req.url, "http://x");
    res.setHeader("Access-Control-Allow-Origin", "*");
    if (u.pathname.startsWith("/skiplists/")) {
      const body = online.get(u.pathname.slice("/skiplists/".length));
      res.writeHead(body ? 200 : 404, { "Content-Type": "application/json" });
      res.end(body || "{}");
    } else if (u.pathname === `/relay/kms-${code}` && req.method === "POST") {
      let body = "";
      req.on("data", (d) => (body += d));
      req.on("end", () => {
        relay.posts.push(JSON.parse(body));
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ id: "req0000001", time: Math.floor(Date.now() / 1000), event: "message" }));
      });
    } else if (u.pathname === `/relay/kms-${code}/json`) {
      const lines = relay.status
        ? [JSON.stringify({ id: `st${Date.now()}`.slice(0, 12), time: Math.floor(Date.now() / 1000) + 1,
                            event: "message", message: JSON.stringify(relay.status) })]
        : [];
      res.writeHead(200, { "Content-Type": "application/x-ndjson" });
      res.end(lines.join("\n") + "\n");
    } else {
      res.writeHead(404);
      res.end();
    }
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${server.address().port}`;

  // ---- stand-in native host (Linux/macOS: a manifest in the profile folder; Windows needs the registry)
  const native = process.platform !== "win32";
  if (native) {
    const hostList = path.join(tmp, "host-list.json");
    fs.writeFileSync(hostList, JSON.stringify(list));
    const launcher = path.join(tmp, "fake_host.sh");
    fs.writeFileSync(launcher, `#!/bin/sh\nFAKE_HOST_LIST="${hostList}" exec "${process.execPath}" ` +
                               `"${path.join(__dirname, "fake_native_host.js")}" "$@"\n`, { mode: 0o755 });
    const dir = path.join(tmp, "profile", "NativeMessagingHosts");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, `${HOST}.json`), JSON.stringify({
      name: HOST, description: "test", path: launcher, type: "stdio",
      allowed_origins: [`chrome-extension://${EXT_ID}/`],
    }));
  }

  const ctx = await chromium.launchPersistentContext(path.join(tmp, "profile"), {
    channel: "chromium",
    headless: !args.includes("--headed"),
    viewport: { width: 1200, height: 800 },
    args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`,
           "--autoplay-policy=no-user-gesture-required"],
  });
  let rc = 1;
  try {
    await ctx.route("https://www.youtube.com/**", (route) => {
      const req = route.request();
      const u = new URL(req.url());
      if (u.pathname === "/watch") return route.fulfill({ contentType: "text/html", body: WATCH_PAGE });
      if (u.pathname === "/video.webm") {
        const m = /bytes=(\d+)-(\d*)/.exec(req.headers().range || "");
        const start = m ? Number(m[1]) : 0;
        const end = m && m[2] ? Math.min(Number(m[2]), video.length - 1) : video.length - 1;
        return route.fulfill({
          status: m ? 206 : 200, contentType: "video/webm", body: video.subarray(start, end + 1),
          headers: { "Accept-Ranges": "bytes", ...(m ? { "Content-Range": `bytes ${start}-${end}/${video.length}` } : {}) },
        });
      }
      return route.fulfill({ status: 404, body: "" });
    });

    const sw = ctx.serviceWorkers()[0] || (await ctx.waitForEvent("serviceworker", { timeout: 20000 }));
    check("extension loaded as a service worker under its fixed ID", sw.url() === `chrome-extension://${EXT_ID}/background.js`,
          sw.url());
    const ext = (p) => `chrome-extension://${EXT_ID}/${p}`;
    const store = async (page, items) => page.evaluate((i) => chrome.storage.local.set(i), items);

    // ---- settings page
    const settings = await ctx.newPage();
    const errors = [];
    settings.on("pageerror", (e) => errors.push(String(e)));
    await settings.goto(ext("options.html"));
    const groups = await wait(() => settings.$$eval("#groups fieldset legend", (l) => l.length && l.map((x) => x.textContent)));
    check("settings: every group listed", groups && groups.length === profiles.length, JSON.stringify(groups));
    // only the bundled lists for now (no internet needed)
    await store(settings, { baseUrl: "" });

    // ---- the test video, bundled list
    const yt = await ctx.newPage();
    yt.on("pageerror", (e) => errors.push(String(e)));
    await yt.goto(`https://www.youtube.com/watch?v=${VIDEO}`);
    const counts = () => yt.evaluate(() => {
      const b = document.querySelector(".cys-markers");
      return b && [b.querySelectorAll(".cys-skip").length, b.querySelectorAll(".cys-hl").length];
    });
    const stats = () => yt.evaluate(() => document.querySelector("ytd-watch-metadata #title + .cys-stats")?.textContent || "");
    const hl = list.highlights[0];
    const n = await wait(counts);
    check("progress-bar markers", n && n[0] === list.skips.length && n[1] === hl.ranges.length, JSON.stringify(n));
    const pct = (ranges) => Math.round((100 * ranges.reduce((a, r) => a + r.end - r.start, 0)) / list.duration);
    const wantLine = `${hl.name}: ${pct(hl.ranges)}% · Skipped: ${pct(list.skips)}%`;
    check("line under the title", await wait(async () => (await stats()) === wantLine), JSON.stringify(await stats()));
    check("Next button", await wait(() => yt.$eval(".cys-next-btn", (b) => b.textContent)) === `${hl.name} ⏭`);

    const s0 = list.skips[0];
    const seek = (t) => yt.evaluate((x) => {
      const v = document.querySelector("#movie_player video");
      v.currentTime = x;
      return v.play().catch(() => {});
    }, t);
    const now = () => yt.evaluate(() => document.querySelector("#movie_player video").currentTime);
    await seek(s0.start + 0.5);
    const after = await wait(async () => ((await now()) >= s0.end - 0.2 ? now() : null));
    check("skips the first skip range", after && after < s0.end + 5, `at ${after} (range ${s0.start}-${s0.end})`);
    const toast = await wait(() => yt.$eval(".cys-toast.cys-show span", (e) => e.textContent));
    check("toast", toast && toast.startsWith("Skipped "), JSON.stringify(toast));
    await yt.$eval(".cys-toast button", (b) => b.click());
    await new Promise((r) => setTimeout(r, 1500));
    const back = await now();
    check("Undo goes back and stays", back >= s0.start && back < s0.end, `at ${back}`);

    // the content script answers the popup's question (a promise in Firefox, sendResponse here)
    const status = await sw.evaluate(async () => {
      for (const t of await chrome.tabs.query({})) {
        try {
          const r = await chrome.tabs.sendMessage(t.id, { type: "status" });
          if (r) return r;
        } catch (e) {
          /* not a tab of ours */
        }
      }
      return null;
    });
    check("content script answers \"status\"", status && status.videoId === VIDEO && status.scanned &&
          status.skips === list.skips.length && status.source === "bundled", JSON.stringify(status && status.source));

    // popup (as a tab; ?video= names the video, as in browser_test.py)
    const popup = await ctx.newPage();
    popup.on("pageerror", (e) => errors.push(String(e)));
    await popup.goto(ext(`popup.html?video=${VIDEO}`));
    const who = await wait(() => popup.$eval("#profile", (e) => (e.textContent.startsWith("Skipping") ? e.textContent : "")));
    check("popup: who is skipped", who === "Skipping Carmen & Ye-on (Hearts2Hearts)", JSON.stringify(who));
    await popup.$eval("#enabled", (e) => e.click());
    check("popup: skipping off removes the skip markers", await wait(async () => (await counts())[0] === 0));
    check("line follows", await wait(async () => (await stats()) === `${hl.name}: ${pct(hl.ranges)}%`), JSON.stringify(await stats()));
    await popup.$eval("#enabled", (e) => e.click());
    check("popup: skipping on again", await wait(async () => (await counts())[0] === list.skips.length));
    await popup.close();

    // a choice on the settings page changes the open video without a reload
    const key = R.keyOf("hearts2hearts", "ian");
    const want = R.forList(list, profiles, { [key]: "skip" }, "any");
    await settings.$eval(`input[name="${key}"][value="skip"]`, (e) => e.click());
    const changed = await wait(async () => {
      const c = await counts();
      return c && c[0] === want.skips.length ? c : null;
    });
    check("settings: Ian -> Skip changes the open video's skips", changed, `${JSON.stringify(changed)} want ${want.skips.length} skips`);
    await settings.$eval("#resetChoices", (e) => e.click());
    check("settings: Reset to defaults", await wait(async () => (await counts())[0] === list.skips.length));

    // blur: off by default; on, boxes where a skipped member shares the picture
    const blurCount = () => yt.evaluate(() => Number(document.querySelector(".cys-blur-layer")?.dataset.count || 0));
    check("blur: off by default", (await blurCount()) === 0);
    await store(settings, { blur: true });
    let seen = 0;
    for (const t of [120, 121, 122, 123, 124, 125, 119, 118]) { // browser_test.py: about 2:00 of the test video
      await seek(t);
      seen = await wait(blurCount, 2500);
      if (seen) break;
    }
    check("blur: boxes drawn over the video", VIDEO !== "oVMQTIUI_2c" || seen > 0, `${seen} box(es)`);
    await settings.evaluate(() => chrome.storage.local.remove("blur"));
    check("blur: off again", await wait(async () => (await blurCount()) === 0));

    // "next highlight" from the background (what the Alt+Shift+H command sends)
    await seek(0);
    await sw.evaluate(async () => {
      for (const t of await chrome.tabs.query({})) chrome.tabs.sendMessage(t.id, { type: "nextHighlight" }).catch(() => {});
    });
    const first = hl.ranges.find((r) => r.start > 1);
    check("next highlight jumps to her next part", await wait(async () => Math.abs((await now()) - first.start) < 3), `want ${first.start}`);

    // ---- a list that is only online, reached by in-page navigation
    online.set(`${ONLINE_ID}.json`, JSON.stringify({ ...list, videoId: ONLINE_ID }));
    online.set("profiles.json", JSON.stringify(profiles));
    await store(settings, { baseUrl: `${base}/skiplists/`, cacheMinutes: 30 });
    await yt.evaluate((id) => {
      history.pushState({}, "", `/watch?v=${id}`);
      document.dispatchEvent(new Event("yt-navigate-finish"));
    }, ONLINE_ID);
    check("online skip list after in-page navigation", await wait(async () => {
      const c = await counts();
      return c && c[0] === list.skips.length;
    }), JSON.stringify(await counts()));
    const src = await sw.evaluate((id) => new Promise((r) => chrome.storage.local.get(`cache:${id}`, (x) => r(!!x[`cache:${id}`]?.data))), ONLINE_ID);
    check("fetched list is cached", src);

    // ---- "Filter this video" (the scanner PC), through the stand-in native host
    if (native) {
      await yt.goto(`https://www.youtube.com/watch?v=${NATIVE_ID}`);
      const line = await wait(async () => ((await stats()).includes("Filter this video") ? stats() : null), 20000);
      check("unscanned video offers \"Filter this video\"", line === "Not filtered yet · Filter this video", JSON.stringify(await stats()));
      await yt.$eval(".cys-stats .cys-filter", (b) => b.click());
      const prog = await wait(async () => (/^Filtering… \d+%/.test(await stats()) ? stats() : null), 10000, 100);
      check("progress while it scans", prog, JSON.stringify(prog));
      check("scan done: skips applied", await wait(async () => {
        const c = await counts();
        return c && c[0] === list.skips.length;
      }, 20000), JSON.stringify(await counts()));
      const banner = await yt.evaluate(() => document.querySelector(".cys-banner")?.textContent || "");
      check("banner says Done", banner.startsWith("Done:"), JSON.stringify(banner));
    } else {
      console.log("[skip] \"Filter this video\": the stand-in native host is not set up on Windows");
    }

    // ---- "Request filtering" (a friend's PC), through the stand-in relay
    await store(settings, { forceRequest: true, relayUrl: `${base}/relay/`, requestCode: code });
    await yt.goto(`https://www.youtube.com/watch?v=${REQUEST_ID}`);
    const rline = await wait(async () => ((await stats()).includes("Request filtering") ? stats() : null), 20000);
    check("unscanned video offers \"Request filtering\"", rline === "Not filtered yet · Request filtering", JSON.stringify(await stats()));
    await yt.$eval(".cys-stats .cys-filter", (b) => b.click());
    check("request sent to the relay", await wait(() => relay.posts.length === 1 && relay.posts[0].k === "req" &&
          relay.posts[0].v === REQUEST_ID), JSON.stringify(relay.posts));
    check("line: requested", await wait(async () => (await stats()).startsWith("Requested")), JSON.stringify(await stats()));
    relay.status = { k: "st", v: REQUEST_ID, s: "scanning", p: 40, e: 65 };
    check("line follows the scanner PC's progress", await wait(async () => (await stats()).startsWith("Being filtered… 40%"), 40000),
          JSON.stringify(await stats()));
    online.set(`${REQUEST_ID}.json`, JSON.stringify({ ...list, videoId: REQUEST_ID }));
    relay.status = { k: "st", v: REQUEST_ID, s: "done", p: 100 };
    check("requested skip list arrives and is applied", await wait(async () => {
      const c = await counts();
      return c && c[0] === list.skips.length;
    }, 60000), JSON.stringify(await counts()));

    check("no script errors", errors.length === 0, errors.slice(0, 3).join(" | "));
    await yt.screenshot({ path: path.join(tmp, "chromium_test.png") });
    console.log(`screenshot: ${path.join(tmp, "chromium_test.png")}`);
    const failed = results.filter((r) => !r[1]);
    console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
    rc = failed.length ? 1 : 0;
  } finally {
    await ctx.close();
    server.close();
  }
  return rc;
}

main().then((rc) => process.exit(rc), (e) => {
  console.error(e);
  process.exit(1);
});

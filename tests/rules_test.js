// The add-on's rules (extension/rules.js) with the default choices must give
// the scanner's default skips and highlights for every skip list in the repo.
//
//   node tests/rules_test.js                 every skiplists/<videoId>.json
//   node tests/rules_test.js --apply <list.json> <choices.json>   (used by rules_crosscheck.py)
"use strict";

const fs = require("fs");
const path = require("path");
const R = require("../extension/rules.js");

const ROOT = path.resolve(__dirname, "..");
const TOL = 0.1; // s
const readJson = (f) => JSON.parse(fs.readFileSync(f, "utf8"));

function sameRanges(a, b) {
  if (a.length !== b.length) return `${a.length} ranges vs ${b.length}`;
  let exact = true;
  for (let i = 0; i < a.length; i++) {
    const ds = Math.abs(a[i].start - b[i].start), de = Math.abs(a[i].end - b[i].end);
    if (ds > TOL || de > TOL) return `range ${i}: ${JSON.stringify(a[i])} vs ${JSON.stringify(b[i])}`;
    if (ds || de) exact = false;
  }
  return exact ? "" : "within";
}

function main() {
  const args = process.argv.slice(2);
  if (args[0] === "--apply") {
    const tl = readJson(args[1]).timeline;
    const out = readJson(args[2]).map((ch) => R.apply(tl, ch));
    process.stdout.write(JSON.stringify(out));
    return 0;
  }
  const profiles = readJson(path.join(ROOT, "extension", "skiplists", "profiles.json"));
  const dir = path.join(ROOT, "skiplists");
  const files = fs.readdirSync(dir).filter((f) => /^[A-Za-z0-9_-]{11}\.json$/.test(f)).sort();
  let fails = 0, checked = 0;
  for (const f of files) {
    const d = readJson(path.join(dir, f));
    if (!d.timeline) {
      console.log(`[SKIP] ${f}: format ${d.format}, no timeline`);
      continue;
    }
    checked++;
    // what an add-on user with untouched settings gets
    const res = R.apply(d.timeline, R.effectiveChoices(d.timeline, profiles, {}));
    const problems = [];
    const s = sameRanges(res.skips, d.skips);
    if (s && s !== "within") problems.push(`skips: ${s}`);
    const wantHl = d.highlights.map((h) => `${h.profile}:${h.member}`);
    const gotHl = res.highlights.map((h) => h.key);
    if (wantHl.join() !== gotHl.join()) problems.push(`highlighted members ${gotHl} vs ${wantHl}`);
    else {
      d.highlights.forEach((h, i) => {
        const r = sameRanges(res.highlights[i].ranges, h.ranges);
        if (r && r !== "within") problems.push(`${h.name}: ${r}`);
      });
    }
    if (problems.length) fails++;
    const exact = !problems.length && JSON.stringify(res.skips) === JSON.stringify(d.skips) &&
      res.highlights.every((h, i) => JSON.stringify(h.ranges) === JSON.stringify(d.highlights[i].ranges));
    console.log(`[${problems.length ? "FAIL" : "PASS"}] ${f}: ${res.skips.length} skips, ` +
                `${res.highlights.map((h) => `${h.ranges.length} ${h.key.split(":")[1]}`).join(", ") || "no highlights"}` +
                `${problems.length ? " - " + problems.join("; ") : exact ? " (exact)" : " (within 0.1 s)"}`);
  }
  console.log(`\n${checked - fails}/${checked} skip lists: add-on defaults = scanner defaults`);
  return fails || !checked ? 1 : 0;
}

process.exitCode = main();

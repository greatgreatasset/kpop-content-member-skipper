// Face boxes for blurring (extension/blur.js): interpolation between samples,
// hold before / after a track, scene cuts (never shown across one, no fade
// there), fades, malformed input.
//
//   node tests/blur_test.js
//   node tests/blur_test.js --at <list.json> <queries.json>   (used by blur_test.py:
//        queries [[key, track index, t], ...] -> [[x, y, w, h, op] | null, ...])
"use strict";

const fs = require("fs");
const B = require("../extension/blur.js");

const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
const enc = (v) => {
  const q = Math.round(Math.min(1, Math.max(0, v)) * 4095);
  return B64[q >> 6] + B64[q & 63];
};
// [[frame, [x, y, w, h]], ...] -> [first frame, samples]
const track = (samples) => [samples[0][0], samples.map(([f, b], i) =>
  B64[i ? f - samples[i - 1][0] : 0] + b.map(enc).join("")).join("")];

let fails = 0;
function check(name, ok, info = "") {
  if (!ok) fails++;
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${info ? " " + info : ""}`);
}
const near = (a, b, tol = 2e-3) => Math.abs(a - b) <= tol;

function synthetic() {
  // grid 60/s; a cut at 2.0 s (frame 120)
  const faces = {
    fps: 60, hold: 0.34, cuts: [120],
    tracks: {
      "g:a": [track([[60, [0.1, 0.2, 0.1, 0.1]], [80, [0.2, 0.2, 0.1, 0.1]], [100, [0.4, 0.3, 0.2, 0.1]]])],
      "g:b": [track([[130, [0.5, 0.5, 0.1, 0.1]]])],
      "g:c": [track([[300, [0.7, 0.1, 0.1, 0.1]], [310, [0.8, 0.1, 0.1, 0.1]]])],
    },
  };
  const dec = B.decode(faces);
  const [a] = dec.tracks["g:a"], [b] = dec.tracks["g:b"], [c] = dec.tracks["g:c"];
  const at = (tr, t) => B.boxAt(tr, t);
  let r = at(a, (1.0 + 4 / 3) / 2);
  check("interpolates halfway between two samples", r && near(r.x, 0.15) && near(r.y, 0.2) && r.op === 1,
        JSON.stringify(r));
  r = at(a, 1.5);
  check("interpolates between the 2nd and 3rd sample", r && near(r.x, 0.3) && near(r.w, 0.15), JSON.stringify(r));
  r = at(a, 0.7);
  check("held before the first sample, fading in", r && near(r.x, 0.1) && near(r.op, (0.7 - 0.66) / 0.15),
        JSON.stringify(r));
  check("not shown before the hold", at(a, 0.65) === null);
  r = at(a, 1.95);
  check("held after the last sample up to the cut, no fade there", r && near(r.x, 0.4) && r.op === 1,
        JSON.stringify(r));
  check("never shown at / after the cut", at(a, 2.0) === null && at(a, 2.1) === null);
  r = at(b, 2.0);
  check("a track after a cut starts at the cut, no fade in", r && near(r.x, 0.5) && r.op === 1, JSON.stringify(r));
  check("... and not before it", at(b, 1.999) === null);
  r = at(b, 2.1667 + 0.3);
  check("one-sample track held after its sample, fading out", r && near(r.op, (2.1667 + 0.34 - 2.4667) / 0.15, 0.01),
        JSON.stringify(r));
  check("fully faded out at the end", at(b, 2.1667 + 0.34 + 0.001) === null);
  r = at(c, 300 / 60 + 1 / 12);
  check("variable sample step (1/6 s)", r && near(r.x, 0.75), JSON.stringify(r));
  // the index: only the chosen members, by second
  const idx = B.index(dec, ["g:a", "g:c"]);
  check("index: only the chosen members", idx.count === 2);
  const got = B.boxesAt(idx, 1.5).map((x) => x.key);
  check("boxesAt finds the active track", got.join() === "g:a", got.join());
  check("boxesAt: nothing across the cut", B.boxesAt(idx, 2.05).length === 0);
  check("boxesAt: member not chosen is not returned", B.boxesAt(idx, 2.2).length === 0);
  // malformed input
  check("decode: not a faces block -> null", B.decode(null) === null && B.decode({ fps: 0 }) === null);
  const bad = B.decode({ fps: 60, hold: 0.3, cuts: [], tracks: { "g:a": [[0, "AAA"], [0, "!AAAAAAAA"], "x"] } });
  check("decode: malformed tracks are dropped", bad && bad.tracks["g:a"].length === 0);
}

function main() {
  const args = process.argv.slice(2);
  if (args[0] === "--at") {
    const faces = JSON.parse(fs.readFileSync(args[1], "utf8")).timeline.faces;
    const dec = B.decode(faces);
    const out = JSON.parse(fs.readFileSync(args[2], "utf8")).map(([k, i, t]) => {
      const r = B.boxAt(dec.tracks[k][i], t);
      return r && [r.x, r.y, r.w, r.h, r.op];
    });
    process.stdout.write(JSON.stringify(out));
    return 0;
  }
  synthetic();
  console.log(fails ? `\n${fails} failed` : "\nall passed");
  return fails ? 1 : 0;
}

process.exitCode = main();

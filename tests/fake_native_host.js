#!/usr/bin/env node
// Stand-in for native_host.py in tests/chromium_test.js: speaks the same
// messages (4-byte little-endian length + JSON) but "scans" by handing back a
// prepared skip list. FAKE_HOST_LIST = path of that list (its videoId is set to
// the requested video's).
"use strict";

const fs = require("fs");

function send(msg) {
  const data = Buffer.from(JSON.stringify(msg), "utf8");
  const len = Buffer.alloc(4);
  len.writeUInt32LE(data.length);
  fs.writeSync(1, Buffer.concat([len, data]));
}

function read() {
  const len = Buffer.alloc(4);
  if (fs.readSync(0, len, 0, 4) < 4) return null;
  const n = len.readUInt32LE();
  const data = Buffer.alloc(n);
  let got = 0;
  while (got < n) got += fs.readSync(0, data, got, n - got);
  return JSON.parse(data.toString("utf8"));
}

const sleep = (ms) => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);

send({ type: "hello", version: 1 });
const msg = read();
if (msg && msg.type === "attach") {
  send({ type: "idle", videoId: msg.videoId });
} else if (msg && msg.type === "scan") {
  const vid = msg.videoId;
  send({ type: "progress", videoId: vid, stage: "download", percent: 5, eta: 60 });
  sleep(1500);
  send({ type: "progress", videoId: vid, stage: "scan", percent: 50, eta: 30 });
  sleep(1500);
  const list = JSON.parse(fs.readFileSync(process.env.FAKE_HOST_LIST, "utf8"));
  // the real host has the same limit: one message is at most 1 MB
  delete list.timeline.faces;
  send({ type: "done", videoId: vid, list: { ...list, videoId: vid, scannedAt: new Date().toISOString() },
         pushed: false, note: "Not uploaded (test)." });
}

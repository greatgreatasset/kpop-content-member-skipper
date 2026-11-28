// Lets the same code run in Firefox and in Chromium browsers (Chrome, Edge,
// Brave). Loaded before every other script of the add-on: listed first in the
// manifest (background and content scripts), a <script> in the popup and the
// settings page, and importScripts() in background.js where that file runs as
// a service worker (Chromium).
"use strict";

// Chromium has the WebExtension API under `chrome` (with promises, Manifest V3).
if (typeof globalThis.browser === "undefined") globalThis.browser = globalThis.chrome;

// browser.runtime.onMessage for both: handler(msg, sender) returns the answer
// or a promise of it; undefined = "not for me". (Answering by returning a
// promise from the listener itself is Firefox-only.)
globalThis.kmsOnMessage = (handler) => {
  browser.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    let r;
    try {
      r = handler(msg, sender);
    } catch (e) {
      console.warn("[kms] message failed:", e);
      return undefined;
    }
    if (r === undefined) return undefined;
    Promise.resolve(r).then(sendResponse, (e) => {
      console.warn("[kms] message failed:", e);
      sendResponse(undefined);
    });
    return true; // the answer comes later
  });
};

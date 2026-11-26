"""End-to-end check of "Request filtering" (a friend's PC asks this PC for a scan).

    python tests/request_test.py --video <id of a video with no skip list yet> [--headed]
    python tests/request_test.py --ui-only      (no scan: only the settings page and the button)

Real Firefox (Selenium) with extension/ as a temporary add-on, told to behave
like a PC without the scanner (storage "forceRequest"), and a request code made
for this run only (never the real one). It checks: the settings page refuses a
mistyped code and saves a good one; an unscanned video shows "Not filtered yet ·
Request filtering"; clicking it shows "Requested · waiting for the scanner PC";
then request_watcher.py is started with the same code and the line follows the
scan ("Being filtered… N%") until the skip list arrives from GitHub and its
skips show up. That part is a real scan: it commits and pushes the video's skip
list like scan.bat. Screenshots go to cache/debug/.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import browser_test as bt  # noqa: E402
import request_watcher  # noqa: E402

check, js, wait = bt.check, bt.js, bt.wait


def ext_call(d, msg):
    return d.execute_async_script("""const done = arguments[arguments.length - 1];
        browser.runtime.sendMessage(arguments[0]).then(done, e => done('error ' + e));""", msg)


def settings(d, ext_uuid, code):
    bt.open_ext_page(d, ext_uuid, "options.html")
    d.execute_async_script("""const done = arguments[arguments.length - 1];
        browser.storage.local.set({forceRequest: true}).then(() => done(true));""")
    box, save = d.find_element(By.ID, "requestCode"), d.find_element(By.ID, "saveCode")
    msg = lambda: d.find_element(By.ID, "codeMsg").text  # noqa: E731
    wrong = code[:-1] + ("a" if code[-1] != "a" else "b")
    box.send_keys(wrong)
    save.click()
    check("settings: a mistyped code is refused", wait(d, lambda: "isn't right" in msg(), 5), repr(msg()))
    check("settings: nothing saved for it", ext_call(d, {"type": "requestSetup"}).get("hasCode") is False)
    box.clear()
    box.send_keys("  " + request_watcher.pretty(code).upper() + " ")
    save.click()
    check("settings: the code is saved", wait(d, lambda: msg().startswith("Saved"), 5), repr(msg()))
    setup = ext_call(d, {"type": "requestSetup"})
    check("settings: stored as typed by the watcher", setup.get("code") == code and setup.get("ok"), repr(setup.get("ok")))
    d.save_screenshot(str(bt.OUT / "ext_request_settings.png"))
    d.close()
    d.switch_to.window(d.window_handles[0])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", help="a video without a skip list (it is scanned and its list pushed)")
    ap.add_argument("--ui-only", action="store_true")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    if not a.ui_only and (not a.video or (ROOT / "skiplists" / f"{a.video}.json").exists()):
        raise SystemExit("--video must be a video that has no skip list yet (or use --ui-only)")
    vid = a.video or "jNQXAC9IVRw"  # --ui-only: any video nobody scanned
    bt.OUT.mkdir(parents=True, exist_ok=True)
    code = request_watcher.new_code()

    ext_uuid = str(uuid.uuid4())
    o = Options()
    if not a.headed:
        o.add_argument("-headless")
    o.set_preference("media.autoplay.default", 0)
    o.set_preference("media.volume_scale", "0.0")
    o.set_preference("extensions.webextensions.uuids", json.dumps({bt.ADDON_ID: ext_uuid}))
    d = webdriver.Firefox(options=o, service=Service(service_args=["--allow-system-access"]))
    d.set_window_size(1400, 900)
    watcher = None
    try:
        d.install_addon(str(bt.EXT), temporary=True)
        settings(d, ext_uuid, code)

        d.get(f"https://www.youtube.com/watch?v={vid}")
        bt.handle_consent(d)
        if "watch" not in d.current_url:
            d.get(f"https://www.youtube.com/watch?v={vid}")
        check("video loaded, no ad", bt.wait_no_ad(d, min_len=5))
        yt = d.current_window_handle
        btn = wait(d, lambda: d.find_elements(By.CSS_SELECTOR, ".cys-stats .cys-filter"), 20)
        line = bt.stats_text(d)
        check("under-title offers 'Request filtering'", btn and line == "Not filtered yet · Request filtering", repr(line))
        if not btn:
            return
        # the popup offers it too
        bt.open_ext_page(d, ext_uuid, f"popup.html?video={vid}")
        pb = wait(d, lambda: (lambda b: b if b.is_displayed() else None)(d.find_element(By.ID, "scanBtn")), 10)
        check("popup offers 'Request filtering'", pb and pb.text == "Request filtering", repr(pb and pb.text))
        d.close()
        d.switch_to.window(yt)
        if a.ui_only:
            return

        js(d, "document.querySelectorAll('ytd-mealbar-promo-renderer, yt-mealbar-promo-renderer').forEach(e => e.remove())")
        btn[0].click()
        t = wait(d, lambda: (lambda x: x if x and x.startswith("Requested") else None)(bt.stats_text(d)), 15, 0.2)
        check("after the click: requested, waiting for the scanner PC", t, repr(bt.stats_text(d)))
        d.save_screenshot(str(bt.OUT / "ext_request_sent.png"))
        time.sleep(3)  # the scanner PC is "off" for a moment: the request waits on the relay

        env = dict(os.environ, KMS_REQ_CODE=code, PYTHONIOENCODING="utf-8")
        watcher = subprocess.Popen([sys.executable, str(ROOT / "request_watcher.py")], cwd=ROOT, env=env)
        seen = []

        def done():
            t = bt.stats_text(d)
            if t and t not in seen:
                seen.append(t)
                if len(seen) == 3:
                    d.save_screenshot(str(bt.OUT / "ext_request_progress.png"))
            return t if t and "Skipped" in t else None

        t0 = time.time()
        final = wait(d, done, 900, 0.5)
        print(f"       line under the title: {seen}  ({time.time() - t0:.0f} s)")
        check("progress from the scanner PC is shown", any(x.startswith("Being filtered") for x in seen), repr(seen))
        check("the skip list arrives by itself", final, repr(final))
        if not final:
            d.save_screenshot(str(bt.OUT / "ext_request_fail.png"))
            return
        sl = bt.load_list(vid)
        check("stats line after the scan", final == bt.expected_stats(sl), f"{final!r}, expected {bt.expected_stats(sl)!r}")
        nexp = len(sl["skips"]) + sum(len(h["ranges"]) for h in sl["highlights"])
        n = wait(d, lambda: js(d, """const b=document.querySelector('.cys-markers');
            return b && b.querySelectorAll('.cys-skip, .cys-hl').length"""), 10) or 0
        check("skips and highlights applied (markers)", n == nexp, f"{n} markers, expected {nexp}")
        js(d, "document.querySelector('#movie_player').dispatchEvent(new MouseEvent('mousemove',{bubbles:true}))")
        d.save_screenshot(str(bt.OUT / "ext_request_done.png"))
        bt.open_ext_page(d, ext_uuid, "options.html")
        left = d.execute_async_script("""const done = arguments[arguments.length - 1];
            browser.storage.local.get(null).then(s => done(Object.keys(s).filter(k => k.startsWith('req:'))));""")
        check("the request is cleared", left == [], repr(left))
    finally:
        if watcher:
            watcher.terminate()
        d.quit()
        passed = sum(1 for _, ok, _ in bt.results if ok)
        print(f"\n{passed}/{len(bt.results)} checks passed")
    return 0 if all(ok for _, ok, _ in bt.results) else 1


if __name__ == "__main__":
    sys.exit(main())

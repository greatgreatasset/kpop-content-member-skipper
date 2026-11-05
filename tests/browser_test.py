"""End-to-end check of the extension in real (headless) Firefox via Selenium.

    python tests/browser_test.py [--headed] [--video oVMQTIUI_2c]

Installs extension/ as a temporary add-on in a fresh profile, opens the video,
waits out ads, and checks: progress-bar markers, skip at the first skip range,
toast + Undo, the Next Ian button, the on/off toggle (via the popup page) and
in-page navigation to another video. Screenshots go to cache/debug/.
"""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"
OUT = ROOT / "cache" / "debug"
ADDON_ID = "kpop-content-member-skipper@kirk.local"

results = []


def check(name, ok, info=""):
    results.append((name, bool(ok), info))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {info}")


def js(d, code, *args):
    return d.execute_script(code, *args)


def wait(d, cond, timeout=30, step=0.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = cond()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


def handle_consent(d):
    """Decline non-essential cookies if YouTube's consent page shows up."""
    if "consent." not in d.current_url:
        return
    for b in d.find_elements(By.CSS_SELECTOR, "button"):
        if "reject" in (b.text or "").lower():
            b.click()
            time.sleep(3)
            return


def wait_no_ad(d, timeout=120):
    return wait(d, lambda: js(d, """
        const p = document.querySelector('#movie_player'), v = p && p.querySelector('video');
        return p && v && !p.classList.contains('ad-showing') && v.readyState >= 1 &&
               isFinite(v.duration) && v.duration > 600;"""), timeout)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--video", default="oVMQTIUI_2c")
    ap.add_argument("--profile", default="hearts2hearts")
    a = ap.parse_args()
    sl = json.loads((ROOT / "skiplists" / a.profile / f"{a.video}.json").read_text(encoding="utf-8"))
    hl = sl["highlights"][0]
    OUT.mkdir(parents=True, exist_ok=True)

    ext_uuid = str(uuid.uuid4())
    o = Options()
    if not a.headed:
        o.add_argument("-headless")
    o.set_preference("media.autoplay.default", 0)
    o.set_preference("media.volume_scale", "0.0")
    o.set_preference("extensions.webextensions.uuids", json.dumps({ADDON_ID: ext_uuid}))
    # --allow-system-access lets the test open extension pages from chrome context
    from selenium.webdriver.firefox.service import Service
    d = webdriver.Firefox(options=o, service=Service(service_args=["--allow-system-access"]))
    d.set_window_size(1400, 900)
    try:
        d.install_addon(str(EXT), temporary=True)  # noqa: selenium 4.52 still supports it
        d.get(f"https://www.youtube.com/watch?v={a.video}")
        handle_consent(d)
        if "watch" not in d.current_url:
            d.get(f"https://www.youtube.com/watch?v={a.video}")
        page = d.page_source.lower()
        if "confirm you" in page and "not a bot" in page:
            check("youtube playable", False, "YouTube asked for a bot check; stopping (not bypassing it)")
            return
        ok = wait_no_ad(d)
        check("video loaded, no ad", ok)
        if not ok:
            d.save_screenshot(str(OUT / "ext_fail_load.png"))
            return
        js(d, "const v=document.querySelector('#movie_player video'); v.muted=true; v.play();")

        # markers
        n = wait(d, lambda: js(d, """const b=document.querySelector('.cys-markers');
            return b && [b.querySelectorAll('.cys-skip').length, b.querySelectorAll('.cys-hl').length];"""), 20)
        check("progress-bar markers", n == [len(sl["skips"]), len(hl["ranges"])],
              f"found {n}, expected {[len(sl['skips']), len(hl['ranges'])]}")

        # skip 0:04 -> first skip end
        s0 = sl["skips"][0]
        js(d, "const v=document.querySelector('#movie_player video'); v.currentTime=arguments[0]; v.play();",
           s0["start"] - 1.5)
        t = wait(d, lambda: (lambda x: x if x >= s0["end"] - 0.5 else None)(
            js(d, "return document.querySelector('#movie_player video').currentTime")), 15, 0.25)
        check(f"skips {s0['start']}->{s0['end']}", t, f"currentTime={t}")
        toast = js(d, "const e=document.querySelector('.cys-toast.cys-show'); return e && e.textContent")
        check("toast shown", toast and "Skipped" in toast, repr(toast))
        js(d, "document.querySelector('#movie_player').dispatchEvent(new MouseEvent('mousemove',{bubbles:true}))")
        time.sleep(0.4)
        d.save_screenshot(str(OUT / "ext_after_skip.png"))
        d.find_element(By.CSS_SELECTOR, ".cys-toast button").click()
        time.sleep(2.5)
        t = js(d, "return document.querySelector('#movie_player video').currentTime")
        check("undo jumps back and stays", s0["start"] - 0.5 <= t < s0["end"] - 1, f"currentTime={t:.1f}")

        # next Ian button
        js(d, "document.querySelector('#movie_player').dispatchEvent(new MouseEvent('mousemove',{bubbles:true}))")
        btn = d.find_elements(By.CSS_SELECTOR, ".cys-next-btn")
        if btn:
            cur = js(d, "return document.querySelector('#movie_player video').currentTime")
            check("next button label", btn[0].text.strip() == f"{hl['name']} ⏭", repr(btn[0].text))
            nxt = next(r for r in hl["ranges"] if r["start"] > cur + 1)
            js(d, "arguments[0].click()", btn[0])
            time.sleep(1)
            t = js(d, "return document.querySelector('#movie_player video').currentTime")
            check(f"Next {hl['name']} button", abs(t - nxt["start"]) < 1.5, f"jumped to {t:.1f}, expected {nxt['start']}")
        else:
            check(f"Next {hl['name']} button", False, "button not found in .ytp-right-controls")

        # second skip range, after Undo suppression of the first one
        s1 = sl["skips"][1]
        js(d, "document.querySelector('#movie_player video').currentTime=arguments[0]", s1["start"] + 1)
        t = wait(d, lambda: (lambda x: x if x >= s1["end"] - 0.5 else None)(
            js(d, "return document.querySelector('#movie_player video').currentTime")), 10, 0.25)
        check("seeking into a skip range skips", t, f"currentTime={t}")

        # popup page: status needs the YouTube tab active, so just test the toggle
        yt = d.current_window_handle

        def open_ext_page(page):
            # WebDriver may not navigate to moz-extension:// itself; open the tab from chrome context
            before = set(d.window_handles)
            with d.context(d.CONTEXT_CHROME):
                d.execute_script("""
                    const w = Services.wm.getMostRecentWindow('navigator:browser');
                    w.gBrowser.selectedTab = w.gBrowser.addTab(arguments[0],
                        {triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal()});""",
                                 f"moz-extension://{ext_uuid}/{page}")
            h = wait(d, lambda: (set(d.window_handles) - before) or None, 10)
            d.switch_to.window(h.pop())
            time.sleep(1.5)

        open_ext_page("popup.html")
        cb = d.find_element(By.ID, "enabled")
        check("popup loads, skipping on", cb.is_selected(), f"status={d.find_element(By.ID, 'status').text!r}")
        prof_txt = d.find_element(By.ID, "profile").text
        check("popup shows profile", "Hearts2Hearts" in prof_txt and "Carmen" in prof_txt, repr(prof_txt))
        check("popup highlight label", d.find_element(By.ID, "hlLabel").text == f"{hl['name']} highlights",
              repr(d.find_element(By.ID, "hlLabel").text))
        cb.click()
        time.sleep(0.5)
        d.close()
        d.switch_to.window(yt)
        open_ext_page("options.html")
        info = d.find_element(By.ID, "profileInfo").text
        check("settings: profile selector", d.find_element(By.ID, "profile").get_attribute("value") == a.profile
              and "1 video" in info, repr(info))
        base = d.find_element(By.ID, "baseUrl").get_attribute("value")
        check("settings: default skip list URL", "kpop-content-member-skipper" in base, base)
        d.close()
        d.switch_to.window(yt)
        s2 = sl["skips"][2]
        js(d, "document.querySelector('#movie_player video').currentTime=arguments[0]", s2["start"] + 0.5)
        time.sleep(2)
        t = js(d, "return document.querySelector('#movie_player video').currentTime")
        check("toggle off disables skipping", t < s2["end"] - 0.5, f"currentTime={t:.1f}")
        n = js(d, "return document.querySelectorAll('.cys-markers .cys-skip').length")
        check("toggle off hides skip markers", n == 0, f"{n} skip markers")

        # in-page navigation to another (unscanned) video
        js(d, """const a=[...document.querySelectorAll('a[href*="/watch?v="]')]
                  .find(x=>!x.href.includes(arguments[0]) && x.offsetParent); a && a.click();""", a.video)
        moved = wait(d, lambda: a.video not in d.current_url and "watch" in d.current_url, 20)
        if moved:
            time.sleep(3)
            n = js(d, "return document.querySelectorAll('.cys-markers').length")
            check("SPA navigation clears markers", n == 0, f"url={d.current_url} markers={n}")
            d.back()
            wait(d, lambda: a.video in d.current_url, 15)
            wait_no_ad(d, 90)
            n = wait(d, lambda: js(d, "return document.querySelectorAll('.cys-markers .cys-hl').length"), 15)
            check("back navigation restores markers", n == len(hl["ranges"]), f"{n} highlight markers")
        else:
            check("SPA navigation", False, "could not find/click another video link")
    finally:
        d.quit()
        passed = sum(ok for _, ok, _ in results)
        print(f"\n{passed}/{len(results)} checks passed")


if __name__ == "__main__":
    sys.exit(main())

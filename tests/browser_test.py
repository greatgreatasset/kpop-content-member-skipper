"""End-to-end check of the extension in real (headless) Firefox via Selenium.

    python tests/browser_test.py [--headed] [--video oVMQTIUI_2c]
    python tests/browser_test.py --scan-video tPNGxVu5d68 [--rescan]

Installs extension/ as a temporary add-on in a fresh profile, opens the video,
waits out ads, and checks: progress-bar markers, skip at the first skip range,
toast + Undo, the Next Ian button, the "Ian: 25% · Skipped: 17%" line under the
title, the on/off toggle (via the popup page) and in-page navigation to another
video. With --scan-video it instead runs "Filter this video" for real (needs
install_native_host.bat; commits and pushes the skip list like scan.bat):
button -> progress banner -> skips applied. Screenshots go to cache/debug/.
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


def wait_no_ad(d, timeout=120, min_len=600):
    return wait(d, lambda: js(d, """
        const p = document.querySelector('#movie_player'), v = p && p.querySelector('video');
        return p && v && !p.classList.contains('ad-showing') && v.readyState >= 1 &&
               isFinite(v.duration) && v.duration > arguments[0];""", min_len), timeout)


def expected_stats(sl, enabled=True, highlights=True):
    def pct(rs):
        return round(100 * sum(max(0, r["end"] - r["start"]) for r in rs) / sl["duration"])
    parts = [f"{h['name']}: {pct(h['ranges'])}%" for h in sl["highlights"]] if highlights else []
    if enabled:
        parts.append(f"Skipped: {pct(sl['skips'])}%")
    return " · ".join(parts)


def stats_text(d):
    return js(d, "const e=document.querySelector('ytd-watch-metadata #title + .cys-stats'); "
                 "return e && e.textContent")


def open_ext_page(d, ext_uuid, page):
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


def scan_flow(d, a, ext_uuid):
    """'Filter this video' end to end, with a real scan."""
    vid = a.scan_video
    d.get(f"https://www.youtube.com/watch?v={vid}")
    handle_consent(d)
    ok = wait_no_ad(d, min_len=10)
    check("scan video loaded, no ad", ok)
    if not ok:
        return
    yt = d.current_window_handle
    btn = wait(d, lambda: d.find_elements(By.CSS_SELECTOR, ".cys-stats .cys-filter"), 15)
    line = stats_text(d)
    if btn and not a.rescan:
        check("under-title offers 'Filter this video'", "Not filtered yet" in (line or ""), repr(line))
        # YouTube's promo bars can sit on top of the line
        js(d, "document.querySelectorAll('ytd-mealbar-promo-renderer, yt-mealbar-promo-renderer').forEach(e => e.remove())")
        btn[0].click()
    else:
        # already scanned (or --rescan): start it from the popup page, as the toolbar popup does
        if not btn:
            print(f"       (no under-title button: {line!r}; using the popup)")
        open_ext_page(d, ext_uuid, f"popup.html?video={vid}" + ("&rescan=1" if a.rescan else ""))
        pb = wait(d, lambda: (lambda b: b if b.is_displayed() else None)(d.find_element(By.ID, "scanBtn")), 10)
        check("popup shows the filter button", pb, repr(d.find_element(By.ID, "scanText").text))
        if not pb:
            return
        pb.click()
        prog = wait(d, lambda: (lambda t: t if "Filtering" in t else None)(d.find_element(By.ID, "scanText").text),
                    20, 0.2)
        check("popup shows progress", prog, repr(prog))
        # another request for the same video must follow the running scan, not start one
        again = d.execute_async_script("""const done = arguments[arguments.length - 1];
            browser.runtime.sendMessage({type: 'startScan', id: arguments[0]})
              .then(r => done(r.job), e => done(String(e)));""", vid)
        check("second request follows the running scan",
              isinstance(again, dict) and again.get("state") in ("running", "done"), repr(again))
        d.close()
        d.switch_to.window(yt)
    seen = []

    def banner():
        t = js(d, "const e=document.querySelector('#movie_player .cys-banner.cys-show'); return e && e.textContent")
        if t and t not in seen:
            seen.append(t)
        return t if t and t.startswith("Done") else None

    t0 = time.time()
    done = wait(d, banner, 900, 0.25)
    print(f"       banner texts: {seen[:3]} ... {seen[-2:]}  ({time.time() - t0:.0f} s)")
    check("in-page progress banner", any(x.startswith("Filtering") for x in seen), repr(seen[:3]))
    check("done banner", done, repr(done))
    js(d, "document.querySelector('#movie_player').dispatchEvent(new MouseEvent('mousemove',{bubbles:true}))")
    d.save_screenshot(str(OUT / "ext_scan_done.png"))
    if not done:
        return
    sl = json.loads((ROOT / "skiplists" / a.profile / f"{vid}.json").read_text(encoding="utf-8"))
    nexp = len(sl["skips"]) + sum(len(h["ranges"]) for h in sl["highlights"])
    n = wait(d, lambda: js(d, """const b=document.querySelector('.cys-markers');
        return b && b.querySelectorAll('.cys-skip, .cys-hl').length"""), 10)
    check("skips applied right away (markers)", n == nexp, f"{n} markers, expected {nexp}")
    check("stats line after scan", stats_text(d) == expected_stats(sl),
          f"{stats_text(d)!r}, expected {expected_stats(sl)!r}")
    open_ext_page(d, ext_uuid, "options.html")
    src = d.execute_async_script("""const done = arguments[arguments.length - 1];
        browser.runtime.sendMessage({type: 'getList', id: arguments[0]})
          .then(r => done(r.source + ' ' + (r.data && r.data.scannedAt)), e => done('error ' + e));""", vid)
    check("list served without waiting for GitHub", src.endswith(sl["scannedAt"]), src)
    d.close()
    d.switch_to.window(yt)
    if sl["skips"]:
        s0 = sl["skips"][0]
        t = None
        for _ in range(4):  # a mid-roll ad may start first; the add-on never skips during ads
            wait_no_ad(d, 90, min_len=10)
            # seeking into the range triggers the skip without needing playback
            js(d, "const v=document.querySelector('#movie_player video'); v.muted=true; v.currentTime=arguments[0]; v.play();",
               s0["start"] + 0.3)
            t = wait(d, lambda: (lambda x: x if x >= s0["end"] - 0.5 else None)(
                js(d, "const p=document.querySelector('#movie_player'); return p.classList.contains('ad-showing') ? 0 : p.querySelector('video').currentTime")), 8, 0.25)
            if t:
                break
        check("new skip list skips", t, f"currentTime={t}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--video", default="oVMQTIUI_2c")
    ap.add_argument("--profile", default="hearts2hearts")
    ap.add_argument("--scan-video", help="run 'Filter this video' on this video (real scan + push)")
    ap.add_argument("--rescan", action="store_true", help="with --scan-video: rescan an already scanned video")
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
        if a.scan_video:
            scan_flow(d, a, ext_uuid)
            return
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

        # "Ian: 25% · Skipped: 17%" under the title
        want = expected_stats(sl)
        got = wait(d, lambda: stats_text(d), 15)
        check("stats line under the title", got == want, f"{got!r}, expected {want!r}")
        color = js(d, "const e=document.querySelector('.cys-stats .cys-stat-hl'); return e && e.style.color")
        check("highlight name in marker pink", color == "rgb(255, 79, 163)", repr(color))
        js(d, "document.querySelector('ytd-watch-metadata').scrollIntoView()")
        for theme in ("light", "dark"):
            js(d, "document.documentElement.toggleAttribute('dark', arguments[0])", theme == "dark")
            time.sleep(0.5)
            d.find_element(By.CSS_SELECTOR, "ytd-watch-metadata").screenshot(str(OUT / f"ext_stats_{theme}.png"))
        js(d, "document.documentElement.toggleAttribute('dark', false); window.scrollTo(0, 0)")

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

        open_ext_page(d, ext_uuid, "popup.html")
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
        open_ext_page(d, ext_uuid, "options.html")
        info = d.find_element(By.ID, "profileInfo").text
        check("settings: profile selector", d.find_element(By.ID, "profile").get_attribute("value") == a.profile
              and "video(s) bundled" in info, repr(info))
        base = d.find_element(By.ID, "baseUrl").get_attribute("value")
        check("settings: default skip list URL", "kpop-content-member-skipper" in base, base)
        # ask the background script (from this extension page) where the list comes from
        src = d.execute_async_script("""const done = arguments[arguments.length - 1];
            browser.runtime.sendMessage({type: 'getList', id: arguments[0]})
              .then(r => done(r.source + ' ' + (r.data ? r.data.skips.length : 0)), e => done('error ' + e));""",
                                     a.video)
        print(f"       skip list source: {src} (online = fetched from the base URL, bundled = fallback)")
        check("skip list loaded", src.split()[-1] == str(len(sl["skips"])), src)
        d.close()
        d.switch_to.window(yt)
        s2 = sl["skips"][2]
        js(d, "document.querySelector('#movie_player video').currentTime=arguments[0]", s2["start"] + 0.5)
        time.sleep(2)
        t = js(d, "return document.querySelector('#movie_player video').currentTime")
        check("toggle off disables skipping", t < s2["end"] - 0.5, f"currentTime={t:.1f}")
        n = js(d, "return document.querySelectorAll('.cys-markers .cys-skip').length")
        check("toggle off hides skip markers", n == 0, f"{n} skip markers")
        want = expected_stats(sl, enabled=False)
        got = stats_text(d)
        check("toggle off: stats line drops Skipped", got == want, f"{got!r}, expected {want!r}")

        # in-page navigation to another (unscanned) video
        js(d, """const a=[...document.querySelectorAll('a[href*="/watch?v="]')]
                  .find(x=>!x.href.includes(arguments[0]) && x.offsetParent); a && a.click();""", a.video)
        moved = wait(d, lambda: a.video not in d.current_url and "watch" in d.current_url, 20)
        if moved:
            time.sleep(3)
            n = js(d, "return document.querySelectorAll('.cys-markers').length")
            check("SPA navigation clears markers", n == 0, f"url={d.current_url} markers={n}")
            other = d.current_url.split("v=")[1][:11]
            if not (ROOT / "skiplists" / a.profile / f"{other}.json").exists():
                line = wait(d, lambda: (lambda t: t if t and "Not filtered" in t else None)(stats_text(d)), 10)
                has_btn = bool(js(d, "return document.querySelector('.cys-stats .cys-filter')"))
                check("unscanned video: 'Not filtered yet'", line, repr(stats_text(d)))
                print(f"       'Filter this video' offered: {has_btn} (true only with the scanner installed)")
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

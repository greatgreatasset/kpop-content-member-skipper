"""End-to-end check of the extension in real (headless) Firefox via Selenium.

    python tests/browser_test.py [--headed] [--video oVMQTIUI_2c] [--other Vk5-c_v4gMU]
    python tests/browser_test.py --bundled     (only the lists in extension/skiplists/, e.g. before pushing)
    python tests/browser_test.py --scan-video tPNGxVu5d68 [--rescan]

Installs extension/ as a temporary add-on in a fresh profile, opens the video,
waits out ads, and checks: progress-bar markers, skip at the first skip range,
toast + Undo, the Next Ian button, the "Ian: 25% · Skipped: 17%" line under the
title, the popup ("Skipping Carmen & Ye-on (Hearts2Hearts)"), the settings page
(groups listed, no group selector), the on/off toggle (via the popup page) and
in-page navigation to another video. Then a video of another group with no
highlighted member (--other, an ILLIT video): "Skipped: N%" only, no Next
button, popup "Skipping Yunah (ILLIT)". Then per-user choices: on the settings
page Carmen -> Highlight and Ian -> Skip, which must change the skips, markers
and stats line of the open video without a reload (expected values from
rules.py), and "Reset to defaults" restores them. Last, a KiiiKiii video
(--kiiikiii) shows Kya's highlights, and with "Highlight when: only when she's
the main focus" only her focus parts (rules.py), back to all with "any time". With --scan-video it instead runs "Filter this video" for real (needs
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
sys.path.insert(0, str(ROOT))
import rules  # noqa: E402
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


def load_list(vid):
    return json.loads((ROOT / "skiplists" / f"{vid}.json").read_text(encoding="utf-8"))


def by_group(members):
    """[{name, group}] -> 'Carmen & Ye-on (Hearts2Hearts), Yunah (ILLIT)', as the popup writes it."""
    groups = {}
    for m in members:
        groups.setdefault(m["group"], []).append(m["name"])
    return ", ".join(f"{' & '.join(n)} ({g})" for g, n in groups.items())


def with_choices(sl, choices, hl_when="any"):
    """The skip list as the add-on shows it with these member choices
    ({"<profile>:<member>": choice}): skips and highlights from rules.py, default
    highlights first (as rules.js orders them)."""
    tl = sl["timeline"]
    res = rules.apply(tl, choices, hl_when=hl_when)
    info = {m["key"]: m for m in tl["members"]}
    order = {m["key"]: i for i, m in enumerate(tl["members"])}
    hls = sorted(res["highlights"], key=lambda h: (info[h["key"]]["default"] != "highlight", order[h["key"]]))
    return {**sl, "skips": res["skips"],
            "highlights": [{"name": info[h["key"]]["name"], "ranges": h["ranges"]} for h in hls]}


def expected_stats(sl, enabled=True, highlights=True):
    def pct(rs):
        return round(100 * sum(max(0, r["end"] - r["start"]) for r in rs) / sl["duration"])
    parts = [f"{h['name']}: {pct(h['ranges'])}%" for h in sl["highlights"] if h["ranges"]] if highlights else []
    if enabled:
        parts.append(f"Skipped: {pct(sl['skips'])}%")
    return " · ".join(parts)


MARKERS_JS = """const b=document.querySelector('.cys-markers'); if (!b) return null;
    const get = (sel) => [...b.querySelectorAll(sel)].map(e => [parseFloat(e.style.left), parseFloat(e.style.width)]);
    return {skip: get('.cys-skip'), hl: get('.cys-hl'),
            colors: [...new Set([...b.querySelectorAll('.cys-hl')].map(e => e.style.backgroundColor))]};"""


def markers_match(got, sl, dur):
    """Marker left/width (% of the duration) match the list's skips and highlights."""
    if not got or not dur:
        return False
    want_s = [(100 * r["start"] / dur, 100 * (min(r["end"], dur) - r["start"]) / dur) for r in sl["skips"]]
    want_h = [(100 * r["start"] / dur, 100 * (min(r["end"], dur) - r["start"]) / dur)
              for h in sl["highlights"] for r in h["ranges"]]

    def near(a, b):
        a, b = sorted(map(tuple, a)), sorted(b)
        return len(a) == len(b) and all(abs(x[0] - y[0]) < 0.02 and abs(x[1] - y[1]) < 0.02 for x, y in zip(a, b))
    return near(got["skip"], want_s) and near(got["hl"], want_h)


def video_duration(d):
    return js(d, "return document.querySelector('#movie_player video').duration")


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
    sl = load_list(vid)
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


def other_group(d, a, ext_uuid):
    """A video of another group with no highlighted member: only "Skipped: N%",
    no Next button, popup "Skipping Yunah (ILLIT)"."""
    sl = load_list(a.other)
    groups = " + ".join(g["name"] for g in sl["groups"])
    # the toggle test above switched skipping off; switch it back on
    yt0 = d.current_window_handle
    open_ext_page(d, ext_uuid, "popup.html")
    cb = d.find_element(By.ID, "enabled")
    if not cb.is_selected():
        cb.click()
        time.sleep(0.5)
    d.close()
    d.switch_to.window(yt0)
    d.get(f"https://www.youtube.com/watch?v={a.other}")
    ok = wait_no_ad(d, min_len=30)
    check(f"{groups} video loaded, no ad", ok)
    if not ok:
        return
    want = expected_stats(sl)
    got = wait(d, lambda: (lambda t: t if t == want else None)(stats_text(d)), 15)
    check(f"{groups}: stats line has no highlight part", got == want and want.startswith("Skipped:"),
          f"{stats_text(d)!r}, expected {want!r}")
    n = wait(d, lambda: js(d, """const b=document.querySelector('.cys-markers');
        return b && [b.querySelectorAll('.cys-skip').length, b.querySelectorAll('.cys-hl').length];"""), 15)
    check(f"{groups}: skip markers, no highlight markers", n == [len(sl["skips"]), 0], repr(n))
    js(d, "document.querySelector('#movie_player').dispatchEvent(new MouseEvent('mousemove',{bubbles:true}))")
    time.sleep(1.5)  # ensure() runs every second
    check(f"{groups}: no Next button", not d.find_elements(By.CSS_SELECTOR, ".cys-next-btn"))
    if sl["skips"]:
        s0 = sl["skips"][0]
        js(d, "const v=document.querySelector('#movie_player video'); v.muted=true; v.currentTime=arguments[0]; v.play();",
           s0["start"] + 0.3)
        t = wait(d, lambda: (lambda x: x if x >= s0["end"] - 0.5 else None)(js(d, (
            "const p=document.querySelector('#movie_player');"
            "return p.classList.contains('ad-showing') ? 0 : p.querySelector('video').currentTime"))), 10, 0.25)
        check(f"{groups}: skips {s0['start']}->{s0['end']}", t, f"currentTime={t}")
    d.save_screenshot(str(OUT / "ext_other_group.png"))
    yt = d.current_window_handle
    open_ext_page(d, ext_uuid, f"popup.html?video={a.other}")
    txt = d.find_element(By.ID, "profile").text
    check(f"{groups}: popup names who is skipped", txt == f"Skipping {by_group(sl['skipMembers'])}", repr(txt))
    check(f"{groups}: popup has no next-highlight button", not d.find_element(By.ID, "nextHl").is_displayed())
    hl_label = d.find_element(By.ID, "hlLabel").text
    check(f"{groups}: popup highlight label", hl_label == "Highlights", repr(hl_label))
    d.close()
    d.switch_to.window(yt)


def check_settings(d):
    """Settings page: every group with each member's Skip / Normal / Highlight
    control, the default marked and selected (nothing changed yet)."""
    profiles = json.loads((EXT / "skiplists" / "profiles.json").read_text("utf-8"))
    legends = [e.text for e in d.find_elements(By.CSS_SELECTOR, "#groups fieldset legend")]
    check("settings: groups listed", legends == [p["group"] for p in profiles] and
          not d.find_elements(By.CSS_SELECTOR, "select"), repr(legends))
    bad = []
    for p in profiles:
        for m in p["members"]:
            key = f"{p['id']}:{m['id']}"
            row = d.find_elements(By.CSS_SELECTOR, f".member[data-key='{key}']")
            if not row:
                bad.append(f"{key} missing")
                continue
            opts = [(e.get_attribute("value"), e.is_selected()) for e in row[0].find_elements(By.CSS_SELECTOR, "input")]
            dflt = [e.find_element(By.CSS_SELECTOR, "input").get_attribute("value")
                    for e in row[0].find_elements(By.CSS_SELECTOR, "label.is-default")]
            name = row[0].find_element(By.CSS_SELECTOR, ".name").text
            if [o[0] for o in opts] != ["skip", "normal", "highlight"] or dflt != [m["default"]] or \
                    [o[0] for o in opts if o[1]] != [m["default"]] or name != m["name"]:
                bad.append(f"{key}: {opts} default {dflt} name {name!r}")
    n = sum(len(p["members"]) for p in profiles)
    check(f"settings: {n} members with Skip/Normal/Highlight, defaults marked and selected", not bad, repr(bad[:3]))
    check("settings: Reset to defaults button", d.find_elements(By.ID, "resetChoices"))
    hw = [(e.get_attribute("value"), e.is_selected()) for e in d.find_elements(By.CSS_SELECTOR, "input[name='hlWhen']")]
    check("settings: Highlight when = any time (default)", hw == [("any", True), ("focus", False)], repr(hw))


def pick(d, key, choice):
    """Click the member's Skip / Normal / Highlight option on the settings page."""
    d.find_element(By.CSS_SELECTOR, f".member[data-key='{key}'] input[value='{choice}']").find_element(
        By.XPATH, "..").click()
    time.sleep(0.3)


def stored_choices(d):
    return d.execute_async_script("""const done = arguments[arguments.length - 1];
        browser.storage.local.get('choices').then(r => done(r.choices || null));""")


def seek_and_wait_skip(d, s0, tries=3):
    """Seek (paused, so playback can't reach the end by itself) into s0 and
    wait for the add-on to jump to its end."""
    t = None
    for _ in range(tries):  # a mid-roll ad may start first; the add-on never skips during ads
        wait_no_ad(d, 90, min_len=10)
        js(d, "const v=document.querySelector('#movie_player video'); v.muted=true; v.pause(); v.currentTime=arguments[0];",
           s0["start"] + 0.3)
        t = wait(d, lambda: (lambda x: x if x >= s0["end"] - 0.5 else None)(js(d, (
            "const p=document.querySelector('#movie_player');"
            "return p.classList.contains('ad-showing') ? 0 : p.querySelector('video').currentTime"))), 4, 0.25)
        if t:
            break
    return t


def custom_choices(d, a, ext_uuid):
    """Carmen -> Highlight, Ian -> Skip on the settings page: the open video's
    skips, markers, stats line and Next button follow without a reload; Reset
    to defaults restores the default behaviour."""
    sl = load_list(a.video)
    d.get(f"https://www.youtube.com/watch?v={a.video}")
    ok = wait_no_ad(d)
    check("choices: video loaded, no ad", ok)
    if not ok:
        return
    want_def = expected_stats(sl)
    wait(d, lambda: stats_text(d) == want_def, 15)
    yt = d.current_window_handle
    open_ext_page(d, ext_uuid, "options.html")
    opts = d.current_window_handle
    pick(d, "hearts2hearts:carmen", "highlight")
    pick(d, "hearts2hearts:ian", "skip")
    stored = stored_choices(d)
    check("choices: stored in storage.local",
          stored == {"hearts2hearts:carmen": "highlight", "hearts2hearts:ian": "skip"}, repr(stored))
    d.switch_to.window(yt)  # the settings tab stays open; the video is not reloaded
    cs = with_choices(sl, stored or {})
    want = expected_stats(cs)
    got = wait(d, lambda: (lambda t: t if t == want else None)(stats_text(d)), 10)
    check("choices: stats line follows without reload", got == want and want.startswith("Carmen:"),
          f"{stats_text(d)!r}, expected {want!r}")
    m = wait(d, lambda: (lambda x: x if markers_match(x, cs, video_duration(d)) else None)(js(d, MARKERS_JS)), 10)
    got_m = js(d, MARKERS_JS) or {"skip": [], "hl": [], "colors": []}
    check("choices: markers = rules.py with these choices", m,
          f"{len(got_m['skip'])} skip / {len(got_m['hl'])} hl markers, expected {len(cs['skips'])} / "
          f"{sum(len(h['ranges']) for h in cs['highlights'])} (defaults: {len(sl['skips'])} / "
          f"{sum(len(h['ranges']) for h in sl['highlights'])})")
    check("choices: skips differ from the defaults", cs["skips"] != sl["skips"],
          f"{len(cs['skips'])} skips ({round(sum(s['end'] - s['start'] for s in cs['skips']))} s) vs "
          f"{len(sl['skips'])} ({round(sum(s['end'] - s['start'] for s in sl['skips']))} s)")
    check("choices: Carmen in the first highlight colour (pink)",
          got_m["colors"] == ["rgb(255, 79, 163)"], repr(got_m["colors"]))
    js(d, "document.querySelector('#movie_player').dispatchEvent(new MouseEvent('mousemove',{bubbles:true}))")
    time.sleep(1.5)
    btn = d.find_elements(By.CSS_SELECTOR, ".cys-next-btn")
    check("choices: Next button for Carmen", btn and btn[0].text.strip() == "Carmen ⏭",
          repr(btn and btn[0].text))
    # a skip that only exists with these choices (Ian is skipped now)
    new = [s for s in cs["skips"] if not any(o["start"] < s["end"] and s["start"] < o["end"] for o in sl["skips"])]
    if new:
        s0 = new[0]
        t = seek_and_wait_skip(d, s0)
        check(f"choices: skips Ian's part {s0['start']}->{s0['end']}", t, f"currentTime={t}")
    js(d, "document.querySelector('#movie_player').dispatchEvent(new MouseEvent('mousemove',{bubbles:true}))")
    time.sleep(0.4)
    d.save_screenshot(str(OUT / "ext_choices.png"))
    # Ian back to Highlight as well: two highlighted members, two colours, Ian keeps pink
    d.switch_to.window(opts)
    pick(d, "hearts2hearts:ian", "highlight")
    d.switch_to.window(yt)
    cs2 = with_choices(sl, {"hearts2hearts:carmen": "highlight"})
    want2 = expected_stats(cs2)
    got = wait(d, lambda: (lambda t: t if t == want2 else None)(stats_text(d)), 10)
    check("choices: two highlighted members in the stats line", got == want2 and want2.startswith("Ian:")
          and " · Carmen: " in want2, f"{stats_text(d)!r}, expected {want2!r}")
    cols = js(d, """const out = {}; for (const e of document.querySelectorAll('.cys-markers .cys-hl'))
        (out[e.title.split(' ')[0]] = out[e.title.split(' ')[0]] || new Set()).add(e.style.backgroundColor);
        return Object.fromEntries(Object.entries(out).map(([k, v]) => [k, [...v]]));""")
    check("choices: distinct colours, Ian pink", cols.get("Ian") == ["rgb(255, 79, 163)"] and
          len(cols.get("Carmen", [])) == 1 and cols["Carmen"] != cols["Ian"], repr(cols))
    stat_cols = js(d, "return [...document.querySelectorAll('.cys-stats .cys-stat-hl')].map(e => e.style.color)")
    check("choices: stats names in their marker colours",
          stat_cols == [cols.get("Ian", [None])[0], cols.get("Carmen", [None])[0]], repr(stat_cols))
    js(d, "document.querySelector('#movie_player').dispatchEvent(new MouseEvent('mousemove',{bubbles:true}))")
    time.sleep(1.5)
    btn = d.find_elements(By.CSS_SELECTOR, ".cys-next-btn")
    check("choices: Next button cycles through both", btn and btn[0].text.strip() == "Next ⏭",
          repr(btn and btn[0].text))
    if btn:
        allr = sorted(r["start"] for h in cs2["highlights"] for r in h["ranges"])
        js(d, "const v=document.querySelector('#movie_player video'); v.pause(); v.currentTime=arguments[0];",
           allr[0] + 0.5)
        time.sleep(0.5)
        js(d, "arguments[0].click()", btn[0])
        time.sleep(1)
        t = js(d, "return document.querySelector('#movie_player video').currentTime")
        check("choices: Next goes to the next part of either member", abs(t - allr[1]) < 1.5,
              f"{t:.1f}, expected {allr[1]}")
    # Reset to defaults
    d.switch_to.window(opts)
    d.find_element(By.ID, "resetChoices").click()
    time.sleep(0.5)
    sel = d.find_element(By.CSS_SELECTOR, ".member[data-key='hearts2hearts:ian'] input[value='highlight']").is_selected()
    stored = stored_choices(d)
    check("choices: Reset to defaults", sel and not stored, f"ian highlight selected={sel}, stored={stored!r}")
    d.close()
    d.switch_to.window(yt)
    got = wait(d, lambda: (lambda t: t if t == want_def else None)(stats_text(d)), 10)
    check("choices: reset restores the default stats line", got == want_def, f"{stats_text(d)!r}, expected {want_def!r}")
    check("choices: reset restores the default markers",
          wait(d, lambda: markers_match(js(d, MARKERS_JS), sl, video_duration(d)), 10))


def kiiikiii(d, a, ext_uuid):
    """A KiiiKiii video: Kya (highlighted by default) has pink markers, the
    stats line and a "Kya ⏭" button; nobody is skipped."""
    sl = load_list(a.kiiikiii)
    d.get(f"https://www.youtube.com/watch?v={a.kiiikiii}")
    ok = wait_no_ad(d, min_len=30)
    check("KiiiKiii video loaded, no ad", ok)
    if not ok:
        return
    kya = [h for h in sl["highlights"] if h["member"] == "kya"]
    check("KiiiKiii list has Kya parts", kya and kya[0]["ranges"], repr(sl["highlights"]))
    if not kya:
        return
    want = expected_stats(sl)
    got = wait(d, lambda: (lambda t: t if t == want else None)(stats_text(d)), 15)
    check("KiiiKiii: stats line with Kya", got == want and want.startswith("Kya:"),
          f"{stats_text(d)!r}, expected {want!r}")
    m = wait(d, lambda: (lambda x: x if markers_match(x, sl, video_duration(d)) else None)(js(d, MARKERS_JS)), 15)
    check("KiiiKiii: Kya markers in pink", m and len(m["hl"]) == len(kya[0]["ranges"]) and
          m["colors"] == ["rgb(255, 79, 163)"], repr(m and (len(m["hl"]), m["colors"])))
    js(d, "document.querySelector('#movie_player').dispatchEvent(new MouseEvent('mousemove',{bubbles:true}))")
    time.sleep(1.5)
    btn = d.find_elements(By.CSS_SELECTOR, ".cys-next-btn")
    check("KiiiKiii: Next Kya button", btn and btn[0].text.strip() == "Kya ⏭", repr(btn and btn[0].text))
    if btn:
        js(d, "const v=document.querySelector('#movie_player video'); v.muted=true; v.currentTime=0;")
        time.sleep(0.5)
        js(d, "arguments[0].click()", btn[0])
        time.sleep(1)
        t = js(d, "return document.querySelector('#movie_player video').currentTime")
        first = kya[0]["ranges"][0]["start"]
        check("KiiiKiii: Next jumps to Kya's first part", abs(t - first) < 1.5, f"{t:.1f}, expected {first}")
    js(d, "document.querySelector('#movie_player').dispatchEvent(new MouseEvent('mousemove',{bubbles:true}))")
    time.sleep(0.4)
    d.save_screenshot(str(OUT / "ext_kiiikiii.png"))
    hl_focus(d, ext_uuid, sl)


def hl_when_pick(d, value):
    d.find_element(By.CSS_SELECTOR, f"input[name='hlWhen'][value='{value}']").find_element(By.XPATH, "..").click()
    time.sleep(0.3)
    return d.execute_async_script("""const done = arguments[arguments.length - 1];
        browser.storage.local.get('hlWhen').then(r => done(r.hlWhen || null));""")


def hl_focus(d, ext_uuid, sl):
    """Settings "Highlight when: only when she's the main focus": the open
    KiiiKiii video's Kya markers and stats follow rules.py (fewer parts), no
    reload; "any time" brings back the default ones."""
    yt = d.current_window_handle
    want_def = expected_stats(sl)
    fs = with_choices(sl, {}, "focus")
    n_def = sum(len(h["ranges"]) for h in sl["highlights"])
    n_foc = sum(len(h["ranges"]) for h in fs["highlights"])
    check("focus: fewer Kya parts than any time (rules.py)", 0 < n_foc < n_def, f"{n_foc} vs {n_def}")
    open_ext_page(d, ext_uuid, "options.html")
    opts = d.current_window_handle
    stored = hl_when_pick(d, "focus")
    check("focus: stored in storage.local", stored == "focus", repr(stored))
    d.switch_to.window(yt)
    m = wait(d, lambda: (lambda x: x if markers_match(x, fs, video_duration(d)) else None)(js(d, MARKERS_JS)), 10)
    got_m = js(d, MARKERS_JS) or {"skip": [], "hl": []}
    check("focus: Kya markers = rules.py focus parts", m, f"{len(got_m['hl'])} hl markers, expected {n_foc}")
    want = expected_stats(fs)
    got = wait(d, lambda: (lambda t: t if t == want else None)(stats_text(d)), 10)
    check("focus: stats line follows", got == want, f"{stats_text(d)!r}, expected {want!r}")
    d.switch_to.window(opts)
    stored = hl_when_pick(d, "any")
    d.close()
    d.switch_to.window(yt)
    check("focus: any time removes the setting", stored is None, repr(stored))
    got = wait(d, lambda: (lambda t: t if t == want_def else None)(stats_text(d)), 10)
    check("focus: any time restores the default stats line", got == want_def, f"{stats_text(d)!r}")
    check("focus: any time restores the default markers",
          wait(d, lambda: markers_match(js(d, MARKERS_JS), sl, video_duration(d)), 10))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--video", default="oVMQTIUI_2c")
    ap.add_argument("--other", default="Vk5-c_v4gMU", help="a video of another group, without highlights")
    ap.add_argument("--kiiikiii", default="zhHB4dZTChw", help="a KiiiKiii video (Kya highlights)")
    ap.add_argument("--bundled", action="store_true",
                    help="use only the skip lists bundled in extension/ (test lists before pushing them)")
    ap.add_argument("--scan-video", help="run 'Filter this video' on this video (real scan + push)")
    ap.add_argument("--rescan", action="store_true", help="with --scan-video: rescan an already scanned video")
    a = ap.parse_args()
    sl = load_list(a.video)
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
        if a.bundled:
            # only the skip lists bundled in extension/ (e.g. before they are pushed)
            open_ext_page(d, ext_uuid, "options.html")
            d.execute_async_script("""const done = arguments[arguments.length - 1];
                browser.storage.local.set({baseUrl: ''}).then(() => done(true));""")
            d.close()
            d.switch_to.window(d.window_handles[0])
        if a.scan_video:
            scan_flow(d, a, ext_uuid)
            return
        d.get(f"https://www.youtube.com/watch?v={a.video}")
        handle_consent(d)
        if "watch" not in d.current_url:
            d.get(f"https://www.youtube.com/watch?v={a.video}")
        page = d.page_source.lower()
        if ("confirm you" in page and "not a bot" in page) or "unusual traffic" in page:
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
        # default choices: exactly the scanner's default skips and highlights (worked out by rules.js)
        check("default choices: markers at the scanner's skips/highlights",
              markers_match(js(d, MARKERS_JS), sl, video_duration(d)))

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
        check("popup without a video: every group's skips",
              prof_txt == "Skips Carmen & Ye-on (Hearts2Hearts), Yunah (ILLIT)", repr(prof_txt))
        all_hl = " & ".join(m["name"] for p in json.loads((EXT / "skiplists" / "profiles.json").read_text("utf-8"))
                            for m in p["members"] if m["default"] == "highlight")
        check("popup highlight label (no video: every group's)",
              d.find_element(By.ID, "hlLabel").text == f"{all_hl} highlights",
              repr(d.find_element(By.ID, "hlLabel").text))
        cb.click()
        time.sleep(0.5)
        d.close()
        d.switch_to.window(yt)
        open_ext_page(d, ext_uuid, "options.html")
        check_settings(d)
        base = d.find_element(By.ID, "baseUrl").get_attribute("value")
        if not a.bundled:
            check("settings: default skip list URL", "kpop-content-member-skipper" in base, base)
        # ask the background script (from this extension page) where the list comes from
        src = d.execute_async_script("""const done = arguments[arguments.length - 1];
            browser.runtime.sendMessage({type: 'getList', id: arguments[0]})
              .then(r => done(r.source + ' format ' + (r.data && r.data.format) + ' ' +
                              (r.data ? r.data.skips.length : 0)), e => done('error ' + e));""",
                                     a.video)
        print(f"       skip list source: {src} (online = fetched from the base URL, bundled = fallback;"
              f" format 2 = read from the pre-0.5.0 per-group path)")
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
            other = d.current_url.split("v=")[1][:11]
            # markers of the old video are gone (the next video may be scanned too)
            n = js(d, "return [...document.querySelectorAll('.cys-markers')].filter(b => b.dataset.videoId === arguments[0]).length",
                   a.video)
            check("SPA navigation clears markers", n == 0, f"url={d.current_url} old-video markers={n}")
            if not (ROOT / "skiplists" / f"{other}.json").exists():
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

        # the popup for this video (popup.html?video= stands in for the active tab)
        open_ext_page(d, ext_uuid, f"popup.html?video={a.video}")
        txt = d.find_element(By.ID, "profile").text
        check("popup: who this video skips", txt == f"Skipping {by_group(sl['skipMembers'])}", repr(txt))
        d.close()
        d.switch_to.window(yt)
        other_group(d, a, ext_uuid)
        custom_choices(d, a, ext_uuid)
        kiiikiii(d, a, ext_uuid)
    finally:
        d.quit()
        passed = sum(ok for _, ok, _ in results)
        print(f"\n{passed}/{len(results)} checks passed")


if __name__ == "__main__":
    sys.exit(main())

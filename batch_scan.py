"""Scan every video of a YouTube channel, one at a time, gently paced. Resumable.

    .venv\\Scripts\\python batch_scan.py                      # Hearts2Hearts channel, videos tab
    .venv\\Scripts\\python batch_scan.py --list               # only show what is still to do
    .venv\\Scripts\\python batch_scan.py --limit 10 --push    # scan 10, then upload their lists
    .venv\\Scripts\\python batch_scan.py <channel url or UC... id> [--shorts]

Videos that already have a skip list from this scanner version are left out, and
so are videos that failed for good before (private, members-only, removed; see
--retry-failed). Stop any time with Ctrl+C: the video being scanned is lost, all
finished ones are kept, and the next run picks up where this one stopped.

Pacing, so YouTube doesn't start asking "confirm you're not a bot": a random
pause of --pause seconds (default 45-120) after every download, a longer break
(--break, default 10-20 min) every --batch downloads (default 15), and the run
stops by itself on the first bot check / rate limit (wait a few hours, then run
it again). Videos whose detections are cached need no download and no pause.

Each finished list is committed locally (like scan.bat --no-push); --push
uploads them all at the end (or after Ctrl+C / a stop), in one push. The
downloaded .mp4 is deleted after its scan (the cached detections are all that
rescans need); --keep-videos keeps them.

State: cache/batch/<channel id>.json (per video: done / failed / error),
log: cache/batch/<channel id>.log.
"""
import argparse
import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import time

from common import CACHE, ROOT, SKIPLISTS

H2H_CHANNEL = "UC7Q3HUnJA3nvjZR2JeMn2Cw"  # youtube.com/@hearts2hearts.official
BATCH = CACHE / "batch"
LIST_MAX_AGE = 6 * 3600  # s: re-list the channel after this long
# yt-dlp errors that won't go away by trying again
PERMANENT = re.compile(r"private video|members[- ]only|join this channel|video unavailable|"
                       r"has been removed|account .* terminated|premieres in|not available in your country|"
                       r"live event will begin|this live event", re.I)
# YouTube pushing back: stop the whole run
BLOCKED = re.compile(r"not a bot|sign in to confirm|http error 429|too many requests|rate.?limit", re.I)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def channel_id(text):
    m = re.search(r"(UC[A-Za-z0-9_-]{22})", text or "")
    return m.group(1) if m else None


def resolve_channel(text):
    """UC... id for a channel id / URL / @handle (one yt-dlp call for a handle)."""
    cid = channel_id(text)
    if cid:
        return cid
    import yt_dlp
    url = text if text.startswith("http") else f"https://www.youtube.com/{text.lstrip('/')}"
    with yt_dlp.YoutubeDL({"extract_flat": True, "quiet": True, "playlistend": 1}) as y:
        info = y.extract_info(url, download=False)
    cid = info.get("channel_id") or channel_id(info.get("id", ""))
    if not cid:
        raise SystemExit(f"Couldn't find the channel of {text}")
    return cid


def list_channel(cid, tabs):
    """[{id, title, duration, timestamp, tab}] newest first, without downloading."""
    import yt_dlp
    from scan import _YdlLogger
    opts = {"extract_flat": "in_playlist", "quiet": True, "no_warnings": True,
            "logger": _YdlLogger(False),
            "extractor_args": {"youtubetab": {"approximate_date": [""]}}}
    out = []
    with yt_dlp.YoutubeDL(opts) as y:
        for tab in tabs:
            try:
                info = y.extract_info(f"https://www.youtube.com/channel/{cid}/{tab}", download=False)
            except Exception as e:
                if "does not have a" in str(e):
                    continue
                raise
            for e in info.get("entries") or []:
                if e.get("id"):
                    out.append({"id": e["id"], "title": e.get("title"), "duration": e.get("duration"),
                                "timestamp": e.get("timestamp"), "tab": tab})
            channel = info.get("channel") or info.get("uploader")
    return out, channel


class State:
    def __init__(self, cid):
        BATCH.mkdir(parents=True, exist_ok=True)
        self.path = BATCH / f"{cid}.json"
        self.log_path = BATCH / f"{cid}.log"
        self.d = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.d.setdefault("channelId", cid)
        self.d.setdefault("videos", {})

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def log(self, msg):
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(f"{now()}  {msg}\n")

    def video(self, vid):
        return self.d["videos"].setdefault(vid, {})


def has_list(vid):
    from scan import SCANNER_VERSION
    f = SKIPLISTS / f"{vid}.json"
    if not f.exists():
        return False
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("scannerVersion") == SCANNER_VERSION
    except ValueError:
        return False


def refresh_listing(st, cid, tabs, force):
    age = time.time() - st.d.get("listedAtEpoch", 0)
    if not force and age < LIST_MAX_AGE and st.d.get("tabs") == tabs and st.d.get("order"):
        return
    print("Listing the channel's videos...", flush=True)
    entries, channel = list_channel(cid, tabs)
    st.d.update(channel=channel, tabs=tabs, listedAt=now(), listedAtEpoch=time.time(),
                order=[e["id"] for e in entries])
    for e in entries:
        v = st.video(e["id"])
        v.update({k: e[k] for k in ("title", "duration", "timestamp", "tab") if e[k] is not None})
    st.save()


def todo(st, retry_failed):
    out = []
    for vid in st.d["order"]:
        v = st.d["videos"][vid]
        if has_list(vid):
            if v.get("status") != "done":
                v.update(status="done", at=v.get("at") or now())
            continue
        if v.get("status") == "failed" and not retry_failed:
            continue
        out.append(vid)
    return out


def clock(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def wait(seconds, why):
    end = time.time() + seconds
    while True:
        left = end - time.time()
        if left <= 0:
            break
        sys.stdout.write(f"\r    {why}: {clock(left)} ".ljust(60))
        sys.stdout.flush()
        time.sleep(min(1.0, left))
    sys.stdout.write("\r" + " " * 60 + "\r")


def push():
    r = subprocess.run(["git", "push", "-q", "origin", "master"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        print(f"!! Upload failed (lists are committed on this PC): {r.stderr.strip()}\n"
              f"   Run  git push origin master  later.")
        return False
    print("Uploaded.")
    return True


def pair(text):
    a, _, b = text.partition("-")
    a = float(a)
    return a, float(b or a)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.chdir(ROOT)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("channel", nargs="?", default=H2H_CHANNEL, help="channel URL, @handle or UC... id")
    ap.add_argument("--shorts", action="store_true", help="also scan the Shorts tab")
    ap.add_argument("--streams", action="store_true", help="also scan the Live tab")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many videos (0 = all)")
    ap.add_argument("--max-minutes", type=float, default=0, help="skip videos longer than this")
    ap.add_argument("--pause", type=pair, default=(45, 120), help="s between downloads, e.g. 45-120")
    ap.add_argument("--batch", type=int, default=15, help="downloads between longer breaks")
    ap.add_argument("--break", dest="brk", type=pair, default=(600, 1200), help="s, e.g. 600-1200")
    ap.add_argument("--push", action="store_true", help="upload the new lists at the end")
    ap.add_argument("--keep-videos", action="store_true", help="don't delete the .mp4 after a scan")
    ap.add_argument("--retry-failed", action="store_true", help="try videos that failed for good again")
    ap.add_argument("--relist", action="store_true", help="list the channel again now")
    ap.add_argument("--list", action="store_true", help="only show what's still to do")
    ap.add_argument("--no-cookies", action="store_true", help="download without Firefox's cookies")
    a = ap.parse_args()
    if a.no_cookies:
        os.environ["KMS_COOKIES"] = "0"

    cid = resolve_channel(a.channel)
    st = State(cid)
    tabs = ["videos"] + (["shorts"] if a.shorts else []) + (["streams"] if a.streams else [])
    refresh_listing(st, cid, tabs, a.relist)
    queue = todo(st, a.retry_failed)
    if a.max_minutes:
        queue = [v for v in queue if (st.d["videos"][v].get("duration") or 0) <= a.max_minutes * 60]
    st.save()
    vids = st.d["videos"]
    done = sum(1 for v in st.d["order"] if vids[v].get("status") == "done")
    failed = sum(1 for v in st.d["order"] if vids[v].get("status") == "failed")
    hours = sum(vids[v].get("duration") or 0 for v in queue) / 3600
    print(f"{st.d.get('channel') or cid}: {len(st.d['order'])} videos, {done} scanned, "
          f"{failed} failed for good, {len(queue)} to do ({hours:.1f} h of video).")
    if a.list:
        for v in queue:
            print(f"  {v}  {clock(vids[v].get('duration') or 0)}  {vids[v].get('title')}")
        return 0
    if a.limit:
        queue = queue[:a.limit]
    if not queue:
        return 0

    import scan
    import scan_cli
    p = {k: v for k, v in scan.DEFAULTS.items()}
    new, downloads, rc = [], 0, 0
    st.log(f"start: {len(queue)} queued")
    try:
        for i, vid in enumerate(queue, 1):
            v = vids[vid]
            print(f"\n[{i}/{len(queue)}] {vid}  {clock(v.get('duration') or 0)}  {v.get('title')}", flush=True)
            cached = (CACHE / f"{vid}.faces.npz").exists() or (CACHE / f"{vid}.mp4").exists()
            if not cached and downloads:
                if downloads % a.batch == 0:
                    wait(random.uniform(*a.brk), "Longer break before the next download")
                else:
                    wait(random.uniform(*a.pause), "Pause before the next download")
            prog = scan_cli.Progress(sys.stdout)
            prog.start_download()
            t0 = time.time()
            try:
                res, T = scan_cli.scan_video(vid, p, False, None, False, prog)
            except scan.DownloadFailed as e:
                prog.end()
                msg = str(e)
                downloads += not cached
                if BLOCKED.search(msg):
                    print(f"!! YouTube is asking for a bot check / rate limiting: {msg}\n"
                          f"   Stopping. Wait a few hours (or open YouTube in Firefox once), then run again.")
                    v.update(status="blocked", error=msg, at=now())
                    st.log(f"{vid} BLOCKED {msg}")
                    rc = 3
                    break
                perm = bool(PERMANENT.search(msg))
                v.update(status="failed" if perm else "error", error=msg, at=now())
                st.log(f"{vid} {'failed' if perm else 'error'}: {msg}")
                print(f"!! Couldn't download: {msg}" + ("  (won't retry)" if perm else "  (will retry next run)"))
                continue
            except Exception as e:
                prog.end()
                msg = scan._one_line(e)
                v.update(status="error", error=msg, at=now())
                st.log(f"{vid} scan error: {msg}")
                print(f"!! Scan failed: {msg}")
                continue
            finally:
                st.save()
            prog.detect_end()
            downloads += not cached
            print(f"    {scan_cli.summary(res)}  [{time.time() - t0:.0f} s]")
            prc, out = scan_cli.publish(vid, no_push=True, capture=True)
            v.update(status="done", error=None, at=now(), committed=prc == 0)
            v.pop("error", None)
            st.save()
            st.log(f"{vid} done: {scan_cli.summary(res)}")
            new.append(vid)
            if not a.keep_videos:
                mp4 = CACHE / f"{vid}.mp4"
                if mp4.exists() and (CACHE / f"{vid}.faces.npz").exists():
                    try:
                        mp4.unlink()
                    except OSError:
                        pass
    except KeyboardInterrupt:
        print("\nStopped. Finished videos are kept; run again to continue.")
        st.log("stopped (Ctrl+C)")
        rc = 130
    finally:
        st.save()
    print(f"\n{len(new)} new skip list{'s' if len(new) != 1 else ''} this run.")
    if new and a.push:
        push()
    elif new:
        print("Committed on this PC. Upload with:  git push origin master   (or run with --push)")
    return rc


if __name__ == "__main__":
    sys.exit(main())

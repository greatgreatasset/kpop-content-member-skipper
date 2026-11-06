"""What scan.bat runs: ask for / take a link, scan it with progress, upload the skip list.

    scan.bat                         (double-click) asks for the link, waits for a key at the end
    scan.bat <link> [--no-push] [--profile illit] [scan.py options]

Every group is looked for; --profile only limits the scan to some groups.

All the logic lives here rather than in scan.bat: cmd.exe re-reads a running .bat
file from disk, so editing a long batch file while a scan is running breaks it,
and its quoting rules mangle links. scan.bat only starts this script.

Shows three stages, each on one updating line:
    1/3 Downloading video...   45%  (12.3 MB/s)
    2/3 Scanning faces...  [######----]  62%  4:10 / 6:42  ETA 0:25
    3/3 Uploading skip list...
Set KMS_NO_PUSH=1 (or pass --no-push) to commit the skip list without uploading.
"""
import contextlib
import io
import os
import subprocess
import sys
import time
import traceback
import warnings

warnings.filterwarnings("ignore", category=FutureWarning)

from common import ROOT  # noqa: E402

NOTHING = "\n!! Scan failed - nothing was uploaded."
WIDTH = 78  # stay under 80 columns: a wrapped line can't be redrawn with \r


def clock(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


class Progress:
    """Single redrawn line per stage, written to the real console."""

    def __init__(self, out):
        self.out = out
        self.last = 0.0
        self.open = False
        self.duration = None
        self.t0 = None
        self.dl_done = False

    def line(self, text, force=False):
        now = time.perf_counter()
        if not force and now - self.last < 0.2:
            return
        self.last = now
        self.out.write("\r" + text[:WIDTH].ljust(WIDTH))
        self.out.flush()
        self.open = True

    def end(self, text=None):
        if text is not None:
            self.line(text, force=True)
        if self.open:
            self.out.write("\n")
            self.out.flush()
            self.open = False

    def start_download(self):
        self.line("1/3 Downloading video...", force=True)

    def download_hook(self, d):
        st = d.get("status")
        if st == "downloading":
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            speed = d.get("speed")
            pct = f"{100 * done / total:3.0f}%" if total else f"{done / 1e6:.0f} MB"
            sp = f"  ({speed / 1e6:.1f} MB/s)" if speed else ""
            self.line(f"1/3 Downloading video...  {pct}{sp}")
        elif st == "finished" and not self.dl_done:
            self.dl_done = True
            total = d.get("total_bytes") or d.get("downloaded_bytes")
            size = f"  ({total / 1e6:.0f} MB)" if total else ""
            self.end(f"1/3 Downloading video...  done{size}")

    def finish_download(self):
        if not self.dl_done:  # yt-dlp found the file from an earlier scan
            self.dl_done = True
            self.end("1/3 Downloading video...  already downloaded")

    def detect_start(self, duration):
        self.finish_download()
        self.duration = duration
        self.t0 = time.perf_counter()
        self.line("2/3 Scanning faces...  starting", force=True)

    def frame(self, t):
        if not self.duration:
            self.line(f"2/3 Scanning faces...  {clock(t)}")
            return
        f = min(1.0, t / self.duration)
        bar = "#" * int(f * 10) + "-" * (10 - int(f * 10))
        el = time.perf_counter() - self.t0
        eta = f"  ETA {clock(el * (1 - f) / f)}" if f > 0.02 else ""
        self.line(f"2/3 Scanning faces...  [{bar}] {100 * f:3.0f}%  "
                  f"{clock(t)} / {clock(self.duration)}{eta}")

    def detect_end(self):
        if self.t0 is not None:
            self.end(f"2/3 Scanning faces...  [##########] 100%  done in "
                     f"{clock(time.perf_counter() - self.t0)}")

    def cached(self):
        self.finish_download()
        self.end("2/3 Scanning faces...  using earlier scan of this video")


def run(argv, interactive, out):
    import scan

    ap = scan.build_parser(prog="scan.bat")
    ap.add_argument("--no-push", action="store_true", help="commit the skip list but don't upload it")
    if interactive:
        text = input("Paste the YouTube link: ").replace("﻿", "").strip()
        if not text:
            print("No link given.")
            return 1
        argv = text.split()
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return e.code or 0
    if a.no_cookies:
        os.environ["KMS_COOKIES"] = "0"
    vid = scan.parse_link(a.url)
    if not vid:
        print(f"Couldn't find a YouTube video ID in: {' '.join(a.url)}")
        print(NOTHING)
        return 2
    p = {k: getattr(a, k) for k in scan.DEFAULTS}
    prog = Progress(out)
    prog.start_download()
    try:
        res, T = scan_video(vid, p, a.rescan, a.profile, a.debug, prog)
    except scan.DownloadFailed as e:
        prog.end()
        print(f"Couldn't download the video: {e}. Check the link and your internet.")
        print(NOTHING)
        return 3
    except Exception as e:
        prog.end()
        if a.debug:
            traceback.print_exc()
        else:
            print(f"Scan failed: {scan._one_line(e)}  (run with --debug for details)")
        print(NOTHING)
        return 4
    prog.detect_end()
    print(f"    {res['videoId']}: {summary(res)}  [{T['total']:.0f} s total]")

    print("3/3 Uploading skip list...", flush=True)
    rc, _ = publish(res["videoId"], a.no_push or os.environ.get("KMS_NO_PUSH") == "1")
    return rc


# ---- shared with native_host.py (the extension's "Filter this video" button)

def scan_video(vid, p, rescan, profile, debug, progress):
    """Download + scan one video, writing skiplists/<vid>.json (profile: None = all groups).
    Returns (skip list dict, timings). Raises scan.DownloadFailed or other errors."""
    import scan

    # insightface/onnxruntime print setup chatter; keep it off the progress lines
    chatter = io.StringIO()
    with contextlib.redirect_stdout(sys.stdout if debug else chatter):
        res, T, _, _, _ = scan.scan(scan.watch_url(vid), p, rescan, profile, debug, progress=progress)
    return res, T


def summary(res):
    def n(k, word):
        return f"{k} {word}{'' if k == 1 else 's'}"
    secs = round(sum(s["end"] - s["start"] for s in res["skips"]))
    hl = ", ".join(n(len(h["ranges"]), f"{h['name']} part") for h in res["highlights"])
    groups = " + ".join(g["name"] for g in res.get("groups", [])) or "no known group"
    return f"{groups}: {n(len(res['skips']), 'skip')} ({secs} s)" + (f", {hl}" if hl else "")


def publish(vid, no_push, capture=False):
    """Refresh the bundled copies, then commit (+ push) only this video's skip list.
    Returns (exit code, output if capture). 0 = uploaded/unchanged, 2 = committed
    but the push failed, 5 = bundled copies could not be refreshed."""
    py = sys.executable
    r = subprocess.run([py, "sync_skiplists.py"], cwd=ROOT, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE if capture else None, text=True, encoding="utf-8")
    if r.returncode:
        msg = "Could not refresh the bundled skip lists."
        if not capture:
            print("!! " + msg)
        return 5, msg + (f"\n{r.stderr}" if capture else "")
    cmd = [py, "publish_skiplists.py", vid]
    if no_push:
        cmd.append("--no-push")
    r = subprocess.run(cmd, cwd=ROOT, capture_output=capture, text=True, encoding="utf-8",
                       errors="replace")
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip() if capture else ""


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.chdir(ROOT)
    argv = sys.argv[1:]
    interactive = not argv
    out = sys.stdout
    try:
        rc = run(argv, interactive, out)
    except KeyboardInterrupt:
        print("\nStopped.")
        rc = 130
    except EOFError:
        print("\nNo link given.")
        rc = 1
    if interactive and sys.stdin.isatty():  # double-clicked: keep the window open
        print("\nPress any key to close...", end="", flush=True)
        try:
            import msvcrt
            msvcrt.getch()
        except Exception:
            pass
        print()
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()

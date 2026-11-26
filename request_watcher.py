"""Scan the videos friends ask for with the add-on's "Request filtering" button.

    watch_requests.bat                 listen and scan (leave the window open)
    watch_requests.bat /install        also start it (minimized) when you log in to Windows
    watch_requests.bat /uninstall      stop starting it at login
    python request_watcher.py --code       show the request code to give to friends
    python request_watcher.py --new-code   make a new code (the old one stops working)
    python request_watcher.py --once       handle what is waiting, then exit

Friends' PCs have no scanner, so their add-on can't scan. Instead it posts the
video's ID to a topic on a public relay (ntfy.sh, no account needed), and this
script, running on the PC with the scanner, listens on that topic, scans the
video like "Filter this video" does (same worker, same commit + push of the skip
list) and posts short progress messages back. The friend's add-on then fetches
the list from GitHub as always.

The topic's name is the "request code": 20 letters and digits made here, kept
in the gitignored requests.local.json and typed once by each friend (add-on
Settings, or when they first click "Request filtering"). It is not in the
add-on or the repo, so only people you gave it to can ask for scans. Nothing
on this PC is reachable from outside: this script only makes outgoing requests,
and all it ever takes from the relay is an 11-character YouTube video ID.

Messages on the topic (JSON text):
    request  {"k": "req", "v": "<videoId>"}
    status   {"k": "st", "v": "<videoId>", "s": "queued|scanning|upload|done|error",
              "p": percent, "e": eta seconds, "q": videos ahead in line, "m": "message"}
Statuses never carry paths, names or error details; those go to
cache/requests/watcher.log only.

Limits (environment variables): KMS_REQ_MAX_PER_DAY (30 scans a day),
KMS_REQ_MAX_QUEUE (20 videos waiting). KMS_RELAY: another ntfy server.
"""
import argparse
import datetime
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import native_host as nh

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "requests.local.json"  # {"code": "..."}; gitignored
# KMS_REQ_CODE (tests: another code for one run) keeps its own state
STATE_DIR = ROOT / "cache" / ("requests-test" if os.environ.get("KMS_REQ_CODE") else "requests")
STATE = STATE_DIR / "state.json"       # {"since": last message id, "day": "...", "count": scans today}
PIDFILE = STATE_DIR / "watcher.pid"
LOG = STATE_DIR / "watcher.log"
RELAY = os.environ.get("KMS_RELAY", "https://ntfy.sh/").rstrip("/") + "/"
MAX_PER_DAY = int(os.environ.get("KMS_REQ_MAX_PER_DAY", "30"))
MAX_QUEUE = int(os.environ.get("KMS_REQ_MAX_QUEUE", "20"))
CODE_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"  # no i l o 0 1 (the same as the add-on's background.js)
CODE_LEN = 20
PROGRESS_MARKS = (35, 65)   # a status at these percents; the relay allows ~250 messages a day
FAIL_KEEP = 30 * 60         # answer a repeated request for a failed video from memory for this long
STALL = 45 * 60             # a scan with no progress for this long is stopped
STARTUP_NAME = "K-pop skipper scan requests.bat"

MSG_DOWNLOAD = ("The video couldn't be downloaded (it may be private, members-only, "
                "age-restricted or a live stream).")
MSG_SCAN = "The scan failed on the scanner PC."
MSG_UPLOAD = "The video was filtered, but uploading the skip list failed. Ask again later."
MSG_BUSY_DAY = "The scanner PC has filtered as many videos as it does in a day. Ask again tomorrow."
MSG_BUSY_QUEUE = "Too many videos are waiting to be filtered. Ask again later."


# ------------------------------------------------------------ request code

def _check(body):
    n = len(CODE_CHARS)
    s = sum((i + 1) * CODE_CHARS.index(c) for i, c in enumerate(body)) % (n * n)
    return CODE_CHARS[s // n] + CODE_CHARS[s % n]


def new_code():
    body = "".join(secrets.choice(CODE_CHARS) for _ in range(CODE_LEN - 2))
    return body + _check(body)


def valid_code(code):
    return (isinstance(code, str) and len(code) == CODE_LEN and all(c in CODE_CHARS for c in code)
            and _check(code[:-2]) == code[-2:])


def load_code(create=True, replace=False):
    code = None
    if not replace:
        try:
            code = json.loads(CONFIG.read_text(encoding="utf-8")).get("code")
        except (OSError, ValueError):
            pass
    if not valid_code(code):
        if not create:
            return None
        code = new_code()
        CONFIG.write_text(json.dumps({"code": code}, indent=1) + "\n", encoding="utf-8")
    return code


def pretty(code):
    return "-".join(code[i:i + 5] for i in range(0, len(code), 5))


# ------------------------------------------------------------ small helpers

_log_lock = threading.Lock()


def say(text, also_print=True):
    stamp = datetime.datetime.now().strftime("%m-%d %H:%M:%S")
    with _log_lock:
        if also_print:
            print(f"{stamp[6:]}  {text}", flush=True)
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(f"{stamp}  {text}\n")
        except OSError:
            pass


def read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    os.replace(tmp, STATE)


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def ensure_pushed():
    """True when origin has every local commit (pushing them if needed), so a
    "done" status is only sent for a list friends can really fetch."""
    r = git("rev-list", "--count", "origin/master..master")
    if r.returncode == 0 and r.stdout.strip() == "0":
        return True
    r = git("push", "-q", "origin", "master")
    if r.returncode:
        say(f"   upload failed: {nh_one_line(r.stderr)}")
        return False
    return True


def nh_one_line(text):
    return " ".join(str(text).split())[:300]


def has_list(vid):
    return (ROOT / "skiplists" / f"{vid}.json").exists()


# ------------------------------------------------------------ relay

class Relay:
    def __init__(self, code):
        self.url = f"{RELAY}kms-{code}"

    def _open(self, url, data=None, timeout=30):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "kms-request-watcher"})
        return urllib.request.urlopen(req, timeout=timeout)

    def post(self, body):
        """Publish one message. Never raises: a lost status only delays a friend's screen."""
        for attempt in range(3):
            try:
                with self._open(self.url, json.dumps(body, separators=(",", ":")).encode("utf-8")):
                    return True
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    say("   (the relay's message limit is reached; a status was not sent)", False)
                    return False
            except OSError:
                pass
            time.sleep(2 * (attempt + 1))
        return False

    def status(self, vid, state, **kw):
        return self.post({"k": "st", "v": vid, "s": state, **kw})

    def messages(self, since, poll=False):
        """Yield (id, time, body dict) for messages after `since`; with poll=False
        it keeps listening (raises OSError when the connection drops)."""
        url = f"{self.url}/json?since={since or '12h'}" + ("&poll=1" if poll else "")
        with self._open(url, timeout=120) as resp:  # the relay sends a keepalive every 45 s
            for raw in resp:
                try:
                    m = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                if m.get("event") == "open":
                    yield None, None, None  # connected
                    continue
                if m.get("event") != "message":
                    continue
                try:
                    body = json.loads(m.get("message") or "")
                except ValueError:
                    body = None
                yield m.get("id"), m.get("time"), body if isinstance(body, dict) else None


# ------------------------------------------------------------ the queue

class Watcher:
    def __init__(self, relay):
        self.relay = relay
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.queue = []       # video ids waiting
        self.current = None   # (video id, last status kwargs)
        self.failed = {}      # video id -> (time, message)
        self.state = read_state()

    # -- requests (listener thread)
    def _scans_today(self):
        today = datetime.date.today().isoformat()
        if self.state.get("day") != today:
            self.state.update(day=today, count=0)
        return self.state.get("count", 0)

    def on_request(self, vid):
        with self.lock:
            if self.current and self.current[0] == vid:
                self.relay.status(vid, "scanning", **self.current[1])
                return
            if vid in self.queue:
                self.relay.status(vid, "queued", q=self.queue.index(vid) + (1 if self.current else 0))
                return
            if has_list(vid):
                ok = ensure_pushed()
                say(f"{vid}: asked for, already filtered")
                if ok:
                    self.relay.status(vid, "done", p=100)
                else:
                    self.relay.status(vid, "error", m=MSG_UPLOAD)
                return
            failed = self.failed.get(vid)
            if failed and time.time() - failed[0] < FAIL_KEEP:
                self.relay.status(vid, "error", m=failed[1])
                return
            if self._scans_today() >= MAX_PER_DAY:
                say(f"{vid}: asked for, but today's limit of {MAX_PER_DAY} scans is reached")
                self.relay.status(vid, "error", m=MSG_BUSY_DAY)
                return
            if len(self.queue) >= MAX_QUEUE:
                say(f"{vid}: asked for, but {MAX_QUEUE} videos are already waiting")
                self.relay.status(vid, "error", m=MSG_BUSY_QUEUE)
                return
            ahead = len(self.queue) + (1 if self.current else 0)
            self.queue.append(vid)
            self.state["count"] = self._scans_today() + 1
            write_state(self.state)
            say(f"{vid}: asked for" + (f" ({ahead} ahead of it)" if ahead else ""))
            if ahead:  # otherwise the scan's own first status follows at once
                self.relay.status(vid, "queued", q=ahead)
        self.wake.set()

    def seen(self, msg_id):
        with self.lock:
            self.state["since"] = msg_id
            write_state(self.state)

    def listen(self, once=False):
        """Read requests from the relay, forever (or just what is waiting)."""
        delay, down = 5, False
        while True:
            try:
                for msg_id, _, body in self.relay.messages(self.state.get("since"), poll=once):
                    if msg_id is None:
                        if down:
                            say("Connected again.")
                        delay, down = 5, False
                        continue
                    if body and body.get("k") == "req" and nh.ID_RE.match(str(body.get("v") or "")):
                        self.on_request(body["v"])
                    self.seen(msg_id)
                if once:
                    return
            except Exception as e:  # dropped connection, timeout, half a line...
                if once:
                    raise
                if not down:
                    say(f"No connection to the relay ({nh_one_line(e)[:80]}); trying again...")
                down = True
            time.sleep(delay)
            delay = min(60, delay * 2)

    # -- scans (main thread)
    def next(self):
        with self.lock:
            if not self.queue:
                return None
            vid = self.queue.pop(0)
            self.current = (vid, {"p": 0})
            return vid

    def _status(self, vid, state, **kw):
        with self.lock:
            if self.current and self.current[0] == vid:
                self.current = (vid, kw)
        self.relay.status(vid, state, **kw)

    def scan(self, vid):
        """Run the same worker as "Filter this video" and report its progress."""
        say(f"{vid}: filtering...")
        job = nh.read_job(vid)
        if not nh.running(job):  # else: already being scanned here (Filter this video); follow it
            nh.JOBS.mkdir(parents=True, exist_ok=True)
            proc = nh.start_worker(vid, "", False)
            nh.write_job(vid, dict(state="starting", pid=proc.pid, videoId=vid, profile=None,
                                   stage="starting", percent=0, eta=None, started=time.time(),
                                   updated=time.time()))
        self._status(vid, "scanning", p=0)
        marks = list(PROGRESS_MARKS)
        while True:
            time.sleep(1)
            job = nh.read_job(vid) or {}
            state = job.get("state")
            if state == "done":
                break
            if state == "error":
                detail = job.get("message", "")
                say(f"{vid}: failed. {detail}")
                return self.fail(vid, MSG_DOWNLOAD if detail.startswith("Couldn't download") else MSG_SCAN)
            if not nh.pid_alive(job.get("pid")):
                say(f"{vid}: the scanner stopped unexpectedly (see cache/jobs/{vid}.log)")
                return self.fail(vid, MSG_SCAN)
            if time.time() - job.get("updated", time.time()) > STALL:
                say(f"{vid}: no progress for {STALL // 60} minutes, stopped")
                subprocess.run(["taskkill", "/PID", str(job.get("pid")), "/T", "/F"], capture_output=True)
                return self.fail(vid, MSG_SCAN)
            pct = job.get("percent") or 0
            if marks and pct >= marks[0] and job.get("stage") == "scan":
                while marks and pct >= marks[0]:
                    marks.pop(0)
                kw = {"p": pct}
                if job.get("eta"):
                    kw["e"] = job["eta"]
                self._status(vid, "scanning", **kw)
        if not (job.get("pushed") or ensure_pushed()):
            say(f"{vid}: filtered, but not uploaded ({job.get('note', '')})")
            return self.fail(vid, MSG_UPLOAD, remember=False)
        say(f"{vid}: done" + (f" - {summary(job.get('list'))}" if job.get("list") else ""))
        self.relay.status(vid, "done", p=100)
        return True

    def fail(self, vid, message, remember=True):
        if remember:
            self.failed[vid] = (time.time(), message)
        self.relay.status(vid, "error", m=message)
        return False

    def work(self, once=False):
        while True:
            vid = self.next()
            if vid is None:
                if once:
                    return
                self.wake.wait(5)
                self.wake.clear()
                continue
            try:
                self.scan(vid)
            except Exception as e:  # one bad video must not stop the watcher
                say(f"{vid}: failed. {nh_one_line(repr(e))}")
                self.fail(vid, MSG_SCAN)
            finally:
                with self.lock:
                    self.current = None


def summary(res):
    try:
        import scan_cli
        return scan_cli.summary(res)
    except Exception:
        return ""


# ------------------------------------------------------------ start at login

def startup_file():
    return (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            / "Startup" / STARTUP_NAME)


def install():
    f = startup_file()
    f.write_text(f'@echo off\r\nstart "K-pop skipper scan requests" /min "{ROOT / "watch_requests.bat"}"\r\n',
                 encoding="mbcs")
    print("Installed: scan requests are listened for (in a minimized window) whenever you log in.")
    print("Start it now by double-clicking watch_requests.bat.")
    return 0


def uninstall():
    startup_file().unlink(missing_ok=True)
    print("Uninstalled: it no longer starts at login. Close its window to stop it now.")
    return 0


# ------------------------------------------------------------ main

def already_running():
    try:
        pid = int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return False
    return pid != os.getpid() and nh.pid_alive(pid)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Scan the videos friends ask for with 'Request filtering'.")
    ap.add_argument("--code", action="store_true", help="show the request code and exit")
    ap.add_argument("--new-code", action="store_true", help="make a new request code (the old one stops working)")
    ap.add_argument("--install", action="store_true", help="start at Windows login")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--once", action="store_true", help="handle the requests that are waiting, then exit")
    a = ap.parse_args()
    os.chdir(ROOT)
    if a.install:
        return install()
    if a.uninstall:
        return uninstall()
    code = os.environ.get("KMS_REQ_CODE") or load_code(replace=a.new_code)  # KMS_REQ_CODE: tests
    if not valid_code(code):
        print("!! KMS_REQ_CODE is not a valid request code.")
        return 1
    if a.code or a.new_code:
        print(f"Request code: {pretty(code)}")
        if a.new_code:
            print("The old code no longer works: give friends the new one (add-on Settings > Request code),")
            print("and restart watch_requests.bat if it is running.")
        return 0
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if already_running():
        print("Scan requests are already being listened for (another window). Nothing to do.")
        return 0
    PIDFILE.write_text(str(os.getpid()))
    print("Listening for \"Request filtering\" from friends' add-ons. Leave this window open;")
    print("close it (or Ctrl+C) to stop. Requests made while it is closed wait up to 12 hours.")
    print(f"\n    Request code for friends: {pretty(code)}\n")
    w = Watcher(Relay(code))
    try:
        if a.once:
            w.listen(once=True)
            w.work(once=True)
            return 0
        threading.Thread(target=w.listen, daemon=True).start()
        w.work()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        PIDFILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

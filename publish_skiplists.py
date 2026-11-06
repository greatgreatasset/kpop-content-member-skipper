"""Commit and push only skip-list files (used by scan.bat after a scan).

    python publish_skiplists.py [<videoId>] [--no-push]

Stages skiplists/<id>.json, skiplists/index.json and the bundled copies in
extension/skiplists/, commits exactly those paths (anything else that happens
to be staged is left alone), pushes to origin master, and prints a one-line
summary. Exit code 0 = published (or nothing new), 2 = committed locally but
the push failed.
"""
import argparse
import json
import subprocess
import sys

from common import CACHE, ROOT, SKIPLISTS


def git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r


def summary(d):
    secs = round(sum(s["end"] - s["start"] for s in d["skips"]))
    parts = [f"{len(d['skips'])} skip{'s' if len(d['skips']) != 1 else ''} ({secs} s)"]
    for h in d.get("highlights", []):
        n = len(h["ranges"])
        parts.append(f"{n} {h['name']} part{'s' if n != 1 else ''}")
    return ", ".join(parts)


def group_names(d):
    return " + ".join(g["name"] for g in d.get("groups", [])) or "no known group"


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id", nargs="?", help="default: the video scan.py scanned last")
    ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args()
    if not a.video_id:
        a.video_id = json.loads((CACHE / "last_scan.json").read_text(encoding="utf-8"))["videoId"]
    d = json.loads((SKIPLISTS / f"{a.video_id}.json").read_text(encoding="utf-8"))
    title, summ = d.get("title") or a.video_id, f"{group_names(d)}: {summary(d)}"
    paths = [f"skiplists/{a.video_id}.json", "skiplists/index.json",
             f"extension/skiplists/{a.video_id}.json", "extension/skiplists/index.json",
             "extension/skiplists/profiles.json", "skiplists/profiles.json"]
    paths = [p for p in paths if (ROOT / p).exists()]
    git("add", "--", *paths)
    if git("diff", "--cached", "--quiet", "--", *paths, check=False).returncode == 0:
        print(f"\nDone: {summ}. (Skip list unchanged, nothing to upload.)")
        return 0
    git("commit", "-q", "-m", f"Skip list: {a.video_id} ({group_names(d)})", "--", *paths)
    if a.no_push:
        print(f"\nDone: {summ}. Committed locally (not uploaded).")
        return 0
    r = git("push", "-q", "origin", "master", check=False)
    if r.returncode:
        print(f"\n!! UPLOAD FAILED - the skip list is saved and committed on this PC only.\n"
              f"{r.stderr.strip()}\n"
              f"Fix the problem (e.g. internet connection) and run:  git push origin master\n"
              f"Meanwhile, click Reload on the add-on in about:debugging to use the local copy.")
        print(f"\nScanned: {summ}.")
        return 2
    print(f"\nDone: {summ}.\n\"{title}\"\nUploaded. Refresh the YouTube tab (it can take a minute or two to appear).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

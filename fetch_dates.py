"""Exact upload dates of the batch-listed videos (metadata only, paced) -> cache/batch/upload_dates.json.

    .venv\Scripts\python fetch_dates.py

The channel listing only gives approximate dates ("1 year ago"); screen_time.py
uses these when present. Already fetched ids are skipped.
"""
import json
import random
import sys
import time

from common import CACHE
import scan

OUT = CACHE / "batch" / "upload_dates.json"


def main():
    import yt_dlp
    have = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    ids = []
    for f in (CACHE / "batch").glob("UC*.json"):
        ids += json.loads(f.read_text(encoding="utf-8")).get("order", [])
    todo = [i for i in ids if i not in have]
    print(f"{len(have)} known, {len(todo)} to fetch", flush=True)
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": scan._YdlLogger(False),
            "ignore_no_formats_error": True}
    with scan._ydl_with_cookies(yt_dlp, opts) as y:
        for n, vid in enumerate(todo, 1):
            try:
                info = y.extract_info(scan.watch_url(vid), download=False, process=False)
            except Exception as e:
                msg = scan._one_line(e)
                print(f"{vid}: {msg}", flush=True)
                if "not a bot" in msg or "429" in msg:
                    break
                continue
            d = info.get("upload_date")
            if d:
                have[vid] = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                OUT.write_text(json.dumps(have, indent=0), encoding="utf-8")
            if n % 25 == 0:
                print(f"{n}/{len(todo)}", flush=True)
            time.sleep(random.uniform(1.5, 3.5))
    print(f"done: {len(have)} dates")


if __name__ == "__main__":
    sys.exit(main())

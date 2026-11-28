"""Publish a signed .xpi (made by sign.bat) as a GitHub release and add it to
updates.json, so installed copies of the extension update themselves. The
release also gets the zip for Chrome, Edge and Brave (build_chromium.py).

    python release.py [--dry-run]

Steps: find dist/*-<version>.xpi for the version in extension/manifest.json,
copy it to dist/kpop-content-member-skipper-<version>.xpi, build
dist/kpop-content-member-skipper-<version>-chromium.zip, create GitHub
release v<version> with both attached (gh CLI), add the version to
updates.json, commit only updates.json and push. Firefox checks updates.json
(the manifest's update_url) about once a day; the Chromium build is loaded
unpacked and updated by hand.
"""
import argparse
import json
import shutil
import subprocess
import sys

import build_chromium
from common import ROOT

REPO = "greatgreatasset/kpop-content-member-skipper"
SLUG = "kpop-content-member-skipper"


def run(*cmd, check=True):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"!! {' '.join(cmd[:3])} failed:\n{r.stderr.strip()}")
    return r


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="only update updates.json locally")
    a = ap.parse_args()
    manifest = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
    ver = manifest["version"]
    addon_id = manifest["browser_specific_settings"]["gecko"]["id"]
    signed = sorted((ROOT / "dist").glob(f"*-{ver}.xpi"))
    signed = [p for p in signed if p.name != f"{SLUG}-{ver}.xpi"] or signed
    if not signed:
        raise SystemExit(f"!! No signed .xpi for version {ver} in dist\\ - run sign.bat first.")
    xpi = ROOT / "dist" / f"{SLUG}-{ver}.xpi"
    if signed[0] != xpi:
        shutil.copyfile(signed[0], xpi)
    tag = f"v{ver}"
    link = f"https://github.com/{REPO}/releases/download/{tag}/{xpi.name}"

    upd_path = ROOT / "updates.json"
    upd = json.loads(upd_path.read_text(encoding="utf-8")) if upd_path.exists() else {"addons": {}}
    entries = upd.setdefault("addons", {}).setdefault(addon_id, {}).setdefault("updates", [])
    if any(e["version"] == ver for e in entries):
        print(f"updates.json already lists {ver}.")
    else:
        entries.append({"version": ver, "update_link": link})
    upd_path.write_text(json.dumps(upd, indent=1) + "\n", encoding="utf-8")
    _, czip, _ = build_chromium.build()  # the same code for Chrome, Edge and Brave
    if a.dry_run:
        print(f"[dry run] {xpi.name} -> {link}")
        print(f"[dry run] {czip.name} ({czip.stat().st_size / 1e6:.1f} MB) would be attached too")
        return

    if run("gh", "release", "view", tag, "-R", REPO, check=False).returncode == 0:
        print(f"Release {tag} already exists; not uploading the .xpi again.")
        run("gh", "release", "upload", tag, str(czip), "-R", REPO, "--clobber")
        print(f"Attached {czip.name} to it.")
    else:
        run("gh", "release", "create", tag, str(xpi), str(czip), "-R", REPO, "--title", f"Extension {ver}",
            "--notes", "Firefox: open the .xpi in Firefox to install; installed copies update "
                       "automatically.\n\n"
                       "Chrome, Edge, Brave: download the -chromium.zip, unzip it to a folder you keep, "
                       "open chrome://extensions, turn on Developer mode, click \"Load unpacked\" and "
                       "pick that folder. Updates: replace the folder's contents with the new zip and "
                       "click the reload arrow.")
        print(f"Created release {tag} with {xpi.name} and {czip.name}")
    run("git", "add", "--", "updates.json")
    if run("git", "diff", "--cached", "--quiet", "--", "updates.json", check=False).returncode:
        run("git", "commit", "-q", "-m", f"updates.json: extension {ver}", "--", "updates.json")
    r = run("git", "push", "-q", "origin", "master", check=False)
    if r.returncode:
        raise SystemExit(f"!! Push failed (updates.json committed locally):\n{r.stderr.strip()}")
    print(f"Published. Download link for friends: https://github.com/{REPO}/releases/latest")


if __name__ == "__main__":
    main()

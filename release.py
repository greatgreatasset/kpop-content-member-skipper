"""Publish a signed .xpi (made by sign.bat) as a GitHub release and add it to
updates.json, so installed copies of the extension update themselves.

    python release.py [--dry-run]

Steps: find dist/*-<version>.xpi for the version in extension/manifest.json,
copy it to dist/kpop-content-member-skipper-<version>.xpi, create GitHub
release v<version> with it attached (gh CLI), add the version to updates.json,
commit only updates.json and push. Firefox checks updates.json (the manifest's
update_url) about once a day.
"""
import argparse
import json
import shutil
import subprocess
import sys

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
    if a.dry_run:
        print(f"[dry run] {xpi.name} -> {link}")
        return

    if run("gh", "release", "view", tag, "-R", REPO, check=False).returncode == 0:
        print(f"Release {tag} already exists; not uploading again.")
    else:
        run("gh", "release", "create", tag, str(xpi), "-R", REPO, "--title", f"Extension {ver}",
            "--notes", "Signed Firefox add-on. Open the .xpi in Firefox to install; "
                       "installed copies update automatically.")
        print(f"Created release {tag} with {xpi.name}")
    run("git", "add", "--", "updates.json")
    if run("git", "diff", "--cached", "--quiet", "--", "updates.json", check=False).returncode:
        run("git", "commit", "-q", "-m", f"updates.json: extension {ver}", "--", "updates.json")
    r = run("git", "push", "-q", "origin", "master", check=False)
    if r.returncode:
        raise SystemExit(f"!! Push failed (updates.json committed locally):\n{r.stderr.strip()}")
    print(f"Published. Download link for friends: https://github.com/{REPO}/releases/latest")


if __name__ == "__main__":
    main()

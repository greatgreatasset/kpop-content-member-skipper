"""Build the extension for Chromium browsers (Chrome, Edge, Brave).

    python build_chromium.py [--no-zip]

The code in extension/ is the same for every browser; only the manifest
differs. This copies extension/ to dist/chromium/ with a Chromium manifest made
from extension/manifest.json (service worker instead of background scripts, PNG
icons, no Firefox settings) and zips it as
dist/kpop-content-member-skipper-<version>-chromium.zip (manifest at the top
level of the zip: unzip, then "Load unpacked" on the folder).

dist/chromium/ can be loaded directly: chrome://extensions (edge://extensions,
brave://extensions) -> Developer mode -> Load unpacked.

The manifest's "key" pins the extension ID (CHROMIUM_ID), wherever the folder
is: settings survive moving or replacing the folder, and the native host
("Filter this video", native_host.py) can name the extension it accepts.
"""
import argparse
import base64
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "extension"
OUT = ROOT / "dist" / "chromium"
SLUG = "kpop-content-member-skipper"
SKIP = {"manifest.json", ".amo-upload-uuid", "icon.svg"}  # not part of the Chromium build
ICON_SIZES = (16, 32, 48, 128)
MIN_CHROME = "110"

# Public key (DER, base64). Chromium derives the extension ID from it. Nothing
# needs the private key: the extension is loaded unpacked, never packed as .crx.
KEY = ("MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA+8eFsGUtAVnxiCS79IhoLpTBCiSDcJ/yBcAtbiV2tM7DWeTiy5DKledx"
       "+s2C3tAsjNqCQfynyJee4tyDeF0GWJROVDY1XBiZDZz8z3aYFHJoqex68/RTtGhsrBu/c1rDB7TDnYm6ndbSQh6PS/fzpK/DcTFj"
       "1dwmijd+b4+jqcxNf5+0KLlURXMdHGnYxZ3H0VxCak3UDyMqCZ71xM4IywcU1qK5IR9JtzXl7mxMhs02em9TpuseMSn2yw4hdFNQ"
       "Ux1Wr1Invf/2a4YxVqr7AuWYvfV1OVin/Xfw7vlnXvxbU412WEROIJkrgARgPbGTqw3SLiaAgNXXeFzKrht75QIDAQAB")


def extension_id(key=KEY):
    """The ID Chromium gives an extension with this key: the first 16 bytes of
    the key's SHA-256, written with the letters a-p."""
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


CHROMIUM_ID = extension_id()  # fddakmaaelefbchjakeceijlhdjpfejg


def chromium_manifest(m):
    """extension/manifest.json (Firefox) -> the manifest for Chromium browsers."""
    out = {}
    for k, v in m.items():
        if k == "browser_specific_settings":
            continue  # Firefox only (add-on ID, update URL)
        if k == "description":
            out[k] = v
            out["key"] = KEY
            out["minimum_chrome_version"] = MIN_CHROME
        elif k == "background":
            out[k] = {"service_worker": "background.js"}  # it loads compat.js itself
        elif k == "icons":
            out[k] = {str(n): f"icons/icon-{n}.png" for n in ICON_SIZES}  # no SVG icons in Chromium
        elif k == "action":
            out[k] = {**v, "default_icon": {str(n): f"icons/icon-{n}.png" for n in ICON_SIZES[:2]}}
        elif k == "options_ui":
            out[k] = {**v, "open_in_tab": True}  # a tab of its own, not a small dialog
        else:
            out[k] = v
    missing = {"key", "background", "icons", "action"} - set(out)
    if missing:
        raise SystemExit(f"!! extension/manifest.json has no {', '.join(sorted(missing))}")
    return out


def source_files():
    return sorted(p for p in SRC.rglob("*") if p.is_file() and p.relative_to(SRC).as_posix() not in SKIP)


def build(make_zip=True):
    """-> (folder, zip path or None, version)"""
    m = json.loads((SRC / "manifest.json").read_text(encoding="utf-8"))
    manifest = json.dumps(chromium_manifest(m), indent=2, ensure_ascii=False) + "\n"
    files = source_files()
    for n in ICON_SIZES:
        if not (SRC / "icons" / f"icon-{n}.png").exists():
            raise SystemExit(f"!! extension/icons/icon-{n}.png is missing")
    # update the folder in place (a browser may have it loaded): copy, then
    # remove what is no longer in extension/
    OUT.mkdir(parents=True, exist_ok=True)
    keep = {"manifest.json"}
    for p in files:
        rel = p.relative_to(SRC)
        keep.add(rel.as_posix())
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dst)
    (OUT / "manifest.json").write_text(manifest, encoding="utf-8")
    for p in sorted(OUT.rglob("*"), reverse=True):
        if p.is_file() and p.relative_to(OUT).as_posix() not in keep:
            p.unlink()
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    zpath = None
    if make_zip:
        zpath = ROOT / "dist" / f"{SLUG}-{m['version']}-chromium.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            z.writestr("manifest.json", manifest)
            for p in files:
                z.write(p, p.relative_to(SRC).as_posix())
    return OUT, zpath, m["version"]


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-zip", action="store_true", help="only refresh dist/chromium/")
    a = ap.parse_args()
    folder, zpath, ver = build(make_zip=not a.no_zip)
    print(f"Chromium build {ver} (extension ID {CHROMIUM_ID})")
    print(f"  folder: {folder}   <- Load unpacked")
    if zpath:
        print(f"  zip:    {zpath}   ({zpath.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()

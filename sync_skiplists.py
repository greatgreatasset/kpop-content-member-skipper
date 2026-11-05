"""Copy skiplists/<profile>/*.json into extension/skiplists/<profile>/ (the
bundled fallback), refresh each index.json, and write
extension/skiplists/profiles.json for the extension's profile selector."""
import json
import re
import shutil

from common import ROOT, SKIPLISTS, Profile, all_profiles, update_index

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def main():
    dst_root = ROOT / "extension" / "skiplists"
    if dst_root.exists():
        shutil.rmtree(dst_root)  # generated folder; rebuilt from skiplists/
    profiles = []
    for pid in all_profiles():
        prof = Profile(pid)
        src, dst = SKIPLISTS / pid, dst_root / pid
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for f in sorted(src.glob("*.json")) if src.exists() else []:
            if not ID_RE.match(f.stem):
                continue
            json.loads(f.read_text(encoding="utf-8"))  # refuse to bundle broken JSON
            shutil.copyfile(f, dst / f.name)
            n += 1
        if src.exists():
            update_index(src, prof)
        update_index(dst, prof)
        profiles.append({**prof.summary(), "bundled": n})
        print(f"{pid}: bundled {n} skip list(s)")
    (dst_root / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1) + "\n",
                                            encoding="utf-8")


if __name__ == "__main__":
    main()

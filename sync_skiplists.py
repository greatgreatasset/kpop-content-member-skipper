"""Copy skiplists/<videoId>.json into extension/skiplists/ (the bundled
fallback), refresh both index.json files, and write
extension/skiplists/profiles.json (the groups, skip and highlight members the
popup and settings page show).

The old per-group folders (skiplists/<profile>/) are left alone for add-on
versions before 0.5.0 and are not bundled."""
import json
import re
import shutil

from common import ROOT, SKIPLISTS, Profile, all_profiles, update_index

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def main():
    dst = ROOT / "extension" / "skiplists"
    if dst.exists():
        shutil.rmtree(dst)  # generated folder; rebuilt from skiplists/
    dst.mkdir(parents=True)
    n = 0
    for f in sorted(SKIPLISTS.glob("*.json")):
        if not ID_RE.match(f.stem):
            continue
        json.loads(f.read_text(encoding="utf-8"))  # refuse to bundle broken JSON
        shutil.copyfile(f, dst / f.name)
        n += 1
    SKIPLISTS.mkdir(exist_ok=True)
    update_index(SKIPLISTS)
    update_index(dst)
    profiles = [Profile(pid).summary() for pid in all_profiles()]
    (dst / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1) + "\n",
                                       encoding="utf-8")
    print(f"bundled {n} skip list(s); groups: {', '.join(p['group'] for p in profiles)}")


if __name__ == "__main__":
    main()

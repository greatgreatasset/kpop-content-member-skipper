"""rules.py (scanner) and extension/rules.js (add-on) must agree for any member
choices, not just the defaults: random Skip / Normal / Highlight choices for
every skip list in the repo, computed by both (the JS side through node).

    python tests/rules_crosscheck.py [--cases 25] [--seed 1]
"""
import argparse
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import rules  # noqa: E402

TOL = 0.1


def close(a, b):
    return len(a) == len(b) and all(abs(x["start"] - y["start"]) <= TOL and abs(x["end"] - y["end"]) <= TOL
                                    for x, y in zip(a, b))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=25)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    fails = total = 0
    for f in sorted((ROOT / "skiplists").glob("*.json")):
        if len(f.stem) != 11:
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        tl = d.get("timeline")
        if not tl or not tl["members"]:
            continue
        keys = [m["key"] for m in tl["members"]]
        cases = [{}]  # defaults
        for k in keys:  # each member alone set to skip, then to highlight
            cases += [{k: "skip"}, {k: "highlight"}]
        cases += [{k: rng.choice(rules.CHOICES) for k in keys} for _ in range(a.cases)]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as t:
            json.dump(cases, t)
        js = json.loads(subprocess.run(["node", str(ROOT / "tests" / "rules_test.js"), "--apply", str(f), t.name],
                                       capture_output=True, text=True, check=True).stdout)
        Path(t.name).unlink()
        bad = 0
        for ch, j in zip(cases, js):
            py = rules.apply(tl, ch)
            ok = close(py["skips"], j["skips"]) and [h["key"] for h in py["highlights"]] == \
                [h["key"] for h in j["highlights"]] and all(close(x["ranges"], y["ranges"])
                                                            for x, y in zip(py["highlights"], j["highlights"]))
            if not ok:
                bad += 1
                if bad <= 2:
                    print(f"  {f.stem} differs for {ch}")
        total += len(cases)
        fails += bad
        print(f"[{'FAIL' if bad else 'PASS'}] {f.stem}: {len(cases) - bad}/{len(cases)} choice sets agree")
    print(f"\n{total - fails}/{total} choice sets: rules.py = rules.js")
    return 1 if fails or not total else 0


if __name__ == "__main__":
    sys.exit(main())

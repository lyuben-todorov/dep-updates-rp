"""RustSec cross-reference over DS1 candidates.

Clones (or reuses) the RustSec advisory DB and reports, for every
candidate in the enriched DS1 file, whether the PR's declared
`dependency_name` + `previous_version` → `new_version` bump crosses
an advisory boundary in either direction:

  * SECURITY_MOTIVATED  — prev affected, new safe, PR date >= advisory
  * COINCIDENTAL_ESCAPE — prev affected, new safe, PR date <  advisory
  * SECURITY_REGRESSION — prev safe,     new affected, PR date >= advisory
                          (dependabot recommended a still-vulnerable version)
  * PRE_ADVISORY_REGR   — prev safe,     new affected, PR date <  advisory
                          (dependabot couldn't have known)
  * NONE                — no advisory crossing

Joins against `drive_state` to report reproducibility outcome per cohort.

Usage:
    python3 scripts/rustsec_crossings.py \
        --db data/pipeline.sqlite \
        --run-id ds1-full-crack \
        --candidates data/rebatchi/ds1_candidates_enriched.jsonl \
        [--rustsec-dir /tmp/rustsec-db] [--clone]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from pathlib import Path


VER_PATTERN = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?")
RANGE_OP = re.compile(r"^(>=|<=|<|>|=|\^|~)\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version(v):
    if not v:
        return None
    m = VER_PATTERN.match(str(v).strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def ver_in_range(vt, rng: str) -> bool:
    if not vt:
        return False
    for part in [p.strip() for p in rng.split(",")]:
        if part == "*":
            continue
        m = RANGE_OP.match(part)
        if not m:
            return False
        op = m.group(1)
        bound = (int(m.group(2)), int(m.group(3) or 0), int(m.group(4) or 0))
        if op == ">=" and vt < bound:
            return False
        if op == ">" and vt <= bound:
            return False
        if op == "<=" and vt > bound:
            return False
        if op == "<" and vt >= bound:
            return False
        if op == "=" and vt != bound:
            return False
    return True


def load_advisories(rustsec_dir: Path) -> dict[str, list[dict]]:
    advisories: dict[str, list[dict]] = defaultdict(list)
    crates_dir = rustsec_dir / "crates"
    for crate_dir in crates_dir.iterdir():
        if not crate_dir.is_dir():
            continue
        pkg = crate_dir.name
        for f in crate_dir.iterdir():
            if not f.name.startswith("RUSTSEC"):
                continue
            txt = f.read_text(errors="replace")
            m = re.search(r"```toml\s*(.*?)\s*```", txt, re.DOTALL)
            if not m:
                continue
            try:
                t = tomllib.loads(m.group(1))
            except Exception:
                continue
            adv = t.get("advisory", {})
            ver = t.get("versions", {})
            advisories[pkg].append({
                "id": adv.get("id"),
                "date": str(adv.get("date", "")),
                "informational": adv.get("informational") or "",
                "patched": ver.get("patched", []),
                "unaffected": ver.get("unaffected", []),
            })
    return advisories


def classify_bump(prev_v, new_v, adv_date: str, pr_date: str, adv: dict) -> str | None:
    """Return one of SECURITY_MOTIVATED / COINCIDENTAL_ESCAPE /
    SECURITY_REGRESSION / PRE_ADVISORY_REGR / None.
    """
    if not (prev_v and new_v):
        return None
    prev_un = any(ver_in_range(prev_v, rg) for rg in adv["unaffected"])
    prev_pat = any(ver_in_range(prev_v, rg) for rg in adv["patched"])
    new_un = any(ver_in_range(new_v, rg) for rg in adv["unaffected"])
    new_pat = any(ver_in_range(new_v, rg) for rg in adv["patched"])

    prev_affected = not (prev_un or prev_pat)
    new_affected = not (new_un or new_pat)

    timing_post_adv = (pr_date >= adv_date) if (pr_date and adv_date) else None

    if prev_affected and not new_affected:
        return "SECURITY_MOTIVATED" if timing_post_adv else "COINCIDENTAL_ESCAPE"
    if not prev_affected and new_affected:
        return "SECURITY_REGRESSION" if timing_post_adv else "PRE_ADVISORY_REGR"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--rustsec-dir", default="/tmp/rustsec-db")
    ap.add_argument("--clone", action="store_true",
                    help="shallow-clone RustSec into --rustsec-dir if missing")
    args = ap.parse_args()

    rustsec_dir = Path(args.rustsec_dir)
    if not rustsec_dir.exists():
        if not args.clone:
            print(f"error: {rustsec_dir} does not exist; pass --clone to fetch", file=sys.stderr)
            return 2
        print(f"cloning RustSec advisory DB -> {rustsec_dir}", file=sys.stderr)
        subprocess.check_call(
            ["git", "clone", "--depth=1", "--quiet",
             "https://github.com/rustsec/advisory-db.git", str(rustsec_dir)]
        )

    advisories = load_advisories(rustsec_dir)
    total_advs = sum(len(v) for v in advisories.values())
    print(f"indexed {len(advisories)} crates, {total_advs} advisories",
          file=sys.stderr)

    # Load run status.
    conn = sqlite3.connect(args.db)
    status_by_key = dict(
        conn.execute(
            "SELECT candidate_key, status FROM drive_state WHERE run_id = ?",
            (args.run_id,),
        ).fetchall()
    )
    print(f"drive_state rows for {args.run_id}: {len(status_by_key)}", file=sys.stderr)

    buckets: dict[str, list[dict]] = defaultdict(list)

    with open(args.candidates) as f:
        for line in f:
            r = json.loads(line)
            dep = r.get("dependency_name", "")
            if dep not in advisories:
                continue
            prev_v = parse_version(r.get("previous_version", ""))
            new_v = parse_version(r.get("new_version", ""))
            if not (prev_v and new_v):
                continue
            pr_date = (r.get("post_commit_date", "") or "")[:10]
            key = f"{r['repo']}#{r['pr_number']}"

            for adv in advisories[dep]:
                label = classify_bump(prev_v, new_v, adv["date"], pr_date, adv)
                if not label:
                    continue
                buckets[label].append({
                    "key": key, "dep": dep,
                    "prev": r.get("previous_version"),
                    "new": r.get("new_version"),
                    "adv": adv["id"], "adv_date": adv["date"],
                    "pr_date": pr_date,
                    "informational": adv["informational"],
                    "status": status_by_key.get(key, "unprocessed"),
                })
                # A candidate only needs to match one advisory for bookkeeping.
                break

    print()
    print("=== Advisory crossings by class ===")
    print(f"{'class':<24s} {'n':>5s}  reproducibility breakdown")
    print("-" * 72)
    for label in ("SECURITY_MOTIVATED", "COINCIDENTAL_ESCAPE",
                  "SECURITY_REGRESSION", "PRE_ADVISORY_REGR"):
        items = buckets.get(label, [])
        if not items:
            print(f"{label:<24s} {0:>5d}")
            continue
        stc = Counter(it["status"] for it in items)
        s = ", ".join(f"{k}={v}" for k, v in stc.most_common())
        print(f"{label:<24s} {len(items):>5d}  {s}")

    # Detail tables.
    for label in ("SECURITY_MOTIVATED", "SECURITY_REGRESSION"):
        items = buckets.get(label, [])
        if not items:
            continue
        print()
        print(f"=== {label} (n={len(items)}) ===")
        for it in sorted(items, key=lambda x: x["adv_date"]):
            flag = "INFO " if it["informational"] else "CVE  "
            prev, new = it["prev"] or "?", it["new"] or "?"
            print(f"  {it['key']:52s} {it['dep']:22s} {prev:>10s} -> {new:<10s}  "
                  f"{it['adv']}  adv={it['adv_date']:<10s} pr={it['pr_date']:<10s}  "
                  f"{flag} [{it['status']}]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

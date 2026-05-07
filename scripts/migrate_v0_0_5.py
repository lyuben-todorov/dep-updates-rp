"""One-shot migration from v0.0.4 entries + v0.1.0 fat-image index to v0.0.5.

Run once. After this:
  - data/cargo/*.json bumped to schemaVersion 0.0.5, with
    reproduction.environmentFingerprint wrapped into a single-entry
    reproduction.environmentFingerprints list tagged linux/arm64 (or
    whatever --platform overrides).
  - docker/cargo-fat/index.json bumped to schemaVersion 0.2.0, with each
    record's scalar environmentFingerprint wrapped into a one-entry
    environmentFingerprints list tagged likewise.

Idempotent: re-running on an already-migrated file is a no-op.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _migrate_entry(path: Path, platform: str) -> bool:
    with path.open() as f:
        entry = json.load(f)

    if entry.get("schemaVersion") == "0.0.5":
        return False

    repro = entry.get("reproduction")
    if repro is None:
        # unreproducible entries have no reproduction block — just bump schema.
        entry["schemaVersion"] = "0.0.5"
        with path.open("w") as f:
            json.dump(entry, f, indent=2)
            f.write("\n")
        return True

    old_fp = repro.pop("environmentFingerprint", None)
    if old_fp is None:
        # No scalar fingerprint and no list? Leave alone.
        return False

    if "environmentFingerprints" not in repro:
        new_fp = {"platform": platform, **{k: v for k, v in old_fp.items() if v is not None}}
        # Preserve the canonical field order: platform, digest, files, rustcVersion, packageCount
        ordered = {"platform": platform, "digest": old_fp["digest"], "files": old_fp["files"]}
        if old_fp.get("rustcVersion") is not None:
            ordered["rustcVersion"] = old_fp["rustcVersion"]
        if old_fp.get("packageCount") is not None:
            ordered["packageCount"] = old_fp["packageCount"]
        repro["environmentFingerprints"] = [ordered]

    entry["schemaVersion"] = "0.0.5"
    with path.open("w") as f:
        json.dump(entry, f, indent=2)
        f.write("\n")
    return True


def _migrate_fat_index(path: Path, platform: str) -> int:
    with path.open() as f:
        idx = json.load(f)

    n = 0
    for rec in idx.get("fatImages", []):
        if "environmentFingerprints" in rec:
            continue
        old_digest = rec.pop("environmentFingerprint", None)
        old_pkg_count = rec.pop("packageCount", None)
        if old_digest is None:
            continue
        fp_entry: dict = {"platform": platform, "digest": old_digest}
        if old_pkg_count is not None:
            fp_entry["packageCount"] = old_pkg_count
        rec["environmentFingerprints"] = [fp_entry]
        n += 1

    idx["schemaVersion"] = "0.2.0"
    with path.open("w") as f:
        json.dump(idx, f, indent=2)
        f.write("\n")
    return n


def main() -> int:
    p = argparse.ArgumentParser(description="v0.0.4 → v0.0.5 per-arch fingerprint migration.")
    p.add_argument("--platform", default="linux/arm64",
                   help="Platform to tag existing fingerprints with. Default: linux/arm64 "
                        "(both committed entries + the ledger were authored on an arm64 host).")
    p.add_argument("--entries-dir", default=str(ROOT / "data" / "cargo"))
    p.add_argument("--fat-index", default=str(ROOT / "docker" / "cargo-fat" / "index.json"))
    args = p.parse_args()

    entries_dir = Path(args.entries_dir)
    fat_index = Path(args.fat_index)

    print(f"platform tag: {args.platform}", file=sys.stderr)

    migrated_entries = 0
    skipped_entries = 0
    for entry_path in sorted(entries_dir.glob("*.json")):
        if _migrate_entry(entry_path, args.platform):
            migrated_entries += 1
            print(f"  migrated {entry_path.name}", file=sys.stderr)
        else:
            skipped_entries += 1

    migrated_images = _migrate_fat_index(fat_index, args.platform)

    print("", file=sys.stderr)
    print(f"entries migrated:  {migrated_entries}", file=sys.stderr)
    print(f"entries skipped:   {skipped_entries}  (already v0.0.5 or no fingerprint)", file=sys.stderr)
    print(f"fat images migrated: {migrated_images}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

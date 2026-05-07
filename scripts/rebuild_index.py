"""Rebuild `data/pipeline.sqlite` from the git-committed canonical layers.

The index is a pure function of:
  - Layer 0: docker/cargo-fat/index.json       → fat_images table
  - Layer 1: data/cargo/*.json                  → entries,
                                                   ingestion_sources,
                                                   classifications (seed)

Idempotent. Safe to rerun. Does not touch reproduction_attempts,
drive_state, or gh_api_cache — those are per-run history that can't be
rebuilt from canonical layers. If you want those gone, delete the DB.

Source tagging: entries whose filenames follow a known producer's
convention get a best-effort `source`. Without provenance metadata in the
JSON, we default to 'unknown'; rebatchi ingestion scripts should upsert
ingestion_sources directly at entry-creation time to get the real tag.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# scripts/ is a sibling of lib/; put lib/ on sys.path.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from bump_ext import PipelineDB, SchemaError, validate_entry  # noqa: E402


def _rebuild_fat_images(db: PipelineDB, index_path: Path) -> int:
    if not index_path.exists():
        print(f"warn: fat-image index not found at {index_path}", file=sys.stderr)
        return 0
    with index_path.open() as f:
        idx = json.load(f)
    n = 0
    for rec in idx.get("fatImages", []):
        db.upsert_fat_image(
            tag=rec["tag"],
            rust_version=rec["rustVersion"],
            debian_release=rec["debianRelease"],
            source_date_epoch=int(rec["sourceDateEpoch"]),
            apt_snapshot=rec["aptSnapshot"],
            environment_fingerprint=rec["environmentFingerprint"],
            package_count=rec.get("packageCount"),
            first_seen_at=rec.get("firstSeenAt"),
            notes=rec.get("notes"),
            status="valid",
        )
        n += 1
    return n


def _rebuild_entries(db: PipelineDB, entries_dir: Path) -> tuple[int, int, int]:
    """Returns (n_entries, n_ingestion, n_classifications_seeded)."""
    n_entries = n_ingestion = n_cls = 0
    for path in sorted(entries_dir.glob("*.json")):
        with path.open() as f:
            entry = json.load(f)
        try:
            validate_entry(entry)
        except SchemaError as e:
            print(f"SKIP: {path.name}: {e}", file=sys.stderr)
            continue

        entry_id = db.upsert_entry_from_json(path)
        n_entries += 1

        # ingestion_sources: best-effort. If already present, keep it; else
        # write a stub with source='unknown'. Ingestion scripts should own
        # the real tag at entry-creation time going forward.
        cur = db.conn.execute(
            "SELECT 1 FROM ingestion_sources WHERE entry_id = ? LIMIT 1", (entry_id,)
        )
        if cur.fetchone() is None:
            db.upsert_ingestion_source(
                entry_id=entry_id,
                source="unknown",
                source_ref=None,
                ingested_by="rebuild_index.py",
            )
            n_ingestion += 1

        # classifications: seed from failure block (is_current=TRUE).
        failure = entry.get("failure")
        if failure:
            inserted = db.seed_classification_if_absent(
                entry_id=entry_id,
                classifier_version="seed-from-json",
                classifier_git_sha="",
                top_category=failure.get("topCategory", "OTHER"),
                sub_category=failure.get("subCategory"),
                error_codes=failure.get("errorCodes") or [],
            )
            if inserted:
                n_cls += 1

    return n_entries, n_ingestion, n_cls


def main() -> int:
    p = argparse.ArgumentParser(description="Rebuild pipeline.sqlite from canonical layers.")
    p.add_argument("--db", default=str(ROOT / "data" / "pipeline.sqlite"),
                   help="Path to SQLite file. Created if absent.")
    p.add_argument("--entries-dir", default=str(ROOT / "data" / "cargo"),
                   help="Directory containing entry JSONs.")
    p.add_argument("--fat-index", default=str(ROOT / "docker" / "cargo-fat" / "index.json"),
                   help="Path to the fat-image ledger.")
    args = p.parse_args()

    db_path = Path(args.db)
    entries_dir = Path(args.entries_dir)
    fat_index = Path(args.fat_index)

    print(f"db: {db_path}", file=sys.stderr)
    print(f"entries: {entries_dir}", file=sys.stderr)
    print(f"fat index: {fat_index}", file=sys.stderr)

    with PipelineDB(db_path) as db:
        n_fat = _rebuild_fat_images(db, fat_index)
        n_e, n_i, n_c = _rebuild_entries(db, entries_dir)

    print("", file=sys.stderr)
    print(f"fat_images:        {n_fat}", file=sys.stderr)
    print(f"entries:           {n_e}", file=sys.stderr)
    print(f"ingestion (new):   {n_i}", file=sys.stderr)
    print(f"classifications:   {n_c}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

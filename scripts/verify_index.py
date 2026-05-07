"""CI drift check: entry JSONs on disk vs the SQLite index.

Walks data/cargo/*.json, recomputes each file's sha256, and compares
against the entries table. Fails loud on any of:

  - JSON on disk with no row in the index
  - Row in the index with no JSON on disk
  - Hash mismatch (JSON edited but index not rebuilt)

Exit 0 = clean, exit 1 = drift. Intended as a pre-commit or CI check.
The DB is rebuildable; this is the canary that catches a stale index
hiding behind edited entries.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from bump_ext import PipelineDB  # noqa: E402


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    p = argparse.ArgumentParser(description="Verify pipeline.sqlite is in sync with entry JSONs.")
    p.add_argument("--db", default=str(ROOT / "data" / "pipeline.sqlite"))
    p.add_argument("--entries-dir", default=str(ROOT / "data" / "cargo"))
    args = p.parse_args()

    db_path = Path(args.db)
    entries_dir = Path(args.entries_dir)

    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path} — run scripts/rebuild_index.py", file=sys.stderr)
        return 1

    on_disk: dict[str, Path] = {}
    for path in sorted(entries_dir.glob("*.json")):
        on_disk[path.stem] = path

    errors: list[str] = []

    with PipelineDB(db_path) as db:
        indexed: dict[str, tuple[str, str]] = {}
        for row in db.iter_entries():
            indexed[row["id"]] = (row["file_path"], row["file_hash"])

    # Entries missing from DB
    for entry_id, path in on_disk.items():
        if entry_id not in indexed:
            errors.append(f"MISSING IN DB: {path} (id={entry_id})")
            continue
        db_path_str, db_hash = indexed[entry_id]
        actual_hash = _sha256_file(path)
        if actual_hash != db_hash:
            errors.append(
                f"HASH MISMATCH: {entry_id}\n"
                f"  file:  {path}\n"
                f"  disk:  {actual_hash}\n"
                f"  index: {db_hash}"
            )

    # Entries in DB but no file on disk
    for entry_id in indexed:
        if entry_id not in on_disk:
            errors.append(f"STALE ROW: {entry_id} in index but no JSON on disk")

    if errors:
        print(f"{len(errors)} drift issue(s):\n", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print("\nRun scripts/rebuild_index.py to refresh the index.", file=sys.stderr)
        return 1

    print(f"ok: {len(on_disk)} entries match index", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

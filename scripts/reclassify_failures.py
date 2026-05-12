"""Post-hoc reclassifier for not_reproducible candidates.

Reads `drive_state` + the on-disk `<short>-pre.log` files produced during
a reproduction run, classifies each failure into a sub-category, and
writes the result into `drive_state_classifications` (additive — never
touches `drive_state` itself).

Category philosophy:

  REPO_GONE            — repository/path evaporated ("Cargo.toml not found"
                         after successful git clone). A corpus-health
                         issue, not a reproducibility failure.
  LOCK_FILE_STALE      — `Cargo.lock` can't resolve under the frozen
                         registry snapshot with --locked.
  OPENSSL_MISMATCH     — openssl-sys / ring build-script fails against
                         the fat image's OpenSSL headers. The canonical
                         environmental-era mismatch.
  NATIVE_DEP_MISSING   — pkg-config reports "package X not found" for
                         non-openssl system deps (fuse, nasm, sgx, etc.).
  RUSTC_BITROT         — a single concrete rustc error code (E####) that
                         compiled under the author's toolchain but not
                         the fat image's. Sub-coded by error number.
  TEST_FAILURE         — pre-commit test failed (author-environment
                         assumption that doesn't hold in our container).
  RUNTIME_CRASH        — SIGSEGV / panic in build-script or tests.
  NETWORK_ERROR        — zlib stream, DNS, git fetch, connection timeouts.
  TIMEOUT              — reproducer hit --timeout (default 1800s).
  OTHER                — classifier fell through. Captures the last
                         error line for manual inspection.

Usage:
    python3 scripts/reclassify_failures.py \
        --db data/pipeline.sqlite \
        --run-id ds1-full-crack \
        --logs-dir data/cargo-logs \
        --candidates data/rebatchi/ds1_candidates_enriched.jsonl

Writes/updates `drive_state_classifications (run_id, candidate_key, category, subcategory, evidence)`.
Prints a summary table to stdout.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
RUSTC_CODE = re.compile(r"error\[(E\d{4})\]")
COULD_NOT_COMPILE = re.compile(r"error: could not compile [`\"]([^`\"]+)[`\"]")
PKG_CONFIG_MISSING = re.compile(r"Package ['`\"]?([a-zA-Z0-9_.+-]+)['`\"]? was not found", re.IGNORECASE)


CATEGORIES = (
    "REPO_GONE",
    "LOCK_FILE_STALE",
    "OPENSSL_MISMATCH",
    "NATIVE_DEP_MISSING",
    "RUSTC_BITROT",
    "TEST_FAILURE",
    "RUNTIME_CRASH",
    "NETWORK_ERROR",
    "TIMEOUT",
    "DEPENDENCY_RESOLUTION",
    "OLD_MESSAGE_FORMAT",
    "NO_LOG",
    "OTHER",
)


def classify(text: str) -> tuple[str, str | None, str]:
    """Return (category, subcategory, evidence_snippet).

    subcategory is optional — e.g. error code for RUSTC_BITROT, crate
    name for TEST_FAILURE, missing package name for NATIVE_DEP_MISSING.
    """
    clean = ANSI.sub("", text)
    low = clean.lower()

    # Timeouts (our reproducer writes a specific marker).
    if "error: reproducer timeout" in low:
        return "TIMEOUT", None, "reproducer timeout marker"

    # Repo gone / missing manifest.
    if "could not find `cargo.toml`" in low:
        return "REPO_GONE", None, "Cargo.toml not found in /src"

    # Lock file stale.
    if "needs to be updated but --locked" in low:
        return "LOCK_FILE_STALE", None, "--locked rejected stale lockfile"

    # OpenSSL — extremely common in DS1.
    if ("unable to detect openssl version" in low
            or "openssl-sys" in low and "build failed" in low
            or "failed to run custom build command for `openssl" in low):
        return "OPENSSL_MISMATCH", None, "openssl-sys/ring build-script failure"

    # Other native dep via pkg-config.
    m = PKG_CONFIG_MISSING.search(clean)
    if m:
        return "NATIVE_DEP_MISSING", m.group(1), f"pkg-config: {m.group(1)} not found"

    # Dependency resolution (non-lockfile).
    if ("error: failed to select a version" in low
            or "error: no matching package named" in low
            or "failed to get `" in low):
        return "DEPENDENCY_RESOLUTION", None, "cargo resolver rejected"

    # Runtime crashes during build (build.rs panics, tests crashing pre-flight).
    if "sigsegv" in low or "signal: 11" in low:
        return "RUNTIME_CRASH", "SIGSEGV", "SIGSEGV"
    if "panicked at" in low and ("build-script" in low or "build.rs" in low or "custom build command" in low):
        return "RUNTIME_CRASH", "BUILD_SCRIPT_PANIC", "build-script panic"

    # rustc error code — the bitrot workhorse.
    m = RUSTC_CODE.search(clean)
    if m:
        return "RUSTC_BITROT", m.group(1), f"error[{m.group(1)}]"

    # Test failures (pre-commit test expected to pass, didn't).
    if "test result: failed" in low or "error: test failed" in low:
        return "TEST_FAILURE", None, "cargo test exit != 0"

    # Network.
    if "error reading from the zlib stream" in low or "connection timed out" in low or "failed to fetch" in low:
        return "NETWORK_ERROR", None, "network fetch failure"

    # Old cargo rejecting our message-format flag (pipeline-era issue, mostly fixed).
    if "json-diagnostic-rendered-ansi" in low and "is not a valid value" in low:
        return "OLD_MESSAGE_FORMAT", None, "cargo too old for our flag"

    # Fallback — grab a "could not compile" crate or the last error line.
    m = COULD_NOT_COMPILE.search(clean)
    if m:
        return "RUSTC_BITROT", None, f"could not compile {m.group(1)}"

    errs = [ln.strip() for ln in clean.splitlines() if ln.strip().lower().startswith("error")]
    return "OTHER", None, errs[-1][:120] if errs else "(no error lines)"


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS drive_state_classifications (
             run_id        TEXT NOT NULL,
             candidate_key TEXT NOT NULL,
             category      TEXT NOT NULL,
             subcategory   TEXT,
             evidence      TEXT,
             classified_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
             PRIMARY KEY (run_id, candidate_key)
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dsc_category ON drive_state_classifications(category)"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--logs-dir", required=True)
    ap.add_argument("--candidates", required=True,
                    help="JSONL file with enriched candidates; needed to resolve post_commit "
                         "short hashes that prefix the log file names.")
    ap.add_argument("--only", default="not_reproducible",
                    help="drive_state status to reclassify (default: not_reproducible). "
                         "Pass 'all' to classify every row.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logs_dir = Path(args.logs_dir)

    # Map candidate_key -> post_commit[:8] for log lookup.
    key_to_short: dict[str, str] = {}
    with open(args.candidates) as f:
        for line in f:
            c = json.loads(line)
            k = f"{c['repo']}#{c['pr_number']}"
            key_to_short[k] = c["post_commit"][:8]

    conn = sqlite3.connect(args.db)
    ensure_table(conn)

    if args.only == "all":
        rows = conn.execute(
            "SELECT candidate_key, status, reason FROM drive_state WHERE run_id = ?",
            (args.run_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT candidate_key, status, reason FROM drive_state "
            "WHERE run_id = ? AND status = ?",
            (args.run_id, args.only),
        ).fetchall()

    print(f"reclassifying {len(rows)} row(s) from run={args.run_id} status={args.only}",
          file=sys.stderr)

    counts: collections.Counter[str] = collections.Counter()
    subcounts: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    examples: dict[str, list[str]] = collections.defaultdict(list)
    to_write: list[tuple[str, str, str, str | None, str]] = []

    for candidate_key, status, reason in rows:
        # Timeouts already identifiable without log-reading.
        if reason and "pre_build_timed_out" in reason:
            cat, sub, ev = "TIMEOUT", None, reason
        else:
            short = key_to_short.get(candidate_key)
            if not short:
                cat, sub, ev = "OTHER", None, "post_commit not in candidates file"
            else:
                pre_log = logs_dir / f"{short}-pre.log"
                if not pre_log.exists():
                    cat, sub, ev = "NO_LOG", None, f"no {pre_log.name}"
                else:
                    text = pre_log.read_text(errors="replace")
                    cat, sub, ev = classify(text)

        counts[cat] += 1
        if sub:
            subcounts[cat][sub] += 1
        if len(examples[cat]) < 3:
            examples[cat].append(candidate_key)
        to_write.append((args.run_id, candidate_key, cat, sub, ev))

    if not args.dry_run:
        conn.executemany(
            """INSERT INTO drive_state_classifications
                 (run_id, candidate_key, category, subcategory, evidence)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(run_id, candidate_key) DO UPDATE SET
                 category=excluded.category,
                 subcategory=excluded.subcategory,
                 evidence=excluded.evidence,
                 classified_at=CURRENT_TIMESTAMP""",
            to_write,
        )
        conn.commit()

    # Summary.
    total = sum(counts.values())
    print()
    print(f"{'category':<25s} {'count':>7s}  {'share':>8s}  example")
    print("-" * 80)
    for cat, n in counts.most_common():
        share = n / total * 100 if total else 0.0
        ex = examples[cat][0] if examples[cat] else ""
        print(f"{cat:<25s} {n:>7d}  {share:>7.1f}%  {ex}")
    print()

    # Subcategory breakdown where useful.
    for cat in ("RUSTC_BITROT", "NATIVE_DEP_MISSING", "RUNTIME_CRASH"):
        if cat in subcounts and subcounts[cat]:
            print(f"{cat} subcategory breakdown:")
            for sub, n in subcounts[cat].most_common(15):
                print(f"  {sub:<15s} {n:>5d}")
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""End-to-end driver — candidates JSONL → v0.0.4 entries + verification records.

Wires together the pipeline stages under a single entry point:

  1. For each candidate:
     a. Read `rust_msrv` + `post_commit_date` from the candidate
        (populated at ingestion by cargo_miner / rebatchi_to_candidate).
        Falls back to GitHub API for missing values.
     b. Enforce the run's `max_sde_date` — candidates whose commit date
        is past that are parked with `commit_after_max_sde_date`.
     c. Bucketize via fat_image.bucket_for; compute canonical SDE and
        tag. On tag-not-in-index, either build (if `--build-missing-bases`)
        or park with `fat_image_missing`.
     d. Reproduce via cargo_reproducer. Discover the PR's category from
        exit codes (pre pass + post fail → breaking; pre pass + post
        pass → non-breaking; pre fail → unreproducible).
     e. Classify the post log (breaking only).
     f. Assemble a v0.0.4 entry (fingerprint extracted from the fat
        image) and write it under --out-dir.
     g. Optionally regenerate-verify via cargo_regenerate.

  2. Append a per-candidate record to --state. Resumable: candidates with
     a terminal record in --state are skipped on subsequent runs.

Non-goals for this driver:

- Parallelism. --parallel > 1 logs a warning and serialises anyway. Docker
  builds serialise on the daemon; premature parallelism invites churn.
- Fat-image clustering. Each candidate's canonical tag is deterministic
  (see fat_image.canonical_sde_for); buckets that share a tag via
  rust_base_pub coincidence are the only form of cross-bucket sharing.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
from bump_ext import SchemaError, validate_entry  # noqa: E402

from . import cargo_assemble_entry as _assemble
from . import cargo_classifier as _classifier
from . import cargo_regenerate as _regenerate
from . import cargo_reproducer as _reproducer
from . import cargo_toolchain as _toolchain
from . import fat_image as _fat


MSRV_FLOOR = "1.56"  # edition-2021; any real Cargo project needs at least this.


# ---- per-candidate status ---------------------------------------------------

class Status:
    OK = "ok"
    FAT_IMAGE_MISSING = "fat_image_missing"
    FAT_IMAGE_BUILD_FAILED = "fat_image_build_failed"
    NOT_REPRODUCIBLE = "not_reproducible"
    ASSEMBLE_FAILED = "assemble_failed"
    REGENERATE_MISMATCH = "regenerate_mismatch"
    METADATA_FETCH_FAILED = "metadata_fetch_failed"
    COMMIT_AFTER_MAX_SDE_DATE = "commit_after_max_sde_date"

    TERMINAL_SUCCESS = {OK}
    TERMINAL_FAILURE = {
        FAT_IMAGE_MISSING, FAT_IMAGE_BUILD_FAILED, NOT_REPRODUCIBLE,
        ASSEMBLE_FAILED, REGENERATE_MISMATCH, METADATA_FETCH_FAILED,
        COMMIT_AFTER_MAX_SDE_DATE,
    }


@dataclass
class DriveRecord:
    candidate_key: str     # "<repo>#<pr_number>"
    status: str
    entry_path: str | None = None
    fat_image_tag: str | None = None
    rust_msrv: str | None = None
    commit_date: str | None = None
    max_sde_date: str | None = None       # run parameter, recorded for traceability
    reason: str | None = None
    timestamp: str = ""


def _key(candidate: dict) -> str:
    return f"{candidate['repo']}#{candidate['pr_number']}"


# ---- state file -------------------------------------------------------------

def load_state(path: Path) -> dict[str, DriveRecord]:
    if not path.exists():
        return {}
    state: dict[str, DriveRecord] = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            state[obj["candidate_key"]] = DriveRecord(**obj)
    return state


def append_state(path: Path, record: DriveRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(dataclasses.asdict(record)) + "\n")


# ---- candidate metadata (MSRV + commit date) --------------------------------
# The candidate-generation step (cargo_miner.py, rebatchi_to_candidate.py)
# enriches each candidate with `rust_msrv` + `post_commit_date` when
# possible. The driver reads those fields directly and falls back to a
# single GitHub API call per missing field.

def _resolve_metadata(candidate: dict) -> tuple[str, dt.date | None]:
    """Return (rust_msrv, commit_date). Uses candidate fields if present,
    falls back to GitHub API for whichever is missing."""
    msrv = candidate.get("rust_msrv")
    if msrv is None:
        msrv = _toolchain.msrv_at_commit(candidate["repo"], candidate["post_commit"])
    if msrv is None:
        msrv = MSRV_FLOOR

    date_str = candidate.get("post_commit_date")
    if date_str is None:
        date_str = _toolchain.commit_date_at(candidate["repo"], candidate["post_commit"])
    commit_date = None
    if date_str:
        try:
            commit_date = dt.date.fromisoformat(date_str)
        except ValueError:
            commit_date = None

    return msrv, commit_date


# ---- per-candidate flow -----------------------------------------------------

def process(candidate: dict, *, out_dir: Path, logs_dir: Path,
            build_missing_bases: bool, regenerate_verify: bool,
            timeout_s: int, host_label: str | None,
            max_sde_date: dt.date) -> DriveRecord:
    key = _key(candidate)
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    rec = DriveRecord(candidate_key=key, status="", timestamp=now,
                      max_sde_date=max_sde_date.isoformat())

    # --- 1a/1b: MSRV + commit date (candidate fields first, GH fallback) ---
    rust_msrv, commit_date = _resolve_metadata(candidate)
    if commit_date is None:
        rec.status = Status.METADATA_FETCH_FAILED
        rec.reason = "could not fetch commit date via GitHub API"
        return rec
    rec.rust_msrv = rust_msrv
    rec.commit_date = commit_date.isoformat()

    # --- 1b': enforce max_sde_date. Candidates with commit_date later than
    # the run's max_sde_date are rejected — the run is configured to stop
    # at this date, and bucketizing them would require future apt snapshots.
    if commit_date > max_sde_date:
        rec.status = Status.COMMIT_AFTER_MAX_SDE_DATE
        rec.reason = (f"commit_date={commit_date} > max_sde_date={max_sde_date}; "
                      f"raise --max-sde-date to include this candidate")
        return rec

    print(f"[{key}]   MSRV={rust_msrv}  commit_date={commit_date}", file=sys.stderr)

    # --- 1c: resolve fat image via canonical bucketing ---
    debian = _toolchain.debian_release_for(commit_date)
    bucket = _fat.bucket_for(rust_msrv, commit_date, debian)
    if bucket is None:
        rec.status = Status.FAT_IMAGE_MISSING
        rec.reason = (f"no supported fat image for (msrv={rust_msrv!r}, "
                      f"debian={debian}) — see fat_image.MILESTONE_DEBIAN_SUPPORTED")
        return rec
    sde_info = _fat.canonical_sde_for(bucket, max_sde_date=max_sde_date)
    tag = _fat.tag_for(bucket, sde_info.sde)

    existing = {r.tag: r for r in _fat.load_index()}
    if tag in existing:
        resolve_match = existing[tag]
        print(f"[{key}] resolved fat image: {tag}", file=sys.stderr)
    else:
        if not build_missing_bases:
            rec.status = Status.FAT_IMAGE_MISSING
            rec.reason = f"fat image not present: {tag}"
            return rec
        print(f"[{key}] building {tag}...", file=sys.stderr)
        try:
            _fat.build_fat_image(bucket.rust_patch(), sde_info.sde,
                                 debian_release=bucket.debian)
            record = _fat.introspect_fat_image(tag)
            record = dataclasses.replace(
                record, firstSeenAt=dt.date.today().isoformat(),
                notes=f"built for {key}",
            )
            _fat.register(record)
            print(f"[{key}] registered {tag}", file=sys.stderr)
            resolve_match = record
        except (_fat.IndexError, ValueError) as e:
            rec.status = Status.FAT_IMAGE_BUILD_FAILED
            rec.reason = str(e)
            return rec

    rec.fat_image_tag = resolve_match.tag

    # --- 1d: reproduce ---
    print(f"[{key}] reproducing...", file=sys.stderr)
    repro = _reproducer.reproduce(
        candidate, logs_dir, resolve_match.tag, timeout_s, resolve_match.tag,
    )

    # Decide category from raw pass/fail facts. We don't get the category
    # from the candidate; we *discover* it here.
    #   pre fail         → unreproducible (pre_build_failed)
    #   pre pass, post fail → breaking
    #   pre pass, post pass → non-breaking
    # fix-after-update requires a third commit we don't have at ingestion.
    if not repro.pre_passed:
        rec.status = Status.NOT_REPRODUCIBLE
        rec.reason = f"pre_build_failed (pre_rc={repro.pre_exit_code}, post_rc={repro.post_exit_code})"
        return rec

    if not repro.post_passed:
        discovered_category = "breaking"
    else:
        discovered_category = "non-breaking"

    # --- 1e: classify (only for breaking — failure metadata requires a
    # failing log to parse) ---
    classification_dict: dict | None = None
    if discovered_category == "breaking":
        print(f"[{key}] classifying...", file=sys.stderr)
        post_log_text = Path(repro.post_log_path).read_text(errors="replace")
        classification = _classifier.classify(post_log_text)
        classification_dict = classification.__dict__

    # --- 1f: assemble ---
    print(f"[{key}] assembling entry (category={discovered_category})...", file=sys.stderr)
    try:
        entry = _assemble.build_entry(
            candidate,
            dataclasses.asdict(repro),
            classification_dict,
            category=discovered_category,
            fat_image_tag=resolve_match.tag,
            source_date_epoch=resolve_match.sourceDateEpoch,
            build_flags=["--locked", "--offline"],
            record_fat_digest=False,
        )
    except _assemble.AssembleError as e:
        rec.status = Status.ASSEMBLE_FAILED
        rec.reason = str(e)
        return rec

    # Write entry.
    from bump_ext import EntryWriter  # lazy import to stay under the sys.path hack
    out = EntryWriter(out_dir).write(entry)
    rec.entry_path = str(out)
    print(f"[{key}] wrote {out}", file=sys.stderr)

    # --- 1g: regenerate-verify ---
    if regenerate_verify:
        print(f"[{key}] regenerate-verify...", file=sys.stderr)
        rc = _regenerate.regenerate(
            Path(out), build_missing_bases=False, skip_tests=False,
            host_label=host_label, timeout_s=timeout_s, builder="desktop-linux",
        )
        if rc != _regenerate.EXIT_OK:
            rec.status = Status.REGENERATE_MISMATCH
            rec.reason = f"regenerate exit={rc}"
            return rec

    rec.status = Status.OK
    return rec


# ---- main -------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="End-to-end Cargo pipeline driver.")
    p.add_argument("--candidates", required=True, help="JSONL of candidates (cargo_miner.py format).")
    p.add_argument("--out-dir", default="data/cargo", help="Where to write entry JSONs.")
    p.add_argument("--logs-dir", default="data/cargo/logs", help="Where reproducer logs land.")
    p.add_argument("--state", default="data/cargo/drive-state.jsonl",
                   help="State file for resumability.")
    p.add_argument("--build-missing-bases", action="store_true",
                   help="Auto-build a fat image when no existing one covers a candidate.")
    p.add_argument("--regenerate-verify", action="store_true",
                   help="After assembling, run cargo_regenerate.py on the entry as a sanity check.")
    p.add_argument("--limit", type=int, default=None, help="Stop after N candidates.")
    p.add_argument("--parallel", type=int, default=1,
                   help="Accepted for forward-compat; >1 logs a warning and serialises.")
    p.add_argument("--timeout", type=int, default=1800, help="Per-stage timeout (s).")
    p.add_argument("--host", default=None, help="Host label recorded in regenerate-verify records.")
    p.add_argument("--max-sde-date", type=lambda s: dt.date.fromisoformat(s),
                   default=None,
                   help="Upper bound on acceptable commit dates (YYYY-MM-DD). "
                        "Candidates with later commits are recorded as "
                        "commit_after_max_sde_date. Default: Dec 31 of last year. "
                        "This is a run-level parameter — keep stable across a batch "
                        "so tags are deterministic.")
    args = p.parse_args()

    max_sde_date = args.max_sde_date or _fat.default_max_sde_date()
    print(f"max_sde_date (run parameter): {max_sde_date}", file=sys.stderr)

    if args.parallel > 1:
        print(f"WARN: --parallel={args.parallel} not supported yet, running serially.", file=sys.stderr)

    out_dir = Path(args.out_dir)
    logs_dir = Path(args.logs_dir)
    state_path = Path(args.state)
    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    existing = load_state(state_path)
    terminal = Status.TERMINAL_SUCCESS | Status.TERMINAL_FAILURE

    counts = {"skipped": 0, "processed": 0}
    status_counts: dict[str, int] = {}

    with Path(args.candidates).open() as f:
        n = 0
        for line in f:
            line = line.strip()
            if not line:
                continue
            candidate = json.loads(line)
            key = _key(candidate)

            if key in existing and existing[key].status in terminal:
                counts["skipped"] += 1
                continue

            rec = process(
                candidate,
                out_dir=out_dir, logs_dir=logs_dir,
                build_missing_bases=args.build_missing_bases,
                regenerate_verify=args.regenerate_verify,
                timeout_s=args.timeout,
                host_label=args.host,
                max_sde_date=max_sde_date,
            )
            append_state(state_path, rec)
            counts["processed"] += 1
            status_counts[rec.status] = status_counts.get(rec.status, 0) + 1
            print(f"[{key}] → {rec.status}" + (f"  ({rec.reason})" if rec.reason else ""), file=sys.stderr)

            n += 1
            if args.limit and n >= args.limit:
                break

    print("", file=sys.stderr)
    print(f"summary: skipped={counts['skipped']}  processed={counts['processed']}", file=sys.stderr)
    for status, c in sorted(status_counts.items()):
        print(f"  {status}: {c}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

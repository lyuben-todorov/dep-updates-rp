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
import platform as host_platform
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
from bump_ext import PipelineDB, SchemaError, validate_entry  # noqa: E402

from . import cargo_assemble_entry as _assemble
from . import cargo_classifier as _classifier
from . import cargo_regenerate as _regenerate
from . import cargo_reproducer as _reproducer
from . import cargo_toolchain as _toolchain
from . import fat_image as _fat


CLASSIFIER_VERSION = "cargo_classifier@v0.1"


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


def _docker_platform() -> str:
    mach = host_platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(mach, mach)
    sys_name = host_platform.system().lower()
    return f"{sys_name}/{arch}"


def _git_sha() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[2]),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _mirror_drive_state(db: PipelineDB, run_id: str, rec: DriveRecord) -> None:
    db.upsert_drive_state(
        run_id=run_id,
        candidate_key=rec.candidate_key,
        status=rec.status,
        entry_path=rec.entry_path,
        fat_image_tag=rec.fat_image_tag,
        rust_msrv=rec.rust_msrv,
        commit_date=rec.commit_date,
        reason=rec.reason,
        updated_at=rec.timestamp or None,
    )


# ---- candidate metadata (MSRV + commit date) --------------------------------
# The candidate-generation step (cargo_miner.py, rebatchi_to_candidate.py)
# enriches each candidate with `rust_msrv` + `post_commit_date` when
# possible. The driver reads those fields directly and falls back to a
# single GitHub API call per missing field.

def _resolve_metadata(candidate: dict) -> tuple[str, bool, dt.date | None]:
    """Return (rust_msrv, msrv_detected, commit_date).

    `msrv_detected` is False when we fell back to MSRV_FLOOR because the
    project declared nothing at the post-commit. True means rust-toolchain /
    Cargo.toml's rust-version supplied the value.
    """
    msrv = candidate.get("rust_msrv")
    if msrv is None:
        msrv = _toolchain.msrv_at_commit(candidate["repo"], candidate["post_commit"])
    detected = msrv is not None
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

    return msrv, detected, commit_date


# ---- per-candidate flow -----------------------------------------------------

def process(candidate: dict, *, out_dir: Path, logs_dir: Path,
            build_missing_bases: bool, regenerate_verify: bool,
            timeout_s: int, host_label: str | None,
            max_sde_date: dt.date,
            db: PipelineDB | None = None,
            run_id: str | None = None) -> DriveRecord:
    key = _key(candidate)
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    rec = DriveRecord(candidate_key=key, status="", timestamp=now,
                      max_sde_date=max_sde_date.isoformat())

    # --- 1a/1b: MSRV + commit date (candidate fields first, GH fallback) ---
    rust_msrv, msrv_detected, commit_date = _resolve_metadata(candidate)
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
            ecosystem_metadata={
                "rustMsrv": rust_msrv,
                "rustMsrvDetected": msrv_detected,
            },
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

    # ---- DB mirror (optional) ----
    # Entry is on disk → index it. Record the reproduction attempt under
    # this run. Seed the classification row if we classified. Keep this
    # additive; JSONL stays primary.
    if db is not None:
        entry_id = db.upsert_entry_from_json(Path(out))
        db.patch_entry_metadata(
            entry_id,
            post_commit_date=rec.commit_date,
            rust_msrv=rec.rust_msrv,
            msrv_detected=msrv_detected,
        )
        db.upsert_ingestion_source(
            entry_id=entry_id,
            source=candidate.get("source") or "unknown",
            source_ref=key,
            ingested_by="cargo_drive.py",
        )
        db.record_attempt(
            entry_id=entry_id,
            run_id=run_id,
            host_id=host_label or socket.gethostname(),
            host_os=host_platform.system().lower(),
            host_arch=_docker_platform(),
            started_at=now,
            finished_at=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            fat_image_tag_used=resolve_match.tag,
            fingerprint_expected=resolve_match.environmentFingerprint,
            fingerprint_actual=resolve_match.environmentFingerprint,
            fingerprint_matched=True,
            pre_exit_code=repro.pre_exit_code,
            post_exit_code=repro.post_exit_code,
            fix_exit_code=repro.fix_exit_code,
            outcome_matched=repro.matches_category(discovered_category),
            pre_log_path=repro.pre_log_path,
            post_log_path=repro.post_log_path,
            fix_log_path=repro.fix_log_path,
        )
        if classification_dict is not None:
            db.seed_classification_if_absent(
                entry_id=entry_id,
                classifier_version=CLASSIFIER_VERSION,
                classifier_git_sha="",
                top_category=classification_dict.get("topCategory") or "OTHER",
                sub_category=classification_dict.get("subCategory"),
                error_codes=classification_dict.get("errorCodes") or [],
            )

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
    p.add_argument("--out-dir", default="data/cargo", help="Where to write entry JSONs. "
                   "Note: data/cargo/ is a submodule (dep-updates-rp-data); writes land there.")
    p.add_argument("--logs-dir", default="data/cargo-logs", help="Where reproducer logs land. "
                   "Kept outside the submodule — logs are transient dev artefacts, not Layer 1.")
    p.add_argument("--state", default="data/cargo-logs/drive-state.jsonl",
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
    p.add_argument("--db", default=None,
                   help="Optional path to pipeline.sqlite. When set, mirrors "
                        "drive_state + reproduction_attempts + classifications + "
                        "entries alongside the JSONL. JSONL stays primary.")
    p.add_argument("--run-id", default=None,
                   help="Run identifier written to the DB. Defaults to "
                        "'drive-<host>-<ISO timestamp>'. Ignored without --db.")
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

    db: PipelineDB | None = None
    run_id: str | None = None
    if args.db:
        db = PipelineDB(Path(args.db))
        host = args.host or socket.gethostname()
        run_id = args.run_id or f"drive-{host}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        db.start_run(
            run_id=run_id,
            host=host,
            git_sha=_git_sha(),
            candidates_source=str(Path(args.candidates).resolve()),
            max_sde_date=max_sde_date,
            python_version=host_platform.python_version(),
        )
        print(f"db: {args.db}  run_id={run_id}", file=sys.stderr)

    try:
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
                    db=db,
                    run_id=run_id,
                )
                append_state(state_path, rec)
                if db is not None and run_id is not None:
                    _mirror_drive_state(db, run_id, rec)
                counts["processed"] += 1
                status_counts[rec.status] = status_counts.get(rec.status, 0) + 1
                print(f"[{key}] → {rec.status}" + (f"  ({rec.reason})" if rec.reason else ""), file=sys.stderr)

                n += 1
                if args.limit and n >= args.limit:
                    break
    finally:
        if db is not None and run_id is not None:
            db.finish_run(run_id)
            db.close()

    print("", file=sys.stderr)
    print(f"summary: skipped={counts['skipped']}  processed={counts['processed']}", file=sys.stderr)
    for status, c in sorted(status_counts.items()):
        print(f"  {status}: {c}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
import signal
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
from bump_ext import PipelineDB, SchemaError, validate_entry  # noqa: E402

from . import cargo_assemble_entry as _assemble
from . import cargo_classifier as _classifier
from . import cargo_failure_classifier as _failure_classifier
from . import cargo_regenerate as _regenerate
from . import cargo_reproducer as _reproducer
from . import cargo_toolchain as _toolchain
from . import fat_image as _fat


CLASSIFIER_VERSION = "cargo_classifier@v0.1"


MSRV_FLOOR = "1.56"  # edition-2021; any real Cargo project needs at least this.
# Date-aware MSRV floor for projects that declare no MSRV. A 2018-2019 project
# falling back to 1.56 is sent 11+ minor versions ahead of its native toolchain,
# triggering borrow-check tightening and stdlib regressions the author never saw.
# Route pre-2020 undeclared-MSRV projects to 1.39 (the async/await cliff) instead.
MSRV_FLOOR_CUTOVERS = [
    (dt.date(2020, 1, 1), "1.39"),
    (dt.date(9999, 12, 31), "1.56"),
]


def _msrv_floor_for(commit_date: dt.date | None) -> str:
    """Pick the MSRV floor for a project with no declared MSRV, based on the
    post-commit date. Newer commits get a higher floor; pre-2020 commits get
    an era-appropriate floor (1.39 + stretch) rather than the 1.56 default.
    """
    if commit_date is None:
        return MSRV_FLOOR
    for cutover, floor in MSRV_FLOOR_CUTOVERS:
        if commit_date < cutover:
            return floor
    return MSRV_FLOOR


# ---- per-candidate status ---------------------------------------------------

class Status:
    OK = "ok"
    # Reproduction succeeded only after `cargo generate-lockfile && cargo test
    # --frozen` (i.e. the original Cargo.lock was rejected with --locked).
    # Distinct from OK so the headline reproducibility number can split
    # "reproducible under the strict contract" from "reproducible after
    # lockfile regeneration". Operator opts in via --relax-locked.
    OK_AFTER_RELOCK = "ok_after_relock"
    # Multi-attempt mode: per-stage attempts disagreed (some passed, some
    # failed). One status covers both directions — there's no honest way
    # to call a flaky candidate "ok" or "not reproducible", and a single
    # FLAKY label keeps the headline split clean: ok / flaky / not_reproducible.
    FLAKY = "flaky"
    FAT_IMAGE_MISSING = "fat_image_missing"
    FAT_IMAGE_BUILD_FAILED = "fat_image_build_failed"
    NOT_REPRODUCIBLE = "not_reproducible"
    ASSEMBLE_FAILED = "assemble_failed"
    REGENERATE_MISMATCH = "regenerate_mismatch"
    METADATA_FETCH_FAILED = "metadata_fetch_failed"
    COMMIT_AFTER_MAX_SDE_DATE = "commit_after_max_sde_date"
    # Existing entry's fatImage disagrees with the current bucketer's answer.
    # We refuse to silently overwrite — operator passes --reassemble-stale to
    # opt into the old-entry-discard-and-reassemble path.
    ENTRY_BUCKET_STALE = "entry_bucket_stale"

    TERMINAL_SUCCESS = {OK, OK_AFTER_RELOCK}
    TERMINAL_FLAKY = {FLAKY}
    TERMINAL_FAILURE = {
        FAT_IMAGE_MISSING, FAT_IMAGE_BUILD_FAILED, NOT_REPRODUCIBLE,
        ASSEMBLE_FAILED, REGENERATE_MISMATCH, METADATA_FETCH_FAILED,
        COMMIT_AFTER_MAX_SDE_DATE, ENTRY_BUCKET_STALE,
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
    # Scheme-2 classification, populated inline by `process()` when status
    # is NOT_REPRODUCIBLE. Mirrored to drive_state_classifications.
    failure_category: str | None = None
    failure_subcategory: str | None = None
    failure_evidence: str | None = None
    # Full distribution of rustc E-codes seen in the pre-log (canonical
    # source: cargo's JSON compiler-message stream). For RUSTC_BITROT
    # candidates this captures the 17×E0308 + 10×E0277 -style multi-code
    # picture that subcategory (one code) flattens. Empty dict when no
    # rustc errors are present (most non-RUSTC failure modes).
    failure_error_code_counts: dict | None = None
    # Multi-attempt flakiness annotations. Set when attempts > 1 and the
    # repeated cargo-test invocations disagreed.
    flaky_pre: bool = False
    flaky_post: bool = False


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


def _ledger_digest_for_this_host(record) -> str | None:
    """Look up the fat-image fingerprint in the ledger matching this host's
    container platform. None if the ledger record has no fingerprint for
    this arch (e.g. fresh VM, only this-arch-now-building)."""
    container_platform = _regenerate.detect_container_platform(record.tag)
    fp = record.fingerprint_for(container_platform)
    return fp.digest if fp else None


def _record_repro_attempts(db: PipelineDB, run_id: str, *,
                           candidate_key: str, host_label: str | None,
                           started_at: str, fat_image_tag: str | None,
                           fingerprint: str | None, repro,
                           outcome_matched: bool | None = None,
                           entry_id: str | None = None) -> None:
    """Write one row per attempt to reproduction_attempts. Bug E pre-fix
    only the success path called record_attempt; this helper folds in
    failure / regenerate-mismatch / multi-attempt paths so the table
    reflects every cargo-test invocation. attempt_number defaults to 1
    for the single-attempt case; multi-attempt callers iterate.
    """
    if db is None:
        return
    finished_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    base = dict(
        host_id=host_label or socket.gethostname(),
        run_id=run_id,
        candidate_key=candidate_key,
        host_os=host_platform.system().lower(),
        host_arch=_docker_platform(),
        started_at=started_at,
        finished_at=finished_at,
        fat_image_tag_used=fat_image_tag,
        fingerprint_expected=fingerprint,
        fingerprint_actual=fingerprint,
        fingerprint_matched=fingerprint is not None,
        outcome_matched=outcome_matched,
        entry_id=entry_id,
    )
    # Per-attempt rows: when the reproducer ran a single attempt we still
    # write attempt_number=1 so the table is queryable uniformly.
    attempt_pre = list(getattr(repro, "pre_exit_codes", None) or [repro.pre_exit_code])
    attempt_post = list(getattr(repro, "post_exit_codes", None) or [repro.post_exit_code])
    attempt_fix = list(getattr(repro, "fix_exit_codes", None) or
                       ([repro.fix_exit_code] if repro.fix_exit_code is not None else []))
    pre_logs = list(getattr(repro, "pre_log_paths", None) or [repro.pre_log_path])
    post_logs = list(getattr(repro, "post_log_paths", None) or [repro.post_log_path])
    fix_logs = list(getattr(repro, "fix_log_paths", None) or
                    ([repro.fix_log_path] if repro.fix_log_path else []))
    n = max(len(attempt_pre), len(attempt_post))
    for i in range(n):
        db.record_attempt(
            attempt_number=i + 1,
            pre_exit_code=attempt_pre[i] if i < len(attempt_pre) else None,
            post_exit_code=attempt_post[i] if i < len(attempt_post) else None,
            fix_exit_code=attempt_fix[i] if i < len(attempt_fix) else None,
            pre_log_path=pre_logs[i] if i < len(pre_logs) else None,
            post_log_path=post_logs[i] if i < len(post_logs) else None,
            fix_log_path=fix_logs[i] if i < len(fix_logs) else None,
            **base,
        )


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
    # Scheme-2 classification mirror: only populated for not_reproducible
    # records (process() leaves it None for ok/breaking/non-breaking).
    if rec.failure_category is not None:
        db.upsert_drive_state_classification(
            run_id=run_id,
            candidate_key=rec.candidate_key,
            category=rec.failure_category,
            subcategory=rec.failure_subcategory,
            evidence=rec.failure_evidence,
            error_code_counts=rec.failure_error_code_counts,
            classified_at=rec.timestamp or None,
        )


# ---- candidate metadata (MSRV + commit date) --------------------------------
# The candidate-generation step (cargo_miner.py, rebatchi_to_candidate.py)
# enriches each candidate with `rust_msrv` + `post_commit_date` when
# possible. The driver reads those fields directly and falls back to a
# single GitHub API call per missing field.

def _fat_image_present_locally(tag: str) -> bool:
    """True iff `docker image inspect <tag>` returns a local image. Does NOT
    trigger a registry pull."""
    r = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def _preflight_fat_images(
    candidates: list[dict], max_sde_date: dt.date,
    force_fat_image: str | None = None,
) -> tuple[dict[str, int], list[str], list[str]]:
    """Walk candidates, compute each one's canonical fat-image tag, and
    check whether each tag is locally present as a Docker image.

    Returns:
        needed        — {tag: candidate_count}
        missing       — tags not present locally
        build_cmds    — exact `fat_image build` commands for missing tags
    """
    if force_fat_image is not None:
        # Single-image preflight: every candidate routes to the forced tag.
        count = sum(
            1 for c in candidates
            if _resolve_metadata(c)[2] is not None
            and _resolve_metadata(c)[2] <= max_sde_date
        )
        missing = [] if _fat_image_present_locally(force_fat_image) else [force_fat_image]
        return ({force_fat_image: count}, missing, [])
    needed: dict[str, int] = {}
    tag_to_build_args: dict[str, tuple[str, str, int]] = {}
    for cand in candidates:
        msrv, _detected, commit_date = _resolve_metadata(cand)
        if commit_date is None or commit_date > max_sde_date:
            continue
        debian = _toolchain.debian_release_for(commit_date)
        bucket = _fat.bucket_for(msrv, commit_date, debian)
        if bucket is None:
            continue
        sde_info = _fat.canonical_sde_for(bucket, max_sde_date=max_sde_date)
        tag = _fat.tag_for(bucket, sde_info.sde)
        needed[tag] = needed.get(tag, 0) + 1
        tag_to_build_args[tag] = (bucket.rust_patch(), bucket.debian, sde_info.sde)

    missing = [tag for tag in needed if not _fat_image_present_locally(tag)]
    build_cmds = []
    for tag in missing:
        rust, debian, sde = tag_to_build_args[tag]
        build_cmds.append(
            f"python3 -m pipelines.cargo.fat_image build "
            f"--rust-version {rust} --debian-release {debian} "
            f"--source-date-epoch {sde}"
        )
    return needed, missing, build_cmds


def _regenerate_or_flag_stale(
    candidate: dict,
    entry_path: Path,
    rust_msrv: str,
    commit_date: dt.date,
    max_sde_date: dt.date,
    reassemble_stale: bool,
    rec: DriveRecord,
    *,
    logs_dir: Path,
    host_label: str | None,
    timeout_s: int,
) -> str:
    """Compare the existing entry's fatImage to what the current bucketer
    produces. If they match, hand off to cargo_regenerate and return
    "handled" (caller returns rec). If they differ and reassemble_stale
    is False, park the candidate as entry_bucket_stale and return
    "handled". If they differ and reassemble_stale is True, return
    "fall_through" — caller re-runs the full assembly pipeline.
    """
    try:
        with entry_path.open() as f:
            entry = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        rec.status = Status.ENTRY_BUCKET_STALE
        rec.reason = f"could not read existing entry: {e}"
        return "handled"

    entry_fat = (entry.get("reproduction") or {}).get("fatImage") or {}
    entry_tag = (f"rp2026/cargo-fat:{entry_fat.get('rustVersion')}-"
                 f"{entry_fat.get('debianRelease')}-"
                 f"{_sde_to_yyyymmdd(entry_fat.get('sourceDateEpoch'))}")
    rec.entry_path = str(entry_path)

    debian = _toolchain.debian_release_for(commit_date)
    bucket = _fat.bucket_for(rust_msrv, commit_date, debian)
    if bucket is None:
        # Can't bucket this candidate today (e.g. milestone/debian combo no
        # longer supported). Treat as stale — don't touch the entry.
        rec.status = Status.ENTRY_BUCKET_STALE
        rec.reason = (f"current bucketer refuses this candidate "
                      f"(msrv={rust_msrv}, debian={debian}); entry recorded {entry_tag}")
        return "handled"
    sde_info = _fat.canonical_sde_for(bucket, max_sde_date=max_sde_date)
    current_tag = _fat.tag_for(bucket, sde_info.sde)

    if current_tag != entry_tag:
        if not reassemble_stale:
            rec.fat_image_tag = entry_tag
            rec.status = Status.ENTRY_BUCKET_STALE
            rec.reason = (f"entry fatImage={entry_tag} but current bucketer "
                          f"says {current_tag}; pass --reassemble-stale to "
                          f"discard the entry and re-reproduce.")
            return "handled"
        return "fall_through"

    # Bucket matches — existing entry is current-bucketer-canonical. Hand
    # off to regenerate, which knows how to: verify the fat-image
    # fingerprint, build thin images, rerun tests, append a verifiedOn
    # record, and append a new-arch fingerprint if needed.
    rec.fat_image_tag = entry_tag
    key = _key(candidate)
    print(f"[{key}] existing entry at {entry_path} — regenerate-only path", file=sys.stderr)
    builder = "desktop-linux"
    # pick a builder that exists on this host
    for candidate_builder in ("rp2026", "default", "desktop-linux"):
        probe = subprocess.run(
            ["docker", "buildx", "inspect", candidate_builder],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if probe.returncode == 0:
            builder = candidate_builder
            break

    rc = _regenerate.regenerate(
        entry_path,
        build_missing_bases=False,
        skip_tests=False,
        host_label=host_label or socket.gethostname(),
        timeout_s=timeout_s,
        builder=builder,
    )
    if rc == _regenerate.EXIT_OK:
        rec.status = Status.OK
    elif rc == _regenerate.EXIT_FINGERPRINT_MISMATCH:
        rec.status = Status.REGENERATE_MISMATCH
        rec.reason = "regenerate: environment fingerprint mismatch"
    elif rc == _regenerate.EXIT_THIN_BUILD_FAILED:
        rec.status = Status.NOT_REPRODUCIBLE
        rec.reason = "regenerate: thin-image build failed"
    elif rc == _regenerate.EXIT_FAT_IMAGE_MISSING:
        rec.status = Status.FAT_IMAGE_MISSING
        rec.reason = f"regenerate: fat image not present: {entry_tag}"
    elif rc == _regenerate.EXIT_OUTCOME_MISMATCH:
        rec.status = Status.REGENERATE_MISMATCH
        rec.reason = "regenerate: outcome did not match entry's category"
    else:
        rec.status = Status.REGENERATE_MISMATCH
        rec.reason = f"regenerate returned unexpected rc={rc}"
    return "handled"


def _sde_to_yyyymmdd(sde: int | None) -> str:
    if sde is None:
        return "????????"
    return dt.datetime.fromtimestamp(int(sde), tz=dt.timezone.utc).strftime("%Y%m%d")


def _resolve_metadata(candidate: dict) -> tuple[str, bool, dt.date | None]:
    """Return (rust_msrv, msrv_detected, commit_date).

    `msrv_detected` is False when we fell back to a date-aware MSRV floor
    because the project declared nothing at the post-commit. True means
    rust-toolchain / Cargo.toml's rust-version supplied the value.
    """
    # Resolve commit_date first so the MSRV fallback can be date-aware.
    date_str = candidate.get("post_commit_date")
    if date_str is None:
        date_str = _toolchain.commit_date_at(candidate["repo"], candidate["post_commit"])
    commit_date = None
    if date_str:
        try:
            commit_date = dt.date.fromisoformat(date_str)
        except ValueError:
            commit_date = None

    msrv = candidate.get("rust_msrv")
    if msrv is None:
        msrv = _toolchain.msrv_at_commit(candidate["repo"], candidate["post_commit"])
    detected = msrv is not None
    if msrv is None:
        msrv = _msrv_floor_for(commit_date)

    return msrv, detected, commit_date


# ---- per-candidate flow -----------------------------------------------------

def process(candidate: dict, *, out_dir: Path, logs_dir: Path,
            build_missing_bases: bool, regenerate_verify: bool,
            timeout_s: int, host_label: str | None,
            max_sde_date: dt.date,
            reassemble_stale: bool = False,
            db: PipelineDB | None = None,
            run_id: str | None = None,
            db_lock: threading.Lock | None = None,
            force_fat_image: str | None = None,
            relax_locked: bool = False,
            attempts: int = 1) -> DriveRecord:
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

    # --- Short-circuit: existing entry on disk ---
    # If an entry JSON was produced by a prior host, we do NOT re-run the
    # full pipeline (which would overwrite it). Instead, we verify the
    # current bucketer agrees with the entry's recorded fatImage and, if
    # so, hand off to cargo_regenerate which merges per-arch fingerprints
    # and appends a verifiedOn record. If the bucketer disagrees, we park
    # the candidate as entry_bucket_stale so the operator can decide
    # whether to re-assemble (--reassemble-stale) or accept the old entry.
    entry_id = f"cargo-{candidate['post_commit'][:8]}"
    existing_entry_path = out_dir / f"{entry_id}.json"
    if existing_entry_path.exists():
        action = _regenerate_or_flag_stale(
            candidate, existing_entry_path, rust_msrv, commit_date,
            max_sde_date, reassemble_stale, rec,
            logs_dir=logs_dir, host_label=host_label,
            timeout_s=timeout_s,
        )
        if action == "handled":
            return rec
        # action == "fall_through" — stale + --reassemble-stale was set.
        print(f"[{key}] stale entry, --reassemble-stale set — re-running pipeline", file=sys.stderr)

    # --- 1b': enforce max_sde_date. Candidates with commit_date later than
    # the run's max_sde_date are rejected — the run is configured to stop
    # at this date, and bucketizing them would require future apt snapshots.
    if commit_date > max_sde_date:
        rec.status = Status.COMMIT_AFTER_MAX_SDE_DATE
        rec.reason = (f"commit_date={commit_date} > max_sde_date={max_sde_date}; "
                      f"raise --max-sde-date to include this candidate")
        return rec

    print(f"[{key}]   MSRV={rust_msrv}  commit_date={commit_date}", file=sys.stderr)

    # --- 1c: resolve fat image. Normal path uses the bucketer; the override
    # (--force-fat-image) bypasses it for targeted retries.
    existing = {r.tag: r for r in _fat.load_index()}
    if force_fat_image is not None:
        tag = force_fat_image
        if tag not in existing:
            rec.status = Status.FAT_IMAGE_MISSING
            rec.reason = f"--force-fat-image tag not in index: {tag}"
            return rec
    else:
        debian = _toolchain.debian_release_for(commit_date)
        bucket = _fat.bucket_for(rust_msrv, commit_date, debian)
        if bucket is None:
            rec.status = Status.FAT_IMAGE_MISSING
            rec.reason = (f"no supported fat image for (msrv={rust_msrv!r}, "
                          f"debian={debian}) — see fat_image.MILESTONE_DEBIAN_SUPPORTED")
            return rec
        sde_info = _fat.canonical_sde_for(bucket, max_sde_date=max_sde_date)
        tag = _fat.tag_for(bucket, sde_info.sde)
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
        run_id=run_id, attempts=attempts,
    )
    rec.flaky_pre = repro.flaky_pre
    rec.flaky_post = repro.flaky_post

    # Decide category from raw pass/fail facts. We don't get the category
    # from the candidate; we *discover* it here.
    #   pre fail         → unreproducible (pre_build_failed)
    #   pre pass, post fail → breaking
    #   pre pass, post pass → non-breaking
    # fix-after-update requires a third commit we don't have at ingestion.
    if not repro.pre_passed:
        rec.status = Status.NOT_REPRODUCIBLE
        # exit 124 is our reproducer-side timeout marker. Distinguish it
        # from real cargo errors so the drive_state readout + dashboards
        # can separate "we don't know" from "we know it fails".
        if repro.pre_exit_code == 124 or repro.post_exit_code == 124:
            rec.reason = (f"pre_build_timed_out (pre_rc={repro.pre_exit_code}, "
                          f"post_rc={repro.post_exit_code}, timeout_s={timeout_s})")
        else:
            rec.reason = (f"pre_build_failed (pre_rc={repro.pre_exit_code}, "
                          f"post_rc={repro.post_exit_code})")
        # Inline Scheme-2 classification. Write a row in
        # drive_state_classifications alongside drive_state so the failure
        # taxonomy is populated as the run progresses, not days later via
        # an off-driver script. Reason-only short-circuit handles
        # timeouts without paying the log read.
        from_reason = _failure_classifier.classify_from_reason(rec.reason)
        if from_reason is not None:
            cat, sub, ev = from_reason
            ecc: dict[str, int] = {}
        else:
            try:
                pre_text = Path(repro.pre_log_path).read_text(errors="replace")
            except OSError:
                pre_text = ""
            if not pre_text:
                cat, sub, ev = "NO_LOG", None, "no error line in pre-log"
                ecc = {}
            else:
                cat, sub, ev, ecc = _failure_classifier.classify_full(pre_text)
        rec.failure_category = cat
        rec.failure_subcategory = sub
        rec.failure_evidence = ev
        rec.failure_error_code_counts = ecc or None

        # --relax-locked retry path. When LOCK_FILE_STALE is the failure
        # cause, retry once with `cargo generate-lockfile && cargo test
        # --frozen`. Successful retries get `ok_after_relock` (distinct
        # from OK so the headline reproducibility number doesn't conflate
        # strict-contract success with lockfile-regenerated success).
        if relax_locked and cat == "LOCK_FILE_STALE":
            print(f"[{key}] LOCK_FILE_STALE — retrying with relax_locked",
                  file=sys.stderr)
            relock = _reproducer.reproduce(
                candidate, logs_dir, resolve_match.tag, timeout_s,
                resolve_match.tag, run_id=run_id, relax_locked=True,
            )
            if relock.pre_passed and relock.post_passed:
                rec.status = Status.OK_AFTER_RELOCK
                rec.reason = (f"ok_after_relock (orig pre={repro.pre_exit_code}, "
                              f"relock pre={relock.pre_exit_code}/post={relock.post_exit_code})")
                # Drop the failure-classification fields — this is now a success.
                rec.failure_category = None
                rec.failure_subcategory = None
                rec.failure_evidence = None
                rec.failure_error_code_counts = None
                # The relock'd repro becomes the entry-assembly source.
                repro = relock
                # Fall through to the breaking/non-breaking decision below.
            else:
                # Relock didn't help; keep the original failure record but
                # annotate that we tried.
                rec.reason += " (relock retry failed)"
                if db is not None:
                    with (db_lock if db_lock is not None else nullcontext()):
                        _record_repro_attempts(
                            db, run_id, candidate_key=key, host_label=host_label,
                            started_at=now, fat_image_tag=resolve_match.tag,
                            fingerprint=_ledger_digest_for_this_host(resolve_match),
                            repro=repro,
                        )
                return rec
        else:
            if db is not None:
                with (db_lock if db_lock is not None else nullcontext()):
                    _record_repro_attempts(
                        db, run_id, candidate_key=key, host_label=host_label,
                        started_at=now, fat_image_tag=resolve_match.tag,
                        fingerprint=_ledger_digest_for_this_host(resolve_match),
                        repro=repro,
                    )
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
    # additive; JSONL stays primary. Under --parallel > 1 these calls race;
    # gate the compound write with db_lock.
    if db is not None:
        # Gate the whole compound write under --parallel > 1 so threads
        # don't interleave mid-transaction. Cheap for N=1 (uncontended lock).
        with (db_lock if db_lock is not None else nullcontext()):
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
            _record_repro_attempts(
                db, run_id,
                candidate_key=key,
                host_label=host_label,
                started_at=now,
                fat_image_tag=resolve_match.tag,
                fingerprint=_ledger_digest_for_this_host(resolve_match),
                repro=repro,
                outcome_matched=repro.matches_category(discovered_category),
                entry_id=entry_id,
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

    # Preserve OK_AFTER_RELOCK if the relax-locked retry put us here.
    # If the multi-attempt run flagged flakiness on either side, mark
    # FLAKY so the headline can split stable from flaky reproductions.
    # Otherwise canonical strict-contract OK.
    if rec.status == Status.OK_AFTER_RELOCK:
        pass
    elif rec.flaky_pre or rec.flaky_post:
        rec.status = Status.FLAKY
    else:
        rec.status = Status.OK
    return rec


# ---- reclassify (post-hoc) --------------------------------------------------

def _reclassify_mode(args) -> int:
    """Re-apply Scheme-2 classification to an existing run's not_reproducible
    rows. Reads `data/cargo-logs/<short>-pre.log`, runs `classify()`, upserts
    the result. Idempotent — same primary key, ON CONFLICT DO UPDATE.

    No reproduction is run; this is purely "the rules changed, update old
    rows". The contract used to be a separate `scripts/reclassify_failures.py`
    script; folding it into the driver keeps DB writes on one path and
    populates the same Grafana dashboards as live runs.
    """
    if not args.db or not args.run_id:
        print("ERROR: --reclassify requires both --db and --run-id",
              file=sys.stderr)
        return 2
    # Logs live under <logs_dir>/<run_id>/ from May 2026 onward. Older
    # runs put them in the flat <logs_dir>/. Prefer the per-run subdir
    # when it exists; fall back to the flat directory.
    logs_base = Path(args.logs_dir)
    per_run_dir = logs_base / args.run_id
    logs_dir = per_run_dir if per_run_dir.is_dir() else logs_base
    if not logs_dir.is_dir():
        print(f"ERROR: --logs-dir {logs_base} does not exist", file=sys.stderr)
        return 2

    # candidate_key -> post_commit[:8] for log lookup. The candidates JSONL
    # is the same one the run was driven from; without it we can't map
    # "owner/repo#42" back to its pre-log filename.
    key_to_short: dict[str, str] = {}
    with open(args.candidates) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            c = json.loads(line)
            key_to_short[f"{c['repo']}#{c['pr_number']}"] = c["post_commit"][:8]

    # The reproducer now writes <short>-<run_id>-pre.log when run_id is set.
    # For backward compat with older runs (which used <short>-pre.log) we
    # try the run-id-suffixed path first, then fall back. New runs benefit
    # automatically; old runs keep working until the logs age out.
    def _candidate_pre_log(short: str) -> Path | None:
        suffixed = logs_dir / f"{short}-{args.run_id}-pre.log"
        legacy = logs_dir / f"{short}-pre.log"
        if suffixed.exists():
            return suffixed
        if legacy.exists():
            return legacy
        return None

    db = PipelineDB(Path(args.db))
    rows = db.conn.execute(
        "SELECT candidate_key, status, reason FROM drive_state "
        "WHERE run_id = ? AND status = ?",
        (args.run_id, Status.NOT_REPRODUCIBLE),
    ).fetchall()
    print(f"reclassifying {len(rows)} not_reproducible row(s) "
          f"from run={args.run_id}", file=sys.stderr)

    counts: dict[str, int] = {}
    for candidate_key, status, reason in rows:
        ecc: dict[str, int] = {}
        from_reason = _failure_classifier.classify_from_reason(reason)
        if from_reason is not None:
            cat, sub, ev = from_reason
        else:
            short = key_to_short.get(candidate_key)
            if short is None:
                cat, sub, ev = "OTHER", None, "post_commit not in candidates file"
            else:
                pre_log = _candidate_pre_log(short)
                if pre_log is None:
                    cat, sub, ev = "NO_LOG", None, f"no <short>-pre.log under {logs_dir}"
                else:
                    text = pre_log.read_text(errors="replace")
                    if not text:
                        cat, sub, ev = "NO_LOG", None, f"empty {pre_log.name}"
                    else:
                        cat, sub, ev, ecc = _failure_classifier.classify_full(text)
        db.upsert_drive_state_classification(
            run_id=args.run_id,
            candidate_key=candidate_key,
            category=cat,
            subcategory=sub,
            evidence=ev,
            error_code_counts=ecc or None,
        )
        counts[cat] = counts.get(cat, 0) + 1

    print(file=sys.stderr)
    print(f"{'category':<25s} {'count':>7s}", file=sys.stderr)
    print("-" * 36, file=sys.stderr)
    for cat in sorted(counts, key=lambda c: -counts[c]):
        print(f"{cat:<25s} {counts[cat]:>7d}", file=sys.stderr)
    return 0


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
                   help="Number of worker threads. Each worker runs an "
                        "independent candidate; the Docker daemon handles "
                        "concurrent containers. N>1 requires all fat images "
                        "pre-built (incompatible with --build-missing-bases). "
                        "Good starting points: 4 on a 16-core box, 8 on a "
                        "32-core box. RAM bound: each cargo test run can use "
                        "~4-8 GB.")
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
    p.add_argument("--skip-preflight", action="store_true",
                   help="Skip the fat-image availability check at startup. "
                        "Default is to fail fast if any candidate's canonical "
                        "fat-image tag is not locally present as a Docker image.")
    p.add_argument("--reassemble-stale", action="store_true",
                   help="When an existing entry JSON's fatImage disagrees with "
                        "what the current bucketer produces, discard the entry "
                        "and re-reproduce from scratch. Default: park the "
                        "candidate as entry_bucket_stale — an operator signal "
                        "that bucketing logic or max_sde_date changed since "
                        "the entry was written.")
    p.add_argument("--force-fat-image", default=None, metavar="TAG",
                   help="Override the bucketer and use TAG for every candidate. "
                        "The tag must already be registered in the index and "
                        "present in the Docker daemon's image store. Intended "
                        "for targeted re-runs against a hypothesised better "
                        "fat image (e.g. routing the OPENSSL_MISMATCH cohort "
                        "to a stretch-era image regardless of commit date). "
                        "Bypasses bucket_for / canonical_sde_for entirely.")
    p.add_argument("--cargo-cache", default=None,
                   help="Host directory bind-mounted into every reproducer "
                        "container at /usr/local/cargo. First candidate pulls "
                        "the crates.io index + crate tarballs; subsequent "
                        "candidates reuse the cache, cutting network ~3-5×. "
                        "Pass empty string to disable. Default: data/cargo-cache/ "
                        "next to the state file.")
    p.add_argument("--shuffle", action="store_true",
                   help="Shuffle the to-do list before dispatch. Spreads "
                        "expensive fork-clusters (libra/diem/solana family, "
                        "or repos with N adjacent PRs that share heavy deps) "
                        "across workers, stopping a single 30min linker from "
                        "blocking N workers' progress while their similar "
                        "candidates queue behind it. Resume-safe — shuffle "
                        "happens after the skip-list filter, so already-done "
                        "candidates are never re-shuffled in.")
    p.add_argument("--shuffle-seed", type=int, default=None,
                   help="Seed for --shuffle. Default: nondeterministic.")
    p.add_argument("--attempts", type=int, default=1,
                   help="Repeat each candidate's pre and post cargo-test "
                        "invocations N times. With N>1, mixed pass/fail "
                        "outcomes mark the candidate as ok_flaky / "
                        "not_reproducible_flaky. Default 1 (single attempt). "
                        "Wall clock multiplies roughly by N for the failure "
                        "cohort and by N for the success cohort.")
    p.add_argument("--relax-locked", action="store_true",
                   help="On a not_reproducible outcome classified LOCK_FILE_STALE, "
                        "retry once with `cargo generate-lockfile && cargo test "
                        "--frozen` instead of --locked. Successful retries get "
                        "status `ok_after_relock` (distinct from OK so the "
                        "headline doesn't conflate strict-contract reproductions "
                        "with lockfile-regenerated ones). Recovers ~25-35 of the "
                        "40 LOCK_FILE_STALE candidates from ds1-full per the "
                        "round-2 audit.")
    p.add_argument("--reclassify", action="store_true",
                   help="Post-hoc re-classification mode. Skips reproduction "
                        "entirely; iterates over the existing run's "
                        "drive_state rows, re-reads each candidate's "
                        "<short>-pre.log under --logs-dir, runs Scheme-2 "
                        "classify(), and upserts drive_state_classifications. "
                        "Used to apply newer classifier rules to an old run "
                        "without re-running cargo. Requires --db, --run-id, "
                        "--candidates, --logs-dir.")
    args = p.parse_args()

    if args.reclassify:
        return _reclassify_mode(args)

    max_sde_date = args.max_sde_date or _fat.default_max_sde_date()
    print(f"max_sde_date (run parameter): {max_sde_date}", file=sys.stderr)

    if args.parallel > 1 and args.build_missing_bases:
        print("ERROR: --parallel > 1 is incompatible with --build-missing-bases "
              "(fat-image register() writes the shared index and would race). "
              "Pre-build all fat images first via `cargo_plan_fat_images | bash`, "
              "then re-run without --build-missing-bases.", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    state_path = Path(args.state)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Logs go under <logs_dir>/<run_id>/ so each run's per-candidate
    # pre/post log files live in their own subdirectory. Previously every
    # run's logs piled into a single flat directory; with run-id-suffixed
    # filenames that was at least no-overwrite-safe, but it scaled to ~5k
    # files per run and made forensic auditing painful (which run did this
    # log come from?). Per-run subdirs make `ls`+`grep` workable and let
    # you tar up a run cleanly. Resolve run_id early so the path is final
    # before any worker writes.
    host_for_run = args.host or socket.gethostname()
    run_id_resolved = args.run_id or (
        f"drive-{host_for_run}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    logs_dir = Path(args.logs_dir) / run_id_resolved

    # Cargo cache: set the env var `_reproducer` reads. Create dir now so the
    # first worker doesn't race on mkdir; chmod 0777 so containers running
    # as root-in-container + later host-user cleanup both work.
    if args.cargo_cache is None:
        cargo_cache_path = (Path(args.state).parent.parent / "cargo-cache").resolve()
    elif args.cargo_cache == "":
        cargo_cache_path = None
    else:
        cargo_cache_path = Path(args.cargo_cache).resolve()
    if cargo_cache_path is not None:
        cargo_cache_path.mkdir(parents=True, exist_ok=True)
        cargo_cache_path.chmod(0o777)
        import os as _os
        # Docker -v needs an absolute path; relative paths get interpreted as
        # named volumes and fail with "invalid characters for a local volume
        # name". resolve() above handles that.
        _os.environ["CARGO_CACHE_DIR"] = str(cargo_cache_path)
        print(f"cargo cache: {cargo_cache_path}", file=sys.stderr)
    else:
        print("cargo cache: disabled", file=sys.stderr)
    logs_dir.mkdir(parents=True, exist_ok=True)

    existing = load_state(state_path)
    terminal = Status.TERMINAL_SUCCESS | Status.TERMINAL_FAILURE

    counts = {"skipped": 0, "processed": 0}
    status_counts: dict[str, int] = {}

    db: PipelineDB | None = None
    run_id: str | None = None
    if args.db:
        db = PipelineDB(Path(args.db))
        run_id = run_id_resolved
        db.start_run(
            run_id=run_id,
            host=host_for_run,
            git_sha=_git_sha(),
            candidates_source=str(Path(args.candidates).resolve()),
            max_sde_date=max_sde_date,
            python_version=host_platform.python_version(),
        )
        print(f"db: {args.db}  run_id={run_id}", file=sys.stderr)

    # Read candidates first; skip-list filter; then dispatch.
    todo: list[dict] = []
    with Path(args.candidates).open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            candidate = json.loads(line)
            if _key(candidate) in existing and existing[_key(candidate)].status in terminal:
                counts["skipped"] += 1
                continue
            todo.append(candidate)
            if args.limit and len(todo) >= args.limit:
                break

    # Shuffling spreads expensive fork-clusters (libra/diem/solana, plus
    # repos with N adjacent PRs that share a workspace) across the workers
    # instead of clumping them at the alphabetical-order JSONL slice. Big
    # impact on wall-clock when --parallel > 1: stops a single 30min
    # libra build from blocking 5 workers' progress while their own
    # libra-family candidates queue behind it.
    if args.shuffle:
        import random
        rng = random.Random(args.shuffle_seed) if args.shuffle_seed else random.Random()
        rng.shuffle(todo)
        print(f"shuffled {len(todo)} candidates "
              f"(seed={args.shuffle_seed if args.shuffle_seed else 'random'})",
              file=sys.stderr)

    # --- Preflight: every to-be-run candidate's canonical fat-image tag
    # must already be built locally. Without this, `docker run <tag>`
    # silently falls back to a registry pull and exits 125 when Docker
    # Hub denies access — which looks like a cargo pre-build failure in
    # the state log. Fail fast with a clear message + build commands.
    if todo and not args.skip_preflight and not args.build_missing_bases:
        print(f"preflight: checking fat images for {len(todo)} candidate(s)...", file=sys.stderr)
        needed, missing, build_cmds = _preflight_fat_images(
            todo, max_sde_date, force_fat_image=args.force_fat_image)
        print(f"  {len(needed)} distinct tags needed, {len(missing)} missing", file=sys.stderr)
        if missing:
            print("", file=sys.stderr)
            print("ERROR: fat images missing from the local Docker daemon:", file=sys.stderr)
            for tag in missing:
                print(f"  {tag}  ({needed[tag]} candidate(s) depend on it)", file=sys.stderr)
            print("", file=sys.stderr)
            print("Build them first:", file=sys.stderr)
            for cmd in build_cmds:
                print(f"  {cmd}", file=sys.stderr)
            print("", file=sys.stderr)
            print("Or re-run with --skip-preflight to dispatch anyway "
                  "(failures will be recorded as pre_build_failed).", file=sys.stderr)
            if db is not None and run_id is not None:
                db.finish_run(run_id)
                db.close()
            return 2

    n_workers = max(1, args.parallel)
    state_lock = threading.Lock()
    db_lock = threading.Lock() if (db is not None and n_workers > 1) else None

    # Graceful-shutdown plumbing. SIGTERM/SIGINT set the event; workers that
    # haven't yet entered a docker call check it and bail without writing
    # state. In-flight docker containers get docker-killed so the Python
    # subprocess.run returns promptly. Interrupted candidates never get a
    # terminal row in state/DB, so resume re-tries them.
    shutdown_event = threading.Event()

    def _install_signal_handlers() -> None:
        def _handler(signum, _frame):
            if shutdown_event.is_set():
                return
            shutdown_event.set()
            name = {signal.SIGTERM: "SIGTERM", signal.SIGINT: "SIGINT"}.get(signum, str(signum))
            print(f"\n[shutdown] caught {name} — stopping new work and "
                  f"killing in-flight containers...", file=sys.stderr)
            killed = _reproducer.kill_active_containers()
            print(f"[shutdown] docker-killed {killed} active container(s)", file=sys.stderr)
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass

    def _worker(candidate: dict) -> DriveRecord | None:
        if shutdown_event.is_set():
            return None
        rec = process(
            candidate,
            out_dir=out_dir, logs_dir=logs_dir,
            build_missing_bases=args.build_missing_bases,
            regenerate_verify=args.regenerate_verify,
            timeout_s=args.timeout,
            host_label=args.host,
            max_sde_date=max_sde_date,
            reassemble_stale=args.reassemble_stale,
            db=db,
            run_id=run_id,
            db_lock=db_lock,
            force_fat_image=args.force_fat_image,
            relax_locked=args.relax_locked,
            attempts=args.attempts,
        )
        # If shutdown fired mid-reproduction, the docker process was killed
        # and we got a partial/failed rec. Don't persist — resume re-tries.
        if shutdown_event.is_set():
            return None
        with state_lock:
            append_state(state_path, rec)
            if db is not None and run_id is not None:
                with (db_lock if db_lock is not None else nullcontext()):
                    _mirror_drive_state(db, run_id, rec)
        return rec

    print(f"workers: {n_workers}", file=sys.stderr)
    _install_signal_handlers()

    try:
        if n_workers == 1:
            # Serial path — unchanged semantics for resume ergonomics,
            # log readability, and "no threads at all" debugging.
            for candidate in todo:
                if shutdown_event.is_set():
                    break
                rec = _worker(candidate)
                if rec is None:
                    continue  # shutdown
                counts["processed"] += 1
                status_counts[rec.status] = status_counts.get(rec.status, 0) + 1
                print(f"[{rec.candidate_key}] → {rec.status}"
                      + (f"  ({rec.reason})" if rec.reason else ""),
                      file=sys.stderr)
        else:
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = [pool.submit(_worker, c) for c in todo]
                try:
                    for fut in as_completed(futures):
                        try:
                            rec = fut.result()
                        except Exception as e:
                            # A worker crashed. Log and move on — JSONL already
                            # missing the record, so the candidate will be
                            # re-tried on the next run. Don't kill the pool.
                            print(f"WORKER ERROR: {e!r}", file=sys.stderr)
                            continue
                        if rec is None:
                            continue  # shutdown-interrupted worker
                        counts["processed"] += 1
                        status_counts[rec.status] = status_counts.get(rec.status, 0) + 1
                        print(f"[{rec.candidate_key}] → {rec.status}"
                              + (f"  ({rec.reason})" if rec.reason else ""),
                              file=sys.stderr)
                finally:
                    if shutdown_event.is_set():
                        # Cancel pending futures so the pool exits promptly
                        # instead of starting more work after signal.
                        for fut in futures:
                            fut.cancel()
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

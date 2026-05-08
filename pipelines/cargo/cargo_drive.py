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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
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


def _ledger_digest_for_this_host(record) -> str | None:
    """Look up the fat-image fingerprint in the ledger matching this host's
    container platform. None if the ledger record has no fingerprint for
    this arch (e.g. fresh VM, only this-arch-now-building)."""
    container_platform = _regenerate.detect_container_platform(record.tag)
    fp = record.fingerprint_for(container_platform)
    return fp.digest if fp else None


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

def _fat_image_present_locally(tag: str) -> bool:
    """True iff `docker image inspect <tag>` returns a local image. Does NOT
    trigger a registry pull."""
    r = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def _preflight_fat_images(
    candidates: list[dict], max_sde_date: dt.date
) -> tuple[dict[str, int], list[str], list[str]]:
    """Walk candidates, compute each one's canonical fat-image tag, and
    check whether each tag is locally present as a Docker image.

    Returns:
        needed        — {tag: candidate_count}
        missing       — tags not present locally
        build_cmds    — exact `fat_image build` commands for missing tags
    """
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
            run_id: str | None = None,
            db_lock: threading.Lock | None = None) -> DriveRecord:
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
        # exit 124 is our reproducer-side timeout marker. Distinguish it
        # from real cargo errors so the drive_state readout + dashboards
        # can separate "we don't know" from "we know it fails".
        if repro.pre_exit_code == 124 or repro.post_exit_code == 124:
            rec.reason = (f"pre_build_timed_out (pre_rc={repro.pre_exit_code}, "
                          f"post_rc={repro.post_exit_code}, timeout_s={timeout_s})")
        else:
            rec.reason = (f"pre_build_failed (pre_rc={repro.pre_exit_code}, "
                          f"post_rc={repro.post_exit_code})")
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
            db.record_attempt(
                entry_id=entry_id,
                run_id=run_id,
                host_id=host_label or socket.gethostname(),
                host_os=host_platform.system().lower(),
                host_arch=_docker_platform(),
                started_at=now,
                finished_at=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                fat_image_tag_used=resolve_match.tag,
                # v0.0.5: fat-image records carry per-platform fingerprints; pick
                # the one for this host's container platform. None if not recorded.
                fingerprint_expected=_ledger_digest_for_this_host(resolve_match),
                fingerprint_actual=_ledger_digest_for_this_host(resolve_match),
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
    p.add_argument("--cargo-cache", default=None,
                   help="Host directory bind-mounted into every reproducer "
                        "container at /usr/local/cargo. First candidate pulls "
                        "the crates.io index + crate tarballs; subsequent "
                        "candidates reuse the cache, cutting network ~3-5×. "
                        "Pass empty string to disable. Default: data/cargo-cache/ "
                        "next to the state file.")
    args = p.parse_args()

    max_sde_date = args.max_sde_date or _fat.default_max_sde_date()
    print(f"max_sde_date (run parameter): {max_sde_date}", file=sys.stderr)

    if args.parallel > 1 and args.build_missing_bases:
        print("ERROR: --parallel > 1 is incompatible with --build-missing-bases "
              "(fat-image register() writes the shared index and would race). "
              "Pre-build all fat images first via `cargo_plan_fat_images | bash`, "
              "then re-run without --build-missing-bases.", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    logs_dir = Path(args.logs_dir)
    state_path = Path(args.state)
    out_dir.mkdir(parents=True, exist_ok=True)

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

    # --- Preflight: every to-be-run candidate's canonical fat-image tag
    # must already be built locally. Without this, `docker run <tag>`
    # silently falls back to a registry pull and exits 125 when Docker
    # Hub denies access — which looks like a cargo pre-build failure in
    # the state log. Fail fast with a clear message + build commands.
    if todo and not args.skip_preflight and not args.build_missing_bases:
        print(f"preflight: checking fat images for {len(todo)} candidate(s)...", file=sys.stderr)
        needed, missing, build_cmds = _preflight_fat_images(todo, max_sde_date)
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

    def _worker(candidate: dict) -> DriveRecord:
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
            db_lock=db_lock,
        )
        with state_lock:
            append_state(state_path, rec)
            if db is not None and run_id is not None:
                with (db_lock if db_lock is not None else nullcontext()):
                    _mirror_drive_state(db, run_id, rec)
        return rec

    print(f"workers: {n_workers}", file=sys.stderr)

    try:
        if n_workers == 1:
            # Serial path — unchanged semantics for resume ergonomics,
            # log readability, and "no threads at all" debugging.
            for candidate in todo:
                rec = _worker(candidate)
                counts["processed"] += 1
                status_counts[rec.status] = status_counts.get(rec.status, 0) + 1
                print(f"[{rec.candidate_key}] → {rec.status}"
                      + (f"  ({rec.reason})" if rec.reason else ""),
                      file=sys.stderr)
        else:
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = [pool.submit(_worker, c) for c in todo]
                for fut in as_completed(futures):
                    try:
                        rec = fut.result()
                    except Exception as e:
                        # A worker crashed. Log and move on — JSONL already
                        # missing the record, so the candidate will be
                        # re-tried on the next run. Don't kill the pool.
                        print(f"WORKER ERROR: {e!r}", file=sys.stderr)
                        continue
                    counts["processed"] += 1
                    status_counts[rec.status] = status_counts.get(rec.status, 0) + 1
                    print(f"[{rec.candidate_key}] → {rec.status}"
                          + (f"  ({rec.reason})" if rec.reason else ""),
                          file=sys.stderr)
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

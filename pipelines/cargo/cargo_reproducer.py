"""Cargo reproducer — run pre / post (and optionally fix) commits in Docker.

Given a `Candidate` JSON line (produced by `cargo_miner.py` or
`scripts/rebatchi_to_candidate.py`), clones the repo at the relevant
commits, runs `cargo test` inside a toolchain container, and captures
raw exit codes + full cargo JSON diagnostics for later classification.

The `ReproductionResult` holds raw facts; the caller (typically
`cargo_drive.py`) decides whether the outcome matches the candidate's
expected category via `ReproductionResult.matches_category(...)`.

The toolchain image is either passed explicitly (--toolchain) or, by
default, auto-detected per candidate by reading the repo's
rust-toolchain.toml, rust-toolchain, or Cargo.toml `rust-version` from
a throwaway shallow clone inside a small helper container. Auto-detect
returns a `rust:<minor>-alpine` tag suitable only for pure-Rust
projects — real batches should pass a fat image tag via
`--toolchain rp2026/cargo-fat:<...>`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Track every active reproducer container so the driver's signal handler
# can `docker kill` them on SIGTERM. Without this, orphan containers from
# in-flight reproductions keep running under the Docker daemon after the
# Python process exits — observed consuming ~28 GB of RAM across three
# libra/diem builds on 2026-05-09.
_active_containers: set[str] = set()
_active_containers_lock = threading.Lock()
# Set by `request_shutdown()` (via the driver's signal handler). Once set,
# every subsequent `_run_in_docker` invocation returns 137 (SIGKILL exit
# code) without spawning a container. Necessary because a single
# reproduce() runs pre + post commits sequentially: when SIGTERM lands
# during pre, the kill_active_containers() snapshot only sees the pre
# container, but post() would otherwise spawn a fresh one and leak.
_shutdown_requested = threading.Event()


def request_shutdown() -> None:
    """Mark the reproducer as shutting down. Idempotent."""
    _shutdown_requested.set()


def _register_container(name: str) -> None:
    with _active_containers_lock:
        _active_containers.add(name)


def _unregister_container(name: str) -> None:
    with _active_containers_lock:
        _active_containers.discard(name)


def kill_active_containers(timeout_per_kill_s: int = 10) -> int:
    """Best-effort `docker kill` on every currently-tracked container.
    Called from the driver's signal handler. Returns the number of kills
    attempted. Idempotent — safe to call multiple times. Sets the
    shutdown flag so subsequent docker runs are skipped.
    """
    request_shutdown()
    with _active_containers_lock:
        names = list(_active_containers)
    for n in names:
        try:
            subprocess.run(
                ["docker", "kill", n],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout_per_kill_s,
            )
        except Exception:
            pass
    return len(names)

# Dual-mode import so the module works both as `python -m pipelines.cargo.cargo_reproducer`
# (package context) and as a standalone script. The driver uses the former.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from pipelines.cargo.cargo_toolchain import detect_toolchain  # noqa: E402
else:
    from .cargo_toolchain import detect_toolchain  # noqa: E402

DEFAULT_RUST_IMAGE = "rust:1.75-alpine"
# Script fragments must be POSIX sh compatible (Alpine ships ash, not bash).
APK_DEPS = "apk add --no-cache git musl-dev gcc pkgconfig openssl-dev"
DEB_THIN_DEPS = "apt-get update >/dev/null && apt-get install -y --no-install-recommends git pkg-config build-essential libssl-dev >/dev/null"
# Plain `json` message format — supported by every cargo since ~2017.
# The richer `json-diagnostic-rendered-ansi` variant (added ~rustc 1.38) keeps
# the pretty-printed `rendered` field which is nicer for humans reading logs,
# but some candidate repos pin an older rustc via rust-toolchain that rejects
# the fancier format with "isn't a valid value for --message-format" — exit 1
# before a single test runs, classified wrongly as pre_build_failed.
# The classifier only reads the JSON stream, so dropping `-rendered-ansi`
# costs nothing and unbreaks the old-toolchain candidates.
BUILD_CMD = "cargo test --locked --message-format=json --no-fail-fast"
# Relaxed variant for the LOCK_FILE_STALE retry path: regenerate Cargo.lock
# from scratch, then build without --locked or --frozen (so cargo can
# download the crates the regenerated lockfile points to). Recovers the
# Dependabot-bumped-Cargo.toml-without-relock pattern documented in
# docs/ds1-reconcile.md (overdrop-sebool, rust-central-station fork-clusters).
# Successes from this path get a distinct status (`ok_after_relock`) so the
# headline reproducibility number stays honest about which reproductions
# required lockfile regeneration.
#
# An earlier version chained `cargo test --frozen`; --frozen forbids both
# lock changes AND network, so it failed at "attempting to make an HTTP
# request, but --frozen was specified" when fetching crates the new lock
# pointed at. Plain `cargo test` is honest about the relaxation.
BUILD_CMD_RELAXED = (
    "cargo generate-lockfile && "
    "cargo test --message-format=json --no-fail-fast"
)

# A tiny image used only for `git clone` + file-read during toolchain detection.
GIT_HELPER_IMAGE = "alpine/git:latest"

# Host-side cargo cache directory. Mounted into every reproducer container at
# /usr/local/cargo so that the crates.io index + downloaded crate tarballs
# persist across candidates. First candidate pays the full download cost;
# subsequent candidates hit the cache for ~60-80% of deps (2019-era Rust
# ecosystem has heavy overlap on serde/tokio/hyper/clap/log). Cuts network
# traffic ~3-5×, which matters on bandwidth-constrained hosts (5G, cloud
# egress caps).
#
# Read per-call from the CARGO_CACHE_DIR env var (set by cargo_drive). Empty
# or unset disables the mount and each candidate gets a fresh empty cache.


def _cargo_cache_dir() -> str:
    return os.environ.get("CARGO_CACHE_DIR", "")

# Image-tag prefixes that are pre-provisioned with git + all *-dev packages
# (see docker/cargo-fat/Dockerfile). These skip the runtime install step.
FAT_IMAGE_PREFIXES = ("rp2026/cargo-fat", "ghcr.io/tudelft-rp2026/cargo-fat")


@dataclass
class ReproductionResult:
    """Raw pass/fail facts from running pre + post (and optionally fix)
    commits. The caller decides reproducibility based on the entry's
    category — `breaking` wants pre-pass + post-fail, `non-breaking` wants
    pre-pass + post-pass, `fix-after-update` wants all three pre/post/fix
    with the middle one failing and the others passing.

    pre_exit_code / post_exit_code / fix_exit_code are the *primary*
    outcomes (majority vote when --attempts > 1). The full per-attempt
    history lives in pre_exit_codes / post_exit_codes / fix_exit_codes
    + the matching log-path lists; for --attempts=1 these contain a
    single element each (or None for fix when there's no fix commit).
    """
    repo: str
    pr_number: int
    pre_commit: str
    post_commit: str
    fix_commit: str | None
    pre_exit_code: int
    post_exit_code: int
    fix_exit_code: int | None
    pre_log_path: str
    post_log_path: str
    fix_log_path: str | None
    toolchain: str
    detected_toolchain: bool = False
    # Per-attempt fields, populated when --attempts > 1. Always non-empty
    # lists matching the count of attempts that ran.
    pre_exit_codes: list[int] = field(default_factory=list)
    post_exit_codes: list[int] = field(default_factory=list)
    fix_exit_codes: list[int] = field(default_factory=list)
    pre_log_paths: list[str] = field(default_factory=list)
    post_log_paths: list[str] = field(default_factory=list)
    fix_log_paths: list[str] = field(default_factory=list)
    flaky_pre: bool = False
    flaky_post: bool = False

    @property
    def pre_passed(self) -> bool:
        return self.pre_exit_code == 0

    @property
    def post_passed(self) -> bool:
        return self.post_exit_code == 0

    @property
    def fix_passed(self) -> bool | None:
        if self.fix_exit_code is None:
            return None
        return self.fix_exit_code == 0

    def matches_category(self, category: str) -> bool:
        """Does this result match the expected pass/fail pattern for
        `category`? Used by drivers to decide whether to accept the
        reproduction as successful."""
        if category == "breaking":
            return self.pre_passed and not self.post_passed
        if category == "non-breaking":
            return self.pre_passed and self.post_passed
        if category == "fix-after-update":
            return (
                self.pre_passed
                and not self.post_passed
                and self.fix_passed is True
            )
        return False


def _image_flavor(image: str) -> str:
    """Rough classification: alpine or debian-ish."""
    return "alpine" if "alpine" in image else "debian"


def _install_deps_cmd(image: str) -> str:
    # Fat images already have git + every -dev package baked in.
    if any(image.startswith(p) for p in FAT_IMAGE_PREFIXES):
        return "true"
    return APK_DEPS if _image_flavor(image) == "alpine" else DEB_THIN_DEPS


def _fetch_toolchain_files(repo: str, commit: str, dest: Path) -> None:
    """Fetch rust-toolchain.toml / rust-toolchain / Cargo.toml at `commit`
    into `dest` using a throwaway container. No git required on host."""
    dest.mkdir(parents=True, exist_ok=True)
    script = (
        f"cd /tmp && "
        f"git clone --quiet --depth 50 https://github.com/{repo}.git repo && "
        f"cd repo && "
        f"(git checkout --quiet {commit} 2>/dev/null || "
        f"  (git fetch --quiet origin {commit}:_repro && git checkout --quiet _repro)) && "
        f"cp -f rust-toolchain.toml /out/ 2>/dev/null ; "
        f"cp -f rust-toolchain /out/ 2>/dev/null ; "
        f"cp -f Cargo.toml /out/ 2>/dev/null ; "
        f"true"
    )
    subprocess.run(
        [
            "docker", "run", "--rm", "--entrypoint", "sh",
            "-v", f"{dest}:/out",
            GIT_HELPER_IMAGE, "-c", script,
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=180,
    )


def _run_in_docker(
    repo: str,
    commit: str,
    toolchain_image: str,
    log_out: Path,
    timeout_s: int,
    build_cmd: str = BUILD_CMD,
) -> int:
    """Clone repo, checkout commit (including PR refs), run cargo test."""
    # Refuse to start new docker work once shutdown has been requested.
    # Otherwise a reproduce() in mid-flight (pre killed, post about to
    # start) would spawn a fresh container the signal handler never sees,
    # leaking compute past driver shutdown.
    if _shutdown_requested.is_set():
        try:
            with log_out.open("wb") as f:
                f.write(b"error: reproducer shutdown - pre-empted before docker run\n")
        except OSError:
            pass
        return 137
    repo_url = f"https://github.com/{repo}.git"
    # Name the container so we can `docker kill` it on timeout — without this,
    # subprocess.run's TimeoutExpired terminates the `docker` CLI client but
    # leaves the daemon-side container running (hung tests = N workers
    # deadlocked waiting on container exit that never comes).
    container_name = f"cargo-repro-{uuid.uuid4().hex[:12]}"
    # Cargo.toml-discovery shim. Many DS1 repos are polyglot projects whose
    # Cargo.toml lives one level down (e.g. ./rust/, ./packages/server/,
    # ./foodi-backend/). Hard-coding /src mis-counted 76 such candidates
    # as REPO_GONE in DS1-full. Audit confirmed every one of those cases
    # had Cargo.toml at exactly depth 1; deeper search risks picking up
    # vendored fixture manifests, so we cap at maxdepth=2 (depth 1 from
    # /src). Prefer a Cargo.toml carrying [workspace]; otherwise the
    # first depth-1 manifest. Falls back to /src when nothing found.
    discover_workdir = (
        "WORKDIR=/src; "
        "if [ ! -f /src/Cargo.toml ]; then "
        "  M=$(find /src -mindepth 2 -maxdepth 2 -name Cargo.toml 2>/dev/null "
        "    | xargs -r grep -l '^\\[workspace\\]' 2>/dev/null | head -1); "
        "  if [ -z \"$M\" ]; then "
        "    M=$(find /src -mindepth 2 -maxdepth 2 -name Cargo.toml 2>/dev/null "
        "      | head -1); "
        "  fi; "
        "  [ -n \"$M\" ] && WORKDIR=$(dirname \"$M\"); "
        "fi; "
        "echo \"[reproducer] workdir=$WORKDIR\""
    )
    # Handles both branch-tip commits and closed-PR commits that aren't
    # reachable from the default branch: first try a plain checkout, then
    # fall back to fetching the commit explicitly.
    inner_script = (
        f"{_install_deps_cmd(toolchain_image)} && "
        f"git clone --quiet {repo_url} /src && "
        f"cd /src && "
        f"(git checkout --quiet {commit} 2>/dev/null || "
        f"  (git fetch --quiet origin {commit}:_repro && git checkout --quiet _repro)) && "
        f"{discover_workdir} && "
        f"cd \"$WORKDIR\" && "
        f"{build_cmd}"
    )
    cmd = [
        "docker", "run", "--rm",
        "--name", container_name,
        "--network", "bridge",
        # Cap per-container memory. With --parallel 4 on a 32G host this
        # caps aggregate at 32G worst case; realistic steady state is much
        # lower. Protects against a single pathological linker starving
        # the host — observed on diem/libra DS1 candidates.
        "--memory=8g",
    ]
    cache_dir = _cargo_cache_dir()
    if cache_dir:
        # Ensure the host dir exists (driver creates it with mode 0o777 at
        # startup; this is a belt-and-braces no-op).
        #
        # Mount to /cargo-cache and set CARGO_HOME there, NOT onto
        # /usr/local/cargo — that path contains the fat image's actual cargo
        # and rustc binaries, and bind-mounting over it hides them.
        # CARGO_HOME relocates the registry + git + .cargo-lock; the binaries
        # keep being found via PATH.
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        cmd += ["-v", f"{cache_dir}:/cargo-cache",
                "-e", "CARGO_HOME=/cargo-cache"]
    cmd += [toolchain_image, "sh", "-c", inner_script]
    _register_container(container_name)
    try:
        with log_out.open("wb") as f:
            try:
                r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=timeout_s)
                return r.returncode
            except subprocess.TimeoutExpired:
                # Don't leave the container running — it'll hold the worker.
                # The kill itself can slow-path under heavy Docker daemon load
                # (seen with N=8 during bulk container cleanup). Swallow a
                # kill-timeout and return the reproduction-timeout code anyway
                # — a zombie container costs less than a lost candidate record.
                try:
                    subprocess.run(
                        ["docker", "kill", container_name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=30,
                    )
                except (subprocess.TimeoutExpired, OSError):
                    pass
                # Write a clear marker to the log so the classifier can route
                # this to TEST_TIMEOUT rather than OTHER.
                try:
                    f.write(
                        f"\nerror: reproducer timeout — cargo test exceeded "
                        f"{timeout_s} seconds and was killed\n".encode("utf-8")
                    )
                except OSError:
                    pass
                return 124
    finally:
        _unregister_container(container_name)


def _detect_toolchain_for_candidate(candidate: dict, default: str) -> tuple[str, bool]:
    """Return (toolchain_image, detected_bool)."""
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        _fetch_toolchain_files(candidate["repo"], candidate["post_commit"], dest)
        tc = detect_toolchain(dest, default=default)
    return tc, tc != default


def reproduce(candidate: dict, logs_dir: Path, toolchain: str | None,
              timeout_s: int, default_image: str,
              run_id: str | None = None,
              relax_locked: bool = False,
              attempts: int = 1) -> ReproductionResult:
    if toolchain is None:
        tc, detected = _detect_toolchain_for_candidate(candidate, default_image)
    else:
        tc, detected = toolchain, False

    # Suffix run_id into log paths so cross-run replays don't overwrite
    # historical evidence. Without this, a retry/re-classification run
    # silently destroys the original DS1-full pre-log under the same
    # post_commit short hash — which is how the 48-candidate contamination
    # cluster ended up unauditable in 2026-05-12.
    short = candidate["post_commit"][:8]
    suffix = f"-{run_id}" if run_id else ""
    relax_tag = "-relock" if relax_locked else ""
    build_cmd = BUILD_CMD_RELAXED if relax_locked else BUILD_CMD

    def _attempt_log(stage: str, idx: int) -> Path:
        """Per-attempt log path. attempts==1 keeps the historical
        `<short>{suffix}{relax}-{stage}.log` shape so external tools
        that grep <short>-pre.log keep working. attempts>1 inserts
        `-aN` to disambiguate."""
        suf = "" if attempts == 1 else f"-a{idx + 1}"
        return logs_dir / f"{short}{suffix}{relax_tag}-{stage}{suf}.log"

    def _run_n(commit: str, stage: str) -> tuple[list[int], list[str], int, bool]:
        """Run `attempts` invocations of (clone, checkout commit, cargo test).
        Returns (exit_codes, log_paths, primary_rc, flaky).

        primary_rc = 0 if any attempt passed, else the first non-zero rc.
        This is asymmetric on purpose: a single passing attempt counts as
        "the candidate can pass" — flakiness goes the other way (a passing
        candidate that sometimes fails). flaky = True iff the attempts
        disagree (mix of pass and non-pass exit codes)."""
        rcs: list[int] = []
        logs: list[str] = []
        for i in range(attempts):
            log = _attempt_log(stage, i)
            rc = _run_in_docker(candidate["repo"], commit, tc, log, timeout_s, build_cmd)
            rcs.append(rc)
            logs.append(str(log))
            # Once shutdown was requested, abort the loop early.
            if _shutdown_requested.is_set():
                break
        if not rcs:
            return rcs, logs, 137, False
        passes = sum(1 for r in rcs if r == 0)
        flaky = 0 < passes < len(rcs)
        primary = 0 if passes > 0 else next((r for r in rcs if r != 0), rcs[0])
        return rcs, logs, primary, flaky

    pre_rcs, pre_logs, pre_rc, flaky_pre = _run_n(candidate["pre_commit"], "pre")
    post_rcs, post_logs, post_rc, flaky_post = _run_n(candidate["post_commit"], "post")

    # Optional fix commit for fix-after-update candidates.
    fix_commit = candidate.get("fix_commit")
    fix_rcs: list[int] = []
    fix_logs: list[str] = []
    fix_rc: int | None = None
    if fix_commit:
        fix_rcs, fix_logs, fix_rc, _ = _run_n(fix_commit, "fix")

    return ReproductionResult(
        repo=candidate["repo"],
        pr_number=candidate["pr_number"],
        pre_commit=candidate["pre_commit"],
        post_commit=candidate["post_commit"],
        fix_commit=fix_commit,
        pre_exit_code=pre_rc,
        post_exit_code=post_rc,
        fix_exit_code=fix_rc,
        pre_log_path=pre_logs[0] if pre_logs else "",
        post_log_path=post_logs[0] if post_logs else "",
        fix_log_path=(fix_logs[0] if fix_logs else None),
        toolchain=tc,
        detected_toolchain=detected,
        pre_exit_codes=pre_rcs,
        post_exit_codes=post_rcs,
        fix_exit_codes=fix_rcs,
        pre_log_paths=pre_logs,
        post_log_paths=post_logs,
        fix_log_paths=fix_logs,
        flaky_pre=flaky_pre,
        flaky_post=flaky_post,
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Reproduce a Cargo breaking-update candidate in Docker.")
    p.add_argument("--in", dest="inp", required=True, help="Candidate JSONL (one candidate = one line).")
    p.add_argument("--logs-dir", default="./data/cargo-logs")
    p.add_argument("--out", default="-", help="Output JSONL of reproduction results.")
    p.add_argument(
        "--toolchain",
        default=None,
        help="Override the toolchain image. Default: auto-detect per candidate.",
    )
    p.add_argument(
        "--default-image",
        default=DEFAULT_RUST_IMAGE,
        help="Fallback image when detection fails.",
    )
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--run-id", default=None,
                   help="Suffix added to log filenames so cross-run replays "
                        "don't overwrite historical evidence.")
    p.add_argument("--attempts", type=int, default=1,
                   help="Number of repeated cargo-test invocations per "
                        "commit (pre and post). When >1, the result records "
                        "every attempt's exit code; the primary outcome is "
                        "pass if any attempt passed (flakiness is then "
                        "recorded separately).")
    args = p.parse_args()

    logs_dir = Path(args.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    out_fh = sys.stdout if args.out == "-" else open(args.out, "w")
    try:
        with open(args.inp) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                cand = json.loads(line)
                print(f"reproducing {cand['repo']}#{cand['pr_number']} ...", file=sys.stderr)
                res = reproduce(cand, logs_dir, args.toolchain, args.timeout,
                                args.default_image, run_id=args.run_id,
                                attempts=args.attempts)
                print(f"  toolchain: {res.toolchain} ({'detected' if res.detected_toolchain else 'override/default'})", file=sys.stderr)
                out_fh.write(json.dumps(asdict(res)) + "\n")
                out_fh.flush()
                fix_tag = f", fix_rc={res.fix_exit_code}" if res.fix_exit_code is not None else ""
                print(f"  -> pre_rc={res.pre_exit_code}, post_rc={res.post_exit_code}{fix_tag}", file=sys.stderr)
    finally:
        if out_fh is not sys.stdout:
            out_fh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

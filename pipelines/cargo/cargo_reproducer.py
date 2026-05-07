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
from dataclasses import asdict, dataclass, field
from pathlib import Path

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
BUILD_CMD = "cargo test --locked --message-format=json-diagnostic-rendered-ansi --no-fail-fast"

# A tiny image used only for `git clone` + file-read during toolchain detection.
GIT_HELPER_IMAGE = "alpine/git:latest"

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
) -> int:
    """Clone repo, checkout commit (including PR refs), run cargo test."""
    repo_url = f"https://github.com/{repo}.git"
    # Handles both branch-tip commits and closed-PR commits that aren't
    # reachable from the default branch: first try a plain checkout, then
    # fall back to fetching the commit explicitly.
    inner_script = (
        f"{_install_deps_cmd(toolchain_image)} && "
        f"git clone --quiet {repo_url} /src && "
        f"cd /src && "
        f"(git checkout --quiet {commit} 2>/dev/null || "
        f"  (git fetch --quiet origin {commit}:_repro && git checkout --quiet _repro)) && "
        f"{BUILD_CMD}"
    )
    cmd = [
        "docker", "run", "--rm",
        "--network", "bridge",
        toolchain_image,
        "sh", "-c", inner_script,
    ]
    with log_out.open("wb") as f:
        try:
            r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=timeout_s)
            return r.returncode
        except subprocess.TimeoutExpired:
            return 124


def _detect_toolchain_for_candidate(candidate: dict, default: str) -> tuple[str, bool]:
    """Return (toolchain_image, detected_bool)."""
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        _fetch_toolchain_files(candidate["repo"], candidate["post_commit"], dest)
        tc = detect_toolchain(dest, default=default)
    return tc, tc != default


def reproduce(candidate: dict, logs_dir: Path, toolchain: str | None, timeout_s: int, default_image: str) -> ReproductionResult:
    if toolchain is None:
        tc, detected = _detect_toolchain_for_candidate(candidate, default_image)
    else:
        tc, detected = toolchain, False

    short = candidate["post_commit"][:8]
    pre_log = logs_dir / f"{short}-pre.log"
    post_log = logs_dir / f"{short}-post.log"

    pre_rc = _run_in_docker(candidate["repo"], candidate["pre_commit"], tc, pre_log, timeout_s)
    post_rc = _run_in_docker(candidate["repo"], candidate["post_commit"], tc, post_log, timeout_s)

    # Optional fix commit for fix-after-update candidates.
    fix_commit = candidate.get("fix_commit")
    fix_rc: int | None = None
    fix_log: Path | None = None
    if fix_commit:
        fix_log = logs_dir / f"{short}-fix.log"
        fix_rc = _run_in_docker(candidate["repo"], fix_commit, tc, fix_log, timeout_s)

    return ReproductionResult(
        repo=candidate["repo"],
        pr_number=candidate["pr_number"],
        pre_commit=candidate["pre_commit"],
        post_commit=candidate["post_commit"],
        fix_commit=fix_commit,
        pre_exit_code=pre_rc,
        post_exit_code=post_rc,
        fix_exit_code=fix_rc,
        pre_log_path=str(pre_log),
        post_log_path=str(post_log),
        fix_log_path=str(fix_log) if fix_log else None,
        toolchain=tc,
        detected_toolchain=detected,
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
                res = reproduce(cand, logs_dir, args.toolchain, args.timeout, args.default_image)
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

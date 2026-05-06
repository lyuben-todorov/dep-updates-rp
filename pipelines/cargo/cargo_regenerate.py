"""Cargo regenerator — entry-driven reproducibility checker.

Given a v0.0.4 entry JSON, this script:

  1. Resolves the fat toolchain image named by the entry's
     `reproduction.fatImage` inputs. If absent locally and
     `--build-missing-bases` is set, builds it with the pinned Rust
     patch version + apt snapshot via the vendored repro-sources-list.sh
     (see docker/cargo-fat/Dockerfile).
  2. Extracts /manifest/{packages.txt, rustc.txt, cargo.txt, os-release,
     sources.list} from the fat image and computes the environment
     fingerprint. Compares to the entry's recorded fingerprint.
     Mismatch is a hard failure: the environment differs from what the
     entry was validated against.
  3. Builds the thin `<hash>-pre`, `<hash>-post`, and (for
     fix-after-update entries) `<hash>-fix` images with cargo-vendor
     baked in for offline runs.
  4. Runs `cargo test <flags>` inside each thin image with
     `--network none`. Confirms the pass/fail pattern matches the
     entry's category (breaking / non-breaking / fix-after-update).
  5. Appends a record to `reproduction.verifiedOn` and writes the
     entry back.

The `fatImage.expectedDigest` field is ADVISORY per Fork B (see
../../../docs/reproducible-builds.md): mismatches are warnings, not
errors, because apt layer bytes can jitter across rebuilds even with
pinned inputs. The environment fingerprint is the actual
reproducibility contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform as host_platform
import shutil
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
from bump_ext import validate_entry, SchemaError  # noqa: E402

# Canonical concat order for the environment fingerprint. MUST match the
# order used when the entry was first written, otherwise the digest won't
# reproduce.
FINGERPRINT_FILES = ["packages.txt", "rustc.txt", "cargo.txt", "os-release", "sources.list"]

FAT_IMAGE_REPO = "rp2026/cargo-fat"
DOCKERFILE_DIR = Path(__file__).resolve().parents[2] / "docker" / "cargo-fat"

# Thin-image Dockerfile template. `--remap-path-prefix=/src=.` is a defensive
# measure against build-path leakage — /src is already stable inside the
# container but this guards against accidental cwd changes.
THIN_DOCKERFILE = textwrap.dedent("""\
    FROM {base_image} AS build
    WORKDIR /src
    RUN git clone {repo_url} . && \\
        ( git checkout --quiet {commit} 2>/dev/null || \\
          ( git fetch --quiet origin {commit}:_repro && git checkout --quiet _repro ) )
    RUN cargo vendor --quiet vendor > /tmp/vendor.toml
    RUN mkdir -p .cargo && \\
        printf '%s\\n' \\
            '[source.crates-io]' \\
            'replace-with = "vendored-sources"' \\
            '[source.vendored-sources]' \\
            'directory = "vendor"' \\
            > .cargo/config.toml
    ENV RUSTFLAGS="--remap-path-prefix=/src=."
    CMD {cmd_json}
""")

EXIT_OK = 0
EXIT_FINGERPRINT_MISMATCH = 1
EXIT_THIN_BUILD_FAILED = 2
EXIT_FAT_IMAGE_MISSING = 3
EXIT_OUTCOME_MISMATCH = 4


class RegenerateError(Exception):
    """Raised when the regeneration contract is violated."""


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin wrapper that prints the command before running."""
    print(f"  $ {' '.join(cmd)}", file=sys.stderr)
    return subprocess.run(cmd, **kwargs)


# ---- fat image resolution (delegates to fat_image.py) ------------------------

def fat_image_tag(rust_version: str, debian_release: str, apt_snapshot: str) -> str:
    """Canonical tag. `apt_snapshot` is a 'YYYYMMDDTHHMMSSZ' string;
    `fat_image.fat_image_tag` takes an SDE int, so we convert.
    """
    import datetime as _dt
    date_part = apt_snapshot.split("T")[0]
    sde = int(_dt.datetime.strptime(date_part, "%Y%m%d")
              .replace(tzinfo=_dt.timezone.utc).timestamp())
    from . import fat_image as _fat
    return _fat.fat_image_tag(rust_version, debian_release, sde)


def fat_image_exists(tag: str) -> bool:
    r = subprocess.run(
        ["docker", "image", "inspect", tag],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


def fat_image_digest(tag: str) -> str | None:
    """Return the local image ID (`Id` field) as `sha256:...`, or None."""
    r = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    if r.returncode != 0:
        return None
    digest = r.stdout.strip()
    return digest or None


def build_fat_image(tag: str, rust_version: str, debian_release: str, sde: int,
                    *, builder: str = "desktop-linux") -> None:
    """Thin wrapper around fat_image.build_fat_image. `tag` is derived
    internally from the other args; we only accept it as a parameter
    for call-site readability + to assert the caller expected the
    same tag."""
    from . import fat_image as _fat
    try:
        produced_tag = _fat.build_fat_image(rust_version, sde,
                                            debian_release=debian_release,
                                            builder=builder)
    except _fat.IndexError as e:
        raise RegenerateError(str(e)) from e
    if produced_tag != tag:
        raise RegenerateError(
            f"fat image tag mismatch: expected {tag}, built {produced_tag}"
        )


# ---- environment fingerprint extraction --------------------------------------

def extract_manifest(tag: str, dest: Path) -> None:
    """Copy /manifest/* from the fat image onto the host."""
    dest.mkdir(parents=True, exist_ok=True)
    script = "cp /manifest/* /out/ && ls /out"
    r = _run(
        ["docker", "run", "--rm", "-v", f"{dest}:/out", tag, "sh", "-c", script],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if r.returncode != 0:
        raise RegenerateError(f"failed to extract /manifest/ from {tag}: {r.stderr}")


def compute_fingerprint(manifest_dir: Path) -> tuple[str, list[dict]]:
    """Return (digest, per_file_records) matching the v0.0.4 schema shape."""
    concat = b""
    per_file = []
    for name in FINGERPRINT_FILES:
        path = manifest_dir / name
        data = path.read_bytes()
        concat += data
        per_file.append({
            "path": f"/manifest/{name}",
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        })
    digest = "sha256:" + hashlib.sha256(concat).hexdigest()
    return digest, per_file


def diff_fingerprint(expected: dict, actual_files: list[dict]) -> list[str]:
    """Human-readable per-file diff lines."""
    exp_by_path = {f["path"]: f for f in expected["files"]}
    act_by_path = {f["path"]: f for f in actual_files}
    lines = []
    for path in sorted(set(exp_by_path) | set(act_by_path)):
        e = exp_by_path.get(path)
        a = act_by_path.get(path)
        if e is None:
            lines.append(f"  + unexpected file: {path} ({a['sha256'][:12]}..., {a['bytes']} bytes)")
        elif a is None:
            lines.append(f"  - missing file: {path} (expected {e['sha256'][:12]}..., {e['bytes']} bytes)")
        elif e["sha256"] != a["sha256"]:
            lines.append(
                f"  ! {path}: sha256 {e['sha256'][:12]}... → {a['sha256'][:12]}..., "
                f"bytes {e['bytes']} → {a['bytes']}"
            )
    return lines


# ---- thin image build --------------------------------------------------------

def _write_thin_dockerfile(tmp: Path, repo_url: str, commit: str, base_image: str, build_flags: list[str]) -> Path:
    """Emit a thin Dockerfile. CMD runs cargo test with the entry's flags."""
    cmd_list = ["cargo", "test", *build_flags, "--no-fail-fast"]
    cmd_json = json.dumps(cmd_list)
    p = tmp / f"Dockerfile.{commit[:8]}"
    p.write_text(THIN_DOCKERFILE.format(
        base_image=base_image, repo_url=repo_url, commit=commit, cmd_json=cmd_json,
    ))
    return p


def build_thin_image(dockerfile: Path, tag: str, context: Path) -> None:
    r = _run(["docker", "build", "-f", str(dockerfile), "-t", tag, str(context)])
    if r.returncode != 0:
        raise RegenerateError(f"thin image build failed: {tag}")


def image_id(tag: str) -> str | None:
    r = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.strip()


# ---- test execution ----------------------------------------------------------

def run_tests(tag: str, timeout_s: int) -> int:
    r = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", tag],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=timeout_s,
    )
    return r.returncode


# ---- entry update ------------------------------------------------------------

def append_verified_on(entry: dict, record: dict) -> None:
    entry["reproduction"]["verifiedOn"].append(record)


def detect_platform() -> str:
    """Docker-style platform string for this host."""
    mach = host_platform.machine().lower()
    arch = {
        "x86_64": "amd64", "amd64": "amd64",
        "aarch64": "arm64", "arm64": "arm64",
    }.get(mach, mach)
    sys_name = host_platform.system().lower()
    return f"{sys_name}/{arch}"


# ---- main flow ---------------------------------------------------------------

def regenerate(entry_path: Path, *, build_missing_bases: bool, skip_tests: bool,
               host_label: str | None, timeout_s: int, builder: str) -> int:
    with entry_path.open() as f:
        entry = json.load(f)

    try:
        validate_entry(entry)
    except SchemaError as e:
        print(f"ERROR: entry {entry_path} does not validate: {e}", file=sys.stderr)
        return EXIT_FINGERPRINT_MISMATCH

    if entry.get("category") == "unreproducible" or not entry.get("reproduction"):
        print("entry has no reproduction; nothing to do", file=sys.stderr)
        return EXIT_OK

    repro = entry["reproduction"]
    fat = repro["fatImage"]
    tag = fat_image_tag(fat["rustVersion"], fat["debianRelease"], fat["aptSnapshot"])
    print(f"entry: {entry['id']}  fat image: {tag}", file=sys.stderr)

    # --- 1. Resolve fat image ---
    if not fat_image_exists(tag):
        if not build_missing_bases:
            print(
                f"ERROR: fat image {tag} not present locally.\n"
                f"Rebuild with:\n"
                f"  SOURCE_DATE_EPOCH={fat['sourceDateEpoch']} docker buildx build \\\n"
                f"    --build-arg RUST_VERSION={fat['rustVersion']} \\\n"
                f"    --build-arg DEBIAN_RELEASE={fat['debianRelease']} \\\n"
                f"    --build-arg SOURCE_DATE_EPOCH={fat['sourceDateEpoch']} \\\n"
                f"    -t {tag} --load {DOCKERFILE_DIR}\n"
                f"Or re-run with --build-missing-bases.",
                file=sys.stderr,
            )
            return EXIT_FAT_IMAGE_MISSING
        print(f"fat image missing, building...", file=sys.stderr)
        try:
            build_fat_image(tag, fat["rustVersion"], fat["debianRelease"],
                            int(fat["sourceDateEpoch"]), builder=builder)
        except RegenerateError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return EXIT_FAT_IMAGE_MISSING

    actual_fat_digest = fat_image_digest(tag)
    expected_fat_digest = fat.get("expectedDigest")
    fat_digest_match: bool | None
    if expected_fat_digest is None:
        fat_digest_match = None
        print(f"  fat digest: {actual_fat_digest} (no expected recorded)", file=sys.stderr)
    else:
        fat_digest_match = (actual_fat_digest == expected_fat_digest)
        tag_str = "match" if fat_digest_match else "MISMATCH (advisory)"
        print(f"  fat digest: {actual_fat_digest}  vs expected {expected_fat_digest}  [{tag_str}]", file=sys.stderr)

    # --- 2. Fingerprint ---
    with tempfile.TemporaryDirectory() as td:
        manifest_dir = Path(td)
        extract_manifest(tag, manifest_dir)
        actual_digest, actual_files = compute_fingerprint(manifest_dir)

    expected_fp = repro["environmentFingerprint"]
    fp_match = (actual_digest == expected_fp["digest"])
    print(f"  fingerprint: {actual_digest}", file=sys.stderr)
    print(f"  expected:    {expected_fp['digest']}  [{'match' if fp_match else 'MISMATCH'}]", file=sys.stderr)

    if not fp_match:
        print("  per-file diff:", file=sys.stderr)
        for line in diff_fingerprint(expected_fp, actual_files):
            print(line, file=sys.stderr)
        print("  environment fingerprint mismatch — refusing to proceed.", file=sys.stderr)
        return EXIT_FINGERPRINT_MISMATCH

    # --- 3 & 4. Thin images + tests ---
    repo_url = entry["project"]["url"]
    if not repo_url.endswith(".git"):
        repo_url = repo_url + ".git"
    pre_commit = entry["commits"]["pre"]
    post_commit = entry["commits"]["post"]
    fix_commit = entry["commits"].get("fix")
    build_flags = repro["buildFlags"]

    short = post_commit[:8]
    pre_tag = f"rp2026/cargo-thin:{short}-pre"
    post_tag = f"rp2026/cargo-thin:{short}-post"
    fix_tag = f"rp2026/cargo-thin:{short}-fix" if fix_commit else None

    with tempfile.TemporaryDirectory() as td:
        ctx = Path(td)
        pre_df = _write_thin_dockerfile(ctx, repo_url, pre_commit, tag, build_flags)
        post_df = _write_thin_dockerfile(ctx, repo_url, post_commit, tag, build_flags)
        fix_df = _write_thin_dockerfile(ctx, repo_url, fix_commit, tag, build_flags) if fix_commit else None

        try:
            build_thin_image(pre_df, pre_tag, ctx)
            build_thin_image(post_df, post_tag, ctx)
            if fix_df:
                build_thin_image(fix_df, fix_tag, ctx)
        except RegenerateError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return EXIT_THIN_BUILD_FAILED

    outcome_match: bool | None = None
    if not skip_tests:
        print("  running pre test...", file=sys.stderr)
        pre_rc = run_tests(pre_tag, timeout_s)
        print(f"  pre exit: {pre_rc}", file=sys.stderr)
        print("  running post test...", file=sys.stderr)
        post_rc = run_tests(post_tag, timeout_s)
        print(f"  post exit: {post_rc}", file=sys.stderr)
        fix_rc: int | None = None
        if fix_tag:
            print("  running fix test...", file=sys.stderr)
            fix_rc = run_tests(fix_tag, timeout_s)
            print(f"  fix exit: {fix_rc}", file=sys.stderr)

        # Expected pass/fail pattern per category:
        #   breaking:         pre pass, post fail
        #   non-breaking:     pre pass, post pass
        #   fix-after-update: pre pass, post fail, fix pass
        cat = entry["category"]
        pre_ok = (pre_rc == 0)
        post_ok = (post_rc == 0)
        fix_ok = (fix_rc == 0) if fix_rc is not None else None
        if cat == "breaking":
            reproduced = pre_ok and not post_ok
        elif cat == "non-breaking":
            reproduced = pre_ok and post_ok
        elif cat == "fix-after-update":
            reproduced = pre_ok and not post_ok and fix_ok is True
        else:
            reproduced = False
        outcome_match = reproduced
        tag_str = "match" if outcome_match else "MISMATCH"
        print(f"  outcome: category={cat}, reproduced={reproduced} [{tag_str}]", file=sys.stderr)

    # --- 5. Append verifiedOn + write ---
    record = {
        "platform": detect_platform(),
        "host": host_label,
        "verifiedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fingerprintMatch": True,
        "fatImageDigestMatch": fat_digest_match,
        "outcomeMatch": outcome_match,
    }
    append_verified_on(entry, record)

    # Re-validate before writing to catch any accidental drift.
    try:
        validate_entry(entry)
    except SchemaError as e:
        print(f"ERROR: updated entry no longer validates: {e}", file=sys.stderr)
        return EXIT_FINGERPRINT_MISMATCH

    with entry_path.open("w") as f:
        json.dump(entry, f, indent=2)
        f.write("\n")
    print(f"updated {entry_path} (verifiedOn now has {len(entry['reproduction']['verifiedOn'])} record(s))", file=sys.stderr)

    if outcome_match is False:
        return EXIT_OUTCOME_MISMATCH
    return EXIT_OK


def main() -> int:
    p = argparse.ArgumentParser(description="Regenerate a v0.0.4 Cargo entry and verify reproducibility.")
    p.add_argument("--entry", required=True, type=Path, help="Path to entry JSON (v0.0.4).")
    p.add_argument("--build-missing-bases", action="store_true",
                   help="If fat image not present locally, build it. Default: fail.")
    p.add_argument("--skip-tests", action="store_true",
                   help="Build thin images but don't run cargo test.")
    p.add_argument("--host", default=None, help="Host label recorded in verifiedOn (e.g. 'macbook-local').")
    p.add_argument("--timeout", type=int, default=1800, help="Test timeout per image (s).")
    p.add_argument("--builder", default="desktop-linux",
                   help="docker buildx builder to use when building the fat image.")
    args = p.parse_args()

    if not shutil.which("docker"):
        print("ERROR: docker not on PATH", file=sys.stderr)
        return 127

    return regenerate(
        args.entry,
        build_missing_bases=args.build_missing_bases,
        skip_tests=args.skip_tests,
        host_label=args.host,
        timeout_s=args.timeout,
        builder=args.builder,
    )


if __name__ == "__main__":
    raise SystemExit(main())

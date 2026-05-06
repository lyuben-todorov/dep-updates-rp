"""Glue step — combine mining, reproduction, classification into a v0.0.4 Entry.

Reads a candidate, its reproduction result, and the classified failure, plus
the fat image that was actually used to reproduce. Extracts the environment
fingerprint from the fat image, builds a schema-valid <id>.json, and writes
it under data/cargo/.

Schema shape notes:
- No registry URL. Entries do not carry image references; the regenerator
  rebuilds thin images on demand from the `fatImage` inputs.
- `reproduction.fatImage` records the regenerator's inputs (rustVersion,
  SDE, apt snapshot, Debian release).
- `reproduction.environmentFingerprint` is extracted from /manifest/* inside
  the fat image at assembly time. This is the reproducibility contract.
- `reproduction.verifiedOn` starts empty; entries earn verification records
  each time `cargo_regenerate.py` runs and matches the fingerprint.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
from bump_ext import (  # noqa: E402
    Commits,
    Ecosystem,
    Entry,
    EntryWriter,
    EnvironmentFingerprint,
    FatImage,
    Failure,
    FingerprintFile,
    PR,
    Project,
    Reproduction,
    SCHEMA_VERSION,
    TopFailureCategory,
    Update,
    UpdateCategory,
    VersionUpdateType,
)

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

# Canonical concat order — must match cargo_regenerate.py's FINGERPRINT_FILES.
FINGERPRINT_FILES = ["packages.txt", "rustc.txt", "cargo.txt", "os-release", "sources.list"]

# Parses the snapshot timestamp out of a sources.list line like
# `deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260415T000000Z bookworm main`.
SNAPSHOT_RE = re.compile(r"/archive/debian/(\d{8}T\d{6}Z)\s")
OS_RELEASE_CODENAME_RE = re.compile(r'^VERSION_CODENAME=(\w+)', re.MULTILINE)
RUSTC_VERSION_RE = re.compile(r"^rustc (\d+\.\d+\.\d+)")


class AssembleError(Exception):
    pass


def classify_version_bump(prev: str, new: str) -> VersionUpdateType:
    p = SEMVER_RE.match(prev)
    n = SEMVER_RE.match(new)
    if not p or not n:
        return VersionUpdateType.other
    if p.group(1) != n.group(1):
        return VersionUpdateType.major
    if p.group(2) != n.group(2):
        return VersionUpdateType.minor
    if p.group(3) != n.group(3):
        return VersionUpdateType.patch
    return VersionUpdateType.other


# ---- fat image introspection -------------------------------------------------

def extract_manifest(tag: str, dest: Path) -> None:
    """Copy /manifest/* from the fat image onto the host."""
    dest.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{dest}:/out", tag,
         "sh", "-c", "cp /manifest/* /out/"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if r.returncode != 0:
        raise AssembleError(f"failed to extract /manifest/ from {tag}: {r.stderr}")


def fat_image_digest(tag: str) -> str | None:
    r = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.strip()


def derive_fat_image_fields(manifest_dir: Path) -> tuple[str, str, str]:
    """Returns (rust_version, apt_snapshot, debian_release) parsed from /manifest/*.

    rustVersion from rustc.txt, aptSnapshot from sources.list, debianRelease
    from os-release.
    """
    rustc_txt = (manifest_dir / "rustc.txt").read_text()
    m = RUSTC_VERSION_RE.search(rustc_txt)
    if not m:
        raise AssembleError(f"could not parse rustc version from rustc.txt:\n{rustc_txt}")
    rust_version = m.group(1)

    sources = (manifest_dir / "sources.list").read_text()
    m = SNAPSHOT_RE.search(sources)
    if not m:
        raise AssembleError(f"could not parse apt snapshot from sources.list:\n{sources}")
    apt_snapshot = m.group(1)

    os_release = (manifest_dir / "os-release").read_text()
    m = OS_RELEASE_CODENAME_RE.search(os_release)
    if not m:
        raise AssembleError(f"could not parse VERSION_CODENAME from os-release:\n{os_release}")
    debian_release = m.group(1)

    return rust_version, apt_snapshot, debian_release


def compute_fingerprint(manifest_dir: Path) -> tuple[str, list[FingerprintFile], str, int]:
    """Return (digest, files, rustc_first_line, package_count)."""
    concat = b""
    files = []
    for name in FINGERPRINT_FILES:
        data = (manifest_dir / name).read_bytes()
        concat += data
        files.append(FingerprintFile(
            path=f"/manifest/{name}",
            sha256=hashlib.sha256(data).hexdigest(),
            bytes=len(data),
        ))
    digest = "sha256:" + hashlib.sha256(concat).hexdigest()
    rustc_first = (manifest_dir / "rustc.txt").read_text().splitlines()[0]
    pkg_count = len((manifest_dir / "packages.txt").read_text().splitlines())
    return digest, files, rustc_first, pkg_count


# ---- entry construction ------------------------------------------------------

def build_entry(
    candidate: dict,
    reproduction: dict | None,
    classification: dict | None,
    *,
    category: str | UpdateCategory,
    fat_image_tag: str,
    source_date_epoch: int,
    build_flags: list[str],
    record_fat_digest: bool,
) -> Entry:
    """Build a v0.0.4 Entry.

    `category` must be supplied by the caller — it's decided at
    reproduction-result time, not at candidate time. The reproducer's
    raw exit codes + the category together determine whether the entry
    gets a `reproduction` block or is marked `unreproducible`.
    """
    org, name = candidate["repo"].split("/")
    short = candidate["post_commit"][:8]
    entry_id = f"cargo-{short}"

    if isinstance(category, str):
        category_enum = UpdateCategory(category)
    else:
        category_enum = category

    has_reproduction_block = category_enum != UpdateCategory.unreproducible

    repro_obj = None
    if has_reproduction_block:
        with tempfile.TemporaryDirectory() as td:
            manifest_dir = Path(td)
            extract_manifest(fat_image_tag, manifest_dir)
            rust_version, apt_snapshot, debian_release = derive_fat_image_fields(manifest_dir)
            digest, files, rustc_first, pkg_count = compute_fingerprint(manifest_dir)

        expected_digest = fat_image_digest(fat_image_tag) if record_fat_digest else None

        repro_obj = Reproduction(
            fatImage=FatImage(
                rustVersion=rust_version,
                sourceDateEpoch=source_date_epoch,
                aptSnapshot=apt_snapshot,
                debianRelease=debian_release,
                expectedDigest=expected_digest,
            ),
            buildFlags=build_flags,
            environmentFingerprint=EnvironmentFingerprint(
                digest=digest,
                files=files,
                rustcVersion=rustc_first,
                packageCount=pkg_count,
            ),
            thinImages=None,
            verifiedOn=[],
        )

    # Failure block only makes sense for entries that *have* a failing
    # commit — breaking and fix-after-update. Non-breaking entries skip it.
    fail_obj = None
    if classification and category_enum in (
        UpdateCategory.breaking, UpdateCategory.fix_after_update,
    ):
        fail_obj = Failure(
            topCategory=TopFailureCategory(classification["topCategory"]),
            subCategory=classification.get("subCategory"),
            errorCodes=classification.get("errorCodes", []),
        )

    return Entry(
        id=entry_id,
        schemaVersion=SCHEMA_VERSION,
        ecosystem=Ecosystem.cargo,
        category=category_enum,
        project=Project(
            url=f"https://github.com/{candidate['repo']}",
            organisation=org,
            name=name,
        ),
        pr=PR(
            url=candidate["pr_url"],
            number=candidate["pr_number"],
            author=candidate["pr_author"],
            authorType="bot" if candidate.get("bot_type") else "human",
            botType=candidate.get("bot_type"),
            merged=candidate.get("merged"),
        ),
        commits=Commits(
            pre=candidate["pre_commit"],
            post=candidate["post_commit"],
            fix=candidate.get("fix_commit"),
        ),
        update=Update(
            dependencyName=candidate["dependency_name"],
            previousVersion=candidate["previous_version"],
            newVersion=candidate["new_version"],
            versionUpdateType=classify_version_bump(
                candidate["previous_version"], candidate["new_version"]
            ),
            scope="runtime",
        ),
        reproduction=repro_obj,
        failure=fail_obj,
        ecosystemMetadata={},
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Assemble a v0.0.4 Cargo entry JSON.")
    p.add_argument("--candidate", required=True)
    p.add_argument("--reproduction", required=False)
    p.add_argument("--classification", required=False)
    p.add_argument("--category", required=True,
                   choices=["breaking", "non-breaking", "fix-after-update", "unreproducible"],
                   help="Entry category. Normally decided by the driver from reproducer exit codes.")

    fat_group = p.add_mutually_exclusive_group(required=True)
    fat_group.add_argument("--fat-image",
                           help="Fat-image tag that was used to reproduce, e.g. rp2026/cargo-fat:1.92.0-bookworm-20260415")
    fat_group.add_argument("--fat-image-auto", action="store_true",
                           help="Derive the fat-image tag via the canonical bucketing API "
                                "(fat_image.bucket_for + canonical_sde_for + tag_for). Requires "
                                "--rust-msrv and --commit-date.")
    p.add_argument("--rust-msrv", default=None,
                   help="Only with --fat-image-auto. E.g. '1.70'. Pass empty string to let "
                        "bucket_for fall back to latest-milestone-before-commit.")
    p.add_argument("--commit-date", default=None,
                   help="Only with --fat-image-auto. Post-commit date (YYYY-MM-DD).")
    p.add_argument("--debian-release", default=None,
                   help="Only with --fat-image-auto. Override the debian release that "
                        "cargo_toolchain.debian_release_for would pick for the commit date.")
    p.add_argument("--max-sde-date", type=lambda s: dt.date.fromisoformat(s), default=None,
                   help="Only with --fat-image-auto. Passed to canonical_sde_for for "
                        "API symmetry. Default: fat_image.default_max_sde_date().")

    p.add_argument("--source-date-epoch", type=int, default=None,
                   help="SOURCE_DATE_EPOCH the fat image was built with. Required with "
                        "--fat-image; with --fat-image-auto, derived from canonical_sde_for.")
    p.add_argument("--build-flags", default="--locked,--offline",
                   help="Comma-separated cargo test flags recorded in the entry.")
    p.add_argument("--record-fat-digest", action="store_true",
                   help="Advisory only: record this host's fat-image OCI digest as "
                        "fatImage.expectedDigest. Usually a bad idea on first capture — "
                        "apt layers jitter across hosts (see docs/reproducible-builds.md).")
    p.add_argument("--out-dir", default="./data/cargo")
    args = p.parse_args()

    # Resolve --fat-image-auto → canonical tag + SDE.
    if args.fat_image_auto:
        from . import fat_image as _fat
        from . import cargo_toolchain as _toolchain
        if args.commit_date is None:
            print("ERROR: --fat-image-auto requires --commit-date", file=sys.stderr)
            return 2
        try:
            date = dt.date.fromisoformat(args.commit_date)
        except ValueError as e:
            print(f"ERROR: --commit-date must be YYYY-MM-DD ({e})", file=sys.stderr)
            return 2
        debian = args.debian_release or _toolchain.debian_release_for(date)
        msrv = args.rust_msrv if args.rust_msrv else None
        bucket = _fat.bucket_for(msrv, date, debian)
        if bucket is None:
            print(f"ERROR: no supported fat image for (msrv={msrv!r}, commit_date={date}, "
                  f"debian={debian}). See MILESTONE_DEBIAN_SUPPORTED.", file=sys.stderr)
            return 2
        max_sde_date = args.max_sde_date or _fat.default_max_sde_date()
        sde_info = _fat.canonical_sde_for(bucket, max_sde_date=max_sde_date)
        args.fat_image = _fat.tag_for(bucket, sde_info.sde)
        if args.source_date_epoch is None:
            args.source_date_epoch = sde_info.sde
        print(f"auto-resolved fat image: {args.fat_image} "
              f"(bucket={bucket.milestone}/{bucket.year}/{bucket.debian}, "
              f"sde={sde_info.sde_date})", file=sys.stderr)

    if args.source_date_epoch is None:
        print("ERROR: --source-date-epoch is required when --fat-image is specified explicitly",
              file=sys.stderr)
        return 2

    candidate = json.loads(Path(args.candidate).read_text())
    reproduction = (
        json.loads(Path(args.reproduction).read_text()) if args.reproduction else None
    )
    classification = (
        json.loads(Path(args.classification).read_text()) if args.classification else None
    )

    build_flags = [f.strip() for f in args.build_flags.split(",") if f.strip()]
    if not build_flags:
        print("ERROR: --build-flags cannot be empty", file=sys.stderr)
        return 2

    try:
        entry = build_entry(
            candidate, reproduction, classification,
            category=args.category,
            fat_image_tag=args.fat_image,
            source_date_epoch=args.source_date_epoch,
            build_flags=build_flags,
            record_fat_digest=args.record_fat_digest,
        )
    except AssembleError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    out = EntryWriter(args.out_dir).write(entry)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

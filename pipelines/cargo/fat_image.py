"""Fat toolchain image inventory + resolver.

The "fat image" is the shared apt + rustc base on which every per-entry thin
image is built. Entries refer to a fat image by its reproduction inputs
(rustVersion, sourceDateEpoch, aptSnapshot, debianRelease) plus the
environment fingerprint a consumer can verify against.

This module owns the index at ``docker/cargo-fat/index.json`` — a manifest
of fat images we've built and stood behind. Entries are added here after a
build succeeds and its fingerprint has been captured.

Responsibilities:

- Read / write the index JSON.
- Resolve "which fat image should I use for this entry?" from its Rust MSRV
  + breaking-commit date. Policy is intentionally dumb on day 1; we'll turn
  it into a bin-packing problem after we have ~50 entries of data.
- Build a new fat image via buildx and register it in the index.
- ``list`` / ``resolve`` / ``build`` / ``register`` CLI for humans.

Non-goals (deliberate):

- The regenerator does NOT consult this index. It reads the entry's
  ``reproduction.fatImage`` block directly. The index is for assembly and
  planning, not verification.
- No automatic rebuild when the index says "you need X" — building a 3 GB
  image with apt work in it is a user-visible operation, always opt-in.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


INDEX_PATH = Path(__file__).resolve().parents[2] / "docker" / "cargo-fat" / "index.json"
DOCKERFILE_DIR = Path(__file__).resolve().parents[2] / "docker" / "cargo-fat"
CACHE_DIR = Path.home() / ".cache" / "rp2026"

FINGERPRINT_FILES = ["packages.txt", "rustc.txt", "cargo.txt", "os-release", "sources.list"]
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")
SNAPSHOT_RE = re.compile(r"^(\d{8})T\d{6}Z$")

# Rust milestones we pick fat images at. MSRVs get rounded *up* to the
# smallest milestone ≥ MSRV — this is what makes (1.49, 2020) and (1.56, 2020)
# buckets share an image.
MILESTONES = ["1.49", "1.56", "1.65", "1.75", "1.85", "1.92"]

# Upstream release dates, authoritative from rust-lang/rust/RELEASES.md (verified
# 2026-05-05). When MILESTONES grows, re-fetch:
#   curl -sfL https://raw.githubusercontent.com/rust-lang/rust/master/RELEASES.md \
#     | grep -E "^Version 1\.(...)\\.0"
MILESTONE_RELEASE_DATES: dict[str, dt.date] = {
    "1.49": dt.date(2020, 12, 31),
    "1.56": dt.date(2021, 10, 21),
    "1.65": dt.date(2022, 11,  3),
    "1.75": dt.date(2023, 12, 28),
    "1.85": dt.date(2025,  2, 20),
    "1.92": dt.date(2025, 12, 11),
}

# Which (milestone, debian) combinations Docker Hub actually publishes as
# `rust:<milestone>.0-<debian>`. Probed 2026-05-05 via the API; regenerate by
# running `scripts/probe_rust_images.py` (or ad-hoc via
# `_rust_base_published_cached(patch, debian) is not None`).
#
# Why hardcoded: avoids a network call at bucketize time, and the grid
# changes only when Docker Hub adds a track (rarely).
MILESTONE_DEBIAN_SUPPORTED: set[tuple[str, str]] = {
    ("1.49", "buster"),
    ("1.56", "buster"), ("1.56", "bullseye"),
    ("1.65", "buster"), ("1.65", "bullseye"),
    ("1.75", "buster"), ("1.75", "bullseye"), ("1.75", "bookworm"),
    ("1.85", "bullseye"), ("1.85", "bookworm"),
    ("1.92", "bullseye"), ("1.92", "bookworm"), ("1.92", "trixie"),
}

def default_max_sde_date(today: dt.date | None = None) -> dt.date:
    """Default upper bound on acceptable commit dates and SDEs.

    Dec 31 of (today.year - 1) — last completed New Year's Eve. Using the
    previous year's end means every bucket we process is a *finished* year,
    so `canonical_sde_for` can always use `Dec 31 of bucket.year` without
    asking for future snapshots.

    Callers should pass an explicit `max_sde_date` when running the pipeline
    as a research run. This helper exists for scripts that want a sensible
    default when no run-level parameter is threaded in.
    """
    if today is None:
        today = dt.date.today()
    return dt.date(today.year - 1, 12, 31)


# ---- data model --------------------------------------------------------------

@dataclass(frozen=True)
class FatImageRecord:
    tag: str
    rustVersion: str            # full patch version, e.g. "1.92.0"
    sourceDateEpoch: int
    aptSnapshot: str            # "YYYYMMDDTHHMMSSZ"
    debianRelease: str
    environmentFingerprint: str # "sha256:..."
    packageCount: int | None = None
    firstSeenAt: str | None = None  # YYYY-MM-DD
    notes: str | None = None

    def snapshot_date(self) -> dt.date:
        m = SNAPSHOT_RE.match(self.aptSnapshot)
        if not m:
            raise ValueError(f"invalid aptSnapshot: {self.aptSnapshot}")
        return dt.datetime.strptime(m.group(1), "%Y%m%d").date()

    def rust_tuple(self) -> tuple[int, int, int]:
        return parse_semver(self.rustVersion)


def parse_semver(v: str) -> tuple[int, int, int]:
    m = SEMVER_RE.match(v)
    if not m:
        raise ValueError(f"not a semver: {v!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


# ---- bucket / canonical-SDE primitives --------------------------------------
# Post-refactor: each candidate maps to exactly one (milestone, year, debian)
# BucketKey. Each BucketKey has exactly one canonical SDE, deterministic. Each
# (BucketKey, SDE) produces exactly one tag. Reuse is pure tag-equality: if
# the index has that tag, reuse it; else build it.

_MILESTONE_TUPLES = [parse_semver(m) for m in MILESTONES]


@dataclass(frozen=True)
class BucketKey:
    milestone: str    # one of MILESTONES
    year: int
    debian: str       # buster / bullseye / bookworm / trixie

    def rust_patch(self) -> str:
        """'1.56' -> '1.56.0'. Always the .0 patch of the milestone."""
        return f"{self.milestone}.0"


def round_up_to_milestone(msrv: str) -> str | None:
    """'1.50' -> '1.56'. Returns None if MSRV exceeds the largest milestone."""
    try:
        t = parse_semver(msrv)
    except ValueError:
        return None
    for i, mt in enumerate(_MILESTONE_TUPLES):
        if mt >= t:
            return MILESTONES[i]
    return None


def latest_milestone_before(commit_date: dt.date) -> str:
    """Largest milestone whose release date is ≤ commit_date.

    Used by `bucket_for` as the fallback when a candidate has no
    declared MSRV — picks the rust the PR author was plausibly
    targeting rather than a language-edition floor that understates
    what the code actually needs.

    For commit dates before any milestone's release (pre-2020 PRs),
    returns the smallest milestone in MILESTONES. Those rows will
    carry pre_rust_base anyway once canonical_sde_for runs.
    """
    below = [m for m in MILESTONES if MILESTONE_RELEASE_DATES[m] <= commit_date]
    if not below:
        return MILESTONES[0]
    return max(below, key=parse_semver)


def _reroute_to_supported(milestone: str, debian: str) -> str | None:
    """Return the smallest milestone ≥ `milestone` that Docker Hub actually
    publishes for `debian`. None if no upward bump is available.

    Docker Hub doesn't publish every (rust, debian) pair — pre-1.54 has no
    bullseye tag, post-1.75 has no buster tag, etc. When the original
    (milestone, debian) isn't supported we try to keep the debian (which
    tracks the commit's OS era) and bump milestone up (rust is backward-
    compatible; a newer rustc compiles code written for an older one).
    Bumping debian instead would place the code against a wrong-era OS,
    which is a worse lie.
    """
    if (milestone, debian) in MILESTONE_DEBIAN_SUPPORTED:
        return milestone
    target = parse_semver(milestone)
    for m in MILESTONES:
        if parse_semver(m) < target:
            continue
        if (m, debian) in MILESTONE_DEBIAN_SUPPORTED:
            return m
    return None


def bucket_for(rust_msrv: str | None, commit_date: dt.date, debian: str) -> BucketKey | None:
    """Map candidate properties to a BucketKey. None if no available fat
    image can serve this (milestone, debian) pair.

    Steps:
      1. Pick the initial milestone: round_up_to_milestone(rust_msrv)
         when MSRV is known, else latest_milestone_before(commit_date).
      2. If (milestone, debian) isn't a published rust:<patch>-<debian>
         tag on Docker Hub, reroute upward to the smallest supported
         milestone on that debian. This keeps the OS era intact and
         bumps rust (backward-compatible).
      3. Return None if rerouting finds no viable milestone — that
         debian never hosted a supported rustc on this track.
    """
    if rust_msrv is None:
        milestone: str | None = latest_milestone_before(commit_date)
    else:
        milestone = round_up_to_milestone(rust_msrv)
    if milestone is None:
        return None
    milestone = _reroute_to_supported(milestone, debian)
    if milestone is None:
        return None
    return BucketKey(milestone=milestone, year=commit_date.year, debian=debian)


def _rust_base_published_cached(rust_patch: str, debian: str) -> dt.date | None:
    """Docker Hub lookup with a ~24h file-cache. Returns None on failure."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / "rust-base-pub.json"
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except Exception:
            cache = {}

    key = f"{rust_patch}-{debian}"
    cached = cache.get(key)
    if cached:
        fetched_ts = cached.get("fetched")
        pub = cached.get("pub")
        if fetched_ts and pub and (dt.datetime.now(dt.timezone.utc).timestamp() - fetched_ts < 86400):
            try:
                return dt.date.fromisoformat(pub)
            except ValueError:
                pass

    try:
        import requests
        r = requests.get(
            f"https://hub.docker.com/v2/repositories/library/rust/tags/{rust_patch}-{debian}",
            timeout=20,
        )
    except Exception:
        return None
    if r.status_code != 200:
        return None
    iso = r.json().get("last_updated")
    if not iso:
        return None
    pub = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).date()

    cache[key] = {"fetched": dt.datetime.now(dt.timezone.utc).timestamp(), "pub": pub.isoformat()}
    try:
        cache_path.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass
    return pub


@dataclass(frozen=True)
class CanonicalSde:
    sde: int
    sde_date: dt.date
    pre_rust_base: bool        # bucket's year ends before rust base image was published
    rust_base_unknown: bool    # Docker Hub didn't tell us when; caller should inspect


def canonical_sde_for(
    bucket: BucketKey,
    *,
    max_sde_date: dt.date | None = None,
) -> CanonicalSde:
    """Pure function: (BucketKey) → the canonical SDE for the bucket's image.

    Rule:
        target = December 31 of bucket.year
        lower  = rust:<patch>-<debian>'s Docker Hub publication date
                 (None if unreachable)

        SDE_date = max(target, lower)

    There is **no upper clamp**. The caller is expected to reject
    candidates whose commit_date is later than `max_sde_date` before
    bucketizing (see `bucketize` in `cargo_plan_fat_images`), so every
    bucket we see here has a year whose Dec 31 is already in the past.

    `max_sde_date` is accepted as a keyword argument for API symmetry
    with the rest of the pipeline — the planner and driver carry it as
    a run-level parameter. This function does not consult it directly;
    the upper bound is enforced upstream.

    Flags:
      pre_rust_base     — bucket year ended before the Rust base image
                          existed on Docker Hub; SDE is lifted forward
                          to rust_pub. Known OS-era drift, recorded for
                          the paper.
      rust_base_unknown — Docker Hub didn't tell us when the base image
                          was published. Build may fail at apt-time if
                          target predates the base image. The
                          `_reroute_to_supported` pre-pass in
                          `bucket_for` should normally prevent this
                          state.

    Determinism: for any `(bucket,)` the output is fixed forever. This
    function no longer reads `dt.date.today()`.
    """
    # max_sde_date is threaded through for future use but currently not
    # read here — `bucketize` enforces the upper bound upstream.
    del max_sde_date

    rust_patch = bucket.rust_patch()
    rust_pub = _rust_base_published_cached(rust_patch, bucket.debian)
    rust_base_unknown = rust_pub is None

    target = dt.date(bucket.year, 12, 31)

    if rust_pub is not None and target < rust_pub:
        sde_date = rust_pub
        pre_rust_base = True
    else:
        sde_date = target
        pre_rust_base = False

    sde = int(dt.datetime.combine(sde_date, dt.time(0, 0), tzinfo=dt.timezone.utc).timestamp())
    return CanonicalSde(
        sde=sde,
        sde_date=sde_date,
        pre_rust_base=pre_rust_base,
        rust_base_unknown=rust_base_unknown,
    )


def tag_for(bucket: BucketKey, sde: int) -> str:
    """Canonical tag: rp2026/cargo-fat:<milestone>.0-<debian>-<yyyymmdd>."""
    return fat_image_tag(bucket.rust_patch(), bucket.debian, sde)


# ---- index I/O ---------------------------------------------------------------

class IndexError(Exception):
    pass


def load_index(path: Path = INDEX_PATH) -> list[FatImageRecord]:
    if not path.exists():
        return []
    with path.open() as f:
        raw = json.load(f)
    records: list[FatImageRecord] = []
    for item in raw.get("fatImages", []):
        records.append(FatImageRecord(**item))
    return records


def save_index(records: Iterable[FatImageRecord], path: Path = INDEX_PATH) -> None:
    existing = {}
    if path.exists():
        with path.open() as f:
            existing = json.load(f)
    out = {
        "schemaVersion": existing.get("schemaVersion", "0.1.0"),
        "description": existing.get("description", ""),
        "fatImages": [
            {k: v for k, v in dataclasses.asdict(r).items() if v is not None}
            for r in records
        ],
    }
    with path.open("w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")


def register(record: FatImageRecord, path: Path = INDEX_PATH) -> None:
    """Append a new record, rejecting tag collisions."""
    records = load_index(path)
    for r in records:
        if r.tag == record.tag:
            raise IndexError(f"tag already registered: {record.tag}")
    records.append(record)
    save_index(records, path)


# ---- build helper ------------------------------------------------------------

def fat_image_tag(rust_version: str, debian_release: str, sde: int) -> str:
    """Canonical fat-image tag: rp2026/cargo-fat:<rust>-<debian>-<yyyymmdd>."""
    yyyymmdd = dt.datetime.fromtimestamp(sde, tz=dt.timezone.utc).strftime("%Y%m%d")
    return f"rp2026/cargo-fat:{rust_version}-{debian_release}-{yyyymmdd}"


def build_fat_image(rust_version: str, sde: int, *,
                    debian_release: str = "bookworm",
                    include_gui: bool | None = None,
                    builder: str = "desktop-linux",
                    dry_run: bool = False) -> str:
    """Build a fat image and return its resolved tag.

    `include_gui` defaults to True for bookworm+ (modern Tauri era) and
    False for bullseye and older (where libwebkit2gtk-4.1 et al. don't
    exist in apt and pre-Tauri Rust code doesn't need them anyway).
    Override with an explicit bool if you know better.
    """
    if include_gui is None:
        include_gui = debian_release in {"bookworm", "trixie"}

    tag = fat_image_tag(rust_version, debian_release, sde)

    cmd = [
        "env", f"SOURCE_DATE_EPOCH={sde}",
        "docker", "buildx", "build",
        "--builder", builder,
        "--build-arg", f"RUST_VERSION={rust_version}",
        "--build-arg", f"DEBIAN_RELEASE={debian_release}",
        "--build-arg", f"SOURCE_DATE_EPOCH={sde}",
        "--build-arg", f"INCLUDE_GUI={'1' if include_gui else '0'}",
        "-t", tag,
        "--load",
        str(DOCKERFILE_DIR),
    ]
    print(f"  $ {' '.join(cmd)}", file=sys.stderr)
    if dry_run:
        return tag
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise IndexError(f"fat image build failed for {tag}")
    return tag


def introspect_fat_image(tag: str) -> FatImageRecord:
    """Extract /manifest/* from a built fat image and synthesize a FatImageRecord.

    Leaves firstSeenAt unset (caller stamps it). Caller also supplies SDE,
    since it's not recoverable from the image alone.
    """
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{out}:/out", tag,
             "sh", "-c", "cp /manifest/* /out/"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        if r.returncode != 0:
            raise IndexError(f"failed to extract /manifest/ from {tag}: {r.stderr}")

        # Snapshot + debian release + rust version — derive from the manifest.
        sources = (out / "sources.list").read_text()
        m = re.search(r"/archive/debian/(\d{8}T\d{6}Z)\s", sources)
        if not m:
            raise IndexError(f"could not parse snapshot URL from sources.list")
        apt_snapshot = m.group(1)

        os_release = (out / "os-release").read_text()
        m = re.search(r'^VERSION_CODENAME=(\w+)', os_release, re.MULTILINE)
        if not m:
            raise IndexError(f"could not parse VERSION_CODENAME from os-release")
        debian_release = m.group(1)

        rustc_txt = (out / "rustc.txt").read_text()
        m = re.search(r"^rustc (\d+\.\d+\.\d+)", rustc_txt)
        if not m:
            raise IndexError(f"could not parse rustc version")
        rust_version = m.group(1)

        concat = b""
        for name in FINGERPRINT_FILES:
            concat += (out / name).read_bytes()
        digest = "sha256:" + hashlib.sha256(concat).hexdigest()
        pkg_count = len((out / "packages.txt").read_text().splitlines())

    # SDE comes from the snapshot string (midnight UTC of that date).
    snap_date = dt.datetime.strptime(apt_snapshot, "%Y%m%dT%H%M%SZ")
    sde = int(snap_date.replace(tzinfo=dt.timezone.utc).timestamp())

    return FatImageRecord(
        tag=tag,
        rustVersion=rust_version,
        sourceDateEpoch=sde,
        aptSnapshot=apt_snapshot,
        debianRelease=debian_release,
        environmentFingerprint=digest,
        packageCount=pkg_count,
    )


# ---- CLI ---------------------------------------------------------------------

def _cli_list(args: argparse.Namespace) -> int:
    records = load_index()
    if not records:
        print("(no fat images registered)", file=sys.stderr)
        return 0
    w_tag = max(len(r.tag) for r in records)
    print(f"{'TAG'.ljust(w_tag)}  RUST       SNAPSHOT           DEBIAN     FINGERPRINT")
    for r in records:
        print(f"{r.tag.ljust(w_tag)}  {r.rustVersion:<9}  {r.aptSnapshot}  {r.debianRelease:<9}  {r.environmentFingerprint[:19]}...")
    return 0


def _cli_resolve(args: argparse.Namespace) -> int:
    """Resolve (msrv, commit_date, debian) → canonical fat-image tag.

    Prints the tag and notes whether it's currently in the index. Uses
    the same canonical path as the planner and driver. Does not mutate
    anything — read-only resolver for diagnostics.
    """
    # Local import: debian_release_for lives in cargo_toolchain, which
    # imports helpers from here — module-level import would cycle.
    from . import cargo_toolchain as _toolchain

    try:
        date = dt.date.fromisoformat(args.commit_date)
    except ValueError as e:
        print(f"ERROR: --commit-date must be YYYY-MM-DD ({e})", file=sys.stderr)
        return 2

    debian = args.debian_release or _toolchain.debian_release_for(date)
    msrv = args.rust_msrv if args.rust_msrv else None

    max_sde_date = args.max_sde_date or default_max_sde_date()
    if date > max_sde_date:
        print(f"ERROR: commit_date {date} > max_sde_date {max_sde_date}. "
              f"Raise --max-sde-date to include this commit.", file=sys.stderr)
        return 2

    bucket = bucket_for(msrv, date, debian)
    if bucket is None:
        print(f"ERROR: no supported fat image for (msrv={msrv!r}, "
              f"commit_date={date}, debian={debian}). "
              f"See MILESTONE_DEBIAN_SUPPORTED.", file=sys.stderr)
        return 2

    sde_info = canonical_sde_for(bucket, max_sde_date=max_sde_date)
    tag = tag_for(bucket, sde_info.sde)

    existing = {r.tag for r in load_index()}
    status = "IN INDEX" if tag in existing else "NOT BUILT"

    flags = []
    if sde_info.pre_rust_base:
        flags.append("pre_rust_base")
    if sde_info.rust_base_unknown:
        flags.append("rust_base_unknown")
    flag_s = f"  [{', '.join(flags)}]" if flags else ""

    print(tag)
    print(f"  bucket={bucket.milestone}/{bucket.year}/{bucket.debian}  "
          f"sde_date={sde_info.sde_date}  {status}{flag_s}", file=sys.stderr)
    return 0


def _cli_build(args: argparse.Namespace) -> int:
    include_gui: bool | None
    if args.include_gui == "auto":
        include_gui = None
    else:
        include_gui = args.include_gui == "1"
    try:
        tag = build_fat_image(args.rust_version, args.source_date_epoch,
                              debian_release=args.debian_release,
                              include_gui=include_gui,
                              builder=args.builder, dry_run=args.dry_run)
    except IndexError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(tag)
        return 0

    # Auto-register unless told not to.
    if not args.no_register:
        record = introspect_fat_image(tag)
        record = dataclasses.replace(record, firstSeenAt=dt.date.today().isoformat())
        try:
            register(record)
            print(f"registered {tag} in {INDEX_PATH}")
        except IndexError as e:
            print(f"WARNING: not registered: {e}", file=sys.stderr)
    print(tag)
    return 0


def _cli_register(args: argparse.Namespace) -> int:
    try:
        record = introspect_fat_image(args.tag)
    except IndexError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    record = dataclasses.replace(record, firstSeenAt=dt.date.today().isoformat(), notes=args.notes)
    try:
        register(record)
    except IndexError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"registered {record.tag}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Fat toolchain image index + resolver.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List registered fat images.")

    pr = sub.add_parser("resolve", help="Print the canonical fat-image tag for a given candidate.")
    pr.add_argument("--rust-msrv", default=None, help="MSRV, e.g. '1.70'. Empty/omit → latest milestone before commit date.")
    pr.add_argument("--commit-date", required=True, help="Post-commit date (YYYY-MM-DD).")
    pr.add_argument("--debian-release", default=None, help="Override debian_release_for(commit_date).")
    pr.add_argument("--max-sde-date", type=lambda s: dt.date.fromisoformat(s), default=None,
                    help="Upper bound on acceptable commit dates. Default: Dec 31 of last year.")

    pb = sub.add_parser("build", help="Build a fat image via buildx and register it.")
    pb.add_argument("--rust-version", required=True, help="Full patch version, e.g. '1.92.0'.")
    pb.add_argument("--debian-release", default="bookworm",
                    help="Debian release for the FROM base; must have a rust:<ver>-<release> tag on Docker Hub.")
    pb.add_argument("--include-gui", choices=["auto", "0", "1"], default="auto",
                    help="Include the GTK/Tauri package stack. 'auto' = yes for bookworm+, no otherwise.")
    pb.add_argument("--source-date-epoch", type=int, required=True, help="SDE used for SNAPSHOT + OCI stamps.")
    pb.add_argument("--builder", default="desktop-linux", help="docker buildx builder name.")
    pb.add_argument("--no-register", action="store_true", help="Skip registering the built image.")
    pb.add_argument("--dry-run", action="store_true", help="Print the command and tag without building.")

    prg = sub.add_parser("register", help="Introspect an already-built image and register it.")
    prg.add_argument("--tag", required=True, help="Image tag to register.")
    prg.add_argument("--notes", default=None, help="Human notes.")

    args = p.parse_args()
    cmds = {"list": _cli_list, "resolve": _cli_resolve, "build": _cli_build, "register": _cli_register}
    return cmds[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())

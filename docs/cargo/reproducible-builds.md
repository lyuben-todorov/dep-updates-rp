# Reproducible builds — findings + current model

What we tried, what we learned, what we shipped. This is the
reproducibility theory for the Cargo pipeline; operational state (what
images are on disk, how to run a batch) lives in
[`running-a-batch.md`](running-a-batch.md) and
[`image-selection.md`](image-selection.md).

## TL;DR

| Question | Answer |
| --- | --- |
| What's the reproducibility claim? | **Environmental equivalence.** Two hosts "agree on the same environment" iff their fat image emits the same sha256 over `/manifest/{packages,rustc,cargo,os-release,sources.list}`. |
| Why not byte-identical OCI digests? | Apt-internal non-determinism (pkgcache.bin ordering, log timestamps, etc.) jitters OCI layers by ~tens of bytes per `RUN` *even with pinned `SOURCE_DATE_EPOCH` + pinned apt snapshot*. Documented below. |
| Is this RB.org canon? | No. RB.org defines reproducibility as bit-for-bit. We consciously relax to environment equivalence because it's what the supervisor actually asked for ("ship a regeneration script") and what survives contact with apt. |
| What's the contract? | Entry JSON records `fatImage.{rustVersion, sourceDateEpoch, aptSnapshot, debianRelease}` + `environmentFingerprints[]` — one entry per container platform (`linux/arm64`, `linux/amd64`, ...), each with `{platform, digest, files[]}`. Regenerator rebuilds, recomputes fingerprint, looks up the expected digest by this host's container platform, asserts equality. Mismatch for a recorded platform is a hard fail; a container platform not yet in the list is appended on first run (schema v0.0.5). |
| Is the OCI digest recorded at all? | Yes, as `fatImage.expectedDigest`. Advisory only — mismatch logged as a warning for cross-host drift analysis, not a fail. |

## The pivot (2026-05-03)

Original plan: make fat images byte-identical across rebuilds, record
the digest in the entry, treat `docker pull <digest>` as ground truth.

Reality: after patching the Dockerfile for every RB.org-recommended
practice (pinned `RUST_VERSION` patch, `repro-sources-list.sh`, SDE
passthrough, `rewrite-timestamp=true` on `docker-container` driver),
**double-building the same Dockerfile still produced different OCI
digests**. The difference is real: compressed layer sizes varied by
9–41 bytes per apt-install RUN.

With all the right flags, the base-image layers (1–5) match exactly and
OCI manifest annotations (`image.created`) are stamped to SDE. The apt
layers still jitter. The fat image is "mostly" reproducible but not
bit-for-bit.

Two forks:

- **Fork A** (chase byte-identical) — 2–4 more days of apt-layer
  whack-a-mole, likely succeeds on same-arch, likely fails cross-arch,
  doesn't propagate to per-entry thin images anyway.
- **Fork B** (pivot to environmental equivalence) — accept the jitter
  as scientific observation, switch the reproducibility contract to a
  fingerprint over the apt package set + rustc + cargo + os-release +
  sources.list.

We picked Fork B. Fork A remains as a sub-investigation for the paper
("here's the residual O(100) bytes per RUN and why").

## Evidence — the double-build experiment

### Setup (2026-05-03)

- Host: darwin arm64 (Apple silicon), Docker Desktop 27.5.1,
  buildx v0.20.1-desktop.2, BuildKit v0.18.2.
- Dockerfile: `docker/cargo-fat/Dockerfile` with pinned
  `RUST_VERSION=1.92.0`, `DEBIAN_RELEASE=bookworm`, `SOURCE_DATE_EPOCH`
  build-arg, vendored `repro-sources-list.sh`.
- Platform: `linux/arm64` native (avoided amd64/QEMU for this first
  pass).

### Finding 0 — apt snapshot date must be ≥ base-image publication date

First attempt used `SOURCE_DATE_EPOCH=2025-01-01 UTC`, before the
`rust:1.92.0-bookworm` image existed on Docker Hub (it was published
2026-01-13). Result:

```
libobjc-12-dev : Depends: gcc-12-base (= 12.2.0-14)
                 but 12.2.0-14+deb12u1 is to be installed
E: Unable to correct problems, you have held broken packages.
```

The Rust base image carries security-updated packages. A snapshot before
the security update landed doesn't have them at that version → apt
can't resolve.

**Fix:** SDE ≥ rust base publication date. The only clamp in
`canonical_sde_for`. A Docker Hub lookup (cached 24h at
`~/.cache/rp2026/rust-base-pub.json`) supplies the date.

### Finding 1 — default docker driver ignores `rewrite-timestamp`

With the default `docker` driver,
`--output type=image,...,rewrite-timestamp=true` silently does nothing:

```
$ tar -tvf <layer>.tar
drwxr-xr-x  0 0 0  0 May  3 13:16 etc/
-rw-r--r--  0 0 0 51 May  3 13:16 etc/apt/apt.conf.d/keep-cache
```

`May 3 13:16` is the build wall-clock, not SDE.

**Fix:** switch to the `docker-container` driver:

```
docker buildx create --name repro-builder --driver docker-container --bootstrap
docker buildx build --builder repro-builder --output type=oci,rewrite-timestamp=true ...
```

After switching: OCI `image.created` correctly reads the SDE timestamp,
file mtimes in layers are SDE-stamped. `fat_image.py` uses
`desktop-linux` (docker-container-backed on Docker Desktop) by default.

### Finding 2 — layer blobs still differ after SDE is properly applied

Same Dockerfile, same SDE, same driver, everything-the-same double
build. Top-level manifest digests diverge. First 5 layers (inherited
from `rust:1.92.0-bookworm`) match exactly. Layers built by our
Dockerfile diverge in both digest *and compressed size*:

| Layer | Size A (bytes) | Size B (bytes) | Δ |
|---|---|---|---|
| 1 | 256,093,596 | 256,093,555 | A larger by 41 |
| 2 | 60,427,835  | 60,427,826  | A larger by 9  |
| 3 | 359,416,045 | 359,416,068 | B larger by 23 |

Compressed-size differences bound content difference: a 41-byte
compressed delta could be 1 byte of real content inflated by gzip state
skew, or up to 41 real bytes. We stopped short of decompressing and
`cmp`-comparing tar streams — the fingerprint pivot made it
unnecessary.

**Hypotheses for the residual jitter:**

- Apt internal state non-determinism:
  - `/var/log/apt/history.log`, `/var/log/apt/term.log` — include
    timestamps and PIDs.
  - `/var/cache/apt/pkgcache.bin`, `srcpkgcache.bin` — binary caches
    with hash-table-iteration-order-dependent layout.
  - `/var/lib/apt/extended_states` — install-time metadata.
  - `/var/lib/dpkg/lock*` — empty files with build-time mtime.
  - `/var/lib/dpkg/info/*.list` — dir enumeration order FS-dependent.
- Package download cache ordering. `repro-sources-list.sh` sets
  `Binary::apt::APT::Keep-Downloaded-Packages "true"`, so
  `/var/cache/apt/archives/*.deb` files land in the layer. Bytes
  identical (same upstream .deb) but tar entry order depends on how apt
  wrote them.
- Gzip-state skew: BuildKit uses a deterministic gzip wrapper, so this
  is an unlikely sole cause. Not ruled out.

### What's confirmed deterministic

- Base image layers 1–5 (inherited from `rust:*-<debian>`).
- OCI manifest annotations: `image.created` = SDE timestamp.
- `COPY repro-sources-list.sh` layer — same digest both builds.
- `repro-sources-list.sh` runtime output — identical `sources.list`.

## The environment fingerprint (current contract)

The Dockerfile's last RUN emits five files under `/manifest/`:

| File | Content |
|---|---|
| `packages.txt` | `dpkg-query -W -f='${Package} ${Version} ${Architecture}\n' \| LC_ALL=C sort` |
| `rustc.txt` | `rustc -vV` |
| `cargo.txt` | `cargo -vV` |
| `os-release` | `/etc/os-release` verbatim |
| `sources.list` | `/etc/apt/sources.list` verbatim — the snapshot URL |

Fingerprint:

```
cat /manifest/packages.txt /manifest/rustc.txt /manifest/cargo.txt \
    /manifest/os-release /manifest/sources.list | sha256sum
```

Entries record the fingerprint + per-file hashes + `packageCount` +
`rustcVersion` (human-glanceable). The regenerator rebuilds the fat
image, extracts `/manifest/*`, recomputes the digest, asserts equality.

### What this buys

- Rebuild host X, run the regenerator, it compares X's `/manifest/*` to
  the entry's recorded fingerprint. Match = proceed. Mismatch = print
  per-file diff and stop.
- Covers the real concern ("is the scientific environment the same?")
  without over-claiming ("are the bytes the same?").
- Diagnosable: when `packages.txt` differs, the diff names the drifted
  package. No hunting through layer blobs.
- Cheap: ~25 KB per fat image, can be embedded in the entry if a
  zero-network verify path is wanted later.

### What this does NOT cover

- **Thin per-entry images.** Their byte-reproducibility requires
  `cargo vendor` determinism + `-Cmetadata` hashing + proc-macro
  determinism — out of scope. Thin-image digests are recorded
  advisorily (`reproduction.thinImages.{expectedPre, expectedPost,
  expectedFix}`) but not load-bearing.
- **Non-fat toolchain properties.** Kernel version, CPU flags, glibc —
  host-level, outside the fat image's control. If a reproduction drifts
  due to host kernel, the fingerprint will match but outcome may
  differ. Recorded in `reproduction.verifiedOn[]` per-host for later
  analysis.
- **Apt download cache.** `/var/cache/apt/archives/*.deb` is not in
  `packages.txt`. It's the main byte-jitter source. Intentionally
  excluded — those are ephemeral build artifacts.

## Upstream contributions

Three gaps in RB.org's Rust documentation, ranked by feasibility:

**1. Containerised Cargo builds (best pitch).** RB.org's Rust page
doesn't discuss Docker at all — no Dockerfile example, no base-image
pinning, no apt-snapshot integration, no `*-sys` workflow, no vendoring
recipe. Our fat-Dockerfile template + `repro-sources-list.sh`
integration + 93% `*-sys` coverage number directly fills this. ~1–2
days of writing + PR to `reproducible-builds/reproducible-website`.

**2. `*-sys` coverage data.** Our 50-repo survey has quantified data
(93% coverage with 35 packages). Half-day to polish into prose + a YAML
package list.

**3. Reproducible Cargo Central.** No Rust equivalent of Maven's
`jvm-repo-rebuild/reproducible-central`. Weeks of engineering, beyond
RP scope but a natural Future Work direction — the pipeline could serve
as the foundation.

Paper payoff: if any of these lands as a submitted/merged PR, it's a
concrete supplementary deliverable matching the supervisor's explicit
suggestion.

## Where the code lives

| Artifact | Path |
|---|---|
| Fat-image Dockerfile | [`docker/cargo-fat/Dockerfile`](../../docker/cargo-fat/Dockerfile) |
| Vendored `repro-sources-list.sh` (locally patched for buster) | [`docker/cargo-fat/repro-sources-list.sh`](../../docker/cargo-fat/repro-sources-list.sh) |
| Fat-image inventory | [`docker/cargo-fat/index.json`](../../docker/cargo-fat/index.json) |
| Canonical bucketing + SDE policy | [`pipelines/cargo/fat_image.py`](../../pipelines/cargo/fat_image.py) |
| Entry regenerator (fingerprint verifier) | [`pipelines/cargo/cargo_regenerate.py`](../../pipelines/cargo/cargo_regenerate.py) |
| Schema definitions | [`schema/entry.schema.json`](../../schema/entry.schema.json), [`lib/bump_ext/models.py`](../../lib/bump_ext/models.py) |
| Run instructions | [`running-a-batch.md`](running-a-batch.md) |
| Image-selection logic (full walkthrough) | [`image-selection.md`](image-selection.md) |

## Fat-image policy summary

Canonical SDE per bucket: `last-day-of-bucket-year`, raised to
`rust_base_pub` if the bucket predates the Rust base image. There is no
upper clamp — candidates past the run's `max_sde_date` are rejected
upstream by `bucketize` / `process`. See
[`fat_image.py::canonical_sde_for`](../../pipelines/cargo/fat_image.py)
and [`image-selection.md`](image-selection.md) for the full rule.

Buckets where `year_end < rust_base_pub` are flagged `pre_rust_base` —
the image will build but with apt packages from a later era than the
commit. This is the honest cost of reproducing old code with no older
Rust base image available; the actual reproduction rate under these
images is empirical and measured by the driver.

Fat-image tag convention: `rp2026/cargo-fat:<rust-patch>-<debian>-<yyyymmdd>`.

## Risks + mitigations

| Risk | Mitigation |
| --- | --- |
| `snapshot.debian.org` eviction for old buster dates | Documented lower bound at ~2020-Q2 for buster-security. `repro-sources-list.sh` locally patched for pre-bullseye URL layout. Test commands in `running-a-batch.md` troubleshooting. |
| Cross-architecture fingerprint drift | `packages.txt` includes `${Architecture}`, so arm64 and amd64 fingerprints differ deliberately. `verifiedOn[]` records per-host arch for analysis. |
| Private PR commits GC'd | Archive the commit's tree to the entry at ingestion (future work, ~1 MB compressed per entry). Not implemented. |
| `cargo vendor` non-determinism | Rare but possible. Double-build on same machine before trusting cross-host results. |
| Buildx version skew across hosts | Document the buildx version on each `verifiedOn` record (future field). Minimum currently: `buildx >= 0.20` with `docker-container` driver. |

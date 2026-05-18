# Fat Debian-based Rust image.
# Includes the ~35 -dev packages that (per docs/cargo/survey-findings.md)
# cover ~93% of real-world Rust projects that consume native C libraries
# via *-sys crates.
#
# Byte-reproducible: pinned Rust patch version, apt pulled from a
# snapshot.debian.org URL keyed on SOURCE_DATE_EPOCH. Build with buildx
# and `rewrite-timestamp=true` so OCI metadata and file mtimes are
# SDE-stamped.
#
# Build (three build-args; DEBIAN_RELEASE defaults to bookworm):
#   docker buildx build \
#     --build-arg RUST_VERSION=1.92.0 \
#     --build-arg DEBIAN_RELEASE=bookworm \
#     --build-arg SOURCE_DATE_EPOCH=1735689600 \
#     --output type=image,name=rp2026/cargo-fat:1.92.0-bookworm-20250101,rewrite-timestamp=true \
#     docker/cargo-fat/
#
# Tag convention: rp2026/cargo-fat:<rust-patch>-<debian-release>-<apt-snapshot-YYYYMMDD>
#
# Supported DEBIAN_RELEASE values: bookworm, bullseye, buster (must match a
# `rust:${RUST_VERSION}-${DEBIAN_RELEASE}` tag on Docker Hub). The vendored
# repro-sources-list.sh handles all three releases.
ARG RUST_VERSION
ARG DEBIAN_RELEASE=bookworm
FROM rust:${RUST_VERSION}-${DEBIAN_RELEASE}

ARG SOURCE_DATE_EPOCH
ENV DEBIAN_FRONTEND=noninteractive

# Hard-fail if the caller didn't pass SOURCE_DATE_EPOCH. Without it the
# vendored repro-sources-list.sh falls back to stat'ing sources.list,
# which makes the apt snapshot depend on when the base image was pulled.
RUN test -n "${SOURCE_DATE_EPOCH}" \
    || (echo "ERROR: SOURCE_DATE_EPOCH build-arg is required" >&2; exit 1)

# Pin apt to the snapshot.debian.org archive keyed on SOURCE_DATE_EPOCH.
# Script vendored from reproducible-containers/repro-sources-list.sh at
# commit 39fbf150e3a5062d4c6b9a241f25af133e7cb6f0 (upstream sha256
# c125df9762b0c7233459087bb840c0e5dbfc4d9690ee227f1ed8994f4d51d2e0).
# Locally patched to use the pre-bullseye security-URL layout for buster/
# stretch/jessie (file now sha256
# 88c8f60cb92446be4106ecbd6c71d86d35e6dcf0db5af6e79a0077b723dad896).
COPY repro-sources-list.sh /usr/local/bin/repro-sources-list.sh
RUN chmod +x /usr/local/bin/repro-sources-list.sh \
 && /usr/local/bin/repro-sources-list.sh

# Core build tooling.
#
# --allow-unauthenticated is required for stretch-era snapshots on
# snapshot.debian.org: apt-secure validates against the keyring
# shipped in the base image, but snapshot serves security packages
# signed with rotated keys that the 2019-era keyring doesn't trust.
# Reproducibility is unaffected because the snapshot URL + SDE still
# pin exactly which bytes land. Authentication is an orthogonal
# concern that we trade off for historical-reproducibility coverage.
RUN apt-get update && apt-get install -y --no-install-recommends --allow-unauthenticated \
    git \
    pkg-config \
    build-essential \
    cmake \
    clang \
    libclang-dev

# Common native library -dev packages that show up in *-sys dependencies.
# Grouped thematically; grouping is cosmetic, apt resolves everything in one pass.
#
# `|| true` on the round-2 additions: not every package exists on every
# Debian release (slurm-client and libmpich-dev are missing on stretch,
# webkit2gtk-4.0 was renamed to 4.1 in bookworm, etc.). Per-package
# tolerance keeps the build succeeding for releases where a particular
# library is unavailable — at the cost of *some* fat images covering
# fewer candidates than others. The /manifest/packages.txt fingerprint
# captures the actual outcome, so reproducibility-vs-policy is
# distinguishable per candidate.
RUN apt-get install -y --no-install-recommends --allow-unauthenticated \
    # crypto / TLS / networking
    libssl-dev \
    libcurl4-openssl-dev \
    libssh2-1-dev \
    libnghttp2-dev \
    # databases
    libsqlite3-dev \
    libpq-dev \
    librocksdb-dev \
    # compression
    zlib1g-dev \
    libzstd-dev \
    libbz2-dev \
    liblzma-dev \
    liblz4-dev \
    # text / regex
    libonig-dev \
    # packet capture / raw net
    libpcap-dev \
    # audio / hardware / ipc
    libasound2-dev \
    libudev-dev \
    libdbus-1-dev \
    # misc
    libexpat1-dev \
    libxml2-dev \
 && apt-get install -y --no-install-recommends --allow-unauthenticated \
    # Round-2 audit additions — gap-fillers surfaced by DS1-full failure
    # classification. Per-package via `|| true` so missing packages on
    # older releases (slurm/mpich on stretch, webkit2gtk-4.0 vs -4.1 on
    # bookworm) don't kill the build.
    #
    # graphics / windowing (35 unsubcoded RUSTC_BITROT cases were linker
    # errors against these; SDL2 alone covered 17 candidates from the
    # evanjs/rrbg fork-cluster).
    libsdl2-dev \
    libxtst-dev \
    libxcb-shape0-dev \
    # video capture / FUSE / desktop notifications.
    libfuse-dev \
    libv4l-dev \
    libnotify-dev \
    # crypto + Python C-API + capabilities (undefined-reference linker
    # failures: gcry_randomize ×2, PyTuple_New ×3, PyExc_Exception ×1,
    # plus libcap-ng pkg-config misses).
    libgcrypt20-dev \
    libpython3-dev \
    libcap-ng-dev \
    # compression that wasn't already covered.
    libsnappy-dev \
    librrd-dev \
    # MPI + workload-scheduler clients (HPC-flavoured Rust crates).
    libmpich-dev \
    slurm-client \
    libslurm-dev \
    || true

# Rust components that live outside apt: rustfmt + clippy via rustup.
# `|| true` because pre-rustup-component-install rustc images won't
# accept these. Six OTHER cases were `'rustfmt' is not installed for
# the toolchain` errors that this addresses.
RUN rustup component add rustfmt clippy 2>/dev/null || true

# libgit2-dev on stretch transitively depends on libcurl4-gnutls-dev which
# conflicts with the libcurl4-openssl-dev we install above. buster+ doesn't
# have this conflict. Install libgit2-dev separately on non-stretch so the
# stretch builds stay clean; git2-rs crate users on stretch fall back to
# the bundled libgit2 (git2 has a vendored feature flag).
ARG DEBIAN_RELEASE
RUN if [ "${DEBIAN_RELEASE}" != "stretch" ]; then \
        apt-get install -y --no-install-recommends --allow-unauthenticated libgit2-dev; \
    fi

# GUI stack (GTK3 + WebKit + Tauri deps) — needed for Tauri-based projects
# such as the netscli test case. Big (~500 MB), the main cost of the fat
# image. Disabled for pre-bullseye releases: libsoup-3.0, libwebkit2gtk-4.1,
# libegl1 (→ libegl-dev) are bullseye+ era, and buster-targeting Rust code
# predates the Tauri ecosystem anyway.
#
# Set INCLUDE_GUI=0 for older debian releases or to opt out.
ARG INCLUDE_GUI=1
RUN if [ "${INCLUDE_GUI}" = "1" ]; then \
        apt-get install -y --no-install-recommends \
            libgtk-3-dev \
            libglib2.0-dev \
            libgdk-pixbuf2.0-dev \
            libpango1.0-dev \
            libcairo2-dev \
            libatk1.0-dev \
            libsoup-3.0-dev \
            libwebkit2gtk-4.1-dev \
            libayatana-appindicator3-dev \
            libwayland-dev \
            libegl-dev \
            libgl-dev; \
    fi

# Sanity check
RUN cargo --version && rustc --version && pkg-config --version

# Environment fingerprint — the reproducibility contract under "Fork B"
# semantics. Regeneration compares these files against what the entry
# JSON recorded when it was first validated. Equivalence of these files
# is the bar, not OCI layer digest identity.
#
# /manifest/packages.txt — every installed apt package + exact version.
# /manifest/rustc.txt     — rustc -vV (toolchain + host triple + commit).
# /manifest/cargo.txt     — cargo -vV.
# /manifest/os-release    — /etc/os-release copy, for debian version
#                           and release codename.
# /manifest/sources.list  — the snapshot URL chosen by repro-sources-list.sh.
#
# Files live under /manifest so consumers don't need to know their exact
# paths inside the usual FHS locations.
RUN mkdir -p /manifest \
 && dpkg-query -W -f='${Package} ${Version} ${Architecture}\n' \
        | LC_ALL=C sort > /manifest/packages.txt \
 && rustc -vV > /manifest/rustc.txt \
 && cargo -vV > /manifest/cargo.txt \
 && cp /etc/os-release /manifest/os-release \
 && cp /etc/apt/sources.list /manifest/sources.list

CMD ["cargo", "--version"]

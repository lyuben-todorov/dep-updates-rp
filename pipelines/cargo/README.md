# Cargo pipeline

Mining and reproduction pipeline for Rust/Cargo dependency updates.
Produces schema-valid (v0.0.4) entries under `data/cargo/` conforming to
[`schema/entry.schema.json`](../../schema/entry.schema.json).

For the end-to-end runbook (fresh checkout → verified entries), see
[`docs/cargo/running-a-batch.md`](../../docs/cargo/running-a-batch.md).
This README documents the per-script API.

## Architecture

The pipeline is orchestrated by `cargo_drive.py` but composed of
single-purpose scripts that can be invoked independently for debugging.

```
ingest                       classify + assemble
  ┌──────────────────┐         ┌──────────────────┐
  │ cargo_miner.py   │         │ cargo_classifier │
  │ rebatchi_to_…    │         │ cargo_assemble_… │
  └─────┬────────────┘         └──────────────────┘
        │ candidates.jsonl              ↑
        │                               │
        ↓                               │
  ┌──────────────────┐         ┌────────┴─────────┐
  │ cargo_drive.py   │────────►│ cargo_reproducer │
  │ (orchestrates)   │         └──────────────────┘
  └─────┬────────────┘                  │
        │                               ↓
        ↓                         data/cargo/*.log
  ┌──────────────────┐
  │ fat_image.py     │
  │ (index, bucket,  │
  │  canonical tag)  │
  └──────────────────┘

verify-on-rebuild (independent)
  ┌────────────────────────┐
  │ cargo_regenerate.py    │
  │ (entry → rebuild →     │
  │  fingerprint verify →  │
  │  append verifiedOn)    │
  └────────────────────────┘
```

## Scripts

### `_candidate.py`

Internal — shared `Candidate` dataclass and GitHub helpers
(`gh_headers`, `classify_author`, `BUMP_RE`). Imported by both miner
and rebatchi translator. Not invokable.

### `cargo_miner.py` — live GitHub mining

```
python3 -m pipelines.cargo.cargo_miner <owner/repo> [--limit N] [--out file.jsonl]
```

Walks `/repos/<owner/repo>/pulls`, filters to PRs authored by
Dependabot/Renovate that touch only `Cargo.toml` (and optionally
`Cargo.lock`) with a single-line version bump, and emits one
`Candidate` per PR. Populates `rust_msrv` + `post_commit_date`
(4 GitHub API calls per emitted candidate).

### `cargo_toolchain.py` — MSRV + commit-date fetchers

Two API surfaces:

- **File-based parsers** — `detect_from_{rust_toolchain_toml, rust_toolchain,
  cargo_toml}(path)` + `detect_toolchain(repo_root)` — used when you
  have a local checkout.
- **GitHub-API fetchers** — `msrv_at_commit(repo, sha)`,
  `commit_date_at(repo, sha)` — used by the candidate producers.

MSRV precedence (strongest first): `rust-toolchain.toml` →
`rust-toolchain` → `Cargo.toml:[package].rust-version` → edition
fallback (`edition = "2021"` → `"1.56"`). Returns `None` for channel
tags (`stable`, `beta`, `nightly`) since they don't pin a concrete rust
version.

`debian_release_for(date)` picks a Debian codename from a commit date
(pre-2021-08 → buster, 2021-08 to 2023-06 → bullseye, 2023-06 to
2025-08 → bookworm, ≥ 2025-08 → trixie).

### `cargo_reproducer.py` — pre/post (and optional fix) verification

```
python3 -m pipelines.cargo.cargo_reproducer \
  --in candidates.jsonl \
  --out reproduction.jsonl \
  --toolchain rp2026/cargo-fat:1.56.0-buster-20211022 \
  --logs-dir ./logs \
  --timeout 1800
```

For each candidate, spawns (up to) three transient Docker containers:

1. Clone the repo, checkout `pre_commit`, run `cargo test --locked
   --message-format=json-diagnostic-rendered-ansi --no-fail-fast`.
2. Same for `post_commit`.
3. (Only if `fix_commit` is set on the candidate) same for `fix_commit`.

Writes logs to `<logs_dir>/<shortHash>-{pre,post,fix}.log` and emits
a `ReproductionResult` with raw exit codes plus derived
`pre_passed` / `post_passed` / `fix_passed` properties and a
`matches_category(category)` helper the driver uses.

If no `--toolchain` is passed the reproducer attempts auto-detection via
`cargo_toolchain.detect_toolchain` (returns a `rust:<minor>-alpine` tag)
— fine for pure Rust but insufficient for anything pulling in `*-sys`
crates. Use an explicit fat image in practice.

### `cargo_classifier.py` — log → failure taxonomy

```
python3 -m pipelines.cargo.cargo_classifier <log-path>
```

Parses `cargo`'s JSON diagnostic stream, extracts rustc error codes,
maps them into the shared taxonomy (`schema/failure-taxonomy.md`).
Falls back to keyword matching for test failures, dependency-resolution
failures, environment failures. Stdout is a single JSON object matching
the schema's `failure` subobject.

### `cargo_assemble_entry.py` — candidate + reproduction + classification → entry

```
python3 -m pipelines.cargo.cargo_assemble_entry \
  --candidate c.json --reproduction r.json --classification f.json \
  --category breaking \
  --fat-image rp2026/cargo-fat:1.56.0-buster-20211022 \
  --source-date-epoch 1634860800 \
  --build-flags=--locked,--offline
```

- `--category` — `breaking` / `non-breaking` / `fix-after-update` /
  `unreproducible`. The driver discovers this from reproducer exit
  codes; when invoked standalone, pass it explicitly.
- `--fat-image` — tag of the fat image the reproduction ran against.
  The assembler runs a one-shot `docker run` to extract `/manifest/*`
  and compute the environment fingerprint that goes into the entry.
- `--fat-image-auto` — resolve the tag from
  `docker/cargo-fat/index.json` using `--rust-msrv` + `--commit-date`.
- `--source-date-epoch` — required with `--fat-image`; derived from the
  resolved record when using `--fat-image-auto`.

Writes `data/cargo/cargo-<shortHash>.json`, schema-validated before
writing.

### `cargo_regenerate.py` — verify an entry on any host

```
python3 -m pipelines.cargo.cargo_regenerate \
  --entry data/cargo/cargo-<id>.json \
  [--build-missing-bases] [--skip-tests] \
  --host $(hostname)
```

1. Reads `reproduction.fatImage` from the entry. Constructs the
   canonical tag. If not locally present, requires
   `--build-missing-bases` to build it.
2. Extracts `/manifest/*`, computes the environment fingerprint,
   asserts equality with `reproduction.environmentFingerprint.digest`.
   **Mismatch is a hard fail.**
3. Builds `<hash>-pre`, `<hash>-post`, and optionally `<hash>-fix` thin
   images, each with `cargo vendor` pre-populated and
   `RUSTFLAGS=--remap-path-prefix=/src=.` set.
4. Runs each thin image with `--network none`, compares pass/fail to
   the entry's `category` expectation.
5. Appends a `verifiedOn` record to the entry and writes back in place.

Exit codes:
- `0` — fingerprint match and (if tests ran) outcome match.
- `1` — fingerprint mismatch; the entry was validated against a
  different environment than the one we just produced.
- `2` — thin image build failed.
- `3` — fat image missing and `--build-missing-bases` not set.
- `4` — outcome mismatch (pass/fail doesn't match category).

### `fat_image.py` — inventory + canonical bucketing + build CLI

See [`../../docs/cargo/image-selection.md`](../../docs/cargo/image-selection.md)
for the full logic of how (msrv, commit_date, debian) becomes a fat-image
tag, including the Docker Hub support grid and the upward-rerouting rule.

Three layers:

**Canonical primitives** (pure functions):
```python
BucketKey(milestone, year, debian)
bucket_for(msrv, commit_date, debian) → BucketKey | None
canonical_sde_for(BucketKey, *, max_sde_date) → CanonicalSde(sde, sde_date, pre_rust_base, rust_base_unknown)
tag_for(BucketKey, sde) → str
default_max_sde_date() → dt.date     # Dec 31 of last year
```

**Index I/O**:
```python
load_index() → list[FatImageRecord]
register(record)            # appends to docker/cargo-fat/index.json
introspect_fat_image(tag) → FatImageRecord
```

**Build + CLI**:
```
python3 -m pipelines.cargo.fat_image list
python3 -m pipelines.cargo.fat_image resolve --rust-msrv 1.56 --commit-date 2020-05-01
python3 -m pipelines.cargo.fat_image build \
    --rust-version 1.56.0 --debian-release buster --source-date-epoch 1634860800
python3 -m pipelines.cargo.fat_image register --tag <tag>
```

`canonical_sde_for` caches Docker Hub lookups for 24 hours at
`~/.cache/rp2026/rust-base-pub.json`.

### `cargo_plan_fat_images.py` — read-only batch planner

```
python3 -m pipelines.cargo.cargo_plan_fat_images \
  --candidates data/rebatchi/ds1_candidates_enriched.jsonl
```

Reads a candidate JSONL, bucketizes into canonical BucketKeys, maps
each to its canonical tag, groups proposals by tag (two buckets can
dedupe when their SDEs clamp to the same rust_base_pub), and looks up
each tag in the index. Prints a table of existing-reused +
proposed-to-build images and the exact build commands. Does not touch
Docker.

Flags: `--min-density N`, `--resolve-missing` (fetch missing MSRV /
commit_date via GitHub API at plan time), `--max-sde-date YYYY-MM-DD`
(run-level upper bound on commit dates; candidates past it are
rejected as `commit_too_recent`; default `default_max_sde_date()`).

### `cargo_drive.py` — end-to-end orchestrator

```
python3 -m pipelines.cargo.cargo_drive \
  --candidates candidates.jsonl \
  --out-dir data/cargo/ \
  --logs-dir data/cargo/logs/ \
  --state data/drive-state.jsonl \
  [--build-missing-bases] [--regenerate-verify] \
  [--limit N] [--host label]
```

Per candidate:

1. Read MSRV + commit-date from candidate fields (GH-API fallback if
   missing). Fail with `metadata_fetch_failed` if neither works.
2. Compute canonical BucketKey + SDE + tag. Fail with
   `fat_image_missing` if tag not in index and `--build-missing-bases`
   not set.
3. Build the fat image if needed, register in index.
4. Reproduce (`cargo_reproducer.reproduce`).
5. Discover category from exit codes:
   - `pre_rc != 0` → `not_reproducible` / `pre_build_failed`.
   - `pre pass, post fail` → `breaking`.
   - `pre pass, post pass` → `non-breaking`.
6. Classify (breaking only).
7. Assemble entry → `data/cargo/<id>.json`.
8. Optional `--regenerate-verify` → `cargo_regenerate.regenerate(...)`.
9. Append record to state JSONL.

State records are terminal — a second run skips candidates already
resolved. Re-drive a candidate by deleting its line from the state
file.

## Worked example

See [`docs/cargo/running-a-batch.md`](../../docs/cargo/running-a-batch.md)
for the full from-scratch run. The committed entries under `data/cargo/`
(`cargo-9ac20c07.json`, `cargo-f82e5be0.json`) were produced by this
pipeline and can be regenerated to verify.

## Known sharp edges

- **Miner only parses simple `name = "x.y.z"` bumps**. Table-style
  entries (`[dependencies.name] \n version = "…"`) are regex-matched
  but untested.
- **Flaky-test detection not implemented.** BUMP runs each build 3×;
  we run once.
- **`versionUpdateType` requires a three-part semver** — `0.20 → 0.21`
  classifies as `other`.
- **Candidate enrichment is not resumable**. If
  `rebatchi_to_candidate.py` is killed mid-run, it restarts from row 0.
  Fix tracked in `../../../docs/db-design.md`.
- **Environmental vs outcome failure indistinguishable** — a docker
  daemon crash, disk-full, or network hiccup look identical to a
  legitimate pre-build failure at exit-code level. Inspect logs to
  distinguish.

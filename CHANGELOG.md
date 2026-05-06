# Changelog

## v0.0.4 — 2026-05-04 (+ 2026-05-06 addendum)

Category-neutral nomenclature + fat-image internals refactor. The
addendum below (2026-05-06) is policy / code only; no schema change,
`SCHEMA_VERSION` stays at 0.0.4.

### Addendum (2026-05-06) — fat-image policy simplification

- **MILESTONE_RELEASE_DATES.** Dates fetched authoritatively from
  `rust-lang/rust/RELEASES.md`. Feeds `latest_milestone_before(commit_date)`.

- **MSRV fallback: edition-floor dropped, latest-milestone-at-commit
  used instead.** `_EDITION_TO_MIN_MSRV` / `_edition_from_cargo_toml_bytes`
  removed from `cargo_toolchain.py`. Candidates without a declared MSRV
  now round up to the latest milestone released by their commit date
  rather than defaulting to `1.56`. Closer to what the author was
  plausibly targeting at the time.

- **Docker Hub support grid.** `MILESTONE_DEBIAN_SUPPORTED` hardcoded
  (probed 2026-05-05). `bucket_for` reroutes unsupported
  `(milestone, debian)` pairs upward on the milestone axis, keeping
  debian fixed to preserve the commit's OS era.

- **`max_sde_date` as a run parameter.**
  - `canonical_sde_for(bucket, *, max_sde_date)` — pure function of the
    bucket. No more `today()` reads, no more upper clamp. Determinism
    across hosts and time.
  - `bucketize(candidates, *, max_sde_date)` — candidates past the
    cutoff are rejected as `commit_too_recent`.
  - `process(candidate, *, max_sde_date)` — parks with
    `Status.COMMIT_AFTER_MAX_SDE_DATE`.
  - `DriveRecord.max_sde_date` — every state record carries the run
    parameter that included/excluded the candidate.
  - `default_max_sde_date()` — Dec 31 of last year.
  - CLIs: `--max-sde-date YYYY-MM-DD` on planner and driver.

- **Axis A removed.** The planner no longer collapses lower-milestone
  buckets into same-(year, debian) higher-milestone neighbors. One
  bucket = one proposal, unless two buckets produce the same canonical
  tag via SDE coincidence. Entries now record their minimum-required
  rust faithfully.

- **Dead code removed.** `collapse_axis_a`, `ResolveRequest`,
  `ResolveResult`, `resolve()` all deleted from `fat_image.py`.
  `fat_image resolve` CLI rewritten against the canonical bucketing
  API (same I/O). `cargo_assemble_entry.py --fat-image-auto` ported to
  the canonical API.

- **Docs.**
  - New: `dep-updates-poc/docs/cargo/image-selection.md` — full chain
    of `bucket_for` → `canonical_sde_for` → `tag_for` with worked
    examples, the Docker Hub support grid, and the run-parameter story.
  - `docs/reproducible-builds.md` — SDE rule no longer mentions the
    removed `today - 2d` clamp.
  - `docs/research-plan.md` — refreshed for v0.0.4 reality (Fork B,
    `cargo_drive` entry point, Zenodo-only distribution, no registry
    hosting needed).
  - `docs/rebatchi.md`, `docs/db-design.md`, `docs/cargo/running-a-batch.md`:
    DS1 numbers updated (4 proposals now, not 5).
  - Module docstrings across the Cargo pipeline refreshed; stale
    references to v0.0.3, `-breaking` tag, `breaking_commit` field
    removed.

- **`runs` table added to `docs/db-design.md`.** First-listed table in
  the planned SQLite index. `max_sde_date` documented as the flagship
  run parameter. `drive_state` grows a `run_id` foreign key and
  composite `(run_id, candidate_key)` primary key.

**DS1 plan as of this addendum** (default `max_sde_date=2025-12-31`):

- 2607 / 2608 candidates bucketize.
- 4 proposed images:
  - `rp2026/cargo-fat:1.49.0-buster-20210209` — 2405 candidates
    (2018 / 2019 / 2020 buster, all clamp to rust_pub).
  - `rp2026/cargo-fat:1.49.0-buster-20211231` — 198 (2021, 1.49).
  - `rp2026/cargo-fat:1.56.0-buster-20211231` — 3 (2021, 1.56).
  - `rp2026/cargo-fat:1.56.0-bullseye-20211231` — 1 (2021 bullseye).

### Original v0.0.4 release (2026-05-04) — category-neutral nomenclature + fat-image internals refactor

**Schema (breaking).**
- `commits.preBreaking` → `commits.pre`, `commits.breaking` → `commits.post`.
  Optional `commits.fix` added for fix-after-update entries.
- `commits.{preBreaking,breaking}AuthorType` → `commits.{pre,post,fix}AuthorType`.
- `reproduction.thinImages.{expectedPreDigest, expectedBreakingDigest}`
  → `{expectedPre, expectedPost, expectedFix}`.
- `unreproducibilityReason` enum values renamed to be category-neutral:
  `pre_breaking_build_failed` → `pre_build_failed`;
  `breaking_build_passed` → `post_passed_when_expected_to_fail`,
  `post_failed_when_expected_to_pass`; new `fix_did_not_restore`.
- Schema `$id` bumped to 0.0.4. No backward-compat shim: the only two real
  entries on disk were migrated in-place.

**Pipeline code — category-neutral fields.**
- `_candidate.py` — `Candidate.{breaking_commit, pre_breaking_commit,
  breaking_commit_date}` → `{post_commit, pre_commit, post_commit_date}`.
- `cargo_miner.py`, `scripts/rebatchi_to_candidate.py` — populate new fields.
- `cargo_reproducer.py` — `ReproductionResult` reshaped. Now reports raw
  exit codes + derived `pre_passed`/`post_passed`/`fix_passed` properties
  and a `matches_category(category_str)` helper. Optional third fix-commit
  run for fix-after-update candidates. Log files named
  `<short>-pre.log` / `<short>-post.log` / `<short>-fix.log`.
- `cargo_regenerate.py` — reads `commits.{pre,post,fix}`, builds thin
  tags `cargo-thin:<hash>-{pre,post,fix}`, outcome check branches per
  category (breaking / non-breaking / fix-after-update).
- `cargo_assemble_entry.py` — `build_entry()` now requires `category`
  as an explicit parameter (previously inferred from reproducer output).
  Only breaking / fix-after-update get a `failure` block.
- `cargo_drive.py` — discovers category from exit codes at processing
  time: `pre fail → unreproducible`, `pre pass + post fail → breaking`,
  `pre pass + post pass → non-breaking`. Feeds discovered category to
  the assembler. Only classifies the post log when the category is
  `breaking`.

**Fat-image internals refactor.**
The fat-image policy collapsed from eight interacting flags
(reuse_window_days, exact-SDE-match override, pre_rust_base, ad-hoc SDE
picking, etc.) into three pure functions.

New surface in `pipelines/cargo/fat_image.py`:

- `BucketKey(milestone, year, debian)` — canonical identity for a
  fat-image-needing bucket.
- `bucket_for(msrv, commit_date, debian) → BucketKey | None` — maps a
  candidate to its bucket. MSRV rounds up to the smallest milestone
  (`MILESTONES = [1.49, 1.56, 1.65, 1.75, 1.85, 1.92]`).
- `canonical_sde_for(BucketKey) → CanonicalSde` — the *one* place that
  decides SDE: `last-day-of-year` clamped to
  `[rust_base_pub, today - 2d]`. Pure function modulo a 24h-cached
  Docker Hub lookup at `~/.cache/rp2026/rust-base-pub.json`.
- `tag_for(BucketKey, sde) → str` — canonical tag format.
- `collapse_axis_a(bucket_counts)` — merges same-(year, debian) buckets
  to the highest-milestone representative.

Removed (subsumed): `cargo_drive._pick_sde`, `cargo_drive._rust_base_published`,
`cargo_drive._latest_patch_for_msrv`, `cargo_drive.SNAPSHOT_SAFETY_DAYS`;
`cargo_plan_fat_images`' local `collapse_axis_a`, `dedupe_proposals`,
`_DEBIAN_CUTOVERS`, bespoke `Bucket`/`ProposedImage`/`round_up_to_milestone`.

Reuse is now **tag equality only** — no windows, no overrides. A tag
either exists in the index or doesn't.

**`cargo_plan_fat_images` — full rewrite.**
Now ~150 lines shorter. `plan()` is: bucketize → collapse_axis_a →
canonical_sde_for → tag_for → compare against index. `--reuse-window-days`
flag removed (behavior folded into canonical SDE).

**`cargo_regenerate.py`.**
Its local `build_fat_image` and `fat_image_tag` are now thin delegators
to `fat_image.py`.

**Data migrated to v0.0.4.**
`data/cargo/cargo-9ac20c07.json`, `data/cargo/cargo-f82e5be0.json`,
`schema/examples/cargo-example.json` rewritten in place. Candidate JSONLs
(`data/rebatchi/ds1_candidates_enriched*.jsonl`) field-renamed.

**Regression-tested.**
Full DS1 planner run on the 2608 enriched candidates covers
2607/2608 (100%) with 5 proposals — 3 reuse the existing
`1.56.0-buster-20211022` for 2018/2019/2020 buckets, 2 new builds
proposed for 2021 buster + 2021 bullseye. Pre-refactor the planner
emitted 2 proposals reusing the same image across all years; the new
output is more honest (per-year canonical SDE, no hidden reuse).

## v0.0.3 — 2026-05-03

Reproducibility pivot to **environmental equivalence** (Fork B in
`reproducible-builds-findings.md`). Byte-identical OCI digests turned out
to be blocked by apt-internal non-determinism even with pinned SDE + apt
snapshot; the paper will instead stand on deterministic *environments*
(same rustc, same apt package set, same snapshot URL) while recording
OCI digests as advisory diagnostics.

**Schema (breaking).**
- `reproduction.preImage` / `breakingImage` / `toolchain` → removed.
- `reproduction.fatImage` added (`rustVersion`, `sourceDateEpoch`,
  `aptSnapshot`, `debianRelease`, advisory `expectedDigest`).
- `reproduction.buildFlags` added — exact flags recorded, so an older/
  newer pipeline can't silently change them.
- `reproduction.environmentFingerprint` added — `digest` (sha256 of a
  canonical concat of five manifest files) + per-file `files[]` for
  diagnostic diffs. Exact match is the reproducibility contract.
- `reproduction.thinImages` added, advisory-only.
- `reproduction.verifiedOn` is now an array of records
  (`platform`, `host`, `verifiedAt`, `fingerprintMatch`,
  `fatImageDigestMatch`, `outcomeMatch`), appended by
  `cargo_regenerate.py`.

**Fat image (`docker/cargo-fat/Dockerfile`).**
- `RUST_VERSION` no longer defaults; caller must pass a full patch version.
- `SOURCE_DATE_EPOCH` now a required build-arg; hard-fails if missing.
- `repro-sources-list.sh` vendored (commit `39fbf150...`) and used to
  point apt at `snapshot.debian.org/archive/debian/<sde>/`.
- Last RUN emits `/manifest/{packages.txt,rustc.txt,cargo.txt,os-release,sources.list}`
  — the environment fingerprint the regenerator verifies.

**New / reshaped scripts.**
- `pipelines/cargo/cargo_regenerate.py` — entry-driven regenerator.
  Resolves fat image → verifies fingerprint → builds thin `pre`/`breaking`
  images → runs offline tests → appends `verifiedOn`.
- `pipelines/cargo/fat_image.py` — fat-image inventory + resolver + CLI
  (`list`, `resolve`, `build`, `register`). Backed by
  `docker/cargo-fat/index.json`.
- `pipelines/cargo/cargo_drive.py` — end-to-end driver. Candidate JSONL
  → entries with verification records. Resumable via state JSONL.
- `pipelines/cargo/cargo_assemble_entry.py` — rewritten for v0.0.3.
  Introspects fat image to populate `fatImage` + `environmentFingerprint`.
  `--fat-image-auto` consults the resolver.
- `pipelines/cargo/cargo_reproducer.py` — `--locked` added to the build
  command.

**Retired.**
- `pipelines/cargo/cargo_dockerizer.py` — deleted. The Fork B pivot made
  published image refs non-load-bearing.
- `bump_ext.image_ref()` — deleted from `writer.py` and `__init__.py`.
  No real callers remained after the schema reshape.

**Rebatchi work (continues).**
- Added `scripts/rebatchi_to_candidate.py` — translates Rebatchi CSV rows
  into candidate JSONL by resolving PR SHAs via the GitHub API. Supports
  `--require-cargo` post-filter and `--skip-gh-verify` dry-runs.
- Feasibility verdict: Dataset 2 alone is too narrow (~32 real Cargo bumps
  after filtering). Dataset 1 (3.7 GB) is the real target. See
  `docs/cargo/rebatchi-feasibility.md`.

**Docs.**
- New: `reproducible-builds-findings.md` (adjacent to the repo) — Fork A
  vs Fork B decision, the 41-byte apt-layer delta, environment
  fingerprint spec.
- Older docs under `docs/cargo/` and `docs/shared/` are **partially
  stale** post-pivot (pre-pivot image-management assumptions, v0.0.2
  schema references). Authoritative: source code + schema file.

## v0.0.2 — 2026-04-30

- Real end-to-end reproduction of a Dependabot breaking update: `fstubner/netscli#22` (`ipnetwork 0.20 → 0.21`) now produces `data/cargo/cargo-9ac20c07.json` classified as `COMPILATION_FAILURE` / `TYPE_MISMATCH` / `E0308`.
- Built `docker/cargo-fat/Dockerfile` — Debian-bookworm-based Rust image with ~35 `-dev` packages. Survey shows ~93% coverage of real-world Cargo projects (up from ~15% on Alpine).
- Added Cargo toolchain auto-detection (`cargo_toolchain.py`). Reads `rust-toolchain.toml`, `rust-toolchain`, or `Cargo.toml:rust-version` and returns the matching Rust image tag.
- Reproducer (`cargo_reproducer.py`) now fetches closed-unmerged PR commits via `git fetch origin <sha>:_repro` when the commit isn't on the default branch.
- Reproducer recognises fat images (`rp2026/cargo-fat:*`) and skips the runtime apt install.
- Schema: `commits.preBreakingAuthorType` and `commits.breakingAuthorType` are now nullable (caught by the schema validator on the first real reproduction).
- Renamed all Cargo-specific files with `cargo_` prefix for clarity alongside shared infra.
- Reorganised docs into `docs/shared/` (schema, library) and `docs/cargo/` (survey findings, image management).
- Added `pipelines/cargo/README.md` with the full end-to-end worked example.
- Added `docs/shared/schema.md` and `docs/shared/bump_ext-library.md` — explainers for the two shared contracts.

## v0.0.1 — 2026-04-30

- Initial POC draft.
- Schema: `entry.schema.json` v0.0.1 with required fields for project, PR, commits, update, category; optional reproduction and failure; open `ecosystemMetadata` escape hatch.
- Failure taxonomy: 5 top-level categories; Cargo subcategories defined.
- Python library `bump_ext`: Pydantic models, JSON Schema validator, `EntryWriter`, canonical `image_ref` helper.
- Cargo pipeline: miner, reproducer, dockerizer, classifier, assemble_entry.
- Docker contract: `ghcr.io/tudelft-rp2026/breaking-updates-<ecosystem>:<shortHash>-{pre|breaking}`.

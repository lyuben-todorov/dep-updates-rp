# Failure Taxonomy

Two distinct taxonomies live in this project. Both answer "what went
wrong," but they classify different populations of logs:

1. **Breaking-update classifier** (this section) — maps logs from
   *successful pre-build, failing post-build* candidates to the
   BUMP-shared top-level scheme. Per-entry field
   `failureClassification`. Implemented in
   `pipelines/cargo/cargo_classifier.py`. Shared across ecosystems.
2. **Reproduction-failure reclassifier** (section below) —
   post-hoc, reads the *pre* logs of candidates whose pre-build
   failed (`not_reproducible`). Implemented in
   `scripts/reclassify_failures.py`. Cargo-specific. Populates the
   `drive_state_classifications` table, not an entry-JSON field.

A candidate's outcome puts it into exactly one of these populations:

- `pre passes, post fails` → breaking → classifier runs (Scheme 1).
- `pre fails` → not_reproducible → reclassifier runs (Scheme 2).
- `pre passes, post passes` → non-breaking → no classifier.
- `pre fails, post passes` → fix-after-update → neither classifier
  runs by default; a post-hoc pass can use Scheme 1 on the
  pre-commit log to explain what the fix repaired.

---

## Scheme 1 — Breaking-update classifier (BUMP-shared)

Two-level taxonomy shared across ecosystems. The top level is closed
(all ecosystems map into it); the sub-level is open per ecosystem.

### Top-level categories

- `COMPILATION_FAILURE` — source code fails to compile after the update.
- `TEST_FAILURE` — compilation succeeds, tests fail.
- `DEPENDENCY_RESOLUTION_FAILURE` — the package manager cannot
  resolve or fetch the new dependency set.
- `ENVIRONMENT_FAILURE` — failure caused by the build environment
  (toolchain, OS, missing binaries) rather than the dependency update.
- `OTHER` — everything not captured above; must be rare.

Every ecosystem MUST map each confirmed failure to exactly one
top-level category.

### Cargo subcategories (owned by Cargo RQ)

Under `COMPILATION_FAILURE`:
- `TRAIT_BOUND_NOT_SATISFIED` — rustc E0277.
- `TYPE_MISMATCH` — rustc E0308.
- `UNRESOLVED_IMPORT` — rustc E0432.
- `UNRESOLVED_PATH` — rustc E0433.
- `NO_METHOD_FOUND` — rustc E0599.
- `MISSING_TRAIT_IMPL` — rustc E0046, E0277 (when trait not
  implemented rather than bound unsatisfied).
- `OTHER_COMPILE_ERROR` — any other rustc error code.

Under `TEST_FAILURE`:
- `ASSERTION_FAILED`
- `PANIC`
- `TEST_TIMEOUT`
- `OTHER_TEST_FAILURE`

Under `DEPENDENCY_RESOLUTION_FAILURE`:
- `CRATE_NOT_FOUND`
- `VERSION_CONFLICT`
- `LOCK_INCOMPATIBLE`

Under `ENVIRONMENT_FAILURE`:
- `TOOLCHAIN_MISMATCH`
- `EDITION_MISMATCH`
- `MISSING_SYSTEM_DEPENDENCY`

### Adding new subcategories

Ecosystem owners may add subcategories to their ecosystem's section
without a schema bump. Adding new top-level categories requires a
schema bump (major version) and team consensus.

---

## Scheme 2 — Reproduction-failure reclassifier (Cargo)

Run after a batch via `scripts/reclassify_failures.py`. Reads the
pre-commit log of every `not_reproducible` drive-state row, prioritises
the *terminal* error (the last substantive `error:` line, skipping
generic `error: build failed` and `could not compile X due to N
previous errors` summary lines), and buckets into one of the
categories below. Writes the result to
`drive_state_classifications (run_id, candidate_key, category,
subcategory, evidence)`.

The scheme is flat — no top/sub hierarchy like Scheme 1 — because the
questions it answers are operational (can pipeline work recover it?)
rather than categorical.

### Categories

| category | populated sub | meaning |
| --- | --- | --- |
| `REPO_GONE` | — | `git clone` succeeded but `Cargo.toml` is absent — the project has been renamed, moved, or gutted since the PR. Corpus-health problem; should not count as a reproduction failure against RQ1. |
| `LOCK_FILE_STALE` | — | `Cargo.lock` cannot resolve under the frozen snapshot with `--locked` — usually pinned to a yanked or archive-removed version. |
| `OPENSSL_MISMATCH` | — | `openssl-sys` / `ring` build-script fails against the fat image's libssl ABI. Dominant in 2018-2020 Cargo code under buster+ fat images. |
| `NATIVE_DEP_MISSING` | pkg name (e.g. `fuse`, `libv4l2`) | pkg-config reports "Package X was not found" for a non-OpenSSL system dep. Fixable by baking the package into a fat-image variant. |
| `NIGHTLY_REQUIRED` | aborting crate (e.g. `pear_codegen`, `rocket`) | A build.rs emits `"Aborting compilation due to incompatible compiler"`. Common for Rocket 0.3/0.4-era projects and a handful of nightly-only proc macros. Not fixable with stable-toolchain fat images. |
| `RUSTC_BITROT` | rustc error code (e.g. `E0713`, `E0283`) | Code that compiled on the author's native rustc fails on the fat image's (usually newer) rustc because of a stricter borrow-check, inference regression, or stdlib rename. |
| `RUNTIME_CRASH` | `BUILD_SCRIPT_PANIC` \| `SIGSEGV` | build.rs panic (usually author-environment assumption) or SIGSEGV inside a tool. |
| `TEST_FAILURE` | — | pre-commit tests actually ran and reported non-zero results. Often author-environment assumptions about the host (DNS, filesystem layout, specific hardware). |
| `DEPENDENCY_RESOLUTION` | — | Non-lockfile resolver failures: `failed to select a version`, `no matching package named`, registry fetch failures that survive `--locked`. |
| `NETWORK_ERROR` | — | zlib stream corruption, git fetch DNS, connect timeouts. Usually transient; worth retrying. |
| `TIMEOUT` | — | Our reproducer's `--timeout` (default 1800 s) exceeded. Heavy workspaces (libra/diem/solana) dominate this bucket. Distinguishable from OS-level SIGKILL. |
| `OLD_MESSAGE_FORMAT` | — | Old cargo (typically ≤ 1.34) rejects our `--message-format=json-diagnostic-rendered-ansi` flag. Pipeline-era issue; mostly fixed since Bug A of the 200-slice. |
| `NO_LOG` | — | Pre-log file missing on disk. Indicates a pipeline-side interruption (SIGKILL, host crash) before the log could be flushed. Not a candidate-side failure. |
| `OTHER` | — | Classifier fell through. Evidence field carries the terminal error line for manual inspection. |

### Why it is separate from Scheme 1

Scheme 1 is the BUMP-style breaking-failure taxonomy: every entry in
the reproducible cohort's breaking subset gets exactly one class. It
is narrow, closed, and cross-ecosystem.

Scheme 2 exists because **the single largest population in DS1 is
`not_reproducible`, which Scheme 1 has no place for.** Reproduction
failures don't cleanly map onto
`COMPILATION_FAILURE`/`TEST_FAILURE`/... because many are
environment-authored rather than code-authored (e.g. OpenSSL ABI drift
is environmental, but it surfaces as a compilation error; REPO_GONE
is neither a compilation nor a test nor a resolution failure in the
BUMP sense). Collapsing reproduction failures into Scheme 1 would
either expand the top-level categories beyond what BUMP uses or hide
real distinctions in sub-categories.

The two schemes therefore have different jobs:
- Scheme 1 answers the paper's *RQ2 breaking-rate* question.
- Scheme 2 answers *what fraction of the `not_reproducible` cohort is
  pipeline-fixable vs genuinely a corpus property* — a precondition
  for interpreting the headline reproducibility rate.

### Re-classifying a run

```sh
python3 scripts/reclassify_failures.py \
  --db data/pipeline.sqlite \
  --run-id ds1-full-crack \
  --logs-dir data/cargo-logs \
  --candidates data/rebatchi/ds1_candidates_enriched.jsonl
```

Idempotent — uses `ON CONFLICT DO UPDATE`. Re-run whenever the
classifier rules evolve.

### Current DS1-full breakdown (2026-05-12, post-retry sharpening)

| category | n | % of 1395 fails |
| --- | ---: | ---: |
| RUSTC_BITROT | 473 | 33.9 % |
| RUNTIME_CRASH | 352 | 25.2 % |
| OPENSSL_MISMATCH | 144 | 10.3 % |
| NIGHTLY_REQUIRED | 92 | 6.6 % |
| OTHER | 88 | 6.3 % |
| REPO_GONE | 79 | 5.7 % |
| DEPENDENCY_RESOLUTION | 75 | 5.4 % |
| LOCK_FILE_STALE | 38 | 2.7 % |
| TIMEOUT | 25 | 1.8 % |
| NATIVE_DEP_MISSING | 19 | 1.4 % |
| NETWORK_ERROR | 9 | 0.6 % |
| TEST_FAILURE | 1 | 0.1 % |

`OLD_MESSAGE_FORMAT` and `NO_LOG` are in the category list but have
zero rows on this run — they exist for operational robustness, not as
empirical findings.

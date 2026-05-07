# RP 2025/2026 Q4 — Shared Infrastructure POC

Cross-ecosystem shared infrastructure for the TU Delft Research Project
*"Mining Reproducible Dependency Updates Across Ecosystems"* (extending
BUMP). Current state: schema v0.0.4, Fork B reproducibility model
(environmental equivalence over byte-identical OCI digests).

## What this POC is

1. A **shared JSON schema** (v0.0.4) for one reproducible dependency-update
   entry — covering `breaking`, `non-breaking`, and `fix-after-update`
   categories.
2. A **shared failure taxonomy** with an ecosystem-agnostic top level and
   ecosystem-specific subcategories.
3. A **shared Python library (`bump_ext`)** with Pydantic models, JSON
   Schema validation, and an entry writer.
4. A **Cargo pipeline** exercising the shared contracts end-to-end:
   candidate generation (Rebatchi DS1 or live GitHub) → fat-image
   resolution → reproduction → classification → assembly → optional
   regenerate-verify.
5. A **fat-image toolkit** — index, resolver, deterministic canonical
   tags, build CLI — so the reproducibility story is "rebuild locally
   and verify fingerprint", not "pull a published image."

Other ecosystem owners (Maven, pip, npm) write their own pipeline against
the same schema + library. RQ1 / RQ2 consume the combined corpus.

## Reproducibility model (Fork B)

The reproducibility contract is an **environment fingerprint** — a sha256
over the concatenation of five files emitted by the fat image:
`packages.txt`, `rustc.txt`, `cargo.txt`, `os-release`, `sources.list`.
Two hosts agree on "same environment" if and only if they agree on this
hash.

Byte-identical OCI digests proved impossible in practice due to
apt-internal non-determinism even with pinned `SOURCE_DATE_EPOCH` + apt
snapshot. Rationale and evidence in
[`docs/cargo/reproducible-builds.md`](docs/cargo/reproducible-builds.md).

## Directory layout

```
schema/
  entry.schema.json         master contract, v0.0.4
  failure-taxonomy.md       shared top-level + Cargo subcategories
  examples/
    cargo-example.json      filled-in example entry
lib/
  bump_ext/                 shared Python library
    models.py               Pydantic models matching the schema
    validate.py             JSON Schema validator
    writer.py               EntryWriter
    __init__.py             SCHEMA_VERSION + re-exports
pipelines/
  cargo/
    _candidate.py           shared Candidate dataclass + GitHub helpers
    cargo_miner.py          live-GitHub PR miner
    cargo_toolchain.py      MSRV detection (file parsers + GitHub API)
    cargo_reproducer.py     pre/post/fix commit verification in Docker
    cargo_classifier.py     cargo log → failure taxonomy
    cargo_assemble_entry.py candidate + reproduction + classification → v0.0.4 entry
    cargo_regenerate.py     entry-driven rebuild + fingerprint verify
    cargo_drive.py          end-to-end driver (JSONL → entries)
    cargo_plan_fat_images.py batch planner, read-only
    fat_image.py            fat-image index + canonical bucketing + build CLI
scripts/
  cargo_survey_sys_deps.py  Cargo *-sys coverage survey (one-off analysis)
  rebatchi_to_candidate.py  Rebatchi CSV/JSONL row → candidate JSONL
  rebatchi_ds1_filter.py    rar-stream pre-filter for DS1
docker/
  cargo-fat/
    Dockerfile              parameterised on RUST_VERSION, DEBIAN_RELEASE, SOURCE_DATE_EPOCH, INCLUDE_GUI
    repro-sources-list.sh   vendored apt-snapshot pinner (locally patched for buster-era)
    index.json              inventory of registered fat images
data/
  cargo/                    submodule → lyuben-todorov/dep-updates-rp-data
                            canonical v0.0.4 entry JSONs (Zenodo-bound)
  cargo-logs/               reproducer + driver logs (gitignored)
  cargo-dockerfiles/        transient thin-image Dockerfiles (gitignored)
  pipeline.sqlite           derived query index, rebuildable (gitignored)
  rebatchi/
    ds1_cargo_candidates.jsonl        DS1 filter output (pre-enrichment)
    ds1_candidates_enriched.jsonl     full DS1, enriched (msrv + commit_date), --require-cargo filtered
    ds1_candidates_enriched_500.jsonl first 500 of DS1, kept for regression
    sample-drive/                     output of the 5-candidate smoke test
docs/
  shared/
    schema.md               schema design + field-by-field tour
    bump_ext-library.md     library API
  cargo/
    running-a-batch.md      end-to-end runbook — from a fresh checkout to verified entries
    (rebatchi + reproducibility design moved to ../docs/ at repo root)
    survey-findings.md      *-sys crate coverage survey (93% under 35 packages)
pyproject.toml
README.md
CHANGELOG.md
```

## Quick start

```bash
# 1. Clone with submodules (data/cargo/ is a submodule).
git clone --recurse-submodules <repo-url> && cd dep-updates-poc
# If you already cloned: git submodule update --init

# 2. Install the shared library (editable, with Cargo extras).
pip install -e '.[cargo]'

# 3. Set a GitHub token (for candidate enrichment + commit-date lookups).
export GITHUB_TOKEN=<your_pat>

# 4. Run a small batch end-to-end against the bundled test slice.
python3 -m pipelines.cargo.cargo_drive \
  --candidates data/rebatchi/ds1_candidates_enriched_500.jsonl \
  --out-dir /tmp/drive-out \
  --logs-dir /tmp/drive-logs \
  --state /tmp/drive-state.jsonl \
  --build-missing-bases \
  --limit 5 \
  --host $(hostname)
```

For the full end-to-end workflow (planning → fat-image builds →
batch drive → verification), see
[`docs/cargo/running-a-batch.md`](docs/cargo/running-a-batch.md).

## Proof it works

Two real v0.0.4 entries live in the `data/cargo/` submodule
([`lyuben-todorov/dep-updates-rp-data`](https://github.com/lyuben-todorov/dep-updates-rp-data)):

- `cargo-9ac20c07.json` — `fstubner/netscli#22`, Dependabot
  `ipnetwork 0.20 → 0.21`. Category `breaking`
  (COMPILATION_FAILURE / TYPE_MISMATCH / E0308). Reproduced under
  `rp2026/cargo-fat:1.92.0-bookworm-20260427`.
- `cargo-f82e5be0.json` — `passy/revmenu#21`, Dependabot-preview
  `im 10.2.0 → 12.3.1`. Category `breaking`. Reproduced under
  `rp2026/cargo-fat:1.56.0-buster-20211022`.

## Key design decisions

| Decision | See |
| --- | --- |
| Environment fingerprint over OCI digest | [`docs/cargo/reproducible-builds.md`](docs/cargo/reproducible-builds.md) |
| Fat image covers ~93% of Cargo *-sys crates | [`docs/cargo/survey-findings.md`](docs/cargo/survey-findings.md) |
| Dataset 1 over Dataset 2 for the paper corpus | [`../docs/rebatchi.md`](../docs/rebatchi.md) |
| Canonical fat-image tags (`<rust>-<debian>-<yyyymmdd>`) | `pipelines/cargo/fat_image.py` |
| `pre` / `post` / `fix` commit naming (not `preBreaking`/`breaking`) | `CHANGELOG.md` v0.0.4 |

## Status

**v0.0.4 — category-neutral schema + fat-image internals refactor +
SQLite index layer.**
Full DS1 enrichment (2608 candidates) completed; plan proposes 4 fat
images to cover the dataset. Layer 1 extracted to its own repo
(`dep-updates-rp-data`, wired in as a submodule at `data/cargo/`).
`PipelineDB` / `rebuild_index.py` / `verify_index.py` shipped;
`cargo_drive` has optional `--db` mirror. Next milestones: 500-slice
dry run, then the full DS1 batch on a VM; deploy script for a fresh
machine.

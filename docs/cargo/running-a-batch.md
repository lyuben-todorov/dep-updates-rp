# Running a Cargo batch — fresh-checkout runbook

From a freshly-cloned repo to verified entries. Written for a second
machine picking up the project: Lyuben's VM, a TU Delft CI runner, or
just this laptop reset.

Target audience: someone who has the repo + a recent Linux/macOS + Docker
+ a GitHub token, and wants to run the DS1 batch unattended.

## Prerequisites

| Thing | Version | Why |
| --- | --- | --- |
| Docker Desktop or Engine | 27.x+ (buildx v0.20+) | Fat-image builds use `buildx`, `--output type=image,rewrite-timestamp=true`, which needs a docker-container driver |
| Python | 3.11+ | `tomllib` stdlib module |
| Disk (Docker VM) | ≥ 80 GB free | Fat images are 2-3 GB each; thin-image build cache adds ~5 GB per candidate |
| Disk (host) | ≥ 30 GB free | Rebatchi rar archives + extracted JSON if you're re-running DS1 filter |
| GitHub PAT | read-only | 5000 req/hour vs 60 unauthenticated |

```bash
# Verify prerequisites
docker version --format '{{.Server.Version}}'   # want 27.x+
docker buildx version                           # want 0.20+
python3 --version                               # want 3.11+
df -h ~                                         # check available disk
```

## Step 1 — Clone + install

```bash
git clone <repo-url> rp2026 && cd rp2026/dep-updates-poc
pip install -e '.[cargo]'
```

Dependencies pulled: `pydantic>=2`, `jsonschema`, `requests`,
`tomllib` (stdlib on 3.11+).

## Step 2 — Configure GitHub token

```bash
# Create a fine-grained PAT at github.com/settings/tokens with
# "public repo: read" scope. Paste below.
cat > .env <<'EOF'
GITHUB_TOKEN=ghp_...your-token-here...
EOF

# Source into the shell (need to do this in each new shell)
set -a; . .env; set +a
```

Verify:

```bash
curl -sf -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://api.github.com/rate_limit \
  | python3 -c "import json,sys; d=json.load(sys.stdin)['resources']['core']; print(f'remaining={d[\"remaining\"]}/{d[\"limit\"]}')"
# Expected: remaining=5000/5000
```

## Step 3 — Pick a candidate set

Two options.

### Option A: use the committed enriched JSONL (fastest)

The repo ships with `data/rebatchi/ds1_candidates_enriched.jsonl`
(2608 candidates, already filtered by `--require-cargo` and enriched
with `rust_msrv` + `post_commit_date`). Jump to step 4.

### Option B: regenerate from raw DS1

Only needed if you want a fresh enrichment (e.g., to pick up new
`edition` parsing logic). Costs ~4-5 hours and ~20k GitHub API calls.

```bash
# 1. Download DS1 (from Rebatchi's Zenodo drop, ~3.7 GB)
#    See ../../../docs/rebatchi.md for the URL + filter recipe.

# 2. Filter to candidates (streams the rar archives, keeps plausible
#    Cargo PRs)
python3 scripts/rebatchi_ds1_filter.py \
  --dataset-dir data/rebatchi/Dataset \
  --out data/rebatchi/ds1_cargo_candidates.jsonl

# 3. Enrich with GitHub API (SHAs, MSRV, commit date, --require-cargo filter)
python3 scripts/rebatchi_to_candidate.py \
  --jsonl data/rebatchi/ds1_cargo_candidates.jsonl \
  --require-cargo \
  --source rebatchi-ds1 \
  --out data/rebatchi/ds1_candidates_enriched.jsonl
```

`rebatchi_to_candidate.py` is **not resumable** as of v0.0.4 — if it
crashes mid-run you restart from row 0. For overnight runs, wrap it in
`nohup` + redirect stderr.

## Step 4 — Plan the fat images

Read-only. Shows which images you need to build.

```bash
python3 -m pipelines.cargo.cargo_plan_fat_images \
  --candidates data/rebatchi/ds1_candidates_enriched.jsonl
```

Expected output for the full DS1 (as of 2026-05-06, default
`max_sde_date=2025-12-31`):

```
Run parameters:
  max_sde_date:                  2025-12-31
Candidates read:                 2608
Buckets (rust milestone × year × debian):    6
Proposed fat images:             4
  existing reused:               0
  new builds:                    4
Covers candidates:               2607 / 2608  (100.0%)
```

The largest proposal (`1.49.0-buster-20210209`) serves 2405 candidates
— all 2018/2019/2020 buster buckets dedupe into this one tag because
their canonical SDEs all clamp up to `rust:1.49.0-buster`'s publication
date.

The planner prints exact `fat_image build` commands for each
to-be-built image. Flags to watch for:

- `pre_rust_base` — bucket's year ends before the rust base image was
  published on Docker Hub; SDE clamped forward to the publication
  date. Image will build, but ABI era will be off from the commit era.
  Expected for 2018-2020 buster buckets.
- `rust_base_unknown` — Docker Hub doesn't have a
  `rust:<ver>-<debian>` tag. Build will fail. Pick a different rust
  milestone or debian release.

## Step 5 — Build fat images

Each fat image takes ~5-10 minutes and uses ~3 GB disk. Run from the
planner's command list.

```bash
# Example for the full-DS1 plan:
python3 -m pipelines.cargo.fat_image build \
  --rust-version 1.56.0 --debian-release buster \
  --source-date-epoch 1634860800

python3 -m pipelines.cargo.fat_image build \
  --rust-version 1.56.0 --debian-release buster \
  --source-date-epoch 1640908800

# Each build auto-registers into docker/cargo-fat/index.json.

# Verify all expected images are present:
python3 -m pipelines.cargo.fat_image list
```

Skip the builds that show up as `existing reused` in the plan — those
are already in the index (e.g., `1.56.0-buster-20211022` is committed
seed-built).

**Optional**: `--include-gui=0` to skip the GTK/Tauri stack
(automatically disabled for bullseye/buster since those packages
don't exist there; bookworm+ gets GUI by default).

## Step 6 — Drive the batch

```bash
mkdir -p data/cargo/logs data/rebatchi/batch

python3 -m pipelines.cargo.cargo_drive \
  --candidates data/rebatchi/ds1_candidates_enriched.jsonl \
  --out-dir data/cargo/ \
  --logs-dir data/cargo/logs/ \
  --state data/rebatchi/batch/drive-state.jsonl \
  --timeout 1800 \
  --host $(hostname) \
  2>&1 | tee data/rebatchi/batch/drive.log
```

Per-candidate work:
- ~5 min reproduction (pre + post commits inside Docker, `cargo test`).
- Instant classification + assembly.

Full DS1 expected wall time: **3-4 days of Docker time** assuming
average ~5 minutes per candidate × 2608 candidates. Bound by Docker's
per-build serialization.

Can stop + resume — on restart, candidates with a terminal status in
the state JSONL are skipped.

### Status distribution you should expect

From the 500-candidate sample:
- ~20% `ok` (entries written; mostly `breaking`)
- ~75% `not_reproducible` with `pre=0, post=0` (non-breaking
  Dependabot PRs — the dependency bump compiled cleanly, no regression)
- ~3-5% `not_reproducible` with other patterns (pre fails = old toolchain
  can't build the project at all; env errors)
- handful `fat_image_missing` or `metadata_fetch_failed` (tolerable tail)

Non-breaking PRs aren't a pipeline failure — they're data. The current
driver marks them `not_reproducible` because we only emit entries for
outcomes that match the schema's reproducibility contract for their
category. (Future work: emit non-breaking entries too.)

## Step 7 — Verify (optional)

Sanity-check a handful of produced entries by re-running them through
the regenerator:

```bash
for entry in data/cargo/cargo-*.json; do
  python3 -m pipelines.cargo.cargo_regenerate \
    --entry "$entry" --host $(hostname) --skip-tests
done
```

`--skip-tests` runs just the fingerprint check (fast). Drop the flag
to re-run the full `cargo test` pair (slow, rebuilds thin images).

Each run appends a `verifiedOn` record to the entry. Over time, the
entry accumulates cross-host verifications that become the paper's
reproducibility-rate evidence.

## Operational concerns

### Rate limits

GitHub: 5000 req/hour authenticated. `rebatchi_to_candidate.py`
burns ~5 per candidate — at 2608 candidates, you'll hit the limit
twice. The script has retry logic (`rate-limited, sleeping Ns`), so
just let it run; or split the input.

Docker Hub: 100 pulls/6-hour anonymous. Authenticate if building many
fat images at once (`docker login`).

### Disk

```bash
# Check Docker VM disk usage
docker system df

# Reclaim: delete stopped containers, dangling images, build cache
docker container prune -f
docker image prune -f
docker builder prune -af
```

Each fat image is ~3 GB; thin-image build cache adds ~5 GB per driver
run per candidate. Full DS1 can consume ~100 GB if you don't GC
periodically. Set a cron or run `docker builder prune -af` after every
N candidates.

### Resume

```bash
# Just re-run the same command. Terminal status in state JSONL =
# skip that candidate.
python3 -m pipelines.cargo.cargo_drive \
  --candidates ... --state data/rebatchi/batch/drive-state.jsonl ...
```

To re-process a candidate (e.g., after fixing the pipeline), delete its
line from the state file.

### Parallelism

The `--parallel N` flag in `cargo_drive.py` is a stub — it prints a
warning and serializes anyway. Docker builds serialize on the daemon,
so real parallelism requires multiple Docker engines. Not worth
engineering yet.

## Troubleshooting

### "fat image not present locally" / `EXIT_FAT_IMAGE_MISSING`

Either:
- The planner proposed a new build you haven't run yet → run it.
- You're re-driving entries from another host and the local index
  doesn't have the tag → use `--build-missing-bases` on
  `cargo_drive.py` / `cargo_regenerate.py` to build on demand.

### "environment fingerprint mismatch"

`cargo_regenerate.py` rebuilt the fat image and got a *different*
`/manifest/*` fingerprint than what the entry was validated against.
Likely causes, ordered by frequency:

1. The `docker/cargo-fat/Dockerfile` changed between when the entry was
   produced and now. Check `git log docker/cargo-fat/`.
2. The vendored `repro-sources-list.sh` was updated. Check its
   `sha256sum`.
3. `snapshot.debian.org` evicted the date being requested (rare but
   documented for very old buster snapshots).
4. Docker Buildx version skew changed layer metadata in ways the
   fingerprint picks up. Compare `docker buildx version` across hosts.

Next step: `cargo_regenerate.py` prints a per-file diff. Inspect which
manifest file changed; the name tells you the category of change.

### apt install fails on old buster fat images

`snapshot.debian.org` has spotty coverage for `buster-security` dates
before ~2020-Q2. If you need a fat image with `SOURCE_DATE_EPOCH <
2020-04-01` on buster, test the exact date first:

```bash
curl -sI http://snapshot.debian.org/archive/debian-security/YYYYMMDDT000000Z/dists/buster/updates/Release
```

404 → pick a later SDE.

### "Docker daemon out of space" mid-batch

```bash
docker system df                # see what's taking it
docker builder prune -af        # reclaims ~30 GB typically
docker image prune -f           # dangling images
```

If still full, shrink Docker's VM disk image in Docker Desktop
settings (trickier on Linux).

### "rust_base_unknown" warning in planner

Docker Hub doesn't have a `rust:X.Y.Z-<debian>` tag for that combination.
Check Hub manually:

```bash
curl -sI https://registry.hub.docker.com/v2/repositories/library/rust/tags/<tag>
```

Fix: pick a different Debian release, or pick a rust version that
existed on that release. See
<https://hub.docker.com/_/rust/tags> for available combinations.

## Expected artifacts at the end

```
data/cargo/
  cargo-<hash>.json        one per ok-status candidate (hundreds to low thousands)
  logs/
    <hash>-pre.log         cargo test output for pre commit
    <hash>-post.log        cargo test output for post commit
    <hash>-fix.log         only for fix-after-update entries

data/rebatchi/batch/
  drive-state.jsonl        per-candidate: {status, fat_image_tag, reason, timestamp}
  drive.log                full stderr stream

docker/cargo-fat/
  index.json               append-only registry of built fat images
```

Cross-check at end of run:

```bash
# Status distribution
cut -d'"' -f6 data/rebatchi/batch/drive-state.jsonl | sort | uniq -c | sort -rn

# How many entries produced
ls data/cargo/cargo-*.json | wc -l

# Which fat images got used
jq -r '.fat_image_tag' data/rebatchi/batch/drive-state.jsonl | sort | uniq -c
```

## Shipping the results

Entries + state file + logs + fat-image index are the full benchmark
artifact. Zip `data/cargo/*.json`, `data/rebatchi/batch/*.jsonl`,
`docker/cargo-fat/index.json` → Zenodo DOI. Images themselves never
shipped (supervisor's directive); anyone with the DOI + the repo can
regenerate the images locally from the index.

## When things finish

Next logical steps (not covered here):

1. Populate a SQLite index over the entries for easy querying. See
   `../../../docs/db-design.md`.
2. Cross-host regenerate-verify on a different architecture (x86_64 if
   the batch ran on arm64, or vice versa). Each run appends to
   `verifiedOn[]`; after N hosts verify an entry, it's
   cross-host-reproducible.
3. Write up the reproduction rate per (year, milestone, debian) cell.
   Drive-state's `status` + `fat_image_tag` is the data you need.

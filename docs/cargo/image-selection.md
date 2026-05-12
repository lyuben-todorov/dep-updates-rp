# Fat-image selection — how a PR becomes a tag

Given a candidate PR, how do we decide which environment(further referred to as "fat image") it gets built
against? The full chain is four pure functions, all in
[`pipelines/cargo/fat_image.py`](../../pipelines/cargo/fat_image.py):

```
(rust_msrv, post_commit_date, debian)
        │
        ▼
  bucket_for   ────►  BucketKey(milestone, year, debian)
        │                        │
        ▼                        ▼
canonical_sde_for         (milestone decides rust)
  CanonicalSde(sde,       (debian decides OS era)
    pre_rust_base, …)     (year decides apt snapshot)
        │
        ▼
    tag_for   ────►  rp2026/cargo-fat:<milestone>.0-<debian>-<yyyymmdd>
```

Every bucket produces exactly one tag. Reuse is pure tag-equality: if
the tag exists in the index, use it; else build it.

## Step 0 — the run parameter `max_sde_date`

Before bucketizing, the planner and driver enforce one upper bound:

- `--max-sde-date YYYY-MM-DD` on both CLIs. Default: Dec 31 of last
  year (`fat_image.default_max_sde_date()`).
- Candidates whose `post_ commit_date` is later than `max_sde_date`
  are rejected — planner reports them as `commit_too_recent`, driver
  parks them with status `commit_after_max_sde_date`.

This is the only place the pipeline knows about "recent." All
downstream functions are pure functions of the bucket — they don't
consult `today()`, and their outputs are fixed forever once
`max_sde_date` is chosen. That makes the fat-image tag we pick for a
given PR deterministic across machines and across time, as long as
the run's `max_sde_date` stays pinned.

`max_sde_date` is a **run-level parameter**: one value per research
run, threaded from the CLI → driver → planner → `canonical_sde_for`.
See `docs/db-design.md` for how runs will be keyed in the DB — each
row in `drive_state` / `reproduction_attempts` / etc. will carry a
`run_id` that references the `max_sde_date` used.

## Step 1 — `bucket_for(msrv, commit_date, debian)`

Picks the `BucketKey` that identifies which fat image this PR needs.

### Picking the milestone

The rust milestone is the smallest member of
`MILESTONES = ["1.39", "1.49", "1.56", "1.65", "1.75", "1.85", "1.92"]`
whose rustc can compile the code. `1.39` was added in 2026-05-12 to
anchor the async/await cliff (async fn syntax stabilised in 1.39,
parsing fails on 1.38-), making it a natural home for 2018-2019 Cargo
code that otherwise had to jump straight to 1.49.

- **If the candidate declares an MSRV** (from `rust-toolchain.toml` →
  `rust-toolchain` → `Cargo.toml:rust-version`), round it *up* to the
  next milestone. `1.50 → 1.56`.
- **If no MSRV is declared**, pick the latest milestone that had shipped
  by the PR's commit date (`latest_milestone_before`). A 2020 PR with
  no rust-version lands on `1.49` (released Dec 31 2020 — just before
  the bucket year ended). A 2022 PR lands on `1.56` or `1.65` depending
  on when in the year.

Latest-milestone-at-commit is a better guess than edition-floor
(which we had briefly): edition-2018 code can technically compile on
`1.31` but in practice uses features from whatever rust was current at
writing time. Edition-floor understates what the code needs.

### Picking the debian

`debian_release_for(commit_date)` in `cargo_toolchain.py`. Hardcoded
cutovers at the actual Debian release dates:

| Commit date range | Debian |
| --- | --- |
| before 2019-07-06 | stretch |
| 2019-07-06 → 2021-08-14 | buster |
| 2021-08-14 → 2023-06-10 | bullseye |
| 2023-06-10 → 2025-08-09 | bookworm |
| ≥ 2025-08-09 | trixie |

`stretch` routing was added in 2026-05-12 to pair 1.39 with an era-
appropriate libssl-dev. Debian 9 stretch ships both libssl 1.0.2 and
libssl 1.1.0, whereas buster jumps to 1.1.1 — and 2018-2019 `openssl-sys`
crates pinned against 1.0.x ABI don't link against 1.1.1 headers.

The planner and the driver call this helper — the caller doesn't have
to know about the cutovers.

### Rerouting when `(milestone, debian)` isn't published

Docker Hub doesn't build every `rust:<milestone>-<debian>` pair. Buster
stops at 1.75, bullseye starts at 1.54, bookworm starts at 1.67, and
so on. The real support grid (probed 2026-05-05):

```
milestone  stretch  buster   bullseye  bookworm  trixie
1.39       ✓        ✓        —         —         —
1.49       —        ✓        —         —         —
1.56       —        ✓        ✓         —         —
1.65       —        ✓        ✓         —         —
1.75       —        ✓        ✓         ✓         —
1.85       —        —        ✓         ✓         —
1.92       —        —        ✓         ✓         ✓
```

The grid is hardcoded as `MILESTONE_DEBIAN_SUPPORTED` in `fat_image.py`
— refreshed only when Docker Hub adds a new track.

When `bucket_for` hits an unsupported pair, it **reroutes upward**:
keep the debian (which records the commit's OS era), bump the
milestone to the smallest supported one on that debian.

Examples:

| Requested | Rerouted | Why |
| --- | --- | --- |
| `(1.49, buster)` | `(1.49, buster)` | direct hit |
| `(1.49, bullseye)` | `(1.56, bullseye)` | no `rust:1.49.0-bullseye` on Hub; bump rust |
| `(1.49, bookworm)` | `(1.75, bookworm)` | bookworm starts at 1.67 → smallest on-track is 1.75 |
| `(1.85, buster)` | **None** | buster ends at 1.75; no supported upward bump |
| `(1.92, buster)` | **None** | same |

When `bucket_for` returns None, the candidate is unbucketable — the
planner reports it; the driver parks it with a status reason.

Why upward on milestone, not sideways on debian? The entry records
which environment the reproduction ran in (`reproduction.fatImage.
debianRelease` et al.). Whichever axis we adjust, the entry's claim
has to stay true to the code's era.

- **Bumping milestone up** changes the rust toolchain from — say —
  1.49 to 1.56. The code's MSRV constraint was "≥ 1.49"; rust is
  backward-compatible, so a 1.56 rustc compiles anything a 1.49
  project needs. The entry's `rustVersion: "1.56.0"` is a correct
  statement about what was used. The underlying OS libraries
  (`libssl`, `glibc`, every `-dev` package) stay on the debian
  release that matches the commit's era.
- **Bumping debian sideways** (e.g. bullseye → buster for a 2022
  PR) changes the OS environment to a release that was EOL before
  the PR was written. The entry's `debianRelease: "buster"` would
  claim we reproduced the 2022 code on a 2019-era Debian that
  nobody was actually targeting in 2022. Every `-dev` package's
  major version could be wrong; C ABI across Debian majors is not
  a stable thing.

The rust bump is a true statement we can live with. The debian
sideways move is a false statement about what environment the code
was paired with. Bump rust.

## Step 2 — `canonical_sde_for(BucketKey)`

Turns a bucket into a canonical SDE (the Unix timestamp that tells apt
which `snapshot.debian.org` snapshot to use).

The rule in one sentence: **use December 31 of the bucket's year,
raised up to `rust_base_pub` if the bucket predates the Rust image**.

Two cases:

```
target = Dec 31 of bucket.year
lower  = publication date of rust:<milestone>.0-<debian> on Docker Hub

target ≥ lower  → SDE = target  (common case)
target < lower  → SDE = lower,  pre_rust_base = True
```

**Why the lower wall:** the Rust Docker image has specific apt package
versions baked in (often security-fixed versions). An apt snapshot
from before the image's publication date doesn't have those versions
→ apt can't reconcile → build fails at install time.

**No upper wall, because `max_sde_date` is enforced upstream.** The
planner (`bucketize`) and driver (`process`) reject any candidate
whose `commit_date` is later than the run's `max_sde_date`. By the
time we're computing an SDE, every bucket we see has a year whose
Dec 31 is already in the past — so `target` is never in the future.

**Why not clamp against `today` here?** Two reasons. First,
`canonical_sde_for` is a pure function of `(bucket,)`; reading
`today` would make outputs drift day-to-day, breaking reuse. Second,
the decision "is this PR recent enough to include in this run?" is a
run-level policy question, not an SDE-picking question — it belongs
to the run parameter `max_sde_date` (see `docs/db-design.md`'s run
cardinality section).

**The `pre_rust_base` flag** is a sticky note on the output. It means
the PR's year-end predates the Rust image on Docker Hub, so the SDE is
years later than the PR's era — expect OS drift in apt package
versions.

## Step 3 — `tag_for(BucketKey, sde)`

Pure string formatting: `rp2026/cargo-fat:<milestone>.0-<debian>-<yyyymmdd>`.

Examples:

- `bucket_for("1.31", 2020-06-15, "buster")` → `(1.49, 2020, buster)`
  → SDE lifted to 2021-02-09 (rust_base_pub for 1.49 on buster) →
  `rp2026/cargo-fat:1.49.0-buster-20210209`, `pre_rust_base=True`.

- `bucket_for(None, 2022-03-14, "bullseye")` → `(1.56, 2022, bullseye)`
  (latest milestone before 2022-03-14 is 1.56) → target NYE 2022
  between walls → `rp2026/cargo-fat:1.56.0-bullseye-20221231`.

- `bucket_for("1.92", 2026-04-27, "bookworm")` →
  **rejected upstream** when the run's `max_sde_date` is the default
  (Dec 31 of last year). To include this candidate, override:
  `cargo_drive --max-sde-date 2026-05-04 ...`. With that override,
  the candidate bucketizes to `(1.92, 2026, bookworm)`, target NYE
  2026 raises `pre_rust_base=False` (rust:1.92.0-bookworm published
  2026-01-13), and since Dec 31 2026 is in the future the run should
  really have been configured with `max_sde_date ≥ 2026-12-31`. In
  practice, ingesting current-year PRs means waiting for the year to
  close or accepting that bucketize will reject them.

## Step 4 — reuse vs. build

Given a tag, `plan()` (in `cargo_plan_fat_images.py`) looks it up in
`docker/cargo-fat/index.json`:

- **Tag in index** → proposal marked "reuse existing." No build needed.
- **Tag not in index** → proposal marked "build." The planner prints
  the exact command.

`cargo_drive.py` does the same check per candidate: if the resolved
tag isn't locally built, it either builds it (when
`--build-missing-bases`) or parks the candidate with
`fat_image_missing`.

## Group-by-tag — when distinct buckets produce the same image

One bucket = one proposed image, **except** when two buckets' canonical
SDEs clamp to the same `rust_base_pub`. In that case both buckets
produce the exact same tag — same rust, same debian, same SDE — and
the planner groups them into one proposal whose `absorbed` list
records the source buckets.

Example on DS1: buckets `(1.49, 2018, buster)`, `(1.49, 2019, buster)`,
and `(1.49, 2020, buster)` all have Dec 31 of their year earlier than
`rust:1.49.0-buster` was published (2021-02-09). `canonical_sde_for`
lifts all three SDEs to 2021-02-09 → same tag
`rp2026/cargo-fat:1.49.0-buster-20210209`. The planner prints one
proposal "serves 3 buckets, 2405 candidates."

This is the only cross-bucket collapsing the planner does. We do not
merge buckets that differ in milestone (e.g. `(1.49, 2020, buster)`
stays separate from `(1.56, 2020, buster)`) even though the higher
milestone would technically cover both. Each `(milestone, year, debian)`
bucket records the minimum rust it genuinely needed; the entry's
`reproduction.fatImage.rustVersion` is the faithful value for that
candidate, not an upward-rounded one.

Cost: a few extra images where two milestones happen to fire in the
same `(year, debian)`. Benefit: simpler policy and honest per-entry
data. We may add milestone-collapsing later if disk pressure warrants
it; for now we keep it simple until reproduction rates tell us
something we didn't know.

## Known failure modes

### Rejected: `commit_too_recent` / `commit_after_max_sde_date`

The candidate's `post_commit_date` is later than the run's
`max_sde_date`. Happens when:

- The run defaults `max_sde_date = Dec 31 of last year` and the PR is
  from the current year. Expected for live-mining runs in H1 of a
  year — either wait for year-end or override
  `--max-sde-date <today-or-earlier>` knowing the current-year run is
  a "pilot" (see the warning in Step 2 about Dec 31 of the current
  year being in the future).
- The candidate came from an older corpus (Rebatchi DS1 ends 2021-06)
  but the run's `max_sde_date` was set too tight.

Raising `--max-sde-date` includes more candidates. Keep it pinned for
the duration of a research run so tags don't drift.

### Unbucketable: no milestone supports the debian

`(1.85, buster)` and `(1.92, buster)` fail outright. Buster's
rust track stops at 1.75. There's no upward bump available. The PR is
reported as unbucketable.

**In practice we should never see these on real data.**
`debian_release_for` and `latest_milestone_before` are keyed off the
same `commit_date`, and their cutoffs move together: a commit_date
that routes to buster (pre-2021-08) also routes to a milestone ≤ 1.56
via `latest_milestone_before`. The only ways to land on
`(1.85, buster)` in practice are:

- a candidate whose `Cargo.toml` explicitly declares
  `rust-version = "1.85"` with a pre-2021 commit date (implausible —
  1.85 didn't exist then), or
- a Docker Hub track change we haven't propagated into
  `MILESTONE_DEBIAN_SUPPORTED`, or
- a bug in `debian_release_for` / `latest_milestone_before`.

Any of those is diagnostic information worth surfacing. The two rows
are in the example table for completeness — they document what
happens in the impossible case — not because we expect them to fire.

### Unbucketable: MSRV exceeds the milestone list

If a candidate declares `rust-version = "2.0"` or something beyond
1.92, `round_up_to_milestone` returns None. No upward bump. Add a new
entry to `MILESTONES` and `MILESTONE_RELEASE_DATES` when this starts
happening.

### `pre_rust_base` flag is noisy but honest

All 2018-2020 buster buckets will be flagged. That's ~95% of DS1.
The flag isn't a failure — it says "the OS env in the fat image is
from 2021, the code is from before 2021." We're deliberately using a
later-than-ideal OS for ancient code because no earlier Rust image
exists. The empirical reproduction rate under those images is what
the paper reports.

## Refresh the support grid

If Docker Hub adds a new rust-debian track (e.g. `rust:1.97.0-trixie`
becomes available):

```bash
# Quick probe — prints the current grid
python3 -c "
from pipelines.cargo.fat_image import _rust_base_published_cached, MILESTONES
for rust in MILESTONES:
    row = f'{rust:<5}'
    for debian in ['buster', 'bullseye', 'bookworm', 'trixie']:
        d = _rust_base_published_cached(f'{rust}.0', debian)
        row += f'  {debian}: {d.isoformat() if d else \"—\":<12}'
    print(row)
"
```

Then edit `MILESTONE_DEBIAN_SUPPORTED` in `fat_image.py` to match, and
re-run the planner to see how coverage shifted.

## Cheat sheet

| Input | Function | Output |
| --- | --- | --- |
| (run) | `default_max_sde_date()` | `Dec 31 of last year` |
| `(commit_date, max_sde_date)` | upstream rejection in `bucketize` / `process` | pass-through or `commit_too_recent` |
| `(msrv, commit_date)` | `round_up_to_milestone` / `latest_milestone_before` | milestone |
| `commit_date` | `debian_release_for` | debian |
| `(milestone, debian)` | `_reroute_to_supported` | milestone on this debian (or None) |
| `(msrv, commit_date, debian)` | `bucket_for` | `BucketKey(milestone, year, debian)` or None |
| `BucketKey` | `canonical_sde_for` | `CanonicalSde(sde, date, pre_rust_base, rust_base_unknown)` |
| `(BucketKey, sde)` | `tag_for` | canonical tag string |

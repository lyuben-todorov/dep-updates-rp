# Shared Schema — v0.0.4 tour

Authoritative file:
[`schema/entry.schema.json`](../../schema/entry.schema.json).
Failure taxonomy:
[`schema/failure-taxonomy.md`](../../schema/failure-taxonomy.md).

One JSON file = one reproducible dependency-update entry. Every ecosystem
(Cargo, Maven, pip, npm) produces entries in this shape. RQ1 and RQ2
consume the union.

## Purpose

The schema is the **only contract** between ecosystems. Pipelines can be
written in any language as long as their output validates against
`entry.schema.json`.

## Top-level shape

```
id                      — "<ecosystem>-<shortHash>", globally unique
schemaVersion           — semver, e.g. "0.0.4"
ecosystem               — cargo | maven | pip | npm (closed enum)
category                — breaking | non-breaking | fix-after-update | unreproducible
project                 — { url, organisation, name }
pr                      — { url, number, author, authorType, botType, merged, mergedAt }
commits                 — { pre, post, fix?, preAuthorType?, postAuthorType?, fixAuthorType? }
update                  — { dependencyName, previousVersion, newVersion,
                            versionUpdateType, scope }
reproduction            — { fatImage, buildFlags, environmentFingerprint,
                            thinImages?, verifiedOn[] }   (null when unreproducible)
failure                 — { topCategory, subCategory, errorCodes }   (null for non-breaking)
ecosystemMetadata       — open object, per-ecosystem extras
unreproducibilityReason — enum (only when category == unreproducible)
```

## Field-by-field

### `id`

`<ecosystem>-<first-8-chars-of-post-commit>`. Deterministic — same
post-commit, same ID. Collision-resistant at benchmark scale (low
thousands of entries).

### `schemaVersion`

Semver. Current: `"0.0.4"`. Incompatible entries must be migrated
before consumption.

### `category`

Four values, each with a distinct reproduction pattern:

| Category | pre commit | post commit | fix commit |
| --- | --- | --- | --- |
| `breaking` | must pass | must fail | (n/a) |
| `non-breaking` | must pass | must pass | (n/a) |
| `fix-after-update` | must pass | must fail | must pass |
| `unreproducible` | — | — | — |

The category is **discovered** from reproducer exit codes, not declared
up front. For `fix-after-update`, candidate producers must supply a
`fix_commit` — we don't yet have a detection recipe for it at mining
time.

### `commits`

```json
{
  "pre": "<sha>",
  "post": "<sha>",
  "fix": "<sha-or-null>",
  "preAuthorType": "human | bot | null",
  "postAuthorType": "human | bot | null",
  "fixAuthorType": "human | bot | null"
}
```

Renamed from v0.0.3's `preBreaking` / `breaking`: the old names baked
"this is a breaking update" into the shape, which the schema now has
to carry across non-breaking and fix-after-update categories too.

### `update`

```json
{
  "dependencyName": "ipnetwork",
  "previousVersion": "0.20",
  "newVersion": "0.21",
  "versionUpdateType": "major | minor | patch | other",
  "scope": "runtime | dev | build | test | other"
}
```

`versionUpdateType` requires a three-part semver to classify; two-part
versions land in `other`.

### `reproduction`

The core of the Fork B reproducibility contract. Null when the entry is
`unreproducible`.

```json
{
  "fatImage": {
    "rustVersion": "1.56.0",
    "sourceDateEpoch": 1634860800,
    "aptSnapshot": "20211022T000000Z",
    "debianRelease": "buster",
    "expectedDigest": "sha256:…"
  },
  "buildFlags": ["--locked", "--offline"],
  "environmentFingerprint": {
    "digest": "sha256:…",
    "files": [
      {"path": "/manifest/packages.txt", "sha256": "…", "bytes": 22866},
      {"path": "/manifest/rustc.txt",     "sha256": "…", "bytes": 197},
      {"path": "/manifest/cargo.txt",     "sha256": "…", "bytes": 329},
      {"path": "/manifest/os-release",    "sha256": "…", "bytes": 267},
      {"path": "/manifest/sources.list",  "sha256": "…", "bytes": 326}
    ],
    "rustcVersion": "rustc 1.56.0 (09c42c458 2021-10-18)",
    "packageCount": 456
  },
  "thinImages": {
    "expectedPre": "sha256:…",
    "expectedPost": "sha256:…",
    "expectedFix": null
  },
  "verifiedOn": [
    {
      "platform": "darwin/arm64",
      "host": "macbook-local",
      "verifiedAt": "2026-05-03T12:13:24.548795Z",
      "fingerprintMatch": true,
      "fatImageDigestMatch": null,
      "outcomeMatch": true
    }
  ]
}
```

- **`fatImage`** is the input contract for `docker build`. Anyone with
  the Dockerfile + these four fields can rebuild the same image.
  `expectedDigest` is advisory only (OCI layer bytes jitter even under
  pinned inputs).
- **`buildFlags`** — exact flags passed to `cargo test`. Recorded so an
  updated pipeline can't silently change them.
- **`environmentFingerprint`** — the reproducibility check. The fat
  image emits `/manifest/*` in a fixed order; consumers rebuild,
  re-extract, recompute the sha256, assert equality. **Mismatch is a
  hard fail.**
- **`thinImages`** — optional advisory digests for the per-entry
  `<hash>-pre` / `<hash>-post` / `<hash>-fix` images. Mismatch is *not*
  a fail; this is for cross-host reproducibility studies.
- **`verifiedOn`** — appended to by `cargo_regenerate.py` each time an
  entry is re-verified. `fingerprintMatch: true` is required;
  `outcomeMatch: true` means the pass/fail pattern matched the
  category.

### `failure`

Present when the entry has a failing commit (breaking or
fix-after-update's middle step). Null for non-breaking and
unreproducible.

```json
{
  "topCategory": "COMPILATION_FAILURE | TEST_FAILURE | DEPENDENCY_RESOLUTION_FAILURE | ENVIRONMENT_FAILURE | OTHER",
  "subCategory": "TYPE_MISMATCH",
  "errorCodes": ["E0308"]
}
```

`topCategory` is closed; `subCategory` is an open string (each ecosystem
documents its own enum in `failure-taxonomy.md`).

### `unreproducibilityReason`

Enum. Only present when `category == unreproducible`:

```
pre_build_failed                    — the pre-commit doesn't build; no baseline
post_passed_when_expected_to_fail   — breaking claim not reproducible
post_failed_when_expected_to_pass   — non-breaking claim not reproducible
fix_did_not_restore                 — fix-after-update's fix didn't work
external_service_required
toolchain_unavailable
flaky_tests
timeout
network_required
other
```

## Design decisions

### 1. Flat-ish with nested groupings

Concerns are grouped (`project`, `pr`, `commits`, `update`,
`reproduction`, `failure`) so consumers can destructure cleanly and the
reproduction block can be optional as a whole. `jq` queries remain
trivial (`.update.previousVersion`).

### 2. Closed top-level enums, open subcategories

`ecosystem`, `category`, `authorType`, `botType`, `versionUpdateType`,
`scope`, `failure.topCategory`, `unreproducibilityReason` — closed.
`failure.subCategory` — open string, each ecosystem owns its enum.

### 3. Environment fingerprint, not OCI digest

Originally the schema recorded image digests. Investigation in
`../../../docs/reproducible-builds.md` showed that apt-internal
non-determinism jitters OCI layers by ~tens of bytes per `RUN` even
with pinned `SOURCE_DATE_EPOCH` + snapshot. The v0.0.3 pivot replaced
"image digests match" with "environment fingerprint matches" as the
reproducibility contract.

### 4. `unreproducible` is a first-class category

Dropping unreproducible candidates hides cost of the benchmark. We keep
them so RQ1 can analyse drop-out rates and so an upgraded pipeline can
promote them later.

### 5. `ecosystemMetadata` escape hatch

Per-ecosystem free-form object. Cargo uses it for `edition`,
`cargoLockChanged`, `transitivesChanged`. Keeps the core schema clean.
Cross-ecosystem consumers can ignore it.

### 6. `id` is derived

Deterministic `<ecosystem>-<first-8-of-post>`. Idempotent pipelines.

### 7. Schema versioning

Semver on `schemaVersion`:
- **patch** — doc-only.
- **minor** — additive (new optional fields, new enum values).
- **major** — breaking (renamed/removed fields, tightened constraints).

We've been on 0.0.x so far with breaking changes at each step. When we
hit 0.1.0 or 1.0.0, breaking changes should carry migrator scripts
(`migrations/<from>-to-<to>.py`).

## How to validate

From Python:

```python
import json
from bump_ext import validate_entry, SchemaError

try:
    validate_entry(json.load(open("data/cargo/cargo-9ac20c07.json")))
except SchemaError as e:
    print(e)
```

From any language: use a JSON Schema 2020-12 validator against
`schema/entry.schema.json`. Examples: `ajv-cli` (JS),
`jsonschema` (Python), `kaggle/jsonschema` (Rust).

## What NOT to put in the schema

- Anything derivable from the commit hashes (file sizes, commit
  timestamps) — consumers compute from git / Docker.
- Analysis outputs (RQ-specific statistics, aggregations). Those live
  with the paper, not the entry.
- Ecosystem-specific fields — use `ecosystemMetadata`.

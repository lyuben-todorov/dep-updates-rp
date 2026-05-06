# `bump_ext` — shared Python library

A thin library that makes the shared schema (v0.0.4) ergonomic from
Python. Four source files under `lib/bump_ext/`. Optional for pipelines
not written in Python — those can validate against
`schema/entry.schema.json` directly.

## Why it exists

Without a shared library, every ecosystem pipeline re-implements:
- Schema validation.
- JSON output shape (field ordering, null handling, enum serialisation).
- Ecosystem-specific naming conventions.

Drift is inevitable. The library enforces one source of truth for the
first two — ecosystem-specific naming (fat image tags, thin-image tags)
lives in the respective `pipelines/<eco>/` tree.

## Layout

```
lib/bump_ext/
  __init__.py   — public API, SCHEMA_VERSION constant
  models.py     — Pydantic v2 models mirroring the schema
  validate.py   — JSON Schema 2020-12 validator wrapper
  writer.py     — EntryWriter: validate then write
```

## `models.py` — Pydantic v2 models

Every nesting level has a Python class:

- `Entry` (root)
- `Project`, `PR`, `Commits`, `Update`, `Failure`
- `Reproduction` with sub-models: `FatImage`, `EnvironmentFingerprint`,
  `FingerprintFile`, `ThinImages`, `VerifiedOn`

Enums mirrored as Python enums:
- `Ecosystem` (cargo/maven/pip/npm)
- `UpdateCategory` (breaking/non-breaking/fix-after-update/unreproducible)
- `AuthorType`, `BotType`, `VersionUpdateType`, `Scope`
- `TopFailureCategory`
- `UnreproducibilityReason`

Pydantic enforces at construction time:
- Enum membership — `Ecosystem("xcode")` raises.
- Numeric ranges — `PR(number=0)` raises (minimum 1).
- Regex patterns — `Entry(id="nope")` raises.
- Extra fields — typos like `reproductions` instead of `reproduction`
  raise (all models use `extra="forbid"`).

`use_enum_values=True` on `Entry` means enums serialise as their string
values when dumping to JSON.

## `validate.py` — JSON Schema validator

Two responsibilities:
1. Load `schema/entry.schema.json` once (cached at module level).
2. Expose `validate_entry(dict)` which raises `SchemaError` with
   readable messages when the dict does not match.

## Why validate twice (Pydantic + JSON Schema)?

They validate different things:
- **Pydantic** validates Python objects at construction.
- **JSON Schema** validates JSON at serialisation.

They can disagree. If `models.py` drifts from `entry.schema.json`,
Pydantic will happily build an `Entry` that fails schema validation.
Running both at the write boundary (Python → `model_dump()` →
`validate_entry()` → disk) catches drift where it matters. Cost is
microseconds per entry.

## `writer.py` — entry writer

```python
EntryWriter(output_dir).write(entry) -> Path
```

Under the hood: `entry.model_dump(mode="json")` →
`validate_entry(data)` → `json.dump(data, f)`. An invalid entry never
touches disk.

Note: previous versions also exposed `image_ref()`. Removed in v0.0.3
when the reproducibility model switched from published OCI digests to
environment fingerprints. Fat-image tag conventions now live in
`pipelines/cargo/fat_image.py::fat_image_tag()`.

## `__init__.py` — public API

```python
from bump_ext import (
    # Root + nested models
    Entry, Project, PR, Commits, Update,
    Reproduction, FatImage, EnvironmentFingerprint, FingerprintFile,
    ThinImages, VerifiedOn, Failure,
    # Enums
    Ecosystem, UpdateCategory, AuthorType, BotType,
    VersionUpdateType, Scope, TopFailureCategory,
    UnreproducibilityReason,
    # Writer + validator
    EntryWriter, validate_entry, SchemaError,
    # Constant
    SCHEMA_VERSION,
)
```

`SCHEMA_VERSION` (`"0.0.4"`) must be stamped on every entry's
`schemaVersion` field. New ecosystem pipelines just `import
SCHEMA_VERSION` — no string literals.

## Usage from Python pipelines

```python
from bump_ext import (
    Entry, EntryWriter, Ecosystem, UpdateCategory,
    Project, PR, Commits, Update,
    Reproduction, FatImage, EnvironmentFingerprint, FingerprintFile,
    ThinImages, VerifiedOn,
    Failure, TopFailureCategory,
    SCHEMA_VERSION,
)

entry = Entry(
    id=f"cargo-{post_sha[:8]}",
    schemaVersion=SCHEMA_VERSION,
    ecosystem=Ecosystem.cargo,
    category=UpdateCategory.breaking,
    project=Project(url="https://github.com/foo/bar", organisation="foo", name="bar"),
    pr=PR(url="https://github.com/foo/bar/pull/42",
          number=42, author="dependabot[bot]",
          authorType="bot", botType="dependabot"),
    commits=Commits(pre=pre_sha, post=post_sha),
    update=Update(
        dependencyName="serde",
        previousVersion="1.0.150",
        newVersion="1.0.160",
        versionUpdateType="minor",
        scope="runtime",
    ),
    reproduction=Reproduction(
        fatImage=FatImage(
            rustVersion="1.56.0",
            sourceDateEpoch=1634860800,
            aptSnapshot="20211022T000000Z",
            debianRelease="buster",
        ),
        buildFlags=["--locked", "--offline"],
        environmentFingerprint=EnvironmentFingerprint(
            digest="sha256:…",
            files=[
                FingerprintFile(path="/manifest/packages.txt", sha256="…", bytes=22866),
                # ... etc
            ],
        ),
        thinImages=None,
        verifiedOn=[],
    ),
    failure=Failure(
        topCategory=TopFailureCategory.COMPILATION_FAILURE,
        subCategory="TYPE_MISMATCH",
        errorCodes=["E0308"],
    ),
)
EntryWriter("./data/cargo").write(entry)
```

For a `non-breaking` entry, `failure` is `None` (no failing commit to
classify). For `fix-after-update`, add `commits.fix` and
`reproduction.thinImages.expectedFix` if known.

## Usage from non-Python pipelines

1. Read `schema/entry.schema.json`.
2. Build JSON in whatever tool fits your ecosystem.
3. Validate against the schema with any JSON Schema 2020-12 validator
   (`ajv` for JS, `jsonschema` for Java, `jsonschema` / `jtd` crates
   for Rust).
4. For fat-image tag naming: `rp2026/cargo-fat:<rust>-<debian>-<yyyymmdd>`.
   (Cargo-specific; each ecosystem owns its own image convention.)

The library is a convenience, not a requirement. The schema is the
contract.

## Scope

**In scope:**
- Types mirroring the schema.
- Validation.
- The entry writer.

**Out of scope:**
- Ecosystem-specific logic (that's `pipelines/<eco>/`).
- Mining / reproduction / classification.
- Analysis or aggregation (lives with the paper).

Keep the library small — a new ecosystem owner should be able to read
it in one sitting.

## Versioning

- `SCHEMA_VERSION` in `__init__.py` tracks the schema's semver. Every
  entry stamps its `schemaVersion` from this constant.
- `pyproject.toml` tracks the library's package version (usually the
  same).
- Breaking API changes in the library bump the library's major version.
- Breaking schema changes bump both.

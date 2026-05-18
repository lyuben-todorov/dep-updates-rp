"""SQLite index layer for the pipeline.

Wraps `data/pipeline.sqlite` — a derived, rebuildable query index over the
canonical JSON entries in `data/cargo/` plus per-host pipeline working state.

The file is not a source of truth. Layers 0 (Dockerfiles + scripts) and 1
(entry JSONs + fat-image index.json) in git are canonical. This index is
rebuildable from `scripts/rebuild_index.py`. See `docs/db-design.md`.

Raw sqlite3 — no ORM. Schema DDL is embedded in `SCHEMA`. Connections use
WAL mode and enforce foreign keys. Single-writer by assumption.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = "1"


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  run_id            TEXT PRIMARY KEY,
  started_at        TIMESTAMP NOT NULL,
  finished_at       TIMESTAMP,
  host              TEXT NOT NULL,
  git_sha           TEXT NOT NULL,
  candidates_source TEXT NOT NULL,
  max_sde_date      DATE NOT NULL,
  python_version    TEXT,
  docker_version    TEXT,
  buildx_version    TEXT,
  notes             TEXT
);

CREATE TABLE IF NOT EXISTS entries (
  id                  TEXT PRIMARY KEY,
  ecosystem           TEXT NOT NULL,
  schema_version      TEXT NOT NULL,
  category            TEXT NOT NULL,
  project_org         TEXT,
  project_name        TEXT,
  pr_number           INTEGER,
  pr_author_type      TEXT,
  pr_bot_type         TEXT,
  pre_commit          TEXT,
  post_commit         TEXT,
  fix_commit          TEXT,
  dep_name            TEXT,
  previous_version    TEXT,
  new_version         TEXT,
  version_update_type TEXT,
  post_commit_date    DATE,
  rust_msrv           TEXT,
  msrv_detected       BOOLEAN,
  fat_image_tag       TEXT,
  fingerprint_digest  TEXT,
  file_path           TEXT NOT NULL,
  file_hash           TEXT NOT NULL,
  indexed_at          TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_category       ON entries(category);
CREATE INDEX IF NOT EXISTS idx_entries_post_date      ON entries(post_commit_date);
CREATE INDEX IF NOT EXISTS idx_entries_dep_name       ON entries(dep_name);
CREATE INDEX IF NOT EXISTS idx_entries_version_update ON entries(version_update_type);
CREATE INDEX IF NOT EXISTS idx_entries_fat_image      ON entries(fat_image_tag);
CREATE INDEX IF NOT EXISTS idx_entries_fingerprint    ON entries(fingerprint_digest);
CREATE INDEX IF NOT EXISTS idx_entries_msrv_detected  ON entries(msrv_detected);

CREATE TABLE IF NOT EXISTS fat_images (
  tag                     TEXT PRIMARY KEY,
  rust_version            TEXT NOT NULL,
  debian_release          TEXT NOT NULL,
  source_date_epoch       INTEGER NOT NULL,
  apt_snapshot            TEXT NOT NULL,
  dockerfile_hash         TEXT,
  repro_script_hash       TEXT,
  first_seen_at           DATE,
  built_on_host           TEXT,
  local_image_id          TEXT,
  status                  TEXT NOT NULL,
  notes                   TEXT
);

CREATE INDEX IF NOT EXISTS idx_fat_rust    ON fat_images(rust_version);
CREATE INDEX IF NOT EXISTS idx_fat_debian  ON fat_images(debian_release);
CREATE INDEX IF NOT EXISTS idx_fat_sde     ON fat_images(source_date_epoch);
CREATE INDEX IF NOT EXISTS idx_fat_status  ON fat_images(status);

-- Per-container-platform fingerprints for each fat image. One row per
-- (tag, platform). Matches v0.0.5 entry JSON reproduction.environmentFingerprints.
CREATE TABLE IF NOT EXISTS fat_image_fingerprints (
  tag                     TEXT NOT NULL,
  platform                TEXT NOT NULL,
  digest                  TEXT NOT NULL,
  package_count           INTEGER,
  PRIMARY KEY (tag, platform),
  FOREIGN KEY (tag) REFERENCES fat_images(tag) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_fat_fp_platform ON fat_image_fingerprints(platform);
CREATE INDEX IF NOT EXISTS idx_fat_fp_digest   ON fat_image_fingerprints(digest);

-- Per-attempt reproduction record. One row per pre+post (and optional fix)
-- cargo invocation pair. Bug E pre-fix: only the success path wrote here,
-- so the table under-reported by ~50× on a 2608-candidate run. Post-fix:
-- every reproduction (success, failure, regenerate-short-circuit) records
-- a row; entry_id is nullable because failed reproductions never produce
-- an entry. candidate_key lets us join to drive_state for the failure
-- cohort without an entry. attempt_number distinguishes the N runs of
-- the same candidate under --attempts > 1 (flakiness check).
CREATE TABLE IF NOT EXISTS reproduction_attempts (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id                TEXT,
  candidate_key           TEXT,
  attempt_number          INTEGER NOT NULL DEFAULT 1,
  run_id                  TEXT,
  host_id                 TEXT NOT NULL,
  host_os                 TEXT,
  host_arch               TEXT,
  docker_buildx_version   TEXT,
  started_at              TIMESTAMP NOT NULL,
  finished_at             TIMESTAMP,
  fat_image_tag_used      TEXT,
  fingerprint_expected    TEXT,
  fingerprint_actual      TEXT,
  fingerprint_matched     BOOLEAN,
  pre_exit_code           INTEGER,
  post_exit_code          INTEGER,
  fix_exit_code           INTEGER,
  outcome_matched         BOOLEAN,
  pre_log_path            TEXT,
  post_log_path           TEXT,
  fix_log_path            TEXT,
  notes                   TEXT,
  FOREIGN KEY (entry_id)      REFERENCES entries(id),
  FOREIGN KEY (run_id)        REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_attempts_entry     ON reproduction_attempts(entry_id);
CREATE INDEX IF NOT EXISTS idx_attempts_candidate ON reproduction_attempts(candidate_key);
CREATE INDEX IF NOT EXISTS idx_attempts_run       ON reproduction_attempts(run_id);
CREATE INDEX IF NOT EXISTS idx_attempts_host      ON reproduction_attempts(host_id);
CREATE INDEX IF NOT EXISTS idx_attempts_fp_match  ON reproduction_attempts(fingerprint_matched);
CREATE INDEX IF NOT EXISTS idx_attempts_started   ON reproduction_attempts(started_at);

CREATE TABLE IF NOT EXISTS classifications (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id           TEXT NOT NULL,
  classifier_version TEXT NOT NULL,
  classifier_git_sha TEXT NOT NULL,
  top_category       TEXT NOT NULL,
  sub_category       TEXT,
  error_codes        TEXT,
  classified_at      TIMESTAMP NOT NULL,
  source_log_hash    TEXT,
  is_current         BOOLEAN NOT NULL,
  FOREIGN KEY (entry_id) REFERENCES entries(id)
);

CREATE INDEX IF NOT EXISTS idx_cls_entry   ON classifications(entry_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cls_current ON classifications(entry_id) WHERE is_current;
CREATE INDEX IF NOT EXISTS idx_cls_version ON classifications(classifier_version);

CREATE TABLE IF NOT EXISTS ingestion_sources (
  entry_id      TEXT PRIMARY KEY,
  source        TEXT NOT NULL,
  source_ref    TEXT,
  ingested_at   TIMESTAMP NOT NULL,
  ingested_by   TEXT NOT NULL,
  FOREIGN KEY (entry_id) REFERENCES entries(id)
);

CREATE TABLE IF NOT EXISTS drive_state (
  run_id         TEXT NOT NULL,
  candidate_key  TEXT NOT NULL,
  status         TEXT NOT NULL,
  entry_path     TEXT,
  fat_image_tag  TEXT,
  rust_msrv      TEXT,
  commit_date    DATE,
  reason         TEXT,
  updated_at     TIMESTAMP NOT NULL,
  PRIMARY KEY (run_id, candidate_key),
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_drive_status ON drive_state(status);

-- Scheme-2 (reproduction-failure) classifier output. One row per
-- not_reproducible candidate, written either inline by cargo_drive after
-- the reproducer returns (the live path) or post-hoc by `cargo_drive
-- reclassify` over an existing run's logs. Earlier deployments populated
-- this via scripts/reclassify_failures.py; that script is now a shim.
CREATE TABLE IF NOT EXISTS drive_state_classifications (
  run_id             TEXT NOT NULL,
  candidate_key      TEXT NOT NULL,
  category           TEXT NOT NULL,
  subcategory        TEXT,
  evidence           TEXT,
  -- JSON dict {E_code: count} of every rustc error code seen in the
  -- pre-log. Canonical source: cargo's JSON `compiler-message` records.
  -- Subcategory is the most-fired code for RUSTC_BITROT — but the
  -- distribution often matters (e.g. `lexical-core` emits 17×E0308 +
  -- 10×E0277 in one cargo invocation; picking just one loses signal).
  error_code_counts  TEXT,
  classified_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (run_id, candidate_key),
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_dsc_category ON drive_state_classifications(category);

CREATE TABLE IF NOT EXISTS gh_api_cache (
  key         TEXT PRIMARY KEY,
  etag        TEXT,
  body_json   TEXT NOT NULL,
  fetched_at  TIMESTAMP NOT NULL
);
"""


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


class PipelineDB:
    """Connection wrapper around `data/pipeline.sqlite`.

    Usage:
        with PipelineDB(Path("data/pipeline.sqlite")) as db:
            db.upsert_entry(...)

    Or keep a long-lived instance: `db = PipelineDB(path); ...; db.close()`.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # No PARSE_DECLTYPES — we store ISO-8601 strings and want strings back.
        # sqlite3's built-in TIMESTAMP converter assumes a space separator and
        # crashes on ISO's `T`.
        # check_same_thread=False: the ThreadPoolExecutor main loop in
        # cargo_drive shares one Connection across workers. Thread safety is
        # ensured by the caller via a `db_lock` around compound writes; the
        # sqlite3 module's default thread-local check is redundant for us.
        self.conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        # Multi-driver / multi-threaded writers need this: SQLITE_BUSY retries
        # for up to 10s before the client sees an error. Single-writer runs
        # never trip it; parallel runs need it to not error instantly.
        self.conn.execute("PRAGMA busy_timeout = 10000")
        self._init_schema()

    def __enter__(self) -> "PipelineDB":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction. isolation_level=None → autocommit by
        default; `BEGIN`/`COMMIT` here give us atomic multi-statement ops."""
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # ---- runs ---------------------------------------------------------------

    def start_run(
        self,
        *,
        run_id: str,
        host: str,
        git_sha: str,
        candidates_source: str,
        max_sde_date: dt.date | str,
        python_version: str | None = None,
        docker_version: str | None = None,
        buildx_version: str | None = None,
        notes: str | None = None,
        started_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO runs
                 (run_id, started_at, finished_at, host, git_sha,
                  candidates_source, max_sde_date,
                  python_version, docker_version, buildx_version, notes)
               VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                started_at or _utcnow_iso(),
                host,
                git_sha,
                candidates_source,
                str(max_sde_date),
                python_version,
                docker_version,
                buildx_version,
                notes,
            ),
        )

    def finish_run(self, run_id: str, finished_at: str | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ? WHERE run_id = ?",
            (finished_at or _utcnow_iso(), run_id),
        )

    # ---- entries ------------------------------------------------------------

    def upsert_entry_from_json(self, entry_path: Path) -> str:
        """Read a v0.0.4 entry JSON and upsert into `entries`. Returns entry id.

        Assumes the JSON has already been schema-validated by the writer.
        Computes file_hash from disk bytes.
        """
        with entry_path.open() as f:
            entry = json.load(f)

        file_hash = _sha256_file(entry_path)
        repro = entry.get("reproduction") or {}
        fat = repro.get("fatImage") or {}

        # v0.0.5: environmentFingerprints is a list of per-platform entries.
        # For the denormalised entries.fingerprint_digest column we pick the
        # first fingerprint (for display/query). Per-platform matching lives
        # in reproduction_attempts.
        fps = repro.get("environmentFingerprints") or []
        fp = fps[0] if fps else {}

        fat_tag: str | None = None
        if fat:
            fat_tag = (
                f"rp2026/cargo-fat:{fat['rustVersion']}-{fat['debianRelease']}"
                f"-{_sde_to_date(fat['sourceDateEpoch'])}"
            )

        pr = entry.get("pr") or {}
        commits = entry.get("commits") or {}
        update = entry.get("update") or {}
        project = entry.get("project") or {}
        ecos_meta = entry.get("ecosystemMetadata") or {}

        # rustMsrv / rustMsrvDetected live in ecosystemMetadata (Cargo-specific).
        # post_commit_date isn't in the v0.0.4 schema; cargo_drive patches it
        # via patch_entry_metadata. Rebuild-only paths leave it NULL.
        rust_msrv = ecos_meta.get("rustMsrv")
        msrv_detected = ecos_meta.get("rustMsrvDetected")

        self.conn.execute(
            """INSERT INTO entries
                 (id, ecosystem, schema_version, category,
                  project_org, project_name, pr_number,
                  pr_author_type, pr_bot_type,
                  pre_commit, post_commit, fix_commit,
                  dep_name, previous_version, new_version,
                  version_update_type, post_commit_date,
                  rust_msrv, msrv_detected,
                  fat_image_tag, fingerprint_digest,
                  file_path, file_hash, indexed_at)
               VALUES (?, ?, ?, ?,   ?, ?, ?,   ?, ?,
                       ?, ?, ?,   ?, ?, ?,   ?, ?,
                       ?, ?,   ?, ?,   ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 ecosystem=excluded.ecosystem,
                 schema_version=excluded.schema_version,
                 category=excluded.category,
                 project_org=excluded.project_org,
                 project_name=excluded.project_name,
                 pr_number=excluded.pr_number,
                 pr_author_type=excluded.pr_author_type,
                 pr_bot_type=excluded.pr_bot_type,
                 pre_commit=excluded.pre_commit,
                 post_commit=excluded.post_commit,
                 fix_commit=excluded.fix_commit,
                 dep_name=excluded.dep_name,
                 previous_version=excluded.previous_version,
                 new_version=excluded.new_version,
                 version_update_type=excluded.version_update_type,
                 post_commit_date=COALESCE(excluded.post_commit_date, entries.post_commit_date),
                 rust_msrv=COALESCE(excluded.rust_msrv, entries.rust_msrv),
                 msrv_detected=COALESCE(excluded.msrv_detected, entries.msrv_detected),
                 fat_image_tag=excluded.fat_image_tag,
                 fingerprint_digest=excluded.fingerprint_digest,
                 file_path=excluded.file_path,
                 file_hash=excluded.file_hash,
                 indexed_at=excluded.indexed_at
            """,
            (
                entry["id"],
                entry["ecosystem"],
                entry["schemaVersion"],
                entry["category"],
                project.get("organisation"),
                project.get("name"),
                pr.get("number"),
                pr.get("authorType"),
                pr.get("botType"),
                commits.get("pre"),
                commits.get("post"),
                commits.get("fix"),
                update.get("dependencyName"),
                update.get("previousVersion"),
                update.get("newVersion"),
                update.get("versionUpdateType"),
                None,  # post_commit_date — patched by cargo_drive
                rust_msrv,
                msrv_detected,
                fat_tag,
                fp.get("digest"),
                str(entry_path),
                file_hash,
                _utcnow_iso(),
            ),
        )
        return entry["id"]

    def get_entry(self, entry_id: str) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,))
        return cur.fetchone()

    def iter_entries(self) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute("SELECT * FROM entries ORDER BY id")

    def patch_entry_metadata(
        self,
        entry_id: str,
        *,
        post_commit_date: str | None = None,
        rust_msrv: str | None = None,
        msrv_detected: bool | None = None,
    ) -> None:
        """Set fields the drive computes that aren't always derivable from JSON.

        post_commit_date isn't in v0.0.4 entry JSON; rust_msrv +
        msrv_detected are in `ecosystemMetadata` if the assembler writes
        them, but this patch keeps live-drive writes authoritative without
        a re-read.
        """
        if post_commit_date is not None:
            self.conn.execute(
                "UPDATE entries SET post_commit_date = ? WHERE id = ?",
                (post_commit_date, entry_id),
            )
        if rust_msrv is not None:
            self.conn.execute(
                "UPDATE entries SET rust_msrv = ? WHERE id = ?",
                (rust_msrv, entry_id),
            )
        if msrv_detected is not None:
            self.conn.execute(
                "UPDATE entries SET msrv_detected = ? WHERE id = ?",
                (msrv_detected, entry_id),
            )

    # ---- ingestion_sources --------------------------------------------------

    def upsert_ingestion_source(
        self,
        *,
        entry_id: str,
        source: str,
        source_ref: str | None,
        ingested_by: str,
        ingested_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO ingestion_sources
                 (entry_id, source, source_ref, ingested_at, ingested_by)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(entry_id) DO UPDATE SET
                 source=excluded.source,
                 source_ref=excluded.source_ref,
                 ingested_at=excluded.ingested_at,
                 ingested_by=excluded.ingested_by
            """,
            (entry_id, source, source_ref, ingested_at or _utcnow_iso(), ingested_by),
        )

    # ---- fat_images ---------------------------------------------------------

    def upsert_fat_image(
        self,
        *,
        tag: str,
        rust_version: str,
        debian_release: str,
        source_date_epoch: int,
        apt_snapshot: str,
        dockerfile_hash: str | None = None,
        repro_script_hash: str | None = None,
        first_seen_at: str | None = None,
        built_on_host: str | None = None,
        local_image_id: str | None = None,
        status: str = "valid",
        notes: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO fat_images
                 (tag, rust_version, debian_release, source_date_epoch,
                  apt_snapshot,
                  dockerfile_hash, repro_script_hash, first_seen_at,
                  built_on_host, local_image_id, status, notes)
               VALUES (?, ?, ?, ?,   ?,   ?, ?, ?,   ?, ?, ?, ?)
               ON CONFLICT(tag) DO UPDATE SET
                 rust_version=excluded.rust_version,
                 debian_release=excluded.debian_release,
                 source_date_epoch=excluded.source_date_epoch,
                 apt_snapshot=excluded.apt_snapshot,
                 dockerfile_hash=COALESCE(excluded.dockerfile_hash, fat_images.dockerfile_hash),
                 repro_script_hash=COALESCE(excluded.repro_script_hash, fat_images.repro_script_hash),
                 first_seen_at=COALESCE(fat_images.first_seen_at, excluded.first_seen_at),
                 built_on_host=COALESCE(excluded.built_on_host, fat_images.built_on_host),
                 local_image_id=COALESCE(excluded.local_image_id, fat_images.local_image_id),
                 status=excluded.status,
                 notes=COALESCE(excluded.notes, fat_images.notes)
            """,
            (
                tag,
                rust_version,
                debian_release,
                source_date_epoch,
                apt_snapshot,
                dockerfile_hash,
                repro_script_hash,
                first_seen_at,
                built_on_host,
                local_image_id,
                status,
                notes,
            ),
        )

    def upsert_fat_image_fingerprint(
        self,
        *,
        tag: str,
        platform: str,
        digest: str,
        package_count: int | None = None,
    ) -> None:
        """Upsert per-platform fingerprint row. `fat_images(tag)` must exist."""
        self.conn.execute(
            """INSERT INTO fat_image_fingerprints
                 (tag, platform, digest, package_count)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(tag, platform) DO UPDATE SET
                 digest=excluded.digest,
                 package_count=COALESCE(excluded.package_count,
                                         fat_image_fingerprints.package_count)
            """,
            (tag, platform, digest, package_count),
        )

    def set_fat_image_status(self, tag: str, status: str) -> None:
        self.conn.execute(
            "UPDATE fat_images SET status = ? WHERE tag = ?", (status, tag)
        )

    # ---- reproduction_attempts ---------------------------------------------

    def record_attempt(
        self,
        *,
        host_id: str,
        entry_id: str | None = None,
        candidate_key: str | None = None,
        attempt_number: int = 1,
        run_id: str | None = None,
        host_os: str | None = None,
        host_arch: str | None = None,
        docker_buildx_version: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        fat_image_tag_used: str | None = None,
        fingerprint_expected: str | None = None,
        fingerprint_actual: str | None = None,
        fingerprint_matched: bool | None = None,
        pre_exit_code: int | None = None,
        post_exit_code: int | None = None,
        fix_exit_code: int | None = None,
        outcome_matched: bool | None = None,
        pre_log_path: str | None = None,
        post_log_path: str | None = None,
        fix_log_path: str | None = None,
        notes: str | None = None,
    ) -> int:
        """Record one reproduction attempt. entry_id is nullable (failed
        reproductions never produce an entry); candidate_key is the
        owner/repo#PR string for the failure-cohort join. attempt_number
        is 1-indexed, used by the multi-attempt flakiness check."""
        cur = self.conn.execute(
            """INSERT INTO reproduction_attempts
                 (entry_id, candidate_key, attempt_number, run_id,
                  host_id, host_os, host_arch,
                  docker_buildx_version, started_at, finished_at,
                  fat_image_tag_used, fingerprint_expected, fingerprint_actual,
                  fingerprint_matched, pre_exit_code, post_exit_code,
                  fix_exit_code, outcome_matched,
                  pre_log_path, post_log_path, fix_log_path, notes)
               VALUES (?, ?, ?, ?,   ?, ?, ?,   ?, ?, ?,   ?, ?, ?,
                       ?, ?, ?,   ?, ?,   ?, ?, ?, ?)""",
            (
                entry_id,
                candidate_key,
                attempt_number,
                run_id,
                host_id,
                host_os,
                host_arch,
                docker_buildx_version,
                started_at or _utcnow_iso(),
                finished_at,
                fat_image_tag_used,
                fingerprint_expected,
                fingerprint_actual,
                fingerprint_matched,
                pre_exit_code,
                post_exit_code,
                fix_exit_code,
                outcome_matched,
                pre_log_path,
                post_log_path,
                fix_log_path,
                notes,
            ),
        )
        return int(cur.lastrowid)

    # ---- classifications ----------------------------------------------------

    def flip_classification(
        self,
        *,
        entry_id: str,
        classifier_version: str,
        classifier_git_sha: str,
        top_category: str,
        sub_category: str | None,
        error_codes: list[str] | None,
        source_log_hash: str | None = None,
        classified_at: str | None = None,
    ) -> int:
        """Insert a new `is_current=TRUE` row; flip prior `is_current` rows
        for this entry to FALSE. Atomic."""
        with self.transaction() as cx:
            cx.execute(
                "UPDATE classifications SET is_current = 0 WHERE entry_id = ? AND is_current = 1",
                (entry_id,),
            )
            cur = cx.execute(
                """INSERT INTO classifications
                     (entry_id, classifier_version, classifier_git_sha,
                      top_category, sub_category, error_codes,
                      classified_at, source_log_hash, is_current)
                   VALUES (?, ?, ?,   ?, ?, ?,   ?, ?, 1)""",
                (
                    entry_id,
                    classifier_version,
                    classifier_git_sha,
                    top_category,
                    sub_category,
                    json.dumps(error_codes or []),
                    classified_at or _utcnow_iso(),
                    source_log_hash,
                ),
            )
            return int(cur.lastrowid)

    def seed_classification_if_absent(
        self,
        *,
        entry_id: str,
        classifier_version: str,
        classifier_git_sha: str,
        top_category: str,
        sub_category: str | None,
        error_codes: list[str] | None,
    ) -> bool:
        """For rebuild_index: if no classification row exists for this entry,
        seed one from the failure block in the entry JSON. Returns True if
        inserted."""
        cur = self.conn.execute(
            "SELECT 1 FROM classifications WHERE entry_id = ? LIMIT 1",
            (entry_id,),
        )
        if cur.fetchone() is not None:
            return False
        self.conn.execute(
            """INSERT INTO classifications
                 (entry_id, classifier_version, classifier_git_sha,
                  top_category, sub_category, error_codes,
                  classified_at, source_log_hash, is_current)
               VALUES (?, ?, ?,   ?, ?, ?,   ?, NULL, 1)""",
            (
                entry_id,
                classifier_version,
                classifier_git_sha,
                top_category,
                sub_category,
                json.dumps(error_codes or []),
                _utcnow_iso(),
            ),
        )
        return True

    # ---- drive_state --------------------------------------------------------

    def upsert_drive_state(
        self,
        *,
        run_id: str,
        candidate_key: str,
        status: str,
        entry_path: str | None = None,
        fat_image_tag: str | None = None,
        rust_msrv: str | None = None,
        commit_date: str | None = None,
        reason: str | None = None,
        updated_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO drive_state
                 (run_id, candidate_key, status, entry_path, fat_image_tag,
                  rust_msrv, commit_date, reason, updated_at)
               VALUES (?, ?, ?,   ?, ?,   ?, ?, ?, ?)
               ON CONFLICT(run_id, candidate_key) DO UPDATE SET
                 status=excluded.status,
                 entry_path=excluded.entry_path,
                 fat_image_tag=excluded.fat_image_tag,
                 rust_msrv=excluded.rust_msrv,
                 commit_date=excluded.commit_date,
                 reason=excluded.reason,
                 updated_at=excluded.updated_at
            """,
            (
                run_id,
                candidate_key,
                status,
                entry_path,
                fat_image_tag,
                rust_msrv,
                commit_date,
                reason,
                updated_at or _utcnow_iso(),
            ),
        )

    # ---- drive_state_classifications ----------------------------------------

    def upsert_drive_state_classification(
        self,
        *,
        run_id: str,
        candidate_key: str,
        category: str,
        subcategory: str | None = None,
        evidence: str | None = None,
        error_code_counts: dict[str, int] | None = None,
        classified_at: str | None = None,
    ) -> None:
        """Write a Scheme-2 classification row. Idempotent — overwrites on
        conflict so re-classifying with newer rules updates the record in
        place. `error_code_counts` is a `{E_code: count}` dict; serialised
        as JSON. Empty dict and None both store NULL (no signal vs no data
        is indistinguishable in practice for this column)."""
        ecc_json = (
            json.dumps(error_code_counts, sort_keys=True)
            if error_code_counts else None
        )
        self.conn.execute(
            """INSERT INTO drive_state_classifications
                 (run_id, candidate_key, category, subcategory, evidence,
                  error_code_counts, classified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id, candidate_key) DO UPDATE SET
                 category=excluded.category,
                 subcategory=excluded.subcategory,
                 evidence=excluded.evidence,
                 error_code_counts=excluded.error_code_counts,
                 classified_at=excluded.classified_at
            """,
            (
                run_id,
                candidate_key,
                category,
                subcategory,
                evidence,
                ecc_json,
                classified_at or _utcnow_iso(),
            ),
        )

    # ---- gh_api_cache -------------------------------------------------------

    def cache_get(self, key: str) -> tuple[str | None, str] | None:
        cur = self.conn.execute(
            "SELECT etag, body_json FROM gh_api_cache WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        return (row["etag"], row["body_json"])

    def cache_put(self, key: str, body_json: str, etag: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO gh_api_cache (key, etag, body_json, fetched_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 etag=excluded.etag,
                 body_json=excluded.body_json,
                 fetched_at=excluded.fetched_at
            """,
            (key, etag, body_json, _utcnow_iso()),
        )


def _sde_to_date(sde: int) -> str:
    """1634860800 -> '20211022'. Matches the tag format
    rp2026/cargo-fat:<rust>-<debian>-<YYYYMMDD>."""
    return dt.datetime.fromtimestamp(int(sde), tz=dt.timezone.utc).strftime("%Y%m%d")

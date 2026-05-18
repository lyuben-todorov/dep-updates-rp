-- Migrate reproduction_attempts to the round-2 schema:
--   * entry_id NOT NULL → entry_id (nullable; failed reproductions have no entry)
--   * + candidate_key TEXT (the owner/repo#PR for failure-cohort joins)
--   * + attempt_number INTEGER NOT NULL DEFAULT 1 (multi-attempt support)
-- SQLite can't relax NOT NULL via ALTER TABLE, so we rename the old table,
-- create the new one, and copy rows over.

BEGIN;

ALTER TABLE reproduction_attempts RENAME TO reproduction_attempts_old;

CREATE TABLE reproduction_attempts (
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

INSERT INTO reproduction_attempts
  (id, entry_id, candidate_key, attempt_number, run_id, host_id, host_os, host_arch,
   docker_buildx_version, started_at, finished_at, fat_image_tag_used,
   fingerprint_expected, fingerprint_actual, fingerprint_matched,
   pre_exit_code, post_exit_code, fix_exit_code, outcome_matched,
   pre_log_path, post_log_path, fix_log_path, notes)
SELECT
   id, entry_id, NULL, 1, run_id, host_id, host_os, host_arch,
   docker_buildx_version, started_at, finished_at, fat_image_tag_used,
   fingerprint_expected, fingerprint_actual, fingerprint_matched,
   pre_exit_code, post_exit_code, fix_exit_code, outcome_matched,
   pre_log_path, post_log_path, fix_log_path, notes
FROM reproduction_attempts_old;

DROP TABLE reproduction_attempts_old;

CREATE INDEX IF NOT EXISTS idx_attempts_entry     ON reproduction_attempts(entry_id);
CREATE INDEX IF NOT EXISTS idx_attempts_candidate ON reproduction_attempts(candidate_key);
CREATE INDEX IF NOT EXISTS idx_attempts_run       ON reproduction_attempts(run_id);
CREATE INDEX IF NOT EXISTS idx_attempts_host      ON reproduction_attempts(host_id);
CREATE INDEX IF NOT EXISTS idx_attempts_fp_match  ON reproduction_attempts(fingerprint_matched);
CREATE INDEX IF NOT EXISTS idx_attempts_started   ON reproduction_attempts(started_at);

COMMIT;

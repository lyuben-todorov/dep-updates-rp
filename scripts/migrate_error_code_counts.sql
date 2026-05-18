-- Add error_code_counts column to drive_state_classifications.
-- Stores the JSON dict {E_code: count} of every rustc error code seen
-- in the pre-log (canonical source: cargo's JSON compiler-message stream).
-- Subcategory keeps its single-most-fired-code semantic for compat;
-- this column captures the full distribution, e.g. lexical-core's
-- 17×E0308 + 10×E0277 instead of just E0308.

ALTER TABLE drive_state_classifications ADD COLUMN error_code_counts TEXT;

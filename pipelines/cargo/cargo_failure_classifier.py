"""Scheme-2 failure classifier — bucket a `not_reproducible` candidate's
pre-log into one of the categories below.

Two callers:

  1. The driver, inline. After `cargo_drive.process()` records a
     `not_reproducible` outcome, it calls `classify(text)` on the pre-log
     and writes the result into `drive_state_classifications` in the same
     transaction as the `drive_state` row.

  2. `cargo_drive.py reclassify --run-id X`, post-hoc. Iterates over an
     existing run's `not_reproducible` rows, re-reads each
     `<short>-pre.log`, calls `classify()`, and upserts the result. Used
     when classifier rules evolve and we want to update an old run's
     bucketing without re-running cargo.

The function `classify(text) -> (category, subcategory, evidence)` is the
single source of truth for both paths. No more parallel implementations.

Category philosophy:

  REPO_GONE            — repository/path evaporated ("Cargo.toml not found"
                         after successful git clone). A corpus-health
                         issue, not a reproducibility failure.
  LOCK_FILE_STALE      — `Cargo.lock` can't resolve under the frozen
                         registry snapshot with --locked.
  OPENSSL_MISMATCH     — openssl-sys / ring build-script fails against
                         the fat image's OpenSSL headers. The canonical
                         environmental-era mismatch.
  NATIVE_DEP_MISSING   — pkg-config reports "package X not found" for
                         non-openssl system deps (fuse, nasm, sgx, etc.).
  RUSTC_BITROT         — a single concrete rustc error code (E####) that
                         compiled under the author's toolchain but not
                         the fat image's. Sub-coded by error number.
  TEST_FAILURE         — pre-commit test failed (author-environment
                         assumption that doesn't hold in our container).
  RUNTIME_CRASH        — SIGSEGV / panic in build-script or tests.
  NETWORK_ERROR        — zlib stream, DNS, git fetch, connection timeouts.
  TIMEOUT              — reproducer hit --timeout (default 1800s).
  MSRV_TOO_LOW         — transitive dep declares a rust-version newer
                         than the fat image's rustc.
  OLD_MESSAGE_FORMAT   — old cargo rejected our --message-format flag.
  NO_LOG               — pre-log file missing on disk.
  OTHER                — classifier fell through.
"""

from __future__ import annotations

import json
import re

ANSI = re.compile(r"\x1b\[[0-9;]*m")
RUSTC_CODE = re.compile(r"error\[(E\d{4})\]")
# Validates a code string (without surrounding `error[...]`) — used when
# counting E-codes pulled from cargo's JSON `compiler-message.code.code`.
RUSTC_CODE_HEAD = re.compile(r"^E\d{4}$")
# Matches the *header* of a rustc error line: `error[E####]:` followed by
# a message. Excludes prose mentions like `try rustc --explain E0308` or
# the explanation block quoting the code in narrative form. Anchored to
# line start (multiline mode) so each header is counted once per line.
RUSTC_CODE_HEADER_LINE = re.compile(r"^error\[(E\d{4})\]:", re.MULTILINE)
# Linker errors hide in cargo's JSON `compiler-message` records, not as
# top-level `error: …` lines. The round-2 audit found 35 candidates
# mis-classed as RUSTC_BITROT whose actual cause was `cannot find -lSDL2`
# or `undefined reference to PyTuple_New`. Detecting those means parsing
# the JSON stream — these regexes pull lib/symbol from the rendered text.
LINKER_LIB_MISSING = re.compile(r"cannot find -l(\S+)")
LINKER_UNDEFINED_REF = re.compile(r"undefined reference to `([^']+)'")
# rustc 1.39 turned UB into a runtime panic for `mem::uninitialized()` on
# non-zeroable types. Compile passes, test panics with this message. The
# audit found 20 such candidates classified TEST_FAILURE that are actually
# a runtime form of RUSTC_BITROT.
MEM_UNINIT_RUNTIME = re.compile(
    r"attempted to (?:leave|zero-initialize) type `[^`]+`",
)
# E-codes that aren't real bitrot — they fire on every stable rustc when
# the source code uses `#![feature(...)]` (E0554, E0658) and on cfg gates
# that need nightly. The candidate fundamentally needs a nightly toolchain;
# routing through bitrot-recovery (older milestone) won't help.
NIGHTLY_E_CODES = {"E0554", "E0658"}
# Case-insensitive: older cargo (≤1.34) emits "Could not compile X" with a
# capital C; without IGNORECASE the fallback misses these and they land in OTHER.
COULD_NOT_COMPILE = re.compile(r"error: could not compile [`\"]([^`\"]+)[`\"]", re.IGNORECASE)
PKG_CONFIG_MISSING = re.compile(r"Package ['`\"]?([a-zA-Z0-9_.+-]+)['`\"]? was not found", re.IGNORECASE)
# Cargo emits this when a transitive dep's own MSRV (declared via rust-version
# in its Cargo.toml) is higher than the fat image's rustc. Distinct from
# RUSTC_BITROT (older code on too-new rustc) — this is too-new-deps on too-old
# rustc, a bucketer-routing failure rather than code-aging.
MSRV_TOO_LOW = re.compile(
    r"package `([^`]+)` cannot be built because it requires rustc ([0-9.]+) or newer",
    re.IGNORECASE,
)


CATEGORIES = (
    "REPO_GONE",
    "LOCK_FILE_STALE",
    "OPENSSL_MISMATCH",
    "NATIVE_DEP_MISSING",
    "NIGHTLY_REQUIRED",
    "RUSTC_BITROT",
    "TEST_FAILURE",
    "RUNTIME_CRASH",
    "NETWORK_ERROR",
    "TIMEOUT",
    "DEPENDENCY_RESOLUTION",
    "MSRV_TOO_LOW",
    "OLD_MESSAGE_FORMAT",
    "NO_LOG",
    "OTHER",
)


def count_rustc_error_codes(text: str) -> dict[str, int]:
    """Tally `error[E####]` occurrences in the log.

    Source of truth: cargo's JSON `compiler-message` records — each
    level=error message has a `code.code` field (e.g. "E0308"). Counting
    those is canonical and dedup-safe (every error is one record).

    Fallback: when JSON records are absent (older cargo, malformed
    stream), scan the human-readable `error[E####]:` line *headers*
    only. We exclude prose mentions of E-codes (e.g. `try rustc --explain
    E0308` or the explanation block that quotes the code). The header
    pattern requires a colon-or-newline after the bracket so we don't
    double-count the explainer text.
    """
    counts: dict[str, int] = {}
    json_seen = False
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("reason") != "compiler-message":
            continue
        msg = obj.get("message") or {}
        if msg.get("level") != "error":
            continue
        code = (msg.get("code") or {}).get("code")
        if not code or not RUSTC_CODE_HEAD.match(code):
            continue
        counts[code] = counts.get(code, 0) + 1
        json_seen = True
    if json_seen:
        return counts
    # Text fallback: only count `error[E####]:` headers, not prose mentions.
    for m in RUSTC_CODE_HEADER_LINE.finditer(text):
        code = m.group(1)
        counts[code] = counts.get(code, 0) + 1
    return counts

# "error: build failed" is always the last line; it's a summary, not a cause.
# Same with "could not compile ... due to N previous errors" when N>0.
_GENERIC_TERMINAL = re.compile(
    r"^(error: build failed|error: could not compile .* due to \d+ previous errors?\.?)\s*$",
    re.IGNORECASE,
)


def extract_linker_failure(text: str) -> tuple[str, str] | None:
    """Scan cargo's JSON compiler-message stream for a linker error.

    Returns (kind, name) where kind is `lib` or `symbol`. None if no
    linker error is found. The audit established this hides ~35 cases
    in the BITROT/unsubcoded fallback that should be NATIVE_DEP_MISSING.

    `text` is the raw log; we walk JSON lines, look at level=error
    `compiler-message` records, search the `rendered` text. We stop at
    the first match — multiple linker errors usually share the same
    library, and the first one is the most diagnostic.
    """
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("reason") != "compiler-message":
            continue
        msg = obj.get("message") or {}
        if msg.get("level") != "error":
            continue
        rendered = msg.get("rendered") or ""
        m = LINKER_LIB_MISSING.search(rendered)
        if m:
            return ("lib", m.group(1))
        m = LINKER_UNDEFINED_REF.search(rendered)
        if m:
            return ("symbol", m.group(1)[:60])
    return None


def extract_failures_block(text: str) -> str:
    """Pull the `failures:\\n\\n…` section that cargo's test harness writes
    just before `test result: FAILED`. Empty string if not present.

    Used by the runtime-bitrot detector — the `mem::uninitialized` panic
    appears in test stdout, not in any error: line. Looking for the
    panic in the failures block (rather than the whole log) avoids
    matching unrelated debug output.
    """
    m = re.search(
        r"failures:\n\n(.*?)(?:\nfailures:\n|\ntest result|\Z)",
        text, re.S | re.I,
    )
    return m.group(1) if m else ""


def extract_terminal_error(clean: str) -> str | None:
    """Walk backward through the log, return the last non-generic
    `error:`/`error[E####]:` line. Skips the summary-only `error: build
    failed` and `error: could not compile ... due to N previous errors`
    forms — those restate the same fact every failed build emits, so
    using them as the classifier's input systematically misattributes
    causes."""
    lines = clean.splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped.lower().startswith("error"):
            continue
        if _GENERIC_TERMINAL.match(stripped):
            continue
        return stripped
    return None


def classify(text: str) -> tuple[str, str | None, str]:
    """Return (category, subcategory, evidence_snippet). Backwards-compat
    wrapper around `classify_full` for callers that don't need the
    error-code counts.
    """
    cat, sub, ev, _ = classify_full(text)
    return cat, sub, ev


def classify_full(text: str) -> tuple[str, str | None, str, dict[str, int]]:
    """Return (category, subcategory, evidence_snippet, error_code_counts).

    `error_code_counts` is a `{E_code: count}` dict for every E-code seen
    in the log (canonical source: cargo's JSON `compiler-message`
    records). Always populated when the log contains rustc errors;
    typically empty for non-rustc failure modes (REPO_GONE, NETWORK_ERROR,
    etc.). Stored in `drive_state_classifications.error_code_counts`
    as JSON; lets analysts see the full E-code distribution per
    candidate (e.g. `lexical-core` failures emit 17×E0308 + 10×E0277,
    not just the single picked subcategory).

    For RUSTC_BITROT, subcategory is the *most-fired* E-code (ties
    broken alphabetically). When that's also a NIGHTLY_E_CODE the
    candidate gets re-routed to NIGHTLY_REQUIRED.
    """
    code_counts = count_rustc_error_codes(text)
    cat, sub, ev = _classify_inner(text, code_counts)
    return cat, sub, ev, code_counts


def _classify_inner(text: str, code_counts: dict[str, int]) -> tuple[str, str | None, str]:
    """Body of the classifier. Pulled out so `classify_full` can compute
    the E-code counts once and pass them down without re-walking the log.

    Prioritises the *terminal* error (the last non-generic `error:` line)
    over opportunistic keyword matches across the whole log. Many DS1
    candidates emit OpenSSL / env / lockfile chatter early and then die
    on an unrelated cause hundreds of lines later; the terminal-first
    rule prevents those from being miscategorised.

    subcategory is optional — e.g. error code for RUSTC_BITROT, crate
    name for TEST_FAILURE, missing package name for NATIVE_DEP_MISSING.
    """
    clean = ANSI.sub("", text)
    low = clean.lower()
    terminal = extract_terminal_error(clean) or ""
    term_low = terminal.lower()

    # Reproducer timeout (we write the marker ourselves; it's definitive).
    if "error: reproducer timeout" in low:
        return "TIMEOUT", None, "reproducer timeout marker"

    # Repo gone / missing manifest.
    if "could not find `cargo.toml`" in low:
        return "REPO_GONE", None, "Cargo.toml not found in /src"

    # Lock file stale. Includes "cannot update the lock file ... --locked"
    # (newer cargo's wording) and "checksum changed between lock files" (a
    # registry-snapshot-vs-checked-in-lock mismatch — distinct cause, same
    # operational class: lockfile and resolver disagree, --frozen would resolve).
    if ("needs to be updated but --locked" in low
            or ("cannot update the lock file" in low and "--locked" in low)
            or ("checksum for `" in low and "changed between lock files" in low)):
        return "LOCK_FILE_STALE", None, "--locked rejected stale/changed lockfile"

    # Nightly-required. Pear, rocket 0.3/0.4 pre-Stable, and similar
    # emit the same "incompatible compiler" marker from their build.rs.
    # Also catches `.json` target specs (Z-flag-gated rustc feature).
    if "aborting compilation due to incompatible compiler" in low:
        # Try to name the crate that aborted.
        m = re.search(
            r"failed to run custom build command for `([a-zA-Z0-9_-]+) v[0-9.]+`",
            clean,
        )
        sub = m.group(1) if m else None
        return "NIGHTLY_REQUIRED", sub, "pear/rocket-style nightly-only crate"
    if "json-target-spec" in low and "to be added to the cargo invocation" in low:
        return "NIGHTLY_REQUIRED", "json_target_spec", "Z-flag-gated nightly feature"

    # Terminal-error first — OpenSSL / native-dep.
    if ("openssl-sys" in term_low and "build" in term_low) or \
       "failed to run custom build command for `openssl" in term_low or \
       "unable to detect openssl version" in term_low:
        return "OPENSSL_MISMATCH", None, terminal[:120]

    m = PKG_CONFIG_MISSING.search(clean)
    if m:
        # Only classify as NATIVE_DEP_MISSING if the terminal cause is
        # actually the pkg-config failure; otherwise fall through.
        if "pkg-config" in term_low or "was not found" in term_low or "fuse" in term_low or "v4l2" in term_low:
            return "NATIVE_DEP_MISSING", m.group(1), f"pkg-config: {m.group(1)} not found"

    # Transitive-dep MSRV exceeds the fat-image rustc — a bucketer routing
    # failure (project's own deps demand newer rustc than we ship). Distinct
    # from RUSTC_BITROT (older code on stricter rustc); reverse direction.
    m = MSRV_TOO_LOW.search(clean)
    if m:
        return "MSRV_TOO_LOW", m.group(1), f"requires rustc {m.group(2)}+"

    # Dependency resolution. Split on direction:
    #   - `failed to get <crate>`         → almost always a git-source dep whose
    #                                       upstream repo is gone (corpus
    #                                       tombstone for transitive deps).
    #   - `failed to load source`         → similar — git source unreachable.
    #   - `failed to resolve patches`     → registry index couldn't be patched
    #                                       (e.g. [patch] points at gone repo).
    #   - `failed to select a version`,
    #     `no matching package named`     → real registry resolver failure
    #                                       (yanks, unsatisfiable ranges).
    if "error: failed to get `" in low or "failed to load source for a dependency on" in low:
        return "DEPENDENCY_RESOLUTION", "GIT_DEP_GONE", "git-sourced dep no longer reachable"
    if "failed to resolve patches" in low:
        return "DEPENDENCY_RESOLUTION", "RESOLVE_PATCHES", "patch table unresolvable"
    if "error: failed to select a version" in low or "error: no matching package named" in low:
        return "DEPENDENCY_RESOLUTION", "REGISTRY_RESOLVER", "cargo resolver rejected"

    # Runtime crashes during build — terminal cause must mention build-script
    # / SIGSEGV. The previous "anywhere in log" rule mis-attributed test-stdout
    # panics (e.g. cargo's test harness "panicked at" inside `---- name stdout`)
    # whose actual terminal cause was `error: test failed`. Keeping the rule
    # terminal-only confines RUNTIME_CRASH to its real population.
    if "sigsegv" in term_low or "signal: 11" in term_low:
        return "RUNTIME_CRASH", "SIGSEGV", terminal[:120]
    if ("panicked at" in term_low or "failed to run custom build command" in term_low) \
            and ("build-script" in term_low or "build.rs" in term_low
                 or "custom build command" in term_low):
        return "RUNTIME_CRASH", "BUILD_SCRIPT_PANIC", terminal[:120]

    # rustc error code — the bitrot workhorse. Pick the *most-fired*
    # E-code from `code_counts` as the subcategory; ties broken
    # alphabetically. Previously took the first regex match anywhere in
    # the log, which was effectively arbitrary when multiple distinct
    # codes coexisted (lexical-core 0.7.x emits 17×E0308 + 10×E0277, but
    # whichever the regex hit first won — usually defensible, sometimes
    # wrong, never principled). The full distribution is recorded in the
    # `error_code_counts` field that the caller writes to the DB.
    if code_counts:
        # Most-fired wins. Sort by (-count, code) so ties → smallest code.
        code = sorted(code_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        # E0554 (`#![feature]` on stable) and E0658 (use of unstable lib
        # feature) fire on every stable rustc, regardless of milestone.
        # Re-routing to an older fat image won't recover these; they
        # belong with NIGHTLY_REQUIRED.
        if code in NIGHTLY_E_CODES:
            return "NIGHTLY_REQUIRED", code, f"error[{code}] — needs nightly"
        ev = f"error[{code}] (×{code_counts[code]}, of {sum(code_counts.values())} rustc errors)"
        return "RUSTC_BITROT", code, ev

    # Test failures (pre-commit test expected to pass, didn't). Before
    # accepting the generic TEST_FAILURE bucket, check whether the
    # failures-block panic message indicates a runtime form of bitrot:
    # rustc 1.39 turned `mem::uninitialized()` on non-zeroable types into
    # a runtime panic. The compile succeeded, the test runs, the panic
    # fires inside the test binary. Same root cause as compile-time
    # BITROT, different surface — flag it as RUSTC_BITROT/RUNTIME_MEM_UNINIT
    # so the taxonomy keeps these together for milestone-mapping work.
    if "test result: failed" in low or "error: test failed" in low:
        failures = extract_failures_block(clean)
        if MEM_UNINIT_RUNTIME.search(failures):
            return ("RUSTC_BITROT", "RUNTIME_MEM_UNINIT",
                    "mem::uninitialized() runtime panic (rustc 1.39+)")
        return "TEST_FAILURE", None, "cargo test exit != 0"

    # Network. Includes git-over-SSH auth failures (no auth sock variable),
    # which surface in DS1 when a candidate's [patch] / git-dep tries SSH
    # and the container has no agent.
    if ("error reading from the zlib stream" in low
            or "connection timed out" in low
            or "failed to fetch" in low
            or "no auth sock variable" in low):
        return "NETWORK_ERROR", None, "network fetch failure"

    # Old cargo rejecting our message-format flag (pipeline-era issue, mostly fixed).
    if "json-diagnostic-rendered-ansi" in low and "is not a valid value" in low:
        return "OLD_MESSAGE_FORMAT", None, "cargo too old for our flag"

    # Linker errors are buried in cargo's JSON compiler-message records,
    # not the top-level `error: …` lines. The audit found ~35 of the
    # unsubcoded RUSTC_BITROT fallback are actually NATIVE_DEP_MISSING
    # (SDL2 ×17, Xtst ×6, xcb-shape ×4, snappy/rrd ×1, plus undefined
    # references to libpython3 / libgcrypt symbols). Detecting them
    # before the could-not-compile fallback re-classes them with the
    # right top-level category.
    linker = extract_linker_failure(clean)
    if linker is not None:
        kind, name = linker
        if kind == "lib":
            return "NATIVE_DEP_MISSING", f"-l{name}", f"linker: cannot find -l{name}"
        return "NATIVE_DEP_MISSING", f"sym:{name}", f"linker: undefined reference to {name}"

    # Fallback — grab a "could not compile" crate or the last error line.
    m = COULD_NOT_COMPILE.search(clean)
    if m:
        return "RUSTC_BITROT", None, f"could not compile {m.group(1)}"

    # Empty / no-error log: a candidate whose pre-build silently crashed or
    # produced no diagnostics. Operationally distinct from "classifier fell
    # through on a real error line" — surface as NO_LOG so the OTHER bucket
    # only contains cases that need a new rule.
    if not terminal:
        return "NO_LOG", None, "no error line in pre-log"

    return "OTHER", None, terminal[:120]


def classify_from_reason(reason: str | None) -> tuple[str, str | None, str] | None:
    """Some failure modes are recorded directly in `drive_state.reason`
    without needing to read the log — notably the reproducer's own
    timeout marker and the regenerate-path thin-image-build failure.
    Returns a (category, sub, evidence) triple if the reason maps cleanly,
    or None to fall back to log-based classification.
    """
    if not reason:
        return None
    if "pre_build_timed_out" in reason:
        return "TIMEOUT", None, reason[:120]
    if "regenerate" in reason and "thin-image" in reason:
        # Regenerate-path failure: the pre-log on disk is from an earlier
        # run and unrelated to this candidate's failure. Don't read it.
        return "OTHER", "REGENERATE_FAIL", reason[:120]
    return None

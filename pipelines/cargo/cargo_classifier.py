"""Cargo failure classifier — map logs to the shared taxonomy.

Parses cargo JSON diagnostics (--message-format=json) and cargo test output
to produce a (topCategory, subCategory, errorCodes) tuple.

POC scope: covers the main rustc codes listed in schema/failure-taxonomy.md.
Unknown codes fall back to OTHER_COMPILE_ERROR under COMPILATION_FAILURE.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
from bump_ext import TopFailureCategory  # noqa: E402

ERR_CODE_SUB = {
    "E0277": "TRAIT_BOUND_NOT_SATISFIED",
    "E0308": "TYPE_MISMATCH",
    "E0432": "UNRESOLVED_IMPORT",
    "E0433": "UNRESOLVED_PATH",
    "E0599": "NO_METHOD_FOUND",
    "E0046": "MISSING_TRAIT_IMPL",
}

TEST_FAIL_MARKERS = (
    "test result: FAILED",
    "panicked at",
    "assertion failed",
)

# Our reproducer writes this marker when subprocess.run timed out. The
# classifier needs to catch it BEFORE the generic TEST_FAIL_MARKERS so
# that a test that hung + was killed doesn't get classified as
# TEST_FAILURE (the test never finished, so we can't claim it failed on
# merit).
REPRODUCER_TIMEOUT_MARKER = "error: reproducer timeout — cargo test exceeded"

RESOLUTION_MARKERS = (
    "error: failed to select a version",
    "error: no matching package named",
    "error: failed to get `",
    "could not find `Cargo.toml`",
    "error: the lock file ",
)

ENV_MARKERS = (
    "error: toolchain '",
    "error: rustup could not",
    "cannot find -l",
    "linker `cc` not found",
)

# Build-script panics that indicate a host-environment mismatch rather than
# a code defect. These are "we can't reproduce because the environment
# changed upstream" — distinct from the project's own code failing to compile.
# Checked before falling through to DEPENDENCY_RESOLUTION / TEST / plain ENV.
BUILD_SCRIPT_ENV_MARKERS: dict[str, str] = {
    # openssl@0.9.x build.rs can't parse Debian buster+'s OpenSSL 1.1 headers.
    # Intrinsic to pre-2018 openssl crates + post-stretch Debian.
    "unable to detect openssl version": "OPENSSL_VERSION_DETECT",
    # pear_codegen / rocket_codegen aborted due to nightly/incompatible rustc.
    "aborting compilation due to incompatible compiler": "INCOMPATIBLE_COMPILER",
}

# Lockfile / registry state. Genuinely cargo-level, but neither resolution
# (we never got to resolve) nor compile — it's "the project's own Cargo.lock
# is stale relative to Cargo.toml and we ran --locked to enforce
# reproducibility."
LOCKFILE_MARKERS = (
    "needs to be updated but --locked was passed",
    "needs to be updated but --frozen was passed",
)


@dataclass
class Classification:
    topCategory: str
    subCategory: str | None
    errorCodes: list[str]


def classify(log_text: str) -> Classification:
    codes: list[str] = []
    for line in log_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("reason") != "compiler-message":
            continue
        msg = obj.get("message") or {}
        code = (msg.get("code") or {}).get("code")
        if code and code not in codes:
            codes.append(code)

    if codes:
        sub = ERR_CODE_SUB.get(codes[0], "OTHER_COMPILE_ERROR")
        return Classification(
            topCategory=TopFailureCategory.COMPILATION_FAILURE.value,
            subCategory=sub,
            errorCodes=codes,
        )

    low = log_text.lower()

    # Reproducer-side timeout takes highest priority. A hung test that was
    # killed by us is neither TEST_FAILURE (we don't know the outcome) nor
    # a compile error — it's an environmental/resource classification.
    if REPRODUCER_TIMEOUT_MARKER.lower() in low:
        return Classification(
            topCategory=TopFailureCategory.ENVIRONMENT_FAILURE.value,
            subCategory="TEST_TIMEOUT",
            errorCodes=[],
        )

    # Build-script environmental panics come before the generic markers — the
    # openssl / codegen cases would otherwise fall through to OTHER.
    for marker, sub in BUILD_SCRIPT_ENV_MARKERS.items():
        if marker in low:
            return Classification(
                topCategory=TopFailureCategory.ENVIRONMENT_FAILURE.value,
                subCategory=sub,
                errorCodes=[],
            )

    if any(m in low for m in LOCKFILE_MARKERS):
        return Classification(
            topCategory=TopFailureCategory.DEPENDENCY_RESOLUTION_FAILURE.value,
            subCategory="LOCK_FILE_STALE",
            errorCodes=[],
        )

    if any(m.lower() in low for m in RESOLUTION_MARKERS):
        return Classification(
            topCategory=TopFailureCategory.DEPENDENCY_RESOLUTION_FAILURE.value,
            subCategory=None,
            errorCodes=[],
        )

    if any(m.lower() in low for m in TEST_FAIL_MARKERS):
        return Classification(
            topCategory=TopFailureCategory.TEST_FAILURE.value,
            subCategory="OTHER_TEST_FAILURE",
            errorCodes=[],
        )

    if any(m.lower() in low for m in ENV_MARKERS):
        return Classification(
            topCategory=TopFailureCategory.ENVIRONMENT_FAILURE.value,
            subCategory=None,
            errorCodes=[],
        )

    return Classification(
        topCategory=TopFailureCategory.OTHER.value,
        subCategory=None,
        errorCodes=[],
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Classify a cargo build/test log.")
    p.add_argument("log", help="Path to log file (cargo --message-format=json output + stdout).")
    args = p.parse_args()
    text = Path(args.log).read_text(errors="replace")
    c = classify(text)
    print(json.dumps(c.__dict__, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

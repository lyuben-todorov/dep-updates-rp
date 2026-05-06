"""Shared Candidate dataclass + GitHub helpers for the Cargo producers.

Both `cargo_miner.py` (live-GitHub mining) and `scripts/rebatchi_to_candidate.py`
(Rebatchi CSV translation) emit the same `Candidate` shape. This module owns
that shape and the minimal GitHub plumbing shared between them.

Candidate enrichment: when we produce a candidate we already have the
breaking commit SHA *and* we're already paying at least one GitHub round
trip per candidate. Fetching the MSRV (from rust-toolchain.toml /
rust-toolchain / Cargo.toml at the breaking commit) and the commit's
author date here costs one or two more API calls per candidate and
saves the driver a Docker clone per candidate later. Both fields are
optional — producers that can't or don't want to enrich leave them None,
and the driver falls back to its own lookup.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any


BUMP_RE = re.compile(
    r"[Bb]ump\s+([A-Za-z0-9_\-\.]+)\s+from\s+([0-9][\w\.\-\+]*)\s+to\s+([0-9][\w\.\-\+]*)"
)


@dataclass
class Candidate:
    """One mined Cargo dependency-update PR candidate.

    Fields below `new_version` are optional enrichment added after the core
    mining step; they may be None if enrichment failed or was skipped.

    Naming: `pre_commit` / `post_commit` are the before/after SHAs of the
    dependency update. `post_commit` was previously called `breaking_commit`;
    renamed in v0.0.4 because we now also ingest non-breaking and
    fix-after-update PRs — the category is decided at classification time,
    not at mining time.
    """

    ecosystem: str
    repo: str  # owner/name
    pr_number: int
    pr_url: str
    pr_author: str
    bot_type: str | None
    merged: bool
    pre_commit: str
    post_commit: str
    dependency_name: str
    previous_version: str
    new_version: str

    # Optional enrichment — added by producers when possible.
    rust_msrv: str | None = None                 # "1.70" or None. major.minor.
    post_commit_date: str | None = None          # ISO date "YYYY-MM-DD" or None.
    source: str | None = None                    # e.g. "rebatchi-ds1", "live-gh". Empty for miner.


def gh_headers() -> dict[str, str]:
    """Standard GitHub API headers, with auth when `GITHUB_TOKEN` is set."""
    tok = os.environ.get("GITHUB_TOKEN")
    hdrs = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if tok:
        hdrs["Authorization"] = f"Bearer {tok}"
    return hdrs


def classify_author(login: str) -> tuple[str, str | None]:
    """Return (authorType, botType)."""
    low = (login or "").lower()
    if "dependabot" in low:
        return "bot", "dependabot"
    if "renovate" in low:
        return "bot", "renovate"
    if low.endswith("[bot]"):
        return "bot", "other"
    return "human", None

"""Detect the Rust toolchain a project expects.

Priority (highest wins):
  1. rust-toolchain.toml  -> [toolchain].channel
  2. rust-toolchain       -> raw channel string (legacy)
  3. Cargo.toml           -> [package].rust-version or [workspace.package].rust-version

`detect_toolchain(repo_root)` returns a Docker image tag like "rust:1.92-alpine"
for callers that want one. `msrv_at_commit(repo, sha)` returns a bare "major.minor"
string by probing the GitHub Contents API — no local clone needed.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import os
import re
import sys
import tomllib
from pathlib import Path

from ._candidate import gh_headers  # noqa: E402 — package-relative helper.

RUST_VER_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")


def _normalize_channel(channel: str) -> str | None:
    c = channel.strip().strip('"').strip("'")
    if not c:
        return None
    if c in {"stable", "beta", "nightly"}:
        return c
    m = RUST_VER_RE.match(c)
    if not m:
        return None
    major, minor = m.group(1), m.group(2)
    # Alpine images are tagged by major.minor (e.g. rust:1.92-alpine), not patch.
    return f"{major}.{minor}"


def detect_from_rust_toolchain_toml(path: Path) -> str | None:
    if not path.is_file():
        return None
    data = tomllib.loads(path.read_text())
    ch = (data.get("toolchain") or {}).get("channel")
    return _normalize_channel(ch) if ch else None


def detect_from_rust_toolchain(path: Path) -> str | None:
    if not path.is_file():
        return None
    return _normalize_channel(path.read_text())


def detect_from_cargo_toml(path: Path) -> str | None:
    if not path.is_file():
        return None
    data = tomllib.loads(path.read_text())
    ver = (
        (data.get("package") or {}).get("rust-version")
        or ((data.get("workspace") or {}).get("package") or {}).get("rust-version")
    )
    return _normalize_channel(ver) if ver else None


def detect_toolchain(repo_root: Path, default: str = "rust:1.75-alpine") -> str:
    root = Path(repo_root)
    for finder in (
        lambda: detect_from_rust_toolchain_toml(root / "rust-toolchain.toml"),
        lambda: detect_from_rust_toolchain(root / "rust-toolchain"),
        lambda: detect_from_cargo_toml(root / "Cargo.toml"),
    ):
        tc = finder()
        if tc is None:
            continue
        if tc in {"stable", "beta", "nightly"}:
            return f"rust:{tc}-alpine" if tc == "stable" else f"rustlang/rust:{tc}-alpine"
        return f"rust:{tc}-alpine"
    return default


# ---- GitHub-API-driven MSRV + commit-date fetchers --------------------------

GITHUB_API = "https://api.github.com"


def _channel_from_rust_toolchain_toml_bytes(data: bytes) -> str | None:
    try:
        parsed = tomllib.loads(data.decode("utf-8", errors="replace"))
    except Exception:
        return None
    ch = (parsed.get("toolchain") or {}).get("channel")
    return _normalize_channel(ch) if ch else None


def _channel_from_rust_toolchain_bytes(data: bytes) -> str | None:
    return _normalize_channel(data.decode("utf-8", errors="replace"))


def _channel_from_cargo_toml_bytes(data: bytes) -> str | None:
    try:
        parsed = tomllib.loads(data.decode("utf-8", errors="replace"))
    except Exception:
        return None
    ver = (
        (parsed.get("package") or {}).get("rust-version")
        or ((parsed.get("workspace") or {}).get("package") or {}).get("rust-version")
    )
    return _normalize_channel(ver) if ver else None


# Rust edition → minimum rustc it first compiles under. A weaker signal
def _fetch_contents(repo: str, path: str, ref: str) -> bytes | None:
    """GET /repos/{repo}/contents/{path}?ref={sha}, returning decoded bytes.

    Returns None on 404 or any other failure. Does not raise."""
    try:
        import requests
    except ImportError:
        return None
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    try:
        r = requests.get(url, headers=gh_headers(), params={"ref": ref}, timeout=30)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    body = r.json()
    if isinstance(body, list):
        # Directory listing — `path` points at a dir, not a file. Not useful here.
        return None
    content = body.get("content")
    if not content:
        return None
    if body.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(content)
    except Exception:
        return None


def msrv_at_commit(repo: str, sha: str) -> str | None:
    """Probe the three toolchain-defining files via GitHub Contents API
    and return the MSRV as a 'major.minor' string.

    Precedence (highest first):
      1. rust-toolchain.toml  [toolchain].channel
      2. rust-toolchain       raw channel
      3. Cargo.toml           [package].rust-version (or [workspace.package])

    Returns None if nothing explicit is found. The caller (typically
    `fat_image.bucket_for`) applies the "latest milestone at commit time"
    fallback — a better guess than the old edition-floor fallback because
    it approximates what the author was plausibly targeting.

    Channel tags like 'stable' / 'beta' / 'nightly' also return None — they
    don't pin a concrete rustc version usable for resolving a fat image.
    """
    for path, parse in (
        ("rust-toolchain.toml", _channel_from_rust_toolchain_toml_bytes),
        ("rust-toolchain", _channel_from_rust_toolchain_bytes),
    ):
        data = _fetch_contents(repo, path, sha)
        if data is None:
            continue
        ch = parse(data)
        if ch is None:
            continue
        if ch in {"stable", "beta", "nightly"}:
            return None
        return ch

    cargo_bytes = _fetch_contents(repo, "Cargo.toml", sha)
    if cargo_bytes is not None:
        ch = _channel_from_cargo_toml_bytes(cargo_bytes)
        if ch in {"stable", "beta", "nightly"}:
            return None
        if ch is not None:
            return ch
    return None


# Debian release cutovers. These are actual Debian release dates. Used to
# pick which `rust:<ver>-<release>` base image to ask for when we don't
# have one already. The commit's era → Debian era; good enough for a
# default, and the operator can override in the CLI.
_DEBIAN_CUTOVERS = [
    (dt.date(2021, 8, 14), "buster"),
    (dt.date(2023, 6, 10), "bullseye"),
    (dt.date(2025, 8, 9),  "bookworm"),
    (dt.date(9999, 12, 31), "trixie"),
]


def debian_release_for(date: dt.date) -> str:
    """Pick the Debian release codename appropriate for a commit on `date`."""
    for cutover, prev_release in _DEBIAN_CUTOVERS:
        if date < cutover:
            return prev_release
    return "trixie"


def commit_date_at(repo: str, sha: str) -> str | None:
    """GET /repos/{repo}/commits/{sha} → author date as ISO 'YYYY-MM-DD'.

    Returns None on any failure."""
    try:
        import requests
    except ImportError:
        return None
    url = f"{GITHUB_API}/repos/{repo}/commits/{sha}"
    try:
        r = requests.get(url, headers=gh_headers(), timeout=30)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    iso = (r.json().get("commit") or {}).get("author", {}).get("date")
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("repo_root")
    p.add_argument("--default", default="rust:1.75-alpine")
    a = p.parse_args()
    print(detect_toolchain(Path(a.repo_root), a.default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

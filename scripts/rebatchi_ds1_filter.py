"""Stream through Rebatchi Dataset 1 rar archives, filter for Cargo PRs, emit JSONL.

Dataset 1 is ~9 million GitHub Issues API payloads split across 16 rar
archives (~3.5 GB total, ~250 GB uncompressed). We extract one rar at a
time, scan its JSONs for rows that look like Cargo dependency updates,
emit them to a JSONL file, then delete the extracted files before
moving to the next rar.

Filter (keep a row if ALL hold):

 1. It is actually a PR (`pull_request` key is present).
 2. Title matches "Bump <pkg> from <old> to <new>" OR the title mentions
    a known Rust crate OR the body mentions "Cargo.toml".
 3. For Rust-ness: final decision is deferred — this script just keeps
    everything that plausibly might be a Cargo PR. A follow-up GitHub
    API call (via rebatchi_to_candidate.py --require-cargo) can
    confirm by inspecting the PR's file list.

Emits one JSON line per match with the minimal fields we need
downstream (owner, repo, number, title, state, created_at, user.login,
labels, pull_request.html_url).

Dataset 1 has NO language column (step (5) of the paper's pipeline was
not run), so we cannot pre-filter on Rust the way we did for Dataset 2.
Instead we over-collect now and post-filter via GitHub later.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterator

BUMP_RE = re.compile(
    r"[Bb]ump\s+([A-Za-z0-9_\-\.]+)\s+from\s+([0-9][\w\.\-\+]*)\s+to\s+([0-9][\w\.\-\+]*)"
)

# Heuristic: in Dataset 1 most rows are non-Cargo. We want to keep any
# row that plausibly could be Cargo and let the GitHub API confirm.
# Strategy:
#  - Title contains "Bump X from A to B" (dependabot style) — keep.
#  - Body mentions "Cargo.toml" — keep.
# This deliberately over-collects from other ecosystems. Final Cargo
# filtering happens at the rebatchi_to_candidate step.


def _iter_rar_json(rar_path: Path, scratch: Path) -> Iterator[Path]:
    """Unpack rar into scratch, yield JSON file paths, caller deletes scratch."""
    scratch.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["unar", "-q", "-f", "-o", str(scratch), str(rar_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if r.returncode != 0:
        raise RuntimeError(f"unar failed on {rar_path}: {r.stderr.decode(errors='replace')}")
    yield from scratch.rglob("*.json")


def _extract_candidate_fields(item: dict) -> dict | None:
    pr = item.get("pull_request")
    if not pr:
        return None
    title = item.get("title") or ""
    body = item.get("body") or ""
    has_bump = bool(BUMP_RE.search(title))
    mentions_cargo = "Cargo.toml" in body or "Cargo.toml" in title
    if not (has_bump or mentions_cargo):
        return None
    html_url = item.get("html_url") or pr.get("html_url") or ""
    # owner/repo/number from html_url: https://github.com/owner/repo/pull/N
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/(?:pull|issues)/(\d+)", html_url)
    if not m:
        return None
    owner, repo, number = m.group(1), m.group(2), int(m.group(3))
    user = (item.get("user") or {}).get("login") or ""
    labels = [lab.get("name") for lab in (item.get("labels") or []) if lab.get("name")]
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "title": title,
        "state": item.get("state"),
        "created_at": item.get("created_at"),
        "closed_at": item.get("closed_at"),
        "user": user,
        "labels": labels,
        "pr_url": html_url,
        "has_bump_title": has_bump,
        "body_mentions_cargo_toml": mentions_cargo,
    }


def scan_json(path: Path) -> Iterator[dict]:
    try:
        with path.open("rb") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    for item in payload.get("items") or []:
        rec = _extract_candidate_fields(item)
        if rec is not None:
            yield rec


def process_rar(rar_path: Path, out_fh, progress) -> tuple[int, int]:
    """Return (jsons_scanned, matches_written)."""
    scanned = matched = 0
    with tempfile.TemporaryDirectory(prefix="ds1_", dir="/tmp") as td:
        scratch = Path(td)
        for jp in _iter_rar_json(rar_path, scratch):
            scanned += 1
            for rec in scan_json(jp):
                out_fh.write(json.dumps(rec) + "\n")
                matched += 1
            if scanned % 500 == 0:
                progress(rar_path.name, scanned, matched)
    return scanned, matched


def main() -> int:
    p = argparse.ArgumentParser(description="Filter Rebatchi Dataset 1 rars for Cargo candidates.")
    p.add_argument("--dataset-dir", required=True, help="Dir containing Part N.rar files.")
    p.add_argument("--out", required=True, help="JSONL output with plausible Cargo rows.")
    p.add_argument(
        "--rars",
        nargs="*",
        default=None,
        help="Optional subset of rar names to process (default: all Part *.rar).",
    )
    args = p.parse_args()

    dsd = Path(args.dataset_dir)
    if args.rars:
        rars = [dsd / name for name in args.rars]
    else:
        rars = sorted(dsd.glob("Part *.rar"))
    if not rars:
        print(f"no Part *.rar files in {dsd}", file=sys.stderr)
        return 1

    total_scanned = total_matched = 0
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as out_fh:
        for rar in rars:
            print(f"=== {rar.name} ===", file=sys.stderr)

            def _progress(name, scanned, matched):
                print(f"  {name}: scanned={scanned} matched={matched}", file=sys.stderr)

            try:
                scanned, matched = process_rar(rar, out_fh, _progress)
            except Exception as e:
                print(f"  ERROR: {e}", file=sys.stderr)
                continue
            total_scanned += scanned
            total_matched += matched
            print(f"  done: scanned={scanned} matched={matched}", file=sys.stderr)
            out_fh.flush()
    print(
        f"\nTotal: scanned {total_scanned} JSON pages, matched {total_matched} rows → {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

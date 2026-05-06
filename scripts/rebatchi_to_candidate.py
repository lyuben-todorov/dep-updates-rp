"""Translate Rebatchi PR rows into our cargo candidate JSONL format.

Rebatchi Dataset 2 (and 1) contains rows like:
    Owner, Repo, Number, Title, ...
but does NOT record the commit SHAs or the touched files. We therefore
call the GitHub API to resolve each row to a `Candidate` compatible with
`pipelines/cargo/cargo_reproducer.py`.

Title parsing is best-effort: matches the dependabot-preview pattern
"Bump <pkg> from <a> to <b>". Rows whose title doesn't parse are skipped
with a warning.

Key semantic difference vs our own miner: we do NOT enforce the
"only Cargo.toml/Cargo.lock touched, single-line bump" filter here.
Rebatchi already selected these as dependency-bump PRs; it's the caller's
job to decide whether to post-filter to Cargo-only changes.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

import requests

# Script sits in scripts/; the pipelines package is a sibling. Put the
# repo root on sys.path so `pipelines.cargo` is importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipelines.cargo._candidate import BUMP_RE, Candidate, classify_author, gh_headers  # noqa: E402
from pipelines.cargo.cargo_toolchain import commit_date_at, msrv_at_commit  # noqa: E402

GITHUB_API = "https://api.github.com"


def _resolve_pr(repo: str, number: int) -> dict | None:
    url = f"{GITHUB_API}/repos/{repo}/pulls/{number}"
    for attempt in range(3):
        r = requests.get(url, headers=gh_headers(), timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (404, 410, 301):
            return None
        if r.status_code == 403 and "rate limit" in r.text.lower():
            reset = int(r.headers.get("X-RateLimit-Reset", "0"))
            wait = max(5, reset - int(time.time()) + 2)
            print(f"  rate-limited, sleeping {wait}s", file=sys.stderr)
            time.sleep(min(wait, 120))
            continue
        r.raise_for_status()
    return None


def _pr_touches_cargo(repo: str, number: int) -> tuple[bool, bool]:
    """Return (touches_cargo_toml, only_cargo_files)."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{number}/files"
    r = requests.get(url, headers=gh_headers(), timeout=30)
    if r.status_code != 200:
        return (False, False)
    files = r.json()
    if not files:
        return (False, False)
    paths = {f["filename"] for f in files}
    touches = any(p.endswith("Cargo.toml") for p in paths)
    only = all(p.endswith(("Cargo.toml", "Cargo.lock")) for p in paths)
    return (touches, only)


def translate_row(
    row: dict,
    source: str,
    require_cargo: bool,
) -> Candidate | None:
    title = row.get("Title") or ""
    m = BUMP_RE.search(title)
    if not m:
        return None
    dep, old, new = m.group(1), m.group(2), m.group(3)

    repo = f"{row['Owner']}/{row['Repo']}"
    try:
        number = int(float(row["Number"]))
    except (TypeError, ValueError):
        return None

    pr = _resolve_pr(repo, number)
    if pr is None:
        return None

    if require_cargo:
        touches_toml, only_cargo = _pr_touches_cargo(repo, number)
        if not only_cargo:
            return None

    head_sha = pr["head"]["sha"]
    base_sha = pr["base"]["sha"]
    author = (pr.get("user") or {}).get("login", "")
    _, bot_type = classify_author(author)

    return Candidate(
        ecosystem="cargo",
        repo=repo,
        pr_number=number,
        pr_url=pr["html_url"],
        pr_author=author,
        bot_type=bot_type,
        merged=pr.get("merged_at") is not None,
        pre_commit=base_sha,
        post_commit=head_sha,
        dependency_name=dep,
        previous_version=old,
        new_version=new,
        rust_msrv=msrv_at_commit(repo, head_sha),
        post_commit_date=commit_date_at(repo, head_sha),
        source=source,
    )


def iter_csv(path: str) -> Iterator[dict]:
    with open(path, newline="") as f:
        yield from csv.DictReader(f)


# DS1 (rebatchi_ds1_filter.py output) uses lowercase keys; DS2 CSVs use
# Excel-style capitalized keys. translate_row was written for the latter,
# so we normalize at the edge.
_DS1_TO_DS2_KEYS = {
    "owner": "Owner",
    "repo": "Repo",
    "number": "Number",
    "title": "Title",
    "user": "User",
    "state": "State",
    "pr_url": "PRUrl",
    "created_at": "CreatedAt",
    "closed_at": "ClosedAt",
}


def iter_jsonl(path: str) -> Iterator[dict]:
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            yield {_DS1_TO_DS2_KEYS.get(k, k): v for k, v in raw.items()}


def main() -> int:
    p = argparse.ArgumentParser(description="Translate Rebatchi Cargo PR rows → candidate JSONL.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="Rebatchi CSV (e.g. rust_ds2.csv). DS2 shape.")
    src.add_argument("--jsonl", help="Rebatchi JSONL (e.g. DS1 filter output). DS1 shape.")
    p.add_argument("--out", default="-", help="Output JSONL. Default: stdout.")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument(
        "--source",
        default="rebatchi-ds2",
        help="Provenance tag written into each Candidate record.",
    )
    p.add_argument(
        "--skip-gh-verify",
        action="store_true",
        help="Trust the CSV row without calling GitHub. Commits will be missing; "
             "use only for dry-run counts.",
    )
    p.add_argument(
        "--require-cargo",
        action="store_true",
        help="Skip PRs whose files are not Cargo.toml/Cargo.lock only.",
    )
    args = p.parse_args()

    out_fh = sys.stdout if args.out == "-" else open(args.out, "w")
    n_in = n_out = 0
    row_iter = iter_csv(args.csv) if args.csv else iter_jsonl(args.jsonl)
    try:
        for row in row_iter:
            n_in += 1
            if args.skip_gh_verify:
                m = BUMP_RE.search(row.get("Title") or "")
                if not m:
                    continue
                record = {
                    "ecosystem": "cargo",
                    "repo": f"{row['Owner']}/{row['Repo']}",
                    "pr_number": int(float(row["Number"])),
                    "dependency_name": m.group(1),
                    "previous_version": m.group(2),
                    "new_version": m.group(3),
                    "source": args.source,
                }
                out_fh.write(json.dumps(record) + "\n")
                n_out += 1
            else:
                cand = translate_row(row, args.source, args.require_cargo)
                if cand is None:
                    continue
                out_fh.write(json.dumps(asdict(cand)) + "\n")
                out_fh.flush()
                n_out += 1

            if args.limit and n_out >= args.limit:
                break

        print(f"translated {n_out}/{n_in} rows", file=sys.stderr)
    finally:
        if out_fh is not sys.stdout:
            out_fh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

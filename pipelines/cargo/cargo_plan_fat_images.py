"""Read a candidate JSONL and report a fat-image plan.

Given candidates enriched with `rust_msrv` + `post_commit_date`, this
script:

  1. Buckets each candidate via ``fat_image.bucket_for`` into a
     (milestone, year, debian) BucketKey.
  2. Computes each bucket's canonical SDE + tag via
     ``fat_image.canonical_sde_for`` / ``fat_image.tag_for``. Everything
     deterministic.
  3. Groups proposals by resulting tag — two distinct buckets can clamp
     their SDE to the same `rust_base_pub` and thus produce the same tag.
  4. Checks ``docker/cargo-fat/index.json`` for each tag: if present,
     reuse; else propose a new build.
  5. Prints a console report.

One bucket = one proposed image (unless two buckets happen to dedupe to
the same tag). No cross-bucket upward-milestone collapsing — each
(milestone, year, debian) is its own image.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import fat_image as _fat
from .cargo_toolchain import commit_date_at, debian_release_for, msrv_at_commit


# ---- loader -----------------------------------------------------------------

def load_candidates(path: Path, resolve_missing: bool) -> tuple[list[dict], dict[str, int]]:
    """Load candidates. If `resolve_missing`, fill gaps via GitHub API.
    Returns (rows, counters)."""
    rows: list[dict] = []
    counters: dict[str, int] = defaultdict(int)
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            c = json.loads(line)
            counters["total"] += 1

            msrv = c.get("rust_msrv")
            if msrv is None:
                counters["missing_msrv"] += 1
                if resolve_missing:
                    msrv = msrv_at_commit(c["repo"], c["post_commit"])
                    if msrv is not None:
                        c["rust_msrv"] = msrv
                        counters["resolved_msrv"] += 1

            date_str = c.get("post_commit_date")
            if date_str is None:
                counters["missing_commit_date"] += 1
                if resolve_missing:
                    date_str = commit_date_at(c["repo"], c["post_commit"])
                    if date_str is not None:
                        c["post_commit_date"] = date_str
                        counters["resolved_commit_date"] += 1
            if date_str:
                try:
                    c["_commit_date"] = dt.date.fromisoformat(date_str)
                except ValueError:
                    pass

            rows.append(c)
    return rows, dict(counters)


# ---- bucketing --------------------------------------------------------------

def bucketize(
    candidates: list[dict],
    *,
    max_sde_date: dt.date,
) -> tuple[dict[_fat.BucketKey, list[dict]], list[dict]]:
    """Group candidates into BucketKeys. Returns (buckets, unbucketable).

    Candidates whose commit_date is later than `max_sde_date` are rejected
    as `commit_too_recent`. This is the single upper bound on the
    pipeline's acceptable commit dates — no today-clamping happens anywhere
    downstream. `max_sde_date` should default to "last New Year's Eve" when
    no run-level override is provided (see
    `fat_image.default_max_sde_date`).
    """
    buckets: dict[_fat.BucketKey, list[dict]] = defaultdict(list)
    unbucketable: list[dict] = []
    for c in candidates:
        commit_date = c.get("_commit_date")
        if commit_date is None:
            unbucketable.append({"candidate": c, "reason": "no commit_date"})
            continue
        if commit_date > max_sde_date:
            unbucketable.append({
                "candidate": c,
                "reason": f"commit_too_recent (commit_date={commit_date} > "
                          f"max_sde_date={max_sde_date})",
            })
            continue
        debian = debian_release_for(commit_date)
        b = _fat.bucket_for(c.get("rust_msrv"), commit_date, debian)
        if b is None:
            unbucketable.append({
                "candidate": c,
                "reason": f"msrv {c.get('rust_msrv')!r} has no supported "
                          f"(milestone, debian) on Docker Hub",
            })
            continue
        buckets[b].append(c)
    return dict(buckets), unbucketable


# ---- proposals --------------------------------------------------------------

@dataclass
class Proposal:
    tag: str
    kept_bucket: _fat.BucketKey
    absorbed: list[_fat.BucketKey]       # including kept_bucket itself
    candidate_count: int
    sde: _fat.CanonicalSde
    existing_tag: str | None              # tag from index (same as self.tag if present)


def plan(buckets: dict[_fat.BucketKey, list[dict]],
         existing_index: list[_fat.FatImageRecord],
         *,
         max_sde_date: dt.date) -> list[Proposal]:
    """Turn a dict of (BucketKey -> candidates) into a list of Proposals.

    One bucket = one proposal, keyed on the canonical tag. Two buckets can
    clamp to the same rust_base_pub (e.g. 2018/2019/2020 buster all produce
    `1.49.0-buster-20210209`) — those merge into a single proposal whose
    `absorbed` list records the source buckets.

    `max_sde_date` is passed to `canonical_sde_for` for API symmetry — the
    function doesn't read it today, but callers that thread the value
    through stay consistent as the policy evolves.
    """
    existing_tags = {r.tag for r in existing_index}

    by_tag: dict[str, Proposal] = {}
    for bucket, cands in buckets.items():
        sde = _fat.canonical_sde_for(bucket, max_sde_date=max_sde_date)
        tag = _fat.tag_for(bucket, sde.sde)
        if tag not in by_tag:
            by_tag[tag] = Proposal(
                tag=tag,
                kept_bucket=bucket,
                absorbed=[bucket],
                candidate_count=len(cands),
                sde=sde,
                existing_tag=tag if tag in existing_tags else None,
            )
        else:
            merged = by_tag[tag]
            merged.absorbed.append(bucket)
            merged.absorbed.sort(key=lambda b: (_fat.parse_semver(b.milestone), b.year))
            merged.candidate_count += len(cands)

    proposals = list(by_tag.values())
    proposals.sort(key=lambda p: -p.candidate_count)
    return proposals


# ---- report -----------------------------------------------------------------

def print_report(rows: list[dict], counters: dict[str, int],
                 buckets: dict[_fat.BucketKey, list[dict]],
                 unbucketable: list[dict],
                 proposals: list[Proposal], min_density: int,
                 max_sde_date: dt.date) -> None:
    total = counters.get("total", len(rows))
    print(f"Run parameters:")
    print(f"  max_sde_date:                  {max_sde_date}")
    print(f"Candidates read:                 {total}")
    if counters.get("missing_msrv"):
        print(f"  missing rust_msrv field:       {counters['missing_msrv']} "
              f"(resolved via GH: {counters.get('resolved_msrv', 0)})")
    if counters.get("missing_commit_date"):
        print(f"  missing post_commit_date:      {counters['missing_commit_date']} "
              f"(resolved via GH: {counters.get('resolved_commit_date', 0)})")
    print()

    print(f"Buckets (rust milestone × year × debian):    {len(buckets)}")
    by_size = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0].milestone, kv[0].year, kv[0].debian))
    print(f"  below --min-density {min_density}: "
          f"{sum(1 for _, cs in by_size if len(cs) < min_density)}")
    print()
    print(f"{'MILESTONE':<10} {'YEAR':>5} {'DEBIAN':<10} {'COUNT':>6}  DENSITY")
    for b, cs in by_size:
        flag = "  sparse" if len(cs) < min_density else ""
        print(f"{b.milestone:<10} {b.year:>5} {b.debian:<10} {len(cs):>6}{flag}")
    print()

    existing = [p for p in proposals if p.existing_tag is not None]
    new = [p for p in proposals if p.existing_tag is None]
    covered = sum(p.candidate_count for p in proposals)
    print(f"Proposed fat images:             {len(proposals)}")
    print(f"  existing reused:               {len(existing)}")
    print(f"  new builds:                    {len(new)}")
    if total:
        print(f"Covers candidates:               {covered} / {total}  ({100*covered/total:.1f}%)")
    print()

    if existing:
        print("Reuse existing (no build):")
        for p in existing:
            print(f"  {p.existing_tag}  [{p.candidate_count} candidates]")
            for b in p.absorbed:
                print(f"       serves: ({b.milestone}, {b.year}, {b.debian})  [{len(buckets.get(b, []))}]")
        print()

    if new:
        print("Propose to build:")
        print(f"{'RUST':<8} {'DEBIAN':<10} {'SNAPSHOT':<12} {'COUNT':>6}  FLAGS")
        for p in new:
            flags = []
            if p.sde.pre_rust_base:
                flags.append("pre_rust_base")
            if p.sde.rust_base_unknown:
                flags.append("rust_base_unknown")
            flag_s = "  " + " ".join(flags) if flags else ""
            print(f"{p.kept_bucket.rust_patch():<8} {p.kept_bucket.debian:<10} "
                  f"{p.sde.sde_date.isoformat():<12} {p.candidate_count:>6}{flag_s}")
            for b in p.absorbed:
                print(f"       serves: ({b.milestone}, {b.year}, {b.debian})  [{len(buckets.get(b, []))}]")
        print()
        print("To build each (one command per line):")
        for p in new:
            print(f"  python3 -m pipelines.cargo.fat_image build "
                  f"--rust-version {p.kept_bucket.rust_patch()} "
                  f"--debian-release {p.kept_bucket.debian} "
                  f"--source-date-epoch {p.sde.sde}")
        print()

    if unbucketable:
        print(f"Unbucketable: {len(unbucketable)}")
        # Collapse fine-grained variants into a high-level category for the
        # summary table (e.g. all "commit_too_recent (...=2021-04-24 > ...)"
        # variations fold into one "commit_too_recent" row). Full per-candidate
        # reasons are still in each unbucketable record if callers want them.
        _CATEGORIES = (
            "commit_too_recent",
            "no commit_date",
            "no supported fat image",
            "msrv",
        )
        def _category(reason: str) -> str:
            for prefix in _CATEGORIES:
                if reason.startswith(prefix):
                    return prefix
            return reason
        reasons: dict[str, int] = {}
        for u in unbucketable:
            reasons[_category(u["reason"])] = reasons.get(_category(u["reason"]), 0) + 1
        for r, c in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {c:>5}  {r}")
        print()


# ---- main -------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Propose a fat-image plan for a batch of candidates.")
    p.add_argument("--candidates", required=True, type=Path,
                   help="Candidate JSONL produced by cargo_miner / rebatchi_to_candidate.")
    p.add_argument("--min-density", type=int, default=1,
                   help="Flag buckets with fewer than N candidates as sparse (default 1).")
    p.add_argument("--resolve-missing", action="store_true",
                   help="If candidate records lack rust_msrv / post_commit_date, "
                        "fetch them via GitHub API.")
    p.add_argument("--max-sde-date", type=lambda s: dt.date.fromisoformat(s),
                   default=None,
                   help="Upper bound on acceptable commit dates (YYYY-MM-DD). "
                        "Candidates with later commits are rejected as "
                        "commit_too_recent. Default: Dec 31 of last year.")
    args = p.parse_args()

    max_sde_date = args.max_sde_date or _fat.default_max_sde_date()

    rows, counters = load_candidates(args.candidates, args.resolve_missing)
    buckets, unbucketable = bucketize(rows, max_sde_date=max_sde_date)
    existing_index = _fat.load_index()
    proposals = plan(buckets, existing_index, max_sde_date=max_sde_date)
    print_report(rows, counters, buckets, unbucketable, proposals,
                 args.min_density, max_sde_date=max_sde_date)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

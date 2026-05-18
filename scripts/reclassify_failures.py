"""DEPRECATED — use `python -m pipelines.cargo.cargo_drive --reclassify ...`.

The Scheme-2 classifier now lives at
`pipelines.cargo.cargo_failure_classifier` and is invoked through the
driver, both inline (during a run, alongside drive_state writes) and
post-hoc (via `cargo_drive --reclassify`). This shim exists only to
preserve the old invocation path for muscle memory; it forwards to
the driver.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser(
        description="DEPRECATED. Forwards to "
                    "`python -m pipelines.cargo.cargo_drive --reclassify`.",
    )
    ap.add_argument("--db", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--logs-dir", required=True)
    ap.add_argument("--candidates", required=True)
    args, extra = ap.parse_known_args()

    print(
        "WARNING: scripts/reclassify_failures.py is deprecated. The Scheme-2 "
        "classifier is now wired into cargo_drive.py:\n"
        "  python -m pipelines.cargo.cargo_drive --reclassify \\\n"
        f"    --db {args.db} --run-id {args.run_id} \\\n"
        f"    --logs-dir {args.logs_dir} --candidates {args.candidates}\n"
        "Forwarding...",
        file=sys.stderr,
    )

    cmd = [
        sys.executable, "-m", "pipelines.cargo.cargo_drive",
        "--reclassify",
        "--db", args.db,
        "--run-id", args.run_id,
        "--logs-dir", args.logs_dir,
        "--candidates", args.candidates,
        # cargo_drive requires --candidates; --reclassify ignores any
        # other run-time args, but argparse will complain if --out-dir or
        # --state aren't passed. Provide harmless defaults.
        "--out-dir", "/tmp/_unused-reclassify-out",
        "--state", "/tmp/_unused-reclassify-state.jsonl",
    ]
    cmd.extend(extra)
    os.execvp(cmd[0], cmd)
    return 0  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())

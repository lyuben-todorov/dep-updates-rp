#!/usr/bin/env bash
# End-to-end verification of a bootstrapped box.
#
# Rebuilds the fat image that reproduced cargo-9ac20c07 (committed seed
# entry — netscli #22) and re-runs cargo_regenerate on it. Proves:
#   - docker buildx can produce a fat image
#   - the repo-committed fat-image recipe matches the fingerprint
#   - the regeneration loop (thin build + test + verifiedOn append) works
#   - SQLite mirror records the attempt
#
# Expected runtime: ~8-12 min on a decent box (most of it is the fat-image build).
# Network: ~500 MB apt fetches from snapshot.debian.org during fat-image build.
#
# This does a real reproduction — writes a verifiedOn record to the
# committed entry JSON. Revert with `cd data/cargo && git checkout -- .`
# if you want a pristine submodule.

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/rp2026}"
ENTRY="${ENTRY:-$REPO_DIR/data/cargo/cargo-9ac20c07.json}"
DB="${DB:-$REPO_DIR/data/pipeline.sqlite}"
# Docker Desktop ships `desktop-linux`; Linux with manual buildx setup ships
# whatever bootstrap.sh created (default: `rp2026`). Pick whichever exists.
BUILDX_BUILDER="${BUILDX_BUILDER:-}"
if [ -z "$BUILDX_BUILDER" ]; then
  if docker buildx inspect desktop-linux >/dev/null 2>&1; then
    BUILDX_BUILDER="desktop-linux"
  elif docker buildx inspect rp2026 >/dev/null 2>&1; then
    BUILDX_BUILDER="rp2026"
  else
    BUILDX_BUILDER="default"
  fi
fi

log() { printf '\033[36m[smoke]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[smoke] %s\033[0m\n' "$*" >&2; exit 1; }

cd "$REPO_DIR"

[ -f "$ENTRY" ] || die "entry JSON missing: $ENTRY (submodule not initialised?)"
[ -d .venv ]    || die "no venv — run deploy/bootstrap.sh first"

# Load token so MSRV / commit-date fallbacks work if anything falls through.
# shellcheck disable=SC1091
[ -f .env ] && { set -a; . .env; set +a; }

HOST="$(hostname)"
log "host=$HOST entry=$ENTRY builder=$BUILDX_BUILDER"

# ---- run regenerate ---------------------------------------------------------

log "regenerating $(basename "$ENTRY") — this builds the fat image if missing (~8-12 min)"
.venv/bin/python3 -m pipelines.cargo.cargo_regenerate \
  --entry "$ENTRY" \
  --build-missing-bases \
  --host "$HOST" \
  --timeout 1800 \
  --builder "$BUILDX_BUILDER"

# ---- refresh SQLite mirror --------------------------------------------------

log "rebuilding index + verifying drift"
.venv/bin/python3 scripts/rebuild_index.py >/dev/null
.venv/bin/python3 scripts/verify_index.py

# ---- summary ----------------------------------------------------------------

log "verifiedOn records for cargo-9ac20c07:"
python3 - "$ENTRY" <<'PY'
import json, sys
entry = json.load(open(sys.argv[1]))
for r in entry["reproduction"]["verifiedOn"]:
    print(f'  host={r.get("host")!s:<20} platform={r.get("platform")!s:<16} '
          f'fingerprintMatch={r.get("fingerprintMatch")!s:<5} '
          f'outcomeMatch={r.get("outcomeMatch")!s:<5} at={r.get("verifiedAt")}')
PY

cat <<EOF

  smoke test passed.

  the full pipeline works on this box:
    docker buildx → fat image → fingerprint match → thin images → cargo test → verifiedOn append.

  to publish this host's verification to the data submodule:
    cd $REPO_DIR/data/cargo
    git add cargo-9ac20c07.json
    git commit -m "verifiedOn: $HOST"
    git push

EOF

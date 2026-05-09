#!/usr/bin/env bash
# Cargo cache observability for Prometheus node_exporter textfile collector.
#
# Emits three gauges:
#   cargo_cache_bytes{kind="git"}       — size of the shared git subtree
#   cargo_cache_bytes{kind="registry"}  — size of the shared registry subtree
#   cargo_crates_fetched_5m             — count of registry files mtime'd
#                                          within the last 5 minutes
#
# Invoke via systemd timer or cron every 1-5 minutes. Writes atomically so
# node_exporter never sees a half-written file.

set -euo pipefail

CACHE_DIR=${CACHE_DIR:-$HOME/rp2026/data/cargo-cache}
OUT=${OUT:-/var/lib/prometheus/node-exporter/cargo_cache.prom}

du_bytes() {
  local path="$1"
  if [[ -d "$path" ]]; then
    du -sb "$path" | awk '{print $1}'
  else
    echo 0
  fi
}

git_bytes=$(du_bytes "$CACHE_DIR/git")
registry_bytes=$(du_bytes "$CACHE_DIR/registry")

if [[ -d "$CACHE_DIR/registry/cache" ]]; then
  recent=$(find "$CACHE_DIR/registry/cache" -type f -newermt '-5 minutes' 2>/dev/null | wc -l)
else
  recent=0
fi

tmp=$(mktemp --tmpdir="$(dirname "$OUT")" cargo_cache.XXXXXX.prom)
cat > "$tmp" <<EOF
# HELP cargo_cache_bytes Cargo cache size on disk, by subtree.
# TYPE cargo_cache_bytes gauge
cargo_cache_bytes{kind="git"} $git_bytes
cargo_cache_bytes{kind="registry"} $registry_bytes
# HELP cargo_crates_fetched_5m Crate files added to the registry cache in the last 5 minutes.
# TYPE cargo_crates_fetched_5m gauge
cargo_crates_fetched_5m $recent
EOF
mv "$tmp" "$OUT"

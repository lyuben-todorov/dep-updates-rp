#!/usr/bin/env bash
# Deploy a second Grafana instance at /rp/ + Prometheus + node_exporter.
#
# Runs against an already-bootstrapped host (`deploy/bootstrap.sh`) with
# Grafana already installed (the main `/imot/` instance). Idempotent —
# safe to rerun.
#
# Assumes passwordless sudo is configured for the invoking user.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="${REPO_DIR:-$HOME/rp2026}"
SQLITE_PATH="${SQLITE_PATH:-$REPO_DIR/data/pipeline.sqlite}"
NGINX_CONF="${NGINX_CONF:-/etc/nginx/sites-enabled/bigcrack}"

log() { printf '\033[36m[grafana-rp]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[grafana-rp] %s\033[0m\n' "$*" >&2; exit 1; }

# ---- 1. prometheus + node-exporter -----------------------------------------

log "installing prometheus + node-exporter"
sudo apt-get update -qq
sudo apt-get install -y -qq prometheus prometheus-node-exporter

log "writing /etc/prometheus/prometheus.yml"
sudo install -m 0644 "$HERE/prometheus.yml" /etc/prometheus/prometheus.yml
sudo systemctl restart prometheus prometheus-node-exporter
sudo systemctl enable  prometheus prometheus-node-exporter

# Sanity check — prometheus should be scraping localhost:9100.
sleep 2
if ! curl -sf http://localhost:9090/api/v1/targets >/dev/null; then
  die "prometheus not responding on :9090"
fi
if ! curl -sf http://localhost:9100/metrics >/dev/null; then
  die "node-exporter not responding on :9100"
fi

# ---- 2. grafana-rp paths + config ------------------------------------------

log "creating /etc/grafana-rp, /var/lib/grafana-rp, /var/log/grafana-rp"
sudo install -d -o grafana -g grafana -m 0755 /etc/grafana-rp
sudo install -d -o grafana -g grafana -m 0755 /etc/grafana-rp/provisioning/datasources
sudo install -d -o grafana -g grafana -m 0755 /etc/grafana-rp/provisioning/dashboards
sudo install -d -o grafana -g grafana -m 0755 /etc/grafana-rp/dashboards
sudo install -d -o grafana -g grafana -m 0750 /var/lib/grafana-rp
sudo install -d -o grafana -g grafana -m 0755 /var/log/grafana-rp

log "installing grafana.ini"
sudo install -m 0640 -o root -g grafana "$HERE/grafana.ini" /etc/grafana-rp/grafana.ini

log "installing provisioning"
sudo install -m 0644 -o grafana -g grafana \
  "$HERE/provisioning/datasources/datasources.yaml" \
  /etc/grafana-rp/provisioning/datasources/datasources.yaml
sudo install -m 0644 -o grafana -g grafana \
  "$HERE/provisioning/dashboards/dashboards.yaml" \
  /etc/grafana-rp/provisioning/dashboards/dashboards.yaml

log "installing dashboards"
for f in "$HERE/dashboards/"*.json; do
  sudo install -m 0644 -o grafana -g grafana "$f" "/etc/grafana-rp/dashboards/$(basename "$f")"
done

# ---- 3. pipeline.sqlite readability ----------------------------------------

log "granting grafana user read access to $SQLITE_PATH"
if [ ! -f "$SQLITE_PATH" ]; then
  die "pipeline.sqlite not found at $SQLITE_PATH — run scripts/rebuild_index.py first"
fi
SQLITE_GROUP="$(stat -c '%G' "$SQLITE_PATH")"
log "sqlite group: $SQLITE_GROUP"
if ! id -nG grafana | tr ' ' '\n' | grep -qx "$SQLITE_GROUP"; then
  sudo usermod -aG "$SQLITE_GROUP" grafana
  log "added grafana to group $SQLITE_GROUP (service restart will pick it up)"
fi
chmod g+rX "$SQLITE_PATH" || true
chmod g+rX "$(dirname  "$SQLITE_PATH")" || true
chmod g+rX "$(dirname  "$(dirname "$SQLITE_PATH")")" || true
chmod g+rX "$(dirname  "$(dirname "$(dirname "$SQLITE_PATH")")")" || true

# ---- 4. systemd unit -------------------------------------------------------

log "installing systemd unit grafana-server-rp.service"
sudo install -m 0644 "$HERE/grafana-server-rp.service" /etc/systemd/system/grafana-server-rp.service
sudo systemctl daemon-reload
sudo systemctl enable grafana-server-rp

# ---- 5. nginx location -----------------------------------------------------

if sudo grep -q "location /rp/" "$NGINX_CONF"; then
  log "nginx already has /rp/ location; skipping edit"
else
  log "adding /rp/ location to $NGINX_CONF"
  TMPFILE=$(mktemp)
  sudo awk -v snippet_file="$HERE/nginx-rp.location" '
    /location \/imot\// && !inserted {
      while ((getline line < snippet_file) > 0) print line
      close(snippet_file)
      print ""
      inserted=1
    }
    { print }
  ' "$NGINX_CONF" > "$TMPFILE"
  sudo install -m 0644 "$TMPFILE" "$NGINX_CONF"
  rm -f "$TMPFILE"
fi

sudo nginx -t
sudo systemctl reload nginx

# ---- 6. start grafana-rp ---------------------------------------------------

log "starting grafana-server-rp"
sudo systemctl restart grafana-server-rp
sleep 3
if ! curl -sf http://127.0.0.1:3001/api/health >/dev/null; then
  log "grafana-rp not responding yet — tailing its log:"
  sudo journalctl -u grafana-server-rp -n 30 --no-pager
  die "grafana-rp did not come up healthy"
fi

# ---- done ------------------------------------------------------------------

cat <<EOF

  grafana-rp is up.

  endpoints:
    grafana-rp:     http://localhost:3001/rp/
    public:         https://bigcrack.net/rp/
    prometheus:     http://localhost:9090/
    node-exporter:  http://localhost:9100/metrics

  first login: admin / admin (change it)

  dashboards (auto-provisioned, folder 'RP2026'):
    - Live Drive Progress
    - Runs
    - Host (crack)

  to tail: journalctl -u grafana-server-rp -f
  to stop: sudo systemctl stop grafana-server-rp
  to revoke: sudo systemctl disable --now grafana-server-rp && \
             sudo rm -rf /etc/grafana-rp /var/lib/grafana-rp /var/log/grafana-rp \
                         /etc/systemd/system/grafana-server-rp.service

EOF

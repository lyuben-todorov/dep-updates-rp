# Cargo cache metrics

Three files in this directory wire cargo-cache size + fresh-crate-fetch rate
into the existing Prometheus / Grafana stack:

- `cache-metrics.sh` — the collector. Writes a Prometheus text-format file
  that `node_exporter`'s textfile collector picks up.
- `cache-metrics.service` — oneshot systemd unit invoking the script.
- `cache-metrics.timer` — systemd timer firing the service every minute.

## Metrics

- `cargo_cache_bytes{kind="git"}` — bytes under `data/cargo-cache/git/`
- `cargo_cache_bytes{kind="registry"}` — bytes under `data/cargo-cache/registry/`
- `cargo_crates_fetched_5m` — count of registry-cache files with mtime
  in the last 5 minutes. A rough fetch-rate proxy; noisy but directional.

## Install (crack)

Requires `node_exporter` to already be running with the textfile collector
enabled. Default path is `/var/lib/prometheus/node-exporter/`.
If not enabled, add `--collector.textfile.directory=<path>` to the
node_exporter flags and reload.

```sh
# 1. Create the collector dir if missing
sudo mkdir -p /var/lib/prometheus/node-exporter
sudo chown prometheus: /var/lib/prometheus/node-exporter

# 2. Let the user write there (textfile collector runs as prometheus user,
#    but our script runs as ltodorov). Two options:
#    a) make the directory group-writable by a shared group
#    b) give ltodorov write access via ACL
sudo setfacl -m u:ltodorov:rwx /var/lib/prometheus/node-exporter

# 3. Install units (either system-wide as root, or user units)
sudo cp cache-metrics.service /etc/systemd/system/
sudo cp cache-metrics.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cache-metrics.timer

# 4. Verify
systemctl status cache-metrics.timer
cat /var/lib/prometheus/node-exporter/cargo_cache.prom
curl -s localhost:9100/metrics | grep cargo_
```

## Grafana panel

Once metrics are scraping, add two panels to the host dashboard:

- Cache size (stacked area):
  ```
  cargo_cache_bytes{kind="git"}
  cargo_cache_bytes{kind="registry"}
  ```
- Fetch rate (last 5m): `cargo_crates_fetched_5m`

Or derivative of the size series for a smoother MB/h view:
```
deriv(cargo_cache_bytes{kind="registry"}[10m]) * 3600
```

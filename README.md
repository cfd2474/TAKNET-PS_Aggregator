# TAKNET-PS Aggregator v1.0.37

Distributed ADS-B aircraft tracking aggregation system designed for multi-agency public safety deployments. Collects Beast protocol data from a network of Raspberry Pi feeders connected via Tailscale VPN, NetBird VPN, or public IP, deduplicates and processes it through readsb, and provides a web dashboard for monitoring feeders, viewing aircraft on a map, and managing the system.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Fresh VPS Setup (Rocky Linux 8)](#fresh-vps-setup-rocky-linux-8)
- [Installation](#installation)
- [Configuration](#configuration)
- [CLI Reference](#cli-reference)
- [Dashboard Pages](#dashboard-pages)
- [VPN Support](#vpn-support)
- [Port Reference](#port-reference)
- [Data Flow](#data-flow)
- [Database](#database)
- [API Endpoints](#api-endpoints)
- [GeoIP Setup](#geoip-setup)
- [Troubleshooting](#troubleshooting)
- [File Structure](#file-structure)
- [Uninstalling](#uninstalling)
- [Future Roadmap](#future-roadmap)

---

## Overview

TAKNET-PS Aggregator replaces a bare-metal readsb/tar1090 installation with a fully containerized stack that adds feeder management, connection tracking, VPN peer monitoring, and a web-based dashboard. It is designed to run on a Rocky Linux VPS alongside (or in place of) an existing aggregator.

**Key capabilities:**

- Aggregate Beast reduce plus data from 20-30+ feeders simultaneously
- Automatically classify feeders as Tailscale, NetBird, or public IP
- Resolve feeder hostnames via VPN APIs (Tailscale socket on host, NetBird management)
- Track per-feeder connection history, byte counts, and message stats in SQLite
- Display aggregated aircraft on a tar1090 map with graphs1090 statistics
- Provide Docker container management (restart, logs) from the web UI
- Run everything from a single `docker compose up -d` command

---

## Architecture

Six Docker containers in one compose stack on a shared bridge network (`taknet-internal`). Tailscale runs directly on the host and its socket is mounted into containers for peer resolution.

| Container | Image | Exposed Port(s) | Purpose |
|-----------|-------|------------------|---------|
| `beast-proxy` | Custom (Python 3.11) | 30004/tcp | Intercepts Beast reduce plus from feeders, classifies VPN/public, logs to SQLite, forwards to readsb |
| `readsb` | ghcr.io/sdr-enthusiasts/docker-readsb-protobuf | 30003/tcp (SBS out) | ADS-B aggregation engine in net-only mode (no SDR hardware) |
| `mlat-server` | Custom (wiedehopf/mlat-server) | 30105/tcp (in), 39001/tcp (results) | Multilateration — calculates positions from multiple feeder timing data |
| `tar1090` | ghcr.io/sdr-enthusiasts/docker-tar1090 | *(internal only)* | Aircraft map visualization and graphs1090 performance statistics |
| `dashboard` | Custom (Flask/Gunicorn) | *(internal only)* | Web UI, REST API, background scheduler for feeder status updates |
| `nginx` | nginx:alpine | WEB_PORT (default 80) | Reverse proxy routing all web traffic to dashboard, tar1090, and graphs1090 |

```
                               Tailscale (on host)
                                     │
Feeders (Pi) ──Beast 30004──▶ beast-proxy ──▶ readsb:30006 ──▶ tar1090
                │                    │              │                │
                │                    │ SQLite       │ aircraft.json  │
                │                    ▼              ▼                ▼
                │             /data/aggregator.db  /run/readsb/   map + graphs
                │                    │
                │                    │ shared volume
                │                    ▼
                │             dashboard:5000 ◀── nginx:80 ◀── Browser
                │
                └──MLAT 30105──▶ mlat-server ──results 39001──▶ Feeders
```

**Shared volumes:**

- `taknet-db-data` — SQLite database (shared between beast-proxy and dashboard)
- `taknet-readsb-run` — readsb runtime data including aircraft.json (shared with tar1090)
- `taknet-tar1090-data` — tar1090 history and heatmap data
- `taknet-graphs1090-data` — collectd statistics for graphs1090

---

## Prerequisites

- **OS:** Rocky Linux 8.x or 9.x (CentOS Stream, AlmaLinux, or RHEL also work)
- **Hardware:** 2+ CPU cores, 4GB+ RAM, 20GB+ disk (production: 8 cores, 31GB RAM for 30+ feeders)
- **Network:** Public IP with ports open for Beast/MLAT input and web access
- **Docker:** Installed automatically by `install.sh` if not present
- **Tailscale:** Already running on the host — used for feeder VPN connectivity
- **NetBird (optional):** Future VPN, can run alongside Tailscale during migration

---

## Fresh VPS Setup (Rocky Linux 8)

Run these first on a clean image before installing the aggregator:

```bash
# System update and base tools
dnf update -y
dnf install -y epel-release
dnf install -y git curl jq tar rsync

# Install Docker
dnf install -y dnf-utils
dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# Verify
docker compose version
git --version
```

Once that's done, proceed to [Installation](#installation).

---

## Installation

### One-Liner Install

```bash
curl -sSL https://raw.githubusercontent.com/cfd2474/TAKNET-PS_Aggregator/main/install.sh | sudo bash
```

This clones the repo, copies files to `/opt/taknet-aggregator/`, installs Docker if needed, opens firewall ports, and starts all containers.

### Manual Install

```bash
git clone https://github.com/cfd2474/TAKNET-PS_Aggregator.git
cd TAKNET-PS_Aggregator
sudo bash install.sh
```

### Post-Install

Edit site coordinates and VPN settings if needed:

```bash
sudo nano /opt/taknet-aggregator/.env
taknet-agg restart
```

### What the Installer Does

1. Clones the repo from GitHub (if run via curl pipe; skipped if run from local clone)
2. Installs Docker CE and docker-compose-plugin if not present
3. Installs system dependencies (`curl`, `jq`)
4. Deploys files to `/opt/taknet-aggregator/` (preserves existing `.env` on upgrades)
5. Configures firewalld rules for all required ports (80, 30004, 30105, 39001, 30003)
6. Installs the `taknet-agg` CLI tool to `/usr/local/bin/`
7. Runs `docker compose up -d --build` to build and start all containers

### Verify Installation

```bash
taknet-agg status
curl http://localhost
curl http://localhost/api/status
```

---

## Configuration

All configuration is in `/opt/taknet-aggregator/.env`. Changes require a restart (`taknet-agg restart`).

### Web Interface

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_PORT` | `80` | External port for the web dashboard. |

### Aggregator Ports

Feeders send Beast reduce plus on port 30004, MLAT data on port 30105, and receive MLAT results on port 39001.

| Variable | Default | Description |
|----------|---------|-------------|
| `BEAST_PORT` | `30004` | Beast reduce plus input — feeders connect here. |
| `SBS_PORT` | `30003` | SBS (BaseStation) output for downstream consumers. |
| `MLAT_IN_PORT` | `30105` | MLAT data input from feeders. |
| `MLAT_RESULTS_PORT` | `39001` | MLAT calculated position results back to feeders. |

### Site Information

| Variable | Default | Description |
|----------|---------|-------------|
| `SITE_NAME` | `TAKNET-PS Aggregator` | Display name shown in dashboard and tar1090 page title. |
| `SITE_LAT` | `33.8753` | Site latitude (decimal degrees) — used for tar1090 map centering. |
| `SITE_LON` | `-117.5664` | Site longitude — used for tar1090 map centering. |
| `SITE_ALT_FT` | `738` | Site altitude in feet. |
| `TZ` | `America/Los_Angeles` | Timezone for all containers. |

### VPN — Tailscale (on host)

Tailscale runs directly on the host. Its daemon socket is mounted read-only into the beast-proxy and dashboard containers for peer resolution.

| Variable | Default | Description |
|----------|---------|-------------|
| `TAILSCALE_ENABLED` | `true` | Enable Tailscale peer detection and hostname resolution. |
| `TAILSCALE_API_SOCKET` | `/var/run/tailscale/tailscaled.sock` | Path to Tailscale daemon socket on the host. |
| `TAILSCALE_CIDR` | `100.64.0.0/10` | CIDR range used by Tailscale. Connections from this range are classified as Tailscale. |

### VPN — NetBird (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `NETBIRD_ENABLED` | `false` | Enable NetBird peer detection and hostname resolution. |
| `NETBIRD_API_URL` | `http://localhost:33073` | NetBird management API endpoint. |
| `NETBIRD_API_TOKEN` | *(empty)* | Bearer token for NetBird API authentication. |
| `NETBIRD_CIDR` | `100.64.0.0/10` | CIDR range used by NetBird. |

### GeoIP

| Variable | Default | Description |
|----------|---------|-------------|
| `GEOIP_ENABLED` | `true` | Enable GeoIP lookups for public IP feeders. Database is auto-downloaded at build time. |

---

## CLI Reference

The `taknet-agg` command is installed to `/usr/local/bin/` and wraps common Docker Compose operations.

```
taknet-agg <command> [args]
```

| Command | Description |
|---------|-------------|
| `start` | Start all services (`docker compose up -d --build`) |
| `stop` | Stop all services (`docker compose down`) |
| `restart` | Restart all services, or a specific one: `taknet-agg restart dashboard` |
| `status` | Show version and `docker compose ps` output |
| `logs` | Tail logs from all services. Filter by name: `taknet-agg logs beast-proxy` |
| `update` | Pull latest from GitHub, rebuild, and restart |
| `rebuild` | Force recreate all containers from scratch |

---

## Dashboard Pages

### Dashboard (`/`)

Landing page with four clickable stat cards (feeders, aircraft tracked, system uptime, outputs), feeder breakdown by VPN type, system health bars (CPU, memory, disk), and a live activity log showing recent feeder connects/disconnects. Auto-refreshes every 15 seconds.

### Feeders (`/inputs/feeders`)

Sortable and filterable table of all registered feeders. Filter by status (active/stale/offline) and connection type (Tailscale/NetBird/public). Search by name or IP. Click any row to open the feeder detail page. Stat cards at the top show active/stale/total counts.

### Feeder Detail (`/inputs/feeder/<id>`)

Full detail view for a single feeder: connection info (type, IP, hostname, location, first/last seen), statistics (messages, bytes, positions, MLAT status), an edit form (name, tar1090 URL, graphs1090 URL, notes), and a connection history table showing past sessions with duration and bytes transferred. Includes delete button.

### Map (`/map`)

Full-height iframe embedding tar1090. Toolbar shows live aircraft count and a "Full Screen" link to open tar1090 in a new tab. Aircraft data comes from the shared readsb volume.

### Statistics (`/stats`)

Full-height iframe embedding graphs1090. Shows message rate, aircraft count, range, CPU, and other performance metrics over time.

### Outputs (`/outputs`)

Placeholder page for future data sharing to FlightAware, adsb.fi, adsb.lol, airplanes.live, and ADSBCot/TAK. Not yet functional.

### VPN (`/config/vpn`)

Live status display for both VPN providers. Shows Tailscale self-node info (hostname, tailnet, IPs) read from the host's Tailscale socket, online/total peer counts, and a peer table with hostname, IPs, OS, and online status. Same layout for NetBird when enabled. Each section shows appropriate messages when disabled, unreachable, or missing API tokens.

### Services (`/config/services`)

Docker container management. Table of all `taknet-*` containers with name, image, status, started time, and action buttons. "Restart" restarts a single container. "Logs" opens a modal showing the last 200 lines of container logs with auto-scroll to bottom.

### Updates (`/config/updates`)

Version information showing installed version vs. latest available. Currently checks locally. Future versions will query the GitHub Releases API.

### About (`/about`)

Project description, version, architecture summary, and component list with descriptions.

---

## VPN Support

The aggregator supports three simultaneous connection types. Tailscale is the primary VPN and runs on the host — not inside Docker.

When a feeder connects to the beast-proxy on port 30004, the proxy classifies the source IP:

1. **Tailscale** — IP falls within `TAILSCALE_CIDR` and is confirmed via the Tailscale daemon socket API at `/var/run/tailscale/tailscaled.sock` (mounted from the host). Hostname is resolved from the local API's status endpoint.

2. **NetBird** — IP falls within `NETBIRD_CIDR` and is confirmed via the NetBird management API. Hostname is resolved from the `/api/peers` endpoint.

3. **Public** — Any IP not matching a VPN range. Geolocated via db-ip.com City Lite database (auto-downloaded).

When both VPNs use overlapping CIDR ranges (both default to `100.64.0.0/10`), the proxy checks Tailscale first (as the current production VPN), then NetBird. If neither API confirms the peer, the IP is still classified by whichever CIDR range matches.

### Tailscale Socket Access

Tailscale runs on the host. The daemon socket at `/var/run/tailscale/tailscaled.sock` is mounted read-only into the `beast-proxy` and `dashboard` containers via Docker volume bind. This lets them query Tailscale peer status and resolve hostnames without needing the `tailscale` CLI binary or Tailscale running inside any container.

### Dual VPN Migration

This architecture supports a gradual migration from Tailscale to NetBird:

1. Start with `TAILSCALE_ENABLED=true`, `NETBIRD_ENABLED=false` (current state)
2. Install NetBird on the host, set `NETBIRD_ENABLED=true` and provide the API token
3. Migrate feeders one at a time from Tailscale to NetBird
4. Once all feeders are on NetBird, set `TAILSCALE_ENABLED=false`

Both VPNs work simultaneously — feeders on either VPN appear correctly classified in the dashboard.

---

## Port Reference

### External Ports (feeders and browsers connect to these)

| Port | Protocol | Container | Direction | Description |
|------|----------|-----------|-----------|-------------|
| 80 | TCP | nginx | Inbound | Web dashboard |
| 30004 | TCP | beast-proxy | Inbound | Beast reduce plus input from feeders |
| 30105 | TCP | mlat-server | Inbound | MLAT data input from feeders |
| 39001 | TCP | mlat-server | Outbound | MLAT position results back to feeders |
| 30003 | TCP | readsb | Outbound | SBS BaseStation output |

### Internal Ports (container-to-container only)

| Port | Protocol | Container | Description |
|------|----------|-----------|-------------|
| 30006 | TCP | readsb | Beast input (beast-proxy → readsb forwarding) |
| 30005 | TCP | readsb | Beast output (readsb → tar1090) |
| 80 | TCP | tar1090 | Map and graphs web interface (behind nginx) |
| 5000 | TCP | dashboard | Flask app (behind nginx) |

Internal ports are only accessible between containers on the `taknet-internal` Docker network.

---

## Data Flow

### Beast Data (aircraft tracking)

```
Feeder Pi ─── Beast reduce plus (port 30004) ───▶ beast-proxy
                                                      │
                                 ┌────────────────────┤
                                 │                    │
                                 ▼                    ▼
                          SQLite Database        readsb (port 30006)
                          - feeder registry           │
                          - connection log            ├──▶ aircraft.json ──▶ tar1090 (map)
                          - activity log              │
                                 │                    ├──▶ collectd ──▶ graphs1090 (charts)
                                 │                    │
                                 ▼                    └──▶ SBS output (port 30003)
                          dashboard (Flask)
                          - reads feeder DB
                          - reads aircraft.json via tar1090
                          - manages Docker containers
                                 │
                                 ▼
                            nginx (port 80)
                                 │
                                 ▼
                              Browser
```

### MLAT Data (multilateration)

```
Feeder Pi ─── MLAT timing data (port 30105) ───▶ mlat-server
                                                      │
                                                      │ calculates positions from
                                                      │ 3+ feeders seeing same aircraft
                                                      │
Feeder Pi ◀── MLAT results (port 39001) ──────────────┤
                                                      │
readsb:30006 ◀── MLAT results (beast,connect) ───────┘
                       │
                       ▼
                  tar1090 map (MLAT positions appear as purple icons)
```

MLAT data flows directly between feeders and mlat-server — it does not pass through the beast-proxy. The mlat-server is configured with two result outputs: feeders connect to port 39001 to receive calculated positions back, and mlat-server also pushes results into readsb on port 30006 (beast format) so MLAT-derived positions appear on the tar1090 map.

---

## Database

SQLite database stored at `/data/aggregator.db` inside the `taknet-db-data` Docker volume. Shared between `beast-proxy` (writes) and `dashboard` (reads/writes) via WAL mode for concurrent access.

### Tables

**`feeders`** — One row per unique feeder IP. Tracks name, connection type (tailscale/netbird/public), hostname, GeoIP location, latitude/longitude, message/byte/position counters, status, MLAT enabled flag, and user-editable fields (tar1090_url, graphs1090_url, notes). Auto-generates names for new feeders based on hostname (VPN) or city-hash (public).

**`connections`** — One row per feeder TCP session. Records feeder_id, IP, connect time, disconnect time, computed duration in seconds, and bytes transferred. Foreign key to feeders with CASCADE delete.

**`activity_log`** — Event stream for the dashboard activity feed. Records event_type (feeder_connected, feeder_disconnected), feeder_id, and human-readable message. Auto-cleaned after 7 days by the background scheduler.

**`settings`** — Key-value store for application configuration that may be set from the dashboard.

### Status Lifecycle

- **active** — Feeder has been seen within the last 2 minutes
- **stale** — Feeder was active but hasn't been seen for >2 minutes (background job runs every 30 seconds)
- **offline** — Feeder TCP session has ended (set on disconnect by beast-proxy)

---

## API Endpoints

All endpoints return JSON. Base path: `/api/`

### Status

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | Dashboard overview: feeder stats, aircraft count, system health, recent activity |
| GET | `/api/system` | CPU, memory, disk usage, system uptime, app uptime |
| GET | `/api/activity` | Recent activity log (query param: `?limit=N`, default 20) |

### Feeders

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/feeders` | List all feeders with stats summary. Query params: `?status=active&conn_type=tailscale` |
| GET | `/api/feeders/<id>` | Single feeder full details + 20 most recent connections |
| PUT | `/api/feeders/<id>` | Update feeder metadata. Body: JSON with name, tar1090_url, graphs1090_url, notes |
| DELETE | `/api/feeders/<id>` | Delete feeder and cascade-delete connection history |
| GET | `/api/feeders/<id>/connections` | Connection history (query param: `?limit=N`, default 50) |

### Aircraft

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/aircraft` | Current totals from readsb via tar1090: total aircraft, with_position count, message count |

### VPN

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/vpn/status` | Combined Tailscale + NetBird status with full peer lists, online counts, self-node info |

### Docker

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/docker/containers` | List all taknet-* containers: name, status, image, started_at |
| POST | `/api/docker/containers/<n>/restart` | Restart a container (30s timeout). Name must start with `taknet-`. |
| GET | `/api/docker/containers/<n>/logs` | Container logs with timestamps (query param: `?tail=N`, default 100) |

---

## GeoIP Setup

GeoIP is built in automatically. The beast-proxy container downloads the free [db-ip.com](https://db-ip.com/db/lite.php) City Lite database at build time (mmdb format, no registration required). It is enabled by default (`GEOIP_ENABLED=true` in `.env`).

To refresh the database, rebuild the beast-proxy container:

```bash
taknet-agg rebuild
```
5. Restart: `taknet-agg restart beast-proxy`

Public IP feeders will now show city/state location in the dashboard and get auto-generated names like `feeder-corona-ca-a3f2`.

---

## Troubleshooting

### No feeders showing in dashboard

```bash
# Check beast-proxy is listening
taknet-agg logs beast-proxy
ss -tuln | grep 30004

# Check firewall
firewall-cmd --list-ports | grep 30004

# Test connection from another host
nc -zv <VPS_IP> 30004
```

### Dashboard shows "Loading..." or errors

```bash
# Check all containers are running
taknet-agg status

# Check dashboard logs for Python errors
taknet-agg logs dashboard

# Verify readsb is healthy
docker inspect taknet-readsb | jq '.[0].State.Health'

# Check if database is accessible
docker exec taknet-dashboard python3 -c "
import sqlite3
c = sqlite3.connect('/data/aggregator.db')
print(c.execute('SELECT COUNT(*) FROM feeders').fetchone())
"
```

### No aircraft on map

```bash
# Verify readsb is receiving data
docker exec taknet-readsb cat /run/readsb/aircraft.json | jq '.aircraft | length'

# Verify tar1090 is serving data
curl -s http://localhost/tar1090/data/aircraft.json | jq '.aircraft | length'
```

### Tailscale peers not resolving

Tailscale runs on the host. The socket is mounted into containers.

```bash
# Verify socket exists on the host
ls -la /var/run/tailscale/tailscaled.sock

# Verify socket is mounted into container
docker exec taknet-beast-proxy ls -la /var/run/tailscale/

# Test Tailscale API from inside the dashboard container
docker exec taknet-dashboard python3 -c "
import socket, http.client, json
class C(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect('/var/run/tailscale/tailscaled.sock')
c = C('localhost')
c.request('GET', '/localapi/v0/status')
r = c.getresponse()
d = json.loads(r.read())
print(f'Tailnet: {d.get(\"CurrentTailnet\",{}).get(\"Name\")}')
print(f'Peers: {len(d.get(\"Peer\",{}))}')
"
```

### MLAT not working

```bash
# Check mlat-server is running and ports are listening
ss -tuln | grep -E '30105|39001'

# Check mlat-server logs
taknet-agg logs mlat-server
```

### Container won't start or is in a restart loop

```bash
# Check build output for errors
cd /opt/taknet-aggregator && docker compose build dashboard 2>&1 | tail -30

# Check crash logs
docker logs taknet-dashboard --tail 50

# Rebuild from scratch
taknet-agg rebuild
```

### Database locked errors

The SQLite database uses WAL mode with a 5-second busy timeout. If you see locking errors under heavy load, it typically means both beast-proxy and dashboard are writing simultaneously. This should resolve on its own. If persistent:

```bash
taknet-agg restart beast-proxy
```

---

## File Structure

```
taknet-aggregator/
├── VERSION                         # Aggregator version (1.0.37)
├── README.md                       # This file
├── env.example                     # Environment variable template
├── .gitignore
├── docker-compose.yml              # Full 6-container stack definition
├── install.sh                      # Automated installer for Rocky Linux
├── uninstall.sh                    # Clean removal script
│
├── beast-proxy/                    # Beast TCP Proxy container
│   ├── Dockerfile
│   ├── requirements.txt            # maxminddb, requests
│   ├── proxy.py                    # Async TCP server — listens on 30004
│   ├── db.py                       # SQLite write operations
│   ├── vpn_resolver.py             # Tailscale + NetBird IP classification
│   ├── geoip_helper.py             # GeoIP lookups for public IPs (db-ip.com)
│   ├── schema.sql                  # Database schema (CREATE TABLE IF NOT EXISTS)
│   └── GeoLite2-City.mmdb          # Auto-downloaded at build time
│
├── mlat-server/                    # MLAT Server container
│   └── Dockerfile                  # Builds from wiedehopf/mlat-server GitHub repo
│
├── web/                            # Flask Dashboard container
│   ├── Dockerfile
│   ├── requirements.txt            # flask, gunicorn, psutil, docker, apscheduler
│   ├── VERSION
│   ├── schema.sql                  # Schema copy for dashboard-side DB init
│   ├── app.py                      # Flask app factory, blueprint registration, scheduler
│   ├── models.py                   # FeederModel, ConnectionModel, ActivityModel queries
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── dashboard.py            # GET / and /dashboard
│   │   ├── inputs.py               # GET /inputs/feeders, /inputs/feeder/<id>
│   │   ├── pages.py                # GET /map, /stats, /outputs, /about
│   │   ├── config.py               # GET /config, /config/vpn, /config/services, /config/updates
│   │   └── api.py                  # All /api/* JSON endpoints
│   ├── services/
│   │   ├── __init__.py
│   │   ├── docker_service.py       # Container list, restart, logs via Docker socket
│   │   └── vpn_service.py          # Tailscale (host socket) + NetBird status readers
│   └── templates/
│       ├── base.html               # Dark-theme layout, sidebar nav, CSS, JS helpers
│       ├── dashboard.html          # Stat cards, system health, activity log
│       ├── map.html                # tar1090 iframe embed
│       ├── stats.html              # graphs1090 iframe embed
│       ├── outputs.html            # Placeholder for future feed sharing
│       ├── about.html              # Project info and component list
│       ├── inputs/
│       │   ├── feeders.html        # Filterable feeder table
│       │   └── feeder_detail.html  # Single feeder view with edit form
│       └── config/
│           ├── config.html         # Config hub with nav cards
│           ├── vpn.html            # Tailscale + NetBird peer tables
│           ├── services.html       # Docker container management + logs modal
│           └── updates.html        # Version check
│
└── nginx/                          # Nginx Reverse Proxy
    ├── nginx.conf                  # Worker and event config
    └── conf.d/
        └── aggregator.conf         # Routes to dashboard + tar1090 + graphs1090
```

---

## Uninstalling

```bash
sudo bash /opt/taknet-aggregator/uninstall.sh
```

This will:

1. Stop all containers (`docker compose down`)
2. Prompt whether to delete data volumes (database, stats history)
3. Remove `/opt/taknet-aggregator/`
4. Remove the `taknet-agg` CLI from `/usr/local/bin/`
5. Remove the `taknet-internal` Docker network

---

## Future Roadmap

### v1.1 — Outputs

- FlightAware feed integration (Beast + MLAT forwarding)
- adsb.fi / adsb.lol / airplanes.live (Beast forwarding containers)
- ADSBCot for TAK Server integration
- Per-output enable/disable toggle from the dashboard

### v1.2 — Analytics

- Per-feeder performance graphs (message rate over time)
- Coverage heatmaps
- MLAT contribution rankings
- Aircraft type breakdown

### v2.0 — Multi-Aggregator

- Federated architecture with inter-aggregator Beast forwarding
- Shared feeder registry across aggregator instances
- Regional aggregation hierarchy

---

*TAKNET-PS Aggregator v1.0.37 — Built for public safety ADS-B operations.*

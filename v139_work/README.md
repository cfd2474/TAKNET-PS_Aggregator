# TAKNET-PS Aggregator v1.0.54

Distributed ADS-B aircraft tracking aggregation system designed for multi-agency public safety deployments. Collects Beast protocol data from a network of remote feeders connected via NetBird VPN, deduplicates and processes it through readsb, and provides a web dashboard for monitoring feeders, viewing aircraft on a map, and managing the system.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Fresh VPS Setup](#fresh-vps-setup)
- [Installation](#installation)
- [Configuration](#configuration)
- [CLI Reference](#cli-reference)
- [Dashboard Pages](#dashboard-pages)
- [NetBird VPN](#netbird-vpn)
- [Port Reference](#port-reference)
- [Data Flow](#data-flow)
- [Database](#database)
- [API Endpoints](#api-endpoints)
- [Troubleshooting](#troubleshooting)
- [File Structure](#file-structure)
- [Uninstalling](#uninstalling)
- [Roadmap](#roadmap)

---

## Overview

TAKNET-PS Aggregator is a fully containerized ADS-B aggregation stack that collects Beast data from distributed feeders over a NetBird mesh VPN, processes it through readsb, and presents a unified aircraft picture via tar1090.

**Key capabilities:**

- Aggregate raw Beast data from 20–30+ feeders simultaneously with no range restrictions
- Automatically classify and name feeders by VPN peer (NetBird) or public IP (GeoIP)
- Track per-feeder connection history, byte counts, and message stats in SQLite
- Role-based access control (admin / network_admin / viewer)
- Display aggregated aircraft on a live tar1090 map
- Manage Docker containers and perform updates from the web UI
- Deploy with a single `docker compose up -d` command

---

## Architecture

Six Docker containers in one Compose stack on a shared bridge network (`taknet-internal`).

| Container | Image | Exposed Port(s) | Purpose |
|-----------|-------|-----------------|---------|
| `beast-proxy` | Custom (Python 3.11) | 30004/tcp | Receives Beast data from feeders, classifies by VPN peer, logs to SQLite, forwards to readsb |
| `readsb` | ghcr.io/sdr-enthusiasts/docker-readsb-protobuf | 30003/tcp (SBS out) | ADS-B aggregation engine in net-only mode |
| `mlat-server` | Custom (wiedehopf/mlat-server) | 30105/tcp (in), 39001/tcp (results) | Multilateration — calculates positions from multiple feeders |
| `tar1090` | ghcr.io/sdr-enthusiasts/docker-tar1090 | *(internal)* | Aircraft map and performance graphs |
| `dashboard` | Custom (Flask/Gunicorn) | *(internal)* | Web UI, REST API, background scheduler |
| `nginx` | nginx:alpine | 80/tcp | Reverse proxy routing web traffic |

```
Feeders (Pi) ──Beast 30004──▶ beast-proxy ──▶ readsb:30006 ──▶ tar1090 (map)
                                    │               │
                                    ▼               ▼
                              SQLite DB       aircraft.json
                                    │
                                    ▼
                              dashboard ◀── nginx:80 ◀── Browser

Feeders ──MLAT 30105──▶ mlat-server ──results 39001──▶ Feeders
                                │
                         readsb:30006 (MLAT positions on map)
```

**Shared volumes:**

- `taknet-db-data` — SQLite database (beast-proxy + dashboard)
- `taknet-readsb-run` — readsb runtime data shared with tar1090
- `taknet-tar1090-data` — tar1090 history and heatmap data
- `taknet-graphs1090-data` — collectd statistics for graphs1090

---

## Prerequisites

- **OS:** Rocky Linux 8/9 (or CentOS Stream, AlmaLinux, RHEL)
- **Hardware:** 2+ CPU cores, 4GB+ RAM, 20GB+ disk
- **Network:** Public IP with ports open for Beast/MLAT input and web access
- **Docker:** Installed automatically by `install.sh` if not present
- **NetBird:** Self-hosted management server recommended. Reference: https://docs.netbird.io/

---

## Fresh VPS Setup

Run these first on a clean Rocky Linux image:

```bash
dnf update -y
dnf install -y epel-release git curl jq tar rsync

# Install Docker
dnf install -y dnf-utils
dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# Verify
docker compose version
```

---

## Installation

### One-Liner

```bash
curl -sSL https://raw.githubusercontent.com/cfd2474/TAKNET-PS_Aggregator/main/install.sh | sudo bash
```

### Manual

```bash
git clone https://github.com/cfd2474/TAKNET-PS_Aggregator.git
cd TAKNET-PS_Aggregator
sudo bash install.sh
```

### Post-Install

```bash
sudo nano /opt/taknet-aggregator/.env
taknet-agg restart
```

### What the Installer Does

1. Installs Docker CE if not present
2. Deploys files to `/opt/taknet-aggregator/` (preserves existing `.env` on upgrades)
3. Configures firewalld rules (ports 80, 30004, 30105, 39001, 30003)
4. Installs `taknet-agg` CLI to `/usr/local/bin/`
5. Runs `docker compose up -d --build`

---

## Configuration

All configuration is in `/opt/taknet-aggregator/.env`. Changes require `taknet-agg restart`.

### Web Interface

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_PORT` | `80` | External port for the web dashboard |
| `SECRET_KEY` | *(set this)* | Flask session secret — set to a random string in production |
| `SITE_NAME` | `TAKNET-PS Aggregator` | Display name in dashboard and map title |
| `TZ` | `America/Los_Angeles` | Timezone for all containers |

### Aggregator Ports

| Variable | Default | Description |
|----------|---------|-------------|
| `BEAST_PORT` | `30004` | Beast data input from feeders |
| `SBS_PORT` | `30003` | SBS (BaseStation) output |
| `MLAT_IN_PORT` | `30105` | MLAT data input from feeders |
| `MLAT_RESULTS_PORT` | `39001` | MLAT position results back to feeders |

### VPN — NetBird (Primary)

NetBird is the primary feeder connectivity method. For setup documentation see https://docs.netbird.io/

| Variable | Default | Description |
|----------|---------|-------------|
| `NETBIRD_ENABLED` | `true` | Enable NetBird peer detection and hostname resolution |
| `NETBIRD_API_URL` | `https://netbird.yourdomain.com` | NetBird management API endpoint |
| `NETBIRD_API_TOKEN` | *(required)* | Service user PAT for NetBird API authentication |
| `NETBIRD_CIDR` | `100.64.0.0/10` | CIDR range used by NetBird |

### GeoIP

| Variable | Default | Description |
|----------|---------|-------------|
| `GEOIP_ENABLED` | `true` | Enable GeoIP lookups for public IP feeders (db-ip.com City Lite, auto-downloaded) |

---

## CLI Reference

```
taknet-agg <command>
```

| Command | Description |
|---------|-------------|
| `start` | Start all services |
| `stop` | Stop all services |
| `restart [service]` | Restart all services, or a specific one |
| `status` | Show version and container status |
| `logs [service]` | Tail logs from all or a specific service |
| `update` | Pull latest from GitHub, rebuild, and restart |
| `rebuild` | Force recreate all containers |

---

## Dashboard Pages

### Dashboard (`/`)
Overview with stat cards (feeders, aircraft, system uptime), feeder breakdown by connection type, system health (CPU, memory, disk), and live activity log. Auto-refreshes every 15 seconds.

### Feeders (`/inputs/feeders`)
Sortable, filterable table of all registered feeders. Filter by status and connection type. Click a row for the detail view.

### Feeder Detail (`/inputs/feeder/<id>`)
Full detail: connection info, statistics, edit form (name, tar1090 URL, notes), and connection history.

### Map (`/map`)
Full-page tar1090 embed with live aircraft count in the toolbar. Accessible to all roles including viewer.

### Statistics (`/stats`)
Full-page graphs1090 embed showing message rate, aircraft count, range, and CPU over time.

### VPN (`/config/vpn`)
Live NetBird peer status — online/total count, peer table with hostname, IP, and connection state. Also shows the NetBird client enrollment state for the aggregator server itself.

### Services (`/config/services`)
Docker container management. Restart individual containers or use **Restart All Services** for a full soft reset. View last 200 lines of logs per container.

### Updates (`/config/updates`)
Checks GitHub for the latest version and runs the web update workflow with live log streaming.

### Users (`/config/users`)
User management (admin only). Create users, assign roles, reset passwords.

---

## NetBird VPN

NetBird is the primary VPN for feeder connectivity. The aggregator server runs a NetBird client container that enrolls into your NetBird management server, providing a mesh VPN IP that feeders connect to.

**Reference documentation:** https://docs.netbird.io/

### How It Works

When a feeder connects to beast-proxy on port 30004, the source IP is classified:

1. **NetBird** — IP falls within `NETBIRD_CIDR` and is confirmed via the NetBird management API. Hostname is resolved from `/api/peers`.
2. **Public** — Any IP not matching the VPN range. Geolocated via db-ip.com.

### Server Enrollment

The aggregator server itself enrolls into NetBird via the **VPN → NetBird Enrollment** section of the dashboard. Enter a setup key from your NetBird management console and click Enroll. The NetBird client runs as a Docker container on the aggregator host.

### Feeder Setup

Each feeder must be enrolled in the same NetBird network and configured to send Beast data to the aggregator's NetBird IP (e.g. `vpn.yourdomain.com`) on port 30004 using `beast_out` (not `beast_reduce_plus_out`).

```
ULTRAFEEDER_CONFIG=adsb,vpn.yourdomain.com,30004,beast_out;mlat,vpn.yourdomain.com,30105,39001
```

> **Important:** Use `beast_out`, not `beast_reduce_plus_out`. The reduce format strips position data, resulting in Mode-S only aircraft with no map positions.

---

## Port Reference

### External

| Port | Protocol | Direction | Description |
|------|----------|-----------|-------------|
| 80 | TCP | Inbound | Web dashboard |
| 30004 | TCP | Inbound | Beast data input from feeders |
| 30105 | TCP | Inbound | MLAT data input from feeders |
| 39001 | TCP | Outbound | MLAT position results to feeders |
| 30003 | TCP | Outbound | SBS BaseStation output |

### Internal (container-to-container only)

| Port | Container | Description |
|------|-----------|-------------|
| 30006 | readsb | Beast input (from beast-proxy) |
| 30005 | readsb | Beast output (to tar1090) |
| 5000 | dashboard | Flask app (behind nginx) |

---

## Data Flow

### Beast (aircraft tracking)

```
Feeder ──Beast (30004)──▶ beast-proxy ──▶ readsb (30006)
                               │                │
                               ▼                ├──▶ tar1090 (map)
                          SQLite DB             ├──▶ graphs1090
                               │                └──▶ SBS (30003)
                               ▼
                          dashboard ──▶ nginx ──▶ Browser
```

### MLAT (multilateration)

```
Feeders ──timing (30105)──▶ mlat-server ──▶ results (39001)──▶ Feeders
                                  └──▶ readsb (30006) ──▶ MLAT positions on map
```

---

## Database

SQLite at `/data/aggregator.db` in the `taknet-db-data` volume. WAL mode for concurrent beast-proxy (writes) and dashboard (reads/writes) access.

### Tables

- **`feeders`** — One row per unique feeder. Tracks connection type, hostname, GeoIP location, message/byte/position counters, status, and user-editable fields.
- **`connections`** — One row per TCP session with duration and bytes transferred.
- **`activity_log`** — Event stream for the dashboard feed. Auto-cleaned after 7 days.
- **`settings`** — Key-value store for dashboard configuration.
- **`users`** — Authentication: username, bcrypt password hash, role (admin/network_admin/viewer).
- **`update_history`** — Log of version updates performed via web UI or CLI.

### Feeder Status Lifecycle

- **active** — Seen within the last 2 minutes
- **stale** — Not seen for >2 minutes (checked every 30 seconds)
- **offline** — TCP session ended

---

## API Endpoints

All endpoints return JSON and require authentication. Base path: `/api/`

| Method | Path | Role | Description |
|--------|------|------|-------------|
| GET | `/api/status` | network_admin | Dashboard overview data |
| GET | `/api/aircraft` | viewer | Aircraft totals |
| GET | `/api/feeders` | network_admin | List all feeders |
| GET | `/api/feeders/<id>` | network_admin | Single feeder detail |
| PUT | `/api/feeders/<id>` | network_admin | Update feeder metadata |
| DELETE | `/api/feeders/<id>` | admin | Delete feeder |
| GET | `/api/vpn/status` | admin | NetBird peer status |
| GET | `/api/docker/containers` | admin | List containers |
| POST | `/api/docker/containers/<n>/restart` | admin | Restart a container |
| POST | `/api/docker/restart-all` | admin | Restart all containers (soft reset) |
| GET | `/api/docker/containers/<n>/logs` | admin | Container logs |
| GET | `/api/updates/check` | admin | Check GitHub for latest version |
| POST | `/api/updates/run` | admin | Start web update |

---

## Troubleshooting

### No feeders appearing

```bash
taknet-agg logs beast-proxy
ss -tuln | grep 30004
firewall-cmd --list-ports | grep 30004
```

### Aircraft showing as Mode-S only (no positions)

Feeder is sending `beast_reduce_plus_out` instead of `beast_out`. Update feeder ULTRAFEEDER_CONFIG — replace `beast_reduce_plus_out` with `beast_out` for the aggregator entry only.

### Aircraft from remote feeders not appearing on map

Ensure `READSB_LAT`, `READSB_LON`, and tar1090 `LAT`/`LONG` are not set in docker-compose.yml. Fixed reference coordinates cause CPR range check failures for distant aircraft.

### NetBird peers not resolving

```bash
# Verify API token is set in .env
grep NETBIRD_API_TOKEN /opt/taknet-aggregator/.env

# Test API reachability from beast-proxy container
docker exec taknet-beast-proxy curl -s -H "Authorization: Token $NETBIRD_API_TOKEN" \
  $NETBIRD_API_URL/api/peers | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d), 'peers')"
```

### Dashboard errors

```bash
taknet-agg status
taknet-agg logs dashboard
```

### Container in restart loop

```bash
docker logs taknet-dashboard --tail 50
taknet-agg rebuild
```

---

## File Structure

```
taknet-aggregator/
├── VERSION
├── README.md
├── RELEASES.json
├── env.example
├── docker-compose.yml
├── install.sh
├── uninstall.sh
│
├── beast-proxy/
│   ├── Dockerfile
│   ├── proxy.py              # Async TCP server — listens on 30004
│   ├── db.py                 # SQLite write operations
│   ├── vpn_resolver.py       # NetBird IP classification + hostname resolution
│   ├── geoip_helper.py       # GeoIP for public IPs
│   └── schema.sql
│
├── mlat-server/
│   └── Dockerfile
│
├── web/
│   ├── Dockerfile
│   ├── app.py                # Flask app factory, Flask-Login, scheduler
│   ├── models.py             # DB models: Feeder, Connection, Activity, User
│   ├── routes/
│   │   ├── auth.py           # Login, logout, profile
│   │   ├── auth_utils.py     # Role decorators: admin_required, network_admin_required
│   │   ├── dashboard.py
│   │   ├── inputs.py
│   │   ├── pages.py
│   │   ├── config.py         # VPN, services, updates, user management
│   │   └── api.py            # All /api/* JSON endpoints
│   ├── services/
│   │   ├── docker_service.py
│   │   └── vpn_service.py
│   ├── static/
│   │   └── img/
│   │       └── taknetlogo.png
│   └── templates/
│       ├── base.html
│       ├── auth/
│       │   ├── login.html
│       │   └── profile.html
│       ├── config/
│       │   ├── vpn.html
│       │   ├── services.html
│       │   ├── updates.html
│       │   └── users.html
│       └── inputs/
│           ├── feeders.html
│           └── feeder_detail.html
│
└── nginx/
    ├── nginx.conf
    └── conf.d/
        └── aggregator.conf
```

---

## Uninstalling

```bash
sudo bash /opt/taknet-aggregator/uninstall.sh
```

Stops containers, optionally removes data volumes, removes install directory and CLI.

---

## Roadmap

### v1.1 — Outputs
- FlightAware, adsb.fi, adsb.lol, airplanes.live feed forwarding
- ADSBCot for TAK Server integration
- Per-output toggle from dashboard

### v1.2 — Analytics
- Per-feeder performance graphs
- Coverage heatmaps
- MLAT contribution rankings

### v2.0 — Multi-Aggregator
- Federated inter-aggregator Beast forwarding
- Shared feeder registry across instances

---

*TAKNET-PS Aggregator v1.0.54 — Built for public safety ADS-B operations.*

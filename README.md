# TAKNET-PS Aggregator v1.0.0

Distributed ADS-B aircraft tracking aggregation system for multi-agency public safety deployments.

## Quick Start

```bash
git clone https://github.com/<user>/taknet-aggregator
cd taknet-aggregator
cp .env.example .env
nano .env                    # Edit site coordinates, ports, VPN settings
sudo bash install.sh
```

## Architecture

Six Docker containers in one compose stack:

| Container | Purpose |
|-----------|---------|
| **beast-proxy** | TCP proxy — feeder tracking, VPN classification, Beast forwarding |
| **readsb** | ADS-B aggregation engine (net-only) |
| **mlat-server** | Multilateration positioning |
| **tar1090** | Aircraft map + graphs1090 statistics |
| **dashboard** | Flask web UI and API |
| **nginx** | Reverse proxy and routing |

## VPN Support

Supports three connection types simultaneously:
- **Tailscale** — current feeder VPN (reads status via socket API)
- **NetBird** — future feeder VPN (reads status via management API)
- **Public IP** — direct connections with optional GeoIP geolocation

## CLI

```bash
taknet-agg status     # Show service status
taknet-agg logs       # View logs
taknet-agg restart    # Restart all services
taknet-agg cutover    # Switch to production ports (80/30005)
```

## Ports

| Port | Service | Purpose |
|------|---------|---------|
| 8080 | nginx | Web dashboard (80 after cutover) |
| 30005 | beast-proxy | Beast input from feeders |
| 30003 | readsb | SBS output |
| 30007 | mlat-server | MLAT input |

## Configuration

Edit `/opt/taknet-aggregator/.env` — see `.env.example` for all options.

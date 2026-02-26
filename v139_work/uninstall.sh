#!/bin/bash
# =============================================================================
# TAKNET-PS Aggregator — Uninstaller
# =============================================================================
set -e

INSTALL_DIR="/opt/taknet-aggregator"
DATA_DIR="/var/lib/taknet-aggregator"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

[[ $EUID -ne 0 ]] && { echo -e "${RED}Run as root${NC}"; exit 1; }

echo ""
echo "TAKNET-PS Aggregator — Uninstall"
echo ""

read -p "This will stop all services and remove the installation. Continue? [y/N] " -n 1 -r
echo ""
[[ ! $REPLY =~ ^[Yy]$ ]] && { echo "Cancelled."; exit 0; }

# Stop containers
if [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
    echo "Stopping containers..."
    cd "$INSTALL_DIR" && docker compose down --remove-orphans 2>/dev/null || true
fi

# Remove volumes
read -p "Remove data volumes (database, stats)? [y/N] " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    docker volume rm taknet-db-data taknet-readsb-run taknet-tar1090-data taknet-graphs1090-data 2>/dev/null || true
    rm -rf "$DATA_DIR"
    echo -e "${YELLOW}Data volumes removed${NC}"
fi

# Remove files
rm -rf "$INSTALL_DIR"
rm -f /usr/local/bin/taknet-agg

# Remove network
docker network rm taknet-internal 2>/dev/null || true

echo ""
echo -e "${GREEN}TAKNET-PS Aggregator has been uninstalled.${NC}"
echo ""

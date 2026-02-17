#!/bin/bash
# =============================================================================
# TAKNET-PS Aggregator v1.0.2 — Installer
# Target: Rocky Linux 8.x / 9.x
# =============================================================================
set -e

INSTALL_DIR="/opt/taknet-aggregator"
DATA_DIR="/var/lib/taknet-aggregator"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION=$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo "1.0.2")

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

echo ""
echo "=========================================="
echo "  TAKNET-PS Aggregator v${VERSION}"
echo "  Installation Script"
echo "=========================================="
echo ""

# Check root
[[ $EUID -ne 0 ]] && err "This script must be run as root (sudo bash install.sh)"

# ── 1. Install Docker if missing ────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    info "Installing Docker..."
    dnf install -y dnf-utils
    dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    ok "Docker installed"
else
    ok "Docker already installed ($(docker --version | awk '{print $3}'))"
fi

# Verify docker compose
if ! docker compose version &>/dev/null; then
    err "docker compose plugin not found. Install docker-compose-plugin."
fi

# ── 2. Install dependencies ─────────────────────────────────────────────────
info "Installing system dependencies..."
dnf install -y curl jq 2>/dev/null || true
ok "Dependencies installed"

# ── 3. Create directories ───────────────────────────────────────────────────
info "Setting up directories..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$DATA_DIR"
ok "Directories created"

# ── 4. Copy files ───────────────────────────────────────────────────────────
info "Copying files to ${INSTALL_DIR}..."
cp -r "$SCRIPT_DIR"/* "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR"/.gitignore "$INSTALL_DIR/" 2>/dev/null || true
cp -r "$SCRIPT_DIR"/.env.example "$INSTALL_DIR/" 2>/dev/null || true

# Create .env from example if it doesn't exist
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    warn "Created .env from template — edit $INSTALL_DIR/.env to customize"
fi
ok "Files copied"

# ── 5. Create placeholder GeoIP database ────────────────────────────────────
if [ ! -f "$INSTALL_DIR/beast-proxy/GeoLite2-City.mmdb" ]; then
    touch "$INSTALL_DIR/beast-proxy/GeoLite2-City.mmdb"
    warn "GeoIP database not found. Download GeoLite2-City.mmdb from MaxMind"
    warn "  and place it at: $INSTALL_DIR/beast-proxy/GeoLite2-City.mmdb"
fi

# ── 6. Firewall rules ───────────────────────────────────────────────────────
if command -v firewall-cmd &>/dev/null && systemctl is-active --quiet firewalld; then
    info "Configuring firewall..."
    # Source .env for ports
    source "$INSTALL_DIR/.env"
    WEB=${WEB_PORT:-8080}
    BEAST=${BEAST_PORT:-30004}
    SBS=${SBS_PORT:-30003}
    MLAT_IN=${MLAT_IN_PORT:-30105}
    MLAT_RESULTS=${MLAT_RESULTS_PORT:-39001}

    firewall-cmd --permanent --add-port=${WEB}/tcp 2>/dev/null || true
    firewall-cmd --permanent --add-port=${BEAST}/tcp 2>/dev/null || true
    firewall-cmd --permanent --add-port=${SBS}/tcp 2>/dev/null || true
    firewall-cmd --permanent --add-port=${MLAT_IN}/tcp 2>/dev/null || true
    firewall-cmd --permanent --add-port=${MLAT_RESULTS}/tcp 2>/dev/null || true
    firewall-cmd --reload 2>/dev/null || true
    ok "Firewall rules added (ports: ${WEB}, ${BEAST}, ${SBS}, ${MLAT_IN}, ${MLAT_RESULTS})"
else
    warn "firewalld not active — configure your firewall manually"
fi

# ── 7. Create CLI tool ──────────────────────────────────────────────────────
info "Installing CLI tool..."
cat > /usr/local/bin/taknet-agg << 'CLIEOF'
#!/bin/bash
INSTALL_DIR="/opt/taknet-aggregator"
cd "$INSTALL_DIR" || { echo "Error: $INSTALL_DIR not found"; exit 1; }

case "${1:-help}" in
    start)
        docker compose up -d --build
        ;;
    stop)
        docker compose down
        ;;
    restart)
        docker compose restart ${2:-}
        ;;
    status)
        echo "=== TAKNET-PS Aggregator ==="
        echo "Version: $(cat VERSION 2>/dev/null || echo unknown)"
        echo ""
        docker compose ps
        ;;
    logs)
        docker compose logs ${2:---tail=50} ${3:-}
        ;;
    update)
        echo "Pulling latest images..."
        docker compose pull
        docker compose up -d --build
        echo "Update complete."
        ;;
    rebuild)
        docker compose up -d --build --force-recreate
        ;;
    cutover)
        echo "Switching to production ports (80, 30004)..."
        sed -i 's/^WEB_PORT=.*/WEB_PORT=80/' .env
        sed -i 's/^BEAST_PORT=.*/BEAST_PORT=30004/' .env
        docker compose up -d --build
        echo "Cutover complete. Dashboard on port 80, Beast on port 30004."
        ;;
    help|*)
        echo "Usage: taknet-agg <command> [args]"
        echo ""
        echo "Commands:"
        echo "  start      Start all services"
        echo "  stop       Stop all services"
        echo "  restart    Restart all or specific service"
        echo "  status     Show service status"
        echo "  logs       Show logs (optional: service name)"
        echo "  update     Pull latest and restart"
        echo "  rebuild    Force rebuild all containers"
        echo "  cutover    Switch to production ports (80/30004)"
        ;;
esac
CLIEOF
chmod +x /usr/local/bin/taknet-agg
ok "CLI tool installed: taknet-agg"

# ── 8. Build and start ──────────────────────────────────────────────────────
info "Building and starting containers..."
cd "$INSTALL_DIR"
docker compose up -d --build

echo ""
echo "=========================================="
echo -e "  ${GREEN}Installation Complete!${NC}"
echo "=========================================="
echo ""
echo "  Dashboard:  http://$(hostname -I | awk '{print $1}'):$(grep WEB_PORT .env | cut -d= -f2 || echo 8080)"
echo "  Beast Port: $(grep BEAST_PORT .env | cut -d= -f2 || echo 30004)"
echo ""
echo "  CLI:        taknet-agg status"
echo "  Config:     $INSTALL_DIR/.env"
echo "  Data:       $DATA_DIR/"
echo ""
echo "  Point feeders to $(hostname -I | awk '{print $1}'):$(grep BEAST_PORT .env | cut -d= -f2 || echo 30004)"
echo ""

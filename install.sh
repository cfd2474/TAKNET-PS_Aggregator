#!/bin/bash
# =============================================================================
# TAKNET-PS Aggregator v1.0.5 — Installer
# Target: Rocky Linux 8.x / 9.x
#
# Install methods:
#   curl -sSL https://raw.githubusercontent.com/cfd2474/TAKNET-PS_Aggregator/main/install.sh | sudo bash
#   git clone https://github.com/cfd2474/TAKNET-PS_Aggregator.git && cd TAKNET-PS_Aggregator && sudo bash install.sh
# =============================================================================
set -e

REPO_URL="https://github.com/cfd2474/TAKNET-PS_Aggregator.git"
INSTALL_DIR="/opt/taknet-aggregator"
DATA_DIR="/var/lib/taknet-aggregator"

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
echo "  TAKNET-PS Aggregator — Installer"
echo "=========================================="
echo ""

# ── Check root ──────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && err "Run as root: curl ... | sudo bash  OR  sudo bash install.sh"

# ── Determine source: piped from curl or run from cloned repo ───────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" 2>/dev/null)" 2>/dev/null && pwd)"
SOURCE_DIR=""

if [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
    # Running from inside cloned repo
    SOURCE_DIR="$SCRIPT_DIR"
    ok "Running from local repo: $SOURCE_DIR"
else
    # Piped from curl or run outside repo — need to clone
    info "Cloning from GitHub..."
    dnf install -y git 2>/dev/null || yum install -y git 2>/dev/null || true
    TMPDIR=$(mktemp -d)
    git clone --depth 1 "$REPO_URL" "$TMPDIR/taknet-aggregator"
    SOURCE_DIR="$TMPDIR/taknet-aggregator"
    ok "Cloned to $SOURCE_DIR"
fi

VERSION=$(cat "$SOURCE_DIR/VERSION" 2>/dev/null || echo "unknown")
info "Version: v${VERSION}"

# ── 1. Install Docker if missing ────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    info "Installing Docker..."
    dnf install -y dnf-utils 2>/dev/null || true
    dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo 2>/dev/null || true
    dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    ok "Docker installed"
else
    ok "Docker already installed ($(docker --version | awk '{print $3}'))"
fi

docker compose version &>/dev/null || err "docker compose plugin not found"

# ── 2. System dependencies ──────────────────────────────────────────────────
info "Installing dependencies..."
dnf install -y curl jq 2>/dev/null || true
ok "Dependencies ready"

# ── 3. Deploy files ─────────────────────────────────────────────────────────
info "Deploying to ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
rsync -a --exclude='.git' "$SOURCE_DIR/" "$INSTALL_DIR/" 2>/dev/null || cp -a "$SOURCE_DIR"/. "$INSTALL_DIR/"

# Create .env if missing — preserve existing on upgrades
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    warn "Created .env — edit /opt/taknet-aggregator/.env to customize"
else
    ok "Existing .env preserved"
fi
ok "Files deployed"

# ── 4. GeoIP placeholder ───────────────────────────────────────────────────
if [ ! -s "$INSTALL_DIR/beast-proxy/GeoLite2-City.mmdb" ]; then
    touch "$INSTALL_DIR/beast-proxy/GeoLite2-City.mmdb"
    warn "GeoIP: place GeoLite2-City.mmdb in $INSTALL_DIR/beast-proxy/ to enable"
fi

# ── 5. Firewall ─────────────────────────────────────────────────────────────
if command -v firewall-cmd &>/dev/null && systemctl is-active --quiet firewalld; then
    info "Opening firewall ports..."
    source "$INSTALL_DIR/.env"
    for port in ${WEB_PORT:-80} ${BEAST_PORT:-30004} ${SBS_PORT:-30003} ${MLAT_IN_PORT:-30105} ${MLAT_RESULTS_PORT:-39001}; do
        firewall-cmd --permanent --add-port=${port}/tcp 2>/dev/null || true
    done
    firewall-cmd --reload 2>/dev/null || true
    ok "Firewall configured"
else
    warn "firewalld not active — open ports manually if needed"
fi

# ── 6. CLI tool ─────────────────────────────────────────────────────────────
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
        echo "Pulling latest from GitHub..."
        cd /tmp && rm -rf taknet-update
        git clone --depth 1 https://github.com/cfd2474/TAKNET-PS_Aggregator.git taknet-update
        rsync -a --exclude='.git' --exclude='.env' /tmp/taknet-update/ "$INSTALL_DIR/" 2>/dev/null || \
            cp -a /tmp/taknet-update/. "$INSTALL_DIR/"
        rm -rf /tmp/taknet-update
        docker compose pull 2>/dev/null || true
        docker compose up -d --build
        echo "Updated to v$(cat VERSION 2>/dev/null || echo unknown)"
        ;;
    rebuild)
        docker compose up -d --build --force-recreate
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
        echo "  update     Pull latest from GitHub and restart"
        echo "  rebuild    Force rebuild all containers"
        ;;
esac
CLIEOF
chmod +x /usr/local/bin/taknet-agg
ok "CLI installed: taknet-agg"

# ── 7. Build and start ─────────────────────────────────────────────────────
info "Building and starting containers..."
cd "$INSTALL_DIR"
docker compose up -d --build

# ── Cleanup temp clone if we made one ───────────────────────────────────────
[ -n "$TMPDIR" ] && rm -rf "$TMPDIR"

# ── Done ────────────────────────────────────────────────────────────────────
source "$INSTALL_DIR/.env" 2>/dev/null || true
IP=$(hostname -I | awk '{print $1}')
echo ""
echo "=========================================="
echo -e "  ${GREEN}TAKNET-PS Aggregator v${VERSION} — Installed${NC}"
echo "=========================================="
echo ""
echo "  Dashboard:   http://${IP}"
echo "  Beast Input:  ${IP}:${BEAST_PORT:-30004}"
echo "  MLAT Input:   ${IP}:${MLAT_IN_PORT:-30105}"
echo "  MLAT Results: ${IP}:${MLAT_RESULTS_PORT:-39001}"
echo ""
echo "  CLI:     taknet-agg status"
echo "  Config:  $INSTALL_DIR/.env"
echo "  Update:  taknet-agg update"
echo ""

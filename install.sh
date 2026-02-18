#!/bin/bash
# =============================================================================
# TAKNET-PS Aggregator v1.0.32 — Installer
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

# ── Early dependencies (needed before clone) ────────────────────────────────
for pkg in git curl jq tar; do
    if ! command -v "$pkg" &>/dev/null; then
        info "Installing $pkg..."
        dnf install -y "$pkg" 2>/dev/null || true
    fi
done
if ! command -v sqlite3 &>/dev/null; then
    info "Installing sqlite..."
    dnf install -y sqlite 2>/dev/null || true
fi

# ── Determine source: local repo or curl pipe ──────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" 2>/dev/null)" 2>/dev/null && pwd)"
CLEANUP_DIR=""

if [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
    SOURCE_DIR="$SCRIPT_DIR"
    ok "Running from local repo: $SOURCE_DIR"
else
    info "Cloning from GitHub..."
    CLEANUP_DIR=$(mktemp -d)
    git clone --depth 1 "$REPO_URL" "$CLEANUP_DIR/repo"
    SOURCE_DIR="$CLEANUP_DIR/repo"
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

# ── 2. Deploy files ─────────────────────────────────────────────────────────
info "Deploying to ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR" "$DATA_DIR"

if command -v rsync &>/dev/null; then
    rsync -a --exclude='.git' "$SOURCE_DIR/" "$INSTALL_DIR/"
else
    shopt -s dotglob
    cp -a "$SOURCE_DIR"/* "$INSTALL_DIR/" 2>/dev/null || true
    shopt -u dotglob
    rm -rf "$INSTALL_DIR/.git" 2>/dev/null || true
fi
ok "Files deployed"

# ── 3. Create or fix .env ───────────────────────────────────────────────────
if [ -f "$INSTALL_DIR/.env" ]; then
    # Fix unquoted SITE_NAME from earlier versions
    sed -i '/^SITE_NAME=/{/"/!s/=\(.*\)/="\1"/}' "$INSTALL_DIR/.env" 2>/dev/null || true
    ok "Existing .env preserved"
elif [ -f "$INSTALL_DIR/env.example" ]; then
    cp "$INSTALL_DIR/env.example" "$INSTALL_DIR/.env"
    warn "Created .env from env.example — edit $INSTALL_DIR/.env to customize"
else
    cat > "$INSTALL_DIR/.env" << 'ENVEOF'
WEB_PORT=80
BEAST_PORT=30004
SBS_PORT=30003
MLAT_IN_PORT=30105
MLAT_RESULTS_PORT=39001
SITE_NAME="TAKNET-PS Aggregator"
SITE_LAT=33.8753
SITE_LON=-117.5664
SITE_ALT_FT=738
TZ=America/Los_Angeles
TAILSCALE_ENABLED=true
TAILSCALE_API_SOCKET=/var/run/tailscale/tailscaled.sock
TAILSCALE_CIDR=100.64.0.0/10
NETBIRD_ENABLED=false
NETBIRD_API_URL=http://localhost:33073
NETBIRD_API_TOKEN=
NETBIRD_CIDR=100.64.0.0/10
GEOIP_ENABLED=true
ENVEOF
    warn "Created .env with defaults — edit $INSTALL_DIR/.env to customize"
fi

# ── 4. Firewall ─────────────────────────────────────────────────────────────
if command -v firewall-cmd &>/dev/null && systemctl is-active --quiet firewalld; then
    info "Opening firewall ports..."
    eval "$(grep -E '^(WEB_PORT|BEAST_PORT|SBS_PORT|MLAT_IN_PORT|MLAT_RESULTS_PORT)=' "$INSTALL_DIR/.env" 2>/dev/null)"
    for port in ${WEB_PORT:-80} ${BEAST_PORT:-30004} ${SBS_PORT:-30003} ${MLAT_IN_PORT:-30105} ${MLAT_RESULTS_PORT:-39001}; do
        firewall-cmd --permanent --add-port="${port}/tcp" 2>/dev/null || true
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
        OLD_VERSION=$(cat VERSION 2>/dev/null || echo "unknown")
        echo "Pulling latest from GitHub..."
        TMPDIR=$(mktemp -d)
        git clone --depth 1 https://github.com/cfd2474/TAKNET-PS_Aggregator.git "$TMPDIR/repo"
        shopt -s dotglob
        cp -a "$TMPDIR/repo"/* "$INSTALL_DIR/" 2>/dev/null || true
        shopt -u dotglob
        rm -rf "$INSTALL_DIR/.git" "$TMPDIR"
        docker compose pull 2>/dev/null || true
        docker compose up -d --build
        NEW_VERSION=$(cat VERSION 2>/dev/null || echo "unknown")
        echo "Updated from v$OLD_VERSION to v$NEW_VERSION"
        # Log update to database
        DB_FILE="/var/lib/docker/volumes/taknet-db-data/_data/aggregator.db"
        if command -v sqlite3 &>/dev/null && [ -f "$DB_FILE" ]; then
            sqlite3 "$DB_FILE" "CREATE TABLE IF NOT EXISTS update_history (id INTEGER PRIMARY KEY AUTOINCREMENT, from_version TEXT, to_version TEXT, success BOOLEAN DEFAULT 1, output TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP);"
            sqlite3 "$DB_FILE" "INSERT INTO update_history (from_version, to_version, success, output) VALUES ('$OLD_VERSION', '$NEW_VERSION', 1, 'Updated via taknet-agg update');"
        fi
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
[ -n "$CLEANUP_DIR" ] && rm -rf "$CLEANUP_DIR"

# ── Done ────────────────────────────────────────────────────────────────────
eval "$(grep -E '^(WEB_PORT|BEAST_PORT|MLAT_IN_PORT|MLAT_RESULTS_PORT)=' "$INSTALL_DIR/.env" 2>/dev/null)"
IP=$(hostname -I | awk '{print $1}')
echo ""
echo "=========================================="
echo -e "  ${GREEN}TAKNET-PS Aggregator v${VERSION} — Installed${NC}"
echo "=========================================="
echo ""
echo "  Dashboard:    http://${IP}"
echo "  Beast Input:  ${IP}:${BEAST_PORT:-30004}"
echo "  MLAT Input:   ${IP}:${MLAT_IN_PORT:-30105}"
echo "  MLAT Results: ${IP}:${MLAT_RESULTS_PORT:-39001}"
echo ""
echo "  CLI:     taknet-agg status"
echo "  Config:  $INSTALL_DIR/.env"
echo "  Update:  taknet-agg update"
echo ""

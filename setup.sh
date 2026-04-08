#!/usr/bin/env bash
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ ERR]${NC}  $*"; exit 1; }

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo "  ╦═╗┌─┐┌─┐┌┬┐┌─┐┬┌┐┌┌─┐"
echo "  ╠╦╝├┤ ├─┤ ││├┤ ││││├┤ "
echo "  ╩╚═└─┘┴ ┴─┴┘└  ┴┘└┘└─┘  Setup Wizard"
echo ""

# ── 1. Prerequisites ──────────────────────────────────────────────────────────
info "Checking prerequisites..."
command -v docker      >/dev/null 2>&1 || error "Docker not found. Install: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || error "Docker Compose plugin not found."
command -v python3     >/dev/null 2>&1 || error "python3 required for key generation."
success "All prerequisites met."
echo ""

# ── 2. Prompts ────────────────────────────────────────────────────────────────
echo -e "${BLUE}── Database ──────────────────────────────────────${NC}"

read -rp "  DB name     [readfine]: " DB_NAME;     DB_NAME="${DB_NAME:-readfine}"
read -rp "  DB user     [readfine]: " DB_USER;     DB_USER="${DB_USER:-readfine}"
read -rsp "  DB password [random]:   " DB_PASSWORD; echo ""
if [[ -z "$DB_PASSWORD" ]]; then
    DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(18))")
    info "Generated DB password: ${YELLOW}${DB_PASSWORD}${NC}"
else
    read -rsp "  Confirm DB password:    " DB_PASSWORD2; echo ""
    while [[ "$DB_PASSWORD" != "$DB_PASSWORD2" ]]; do
        warn "Passwords do not match. Try again."
        read -rsp "  DB password [random]:   " DB_PASSWORD; echo ""
        if [[ -z "$DB_PASSWORD" ]]; then
            DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(18))")
            info "Generated DB password: ${YELLOW}${DB_PASSWORD}${NC}"
            break
        fi
        read -rsp "  Confirm DB password:    " DB_PASSWORD2; echo ""
    done
fi

echo ""
echo -e "${BLUE}── Admin account ─────────────────────────────────${NC}"

read -rp "  Admin email: " ADMIN_EMAIL
while [[ -z "$ADMIN_EMAIL" ]]; do
    warn "Email cannot be empty."; read -rp "  Admin email: " ADMIN_EMAIL
done

while true; do
    read -rsp "  Admin password: " ADMIN_PASSWORD; echo ""
    if [[ -z "$ADMIN_PASSWORD" ]]; then
        warn "Password cannot be empty."
        continue
    fi
    read -rsp "  Confirm password: " ADMIN_PASSWORD2; echo ""
    if [[ "$ADMIN_PASSWORD" == "$ADMIN_PASSWORD2" ]]; then
        break
    fi
    warn "Passwords do not match. Try again."
done

echo ""
echo -e "${BLUE}── Application ───────────────────────────────────${NC}"

read -rp "  Domain or server IP: " DOMAIN
while [[ -z "$DOMAIN" ]]; do
    warn "Cannot be empty."; read -rp "  Domain or server IP: " DOMAIN
done

# Detect IP vs domain
if [[ "$DOMAIN" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    BASE_URL="http://${DOMAIN}"
    ALLOWED_HOSTS="[\"${DOMAIN}\", \"localhost\"]"
    IS_DOMAIN=false
else
    BASE_URL="https://${DOMAIN}"
    ALLOWED_HOSTS="[\"${DOMAIN}\", \"www.${DOMAIN}\"]"
    IS_DOMAIN=true
fi

echo ""

# ── 3. Check SSL certificate ──────────────────────────────────────────────────
if [[ "$IS_DOMAIN" == true ]]; then
    if [[ ! -f "certs/cert.pem" || ! -f "certs/cert.key" ]]; then
        echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${YELLOW}  SSL certificate not found.${NC}"
        echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
        echo "  Place your SSL certificate files on this server:"
        echo ""
        echo "    mkdir -p certs"
        echo "    nano certs/cert.pem   # certificate (+ intermediates if any)"
        echo "    nano certs/cert.key   # private key"
        echo ""
        echo "  Then run setup.sh again."
        echo ""
        exit 1
    fi
    success "SSL certificate found."
fi

# ── 4. Generate secrets ───────────────────────────────────────────────────────
info "Generating secret keys..."
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
ENCRYPTION_KEY=$(python3 -c "import secrets; print(secrets.token_hex(16))")
success "Keys generated."

# ── 5. Write .env ─────────────────────────────────────────────────────────────
info "Writing .env..."

# URL-encode credentials (handles special chars like @, :, /, ?)
DB_USER_URL=$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$DB_USER")
DB_PASSWORD_URL=$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1], safe=''))" "$DB_PASSWORD")

umask 077
cat > .env <<EOF
# ── Database ───────────────────────────────────────────────────────────────────
DB_NAME=${DB_NAME}
DB_USER=${DB_USER}
DB_PASSWORD=${DB_PASSWORD}
DATABASE_URL=postgresql+asyncpg://${DB_USER_URL}:${DB_PASSWORD_URL}@db:5432/${DB_NAME}

# ── Security ───────────────────────────────────────────────────────────────────
SECRET_KEY=${SECRET_KEY}
ENCRYPTION_KEY=${ENCRYPTION_KEY}

# ── Application ────────────────────────────────────────────────────────────────
DEBUG=false
BASE_URL=${BASE_URL}
ALLOWED_HOSTS=${ALLOWED_HOSTS}

# ── First admin (removed automatically by setup.sh after startup) ─────────────
FIRST_ADMIN_EMAIL=${ADMIN_EMAIL}
FIRST_ADMIN_PASSWORD=${ADMIN_PASSWORD}
EOF

chmod 600 .env
success ".env written (permissions: 600)."

# ── 6. Write nginx.conf ───────────────────────────────────────────────────────
info "Writing nginx.conf..."

if [[ "$IS_DOMAIN" == true ]]; then
    cat > nginx.conf <<NGINX
server {
    listen 80;
    server_name ${DOMAIN};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name ${DOMAIN};

    ssl_certificate     /etc/nginx/certs/cert.pem;
    ssl_certificate_key /etc/nginx/certs/cert.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    location / {
        proxy_pass         http://app:8000;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
        client_max_body_size 10M;
    }
}
NGINX
    success "nginx.conf written (HTTPS)."
else
    cat > nginx.conf <<NGINX
server {
    listen 80;
    server_name ${DOMAIN};

    location / {
        proxy_pass         http://app:8000;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
        client_max_body_size 10M;
    }
}
NGINX
    success "nginx.conf written (HTTP)."
fi

# ── 7. Build & start ──────────────────────────────────────────────────────────
info "Building and starting containers (this may take a few minutes)..."
info "(migrations run automatically on first startup)"
docker compose up -d --build
success "Containers started."

# ── 8. Wait for app, then remove first-admin credentials ─────────────────────
info "Waiting for app to be ready..."
READY=false
for i in $(seq 1 30); do
    if curl -sf "http://localhost" >/dev/null 2>&1 || curl -sfk "https://localhost" >/dev/null 2>&1; then
        READY=true
        break
    fi
    sleep 3
done

if [[ "$READY" == true ]]; then
    info "Removing first-admin credentials from .env..."
    sed -i '/^# ── First admin/d; /^FIRST_ADMIN_/d' .env
    success "Credentials removed."
else
    warn "App did not respond within 90s. Credentials NOT removed from .env."
    warn "Check logs: docker compose logs app"
    warn "Remove manually when ready: sed -i '/^FIRST_ADMIN_/d' .env"
fi

# ── 9. Done ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Readfine is running!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  URL:         ${BLUE}${BASE_URL}${NC}"
echo -e "  Admin email: ${BLUE}${ADMIN_EMAIL}${NC}"
echo ""

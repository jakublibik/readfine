#!/usr/bin/env bash
# Helper for obtaining a Let's Encrypt certificate via certbot.
# Use this if you don't have a certificate yet.
#
# Usage: sudo bash ssl.sh <domain> <email>
# Example: sudo bash ssl.sh readfine.app admin@readfine.app
#
# After running this script, place the generated files:
#   cp /etc/letsencrypt/live/<domain>/fullchain.pem certs/cert.pem
#   cp /etc/letsencrypt/live/<domain>/privkey.pem   certs/cert.key
# Then run setup.sh.
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[ OK ]${NC}  $*"; }
error()   { echo -e "${RED}[ ERR]${NC}  $*"; exit 1; }

DOMAIN="${1:-}"; EMAIL="${2:-}"
[[ -z "$DOMAIN" || -z "$EMAIL" ]] && error "Usage: sudo bash ssl.sh <domain> <email>"
[[ "$EUID" -ne 0 ]] && error "Run with sudo."

# Preflight: check distro
if ! command -v apt-get >/dev/null 2>&1; then
    error "This script requires a Debian/Ubuntu system (apt-get not found)."
fi

# Preflight: check port 80 is free
if ss -tlnp 2>/dev/null | grep -q ':80 ' || netstat -tlnp 2>/dev/null | grep -q ':80 '; then
    error "Port 80 is already in use. Stop the process using it before running certbot."
fi

info "Installing certbot..."
apt-get update -q && apt-get install -y certbot

info "Obtaining certificate for ${DOMAIN}..."
certbot certonly \
    --standalone \
    --domain "${DOMAIN}" \
    --email "${EMAIL}" \
    --non-interactive \
    --agree-tos \
    --no-eff-email

success "Certificate obtained."
echo ""
echo "  Now copy the certificate files and run setup.sh:"
echo ""
echo "    mkdir -p certs"
echo "    cp /etc/letsencrypt/live/${DOMAIN}/fullchain.pem certs/cert.pem"
echo "    cp /etc/letsencrypt/live/${DOMAIN}/privkey.pem   certs/cert.key"
echo "    bash setup.sh"
echo ""
echo "  Auto-renewal: certbot installs a systemd timer automatically."
echo "  After renewal, restart nginx: docker compose restart nginx"
echo ""

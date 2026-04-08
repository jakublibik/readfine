# Readfine

Self-hosted RSS reader. Supports folders, labels, filters, readable extraction, and AI summaries.

## Requirements

- Docker + Docker Compose plugin
- A server with ports 80 and 443 open (or just 80 for IP-only installs)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/your-username/readfine.git
cd readfine
```

### 2. Prepare an SSL certificate (domain installs only)

You need a certificate before running setup. Two options:

**Option A — via your DNS/CDN provider** (e.g. Cloudflare Origin Certificate):
```bash
mkdir -p certs
nano certs/cert.pem   # paste the certificate
nano certs/cert.key   # paste the private key
```

**Option B — Let's Encrypt** (certbot must be able to bind port 80):
```bash
sudo bash ssl.sh your-domain.com admin@your-domain.com
mkdir -p certs
cp /etc/letsencrypt/live/your-domain.com/fullchain.pem certs/cert.pem
cp /etc/letsencrypt/live/your-domain.com/privkey.pem   certs/cert.key
```

> Skip this step entirely if you're installing on an IP address (HTTP only).

### 3. Run setup

```bash
bash setup.sh
```

The wizard will ask for:
- Database name, user, and password
- Admin email and password
- Domain or server IP

It will then generate the configuration, build and start all containers, and run database migrations automatically.

### 4. Log in

Open your browser at the URL shown at the end of setup and log in with the admin credentials you provided.

---

## Updating

```bash
git pull
docker compose up -d --build
```

Migrations run automatically on startup.

## Useful commands

```bash
# View logs
docker compose logs -f

# Restart the app
docker compose restart app

# Stop everything
docker compose down

# Stop and delete all data (irreversible)
docker compose down -v
```

## Stack

- **Backend:** Python 3.12 + FastAPI
- **Database:** PostgreSQL 16
- **Frontend:** HTMX + Jinja2 + Tailwind CSS
- **Task queue:** APScheduler
- **Proxy:** nginx

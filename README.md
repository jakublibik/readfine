# Readfine

Self-hosted RSS reader. Supports folders, labels, filters, readable extraction, web scraping, and AI summaries.

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

## Client IP setting (login lockout)

The login lockout keys on the visitor's IP. The bundled `docker-compose.yml` always runs
**nginx in front of the app**, so a Docker deployment always has at least one proxy. Tell
the app how many proxies sit in front so it can find the real visitor IP — otherwise an
attacker could spoof headers and dodge the lockout.

**Pick one line and set it in `.env`:**

| How you run it | `.env` |
|---|---|
| Docker **+ Cloudflare** in front | `TRUST_CLOUDFLARE=true` |
| Docker, **no Cloudflare** | `TRUSTED_PROXY_COUNT=1` |
| App started manually without nginx (local dev) | leave default `TRUSTED_PROXY_COUNT=0` |

That's all most deployments need. The two settings are explained below.

### If you use Cloudflare: lock the origin down

`TRUST_CLOUDFLARE=true` trusts Cloudflare's `CF-Connecting-IP` header. That's only safe if
your server **cannot** be reached except through Cloudflare — otherwise someone could hit
it directly with a forged header. Restrict inbound 80/443 to Cloudflare's IP ranges, e.g.
with UFW:

```bash
for cidr in $(curl -s https://www.cloudflare.com/ips-v4) $(curl -s https://www.cloudflare.com/ips-v6); do
  sudo ufw allow from "$cidr" to any port 80,443 proto tcp
done
sudo ufw deny 80/tcp
sudo ufw deny 443/tcp
```

### How `TRUSTED_PROXY_COUNT` works

`TRUSTED_PROXY_COUNT=N` reads the visitor IP from the `X-Forwarded-For` header counting `N`
entries from the **right** — those are the hops your own proxies added. Anything further
left is supplied by the client and ignored, so it can't be spoofed. With the bundled nginx
that's `1`; add `1` more for each extra proxy you put in front.

> **Optional (nicer logs, not required):** to make the visitor IP correct in nginx's own
> access logs behind Cloudflare, add the real_ip module to your nginx config (replace
> `<CF range>` with each range from https://www.cloudflare.com/ips/). The login lockout
> works without this.
>
> ```nginx
> set_real_ip_from <CF range>;   # repeat for every Cloudflare range
> real_ip_header CF-Connecting-IP;
> ```

---

## Updating

```bash
git pull
docker compose up -d --build
```

Migrations run automatically on startup. Updates never re-run `setup.sh`; the `ENCRYPTION_KEY`
in `.env` must stay stable for the life of the install — changing it makes all stored API keys
and feed passwords permanently unreadable.

> **Run a single worker.** Rate limiting and the login brute-force lockout are
> kept in process memory, so they only work correctly with **one** Uvicorn worker
> (the shipped `docker-compose.yml` does this). Adding workers silently splits the
> counters per worker and weakens those protections. Horizontal scaling would need
> a shared (DB/Redis) backend for the lockout — not yet implemented.

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

## Development

### Requirements

- Node.js 18+ (for building Tailwind CSS)
- Python 3.12 + uv

### Setup

```bash
npm install
```

### CSS build

Tailwind CSS is compiled from templates into a static file — `backend/app/static/css/tailwind.css`. This file is committed to the repository.

**Rebuild after changing Tailwind classes in any template:**

```bash
npm run build
```

**Watch mode (auto-rebuild on every template save):**

```bash
npm run dev
```

Run this in a second terminal alongside `uvicorn` during development.

### Hybrid development (Postgres in Docker, app run locally)

The recommended dev setup: run only PostgreSQL in Docker and run FastAPI locally
via uv, so the app reloads instantly on code changes.

```bash
# 1. Create your local env file (DATABASE_URL already points at localhost:5432)
cp .env.example .env

# 2. Start only the database
docker compose up -d db

# 3. Install backend deps (includes the dev group) and apply migrations
cd backend
uv sync
uv run alembic upgrade head

# 4. Run the app with auto-reload
uv run uvicorn app.main:app --reload
```

The app is then at http://localhost:8000. Build the CSS once (`npm run build`) or
run `npm run dev` in watch mode alongside.

### Running tests

```bash
cd backend
uv run pytest
```

Most tests use mocks and need no database. The integration tests (retention,
catch-up) require the `db` container to be running — start it with
`docker compose up -d db` first. Without a reachable database they are skipped
locally (and fail in CI, where Postgres is always provisioned).

### Running the app in Docker

`docker-compose.yml` is a production setup: the `app` container runs from the
code baked into the image at build time, so updates require a rebuild
(`docker compose up -d --build`). There is intentionally no source bind mount.

If you prefer to develop with the app inside Docker and live-reload edits
without rebuilding, add a `docker-compose.override.yml` (gitignored, auto-loaded
locally) with a bind mount — do **not** commit it, so production keeps building
immutable images:

```yaml
services:
  app:
    volumes:
      - ./backend:/app
```

### Updating HTMX

HTMX is self-hosted at `backend/app/static/js/htmx.min.js` (currently v2.0.4). To upgrade:

```bash
curl -o backend/app/static/js/htmx.min.js https://unpkg.com/htmx.org@<version>/dist/htmx.min.js
```

### Git hook (optional but recommended)

Automatically rebuilds CSS before every commit:

```bash
cp hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

---

## Stack

- **Backend:** Python 3.12 + FastAPI
- **Database:** PostgreSQL 16
- **Frontend:** HTMX + Jinja2 + Tailwind CSS
- **Task queue:** APScheduler
- **Proxy:** nginx

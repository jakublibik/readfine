# Staging

A throwaway parallel instance for verifying that an update **builds, migrates,
and runs on the server** before you promote the same commit to production. It
catches "works on my dev machine, breaks on the server" issues: image build
differences, migration ordering, strict (`DEBUG=false`) config, missing system
libraries. All without touching production data.

It runs as its own Docker Compose project (`readfine-staging`) with its **own
database volume**, fully isolated from the production stack on the same host.

## One-time setup

Use a **separate clone** so staging's git checkout never disturbs production:

```bash
git clone https://github.com/jakublibik/readfine.git ~/readfine-staging
cd ~/readfine-staging
git checkout dev

cp .env.staging.example .env.staging
# Generate FRESH keys (do not reuse production's):
#   python3 -c "import secrets; print(secrets.token_hex(32))"   # SECRET_KEY
#   python3 -c "import secrets; print(secrets.token_hex(16))"   # ENCRYPTION_KEY
nano .env.staging
```

Set `ALLOWED_HOSTS` to your staging hostname and the proxy settings
(`TRUSTED_PROXY_COUNT` / `TRUST_CLOUDFLARE`) to match how you expose it.

## Exposing it

The staging stack ships **no reverse proxy**: the app is published on
`127.0.0.1:8001` only, so it is never public by accident. Put it behind your
existing edge however you prefer, and **gate it** (it is not meant to be open):

- **Reverse-proxy subdomain:** add a server block to your existing proxy that
  forwards your staging hostname to the app. From a containerised nginx, reach
  the host with `host.docker.internal` (add
  `extra_hosts: ["host.docker.internal:host-gateway"]` to that nginx service) and
  `proxy_pass http://host.docker.internal:8001`. In that case the app must NOT be
  bound to loopback only, or the proxy container can't reach it. Set
  `STAGING_BIND=0.0.0.0` in `.env.staging` and keep the host/network firewall
  restricting inbound to 80/443 (+22) so 8001 stays private.
- **Tunnel** (Cloudflare Tunnel, Tailscale, …) pointed at `localhost:8001`.
- **SSH tunnel** for ad-hoc checks:
  `ssh -L 8001:localhost:8001 user@server`, then open `http://localhost:8001`.

Protect it with your SSO / access control / basic auth. With `DEBUG=false` the
app enforces the same security as production, so the proxy chain
(`TRUSTED_PROXY_COUNT` / `TRUST_CLOUDFLARE`) must match your real setup or the
login lockout will read the wrong client IP.

## Workflow

```bash
cd ~/readfine-staging
./deploy-staging.sh dev      # pull dev, rebuild, migrate, tail logs
```

Check the app on your staging URL. If it builds, migrates, and behaves, deploy
the **same commit** to production the usual way.

## Reset / teardown

```bash
# stop (keep the staging DB):
docker compose -f docker-compose.staging.yml --env-file .env.staging down
# stop and wipe the staging DB volume:
docker compose -f docker-compose.staging.yml --env-file .env.staging down -v
```

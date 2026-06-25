#!/usr/bin/env bash
# Update the Readfine STAGING instance: pull a branch, rebuild, run migrations,
# and tail the app log so you can confirm the update applied cleanly before
# promoting the same commit to production.
#
# Run from a dedicated staging clone (e.g. ~/readfine-staging) — NOT the
# production checkout, since this checks out and pulls a branch.
#
#   ./deploy-staging.sh [branch]   # branch defaults to: dev
set -euo pipefail

BRANCH="${1:-dev}"
COMPOSE=(docker compose -f docker-compose.staging.yml --env-file .env.staging)

[[ -f docker-compose.staging.yml ]] || { echo "Run this from the staging clone (docker-compose.staging.yml not found)." >&2; exit 1; }
[[ -f .env.staging ]]              || { echo "Missing .env.staging — copy .env.staging.example and fill it in." >&2; exit 1; }

echo "==> Updating to origin/$BRANCH"
git fetch origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "==> Building + starting staging (migrations run on startup)"
"${COMPOSE[@]}" up -d --build

echo "==> Recent app logs:"
"${COMPOSE[@]}" logs --tail=40 app
echo
echo "Done. Verify your staging URL, then deploy the same commit to production."

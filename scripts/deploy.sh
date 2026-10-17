#!/usr/bin/env bash
# Update the Readfine PRODUCTION instance: pull master, refresh the live landing
# page from the tracked example, rebuild + start the stack, prune old build cache,
# and follow the logs.
#
# Run from the production checkout, or from a symlink to this file (e.g. ~/deploy.sh):
#
#   ./deploy.sh
#
# The body is wrapped in main() because `git pull` can rewrite this very file while
# it runs. Bash reads a script as it goes, so without the wrapper it would continue
# from the same byte offset in the new version. A function is parsed whole first.
set -euo pipefail

main() {
    local repo_dir
    repo_dir="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
    cd "$repo_dir"

    [[ -f docker-compose.yml ]] || { echo "No docker-compose.yml in $repo_dir." >&2; exit 1; }

    echo "==> Pulling latest from master..."
    git checkout master
    git pull --ff-only origin master

    echo "==> Refreshing live landing page from example..."
    cp backend/app/templates/landing.example.html backend/app/templates/landing.html

    echo "==> Building and starting containers..."
    docker compose up -d --build

    # Every rebuild leaves cache layers behind and nothing removes them; on the
    # production VPS they had grown to 17 GB. A week of cache keeps rebuilds fast.
    echo "==> Pruning build cache older than a week..."
    docker builder prune -f --filter until=168h

    echo "==> Done."
    docker compose ps

    echo "==> Tailing logs (Ctrl+C to stop, containers keep running)..."
    docker compose logs -f --tail=50
}

main "$@"
exit

# Releasing Readfine

How versions work and what to check before cutting a release.

## Versioning (SemVer)

Versions are `MAJOR.MINOR.PATCH`, each an **independent integer** — after `0.9.0`
comes `0.10.0` (not `0.91`), and `0.99.0` is followed by `0.100.0`.

While the project is `0.x` (API/schema not yet stable):

| Bump  | Example         | When                                                                                          |
| ----- | --------------- | --------------------------------------------------------------------------------------------- |
| PATCH | `0.9.0 → 0.9.1` | Backwards-compatible bug fixes only. No new features, no schema/config change requiring action. |
| MINOR | `0.9.0 → 0.10.0`| New features **and/or** breaking changes (DB migrations, env/config changes). Normal pre-1.0 cadence. |
| MAJOR | `→ 1.0.0`       | First stability commitment.                                                                   |

## Deploy vs. release

`master` is the **production trunk**, not a release branch. Two distinct events:

- **Deploy** = merge `dev → master` and pull on the server. Master is continuously
  deployable and normally sits ahead of the last tag by several commits. You do **not**
  bump the version or cut a changelog section to deploy — a hotfix or a finished chunk
  can ship straight away. Pushing to master without a tag is expected, not a mistake.
- **Release** = a tag + dated `CHANGELOG` section cut over commits that are *already* on
  master, when a larger chunk is done and you want a labelled milestone. Multiple deploys
  batch into one versioned release.

**Changelog at both events.** Before a **deploy** (merge to master), confirm every
notable change in the batch is already in `CHANGELOG.md` `[Unreleased]` (see the
per-commit rule in `CLAUDE.local.md`) — deploying is the last chance to catch a missed
entry while the context is fresh. At **release**, that `[Unreleased]` list is what you
move into the dated section, so keeping it current as you go makes the release a rename,
not an archaeology dig.

**Gate: before any merge to `master`, verify on staging first** (`staging.readfine.app`,
`./deploy-staging.sh dev`). Staging tracks `dev`, so it exercises exactly what you're about
to merge.

`git describe --tags` tells you how far master is ahead of the last release at any time.

## When to cut a release

You do **not** bump on every merge to `master`. Bump when you cut a release (tag) over
what's already deployed. To pick the bump, read `CHANGELOG.md` `[Unreleased]`: any
migration / breaking change / new feature → minor; fixes only → patch.

## Pre-release checklist

**Version & changelog**

- [ ] Decide the bump (patch vs minor) from the `[Unreleased]` contents
- [ ] Bump the version in `backend/pyproject.toml` **and** `package.json` (keep in sync)
- [ ] `CHANGELOG.md`: move `[Unreleased]` notes into a new dated `[X.Y.Z]` section

**Code & assets**

- [ ] Full test suite green: `cd backend && uv run pytest`
- [ ] If templates/classes changed: rebuild CSS (`npm run build`) and commit the result
- [ ] Alembic: single head and a clean upgrade on a fresh DB (`uv run alembic upgrade head`)

**User-facing docs** — only touch when the release actually changes them; screenshots and
landing copy are version-independent, so leave them unless the UI/feature set moved.

- [ ] `README.md` — features list / screenshots
- [ ] `backend/app/templates/landing.example.html` — features / screenshots / copy
- [ ] `backend/app/templates/help.html` (`/help`) — new features, FAQ entries
- [ ] OpenAPI / API docs — if the API surface changed

**Ship** — the feature code is usually already deployed on master; a release adds the
version label on top.

- [ ] Commit the bump + changelog on `dev`, verify on staging, then merge `dev → master`
- [ ] Deploy to production (pull master) if the bump commit or any batched change isn't
      live yet; purge stale CDN cache if needed
- [ ] Tag the release: `git tag vX.Y.Z && git push --tags`
- [ ] Create a GitHub release from the tag (paste the `CHANGELOG.md` section)

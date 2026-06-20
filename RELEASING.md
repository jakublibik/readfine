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

`master` is production. You do **not** bump on every merge to `dev` — you bump when you
cut a release (merge `dev → master`). Multiple `dev` merges batch into one versioned
release. To pick the bump, read `CHANGELOG.md` `[Unreleased]`: any migration / breaking
change / new feature → minor; fixes only → patch.

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

**Ship**

- [ ] Commit on `dev`, then merge `dev → master`
- [ ] Tag the release: `git tag vX.Y.Z && git push --tags`
- [ ] Create a GitHub release from the tag (paste the `CHANGELOG.md` section)
- [ ] Deploy to production and purge any stale CDN cache if needed

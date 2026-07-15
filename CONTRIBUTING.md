# Contributing to Readfine

Thanks for your interest in Readfine! This is a small, single-maintainer
self-hosted project.

> **Current status:** **Bug reports and feature suggestions are very welcome**;
> please open an issue. **Code pull requests are not actively sought yet:** the
> project is young and I'm still settling its direction, so I can't promise PRs
> will be reviewed or merged. If you'd like to contribute code, **open an issue
> first** to check whether it fits before investing time. This policy may relax
> as the project matures.

## Before you start

- **Bugs / features:** open an issue, the best way to contribute right now.
- **Security issues:** do **not** open a public issue. See [SECURITY.md](SECURITY.md).
- **License:** by contributing you agree your work is licensed under the
  project's [AGPL-3.0](LICENSE).

## Development setup

See the [Development](README.md#development) section of the README for the full
setup (hybrid Postgres-in-Docker + local app, CSS build, etc.). In short:

```bash
cp .env.example .env          # DEBUG=true for local dev
docker compose up -d db
cd backend && uv sync && uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

## Branches

- `dev`: development (open PRs against this branch)
- `master`: production/release (merged only on release)

## Conventions

- **Language:** code, comments, commits, and docs are in English.
- **CSS:** after changing Tailwind classes in templates, rebuild with
  `npm run build` (the compiled `tailwind.css` is committed). The optional
  pre-commit hook in `hooks/` does this automatically.
- **CSP:** inline `<script>` needs `nonce="{{ request.state.csp_nonce }}"`;
  inline event handlers belong in external JS.
- **Tests:** add tests for anything irreversible, security-critical, or with
  non-trivial business logic (auth, fetcher, filters, AI pipeline, purge,
  crypto, SSRF). Simple CRUD routes and templates don't need tests.

## Running tests

```bash
cd backend
uv run pytest
```

Most tests use mocks and need no database; integration tests (retention,
catch-up) need the `db` container running.

## Pull requests

If we agreed on a change in an issue (see the status note above), these keep it
reviewable:

- Keep PRs focused: one logical change per PR.
- Make sure `pytest` passes and the CSS is rebuilt if you touched templates.
- Describe what changed and why; link the related issue.

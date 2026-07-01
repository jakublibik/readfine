# Project: Readfine

Self-hosted web RSS reader (inspired by Tiny Tiny RSS) with feed scraping, filters,
article scoring, and AI summaries, scoring and briefings.

## Tech stack
- Backend: Python 3.12 + FastAPI
- Database: PostgreSQL
- Task queue: APScheduler (in-process, inside the FastAPI process)
- Frontend: HTMX + Jinja2 + Tailwind CSS
- Readable extraction: trafilatura → readability-lxml (fallback)
- AI: Anthropic Claude / OpenAI / Google Gemini (configurable, bring-your-own-key)
- Auth: signed session cookies (web) + JWT (API)
- Deployment: Docker
- Package manager: uv

## Technical decisions
- **Dev environment**: hybrid — PostgreSQL in Docker, FastAPI run locally via uv
- **CSRF**: the web is protected by `starlette_csrf.CSRFMiddleware` (double-submit
  cookie + `x-csrftoken` header; HTMX requests attach it via `csrf.js`). The API is
  exempt (it authenticates with a JWT in the `Authorization` header), as are the auth
  forms (`/login`, `/register`, `/logout`, `/reset-password`, `/resend-verification`)
- **Git workflow**: `dev` = active development, `master` = production trunk and the
  GitHub default branch. New work branches off `dev`; deploying to production means
  merging `dev → master` and pulling on the server — master is continuously deployable
  and may sit ahead of the last release. A **version release is a separate event**: a
  tag + `CHANGELOG` section cut over commits already on master (see `RELEASING.md`), not
  a prerequisite for deploying. **Before any merge to `master`, verify on staging first.**

## Status
Released and on the post-release `0.x` line — first public release was v0.9.0 (2026-06-20);
for the current version and release notes see `CHANGELOG.md` (and `RELEASING.md` for the
process). Implemented: RSS/Atom and
web-scraping feeds, folders, scheduled fetching, readable extraction, 3-panel reading UI,
article states, labels, filters (conditions → actions, regex, AND/OR, feed/folder scoping),
per-user settings, admin panel, SMTP, API tokens (JWT), tiered retention/purge, dark mode,
OPML import/export (incl. TT-RSS compatibility), AI summaries, relevance scoring, chat over
articles, and Catch me up & briefings. Self-hosted via Docker; hosted instance at
readfine.app.

## Testing
- **Test**: auth flows (login, registration, verification, password reset), account deletion,
  irreversible/destructive data operations, business-logic services (fetcher, filters, AI
  pipeline, briefing, scoring, purge), security-critical paths (crypto, rate limiting,
  URL/SSRF validation)
- **Don't test**: CRUD routes (name/email/password/settings changes), admin UI, Jinja2
  templates, simple static routes — low risk, reversible or trivial
- A new feature gets a test if it is irreversible, security-critical, or contains non-trivial
  business logic

## CSS / Tailwind conventions
- When fixing layout bugs, find the root cause (e.g. a flex/truncate parent) rather than
  patching symptoms
- After changing classes in templates, rebuild the stylesheet: `npm run build`
- CSP is nonce-based: an inline `<script>` needs `nonce="{{ request.state.csp_nonce }}"`;
  inline event handlers belong in external JS

## Before large changes
- For non-trivial work (e.g. error handling, new features), propose at least two approaches
  with tradeoffs and wait for approval before implementing
- Don't assume behavior is a bug — verify the current behavior is actually wrong before
  "fixing" it

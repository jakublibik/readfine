# Changelog

All notable changes to Readfine are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, minor releases may include breaking changes (database
migrations, config changes); `1.0.0` will mark the first API/stability commitment.

## [Unreleased]

### Added

- In-app feedback / bug report: a "Send feedback" item in the user menu opens a
  form (type, subject, message) that emails all admins via the configured SMTP,
  with `Reply-To` set to the sender's account email. Off by default; admins enable
  it in Admin → Settings (requires SMTP).

## [0.9.0] - 2026-06-20

First public release. Self-hosted RSS reader with:

- RSS/Atom feeds and web-scraping feeds (CSS selectors), folders, scheduled fetching
- Readable extraction (trafilatura → readability-lxml fallback)
- 3-panel reading UI (HTMX + Tailwind), article states, labels, dark mode
- Filters (conditions → actions, regex, AND/OR, feed/folder scoping) with retroactive apply
- AI summaries, relevance scoring, chat over articles, and Catch me up & briefings
  (Anthropic / OpenAI / Gemini, bring-your-own-key)
- Per-user settings, admin panel, SMTP, API tokens (JWT), tiered retention/purge
- OPML import/export, including web-scraping feeds (round-trips via custom outline
  attributes) and Tiny Tiny RSS compatibility
- `/healthz` endpoint (lightweight DB ping, GET + HEAD) for uptime/monitoring probes
- `backup.sh` — off-site PostgreSQL backups via `pg_dump` + restic (encrypted,
  deduplicated, retention), with a Cloudflare R2 example config. See README → Backups.

Notes for self-hosters:

- **Registration is closed by default** on a fresh install — only the admin account
  exists; enable sign-ups in the admin panel to open the instance.
- Shell scripts are pinned to LF line endings (`.gitattributes`) so `setup.sh` runs
  correctly when the repo is cloned/unzipped on Windows.

### Release process

See [`RELEASING.md`](RELEASING.md) for versioning rules and the full pre-release checklist.

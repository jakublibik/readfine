# Changelog

All notable changes to Readfine are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, minor releases may include breaking changes (database
migrations, config changes); `1.0.0` will mark the first API/stability commitment.

## [Unreleased]

### Changed

- The fetcher now reads rate-limit response headers (`Retry-After`, `RateLimit-*`
  and `X-RateLimit-*`) on both successful and 429 responses and applies a per-host
  cooldown: once a host reports its budget is exhausted (e.g. Reddit's
  `x-ratelimit-remaining: 0`), other feeds on that host wait out the reset instead
  of bursting into repeated HTTP 429s. The wait happens within the fetch round (so a
  rate-limited host drains several feeds per 15-min round) up to a round budget that
  keeps the round short enough not to miss the next slot; anything past the budget
  defers to the next round. Feeds on other hosts are unaffected and still fetch in
  parallel.

## [0.11.0] - 2026-06-30

### Added

- Search is now also a filter view: alongside the text query you can scope to
  feeds/folders, filter by labels (any / specific) and read status (all / unread /
  read), and choose the sort (relevance / newest / oldest). Leaving the text empty
  applies the filters on their own. Search moved from the user menu to an icon in
  the sidebar.
- Feeds are now fetched conditionally: Readfine remembers each feed's `ETag` /
  `Last-Modified` and sends them back on the next poll, so an unchanged feed answers
  `304 Not Modified` with no body and the download and parse are skipped entirely.
  Less bandwidth, and lighter on rate-limited sites.
- Catch me up & briefings now have a dedicated label filter (any label / specific
  labels, OR) shown alongside the feed scope, replacing the old "Labeled only"
  relevance radio. The minimum-score filter is now an independent toggle (shown only
  when scoring is configured) rather than bundled with labels. The "Since yesterday"
  period is now labelled "Yesterday+".
- Adding a feed lets you set its fetch interval from the subscribe form, and owners
  of a private or solely-subscribed feed can change the interval when editing it.
  Shared public feeds show the interval read-only (only an admin can change it).
- Errored feeds now show when they will next be retried, both on the feed list and
  the feed detail page; a feed auto-disabled after repeated failures says so
  explicitly instead of leaving the next fetch ambiguous.

### Changed

- Switching between sections (Starred, Labeled, folders, feeds) now shows a brief
  loading overlay over the article list, so the sidebar highlight no longer appears
  to change before the list it points at has loaded.
- Creating, renaming, or deleting a folder immediately updates the folder dropdown in
  the add-feed form without a page reload.

### Fixed

- Feeds where every item points at one shared link (e.g. a podcast whose episodes all
  link to the show page) no longer have every new item after the first silently
  dropped as a duplicate; items are now de-duplicated by links that actually identify
  a single item, falling back to the unique GUID otherwise.
- Reddit (and similar) article content built from a header-less layout table no longer
  overflows the reading panel: such tables now stack the image above the text, images
  are constrained to the column width, and genuine data tables scroll horizontally
  instead of overflowing.
- Text search combined with a read-status filter no longer skips results while
  scrolling: mark-as-read-on-scroll is disabled for that specific case (where it
  shifted the offset-paginated result set), leaving plain search and the filter view
  unaffected.

- A feed returning HTTP 429 (Too Many Requests) is no longer disabled on the first
  hit. 429 and 408 are now treated as transient: the feed backs off via the normal
  error tier and is only disabled after the usual run of consecutive failures. When
  the server sends a `Retry-After` header, the scheduler waits at least that long
  before re-fetching.
- Adding a feed now costs a single network request instead of up to three. The
  "Test" step caches the fetched feed briefly and Subscribe reuses it for both the
  title and the initial article import, so rate-limited sites (e.g. Reddit) no
  longer return 429 mid-subscribe.
- When several feeds share a host (e.g. multiple Reddit subreddits), a scheduled
  fetch no longer requests them all at once. Requests to a given host are now
  serialized within a fetch round — different hosts still run in parallel — which
  flattens the burst that made some of those feeds return HTTP 429.
- Readable extraction that returns no usable content — e.g. a Reddit article page
  that serves a bot-verification wall (HTTP 200) instead of the article — is no
  longer saved as a blank "successful" extraction that rendered an empty body. Such
  articles now show their original feed content, and a feed whose pages keep
  extracting nothing auto-disables full-content extraction after repeated empties
  (the same way persistent HTTP 403 blocks already did) instead of re-fetching every
  page forever.
- The auto-disabled notice for full-content extraction now states why it was turned
  off — the feed already delivers full articles, or the site blocked extraction /
  returned no readable content — instead of always claiming the site blocked it.
- The article view no longer flickers an endless "Extracting full content…" spinner
  for an article whose extraction failed and is waiting to retry; it shows the feed
  content quietly, and the spinner appears only while a first attempt is in flight.
- "Extract full content" from the article menu no longer momentarily drops the
  article's star, archive, or label state from the action bar.

## [0.10.1] - 2026-06-27

### Added

- One-command local demo: `docker compose -f docker-compose.demo.yml up` brings the
  app up on `http://localhost:8000` with a seeded admin and no setup wizard, for
  trying it out before a full install. Demo only — plain HTTP, `DEBUG=true`, and
  hard-coded throwaway secrets; not for production. See README → Quick demo.

### Fixed

- Infinite scroll in unread/label views could stop early or silently skip articles
  when rows were marked read while scrolling (the unread set shrank under the
  numeric page offset). The article list now uses keyset (cursor) pagination, so
  scrolling reliably loads every remaining article regardless of mark-read-on-scroll.
- Mobile: the active tab in the horizontal side-nav strip now scrolls into view on
  load, instead of staying off-screen when the strip was left scrolled elsewhere.
- Docker: the `db` healthcheck now probes the actual database (`pg_isready -d`),
  so a `DB_USER` that differs from `DB_NAME` no longer logs a Postgres FATAL on
  every check.

## [0.10.0] - 2026-06-25

### Added

- In-app feedback / bug report: a "Send feedback" item in the user menu opens a
  form (type, subject, message) that emails all admins via the configured SMTP,
  with `Reply-To` set to the sender's account email. Off by default; admins enable
  it in Admin → Settings (requires SMTP).
- AI error badge: a red dot on the user menu and the Settings → AI nav item when a
  background AI call (e.g. scoring) last failed, so credit/quota errors are visible
  without opening Settings. Self-clears on the next successful AI call, or dismiss it
  manually via the × on the error panel in Settings → AI.
- Filter action **archive**: alongside label / mark-as-read / star, a filter can now
  archive matching articles (removes them from the inbox and exempts them from
  retention purge). Available in Settings → Filters and via OPML round-trip.

### Changed

- Stats: the single "Backlog" figure is split into **labeled backlog** (unread items
  carrying a label) and **starred backlog** (your read-later pile); both are now
  all-time rather than capped at 90 days. Reading streak, per-day reads and the most
  active hour are computed in your own timezone instead of UTC.
- OPML import: Tiny Tiny RSS filter scope (feed / category) is now matched by name and
  mapped to the corresponding Readfine feed/folder scope, instead of being dropped and
  imported as global. Mixed scoped/global filters still import as global with a warning.
- Admin → Settings: the SMTP test now shows the underlying error detail on failure,
  making misconfiguration easier to diagnose.

### Fixed

- Stats: corrected the engagement funnel bars.
- Mobile: the collapsible sidebar reliably reappears after a refresh instead of
  occasionally staying hidden.
- Favicon: app pages now declare the raster apple-touch-icon, so Firefox/Android use
  it for link previews and home-screen tiles instead of rasterizing the SVG.

### Security

- Migrated JWT handling from the unmaintained `python-jose` to `PyJWT`.

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

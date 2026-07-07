# Changelog

All notable changes to Readfine are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, minor releases may include breaking changes (database
migrations, config changes); `1.0.0` will mark the first API/stability commitment.

## [Unreleased]

### Added

- A "Copy" action on the article ··· menu (desktop) and the bottom action bar
  (mobile) copies the article — title, source and body — to the clipboard as both
  rich HTML and plain text, so it pastes with formatting and images into rich
  editors and as clean text everywhere else. Relative image/link URLs are made
  absolute so they still resolve once pasted.
- The Stats backlog cards (labeled and starred) now link straight into the reader's
  matching view, so you can jump from the count to the actual articles.
- Admin → Feeds gained a "Rate limits" view (shown when any exist) listing the fetch
  pace Readfine has learned for each host — host, spacing, how it was learned and when
  — each with a Clear action to reset it. Errored feeds in the admin table now also
  show their predicted next fetch, and a feed with no per-feed interval override shows
  the effective default interval instead of a blank dash.
- Admin → Feeds gained a "By host / A–Z" toggle that groups the feed list by fetch host
  (so all of a site's feeds — e.g. every Reddit feed — sit together under a host header
  with a count) instead of one flat alphabetical list. Groups sort alphabetically by host,
  single-feed hosts collapse into an "Other" bucket, and any host (or the Other bucket)
  containing an errored feed floats to the top so problems stay visible. Feeds within a
  group — and the flat A–Z list itself — now order by status first (errored, then disabled,
  then paused, then active) and then by name, so feeds needing attention surface in both
  views. The choice is remembered per browser and preserved across feed actions.

### Changed

- An HTTP 403 from a feed no longer disables it on the first hit. Reddit and
  YouTube return 403 as a transient anti-bot / rate-adjacent block (datacenter IP,
  generic user-agent) far more often than as a permanent denial, so 403 now backs
  off through the error tier and is disabled only after several consecutive
  failures — the same treatment as 408/429 and 5xx. Genuinely permanent 4xx (400,
  401, 404, 410) still disable immediately.
- The fetcher now reads rate-limit response headers (`Retry-After`, `RateLimit-*`
  and `X-RateLimit-*`) on both successful and 429 responses and applies a per-host
  cooldown: once a host reports its budget is exhausted (e.g. Reddit's
  `x-ratelimit-remaining: 0`), other feeds on that host wait out the reset instead
  of bursting into repeated HTTP 429s. The wait happens within the fetch round (so a
  rate-limited host drains several feeds per 15-min round) up to a round budget that
  keeps the round short enough not to miss the next slot; anything past the budget
  defers to the next round. Feeds on other hosts are unaffected and still fetch in
  parallel.
- Manually refreshing a feed (the sidebar ↻ and the admin "force fetch") now respects
  a known rate-limit window instead of firing another request straight into an HTTP
  429: when the host is still cooling down it shows "Rate-limited — try again in …"
  (seconds or minutes, read from the server's reset headers). A bare HTTP 403 anti-bot
  block is treated differently — only the background scheduler paces itself on those,
  while an explicit manual refresh is still allowed to retry, since such blocks are
  often transient.
- Readfine now learns a sustainable fetch pace for each host and spaces its requests
  accordingly, rather than only backing off after a host reports its budget already
  exhausted. It reads the pace precisely from a host's rate-limit headers on successful
  responses, and tightens it further when a host still answers with repeated HTTP 429s;
  the learned pace only ever tightens (so it doesn't oscillate) and is capped so a feed
  can't stall indefinitely. This stops aggressive hosts such as Reddit — where a burst
  of same-host feeds fetched back-to-back would trip a 403/429 — from being throttled.
  The learned pace is stored and survives restarts and deploys (so a host isn't
  re-probed into a rate limit on every restart), and manual refreshes respect it too,
  showing "try again in …" instead of firing into a known gap.
- Feeds are now fetched at whichever of the four 15-min ticks (:00/:15/:30/:45)
  first follows their refresh interval, rather than being pinned to the top of the
  hour by interval. This spreads fetch load across the hour on each feed's own phase
  instead of piling every hourly feed onto :00, and improves freshness: an hourly
  feed fetched a few minutes past the hour used to wait until the next :00 (up to
  ~2 h between fetches) and now refreshes about an hour later as intended. Feeds that
  miss a scheduled fetch — deferred by a host cooldown, a transient error, or an app
  restart mid-round — likewise recover at the next tick instead of waiting a full
  interval, and a rate-limited host (e.g. Reddit) drains across all four ticks per
  hour rather than only at the top of the hour.
- The per-feed refresh button (↻ in the sidebar) now reloads the article list when
  you are viewing that feed, so newly fetched articles appear right away instead of
  only after re-selecting the feed. Works across the 3-panel, 2-panel and mobile
  layouts; refreshing a feed you are not currently viewing still just updates its
  unread badge.
- The fetch-interval selector now spells out the server default next to the
  "Default" option (e.g. "Default (60 min)") on the subscribe, scrape-setup and
  feed-edit forms, and wraps more cleanly on narrow/mobile layouts.
- Adding a feed now shows specific messages for rate-limiting (HTTP 429 — including
  when to retry, read from `Retry-After` / `X-RateLimit-Reset`) and temporary server
  errors (5xx), instead of a bare "HTTP error {status}".
- The Feeds, Filters and Labels settings pages and the admin Users page now show the
  item count next to the page heading (matching the admin Feeds page), kept up to
  date as items are added or removed without a page reload.
- Labels and filters now sort case-insensitively — in the settings lists, label
  pickers and label chips alike. Previously the database collation ordered all
  uppercase names before any lowercase one, so a new lowercase-named label or
  filter appeared stuck at the end of the list instead of in its alphabetical
  place.
- The filter list shows a "priority N" badge on filters whose priority differs
  from the default, so it is visible why a filter sorts (and runs) ahead of the
  alphabetical order.
- Briefings sent to extra recipients now address the account owner in the visible
  `To:` and put the additional recipients in `Bcc`, so co-subscribers can no longer
  see each other's email addresses. The modal also notes that delivery can lag the
  scheduled time by up to 15 min (the scheduler tick interval).
- The admin "force fetch" button now shows a spinner and blocks double-clicks while
  the synchronous fetch runs, instead of appearing to do nothing for several seconds.
- The Settings → AI cost estimates now cover the current Anthropic, OpenAI and Google
  model families, and a configured model that isn't in the built-in price list is
  estimated from a typical model for its provider — shown with a "~" and a note under
  the table — instead of appearing as an unknown or zero cost.
- The Trend column in the AI cost table now tracks estimated cost rather than the raw
  number of operations, and the Fast/Quality/Total rows show a trend too (previously
  blank), so the arrows reflect what actually moves your spend — e.g. longer articles
  costing more even at the same number of runs.

### Fixed

- Collapsing or expanding the sidebar is now instant. It previously refetched the
  whole sidebar from the server on every toggle — so the old layout lingered
  (briefly squished into the new width) until the request came back. The collapsed
  rail and the full sidebar are now both rendered up front and the toggle just
  switches between them client-side, with no round-trip. The same applies to the
  mobile "collapsible" sidebar's open/close.
- Toast notifications (e.g. a feed's error message when you open it, or a manual
  refresh result) no longer render at roughly half-width on mobile. They now stretch
  edge-to-edge with a small gutter on narrow screens and stay centred with a sensible
  max-width on wider ones.
- Filters sharing the same priority now run in a deterministic order — exactly the
  order the Settings → Filters list shows (priority, then name). Previously the
  execution order of equal-priority filters was left to the database, so a
  "stop on match" filter could behave inconsistently between fetches.

- The green "Feed added successfully" banner no longer reappears when you refresh the
  Feeds settings page after subscribing.
- Articles carrying a label now stay visible in their label view even after their
  feed is deleted or unsubscribed. The label view used to inner-join the feed and so
  hide such articles, leaving the sidebar label badge showing a count for an
  apparently empty category.
- The article-list loading overlay now matches the neutral dark-mode background
  instead of a blue-tinted grey, and is delayed slightly so quick (cached) loads no
  longer flash a spinner.
- The AI cost table's total is no longer silently understated when a model slot uses a
  model missing from the price list: that slot used to be added to the total as $0,
  making the grand total look complete while omitting part of the cost.

### Security

- Filter `regex` conditions are now evaluated under a per-match timeout, closing a
  denial-of-service hole: the previous create-time heuristic could be bypassed by a
  catastrophic-backtracking pattern (e.g. `([a-z]+)*`), and because matching ran
  synchronously on the event loop during fetch, filter test and retroactive apply —
  and CPython's `re` neither times out nor releases the GIL — a single crafted filter
  could freeze the whole app for every user. Evaluation now uses the `regex` module
  with a hard timeout (a timed-out pattern is treated as "no match"); existing filter
  behaviour is unchanged.
- Authenticated HTML responses (full pages and HTMX partials) are now sent with
  `Cache-Control: no-store` and `Vary: Cookie`, so a shared browser can no longer
  show one user's rendered page to the next after an account switch — previously the
  back/forward cache (bfcache) could surface the prior user's content (CWE-525).
  Static assets stay cacheable.

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

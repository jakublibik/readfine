# Changelog

All notable changes to Readfine are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, minor releases may include breaking changes (database
migrations, config changes); `1.0.0` will mark the first API/stability commitment.

## [Unreleased]

### Added

- Preferences → "Advance after mark all as read" (off by default): after you mark a feed, folder or label read from the sidebar, Readfine selects and opens the next one that still has unread articles, expanding a collapsed folder if needed. Feeds advance across folder boundaries; empty scopes are skipped, and the special views (All articles, Starred, Archived) are left alone.

### Fixed

- Marking a feed or folder read from the sidebar now refreshes the whole sidebar, so counts that share those articles (labels, other feeds) update right away instead of going stale until the next reload.

## [0.12.0] - 2026-07-07

### Added

- **Copy article** action on the article ··· menu (desktop) and the bottom action bar (mobile). It copies the title, source and body to the clipboard as both rich HTML and plain text, so it pastes with formatting and images into rich editors and as clean text everywhere else. Relative image and link URLs are rewritten to absolute so they still resolve after pasting.
- The Stats backlog cards (labeled and starred) now link straight into the matching reader view, so you can jump from a count to the actual articles.
- Admin → Feeds: a "Rate limits" view (shown only when some exist) lists the fetch pace Readfine has learned per host, with the host, its spacing, and how and when that was learned, each with a Clear action. Errored feeds in the admin table also show their predicted next fetch, and a feed with no per-feed interval override shows the effective default instead of a blank dash.
- Admin → Feeds: a "By host / A-Z" toggle that groups the feed list by fetch host, so all of a site's feeds (say, every Reddit feed) sit under one host header with a count instead of a flat alphabetical list. Hosts sort alphabetically, single-feed hosts fall into an "Other" bucket, and any host holding an errored feed floats to the top so problems stay visible. Within a group, and in the flat list, feeds now sort by status first (errored, disabled, paused, active) and then by name. The choice is remembered per browser.

### Changed

- A single HTTP 403 no longer disables a feed. Reddit and YouTube return 403 as a transient anti-bot or rate-adjacent block (datacenter IP, generic user-agent) far more often than as a permanent denial, so 403 now backs off through the error tier like 408/429 and 5xx and only disables after several consecutive failures. Genuinely permanent 4xx (400, 401, 404, 410) still disable immediately.
- The fetcher reads rate-limit headers (`Retry-After`, `RateLimit-*`, `X-RateLimit-*`) on both successful and 429 responses and applies a per-host cooldown. Once a host reports its budget is spent (for example Reddit's `x-ratelimit-remaining: 0`), other feeds on that host wait out the reset instead of hammering it with more 429s. The waiting happens inside the fetch round, up to a budget that keeps the round short enough not to miss the next slot; anything over that defers to the next round. Feeds on other hosts still fetch in parallel.
- Manually refreshing a feed (the sidebar ↻ and the admin "force fetch") now respects a known rate-limit window instead of firing straight into another 429. While the host is cooling down it shows "Rate-limited, try again in …" (seconds or minutes, from the server's reset headers). A bare 403 anti-bot block is treated differently: only the background scheduler paces itself on those, since a manual retry often succeeds.
- Readfine learns a sustainable fetch pace per host and spaces its requests accordingly, rather than only reacting once a host reports its budget already spent. It reads the pace from a host's rate-limit headers on successful responses and tightens it when the host keeps answering 429. The learned pace only ever tightens, so it never oscillates, and is capped so a feed can't stall forever. This keeps aggressive hosts like Reddit, where a burst of same-host feeds fetched back to back would trip a 403/429, from being throttled. The pace is stored and survives restarts and deploys, so a host isn't re-probed into a rate limit on every restart, and manual refreshes respect it too.
- Feeds are fetched at whichever of the four 15-minute ticks (:00/:15/:30/:45) first follows their interval, instead of being pinned to the top of the hour. This spreads load across the hour on each feed's own phase rather than piling every hourly feed onto :00, and it improves freshness: an hourly feed first fetched a few minutes past the hour used to wait until the next :00 (up to ~2 h between fetches) and now refreshes about an hour later as intended. Feeds that miss a tick (a host cooldown, a transient error, a restart mid-round) recover at the next tick instead of waiting a full interval.
- The per-feed refresh button (↻) now reloads the article list when you're viewing that feed, so newly fetched articles appear right away instead of only after re-selecting it. Refreshing a feed you're not viewing still just updates its unread badge.
- The fetch-interval selector spells out the server default next to the "Default" option (for example "Default (60 min)") on the subscribe, scrape-setup and feed-edit forms, and wraps better on narrow screens.
- Adding a feed now shows specific messages for rate-limiting (429, including when to retry) and temporary server errors (5xx) instead of a bare "HTTP error {status}".
- The Feeds, Filters and Labels settings pages and the admin Users page show an item count next to the heading, kept current as items are added or removed without a reload.
- Labels and filters sort case-insensitively everywhere now: settings lists, label pickers and chips. Previously the database collation put all uppercase names before any lowercase one, so a new lowercase label or filter got stuck at the end of the list.
- The filter list shows a "priority N" badge on filters whose priority isn't the default, making it clear why a filter sorts and runs ahead of alphabetical order.
- Briefings sent to extra recipients now put the account owner in `To:` and the additional recipients in `Bcc`, so co-subscribers no longer see each other's addresses. The modal also notes that delivery can lag the scheduled time by up to 15 minutes (the scheduler tick).
- The admin "force fetch" button shows a spinner and blocks double-clicks while the synchronous fetch runs, instead of looking like it did nothing for several seconds.
- Settings → AI cost estimates now cover the current Anthropic, OpenAI and Google model families. A configured model that isn't in the built-in price list is estimated from a typical model for its provider (shown with a "~" and a note under the table) instead of showing as unknown or zero.
- The Trend column in the AI cost table tracks estimated cost rather than raw operation count, and the Fast/Quality/Total rows show a trend too (previously blank), so the arrows reflect what actually moves your spend, such as longer articles costing more at the same number of runs.

### Fixed

- Collapsing and expanding the sidebar is instant now. It used to refetch the whole sidebar from the server on every toggle, so the old layout lingered, briefly squished into the new width, until the request returned. Both the collapsed rail and the full sidebar are rendered up front and the toggle just switches between them in the browser, with no round-trip. On the mobile "collapsible" sidebar, opening the overlay no longer reflows the article-list text either, because the rail is a fixed strip now and the list keeps a constant width.
- Toast notifications (a feed's error when you open it, or a manual refresh result) no longer render at roughly half-width on mobile. They stretch edge to edge with a small gutter on narrow screens and stay centred with a sensible max-width on wider ones.
- Filters that share a priority run in a fixed order now, exactly the order the Settings → Filters list shows (priority, then name). Their order used to be left to the database, so a "stop on match" filter could behave differently between fetches.
- The green "Feed added successfully" banner no longer reappears when you refresh the Feeds settings page after subscribing.
- Articles that carry a label stay visible in their label view even after their feed is deleted or unsubscribed. The view used to inner-join the feed and hide them, leaving the sidebar badge counting an apparently empty category.
- The article-list loading overlay matches the neutral dark-mode background instead of a blue-tinted grey, and is delayed slightly so quick cached loads don't flash a spinner.
- The AI cost table's total is no longer quietly understated when a model slot uses a model missing from the price list. That slot used to be added as $0, making the total look complete while dropping part of the cost.

### Security

- Filter `regex` conditions run under a per-match timeout now, closing a denial-of-service hole. The old create-time heuristic could be bypassed by a catastrophic-backtracking pattern (for example `([a-z]+)*`), and because matching ran synchronously on the event loop during fetch, filter tests and retroactive apply, and CPython's `re` neither times out nor releases the GIL, a single crafted filter could freeze the whole app for every user. Evaluation now uses the `regex` module with a hard timeout; a timed-out pattern counts as no match, and normal filter behaviour is unchanged.

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

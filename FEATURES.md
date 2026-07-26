<!-- Generated from backend/app/content/features.yml by scripts/gen_features.py. Do not edit by hand. -->

# Readfine features

Everything Readfine does, grouped by area. Reading works out of the box; the AI features are an optional layer that runs on your own API key.

## Feeds & subscriptions

_Get content in, from anywhere._

- **RSS & Atom feeds:** Subscribe to any standard feed URL.
- **Web-scraping feeds:** Follow sites that have no feed. Point Readfine at a listing page and a CSS selector that matches the article links; with an AI key it can suggest the selector for you.
- **Authenticated feeds:** Subscribe to feeds behind HTTP Basic Auth with per-feed credentials, stored encrypted.
- **Folders:** Group feeds into folders for organizing and for scoping filters.
- **Scheduled background fetching:** New articles are pulled automatically on a schedule, with adaptive per-feed intervals that back off quiet feeds and check busy ones more often.
- **OPML import & export:** Move your subscriptions in and out, including files compatible with Tiny Tiny RSS.

## Reading experience

_A clean reader that adapts to your screen._

- **Readable extraction:** The full article text is pulled into the reader (`trafilatura`, with `readability-lxml` as a fallback), without the cookie banners and sidebars of the original page.
- **Adaptive layout:** Choose a 2- or 3-panel view per screen size, with breakpoints you set yourself.
- **Dedicated mobile layout:** A real small-screen layout with a collapsible sidebar and inline or full-screen article view, not a squeezed-down desktop.
- **Search:** Full-text search across your articles, scoped by feed, folder, status, or label. Open it from anywhere with the `/` shortcut.
- **Dark mode:** Light, dark, or follow your system preference.
- **Reading typography:** Choose the font family and text size for the reading view.
- **Number & date format:** Pick how numbers and dates are written (decimal separator and date order) independently of the interface language, so English can pair with `1 234,56` and `25.06.2026`.
- **List density:** Compact, comfortable, or summary, set separately for desktop and mobile.
- **Mark read on scroll:** Optionally mark articles read as they scroll past.
- **Article states:** Mark articles read, star favourites, and archive what you want out of the way.
- **Labels:** Your own colour-coded tags for sorting articles by hand.

## Filtering & scoring

_Trim the stream down to what matters._

- **Filters:** Match on title, content, author, URL, or AI score, then automatically add a label, mark read, star, or archive.
- **Regex and AND/OR:** Combine conditions with AND/OR and match with regular expressions (Python syntax) when plain "contains" is not enough.
- **Feed & folder scoping:** Restrict any filter to specific feeds or folders.
- **Retroactive apply:** Run a filter over existing articles, not just new ones.
- **Relevance scoring:** With an AI key, each incoming article is scored against your interest profile, so you can surface or hide articles by score (a filter can mark anything below a threshold as read).

## AI (bring your own key)

_Optional AI on your own provider key._

- **Summaries:** A one-tap summary of any article.
- **Chat over articles:** Ask questions about an article and get answers grounded in its text.
- **Interest profile:** Describe what you care about, or have the AI draft it from what you read; it drives scoring and digests.
- **Profile on a schedule:** Have the profile regenerate itself every 2 or 4 weeks as your reading shifts, with the replaced version one click away.
- **Catch me up:** An on-demand digest of what happened in your feeds over a period and scope you choose.
- **Scheduled briefings:** The same digest on a schedule, delivered to your inbox.
- **Your choice of provider:** Anthropic, OpenAI, or Google Gemini. Keys are per-user, stored encrypted, and used only for your own requests.
- **Custom prompts & models:** Set a fast model for scoring and a quality model for summaries, chat, and briefings, each on the provider you pick. The summary and context prompts are editable, and each briefing can carry its own prompt.

## Insights & stats

_See how you actually read._

- **Reading stats:** Your read rate, reading streak, average dwell time, and your single most active day and hour.
- **Scoring calibration:** Check how well AI relevance scores match what you actually read, and surface "missed gems" the AI rated highly but you never opened.
- **AI usage & cost:** An estimate of your AI spend per provider over time.

## Accounts & administration

_Multi-user, with the operator controls to match._

- **Per-user accounts:** Every account has its own feeds, filters, labels, and preferences.
- **Admin panel:** Manage users and instance-wide settings.
- **Email (SMTP):** Address verification, password reset, and briefing delivery.
- **API tokens:** A JSON API authenticated with JWT tokens, for scripts and integrations.
- **Retention & purge:** Tiered retention rules that purge old articles automatically.

## Privacy & self-hosting

_Self-hosted and fully open source._

- **Self-hosted:** Runs on your own server with Docker; your data stays with you.
- **Open source:** AGPL-3.0 licensed, so you can read and modify every line.
- **Encrypted key storage:** AI keys and other secrets are stored encrypted at rest.
- **SSRF protection:** Feed and scrape fetches are guarded against server-side request forgery.
- **Hosted option:** Prefer not to self-host? Use the hosted instance at [readfine.app](https://readfine.app).

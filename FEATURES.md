<!-- Generated from backend/app/content/features.yml by scripts/gen_features.py. Do not edit by hand. -->

# Readfine features

Everything Readfine does, grouped by area. Reading works out of the box; the AI features are an optional layer that runs on your own API key.

## Feeds & subscriptions

_Get content in, from anywhere._

- **RSS & Atom feeds:** Subscribe to any standard feed URL. Before you commit, a test says whether the address works, what the feed is called, and how many articles it holds.
- **Finds the feed for you:** Hand it the address of a site rather than a feed and it looks for the feeds on the page, then offers them by name so you can tell them apart. Where a site declares nothing, the usual addresses are tried anyway, and a YouTube channel address becomes that channel's feed. If there is genuinely no feed, it offers to scrape the page instead.
- **Web-scraping feeds:** Follow sites that have no feed. Point Readfine at a listing page and a CSS selector that matches the article links; with an AI key it can suggest the selector for you.
- **Authenticated feeds:** Subscribe to feeds and scrape pages behind HTTP Basic Auth, in their own fields or written into the address. Either way the credentials are stored encrypted, per feed.
- **Shared & private feeds:** On an instance with several accounts, everyone following the same feed shares one fetch, so a site is polled once however many subscribers it has. Mark a feed private and it is kept to your account, with its own fetch and its own copy of the articles.
- **Folders:** Group feeds into folders for organizing and for scoping filters.
- **Scheduled background fetching:** New articles are pulled automatically on a schedule, with adaptive per-feed intervals that back off quiet feeds and check busy ones more often. Any feed can be pinned to a fixed interval instead.
- **Feed health:** A feed that stops working says so instead of just going quiet: a red marker in the sidebar, the reason on hover, and a retry button. Sites that rate-limit get backed off automatically, and readable extraction switches itself off for a site that blocks it rather than filling the reader with empty articles.
- **OPML import & export:** Move your subscriptions in and out, including files compatible with Tiny Tiny RSS.

## Reading experience

_A clean reader that adapts to your screen._

- **Readable extraction:** The full article text is pulled into the reader (`trafilatura`, with `readability-lxml` as a fallback), without the cookie banners and sidebars of the original page.
- **Straight to the source:** Feeds that carry only headlines and links leave nothing to read. Such articles offer the original page as a button, and can open it in a new tab on click if you turn that on.
- **Videos play in the reader:** A YouTube or Vimeo link is saved as the video, with its full description under it. Playing it starts the player in place, and no player is loaded until you press play. The thumbnail is served from your own server, so opening an article never tells the video service your address.
- **Adaptive layout:** Choose a 2- or 3-panel view per screen size, with breakpoints you set yourself.
- **Dedicated mobile layout:** A real small-screen layout with a collapsible sidebar and inline or full-screen article view, not a squeezed-down desktop.
- **Install it as an app:** Put Readfine on a phone's home screen or a desktop, where it opens in its own window with no address bar. On Android it also joins the share sheet, so a link shared from any other app lands in Saved. Needs HTTPS, which browsers require before offering to install anything.
- **Search:** Full-text search across your articles, scoped by feed, folder, status, or label. Open it from anywhere with the `/` shortcut.
- **Dark mode:** Light, dark, or follow your system preference.
- **Reading typography:** Choose the font family and text size for the reading view.
- **Number & date format:** Pick how numbers and dates are written (decimal separator and date order) independently of the interface language, so English can pair with `1 234,56` and `25.06.2026`.
- **List density:** Compact, comfortable, or summary, set separately for desktop and mobile.
- **Mark read on scroll:** Optionally mark articles read as they scroll past.
- **Article states:** Mark articles read, star favourites, and archive what you want out of the way.
- **Labels:** Your own colour-coded tags for sorting articles by hand.
- **Save by URL:** Paste a link to keep an article that is not in any of your feeds. It goes through the same readable extraction, lands in Saved, and is kept until you remove it. Also in the API, so a phone shortcut or a bookmarklet can save a link without opening the app.
- **Share by link:** Hand a single article to someone with no Readfine account. The link reads without signing in and stays live until you revoke it.

## Filtering & scoring

_Trim the stream down to what matters._

- **Filters:** Match on title, content, author, URL, or AI score, then automatically add a label, mark read, star, or archive.
- **Regex and AND/OR:** Combine conditions with AND/OR and match with regular expressions (Python syntax) when plain "contains" is not enough.
- **Feed & folder scoping:** Restrict any filter to specific feeds or folders.
- **Retroactive apply:** Run a filter over existing articles, not just new ones.
- **Duplicates across feeds:** A story that two of your feeds both carry is not put in front of you twice. The first copy stays unread and the later one arrives already marked read, so it is still there if you want it.
- **Relevance scoring:** With an AI key, each incoming article is scored against your interest profile, so you can surface or hide articles by score (a filter can mark anything below a threshold as read).

## AI (bring your own key)

_Optional AI on your own provider key._

- **Summaries:** A one-tap summary of any article, plus an option to have starred articles summarized automatically.
- **Chat over articles:** Ask questions about an article and get answers grounded in its text.
- **Interest profile:** Describe what you care about, or have the AI draft it from what you read; it drives scoring and digests.
- **Profile on a schedule:** Have the profile regenerate itself every 2 or 4 weeks as your reading shifts, with the replaced version one click away.
- **Catch me up:** An on-demand digest of what happened in your feeds over a period and scope you choose.
- **Scheduled briefings:** The same digest on a schedule, delivered to your inbox.
- **Your choice of provider:** Anthropic, OpenAI, or Google Gemini. Keys are per-user, stored encrypted, and used only for your own requests.
- **Your own model, your own endpoint:** Point Readfine at anything that speaks the OpenAI API: Ollama or llama.cpp on your own machine, vLLM, a LiteLLM proxy, a gateway like OpenRouter. A local server needs no key at all, and nothing leaves your network. One endpoint per account, with each slot free to run a different model on it.
- **Custom prompts & models:** Set a small model for scoring and a main model for summaries, chat, and briefings, each on the provider you pick. The summary and context prompts are editable, and each briefing can carry its own prompt.

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

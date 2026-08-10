# Changelog

All notable changes to Readfine are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, minor releases may include breaking changes (database
migrations, config changes); `1.0.0` will mark the first API/stability commitment.

## [Unreleased]

### Added

- **Save an article by pasting its link, even one that isn't in any of your feeds.** A new **Saved** entry sits under Archived in the sidebar, with a box at the top of the list to paste a URL into. The page is fetched, run through the same readable extraction as everything else, and shows up as an ordinary article you can read, label, summarize and chat about. It appears in the list straight away, first under its web address and then under its real headline once the text has been pulled in, with a spinner while the fetch runs and a red mark (the same one a broken feed gets) on a row that yielded nothing at all. Your filters run on it, so a rule scoped to "All articles" can label or star it as it arrives; scoring deliberately doesn't, since you already decided this one was worth keeping. A saved article is kept for good and is never purged by retention until you remove it from Saved yourself, from the article's ··· menu or the button row under it. Video links save as the video, and a link that redirects to a consent, login or paywall page is recognised as such rather than saved as article text; both still offer "Open original" and a retry. Because Saved is ordered by publication date a pasted link often lands partway down the list, so saving jumps to the article and flashes it.

- **Save a link from outside the app.** `POST /api/v1/articles/save-url` takes `{"url": "…"}` and does exactly what the Saved box does, so a phone share-sheet shortcut, a bookmarklet or a line of curl can keep an article without opening Readfine. The article comes back straight away, with its address standing in for the title while the page is fetched in the background, so a client that wants the text asks for the article again a moment later. A link you already have is attached to your Saved list rather than duplicated, and says so with a plain 200 instead of the 201 a new save gets. Taking an article back out of Saved is in the API too, as `{"is_saved": false}`. The endpoint allows the same ten saves a minute as the web form, since each one fetches a page.

- **Videos play in the reader.** An article holding a YouTube or Vimeo video, whether you saved the link yourself or it came embedded in the article, shows the thumbnail with a play button and starts the player in place instead of sending you off to the video site. No player is loaded until you press play, so an article you only scrolled past cannot have a video service's cookies set on it; YouTube is loaded through youtube-nocookie.com, and the caption under the thumbnail says which site the player will come from. The thumbnail is served by Readfine, which fetches it from the video service once and caches it, so opening an article never tells YouTube or Vimeo your address or which video it was. Videos from a YouTube feed now show the video from the moment the article arrives, with the whole description under it: the feed already carries both, so the 1.4 MB watch page they were being read from is not requested at all any more. Where scripts cannot run, the thumbnail stays a plain link out to the video.

- **Chapter marks in a video's description are clickable.** A description that lists "0:00 Intro, 3:40 The build" now turns each of those times into a link that jumps the player to that point, starting it if it isn't playing yet. Nothing else in a description is linked, since it is mostly sponsor and affiliate URLs. Where the page's scripts don't run, a mark still opens the video on its own site at the same moment.

- **The article you are reading is now marked in the list.** In the three-panel layout the list gave no clue which of its rows was open in the reading pane, so finding your place after scrolling meant recognising the headline. The open row now carries a blue bar on its left and the same pale fill the sidebar uses for the section you are in, and it stops being dimmed even if you have already read it (which is most of what going through starred articles is made of). It follows the article however you got to it, including a direct link and the Next button on a phone, and stays put when the row is redrawn. The two-panel layout is unchanged, since there the article opens under its own row.

### Changed

- Fetch failures are no longer logged forever. Nothing ever pruned that table, so a feed that kept failing left one row per attempt for the lifetime of the instance. Those rows are now dropped after 90 days, which is well past anything that reads them: the admin dashboard groups the last 30 days and the fetch log page shows the newest 100 entries, so nothing on screen changes.

- Subscribing to a feed now tells you what the feed is called before you commit to it. The box has always had a "Custom title" field, but the feed's own name appeared nowhere on the page, so there was no way to judge whether you wanted a custom one, and the name a feed showed up under was a surprise. Where it mattered most was the list Readfine offers when the address you gave isn't a feed itself: the feeds found on the page were listed by URL alone, with nothing to tell them apart. They now carry their real names, read from each feed rather than from the `title` attribute on the page's link tag, which is usually missing or just says "RSS". That also names the addresses Readfine guesses at (`/feed`, `/rss.xml`), which had nothing before. Picking one from the list fills its name into the custom title's placeholder, and so does testing an address yourself, the same way the feed edit page has always worked: leave the field blank and the feed keeps its own name. None of this costs an extra request. The green "feed added" confirmation also steps aside as soon as you test or add the next one, instead of staying on screen naming a feed you dealt with several feeds ago.

- The admin dashboard is reordered around what actually needs attention, with fetch errors near the top instead of buried under the readable-extraction tables. A row of pills under the counters names every problem the dashboard found and jumps to it, and an instance with nothing wrong says so in one line rather than printing five empty tables. One of those pills carries something the dashboard never reported at all: a feed that failed often enough to switch its own fetching off. Such a feed stops being fetched, so it stops logging errors and drops out of the 30-day list, and the error counter passes over it too because that counts feeds still retrying. It now has a pill of its own, however long ago it gave up, leading to the feeds table where it can be started again or removed. Fetch errors are now one row per feed rather than one per attempt, which is what made the old list mostly the same broken feed repeated: each feed shows how many fetches failed in the last 24 hours, 7 days and 30 days, and the feed's state right now (active, throttled, error, disabled) next to its last error, so you can see at a glance whether it has recovered since. The tables also show more than the five rows they were capped at, and say how many more there are. A fourth counter card tracks feeds currently in error, and the readable-extraction numbers now say what they count, which they previously left to guesswork.

- Private feeds are now marked in the feed lists. Settings → Feeds and the admin feeds table put a small lock next to a feed that is kept to your account rather than shared with other subscribers on the instance, which until now you could only find out by opening the feed. Public feeds stay unmarked, since that is what almost every feed is. The badge on the feed edit page went gray at the same time: amber means "something needs attention" everywhere else in the app, and a private feed is not that.

- AI summaries of long articles are no longer cut short. The prompt asks the model to match the summary's length to the article, but the room it had was fixed, so a long feature ran out of space and stopped mid-sentence while a short news item was never near the limit. That room now scales with the article, from the previous allowance for short pieces up to roughly three times that for the longest ones. Nothing to configure, and the ceiling keeps a custom summary prompt from turning into an essay.

- When an AI provider returns nothing, the error now says why. "returned no usable content" covered three different situations: the model refused the request, it ran out of room before writing anything, or it genuinely replied with nothing. The notice now carries the provider's own stop reason (and, for Gemini, the reason a prompt was blocked), so you can tell whether to reword a prompt, raise a limit, or just retry.

- The AI error notice in Settings → AI now says which article the failure happened on, with a link straight to it, so a failure on one odd article no longer looks the same as a broken API key. Errors that belong to no particular article, such as a failed interest profile, show the message alone. If retention has since removed the article, the notice keeps the message and drops the link.

- The help page now covers the reader itself, not just the setup. A new "Getting around the reader" section names the things you would otherwise have to stumble on: the ↻ and ✓ buttons that appear when you hover a feed, folder, label or view in the sidebar (press and hold on a touchscreen), folding folders and the sidebar away, the ··· menu on an article, and the `/` shortcut for search. New answers explain Saved, sharing an article by link and how to revoke it, what a red marker on a feed means, how long articles are kept and what makes one stay for good, and that there is an API.

### Fixed

- Testing a feed no longer fails in silence when you have tested a lot of them in a row. Ten tests a minute are allowed, and going over that emptied the result box and stopped the spinner with nothing else to show, which read as the test having done nothing at all. It now says the limit was hit and that a minute's wait clears it. Any other failed request in that box reports itself too, instead of disappearing.

- The error message on a failed feed no longer quotes the address with its secrets in it. That message is shown in your feed list and in the admin panel, in the latter right next to the address itself, which is printed with any API key in it blanked out. A message that quoted the address in full walked straight around that. HTTP errors were already built from the blanked version; everything else now gets the same treatment, and credentials written into an address are dropped wherever they turn up.

- Saved pages no longer show raw codes like `&#x27;` where an apostrophe belongs. A few sites escape their own text twice, Vimeo does it on every page, so reading it back once left the second layer showing: a saved video arrived titled "Here&#x27;s how to add music" and its description was peppered with the same thing. Such text is now decoded the rest of the way, in the title and in the description a saved video keeps as its body. Text that only escaped its characters once, which is nearly everything, is untouched.

- The amber "throttled" badge is readable in dark mode again. Its fill kept the light theme's pale yellow while the label turned bright amber, so the two sat almost on top of each other and the word was barely there. The fill now darkens with the rest of the theme. The same pairing is used by the 429 tag in the admin rate-limit list and the warning on a feed test in Settings, so those are fixed too.

- Video thumbnails no longer reach out to the video service when you open an article. A thumbnail's address pointed straight at img.youtube.com, or at vumbnail.com for Vimeo, so your browser fetched it from there the moment an article opened and handed that host your IP address and which video it was, before any click. Thumbnails are now served by Readfine, which fetches each one once, caches it, and hands your browser only its own address; for Vimeo this also drops vumbnail.com, a third party, in favour of Vimeo's own thumbnail. Articles saved before this update are covered too. The player is unchanged: clicking still loads it from youtube-nocookie.com or player.vimeo.com, which is the point at which those services can set their own cookies. (The exposure had been there since videos in articles first showed a thumbnail.)

- Summaries and relevance scores now work on the newest Claude models. Models of the latest generation (Opus 5, Sonnet 5, Fable 5) reason before they answer even when nothing asks them to, and that reasoning is paid for out of the same allowance as the answer itself. Readfine sizes those allowances for the answer alone, and scoring asks only for a single number, so the whole allowance went on reasoning and the reply came back empty. Every request to Claude now says reasoning should be left off, which puts the allowances back to meaning what they say. Models that insist on reasoning anyway, such as Fable 5, are asked again without it. Nothing changes for OpenAI or Gemini, or for the Claude models you were already using.

- The "Test connection" button in Settings → AI no longer reports success for a model that cannot actually answer. It sent a greeting and checked that the request came back, not that anything was written in reply, so a model failing in the way above reported a working connection while every summary and score errored out. It now reads the answer, and a model that returns nothing fails the check with the provider's own reason.

- A summary that ran out of room now says so, instead of passing itself off as complete. Readfine stored whatever the model had written by the time it hit the limit and showed it like any other summary, so a summary ending mid-sentence looked like the model's own choice of words. Such a summary is still kept, but the "AI summary" heading now reads "AI summary · truncated". Regenerating one that comes back complete clears the note.

- A summary that starts on its own now shows its spinner. Starring an article with auto-summary on, or opening a starred article whose extraction finishes and produces one, generated the summary in the background with nothing on screen to show for it, so the only way to find out was to open the article again. The "Generating summary…" spinner now appears in both cases, and a summary that arrives while you are reading is shown straight away. Unstarring while it spins removes it.

- Consent, login and paywall walls are no longer stored as an article's text. Some sites answer a fetch that carries no consent cookie by redirecting to such a page, and since it comes back as HTTP 200 the notice was stored as the article, filed under the site's own name (iDNES and a Google News link were two cases). A fetch that ends on a page holding the address it interrupted, which is how a wall returns you afterwards, is now recognised from the redirect itself, so it needs no wordlist and works in any language. Such an article says the site returned a consent page and offers "Open original" and Retry, and keeps the link you saved so neither button walks back into the wall. Articles from feeds whose pages answer this way fall back to the text the feed itself delivers. Retrying on a schedule is not attempted, since a wall answers the same way every time; the Retry button still works.

- Full-text extraction no longer turns itself off on a feed where it works. Readfine watches whether a feed already delivers whole articles and switches extraction off when it does, but that check read the word count stored with each article, and a successful extraction replaces that number with the word count of the page it pulled in. A feed publishing two-sentence teasers then looked like a full-content feed once you had read a few of its articles, and extraction was switched off at the next fetch, with nothing under readable errors because nothing had failed. The check now counts the words the feed itself delivered, which no extraction overwrites. The same measurement decides the setting when you subscribe to a feed someone else on the instance already reads.

- Article text no longer runs off the side of the screen. Editors like WordPress often glue words together with non-breaking spaces around bold text, so a phrase such as "Wednesday, Anthropic and AMD announced" counted as one 38-character word that a browser cannot wrap, and it hung over the edge of a narrow reading pane. Newly fetched articles now get the longest of these runs broken back into ordinary spaces, keeping the ones that are there on purpose (10 km, 50 %, one-letter prepositions). Articles already stored, and anything else without a break point such as a very long URL, now wrap instead of overflowing.

- An article no longer jumps around while its full text is being extracted. Opening an article whose extraction had not run yet starts one in the background, and the reader checked on it every two seconds by redrawing the entire article, which re-laid out the text, reloaded the images and flashed the action bar. Most obvious on a phone, where the page visibly hopped every couple of seconds. The check now touches only the small "Extracting full content…" line, and the article itself is rebuilt once, when the text is ready.

- The article list highlights the row under the mouse again. The row and the list behind it had both ended up white, so in the light theme nothing happened when you moved across the list. Rows now use the same faint gray the rest of the app uses for hovering, and the dark theme keeps the shade it already had. Touchscreens deliberately have no highlight, since a tap would leave one row lit up; that now follows from the device rather than the width of the window, so a narrow window on a desktop keeps its highlight.

- Share → "Original article" copied a link to Readfine instead of the article's own address. The share menu reads the source URL off the article in the reading pane, but a wrapper element added around it later was picked up first, and it carries no URL, so the code fell back to the page you were on. Sharing to Readfine was affected too, in a smaller way: the shared link itself was right, but the title handed to the system share sheet was empty.

- The counts next to Starred and Archived in the sidebar could stand above the number of articles those views actually open onto. Articles that reach the end of their retention are stripped to a snippet and hidden everywhere they used to be listed, but the two counters were taken straight off your article states without checking, so such an article kept its place in the badge while being absent from the list. Both counters now count what the list shows, and so does the number that appears after you mark a whole view as read.

- Switching between Settings and Admin on a phone no longer hides the page you landed on. The row of tabs across the top scrolls sideways, and the active tab was being scrolled into view on load, but the browser restored the strip's old position a moment later and undid it. The tab is now brought back into view after that, and left alone as soon as you scroll the strip yourself.

- Adding a feed by filling in the HTTP auth fields no longer fails on the first fetch. The credentials were saved with the feed but not used for the fetch that happens while the feed is being added, so a feed behind HTTP auth answered 401 at the moment of subscribing, and the only way in was to write the credentials into its address instead. Both ways work now.

- The readable-extraction panels in the admin dashboard read straight and no longer scroll sideways. The revival panel's heading counted the feeds still awaiting a probe while its table listed the feeds a probe had already revived, two different sets, so the count looked wrong against the rows; the heading now belongs to the table and the feeds still waiting are named in a sentence below it. A feed could also turn up claiming it was revived by no probe, because saving the feed's form cleared the revival bookkeeping while the date stayed behind; the date now goes with the rest. Both extraction tables had also gained a feed column that pushed them past the width of the page: the feed now sits on a second line under the article title, the columns have fixed proportions, and a screen too narrow for all of it scrolls rather than squeezing the text.

### Security

- A feed's password is no longer kept in the clear. Credentials can be given to a feed two ways, in the HTTP auth fields or written into its address as `https://user:password@host/feed`, and only the first was encrypted. The second was stored exactly as typed, which put the password in the database and in every backup, in the feed's name when the feed offered none of its own, in the addresses of every article of a scraping feed, and in the OPML file you get when you export your feeds. It also left such a feed in the shared pool, so anyone else on the instance subscribing to the same address fetched it with your credentials. Credentials are now taken out of the address as the feed is added, encrypted like the ones from the fields, and the feed is kept to your account. Feeds you already have are converted on upgrade, their names and their articles' addresses included. Adding a feed does not change: an address with credentials in it still works, and what you see afterwards is the plain address. One consequence worth knowing: an OPML export now carries that plain address, so re-importing a feed that needs credentials asks for them again, which is already how feeds set up through the fields behave.

- A feed's login details are no longer offered to whatever a redirect points at. When a feed authenticates with a username and password, those were attached to the HTTP client rather than to the individual request, so they went out again at every step of a redirect chain, including a step that left the site. A feed host answering with a single redirect elsewhere could collect them that way. They are now sent only while the address being fetched is still on the site they were entered for, and a redirect that leads off it is followed without them. A host moving its own address from http to https keeps them, since that is the same host and the safer of the two.

- Full-text extraction now connects to the address it checked. Every outside address is verified before Readfine requests it, so it cannot lead to the machine itself or to something on the local network, but the extraction path then passed the host name on to the HTTP client, which looked it up a second time. A name server under someone else's control can answer those two lookups differently, handing a public address to the check and a private one to the connection a moment later. Extraction now uses the same verified-address path the feed fetcher already used, on the first request and on every redirect it follows. This closes an opening that grew more exposed with saving an article by link, which fetches whatever address it is given, on request and as often as asked.

- The admin feed list and fetch log no longer show the secrets some feed addresses carry. A feed can authenticate through its own address, either as `https://user:password@host/feed` or with an API token in the query, and both printed those addresses in full. Both now hide the credentials and the value of any parameter that names a secret, while keeping the rest of the address, which is what tells two feeds on the same site apart. A redacted address is not a working link any more, so it is shown as plain text instead of a dead one. The edit form for a scraping feed also stopped carrying the address in a hidden field; the preview and the AI selector ask the server for it by id, and the server hands it over only for a feed you are subscribed to.

- Readfine no longer downloads a page of unlimited size. Nothing put a ceiling on how much a fetch could pull in, which mattered little while the addresses being fetched were feeds you had subscribed to once, but saving an article fetches whatever address you paste, as often as you paste one. A link that turns out to point at a disk image or a video file was read into memory whole. Downloads now stop at 10 MB, counted after decompression, so a small response that unpacks into gigabytes is stopped as well. A page over the limit says so and is not retried on a schedule, since it would be the same size next time; the Retry button still works. The body of a redirect is not downloaded at all any more, since only the address it points to was ever wanted. Self-hosters can change the limit with `MAX_FETCH_BYTES`.

## [0.14.0] - 2026-07-30

### Added

- Articles with nothing to read now offer the original page. Feeds that carry only a headline and a link (Hacker News is the clearest case) produce articles with no text at all, and opening one showed a blank pane and a line saying so. Such an article now shows an "Open original" button, plus "Retry extraction" when a previous attempt failed, so a source that was temporarily unreachable can be tried again without digging through the menu. Settings → Preferences adds "Open original for empty articles", off by default: with it on, clicking one of these articles opens the source in a new tab straight away, while the article itself still opens in the reader behind it, so it is marked read and stays starrable and labellable. Only articles that will never have text are affected, never one whose extraction is still running or could still be started. Time spent on the source counts towards reading time.

- The interest profile used by AI scoring can now update itself. Settings → AI has an "Auto-generate" switch next to the Generate button with Off (the default), every 2 weeks and every 4 weeks. Until now the profile only changed when you opened settings and pressed a button, so scoring kept ranking articles by whatever you were interested in months ago. An automatic run saves straight away, keeps the version it replaced and offers a one-click revert, and the settings page always shows when the profile last changed and whether it was you or the schedule. Several things have to line up before it spends anything: the interval has to be up, at least 20 articles you read or starred have to be new since the last change, and there have to be enough reading signals that the profile is not padded with feed names. Output that does not look like a profile is thrown away rather than saved, and any attempt that reaches the model, successful or not, waits a full interval before the next one, so a dead API key cannot cost you a call a day. After three failures the schedule turns itself off and says why, in settings and on the admin dashboard.

- Bot protection on the registration form, on by default and with nothing to configure. Open registration means the instance will email any address a visitor types, which bots abuse to flood scraped third-party inboxes using your domain as the sender. The form now carries a hidden honeypot field that only a script fills in, and a signed timestamp that rejects submissions arriving faster than a person could type. A caught submission creates no account and sends no email. Only the honeypot, which nothing legitimate can fill in, answers with the normal "check your email" page so the script cannot tell it was caught; the timing check is a heuristic, so it puts the form back in front of you instead, as does a form left open for hours. Correcting a mistyped password and submitting again does not restart the timer either, so a password manager refilling the form in one click cannot trip it. The README explains what else to put in front of a public instance.
- `LOG_OUTBOUND_REQUESTS` (off by default): a diagnostic switch that logs one line per outbound HTTP request, covering feed fetches, scraping and readable extraction. Each line records the host, status, HTTP version, elapsed time and any rate-limit headers the server sent (`Retry-After`, `X-RateLimit-*`). Per-feed error records only show failures, and only per feed, so they cannot answer how often a host is really being hit when several feeds and the extraction pipeline share it. Turn this on when a site starts returning 403 or 429, read the real request rate and spacing from the log, then turn it off again.

- `LOG_LEVEL` (`DEBUG`/`INFO`/`WARNING`/`ERROR`, default `WARNING`): how much the app logs. `WARNING` keeps the log to things that need attention, `INFO` adds the running commentary from the scheduler and fetcher. Noisy libraries (httpx, APScheduler) stay at `WARNING` either way, so raising the level surfaces Readfine's own records rather than a wall of third-party chatter.

- `FETCH_SCHEDULE_OFFSET_MIN` (minutes, default `0`): shifts the four 15-minute feed-fetch ticks off the usual :00/:15/:30/:45. Useful when two instances share a host (for example a staging instance next to production) and you don't want both firing their fetch round at the same wall-clock moment. Set staging to `7` and it polls at :07/:22/:37/:52 instead. The value is folded into 0–14, and the predicted next-fetch times in the UI follow the shifted schedule.

### Fixed

- A filter regex could quietly fail to match on a busy instance. Filter patterns run under a time limit so that a pathological one cannot freeze the app, and a pattern that runs out of time counts as "no match", which means the filter silently does not fire. The limit was 0.1 seconds of wall-clock time, close enough to what an ordinary pattern costs on a long article that a busy fetch round was enough to trip it: a production log showed `\bAI\b` timing out. The limit is now a full second, which still stops a runaway pattern but leaves normal ones far below it.
- A feed whose server dropped the connection mid-request is now retried once instead of counting as a failed fetch. Reusing a kept-alive connection races with the server closing it, and HTTP/2 servers close them routinely, so a request could die with nothing sent back through no fault of the request itself. Fetches are plain GETs, so a second attempt on a fresh connection is safe, and it recovers feeds that were losing a poll here and there for this reason.
- Settings → AI no longer announces a nightly interest-profile generation that cannot happen. The status line checked that a quality model was picked, but not that there was a key to use it with, so an account whose key had been removed (or a restore that lost the encryption key) read "The next generation runs tonight" indefinitely while the job skipped every night without recording anything. It now says which provider is missing a key.
- Three AI settings actions (saving a provider key, verifying a model, queueing summaries for all starred articles) were served with no rate limit, despite carrying one in the code. The limit was attached to the route in the wrong order, so it wrapped a copy of the handler that nothing ever called. Generating the interest profile by hand had no limit declared at all, which mattered more: it is the most expensive call the app can make, it builds its prompt from the whole reading history, and one stuck button could run it as fast as the provider would answer. It now allows 5 per hour, configurable through `RATE_LIMIT_AI_PREFERENCE`. A test now checks every rate-limited route is really limited, since the failure left no trace anywhere.
- The general AI chat no longer scrolls the page behind it once you have sent a message. The message list was set to swallow the scroll at its own end, but only until the first reply arrived, because the server rebuilt the panel without that setting. Most visible on a phone, where reaching the bottom of a conversation started dragging the article list underneath.
- The label next to the paperclip in an article's chat now updates when you detach or reattach the article, instead of only doing so after your first message. The panel was drawn without the hook the script needed to find it, and a later redraw quietly added it back.

- Saving Settings → AI with scoring switched off no longer erases the interest profile. The profile field is disabled while scoring is off, and a disabled field sends nothing at all, so the save read it as "cleared" and wiped the text. Fields that were not part of the submit now keep their stored value, which also covers the new auto-generate schedule and the score-in-list toggle.
- The scoring checkbox in Settings → AI now enables and disables the fields below it as you click it, instead of only after saving. The checkbox carried two id attributes and the script hooked onto the one the browser had thrown away.
- Feeds on sites that block automated clients no longer get marked as failing and eventually switched off. Some sites (Reddit is the obvious one) refuse a share of requests with HTTP 403 or a bare 429 and let the rest through, in waves that last minutes to hours and hit every feed on the site at once. Readfine counted each refusal as a fetch error, so five of them in a row disabled a feed that was working fine. Refusals are now tracked separately from real errors: the feed keeps its normal state, backs off progressively instead of retrying on its usual schedule, and is only switched off after ten in a row. Settings → Feeds and the admin feed table label such a feed "throttled" once it has been refused three times running, in amber rather than red, and one stray refusal shows nothing at all. Any successful fetch clears the count.
- Readfine could learn a fetch spacing that no site had actually asked for. A site that reports its rate limit as exhausted also reports how long until the limit resets, and that countdown was read as if it were the sustainable gap between requests. It is not: it depends on when the request happened to land in the site's current window, so the value swung between 0 and 60 seconds on identical traffic, and the "never loosen" rule kept the highest number seen. On Reddit this settled at 78 seconds, throttling every feed on the host for no reason. The countdown is now used for what it is, a deadline before the next request, and the learned spacing only comes from a limit with room left in it or from an explicit `Retry-After`. The stored values are cleared once on upgrade so nothing carries the old numbers forward; sites that advertise a real limit are re-learned on the next fetch.
- Admin → Rate limits could list a host at a spacing of 0 seconds, which read as a learned limit but meant the opposite: a single rate-limit response had created the row before anything was learned, and it then stayed forever. Such rows are no longer written, and existing ones are removed.
- Full-content extraction switched off because a site refused it now gets a second chance instead of staying off for good. Three refusals in a row disable extraction for a feed, for everyone subscribed to it, and only editing the feed by hand ever turned it back on. That made a passing block permanent: the move to HTTP/2 fixed a whole class of these refusals, yet the feeds it fixed stayed disabled, and the damage went past the article body, because labelled articles from such a feed reach AI scoring with the short feed text instead of the full one. A feed in this state is now retried twice, three days after it was disabled and a fortnight after that, and extraction comes back on if the site lets a page through. The retry only looks at whether the page downloads, so a video post or live blog at the top of the feed cannot condemn it. Feeds you switched off yourself are left alone, and so are feeds disabled for the other reason (they already carry full articles). A feed that gets refused again after coming back is not retried a third time. Existing feeds are queued for a retry when you upgrade, spread over a week. Nothing is re-extracted retroactively; articles already stored keep the text they have.
- Settings → AI spells OpenAI's name the way OpenAI does. The API key list capitalised the stored identifier, so `openai` came out as "Openai", and the two provider dropdowns showed the identifier untouched, in lowercase. All three now use the providers' own names.
- Application log records never reached the log at all. Uvicorn configures only its own loggers and leaves the root logger without a handler, so every `logger.info()` in Readfine was discarded and warnings fell through Python's fallback handler with no timestamp and no source, leaving no way to tell which part of the app wrote a line. Logging is now configured at startup with a proper format. This also means `LOG_OUTBOUND_REQUESTS` produces visible output; it lifts its own records above `LOG_LEVEL`, so switching it on is enough.

### Changed

- Settings → AI is now grouped by feature: Scoring, Summaries & context, Chat, and Limits, each under its own heading. The interest profile used to sit in a section of its own at the bottom of the page, far from the switch that decides whether it is used at all; it is now indented under "Enable article scoring" together with "Show score in article list", so it is visible which settings depend on which.
- A feed that has permanently moved now gets its stored address updated, so each fetch is a single request again. Until now the redirect was followed but never remembered, and the feed walked the same chain on every poll, forever: one feed here cost three requests and 800 ms where one would do, and it counted against the request budget of sites that ration them. The new address is only taken when the site said the move is permanent, the fetch actually produced articles, and nothing changes along the way that should not (a query string, an HTTPS connection, or credentials in the URL, which are neither dropped nor picked up: a site that answers with credentials in its redirect does not get them stored on a feed everyone shares). Existing feeds fix themselves on their next fetch. Adding a feed resolves the address first as well, so a subscription, or an OPML import carrying a years-old URL, lands on the feed you already have instead of creating a second copy of it.
- Admin dashboard: a "Feed redirect conflicts" section, shown only when there is something to report. It lists feeds that permanently redirect onto a URL another feed already holds, the one case where the address above cannot be updated, so the feed keeps re-walking its redirect. The fix is to merge the pair by hand; the section names both feeds so it can be found.
- Admin → Users: hovering the "Joined" date now shows the exact date and time of sign-up. The column itself still shows the date alone, which is not enough to see that a burst of accounts arrived within the same minute.

## [0.13.0] - 2026-07-19

### Added

- Preferences → "Number & date format": a per-user choice of how numbers and dates are written, independent of the interface language. Five profiles cover the common conventions (US, UK/International, Europe, DE/AT, ISO), each differing in the decimal separator, thousands separator and date order, so you can keep the app in English yet see `1 234,56` and `25.06.2026`. New accounts are detected from the browser at sign-up; existing accounts start on the Europe profile and can switch anytime. The setting drives numbers across the stats and AI cost views and the numeric date formats throughout the app, including the date shown in briefing emails (in your timezone). Times stay 24-hour for now.
- A categorized feature list at `/features`, linked from the landing page and the help guide, plus a matching `FEATURES.md` at the repo root. Both are generated from one source (`backend/app/content/features.yml`): the app renders it at runtime, and `scripts/gen_features.py` projects it into `FEATURES.md`, so the list is never maintained in two places. CI regenerates the Markdown and fails if the committed copy is stale.
- Adaptive fetch intervals: a feed left on "Auto" is now polled at a cadence derived from how often it actually publishes, rather than the flat default. Readfine counts each feed's items over the last 7 days and targets an interval a bit shorter than its real publish gap, so busy feeds refresh more often and quiet ones less. New feeds and feeds without enough history keep using the global default, and an explicit per-feed interval still wins. Admin → Settings gains a "Maximum fetch interval" cap for how rarely a quiet feed may be polled; the feed edit screen shows the interval Auto would pick. Cadence is recomputed daily and at startup. The feeds tables (Settings → Feeds and the admin panel) show each feed's effective interval and its predicted next fetch (relative, e.g. "next ~1h") under the last-fetch column, with intervals of an hour or more rendered in hours.
- Preferences → "Advance after mark all as read" (off by default): after you mark a feed, folder or label read from the sidebar, Readfine selects and opens the next one that still has unread articles, expanding a collapsed folder if needed. Feeds advance across folder boundaries; empty scopes are skipped, and the special views (All articles, Starred, Archived) are left alone.
- Admin → Feeds: an "Edit" action on the table's ··· menu for shared feed fields (title, status, fetch interval, and the scrape article-links selector with a live preview). Per-subscriber preferences and feed credentials are intentionally left out, since an admin usually is not the subscriber.
- Admin dashboard: a "Briefing errors" section listing catch-up configs whose scheduled briefing is currently failing (user, config, error, retries, next send). Configs with no scheduled retry, for example when SMTP is unconfigured, sort first and are flagged as needing manual attention; the entry clears once a send succeeds.
- Admin → Users: per-user columns showing whether an account is currently active, not just its lifetime totals. "Read 7d" counts articles genuinely read in the last 7 days (marked read with at least 30 seconds of dwell, the same signal the reading stats use), "AI 7d" counts AI operations in the last 7 days across summaries, scoring, context, chat and catch-up, and a filter count.

### Changed

- Fetch interval dropdowns (Admin → Settings and the feed edit screens) now show longer intervals in hours, for example `6h` or `24h`, instead of raw minutes like `360 min`. This matches how the feeds tables already render intervals. The admin settings also spell out how the default, minimum and maximum intervals interact with Auto mode (the minimum floors Auto too, and Auto never polls faster than 30 minutes).
- Readable extraction backfills an article's publication date from the article page when the feed listing carried no date, so undated articles sort and expire correctly instead of all landing at fetch time. It reads the page's structured `datePublished` (via htmldate) rather than the oldest date on the page, guards against implausible future dates, and never overrides a date the feed already provided. The reader's date updates once extraction finishes.
- Deleting a feed subscription or folder now cleans up references to it left dangling in filter scopes and catch-up/briefing scopes. As a safeguard against silently widening, a filter or briefing whose scope would empty out is deactivated or disabled instead, and the affected filter and briefing names are surfaced in the feeds settings banner.

### Fixed

- Admin → Feeds: the "Force fetch" button no longer returns a 500 when the fetch fails (for example a rate-limited or erroring feed). A failed fetch rolls back the database session, which expires the loaded feed and admin objects; the handler then read an attribute off one of them and crashed with `MissingGreenlet`. It now captures what it needs before fetching and reloads the feed's post-fetch state safely, so the usual "Rate-limited, try again in..." toast shows instead of an error.
- The sidebar now highlights the active category right after opening or reloading the app, not only after a click. On load the highlight was applied to the collapsed rail copy of each nav item (which is hidden) instead of the visible full-sidebar copy, so nothing appeared selected until you clicked a category.
- Feeds behind some CDNs (most visibly Reddit, via Fastly) that kept failing with `403 Blocked` or persistent `429` now fetch normally. The fetcher spoke plain HTTP/1.1, which these CDNs treat as a bot signal and answer with a header-less 403 or a near-zero rate budget, while serving HTTP/2 clients as usual. Server-side fetches (feeds, scraping, readable extraction) now negotiate HTTP/2 when the server offers it and fall back to HTTP/1.1 otherwise. The User-Agent was never the cause.
- Scrape-type feeds now record a "last published" date in the feeds tables (Settings → Feeds and the admin panel), like RSS feeds already did. The scrape fetcher tracked every other feed field but never set this one, so the column stayed empty even when the scraped listing carried article dates. It now advances to the newest dated link on each fetch and stays empty only when the listing exposes no dates at all.
- The single-feed API endpoints (`GET`/`POST`/`PATCH /api/v1/feeds/{id}`) now report `unread_count` computed fresh from the database, like the feed list already did. They previously serialized a cached column that no longer had a live writer on every path, so it could read as stale or zero. The cached column has been dropped (migration 0079); every response counts unread on read.
- Server-side feed and scrape fetches now pin each connection to the IP address they validated, closing a DNS-rebinding gap: previously the SSRF check resolved the hostname once and the HTTP client resolved it again at connect time, so a hostname whose DNS flipped between the two could pass validation yet connect to a private or cloud-metadata address. Every request and redirect hop now connects to the checked IP, carrying the original `Host` header and, for HTTPS, the original hostname for TLS SNI and certificate verification.
- Readable extraction is no longer enabled for Tumblr feeds, which already deliver the full post in the feed. Extracting the page instead pulled in the likes/reblogs "notes" list as the body and duplicated the post text; Tumblr (and other feeds that advertise themselves as full-content via their `<generator>`) are now treated as full-content at subscribe time, so their feed content is shown as-is. Should extraction still run on a Tumblr page, the notes list and tracking pixels are stripped before extraction as a safeguard.
- Marking a feed or folder read from the sidebar now refreshes the whole sidebar, so counts that share those articles (labels, other feeds) update right away instead of going stale until the next reload.
- Readable extraction removes duplicate images: some news sites emit the same photo as several renditions (lead, inline, responsive) and each was extracted, so the body showed the same picture two or three times. Matching on the image filename now collapses them to a single copy.
- Admin → Feeds: on narrow screens the "Feed" column no longer collapses to a couple of unreadable characters. As the table grew it stopped fitting the panel, and the flexible feed column shrank to nothing while the rest scrolled; it now keeps a readable minimum width and the table scrolls horizontally instead.
- Admin → Feeds: a host group that contains a failing feed no longer paints its entire header red, which overstated severity and blended into the group separator; only the host name turns red now.
- Scheduled fetching no longer crashes while arming per-host pacing: after a fetch committed, the scheduler re-read the feed's URL off an expired ORM object, which raised a `MissingGreenlet` error, failed the fetch, and skipped arming the adaptive per-host spacing. The spacing was therefore never enforced between same-host feeds, so hosts with several feeds (Reddit, YouTube) were fetched in bursts and more likely to answer 403. The URL is now captured before the fetch, so pacing is armed as intended.
- A feed marked errored in the sidebar now clears its red indicator immediately when a manual refresh succeeds, instead of staying red until the next full page reload. The refresh only swapped the unread count, which sits apart from the error marker; it now updates the marker out-of-band from the feed's fresh status.

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
  serialized within a fetch round (different hosts still run in parallel), which
  flattens the burst that made some of those feeds return HTTP 429.
- Readable extraction that returns no usable content, e.g. a Reddit article page
  that serves a bot-verification wall (HTTP 200) instead of the article, is no
  longer saved as a blank "successful" extraction that rendered an empty body. Such
  articles now show their original feed content, and a feed whose pages keep
  extracting nothing auto-disables full-content extraction after repeated empties
  (the same way persistent HTTP 403 blocks already did) instead of re-fetching every
  page forever.
- The auto-disabled notice for full-content extraction now states why it was turned
  off (the feed already delivers full articles, or the site blocked extraction /
  returned no readable content) instead of always claiming the site blocked it.
- The article view no longer flickers an endless "Extracting full content…" spinner
  for an article whose extraction failed and is waiting to retry; it shows the feed
  content quietly, and the spinner appears only while a first attempt is in flight.
- "Extract full content" from the article menu no longer momentarily drops the
  article's star, archive, or label state from the action bar.

## [0.10.1] - 2026-06-27

### Added

- One-command local demo: `docker compose -f docker-compose.demo.yml up` brings the
  app up on `http://localhost:8000` with a seeded admin and no setup wizard, for
  trying it out before a full install. Demo only: plain HTTP, `DEBUG=true`, and
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
- `backup.sh`: off-site PostgreSQL backups via `pg_dump` + restic (encrypted,
  deduplicated, retention), with a Cloudflare R2 example config. See README → Backups.

Notes for self-hosters:

- **Registration is closed by default** on a fresh install. Only the admin account
  exists; enable sign-ups in the admin panel to open the instance.
- Shell scripts are pinned to LF line endings (`.gitattributes`) so `setup.sh` runs
  correctly when the repo is cloned/unzipped on Windows.

### Release process

See [`RELEASING.md`](RELEASING.md) for versioning rules and the full pre-release checklist.

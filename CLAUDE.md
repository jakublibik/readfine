# Projekt: Readfine

Self-hosted webová RSS čtečka podobná Tiny Tiny RSS.
Specifikace: `RSS_Aplikace_Specifikace.md` — přečíst na začátku práce.

## Tech stack
- Backend: Python 3.12 + FastAPI
- Databáze: PostgreSQL
- Task queue: APScheduler (uvnitř FastAPI procesu)
- Frontend: HTMX + Jinja2 + Tailwind CSS
- Readable extrakce: trafilatura → readability-lxml
- AI: Anthropic Claude API + OpenAI API + Google gemini API (konfigurovatelné) 
- Auth: JWT tokeny
- Nasazení: Docker na VPS
- Package manager: uv

## Technická rozhodnutí
- **Prostředí**: hybrid – PostgreSQL v Dockeru, FastAPI lokálně přes uv
- **SMTP**: vlastní schránka na Webglobe (libik.cz)
- **VPS**: řeší se při nasazení (po MVP)
- **CSRF**: JWT v `Authorization` headeru → CSRF není potřeba
- **Git workflow**: `dev` = vývoj (výchozí větev), `master` = produkce/release; merge do master jen při vydání verze

## Stav implementace
- Core implementace dokončena (MVP + post-MVP + AI). Připravujeme veřejné nasazení.
- Plán zveřejnění: `../plans/GoPublicPlan.md`

### Hotovo (Fáze 1–7 + post-MVP)
- Fáze 1: struktura projektu, config, Docker, Alembic migrace
- Fáze 2: autentizace, JWT, registrace, reset hesla
- Fáze 3: feeds, fetchování, APScheduler, složky
- Fáze 4: články, 3-panel UI, HTMX, stavy článků
- Fáze 5: štítky, filtry (včetně multi-select scope_include/scope_except)
- Fáze 6: nastavení uživatele, admin panel, SMTP, API tokeny
- Fáze 7 – readable extraction: trafilatura → readability-lxml pipeline, scheduler, retry, admin dashboard
- Fáze 7 – purge jobs: age- a count-based retention
- Fáze 7 – různé: content source label, folder UniqueConstraint, fetch_auth_pass SecretStr
- Post-MVP: dark mode, layout přepínač (2/3-panel), zobrazení labelů na článcích, HTTP auth při editaci feedu
- Post-MVP: web scraping feed type — CSS selector, AI prompt, scrape setup UI, feed detection validation, published_at z URL (`_YYMMDDHHMM_`), excerpt z listingu
- Post-MVP: sidebar UX — synchronní feed refresh se spinnerem, badge update, toast (ok/warning/error); červený pruh u chybných feedů; warning toast při kliknutí na chybný feed
- Post-MVP: /feeds/detect — auto-detekce RSS/Atom feed URL ze zadané stránky (scrape `<link rel="alternate">` + fallback heuristika)

## TODO
- **Readable před filtrováním (scrape feedy)**: readable extraction synchronně při fetchi → filtry mají k dispozici plný text; kompromis: zpomalí fetch
- limit na počet článků při prvním stažení
- **Per-feed/per-user retention (ke zvážení)**: `UserFeed.purge_after_days` a `purge_keep_count` existují v DB, ale nemají UI. Multi-user scénář je komplikovaný — purge by musel brát nejbenevolentnější nastavení ze všech userů pro daný feed a mazat jen per-user přiřazení, ne článek samotný. Pravděpodobně zbytečná komplexita.
- **Katalog veřejných feedů**: při přidávání feedu možnost vybrat z předpřipravené nabídky veřejných/populárních feedů
- **JWT refresh tokeny**: access token 15 min + refresh token (dlouhodobý, v HttpOnly cookie)
- **Rate limiting — DB lockout**: `failed_login_attempts` + `locked_until` v tabulce `users`; persistentní, funguje při multi-process deployi; vyžaduje DB migraci + cleanup job. Aktuálně řešeno in-memory counterem (resetuje se při restartu).
- Filter scope S2: scope_include/scope_except → JSONB
- **OPML export scrape feedů**: OPML standard CSS selektory nepodporuje — zvážit vlastní rozšíření formátu pro round-trip export/import scrape feedů
- **AI integrace**: fáze — shrnutí, kategorizace, doporučení článků
- **Catch me up — global default prompt**: globální výchozí prompt pro catch me up v Settings → AI (stejný vzor jako Summary prompt / Context prompt) — přepíše vestavěný default, ale per-config custom_prompt má přednost. Pole `ai_catchup_prompt` v `user_settings`.
- **Catch me up — user profile v promptu**: přidat volitelný checkbox "Use my reading profile" do catch me up formuláře — pokud `ai_preference_text` existuje, přidat ho do systémového promptu pro framing digestu. Výchozí vypnuté, zobrazovat jen pokud profil existuje. Poznámka: scoring + scope selector už pre-filtrují dle preferencí, profil přidá spíš framing než selekci.
- **htmldate pro published_at**: integrovat `htmldate.find_date()` do `readable_service.py` — po úspěšné readable extraction spustit na stránce článku a aktualizovat `published_at`, pokud dosud nebylo nastaveno z listingu. Pomůže webům bez `<time datetime>` v kartičkách (Aktuálně, Respekt, Deník…). Nevyřeší weby se zakázaným readable. Závislost htmldate je tranzitivní přes trafilatura (uv add htmldate).
- **Scraping — Prozkoumat**: headless prohlížeč (Playwright, Puppeteer) pro fetch JS-rendered stránek a stránek vyžadujících přihlášení. Řešení by bylo použít headless prohlížeč pro fetch — ale to je výrazně složitější infrastruktura a mimo scope aktuálního scraping setupu.
- **robots.txt pro scrape feedy**: před scrapingem zkontrolovat robots.txt cílového webu (urllib.robotparser). Zvážit: snížení úspěšnosti vs. férovost vůči serverům. Readable extraction je jako browser reader mode — robots.txt typicky neřeší.
- **Filter akce `archive`**: přidat jako akci filtru (vedle label, mark_read, star) — nastaví `is_archived = true` na `user_article_states`. Schéma, service i šablona filter_edit.
- **Streaming summary/context**: on-demand generování summary a context streamovat místo čekání na celou odpověď — uživatel vidí text jak se generuje. FastAPI `StreamingResponse` + SSE nebo chunked transfer + JS/HTMX update na frontendu. Zvážit také snížení `_CONTENT_MAX_CHARS` pro summary z 12 000 na ~5 000 znaků.
- **Datum bez přebliknutí**: datum v article listu a detailu se přeformátuje JS po načtení → viditelný flicker. Řešení: formátovat datum na serveru (Jinja2/Python) s timezone uživatele — přidat pole `timezone` do user profilu, aplikovat přes `zoneinfo`. Postupně: nejdřív bez timezone (UTC), pak přidat nastavení v profilu. **Po implementaci uložení timezone:** při změně timezone přepočítat `briefing_next_send_at` pro všechny aktivní briefingy uživatele (`UserCatchupConfig` kde `briefing_enabled=True`) pomocí `briefing_service.compute_next_send_at()`.
- **Web search v chatu**: prozkoumat a zvážit implementaci built-in web search nástroje pro AI chat (Anthropic web search tool, OpenAI Bing grounding, Gemini Google Search grounding) — umožní odpovídat na aktuální dotazy nad rámec tréninkových dat. Zvážit cenu, přínos a zda to dává smysl v kontextu čtečky (primární use-case je chat nad článkem, ne vyhledávání).
- **Read per day tabulka (stats)**: aktuálně skryta — zvážit rozšíření zobrazovaných hodnot (např. přečteno vs. otevřeno, dwell time, trend) než ji znovu zobrazíme. Šablona: `settings/stats.html`.
- **Settings feeds tabulka — layout**: sloupce (Type, Status, Articles, Last fetch, Published) mají příliš velké rozestupy, tabulka zbytečně moc zaujímá prostor. Potřeba přepracovat layout — odstranit `w-full`, sjednotit padding s ostatními tabulkami v settings. Šablona: `settings/partials/feeds_list.html`.
- **Briefings — interval Monthly**: přidat měsíční interval do Catch me up & Briefings — vyžaduje novou periodu `30days` v `catchup_service.py`, řešení edge cases (překrývání period, day-of-month výběr 1–28 + "Last day of month").
- **Mazání feedu/složky — cleanup scope**: při mazání feedu nebo složky zkontrolovat použití ve filtrech, `scope_include` catchup configs a briefing scope — upozornit uživatele a vyčistit scope ze všech míst. Systémové řešení napříč celou appkou.
- **Admin — přehled briefing chyb**: tabulka v admin sekci zobrazující konfigurace kde `briefing_last_error IS NOT NULL` — sloupce: uživatel, název konfigurace, chyba, čas posledního pokusu. Pouze read-only přehled, oprava je na uživateli.
- **YouTube feed type — vylepšení**: `youtube` je rezervováno v DB, ale nemá vlastní fetcher. Implementovat: embed videa v article detailu (YouTube iframe/thumbnail místo odkazu), thumbnail jako cover image, případně délka videa v metadatech. Fetch zůstane přes standardní RSS (`youtube.com/feeds/videos.xml?channel_id=...`). Detect pro `@handle` URL funguje přes HTML fallback (`<link rel="alternate">`).
- **Cache-busting pro statické soubory**: přidat query param s hashem (např. `?v={hash}`) k `tailwind.css` a JS souborům v `base.html`, aby se URL při každém buildu změnila a Cloudflare/browser nikdy neservíroval zastaralou verzi. Bez toho je nutné po každém deployi ručně purgovat Cloudflare cache (CSS má `max-age=14400`, tj. 4h). Implementace: hash souboru při startu FastAPI → kontextová proměnná v Jinja2 (`{{ static_url('css/tailwind.css') }}`).
- **Per-feed scoring toggle — zapojit + opravit pořadí kontrol**: pole `UserFeed.ai_scoring_enabled` (tri-state True/False/None) existuje v DB, ale žádné UI ho nenastaví → vždy `None`. Přidat per-feed přepínač (feed edit). POZOR: `scoring_eligible()` v `ai_scoring_service.py` kontroluje `ai_scoring_enabled_default` PŘED per-feed override, takže per-feed `True` dnes NEPŘEBIJE vypnutý globální default (umí jen vypnout). Komentář slibuje opak. Při zapojení UI upravit pořadí: per-feed `True` → zapnout navzdory defaultu, `False` → vypnout, `None` → řídit se defaultem.

## Testování
- **Testovat**: auth flows (login, registrace, verifikace, reset hesla), správa účtu (smazání), nevratné/destruktivní operace s daty, business logic services (fetcher, filtry, AI pipeline, briefing, scoring, purge), security-critical paths (crypto, rate limiting, URL/SSRF validace)
- **Netestovat**: CRUD routes (změna jména, emailu, hesla, nastavení), admin UI, Jinja2 šablony, jednoduché statické routes — nízké riziko, reversibilní nebo triviální
- Nová funkce dostane test pokud: je nevratná, security-critical, nebo obsahuje netriviální business logiku

## CSS/Tailwind Conventions
- When fixing layout bugs, find the root cause (e.g. flex/truncate parent) rather than patching symptoms

## Before Large Changes
- For non-trivial fixes (e.g. error handling, new features), propose at least 2 possible approaches with tradeoffs. Wait for my approval before implementing.
- Don't assume behavior is a bug — verify current behavior is actually wrong before 'fixing' it

## Preference uživatele
- odpovídej stručně a věcně
- Komunikace v češtině
- Výchozí jazyk aplikace: **en** (angličtina)
- Na konci session: `git push` (do `dev`) + `/clear`

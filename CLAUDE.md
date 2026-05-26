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
- MVP dokončeno, post-MVP fáze

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
- **Katalog veřejných feedů**: při přidávání feedu možnost vybrat z předpřipravené nabídky veřejných/populárních feedů
- **JWT refresh tokeny**: access token 15 min + refresh token (dlouhodobý, v HttpOnly cookie)
- Filter scope S2: scope_include/scope_except → JSONB
- **OPML export scrape feedů**: OPML standard CSS selektory nepodporuje — zvážit vlastní rozšíření formátu pro round-trip export/import scrape feedů
- **AI integrace**: fáze — shrnutí, kategorizace, doporučení článků
- **htmldate pro published_at**: integrovat `htmldate.find_date()` do `readable_service.py` — po úspěšné readable extraction spustit na stránce článku a aktualizovat `published_at`, pokud dosud nebylo nastaveno z listingu. Pomůže webům bez `<time datetime>` v kartičkách (Aktuálně, Respekt, Deník…). Nevyřeší weby se zakázaným readable. Závislost htmldate je tranzitivní přes trafilatura (uv add htmldate).
- **robots.txt pro scrape feedy**: před scrapingem zkontrolovat robots.txt cílového webu (urllib.robotparser). Zvážit: snížení úspěšnosti vs. férovost vůči serverům. Readable extraction je jako browser reader mode — robots.txt typicky neřeší.
- **Filter akce `archive`**: přidat jako akci filtru (vedle label, mark_read, star) — nastaví `is_archived = true` na `user_article_states`. Schéma, service i šablona filter_edit.
- **Streaming summary/context**: on-demand generování summary a context streamovat místo čekání na celou odpověď — uživatel vidí text jak se generuje. FastAPI `StreamingResponse` + SSE nebo chunked transfer + JS/HTMX update na frontendu. Zvážit také snížení `_CONTENT_MAX_CHARS` pro summary z 12 000 na ~5 000 znaků.
- **Datum bez přebliknutí**: datum v article listu a detailu se přeformátuje JS po načtení → viditelný flicker. Řešení: formátovat datum na serveru (Jinja2/Python) s timezone uživatele — přidat pole `timezone` do user profilu, aplikovat přes `zoneinfo`. Postupně: nejdřív bez timezone (UTC), pak přidat nastavení v profilu.
- **Web search v chatu**: prozkoumat a zvážit implementaci built-in web search nástroje pro AI chat (Anthropic web search tool, OpenAI Bing grounding, Gemini Google Search grounding) — umožní odpovídat na aktuální dotazy nad rámec tréninkových dat. Zvážit cenu, přínos a zda to dává smysl v kontextu čtečky (primární use-case je chat nad článkem, ne vyhledávání).
- **Read per day tabulka (stats)**: aktuálně skryta — zvážit rozšíření zobrazovaných hodnot (např. přečteno vs. otevřeno, dwell time, trend) než ji znovu zobrazíme. Šablona: `settings/stats.html`.

## Testování
- Strategie: testy jen pro kritické části (auth, fetcher, filtry) — CRUD a UI bez testů

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

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

## TODO
- limit na počet článků při prvním stažení
- **Katalog veřejných feedů**: při přidávání feedu možnost vybrat z předpřipravené nabídky veřejných/populárních feedů
- **JWT refresh tokeny**: access token 15 min + refresh token (dlouhodobý, v HttpOnly cookie)
- **/feeds/detect**: auto-detekce RSS/Atom feed URL ze zadané stránky (scrape `<link rel="alternate">` + fallback heuristika)
- Filter scope S2: scope_include/scope_except → JSONB
- Plošné testové pokrytí: rozšíření testů nad rámec kritických částí
- **Web scraping**: fáze — sledování libovolných webových stránek bez RSS
- **OPML export scrape feedů**: OPML standard CSS selektory nepodporuje — zvážit vlastní rozšíření formátu pro round-trip export/import scrape feedů
- **AI integrace**: fáze — shrnutí, kategorizace, doporučení článků

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

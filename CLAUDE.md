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
- dokončena fáze 7, ladíme chyby

### Hotovo (Fáze 1–6 + většina Fáze 7)
- Fáze 1: struktura projektu, config, Docker, Alembic migrace
- Fáze 2: autentizace, JWT, registrace, reset hesla
- Fáze 3: feeds, fetchování, APScheduler, složky
- Fáze 4: články, 3-panel UI, HTMX, stavy článků
- Fáze 5: štítky, filtry (včetně multi-select scope_include/scope_except)
- Fáze 6: nastavení uživatele, admin panel, SMTP, API tokeny
- Fáze 7 – readable extraction: trafilatura → readability-lxml pipeline, scheduler, retry, admin dashboard
- Fáze 7 – purge jobs: age- a count-based retention
- Fáze 7 – různé: content source label, folder UniqueConstraint, fetch_auth_pass SecretStr

### Zbývá dokončit MVP (Fáze 7)


## Odložené nálezy (post-MVP nebo ve volném prostoru)
1. JWT lifetime 15 min + refresh token
2. Filter scope S2: scope_include/scope_except → JSONB
3. /feeds/detect endpoint (auto-detekce feed URL ze stránky)

## Post-MVP TODO
- limit na počet článků při prvním stažení
- **Layout přepínač**: volba mezi 3-panel a 2-panel zobrazením v nastavení uživatele
- **Katalog veřejných feedů**: při přidávání feedu možnost vybrat z předpřipravené nabídky veřejných/populárních feedů
- **Readable extraction – cookie injection**: Per-feed session cookies pro weby s cookie-based auth (seekingalpha.com apod.). Šifrovat stejně jako fetch_auth_pass, uživatel obnovuje po expiraci.
- **Zobrazení labelů na článcích**: barevné badgy v seznamu článků i v detailu. Vyžaduje: přidat labels do ArticleResponse (schema + JOIN/array_agg), šablony article_row.html + article_detail.html. Zvážit toggle v user_settings.
- **HTTP auth při editaci feedu pro jediného odběratele**: pokud je user jediný subscriber daného feedu, umožnit mu nastavit/změnit HTTP Basic Auth přímo v feed_edit (fetch_auth_user + fetch_auth_pass). Aktuálně je to možné jen při přidávání feedu.
- **Relativní URL obrázků v článcích**: článek může obsahovat relativní `<img src="/...">` URL, které prohlížeč doplní jako `readfine.app/...` místo původní domény. Fix: při ukládání/zobrazování obsahu přepsat relativní URL na absolutní pomocí base URL článku.
- **Dark mode**: přidat volbu tmavého tématu do nastavení uživatele. Vyžaduje `darkMode: 'class'` v tailwind.config.js + `dark_mode` pole v user_settings + dark: varianty ve všech šablonách (~1-2 dny práce).

## Post-MVP nápady
- **Plošné testové pokrytí**: Po MVP zvážit rozšíření testů nad rámec kritických částí.

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

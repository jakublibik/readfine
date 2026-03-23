# Projekt: Filtread

Pracujeme na self-hosted webové RSS čtečce podobné Tiny Tiny RSS, název aplikace je Filtread.
Specifikace je v `RSS_Aplikace_Specifikace.md` — vždy ji přečti na začátku práce.
Reference funkcí TTRSS: `TinyTinyRSS_Funkce_Reference.docx`

## Navržený tech stack
- Backend: Python + FastAPI
- Databáze: PostgreSQL
- Task queue: Celery + Redis
- Frontend: React + Vite
- Readable extrakce: readability-lxml
- AI: Anthropic Claude API + OpenAI API (konfigurovatelné)
- Auth: JWT tokeny
- Nasazení: Docker na VPS

## Stav projektu
- Specifikace hotová, kódování nezačato
- Otevřené otázky viz konec specifikace (sdílené kanály, offline čtení, API standard, open-source distribuce)

## Technická rozhodnutí
- **Package manager**: uv
- **Python**: 3.12
- **Prostředí**: hybrid – PostgreSQL v Dockeru, FastAPI lokálně přes uv
- **SMTP**: vlastní schránka na Webglobe (libik.cz) – SMTP údaje z administrace Webglobe
- **VPS**: zatím ne, řeší se až při nasazení (fáze 6–7)

## TODO na příští session
- [ ] Bod 0: Založit git repozitář
- [ ] Zahájit implementaci – začít bodem 1 (Základy projektu):
  - Adresářová struktura, `pyproject.toml` (závislosti)
  - `config.py`, `database.py`, `main.py`
  - Docker + `docker-compose.yml`
  - Alembic – první migrace (celé DB schéma)

## Plán implementace (7 fází)
1. **Základy projektu** – struktura, config, Docker, Alembic migrace
2. **Autentizace** – User modely, registrace, přihlášení, session, šablony
3. **Feeds a fetchování** – Feed/Folder modely, RSS fetcher, APScheduler
4. **Články a UI** – Article model, 3-panel layout, HTMX, stavy článků
5. **Štítky a filtry** – Label/Filter modely, aplikace filtrů při fetchování
6. **Nastavení a admin** – nastavení uživatele, admin panel, SMTP
7. **Dokončení MVP** – OPML, vyhledávání, readable extrakce, purge joby

## Preference uživatele
- Komunikace v češtině

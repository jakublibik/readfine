# Review commitu `155f9f8d1f2e79f705d511b31acd7e39f17d10cb`

Repo: `jakublibik/filtread`  
Datum review: 2026-03-24

## 1) Logické chyby a edge cases

### ✅ Co je dobře
- Commit konzistentně zavádí základ projektu (FastAPI app factory, async SQLAlchemy, Alembic, modely).
- DB návrh odpovídá cíli „sdílené feedy + per-user stav“.
- `updated_at` triggery jsou přidané na tabulky, které mají být podle architektury automaticky aktualizované.
- FTS index řeší `unaccent` přes immutable wrapper (`immutable_unaccent`), což je správně kvůli index expression.

### ⚠️ Problémy / rizika
1. **Nesoulad datového typu v `AuditLog.id` mezi migrací a ORM**
   - Migrace: `audit_log.id` je `BigInteger`.
   - ORM (`backend/app/models/settings.py`): `AuditLog.id` je `Integer`.
   - Riziko: overflow v budoucnu + nekonzistence introspekce/ORM mapování.

2. **Nesoulad specifikace vs implementace modelových souborů**
   - Architektura uvádí `fetch_log.py`, ale implementace má `FetchLog` v `article.py`.
   - Není to runtime bug, ale snižuje to čitelnost a porušuje deklarovanou strukturu.

3. **`allowed_hosts` default v kódu je `["*"]`**
   - V `.env.example` jsou hosty restriktivní, ale pokud ENV chybí, app akceptuje vše.
   - Pro secure-by-default by měl být bezpečný default i v kódu.

4. **`app/static` mount bez ověření existence cesty**
   - `StaticFiles(directory="app/static")` může při špatném working directory nebo chybějící složce failnout při startu.
   - V Dockeru to pravděpodobně projde, mimo Docker nemusí.

---

## 2) Bezpečnost (SQLi, XSS, CSRF, citlivá data)

### SQL injection
- V tomhle commitu nejsou vidět dynamické SQL dotazy z user input (kromě statických `op.execute` v migraci), takže **aktuálně bez přímého SQLi nálezu**.

### XSS
- Architektura správně požaduje sanitizaci RSS obsahu (`nh3`) a CSP.
- V commitu je CSP middleware přidán — to je plus.
- Sanitizační vrstva ještě není implementovaná (což je v této fázi očekávatelné), ale znamená to, že jakmile se začne renderovat RSS HTML, je to kritická oblast.

### CSRF
- **Kritický gap vůči Architektura.md:** v závislostech je `starlette-csrf`, ale middleware/validace tokenu zatím není zapojená.
- Vzhledem k plánovanému session auth + HTMX POST endpointům je to důležité doplnit před prvními mutačními routami.

### Citlivá data
- `.env.example` obsahuje jasné placeholdery, to je OK.
- `docker-compose.yml` má hardcoded DB heslo `filtread/filtread` (pro lokální dev přijatelné, ale musí být explicitně označeno jako non-production).
- `SessionMiddleware` používá `SECRET_KEY` a v produkci `https_only=True` (díky `not settings.debug`) — správně.

### Další bezpečnostní poznámka
- CSP má `img-src * data:` — funkčně to dává smysl pro externí obrázky z feedů, ale je to volnější politika; doporučit alespoň budoucí zvážení proxy obrázků nebo whitelisting.

---

## 3) DB schéma (FK, indexy, constraints)

### ✅ Silné stránky
- FK a `ondelete` strategie jsou z větší části dobře navržené.
- Partial unique indexy pro veřejné feedy (`ix_feeds_url_public`) a share token (`ix_uas_share_token`) dávají smysl.
- Deduplikace článků přes `(feed_id, guid_hash)` partial unique je správný směr.
- Check constraints pro enum-like pole (`feeds.status`, `feeds.feed_type`, `articles.readable_status`) jsou přítomné.

### ⚠️ Nálezy
1. **`audit_log.id` typová nekonzistence** (viz výše) — opravit v ORM na `BigInteger`.
2. **Chybějící DB-level constraints pro některé enum-like sloupce**
   - Např. `users.role`, `filters.match_operator`, `filter_actions.action_type` nejsou omezeny CHECKem.
   - Není to blocker, ale zvyšuje riziko nekonzistence dat.
3. **`app_settings` singleton (id=1) je jen konvencí**
   - Seed row je vložen, ale chybí explicitní constraint/guard, který by bránil vložení dalších řádků.
   - V praxi lze držet aplikačně, ale DB guard by byl robustnější.
4. **Indexová strategie pro budoucí časté query není úplná**
   - Např. u `password_reset_tokens` může chybět index na `(user_id, expires_at)` pro cleanup/lookup scénáře.
   - Není kritické pro MVP, ale dobré doplnit postupně.

---

## 4) Konzistence s `Architektura.md`

### ✅ Sedí
- Sdílené feedy + per-user stav a nastavení (tabulky `feeds`, `user_feeds`, `user_article_states`) sedí.
- AI tabulky/sloupce připravené už ve Fázi 1 sedí.
- `updated_at` triggery sedí.
- Základ middleware stacku (TrustedHost, session, security headers, rate limiting integration) odpovídá návrhu.

### ⚠️ Nesoulady
1. **Chybí CSRF middleware/integrace**, ačkoli je v architektuře explicitně povinná.
2. **Nesoulad projektové struktury** (`fetch_log.py` vs `FetchLog` v `article.py`).
3. **V architektuře je zmínka o session datech v PostgreSQL**, ale v commitu je použita pouze podpisová cookie session (`SessionMiddleware`), bez server-side session store implementace.

---

## 5) Co chybí implementovat

1. **CSRF ochrana end-to-end**
   - middleware, token issuance/validation, HTMX header (`X-CSRFToken`), hidden input pro formuláře.
2. **Inicializační seed prvního admina**
   - konfigurace (`FIRST_ADMIN_EMAIL/PASSWORD`) je připravená, ale není vidět realizační logika.
3. **Router registration**
   - skeleton adresáře existuje, ale `main.py` zatím nepřipojuje žádné routery.
4. **DB session guard**
   - `get_db()` spoléhá na globální `async_session_factory`; chybí fail-fast check s jasnou chybou, pokud factory není inicializovaná.
5. **Readme/dev bootstrap kroky**
   - pro nový projekt by pomohlo explicitně popsat `alembic upgrade head`, jak startovat app a jak se seeduje admin.

---

## 6) Doporučení

### Priorita P0 (doporučeno hned)
1. **Doplnit CSRF ochranu podle Architektura.md.**
2. **Opravit `AuditLog.id` typ v ORM na `BigInteger`.**
3. **Změnit secure default `allowed_hosts` z `["*"]` na restriktivní hodnotu** (např. localhost) a vynutit override pro produkci.

### Priorita P1
4. Přidat CHECK constraints aspoň pro:
   - `users.role IN ('admin','user')`
   - `filters.match_operator IN ('AND','OR')`
5. Sjednotit strukturu modelů s dokumentací (`fetch_log.py`), nebo aktualizovat `Architektura.md`.
6. Přidat guardy/validace pro singleton `app_settings` (alespoň servisní vrstva + test).

### Priorita P2
7. Zpřesnit CSP dlouhodobě (zejména `img-src`).
8. Přidat základní integrační test: start app + DB init + migrace + smoke test middleware.

---

## Závěrečné hodnocení

**⚠️ Drobnosti**

Důvod:
- Nevidím kritickou chybu, která by sama o sobě invalidovala foundation commit.
- Ale jsou zde důležité bezpečnostní a konzistenční mezery (zejména CSRF a typová nekonzistence `audit_log.id`), které je potřeba opravit před dalším rozšiřováním funkčnosti.
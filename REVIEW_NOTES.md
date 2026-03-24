# REVIEW – Filtread (jakublibik/filtread)

Datum review: 2026-03-24  
Reviewer: Senior software architect (AI)

## Kontext
Tento review je proveden **nad dostupnou dokumentací**:
- `Architektura.md`
- `RSS_Aplikace_Specifikace.md`
- `CLAUDE.md`
- předchozí `REVIEW_NOTES.md`

> Poznámka: Nebyl dostupný zdrojový kód `backend/app/...`, proto nelze potvrdit konkrétní implementační detaily (např. reálné SQL dotazy, escaping v šablonách, middleware pořadí, CSRF token validaci v runtime).

---

## 1) Logické chyby / edge cases

### 1.1 Rozpor v modelu mazání feedu vs zachování článků
V dokumentaci jsou současně přítomny dvě ne zcela kompatibilní myšlenky:
- zachování článků i po zrušení feedu (historie),
- mazání feedu při `subscriber_count = 0` přes cleanup job.

To může vést k nekonzistentnímu chování:
- jednou článek přežije bez feedu (`feed_id = NULL`),
- jindy dojde k cascade scénáři mimo očekávání.

**Doporučení:**
- definovat **jednoznačné invarianty**:
  - co se musí stát s články při unsubscribe posledního uživatele;
  - kdy se smí fyzicky smazat feed;
  - jak se chovají read/starred/archive stavy po odpojení feedu.

### 1.2 Dualita „MVP API ano/ne“
V části dokumentace je REST API podrobně popsáno, jinde je zmíněno „MVP bez externího API“.  
To vytváří nejistotu v prioritách, testech i security scope.

**Doporučení:**
- explicitně označit, zda `/api/v1` je:
  1) součást MVP, nebo
  2) post-MVP fáze.

### 1.3 Scheduler model – riziko duplikace jobů
APScheduler běžící v procesu FastAPI je v pohodě pro 1 worker, ale problém při scale-outu.

**Doporučení:**
- v architektuře tvrdě zafixovat provozní režim:
  - MVP = 1 worker (a zapsat do Docker command), nebo
  - samostatný scheduler container / persistent jobstore + leader lock.

### 1.4 Readable extrakce – UX timeout edge case
Asynchronní extrakce s pollováním je dobrá, ale musí být definováno:
- co po timeoutu,
- jak UI pozná trvalé selhání,
- kdy se retry zastaví.

**Doporučení:**
- přidat explicitní stavový model (`pending/success/failed`) a retry policy.

---

## 2) Bezpečnost (SQL injection, XSS, CSRF, citlivá data)

## 2.1 CSRF
Dokumentace CSRF řeší správně koncepčně, ale není ověřeno, že je skutečně implementováno ve všech state-changing routách (vč. HTMX).

**Riziko:** vysoké, pokud není enforce middleware + token validace všude.

**Doporučení:**
- vynutit CSRF pro `POST/PUT/PATCH/DELETE`,
- sjednotit předávání tokenu pro HTMX i klasické formuláře,
- přidat integrační testy (negativní scénáře bez tokenu).

### 2.2 XSS
Aplikace pracuje s RSS/HTML obsahem článků. Bez sanitizace je riziko persistent XSS vysoké.

**Doporučení:**
- sanitizovat `content/readable_content` (allowlist tagů),
- v Jinja2 nepoužívat `|safe` bez sanitizace,
- přidat CSP hlavičky.

### 2.3 SQL injection
Architektura zmiňuje SQLAlchemy + parametrizované dotazy, ale bez zdrojáku nelze potvrdit u raw SQL (`text(...)`).

**Doporučení:**
- audit všech raw SQL částí: pouze bind parametry, žádné string concat.
- přidat testy na injection payloady u search/filter endpointů.

### 2.4 Secrets a šifrování
`SECRET_KEY` a `ENCRYPTION_KEY` jsou správně oddělené. Chybí ale jasný rotační postup v provozní dokumentaci.

**Doporučení:**
- popsat playbook rotace klíčů (downtime/no-downtime varianta),
- zavést secret manager (ne plain `.env` v produkci),
- log redaction pro citlivé hodnoty.

### 2.5 Rate limiting
Zmíněno správně, ale je potřeba ověřit pokrytí všech citlivých endpointů:
- login, reset password, invitation consume, share token, token creation.

---

## 3) DB schéma (FK, indexy, constraints)

### Silné stránky
- Dobré FK vazby a per-user oddělení dat.
- Smysluplné unikátní klíče (`(user_id, feed_id)`, `(user_id, article_id)` atd.).
- Důležité indexy pro listování a filtry.
- Promyšlený model `user_article_states` (lazy state creation).

### Rizika / připomínky
1. **Partial unique a nullable scénáře** – správně navrženo, ale nutné ověřit skutečné migrace a názvy indexů.
2. **CHECK constraints** (`status`, `feed_type`) – dobré, ale ověřit konzistenci enum hodnot napříč backendem.
3. **`updated_at` triggery** – v dokumentaci jsou, nutné ověřit, že opravdu existují ve všech relevantních tabulkách.
4. **FTS pro češtinu** – `simple + unaccent` je MVP OK, ale kvalita vyhledávání bude omezená.

---

## 4) Konzistence vůči `Architektura.md`

### Nalezená nekonzistence
- `CLAUDE.md` obsahuje historický stack (React/Vite, Celery/Redis), zatímco `Architektura.md` má HTMX/Jinja2 + APScheduler.

**Dopad:** zmatky při implementaci, onboarding, nesoulad s očekáváním contributorů.

**Doporučení:**
- `CLAUDE.md` buď:
  - aktualizovat na aktuální architekturu, nebo
  - přesunout do „historické poznámky“.

---

## 5) Chybějící věci (pravděpodobně)

> Na základě dokumentace, nikoliv potvrzené implementace:

1. Jednoznačná provozní strategie scheduleru pro produkci.
2. Formální threat model (minimálně stručný security checklist).
3. Explicitní retention politika pro články/feedy při unsubscribe.
4. Incident/backup/restore postupy pro PostgreSQL.
5. E2E test scénáře pro CSRF + XSS + auth edge cases.
6. Definice audit logu pro admin akce (reset hesla, aktivace/deaktivace účtu, invitations).

---

## 6) Doporučení (co zlepšit)

### Krátkodobě (nejvyšší priorita)
1. Sjednotit dokumentaci (`Architektura.md` × `CLAUDE.md` × specifikace).
2. Dopsat a vynutit CSRF + testy.
3. Zafixovat politiku mazání feedů/článků.
4. Explicitně uzavřít status `/api/v1` v MVP.
5. Zapsat scheduler strategy do deploymentu (a CI kontrolu).

### Střednědobě
1. Security hardening: CSP, secure headers, login audit trail.
2. Test matice pro filtry/search (včetně injection payloadů).
3. Lepší FTS pro češtinu (trigram / custom dictionary dle potřeby).
4. Připravit playbook rotace klíčů + re-encryption migrace.

---

## Závěrečné hodnocení

### Celkově
Architektonický návrh je **silný a promyšlený**, zejména v DB modelu a oddělení per-user stavů.  
Hlavní problém není „špatný design“, ale **nekonzistence dokumentace + několik neuzavřených bezpečnostních/provozních rozhodnutí**.

**Shrnutí:** ⚠️ **Drobnosti** (s potenciálem přerůst v kritické při produkčním nasazení, pokud se neuzavřou).
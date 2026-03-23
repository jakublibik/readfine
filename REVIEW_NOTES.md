# Review poznámky k dokumentům (RSS_Aplikace_Specifikace.md a Architektura.md)

Datum revize: 2026-03-23

## Shrnutí
Návrh dává z velké části smysl: MVP scope, DB schéma i plán rozšíření jsou konzistentní a promyšlené. Před zahájením implementace je ale dobré vyřešit několik nesouladů a rizik, jinak bude později potřeba přepisovat.

## 1) Největší nesoulady mezi dokumenty

### A) Frontend: React vs HTMX/Jinja
- Specifikace/architektura pro MVP počítají s HTMX + Jinja2 + Tailwind.
- CLAUDE.md stále zmiňuje React + Vite.

**Doporučení:** sjednotit CLAUDE.md se zbytkem dokumentace (nebo jasně označit jako historickou úvahu).

### B) Task queue / scheduler: Celery+Redis vs APScheduler
- CLAUDE.md uvádí Celery + Redis.
- Architektura.md uvádí APScheduler uvnitř FastAPI procesu.

**Doporučení:** zvolit jednu cestu pro MVP a explicitně ji napsat jako “source of truth”.
- APScheduler je jednodušší na provoz pro MVP, ale musí se ošetřit multi-worker nasazení.
- Celery/Redis je robustnější pro delší úlohy a škálování, ale přidává infra.

### C) „MVP bez externího API“ vs návrh REST API
- Specifikace říká, že MVP nebude mít externí API.
- Architektura detailně navrhuje REST API `/api/v1/`.

**Doporučení:** ujasnit formulaci:
- buď „MVP má interní JSON API pro vlastní web a budoucí klienty“,
- nebo „MVP API odkládáme“.

## 2) DB schéma – kritické body

### A) Mazání feedu vs FK v `articles.feed_id`
- Specifikace: při smazání kanálu se hvězdičkované a archivované články zachovají bez kanálu.
- Architektura: `articles.feed_id` je `NOT NULL` a `ON DELETE CASCADE`.

To je přímý konflikt.

**Možnosti řešení:**
1) Smazání feedu smaže články (jednodušší, běžné chování RSS čteček).
2) Zachovat články bez feedu:
   - udělat `feed_id` NULLable + `ON DELETE SET NULL`, nebo
   - místo hard delete udělat soft delete feedu.

### B) Izolace feedů per user
Je to OK pro MVP (jednodušší), ale do budoucna to komplikuje sdílení feedů a deduplikaci stažení napříč uživateli.

### C) `api_tokens.token_hash` – bcrypt vs SHA-256
Dokumentace zmiňuje bcrypt/SHA-256.

**Doporučení:** zvolit jeden model ukládání:
- typicky SHA-256(token) + prefix pro UI;
- bcrypt je také možný, ale ztěžuje lookup a je dražší.

### D) `share_token`
- Token musí být kryptograficky náhodný (např. 128 bit).
- Sdílecí endpoint později doplnit o rate limit.

### E) Full-text search
- `simple` je pro MVP OK.
- U češtiny je dobré počítat s omezeným stemmingem a případně později řešit lepší konfiguraci.

## 3) Backend architektura – rizika pro implementaci

### A) APScheduler uvnitř FastAPI procesu
Při běhu s více workery hrozí duplikátní spouštění jobů.

**Doporučení (MVP):** explicitně napsat jednu strategii:
- MVP běží s 1 workerem, nebo
- scheduler běží v separátním procesu/containeru, nebo
- použít persistent jobstore + leader/lock.

### B) Readable extrakce jen při „zajímavých“ článcích
Je to chytré pro snížení zátěže, ale vyžaduje UX:
- indikátor „načítám fulltext“,
- refresh detailu po dokončení extrakce.

### C) Deduplikace přes `guid_hash`
Je to OK, jen u některých feedů je GUID nestabilní – dobré fall backovat na URL (což už v dokumentu zmiňuješ).

## 4) Co doplnit (security / provoz)
1) CSRF ochrana pro web se session cookies (i pro HTMX POST/DELETE).
2) Rate limiting pro login / reset password / token endpointy.
3) CORS politika, pokud API použijí externí klienti.
4) Secret management (rotace `SECRET_KEY`/`ENCRYPTION_KEY` a dopad na sessions/tokeny).
5) DB triggery pro `updated_at`: popsat implementaci v Alembic migraci (trigger function + trigger).

## 5) Co opravit hned (checklist)
1) Sjednotit CLAUDE.md s Architektura.md (React/Celery pryč, nebo označit jako historické).
2) Rozhodnout a opravit rozpor kolem mazání feedu vs zachování článků.
3) Ujasnit, zda `/api/v1` je MVP deliverable nebo až později, a upravit text ve specifikaci.
4) Explicitně popsat strategii scheduleru v produkci (1 worker vs separátním proces vs leader election).
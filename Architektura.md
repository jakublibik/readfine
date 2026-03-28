# Filtread – Architektura aplikace (Fáze 1 MVP + příprava na Fázi 2)

Datum: 23. 3. 2026 (aktualizováno: 28. 3. 2026)
Stav: Návrh architektury — poznámky o stavu implementace jsou označeny ⚠️

---

## 1. DB schéma (PostgreSQL)

### Principy návrhu

- Feedy jsou sdílené napříč uživateli – každý feed se fetchuje jednou globálně, bez ohledu na počet předplatitelů.
- Per-user nastavení feedu (složka, název, purge, pozice) je v tabulce `user_feeds`.
- Per-user stavy článků (přečteno, hvězdička, archivace) jsou v tabulce `user_article_states`.
- Všechny numerické cizí klíče jsou `INTEGER` (PostgreSQL sequence).
- `updated_at` triggery se nastaví na DB úrovni.
- Sloupce pro Fázi 2 (AI) jsou přítomny od začátku, ale mohou být NULL a aplikace je ignoruje, dokud AI není aktivní.

---

### Tabulka: `users`

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SERIAL` | PK | Primární klíč |
| `email` | `VARCHAR(255)` | UNIQUE, NOT NULL | Přihlašovací email |
| `password_hash` | `VARCHAR(255)` | NOT NULL | bcrypt hash hesla |
| `display_name` | `VARCHAR(100)` | NOT NULL | Zobrazované jméno |
| `role` | `VARCHAR(20)` | NOT NULL, DEFAULT `'user'` | `'admin'` nebo `'user'` |
| `is_active` | `BOOLEAN` | NOT NULL, DEFAULT `TRUE` | Aktivní/deaktivovaný účet |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Datum registrace |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Poslední změna |

Indexy: `email` (unique), `role`

---

### Tabulka: `user_settings`

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `user_id` | `INTEGER` | PK, FK → `users.id` ON DELETE CASCADE | 1:1 s users |
| `list_density_web` | `VARCHAR(20)` | DEFAULT `'medium'` | `compact`, `medium`, `full` |
| `list_density_mobile` | `VARCHAR(20)` | DEFAULT `'compact'` | `compact`, `medium`, `full` |
| `mark_read_on_scroll` | `BOOLEAN` | DEFAULT `TRUE` | Automark přečteno při scrollu |
| `unread_filter` | `VARCHAR(20)` | DEFAULT `'adaptive'` | ⚠️ Nahrazuje `show_unread_only`: `adaptive`, `unread_only`, `show_all` |
| `default_sort_order` | `VARCHAR(10)` | DEFAULT `'newest'` | `newest`, `oldest` |
| `left_panel_pinned` | `BOOLEAN` | DEFAULT `TRUE` | Levý panel připnutý/overlay |
| `articles_per_page` | `SMALLINT` | DEFAULT `40` | ⚠️ MVP: jedno pole místo desktop/mobile variant |
| `timezone` | `VARCHAR(50)` | DEFAULT `'UTC'` | Časová zóna uživatele (záloha pro serverové formátování) |
| `language` | `VARCHAR(10)` | DEFAULT `'en'` | Jazyk UI (`cs`, `en`) |
| `keyboard_shortcuts_enabled` | `BOOLEAN` | DEFAULT `TRUE` | Klávesové zkratky |
| `ai_enabled` | `BOOLEAN` | DEFAULT `TRUE` | AI funkce zapnuty pro tohoto uživatele (relevantní jen pokud `app_settings.ai_enabled = TRUE`) |
| `list_content_fields` | `JSONB` | DEFAULT `'["summary"]'` | ⚠️ Plánováno pro Fázi 7 — zatím není implementováno; snippet se generuje automaticky ze summary/content |
| `detail_content_fields` | `JSONB` | DEFAULT `'["content"]'` | ⚠️ Plánováno pro Fázi 7 — zatím se renderuje content/readable_content dle dostupnosti |

Poznámka k `list_content_fields` a `detail_content_fields` (plánováno Fáze 7):
- Dostupné hodnoty: `summary`, `ai_summary`, `readable_content`, `content`
- `ai_summary` se nabídne jen pokud `app_settings.ai_enabled AND user_settings.ai_enabled AND user_feeds.ai_enabled`
- `readable_content` se nabídne jen pokud `user_feeds.extract_readable = TRUE`
- Fallback při chybějícím obsahu (jen pokud záložní pole není samo v seznamu): `ai_summary` → `summary`, `readable_content` → `content`
- Výchozí hodnoty pro nového uživatele: `list_content_fields` = `["ai_summary"]` pokud AI dostupné, jinak `["summary"]`; `detail_content_fields` = `["readable_content"]` pokud readable dostupné, jinak `["content"]`

---

### Tabulka: `api_tokens`

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SERIAL` | PK | Primární klíč |
| `user_id` | `INTEGER` | NOT NULL, FK → `users.id` ON DELETE CASCADE | Vlastník tokenu |
| `name` | `VARCHAR(100)` | NOT NULL | Pojmenování (např. "iPhone") |
| `token_hash` | `CHAR(64)` | NOT NULL, UNIQUE | SHA-256 hex hash tokenu |
| `token_prefix` | `VARCHAR(10)` | NOT NULL | Prvních 8 znaků pro identifikaci (zobrazení) |
| `last_used_at` | `TIMESTAMPTZ` | | Poslední použití |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Datum vytvoření |
| `revoked_at` | `TIMESTAMPTZ` | | NULL = aktivní, datum = odvolaný |

Indexy: `user_id`, `token_hash` (unique)

---

### Tabulka: `password_reset_tokens`

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SERIAL` | PK | Primární klíč |
| `user_id` | `INTEGER` | NOT NULL, FK → `users.id` ON DELETE CASCADE | Vlastník |
| `token_hash` | `VARCHAR(255)` | NOT NULL, UNIQUE | Hash tokenu |
| `expires_at` | `TIMESTAMPTZ` | NOT NULL | Platnost (1 hodina) |
| `used_at` | `TIMESTAMPTZ` | | NULL = nevyužit |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Datum vytvoření |

---

### Tabulka: `invitations`

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SERIAL` | PK | Primární klíč |
| `created_by` | `INTEGER` | NOT NULL, FK → `users.id` | Admin, který vytvořil pozvánku |
| `token` | `VARCHAR(64)` | NOT NULL, UNIQUE | URL token pozvánky |
| `email` | `VARCHAR(255)` | | Volitelně fixované na konkrétní email |
| `expires_at` | `TIMESTAMPTZ` | | NULL = neomezená platnost |
| `used_at` | `TIMESTAMPTZ` | | NULL = nevyužita |
| `used_by` | `INTEGER` | FK → `users.id` | Kdo pozvánku využil |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Datum vytvoření |

---

### Tabulka: `app_settings`

Globální nastavení aplikace – vždy právě jeden řádek (id=1).

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SMALLINT` | PK, DEFAULT 1 | Vždy 1 |
| `registration_enabled` | `BOOLEAN` | NOT NULL, DEFAULT `TRUE` | Otevřená/uzavřená registrace |
| `default_fetch_interval_min` | `SMALLINT` | NOT NULL, DEFAULT `60` | Výchozí interval fetchování v minutách (použije se, pokud feed nemá vlastní `fetch_interval_min`) |
| `min_fetch_interval_min` | `SMALLINT` | NOT NULL, DEFAULT `15` | Minimální povolený interval – uživatel nemůže nastavit nižší hodnotu per-feed |
| `max_feeds_per_user` | `SMALLINT` | NOT NULL, DEFAULT `200` | Limit kanálů na uživatele |
| `default_purge_after_days` | `SMALLINT` | DEFAULT `90` | NULL = nemazat dle stáří |
| `default_purge_keep_count` | `SMALLINT` | DEFAULT `500` | NULL = nemazat dle počtu |
| `smtp_host` | `VARCHAR(255)` | | SMTP server |
| `smtp_port` | `SMALLINT` | DEFAULT `587` | SMTP port |
| `smtp_user` | `VARCHAR(255)` | | SMTP uživatel |
| `smtp_password_encrypted` | `TEXT` | | Šifrované heslo |
| `smtp_from_email` | `VARCHAR(255)` | | Odesílací email |
| `smtp_use_tls` | `BOOLEAN` | DEFAULT `TRUE` | TLS pro SMTP |
| `ai_enabled` | `BOOLEAN` | DEFAULT `FALSE` | Globální přepínač AI (Fáze 2) |
| `ai_require_user_keys` | `BOOLEAN` | DEFAULT `FALSE` | Vynucení vlastních klíčů uživatelů (Fáze 2) |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Poslední změna |

---

### Tabulka: `folders`

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SERIAL` | PK | Primární klíč |
| `user_id` | `INTEGER` | NOT NULL, FK → `users.id` ON DELETE CASCADE | Vlastník |
| `name` | `VARCHAR(100)` | NOT NULL | Název složky |
| `position` | `SMALLINT` | NOT NULL, DEFAULT `0` | Pořadí v levém panelu |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Datum vytvoření |

Indexy: `(user_id, name)` UNIQUE, `user_id`

---

### Tabulka: `feeds`

Globální pool feedů. Veřejné feedy jsou sdílené napříč uživateli (každá URL jednou). Privátní feedy (s auth) jsou per-user – stejná URL může existovat vícekrát jako různé privátní instance.

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SERIAL` | PK | Primární klíč |
| `feed_url` | `VARCHAR(2048)` | NOT NULL | URL RSS/Atom feedu |
| `is_private` | `BOOLEAN` | NOT NULL, DEFAULT `FALSE` | Privátní feed (auth) – per-user, nesdílený |
| `fetch_auth_user` | `VARCHAR(255)` | | HTTP Basic Auth user |
| `fetch_auth_pass_encrypted` | `TEXT` | | HTTP Basic Auth heslo (šifrované) |
| `site_url` | `VARCHAR(2048)` | | URL webu |
| `title` | `VARCHAR(255)` | NOT NULL | Název z feedu |
| `favicon_url` | `VARCHAR(2048)` | | URL favicony |
| `favicon_data` | `TEXT` | | Base64 favicona (fallback cache) |
| `status` | `VARCHAR(20)` | NOT NULL, DEFAULT `'active'` | `active`, `error`, `paused` |
| `last_error` | `TEXT` | | Poslední chybová zpráva |
| `last_fetched_at` | `TIMESTAMPTZ` | | Poslední úspěšný fetch |
| `last_fetch_duration_ms` | `INTEGER` | | Doba trvání posledního fetche |
| `last_published_at` | `TIMESTAMPTZ` | | Datum posledního článku ve feedu |
| `effective_fetch_interval_min` | `SMALLINT` | | ⚠️ Zatím neimplementováno — scheduler počítá efektivní interval inline (GREATEST(feed.fetch_interval_min, app_settings.min_fetch_interval_min)). Denormalizovaný sloupec přidáme až při optimalizaci výkonu scheduleru. |
| `subscriber_count` | `INTEGER` | NOT NULL, DEFAULT `0` | Počet předplatitelů (denorm.) |
| `feed_type` | `VARCHAR(20)` | NOT NULL, DEFAULT `'rss'` | `rss`, `youtube`, `scrape`, `twitter`, `podcast` |
| `type_config` | `JSONB` | | Konfigurace specifická pro typ |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Datum přidání do systému |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Poslední změna |

Indexy: `feed_url` (unique partial WHERE `is_private = FALSE`), `status`, `last_fetched_at`

Constraints: CHECK `status IN ('active', 'error', 'paused')`, CHECK `feed_type IN ('rss', 'youtube', 'scrape', 'twitter', 'podcast')`

Poznámka: V MVP je vždy `feed_type = 'rss'`. Ostatní typy přicházejí ve Fázi 3.

---

### Tabulka: `user_feeds`

Per-user předplatné feedu – nastavení, která jsou specifická pro každého uživatele.

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SERIAL` | PK | Primární klíč |
| `user_id` | `INTEGER` | NOT NULL, FK → `users.id` ON DELETE CASCADE | Uživatel |
| `feed_id` | `INTEGER` | NOT NULL, FK → `feeds.id` ON DELETE CASCADE | Feed |
| `folder_id` | `INTEGER` | FK → `folders.id` ON DELETE SET NULL | Složka (NULL = bez složky) |
| `custom_title` | `VARCHAR(255)` | | Přepsaný název uživatelem |
| `description` | `TEXT` | | Vlastní poznámka uživatele |
| `extract_readable` | `BOOLEAN` | NOT NULL, DEFAULT `TRUE` | Extrahovat readable verzi článků z tohoto feedu |
| `ai_enabled` | `BOOLEAN` | NOT NULL, DEFAULT `TRUE` | AI funkce zapnuty pro tento feed (relevantní jen pokud user i globálně zapnuto) |
| `list_content_fields` | `JSONB` | | ⚠️ Plánováno Fáze 7 — NULL = použij `user_settings.list_content_fields`; per-feed override |
| `detail_content_fields` | `JSONB` | | ⚠️ Plánováno Fáze 7 — NULL = použij `user_settings.detail_content_fields`; per-feed override |
| `fetch_interval_min` | `SMALLINT` | | NULL = použij `app_settings.default_fetch_interval_min`; scheduler vynucuje `app_settings.min_fetch_interval_min` inline |
| `unread_count` | `INTEGER` | NOT NULL, DEFAULT `0` | Nepřečtených (denorm.) |
| `purge_after_days` | `SMALLINT` | | NULL = global default |
| `purge_keep_count` | `SMALLINT` | | NULL = global default |
| `position` | `SMALLINT` | NOT NULL, DEFAULT `0` | Pořadí v rámci složky |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Datum přidání feedu uživatelem |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Poslední změna |

Indexy: `(user_id, feed_id)` UNIQUE, `user_id`, `(user_id, folder_id)`, `feed_id`

**Životní cyklus feedu:**
- Uživatel přidá **veřejný feed** → pokud URL v DB neexistuje, vytvoří se `feeds` záznam (`is_private = FALSE`) a spustí se první fetch. Vytvoří se `user_feeds` záznam, `subscriber_count` +1.
- Uživatel přidá **privátní feed (s auth)** → vždy se vytvoří nový `feeds` záznam (`is_private = TRUE`) jen pro tohoto uživatele, `subscriber_count` = 1. Články jsou de facto soukromé – stejná URL může existovat vícekrát jako různé privátní instance pro různé uživatele.
- Uživatel odebere feed → smaže se `user_feeds` záznam, `feeds.subscriber_count` se sníží o 1. Nehvězdičkované a nearchivované `user_article_states` pro daného uživatele se smažou. Hvězdičkované/archivované stavy zůstanou (článek je stále v DB).
- `subscriber_count = 0` → background job aplikační logikou smaže články bez hvězdičky/archivu, pak smaže feed. Zbývající hvězdičkované/archivované články dostanou `feed_id = NULL` (ON DELETE SET NULL) a uživatel je nadále vidí ve Hvězdičkovaných/Archivovaných.

**Invarianty – kdy se fyzicky maže článek:**
- Článek s `feed_id IS NOT NULL` → maže se pouze cascade při smazání feedu (pokud nemá hvězdičku/archiv)
- Článek s `feed_id IS NULL` → cleanup job ho smaže pokud neexistuje žádný `user_article_states` s `is_starred = TRUE OR is_archived = TRUE`
- Článek s `feed_id IS NULL` a hvězdičkou/archivem → zůstává v DB dokud ho uživatel ručně nesmaže nebo neodstraní hvězdičku/archiv

---

### Tabulka: `articles`

Sdílené články – každý článek existuje v DB jednou, bez ohledu na počet předplatitelů feedu.

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `BIGSERIAL` | PK | Primární klíč |
| `feed_id` | `INTEGER` | NULLABLE, FK → `feeds.id` ON DELETE SET NULL | Zdrojový feed (NULL = feed smazán, článek zachován) |
| `guid` | `VARCHAR(2048)` | NOT NULL | Unikátní ID z feedu |
| `guid_hash` | `CHAR(64)` | NOT NULL | SHA-256 hash guid (pro rychlé lookup) |
| `url` | `VARCHAR(2048)` | | URL článku |
| `title` | `VARCHAR(1000)` | NOT NULL | Titulek |
| `author` | `VARCHAR(255)` | | Autor |
| `content` | `TEXT` | | Obsah (HTML) |
| `content_source` | `VARCHAR(20)` | | `feed_full`, `feed_summary`, `readable` |
| `readable_content` | `TEXT` | | Readable extrakce (zachována zvlášť) |
| `readable_status` | `VARCHAR(10)` | NOT NULL, DEFAULT `'skipped'` | `pending`, `success`, `failed`, `skipped` |
| `readable_retries` | `SMALLINT` | NOT NULL, DEFAULT `0` | Počet pokusů o extrakci |
| `readable_next_retry_at` | `TIMESTAMPTZ` | | Kdy zkusit znovu (exponenciální backoff) |
| `summary` | `TEXT` | | Perex (pro zobrazení v seznamu) |
| `published_at` | `TIMESTAMPTZ` | | Datum publikace z feedu |
| `fetched_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Datum stažení |
| `estimated_read_min` | `SMALLINT` | | Odhad doby čtení v minutách |
| `word_count` | `INTEGER` | | Počet slov |
| `image_url` | `VARCHAR(2048)` | | Hlavní obrázek článku |
| `ai_summary` | `TEXT` | | AI shrnutí (Fáze 2) |
| `ai_score` | `REAL` | | AI relevance skóre 0–1 (Fáze 2) |
| `ai_tags_suggested` | `TEXT[]` | | Navrhnuté AI štítky (Fáze 2) |
| `ai_processed_at` | `TIMESTAMPTZ` | | Kdy zpracováno AI (Fáze 2) |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Interní datum vložení |

Indexy:
- `(feed_id, guid_hash)` UNIQUE (partial WHERE `feed_id IS NOT NULL`) – deduplikace
- `(feed_id, published_at DESC)` – per-feed výpis
- Full-text: GIN index nad `to_tsvector('simple', unaccent(title) || ' ' || unaccent(COALESCE(content, '')))`

---

### Tabulka: `user_article_states`

Per-user stav článku – vzniká lazy (až uživatel s článkem nějak interaguje, nebo při prvním načtení feedu).

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `user_id` | `INTEGER` | NOT NULL, FK → `users.id` ON DELETE CASCADE | Uživatel |
| `article_id` | `BIGINT` | NOT NULL, FK → `articles.id` ON DELETE CASCADE | Článek |
| `is_read` | `BOOLEAN` | NOT NULL, DEFAULT `FALSE` | Přečtený |
| `is_starred` | `BOOLEAN` | NOT NULL, DEFAULT `FALSE` | Hvězdičkovaný |
| `is_archived` | `BOOLEAN` | NOT NULL, DEFAULT `FALSE` | Archivovaný |
| `is_hidden` | `BOOLEAN` | NOT NULL, DEFAULT `FALSE` | Skrytý filtrem |
| `read_at` | `TIMESTAMPTZ` | | Kdy byl označen přečtený |
| `share_token` | `VARCHAR(32)` | UNIQUE | Token pro sdílení odkazu |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Datum vytvoření záznamu |

PK: `(user_id, article_id)`

Indexy:
- `(user_id, is_read, article_id)` – hlavní výpis nepřečtených
- `(user_id, is_starred)` – hvězdičkované
- `(user_id, is_archived)` – archivované
- `share_token` UNIQUE (partial WHERE NOT NULL)

Poznámka: Pro dotazy „všechny články feedu pro uživatele" je nutný JOIN `articles` → `user_article_states` (LEFT JOIN, protože stav nemusí existovat). Absence záznamu = článek je nepřečtený.

---

### Tabulka: `labels`

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SERIAL` | PK | Primární klíč |
| `user_id` | `INTEGER` | NOT NULL, FK → `users.id` ON DELETE CASCADE | Vlastník |
| `name` | `VARCHAR(100)` | NOT NULL | Název štítku |
| `color` | `CHAR(7)` | NOT NULL, DEFAULT `'#6366f1'` | Hex barva (#RRGGBB) |
| `position` | `SMALLINT` | NOT NULL, DEFAULT `0` | Pořadí v levém panelu |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Datum vytvoření |

Indexy: `(user_id, name)` UNIQUE

---

### Tabulka: `article_labels`

Vazební tabulka M:N mezi `articles` a `labels`. Štítky jsou per-user, proto je `user_id` součástí PK.

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `user_id` | `INTEGER` | NOT NULL, FK → `users.id` ON DELETE CASCADE | Uživatel |
| `article_id` | `BIGINT` | NOT NULL, FK → `articles.id` ON DELETE CASCADE | Článek |
| `label_id` | `INTEGER` | NOT NULL, FK → `labels.id` ON DELETE CASCADE | Štítek |
| `assigned_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Kdy byl štítek přiřazen |
| `assigned_by_filter` | `BOOLEAN` | NOT NULL, DEFAULT `FALSE` | True = přiřazeno filtrem automaticky |

PK: `(user_id, article_id, label_id)`

Indexy: `(user_id, label_id)`, `article_id`

---

### Tabulka: `filters`

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SERIAL` | PK | Primární klíč |
| `user_id` | `INTEGER` | NOT NULL, FK → `users.id` ON DELETE CASCADE | Vlastník |
| `name` | `VARCHAR(100)` | NOT NULL | Název filtru |
| `is_active` | `BOOLEAN` | NOT NULL, DEFAULT `TRUE` | Aktivní/neaktivní |
| `match_operator` | `VARCHAR(5)` | NOT NULL, DEFAULT `'AND'` | `AND`, `OR` |
| `position` | `SMALLINT` | NOT NULL, DEFAULT `0` | Priorita (pořadí aplikace) |
| `stop_on_match` | `BOOLEAN` | NOT NULL, DEFAULT `FALSE` | Zastavit aplikaci dalších filtrů |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Datum vytvoření |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Poslední změna |

Indexy: `user_id`, `(user_id, is_active, position)`

---

### Tabulka: `filter_conditions`

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SERIAL` | PK | Primární klíč |
| `filter_id` | `INTEGER` | NOT NULL, FK → `filters.id` ON DELETE CASCADE | Nadřazený filtr |
| `field` | `VARCHAR(30)` | NOT NULL | `title`, `content`, `author`, `url`, `feed_id`, `folder_id`, `published_at` |
| `operator` | `VARCHAR(20)` | NOT NULL | `contains`, `not_contains`, `equals`, `regex`, `gt`, `lt` |
| `value` | `TEXT` | NOT NULL | Hodnota podmínky |
| `position` | `SMALLINT` | NOT NULL, DEFAULT `0` | Pořadí podmínky |

Indexy: `filter_id`

---

### Tabulka: `filter_actions`

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SERIAL` | PK | Primární klíč |
| `filter_id` | `INTEGER` | NOT NULL, FK → `filters.id` ON DELETE CASCADE | Nadřazený filtr |
| `action_type` | `VARCHAR(30)` | NOT NULL | `label`, `mark_read`, `star`, `hide`, `notify` |
| `action_value` | `TEXT` | | Parametr akce (např. label_id pro `label`) |

Indexy: `filter_id`

---

### Tabulka: `fetch_logs`

Logujeme pouze chyby – úspěšné fetche se nezaznamenávají, poslední stav je v `feeds`.

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `BIGSERIAL` | PK | Primární klíč |
| `feed_id` | `INTEGER` | NOT NULL, FK → `feeds.id` ON DELETE CASCADE | Feed |
| `failed_at` | `TIMESTAMPTZ` | NOT NULL | Kdy chyba nastala |
| `http_status` | `SMALLINT` | | HTTP status odpovědi (NULL pokud chyba před HTTP) |
| `error_message` | `TEXT` | NOT NULL | Chybová zpráva |

Indexy: `(feed_id, failed_at DESC)`

Poznámka: Záznamy starší 30 dní se automaticky mažou (purge job). Aktuální stav feedu (poslední fetch, poslední chyba, duration) je vždy v tabulce `feeds`.

---

### Tabulka: `ai_profiles` (Fáze 2 – připravena v DB od Fáze 1)

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SERIAL` | PK | Primární klíč |
| `name` | `VARCHAR(100)` | NOT NULL | Název profilu |
| `purpose` | `VARCHAR(30)` | NOT NULL | `summary`, `translation`, `scoring`, `categorization`, `tts` |
| `provider` | `VARCHAR(30)` | NOT NULL | `anthropic`, `openai`, `gemini` |
| `model` | `VARCHAR(100)` | NOT NULL | Název modelu (např. `claude-sonnet-4-6`) |
| `api_key_encrypted` | `TEXT` | | Globální API klíč (šifrovaný) |
| `max_tokens` | `INTEGER` | DEFAULT `1000` | Max tokeny |
| `summary_language` | `VARCHAR(10)` | DEFAULT `'cs'` | Jazyk výstupu |
| `is_active` | `BOOLEAN` | DEFAULT `FALSE` | Aktivní profil |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Datum vytvoření |

---

### Tabulka: `user_ai_keys` (Fáze 2 – připravena od Fáze 1)

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `user_id` | `INTEGER` | NOT NULL, FK → `users.id` ON DELETE CASCADE | Uživatel |
| `provider` | `VARCHAR(30)` | NOT NULL | `anthropic`, `openai`, `gemini` |
| `api_key_encrypted` | `TEXT` | NOT NULL | Šifrovaný API klíč |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Poslední aktualizace |

Indexy: `(user_id, provider)` PK

---

### Tabulka: `audit_log`

Append-only log admin akcí. Záznamy se nemažou.

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `BIGSERIAL` | PK | Primární klíč |
| `admin_id` | `INTEGER` | NOT NULL, FK → `users.id` ON DELETE RESTRICT | Admin který akci provedl |
| `action` | `VARCHAR(50)` | NOT NULL | `user_reset_password`, `user_activate`, `user_deactivate`, `invitation_create`, `invitation_revoke`, `app_settings_update`, `ai_profile_create`, `ai_profile_delete` |
| `target_type` | `VARCHAR(30)` | | `user`, `invitation`, `app_settings`, `ai_profile` |
| `target_id` | `INTEGER` | | ID cílového záznamu |
| `detail` | `JSONB` | | Doplňující kontext (např. změněná pole) |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Kdy akce proběhla |

Indexy: `(admin_id, created_at DESC)`, `(target_type, target_id)`, `created_at DESC`

Zobrazeno v admin panelu na `/admin/audit-log`.

---

### Shrnutí relací

```
users (1) ──< user_feeds (N)
users (1) ──< folders (N)
users (1) ──< labels (N)
users (1) ──< filters (N)
users (1) ── user_settings (1)
folders (1) ──< user_feeds (N)
feeds (1) ──< user_feeds (N)          sdílený feed ← předplatitelé
feeds (1) ──< articles (N)
articles (M) ──>< labels (N)          přes article_labels (user_id, article_id, label_id)
articles (1) ──< user_article_states  per-user stav článku
filters (1) ──< filter_conditions (N)
filters (1) ──< filter_actions (N)
feeds (1) ──< fetch_logs (N)          pouze chybové záznamy
users (1) ──< audit_log (N)           admin akce (append-only)
```

---

## 2. Struktura projektu

```
filtread/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── .env                            # gitignore
├── README.md
│
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/
│   │
│   └── app/
│       ├── main.py                 # FastAPI app factory, lifespan, middleware
│       ├── config.py               # Pydantic Settings (načítá .env)
│       ├── database.py             # SQLAlchemy engine, session factory, Base
│       │
│       ├── models/
│       │   ├── __init__.py
│       │   ├── user.py             # User, UserSettings
│       │   ├── auth.py             # ApiToken, PasswordResetToken, Invitation
│       │   ├── feed.py             # Feed, Folder
│       │   ├── article.py          # Article
│       │   ├── label.py            # Label, ArticleLabel
│       │   ├── filter.py           # Filter, FilterCondition, FilterAction
│       │   ├── fetch_log.py        # FetchLog
│       │   ├── settings.py         # AppSettings
│       │   └── ai.py               # AiProfile, UserAiKey (Fáze 2)
│       │
│       ├── schemas/                # Pydantic schémata (request/response)
│       │   ├── __init__.py
│       │   ├── user.py
│       │   ├── auth.py
│       │   ├── feed.py
│       │   ├── article.py
│       │   ├── label.py
│       │   ├── filter.py
│       │   └── settings.py
│       │
│       ├── routers/
│       │   ├── web/                # HTML routery (Jinja2 + HTMX)
│       │   │   ├── auth.py         # /login, /logout, /register, /reset-password
│       │   │   ├── feeds.py
│       │   │   ├── articles.py     # stránky + HTMX fragmenty
│       │   │   ├── labels.py
│       │   │   ├── filters.py
│       │   │   ├── settings.py
│       │   │   └── admin.py
│       │   │
│       │   └── api/
│       │       └── v1/             # REST API (JSON)
│       │           ├── auth.py
│       │           ├── feeds.py
│       │           ├── articles.py
│       │           ├── labels.py
│       │           └── filters.py
│       │
│       ├── services/               # Business logika
│       │   ├── auth_service.py
│       │   ├── feed_service.py
│       │   ├── article_service.py
│       │   ├── filter_service.py
│       │   ├── label_service.py
│       │   ├── search_service.py
│       │   ├── email_service.py
│       │   ├── readable_service.py
│       │   └── ai_service.py       # Stub pro Fázi 2
│       │
│       ├── fetcher/                # RSS fetching engine
│       │   ├── scheduler.py        # APScheduler setup
│       │   ├── feed_fetcher.py     # HTTP stažení + parsování
│       │   ├── article_processor.py
│       │   └── favicon_fetcher.py
│       │
│       ├── auth/
│       │   ├── session.py
│       │   ├── jwt_handler.py
│       │   └── dependencies.py     # get_current_user, require_admin
│       │
│       ├── templates/              # Jinja2 šablony
│       │   ├── base.html
│       │   ├── auth/
│       │   │   ├── login.html
│       │   │   ├── register.html
│       │   │   └── reset_password.html
│       │   ├── app/
│       │   │   ├── main.html       # 3-panel layout
│       │   │   └── partials/       # HTMX fragmenty
│       │   │       ├── article_list.html
│       │   │       ├── article_detail.html
│       │   │       ├── sidebar.html
│       │   │       ├── feed_item.html
│       │   │       └── toast.html
│       │   ├── settings/
│       │   │   ├── profile.html
│       │   │   ├── feeds.html
│       │   │   ├── labels.html
│       │   │   └── filters.html
│       │   └── admin/
│       │       ├── dashboard.html
│       │       ├── users.html
│       │       └── settings.html
│       │
│       └── static/
│           ├── css/app.css         # Tailwind build output
│           ├── js/
│           │   ├── htmx.min.js
│           │   └── app.js          # Alpine.js nebo vanilla JS
│           └── icons/
│
└── frontend/                       # Tailwind build
    ├── package.json
    ├── tailwind.config.js
    └── src/app.css
```

---

## 3. API endpointy

### Konvence

- Webové routy vrací HTML (Jinja2 nebo HTMX partial).
- API routy pod `/api/v1/` vrací JSON, vyžadují Bearer token nebo session.
- Chybové odpovědi: `{"error": "...", "detail": "..."}` s příslušným HTTP kódem.
- Paginace: `?page=1&per_page=50`

---

### AUTH – webové routy

| Metoda | Cesta | Popis |
|---|---|---|
| `GET` | `/login` | Přihlašovací stránka |
| `POST` | `/login` | Zpracování přihlášení, nastaví session cookie |
| `POST` | `/logout` | Odhlášení |
| `GET` | `/register` | Registrační stránka |
| `POST` | `/register` | Zpracování registrace |
| `GET` | `/register/{token}` | Registrace přes pozvánkový link |
| `POST` | `/register/{token}` | Zpracování registrace přes pozvánku |
| `GET` | `/forgot-password` | Stránka pro reset hesla |
| `POST` | `/forgot-password` | Odeslání reset emailu |
| `GET` | `/reset-password/{token}` | Formulář nového hesla |
| `POST` | `/reset-password/{token}` | Nastavení nového hesla |

---

### HTMX fragmenty

| Metoda | Cesta | Popis |
|---|---|---|
| `GET` | `/htmx/sidebar` | Levý panel (složky, kanály, štítky) |
| `GET` | `/htmx/articles` | Seznam článků (`?feed=`, `?folder=`, `?label=`, `?view=all\|starred\|archived`) |
| `GET` | `/htmx/articles/{id}` | Detail článku |
| `POST` | `/htmx/articles/{id}/read` | Toggle přečteno |
| `POST` | `/htmx/articles/{id}/star` | Toggle hvězdičky |
| `POST` | `/htmx/articles/{id}/archive` | Toggle archivace |
| `POST` | `/htmx/articles/mark-read-bulk` | Označit vybrané jako přečtené |
| `POST` | `/htmx/feeds/{id}/refresh` | Ruční refresh feedu |
| `POST` | `/htmx/feeds/{id}/mark-read` | Označit celý feed jako přečtený |
| `POST` | `/htmx/folders/{id}/mark-read` | Označit celou složku jako přečtenou |
| `POST` | `/htmx/all/mark-read` | Označit vše jako přečtené |
| `GET` | `/htmx/articles/search` | Full-text vyhledávání (`?q=...`) |
| `POST` | `/htmx/articles/{id}/share` | Vygenerování share_token (128 bit náhodný) |
| `DELETE` | `/htmx/articles/{id}/share` | Zrušení share_token |
| `GET` | `/share/{token}` | Veřejné zobrazení sdíleného článku ⚠️ rate limit |

---

### NASTAVENÍ – webové routy

| Metoda | Cesta | Popis |
|---|---|---|
| `GET/POST` | `/settings/profile` | Profil uživatele |
| `GET` | `/settings/tokens` | Správa API tokenů |
| `POST` | `/settings/tokens` | Vytvoření nového tokenu |
| `DELETE` | `/settings/tokens/{id}` | Odvolání tokenu |
| `GET` | `/settings/feeds` | Správa kanálů |
| `POST` | `/settings/feeds` | Přidání kanálu |
| `GET/POST` | `/settings/feeds/{id}/edit` | Editace kanálu |
| `DELETE` | `/settings/feeds/{id}` | Smazání kanálu |
| `GET/POST` | `/settings/folders` | Správa složek |
| `POST/DELETE` | `/settings/folders/{id}` | Aktualizace/smazání složky |
| `GET/POST` | `/settings/labels` | Správa štítků |
| `POST/DELETE` | `/settings/labels/{id}` | Aktualizace/smazání štítku |
| `GET/POST` | `/settings/filters` | Správa filtrů |
| `GET/POST` | `/settings/filters/{id}/edit` | Editace filtru |
| `DELETE` | `/settings/filters/{id}` | Smazání filtru |
| `POST` | `/settings/filters/{id}/test` | Test filtru |
| `POST` | `/settings/filters/{id}/apply` | Zpětná aplikace filtru |
| `GET` | `/settings/opml/export` | Export OPML |
| `POST` | `/settings/opml/import` | Import OPML |
| `GET/POST` | `/settings/appearance` | Nastavení zobrazení |

---

### ADMIN – webové routy

| Metoda | Cesta | Popis |
|---|---|---|
| `GET` | `/admin` | Dashboard |
| `GET/POST` | `/admin/users` | Seznam / vytvoření uživatele |
| `POST` | `/admin/users/{id}/activate` | Aktivace/deaktivace uživatele |
| `POST` | `/admin/users/{id}/reset-password` | Admin reset hesla |
| `GET/POST` | `/admin/invitations` | Seznam / vygenerování pozvánky |
| `DELETE` | `/admin/invitations/{id}` | Zrušení pozvánky |
| `GET/POST` | `/admin/settings` | Globální nastavení |
| `POST` | `/admin/settings/smtp-test` | Test SMTP |
| `GET` | `/admin/fetch-logs` | Přehled logů fetchování |
| `GET` | `/admin/audit-log` | Audit log admin akcí |
| `GET` | `/admin/ai-profiles` | Správa AI profilů (stub, Fáze 2) |

---

### REST API v1 – JSON *(plánováno po MVP)*

Auth: `Authorization: Bearer <token>` nebo platná session cookie.

#### Auth

| Metoda | Cesta | Popis |
|---|---|---|
| `POST` | `/api/v1/auth/login` | Přihlášení, vrací JWT |
| `POST` | `/api/v1/auth/logout` | Odhlášení |
| `GET` | `/api/v1/auth/me` | Info o přihlášeném uživateli |
| `POST` | `/api/v1/auth/tokens` | Vytvoření API tokenu |
| `GET` | `/api/v1/auth/tokens` | Seznam API tokenů |
| `DELETE` | `/api/v1/auth/tokens/{id}` | Odvolání tokenu |

#### Feeds

| Metoda | Cesta | Popis |
|---|---|---|
| `GET` | `/api/v1/feeds` | Seznam feedů |
| `POST` | `/api/v1/feeds` | Přidání feedu |
| `GET` | `/api/v1/feeds/{id}` | Detail feedu |
| `PATCH` | `/api/v1/feeds/{id}` | Aktualizace feedu |
| `DELETE` | `/api/v1/feeds/{id}` | Smazání feedu |
| `POST` | `/api/v1/feeds/{id}/refresh` | Ruční refresh |
| `GET` | `/api/v1/feeds/detect` | Detekce RSS URL z URL webu |
| `GET` | `/api/v1/folders` | Seznam složek |
| `POST` | `/api/v1/folders` | Vytvoření složky |
| `PATCH` | `/api/v1/folders/{id}` | Aktualizace složky |
| `DELETE` | `/api/v1/folders/{id}` | Smazání složky |
| `GET` | `/api/v1/opml` | Export OPML |
| `POST` | `/api/v1/opml` | Import OPML |

#### Articles

| Metoda | Cesta | Popis |
|---|---|---|
| `GET` | `/api/v1/articles` | Seznam článků (`?feed_id=`, `?folder_id=`, `?label_id=`, `?unread=`, `?sort=`, `?page=`) |
| `GET` | `/api/v1/articles/{id}` | Detail článku |
| `PATCH` | `/api/v1/articles/{id}` | Aktualizace stavu (is_read, is_starred, is_archived) |
| `POST` | `/api/v1/articles/mark-read` | Hromadné označení přečtených |
| `GET` | `/api/v1/articles/search` | Full-text vyhledávání (`?q=`) |

#### Labels

| Metoda | Cesta | Popis |
|---|---|---|
| `GET` | `/api/v1/labels` | Seznam štítků |
| `POST` | `/api/v1/labels` | Vytvoření štítku |
| `PATCH` | `/api/v1/labels/{id}` | Aktualizace štítku |
| `DELETE` | `/api/v1/labels/{id}` | Smazání štítku |
| `POST` | `/api/v1/articles/{id}/labels` | Přiřazení štítku článku |
| `DELETE` | `/api/v1/articles/{id}/labels/{label_id}` | Odebrání štítku |

#### Filters

| Metoda | Cesta | Popis |
|---|---|---|
| `GET` | `/api/v1/filters` | Seznam filtrů |
| `POST` | `/api/v1/filters` | Vytvoření filtru |
| `GET` | `/api/v1/filters/{id}` | Detail filtru |
| `PATCH` | `/api/v1/filters/{id}` | Aktualizace filtru |
| `DELETE` | `/api/v1/filters/{id}` | Smazání filtru |
| `POST` | `/api/v1/filters/{id}/test` | Test filtru |
| `POST` | `/api/v1/filters/{id}/apply` | Zpětná aplikace filtru |

---

## 4. Klíčová architektonická rozhodnutí

### Autentizace (dual-mode)

- **Session cookies** pro webový frontend: HttpOnly, Secure, SameSite=Lax; session data v PostgreSQL.
- **JWT Bearer tokeny** pro REST API: krátká platnost (15 min) + refresh token.
- **API tokeny** (z tabulky `api_tokens`): alternativa pro mobilní klienty.
- FastAPI dependency `get_current_user` automaticky detekuje obě varianty.

### APScheduler

Běží přímo v FastAPI procesu. Jobs:
- `fetch_all_feeds` – každých N minut
- `purge_old_articles` – jednou denně
- `cleanup_fetch_logs` – jednou týdně (chybové záznamy starší 30 dní)
- `cleanup_expired_tokens` – denně

**MVP = 1 worker.** Uvicorn se spouští s `--workers 1` – zajištěno v `docker-compose.yml` napevno v command. Díky tomu existuje právě jedna instance scheduleru a joby neběží duplicitně.

FastAPI je async, takže 1 worker zvládne self-hosted provoz s desítkami uživatelů bez problémů.

Do budoucna při škálování: přesunout scheduler do separátního containeru, nebo použít `PersistentJobStore` (PostgreSQL/Redis) s leader election.

### Deduplikace článků

Při importu: `guid_hash` (SHA-256 z guid nebo URL) se porovná s existujícími záznamy pro daný feed. Pokud existuje, zkontroluje se změna obsahu a případně se aktualizuje – nikdy se nevytváří duplikát.

### Readable extrakce

`readable_service.py` waterfall:
1. `trafilatura` (primární)
2. `readability-lxml` (fallback)
3. Raw content z feedu (fallback)

Výsledek do `articles.readable_content`. UI přepínač volí, který sloupec zobrazit.

**Stavový model (`readable_status`):**
- `pending` – zařazeno k extrakci, ještě neproběhlo
- `success` – extrakce OK, `readable_content` vyplněno
- `failed` – extrakce selhala po max. pokusech
- `skipped` – přeskočeno záměrně (krátký obsah, vypnuto per feed, chybí URL)

**Retry policy:**
- Max 3 pokusy, exponenciální backoff: 1. retry po 5 min, 2. po 15 min, 3. po 60 min
- Po 3. selhání → `readable_status = failed`, žádné další pokusy

**UX při čekání na extrakci:**
- `pending` → indikátor „načítám fulltext...", HTMX polling každé 2s
- `success` → polling se zastaví, zobrazí se readable obsah
- `failed` → zobrazí se „Fulltext nedostupný", původní obsah z feedu
- `skipped` → rovnou zobrazit původní obsah, žádný polling

**Rozhodovací logika – extrakce se přeskočí pokud:**
- `feeds.extract_readable = FALSE` – uživatel vypnul per kanál
- Článek nemá URL – nemáme co stahovat
- `word_count > 500` – feed pravděpodobně posílá plný obsah (hranice konfigurovatelná)
- Článek po aplikaci filtrů nedostal hvězdičku ani štítek – nezajímavý článek, šetříme DB a cílový web

**Pořadí operací v `article_processor.py`:**
1. Ulož článek (bez readable)
2. Aplikuj filtry
3. Pokud článek dostal hvězdičku nebo štítek → spusť readable extrakci
4. Jinak → přeskoč

**Automatická detekce plného obsahu:** pokud prvních N článků kanálu má `word_count > 500`, nastaví se `feeds.extract_readable = FALSE` automaticky.

### Konvence pro DB dotazy

- V čistém SQL vždy vypisovat konkrétní sloupce, nikdy `SELECT *`
- V SQLAlchemy ORM výstup omezuje Pydantic schema – do API odejde jen co je v response modelu
- Složité dotazy (agregace, hromadné akce) psát jako čisté SQL přes `db.execute(text(...))`
- N+1 problém řešit pomocí `joinedload()` nebo `selectinload()` při načítání relationships

### Lokalizace (i18n)

Překlady přes GNU gettext + `Babel`. Texty v Jinja2 šablonách obalené `_()`, překladové soubory v `backend/app/locales/{lang}/LC_MESSAGES/`.

- **MVP**: pouze angličtina (`en`), další překlady se nedodávají
- **Do budoucna**: čeština (`cs`) a případně další jazyky; sloupec `user_settings.language` je připraven
- Výchozí jazyk aplikace: `en`

### Full-text search

PostgreSQL nativní full-text search přes `tsvector` + GIN index. Nulová externí závislost. Transparentní upgrade na Elasticsearch/Meilisearch do budoucna.

Konfigurace: `simple` (žádný stemming) + `unaccent` extension pro ignorování diakritiky. Hledání probíhá přes přesné tvary slov – uživatel musí zadat správný tvar (např. „programování", ne „programovat"). Plný český stemming je možné doplnit později vlastním slovníkem nebo `pg_trgm` trigram indexem.

Migrace musí obsahovat: `CREATE EXTENSION IF NOT EXISTS unaccent;`

### DB triggery pro `updated_at`

PostgreSQL neaktualizuje `updated_at` automaticky – je potřeba trigger. Implementace v první Alembic migraci přes `op.execute()`:

```sql
-- Jedna sdílená trigger function
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger na každou tabulku s updated_at
CREATE TRIGGER trg_updated_at BEFORE UPDATE ON users
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_updated_at BEFORE UPDATE ON feeds
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_updated_at BEFORE UPDATE ON user_feeds
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_updated_at BEFORE UPDATE ON filters
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER trg_updated_at BEFORE UPDATE ON app_settings
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

V Alembic downgrade: `DROP TRIGGER` + `DROP FUNCTION`.

---

### Příprava na Fázi 2 (AI)

- Tabulky `ai_profiles` a `user_ai_keys` vytvořeny v první DB migraci.
- AI sloupce v `articles` přítomny od začátku (nullable).
- `ai_service.py` jako stub s definovaným interface.
- `app_settings.ai_enabled = FALSE` přepínač.

---

## 5. Security

### XSS ochrana

RSS obsah pochází z externích zdrojů – bez sanitizace je persistent XSS snadný útok.

**Sanitizace obsahu (při uložení do DB):**
- Knihovna `nh3` (Rust-based, rychlejší než `bleach`) – allowlist tagů
- Povolené tagy: `p, br, h1, h2, h3, h4, h5, h6, a, img, ul, ol, li, blockquote, code, pre, strong, em, figure, figcaption`
- Povolené atributy: `href` (pouze `http/https`), `src` (pouze `http/https`), `alt`, `title`
- Sanitizace se provádí v `readable_service.py` i při parsování feedu před uložením `content`
- V Jinja2 šablonách používat `|safe` **pouze** na již sanitizovaný obsah – nikdy na raw data

**CSP hlavičky (druhá vrstva obrany):**
```
Content-Security-Policy: default-src 'self'; script-src 'self'; img-src * data:; style-src 'self' 'unsafe-inline';
```
Nastavit jako middleware v `main.py` pro všechny responses.

---

### CSRF ochrana

Web používá session cookies → zranitelný na CSRF. Ochrana je povinná pro všechny POST/PUT/PATCH/DELETE endpointy včetně HTMX fragmentů.

Implementace:
- Middleware `starlette-csrf` (nebo vlastní) generuje CSRF token, ukládá do cookie
- Každý mutující request musí přiložit token v headeru `X-CSRFToken`
- V base Jinja2 šabloně globálně nastavit HTMX header: `hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'`
- Klasické HTML formuláře dostanou hidden input `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`

### Secret management

Dva klíče v `.env`, každý s jiným dopadem při rotaci:

**`SECRET_KEY`** – podepisuje session cookies.
- Rotace → všechny aktivní sessions okamžitě neplatné, uživatelé se musí znovu přihlásit.
- API tokeny (SHA-256 hash) nejsou ovlivněny.
- Postup rotace: nastavit nový `SECRET_KEY` v `.env` → restartovat app. Hotovo.
- Zero-downtime varianta (volitelně): přidat `SECRET_KEY_OLD` a validovat session nejdříve novým, pak starým klíčem po dobu přechodu (např. 24h).

**`ENCRYPTION_KEY`** – šifruje citlivá data v DB: `smtp_password_encrypted`, `fetch_auth_pass_encrypted`, `user_ai_keys.api_key_encrypted`.
- Rotace bez migrace → všechna zašifrovaná data nečitelná.
- Postup rotace:
  1. Nastavit `ENCRYPTION_KEY_NEW` v `.env` vedle stávajícího `ENCRYPTION_KEY`
  2. Spustit migration script: pro každý zašifrovaný záznam → dešifrovat starým klíčem → zašifrovat novým → uložit (v DB transakci)
  3. Přejmenovat `ENCRYPTION_KEY_NEW` → `ENCRYPTION_KEY`, odstranit starý
  4. Restartovat app
- Dotčené tabulky: `app_settings`, `feeds`, `user_ai_keys`

Pro MVP se rotační script neimplementuje – vytvoří se až při první potřebě rotace. Klíče se generují jednou při nasazení a uchovávají v zabezpečeném `.env` (mimo repozitář).

---

### CORS

MVP nepovoluje CORS – web běží na stejné doméně, HTMX nepotřebuje cross-origin přístup. `CORSMiddleware` se nepřidává.

Nastavit až při implementaci REST API (po MVP) – povolit pouze explicitně uvedené origins přes `.env`.

---

### Rate limiting

Knihovna `slowapi` (dekorátor na endpoint, in-memory store – dostačující pro 1 worker).

| Endpoint | Limit |
|---|---|
| `POST /login` | 5 / minuta per IP |
| `POST /reset-password` | 2 / hodina per IP |
| `POST /register` | 3 / hodina per IP |
| `GET /share/{token}` | 20 / minuta per IP |
| `POST /settings/tokens` | 5 / hodina per user |

Při překročení limitu vrátit HTTP 429. Limity jsou konfigurovatelné přes `.env`.

---

## 6. Docker nasazení

```yaml
# docker-compose.yml obsahuje:
services:
  app:    # FastAPI + APScheduler (gunicorn + uvicorn workers), port 8000
  db:     # PostgreSQL 16 (persistent volume)
  # redis: vynecháno v Fázi 1

volumes:
  postgres_data:
  app_static:       # pro nginx serve statiky
```

Klíčové env proměnné (`.env`):
```
DATABASE_URL
SECRET_KEY           # JWT + session podpis
ENCRYPTION_KEY       # šifrování API klíčů a hesel v DB
ALLOWED_HOSTS
DEBUG
FIRST_ADMIN_EMAIL    # inicializační seed
FIRST_ADMIN_PASSWORD
```

### Zálohy PostgreSQL

MVP (lokální vývoj) – zálohy se neřeší.

TODO při nasazení na VPS (fáze 6–7):
- Týdenní `pg_dump` + gzip, ukládat na externí úložiště (S3 / Backblaze B2)
- Retention: poslední 4 týdenní zálohy
- Před nasazením otestovat restore postup

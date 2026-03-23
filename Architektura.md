# Filtread – Architektura aplikace (Fáze 1 MVP + příprava na Fázi 2)

Datum: 23. 3. 2026
Stav: Návrh architektury

---

## 1. DB schéma (PostgreSQL)

### Principy návrhu

- Kanály jsou izolované per uživatel – žádné sdílení feedů mezi uživateli.
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
| `show_unread_only` | `BOOLEAN` | DEFAULT `TRUE` | Výchozí filtr nepřečtených |
| `default_sort_order` | `VARCHAR(10)` | DEFAULT `'newest'` | `newest`, `oldest` |
| `left_panel_pinned` | `BOOLEAN` | DEFAULT `TRUE` | Levý panel připnutý/overlay |
| `articles_per_page` | `SMALLINT` | DEFAULT `50` | Počet článků na stránku |
| `timezone` | `VARCHAR(50)` | DEFAULT `'UTC'` | Časová zóna uživatele |
| `language` | `VARCHAR(10)` | DEFAULT `'cs'` | Jazyk UI (`cs`, `en`) |
| `keyboard_shortcuts_enabled` | `BOOLEAN` | DEFAULT `TRUE` | Klávesové zkratky |

---

### Tabulka: `api_tokens`

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SERIAL` | PK | Primární klíč |
| `user_id` | `INTEGER` | NOT NULL, FK → `users.id` ON DELETE CASCADE | Vlastník tokenu |
| `name` | `VARCHAR(100)` | NOT NULL | Pojmenování (např. "iPhone") |
| `token_hash` | `VARCHAR(255)` | NOT NULL, UNIQUE | bcrypt/SHA-256 hash tokenu |
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
| `default_fetch_interval_min` | `SMALLINT` | NOT NULL, DEFAULT `60` | Interval fetchování v minutách |
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

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `SERIAL` | PK | Primární klíč |
| `user_id` | `INTEGER` | NOT NULL, FK → `users.id` ON DELETE CASCADE | Vlastník |
| `folder_id` | `INTEGER` | FK → `folders.id` ON DELETE SET NULL | Složka (NULL = bez složky) |
| `feed_url` | `VARCHAR(2048)` | NOT NULL | URL RSS/Atom feedu |
| `site_url` | `VARCHAR(2048)` | | URL webu |
| `title` | `VARCHAR(255)` | NOT NULL | Název (z feedu nebo vlastní) |
| `custom_title` | `VARCHAR(255)` | | Přepsaný název uživatelem |
| `description` | `TEXT` | | Vlastní poznámka uživatele |
| `favicon_url` | `VARCHAR(2048)` | | URL favicony |
| `favicon_data` | `TEXT` | | Base64 favicona (fallback cache) |
| `status` | `VARCHAR(20)` | NOT NULL, DEFAULT `'active'` | `active`, `error`, `paused` |
| `last_error` | `TEXT` | | Poslední chybová zpráva |
| `last_fetched_at` | `TIMESTAMPTZ` | | Poslední úspěšný fetch |
| `last_fetch_duration_ms` | `INTEGER` | | Doba trvání posledního fetche |
| `last_published_at` | `TIMESTAMPTZ` | | Datum posledního článku ve feedu |
| `fetch_interval_min` | `SMALLINT` | | NULL = global default |
| `fetch_auth_user` | `VARCHAR(255)` | | HTTP Basic Auth user |
| `fetch_auth_pass_encrypted` | `TEXT` | | HTTP Basic Auth heslo (šifrované) |
| `extract_readable` | `BOOLEAN` | NOT NULL, DEFAULT `TRUE` | Extrahovat readable verzi |
| `article_count` | `INTEGER` | NOT NULL, DEFAULT `0` | Celkový počet článků (denorm.) |
| `unread_count` | `INTEGER` | NOT NULL, DEFAULT `0` | Nepřečtených (denorm.) |
| `purge_after_days` | `SMALLINT` | | NULL = global |
| `purge_keep_count` | `SMALLINT` | | NULL = global |
| `position` | `SMALLINT` | NOT NULL, DEFAULT `0` | Pořadí v rámci složky |
| `feed_type` | `VARCHAR(20)` | NOT NULL, DEFAULT `'rss'` | `rss`, `youtube`, `scrape`, `twitter`, `podcast` |
| `type_config` | `JSONB` | | Konfigurace specifická pro typ (CSS selektor pro scraping, channel_id pro YouTube apod.) |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Datum přidání |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Poslední změna |

Indexy: `user_id`, `(user_id, folder_id)`, `status`, `last_fetched_at`

Constraints: CHECK `status IN ('active', 'error', 'paused')`, CHECK `feed_type IN ('rss', 'youtube', 'scrape', 'twitter', 'podcast')`

Poznámka: V MVP je vždy `feed_type = 'rss'`. Ostatní typy přicházejí ve Fázi 3. `type_config` je JSONB – každý typ má jiná nastavení, není potřeba desítky nullable sloupců.

---

### Tabulka: `articles`

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `BIGSERIAL` | PK | Primární klíč |
| `feed_id` | `INTEGER` | NOT NULL, FK → `feeds.id` ON DELETE CASCADE | Zdrojový feed |
| `user_id` | `INTEGER` | NOT NULL, FK → `users.id` ON DELETE CASCADE | Vlastník (denorm. pro rychlé dotazy) |
| `guid` | `VARCHAR(2048)` | NOT NULL | Unikátní ID z feedu |
| `guid_hash` | `CHAR(64)` | NOT NULL | SHA-256 hash guid (pro rychlé lookup) |
| `url` | `VARCHAR(2048)` | | URL článku |
| `title` | `VARCHAR(1000)` | NOT NULL | Titulek |
| `author` | `VARCHAR(255)` | | Autor |
| `content` | `TEXT` | | Obsah (HTML) |
| `content_source` | `VARCHAR(20)` | | `feed_full`, `feed_summary`, `readable` |
| `readable_content` | `TEXT` | | Readable extrakce (zachována zvlášť) |
| `summary` | `TEXT` | | Perex (pro zobrazení v seznamu) |
| `published_at` | `TIMESTAMPTZ` | | Datum publikace z feedu |
| `fetched_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Datum stažení |
| `is_read` | `BOOLEAN` | NOT NULL, DEFAULT `FALSE` | Přečtený |
| `is_starred` | `BOOLEAN` | NOT NULL, DEFAULT `FALSE` | Hvězdičkovaný |
| `is_archived` | `BOOLEAN` | NOT NULL, DEFAULT `FALSE` | Archivovaný |
| `is_hidden` | `BOOLEAN` | NOT NULL, DEFAULT `FALSE` | Skrytý filtrem |
| `read_at` | `TIMESTAMPTZ` | | Kdy byl označen přečtený |
| `estimated_read_min` | `SMALLINT` | | Odhad doby čtení v minutách |
| `word_count` | `INTEGER` | | Počet slov |
| `image_url` | `VARCHAR(2048)` | | Hlavní obrázek článku |
| `share_token` | `VARCHAR(32)` | UNIQUE | Token pro sdílení odkazu |
| `ai_summary` | `TEXT` | | AI shrnutí (Fáze 2) |
| `ai_score` | `REAL` | | AI relevance skóre 0–1 (Fáze 2) |
| `ai_tags_suggested` | `TEXT[]` | | Navrhnuté AI štítky (Fáze 2) |
| `ai_processed_at` | `TIMESTAMPTZ` | | Kdy zpracováno AI (Fáze 2) |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Interní datum vložení |

Indexy:
- `(feed_id, guid_hash)` UNIQUE – deduplikace
- `(user_id, is_read, published_at DESC)` – hlavní výpis
- `(user_id, is_starred)` – hvězdičkované
- `(user_id, is_archived)` – archivované
- `(feed_id, published_at DESC)` – per-feed výpis
- `share_token` UNIQUE (partial WHERE NOT NULL)
- Full-text: GIN index nad `to_tsvector('simple', title || ' ' || COALESCE(content, ''))`

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

Vazební tabulka M:N mezi `articles` a `labels`.

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `article_id` | `BIGINT` | NOT NULL, FK → `articles.id` ON DELETE CASCADE | Článek |
| `label_id` | `INTEGER` | NOT NULL, FK → `labels.id` ON DELETE CASCADE | Štítek |
| `assigned_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | Kdy byl štítek přiřazen |
| `assigned_by_filter` | `BOOLEAN` | NOT NULL, DEFAULT `FALSE` | True = přiřazeno filtrem automaticky |

Indexy: `(article_id, label_id)` PK, `label_id`, `article_id`

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
| `action_type` | `VARCHAR(30)` | NOT NULL | `add_label`, `mark_read`, `star`, `hide`, `notify` |
| `action_value` | `TEXT` | | Parametr akce (např. label_id pro `add_label`) |

Indexy: `filter_id`

---

### Tabulka: `fetch_logs`

Logujeme pouze chyby – úspěšné fetche se nezaznamenávají, poslední stav je v `feeds`.

| Sloupec | Typ | Constraints | Popis |
|---|---|---|---|
| `id` | `BIGSERIAL` | PK | Primární klíč |
| `feed_id` | `INTEGER` | NOT NULL, FK → `feeds.id` ON DELETE CASCADE | Feed |
| `user_id` | `INTEGER` | NOT NULL | Vlastník feedu (denorm.) |
| `failed_at` | `TIMESTAMPTZ` | NOT NULL | Kdy chyba nastala |
| `http_status` | `SMALLINT` | | HTTP status odpovědi (NULL pokud chyba před HTTP) |
| `error_message` | `TEXT` | NOT NULL | Chybová zpráva |

Indexy: `(feed_id, failed_at DESC)`, `(user_id, failed_at DESC)`

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

### Shrnutí relací

```
users (1) ──< feeds (N)
users (1) ──< folders (N)
users (1) ──< labels (N)
users (1) ──< filters (N)
users (1) ── user_settings (1)
folders (1) ──< feeds (N)
feeds (1) ──< articles (N)
articles (M) ──>< labels (N)     přes article_labels
filters (1) ──< filter_conditions (N)
filters (1) ──< filter_actions (N)
feeds (1) ──< fetch_logs (N)     pouze chybové záznamy
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
| `GET` | `/admin/ai-profiles` | Správa AI profilů (stub, Fáze 2) |

---

### REST API v1 – JSON

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

Při více workerech: APScheduler s `PersistentJobStore` v PostgreSQL, nebo scheduler jen v prvním workeru.

### Deduplikace článků

Při importu: `guid_hash` (SHA-256 z guid nebo URL) se porovná s existujícími záznamy pro daný feed. Pokud existuje, zkontroluje se změna obsahu a případně se aktualizuje – nikdy se nevytváří duplikát.

### Readable extrakce

`readable_service.py` waterfall:
1. `trafilatura` (primární)
2. `readability-lxml` (fallback)
3. Raw content z feedu (fallback)

Výsledek do `articles.readable_content`. UI přepínač volí, který sloupec zobrazit.

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

- **MVP**: pouze čeština (`cs`), anglické překlady se nedodávají
- **Do budoucna**: angličtina (`en`) a případně další jazyky; sloupec `user_settings.language` je připraven
- Výchozí jazyk aplikace: `cs`

### Full-text search

PostgreSQL nativní full-text search přes `tsvector` + GIN index. Nulová externí závislost. Transparentní upgrade na Elasticsearch/Meilisearch do budoucna.

### Příprava na Fázi 2 (AI)

- Tabulky `ai_profiles` a `user_ai_keys` vytvořeny v první DB migraci.
- AI sloupce v `articles` přítomny od začátku (nullable).
- `ai_service.py` jako stub s definovaným interface.
- `app_settings.ai_enabled = FALSE` přepínač.

---

## 5. Docker nasazení

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

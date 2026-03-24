# Filtread – Specifikace aplikace

Datum: 21. 3. 2026  
Stav: Návrh

---

## Přehled projektu

Webová aplikace pro správu RSS kanálů s podporou více uživatelů, filtrování, štítkování a AI shrnutí článků. Cíleno jako self-hosted řešení na VPS, s REST API kompatibilním s existujícími RSS klienty. Do budoucna plánováno jako open-source (vyřešit způsob nasazení a aktualizace).

---

## Fáze 1 – MVP (bez AI)

### Uživatelé a autentizace
- Registrace a přihlášení (email + heslo)
- Role: admin a user
- Každý uživatel má vlastní sadu kanálů, štítků a filtrů
- Více pojmenovaných API tokenů (např. "iPhone", "Android") s možností odvolat jednotlivě
- Správa uživatelů adminem
- Zapomenuté heslo – reset přes email
- Změna hesla a emailu v nastavení uživatele
- Pozvánky – admin vygeneruje pozvánkový link
- Zakázání registrace – přepínač v admin nastavení (pro soukromé instance)

### RSS kanály
- Přidání kanálu přes URL (RSS/Atom)
- Automatická detekce RSS – při zadání URL webu najít RSS odkaz automaticky (z `<link rel="alternate">`)
- Upozornění při přidání duplicitního kanálu
- Automatická detekce názvu a ikony (favicon)
- Přejmenování kanálu – vlastní název místo toho z feedu
- Poznámka ke kanálu – krátký popis pro vlastní orientaci
- Organizace kanálů do složek/kategorií
- Nastavení frekvence stahování (globálně i per kanál)
- Ruční refresh kanálu
- Stav kanálu: aktivní / chyba / pozastavený, datum poslední aktualizace, chybová zpráva
- Statistiky kanálu – počet článků, průměrná frekvence publikování
- OPML import a export
- Podpora autentizovaných feedů (HTTP Basic Auth pro privátní RSS kanály)

### Články
- Ukládání do DB: titulek, URL, obsah, datum, autor, zdroj
- Obsah článku – priorita: `content:encoded` → `description` → readable verze; readable verze uložena při fetchování pokud zapnuto na kanálu
- Stavy článku: nepřečtený / přečtený / hvězdička (fronta k přečtení) / archivovaný (uchovat natrvalo)
- Automatické označení článku jako přečteného po odscrollování (nastavitelné)
- Označit vše jako přečtené – pro kanál, složku nebo celý seznam
- Hromadné akce – označit výběr jako přečtené/nepřečtené, přidat štítek více článkům najednou
- Přepínač přečtené/nepřečtené – globální, v horní liště; default zobrazení pouze nepřečtených
- Řazení nejnovější/nejstarší
- Klávesové zkratky pro navigaci
- Sdílení článku odkazem
- Vyhledávání v článcích
- Deduplikace článků
- Adaptivní stránkování
- Automatické čištění starých článků (purge dle stáří nebo počtu na kanál); hvězdičkované a archivované články vyjmuty z purge
- Smazání kanálu – nehvězdičkované a nearchivované články se smažou; hvězdičkované a archivované se zachovají bez kanálu (feed_id = NULL, zobrazí se ve Hvězdičkovaných / Archivovaných)

### Zobrazení článků
- Layout: 3-panel (levý panel: kategorie, střední: seznam, pravý: detail)
- Levý panel collapsible – lze připnout nebo skrýt jako overlay/drawer
- Šířka panelů nastavitelná (drag to resize)
- Detail článku v pravém panelu; klik na nadpis = originál v novém tabu; klik na nadpis v detailu = celá obrazovka
- Nastavení hustoty seznamu (globální, zvlášť pro web a mobil):
  - Kompaktní – jen titulek
  - Střední – titulek + perex
  - Plné – titulek + obrázek + perex
- Odhad doby čtení u každého článku
- Přepínač readable verze / původní obsah v detailu článku
- Mobil: navigace Kategorie → Seznam → Detail (3 samostatné obrazovky, swipe back)

### Kategorie (levý panel)
- Všechny články
- Hvězdičkované
- Archivované
- Složky s kanály
- Štítky

### Filtry a štítky
- Uživatelské štítky (název, barva)
- Filtr = sada podmínek → akce
- Podmínky: obsahuje slovo (titulek / obsah / autor / URL), kanál, datum, složka
- Operátory: AND / OR (případně podpora regex)
- Akce: přidat štítek, označit přečtené, přidat hvězdičku, skrýt článek, odeslat notifikaci
- Priorita filtrů – nastavitelné pořadí aplikace
- Aktivní/neaktivní filtr – dočasně vypnout bez mazání
- Filtry se aplikují automaticky při importu nových článků
- Zpětná aplikace filtru na existující články
- Testování filtru (zobrazení článků, které by filtr zachytil)

### API
- MVP bez externího API, web bude responzivní pro mobilní použití
- Fever API plánováno do budoucna (kompatibilita s Reeder, FeedMe, Mr. Reader)
- Nutnost ověřit před výběrem Android appky: podpora hromadného procházení kategorie "články označené štítkem"

### Nastavení – admin
- Správa uživatelů (vytváření, deaktivace)
- Pozvánky – generování pozvánkových linků
- Zakázání registrace – přepínač pro soukromé instance
- Globální interval fetchování RSS
- Globální purge nastavení – výchozí pravidla čištění článků (stáří, počet na kanál)
- Limity: max kanálů na uživatele
- SMTP nastavení – pro odesílání emailů (reset hesla, pozvánky, notifikace)
- Nastavení AI – připraveno, deaktivované v MVP; viz detaily ve Fázi 2
- Logy fetchování – přehled chyb a aktivit (selhané kanály, časy fetchování)
- Záloha a obnovení – export/import celé DB nebo nastavení

---

## Fáze 2 – AI rozšíření

### Správa AI účtů a API klíčů

- **Globální AI profily** – admin definuje profily per účel (shrnutí, překlad, scoring, TTS), každý s vlastním providerem, modelem a API klíčem
- **Per-user API klíče** – uživatel si může zadat vlastní klíč pro daný provider; pokud je zadán, má přednost před globálním
- **Režim povinných uživatelských klíčů** – admin může nastavit, že globální klíč se nepoužije a každý uživatel musí mít svůj vlastní; uživatel bez klíče nemá AI funkce k dispozici
- Nastavení AI klíčů uživatelem v jeho profilu/nastavení

### AI funkce

- AI shrnutí článku (Claude / OpenAI / Gemini – konfigurovatelný provider)
- Nastavení profilu: model, max tokeny, jazyk shrnutí
- Shrnutí generováno on-demand nebo automaticky dle nastavení
- AI označování hvězdičkou – na základě dříve označených článků
- AI filtrování – scoring relevance článku pro uživatele, použitelný jako podmínka ve filtru
- AI překlad – přeložit článek do zvoleného jazyka
- AI kategorizace – automatické návrhy štítků na základě obsahu článku
- Chat s článkem – možnost se zeptat AI na obsah článku (Q&A)
- Denní/týdenní digest – AI shrnutí nejzajímavějších článků za období, odesláno emailem
- on-demand možnost vygenerovat z článku podcast (audio) - konfigurovatelné

---

## Fáze 3 – Rozšíření zdrojů

- YouTube kanály – sledování nových videí
- YouTube shrnutí videa (přes transcript + AI)
- Web scraping – sledování stránek bez RSS
- Sledování platformy X (Twitter)
- Podcasty – sledování RSS feedů s audio, přehrávač přímo ve čtečce

---

## Technický stack

| Vrstva | Technologie |
|---|---|
| Backend | Python + FastAPI |
| Databáze | PostgreSQL |
| Task queue | APScheduler (v FastAPI procesu) |
| Frontend | HTMX + Jinja2 + Tailwind CSS |
| Readable extrakce | trafilatura + readability-lxml (fallback) |
| AI integrace | Anthropic Claude API + OpenAI API |
| Auth | Session cookies (web) + JWT tokeny (API) |
| Nasazení | Docker + docker-compose |

---

## Rozhodnutí

- **Sdílené feedy** – veřejné kanály jsou sdíleny napříč uživateli (fetchují se jednou globálně); privátní kanály s auth jsou per-user. Per-user nastavení feedu je v `user_feeds`, per-user stavy článků v `user_article_states`.
- **Readable verze** – extrahuje se při fetchování a ukládá do DB jako obsah článku; nastavitelné per kanál (default: zapnuto)
- **API standard** – MVP bez externího API; web bude responzivní pro mobilní použití; Fever API případně přidáme dodatečně
- **Distribuce** – GitHub (zdrojový kód) + Docker Hub (hotové image); nastavíme až bude aplikace funkční

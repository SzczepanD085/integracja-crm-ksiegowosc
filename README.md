# Integracja systemów: CRM ↔ księgowość

Projekt zaliczeniowy z przedmiotu **Integracja systemów informatycznych**.

Docelowo: dwukierunkowa synchronizacja danych klientów między systemem CRM
(dział handlowy) a systemem księgowym przez REST API.
Aktualny etap: **import danych z plików CSV/JSON do bazy CRM** (wymagania na 3.0).

## Wymagania

- Python 3.10+
- brak zewnętrznych bibliotek (tylko biblioteka standardowa: `sqlite3`, `csv`, `json`)

## Uruchomienie

```
python import_crm.py                 # domyślnie importuje klienci.csv
python import_crm.py klienci.json    # import z JSON
```

Baza `crm.db` (SQLite) tworzy się automatycznie przy pierwszym uruchomieniu.

## Formaty wejściowe

**CSV**: kodowanie UTF-8, separator przecinek, pierwszy wiersz to nagłówek z kolumnami:
`company_name, tax_id, regon, contact_person, phone, email, street, postal_code, city, lead_status, notes`

**JSON**: lista obiektów z polami jak wyżej. Brakujące pola są traktowane jak puste,
nadmiarowe są ignorowane. NIP należy podawać jako tekst (`"5260001246"`),
bo zapisany jako liczba traci zera wiodące.

## Walidacja

| Pole | Reguła |
|---|---|
| `company_name` | wymagane |
| `tax_id` (NIP) | wymagany, 10 cyfr, poprawna suma kontrolna (wagi 6,5,7,2,3,4,5,6,7, mod 11); myślniki i spacje są usuwane |
| `email` | opcjonalny; jeśli podany, musi mieć format `coś@coś.coś` |

Błędne rekordy są pomijane z komunikatem, a import pozostałych trwa dalej.
Błędy całego pliku (brak pliku, złe kodowanie, brak kolumn, niepoprawny JSON)
przerywają import, zanim program dotknie bazy.

## Decyzje projektowe

- **NIP jako identyfikator biznesowy** wspólny dla obu systemów (`UNIQUE` w bazie);
  wewnętrzne `id` jest lokalne dla każdego systemu.
- **Upsert zamiast pomijania duplikatów**: istniejący klient (ten sam NIP) jest aktualizowany.
  Dzięki temu import jest **idempotentny**: wielokrotne uruchomienie daje ten sam stan bazy.
- **Upsert nie nadpisuje pól należących do księgowości** (`balance_due`, `payment_status`).
- **Cały import w jednej transakcji**: przy błędzie bazy następuje rollback, nie ma importu „w połowie”.
- **Zapytania parametryzowane (`?`)**: ochrona przed SQL injection.
- **Kwoty w groszach (`INTEGER`)**: SQLite nie ma typu dziesiętnego, a float zaokrągla.

## Kody wyjścia

| Kod | Znaczenie |
|---|---|
| 0 | sukces (pominięte rekordy nie są błędem programu) |
| 1 | błąd pliku wejściowego |
| 2 | błąd bazy danych, import wycofany |

## Znane ograniczenia

- Każdy rekord jest traktowany jako **pełny stan klienta**: pominięte pole
  przy aktualizacji zostaje wyczyszczone.
- NIP po zapisie nie powinien się zmieniać (jest kluczem dopasowania).
- Walidacja e-maila sprawdza tylko format, nie istnienie skrzynki.
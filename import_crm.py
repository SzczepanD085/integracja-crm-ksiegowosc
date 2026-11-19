import csv
import json
import re
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "crm.db"
DEFAULT_INPUT = BASE_DIR / "klienci.csv"

REQUIRED_COLUMNS = {"company_name", "tax_id", "regon", "contact_person", "phone",
                    "email", "street", "postal_code", "city", "lead_status", "notes"}

NIP_WEIGHTS = [6, 5, 7, 2, 3, 4, 5, 6, 7]

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

UPSERT_SQL = """
    INSERT INTO customers (company_name, tax_id, regon, contact_person, phone,
                           email, street, postal_code, city, lead_status, notes)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(tax_id) DO UPDATE SET
        company_name   = excluded.company_name,
        regon          = excluded.regon,
        contact_person = excluded.contact_person,
        phone          = excluded.phone,
        email          = excluded.email,
        street         = excluded.street,
        postal_code    = excluded.postal_code,
        city           = excluded.city,
        lead_status    = excluded.lead_status,
        notes          = excluded.notes
"""


class ImportFileError(Exception):
    pass


# ---------- baza ----------

def create_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name   TEXT NOT NULL,
            tax_id         TEXT NOT NULL UNIQUE,
            regon          TEXT,
            contact_person TEXT,
            phone          TEXT,
            email          TEXT,
            street         TEXT,
            postal_code    TEXT,
            city           TEXT,
            lead_status    TEXT NOT NULL DEFAULT 'lead',
            notes          TEXT,
            balance_due    INTEGER NOT NULL DEFAULT 0,
            payment_status TEXT NOT NULL DEFAULT 'ok'
        )
    """)
    conn.commit()


# ---------- odczyt pliku ----------

def read_csv(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
            if missing:
                raise ImportFileError("brak kolumn: " + ", ".join(sorted(missing)))
            return [(f"wiersz {n}", row) for n, row in enumerate(reader, start=2)]
    except UnicodeDecodeError:
        raise ImportFileError("plik nie jest w UTF-8, zapisz go ponownie jako UTF-8")


def to_text(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return None
    return str(value)


def read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except UnicodeDecodeError:
        raise ImportFileError("plik nie jest w UTF-8, zapisz go ponownie jako UTF-8")
    except json.JSONDecodeError as e:
        raise ImportFileError(f"niepoprawny JSON (linia {e.lineno}, kolumna {e.colno}): {e.msg}")

    if not isinstance(data, list):
        raise ImportFileError("JSON musi zawierać listę klientów: [ {...}, {...} ]")

    rows = []
    for n, item in enumerate(data, start=1):
        if isinstance(item, dict):
            row = {col: to_text(item.get(col)) for col in REQUIRED_COLUMNS}
        else:
            row = {col: None for col in REQUIRED_COLUMNS}
        rows.append((f"rekord {n}", row))
    return rows


def read_file(path):
    if not path.exists():
        raise ImportFileError(f"nie znaleziono pliku: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return read_csv(path)
    if suffix == ".json":
        return read_json(path)
    raise ImportFileError(f"nieobsługiwany format: {suffix or 'brak rozszerzenia'} "
                          f"(obsługiwane: .csv, .json)")


# ---------- walidacja ----------

def clean_nip(nip):
    return nip.replace("-", "").replace(" ", "").strip()


def is_valid_nip(nip):
    if not re.fullmatch(r"[0-9]{10}", nip):
        return False
    total = sum(int(digit) * weight for digit, weight in zip(nip, NIP_WEIGHTS))
    return total % 11 == int(nip[9])


def validate_row(row):
    if None in row or None in row.values():
        return ["nieprawidłowa struktura rekordu"]
    errors = []

    if not row["company_name"].strip():
        errors.append("brak nazwy firmy")

    nip = clean_nip(row["tax_id"])
    if not is_valid_nip(nip):
        errors.append(f"nieprawidłowy NIP: {row['tax_id']!r}")
        nip = clean_nip(row["tax_id"])
    if not nip:
        errors.append("brak NIP-u")

    email = row["email"].strip()
    if email and not EMAIL_RE.fullmatch(email):
        errors.append(f"nieprawidłowy e-mail: {email!r}")

    return errors


# ---------- zapis ----------

def opt(value):
    value = value.strip()
    return value or None


def to_record(row):
    return (
        row["company_name"].strip(),
        clean_nip(row["tax_id"]),
        opt(row["regon"]),
        opt(row["contact_person"]),
        opt(row["phone"]),
        opt(row["email"]),
        opt(row["street"]),
        opt(row["postal_code"]),
        opt(row["city"]),
        opt(row["lead_status"]) or "lead",
        opt(row["notes"]),
    )


def import_rows(conn, rows):
    stats = {"dodane": 0, "zaktualizowane": 0, "odrzucone": 0}
    with conn:
        for where, row in rows:
            errors = validate_row(row)
            if errors:
                print(f"{where}: POMINIĘTY: " + "; ".join(errors))
                stats["odrzucone"] += 1
                continue

            record = to_record(row)
            exists = conn.execute(
                "SELECT 1 FROM customers WHERE tax_id = ?", (record[1],)
            ).fetchone()
            conn.execute(UPSERT_SQL, record)

            if exists:
                stats["zaktualizowane"] += 1
            else:
                stats["dodane"] += 1
    return stats


# ---------- start ----------

if __name__ == "__main__":
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
    if not input_path.is_absolute():
        input_path = BASE_DIR / input_path

    try:
        rows = read_file(input_path)
        conn = sqlite3.connect(DB_PATH)
        try:
            create_table(conn)
            stats = import_rows(conn, rows)
            count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        finally:
            conn.close()
    except ImportFileError as e:
        print(f"BŁĄD PLIKU: {e}")
        sys.exit(1)
    except sqlite3.Error as e:
        print(f"BŁĄD BAZY: {e}. Import wycofany, baza bez zmian.")
        sys.exit(2)

    print(f"Plik: {input_path.name}")
    print(f"Dodane: {stats['dodane']}, zaktualizowane: {stats['zaktualizowane']}, "
          f"odrzucone: {stats['odrzucone']}")
    print(f"Klientów w bazie: {count}")
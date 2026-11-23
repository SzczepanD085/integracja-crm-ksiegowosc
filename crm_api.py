import hmac
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request

from import_crm import (DB_PATH, REQUIRED_COLUMNS, clean_nip, create_table,
                        to_record, to_text, validate_row)

load_dotenv(Path(__file__).parent / ".env")
CRM_TOKEN = os.environ.get("CRM_TOKEN")
if not CRM_TOKEN:
    raise SystemExit("Brak CRM_TOKEN w pliku .env, serwer nie wystartuje.")

app = Flask(__name__)
app.json.ensure_ascii = False

@app.before_request
def sprawdz_token():
    naglowek = request.headers.get("Authorization", "")
    oczekiwany = f"Bearer {CRM_TOKEN}"
    if not hmac.compare_digest(naglowek.encode(), oczekiwany.encode()):
        return jsonify(error="brak lub nieprawidłowy token"), 401, {"WWW-Authenticate": "Bearer"}

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db              # ← na poziomie ifa, czyli zawsze

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

@app.get("/customers")
def list_customers():
    rows = get_db().execute(
        "SELECT * FROM customers ORDER BY company_name"
    ).fetchall()
    return jsonify([dict(row) for row in rows])

INSERT_SQL = """
    INSERT INTO customers (company_name, tax_id, regon, contact_person, phone,
                           email, street, postal_code, city, lead_status, notes)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def fetch_customer(tax_id):
    row = get_db().execute(
        "SELECT * FROM customers WHERE tax_id = ?", (tax_id,)
    ).fetchone()
    return dict(row) if row else None


@app.get("/customers/<tax_id>")
def get_customer(tax_id):
    customer = fetch_customer(clean_nip(tax_id))
    if customer is None:
        return jsonify(error="nie znaleziono klienta"), 404
    return jsonify(customer)


@app.post("/customers")
def create_customer():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="oczekiwano obiektu JSON"), 400

    row = {col: to_text(data.get(col)) for col in REQUIRED_COLUMNS}
    errors = validate_row(row)
    if errors:
        return jsonify(errors=errors), 400

    record = to_record(row)
    db = get_db()
    try:
        with db:
            db.execute(INSERT_SQL, record)
    except sqlite3.IntegrityError:
        return jsonify(error=f"klient z NIP {record[1]} już istnieje"), 409

    return jsonify(fetch_customer(record[1])), 201, {"Location": f"/customers/{record[1]}"}

UPDATE_SQL = """
    UPDATE customers SET
        company_name = ?, regon = ?, contact_person = ?, phone = ?, email = ?,
        street = ?, postal_code = ?, city = ?, lead_status = ?, notes = ?
    WHERE tax_id = ?
"""


@app.patch("/customers/<tax_id>")
def update_customer(tax_id):
    tax_id = clean_nip(tax_id)
    current = fetch_customer(tax_id)
    if current is None:
        return jsonify(error="nie znaleziono klienta"), 404

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="oczekiwano obiektu JSON"), 400

    unknown = set(data) - REQUIRED_COLUMNS
    if unknown:
        return jsonify(error="nieznane pola: " + ", ".join(sorted(unknown))), 400

    if "tax_id" in data and clean_nip(to_text(data["tax_id"]) or "") != tax_id:
        return jsonify(error="NIP nie może być zmieniony"), 400

    row = {col: to_text(current[col]) for col in REQUIRED_COLUMNS}
    for col, value in data.items():
        row[col] = to_text(value)

    errors = validate_row(row)
    if errors:
        return jsonify(errors=errors), 400

    record = to_record(row)
    db = get_db()
    with db:
        db.execute(UPDATE_SQL, (record[0], *record[2:], record[1]))

    return jsonify(fetch_customer(tax_id))

@app.put("/integration/customers/<tax_id>/payment")
def update_payment(tax_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="oczekiwano obiektu JSON"), 400

    balance = data.get("balance_due")
    status = data.get("payment_status")
    errors = []
    if not isinstance(balance, int) or isinstance(balance, bool) or balance < 0:
        errors.append("balance_due musi być nieujemną liczbą całkowitą (grosze)")
    if status not in ("ok", "overdue"):
        errors.append("payment_status musi mieć wartość 'ok' albo 'overdue'")
    if errors:
        return jsonify(errors=errors), 400

    db = get_db()
    with db:
        result = db.execute(
            "UPDATE customers SET balance_due = ?, payment_status = ? WHERE tax_id = ?",
            (balance, status, clean_nip(tax_id)),
        )
    if result.rowcount == 0:
        return jsonify(error="nie znaleziono klienta"), 404
    return jsonify(fetch_customer(clean_nip(tax_id)))

if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    create_table(conn)
    conn.close()
    app.run(port=5001, debug=True)

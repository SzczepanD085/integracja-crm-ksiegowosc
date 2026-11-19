import sqlite3

from flask import Flask, g, jsonify, request

from import_crm import (DB_PATH, REQUIRED_COLUMNS, clean_nip, create_table,
                        to_record, to_text, validate_row)

app = Flask(__name__)
app.json.ensure_ascii = False

def get_db():
    if "db" not in g:
        g.db=sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        return g.db

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

if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    create_table(conn)
    conn.close()
    app.run(port=5001, debug=True)

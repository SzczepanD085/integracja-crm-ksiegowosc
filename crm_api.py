import sqlite3
from flask import Flask, g, jsonify
from import_crm import DB_PATH, create_table

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


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    create_table(conn)
    conn.close()
    app.run(port=5001, debug=True)

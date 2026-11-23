import sqlite3
from pathlib import Path

from flask import Flask, g, jsonify

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "ksiegowosc.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS kontrahenci (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nazwa         TEXT NOT NULL,
    nip           TEXT NOT NULL UNIQUE,
    regon         TEXT,
    telefon       TEXT,
    email         TEXT,
    ulica         TEXT,
    kod_pocztowy  TEXT,
    miasto        TEXT,
    nr_rachunku   TEXT
);

CREATE TABLE IF NOT EXISTS faktury (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    nr_faktury        TEXT NOT NULL UNIQUE,
    kontrahent_id     INTEGER NOT NULL REFERENCES kontrahenci(id),
    kwota_gr          INTEGER NOT NULL CHECK (kwota_gr > 0),
    status            TEXT NOT NULL DEFAULT 'wystawiona'
                      CHECK (status IN ('wystawiona', 'oplacona')),
    termin_platnosci  TEXT NOT NULL
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables(conn):
    conn.executescript(SCHEMA)


app = Flask(__name__)
app.json.ensure_ascii = False


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.get("/kontrahenci")
def lista_kontrahentow():
    rows = get_db().execute("SELECT * FROM kontrahenci ORDER BY nazwa").fetchall()
    return jsonify([dict(row) for row in rows])


if __name__ == "__main__":
    conn = connect()
    create_tables(conn)
    conn.close()
    app.run(port=5002, debug=True)
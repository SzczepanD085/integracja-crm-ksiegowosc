import sqlite3
from pathlib import Path
from datetime import date
from flask import Flask, g, jsonify, request
import hmac
import os
from dotenv import load_dotenv

from import_crm import EMAIL_RE, clean_nip, is_valid_nip, opt, to_text

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "ksiegowosc.db"
load_dotenv(BASE_DIR / ".env")
KSIEG_TOKEN = os.environ.get("KSIEG_TOKEN")
if not KSIEG_TOKEN:
    raise SystemExit("Brak KSIEG_TOKEN w pliku .env, serwer nie wystartuje.")

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

POLA = ("nazwa", "nip", "regon", "telefon", "email",
        "ulica", "kod_pocztowy", "miasto", "nr_rachunku")

INSERT_SQL = """
    INSERT INTO kontrahenci (nazwa, nip, regon, telefon, email,
                             ulica, kod_pocztowy, miasto, nr_rachunku)
    VALUES (:nazwa, :nip, :regon, :telefon, :email,
            :ulica, :kod_pocztowy, :miasto, :nr_rachunku)
"""

UPDATE_SQL = """
    UPDATE kontrahenci SET
        nazwa = :nazwa, regon = :regon, telefon = :telefon, email = :email,
        ulica = :ulica, kod_pocztowy = :kod_pocztowy, miasto = :miasto,
        nr_rachunku = :nr_rachunku
    WHERE nip = :nip
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
@app.before_request
def sprawdz_token():
    naglowek = request.headers.get("Authorization", "")
    oczekiwany = f"Bearer {KSIEG_TOKEN}"
    if not hmac.compare_digest(naglowek.encode(), oczekiwany.encode()):
        return jsonify(error="brak lub nieprawidłowy token"), 401, {"WWW-Authenticate": "Bearer"}

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

def waliduj(w):
    if None in w.values():
        return ["nieprawidłowa struktura rekordu"]

    bledy = []
    if not w["nazwa"].strip():
        bledy.append("brak nazwy")

    nip = clean_nip(w["nip"])
    if not nip:
        bledy.append("brak NIP-u")
    elif not is_valid_nip(nip):
        bledy.append(f"nieprawidłowy NIP: {w['nip']!r}")

    email = w["email"].strip()
    if email and not EMAIL_RE.fullmatch(email):
        bledy.append(f"nieprawidłowy e-mail: {email!r}")

    return bledy


def do_zapisu(w):
    rekord = {p: opt(w[p]) for p in POLA}
    rekord["nip"] = clean_nip(w["nip"])
    return rekord


def pobierz_kontrahenta(nip):
    row = get_db().execute(
        "SELECT * FROM kontrahenci WHERE nip = ?", (nip,)
    ).fetchone()
    return dict(row) if row else None


@app.get("/kontrahenci/<nip>")
def jeden_kontrahent(nip):
    k = pobierz_kontrahenta(clean_nip(nip))
    if k is None:
        return jsonify(error="nie znaleziono kontrahenta"), 404
    return jsonify(k)


@app.post("/kontrahenci")
def dodaj_kontrahenta():
    dane = request.get_json(silent=True)
    if not isinstance(dane, dict):
        return jsonify(error="oczekiwano obiektu JSON"), 400

    w = {p: to_text(dane.get(p)) for p in POLA}
    bledy = waliduj(w)
    if bledy:
        return jsonify(errors=bledy), 400

    rekord = do_zapisu(w)
    db = get_db()
    try:
        with db:
            db.execute(INSERT_SQL, rekord)
    except sqlite3.IntegrityError:
        return jsonify(error=f"kontrahent z NIP {rekord['nip']} już istnieje"), 409

    return (jsonify(pobierz_kontrahenta(rekord["nip"])), 201,
            {"Location": f"/kontrahenci/{rekord['nip']}"})


@app.patch("/kontrahenci/<nip>")
def zmien_kontrahenta(nip):
    nip = clean_nip(nip)
    obecny = pobierz_kontrahenta(nip)
    if obecny is None:
        return jsonify(error="nie znaleziono kontrahenta"), 404

    dane = request.get_json(silent=True)
    if not isinstance(dane, dict):
        return jsonify(error="oczekiwano obiektu JSON"), 400

    nieznane = set(dane) - set(POLA)
    if nieznane:
        return jsonify(error="nieznane pola: " + ", ".join(sorted(nieznane))), 400

    if "nip" in dane and clean_nip(to_text(dane["nip"]) or "") != nip:
        return jsonify(error="NIP nie może być zmieniony"), 400

    w = {p: to_text(obecny[p]) for p in POLA}
    for p, v in dane.items():
        w[p] = to_text(v)

    bledy = waliduj(w)
    if bledy:
        return jsonify(errors=bledy), 400

    db = get_db()
    with db:
        db.execute(UPDATE_SQL, do_zapisu(w))

    return jsonify(pobierz_kontrahenta(nip))

NALEZNOSC_SQL = """
    SELECT
        k.nip,
        COALESCE(SUM(CASE WHEN f.status = 'wystawiona'
                          THEN f.kwota_gr END), 0) AS naleznosc_gr,
        COALESCE(SUM(CASE WHEN f.status = 'wystawiona'
                           AND f.termin_platnosci < date('now')
                          THEN f.kwota_gr END), 0) AS zalegle_gr
    FROM kontrahenci k
    LEFT JOIN faktury f ON f.kontrahent_id = k.id
    WHERE k.nip = ?
    GROUP BY k.id
"""


def policz_naleznosc(nip):
    row = get_db().execute(NALEZNOSC_SQL, (nip,)).fetchone()
    if row is None:
        return None
    return {
        "nip": row["nip"],
        "naleznosc_gr": row["naleznosc_gr"],
        "status_platnosci": "overdue" if row["zalegle_gr"] > 0 else "ok",
    }


@app.get("/kontrahenci/<nip>/naleznosc")
def naleznosc(nip):
    wynik = policz_naleznosc(clean_nip(nip))
    if wynik is None:
        return jsonify(error="nie znaleziono kontrahenta"), 404
    return jsonify(wynik)


@app.get("/kontrahenci/<nip>/faktury")
def faktury_kontrahenta(nip):
    rows = get_db().execute("""
        SELECT f.nr_faktury, f.kwota_gr, f.status, f.termin_platnosci
        FROM faktury f
        JOIN kontrahenci k ON k.id = f.kontrahent_id
        WHERE k.nip = ?
        ORDER BY f.termin_platnosci
    """, (clean_nip(nip),)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/faktury")
def dodaj_fakture():
    dane = request.get_json(silent=True)
    if not isinstance(dane, dict):
        return jsonify(error="oczekiwano obiektu JSON"), 400

    bledy = []
    nr = to_text(dane.get("nr_faktury")) or ""
    if not nr.strip():
        bledy.append("brak numeru faktury")

    nip = clean_nip(to_text(dane.get("nip")) or "")
    kontrahent = pobierz_kontrahenta(nip) if nip else None
    if kontrahent is None:
        bledy.append(f"nieznany kontrahent: {nip!r}")

    kwota = dane.get("kwota_gr")
    if not isinstance(kwota, int) or isinstance(kwota, bool) or kwota <= 0:
        bledy.append("kwota_gr musi być dodatnią liczbą całkowitą (w groszach)")

    termin = to_text(dane.get("termin_platnosci")) or ""
    try:
        date.fromisoformat(termin)
    except ValueError:
        bledy.append("termin_platnosci musi mieć format RRRR-MM-DD")

    if bledy:
        return jsonify(errors=bledy), 400

    db = get_db()
    try:
        with db:
            db.execute("""
                INSERT INTO faktury (nr_faktury, kontrahent_id, kwota_gr, termin_platnosci)
                VALUES (?, ?, ?, ?)
            """, (nr.strip(), kontrahent["id"], kwota, termin))
    except sqlite3.IntegrityError:
        return jsonify(error=f"faktura {nr.strip()} już istnieje"), 409

    return jsonify(policz_naleznosc(nip)), 201


@app.post("/faktury/<path:nr>/oplac")
def oplac_fakture(nr):
    db = get_db()
    with db:
        wynik = db.execute(
            "UPDATE faktury SET status = 'oplacona' WHERE nr_faktury = ?", (nr,)
        )
    if wynik.rowcount == 0:
        return jsonify(error="nie znaleziono faktury"), 404

    nip = db.execute("""
        SELECT k.nip FROM faktury f JOIN kontrahenci k ON k.id = f.kontrahent_id
        WHERE f.nr_faktury = ?
    """, (nr,)).fetchone()["nip"]
    return jsonify(policz_naleznosc(nip))


if __name__ == "__main__":
    conn = connect()
    create_tables(conn)
    conn.close()
    app.run(port=5002, debug=True)
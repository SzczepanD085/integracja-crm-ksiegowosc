import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
SYNC_DB = BASE_DIR / "sync.db"
LOG_FILE = BASE_DIR / "sync.log"

CRM_URL = "http://127.0.0.1:5001"
KSIEG_URL = "http://127.0.0.1:5002"
TIMEOUT = 5
PROBY = 3

load_dotenv(BASE_DIR / ".env")
CRM_TOKEN = os.environ.get("CRM_TOKEN")
KSIEG_TOKEN = os.environ.get("KSIEG_TOKEN")
if not CRM_TOKEN or not KSIEG_TOKEN:
    raise SystemExit("Brak CRM_TOKEN lub KSIEG_TOKEN w pliku .env.")

# pole w CRM -> pole w księgowości (tylko pola wspólne)
MAPA = {
    "company_name": "nazwa",
    "tax_id": "nip",
    "regon": "regon",
    "phone": "telefon",
    "email": "email",
    "street": "ulica",
    "postal_code": "kod_pocztowy",
    "city": "miasto",
}
WSPOLNE = [p for p in MAPA if p != "tax_id"]

log = logging.getLogger("sync")


# ---------- logowanie ----------

def ustaw_logowanie():
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    plik = logging.FileHandler(LOG_FILE, encoding="utf-8")
    plik.setFormatter(fmt)
    konsola = logging.StreamHandler()
    konsola.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(plik)
    log.addHandler(konsola)


# ---------- komunikacja z systemami ----------

class BladIntegracji(Exception):
    pass


def sesja(token):
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    return s


def zapytanie(sesja_http, metoda, url, **kwargs):
    """Wysyła zapytanie, ponawiając je przy błędach sieci i błędach serwera (5xx)."""
    for proba in range(1, PROBY + 1):
        try:
            r = sesja_http.request(metoda, url, timeout=TIMEOUT, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            powod = f"brak połączenia ({type(e).__name__})"
        else:
            if r.status_code < 500:
                return r
            powod = f"błąd serwera {r.status_code}"

        if proba < PROBY:
            czekaj = 2 ** (proba - 1)
            log.warning(f"{metoda} {url}: {powod}, próba {proba}/{PROBY}, ponawiam za {czekaj} s")
            time.sleep(czekaj)

    raise BladIntegracji(f"{metoda} {url}: {powod} po {PROBY} próbach")


def odpowiedz_json(r, *oczekiwane):
    """Sprawdza status odpowiedzi i zwraca jej treść jako JSON."""
    if r.status_code not in oczekiwane:
        raise BladIntegracji(f"{r.request.method} {r.url}: status {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except ValueError:
        raise BladIntegracji(f"{r.request.method} {r.url}: odpowiedź nie jest poprawnym JSON-em")


# ---------- pamięć synchronizacji ----------

def polacz_sync():
    conn = sqlite3.connect(SYNC_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS migawki (
            nip               TEXT PRIMARY KEY,
            dane              TEXT NOT NULL,
            zsynchronizowano  TEXT NOT NULL
        )
    """)
    return conn


def wczytaj_migawke(conn, nip):
    row = conn.execute("SELECT dane FROM migawki WHERE nip = ?", (nip,)).fetchone()
    return json.loads(row[0]) if row else None


def zapisz_migawke(conn, nip, dane):
    teraz = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute("""
            INSERT INTO migawki (nip, dane, zsynchronizowano) VALUES (?, ?, ?)
            ON CONFLICT(nip) DO UPDATE SET
                dane = excluded.dane,
                zsynchronizowano = excluded.zsynchronizowano
        """, (nip, json.dumps(dane, ensure_ascii=False), teraz))


# ---------- mapowanie ----------

def z_crm(klient):
    return {p: klient[p] for p in WSPOLNE}


def z_ksiegowosci(kontrahent):
    return {p: kontrahent[MAPA[p]] for p in WSPOLNE}


def do_ksiegowosci(dane):
    return {MAPA[p]: v for p, v in dane.items()}


# ---------- porównanie trójstronne ----------

def porownaj(a, b, baza):
    """a = stan w CRM, b = stan w księgowości, baza = stan z ostatniej synchronizacji."""
    do_crm, do_ksieg, konflikty, wynik = {}, {}, [], {}

    for p in WSPOLNE:
        va, vb = a[p], b[p]
        vbaza = baza.get(p) if baza else None

        if va == vb:
            wynik[p] = va
        elif baza is None and va is None:
            do_crm[p] = vb
            wynik[p] = vb
        elif baza is None and vb is None:
            do_ksieg[p] = va
            wynik[p] = va
        elif baza is not None and va == vbaza:
            do_crm[p] = vb
            wynik[p] = vb
        elif baza is not None and vb == vbaza:
            do_ksieg[p] = va
            wynik[p] = va
        else:
            konflikty.append(p)
            wynik[p] = vbaza

    return do_crm, do_ksieg, konflikty, wynik


# ---------- synchronizacja pojedynczego klienta ----------

def sync_dane_klienta(crm, ksieg, conn, klient, stat):
    nip = klient["tax_id"]
    a = z_crm(klient)
    r = zapytanie(ksieg, "GET", f"{KSIEG_URL}/kontrahenci/{nip}")

    if r.status_code == 404:
        nowy = do_ksiegowosci(a)
        nowy["nip"] = nip
        odpowiedz_json(zapytanie(ksieg, "POST", f"{KSIEG_URL}/kontrahenci", json=nowy), 201)
        zapisz_migawke(conn, nip, a)
        log.info(f"{nip}: utworzono kontrahenta w księgowości")
        stat["zmiany"] += 1
        return

    b = z_ksiegowosci(odpowiedz_json(r, 200))
    baza = wczytaj_migawke(conn, nip)
    do_crm, do_ksieg, konflikty, wynik = porownaj(a, b, baza)

    if do_ksieg:
        odpowiedz_json(zapytanie(ksieg, "PATCH", f"{KSIEG_URL}/kontrahenci/{nip}",
                                 json=do_ksiegowosci(do_ksieg)), 200)
        log.info(f"{nip}: CRM → księgowość: {', '.join(do_ksieg)}")
        stat["zmiany"] += 1

    if do_crm:
        odpowiedz_json(zapytanie(crm, "PATCH", f"{CRM_URL}/customers/{nip}", json=do_crm), 200)
        log.info(f"{nip}: księgowość → CRM: {', '.join(do_crm)}")
        stat["zmiany"] += 1

    for p in konflikty:
        log.warning(f"{nip}: KONFLIKT w polu {p}: CRM={a[p]!r}, księgowość={b[p]!r}")
        stat["konflikty"] += 1

    zapisz_migawke(conn, nip, wynik)


def sync_naleznosc_klienta(crm, ksieg, klient, stat):
    nip = klient["tax_id"]
    r = zapytanie(ksieg, "GET", f"{KSIEG_URL}/kontrahenci/{nip}/naleznosc")
    if r.status_code == 404:
        return

    n = odpowiedz_json(r, 200)
    if (klient["balance_due"] == n["naleznosc_gr"]
            and klient["payment_status"] == n["status_platnosci"]):
        return

    odpowiedz_json(zapytanie(crm, "PUT", f"{CRM_URL}/integration/customers/{nip}/payment",
                             json={"balance_due": n["naleznosc_gr"],
                                   "payment_status": n["status_platnosci"]}), 200)
    log.info(f"{nip}: należność → CRM: {n['naleznosc_gr'] / 100:.2f} zł, {n['status_platnosci']}")
    stat["zmiany"] += 1


# ---------- przebieg synchronizacji ----------

def pobierz_klientow(crm):
    klienci = odpowiedz_json(zapytanie(crm, "GET", f"{CRM_URL}/customers"), 200)
    if not isinstance(klienci, list):
        raise BladIntegracji("CRM zwrócił dane w nieoczekiwanym formacie (oczekiwano listy)")
    return [k for k in klienci if isinstance(k, dict) and k.get("lead_status") == "customer"]


def dla_kazdego(klienci, krok, stat, *args):
    for klient in klienci:
        nip = klient.get("tax_id", "?")
        try:
            krok(*args, klient, stat)
        except BladIntegracji as e:
            log.error(f"{nip}: {e}")
            stat["bledy"] += 1
        except (KeyError, TypeError) as e:
            log.error(f"{nip}: nieoczekiwany format danych ({e!r})")
            stat["bledy"] += 1


def synchronizuj():
    crm = sesja(CRM_TOKEN)
    ksieg = sesja(KSIEG_TOKEN)
    conn = polacz_sync()
    stat = {"zmiany": 0, "konflikty": 0, "bledy": 0}
    try:
        klienci = pobierz_klientow(crm)
        log.info(f"Start synchronizacji: {len(klienci)} klientów do sprawdzenia")
        dla_kazdego(klienci, sync_dane_klienta, stat, crm, ksieg, conn)
        klienci = pobierz_klientow(crm)
        dla_kazdego(klienci, sync_naleznosc_klienta, stat, crm, ksieg)
    except BladIntegracji as e:
        log.error(f"Synchronizacja przerwana: {e}")
        return False
    finally:
        conn.close()

    log.info(f"Koniec synchronizacji: zmiany {stat['zmiany']}, "
             f"konflikty {stat['konflikty']}, błędy {stat['bledy']}")
    return stat["bledy"] == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Synchronizacja CRM ↔ księgowość")
    parser.add_argument("--co", type=int, metavar="SEKUNDY",
                        help="uruchamiaj synchronizację cyklicznie co N sekund")
    args = parser.parse_args()
    ustaw_logowanie()

    if args.co:
        log.info(f"Tryb cykliczny: synchronizacja co {args.co} s (Ctrl+C kończy)")
        try:
            while True:
                synchronizuj()
                time.sleep(args.co)
        except KeyboardInterrupt:
            log.info("Tryb cykliczny zatrzymany")
    else:
        sys.exit(0 if synchronizuj() else 1)
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
SYNC_DB = BASE_DIR / "sync.db"

CRM_URL = "http://127.0.0.1:5001"
KSIEG_URL = "http://127.0.0.1:5002"
TIMEOUT = 5

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

        if va == vb:                              # zgodne, nic do roboty
            wynik[p] = va
        elif baza is None and va is None:         # pierwszy sync: uzupełniamy braki
            do_crm[p] = vb
            wynik[p] = vb
        elif baza is None and vb is None:
            do_ksieg[p] = va
            wynik[p] = va
        elif baza is not None and va == vbaza:    # zmieniła tylko księgowość
            do_crm[p] = vb
            wynik[p] = vb
        elif baza is not None and vb == vbaza:    # zmienił tylko CRM
            do_ksieg[p] = va
            wynik[p] = va
        else:                                     # zmienili oboje: konflikt
            konflikty.append(p)
            wynik[p] = vbaza

    return do_crm, do_ksieg, konflikty, wynik


# ---------- synchronizacja ----------

def synchronizuj():
    conn = polacz_sync()
    klienci = requests.get(f"{CRM_URL}/customers", timeout=TIMEOUT).json()
    print(f"Synchronizacja: {len(klienci)} klientów w CRM")

    for klient in klienci:
        nip = klient["tax_id"]
        if klient["lead_status"] != "customer":
            print(f"  {nip}: pominięty (lead)")
            continue

        a = z_crm(klient)
        r = requests.get(f"{KSIEG_URL}/kontrahenci/{nip}", timeout=TIMEOUT)

        if r.status_code == 404:
            nowy = do_ksiegowosci(a)
            nowy["nip"] = nip
            r = requests.post(f"{KSIEG_URL}/kontrahenci", json=nowy, timeout=TIMEOUT)
            if r.status_code == 201:
                zapisz_migawke(conn, nip, a)
                print(f"  {nip}: UTWORZONO w księgowości")
            else:
                print(f"  {nip}: BŁĄD {r.status_code} przy tworzeniu: {r.text}")
            continue

        if r.status_code != 200:
            print(f"  {nip}: BŁĄD {r.status_code} przy odczycie z księgowości")
            continue

        b = z_ksiegowosci(r.json())
        baza = wczytaj_migawke(conn, nip)
        do_crm, do_ksieg, konflikty, wynik = porownaj(a, b, baza)
        ok = True

        if do_ksieg:
            r = requests.patch(f"{KSIEG_URL}/kontrahenci/{nip}",
                               json=do_ksiegowosci(do_ksieg), timeout=TIMEOUT)
            if r.status_code == 200:
                print(f"  {nip}: CRM → księgowość: {', '.join(do_ksieg)}")
            else:
                ok = False
                print(f"  {nip}: BŁĄD {r.status_code} przy zapisie do księgowości: {r.text}")

        if do_crm:
            r = requests.patch(f"{CRM_URL}/customers/{nip}", json=do_crm, timeout=TIMEOUT)
            if r.status_code == 200:
                print(f"  {nip}: księgowość → CRM: {', '.join(do_crm)}")
            else:
                ok = False
                print(f"  {nip}: BŁĄD {r.status_code} przy zapisie do CRM: {r.text}")

        for p in konflikty:
            print(f"  {nip}: KONFLIKT w polu {p}: CRM={a[p]!r}, księgowość={b[p]!r}")

        if not (do_crm or do_ksieg or konflikty):
            print(f"  {nip}: bez zmian")

        if ok:
            zapisz_migawke(conn, nip, wynik)

    conn.close()


if __name__ == "__main__":
    synchronizuj()
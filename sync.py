import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
SYNC_DB = BASE_DIR / "sync.db"

CRM_URL = "http://127.0.0.1:5001"
KSIEG_URL = "http://127.0.0.1:5002"
TIMEOUT = 5

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


# ---------- połączenia z systemami ----------

def sesja(token):
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    return s


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


# ---------- synchronizacja danych firm (dwukierunkowa) ----------

def synchronizuj_dane(crm, ksieg, conn, klienci):
    print("Dane firm (CRM ↔ księgowość):")
    for klient in klienci:
        nip = klient["tax_id"]
        if klient["lead_status"] != "customer":
            print(f"  {nip}: pominięty (lead)")
            continue

        a = z_crm(klient)
        r = ksieg.get(f"{KSIEG_URL}/kontrahenci/{nip}", timeout=TIMEOUT)

        if r.status_code == 404:
            nowy = do_ksiegowosci(a)
            nowy["nip"] = nip
            r = ksieg.post(f"{KSIEG_URL}/kontrahenci", json=nowy, timeout=TIMEOUT)
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
            r = ksieg.patch(f"{KSIEG_URL}/kontrahenci/{nip}",
                            json=do_ksiegowosci(do_ksieg), timeout=TIMEOUT)
            if r.status_code == 200:
                print(f"  {nip}: CRM → księgowość: {', '.join(do_ksieg)}")
            else:
                ok = False
                print(f"  {nip}: BŁĄD {r.status_code} przy zapisie do księgowości: {r.text}")

        if do_crm:
            r = crm.patch(f"{CRM_URL}/customers/{nip}", json=do_crm, timeout=TIMEOUT)
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


# ---------- synchronizacja należności (księgowość → CRM) ----------

def synchronizuj_naleznosci(crm, ksieg, klienci):
    print("Należności (księgowość → CRM):")
    for klient in klienci:
        nip = klient["tax_id"]
        if klient["lead_status"] != "customer":
            continue

        r = ksieg.get(f"{KSIEG_URL}/kontrahenci/{nip}/naleznosc", timeout=TIMEOUT)
        if r.status_code == 404:
            print(f"  {nip}: jeszcze nie ma w księgowości")
            continue
        if r.status_code != 200:
            print(f"  {nip}: BŁĄD {r.status_code} przy odczycie należności")
            continue

        n = r.json()
        if (klient["balance_due"] == n["naleznosc_gr"]
                and klient["payment_status"] == n["status_platnosci"]):
            print(f"  {nip}: bez zmian ({n['naleznosc_gr'] / 100:.2f} zł, {n['status_platnosci']})")
            continue

        r = crm.put(f"{CRM_URL}/integration/customers/{nip}/payment",
                    json={"balance_due": n["naleznosc_gr"],
                          "payment_status": n["status_platnosci"]},
                    timeout=TIMEOUT)
        if r.status_code == 200:
            print(f"  {nip}: ZAKTUALIZOWANO: {n['naleznosc_gr'] / 100:.2f} zł, {n['status_platnosci']}")
        else:
            print(f"  {nip}: BŁĄD {r.status_code} przy zapisie należności: {r.text}")


# ---------- start ----------

def synchronizuj():
    crm = sesja(CRM_TOKEN)
    ksieg = sesja(KSIEG_TOKEN)
    conn = polacz_sync()
    try:
        klienci = crm.get(f"{CRM_URL}/customers", timeout=TIMEOUT).json()
        synchronizuj_dane(crm, ksieg, conn, klienci)
        klienci = crm.get(f"{CRM_URL}/customers", timeout=TIMEOUT).json()
        synchronizuj_naleznosci(crm, ksieg, klienci)
    finally:
        conn.close()


if __name__ == "__main__":
    synchronizuj()
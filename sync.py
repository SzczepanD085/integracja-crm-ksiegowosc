import requests

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


def crm_do_ksiegowosci(klient):
    return {pole_k: klient[pole_c] for pole_c, pole_k in MAPA.items()}


def sync_crm_do_ksiegowosci():
    klienci = requests.get(f"{CRM_URL}/customers", timeout=TIMEOUT).json()
    print(f"CRM → księgowość: {len(klienci)} klientów w CRM")

    for klient in klienci:
        nip = klient["tax_id"]
        if klient["lead_status"] != "customer":
            print(f"  {nip}: pominięty (lead, jeszcze nie klient)")
            continue

        dane = crm_do_ksiegowosci(klient)
        r = requests.get(f"{KSIEG_URL}/kontrahenci/{nip}", timeout=TIMEOUT)

        if r.status_code == 404:
            r = requests.post(f"{KSIEG_URL}/kontrahenci", json=dane, timeout=TIMEOUT)
            if r.status_code == 201:
                print(f"  {nip}: UTWORZONO w księgowości")
            else:
                print(f"  {nip}: BŁĄD {r.status_code}: {r.text}")

        elif r.status_code == 200:
            obecny = r.json()
            zmiany = {p: v for p, v in dane.items()
                      if p != "nip" and obecny.get(p) != v}
            if not zmiany:
                print(f"  {nip}: bez zmian")
                continue
            r = requests.patch(f"{KSIEG_URL}/kontrahenci/{nip}", json=zmiany, timeout=TIMEOUT)
            if r.status_code == 200:
                print(f"  {nip}: ZAKTUALIZOWANO {', '.join(zmiany)}")
            else:
                print(f"  {nip}: BŁĄD {r.status_code}: {r.text}")

        else:
            print(f"  {nip}: BŁĄD {r.status_code} przy odczycie z księgowości")


if __name__ == "__main__":
    sync_crm_do_ksiegowosci()
from ksiegowosc_api import connect, create_tables

KONTRAHENCI = [
    # nazwa, nip, email, ulica, kod_pocztowy, miasto, nr_rachunku
    ("Kwadrat Sp. z o.o.", "5260001246", "biuro@kwadrat.pl",
     "ul. Lipowa 12A", "20-001", "Lublin", "PL00000000000000000000000000"),
    ("Zielony Ogród", "9462345672", "kontakt@zielonyogrod.pl",
     "ul. Kwiatowa 3/5", "20-400", "Lublin", None),
]

FAKTURY = [
    # nr_faktury, nip_kontrahenta, kwota_gr, status, termin_platnosci
    ("FV/2026/10/001", "5260001246", 120000, "oplacona", "2026-10-15"),
    ("FV/2026/10/002", "5260001246", 45000, "wystawiona", "2026-10-31"),
    ("FV/2026/11/001", "9462345672", 89900, "wystawiona", "2026-12-15"),
]

conn = connect()
create_tables(conn)
with conn:
    conn.executemany("""
        INSERT OR IGNORE INTO kontrahenci
            (nazwa, nip, email, ulica, kod_pocztowy, miasto, nr_rachunku)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, KONTRAHENCI)

    conn.executemany("""
        INSERT OR IGNORE INTO faktury
            (nr_faktury, kontrahent_id, kwota_gr, status, termin_platnosci)
        VALUES (?, (SELECT id FROM kontrahenci WHERE nip = ?), ?, ?, ?)
    """, FAKTURY)

count_k = conn.execute("SELECT COUNT(*) FROM kontrahenci").fetchone()[0]
count_f = conn.execute("SELECT COUNT(*) FROM faktury").fetchone()[0]
conn.close()
print(f"Kontrahentów: {count_k}, faktur: {count_f}")
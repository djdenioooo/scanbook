import os
import sys
import json
import sqlite3
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta

# 🔑 DEINEN GOOGLE API KEY HIER EINFÜGEN ODER ALS UMGEBUNGSVARIABLE SETZEN:
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "DEIN_API_KEY_HIER")
DB_NAME = "buecherei_system.db"

# 🌐 GITHUB DETAILS FÜR AUTOMATISCHE UPDATES:
GITHUB_RAW_URL = "https://raw.githubusercontent.com/djdenioooo/scanbook/refs/heads/main/ScanBook.py"


# --- AUTOMATISCHES UPDATE SYSTEM ---
def checkForUpdates():
    print("\n" + "=" * 50)
    print("🔄 SUCHE NACH UPDATES AUF GITHUB...")
    print("=" * 50)
    
    current_script = os.path.realpath(__file__)
    
    try:
        # Code von GitHub abrufen
        req = urllib.request.Request(GITHUB_RAW_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            remote_code = response.read().decode('utf-8')
        
        # Lokalen Code einlesen
        with open(current_script, 'r', encoding='utf-8') as f:
            local_code = f.read()

        if remote_code.strip() == local_code.strip():
            print("✅ Das Programm ist bereits auf dem neuesten Stand!")
            input("\nDrücke [ENTER], um zum Hauptmenü zurückzukehren...")
            return

        print("✨ Neue Version auf GitHub gefunden!")
        bestaetigung = input("Möchtest du das Update jetzt installieren? (j/n): ").strip().lower()
        
        if bestaetigung == 'j':
            # 1. Backup erstellen
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = f"{current_script}.bak_{timestamp}.py"
            with open(backup_file, 'w', encoding='utf-8') as f:
                f.write(local_code)
            print(f"📦 Backup gespeichert unter: {os.path.basename(backup_file)}")

            # 2. Datei überschreiben
            with open(current_script, 'w', encoding='utf-8') as f:
                f.write(remote_code)
            print("🚀 Update erfolgreich installiert!")
            print("🔄 Das Programm wird jetzt neu gestartet...\n")

            # 3. Programm automatisch neu starten
            os.execv(sys.executable, [sys.executable] + sys.argv)
        else:
            print("Update abgebrochen.")

    except Exception as e:
        print(f"⚠️ Fehler beim Suchen nach Updates: {e}")
        print("Stelle sicher, dass die Internetverbindung steht und der GitHub-Pfad korrekt ist.")
        input("\nDrücke [ENTER], um fortzufahren...")


# --- HELFER: NACHNAME EXTRACTOR ---
def get_sort_name(autor_string):
    if not autor_string or autor_string.strip() in ['Unbekannt', 'N/A']:
        return "ZZZ_UNBEKANNT", "Unbekannt"

    erster_autor = autor_string.split(',')[0].strip()
    teile = erster_autor.split()

    if len(teile) == 1:
        return teile[0].upper(), erster_autor
    
    nachname = teile[-1]
    vorname = " ".join(teile[:-1])
    return nachname.upper(), f"{nachname}, {vorname}"


# --- DATENBANK INITIALISIEREN ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS buecher (
                isbn TEXT PRIMARY KEY,
                titel TEXT,
                autoren TEXT,
                autor_nachname TEXT,
                autor_formatiert TEXT,
                verlag TEXT,
                erscheinungsjahr TEXT,
                genre TEXT,
                anzahl_gesamt INTEGER DEFAULT 1,
                anzahl_verfuegbar INTEGER DEFAULT 1
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ausleihen (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                isbn TEXT,
                ausleiher TEXT,
                ausgeliehen_am DATE,
                rueckgabe_bis DATE,
                status TEXT DEFAULT 'aktiv',
                FOREIGN KEY(isbn) REFERENCES buecher(isbn)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS einstellungen (
                schluessel TEXT PRIMARY KEY,
                wert TEXT
            )
        ''')

        defaults = {
            "schulname": "Muster-Gymnasium",
            "bib_name": "Schulbibliothek",
            "adresse": "Schulstraße 1, 12345 Musterstadt",
            "kontakt_email": "bibliothek@schule.de"
        }

        for key, val in defaults.items():
            cursor.execute("INSERT OR IGNORE INTO einstellungen (schluessel, wert) VALUES (?, ?)", (key, val))


# --- HELFER: STAMMDATEN LADEN & SPEICHERN ---
def get_einstellung(schluessel, default=""):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT wert FROM einstellungen WHERE schluessel = ?", (schluessel,))
        row = cursor.fetchone()
        return row[0] if row else default


def set_einstellung(schluessel, wert):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO einstellungen (schluessel, wert) VALUES (?, ?)", (schluessel, wert))


# --- STAMMDATEN VERWALTEN (MENÜ) ---
def manage_stammdaten():
    while True:
        schulname = get_einstellung("schulname")
        bib_name = get_einstellung("bib_name")
        adresse = get_einstellung("adresse")
        email = get_einstellung("kontakt_email")

        print("\n" + "=" * 50)
        print("⚙️ STAMMDATEN VERWALTEN")
        print("=" * 50)
        print(f"1. Schulname:       {schulname}")
        print(f"2. Bibliotheksname: {bib_name}")
        print(f"3. Adresse:         {adresse}")
        print(f"4. E-Mail:          {email}")
        print("5. Zurück zum Hauptmenü")

        wahl = input("\nWelchen Eintrag möchtest du ändern? (1-5): ").strip()

        if wahl == "1":
            neu = input("Neuer Schulname: ").strip()
            if neu:
                set_einstellung("schulname", neu)
                print("✅ Schulname aktualisiert!")
        elif wahl == "2":
            neu = input("Neuer Bibliotheksname: ").strip()
            if neu:
                set_einstellung("bib_name", neu)
                print("✅ Bibliotheksname aktualisiert!")
        elif wahl == "3":
            neu = input("Neue Adresse: ").strip()
            if neu:
                set_einstellung("adresse", neu)
                print("✅ Adresse aktualisiert!")
        elif wahl == "4":
            neu = input("Neue Kontakt-E-Mail: ").strip()
            if neu:
                set_einstellung("kontakt_email", neu)
                print("✅ E-Mail aktualisiert!")
        elif wahl == "5":
            break
        else:
            print("Ungültige Eingabe.")


# --- GOOGLE BOOKS API ABFRAGE ---
def fetch_from_google(isbn):
    clean_isbn = isbn.replace("-", "").strip()
    params = f"q=isbn:{clean_isbn}"
    if GOOGLE_API_KEY and GOOGLE_API_KEY != "DEIN_API_KEY_HIER":
        params += f"&key={GOOGLE_API_KEY}"
        
    url = f"https://www.googleapis.com/books/v1/volumes?{params}"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            
        if "items" in data and len(data["items"]) > 0:
            info = data["items"][0]["volumeInfo"]
            authors_list = info.get('authors', ['Unbekannt'])
            autoren_raw = ', '.join(authors_list) if authors_list else 'Unbekannt'
            sort_key, formatiert = get_sort_name(autoren_raw)
            
            return {
                'isbn': clean_isbn,
                'titel': info.get('title', 'Unbekannter Titel'),
                'autoren': autoren_raw,
                'autor_nachname': sort_key,
                'autor_formatiert': formatiert,
                'verlag': info.get('publisher', 'N/A'),
                'erscheinungsjahr': info.get('publishedDate', 'N/A')[:4],
                'genre': ', '.join(info.get('categories', ['Keine Angabe']))
            }
    except Exception as e:
        print(f"⚠️ API-Abfragefehlgeschlagen: {e}")
    return None


# --- BUCH SCANNEN UND HINZUFÜGEN ---
def scan_and_add():
    print("\n" + "=" * 50)
    print("📥 SCANNER-MODUS AKTIV")
    print("Scanne nacheinander deine Bücher.")
    print("Gib 'q' ein, um zum Hauptmenü zurückzukehren.")
    print("=" * 50)

    while True:
        try:
            isbn_input = input("\n👉 Scanne/Gib ISBN ein (oder 'q' für Menü): ").strip()
            clean_isbn = isbn_input.replace("-", "").strip()
            
            if clean_isbn.lower() == 'q':
                break

            if not clean_isbn:
                continue

            with sqlite3.connect(DB_NAME) as conn:
                cursor = conn.cursor()

                cursor.execute("SELECT titel, anzahl_gesamt, anzahl_verfuegbar FROM buecher WHERE isbn = ?", (clean_isbn,))
                existing = cursor.fetchone()

                if existing:
                    titel, gesamt, verfuegbar = existing
                    cursor.execute('''
                        UPDATE buecher 
                        SET anzahl_gesamt = ?, anzahl_verfuegbar = ? 
                        WHERE isbn = ?
                    ''', (gesamt + 1, verfuegbar + 1, clean_isbn))
                    print(f"✅ '{titel}' aufgestockt -> Gesamt: {gesamt + 1}x (Verfügbar: {verfuegbar + 1}x)")
                else:
                    print(f"🔍 Lade Daten für ISBN {clean_isbn}...")
                    data = fetch_from_google(clean_isbn)
                    if data:
                        cursor.execute('''
                            INSERT INTO buecher 
                            (isbn, titel, autoren, autor_nachname, autor_formatiert, verlag, erscheinungsjahr, genre, anzahl_gesamt, anzahl_verfuegbar)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
                        ''', (
                            data['isbn'], data['titel'], data['autoren'], 
                            data['autor_nachname'], data['autor_formatiert'], 
                            data['verlag'], data['erscheinungsjahr'], data['genre']
                        ))
                        print(f"✨ NEU HINZUGEFÜGT: {data['titel']} ({data['autor_formatiert']})")
                    else:
                        print("❌ Kein Buch mit dieser ISBN gefunden.")

        except Exception as e:
            print(f"⚠️ Fehler: {e}")


# --- BUCH AUSLEIHEN ---
def book_checkout():
    print("\n" + "=" * 50)
    print("📤 BUCH AUSLEIHEN")
    print("=" * 50)
    
    isbn_input = input("👉 Scanne/Gib die ISBN des Buches ein: ").strip()
    clean_isbn = isbn_input.replace("-", "").strip()
    if not clean_isbn:
        return

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT titel, anzahl_verfuegbar FROM buecher WHERE isbn = ?", (clean_isbn,))
        book = cursor.fetchone()

        if not book:
            print("❌ Dieses Buch existiert nicht im Katalog. Bitte zuerst erfassen.")
            return

        titel, verfuegbar = book
        if verfuegbar <= 0:
            print(f"❌ '{titel}' ist aktuell vollständig ausgeliehen!")
            return

        print(f"📖 Ausleihe für: '{titel}' (Verfügbar: {verfuegbar} Exemplar(e))")
        ausleiher = input("👤 Name des Ausleihers: ").strip()
        if not ausleiher:
            print("❌ Ausleihe abgebrochen. Name erforderlich.")
            return

        tage_input = input("📅 Ausleihdauer in Tagen [Standard: 14]: ").strip()
        tage = int(tage_input) if tage_input.isdigit() else 14

        heute = datetime.now().date()
        rueckgabe_datum = heute + timedelta(days=tage)

        cursor.execute('''
            INSERT INTO ausleihen (isbn, ausleiher, ausgeliehen_am, rueckgabe_bis, status)
            VALUES (?, ?, ?, ?, 'aktiv')
        ''', (clean_isbn, ausleiher, heute, rueckgabe_datum))

        cursor.execute('''
            UPDATE buecher SET anzahl_verfuegbar = anzahl_verfuegbar - 1 WHERE isbn = ?
        ''', (clean_isbn,))

    print("\n✅ AUSLEIHE ERFOLGREICH GESPEICHERT!")
    print(f"👤 Ausleiher:    {ausleiher}")
    print(f"📖 Buch:        {titel}")
    print(f"📅 Rückgabe bis: {rueckgabe_datum.strftime('%d.%m.%Y')}")


# --- BUCH ZURÜCKGEBEN ---
def book_return():
    print("\n" + "=" * 50)
    print("📥 BUCH ZURÜCKGEBEN")
    print("=" * 50)

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()

        cursor.execute('''
            SELECT a.id, a.isbn, b.titel, a.ausleiher, a.rueckgabe_bis 
            FROM ausleihen a
            JOIN buecher b ON a.isbn = b.isbn
            WHERE a.status = 'aktiv'
        ''')
        aktive_ausleihen = cursor.fetchall()

        if not aktive_ausleihen:
            print("Es gibt aktuell keine ausgeliehenen Bücher.")
            return

        print("\nAktuell ausgeliehene Bücher:")
        for idx, (ausleihe_id, isbn, titel, ausleiher, rueckgabe_bis) in enumerate(aktive_ausleihen, 1):
            print(f"{idx}. '{titel}' | Geliehen von: {ausleiher} (Soll-Rückgabe: {rueckgabe_bis})")

        wahl = input("\nWähle die Nummer des zurückgegebenen Buches (oder [ENTER] zum Abbrechen): ").strip()
        if not wahl.isdigit() or int(wahl) < 1 or int(wahl) > len(aktive_ausleihen):
            print("Abgebrochen.")
            return

        selected = aktive_ausleihen[int(wahl) - 1]
        ausleihe_id, isbn, titel, ausleiher, _ = selected

        cursor.execute("UPDATE ausleihen SET status = 'zurueckgegeben' WHERE id = ?", (ausleihe_id,))
        cursor.execute("UPDATE buecher SET anzahl_verfuegbar = anzahl_verfuegbar + 1 WHERE isbn = ?", (isbn,))

    print(f"\n✅ '{titel}' wurde erfolgreich als zurückgegeben markiert!")


# --- AUSLEIH-ÜBERSICHT & ÜBERFÄLLIGE BÜCHER ANZEIGEN ---
def show_loans():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()

        cursor.execute('''
            SELECT a.isbn, b.titel, b.autor_formatiert, a.ausleiher, a.ausgeliehen_am, a.rueckgabe_bis
            FROM ausleihen a
            JOIN buecher b ON a.isbn = b.isbn
            WHERE a.status = 'aktiv'
            ORDER BY a.rueckgabe_bis ASC
        ''')
        ausleihen = cursor.fetchall()

    print("\n" + "=" * 75)
    print("📋 AKTUELL AUSGELIEHENE BÜCHER")
    print("=" * 75)

    if not ausleihen:
        print("Keine Bücher ausgeliehen. Alle Exemplare sind in der Bücherei.")
    else:
        heute = datetime.now().date()
        for isbn, titel, autor, ausleiher, geliehen_am, rueckgabe_bis in ausleihen:
            due_date = datetime.strptime(rueckgabe_bis, '%Y-%m-%d').date()
            diff_days = (due_date - heute).days

            if diff_days < 0:
                status_str = f"⚠️ ÜBERFÄLLIG seit {abs(diff_days)} Tag(en)!"
            elif diff_days == 0:
                status_str = "⌛ HEUTE FÄLLIG!"
            else:
                status_str = f"🟢 Noch {diff_days} Tag(e) Zeit"

            print(f"📖 Buch:         {titel} ({autor})")
            print(f"👤 Geliehen von: {ausleiher}")
            print(f"📅 Geliehen am:  {geliehen_am}  |  Rückgabe bis: {due_date.strftime('%d.%m.%Y')}")
            print(f"📌 Status:       {status_str}")
            print("-" * 75)

    input("\nDrücke [ENTER], um zum Hauptmenü zurückzukehren...")


# --- KATALOG ANZEIGEN ---
def show_sorted_catalog():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT autor_formatiert, titel, erscheinungsjahr, genre, anzahl_gesamt, anzahl_verfuegbar, isbn 
            FROM buecher 
            ORDER BY autor_nachname ASC, titel ASC
        ''')
        buecher = cursor.fetchall()

    print("\n" + "=" * 75)
    print("📚 BÜCHEREI-KATALOG (SORTIERT NACH AUTOR-NACHNAME)")
    print("=" * 75)

    if not buecher:
        print("Der Katalog ist leer.")
    else:
        for autor_formatiert, titel, jahr, genre, gesamt, verfuegbar, isbn in buecher:
            status_bestand = f"Bestand: {verfuegbar}/{gesamt} verfügbar"
            print(f"• {autor_formatiert:<25} | {titel} ({jahr}) [{status_bestand}]")
            print(f"  Genre: {genre} | ISBN: {isbn}")
            print("-" * 75)

    input("\nDrücke [ENTER], um zum Hauptmenü zurückzukehren...")


# --- HAUPTMENÜ ---
def main():
    init_db()

    while True:
        bib_name = get_einstellung("bib_name", "Schulbibliothek")
        print(f"\n=== 📚 ScanBook - {bib_name} ===")
        print("1. Bücher scannen / Bestand aufstocken")
        print("2. Gesamten Katalog anzeigen (nach Autor)")
        print("3. Buch AUSLEIHEN")
        print("4. Buch ZURÜCKGEBEN")
        print("5. Ausleih-Übersicht & Überfällige Bücher anzeigen")
        print("6. Nach Programm-Updates suchen (GitHub)")
        print("7. Stammdaten")
        print("8. Beenden")

        wahl = input("\nWähle eine Option (1-8): ").strip()

        if wahl == "1":
            scan_and_add()
        elif wahl == "2":
            show_sorted_catalog()
        elif wahl == "3":
            book_checkout()
        elif wahl == "4":
            book_return()
        elif wahl == "5":
            show_loans()
        elif wahl == "6":
            checkForUpdates()
        elif wahl == "7":
            manage_stammdaten()
        elif wahl == "8":
            print("\nProgramm beendet. Auf Wiedersehen!")
            break
        else:
            print("Ungültige Option, bitte 1 bis 8 wählen.")


if __name__ == "__main__":
    main()

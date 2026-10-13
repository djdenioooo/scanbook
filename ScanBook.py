import os
import re
import sys
import csv
import json
import sqlite3
import urllib.request
import urllib.parse
import urllib.error
from contextlib import contextmanager
from datetime import datetime, date, timedelta

# 🏷️ PROGRAMMVERSION – bei jeder neuen Version auf GitHub erhöhen, sonst wird kein Update erkannt!
VERSION = "2.0.1"

# 🔑 DEINEN GOOGLE API KEY HIER EINFÜGEN ODER ALS UMGEBUNGSVARIABLE SETZEN:
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "AIzaSyCOHKdh3i_QjAaiM2On-5fEjbogeSLXOZ0")

# 📁 ALLE DATEIEN LIEGEN IM ORDNER DES PROGRAMMS (egal, von wo es gestartet wird)
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
DB_FILENAME = "buecherei_system.db"
DB_NAME = os.path.join(BASE_DIR, DB_FILENAME)
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
EXPORT_DIR = os.path.join(BASE_DIR, "exporte")

MAX_AUTO_BACKUPS = 14
MAX_UPDATE_BACKUPS = 3

# 🌐 GITHUB DETAILS FÜR AUTOMATISCHE UPDATES:
GITHUB_REPO = "djdenioooo/scanbook"
GITHUB_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RAW_TEMPLATE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{{ref}}/ScanBook.py"

USER_AGENT = f"ScanBook/{VERSION}"

# Werte, die bedeuten "diese Angabe fehlt"
LEERWERTE = {"", "Unbekannt", "Unbekannter Titel", "N/A", "Keine Angabe"}

BUCH_FELDER = [
    ("titel", "Titel"),
    ("autoren", "Autor(en)"),
    ("verlag", "Verlag"),
    ("erscheinungsjahr", "Erscheinungsjahr"),
    ("genre", "Genre"),
]

STAMMDATEN = [
    ("schulname", "Schulname", None),
    ("bib_name", "Bibliotheksname", None),
    ("adresse", "Adresse", None),
    ("kontakt_email", "E-Mail", None),
    ("standard_leihdauer", "Standard-Leihdauer (Tage)", "positiv"),
    ("loeschfrist_monate", "Löschfrist alter Ausleihen (Monate, 0 = nie)", "zahl"),
]

DEFAULT_EINSTELLUNGEN = {
    "schulname": "Muster-Gymnasium",
    "bib_name": "Schulbibliothek",
    "adresse": "Schulstraße 1, 12345 Musterstadt",
    "kontakt_email": "bibliothek@schule.de",
    "standard_leihdauer": "14",
    "loeschfrist_monate": "12",
}

BUCH_SPALTEN = "autor_formatiert, titel, erscheinungsjahr, genre, anzahl_gesamt, anzahl_verfuegbar, isbn"


# --- ALLGEMEINE HELFER ---
@contextmanager
def db_connection():
    conn = sqlite3.connect(DB_NAME)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ueberschrift(titel, breite=50):
    print("\n" + "=" * breite)
    print(titel)
    print("=" * breite)


def warte():
    input("\nDrücke [ENTER], um zum Hauptmenü zurückzukehren...")


def ja_nein(frage):
    return input(f"{frage} (j/n): ").strip().lower() == "j"


def heute():
    return datetime.now().date()


def de_datum(iso_datum):
    try:
        return date.fromisoformat(iso_datum).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return iso_datum or "-"


def faelligkeit_text(rueckgabe_bis):
    diff_days = (date.fromisoformat(rueckgabe_bis) - heute()).days
    if diff_days < 0:
        return f"⚠️ ÜBERFÄLLIG seit {abs(diff_days)} Tag(en)!"
    if diff_days == 0:
        return "⌛ HEUTE FÄLLIG!"
    return f"🟢 Noch {diff_days} Tag(e) Zeit"


def waehle_aus_liste(eintraege, formatierer, prompt="\nWähle eine Nummer (oder [ENTER] zum Abbrechen): "):
    for idx, eintrag in enumerate(eintraege, 1):
        print(f"{idx}. {formatierer(eintrag)}")
    wahl = input(prompt).strip()
    if wahl.isdigit() and 1 <= int(wahl) <= len(eintraege):
        return eintraege[int(wahl) - 1]
    print("Abgebrochen.")
    return None


def alte_dateien_aufraeumen(ordner, praefix, behalten):
    if not os.path.isdir(ordner):
        return
    dateien = sorted(n for n in os.listdir(ordner) if n.startswith(praefix))
    for name in dateien[:-behalten]:
        os.remove(os.path.join(ordner, name))


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8")


# --- ISBN PRÜFEN & VEREINHEITLICHEN ---
def isbn10_gueltig(isbn):
    if not re.fullmatch(r"\d{9}[\dX]", isbn):
        return False
    werte = [10 if z == "X" else int(z) for z in isbn]
    return sum((10 - i) * w for i, w in enumerate(werte)) % 11 == 0


def isbn13_gueltig(isbn):
    if not re.fullmatch(r"\d{13}", isbn):
        return False
    return sum(int(z) * (1 if i % 2 == 0 else 3) for i, z in enumerate(isbn)) % 10 == 0


def isbn10_zu_13(isbn10):
    kern = "978" + isbn10[:9]
    pruefziffer = (10 - sum(int(z) * (1 if i % 2 == 0 else 3) for i, z in enumerate(kern)) % 10) % 10
    return kern + str(pruefziffer)


def normalize_isbn(eingabe):
    """Gibt die ISBN-13 zurück, oder None, wenn die Eingabe keine gültige ISBN ist."""
    isbn = re.sub(r"[\s-]", "", eingabe or "").upper()
    if len(isbn) == 10 and isbn10_gueltig(isbn):
        return isbn10_zu_13(isbn)
    if len(isbn) == 13 and isbn13_gueltig(isbn):
        return isbn
    return None


def lies_isbn(prompt):
    eingabe = input(prompt).strip()
    if not eingabe:
        return None
    isbn = normalize_isbn(eingabe)
    if not isbn:
        print("❌ Ungültige ISBN – die Prüfziffer stimmt nicht. Bitte erneut scannen oder eingeben.")
    return isbn


# --- AUTOMATISCHES UPDATE SYSTEM ---
def lies_version(code):
    treffer = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', code, re.MULTILINE)
    return treffer.group(1) if treffer else None


def version_tupel(version):
    return tuple(int(teil) for teil in re.findall(r"\d+", version))


def hole_update_code():
    """Holt bevorzugt das neueste offizielle Release, sonst den main-Branch."""
    try:
        release = json.loads(http_get(GITHUB_RELEASE_API))
        tag = release["tag_name"]
        return f"Release {tag}", http_get(GITHUB_RAW_TEMPLATE.format(ref=tag))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    # Noch kein Release vorhanden → main-Branch
    return "main-Branch", http_get(GITHUB_RAW_TEMPLATE.format(ref="refs/heads/main"))


def checkForUpdates():
    ueberschrift("🔄 SUCHE NACH UPDATES AUF GITHUB...")
    print(f"Installierte Version: {VERSION}")

    current_script = os.path.realpath(__file__)

    try:
        quelle, remote_code = hole_update_code()
        remote_version = lies_version(remote_code)

        if not remote_version:
            print("⚠️ Die Datei auf GitHub enthält keine Versionsnummer – Update wird nicht installiert.")
            warte()
            return

        if version_tupel(remote_version) <= version_tupel(VERSION):
            print("✅ Das Programm ist bereits auf dem neuesten Stand!")
            warte()
            return

        # Sicherstellen, dass der heruntergeladene Code überhaupt lauffähig ist
        try:
            compile(remote_code, "ScanBook.py", "exec")
        except SyntaxError as e:
            print(f"⚠️ Die neue Version ist fehlerhaft ({e}) – Update wird nicht installiert.")
            warte()
            return

        print(f"✨ Neue Version {remote_version} gefunden ({quelle})!")
        if not ja_nein("Möchtest du das Update jetzt installieren?"):
            print("Update abgebrochen.")
            return

        # 1. Backup erstellen (nur die letzten paar werden behalten)
        os.makedirs(BACKUP_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = os.path.join(BACKUP_DIR, f"ScanBook_{timestamp}_v{VERSION}.py")
        with open(current_script, "r", encoding="utf-8") as f:
            local_code = f.read()
        with open(backup_file, "w", encoding="utf-8") as f:
            f.write(local_code)
        alte_dateien_aufraeumen(BACKUP_DIR, "ScanBook_", MAX_UPDATE_BACKUPS)
        print(f"📦 Backup gespeichert unter: backups/{os.path.basename(backup_file)}")

        # 2. Datei überschreiben
        with open(current_script, "w", encoding="utf-8") as f:
            f.write(remote_code)
        print("🚀 Update erfolgreich installiert!")
        print("🔄 Das Programm wird jetzt neu gestartet...\n")

        # 3. Programm automatisch neu starten
        os.execv(sys.executable, [sys.executable, current_script] + sys.argv[1:])

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
def alte_datenbank_uebernehmen():
    """Frühere Versionen legten die Datenbank im aktuellen Startordner ab."""
    alter_pfad = os.path.join(os.getcwd(), DB_FILENAME)
    if os.path.exists(DB_NAME) or os.path.realpath(alter_pfad) == DB_NAME or not os.path.exists(alter_pfad):
        return
    src = sqlite3.connect(alter_pfad)
    dst = sqlite3.connect(DB_NAME)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"📂 Bestehende Datenbank aus {os.getcwd()} in den Programmordner übernommen.")


def init_db():
    with db_connection() as conn:
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
                zurueckgegeben_am DATE,
                verlaengerungen INTEGER DEFAULT 0,
                FOREIGN KEY(isbn) REFERENCES buecher(isbn)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS einstellungen (
                schluessel TEXT PRIMARY KEY,
                wert TEXT
            )
        ''')

        # Spalten nachrüsten, die ältere Versionen noch nicht hatten
        vorhandene_spalten = {row[1] for row in cursor.execute("PRAGMA table_info(ausleihen)")}
        if "zurueckgegeben_am" not in vorhandene_spalten:
            cursor.execute("ALTER TABLE ausleihen ADD COLUMN zurueckgegeben_am DATE")
        if "verlaengerungen" not in vorhandene_spalten:
            cursor.execute("ALTER TABLE ausleihen ADD COLUMN verlaengerungen INTEGER DEFAULT 0")

        for key, val in DEFAULT_EINSTELLUNGEN.items():
            cursor.execute("INSERT OR IGNORE INTO einstellungen (schluessel, wert) VALUES (?, ?)", (key, val))

        isbns_vereinheitlichen(cursor)


def isbns_vereinheitlichen(cursor):
    """Wandelt alte ISBN-10-Einträge in ISBN-13 um und führt doppelte Bücher zusammen."""
    for (alt,) in cursor.execute("SELECT isbn FROM buecher").fetchall():
        neu = normalize_isbn(alt)
        if not neu or neu == alt:
            continue

        cursor.execute("SELECT 1 FROM buecher WHERE isbn = ?", (neu,))
        if cursor.fetchone():
            cursor.execute('''
                UPDATE buecher SET
                    anzahl_gesamt = anzahl_gesamt + (SELECT anzahl_gesamt FROM buecher WHERE isbn = ?),
                    anzahl_verfuegbar = anzahl_verfuegbar + (SELECT anzahl_verfuegbar FROM buecher WHERE isbn = ?)
                WHERE isbn = ?
            ''', (alt, alt, neu))
            cursor.execute("DELETE FROM buecher WHERE isbn = ?", (alt,))
        else:
            cursor.execute("UPDATE buecher SET isbn = ? WHERE isbn = ?", (neu, alt))

        cursor.execute("UPDATE ausleihen SET isbn = ? WHERE isbn = ?", (neu, alt))


# --- HELFER: STAMMDATEN LADEN & SPEICHERN ---
def get_einstellung(schluessel, default=""):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT wert FROM einstellungen WHERE schluessel = ?", (schluessel,))
        row = cursor.fetchone()
        return row[0] if row else default


def get_einstellung_int(schluessel, default):
    wert = get_einstellung(schluessel, str(default))
    return int(wert) if wert.isdigit() else default


def set_einstellung(schluessel, wert):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO einstellungen (schluessel, wert) VALUES (?, ?)", (schluessel, wert))


# --- STAMMDATEN VERWALTEN (MENÜ) ---
def manage_stammdaten():
    while True:
        ueberschrift("⚙️ STAMMDATEN VERWALTEN")
        for idx, (key, label, _) in enumerate(STAMMDATEN, 1):
            print(f"{idx}. {label + ':':<46} {get_einstellung(key)}")
        zurueck = len(STAMMDATEN) + 1
        print(f"{zurueck}. Zurück zum Hauptmenü")

        wahl = input(f"\nWelchen Eintrag möchtest du ändern? (1-{zurueck}): ").strip()
        if wahl == str(zurueck):
            break
        if not wahl.isdigit() or not 1 <= int(wahl) < zurueck:
            print("Ungültige Eingabe.")
            continue

        key, label, pruefung = STAMMDATEN[int(wahl) - 1]
        neu = input(f"Neuer Wert für '{label}': ").strip()
        if not neu:
            continue
        if pruefung and not neu.isdigit():
            print("❌ Bitte eine ganze Zahl eingeben.")
            continue
        if pruefung == "positiv" and int(neu) < 1:
            print("❌ Der Wert muss mindestens 1 sein.")
            continue

        set_einstellung(key, neu)
        print(f"✅ {label} aktualisiert!")


# --- BUCHDATEN AUS DEM INTERNET LADEN ---
def neue_buchdaten(titel, autoren, verlag, jahr, genre):
    return {
        'titel': titel or 'Unbekannter Titel',
        'autoren': autoren or 'Unbekannt',
        'verlag': verlag or 'N/A',
        'erscheinungsjahr': jahr or 'N/A',
        'genre': genre or 'Keine Angabe',
    }


def fetch_from_google(isbn):
    params = f"q=isbn:{isbn}"
    if GOOGLE_API_KEY and GOOGLE_API_KEY != "DEIN_API_KEY_HIER":
        params += f"&key={urllib.parse.quote(GOOGLE_API_KEY)}"

    try:
        data = json.loads(http_get(f"https://www.googleapis.com/books/v1/volumes?{params}"))
        if data.get("items"):
            info = data["items"][0]["volumeInfo"]
            return neue_buchdaten(
                info.get('title'),
                ', '.join(info.get('authors', [])),
                info.get('publisher'),
                info.get('publishedDate', '')[:4],
                ', '.join(info.get('categories', [])),
            )
    except Exception as e:
        print(f"⚠️ Google-Books-Abfrage fehlgeschlagen: {e}")
    return None


def fetch_from_openlibrary(isbn):
    url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
    try:
        eintrag = json.loads(http_get(url)).get(f"ISBN:{isbn}")
        if eintrag:
            jahr = re.search(r"\d{4}", eintrag.get("publish_date", ""))
            return neue_buchdaten(
                eintrag.get("title"),
                ', '.join(a["name"] for a in eintrag.get("authors", [])),
                ', '.join(p["name"] for p in eintrag.get("publishers", [])),
                jahr.group(0) if jahr else None,
                ', '.join(s["name"] for s in eintrag.get("subjects", [])[:3]),
            )
    except Exception as e:
        print(f"⚠️ Open-Library-Abfrage fehlgeschlagen: {e}")
    return None


def fetch_book_data(isbn):
    for name, quelle in (("Google Books", fetch_from_google), ("Open Library", fetch_from_openlibrary)):
        daten = quelle(isbn)
        if daten:
            print(f"📡 Gefunden bei {name}.")
            return daten
    return None


# --- BUCHDATEN ANZEIGEN, PRÜFEN & BEARBEITEN ---
def zeige_buchdaten(daten):
    for feld, label in BUCH_FELDER:
        print(f"   {label + ':':<18} {daten[feld]}")


def fehlende_felder_abfragen(daten):
    fehlend = [(f, l) for f, l in BUCH_FELDER if daten[f].strip() in LEERWERTE]
    if not fehlend:
        return
    print("ℹ️ Einige Angaben fehlen ([ENTER] = überspringen):")
    for feld, label in fehlend:
        neu = input(f"   {label}: ").strip()
        if neu:
            daten[feld] = neu


def buchdaten_bearbeiten(daten):
    print("✏️ Neue Werte eingeben ([ENTER] = beibehalten):")
    for feld, label in BUCH_FELDER:
        neu = input(f"   {label} [{daten[feld]}]: ").strip()
        if neu:
            daten[feld] = neu


def buch_speichern(isbn, daten, anzahl=1):
    sort_key, formatiert = get_sort_name(daten['autoren'])
    with db_connection() as conn:
        conn.execute('''
            INSERT INTO buecher
            (isbn, titel, autoren, autor_nachname, autor_formatiert, verlag, erscheinungsjahr, genre, anzahl_gesamt, anzahl_verfuegbar)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            isbn, daten['titel'], daten['autoren'], sort_key, formatiert,
            daten['verlag'], daten['erscheinungsjahr'], daten['genre'], anzahl, anzahl
        ))
    print(f"✨ NEU HINZUGEFÜGT: {daten['titel']} ({formatiert}) – {anzahl}x")


def lade_buch(isbn):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT titel, autoren, verlag, erscheinungsjahr, genre, anzahl_gesamt, anzahl_verfuegbar
            FROM buecher WHERE isbn = ?
        ''', (isbn,))
        row = cursor.fetchone()
    if not row:
        return None
    daten = dict(zip([f for f, _ in BUCH_FELDER], row[:5]))
    daten['anzahl_gesamt'], daten['anzahl_verfuegbar'] = row[5], row[6]
    return daten


# --- BUCH SCANNEN UND HINZUFÜGEN ---
def scan_and_add():
    ueberschrift("📥 SCANNER-MODUS AKTIV")
    print("Scanne nacheinander deine Bücher.")
    print("Gib 'q' ein, um zum Hauptmenü zurückzukehren.")

    while True:
        try:
            eingabe = input("\n👉 Scanne/Gib ISBN ein (oder 'q' für Menü): ").strip()
            if eingabe.lower() == 'q':
                break
            if not eingabe:
                continue

            isbn = normalize_isbn(eingabe)
            if not isbn:
                print("❌ Ungültige ISBN – die Prüfziffer stimmt nicht. Bitte erneut scannen.")
                continue

            vorhanden = lade_buch(isbn)
            if vorhanden:
                with db_connection() as conn:
                    conn.execute('''
                        UPDATE buecher
                        SET anzahl_gesamt = anzahl_gesamt + 1, anzahl_verfuegbar = anzahl_verfuegbar + 1
                        WHERE isbn = ?
                    ''', (isbn,))
                print(f"✅ '{vorhanden['titel']}' aufgestockt -> Gesamt: {vorhanden['anzahl_gesamt'] + 1}x "
                      f"(Verfügbar: {vorhanden['anzahl_verfuegbar'] + 1}x)")
                continue

            print(f"🔍 Lade Daten für ISBN {isbn}...")
            daten = fetch_book_data(isbn)
            if not daten:
                print("❌ Kein Buch mit dieser ISBN gefunden.")
                if ja_nein("Möchtest du es von Hand eintragen?"):
                    manuelle_eingabe(isbn)
                continue

            zeige_buchdaten(daten)
            fehlende_felder_abfragen(daten)

            wahl = input("💾 [ENTER] = speichern, 'ä' = ändern, 'n' = verwerfen: ").strip().lower()
            if wahl == 'n':
                print("🗑️ Verworfen.")
                continue
            if wahl in ('ä', 'a'):
                buchdaten_bearbeiten(daten)
            buch_speichern(isbn, daten)

        except EOFError:
            raise
        except Exception as e:
            print(f"⚠️ Fehler: {e}")


# --- BUCH VON HAND EINTRAGEN ---
def manuelle_eingabe(isbn=None):
    if isbn is None:
        ueberschrift("✍️ BUCH VON HAND EINTRAGEN")
        isbn = lies_isbn("👉 ISBN des Buches: ")
        if not isbn:
            return
        if lade_buch(isbn):
            print("ℹ️ Dieses Buch ist schon im Katalog. Zum Aufstocken einfach im Scanner-Modus scannen.")
            return

    titel = input("   Titel: ").strip()
    if not titel:
        print("❌ Abgebrochen. Ein Titel ist erforderlich.")
        return

    daten = neue_buchdaten(
        titel,
        input("   Autor(en), mit Komma getrennt: ").strip(),
        input("   Verlag: ").strip(),
        input("   Erscheinungsjahr: ").strip(),
        input("   Genre: ").strip(),
    )
    anzahl_input = input("   Anzahl Exemplare [1]: ").strip()
    anzahl = int(anzahl_input) if anzahl_input.isdigit() and int(anzahl_input) > 0 else 1

    buch_speichern(isbn, daten, anzahl)


# --- KATALOG ANZEIGEN & SUCHEN ---
def zeige_buchliste(buecher):
    for autor_formatiert, titel, jahr, genre, gesamt, verfuegbar, isbn in buecher:
        print(f"• {autor_formatiert:<25} | {titel} ({jahr}) [Bestand: {verfuegbar}/{gesamt} verfügbar]")
        print(f"  Genre: {genre} | ISBN: {isbn}")
        print("-" * 75)


def show_sorted_catalog():
    with db_connection() as conn:
        buecher = conn.execute(f'''
            SELECT {BUCH_SPALTEN} FROM buecher
            ORDER BY autor_nachname ASC, titel ASC
        ''').fetchall()

    ueberschrift("📚 BÜCHEREI-KATALOG (SORTIERT NACH AUTOR-NACHNAME)", 75)
    if not buecher:
        print("Der Katalog ist leer.")
    else:
        zeige_buchliste(buecher)
    warte()


def search_books():
    ueberschrift("🔎 BUCH SUCHEN")
    suchbegriff = input("Titel, Autor, Genre oder ISBN: ").strip()
    if not suchbegriff:
        return

    isbn = normalize_isbn(suchbegriff)
    muster = f"%{isbn or suchbegriff}%"
    with db_connection() as conn:
        treffer = conn.execute(f'''
            SELECT {BUCH_SPALTEN} FROM buecher
            WHERE titel LIKE ? OR autoren LIKE ? OR genre LIKE ? OR isbn LIKE ?
            ORDER BY autor_nachname ASC, titel ASC
        ''', (muster, muster, muster, muster)).fetchall()

    print(f"\n{len(treffer)} Treffer für '{suchbegriff}':")
    print("-" * 75)
    zeige_buchliste(treffer)
    warte()


# --- BUCH BEARBEITEN / LÖSCHEN ---
def manage_book():
    ueberschrift("🛠️ BUCH BEARBEITEN / LÖSCHEN")
    isbn = lies_isbn("👉 Scanne/Gib die ISBN des Buches ein: ")
    if not isbn:
        return

    while True:
        daten = lade_buch(isbn)
        if not daten:
            print("❌ Dieses Buch existiert nicht im Katalog.")
            return

        print(f"\n📖 ISBN {isbn}")
        zeige_buchdaten(daten)
        print(f"   {'Bestand:':<18} {daten['anzahl_verfuegbar']}/{daten['anzahl_gesamt']} verfügbar")
        print("\n1. Buchdaten bearbeiten")
        print("2. Ein Exemplar entfernen (z. B. verloren oder kaputt)")
        print("3. Buch komplett löschen")
        print("4. Zurück zum Hauptmenü")
        wahl = input("\nWähle eine Option (1-4): ").strip()

        if wahl == "1":
            buchdaten_bearbeiten(daten)
            sort_key, formatiert = get_sort_name(daten['autoren'])
            with db_connection() as conn:
                conn.execute('''
                    UPDATE buecher SET titel = ?, autoren = ?, autor_nachname = ?, autor_formatiert = ?,
                        verlag = ?, erscheinungsjahr = ?, genre = ?
                    WHERE isbn = ?
                ''', (daten['titel'], daten['autoren'], sort_key, formatiert,
                      daten['verlag'], daten['erscheinungsjahr'], daten['genre'], isbn))
            print("✅ Buchdaten gespeichert!")

        elif wahl == "2":
            if daten['anzahl_gesamt'] <= 1:
                print("ℹ️ Das ist das letzte Exemplar – nutze 'Buch komplett löschen'.")
            elif daten['anzahl_verfuegbar'] <= 0:
                print("❌ Alle Exemplare sind ausgeliehen. Erst nach einer Rückgabe kann eins entfernt werden.")
            else:
                with db_connection() as conn:
                    conn.execute('''
                        UPDATE buecher SET anzahl_gesamt = anzahl_gesamt - 1, anzahl_verfuegbar = anzahl_verfuegbar - 1
                        WHERE isbn = ?
                    ''', (isbn,))
                print("✅ Ein Exemplar entfernt.")

        elif wahl == "3":
            with db_connection() as conn:
                aktiv = conn.execute(
                    "SELECT COUNT(*) FROM ausleihen WHERE isbn = ? AND status = 'aktiv'", (isbn,)
                ).fetchone()[0]
            if aktiv:
                print(f"❌ Das Buch ist noch {aktiv}x ausgeliehen und kann nicht gelöscht werden.")
            elif ja_nein(f"⚠️ '{daten['titel']}' samt Ausleih-Historie wirklich löschen?"):
                with db_connection() as conn:
                    conn.execute("DELETE FROM ausleihen WHERE isbn = ?", (isbn,))
                    conn.execute("DELETE FROM buecher WHERE isbn = ?", (isbn,))
                print("🗑️ Buch gelöscht.")
                return

        elif wahl == "4":
            return
        else:
            print("Ungültige Eingabe.")


# --- BUCH AUSLEIHEN ---
def book_checkout():
    ueberschrift("📤 BUCH AUSLEIHEN")

    isbn = lies_isbn("👉 Scanne/Gib die ISBN des Buches ein: ")
    if not isbn:
        return

    buch = lade_buch(isbn)
    if not buch:
        print("❌ Dieses Buch existiert nicht im Katalog. Bitte zuerst erfassen.")
        return

    titel, verfuegbar = buch['titel'], buch['anzahl_verfuegbar']
    if verfuegbar <= 0:
        print(f"❌ '{titel}' ist aktuell vollständig ausgeliehen!")
        return

    print(f"📖 Ausleihe für: '{titel}' (Verfügbar: {verfuegbar} Exemplar(e))")
    ausleiher = input("👤 Name des Ausleihers: ").strip()
    if not ausleiher:
        print("❌ Ausleihe abgebrochen. Name erforderlich.")
        return

    standard = get_einstellung_int("standard_leihdauer", 14)
    tage_input = input(f"📅 Ausleihdauer in Tagen [Standard: {standard}]: ").strip()
    tage = int(tage_input) if tage_input.isdigit() and int(tage_input) > 0 else standard

    ausgeliehen_am = heute()
    rueckgabe_datum = ausgeliehen_am + timedelta(days=tage)

    with db_connection() as conn:
        # Gleiche Schreibweise wie bei früheren Ausleihen verwenden ("max mustermann" → "Max Mustermann")
        bekannt = conn.execute(
            "SELECT ausleiher FROM ausleihen WHERE LOWER(ausleiher) = LOWER(?) LIMIT 1", (ausleiher,)
        ).fetchone()
        if bekannt:
            ausleiher = bekannt[0]

        conn.execute('''
            INSERT INTO ausleihen (isbn, ausleiher, ausgeliehen_am, rueckgabe_bis, status)
            VALUES (?, ?, ?, ?, 'aktiv')
        ''', (isbn, ausleiher, ausgeliehen_am.isoformat(), rueckgabe_datum.isoformat()))
        conn.execute("UPDATE buecher SET anzahl_verfuegbar = anzahl_verfuegbar - 1 WHERE isbn = ?", (isbn,))

    print("\n✅ AUSLEIHE ERFOLGREICH GESPEICHERT!")
    print(f"👤 Ausleiher:    {ausleiher}")
    print(f"📖 Buch:         {titel}")
    print(f"📅 Rückgabe bis: {rueckgabe_datum.strftime('%d.%m.%Y')}")


# --- AKTIVE AUSLEIHE AUSWÄHLEN (PER SCAN ODER LISTE) ---
def finde_aktive_ausleihe():
    eingabe = input("👉 Scanne die ISBN des Buches (oder [ENTER] für die Liste): ").strip()
    sql = '''
        SELECT a.id, a.isbn, b.titel, a.ausleiher, a.rueckgabe_bis
        FROM ausleihen a
        JOIN buecher b ON a.isbn = b.isbn
        WHERE a.status = 'aktiv'
    '''

    with db_connection() as conn:
        if eingabe:
            isbn = normalize_isbn(eingabe)
            if not isbn:
                print("❌ Ungültige ISBN – die Prüfziffer stimmt nicht.")
                return None
            ausleihen = conn.execute(sql + " AND a.isbn = ? ORDER BY a.rueckgabe_bis", (isbn,)).fetchall()
            if not ausleihen:
                print("❌ Für dieses Buch gibt es keine aktive Ausleihe.")
                return None
        else:
            ausleihen = conn.execute(sql + " ORDER BY a.rueckgabe_bis").fetchall()
            if not ausleihen:
                print("Es gibt aktuell keine ausgeliehenen Bücher.")
                return None

    if eingabe and len(ausleihen) == 1:
        return ausleihen[0]

    print("\nAktive Ausleihen:")
    return waehle_aus_liste(
        ausleihen,
        lambda a: f"'{a[2]}' | Geliehen von: {a[3]} (Rückgabe bis: {de_datum(a[4])})",
    )


# --- BUCH ZURÜCKGEBEN ---
def book_return():
    ueberschrift("📥 BUCH ZURÜCKGEBEN")

    ausleihe = finde_aktive_ausleihe()
    if not ausleihe:
        return

    ausleihe_id, isbn, titel, ausleiher, rueckgabe_bis = ausleihe
    with db_connection() as conn:
        conn.execute(
            "UPDATE ausleihen SET status = 'zurueckgegeben', zurueckgegeben_am = ? WHERE id = ?",
            (heute().isoformat(), ausleihe_id),
        )
        conn.execute("UPDATE buecher SET anzahl_verfuegbar = anzahl_verfuegbar + 1 WHERE isbn = ?", (isbn,))

    print(f"\n✅ '{titel}' von {ausleiher} wurde erfolgreich als zurückgegeben markiert!")
    verspaetung = (heute() - date.fromisoformat(rueckgabe_bis)).days
    if verspaetung > 0:
        print(f"⚠️ Die Rückgabe war {verspaetung} Tag(e) zu spät.")


# --- AUSLEIHE VERLÄNGERN ---
def book_extend():
    ueberschrift("🔁 AUSLEIHE VERLÄNGERN")

    ausleihe = finde_aktive_ausleihe()
    if not ausleihe:
        return

    ausleihe_id, _, titel, ausleiher, rueckgabe_bis = ausleihe
    standard = get_einstellung_int("standard_leihdauer", 14)
    tage_input = input(f"📅 Um wie viele Tage verlängern? [Standard: {standard}]: ").strip()
    tage = int(tage_input) if tage_input.isdigit() and int(tage_input) > 0 else standard

    # Bei überfälligen Büchern ab heute verlängern, sonst ab dem bisherigen Rückgabedatum
    basis = max(heute(), date.fromisoformat(rueckgabe_bis))
    neues_datum = basis + timedelta(days=tage)

    with db_connection() as conn:
        conn.execute('''
            UPDATE ausleihen SET rueckgabe_bis = ?, verlaengerungen = COALESCE(verlaengerungen, 0) + 1
            WHERE id = ?
        ''', (neues_datum.isoformat(), ausleihe_id))

    print(f"\n✅ '{titel}' ({ausleiher}) verlängert bis {neues_datum.strftime('%d.%m.%Y')}.")


# --- AUSLEIH-ÜBERSICHT & ÜBERFÄLLIGE BÜCHER ANZEIGEN ---
def show_loans():
    with db_connection() as conn:
        ausleihen = conn.execute('''
            SELECT b.titel, b.autor_formatiert, a.ausleiher, a.ausgeliehen_am, a.rueckgabe_bis,
                   COALESCE(a.verlaengerungen, 0)
            FROM ausleihen a
            JOIN buecher b ON a.isbn = b.isbn
            WHERE a.status = 'aktiv'
            ORDER BY a.rueckgabe_bis ASC
        ''').fetchall()

    ueberschrift("📋 AKTUELL AUSGELIEHENE BÜCHER", 75)

    if not ausleihen:
        print("Keine Bücher ausgeliehen. Alle Exemplare sind in der Bücherei.")
    else:
        ueberfaellig = sum(1 for a in ausleihen if date.fromisoformat(a[4]) < heute())
        print(f"{len(ausleihen)} Ausleihe(n), davon {ueberfaellig} überfällig.")
        print("-" * 75)
        for titel, autor, ausleiher, geliehen_am, rueckgabe_bis, verlaengerungen in ausleihen:
            print(f"📖 Buch:         {titel} ({autor})")
            print(f"👤 Geliehen von: {ausleiher}")
            print(f"📅 Geliehen am:  {de_datum(geliehen_am)}  |  Rückgabe bis: {de_datum(rueckgabe_bis)}"
                  + (f"  ({verlaengerungen}x verlängert)" if verlaengerungen else ""))
            print(f"📌 Status:       {faelligkeit_text(rueckgabe_bis)}")
            print("-" * 75)

    warte()


# --- ÜBERSICHT PRO AUSLEIHER ---
def show_borrower():
    ueberschrift("👤 AUSLEIHER-ÜBERSICHT")
    suche = input("Name (oder ein Teil davon): ").strip()
    if not suche:
        return

    with db_connection() as conn:
        namen = [row[0] for row in conn.execute(
            "SELECT DISTINCT ausleiher FROM ausleihen WHERE ausleiher LIKE ? ORDER BY ausleiher", (f"%{suche}%",)
        )]

    if not namen:
        print("❌ Niemand mit diesem Namen gefunden.")
        return
    name = namen[0] if len(namen) == 1 else waehle_aus_liste(namen, str)
    if not name:
        return

    with db_connection() as conn:
        eintraege = conn.execute('''
            SELECT b.titel, a.ausgeliehen_am, a.rueckgabe_bis, a.status, a.zurueckgegeben_am
            FROM ausleihen a
            JOIN buecher b ON a.isbn = b.isbn
            WHERE a.ausleiher = ?
            ORDER BY a.ausgeliehen_am DESC
        ''', (name,)).fetchall()

    aktiv = [e for e in eintraege if e[3] == 'aktiv']
    frueher = [e for e in eintraege if e[3] != 'aktiv']

    ueberschrift(f"👤 {name}", 75)
    print(f"📚 Aktuell ausgeliehen ({len(aktiv)}):")
    for titel, geliehen_am, rueckgabe_bis, _, _ in aktiv:
        print(f"   • {titel} – bis {de_datum(rueckgabe_bis)} – {faelligkeit_text(rueckgabe_bis)}")
    if not aktiv:
        print("   (nichts)")

    print(f"\n🕘 Frühere Ausleihen ({len(frueher)}):")
    for titel, geliehen_am, _, _, zurueck_am in frueher:
        print(f"   • {titel} – {de_datum(geliehen_am)} bis {de_datum(zurueck_am)}")
    if not frueher:
        print("   (keine)")

    warte()


# --- STATISTIK ---
def show_statistics():
    heute_iso = heute().isoformat()
    with db_connection() as conn:
        titel_anzahl, exemplare, verfuegbar = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(anzahl_gesamt), 0), COALESCE(SUM(anzahl_verfuegbar), 0) FROM buecher"
        ).fetchone()
        aktiv, ueberfaellig, ausleiher = conn.execute('''
            SELECT COUNT(*), COALESCE(SUM(rueckgabe_bis < ?), 0), COUNT(DISTINCT ausleiher)
            FROM ausleihen WHERE status = 'aktiv'
        ''', (heute_iso,)).fetchone()
        top = conn.execute('''
            SELECT b.titel, COUNT(*) AS anzahl
            FROM ausleihen a JOIN buecher b ON a.isbn = b.isbn
            GROUP BY a.isbn ORDER BY anzahl DESC, b.titel LIMIT 5
        ''').fetchall()

    ueberschrift("📊 STATISTIK")
    print(f"📚 Verschiedene Titel:     {titel_anzahl}")
    print(f"📦 Exemplare gesamt:       {exemplare}")
    print(f"✅ Davon verfügbar:        {verfuegbar}")
    print(f"📤 Aktive Ausleihen:       {aktiv} (an {ausleiher} Person(en))")
    print(f"⚠️ Davon überfällig:       {ueberfaellig}")

    print("\n🏆 Meistgeliehene Bücher:")
    for platz, (titel, anzahl) in enumerate(top, 1):
        print(f"   {platz}. {titel} ({anzahl}x)")
    if not top:
        print("   (noch keine Ausleihen)")

    warte()


# --- EXPORT ALS CSV (ÖFFNET SICH IN EXCEL) ---
def exportiere_csv(praefix, kopfzeile, zeilen):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    pfad = os.path.join(EXPORT_DIR, f"{praefix}_{datetime.now():%Y%m%d_%H%M%S}.csv")
    # Semikolon + BOM, damit ein deutsches Excel Spalten und Umlaute korrekt erkennt
    with open(pfad, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(kopfzeile)
        writer.writerows(zeilen)
    print(f"✅ {len(zeilen)} Zeile(n) exportiert nach: {pfad}")


def export_daten(typ):
    """Liefert (Kopfzeile, Zeilen) für 'katalog', 'ausleihen' oder 'mahnliste'."""
    with db_connection() as conn:
        if typ == "katalog":
            zeilen = conn.execute('''
                SELECT isbn, titel, autoren, verlag, erscheinungsjahr, genre, anzahl_gesamt, anzahl_verfuegbar
                FROM buecher ORDER BY autor_nachname, titel
            ''').fetchall()
            return ["ISBN", "Titel", "Autor(en)", "Verlag", "Jahr", "Genre", "Exemplare", "Verfügbar"], zeilen

        sql = '''
            SELECT a.ausleiher, b.titel, a.isbn, a.ausgeliehen_am, a.rueckgabe_bis
            FROM ausleihen a JOIN buecher b ON a.isbn = b.isbn
            WHERE a.status = 'aktiv'
        '''
        params = ()
        if typ == "mahnliste":
            sql += " AND a.rueckgabe_bis < ?"
            params = (heute().isoformat(),)
        rohdaten = conn.execute(sql + " ORDER BY a.ausleiher, a.rueckgabe_bis", params).fetchall()

    zeilen = [
        (name, titel, isbn, de_datum(von), de_datum(bis), max(0, (heute() - date.fromisoformat(bis)).days))
        for name, titel, isbn, von, bis in rohdaten
    ]
    return ["Ausleiher", "Titel", "ISBN", "Geliehen am", "Rückgabe bis", "Tage überfällig"], zeilen


def export_menu():
    ueberschrift("📄 EXPORT (CSV / EXCEL)")
    print("1. Gesamter Katalog")
    print("2. Aktuelle Ausleihen")
    print("3. Mahnliste (überfällige Bücher)")
    print("4. Zurück zum Hauptmenü")
    wahl = input("\nWähle eine Option (1-4): ").strip()

    typ = {"1": "katalog", "2": "ausleihen", "3": "mahnliste"}.get(wahl)
    if not typ:
        return
    kopf, zeilen = export_daten(typ)
    exportiere_csv(typ, kopf, zeilen)


# --- DATENBANK-BACKUP ---
def erstelle_backup(praefix="backup"):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    pfad = os.path.join(BACKUP_DIR, f"{praefix}_{datetime.now():%Y%m%d_%H%M%S}.db")
    src = sqlite3.connect(DB_NAME)
    dst = sqlite3.connect(pfad)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return pfad


def auto_backup():
    """Einmal pro Tag beim Start automatisch sichern."""
    if not os.path.exists(DB_NAME):
        return
    tages_praefix = f"auto_{datetime.now():%Y%m%d}"
    if os.path.isdir(BACKUP_DIR) and any(n.startswith(tages_praefix) for n in os.listdir(BACKUP_DIR)):
        return
    erstelle_backup("auto")
    alte_dateien_aufraeumen(BACKUP_DIR, "auto_", MAX_AUTO_BACKUPS)


def backup_menu():
    ueberschrift("💾 DATENBANK-BACKUP")
    pfad = erstelle_backup()
    print(f"✅ Backup gespeichert unter: {pfad}")
    print("Zum Wiederherstellen: Programm beenden und die Backup-Datei als")
    print(f"'{DB_FILENAME}' in den Programmordner kopieren.")


# --- DATENSCHUTZ: ALTE AUSLEIHEN LÖSCHEN ---
def alte_ausleihen_loeschen():
    monate = get_einstellung_int("loeschfrist_monate", 12)
    if monate <= 0:
        return
    grenze = (heute() - timedelta(days=monate * 30)).isoformat()
    with db_connection() as conn:
        geloescht = conn.execute('''
            DELETE FROM ausleihen
            WHERE status = 'zurueckgegeben' AND COALESCE(zurueckgegeben_am, rueckgabe_bis) < ?
        ''', (grenze,)).rowcount
    if geloescht:
        print(f"🔒 Datenschutz: {geloescht} abgeschlossene Ausleihe(n) älter als {monate} Monate gelöscht.")


# --- HAUPTMENÜ ---
MENUE = [
    ("Bücher scannen / Bestand aufstocken", scan_and_add),
    ("Buch von Hand eintragen", manuelle_eingabe),
    ("Gesamten Katalog anzeigen (nach Autor)", show_sorted_catalog),
    ("Buch suchen", search_books),
    ("Buch bearbeiten / löschen", manage_book),
    ("Buch AUSLEIHEN", book_checkout),
    ("Buch ZURÜCKGEBEN", book_return),
    ("Ausleihe VERLÄNGERN", book_extend),
    ("Ausleih-Übersicht & Überfällige Bücher", show_loans),
    ("Ausleiher-Übersicht", show_borrower),
    ("Statistik", show_statistics),
    ("Export (CSV / Excel)", export_menu),
    ("Datenbank-Backup erstellen", backup_menu),
    ("Stammdaten", manage_stammdaten),
    ("Nach Programm-Updates suchen (GitHub)", checkForUpdates),
]


def main():
    alte_datenbank_uebernehmen()
    auto_backup()
    init_db()
    alte_ausleihen_loeschen()

    while True:
        bib_name = get_einstellung("bib_name", "Schulbibliothek")
        print(f"\n=== 📚 ScanBook {VERSION} - {bib_name} ===")
        for idx, (text, _) in enumerate(MENUE, 1):
            print(f"{idx:>2}. {text}")
        print(" 0. Beenden")

        wahl = input(f"\nWähle eine Option (0-{len(MENUE)}): ").strip()

        if wahl == "0":
            print("\nProgramm beendet. Auf Wiedersehen!")
            break
        if not wahl.isdigit() or not 1 <= int(wahl) <= len(MENUE):
            print(f"Ungültige Option, bitte 0 bis {len(MENUE)} wählen.")
            continue

        try:
            MENUE[int(wahl) - 1][1]()
        except KeyboardInterrupt:
            print("\n↩️ Abgebrochen – zurück zum Hauptmenü.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n\nProgramm beendet. Auf Wiedersehen!")

import os
import re
import html
import time
import threading
import sys
import csv
import json
import ssl
import bisect
import shutil
import sqlite3
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, date, timedelta

# 🏷️ PROGRAMMVERSION – bei jeder neuen Version auf GitHub erhöhen, sonst wird kein Update erkannt!
VERSION = "2.8.0"

# 🔑 OPTIONAL: GOOGLE API KEY ALS UMGEBUNGSVARIABLE SETZEN (ohne Schlüssel gibt es weniger Abfragen pro Tag).
# Keinen Schlüssel hier eintragen – die Datei ist öffentlich auf GitHub!
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

# 📁 ALLE DATEIEN LIEGEN IM ORDNER DES PROGRAMMS (egal, von wo es gestartet wird)
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
DB_FILENAME = "buecherei_system.db"
DB_NAME = os.path.join(BASE_DIR, DB_FILENAME)
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
EXPORT_DIR = os.path.join(BASE_DIR, "exporte")

MAX_AUTO_BACKUPS = 14
BACKUP_ORDNER_EINSTELLUNG = "backup_ordner"  # optionaler zweiter Ordner, z. B. USB-Stick oder OneDrive
MAX_UPDATE_BACKUPS = 3

# 🌐 GITHUB DETAILS FÜR AUTOMATISCHE UPDATES:
GITHUB_REPO = "djdenioooo/scanbook"
GITHUB_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RAW_TEMPLATE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{{ref}}/{{datei}}"
# Dateien, die beim Update ersetzt werden (Datei auf GitHub, Präfix für Backups)
UPDATE_DATEIEN = [("ScanBook.py", "ScanBook_"), ("ScanBook_Web.py", "ScanBookWeb_")]

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

# Freiwillige Angaben – leer heißt "gehört zu keiner Serie"
SERIEN_FELDER = [
    ("serie", "Serie"),
    ("band", "Band"),
]

# Angaben, die online gefunden werden (Beschreibung lässt sich im Browser auch von Hand ändern)
ZUSATZ_FELDER = ["beschreibung", "cover_url"]

STAMMDATEN = [
    ("schulname", "Schulname", None),
    ("bib_name", "Bibliotheksname", None),
    ("adresse", "Adresse", None),
    ("kontakt_email", "E-Mail", None),
    ("standard_leihdauer", "Standard-Leihdauer (Tage)", "positiv"),
    ("loeschfrist_monate", "Löschfrist alter Ausleihen (Monate, 0 = nie)", "zahl"),
    ("ausweis_praefix", "Vorsilbe für Ausweisnummern", None),
    ("mediencode_praefix", "Vorsilbe für Mediencodes", None),
]

DEFAULT_EINSTELLUNGEN = {
    "schulname": "Muster-Gymnasium",
    "bib_name": "Schulbibliothek",
    "adresse": "Schulstraße 1, 12345 Musterstadt",
    "kontakt_email": "bibliothek@schule.de",
    "standard_leihdauer": "14",
    "loeschfrist_monate": "12",
    "ausweis_praefix": "S",
    "mediencode_praefix": "M",
}

BUCH_SPALTEN = "autor_formatiert, titel, erscheinungsjahr, genre, anzahl_gesamt, anzahl_verfuegbar, isbn, serie, band"


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


# Zertifikatsdateien, die es je nach System gibt (macOS, Linux)
ZERTIFIKAT_DATEIEN = ["/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"]
_ssl_kontext = None


def ssl_kontext():
    """Prüft Zertifikate immer, ergänzt aber fehlende Stammzertifikate.

    Python von python.org bringt auf dem Mac keine Zertifikate mit, und unter Windows
    fehlen manchmal Stammzertifikate. Dann scheitert z. B. die DNB-Abfrage mit
    CERTIFICATE_VERIFY_FAILED. Zusätzlich geladen werden das Paket certifi (falls
    installiert) und die Zertifikatsdatei des Betriebssystems."""
    global _ssl_kontext
    if _ssl_kontext is None:
        kontext = ssl.create_default_context()
        dateien = list(ZERTIFIKAT_DATEIEN)
        try:
            import certifi
            dateien.insert(0, certifi.where())
        except ImportError:
            pass
        for datei in dateien:
            if os.path.isfile(datei):
                try:
                    kontext.load_verify_locations(cafile=datei)
                except (ssl.SSLError, OSError):
                    pass
        _ssl_kontext = kontext
    return _ssl_kontext


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_kontext()) as response:
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


def update_quelle():
    """Bevorzugt das neueste offizielle Release, sonst den main-Branch."""
    try:
        tag = json.loads(http_get(GITHUB_RELEASE_API))["tag_name"]
        return f"Release {tag}", tag
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    # Noch kein Release vorhanden → main-Branch
    return "main-Branch", "refs/heads/main"


def hole_update_datei(ref, datei):
    return http_get(GITHUB_RAW_TEMPLATE.format(ref=ref, datei=datei))


def neu_starten(skript):
    # os.execv setzt unter Windows Pfade mit Leerzeichen nicht in Anführungszeichen
    if os.name == "nt":
        sys.exit(subprocess.call([sys.executable, skript] + sys.argv[1:]))
    os.execv(sys.executable, [sys.executable, skript] + sys.argv[1:])


def update_suchen():
    """Liefert (Version auf GitHub, Quelle, Dateien). Dateien ist None, wenn kein neueres Update da ist.
    Alle Dateien werden geladen und geprüft, bevor irgendetwas ersetzt wird."""
    quelle, ref = update_quelle()
    remote_code = hole_update_datei(ref, "ScanBook.py")
    remote_version = lies_version(remote_code)
    if not remote_version:
        raise ValueError("Die Datei auf GitHub enthält keine Versionsnummer – Update wird nicht installiert.")
    if version_tupel(remote_version) <= version_tupel(VERSION):
        return remote_version, quelle, None

    neue_dateien = []
    for datei, backup_praefix in UPDATE_DATEIEN:
        code = remote_code if datei == "ScanBook.py" else hole_update_datei(ref, datei)
        try:
            compile(code, datei, "exec")
        except SyntaxError as e:
            raise ValueError(f"Die neue Version ist fehlerhaft ({e}) – Update wird nicht installiert.")
        neue_dateien.append((os.path.join(BASE_DIR, datei), backup_praefix, code))
    return remote_version, quelle, neue_dateien


def update_installieren(neue_dateien):
    """Sichert die alten Programmdateien (nur die letzten paar bleiben) und schreibt die neuen.
    Liefert die Namen der Backup-Dateien."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backups = []
    for pfad, backup_praefix, _ in neue_dateien:
        if not os.path.exists(pfad):
            continue
        backup_file = os.path.join(BACKUP_DIR, f"{backup_praefix}{timestamp}_v{VERSION}.py")
        with open(pfad, "r", encoding="utf-8") as f:
            local_code = f.read()
        with open(backup_file, "w", encoding="utf-8") as f:
            f.write(local_code)
        alte_dateien_aufraeumen(BACKUP_DIR, backup_praefix, MAX_UPDATE_BACKUPS)
        backups.append(os.path.basename(backup_file))

    for pfad, _, code in neue_dateien:
        with open(pfad, "w", encoding="utf-8") as f:
            f.write(code)
    return backups


def checkForUpdates():
    ueberschrift("🔄 SUCHE NACH UPDATES AUF GITHUB...")
    print(f"Installierte Version: {VERSION}")

    try:
        remote_version, quelle, neue_dateien = update_suchen()
        if not neue_dateien:
            print("✅ Das Programm ist bereits auf dem neuesten Stand!")
            warte()
            return

        print(f"✨ Neue Version {remote_version} gefunden ({quelle})!")
        if not ja_nein("Möchtest du das Update jetzt installieren?"):
            print("Update abgebrochen.")
            return

        for name in update_installieren(neue_dateien):
            print(f"📦 Backup gespeichert unter: backups/{name}")
        print("🚀 Update erfolgreich installiert!")
        print("🔄 Das Programm wird jetzt neu gestartet...\n")
        neu_starten(os.path.realpath(__file__))

    except ValueError as e:
        print(f"⚠️ {e}")
        warte()
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
                anzahl_verfuegbar INTEGER DEFAULT 1,
                serie TEXT DEFAULT '',
                band TEXT DEFAULT '',
                beschreibung TEXT DEFAULT '',
                cover_url TEXT DEFAULT '',
                nachgeladen INTEGER DEFAULT 0,
                regal_id INTEGER,
                fach INTEGER
            )
        ''')

        # Regale: genres = mit Komma getrennt, leer = Regal für alle übrigen Bücher
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS regale (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                faecher INTEGER DEFAULT 1,
                kapazitaet INTEGER DEFAULT 30,
                genres TEXT DEFAULT '',
                reihenfolge INTEGER DEFAULT 0
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

        # Schüler mit Ausweisnummer (steht als Barcode auf dem Schülerausweis)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS schueler (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ausweis TEXT UNIQUE COLLATE NOCASE,
                vorname TEXT,
                nachname TEXT,
                klasse TEXT DEFAULT ''
            )
        ''')

        # Mediencodes: eigene Nummer pro Exemplar, als Etikett im Buch (eigener = schon vorhandenes Etikett)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS exemplare (
                code TEXT PRIMARY KEY COLLATE NOCASE,
                isbn TEXT,
                eigener INTEGER DEFAULT 0,
                gedruckt INTEGER DEFAULT 0,
                FOREIGN KEY(isbn) REFERENCES buecher(isbn)
            )
        ''')
        cursor.execute("CREATE INDEX IF NOT EXISTS exemplare_isbn ON exemplare(isbn)")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS einstellungen (
                schluessel TEXT PRIMARY KEY,
                wert TEXT
            )
        ''')

        # Spalten nachrüsten, die ältere Versionen noch nicht hatten
        buch_spalten = {row[1] for row in cursor.execute("PRAGMA table_info(buecher)")}
        for spalte in ("serie", "band", "beschreibung", "cover_url"):
            if spalte not in buch_spalten:
                cursor.execute(f"ALTER TABLE buecher ADD COLUMN {spalte} TEXT DEFAULT ''")
        if "nachgeladen" not in buch_spalten:
            # 0 = Beschreibung, Cover und Serie wurden noch nicht online gesucht
            cursor.execute("ALTER TABLE buecher ADD COLUMN nachgeladen INTEGER DEFAULT 0")
        # Standort, an dem das Buch tatsächlich steht (leer = noch nicht eingeräumt)
        for spalte in ("regal_id", "fach"):
            if spalte not in buch_spalten:
                cursor.execute(f"ALTER TABLE buecher ADD COLUMN {spalte} INTEGER")

        vorhandene_spalten = {row[1] for row in cursor.execute("PRAGMA table_info(ausleihen)")}
        if "zurueckgegeben_am" not in vorhandene_spalten:
            cursor.execute("ALTER TABLE ausleihen ADD COLUMN zurueckgegeben_am DATE")
        if "verlaengerungen" not in vorhandene_spalten:
            cursor.execute("ALTER TABLE ausleihen ADD COLUMN verlaengerungen INTEGER DEFAULT 0")
        # Wer (Schüler) und welches Exemplar (Mediencode) – leer bei Ausleihen per Name bzw. ISBN
        if "schueler_id" not in vorhandene_spalten:
            cursor.execute("ALTER TABLE ausleihen ADD COLUMN schueler_id INTEGER")
        if "exemplar" not in vorhandene_spalten:
            cursor.execute("ALTER TABLE ausleihen ADD COLUMN exemplar TEXT")

        # Indizes, damit Schülerliste, Ausleihen und Buchvorschau auch bei vielen Einträgen sofort da sind
        cursor.execute("CREATE INDEX IF NOT EXISTS ausleihen_status ON ausleihen(status, rueckgabe_bis)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ausleihen_schueler ON ausleihen(schueler_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ausleihen_isbn ON ausleihen(isbn, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ausleihen_name ON ausleihen(ausleiher)")

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
def zertifikat_hinweis(fehler):
    if "CERTIFICATE_VERIFY_FAILED" in str(fehler):
        print("   💡 Deinem Python fehlen Sicherheitszertifikate. Abhilfe: im Terminal")
        print("      'python3 -m pip install certifi' (Windows: 'py -m pip install certifi') ausführen")
        print("      und ScanBook neu starten.")


# --- GENRE ERKENNEN ---
MAX_GENRES = 3

# Begriffe aus Google Books, Open Library und DNB -> deutsches Genre (spezielle Muster zuerst)
GENRE_MUSTER = [
    (r"juvenile|young adult|children|kinder|jugend", "Kinder- und Jugendbuch"),
    (r"science.fiction", "Science-Fiction"),
    (r"fantasy", "Fantasy"),
    (r"historical fiction|historische[rn]? roman", "Historischer Roman"),
    (r"crime|mystery|detective|krimi|detektiv", "Krimi"),
    (r"thriller|suspense", "Thriller"),
    (r"horror|gruselgeschichte", "Horror"),
    (r"romance|liebesroman|liebesgeschichte", "Liebesroman"),
    (r"comic|graphic novel|manga", "Comic"),
    (r"poetry|lyrik|gedicht", "Lyrik"),
    (r"^drama|theater|schauspiel", "Drama"),
    (r"humou?r|satire", "Humor"),
    (r"biograph|biograf|memoir|autobiogra", "Biografie"),
    (r"cook|kochbuch|kochen|rezept", "Kochbuch"),
    (r"travel|reiseführer|reisebericht", "Reisen"),
    (r"self-help|ratgeber|lebenshilfe", "Ratgeber"),
    (r"psycholog", "Psychologie"),
    (r"philosoph", "Philosophie"),
    (r"religio|theolog|bible|bibel", "Religion"),
    (r"political science|politik", "Politik"),
    (r"business|economics|wirtschaft", "Wirtschaft"),
    (r"computer|informatik|programmier", "Informatik"),
    (r"health|medic|gesundheit|medizin", "Gesundheit"),
    (r"\bsports?\b", "Sport"),
    (r"music|musik", "Musik"),
    (r"^art\b|\bkunst", "Kunst"),
    (r"^history|^geschichte", "Geschichte"),
    (r"^science$|^nature$|naturwissenschaft", "Naturwissenschaften"),
    (r"fiction|roman$|belletristik|erzählung|novelle|fiktionale darstellung", "Roman"),
]

# Genaue Romangenres – daneben ist "Roman" überflüssig
ROMAN_GENRES = {"Science-Fiction", "Fantasy", "Historischer Roman", "Krimi", "Thriller", "Horror", "Liebesroman"}

# DNB-Sachgruppen (Buchstaben) und DDC-Nummern -> Genre
DNB_SACHGRUPPEN = {"B": "Roman", "K": "Kinder- und Jugendbuch", "S": "Schulbuch"}
DDC_GENRES = [  # (Anfang der DDC-Nummer, Genre) – längster Treffer gewinnt
    ("0", "Sachbuch"), ("004", "Informatik"), ("005", "Informatik"),
    ("1", "Philosophie"), ("13", "Ratgeber"), ("15", "Psychologie"),
    ("2", "Religion"),
    ("3", "Gesellschaft"), ("32", "Politik"), ("33", "Wirtschaft"), ("34", "Recht"),
    ("37", "Pädagogik"), ("39", "Bräuche & Folklore"),
    ("4", "Sprache"),
    ("5", "Naturwissenschaften"), ("51", "Mathematik"),
    ("6", "Technik"), ("61", "Gesundheit"), ("63", "Garten & Landwirtschaft"), ("64", "Haushalt"),
    ("641", "Kochbuch"), ("65", "Wirtschaft"),
    ("7", "Kunst"), ("741.5", "Comic"), ("78", "Musik"), ("79", "Sport & Freizeit"),
    ("8", "Literatur"), ("8x1", "Lyrik"), ("8x2", "Drama"), ("8x3", "Roman"), ("8x7", "Humor"),
    ("9", "Geschichte"), ("91", "Reisen"), ("92", "Biografie"),
]

# Open-Library-Subjects, die nichts über das Genre sagen
GENRE_RAUSCHEN = re.compile(r"accessible book|protected daisy|in library|lending library|"
                            r"^nyt:|award|large type|^open library|^long now|reading level|"
                            r"^\d|translations into", re.I)


def genre_aus_text(begriff):
    begriff = (begriff or "").strip().lower()
    return [genre for muster, genre in GENRE_MUSTER if re.search(muster, begriff)]


def genre_aus_ddc(nummer):
    nummer = (nummer or "").strip().upper()
    if nummer in DNB_SACHGRUPPEN:
        return DNB_SACHGRUPPEN[nummer]
    if not re.match(r"\d", nummer):
        return None
    beste, laenge = None, 0
    for anfang, genre in DDC_GENRES:
        if "x" in anfang:  # Literatur einer beliebigen Sprache, z. B. 833 = deutscher Roman
            passt = len(nummer) >= 3 and nummer[0] == "8" and nummer[1] != "0" and nummer[2] == anfang[2]
        else:
            passt = nummer.startswith(anfang)
        if passt and len(anfang) > laenge:
            beste, laenge = genre, len(anfang)
    return beste


def genres_zusammenfuehren(*listen):
    ergebnis = []
    for liste in listen:
        for genre in liste:
            if genre and genre not in ergebnis:
                ergebnis.append(genre)
    # Allgemeine Angaben weglassen, wenn schon ein genaueres Genre da ist
    if len(ergebnis) > 1:
        ergebnis = [g for g in ergebnis if g not in ("Literatur", "Sachbuch")] or ergebnis
    if any(g in ROMAN_GENRES for g in ergebnis):
        ergebnis = [g for g in ergebnis if g != "Roman"]
    return ergebnis[:MAX_GENRES]


def genre_ermitteln(begriffe, klassen=(), schlagwoerter=()):
    """Liefert (Genres, sicher). 'sicher' = aus einer Einordnung erkannt statt nur Schlagwörter übernommen."""
    erkannt = genres_zusammenfuehren(
        [genre_aus_ddc(k) for k in klassen],
        [g for b in begriffe for g in genre_aus_text(b)],
    )
    if erkannt:
        return erkannt, erkannt != ["Roman"]  # nur "Roman" -> andere Quellen nach Genaurem fragen
    roh = [b.strip() for b in list(begriffe) + list(schlagwoerter)
           if b and b.strip() and not GENRE_RAUSCHEN.search(b)]
    return genres_zusammenfuehren(roh), False


def neue_buchdaten(titel, autoren, verlag, jahr, genre, serie=None, band=None, beschreibung=None, cover_url=None):
    serie = (serie or "").strip()
    return {
        'titel': titel or 'Unbekannter Titel',
        'autoren': autoren or 'Unbekannt',
        'verlag': verlag or 'N/A',
        'erscheinungsjahr': jahr or 'N/A',
        'genre': genre or 'Keine Angabe',
        'serie': serie,
        'band': band_normalisieren(band) if serie else '',
        'beschreibung': (beschreibung or '').strip(),
        'cover_url': (cover_url or '').strip(),
    }


# --- SERIEN ERKENNEN ---
# Verlagsreihen wie "dtv ; 14040" sind keine Buchserien und werden ignoriert
VERLAGSREIHEN = re.compile(
    r"^(dtv|rororo|btb|kiwi|goldmann|heyne|fischer|ullstein|piper|knaur|diogenes|blanvalet|"
    r"bastei|lübbe|suhrkamp|insel|reclam|aufbau|droemer|list|cbj|cbt|ravensburger|carlsen|"
    r"arena|oetinger|beltz|fischer klassik|penguin|vintage)\b|taschenbuch|bibliothek|edition|"
    r"universal-bibliothek|paperback|klassiker", re.I)


def band_normalisieren(band):
    """'Bd. 3' -> '3', '[Bd. 2].' -> '2'; ohne Zahl bleibt der Text (z. B. 'Sonderband')."""
    band = marc_text(str(band or "")).strip("[](). ")
    zahl = re.search(r"\d+", band)
    if not zahl:
        return band
    return zahl.group(0).lstrip("0") or "0"


def band_sortierwert(band):
    zahl = re.match(r"\d+", band or "")
    return (0, int(zahl.group(0)), band) if zahl else (1, 0, band or "")


def ist_buchserie(name, band=""):
    if not name or VERLAGSREIHEN.search(name):
        return False
    zahl = re.search(r"\d+", band or "")
    return not (zahl and int(zahl.group(0)) >= 1000)  # vierstellige Nummern = Verlagsreihe


def serie_aus_titel(titel):
    """Erkennt Titel wie 'Die Tribute von Panem 2. Gefährliche Liebe',
    'Harry Potter 2: Harry Potter und die Kammer des Schreckens' oder '... (Warrior Cats, Band 3)'."""
    titel = titel or ""
    treffer = re.search(r"\(([^()]+?)[,;:]?\s+(?:Bd\.?|Band|Teil|Folge|Buch|Book|Vol\.?|Volume|Nr\.?|#)\s*(\d{1,3})\)", titel)
    if treffer:
        return treffer.group(1).strip(), treffer.group(2)
    treffer = re.match(r"^(.+?)[,\s]+(?:(?:Bd\.?|Band|Teil|Folge|Buch|Book|Vol\.?|Volume|Nr\.?|#)\s*)?(\d{1,2})\s*([.:])\s+(.+)$", titel)
    if treffer:
        serie, band, trenner, rest = treffer.groups()
        serie = serie.strip()
        if re.match(r"(Klasse|Schuljahr|Jahrgang|Auflage|Jahrhundert|Kapitel)\b", rest, re.I):
            return None, None  # z. B. "Mathe 7. Klasse"
        if len(serie.split()) < 2 and (len(serie) < 5 or serie.lower() in ("der", "die", "das", "the")):
            return None, None  # z. B. "Die 5. Welle"
        # Bei ":" nur, wenn der Serienname danach wiederkommt (sonst z. B. "Catch 22: ...")
        if trenner == "." or serie.split()[0].lower() in rest.lower():
            return serie, band
    return None, None


def text_ohne_html(text):
    text = re.sub(r"<br\s*/?>|</p>", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --- QUELLEN ---
def fetch_from_google(isbn):
    params = f"q=isbn:{isbn}"
    if GOOGLE_API_KEY and GOOGLE_API_KEY != "DEIN_API_KEY_HIER":
        params += f"&key={urllib.parse.quote(GOOGLE_API_KEY)}"

    try:
        data = json.loads(http_get(f"https://www.googleapis.com/books/v1/volumes?{params}"))
        if data.get("items"):
            info = data["items"][0]["volumeInfo"]
            # Kategorien wie "Fiction / Fantasy / Epic" in Einzelteile zerlegen
            begriffe = [t for k in info.get('categories', []) for t in k.split("/")]
            genres, sicher = genre_ermitteln(begriffe)
            titel = info.get('title')
            serie, band = serie_aus_titel(" ".join(filter(None, [titel, info.get('subtitle')])))
            if serie and not ist_buchserie(serie, band):
                serie, band = None, None
            bilder = info.get('imageLinks', {})
            cover = bilder.get('thumbnail') or bilder.get('smallThumbnail') or ''
            cover = cover.replace("http://", "https://", 1).replace("&edge=curl", "")
            daten = neue_buchdaten(
                titel,
                ', '.join(info.get('authors', [])),
                info.get('publisher'),
                info.get('publishedDate', '')[:4],
                ', '.join(genres),
                serie, band,
                text_ohne_html(info.get('description')),
                cover,
            )
            daten['_genres'], daten['_genre_sicher'] = genres, sicher
            return daten
    except Exception as e:
        print(f"⚠️ Google-Books-Abfrage fehlgeschlagen: {e}")
        fehler_merken("Google Books")
        zertifikat_hinweis(e)
    return None


def fetch_from_openlibrary(isbn):
    url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
    try:
        eintrag = json.loads(http_get(url)).get(f"ISBN:{isbn}")
        if eintrag:
            jahr = re.search(r"\d{4}", eintrag.get("publish_date", ""))
            genres, sicher = genre_ermitteln([s["name"] for s in eintrag.get("subjects", [])])
            serie, band = serie_aus_titel(eintrag.get("title"))
            if serie and not ist_buchserie(serie, band):
                serie, band = None, None
            cover = eintrag.get("cover", {})
            daten = neue_buchdaten(
                eintrag.get("title"),
                ', '.join(a["name"] for a in eintrag.get("authors", [])),
                ', '.join(p["name"] for p in eintrag.get("publishers", [])),
                jahr.group(0) if jahr else None,
                ', '.join(genres),
                serie, band,
                None,
                cover.get("medium") or cover.get("large") or cover.get("small"),
            )
            daten['_genres'], daten['_genre_sicher'] = genres, sicher
            return daten
    except Exception as e:
        print(f"⚠️ Open-Library-Abfrage fehlgeschlagen: {e}")
        fehler_merken("Open Library")
        zertifikat_hinweis(e)
    return None


DNB_SRU_URL = "https://services.dnb.de/sru/dnb"
MARC_NS = {"m": "http://www.loc.gov/MARC21/slim"}


def marc_unterfelder(feld, codes):
    return [(u.text or "").strip() for u in feld.findall("m:subfield", MARC_NS) if u.get("code") in codes]


def marc_text(text):
    """Entfernt Nichtsortierzeichen und Trennzeichen, die MARC ans Feldende hängt."""
    text = re.sub(r"[\u0098\u009c]", "", text or "")
    return re.sub(r"\s*[/:;,=]+$", "", text.strip()).strip()


def marc_person(name):
    """'Tolkien, J. R. R.' -> 'J. R. R. Tolkien' (so wie bei Google Books)."""
    name = marc_text(name)
    if name.count(",") == 1:
        nachname, vorname = (t.strip() for t in name.split(","))
        return f"{vorname} {nachname}"
    return name


def parse_dnb_marc(record):
    def felder(tag):
        return record.findall(f"m:datafield[@tag='{tag}']", MARC_NS)

    titel = None
    for feld in felder("245"):
        # "[Bd. 2]." -> "Bd. 2", damit der Titel nicht "Panem. [Bd. 2].. Gefährliche Liebe" lautet
        teile = [marc_text(t).strip("[] ").rstrip(".") for t in marc_unterfelder(feld, "anp")]
        untertitel = [marc_text(t) for t in marc_unterfelder(feld, "b")]
        titel = ". ".join(t for t in teile if t)
        if untertitel and untertitel[0]:
            titel += f": {untertitel[0]}"

    autoren = []
    for feld in felder("100") + felder("700"):
        if marc_unterfelder(feld, "t"):
            continue  # Verweis auf ein anderes Werk (z. B. Originaltitel)
        rollen = marc_unterfelder(feld, "4e")
        if feld.get("tag") == "700" and not any(r in ("aut", "Verfasser") for r in rollen):
            continue  # Übersetzer, Illustratoren usw.
        for name in marc_unterfelder(feld, "a"):
            person = marc_person(name)
            if person and person not in autoren:
                autoren.append(person)
    if not autoren:
        autoren = [marc_text(n) for f in felder("110") for n in marc_unterfelder(f, "a")]

    verlag, jahr = None, None
    veroeffentlichung = [f for f in felder("264") if f.get("ind2") == "1"] + felder("260")
    for feld in veroeffentlichung:
        verlag = verlag or ", ".join(marc_text(v) for v in marc_unterfelder(feld, "b")) or None
        treffer = re.search(r"\d{4}", " ".join(marc_unterfelder(feld, "c")))
        jahr = jahr or (treffer.group(0) if treffer else None)
    if not jahr:
        feld008 = record.find("m:controlfield[@tag='008']", MARC_NS)
        if feld008 is not None and re.fullmatch(r"\d{4}", (feld008.text or "")[7:11]):
            jahr = feld008.text[7:11]

    schlagwoerter = []
    for feld in felder("650") + felder("689"):
        for wort in marc_unterfelder(feld, "a"):
            wort = marc_text(wort)
            if wort and wort not in schlagwoerter:
                schlagwoerter.append(wort)

    # Sachgruppe/DDC (z. B. "B" = Belletristik, "833" = deutscher Roman) und Gattung (z. B. "Roman", "Fantasy")
    klassen = [k for tag in ("082", "083", "084") for f in felder(tag) for k in marc_unterfelder(f, "a")]
    gattungen = [marc_text(g) for f in felder("655") for g in marc_unterfelder(f, "a")]

    if not titel:
        return None

    # Serie: Gesamttitel (830/800/490), sonst mehrteiliges Werk wie "Die Tribute von Panem. [Bd. 2]. Gefährliche Liebe"
    serie, band = None, None
    for tag in ("830", "800", "810", "490"):
        for feld in felder(tag):
            name = marc_text(" ".join(marc_unterfelder(feld, "t" if tag in ("800", "810") else "a")))
            nummer = " ".join(marc_unterfelder(feld, "v"))
            if not serie and ist_buchserie(name, nummer):
                serie, band = name, nummer
    if not serie:
        for feld in felder("245"):
            name = marc_text(" ".join(marc_unterfelder(feld, "a")))
            nummer = marc_unterfelder(feld, "n")
            if name and nummer:
                serie, band = name, nummer[0]

    beschreibung = "\n\n".join(t for f in felder("520") for t in marc_unterfelder(f, "a") if t)

    genres, sicher = genre_ermitteln(gattungen, klassen, schlagwoerter)
    daten = neue_buchdaten(titel, ', '.join(autoren), verlag, jahr, ', '.join(genres),
                           serie, band, beschreibung)
    daten['_genres'], daten['_genre_sicher'] = genres, sicher
    return daten


def fetch_from_dnb(isbn):
    params = urllib.parse.urlencode({
        "version": "1.1",
        "operation": "searchRetrieve",
        "query": f"num={isbn}",
        "recordSchema": "MARC21-xml",
        "maximumRecords": "1",
    })
    try:
        wurzel = ET.fromstring(http_get(f"{DNB_SRU_URL}?{params}"))
        record = wurzel.find(".//m:record", MARC_NS)
        if record is not None:
            return parse_dnb_marc(record)
    except Exception as e:
        print(f"⚠️ DNB-Abfrage fehlgeschlagen: {e}")
        fehler_merken("Deutsche Nationalbibliothek")
        zertifikat_hinweis(e)
    return None


# Quellen, die bei der letzten Suche (in diesem Thread) nicht erreichbar waren
abfrage = threading.local()


def fehler_merken(quelle):
    if not hasattr(abfrage, "fehler"):
        abfrage.fehler = []
    abfrage.fehler.append(quelle)


def letzte_fehler():
    return getattr(abfrage, "fehler", [])

# Was jede Quelle liefern kann – eine Quelle wird nur gefragt, wenn sie noch etwas Fehlendes ergänzen kann
QUELLEN = (
    ("Google Books", fetch_from_google, {"buch", "genre", "serie", "beschreibung", "cover_url"}),
    ("Deutsche Nationalbibliothek", fetch_from_dnb, {"buch", "genre", "serie", "beschreibung"}),
    ("Open Library", fetch_from_openlibrary, {"buch", "genre", "serie", "cover_url"}),
)


def fehlende_angaben(daten, genre_sicher):
    if not daten:
        return {"buch"}
    fehlt = {f for f in ("serie", "beschreibung", "cover_url") if not daten[f]}
    if not genre_sicher:
        fehlt.add("genre")
    return fehlt


def buchdaten_ergaenzen(daten, treffer):
    """Füllt leere Angaben in 'daten' aus 'treffer' auf. Liefert die Namen der ergänzten Felder."""
    ergaenzt = []
    for feld, _ in BUCH_FELDER:
        if feld != "genre" and daten[feld].strip() in LEERWERTE and treffer[feld].strip() not in LEERWERTE:
            daten[feld] = treffer[feld]
            ergaenzt.append(feld)
    if not daten["serie"] and treffer["serie"]:
        daten["serie"], daten["band"] = treffer["serie"], treffer["band"]
        ergaenzt.append("serie")
    elif not daten["band"] and treffer["band"] and daten["serie"].lower() == treffer["serie"].lower():
        daten["band"] = treffer["band"]
        ergaenzt.append("band")
    for feld in ZUSATZ_FELDER:
        if not daten[feld] and treffer[feld]:
            daten[feld] = treffer[feld]
            ergaenzt.append(feld)
    return ergaenzt


def fetch_book_data(isbn):
    abfrage.fehler = []
    daten, sichere, unsichere = None, [], []
    for name, quelle, liefert in QUELLEN:
        if not fehlende_angaben(daten, bool(sichere)) & liefert:
            continue
        treffer = quelle(isbn)
        if not treffer:
            continue
        if not daten:
            daten = treffer
            print(f"📡 Gefunden bei {name}.")
        else:
            ergaenzt = buchdaten_ergaenzen(daten, treffer)
            if treffer['_genre_sicher'] and not sichere:
                ergaenzt.append("genre")
            if ergaenzt:
                namen = dict(BUCH_FELDER + SERIEN_FELDER, beschreibung="Beschreibung", cover_url="Cover")
                print(f"📡 Ergänzt aus {name}: {', '.join(namen[f] for f in ergaenzt)}.")
        (sichere if treffer['_genre_sicher'] else unsichere).append(treffer['_genres'])

    if not daten:
        return None
    genres = genres_zusammenfuehren(*(sichere or [g for g in unsichere if g][:1]))
    daten['genre'] = ', '.join(genres) or 'Keine Angabe'
    daten.pop('_genres', None)
    daten.pop('_genre_sicher', None)
    return daten


# --- BUCHDATEN ANZEIGEN, PRÜFEN & BEARBEITEN ---
def serie_text(serie, band):
    if not serie:
        return ""
    return f"{serie}, Band {band}" if band else serie


def zeige_buchdaten(daten):
    for feld, label in BUCH_FELDER:
        print(f"   {label + ':':<18} {daten[feld]}")
    if daten.get('serie'):
        print(f"   {'Serie:':<18} {serie_text(daten['serie'], daten['band'])}")
    if daten.get('beschreibung'):
        kurz = " ".join(daten['beschreibung'].split())
        print(f"   {'Beschreibung:':<18} {kurz[:150] + ' …' if len(kurz) > 150 else kurz}")


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
    print("✏️ Neue Werte eingeben ([ENTER] = beibehalten, '-' = Serie/Band leeren):")
    for feld, label in BUCH_FELDER + SERIEN_FELDER:
        if feld == "band" and not daten['serie']:
            daten['band'] = ""
            continue
        neu = input(f"   {label} [{daten[feld]}]: ").strip()
        if neu == "-" and feld in ("serie", "band"):
            daten[feld] = ""
        elif neu:
            daten[feld] = band_normalisieren(neu) if feld == "band" else neu
    if not daten['serie']:
        daten['band'] = ""


def buch_speichern(isbn, daten, anzahl=1, nachgeladen=False):
    """nachgeladen = Daten kommen aus der Online-Suche (Beschreibung, Cover, Serie wurden schon gesucht)."""
    sort_key, formatiert = get_sort_name(daten['autoren'])
    with db_connection() as conn:
        conn.execute('''
            INSERT INTO buecher
            (isbn, titel, autoren, autor_nachname, autor_formatiert, verlag, erscheinungsjahr, genre,
             anzahl_gesamt, anzahl_verfuegbar, serie, band, beschreibung, cover_url, nachgeladen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            isbn, daten['titel'], daten['autoren'], sort_key, formatiert,
            daten['verlag'], daten['erscheinungsjahr'], daten['genre'], anzahl, anzahl,
            daten.get('serie', ''), daten.get('band', ''), daten.get('beschreibung', ''),
            daten.get('cover_url', ''), int(nachgeladen),
        ))
    print(f"✨ NEU HINZUGEFÜGT: {daten['titel']} ({formatiert}) – {anzahl}x")


def buch_aktualisieren(isbn, daten):
    """Speichert die bearbeitbaren Felder, die in 'daten' vorkommen."""
    felder = [f for f, _ in BUCH_FELDER + SERIEN_FELDER if f in daten] + [f for f in ZUSATZ_FELDER if f in daten]
    werte = [daten[f] for f in felder]
    if "autoren" in daten:
        sort_key, formatiert = get_sort_name(daten['autoren'])
        felder += ["autor_nachname", "autor_formatiert"]
        werte += [sort_key, formatiert]
    with db_connection() as conn:
        conn.execute(f"UPDATE buecher SET {', '.join(f + ' = ?' for f in felder)} WHERE isbn = ?", (*werte, isbn))


def lade_buch(isbn):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT titel, autoren, verlag, erscheinungsjahr, genre, anzahl_gesamt, anzahl_verfuegbar,
                   serie, band, beschreibung, cover_url, nachgeladen
            FROM buecher WHERE isbn = ?
        ''', (isbn,))
        row = cursor.fetchone()
    if not row:
        return None
    daten = dict(zip([f for f, _ in BUCH_FELDER], (w or "" for w in row[:5])))
    daten['anzahl_gesamt'], daten['anzahl_verfuegbar'] = row[5], row[6]
    daten['serie'], daten['band'], daten['beschreibung'], daten['cover_url'] = (w or "" for w in row[7:11])
    daten['nachgeladen'] = bool(row[11])
    return daten


def daten_nachladen(isbn):
    """Sucht online nach fehlenden Angaben (Beschreibung, Cover, Serie, leere Felder) und ergänzt nur,
    was noch leer ist – von Hand eingetragene Angaben bleiben unverändert.
    Liefert die Liste der ergänzten Felder oder None, wenn das Buch online nicht gefunden wurde.
    War eine Quelle nicht erreichbar, wird das Buch beim nächsten "Alle nachladen" noch einmal versucht."""
    buch = lade_buch(isbn)
    if not buch:
        return None
    gefunden = fetch_book_data(isbn)
    fehler = letzte_fehler()
    ergaenzt = []
    if gefunden:
        ergaenzt = buchdaten_ergaenzen(buch, gefunden)
        if buch['genre'].strip() in LEERWERTE and gefunden['genre'].strip() not in LEERWERTE:
            buch['genre'] = gefunden['genre']
            ergaenzt.append("genre")
        if ergaenzt:
            buch_aktualisieren(isbn, {f: buch[f] for f in [f for f, _ in BUCH_FELDER + SERIEN_FELDER] + ZUSATZ_FELDER})
    if not fehler:
        with db_connection() as conn:
            conn.execute("UPDATE buecher SET nachgeladen = 1 WHERE isbn = ?", (isbn,))
    return ergaenzt if gefunden else None


def noch_nicht_nachgeladen():
    with db_connection() as conn:
        return [r[0] for r in conn.execute(
            "SELECT isbn FROM buecher WHERE nachgeladen = 0 OR nachgeladen IS NULL ORDER BY autor_nachname, titel")]


def alle_nachladen(fortschritt=None, pause=0.5):
    """Lädt für alle Bücher, die noch nicht online ergänzt wurden, die fehlenden Angaben nach.
    fortschritt(erledigt, gesamt, titel) wird nach jedem Buch aufgerufen.
    Bricht ab, wenn bei 3 Büchern nacheinander keine Quelle erreichbar war (z. B. kein Internet)."""
    isbns = noch_nicht_nachgeladen()
    ergaenzt = nicht_erreichbar = 0
    for nr, isbn in enumerate(isbns, 1):
        felder = daten_nachladen(isbn)
        if felder:
            ergaenzt += 1
        if len(set(letzte_fehler())) == len(QUELLEN):
            nicht_erreichbar += 1
            if nicht_erreichbar >= 3:
                raise ConnectionError("Die Online-Quellen sind gerade nicht erreichbar. Bitte später noch einmal versuchen.")
        else:
            nicht_erreichbar = 0
        if fortschritt:
            fortschritt(nr, len(isbns), (lade_buch(isbn) or {}).get("titel", isbn))
        if nr < len(isbns):
            time.sleep(pause)  # Online-Quellen nicht mit Anfragen überhäufen
    return len(isbns), ergaenzt


# --- REGALE & STANDORTE ---
# Jedes Regal hat Fächer mit gleich viel Platz und eine Liste von Genres (leer = alle übrigen Bücher).
# Ein Buch kommt in das erste Regal, das eines seiner Genres hat. Innerhalb des Regals wird sortiert:
# nach Genre (Reihenfolge wie beim Regal eingetragen), dann nach Autor; Bände einer Serie stehen
# zusammen in Band-Reihenfolge. Ist ein Regal voll, rutschen die letzten Bücher ins nächste Regal,
# das ihr Genre (oder alle übrigen Bücher) aufnimmt.
# Gespeichert wird, wo ein Buch tatsächlich steht (regal_id, fach). Der Plan lässt diese Bücher
# möglichst stehen und schlägt nur dann Umstellungen vor, wenn ein Fach zu voll wird.

def genre_liste(text):
    return [g.strip() for g in (text or "").split(",") if g.strip() and g.strip() not in LEERWERTE]


def lade_regale():
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT id, name, faecher, kapazitaet, genres FROM regale ORDER BY reihenfolge, id").fetchall()
    return [{"id": r[0], "name": r[1], "faecher": max(1, r[2] or 1), "kapazitaet": max(1, r[3] or 1),
             "genres": genre_liste(r[4])} for r in rows]


def regal_speichern(regal_id, name, faecher, kapazitaet, genres):
    """Legt ein Regal an (regal_id None) oder ändert es. genres = Liste. Liefert die ID."""
    werte = (name.strip(), int(faecher), int(kapazitaet), ", ".join(genres))
    with db_connection() as conn:
        if regal_id:
            conn.execute("UPDATE regale SET name = ?, faecher = ?, kapazitaet = ?, genres = ? WHERE id = ?",
                         (*werte, regal_id))
            return regal_id
        naechste = conn.execute("SELECT COALESCE(MAX(reihenfolge), 0) + 1 FROM regale").fetchone()[0]
        return conn.execute("INSERT INTO regale (name, faecher, kapazitaet, genres, reihenfolge) VALUES (?, ?, ?, ?, ?)",
                            (*werte, naechste)).lastrowid


def regal_loeschen(regal_id):
    with db_connection() as conn:
        conn.execute("UPDATE buecher SET regal_id = NULL, fach = NULL WHERE regal_id = ?", (regal_id,))
        conn.execute("DELETE FROM regale WHERE id = ?", (regal_id,))


def regal_verschieben(regal_id, richtung):
    """richtung -1 = nach vorne (wird bei gleichen Genres zuerst gefüllt), +1 = nach hinten."""
    ids = [r["id"] for r in lade_regale()]
    if regal_id not in ids:
        return
    i = ids.index(regal_id)
    j = i + richtung
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    with db_connection() as conn:
        for nr, rid in enumerate(ids, 1):
            conn.execute("UPDATE regale SET reihenfolge = ? WHERE id = ?", (nr, rid))


def alle_genres():
    with db_connection() as conn:
        genres = {g for (text,) in conn.execute("SELECT genre FROM buecher") for g in genre_liste(text)}
    return sorted(genres, key=str.lower)


def natuerlich(text):
    """Sortierschlüssel, bei dem "Teil 2" vor "Teil 10" kommt."""
    return re.sub(r"\d+", lambda m: m.group(0).zfill(8), (text or "").lower())


def laengste_folge(werte):
    """Positionen der längsten nicht fallenden Teilfolge – diese Bücher können stehen bleiben."""
    enden, enden_pos, vorgaenger = [], [], [-1] * len(werte)
    for i, wert in enumerate(werte):
        k = bisect.bisect_right(enden, wert)
        if k == len(enden):
            enden.append(wert)
            enden_pos.append(i)
        else:
            enden[k], enden_pos[k] = wert, i
        vorgaenger[i] = enden_pos[k - 1] if k else -1
    folge, i = [], enden_pos[-1] if enden_pos else -1
    while i != -1:
        folge.append(i)
        i = vorgaenger[i]
    return folge[::-1]


def faecher_belegen(buecher, regal):
    """Verteilt die sortierten Bücher auf die Fächer. Liefert (Liste je Fach, Bücher ohne Platz)."""
    anzahl_faecher, kap = regal["faecher"], regal["kapazitaet"]
    platz = [max(1, b["anzahl"]) for b in buecher]
    gespeichert = [b["fach"] if b["regal_id"] == regal["id"] and b["fach"] and 1 <= b["fach"] <= anzahl_faecher
                   else None for b in buecher]
    mit_fach = [i for i, f in enumerate(gespeichert) if f]
    bleiben = {mit_fach[k] for k in laengste_folge([gespeichert[i] for i in mit_fach])}

    fach = [None] * len(buecher)
    if not bleiben:
        # Regal wird neu eingeräumt: gleichmäßig verteilen, damit überall Platz für Neues bleibt
        ziel = min(kap, -(-sum(platz) // anzahl_faecher))
        aktuell, belegt = 1, 0
        for i in range(len(buecher)):
            if belegt and belegt + platz[i] > ziel and aktuell < anzahl_faecher:
                aktuell, belegt = aktuell + 1, 0
            fach[i] = aktuell
            belegt += platz[i]
    else:
        # Neue Bücher kommen zu ihrem Vorgänger in der Sortierung (am Anfang: zum Nachfolger)
        for i in bleiben:
            fach[i] = gespeichert[i]
        erstes = fach[min(bleiben)]
        vorher = None
        for i in range(len(buecher)):
            if fach[i] is None:
                fach[i] = vorher or erstes
            vorher = fach[i]

    inhalt = [[] for _ in range(anzahl_faecher + 2)]  # Index 1..n = Fächer, n+1 = passt nicht mehr
    for i, f in enumerate(fach):
        inhalt[f].append(i)

    def belegt(f):
        return sum(platz[i] for i in inhalt[f])

    # Zu volle Fächer: erstes Buch ins vorige Fach (wenn dort Platz ist), sonst letztes ins nächste
    for f in range(1, anzahl_faecher + 1):
        while belegt(f) > kap and len(inhalt[f]) > 1:
            if f > 1 and belegt(f - 1) + platz[inhalt[f][0]] <= kap:
                inhalt[f - 1].append(inhalt[f].pop(0))
            else:
                inhalt[f + 1].insert(0, inhalt[f].pop())
    return ([[buecher[i] for i in inhalt[f]] for f in range(1, anzahl_faecher + 1)],
            [buecher[i] for i in inhalt[anzahl_faecher + 1]])


def regal_plan():
    """Berechnet, wo jedes Buch stehen sollte.
    Liefert {"regale": [... mit "inhalt" je Fach], "buecher": {isbn: buch}, "ohne_platz": [(buch, grund)]}.
    Jedes Buch hat "soll" = (regal_id, fach) oder None und "ist" = gespeicherter Standort oder None."""
    regale = lade_regale()
    with db_connection() as conn:
        rows = conn.execute('''
            SELECT isbn, titel, autor_formatiert, autor_nachname, genre, serie, band, anzahl_gesamt, regal_id, fach
            FROM buecher
        ''').fetchall()
    regal_ids = {r["id"] for r in regale}
    buecher = []
    for isbn, titel, autor, nachname, genre, serie, band, anzahl, regal_id, fach in rows:
        buecher.append({
            "isbn": isbn, "titel": titel or "", "autor": autor or "", "nachname": (nachname or "").lower(),
            "genres": genre_liste(genre), "serie": (serie or "").strip(), "band": band or "", "anzahl": anzahl or 1,
            "regal_id": regal_id if regal_id in regal_ids else None, "fach": fach, "soll": None,
        })
        b = buecher[-1]
        b["ist"] = (b["regal_id"], fach) if b["regal_id"] and fach else None

    # Eine Serie wird unter dem Autor ihres ersten Bandes einsortiert
    serien_autor = {}
    for b in sorted((b for b in buecher if b["serie"]), key=lambda b: band_sortierwert(b["band"])):
        serien_autor.setdefault(b["serie"].lower(), b["nachname"])

    def sortierung(regal, b):
        kleine = [g.lower() for g in b["genres"]]
        if regal["genres"]:
            eigene = [g.lower() for g in regal["genres"]]
            genre = (min((eigene.index(g) for g in kleine if g in eigene), default=len(eigene)), "")
        else:
            genre = (0, kleine[0] if kleine else "￿")  # Bücher ohne Genre ans Ende
        if b["serie"]:
            return genre, serien_autor[b["serie"].lower()], natuerlich(b["serie"]), band_sortierwert(b["band"]), natuerlich(b["titel"])
        return genre, b["nachname"], natuerlich(b["titel"]), (0, 0, ""), ""

    def nimmt_auf(regal, b):
        eigene = {g.lower() for g in regal["genres"]}
        return not eigene or any(g.lower() in eigene for g in b["genres"])

    def erstes_regal(b):
        for nr, regal in enumerate(regale):
            if regal["genres"] and nimmt_auf(regal, b):
                return nr
        return next((nr for nr, regal in enumerate(regale) if not regal["genres"]), None)

    zuordnung = {b["isbn"]: erstes_regal(b) for b in buecher}
    uebrig = []
    for nr, regal in enumerate(regale):
        hierher = [b for b in buecher if zuordnung[b["isbn"]] == nr] + [b for b in uebrig if nimmt_auf(regal, b)]
        uebrig = [b for b in uebrig if not nimmt_auf(regal, b)]
        hierher.sort(key=lambda b: sortierung(regal, b))
        regal["inhalt"], zu_viel = faecher_belegen(hierher, regal)
        for fach_nr, inhalt in enumerate(regal["inhalt"], 1):
            for b in inhalt:
                b["soll"] = (regal["id"], fach_nr)
        uebrig += zu_viel

    ohne_platz = [(b, "voll") for b in uebrig]
    ohne_platz += [(b, "kein_regal") for b in buecher if zuordnung[b["isbn"]] is None]
    return {"regale": regale, "buecher": {b["isbn"]: b for b in buecher}, "ohne_platz": ohne_platz}


def standort_text(regale, standort):
    if not standort:
        return ""
    regal = next((r for r in regale if r["id"] == standort[0]), None)
    return f"{regal['name']}, Fach {standort[1]}" if regal else ""


def umstellungen(plan):
    """Bücher, die schon eingeräumt sind, aber woanders hin sollen."""
    return [b for b in plan["buecher"].values() if b["ist"] and b["soll"] and b["ist"] != b["soll"]]


def noch_einzuraeumen(plan):
    return [b for b in plan["buecher"].values() if not b["ist"] and b["soll"]]


def plan_uebernehmen():
    """Merkt sich für alle Bücher den geplanten Standort (= alles wurde so eingeräumt)."""
    plan = regal_plan()
    with db_connection() as conn:
        for isbn, b in plan["buecher"].items():
            regal_id, fach = b["soll"] or (None, None)
            conn.execute("UPDATE buecher SET regal_id = ?, fach = ? WHERE isbn = ?", (regal_id, fach, isbn))


def buch_einraeumen(isbn):
    """Für ein neues Buch: Liefert (Standort-Text, Anzahl nötiger Umstellungen, Grund falls kein Platz).
    Der Platz wird gespeichert, sobald im Regal schon andere Bücher fest eingeräumt sind –
    beim ersten Einräumen wird erst gespeichert, wenn der ganze Plan übernommen wird."""
    plan = regal_plan()
    if not plan["regale"]:
        return "", 0, None
    b = plan["buecher"].get(isbn)
    if not b:
        return "", 0, None
    if not b["soll"]:
        grund = next((g for x, g in plan["ohne_platz"] if x["isbn"] == isbn), "voll")
        return "", 0, grund
    regal_id = b["soll"][0]
    if not b["ist"] and any(x["ist"] and x["ist"][0] == regal_id for x in plan["buecher"].values()):
        with db_connection() as conn:
            conn.execute("UPDATE buecher SET regal_id = ?, fach = ? WHERE isbn = ?", (*b["soll"], isbn))
    andere = [x for x in umstellungen(plan) if x["isbn"] != isbn]
    return standort_text(plan["regale"], b["soll"]), len(andere), None


def standort_von(isbn, plan=None):
    """(gespeicherter Standort, geplanter Standort falls anders) als Text."""
    plan = plan or regal_plan()
    b = plan["buecher"].get(isbn)
    if not b or not plan["regale"]:
        return "", ""
    ist, soll = standort_text(plan["regale"], b["ist"]), standort_text(plan["regale"], b["soll"])
    return ist, (soll if soll != ist else "")


def standorte(plan=None):
    """Für den Katalog: {isbn: (Standort-Text, Regal-ID, geplant)}.
    Steht ein Buch schon fest im Regal, zählt dieser Platz, sonst der geplante. Ohne Regale: leer."""
    plan = plan or regal_plan()
    if not plan["regale"]:
        return {}
    ergebnis = {}
    for isbn, b in plan["buecher"].items():
        platz = b["ist"] or b["soll"]
        ergebnis[isbn] = (standort_text(plan["regale"], platz), platz[0], not b["ist"]) if platz else ("", None, False)
    return ergebnis


def zahl_eingabe(prompt, standard):
    while True:
        eingabe = input(f"{prompt} [{standard}]: ").strip()
        if not eingabe:
            return standard
        if eingabe.isdigit() and int(eingabe) > 0:
            return int(eingabe)
        print("Bitte eine ganze Zahl größer 0 eingeben.")


def regal_formular(regal=None):
    regal = regal or {"id": None, "name": "", "faecher": 5, "kapazitaet": 30, "genres": []}
    name = input(f"   Name{f' [{regal['name']}]' if regal['name'] else ' (z. B. Regal 1)'}: ").strip() or regal["name"]
    if not name:
        print("❌ Abgebrochen. Ein Name ist erforderlich.")
        return
    faecher = zahl_eingabe("   Anzahl Fächer", regal["faecher"])
    kapazitaet = zahl_eingabe("   Bücher pro Fach (ungefähr)", regal["kapazitaet"])
    vorhanden = alle_genres()
    if vorhanden:
        print(f"   Genres im Katalog: {', '.join(vorhanden)}")
    aktuell = ", ".join(regal["genres"])
    eingabe = input(f"   Genres, mit Komma getrennt (leer = alle übrigen Bücher){f' [{aktuell}]' if aktuell else ''}"
                    f"{', - = leeren' if aktuell else ''}: ").strip()
    genres = [] if eingabe == "-" else genre_liste(eingabe) if eingabe else regal["genres"]
    regal_speichern(regal["id"], name, faecher, kapazitaet, genres)
    print("✅ Regal gespeichert.")


def zeige_regale(plan):
    for nr, r in enumerate(plan["regale"], 1):
        belegt = sum(b["anzahl"] for inhalt in r["inhalt"] for b in inhalt)
        print(f"{nr}. {r['name']} – {r['faecher']} Fächer × {r['kapazitaet']} Bücher, belegt: {belegt}/{r['faecher'] * r['kapazitaet']}")
        print(f"   Genres: {', '.join(r['genres']) or 'alle übrigen Bücher'}")


def waehle_regal(plan):
    if not plan["regale"]:
        print("Noch keine Regale angelegt.")
        return None
    zeige_regale(plan)
    wahl = input("\nNummer des Regals (oder [ENTER] zum Abbrechen): ").strip()
    if wahl.isdigit() and 1 <= int(wahl) <= len(plan["regale"]):
        return plan["regale"][int(wahl) - 1]
    return None


def zeige_einraeumplan(plan):
    regale = plan["regale"]
    for r in regale:
        ueberschrift(f"🗄️ {r['name']}", 75)
        for fach_nr, inhalt in enumerate(r["inhalt"], 1):
            belegt = sum(b["anzahl"] for b in inhalt)
            print(f"\nFach {fach_nr} ({belegt}/{r['kapazitaet']}):")
            for b in inhalt:
                hinweis = ""
                if not b["ist"]:
                    hinweis = "  ➕ neu einräumen"
                elif b["ist"] != b["soll"]:
                    hinweis = f"  ↪️ umstellen von {standort_text(regale, b['ist'])}"
                menge = f" ({b['anzahl']}x)" if b["anzahl"] > 1 else ""
                print(f"   • {b['autor'][:22]:<22} | {b['titel'][:40]}{menge}{hinweis}")
            if not inhalt:
                print("   (leer)")
    if plan["ohne_platz"]:
        ueberschrift("⚠️ OHNE PLATZ", 75)
        for b, grund in plan["ohne_platz"]:
            text = "Regale voll" if grund == "voll" else f"kein Regal für: {', '.join(b['genres']) or 'ohne Genre'}"
            print(f"   • {b['titel'][:45]} – {text}")


def regale_menue():
    while True:
        plan = regal_plan()
        ueberschrift("🗄️ REGALE & STANDORTE")
        if plan["regale"]:
            zeige_regale(plan)
        else:
            print("Lege deine Regale an. ScanBook sagt dir dann für jedes Buch, wo es hingehört.")
        umstellen, neu = umstellungen(plan), noch_einzuraeumen(plan)
        zu_tun = [text for anzahl, text in ((len(neu), "einräumen"), (len(umstellen), "umstellen")) if anzahl
                  for text in [f"{anzahl} {'Buch' if anzahl == 1 else 'Bücher'} {text}"]]
        if zu_tun:
            print(f"\n📦 Zu tun: {', '.join(zu_tun)} (siehe Einräumplan, danach Punkt 6).")
        if plan["ohne_platz"]:
            print(f"⚠️ {len(plan['ohne_platz'])} Buch/Bücher haben keinen Platz – siehe Einräumplan.")
        print("\n1. Regal anlegen")
        print("2. Regal bearbeiten")
        print("3. Regal löschen")
        print("4. Regal nach vorne schieben (wird bei gleichen Genres zuerst gefüllt)")
        print("5. Einräumplan anzeigen (was steht wo, was muss umgestellt werden)")
        print("6. Alles eingeräumt – Plan übernehmen")
        print("7. Zurück zum Hauptmenü")
        wahl = input("\nWähle eine Option (1-7): ").strip()

        if wahl == "1":
            regal_formular()
        elif wahl == "2":
            regal = waehle_regal(plan)
            if regal:
                regal_formular(regal)
        elif wahl == "3":
            regal = waehle_regal(plan)
            if regal and ja_nein(f"⚠️ '{regal['name']}' wirklich löschen?"):
                regal_loeschen(regal["id"])
                print("🗑️ Regal gelöscht.")
        elif wahl == "4":
            regal = waehle_regal(plan)
            if regal:
                regal_verschieben(regal["id"], -1)
        elif wahl == "5":
            zeige_einraeumplan(plan)
            warte()
        elif wahl == "6":
            plan_uebernehmen()
            print("✅ Gespeichert. Neue Bücher werden ab jetzt passend dazu einsortiert.")
        elif wahl == "7":
            return
        else:
            print("Ungültige Eingabe.")


def standort_ausgeben(isbn, neu=False):
    if neu:
        text, andere, grund = buch_einraeumen(isbn)
    else:
        text, soll = standort_von(isbn)
        andere, grund = 0, None
        if soll:
            text = f"{text or 'noch nicht eingeräumt'} → soll nach {soll}"
    if text:
        print(f"📍 Standort: {text}")
    elif grund == "voll":
        print("⚠️ Die passenden Regale sind voll – lege ein Fach oder Regal dazu.")
    elif grund == "kein_regal":
        print("⚠️ Für dieses Genre gibt es noch kein Regal.")
    if andere:
        print(f"↪️ Dafür sollten {andere} Buch/Bücher umgestellt werden (Menü 'Regale & Standorte').")


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
                standort_ausgeben(isbn)
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
            buch_speichern(isbn, daten, nachgeladen=True)
            standort_ausgeben(isbn, neu=True)

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
        input("   Serie (leer = keine): ").strip(),
    )
    if daten['serie']:
        daten['band'] = band_normalisieren(input("   Band-Nummer: ").strip())
    anzahl_input = input("   Anzahl Exemplare [1]: ").strip()
    anzahl = int(anzahl_input) if anzahl_input.isdigit() and int(anzahl_input) > 0 else 1

    buch_speichern(isbn, daten, anzahl)
    standort_ausgeben(isbn, neu=True)


# --- KATALOG ANZEIGEN & SUCHEN ---
def katalog_sortierung(buch):
    """Nach Autor, Bände einer Serie direkt nacheinander in der richtigen Reihenfolge."""
    autor, titel, serie, band = buch[0], buch[1], buch[7] or "", buch[8] or ""
    return (autor or "").lower(), serie.lower(), band_sortierwert(band) if serie else (0, 0, ""), titel.lower()


def zeige_buchliste(buecher):
    for autor_formatiert, titel, jahr, genre, gesamt, verfuegbar, isbn, serie, band in sorted(buecher, key=katalog_sortierung):
        print(f"• {autor_formatiert:<25} | {titel} ({jahr}) [Bestand: {verfuegbar}/{gesamt} verfügbar]")
        if serie:
            print(f"  📚 Serie: {serie_text(serie, band)}")
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
    suchbegriff = input("Titel, Autor, Serie, Genre oder ISBN: ").strip()
    if not suchbegriff:
        return

    isbn = normalize_isbn(suchbegriff)
    muster = f"%{isbn or suchbegriff}%"
    with db_connection() as conn:
        treffer = conn.execute(f'''
            SELECT {BUCH_SPALTEN} FROM buecher
            WHERE titel LIKE ? OR autoren LIKE ? OR genre LIKE ? OR isbn LIKE ? OR serie LIKE ?
            ORDER BY autor_nachname ASC, titel ASC
        ''', (muster,) * 5).fetchall()

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
        ist, soll = standort_von(isbn)
        if ist or soll:
            print(f"   {'Standort:':<18} {ist or 'noch nicht eingeräumt'}{f' → soll nach {soll}' if soll else ''}")
        print("\n1. Buchdaten bearbeiten")
        print("2. Ein Exemplar entfernen (z. B. verloren oder kaputt)")
        print("3. Buch komplett löschen")
        print("4. Fehlende Angaben online nachladen (Beschreibung, Cover, Serie)")
        print("5. Zurück zum Hauptmenü")
        wahl = input("\nWähle eine Option (1-5): ").strip()

        if wahl == "1":
            buchdaten_bearbeiten(daten)
            buch_aktualisieren(isbn, {f: daten[f] for f, _ in BUCH_FELDER + SERIEN_FELDER})
            print("✅ Buchdaten gespeichert!")

        elif wahl == "2":
            codes = exemplare_von(isbn)
            code = None
            if codes:
                print("Mediencodes: " + ", ".join(e["code"] + (f" (bei {e['ausgeliehen_an']})" if e["ausgeliehen_an"] else "")
                                                  for e in codes))
                code = input("Mediencode des Exemplars ([ENTER] = Exemplar ohne Code): ").strip() or None
            try:
                exemplar_entfernen(isbn, code)
                print("✅ Ein Exemplar entfernt.")
            except ValueError as e:
                print(f"❌ {e}")

        elif wahl == "3":
            if ja_nein(f"⚠️ '{daten['titel']}' samt Ausleih-Historie wirklich löschen?"):
                try:
                    buch_loeschen(isbn)
                except ValueError as e:
                    print(f"❌ {e}")
                    continue
                print("🗑️ Buch gelöscht.")
                return

        elif wahl == "4":
            print(f"🔍 Suche online nach ISBN {isbn}...")
            ergaenzt = daten_nachladen(isbn)
            if ergaenzt is None and letzte_fehler():
                print("❌ Die Online-Quellen waren nicht erreichbar. Bitte später noch einmal versuchen.")
            elif ergaenzt is None:
                print("❌ Online nicht gefunden.")
            elif ergaenzt:
                print("✅ Fehlende Angaben ergänzt.")
            else:
                print("ℹ️ Nichts Neues gefunden – alle verfügbaren Angaben sind schon da.")

        elif wahl == "5":
            return
        else:
            print("Ungültige Eingabe.")


def buchdaten_nachladen_menue():
    ueberschrift("🌐 BUCHDATEN ONLINE NACHLADEN")
    anzahl = len(noch_nicht_nachgeladen())
    if not anzahl:
        print("✅ Für alle Bücher wurde schon online gesucht.")
        print("   Einzelne Bücher lassen sich unter 'Buch bearbeiten' erneut nachladen.")
        warte()
        return
    print(f"Für {anzahl} Bücher werden Beschreibung, Cover und Serie online gesucht.")
    print("Von Hand eingetragene Angaben bleiben unverändert. Abbrechen mit Strg+C, später geht es dort weiter.")
    if not ja_nein("Jetzt starten?"):
        return

    def fortschritt(nr, gesamt, titel):
        print(f"   [{nr}/{gesamt}] {titel}")

    try:
        gesamt, ergaenzt = alle_nachladen(fortschritt)
    except ConnectionError as e:
        print(f"\n❌ {e}")
    else:
        print(f"\n✅ Fertig: {gesamt} Bücher geprüft, bei {ergaenzt} wurden Angaben ergänzt.")
        rest = len(noch_nicht_nachgeladen())
        if rest:
            print(f"ℹ️ Bei {rest} Büchern war eine Quelle nicht erreichbar – einfach später noch einmal starten.")
    warte()


# --- SCHÜLER & MEDIENCODES ---
AUSWEIS_STELLEN = 4
MEDIENCODE_STELLEN = 5


def code_gueltig(code):
    """Codes stehen als Barcode (Code 128) auf Etiketten und Ausweisen: nur einfache Zeichen, nicht zu lang."""
    return 0 < len(code) <= 30 and all(32 < ord(z) < 127 for z in code)


def code_vergeben(conn, code, ausser_schueler=None):
    return bool(
        conn.execute("SELECT 1 FROM exemplare WHERE code = ?", (code,)).fetchone()
        or conn.execute("SELECT 1 FROM schueler WHERE ausweis = ? AND id IS NOT ?", (code, ausser_schueler)).fetchone()
    )


def naechster_code(conn, einstellung, stellen):
    """Nächste freie Nummer mit Vorsilbe, z. B. M00013 – zählt ab der höchsten schon vergebenen weiter."""
    row = conn.execute("SELECT wert FROM einstellungen WHERE schluessel = ?", (einstellung,)).fetchone()
    praefix = (row[0] if row else DEFAULT_EINSTELLUNGEN[einstellung]).strip()
    hoechste = 0
    for (code,) in conn.execute("SELECT code FROM exemplare UNION ALL SELECT ausweis FROM schueler"):
        rest = (code or "")[len(praefix):]
        if code and code.upper().startswith(praefix.upper()) and rest.isdigit():
            hoechste = max(hoechste, int(rest))
    while True:
        hoechste += 1
        code = f"{praefix}{hoechste:0{stellen}d}"
        if not normalize_isbn(code) and not code_vergeben(conn, code):
            return code


def neuer_code_pruefen(conn, code, ausser_schueler=None):
    if not code_gueltig(code):
        raise ValueError("Ein Code darf höchstens 30 Zeichen lang sein und keine Leerzeichen oder Umlaute enthalten.")
    if normalize_isbn(code):
        raise ValueError(f"'{code}' ist eine ISBN und kann nicht als Code verwendet werden.")
    if code_vergeben(conn, code, ausser_schueler):
        raise ValueError(f"Der Code '{code}' ist schon vergeben.")


def schueler_name(vorname, nachname):
    return f"{vorname} {nachname}".strip()


def schueler_speichern(schueler_id, vorname, nachname, klasse, ausweis=""):
    """Legt einen Schüler an (schueler_id leer) oder ändert ihn. Leere Ausweisnummer = automatisch vergeben."""
    vorname, nachname, klasse, ausweis = (str(w or "").strip() for w in (vorname, nachname, klasse, ausweis))
    if not vorname or not nachname:
        raise ValueError("Bitte Vor- und Nachname eingeben.")
    with db_connection() as conn:
        if ausweis:
            neuer_code_pruefen(conn, ausweis, schueler_id)
        elif schueler_id:
            row = conn.execute("SELECT ausweis FROM schueler WHERE id = ?", (schueler_id,)).fetchone()
            ausweis = row[0] if row else ""
        if not ausweis:
            ausweis = naechster_code(conn, "ausweis_praefix", AUSWEIS_STELLEN)
        if schueler_id:
            if not conn.execute("UPDATE schueler SET vorname = ?, nachname = ?, klasse = ?, ausweis = ? WHERE id = ?",
                                (vorname, nachname, klasse, ausweis, schueler_id)).rowcount:
                raise ValueError("Diesen Schüler gibt es nicht (mehr).")
            # Name auch bei den Ausleihen anpassen, damit Listen und Mahnliste stimmen
            conn.execute("UPDATE ausleihen SET ausleiher = ? WHERE schueler_id = ?",
                         (schueler_name(vorname, nachname), schueler_id))
            return schueler_id
        return conn.execute("INSERT INTO schueler (ausweis, vorname, nachname, klasse) VALUES (?, ?, ?, ?)",
                            (ausweis, vorname, nachname, klasse)).lastrowid


def schueler_loeschen(schueler_id):
    with db_connection() as conn:
        offen = conn.execute("SELECT COUNT(*) FROM ausleihen WHERE schueler_id = ? AND status = 'aktiv'",
                             (schueler_id,)).fetchone()[0]
        if offen:
            raise ValueError(f"Der Schüler hat noch {offen} Buch/Bücher ausgeliehen.")
        # Frühere Ausleihen bleiben mit Namen erhalten (bis zur Löschfrist)
        conn.execute("UPDATE ausleihen SET schueler_id = NULL WHERE schueler_id = ?", (schueler_id,))
        conn.execute("DELETE FROM schueler WHERE id = ?", (schueler_id,))


def lade_schueler(schueler_id):
    with db_connection() as conn:
        row = conn.execute("SELECT id, ausweis, vorname, nachname, klasse FROM schueler WHERE id = ?",
                           (schueler_id,)).fetchone()
    if not row:
        return None
    return {"id": row[0], "ausweis": row[1], "vorname": row[2], "nachname": row[3], "klasse": row[4] or "",
            "name": schueler_name(row[2], row[3])}


def klassen_sortierwert(klasse):
    """'5a' vor '10b': erst nach Zahl, dann nach dem Rest."""
    zahl = re.match(r"\d+", klasse or "")
    return (int(zahl.group()) if zahl else 999, (klasse or "").lower())


SCHUELER_KOPF = {"vorname": "vorname", "nachname": "nachname", "name": "nachname", "familienname": "nachname",
                 "klasse": "klasse", "ausweis": "ausweis", "ausweisnummer": "ausweis", "nummer": "ausweis"}


def schueler_importieren(text):
    """Liest eine Klassenliste ein (aus Excel kopiert oder als CSV-Datei).
    Spalten: Nachname, Vorname, Klasse und optional Ausweisnummer – oder in der Reihenfolge der Kopfzeile."""
    zeilen = [z for z in (text or "").lstrip("﻿").splitlines() if z.strip()]
    if not zeilen:
        return {"neu": 0, "vorhanden": 0, "fehler": []}
    trenner = max("\t;,", key=zeilen[0].count)
    zeilen = list(csv.reader(zeilen, delimiter=trenner))
    reihenfolge = ["nachname", "vorname", "klasse", "ausweis"]
    kopf = [SCHUELER_KOPF.get(z.strip().lower().replace("-", "")) for z in zeilen[0]]
    if "vorname" in kopf and "nachname" in kopf:
        reihenfolge, zeilen = kopf, zeilen[1:]

    neu, vorhanden, fehler = 0, 0, []
    with db_connection() as conn:
        bekannt = {(v.lower(), n.lower(), (k or "").lower())
                   for v, n, k in conn.execute("SELECT vorname, nachname, klasse FROM schueler")}
    for nr, zeile in enumerate(zeilen, 1):
        werte = {feld: zeile[i].strip() for i, feld in enumerate(reihenfolge) if feld and i < len(zeile)}
        schluessel = tuple(werte.get(f, "").lower() for f in ("vorname", "nachname", "klasse"))
        if schluessel in bekannt:
            vorhanden += 1
            continue
        try:
            schueler_speichern(None, werte.get("vorname"), werte.get("nachname"), werte.get("klasse"), werte.get("ausweis"))
            bekannt.add(schluessel)
            neu += 1
        except ValueError as e:
            fehler.append(f"Zeile {nr} ({' '.join(zeile).strip()}): {e}")
    return {"neu": neu, "vorhanden": vorhanden, "fehler": fehler}


def exemplare_von(isbn):
    """Mediencodes eines Buchs, mit der Person, die das Exemplar gerade hat."""
    with db_connection() as conn:
        rows = conn.execute('''
            SELECT e.code, e.eigener, e.gedruckt,
                   (SELECT a.ausleiher FROM ausleihen a WHERE a.exemplar = e.code AND a.status = 'aktiv')
            FROM exemplare e WHERE e.isbn = ? ORDER BY e.code
        ''', (isbn,)).fetchall()
    return [{"code": c, "eigener": bool(e), "gedruckt": bool(g), "ausgeliehen_an": a or ""} for c, e, g, a in rows]


def exemplar_anlegen(isbn, code=""):
    """Gibt einem Exemplar einen Mediencode: einen eigenen (z. B. ein schon aufgeklebtes Etikett) oder,
    wenn code leer ist, die nächste freie Nummer. Haben schon alle Exemplare einen Code, ist es ein
    weiteres Exemplar und der Bestand steigt um eins. Liefert (code, aufgestockt)."""
    code = str(code or "").strip()
    eigener = bool(code)
    with db_connection() as conn:
        row = conn.execute("SELECT anzahl_gesamt FROM buecher WHERE isbn = ?", (isbn,)).fetchone()
        if not row:
            raise ValueError("Dieses Buch ist nicht im Katalog.")
        if eigener:
            neuer_code_pruefen(conn, code)
        else:
            code = naechster_code(conn, "mediencode_praefix", MEDIENCODE_STELLEN)
        mit_code = conn.execute("SELECT COUNT(*) FROM exemplare WHERE isbn = ?", (isbn,)).fetchone()[0]
        aufgestockt = mit_code >= row[0]
        if aufgestockt:
            conn.execute("UPDATE buecher SET anzahl_gesamt = anzahl_gesamt + 1, anzahl_verfuegbar = anzahl_verfuegbar + 1 "
                         "WHERE isbn = ?", (isbn,))
        conn.execute("INSERT INTO exemplare (code, isbn, eigener) VALUES (?, ?, ?)", (code, isbn, int(eigener)))
    return code, aufgestockt


def fehlende_codes_erzeugen(isbn=None):
    """Erzeugt Mediencodes für alle Exemplare ohne Code – für ein Buch oder den ganzen Katalog."""
    with db_connection() as conn:
        sql = "SELECT isbn, anzahl_gesamt - (SELECT COUNT(*) FROM exemplare e WHERE e.isbn = b.isbn) FROM buecher b"
        offen = conn.execute(sql + (" WHERE isbn = ?" if isbn else "") + " ORDER BY autor_nachname, titel",
                             (isbn,) if isbn else ()).fetchall()
        neu = []
        for buch_isbn, anzahl in offen:
            for _ in range(max(0, anzahl)):
                code = naechster_code(conn, "mediencode_praefix", MEDIENCODE_STELLEN)
                conn.execute("INSERT INTO exemplare (code, isbn) VALUES (?, ?)", (code, buch_isbn))
                neu.append(code)
    return neu


def exemplare_ohne_code():
    with db_connection() as conn:
        return conn.execute('''
            SELECT COALESCE(SUM(MAX(0, anzahl_gesamt - (SELECT COUNT(*) FROM exemplare e WHERE e.isbn = b.isbn))), 0)
            FROM buecher b
        ''').fetchone()[0]


def exemplar_loeschen(code):
    """Entfernt nur den Mediencode – das Exemplar selbst bleibt im Bestand."""
    with db_connection() as conn:
        if conn.execute("SELECT 1 FROM ausleihen WHERE exemplar = ? AND status = 'aktiv'", (code,)).fetchone():
            raise ValueError("Dieses Exemplar ist gerade ausgeliehen.")
        if not conn.execute("DELETE FROM exemplare WHERE code = ?", (code,)).rowcount:
            raise ValueError("Diesen Mediencode gibt es nicht (mehr).")


def exemplar_entfernen(isbn, code=None):
    """Nimmt ein Exemplar aus dem Bestand (verloren, kaputt). Mit Mediencode genau dieses, sonst eins ohne Code."""
    with db_connection() as conn:
        row = conn.execute("SELECT anzahl_gesamt, anzahl_verfuegbar FROM buecher WHERE isbn = ?", (isbn,)).fetchone()
        if not row:
            raise ValueError("Dieses Buch ist nicht im Katalog.")
        gesamt, verfuegbar = row
        if gesamt <= 1:
            raise ValueError("Das ist das letzte Exemplar – bitte stattdessen das Buch löschen.")
        if code:
            if conn.execute("SELECT 1 FROM ausleihen WHERE exemplar = ? AND status = 'aktiv'", (code,)).fetchone():
                raise ValueError("Dieses Exemplar ist gerade ausgeliehen.")
            if not conn.execute("DELETE FROM exemplare WHERE code = ? AND isbn = ?", (code, isbn)).rowcount:
                raise ValueError("Dieser Mediencode gehört nicht zu diesem Buch.")
        elif conn.execute("SELECT COUNT(*) FROM exemplare WHERE isbn = ?", (isbn,)).fetchone()[0] >= gesamt:
            raise ValueError("Alle Exemplare haben einen Mediencode – bitte den Code des Exemplars angeben, das entfernt wird.")
        if verfuegbar <= 0:
            raise ValueError("Alle Exemplare sind ausgeliehen. Erst nach einer Rückgabe kann eins entfernt werden.")
        conn.execute("UPDATE buecher SET anzahl_gesamt = anzahl_gesamt - 1, anzahl_verfuegbar = anzahl_verfuegbar - 1 "
                     "WHERE isbn = ?", (isbn,))


def buch_loeschen(isbn):
    with db_connection() as conn:
        aktiv = conn.execute("SELECT COUNT(*) FROM ausleihen WHERE isbn = ? AND status = 'aktiv'", (isbn,)).fetchone()[0]
        if aktiv:
            raise ValueError(f"Das Buch ist noch {aktiv}x ausgeliehen und kann nicht gelöscht werden.")
        conn.execute("DELETE FROM ausleihen WHERE isbn = ?", (isbn,))
        conn.execute("DELETE FROM exemplare WHERE isbn = ?", (isbn,))
        conn.execute("DELETE FROM buecher WHERE isbn = ?", (isbn,))


def code_aufloesen(eingabe):
    """Was wurde gescannt? ("exemplar", isbn, code), ("schueler", id, ausweis), ("isbn", isbn, None) oder None."""
    eingabe = str(eingabe or "").strip()
    if not eingabe:
        return None
    with db_connection() as conn:
        row = conn.execute("SELECT isbn, code FROM exemplare WHERE code = ?", (eingabe,)).fetchone()
        if row:
            return ("exemplar", row[0], row[1])
        row = conn.execute("SELECT id, ausweis FROM schueler WHERE ausweis = ?", (eingabe,)).fetchone()
        if row:
            return ("schueler", row[0], row[1])
    isbn = normalize_isbn(eingabe)
    return ("isbn", isbn, None) if isbn else None


def person_aufloesen(eingabe):
    """Ausweis gescannt oder Name eingetippt? Liefert (Name, schueler_id oder None)."""
    eingabe = str(eingabe or "").strip()
    treffer = code_aufloesen(eingabe)
    if treffer and treffer[0] == "schueler":
        s = lade_schueler(treffer[1])
        return s["name"], s["id"]
    # "Max Muster (5a)" aus der Vorschlagsliste oder genau ein Schüler mit diesem Namen
    name = re.sub(r"\s*\([^()]*\)$", "", eingabe)
    klasse = eingabe[len(name):].strip(" ()")
    with db_connection() as conn:
        rows = conn.execute('''
            SELECT id FROM schueler
            WHERE LOWER(vorname || ' ' || nachname) = LOWER(?) AND (? = '' OR LOWER(klasse) = LOWER(?))
        ''', (name, klasse, klasse)).fetchall()
    if len(rows) == 1:
        s = lade_schueler(rows[0][0])
        return s["name"], s["id"]
    return name, None


def buch_ausleihen(isbn, ausleiher, tage, schueler_id=None, exemplar=None):
    """Trägt eine Ausleihe ein. Liefert (Name des Ausleihers, Rückgabedatum)."""
    ausgeliehen_am = heute()
    rueckgabe_bis = ausgeliehen_am + timedelta(days=tage)
    with db_connection() as conn:
        if exemplar:
            besitzer = conn.execute("SELECT ausleiher FROM ausleihen WHERE exemplar = ? AND status = 'aktiv'",
                                    (exemplar,)).fetchone()
            if besitzer:
                raise ValueError(f"Das Exemplar {exemplar} ist noch an {besitzer[0]} ausgeliehen. Bitte erst zurückgeben.")
        if not conn.execute(
            "UPDATE buecher SET anzahl_verfuegbar = anzahl_verfuegbar - 1 WHERE isbn = ? AND anzahl_verfuegbar > 0",
            (isbn,),
        ).rowcount:
            titel = conn.execute("SELECT titel FROM buecher WHERE isbn = ?", (isbn,)).fetchone()
            raise ValueError(f"'{titel[0] if titel else isbn}' ist aktuell vollständig ausgeliehen!")

        if not schueler_id:
            # Gleiche Schreibweise wie bei früheren Ausleihen verwenden ("max mustermann" → "Max Mustermann")
            bekannt = conn.execute(
                "SELECT ausleiher FROM ausleihen WHERE LOWER(ausleiher) = LOWER(?) LIMIT 1", (ausleiher,)
            ).fetchone()
            if bekannt:
                ausleiher = bekannt[0]

        conn.execute('''
            INSERT INTO ausleihen (isbn, ausleiher, ausgeliehen_am, rueckgabe_bis, status, schueler_id, exemplar)
            VALUES (?, ?, ?, ?, 'aktiv', ?, ?)
        ''', (isbn, ausleiher, ausgeliehen_am.isoformat(), rueckgabe_bis.isoformat(), schueler_id, exemplar))
    return ausleiher, rueckgabe_bis


def lies_buchcode(prompt):
    """Für Ausleihe und Rückgabe im Terminal: Mediencode oder ISBN. Liefert (isbn, mediencode)."""
    eingabe = input(prompt).strip()
    if not eingabe:
        return None, None
    treffer = code_aufloesen(eingabe)
    if treffer and treffer[0] == "exemplar":
        return treffer[1], treffer[2]
    if treffer and treffer[0] == "isbn":
        return treffer[1], None
    print("❌ Unbekannter Code bzw. ungültige ISBN – die Prüfziffer stimmt nicht. Bitte erneut scannen.")
    return None, None


# --- BUCH AUSLEIHEN ---
def book_checkout():
    ueberschrift("📤 BUCH AUSLEIHEN")

    isbn, exemplar = lies_buchcode("👉 Scanne das Buch (Mediencode oder ISBN): ")
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

    print(f"📖 Ausleihe für: '{titel}'{f' – Exemplar {exemplar}' if exemplar else ''} (Verfügbar: {verfuegbar} Exemplar(e))")
    ausleiher, schueler_id = person_aufloesen(input("👤 Schülerausweis scannen oder Name eingeben: "))
    if not ausleiher:
        print("❌ Ausleihe abgebrochen. Name erforderlich.")
        return
    if schueler_id:
        s = lade_schueler(schueler_id)
        print(f"🎓 Schüler: {s['name']}" + (f", Klasse {s['klasse']}" if s["klasse"] else ""))

    standard = get_einstellung_int("standard_leihdauer", 14)
    tage_input = input(f"📅 Ausleihdauer in Tagen [Standard: {standard}]: ").strip()
    tage = int(tage_input) if tage_input.isdigit() and int(tage_input) > 0 else standard

    try:
        ausleiher, rueckgabe_datum = buch_ausleihen(isbn, ausleiher, tage, schueler_id, exemplar)
    except ValueError as e:
        print(f"❌ {e}")
        return

    print("\n✅ AUSLEIHE ERFOLGREICH GESPEICHERT!")
    print(f"👤 Ausleiher:    {ausleiher}")
    print(f"📖 Buch:         {titel}")
    print(f"📅 Rückgabe bis: {rueckgabe_datum.strftime('%d.%m.%Y')}")


# --- AKTIVE AUSLEIHE AUSWÄHLEN (PER SCAN ODER LISTE) ---
def ausleihen_zum_code(eingabe):
    """IDs der aktiven Ausleihen zu einem gescannten Mediencode oder einer ISBN.
    Beim Mediencode genau dieses Exemplar; wurde es per ISBN ausgeliehen, die Ausleihen dieses Buchs ohne Code."""
    treffer = code_aufloesen(eingabe)
    if not treffer or treffer[0] == "schueler":
        raise ValueError("Unbekannter Code bzw. ungültige ISBN – die Prüfziffer stimmt nicht.")
    typ, isbn, code = treffer
    with db_connection() as conn:
        if typ == "exemplar":
            ids = conn.execute("SELECT id FROM ausleihen WHERE exemplar = ? AND status = 'aktiv'", (code,)).fetchall()
            if ids:
                return [i for (i,) in ids]
            sql = "SELECT id FROM ausleihen WHERE isbn = ? AND exemplar IS NULL AND status = 'aktiv' ORDER BY rueckgabe_bis"
        else:
            sql = "SELECT id FROM ausleihen WHERE isbn = ? AND status = 'aktiv' ORDER BY rueckgabe_bis"
        return [i for (i,) in conn.execute(sql, (isbn,))]


def finde_aktive_ausleihe():
    eingabe = input("👉 Scanne das Buch – Mediencode oder ISBN (oder [ENTER] für die Liste): ").strip()
    sql = '''
        SELECT a.id, a.isbn, b.titel, a.ausleiher, a.rueckgabe_bis
        FROM ausleihen a
        JOIN buecher b ON a.isbn = b.isbn
        WHERE a.status = 'aktiv'
    '''

    if eingabe:
        try:
            ids = ausleihen_zum_code(eingabe)
        except ValueError as e:
            print(f"❌ {e}")
            return None
        if not ids:
            print("❌ Für dieses Buch gibt es keine aktive Ausleihe.")
            return None
    with db_connection() as conn:
        if eingabe:
            ausleihen = conn.execute(sql + f" AND a.id IN ({','.join('?' * len(ids))}) ORDER BY a.rueckgabe_bis",
                                     ids).fetchall()
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
                SELECT isbn, titel, autoren, verlag, erscheinungsjahr, genre, serie, band, anzahl_gesamt, anzahl_verfuegbar
                FROM buecher ORDER BY autor_nachname, titel
            ''').fetchall()
            orte = standorte()

            def ort(isbn):
                text, _, geplant = orte.get(isbn, ("", None, False))
                return f"{text} (geplant)" if text and geplant else text
            zeilen = [(*z, ort(z[0])) for z in zeilen]
            return ["ISBN", "Titel", "Autor(en)", "Verlag", "Jahr", "Genre", "Serie", "Band", "Exemplare", "Verfügbar",
                    "Standort"], zeilen

        sql = '''
            SELECT a.ausleiher, COALESCE(s.klasse, ''), b.titel, a.isbn, COALESCE(a.exemplar, ''),
                   a.ausgeliehen_am, a.rueckgabe_bis
            FROM ausleihen a JOIN buecher b ON a.isbn = b.isbn LEFT JOIN schueler s ON s.id = a.schueler_id
            WHERE a.status = 'aktiv'
        '''
        params = ()
        if typ == "mahnliste":
            sql += " AND a.rueckgabe_bis < ?"
            params = (heute().isoformat(),)
        rohdaten = conn.execute(sql, params).fetchall()

    # Nach Klasse (5a vor 10b), dann Name – so lässt sich die Mahnliste pro Klasse an die Lehrkräfte geben
    rohdaten.sort(key=lambda r: (klassen_sortierwert(r[1]) if r[1] else (1000, ""), r[0].lower(), r[6]))
    zeilen = [
        (name, klasse, titel, isbn, code, de_datum(von), de_datum(bis), max(0, (heute() - date.fromisoformat(bis)).days))
        for name, klasse, titel, isbn, code, von, bis in rohdaten
    ]
    return ["Ausleiher", "Klasse", "Titel", "ISBN", "Mediencode", "Geliehen am", "Rückgabe bis", "Tage überfällig"], zeilen


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
# Sicherungen liegen im Ordner "backups" neben dem Programm. Ist ein zweiter Ordner eingestellt
# (USB-Stick, OneDrive, Netzlaufwerk), kommt jede Sicherung zusätzlich dorthin – falls der PC ausfällt.
backup_sperre = threading.Lock()
backup_status = {"kopie_fehler": ""}


def zweiter_backup_ordner():
    try:
        return get_einstellung(BACKUP_ORDNER_EINSTELLUNG).strip()
    except sqlite3.Error:  # ganz alte Datenbank ohne Einstellungen
        return ""


def backup_ordner_pruefen(ordner):
    """Liefert eine Fehlermeldung oder "" wenn der Ordner benutzbar ist."""
    if not os.path.isdir(ordner):
        return "Diesen Ordner gibt es nicht (ist der USB-Stick eingesteckt?)."
    test = os.path.join(ordner, ".scanbook_test")
    try:
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
    except OSError:
        return "In diesen Ordner kann nicht geschrieben werden."
    return ""


def erstelle_backup(praefix="backup"):
    with backup_sperre:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        pfad = os.path.join(BACKUP_DIR, f"{praefix}_{datetime.now():%Y%m%d_%H%M%S}.db")
        src = sqlite3.connect(DB_NAME)
        dst = sqlite3.connect(pfad)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        backup_kopieren(pfad, praefix)
        return pfad


def backup_kopieren(pfad, praefix):
    ordner = zweiter_backup_ordner()
    backup_status["kopie_fehler"] = ""
    if not ordner:
        return
    fehler = backup_ordner_pruefen(ordner)
    if not fehler:
        try:
            shutil.copy2(pfad, os.path.join(ordner, os.path.basename(pfad)))
            if praefix == "auto":
                alte_dateien_aufraeumen(ordner, "auto_", MAX_AUTO_BACKUPS)
        except OSError as e:
            fehler = f"Kopieren fehlgeschlagen: {e}"
    backup_status["kopie_fehler"] = fehler


def auto_backup():
    """Einmal pro Tag automatisch sichern – beim Start und, solange ScanBook läuft, auch an den Folgetagen."""
    if not os.path.exists(DB_NAME):
        return
    tages_praefix = f"auto_{datetime.now():%Y%m%d}"
    if os.path.isdir(BACKUP_DIR) and any(n.startswith(tages_praefix) for n in os.listdir(BACKUP_DIR)):
        return
    erstelle_backup("auto")
    alte_dateien_aufraeumen(BACKUP_DIR, "auto_", MAX_AUTO_BACKUPS)


def backups_liste():
    """Vorhandene Sicherungen, neueste zuerst: [(Dateiname, Zeitpunkt, Größe in Bytes)]."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    liste = []
    for name in os.listdir(BACKUP_DIR):
        if name.endswith(".db"):
            pfad = os.path.join(BACKUP_DIR, name)
            liste.append((name, datetime.fromtimestamp(os.path.getmtime(pfad)), os.path.getsize(pfad)))
    return sorted(liste, key=lambda x: x[1], reverse=True)


def backup_menu():
    ueberschrift("💾 DATENBANK-BACKUP")
    pfad = erstelle_backup()
    print(f"✅ Backup gespeichert unter: {pfad}")
    ordner = zweiter_backup_ordner()
    if ordner and backup_status["kopie_fehler"]:
        print(f"⚠️ Kopie nach {ordner} fehlgeschlagen: {backup_status['kopie_fehler']}")
    elif ordner:
        print(f"✅ Kopie liegt auch in: {ordner}")
    print(f"Automatisch wird einmal täglich gesichert (die letzten {MAX_AUTO_BACKUPS} Tage bleiben erhalten).")
    print("Zum Wiederherstellen: Programm beenden und die Backup-Datei als")
    print(f"'{DB_FILENAME}' in den Programmordner kopieren.")

    print(f"\nZweiter Backup-Ordner (z. B. USB-Stick oder OneDrive): {ordner or '– keiner –'}")
    neu = input("Neuen Ordner eingeben ('-' = keiner, Enter = so lassen): ").strip().strip('"')
    if neu == "-":
        set_einstellung(BACKUP_ORDNER_EINSTELLUNG, "")
        print("✅ Es wird nur noch im Ordner 'backups' gesichert.")
    elif neu:
        fehler = backup_ordner_pruefen(neu)
        if fehler:
            print(f"❌ {fehler}")
        else:
            set_einstellung(BACKUP_ORDNER_EINSTELLUNG, neu)
            erstelle_backup()
            print(f"✅ Gespeichert. Backups werden ab jetzt auch nach {neu} kopiert.")


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
    ("Datensicherung (Backup)", backup_menu),
    ("Stammdaten", manage_stammdaten),
    ("Buchdaten online nachladen (Beschreibung, Cover, Serie)", buchdaten_nachladen_menue),
    ("Regale & Standorte", regale_menue),
    ("Nach Programm-Updates suchen (GitHub)", checkForUpdates),
]


def main():
    alte_datenbank_uebernehmen()
    auto_backup()
    init_db()
    alte_ausleihen_loeschen()

    while True:
        auto_backup()  # falls das Programm über Nacht offen bleibt
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

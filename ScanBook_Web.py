"""ScanBook – Oberfläche im Browser.

Starten mit:  python3 ScanBook_Web.py
Danach öffnet sich der Browser automatisch. Beenden mit Strg+C im Terminal.
Die Oberfläche benutzt dieselbe Datenbank wie das Terminal-Programm ScanBook.py.
"""
import io
import os
import re
import html
import csv
import sys
import time
import json
import threading
import webbrowser
from datetime import date, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import ScanBook as sb

HOST = "127.0.0.1"
START_PORT = 8765
ERLAUBTE_HOSTS = {"127.0.0.1", "localhost"}


class ApiFehler(Exception):
    def __init__(self, text, status=400):
        super().__init__(text)
        self.status = status


# --- HELFER ---
def isbn_pruefen(wert):
    isbn = sb.normalize_isbn(str(wert or ""))
    if not isbn:
        raise ApiFehler("Ungültige ISBN – die Prüfziffer stimmt nicht.")
    return isbn


def buch_oder_fehler(isbn):
    buch = sb.lade_buch(isbn)
    if not buch:
        raise ApiFehler("Dieses Buch ist nicht im Katalog.", 404)
    buch["isbn"] = isbn
    buch["standort"], buch["standort_soll"] = sb.standort_von(isbn)
    return buch


def text(daten, feld):
    return str(daten.get(feld) or "").strip()


def positive_zahl(wert, default):
    try:
        zahl = int(wert)
    except (TypeError, ValueError):
        return default
    return zahl if zahl > 0 else default


def tage_bis(iso_datum):
    return (date.fromisoformat(iso_datum) - sb.heute()).days


def buchdaten_aus(body):
    return sb.neue_buchdaten(*(text(body, feld) for feld, _ in sb.BUCH_FELDER),
                             *(text(body, feld) for feld, _ in sb.SERIEN_FELDER),
                             *(text(body, feld) for feld in sb.ZUSATZ_FELDER))


def fehlende_felder(daten):
    return [feld for feld, _ in sb.BUCH_FELDER if daten[feld].strip() in sb.LEERWERTE]


# --- LESENDE ABFRAGEN (GET) ---
def api_info(_):
    return {
        "version": sb.VERSION,
        "bib_name": sb.get_einstellung("bib_name", "Schulbibliothek"),
        "schulname": sb.get_einstellung("schulname"),
        "standard_leihdauer": sb.get_einstellung_int("standard_leihdauer", 14),
    }


def api_books(params):
    suche = params.get("q", "").strip()
    sql = f"SELECT {sb.BUCH_SPALTEN}, autoren, verlag, cover_url FROM buecher"
    werte = ()
    if suche:
        muster = f"%{sb.normalize_isbn(suche) or suche}%"
        sql += " WHERE titel LIKE ? OR autoren LIKE ? OR genre LIKE ? OR isbn LIKE ? OR verlag LIKE ? OR serie LIKE ?"
        werte = (muster,) * 6
    with sb.db_connection() as conn:
        rows = conn.execute(sql + " ORDER BY autor_nachname, titel", werte).fetchall()
    plan = sb.regal_plan()
    orte = sb.standorte(plan)
    regal_nr = {r["id"]: i for i, r in enumerate(plan["regale"])}
    regal_name = {r["id"]: r["name"] for r in plan["regale"]}
    buecher = []
    for r in rows:
        ort, regal_id, geplant = orte.get(r[6], ("", None, False))
        buecher.append({
            "autor": r[0], "titel": r[1], "jahr": r[2], "genre": r[3], "gesamt": r[4],
            "verfuegbar": r[5], "isbn": r[6], "serie": r[7] or "", "band": r[8] or "", "autoren": r[9], "verlag": r[10],
            "standort": ort, "geplant": geplant, "regal": regal_name.get(regal_id, ""), "regal_nr": regal_nr.get(regal_id, -1),
            "cover_url": r[11] or "",
        })
    return buecher


def api_book(params):
    """Ein Buch mit allem, was die Vorschau braucht: aktuelle Ausleihen und die anderen Bände der Serie."""
    isbn = isbn_pruefen(params.get("isbn"))
    buch = buch_oder_fehler(isbn)
    with sb.db_connection() as conn:
        buch["ausleihen"] = [
            {"name": name, "bis": bis, "tage": tage_bis(bis), "exemplar": exemplar or ""}
            for name, bis, exemplar in conn.execute(
                "SELECT ausleiher, rueckgabe_bis, exemplar FROM ausleihen WHERE isbn = ? AND status = 'aktiv' ORDER BY rueckgabe_bis",
                (isbn,))
        ]
        baende = []
        if buch["serie"]:
            baende = [
                {"isbn": r[0], "titel": r[1], "band": r[2] or "", "verfuegbar": r[3], "gesamt": r[4]}
                for r in conn.execute(
                    "SELECT isbn, titel, band, anzahl_verfuegbar, anzahl_gesamt FROM buecher WHERE serie = ? COLLATE NOCASE",
                    (buch["serie"],))
            ]
    buch["serie_baende"] = sorted(baende, key=lambda b: (sb.band_sortierwert(b["band"]), b["titel"]))
    buch["exemplare"] = sb.exemplare_von(isbn)
    return buch


def api_loans(params):
    sql = '''
        SELECT a.id, a.isbn, b.titel, b.autor_formatiert, a.ausleiher, a.ausgeliehen_am, a.rueckgabe_bis,
               COALESCE(a.verlaengerungen, 0), COALESCE(s.klasse, ''), COALESCE(a.exemplar, '')
        FROM ausleihen a JOIN buecher b ON a.isbn = b.isbn LEFT JOIN schueler s ON s.id = a.schueler_id
        WHERE a.status = 'aktiv'
    '''
    werte = ()
    code = params.get("code") or params.get("isbn")
    if code:
        try:
            werte = tuple(sb.ausleihen_zum_code(code))
        except ValueError as e:
            raise ApiFehler(str(e))
        sql += f" AND a.id IN ({','.join('?' * len(werte)) or 'NULL'})"
    with sb.db_connection() as conn:
        rows = conn.execute(sql + " ORDER BY a.rueckgabe_bis", werte).fetchall()
    return [
        {"id": r[0], "isbn": r[1], "titel": r[2], "autor": r[3], "ausleiher": r[4],
         "ausgeliehen_am": r[5], "rueckgabe_bis": r[6], "verlaengerungen": r[7], "tage": tage_bis(r[6]),
         "klasse": r[8], "exemplar": r[9]}
        for r in rows
    ]


def api_borrowers(params):
    with sb.db_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ausleiher FROM ausleihen WHERE ausleiher LIKE ? ORDER BY ausleiher LIMIT 500",
            (f"%{params.get('q', '').strip()}%",),
        ).fetchall()
    return [r[0] for r in rows]


def api_borrower(params):
    name = params.get("name", "")
    with sb.db_connection() as conn:
        rows = conn.execute('''
            SELECT a.id, b.titel, a.ausgeliehen_am, a.rueckgabe_bis, a.status, a.zurueckgegeben_am
            FROM ausleihen a JOIN buecher b ON a.isbn = b.isbn
            WHERE a.ausleiher = ?
            ORDER BY a.ausgeliehen_am DESC
        ''', (name,)).fetchall()
    eintraege = [
        {"id": r[0], "titel": r[1], "ausgeliehen_am": r[2], "rueckgabe_bis": r[3],
         "aktiv": r[4] == "aktiv", "zurueckgegeben_am": r[5],
         "tage": tage_bis(r[3]) if r[4] == "aktiv" else None}
        for r in rows
    ]
    return {
        "name": name,
        "aktiv": [e for e in eintraege if e["aktiv"]],
        "frueher": [e for e in eintraege if not e["aktiv"]],
    }


def api_stats(_):
    with sb.db_connection() as conn:
        titel, exemplare, verfuegbar = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(anzahl_gesamt), 0), COALESCE(SUM(anzahl_verfuegbar), 0) FROM buecher"
        ).fetchone()
        aktiv, ueberfaellig, personen = conn.execute('''
            SELECT COUNT(*), COALESCE(SUM(rueckgabe_bis < ?), 0), COUNT(DISTINCT ausleiher)
            FROM ausleihen WHERE status = 'aktiv'
        ''', (sb.heute().isoformat(),)).fetchone()
        top = conn.execute('''
            SELECT b.titel, COUNT(*) AS anzahl
            FROM ausleihen a JOIN buecher b ON a.isbn = b.isbn
            GROUP BY a.isbn ORDER BY anzahl DESC, b.titel LIMIT 5
        ''').fetchall()
    return {
        "titel": titel, "exemplare": exemplare, "verfuegbar": verfuegbar,
        "aktiv": aktiv, "ueberfaellig": ueberfaellig, "personen": personen,
        "top": [{"titel": t, "anzahl": a} for t, a in top],
    }


def api_settings(_):
    return [
        {"key": key, "label": label, "zahl": pruefung is not None, "wert": sb.get_einstellung(key)}
        for key, label, pruefung in sb.STAMMDATEN
    ]


# --- SCHREIBENDE AKTIONEN (POST) ---
def sieht_aus_wie_isbn(eingabe):
    # Nur 10 oder 13 Stellen sind eine verscannte ISBN; andere Ziffernfolgen wie 405-70211226 sind Mediencodes
    ziffern = re.sub(r"[\s-]", "", eingabe)
    return bool(re.fullmatch(r"\d{9}[\dXx]|\d{13}", ziffern))


def api_scan(body):
    eingabe = sb.code_normalisieren(text(body, "isbn"))
    neuer_code = sb.code_normalisieren(text(body, "code"))  # vorher gescanntes, noch unbekanntes Etikett, das zugeordnet werden soll
    treffer = sb.code_aufloesen(eingabe)
    if treffer and treffer[0] == "exemplar":
        return {"status": "exemplar", "code": treffer[2], "buch": buch_oder_fehler(treffer[1])}
    if treffer and treffer[0] == "schueler":
        raise ApiFehler("Das ist ein Schülerausweis. Ausleihen geht im Tab „Ausleihe“.")
    if not treffer:
        if not sb.code_gueltig(eingabe) and sb.ungueltige_zeichen(eingabe):
            raise ApiFehler(f"„{eingabe}“ enthält Zeichen, die in keinem Barcode vorkommen: "
                            f"{' '.join(sb.ungueltige_zeichen(eingabe))}. Ist der Scanner auf die deutsche Tastatur eingestellt?")
        if neuer_code or sieht_aus_wie_isbn(eingabe) or not sb.code_gueltig(eingabe):
            raise ApiFehler("Ungültige ISBN – die Prüfziffer stimmt nicht.")
        # Unbekanntes Etikett: als eigener Mediencode merken, das nächste gescannte Buch bekommt ihn
        return {"status": "neuer_code", "code": eingabe}
    isbn = treffer[1]

    if neuer_code and sb.lade_buch(isbn):
        try:
            code, aufgestockt = sb.exemplar_anlegen(isbn, neuer_code)
        except ValueError as e:
            raise ApiFehler(str(e))
        return {"status": "zugeordnet", "code": code, "aufgestockt": aufgestockt, "buch": buch_oder_fehler(isbn)}

    if not neuer_code:
        with sb.db_connection() as conn:
            aufgestockt = conn.execute('''
                UPDATE buecher SET anzahl_gesamt = anzahl_gesamt + 1, anzahl_verfuegbar = anzahl_verfuegbar + 1
                WHERE isbn = ?
            ''', (isbn,)).rowcount
        if aufgestockt:
            return {"status": "aufgestockt", "buch": buch_oder_fehler(isbn)}

    daten = sb.fetch_book_data(isbn)
    return {
        "status": "neu",
        "isbn": isbn,
        "code": neuer_code,
        "daten": daten,
        "fehlend": fehlende_felder(daten) if daten else [feld for feld, _ in sb.BUCH_FELDER],
    }


def api_book_create(body):
    isbn = isbn_pruefen(body.get("isbn"))
    if not text(body, "titel"):
        raise ApiFehler("Bitte einen Titel eingeben.")
    if sb.lade_buch(isbn):
        raise ApiFehler("Dieses Buch ist schon im Katalog. Zum Aufstocken einfach scannen.")
    code = text(body, "code")
    if code:
        with sb.db_connection() as conn:
            try:
                sb.neuer_code_pruefen(conn, code)
            except ValueError as e:
                raise ApiFehler(str(e))
    sb.buch_speichern(isbn, buchdaten_aus(body), positive_zahl(body.get("anzahl"), 1),
                      nachgeladen=body.get("nachgeladen") in (True, "1"))
    if code:
        sb.exemplar_anlegen(isbn, code)
    _, umstellen, grund = sb.buch_einraeumen(isbn)
    buch = buch_oder_fehler(isbn)
    buch["umstellen"], buch["kein_platz"] = umstellen, grund
    return buch


def api_book_update(body):
    isbn = isbn_pruefen(body.get("isbn"))
    buch_oder_fehler(isbn)
    if not text(body, "titel"):
        raise ApiFehler("Bitte einen Titel eingeben.")
    daten = buchdaten_aus(body)
    felder = [f for f, _ in sb.BUCH_FELDER + sb.SERIEN_FELDER] + ["beschreibung"]
    sb.buch_aktualisieren(isbn, {f: daten[f] for f in felder})
    return buch_oder_fehler(isbn)


def api_book_refresh(body):
    isbn = isbn_pruefen(body.get("isbn"))
    buch_oder_fehler(isbn)
    ergaenzt = sb.daten_nachladen(isbn)
    return {"gefunden": ergaenzt is not None, "ergaenzt": ergaenzt or [], "nicht_erreichbar": sb.letzte_fehler()}


# Nachladen für den ganzen Katalog läuft im Hintergrund, die Oberfläche fragt den Fortschritt ab
nachladen_status = {"laeuft": False, "erledigt": 0, "gesamt": 0, "titel": "", "ergaenzt": 0, "fehler": ""}
nachladen_sperre = threading.Lock()


def nachladen_im_hintergrund():
    def fortschritt(nr, gesamt, titel):
        nachladen_status.update(erledigt=nr, gesamt=gesamt, titel=titel)
    try:
        _, ergaenzt = sb.alle_nachladen(fortschritt)
        nachladen_status["ergaenzt"] = ergaenzt
    except Exception as e:
        print(f"⚠️ Nachladen abgebrochen: {e}")
        nachladen_status["fehler"] = str(e)
    finally:
        nachladen_status["laeuft"] = False


def api_refresh_all(_):
    with nachladen_sperre:
        if not nachladen_status["laeuft"]:
            offen = len(sb.noch_nicht_nachgeladen())
            nachladen_status.update(laeuft=bool(offen), erledigt=0, gesamt=offen, titel="", ergaenzt=0, fehler="")
            if offen:
                threading.Thread(target=nachladen_im_hintergrund, daemon=True).start()
    return api_refresh_status(None)


def api_refresh_status(_):
    return dict(nachladen_status, offen=len(sb.noch_nicht_nachgeladen()))


def api_book_remove_copy(body):
    isbn = isbn_pruefen(body.get("isbn"))
    buch_oder_fehler(isbn)
    try:
        sb.exemplar_entfernen(isbn)
    except ValueError as e:
        raise ApiFehler(str(e))
    return buch_oder_fehler(isbn)


def api_book_delete(body):
    isbn = isbn_pruefen(body.get("isbn"))
    buch_oder_fehler(isbn)
    try:
        sb.buch_loeschen(isbn)
    except ValueError as e:
        raise ApiFehler(str(e))
    return {"ok": True}


def api_checkout(body):
    """Buch (Mediencode oder ISBN) an eine Person (Schülerausweis oder Name) ausleihen."""
    treffer = sb.code_aufloesen(text(body, "code") or text(body, "isbn"))
    if not treffer or treffer[0] == "schueler":
        raise ApiFehler("Bitte ein Buch scannen (Mediencode oder ISBN).")
    _, isbn, exemplar = treffer
    buch = buch_oder_fehler(isbn)
    ausleiher, schueler_id = sb.person_aufloesen(text(body, "ausleiher"))
    if not ausleiher:
        raise ApiFehler("Bitte den Schülerausweis scannen oder einen Namen eingeben.")
    tage = positive_zahl(body.get("tage"), sb.get_einstellung_int("standard_leihdauer", 14))
    try:
        ausleiher, rueckgabe_bis = sb.buch_ausleihen(isbn, ausleiher, tage, schueler_id, exemplar)
    except ValueError as e:
        raise ApiFehler(str(e))
    return {"titel": buch["titel"], "ausleiher": ausleiher, "rueckgabe_bis": rueckgabe_bis.isoformat(),
            "exemplar": exemplar or ""}


def aktive_ausleihe(conn, ausleihe_id):
    row = conn.execute('''
        SELECT a.isbn, b.titel, a.ausleiher, a.rueckgabe_bis
        FROM ausleihen a JOIN buecher b ON a.isbn = b.isbn
        WHERE a.id = ? AND a.status = 'aktiv'
    ''', (ausleihe_id,)).fetchone()
    if not row:
        raise ApiFehler("Diese Ausleihe gibt es nicht (mehr).", 404)
    return row


def api_return(body):
    with sb.db_connection() as conn:
        isbn, titel, ausleiher, rueckgabe_bis = aktive_ausleihe(conn, body.get("id"))
        conn.execute(
            "UPDATE ausleihen SET status = 'zurueckgegeben', zurueckgegeben_am = ? WHERE id = ?",
            (sb.heute().isoformat(), body.get("id")),
        )
        conn.execute("UPDATE buecher SET anzahl_verfuegbar = anzahl_verfuegbar + 1 WHERE isbn = ?", (isbn,))
    return {"titel": titel, "ausleiher": ausleiher, "verspaetung": max(0, -tage_bis(rueckgabe_bis))}


def api_extend(body):
    tage = positive_zahl(body.get("tage"), sb.get_einstellung_int("standard_leihdauer", 14))
    with sb.db_connection() as conn:
        _, titel, ausleiher, rueckgabe_bis = aktive_ausleihe(conn, body.get("id"))
        # Bei überfälligen Büchern ab heute verlängern, sonst ab dem bisherigen Rückgabedatum
        neues_datum = max(sb.heute(), date.fromisoformat(rueckgabe_bis)) + timedelta(days=tage)
        conn.execute('''
            UPDATE ausleihen SET rueckgabe_bis = ?, verlaengerungen = COALESCE(verlaengerungen, 0) + 1
            WHERE id = ?
        ''', (neues_datum.isoformat(), body.get("id")))
    return {"titel": titel, "ausleiher": ausleiher, "rueckgabe_bis": neues_datum.isoformat()}


def api_settings_save(body):
    werte = body.get("werte") or {}
    for key, label, pruefung in sb.STAMMDATEN:
        if key not in werte:
            continue
        neu = str(werte[key]).strip()
        if not neu:
            raise ApiFehler(f"'{label}' darf nicht leer sein.")
        if pruefung and not neu.isdigit():
            raise ApiFehler(f"'{label}': bitte eine ganze Zahl eingeben.")
        if pruefung == "positiv" and int(neu) < 1:
            raise ApiFehler(f"'{label}' muss mindestens 1 sein.")
    for key, _, _ in sb.STAMMDATEN:
        if key in werte:
            sb.set_einstellung(key, str(werte[key]).strip())
    return api_settings(None)


def api_backup(_):
    pfad = sb.erstelle_backup()
    return {"pfad": pfad, "kopie_fehler": sb.backup_status["kopie_fehler"]}


def api_backups(_):
    liste = sb.backups_liste()
    return {
        "ordner": sb.BACKUP_DIR,
        "anzahl": len(liste),
        "letztes": liste[0][1].isoformat(timespec="minutes") if liste else "",
        "behalten": sb.MAX_AUTO_BACKUPS,
        "zweiter_ordner": sb.zweiter_backup_ordner(),
        "kopie_fehler": sb.backup_status["kopie_fehler"],
    }


def api_backup_ordner(body):
    ordner = text(body, "ordner").strip('"')
    if ordner:
        fehler = sb.backup_ordner_pruefen(ordner)
        if fehler:
            raise ApiFehler(fehler)
    sb.set_einstellung(sb.BACKUP_ORDNER_EINSTELLUNG, ordner)
    if ordner:
        sb.erstelle_backup()  # gleich eine erste Kopie dorthin legen
    return api_backups(None)


def backups_im_hintergrund():
    """Läuft ScanBook über Nacht weiter, wird trotzdem jeden Tag gesichert."""
    while True:
        time.sleep(15 * 60)
        try:
            sb.auto_backup()
        except Exception as e:  # eine fehlgeschlagene Sicherung darf den Server nicht stoppen
            print(f"⚠️ Automatisches Backup fehlgeschlagen: {e}")


# --- REGALE ---
def regal_buch(regale, b):
    return {"isbn": b["isbn"], "titel": b["titel"], "autor": b["autor"], "serie": b["serie"], "band": b["band"],
            "anzahl": b["anzahl"], "genre": ", ".join(b["genres"]),
            "neu": not b["ist"], "von": sb.standort_text(regale, b["ist"]) if b["ist"] != b["soll"] else ""}


def api_regale(_):
    plan = sb.regal_plan()
    regale = plan["regale"]
    return {
        "regale": [
            {"id": r["id"], "name": r["name"], "faecher": r["faecher"], "kapazitaet": r["kapazitaet"], "genres": r["genres"],
             "fach_genres": r["fach_genres"],
             "inhalt": [[regal_buch(regale, b) for b in fach] for fach in r["inhalt"]]}
            for r in regale
        ],
        "umstellen": [dict(regal_buch(regale, b), nach=sb.standort_text(regale, b["soll"])) for b in sb.umstellungen(plan)],
        "einraeumen": len(sb.noch_einzuraeumen(plan)),
        "ohne_platz": [dict(regal_buch(regale, b), grund=grund) for b, grund in plan["ohne_platz"]],
        "genres": sb.alle_genres(),
    }


def api_regal_save(body):
    name = text(body, "name")
    if not name:
        raise ApiFehler("Bitte einen Namen für das Regal eingeben.")
    faecher, kapazitaet = positive_zahl(body.get("faecher"), 0), positive_zahl(body.get("kapazitaet"), 0)
    if not faecher or not kapazitaet:
        raise ApiFehler("Fächer und Bücher pro Fach müssen mindestens 1 sein.")
    regal_id = body.get("id") or None
    if regal_id and regal_id not in {r["id"] for r in sb.lade_regale()}:
        raise ApiFehler("Dieses Regal gibt es nicht (mehr).", 404)
    fach_genres = body.get("fach_genres")
    fach_genres = [sb.genre_liste(str(g or "")) for g in fach_genres] if isinstance(fach_genres, list) else None
    sb.regal_speichern(regal_id, name, faecher, kapazitaet, sb.genre_liste(text(body, "genres")), fach_genres)
    return api_regale(None)


def api_regal_delete(body):
    sb.regal_loeschen(body.get("id"))
    return api_regale(None)


def api_regal_move(body):
    sb.regal_verschieben(body.get("id"), -1 if body.get("richtung") == "hoch" else 1)
    return api_regale(None)


def api_regale_uebernehmen(_):
    sb.plan_uebernehmen()
    return api_regale(None)


# --- PROGRAMM-UPDATE ---
neustart = {"angefordert": False}


def api_update_check(_):
    try:
        version, quelle, dateien = sb.update_suchen()
    except ValueError as e:
        raise ApiFehler(str(e))
    except Exception as e:
        raise ApiFehler(f"GitHub ist gerade nicht erreichbar ({e}).", 502)
    return {"installiert": sb.VERSION, "verfuegbar": version, "quelle": quelle, "neu": bool(dateien)}


def api_update_install(_):
    try:
        version, _, dateien = sb.update_suchen()
    except ValueError as e:
        raise ApiFehler(str(e))
    except Exception as e:
        raise ApiFehler(f"GitHub ist gerade nicht erreichbar ({e}).", 502)
    if not dateien:
        raise ApiFehler("ScanBook ist schon auf dem neuesten Stand.")
    backups = sb.update_installieren(dateien)
    print(f"🚀 Update auf Version {version} installiert – ScanBook startet neu ...")
    neustart["angefordert"] = True
    # Erst antworten, dann den Server anhalten; main() startet das Programm danach neu
    threading.Thread(target=lambda: (time.sleep(0.5), server_ref["server"].shutdown()), daemon=True).start()
    return {"version": version, "backups": backups}


# --- BARCODES (Code 128, ohne Zusatzpakete) ---
# Breiten von Strich/Lücke/Strich/Lücke/Strich/Lücke für die Werte 0–106 (104 = Start B, 106 = Stopp)
CODE128 = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 "
    "221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 "
    "221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
    "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 "
    "231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 "
    "231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
    "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 "
    "112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
    "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 "
    "114131 311141 411131 211412 211214 211232 2331112"
).split()


def code128_module(text):
    """Strich- und Lückenbreiten (in Modulen) für Code 128 B, mit Prüfzeichen."""
    werte = [104] + [ord(z) - 32 for z in text]
    pruefsumme = sum(w * max(i, 1) for i, w in enumerate(werte)) % 103
    return "".join(CODE128[w] for w in werte + [pruefsumme, 106])


def barcode_svg(text, hoehe=40):
    """Barcode als SVG, das sich im Browser in jeder Größe scharf drucken lässt."""
    ruhezone = 10
    x, striche = ruhezone, []
    for i, breite in enumerate(code128_module(text)):
        if i % 2 == 0:
            striche.append(f'<rect x="{x}" width="{breite}" height="{hoehe}"/>')
        x += int(breite)
    return (f'<svg class="barcode" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {x + ruhezone} {hoehe}" '
            f'preserveAspectRatio="none" shape-rendering="crispEdges">{"".join(striche)}</svg>')


# --- SCHÜLER ---
def schueler_daten(row):
    sid, ausweis, vorname, nachname, klasse, aktiv, ueberfaellig = row
    return {"id": sid, "ausweis": ausweis, "vorname": vorname, "nachname": nachname, "klasse": klasse or "",
            "name": sb.schueler_name(vorname, nachname), "aktiv": aktiv, "ueberfaellig": ueberfaellig}


# Aktive Ausleihen einmal pro Schüler zusammenzählen (statt zwei Unterabfragen je Schüler)
SCHUELER_SQL = '''
    SELECT s.id, s.ausweis, s.vorname, s.nachname, s.klasse, COALESCE(z.aktiv, 0), COALESCE(z.ueber, 0)
    FROM schueler s LEFT JOIN (
        SELECT schueler_id, COUNT(*) AS aktiv, SUM(rueckgabe_bis < ?) AS ueber
        FROM ausleihen WHERE status = 'aktiv' AND schueler_id IS NOT NULL GROUP BY schueler_id
    ) z ON z.schueler_id = s.id
'''


def lade_schuelerliste(klasse=None, suche="", ids=None):
    sql, werte = SCHUELER_SQL + " WHERE 1", [sb.heute().isoformat()]
    if klasse is not None:
        sql += " AND s.klasse = ?"
        werte.append(klasse)
    if suche:
        sql += " AND (s.vorname || ' ' || s.nachname LIKE ? OR s.nachname || ' ' || s.vorname LIKE ? OR s.ausweis LIKE ?)"
        werte += [f"%{suche}%"] * 3
    if ids is not None:
        sql += f" AND s.id IN ({','.join('?' * len(ids)) or 'NULL'})"
        werte += ids
    with sb.db_connection() as conn:
        rows = conn.execute(sql, werte).fetchall()
    liste = [schueler_daten(r) for r in rows]
    liste.sort(key=lambda s: (sb.klassen_sortierwert(s["klasse"]), s["nachname"].lower(), s["vorname"].lower()))
    return liste


def api_schueler(params):
    klasse = params.get("klasse")
    with sb.db_connection() as conn:
        klassen = [k for (k,) in conn.execute("SELECT DISTINCT klasse FROM schueler")]
    return {
        "schueler": lade_schuelerliste(klasse, params.get("q", "").strip()),
        "klassen": sorted(klassen, key=sb.klassen_sortierwert),
    }


def schueler_mit_ausleihen(schueler_id):
    s = sb.lade_schueler(schueler_id)
    if not s:
        raise ApiFehler("Diesen Schüler gibt es nicht (mehr).", 404)
    with sb.db_connection() as conn:
        s["ausleihen"] = [
            {"titel": t, "bis": bis, "tage": tage_bis(bis), "exemplar": ex or ""}
            for t, bis, ex in conn.execute('''
                SELECT b.titel, a.rueckgabe_bis, a.exemplar FROM ausleihen a JOIN buecher b ON a.isbn = b.isbn
                WHERE a.schueler_id = ? AND a.status = 'aktiv' ORDER BY a.rueckgabe_bis
            ''', (schueler_id,))
        ]
    return s


def api_code(params):
    """Was steckt hinter einem gescannten Code? Schülerausweis, Mediencode oder ISBN."""
    eingabe = text(params, "c")
    treffer = sb.code_aufloesen(eingabe)
    if not treffer:
        raise ApiFehler(f"Der Code „{eingabe}“ ist weder ein Schülerausweis noch ein Mediencode oder eine gültige ISBN.", 404)
    typ, schluessel, code = treffer
    if typ == "schueler":
        return {"typ": "schueler", "schueler": schueler_mit_ausleihen(schluessel)}
    buch = buch_oder_fehler(schluessel)
    besitzer = ""
    if code:
        with sb.db_connection() as conn:
            row = conn.execute("SELECT ausleiher FROM ausleihen WHERE exemplar = ? AND status = 'aktiv'", (code,)).fetchone()
        besitzer = row[0] if row else ""
    return {"typ": "buch", "buch": buch, "exemplar": code or "", "ausgeliehen_an": besitzer}


def api_schueler_save(body):
    try:
        sid = sb.schueler_speichern(body.get("id") or None, text(body, "vorname"), text(body, "nachname"),
                                    text(body, "klasse"), text(body, "ausweis"))
    except ValueError as e:
        raise ApiFehler(str(e))
    return sb.lade_schueler(sid)


def api_schueler_delete(body):
    try:
        sb.schueler_loeschen(body.get("id"))
    except ValueError as e:
        raise ApiFehler(str(e))
    return {"ok": True}


def api_schueler_import(body):
    return sb.schueler_importieren(body.get("text"))


# --- MEDIENCODES ---
def api_exemplar_neu(body):
    isbn = isbn_pruefen(body.get("isbn"))
    try:
        code, aufgestockt = sb.exemplar_anlegen(isbn, text(body, "code"))
    except ValueError as e:
        raise ApiFehler(str(e))
    return {"code": code, "aufgestockt": aufgestockt, "buch": buch_oder_fehler(isbn)}


def api_codes_erzeugen(body):
    isbn = isbn_pruefen(body.get("isbn")) if body.get("isbn") else None
    return {"codes": sb.fehlende_codes_erzeugen(isbn)}


def api_exemplar_loeschen(body):
    try:
        sb.exemplar_loeschen(text(body, "code"))
    except ValueError as e:
        raise ApiFehler(str(e))
    return {"ok": True}


def api_exemplar_entfernen(body):
    isbn = isbn_pruefen(body.get("isbn"))
    try:
        sb.exemplar_entfernen(isbn, text(body, "code") or None)
    except ValueError as e:
        raise ApiFehler(str(e))
    return buch_oder_fehler(isbn)


def api_etiketten_gedruckt(body):
    codes = [str(c) for c in body.get("codes") or []]
    with sb.db_connection() as conn:
        conn.executemany("UPDATE exemplare SET gedruckt = 1 WHERE code = ?", [(c,) for c in codes])
    return {"ok": True}


def api_codes_status(_):
    with sb.db_connection() as conn:
        mit_code, ungedruckt = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(eigener = 0 AND gedruckt = 0), 0) FROM exemplare").fetchone()
    return {"mit_code": mit_code, "ohne_code": sb.exemplare_ohne_code(), "ungedruckt": ungedruckt}


# --- DRUCKSEITEN (Ausweise und Etiketten, gedruckt über den Browser) ---
DRUCK_CSS = """
* { box-sizing: border-box; }
body { margin: 0; font: 13px/1.3 -apple-system, BlinkMacSystemFont, "Helvetica Neue", "Segoe UI", Roboto, Arial, sans-serif; color: #000; background: #f2f2f7; }
.leiste { position: sticky; top: 0; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; padding: 12px 20px; background: rgba(255,255,255,.92); border-bottom: .5px solid rgba(60,60,67,.2); }
.leiste .grow { flex: 1; min-width: 200px; color: rgba(60,60,67,.7); }
.leiste button { font: inherit; font-weight: 600; border: 0; border-radius: 10px; padding: 8px 16px; background: #007aff; color: #fff; cursor: pointer; }
.leiste button.zweit { background: rgba(118,118,128,.12); color: #007aff; }
.blatt { width: 210mm; height: 297mm; margin: 16px auto; background: #fff; display: grid; align-content: start; overflow: hidden; box-shadow: 0 2px 12px rgba(0,0,0,.1); break-after: page; }
.barcode { display: block; width: 100%; fill: #000; }
.leer { padding: 40px; text-align: center; color: rgba(60,60,67,.7); }
/* Schülerausweise im Scheckkartenformat, 10 pro A4-Seite */
.ausweise { grid-template-columns: repeat(2, 85.6mm); grid-auto-rows: 54mm; gap: 3mm 8mm; padding: 7mm 15.4mm; }
.ausweis { border: .3mm dashed #bbb; border-radius: 3.2mm; padding: 4mm 5mm 3mm; display: flex; flex-direction: column; overflow: hidden; break-inside: avoid; }
.ausweis .kopf { display: flex; justify-content: space-between; gap: 4mm; font-size: 8pt; color: #555; border-bottom: .3mm solid #007aff; padding-bottom: 1.5mm; }
.ausweis .kopf b { color: #007aff; }
.ausweis .name { font-size: 14pt; font-weight: 700; margin-top: 2.5mm; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ausweis .klasse { font-size: 10pt; color: #333; }
.ausweis .barcode { height: 12mm; margin-top: auto; }
.ausweis .nummer { text-align: center; font: 9pt ui-monospace, Menlo, Consolas, monospace; letter-spacing: .1em; margin-top: .8mm; }
/* Buchetiketten 70 × 37 mm, 24 pro A4-Seite (3 × 8) */
.etiketten { grid-template-columns: repeat(3, 70mm); grid-auto-rows: 37mm; }
.etikett { padding: 3mm 5mm 2mm; display: flex; flex-direction: column; overflow: hidden; break-inside: avoid; outline: .2mm dashed #ddd; outline-offset: -.1mm; }
.etikett .bib { font-size: 7pt; color: #555; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.etikett .titel { font-size: 8.5pt; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.etikett .barcode { height: 13mm; margin-top: auto; }
.etikett .nummer { text-align: center; font: 9pt ui-monospace, Menlo, Consolas, monospace; letter-spacing: .1em; }
@page { size: A4; margin: 0; }
@media print {
  body { background: #fff; }
  .leiste { display: none; }
  .blatt { margin: 0; box-shadow: none; }
  .ausweis, .etikett { outline: none; }
}
"""


def druckseite(titel, hinweis, felder, pro_seite, klasse, skript=""):
    knopf = '<button onclick="window.print()">Drucken</button>' if felder else ""
    blaetter = "".join(f'<div class="blatt {klasse}">{"".join(felder[i:i + pro_seite])}</div>'
                       for i in range(0, len(felder), pro_seite))
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><title>{html.escape(titel)}</title>
<style>{DRUCK_CSS}</style></head><body>
<div class="leiste"><span class="grow"><b>{html.escape(titel)}</b> · {html.escape(hinweis)}</span>{knopf}
<button class="zweit" onclick="window.close()">Schließen</button></div>
{blaetter or '<p class="leer">Nichts zu drucken.</p>'}
<script>{skript}</script></body></html>"""


def druck_ausweise(params):
    ids = [int(i) for i in params.get("ids", "").split(",") if i.isdigit()] if params.get("ids") else None
    liste = lade_schuelerliste(params.get("klasse"), ids=ids)
    bib = sb.get_einstellung("bib_name", "Schulbibliothek")
    schule = sb.get_einstellung("schulname")
    karten = [f"""<div class="ausweis">
  <div class="kopf"><b>{html.escape(bib)}</b><span>{html.escape(schule)}</span></div>
  <div class="name">{html.escape(s["name"])}</div>
  <div class="klasse">{f"Klasse {html.escape(s['klasse'])}" if s["klasse"] else "Leseausweis"}</div>
  {barcode_svg(s["ausweis"])}
  <div class="nummer">{html.escape(s["ausweis"])}</div>
</div>""" for s in liste]
    titel = f"Ausweise Klasse {params['klasse']}" if params.get("klasse") else "Schülerausweise"
    return druckseite(titel, f"{len(liste)} Ausweis(e), 10 pro Seite. Beim Drucken „Tatsächliche Größe“ bzw. 100 % wählen.",
                      karten, 10, "ausweise")


def druck_etiketten(params):
    sql = '''
        SELECT e.code, b.titel FROM exemplare e JOIN buecher b ON b.isbn = e.isbn
    '''
    if params.get("isbn"):
        sql, werte = sql + " WHERE e.isbn = ? AND e.eigener = 0", (isbn_pruefen(params["isbn"]),)
    elif params.get("codes"):
        codes = params["codes"].split(",")
        sql, werte = sql + f" WHERE e.code IN ({','.join('?' * len(codes))})", codes
    else:  # alle selbst erzeugten Codes, die noch nicht gedruckt wurden
        sql, werte = sql + " WHERE e.eigener = 0 AND e.gedruckt = 0", ()
    with sb.db_connection() as conn:
        rows = conn.execute(sql + " ORDER BY b.autor_nachname, b.titel, e.code", werte).fetchall()
    bib = sb.get_einstellung("bib_name", "Schulbibliothek")
    # Leere Felder am Anfang, damit ein angebrochener Etikettenbogen weiter benutzt werden kann
    leer = min(positive_zahl(params.get("frei"), 0), 23)
    etiketten = ['<div class="etikett"></div>'] * leer + [f"""<div class="etikett">
  <div class="bib">{html.escape(bib)}</div><div class="titel">{html.escape(titel)}</div>
  {barcode_svg(code)}<div class="nummer">{html.escape(code)}</div>
</div>""" for code, titel in rows]
    codes = json.dumps([c for c, _ in rows])
    # Nach dem Drucken als gedruckt merken, damit sie beim nächsten Mal nicht wieder erscheinen
    skript = f"""window.addEventListener("afterprint", () => fetch("/api/etiketten_gedruckt", {{
  method: "POST", headers: {{"Content-Type": "application/json", "X-ScanBook": "1"}}, body: JSON.stringify({{codes: {codes}}})
}}));"""
    return druckseite("Buchetiketten", f"{len(rows)} Etikett(en) im Format 70 × 37 mm, 24 pro Seite (z. B. Zweckform 3474). "
                      "Beim Drucken 100 % wählen.", etiketten if rows else [], 24, "etiketten", skript)


DRUCK_ROUTEN = {"ausweise": druck_ausweise, "etiketten": druck_etiketten}


GET_ROUTEN = {
    "info": api_info,
    "books": api_books,
    "backups": api_backups,
    "book": api_book,
    "refresh_status": api_refresh_status,
    "loans": api_loans,
    "borrowers": api_borrowers,
    "borrower": api_borrower,
    "stats": api_stats,
    "settings": api_settings,
    "regale": api_regale,
    "update_check": api_update_check,
    "schueler": api_schueler,
    "code": api_code,
    "codes_status": api_codes_status,
}

POST_ROUTEN = {
    "scan": api_scan,
    "book_create": api_book_create,
    "book_update": api_book_update,
    "book_remove_copy": api_book_remove_copy,
    "book_delete": api_book_delete,
    "book_refresh": api_book_refresh,
    "refresh_all": api_refresh_all,
    "checkout": api_checkout,
    "return": api_return,
    "extend": api_extend,
    "settings": api_settings_save,
    "backup": api_backup,
    "backup_ordner": api_backup_ordner,
    "regal_save": api_regal_save,
    "regal_delete": api_regal_delete,
    "regal_move": api_regal_move,
    "regale_uebernehmen": api_regale_uebernehmen,
    "update_install": api_update_install,
    "schueler_save": api_schueler_save,
    "schueler_delete": api_schueler_delete,
    "schueler_import": api_schueler_import,
    "exemplar_neu": api_exemplar_neu,
    "exemplar_loeschen": api_exemplar_loeschen,
    "exemplar_entfernen": api_exemplar_entfernen,
    "codes_erzeugen": api_codes_erzeugen,
    "etiketten_gedruckt": api_etiketten_gedruckt,
}

EXPORT_TYPEN = ("katalog", "ausleihen", "mahnliste")


# --- WEBSERVER ---
class Handler(BaseHTTPRequestHandler):
    server_version = "ScanBook"

    def log_message(self, *args):
        pass  # Terminal nicht mit Zugriffen vollschreiben

    def _senden(self, status, inhalt, content_type, extra_header=None):
        daten = inhalt.encode("utf-8") if isinstance(inhalt, str) else inhalt
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(daten)))
        self.send_header("Cache-Control", "no-store")
        for name, wert in (extra_header or {}).items():
            self.send_header(name, wert)
        self.end_headers()
        self.wfile.write(daten)

    def _json(self, status, daten):
        self._senden(status, json.dumps(daten, ensure_ascii=False), "application/json; charset=utf-8")

    def _host_erlaubt(self):
        # Schutz davor, dass fremde Webseiten über den Browser auf ScanBook zugreifen
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ERLAUBTE_HOSTS:
            self._json(403, {"fehler": "Zugriff verweigert."})
            return False
        return True

    def _api_ausfuehren(self, funktion, argument):
        try:
            self._json(200, funktion(argument))
        except ApiFehler as e:
            self._json(e.status, {"fehler": str(e)})
        except Exception as e:
            print(f"⚠️ Fehler: {e}")
            self._json(500, {"fehler": f"Interner Fehler: {e}"})

    def do_GET(self):
        if not self._host_erlaubt():
            return
        url = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(url.query).items()}

        if url.path == "/":
            self._senden(200, HTML, "text/html; charset=utf-8")
        elif url.path.startswith("/api/") and url.path[5:] in GET_ROUTEN:
            self._api_ausfuehren(GET_ROUTEN[url.path[5:]], params)
        elif url.path.startswith("/druck/") and url.path[7:] in DRUCK_ROUTEN:
            try:
                self._senden(200, DRUCK_ROUTEN[url.path[7:]](params), "text/html; charset=utf-8")
            except ApiFehler as e:
                self._senden(e.status, html.escape(str(e)), "text/plain; charset=utf-8")
        elif url.path.startswith("/export/") and url.path[8:-4] in EXPORT_TYPEN and url.path.endswith(".csv"):
            typ = url.path[8:-4]
            kopf, zeilen = sb.export_daten(typ)
            puffer = io.StringIO()
            writer = csv.writer(puffer, delimiter=";")
            writer.writerow(kopf)
            writer.writerows(zeilen)
            dateiname = f"{typ}_{sb.heute():%Y%m%d}.csv"
            self._senden(200, "﻿" + puffer.getvalue(), "text/csv; charset=utf-8",
                         {"Content-Disposition": f'attachment; filename="{dateiname}"'})
        else:
            self._json(404, {"fehler": "Nicht gefunden."})

    def do_POST(self):
        if not self._host_erlaubt():
            return
        if self.headers.get("X-ScanBook") != "1":
            self._json(403, {"fehler": "Zugriff verweigert."})
            return
        name = urlparse(self.path).path[5:]
        if not self.path.startswith("/api/") or name not in POST_ROUTEN:
            self._json(404, {"fehler": "Nicht gefunden."})
            return
        try:
            laenge = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(laenge) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            self._json(400, {"fehler": "Ungültige Anfrage."})
            return
        self._api_ausfuehren(POST_ROUTEN[name], body)


server_ref = {}


def starte_server():
    # Nach einem Update wieder denselben Port nehmen, damit die offene Seite weiterläuft
    wunsch = [int(a.split("=", 1)[1]) for a in sys.argv[1:] if a.startswith("--port=") and a[7:].isdigit()]
    for port in wunsch + list(range(START_PORT, START_PORT + 20)):
        try:
            return ThreadingHTTPServer((HOST, port), Handler), port
        except OSError:
            continue
    raise SystemExit("❌ Kein freier Port gefunden.")


def main():
    sb.alte_datenbank_uebernehmen()
    sb.auto_backup()
    sb.init_db()
    sb.alte_ausleihen_loeschen()

    server, port = starte_server()
    server_ref["server"] = server
    threading.Thread(target=backups_im_hintergrund, daemon=True).start()
    url = f"http://localhost:{port}/"
    print(f"📚 ScanBook {sb.VERSION} – Oberfläche läuft unter: {url}")
    print("   Zum Beenden hier im Terminal Strg+C drücken.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nProgramm beendet. Auf Wiedersehen!")
    finally:
        server.server_close()

    if neustart["angefordert"]:
        sys.argv = [sys.argv[0], "--no-browser", f"--port={port}"]
        sb.neu_starten(os.path.realpath(__file__))


# --- OBERFLÄCHE (HTML, CSS & JAVASCRIPT) ---
HTML = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ScanBook</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'><rect width='512' height='512' rx='112' fill='%23234a70'/><path d='M92 170Q172 140 250 176V386Q172 352 92 378Z' fill='%23fff'/><path d='M420 170Q340 140 262 176V386Q340 352 420 378Z' fill='%23fff'/><rect x='68' y='252' width='376' height='24' rx='12' fill='%23ff5a4e'/></svg>">
<style>
/* Gestaltung nach Apples Human Interface Guidelines: Systemschrift, Systemfarben,
   gruppierte Listen, Seitenleiste (breit) bzw. Tab-Leiste unten (schmal), Hell/Dunkel automatisch */
:root {
  --bg: #f2f2f7; --surface: #ffffff; --surface-2: #f2f2f7; --fill: rgba(118, 118, 128, .12); --border: rgba(60, 60, 67, .18);
  --text: #000000; --muted: rgba(60, 60, 67, .6); --accent: #007aff; --accent-hover: #0066d6; --accent-soft: rgba(0, 122, 255, .1);
  --danger: #ff3b30; --danger-soft: rgba(255, 59, 48, .12); --warn: #c77c00; --warn-soft: rgba(255, 149, 0, .14); --ok: #248a3d; --ok-soft: rgba(52, 199, 89, .14);
  --bar: rgba(249, 249, 249, .78);
  /* Seitenleiste in Nachtblau, passend zum Logo */
  --side: linear-gradient(180deg, #22385f, #16263f); --side-text: rgba(255, 255, 255, .86); --side-muted: rgba(255, 255, 255, .55);
  --side-icon: #8fb4ff; --side-hover: rgba(255, 255, 255, .08); --side-active: rgba(255, 255, 255, .15); --marke: #ff5a4e;
  --shadow: none;
  --radius: 12px;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #000000; --surface: #1c1c1e; --surface-2: #2c2c2e; --fill: rgba(118, 118, 128, .24); --border: rgba(84, 84, 88, .6);
    --text: #ffffff; --muted: rgba(235, 235, 245, .6); --accent: #0a84ff; --accent-hover: #409cff; --accent-soft: rgba(10, 132, 255, .2);
    --danger: #ff453a; --danger-soft: rgba(255, 69, 58, .2); --warn: #ff9f0a; --warn-soft: rgba(255, 159, 10, .18); --ok: #30d158; --ok-soft: rgba(48, 209, 88, .18);
    --bar: rgba(22, 22, 24, .8); --side: linear-gradient(180deg, #15233b, #0b1424);
    color-scheme: dark;
  }
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.47 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", "Segoe UI", Roboto, Arial, sans-serif;
  -webkit-font-smoothing: antialiased; letter-spacing: -.01em;
}
/* Seitenleiste */
header {
  position: fixed; top: 0; bottom: 0; left: 0; width: 232px; z-index: 10;
  background: var(--side);
}
.header-inner { height: 100%; display: flex; flex-direction: column; padding: 18px 10px 14px; }
.brand { display: grid; grid-template-columns: 42px 1fr; column-gap: 12px; align-items: center; padding: 2px 8px 20px; }
.brand .logo { grid-row: 1 / span 2; width: 42px; height: 42px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0, 0, 0, .35); }
.brand h1 { margin: 0; font-size: 19px; font-weight: 700; letter-spacing: -.02em; color: #fff; align-self: end; }
.brand .bib { color: var(--side-muted); font-size: 12px; line-height: 1.3; align-self: start; }
.brand .version { position: absolute; bottom: 16px; left: 20px; color: var(--side-muted); font-size: 12px; }
#update-hinweis { grid-column: 1 / -1; }
nav { display: flex; flex-direction: column; gap: 2px; }
nav button {
  display: flex; align-items: center; gap: 10px; width: 100%; text-align: left;
  border: 0; background: none; color: var(--side-text); font: inherit; font-size: 14px; font-weight: 500;
  padding: 7px 10px; border-radius: 8px; cursor: pointer; white-space: nowrap;
}
nav button svg { width: 19px; height: 19px; flex: none; color: var(--side-icon); }
nav button:hover { background: var(--side-hover); }
nav button.active { background: var(--side-active); color: #fff; box-shadow: inset 3px 0 0 var(--marke); }
nav button.active svg { color: #fff; }
main { margin-left: 232px; max-width: 1060px; padding: 34px 36px 60px; }
.seiten-titel { margin: 0 0 18px; font-size: 34px; font-weight: 700; letter-spacing: -.025em; line-height: 1.15; }
section { display: none; }
section.active { display: block; animation: rein .25s ease-out; }
/* Schmale Fenster: Titelleiste oben, Tab-Leiste unten wie auf dem iPhone */
@media (max-width: 860px) {
  /* ohne backdrop-filter, sonst hängt die fixierte Tab-Leiste am Kopf statt am Fensterrand */
  header { position: sticky; bottom: auto; right: 0; width: auto; }
  .header-inner { padding: 10px 16px; }
  .brand { display: flex; align-items: center; gap: 8px; padding: 0; }
  .brand .logo { width: 28px; height: 28px; border-radius: 7px; box-shadow: none; }
  .brand h1 { font-size: 17px; font-weight: 600; align-self: auto; }
  .brand .bib { align-self: auto; }
  .brand .version { display: none; }
  nav {
    position: fixed; left: 0; right: 0; bottom: 0; flex-direction: row; gap: 0; overflow-x: auto; scrollbar-width: none;
    padding: 6px 4px calc(6px + env(safe-area-inset-bottom)); background: var(--bar); border-top: .5px solid var(--border);
    -webkit-backdrop-filter: saturate(180%) blur(20px); backdrop-filter: saturate(180%) blur(20px);
  }
  nav button { flex: 1 1 0; min-width: 54px; flex-direction: column; gap: 2px; padding: 4px 2px; font-size: 10px; color: var(--muted); text-align: center; }
  nav button span { overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
  nav button svg { width: 24px; height: 24px; color: inherit; }
  nav button:hover { background: none; }
  nav button.active { background: none; color: var(--accent); box-shadow: none; }
  nav button.active svg { color: var(--accent); }
  main { margin-left: 0; padding: 18px 16px 110px; }
  .seiten-titel { font-size: 30px; margin-bottom: 14px; }
}
/* Gruppierte Karten */
.card {
  background: var(--surface); border: 0; border-radius: var(--radius);
  padding: 18px 20px; margin-bottom: 22px;
}
.card h2 { margin: 0 0 4px; font-size: 20px; font-weight: 600; letter-spacing: -.02em; }
.card h3 { margin: 0 0 10px; font-size: 13px; font-weight: 400; text-transform: uppercase; letter-spacing: .02em; color: var(--muted); }
.hint { margin: 0 0 14px; color: var(--muted); font-size: 13px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 22px; }
.grid-2 > .card { margin-bottom: 0; }
@media (max-width: 1060px) { .grid-2 { grid-template-columns: 1fr; } }
.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.row > .grow { flex: 1; min-width: 180px; }
label { display: block; font-size: 13px; font-weight: 400; color: var(--muted); margin-bottom: 5px; }
input, textarea {
  width: 100%; font: inherit; color: var(--text); background: var(--fill);
  border: 0; border-radius: 10px; padding: 9px 12px; transition: box-shadow .15s;
}
input::placeholder, textarea::placeholder { color: var(--muted); }
textarea { resize: vertical; min-height: 90px; line-height: 1.45; }
input:focus, textarea:focus { outline: none; box-shadow: 0 0 0 3.5px color-mix(in srgb, var(--accent) 40%, transparent); }
input.big { font-size: 20px; padding: 13px 16px; border-radius: 12px; }
input.missing { background: var(--warn-soft); box-shadow: inset 0 0 0 1px var(--warn); }
.form-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px 14px; }
.form-grid .full { grid-column: 1 / -1; }
@media (max-width: 560px) { .form-grid { grid-template-columns: 1fr; } }
/* Knöpfe: gefüllt (Hauptaktion), grau (Nebenaktion), rot (zerstörend) */
button.btn {
  font: inherit; font-weight: 600; border-radius: 10px; padding: 9px 18px; cursor: pointer;
  border: 0; background: var(--accent); color: #fff; white-space: nowrap; transition: filter .15s, transform .1s;
}
button.btn:hover { background: var(--accent-hover); }
button.btn:active { transform: scale(.97); }
button.btn.secondary { background: var(--fill); color: var(--accent); }
button.btn.secondary:hover { filter: brightness(.95); }
button.btn.danger { background: var(--danger-soft); color: var(--danger); }
button.btn.danger:hover { filter: brightness(.95); }
button.btn.small { padding: 5px 12px; font-size: 13px; border-radius: 999px; }
button.btn:disabled { opacity: .4; cursor: default; transform: none; }
button.link { border: 0; background: none; color: var(--accent); font: inherit; cursor: pointer; padding: 0; margin-top: 14px; }
button.link:hover { opacity: .7; }
.actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 18px; }
/* Tabellen als Liste mit eingerückten Trennlinien */
.table-wrap { overflow-x: auto; margin: 0 -20px -18px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 11px 12px; vertical-align: top; }
td { box-shadow: inset 0 .5px 0 var(--border); }
th:first-child, td:first-child { padding-left: 20px; }
th:last-child, td:last-child { padding-right: 20px; }
th { font-size: 12px; text-transform: uppercase; letter-spacing: .02em; color: var(--muted); font-weight: 400; padding-top: 6px; padding-bottom: 6px; }
tbody tr.clickable { cursor: pointer; }
tbody tr.clickable:hover, tbody tr.highlight { background: var(--fill); }
td .sub { color: var(--muted); font-size: 13px; }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
.empty { color: var(--muted); padding: 18px 20px; }
.badge { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap; }
.badge.ok { background: var(--ok-soft); color: var(--ok); }
.badge.warn { background: var(--warn-soft); color: var(--warn); }
.badge.danger { background: var(--danger-soft); color: var(--danger); }
.book-info { margin-top: 10px; padding: 11px 14px; border-radius: 10px; background: var(--fill); font-size: 14px; min-height: 42px; }
.book-info.error { background: var(--danger-soft); color: var(--danger); }
.log { list-style: none; margin: 0; padding: 0; }
.log li { padding: 9px 0; box-shadow: inset 0 .5px 0 var(--border); display: flex; gap: 10px; align-items: baseline; }
.log li:first-child { box-shadow: none; }
.log .time { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; margin-bottom: 22px; }
.tile { background: var(--surface); border-radius: var(--radius); padding: 16px 18px; }
.tile .value { font-size: 30px; font-weight: 700; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
.tile .label { color: var(--muted); font-size: 13px; }
.tile.alert .value { color: var(--danger); }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.chip { border: 0; background: var(--fill); color: var(--text); border-radius: 999px; padding: 6px 13px; font: inherit; font-size: 14px; cursor: pointer; }
.chip:hover { filter: brightness(.95); }
.chip.active { background: var(--accent); color: #fff; }
ol.top { margin: 0; padding-left: 22px; }
ol.top li { padding: 3px 0; }
.hidden { display: none !important; }
/* Dialoge als Sheet */
dialog {
  border: 0; border-radius: 14px; background: var(--surface); color: var(--text);
  padding: 0; width: min(620px, calc(100vw - 32px)); box-shadow: 0 20px 60px rgba(0,0,0,.3);
}
dialog[open] { animation: sheet .28s cubic-bezier(.2, .9, .3, 1); }
dialog::backdrop { background: rgba(0, 0, 0, .35); }
dialog .dlg-body { padding: 24px; }
dialog h2 { margin: 0 0 4px; font-size: 22px; font-weight: 700; letter-spacing: -.02em; }
#toasts { position: fixed; top: 18px; right: 20px; left: 20px; display: flex; flex-direction: column; align-items: center; gap: 8px; z-index: 100; pointer-events: none; }
.toast {
  pointer-events: auto; max-width: 440px; padding: 11px 18px; border-radius: 999px; font-size: 14px; font-weight: 500;
  background: var(--bar); color: var(--text); box-shadow: 0 8px 30px rgba(0,0,0,.18);
  -webkit-backdrop-filter: saturate(180%) blur(20px); backdrop-filter: saturate(180%) blur(20px); animation: toast .3s ease-out;
}
.toast.error { background: var(--danger); color: #fff; }
/* Katalog: Serien */
.toolbar { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin: 12px 0 14px; }
.toolbar .hint { margin: 0; }
.check { display: inline-flex; gap: 8px; align-items: center; margin: 0; font-size: 14px; color: var(--text); font-weight: 400; cursor: pointer; }
/* Kontrollkästchen als iOS-Schalter */
.check input {
  appearance: none; -webkit-appearance: none; width: 38px; height: 23px; margin: 0; padding: 0; flex: none;
  border-radius: 999px; background: var(--fill); position: relative; cursor: pointer; transition: background .2s;
}
.check input::after {
  content: ""; position: absolute; top: 2px; left: 2px; width: 19px; height: 19px; border-radius: 50%;
  background: #fff; box-shadow: 0 2px 4px rgba(0,0,0,.2); transition: transform .2s;
}
.check input:checked { background: #34c759; }
.check input:checked::after { transform: translateX(15px); }
tr.serie-zeile td { background: transparent; }
.ort { white-space: nowrap; }
.ort.geplant { color: var(--muted); }
/* Auf dem Handy ist die Standort-Spalte weit rechts – dort steht er zusätzlich unter dem Titel */
.ort-mobil { display: none; margin-top: 2px; font-size: 13px; }
@media (max-width: 860px) { .ort-mobil { display: block; } }
tr.serie-zeile .pfeil { display: inline-block; width: 1em; color: var(--muted); transition: transform .15s; }
tr.serie-zeile.offen .pfeil { transform: rotate(90deg); }
tr.band-zeile td:nth-child(2) { padding-left: 34px; }
.band-nr { display: inline-block; min-width: 2.4em; margin-right: 6px; padding: 1px 7px; border-radius: 6px; background: var(--accent-soft); color: var(--accent); font-size: 12px; font-weight: 600; text-align: center; }
.serie-tag { color: var(--accent); font-size: 13px; }
.nachladen { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; padding: 12px 16px; border-radius: 12px; background: var(--accent-soft); margin-bottom: 14px; font-size: 14px; }
.nachladen .grow { flex: 1; min-width: 200px; }
.balken { height: 6px; border-radius: 3px; background: var(--fill); overflow: hidden; margin-top: 6px; }
.balken > div { height: 100%; background: var(--accent); width: 0; transition: width .3s; border-radius: 3px; }

/* Buchvorschau */
dialog.vorschau { width: min(760px, calc(100vw - 32px)); }
.vorschau-kopf { display: grid; grid-template-columns: 150px 1fr; gap: 22px; }
@media (max-width: 560px) { .vorschau-kopf { grid-template-columns: 1fr; } .cover-box { max-width: 160px; } }
.cover-box { aspect-ratio: 2 / 3; border-radius: 8px; overflow: hidden; background: var(--fill); display: flex; align-items: center; justify-content: center; box-shadow: 0 4px 14px rgba(0,0,0,.15); }
.cover-box img { width: 100%; height: 100%; object-fit: cover; display: block; }
.cover-leer { color: var(--muted); font-size: 44px; }
.vorschau-titel { margin: 0 0 4px; font-size: 24px; font-weight: 700; letter-spacing: -.02em; line-height: 1.2; }
.vorschau-autor { color: var(--muted); margin-bottom: 10px; }
.daten { display: grid; grid-template-columns: auto 1fr; gap: 3px 14px; font-size: 14px; margin: 12px 0 0; }
.daten dt { color: var(--muted); }
.daten dd { margin: 0; }
.beschreibung { margin-top: 18px; font-size: 14px; line-height: 1.6; white-space: pre-line; max-height: 260px; overflow-y: auto; padding-right: 6px; }
.beschreibung.leer { color: var(--muted); font-style: italic; }
.vorschau-block { margin-top: 18px; }
.vorschau-block h3 { font-size: 13px; font-weight: 400; text-transform: uppercase; letter-spacing: .02em; color: var(--muted); margin: 0 0 8px; }
.baende { list-style: none; margin: 0; padding: 0; }
.baende li { display: flex; gap: 8px; align-items: center; padding: 6px 10px; border-radius: 8px; cursor: pointer; font-size: 14px; }
.baende li:hover { background: var(--fill); }
.baende li.aktuell { background: var(--accent-soft); cursor: default; font-weight: 600; }
.baende li .badge { margin-left: auto; }
.nb-vorschau { display: flex; gap: 14px; align-items: flex-start; margin-bottom: 14px; }
.nb-vorschau .cover-box { width: 70px; flex: none; }
.nb-vorschau .sub { color: var(--muted); font-size: 13px; display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }
/* Regale */
.platz-box { margin-top: 12px; padding: 12px 16px; border-radius: 12px; background: var(--accent-soft); font-size: 15px; }
.platz-box b { font-size: 18px; color: var(--accent); }
.platz-box.warn { background: var(--warn-soft); color: var(--warn); }
.regal { border-radius: 12px; margin-top: 12px; background: var(--surface-2); overflow: hidden; }
.regal-kopf { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 12px 16px; }
.regal-kopf .grow { flex: 1; min-width: 200px; }
.regal-kopf .sub { color: var(--muted); font-size: 13px; }
.regal summary { cursor: pointer; padding: 0 16px 12px; color: var(--accent); font-size: 14px; }
.fach { display: grid; grid-template-columns: 90px 1fr; gap: 4px 12px; padding: 10px 16px; box-shadow: inset 0 .5px 0 var(--border); background: var(--surface); }
.fach-nr { font-weight: 600; font-size: 14px; }
.fach-nr .sub { display: block; font-weight: 400; }
.fach .balken { margin: 0 0 8px; }
.fach .balken.voll > div { background: var(--warn); }
.fach-buecher { display: flex; flex-wrap: wrap; gap: 5px; }
.fach-nr .fach-genre { display: block; font-weight: 500; font-size: 12px; color: var(--accent); overflow-wrap: anywhere; }
#rg-fach-details { margin-top: 12px; }
#rg-fach-details summary { cursor: pointer; color: var(--accent); font-size: 14px; }
.fach-genres { display: grid; grid-template-columns: auto 1fr; gap: 6px 10px; align-items: center; }
.fach-genres span { font-size: 14px; font-weight: 600; white-space: nowrap; }
.buch-chip { font-size: 13px; padding: 3px 10px; border-radius: 999px; border: 0; background: var(--fill); cursor: pointer; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.buch-chip:hover { filter: brightness(.95); }
.buch-chip.neu { background: var(--ok-soft); color: var(--ok); }
.buch-chip.umstellen { background: var(--warn-soft); color: var(--warn); }
.legende { display: flex; gap: 14px; flex-wrap: wrap; font-size: 13px; color: var(--muted); margin-top: 6px; }
.legende .buch-chip { cursor: default; }
.aufgaben li { display: flex; gap: 8px; flex-wrap: wrap; align-items: baseline; }
.aufgaben .pfad { color: var(--muted); font-size: 13px; }
#update-hinweis { margin-top: 8px; justify-self: start; }
@media (max-width: 860px) { #update-hinweis { margin: 0 0 0 auto; } }
/* Schüler & Mediencodes */
.code { font: 13px ui-monospace, "SF Mono", Menlo, Consolas, monospace; letter-spacing: .03em; padding: 1px 7px; border-radius: 6px; background: var(--fill); white-space: nowrap; }
.codes li { align-items: center; flex-wrap: wrap; }
.codes li .grow { flex: 1; min-width: 140px; }
.code-zeile { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.code-zeile input { flex: 1; min-width: 180px; }
.person-box { display: flex; gap: 12px; align-items: center; }
.person-box .avatar { width: 40px; height: 40px; border-radius: 50%; background: var(--accent); color: #fff; display: flex; align-items: center; justify-content: center; font-weight: 600; flex: none; }
.person-box .sub { color: var(--muted); font-size: 13px; }
#co-log { margin-top: 10px; }
#co-log:empty { display: none; }
/* Segment-Umschalter wie in iOS */
.segment { display: inline-flex; gap: 2px; padding: 2px; border-radius: 9px; background: var(--fill); margin-bottom: 12px; max-width: 100%; overflow-x: auto; scrollbar-width: none; }
.segment button { border: 0; background: none; color: var(--text); font: inherit; font-size: 13px; font-weight: 500; padding: 5px 14px; border-radius: 7px; cursor: pointer; white-space: nowrap; transition: background .15s; }
.segment button.active { background: var(--surface); box-shadow: 0 1px 3px rgba(0,0,0,.12); }
@media (prefers-color-scheme: dark) { .segment button.active { background: #636366; } }
.segment button span { color: var(--muted); font-variant-numeric: tabular-nums; margin-left: 2px; }
.segment.gross { margin-bottom: 18px; }
.segment.gross button { font-size: 14px; padding: 6px 20px; }
/* Kennzahlen oben auf der Ausleihe-Seite */
.tiles.klein { grid-template-columns: repeat(4, 1fr); margin-bottom: 22px; }
.tiles.klein .tile { padding: 12px 16px; cursor: pointer; transition: transform .1s; }
.tiles.klein .tile:active { transform: scale(.98); }
.tiles.klein .value { font-size: 24px; }
.tile.warn .value { color: var(--warn); }
@media (max-width: 860px) { .tiles.klein { grid-template-columns: repeat(2, 1fr); } }
/* Lange Listen stückweise zeichnen */
.mehr { padding: 14px 20px 18px; text-align: center; color: var(--muted); font-size: 13px; box-shadow: inset 0 .5px 0 var(--border); }
.mehr button { margin-left: 8px; }
.chips.zeile { flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none; padding-bottom: 2px; }
.chips.zeile .chip { flex: none; }
#katalog-filter .chip, #regal-filter .chip, #klassen-chips .chip { padding: 4px 12px; font-size: 13px; }
td.titel-zelle { min-width: 220px; }
/* Katalog als Cover-Wand: echtes Cover, sonst ein gemaltes in der Farbe des Genres */
.cover-wand { display: grid; grid-template-columns: repeat(auto-fill, minmax(136px, 1fr)); gap: 22px 18px; padding: 6px 20px 20px; }
.cover-wand > .empty { grid-column: 1 / -1; padding: 0; }
.kachel { border: 0; background: none; padding: 0; font: inherit; color: var(--text); text-align: left; cursor: pointer; min-width: 0; }
.kachel:focus-visible { outline: none; }
.kachel:focus-visible .cover { box-shadow: 0 0 0 3.5px color-mix(in srgb, var(--accent) 50%, transparent); }
.cover {
  aspect-ratio: 2 / 3; position: relative; overflow: hidden; border-radius: 4px 10px 10px 4px; color: #fff;
  background: linear-gradient(180deg, transparent 55%, rgba(0, 0, 0, .25)), linear-gradient(150deg, var(--c2), var(--c1));
  box-shadow: 0 1px 2px rgba(0, 0, 0, .15), 0 10px 22px rgba(20, 40, 80, .18); transition: transform .15s;
}
.kachel:hover .cover { transform: translateY(-4px) rotate(-.6deg); }
.cover::before { content: ""; position: absolute; z-index: 2; left: 0; top: 0; bottom: 0; width: 7px; background: rgba(0, 0, 0, .18); box-shadow: 1px 0 0 rgba(255, 255, 255, .25); }
.cover img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; background: var(--surface); }
.cover-text { position: absolute; inset: 0; padding: 14px 12px 12px 18px; display: flex; flex-direction: column; justify-content: space-between; }
.cover-titel { font: 700 15px/1.2 Georgia, "Times New Roman", serif; letter-spacing: 0; text-shadow: 0 1px 2px rgba(0, 0, 0, .2); display: -webkit-box; -webkit-line-clamp: 6; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
.cover-autor { font-size: 11px; opacity: .9; text-transform: uppercase; letter-spacing: .06em; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.cover.mit-band .cover-text { padding-top: 38px; }
.cover-band { position: absolute; z-index: 3; top: 11px; left: 17px; background: rgba(255, 255, 255, .92); color: #16263f; font-size: 11px; font-weight: 700; border-radius: 999px; padding: 1px 7px; }
/* Serie: als Stapel, aufgeklappt mit Rahmen */
.stapel { position: relative; isolation: isolate; margin-bottom: 8px; }
.stapel::before, .stapel::after { content: ""; position: absolute; z-index: -1; inset: 0; border-radius: 4px 10px 10px 4px; }
.stapel::before { transform: translate(8px, 8px); background: color-mix(in srgb, var(--c2) 25%, var(--surface)); }
.stapel::after { transform: translate(4px, 4px); background: color-mix(in srgb, var(--c2) 50%, var(--surface)); }
.kachel.band .cover { outline: 2px solid var(--accent-soft); outline-offset: 3px; }
.kachel-titel { margin-top: 9px; font-size: 13px; font-weight: 600; line-height: 1.3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.kachel-sub { color: var(--muted); font-size: 12px; line-height: 1.35; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.punkt { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; background: var(--ok); vertical-align: 0; }
.punkt.weg { background: var(--warn); }
@media (max-width: 560px) {
  .cover-wand { grid-template-columns: repeat(auto-fill, minmax(100px, 1fr)); gap: 16px 12px; padding: 4px 16px 16px; }
  .cover-text { padding: 10px 8px 8px 13px; }
  .cover.mit-band .cover-text { padding-top: 32px; }
  .cover-band { top: 8px; left: 12px; }
  .cover-titel { font-size: 12.5px; }
  .cover-autor { font-size: 9.5px; }
}
@keyframes rein { from { opacity: 0; transform: translateY(6px); } }
@keyframes toast { from { opacity: 0; transform: translateY(-12px) scale(.96); } }
@keyframes sheet { from { opacity: 0; transform: translateY(24px) scale(.98); } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <div class="brand">
      <svg class="logo" viewBox="0 0 512 512" aria-hidden="true">
        <defs><linearGradient id="logo-bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2b5c8a"/><stop offset="1" stop-color="#173452"/></linearGradient>
        <clipPath id="logo-seiten"><path d="M92 170Q172 140 250 176V386Q172 352 92 378Z"/><path d="M420 170Q340 140 262 176V386Q340 352 420 378Z"/></clipPath></defs>
        <rect width="512" height="512" rx="112" fill="url(#logo-bg)"/>
        <path d="M92 170Q172 140 250 176V386Q172 352 92 378Z" fill="#fff"/><path d="M420 170Q340 140 262 176V386Q340 352 420 378Z" fill="#fff"/>
        <g clip-path="url(#logo-seiten)" fill="#173452"><rect x="118" y="196" width="8" height="150"/><rect x="132" y="196" width="4" height="150"/><rect x="142" y="196" width="12" height="150"/><rect x="160" y="196" width="4" height="150"/><rect x="170" y="196" width="6" height="150"/><rect x="182" y="196" width="10" height="150"/><rect x="198" y="196" width="4" height="150"/><rect x="208" y="196" width="8" height="150"/><rect x="222" y="196" width="4" height="150"/><rect x="286" y="196" width="4" height="150"/><rect x="296" y="196" width="10" height="150"/><rect x="312" y="196" width="4" height="150"/><rect x="322" y="196" width="8" height="150"/><rect x="336" y="196" width="12" height="150"/><rect x="354" y="196" width="4" height="150"/><rect x="364" y="196" width="6" height="150"/><rect x="376" y="196" width="10" height="150"/></g>
        <rect x="68" y="258" width="376" height="12" rx="6" fill="#ff5a4e" opacity=".35"/><rect x="68" y="260" width="376" height="8" rx="4" fill="#ff5a4e"/>
      </svg>
      <h1>ScanBook</h1><span class="bib" id="bib-name"></span><span class="version" id="version"></span><button class="btn small hidden" type="button" id="update-hinweis"></button>
    </div>
    <nav id="nav">
      <button data-tab="ausleihe"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 4 3 8l4 4M3 8h13M17 20l4-4-4-4M21 16H8"/></svg><span>Ausleihe</span></button>
      <button data-tab="katalog"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 19.5V5a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v14.5M9 19.5V7a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v12.5"/><path d="m14.5 7.8 2.9-.8a1 1 0 0 1 1.2.7l2.9 10.9a1 1 0 0 1-.7 1.2l-2.9.8a1 1 0 0 1-1.2-.7L14 8.9"/><path d="M3 20h12"/></svg><span>Katalog</span></button>
      <button data-tab="schueler"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="5" width="19" height="14" rx="2.5"/><circle cx="8.5" cy="11" r="2.2"/><path d="M5 16.2c.5-1.7 1.8-2.6 3.5-2.6s3 .9 3.5 2.6M14.5 10h4M14.5 13.5h3"/></svg><span>Schüler</span></button>
      <button data-tab="scan"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3"/><path d="M8 8v8M11 8v8M14 8v8M17 8v8"/></svg><span>Erfassen</span></button>
      <button data-tab="regale"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M7 5.5v2M10 5.5v2M7 11.5v2M14 17.5v2M17 17.5v2"/></svg><span>Regale</span></button>
      <button data-tab="verwaltung"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/></svg><span>Verwaltung</span></button>
    </nav>
  </div>
</header>

<main>
  <h1 class="seiten-titel" id="seiten-titel"></h1>
  <!-- SCANNEN -->
  <section id="tab-scan">
    <div class="card">
      <h2>Bücher scannen</h2>
      <p class="hint">Neue Bücher werden online nachgeschlagen, bekannte Bücher werden automatisch um ein Exemplar aufgestockt.</p>
      <form id="scan-form" class="row">
        <input id="scan-isbn" class="big grow" placeholder="ISBN oder eigenes Etikett scannen …" autocomplete="off">
        <button class="btn" type="submit">Hinzufügen</button>
      </form>
      <div class="platz-box hidden" id="code-box"></div>
      <div class="platz-box hidden" id="platz-box"></div>
      <button class="link" id="manual-btn" type="button">Buch ohne Scan von Hand eintragen</button>
      <p class="hint" style="margin:10px 0 0">Eigenes Etikett im Buch? Erst das Etikett scannen, dann die ISBN. Codes erzeugen und Etiketten drucken findest du unter <a href="#verwaltung">Verwaltung</a>.</p>
    </div>

    <div class="card hidden" id="new-book-card">
      <h2 id="new-book-title">Neues Buch</h2>
      <p class="hint" id="new-book-hint"></p>
      <form id="new-book-form">
        <div class="nb-vorschau hidden" id="nb-vorschau">
          <div class="cover-box"><img alt="" referrerpolicy="no-referrer"><span class="cover-leer hidden">📖</span></div>
          <div class="sub" id="nb-beschreibung"></div>
        </div>
        <input type="hidden" name="beschreibung"><input type="hidden" name="cover_url"><input type="hidden" name="nachgeladen"><input type="hidden" name="code">
        <div class="form-grid">
          <div><label for="nb-isbn">ISBN</label><input id="nb-isbn" name="isbn" autocomplete="off"></div>
          <div><label for="nb-anzahl">Anzahl Exemplare</label><input id="nb-anzahl" name="anzahl" type="number" min="1" value="1"></div>
          <div class="full"><label for="nb-titel">Titel</label><input id="nb-titel" name="titel"></div>
          <div class="full"><label for="nb-autoren">Autor(en), mit Komma getrennt</label><input id="nb-autoren" name="autoren"></div>
          <div><label for="nb-verlag">Verlag</label><input id="nb-verlag" name="verlag"></div>
          <div><label for="nb-erscheinungsjahr">Erscheinungsjahr</label><input id="nb-erscheinungsjahr" name="erscheinungsjahr"></div>
          <div class="full"><label for="nb-genre">Genre</label><input id="nb-genre" name="genre"></div>
          <div><label for="nb-serie">Serie (leer = keine)</label><input id="nb-serie" name="serie" list="serien-liste" autocomplete="off"></div>
          <div><label for="nb-band">Band</label><input id="nb-band" name="band" inputmode="numeric" autocomplete="off"></div>
        </div>
        <div class="actions">
          <button class="btn" type="submit" id="nb-save">Speichern</button>
          <button class="btn secondary" type="button" id="nb-cancel">Verwerfen</button>
        </div>
      </form>
    </div>

    <div class="card">
      <h3>Zuletzt erfasst</h3>
      <ul class="log" id="scan-log"><li class="sub" style="color:var(--muted)">Noch nichts gescannt.</li></ul>
    </div>

  </section>

  <!-- KATALOG -->
  <section id="tab-katalog">
    <div class="card">
      <div class="row">
        <input id="katalog-suche" class="grow" placeholder="Suchen nach Titel, Autor, Serie, Genre, Verlag, Regal oder ISBN …" autocomplete="off">
        <span class="sub" id="katalog-anzahl" style="color:var(--muted)"></span>
      </div>
      <div class="toolbar">
        <div class="chips" id="katalog-filter" style="margin:0"></div>
        <div class="chips hidden" id="regal-filter" style="margin:0"></div>
        <label class="check" style="margin-left:auto"><input type="checkbox" id="serien-gruppieren" checked> Serien zusammenfassen</label>
        <div class="segment" id="katalog-ansicht" style="margin:0"><button type="button" data-ansicht="cover">Cover</button><button type="button" data-ansicht="liste">Liste</button></div>
      </div>
      <div class="nachladen hidden" id="nachladen-box">
        <div class="grow"><span id="nachladen-text"></span><div class="balken hidden" id="nachladen-balken"><div></div></div></div>
        <button class="btn small" type="button" id="nachladen-btn">Alle Bücher nachladen</button>
      </div>
      <div class="table-wrap">
        <table id="katalog-tabelle">
          <thead><tr><th>Titel</th><th>Autor</th><th>Jahr</th><th>Genre</th><th>Standort</th><th>Verfügbar</th></tr></thead>
          <tbody id="katalog-body"></tbody>
        </table>
        <div class="cover-wand" id="katalog-wand"></div>
        <div class="mehr hidden" id="katalog-mehr"></div>
      </div>
    </div>
  </section>

  <!-- REGALE -->
  <section id="tab-regale">
    <div class="card hidden" id="regal-aufgaben">
      <h2>📦 Zu tun</h2>
      <p class="hint" id="regal-aufgaben-text"></p>
      <ul class="log aufgaben" id="regal-umstellen"></ul>
      <div class="actions"><button class="btn" type="button" id="regal-uebernehmen">Erledigt – so eingeräumt</button></div>
    </div>
    <div class="card">
      <div class="row">
        <h2 class="grow" style="margin:0">Regale</h2>
        <button class="btn" type="button" id="regal-neu">Regal hinzufügen</button>
      </div>
      <p class="hint" style="margin-top:6px">Lege deine Regale mit Fächern an und trage ein, welche Genres hineinkommen. Ein Regal ohne Genres nimmt alle übrigen Bücher auf. ScanBook sortiert innerhalb eines Regals nach Genre, dann nach Autor; Bände einer Serie stehen zusammen. Beim Scannen siehst du sofort, wo ein neues Buch hingehört.</p>
      <div class="legende"><span><span class="buch-chip neu">Titel</span> neu einräumen</span><span><span class="buch-chip umstellen">Titel</span> umstellen</span></div>
      <div id="regale-liste"></div>
    </div>
    <div class="card hidden" id="ohne-platz">
      <h2>⚠️ Ohne Platz</h2>
      <p class="hint">Diese Bücher passen in kein Regal. Lege mehr Fächer an, erhöhe „Bücher pro Fach“ oder lege ein Regal ohne Genres für alle übrigen Bücher an.</p>
      <ul class="log aufgaben" id="ohne-platz-liste"></ul>
    </div>
  </section>

  <!-- AUSLEIHE -->
  <section id="tab-ausleihe">
    <div class="tiles klein" id="ausleihe-tiles"></div>
    <div class="grid-2" style="margin-bottom:22px">
      <div class="card">
        <h2>Buch ausleihen</h2>
        <p class="hint">Schülerausweis scannen, dann die Bücher scannen. Jedes Buch wird sofort ausgeliehen.</p>
        <form id="person-form">
          <label for="co-name">Schülerausweis oder Name</label>
          <input id="co-name" list="namen-liste" autocomplete="off" placeholder="Ausweis scannen oder Namen eingeben …">
        </form>
        <div class="book-info" id="co-person">Noch niemand ausgewählt.</div>
        <form id="checkout-form">
          <div class="row" style="margin-top:12px">
            <div class="grow"><label for="co-isbn">Buch (Mediencode oder ISBN)</label><input id="co-isbn" autocomplete="off" placeholder="Buch scannen …" disabled></div>
            <div style="width:110px"><label for="co-tage">Tage</label><input id="co-tage" type="number" min="1"></div>
          </div>
          <ul class="log" id="co-log"></ul>
          <div class="actions"><button class="btn secondary" type="button" id="co-fertig" disabled>Fertig – nächste Person</button></div>
        </form>
        <datalist id="namen-liste"></datalist>
      </div>
      <div class="card">
        <h2>Zurückgeben oder verlängern</h2>
        <p class="hint">Mediencode scannen – das Exemplar wird sofort zurückgebucht. Bei einer ISBN fragt ScanBook nach, ist das Buch mehrfach ausgeliehen, wird es unten hervorgehoben.</p>
        <form id="return-form">
          <label for="re-isbn">Buch (Mediencode oder ISBN)</label>
          <input id="re-isbn" autocomplete="off" placeholder="Buch scannen …">
        </form>
        <div class="book-info" id="re-info">Oder unten in der Liste auswählen.</div>
      </div>
    </div>

    <div class="card">
      <div class="row" style="margin-bottom:14px">
        <h2 class="grow" style="margin:0">Aktuelle Ausleihen</h2>
        <input id="loans-filter" style="max-width:260px" placeholder="Filtern nach Name, Klasse oder Titel …" autocomplete="off">
      </div>
      <div class="segment" id="loans-status">
        <button type="button" data-status="alle" class="active">Alle <span id="anz-alle"></span></button>
        <button type="button" data-status="ueber">Überfällig <span id="anz-ueber"></span></button>
        <button type="button" data-status="bald">Bald fällig <span id="anz-bald"></span></button>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Buch</th><th>Ausleiher</th><th>Geliehen am</th><th>Rückgabe bis</th><th>Status</th><th></th></tr></thead>
          <tbody id="loans-body"></tbody>
        </table>
        <div class="mehr hidden" id="loans-mehr"></div>
      </div>
    </div>
  </section>

  <!-- SCHÜLER -->
  <section id="tab-schueler">
    <div class="segment gross" id="schueler-ansicht">
      <button type="button" data-ansicht="liste" class="active">Schülerliste</button>
      <button type="button" data-ansicht="verlauf">Ausleih-Verlauf</button>
    </div>
    <div id="ansicht-liste">
    <div class="card">
      <div class="row">
        <input id="schueler-suche" class="grow" placeholder="Suchen nach Name oder Ausweisnummer …" autocomplete="off">
        <button class="btn" type="button" id="schueler-neu">Schüler hinzufügen</button>
        <button class="btn secondary" type="button" id="schueler-import-btn">Klassenliste importieren</button>
      </div>
      <div class="chips zeile" id="klassen-chips"></div>
      <div class="toolbar">
        <p class="hint" id="schueler-anzahl"></p>
        <button class="btn small secondary" type="button" id="ausweise-drucken" style="margin-left:auto">Ausweise drucken</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Name</th><th>Klasse</th><th>Ausweis</th><th>Ausgeliehen</th><th></th></tr></thead>
          <tbody id="schueler-body"></tbody>
        </table>
        <div class="mehr hidden" id="schueler-mehr"></div>
      </div>
    </div>
    </div>
    <div id="ansicht-verlauf" class="hidden">
    <div class="card">
      <p class="hint">Was hat jemand gerade oder früher ausgeliehen? Auch Ausleihen ohne Schülerausweis (z. B. Lehrkräfte).</p>
      <input id="personen-suche" placeholder="Name suchen …" autocomplete="off">
      <div class="chips" id="personen-liste"></div>
    </div>
    <div id="person-detail"></div>
    </div>
  </section>

  <!-- VERWALTUNG -->
  <section id="tab-verwaltung">
    <div class="tiles" id="stat-tiles"></div>
    <div class="grid-2" style="margin-bottom:22px">
      <div class="card">
        <h3>🏆 Meistgeliehene Bücher</h3>
        <div id="stat-top"></div>
      </div>
      <div class="card">
        <h3>📄 Export (öffnet sich in Excel)</h3>
        <div class="actions" style="margin-top:0">
          <a href="/export/katalog.csv"><button class="btn secondary" type="button">Katalog</button></a>
          <a href="/export/ausleihen.csv"><button class="btn secondary" type="button">Aktuelle Ausleihen</button></a>
          <a href="/export/mahnliste.csv"><button class="btn secondary" type="button">Mahnliste</button></a>
        </div>
        <h3 style="margin-top:22px">💾 Datensicherung</h3>
        <p class="hint" id="backup-info">Einmal täglich wird automatisch gesichert. Hier kannst du zusätzlich ein Backup erstellen.</p>
        <p class="hint hidden" id="backup-warnung" style="color:var(--danger)"></p>
        <label for="backup-ordner">Zusätzlich sichern nach (z. B. USB-Stick, OneDrive oder Netzlaufwerk)</label>
        <div class="row">
          <input id="backup-ordner" class="grow" placeholder="z. B. E:\ScanBook-Backups oder C:\Users\…\OneDrive\ScanBook" autocomplete="off">
          <button class="btn secondary" type="button" id="backup-ordner-btn">Speichern</button>
        </div>
        <div class="actions"><button class="btn secondary" type="button" id="backup-btn">Backup jetzt erstellen</button></div>
      </div>
    </div>
    <div class="card">
      <h2>Mediencodes &amp; Etiketten</h2>
      <p class="hint">Jedes Exemplar kann eine eigene Nummer bekommen, die als Etikett im Buch klebt. Beim Ausleihen und Zurückgeben lässt sich dann der Mediencode statt der ISBN scannen, und du siehst genau, welches Exemplar wer hat.</p>
      <p class="hint"><b>Eigene Etiketten verwenden:</b> den Code des Etiketts unter „Erfassen“ ins Scan-Feld scannen, danach die ISBN des Buchs. ScanBook merkt sich den Code für dieses Exemplar. Einzelne Codes lassen sich auch in der Buchvorschau eintragen.</p>
      <p class="hint" id="codes-status"></p>
      <div class="actions" style="margin-top:0">
        <button class="btn secondary" type="button" id="codes-erzeugen">Codes für alle Exemplare ohne Code erzeugen</button>
        <button class="btn secondary" type="button" id="etiketten-drucken">Neue Etiketten drucken</button>
      </div>
    </div>
    <div class="card">
      <h2>Stammdaten &amp; Einstellungen</h2>
      <p class="hint">Abgeschlossene Ausleihen werden nach der Löschfrist beim Programmstart automatisch gelöscht (Datenschutz).</p>
      <form id="settings-form">
        <div class="form-grid" id="settings-fields"></div>
        <div class="actions"><button class="btn" type="submit">Speichern</button></div>
      </form>
    </div>
    <div class="card" id="update-karte">
      <h2>Programm-Update</h2>
      <p class="hint" id="update-text">Prüft auf GitHub, ob es eine neuere Version von ScanBook gibt. Deine Daten bleiben beim Update erhalten.</p>
      <div class="actions" style="margin-top:0">
        <button class="btn secondary" type="button" id="update-check">Nach Updates suchen</button>
        <button class="btn hidden" type="button" id="update-install">Jetzt installieren</button>
      </div>
    </div>
  </section>
</main>

<!-- BUCH-BEARBEITEN-DIALOG -->
<dialog id="edit-dialog">
  <form id="edit-form" class="dlg-body">
    <h2>Buch bearbeiten</h2>
    <p class="hint" id="edit-hint"></p>
    <div class="form-grid">
      <div class="full"><label for="ed-titel">Titel</label><input id="ed-titel" name="titel"></div>
      <div class="full"><label for="ed-autoren">Autor(en), mit Komma getrennt</label><input id="ed-autoren" name="autoren"></div>
      <div><label for="ed-verlag">Verlag</label><input id="ed-verlag" name="verlag"></div>
      <div><label for="ed-erscheinungsjahr">Erscheinungsjahr</label><input id="ed-erscheinungsjahr" name="erscheinungsjahr"></div>
      <div class="full"><label for="ed-genre">Genre</label><input id="ed-genre" name="genre"></div>
      <div><label for="ed-serie">Serie (leer = keine)</label><input id="ed-serie" name="serie" list="serien-liste" autocomplete="off"></div>
      <div><label for="ed-band">Band</label><input id="ed-band" name="band" inputmode="numeric" autocomplete="off"></div>
      <div class="full"><label for="ed-beschreibung">Beschreibung</label><textarea id="ed-beschreibung" name="beschreibung" rows="5"></textarea></div>
    </div>
    <div class="actions">
      <button class="btn" type="submit">Speichern</button>
      <button class="btn secondary" type="button" id="ed-remove">Ein Exemplar entfernen</button>
      <button class="btn danger" type="button" id="ed-delete">Buch löschen</button>
      <button class="btn secondary" type="button" id="ed-close" style="margin-left:auto">Schließen</button>
    </div>
  </form>
</dialog>

<!-- BUCHVORSCHAU -->
<dialog id="preview-dialog" class="vorschau">
  <div class="dlg-body">
    <div class="vorschau-kopf">
      <div class="cover-box" id="pv-cover"><img alt="" referrerpolicy="no-referrer"><span class="cover-leer hidden">📖</span></div>
      <div>
        <h2 class="vorschau-titel" id="pv-titel"></h2>
        <div class="vorschau-autor" id="pv-autor"></div>
        <div id="pv-bestand"></div>
        <dl class="daten" id="pv-daten"></dl>
      </div>
    </div>
    <div class="beschreibung" id="pv-beschreibung"></div>
    <div class="vorschau-block hidden" id="pv-baende-block"><h3 id="pv-baende-titel"></h3><ul class="baende" id="pv-baende"></ul></div>
    <div class="vorschau-block hidden" id="pv-ausleihen-block"><h3>Gerade ausgeliehen</h3><ul class="log" id="pv-ausleihen"></ul></div>
    <div class="vorschau-block" id="pv-codes-block">
      <h3 id="pv-codes-titel">Mediencodes</h3>
      <ul class="log codes" id="pv-codes"></ul>
      <form class="code-zeile" id="pv-code-form">
        <input id="pv-code-neu" placeholder="Eigenen Code scannen oder eingeben …" autocomplete="off">
        <button class="btn small" type="submit">Zuordnen</button>
        <button class="btn small secondary" type="button" id="pv-codes-erzeugen">Fehlende Codes erzeugen</button>
        <button class="btn small secondary" type="button" id="pv-etiketten">Etiketten drucken</button>
      </form>
    </div>
    <div class="actions">
      <button class="btn" type="button" id="pv-edit">Bearbeiten</button>
      <button class="btn secondary" type="button" id="pv-refresh">Daten online nachladen</button>
      <button class="btn secondary" type="button" id="pv-close" style="margin-left:auto">Schließen</button>
    </div>
  </div>
</dialog>

<!-- REGAL-DIALOG -->
<dialog id="regal-dialog">
  <form id="regal-form" class="dlg-body">
    <h2 id="regal-dlg-titel">Regal</h2>
    <p class="hint">„Bücher pro Fach“ ist ein ungefährer Wert – lieber etwas weniger angeben, dann bleibt Platz für neue Bücher.</p>
    <input type="hidden" name="regal_id">
    <div class="form-grid">
      <div class="full"><label for="rg-name">Name</label><input id="rg-name" name="name" placeholder="z. B. Regal 1 oder Wohnzimmer links" autocomplete="off"></div>
      <div><label for="rg-faecher">Anzahl Fächer</label><input id="rg-faecher" name="faecher" type="number" min="1"></div>
      <div><label for="rg-kapazitaet">Bücher pro Fach</label><input id="rg-kapazitaet" name="kapazitaet" type="number" min="1"></div>
      <div class="full"><label for="rg-genres">Genres, mit Komma getrennt (leer = alle übrigen Bücher)</label><input id="rg-genres" name="genres" autocomplete="off"></div>
    </div>
    <details id="rg-fach-details">
      <summary>Eigene Genres für einzelne Fächer</summary>
      <p class="hint">Leer = das Fach nimmt, was zum Regal passt. Ein Fach mit eigenen Genres bekommt genau diese Bücher – ist es voll, kommen sie in die übrigen Fächer.</p>
      <div class="fach-genres" id="rg-fach-genres"></div>
    </details>
    <p class="hint" style="margin:10px 0 0">Genre antippen, um es ins ausgewählte Feld zu übernehmen:</p>
    <div class="chips" id="rg-genre-chips"></div>
    <div class="actions">
      <button class="btn" type="submit">Speichern</button>
      <button class="btn secondary" type="button" id="rg-close" style="margin-left:auto">Abbrechen</button>
    </div>
  </form>
</dialog>

<!-- SCHÜLER-DIALOG -->
<dialog id="schueler-dialog">
  <form id="schueler-form" class="dlg-body">
    <h2 id="schueler-dlg-titel">Schüler</h2>
    <p class="hint">Die Ausweisnummer steht als Barcode auf dem Ausweis. Leer lassen, dann vergibt ScanBook die nächste freie Nummer. Vorhandene Ausweise (z. B. vom Schülerausweis der Schule) einfach einscannen.</p>
    <input type="hidden" name="id">
    <div class="form-grid">
      <div><label for="sc-vorname">Vorname</label><input id="sc-vorname" name="vorname" autocomplete="off"></div>
      <div><label for="sc-nachname">Nachname</label><input id="sc-nachname" name="nachname" autocomplete="off"></div>
      <div><label for="sc-klasse">Klasse</label><input id="sc-klasse" name="klasse" list="klassen-liste" autocomplete="off" placeholder="z. B. 5a"></div>
      <div><label for="sc-ausweis">Ausweisnummer</label><input id="sc-ausweis" name="ausweis" autocomplete="off" placeholder="leer = automatisch"></div>
    </div>
    <div class="vorschau-block hidden" id="sc-ausleihen-block"><h3>Gerade ausgeliehen</h3><ul class="log" id="sc-ausleihen"></ul></div>
    <div class="actions">
      <button class="btn" type="submit">Speichern</button>
      <button class="btn secondary" type="button" id="sc-drucken">Ausweis drucken</button>
      <button class="btn danger" type="button" id="sc-loeschen">Löschen</button>
      <button class="btn secondary" type="button" id="sc-close" style="margin-left:auto">Abbrechen</button>
    </div>
  </form>
</dialog>

<!-- KLASSENLISTE-IMPORT -->
<dialog id="import-dialog">
  <form id="import-form" class="dlg-body">
    <h2>Klassenliste importieren</h2>
    <p class="hint">Eine Zeile pro Schüler: <b>Nachname; Vorname; Klasse</b>, optional als vierte Spalte eine vorhandene Ausweisnummer. Am einfachsten die Spalten aus Excel kopieren und hier einfügen, oder eine CSV-Datei wählen. Eine Kopfzeile mit „Vorname“, „Nachname“, „Klasse“ darf dabei sein, dann ist die Reihenfolge egal. Schüler, die es schon gibt, werden übersprungen.</p>
    <textarea name="text" id="import-text" rows="9" placeholder="Muster;Max;5a&#10;Beispiel;Lea;5a"></textarea>
    <div class="row" style="margin-top:10px"><input type="file" id="import-datei" accept=".csv,.txt,text/csv,text/plain" style="background:none;padding:0"></div>
    <div class="actions">
      <button class="btn" type="submit">Importieren</button>
      <button class="btn secondary" type="button" id="import-close" style="margin-left:auto">Abbrechen</button>
    </div>
  </form>
</dialog>

<datalist id="klassen-liste"></datalist>
<datalist id="serien-liste"></datalist>
<div id="toasts"></div>

<script>
"use strict";
const $ = (sel) => document.querySelector(sel);
const FELDER = ["titel", "autoren", "verlag", "erscheinungsjahr", "genre"];
const SERIEN_FELDER = ["serie", "band"];
const LEERWERTE = ["", "Unbekannt", "Unbekannter Titel", "N/A", "Keine Angabe"];
let info = { standard_leihdauer: 14 };

// --- Helfer ---
function esc(wert) {
  return String(wert ?? "").replace(/[&<>"']/g, (z) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[z]));
}
function deDatum(iso) {
  if (!iso) return "–";
  const [j, m, t] = iso.split("-");
  return `${t}.${m}.${j}`;
}
function statusBadge(tage) {
  if (tage < 0) return `<span class="badge danger">Überfällig seit ${-tage} Tag${tage === -1 ? "" : "en"}</span>`;
  if (tage === 0) return `<span class="badge warn">Heute fällig</span>`;
  if (tage <= 2) return `<span class="badge warn">Noch ${tage} Tag${tage === 1 ? "" : "e"}</span>`;
  return `<span class="badge ok">Noch ${tage} Tage</span>`;
}
function toast(text, fehler = false) {
  const el = document.createElement("div");
  el.className = "toast" + (fehler ? " error" : "");
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), fehler ? 6000 : 3500);
}
async function antwort(res) {
  const daten = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(daten.fehler || "Unbekannter Fehler");
  return daten;
}
async function get(name, params = {}) {
  return antwort(await fetch(`/api/${name}?` + new URLSearchParams(params)));
}
async function post(name, body = {}) {
  return antwort(await fetch(`/api/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-ScanBook": "1" },
    body: JSON.stringify(body),
  }));
}
function formDaten(form) {
  return Object.fromEntries(new FormData(form).entries());
}
function debounce(fn, ms = 200) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
function speicherLesen(name, standard) {
  try { const w = localStorage.getItem(name); return w === null ? standard : w; } catch { return standard; }
}
function speicherSchreiben(name, wert) {
  try { localStorage.setItem(name, wert); } catch { /* egal */ }
}
// Cover: gespeicherte Adresse, sonst Open Library und DNB nach ISBN fragen
function zeigeCover(box, b) {
  const img = box.querySelector("img"), leer = box.querySelector(".cover-leer");
  const quellen = [b.cover_url, b.isbn && `https://covers.openlibrary.org/b/isbn/${b.isbn}-M.jpg?default=false`,
    b.isbn && `https://portal.dnb.de/opac/mvb/cover?isbn=${b.isbn}`].filter(Boolean);
  let i = 0;
  const weiter = () => {
    if (i < quellen.length) { img.src = quellen[i++]; return; }
    img.removeAttribute("src"); img.classList.add("hidden"); leer.classList.remove("hidden");
  };
  img.onerror = weiter;
  img.onload = () => { if (img.naturalWidth < 20) weiter(); };  // 1×1-Platzhalter überspringen
  img.classList.remove("hidden"); leer.classList.add("hidden");
  weiter();
}
function bandWert(band) {
  const n = parseInt(band, 10);
  return Number.isNaN(n) ? Infinity : n;
}
function nachBand(a, b) {
  return bandWert(a.band) - bandWert(b.band) || String(a.band).localeCompare(String(b.band)) || a.titel.localeCompare(b.titel, "de");
}
function serieText(serie, band) {
  return serie ? (band ? `${serie} · Band ${band}` : serie) : "";
}
function codeHtml(code) {
  return `<span class="code">${esc(code)}</span>`;
}
function drucken(pfad, params = {}) {
  window.open(`/druck/${pfad}?` + new URLSearchParams(params), "_blank");
}
// Liefert null, wenn sich die Antwort seit dem letzten Mal nicht geändert hat –
// dann bleibt die Liste stehen, statt beim Tabwechsel neu aufgebaut zu werden.
const letzteAntwort = new Map();
async function getNeu(name, params = {}) {
  const res = await fetch(`/api/${name}?` + new URLSearchParams(params));
  const text = await res.text();
  let daten = {};
  try { daten = JSON.parse(text); } catch { /* leer lassen */ }
  if (!res.ok) throw new Error(daten.fehler || "Unbekannter Fehler");
  if (letzteAntwort.get(name) === text) return null;
  letzteAntwort.set(name, text);
  return daten;
}
// Lange Listen in Stücken zeichnen: erst SCHRITT Zeilen, weitere beim Herunterscrollen.
// behalten = so viele Zeilen wie vorher zeigen (z. B. nach dem Aufklappen einer Serie)
const SCHRITT = 100;
const mehrBeobachter = "IntersectionObserver" in window
  ? new IntersectionObserver((eintraege) => eintraege.forEach((e) => e.isIntersecting && e.target.weiter?.()), { rootMargin: "600px" })
  : null;
function stueckweise(tbody, mehr, liste, zeile, leerHtml, behalten = false) {
  const ziel = behalten ? Math.max(SCHRITT, mehr.gezeigt || 0) : SCHRITT;
  let n = 0;
  mehr.weiter = (anzahl = SCHRITT) => {
    if (n >= liste.length) return;
    const teil = liste.slice(n, n + anzahl);
    n += teil.length;
    mehr.gezeigt = n;
    tbody.insertAdjacentHTML("beforeend", teil.map(zeile).join(""));
    const rest = liste.length - n;
    mehr.classList.toggle("hidden", rest <= 0);
    if (rest > 0) mehr.innerHTML = `${n} von ${liste.length} angezeigt <button class="btn small secondary" type="button">Weitere anzeigen</button>`;
  };
  if (mehrBeobachter && !mehr.beobachtet) { mehrBeobachter.observe(mehr); mehr.beobachtet = true; }
  tbody.innerHTML = liste.length ? "" : leerHtml;
  mehr.classList.add("hidden");
  mehr.gezeigt = 0;
  mehr.weiter(ziel);
}
document.addEventListener("click", (e) => {
  const box = e.target.closest(".mehr");
  if (box && e.target.closest("button")) box.weiter?.();
});
async function versuche(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); }
}

// --- Navigation ---
const LADER = {
  scan: () => $("#scan-isbn").focus(),
  katalog: () => ladeKatalog(),
  regale: () => ladeRegale(),
  ausleihe: () => { ladeAusleihen(); ladeNamen(); (ausleihPerson ? $("#co-isbn") : $("#co-name")).focus(); },
  schueler: () => (schuelerAnsicht === "verlauf" ? ladePersonen() : ladeSchueler()),
  verwaltung: () => { ladeStatistik(); ladeCodesStatus(); ladeEinstellungen(); ladeBackups(); },
};
// Alte Lesezeichen weiter unterstützen
const ALTE_TABS = { personen: "schueler", statistik: "verwaltung", einstellungen: "verwaltung" };
const SEITEN_TITEL = { scan: "Bücher erfassen" };
function zeigeTab(name) {
  if (ALTE_TABS[name]) { if (name === "personen") zeigeSchuelerAnsicht("verlauf", false); name = ALTE_TABS[name]; }
  if (!LADER[name]) name = "ausleihe";
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $("#seiten-titel").textContent = SEITEN_TITEL[name] || $(`nav button[data-tab="${name}"]`).textContent;
  document.querySelectorAll("main > section").forEach((s) => s.classList.toggle("active", s.id === "tab-" + name));
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  LADER[name]();
}
$("#nav").addEventListener("click", (e) => { const b = e.target.closest("button[data-tab]"); if (b) zeigeTab(b.dataset.tab); });
window.addEventListener("hashchange", () => zeigeTab(location.hash.slice(1)));

async function ladeInfo() {
  info = await get("info");
  $("#bib-name").textContent = info.bib_name + (info.schulname ? " · " + info.schulname : "");
  $("#version").textContent = "Version " + info.version;
  $("#co-tage").placeholder = info.standard_leihdauer;
}

// --- Scannen ---
function logEintrag(html) {
  const log = $("#scan-log");
  if (log.querySelector(".sub")) log.innerHTML = "";
  const zeit = new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  log.insertAdjacentHTML("afterbegin", `<li><span class="time">${zeit}</span><span>${html}</span></li>`);
  while (log.children.length > 15) log.lastChild.remove();
}

function zeigeNeuesBuch(isbn, daten, fehlend, hinweis) {
  const form = $("#new-book-form");
  form.reset();
  form.code.value = wartenderCode || "";
  form.isbn.value = isbn || "";
  form.isbn.readOnly = Boolean(isbn);
  for (const f of FELDER) {
    const wert = daten?.[f] ?? "";
    form[f].value = LEERWERTE.includes(wert) ? "" : wert;
    form[f].classList.toggle("missing", fehlend.includes(f));
  }
  for (const f of [...SERIEN_FELDER, "beschreibung", "cover_url"]) form[f].value = daten?.[f] ?? "";
  form.nachgeladen.value = daten ? "1" : "";
  const vorschau = $("#nb-vorschau");
  vorschau.classList.toggle("hidden", !daten);
  if (daten) {
    zeigeCover(vorschau.querySelector(".cover-box"), { ...daten, isbn });
    $("#nb-beschreibung").textContent = daten.beschreibung || "Keine Beschreibung gefunden.";
  }
  $("#new-book-title").textContent = daten ? (daten.titel || "Neues Buch") : "Neues Buch eintragen";
  $("#new-book-hint").textContent = hinweis;
  $("#new-book-card").classList.remove("hidden");
  // Ohne ISBN → ISBN-Feld, bei fehlenden Angaben → erstes fehlendes Feld, sonst → Speichern ([Enter] speichert)
  (!isbn ? form.isbn : fehlend.length ? form[fehlend[0]] : $("#nb-save")).focus();
}

// Großer Hinweis unter dem Scan-Feld: Wo gehört das Buch hin?
function zeigePlatz(b) {
  const box = $("#platz-box");
  const platz = b.standort_soll || b.standort;
  box.classList.toggle("warn", !platz);
  if (platz) {
    box.innerHTML = `📍 „${esc(b.titel)}“ gehört nach <b>${esc(platz)}</b>`
      + (b.umstellen ? `<div class="sub">Dafür ${b.umstellen === 1 ? "muss 1 anderes Buch" : `müssen ${b.umstellen} andere Bücher`} umgestellt werden – siehe Tab „Regale“.</div>` : "");
  } else if (b.kein_platz === "voll") {
    box.textContent = `Für „${b.titel}“ ist in den passenden Regalen kein Platz mehr. Lege im Tab „Regale“ ein Fach oder Regal dazu.`;
  } else if (b.kein_platz === "kein_regal") {
    box.textContent = `Für das Genre von „${b.titel}“ gibt es noch kein Regal. Lege im Tab „Regale“ eins an (oder eins ohne Genres für alle übrigen Bücher).`;
  } else {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
}

function schliesseNeuesBuch() {
  $("#new-book-card").classList.add("hidden");
  setzeWartendenCode(null);
  $("#scan-isbn").focus();
}

// Eigenes Etikett gescannt, das ScanBook noch nicht kennt: das nächste gescannte Buch bekommt diesen Code
let wartenderCode = null;
function setzeWartendenCode(code) {
  wartenderCode = code;
  const box = $("#code-box");
  box.classList.toggle("hidden", !code);
  if (code) box.innerHTML = `🏷️ Neuer Mediencode ${codeHtml(code)} – jetzt die <b>ISBN des Buchs</b> scannen, um ihn zuzuordnen.
    <button class="btn small secondary" type="button" id="code-abbrechen" style="margin-left:8px">Abbrechen</button>`;
}
$("#code-box").addEventListener("click", (e) => {
  if (e.target.id === "code-abbrechen") { setzeWartendenCode(null); $("#scan-isbn").focus(); }
});

$("#scan-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const feld = $("#scan-isbn");
  const isbn = feld.value.trim();
  if (!isbn) return;
  feld.value = "";
  versuche(async () => {
    feld.disabled = true;
    try {
      const r = await post("scan", { isbn, code: wartenderCode || "" });
      if (r.status === "neuer_code") {
        setzeWartendenCode(r.code);
      } else if (r.status === "exemplar") {
        const b = r.buch;
        toast(`${r.code} gehört zu „${b.titel}“`);
        logEintrag(`🏷️ ${codeHtml(r.code)} ist ein Exemplar von <b>${esc(b.titel)}</b>${b.standort ? ` · 📍 ${esc(b.standort)}` : ""}`);
        zeigePlatz(b);
      } else if (r.status === "zugeordnet") {
        const b = r.buch;
        setzeWartendenCode(null);
        toast(`${r.code} → „${b.titel}“` + (r.aufgestockt ? ` – neues Exemplar, jetzt ${b.anzahl_gesamt}×` : ""));
        logEintrag(`🏷️ ${codeHtml(r.code)} → <b>${esc(b.titel)}</b>` + (r.aufgestockt ? ` (neues Exemplar, ${b.anzahl_gesamt}×)` : " (vorhandenes Exemplar)"));
        zeigePlatz(b);
        ladeCodesStatus();
      } else if (r.status === "aufgestockt") {
        const b = r.buch;
        toast(`„${b.titel}“ aufgestockt – jetzt ${b.anzahl_gesamt}×`);
        logEintrag(`➕ <b>${esc(b.titel)}</b> aufgestockt (${b.anzahl_verfuegbar}/${b.anzahl_gesamt} verfügbar)${b.standort ? ` · 📍 ${esc(b.standort)}` : ""}`);
        zeigePlatz(b);
      } else if (r.daten) {
        zeigeNeuesBuch(r.isbn, r.daten, r.fehlend,
          (r.fehlend.length ? "Gefunden! Gelb markierte Angaben fehlen – du kannst sie ergänzen. [Enter] speichert." : "Gefunden! Bitte kurz prüfen. [Enter] speichert.")
          + (r.code ? ` Das Exemplar bekommt den Mediencode ${r.code}.` : ""));
      } else {
        zeigeNeuesBuch(r.isbn, null, ["titel"], "Dieses Buch wurde online nicht gefunden. Bitte die Daten von Hand eintragen.");
      }
    } finally {
      feld.disabled = false;
      if ($("#new-book-card").classList.contains("hidden")) feld.focus();
    }
  });
});

$("#manual-btn").addEventListener("click", () => zeigeNeuesBuch("", null, [], "ISBN und Buchdaten von Hand eintragen."));
$("#nb-cancel").addEventListener("click", schliesseNeuesBuch);
$("#new-book-form").addEventListener("submit", (e) => {
  e.preventDefault();
  versuche(async () => {
    const b = await post("book_create", formDaten(e.target));
    toast(`„${b.titel}“ gespeichert`);
    if (wartenderCode) ladeCodesStatus();
    const platz = b.standort_soll || b.standort;
    logEintrag(`✨ <b>${esc(b.titel)}</b> neu hinzugefügt (${b.anzahl_gesamt}×)${platz ? ` · 📍 ${esc(platz)}` : ""}`
      + (b.umstellen ? ` · ↪️ ${b.umstellen} Buch${b.umstellen === 1 ? "" : "er"} umstellen (Tab „Regale“)` : ""));
    zeigePlatz(b);
    schliesseNeuesBuch();
    if (b.serie) katalogHolen().catch(() => {});
  });
});

// --- Mediencodes & Etiketten ---
async function ladeCodesStatus() {
  const st = await get("codes_status").catch(() => null);
  if (!st) return;
  const teile = [`${st.mit_code} Exemplar${st.mit_code === 1 ? " hat" : "e haben"} einen Mediencode`];
  if (st.ohne_code) teile.push(`${st.ohne_code} noch keinen`);
  if (st.ungedruckt) teile.push(`${st.ungedruckt} Etikett${st.ungedruckt === 1 ? "" : "en"} noch nicht gedruckt`);
  $("#codes-status").textContent = teile.join(", ") + ".";
  $("#codes-erzeugen").disabled = !st.ohne_code;
  $("#etiketten-drucken").disabled = !st.ungedruckt;
}
$("#codes-erzeugen").addEventListener("click", () => {
  if (!confirm("Für alle Exemplare ohne Mediencode eine neue Nummer erzeugen? Die Etiketten druckst du danach mit „Neue Etiketten drucken“.")) return;
  versuche(async () => {
    const r = await post("codes_erzeugen");
    toast(`${r.codes.length} Mediencode${r.codes.length === 1 ? "" : "s"} erzeugt`);
    ladeCodesStatus();
  });
});
$("#etiketten-drucken").addEventListener("click", () => {
  const frei = prompt("Wie viele Etiketten auf dem ersten Bogen sind schon verbraucht? (0 = neuer Bogen)", "0");
  if (frei === null) return;
  drucken("etiketten", { frei });
  setTimeout(ladeCodesStatus, 4000);
});

// --- Katalog ---
const offeneSerien = new Set();
$("#serien-gruppieren").checked = speicherLesen("serienGruppieren", "1") === "1";

function buchZeile(b, alsBand = false) {
  const titel = alsBand
    ? `${b.band ? `<span class="band-nr">${esc(b.band)}</span>` : ""}<b>${esc(b.titel)}</b>`
    : `<b>${esc(b.titel)}</b>${b.serie ? `<div class="serie-tag">📚 ${esc(serieText(b.serie, b.band))}</div>` : ""}`;
  const verlag = !alsBand && b.verlag && !LEERWERTE.includes(b.verlag) ? `${esc(b.verlag)} · ` : "";
  return `
      <tr class="clickable${alsBand ? " band-zeile" : ""}" data-isbn="${esc(b.isbn)}">
        <td class="titel-zelle">${titel}<div class="sub">${verlag}${esc(b.isbn)}</div>${b.standort ? `<div class="ort-mobil">${ortHtml(b.standort, b.geplant)}</div>` : ""}</td>
        <td>${esc(b.autor)}</td>
        <td class="num">${esc(b.jahr)}</td>
        <td>${esc(b.genre)}</td>
        <td>${ortHtml(b.standort, b.geplant)}</td>
        <td class="num"><span class="badge ${b.verfuegbar > 0 ? "ok" : "danger"}">${b.verfuegbar} von ${b.gesamt}</span></td>
      </tr>`;
}
function ortHtml(text, geplant = false) {
  if (!text) return "";
  return `<span class="ort${geplant ? " geplant" : ""}" title="${geplant ? "Geplanter Platz, noch nicht eingeräumt" : "Steht hier im Regal"}">📍 ${esc(text)}${geplant ? " (geplant)" : ""}</span>`;
}
// Serie: ein gemeinsamer Platz, sonst das Regal oder „mehrere Regale“
function serienOrt(baende) {
  const orte = [...new Set(baende.map((b) => b.standort).filter(Boolean))];
  if (orte.length <= 1) return ortHtml(orte[0] || "", baende.some((b) => b.standort && b.geplant));
  const regale = [...new Set(baende.map((b) => b.regal).filter(Boolean))];
  return ortHtml(regale.length === 1 ? `${regale[0]}, mehrere Fächer` : "mehrere Regale");
}

function serienZeilen(g, offen) {
  const autoren = [...new Set(g.baende.map((b) => b.autor))];
  const jahre = g.baende.map((b) => parseInt(b.jahr, 10)).filter((j) => !Number.isNaN(j));
  const von = Math.min(...jahre), bis = Math.max(...jahre);
  const verfuegbar = g.baende.reduce((n, b) => n + b.verfuegbar, 0);
  const gesamt = g.baende.reduce((n, b) => n + b.gesamt, 0);
  const genres = [...new Set(g.baende.map((b) => b.genre).filter((x) => !LEERWERTE.includes(x)))];
  return `
      <tr class="clickable serie-zeile${offen ? " offen" : ""}" data-serie="${esc(g.schluessel)}">
        <td class="titel-zelle"><span class="pfeil">▸</span> <b>📚 ${esc(g.serie)}</b><div class="sub">${g.baende.length} Bände · ${offen ? "zuklappen" : "aufklappen"}</div></td>
        <td>${esc(autoren.length > 2 ? autoren.slice(0, 2).join(", ") + " u. a." : autoren.join(", "))}</td>
        <td class="num">${jahre.length ? (von === bis ? von : `${von}–${bis}`) : ""}</td>
        <td>${esc(genres.length > 1 ? genres[0] + " …" : genres[0] || "")}</td>
        <td>${serienOrt(g.baende)}</td>
        <td class="num"><span class="badge ${verfuegbar > 0 ? "ok" : "danger"}">${verfuegbar} von ${gesamt}</span></td>
      </tr>` + (offen ? g.baende.map((b) => buchZeile(b, true)).join("") : "");
}

// Cover-Ansicht: gemaltes Cover in der Farbe des Genres, darüber das echte, sobald es geladen ist
const COVER_FARBEN = [["#5a3d9a", "#8e5ccf"], ["#d65a2e", "#f29a52"], ["#1f86b5", "#4cc3e8"], ["#2b2d42", "#55597a"], ["#2e8b57", "#5cc189"],
  ["#b83227", "#e2665a"], ["#7a6650", "#a8907a"], ["#1d4e89", "#3a7bd5"], ["#a83c66", "#e07a9f"], ["#2f6f69", "#5faea4"]];
function coverFarbe(text) {
  let h = 0;
  for (const z of String(text).toLowerCase()) h = (h * 31 + z.codePointAt(0)) >>> 0;
  const [c1, c2] = COVER_FARBEN[h % COVER_FARBEN.length];
  return `--c1:${c1};--c2:${c2}`;
}
function coverHtml(b, titel, autor, band = "") {
  const genre = LEERWERTE.includes(b.genre) ? "" : b.genre;
  const bild = b.cover_url ? `<img src="${esc(b.cover_url)}" alt="" loading="lazy" onerror="this.remove()" onload="if (this.naturalWidth < 20) this.remove()">` : "";
  return `<div class="cover${band ? " mit-band" : ""}" style="${coverFarbe(genre || titel)}"><div class="cover-text"><span class="cover-titel">${esc(titel)}</span><span class="cover-autor">${esc(autor)}</span></div>${bild}${band ? `<span class="cover-band">${esc(band)}</span>` : ""}</div>`;
}
function buchKachel(b, alsBand = false) {
  const da = b.verfuegbar > 0 ? `${b.verfuegbar} von ${b.gesamt} da` : "alle ausgeliehen";
  return `
      <button class="kachel${alsBand ? " band" : ""}" type="button" data-isbn="${esc(b.isbn)}" title="${esc(b.titel)}">
        ${coverHtml(b, b.titel, b.autor, b.band ? `Bd. ${b.band}` : "")}
        <div class="kachel-titel">${esc(b.titel)}</div>
        <div class="kachel-sub"><i class="punkt${b.verfuegbar > 0 ? "" : " weg"}"></i>${da}</div>
        ${b.standort ? `<div class="kachel-sub" title="${esc(b.standort)}">📍 ${esc(b.standort)}${b.geplant ? " (geplant)" : ""}</div>` : ""}
      </button>`;
}
function serienKacheln(g, offen) {
  const verfuegbar = g.baende.reduce((n, b) => n + b.verfuegbar, 0);
  const gesamt = g.baende.reduce((n, b) => n + b.gesamt, 0);
  const erster = g.baende[0];
  return `
      <button class="kachel serie" type="button" data-serie="${esc(g.schluessel)}" title="${esc(g.serie)}">
        <div class="stapel" style="${coverFarbe(g.serie)}">${coverHtml({ ...erster, genre: "" }, g.serie, erster.autor, `${g.baende.length} Bände`)}</div>
        <div class="kachel-titel">📚 ${esc(g.serie)}</div>
        <div class="kachel-sub"><i class="punkt${verfuegbar > 0 ? "" : " weg"}"></i>${verfuegbar} von ${gesamt} da · ${offen ? "zuklappen" : "aufklappen"}</div>
      </button>` + (offen ? g.baende.map((b) => buchKachel(b, true)).join("") : "");
}

// Der Katalog wird einmal geladen und dann im Browser durchsucht – das geht ohne Wartezeit
let katalog = [], katalogGeladen = false, katalogFilter = "alle", regalFilter = "alle";
let katalogAnsicht = speicherLesen("katalogAnsicht", "cover") === "liste" ? "liste" : "cover";
const katalogZiel = () => $(katalogAnsicht === "cover" ? "#katalog-wand" : "#katalog-body");
function passtZurSuche(b, worte) {
  const text = `${b.titel} ${b.autoren} ${b.autor} ${b.genre} ${b.isbn} ${b.verlag} ${b.serie} ${b.standort}`.toLowerCase();
  return worte.every((w) => text.includes(w) || text.includes(w.replace(/-/g, "")));
}
function zeichneKatalog(behalten = false) {
  const suche = $("#katalog-suche").value.trim().toLowerCase();
  const worte = suche.split(/\s+/).filter(Boolean);
  zeichneRegalFilter();
  const liste = katalog.filter((b) =>
    (katalogFilter === "alle" || (katalogFilter === "da" ? b.verfuegbar > 0 : b.verfuegbar === 0)) &&
    (regalFilter === "alle" || (regalFilter === "ohne" ? !b.regal : "r:" + b.regal === regalFilter)) &&
    (!worte.length || passtZurSuche(b, worte)));
  $("#katalog-anzahl").textContent = liste.length === katalog.length ? `${katalog.length} Titel` : `${liste.length} von ${katalog.length} Titeln`;
  const weg = katalog.filter((b) => b.verfuegbar === 0).length;
  $("#katalog-filter").innerHTML = [["alle", "Alle"], ["da", "Verfügbar"], ["weg", `Alle ausgeliehen${weg ? ` · ${weg}` : ""}`]]
    .map(([k, t]) => `<button class="chip ${k === katalogFilter ? "active" : ""}" type="button" data-filter="${k}">${t}</button>`).join("");
  let eintraege = liste;
  if ($("#serien-gruppieren").checked) {
    // Bände einer Serie an der Stelle des ersten Bandes zusammenfassen
    const gruppen = new Map();
    eintraege = [];
    for (const b of liste) {
      const schluessel = b.serie.trim().toLowerCase();
      if (!schluessel) { eintraege.push(b); continue; }
      if (!gruppen.has(schluessel)) {
        const g = { schluessel, serie: b.serie, baende: [] };
        gruppen.set(schluessel, g);
        eintraege.push(g);
      }
      gruppen.get(schluessel).baende.push(b);
    }
    eintraege = eintraege.map((e) => (e.baende && e.baende.length === 1 ? e.baende[0] : e));
  }
  const wand = katalogAnsicht === "cover";
  $("#katalog-tabelle").classList.toggle("hidden", wand);
  $("#katalog-wand").classList.toggle("hidden", !wand);
  $("#katalog-ansicht").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.ansicht === katalogAnsicht));
  const zeile = (e) => {
    if (!e.baende) return wand ? buchKachel(e) : buchZeile(e);
    e.baende.sort(nachBand);
    const offen = Boolean(suche) || offeneSerien.has(e.schluessel);
    return wand ? serienKacheln(e, offen) : serienZeilen(e, offen);
  };
  const hinweis = suche || katalogFilter !== "alle" || regalFilter !== "alle" ? "Keine Treffer." : "Der Katalog ist leer. Scanne dein erstes Buch unter „Erfassen“.";
  const leer = wand ? `<div class="empty">${hinweis}</div>` : `<tr><td colspan="6" class="empty">${hinweis}</td></tr>`;
  $(wand ? "#katalog-body" : "#katalog-wand").innerHTML = "";
  stueckweise(katalogZiel(), $("#katalog-mehr"), eintraege, zeile, leer, behalten);
}

// Regal-Filter: nur sichtbar, wenn Regale angelegt sind; Reihenfolge wie im Tab „Regale“
function zeichneRegalFilter() {
  const regale = [...new Map(katalog.filter((b) => b.regal).map((b) => [b.regal, b.regal_nr])).entries()]
    .sort((a, b) => a[1] - b[1]).map(([name]) => name);
  if (regalFilter.startsWith("r:") && !regale.includes(regalFilter.slice(2))) regalFilter = "alle";
  const ohne = katalog.filter((b) => !b.regal).length;
  if (regalFilter === "ohne" && !ohne) regalFilter = "alle";
  const box = $("#regal-filter");
  box.classList.toggle("hidden", !regale.length);
  if (!regale.length) return;
  const chips = [["alle", "Alle Regale"], ...regale.map((r) => ["r:" + r, `📍 ${r}`]), ...(ohne ? [["ohne", `Ohne Platz · ${ohne}`]] : [])];
  box.innerHTML = chips.map(([k, t]) =>
    `<button class="chip ${k === regalFilter ? "active" : ""}" type="button" data-regal="${esc(k)}">${esc(t)}</button>`).join("");
}

// Vorschläge für das Serien-Feld, damit derselbe Name immer gleich geschrieben wird
function setzeSerienListe(buecher) {
  const serien = [...new Set(buecher.map((b) => b.serie).filter(Boolean))].sort((a, b) => a.localeCompare(b, "de"));
  $("#serien-liste").innerHTML = serien.map((s) => `<option value="${esc(s)}">`).join("");
}

async function katalogHolen() {
  const neu = await getNeu("books");
  if (!neu) return false;
  katalog = neu;
  katalogGeladen = true;
  setzeSerienListe(katalog);
  return true;
}
async function ladeKatalog() {
  if (katalogGeladen) zeichneKatalog(true);  // sofort zeigen, was schon da ist, dann still abgleichen
  await versuche(async () => {
    if (await katalogHolen() || !katalogZiel().children.length) zeichneKatalog(true);
  });
  zeigeNachladen(await get("refresh_status").catch(() => null));
}
$("#katalog-suche").addEventListener("input", debounce(() => zeichneKatalog(), 120));
$("#katalog-filter").addEventListener("click", (e) => {
  if (!e.target.dataset.filter) return;
  katalogFilter = e.target.dataset.filter;
  zeichneKatalog();
});
$("#regal-filter").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-regal]");
  if (!chip) return;
  regalFilter = chip.dataset.regal;
  zeichneKatalog();
});
$("#serien-gruppieren").addEventListener("change", (e) => {
  speicherSchreiben("serienGruppieren", e.target.checked ? "1" : "0");
  zeichneKatalog();
});
$("#katalog-ansicht").addEventListener("click", (e) => {
  const knopf = e.target.closest("[data-ansicht]");
  if (!knopf || knopf.dataset.ansicht === katalogAnsicht) return;
  katalogAnsicht = knopf.dataset.ansicht;
  speicherSchreiben("katalogAnsicht", katalogAnsicht);
  zeichneKatalog(true);
});
$("#katalog-wand").addEventListener("click", (e) => {
  const serie = e.target.closest("[data-serie]");
  if (serie) {
    const s = serie.dataset.serie;
    offeneSerien.has(s) ? offeneSerien.delete(s) : offeneSerien.add(s);
    zeichneKatalog(true);
    return;
  }
  const kachel = e.target.closest("[data-isbn]");
  if (kachel) oeffneVorschau(kachel.dataset.isbn);
});
$("#katalog-body").addEventListener("click", (e) => {
  const serie = e.target.closest("tr[data-serie]");
  if (serie) {
    const s = serie.dataset.serie;
    offeneSerien.has(s) ? offeneSerien.delete(s) : offeneSerien.add(s);
    zeichneKatalog(true);
    return;
  }
  const zeile = e.target.closest("tr[data-isbn]");
  if (zeile) oeffneVorschau(zeile.dataset.isbn);
});

// --- Fehlende Angaben für den ganzen Katalog nachladen ---
let nachladenTimer = null;
function zeigeNachladen(st) {
  const box = $("#nachladen-box");
  if (!st) return;
  clearTimeout(nachladenTimer);
  const balken = $("#nachladen-balken");
  if (st.laeuft) {
    box.classList.remove("hidden");
    balken.classList.remove("hidden");
    balken.firstElementChild.style.width = `${st.gesamt ? Math.round(100 * st.erledigt / st.gesamt) : 0}%`;
    $("#nachladen-text").textContent = `Lade Cover, Beschreibung und Serie … ${st.erledigt} von ${st.gesamt}` + (st.titel ? ` (zuletzt: ${st.titel})` : "");
    $("#nachladen-btn").disabled = true;
    nachladenTimer = setTimeout(async () => {
      const neu = await get("refresh_status").catch(() => null);
      if (neu && !neu.laeuft) {
        toast(neu.fehler ? `Nachladen abgebrochen: ${neu.fehler}`
          : `Fertig: ${neu.gesamt} Bücher geprüft, bei ${neu.ergaenzt} Angaben ergänzt.`
            + (neu.offen ? ` Bei ${neu.offen} war eine Quelle nicht erreichbar, später noch einmal versuchen.` : ""), Boolean(neu.fehler));
        if ($("#tab-katalog").classList.contains("active")) { ladeKatalog(); return; }
      }
      zeigeNachladen(neu);
    }, 1500);
    return;
  }
  balken.classList.add("hidden");
  $("#nachladen-btn").disabled = false;
  box.classList.toggle("hidden", !st.offen);
  $("#nachladen-text").textContent = st.offen === 1
    ? "Für 1 Buch wurden Cover, Beschreibung und Serie noch nicht gesucht."
    : `Für ${st.offen} Bücher wurden Cover, Beschreibung und Serie noch nicht gesucht. Deine eigenen Angaben bleiben dabei unverändert.`;
}
$("#nachladen-btn").addEventListener("click", () => versuche(async () => zeigeNachladen(await post("refresh_all"))));

// --- Buchvorschau ---
let aktuelleIsbn = null;
async function oeffneVorschau(isbn) {
  await versuche(async () => {
    const b = await get("book", { isbn });
    aktuelleIsbn = isbn;
    const leer = (w) => !w || LEERWERTE.includes(w);
    zeigeCover($("#pv-cover"), { ...b, isbn });
    $("#pv-titel").textContent = b.titel;
    $("#pv-autor").textContent = leer(b.autoren) ? "Autor unbekannt" : b.autoren;
    $("#pv-bestand").innerHTML = `<span class="badge ${b.anzahl_verfuegbar > 0 ? "ok" : "danger"}">${b.anzahl_verfuegbar} von ${b.anzahl_gesamt} verfügbar</span>`;
    const daten = [
      ["Standort", b.standort ? (b.standort_soll ? `${b.standort} (umstellen nach ${b.standort_soll})` : b.standort)
        : (b.standort_soll ? `${b.standort_soll} (noch nicht eingeräumt)` : "")],
      ["Serie", serieText(b.serie, b.band)], ["Genre", b.genre], ["Verlag", b.verlag],
      ["Erschienen", b.erscheinungsjahr], ["ISBN", isbn],
    ].filter(([, w]) => !leer(w));
    $("#pv-daten").innerHTML = daten.map(([k, w]) => `<dt>${k}</dt><dd>${esc(w)}</dd>`).join("");
    const beschreibung = $("#pv-beschreibung");
    beschreibung.textContent = b.beschreibung || (b.nachgeladen
      ? "Keine Beschreibung gefunden. Unter „Bearbeiten“ kannst du selbst eine eintragen."
      : "Noch keine Beschreibung – „Daten online nachladen“ sucht danach.");
    beschreibung.classList.toggle("leer", !b.beschreibung);

    const baende = b.serie_baende || [];
    $("#pv-baende-block").classList.toggle("hidden", baende.length < 2);
    $("#pv-baende-titel").textContent = `${b.serie} – ${baende.length} Bände im Katalog`;
    $("#pv-baende").innerHTML = baende.map((x) => `
      <li data-isbn="${esc(x.isbn)}" class="${x.isbn === isbn ? "aktuell" : ""}">
        <span class="band-nr">${esc(x.band || "–")}</span><span>${esc(x.titel)}</span>
        <span class="badge ${x.verfuegbar > 0 ? "ok" : "danger"}">${x.verfuegbar}/${x.gesamt}</span>
      </li>`).join("");

    $("#pv-ausleihen-block").classList.toggle("hidden", !b.ausleihen.length);
    $("#pv-ausleihen").innerHTML = b.ausleihen.map((a) =>
      `<li><span>${esc(a.name)}</span>${a.exemplar ? codeHtml(a.exemplar) : ""}<span class="time">bis ${deDatum(a.bis)}</span>${statusBadge(a.tage)}</li>`).join("");
    zeigeCodes(b);

    const dlg = $("#preview-dialog");
    if (!dlg.open) dlg.showModal();
    dlg.querySelector(".dlg-body").scrollTop = 0;
  });
}
$("#pv-close").addEventListener("click", () => $("#preview-dialog").close());

function zeigeCodes(b) {
  const codes = b.exemplare;
  const ohne = Math.max(0, b.anzahl_gesamt - codes.length);
  const perIsbn = b.ausleihen.filter((a) => !a.exemplar).length;
  $("#pv-codes-titel").textContent = codes.length
    ? `Mediencodes – ${codes.length} von ${b.anzahl_gesamt} Exemplar${b.anzahl_gesamt === 1 ? "" : "en"}`
    : "Mediencodes";
  $("#pv-codes").innerHTML = codes.map((e) => `
    <li><span class="grow">${codeHtml(e.code)} <span class="time">${e.eigener ? "eigenes Etikett" : e.gedruckt ? "Etikett gedruckt" : "Etikett noch nicht gedruckt"}</span></span>
      ${e.ausgeliehen_an ? `<span class="badge warn">bei ${esc(e.ausgeliehen_an)}</span>` : `<span class="badge ok">da</span>`}
      <button class="btn small secondary" type="button" data-code-loeschen="${esc(e.code)}" title="Nur den Code entfernen, das Exemplar bleibt im Bestand">Code löschen</button>
      <button class="btn small danger" type="button" data-ausmustern="${esc(e.code)}" title="Exemplar ist verloren oder kaputt">Ausmustern</button>
    </li>`).join("") + (ohne ? `<li class="sub" style="color:var(--muted)">${ohne} Exemplar${ohne === 1 ? "" : "e"} noch ohne Code.</li>` : "")
    + (perIsbn && codes.length ? `<li class="sub" style="color:var(--muted)">${perIsbn} Ausleihe${perIsbn === 1 ? " wurde" : "n wurden"} per ISBN gebucht, darum ist nicht bekannt, welches Exemplar.</li>` : "");
  $("#pv-codes-erzeugen").disabled = !ohne;
  $("#pv-etiketten").disabled = !codes.some((e) => !e.eigener);
}
$("#pv-code-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const feld = $("#pv-code-neu");
  const code = feld.value.trim();
  if (!code) return;
  versuche(async () => {
    const r = await post("exemplar_neu", { isbn: aktuelleIsbn, code });
    feld.value = "";
    toast(`${r.code} zugeordnet` + (r.aufgestockt ? ` – neues Exemplar, jetzt ${r.buch.anzahl_gesamt}×` : ""));
    await oeffneVorschau(aktuelleIsbn);
    feld.focus();
    if (r.aufgestockt) ladeKatalog();
  });
});
$("#pv-codes-erzeugen").addEventListener("click", () => versuche(async () => {
  const r = await post("codes_erzeugen", { isbn: aktuelleIsbn });
  toast(`${r.codes.length} Mediencode${r.codes.length === 1 ? "" : "s"} erzeugt`);
  await oeffneVorschau(aktuelleIsbn);
}));
$("#pv-etiketten").addEventListener("click", () => drucken("etiketten", { isbn: aktuelleIsbn }));
$("#pv-codes").addEventListener("click", (e) => {
  const d = e.target.dataset;
  if (d.codeLoeschen && confirm(`Mediencode ${d.codeLoeschen} löschen? Das Exemplar bleibt im Bestand, nur ohne Code.`)) {
    versuche(async () => { await post("exemplar_loeschen", { code: d.codeLoeschen }); toast("Code gelöscht"); await oeffneVorschau(aktuelleIsbn); });
  }
  if (d.ausmustern && confirm(`Exemplar ${d.ausmustern} ausmustern (verloren oder kaputt)? Der Bestand sinkt um eins.`)) {
    versuche(async () => {
      await post("exemplar_entfernen", { isbn: aktuelleIsbn, code: d.ausmustern });
      toast("Exemplar ausgemustert");
      await oeffneVorschau(aktuelleIsbn);
      ladeKatalog();
    });
  }
});
$("#pv-baende").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-isbn]");
  if (li && li.dataset.isbn !== aktuelleIsbn) oeffneVorschau(li.dataset.isbn);
});
$("#pv-edit").addEventListener("click", () => {
  $("#preview-dialog").close();
  oeffneBearbeiten(aktuelleIsbn);
});
$("#pv-refresh").addEventListener("click", () => versuche(async () => {
  const knopf = $("#pv-refresh");
  knopf.disabled = true;
  knopf.textContent = "Suche online …";
  try {
    const r = await post("book_refresh", { isbn: aktuelleIsbn });
    if (r.ergaenzt.length) toast("Fehlende Angaben ergänzt.");
    else if (r.nicht_erreichbar.length) toast(`Nicht erreichbar: ${r.nicht_erreichbar.join(", ")}. Bitte später noch einmal versuchen.`, true);
    else toast(r.gefunden ? "Nichts Neues gefunden." : "Online nicht gefunden.");
    await oeffneVorschau(aktuelleIsbn);
    ladeKatalog();
  } finally {
    knopf.disabled = false;
    knopf.textContent = "Daten online nachladen";
  }
}));

// --- Bearbeiten-Dialog ---
async function oeffneBearbeiten(isbn) {
  await versuche(async () => {
    const b = await get("book", { isbn });
    aktuelleIsbn = isbn;
    const form = $("#edit-form");
    for (const f of FELDER) form[f].value = LEERWERTE.includes(b[f]) ? "" : b[f];
    for (const f of [...SERIEN_FELDER, "beschreibung"]) form[f].value = b[f] || "";
    $("#edit-hint").textContent = `ISBN ${isbn} · Bestand: ${b.anzahl_verfuegbar}/${b.anzahl_gesamt} verfügbar`;
    $("#edit-dialog").showModal();
  });
}
$("#ed-close").addEventListener("click", () => $("#edit-dialog").close());
$("#edit-form").addEventListener("submit", (e) => {
  e.preventDefault();
  versuche(async () => {
    const b = await post("book_update", { isbn: aktuelleIsbn, ...formDaten(e.target) });
    toast(`„${b.titel}“ gespeichert`);
    $("#edit-dialog").close();
    ladeKatalog();
    oeffneVorschau(aktuelleIsbn);
  });
});
$("#ed-remove").addEventListener("click", () => versuche(async () => {
  const b = await post("book_remove_copy", { isbn: aktuelleIsbn });
  toast(`Ein Exemplar entfernt – jetzt ${b.anzahl_gesamt}×`);
  $("#edit-hint").textContent = `ISBN ${aktuelleIsbn} · Bestand: ${b.anzahl_verfuegbar}/${b.anzahl_gesamt} verfügbar`;
  ladeKatalog();
}));
$("#ed-delete").addEventListener("click", () => {
  const titel = $("#edit-form").titel.value;
  if (!confirm(`„${titel}“ samt Ausleih-Historie wirklich löschen?`)) return;
  versuche(async () => {
    await post("book_delete", { isbn: aktuelleIsbn });
    toast(`„${titel}“ gelöscht`);
    $("#edit-dialog").close();
    ladeKatalog();
  });
});

// --- Regale ---
let regalDaten = { regale: [], genres: [] };
function buchChip(b) {
  const mitSerie = b.serie && b.band && !b.titel.toLowerCase().includes(b.serie.toLowerCase());
  const titel = (mitSerie ? `${b.serie} ${b.band}: ` : "") + b.titel + (b.anzahl > 1 ? ` (${b.anzahl}×)` : "");
  const klasse = b.neu ? "neu" : b.von ? "umstellen" : "";
  const tipp = [b.autor, b.genre, b.neu ? "noch nicht eingeräumt" : b.von ? `steht noch in ${b.von}` : ""].filter(Boolean).join(" · ");
  return `<span class="buch-chip ${klasse}" data-isbn="${esc(b.isbn)}" title="${esc(tipp)}">${esc(titel)}</span>`;
}
function regalGenreText(r) {
  const eigene = r.fach_genres.map((g, f) => (g.length ? `Fach ${f + 1}: ${g.join(", ")}` : "")).filter(Boolean);
  const rest = r.fach_genres.some((g) => !g.length);
  const teile = [...eigene];
  if (rest) teile.push((eigene.length ? "sonst " : "") + (r.genres.length ? r.genres.join(", ") : "alle übrigen Bücher"));
  return esc(teile.join(" · "));
}
function zeichneRegale(d) {
  regalDaten = d;
  const anzahl = d.regale.length;
  $("#regale-liste").innerHTML = anzahl ? d.regale.map((r, i) => {
    const belegt = r.inhalt.flat().reduce((n, b) => n + b.anzahl, 0);
    const platz = r.faecher * r.kapazitaet;
    const faecher = r.inhalt.map((fach, f) => {
      const n = fach.reduce((s, b) => s + b.anzahl, 0);
      const eigene = r.fach_genres[f] || [];
      return `<div class="fach"><div class="fach-nr">Fach ${f + 1}<span class="sub">${n}/${r.kapazitaet}</span>${eigene.length ? `<span class="fach-genre">${esc(eigene.join(", "))}</span>` : ""}</div>
        <div><div class="balken ${n >= r.kapazitaet ? "voll" : ""}"><div style="width:${Math.min(100, Math.round(100 * n / r.kapazitaet))}%"></div></div>
        <div class="fach-buecher">${fach.map(buchChip).join("") || `<span class="sub" style="color:var(--muted)">leer</span>`}</div></div></div>`;
    }).join("");
    return `<div class="regal">
      <div class="regal-kopf">
        <div class="grow"><b>${esc(r.name)}</b>
          <div class="sub">${r.faecher} Fächer × ${r.kapazitaet} Bücher · belegt ${belegt}/${platz} · ${regalGenreText(r)}</div></div>
        <button class="btn small secondary" data-hoch="${r.id}" ${i === 0 ? "disabled" : ""} title="Nach vorne (wird bei gleichen Genres zuerst gefüllt)">↑</button>
        <button class="btn small secondary" data-runter="${r.id}" ${i === anzahl - 1 ? "disabled" : ""} title="Nach hinten">↓</button>
        <button class="btn small secondary" data-bearbeiten="${r.id}">Bearbeiten</button>
        <button class="btn small danger" data-loeschen="${r.id}">Löschen</button>
      </div>
      <details ${speicherLesen("regalOffen" + r.id, "1") === "1" ? "open" : ""} data-regal="${r.id}"><summary>Fächer anzeigen</summary>${faecher}</details>
    </div>`;
  }).join("") : `<p class="empty" style="padding-left:0">Noch keine Regale. Klicke auf „Regal hinzufügen“.</p>`;

  const zuTun = d.umstellen.length + d.einraeumen;
  $("#regal-aufgaben").classList.toggle("hidden", !zuTun);
  const teile = [];
  if (d.einraeumen) teile.push(`${d.einraeumen} Buch${d.einraeumen === 1 ? "" : "er"} noch einräumen (grün markiert)`);
  if (d.umstellen.length) teile.push(`${d.umstellen.length} Buch${d.umstellen.length === 1 ? "" : "er"} umstellen (gelb markiert)`);
  $("#regal-aufgaben-text").textContent = teile.join(", ") + ". Wenn alles so steht wie unten angezeigt, klicke auf „Erledigt“. Neue Bücher werden danach passend dazu einsortiert.";
  $("#regal-umstellen").innerHTML = d.umstellen.map((b) =>
    `<li>↪️ <b>${esc(b.titel)}</b><span class="pfad">${esc(b.von)} → ${esc(b.nach)}</span></li>`).join("");

  $("#ohne-platz").classList.toggle("hidden", !d.ohne_platz.length);
  $("#ohne-platz-liste").innerHTML = d.ohne_platz.map((b) =>
    `<li><b>${esc(b.titel)}</b><span class="pfad">${b.grund === "voll" ? "passende Regale sind voll" : `kein Regal für: ${esc(b.genre || "ohne Genre")}`}</span></li>`).join("");
}
async function ladeRegale() {
  await versuche(async () => zeichneRegale(await get("regale")));
}
// Genre-Chips wirken auf das zuletzt angeklickte Feld (Regal oder ein Fach)
let genreFeld = null;
function zeichneFachGenres(werte) {
  const box = $("#rg-fach-genres");
  const alt = werte || [...box.querySelectorAll("input")].map((i) => i.value);
  const anzahl = Math.min(50, Math.max(1, Number($("#rg-faecher").value) || 1));
  box.innerHTML = Array.from({ length: anzahl }, (_, f) =>
    `<span>Fach ${f + 1}</span><input data-fach="${f}" placeholder="wie das Regal" autocomplete="off" value="${esc(alt[f] || "")}">`).join("");
  if (genreFeld && !genreFeld.isConnected) genreFeld = $("#rg-genres");
}
function genreChips() {
  const feld = genreFeld || $("#rg-genres");
  const gewaehlt = feld.value.split(",").map((g) => g.trim().toLowerCase()).filter(Boolean);
  $("#rg-genre-chips").innerHTML = regalDaten.genres.map((g) =>
    `<button type="button" class="chip ${gewaehlt.includes(g.toLowerCase()) ? "active" : ""}" data-genre="${esc(g)}">${esc(g)}</button>`).join("");
}
function oeffneRegal(r) {
  const form = $("#regal-form");
  form.regal_id.value = r?.id ?? "";
  form.name.value = r?.name ?? `Regal ${regalDaten.regale.length + 1}`;
  form.faecher.value = r?.faecher ?? 5;
  form.kapazitaet.value = r?.kapazitaet ?? 30;
  form.genres.value = r ? r.genres.join(", ") : "";
  $("#regal-dlg-titel").textContent = r ? "Regal bearbeiten" : "Neues Regal";
  const fachGenres = (r?.fach_genres || []).map((g) => g.join(", "));
  zeichneFachGenres(fachGenres);
  $("#rg-fach-details").open = fachGenres.some(Boolean);
  genreFeld = $("#rg-genres");
  genreChips();
  $("#regal-dialog").showModal();
  form.name.focus();
}
$("#regal-neu").addEventListener("click", () => oeffneRegal(null));
$("#rg-close").addEventListener("click", () => $("#regal-dialog").close());
$("#regal-form").addEventListener("focusin", (e) => {
  if (e.target.id === "rg-genres" || e.target.dataset.fach !== undefined) { genreFeld = e.target; genreChips(); }
});
$("#regal-form").addEventListener("input", (e) => {
  if (e.target === genreFeld) genreChips();
  if (e.target.id === "rg-faecher") zeichneFachGenres();
});
$("#rg-genre-chips").addEventListener("click", (e) => {
  const g = e.target.dataset.genre;
  if (!g) return;
  const feld = genreFeld || $("#rg-genres");
  const liste = feld.value.split(",").map((x) => x.trim()).filter(Boolean);
  const pos = liste.findIndex((x) => x.toLowerCase() === g.toLowerCase());
  pos >= 0 ? liste.splice(pos, 1) : liste.push(g);
  feld.value = liste.join(", ");
  genreChips();
});
$("#regal-form").addEventListener("submit", (e) => {
  e.preventDefault();
  versuche(async () => {
    const daten = formDaten(e.target);
    const fach_genres = [...$("#rg-fach-genres").querySelectorAll("input")].map((i) => i.value);
    zeichneRegale(await post("regal_save", { ...daten, fach_genres, id: Number(daten.regal_id) || null }));
    $("#regal-dialog").close();
    toast("Regal gespeichert");
  });
});
$("#regale-liste").addEventListener("click", (e) => {
  const d = e.target.dataset;
  if (d.isbn) return oeffneVorschau(d.isbn);
  if (d.bearbeiten) return oeffneRegal(regalDaten.regale.find((r) => r.id === Number(d.bearbeiten)));
  if (d.hoch || d.runter) return versuche(async () =>
    zeichneRegale(await post("regal_move", { id: Number(d.hoch || d.runter), richtung: d.hoch ? "hoch" : "runter" })));
  if (d.loeschen) {
    const r = regalDaten.regale.find((x) => x.id === Number(d.loeschen));
    if (confirm(`Regal „${r.name}“ löschen? Die Bücher bleiben im Katalog und werden auf die anderen Regale verteilt.`))
      versuche(async () => { zeichneRegale(await post("regal_delete", { id: r.id })); toast("Regal gelöscht"); });
  }
});
$("#regale-liste").addEventListener("toggle", (e) => {
  if (e.target.dataset.regal) speicherSchreiben("regalOffen" + e.target.dataset.regal, e.target.open ? "1" : "0");
}, true);
$("#regal-uebernehmen").addEventListener("click", () => versuche(async () => {
  zeichneRegale(await post("regale_uebernehmen"));
  toast("Gespeichert – so stehen die Bücher jetzt im Regal");
}));

// --- Programm-Update ---
function zeigeUpdate(r) {
  $("#update-text").textContent = r.neu
    ? `Version ${r.verfuegbar} ist verfügbar (installiert: ${r.installiert}). Deine Daten bleiben beim Update erhalten, ScanBook startet danach von selbst neu.`
    : `ScanBook ist auf dem neuesten Stand (Version ${r.installiert}).`;
  $("#update-install").classList.toggle("hidden", !r.neu);
  $("#update-install").textContent = r.neu ? `Version ${r.verfuegbar} installieren` : "";
  $("#update-hinweis").classList.toggle("hidden", !r.neu);
  $("#update-hinweis").textContent = `Update auf ${r.verfuegbar}`;
}
$("#update-check").addEventListener("click", () => versuche(async () => {
  const knopf = $("#update-check");
  knopf.disabled = true;
  knopf.textContent = "Suche …";
  try { zeigeUpdate(await get("update_check")); } finally { knopf.disabled = false; knopf.textContent = "Nach Updates suchen"; }
}));
$("#update-hinweis").addEventListener("click", () => {
  zeigeTab("verwaltung");
  $("#update-karte").scrollIntoView({ behavior: "smooth" });
});
$("#update-install").addEventListener("click", () => {
  if (!confirm("Update jetzt installieren? ScanBook startet danach neu, die Seite lädt sich von selbst neu.")) return;
  versuche(async () => {
    const knopf = $("#update-install");
    knopf.disabled = true;
    knopf.textContent = "Installiere …";
    const alt = info.version;
    const r = await post("update_install");
    $("#update-text").textContent = `Version ${r.version} installiert. ScanBook startet neu …`;
    // Warten, bis der neu gestartete Server antwortet, dann die Seite neu laden
    for (let i = 0; i < 40; i++) {
      await new Promise((ok) => setTimeout(ok, 1000));
      const neu = await get("info").catch(() => null);
      if (neu && neu.version !== alt) { location.reload(); return; }
    }
    knopf.disabled = false;
    knopf.textContent = "Jetzt installieren";
    toast("ScanBook antwortet nicht. Bitte im Terminal neu starten und die Seite neu laden.", true);
  });
});

// --- Ausleihe ---
let ausleihen = [];
let markierteIsbn = null;

async function ladeNamen() {
  const [namen, sc] = await Promise.all([get("borrowers").catch(() => []), get("schueler").catch(() => ({ schueler: [] }))]);
  // Schüler mit Klasse, dazu frühere Namen (z. B. Lehrkräfte), die kein Schüler sind
  const schuelerNamen = new Set(sc.schueler.map((x) => x.name.toLowerCase()));
  const vorschlaege = [...sc.schueler.map((x) => x.klasse ? `${x.name} (${x.klasse})` : x.name),
    ...namen.filter((n) => !schuelerNamen.has(n.toLowerCase()))];
  $("#namen-liste").innerHTML = vorschlaege.map((n) => `<option value="${esc(n)}">`).join("");
}

let ausleihStatus = "alle";
const AUSLEIH_STATUS = { alle: () => true, ueber: (a) => a.tage < 0, bald: (a) => a.tage >= 0 && a.tage <= 2 };
async function ladeAusleihen() {
  await versuche(async () => {
    const neu = await getNeu("loans");
    if (!neu && $("#loans-body").children.length) return;  // nichts geändert
    if (neu) ausleihen = neu;
    const anzahl = (k) => ausleihen.filter(AUSLEIH_STATUS[k]).length;
    const ueber = anzahl("ueber"), bald = anzahl("bald");
    const personen = new Set(ausleihen.map((a) => a.ausleiher.toLowerCase())).size;
    $("#ausleihe-tiles").innerHTML = [
      [ausleihen.length, "Ausgeliehen", "alle", ""],
      [ueber, "Überfällig", "ueber", ueber ? "alert" : ""],
      [bald, "Bald fällig", "bald", bald ? "warn" : ""],
      [personen, personen === 1 ? "Person mit Büchern" : "Personen mit Büchern", "", ""],
    ].map(([wert, label, status, art]) =>
      `<div class="tile ${art}" ${status ? `data-status="${status}" role="button" tabindex="0"` : ""}><div class="value">${wert}</div><div class="label">${label}</div></div>`).join("");
    $("#anz-alle").textContent = ausleihen.length;
    $("#anz-ueber").textContent = ueber;
    $("#anz-bald").textContent = bald;
    zeichneAusleihen();
  });
}

function setzeAusleihStatus(status) {
  ausleihStatus = status;
  document.querySelectorAll("#loans-status button").forEach((b) => b.classList.toggle("active", b.dataset.status === status));
  zeichneAusleihen();
}
$("#loans-status").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-status]");
  if (b) setzeAusleihStatus(b.dataset.status);
});
$("#ausleihe-tiles").addEventListener("click", (e) => {
  const t = e.target.closest("[data-status]");
  if (!t) return;
  setzeAusleihStatus(t.dataset.status);
  $("#loans-status").scrollIntoView({ behavior: "smooth", block: "start" });
});

function ausleihZeile(a) {
  return `
    <tr class="${markierteIsbn ? "highlight" : ""}">
      <td><b>${esc(a.titel)}</b><div class="sub">${esc(a.autor)}${a.exemplar ? ` · ${esc(a.exemplar)}` : ""}</div></td>
      <td>${esc(a.ausleiher)}${a.klasse ? `<div class="sub">Klasse ${esc(a.klasse)}</div>` : ""}</td>
      <td class="num">${deDatum(a.ausgeliehen_am)}</td>
      <td class="num">${deDatum(a.rueckgabe_bis)}${a.verlaengerungen ? `<div class="sub">${a.verlaengerungen}× verlängert</div>` : ""}</td>
      <td>${statusBadge(a.tage)}</td>
      <td style="white-space:nowrap">
        <button class="btn small" data-return="${a.id}">Zurückgeben</button>
        <button class="btn small secondary" data-extend="${a.id}">Verlängern</button>
      </td>
    </tr>`;
}
function zeichneAusleihen() {
  const filter = $("#loans-filter").value.trim().toLowerCase();
  const liste = ausleihen.filter((a) =>
    (!markierteIsbn || a.isbn === markierteIsbn) &&
    (markierteIsbn || AUSLEIH_STATUS[ausleihStatus](a)) &&
    (!filter || [a.ausleiher, a.titel, a.klasse, a.exemplar].some((x) => x.toLowerCase().includes(filter))));
  const leer = `<tr><td colspan="6" class="empty">${ausleihen.length ? "Keine passenden Ausleihen." : "Keine Bücher ausgeliehen – alle Exemplare sind in der Bücherei."}</td></tr>`;
  stueckweise($("#loans-body"), $("#loans-mehr"), liste, ausleihZeile, leer, true);
}
$("#loans-filter").addEventListener("input", zeichneAusleihen);

async function zurueckgeben(id) {
  await versuche(async () => {
    const r = await post("return", { id });
    toast(`„${r.titel}“ von ${r.ausleiher} zurückgegeben` + (r.verspaetung ? ` – ${r.verspaetung} Tag(e) zu spät` : ""));
    markierteIsbn = null;
    $("#re-info").textContent = "Oder unten in der Liste auswählen.";
    $("#re-info").classList.remove("error");
    await ladeAusleihen();
    $("#re-isbn").focus();
  });
}
async function verlaengern(id) {
  const eingabe = prompt("Um wie viele Tage verlängern?", info.standard_leihdauer);
  if (eingabe === null) return;
  await versuche(async () => {
    const r = await post("extend", { id, tage: eingabe });
    toast(`„${r.titel}“ verlängert bis ${deDatum(r.rueckgabe_bis)}`);
    await ladeAusleihen();
  });
}
$("#loans-body").addEventListener("click", (e) => {
  if (e.target.dataset.return) zurueckgeben(Number(e.target.dataset.return));
  if (e.target.dataset.extend) verlaengern(Number(e.target.dataset.extend));
});

// Ausleihen: erst die Person (Ausweis oder Name), dann beliebig viele Bücher
let ausleihPerson = null;
function zeigeAusleihPerson(p) {
  ausleihPerson = p;
  const box = $("#co-person");
  box.classList.remove("error");
  $("#co-isbn").disabled = !p;
  $("#co-fertig").disabled = !p;
  if (!p) { box.textContent = "Noch niemand ausgewählt."; return; }
  const ueber = (p.ausleihen || []).filter((a) => a.tage < 0).length;
  const anzahl = (p.ausleihen || []).length;
  const kuerzel = p.name.split(/\s+/).map((t) => t[0] || "").slice(0, 2).join("").toUpperCase();
  box.innerHTML = `<div class="person-box"><div class="avatar">${esc(kuerzel)}</div><div>
    <b>${esc(p.name)}</b>${p.klasse ? ` · Klasse ${esc(p.klasse)}` : ""}${p.ausweis ? ` · ${codeHtml(p.ausweis)}` : ""}
    <div class="sub">${p.id ? (anzahl ? `Hat ${anzahl} ${anzahl === 1 ? "Buch" : "Bücher"} ausgeliehen` : "Hat gerade nichts ausgeliehen") : "Kein Schüler aus der Liste – wird als Name eingetragen"}</div>
    ${ueber ? `<span class="badge danger">${ueber} überfällig</span>` : ""}</div></div>`;
}
async function waehlePerson(eingabe) {
  eingabe = eingabe.trim();
  $("#co-log").innerHTML = "";
  if (!eingabe) { zeigeAusleihPerson(null); return; }
  try {
    const r = await get("code", { c: eingabe });
    if (r.typ === "schueler") { zeigeAusleihPerson(r.schueler); return true; }
    throw new Error("Das ist ein Buch, kein Ausweis. Bitte zuerst den Schülerausweis scannen.");
  } catch (e) {
    if (!/weder ein Schülerausweis/.test(e.message)) {
      zeigeAusleihPerson(null);
      $("#co-person").classList.add("error");
      $("#co-person").textContent = e.message;
      return false;
    }
  }
  // Kein Ausweis: Schüler aus der Vorschlagsliste ("Name (Klasse)") oder ein frei eingegebener Name
  const sc = await get("schueler", { q: eingabe.replace(/\s*\([^()]*\)$/, "") }).catch(() => ({ schueler: [] }));
  const treffer = sc.schueler.filter((x) => [x.name, `${x.name} (${x.klasse})`].some((n) => n.toLowerCase() === eingabe.toLowerCase()));
  if (treffer.length === 1) {
    const r = await get("code", { c: treffer[0].ausweis });
    zeigeAusleihPerson(r.schueler);
  } else {
    zeigeAusleihPerson({ name: eingabe });
  }
  return true;
}
$("#person-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (await waehlePerson($("#co-name").value)) $("#co-isbn").focus();
});
$("#co-name").addEventListener("change", () => waehlePerson($("#co-name").value));
// Das Formular hat zwei Felder und keinen Absenden-Knopf, darum [Enter] im Buchfeld selbst abfangen
$("#co-isbn").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("#checkout-form").requestSubmit(); }
});
$("#co-fertig").addEventListener("click", () => {
  $("#co-name").value = "";
  zeigeAusleihPerson(null);
  $("#co-log").innerHTML = "";
  $("#co-name").focus();
});
$("#checkout-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const feld = $("#co-isbn");
  const code = feld.value.trim();
  if (!code || !ausleihPerson) return;
  feld.value = "";
  versuche(async () => {
    // Nächsten Ausweis gescannt? Dann einfach die Person wechseln
    const was = await get("code", { c: code });
    if (was.typ === "schueler") {
      $("#co-name").value = was.schueler.ausweis;
      zeigeAusleihPerson(was.schueler);
      $("#co-log").innerHTML = "";
      return;
    }
    if (was.ausgeliehen_an) throw new Error(`Das Exemplar ${was.exemplar} ist noch an ${was.ausgeliehen_an} ausgeliehen. Bitte erst zurückgeben.`);
    const r = await post("checkout", { code, ausleiher: ausleihPerson.ausweis || ausleihPerson.name, tage: $("#co-tage").value });
    toast(`„${r.titel}“ an ${r.ausleiher} ausgeliehen – bis ${deDatum(r.rueckgabe_bis)}`);
    $("#co-log").insertAdjacentHTML("afterbegin",
      `<li>✅ <span><b>${esc(r.titel)}</b>${r.exemplar ? " " + codeHtml(r.exemplar) : ""}</span><span class="time">bis ${deDatum(r.rueckgabe_bis)}</span></li>`);
    if (ausleihPerson.ausweis) zeigeAusleihPerson((await get("code", { c: ausleihPerson.ausweis })).schueler);
    await ladeAusleihen();
  }).finally(() => feld.focus());
});

// Rückgabe per Scan
$("#return-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const feld = $("#re-isbn");
  const isbn = feld.value.trim();
  const infoBox = $("#re-info");
  feld.value = "";
  infoBox.classList.remove("error");
  if (!isbn) { markierteIsbn = null; zeichneAusleihen(); return; }
  versuche(async () => {
    try {
      const treffer = await get("loans", { code: isbn });
      if (treffer.length === 1 && treffer[0].exemplar.toLowerCase() === isbn.replace(/[ßẞ]/g, "-").toLowerCase()) {
        return zurueckgeben(treffer[0].id);  // Mediencode: eindeutig, ohne Rückfrage
      }
      if (!treffer.length) {
        markierteIsbn = null;
        infoBox.classList.add("error");
        infoBox.textContent = "Für dieses Buch gibt es keine aktive Ausleihe.";
      } else if (treffer.length === 1) {
        const a = treffer[0];
        if (confirm(`„${a.titel}“ von ${a.ausleiher} zurückgeben?`)) return zurueckgeben(a.id);
        markierteIsbn = a.isbn;
        infoBox.textContent = "Ausleihe unten hervorgehoben.";
      } else {
        markierteIsbn = treffer[0].isbn;
        infoBox.innerHTML = `📖 <b>${esc(treffer[0].titel)}</b> ist ${treffer.length}× ausgeliehen – bitte unten die richtige Person wählen.`;
      }
      zeichneAusleihen();
    } catch (err) {
      infoBox.classList.add("error");
      infoBox.textContent = err.message;
    }
  });
});

// --- Schüler ---
let schuelerDaten = { schueler: [], klassen: [] };
let schuelerAnsicht = "liste";
function zeigeSchuelerAnsicht(ansicht, laden = true) {
  schuelerAnsicht = ansicht;
  document.querySelectorAll("#schueler-ansicht button").forEach((b) => b.classList.toggle("active", b.dataset.ansicht === ansicht));
  $("#ansicht-liste").classList.toggle("hidden", ansicht !== "liste");
  $("#ansicht-verlauf").classList.toggle("hidden", ansicht !== "verlauf");
  if (laden) LADER.schueler();
}
$("#schueler-ansicht").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-ansicht]");
  if (b) zeigeSchuelerAnsicht(b.dataset.ansicht);
});
let aktiveKlasse = null;
async function ladeSchueler() {
  await versuche(async () => {
    const params = { q: $("#schueler-suche").value };
    if (aktiveKlasse !== null) params.klasse = aktiveKlasse;
    schuelerDaten = await get("schueler", params);
    if (aktiveKlasse !== null && !schuelerDaten.klassen.includes(aktiveKlasse)) { aktiveKlasse = null; return ladeSchueler(); }
    const liste = schuelerDaten.schueler;
    $("#klassen-chips").innerHTML = schuelerDaten.klassen.length
      ? [`<button class="chip ${aktiveKlasse === null ? "active" : ""}" data-alle="1">Alle</button>`,
        ...schuelerDaten.klassen.map((k) => `<button class="chip ${k === aktiveKlasse ? "active" : ""}" data-klasse="${esc(k)}">${k ? esc(k) : "ohne Klasse"}</button>`)].join("")
      : "";
    $("#klassen-liste").innerHTML = schuelerDaten.klassen.filter(Boolean).map((k) => `<option value="${esc(k)}">`).join("");
    $("#schueler-anzahl").textContent = liste.length
      ? `${liste.length} Schüler${aktiveKlasse ? ` in Klasse ${aktiveKlasse}` : ""}. Klicke auf einen Namen zum Bearbeiten.`
      : "";
    $("#ausweise-drucken").disabled = !liste.length;
    $("#ausweise-drucken").textContent = aktiveKlasse ? `Ausweise Klasse ${aktiveKlasse} drucken` : "Alle Ausweise drucken";
    const leer = `<tr><td colspan="5" class="empty">${$("#schueler-suche").value.trim() ? "Niemand gefunden." : "Noch keine Schüler. Lege sie einzeln an oder importiere eine Klassenliste."}</td></tr>`;
    stueckweise($("#schueler-body"), $("#schueler-mehr"), liste, (x) => `
      <tr class="clickable" data-id="${x.id}">
        <td><b>${esc(x.nachname)}</b>, ${esc(x.vorname)}</td>
        <td>${esc(x.klasse)}</td>
        <td>${codeHtml(x.ausweis)}</td>
        <td>${x.aktiv ? `${x.aktiv} ${x.aktiv === 1 ? "Buch" : "Bücher"}` : `<span class="sub">–</span>`}${x.ueberfaellig ? ` <span class="badge danger">${x.ueberfaellig} überfällig</span>` : ""}</td>
        <td style="text-align:right"><button class="btn small secondary" type="button" data-ausweis="${x.id}">Ausweis</button></td>
      </tr>`, leer);
  });
}
$("#schueler-suche").addEventListener("input", debounce(ladeSchueler));
$("#klassen-chips").addEventListener("click", (e) => {
  const d = e.target.dataset;
  if (d.alle) aktiveKlasse = null;
  else if (d.klasse !== undefined) aktiveKlasse = d.klasse;
  else return;
  ladeSchueler();
});
$("#ausweise-drucken").addEventListener("click", () =>
  drucken("ausweise", aktiveKlasse !== null ? { klasse: aktiveKlasse } : {}));
$("#schueler-body").addEventListener("click", (e) => {
  if (e.target.dataset.ausweis) return drucken("ausweise", { ids: e.target.dataset.ausweis });
  const zeile = e.target.closest("tr[data-id]");
  if (zeile) oeffneSchueler(schuelerDaten.schueler.find((x) => x.id === Number(zeile.dataset.id)));
});
async function oeffneSchueler(x) {
  const form = $("#schueler-form");
  form.reset();
  form.id.value = x?.id ?? "";
  for (const f of ["vorname", "nachname", "klasse", "ausweis"]) form[f].value = x?.[f] ?? "";
  if (!x && aktiveKlasse) form.klasse.value = aktiveKlasse;
  $("#schueler-dlg-titel").textContent = x ? x.name : "Neuer Schüler";
  $("#sc-drucken").classList.toggle("hidden", !x);
  $("#sc-loeschen").classList.toggle("hidden", !x);
  $("#sc-ausleihen-block").classList.add("hidden");
  $("#schueler-dialog").showModal();
  form.vorname.focus();
  if (x?.aktiv) {
    const r = await get("code", { c: x.ausweis }).catch(() => null);
    if (!r) return;
    $("#sc-ausleihen").innerHTML = r.schueler.ausleihen.map((a) =>
      `<li><span>${esc(a.titel)}</span>${a.exemplar ? codeHtml(a.exemplar) : ""}<span class="time">bis ${deDatum(a.bis)}</span>${statusBadge(a.tage)}</li>`).join("");
    $("#sc-ausleihen-block").classList.remove("hidden");
  }
}
$("#schueler-neu").addEventListener("click", () => oeffneSchueler(null));
$("#sc-close").addEventListener("click", () => $("#schueler-dialog").close());
$("#sc-drucken").addEventListener("click", () => drucken("ausweise", { ids: $("#schueler-form").id.value }));
$("#schueler-form").addEventListener("submit", (e) => {
  e.preventDefault();
  versuche(async () => {
    const daten = formDaten(e.target);
    const neu = !daten.id;
    const x = await post("schueler_save", { ...daten, id: Number(daten.id) || null });
    toast(`${x.name} gespeichert – Ausweis ${x.ausweis}`);
    if (neu) {
      // Gleich den nächsten Schüler derselben Klasse erfassen
      const form = $("#schueler-form");
      for (const f of ["vorname", "nachname", "ausweis"]) form[f].value = "";
      form.vorname.focus();
    } else {
      $("#schueler-dialog").close();
    }
    ladeSchueler();
  });
});
$("#sc-loeschen").addEventListener("click", () => {
  const form = $("#schueler-form");
  if (!confirm(`${form.vorname.value} ${form.nachname.value} löschen? Frühere Ausleihen bleiben mit Namen in der Historie.`)) return;
  versuche(async () => {
    await post("schueler_delete", { id: Number(form.id.value) });
    toast("Schüler gelöscht");
    $("#schueler-dialog").close();
    ladeSchueler();
  });
});
$("#schueler-import-btn").addEventListener("click", () => {
  $("#import-form").reset();
  $("#import-dialog").showModal();
  $("#import-text").focus();
});
$("#import-close").addEventListener("click", () => $("#import-dialog").close());
$("#import-datei").addEventListener("change", async (e) => {
  const datei = e.target.files[0];
  if (!datei) return;
  const bytes = new Uint8Array(await datei.arrayBuffer());
  // Excel speichert CSV oft als Windows-1252 statt UTF-8
  let inhalt;
  try { inhalt = new TextDecoder("utf-8", { fatal: true }).decode(bytes); } catch { inhalt = new TextDecoder("windows-1252").decode(bytes); }
  $("#import-text").value = inhalt;
});
$("#import-form").addEventListener("submit", (e) => {
  e.preventDefault();
  versuche(async () => {
    const r = await post("schueler_import", { text: $("#import-text").value });
    toast(`${r.neu} Schüler importiert` + (r.vorhanden ? `, ${r.vorhanden} gab es schon` : ""), false);
    if (r.fehler.length) alert("Diese Zeilen wurden nicht übernommen:\n\n" + r.fehler.slice(0, 20).join("\n") + (r.fehler.length > 20 ? `\n… und ${r.fehler.length - 20} weitere` : ""));
    else $("#import-dialog").close();
    ladeSchueler();
  });
});

// --- Ausleiher ---
let aktivePerson = null;
async function ladePersonen() {
  await versuche(async () => {
    const namen = await get("borrowers", { q: $("#personen-suche").value });
    const zeigen = namen.slice(0, 40);
    $("#personen-liste").innerHTML = namen.length
      ? zeigen.map((n) => `<button class="chip ${n === aktivePerson ? "active" : ""}" data-name="${esc(n)}">${esc(n)}</button>`).join("")
        + (namen.length > zeigen.length ? `<span class="hint" style="align-self:center;margin:0">… und ${namen.length >= 500 ? "viele" : namen.length - zeigen.length} weitere – tippe einen Namen ein.</span>` : "")
      : `<span class="hint">Niemand gefunden.</span>`;
  });
}
$("#personen-suche").addEventListener("input", debounce(ladePersonen));
$("#personen-liste").addEventListener("click", (e) => {
  if (e.target.dataset.name) zeigePerson(e.target.dataset.name);
});
async function zeigePerson(name) {
  aktivePerson = name;
  document.querySelectorAll("#personen-liste .chip").forEach((c) => c.classList.toggle("active", c.dataset.name === name));
  await versuche(async () => {
    const p = await get("borrower", { name });
    const aktivZeilen = p.aktiv.map((a) => `
      <tr><td><b>${esc(a.titel)}</b></td><td class="num">${deDatum(a.ausgeliehen_am)}</td><td class="num">${deDatum(a.rueckgabe_bis)}</td><td>${statusBadge(a.tage)}</td></tr>`).join("");
    const frueherZeilen = p.frueher.map((a) => `
      <tr><td>${esc(a.titel)}</td><td class="num">${deDatum(a.ausgeliehen_am)}</td><td class="num">${deDatum(a.zurueckgegeben_am)}</td></tr>`).join("");
    $("#person-detail").innerHTML = `
      <div class="card">
        <h2>👤 ${esc(p.name)}</h2>
        <h3 style="margin-top:16px">Aktuell ausgeliehen (${p.aktiv.length})</h3>
        ${p.aktiv.length ? `<div class="table-wrap" style="margin-bottom:0"><table><thead><tr><th>Buch</th><th>Geliehen am</th><th>Rückgabe bis</th><th>Status</th></tr></thead><tbody>${aktivZeilen}</tbody></table></div>` : `<p class="hint">Nichts.</p>`}
      </div>
      <div class="card">
        <h3>Frühere Ausleihen (${p.frueher.length})</h3>
        ${p.frueher.length ? `<div class="table-wrap"><table><thead><tr><th>Buch</th><th>Geliehen am</th><th>Zurückgegeben am</th></tr></thead><tbody>${frueherZeilen}</tbody></table></div>` : `<p class="hint">Keine.</p>`}
      </div>`;
  });
}

// --- Statistik ---
async function ladeStatistik() {
  await versuche(async () => {
    const s = await get("stats");
    const tiles = [
      [s.titel, "Verschiedene Titel"],
      [s.exemplare, "Exemplare gesamt"],
      [s.verfuegbar, "Davon verfügbar"],
      [s.aktiv, `Aktive Ausleihen (${s.personen} Person${s.personen === 1 ? "" : "en"})`],
      [s.ueberfaellig, "Überfällig", s.ueberfaellig > 0],
    ];
    $("#stat-tiles").innerHTML = tiles.map(([wert, label, alarm]) =>
      `<div class="tile ${alarm ? "alert" : ""}"><div class="value">${wert}</div><div class="label">${label}</div></div>`).join("");
    $("#stat-top").innerHTML = s.top.length
      ? `<ol class="top">${s.top.map((t) => `<li>${esc(t.titel)} <span class="sub" style="color:var(--muted)">(${t.anzahl}×)</span></li>`).join("")}</ol>`
      : `<p class="hint">Noch keine Ausleihen.</p>`;
  });
}
$("#backup-btn").addEventListener("click", () => versuche(async () => {
  const r = await post("backup");
  toast("Backup gespeichert: " + r.pfad.split(/[\\/]/).slice(-2).join("/"));
  ladeBackups();
}));
function zeigeBackups(d) {
  const zeit = d.letztes ? new Date(d.letztes).toLocaleString("de-DE", { dateStyle: "medium", timeStyle: "short" }) : "";
  $("#backup-info").textContent = `Einmal täglich wird automatisch gesichert, die letzten ${d.behalten} Tage bleiben erhalten. `
    + (zeit ? `Letztes Backup: ${zeit} · ${d.anzahl} Backup${d.anzahl === 1 ? "" : "s"} im Ordner „backups“.` : "Noch kein Backup vorhanden.");
  if (document.activeElement !== $("#backup-ordner")) $("#backup-ordner").value = d.zweiter_ordner;
  $("#backup-warnung").classList.toggle("hidden", !d.kopie_fehler);
  $("#backup-warnung").textContent = d.kopie_fehler ? `⚠️ Kopie in den zweiten Ordner hat nicht geklappt: ${d.kopie_fehler}` : "";
}
async function ladeBackups() {
  await versuche(async () => zeigeBackups(await get("backups")));
}
$("#backup-ordner-btn").addEventListener("click", () => versuche(async () => {
  const d = await post("backup_ordner", { ordner: $("#backup-ordner").value.trim() });
  zeigeBackups(d);
  toast(d.zweiter_ordner ? "Backups werden ab jetzt auch dorthin kopiert" : "Es wird nur noch im Ordner „backups“ gesichert");
}));

// --- Einstellungen ---
async function ladeEinstellungen() {
  await versuche(async () => {
    const liste = await get("settings");
    $("#settings-fields").innerHTML = liste.map((e) => `
      <div><label for="st-${e.key}">${esc(e.label)}</label>
      <input id="st-${e.key}" name="${e.key}" value="${esc(e.wert)}" ${e.zahl ? 'type="number" min="0"' : ""}></div>`).join("");
  });
}
$("#settings-form").addEventListener("submit", (e) => {
  e.preventDefault();
  versuche(async () => {
    await post("settings", { werte: formDaten(e.target) });
    toast("Einstellungen gespeichert");
    await ladeInfo();
  });
});

// --- Start ---
ladeInfo().catch((e) => toast(e.message, true));
katalogHolen().catch(() => {});
zeigeTab(location.hash.slice(1));
get("update_check").then(zeigeUpdate).catch(() => {});  // still im Hintergrund, Fehler nicht anzeigen
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
